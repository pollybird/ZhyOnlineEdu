"""M2.6 题库判分 + Idempotency-Key（§10.4 · F5）。

验收口径：
1. **答案不下发** —— 学员端所有响应（入口/开始/详情/交卷/历史）递归扫描，
   不得出现 `answer` / `answer_key` / `correct_answer`；标准答案只出现在 `course:edit` 题库接口；
2. **重试不双成绩** —— 交卷必须带 `Idempotency-Key`（= `attempt_uid`）：
   同 key 同 payload → 200 且返回首次成绩；同 key 不同 payload → 409；
   无 key/非法 key → 422；并发条件更新失败时回读首次结果，绝不二次判分。

另覆盖：题型校验（判断题选项服务端固定）、乐观锁、软删、入口权限（draft/member/archived）、
开始作答冻结快照（改题不影响在途）、多选无部分分、作答私有性、历史分页。
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import Engine, select
from sqlalchemy.orm import Session
from zhyonlineedu.api.schemas import QuizAttemptOut, QuizEntryOut, QuizQuestionOut
from zhyonlineedu.app import create_app
from zhyonlineedu.core.auth.keys import clear_key_cache, ensure_keypair
from zhyonlineedu.core.auth.passwords import hash_password
from zhyonlineedu.core.capability import ProbeResult, ProbeState, StaticProbe
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
from zhyonlineedu.core.db.models.quiz import QuizAttempt, QuizQuestion, utcnow
from zhyonlineedu.core.idempotency import HEADER, payload_hash
from zhyonlineedu.modules.assess.service import AssessService

PASSWORD = "S3cret-pass"  # noqa: S105 - 测试用固定口令
READY = ProbeResult(ProbeState.READY, flavor="sqlite", capabilities={"returning": True})

L_PUBLIC = 1  # published/public，含 3 道启用题
L_MEMBER = 2  # published/member
L_DRAFT = 3  # draft/public
L_BLANK = 4  # 无题目（quiz_empty）
L_ARCHIVED = 5  # 所属课程 archived

# 学员端响应里永远不该出现的字段
FORBIDDEN = {"answer", "answer_key", "correct_answer"}


@pytest.fixture(autouse=True)
def _cleanup() -> Iterator[None]:
    compat.reset()
    clear_key_cache()
    yield
    compat.reset()
    clear_key_cache()


def _config(tmp_path: Path, **overrides: str) -> Config:
    environ = {
        "ZHY_ENV": "dev",
        "ZHY_DB_URL": f"sqlite:///{tmp_path / 'quiz.db'}",
        "ZHY_DB_FLAVOR": "sqlite",
        "ZHY_JWT_PRIVATE_KEY_PATH": str(tmp_path / "keys" / "jwt_private.pem"),
        "ZHY_JWT_PUBLIC_KEY_PATH": str(tmp_path / "keys" / "jwt_public.pem"),
        "ZHY_STORAGE_LOCAL_DIR": str(tmp_path / "storage"),
        "ZHY_PROGRESS_RING_DIR": str(tmp_path / "ring"),
        "ZHY_QUIZ_PASS_SCORE": "60",
    }
    environ.update(overrides)
    return load_config(environ=environ, sys_config={})


def _seed(tmp_path: Path, **overrides: str) -> Config:
    config = _config(tmp_path, **overrides)
    ensure_keypair(config.get_str("jwt.private_key_path"), config.get_str("jwt.public_key_path"))
    url = config.get_str("db.url")
    upgrade_head(url, "sqlite")
    engine = create_db_engine(url, "sqlite")
    try:
        with Session(engine) as session:
            now = utcnow()
            for username, role in (
                ("root", "super_admin"),
                ("student", "student"),
                ("other", "student"),
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
            subject = EduSubject(name="数学", sort_order=1024, is_enabled=True)
            session.add(subject)
            session.flush()

            def add_course(title: str, status: str, access_type: str) -> EduCourse:
                row = EduCourse(
                    subject_id=subject.id,
                    title=title,
                    mode="both",
                    access_type=access_type,
                    status=status,
                    sort_order=1024,
                )
                session.add(row)
                session.flush()
                return row

            def add_lesson(course: EduCourse, lesson_id: int) -> EduCourseLesson:
                chapter = EduCourseChapter(
                    course_id=course.id, title=f"章{lesson_id}", sort_order=1024
                )
                session.add(chapter)
                session.flush()
                lesson = EduCourseLesson(
                    id=lesson_id,
                    chapter_id=chapter.id,
                    course_id=course.id,
                    title=f"课{lesson_id}",
                    kind="video",
                    duration_sec=100,
                    is_free=False,
                    sort_order=1024,
                )
                session.add(lesson)
                session.flush()
                return lesson

            public = add_course("公开课", "published", "public")
            public_lesson = add_lesson(public, L_PUBLIC)
            member = add_course("会员课", "published", "member")
            add_lesson(member, L_MEMBER)
            draft = add_course("草稿课", "draft", "public")
            add_lesson(draft, L_DRAFT)
            blank = add_course("空题课", "published", "public")
            add_lesson(blank, L_BLANK)
            archived = add_course("归档课", "archived", "public")
            add_lesson(archived, L_ARCHIVED)

            for kind, stem, options, answer, score in (
                (
                    "single",
                    "1+1=?",
                    [{"key": "A", "text": "2"}, {"key": "B", "text": "3"}],
                    ["A"],
                    5,
                ),
                (
                    "multiple",
                    "哪些是偶数？",
                    [
                        {"key": "A", "text": "2"},
                        {"key": "B", "text": "3"},
                        {"key": "C", "text": "4"},
                    ],
                    ["A", "C"],
                    10,
                ),
            ):
                session.add(
                    QuizQuestion(
                        course_id=public.id,
                        lesson_id=public_lesson.id,
                        kind=kind,
                        stem=stem,
                        options=json.dumps(options, ensure_ascii=False),
                        answer=json.dumps(answer),
                        explanation=f"{stem} 的解析",
                        score=score,
                        sort_order=1024,
                        is_enabled=True,
                        created_at=now,
                        updated_at=now,
                    )
                )
            session.add(
                QuizQuestion(
                    course_id=public.id,
                    lesson_id=public_lesson.id,
                    kind="judge",
                    stem="地球是圆的",
                    options=json.dumps(
                        [{"key": "T", "text": "正确"}, {"key": "F", "text": "错误"}]
                    ),
                    answer=json.dumps(["T"]),
                    explanation=None,
                    score=5,
                    sort_order=2048,
                    is_enabled=True,
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


def _assert_no_answer(value: object, path: str = "$") -> None:
    """递归扫描：学员端任何响应都不得携带答案字段。"""
    if isinstance(value, dict):
        leaked = FORBIDDEN & set(value)
        assert not leaked, f"{path} 泄露答案字段 {leaked}"
        for key, item in value.items():
            _assert_no_answer(item, f"{path}.{key}")
    elif isinstance(value, list):
        for index, item in enumerate(value):
            _assert_no_answer(item, f"{path}[{index}]")


def _question_ids(config: Config) -> list[int]:
    engine = _engine(config)
    try:
        with Session(engine) as session:
            rows = session.execute(
                select(QuizQuestion.id).order_by(QuizQuestion.sort_order, QuizQuestion.id)
            ).scalars()
            return [int(value) for value in rows]
    finally:
        engine.dispose()


def _start(client: Any, headers: dict[str, str], lesson_id: int = L_PUBLIC) -> dict[str, Any]:
    response = client.post("/api/v1/quiz/attempts", json={"lesson_id": lesson_id}, headers=headers)
    assert response.status_code in (200, 201), response.get_json()
    data = response.get_json()["data"]
    assert set(data) == {"attempt", "created"}
    assert set(data["attempt"]) == set(QuizAttemptOut.model_fields)
    return data


def _submit(
    client: Any,
    headers: dict[str, str],
    attempt: dict[str, Any],
    answers: list[dict[str, Any]],
    *,
    key: str | None = None,
) -> Any:
    headers = dict(headers)
    headers[HEADER] = key if key is not None else attempt["attempt_uid"]
    return client.post(
        f"/api/v1/quiz/attempts/{attempt['id']}/submit",
        json={"answers": answers},
        headers=headers,
    )


# ── 题库管理（course:edit）────────────────────────────────────────────────


def test_question_manage_requires_permission(tmp_path: Path) -> None:
    client = _client(_seed(tmp_path))
    headers = _login(client, "student")
    listed = client.get("/api/v1/quiz/questions", headers=headers)
    assert listed.status_code == 403
    assert listed.get_json()["code"] == "PERMISSION_DENIED"
    created = client.post(
        "/api/v1/quiz/questions",
        json={"lesson_id": L_PUBLIC, "stem": "x", "answer": ["A"]},
        headers=headers,
    )
    assert created.status_code == 403


def test_list_questions_returns_answers_for_editor(tmp_path: Path) -> None:
    config = _seed(tmp_path)
    client = _client(config)
    headers = _login(client, "root")
    response = client.get(
        f"/api/v1/quiz/questions?lesson_id={L_PUBLIC}&kind=multiple", headers=headers
    )
    assert response.status_code == 200
    data = response.get_json()["data"]
    assert data["total"] == 1 and len(data["items"]) == 1
    item = data["items"][0]
    assert set(item) == set(QuizQuestionOut.model_fields)
    assert item["answer"] == ["A", "C"]  # 管理端才下发标准答案
    assert item["kind"] == "multiple"


def test_create_question_appends_and_validates(tmp_path: Path) -> None:
    config = _seed(tmp_path)
    client = _client(config)
    headers = _login(client, "root")
    created = client.post(
        "/api/v1/quiz/questions",
        json={
            "lesson_id": L_PUBLIC,
            "kind": "single",
            "stem": "2+2=?",
            "options": [{"key": "A", "text": "4"}, {"key": "B", "text": "5"}],
            "answer": ["A"],
            "explanation": "送分题",
            "score": 5,
        },
        headers=headers,
    )
    assert created.status_code == 200, created.get_json()
    item = created.get_json()["data"]
    assert item["sort_order"] == 3072  # 1024 / 2048 之后追加
    assert item["answer"] == ["A"]
    assert item["version"] == 1

    bad_bodies: list[dict[str, Any]] = [
        {"lesson_id": L_PUBLIC, "stem": "x", "answer": []},
        {"lesson_id": L_PUBLIC, "kind": "single", "stem": "x", "answer": ["A", "B"]},
        {"lesson_id": L_PUBLIC, "kind": "multiple", "stem": "x", "answer": ["A"]},
        {"lesson_id": L_PUBLIC, "stem": "", "answer": ["A"]},
        {"lesson_id": L_PUBLIC, "stem": "x", "answer": ["Z"]},
        {"lesson_id": L_PUBLIC, "kind": "essay", "stem": "x", "answer": ["A"]},
        {"lesson_id": L_PUBLIC, "stem": "x", "answer": ["A"], "score": 0},
        {
            "lesson_id": 999,
            "kind": "single",
            "stem": "x",
            "options": [{"key": "A", "text": "1"}, {"key": "B", "text": "2"}],
            "answer": ["A"],
        },
        {
            "lesson_id": L_PUBLIC,
            "stem": "x",
            "options": [{"key": "A", "text": "1"}, {"key": "A", "text": "2"}],
            "answer": ["A"],
        },
        {
            "lesson_id": L_PUBLIC,
            "stem": "x",
            "options": [{"key": "A", "text": "1"}],
            "answer": ["A"],
        },
    ]
    for body in bad_bodies:
        response = client.post("/api/v1/quiz/questions", json=body, headers=headers)
        expected = 404 if body["lesson_id"] == 999 else 422
        assert response.status_code == expected, body
        assert response.get_json()["code"] == (
            "NOT_FOUND" if expected == 404 else "VALIDATION_FAILED"
        )


def test_create_judge_forces_server_options(tmp_path: Path) -> None:
    config = _seed(tmp_path)
    client = _client(config)
    headers = _login(client, "root")
    response = client.post(
        "/api/v1/quiz/questions",
        json={
            "lesson_id": L_PUBLIC,
            "kind": "judge",
            "stem": "1 是质数",
            "options": [{"key": "X", "text": "是"}, {"key": "Y", "text": "否"}],
            "answer": ["T"],
        },
        headers=headers,
    )
    assert response.status_code == 200, response.get_json()
    item = response.get_json()["data"]
    assert [option["key"] for option in item["options"]] == ["T", "F"]

    invalid = client.post(
        "/api/v1/quiz/questions",
        json={"lesson_id": L_PUBLIC, "kind": "judge", "stem": "x", "answer": ["T", "F"]},
        headers=headers,
    )
    assert invalid.status_code == 422


def test_update_question_uses_optimistic_lock(tmp_path: Path) -> None:
    config = _seed(tmp_path)
    client = _client(config)
    headers = _login(client, "root")
    question_id = _question_ids(config)[0]

    patch = client.patch(
        f"/api/v1/quiz/questions/{question_id}",
        json={"version": 1, "stem": "1+1=?（改）", "score": 8},
        headers=headers,
    )
    assert patch.status_code == 200, patch.get_json()
    item = patch.get_json()["data"]
    assert item["version"] == 2
    assert item["stem"] == "1+1=?（改）"
    assert item["score"] == 8

    stale = client.patch(
        f"/api/v1/quiz/questions/{question_id}",
        json={"version": 1, "stem": "再来一次"},
        headers=headers,
    )
    assert stale.status_code == 409
    body = stale.get_json()
    assert body["code"] == "CONFLICT_RETRY"
    assert body["data"]["expected_version"] == 1

    missing = client.patch(
        "/api/v1/quiz/questions/99999", json={"version": 1, "stem": "x"}, headers=headers
    )
    assert missing.status_code == 404

    empty = client.patch(
        f"/api/v1/quiz/questions/{question_id}", json={"version": 2}, headers=headers
    )
    assert empty.status_code == 422


def test_delete_question_is_soft_and_affects_snapshot_only_for_new(tmp_path: Path) -> None:
    config = _seed(tmp_path)
    client = _client(config)
    headers = _login(client, "root")
    question_id = _question_ids(config)[-1]

    deleted = client.delete(
        f"/api/v1/quiz/questions/{question_id}", json={"version": 1}, headers=headers
    )
    assert deleted.status_code == 200
    assert deleted.get_json()["data"] == {"id": question_id, "deleted": True}

    again = client.delete(
        f"/api/v1/quiz/questions/{question_id}", json={"version": 2}, headers=headers
    )
    assert again.status_code == 404

    listed = client.get(f"/api/v1/quiz/questions?lesson_id={L_PUBLIC}", headers=headers)
    assert listed.get_json()["data"]["total"] == 2

    engine = _engine(config)
    try:
        with Session(engine) as session:
            row = session.get(QuizQuestion, question_id)
        assert row is not None and row.deleted_at is not None
        assert row.delete_seq == 1
    finally:
        engine.dispose()


# ── 入口与快照 ────────────────────────────────────────────────────────────


def test_entry_returns_no_answers(tmp_path: Path) -> None:
    config = _seed(tmp_path)
    client = _client(config)
    headers = _login(client, "student")
    response = client.get(f"/api/v1/lessons/{L_PUBLIC}/quiz", headers=headers)
    assert response.status_code == 200, response.get_json()
    data = response.get_json()["data"]
    assert set(data) == set(QuizEntryOut.model_fields)
    assert data["question_count"] == 3
    assert data["score_total"] == 20
    assert data["pass_score"] == 60
    assert data["attempt"] is None and data["last"] is None
    _assert_no_answer(response.get_json())


def test_entry_access_rules(tmp_path: Path) -> None:
    config = _seed(tmp_path)
    client = _client(config)
    student = _login(client, "student")
    root = _login(client, "root")

    assert client.get(f"/api/v1/lessons/{L_MEMBER}/quiz", headers=student).get_json()["code"] == (
        "ACCESS_LEVEL_REQUIRED"
    )
    draft = client.get(f"/api/v1/lessons/{L_DRAFT}/quiz", headers=student)
    assert draft.status_code == 403
    assert draft.get_json()["code"] == "COURSE_NOT_PUBLISHED"
    assert client.get(f"/api/v1/lessons/{L_DRAFT}/quiz", headers=root).status_code == 404
    assert (
        client.get(f"/api/v1/lessons/{L_BLANK}/quiz", headers=student).get_json()["data"]["reason"]
        == "quiz_empty"
    )
    assert client.get(f"/api/v1/lessons/{L_ARCHIVED}/quiz", headers=student).status_code == 404
    assert client.get("/api/v1/lessons/9999/quiz", headers=student).status_code == 404


def test_start_freezes_snapshot(tmp_path: Path) -> None:
    config = _seed(tmp_path)
    client = _client(config)
    headers = _login(client, "student")
    first = _start(client, headers)
    assert first["created"] is True
    questions = first["attempt"]["questions"]
    assert [item["question_id"] for item in questions] == _question_ids(config)
    _assert_no_answer(first)
    assert first["attempt"]["status"] == "in_progress"
    assert first["attempt"]["result"] is None

    # 再次开始：复用进行中作答，不重复扣题
    again = _start(client, headers)
    assert again["created"] is False
    assert again["attempt"]["id"] == first["attempt"]["id"]
    assert again["attempt"]["attempt_uid"] == first["attempt"]["attempt_uid"]

    # 题库改答案 + 改分值：在途作答必须按快照判分（旧答案 A、旧分值 5）
    question_id = questions[0]["question_id"]
    patch = client.patch(
        f"/api/v1/quiz/questions/{question_id}",
        json={"version": 1, "answer": ["B"], "score": 9},
        headers=_login(client, "root"),
    )
    assert patch.status_code == 200

    submitted = _submit(
        client,
        headers,
        first["attempt"],
        [{"question_id": question_id, "choices": ["A"]}],
    )
    assert submitted.status_code == 201, submitted.get_json()
    attempt = submitted.get_json()["data"]["attempt"]
    assert attempt["score_earned"] == 5  # 按快照：A 仍正确、仍 5 分
    assert attempt["score_total"] == 20


# ── 判分 ──────────────────────────────────────────────────────────────────


def test_submit_grades_set_equality(tmp_path: Path) -> None:
    config = _seed(tmp_path)
    client = _client(config)
    headers = _login(client, "student")
    attempt = _start(client, headers)["attempt"]
    ids = [item["question_id"] for item in attempt["questions"]]

    # 单选取 A（对）、多选顺序倒过来但集合相等（对）、判断答 F（错）
    response = _submit(
        client,
        headers,
        attempt,
        [
            {"question_id": ids[0], "choices": ["A"]},
            {"question_id": ids[1], "choices": ["C", "A"]},
            {"question_id": ids[2], "choices": ["F"]},
        ],
    )
    assert response.status_code == 201, response.get_json()
    body = response.get_json()
    assert body["data"]["created"] is True
    attempt_out = body["data"]["attempt"]
    assert attempt_out["status"] == "submitted"
    assert attempt_out["score_earned"] == 15
    assert attempt_out["score_total"] == 20
    assert attempt_out["percent"] == 75.0
    assert attempt_out["passed"] is True
    details = {item["question_id"]: item for item in attempt_out["result"]}
    assert details[ids[0]]["correct"] is True
    assert details[ids[1]]["correct"] is True
    assert details[ids[2]]["correct"] is False
    assert details[ids[2]]["score_earned"] == 0
    _assert_no_answer(body)


def test_submit_multiple_choice_has_no_partial_credit(tmp_path: Path) -> None:
    config = _seed(tmp_path)
    client = _client(config)
    headers = _login(client, "student")
    attempt = _start(client, headers)["attempt"]
    ids = [item["question_id"] for item in attempt["questions"]]

    response = _submit(
        client,
        headers,
        attempt,
        [
            {"question_id": ids[0], "choices": ["A"]},
            {"question_id": ids[1], "choices": ["A"]},  # 少选 → 0 分
            {"question_id": ids[2], "choices": ["T"]},
        ],
    )
    assert response.status_code == 201
    attempt_out = response.get_json()["data"]["attempt"]
    assert attempt_out["score_earned"] == 10
    assert attempt_out["percent"] == 50.0
    assert attempt_out["passed"] is False

    # 全选（含干扰项）同样 0 分
    second = _start(client, headers)["attempt"]  # 已提交后重新开始
    response = _submit(
        client,
        headers,
        second,
        [
            {"question_id": ids[0], "choices": ["A"]},
            {"question_id": ids[1], "choices": ["A", "B", "C"]},
            {"question_id": ids[2], "choices": ["T"]},
        ],
    )
    assert response.get_json()["data"]["attempt"]["score_earned"] == 10


def test_submit_unanswered_scores_zero(tmp_path: Path) -> None:
    config = _seed(tmp_path)
    client = _client(config)
    headers = _login(client, "student")
    attempt = _start(client, headers)["attempt"]
    ids = [item["question_id"] for item in attempt["questions"]]
    response = _submit(client, headers, attempt, [{"question_id": ids[0], "choices": ["A"]}])
    assert response.status_code == 201
    attempt_out = response.get_json()["data"]["attempt"]
    assert attempt_out["score_earned"] == 5
    missing = [item for item in attempt_out["result"] if not item["correct"]]
    assert len(missing) == 2


def test_submit_rejects_unknown_question_and_keeps_in_progress(tmp_path: Path) -> None:
    config = _seed(tmp_path)
    client = _client(config)
    headers = _login(client, "student")
    attempt = _start(client, headers)["attempt"]
    response = _submit(client, headers, attempt, [{"question_id": 99999, "choices": ["A"]}])
    assert response.status_code == 422
    assert response.get_json()["code"] == "VALIDATION_FAILED"

    engine = _engine(config)
    try:
        with Session(engine) as session:
            row = session.get(QuizAttempt, attempt["id"])
        assert row is not None and row.status == "in_progress"
    finally:
        engine.dispose()


# ── Idempotency-Key ───────────────────────────────────────────────────────


def test_submit_requires_idempotency_key(tmp_path: Path) -> None:
    config = _seed(tmp_path)
    client = _client(config)
    headers = _login(client, "student")
    attempt = _start(client, headers)["attempt"]
    answers = [{"question_id": attempt["questions"][0]["question_id"], "choices": ["A"]}]

    missing = client.post(
        f"/api/v1/quiz/attempts/{attempt['id']}/submit",
        json={"answers": answers},
        headers=headers,
    )
    assert missing.status_code == 422
    body = missing.get_json()
    assert body["code"] == "VALIDATION_FAILED"
    assert body["data"]["header"] == HEADER

    bad = client.post(
        f"/api/v1/quiz/attempts/{attempt['id']}/submit",
        json={"answers": answers},
        headers=dict(headers, **{HEADER: "short"}),
    )
    assert bad.status_code == 422

    mismatched = _submit(client, headers, attempt, answers, key="k" * 32)
    assert mismatched.status_code == 409
    assert mismatched.get_json()["data"]["reason"] == "idempotency_key_mismatch"


def test_submit_replay_returns_first_result(tmp_path: Path) -> None:
    config = _seed(tmp_path)
    client = _client(config)
    headers = _login(client, "student")
    attempt = _start(client, headers)["attempt"]
    answers = [{"question_id": attempt["questions"][0]["question_id"], "choices": ["A"]}]

    first = _submit(client, headers, attempt, answers)
    assert first.status_code == 201
    created = first.get_json()["data"]
    assert created["created"] is True
    assert created["attempt"]["score_earned"] == 5

    replay = _submit(client, headers, attempt, answers)
    assert replay.status_code == 200
    replayed = replay.get_json()["data"]
    assert replayed["created"] is False
    assert replayed["attempt"]["id"] == created["attempt"]["id"]
    assert replayed["attempt"]["score_earned"] == created["attempt"]["score_earned"]
    assert replayed["attempt"]["submitted_at"] == created["attempt"]["submitted_at"]

    engine = _engine(config)
    try:
        with Session(engine) as session:
            rows = list(session.execute(select(QuizAttempt)).scalars())
        assert len(rows) == 1
        assert rows[0].status == "submitted"
        assert rows[0].version == 2
    finally:
        engine.dispose()


def test_submit_replay_with_different_payload_conflicts(tmp_path: Path) -> None:
    config = _seed(tmp_path)
    client = _client(config)
    headers = _login(client, "student")
    attempt = _start(client, headers)["attempt"]
    ids = [item["question_id"] for item in attempt["questions"]]
    first = _submit(client, headers, attempt, [{"question_id": ids[0], "choices": ["A"]}])
    assert first.status_code == 201

    changed = _submit(client, headers, attempt, [{"question_id": ids[0], "choices": ["B"]}])
    assert changed.status_code == 409
    body = changed.get_json()
    assert body["code"] == "CONFLICT_RETRY"
    assert body["data"]["reason"] == "idempotency_key_reused"


def test_submit_twice_without_key_reuse_is_rejected(tmp_path: Path) -> None:
    config = _seed(tmp_path)
    client = _client(config)
    headers = _login(client, "student")
    attempt = _start(client, headers)["attempt"]
    ids = [item["question_id"] for item in attempt["questions"]]
    assert (
        _submit(client, headers, attempt, [{"question_id": ids[0], "choices": ["A"]}]).status_code
        == 201
    )

    # 换一个新 key 再次交卷：已提交状态不可重算 → 409（不会产生第二份成绩）
    second = _submit(
        client, headers, attempt, [{"question_id": ids[0], "choices": ["A"]}], key="n" * 32
    )
    assert second.status_code == 409
    assert second.get_json()["data"]["reason"] == "idempotency_key_mismatch"


def test_concurrent_submit_falls_back_to_replay(tmp_path: Path) -> None:
    """模拟并发：条件更新 rowcount=0 时回读首次结果，不报 500、不双计。"""
    config = _seed(tmp_path)
    client = _client(config)
    headers = _login(client, "student")
    attempt = _start(client, headers)["attempt"]
    ids = [item["question_id"] for item in attempt["questions"]]
    answers = [{"question_id": ids[0], "choices": ["A"]}]
    assert _submit(client, headers, attempt, answers).status_code == 201

    service = AssessService(config)
    result = service.submit(
        user_id=_user_id(config, "student"),
        attempt_id=attempt["id"],
        body={"answers": answers},
        key=attempt["attempt_uid"],
        can_preview=False,
    )
    assert result["created"] is False
    assert result["attempt"]["score_earned"] == 5


# ── 私有性与历史 ──────────────────────────────────────────────────────────


def test_attempt_visibility(tmp_path: Path) -> None:
    config = _seed(tmp_path)
    client = _client(config)
    owner = _login(client, "student")
    attempt = _start(client, owner)["attempt"]

    other = client.get(f"/api/v1/quiz/attempts/{attempt['id']}", headers=_login(client, "other"))
    assert other.status_code == 404
    assert other.get_json()["code"] == "NOT_FOUND"

    assert (
        client.get(
            f"/api/v1/quiz/attempts/{attempt['id']}", headers=_login(client, "root")
        ).status_code
        == 200
    )
    assert client.get("/api/v1/quiz/attempts/99999", headers=owner).status_code == 404

    submitted = _submit(
        client,
        owner,
        attempt,
        [{"question_id": attempt["questions"][0]["question_id"], "choices": ["A"]}],
    )
    assert submitted.status_code == 201
    detail = client.get(f"/api/v1/quiz/attempts/{attempt['id']}", headers=owner)
    assert detail.status_code == 200
    _assert_no_answer(detail.get_json())
    assert detail.get_json()["data"]["status"] == "submitted"


def test_history_pagination_and_filters(tmp_path: Path) -> None:
    config = _seed(tmp_path)
    client = _client(config)
    headers = _login(client, "student")
    for _ in range(3):
        attempt = _start(client, headers)["attempt"]
        _submit(
            client,
            headers,
            attempt,
            [{"question_id": attempt["questions"][0]["question_id"], "choices": ["A"]}],
        )
    page = client.get("/api/v1/my/quiz/attempts?page_size=2", headers=headers)
    assert page.status_code == 200
    data = page.get_json()["data"]
    assert data["total"] == 3 and len(data["items"]) == 2
    assert data["page"] == 1 and data["page_size"] == 2
    second = client.get(
        "/api/v1/my/quiz/attempts?page=2&page_size=2&lesson_id=1&status=submitted", headers=headers
    )
    assert len(second.get_json()["data"]["items"]) == 1
    assert client.get("/api/v1/my/quiz/attempts?status=bogus", headers=headers).status_code == 422
    assert client.get("/api/v1/my/quiz/attempts?page_size=101", headers=headers).status_code == 422


# ── 单元：幂等指纹与配置 ──────────────────────────────────────────────────


def test_payload_hash_is_stable_and_key_order_independent() -> None:
    left = {"a": 1, "b": {"c": 2, "d": 3}}
    right = {"b": {"d": 3, "c": 2}, "a": 1}
    assert payload_hash(left) == payload_hash(right)  # 对象键序无关
    assert payload_hash({"a": 1}) != payload_hash({"a": 2})


def test_pass_score_comes_from_config(tmp_path: Path) -> None:
    config = _seed(tmp_path, ZHY_QUIZ_PASS_SCORE="80")  # noqa: S106 - 及格线阈值，非口令
    client = _client(config)
    headers = _login(client, "student")
    assert (
        client.get(f"/api/v1/lessons/{L_PUBLIC}/quiz", headers=headers).get_json()["data"][
            "pass_score"
        ]
        == 80
    )
    attempt = _start(client, headers)["attempt"]
    ids = [item["question_id"] for item in attempt["questions"]]
    response = _submit(
        client,
        headers,
        attempt,
        [
            {"question_id": ids[0], "choices": ["A"]},
            {"question_id": ids[1], "choices": ["C", "A"]},
            {"question_id": ids[2], "choices": ["F"]},
        ],
    )
    attempt_out = response.get_json()["data"]["attempt"]
    assert attempt_out["percent"] == 75.0
    assert attempt_out["passed"] is False  # 75 < 80


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


def test_quiz_empty_after_all_questions_deleted(tmp_path: Path) -> None:
    config = _seed(tmp_path)
    client = _client(config)
    headers = _login(client, "root")
    for question_id in _question_ids(config):
        response = client.delete(
            f"/api/v1/quiz/questions/{question_id}", json={"version": 1}, headers=headers
        )
        assert response.status_code == 200
    engine = _engine(config)
    try:
        with Session(engine) as session:
            remaining = list(
                session.execute(select(QuizQuestion).where(QuizQuestion.deleted_at.is_(None)))
                .scalars()
                .all()
            )
        assert remaining == []  # 软删后不再参与组卷，但行仍在库中
        with Session(engine) as session:
            assert len(list(session.execute(select(QuizQuestion.id)).scalars())) == 3
    finally:
        engine.dispose()
    student = _login(client, "student")
    entry = client.get(f"/api/v1/lessons/{L_PUBLIC}/quiz", headers=student)
    assert entry.status_code == 404
    assert entry.get_json()["data"]["reason"] == "quiz_empty"
