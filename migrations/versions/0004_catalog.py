# x-zhy-cap: returning:optional, alter_type_online:false
"""create catalog (subject / course / chapter / lesson) and minimal media asset

Revision ID: 0004_catalog
Revises: 0003_auth_rbac
Create Date: 2026-10-29 00:30:00.000000

M2.1：学科树 + 课程三层 + 上架就绪校验所需的最小 `media_asset`。
- 软删表带 `delete_seq`，复合唯一键 `UNIQUE(uq_cols, delete_seq)`（D19a/F6）；
- 课程 `mode` + `access_type` 联合索引供模式切换预检（§9.6）；
- 纯新增表，无类型变更，国产库可直接执行。
"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0004_catalog"
down_revision: str | None = "0003_auth_rbac"
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
        "edu_subject",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("parent_id", sa.Integer(), nullable=True),
        sa.Column("name", sa.String(length=64), nullable=False),
        sa.Column("sort_order", sa.Integer(), server_default="1024", nullable=False),
        sa.Column("is_enabled", sa.Boolean(), server_default=sa.true(), nullable=False),
        sa.Column("version", sa.Integer(), server_default="1", nullable=False),
        sa.Column("deleted_at", sa.DateTime(), nullable=True),
        sa.Column("delete_seq", sa.BigInteger(), server_default="0", nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_edu_subject")),
        sa.UniqueConstraint("parent_id", "name", "delete_seq", name="uq_edu_subject_no_deleted"),
    )
    op.create_index(op.f("ix_edu_subject_parent_id"), "edu_subject", ["parent_id"], unique=False)
    op.create_index(op.f("ix_edu_subject_delete_seq"), "edu_subject", ["delete_seq"], unique=False)

    op.create_table(
        "edu_course",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("subject_id", sa.Integer(), nullable=False),
        sa.Column("title", sa.String(length=128), nullable=False),
        sa.Column("summary", sa.Text(), nullable=True),
        sa.Column("cover_url", sa.String(length=255), nullable=True),
        sa.Column("mode", sa.String(length=16), server_default="both", nullable=False),
        sa.Column("access_type", sa.String(length=16), server_default="public", nullable=False),
        sa.Column("status", sa.String(length=16), server_default="draft", nullable=False),
        sa.Column("teacher_id", sa.Integer(), nullable=True),
        sa.Column("sort_order", sa.Integer(), server_default="1024", nullable=False),
        sa.Column("published_at", sa.DateTime(), nullable=True),
        sa.Column("version", sa.Integer(), server_default="1", nullable=False),
        sa.Column("deleted_at", sa.DateTime(), nullable=True),
        sa.Column("delete_seq", sa.BigInteger(), server_default="0", nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_edu_course")),
        sa.UniqueConstraint("subject_id", "title", "delete_seq", name="uq_edu_course_no_deleted"),
    )
    op.create_index(op.f("ix_edu_course_subject_id"), "edu_course", ["subject_id"], unique=False)
    op.create_index(op.f("ix_edu_course_status"), "edu_course", ["status"], unique=False)
    op.create_index(op.f("ix_edu_course_delete_seq"), "edu_course", ["delete_seq"], unique=False)
    op.create_index(
        "ix_edu_course_mode_access", "edu_course", ["mode", "access_type"], unique=False
    )

    op.create_table(
        "edu_course_chapter",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("course_id", sa.Integer(), nullable=False),
        sa.Column("title", sa.String(length=128), nullable=False),
        sa.Column("sort_order", sa.Integer(), server_default="1024", nullable=False),
        sa.Column("version", sa.Integer(), server_default="1", nullable=False),
        sa.Column("deleted_at", sa.DateTime(), nullable=True),
        sa.Column("delete_seq", sa.BigInteger(), server_default="0", nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_edu_course_chapter")),
        sa.UniqueConstraint(
            "course_id", "title", "delete_seq", name="uq_edu_course_chapter_no_deleted"
        ),
    )
    op.create_index(
        op.f("ix_edu_course_chapter_course_id"), "edu_course_chapter", ["course_id"], unique=False
    )
    op.create_index(
        op.f("ix_edu_course_chapter_delete_seq"),
        "edu_course_chapter",
        ["delete_seq"],
        unique=False,
    )

    op.create_table(
        "edu_course_lesson",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("chapter_id", sa.Integer(), nullable=False),
        sa.Column("course_id", sa.Integer(), nullable=False),
        sa.Column("title", sa.String(length=128), nullable=False),
        sa.Column("kind", sa.String(length=16), server_default="video", nullable=False),
        sa.Column("duration_sec", sa.Integer(), server_default="0", nullable=False),
        sa.Column("is_free", sa.Boolean(), server_default=sa.false(), nullable=False),
        sa.Column("sort_order", sa.Integer(), server_default="1024", nullable=False),
        sa.Column("version", sa.Integer(), server_default="1", nullable=False),
        sa.Column("deleted_at", sa.DateTime(), nullable=True),
        sa.Column("delete_seq", sa.BigInteger(), server_default="0", nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_edu_course_lesson")),
        sa.UniqueConstraint(
            "chapter_id", "title", "delete_seq", name="uq_edu_course_lesson_no_deleted"
        ),
    )
    op.create_index(
        op.f("ix_edu_course_lesson_chapter_id"),
        "edu_course_lesson",
        ["chapter_id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_edu_course_lesson_course_id"), "edu_course_lesson", ["course_id"], unique=False
    )
    op.create_index(
        op.f("ix_edu_course_lesson_delete_seq"),
        "edu_course_lesson",
        ["delete_seq"],
        unique=False,
    )

    op.create_table(
        "media_asset",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("lesson_id", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(length=16), server_default="pending", nullable=False),
        sa.Column("playlist_version", sa.Integer(), server_default="1", nullable=False),
        sa.Column("duration_sec", sa.Integer(), server_default="0", nullable=False),
        sa.Column("error_code", sa.String(length=32), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_media_asset")),
        sa.UniqueConstraint("lesson_id", name=op.f("uq_media_asset_lesson_id")),
    )
    op.create_index(op.f("ix_media_asset_status"), "media_asset", ["status"], unique=False)

    _sync_schema_rev(revision)


def downgrade() -> None:
    op.drop_index(op.f("ix_media_asset_status"), table_name="media_asset")
    op.drop_table("media_asset")
    op.drop_index(op.f("ix_edu_course_lesson_delete_seq"), table_name="edu_course_lesson")
    op.drop_index(op.f("ix_edu_course_lesson_course_id"), table_name="edu_course_lesson")
    op.drop_index(op.f("ix_edu_course_lesson_chapter_id"), table_name="edu_course_lesson")
    op.drop_table("edu_course_lesson")
    op.drop_index(op.f("ix_edu_course_chapter_delete_seq"), table_name="edu_course_chapter")
    op.drop_index(op.f("ix_edu_course_chapter_course_id"), table_name="edu_course_chapter")
    op.drop_table("edu_course_chapter")
    op.drop_index("ix_edu_course_mode_access", table_name="edu_course")
    op.drop_index(op.f("ix_edu_course_delete_seq"), table_name="edu_course")
    op.drop_index(op.f("ix_edu_course_status"), table_name="edu_course")
    op.drop_index(op.f("ix_edu_course_subject_id"), table_name="edu_course")
    op.drop_table("edu_course")
    op.drop_index(op.f("ix_edu_subject_delete_seq"), table_name="edu_subject")
    op.drop_index(op.f("ix_edu_subject_parent_id"), table_name="edu_subject")
    op.drop_table("edu_subject")
    _sync_schema_rev(str(down_revision))
