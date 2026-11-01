"""插件入口模块加载/卸载（§11 · M3.2）。

插件入口 `entry_server` 由 manifest 指定，必须是插件目录内的相对路径，且导出
`HANDLERS: Mapping[str, Callable[[], dict | tuple[dict, int]]]`：
- key 必须是 manifest 声明的 `routes` 子路径（未声明即拒绝加载，避免影子路由）；
- 禁用/卸载时从进程内注册表移除并对 `sys.modules` 做尽力清理，内核路由表随之消失
  （残留路由一律 404，不靠进程重启）。
"""

from __future__ import annotations

import importlib.util
import sys
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from zhyonlineedu.core.codes import PLUGIN_LOAD_FAILED
from zhyonlineedu.core.errors import AppError
from zhyonlineedu.core.plugin.manifest import PluginManifest

HANDLERS_ATTR = "HANDLERS"
PluginHandler = Callable[[], Any]


@dataclass(frozen=True, slots=True)
class LoadedPlugin:
    manifest: PluginManifest
    module_name: str
    handlers: Mapping[str, PluginHandler]

    def handle(self, subpath: str) -> Any:
        handler = self.handlers.get(subpath)
        if handler is None:
            raise AppError(PLUGIN_LOAD_FAILED, data={"code": self.manifest.code, "route": subpath})
        return handler()


def module_name(code: str) -> str:
    return f"zhy_plugin_{code}"


def load_server(plugin_dir: Path, manifest: PluginManifest) -> LoadedPlugin:
    """加载插件入口并校验 HANDLERS 与 manifest.routes 一致。"""
    entry = plugin_dir / manifest.entry_server
    name = module_name(manifest.code)
    spec = importlib.util.spec_from_file_location(name, entry)
    if spec is None or spec.loader is None:  # pragma: no cover - 解释器异常
        raise AppError(PLUGIN_LOAD_FAILED, data={"code": manifest.code, "entry": entry.name})
    module = importlib.util.module_from_spec(spec)
    try:
        spec.loader.exec_module(module)
    except Exception as exc:
        raise AppError(
            PLUGIN_LOAD_FAILED,
            data={"code": manifest.code, "error": f"{type(exc).__name__}: {exc}"},
        ) from exc

    raw = getattr(module, HANDLERS_ATTR, None)
    if not isinstance(raw, Mapping):
        raise AppError(
            PLUGIN_LOAD_FAILED,
            data={"code": manifest.code, "error": f"入口必须导出 {HANDLERS_ATTR} 映射"},
        )
    handlers: dict[str, PluginHandler] = {}
    for route, handler in raw.items():
        if not isinstance(route, str) or not callable(handler):
            raise AppError(
                PLUGIN_LOAD_FAILED,
                data={"code": manifest.code, "error": f"HANDLERS 键值非法：{route!r}"},
            )
        handlers[route] = handler

    declared = set(manifest.routes)
    undeclared = sorted(set(handlers) - declared)
    if undeclared:
        raise AppError(
            PLUGIN_LOAD_FAILED,
            data={"code": manifest.code, "undeclared": undeclared},
        )
    missing = sorted(declared - set(handlers))
    if missing:
        raise AppError(
            PLUGIN_LOAD_FAILED,
            data={"code": manifest.code, "missing_handlers": missing},
        )
    sys.modules[name] = module
    return LoadedPlugin(manifest=manifest, module_name=name, handlers=handlers)


def unload(loaded: LoadedPlugin) -> None:
    """进程内卸载：移除模块缓存（禁用后再次启用时重新加载，拿到最新代码）。"""
    sys.modules.pop(loaded.module_name, None)
