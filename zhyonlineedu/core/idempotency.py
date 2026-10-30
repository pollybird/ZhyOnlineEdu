"""`Idempotency-Key` 解析与请求指纹（§10.4 / M2.6）。

约定：
- 交卷类**不可重试的写操作**必须携带 `Idempotency-Key`，值即作答自然键（`attempt_uid`）；
- key 与服务端持有的事实唯一索引绑定 → 同一 key 重试只返回首次结果；
- 指纹只用于识别「同 key 不同 payload」的误用（命中即 409 `CONFLICT_RETRY`），
  不参与业务判定，避免把客户端时间戳/浮点噪声当成新请求。
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping
from typing import Any

from flask import request

from zhyonlineedu.core.codes import VALIDATION_FAILED
from zhyonlineedu.core.errors import AppError

HEADER = "Idempotency-Key"
KEY_MAX_LEN = 64
_KEY = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{7,63}$")


def idempotency_key(*, required: bool = True) -> str | None:
    """读取并校验请求头；缺失/非法一律 422（带 `header` 字段便于前端提示）。"""
    raw = request.headers.get(HEADER)
    if raw is None or not raw.strip():
        if not required:
            return None
        raise AppError(
            VALIDATION_FAILED,
            data={"header": HEADER, "errors": [f"写操作必须携带 {HEADER}"]},
        )
    key = raw.strip()
    if not _KEY.fullmatch(key):
        raise AppError(
            VALIDATION_FAILED,
            data={
                "header": HEADER,
                "errors": [f"{HEADER} 必须是 8~{KEY_MAX_LEN} 位字母数字与 . _ : -"],
            },
        )
    return key


def payload_hash(payload: Mapping[str, Any] | list[Any] | None) -> str:
    """规范化 JSON 的 sha256（键排序 + 紧凑分隔符），跨机器稳定。"""
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


__all__ = ["HEADER", "KEY_MAX_LEN", "idempotency_key", "payload_hash"]
