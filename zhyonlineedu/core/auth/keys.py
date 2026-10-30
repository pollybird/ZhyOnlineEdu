"""RS256 密钥对：安装期生成、运行时只读（§7.1）。

- 私钥 PEM（PKCS#8）仅本机 0600；公钥 PEM 供其它服务验签；
- 生成函数幂等：私钥已存在时只补公钥，不覆盖历史私钥（避免已签发令牌全部失效）；
- `load_keypair` 带进程内缓存：路径不变时不重复读盘。
"""

from __future__ import annotations

from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa


class JwtKeyError(RuntimeError):
    """密钥缺失/格式不符。"""


@dataclass(frozen=True, slots=True)
class KeyPair:
    private_key: rsa.RSAPrivateKey
    public_key: rsa.RSAPublicKey


def _write_private(path: Path, key: rsa.RSAPrivateKey) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    pem = key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    )
    path.write_bytes(pem)
    path.chmod(0o600)


def _write_public(path: Path, key: rsa.RSAPublicKey) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    pem = key.public_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PublicFormat.SubjectPublicKeyInfo,
    )
    path.write_bytes(pem)
    path.chmod(0o644)


def _load_private(path: Path) -> rsa.RSAPrivateKey:
    try:
        loaded = serialization.load_pem_private_key(path.read_bytes(), password=None)
    except (OSError, ValueError) as exc:
        raise JwtKeyError(f"JWT 私钥不可读或格式不符: {path}（{type(exc).__name__}）") from exc
    if not isinstance(loaded, rsa.RSAPrivateKey):
        raise JwtKeyError(f"JWT 私钥不是 RSA: {path}")
    return loaded


def ensure_keypair(private_path: str | Path, public_path: str | Path) -> KeyPair:
    """安装期调用：缺失则生成 RSA-2048；已有私钥时只补公钥文件。"""
    private = Path(private_path)
    public = Path(public_path)
    if not private.exists():
        key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        _write_private(private, key)
    else:
        key = _load_private(private)
    if not public.exists():
        _write_public(public, key.public_key())
    return KeyPair(private_key=key, public_key=key.public_key())


@lru_cache(maxsize=8)
def load_keypair(private_path: str, public_path: str) -> KeyPair:
    """运行时读取（缓存）；改密/安装完成后仍在同一进程内复用。"""
    private = Path(private_path)
    if not private.exists():
        raise JwtKeyError(f"JWT 私钥不存在: {private}（请先完成安装或执行 keys init）")
    key = _load_private(private)
    public = Path(public_path)
    if public.exists():
        try:
            loaded_public = serialization.load_pem_public_key(public.read_bytes())
        except (OSError, ValueError) as exc:
            raise JwtKeyError(f"JWT 公钥不可读或格式不符: {public}") from exc
        if not isinstance(loaded_public, rsa.RSAPublicKey):
            raise JwtKeyError(f"JWT 公钥不是 RSA: {public}")
        if loaded_public.public_numbers() != key.public_key().public_numbers():
            raise JwtKeyError("JWT 公钥与私钥不匹配（请检查安装期生成的文件）")
        return KeyPair(private_key=key, public_key=loaded_public)
    return KeyPair(private_key=key, public_key=key.public_key())


def clear_key_cache() -> None:
    """测试/密钥轮换后清缓存。"""
    load_keypair.cache_clear()
