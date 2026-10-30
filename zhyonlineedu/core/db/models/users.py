"""班级、班级成员与导入批次（§10.5 / §6.5 · M2.7）。

口径：
- `edu_class` 是班级授权的事实来源：`access_type=class` 的课程仅班级成员可见
  （任一启用班级的成员即视为有班级授权；班级停用后立即失去授权）；
- `edu_class_member` 只保留当前关系，移除即硬删（不参与软删 UK 规约）；
- `sys_import_batch` 记录一次导入的批次号、逐行结果与冲突统计，供管理端复核
  「冲突不覆盖」；`rows_json` 只存脱敏后的行结果（不存口令）。
"""

from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import Boolean, Integer, String, Text, true
from sqlalchemy.orm import Mapped, mapped_column

from ..base import Base
from ..types import SoftDeleteMixin, UTCDatetime, VersionedMixin

SORT_STEP = 1024

CLASS_STATUSES: tuple[str, ...] = ("active", "disabled")
IMPORT_STATUSES: tuple[str, ...] = ("completed", "partial", "failed")
IMPORT_ACTIONS: tuple[str, ...] = ("created", "updated", "skipped", "invalid")
ON_CONFLICT_ACTIONS: tuple[str, ...] = ("report_only", "overwrite")


def utcnow() -> datetime:
    return datetime.now(UTC)


class EduClass(SoftDeleteMixin, VersionedMixin, Base):
    """班级（组织内同名唯一；停用后其成员立即失去 class 课程授权）。"""

    __tablename__ = "edu_class"
    __soft_delete_uq__ = ("org_id", "name")

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    org_id: Mapped[int] = mapped_column(Integer, nullable=False, default=1, server_default="1")
    name: Mapped[str] = mapped_column(String(64), nullable=False)
    teacher_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    is_enabled: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True, server_default=true()
    )
    sort_order: Mapped[int] = mapped_column(Integer, nullable=False, default=SORT_STEP)
    created_at: Mapped[datetime] = mapped_column(UTCDatetime, nullable=False, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(UTCDatetime, nullable=False, default=utcnow)


class EduClassMember(Base):
    """班级成员（复合主键；移除即硬删，避免"退班后仍可看"）。"""

    __tablename__ = "edu_class_member"

    class_id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    created_at: Mapped[datetime] = mapped_column(UTCDatetime, nullable=False, default=utcnow)


class SysImportBatch(Base):
    """导入批次（`import_batch_no` 沿用计划书 §10.5）。"""

    __tablename__ = "sys_import_batch"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    batch_no: Mapped[str] = mapped_column(String(32), nullable=False, unique=True)
    kind: Mapped[str] = mapped_column(
        String(16), nullable=False, default="user", server_default="user"
    )
    status: Mapped[str] = mapped_column(
        String(16), nullable=False, default="completed", server_default="completed", index=True
    )
    on_conflict_action: Mapped[str] = mapped_column(
        String(16), nullable=False, default="report_only", server_default="report_only"
    )
    total_rows: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    created_rows: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0"
    )
    updated_rows: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0"
    )
    skipped_rows: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0"
    )
    invalid_rows: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0"
    )
    # 逐行结果 JSON（row_no/username/user_no/action/error），不含口令与哈希
    rows_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    operator_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    created_at: Mapped[datetime] = mapped_column(UTCDatetime, nullable=False, default=utcnow)
    finished_at: Mapped[datetime | None] = mapped_column(UTCDatetime, nullable=True)
