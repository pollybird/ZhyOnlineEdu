"""M1.1 T18~T19：X-ZHY-Request-Id 透传与非法值替换。"""

from __future__ import annotations

import re

from zhyonlineedu.app import create_app
from zhyonlineedu.core.config import load_config


def _client():  # type: ignore[no-untyped-def]
    return create_app(config=load_config(environ={}, sys_config={})).test_client()


def test_valid_request_id_is_echoed() -> None:
    resp = _client().get("/healthz", headers={"X-ZHY-Request-Id": "abc12345"})
    assert resp.headers["X-ZHY-Request-Id"] == "abc12345"


def test_invalid_request_id_is_replaced() -> None:
    resp = _client().get("/healthz", headers={"X-ZHY-Request-Id": "bad id with spaces"})
    generated = resp.headers["X-ZHY-Request-Id"]
    assert re.fullmatch(r"r_[0-9a-f]{16}", generated)


def test_request_id_present_on_error_envelope() -> None:
    resp = _client().get("/api/v1/subjects", headers={"X-ZHY-Request-Id": "req-0001"})
    body = resp.get_json()
    assert resp.status_code == 409
    assert body["request_id"] == "req-0001"
    assert resp.headers["X-ZHY-Request-Id"] == "req-0001"
