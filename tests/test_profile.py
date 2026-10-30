"""M2.13 学习档案（§10.7 / §15.2 / §23.5）。

验收口径：
- 所有完成率/时长指标 **不包含预览**（`source=preview` 流水不进投影，课程档案为空）；
- 四个读接口全部返回 `metric_version="zhy-progress-v2"`；
- `learn_course_progress` 由 flush 单写者异步维护，`freshness_seconds` 表达滞后；
- 详情只下发已学课时（未学不泄露课程结构）；
- `GET /my/progress/gate` 为 `system:manage` 运维/自诊口，返 `last_gw_seq` 与滞后秒数；
- `GET /my/summary` 首屏一次请求返回学习与测验统计。
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import Engine, func, select, update
from sqlalchemy.orm import Session
from zhyonlineedu.api.schemas import (
    CourseProgressDetailOut,
    CourseProgressListOut,
    MySummaryOut,
    ProgressGateOut,
    TicketOut,
)
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
from zhyonlineedu.core.db.models.learn import (
    LearnCourseProgress,
    LearnProgressLog,
    utcnow,
)
from zhyonlineedu.core.db.models.media import MediaAsset
from zhyonlineedu.core.db.models.quiz import QuizAttempt
from zhyonlineedu.core.progress.flush import ProgressFlush
from zhyonlineedu.core.progress.gateway import ProgressGateway

PASSWORD = "S3cret-pass"  # noqa: S105 - 测试用固定口令
READY = ProbeResult(ProbeState.READY, flavor="sqlite", capabilities={"returning": True})

L1 = 1  # 课程A 第一章
L2 = 2  # 课程A 第一章
L3 = 3  # 课程A 第二章（不学，用于「详情只下发已学」）
L4 = 4  # 课程B（单课时，用于完成率 100%）
L5 = 5  # 草稿课（仅预览）
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
            "ZHY_DB_URL": f"sqlite:///{tmp_path / 'profile.db'}",
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

            def add_course(title: str, status: str) -> EduCourse:
                course = EduCourse(
                    subject_id=subject.id,
                    title=title,
                    mode="both",
                    access_type="public",
                    status=status,
                    sort_order=1024,
                )
                session.add(course)
                session.flush()
                return course

            def add_chapter(course: EduCourse, title: str) -> EduCourseChapter:
                chapter = EduCourseChapter(course_id=course.id, title=title, sort_order=1024)
                session.add(chapter)
                session.flush()
                return chapter

            def add_lesson(
                course: EduCourse, chapter: EduCourseChapter, lesson_id: int
            ) -> EduCourseLesson:
                lesson = EduCourseLesson(
                    id=lesson_id,
                    chapter_id=chapter.id,
                    course_id=course.id,
                    title=f"课时{lesson_id}",
                    kind="video",
                    duration_sec=DURATION,
                    is_free=False,
                    sort_order=1024,
                )
                session.add(lesson)
                return lesson

            course_a = add_course("课程A", "published")
            chapter_a1 = add_chapter(course_a, "A-1")
            chapter_a2 = add_chapter(course_a, "A-2")
            add_lesson(course_a, chapter_a1, L1)
            add_lesson(course_a, chapter_a1, L2)
            add_lesson(course_a, chapter_a2, L3)

            course_b = add_course("课程B", "published")
            add_lesson(course_b, add_chapter(course_b, "B-1"), L4)

            draft = add_course("草稿课", "draft")
            add_lesson(draft, add_chapter(draft, "D-1"), L5)

            for uid, percent, passed in ((0, 90.0, True), (1, 50.0, False)):
                session.add(
                    QuizAttempt(
                        attempt_uid=f"{uid + 1:032x}",
                        user_id=users["student"].id,
                        lesson_id=L1,
                        course_id=course_a.id,
                        status="submitted",
                        question_count=2,
                        score_total=10,
                        score_earned=int(percent) // 10,
                        percent=percent,
                        passed=passed,
                        question_snapshot=json.dumps([]),
                        answer_key=json.dumps([]),
                        started_at=now,
                        submitted_at=now,
                        created_at=now,
                        updated_at=now,
                        version=1,
                    )
                )
            session.commit()
    finally:
        engine.dispose()
    return config


def _day_base() -> datetime:
    """锚定当天 01:00，保证「今日时长」不因跨午夜而抖动。"""
    return utcnow().astimezone(UTC).replace(hour=1, minute=0, second=0, microsecond=0)


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


def _record(
    config: Config,
    username: str,
    lesson_id: int,
    *,
    position: int,
    played: int,
    can_preview: bool = False,
    at: Any = None,
) -> None:
    ProgressGateway(config).record(
        user_id=_user_id(config, username),
        lesson_id=lesson_id,
        body={
            "lesson_id": lesson_id,
            "position_sec": position,
            "played_seconds": played,
            "speed": 1.0,
            "client_ts_ms": 0,
        },
        can_preview=can_preview,
        now=at,
    )


# ── 预览不计入 + metric_version ───────────────────────────────────────────


def test_preview_never_enters_profile(tmp_path: Path) -> None:
    config = _seed(tmp_path)
    base = _day_base()
    _record(config, "student", L1, position=30, played=30, at=base)
    _record(config, "student", L2, position=50, played=50, at=base + timedelta(seconds=1))
    _record(config, "student", L4, position=100, played=100, at=base + timedelta(seconds=2))
    # 超管预览草稿课：只落流水，flush 后不得进入任何统计
    _record(
        config,
        "root",
        L5,
        position=80,
        played=80,
        can_preview=True,
        at=base + timedelta(seconds=3),
    )
    assert ProgressFlush(config).flush_once() == 4

    engine = _engine(config)
    try:
        with Session(engine) as session:
            student_rows = (
                session.execute(
                    select(LearnCourseProgress).where(
                        LearnCourseProgress.user_id == _user_id(config, "student")
                    )
                )
                .scalars()
                .all()
            )
            root_rows = (
                session.execute(
                    select(LearnCourseProgress).where(
                        LearnCourseProgress.user_id == _user_id(config, "root")
                    )
                )
                .scalars()
                .all()
            )
            preview = session.execute(
                select(LearnProgressLog).where(LearnProgressLog.source == "preview")
            ).scalar_one()
        assert len(student_rows) == 2
        assert root_rows == []
        assert preview.flushed_at is not None
    finally:
        engine.dispose()

    client = _client(config)
    student = _login(client, "student")
    listing = client.get("/api/v1/my/progress", headers=student)
    assert listing.status_code == 200
    data = listing.get_json()["data"]
    assert set(data) == set(CourseProgressListOut.model_fields)
    assert data["metric_version"] == ZHY_PROGRESS_METRIC_VERSION
    assert data["total"] == 2
    assert {item["course_title"] for item in data["items"]} == {"课程A", "课程B"}
    assert "草稿课" not in {item["course_title"] for item in data["items"]}

    root = _login(client, "root")
    empty = client.get("/api/v1/my/progress", headers=root).get_json()["data"]
    assert empty["total"] == 0
    summary = client.get("/api/v1/my/summary", headers=root).get_json()["data"]
    assert summary["watch_seconds"] == 0
    assert summary["today_watch_seconds"] == 0


def test_reads_require_auth(tmp_path: Path) -> None:
    client = _client(_seed(tmp_path))
    for path in ("/api/v1/my/progress", "/api/v1/my/summary", f"/api/v1/my/progress/{L1}"):
        response = client.get(path)
        assert response.status_code == 401, path


# ── 列表 / 详情 ──────────────────────────────────────────────────────────


def _flush_student(config: Config) -> None:
    base = _day_base()
    _record(config, "student", L1, position=30, played=30, at=base)
    _record(config, "student", L2, position=50, played=50, at=base + timedelta(seconds=1))
    _record(config, "student", L4, position=100, played=100, at=base + timedelta(seconds=2))
    ProgressFlush(config).flush_once()


def test_list_filters_and_detail_only_learned(tmp_path: Path) -> None:
    config = _seed(tmp_path)
    _flush_student(config)
    client = _client(config)
    headers = _login(client, "student")

    completed = client.get(
        "/api/v1/my/progress", query_string={"status": "completed"}, headers=headers
    ).get_json()["data"]
    assert completed["total"] == 1
    assert completed["items"][0]["course_title"] == "课程B"
    assert completed["items"][0]["percent"] == 100.0

    in_progress = client.get(
        "/api/v1/my/progress", query_string={"status": "in_progress"}, headers=headers
    ).get_json()["data"]
    assert in_progress["total"] == 1
    assert in_progress["items"][0]["course_title"] == "课程A"

    keyword = client.get(
        "/api/v1/my/progress", query_string={"keyword": "B"}, headers=headers
    ).get_json()["data"]
    assert keyword["total"] == 1
    assert keyword["items"][0]["course_title"] == "课程B"

    detail = client.get(f"/api/v1/my/progress/{_course_id(config, '课程A')}", headers=headers)
    assert detail.status_code == 200
    data = detail.get_json()["data"]
    assert set(data) == set(CourseProgressDetailOut.model_fields)
    assert data["metric_version"] == ZHY_PROGRESS_METRIC_VERSION
    assert data["course"]["watch_seconds"] == 80
    assert data["course"]["total_lessons"] == 3
    assert data["course"]["completed_lessons"] == 0
    assert data["course"]["percent"] == 0.0
    # 只下发已学课时：L3 / 第二章不得出现在详情
    lesson_ids = [
        lesson["lesson_id"] for chapter in data["chapters"] for lesson in chapter["lessons"]
    ]
    assert lesson_ids == [L1, L2]
    assert [chapter["title"] for chapter in data["chapters"]] == ["A-1"]

    missing = client.get("/api/v1/my/progress/999", headers=headers)
    assert missing.status_code == 404
    assert missing.get_json()["code"] == "NOT_FOUND"


# ── 首屏汇总 ──────────────────────────────────────────────────────────────


def test_summary_includes_quiz_and_recent(tmp_path: Path) -> None:
    config = _seed(tmp_path)
    _flush_student(config)
    client = _client(config)
    headers = _login(client, "student")

    response = client.get("/api/v1/my/summary", headers=headers)
    assert response.status_code == 200
    data = response.get_json()["data"]
    assert set(data) == set(MySummaryOut.model_fields)
    assert data["metric_version"] == ZHY_PROGRESS_METRIC_VERSION
    assert data["courses_started"] == 2
    assert data["courses_completed"] == 1
    assert data["lessons_completed"] == 1
    assert data["watch_seconds"] == 180
    assert data["speed_time"] == 180
    assert data["today_watch_seconds"] == 180
    assert [item["course_id"] for item in data["recent_courses"]] == [
        _course_id(config, "课程B"),
        _course_id(config, "课程A"),
    ]
    assert data["quiz"] == {
        "attempts": 2,
        "passed": 1,
        "average_percent": 70.0,
        "best_percent": 90.0,
    }
    assert data["freshness_seconds"] == 0


# ── 落库诊断 ──────────────────────────────────────────────────────────────


def test_progress_gate_reports_lag_and_rbac(tmp_path: Path) -> None:
    config = _seed(tmp_path)
    now = utcnow()
    _record(config, "student", L1, position=30, played=30, at=now)
    _record(config, "student", L2, position=50, played=50, at=now)
    _record(config, "student", L4, position=100, played=100, at=now)

    # 模拟 worker 停摆：把未落库流水时间前移，滞后超过容差
    engine = _engine(config)
    try:
        with Session(engine) as session:
            session.execute(
                update(LearnProgressLog).values(received_at=now - timedelta(minutes=10))
            )
            session.commit()
    finally:
        engine.dispose()

    client = _client(config)
    root = _login(client, "root")
    before = client.get("/api/v1/my/progress/gate", headers=root)
    assert before.status_code == 200
    data = before.get_json()["data"]
    assert set(data) == set(ProgressGateOut.model_fields)
    assert data["metric_version"] == ZHY_PROGRESS_METRIC_VERSION
    assert data["pending_logs"] == 3
    assert data["last_gw_seq"] == 3
    assert data["flushed_max_gw_seq"] == 0
    assert data["gap"] == 3
    assert data["lag_seconds"] >= 600
    assert data["healthy"] is False

    assert ProgressFlush(config).flush_once() == 3
    after = client.get("/api/v1/my/progress/gate", headers=root).get_json()["data"]
    assert after["pending_logs"] == 0
    assert after["flushed_max_gw_seq"] == 3
    assert after["gap"] == 0
    assert after["healthy"] is True

    for username in ("student", "teacher"):
        denied = client.get("/api/v1/my/progress/gate", headers=_login(client, username))
        assert denied.status_code == 403
        assert denied.get_json()["data"]["required"] == "system:manage"


# ── 票据口径 ──────────────────────────────────────────────────────────────


def test_ticket_carries_metric_version(tmp_path: Path) -> None:
    """§15.3：票据响应必须带 metric_version（进度统计口径可对比）。"""
    config = _seed(tmp_path)
    engine = _engine(config)
    try:
        with Session(engine) as session:
            session.add(
                MediaAsset(
                    lesson_id=L1,
                    status="encoded",
                    playlist_version=1,
                    duration_sec=DURATION,
                    kid="a" * 32,
                    profiles=json.dumps(["360"]),
                    created_at=utcnow(),
                    updated_at=utcnow(),
                )
            )
            session.commit()
    finally:
        engine.dispose()

    client = _client(config)
    response = client.post(
        "/api/v1/media/tickets", json={"lesson_id": L1}, headers=_login(client, "student")
    )
    assert response.status_code == 200, response.get_json()
    data = response.get_json()["data"]
    assert set(data) == set(TicketOut.model_fields)
    assert data["metric_version"] == ZHY_PROGRESS_METRIC_VERSION


def test_course_progress_count_matches_learned_courses(tmp_path: Path) -> None:
    """投影表是唯一读源：统计口径改动不得绕过 learn_course_progress。"""
    config = _seed(tmp_path)
    _flush_student(config)
    engine = _engine(config)
    try:
        with Session(engine) as session:
            count = int(
                session.execute(select(func.count()).select_from(LearnCourseProgress)).scalar_one()
            )
        assert count == 2
    finally:
        engine.dispose()
