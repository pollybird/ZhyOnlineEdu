"""M1.1 T06~T11：统一信封（成功 / 202 / 业务错误 / 404 / 405 / 500）。"""

from __future__ import annotations

import pytest
import zhyonlineedu.app as app_module
from flask import Blueprint
from zhyonlineedu.app import create_app
from zhyonlineedu.core.capability import ProbeResult, ProbeState, StaticProbe
from zhyonlineedu.core.codes import Code
from zhyonlineedu.core.config import load_config
from zhyonlineedu.core.envelope import accepted_progress, ok
from zhyonlineedu.core.errors import AppError


@pytest.fixture()
def client(monkeypatch):  # type: ignore[no-untyped-def]
    bp = Blueprint("m11_envelope", __name__)

    @bp.get("/api/v1/_e/ok")
    def _ok():  # type: ignore[no-untyped-def]
        return ok({"a": 1})

    @bp.get("/api/v1/_e/accepted")
    def _accepted():  # type: ignore[no-untyped-def]
        return accepted_progress(42, "2026-10-28T12:00:00.000Z")

    @bp.get("/api/v1/_e/expired")
    def _expired():  # type: ignore[no-untyped-def]
        raise AppError(Code.MEDIA_TICKET_EXPIRED, data={"reason": "ttl_refresh"})

    @bp.get("/api/v1/_e/boom")
    def _boom():  # type: ignore[no-untyped-def]
        raise RuntimeError("boom-secret")

    monkeypatch.setattr(
        app_module, "register_business_blueprints", lambda app: app.register_blueprint(bp)
    )
    app = create_app(
        config=load_config(environ={}, sys_config={}),
        probe=StaticProbe(ProbeResult(ProbeState.READY, flavor="sqlite")),
    )
    return app.test_client()


def test_success_envelope(client) -> None:  # type: ignore[no-untyped-def]
    resp = client.get("/api/v1/_e/ok")
    body = resp.get_json()
    assert resp.status_code == 200
    assert body["code"] == "OK"
    assert body["data"] == {"a": 1}
    assert body["request_id"]
    assert body["request_id"] == resp.headers["X-ZHY-Request-Id"]


def test_progress_accepted_is_202(client) -> None:  # type: ignore[no-untyped-def]
    resp = client.get("/api/v1/_e/accepted")
    body = resp.get_json()
    assert resp.status_code == 202
    assert body["code"] == 202
    assert body["message"] == "PROGRESS_ACCEPTED"
    assert body["data"] == {
        "gw_seq": 42,
        "accepted_at": "2026-10-28T12:00:00.000Z",
        "deduped": False,
    }


def test_media_error_has_no_store(client) -> None:  # type: ignore[no-untyped-def]
    resp = client.get("/api/v1/_e/expired")
    body = resp.get_json()
    assert resp.status_code == 401
    assert body["code"] == 40201
    assert body["message"] == "播放票据已过期"
    assert resp.headers["Cache-Control"] == "no-store, private, max-age=0"


def test_404_uses_envelope(client) -> None:  # type: ignore[no-untyped-def]
    resp = client.get("/api/v1/_e/nope")
    body = resp.get_json()
    assert resp.status_code == 404
    assert body["code"] == "NOT_FOUND"
    assert body["request_id"]


def test_405_uses_envelope(client) -> None:  # type: ignore[no-untyped-def]
    resp = client.post("/healthz")
    body = resp.get_json()
    assert resp.status_code == 405
    assert body["code"] == "METHOD_NOT_ALLOWED"


def test_500_hides_stack_trace(client) -> None:  # type: ignore[no-untyped-def]
    resp = client.get("/api/v1/_e/boom")
    body = resp.get_json()
    assert resp.status_code == 500
    assert body["code"] == "INTERNAL_ERROR"
    text = resp.get_data(as_text=True)
    assert "boom-secret" not in text
    assert "Traceback" not in text
