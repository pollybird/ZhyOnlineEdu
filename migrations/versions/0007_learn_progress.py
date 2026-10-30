# x-zhy-cap: returning:optional, alter_type_online:false
"""learn progress log / projection / gate (M2.5)

Revision ID: 0007_learn_progress
Revises: 0006_transcode
Create Date: 2026-10-29 03:00:00.000000

M2.5 进度网关（F2）：
- `learn_progress_log`：追加 only 的上报流水（事实源），`gw_seq` 全局唯一；
- `learn_progress`：单课投影，只允许 flush 单写者以 GREATEST 写入；
- `learn_progress_gate`：单写者租约 + `last_gw_seq` 水位，种入 `default` 分片；
- 纯新增表，无类型变更；迁移后同步 `sys_install_state.schema_rev`（D37）。
"""
from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime

import sqlalchemy as sa
from alembic import op

revision: str = "0007_learn_progress"
down_revision: str | None = "0006_transcode"
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
        "learn_progress_log",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("lesson_id", sa.Integer(), nullable=False),
        sa.Column("course_id", sa.Integer(), nullable=False),
        sa.Column("seq_no", sa.BigInteger(), nullable=False),
        sa.Column("gw_seq", sa.BigInteger(), nullable=False),
        sa.Column("position", sa.Integer(), nullable=False),
        sa.Column("played_seconds", sa.Integer(), nullable=False),
        sa.Column("speed_bps", sa.Integer(), server_default="100", nullable=False),
        sa.Column("speed_time", sa.Integer(), server_default="0", nullable=False),
        sa.Column("position_bucket", sa.Integer(), server_default="0", nullable=False),
        sa.Column("source", sa.String(length=16), server_default="play", nullable=False),
        sa.Column("write_mode", sa.String(length=16), server_default="buffered", nullable=False),
        sa.Column("payload_hash", sa.String(length=64), nullable=False),
        sa.Column("client_ts_ms", sa.BigInteger(), server_default="0", nullable=False),
        sa.Column("received_at", sa.DateTime(), nullable=False),
        sa.Column("flushed_at", sa.DateTime(), nullable=True),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_learn_progress_log")),
        sa.UniqueConstraint("user_id", "lesson_id", "seq_no", name="uq_progress_log_seq"),
    )
    op.create_index(
        op.f("ix_learn_progress_log_user_id"), "learn_progress_log", ["user_id"], unique=False
    )
    op.create_index(
        op.f("ix_learn_progress_log_course_id"), "learn_progress_log", ["course_id"], unique=False
    )
    op.create_index(
        op.f("ix_learn_progress_log_gw_seq"), "learn_progress_log", ["gw_seq"], unique=True
    )
    op.create_index(
        op.f("ix_learn_progress_log_received_at"),
        "learn_progress_log",
        ["received_at"],
        unique=False,
    )
    op.create_index(
        op.f("ix_learn_progress_log_flushed_at"),
        "learn_progress_log",
        ["flushed_at"],
        unique=False,
    )
    op.create_index(
        "ix_progress_log_user_lesson", "learn_progress_log", ["user_id", "lesson_id"], unique=False
    )
    op.create_index(
        "ix_progress_log_user_bucket",
        "learn_progress_log",
        ["user_id", "lesson_id", "position_bucket"],
        unique=False,
    )

    op.create_table(
        "learn_progress",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("lesson_id", sa.Integer(), nullable=False),
        sa.Column("course_id", sa.Integer(), nullable=False),
        sa.Column("watch_seconds", sa.Integer(), server_default="0", nullable=False),
        sa.Column("duration_sec", sa.Integer(), server_default="0", nullable=False),
        sa.Column("percent", sa.Numeric(precision=5, scale=2), server_default="0", nullable=False),
        sa.Column("last_position", sa.Integer(), server_default="0", nullable=False),
        sa.Column("completed", sa.Boolean(), server_default=sa.false(), nullable=False),
        sa.Column("completed_at", sa.DateTime(), nullable=True),
        sa.Column("first_watch_at", sa.DateTime(), nullable=False),
        sa.Column("last_watch_at", sa.DateTime(), nullable=False),
        sa.Column("speed_time", sa.Integer(), server_default="0", nullable=False),
        sa.Column("source", sa.String(length=16), server_default="live", nullable=False),
        sa.Column("version", sa.Integer(), server_default="1", nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_learn_progress")),
        sa.UniqueConstraint("user_id", "lesson_id", name="uq_learn_progress_user_lesson"),
    )
    op.create_index(
        "ix_learn_progress_user_course", "learn_progress", ["user_id", "course_id"], unique=False
    )

    op.create_table(
        "learn_progress_gate",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("shard", sa.String(length=16), nullable=False),
        sa.Column("writer_id", sa.String(length=96), nullable=False),
        sa.Column("leased_until", sa.DateTime(), nullable=False),
        sa.Column("last_gw_seq", sa.BigInteger(), server_default="0", nullable=False),
        sa.Column("last_flush_at", sa.DateTime(), nullable=True),
        sa.Column("recovery_blob", sa.Text(), nullable=True),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_learn_progress_gate")),
        sa.UniqueConstraint("shard", name=op.f("uq_learn_progress_gate_shard")),
    )
    op.create_index(
        op.f("ix_learn_progress_gate_leased_until"),
        "learn_progress_gate",
        ["leased_until"],
        unique=False,
    )

    gate = sa.table(
        "learn_progress_gate",
        sa.column("shard", sa.String),
        sa.column("writer_id", sa.String),
        sa.column("leased_until", sa.DateTime),
        sa.column("last_gw_seq", sa.BigInteger),
        sa.column("last_flush_at", sa.DateTime),
        sa.column("recovery_blob", sa.Text),
        sa.column("updated_at", sa.DateTime),
    )
    now = datetime.now(UTC).replace(tzinfo=None)
    op.bulk_insert(
        gate,
        [
            {
                "shard": "default",
                "writer_id": "",
                "leased_until": now,
                "last_gw_seq": 0,
                "last_flush_at": None,
                "recovery_blob": None,
                "updated_at": now,
            }
        ],
    )

    op.create_table(
        "media_keysession",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("ksession_id", sa.String(length=48), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("lesson_id", sa.Integer(), nullable=False),
        sa.Column("course_id", sa.Integer(), nullable=False),
        sa.Column("kid", sa.String(length=32), nullable=False),
        sa.Column("profiles", sa.Text(), nullable=False),
        sa.Column("preview", sa.Boolean(), server_default=sa.false(), nullable=False),
        sa.Column("ip_family", sa.String(length=24), server_default="", nullable=False),
        sa.Column("access_hash", sa.String(length=32), server_default="", nullable=False),
        sa.Column("nonce", sa.String(length=32), server_default="", nullable=False),
        sa.Column("nonce_used_at", sa.DateTime(), nullable=True),
        sa.Column("nonce_expires_at", sa.DateTime(), nullable=True),
        sa.Column("issued_at", sa.DateTime(), nullable=False),
        sa.Column("expires_at", sa.DateTime(), nullable=False),
        sa.Column("superseded_at", sa.DateTime(), nullable=True),
        sa.Column("revoked_at", sa.DateTime(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_media_keysession")),
        sa.UniqueConstraint("ksession_id", name=op.f("uq_media_keysession_ksession_id")),
    )
    op.create_index(
        op.f("ix_media_keysession_user_id"), "media_keysession", ["user_id"], unique=False
    )
    op.create_index(
        op.f("ix_media_keysession_lesson_id"), "media_keysession", ["lesson_id"], unique=False
    )
    op.create_index(
        op.f("ix_media_keysession_expires_at"), "media_keysession", ["expires_at"], unique=False
    )

    _sync_schema_rev(revision)


def downgrade() -> None:
    op.drop_table("media_keysession")
    op.drop_table("learn_progress_gate")
    op.drop_table("learn_progress")
    op.drop_table("learn_progress_log")
    _sync_schema_rev("0006_transcode")
