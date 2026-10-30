"""M1.5 安装向导服务层（§8：5 步向导 + 诊断分类）。

状态机（落 `sys_install_state.install_step`）：
    start ──POST /install/db/setup──▶ db ──admin──▶ admin ──mode──▶ mode ──finish──▶ done

约束：
- `db/test` 只做探测（`persist=False`），不写 `sys_compat_flags`；
- `finish` 才固化能力位快照并解锁业务 Blueprint（§4.3 装配门禁不变）；
- 建库一律 Alembic；目标库已有系统表但无 `alembic_version` 时拒绝覆盖（禁止 create_all）；
- 一次性安装令牌：启动期生成或 `ZHY_INSTALL_TOKEN`，常量时间比对 + 失败限速 + 审计日志。
"""

from __future__ import annotations

import hmac
import json
import logging
import re
import secrets
import time
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass
from enum import StrEnum

from sqlalchemy import Engine, inspect, select, text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from zhyonlineedu.core.auth.keys import ensure_keypair
from zhyonlineedu.core.auth.passwords import hash_password
from zhyonlineedu.core.capability import ProbeResult, ProbeState
from zhyonlineedu.core.codes import (
    INSTALL_ALREADY_DONE,
    INSTALL_BLOCKED,
    INSTALL_STEP_INVALID,
    INTERNAL_ERROR,
    VALIDATION_FAILED,
    Code,
)
from zhyonlineedu.core.config import Config
from zhyonlineedu.core.db import compat
from zhyonlineedu.core.db.compat import Cap, CompatError, Flavor
from zhyonlineedu.core.db.engine import create_db_engine
from zhyonlineedu.core.db.migration import current_revision, upgrade_head
from zhyonlineedu.core.db.models.account import AccountUser, SysUserRole
from zhyonlineedu.core.db.models.system import SysConfig, SysInstallState, SysModeConfig, utcnow
from zhyonlineedu.core.db.probe import persist_flags, run_probe
from zhyonlineedu.core.errors import AppError

logger = logging.getLogger(__name__)

STATE_TABLE = SysInstallState.__tablename__
STATE_ID = 1
ALEMBIC_TABLE = "alembic_version"
ADMIN_USERNAME_KEY = "install.admin.username"
ADMIN_HASH_KEY = "install.admin.password_hash"
ACTIVE_MODE_KEY = "install.mode"
MODES: tuple[str, ...] = ("internal", "external")
_USERNAME = re.compile(r"^[A-Za-z0-9_.-]{3,64}$")


class InstallStep(StrEnum):
    START = "start"
    DB = "db"
    ADMIN = "admin"
    MODE = "mode"
    FINISH = "finish"
    DONE = "done"


STEPS: tuple[str, ...] = (
    InstallStep.START.value,
    InstallStep.DB.value,
    InstallStep.ADMIN.value,
    InstallStep.MODE.value,
    InstallStep.FINISH.value,
)


@dataclass(frozen=True, slots=True)
class InstallStatus:
    step: InstallStep
    is_installed: bool
    schema_rev: str | None = None
    flavor: str | None = None
    blockers: tuple[str, ...] = ()

    def as_dict(self) -> dict[str, object]:
        return {
            "step": self.step.value,
            "is_installed": self.is_installed,
            "schema_rev": self.schema_rev,
            "flavor": self.flavor,
            "blockers": list(self.blockers),
            "steps": list(STEPS),
        }


def _parse_step(raw: object) -> InstallStep:
    try:
        return InstallStep(str(raw or InstallStep.START.value))
    except ValueError:
        return InstallStep.START


def install_token_from_config(config: Config) -> str:
    """安装令牌：显式配置优先，否则启动期生成并打印（安装完成后作废）。"""
    configured = config.get_str("install.token").strip()
    if configured:
        return configured
    token = secrets.token_urlsafe(24)
    logger.warning(
        "安装令牌（仅本次安装有效）：%s —— POST /api/v1/install/token 提交后获得安装会话，"
        "完成安装自动作废",
        token,
    )
    return token


class InstallAuth:
    """安装期鉴权：一次性令牌 + 短时安装会话 + 失败限速。"""

    def __init__(
        self,
        token: str,
        *,
        max_failures: int = 5,
        window_sec: int = 60,
        session_ttl_sec: int = 1800,
    ) -> None:
        self._token = token.encode()
        self._max_failures = max_failures
        self._window_sec = window_sec
        self._session_ttl_sec = session_ttl_sec
        self._failures: deque[float] = deque()
        self._sessions: dict[str, float] = {}
        # 空令牌表示安装已关闭（应用启动即 READY），所有安装动作统一拒绝。
        self._closed = not token

    @property
    def closed(self) -> bool:
        return self._closed

    def verify(self, candidate: str) -> bool:
        now = time.monotonic()
        self._purge_failures(now)
        if self._closed:
            return False
        if len(self._failures) >= self._max_failures:
            raise AppError(Code.RATE_LIMITED, data={"retry_after": self._window_sec})
        ok = hmac.compare_digest(self._token, candidate.encode())
        if not ok:
            self._failures.append(now)
        return ok

    def open_session(self) -> tuple[str, int]:
        now = time.monotonic()
        self._purge_sessions(now)
        value = secrets.token_urlsafe(32)
        self._sessions[value] = now + self._session_ttl_sec
        return value, self._session_ttl_sec

    def check_session(self, value: str) -> bool:
        if self._closed or not value:
            return False
        expires = self._sessions.get(value)
        if expires is None:
            return False
        if expires <= time.monotonic():
            self._sessions.pop(value, None)
            return False
        return True

    def revoke(self) -> None:
        """安装完成后作废令牌与全部安装会话（一次性）。"""
        self._closed = True
        self._sessions.clear()

    def _purge_failures(self, now: float) -> None:
        while self._failures and self._failures[0] <= now - self._window_sec:
            self._failures.popleft()

    def _purge_sessions(self, now: float) -> None:
        for key, expires in list(self._sessions.items()):
            if expires <= now:
                del self._sessions[key]


class InstallService:
    """安装向导动作集合；所有顺序/前置校验集中在此，Blueprint 只做解析与信封。"""

    def __init__(
        self,
        config: Config,
        *,
        probe_runner: Callable[[], ProbeResult] | None = None,
        on_ready: Callable[[ProbeResult], None] | None = None,
        auth: InstallAuth | None = None,
        token: str | None = None,
    ) -> None:
        self.config = config
        if auth is not None:
            self.auth = auth
        else:
            self.auth = InstallAuth(install_token_from_config(config) if token is None else token)
        self._probe_runner = probe_runner
        self._on_ready = on_ready
        self._probe: ProbeResult | None = None

    # ── 读取状态 ──────────────────────────────────────────────────────────

    def status(self) -> InstallStatus:
        try:
            url, flavor = self._target()
            step, installed, rev = self._read_state(url, flavor)
        except (SQLAlchemyError, CompatError, AppError) as exc:
            return InstallStatus(
                InstallStep.START,
                False,
                blockers=(f"数据库不可达：{type(exc).__name__}: {exc}",),
            )
        blockers = self._probe.blockers if self._probe is not None else ()
        return InstallStatus(step, installed, rev, flavor.value, blockers)

    def status_payload(self) -> dict[str, object]:
        return self.status().as_dict()

    # ── 步骤 1：数据库能力探测 ────────────────────────────────────────────

    def db_test(self) -> dict[str, object]:
        status = self.status()
        if status.is_installed or status.step is InstallStep.DONE:
            raise AppError(INSTALL_ALREADY_DONE)
        probe = self._run_probe()
        self._probe = probe
        dialect = compat.flavor_spec(probe.flavor).dialect.value if probe.flavor else None
        return {
            "ok": not probe.blockers,
            "flavor": probe.flavor,
            "dialect": dialect,
            "caps": dict(probe.capabilities),
            "warnings": self._warnings(probe),
            "blockers": list(probe.blockers),
        }

    # ── 步骤 2：建库（Alembic）────────────────────────────────────────────

    def db_setup(self) -> dict[str, object]:
        status = self.status()
        if status.is_installed or status.step is InstallStep.DONE:
            raise AppError(INSTALL_ALREADY_DONE)
        if status.step in {InstallStep.ADMIN, InstallStep.MODE, InstallStep.FINISH}:
            return {**status.as_dict(), "already": True}
        if status.step not in {InstallStep.START, InstallStep.DB}:
            raise AppError(
                INSTALL_STEP_INVALID,
                data={"step": status.step.value, "expected": ["start", "db"]},
            )

        probe = self._probe if self._probe is not None else self._run_probe()
        self._probe = probe
        if probe.blockers:
            raise AppError(INSTALL_BLOCKED, data={"blockers": list(probe.blockers)})

        url, flavor = self._target()
        self._guard_existing(url, flavor)
        try:
            revision = upgrade_head(url, flavor)
        except Exception as exc:  # noqa: BLE001 - 迁移失败必须转可读诊断与回滚提示
            raise AppError(
                INSTALL_BLOCKED,
                data={
                    "blockers": [f"数据库迁移失败：{type(exc).__name__}: {exc}"],
                    "rollback": "python manage.py db-migrate --rollback-to-last-good",
                },
            ) from exc
        if not revision:
            raise AppError(INTERNAL_ERROR, data={"reason": "迁移后未读到 revision"})

        engine = create_db_engine(url, flavor)
        try:
            self._write_state(
                engine, schema_rev=revision, last_good_rev=revision, step=InstallStep.ADMIN
            )
        finally:
            engine.dispose()
        return {**self.status().as_dict(), "already": False, "schema_rev": revision}

    # ── 步骤 3：首个超管（校验后暂存，M1.6 迁移到账号表）──────────────────

    def set_admin(self, username: object, password: object) -> dict[str, object]:
        name = self._validate_admin(username, password)
        status = self.status()
        if status.is_installed:
            raise AppError(INSTALL_ALREADY_DONE)
        if status.step in {InstallStep.MODE, InstallStep.FINISH}:
            existing = self._read_config(ADMIN_USERNAME_KEY)
            if existing is not None and existing != name:
                raise AppError(
                    INSTALL_STEP_INVALID,
                    data={"reason": "超管已创建，用户名不可更换", "username": existing},
                )
            return {"username": existing or name, "step": status.step.value, "already": True}
        if status.step is not InstallStep.ADMIN:
            raise AppError(
                INSTALL_STEP_INVALID,
                data={"step": status.step.value, "expected": [InstallStep.ADMIN.value]},
            )

        url, flavor = self._target()
        engine = create_db_engine(url, flavor)
        try:
            with Session(engine) as session:
                self._put_config(session, ADMIN_USERNAME_KEY, name)
                self._put_config(session, ADMIN_HASH_KEY, hash_password(str(password)))
                session.commit()
            self._write_state(
                engine,
                schema_rev=status.schema_rev,
                last_good_rev=status.schema_rev,
                step=InstallStep.MODE,
            )
        finally:
            engine.dispose()
        return {"username": name, "step": InstallStep.MODE.value, "already": False}

    # ── 步骤 4：场景模式（写 sys_mode_config，锁定能力位快照）──────────────

    def set_mode(self, mode: object) -> dict[str, object]:
        selected = str(mode or "").strip()
        if selected not in MODES:
            raise AppError(
                VALIDATION_FAILED,
                data={"errors": [f"mode 必须是 {'/'.join(MODES)} 之一"]},
            )
        status = self.status()
        if status.is_installed:
            raise AppError(INSTALL_ALREADY_DONE)
        if status.step is InstallStep.FINISH:
            return {
                "mode": self._read_config(ACTIVE_MODE_KEY) or selected,
                "step": status.step.value,
                "already": True,
            }
        if status.step is not InstallStep.MODE:
            raise AppError(
                INSTALL_STEP_INVALID,
                data={"step": status.step.value, "expected": [InstallStep.MODE.value]},
            )

        caps = dict((self._probe or self._run_probe()).capabilities)
        url, flavor = self._target()
        engine = create_db_engine(url, flavor)
        try:
            with Session(engine) as session:
                row = session.get(SysModeConfig, (selected, "active"))
                if row is None:
                    row = SysModeConfig(
                        mode=selected,
                        config_key="active",
                        value="1",
                        compat_flags=json.dumps(caps, ensure_ascii=False, sort_keys=True),
                    )
                else:
                    row.value = "1"
                    row.compat_flags = json.dumps(caps, ensure_ascii=False, sort_keys=True)
                session.add(row)
                self._put_config(session, ACTIVE_MODE_KEY, selected)
                session.commit()
            self._write_state(
                engine,
                schema_rev=status.schema_rev,
                last_good_rev=status.schema_rev,
                step=InstallStep.FINISH,
            )
        finally:
            engine.dispose()
        return {"mode": selected, "step": InstallStep.FINISH.value, "already": False}

    # ── 步骤 5：完成（固化 caps → 解锁业务路由）───────────────────────────

    def finish(self) -> dict[str, object]:
        status = self.status()
        if status.is_installed or status.step is InstallStep.DONE:
            raise AppError(INSTALL_ALREADY_DONE)
        if status.step is not InstallStep.FINISH:
            raise AppError(
                INSTALL_STEP_INVALID,
                data={"step": status.step.value, "expected": [InstallStep.FINISH.value]},
            )

        probe = self._run_probe()
        self._probe = probe
        if probe.blockers:
            raise AppError(INSTALL_BLOCKED, data={"blockers": list(probe.blockers)})

        # 安装期生成 RSA 密钥对（幂等）：失败即阻断，避免"装完了却无法登录"
        try:
            ensure_keypair(
                self.config.get_str("jwt.private_key_path"),
                self.config.get_str("jwt.public_key_path"),
            )
        except Exception as exc:  # noqa: BLE001 - 密钥落盘失败需可读诊断
            raise AppError(
                INSTALL_BLOCKED,
                data={"blockers": [f"JWT 密钥生成失败：{type(exc).__name__}: {exc}"]},
            ) from exc

        url, flavor = self._target()
        caps = dict(probe.capabilities)
        engine = create_db_engine(url, flavor)
        try:
            persist_flags(engine, flavor, caps)
            promoted = self._promote_pending_admin(engine)
            revision = current_revision(engine) or status.schema_rev
            self._write_state(
                engine,
                schema_rev=revision,
                last_good_rev=revision,
                step=InstallStep.DONE,
                installed=True,
            )
        finally:
            engine.dispose()

        compat.configure(url=url, flavor=flavor, caps=caps, source="install")
        self.auth.revoke()
        ready = ProbeResult(ProbeState.READY, flavor=flavor.value, capabilities=caps)
        if self._on_ready is not None:
            self._on_ready(ready)
        return {
            "installed": True,
            "step": InstallStep.DONE.value,
            "restart_required": True,  # 业务路由需重启进程后按 READY 装配
            "flavor": flavor.value,
            "caps": caps,
            "warnings": self._warnings(probe),
            "admin": promoted,
        }

    # ── 内部：探测 / 迁移守卫 / 状态读写 ─────────────────────────────────

    def _target(self) -> tuple[str, Flavor]:
        try:
            url = self.config.get_str("db.url")
            explicit = self.config.get_str("db.flavor") or None
            flavor = Flavor(explicit) if explicit else compat.detect_flavor(url)
        except CompatError as exc:
            raise AppError(INSTALL_BLOCKED, data={"blockers": [str(exc)]}) from exc
        return url, flavor

    def _run_probe(self) -> ProbeResult:
        if self._probe_runner is not None:
            return self._probe_runner()
        url, flavor = self._target()
        engine = create_db_engine(url, flavor)
        try:
            return run_probe(engine, flavor, persist=False)
        finally:
            engine.dispose()

    def _guard_existing(self, url: str, flavor: Flavor) -> None:
        """既有系统表但无迁移记录 → 拒绝覆盖（禁止 create_all 抹掉旧库）。"""
        engine = create_db_engine(url, flavor)
        try:
            inspector = inspect(engine)
            if inspector.has_table(STATE_TABLE) and not inspector.has_table(ALEMBIC_TABLE):
                raise AppError(
                    INSTALL_BLOCKED,
                    data={
                        "blockers": [
                            "目标库已存在 sys_install_state 但缺少 alembic_version："
                            "禁止 create_all 覆盖，请先备份并确认库状态"
                        ]
                    },
                )
        finally:
            engine.dispose()

    def _read_state(self, url: str, flavor: Flavor) -> tuple[InstallStep, bool, str | None]:
        engine = create_db_engine(url, flavor)
        try:
            inspector = inspect(engine)
            if not inspector.has_table(STATE_TABLE):
                return InstallStep.START, False, None
            columns = {str(column["name"]) for column in inspector.get_columns(STATE_TABLE)}
            fields = "is_installed, schema_rev"
            if "install_step" in columns:
                fields += ", install_step"
            with engine.connect() as conn:
                # fields/STATE_TABLE 均为代码内常量（is_installed/schema_rev/install_step），
                # 仅主键走绑定参数，不存在外部拼接。
                query = text(f"SELECT {fields} FROM {STATE_TABLE} WHERE id = :id")  # noqa: S608
                row = conn.execute(query, {"id": STATE_ID}).first()
            if row is None:
                return InstallStep.START, False, None
            installed = bool(row[0])
            revision = str(row[1]) if row[1] is not None else None
            step = InstallStep.DONE if installed else _parse_step(row[2] if len(row) > 2 else None)
            return step, installed, revision
        finally:
            engine.dispose()

    def _write_state(
        self,
        engine: Engine,
        *,
        schema_rev: str | None,
        last_good_rev: str | None,
        step: InstallStep,
        installed: bool = False,
    ) -> None:
        with Session(engine) as session:
            row = session.get(SysInstallState, STATE_ID)
            if row is None:
                row = SysInstallState(
                    id=STATE_ID,
                    is_installed=installed,
                    install_step=step.value,
                    version=1,
                    updated_at=utcnow(),
                )
                session.add(row)
            row.install_step = step.value
            row.is_installed = installed or row.is_installed
            if schema_rev:
                row.schema_rev = schema_rev
            if last_good_rev:
                row.last_good_rev = last_good_rev
            if installed and row.installed_at is None:
                row.installed_at = utcnow()
            row.updated_at = utcnow()
            row.version = int(row.version or 1) + 1
            session.commit()

    def _read_config(self, key: str) -> str | None:
        url, flavor = self._target()
        engine = create_db_engine(url, flavor)
        try:
            if not inspect(engine).has_table(SysConfig.__tablename__):
                return None
            with Session(engine) as session:
                row = session.get(SysConfig, key)
                return row.value if row is not None else None
        finally:
            engine.dispose()

    def _promote_pending_admin(self, engine: Engine) -> str | None:
        """把暂存在 sys_config 的首个超管落到 account_user + super_admin（M1.6）。

        成功后删除暂存键（口令哈希只留账号表一份）；重复执行是幂等的。
        """
        with Session(engine) as session:
            name_row = session.get(SysConfig, ADMIN_USERNAME_KEY)
            hash_row = session.get(SysConfig, ADMIN_HASH_KEY)
            if name_row is None or hash_row is None:
                return None
            username = name_row.value
            user = session.execute(
                select(AccountUser).where(AccountUser.username == username)
            ).scalar_one_or_none()
            if user is None:
                now = utcnow()
                user = AccountUser(
                    username=username,
                    password_hash=hash_row.value,
                    status="active",
                    token_version=1,
                    created_at=now,
                    updated_at=now,
                )
                session.add(user)
                session.flush()
            if session.get(SysUserRole, (user.id, "super_admin")) is None:
                session.add(SysUserRole(user_id=user.id, role_code="super_admin"))
            session.delete(name_row)
            session.delete(hash_row)
            session.commit()
            return username

    @staticmethod
    def _put_config(session: Session, key: str, value: str) -> None:
        row = session.get(SysConfig, key)
        if row is None:
            session.add(SysConfig(config_key=key, value=value, updated_at=utcnow()))
        else:
            row.value = value
            row.updated_at = utcnow()

    @staticmethod
    def _validate_admin(username: object, password: object) -> str:
        name = username.strip() if isinstance(username, str) else ""
        if not _USERNAME.fullmatch(name):
            raise AppError(
                VALIDATION_FAILED,
                data={"errors": ["username 需为 3~64 位字母/数字/下划线/点/短横线"]},
            )
        if not isinstance(password, str) or not 8 <= len(password) <= 128:
            raise AppError(
                VALIDATION_FAILED,
                data={"errors": ["password 长度需为 8~128 位"]},
            )
        return name

    @staticmethod
    def _warnings(probe: ProbeResult) -> list[str]:
        if not probe.flavor:
            return []
        flavor = Flavor(probe.flavor)
        caps = probe.capabilities
        warnings: list[str] = []
        if not caps.get(Cap.RETURNING.value, True):
            warnings.append(f"{flavor.value}: UPSERT 无 RETURNING，已启用应用层回读路径")
        if flavor in {Flavor.OPENGAUSS_5, Flavor.OPENGAUSS_6}:
            warnings.append("openGauss 5/6 仅支持冲突子句缺失的去重写入路径")
        if flavor is Flavor.MYSQL:
            warnings.append("MySQL 无 RETURNING：去重写入走 ON DUPLICATE 分支，批量回读走查询")
        return warnings
