"""学习档案（§10.7 · M2.13）。

口径（V3）：
- **预览不计入统计**：所有"完成率/学习时长"只读 `learn_course_progress` /
  `learn_progress`（它们由 flush 单写者过滤 `source='preview'` 后落成）；
  今日时长直接查 `learn_progress_log`，条件固定为
  `source = 'play' AND quarantined = false`（与 SQL 层口径一致，可审计）；
- **metric_version**：所有返回统一带 `zhy-progress-v2`，保证网关改造前后可对比；
- **异步投影**：`learn_course_progress` 由 `progress-flush` 异步维护，允许秒级滞后；
  读接口返回 `freshness_seconds`，前端据此展示"更新于 X 分钟前"；
- `GET /my/progress/gate` 是运维/自诊口：返回 `last_gw_seq`、已落库水位与滞后秒数。
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Any, NoReturn, cast

from flask import current_app
from sqlalchemy import case, func, or_, select
from sqlalchemy.orm import Session

from zhyonlineedu.core.codes import NOT_FOUND, VALIDATION_FAILED
from zhyonlineedu.core.config import Config
from zhyonlineedu.core.constants import ZHY_PROGRESS_METRIC_VERSION
from zhyonlineedu.core.db.engine import Database, ensure_database
from zhyonlineedu.core.db.models.catalog import (
    EduCourse,
    EduCourseChapter,
    EduCourseLesson,
    EduSubject,
)
from zhyonlineedu.core.db.models.learn import (
    DEFAULT_SHARD,
    LearnCourseProgress,
    LearnProgress,
    LearnProgressGate,
    LearnProgressLog,
    utcnow,
)
from zhyonlineedu.core.db.models.quiz import QuizAttempt
from zhyonlineedu.core.db.query import not_deleted
from zhyonlineedu.core.errors import AppError

PAGE_SIZE_DEFAULT = 20
PAGE_SIZE_MAX = 100
STATUSES = ("in_progress", "completed")
HEALTH_TOLERANCE_SEC = 60


def _iso(value: datetime | None) -> str | None:
    return value.isoformat(timespec="seconds") if value is not None else None


def _fail(message: str) -> NoReturn:
    raise AppError(VALIDATION_FAILED, data={"errors": [message]})


def _int(value: object, field: str, *, minimum: int, maximum: int | None = None) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        _fail(f"{field} 必须是整数")
    if value < minimum:
        _fail(f"{field} 不能小于 {minimum}")
    if maximum is not None and value > maximum:
        _fail(f"{field} 不能大于 {maximum}")
    return value


def _query_int(
    value: object, field: str, *, default: int, minimum: int, maximum: int | None = None
) -> int:
    if value is None or value == "":
        return default
    if isinstance(value, str) and value.lstrip("-").isdigit():
        value = int(value)
    return _int(value, field, minimum=minimum, maximum=maximum)


def _choice(value: object, field: str, allowed: tuple[str, ...]) -> str:
    if not isinstance(value, str) or value not in allowed:
        _fail(f"{field} 只能是 {'/'.join(allowed)}")
    return value


class ProfileService:
    """无状态服务：每个读动作自建短会话（读写分离时可直接切只读库）。"""

    def __init__(self, config: Config, *, db: Database | None = None) -> None:
        self.config = config
        self._db = db

    @property
    def db(self) -> Database:
        if self._db is None:
            self._db = ensure_database(self.config)
        return self._db

    # ── 列表（本人）─────────────────────────────────────────────────────

    def list_progress(self, user_id: int, query: Mapping[str, Any]) -> dict[str, Any]:
        page = _query_int(query.get("page"), "page", default=1, minimum=1)
        page_size = _query_int(
            query.get("page_size"),
            "page_size",
            default=PAGE_SIZE_DEFAULT,
            minimum=1,
            maximum=PAGE_SIZE_MAX,
        )
        statement = (
            select(LearnCourseProgress, EduCourse, EduSubject)
            .join(EduCourse, EduCourse.id == LearnCourseProgress.course_id)
            .outerjoin(EduSubject, EduSubject.id == EduCourse.subject_id)
            .where(LearnCourseProgress.user_id == user_id, not_deleted(EduCourse))
        )
        status = query.get("status")
        if status not in (None, ""):
            completed = _choice(status, "status", STATUSES) == "completed"
            statement = statement.where(LearnCourseProgress.completed.is_(completed))
        keyword = query.get("keyword")
        if keyword not in (None, ""):
            if not isinstance(keyword, str):
                _fail("keyword 必须是字符串")
            text = keyword.strip()
            if len(text) > 64:
                _fail("keyword 长度不能超过 64")
            if text:
                statement = statement.where(EduCourse.title.like(f"%{text}%"))

        now = utcnow()
        with Session(self.db.engine) as session:
            total = int(
                session.execute(select(func.count()).select_from(statement.subquery())).scalar_one()
            )
            rows = session.execute(
                statement.order_by(
                    LearnCourseProgress.last_watch_at.desc(), LearnCourseProgress.id.desc()
                )
                .offset((page - 1) * page_size)
                .limit(page_size)
            ).all()
            items = [
                self._course_out(progress, course, subject) for progress, course, subject in rows
            ]
            freshness = self._freshness(session, now)
        return {
            "items": items,
            "total": total,
            "page": page,
            "page_size": page_size,
            "metric_version": ZHY_PROGRESS_METRIC_VERSION,
            "freshness_seconds": freshness,
            "generated_at": _iso(now),
        }

    # ── 详情（本人；仅已学课时）─────────────────────────────────────────

    def detail(self, user_id: int, course_id: int) -> dict[str, Any]:
        now = utcnow()
        with Session(self.db.engine) as session:
            progress = session.execute(
                select(LearnCourseProgress).where(
                    LearnCourseProgress.user_id == user_id,
                    LearnCourseProgress.course_id == course_id,
                )
            ).scalar_one_or_none()
            if progress is None:
                raise AppError(
                    NOT_FOUND, data={"resource": "course_progress", "course_id": course_id}
                )
            course = session.execute(
                select(EduCourse).where(EduCourse.id == course_id, not_deleted(EduCourse))
            ).scalar_one_or_none()
            if course is None:
                raise AppError(NOT_FOUND, data={"resource": "course", "course_id": course_id})
            subject = session.execute(
                select(EduSubject).where(EduSubject.id == course.subject_id)
            ).scalar_one_or_none()

            rows = session.execute(
                select(LearnProgress, EduCourseLesson, EduCourseChapter)
                .join(EduCourseLesson, EduCourseLesson.id == LearnProgress.lesson_id)
                .join(EduCourseChapter, EduCourseChapter.id == EduCourseLesson.chapter_id)
                .where(
                    LearnProgress.user_id == user_id,
                    LearnProgress.course_id == course_id,
                    or_(
                        LearnProgress.watch_seconds > 0,
                        LearnProgress.last_position > 0,
                    ),
                    not_deleted(EduCourseLesson),
                    not_deleted(EduCourseChapter),
                )
                .order_by(
                    EduCourseChapter.sort_order,
                    EduCourseChapter.id,
                    EduCourseLesson.sort_order,
                    EduCourseLesson.id,
                )
            ).all()
            chapters: dict[int, dict[str, Any]] = {}
            for lesson_progress, lesson, chapter in rows:
                node = chapters.setdefault(
                    int(chapter.id),
                    {
                        "chapter_id": int(chapter.id),
                        "title": chapter.title,
                        "lessons": [],
                    },
                )
                node["lessons"].append(
                    {
                        "lesson_id": int(lesson.id),
                        "title": lesson.title,
                        "kind": lesson.kind,
                        "duration_sec": int(lesson.duration_sec),
                        "watch_seconds": int(lesson_progress.watch_seconds),
                        "speed_time": int(lesson_progress.speed_time),
                        "percent": round(float(lesson_progress.percent), 2),
                        "last_position": int(lesson_progress.last_position),
                        "completed": bool(lesson_progress.completed),
                        "completed_at": _iso(lesson_progress.completed_at),
                        "last_watch_at": _iso(lesson_progress.last_watch_at),
                    }
                )
            freshness = self._freshness(session, now)

        return {
            "course": self._course_out(progress, course, subject),
            "chapters": list(chapters.values()),
            "metric_version": ZHY_PROGRESS_METRIC_VERSION,
            "freshness_seconds": freshness,
            "generated_at": _iso(now),
        }

    # ── 首屏汇总（§23.5：≤2 请求）────────────────────────────────────────

    def summary(self, user_id: int) -> dict[str, Any]:
        now = utcnow()
        start_of_day = now.astimezone(UTC).replace(hour=0, minute=0, second=0, microsecond=0)
        with Session(self.db.engine) as session:
            aggregate = session.execute(
                select(
                    func.count(LearnCourseProgress.id),
                    func.coalesce(func.sum(LearnCourseProgress.watch_seconds), 0),
                    func.coalesce(func.sum(LearnCourseProgress.speed_time), 0),
                    func.coalesce(func.sum(LearnCourseProgress.completed_lessons), 0),
                    func.coalesce(
                        func.sum(case((LearnCourseProgress.completed.is_(True), 1), else_=0)), 0
                    ),
                ).where(LearnCourseProgress.user_id == user_id)
            ).one()
            today_watch = int(
                session.execute(
                    select(func.coalesce(func.sum(LearnProgressLog.played_seconds), 0)).where(
                        LearnProgressLog.user_id == user_id,
                        LearnProgressLog.source == "play",
                        LearnProgressLog.quarantined.is_(False),
                        LearnProgressLog.received_at >= start_of_day,
                    )
                ).scalar_one()
            )
            recent = session.execute(
                select(LearnCourseProgress, EduCourse, EduSubject)
                .join(EduCourse, EduCourse.id == LearnCourseProgress.course_id)
                .outerjoin(EduSubject, EduSubject.id == EduCourse.subject_id)
                .where(LearnCourseProgress.user_id == user_id, not_deleted(EduCourse))
                .order_by(LearnCourseProgress.last_watch_at.desc(), LearnCourseProgress.id.desc())
                .limit(3)
            ).all()
            quiz = session.execute(
                select(
                    func.count(QuizAttempt.id),
                    func.coalesce(func.sum(case((QuizAttempt.passed.is_(True), 1), else_=0)), 0),
                    func.coalesce(func.avg(QuizAttempt.percent), 0),
                    func.coalesce(func.max(QuizAttempt.percent), 0),
                ).where(QuizAttempt.user_id == user_id, QuizAttempt.status == "submitted")
            ).one()
            freshness = self._freshness(session, now)

        return {
            "user_id": user_id,
            "courses_started": int(aggregate[0]),
            "courses_completed": int(aggregate[4]),
            "lessons_completed": int(aggregate[3]),
            "watch_seconds": int(aggregate[1]),
            "speed_time": int(aggregate[2]),
            "today_watch_seconds": today_watch,
            "recent_courses": [
                self._course_out(progress, course, subject) for progress, course, subject in recent
            ],
            "quiz": {
                "attempts": int(quiz[0]),
                "passed": int(quiz[1]),
                "average_percent": round(float(quiz[2] or 0), 2),
                "best_percent": round(float(quiz[3] or 0), 2),
            },
            "metric_version": ZHY_PROGRESS_METRIC_VERSION,
            "freshness_seconds": freshness,
            "generated_at": _iso(now),
        }

    # ── 运维/自诊（system:manage）───────────────────────────────────────

    def gate(self) -> dict[str, Any]:
        now = utcnow()
        with Session(self.db.engine) as session:
            gate = session.execute(
                select(LearnProgressGate).where(LearnProgressGate.shard == DEFAULT_SHARD)
            ).scalar_one_or_none()
            pending = int(
                session.execute(
                    select(func.count())
                    .select_from(LearnProgressLog)
                    .where(LearnProgressLog.flushed_at.is_(None))
                ).scalar_one()
            )
            oldest = session.execute(
                select(func.min(LearnProgressLog.received_at)).where(
                    LearnProgressLog.flushed_at.is_(None)
                )
            ).scalar_one_or_none()
            flushed_max = int(
                session.execute(
                    select(func.coalesce(func.max(LearnProgressLog.gw_seq), 0)).where(
                        LearnProgressLog.flushed_at.is_not(None)
                    )
                ).scalar_one()
            )
        allocated = int(gate.last_gw_seq) if gate is not None else 0
        lag_seconds = max(0, int((now - oldest).total_seconds())) if oldest is not None else 0
        tolerance = max(
            HEALTH_TOLERANCE_SEC,
            self.config.get_int("progress.flush_interval_sec") * 3,
        )
        return {
            "shard": DEFAULT_SHARD,
            "writer_id": "" if gate is None else gate.writer_id,
            "leased_until": None if gate is None else _iso(gate.leased_until),
            "last_gw_seq": allocated,
            "flushed_max_gw_seq": flushed_max,
            "pending_logs": pending,
            "gap": max(0, allocated - flushed_max),
            "lag_seconds": lag_seconds,
            "last_flush_at": None if gate is None else _iso(gate.last_flush_at),
            "healthy": pending == 0 or lag_seconds <= tolerance,
            "metric_version": ZHY_PROGRESS_METRIC_VERSION,
            "generated_at": _iso(now),
        }

    # ── 内部 ────────────────────────────────────────────────────────────

    @staticmethod
    def _course_out(
        progress: LearnCourseProgress, course: EduCourse, subject: EduSubject | None
    ) -> dict[str, Any]:
        return {
            "course_id": int(course.id),
            "course_title": course.title,
            "subject_id": int(course.subject_id),
            "subject_name": None if subject is None else subject.name,
            "cover_url": course.cover_url,
            "watch_seconds": int(progress.watch_seconds),
            "speed_time": int(progress.speed_time),
            "total_lessons": int(progress.total_lessons),
            "completed_lessons": int(progress.completed_lessons),
            "total_duration_sec": int(progress.total_duration_sec),
            "percent": round(float(progress.percent), 2),
            "completed": bool(progress.completed),
            "resume_lesson_id": progress.resume_lesson_id,
            "resume_position": int(progress.resume_position),
            "first_watch_at": _iso(progress.first_watch_at),
            "last_watch_at": _iso(progress.last_watch_at),
            "updated_at": _iso(progress.updated_at),
        }

    @staticmethod
    def _freshness(session: Session, now: datetime) -> int:
        """待落库的有效流水（play + 未隔离）距当前秒数；无积压即 0。"""
        oldest = session.execute(
            select(func.min(LearnProgressLog.received_at)).where(
                LearnProgressLog.flushed_at.is_(None),
                LearnProgressLog.source == "play",
                LearnProgressLog.quarantined.is_(False),
            )
        ).scalar_one_or_none()
        if oldest is None:
            return 0
        return max(0, int((now - oldest).total_seconds()))


def profile_service() -> ProfileService:
    return cast(ProfileService, current_app.extensions["zhy_profile"])


__all__ = ["ProfileService", "profile_service"]
