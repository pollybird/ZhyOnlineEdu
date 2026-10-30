"""学科树 + 课程三层接口（§10.1 / §10.2 · M2.1）。

权限：统一 `course:edit`（内置 teacher/admin/super_admin 持有；RBAC 由
`require_permission` 执行，学员角色仅 `lesson:play`，因此 403）。
所有写操作都要求 `version`（乐观锁），冲突返回 `CONFLICT_RETRY(409)`。
"""

from __future__ import annotations

from typing import Any

from flask import Blueprint, Response, current_app, request

from zhyonlineedu.api.schemas import (
    ChapterCreateIn,
    ChapterOut,
    ChapterReorderOut,
    ChapterUpdateIn,
    CourseCreateIn,
    CourseDetailOut,
    CourseListOut,
    CourseOut,
    CoursePublishOut,
    CourseReorderOut,
    CourseUnpublishOut,
    CourseUpdateIn,
    DeleteOut,
    LessonCreateIn,
    LessonOut,
    LessonUpdateIn,
    ReorderIn,
    SubjectCreateIn,
    SubjectListOut,
    SubjectOut,
    SubjectUpdateIn,
    VersionIn,
)
from zhyonlineedu.core.auth.decorators import require_permission
from zhyonlineedu.core.codes import VALIDATION_FAILED
from zhyonlineedu.core.envelope import ok
from zhyonlineedu.core.errors import AppError
from zhyonlineedu.core.openapi import documented
from zhyonlineedu.modules.catalog.service import CatalogService

bp = Blueprint("catalog", __name__, url_prefix="/api/v1")

MANAGE = "course:edit"


def _service() -> CatalogService:
    return current_app.extensions["zhy_catalog"]  # type: ignore[no-any-return]


def _payload() -> dict[str, Any]:
    raw = request.get_json(silent=True)
    if raw is None:
        return {}
    if not isinstance(raw, dict):
        raise AppError(VALIDATION_FAILED, data={"errors": ["请求体必须是 JSON 对象"]})
    return raw


# ── 学科树 ────────────────────────────────────────────────────────────────


@bp.get("/subjects")
@require_permission(MANAGE)
@documented(
    "学科树列表",
    description="平铺返回全部未删除节点（parent_id 为空即根），按 parent_id + sort_order 排序。",
    response=SubjectListOut,
    tags=("catalog",),
)
def list_subjects() -> tuple[Response, int]:
    return ok(_service().list_subjects())


@bp.post("/subjects")
@require_permission(MANAGE)
@documented(
    "新建学科节点",
    description="最大深度 5；同级同名拒绝；sort_order 步进 1024 追加到末尾。",
    request=SubjectCreateIn,
    response=SubjectOut,
    tags=("catalog",),
)
def create_subject() -> tuple[Response, int]:
    return ok(_service().create_subject(_payload()))


@bp.patch("/subjects/<int:subject_id>")
@require_permission(MANAGE)
@documented(
    "更新 / 移动学科节点",
    description=(
        "version 必填；改 parent_id 即移动（禁止移动到自身或子节点下，"
        "移动后整棵子树深度不得超过 5）；before_id 控制同级排序。"
    ),
    request=SubjectUpdateIn,
    response=SubjectOut,
    tags=("catalog",),
)
def update_subject(subject_id: int) -> tuple[Response, int]:
    return ok(_service().update_subject(subject_id, _payload()))


@bp.delete("/subjects/<int:subject_id>")
@require_permission(MANAGE)
@documented(
    "删除学科节点",
    description="存在子节点或课程时返回 CATALOG_NOT_EMPTY(409)；软删。",
    request=VersionIn,
    response=DeleteOut,
    tags=("catalog",),
)
def delete_subject(subject_id: int) -> tuple[Response, int]:
    return ok(_service().delete_subject(subject_id, _payload()))


# ── 课程 ──────────────────────────────────────────────────────────────────


@bp.get("/courses")
@require_permission(MANAGE)
@documented(
    "课程列表",
    description="支持 subject_id/status/keyword 过滤与 page/page_size 分页（page_size 上限 100）。",
    response=CourseListOut,
    tags=("catalog",),
)
def list_courses() -> tuple[Response, int]:
    return ok(_service().list_courses(request.args.to_dict()))


@bp.post("/courses")
@require_permission(MANAGE)
@documented(
    "新建课程",
    description="初始 status=draft；mode/access_type 为模式与授权判定输入（§9）。",
    request=CourseCreateIn,
    response=CourseOut,
    tags=("catalog",),
)
def create_course() -> tuple[Response, int]:
    return ok(_service().create_course(_payload()))


@bp.get("/courses/<int:course_id>")
@require_permission(MANAGE)
@documented(
    "课程详情（三层）",
    description="返回章与课时（含 asset_status），供编辑器一次拉全。",
    response=CourseDetailOut,
    tags=("catalog",),
)
def get_course(course_id: int) -> tuple[Response, int]:
    return ok(_service().get_course(course_id))


@bp.patch("/courses/<int:course_id>")
@require_permission(MANAGE)
@documented(
    "更新课程",
    description="version 必填；改 subject_id 时校验目标学科存在且标题在学科内唯一。",
    request=CourseUpdateIn,
    response=CourseOut,
    tags=("catalog",),
)
def update_course(course_id: int) -> tuple[Response, int]:
    return ok(_service().update_course(course_id, _payload()))


@bp.delete("/courses/<int:course_id>")
@require_permission(MANAGE)
@documented(
    "删除课程",
    description="已发布课程需先下架；软删章与课时但保留媒体资产（供审计）。",
    request=VersionIn,
    response=DeleteOut,
    tags=("catalog",),
)
def delete_course(course_id: int) -> tuple[Response, int]:
    return ok(_service().delete_course(course_id, _payload()))


@bp.post("/courses/<int:course_id>/publish")
@require_permission(MANAGE)
@documented(
    "上架课程",
    description=(
        "前置：学科启用 + 至少 1 课时 + 全部课时 asset_status=encoded。"
        "未就绪返回 COURSE_ASSET_NOT_READY(409)，data.not_ready 列出课时。"
    ),
    request=VersionIn,
    response=CoursePublishOut,
    tags=("catalog",),
    extensions={"x-zhy-idempotent": True},
)
def publish_course(course_id: int) -> tuple[Response, int]:
    return ok(_service().publish_course(course_id, _payload()))


@bp.post("/courses/<int:course_id>/unpublish")
@require_permission(MANAGE)
@documented(
    "下架课程",
    description="published → draft，published_at 保留；幂等重复下架会被步骤校验拒绝。",
    request=VersionIn,
    response=CourseUnpublishOut,
    tags=("catalog",),
    extensions={"x-zhy-idempotent": True},
)
def unpublish_course(course_id: int) -> tuple[Response, int]:
    return ok(_service().unpublish_course(course_id, _payload()))


@bp.post("/courses/<int:course_id>/reorder")
@require_permission(MANAGE)
@documented(
    "章排序",
    description="ordered_ids 必须覆盖课程全部章；version 为课程版本，成功后课程 version+1。",
    request=ReorderIn,
    response=CourseReorderOut,
    tags=("catalog",),
    extensions={"x-zhy-idempotent": True},
)
def reorder_chapters(course_id: int) -> tuple[Response, int]:
    return ok(_service().reorder_chapters(course_id, _payload()))


# ── 章 ────────────────────────────────────────────────────────────────────


@bp.post("/courses/<int:course_id>/chapters")
@require_permission(MANAGE)
@documented(
    "新建章",
    description="同课程下章标题唯一；sort_order 追加到末尾。",
    request=ChapterCreateIn,
    response=ChapterOut,
    tags=("catalog",),
)
def create_chapter(course_id: int) -> tuple[Response, int]:
    return ok(_service().create_chapter(course_id, _payload()))


@bp.patch("/chapters/<int:chapter_id>")
@require_permission(MANAGE)
@documented(
    "更新章",
    request=ChapterUpdateIn,
    response=ChapterOut,
    tags=("catalog",),
)
def update_chapter(chapter_id: int) -> tuple[Response, int]:
    return ok(_service().update_chapter(chapter_id, _payload()))


@bp.delete("/chapters/<int:chapter_id>")
@require_permission(MANAGE)
@documented(
    "删除章",
    description="章下仍有课时时返回 CATALOG_NOT_EMPTY(409)；软删。",
    request=VersionIn,
    response=DeleteOut,
    tags=("catalog",),
)
def delete_chapter(chapter_id: int) -> tuple[Response, int]:
    return ok(_service().delete_chapter(chapter_id, _payload()))


@bp.post("/chapters/<int:chapter_id>/reorder")
@require_permission(MANAGE)
@documented(
    "课时排序",
    description="ordered_ids 必须覆盖章下全部课时；version 为章版本。",
    request=ReorderIn,
    response=ChapterReorderOut,
    tags=("catalog",),
    extensions={"x-zhy-idempotent": True},
)
def reorder_lessons(chapter_id: int) -> tuple[Response, int]:
    return ok(_service().reorder_lessons(chapter_id, _payload()))


# ── 课时 ──────────────────────────────────────────────────────────────────


@bp.post("/chapters/<int:chapter_id>/lessons")
@require_permission(MANAGE)
@documented(
    "新建课时",
    description="course_id 由章冗余写入；同章下课时标题唯一。",
    request=LessonCreateIn,
    response=LessonOut,
    tags=("catalog",),
)
def create_lesson(chapter_id: int) -> tuple[Response, int]:
    return ok(_service().create_lesson(chapter_id, _payload()))


@bp.patch("/lessons/<int:lesson_id>")
@require_permission(MANAGE)
@documented(
    "更新课时",
    request=LessonUpdateIn,
    response=LessonOut,
    tags=("catalog",),
)
def update_lesson(lesson_id: int) -> tuple[Response, int]:
    return ok(_service().update_lesson(lesson_id, _payload()))


@bp.delete("/lessons/<int:lesson_id>")
@require_permission(MANAGE)
@documented(
    "删除课时",
    description="软删；媒体资产行保留（重新上架前需确保 encoded）。",
    request=VersionIn,
    response=DeleteOut,
    tags=("catalog",),
)
def delete_lesson(lesson_id: int) -> tuple[Response, int]:
    return ok(_service().delete_lesson(lesson_id, _payload()))
