"""媒体密钥派生与票据签名（§10.3.3 · F3/F4）。

- AES-128 key **不落库**：`HMAC-SHA256(master, kid)[:16]`，一个视频一个 key，
  kid 与用户解耦，因此 m3u8 可上 CDN；
- 票据签名密钥由主密钥 HKDF 式派生（`HMAC(master, "ticket-sign-v1")`），
  与内容 key 分离：即使签名算法实现出错也拿不到明文密钥；
- 主密钥缺失时**仅 dev** 自动生成 `storage/keys/media_master.key`（0400），
  非 dev 由配置交叉校验强制 `media.aes_key_hex`。
"""

from __future__ import annotations

import hashlib
import hmac
import logging
import os
import secrets
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:  # pragma: no cover - 仅类型检查
    from zhyonlineedu.core.config import Config

logger = logging.getLogger(__name__)

KEY_URI_TEMPLATE = "/api/v1/media/key/{kid}"
SIGN_DOMAIN = b"ticket-sign-v1"
AES_KEY_BYTES = 16


class MediaKeyError(Exception):
    """密钥材料不可用（配置缺主密钥且无法生成）；`reason` 为机器可读原因。"""

    def __init__(self, reason: str) -> None:
        self.reason = reason
        super().__init__(reason)


def _ensure_private_dir(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)
    os.chmod(path, 0o700)


def _write_private(path: Path, data: bytes, *, mode: int = 0o400) -> None:
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, mode)
    try:
        os.fchmod(fd, mode)
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
    except BaseException:
        path.unlink(missing_ok=True)
        raise


def load_master_secret(config: Config, storage_root: Path) -> bytes:
    """读取主密钥（16 字节）；dev 缺省时生成并复用 `storage/keys/media_master.key`。"""
    raw = config.get_str("media.aes_key_hex").strip()
    if raw == "":
        if config.get_str("env") != "dev":
            raise MediaKeyError("master_key_missing")
        raw = _dev_master_hex(storage_root)
    if len(raw) != 32:
        raise MediaKeyError("master_key_invalid")
    try:
        return bytes.fromhex(raw)
    except ValueError as exc:
        raise MediaKeyError("master_key_invalid") from exc


def _dev_master_hex(storage_root: Path) -> str:
    key_dir = storage_root / "keys"
    _ensure_private_dir(key_dir)
    path = key_dir / "media_master.key"
    if path.is_file():
        return path.read_text(encoding="ascii").strip()
    value = secrets.token_hex(16)
    _write_private(path, value.encode("ascii"))
    logger.warning("media.aes_key_hex 未配置，已生成开发用主密钥（非 dev 环境禁止）")
    return value


def derive_aes_key(secret: bytes, kid: str) -> bytes:
    """kid 是匿名资产标识（16 字节 hex）；派生 16 字节 AES-128 key。"""
    return hmac.new(secret, kid.encode("ascii"), hashlib.sha256).digest()[:AES_KEY_BYTES]


def derive_sign_key(secret: bytes) -> bytes:
    return hmac.new(secret, SIGN_DOMAIN, hashlib.sha256).digest()


def sign_query(secret: bytes, *parts: str) -> str:
    """对 `ks|n|p|exp` 等字段做 HMAC-SHA256，取前 32 hex（128 bit）防碰撞足够。"""
    message = b"|".join(part.encode("utf-8") for part in parts)
    return hmac.new(derive_sign_key(secret), message, hashlib.sha256).hexdigest()[:32]


def verify_query(secret: bytes, provided: str, *parts: str) -> bool:
    expected = sign_query(secret, *parts)
    return hmac.compare_digest(expected, provided.lower())


__all__ = [
    "AES_KEY_BYTES",
    "KEY_URI_TEMPLATE",
    "MediaKeyError",
    "SIGN_DOMAIN",
    "derive_aes_key",
    "derive_sign_key",
    "load_master_secret",
    "sign_query",
    "verify_query",
]
