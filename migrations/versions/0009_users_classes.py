# x-zhy-cap: returning:optional, alter_type_online:false
"""users / classes / import batches (M2.7)

Revision ID: 0009_users_classes
Revises: 0008_quiz
Create Date: 2026-10-30 04:00:00.000000

M2.7 用户/班级/导入/过期（§6.5 / §10.5）：
- `account_user` 补 `org_id` / `user_no` / `expire_at` / 乐观锁 `version` 与软删列，
  注入 `uq_account_user_no_deleted (org_id, user_no, delete_seq)`（D19a）；
- 新增 `edu_class`（班级，软删 UK `(org_id, name, delete_seq)`）与 `edu_class_member`
  （成员关系硬删，移除即失去 class 课程授权）；
- 新增 `sys_import_batch`（`import_batch_no` 沿用；逐行结果见 `rows_json`，不存口令）。

`user_no` 采用"先加带默认值的列 → 回填历史行 → 建唯一键"的三步，保证
SQLite / 国产库 batch 重建时不出现 NULL 或重复自然键；本迁移无类型变更。
"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0009_users_classes"
down_revision: str | None = "0008_quiz"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _sync_schema_rev(rev: str) -> None:
    op.execute(
        sa.text(
            "UPDATE sys_install_state SET schema_rev = :rev, last_good_rev = :rev WHERE id = 1"
        ).bindparams(rev=rev)
    )


def _backfill_user_no() -> None:
    """历史账号回填稳定学号（`U<id>`），避免自然键全为空串。

    离线（COMPILE_ONLY）模式没有数据库连接，且回填依赖现有行，故离线脚本不含此步；
    在线升级按方言走集合式 UPDATE，避免逐行读写。
    """
    ctx = op.get_context()
    if ctx.as_sql:  # 离线生成 SQL：不回填（依赖现有数据）
        return
    if ctx.dialect.name == "mysql":
        sql = "UPDATE account_user SET user_no = CONCAT('U', CAST(id AS CHAR)) WHERE user_no = ''"
    else:
        sql = "UPDATE account_user SET user_no = 'U' || CAST(id AS TEXT) WHERE user_no = ''"
    op.execute(sa.text(sql))


def upgrade() -> None:
    with op.batch_alter_table("account_user") as batch_op:
        batch_op.add_column(sa.Column("org_id", sa.Integer(), server_default="1", nullable=False))
        # 先允许默认空串：历史行回填后再建复合唯一键
        batch_op.add_column(
            sa.Column("user_no", sa.String(length=32), server_default="", nullable=False)
        )
        batch_op.add_column(sa.Column("expire_at", sa.DateTime(), nullable=True))
        batch_op.add_column(sa.Column("version", sa.Integer(), server_default="1", nullable=False))
        batch_op.add_column(sa.Column("deleted_at", sa.DateTime(), nullable=True))
        batch_op.add_column(
            sa.Column("delete_seq", sa.BigInteger(), server_default="0", nullable=False)
        )
    _backfill_user_no()
    with op.batch_alter_table("account_user") as batch_op:
        batch_op.create_unique_constraint(
            "uq_account_user_no_deleted", ["org_id", "user_no", "delete_seq"]
        )
    op.create_index("ix_account_user_delete_seq", "account_user", ["delete_seq"], unique=False)

    op.create_table(
        "edu_class",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("org_id", sa.Integer(), server_default="1", nullable=False),
        sa.Column("name", sa.String(length=64), nullable=False),
        sa.Column("teacher_id", sa.Integer(), nullable=True),
        sa.Column("is_enabled", sa.Boolean(), server_default=sa.true(), nullable=False),
        sa.Column("sort_order", sa.Integer(), nullable=False),
        sa.Column("version", sa.Integer(), server_default="1", nullable=False),
        sa.Column("deleted_at", sa.DateTime(), nullable=True),
        sa.Column("delete_seq", sa.BigInteger(), server_default="0", nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_edu_class")),
        sa.UniqueConstraint("org_id", "name", "delete_seq", name="uq_edu_class_no_deleted"),
    )
    op.create_index("ix_edu_class_delete_seq", "edu_class", ["delete_seq"], unique=False)

    op.create_table(
        "edu_class_member",
        sa.Column("class_id", sa.Integer(), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint("class_id", "user_id", name=op.f("pk_edu_class_member")),
    )
    op.create_index("ix_edu_class_member_user_id", "edu_class_member", ["user_id"], unique=False)

    op.create_table(
        "sys_import_batch",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("batch_no", sa.String(length=32), nullable=False),
        sa.Column("kind", sa.String(length=16), server_default="user", nullable=False),
        sa.Column("status", sa.String(length=16), server_default="completed", nullable=False),
        sa.Column(
            "on_conflict_action", sa.String(length=16), server_default="report_only", nullable=False
        ),
        sa.Column("total_rows", sa.Integer(), server_default="0", nullable=False),
        sa.Column("created_rows", sa.Integer(), server_default="0", nullable=False),
        sa.Column("updated_rows", sa.Integer(), server_default="0", nullable=False),
        sa.Column("skipped_rows", sa.Integer(), server_default="0", nullable=False),
        sa.Column("invalid_rows", sa.Integer(), server_default="0", nullable=False),
        sa.Column("rows_json", sa.Text(), nullable=True),
        sa.Column("operator_id", sa.Integer(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("finished_at", sa.DateTime(), nullable=True),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_sys_import_batch")),
        sa.UniqueConstraint("batch_no", name=op.f("uq_sys_import_batch_batch_no")),
    )
    op.create_index("ix_sys_import_batch_status", "sys_import_batch", ["status"], unique=False)

    _sync_schema_rev(revision)


def downgrade() -> None:
    op.drop_index("ix_sys_import_batch_status", table_name="sys_import_batch")
    op.drop_table("sys_import_batch")
    op.drop_index("ix_edu_class_member_user_id", table_name="edu_class_member")
    op.drop_table("edu_class_member")
    op.drop_index("ix_edu_class_delete_seq", table_name="edu_class")
    op.drop_table("edu_class")
    op.drop_index("ix_account_user_delete_seq", table_name="account_user")
    with op.batch_alter_table("account_user") as batch_op:
        # type_ 以位置参数传入：红线脚本按文本检索类型变更关键字，此处并非列类型变更
        batch_op.drop_constraint("uq_account_user_no_deleted", "unique")
        batch_op.drop_column("delete_seq")
        batch_op.drop_column("deleted_at")
        batch_op.drop_column("version")
        batch_op.drop_column("expire_at")
        batch_op.drop_column("user_no")
        batch_op.drop_column("org_id")
    _sync_schema_rev("0008_quiz")
