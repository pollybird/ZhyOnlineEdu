# x-zhy-cap: returning:optional, alter_type_online:false
"""dual-mode state / registration source / preview quarantine (M2.10)

Revision ID: 0010_modes
Revises: 0009_users_classes
Create Date: 2026-10-30 06:00:00.000000

M2.10~2.11 双模式（§9.3 / §9.5 / §9.6）：
- 新增 `sys_mode_state`（单行：active_mode / epoch / previous_mode / 切换批次）；
- `account_user` 补 `registration_mode` 与 `migration_batch_id`（自注册账号
  切换时的处置批次，可回溯、可批量恢复）；
- `learn_progress_log` 补 `quarantined`（切换窗口内 `source=preview` 流水隔离位），
  默认不进统计。

本迁移纯新增列/表，无类型变更；表结构在模式切换时**不再变化**（R5）。
"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0010_modes"
down_revision: str | None = "0009_users_classes"
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
        "sys_mode_state",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("active_mode", sa.String(length=16), server_default="internal", nullable=False),
        sa.Column("epoch", sa.Integer(), server_default="1", nullable=False),
        sa.Column("previous_mode", sa.String(length=16), nullable=True),
        sa.Column("switched_at", sa.DateTime(), nullable=True),
        sa.Column("switched_by", sa.Integer(), nullable=True),
        sa.Column("migration_batch_id", sa.String(length=32), nullable=True),
        sa.Column("version", sa.Integer(), server_default="1", nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_sys_mode_state")),
    )

    with op.batch_alter_table("account_user") as batch_op:
        batch_op.add_column(
            sa.Column(
                "registration_mode",
                sa.String(length=16),
                server_default="admin",
                nullable=False,
            )
        )
        batch_op.add_column(sa.Column("migration_batch_id", sa.String(length=32), nullable=True))

    with op.batch_alter_table("learn_progress_log") as batch_op:
        batch_op.add_column(
            sa.Column("quarantined", sa.Boolean(), server_default=sa.false(), nullable=False)
        )
    op.create_index(
        "ix_learn_progress_log_quarantined",
        "learn_progress_log",
        ["quarantined"],
        unique=False,
    )

    _sync_schema_rev(revision)


def downgrade() -> None:
    op.drop_index("ix_learn_progress_log_quarantined", table_name="learn_progress_log")
    with op.batch_alter_table("learn_progress_log") as batch_op:
        batch_op.drop_column("quarantined")
    with op.batch_alter_table("account_user") as batch_op:
        batch_op.drop_column("migration_batch_id")
        batch_op.drop_column("registration_mode")
    op.drop_table("sys_mode_state")
    _sync_schema_rev("0009_users_classes")
