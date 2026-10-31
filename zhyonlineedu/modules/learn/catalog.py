"""学员端课程目录（§10.1 / §14 / M2.14）。

与管理端 `/courses`（`course:edit`）严格分离：
- **只暴露 `published`** 课程，且学科未删除、启用，并按当前场景模式过滤
  （`EduCourse.mode` 必须为 `both` 或当前模式）——这是“模式隔离”在学员端的第一层；
- 列表一次返回课程卡片所需信息（课时数 / 总时长 / 首个可播课时 / 本人进度），
  避免首屏 N+1；详情返回章与课时，并叠加每课时进度；
- `access_ok` 只是 UI 提示（`public` / 有效会员 / 班级成员），**最终裁决始终在
  `POST /media/tickets`**；未授权时前端展示“无权限”态并引导开通会员；
- 进度只读 `learn_progress`（单课）与 `learn_course_progress`（课程投影），
  预览流水不入统计；响应统一带 `metric_version` 与 `freshness_seconds`。
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime
from typing import Any, NoReturn

from flask import current_app
from sqlalchemy import func, select
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
    LearnCourseProgress,
    LearnProgress,
    LearnProgressLog,
    utcnow,
)
from zhyonlineedu.core.db.models.media import MediaAsset
from zhyonlineedu.core.db.query import not_deleted
from zhyonlineedu.core.errors import AppError
from zhyonlineedu.core.mode import get_mode_context
from zhyonlineedu.modules.commerce.service import active_level
from zhyonlineedu.modules.users.service import is_class_member

PAGE_SIZE_DEFAULT = 20
PAGE_SIZE_MAX = 100
REQUIRED_MEMBER_LEVEL = 1


def _iso(value: datetime | None) -> str | None:
    return value.isoformat(timespec="seconds") if value is not None else None


def _fail(message: str) -> NoReturn:
    raise AppError(VALIDATION_FAILED, data={"errors": [message]})


def _query_int(
    value: object, field: str, *, default: int, minimum: int, maximum: int | None = None
) -> int:
    if value is None or value == "":
        return default
    if isinstance(value, str) and value.lstrip("-").isdigit():
        value = int(value)
    if isinstance(value, bool) or not isinstance(value, int):
        _fail(f"{field} 必须是整数")
    if value < minimum:
        _fail(f"{field} 不能小于 {minimum}")
    if maximum is not None and value > maximum:
        _fail(f"{field} 不能大于 {maximum}")
    return value


def _keyword(value: object) -> str | None:
    if value in (None, ""):
        return None
    if not isinstance(value, str):
        _fail("keyword 必须是字符串")
    text = value.strip()
    if len(text) > 64:
        _fail("keyword 长度不能超过 64")
    return text or None


def _subject_id(value: object) -> int | None:
    if value in (None, ""):
        return None
    if isinstance(value, str) and value.lstrip("-").isdigit():
        value = int(value)
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        _fail("subject_id 必须是正整数")
    return value


def _lesson_progress_out(row: LearnProgress) -> dict[str, Any]:
    return {
        "watch_seconds": int(row.watch_seconds),
        "percent": round(float(row.percent), 2),
        "completed": bool(row.completed),
        "last_position": int(row.last_position),
        "last_watch_at": _iso(row.last_watch_at),
    }


def _course_progress_out(row: LearnCourseProgress) -> dict[str, Any]:
    return {
        "watch_seconds": int(row.watch_seconds),
        "percent": round(float(row.percent), 2),
        "completed": bool(row.completed),
        "completed_lessons": int(row.completed_lessons),
        "resume_lesson_id": row.resume_lesson_id,
        "resume_position": int(row.resume_position),
        "last_watch_at": _iso(row.last_watch_at),
    }


class StudentCatalog:
    """学员端只读目录服务；每个读动作自建短会话，不持有状态。"""

    def __init__(self, config: Config, *, db: Database | None = None) -> None:
        self.config = config
        self._db = db

    @property
    def db(self) -> Database:
        if self._db is None:
            self._db = ensure_database(self.config)
        return self._db

    # ── 列表 ─────────────────────────────────────────────────────────────

    def list_courses(self, user_id: int, query: Mapping[str, Any]) -> dict[str, Any]:
        page = _query_int(query.get("page"), "page", default=1, minimum=1)
        page_size = _query_int(
            query.get("page_size"),
            "page_size",
            default=PAGE_SIZE_DEFAULT,
            minimum=1,
            maximum=PAGE_SIZE_MAX,
        )
        subject_id = _subject_id(query.get("subject_id"))
        keyword = _keyword(query.get("keyword"))
        mode = get_mode_context(self.config, db=self.db).snapshot().mode

        statement = (
            select(EduCourse, EduSubject)
            .join(EduSubject, EduSubject.id == EduCourse.subject_id)
            .where(
                not_deleted(EduCourse),
                not_deleted(EduSubject),
                EduSubject.is_enabled.is_(True),
                EduCourse.status == "published",
                EduCourse.mode.in_((mode, "both")),
            )
        )
        if subject_id is not None:
            statement = statement.where(EduCourse.subject_id == subject_id)
        if keyword is not None:
            statement = statement.where(EduCourse.title.like(f"%{keyword}%"))

        now = utcnow()
        with Session(self.db.engine) as session:
            total = int(
                session.execute(select(func.count()).select_from(statement.subquery())).scalar_one()
            )
            rows = session.execute(
                statement.order_by(EduCourse.sort_order, EduCourse.id)
                .offset((page - 1) * page_size)
                .limit(page_size)
            ).all()
            course_ids = [int(course.id) for course, _subject in rows]
            stats = self._lesson_stats(session, course_ids)
            progresses = self._course_progress(session, user_id, course_ids)
            access = self._access_flags(session, user_id)
            freshness = self._freshness(session, now)
            items = [
                self._course_out(
                    course,
                    subject,
                    stats.get(int(course.id), {"total_lessons": 0, "total_duration_sec": 0}),
                    progresses.get(int(course.id)),
                    self._access_ok(course, access),
                    stats.get(int(course.id), {}).get("first_lesson_id"),
                )
                for course, subject in rows
            ]
        return {
            "items": items,
            "total": total,
            "page": page,
            "page_size": page_size,
            "metric_version": ZHY_PROGRESS_METRIC_VERSION,
            "freshness_seconds": freshness,
            "generated_at": _iso(now),
        }

    # ── 详情 ─────────────────────────────────────────────────────────────

    def detail(self, user_id: int, course_id: int) -> dict[str, Any]:
        now = utcnow()
        mode = get_mode_context(self.config, db=self.db).snapshot().mode
        with Session(self.db.engine) as session:
            pair = session.execute(
                select(EduCourse, EduSubject)
                .join(EduSubject, EduSubject.id == EduCourse.subject_id)
                .where(
                    EduCourse.id == course_id,
                    not_deleted(EduCourse),
                    not_deleted(EduSubject),
                    EduSubject.is_enabled.is_(True),
                    EduCourse.status == "published",
                    EduCourse.mode.in_((mode, "both")),
                )
            ).first()
            if pair is None:
                raise AppError(NOT_FOUND, data={"resource": "course", "course_id": course_id})
            course, subject = pair

            chapters = (
                session.execute(
                    select(EduCourseChapter)
                    .where(
                        EduCourseChapter.course_id == course_id,
                        not_deleted(EduCourseChapter),
                    )
                    .order_by(EduCourseChapter.sort_order, EduCourseChapter.id)
                )
                .scalars()
                .all()
            )
            lessons = (
                session.execute(
                    select(EduCourseLesson)
                    .where(
                        EduCourseLesson.course_id == course_id,
                        not_deleted(EduCourseLesson),
                    )
                    .order_by(EduCourseLesson.sort_order, EduCourseLesson.id)
                )
                .scalars()
                .all()
            )
            progresses = {
                int(row.lesson_id): row
                for row in session.execute(
                    select(LearnProgress).where(
                        LearnProgress.user_id == user_id,
                        LearnProgress.course_id == course_id,
                    )
                )
                .scalars()
                .all()
            }
            statuses = {
                int(lesson_id): str(status)
                for lesson_id, status in session.execute(
                    select(MediaAsset.lesson_id, MediaAsset.status).where(
                        MediaAsset.lesson_id.in_([int(lesson.id) for lesson in lessons])
                    )
                ).all()
            }
            course_progress = session.execute(
                select(LearnCourseProgress).where(
                    LearnCourseProgress.user_id == user_id,
                    LearnCourseProgress.course_id == course_id,
                )
            ).scalar_one_or_none()
            access = self._access_flags(session, user_id)
            freshness = self._freshness(session, now)

        by_chapter: dict[int, list[dict[str, Any]]] = {}
        first_lesson_id: int | None = None
        total_duration = 0
        for lesson in lessons:
            if first_lesson_id is None:
                first_lesson_id = int(lesson.id)
            total_duration += int(lesson.duration_sec)
            row = progresses.get(int(lesson.id))
            by_chapter.setdefault(int(lesson.chapter_id), []).append(
                {
                    "lesson_id": int(lesson.id),
                    "title": lesson.title,
                    "kind": lesson.kind,
                    "duration_sec": int(lesson.duration_sec),
                    "is_free": bool(lesson.is_free),
                    "asset_status": statuses.get(int(lesson.id)),
                    "progress": None if row is None else _lesson_progress_out(row),
                }
            )

        payload = self._course_out(
            course,
            subject,
            {
                "total_lessons": len(lessons),
                "total_duration_sec": total_duration,
                "first_lesson_id": first_lesson_id,
            },
            course_progress,
            self._access_ok(course, access),
            first_lesson_id,
        )
        return {
            "course": payload,
            "chapters": [
                {
                    "chapter_id": int(chapter.id),
                    "title": chapter.title,
                    "lessons": by_chapter.get(int(chapter.id), []),
                }
                for chapter in chapters
            ],
            "metric_version": ZHY_PROGRESS_METRIC_VERSION,
            "freshness_seconds": freshness,
            "generated_at": _iso(now),
        }

    # ── 内部 ────────────────────────────────────────────────────────────

    @staticmethod
    def _lesson_stats(session: Session, course_ids: list[int]) -> dict[int, dict[str, Any]]:
        if not course_ids:
            return {}
        rows = session.execute(
            select(
                EduCourseLesson.course_id,
                EduCourseLesson.id,
                EduCourseLesson.duration_sec,
            )
            .join(EduCourseChapter, EduCourseChapter.id == EduCourseLesson.chapter_id)
            .where(
                EduCourseLesson.course_id.in_(course_ids),
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
        stats: dict[int, dict[str, Any]] = {}
        for course_id, lesson_id, duration in rows:
            node = stats.setdefault(
                int(course_id),
                {"total_lessons": 0, "total_duration_sec": 0, "first_lesson_id": int(lesson_id)},
            )
            node["total_lessons"] += 1
            node["total_duration_sec"] += int(duration)
        return stats

    @staticmethod
    def _course_progress(
        session: Session, user_id: int, course_ids: list[int]
    ) -> dict[int, LearnCourseProgress]:
        if not course_ids:
            return {}
        return {
            int(row.course_id): row
            for row in session.execute(
                select(LearnCourseProgress).where(
                    LearnCourseProgress.user_id == user_id,
                    LearnCourseProgress.course_id.in_(course_ids),
                )
            )
            .scalars()
            .all()
        }

    @staticmethod
    def _access_flags(session: Session, user_id: int) -> dict[str, Any]:
        return {
            "member_level": active_level(session, user_id),
            "class_member": is_class_member(session, user_id),
        }

    @staticmethod
    def _access_ok(course: EduCourse, access: Mapping[str, Any]) -> bool:
        if course.access_type == "public":
            return True
        if course.access_type == "member":
            level = access.get("member_level")
            return isinstance(level, int) and level >= REQUIRED_MEMBER_LEVEL
        return bool(access.get("class_member"))

    @staticmethod
    def _course_out(
        course: EduCourse,
        subject: EduSubject,
        stats: Mapping[str, Any],
        progress: LearnCourseProgress | None,
        access_ok: bool,
        first_lesson_id: int | None,
    ) -> dict[str, Any]:
        return {
            "course_id": int(course.id),
            "title": course.title,
            "summary": course.summary,
            "cover_url": course.cover_url,
            "subject_id": int(course.subject_id),
            "subject_name": subject.name,
            "access_type": course.access_type,
            "mode": course.mode,
            "total_lessons": int(stats.get("total_lessons", 0)),
            "total_duration_sec": int(stats.get("total_duration_sec", 0)),
            "first_lesson_id": first_lesson_id,
            "access_ok": access_ok,
            "progress": None if progress is None else _course_progress_out(progress),
        }

    @staticmethod
    def _freshness(session: Session, now: datetime) -> int:
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


def student_catalog() -> StudentCatalog:
    return current_app.extensions["zhy_student_catalog"]  # type: ignore[no-any-return]


__all__ = ["StudentCatalog", "student_catalog"]
