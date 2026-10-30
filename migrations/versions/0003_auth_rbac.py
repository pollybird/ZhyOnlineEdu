# x-zhy-cap: returning:optional, alter_type_online:false
"""create auth / session / RBAC tables and seeds

Revision ID: 0003_auth_rbac
Revises: 0002_install_step
Create Date: 2026-10-28 00:30:00.000000

M1.6：账号、端会话、RBAC（角色 / 权限 / 绑定）与安全日志。
种子与 `zhyonlineedu/core/auth/rbac.py` 一致；高危权限不授予任何内置角色。
仅新增表（无类型变更），国产库可直接执行。
"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0003_auth_rbac"
down_revision: str | None = "0002_install_step"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_PERMISSIONS: tuple[tuple[str, str, str], ...] = (
    ("system:manage", "系统管理", "high"),
    ("system:cap_probe", "重跑能力探测 / 查看能力位", "mid"),
    ("system:progress_health", "查看进度网关水位与滞后", "mid"),
    ("media:transcode:pause", "暂停 / 恢复转码队列", "mid"),
    ("media:key:any", "跨用户取播放密钥", "high"),
    ("progress:direct", "绕过进度网关直写", "high"),
    ("course:edit", "课程编辑", "mid"),
    ("lesson:play", "课时播放", "low"),
    ("user:manage", "用户管理", "mid"),
)

_ROLES: tuple[tuple[str, str, bool], ...] = (
    ("super_admin", "超级管理员", True),
    ("admin", "管理员", True),
    ("teacher", "讲师", True),
    ("student", "学员", True),
)

_ROLE_PERMISSIONS: tuple[tuple[str, str], ...] = (
    ("super_admin", "system:manage"),
    ("super_admin", "system:cap_probe"),
    ("super_admin", "system:progress_health"),
    ("super_admin", "media:transcode:pause"),
    ("super_admin", "media:key:any"),
    ("super_admin", "course:edit"),
    ("super_admin", "lesson:play"),
    ("super_admin", "user:manage"),
    ("admin", "system:manage"),
    ("admin", "system:cap_probe"),
    ("admin", "system:progress_health"),
    ("admin", "media:transcode:pause"),
    ("admin", "course:edit"),
    ("admin", "lesson:play"),
    ("admin", "user:manage"),
    ("teacher", "course:edit"),
    ("teacher", "lesson:play"),
    ("student", "lesson:play"),
)


def _seed() -> None:
    permission = sa.table(
        "sys_permission",
        sa.column("code", sa.String),
        sa.column("name", sa.String),
        sa.column("risk_level", sa.String),
    )
    role = sa.table(
        "sys_role",
        sa.column("code", sa.String),
        sa.column("name", sa.String),
        sa.column("is_builtin", sa.Boolean),
    )
    role_permission = sa.table(
        "sys_role_permission",
        sa.column("role_code", sa.String),
        sa.column("permission_code", sa.String),
    )
    op.bulk_insert(
        permission,
        [{"code": code, "name": name, "risk_level": risk} for code, name, risk in _PERMISSIONS],
    )
    op.bulk_insert(
        role,
        [{"code": code, "name": name, "is_builtin": builtin} for code, name, builtin in _ROLES],
    )
    op.bulk_insert(
        role_permission,
        [
            {"role_code": role_code, "permission_code": permission_code}
            for role_code, permission_code in _ROLE_PERMISSIONS
        ],
    )


def _sync_schema_rev(rev: str) -> None:
    op.execute(
        sa.text(
            "UPDATE sys_install_state SET schema_rev = :rev, last_good_rev = :rev WHERE id = 1"
        ).bindparams(rev=rev)
    )


def upgrade() -> None:
    op.create_table(
        "account_user",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("username", sa.String(length=64), nullable=False),
        sa.Column("password_hash", sa.String(length=255), nullable=False),
        sa.Column("display_name", sa.String(length=64), nullable=True),
        sa.Column("status", sa.String(length=16), server_default="active", nullable=False),
        sa.Column("token_version", sa.Integer(), server_default="1", nullable=False),
        sa.Column("last_login_at", sa.DateTime(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_account_user")),
        sa.UniqueConstraint("username", name=op.f("uq_account_user_username")),
    )
    op.create_table(
        "account_session",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("sid", sa.String(length=64), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("refresh_jti", sa.String(length=64), nullable=True),
        sa.Column("refresh_expires_at", sa.DateTime(), nullable=False),
        sa.Column("revoked_at", sa.DateTime(), nullable=True),
        sa.Column("revoked_by", sa.String(length=32), nullable=True),
        sa.Column("revoke_reason", sa.String(length=32), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("last_used_at", sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_account_session")),
        sa.UniqueConstraint("sid", name=op.f("uq_account_session_sid")),
    )
    op.create_index(
        op.f("ix_account_session_user_id"), "account_session", ["user_id"], unique=False
    )
    op.create_table(
        "sys_role",
        sa.Column("code", sa.String(length=64), nullable=False),
        sa.Column("name", sa.String(length=64), nullable=False),
        sa.Column("is_builtin", sa.Boolean(), server_default=sa.false(), nullable=False),
        sa.PrimaryKeyConstraint("code", name=op.f("pk_sys_role")),
    )
    op.create_table(
        "sys_permission",
        sa.Column("code", sa.String(length=64), nullable=False),
        sa.Column("name", sa.String(length=64), nullable=False),
        sa.Column("risk_level", sa.String(length=16), server_default="low", nullable=False),
        sa.PrimaryKeyConstraint("code", name=op.f("pk_sys_permission")),
    )
    op.create_table(
        "sys_user_role",
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("role_code", sa.String(length=64), nullable=False),
        sa.PrimaryKeyConstraint("user_id", "role_code", name=op.f("pk_sys_user_role")),
    )
    op.create_index(
        op.f("ix_sys_user_role_role_code"), "sys_user_role", ["role_code"], unique=False
    )
    op.create_table(
        "sys_role_permission",
        sa.Column("role_code", sa.String(length=64), nullable=False),
        sa.Column("permission_code", sa.String(length=64), nullable=False),
        sa.PrimaryKeyConstraint(
            "role_code", "permission_code", name=op.f("pk_sys_role_permission")
        ),
    )
    op.create_index(
        op.f("ix_sys_role_permission_permission_code"),
        "sys_role_permission",
        ["permission_code"],
        unique=False,
    )
    op.create_table(
        "sys_log_security",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("event", sa.String(length=64), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=True),
        sa.Column("detail", sa.String(length=255), server_default="", nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_sys_log_security")),
    )
    op.create_index(op.f("ix_sys_log_security_event"), "sys_log_security", ["event"], unique=False)
    op.create_index(
        op.f("ix_sys_log_security_user_id"), "sys_log_security", ["user_id"], unique=False
    )

    _seed()
    _sync_schema_rev(revision)


def downgrade() -> None:
    op.drop_index(op.f("ix_sys_log_security_user_id"), table_name="sys_log_security")
    op.drop_index(op.f("ix_sys_log_security_event"), table_name="sys_log_security")
    op.drop_table("sys_log_security")
    op.drop_index(op.f("ix_sys_role_permission_permission_code"), table_name="sys_role_permission")
    op.drop_table("sys_role_permission")
    op.drop_index(op.f("ix_sys_user_role_role_code"), table_name="sys_user_role")
    op.drop_table("sys_user_role")
    op.drop_table("sys_permission")
    op.drop_table("sys_role")
    op.drop_index(op.f("ix_account_session_user_id"), table_name="account_session")
    op.drop_table("account_session")
    op.drop_table("account_user")
    _sync_schema_rev(str(down_revision))
