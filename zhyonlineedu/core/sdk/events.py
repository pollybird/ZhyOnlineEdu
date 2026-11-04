"""插件事件订阅（M3.7）。

只读派生：插件**只能订阅**内核已登记的事件，**不能派发**（避免伪造 `lesson.finished`
绕过进度唯一写入入口）。订阅在插件 `setup()` 阶段注册，禁用/卸载时由内核按 code 全量摘除。

订阅回调在触发时重新绑定插件作用域（`code` + manifest `capabilities`）：
事件由 worker 派发，回调内同样可以使用 `sdk.store`，能力门禁与 HTTP 分发一致。

使用（必须在 manifest 声明 `event:subscribe` 能力）：

```python
from zhyonlineedu.core import sdk

def on_lesson_finished(payload):
    if payload["via_ticket_preview"]:
        return                      # 预览不计：不发证书、不计数
    sdk.store.upsert(...)           # 幂等写入自有表（plg_<code>_ 前缀）

def setup():
    sdk.events.subscribe("lesson.finished", on_lesson_finished)
```
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from zhyonlineedu.core.codes import PLUGIN_CAP_DENIED
from zhyonlineedu.core.errors import AppError
from zhyonlineedu.core.events import ALLOWED_EVENTS, EventHandler, get_bus, validate_event_name

from .context import current, require, using


def _bind_scope(code: str, capabilities: frozenset[str], handler: EventHandler) -> EventHandler:
    """把回调包在插件作用域内：worker 派发事件时 SDK 门禁与分发路径完全一致。"""

    def invoke(payload: Mapping[str, Any]) -> Any:
        with using(code, capabilities):
            return handler(payload)

    return invoke


def subscribe(event: str, handler: EventHandler, *, timeout_ms: int | None = None) -> None:
    """订阅内核事件；同一插件重复订阅同一 handler 不会去重（由插件自行保证）。"""
    scope = require("event:subscribe")
    validate_event_name(event)
    if not callable(handler):
        raise AppError(PLUGIN_CAP_DENIED, data={"reason": "handler_not_callable"})
    get_bus().subscribe(
        event,
        _bind_scope(scope.code, scope.capabilities, handler),
        plugin_code=scope.code,
        timeout_ms=timeout_ms,
    )


def available() -> tuple[str, ...]:
    """当前允许订阅的事件名（插件启动时可自检，避免拼错字符串）。"""
    current()
    return tuple(sorted(ALLOWED_EVENTS))


__all__: list[str] = ["available", "subscribe"]
