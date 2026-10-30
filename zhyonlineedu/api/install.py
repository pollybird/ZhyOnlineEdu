"""安装向导接口（§8.5 / §15.1 接口清单）：免鉴权，统一信封。

七个端点与计划书一致：
`GET /install/state`、`POST /install/token`、`POST /install/db/test`、
`POST /install/db/setup`、`POST /install/admin`、`POST /install/mode`、`POST /install/finish`。
除 state 与 token 外，均需 `X-ZHY-Install-Session`（由 token 换取）。
"""

from __future__ import annotations

import logging
from typing import Any

from flask import Blueprint, Response, current_app, request

from zhyonlineedu.api.schemas import (
    DbSetupOut,
    DbTestOut,
    InstallAdminIn,
    InstallAdminOut,
    InstallFinishOut,
    InstallModeIn,
    InstallModeOut,
    InstallStateOut,
    InstallTokenIn,
    InstallTokenOut,
)
from zhyonlineedu.core.codes import (
    INSTALL_ALREADY_DONE,
    INSTALL_TOKEN_INVALID,
    VALIDATION_FAILED,
)
from zhyonlineedu.core.constants import ZHY_HEADER_INSTALL_SESSION
from zhyonlineedu.core.envelope import ok
from zhyonlineedu.core.errors import AppError
from zhyonlineedu.core.install import InstallService
from zhyonlineedu.core.openapi import documented

logger = logging.getLogger(__name__)

bp = Blueprint("install", __name__, url_prefix="/api/v1/install")


def _service() -> InstallService:
    return current_app.extensions["zhy_install"]  # type: ignore[no-any-return]


def _payload() -> dict[str, Any]:
    raw = request.get_json(silent=True)
    if raw is None:
        return {}
    if not isinstance(raw, dict):
        raise AppError(VALIDATION_FAILED, data={"errors": ["请求体必须是 JSON 对象"]})
    return raw


def _require_session(service: InstallService) -> None:
    if service.auth.closed:
        raise AppError(INSTALL_ALREADY_DONE)
    session = request.headers.get(ZHY_HEADER_INSTALL_SESSION, "")
    if not service.auth.check_session(session):
        raise AppError(INSTALL_TOKEN_INVALID, data={"reason": "session_missing_or_expired"})


def _audit(event: str, success: bool, **extra: object) -> None:
    logger.info(
        "install_audit event=%s ok=%s ip=%s detail=%s",
        event,
        success,
        request.remote_addr,
        extra,
    )


@bp.get("/state")
@documented(
    "读取安装状态",
    description="未安装时返回 start 与 5 步顺序；数据库不可达时 blockers 非空。",
    response=InstallStateOut,
    tags=("install",),
    auth=False,
)
def install_state() -> tuple[Response, int]:
    return ok(_service().status_payload())


@bp.post("/token")
@documented(
    "一次性令牌换取安装会话",
    description="失败 5 次/60 秒限速；安装完成后令牌与会话全部作废。",
    request=InstallTokenIn,
    response=InstallTokenOut,
    tags=("install",),
    auth=False,
)
def issue_token() -> tuple[Response, int]:
    service = _service()
    if service.auth.closed:
        raise AppError(INSTALL_ALREADY_DONE)
    candidate = _payload().get("token")
    if not isinstance(candidate, str) or not candidate:
        _audit("INSTALL_TOKEN", False, reason="missing")
        raise AppError(INSTALL_TOKEN_INVALID, data={"reason": "token_required"})
    if not service.auth.verify(candidate):
        _audit("INSTALL_TOKEN", False, reason="mismatch")
        raise AppError(INSTALL_TOKEN_INVALID)
    session, ttl = service.auth.open_session()
    _audit("INSTALL_TOKEN", True)
    return ok({"session": session, "expires_in": ttl})


@bp.post("/db/test")
@documented(
    "数据库能力探测",
    description="只探测不固化：返回 flavor/dialect/caps/warnings/blockers；blockers 非空即阻断。",
    response=DbTestOut,
    tags=("install",),
    auth=False,
    extensions={"x-zhy-idempotent": True},
)
def db_test() -> tuple[Response, int]:
    service = _service()
    _require_session(service)
    return ok(service.db_test())


@bp.post("/db/setup")
@documented(
    "建库 / 升级（Alembic）",
    description=(
        "一律执行 Alembic upgrade head；既有系统表但缺 alembic_version 时拒绝覆盖。"
        "迁移失败返回 rollback 命令提示。"
    ),
    response=DbSetupOut,
    tags=("install",),
    auth=False,
    extensions={"x-zhy-idempotent": True},
)
def db_setup() -> tuple[Response, int]:
    service = _service()
    _require_session(service)
    result = service.db_setup()
    _audit("INSTALL_DB_SETUP", True, step=result.get("step"))
    return ok(result)


@bp.post("/admin")
@documented(
    "创建首个超级管理员",
    description="Argon2id 哈希；幂等，已创建后用户名不可更换；finish 时落账号表并授 super_admin。",
    request=InstallAdminIn,
    response=InstallAdminOut,
    tags=("install",),
    auth=False,
    extensions={"x-zhy-idempotent": True},
)
def create_admin() -> tuple[Response, int]:
    service = _service()
    _require_session(service)
    body = _payload()
    result = service.set_admin(body.get("username"), body.get("password"))
    _audit("INSTALL_ADMIN", True, username=result.get("username"))
    return ok(result)


@bp.post("/mode")
@documented(
    "选择场景模式",
    description="internal/external；写入 sys_mode_config 并锁定能力位快照。",
    request=InstallModeIn,
    response=InstallModeOut,
    tags=("install",),
    auth=False,
    extensions={"x-zhy-idempotent": True},
)
def select_mode() -> tuple[Response, int]:
    service = _service()
    _require_session(service)
    result = service.set_mode(_payload().get("mode"))
    _audit("INSTALL_MODE", True, mode=result.get("mode"))
    return ok(result)


@bp.post("/finish")
@documented(
    "完成安装",
    description="固化 sys_compat_flags、生成 JWT 密钥、超管落表；返回 restart_required。",
    response=InstallFinishOut,
    tags=("install",),
    auth=False,
)
def finish() -> tuple[Response, int]:
    service = _service()
    _require_session(service)
    result = service.finish()
    _audit("INSTALL_FINISH", True, flavor=result.get("flavor"))
    return ok(result)
