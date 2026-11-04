"""M3.7 事件总线 + 参考插件（§11 · 附录 E）。

契约口径（EVENT-001~009）：
001 观察者语义：订阅方只能读 payload（只读映射），不能改写主流程结果；
002 隔离：单个订阅抛异常/超时不影响其它订阅，也不影响调用方（`EmitReport` 记账）；
003 白名单：未登记事件 / 非法 timeout 一律 `VALIDATION_FAILED`；
004 生命周期：`unsubscribe_plugin` 按 code 摘除订阅（禁用/卸载/加载失败回滚）；
005 SDK：仅 `sdk.events.subscribe`（无 `emit`），需 `event:subscribe` 能力与插件作用域；
006 作用域重绑：回调执行时重新绑定插件作用域，回调内可用 `sdk.store`；
007 派发点：`ProgressFlush` 仅在「未完成 → 已完成」跃迁、事务提交后发 `lesson.finished`；
008 幂等：同一用户同一课时重复 flush 不重复派发；试看票来源不产生事件项目；
009 参考插件：安装 → 启用 → 订阅生效 → 证书幂等写入 → 禁用摘订阅 → 卸载删表。
"""

from __future__ import annotations

import shutil
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from flask import Flask
from sqlalchemy import Engine, MetaData, Table, inspect, select
from sqlalchemy.orm import Session
from zhyonlineedu.app import create_app
from zhyonlineedu.core import sdk
from zhyonlineedu.core.auth.keys import clear_key_cache, ensure_keypair
from zhyonlineedu.core.auth.passwords import hash_password
from zhyonlineedu.core.capability import ProbeResult, ProbeState, StaticProbe
from zhyonlineedu.core.codes import PLUGIN_CAP_DENIED, VALIDATION_FAILED
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
from zhyonlineedu.core.db.models.learn import utcnow
from zhyonlineedu.core.errors import AppError
from zhyonlineedu.core.events import (
    ALLOWED_EVENTS,
    EVENT_LESSON_FINISHED,
    EventBus,
    get_bus,
    validate_event_name,
)
from zhyonlineedu.core.progress.flush import ProgressFlush
from zhyonlineedu.core.progress.gateway import ProgressGateway
from zhyonlineedu.core.sdk.context import reset as reset_scope
from zhyonlineedu.core.sdk.context import using

PASSWORD = "S3cret-pass"  # noqa: S105 - 测试用固定口令
READY = ProbeResult(ProbeState.READY, flavor="sqlite", capabilities={"returning": True})
REF_CODE = "reference_certificate"
SAMPLE_CODE = "sample_hello"
LESSON_ID = 71
DURATION = 100
CERT_TABLE = "plg_reference_certificate_cert"


@pytest.fixture(autouse=True)
def _cleanup() -> Iterator[None]:
    reset_scope()
    compat.reset()
    clear_key_cache()
    yield
    reset_scope()
    compat.reset()
    clear_key_cache()


# ── 装配 ──────────────────────────────────────────────────────────────────


def _config(tmp_path: Path, plugin_root: Path | None = None) -> Config:
    environ = {
        "ZHY_ENV": "dev",
        "ZHY_DB_URL": f"sqlite:///{tmp_path / 'events.db'}",
        "ZHY_DB_FLAVOR": "sqlite",
        "ZHY_JWT_PRIVATE_KEY_PATH": str(tmp_path / "keys" / "jwt_private.pem"),
        "ZHY_JWT_PUBLIC_KEY_PATH": str(tmp_path / "keys" / "jwt_public.pem"),
        "ZHY_STORAGE_LOCAL_DIR": str(tmp_path / "storage"),
        "ZHY_PROGRESS_RING_DIR": str(tmp_path / "ring"),
        "ZHY_PLUGIN_DIR": str(plugin_root or tmp_path / "plugins"),
    }
    return load_config(environ=environ, sys_config={})


def _seed(tmp_path: Path, plugin_root: Path | None = None) -> tuple[Config, int, int]:
    """返回 (config, student_id, course_id)；课时固定 LESSON_ID。"""
    config = _config(tmp_path, plugin_root)
    ensure_keypair(config.get_str("jwt.private_key_path"), config.get_str("jwt.public_key_path"))
    url = config.get_str("db.url")
    upgrade_head(url, "sqlite")
    compat.configure(flavor="sqlite")
    engine = create_db_engine(url, "sqlite")
    student_id = 0
    course_id = 0
    try:
        with Session(engine) as session:
            now = utcnow()
            for username, role in (("root", "super_admin"), ("student", "student")):
                user = AccountUser(
                    username=username,
                    password_hash=hash_password(PASSWORD),
                    status="active",
                    token_version=1,
                    created_at=now,
                    updated_at=now,
                )
                session.add(user)
                session.flush()
                session.add(SysUserRole(user_id=user.id, role_code=role))
                if username == "student":
                    student_id = int(user.id)
            subject = EduSubject(name="数学", sort_order=1024, is_enabled=True)
            session.add(subject)
            session.flush()
            course = EduCourse(
                subject_id=subject.id,
                title="证书课",
                mode="both",
                access_type="public",
                status="published",
                sort_order=1024,
            )
            session.add(course)
            session.flush()
            course_id = int(course.id)
            chapter = EduCourseChapter(course_id=course.id, title="第一章", sort_order=1024)
            session.add(chapter)
            session.flush()
            session.add(
                EduCourseLesson(
                    id=LESSON_ID,
                    chapter_id=chapter.id,
                    course_id=course.id,
                    title="第一节",
                    kind="video",
                    duration_sec=DURATION,
                    is_free=False,
                    sort_order=1024,
                )
            )
            session.commit()
    finally:
        engine.dispose()
    return config, student_id, course_id


def _engine(config: Config) -> Engine:
    return create_db_engine(config.get_str("db.url"), "sqlite")


def _client(config: Config) -> Any:
    return create_app(config=config, probe=StaticProbe(READY)).test_client()


def _login(client: Any, username: str) -> dict[str, str]:
    response = client.post("/api/v1/auth/login", json={"username": username, "password": PASSWORD})
    assert response.status_code == 200, response.get_json()
    return {"Authorization": f"Bearer {response.get_json()['data']['access_token']}"}


def _record(config: Config, *, student_id: int, position: int, played: int) -> None:
    ProgressGateway(config).record(
        user_id=student_id,
        lesson_id=LESSON_ID,
        body={
            "lesson_id": LESSON_ID,
            "position_sec": position,
            "played_seconds": played,
            "speed": 1.0,
            "client_ts_ms": 0,
        },
        can_preview=False,
    )


def _cert_rows(config: Config) -> list[dict[str, Any]]:
    engine = _engine(config)
    try:
        table = Table(CERT_TABLE, MetaData(), autoload_with=engine)
        with engine.connect() as conn:
            rows = conn.execute(select(table).order_by(table.c.id)).mappings()
            return [dict(row) for row in rows]
    finally:
        engine.dispose()


def _host() -> tuple[Flask, EventBus]:
    """最小宿主 app：只挂事件总线（与 worker 的 `build_event_host` 同口径）。"""
    app = Flask("events-test")
    bus = EventBus()
    app.extensions["zhy_events"] = bus
    return app, bus


# ── 001 / 002 / 003 / 004：总线语义 ───────────────────────────────────────


def test_bus_payload_is_readonly() -> None:
    bus = EventBus()
    seen: list[dict[str, Any]] = []

    def handler(payload: Any) -> None:
        assert payload["user"] == 1
        with pytest.raises(TypeError):
            payload["user"] = 2  # 观察者不可改写主流程数据
        seen.append(dict(payload))

    bus.subscribe(EVENT_LESSON_FINISHED, handler, plugin_code="demo")
    report = bus.emit(EVENT_LESSON_FINISHED, {"user": 1})

    assert report.ok is True
    assert report.delivered == 1
    assert seen == [{"user": 1}]


def test_bus_isolates_failures_and_timeouts() -> None:
    bus = EventBus()
    seen: list[str] = []

    def boom(payload: Any) -> None:
        raise RuntimeError("boom")

    def slow(payload: Any) -> None:
        time.sleep(0.06)

    bus.subscribe(EVENT_LESSON_FINISHED, boom, plugin_code="bad")
    bus.subscribe(EVENT_LESSON_FINISHED, slow, plugin_code="slow", timeout_ms=10)
    bus.subscribe(EVENT_LESSON_FINISHED, lambda payload: seen.append("ok"))

    report = bus.emit(EVENT_LESSON_FINISHED, {"user": 1})

    assert report.delivered == 1
    assert seen == ["ok"]
    assert report.ok is False
    assert report.failures[0]["owner"] == "bad"
    assert "RuntimeError" in report.failures[0]["error"]
    assert report.timeouts[0]["owner"] == "slow"
    assert report.timeouts[0]["budget_ms"] == 10


def test_bus_unsubscribe_plugin_only_removes_its_own() -> None:
    bus = EventBus()
    bus.subscribe(EVENT_LESSON_FINISHED, lambda payload: None, plugin_code="alpha")
    bus.subscribe(EVENT_LESSON_FINISHED, lambda payload: None, plugin_code="beta")
    bus.subscribe(EVENT_LESSON_FINISHED, lambda payload: None)

    assert bus.unsubscribe_plugin("alpha") == 1
    assert [sub.owner for sub in bus.subscriptions()] == ["beta", "core"]
    assert bus.unsubscribe_plugin("ghost") == 0


@pytest.mark.parametrize(
    ("event", "timeout_ms", "handler"),
    [
        ("unknown.event", None, None),
        (EVENT_LESSON_FINISHED, 1, None),
        (EVENT_LESSON_FINISHED, 5000, None),
        (EVENT_LESSON_FINISHED, None, "not-callable"),
    ],
)
def test_bus_rejects_invalid_subscription(event: str, timeout_ms: int | None, handler: Any) -> None:
    bus = EventBus()
    target = handler if handler is not None else (lambda payload: None)
    with pytest.raises(AppError) as exc:
        bus.subscribe(event, target, timeout_ms=timeout_ms)
    assert exc.value.code == VALIDATION_FAILED


def test_event_name_whitelist() -> None:
    assert validate_event_name(EVENT_LESSON_FINISHED) == EVENT_LESSON_FINISHED
    with pytest.raises(AppError) as exc:
        validate_event_name("progress.recorded")
    assert exc.value.code == VALIDATION_FAILED
    assert exc.value.data["allowed"] == sorted(ALLOWED_EVENTS)


def test_get_bus_prefers_app_extension() -> None:
    app, bus = _host()
    with app.app_context():
        assert get_bus() is bus


# ── 005 / 006：SDK 订阅门禁与作用域重绑 ──────────────────────────────────


def test_sdk_events_requires_scope_and_capability() -> None:
    with pytest.raises(AppError) as exc:
        sdk.events.subscribe(EVENT_LESSON_FINISHED, lambda payload: None)
    assert exc.value.code == PLUGIN_CAP_DENIED
    assert exc.value.data["reason"] == "sdk_scope_missing"

    _, bus = _host()
    with using("demo", []), pytest.raises(AppError) as exc2:
        sdk.events.subscribe(EVENT_LESSON_FINISHED, lambda payload: None)
    assert exc2.value.code == PLUGIN_CAP_DENIED
    assert exc2.value.data["reason"] == "capability_missing"
    assert bus.subscriptions() == ()


def test_sdk_events_subscribe_binds_scope_on_dispatch() -> None:
    app, bus = _host()
    seen: list[dict[str, Any]] = []

    def handler(payload: Any) -> None:
        # 回调由 worker 线程触发：作用域必须已重绑，SDK 才能继续使用
        assert sdk.events.available() == ("lesson.finished",)
        seen.append(dict(payload))

    with app.app_context(), using("demo", ["event:subscribe"]):
        sdk.events.subscribe(EVENT_LESSON_FINISHED, handler)

    assert [sub.plugin_code for sub in bus.subscriptions(EVENT_LESSON_FINISHED)] == ["demo"]
    assert bus.emit(EVENT_LESSON_FINISHED, {"user": 7}).delivered == 1
    assert seen == [{"user": 7}]

    assert bus.unsubscribe_plugin("demo") == 1
    assert bus.emit(EVENT_LESSON_FINISHED, {"user": 7}).delivered == 0


def test_sdk_events_has_no_emit() -> None:
    assert not hasattr(sdk.events, "emit")


# ── 007 / 008：flush 跃迁派发 ─────────────────────────────────────────────


def test_flush_emits_lesson_finished_once_on_transition(tmp_path: Path) -> None:
    config, student_id, course_id = _seed(tmp_path)
    bus = EventBus()
    seen: list[dict[str, Any]] = []
    bus.subscribe(EVENT_LESSON_FINISHED, lambda payload: seen.append(dict(payload)))

    _record(config, student_id=student_id, position=95, played=DURATION)
    flush = ProgressFlush(config, events=bus)
    assert flush.flush_once() == 1
    assert seen == [
        {
            "user": student_id,
            "lesson": LESSON_ID,
            "course": course_id,
            "percent": 100.0,
            "via_ticket_preview": False,
        }
    ]

    # 已完成后的再次上报/重放：不重复派发（跃迁语义）
    _record(config, student_id=student_id, position=97, played=5)
    flush.flush_once()
    assert len(seen) == 1


def test_flush_without_bus_keeps_projecting(tmp_path: Path) -> None:
    config, student_id, _ = _seed(tmp_path)
    _record(config, student_id=student_id, position=95, played=DURATION)

    # 无显式总线且无 app 上下文：只跳过节派发，落库照常
    assert ProgressFlush(config).flush_once() == 1


# ── 009：参考插件端到端 ──────────────────────────────────────────────────


def _copy_plugins(dest: Path) -> Path:
    source = Path(__file__).resolve().parent.parent / "plugins"
    dest.mkdir(parents=True, exist_ok=True)
    for name in (SAMPLE_CODE, REF_CODE):
        shutil.copytree(source / name, dest / name)
    return dest


def _tables(config: Config) -> set[str]:
    engine = _engine(config)
    try:
        return set(inspect(engine).get_table_names())
    finally:
        engine.dispose()


def test_sample_hello_end_to_end(tmp_path: Path) -> None:
    plugin_root = _copy_plugins(tmp_path / "plugins")
    config, _, _ = _seed(tmp_path, plugin_root)
    client = _client(config)
    admin = _login(client, "root")
    student = _login(client, "student")
    base = f"/api/v1/system/plugins/{SAMPLE_CODE}"

    installed = client.post(f"{base}/install", headers=admin)
    assert installed.status_code == 200, installed.get_json()
    assert installed.get_json()["data"]["entry_web"] == "web/index.js"
    client.post(f"{base}/enable", headers=admin)

    served = client.get(f"/api/v1/plugin/{SAMPLE_CODE}/hello", headers=student)
    assert served.status_code == 200, served.get_json()
    assert served.get_json()["data"]["message"] == "hello from sample_hello"
    assert served.get_json()["data"]["flavor"] == "sqlite"

    listed = client.get("/api/v1/plugins", headers=student).get_json()["data"]
    assert [item["code"] for item in listed["items"]] == [SAMPLE_CODE]
    asset = client.get(listed["items"][0]["entry_web_url"])
    assert asset.status_code == 200
    assert b"export default" in asset.get_data()


def test_reference_certificate_end_to_end(tmp_path: Path) -> None:
    plugin_root = _copy_plugins(tmp_path / "plugins")
    config, student_id, course_id = _seed(tmp_path, plugin_root)
    app = create_app(config=config, probe=StaticProbe(READY))
    client = app.test_client()
    admin = _login(client, "root")
    student = _login(client, "student")
    base = f"/api/v1/system/plugins/{REF_CODE}"

    installed = client.post(f"{base}/install", headers=admin)
    assert installed.status_code == 200, installed.get_json()
    data = installed.get_json()["data"]
    assert data["schema_rev"] == "reference_certificate_0001"
    assert data["capabilities"] == ["event:subscribe"]
    assert CERT_TABLE in _tables(config)

    enabled = client.post(f"{base}/enable", headers=admin)
    assert enabled.status_code == 200, enabled.get_json()
    bus: EventBus = app.extensions["zhy_events"]
    assert [sub.owner for sub in bus.subscriptions(EVENT_LESSON_FINISHED)] == [REF_CODE]

    listed = client.get("/api/v1/plugins", headers=student).get_json()["data"]
    item = listed["items"][0]
    assert item["menu"] == [
        {"title": "我的证书", "path": "/plugin/reference_certificate/certificates"}
    ]
    assert item["entry_web_url"] == f"/api/v1/plugin/{REF_CODE}/assets/index.js"
    assert client.get(item["entry_web_url"]).status_code == 200

    empty = client.get(f"/api/v1/plugin/{REF_CODE}/certificates", headers=student)
    assert empty.status_code == 200, empty.get_json()
    assert empty.get_json()["data"] == {"items": [], "total": 0}

    payload = {
        "user": student_id,
        "lesson": LESSON_ID,
        "course": course_id,
        "percent": 100.0,
        "via_ticket_preview": True,
    }
    with app.app_context():
        preview = bus.emit(EVENT_LESSON_FINISHED, payload)
    assert preview.ok is True
    assert _cert_rows(config) == []

    with app.app_context():
        first = bus.emit(EVENT_LESSON_FINISHED, {**payload, "via_ticket_preview": False})
    assert first.ok is True, first.as_dict()
    rows = _cert_rows(config)
    assert len(rows) == 1
    assert (rows[0]["user_id"], rows[0]["lesson_id"], rows[0]["course_id"]) == (
        student_id,
        LESSON_ID,
        course_id,
    )

    # 事件重复派发：自然键幂等，只累加 version
    with app.app_context():
        second = bus.emit(EVENT_LESSON_FINISHED, {**payload, "via_ticket_preview": False})
    assert second.ok is True
    rows = _cert_rows(config)
    assert len(rows) == 1
    assert rows[0]["version"] >= 2

    served = client.get(f"/api/v1/plugin/{REF_CODE}/certificates", headers=student).get_json()
    assert served["data"]["total"] == 1
    cert = served["data"]["items"][0]
    assert cert["lesson_id"] == LESSON_ID
    assert cert["percent"] == "100.00"
    assert cert["issued_at"]

    # 禁用：订阅立即摘除，残留路由 404，派发不再产生证书
    client.post(f"{base}/disable", headers=admin)
    assert bus.subscriptions(EVENT_LESSON_FINISHED) == ()
    with app.app_context():
        bus.emit(EVENT_LESSON_FINISHED, {**payload, "via_ticket_preview": False})
    assert len(_cert_rows(config)) == 1
    assert client.get(f"/api/v1/plugin/{REF_CODE}/certificates", headers=student).status_code == 404

    # 卸载：插件独立迁移链降级，表与注册表行同时消失
    removed = client.delete(base, headers=admin)
    assert removed.status_code == 200, removed.get_json()
    assert CERT_TABLE not in _tables(config)
