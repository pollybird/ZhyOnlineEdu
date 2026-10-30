"""认证服务：登录 / 刷新轮换 / 登出 / 改密 / 请求鉴权（§7.1 / §7.2）。

关键口径（AUTH-TV-WINDOW）：
- AT 校验**不**逐请求比对 `token_version`：改密后旧 AT 仍可用，窗口 = `auth.at_ttl_sec`；
- RT 校验同时比对 session 有效性与 `token_version`，改密后 RT 立即失效；
- RT 轮换后旧 jti 再次出现 = 重放：吊销全部会话、tv+1、写 `sys_log_security`。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from zhyonlineedu.core.codes import (
    ACCOUNT_DISABLED,
    ACCOUNT_EXPIRED,
    AUTH_INVALID_CREDENTIALS,
    AUTH_REFRESH_REPLAY,
    AUTH_TOKEN_EXPIRED,
    AUTH_TOKEN_REVOKED,
    VALIDATION_FAILED,
)
from zhyonlineedu.core.config import Config
from zhyonlineedu.core.db.engine import Database, ensure_database
from zhyonlineedu.core.db.models.account import (
    AccountSession,
    AccountUser,
    SysLogSecurity,
)
from zhyonlineedu.core.db.models.system import SysConfig
from zhyonlineedu.core.db.query import not_deleted
from zhyonlineedu.core.errors import AppError

from .keys import KeyPair, load_keypair
from .passwords import dummy_verify, hash_password, password_problem, verify_password
from .rbac import permissions_for, roles_for
from .tokens import (
    IssuedTokens,
    TokenExpired,
    TokenInvalid,
    issue_access,
    issue_refresh,
    new_jti,
    new_sid,
    read_token,
)

REFRESH_COOKIE = "zhy_rt"
RT_TTL_SEC = 14 * 24 * 3600  # §7.1：RT 14 天（不随环境改变）
MODE_CONFIG_KEY = "install.mode"


@dataclass(frozen=True, slots=True)
class AuthUser:
    id: int
    username: str
    display_name: str | None
    roles: tuple[str, ...]
    permissions: frozenset[str]
    token_version: int

    def as_dict(self) -> dict[str, object]:
        return {
            "id": self.id,
            "username": self.username,
            "display_name": self.display_name,
            "roles": list(self.roles),
            "permissions": sorted(self.permissions),
            "token_version": self.token_version,
        }


@dataclass(frozen=True, slots=True)
class LoginResult:
    tokens: IssuedTokens
    user: AuthUser


@dataclass(frozen=True, slots=True)
class RefreshResult:
    tokens: IssuedTokens
    user: AuthUser


class AuthService:
    """无状态服务：每次动作自建短会话，事务边界清晰。"""

    def __init__(self, config: Config, *, db: Database | None = None) -> None:
        self.config = config
        self._db = db

    @property
    def db(self) -> Database:
        if self._db is None:
            self._db = ensure_database(self.config)
        return self._db

    @property
    def at_ttl(self) -> int:
        return self.config.get_int("auth.at_ttl_sec")

    # ── 登录 ────────────────────────────────────────────────────────────

    def login(self, username: object, password: object) -> LoginResult:
        name = username.strip() if isinstance(username, str) else ""
        if not name:
            raise AppError(AUTH_INVALID_CREDENTIALS)
        now = datetime.now(UTC)
        with Session(self.db.engine) as session:
            user = session.execute(
                select(AccountUser).where(AccountUser.username == name, not_deleted(AccountUser))
            ).scalar_one_or_none()
            if user is None:
                dummy_verify()
                raise AppError(AUTH_INVALID_CREDENTIALS)
            if not verify_password(user.password_hash, password):
                raise AppError(AUTH_INVALID_CREDENTIALS)
            self._raise_if_inactive(user)

            sid = new_sid()
            jti = new_jti()
            session.add(
                AccountSession(
                    sid=sid,
                    user_id=user.id,
                    refresh_jti=jti,
                    refresh_expires_at=now + timedelta(seconds=RT_TTL_SEC),
                    created_at=now,
                    last_used_at=now,
                )
            )
            user.last_login_at = now
            user.updated_at = now
            session.commit()

            auth_user = self._snapshot(session, user)
            tokens = self._issue(
                user_id=user.id, token_version=user.token_version, sid=sid, jti=jti
            )
        return LoginResult(tokens=tokens, user=auth_user)

    # ── 刷新（轮换 + 重放判定）──────────────────────────────────────────

    def refresh(self, refresh_token: str | None) -> RefreshResult:
        if not refresh_token:
            raise AppError(AUTH_TOKEN_EXPIRED)
        claims = self._decode(refresh_token, "refresh")
        sid = str(claims["zhy_sid"])
        jti = str(claims["jti"])
        now = datetime.now(UTC)

        with Session(self.db.engine) as session:
            row = session.execute(
                select(AccountSession).where(AccountSession.sid == sid)
            ).scalar_one_or_none()
            if row is None or row.revoked_at is not None:
                raise AppError(AUTH_TOKEN_REVOKED)
            if row.refresh_expires_at <= now:
                raise AppError(AUTH_TOKEN_EXPIRED)
            user = session.execute(
                select(AccountUser).where(AccountUser.id == row.user_id, not_deleted(AccountUser))
            ).scalar_one_or_none()
            if user is None:
                raise AppError(AUTH_TOKEN_REVOKED)
            self._raise_if_inactive(user)
            if int(claims["zhy_tv"]) != int(user.token_version):
                raise AppError(AUTH_TOKEN_REVOKED)
            if row.refresh_jti != jti:
                self._handle_replay(session, user, row, now)

            rotated = new_jti()
            row.refresh_jti = rotated
            row.last_used_at = now
            session.commit()
            auth_user = self._snapshot(session, user)
            tokens = self._issue(
                user_id=user.id, token_version=user.token_version, sid=sid, jti=rotated
            )
        return RefreshResult(tokens=tokens, user=auth_user)

    # ── 登出（幂等）────────────────────────────────────────────────────

    def logout(self, *, refresh_token: str | None, access_token: str | None) -> bool:
        sid = self._sid_of(refresh_token, "refresh") or self._sid_of(access_token, "access")
        if not sid:
            return False
        now = datetime.now(UTC)
        with Session(self.db.engine) as session:
            row = session.execute(
                select(AccountSession).where(AccountSession.sid == sid)
            ).scalar_one_or_none()
            if row is None or row.revoked_at is not None:
                return False
            row.revoked_at = now
            row.revoked_by = "self"
            row.revoke_reason = "logout"
            session.commit()
        return True

    # ── 改密（tv+1，RT 全端失效；AT 窗口内仍可用）──────────────────────

    def change_password(
        self, user_id: int, old_password: object, new_password: object
    ) -> dict[str, object]:
        problem = password_problem(new_password)
        if problem is not None:
            raise AppError(VALIDATION_FAILED, data={"errors": [problem]})
        now = datetime.now(UTC)
        with Session(self.db.engine) as session:
            user = session.execute(
                select(AccountUser).where(AccountUser.id == user_id, not_deleted(AccountUser))
            ).scalar_one_or_none()
            if user is None:
                raise AppError(AUTH_TOKEN_REVOKED)
            self._raise_if_inactive(user)
            if not verify_password(user.password_hash, old_password):
                raise AppError(AUTH_INVALID_CREDENTIALS)

            user.password_hash = hash_password(str(new_password))
            user.token_version = int(user.token_version) + 1
            user.updated_at = now
            revoked = self._revoke_user_sessions(
                session, user.id, now=now, by=str(user_id), reason="password_change"
            )
            session.add(
                SysLogSecurity(
                    event="AUTH_PASSWORD_CHANGED",
                    user_id=user.id,
                    detail=f"revoked_sessions={revoked}",
                    created_at=now,
                )
            )
            session.commit()
            version = int(user.token_version)
        return {
            "token_version": version,
            "sessions_revoked": revoked,
            "at_window_sec": self.at_ttl,
        }

    # ── 请求鉴权（AT 无状态；tv 不比对 = 窗口口径）──────────────────────

    def authenticate(self, access_token: str) -> AuthUser:
        claims = self._decode(access_token, "access")
        user_id = int(claims["sub"])
        with Session(self.db.engine) as session:
            user = session.execute(
                select(AccountUser).where(AccountUser.id == user_id, not_deleted(AccountUser))
            ).scalar_one_or_none()
            if user is None:
                raise AppError(AUTH_TOKEN_REVOKED)
            self._raise_if_inactive(user)
            return self._snapshot(session, user)

    # ── 启动信息 ────────────────────────────────────────────────────────

    def bootstrap(self, user: AuthUser) -> dict[str, object]:
        mode: str | None = None
        try:
            with Session(self.db.engine) as session:
                row = session.get(SysConfig, MODE_CONFIG_KEY)
                mode = row.value if row is not None else None
        except SQLAlchemyError:
            mode = None
        return {
            "user": user.as_dict(),
            "mode": mode,
            "server_time": datetime.now(UTC).isoformat(timespec="seconds"),
        }

    # ── 内部 ────────────────────────────────────────────────────────────

    def _key(self) -> KeyPair:
        return load_keypair(
            self.config.get_str("jwt.private_key_path"),
            self.config.get_str("jwt.public_key_path"),
        )

    def _issue(self, *, user_id: int, token_version: int, sid: str, jti: str) -> IssuedTokens:
        key = self._key()
        access, ttl = issue_access(
            user_id=user_id,
            token_version=token_version,
            sid=sid,
            key=key,
            ttl_sec=self.at_ttl,
        )
        refresh = issue_refresh(
            user_id=user_id,
            token_version=token_version,
            sid=sid,
            jti=jti,
            key=key,
            ttl_sec=RT_TTL_SEC,
        )
        return IssuedTokens(
            access_token=access,
            refresh_token=refresh,
            access_expires_in=ttl,
            refresh_expires_in=RT_TTL_SEC,
        )

    def _decode(self, token: str | None, expected_typ: str) -> dict[str, Any]:
        if not token:
            raise AppError(AUTH_TOKEN_EXPIRED)
        try:
            claims = dict(read_token(token, key=self._key(), expected_typ=expected_typ))
        except TokenExpired:
            raise AppError(AUTH_TOKEN_EXPIRED) from None
        except TokenInvalid:
            raise AppError(AUTH_TOKEN_EXPIRED) from None
        return claims

    def _sid_of(self, token: str | None, expected_typ: str) -> str | None:
        if not token:
            return None
        try:
            return str(read_token(token, key=self._key(), expected_typ=expected_typ)["zhy_sid"])
        except (TokenExpired, TokenInvalid):
            return None

    def _snapshot(self, session: Session, user: AccountUser) -> AuthUser:
        return AuthUser(
            id=user.id,
            username=user.username,
            display_name=user.display_name,
            roles=roles_for(session, user.id),
            permissions=permissions_for(session, user.id),
            token_version=user.token_version,
        )

    @staticmethod
    def _raise_if_inactive(user: AccountUser) -> None:
        if user.status == "disabled":
            raise AppError(ACCOUNT_DISABLED)
        if user.status == "expired":
            raise AppError(ACCOUNT_EXPIRED)
        if user.status != "active":
            raise AppError(ACCOUNT_DISABLED)
        # worker 未跑到时按 `expire_at` 兜底，避免过期账号在窗口期内继续使用（§10.5）
        expire_at = user.expire_at
        if expire_at is not None and expire_at <= datetime.now(UTC):
            raise AppError(ACCOUNT_EXPIRED)

    def _handle_replay(
        self, session: Session, user: AccountUser, row: AccountSession, now: datetime
    ) -> None:
        """RT 重放：全端下线 + tv+1 + 安全日志（随后抛 AUTH_REFRESH_REPLAY）。"""
        user.token_version = int(user.token_version) + 1
        user.updated_at = now
        revoked = self._revoke_user_sessions(
            session, user.id, now=now, by="system", reason="suspect_replay"
        )
        session.add(
            SysLogSecurity(
                event="AUTH_REFRESH_REPLAY",
                user_id=user.id,
                detail=f"sid={row.sid};revoked_sessions={revoked}",
                created_at=now,
            )
        )
        session.commit()
        raise AppError(AUTH_REFRESH_REPLAY)

    @staticmethod
    def _revoke_user_sessions(
        session: Session, user_id: int, *, now: datetime, by: str, reason: str
    ) -> int:
        rows = (
            session.execute(
                select(AccountSession).where(
                    AccountSession.user_id == user_id, AccountSession.revoked_at.is_(None)
                )
            )
            .scalars()
            .all()
        )
        for row in rows:
            row.revoked_at = now
            row.revoked_by = by
            row.revoke_reason = reason
        return len(rows)
