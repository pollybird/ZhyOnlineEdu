"""会员档位 / 我的会员 / 订单 / 支付回调接口（§10.6 · M2.12）。

权限：
- 档位维护、订阅列表、直接开通、查单补偿：`member:manage`（super_admin / admin）；
- 我的会员、下单、订单详情/取消：登录用户本人（订单越权按 404，不泄露存在性）；
- 回调 `POST /pay/{channel}/notify` 免鉴权，HMAC-SHA256 签名即凭证（`X-ZHY-Pay-Signature`）。

状态机（验收 PAY-STATE-001~004）：
- `paid` 是终态：回调/补偿把 `paid` 改回 `paying` 一律 409 `PAY_STATE_INVALID`；
- 重复 `paid` 事件按 `event_id` 幂等返回（`duplicate=true`），会员只发一次；
- 并发/乱序回调通过 `status IN ('created','paying')` 条件更新，只前进不后退；
- 渠道 / 金额与订单快照不一致分别 422 `PAY_CHANNEL_MISMATCH` / 409 `PAY_AMOUNT_MISMATCH`。
"""

from __future__ import annotations

from typing import Any

from flask import Blueprint, Response, current_app, request

from zhyonlineedu.api.schemas import (
    MemberGrantIn,
    MemberGrantOut,
    MemberLevelCreateIn,
    MemberLevelListOut,
    MemberLevelOut,
    MemberLevelUpdateIn,
    MemberMeOut,
    PayNotifyIn,
    PayNotifyOut,
    PayOrderCreateIn,
    PayOrderListOut,
    PayOrderOut,
    PayReconcileIn,
    SubscriptionListOut,
)
from zhyonlineedu.core.auth.decorators import authenticate_request, require_permission
from zhyonlineedu.core.codes import VALIDATION_FAILED
from zhyonlineedu.core.envelope import ok
from zhyonlineedu.core.errors import AppError
from zhyonlineedu.core.openapi import documented
from zhyonlineedu.modules.commerce.service import SIGNATURE_HEADER, CommerceService

bp = Blueprint("commerce", __name__, url_prefix="/api/v1")

MANAGE = "member:manage"


def _service() -> CommerceService:
    return current_app.extensions["zhy_commerce"]  # type: ignore[no-any-return]


def _payload() -> dict[str, Any]:
    raw = request.get_json(silent=True)
    if raw is None:
        return {}
    if not isinstance(raw, dict):
        raise AppError(VALIDATION_FAILED, data={"errors": ["请求体必须是 JSON 对象"]})
    return raw


# ── 会员档位 ────────────────────────────────────────────────────────────────


@bp.get("/member/levels")
@require_permission(MANAGE)
@documented(
    "会员档位列表",
    description=(
        "按 sort_order 升序；支持 is_enabled=true/false 过滤。停用档位不可下单，"
        "但已购订单按快照继续履约（改价不影响在途订单）。"
    ),
    response=MemberLevelListOut,
    tags=("member",),
)
def list_member_levels() -> tuple[Response, int]:
    return ok(_service().list_levels(request.args))


@bp.post("/member/levels")
@require_permission(MANAGE)
@documented(
    "创建会员档位",
    description=(
        "code 在未删除行内唯一（软删 UK `(code, delete_seq)`）；price 用十进制字符串，"
        "统一 6 位小数入库（Money 18,6，禁止 float）。"
    ),
    request=MemberLevelCreateIn,
    response=MemberLevelOut,
    tags=("member",),
)
def create_member_level() -> tuple[Response, int]:
    return ok(_service().create_level(_payload()), http=201)


@bp.patch("/member/levels/<int:level_id>")
@require_permission(MANAGE)
@documented(
    "更新会员档位",
    description=(
        "必须回传 version（乐观锁，不匹配 CONFLICT_RETRY）；停用用 is_enabled=false。"
        "改价只影响之后的新订单。"
    ),
    request=MemberLevelUpdateIn,
    response=MemberLevelOut,
    tags=("member",),
)
def update_member_level(level_id: int) -> tuple[Response, int]:
    return ok(_service().update_level(level_id, _payload()))


# ── 我的会员 / 管理端订阅 / 直接开通 ──────────────────────────────────────────


@bp.get("/member/me")
@documented(
    "我的会员",
    description=(
        "返回当前有效订阅（等级最高、到期最晚）与全部订阅历史；"
        "active=false 时会员课程播放/测验返回 ACCESS_LEVEL_REQUIRED。"
    ),
    response=MemberMeOut,
    tags=("member",),
)
def my_member() -> tuple[Response, int]:
    return ok(_service().my_member(authenticate_request().id))


@bp.get("/member/subscriptions")
@require_permission(MANAGE)
@documented(
    "会员订阅列表",
    description="管理端按 user_id / status（active|expired|revoked）过滤，倒序分页。",
    response=SubscriptionListOut,
    tags=("member",),
)
def list_member_subscriptions() -> tuple[Response, int]:
    return ok(_service().list_subscriptions(request.args))


@bp.post("/member/grant")
@require_permission(MANAGE)
@documented(
    "直接开通会员",
    description=(
        "管理端赠送：source=grant，不产生订单；duration_days 缺省取档位时长。"
        "已有有效订阅时按最晚到期时间叠加，不吞掉剩余时长。"
    ),
    request=MemberGrantIn,
    response=MemberGrantOut,
    tags=("member",),
    extensions={"x-zhy-idempotent": False},
)
def grant_member() -> tuple[Response, int]:
    return ok(_service().grant_member(_payload()), http=201)


# ── 订单 ────────────────────────────────────────────────────────────────────


@bp.get("/pay/orders")
@documented(
    "我的订单",
    description=(
        "按 id 倒序分页；支持 status 过滤。订单金额/等级/时长均为下单快照，"
        "档位改价不会改变已有订单。"
    ),
    response=PayOrderListOut,
    tags=("pay",),
)
def list_pay_orders() -> tuple[Response, int]:
    return ok(_service().list_orders(request.args, user_id=authenticate_request().id))


@bp.post("/pay/orders")
@documented(
    "创建订单",
    description=(
        "冻结档位快照并生成 order_no；未关闭订单数达到 pay.max_open_orders 返回 "
        "PAY_STATE_INVALID(409)。支付窗口 pay.order_ttl_sec（默认 1800s），"
        "超时由 worker 置 expired。"
    ),
    request=PayOrderCreateIn,
    response=PayOrderOut,
    tags=("pay",),
)
def create_pay_order() -> tuple[Response, int]:
    return ok(_service().create_order(_payload(), user_id=authenticate_request().id), http=201)


@bp.get("/pay/orders/<order_no>")
@documented(
    "订单详情",
    description=("仅本人可见；`member:manage` 可查任意订单。越权一律 404（不泄露订单是否存在）。"),
    response=PayOrderOut,
    tags=("pay",),
)
def get_pay_order(order_no: str) -> tuple[Response, int]:
    user = authenticate_request()
    return ok(
        _service().get_order(order_no, user_id=user.id, can_manage=MANAGE in user.permissions)
    )


@bp.post("/pay/orders/<order_no>/cancel")
@documented(
    "取消订单",
    description=(
        "仅本人可取消；`created/paying → cancelled` 条件更新，重复取消幂等返回。"
        "已支付/已过期/已失败订单取消返回 PAY_STATE_INVALID(409)。"
    ),
    response=PayOrderOut,
    tags=("pay",),
    extensions={"x-zhy-idempotent": True},
)
def cancel_pay_order(order_no: str) -> tuple[Response, int]:
    return ok(_service().cancel_order(order_no, user_id=authenticate_request().id))


@bp.post("/pay/orders/<order_no>/reconcile")
@require_permission(MANAGE)
@documented(
    "查单补偿",
    description=(
        "管理端主动向渠道查单后补记状态；与回调共用同一「不倒退」状态机："
        "paid 不可回退 paying，重复补偿幂等（event_id=reconcile:{order_no}:{status}）。"
    ),
    request=PayReconcileIn,
    response=PayNotifyOut,
    tags=("pay",),
    extensions={"x-zhy-idempotent": True},
)
def reconcile_pay_order(order_no: str) -> tuple[Response, int]:
    return ok(_service().reconcile(order_no, _payload()))


# ── 支付回调（免鉴权）───────────────────────────────────────────────────────


@bp.post("/pay/<channel>/notify")
@documented(
    "支付回调",
    description=(
        "免鉴权，签名即凭证：`X-ZHY-Pay-Signature = HMAC-SHA256(secret, "
        "channel|order_no|event_id|status|amount|paid_at)`。"
        "event_id 唯一幂等；状态只前进不后退（paid 不可回退 paying）；"
        "渠道/金额与订单不一致分别拒绝。"
    ),
    request=PayNotifyIn,
    response=PayNotifyOut,
    tags=("pay",),
    auth=False,
    extensions={"x-zhy-idempotent": True},
)
def pay_notify(channel: str) -> tuple[Response, int]:
    return ok(_service().notify(channel, _payload(), request.headers.get(SIGNATURE_HEADER, "")))
