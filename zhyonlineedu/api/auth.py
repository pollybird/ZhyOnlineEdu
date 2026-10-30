"""认证接口（§7 / §15.2）：login · refresh · logout · bootstrap · PATCH password。

- RT 一律 HttpOnly Cookie（path=/api/v1/auth），AT 走 `Authorization: Bearer`；
- 刷新轮换；重放返回 `AUTH_REFRESH_REPLAY` 并清除 Cookie（前端静默回登录页）；
- 改密后 tv+1：RT 全端失效，旧 AT 在 `auth.at_ttl_sec` 窗口内仍可用（AUTH-TV-WINDOW）。
"""

from __future__ import annotations

from flask import Blueprint, Response, current_app, request

from zhyonlineedu.api.schemas import (
    BootstrapOut,
    LoginIn,
    LoginOut,
    LogoutOut,
    PasswordIn,
    PasswordOut,
)
from zhyonlineedu.core.auth.decorators import auth_service, current_auth, login_required
from zhyonlineedu.core.auth.service import REFRESH_COOKIE, RT_TTL_SEC
from zhyonlineedu.core.codes import VALIDATION_FAILED
from zhyonlineedu.core.envelope import fail, ok
from zhyonlineedu.core.errors import AppError
from zhyonlineedu.core.openapi import documented

bp = Blueprint("auth", __name__, url_prefix="/api/v1/auth")

COOKIE_PATH = "/api/v1/auth"


def _secure_cookie() -> bool:
    return bool(current_app.config.get("ZHY_ENV") in {"t1", "t2"})


def _attach_refresh(response: Response, token: str) -> None:
    response.set_cookie(
        REFRESH_COOKIE,
        token,
        max_age=RT_TTL_SEC,
        httponly=True,
        samesite="Lax",
        secure=_secure_cookie(),
        path=COOKIE_PATH,
    )


def _clear_refresh(response: Response) -> None:
    response.delete_cookie(REFRESH_COOKIE, path=COOKIE_PATH)


def _payload() -> dict[str, object]:
    raw = request.get_json(silent=True)
    if raw is None:
        return {}
    if not isinstance(raw, dict):
        raise AppError(VALIDATION_FAILED, data={"errors": ["请求体必须是 JSON 对象"]})
    return raw


@bp.post("/login")
@documented(
    "登录",
    description="Argon2id 校验；成功下发 AT（Bearer）与 RT（HttpOnly Cookie，轮换）。",
    request=LoginIn,
    response=LoginOut,
    tags=("auth",),
    auth=False,
)
def login() -> tuple[Response, int]:
    body = _payload()
    result = auth_service().login(body.get("username"), body.get("password"))
    response, http = ok(
        {
            "access_token": result.tokens.access_token,
            "token_type": "Bearer",
            "expires_in": result.tokens.access_expires_in,
            "user": result.user.as_dict(),
        }
    )
    _attach_refresh(response, result.tokens.refresh_token)
    return response, http


@bp.post("/refresh")
@documented(
    "刷新登录态",
    description="RT 轮换；旧 RT 重放返回 AUTH_REFRESH_REPLAY，全端下线并清除 Cookie。",
    response=LoginOut,
    tags=("auth",),
    auth=False,
)
def refresh() -> tuple[Response, int]:
    token = request.cookies.get(REFRESH_COOKIE)
    try:
        result = auth_service().refresh(token)
    except AppError as exc:
        response, http = fail(exc.code, data=exc.data)
        if http == 401:
            _clear_refresh(response)
        return response, http
    response, http = ok(
        {
            "access_token": result.tokens.access_token,
            "token_type": "Bearer",
            "expires_in": result.tokens.access_expires_in,
            "user": result.user.as_dict(),
        }
    )
    _attach_refresh(response, result.tokens.refresh_token)
    return response, http


@bp.post("/logout")
@documented(
    "退出登录",
    description="吊销会话并清除 RT Cookie；AT 由前端丢弃。",
    response=LogoutOut,
    tags=("auth",),
    auth=False,
)
def logout() -> tuple[Response, int]:
    refresh_token = request.cookies.get(REFRESH_COOKIE)
    header = request.headers.get("Authorization", "")
    access_token = header.partition(" ")[2].strip() or None
    revoked = auth_service().logout(refresh_token=refresh_token, access_token=access_token)
    response, http = ok({"logged_out": True, "session_revoked": revoked})
    _clear_refresh(response)
    return response, http


@bp.patch("/password")
@login_required
@documented(
    "修改口令",
    description="tv+1 全端 RT 失效；旧 AT 在 auth.at_ttl_sec 窗口内仍可用（AUTH-TV-WINDOW）。",
    request=PasswordIn,
    response=PasswordOut,
    tags=("auth",),
)
def change_password() -> tuple[Response, int]:
    body = _payload()
    result = auth_service().change_password(
        current_auth().id, body.get("old_password"), body.get("new_password")
    )
    response, http = ok({**result, "relogin_required": True})
    _clear_refresh(response)
    return response, http


@bp.get("/bootstrap")
@login_required
@documented(
    "读取当前登录上下文",
    description="返回用户、角色、权限与场景模式，供前端路由守卫与权限指令使用。",
    response=BootstrapOut,
    tags=("auth",),
)
def bootstrap() -> tuple[Response, int]:
    return ok(auth_service().bootstrap(current_auth()))
