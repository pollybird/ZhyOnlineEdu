"""模式上下文与 epoch（§9.3 · M2.10）。

口径：
- `sys_mode_state` 单行保存 `active_mode` / `epoch`；切换时 epoch +1；
- **一次请求内只读一次**：Flask 请求内快照放 `g.zhy_mode_state`，切换发生在请求 A
  期间时 A 仍用旧 epoch 完成（MODE-FLIP-001），下一个请求读到新 epoch；
- 进程内对非请求上下文提供 `mode.cache_ttl_sec`（默认 30s）短缓存：切换后全站
  生效 ≤30s（§1.2「切换不改表，全站生效 ≤30s」）；切换方主动 invalidate 立即生效。
"""

from __future__ import annotations

import json
import threading
import time
from dataclasses import dataclass, field
from typing import Any

from flask import g, has_app_context
from sqlalchemy.orm import Session

from zhyonlineedu.core.config import Config
from zhyonlineedu.core.db.engine import Database, ensure_database
from zhyonlineedu.core.db.models.system import SysConfig, SysModeConfig, SysModeState, utcnow

MODES: tuple[str, ...] = ("internal", "external")
ACTIVE_MODE_KEY = "install.mode"


@dataclass(frozen=True, slots=True)
class ModeState:
    """一次判定使用的模式快照（epoch 不可变）。"""

    mode: str
    epoch: int
    capabilities: dict[str, Any] = field(default_factory=dict)
    policy: dict[str, Any] = field(default_factory=dict)

    @property
    def other(self) -> str:
        return "external" if self.mode == "internal" else "internal"


def _parse_json(raw: str | None) -> dict[str, Any]:
    try:
        value = json.loads(raw or "{}")
    except json.JSONDecodeError:
        return {}
    return value if isinstance(value, dict) else {}


def ensure_state(session: Session) -> SysModeState:
    """读取单行状态；缺失时按 `sys_config.install.mode` 建行（兼容历史安装）。"""
    row = session.get(SysModeState, 1)
    if row is not None:
        return row
    config = session.get(SysConfig, ACTIVE_MODE_KEY)
    mode = config.value.strip() if config is not None else "internal"
    if mode not in MODES:
        mode = "internal"
    row = SysModeState(
        id=1,
        active_mode=mode,
        epoch=1,
        previous_mode=None,
        switched_at=None,
        switched_by=None,
        migration_batch_id=None,
        version=1,
        updated_at=utcnow(),
    )
    session.add(row)
    session.commit()
    return row


class ModeContext:
    """模式快照提供者：请求内一次读取 + 进程内 TTL 缓存。"""

    def __init__(self, config: Config, *, db: Database | None = None) -> None:
        self.config = config
        self._db = db
        self._lock = threading.Lock()
        self._state: ModeState | None = None
        self._loaded_at = 0.0

    @property
    def db(self) -> Database:
        if self._db is None:
            self._db = ensure_database(self.config)
        return self._db

    def snapshot(self, *, force: bool = False) -> ModeState:
        if has_app_context():
            cached = g.get("zhy_mode_state")
            if isinstance(cached, ModeState) and not force:
                return cached
            state = self._load()
            g.zhy_mode_state = state
            return state

        ttl = max(0, self.config.get_int("mode.cache_ttl_sec"))
        now = time.monotonic()
        with self._lock:
            if not force and self._state is not None and now - self._loaded_at < ttl:
                return self._state
        state = self._load()
        with self._lock:
            self._state = state
            self._loaded_at = time.monotonic()
        return state

    def invalidate(self) -> None:
        with self._lock:
            self._state = None
            self._loaded_at = 0.0
        if has_app_context():
            g.pop("zhy_mode_state", None)

    def _load(self) -> ModeState:
        with Session(self.db.engine) as session:
            row = ensure_state(session)
            mode = row.active_mode if row.active_mode in MODES else "internal"
            epoch = int(row.epoch)
            caps_row = session.get(SysModeConfig, (mode, "active"))
            policy_row = session.get(SysModeConfig, (mode, "policy"))
            capabilities = _parse_json(caps_row.compat_flags if caps_row is not None else None)
            policy = _parse_json(policy_row.value if policy_row is not None else None)
            return ModeState(mode=mode, epoch=epoch, capabilities=capabilities, policy=policy)


def get_mode_context(config: Config, *, db: Database | None = None) -> ModeContext:
    """取进程内共享的上下文（Flask 装配时注册到 `app.extensions["zhy_mode"]`）。"""
    if has_app_context():
        from flask import current_app

        ctx = current_app.extensions.get("zhy_mode")
        if isinstance(ctx, ModeContext):
            return ctx
    return ModeContext(config, db=db)
