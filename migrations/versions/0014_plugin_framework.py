# x-zhy-cap: returning:optional, alter_type_online:false
"""create plugin registry (plugin framework)

Revision ID: 0014_plugin_framework
Revises: 0013_lesson_duration
Create Date: 2026-11-01 10:00:00.000000

M3.1~3.3：插件框架注册表。
- 一行一插件；`state` 取 disabled / enabled / failed；
- `schema_rev` 为插件**独立迁移链**的当前 revision（与内核 alembic_version 解耦）；
- JSON 结构统一落 Text（避免方言 JSON 差异），`capabilities_v2` 仅保存全 false 快照；
- 纯新增表，无类型变更，国产库可直接执行。
"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0014_plugin_framework"
down_revision: str | None = "0013_lesson_duration"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _sync_schema_rev(rev: str) -> None:
    op.execute(
        sa.text(
            "UPDATE sys_install_state SET schema_rev = :rev, last_good_rev = :rev WHERE id = 1"
        ).bindparams(rev=rev)
    )


def upgrade() -> None:
    op.create_table(
        "plugin_registry",
        sa.Column("code", sa.String(length=64), nullable=False),
        sa.Column("name", sa.String(length=64), nullable=False),
        sa.Column("plugin_version", sa.String(length=16), nullable=False),
        sa.Column("state", sa.String(length=16), server_default="disabled", nullable=False),
        sa.Column("core_api_min", sa.String(length=16), nullable=False),
        sa.Column("table_prefix", sa.String(length=64), nullable=False),
        sa.Column("entry_server", sa.String(length=255), nullable=False),
        sa.Column("schema_rev", sa.String(length=64), nullable=True),
        sa.Column("manifest_hash", sa.String(length=64), server_default="", nullable=False),
        sa.Column("supported_modes", sa.Text(), server_default="[]", nullable=False),
        sa.Column("capabilities", sa.Text(), server_default="[]", nullable=False),
        sa.Column("capabilities_v2", sa.Text(), server_default="{}", nullable=False),
        sa.Column("routes", sa.Text(), server_default="[]", nullable=False),
        sa.Column("menu", sa.Text(), server_default="[]", nullable=False),
        sa.Column("error_message", sa.String(length=255), nullable=True),
        sa.Column("version", sa.Integer(), server_default="1", nullable=False),
        sa.Column("installed_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint("code", name=op.f("pk_plugin_registry")),
    )
    _sync_schema_rev(revision)


def downgrade() -> None:
    op.drop_table("plugin_registry")
    _sync_schema_rev(down_revision or "0013_lesson_duration")
