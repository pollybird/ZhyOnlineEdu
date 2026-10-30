"""题库与判分接口（§10.4 · M2.6）。

两条不可协商的口径（与 `modules/assess/service.py` 同源）：

1. **答案不下发** —— 学员端（`entry` / `start` / `attempt` / `submit`）只返回题干、
   选项、逐题对错与解析；`answer` 字段只出现在 `course:edit` 的题库管理接口里。
2. **重试不双成绩** —— `POST /quiz/attempts/{id}/submit` 必须携带
   `Idempotency-Key: {attempt_uid}`；同 key 同 payload 重放返回首次结果（201 → 200），
   同 key 不同 payload 返回 `CONFLICT_RETRY(409)`，不覆盖首次成绩。
"""

from __future__ import annotations

from typing import Any, cast

from flask import Blueprint, Response, current_app, request

from zhyonlineedu.api.schemas import (
    DeleteOut,
    QuizAttemptCreatedOut,
    QuizAttemptListOut,
    QuizAttemptOut,
    QuizEntryOut,
    QuizQuestionIn,
    QuizQuestionListOut,
    QuizQuestionOut,
    QuizQuestionUpdateIn,
    QuizStartIn,
    QuizSubmitIn,
    QuizSubmitOut,
    VersionIn,
)
from zhyonlineedu.core.auth.decorators import authenticate_request, require_permission
from zhyonlineedu.core.codes import VALIDATION_FAILED
from zhyonlineedu.core.envelope import ok
from zhyonlineedu.core.errors import AppError
from zhyonlineedu.core.idempotency import idempotency_key
from zhyonlineedu.core.openapi import documented
from zhyonlineedu.modules.assess.service import AssessService

bp = Blueprint("quiz", __name__, url_prefix="/api/v1")

MANAGE = "course:edit"
PREVIEW_PERMISSION = "course:edit"


def _service() -> AssessService:
    return current_app.extensions["zhy_quiz"]  # type: ignore[no-any-return]


def _can_preview() -> bool:
    return PREVIEW_PERMISSION in authenticate_request().permissions


def _payload() -> dict[str, Any]:
    raw = request.get_json(silent=True)
    if raw is None:
        return {}
    if not isinstance(raw, dict):
        raise AppError(VALIDATION_FAILED, data={"errors": ["请求体必须是 JSON 对象"]})
    return raw


def _lesson_id(body: dict[str, Any]) -> int:
    value = body.get("lesson_id")
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise AppError(VALIDATION_FAILED, data={"errors": ["lesson_id 必须是正整数"]})
    return value


# ── 题库管理（course:edit）────────────────────────────────────────────────


@bp.get("/quiz/questions")
@require_permission(MANAGE)
@documented(
    "题库列表",
    description=(
        "按 course_id / lesson_id / kind / is_enabled 过滤，page_size 上限 100。"
        "管理端返回标准答案（answer）与解析，供编辑使用。"
    ),
    response=QuizQuestionListOut,
    tags=("quiz",),
)
def list_questions() -> tuple[Response, int]:
    return ok(_service().list_questions(request.args))


@bp.post("/quiz/questions")
@require_permission(MANAGE)
@documented(
    "创建题目",
    description=(
        "题型 single/multiple/judge；判断题选项由服务端固定生成 T/F（忽略入参）。"
        "answer 为选项键数组：单选/判断恰好 1 项，多选至少 2 项；"
        "score 1~100。新题追加到该课时末尾（sort_order 步进 1024）。"
    ),
    request=QuizQuestionIn,
    response=QuizQuestionOut,
    tags=("quiz",),
)
def create_question() -> tuple[Response, int]:
    return ok(_service().create_question(_payload()))


@bp.patch("/quiz/questions/<int:question_id>")
@require_permission(MANAGE)
@documented(
    "修改题目",
    description=(
        "必须回传 version（乐观锁，不匹配 CONFLICT_RETRY）。只改部分字段；"
        "改 kind/options/answer 时三者一起校验。已在途的作答使用开始时的快照，不受影响。"
    ),
    request=QuizQuestionUpdateIn,
    response=QuizQuestionOut,
    tags=("quiz",),
)
def update_question(question_id: int) -> tuple[Response, int]:
    return ok(_service().update_question(question_id, _payload()))


@bp.delete("/quiz/questions/<int:question_id>")
@require_permission(MANAGE)
@documented(
    "删除题目",
    description="软删（delete_seq 单调递增）；不删除历史作答快照，保证成绩可复核。",
    request=VersionIn,
    response=DeleteOut,
    tags=("quiz",),
)
def delete_question(question_id: int) -> tuple[Response, int]:
    return ok(_service().delete_question(question_id, _payload()))


# ── 学员作答 ──────────────────────────────────────────────────────────────


@bp.get("/lessons/<int:lesson_id>/quiz")
@documented(
    "课时测验入口",
    description=(
        "返回题目数/总分/及格线，以及进行中的作答与最近一次成绩。"
        "不含任何答案字段（M2.6 验收口径一）。课时不存在 404；无题目 404（quiz_empty）。"
    ),
    response=QuizEntryOut,
    tags=("quiz",),
)
def quiz_entry(lesson_id: int) -> tuple[Response, int]:
    user = authenticate_request()
    return ok(_service().entry(user_id=user.id, lesson_id=lesson_id, can_preview=_can_preview()))


@bp.post("/quiz/attempts")
@documented(
    "开始作答",
    description=(
        "冻结题目快照（无答案）与答案键（不可下发）；同一课时已有进行中作答时返回"
        "created=false，不重复扣题。响应中的 attempt_uid 即交卷的 Idempotency-Key。"
    ),
    request=QuizStartIn,
    response=QuizAttemptCreatedOut,
    tags=("quiz",),
)
def start_attempt() -> tuple[Response, int]:
    user = authenticate_request()
    body = _payload()
    result = _service().start(
        user_id=user.id, lesson_id=_lesson_id(body), can_preview=_can_preview()
    )
    return ok(result, http=201 if result["created"] else 200)


@bp.post("/quiz/attempts/<int:attempt_id>/submit")
@documented(
    "交卷判分",
    description=(
        "**必须**携带 `Idempotency-Key: {attempt_uid}`。单选/判断需 1 个选项，"
        "多选需与答案集合完全相等（无部分分），未作答按 0 分。"
        "首次提交返回 201；同 key 同 payload 重放返回 200 且成绩不重算；"
        "同 key 不同 payload 返回 CONFLICT_RETRY(409)。响应不含标准答案。"
    ),
    request=QuizSubmitIn,
    response=QuizSubmitOut,
    tags=("quiz",),
    extensions={"x-zhy-idempotent": True, "x-zhy-idempotency-header": "Idempotency-Key"},
)
def submit_attempt(attempt_id: int) -> tuple[Response, int]:
    user = authenticate_request()
    # required=True 时必不以 None 返回；cast 只用于类型窄化
    key = cast(str, idempotency_key())
    result = _service().submit(
        user_id=user.id,
        attempt_id=attempt_id,
        body=_payload(),
        key=key,
        can_preview=_can_preview(),
    )
    return ok(result, http=201 if result["created"] else 200)


@bp.get("/quiz/attempts/<int:attempt_id>")
@documented(
    "作答详情",
    description=(
        "仅本人可见（他人作答统一 404，避免探测）；未交卷返回题目快照与 null 结果，"
        "已交卷返回逐题对错、得分与解析，**不返回标准答案**。"
    ),
    response=QuizAttemptOut,
    tags=("quiz",),
)
def get_attempt(attempt_id: int) -> tuple[Response, int]:
    user = authenticate_request()
    return ok(
        _service().get_attempt(user_id=user.id, attempt_id=attempt_id, can_preview=_can_preview())
    )


@bp.get("/my/quiz/attempts")
@documented(
    "我的作答历史",
    description="按 lesson_id / status 过滤，倒序分页（page_size 上限 100）。",
    response=QuizAttemptListOut,
    tags=("quiz",),
)
def my_attempts() -> tuple[Response, int]:
    user = authenticate_request()
    return ok(_service().history(user_id=user.id, query=request.args))
