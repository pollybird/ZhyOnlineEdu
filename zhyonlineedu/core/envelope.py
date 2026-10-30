"""统一信封：{code, message, data, request_id}（§15.1 / M1.1）。

- 成功：code="OK"；
- 进度：HTTP 202 + code=202（PROGRESS_ACCEPTED），客户端不得当失败；
- 失败：code 为附录 A 数字码或字符串语义码，HTTP 状态由 codes.ERROR_SPECS 决定。
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from flask import Response, g, jsonify

from zhyonlineedu.core.codes import Code, is_media_code, spec_for


@dataclass(frozen=True, slots=True)
class Envelope:
    code: Code | str
    data: Any = None
    message: str | None = None
    request_id: str = ""


def current_request_id() -> str:
    try:
        return str(getattr(g, "request_id", "") or "")
    except RuntimeError:
        return ""


def _respond(
    envelope: Envelope, http: int, headers: Mapping[str, str] | None = None
) -> tuple[Response, int]:
    body: dict[str, Any] = {
        "code": int(envelope.code) if isinstance(envelope.code, Code) else envelope.code
    }
    if envelope.message is not None:
        body["message"] = envelope.message
    body["data"] = envelope.data
    body["request_id"] = envelope.request_id
    response = jsonify(body)
    for name, value in (headers or {}).items():
        response.headers[name] = value
    return response, http


def ok(data: Any = None, *, http: int = 200) -> tuple[Response, int]:
    return _respond(Envelope("OK", data=data, request_id=current_request_id()), http)


def accepted_progress(
    gw_seq: int, accepted_at: str, *, deduped: bool = False
) -> tuple[Response, int]:
    """§15.1：统一 202，携带 gw_seq 与服务端 accepted_at。"""
    spec = spec_for(Code.PROGRESS_ACCEPTED)
    return _respond(
        Envelope(
            Code.PROGRESS_ACCEPTED,
            data={"gw_seq": gw_seq, "accepted_at": accepted_at, "deduped": deduped},
            message="PROGRESS_ACCEPTED",
            request_id=current_request_id(),
        ),
        spec.http,
    )


def fail(
    code: Code | str,
    *,
    data: Any = None,
    headers: Mapping[str, str] | None = None,
) -> tuple[Response, int]:
    spec = spec_for(code)
    merged = dict(headers or {})
    if is_media_code(code):
        merged.setdefault("Cache-Control", "no-store, private, max-age=0")
    return _respond(
        Envelope(code, data=data, message=spec.message, request_id=current_request_id()),
        spec.http,
        merged,
    )
