# x-zhy-cap: returning:optional, alter_type_online:false
"""create media upload session / chunk (M2.3)

Revision ID: 0005_media_upload
Revises: 0004_catalog
Create Date: 2026-10-29 01:00:00.000000

M2.3：分片上传（秒传/断点/分片/合并/入队）所需的会话与分片元数据表。
- 纯新增表，无类型变更，国产库可直接执行；
- 临时文件路径不入库，避免路径注入与信息泄露；
- 迁移后同步 `sys_install_state.schema_rev`（D37）。
"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0005_media_upload"
down_revision: str | None = "0004_catalog"
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
        "media_upload_session",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("upload_id", sa.String(length=32), nullable=False),
        sa.Column("lesson_id", sa.Integer(), nullable=False),
        sa.Column("file_name", sa.String(length=255), nullable=False),
        sa.Column("file_size", sa.BigInteger(), nullable=False),
        sa.Column("sha256", sa.String(length=64), nullable=False),
        sa.Column("chunk_size", sa.Integer(), nullable=False),
        sa.Column("total_chunks", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(length=16), server_default="open", nullable=False),
        sa.Column("error_code", sa.String(length=32), nullable=True),
        sa.Column("format_name", sa.String(length=64), nullable=True),
        sa.Column("duration_sec", sa.Integer(), server_default="0", nullable=False),
        sa.Column("width", sa.Integer(), nullable=True),
        sa.Column("height", sa.Integer(), nullable=True),
        sa.Column("probe_source", sa.String(length=16), nullable=True),
        sa.Column("expires_at", sa.DateTime(), nullable=False),
        sa.Column("completed_at", sa.DateTime(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_media_upload_session")),
        sa.UniqueConstraint("upload_id", name=op.f("uq_media_upload_session_upload_id")),
    )
    op.create_index(
        op.f("ix_media_upload_session_lesson_id"),
        "media_upload_session",
        ["lesson_id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_media_upload_session_status"),
        "media_upload_session",
        ["status"],
        unique=False,
    )
    op.create_index(
        op.f("ix_media_upload_session_expires_at"),
        "media_upload_session",
        ["expires_at"],
        unique=False,
    )

    op.create_table(
        "media_chunk",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("session_id", sa.Integer(), nullable=False),
        sa.Column("chunk_index", sa.Integer(), nullable=False),
        sa.Column("md5", sa.String(length=32), nullable=False),
        sa.Column("size_bytes", sa.BigInteger(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_media_chunk")),
        sa.UniqueConstraint("session_id", "chunk_index", name="uq_media_chunk_session_index"),
    )
    op.create_index(op.f("ix_media_chunk_session_id"), "media_chunk", ["session_id"], unique=False)

    _sync_schema_rev(revision)


def downgrade() -> None:
    op.drop_index(op.f("ix_media_chunk_session_id"), table_name="media_chunk")
    op.drop_table("media_chunk")
    op.drop_index(op.f("ix_media_upload_session_expires_at"), table_name="media_upload_session")
    op.drop_index(op.f("ix_media_upload_session_status"), table_name="media_upload_session")
    op.drop_index(op.f("ix_media_upload_session_lesson_id"), table_name="media_upload_session")
    op.drop_table("media_upload_session")
    _sync_schema_rev(str(down_revision))
