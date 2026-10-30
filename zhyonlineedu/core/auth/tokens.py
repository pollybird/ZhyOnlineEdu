"""JWT 签发与校验（RS256，§7.1）。

claim 口径（冻结）：
- 标准：`sub`（用户 id 字符串）/ `jti` / `iat` / `exp` / `iss`；
- 项目：`zhy_typ` = access|refresh、`zhy_tv` = token_version、`zhy_sid` = 会话 sid。

AT 无状态：校验只验签 + exp + 类型；tv 不逐请求比对（窗口 = AT TTL，AUTH-TV-WINDOW）。
"""

from __future__ import annotations

import secrets
import time
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

import jwt

from .keys import KeyPair

ALGORITHM = "RS256"
ISSUER = "zhyonlineedu"
CLAIM_TYP = "zhy_typ"
CLAIM_TV = "zhy_tv"
CLAIM_SID = "zhy_sid"
ACCESS_TYP = "access"
REFRESH_TYP = "refresh"


class TokenExpired(Exception):
    """令牌过期（映射 AUTH_TOKEN_EXPIRED）。"""


class TokenInvalid(Exception):
    """签名/结构/类型不符（统一映射 AUTH_TOKEN_EXPIRED，不泄露细节）。"""


@dataclass(frozen=True, slots=True)
class IssuedTokens:
    access_token: str
    refresh_token: str
    access_expires_in: int
    refresh_expires_in: int


def _encode(claims: Mapping[str, Any], key: KeyPair) -> str:
    return jwt.encode(dict(claims), key.private_key, algorithm=ALGORITHM)


def issue_access(
    *,
    user_id: int,
    token_version: int,
    sid: str,
    key: KeyPair,
    ttl_sec: int,
    now: int | None = None,
) -> tuple[str, int]:
    issued_at = int(time.time()) if now is None else now
    payload = {
        "sub": str(user_id),
        "jti": secrets.token_urlsafe(16),
        "iat": issued_at,
        "exp": issued_at + ttl_sec,
        "iss": ISSUER,
        CLAIM_TYP: ACCESS_TYP,
        CLAIM_TV: token_version,
        CLAIM_SID: sid,
    }
    return _encode(payload, key), ttl_sec


def issue_refresh(
    *,
    user_id: int,
    token_version: int,
    sid: str,
    jti: str,
    key: KeyPair,
    ttl_sec: int,
    now: int | None = None,
) -> str:
    issued_at = int(time.time()) if now is None else now
    payload = {
        "sub": str(user_id),
        "jti": jti,
        "iat": issued_at,
        "exp": issued_at + ttl_sec,
        "iss": ISSUER,
        CLAIM_TYP: REFRESH_TYP,
        CLAIM_TV: token_version,
        CLAIM_SID: sid,
    }
    return _encode(payload, key)


def read_token(token: str, *, key: KeyPair, expected_typ: str) -> Mapping[str, Any]:
    """校验签名/exp/iss/必需 claim 与类型；失败抛 TokenExpired / TokenInvalid。"""
    try:
        claims: dict[str, Any] = jwt.decode(
            token,
            key.public_key,
            algorithms=[ALGORITHM],
            issuer=ISSUER,
            options={
                "require": ["sub", "jti", "iat", "exp", "iss", CLAIM_TYP, CLAIM_TV, CLAIM_SID]
            },
        )
    except jwt.ExpiredSignatureError as exc:
        raise TokenExpired from exc
    except jwt.InvalidTokenError as exc:
        raise TokenInvalid from exc
    if str(claims.get(CLAIM_TYP)) != expected_typ:
        raise TokenInvalid("token 类型不符")
    try:
        int(claims["sub"])
        int(claims[CLAIM_TV])
    except (KeyError, TypeError, ValueError) as exc:
        raise TokenInvalid("token claim 非数值") from exc
    return claims


def new_jti() -> str:
    return secrets.token_urlsafe(24)


def new_sid() -> str:
    return secrets.token_urlsafe(24)
