# x-zhy-cap: returning:optional, alter_type_online:false
"""transcode job + asset sandbox columns (M2.4)

Revision ID: 0006_transcode
Revises: 0005_media_upload
Create Date: 2026-10-29 02:00:00.000000

M2.4 转码沙箱化：
- 新增 `media_transcode_job`（DB-backed 队列，status/run_after 索引）；
- `media_asset` 增 `kid` / `profiles` / `poster_path` / `sandbox_profile`
  （kid 为匿名资产标识，AES key 由主密钥派生，不落库）；
- 同步 `sys_install_state.schema_rev`（D37）。
"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0006_transcode"
down_revision: str | None = "0005_media_upload"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _sync_schema_rev(rev: str) -> None:
    op.execute(
        sa.text(
            "UPDATE sys_install_state SET schema_rev = :rev, last_good_rev = :rev WHERE id = 1"
        ).bindparams(rev=rev)
    )


def upgrade() -> None:
    with op.batch_alter_table("media_asset") as batch:
        batch.add_column(sa.Column("kid", sa.String(length=32), nullable=True))
        batch.add_column(sa.Column("profiles", sa.Text(), nullable=True))
        batch.add_column(sa.Column("poster_path", sa.String(length=255), nullable=True))
        batch.add_column(sa.Column("sandbox_profile", sa.Text(), nullable=True))
        batch.create_unique_constraint("uq_media_asset_kid", ["kid"])

    op.create_table(
        "media_transcode_job",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("asset_id", sa.Integer(), nullable=False),
        sa.Column("lesson_id", sa.Integer(), nullable=False),
        sa.Column("upload_id", sa.String(length=32), nullable=True),
        sa.Column("status", sa.String(length=16), server_default="queued", nullable=False),
        sa.Column("attempt", sa.Integer(), server_default="1", nullable=False),
        sa.Column("profiles", sa.Text(), nullable=False),
        sa.Column("error_code", sa.String(length=32), nullable=True),
        sa.Column("err", sa.String(length=2048), nullable=True),
        sa.Column("sandbox", sa.Text(), nullable=True),
        sa.Column("run_after", sa.DateTime(), nullable=True),
        sa.Column("started_at", sa.DateTime(), nullable=True),
        sa.Column("finished_at", sa.DateTime(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_media_transcode_job")),
    )
    op.create_index(
        op.f("ix_media_transcode_job_asset_id"),
        "media_transcode_job",
        ["asset_id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_media_transcode_job_lesson_id"),
        "media_transcode_job",
        ["lesson_id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_media_transcode_job_status"),
        "media_transcode_job",
        ["status"],
        unique=False,
    )
    op.create_index(
        op.f("ix_media_transcode_job_run_after"),
        "media_transcode_job",
        ["run_after"],
        unique=False,
    )

    _sync_schema_rev(revision)


def downgrade() -> None:
    op.drop_index(op.f("ix_media_transcode_job_run_after"), table_name="media_transcode_job")
    op.drop_index(op.f("ix_media_transcode_job_status"), table_name="media_transcode_job")
    op.drop_index(op.f("ix_media_transcode_job_lesson_id"), table_name="media_transcode_job")
    op.drop_index(op.f("ix_media_transcode_job_asset_id"), table_name="media_transcode_job")
    op.drop_table("media_transcode_job")

    with op.batch_alter_table("media_asset") as batch:
        batch.drop_constraint("uq_media_asset_kid", "unique")
        batch.drop_column("sandbox_profile")
        batch.drop_column("poster_path")
        batch.drop_column("profiles")
        batch.drop_column("kid")

    _sync_schema_rev(str(down_revision))
