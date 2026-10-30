"""M1.1 T01~T05：/healthz、/readyz、安装门禁、Blueprint 注册门禁。"""

from __future__ import annotations

import zhyonlineedu.app as app_module
from flask import Blueprint
from zhyonlineedu.app import create_app
from zhyonlineedu.core.capability import ProbeResult, ProbeState, StaticProbe
from zhyonlineedu.core.config import load_config
from zhyonlineedu.core.envelope import ok


def _config():  # type: ignore[no-untyped-def]
    # 内存库：安装向导 GET /state 会连库探测，避免在仓库根目录落下 sqlite 文件
    return load_config(environ={"ZHY_DB_URL": "sqlite://"}, sys_config={})


def _ready():  # type: ignore[no-untyped-def]
    return StaticProbe(
        ProbeResult(ProbeState.READY, flavor="sqlite", capabilities={"returning": True})
    )


class _CountingProbe:
    def __init__(self) -> None:
        self.calls = 0

    def probe(self) -> ProbeResult:
        self.calls += 1
        return ProbeResult(ProbeState.READY, flavor="sqlite")


def test_healthz_reports_pending() -> None:
    client = create_app(config=_config()).test_client()
    resp = client.get("/healthz")
    body = resp.get_json()
    assert resp.status_code == 200
    assert body["status"] == "warn"
    assert body["detail"] == "cap_probe_pending"
    assert body["checks"]["db"] == "pending"
    assert "caps" not in body


def test_healthz_reports_ready_caps() -> None:
    client = create_app(config=_config(), probe=_ready()).test_client()
    body = client.get("/healthz").get_json()
    assert body["status"] == "ok"
    assert body["detail"] == "cap_probe_ready"
    assert body["caps"]["flavor"] == "sqlite"
    assert body["caps"]["capabilities"]["returning"] is True


def test_readyz_status_codes() -> None:
    assert create_app(config=_config()).test_client().get("/readyz").status_code == 503
    assert (
        create_app(config=_config(), probe=_ready()).test_client().get("/readyz").status_code == 200
    )


def test_probe_called_once_across_health_checks() -> None:
    counter = _CountingProbe()
    client = create_app(config=_config(), probe=counter).test_client()
    client.get("/healthz")
    client.get("/healthz")
    client.get("/readyz")
    assert counter.calls == 1


def test_install_gate_blocks_business_prefix() -> None:
    client = create_app(config=_config()).test_client()
    resp = client.get("/api/v1/subjects")
    body = resp.get_json()
    assert resp.status_code == 409
    assert body["code"] == "INSTALL_REQUIRED"
    assert body["data"]["probe_state"] == "pending"


def test_install_path_is_not_gated() -> None:
    client = create_app(config=_config()).test_client()
    resp = client.get("/api/v1/install/state")
    assert resp.status_code == 200
    body = resp.get_json()
    assert body["code"] == "OK"
    assert body["data"]["step"] == "start"


def test_business_blueprint_skipped_while_pending(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    bp = Blueprint("m11_pending", __name__)
    bp.add_url_rule("/api/v1/_t/ping", "ping", lambda: ok({"pong": True}))
    monkeypatch.setattr(
        app_module, "register_business_blueprints", lambda app: app.register_blueprint(bp)
    )

    app = create_app(config=_config())
    rules = {rule.rule for rule in app.url_map.iter_rules()}
    assert "/api/v1/_t/ping" not in rules


def test_business_blueprint_registered_when_ready(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    bp = Blueprint("m11_ready", __name__)
    bp.add_url_rule("/api/v1/_t/ping", "ping", lambda: ok({"pong": True}))
    monkeypatch.setattr(
        app_module, "register_business_blueprints", lambda app: app.register_blueprint(bp)
    )

    client = create_app(config=_config(), probe=_ready()).test_client()
    resp = client.get("/api/v1/_t/ping")
    assert resp.status_code == 200
    assert resp.get_json()["data"] == {"pong": True}
