"""M3.8 全链路联调（INSTALL → 课程 → 播放 → 进度 → 事件 → 插件）。

一条链路贯穿五个里程碑的接口，全部走 HTTP（除媒体资产与 worker 外）：

1. 全新空库：`/healthz` warn，业务接口 409 `INSTALL_REQUIRED`；
2. 5 步安装向导（token → db/test → db/setup → admin → mode → finish）；
3. 重启装配后超管建学科/课程/章/课时并上架（媒体资产由 DB 置 encoded）；
4. 学员登录 → 课程可见 → 取播放票 → 上报进度（HTTP 202）；
5. 单写者 worker（带事件总线）落库并在「未完成 → 已完成」跃迁派发 `lesson.finished`；
6. 参考证书插件早已启用：证书由事件生成 → 学员经插件路由读到；
7. 禁用插件 → 插件路由 404、订阅摘除、内核课程接口照常；卸载 → 插件表降级删除。

这是 M3 DoD「插件契约通过且禁用/卸载后内核完整可用」的端到端证据。
"""

from __future__ import annotations

import shutil
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import Engine, inspect, update
from sqlalchemy.orm import Session
from zhyonlineedu.app import create_app
from zhyonlineedu.core.auth.keys import clear_key_cache, ensure_keypair
from zhyonlineedu.core.auth.passwords import hash_password
from zhyonlineedu.core.capability import ProbeResult, ProbeState, StaticProbe
from zhyonlineedu.core.codes import INSTALL_REQUIRED
from zhyonlineedu.core.config import Config, load_config
from zhyonlineedu.core.db import compat
from zhyonlineedu.core.db.engine import create_db_engine
from zhyonlineedu.core.db.models.account import AccountUser, SysUserRole
from zhyonlineedu.core.db.models.catalog import EduCourseLesson
from zhyonlineedu.core.db.models.learn import utcnow
from zhyonlineedu.core.db.models.media import MediaAsset
from zhyonlineedu.core.db.probe import load_flags
from zhyonlineedu.core.events import EVENT_LESSON_FINISHED
from zhyonlineedu.core.progress.flush import ProgressFlush

PASSWORD = "S3cret-pass"  # noqa: S105 - 测试用固定口令
TOKEN = "install-token-e2e-0123456789"  # noqa: S105
SESSION_HEADER = "X-ZHY-Install-Session"
PLUGIN_CODE = "reference_certificate"
CERT_TABLE = "plg_reference_certificate_cert"
DURATION = 100


@pytest.fixture(autouse=True)
def _cleanup() -> Iterator[None]:
    compat.reset()
    clear_key_cache()
    yield
    compat.reset()
    clear_key_cache()


def _config(tmp_path: Path, plugin_root: Path) -> Config:
    return load_config(
        environ={
            "ZHY_ENV": "dev",
            "ZHY_DB_URL": f"sqlite:///{tmp_path / 'e2e.db'}",
            "ZHY_DB_FLAVOR": "sqlite",
            "ZHY_INSTALL_TOKEN": TOKEN,
            "ZHY_JWT_PRIVATE_KEY_PATH": str(tmp_path / "keys" / "jwt_private.pem"),
            "ZHY_JWT_PUBLIC_KEY_PATH": str(tmp_path / "keys" / "jwt_public.pem"),
            "ZHY_STORAGE_LOCAL_DIR": str(tmp_path / "storage"),
            "ZHY_PROGRESS_RING_DIR": str(tmp_path / "ring"),
            "ZHY_PLUGIN_DIR": str(plugin_root),
        },
        sys_config={},
    )


def _engine(config: Config) -> Engine:
    return create_db_engine(config.get_str("db.url"), "sqlite")


def _install(client: Any) -> None:
    """5 步向导（空库 → installed）。"""
    assert client.get("/healthz").get_json()["status"] == "warn"
    blocked = client.get("/api/v1/learn/courses")
    assert blocked.status_code == 409
    assert blocked.get_json()["code"] == INSTALL_REQUIRED

    session = client.post("/api/v1/install/token", json={"token": TOKEN}).get_json()["data"]
    headers = {SESSION_HEADER: session["session"]}

    assert client.post("/api/v1/install/db/test", headers=headers).get_json()["data"]["ok"] is True
    assert client.post("/api/v1/install/db/setup", headers=headers).status_code == 200
    admin = client.post(
        "/api/v1/install/admin",
        json={"username": "root", "password": PASSWORD},
        headers=headers,
    )
    assert admin.status_code == 200, admin.get_json()
    assert (
        client.post("/api/v1/install/mode", json={"mode": "external"}, headers=headers).status_code
        == 200
    )
    finished = client.post("/api/v1/install/finish", headers=headers)
    assert finished.status_code == 200, finished.get_json()
    assert finished.get_json()["data"]["installed"] is True


def _login(client: Any, username: str) -> dict[str, str]:
    response = client.post("/api/v1/auth/login", json={"username": username, "password": PASSWORD})
    assert response.status_code == 200, response.get_json()
    return {"Authorization": f"Bearer {response.get_json()['data']['access_token']}"}


def _seed_student(config: Config) -> int:
    engine = _engine(config)
    try:
        with Session(engine) as session:
            now = utcnow()
            user = AccountUser(
                username="student",
                password_hash=hash_password(PASSWORD),
                status="active",
                token_version=1,
                created_at=now,
                updated_at=now,
            )
            session.add(user)
            session.flush()
            session.add(SysUserRole(user_id=user.id, role_code="student"))
            session.commit()
            return int(user.id)
    finally:
        engine.dispose()


def _mark_encoded(config: Config, lesson_id: int) -> None:
    """资产置 encoded 且回填课时时长（转码链路由 M2 用例覆盖，联调只关心下游）。"""
    engine = _engine(config)
    try:
        with Session(engine) as session:
            now = utcnow()
            session.add(
                MediaAsset(
                    lesson_id=lesson_id,
                    status="encoded",
                    playlist_version=1,
                    duration_sec=DURATION,
                    kid=f"{lesson_id:032x}",
                    profiles='["360"]',
                    created_at=now,
                    updated_at=now,
                )
            )
            session.execute(
                update(EduCourseLesson)
                .where(EduCourseLesson.id == lesson_id)
                .values(duration_sec=DURATION)
            )
            session.commit()
    finally:
        engine.dispose()


def test_full_chain_install_to_plugin_certificate(tmp_path: Path) -> None:
    source = Path(__file__).resolve().parent.parent / "plugins"
    plugin_root = tmp_path / "plugins"
    shutil.copytree(source / PLUGIN_CODE, plugin_root / PLUGIN_CODE)

    config = _config(tmp_path, plugin_root)
    ensure_keypair(config.get_str("jwt.private_key_path"), config.get_str("jwt.public_key_path"))

    # ① 空库：安装向导
    _install(create_app(config=config).test_client())

    # ② 重启按固化能力位装配（业务蓝图 + 插件 boot）
    engine = _engine(config)
    try:
        flags = load_flags(engine)
    finally:
        engine.dispose()
    probe = StaticProbe(ProbeResult(ProbeState.READY, flavor="sqlite", capabilities=flags))
    app = create_app(config=config, probe=probe)
    client = app.test_client()

    admin = _login(client, "root")
    student_id = _seed_student(config)
    student = _login(client, "student")

    # ③ 内容生产：学科 → 课程 → 章 → 课时 → 资产就绪 → 上架
    subject = client.post("/api/v1/subjects", json={"name": "数学"}, headers=admin)
    assert subject.status_code == 200, subject.get_json()
    subject_id = subject.get_json()["data"]["id"]

    course = client.post(
        "/api/v1/courses",
        json={
            "subject_id": subject_id,
            "title": "全链路课程",
            "mode": "both",
            "access_type": "public",
        },
        headers=admin,
    )
    assert course.status_code == 200, course.get_json()
    course_id = course.get_json()["data"]["id"]

    chapter = client.post(
        f"/api/v1/courses/{course_id}/chapters",
        json={"title": "第一章"},
        headers=admin,
    )
    assert chapter.status_code == 200, chapter.get_json()
    chapter_id = chapter.get_json()["data"]["id"]

    lesson = client.post(
        f"/api/v1/chapters/{chapter_id}/lessons",
        json={"title": "第一节", "kind": "video", "is_free": False},
        headers=admin,
    )
    assert lesson.status_code == 200, lesson.get_json()
    lesson_id = lesson.get_json()["data"]["id"]

    _mark_encoded(config, lesson_id)
    detail = client.get(f"/api/v1/courses/{course_id}", headers=admin).get_json()["data"]
    published = client.post(
        f"/api/v1/courses/{course_id}/publish",
        json={"version": detail["version"]},
        headers=admin,
    )
    assert published.status_code == 200, published.get_json()

    # ④ 插件：安装 + 启用（早于进度，确保订阅在 worker 派发前已就绪）
    base = f"/api/v1/system/plugins/{PLUGIN_CODE}"
    installed = client.post(f"{base}/install", headers=admin)
    assert installed.status_code == 200, installed.get_json()
    assert installed.get_json()["data"]["schema_rev"] == "reference_certificate_0001"
    assert client.post(f"{base}/enable", headers=admin).status_code == 200
    bus = app.extensions["zhy_events"]
    assert [sub.owner for sub in bus.subscriptions(EVENT_LESSON_FINISHED)] == [PLUGIN_CODE]

    # ⑤ 学员侧：课程可见 → 取票 → 上报进度
    catalog = client.get("/api/v1/learn/courses", headers=student)
    assert catalog.status_code == 200, catalog.get_json()
    assert [item["course_id"] for item in catalog.get_json()["data"]["items"]] == [course_id]

    ticket = client.post("/api/v1/media/tickets", json={"lesson_id": lesson_id}, headers=student)
    assert ticket.status_code == 200, ticket.get_json()
    assert ticket.get_json()["data"]["preview"] is False

    reported = client.post(
        "/api/v1/learn/progress",
        json={
            "lesson_id": lesson_id,
            "position_sec": DURATION,
            "played_seconds": DURATION,
            "speed": 1.0,
            "client_ts_ms": 0,
        },
        headers=student,
    )
    assert reported.status_code == 202, reported.get_json()

    # ⑥ 单写者 worker 落库：跃迁派发 → 插件收到事件写证书
    with app.app_context():
        flush = ProgressFlush(config, events=bus)
        assert flush.flush_once() == 1

    certs = client.get(f"/api/v1/plugin/{PLUGIN_CODE}/certificates", headers=student)
    assert certs.status_code == 200, certs.get_json()
    items = certs.get_json()["data"]["items"]
    assert len(items) == 1
    assert (items[0]["lesson_id"], items[0]["course_id"]) == (lesson_id, course_id)
    assert items[0]["percent"] == "100.00"

    progress = client.get(f"/api/v1/my/progress/{course_id}", headers=student).get_json()["data"]
    assert progress["course"]["completed_lessons"] == 1
    assert progress["course"]["completed"] is True

    # 重复上报 + 再 flush：幂等（不重复派发、不重复发证）
    client.post(
        "/api/v1/learn/progress",
        json={
            "lesson_id": lesson_id,
            "position_sec": DURATION,
            "played_seconds": 5,
            "speed": 1.0,
            "client_ts_ms": 35_000,
        },
        headers=student,
    )
    with app.app_context():
        ProgressFlush(config, events=bus).flush_once()
    assert (
        client.get(f"/api/v1/plugin/{PLUGIN_CODE}/certificates", headers=student).get_json()[
            "data"
        ]["total"]
        == 1
    )

    # ⑦ 禁用：插件路由 404、订阅摘除、内核照常；卸载：插件表降级删除
    assert client.post(f"{base}/disable", headers=admin).status_code == 200
    assert bus.subscriptions(EVENT_LESSON_FINISHED) == ()
    cert_url = f"/api/v1/plugin/{PLUGIN_CODE}/certificates"
    assert client.get(cert_url, headers=student).status_code == 404
    assert client.get("/api/v1/learn/courses", headers=student).status_code == 200

    assert client.delete(base, headers=admin).status_code == 200
    engine = _engine(config)
    try:
        assert CERT_TABLE not in set(inspect(engine).get_table_names())
    finally:
        engine.dispose()

    # 学员进度与成绩不受插件卸载影响
    assert client.get(f"/api/v1/my/progress/{course_id}", headers=student).status_code == 200
    assert student_id > 0
