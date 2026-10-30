# x-zhy-cap: returning:optional, alter_type_online:false
"""initial system schema

Revision ID: 0001_system_schema
Revises:
Create Date: 2026-10-28 00:00:00.000000

M1.3 首个 revision：系统基础表（sys_install_state / sys_config / sys_mode_config /
sys_compat_flags）。全部为 CREATE TABLE，不含类型变更。
"""
from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime

import sqlalchemy as sa
from alembic import op

revision: str = "0001_system_schema"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _now() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None)


def upgrade() -> None:
    op.create_table(
        "sys_install_state",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("is_installed", sa.Boolean(), nullable=False),
        sa.Column("schema_rev", sa.String(length=32), nullable=True),
        sa.Column("last_good_rev", sa.String(length=32), nullable=True),
        sa.Column("installed_at", sa.DateTime(), nullable=True),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_sys_install_state")),
    )

    op.create_table(
        "sys_config",
        sa.Column("config_key", sa.String(length=128), nullable=False),
        sa.Column("value", sa.Text(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint("config_key", name=op.f("pk_sys_config")),
    )

    op.create_table(
        "sys_mode_config",
        sa.Column("mode", sa.String(length=16), nullable=False),
        sa.Column("config_key", sa.String(length=128), nullable=False),
        sa.Column("value", sa.Text(), nullable=False),
        sa.Column("compat_flags", sa.Text(), nullable=False),
        sa.PrimaryKeyConstraint("mode", "config_key", name=op.f("pk_sys_mode_config")),
    )

    op.create_table(
        "sys_compat_flags",
        sa.Column("cap", sa.String(length=64), nullable=False),
        sa.Column("flavor", sa.String(length=32), nullable=False),
        sa.Column("value", sa.Integer(), nullable=False),
        sa.Column("probed_at", sa.String(length=32), nullable=False),
        sa.Column("detail", sa.String(length=255), nullable=False),
        sa.PrimaryKeyConstraint("cap", name=op.f("pk_sys_compat_flags")),
    )

    # 单行安装状态：last_good_rev 供 --rollback-to-last-good 使用（§5.6）
    install_state = sa.table(
        "sys_install_state",
        sa.column("id", sa.Integer()),
        sa.column("is_installed", sa.Boolean()),
        sa.column("schema_rev", sa.String()),
        sa.column("last_good_rev", sa.String()),
        sa.column("installed_at", sa.DateTime()),
        sa.column("updated_at", sa.DateTime()),
        sa.column("version", sa.Integer()),
    )
    op.bulk_insert(
        install_state,
        [
            {
                "id": 1,
                "is_installed": False,
                "schema_rev": revision,
                "last_good_rev": revision,
                "installed_at": None,
                "updated_at": _now(),
                "version": 1,
            }
        ],
    )


def downgrade() -> None:
    op.drop_table("sys_compat_flags")
    op.drop_table("sys_mode_config")
    op.drop_table("sys_config")
    op.drop_table("sys_install_state")
