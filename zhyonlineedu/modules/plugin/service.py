"""插件生命周期服务（§11 · M3.2）。

状态机（`plugin_registry.state`）：

    install → disabled ──enable──▶ enabled ──disable──▶ disabled
                 ▲                   │                    │
                 └────── enable ──────┘                    │
                                                            ▼
                                                     uninstall（须先 disable）

口径：
- 安装：manifest 校验 → 插件独立迁移链到 head → 写注册表（默认 disabled，不自动挂载路由）；
- 启用：加载入口模块 → 进程内路由表登记；禁用：摘除路由（不重启，残留路由 404）；
- 卸载：先降级插件迁移链（删表）再删注册表行；enabled 态直接卸载一律
  `PLUGIN_STATE_INVALID`；
- 所有变更写 `sys_log_security`（PLUGIN_INSTALL / ENABLE / DISABLE / UNINSTALL / LOAD_FAILED）；
- 启动时只挂载 `state=enabled` 且 manifest 未变更的插件；单个插件加载失败标记 `failed`
  并继续启动，保证"禁用/卸载任一插件内核完整可用"。
"""

from __future__ import annotations

import hashlib
import json
import logging
import threading
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from sqlalchemy import inspect, select
from sqlalchemy.orm import Session

from zhyonlineedu.core.codes import (
    NOT_FOUND,
    PLUGIN_MANIFEST_INVALID,
    PLUGIN_MIGRATION_INVALID,
    PLUGIN_NOT_FOUND,
    PLUGIN_STATE_INVALID,
)
from zhyonlineedu.core.config import Config
from zhyonlineedu.core.db import plugin_migration
from zhyonlineedu.core.db.engine import Database, ensure_database
from zhyonlineedu.core.db.models.account import SysLogSecurity
from zhyonlineedu.core.db.models.plugin import (
    STATE_DISABLED,
    STATE_ENABLED,
    STATE_FAILED,
    PluginRegistry,
)
from zhyonlineedu.core.db.models.system import utcnow
from zhyonlineedu.core.errors import AppError
from zhyonlineedu.core.events import EventBus, get_bus
from zhyonlineedu.core.plugin.loader import LoadedPlugin, load_server, unload
from zhyonlineedu.core.plugin.manifest import (
    MANIFEST_FILE,
    WEB_ROOT,
    PluginManifest,
    load_manifest,
    web_asset_path,
)
from zhyonlineedu.core.sdk.context import using

logger = logging.getLogger(__name__)

AUDIT_INSTALL = "PLUGIN_INSTALL"
AUDIT_ENABLE = "PLUGIN_ENABLE"
AUDIT_DISABLE = "PLUGIN_DISABLE"
AUDIT_UNINSTALL = "PLUGIN_UNINSTALL"
AUDIT_LOAD_FAILED = "PLUGIN_LOAD_FAILED"

# 插件前端资源：只放行代码/样式/字体类后缀（`.py` / 迁移 / 数据文件一律 404）
ASSET_MEDIA_TYPES: dict[str, str] = {
    ".js": "text/javascript",
    ".mjs": "text/javascript",
    ".css": "text/css",
    ".json": "application/json",
    ".svg": "image/svg+xml",
    ".png": "image/png",
    ".woff2": "font/woff2",
}


def manifest_hash(plugin_dir: Path) -> str:
    return hashlib.sha256((plugin_dir / MANIFEST_FILE).read_bytes()).hexdigest()


def _json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


def _loads(text: str, fallback: Any) -> Any:
    try:
        return json.loads(text)
    except (TypeError, ValueError):  # pragma: no cover - 注册表损坏兜底
        return fallback


@dataclass(frozen=True, slots=True)
class PluginSummary:
    code: str
    name: str
    version: str
    state: str
    core_api_min: str
    table_prefix: str
    schema_rev: str | None
    supported_modes: tuple[str, ...]
    capabilities: tuple[str, ...]
    capabilities_v2: Mapping[str, bool]
    routes: tuple[str, ...]
    menu: tuple[Mapping[str, Any], ...]
    entry_web: str | None
    installed_at: datetime
    updated_at: datetime
    error_message: str | None

    def as_dict(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "name": self.name,
            "version": self.version,
            "state": self.state,
            "core_api_min": self.core_api_min,
            "table_prefix": self.table_prefix,
            "schema_rev": self.schema_rev,
            "supported_modes": list(self.supported_modes),
            "capabilities": list(self.capabilities),
            "capabilities_v2": dict(self.capabilities_v2),
            "routes": list(self.routes),
            "menu": [dict(item) for item in self.menu],
            "entry_web": self.entry_web,
            "installed_at": self.installed_at.isoformat(timespec="seconds"),
            "updated_at": self.updated_at.isoformat(timespec="seconds"),
            "error_message": self.error_message,
        }


def _summary(row: PluginRegistry) -> PluginSummary:
    return PluginSummary(
        code=row.code,
        name=row.name,
        version=row.plugin_version,
        state=row.state,
        core_api_min=row.core_api_min,
        table_prefix=row.table_prefix,
        schema_rev=row.schema_rev,
        supported_modes=tuple(_loads(row.supported_modes, [])),
        capabilities=tuple(_loads(row.capabilities, [])),
        capabilities_v2=dict(_loads(row.capabilities_v2, {})),
        routes=tuple(_loads(row.routes, [])),
        menu=tuple(_loads(row.menu, [])),
        entry_web=row.entry_web,
        installed_at=row.installed_at,
        updated_at=row.updated_at,
        error_message=row.error_message,
    )


class PluginService:
    """插件生命周期 + 进程内路由表（无状态服务，自建短事务）。"""

    def __init__(
        self,
        config: Config,
        *,
        db: Database | None = None,
        events: EventBus | None = None,
    ) -> None:
        self.config = config
        self._db = db
        self._events = events
        self._lock = threading.RLock()
        self._loaded: dict[str, LoadedPlugin] = {}

    @property
    def events(self) -> EventBus:
        """事件总线：显式注入优先，否则取 app 上下文 / 进程默认总线。"""
        return self._events or get_bus()

    # ── 基础 ──────────────────────────────────────────────────────────

    @property
    def db(self) -> Database:
        if self._db is None:
            self._db = ensure_database(self.config)
        return self._db

    @property
    def root(self) -> Path:
        return Path(self.config.get_str("plugin.dir")).expanduser().resolve()

    def plugin_dir(self, code: str) -> Path:
        return self.root / code

    def loaded_codes(self) -> tuple[str, ...]:
        with self._lock:
            return tuple(sorted(self._loaded))

    # ── 读 ────────────────────────────────────────────────────────────

    def list(self) -> dict[str, Any]:
        with Session(self.db.engine) as session:
            rows = session.execute(select(PluginRegistry).order_by(PluginRegistry.code)).scalars()
            items = [_summary(row).as_dict() for row in rows]
        return {
            "items": items,
            "total": len(items),
            "plugin_dir": str(self.root),
            "available": self.available(),
        }

    def available(self) -> Sequence[dict[str, Any]]:
        """插件目录里「存在但未安装」的插件，供管理页一键安装。

        manifest 不合法的目录同样返回一项（`error` 说明原因），
        避免插件在管理页静默消失；已安装（含 failed）的插件不在此列表。
        """
        with Session(self.db.engine) as session:
            installed = set(session.execute(select(PluginRegistry.code)).scalars())
        items: list[dict[str, Any]] = []
        if not self.root.is_dir():
            return items
        for entry in sorted(self.root.iterdir()):
            if not entry.is_dir() or entry.name.startswith(".") or entry.name in installed:
                continue
            if not (entry / MANIFEST_FILE).is_file():
                continue
            try:
                manifest = load_manifest(entry)
            except AppError as exc:
                detail = ""
                if isinstance(exc.data, Mapping):
                    detail = "；".join(str(item) for item in exc.data.get("errors", []))
                items.append(
                    {
                        "code": entry.name,
                        "name": entry.name,
                        "version": None,
                        "error": detail or str(exc.code),
                    }
                )
                continue
            items.append(
                {
                    "code": manifest.code,
                    "name": manifest.name,
                    "version": manifest.version,
                    "error": None,
                }
            )
        return items

    def get(self, code: str) -> PluginSummary:
        with Session(self.db.engine) as session:
            return _summary(self._require_row(session, code))

    def enabled(self) -> dict[str, Any]:
        """已启用插件清单（登录即可读）：菜单 + 前端 ESM 入口。

        只返回 `state=enabled` 的行：禁用 / 加载失败 / 未安装的插件不下发，
        前端不会渲染其菜单，也不会尝试拉取不存在的资源。
        """
        with Session(self.db.engine) as session:
            rows = list(
                session.execute(
                    select(PluginRegistry)
                    .where(PluginRegistry.state == STATE_ENABLED)
                    .order_by(PluginRegistry.code)
                ).scalars()
            )
        items = [
            {
                "code": row.code,
                "name": row.name,
                "version": row.plugin_version,
                "menu": [dict(item) for item in _loads(row.menu, [])],
                "entry_web": row.entry_web,
                "entry_web_url": (
                    f"/api/v1/plugin/{row.code}/assets/{web_asset_path(row.entry_web)}"
                    if row.entry_web
                    else None
                ),
            }
            for row in rows
        ]
        return {"items": items, "total": len(items)}

    def asset(self, code: str, subpath: str) -> tuple[bytes, str]:
        """读取已启用插件的前端资源（`<plugin_dir>/web/<subpath>`）。

        安全口径：插件未加载（禁用/失败/未安装）一律 404；只允许 `web/` 目录内的
        代码/样式/字体类后缀；解析后必须仍在 `web/` 内（防目录穿越与符号链接逃逸）。
        """
        with self._lock:
            loaded = self._loaded.get(code)
        if loaded is None:
            raise AppError(PLUGIN_NOT_FOUND, data={"code": code})

        parts = Path(subpath.replace("\\", "/")).parts
        if not parts or any(part in ("", ".", "..") for part in parts):
            raise AppError(NOT_FOUND, data={"code": code, "path": subpath})
        media_type = ASSET_MEDIA_TYPES.get(Path(subpath).suffix.lower())
        if media_type is None:
            raise AppError(NOT_FOUND, data={"code": code, "path": subpath})

        web_root = (self.plugin_dir(code) / WEB_ROOT).resolve()
        target = (web_root / Path(*parts)).resolve()
        if not target.is_relative_to(web_root) or not target.is_file():
            raise AppError(NOT_FOUND, data={"code": code, "path": subpath})
        return target.read_bytes(), media_type

    # ── 生命周期 ──────────────────────────────────────────────────────

    def install(self, code: str, *, operator_id: int | None = None) -> PluginSummary:
        """安装（幂等）：校验 manifest → 迁移链到 head → 注册（默认 disabled）。"""
        with self._lock:
            plugin_dir = self.plugin_dir(code)
            if not plugin_dir.is_dir():
                raise AppError(PLUGIN_NOT_FOUND, data={"code": code, "plugin_dir": str(self.root)})
            manifest = load_manifest(plugin_dir)
            if manifest.code != code:
                raise AppError(
                    PLUGIN_MANIFEST_INVALID,
                    data={"errors": [f"目录名与 manifest.code 不一致：{code} != {manifest.code}"]},
                )

            engine = self.db.engine
            with Session(engine) as session:
                row = session.get(PluginRegistry, code)
                current = row.schema_rev if row is not None else None
                state = row.state if row is not None else STATE_DISABLED

            head = plugin_migration.upgrade(engine, plugin_dir, code, current=current)

            now = utcnow()
            with Session(engine) as session:
                row = session.get(PluginRegistry, code)
                if row is None:
                    row = PluginRegistry(
                        code=code,
                        name=manifest.name,
                        plugin_version=manifest.version,
                        state=STATE_DISABLED,
                        core_api_min=manifest.core_api_min,
                        table_prefix=manifest.table_prefix,
                        entry_server=manifest.entry_server,
                        entry_web=manifest.entry_web,
                        schema_rev=head,
                        manifest_hash=manifest_hash(plugin_dir),
                        supported_modes=_json(list(manifest.supported_modes)),
                        capabilities=_json(list(manifest.capabilities)),
                        capabilities_v2=_json(dict(manifest.capabilities_v2)),
                        routes=_json(list(manifest.routes)),
                        menu=_json([dict(item) for item in manifest.menu]),
                        installed_at=now,
                        updated_at=now,
                    )
                    session.add(row)
                else:
                    row.name = manifest.name
                    row.plugin_version = manifest.version
                    row.core_api_min = manifest.core_api_min
                    row.table_prefix = manifest.table_prefix
                    row.entry_server = manifest.entry_server
                    row.entry_web = manifest.entry_web
                    row.schema_rev = head
                    row.manifest_hash = manifest_hash(plugin_dir)
                    row.supported_modes = _json(list(manifest.supported_modes))
                    row.capabilities = _json(list(manifest.capabilities))
                    row.capabilities_v2 = _json(dict(manifest.capabilities_v2))
                    row.routes = _json(list(manifest.routes))
                    row.menu = _json([dict(item) for item in manifest.menu])
                    row.updated_at = now
                self._audit(session, AUDIT_INSTALL, manifest, operator_id)
                session.commit()
                summary = _summary(row)

            if state == STATE_ENABLED:
                # 已启用插件的重装：按新 manifest 重新挂载（失败则标记 failed，内核继续）
                self._remount_after_install(code, operator_id)
                return self.get(code)
            return summary

    def enable(self, code: str, *, operator_id: int | None = None) -> PluginSummary:
        with self._lock:
            engine = self.db.engine
            with Session(engine) as session:
                row = self._require_row(session, code)
                manifest = self._load_manifest_for(row)
                if row.state == STATE_ENABLED and code in self._loaded:
                    return _summary(row)
            try:
                self._mount(manifest)
            except Exception as exc:  # noqa: BLE001 - 单插件失败降级为 failed
                self._mark_failed(code, exc)
                raise
            with Session(engine) as session:
                row = self._require_row(session, code)
                row.state = STATE_ENABLED
                row.error_message = None
                row.updated_at = utcnow()
                self._audit(session, AUDIT_ENABLE, manifest, operator_id)
                session.commit()
                return _summary(row)

    def disable(self, code: str, *, operator_id: int | None = None) -> PluginSummary:
        with self._lock:
            engine = self.db.engine
            with Session(engine) as session:
                row = self._require_row(session, code)
                if row.state != STATE_ENABLED:
                    return _summary(row)
                loaded = self._loaded.pop(code, None)
                if loaded is not None:
                    unload(loaded)
                # 禁用即摘订阅：disabled 插件不得继续收到事件
                self.events.unsubscribe_plugin(code)
                row.state = STATE_DISABLED
                row.updated_at = utcnow()
                self._audit_event(session, AUDIT_DISABLE, row.code, row.plugin_version, operator_id)
                session.commit()
                return _summary(row)

    def uninstall(self, code: str, *, operator_id: int | None = None) -> None:
        with self._lock:
            engine = self.db.engine
            with Session(engine) as session:
                row = self._require_row(session, code)
                if row.state == STATE_ENABLED:
                    raise AppError(PLUGIN_STATE_INVALID, data={"code": code, "state": row.state})
                current = row.schema_rev
            plugin_dir = self.plugin_dir(code)
            if plugin_dir.is_dir():
                plugin_migration.downgrade(engine, plugin_dir, code, current=current)
            elif current is not None:
                raise AppError(
                    PLUGIN_MIGRATION_INVALID,
                    data={"code": code, "errors": ["插件目录缺失，无法降级"]},
                )

            loaded = self._loaded.pop(code, None)
            if loaded is not None:
                unload(loaded)
            self.events.unsubscribe_plugin(code)
            with Session(engine) as session:
                fresh = session.get(PluginRegistry, code)
                if fresh is None:
                    return
                self._audit_event(
                    session, AUDIT_UNINSTALL, fresh.code, fresh.plugin_version, operator_id
                )
                session.delete(fresh)
                session.commit()

    # ── 启动装配 ──────────────────────────────────────────────────────

    def boot(self) -> None:
        """挂载所有 state=enabled 的插件；单个失败只标记 failed，不阻塞启动。"""
        if not inspect(self.db.engine).has_table(PluginRegistry.__tablename__):
            # READY 但迁移未落库（安装窗口/测试）：不装配插件，避免拖垮内核
            return
        with Session(self.db.engine) as session:
            rows = list(
                session.execute(
                    select(PluginRegistry)
                    .where(PluginRegistry.state == STATE_ENABLED)
                    .order_by(PluginRegistry.code)
                ).scalars()
            )
        for row in rows:
            try:
                manifest = load_manifest(self.plugin_dir(row.code))
                if manifest.code != row.code:
                    raise AppError(PLUGIN_MANIFEST_INVALID, data={"code": row.code})
                current_hash = manifest_hash(self.plugin_dir(row.code))
                if row.manifest_hash and current_hash != row.manifest_hash:
                    raise AppError(
                        PLUGIN_MANIFEST_INVALID,
                        data={"errors": ["manifest 与安装快照不一致，请重新安装"]},
                    )
                with self._lock:
                    self._mount(manifest)
            except Exception as exc:  # noqa: BLE001 - 单插件失败不能拖垮内核
                self._mark_failed(row.code, exc)

    def _mark_failed(self, code: str, exc: Exception) -> None:
        logger.warning("plugin %s 加载失败：%s", code, exc)
        detail = f"{type(exc).__name__}: {exc}"
        try:
            with Session(self.db.engine) as session:
                row = session.get(PluginRegistry, code)
                if row is None:
                    return
                row.state = STATE_FAILED
                row.error_message = detail[:255]
                row.updated_at = utcnow()
                session.add(
                    SysLogSecurity(
                        event=AUDIT_LOAD_FAILED,
                        user_id=None,
                        detail=f"{code} {detail}"[:255],
                        created_at=utcnow(),
                    )
                )
                session.commit()
        except Exception:  # pragma: no cover - 标记失败日志兜底
            logger.exception("plugin %s 失败状态写入失败", code)

    def _remount_after_install(self, code: str, operator_id: int | None) -> None:
        try:
            with Session(self.db.engine) as session:
                row = self._require_row(session, code)
                manifest = self._load_manifest_for(row)
                self._mount(manifest)
                row.error_message = None
                row.updated_at = utcnow()
                self._audit(session, AUDIT_ENABLE, manifest, operator_id)
                session.commit()
        except Exception as exc:  # noqa: BLE001 - 重装挂载失败降级为 failed
            self._loaded.pop(code, None)
            self._mark_failed(code, exc)

    # ── 分发 ──────────────────────────────────────────────────────────

    def dispatch(self, code: str, subpath: str) -> Any:
        with self._lock:
            loaded = self._loaded.get(code)
        if loaded is None:
            raise AppError(PLUGIN_NOT_FOUND, data={"code": code})
        if subpath not in loaded.handlers:
            raise AppError(NOT_FOUND, data={"code": code, "path": subpath})
        # SDK 作用域：插件身份与能力来自 manifest，handler 内无法自造或提权
        with using(loaded.manifest.code, loaded.manifest.capabilities):
            return loaded.handle(subpath)

    # ── 内部 ──────────────────────────────────────────────────────────

    def _mount(self, manifest: PluginManifest) -> None:
        # 重复挂载（重装已启用插件）必须先摘旧订阅，否则 setup() 会叠加一份监听
        self.events.unsubscribe_plugin(manifest.code)
        plugin_dir = self.plugin_dir(manifest.code)
        try:
            self._loaded[manifest.code] = load_server(plugin_dir, manifest)
        except Exception:
            # setup() 阶段可能已注册订阅：加载失败必须回滚，避免 failed 插件仍在派发
            self.events.unsubscribe_plugin(manifest.code)
            raise

    def _load_manifest_for(self, row: PluginRegistry) -> PluginManifest:
        plugin_dir = self.plugin_dir(row.code)
        if not plugin_dir.is_dir():
            raise AppError(PLUGIN_NOT_FOUND, data={"code": row.code, "plugin_dir": str(plugin_dir)})
        manifest = load_manifest(plugin_dir)
        if manifest.code != row.code:
            raise AppError(
                PLUGIN_MANIFEST_INVALID,
                data={"errors": [f"manifest.code 与注册表不一致：{manifest.code} != {row.code}"]},
            )
        return manifest

    def _require_row(self, session: Session, code: str) -> PluginRegistry:
        row = session.get(PluginRegistry, code)
        if row is None:
            raise AppError(PLUGIN_NOT_FOUND, data={"code": code})
        return row

    def _audit(
        self,
        session: Session,
        event: str,
        manifest: PluginManifest,
        operator_id: int | None,
    ) -> None:
        self._audit_event(session, event, manifest.code, manifest.version, operator_id)

    def _audit_event(
        self,
        session: Session,
        event: str,
        code: str,
        version: str,
        operator_id: int | None,
    ) -> None:
        session.add(
            SysLogSecurity(
                event=event,
                user_id=operator_id,
                detail=f"{code} v{version}"[:255],
                created_at=utcnow(),
            )
        )
