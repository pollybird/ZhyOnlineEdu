"""ORM 模型包：导入即注册进 `Base.metadata`，供 Alembic 迁移与 create_all 共用。

M1.6 起账号 / 会话 / RBAC 表由 `account` 模块注册；M2.1 起目录/课程三层与
媒体资产由 `catalog` / `media` 模块注册；M2.5 进度与票据、M2.6 题库作答由
`learn` / `quiz` 模块注册；迁移链与模型列逐一比对（D37）；
M3 起插件注册表由 `plugin` 模块注册。
"""

from __future__ import annotations

from .account import (
    AccountSession,
    AccountUser,
    SysLogSecurity,
    SysPermission,
    SysRole,
    SysRolePermission,
    SysUserRole,
)
from .catalog import EduCourse, EduCourseChapter, EduCourseLesson, EduSubject
from .commerce import MemberLevel, MemberSubscription, PayEvent, PayOrder
from .learn import LearnCourseProgress, LearnProgress, LearnProgressGate, LearnProgressLog
from .media import MediaAsset, MediaKeySession
from .plugin import PluginRegistry
from .quiz import QuizAttempt, QuizQuestion
from .system import SysCompatFlags, SysConfig, SysInstallState, SysModeConfig, SysModeState
from .users import EduClass, EduClassMember, SysImportBatch

__all__ = [
    "AccountSession",
    "AccountUser",
    "EduClass",
    "EduClassMember",
    "EduCourse",
    "EduCourseChapter",
    "EduCourseLesson",
    "EduSubject",
    "LearnCourseProgress",
    "LearnProgress",
    "LearnProgressGate",
    "LearnProgressLog",
    "MediaAsset",
    "MediaKeySession",
    "MemberLevel",
    "MemberSubscription",
    "PayEvent",
    "PayOrder",
    "PluginRegistry",
    "QuizAttempt",
    "QuizQuestion",
    "SysCompatFlags",
    "SysConfig",
    "SysImportBatch",
    "SysInstallState",
    "SysLogSecurity",
    "SysModeConfig",
    "SysModeState",
    "SysPermission",
    "SysRole",
    "SysRolePermission",
    "SysUserRole",
]
