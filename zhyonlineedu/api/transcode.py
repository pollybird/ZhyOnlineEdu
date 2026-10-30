"""转码与资产接口（§15.2 `media/assets` · M2.4）。

权限：`course:edit`（教师/管理员）。
- 资产列表/详情只读沙箱画像与 `ENC-10xx` 错误码，**不暴露 stderr**；
- `retranscode` 幂等：已有 queued/running 任务时返回 `created=false`；
- `delete` 在有活动任务时返回 `CONFLICT_RETRY`（409），避免删掉 worker 正在写的源。
"""

from __future__ import annotations

from typing import Any

from flask import Blueprint, Response, current_app, request

from zhyonlineedu.api.schemas import (
    AssetDeleteOut,
    AssetDetailOut,
    AssetListOut,
    RetranscodeIn,
    RetranscodeOut,
)
from zhyonlineedu.core.auth.decorators import require_permission
from zhyonlineedu.core.codes import VALIDATION_FAILED
from zhyonlineedu.core.envelope import ok
from zhyonlineedu.core.errors import AppError
from zhyonlineedu.core.openapi import documented
from zhyonlineedu.modules.media.transcode_service import TranscodeService

bp = Blueprint("transcode", __name__, url_prefix="/api/v1")

MANAGE = "course:edit"


def _service() -> TranscodeService:
    return current_app.extensions["zhy_transcode"]  # type: ignore[no-any-return]


def _payload() -> dict[str, Any]:
    raw = request.get_json(silent=True)
    if raw is None:
        return {}
    if not isinstance(raw, dict):
        raise AppError(VALIDATION_FAILED, data={"errors": ["请求体必须是 JSON 对象"]})
    return raw


@bp.get("/media/assets")
@require_permission(MANAGE)
@documented(
    "资产列表",
    description=(
        "按 course_id / lesson_id 过滤（可空），分页返回媒体资产；"
        "profiles 为已发布档位，sandbox 为沙箱后端/配额/ffmpeg 版本画像。"
    ),
    response=AssetListOut,
    tags=("media",),
)
def list_assets() -> tuple[Response, int]:
    return ok(
        _service().list_assets(
            course_id=request.args.get("course_id"),
            lesson_id=request.args.get("lesson_id"),
            page=request.args.get("page"),
            page_size=request.args.get("page_size"),
        )
    )


@bp.get("/media/assets/<int:lesson_id>")
@require_permission(MANAGE)
@documented(
    "资产详情",
    description=(
        "返回资产状态、playlist_version、kid 与最近一次任务" "（失败只给 ENC-10xx，无 stderr）。"
    ),
    response=AssetDetailOut,
    tags=("media",),
)
def get_asset(lesson_id: int) -> tuple[Response, int]:
    return ok(_service().get_asset(lesson_id))


@bp.post("/media/assets/<int:lesson_id>/retranscode")
@require_permission(MANAGE)
@documented(
    "重新转码",
    description=(
        "`transcode.paused=true` 时返回 409 TRANSCODE_PAUSED；profiles 缺省取 "
        "`transcode.profiles`，只允许 360/720/1080。已有排队/执行中任务时幂等返回 created=false。"
    ),
    request=RetranscodeIn,
    response=RetranscodeOut,
    tags=("media",),
    extensions={"x-zhy-idempotent": True},
)
def retranscode(lesson_id: int) -> tuple[Response, int]:
    profiles = _payload().get("profiles")
    if profiles is not None and not isinstance(profiles, list):
        raise AppError(VALIDATION_FAILED, data={"errors": ["profiles 必须是数组"]})
    return ok(_service().enqueue(lesson_id, profiles))


@bp.delete("/media/assets/<int:lesson_id>")
@require_permission(MANAGE)
@documented(
    "删除资产",
    description="删除任务记录、资产行与存储目录；存在排队/执行中任务时返回 409 CONFLICT_RETRY。",
    response=AssetDeleteOut,
    tags=("media",),
)
def delete_asset(lesson_id: int) -> tuple[Response, int]:
    return ok(_service().delete_asset(lesson_id))
