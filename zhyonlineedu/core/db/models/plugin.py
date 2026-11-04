"""插件注册表（§6.11 / §11 · M3）。

口径：
- 一行一插件，`code` 为 manifest 的插件标识（`^[a-z][a-z0-9_]{2,63}$`）；
- `state`：`disabled`（已安装未启用）/ `enabled`（路由已挂载）/ `failed`（入口加载失败，
  内核继续可用，需运维修复后重新 enable）；
- 历史直写能力不再有"签名放行"分支：`capabilities_v2` 仅保存全 false 快照，便于审计；
- `schema_rev` 是插件**独立迁移链**的当前 revision，与内核 `alembic_version` 解耦；
- JSON 结构统一落 `Text`，避免方言 JSON 差异（与 `sys_mode_config.compat_flags` 同口径）。
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import String, Text
from sqlalchemy.orm import Mapped, mapped_column

from ..base import Base
from ..types import UTCDatetime, VersionedMixin
from .system import utcnow

STATE_DISABLED = "disabled"
STATE_ENABLED = "enabled"
STATE_FAILED = "failed"
STATES: tuple[str, ...] = (STATE_DISABLED, STATE_ENABLED, STATE_FAILED)


class PluginRegistry(VersionedMixin, Base):
    """插件注册表（字段与 `migrations/versions/0014_plugin_framework.py` 一一对应）。"""

    __tablename__ = "plugin_registry"

    code: Mapped[str] = mapped_column(String(64), primary_key=True)
    name: Mapped[str] = mapped_column(String(64), nullable=False)
    # 插件自身版本（manifest.version）；与 VersionedMixin.version（乐观锁计数）区分
    plugin_version: Mapped[str] = mapped_column(String(16), nullable=False)
    state: Mapped[str] = mapped_column(
        String(16), nullable=False, default=STATE_DISABLED, server_default=STATE_DISABLED
    )
    core_api_min: Mapped[str] = mapped_column(String(16), nullable=False)
    table_prefix: Mapped[str] = mapped_column(String(64), nullable=False)
    entry_server: Mapped[str] = mapped_column(String(255), nullable=False)
    # 前端 ESM 入口（`web/` 下的相对路径，可省略）；供应 `/plugin/<code>/assets/...` 与菜单渲染
    entry_web: Mapped[str | None] = mapped_column(String(255), nullable=True)
    # 插件独立迁移链当前 revision（未跑迁移为 NULL）
    schema_rev: Mapped[str | None] = mapped_column(String(64), nullable=True)
    # manifest 原文 sha256：升级/篡改检测与审计回溯
    manifest_hash: Mapped[str] = mapped_column(
        String(64), nullable=False, default="", server_default=""
    )
    supported_modes: Mapped[str] = mapped_column(
        Text, nullable=False, default="[]", server_default="[]"
    )
    capabilities: Mapped[str] = mapped_column(
        Text, nullable=False, default="[]", server_default="[]"
    )
    capabilities_v2: Mapped[str] = mapped_column(
        Text, nullable=False, default="{}", server_default="{}"
    )
    routes: Mapped[str] = mapped_column(Text, nullable=False, default="[]", server_default="[]")
    menu: Mapped[str] = mapped_column(Text, nullable=False, default="[]", server_default="[]")
    error_message: Mapped[str | None] = mapped_column(String(255), nullable=True)
    installed_at: Mapped[datetime] = mapped_column(UTCDatetime, nullable=False, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(UTCDatetime, nullable=False, default=utcnow)
