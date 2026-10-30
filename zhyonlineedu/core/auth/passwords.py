"""口令哈希与强度校验（Argon2id，§7.1）。

安装向导与登录共用同一实现，避免两处参数漂移；哈希串形如 `$argon2id$...`。
"""

from __future__ import annotations

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError, VerifyMismatchError

MIN_PASSWORD_LEN = 8
MAX_PASSWORD_LEN = 128

_hasher = PasswordHasher()
# 账号不存在时也跑一次校验，抹平"用户是否存在"的时间差（防枚举）
_DUMMY_HASH = _hasher.hash("zhy-dummy-password-for-timing")


def password_problem(password: object) -> str | None:
    """返回可读的强度问题；合法返回 None。"""
    if not isinstance(password, str):
        return "password 必须是字符串"
    if not MIN_PASSWORD_LEN <= len(password) <= MAX_PASSWORD_LEN:
        return f"password 长度需为 {MIN_PASSWORD_LEN}~{MAX_PASSWORD_LEN} 位"
    return None


def hash_password(password: str) -> str:
    problem = password_problem(password)
    if problem is not None:
        raise ValueError(problem)
    return _hasher.hash(password)


def verify_password(password_hash: str | None, password: object) -> bool:
    """常量语义的校验入口：空哈希/格式错误/不匹配统一返回 False。"""
    if not isinstance(password, str) or not password_hash:
        _hasher.verify(_DUMMY_HASH, "zhy-dummy-password-for-timing")
        return False
    try:
        return _hasher.verify(password_hash, password)
    except (VerifyMismatchError, VerificationError, InvalidHashError):
        return False


def dummy_verify() -> None:
    """账号不存在时调用，保持与外层真实校验接近的耗时。"""
    _hasher.verify(_DUMMY_HASH, "zhy-dummy-password-for-timing")
