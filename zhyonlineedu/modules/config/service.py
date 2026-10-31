"""双模式切换向导（§9.3 / §9.5 / §9.6 · M2.10~2.11）。

口径：
- **切换不改表**（R5）：只更新 `sys_mode_state` 单行 + `sys_mode_config` 的策略行，
  `epoch` 每次 +1，请求内快照的不半半保证由 `core.mode.context` 提供；
- **前置预检 4 原条 + 3 新增**（§9.6）：原 4 条为模式合法性 / 转码在途 / 在途作答 /
  课程可见性；新增 3 条为有效票据报告、`source=preview` 流水隔离、`sys_compat_flags` 一致性；
  另加一条自注册账号处置（§9.5，`migration_batch_id` 可回溯、可批量恢复）；
- **切换事务内处置**：`learn_progress_log` 窗口内 `source=preview` 行标 `quarantined=true`；
  对外→对内时自注册账号 `status=disabled` + `token_version+1` + 吊销全部会话 +
  写 `migration_batch_id`；全程写一条 `sys_log_security(MODE_SWITCH)` 审计；
- 存量播放票据**不失效**（TTL ≤ 15min，避免切换瞬间打断在途播放），新签发按新模式。
"""

from __future__ import annotations

import json
import secrets
from collections.abc import Mapping
from dataclasses import asdict, dataclass, replace
from datetime import datetime, timedelta
from typing import Any, cast

from sqlalchemy import func, select, update
from sqlalchemy.engine import CursorResult
from sqlalchemy.orm import Session

from zhyonlineedu.core.codes import (
    MODE_SWITCH_BLOCKED,
    MODE_UNCHANGED,
    VALIDATION_FAILED,
)
from zhyonlineedu.core.config import Config
from zhyonlineedu.core.db import probe
from zhyonlineedu.core.db.engine import Database, ensure_database
from zhyonlineedu.core.db.models.account import AccountSession, AccountUser, SysLogSecurity
from zhyonlineedu.core.db.models.catalog import EduCourse
from zhyonlineedu.core.db.models.learn import LearnProgressLog
from zhyonlineedu.core.db.models.media import MediaAsset, MediaKeySession
from zhyonlineedu.core.db.models.quiz import QuizAttempt
from zhyonlineedu.core.db.models.system import (
    SysConfig,
    SysModeConfig,
    SysModeState,
    utcnow,
)
from zhyonlineedu.core.db.query import not_deleted
from zhyonlineedu.core.errors import AppError
from zhyonlineedu.core.mode import ACTIVE_MODE_KEY, DEFAULT_POLICIES, MODES, get_mode_context

# 自注册来源（§9.5）：切换对外→对内时按 disabled_readonly 处置
SELF_REGISTERED = ("self", "open_admin")
TRANSCODE_ACTIVE = ("pending", "uploaded", "transcoding")


def _iso(value: datetime | None) -> str | None:
    return value.isoformat(timespec="seconds") if value is not None else None


def _mode(value: object) -> str:
    text = str(value or "").strip()
    if text not in MODES:
        raise AppError(
            VALIDATION_FAILED,
            data={"errors": [f"target_mode 必须是 {'/'.join(MODES)} 之一"]},
        )
    return text


def _int(value: object, field: str, *, minimum: int = 0) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise AppError(VALIDATION_FAILED, data={"errors": [f"{field} 必须是整数"]})
    if value < minimum:
        raise AppError(VALIDATION_FAILED, data={"errors": [f"{field} 不能小于 {minimum}"]})
    return value


@dataclass(frozen=True, slots=True)
class ModeCounts:
    """预检与切换报告共用的计数（前端直接展示）。"""

    active_tickets: int = 0
    preview_logs: int = 0
    quarantined_logs: int = 0
    affected_users: int = 0
    revoked_sessions: int = 0
    quiz_in_progress: int = 0
    transcode_active: int = 0
    courses_hidden: int = 0


class ModeService:
    """无状态服务：读状态 / 预检 / 切换，全部自建短事务。"""

    def __init__(self, config: Config, *, db: Database | None = None) -> None:
        self.config = config
        self._db = db

    @property
    def db(self) -> Database:
        if self._db is None:
            self._db = ensure_database(self.config)
        return self._db

    # ── 读 ────────────────────────────────────────────────────────────

    def state(self) -> dict[str, Any]:
        with Session(self.db.engine) as session:
            row = self._state_row(session)
            counts = self._counts(session, target=row.active_mode, now=utcnow())
        return {**self._state_out(row), "counts": asdict(counts)}

    def summary(self) -> dict[str, Any]:
        """向导首屏：当前模式 + 另一模式 + 策略 + 计数。"""
        with Session(self.db.engine) as session:
            row = self._state_row(session)
            other = "external" if row.active_mode == "internal" else "internal"
            counts = self._counts(session, target=other, now=utcnow())
        return {
            **self._state_out(row),
            "other_mode": other,
            "policy": self._policy_out(row.active_mode),
            "counts": asdict(counts),
        }

    # ── 预检 ──────────────────────────────────────────────────────────

    def precheck(self, body: Mapping[str, Any]) -> dict[str, Any]:
        target = _mode(body.get("target_mode"))
        with Session(self.db.engine) as session:
            row = self._state_row(session)
            current = row.active_mode
            epoch = int(row.epoch)
            if target == current:
                raise AppError(
                    MODE_UNCHANGED,
                    data={"mode": current, "epoch": epoch, "reason": "mode_unchanged"},
                )
            checks, counts = self._checks(session, current=current, target=target, now=utcnow())
        blockers = [item["code"] for item in checks if item["status"] == "blocked"]
        warnings = [item["code"] for item in checks if item["status"] == "warn"]
        return {
            "target_mode": target,
            "current_mode": current,
            "epoch": epoch,
            "ready": not blockers,
            "checks": checks,
            "blockers": blockers,
            "warnings": warnings,
            "counts": asdict(counts),
        }

    # ── 切换 ──────────────────────────────────────────────────────────

    def switch(self, body: Mapping[str, Any], *, operator_id: int | None) -> dict[str, Any]:
        target = _mode(body.get("target_mode"))
        expected = _int(body.get("epoch"), "epoch", minimum=1)
        reason = str(body.get("reason") or "").strip()[:200]
        now = utcnow()

        with Session(self.db.engine) as session:
            row = self._state_row(session)
            current = row.active_mode
            epoch = int(row.epoch)
            if target == current:
                raise AppError(
                    MODE_UNCHANGED,
                    data={"mode": current, "epoch": epoch, "reason": "mode_unchanged"},
                )
            if expected != epoch:
                # 乐观锁：前端拿到的 epoch 已过期（别人先切了）
                raise AppError(
                    MODE_SWITCH_BLOCKED,
                    data={
                        "mode": current,
                        "epoch": epoch,
                        "expected_epoch": expected,
                        "reason": "epoch_conflict",
                    },
                )
            checks, _ = self._checks(session, current=current, target=target, now=now)
            blockers = [item["code"] for item in checks if item["status"] == "blocked"]
            if blockers:
                raise AppError(
                    MODE_SWITCH_BLOCKED,
                    data={
                        "mode": current,
                        "epoch": epoch,
                        "target_mode": target,
                        "blockers": blockers,
                        "checks": checks,
                    },
                )

            cutoff = now - timedelta(seconds=self.config.get_int("mode.quarantine_window_sec"))
            quarantined = self._quarantine(session, cutoff)
            batch_id = self._batch_id(now)
            affected, revoked = (0, 0)
            if target == "internal":
                affected, revoked = self._disable_self_registered(
                    session, batch_id=batch_id, now=now
                )

            row.active_mode = target
            row.previous_mode = current
            row.epoch = epoch + 1
            row.switched_at = now
            row.switched_by = operator_id
            row.migration_batch_id = batch_id if affected else row.migration_batch_id
            row.version = int(row.version) + 1
            row.updated_at = now

            self._put_config(session, ACTIVE_MODE_KEY, target)
            self._put_policy(session, target)
            self._sync_compat(session, target)

            session.add(
                SysLogSecurity(
                    event="MODE_SWITCH",
                    user_id=operator_id,
                    detail=(
                        f"{current}->{target};epoch={epoch + 1};batch={batch_id};"
                        f"users={affected};sessions={revoked};quarantined={quarantined}"
                    )[:255],
                    created_at=now,
                )
            )
            session.commit()

            counts = replace(
                self._counts(session, target=target, now=now),
                quarantined_logs=quarantined,
                affected_users=affected,
                revoked_sessions=revoked,
            )
            snapshot = self._state_out(row, epoch=epoch + 1)

        self._invalidate()
        warnings = [item["code"] for item in checks if item["status"] == "warn"]
        return {
            **snapshot,
            "reason": reason,
            "warnings": warnings,
            "checks": checks,
            "counts": asdict(counts),
        }

    # ── 预检明细（4 原条 + 3 新增 + 账号处置）──────────────────────────

    def _checks(
        self, session: Session, *, current: str, target: str, now: datetime
    ) -> tuple[list[dict[str, Any]], ModeCounts]:
        checks: list[dict[str, Any]] = []

        def add(code: str, status: str, message: str, *, count: int | None = None) -> None:
            item: dict[str, Any] = {"code": code, "status": status, "message": message}
            if count is not None:
                item["count"] = count
            checks.append(item)

        # 1（原）目标模式存在且受支持
        add("mode_supported", "ok", "目标模式可用")
        # 2（原）转码队列空闲：在途任务不会因子策略变化错档
        transcode_active = int(
            session.execute(
                select(func.count())
                .select_from(MediaAsset)
                .where(MediaAsset.status.in_(TRANSCODE_ACTIVE))
            ).scalar_one()
        )
        add(
            "transcode_idle",
            "warn" if transcode_active else "ok",
            f"有 {transcode_active} 个转码任务在途，切换后仍按原有清晰度完成",
            count=transcode_active,
        )
        # 3（原）在途作答：切换后交卷按新 epoch，不影响快照
        quiz_active = int(
            session.execute(
                select(func.count())
                .select_from(QuizAttempt)
                .where(QuizAttempt.status == "in_progress")
            ).scalar_one()
        )
        add(
            "quiz_idle",
            "warn" if quiz_active else "ok",
            f"有 {quiz_active} 份作答在途，切换不影响已开始的作答",
            count=quiz_active,
        )
        # 4（原）课程可见性：切换后 0 门可看即为业务风险，只告警不阻断
        courses_hidden = int(
            session.execute(
                select(func.count())
                .select_from(EduCourse)
                .where(
                    not_deleted(EduCourse),
                    EduCourse.status == "published",
                    EduCourse.mode.notin_((target, "both")),
                )
            ).scalar_one()
        )
        add(
            "course_visibility",
            "warn" if courses_hidden else "ok",
            f"切换后 {courses_hidden} 门已上架课程对学员不可见（管理员仍可预览）",
            count=courses_hidden,
        )
        # 5（新）有效票据报告：存量不失效，数量超阈值告警
        active_tickets = int(
            session.execute(
                select(func.count())
                .select_from(MediaKeySession)
                .where(
                    MediaKeySession.revoked_at.is_(None),
                    MediaKeySession.expires_at > now,
                )
            ).scalar_one()
        )
        threshold = self.config.get_int("mode.ticket_warn_threshold")
        add(
            "active_tickets",
            "warn" if active_tickets > threshold else "ok",
            f"尚有 {active_tickets} 个有效播放凭证，切换后仍可继续播放",
            count=active_tickets,
        )
        # 6（新）进度流水 preview 污染：切换事务内隔离，不阻断
        cutoff = now - timedelta(seconds=self.config.get_int("mode.quarantine_window_sec"))
        preview_logs = int(
            session.execute(
                select(func.count())
                .select_from(LearnProgressLog)
                .where(
                    LearnProgressLog.source == "preview",
                    LearnProgressLog.received_at >= cutoff,
                    LearnProgressLog.quarantined.is_(False),
                )
            ).scalar_one()
        )
        add(
            "preview_pollution",
            "warn" if preview_logs else "ok",
            f"最近有 {preview_logs} 条试看记录，切换后不计入学习统计",
            count=preview_logs,
        )
        # 7（新）能力位一致性：禁止"猜测能力位下跑模式策略"
        drift = self._compat_drift(session, current)
        add(
            "compat_flags",
            "ok" if not drift else "blocked",
            ("系统环境检查通过" if not drift else "系统环境检查未通过，请联系技术支持处理"),
            count=len(drift),
        )
        # 8（新）自注册账号处置（仅对外→对内）
        affected = int(
            session.execute(
                select(func.count())
                .select_from(AccountUser)
                .where(
                    not_deleted(AccountUser),
                    AccountUser.status == "active",
                    AccountUser.registration_mode.in_(SELF_REGISTERED),
                )
            ).scalar_one()
        )
        if target == "internal":
            add(
                "self_registered",
                "warn" if affected else "ok",
                f"{affected} 个自助注册账号将被停用（可按批次批量恢复）",
                count=affected,
            )
        else:
            affected = 0
            add("self_registered", "ok", "对外模式无需停用自助注册账号", count=0)

        counts = ModeCounts(
            active_tickets=active_tickets,
            preview_logs=preview_logs,
            affected_users=affected,
            quiz_in_progress=quiz_active,
            transcode_active=transcode_active,
            courses_hidden=courses_hidden,
        )
        return checks, counts

    # ── 事务内处置 ────────────────────────────────────────────────────

    def _quarantine(self, session: Session, cutoff: datetime) -> int:
        result = cast(
            CursorResult[Any],
            session.execute(
                update(LearnProgressLog)
                .where(
                    LearnProgressLog.source == "preview",
                    LearnProgressLog.received_at >= cutoff,
                    LearnProgressLog.quarantined.is_(False),
                )
                .values(quarantined=True),
            ),
        )
        return int(result.rowcount or 0)

    def _disable_self_registered(
        self, session: Session, *, batch_id: str, now: datetime
    ) -> tuple[int, int]:
        """对外→对内：自注册账号处置为 disabled_readonly，批次可回溯可恢复（§9.5）。"""
        result = cast(
            CursorResult[Any],
            session.execute(
                update(AccountUser)
                .where(
                    not_deleted(AccountUser),
                    AccountUser.status == "active",
                    AccountUser.registration_mode.in_(SELF_REGISTERED),
                )
                .values(
                    status="disabled",
                    token_version=AccountUser.token_version + 1,
                    migration_batch_id=batch_id,
                    updated_at=now,
                )
            ),
        )
        affected = int(result.rowcount or 0)
        revoked = 0
        if affected:
            revoked_result = cast(
                CursorResult[Any],
                session.execute(
                    update(AccountSession)
                    .where(
                        AccountSession.user_id.in_(
                            select(AccountUser.id).where(AccountUser.migration_batch_id == batch_id)
                        ),
                        AccountSession.revoked_at.is_(None),
                    )
                    .values(
                        revoked_at=now,
                        revoked_by="system",
                        revoke_reason="mode_switch",
                    )
                ),
            )
            revoked = int(revoked_result.rowcount or 0)
            session.add(
                SysLogSecurity(
                    event="MODE_SWITCH_ACCOUNT",
                    user_id=None,
                    detail=f"batch={batch_id};users={affected};sessions={revoked}"[:255],
                    created_at=now,
                )
            )
        return affected, revoked

    # ── 内部工具 ──────────────────────────────────────────────────────

    def _state_row(self, session: Session) -> SysModeState:
        row = session.get(SysModeState, 1)
        if row is None:
            from zhyonlineedu.core.mode.context import ensure_state

            row = ensure_state(session)
        return row

    def _counts(self, session: Session, *, target: str, now: datetime) -> ModeCounts:
        cutoff = now - timedelta(seconds=self.config.get_int("mode.quarantine_window_sec"))
        return ModeCounts(
            active_tickets=int(
                session.execute(
                    select(func.count())
                    .select_from(MediaKeySession)
                    .where(
                        MediaKeySession.revoked_at.is_(None),
                        MediaKeySession.expires_at > now,
                    )
                ).scalar_one()
            ),
            preview_logs=int(
                session.execute(
                    select(func.count())
                    .select_from(LearnProgressLog)
                    .where(
                        LearnProgressLog.source == "preview",
                        LearnProgressLog.received_at >= cutoff,
                        LearnProgressLog.quarantined.is_(False),
                    )
                ).scalar_one()
            ),
            quiz_in_progress=int(
                session.execute(
                    select(func.count())
                    .select_from(QuizAttempt)
                    .where(QuizAttempt.status == "in_progress")
                ).scalar_one()
            ),
            transcode_active=int(
                session.execute(
                    select(func.count())
                    .select_from(MediaAsset)
                    .where(MediaAsset.status.in_(TRANSCODE_ACTIVE))
                ).scalar_one()
            ),
            courses_hidden=int(
                session.execute(
                    select(func.count())
                    .select_from(EduCourse)
                    .where(
                        not_deleted(EduCourse),
                        EduCourse.status == "published",
                        EduCourse.mode.notin_((target, "both")),
                    )
                ).scalar_one()
            ),
        )

    def _compat_drift(self, session: Session, mode: str) -> list[str]:
        row = session.get(SysModeConfig, (mode, "active"))
        persisted = self._json(row.compat_flags if row is not None else None)
        runtime = probe.load_flags(self.db.engine)
        drift = [
            cap
            for cap, value in sorted(persisted.items())
            if cap in runtime and bool(value) != bool(runtime[cap])
        ]
        return drift

    def _put_config(self, session: Session, key: str, value: str) -> None:
        row = session.get(SysConfig, key)
        if row is None:
            session.add(SysConfig(config_key=key, value=value, updated_at=utcnow()))
        else:
            row.value = value
            row.updated_at = utcnow()

    def _put_policy(self, session: Session, mode: str) -> None:
        base = DEFAULT_POLICIES[mode]
        policy = {
            "ttl_playlist": min(self.config.get_int("media.ttl_playlist"), base.ttl_playlist),
            "ttl_key": min(self.config.get_int("media.ttl_key"), base.ttl_key),
            "preview_seconds": base.preview_seconds,
            "progress_interval_sec": base.progress_interval_sec,
        }
        raw = json.dumps(policy, ensure_ascii=False, sort_keys=True)
        row = session.get(SysModeConfig, (mode, "policy"))
        if row is None:
            session.add(SysModeConfig(mode=mode, config_key="policy", value=raw, compat_flags="{}"))
        else:
            row.value = raw

    def _sync_compat(self, session: Session, mode: str) -> None:
        """目标模式 active 行的能力位以当前库为准（缺失才补建）。"""
        row = session.get(SysModeConfig, (mode, "active"))
        if row is None:
            caps = probe.load_flags(self.db.engine)
            session.add(
                SysModeConfig(
                    mode=mode,
                    config_key="active",
                    value="1",
                    compat_flags=json.dumps(caps, ensure_ascii=False, sort_keys=True),
                )
            )

    def _policy_out(self, mode: str) -> dict[str, int]:
        base = DEFAULT_POLICIES[mode]
        return {
            "ttl_playlist": min(self.config.get_int("media.ttl_playlist"), base.ttl_playlist),
            "ttl_key": min(self.config.get_int("media.ttl_key"), base.ttl_key),
            "preview_seconds": base.preview_seconds,
            "progress_interval_sec": base.progress_interval_sec,
        }

    @staticmethod
    def _json(raw: str | None) -> dict[str, Any]:
        try:
            value = json.loads(raw or "{}")
        except json.JSONDecodeError:
            return {}
        return value if isinstance(value, dict) else {}

    @staticmethod
    def _batch_id(now: datetime) -> str:
        return f"FLP{now:%Y%m%d%H%M%S}{secrets.token_hex(4)}"

    @staticmethod
    def _state_out(row: SysModeState, *, epoch: int | None = None) -> dict[str, Any]:
        return {
            "mode": row.active_mode,
            "epoch": int(row.epoch if epoch is None else epoch),
            "previous_mode": row.previous_mode,
            "switched_at": _iso(row.switched_at),
            "switched_by": row.switched_by,
            "migration_batch_id": row.migration_batch_id,
            "version": int(row.version),
        }

    def _invalidate(self) -> None:
        """同进程切换后立即生效；其余进程按 `mode.cache_ttl_sec` ≤30s 收敛。"""
        try:
            ctx = get_mode_context(self.config, db=self.db)
        except RuntimeError:  # pragma: no cover - 无 Flask 上下文时退化为新建实例
            return
        ctx.invalidate()


__all__ = ["ModeService", "ModeCounts"]
