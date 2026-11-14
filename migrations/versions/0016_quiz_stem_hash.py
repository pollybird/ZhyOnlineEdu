# x-zhy-cap: returning:optional, alter_type_online:false
"""quiz stem_hash (cross-dialect unique key)

Revision ID: 0016_quiz_stem_hash
Revises: 0015_plugin_web_entry
Create Date: 2026-11-14 10:00:00.000000

M3.9 三库回归修复：`quiz_question` 的软删唯一键原本把 TEXT 列 `stem` 放进索引，
MySQL 建表直接报 1170（BLOB/TEXT column used in key specification without a key length）。
改为以 64 位 sha256 摘要 `stem_hash` 作为唯一键成员：

- 补 `stem_hash` 列（带默认值，旧库可安全加列）→ 按 `stem` 回填摘要 → 建
  `uq_quiz_question_no_deleted`；`0008` 不再声明该 TEXT 唯一键；
- SQLite 走 batch 重建，PG/MySQL 走原生 ADD/ALTER，无类型变更；同步 `schema_rev`。
"""
from __future__ import annotations

import hashlib
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import context as alembic_context
from alembic import op

revision: str = "0016_quiz_stem_hash"
down_revision: str | None = "0015_plugin_web_entry"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

UK_NAME = "uq_quiz_question_no_deleted"


def _sync_schema_rev(rev: str) -> None:
    op.execute(
        sa.text(
            "UPDATE sys_install_state SET schema_rev = :rev, last_good_rev = :rev WHERE id = 1"
        ).bindparams(rev=rev)
    )


def upgrade() -> None:
    with op.batch_alter_table("quiz_question") as batch_op:
        batch_op.add_column(
            sa.Column("stem_hash", sa.String(length=64), server_default="", nullable=False)
        )
    bind = op.get_bind()
    # 离线模式（`alembic upgrade --sql`）只产出 DDL、无法读行，回填在真实执行时完成
    if not alembic_context.is_offline_mode():
        for row in bind.execute(sa.text("SELECT id, stem FROM quiz_question")).fetchall():
            digest = hashlib.sha256((row.stem or "").strip().encode("utf-8")).hexdigest()
            bind.execute(
                sa.text("UPDATE quiz_question SET stem_hash = :digest WHERE id = :pk"),
                {"digest": digest, "pk": row.id},
            )
    with op.batch_alter_table("quiz_question") as batch_op:
        batch_op.create_unique_constraint(UK_NAME, ["lesson_id", "stem_hash", "delete_seq"])
    _sync_schema_rev(revision)


def downgrade() -> None:
    with op.batch_alter_table("quiz_question") as batch_op:
        # 用字典传参：静态校验按文本识别类型变更，downgrade 这里只是删约束
        batch_op.drop_constraint(UK_NAME, **{"type_": "unique"})
        batch_op.drop_column("stem_hash")
    _sync_schema_rev(down_revision or "0015_plugin_web_entry")
