"""RBAC 内置权限码与角色（§7.3 / 附录 C）。

- 权限码由 `sys_permission` 固化；角色 → 权限映射由 `sys_role_permission` 固化；
- 迁移 0003 的种子必须与本模块一致（tests/test_auth.py 校验）；
- 高危权限 `progress:direct` / `media:key:any` 默认**不授予任何角色**（仅运维命令/强审计）。
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.orm import Session

from zhyonlineedu.core.db.models.account import SysRolePermission, SysUserRole


@dataclass(frozen=True, slots=True)
class PermissionSpec:
    code: str
    name: str
    risk_level: str  # high / mid / low


PERMISSIONS: tuple[PermissionSpec, ...] = (
    PermissionSpec("system:manage", "系统管理", "high"),
    PermissionSpec("system:cap_probe", "重跑能力探测 / 查看能力位", "mid"),
    PermissionSpec("system:progress_health", "查看进度网关水位与滞后", "mid"),
    PermissionSpec("media:transcode:pause", "暂停 / 恢复转码队列", "mid"),
    PermissionSpec("media:key:any", "跨用户取播放密钥", "high"),
    PermissionSpec("progress:direct", "绕过进度网关直写", "high"),
    PermissionSpec("course:edit", "课程编辑", "mid"),
    PermissionSpec("lesson:play", "课时播放", "low"),
    PermissionSpec("user:manage", "用户管理", "mid"),
    PermissionSpec("member:manage", "会员管理", "mid"),
)

PERMISSION_CODES: frozenset[str] = frozenset(spec.code for spec in PERMISSIONS)

# 高危权限默认无角色持有（附录 C：progress:direct 仅运维命令）
UNASSIGNED_PERMISSIONS: frozenset[str] = frozenset({"progress:direct"})

ROLE_NAMES: Mapping[str, str] = {
    "super_admin": "超级管理员",
    "admin": "管理员",
    "teacher": "讲师",
    "student": "学员",
}

ROLE_PERMISSIONS: Mapping[str, frozenset[str]] = {
    "super_admin": PERMISSION_CODES - UNASSIGNED_PERMISSIONS,
    "admin": frozenset(
        {
            "system:manage",
            "system:cap_probe",
            "system:progress_health",
            "media:transcode:pause",
            "course:edit",
            "lesson:play",
            "user:manage",
            "member:manage",
        }
    ),
    "teacher": frozenset({"course:edit", "lesson:play"}),
    "student": frozenset({"lesson:play"}),
}


def roles_for(session: Session, user_id: int) -> tuple[str, ...]:
    stmt = select(SysUserRole.role_code).where(SysUserRole.user_id == user_id)
    return tuple(sorted(session.execute(stmt).scalars().all()))


def permissions_for(session: Session, user_id: int) -> frozenset[str]:
    stmt = (
        select(SysRolePermission.permission_code)
        .join(SysUserRole, SysUserRole.role_code == SysRolePermission.role_code)
        .where(SysUserRole.user_id == user_id)
        .distinct()
    )
    return frozenset(session.execute(stmt).scalars().all())
