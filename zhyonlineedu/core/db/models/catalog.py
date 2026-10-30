"""目录 / 课程 / 课时三层（§6.6 / §10.1 / §10.2）。

口径：
- 学科树 `parent_id` 自关联，`sort_order` 步进 1024，并发拖拽用 `version` 乐观锁；
- 课程三层：`edu_course` → `edu_course_chapter` → `edu_course_lesson`；
- 软删统一 `UNIQUE(uq_cols, delete_seq)`（D19a/F6），未删 `delete_seq=0`；
- 课程 `mode` / `access_type` 是模式与授权判定的输入，索引 `ix_edu_course_mode_access`
  供模式切换预检（§9.6）统计"切换后 0 门课可看"。
"""

from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import Boolean, Index, Integer, String, Text, false, true
from sqlalchemy.orm import Mapped, mapped_column

from ..base import Base
from ..types import SoftDeleteMixin, UTCDatetime, VersionedMixin


def utcnow() -> datetime:
    return datetime.now(UTC)


class EduSubject(SoftDeleteMixin, VersionedMixin, Base):
    """学科树节点（parent_id 为空 = 根节点）。

    根节点同名唯一由服务层校验：跨方言的 `UNIQUE(parent_id, name, delete_seq)`
    在 `parent_id IS NULL` 时不生效（NULL 互不相等），因此不在库层强依赖。
    """

    __tablename__ = "edu_subject"
    __soft_delete_uq__ = ("parent_id", "name")

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    parent_id: Mapped[int | None] = mapped_column(Integer, nullable=True, index=True)
    name: Mapped[str] = mapped_column(String(64), nullable=False)
    # 排序步进 1024：单条 UPDATE 重排，避免逐行改号放大小数精度问题
    sort_order: Mapped[int] = mapped_column(Integer, nullable=False, default=1024)
    # 学科停用后其课程不可上架（发布前置检查，复用 SUBJECT_DISABLED）
    is_enabled: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True, server_default=true()
    )
    created_at: Mapped[datetime] = mapped_column(UTCDatetime, nullable=False, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(UTCDatetime, nullable=False, default=utcnow)


class EduCourse(SoftDeleteMixin, VersionedMixin, Base):
    """课程（三层第一层）。"""

    __tablename__ = "edu_course"
    __soft_delete_uq__ = ("subject_id", "title")
    __table_args__ = (Index("ix_edu_course_mode_access", "mode", "access_type"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    subject_id: Mapped[int] = mapped_column(Integer, nullable=False, index=True)
    title: Mapped[str] = mapped_column(String(128), nullable=False)
    summary: Mapped[str | None] = mapped_column(Text, nullable=True)
    cover_url: Mapped[str | None] = mapped_column(String(255), nullable=True)
    # internal / external / both（§9 模式隔离）
    mode: Mapped[str] = mapped_column(
        String(16), nullable=False, default="both", server_default="both"
    )
    # public / member / class（统一访问判定输入，§9.4）
    access_type: Mapped[str] = mapped_column(
        String(16), nullable=False, default="public", server_default="public"
    )
    # draft / published / archived
    status: Mapped[str] = mapped_column(
        String(16), nullable=False, default="draft", server_default="draft", index=True
    )
    teacher_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    sort_order: Mapped[int] = mapped_column(Integer, nullable=False, default=1024)
    published_at: Mapped[datetime | None] = mapped_column(UTCDatetime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(UTCDatetime, nullable=False, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(UTCDatetime, nullable=False, default=utcnow)


class EduCourseChapter(SoftDeleteMixin, VersionedMixin, Base):
    """章（三层第二层）。"""

    __tablename__ = "edu_course_chapter"
    __soft_delete_uq__ = ("course_id", "title")

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    course_id: Mapped[int] = mapped_column(Integer, nullable=False, index=True)
    title: Mapped[str] = mapped_column(String(128), nullable=False)
    sort_order: Mapped[int] = mapped_column(Integer, nullable=False, default=1024)
    created_at: Mapped[datetime] = mapped_column(UTCDatetime, nullable=False, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(UTCDatetime, nullable=False, default=utcnow)


class EduCourseLesson(SoftDeleteMixin, VersionedMixin, Base):
    """课时（三层第三层）：`course_id` 冗余，供上架就绪校验与进度聚合免二次 join。"""

    __tablename__ = "edu_course_lesson"
    __soft_delete_uq__ = ("chapter_id", "title")

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    chapter_id: Mapped[int] = mapped_column(Integer, nullable=False, index=True)
    course_id: Mapped[int] = mapped_column(Integer, nullable=False, index=True)
    title: Mapped[str] = mapped_column(String(128), nullable=False)
    # video / text / live
    kind: Mapped[str] = mapped_column(
        String(16), nullable=False, default="video", server_default="video"
    )
    duration_sec: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    is_free: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default=false()
    )
    sort_order: Mapped[int] = mapped_column(Integer, nullable=False, default=1024)
    created_at: Mapped[datetime] = mapped_column(UTCDatetime, nullable=False, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(UTCDatetime, nullable=False, default=utcnow)
