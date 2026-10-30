"""认证依赖：Bearer 解析、登录态注入与权限守卫（供各业务 Blueprint 复用）。"""

from __future__ import annotations

from collections.abc import Callable
from functools import wraps
from typing import ParamSpec, TypeVar, cast

from flask import current_app, g, request

from zhyonlineedu.core.codes import AUTH_TOKEN_EXPIRED, PERMISSION_DENIED
from zhyonlineedu.core.errors import AppError

from .service import AuthService, AuthUser

P = ParamSpec("P")
R = TypeVar("R")


def auth_service() -> AuthService:
    return cast(AuthService, current_app.extensions["zhy_auth"])


def bearer_token() -> str | None:
    raw = request.headers.get("Authorization", "")
    scheme, _, token = raw.partition(" ")
    if scheme.lower() != "bearer" or not token.strip():
        return None
    return token.strip()


def authenticate_request() -> AuthUser:
    cached = getattr(g, "zhy_auth", None)
    if isinstance(cached, AuthUser):
        return cached
    token = bearer_token()
    if token is None:
        raise AppError(AUTH_TOKEN_EXPIRED)
    user = auth_service().authenticate(token)
    g.zhy_auth = user
    return user


def current_auth() -> AuthUser:
    user = getattr(g, "zhy_auth", None)
    if not isinstance(user, AuthUser):
        raise AppError(AUTH_TOKEN_EXPIRED)
    return user


def login_required(view: Callable[P, R]) -> Callable[P, R]:
    @wraps(view)
    def wrapper(*args: P.args, **kwargs: P.kwargs) -> R:
        authenticate_request()
        return view(*args, **kwargs)

    return wrapper


def require_permission(code: str) -> Callable[[Callable[P, R]], Callable[P, R]]:
    """RBAC 守卫：先登录态，再校验权限码；不足统一 403 PERMISSION_DENIED。"""

    def decorator(view: Callable[P, R]) -> Callable[P, R]:
        @wraps(view)
        def wrapper(*args: P.args, **kwargs: P.kwargs) -> R:
            user = authenticate_request()
            if code not in user.permissions:
                raise AppError(PERMISSION_DENIED, data={"required": code})
            return view(*args, **kwargs)

        return wrapper

    return decorator
