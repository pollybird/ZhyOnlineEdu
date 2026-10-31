"""播放票据 / playlist / key / 分片端点（§15.2 · M2.5）。

- `POST /media/tickets` 需要登录；`course:edit` 用户可对未发布课程取票（preview）。
- `GET /media/playlist/{kid}/master.m3u8`、`/{kid}/{profile}/index.m3u8`、
  `GET /media/key/{kid}`、`GET /media/seg/{kid}/{profile}/{name}` 走**签名 URL**，
  不依赖 Bearer：CDN/HLS 播放器无法携带自定义头（这是 kid 与用户解耦的原因）。
  **无 ticket（ks/n/sig 缺失或无效）一律拿不到 key** —— 验收口径。
- playlist 每次下发轮换 nonce，因此响应禁止缓存；分片为 AES-128 密文可长缓存。
"""

from __future__ import annotations

from typing import Any

from flask import Blueprint, Response, current_app, make_response, request, send_file

from zhyonlineedu.api.schemas import (
    CourseProgressDetailOut,
    CourseProgressListOut,
    LearnCourseDetailOut,
    LearnCourseListOut,
    MySummaryOut,
    ProgressGateOut,
    ProgressIn,
    ProgressOut,
    TicketIn,
    TicketOut,
)
from zhyonlineedu.core.auth.decorators import (
    authenticate_request,
    login_required,
    require_permission,
)
from zhyonlineedu.core.codes import VALIDATION_FAILED, Code
from zhyonlineedu.core.constants import ZHY_PROGRESS_METRIC_VERSION
from zhyonlineedu.core.envelope import accepted_progress, ok
from zhyonlineedu.core.errors import AppError
from zhyonlineedu.core.openapi import documented
from zhyonlineedu.modules.learn.catalog import student_catalog
from zhyonlineedu.modules.learn.gateway import record
from zhyonlineedu.modules.learn.playback import PlaybackService, TicketRequest
from zhyonlineedu.modules.learn.profile import ProfileService, profile_service

bp = Blueprint("playback", __name__, url_prefix="/api/v1")

PREVIEW_PERMISSION = "course:edit"
_PLAYLIST_TYPE = "application/vnd.apple.mpegurl"


def _playback() -> PlaybackService:
    return current_app.extensions["zhy_playback"]  # type: ignore[no-any-return]


def _profile() -> ProfileService:
    return profile_service()


def _payload() -> dict[str, Any]:
    raw = request.get_json(silent=True)
    if raw is None:
        return {}
    if not isinstance(raw, dict):
        raise AppError(VALIDATION_FAILED, data={"errors": ["请求体必须是 JSON 对象"]})
    return raw


def _signed_args() -> dict[str, str]:
    values = {
        "ks": request.args.get("ks", ""),
        "nonce": request.args.get("n", ""),
        "exp": request.args.get("exp", ""),
        "sig": request.args.get("sig", ""),
    }
    if any(value == "" for value in values.values()):
        raise AppError(Code.MEDIA_TICKET_EXPIRED, data={"reason": "ticket_required"})
    return values


@bp.post("/media/tickets")
@login_required
@documented(
    "取播放票据",
    description=(
        "校验课时/资产/访问策略后签发 keysession，返回带 ks/n/exp/sig 的 master 地址；"
        "未发布课程仅 course:edit 可取票且 preview=true（进度不计入统计）；"
        "asset 非 encoded 返回 409 40202（附 retry_after）。"
    ),
    request=TicketIn,
    response=TicketOut,
    tags=("learn", "media"),
)
def issue_ticket() -> tuple[Response, int]:
    user = authenticate_request()
    body = _payload()
    lesson_id = body.get("lesson_id")
    if isinstance(lesson_id, bool) or not isinstance(lesson_id, int) or lesson_id < 1:
        raise AppError(VALIDATION_FAILED, data={"errors": ["lesson_id 必须是正整数"]})
    return ok(
        _playback().issue(
            TicketRequest(
                user_id=user.id,
                username=user.username,
                lesson_id=lesson_id,
                can_preview=PREVIEW_PERMISSION in user.permissions,
                ip=str(request.remote_addr or ""),
            )
        )
    )


@bp.get("/media/playlist/<kid>/master.m3u8")
@documented(
    "master playlist",
    description="过滤票据允许的档位并改写变体 URI；响应禁止缓存（每次轮换 nonce）。",
    tags=("media",),
    auth=False,
    media_type=_PLAYLIST_TYPE,
)
def master_playlist(kid: str) -> Response:
    args = _signed_args()
    text = _playback().playlist(
        kid,
        None,
        ks=args["ks"],
        nonce=args["nonce"],
        exp=args["exp"],
        sig=args["sig"],
    )
    response = make_response(text, 200)
    response.headers["Content-Type"] = f"{_PLAYLIST_TYPE}; charset=utf-8"
    response.headers["Cache-Control"] = "no-store, private, max-age=0"
    return response


@bp.get("/media/playlist/<kid>/<profile>/index.m3u8")
@documented(
    "variant playlist",
    description="校验档位在票据允许集合内，把 key/分片 URI 改写为签名地址。",
    tags=("media",),
    auth=False,
    media_type=_PLAYLIST_TYPE,
)
def variant_playlist(kid: str, profile: str) -> Response:
    args = _signed_args()
    text = _playback().playlist(
        kid,
        profile,
        ks=args["ks"],
        nonce=args["nonce"],
        exp=args["exp"],
        sig=args["sig"],
    )
    response = make_response(text, 200)
    response.headers["Content-Type"] = f"{_PLAYLIST_TYPE}; charset=utf-8"
    response.headers["Cache-Control"] = "no-store, private, max-age=0"
    return response


@bp.get("/media/key/<kid>")
@documented(
    "AES-128 密钥",
    description=(
        "校验签名、会话、档位、授权指纹与 nonce 后返回 16 字节密钥；"
        "缺 ticket/过期/重放分别 40201/40206，串课 40203；响应 private 短缓存。"
    ),
    tags=("media",),
    auth=False,
    media_type="application/octet-stream",
)
def media_key(kid: str) -> Response:
    args = _signed_args()
    secret = _playback().key(
        kid, ks=args["ks"], nonce=args["nonce"], exp=args["exp"], sig=args["sig"]
    )
    response = make_response(secret, 200)
    response.headers["Content-Type"] = "application/octet-stream"
    response.headers["Content-Length"] = str(len(secret))
    ttl = _playback().config.get_int("media.ttl_key")
    response.headers["Cache-Control"] = f"private, max-age={ttl}"
    response.headers["X-Content-Type-Options"] = "nosniff"
    return response


@bp.get("/media/seg/<kid>/<profile>/<name>")
@documented(
    "加密分片",
    description="AES-128 密文分片；路径白名单 + kid 归属校验，支持 Range/206。",
    tags=("media",),
    auth=False,
    media_type="video/mp2t",
)
def media_segment(kid: str, profile: str, name: str) -> Response:
    path = _playback().segment_path(kid, profile, name)
    response = send_file(path, conditional=True)
    response.headers["Content-Type"] = "video/mp2t"
    response.headers["Cache-Control"] = "public, max-age=2592000, immutable"
    response.headers["X-Content-Type-Options"] = "nosniff"
    return response


@bp.post("/learn/progress")
@login_required
@documented(
    "上报学习进度",
    description=(
        "HTTP 202 + code=202：网关分配 gw_seq 并追加 learn_progress_log（事实源），"
        "响应到达即表示已落盘；同一 30s 桶重复上报只计一次（deduped=true）。"
        "投影表由单写者 worker 落库，HTTP 路径不写 learn_progress。"
    ),
    request=ProgressIn,
    response=ProgressOut,
    tags=("learn",),
    extensions={"x-zhy-http-status": 202, "x-zhy-idempotent": True},
)
def report_progress() -> tuple[Response, int]:
    user = authenticate_request()
    body = _payload()
    lesson_id = body.get("lesson_id")
    if isinstance(lesson_id, bool) or not isinstance(lesson_id, int) or lesson_id < 1:
        raise AppError(VALIDATION_FAILED, data={"errors": ["lesson_id 必须是正整数"]})
    result = record(
        user_id=user.id,
        lesson_id=lesson_id,
        body=body,
        can_preview=PREVIEW_PERMISSION in user.permissions,
    )
    return accepted_progress(
        result["gw_seq"], result["accepted_at"], deduped=bool(result["deduped"])
    )


# ── 学习档案（§10.7 · M2.13）────────────────────────────────────────────


@bp.get("/my/progress")
@login_required
@documented(
    "我的学习档案",
    description=(
        "课程维度学习档案（本人），按最近学习时间倒序分页；支持 status=in_progress|completed "
        "与课程标题 keyword 过滤。数据来自 `learn_course_progress`（flush 异步维护），"
        "**预览流水不计入统计**；`freshness_seconds` 即投影滞后秒数，"
        "前端可展示「更新于 X 分钟前」。"
    ),
    response=CourseProgressListOut,
    tags=("learn",),
    extensions={"x-zhy-progress-version": ZHY_PROGRESS_METRIC_VERSION},
)
def my_progress() -> tuple[Response, int]:
    user = authenticate_request()
    return ok(_profile().list_progress(user.id, request.args))


@bp.get("/my/progress/gate")
@require_permission("system:manage")
@documented(
    "进度落库诊断",
    description=(
        "运维/自诊接口：返回网关已分配的 `last_gw_seq`、已落库的 `flushed_max_gw_seq`、"
        "未落库流水数与最老流水滞后秒数；`healthy=false` 表示投影滞后超过容差。"
        "预览流水只标记 flushed，不影响统计口径。"
    ),
    response=ProgressGateOut,
    tags=("learn",),
    extensions={"x-zhy-progress-version": ZHY_PROGRESS_METRIC_VERSION},
)
def progress_gate() -> tuple[Response, int]:
    return ok(_profile().gate())


@bp.get("/my/progress/<int:course_id>")
@login_required
@documented(
    "课程学习档案详情",
    description=(
        "本人某门课的学习档案：课程汇总 + 已学课时的章节/课时明细（未学课时不下发）；"
        "无学习记录返回 404。完成率为「已完成课时 / 有效课时总数」，"
        "时长与完成率均为剔除预览后的口径。"
    ),
    response=CourseProgressDetailOut,
    tags=("learn",),
    extensions={"x-zhy-progress-version": ZHY_PROGRESS_METRIC_VERSION},
)
def course_progress_detail(course_id: int) -> tuple[Response, int]:
    user = authenticate_request()
    return ok(_profile().detail(user.id, course_id))


@bp.get("/my/summary")
@login_required
@documented(
    "首屏学习汇总",
    description=(
        "首屏 ≤2 请求：一次返回课程/课时完成数、累计与今日有效时长、最近 3 门课程与测验统计；"
        "今日时长只统计 `source=play AND quarantined=false` 的流水（预览与隔离流水不计入）。"
        "响应带 `metric_version` 与 `freshness_seconds`。"
    ),
    response=MySummaryOut,
    tags=("learn",),
    extensions={"x-zhy-progress-version": ZHY_PROGRESS_METRIC_VERSION},
)
def my_summary() -> tuple[Response, int]:
    user = authenticate_request()
    return ok(_profile().summary(user.id))


# ── 学员端课程目录（M2.14）──────────────────────────────────────────────


@bp.get("/learn/courses")
@login_required
@documented(
    "学员端课程列表",
    description=(
        "只返回 `published` 课程（学科未删除且启用），并按当前场景模式过滤 "
        "（`mode=both` 或当前模式）；支持 subject_id / keyword 过滤与分页。"
        "每张卡片带课时数、总时长、首个可播课时、本人课程进度与 `access_ok`"
        "（仅为 UI 提示，最终授权以 `POST /media/tickets` 为准）。"
    ),
    response=LearnCourseListOut,
    tags=("learn", "catalog"),
    extensions={"x-zhy-progress-version": ZHY_PROGRESS_METRIC_VERSION},
)
def student_courses() -> tuple[Response, int]:
    user = authenticate_request()
    return ok(student_catalog().list_courses(user.id, request.args))


@bp.get("/learn/courses/<int:course_id>")
@login_required
@documented(
    "学员端课程详情",
    description=(
        "返回已上架课程的章 / 课时与每课时学习进度（含续播位置与完成态）、"
        "资产就绪状态与课程级进度；未上架/模式不可见/已删除统一 404（不泄露存在性）。"
    ),
    response=LearnCourseDetailOut,
    tags=("learn", "catalog"),
    extensions={"x-zhy-progress-version": ZHY_PROGRESS_METRIC_VERSION},
)
def student_course_detail(course_id: int) -> tuple[Response, int]:
    user = authenticate_request()
    return ok(student_catalog().detail(user.id, course_id))
