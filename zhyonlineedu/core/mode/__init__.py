"""双模式内核（§9：模式上下文 + 统一访问判定）。

- `context`：`ModeContext` 以 `epoch` 为版本，一次请求内只读一次（MODE-FLIP-001）；
- `policy`：18 条决策表是**唯一判定入口**（§9.4），模式差异若没进表视为未实现。
"""

from __future__ import annotations

from .context import ACTIVE_MODE_KEY, MODES, ModeContext, ModeState, ensure_state, get_mode_context
from .policy import (
    DECISION_TABLE,
    DEFAULT_POLICIES,
    AccessSpec,
    Decision,
    DecisionRow,
    ModePolicy,
    TicketDecision,
    TicketSpec,
    decide_access,
    decide_ticket,
    policy_for,
)

__all__ = [
    "ACTIVE_MODE_KEY",
    "DECISION_TABLE",
    "DEFAULT_POLICIES",
    "MODES",
    "AccessSpec",
    "Decision",
    "DecisionRow",
    "ModeContext",
    "ModePolicy",
    "ModeState",
    "TicketDecision",
    "TicketSpec",
    "decide_access",
    "decide_ticket",
    "ensure_state",
    "get_mode_context",
    "policy_for",
]
