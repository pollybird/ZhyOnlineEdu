"""目录 / 课程三层服务（§10.1 / §10.2 · M2.1）。

口径：
- 学科树：`sort_order` 步进 1024；拖拽/移动走 `version` 乐观锁，冲突统一
  `CONFLICT_RETRY(409)`，前端提示"列表已更新，请重试"；
- 课程三层：course → chapter → lesson；所有写操作都带 `version`（父级或自身）；
- 上架前置：任一未删除课时缺少 `media_asset` 或状态非 `encoded` 即拒发，
  返回 `COURSE_ASSET_NOT_READY(409)` 并附未就绪课时清单（§10.2）；
- 软删一律 `not_deleted()` + `delete_seq` 单调递增（D19a/F6）。
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from typing import Any, NoReturn, cast

from sqlalchemy import Select, case, func, select, update
from sqlalchemy.engine import CursorResult
from sqlalchemy.orm import Session

from zhyonlineedu.core.codes import (
    CATALOG_NOT_EMPTY,
    CONFLICT_RETRY,
    COURSE_ASSET_NOT_READY,
    NOT_FOUND,
    SUBJECT_DISABLED,
    VALIDATION_FAILED,
)
from zhyonlineedu.core.config import Config
from zhyonlineedu.core.db.engine import Database, ensure_database
from zhyonlineedu.core.db.models.catalog import (
    EduCourse,
    EduCourseChapter,
    EduCourseLesson,
    EduSubject,
)
from zhyonlineedu.core.db.models.media import ASSET_READY, ASSET_STATUSES, MediaAsset
from zhyonlineedu.core.db.query import not_deleted
from zhyonlineedu.core.errors import AppError

SORT_STEP = 1024
MAX_SUBJECT_DEPTH = 5
PAGE_SIZE_DEFAULT = 20
PAGE_SIZE_MAX = 100

MODES = ("internal", "external", "both")
ACCESS_TYPES = ("public", "member", "class")
COURSE_STATUSES = ("draft", "published", "archived")
LESSON_KINDS = ("video", "text", "live")


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
    if not isinstance(value, str):
        _fail(f"{field} 只能是 {'/'.join(allowed)}")
    if value not in allowed:
        _fail(f"{field} 只能是 {'/'.join(allowed)}")
    return value


def _query_int(
    value: object, field: str, *, default: int, minimum: int, maximum: int | None = None
) -> int:
    """查询串取值（`request.args` 均为字符串）；为空用默认值。"""
    if value is None or value == "":
        return default
    if isinstance(value, str) and value.lstrip("-").isdigit():
        value = int(value)
    return _int(value, field, minimum=minimum, maximum=maximum)


def _version(body: Mapping[str, Any]) -> int:
    return _int(body.get("version"), "version", minimum=1)


def _subject_out(row: EduSubject) -> dict[str, Any]:
    return {
        "id": row.id,
        "parent_id": row.parent_id,
        "name": row.name,
        "sort_order": row.sort_order,
        "is_enabled": bool(row.is_enabled),
        "version": row.version,
        "created_at": _iso(row.created_at),
        "updated_at": _iso(row.updated_at),
    }


def _lesson_out(row: EduCourseLesson, asset_status: str | None) -> dict[str, Any]:
    return {
        "id": row.id,
        "chapter_id": row.chapter_id,
        "course_id": row.course_id,
        "title": row.title,
        "kind": row.kind,
        "duration_sec": row.duration_sec,
        "is_free": bool(row.is_free),
        "sort_order": row.sort_order,
        "version": row.version,
        "asset_status": asset_status,
    }


def _chapter_out(row: EduCourseChapter, lessons: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "id": row.id,
        "course_id": row.course_id,
        "title": row.title,
        "sort_order": row.sort_order,
        "version": row.version,
        "lessons": lessons,
    }


def _course_out(
    row: EduCourse,
    *,
    subject_name: str | None,
    chapter_count: int,
    lesson_count: int,
) -> dict[str, Any]:
    return {
        "id": row.id,
        "subject_id": row.subject_id,
        "subject_name": subject_name,
        "title": row.title,
        "summary": row.summary,
        "cover_url": row.cover_url,
        "mode": row.mode,
        "access_type": row.access_type,
        "status": row.status,
        "teacher_id": row.teacher_id,
        "sort_order": row.sort_order,
        "chapter_count": chapter_count,
        "lesson_count": lesson_count,
        "version": row.version,
        "published_at": _iso(row.published_at),
        "created_at": _iso(row.created_at),
        "updated_at": _iso(row.updated_at),
    }


class CatalogService:
    """无状态服务：每个动作自建短会话，提交前完成全部校验。"""

    def __init__(self, config: Config, *, db: Database | None = None) -> None:
        self.config = config
        self._db = db

    @property
    def db(self) -> Database:
        if self._db is None:
            self._db = ensure_database(self.config)
        return self._db

    # ── 学科树 ──────────────────────────────────────────────────────────

    def list_subjects(self) -> dict[str, Any]:
        with Session(self.db.engine) as session:
            rows = (
                session.execute(
                    select(EduSubject)
                    .where(not_deleted(EduSubject))
                    .order_by(EduSubject.parent_id, EduSubject.sort_order, EduSubject.id)
                )
                .scalars()
                .all()
            )
        return {"items": [_subject_out(row) for row in rows], "total": len(rows)}

    def create_subject(self, body: Mapping[str, Any]) -> dict[str, Any]:
        name = _text(body.get("name"), "name", max_len=64)
        parent_id = body.get("parent_id")
        parent = None if parent_id is None else _int(parent_id, "parent_id", minimum=1)
        with Session(self.db.engine) as session:
            if parent is None:
                self._ensure_depth(session, None, extra_height=1)
            else:
                parent = self._subject(session, parent).id
                self._ensure_depth(session, parent, extra_height=1)
            self._ensure_subject_name_free(session, name, parent)
            max_order = session.execute(
                select(func.coalesce(func.max(EduSubject.sort_order), 0)).where(
                    not_deleted(EduSubject),
                    (
                        EduSubject.parent_id.is_(None)
                        if parent is None
                        else EduSubject.parent_id == parent
                    ),
                )
            ).scalar_one()
            now = _utcnow()
            row = EduSubject(
                parent_id=parent,
                name=name,
                sort_order=int(max_order) + SORT_STEP,
                is_enabled=True,
                version=1,
                created_at=now,
                updated_at=now,
            )
            session.add(row)
            session.commit()
            return _subject_out(row)

    def update_subject(self, subject_id: int, body: Mapping[str, Any]) -> dict[str, Any]:
        version = _version(body)
        with Session(self.db.engine) as session:
            row = self._subject(session, subject_id)
            values: dict[str, Any] = {}
            if "name" in body:
                values["name"] = _text(body.get("name"), "name", max_len=64)
            if "is_enabled" in body:
                enabled = body.get("is_enabled")
                if not isinstance(enabled, bool):
                    _fail("is_enabled 必须是布尔值")
                values["is_enabled"] = enabled

            new_parent = row.parent_id
            moving = "parent_id" in body
            if moving:
                raw_parent = body.get("parent_id")
                new_parent = (
                    None if raw_parent is None else _int(raw_parent, "parent_id", minimum=1)
                )
                if new_parent == row.id:
                    _fail("不能把节点移动到自身下")
                if new_parent is not None:
                    self._subject(session, new_parent)
                    self._ensure_not_descendant(session, row.id, new_parent)
                self._ensure_depth(
                    session,
                    new_parent,
                    extra_height=self._subtree_height(session, row.id),
                )

            name = str(values.get("name", row.name))
            self._ensure_subject_name_free(session, name, new_parent, exclude_id=row.id)

            result = cast(
                CursorResult[Any],
                session.execute(
                    update(EduSubject)
                    .where(
                        EduSubject.id == row.id,
                        EduSubject.version == version,
                        not_deleted(EduSubject),
                    )
                    .values(
                        **values,
                        parent_id=new_parent,
                        version=EduSubject.version + 1,
                        updated_at=_utcnow(),
                    )
                ),
            )
            if result.rowcount != 1:
                raise AppError(
                    CONFLICT_RETRY,
                    data={"resource": "subject", "id": subject_id, "expected_version": version},
                )
            session.flush()
            session.refresh(row)

            if moving or "before_id" in body:
                before_id = body.get("before_id")
                self._reorder_subject_siblings(
                    session,
                    row,
                    before_id=(
                        None if before_id is None else _int(before_id, "before_id", minimum=1)
                    ),
                )
                session.refresh(row)
            session.commit()
            return _subject_out(row)

    def delete_subject(self, subject_id: int, body: Mapping[str, Any]) -> dict[str, Any]:
        version = _version(body)
        with Session(self.db.engine) as session:
            row = self._subject(session, subject_id)
            if self._has_children(session, subject_id):
                raise AppError(
                    CATALOG_NOT_EMPTY,
                    data={"resource": "subject", "id": subject_id, "reason": "has_children"},
                )
            course = session.execute(
                select(EduCourse.id).where(
                    not_deleted(EduCourse), EduCourse.subject_id == subject_id
                )
            ).first()
            if course is not None:
                raise AppError(
                    CATALOG_NOT_EMPTY,
                    data={"resource": "subject", "id": subject_id, "reason": "has_courses"},
                )
            if row.version != version:
                raise AppError(
                    CONFLICT_RETRY,
                    data={"resource": "subject", "id": subject_id, "expected_version": version},
                )
            self._soft_delete(session, EduSubject, row)
            session.commit()
        return {"id": subject_id, "deleted": True}

    # ── 课程 ────────────────────────────────────────────────────────────

    def list_courses(self, query: Mapping[str, Any]) -> dict[str, Any]:
        page = _query_int(query.get("page"), "page", default=1, minimum=1)
        page_size = _query_int(
            query.get("page_size"),
            "page_size",
            default=PAGE_SIZE_DEFAULT,
            minimum=1,
            maximum=PAGE_SIZE_MAX,
        )
        conditions: list[Any] = [not_deleted(EduCourse)]
        if query.get("subject_id") not in (None, ""):
            conditions.append(
                EduCourse.subject_id
                == _query_int(query.get("subject_id"), "subject_id", default=1, minimum=1)
            )
        if query.get("status") not in (None, ""):
            conditions.append(
                EduCourse.status == _choice(query.get("status"), "status", COURSE_STATUSES)
            )
        keyword = query.get("keyword")
        if isinstance(keyword, str) and keyword.strip():
            conditions.append(EduCourse.title.like(f"%{keyword.strip()}%"))

        with Session(self.db.engine) as session:
            total = session.execute(
                select(func.count()).select_from(EduCourse).where(*conditions)
            ).scalar_one()
            rows = (
                session.execute(
                    select(EduCourse)
                    .where(*conditions)
                    .order_by(EduCourse.sort_order, EduCourse.id)
                    .offset((page - 1) * page_size)
                    .limit(page_size)
                )
                .scalars()
                .all()
            )
            ids = [row.id for row in rows]
            chapter_counts = self._count_by(
                session, EduCourseChapter, EduCourseChapter.course_id, ids
            )
            lesson_counts = self._count_by(session, EduCourseLesson, EduCourseLesson.course_id, ids)
            subject_names = self._subject_names(session, {row.subject_id for row in rows})
            items = [
                _course_out(
                    row,
                    subject_name=subject_names.get(row.subject_id),
                    chapter_count=chapter_counts.get(row.id, 0),
                    lesson_count=lesson_counts.get(row.id, 0),
                )
                for row in rows
            ]
        return {"items": items, "total": int(total), "page": page, "page_size": page_size}

    def create_course(self, body: Mapping[str, Any]) -> dict[str, Any]:
        subject_id = _int(body.get("subject_id"), "subject_id", minimum=1)
        title = _text(body.get("title"), "title", max_len=128)
        summary = _optional_text(body.get("summary"), "summary", max_len=2000)
        cover_url = _optional_text(body.get("cover_url"), "cover_url", max_len=255)
        mode = _choice(body.get("mode", "both"), "mode", MODES)
        access_type = _choice(body.get("access_type", "public"), "access_type", ACCESS_TYPES)
        teacher_id = body.get("teacher_id")
        teacher = None if teacher_id is None else _int(teacher_id, "teacher_id", minimum=1)

        with Session(self.db.engine) as session:
            subject = self._subject(session, subject_id)
            self._ensure_course_title_free(session, title, subject.id)
            max_order = session.execute(
                select(func.coalesce(func.max(EduCourse.sort_order), 0)).where(
                    not_deleted(EduCourse)
                )
            ).scalar_one()
            now = _utcnow()
            row = EduCourse(
                subject_id=subject.id,
                title=title,
                summary=summary,
                cover_url=cover_url,
                mode=mode,
                access_type=access_type,
                status="draft",
                teacher_id=teacher,
                sort_order=int(max_order) + SORT_STEP,
                version=1,
                created_at=now,
                updated_at=now,
            )
            session.add(row)
            session.commit()
            return _course_out(
                row,
                subject_name=subject.name,
                chapter_count=0,
                lesson_count=0,
            )

    def get_course(self, course_id: int) -> dict[str, Any]:
        with Session(self.db.engine) as session:
            row = self._course(session, course_id)
            subject = session.get(EduSubject, row.subject_id)
            chapters = (
                session.execute(
                    select(EduCourseChapter)
                    .where(
                        not_deleted(EduCourseChapter),
                        EduCourseChapter.course_id == course_id,
                    )
                    .order_by(EduCourseChapter.sort_order, EduCourseChapter.id)
                )
                .scalars()
                .all()
            )
            lessons = (
                session.execute(
                    select(EduCourseLesson)
                    .where(not_deleted(EduCourseLesson), EduCourseLesson.course_id == course_id)
                    .order_by(EduCourseLesson.sort_order, EduCourseLesson.id)
                )
                .scalars()
                .all()
            )
            statuses = self._asset_statuses(session, [lesson.id for lesson in lessons])
            by_chapter: dict[int, list[dict[str, Any]]] = {}
            for lesson in lessons:
                by_chapter.setdefault(lesson.chapter_id, []).append(
                    _lesson_out(lesson, statuses.get(lesson.id))
                )
            payload = _course_out(
                row,
                subject_name=subject.name if subject is not None else None,
                chapter_count=len(chapters),
                lesson_count=len(lessons),
            )
            payload["chapters"] = [
                _chapter_out(chapter, by_chapter.get(chapter.id, [])) for chapter in chapters
            ]
        return payload

    def update_course(self, course_id: int, body: Mapping[str, Any]) -> dict[str, Any]:
        version = _version(body)
        with Session(self.db.engine) as session:
            row = self._course(session, course_id)
            values: dict[str, Any] = {}
            if "subject_id" in body:
                values["subject_id"] = self._subject(
                    session, _int(body.get("subject_id"), "subject_id", minimum=1)
                ).id
            if "title" in body:
                values["title"] = _text(body.get("title"), "title", max_len=128)
            if "summary" in body:
                values["summary"] = _optional_text(body.get("summary"), "summary", max_len=2000)
            if "cover_url" in body:
                values["cover_url"] = _optional_text(
                    body.get("cover_url"), "cover_url", max_len=255
                )
            if "mode" in body:
                values["mode"] = _choice(body.get("mode"), "mode", MODES)
            if "access_type" in body:
                values["access_type"] = _choice(
                    body.get("access_type"), "access_type", ACCESS_TYPES
                )
            if "teacher_id" in body:
                raw_teacher = body.get("teacher_id")
                values["teacher_id"] = (
                    None if raw_teacher is None else _int(raw_teacher, "teacher_id", minimum=1)
                )
            subject_id = int(values.get("subject_id", row.subject_id))
            title = str(values.get("title", row.title))
            self._ensure_course_title_free(session, title, subject_id, exclude_id=row.id)

            self._update_versioned(session, EduCourse, row, version, values, "course")
            session.refresh(row)
            subject = session.get(EduSubject, row.subject_id)
            chapter_count = self._count_by(
                session, EduCourseChapter, EduCourseChapter.course_id, [row.id]
            ).get(row.id, 0)
            lesson_count = self._count_by(
                session, EduCourseLesson, EduCourseLesson.course_id, [row.id]
            ).get(row.id, 0)
            session.commit()
            return _course_out(
                row,
                subject_name=subject.name if subject is not None else None,
                chapter_count=chapter_count,
                lesson_count=lesson_count,
            )

    def delete_course(self, course_id: int, body: Mapping[str, Any]) -> dict[str, Any]:
        version = _version(body)
        with Session(self.db.engine) as session:
            row = self._course(session, course_id)
            if row.status == "published":
                _fail("已发布课程需先下架再删除")
            if row.version != version:
                raise AppError(
                    CONFLICT_RETRY,
                    data={"resource": "course", "id": course_id, "expected_version": version},
                )
            lessons = (
                session.execute(
                    select(EduCourseLesson).where(
                        not_deleted(EduCourseLesson), EduCourseLesson.course_id == course_id
                    )
                )
                .scalars()
                .all()
            )
            chapters = (
                session.execute(
                    select(EduCourseChapter).where(
                        not_deleted(EduCourseChapter), EduCourseChapter.course_id == course_id
                    )
                )
                .scalars()
                .all()
            )
            for lesson in lessons:
                self._soft_delete(session, EduCourseLesson, lesson)
            for chapter in chapters:
                self._soft_delete(session, EduCourseChapter, chapter)
            self._soft_delete(session, EduCourse, row)
            session.commit()
        return {"id": course_id, "deleted": True}

    def publish_course(self, course_id: int, body: Mapping[str, Any]) -> dict[str, Any]:
        version = _version(body)
        with Session(self.db.engine) as session:
            row = self._course(session, course_id)
            if row.version != version:
                raise AppError(
                    CONFLICT_RETRY,
                    data={"resource": "course", "id": course_id, "expected_version": version},
                )
            subject = session.get(EduSubject, row.subject_id)
            if subject is None or not subject.is_enabled:
                raise AppError(
                    SUBJECT_DISABLED, data={"subject_id": row.subject_id, "course_id": course_id}
                )
            readiness = self._asset_readiness(session, course_id)
            if not readiness["ready"]:
                raise AppError(
                    COURSE_ASSET_NOT_READY,
                    data={
                        "course_id": course_id,
                        "reason": readiness["reason"],
                        "not_ready": readiness["not_ready"],
                    },
                )
            now = _utcnow()
            self._update_versioned(
                session,
                EduCourse,
                row,
                version,
                {"status": "published", "published_at": now},
                "course",
            )
            session.refresh(row)
            payload = _course_out(
                row,
                subject_name=subject.name,
                chapter_count=readiness["chapter_count"],
                lesson_count=readiness["lesson_count"],
            )
            session.commit()
        return {
            "course": payload,
            "ready": True,
            "not_ready": [],
            "published_at": payload["published_at"],
        }

    def unpublish_course(self, course_id: int, body: Mapping[str, Any]) -> dict[str, Any]:
        version = _version(body)
        with Session(self.db.engine) as session:
            row = self._course(session, course_id)
            if row.status != "published":
                _fail("仅已发布课程可下架")
            self._update_versioned(session, EduCourse, row, version, {"status": "draft"}, "course")
            session.refresh(row)
            subject = session.get(EduSubject, row.subject_id)
            chapter_count = self._count_by(
                session, EduCourseChapter, EduCourseChapter.course_id, [row.id]
            ).get(row.id, 0)
            lesson_count = self._count_by(
                session, EduCourseLesson, EduCourseLesson.course_id, [row.id]
            ).get(row.id, 0)
            payload = _course_out(
                row,
                subject_name=subject.name if subject is not None else None,
                chapter_count=chapter_count,
                lesson_count=lesson_count,
            )
            session.commit()
        return {"course": payload}

    def asset_readiness(self, course_id: int) -> dict[str, Any]:
        """上架就绪探测（供 publish 与外部诊断复用）。"""
        with Session(self.db.engine) as session:
            self._course(session, course_id)
            return self._asset_readiness(session, course_id)

    # ── 章 ──────────────────────────────────────────────────────────────

    def create_chapter(self, course_id: int, body: Mapping[str, Any]) -> dict[str, Any]:
        title = _text(body.get("title"), "title", max_len=128)
        with Session(self.db.engine) as session:
            course = self._course(session, course_id)
            self._ensure_unique(
                session,
                EduCourseChapter,
                EduCourseChapter.title == title,
                EduCourseChapter.course_id == course.id,
                message="同课程下章标题重复",
            )
            max_order = session.execute(
                select(func.coalesce(func.max(EduCourseChapter.sort_order), 0)).where(
                    not_deleted(EduCourseChapter), EduCourseChapter.course_id == course.id
                )
            ).scalar_one()
            now = _utcnow()
            row = EduCourseChapter(
                course_id=course.id,
                title=title,
                sort_order=int(max_order) + SORT_STEP,
                version=1,
                created_at=now,
                updated_at=now,
            )
            session.add(row)
            session.commit()
            return _chapter_out(row, [])

    def update_chapter(self, chapter_id: int, body: Mapping[str, Any]) -> dict[str, Any]:
        version = _version(body)
        with Session(self.db.engine) as session:
            row = self._chapter(session, chapter_id)
            values: dict[str, Any] = {}
            if "title" in body:
                title = _text(body.get("title"), "title", max_len=128)
                self._ensure_unique(
                    session,
                    EduCourseChapter,
                    EduCourseChapter.title == title,
                    EduCourseChapter.course_id == row.course_id,
                    message="同课程下章标题重复",
                    exclude_id=row.id,
                )
                values["title"] = title
            self._update_versioned(session, EduCourseChapter, row, version, values, "chapter")
            session.refresh(row)
            session.commit()
            return _chapter_out(row, [])

    def delete_chapter(self, chapter_id: int, body: Mapping[str, Any]) -> dict[str, Any]:
        version = _version(body)
        with Session(self.db.engine) as session:
            row = self._chapter(session, chapter_id)
            lesson = session.execute(
                select(EduCourseLesson.id).where(
                    not_deleted(EduCourseLesson), EduCourseLesson.chapter_id == chapter_id
                )
            ).first()
            if lesson is not None:
                raise AppError(
                    CATALOG_NOT_EMPTY,
                    data={"resource": "chapter", "id": chapter_id, "reason": "has_lessons"},
                )
            if row.version != version:
                raise AppError(
                    CONFLICT_RETRY,
                    data={"resource": "chapter", "id": chapter_id, "expected_version": version},
                )
            self._soft_delete(session, EduCourseChapter, row)
            session.commit()
        return {"id": chapter_id, "deleted": True}

    def reorder_chapters(self, course_id: int, body: Mapping[str, Any]) -> dict[str, Any]:
        version = _version(body)
        ordered_ids = self._ordered_ids(body.get("ordered_ids"), "ordered_ids")
        with Session(self.db.engine) as session:
            course = self._course(session, course_id)
            rows = (
                session.execute(
                    select(EduCourseChapter).where(
                        not_deleted(EduCourseChapter), EduCourseChapter.course_id == course_id
                    )
                )
                .scalars()
                .all()
            )
            if set(ordered_ids) != {row.id for row in rows} or len(ordered_ids) != len(rows):
                _fail("ordered_ids 必须与课程现有章的集合并集一致")
            self._update_versioned(session, EduCourse, course, version, {}, "course")
            self._renumber(session, EduCourseChapter, ordered_ids)
            session.commit()
            return {
                "course_id": course_id,
                "ordered_ids": ordered_ids,
            }

    # ── 课时 ────────────────────────────────────────────────────────────

    def create_lesson(self, chapter_id: int, body: Mapping[str, Any]) -> dict[str, Any]:
        title = _text(body.get("title"), "title", max_len=128)
        kind = _choice(body.get("kind", "video"), "kind", LESSON_KINDS)
        # 时长为派生值：上传视频后由探测结果写入，创建/编辑时不接受人工输入。
        is_free = body.get("is_free", False)
        if not isinstance(is_free, bool):
            _fail("is_free 必须是布尔值")
        with Session(self.db.engine) as session:
            chapter = self._chapter(session, chapter_id)
            self._ensure_unique(
                session,
                EduCourseLesson,
                EduCourseLesson.title == title,
                EduCourseLesson.chapter_id == chapter.id,
                message="同章下课时标题重复",
            )
            max_order = session.execute(
                select(func.coalesce(func.max(EduCourseLesson.sort_order), 0)).where(
                    not_deleted(EduCourseLesson), EduCourseLesson.chapter_id == chapter.id
                )
            ).scalar_one()
            now = _utcnow()
            row = EduCourseLesson(
                chapter_id=chapter.id,
                course_id=chapter.course_id,
                title=title,
                kind=kind,
                duration_sec=0,
                is_free=is_free,
                sort_order=int(max_order) + SORT_STEP,
                version=1,
                created_at=now,
                updated_at=now,
            )
            session.add(row)
            session.commit()
            return _lesson_out(row, None)

    def update_lesson(self, lesson_id: int, body: Mapping[str, Any]) -> dict[str, Any]:
        version = _version(body)
        with Session(self.db.engine) as session:
            row = self._lesson(session, lesson_id)
            values: dict[str, Any] = {}
            if "title" in body:
                title = _text(body.get("title"), "title", max_len=128)
                self._ensure_unique(
                    session,
                    EduCourseLesson,
                    EduCourseLesson.title == title,
                    EduCourseLesson.chapter_id == row.chapter_id,
                    message="同章下课时标题重复",
                    exclude_id=row.id,
                )
                values["title"] = title
            if "kind" in body:
                values["kind"] = _choice(body.get("kind"), "kind", LESSON_KINDS)
            if "is_free" in body:
                is_free = body.get("is_free")
                if not isinstance(is_free, bool):
                    _fail("is_free 必须是布尔值")
                values["is_free"] = is_free
            self._update_versioned(session, EduCourseLesson, row, version, values, "lesson")
            session.refresh(row)
            status = self._asset_statuses(session, [row.id]).get(row.id)
            session.commit()
            return _lesson_out(row, status)

    def delete_lesson(self, lesson_id: int, body: Mapping[str, Any]) -> dict[str, Any]:
        version = _version(body)
        with Session(self.db.engine) as session:
            row = self._lesson(session, lesson_id)
            if row.version != version:
                raise AppError(
                    CONFLICT_RETRY,
                    data={"resource": "lesson", "id": lesson_id, "expected_version": version},
                )
            self._soft_delete(session, EduCourseLesson, row)
            session.commit()
        return {"id": lesson_id, "deleted": True}

    def reorder_lessons(self, chapter_id: int, body: Mapping[str, Any]) -> dict[str, Any]:
        version = _version(body)
        ordered_ids = self._ordered_ids(body.get("ordered_ids"), "ordered_ids")
        with Session(self.db.engine) as session:
            chapter = self._chapter(session, chapter_id)
            rows = (
                session.execute(
                    select(EduCourseLesson).where(
                        not_deleted(EduCourseLesson),
                        EduCourseLesson.chapter_id == chapter_id,
                    )
                )
                .scalars()
                .all()
            )
            if set(ordered_ids) != {row.id for row in rows} or len(ordered_ids) != len(rows):
                _fail("ordered_ids 必须与章下现有课时的集合并集一致")
            self._update_versioned(session, EduCourseChapter, chapter, version, {}, "chapter")
            self._renumber(session, EduCourseLesson, ordered_ids)
            session.commit()
            return {"chapter_id": chapter_id, "ordered_ids": ordered_ids}

    # ── 内部：查询与校验 ────────────────────────────────────────────────

    @staticmethod
    def _subject(session: Session, subject_id: int) -> EduSubject:
        row = session.execute(
            select(EduSubject).where(EduSubject.id == subject_id, not_deleted(EduSubject))
        ).scalar_one_or_none()
        if row is None:
            raise AppError(NOT_FOUND, data={"resource": "subject", "id": subject_id})
        return row

    @staticmethod
    def _course(session: Session, course_id: int) -> EduCourse:
        row = session.execute(
            select(EduCourse).where(EduCourse.id == course_id, not_deleted(EduCourse))
        ).scalar_one_or_none()
        if row is None:
            raise AppError(NOT_FOUND, data={"resource": "course", "id": course_id})
        return row

    @staticmethod
    def _chapter(session: Session, chapter_id: int) -> EduCourseChapter:
        row = session.execute(
            select(EduCourseChapter).where(
                EduCourseChapter.id == chapter_id, not_deleted(EduCourseChapter)
            )
        ).scalar_one_or_none()
        if row is None:
            raise AppError(NOT_FOUND, data={"resource": "chapter", "id": chapter_id})
        return row

    @staticmethod
    def _lesson(session: Session, lesson_id: int) -> EduCourseLesson:
        row = session.execute(
            select(EduCourseLesson).where(
                EduCourseLesson.id == lesson_id, not_deleted(EduCourseLesson)
            )
        ).scalar_one_or_none()
        if row is None:
            raise AppError(NOT_FOUND, data={"resource": "lesson", "id": lesson_id})
        return row

    @staticmethod
    def _has_children(session: Session, subject_id: int) -> bool:
        return (
            session.execute(
                select(EduSubject.id).where(
                    not_deleted(EduSubject), EduSubject.parent_id == subject_id
                )
            ).first()
            is not None
        )

    @staticmethod
    def _subtree_height(session: Session, subject_id: int) -> int:
        """返回以该节点为根的子树高度（叶子=1，`extra_height` 口径）。"""
        children = (
            session.execute(
                select(EduSubject.id).where(
                    not_deleted(EduSubject), EduSubject.parent_id == subject_id
                )
            )
            .scalars()
            .all()
        )
        if not children:
            return 1
        return 1 + max(CatalogService._subtree_height(session, child) for child in children)

    @staticmethod
    def _ensure_not_descendant(session: Session, subject_id: int, candidate_parent: int) -> None:
        cursor: int | None = candidate_parent
        seen = 0
        while cursor is not None:
            if cursor == subject_id:
                _fail("不能把节点移动到自身或子节点下")
            cursor = session.execute(
                select(EduSubject.parent_id).where(EduSubject.id == cursor)
            ).scalar_one_or_none()
            seen += 1
            if seen > MAX_SUBJECT_DEPTH + 1:
                _fail("学科树存在环，已拒绝移动")

    @staticmethod
    def _node_depth(session: Session, subject_id: int) -> int:
        """节点自身深度（根=1）；调用前节点已确认存在。"""
        depth = 0
        cursor: int | None = subject_id
        while cursor is not None:
            depth += 1
            if depth > MAX_SUBJECT_DEPTH:
                _fail("学科树存在环")
            cursor = session.execute(
                select(EduSubject.parent_id).where(EduSubject.id == cursor)
            ).scalar_one_or_none()
        return depth

    @classmethod
    def _ensure_depth(cls, session: Session, parent_id: int | None, *, extra_height: int) -> None:
        """新子树底部深度 = 父节点深度 + extra_height（叶子按 1 计）。"""
        base = 0 if parent_id is None else cls._node_depth(session, parent_id)
        if base + extra_height > MAX_SUBJECT_DEPTH:
            _fail(f"学科树最大深度为 {MAX_SUBJECT_DEPTH}")

    @staticmethod
    def _ensure_subject_name_free(
        session: Session,
        name: str,
        parent_id: int | None,
        *,
        exclude_id: int | None = None,
    ) -> None:
        stmt = select(EduSubject.id).where(not_deleted(EduSubject), EduSubject.name == name)
        stmt = stmt.where(
            EduSubject.parent_id.is_(None)
            if parent_id is None
            else EduSubject.parent_id == parent_id
        )
        if exclude_id is not None:
            stmt = stmt.where(EduSubject.id != exclude_id)
        if session.execute(stmt).first() is not None:
            _fail("同级下学科名称重复")

    @staticmethod
    def _ensure_course_title_free(
        session: Session, title: str, subject_id: int, *, exclude_id: int | None = None
    ) -> None:
        stmt = select(EduCourse.id).where(
            not_deleted(EduCourse), EduCourse.subject_id == subject_id, EduCourse.title == title
        )
        if exclude_id is not None:
            stmt = stmt.where(EduCourse.id != exclude_id)
        if session.execute(stmt).first() is not None:
            _fail("同学科下课程标题重复")

    @staticmethod
    def _ensure_unique(
        session: Session,
        model: Any,
        *conditions: Any,
        message: str,
        exclude_id: int | None = None,
    ) -> None:
        stmt: Select[Any] = select(model.id).where(not_deleted(model), *conditions)
        if exclude_id is not None:
            stmt = stmt.where(model.id != exclude_id)
        if session.execute(stmt).first() is not None:
            _fail(message)

    @staticmethod
    def _reorder_subject_siblings(
        session: Session, row: EduSubject, *, before_id: int | None
    ) -> None:
        siblings = (
            session.execute(
                select(EduSubject)
                .where(
                    not_deleted(EduSubject),
                    (
                        EduSubject.parent_id.is_(None)
                        if row.parent_id is None
                        else EduSubject.parent_id == row.parent_id
                    ),
                )
                .order_by(EduSubject.sort_order, EduSubject.id)
            )
            .scalars()
            .all()
        )
        ordered = [item.id for item in siblings if item.id != row.id]
        if before_id is None:
            ordered.append(row.id)
        else:
            if before_id not in ordered:
                _fail("before_id 必须是与目标节点同级的其他节点")
            ordered.insert(ordered.index(before_id), row.id)
        CatalogService._renumber(session, EduSubject, ordered)

    @staticmethod
    def _renumber(session: Session, model: Any, ordered_ids: Sequence[int]) -> None:
        if not ordered_ids:
            return
        mapping = {row_id: (index + 1) * SORT_STEP for index, row_id in enumerate(ordered_ids)}
        session.execute(
            update(model)
            .where(model.id.in_(list(ordered_ids)))
            .values(
                sort_order=case(mapping, value=model.id),
                version=model.version + 1,
                updated_at=_utcnow(),
            )
        )

    @staticmethod
    def _update_versioned(
        session: Session,
        model: Any,
        row: Any,
        version: int,
        values: Mapping[str, Any],
        resource: str,
    ) -> None:
        result = cast(
            CursorResult[Any],
            session.execute(
                update(model)
                .where(model.id == row.id, model.version == version, not_deleted(model))
                .values(**values, version=model.version + 1, updated_at=_utcnow())
            ),
        )
        if result.rowcount != 1:
            raise AppError(
                CONFLICT_RETRY,
                data={"resource": resource, "id": row.id, "expected_version": version},
            )
        session.flush()

    @staticmethod
    def _soft_delete(session: Session, model: Any, row: Any) -> None:
        next_seq = int(
            session.execute(select(func.coalesce(func.max(model.delete_seq), 0))).scalar_one()
        )
        row.deleted_at = _utcnow()
        row.delete_seq = next_seq + 1
        row.version = int(row.version) + 1
        session.flush()

    @staticmethod
    def _ordered_ids(value: object, field: str) -> list[int]:
        if not isinstance(value, list) or not value:
            _fail(f"{field} 必须是非空数组")
        ids: list[int] = []
        for item in value:
            ids.append(_int(item, field, minimum=1))
        if len(ids) != len(set(ids)):
            _fail(f"{field} 不能包含重复 id")
        return ids

    @staticmethod
    def _asset_statuses(session: Session, lesson_ids: Sequence[int]) -> dict[int, str]:
        if not lesson_ids:
            return {}
        rows = session.execute(
            select(MediaAsset.lesson_id, MediaAsset.status).where(
                MediaAsset.lesson_id.in_(list(lesson_ids))
            )
        ).all()
        return {int(lesson_id): str(status) for lesson_id, status in rows}

    @staticmethod
    def _count_by(session: Session, model: Any, column: Any, ids: Sequence[int]) -> dict[int, int]:
        if not ids:
            return {}
        rows = session.execute(
            select(column, func.count())
            .where(not_deleted(model), column.in_(list(ids)))
            .group_by(column)
        ).all()
        return {int(key): int(count) for key, count in rows}

    @staticmethod
    def _subject_names(session: Session, ids: set[int]) -> dict[int, str]:
        if not ids:
            return {}
        rows = session.execute(
            select(EduSubject.id, EduSubject.name).where(EduSubject.id.in_(list(ids)))
        ).all()
        return {int(key): str(name) for key, name in rows}

    def _asset_readiness(self, session: Session, course_id: int) -> dict[str, Any]:
        lessons = (
            session.execute(
                select(EduCourseLesson)
                .where(not_deleted(EduCourseLesson), EduCourseLesson.course_id == course_id)
                .order_by(EduCourseLesson.sort_order, EduCourseLesson.id)
            )
            .scalars()
            .all()
        )
        chapter_count = int(
            session.execute(
                select(func.count())
                .select_from(EduCourseChapter)
                .where(not_deleted(EduCourseChapter), EduCourseChapter.course_id == course_id)
            ).scalar_one()
        )
        statuses = self._asset_statuses(session, [lesson.id for lesson in lessons])
        not_ready: list[dict[str, Any]] = []
        for lesson in lessons:
            status = statuses.get(lesson.id)
            if status != ASSET_READY:
                not_ready.append(
                    {
                        "lesson_id": lesson.id,
                        "title": lesson.title,
                        "asset_status": status,
                    }
                )
        reason = "no_lessons" if not lessons else ("asset_not_ready" if not_ready else "")
        return {
            "ready": bool(lessons) and not not_ready,
            "reason": reason,
            "not_ready": not_ready,
            "lesson_count": len(lessons),
            "chapter_count": chapter_count,
            "asset_statuses": ASSET_STATUSES,
        }
