"""学习进度三表（§6.9 · F2 / M2.5）。

- `learn_progress_log`：**追加 only** 的上报流水（事实源），带 `gw_seq`（全局单调）
  与 `seq_no`（同 (user,lesson) 单调），缺口 = 丢进度；
- `learn_progress`：单课维度投影，只允许 flush 单写者用 `GREATEST` 语句写入；
  `version` 乐观锁，冲突时重读再算，绝不以旧值覆盖；
- `learn_progress_gate`：单写者租约 + `last_gw_seq` 水位（对账基准）。

红线：HTTP 路径禁止写 `learn_progress`（`ProgressGateway` 只写 log）；
`played_seconds` 按 `position_bucket = position // 30` 去重，避免 30s 心跳被双计。
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

from sqlalchemy import (
    BigInteger,
    Boolean,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
    false,
)
from sqlalchemy.orm import Mapped, mapped_column

from ..base import Base
from ..types import UTCDatetime, VersionedMixin

PROGRESS_SOURCES: tuple[str, ...] = ("live", "recovery", "backfill")
WRITE_MODES: tuple[str, ...] = ("direct", "buffered", "recovered")
PREVIEW_SOURCE = "preview"  # 试看：只落流水，不计入统计（§9.4 #18）
LOG_SOURCES: tuple[str, ...] = ("play", "preview")
DEFAULT_SHARD = "default"


def utcnow() -> datetime:
    return datetime.now(UTC)


def position_bucket(position: int, bucket_sec: int = 30) -> int:
    return max(0, position) // max(1, bucket_sec)


class LearnProgressLog(Base):
    """进度流水（追加 only，永不 UPDATE/DELETE；审计与重建依赖它）。"""

    __tablename__ = "learn_progress_log"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(Integer, nullable=False, index=True)
    lesson_id: Mapped[int] = mapped_column(Integer, nullable=False)
    course_id: Mapped[int] = mapped_column(Integer, nullable=False, index=True)
    # 同 (user, lesson) 的单调序号；单写者分配，用于逐课缺口检测
    seq_no: Mapped[int] = mapped_column(BigInteger, nullable=False)
    # 网关分配的全局令牌：多实例唯一，缺口即丢进度（UK 兜底重复入队）
    gw_seq: Mapped[int] = mapped_column(BigInteger, nullable=False, unique=True, index=True)
    position: Mapped[int] = mapped_column(Integer, nullable=False)
    played_seconds: Mapped[int] = mapped_column(Integer, nullable=False)
    # 倍速以 bps 存储（100 = 1x），避免 Float 跨方言精度差异（D14）
    speed_bps: Mapped[int] = mapped_column(Integer, nullable=False, default=100)
    speed_time: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    position_bucket: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    source: Mapped[str] = mapped_column(String(16), nullable=False, default="play")
    write_mode: Mapped[str] = mapped_column(String(16), nullable=False, default="buffered")
    payload_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    client_ts_ms: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    received_at: Mapped[datetime] = mapped_column(UTCDatetime, nullable=False, index=True)
    flushed_at: Mapped[datetime | None] = mapped_column(UTCDatetime, nullable=True, index=True)
    # 模式切换窗口内的 preview 流水隔离位（§9.6）：默认不进统计，次日由运维确认后清理
    quarantined: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default=false(), index=True
    )

    __table_args__ = (
        UniqueConstraint("user_id", "lesson_id", "seq_no", name="uq_progress_log_seq"),
        Index("ix_progress_log_user_lesson", "user_id", "lesson_id"),
        Index("ix_progress_log_user_bucket", "user_id", "lesson_id", "position_bucket"),
    )


class LearnProgress(VersionedMixin, Base):
    """单课进度投影：`watch_seconds` / `last_position` 只允许 GREATEST 写入。"""

    __tablename__ = "learn_progress"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(Integer, nullable=False)
    lesson_id: Mapped[int] = mapped_column(Integer, nullable=False)
    course_id: Mapped[int] = mapped_column(Integer, nullable=False)
    watch_seconds: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    duration_sec: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    percent: Mapped[Decimal] = mapped_column(Numeric(5, 2), nullable=False, default=Decimal("0"))
    last_position: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    completed: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default=false()
    )
    completed_at: Mapped[datetime | None] = mapped_column(UTCDatetime, nullable=True)
    first_watch_at: Mapped[datetime] = mapped_column(UTCDatetime, nullable=False)
    last_watch_at: Mapped[datetime] = mapped_column(UTCDatetime, nullable=False)
    # 按 1x 折算的有效时长（V2.2 口径）：sum(played / speed)
    speed_time: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    source: Mapped[str] = mapped_column(String(16), nullable=False, default="live")

    __table_args__ = (
        UniqueConstraint("user_id", "lesson_id", name="uq_learn_progress_user_lesson"),
        Index("ix_learn_progress_user_course", "user_id", "course_id"),
    )


class LearnCourseProgress(VersionedMixin, Base):
    """课程维度学习档案投影（§10.7 · M2.13）。

    由 `progress-flush` 单写者异步维护（允许秒级滞后）：
    - `watch_seconds` / `speed_time` 只允许 GREATEST 写入，绝不回退；
    - `percent` = 已完成课时 / 有效课时总数（课程被追加课时后会重算下降，属口径变化）；
    - `source=preview` 的流水在 flush 时被过滤，**永不进入本表**（预览不计入统计）。
    """

    __tablename__ = "learn_course_progress"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(Integer, nullable=False)
    course_id: Mapped[int] = mapped_column(Integer, nullable=False)
    watch_seconds: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    speed_time: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    total_lessons: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    completed_lessons: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    total_duration_sec: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    percent: Mapped[Decimal] = mapped_column(Numeric(5, 2), nullable=False, default=Decimal("0"))
    completed: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default=false()
    )
    resume_lesson_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    resume_position: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    first_watch_at: Mapped[datetime] = mapped_column(UTCDatetime, nullable=False)
    last_watch_at: Mapped[datetime] = mapped_column(UTCDatetime, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(UTCDatetime, nullable=False)

    __table_args__ = (
        UniqueConstraint("user_id", "course_id", name="uq_learn_course_progress_user_course"),
        Index("ix_learn_course_progress_user", "user_id"),
        Index("ix_learn_course_progress_last_watch", "user_id", "last_watch_at"),
    )


class LearnProgressGate(Base):
    """单写者租约与水位（`shard` 默认 default；未来按 hash(user_id)%N 分片不改协议）。"""

    __tablename__ = "learn_progress_gate"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    shard: Mapped[str] = mapped_column(String(16), nullable=False, unique=True)
    writer_id: Mapped[str] = mapped_column(String(96), nullable=False, default="")
    leased_until: Mapped[datetime] = mapped_column(
        UTCDatetime, nullable=False, default=utcnow, index=True
    )
    last_gw_seq: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    last_flush_at: Mapped[datetime | None] = mapped_column(UTCDatetime, nullable=True)
    # 崩溃恢复的重放游标（JSON，避免在 gate 上加列做迁移风暴）
    recovery_blob: Mapped[str | None] = mapped_column(Text, nullable=True)
    updated_at: Mapped[datetime] = mapped_column(UTCDatetime, nullable=False, default=utcnow)
