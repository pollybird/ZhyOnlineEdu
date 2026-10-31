"""M1.6 认证 RBAC 与 tv 窗口（§7.1 / §7.2 / 附录 C）。

覆盖：
- 登录成功/失败/停用、AT 无状态鉴权；
- RT 轮换、重放全端下线 + tv+1 + 安全日志；
- 登出吊销、改密后的 tv 窗口（旧 AT 可用、RT 立即失效）；
- RBAC 守卫（403 PERMISSION_DENIED）与种子一致性；
- 过期 AT / 缺失 Bearer 的 401 口径。
"""

from __future__ import annotations

import time
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from flask import Blueprint
from sqlalchemy import select
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session
from zhyonlineedu.app import create_app
from zhyonlineedu.core.auth.decorators import require_permission
from zhyonlineedu.core.auth.keys import clear_key_cache, ensure_keypair, load_keypair
from zhyonlineedu.core.auth.passwords import hash_password, verify_password
from zhyonlineedu.core.auth.rbac import (
    PERMISSION_CODES,
    ROLE_PERMISSIONS,
    UNASSIGNED_PERMISSIONS,
)
from zhyonlineedu.core.auth.service import REFRESH_COOKIE, AuthService
from zhyonlineedu.core.auth.tokens import issue_access
from zhyonlineedu.core.capability import ProbeResult, ProbeState, StaticProbe
from zhyonlineedu.core.codes import (
    ACCOUNT_DISABLED,
    AUTH_INVALID_CREDENTIALS,
    AUTH_REFRESH_REPLAY,
    AUTH_TOKEN_EXPIRED,
    AUTH_TOKEN_REVOKED,
    PERMISSION_DENIED,
    VALIDATION_FAILED,
)
from zhyonlineedu.core.config import Config, load_config
from zhyonlineedu.core.db import compat
from zhyonlineedu.core.db.engine import create_db_engine
from zhyonlineedu.core.db.migration import upgrade_head
from zhyonlineedu.core.db.models.account import (
    AccountSession,
    AccountUser,
    SysLogSecurity,
    SysPermission,
    SysRolePermission,
    SysUserRole,
    utcnow,
)
from zhyonlineedu.core.db.models.system import SysConfig
from zhyonlineedu.core.envelope import ok
from zhyonlineedu.core.errors import AppError

PASSWORD = "S3cret-pass"  # noqa: S105 - 测试用固定口令
COOKIE_PATH = "/api/v1/auth"
READY = ProbeResult(ProbeState.READY, flavor="sqlite", capabilities={"returning": True})


@pytest.fixture(autouse=True)
def _cleanup() -> Iterator[None]:
    compat.reset()
    clear_key_cache()
    yield
    compat.reset()
    clear_key_cache()


def _config(tmp_path: Path) -> Config:
    return load_config(
        environ={
            "ZHY_ENV": "dev",
            "ZHY_DB_URL": f"sqlite:///{tmp_path / 'auth.db'}",
            "ZHY_DB_FLAVOR": "sqlite",
            "ZHY_JWT_PRIVATE_KEY_PATH": str(tmp_path / "keys" / "jwt_private.pem"),
            "ZHY_JWT_PUBLIC_KEY_PATH": str(tmp_path / "keys" / "jwt_public.pem"),
        },
        sys_config={},
    )


def _seed(tmp_path: Path, accounts: tuple[tuple[str, str, str], ...] = ()) -> Config:
    """建库（Alembic）+ 生成密钥 + 建账号；accounts 为 (username, role, status)。"""
    config = _config(tmp_path)
    ensure_keypair(config.get_str("jwt.private_key_path"), config.get_str("jwt.public_key_path"))
    url = config.get_str("db.url")
    upgrade_head(url, "sqlite")
    engine = create_db_engine(url, "sqlite")
    try:
        with Session(engine) as session:
            now = utcnow()
            for username, role, status in accounts:
                user = AccountUser(
                    username=username,
                    password_hash=hash_password(PASSWORD),
                    status=status,
                    token_version=1,
                    created_at=now,
                    updated_at=now,
                )
                session.add(user)
                session.flush()
                session.add(SysUserRole(user_id=user.id, role_code=role))
            session.commit()
    finally:
        engine.dispose()
    return config


def _client(config: Config) -> Any:
    return create_app(config=config, probe=StaticProbe(READY)).test_client()


def _login(client: Any, username: str = "teacher", password: str = PASSWORD) -> dict[str, Any]:
    resp = client.post("/api/v1/auth/login", json={"username": username, "password": password})
    assert resp.status_code == 200, resp.get_json()
    return dict(resp.get_json()["data"])


def _refresh_token(client: Any) -> str:
    cookie = client.get_cookie(REFRESH_COOKIE, path=COOKIE_PATH)
    assert cookie is not None
    return str(cookie.value)


# ── 登录与 AT 鉴权 ────────────────────────────────────────────────────────


def test_login_success_returns_access_and_cookie(tmp_path: Path) -> None:
    config = _seed(tmp_path, (("teacher", "teacher", "active"),))
    client = _client(config)
    resp = client.post("/api/v1/auth/login", json={"username": "teacher", "password": PASSWORD})
    assert resp.status_code == 200
    data = resp.get_json()["data"]
    assert data["token_type"] == "Bearer"  # noqa: S105 - 协议固定值，非口令
    assert data["expires_in"] == config.get_int("auth.at_ttl_sec")
    assert data["user"]["username"] == "teacher"
    assert data["user"]["roles"] == ["teacher"]
    assert set(data["user"]["permissions"]) == set(ROLE_PERMISSIONS["teacher"])

    cookie = resp.headers["Set-Cookie"]
    assert "HttpOnly" in cookie
    assert f"Path={COOKIE_PATH}" in cookie

    boot = client.get(
        "/api/v1/auth/bootstrap", headers={"Authorization": f"Bearer {data['access_token']}"}
    )
    assert boot.status_code == 200
    assert boot.get_json()["data"]["user"]["username"] == "teacher"


def test_login_rejects_bad_credentials_and_disabled(tmp_path: Path) -> None:
    client = _client(
        _seed(
            tmp_path,
            (("teacher", "teacher", "active"), ("blocked", "student", "disabled")),
        )
    )
    wrong = client.post(
        "/api/v1/auth/login", json={"username": "teacher", "password": "wrong-pass"}
    )
    assert wrong.status_code == 401
    assert wrong.get_json()["code"] == AUTH_INVALID_CREDENTIALS

    unknown = client.post("/api/v1/auth/login", json={"username": "ghost", "password": PASSWORD})
    assert unknown.status_code == 401
    assert unknown.get_json()["code"] == AUTH_INVALID_CREDENTIALS

    disabled = client.post("/api/v1/auth/login", json={"username": "blocked", "password": PASSWORD})
    assert disabled.status_code == 401
    assert disabled.get_json()["code"] == ACCOUNT_DISABLED


def test_missing_and_expired_access_token(tmp_path: Path) -> None:
    config = _seed(tmp_path, (("teacher", "teacher", "active"),))
    client = _client(config)

    missing = client.get("/api/v1/auth/bootstrap")
    assert missing.status_code == 401
    assert missing.get_json()["code"] == AUTH_TOKEN_EXPIRED

    key = load_keypair(
        config.get_str("jwt.private_key_path"), config.get_str("jwt.public_key_path")
    )
    with Session(create_db_engine(config.get_str("db.url"), "sqlite")) as session:
        user = session.execute(
            select(AccountUser).where(AccountUser.username == "teacher")
        ).scalar_one()
        stale, _ = issue_access(
            user_id=user.id,
            token_version=1,
            sid="sid-expired",
            key=key,
            ttl_sec=300,
            now=int(time.time()) - 400,
        )
    expired = client.get("/api/v1/auth/bootstrap", headers={"Authorization": f"Bearer {stale}"})
    assert expired.status_code == 401
    assert expired.get_json()["code"] == AUTH_TOKEN_EXPIRED


def test_access_token_ignores_token_version_window(tmp_path: Path) -> None:
    """AUTH-TV-WINDOW：改密/tv+1 后旧 AT 在 AT TTL 内仍可用。"""
    config = _seed(tmp_path, (("teacher", "teacher", "active"),))
    client = _client(config)
    access = _login(client)["access_token"]

    engine = create_db_engine(config.get_str("db.url"), "sqlite")
    try:
        with Session(engine) as session:
            user = session.execute(
                select(AccountUser).where(AccountUser.username == "teacher")
            ).scalar_one()
            user.token_version = 99
            session.commit()
    finally:
        engine.dispose()

    resp = client.get("/api/v1/auth/bootstrap", headers={"Authorization": f"Bearer {access}"})
    assert resp.status_code == 200
    assert resp.get_json()["data"]["user"]["token_version"] == 99


# ── RT 轮换 / 重放 / 登出 ─────────────────────────────────────────────────


def test_refresh_rotates_token(tmp_path: Path) -> None:
    client = _client(_seed(tmp_path, (("teacher", "teacher", "active"),)))
    _login(client)
    first = _refresh_token(client)
    resp = client.post("/api/v1/auth/refresh")
    assert resp.status_code == 200, resp.get_json()
    second = _refresh_token(client)
    assert first != second
    assert resp.get_json()["data"]["user"]["username"] == "teacher"


def test_refresh_replay_revokes_all_sessions(tmp_path: Path) -> None:
    config = _seed(tmp_path, (("teacher", "teacher", "active"),))
    client = _client(config)
    access = _login(client)["access_token"]
    stolen = _refresh_token(client)

    assert client.post("/api/v1/auth/refresh").status_code == 200
    rotated = _refresh_token(client)
    assert rotated != stolen

    client.set_cookie(REFRESH_COOKIE, stolen, path=COOKIE_PATH)
    replay = client.post("/api/v1/auth/refresh")
    assert replay.status_code == 401
    assert replay.get_json()["code"] == AUTH_REFRESH_REPLAY
    # 重放后 Cookie 必须被清除（Max-Age=0），避免浏览器继续携带失效 RT
    assert "Max-Age=0" in replay.headers.get("Set-Cookie", "")

    engine = create_db_engine(config.get_str("db.url"), "sqlite")
    try:
        with Session(engine) as db:
            user = db.execute(
                select(AccountUser).where(AccountUser.username == "teacher")
            ).scalar_one()
            assert user.token_version == 2
            sessions = db.execute(select(AccountSession)).scalars().all()
            assert len(sessions) == 1
            assert all(row.revoked_at is not None for row in sessions)
            assert all(row.revoke_reason == "suspect_replay" for row in sessions)
            events = db.execute(select(SysLogSecurity.event)).scalars().all()
            assert "AUTH_REFRESH_REPLAY" in events
    finally:
        engine.dispose()

    client.set_cookie(REFRESH_COOKIE, rotated, path=COOKIE_PATH)
    after = client.post("/api/v1/auth/refresh")
    assert after.status_code == 401
    assert after.get_json()["code"] == AUTH_TOKEN_REVOKED

    # 窗口口径：tv 已 +1，但旧 AT 在 TTL 内仍通过（§7.2 已知取舍）
    boot = client.get("/api/v1/auth/bootstrap", headers={"Authorization": f"Bearer {access}"})
    assert boot.status_code == 200


def test_logout_revokes_session(tmp_path: Path) -> None:
    client = _client(_seed(tmp_path, (("teacher", "teacher", "active"),)))
    _login(client)
    token = _refresh_token(client)

    out = client.post("/api/v1/auth/logout")
    assert out.status_code == 200
    assert out.get_json()["data"]["session_revoked"] is True

    client.set_cookie(REFRESH_COOKIE, token, path=COOKIE_PATH)
    again = client.post("/api/v1/auth/refresh")
    assert again.status_code == 401
    assert again.get_json()["code"] == AUTH_TOKEN_REVOKED


# ── 改密（tv+1，RT 立即失效）────────────────────────────────────────────


def test_change_password_bumps_tv_and_kills_refresh(tmp_path: Path) -> None:
    config = _seed(tmp_path, (("teacher", "teacher", "active"),))
    client = _client(config)
    data = _login(client)
    access = data["access_token"]
    old_rt = _refresh_token(client)

    changed = client.patch(
        "/api/v1/auth/password",
        json={"old_password": PASSWORD, "new_password": "N3w-secret-pass"},
        headers={"Authorization": f"Bearer {access}"},
    )
    assert changed.status_code == 200, changed.get_json()
    body = changed.get_json()["data"]
    assert body["token_version"] == 2
    assert body["relogin_required"] is True
    assert body["sessions_revoked"] == 1
    assert body["at_window_sec"] == config.get_int("auth.at_ttl_sec")

    client.set_cookie(REFRESH_COOKIE, old_rt, path=COOKIE_PATH)
    refresh = client.post("/api/v1/auth/refresh")
    assert refresh.status_code == 401
    assert refresh.get_json()["code"] == AUTH_TOKEN_REVOKED

    old_login = client.post(
        "/api/v1/auth/login", json={"username": "teacher", "password": PASSWORD}
    )
    assert old_login.status_code == 401
    new_login = client.post(
        "/api/v1/auth/login", json={"username": "teacher", "password": "N3w-secret-pass"}
    )
    assert new_login.status_code == 200
    assert new_login.get_json()["data"]["user"]["token_version"] == 2


def test_change_password_validates_input(tmp_path: Path) -> None:
    client = _client(_seed(tmp_path, (("teacher", "teacher", "active"),)))
    access = _login(client)["access_token"]
    headers = {"Authorization": f"Bearer {access}"}

    bad_old = client.patch(
        "/api/v1/auth/password",
        json={"old_password": "nope-nope", "new_password": "N3w-secret-pass"},
        headers=headers,
    )
    assert bad_old.status_code == 401
    assert bad_old.get_json()["code"] == AUTH_INVALID_CREDENTIALS

    weak = client.patch(
        "/api/v1/auth/password",
        json={"old_password": PASSWORD, "new_password": "short"},
        headers=headers,
    )
    assert weak.status_code == 422
    assert weak.get_json()["code"] == VALIDATION_FAILED


# ── RBAC 守卫与种子一致性 ────────────────────────────────────────────────


def _guard_blueprint() -> Blueprint:
    bp = Blueprint("m16_guard", __name__)

    @bp.get("/api/v1/_t/manage")
    @require_permission("system:manage")
    def manage() -> tuple[Any, int]:
        return ok({"ok": True})

    return bp


def test_permission_guard(tmp_path: Path) -> None:
    config = _seed(
        tmp_path,
        (("teacher", "teacher", "active"), ("root", "super_admin", "active")),
    )
    app = create_app(config=config, probe=StaticProbe(READY))
    app.register_blueprint(_guard_blueprint())

    teacher = app.test_client()
    teacher_access = _login(teacher, "teacher")["access_token"]
    denied = teacher.get("/api/v1/_t/manage", headers={"Authorization": f"Bearer {teacher_access}"})
    assert denied.status_code == 403
    assert denied.get_json()["code"] == PERMISSION_DENIED
    assert denied.get_json()["data"]["required"] == "system:manage"

    root = app.test_client()
    root_access = _login(root, "root")["access_token"]
    allowed = root.get("/api/v1/_t/manage", headers={"Authorization": f"Bearer {root_access}"})
    assert allowed.status_code == 200


def test_rbac_seed_matches_code(tmp_path: Path) -> None:
    config = _seed(tmp_path)
    engine = create_db_engine(config.get_str("db.url"), "sqlite")
    try:
        with Session(engine) as session:
            permissions = set(session.execute(select(SysPermission.code)).scalars().all())
            assert permissions == PERMISSION_CODES
            for role, expected in ROLE_PERMISSIONS.items():
                actual = set(
                    session.execute(
                        select(SysRolePermission.permission_code).where(
                            SysRolePermission.role_code == role
                        )
                    )
                    .scalars()
                    .all()
                )
                assert actual == expected, role
            assigned = set(
                session.execute(select(SysRolePermission.permission_code)).scalars().all()
            )
            assert UNASSIGNED_PERMISSIONS.isdisjoint(assigned)
            assert "progress:direct" not in assigned
    finally:
        engine.dispose()


def test_bootstrap_includes_mode(tmp_path: Path) -> None:
    config = _seed(tmp_path, (("teacher", "teacher", "active"),))
    engine = create_db_engine(config.get_str("db.url"), "sqlite")
    try:
        with Session(engine) as session:
            session.add(SysConfig(config_key="install.mode", value="external", updated_at=utcnow()))
            session.commit()
    finally:
        engine.dispose()

    client = _client(config)
    access = _login(client)["access_token"]
    boot = client.get("/api/v1/auth/bootstrap", headers={"Authorization": f"Bearer {access}"})
    assert boot.status_code == 200
    data = boot.get_json()["data"]
    assert data["mode"] == "external"
    assert data["user"]["username"] == "teacher"
    assert data["server_time"]


# ── 口令哈希 ─────────────────────────────────────────────────────────────


def test_password_hash_roundtrip() -> None:
    digest = hash_password(PASSWORD)
    assert digest.startswith("$argon2")
    assert verify_password(digest, PASSWORD) is True
    assert verify_password(digest, "wrong") is False
    assert verify_password(None, PASSWORD) is False
    assert verify_password("not-a-hash", PASSWORD) is False
    with pytest.raises(ValueError):
        hash_password("short")


def test_auth_service_rejects_missing_refresh(tmp_path: Path) -> None:
    config = _seed(tmp_path, (("teacher", "teacher", "active"),))
    service = AuthService(config)
    with pytest.raises(AppError) as excinfo:
        service.refresh(None)
    assert excinfo.value.code == AUTH_TOKEN_EXPIRED


# ── SQLite 写锁（M2.16 压测实测：读事务跨 Argon2 → database is locked）──────


def _lock_error() -> OperationalError:
    return OperationalError("UPDATE account_user", {}, Exception("database is locked"))


def test_login_retries_write_session_on_lock(tmp_path: Path, monkeypatch: Any) -> None:
    """Argon2 校验已在事务外，写会话撞写锁只退避重试，不把 500 透给登录页。"""
    config = _seed(tmp_path, (("teacher", "teacher", "active"),))
    service = AuthService(config)
    calls: list[int] = []
    real = service._write_login_session

    def flaky(user_id: int, password_hash: str, now: datetime) -> Any:
        calls.append(user_id)
        if len(calls) == 1:
            raise _lock_error()
        return real(user_id, password_hash, now)

    monkeypatch.setattr(service, "_write_login_session", flaky)
    result = service.login("teacher", PASSWORD)
    assert len(calls) == 2
    assert result.user.username == "teacher"
    # 重试未重复落库：一次成功登录只应留下一个 account_session
    with Session(service.db.engine) as session:
        rows = session.execute(
            select(AccountSession).where(AccountSession.user_id == result.user.id)
        ).scalars()
        assert len(list(rows)) == 1


def test_login_write_session_rejects_stale_password_hash(tmp_path: Path) -> None:
    """读认证字段与写会话之间被并发改密时，按最新哈希复核并拒绝签发。"""
    config = _seed(tmp_path, (("teacher", "teacher", "active"),))
    service = AuthService(config)
    with Session(service.db.engine) as session:
        user_id = int(
            session.execute(
                select(AccountUser.id).where(AccountUser.username == "teacher")
            ).scalar_one()
        )
    with pytest.raises(AppError) as excinfo:
        service._write_login_session(user_id, "stale-hash", datetime.now(UTC))
    assert excinfo.value.code == AUTH_INVALID_CREDENTIALS
