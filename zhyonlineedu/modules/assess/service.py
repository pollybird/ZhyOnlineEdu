"""题库判分（§6.8 / §10.4 · M2.6）。

验收口径：
1. **答案不下发** —— 学员端任何响应（入口 / 开始作答 / 作答详情 / 交卷）只含题干、
   选项、逐题对错与解析，**不含** `answer` / `correct_answer` / `answer_key`；
   管理端（`course:edit`）才返回标准答案；
2. **重试不双成绩** —— `POST /quiz/attempts/{id}/submit` 强制
   `Idempotency-Key: {attempt_uid}`；同 key 重放返回首次结果（HTTP 201 → 200），
   底层靠 `attempt_uid` 唯一键 + `status='in_progress'` 条件更新，
   并发同 key 也只落一份成绩。

判分口径（写进接口描述，避免前端自行解释）：
- 单选 / 判断：所选集合恰好 1 个且等于答案；
- 多选：所选集合与答案集合**完全相等**才得分，无部分分；
- 未作答的题按 0 分；未知或重复的 `question_id` 直接 422。

快照口径：作答开始时冻结 `question_snapshot`（可下发）与 `answer_key`（不可下发），
交卷只读快照 → 题库事后修改/删除不影响在途作答。
"""

from __future__ import annotations

import json
import secrets
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from decimal import ROUND_HALF_UP, Decimal
from typing import Any, NoReturn, cast

from sqlalchemy import func, select, update
from sqlalchemy.engine import CursorResult
from sqlalchemy.orm import Session

from zhyonlineedu.core.codes import (
    ACCESS_LEVEL_REQUIRED,
    CONFLICT_RETRY,
    NOT_FOUND,
    VALIDATION_FAILED,
)
from zhyonlineedu.core.config import Config
from zhyonlineedu.core.db.engine import Database, ensure_database
from zhyonlineedu.core.db.models.catalog import EduCourse, EduCourseLesson
from zhyonlineedu.core.db.models.quiz import (
    ATTEMPT_STATUSES,
    QUESTION_KINDS,
    QuizAttempt,
    QuizQuestion,
)
from zhyonlineedu.core.db.query import not_deleted
from zhyonlineedu.core.errors import AppError
from zhyonlineedu.core.idempotency import payload_hash
from zhyonlineedu.core.mode import AccessSpec, decide_access, get_mode_context
from zhyonlineedu.modules.commerce.service import active_level
from zhyonlineedu.modules.users.service import is_class_member

PAGE_SIZE_DEFAULT = 20
PAGE_SIZE_MAX = 100
STEM_MAX = 4000
EXPLANATION_MAX = 8000
OPTION_MIN = 2
OPTION_MAX = 8
SCORE_MIN = 1
SCORE_MAX = 100
SORT_STEP = 1024

# 判断题选项由服务端固定生成，客户端自定义一律拒收（防"两个都答对"）
JUDGE_OPTIONS: tuple[dict[str, str], ...] = (
    {"key": "T", "text": "正确"},
    {"key": "F", "text": "错误"},
)


def _utcnow() -> datetime:
    return datetime.now(UTC)


def _iso(value: datetime | None) -> str | None:
    return value.isoformat(timespec="seconds") if value is not None else None


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
    number = value
    if number < minimum:
        _fail(f"{field} 不能小于 {minimum}")
    if maximum is not None and number > maximum:
        _fail(f"{field} 不能大于 {maximum}")
    return number


def _choice(value: object, field: str, allowed: Sequence[str]) -> str:
    if not isinstance(value, str) or value not in allowed:
        _fail(f"{field} 只能是 {'/'.join(allowed)}")
    return value


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


def _percent(earned: int, total: int) -> Decimal:
    if total <= 0:
        return Decimal("0.00")
    value = Decimal(earned * 100) / Decimal(total)
    return value.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


# ── 题目字段规约 ──────────────────────────────────────────────────────────


def _options(kind: str, value: object) -> list[dict[str, str]]:
    """校验选项；判断题忽略入参并强制固定选项。"""
    if kind == "judge":
        return [dict(item) for item in JUDGE_OPTIONS]
    if not isinstance(value, list) or not (OPTION_MIN <= len(value) <= OPTION_MAX):
        _fail(f"options 必须是 {OPTION_MIN}~{OPTION_MAX} 项的数组")
    options: list[dict[str, str]] = []
    seen: set[str] = set()
    for item in value:
        if not isinstance(item, Mapping):
            _fail("options 每项必须是 {key,text} 对象")
        key = _text(item.get("key"), "options.key", max_len=4)
        text = _text(item.get("text"), "options.text", max_len=500)
        if key in seen:
            _fail(f"options.key 不能重复：{key}")
        seen.add(key)
        options.append({"key": key, "text": text})
    return options


def _answer(kind: str, value: object, options: Sequence[Mapping[str, str]]) -> list[str]:
    if not isinstance(value, list) or not value:
        _fail("answer 必须是非空数组")
    keys = [item for item in value if isinstance(item, str)]
    if len(keys) != len(value):
        _fail("answer 必须是选项键数组")
    if len(keys) != len(set(keys)):
        _fail("answer 不能包含重复键")
    known = {option["key"] for option in options}
    unknown = sorted(set(keys) - known)
    if unknown:
        _fail(f"answer 含未知选项键：{','.join(unknown)}")
    if kind in ("single", "judge") and len(keys) != 1:
        _fail(f"{kind} 题答案必须恰好 1 项")
    if kind == "multiple" and len(keys) < 2:
        _fail("multiple 题答案至少 2 项")
    return sorted(keys)


def _dumps(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _loads(raw: str | None) -> Any:
    if raw is None:
        return None
    return json.loads(raw)


def _question_out(row: QuizQuestion) -> dict[str, Any]:
    """管理端视图：含标准答案与解析。"""
    return {
        "id": row.id,
        "course_id": row.course_id,
        "lesson_id": row.lesson_id,
        "kind": row.kind,
        "stem": row.stem,
        "options": _loads(row.options),
        "answer": _loads(row.answer),
        "explanation": row.explanation,
        "score": row.score,
        "sort_order": row.sort_order,
        "is_enabled": bool(row.is_enabled),
        "version": row.version,
        "created_at": _iso(row.created_at),
        "updated_at": _iso(row.updated_at),
    }


def _attempt_out(row: QuizAttempt) -> dict[str, Any]:
    """学员端视图：快照 + 逐题对错，**不含**任何答案字段。"""
    return {
        "id": row.id,
        "attempt_uid": row.attempt_uid,
        "lesson_id": row.lesson_id,
        "course_id": row.course_id,
        "status": row.status,
        "question_count": row.question_count,
        "score_total": row.score_total,
        "score_earned": row.score_earned,
        "percent": round(float(row.percent), 2),
        "passed": bool(row.passed),
        "questions": _loads(row.question_snapshot),
        "result": _loads(row.result_json),
        "started_at": _iso(row.started_at),
        "submitted_at": _iso(row.submitted_at),
    }


def _attempt_brief(row: QuizAttempt) -> dict[str, Any]:
    return {
        "id": row.id,
        "status": row.status,
        "score_earned": row.score_earned,
        "score_total": row.score_total,
        "percent": round(float(row.percent), 2),
        "passed": bool(row.passed),
        "started_at": _iso(row.started_at),
        "submitted_at": _iso(row.submitted_at),
    }


class AssessService:
    """无状态服务：每个动作自建短会话，交卷只做一次条件更新。"""

    def __init__(self, config: Config, *, db: Database | None = None) -> None:
        self.config = config
        self._db = db

    @property
    def db(self) -> Database:
        if self._db is None:
            self._db = ensure_database(self.config)
        return self._db

    @property
    def pass_score(self) -> int:
        return self.config.get_int("quiz.pass_score")

    # ── 题库（管理端，course:edit）──────────────────────────────────────

    def list_questions(self, query: Mapping[str, Any]) -> dict[str, Any]:
        page = _query_int(query.get("page"), "page", default=1, minimum=1)
        page_size = _query_int(
            query.get("page_size"),
            "page_size",
            default=PAGE_SIZE_DEFAULT,
            minimum=1,
            maximum=PAGE_SIZE_MAX,
        )
        with Session(self.db.engine) as session:
            statement = select(QuizQuestion).where(not_deleted(QuizQuestion))
            course_id = query.get("course_id")
            if course_id not in (None, ""):
                statement = statement.where(
                    QuizQuestion.course_id
                    == _query_int(course_id, "course_id", default=0, minimum=1)
                )
            lesson_id = query.get("lesson_id")
            if lesson_id not in (None, ""):
                statement = statement.where(
                    QuizQuestion.lesson_id
                    == _query_int(lesson_id, "lesson_id", default=0, minimum=1)
                )
            kind = query.get("kind")
            if kind not in (None, ""):
                statement = statement.where(
                    QuizQuestion.kind == _choice(kind, "kind", QUESTION_KINDS)
                )
            enabled = query.get("is_enabled")
            if enabled not in (None, ""):
                flag = _choice(enabled, "is_enabled", ("true", "false"))
                statement = statement.where(QuizQuestion.is_enabled == (flag == "true"))
            total = int(
                session.execute(select(func.count()).select_from(statement.subquery())).scalar_one()
            )
            rows = (
                session.execute(
                    statement.order_by(
                        QuizQuestion.lesson_id, QuizQuestion.sort_order, QuizQuestion.id
                    )
                    .offset((page - 1) * page_size)
                    .limit(page_size)
                )
                .scalars()
                .all()
            )
        return {
            "items": [_question_out(row) for row in rows],
            "total": total,
            "page": page,
            "page_size": page_size,
        }

    def create_question(self, body: Mapping[str, Any]) -> dict[str, Any]:
        lesson_id = _int(body.get("lesson_id"), "lesson_id", minimum=1)
        kind = _choice(body.get("kind", "single"), "kind", QUESTION_KINDS)
        stem = _text(body.get("stem"), "stem", max_len=STEM_MAX)
        options = _options(kind, body.get("options"))
        answer = _answer(kind, body.get("answer"), options)
        explanation = _optional_text(
            body.get("explanation"), "explanation", max_len=EXPLANATION_MAX
        )
        score = _int(body.get("score", 5), "score", minimum=SCORE_MIN, maximum=SCORE_MAX)
        is_enabled = bool(body.get("is_enabled", True))
        with Session(self.db.engine) as session:
            lesson, course = self._lesson(session, lesson_id, can_preview=True)
            next_sort = int(
                session.execute(
                    select(func.coalesce(func.max(QuizQuestion.sort_order), 0)).where(
                        not_deleted(QuizQuestion), QuizQuestion.lesson_id == lesson_id
                    )
                ).scalar_one()
            )
            row = QuizQuestion(
                course_id=course.id,
                lesson_id=lesson.id,
                kind=kind,
                stem=stem,
                options=_dumps(options),
                answer=_dumps(answer),
                explanation=explanation,
                score=score,
                sort_order=next_sort + SORT_STEP,
                is_enabled=is_enabled,
                created_at=_utcnow(),
                updated_at=_utcnow(),
            )
            session.add(row)
            session.commit()
            return _question_out(row)

    def update_question(self, question_id: int, body: Mapping[str, Any]) -> dict[str, Any]:
        version = _version(body)
        values: dict[str, Any] = {}
        with Session(self.db.engine) as session:
            row = self._question(session, question_id)
            kind = _choice(body.get("kind", row.kind), "kind", QUESTION_KINDS)
            if "kind" in body:
                values["kind"] = kind
            if "stem" in body:
                values["stem"] = _text(body.get("stem"), "stem", max_len=STEM_MAX)
            if "answer" in body or "options" in body or "kind" in body:
                raw_options = body["options"] if "options" in body else _loads(row.options)
                options = _options(kind, raw_options)
                raw_answer = body["answer"] if "answer" in body else _loads(row.answer)
                values["options"] = _dumps(options)
                values["answer"] = _dumps(_answer(kind, raw_answer, options))
            if "explanation" in body:
                values["explanation"] = _optional_text(
                    body.get("explanation"), "explanation", max_len=EXPLANATION_MAX
                )
            if "score" in body:
                values["score"] = _int(
                    body.get("score"), "score", minimum=SCORE_MIN, maximum=SCORE_MAX
                )
            if "is_enabled" in body:
                values["is_enabled"] = bool(body.get("is_enabled"))
            if not values:
                _fail("没有可更新的字段")
            result = cast(
                CursorResult[Any],
                session.execute(
                    update(QuizQuestion)
                    .where(
                        QuizQuestion.id == row.id,
                        QuizQuestion.version == version,
                        not_deleted(QuizQuestion),
                    )
                    .values(**values, version=QuizQuestion.version + 1, updated_at=_utcnow())
                ),
            )
            if result.rowcount != 1:
                raise AppError(
                    CONFLICT_RETRY,
                    data={"resource": "question", "id": question_id, "expected_version": version},
                )
            session.commit()
            session.expire_all()
            return _question_out(self._question(session, question_id))

    def delete_question(self, question_id: int, body: Mapping[str, Any]) -> dict[str, Any]:
        version = _version(body)
        with Session(self.db.engine) as session:
            row = self._question(session, question_id)
            if row.version != version:
                raise AppError(
                    CONFLICT_RETRY,
                    data={"resource": "question", "id": question_id, "expected_version": version},
                )
            next_seq = int(
                session.execute(
                    select(func.coalesce(func.max(QuizQuestion.delete_seq), 0))
                ).scalar_one()
            )
            row.deleted_at = _utcnow()
            row.delete_seq = next_seq + 1
            row.version = int(row.version) + 1
            session.commit()
        return {"id": question_id, "deleted": True}

    # ── 作答（学员端）────────────────────────────────────────────────────

    def entry(self, *, user_id: int, lesson_id: int, can_preview: bool) -> dict[str, Any]:
        with Session(self.db.engine) as session:
            lesson, course = self._lesson(
                session, lesson_id, can_preview=can_preview, user_id=user_id
            )
            questions = self._enabled_questions(session, lesson.id)
            if not questions:
                raise AppError(
                    NOT_FOUND,
                    data={"resource": "quiz", "lesson_id": lesson_id, "reason": "quiz_empty"},
                )
            current = (
                session.execute(
                    select(QuizAttempt)
                    .where(
                        QuizAttempt.user_id == user_id,
                        QuizAttempt.lesson_id == lesson_id,
                        QuizAttempt.status == "in_progress",
                    )
                    .order_by(QuizAttempt.id.desc())
                )
                .scalars()
                .first()
            )
            last = (
                session.execute(
                    select(QuizAttempt)
                    .where(
                        QuizAttempt.user_id == user_id,
                        QuizAttempt.lesson_id == lesson_id,
                        QuizAttempt.status == "submitted",
                    )
                    .order_by(QuizAttempt.id.desc())
                )
                .scalars()
                .first()
            )
        return {
            "lesson_id": lesson.id,
            "course_id": course.id,
            "question_count": len(questions),
            "score_total": sum(int(row.score) for row in questions),
            "pass_score": self.pass_score,
            "attempt": _attempt_out(current) if current is not None else None,
            "last": _attempt_brief(last) if last is not None else None,
        }

    def start(self, *, user_id: int, lesson_id: int, can_preview: bool) -> dict[str, Any]:
        with Session(self.db.engine) as session:
            lesson, course = self._lesson(
                session, lesson_id, can_preview=can_preview, user_id=user_id
            )
            existing = (
                session.execute(
                    select(QuizAttempt)
                    .where(
                        QuizAttempt.user_id == user_id,
                        QuizAttempt.lesson_id == lesson.id,
                        QuizAttempt.status == "in_progress",
                    )
                    .order_by(QuizAttempt.id.desc())
                )
                .scalars()
                .first()
            )
            if existing is not None:
                return {"attempt": _attempt_out(existing), "created": False}

            questions = self._enabled_questions(session, lesson.id)
            if not questions:
                raise AppError(
                    NOT_FOUND,
                    data={"resource": "quiz", "lesson_id": lesson_id, "reason": "quiz_empty"},
                )
            snapshot = [
                {
                    "question_id": row.id,
                    "kind": row.kind,
                    "stem": row.stem,
                    "options": _loads(row.options),
                    "score": row.score,
                }
                for row in questions
            ]
            answer_key = [
                {
                    "question_id": row.id,
                    "choices": _loads(row.answer),
                    "score": row.score,
                    "explanation": row.explanation,
                }
                for row in questions
            ]
            now = _utcnow()
            row = QuizAttempt(
                attempt_uid=secrets.token_hex(16),
                user_id=user_id,
                lesson_id=lesson.id,
                course_id=course.id,
                status="in_progress",
                question_count=len(snapshot),
                score_total=sum(int(item["score"]) for item in snapshot),
                score_earned=0,
                percent=Decimal("0.00"),
                passed=False,
                question_snapshot=_dumps(snapshot),
                answer_key=_dumps(answer_key),
                started_at=now,
                created_at=now,
                updated_at=now,
            )
            session.add(row)
            session.commit()
            return {"attempt": _attempt_out(row), "created": True}

    def submit(
        self,
        *,
        user_id: int,
        attempt_id: int,
        body: Mapping[str, Any],
        key: str,
        can_preview: bool,
    ) -> dict[str, Any]:
        answers = self._answers(body)
        digest = _digest(attempt_id, answers)
        with Session(self.db.engine) as session:
            row = self._attempt(session, attempt_id, user_id=user_id, can_preview=can_preview)
            if row.attempt_uid != key:
                raise AppError(
                    CONFLICT_RETRY,
                    data={
                        "resource": "attempt",
                        "id": attempt_id,
                        "reason": "idempotency_key_mismatch",
                    },
                )
            replayed = self._replay(session, row, digest=digest)
            if replayed is not None:
                return replayed
            if row.status != "in_progress":
                raise AppError(
                    CONFLICT_RETRY,
                    data={"resource": "attempt", "id": attempt_id, "status": row.status},
                )

            answer_key: list[dict[str, Any]] = _loads(row.answer_key)
            known = {int(item["question_id"]) for item in answer_key}
            unknown = sorted(set(answers) - known)
            if unknown:
                raise AppError(
                    VALIDATION_FAILED,
                    data={"errors": [f"answers 含未知 question_id：{unknown}"]},
                )

            earned = 0
            details: list[dict[str, Any]] = []
            for item in answer_key:
                question_id = int(item["question_id"])
                picks = answers.get(question_id, set())
                expected = {str(key) for key in item["choices"]}
                correct = picks == expected
                score = int(item["score"])
                gained = score if correct else 0
                earned += gained
                details.append(
                    {
                        "question_id": question_id,
                        "correct": correct,
                        "score": score,
                        "score_earned": gained,
                        "explanation": item.get("explanation"),
                    }
                )
            percent = _percent(earned, int(row.score_total))
            now = _utcnow()
            values = {
                "status": "submitted",
                "score_earned": earned,
                "percent": percent,
                "passed": percent >= Decimal(self.pass_score),
                "answers": _dumps(
                    [
                        {"question_id": question_id, "choices": sorted(picks)}
                        for question_id, picks in sorted(answers.items())
                    ]
                ),
                "result_json": _dumps(details),
                "request_hash": digest,
                "submitted_at": now,
                "updated_at": now,
            }
            result = cast(
                CursorResult[Any],
                session.execute(
                    update(QuizAttempt)
                    .where(
                        QuizAttempt.id == attempt_id,
                        QuizAttempt.status == "in_progress",
                        QuizAttempt.attempt_uid == key,
                    )
                    .values(**values, version=QuizAttempt.version + 1)
                ),
            )
            session.commit()
            if result.rowcount != 1:
                # 并发同 key：另一请求已先落库 → 按幂等语义返回首次结果
                session.expire_all()
                row = self._attempt(session, attempt_id, user_id=user_id, can_preview=can_preview)
                replayed = self._replay(session, row, digest=digest)
                if replayed is not None:
                    return replayed
                raise AppError(
                    CONFLICT_RETRY,
                    data={"resource": "attempt", "id": attempt_id, "status": row.status},
                )
            session.expire_all()
            fresh = self._attempt(session, attempt_id, user_id=user_id, can_preview=can_preview)
            return {"attempt": _attempt_out(fresh), "created": True}

    def get_attempt(self, *, user_id: int, attempt_id: int, can_preview: bool) -> dict[str, Any]:
        with Session(self.db.engine) as session:
            row = self._attempt(session, attempt_id, user_id=user_id, can_preview=can_preview)
        return _attempt_out(row)

    def history(self, *, user_id: int, query: Mapping[str, Any]) -> dict[str, Any]:
        page = _query_int(query.get("page"), "page", default=1, minimum=1)
        page_size = _query_int(
            query.get("page_size"),
            "page_size",
            default=PAGE_SIZE_DEFAULT,
            minimum=1,
            maximum=PAGE_SIZE_MAX,
        )
        with Session(self.db.engine) as session:
            statement = select(QuizAttempt).where(QuizAttempt.user_id == user_id)
            lesson_id = query.get("lesson_id")
            if lesson_id not in (None, ""):
                statement = statement.where(
                    QuizAttempt.lesson_id
                    == _query_int(lesson_id, "lesson_id", default=0, minimum=1)
                )
            status = query.get("status")
            if status not in (None, ""):
                statement = statement.where(
                    QuizAttempt.status == _choice(status, "status", ATTEMPT_STATUSES)
                )
            total = int(
                session.execute(select(func.count()).select_from(statement.subquery())).scalar_one()
            )
            rows = (
                session.execute(
                    statement.order_by(QuizAttempt.id.desc())
                    .offset((page - 1) * page_size)
                    .limit(page_size)
                )
                .scalars()
                .all()
            )
        return {
            "items": [_attempt_brief(row) for row in rows],
            "total": total,
            "page": page,
            "page_size": page_size,
        }

    # ── 内部 ────────────────────────────────────────────────────────────

    def _lesson(
        self, session: Session, lesson_id: int, *, can_preview: bool, user_id: int | None = None
    ) -> tuple[EduCourseLesson, EduCourse]:
        """访问判定与 M2.5 播放票据同口径（§9.4 决策表 1~14 唯一入口）。

        - `member` → 需要有效会员订阅（M2.12 `active_sub()`，等级 ≥ 课程要求）；
        - `class` → 内部模式需任一启用班级成员，对外模式 `MODE_ACCESS_DENIED`；
        - 课程 `mode` 与当前模式不符 → `MODE_COURSE_MISMATCH`。
        """
        lesson = session.execute(
            select(EduCourseLesson).where(
                EduCourseLesson.id == lesson_id, not_deleted(EduCourseLesson)
            )
        ).scalar_one_or_none()
        if lesson is None:
            raise AppError(NOT_FOUND, data={"resource": "lesson", "id": lesson_id})
        course = session.execute(
            select(EduCourse).where(EduCourse.id == lesson.course_id, not_deleted(EduCourse))
        ).scalar_one_or_none()
        if course is None or course.status == "archived":
            raise AppError(NOT_FOUND, data={"resource": "lesson", "id": lesson_id})

        state = get_mode_context(self.config, db=self.db).snapshot()
        class_member = False
        if course.access_type == "class" and not can_preview and user_id is not None:
            class_member = is_class_member(session, user_id)
        member_level = (
            active_level(session, user_id)
            if course.access_type == "member" and not can_preview and user_id is not None
            else None
        )
        decision = decide_access(
            AccessSpec(
                mode=state.mode,
                epoch=state.epoch,
                course_mode=course.mode,
                course_status=course.status,
                access_type=course.access_type,
                lesson_is_free=bool(lesson.is_free),
                can_preview=can_preview,
                class_member=class_member,
                member_level=member_level,
                required_level=1,
            )
        )
        if not decision.allowed:
            data: dict[str, object] = {
                "resource": "course",
                "id": course.id,
                "access_type": course.access_type,
                "mode": state.mode,
                "epoch": state.epoch,
                "reason": decision.reason,
            }
            if course.status != "published":
                data["status"] = course.status
            raise AppError(decision.code or ACCESS_LEVEL_REQUIRED, data=data)
        return lesson, course

    @staticmethod
    def _question(session: Session, question_id: int) -> QuizQuestion:
        row = session.execute(
            select(QuizQuestion).where(QuizQuestion.id == question_id, not_deleted(QuizQuestion))
        ).scalar_one_or_none()
        if row is None:
            raise AppError(NOT_FOUND, data={"resource": "question", "id": question_id})
        return row

    @staticmethod
    def _enabled_questions(session: Session, lesson_id: int) -> list[QuizQuestion]:
        return list(
            session.execute(
                select(QuizQuestion)
                .where(
                    not_deleted(QuizQuestion),
                    QuizQuestion.lesson_id == lesson_id,
                    QuizQuestion.is_enabled.is_(True),
                )
                .order_by(QuizQuestion.sort_order, QuizQuestion.id)
            )
            .scalars()
            .all()
        )

    @staticmethod
    def _attempt(
        session: Session, attempt_id: int, *, user_id: int, can_preview: bool
    ) -> QuizAttempt:
        row = session.execute(
            select(QuizAttempt).where(QuizAttempt.id == attempt_id)
        ).scalar_one_or_none()
        # 他人作答统一 404，避免通过状态码探测他人提交情况
        if row is None or (row.user_id != user_id and not can_preview):
            raise AppError(NOT_FOUND, data={"resource": "attempt", "id": attempt_id})
        return row

    @staticmethod
    def _replay(session: Session, row: QuizAttempt, *, digest: str) -> dict[str, Any] | None:
        """已提交：同 payload 视为幂等重放，不同 payload 视为 key 误用。"""
        if row.status != "submitted":
            return None
        if row.request_hash != digest:
            raise AppError(
                CONFLICT_RETRY,
                data={
                    "resource": "attempt",
                    "id": row.id,
                    "reason": "idempotency_key_reused",
                },
            )
        session.expire_all()
        return {"attempt": _attempt_out(row), "created": False}

    @staticmethod
    def _answers(body: Mapping[str, Any]) -> dict[int, set[str]]:
        raw = body.get("answers")
        if not isinstance(raw, list) or not raw:
            _fail("answers 必须是非空数组")
        parsed: dict[int, set[str]] = {}
        for item in raw:
            if not isinstance(item, Mapping):
                _fail("answers 每项必须是 {question_id,choices} 对象")
            question_id = _int(item.get("question_id"), "question_id", minimum=1)
            if question_id in parsed:
                _fail(f"answers 含重复 question_id：{question_id}")
            choices = item.get("choices")
            if not isinstance(choices, list) or not choices:
                _fail("choices 必须是非空字符串数组")
            if any(not isinstance(choice, str) or choice == "" for choice in choices):
                _fail("choices 每项必须是非空字符串")
            parsed[question_id] = set(choices)
        return parsed


def _digest(attempt_id: int, answers: Mapping[int, set[str]]) -> str:
    """幂等指纹：题目 + 排序后的选项集合，与提交顺序无关。"""
    payload = {
        "attempt_id": attempt_id,
        "answers": [
            {"question_id": question_id, "choices": sorted(choices)}
            for question_id, choices in sorted(answers.items())
        ],
    }
    return payload_hash(payload)


__all__ = ["AssessService", "JUDGE_OPTIONS"]
