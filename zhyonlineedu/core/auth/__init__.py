"""认证相关公开入口。"""

from .decorators import (
    auth_service,
    authenticate_request,
    bearer_token,
    current_auth,
    login_required,
    require_permission,
)
from .passwords import hash_password, password_problem, verify_password
from .rbac import PERMISSION_CODES, ROLE_PERMISSIONS, permissions_for, roles_for
from .service import REFRESH_COOKIE, RT_TTL_SEC, AuthService, AuthUser, LoginResult, RefreshResult

__all__ = [
    "PERMISSION_CODES",
    "REFRESH_COOKIE",
    "ROLE_PERMISSIONS",
    "RT_TTL_SEC",
    "AuthService",
    "AuthUser",
    "LoginResult",
    "RefreshResult",
    "authenticate_request",
    "auth_service",
    "bearer_token",
    "current_auth",
    "hash_password",
    "login_required",
    "password_problem",
    "permissions_for",
    "require_permission",
    "roles_for",
    "verify_password",
]
