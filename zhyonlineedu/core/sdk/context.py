"""插件 SDK 调用作用域（M3.4）。

口径：
- SDK **只能**在插件路由处理内调用：`PluginService.dispatch` 在调用 handler 前
  通过 `using(code, capabilities)` 绑定上下文；
- 插件身份与能力来自 manifest，不在 handler 参数里传递，插件无法自造或提权；
- 脱离作用域调用一律 `PLUGIN_CAP_DENIED`（403），便于审计定位越权插件。
"""

from __future__ import annotations

from collections.abc import Iterable, Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass

from zhyonlineedu.core.codes import PLUGIN_CAP_DENIED
from zhyonlineedu.core.errors import AppError


@dataclass(frozen=True, slots=True)
class SDKScope:
    """一次插件调用的身份与能力快照（来自 manifest，只读）。"""

    code: str
    capabilities: frozenset[str]

    def allows(self, capability: str) -> bool:
        return capability in self.capabilities


_SCOPE: ContextVar[SDKScope | None] = ContextVar("zhy_plugin_sdk_scope", default=None)


def _denied(reason: str, **data: object) -> AppError:
    payload: dict[str, object] = {"reason": reason}
    payload.update(data)
    return AppError(PLUGIN_CAP_DENIED, data=payload)


@contextmanager
def using(code: str, capabilities: Iterable[str]) -> Iterator[SDKScope]:
    """绑定插件调用作用域（仅内核装配层可调用）。"""
    scope = SDKScope(code=code, capabilities=frozenset(capabilities))
    token = _SCOPE.set(scope)
    try:
        yield scope
    finally:
        _SCOPE.reset(token)


def current() -> SDKScope:
    """当前插件作用域；脱离分发入口调用即拒绝。"""
    scope = _SCOPE.get()
    if scope is None:
        raise _denied("sdk_scope_missing", hint="SDK 仅可在 /api/v1/plugin/<code>/... 处理内调用")
    return scope


def require(capability: str) -> SDKScope:
    """校验能力声明（manifest `capabilities` 白名单）。"""
    scope = current()
    if not scope.allows(capability):
        raise _denied("capability_missing", required=capability, plugin=scope.code)
    return scope


def reset() -> None:
    """仅测试使用：清除上下文。"""
    _SCOPE.set(None)


__all__ = ["SDKScope", "current", "require", "reset", "using"]
