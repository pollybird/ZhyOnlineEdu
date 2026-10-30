"""能力探测协议与 M1.1 占位实现（§4.3 装配顺序门禁）。

真实探测在 M1.2（core/db/probe.py）实现：连库、冒烟、固化 sys_compat_flags。
本模块只定义协议与应用工厂可注入的两种实现，避免 M1.1 依赖未完成的适配层。
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Protocol


class ProbeState(StrEnum):
    PENDING = "pending"
    READY = "ready"
    FAILED = "failed"


@dataclass(frozen=True, slots=True)
class ProbeResult:
    state: ProbeState
    flavor: str | None = None
    capabilities: Mapping[str, bool] = field(default_factory=dict)
    blockers: tuple[str, ...] = ()


class CapabilityProbe(Protocol):
    def probe(self) -> ProbeResult: ...


@dataclass(frozen=True, slots=True)
class PendingProbe:
    """出厂状态：适配层尚未接入（M1.2 前）。"""

    def probe(self) -> ProbeResult:
        return ProbeResult(ProbeState.PENDING)


@dataclass(frozen=True, slots=True)
class StaticProbe:
    """测试 / 运维显式注入的固定结果。"""

    result: ProbeResult

    def probe(self) -> ProbeResult:
        return self.result
