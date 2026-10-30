# x-zhy-cap: returning:optional, alter_type_online:false
"""add install_step to sys_install_state

Revision ID: 0002_install_step
Revises: 0001_system_schema
Create Date: 2026-10-28 00:10:00.000000

M1.5 安装向导状态机落列：`start → db → admin → mode → finish → done`。
仅新增列（无类型变更），既有库由 server_default 回填为 start。
"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0002_install_step"
down_revision: str | None = "0001_system_schema"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "sys_install_state",
        sa.Column("install_step", sa.String(length=16), nullable=False, server_default="start"),
    )
    # 与 manage.py db-migrate 同步：迁移后 schema_rev/last_good_rev 必须指向新 head（D37）
    op.execute(
        sa.text(
            "UPDATE sys_install_state SET schema_rev = :rev, last_good_rev = :rev " "WHERE id = 1"
        ).bindparams(rev=revision)
    )


def downgrade() -> None:
    op.execute(
        sa.text(
            "UPDATE sys_install_state SET schema_rev = :rev, last_good_rev = :rev " "WHERE id = 1"
        ).bindparams(rev=down_revision)
    )
    op.drop_column("sys_install_state", "install_step")
