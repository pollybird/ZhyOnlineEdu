"""插件框架门面（§11）：manifest 校验、入口加载、独立迁移链。

模块划分：
- `manifest`：附录 E 校验（字段/能力/表前缀/裸 SQL 黑名单）；
- `loader`：入口模块加载与卸载（禁用即摘除路由，不依赖重启）；
- `zhyonlineedu.core.db.plugin_migration`：每插件独立迁移链（`plg_<code>_` + `delete_seq`）。

生命周期与数据库注册表由 `zhyonlineedu.modules.plugin.service.PluginService` 负责。
"""

from __future__ import annotations

from .loader import LoadedPlugin, load_server, unload
from .manifest import (
    ALLOWED_CAPABILITIES,
    MANIFEST_FILE,
    PluginManifest,
    expected_table_prefix,
    load_manifest,
    validate_manifest,
)

__all__ = [
    "ALLOWED_CAPABILITIES",
    "LoadedPlugin",
    "MANIFEST_FILE",
    "PluginManifest",
    "expected_table_prefix",
    "load_manifest",
    "load_server",
    "unload",
    "validate_manifest",
]
