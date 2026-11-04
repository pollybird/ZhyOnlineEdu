"""事件总线（§11 · M3.7）。

**语义**：观察者（observe）模式——订阅方只能读事件，**不能改写主流程结果**，
也不能把"写进度"绕回内核（进度唯一写入入口仍是 `ProgressGateway` + `ProgressFlush`）。

**派发点**：`lesson.finished` 由 `ProgressFlush` 在「未完成 → 已完成」跃迁、
**该组事务提交之后**发出。触发进程是 `progress-flush` worker，因此插件订阅方
永远不会占用 HTTP 请求线程。

**隔离**：
- 单个订阅抛异常 → 记录到 `EmitReport.failures`，继续派发后面的订阅；
- 单次耗时超过订阅预算 → 记录到 `EmitReport.timeouts`，同样继续（同步代码无法强杀，
  只能告警 + 保证不影响其它订阅）；派发总耗时上限 ≈ 订阅数 × 预算。
- 总线不吞掉订阅的返回值，也不允许订阅方修改 payload（传入只读 `MappingProxy`）。

**幂等**：总线**不**去重；订阅方必须按业务主键幂等（参考插件用 `sdk.store.upsert`）。
`lesson.finished` 的 payload 中 `via_ticket_preview=true` 时所有订阅方必须跳过统计/发证书。
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from threading import Lock
from types import MappingProxyType
from typing import Any

from zhyonlineedu.core.codes import VALIDATION_FAILED
from zhyonlineedu.core.errors import AppError

logger = logging.getLogger(__name__)

EVENT_LESSON_FINISHED = "lesson.finished"
DEFAULT_TIMEOUT_MS = 200
MIN_TIMEOUT_MS = 10
MAX_TIMEOUT_MS = 2000

#: 允许订阅/派发的事件白名单；新增事件必须同时补文档与用例
ALLOWED_EVENTS: Mapping[str, str] = {
    EVENT_LESSON_FINISHED: "课时完成（幂等；预览票来源的进度不发证书、不计数）",
}

EventHandler = Callable[[Mapping[str, Any]], Any]


@dataclass(frozen=True, slots=True)
class Subscription:
    event: str
    handler: EventHandler
    plugin_code: str | None
    timeout_ms: int

    @property
    def owner(self) -> str:
        return self.plugin_code or "core"


@dataclass(slots=True)
class EmitReport:
    """一次派发的结果（订阅方失败不影响调用方）。"""

    event: str
    delivered: int = 0
    failures: list[dict[str, Any]] = field(default_factory=list)
    timeouts: list[dict[str, Any]] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.failures and not self.timeouts

    def as_dict(self) -> dict[str, Any]:
        return {
            "event": self.event,
            "delivered": self.delivered,
            "failures": list(self.failures),
            "timeouts": list(self.timeouts),
        }


def _validate_timeout(timeout_ms: int) -> int:
    if timeout_ms < MIN_TIMEOUT_MS or timeout_ms > MAX_TIMEOUT_MS:
        raise AppError(
            VALIDATION_FAILED,
            data={"errors": [f"timeout_ms 必须在 {MIN_TIMEOUT_MS}..{MAX_TIMEOUT_MS}"]},
        )
    return timeout_ms


def validate_event_name(event: str) -> str:
    if event not in ALLOWED_EVENTS:
        raise AppError(
            VALIDATION_FAILED,
            data={"errors": [f"未登记的事件：{event}"], "allowed": sorted(ALLOWED_EVENTS)},
        )
    return event


class EventBus:
    """进程内订阅表；线程安全（插件启停与派发可能并发）。"""

    def __init__(self, *, default_timeout_ms: int = DEFAULT_TIMEOUT_MS) -> None:
        self._subscriptions: dict[str, list[Subscription]] = {}
        self._lock = Lock()
        self._default_timeout_ms = _validate_timeout(default_timeout_ms)

    def subscribe(
        self,
        event: str,
        handler: EventHandler,
        *,
        plugin_code: str | None = None,
        timeout_ms: int | None = None,
    ) -> Subscription:
        validate_event_name(event)
        if not callable(handler):
            raise AppError(VALIDATION_FAILED, data={"errors": ["handler 必须可调用"]})
        sub = Subscription(
            event=event,
            handler=handler,
            plugin_code=plugin_code,
            timeout_ms=_validate_timeout(
                self._default_timeout_ms if timeout_ms is None else timeout_ms
            ),
        )
        with self._lock:
            self._subscriptions.setdefault(event, []).append(sub)
        return sub

    def unsubscribe_plugin(self, code: str) -> int:
        """按插件 code 摘除全部订阅（禁用/卸载/加载失败回滚）。返回摘除条数。"""
        removed = 0
        with self._lock:
            for event, items in list(self._subscriptions.items()):
                keep = [sub for sub in items if sub.plugin_code != code]
                removed += len(items) - len(keep)
                if keep:
                    self._subscriptions[event] = keep
                else:
                    self._subscriptions.pop(event, None)
        return removed

    def subscriptions(self, event: str | None = None) -> tuple[Subscription, ...]:
        with self._lock:
            if event is None:
                return tuple(sub for items in self._subscriptions.values() for sub in items)
            return tuple(self._subscriptions.get(event, ()))

    def emit(self, event: str, payload: Mapping[str, Any]) -> EmitReport:
        """派发事件：逐订阅隔离异常与耗时，永不让订阅方打断调用方。"""
        validate_event_name(event)
        frozen = MappingProxyType(dict(payload))
        report = EmitReport(event=event)
        with self._lock:
            subscriptions = list(self._subscriptions.get(event, ()))
        for sub in subscriptions:
            started = time.perf_counter()
            try:
                sub.handler(frozen)
            except Exception as exc:  # noqa: BLE001 - 单订阅失败必须隔离
                logger.exception("事件订阅失败 event=%s owner=%s", event, sub.owner)
                report.failures.append(
                    {
                        "owner": sub.owner,
                        "error": f"{type(exc).__name__}: {exc}",
                    }
                )
                continue
            elapsed_ms = int((time.perf_counter() - started) * 1000)
            if elapsed_ms > sub.timeout_ms:
                logger.warning(
                    "事件订阅超时 event=%s owner=%s elapsed=%sms budget=%sms",
                    event,
                    sub.owner,
                    elapsed_ms,
                    sub.timeout_ms,
                )
                report.timeouts.append(
                    {
                        "owner": sub.owner,
                        "elapsed_ms": elapsed_ms,
                        "budget_ms": sub.timeout_ms,
                    }
                )
                continue
            report.delivered += 1
        return report


#: 应用级默认总线（`create_app` 与 worker 宿主都会在 `app.extensions` 注册同一实例语义的实例）
_default_bus = EventBus()


def get_bus() -> EventBus:
    """进程级总线；无 app 上下文时（如纯单元测试）也能派发。"""
    try:
        from flask import current_app, has_app_context

        if has_app_context():
            bus = current_app.extensions.get("zhy_events")
            if isinstance(bus, EventBus):
                return bus
    except ImportError:  # pragma: no cover - Flask 恒定可用
        pass
    return _default_bus
