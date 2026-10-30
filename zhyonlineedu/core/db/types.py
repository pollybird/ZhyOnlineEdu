"""跨方言字段类型（§5.3 统一建模规约）。

规约要点：
- 时间一律 UTCDatetime（存 UTC，展示层转时区）；
- 金额一律 Numeric(18,6)，禁止 Float（SQLite 裸绑 Decimal 会抛错，M0 实测 D14）；
- 软删统一 SoftDeleteMixin：UNIQUE(uq_cols, delete_seq)，未删 delete_seq=0（D19a/F6），
  禁止 1970-01-01 单哨兵；
- 乐观锁统一 VersionedMixin：UPDATE 时 version+1，upsert 回读与进度对账必需（D10a/F2）。
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import ClassVar

from sqlalchemy import BigInteger, DateTime, Integer, Numeric, TypeDecorator
from sqlalchemy.engine import Dialect as SQLDialect
from sqlalchemy.orm import Mapped, mapped_column

DeleteSeq = BigInteger  # 未删除 = 0；删除时取序列/毫秒单调值
Money = Numeric(18, 6)  # 金额：18 位、6 位小数（与客户口径一致）


class UTCDatetime(TypeDecorator[datetime]):
    """入库统一 UTC naive，出库补回 tzinfo=UTC（避免各驱动时区行为差异）。"""

    impl = DateTime
    cache_ok = True

    def process_bind_param(self, value: datetime | None, dialect: SQLDialect) -> datetime | None:
        if value is None:
            return None
        aware = value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)
        return aware.replace(tzinfo=None)

    def process_result_value(self, value: datetime | None, dialect: SQLDialect) -> datetime | None:
        if value is None:
            return None
        return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


class SoftDeleteMixin:
    # 具体表在类体里声明业务自然键列，例如 __soft_delete_uq__ = ("org_id", "user_no")；
    # core.db.base.Base 建表前注入 UNIQUE(uq_cols, delete_seq)，启动/迁移时断言（M1.3 接入）。
    __soft_delete_uq__: ClassVar[tuple[str, ...]] = ()

    deleted_at: Mapped[datetime | None] = mapped_column(UTCDatetime, nullable=True, default=None)
    delete_seq: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0, index=True)


class VersionedMixin:
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
