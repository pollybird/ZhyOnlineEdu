"""统一访问判定（§9.4 · M2.10）：18 条决策表是唯一判定入口。

规则 1~14 由 `decide_access` 执行（账号状态 / 课程模式 / 上架态 / 授权方式）；
规则 15~18 由 `decide_ticket` 执行（票据 nonce / 档位裁剪 / 超管预览）。

任何"模式差异"若没有进这张表，视为未实现（计划书 §1.1）。
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

from zhyonlineedu.core.codes import (
    ACCESS_CLASS_REQUIRED,
    ACCESS_LEVEL_REQUIRED,
    COURSE_NOT_PUBLISHED,
    MODE_ACCESS_DENIED,
    MODE_COURSE_MISMATCH,
    NOT_FOUND,
    Code,
)

COURSE_MODES: tuple[str, ...] = ("internal", "external", "both")
ACCESS_TYPES: tuple[str, ...] = ("public", "member", "class")
PROFILE_NAMES: tuple[str, ...] = ("360", "720", "1080")


@dataclass(frozen=True, slots=True)
class DecisionRow:
    """决策表的一行（文档与用例共用的元数据，也是"未进表=未实现"的清单）。"""

    rule: str
    mode: str  # internal / external / any
    scenario: str
    expected: str | Code  # allow 或错误码
    note: str = ""


DECISION_TABLE: tuple[DecisionRow, ...] = (
    DecisionRow("MODE-001", "internal", "在册学员 · published · public", "allow"),
    DecisionRow(
        "MODE-002", "internal", "在册学员 · published · member（无会员等级）", ACCESS_LEVEL_REQUIRED
    ),
    DecisionRow("MODE-003", "internal", "在册学员 · published · class · 班级成员", "allow"),
    DecisionRow(
        "MODE-004", "internal", "在册学员 · published · class · 非成员", ACCESS_CLASS_REQUIRED
    ),
    DecisionRow("MODE-005", "external", "在册学员 · published · public", "allow"),
    DecisionRow("MODE-006", "external", "在册学员 · published · member · 等级足够", "allow"),
    DecisionRow(
        "MODE-007",
        "external",
        "在册学员 · published · class（对外模式不启用班级）",
        MODE_ACCESS_DENIED,
        "班级授权只在内部模式生效",
    ),
    DecisionRow(
        "MODE-008",
        "internal",
        "在册学员 · published · 课程 mode=external",
        MODE_COURSE_MISMATCH,
        "课程可见性由当前模式决定",
    ),
    DecisionRow(
        "MODE-009",
        "external",
        "在册学员 · published · 课程 mode=internal",
        MODE_COURSE_MISMATCH,
    ),
    DecisionRow("MODE-010", "internal", "在册学员 · published · 课程 mode=both", "allow"),
    DecisionRow("MODE-011", "external", "在册学员 · published · 课程 mode=both", "allow"),
    DecisionRow("MODE-012", "any", "在册学员 · draft", COURSE_NOT_PUBLISHED),
    DecisionRow("MODE-013", "any", "在册学员 · archived", NOT_FOUND),
    DecisionRow(
        "MODE-014",
        "any",
        "管理员预览 · draft",
        "allow",
        "允许播放但 preview=true，进度不计入统计",
    ),
    DecisionRow("MODE-015", "any", "合法学员 · ticket 有效 · nonce 首次", "allow", "允许取 key"),
    DecisionRow("MODE-016", "any", "合法学员 · nonce 重放", Code.MEDIA_TICKET_REPLAY),
    DecisionRow(
        "MODE-017",
        "external",
        "月度会员请求 1080p，仅授权 720p",
        "allow",
        "master playlist 服务端裁剪，profile_allowed 不含 1080",
    ),
    DecisionRow(
        "MODE-018",
        "any",
        "超管预览未上架课程",
        "allow",
        "ticket preview=true；learn_progress 不写入、进度流水 source=preview 被过滤",
    ),
)


@dataclass(frozen=True, slots=True)
class AccessSpec:
    """一次访问判定的输入（不含 IO，便于把 18 条表钉进单测）。"""

    mode: str
    course_mode: str
    course_status: str
    access_type: str
    lesson_is_free: bool = False
    can_preview: bool = False
    class_member: bool = False
    member_level: int | None = None
    required_level: int = 0
    epoch: int = 1


@dataclass(frozen=True, slots=True)
class Decision:
    allowed: bool
    code: str | None
    preview: bool
    reason: str
    rule: str
    detail: dict[str, Any] = field(default_factory=dict)


def _deny(rule: str, code: str, reason: str, **detail: Any) -> Decision:
    return Decision(False, code, False, reason, rule, detail)


def _allow(rule: str, reason: str = "allowed", *, preview: bool = False, **detail: Any) -> Decision:
    return Decision(True, None, preview, reason, rule, detail)


def decide_access(spec: AccessSpec) -> Decision:
    """规则 1~14：账号状态之外的全部"能否看/能否播"判定。"""
    if spec.course_status == "archived":
        return _deny("MODE-013", NOT_FOUND, "course_archived")

    # 课程模式与当前模式不匹配：管理员预览可跨模式，学员一律拒绝
    if spec.course_mode not in ("both", spec.mode) and not spec.can_preview:
        return _deny(
            "MODE-008" if spec.mode == "internal" else "MODE-009",
            MODE_COURSE_MISMATCH,
            "course_mode_mismatch",
        )

    if spec.course_status != "published":
        if not spec.can_preview:
            return _deny("MODE-012", COURSE_NOT_PUBLISHED, "course_not_published")
        return _allow("MODE-014", "admin_preview", preview=True)

    # 管理员预览（含跨模式/会员课程）：放行但不计入统计
    if spec.can_preview:
        return _allow("MODE-014", "admin_preview", preview=True)

    if spec.lesson_is_free:
        return _allow("MODE-001", "lesson_free")

    if spec.access_type == "public":
        return _allow("MODE-001" if spec.mode == "internal" else "MODE-005", "public")

    if spec.access_type == "member":
        if spec.member_level is None:
            return _deny("MODE-002", ACCESS_LEVEL_REQUIRED, "membership_required")
        if int(spec.member_level) < int(spec.required_level):
            return _deny("MODE-002", ACCESS_LEVEL_REQUIRED, "membership_level_low")
        return _allow("MODE-006", "membership_level_ok")

    if spec.access_type == "class":
        if spec.mode == "external":
            # 对外模式不启用班级：即便有成员关系也不放行
            return _deny("MODE-007", MODE_ACCESS_DENIED, "class_not_supported_external")
        if not spec.class_member:
            return _deny("MODE-004", ACCESS_CLASS_REQUIRED, "class_membership_required")
        return _allow("MODE-003", "class_member")

    return _deny("MODE-004", ACCESS_CLASS_REQUIRED, "unknown_access_type")


@dataclass(frozen=True, slots=True)
class TicketSpec:
    """票据侧输入（规则 15~18）。"""

    mode: str = "any"
    nonce_state: str = "first"  # first / replay
    requested_profile: str | None = None
    allowed_profiles: tuple[str, ...] = PROFILE_NAMES
    preview: bool = False
    course_status: str = "published"


@dataclass(frozen=True, slots=True)
class TicketDecision:
    allowed: bool
    code: Code | str | None
    profiles: tuple[str, ...]
    preview: bool
    reason: str
    rule: str


def decide_ticket(spec: TicketSpec) -> TicketDecision:
    """规则 15~18：nonce 单次、档位服务端裁剪、超管预览不计统计。"""
    if spec.nonce_state == "replay":
        return TicketDecision(
            False,
            Code.MEDIA_TICKET_REPLAY,
            (),
            spec.preview,
            "nonce_replay",
            "MODE-016",
        )
    allowed = tuple(p for p in spec.allowed_profiles if p in PROFILE_NAMES)
    if spec.requested_profile is not None and spec.requested_profile not in allowed:
        return TicketDecision(
            False,
            Code.MEDIA_TICKET_MISMATCH,
            allowed,
            spec.preview,
            "profile_not_allowed",
            "MODE-017",
        )
    if spec.preview or spec.course_status != "published":
        return TicketDecision(True, None, allowed, True, "admin_preview", "MODE-018")
    return TicketDecision(True, None, allowed, False, "ticket_ok", "MODE-015")


@dataclass(frozen=True, slots=True)
class ModePolicy:
    """模式锁定的运行参数（票据 TTL / 进度窗口 / 试看秒数）。"""

    ttl_playlist: int = 900
    ttl_key: int = 120
    preview_seconds: int = 0
    progress_interval_sec: int = 30


DEFAULT_POLICIES: Mapping[str, ModePolicy] = {
    "internal": ModePolicy(preview_seconds=0),
    "external": ModePolicy(preview_seconds=180),
}


def policy_for(state: Any) -> ModePolicy:
    """从 `sys_mode_config(mode, 'policy')` 的 JSON 解析策略；缺省走模式默认值。"""
    raw = getattr(state, "policy", None) or {}
    base = DEFAULT_POLICIES.get(getattr(state, "mode", "internal"), DEFAULT_POLICIES["internal"])
    values: dict[str, int] = {}
    for field_name in ("ttl_playlist", "ttl_key", "preview_seconds", "progress_interval_sec"):
        value = raw.get(field_name) if isinstance(raw, Mapping) else None
        if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
            values[field_name] = value
    return ModePolicy(
        ttl_playlist=values.get("ttl_playlist", base.ttl_playlist),
        ttl_key=values.get("ttl_key", base.ttl_key),
        preview_seconds=values.get("preview_seconds", base.preview_seconds),
        progress_interval_sec=values.get("progress_interval_sec", base.progress_interval_sec),
    )


__all__ = [
    "ACCESS_TYPES",
    "COURSE_MODES",
    "DECISION_TABLE",
    "DEFAULT_POLICIES",
    "PROFILE_NAMES",
    "AccessSpec",
    "Decision",
    "DecisionRow",
    "ModePolicy",
    "TicketDecision",
    "TicketSpec",
    "decide_access",
    "decide_ticket",
    "policy_for",
]
