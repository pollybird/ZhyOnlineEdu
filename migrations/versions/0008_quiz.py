# x-zhy-cap: returning:optional, alter_type_online:false
"""quiz questions / attempts (M2.6)

Revision ID: 0008_quiz
Revises: 0007_learn_progress
Create Date: 2026-10-30 02:00:00.000000

M2.6 题库判分（F5）：
- `quiz_question`：题目挂课时，`options`/`answer` 为 JSON 文本，仅服务端持有答案；
- `quiz_attempt`：一次作答冻结 `question_snapshot`（可下发）与 `answer_key`（不可下发），
  `attempt_uid` 唯一索引同时充当 `Idempotency-Key`，交卷靠条件更新保证重试不双计；
- 纯新增表，无类型变更；迁移后同步 `sys_install_state.schema_rev`（D37）。
"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0008_quiz"
down_revision: str | None = "0007_learn_progress"
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
        "quiz_question",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("course_id", sa.Integer(), nullable=False),
        sa.Column("lesson_id", sa.Integer(), nullable=False),
        sa.Column("kind", sa.String(length=16), server_default="single", nullable=False),
        sa.Column("stem", sa.Text(), nullable=False),
        sa.Column("options", sa.Text(), nullable=False),
        sa.Column("answer", sa.Text(), nullable=False),
        sa.Column("explanation", sa.Text(), nullable=True),
        sa.Column("score", sa.Integer(), server_default="5", nullable=False),
        sa.Column("sort_order", sa.Integer(), nullable=False),
        sa.Column("is_enabled", sa.Boolean(), server_default=sa.true(), nullable=False),
        sa.Column("version", sa.Integer(), server_default="1", nullable=False),
        sa.Column("deleted_at", sa.DateTime(), nullable=True),
        sa.Column("delete_seq", sa.BigInteger(), server_default="0", nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_quiz_question")),
        # D19a：软删复合唯一键（Base 注入同名约束）
        sa.UniqueConstraint("lesson_id", "stem", "delete_seq", name="uq_quiz_question_no_deleted"),
    )
    op.create_index("ix_quiz_question_lesson", "quiz_question", ["lesson_id"], unique=False)
    op.create_index("ix_quiz_question_course", "quiz_question", ["course_id"], unique=False)
    op.create_index(
        op.f("ix_quiz_question_delete_seq"), "quiz_question", ["delete_seq"], unique=False
    )

    op.create_table(
        "quiz_attempt",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("attempt_uid", sa.String(length=48), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("lesson_id", sa.Integer(), nullable=False),
        sa.Column("course_id", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(length=16), server_default="in_progress", nullable=False),
        sa.Column("question_count", sa.Integer(), server_default="0", nullable=False),
        sa.Column("score_total", sa.Integer(), server_default="0", nullable=False),
        sa.Column("score_earned", sa.Integer(), server_default="0", nullable=False),
        sa.Column("percent", sa.Numeric(precision=5, scale=2), server_default="0", nullable=False),
        sa.Column("passed", sa.Boolean(), server_default=sa.false(), nullable=False),
        sa.Column("question_snapshot", sa.Text(), nullable=False),
        sa.Column("answer_key", sa.Text(), nullable=False),
        sa.Column("answers", sa.Text(), nullable=True),
        sa.Column("result_json", sa.Text(), nullable=True),
        sa.Column("request_hash", sa.String(length=64), server_default="", nullable=False),
        sa.Column("started_at", sa.DateTime(), nullable=False),
        sa.Column("submitted_at", sa.DateTime(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.Column("version", sa.Integer(), server_default="1", nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_quiz_attempt")),
    )
    op.create_index("uq_quiz_attempt_uid", "quiz_attempt", ["attempt_uid"], unique=True)
    op.create_index("ix_quiz_attempt_user", "quiz_attempt", ["user_id"], unique=False)
    op.create_index("ix_quiz_attempt_lesson", "quiz_attempt", ["lesson_id"], unique=False)
    op.create_index("ix_quiz_attempt_course", "quiz_attempt", ["course_id"], unique=False)

    _sync_schema_rev(revision)


def downgrade() -> None:
    op.drop_table("quiz_attempt")
    op.drop_table("quiz_question")
    _sync_schema_rev("0007_learn_progress")
