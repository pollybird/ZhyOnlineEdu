# x-zhy-cap: returning:optional, alter_type_online:false
"""lesson duration derived from uploaded video (M2.14b)

Revision ID: 0013_lesson_duration
Revises: 0012_learning_profile
Create Date: 2026-10-31 11:00:00.000000

课时时长不再由人工填写，改由上传视频的探测结果派生：
- 新数据在「上传完成」写入 `edu_course_lesson.duration_sec`，转码发布时再次校正；
- 迁移时按 `media_asset.duration_sec` 回填一次存量数据（只处理未删除、有时长的课时），
  避免历史人工值让学习完成率/剩余时长失真；
- 不新增表/列，迁移后同步 `sys_install_state.schema_rev`（D37）。
"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0013_lesson_duration"
down_revision: str | None = "0012_learning_profile"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_BACKFILL = """
UPDATE edu_course_lesson
SET duration_sec = (
    SELECT a.duration_sec
    FROM media_asset a
    WHERE a.lesson_id = edu_course_lesson.id AND a.duration_sec > 0
)
WHERE deleted_at IS NULL
  AND delete_seq = 0
  AND EXISTS (
    SELECT 1
    FROM media_asset a
    WHERE a.lesson_id = edu_course_lesson.id AND a.duration_sec > 0
  )
"""


def _sync_schema_rev(rev: str) -> None:
    op.execute(
        sa.text(
            "UPDATE sys_install_state SET schema_rev = :rev, last_good_rev = :rev WHERE id = 1"
        ).bindparams(rev=rev)
    )


def upgrade() -> None:
    op.execute(sa.text(_BACKFILL))
    _sync_schema_rev(revision)


def downgrade() -> None:
    # 时长是派生值，回滚不恢复人工输入；仅回退 schema_rev 标记。
    _sync_schema_rev(down_revision or "0012_learning_profile")
