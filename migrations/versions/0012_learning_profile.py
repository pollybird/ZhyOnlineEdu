# x-zhy-cap: returning:optional, alter_type_online:false
"""course-level learning profile projection (M2.13)

Revision ID: 0012_learning_profile
Revises: 0011_commerce
Create Date: 2026-10-30 09:00:00.000000

M2.13 学习档案（§10.7）：
- 新增 `learn_course_progress`：课程维度投影，由 `progress-flush` 单写者异步维护；
- `watch_seconds` / `speed_time` 只允许 GREATEST 写入；`percent` = 完成课时 / 总课时；
- 迁移时按 `learn_progress`（单课投影）回算一次，存量用户升级后立即可见档案；
- `source=preview` 的流水不进 `learn_progress`，因此本表天然不含预览数据。

纯新增表，无类型变更；迁移后同步 `sys_install_state.schema_rev`（D37）。
"""
from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime
from decimal import Decimal

import sqlalchemy as sa
from alembic import op

revision: str = "0012_learning_profile"
down_revision: str | None = "0011_commerce"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_PERCENT_QUANT = Decimal("0.01")


def _sync_schema_rev(rev: str) -> None:
    op.execute(
        sa.text(
            "UPDATE sys_install_state SET schema_rev = :rev, last_good_rev = :rev WHERE id = 1"
        ).bindparams(rev=rev)
    )


def _backfill() -> None:
    """从单课投影回算课程档案；无存量进度时为空操作。

    离线（COMPILE_ONLY）模式没有数据库连接，且回填依赖现有行，故离线脚本不含此步。
    """
    ctx = op.get_context()
    if ctx.as_sql:  # 离线生成 SQL：不回填（依赖现有数据）
        return
    bind = op.get_bind()
    lessons = {
        int(row[0]): (int(row[1]), int(row[2]))
        for row in bind.execute(
            sa.text(
                "SELECT course_id, COUNT(*), COALESCE(SUM(duration_sec), 0) "
                "FROM edu_course_lesson WHERE deleted_at IS NULL GROUP BY course_id"
            )
        )
    }
    latest: dict[tuple[int, int], tuple[int, int]] = {}
    for row in bind.execute(
        sa.text(
            "SELECT p.user_id, p.course_id, p.lesson_id, p.last_position "
            "FROM learn_progress p "
            "JOIN edu_course_lesson l ON l.id = p.lesson_id AND l.deleted_at IS NULL "
            "ORDER BY p.user_id, p.course_id, p.last_watch_at DESC, p.id DESC"
        )
    ):
        key = (int(row[0]), int(row[1]))
        latest.setdefault(key, (int(row[2]), int(row[3])))

    now = datetime.now(UTC).replace(tzinfo=None)
    rows: list[dict[str, object]] = []
    for row in bind.execute(
        sa.text(
            "SELECT p.user_id, p.course_id, SUM(p.watch_seconds), SUM(p.speed_time), "
            "SUM(CASE WHEN p.completed THEN 1 ELSE 0 END), "
            "MIN(p.first_watch_at), MAX(p.last_watch_at) "
            "FROM learn_progress p "
            "JOIN edu_course_lesson l ON l.id = p.lesson_id AND l.deleted_at IS NULL "
            "GROUP BY p.user_id, p.course_id"
        )
    ):
        user_id, course_id = int(row[0]), int(row[1])
        total, total_duration = lessons.get(course_id, (0, 0))
        completed_lessons = min(int(row[4] or 0), total)
        percent = (
            (Decimal(completed_lessons) * 100 / Decimal(total)).quantize(_PERCENT_QUANT)
            if total > 0
            else Decimal("0.00")
        )
        resume = latest.get((user_id, course_id), (None, 0))
        rows.append(
            {
                "user_id": user_id,
                "course_id": course_id,
                "watch_seconds": int(row[2] or 0),
                "speed_time": int(row[3] or 0),
                "total_lessons": total,
                "completed_lessons": completed_lessons,
                "total_duration_sec": total_duration,
                "percent": percent,
                "completed": total > 0 and completed_lessons >= total,
                "resume_lesson_id": resume[0],
                "resume_position": int(resume[1] or 0),
                "first_watch_at": row[5] or now,
                "last_watch_at": row[6] or now,
                "updated_at": row[6] or now,
                "version": 1,
            }
        )
    if not rows:
        return
    table = sa.table(
        "learn_course_progress",
        sa.column("user_id", sa.Integer),
        sa.column("course_id", sa.Integer),
        sa.column("watch_seconds", sa.Integer),
        sa.column("speed_time", sa.Integer),
        sa.column("total_lessons", sa.Integer),
        sa.column("completed_lessons", sa.Integer),
        sa.column("total_duration_sec", sa.Integer),
        sa.column("percent", sa.Numeric(5, 2)),
        sa.column("completed", sa.Boolean),
        sa.column("resume_lesson_id", sa.Integer),
        sa.column("resume_position", sa.Integer),
        sa.column("first_watch_at", sa.DateTime),
        sa.column("last_watch_at", sa.DateTime),
        sa.column("updated_at", sa.DateTime),
        sa.column("version", sa.Integer),
    )
    op.bulk_insert(table, rows)


def upgrade() -> None:
    op.create_table(
        "learn_course_progress",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("course_id", sa.Integer(), nullable=False),
        sa.Column("watch_seconds", sa.Integer(), server_default="0", nullable=False),
        sa.Column("speed_time", sa.Integer(), server_default="0", nullable=False),
        sa.Column("total_lessons", sa.Integer(), server_default="0", nullable=False),
        sa.Column("completed_lessons", sa.Integer(), server_default="0", nullable=False),
        sa.Column("total_duration_sec", sa.Integer(), server_default="0", nullable=False),
        sa.Column("percent", sa.Numeric(precision=5, scale=2), server_default="0", nullable=False),
        sa.Column("completed", sa.Boolean(), server_default=sa.false(), nullable=False),
        sa.Column("resume_lesson_id", sa.Integer(), nullable=True),
        sa.Column("resume_position", sa.Integer(), server_default="0", nullable=False),
        sa.Column("first_watch_at", sa.DateTime(), nullable=False),
        sa.Column("last_watch_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.Column("version", sa.Integer(), server_default="1", nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_learn_course_progress")),
        sa.UniqueConstraint("user_id", "course_id", name="uq_learn_course_progress_user_course"),
    )
    op.create_index(
        "ix_learn_course_progress_user", "learn_course_progress", ["user_id"], unique=False
    )
    op.create_index(
        "ix_learn_course_progress_last_watch",
        "learn_course_progress",
        ["user_id", "last_watch_at"],
        unique=False,
    )

    _backfill()
    _sync_schema_rev(revision)


def downgrade() -> None:
    op.drop_index("ix_learn_course_progress_last_watch", table_name="learn_course_progress")
    op.drop_index("ix_learn_course_progress_user", table_name="learn_course_progress")
    op.drop_table("learn_course_progress")
    _sync_schema_rev("0011_commerce")
