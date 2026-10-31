"""进度网关（§12.5 · F2 · M2.5）。

HTTP 侧只做三件事：
1. **准入**：课时存在、课程未归档；未发布课程仅 `course:edit` 可预览；
2. **分配**：`learn_progress_gate.last_gw_seq` 自增得全局单调 `gw_seq`（唯一索引兜底重试），
   同 (user, lesson) 的 `seq_no` 连续递增；
3. **落盘**：追加 `learn_progress_log`（事实源）后尝试 `bus.publish`；总线满/不可用只告警，
   **不影响 202**——flush 会从未 flush 的 log 行重放。

红线：本模块**禁止**写 `learn_progress`（投影只能由 flush 单写者写），
`tests/test_progress.py` 会断言 HTTP 上报后投影表仍为空。
"""

from __future__ import annotations

import hashlib
import logging
import time
from collections.abc import Mapping
from datetime import datetime
from typing import Any, NoReturn

from sqlalchemy import func, select, update
from sqlalchemy.exc import IntegrityError, OperationalError
from sqlalchemy.orm import Session

from zhyonlineedu.core.codes import (
    MODE_COURSE_MISMATCH,
    NOT_FOUND,
    VALIDATION_FAILED,
    Code,
)
from zhyonlineedu.core.config import Config
from zhyonlineedu.core.db.engine import Database, ensure_database, is_lock_error
from zhyonlineedu.core.db.models.catalog import EduCourse, EduCourseLesson
from zhyonlineedu.core.db.models.learn import (
    DEFAULT_SHARD,
    LearnProgressGate,
    LearnProgressLog,
    position_bucket,
    utcnow,
)
from zhyonlineedu.core.errors import AppError
from zhyonlineedu.core.mode import get_mode_context
from zhyonlineedu.core.progress.bus import ProgressBus, ProgressCmd, RingFull, get_bus

logger = logging.getLogger(__name__)

GW_SEQ_RETRY = 3
MAX_PLAYED_SEC = 3600
SPEED_MIN_BPS = 50
SPEED_MAX_BPS = 300


def _fail(message: str) -> NoReturn:
    raise AppError(VALIDATION_FAILED, data={"errors": [message]})


def _int_field(body: Mapping[str, Any], name: str, *, minimum: int, maximum: int) -> int:
    value = body.get(name)
    if isinstance(value, bool) or not isinstance(value, int):
        _fail(f"{name} 必须是整数")
    if value < minimum or value > maximum:
        _fail(f"{name} 必须在 {minimum}..{maximum}")
    return value


class ProgressGateway:
    """无状态：每次上报自建短事务；flush 单写者另起长任务（`ProgressFlush`）。"""

    def __init__(
        self,
        config: Config,
        *,
        db: Database | None = None,
        bus: ProgressBus | None = None,
    ) -> None:
        self.config = config
        self._db = db
        self._bus = bus or get_bus(
            config.get_str("progress.bus"),
            ring_dir=config.get_str("progress.ring_dir"),
            url=config.get_str("redis.url"),
        )

    @property
    def db(self) -> Database:
        if self._db is None:
            self._db = ensure_database(self.config)
        return self._db

    # ── 上报 ──────────────────────────────────────────────────────────────

    def record(
        self,
        *,
        user_id: int,
        lesson_id: int,
        body: Mapping[str, Any],
        can_preview: bool,
        now: datetime | None = None,
    ) -> dict[str, Any]:
        moment = now or utcnow()
        lesson, course = self._load_lesson(lesson_id)
        preview = course.status != "published"
        if preview and not can_preview:
            raise AppError(
                Code.MEDIA_TICKET_MISMATCH,
                data={"lesson_id": lesson_id, "reason": "course_not_published"},
            )

        # 模式校验（§9.4 规则 8/9）：学员上报进度同样受当前模式约束，管理端预览可跨模式
        state = get_mode_context(self.config, db=self.db).snapshot()
        if not can_preview and course.mode not in ("both", state.mode):
            raise AppError(
                MODE_COURSE_MISMATCH,
                data={
                    "lesson_id": lesson_id,
                    "course_id": int(course.id),
                    "course_mode": course.mode,
                    "mode": state.mode,
                    "epoch": state.epoch,
                },
            )

        duration = max(int(lesson.duration_sec or 0), 0)
        position_max = 86_400 if duration == 0 else duration + 60
        position = _int_field(body, "position_sec", minimum=0, maximum=position_max)
        played = _int_field(body, "played_seconds", minimum=0, maximum=MAX_PLAYED_SEC)
        speed_bps = self._speed_bps(body.get("speed"))
        client_ts_ms = body.get("client_ts_ms")
        if client_ts_ms is None:
            client_ts_ms = 0
        elif isinstance(client_ts_ms, bool) or not isinstance(client_ts_ms, int):
            _fail("client_ts_ms 必须是整数毫秒")
        source = "preview" if preview else "play"

        payload_hash = self._payload_hash(lesson_id, position, played, speed_bps, source)
        kw = self._insert_log(
            user_id=user_id,
            lesson_id=lesson_id,
            course_id=int(course.id),
            position=position,
            played=played,
            speed_bps=speed_bps,
            source=source,
            payload_hash=payload_hash,
            client_ts_ms=int(client_ts_ms),
            now=moment,
        )
        try:
            self._bus.publish(
                ProgressCmd(
                    gw_seq=kw["gw_seq"],
                    user_id=user_id,
                    lesson_id=lesson_id,
                    course_id=int(course.id),
                    seq_no=kw["seq_no"],
                    position_sec=position,
                    played_seconds=kw["deduped_played"],
                    speed_bps=speed_bps,
                    source=source,
                    client_ts_ms=int(client_ts_ms),
                )
            )
        except RingFull:
            logger.warning("progress ring 已满，本轮仅依赖 DB log 重放（gw_seq=%s）", kw["gw_seq"])
        except (OSError, RuntimeError):
            logger.exception(
                "progress bus publish 失败，依赖 DB log 重放（gw_seq=%s）", kw["gw_seq"]
            )

        return {
            "gw_seq": kw["gw_seq"],
            "accepted_at": moment.isoformat(timespec="seconds"),
            "deduped": kw["deduped"],
        }

    def _load_lesson(self, lesson_id: int) -> tuple[EduCourseLesson, EduCourse]:
        with Session(self.db.engine) as session:
            lesson = session.get(EduCourseLesson, lesson_id)
            if lesson is None or lesson.deleted_at is not None:
                raise AppError(NOT_FOUND, data={"lesson_id": lesson_id})
            course = session.get(EduCourse, lesson.course_id)
            if course is None or course.deleted_at is not None or course.status == "archived":
                raise AppError(NOT_FOUND, data={"course_id": lesson.course_id})
            session.expunge_all()
            return lesson, course

    def _speed_bps(self, value: object) -> int:
        if value is None:
            return 100
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            _fail("speed 必须是数字（倍速）")
        bps = int(round(float(value) * 100))
        if bps < SPEED_MIN_BPS or bps > SPEED_MAX_BPS:
            _fail(f"speed 必须在 {SPEED_MIN_BPS / 100:.1f}..{SPEED_MAX_BPS / 100:.1f}")
        return bps

    @staticmethod
    def _payload_hash(
        lesson_id: int, position: int, played: int, speed_bps: int, source: str
    ) -> str:
        raw = f"{lesson_id}|{position}|{played}|{speed_bps}|{source}"
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()

    def _insert_log(
        self,
        *,
        user_id: int,
        lesson_id: int,
        course_id: int,
        position: int,
        played: int,
        speed_bps: int,
        source: str,
        payload_hash: str,
        client_ts_ms: int,
        now: datetime,
    ) -> dict[str, Any]:
        bucket = position_bucket(position, 30)
        for attempt in range(1, GW_SEQ_RETRY + 1):
            with Session(self.db.engine) as session:
                try:
                    gs = self._allocate_gw_seq(session, now)
                    seq = self._next_seq(session, user_id, lesson_id)
                    deduped = bool(
                        session.execute(
                            select(LearnProgressLog.id)
                            .where(
                                LearnProgressLog.user_id == user_id,
                                LearnProgressLog.lesson_id == lesson_id,
                                LearnProgressLog.position_bucket == bucket,
                                LearnProgressLog.played_seconds > 0,
                                LearnProgressLog.source == "play",
                            )
                            .limit(1)
                        ).first()
                    )
                    played_eff = 0 if deduped else played
                    session.add(
                        LearnProgressLog(
                            user_id=user_id,
                            lesson_id=lesson_id,
                            course_id=course_id,
                            seq_no=seq,
                            gw_seq=gs,
                            position=position,
                            played_seconds=played_eff,
                            speed_bps=speed_bps,
                            speed_time=int(played_eff * 100_000 / max(speed_bps, 1)) // 1000,
                            position_bucket=bucket,
                            source=source,
                            write_mode="buffered",
                            payload_hash=payload_hash,
                            client_ts_ms=client_ts_ms,
                            received_at=now,
                            flushed_at=None,
                        )
                    )
                    session.commit()
                    return {
                        "gw_seq": gs,
                        "seq_no": seq,
                        "deduped": deduped,
                        "deduped_played": played_eff,
                    }
                except IntegrityError:
                    session.rollback()
                    logger.info("progress gw_seq 争用，重试 %s/%s", attempt, GW_SEQ_RETRY)
                except OperationalError as exc:
                    session.rollback()
                    if not is_lock_error(exc) or attempt == GW_SEQ_RETRY:
                        raise
                    # SQLite 写锁竞争（M2.16 压测实测）：退避后重试，避免把 500 透给播放器
                    logger.warning(
                        "progress 写入等锁超时，重试 %s/%s（%s）",
                        attempt,
                        GW_SEQ_RETRY,
                        exc.orig,
                    )
                    time.sleep(0.1 * attempt)
        logger.error("progress gw_seq 连续冲突，放弃上报（user=%s lesson=%s）", user_id, lesson_id)
        raise AppError(Code.RATE_LIMITED, data={"retry_after": 1})

    @staticmethod
    def _allocate_gw_seq(session: Session, now: datetime) -> int:
        session.execute(
            update(LearnProgressGate)
            .where(LearnProgressGate.shard == DEFAULT_SHARD)
            .values(
                last_gw_seq=LearnProgressGate.last_gw_seq + 1,
                updated_at=now,
            )
        )
        value = session.execute(
            select(LearnProgressGate.last_gw_seq).where(LearnProgressGate.shard == DEFAULT_SHARD)
        ).scalar_one()
        return int(value)

    @staticmethod
    def _next_seq(session: Session, user_id: int, lesson_id: int) -> int:
        last = session.execute(
            select(func.max(LearnProgressLog.seq_no)).where(
                LearnProgressLog.user_id == user_id,
                LearnProgressLog.lesson_id == lesson_id,
            )
        ).scalar()
        return int(last or 0) + 1


__all__ = [
    "DEFAULT_SHARD",
    "ProgressGateway",
    "position_bucket",
]
