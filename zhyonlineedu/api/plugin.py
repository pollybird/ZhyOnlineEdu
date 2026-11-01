"""插件管理与路由分发接口（§11 · M3.2）。

权限与口径：
- 管理接口全部 `system:manage`（super_admin / admin 默认持有），teacher / student 403；
- 插件业务路由统一入口 `/api/v1/plugin/<code>/<subpath>`，**只转发已启用插件且 manifest
  声明过的子路径**；禁用/卸载/未安装一律 404（不泄露插件是否存在）；
- 插件入口的返回值只描述 `data`，统一信封由内核包裹；
- 生命周期变更即时生效（禁用不重启，残留路由 404）。
"""

from __future__ import annotations

from flask import Blueprint, Response, current_app

from zhyonlineedu.api.schemas import (
    PluginDispatchOut,
    PluginListOut,
    PluginOut,
    PluginUninstallOut,
)
from zhyonlineedu.core.auth.decorators import authenticate_request, require_permission
from zhyonlineedu.core.envelope import ok
from zhyonlineedu.core.openapi import documented
from zhyonlineedu.modules.plugin.service import PluginService

bp = Blueprint("plugin", __name__, url_prefix="/api/v1")

MANAGE = "system:manage"
METHODS = ("GET", "POST", "PUT", "PATCH", "DELETE")


def _service() -> PluginService:
    return current_app.extensions["zhy_plugin"]  # type: ignore[no-any-return]


@bp.get("/system/plugins")
@require_permission(MANAGE)
@documented(
    "插件列表",
    description=(
        "返回 `plugin_registry` 全量插件：状态（disabled / enabled / failed）、"
        "能力声明、独立迁移链 revision 与加载失败原因。"
    ),
    response=PluginListOut,
    tags=("plugin",),
)
def list_plugins() -> tuple[Response, int]:
    return ok(_service().list())


@bp.post("/system/plugins/<code>/install")
@require_permission(MANAGE)
@documented(
    "安装插件",
    description=(
        "从 `plugin.dir/<code>` 读取 manifest.json 并按附录 E 校验，执行插件独立迁移链到 head，"
        "写入注册表（默认 disabled，不自动挂载路由）。"
        "幂等：重复安装只做 manifest 快照刷新与迁移补齐。"
    ),
    response=PluginOut,
    tags=("plugin",),
)
def install_plugin(code: str) -> tuple[Response, int]:
    user = authenticate_request()
    return ok(_service().install(code, operator_id=user.id).as_dict())


@bp.post("/system/plugins/<code>/enable")
@require_permission(MANAGE)
@documented(
    "启用插件",
    description=(
        "加载 `entry_server` 并校验 HANDLERS 与 manifest.routes 一致后挂载路由；"
        "加载失败返回 `PLUGIN_LOAD_FAILED` 且注册表标记 failed，内核继续可用。"
    ),
    response=PluginOut,
    tags=("plugin",),
)
def enable_plugin(code: str) -> tuple[Response, int]:
    user = authenticate_request()
    return ok(_service().enable(code, operator_id=user.id).as_dict())


@bp.post("/system/plugins/<code>/disable")
@require_permission(MANAGE)
@documented(
    "禁用插件",
    description="立即摘除插件路由（不重启）；禁用后访问插件路由一律 404，内核与数据完整可用。",
    response=PluginOut,
    tags=("plugin",),
)
def disable_plugin(code: str) -> tuple[Response, int]:
    user = authenticate_request()
    return ok(_service().disable(code, operator_id=user.id).as_dict())


@bp.delete("/system/plugins/<code>")
@require_permission(MANAGE)
@documented(
    "卸载插件",
    description=(
        "先降级插件独立迁移链（删插件表），再删注册表行；enabled 态直接卸载返回 409 "
        "`PLUGIN_STATE_INVALID`（需先禁用）。"
    ),
    response=PluginUninstallOut,
    tags=("plugin",),
)
def uninstall_plugin(code: str) -> tuple[Response, int]:
    user = authenticate_request()
    _service().uninstall(code, operator_id=user.id)
    return ok({"code": code, "uninstalled": True})


@bp.route("/plugin/<code>/<path:subpath>", methods=METHODS)
@documented(
    "插件路由分发",
    description=(
        "统一入口：`/api/v1/plugin/<code>/<subpath>`。仅转发**已启用**插件且 manifest "
        "`routes` 声明过的子路径；未安装/已禁用/未声明一律 404。"
    ),
    response=PluginDispatchOut,
    tags=("plugin",),
)
def dispatch_plugin(code: str, subpath: str) -> tuple[Response, int]:
    authenticate_request()
    return ok(_service().dispatch(code, subpath))
