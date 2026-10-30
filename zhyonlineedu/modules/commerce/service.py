"""会员档位 / 订单 / 支付回调 / 订阅授权（§10.6 · M2.12）。

口径（与计划书「状态不倒退」一致）：
- 订单状态机：`created → paying → paid`，`created/paying` 可取消或超时；
  `paid` 为终态，任何"回退到 paying"或"已关闭再成功"一律 `PAY_STATE_INVALID(409)`；
- 回调与查单补偿共用 `_apply_event()`：条件更新 `WHERE status IN ('created','paying')`
  只前进不后退；`rowcount=0` 时回读判定"重复成功"或"非法倒退"（PAY-STATE-001~004）；
- `pay_event.event_id` 唯一 + `member_subscription.order_no` 唯一：重复/并发 paid 回调
  只发一次会员，重放返回首次结果（HTTP 200，`duplicate=true`）；
- 订单冻结 `level / level_code / level_name / duration_days / amount` 快照，
  档位后续改价不影响在途订单；
- 回调签名覆盖 `channel|order_no|event_id|status|amount|paid_at`（HMAC-SHA256），
  渠道 / 金额任一不一致分别 `PAY_CHANNEL_MISMATCH(422)` / `PAY_AMOUNT_MISMATCH(409)`；
- `active_sub()` 是会员课程授权的唯一事实来源：只认 `status=active` 且未到期，
  多个有效订阅取等级最高、到期最晚的一条。
"""

from __future__ import annotations

import hashlib
import hmac
import secrets
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime, timedelta
from decimal import Decimal, InvalidOperation
from typing import Any, NoReturn, cast

from sqlalchemy import func, select, update
from sqlalchemy.engine import CursorResult
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from zhyonlineedu.core.codes import (
    CONFLICT_RETRY,
    NOT_FOUND,
    PAY_AMOUNT_MISMATCH,
    PAY_CHANNEL_MISMATCH,
    PAY_SIGNATURE_INVALID,
    PAY_STATE_INVALID,
    VALIDATION_FAILED,
)
from zhyonlineedu.core.config import Config
from zhyonlineedu.core.db.engine import Database, ensure_database
from zhyonlineedu.core.db.models.account import AccountUser
from zhyonlineedu.core.db.models.commerce import (
    CURRENCY,
    ORDER_OPEN_STATUSES,
    PAY_CHANNELS,
    PAY_EVENT_STATUSES,
    MemberLevel,
    MemberSubscription,
    PayEvent,
    PayOrder,
    dec,
    utcnow,
)
from zhyonlineedu.core.db.query import not_deleted
from zhyonlineedu.core.errors import AppError

PAGE_SIZE_DEFAULT = 20
PAGE_SIZE_MAX = 100
SORT_STEP = 1024
MONEY_QUANT = Decimal("0.000001")
SIGNATURE_HEADER = "X-ZHY-Pay-Signature"


def _iso(value: datetime | None) -> str | None:
    return value.isoformat(timespec="seconds") if value is not None else None


def _money(value: Decimal | None) -> str:
    if value is None:
        return "0.000000"
    return f"{Decimal(value).quantize(MONEY_QUANT):.6f}"


def _fail(message: str) -> NoReturn:
    raise AppError(VALIDATION_FAILED, data={"errors": [message]})


def _text(value: object, field: str, *, max_len: int) -> str:
    if not isinstance(value, str):
        _fail(f"{field} 必须是字符串")
    text = value.strip()
    if text == "":
        _fail(f"{field} 不能为空")
    if len(text) > max_len:
        _fail(f"{field} 长度不能超过 {max_len}")
    return text


def _optional_text(value: object, field: str, *, max_len: int) -> str | None:
    if value is None:
        return None
    return _text(value, field, max_len=max_len)


def _int(value: object, field: str, *, minimum: int = 0, maximum: int | None = None) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        _fail(f"{field} 必须是整数")
    if value < minimum:
        _fail(f"{field} 不能小于 {minimum}")
    if maximum is not None and value > maximum:
        _fail(f"{field} 不能大于 {maximum}")
    return value


def _choice(value: object, field: str, allowed: Sequence[str]) -> str:
    if not isinstance(value, str) or value not in allowed:
        _fail(f"{field} 只能是 {'/'.join(allowed)}")
    return value


def _decimal(value: object, field: str, *, minimum: Decimal = Decimal("0")) -> Decimal:
    if isinstance(value, bool) or value is None:
        _fail(f"{field} 必须是金额")
    try:
        amount = dec(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise AppError(VALIDATION_FAILED, data={"errors": [f"{field} 不是合法金额"]}) from exc
    if amount < minimum:
        _fail(f"{field} 不能小于 {_money(minimum)}")
    return amount


def _query_int(
    value: object, field: str, *, default: int, minimum: int, maximum: int | None = None
) -> int:
    if value is None or value == "":
        return default
    if isinstance(value, str) and value.lstrip("-").isdigit():
        value = int(value)
    return _int(value, field, minimum=minimum, maximum=maximum)


def _version(body: Mapping[str, Any]) -> int:
    return _int(body.get("version"), "version", minimum=1)


def _datetime(value: object, field: str) -> datetime | None:
    if value is None or (isinstance(value, str) and value.strip() == ""):
        return None
    if isinstance(value, datetime):
        moment = value
    elif isinstance(value, str):
        try:
            moment = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
        except ValueError:
            _fail(f"{field} 不是合法的 ISO8601 时间")
    else:
        _fail(f"{field} 必须是 ISO8601 字符串")
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=UTC)
    return moment.astimezone(UTC)


def _paid_at_text(value: object) -> str:
    """签名口径使用回调原文；缺省空串，避免时区/格式在两端不一致。"""
    if value is None:
        return ""
    if isinstance(value, str):
        return value.strip()
    _fail("paid_at 必须是 ISO8601 字符串")


# ── 签名 ───────────────────────────────────────────────────────────────────


def callback_canonical(
    channel: str, order_no: str, event_id: str, status: str, amount: str, paid_at: str
) -> str:
    return "|".join((channel, order_no, event_id, status, amount, paid_at))


def sign_callback(
    secret: str,
    *,
    channel: str,
    order_no: str,
    event_id: str,
    status: str,
    amount: str,
    paid_at: str = "",
) -> str:
    payload = callback_canonical(channel, order_no, event_id, status, amount, paid_at)
    return hmac.new(secret.encode("utf-8"), payload.encode("utf-8"), hashlib.sha256).hexdigest()


# ── 会员授权（playback / assess / worker 共用）─────────────────────────────


def active_sub(
    session: Session, user_id: int, *, now: datetime | None = None
) -> MemberSubscription | None:
    """有效订阅：`status=active` 且未到期；多条取等级最高、到期最晚。"""
    moment = now or utcnow()
    rows = (
        session.execute(
            select(MemberSubscription).where(
                MemberSubscription.user_id == user_id,
                MemberSubscription.status == "active",
                MemberSubscription.start_at <= moment,
                MemberSubscription.expire_at > moment,
            )
        )
        .scalars()
        .all()
    )
    if not rows:
        return None
    return max(rows, key=lambda row: (int(row.level), row.expire_at))


def active_level(session: Session, user_id: int, *, now: datetime | None = None) -> int | None:
    row = active_sub(session, user_id, now=now)
    return None if row is None else int(row.level)


def grant_level(
    session: Session,
    *,
    user_id: int,
    level_id: int,
    level: int,
    level_code: str,
    level_name: str,
    duration_days: int,
    source: str,
    order_no: str | None = None,
    now: datetime | None = None,
) -> MemberSubscription:
    """写入一条会员订阅；同一 `order_no` 幂等（重复回调不双发）。

    叠加口径：新订阅从"当前最晚有效到期时间"继续累加，避免同档续费丢时长。
    """
    moment = now or utcnow()
    if order_no is not None:
        existing = session.execute(
            select(MemberSubscription).where(MemberSubscription.order_no == order_no)
        ).scalar_one_or_none()
        if existing is not None:
            return existing
    latest = session.execute(
        select(func.max(MemberSubscription.expire_at)).where(
            MemberSubscription.user_id == user_id,
            MemberSubscription.status == "active",
            MemberSubscription.expire_at > moment,
        )
    ).scalar_one_or_none()
    base = moment if latest is None else max(moment, latest)
    row = MemberSubscription(
        user_id=user_id,
        level_id=level_id,
        level=level,
        level_code=level_code,
        level_name=level_name,
        source=source,
        order_no=order_no,
        start_at=moment,
        expire_at=base + timedelta(days=duration_days),
        status="active",
        version=1,
        created_at=moment,
        updated_at=moment,
    )
    session.add(row)
    session.flush()
    return row


# ── 输出 ───────────────────────────────────────────────────────────────────


def _level_out(row: MemberLevel) -> dict[str, Any]:
    return {
        "id": row.id,
        "code": row.code,
        "name": row.name,
        "level": row.level,
        "price": _money(row.price),
        "duration_days": row.duration_days,
        "is_enabled": bool(row.is_enabled),
        "sort_order": row.sort_order,
        "version": row.version,
        "created_at": _iso(row.created_at),
        "updated_at": _iso(row.updated_at),
    }


def _order_out(row: PayOrder) -> dict[str, Any]:
    return {
        "order_no": row.order_no,
        "user_id": row.user_id,
        "level_id": row.level_id,
        "level": row.level,
        "level_code": row.level_code,
        "level_name": row.level_name,
        "duration_days": row.duration_days,
        "amount": _money(row.amount),
        "currency": row.currency,
        "status": row.status,
        "channel": row.channel,
        "provider_txn_id": row.provider_txn_id,
        "paid_at": _iso(row.paid_at),
        "expire_at": _iso(row.expire_at),
        "close_reason": row.close_reason,
        "version": row.version,
        "created_at": _iso(row.created_at),
        "updated_at": _iso(row.updated_at),
    }


def _sub_out(row: MemberSubscription) -> dict[str, Any]:
    return {
        "id": row.id,
        "user_id": row.user_id,
        "level_id": row.level_id,
        "level": row.level,
        "level_code": row.level_code,
        "level_name": row.level_name,
        "source": row.source,
        "order_no": row.order_no,
        "start_at": _iso(row.start_at),
        "expire_at": _iso(row.expire_at),
        "status": row.status,
        "version": row.version,
        "created_at": _iso(row.created_at),
        "updated_at": _iso(row.updated_at),
    }


def _member_out(
    user_id: int, current: MemberSubscription | None, subscriptions: list[MemberSubscription]
) -> dict[str, Any]:
    return {
        "user_id": user_id,
        "active": current is not None,
        "level": None if current is None else current.level,
        "level_code": None if current is None else current.level_code,
        "level_name": None if current is None else current.level_name,
        "expire_at": None if current is None else _iso(current.expire_at),
        "subscriptions": [_sub_out(row) for row in subscriptions],
    }


def _event_out(
    order: PayOrder, event: PayEvent | None, *, applied: bool, duplicate: bool
) -> dict[str, Any]:
    return {
        "order_no": order.order_no,
        "event_id": None if event is None else event.event_id,
        "status": order.status,
        "applied": applied,
        "duplicate": duplicate,
    }


class CommerceService:
    """无状态服务：每个动作自建短会话。"""

    def __init__(self, config: Config, *, db: Database | None = None) -> None:
        self.config = config
        self._db = db

    @property
    def db(self) -> Database:
        if self._db is None:
            self._db = ensure_database(self.config)
        return self._db

    @property
    def callback_secret(self) -> str:
        return self.config.get_str("pay.callback_secret")

    # ── 档位（member:manage）─────────────────────────────────────────────

    def list_levels(self, query: Mapping[str, Any]) -> dict[str, Any]:
        conditions: list[Any] = [not_deleted(MemberLevel)]
        if query.get("is_enabled") not in (None, ""):
            flag = _choice(query.get("is_enabled"), "is_enabled", ("true", "false"))
            conditions.append(MemberLevel.is_enabled == (flag == "true"))
        with Session(self.db.engine) as session:
            rows = (
                session.execute(
                    select(MemberLevel)
                    .where(*conditions)
                    .order_by(MemberLevel.sort_order, MemberLevel.id)
                )
                .scalars()
                .all()
            )
        return {"items": [_level_out(row) for row in rows], "total": len(rows)}

    def create_level(self, body: Mapping[str, Any]) -> dict[str, Any]:
        code = _text(body.get("code"), "code", max_len=32)
        name = _text(body.get("name"), "name", max_len=64)
        level = _int(body.get("level", 1), "level", minimum=1, maximum=99)
        price = _decimal(body.get("price", "0"), "price")
        duration_days = _int(
            body.get("duration_days", 30), "duration_days", minimum=1, maximum=3650
        )
        is_enabled = bool(body.get("is_enabled", True))
        now = utcnow()
        with Session(self.db.engine) as session:
            self._ensure_level_code_free(session, code)
            max_order = int(
                session.execute(
                    select(func.coalesce(func.max(MemberLevel.sort_order), 0)).where(
                        not_deleted(MemberLevel)
                    )
                ).scalar_one()
            )
            sort_order = (
                max_order + SORT_STEP
                if body.get("sort_order") is None
                else _int(body.get("sort_order"), "sort_order", minimum=0, maximum=1_000_000)
            )
            row = MemberLevel(
                code=code,
                name=name,
                level=level,
                price=price,
                duration_days=duration_days,
                is_enabled=is_enabled,
                sort_order=sort_order,
                version=1,
                created_at=now,
                updated_at=now,
            )
            session.add(row)
            session.commit()
            return _level_out(row)

    def update_level(self, level_id: int, body: Mapping[str, Any]) -> dict[str, Any]:
        version = _version(body)
        now = utcnow()
        with Session(self.db.engine) as session:
            row = self._level_row(session, level_id)
            if row.version != version:
                self._conflict("member_level", level_id, version)
            values: dict[str, Any] = {}
            if "code" in body:
                code = _text(body.get("code"), "code", max_len=32)
                if code != row.code:
                    self._ensure_level_code_free(session, code, exclude_id=level_id)
                values["code"] = code
            if "name" in body:
                values["name"] = _text(body.get("name"), "name", max_len=64)
            if "level" in body:
                values["level"] = _int(body.get("level"), "level", minimum=1, maximum=99)
            if "price" in body:
                values["price"] = _decimal(body.get("price"), "price")
            if "duration_days" in body:
                values["duration_days"] = _int(
                    body.get("duration_days"), "duration_days", minimum=1, maximum=3650
                )
            if "is_enabled" in body:
                values["is_enabled"] = bool(body.get("is_enabled"))
            if "sort_order" in body:
                values["sort_order"] = _int(
                    body.get("sort_order"), "sort_order", minimum=0, maximum=1_000_000
                )
            if not values:
                _fail("没有可更新的字段")
            result = cast(
                CursorResult[Any],
                session.execute(
                    update(MemberLevel)
                    .where(
                        MemberLevel.id == level_id,
                        MemberLevel.version == version,
                        not_deleted(MemberLevel),
                    )
                    .values(**values, version=MemberLevel.version + 1, updated_at=now)
                ),
            )
            if result.rowcount != 1:
                self._conflict("member_level", level_id, version)
            session.commit()
            return _level_out(self._level_row(session, level_id))

    # ── 我的会员 / 管理端订阅 / 直接开通 ────────────────────────────────────

    def my_member(self, user_id: int) -> dict[str, Any]:
        with Session(self.db.engine) as session:
            current = active_sub(session, user_id)
            rows = (
                session.execute(
                    select(MemberSubscription)
                    .where(MemberSubscription.user_id == user_id)
                    .order_by(MemberSubscription.expire_at.desc(), MemberSubscription.id.desc())
                )
                .scalars()
                .all()
            )
        return _member_out(user_id, current, list(rows))

    def list_subscriptions(self, query: Mapping[str, Any]) -> dict[str, Any]:
        page = _query_int(query.get("page"), "page", default=1, minimum=1)
        page_size = _query_int(
            query.get("page_size"),
            "page_size",
            default=PAGE_SIZE_DEFAULT,
            minimum=1,
            maximum=PAGE_SIZE_MAX,
        )
        conditions: list[Any] = []
        if query.get("user_id") not in (None, ""):
            conditions.append(
                MemberSubscription.user_id
                == _query_int(query.get("user_id"), "user_id", default=1, minimum=1)
            )
        if query.get("status") not in (None, ""):
            conditions.append(
                MemberSubscription.status
                == _choice(query.get("status"), "status", ("active", "expired", "revoked"))
            )
        with Session(self.db.engine) as session:
            total = int(
                session.execute(
                    select(func.count()).select_from(MemberSubscription).where(*conditions)
                ).scalar_one()
            )
            rows = (
                session.execute(
                    select(MemberSubscription)
                    .where(*conditions)
                    .order_by(MemberSubscription.id.desc())
                    .offset((page - 1) * page_size)
                    .limit(page_size)
                )
                .scalars()
                .all()
            )
        return {
            "items": [_sub_out(row) for row in rows],
            "total": total,
            "page": page,
            "page_size": page_size,
        }

    def grant_member(self, body: Mapping[str, Any]) -> dict[str, Any]:
        user_id = _int(body.get("user_id"), "user_id", minimum=1)
        level_id = _int(body.get("level_id"), "level_id", minimum=1)
        now = utcnow()
        with Session(self.db.engine) as session:
            self._user_row(session, user_id)
            level = self._level_row(session, level_id)
            if not bool(level.is_enabled):
                raise AppError(
                    PAY_STATE_INVALID,
                    data={"resource": "member_level", "id": level_id, "reason": "disabled"},
                )
            duration = (
                int(level.duration_days)
                if body.get("duration_days") is None
                else _int(body.get("duration_days"), "duration_days", minimum=1, maximum=3650)
            )
            row = grant_level(
                session,
                user_id=user_id,
                level_id=level_id,
                level=int(level.level),
                level_code=level.code,
                level_name=level.name,
                duration_days=duration,
                source="grant",
                now=now,
            )
            session.commit()
            current = active_sub(session, user_id, now=now)
            rows = (
                session.execute(
                    select(MemberSubscription)
                    .where(MemberSubscription.user_id == user_id)
                    .order_by(MemberSubscription.id.desc())
                )
                .scalars()
                .all()
            )
            return {
                "subscription": _sub_out(row),
                "member": _member_out(user_id, current, list(rows)),
            }

    # ── 订单 ────────────────────────────────────────────────────────────

    def create_order(self, body: Mapping[str, Any], *, user_id: int) -> dict[str, Any]:
        level_id = _int(body.get("level_id"), "level_id", minimum=1)
        channel = (
            "mock"
            if body.get("channel") in (None, "")
            else _choice(body.get("channel"), "channel", PAY_CHANNELS)
        )
        now = utcnow()
        with Session(self.db.engine) as session:
            self._user_row(session, user_id)
            level = self._level_row(session, level_id)
            if not bool(level.is_enabled):
                raise AppError(
                    PAY_STATE_INVALID,
                    data={"resource": "member_level", "id": level_id, "reason": "disabled"},
                )
            open_orders = int(
                session.execute(
                    select(func.count())
                    .select_from(PayOrder)
                    .where(
                        PayOrder.user_id == user_id,
                        PayOrder.status.in_(ORDER_OPEN_STATUSES),
                        PayOrder.expire_at > now,
                    )
                ).scalar_one()
            )
            if open_orders >= self.config.get_int("pay.max_open_orders"):
                raise AppError(
                    PAY_STATE_INVALID,
                    data={
                        "reason": "too_many_open_orders",
                        "max_open_orders": self.config.get_int("pay.max_open_orders"),
                    },
                )
            row = PayOrder(
                order_no=self._new_order_no(now),
                user_id=user_id,
                level_id=level_id,
                level=int(level.level),
                level_code=level.code,
                level_name=level.name,
                duration_days=int(level.duration_days),
                amount=dec(level.price),
                currency=CURRENCY,
                status="created",
                channel=channel,
                expire_at=now + timedelta(seconds=self.config.get_int("pay.order_ttl_sec")),
                version=1,
                created_at=now,
                updated_at=now,
            )
            session.add(row)
            session.commit()
            return _order_out(row)

    def list_orders(self, query: Mapping[str, Any], *, user_id: int) -> dict[str, Any]:
        page = _query_int(query.get("page"), "page", default=1, minimum=1)
        page_size = _query_int(
            query.get("page_size"),
            "page_size",
            default=PAGE_SIZE_DEFAULT,
            minimum=1,
            maximum=PAGE_SIZE_MAX,
        )
        conditions: list[Any] = [PayOrder.user_id == user_id]
        if query.get("status") not in (None, ""):
            conditions.append(
                PayOrder.status
                == _choice(
                    query.get("status"),
                    "status",
                    ("created", "paying", "paid", "cancelled", "expired", "failed"),
                )
            )
        with Session(self.db.engine) as session:
            total = int(
                session.execute(
                    select(func.count()).select_from(PayOrder).where(*conditions)
                ).scalar_one()
            )
            rows = (
                session.execute(
                    select(PayOrder)
                    .where(*conditions)
                    .order_by(PayOrder.id.desc())
                    .offset((page - 1) * page_size)
                    .limit(page_size)
                )
                .scalars()
                .all()
            )
        return {
            "items": [_order_out(row) for row in rows],
            "total": total,
            "page": page,
            "page_size": page_size,
        }

    def get_order(self, order_no: str, *, user_id: int, can_manage: bool) -> dict[str, Any]:
        with Session(self.db.engine) as session:
            row = self._order_row(session, order_no, user_id=None if can_manage else user_id)
            return _order_out(row)

    def cancel_order(self, order_no: str, *, user_id: int) -> dict[str, Any]:
        now = utcnow()
        with Session(self.db.engine) as session:
            row = self._order_row(session, order_no, user_id=user_id)
            if row.status == "cancelled":
                return _order_out(row)
            if row.status not in ORDER_OPEN_STATUSES:
                raise AppError(
                    PAY_STATE_INVALID,
                    data={"order_no": order_no, "status": row.status},
                )
            result = cast(
                CursorResult[Any],
                session.execute(
                    update(PayOrder)
                    .where(
                        PayOrder.id == row.id,
                        PayOrder.status.in_(ORDER_OPEN_STATUSES),
                    )
                    .values(
                        status="cancelled",
                        close_reason="user_cancelled",
                        version=PayOrder.version + 1,
                        updated_at=now,
                    )
                ),
            )
            if result.rowcount != 1:
                session.rollback()
                latest = self._order_row(session, order_no, user_id=user_id)
                if latest.status == "cancelled":
                    return _order_out(latest)
                raise AppError(
                    PAY_STATE_INVALID,
                    data={"order_no": order_no, "status": latest.status},
                )
            session.commit()
            return _order_out(self._order_row(session, order_no, user_id=user_id))

    # ── 支付回调（免鉴权，签名即凭证）────────────────────────────────────

    def notify(self, channel: str, body: Mapping[str, Any], signature: str) -> dict[str, Any]:
        channel = _choice(channel, "channel", PAY_CHANNELS)
        order_no = _text(body.get("order_no"), "order_no", max_len=32)
        event_id = _text(body.get("event_id"), "event_id", max_len=64)
        status = _choice(body.get("status"), "status", PAY_EVENT_STATUSES)
        amount = _decimal(body.get("amount"), "amount")
        paid_at_text = _paid_at_text(body.get("paid_at"))
        if not signature.strip():
            raise AppError(PAY_SIGNATURE_INVALID, data={"reason": "missing_signature"})
        expected = sign_callback(
            self.callback_secret,
            channel=channel,
            order_no=order_no,
            event_id=event_id,
            status=status,
            amount=_money(amount),
            paid_at=paid_at_text,
        )
        if not hmac.compare_digest(expected, signature.strip()):
            raise AppError(PAY_SIGNATURE_INVALID, data={"reason": "signature_mismatch"})
        return self._apply_event(
            channel=channel,
            order_no=order_no,
            event_id=event_id,
            status=status,
            amount=amount,
            paid_at=_datetime(body.get("paid_at"), "paid_at") or utcnow(),
            provider_txn_id=_optional_text(
                body.get("provider_txn_id"), "provider_txn_id", max_len=64
            ),
            detail="notify",
        )

    def reconcile(self, order_no: str, body: Mapping[str, Any]) -> dict[str, Any]:
        """管理端查单补偿：与回调共用同一"不倒退"状态机（PAY-STATE-004）。"""
        with Session(self.db.engine) as session:
            existing = self._order_row(session, order_no)
            channel = existing.channel
            snapshot_amount = dec(existing.amount)
            snapshot_txn = existing.provider_txn_id
        status = _choice(body.get("status", "paid"), "status", PAY_EVENT_STATUSES)
        amount = (
            _decimal(body.get("amount"), "amount")
            if body.get("amount") is not None
            else snapshot_amount
        )
        now = utcnow()
        provider_txn_id = (
            _optional_text(body.get("provider_txn_id"), "provider_txn_id", max_len=64)
            or snapshot_txn
            or f"RECONCILE-{order_no}"
        )
        return self._apply_event(
            channel=channel,
            order_no=order_no,
            event_id=f"reconcile:{order_no}:{status}",
            status=status,
            amount=amount,
            paid_at=now,
            provider_txn_id=provider_txn_id,
            detail="reconcile",
        )

    # ── 过期任务（worker 调用）───────────────────────────────────────────

    def expire_orders(self, *, now: datetime | None = None, batch_size: int = 200) -> int:
        moment = now or utcnow()
        total = 0
        with Session(self.db.engine) as session:
            while True:
                ids = (
                    session.execute(
                        select(PayOrder.id)
                        .where(
                            PayOrder.status.in_(ORDER_OPEN_STATUSES),
                            PayOrder.expire_at <= moment,
                        )
                        .limit(batch_size)
                    )
                    .scalars()
                    .all()
                )
                if not ids:
                    break
                session.execute(
                    update(PayOrder)
                    .where(
                        PayOrder.id.in_(list(ids)),
                        PayOrder.status.in_(ORDER_OPEN_STATUSES),
                    )
                    .values(
                        status="expired",
                        close_reason="timeout",
                        version=PayOrder.version + 1,
                        updated_at=moment,
                    )
                )
                session.commit()
                total += len(ids)
        return total

    def expire_memberships(self, *, now: datetime | None = None, batch_size: int = 500) -> int:
        moment = now or utcnow()
        total = 0
        with Session(self.db.engine) as session:
            while True:
                ids = (
                    session.execute(
                        select(MemberSubscription.id)
                        .where(
                            MemberSubscription.status == "active",
                            MemberSubscription.expire_at <= moment,
                        )
                        .limit(batch_size)
                    )
                    .scalars()
                    .all()
                )
                if not ids:
                    break
                session.execute(
                    update(MemberSubscription)
                    .where(
                        MemberSubscription.id.in_(list(ids)),
                        MemberSubscription.status == "active",
                    )
                    .values(
                        status="expired",
                        version=MemberSubscription.version + 1,
                        updated_at=moment,
                    )
                )
                session.commit()
                total += len(ids)
        return total

    # ── 状态机（回调 / 查单补偿唯一入口）───────────────────────────────────

    def _apply_event(
        self,
        *,
        channel: str,
        order_no: str,
        event_id: str,
        status: str,
        amount: Decimal,
        paid_at: datetime,
        provider_txn_id: str | None,
        detail: str,
    ) -> dict[str, Any]:
        now = utcnow()
        with Session(self.db.engine) as session:
            order = self._order_row(session, order_no)
            if order.channel != channel:
                raise AppError(
                    PAY_CHANNEL_MISMATCH,
                    data={"order_no": order_no, "channel": channel},
                )
            if dec(amount) != dec(order.amount):
                raise AppError(
                    PAY_AMOUNT_MISMATCH,
                    data={
                        "order_no": order_no,
                        "expected": _money(order.amount),
                        "actual": _money(amount),
                    },
                )
            previous = session.execute(
                select(PayEvent).where(PayEvent.event_id == event_id)
            ).scalar_one_or_none()
            if previous is not None:
                return _event_out(order, previous, applied=False, duplicate=True)

            before = order.status
            applied, detail_text = self._transition(
                session,
                order=order,
                status=status,
                paid_at=paid_at,
                provider_txn_id=provider_txn_id,
                now=now,
            )
            event = PayEvent(
                order_no=order_no,
                event_id=event_id,
                channel=channel,
                status_before=before,
                status_after=order.status,
                amount=dec(amount),
                signature_ok=True,
                applied=applied,
                detail=f"{detail};{detail_text}"[:255],
                created_at=now,
            )
            session.add(event)
            try:
                session.commit()
            except IntegrityError:
                # 并发同一 event_id / 同一订单订阅：回滚后回读首次结果
                session.rollback()
                order = self._order_row(session, order_no)
                existing = session.execute(
                    select(PayEvent).where(PayEvent.event_id == event_id)
                ).scalar_one_or_none()
                return _event_out(order, existing, applied=False, duplicate=True)
            return _event_out(order, event, applied=applied, duplicate=False)

    def _transition(
        self,
        session: Session,
        *,
        order: PayOrder,
        status: str,
        paid_at: datetime,
        provider_txn_id: str | None,
        now: datetime,
    ) -> tuple[bool, str]:
        """返回 (是否实际迁移, 说明)；非法倒退一律 PAY_STATE_INVALID。"""
        current = order.status
        if current == "paid":
            if status == "paid":
                return False, "duplicate_paid"
            raise AppError(
                PAY_STATE_INVALID,
                data={"order_no": order.order_no, "status": current, "target": status},
            )
        if current in ("cancelled", "expired"):
            raise AppError(
                PAY_STATE_INVALID,
                data={"order_no": order.order_no, "status": current, "target": status},
            )
        if current == "failed":
            if status == "failed":
                return False, "duplicate_failed"
            raise AppError(
                PAY_STATE_INVALID,
                data={"order_no": order.order_no, "status": current, "target": status},
            )
        if current not in ORDER_OPEN_STATUSES:
            raise AppError(
                PAY_STATE_INVALID,
                data={"order_no": order.order_no, "status": current, "target": status},
            )

        values: dict[str, Any] = {
            "status": status,
            "version": PayOrder.version + 1,
            "updated_at": now,
        }
        if status == "paid":
            values["paid_at"] = paid_at
            values["provider_txn_id"] = provider_txn_id
        if status == "failed":
            values["close_reason"] = "provider_failed"
        if status == "paying" and current == "paying":
            return False, "already_paying"

        version_before = int(order.version)
        updated_at = now
        result = cast(
            CursorResult[Any],
            session.execute(
                update(PayOrder)
                .where(
                    PayOrder.id == order.id,
                    PayOrder.status.in_(ORDER_OPEN_STATUSES),
                    PayOrder.version == version_before,
                )
                .values(**values)
                .execution_options(synchronize_session=False)
            ),
        )
        if result.rowcount != 1:
            # 并发回调已推进：回读判断"重复成功"还是"非法倒退"
            latest = session.execute(select(PayOrder).where(PayOrder.id == order.id)).scalar_one()
            if latest.status == "paid" and status == "paid":
                order.status = latest.status
                return False, "concurrent_paid"
            raise AppError(
                PAY_STATE_INVALID,
                data={"order_no": order.order_no, "status": latest.status, "target": status},
            )

        if status == "paid":
            grant_level(
                session,
                user_id=int(order.user_id),
                level_id=int(order.level_id),
                level=int(order.level),
                level_code=order.level_code,
                level_name=order.level_name,
                duration_days=int(order.duration_days),
                source="order",
                order_no=order.order_no,
                now=now,
            )
        # 条件更新已落库；这里只同步内存对象，避免 ORM 在 flush 时再自增一次 version
        order.status = status
        order.version = version_before + 1
        order.updated_at = updated_at
        if status == "paid":
            order.paid_at = paid_at
            order.provider_txn_id = provider_txn_id
        if status == "failed":
            order.close_reason = "provider_failed"
        return True, f"{current}->{status}"

    # ── 内部 ────────────────────────────────────────────────────────────

    def _order_row(
        self, session: Session, order_no: str, *, user_id: int | None = None
    ) -> PayOrder:
        stmt = select(PayOrder).where(PayOrder.order_no == order_no.strip())
        if user_id is not None:
            stmt = stmt.where(PayOrder.user_id == user_id)
        row = session.execute(stmt).scalar_one_or_none()
        if row is None:
            raise AppError(NOT_FOUND, data={"resource": "pay_order", "order_no": order_no})
        return row

    def _level_row(self, session: Session, level_id: int) -> MemberLevel:
        row = session.execute(
            select(MemberLevel).where(MemberLevel.id == level_id, not_deleted(MemberLevel))
        ).scalar_one_or_none()
        if row is None:
            raise AppError(NOT_FOUND, data={"resource": "member_level", "id": level_id})
        return row

    def _user_row(self, session: Session, user_id: int) -> AccountUser:
        row = session.execute(
            select(AccountUser).where(AccountUser.id == user_id, not_deleted(AccountUser))
        ).scalar_one_or_none()
        if row is None:
            raise AppError(NOT_FOUND, data={"resource": "user", "id": user_id})
        return row

    @staticmethod
    def _conflict(resource: str, row_id: int, version: int) -> NoReturn:
        raise AppError(
            CONFLICT_RETRY,
            data={"resource": resource, "id": row_id, "expected_version": version},
        )

    @staticmethod
    def _ensure_level_code_free(
        session: Session, code: str, *, exclude_id: int | None = None
    ) -> None:
        stmt = select(MemberLevel.id).where(not_deleted(MemberLevel), MemberLevel.code == code)
        if exclude_id is not None:
            stmt = stmt.where(MemberLevel.id != exclude_id)
        if session.execute(stmt).first() is not None:
            _fail("档位编码已存在")

    @staticmethod
    def _new_order_no(now: datetime) -> str:
        return f"P{now:%Y%m%d%H%M%S}{secrets.token_hex(8).upper()}"[:32]


__all__ = [
    "SIGNATURE_HEADER",
    "CommerceService",
    "active_level",
    "active_sub",
    "callback_canonical",
    "grant_level",
    "sign_callback",
]
