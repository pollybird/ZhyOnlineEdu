"""M2.12 会员订单（§10.6 · PAY-STATE-001~004）。

验收口径：
1. **状态不倒退** —— `paid` 是终态：回调/查单补偿把 `paid` 改回 `paying`、`failed`
   或取消一律 409 `PAY_STATE_INVALID`，订单状态 / `paid_at` / 金额快照保持不变；
2. **幂等不双发** —— 同一 `event_id` 重复回调返回 200 `duplicate=true` 且 `applied=false`，
   `member_subscription.order_no` 唯一保证同一订单只发一次会员。

另覆盖：档位 CRUD 与乐观锁、下单快照（改价不影响在途订单）、未关闭订单上限、
订单归属（越权 404）、取消幂等、回调签名/渠道/金额/未知单校验、直接开通叠加、
会员课程播放/测验授权、会员到期旧票据 40204、worker 订单与订阅过期。
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import Engine, func, select
from sqlalchemy.orm import Session
from zhyonlineedu.api.schemas import (
    MemberGrantOut,
    MemberLevelListOut,
    MemberLevelOut,
    MemberMeOut,
    PayNotifyOut,
    PayOrderListOut,
    PayOrderOut,
    SubscriptionListOut,
)
from zhyonlineedu.app import create_app
from zhyonlineedu.core.auth.keys import clear_key_cache, ensure_keypair
from zhyonlineedu.core.auth.passwords import hash_password
from zhyonlineedu.core.capability import ProbeResult, ProbeState, StaticProbe
from zhyonlineedu.core.codes import Code
from zhyonlineedu.core.config import Config, load_config
from zhyonlineedu.core.db import compat
from zhyonlineedu.core.db.engine import create_db_engine
from zhyonlineedu.core.db.migration import upgrade_head
from zhyonlineedu.core.db.models.account import AccountUser, SysUserRole
from zhyonlineedu.core.db.models.catalog import (
    EduCourse,
    EduCourseChapter,
    EduCourseLesson,
    EduSubject,
)
from zhyonlineedu.core.db.models.commerce import (
    MemberLevel,
    MemberSubscription,
    PayEvent,
    PayOrder,
    dec,
)
from zhyonlineedu.core.db.models.media import MediaAsset
from zhyonlineedu.core.db.models.quiz import QuizQuestion
from zhyonlineedu.modules.commerce.service import SIGNATURE_HEADER, sign_callback
from zhyonlineedu.workers.expire import expire_once

PASSWORD = "S3cret-pass"  # noqa: S105 - 测试用固定口令
READY = ProbeResult(ProbeState.READY, flavor="sqlite", capabilities={"returning": True})

L_MEMBER = 1  # published/member，资产 encoded（kid=KID），含 1 道题
KID = "c" * 32
PAID_AT = "2026-10-30T00:00:00+00:00"

MANAGE_ENDPOINTS: tuple[tuple[str, str, dict[str, Any] | None], ...] = (
    ("get", "/api/v1/member/levels", None),
    ("post", "/api/v1/member/levels", {"code": "HACK", "name": "越权档位"}),
    ("patch", "/api/v1/member/levels/1", {"version": 1}),
    ("get", "/api/v1/member/subscriptions", None),
    ("post", "/api/v1/member/grant", {"user_id": 1, "level_id": 1}),
    ("post", "/api/v1/pay/orders/UNKNOWN/reconcile", {"status": "paid"}),
)


@pytest.fixture(autouse=True)
def _cleanup() -> Iterator[None]:
    compat.reset()
    clear_key_cache()
    yield
    compat.reset()
    clear_key_cache()


# ── 装配 ──────────────────────────────────────────────────────────────────


def _config(tmp_path: Path, **overrides: str) -> Config:
    environ = {
        "ZHY_ENV": "dev",
        "ZHY_DB_URL": f"sqlite:///{tmp_path / 'commerce.db'}",
        "ZHY_DB_FLAVOR": "sqlite",
        "ZHY_JWT_PRIVATE_KEY_PATH": str(tmp_path / "keys" / "jwt_private.pem"),
        "ZHY_JWT_PUBLIC_KEY_PATH": str(tmp_path / "keys" / "jwt_public.pem"),
        "ZHY_STORAGE_LOCAL_DIR": str(tmp_path / "storage"),
        "ZHY_PROGRESS_RING_DIR": str(tmp_path / "ring"),
    }
    environ.update(overrides)
    return load_config(environ=environ, sys_config={})


def _write_hls(storage: Path, lesson_id: int, kid: str, profiles: tuple[str, ...]) -> None:
    root = storage / "media" / str(lesson_id) / "hls"
    root.mkdir(parents=True, exist_ok=True)
    master = ["#EXTM3U", "#EXT-X-VERSION:3"]
    for profile in profiles:
        master.append("#EXT-X-STREAM-INF:BANDWIDTH=1000000,RESOLUTION=640x360")
        master.append(f"{profile}/index.m3u8")
    (root / "master.m3u8").write_text("\n".join(master) + "\n", encoding="utf-8")
    for profile in profiles:
        directory = root / profile
        directory.mkdir(parents=True, exist_ok=True)
        for name in ("seg_000.ts", "seg_001.ts"):
            (directory / name).write_bytes(b"ts-" + profile.encode() + b"-" + name.encode())
        variant = [
            "#EXTM3U",
            "#EXT-X-VERSION:3",
            "#EXT-X-TARGETDURATION:4",
            "#EXT-X-MEDIA-SEQUENCE:0",
            f'#EXT-X-KEY:METHOD=AES-128,URI="/api/v1/media/key/{kid}"',
            "#EXTINF:4.000000,",
            "seg_000.ts",
            "#EXTINF:4.000000,",
            "seg_001.ts",
            "#EXT-X-ENDLIST",
        ]
        (directory / "index.m3u8").write_text("\n".join(variant) + "\n", encoding="utf-8")


def _seed(tmp_path: Path, **overrides: str) -> Config:
    config = _config(tmp_path, **overrides)
    ensure_keypair(config.get_str("jwt.private_key_path"), config.get_str("jwt.public_key_path"))
    url = config.get_str("db.url")
    upgrade_head(url, "sqlite")
    engine = create_db_engine(url, "sqlite")
    storage = Path(config.get_str("storage.local_dir"))
    try:
        with Session(engine) as session:
            now = datetime.now(UTC)
            for index, (username, role) in enumerate(
                (
                    ("root", "super_admin"),
                    ("admin", "admin"),
                    ("teacher", "teacher"),
                    ("student", "student"),
                    ("other", "student"),
                ),
                start=1,
            ):
                user = AccountUser(
                    org_id=1,
                    user_no=f"U{index:04d}",
                    username=username,
                    password_hash=hash_password(PASSWORD),
                    status="active",
                    token_version=1,
                    version=1,
                    created_at=now,
                    updated_at=now,
                )
                session.add(user)
                session.flush()
                session.add(SysUserRole(user_id=user.id, role_code=role))

            # 档位 1：月卡（默认售卖）；档位 2：年卡（高等级 + 长时长）
            session.add_all(
                [
                    MemberLevel(
                        code="M30",
                        name="月卡",
                        level=1,
                        price=dec("30.000000"),
                        duration_days=30,
                        is_enabled=True,
                        sort_order=1024,
                        version=1,
                        created_at=now,
                        updated_at=now,
                    ),
                    MemberLevel(
                        code="Y365",
                        name="年卡",
                        level=2,
                        price=dec("288.000000"),
                        duration_days=365,
                        is_enabled=True,
                        sort_order=2048,
                        version=1,
                        created_at=now,
                        updated_at=now,
                    ),
                ]
            )

            subject = EduSubject(name="数学", sort_order=1024, is_enabled=True)
            session.add(subject)
            session.flush()
            course = EduCourse(
                subject_id=subject.id,
                title="会员专享课",
                mode="both",
                access_type="member",
                status="published",
                sort_order=1024,
            )
            session.add(course)
            session.flush()
            chapter = EduCourseChapter(course_id=course.id, title="第一章", sort_order=1024)
            session.add(chapter)
            session.flush()
            session.add(
                EduCourseLesson(
                    id=L_MEMBER,
                    chapter_id=chapter.id,
                    course_id=course.id,
                    title="会员第一讲",
                    kind="video",
                    duration_sec=100,
                    is_free=False,
                    sort_order=1024,
                )
            )
            session.add(
                MediaAsset(
                    lesson_id=L_MEMBER,
                    status="encoded",
                    playlist_version=1,
                    duration_sec=100,
                    kid=KID,
                    profiles=json.dumps(["360"]),
                    created_at=now,
                    updated_at=now,
                )
            )
            session.add(
                QuizQuestion(
                    course_id=course.id,
                    lesson_id=L_MEMBER,
                    kind="single",
                    stem="1+1=?",
                    options=json.dumps(
                        [{"key": "A", "text": "2"}, {"key": "B", "text": "3"}],
                        ensure_ascii=False,
                    ),
                    answer=json.dumps(["A"]),
                    explanation=None,
                    score=5,
                    sort_order=1024,
                    is_enabled=True,
                    created_at=now,
                    updated_at=now,
                )
            )
            session.commit()
    finally:
        engine.dispose()

    _write_hls(storage, L_MEMBER, KID, ("360",))
    return config


def _engine(config: Config) -> Engine:
    return create_db_engine(config.get_str("db.url"), "sqlite")


def _client(config: Config) -> Any:
    return create_app(config=config, probe=StaticProbe(READY)).test_client()


def _login(client: Any, username: str) -> dict[str, str]:
    response = client.post("/api/v1/auth/login", json={"username": username, "password": PASSWORD})
    assert response.status_code == 200, response.get_json()
    return {"Authorization": f"Bearer {response.get_json()['data']['access_token']}"}


def _user_id(config: Config, username: str) -> int:
    engine = _engine(config)
    try:
        with Session(engine) as session:
            value = session.execute(
                select(AccountUser.id).where(AccountUser.username == username)
            ).scalar_one()
        return int(value)
    finally:
        engine.dispose()


def _pay_event_count(config: Config, order_no: str) -> int:
    engine = _engine(config)
    try:
        with Session(engine) as session:
            return int(
                session.execute(
                    select(func.count()).select_from(PayEvent).where(PayEvent.order_no == order_no)
                ).scalar_one()
            )
    finally:
        engine.dispose()


def _create_order(
    client: Any, headers: dict[str, str], *, level_id: int = 1, channel: str = "mock"
) -> dict[str, Any]:
    body: dict[str, Any] = {"level_id": level_id}
    if channel != "mock":
        body["channel"] = channel
    response = client.post("/api/v1/pay/orders", json=body, headers=headers)
    assert response.status_code == 201, response.get_json()
    data = response.get_json()["data"]
    assert set(data) == set(PayOrderOut.model_fields)
    return data


def _notify(
    client: Any,
    config: Config,
    *,
    order_no: str,
    event_id: str,
    status: str = "paid",
    amount: str = "30.000000",
    channel: str = "mock",
    paid_at: str | None = PAID_AT,
    signature: str | None = None,
    provider_txn_id: str | None = None,
) -> Any:
    payload: dict[str, Any] = {
        "order_no": order_no,
        "event_id": event_id,
        "status": status,
        "amount": amount,
    }
    if paid_at is not None:
        payload["paid_at"] = paid_at
    if provider_txn_id is not None:
        payload["provider_txn_id"] = provider_txn_id
    if signature is None:
        signature = sign_callback(
            config.get_str("pay.callback_secret"),
            channel=channel,
            order_no=order_no,
            event_id=event_id,
            status=status,
            amount=amount,
            paid_at=paid_at or "",
        )
    return client.post(
        f"/api/v1/pay/{channel}/notify", json=payload, headers={SIGNATURE_HEADER: signature}
    )


def _order(client: Any, headers: dict[str, str], order_no: str) -> dict[str, Any]:
    response = client.get(f"/api/v1/pay/orders/{order_no}", headers=headers)
    assert response.status_code == 200, response.get_json()
    return response.get_json()["data"]


def _me(client: Any, headers: dict[str, str]) -> dict[str, Any]:
    response = client.get("/api/v1/member/me", headers=headers)
    assert response.status_code == 200, response.get_json()
    data = response.get_json()["data"]
    assert set(data) == set(MemberMeOut.model_fields)
    return data


# ── RBAC ──────────────────────────────────────────────────────────────────


@pytest.mark.parametrize("username", ["teacher", "student"])
def test_member_manage_endpoints_deny_non_admin(tmp_path: Path, username: str) -> None:
    client = _client(_seed(tmp_path))
    headers = _login(client, username)
    for method, path, body in MANAGE_ENDPOINTS:
        response = client.open(path, method=method.upper(), json=body, headers=headers)
        assert response.status_code == 403, f"{method} {path} -> {response.get_json()}"
        assert response.get_json()["code"] == "PERMISSION_DENIED"
        assert response.get_json()["data"]["required"] == "member:manage"


def test_admin_role_can_manage_members(tmp_path: Path) -> None:
    client = _client(_seed(tmp_path))
    headers = _login(client, "admin")
    listed = client.get("/api/v1/member/levels", headers=headers)
    assert listed.status_code == 200, listed.get_json()
    assert listed.get_json()["data"]["total"] == 2


def test_player_endpoints_require_login(tmp_path: Path) -> None:
    client = _client(_seed(tmp_path))
    for method, path, body in (
        ("get", "/api/v1/member/me", None),
        ("get", "/api/v1/pay/orders", None),
        ("post", "/api/v1/pay/orders", {"level_id": 1}),
    ):
        response = client.open(path, method=method.upper(), json=body)
        assert response.status_code == 401
        assert response.get_json()["code"] == "AUTH_TOKEN_EXPIRED"


# ── 档位 CRUD / 乐观锁 ────────────────────────────────────────────────────


def test_member_level_crud_and_optimistic_lock(tmp_path: Path) -> None:
    client = _client(_seed(tmp_path))
    headers = _login(client, "root")

    listed = client.get("/api/v1/member/levels", headers=headers)
    assert listed.status_code == 200, listed.get_json()
    body = listed.get_json()["data"]
    assert set(body) == set(MemberLevelListOut.model_fields)
    assert body["total"] == 2
    assert [item["code"] for item in body["items"]] == ["M30", "Y365"]
    assert body["items"][0]["price"] == "30.000000"
    assert set(body["items"][0]) == set(MemberLevelOut.model_fields)

    empty = client.get("/api/v1/member/levels?is_enabled=false", headers=headers)
    assert empty.get_json()["data"]["total"] == 0
    bad_filter = client.get("/api/v1/member/levels?is_enabled=yes", headers=headers)
    assert bad_filter.status_code == 422

    created = client.post(
        "/api/v1/member/levels",
        json={"code": "Q90", "name": "季卡", "price": "88.500000", "duration_days": 90},
        headers=headers,
    )
    assert created.status_code == 201, created.get_json()
    data = created.get_json()["data"]
    assert set(data) == set(MemberLevelOut.model_fields)
    assert data["code"] == "Q90" and data["price"] == "88.500000"
    assert data["sort_order"] == 3072 and data["version"] == 1

    duplicate = client.post(
        "/api/v1/member/levels", json={"code": "M30", "name": "重复"}, headers=headers
    )
    assert duplicate.status_code == 422
    assert duplicate.get_json()["code"] == "VALIDATION_FAILED"
    assert "档位编码已存在" in duplicate.get_json()["data"]["errors"]

    bad_price = client.post(
        "/api/v1/member/levels",
        json={"code": "BAD", "name": "坏价格", "price": "abc"},
        headers=headers,
    )
    assert bad_price.status_code == 422

    patched = client.patch(
        "/api/v1/member/levels/1",
        json={"version": 1, "price": "45.000000", "name": "月卡（调价）"},
        headers=headers,
    )
    assert patched.status_code == 200, patched.get_json()
    updated = patched.get_json()["data"]
    assert updated["price"] == "45.000000"
    assert updated["name"] == "月卡（调价）"
    assert updated["version"] == 2

    stale = client.patch(
        "/api/v1/member/levels/1", json={"version": 1, "price": "50.000000"}, headers=headers
    )
    assert stale.status_code == 409
    assert stale.get_json()["code"] == "CONFLICT_RETRY"

    noop = client.patch("/api/v1/member/levels/1", json={"version": 2}, headers=headers)
    assert noop.status_code == 422
    assert noop.get_json()["code"] == "VALIDATION_FAILED"

    missing = client.patch("/api/v1/member/levels/9999", json={"version": 1}, headers=headers)
    assert missing.status_code == 404


# ── 下单 / 快照 / 上限 ────────────────────────────────────────────────────


def test_order_snapshot_and_open_order_limit(tmp_path: Path) -> None:
    config = _seed(tmp_path)
    client = _client(config)
    headers = _login(client, "student")

    first = _create_order(client, headers)
    assert first["status"] == "created"
    assert first["amount"] == "30.000000"
    assert first["level"] == 1 and first["level_code"] == "M30"
    assert first["duration_days"] == 30 and first["currency"] == "CNY"
    assert first["channel"] == "mock" and first["version"] == 1
    assert first["paid_at"] is None and first["order_no"].startswith("P")

    # 改价只影响之后的订单：在途订单金额仍是下单快照
    patched = client.patch(
        "/api/v1/member/levels/1", json={"version": 1, "price": "45.000000"}, headers=headers
    )
    assert patched.status_code == 403  # 学员无 member:manage
    root = _login(client, "root")
    assert (
        client.patch(
            "/api/v1/member/levels/1", json={"version": 1, "price": "45.000000"}, headers=root
        ).status_code
        == 200
    )
    assert _order(client, headers, first["order_no"])["amount"] == "30.000000"
    second = _create_order(client, headers)
    assert second["amount"] == "45.000000"

    # 未关闭订单上限：默认 3
    third = _create_order(client, headers)
    blocked = client.post("/api/v1/pay/orders", json={"level_id": 1}, headers=headers)
    assert blocked.status_code == 409, blocked.get_json()
    assert blocked.get_json()["code"] == "PAY_STATE_INVALID"
    assert blocked.get_json()["data"]["reason"] == "too_many_open_orders"
    assert blocked.get_json()["data"]["max_open_orders"] == 3

    # 关闭一单后可继续下单
    cancelled = client.post(f"/api/v1/pay/orders/{second['order_no']}/cancel", headers=headers)
    assert cancelled.status_code == 200
    assert cancelled.get_json()["data"]["status"] == "cancelled"
    again = _create_order(client, headers)
    assert again["status"] == "created"
    assert third["version"] == 1


def test_order_validation_and_disabled_level(tmp_path: Path) -> None:
    config = _seed(tmp_path)
    client = _client(config)
    headers = _login(client, "student")

    assert (
        client.post(
            "/api/v1/pay/orders", json={"level_id": 1, "channel": "paypal"}, headers=headers
        ).status_code
        == 422
    )
    for body in ({}, {"level_id": 0}, {"level_id": True}):
        response = client.post("/api/v1/pay/orders", json=body, headers=headers)
        assert response.status_code == 422, body
        assert response.get_json()["code"] == "VALIDATION_FAILED"
    missing = client.post("/api/v1/pay/orders", json={"level_id": 999}, headers=headers)
    assert missing.status_code == 404
    assert missing.get_json()["code"] == "NOT_FOUND"

    root = _login(client, "root")
    disabled = client.patch(
        "/api/v1/member/levels/1", json={"version": 1, "is_enabled": False}, headers=root
    )
    assert disabled.status_code == 200
    blocked = client.post("/api/v1/pay/orders", json={"level_id": 1}, headers=headers)
    assert blocked.status_code == 409
    assert blocked.get_json()["code"] == "PAY_STATE_INVALID"
    assert blocked.get_json()["data"]["reason"] == "disabled"
    grant = client.post(
        "/api/v1/member/grant",
        json={"user_id": _user_id(config, "student"), "level_id": 1},
        headers=root,
    )
    assert grant.status_code == 409
    assert grant.get_json()["code"] == "PAY_STATE_INVALID"
    # 其他启用档位不受影响
    assert _create_order(client, headers, level_id=2)["level"] == 2


# ── 订单列表 / 归属 / 取消 ────────────────────────────────────────────────


def test_order_list_and_ownership(tmp_path: Path) -> None:
    config = _seed(tmp_path)
    client = _client(config)
    student = _login(client, "student")
    other = _login(client, "other")
    root = _login(client, "root")

    first = _create_order(client, student)
    second = _create_order(client, student)
    client.post(f"/api/v1/pay/orders/{first['order_no']}/cancel", headers=student)

    listed = client.get("/api/v1/pay/orders", headers=student)
    body = listed.get_json()["data"]
    assert set(body) == set(PayOrderListOut.model_fields)
    assert body["total"] == 2
    assert [item["order_no"] for item in body["items"]] == [
        second["order_no"],
        first["order_no"],
    ]
    filtered = client.get("/api/v1/pay/orders?status=cancelled", headers=student)
    assert filtered.get_json()["data"]["total"] == 1
    bad_status = client.get("/api/v1/pay/orders?status=ghost", headers=student)
    assert bad_status.status_code == 422

    assert client.get("/api/v1/pay/orders", headers=other).get_json()["data"]["total"] == 0
    denied = client.get(f"/api/v1/pay/orders/{first['order_no']}", headers=other)
    assert denied.status_code == 404
    assert denied.get_json()["code"] == "NOT_FOUND"
    assert _order(client, root, first["order_no"])["order_no"] == first["order_no"]
    assert client.get("/api/v1/pay/orders/UNKNOWN", headers=student).status_code == 404


def test_cancel_order_idempotent_and_paid_rejected(tmp_path: Path) -> None:
    config = _seed(tmp_path)
    client = _client(config)
    student = _login(client, "student")
    other = _login(client, "other")

    order = _create_order(client, student)
    first = client.post(f"/api/v1/pay/orders/{order['order_no']}/cancel", headers=student)
    assert first.status_code == 200
    data = first.get_json()["data"]
    assert data["status"] == "cancelled"
    assert data["close_reason"] == "user_cancelled"
    assert data["version"] == 2

    replay = client.post(f"/api/v1/pay/orders/{order['order_no']}/cancel", headers=student)
    assert replay.status_code == 200
    assert replay.get_json()["data"]["status"] == "cancelled"
    assert replay.get_json()["data"]["version"] == 2

    denied = client.post(f"/api/v1/pay/orders/{order['order_no']}/cancel", headers=other)
    assert denied.status_code == 404

    paid_order = _create_order(client, student)
    assert (
        _notify(
            client, config, order_no=paid_order["order_no"], event_id="evt-paid-cancel"
        ).status_code
        == 200
    )
    rejected = client.post(f"/api/v1/pay/orders/{paid_order['order_no']}/cancel", headers=student)
    assert rejected.status_code == 409
    assert rejected.get_json()["code"] == "PAY_STATE_INVALID"
    assert rejected.get_json()["data"]["status"] == "paid"

    expired = _create_order(client, student)
    expire_once(config, now=datetime.now(UTC) + timedelta(seconds=1801))
    blocked = client.post(f"/api/v1/pay/orders/{expired['order_no']}/cancel", headers=student)
    assert blocked.status_code == 409


# ── 回调校验 ──────────────────────────────────────────────────────────────


def test_notify_rejects_signature_channel_amount_and_unknown(tmp_path: Path) -> None:
    config = _seed(tmp_path)
    client = _client(config)
    student = _login(client, "student")
    order = _create_order(client, student, channel="wechat")
    amount = order["amount"]

    missing = client.post(
        "/api/v1/pay/wechat/notify",
        json={"order_no": order["order_no"], "event_id": "e1", "status": "paid", "amount": amount},
    )
    assert missing.status_code == 401
    assert missing.get_json()["code"] == "PAY_SIGNATURE_INVALID"
    assert missing.get_json()["data"]["reason"] == "missing_signature"

    forged = _notify(
        client,
        config,
        order_no=order["order_no"],
        event_id="e2",
        amount=amount,
        channel="wechat",
        signature="0" * 64,
    )
    assert forged.status_code == 401
    assert forged.get_json()["data"]["reason"] == "signature_mismatch"

    wrong_channel = _notify(
        client, config, order_no=order["order_no"], event_id="e3", amount=amount, channel="mock"
    )
    assert wrong_channel.status_code == 422
    assert wrong_channel.get_json()["code"] == "PAY_CHANNEL_MISMATCH"

    wrong_amount = _notify(
        client,
        config,
        order_no=order["order_no"],
        event_id="e4",
        amount="29.000000",
        channel="wechat",
    )
    assert wrong_amount.status_code == 409
    assert wrong_amount.get_json()["code"] == "PAY_AMOUNT_MISMATCH"
    assert wrong_amount.get_json()["data"]["expected"] == "30.000000"
    assert wrong_amount.get_json()["data"]["actual"] == "29.000000"

    unknown = _notify(client, config, order_no="P-UNKNOWN", event_id="e5", channel="wechat")
    assert unknown.status_code == 404

    bad_status = _notify(
        client, config, order_no=order["order_no"], event_id="e6", status="done", channel="wechat"
    )
    assert bad_status.status_code == 422
    assert bad_status.get_json()["code"] == "VALIDATION_FAILED"

    bad_channel_path = client.post(
        "/api/v1/pay/paypal/notify",
        json={"order_no": order["order_no"], "event_id": "e7", "status": "paid", "amount": amount},
    )
    assert bad_channel_path.status_code == 422

    # 全部拒绝后订单保持 created，且没有任何事件落库
    assert _order(client, student, order["order_no"])["status"] == "created"
    assert _pay_event_count(config, order["order_no"]) == 0


# ── 验收口径：状态不倒退（PAY-STATE-001~004）──────────────────────────────


def test_pay_state_001_paid_is_terminal(tmp_path: Path) -> None:
    """PAY-STATE-001：paid 不可回退 paying / failed，订单与会员保持首次结果。"""
    config = _seed(tmp_path)
    client = _client(config)
    student = _login(client, "student")
    order = _create_order(client, student)

    paid = _notify(
        client,
        config,
        order_no=order["order_no"],
        event_id="evt-paid-1",
        provider_txn_id="TXN-1",
    )
    assert paid.status_code == 200, paid.get_json()
    body = paid.get_json()["data"]
    assert set(body) == set(PayNotifyOut.model_fields)
    assert body["status"] == "paid" and body["applied"] is True
    assert body["duplicate"] is False

    snapshot = _order(client, student, order["order_no"])
    assert snapshot["status"] == "paid"
    assert snapshot["version"] == 2
    assert snapshot["paid_at"] is not None
    assert snapshot["provider_txn_id"] == "TXN-1"
    assert snapshot["amount"] == "30.000000"

    regress_paying = _notify(
        client, config, order_no=order["order_no"], event_id="evt-regress-paying", status="paying"
    )
    assert regress_paying.status_code == 409
    assert regress_paying.get_json()["code"] == "PAY_STATE_INVALID"
    assert regress_paying.get_json()["data"]["status"] == "paid"
    assert regress_paying.get_json()["data"]["target"] == "paying"

    regress_failed = _notify(
        client, config, order_no=order["order_no"], event_id="evt-regress-failed", status="failed"
    )
    assert regress_failed.status_code == 409
    assert regress_failed.get_json()["code"] == "PAY_STATE_INVALID"

    after = _order(client, student, order["order_no"])
    assert after["status"] == "paid"
    assert after["paid_at"] == snapshot["paid_at"]
    assert after["version"] == snapshot["version"]
    assert _pay_event_count(config, order["order_no"]) == 1

    member = _me(client, student)
    assert member["active"] is True
    assert len(member["subscriptions"]) == 1


def test_pay_state_002_duplicate_event_is_idempotent(tmp_path: Path) -> None:
    """PAY-STATE-002：同 event_id 重放 200 且 duplicate=true，会员只发一次。"""
    config = _seed(tmp_path)
    client = _client(config)
    student = _login(client, "student")
    order = _create_order(client, student)

    first = _notify(client, config, order_no=order["order_no"], event_id="evt-dup")
    assert first.status_code == 200
    assert first.get_json()["data"]["applied"] is True

    replay = _notify(client, config, order_no=order["order_no"], event_id="evt-dup")
    assert replay.status_code == 200, replay.get_json()
    body = replay.get_json()["data"]
    assert body["duplicate"] is True
    assert body["applied"] is False
    assert body["event_id"] == "evt-dup"
    assert body["status"] == "paid"

    # 不同 event_id 的二次成功回调：状态已 paid，按"重复成功"幂等，不再发会员
    second_event = _notify(client, config, order_no=order["order_no"], event_id="evt-dup-2")
    assert second_event.status_code == 200
    assert second_event.get_json()["data"]["applied"] is False
    assert second_event.get_json()["data"]["status"] == "paid"

    member = _me(client, student)
    assert len(member["subscriptions"]) == 1
    assert member["subscriptions"][0]["order_no"] == order["order_no"]
    assert member["subscriptions"][0]["source"] == "order"
    assert _pay_event_count(config, order["order_no"]) == 2


def test_pay_state_003_only_forward_transitions(tmp_path: Path) -> None:
    """PAY-STATE-003：created → paying → paid 只前进；paid 后 failed/取消均拒绝。"""
    config = _seed(tmp_path)
    client = _client(config)
    student = _login(client, "student")
    order = _create_order(client, student)

    paying = _notify(
        client, config, order_no=order["order_no"], event_id="evt-paying", status="paying"
    )
    assert paying.status_code == 200
    assert paying.get_json()["data"]["applied"] is True
    assert paying.get_json()["data"]["status"] == "paying"

    # 重复 paying：状态不变但事件仍记录（applied=false）
    again = _notify(
        client, config, order_no=order["order_no"], event_id="evt-paying-2", status="paying"
    )
    assert again.status_code == 200
    assert again.get_json()["data"]["applied"] is False
    assert again.get_json()["data"]["status"] == "paying"

    paid = _notify(client, config, order_no=order["order_no"], event_id="evt-paid", status="paid")
    assert paid.status_code == 200
    assert paid.get_json()["data"]["applied"] is True
    assert _order(client, student, order["order_no"])["status"] == "paid"

    late_failed = _notify(
        client, config, order_no=order["order_no"], event_id="evt-late-failed", status="failed"
    )
    assert late_failed.status_code == 409
    assert late_failed.get_json()["code"] == "PAY_STATE_INVALID"

    late_cancel = client.post(f"/api/v1/pay/orders/{order['order_no']}/cancel", headers=student)
    assert late_cancel.status_code == 409
    assert late_cancel.get_json()["code"] == "PAY_STATE_INVALID"


def test_pay_state_004_reconcile_shares_state_machine(tmp_path: Path) -> None:
    """PAY-STATE-004：查单补偿与回调共用同一「不倒退」状态机，重复补偿幂等。"""
    config = _seed(tmp_path)
    client = _client(config)
    student = _login(client, "student")
    root = _login(client, "root")
    order = _create_order(client, student)

    first = client.post(
        f"/api/v1/pay/orders/{order['order_no']}/reconcile", json={"status": "paid"}, headers=root
    )
    assert first.status_code == 200, first.get_json()
    body = first.get_json()["data"]
    assert body["status"] == "paid" and body["applied"] is True
    assert body["duplicate"] is False
    assert body["event_id"] == f"reconcile:{order['order_no']}:paid"

    replay = client.post(
        f"/api/v1/pay/orders/{order['order_no']}/reconcile", json={"status": "paid"}, headers=root
    )
    assert replay.status_code == 200
    assert replay.get_json()["data"]["duplicate"] is True
    assert replay.get_json()["data"]["applied"] is False

    regress = client.post(
        f"/api/v1/pay/orders/{order['order_no']}/reconcile",
        json={"status": "paying"},
        headers=root,
    )
    assert regress.status_code == 409
    assert regress.get_json()["code"] == "PAY_STATE_INVALID"

    failed = client.post(
        f"/api/v1/pay/orders/{order['order_no']}/reconcile",
        json={"status": "failed"},
        headers=root,
    )
    assert failed.status_code == 409

    # 回调同样不能把 paid 回退
    callback = _notify(
        client, config, order_no=order["order_no"], event_id="evt-after-reconcile", status="paying"
    )
    assert callback.status_code == 409

    assert _order(client, student, order["order_no"])["status"] == "paid"
    assert len(_me(client, student)["subscriptions"]) == 1

    # 已关闭订单不能补成 paid（过期/取消都是终态）
    closed = _create_order(client, student)
    client.post(f"/api/v1/pay/orders/{closed['order_no']}/cancel", headers=student)
    rejected = client.post(
        f"/api/v1/pay/orders/{closed['order_no']}/reconcile", json={"status": "paid"}, headers=root
    )
    assert rejected.status_code == 409
    assert rejected.get_json()["code"] == "PAY_STATE_INVALID"

    # 金额与快照不一致的补偿同样拒绝
    fresh = _create_order(client, student)
    mismatch = client.post(
        f"/api/v1/pay/orders/{fresh['order_no']}/reconcile",
        json={"status": "paid", "amount": "29.000000"},
        headers=root,
    )
    assert mismatch.status_code == 409
    assert mismatch.get_json()["code"] == "PAY_AMOUNT_MISMATCH"
    assert _order(client, student, fresh["order_no"])["status"] == "created"


# ── 直接开通 / 订阅叠加 ────────────────────────────────────────────────────


def test_grant_member_stacks_and_lists(tmp_path: Path) -> None:
    config = _seed(tmp_path)
    client = _client(config)
    root = _login(client, "root")
    student = _login(client, "student")
    user_id = _user_id(config, "student")

    me = _me(client, student)
    assert me["active"] is False
    assert me["level"] is None and me["expire_at"] is None
    assert me["subscriptions"] == []

    first = client.post(
        "/api/v1/member/grant", json={"user_id": user_id, "level_id": 1}, headers=root
    )
    assert first.status_code == 201, first.get_json()
    body = first.get_json()["data"]
    assert set(body) == set(MemberGrantOut.model_fields)
    sub = body["subscription"]
    assert sub["source"] == "grant" and sub["order_no"] is None
    assert sub["status"] == "active" and sub["level_code"] == "M30"
    assert body["member"]["active"] is True
    first_expire = datetime.fromisoformat(sub["expire_at"])

    second = client.post(
        "/api/v1/member/grant", json={"user_id": user_id, "level_id": 1}, headers=root
    )
    assert second.status_code == 201
    second_expire = datetime.fromisoformat(second.get_json()["data"]["subscription"]["expire_at"])
    # 叠加：第二次从第一次到期时间继续累加，不吞掉剩余时长
    assert second_expire >= first_expire + timedelta(days=29)

    listed = client.get(f"/api/v1/member/subscriptions?user_id={user_id}", headers=root)
    assert listed.status_code == 200
    data = listed.get_json()["data"]
    assert set(data) == set(SubscriptionListOut.model_fields)
    assert data["total"] == 2 and data["page"] == 1 and data["page_size"] == 20
    active_only = client.get(
        f"/api/v1/member/subscriptions?user_id={user_id}&status=active", headers=root
    )
    assert active_only.get_json()["data"]["total"] == 2
    bad_status = client.get("/api/v1/member/subscriptions?status=ghost", headers=root)
    assert bad_status.status_code == 422

    missing_user = client.post(
        "/api/v1/member/grant", json={"user_id": 9999, "level_id": 1}, headers=root
    )
    assert missing_user.status_code == 404
    missing_level = client.post(
        "/api/v1/member/grant", json={"user_id": user_id, "level_id": 999}, headers=root
    )
    assert missing_level.status_code == 404
    bad_duration = client.post(
        "/api/v1/member/grant",
        json={"user_id": user_id, "level_id": 2, "duration_days": 0},
        headers=root,
    )
    assert bad_duration.status_code == 422

    # 自定义时长与高等级档位
    custom = client.post(
        "/api/v1/member/grant",
        json={"user_id": user_id, "level_id": 2, "duration_days": 7},
        headers=root,
    )
    assert custom.status_code == 201
    custom_sub = custom.get_json()["data"]["subscription"]
    assert custom_sub["level"] == 2 and custom_sub["level_code"] == "Y365"
    expire = datetime.fromisoformat(custom_sub["expire_at"])
    start = datetime.fromisoformat(custom_sub["start_at"])
    assert (expire - start) >= timedelta(days=30)  # 从既有到期时间叠加 7 天

    denied = client.post(
        "/api/v1/member/grant", json={"user_id": user_id, "level_id": 2}, headers=student
    )
    assert denied.status_code == 403

    disabled = client.patch(
        "/api/v1/member/levels/1", json={"version": 1, "is_enabled": False}, headers=root
    )
    assert disabled.status_code == 200
    blocked = client.post(
        "/api/v1/member/grant", json={"user_id": user_id, "level_id": 1}, headers=root
    )
    assert blocked.status_code == 409
    assert blocked.get_json()["code"] == "PAY_STATE_INVALID"


# ── 会员课程授权 ──────────────────────────────────────────────────────────


def test_member_course_access_follows_subscription(tmp_path: Path) -> None:
    config = _seed(tmp_path)
    client = _client(config)
    student = _login(client, "student")
    root = _login(client, "root")

    denied_ticket = client.post(
        "/api/v1/media/tickets", json={"lesson_id": L_MEMBER}, headers=student
    )
    assert denied_ticket.status_code == 403
    assert denied_ticket.get_json()["code"] == "ACCESS_LEVEL_REQUIRED"
    denied_quiz = client.get(f"/api/v1/lessons/{L_MEMBER}/quiz", headers=student)
    assert denied_quiz.status_code == 403
    assert denied_quiz.get_json()["code"] == "ACCESS_LEVEL_REQUIRED"

    grant = client.post(
        "/api/v1/member/grant",
        json={"user_id": _user_id(config, "student"), "level_id": 1},
        headers=root,
    )
    assert grant.status_code == 201

    ticket = client.post("/api/v1/media/tickets", json={"lesson_id": L_MEMBER}, headers=student)
    assert ticket.status_code == 200, ticket.get_json()
    ticket_data = ticket.get_json()["data"]
    assert ticket_data["preview"] is False
    quiz = client.get(f"/api/v1/lessons/{L_MEMBER}/quiz", headers=student)
    assert quiz.status_code == 200
    assert quiz.get_json()["data"]["question_count"] == 1

    # 会员到期：worker 置 expired，旧票据授权指纹失配 → 40204
    expire_once(config, now=datetime.now(UTC) + timedelta(days=31))
    stale = client.get(str(ticket_data["playlist_url"]))
    assert stale.status_code == 403
    assert stale.get_json()["code"] == int(Code.MEDIA_ACCESS_REVOKED)
    assert stale.get_json()["data"]["reason"] == "policy_changed"

    retry = client.post("/api/v1/media/tickets", json={"lesson_id": L_MEMBER}, headers=student)
    assert retry.status_code == 403
    assert retry.get_json()["code"] == "ACCESS_LEVEL_REQUIRED"
    assert client.get(f"/api/v1/lessons/{L_MEMBER}/quiz", headers=student).status_code == 403

    me = _me(client, student)
    assert me["active"] is False
    assert me["subscriptions"][0]["status"] == "expired"


# ── worker 过期 ───────────────────────────────────────────────────────────


def test_worker_expires_orders_and_memberships(tmp_path: Path) -> None:
    config = _seed(tmp_path, ZHY_PAY_ORDER_TTL_SEC="60")
    client = _client(config)
    student = _login(client, "student")

    order = _create_order(client, student)
    expire_once(config, now=datetime.now(UTC) + timedelta(seconds=120))
    expired = _order(client, student, order["order_no"])
    assert expired["status"] == "expired"
    assert expired["close_reason"] == "timeout"

    late_paid = _notify(
        client, config, order_no=order["order_no"], event_id="evt-late-paid", status="paid"
    )
    assert late_paid.status_code == 409
    assert late_paid.get_json()["code"] == "PAY_STATE_INVALID"

    # 已支付订单不会被 worker 倒退；订阅到期后才置 expired
    paid_order = _create_order(client, student)
    assert (
        _notify(
            client, config, order_no=paid_order["order_no"], event_id="evt-worker-paid"
        ).status_code
        == 200
    )
    assert _order(client, student, paid_order["order_no"])["status"] == "paid"

    expire_once(config, now=datetime.now(UTC) + timedelta(days=31))
    after = _order(client, student, paid_order["order_no"])
    assert after["status"] == "paid"
    assert after["paid_at"] is not None

    me = _me(client, student)
    assert me["active"] is False
    assert me["subscriptions"][0]["status"] == "expired"
    # 已支付订单的订阅快照保持不变，可继续对账
    engine = _engine(config)
    try:
        with Session(engine) as session:
            row = session.execute(
                select(MemberSubscription).where(
                    MemberSubscription.order_no == paid_order["order_no"]
                )
            ).scalar_one()
            assert row.status == "expired"
            assert int(row.version) >= 2
            order_row = session.execute(
                select(PayOrder).where(PayOrder.order_no == paid_order["order_no"])
            ).scalar_one()
            assert order_row.status == "paid"
            assert dec(order_row.amount) == dec("30.000000")
    finally:
        engine.dispose()
