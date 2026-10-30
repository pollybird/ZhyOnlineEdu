"""分片上传接口（§15.2 `uploads init/chunk/status/complete/del` · M2.3）。

权限：`course:edit`（教师/管理员）。
- 分片请求体为 `application/octet-stream`，`index`/`md5` 走查询串（不 base64 膨胀）；
- `complete` 在 Web 进程内完成 sha256 复核 + 魔数/ffprobe 检疫，不合规即 50103 并清临时文件。
"""

from __future__ import annotations

from typing import Any

from flask import Blueprint, Response, current_app, request

from zhyonlineedu.api.schemas import (
    UploadChunkOut,
    UploadCompleteOut,
    UploadDeleteOut,
    UploadInitIn,
    UploadInitOut,
    UploadStatusOut,
)
from zhyonlineedu.core.auth.decorators import require_permission
from zhyonlineedu.core.codes import VALIDATION_FAILED
from zhyonlineedu.core.envelope import ok
from zhyonlineedu.core.errors import AppError
from zhyonlineedu.core.openapi import documented
from zhyonlineedu.modules.media.upload_service import CHUNK_MAX_BYTES, UploadService

bp = Blueprint("media", __name__, url_prefix="/api/v1")

MANAGE = "course:edit"


def _service() -> UploadService:
    return current_app.extensions["zhy_upload"]  # type: ignore[no-any-return]


def _payload() -> dict[str, Any]:
    raw = request.get_json(silent=True)
    if raw is None:
        return {}
    if not isinstance(raw, dict):
        raise AppError(VALIDATION_FAILED, data={"errors": ["请求体必须是 JSON 对象"]})
    return raw


@bp.post("/media/uploads")
@require_permission(MANAGE)
@documented(
    "初始化上传会话",
    description=(
        "按 lesson_id + file_size + chunk_size 规划分片；返回 upload_id（32 位 hex）。"
        "tmp 目录 0700、文件 0600，写入 O_CREAT|O_EXCL；会话默认 24h 过期。"
    ),
    request=UploadInitIn,
    response=UploadInitOut,
    tags=("media",),
)
def init_upload() -> tuple[Response, int]:
    return ok(_service().init(_payload()))


@bp.post("/media/uploads/<upload_id>/chunks")
@require_permission(MANAGE)
@documented(
    "上传分片",
    description=(
        "请求体：application/octet-stream 原始字节；查询串 index=<分片序号>&md5=<分片 md5 hex>。"
        "md5 不符返回 50101（重传该片）；同 index 同 md5 重传幂等返回 duplicated=true。"
    ),
    response=UploadChunkOut,
    tags=("media",),
)
def upload_chunk(upload_id: str) -> tuple[Response, int]:
    # 先按 Content-Length 拒绝超大体，避免在读到内存后才判大小
    declared = request.content_length
    if declared is None or declared <= 0:
        raise AppError(VALIDATION_FAILED, data={"errors": ["分片请求必须携带 Content-Length"]})
    if declared > CHUNK_MAX_BYTES:
        raise AppError(VALIDATION_FAILED, data={"errors": [f"分片不能超过 {CHUNK_MAX_BYTES} 字节"]})
    return ok(
        _service().chunk(
            upload_id,
            request.args.get("index"),
            request.args.get("md5"),
            request.get_data(cache=False),
        )
    )


@bp.get("/media/uploads/<upload_id>")
@require_permission(MANAGE)
@documented(
    "上传会话状态",
    description="返 received_chunks 与 missing_chunks（断点续传依据）；过期/删除统一 404。",
    response=UploadStatusOut,
    tags=("media",),
)
def upload_status(upload_id: str) -> tuple[Response, int]:
    return ok(_service().status(upload_id))


@bp.post("/media/uploads/<upload_id>/complete")
@require_permission(MANAGE)
@documented(
    "合并并检疫",
    description=(
        "校验缺片（50102 附 missing）→ 合并整文件并复核 sha256 → ffprobe 魔数白名单 + "
        "时长/尺寸上限（不合规 50103 并删除临时文件）→ media_asset 置 uploaded，等待转码。"
    ),
    response=UploadCompleteOut,
    tags=("media",),
    extensions={"x-zhy-idempotent": True},
)
def complete_upload(upload_id: str) -> tuple[Response, int]:
    return ok(_service().complete(upload_id))


@bp.delete("/media/uploads/<upload_id>")
@require_permission(MANAGE)
@documented(
    "删除上传会话",
    description="删除分片与合并临时文件，会话置 deleted（不可再接收分片）。",
    response=UploadDeleteOut,
    tags=("media",),
)
def delete_upload(upload_id: str) -> tuple[Response, int]:
    return ok(_service().delete(upload_id))
