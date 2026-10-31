"""M2.14 学员端课程目录（§10.1 / §14）。

验收口径：
- 只暴露 `published` 课程（学科启用、未删除），并按当前模式过滤（`both` 或当前模式）；
- 课程卡片带课时数 / 总时长 / 首个可播课时 / 本人进度，详情带章与课时级进度；
- `access_ok` 只是 UI 提示：`member` 课程需有效会员、`class` 课程需班级成员；
- 未上架 / 模式不可见统一 404（不泄露存在性）；
- 所有响应带 `metric_version`，进度来自投影，预览不进入统计。
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import Engine, select
from sqlalchemy.orm import Session
from zhyonlineedu.api.schemas import LearnCourseDetailOut, LearnCourseListOut
from zhyonlineedu.app import create_app
from zhyonlineedu.core.auth.keys import clear_key_cache, ensure_keypair
from zhyonlineedu.core.auth.passwords import hash_password
from zhyonlineedu.core.capability import ProbeResult, ProbeState, StaticProbe
from zhyonlineedu.core.config import Config, load_config
from zhyonlineedu.core.constants import ZHY_PROGRESS_METRIC_VERSION
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
from zhyonlineedu.core.db.models.commerce import MemberLevel
from zhyonlineedu.core.db.models.learn import utcnow
from zhyonlineedu.core.db.models.media import MediaAsset
from zhyonlineedu.core.progress.flush import ProgressFlush
from zhyonlineedu.core.progress.gateway import ProgressGateway
from zhyonlineedu.modules.commerce.service import grant_level

PASSWORD = "S3cret-pass"  # noqa: S105 - 测试用固定口令
READY = ProbeResult(ProbeState.READY, flavor="sqlite", capabilities={"returning": True})

L1 = 1
L2 = 2
L3 = 3
L4 = 4
L5 = 5
L6 = 6
L7 = 7
DURATION = 100


@pytest.fixture(autouse=True)
def _cleanup() -> Iterator[None]:
    compat.reset()
    clear_key_cache()
    yield
    compat.reset()
    clear_key_cache()


def _config(tmp_path: Path) -> Config:
    return load_config(
        environ={
            "ZHY_ENV": "dev",
            "ZHY_DB_URL": f"sqlite:///{tmp_path / 'learn-catalog.db'}",
            "ZHY_DB_FLAVOR": "sqlite",
            "ZHY_JWT_PRIVATE_KEY_PATH": str(tmp_path / "keys" / "jwt_private.pem"),
            "ZHY_JWT_PUBLIC_KEY_PATH": str(tmp_path / "keys" / "jwt_public.pem"),
            "ZHY_STORAGE_LOCAL_DIR": str(tmp_path / "storage"),
            "ZHY_PROGRESS_RING_DIR": str(tmp_path / "ring"),
        },
        sys_config={},
    )


def _seed(tmp_path: Path) -> Config:
    config = _config(tmp_path)
    ensure_keypair(config.get_str("jwt.private_key_path"), config.get_str("jwt.public_key_path"))
    upgrade_head(config.get_str("db.url"), "sqlite")
    engine = create_db_engine(config.get_str("db.url"), "sqlite")
    try:
        with Session(engine) as session:
            now = utcnow()
            users: dict[str, AccountUser] = {}
            for username, role in (
                ("root", "super_admin"),
                ("student", "student"),
                ("teacher", "teacher"),
            ):
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
                users[username] = user

            subject = EduSubject(name="数学", sort_order=1024, is_enabled=True)
            session.add(subject)
            session.flush()

            def add_course(
                title: str,
                *,
                status: str = "published",
                mode: str = "both",
                access_type: str = "public",
            ) -> EduCourse:
                course = EduCourse(
                    subject_id=subject.id,
                    title=title,
                    mode=mode,
                    access_type=access_type,
                    status=status,
                    sort_order=1024,
                )
                session.add(course)
                session.flush()
                return course

            chapters: dict[int, EduCourseChapter] = {}

            def add_lesson(course: EduCourse, title: str, lesson_id: int) -> EduCourseLesson:
                chapter = chapters.get(int(course.id))
                if chapter is None:
                    chapter = EduCourseChapter(
                        course_id=course.id, title=f"{course.title}-章", sort_order=1024
                    )
                    session.add(chapter)
                    session.flush()
                    chapters[int(course.id)] = chapter
                lesson = EduCourseLesson(
                    id=lesson_id,
                    chapter_id=chapter.id,
                    course_id=course.id,
                    title=title,
                    kind="video",
                    duration_sec=DURATION,
                    is_free=False,
                    sort_order=1024,
                )
                session.add(lesson)
                session.flush()
                return lesson

            course_a = add_course("课程A")
            add_lesson(course_a, "A-1", L1)
            add_lesson(course_a, "A-2", L2)
            add_lesson(course_a, "A-3", L3)

            course_b = add_course("课程B")
            add_lesson(course_b, "B-1", L4)

            draft = add_course("草稿课", status="draft")
            add_lesson(draft, "D-1", L5)

            external = add_course("外部课", mode="external")
            add_lesson(external, "E-1", L6)

            member = add_course("会员课", access_type="member")
            add_lesson(member, "M-1", L7)

            level = MemberLevel(
                code="vip",
                name="VIP",
                level=1,
                price=0,
                duration_days=30,
                is_enabled=True,
                sort_order=1024,
                created_at=now,
                updated_at=now,
            )
            session.add(level)
            session.flush()

            for lesson_id in (L1, L2, L3, L4, L7):
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
            session.commit()
    finally:
        engine.dispose()
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
            return int(
                session.execute(
                    select(AccountUser.id).where(AccountUser.username == username)
                ).scalar_one()
            )
    finally:
        engine.dispose()


def _course_id(config: Config, title: str) -> int:
    engine = _engine(config)
    try:
        with Session(engine) as session:
            return int(
                session.execute(select(EduCourse.id).where(EduCourse.title == title)).scalar_one()
            )
    finally:
        engine.dispose()


def _subject_id(config: Config) -> int:
    engine = _engine(config)
    try:
        with Session(engine) as session:
            return int(session.execute(select(EduSubject.id)).scalar_one())
    finally:
        engine.dispose()


def test_student_catalog_only_published_and_mode_visible(tmp_path: Path) -> None:
    config = _seed(tmp_path)
    client = _client(config)
    headers = _login(client, "student")

    response = client.get("/api/v1/learn/courses", headers=headers)
    assert response.status_code == 200
    data = response.get_json()["data"]
    assert set(data) == set(LearnCourseListOut.model_fields)
    assert data["metric_version"] == ZHY_PROGRESS_METRIC_VERSION
    titles = [item["title"] for item in data["items"]]
    assert titles == ["课程A", "课程B", "会员课"]
    assert data["total"] == 3

    keyword = client.get(
        "/api/v1/learn/courses", query_string={"keyword": "B"}, headers=headers
    ).get_json()["data"]
    assert [item["title"] for item in keyword["items"]] == ["课程B"]

    by_subject = client.get(
        "/api/v1/learn/courses",
        query_string={"subject_id": _subject_id(config)},
        headers=headers,
    ).get_json()["data"]
    assert by_subject["total"] == 3
    assert len(by_subject["items"]) == 3

    course_a = data["items"][0]
    assert course_a["total_lessons"] == 3
    assert course_a["total_duration_sec"] == DURATION * 3
    assert course_a["first_lesson_id"] == L1
    assert course_a["access_ok"] is True
    assert course_a["progress"] is None

    member = data["items"][2]
    assert member["access_type"] == "member"
    assert member["access_ok"] is False


def test_progress_overlay_and_detail(tmp_path: Path) -> None:
    config = _seed(tmp_path)
    base = datetime.now(UTC).replace(hour=1, minute=0, second=0, microsecond=0)
    gateway = ProgressGateway(config)
    gateway.record(
        user_id=_user_id(config, "student"),
        lesson_id=L1,
        body={
            "lesson_id": L1,
            "position_sec": 90,
            "played_seconds": 90,
            "speed": 1.0,
            "client_ts_ms": 0,
        },
        can_preview=False,
        now=base,
    )
    assert ProgressFlush(config).flush_once() == 1

    client = _client(config)
    headers = _login(client, "student")
    listing = client.get("/api/v1/learn/courses", headers=headers).get_json()["data"]
    course_a = next(item for item in listing["items"] if item["title"] == "课程A")
    assert course_a["progress"] is not None
    assert course_a["progress"]["watch_seconds"] == 90
    assert course_a["progress"]["resume_lesson_id"] == L1
    assert course_a["progress"]["resume_position"] == 90
    assert listing["freshness_seconds"] == 0

    detail = client.get(f"/api/v1/learn/courses/{_course_id(config, '课程A')}", headers=headers)
    assert detail.status_code == 200
    data = detail.get_json()["data"]
    assert set(data) == set(LearnCourseDetailOut.model_fields)
    assert data["metric_version"] == ZHY_PROGRESS_METRIC_VERSION
    assert data["course"]["title"] == "课程A"
    lessons = [lesson for chapter in data["chapters"] for lesson in chapter["lessons"]]
    assert [lesson["lesson_id"] for lesson in lessons] == [L1, L2, L3]
    assert lessons[0]["asset_status"] == "encoded"
    assert lessons[0]["progress"]["watch_seconds"] == 90
    assert lessons[1]["progress"] is None

    # 草稿 / 外部模式 / 删除后统一 404（不泄露存在性）
    draft = client.get(f"/api/v1/learn/courses/{_course_id(config, '草稿课')}", headers=headers)
    assert draft.status_code == 404
    external = client.get(f"/api/v1/learn/courses/{_course_id(config, '外部课')}", headers=headers)
    assert external.status_code == 404

    assert client.get("/api/v1/learn/courses").status_code == 401


def test_member_access_hint_follows_subscription(tmp_path: Path) -> None:
    config = _seed(tmp_path)
    client = _client(config)
    headers = _login(client, "student")

    before = client.get("/api/v1/learn/courses", headers=headers).get_json()["data"]
    member = next(item for item in before["items"] if item["title"] == "会员课")
    assert member["access_ok"] is False

    engine = _engine(config)
    try:
        with Session(engine) as session:
            level_id = int(session.execute(select(MemberLevel.id)).scalar_one())
            grant_level(
                session,
                user_id=_user_id(config, "student"),
                level_id=level_id,
                level=1,
                level_code="vip",
                level_name="VIP",
                duration_days=30,
                source="grant",
                now=datetime.now(UTC) - timedelta(minutes=1),
            )
            session.commit()
    finally:
        engine.dispose()

    after = client.get("/api/v1/learn/courses", headers=headers).get_json()["data"]
    member = next(item for item in after["items"] if item["title"] == "会员课")
    assert member["access_ok"] is True
