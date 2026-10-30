"""双模式状态 / 预检 / 切换向导接口（§9.3 / §9.6 · M2.10~2.11）。

权限：全部 `system:manage`（super_admin / admin 默认持有）；teacher / student 403。

口径：
- `GET /system/mode` 返回当前模式、epoch、策略与计数（前端首屏 + 轮询）；
- `POST /system/mode/precheck` 只读预检，`blocked` 项即拒绝切换；
- `POST /system/mode/switch` 必须回传 `epoch`（乐观锁），切换后同进程立即生效，
  其他进程按 `mode.cache_ttl_sec` ≤30s 收敛；存量票据不失效、新签发按新模式。
"""

from __future__ import annotations

from typing import Any

from flask import Blueprint, Response, current_app, request

from zhyonlineedu.api.schemas import (
    ModePrecheckIn,
    ModePrecheckOut,
    ModeSummaryOut,
    ModeSwitchIn,
    ModeSwitchOut,
)
from zhyonlineedu.core.auth.decorators import authenticate_request, require_permission
from zhyonlineedu.core.codes import VALIDATION_FAILED
from zhyonlineedu.core.envelope import ok
from zhyonlineedu.core.errors import AppError
from zhyonlineedu.core.openapi import documented
from zhyonlineedu.modules.config.service import ModeService

bp = Blueprint("system", __name__, url_prefix="/api/v1")

MANAGE = "system:manage"


def _service() -> ModeService:
    return current_app.extensions["zhy_mode_service"]  # type: ignore[no-any-return]


def _payload() -> dict[str, Any]:
    raw = request.get_json(silent=True)
    if raw is None:
        return {}
    if not isinstance(raw, dict):
        raise AppError(VALIDATION_FAILED, data={"errors": ["请求体必须是 JSON 对象"]})
    return raw


@bp.get("/system/mode")
@require_permission(MANAGE)
@documented(
    "当前模式状态",
    description=(
        "返回 active_mode / epoch / previous_mode / migration_batch_id 与实时计数"
        "（有效票据、preview 流水、在途作答、转码任务、切换后不可见课程）；"
        "epoch 是切换的乐观锁基准。"
    ),
    response=ModeSummaryOut,
    tags=("system",),
)
def get_mode() -> tuple[Response, int]:
    return ok(_service().summary())


@bp.post("/system/mode/precheck")
@require_permission(MANAGE)
@documented(
    "模式切换预检",
    description=(
        "七项检查：模式支持 / 转码在途 / 在途作答 / 课程可见性 / 有效票据 / preview 流水隔离 /"
        " 能力位一致，外加自注册账号处置；任一 `blocked` 即 `ready=false`，"
        "切换接口会以 `MODE_SWITCH_BLOCKED` 拒绝。"
    ),
    request=ModePrecheckIn,
    response=ModePrecheckOut,
    tags=("system",),
)
def precheck_mode() -> tuple[Response, int]:
    return ok(_service().precheck(_payload()))


@bp.post("/system/mode/switch")
@require_permission(MANAGE)
@documented(
    "切换双模式",
    description=(
        "改 `sys_mode_state` 单行（epoch+1）与 `sys_mode_config` 策略行，**不改表结构**；"
        "事务内把窗口内 `source=preview` 流水标 `quarantined=true`；对外→对内时自注册账号"
        " 置 disabled + token_version+1 + 吊销会话并写 `migration_batch_id`；"
        "`epoch` 不匹配返回 409 `epoch_conflict`。"
    ),
    request=ModeSwitchIn,
    response=ModeSwitchOut,
    tags=("system",),
)
def switch_mode() -> tuple[Response, int]:
    user = authenticate_request()
    return ok(_service().switch(_payload(), operator_id=user.id))
