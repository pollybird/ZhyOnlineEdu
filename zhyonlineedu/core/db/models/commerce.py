"""会员档位 / 订阅 / 订单 / 支付事件表（§10.6 · M2.12）。

口径：
- `member_level` 是售卖档位（等级 / 价格 / 时长），软删复合唯一键 `(code, delete_seq)`；
- `pay_order` 冻结下单时的等级、时长与金额快照：档位后续改价不影响在途订单；
- `member_subscription` 是授权事实来源：`active_sub()` 只认 `status=active` 且未到期，
  同一订单号唯一（`order_no`），重复回调绝不会发两次会员；
- `pay_event` 以 `event_id` 唯一：重复/乱序回调可幂等回放，状态只前进不后退（PAY-STATE-001~004）。
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

from sqlalchemy import Boolean, Index, Integer, String, false, true
from sqlalchemy.orm import Mapped, mapped_column

from ..base import Base
from ..types import Money, SoftDeleteMixin, UTCDatetime, VersionedMixin

SORT_STEP = 1024
CURRENCY = "CNY"
ORDER_STATUSES: tuple[str, ...] = ("created", "paying", "paid", "cancelled", "expired", "failed")
ORDER_OPEN_STATUSES: tuple[str, ...] = ("created", "paying")
ORDER_TERMINAL_STATUSES: tuple[str, ...] = ("paid", "cancelled", "expired", "failed")
SUBSCRIPTION_STATUSES: tuple[str, ...] = ("active", "expired", "revoked")
PAY_EVENT_STATUSES: tuple[str, ...] = ("paying", "paid", "failed")
PAY_CHANNELS: tuple[str, ...] = ("wechat", "alipay", "mock")


def utcnow() -> datetime:
    return datetime.now(UTC)


def dec(value: str | int | float | Decimal) -> Decimal:
    """金额统一 6 位小数：与 `Money(18,6)` 的口径一致，避免浮点漂移。"""
    return Decimal(str(value)).quantize(Decimal("0.000001"))


class MemberLevel(SoftDeleteMixin, VersionedMixin, Base):
    """会员档位（如月卡 / 年卡）；停用后不可下单，但已购订单不受影响。"""

    __tablename__ = "member_level"
    __soft_delete_uq__ = ("code",)
    __table_args__ = (Index("ix_member_level_enabled_sort", "is_enabled", "sort_order"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    code: Mapped[str] = mapped_column(String(32), nullable=False)
    name: Mapped[str] = mapped_column(String(64), nullable=False)
    # 等级：会员课程要求 >= 1；后续可按课程要求细分
    level: Mapped[int] = mapped_column(Integer, nullable=False, default=1, server_default="1")
    price: Mapped[Decimal] = mapped_column(
        Money, nullable=False, default=Decimal("0.000000"), server_default="0"
    )
    duration_days: Mapped[int] = mapped_column(
        Integer, nullable=False, default=30, server_default="30"
    )
    is_enabled: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True, server_default=true()
    )
    sort_order: Mapped[int] = mapped_column(
        Integer, nullable=False, default=SORT_STEP, server_default=str(SORT_STEP)
    )
    created_at: Mapped[datetime] = mapped_column(UTCDatetime, nullable=False, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(UTCDatetime, nullable=False, default=utcnow)


class PayOrder(VersionedMixin, Base):
    """支付订单：金额 / 等级 / 时长均为下单时快照（不随后续改价漂移）。"""

    __tablename__ = "pay_order"
    __table_args__ = (Index("ix_pay_order_user_status", "user_id", "status"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    order_no: Mapped[str] = mapped_column(String(32), nullable=False, unique=True)
    user_id: Mapped[int] = mapped_column(Integer, nullable=False, index=True)
    level_id: Mapped[int] = mapped_column(Integer, nullable=False)
    level: Mapped[int] = mapped_column(Integer, nullable=False)
    level_code: Mapped[str] = mapped_column(String(32), nullable=False)
    level_name: Mapped[str] = mapped_column(String(64), nullable=False)
    duration_days: Mapped[int] = mapped_column(Integer, nullable=False)
    amount: Mapped[Decimal] = mapped_column(Money, nullable=False)
    currency: Mapped[str] = mapped_column(
        String(3), nullable=False, default=CURRENCY, server_default=CURRENCY
    )
    status: Mapped[str] = mapped_column(
        String(16), nullable=False, default="created", server_default="created", index=True
    )
    channel: Mapped[str] = mapped_column(String(16), nullable=False)
    provider_txn_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    paid_at: Mapped[datetime | None] = mapped_column(UTCDatetime, nullable=True)
    expire_at: Mapped[datetime] = mapped_column(UTCDatetime, nullable=False, index=True)
    close_reason: Mapped[str | None] = mapped_column(String(64), nullable=True)
    created_at: Mapped[datetime] = mapped_column(UTCDatetime, nullable=False, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(UTCDatetime, nullable=False, default=utcnow)


class MemberSubscription(VersionedMixin, Base):
    """会员订阅：`active_sub()` 的唯一事实来源；同一订单最多一条（重复回调不双发）。"""

    __tablename__ = "member_subscription"
    __table_args__ = (Index("ix_member_subscription_user_status", "user_id", "status"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(Integer, nullable=False, index=True)
    level_id: Mapped[int] = mapped_column(Integer, nullable=False)
    level: Mapped[int] = mapped_column(Integer, nullable=False)
    level_code: Mapped[str] = mapped_column(String(32), nullable=False)
    level_name: Mapped[str] = mapped_column(String(64), nullable=False)
    # order / grant：订单支付或管理端赠送
    source: Mapped[str] = mapped_column(
        String(16), nullable=False, default="order", server_default="order"
    )
    # 订单来源去重键；管理端赠送为空
    order_no: Mapped[str | None] = mapped_column(String(32), nullable=True, unique=True)
    start_at: Mapped[datetime] = mapped_column(UTCDatetime, nullable=False)
    expire_at: Mapped[datetime] = mapped_column(UTCDatetime, nullable=False, index=True)
    status: Mapped[str] = mapped_column(
        String(16), nullable=False, default="active", server_default="active", index=True
    )
    created_at: Mapped[datetime] = mapped_column(UTCDatetime, nullable=False, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(UTCDatetime, nullable=False, default=utcnow)


class PayEvent(Base):
    """支付事件流水：`event_id` 唯一，重复回调幂等；记录状态迁移前后值，便于对账。"""

    __tablename__ = "pay_event"
    __table_args__ = (Index("ix_pay_event_order_created", "order_no", "created_at"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    order_no: Mapped[str] = mapped_column(String(32), nullable=False)
    event_id: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    channel: Mapped[str] = mapped_column(String(16), nullable=False)
    status_before: Mapped[str | None] = mapped_column(String(16), nullable=True)
    status_after: Mapped[str | None] = mapped_column(String(16), nullable=True)
    amount: Mapped[Decimal | None] = mapped_column(Money, nullable=True)
    signature_ok: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True, server_default=true()
    )
    applied: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default=false()
    )
    detail: Mapped[str] = mapped_column(String(255), nullable=False, default="", server_default="")
    created_at: Mapped[datetime] = mapped_column(UTCDatetime, nullable=False, default=utcnow)
