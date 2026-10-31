"""M1.5 安装向导：状态机、一次性令牌、诊断分类与 finish 固化能力位（§8.5）。"""

from __future__ import annotations

import os
from collections.abc import Iterator
from pathlib import Path

import pytest
from flask import Blueprint
from sqlalchemy import Engine, MetaData, Table, inspect, select
from sqlalchemy.orm import Session
from zhyonlineedu import app as app_module
from zhyonlineedu.app import create_app
from zhyonlineedu.core.capability import ProbeResult, ProbeState, StaticProbe
from zhyonlineedu.core.codes import (
    INSTALL_ALREADY_DONE,
    INSTALL_BLOCKED,
    INSTALL_STEP_INVALID,
    INSTALL_TOKEN_INVALID,
    VALIDATION_FAILED,
    Code,
)
from zhyonlineedu.core.config import Config, load_config
from zhyonlineedu.core.db import compat
from zhyonlineedu.core.db.compat import Cap
from zhyonlineedu.core.db.engine import create_db_engine
from zhyonlineedu.core.db.models.account import AccountUser, SysUserRole
from zhyonlineedu.core.db.models.system import (
    SysConfig,
    SysInstallState,
    SysModeConfig,
    utcnow,
)
from zhyonlineedu.core.db.probe import load_flags
from zhyonlineedu.core.envelope import ok
from zhyonlineedu.core.errors import AppError
from zhyonlineedu.core.install import InstallService, InstallStep, install_token_from_config

TOKEN = "test-install-token"  # noqa: S105 - 测试用固定令牌
SESSION_HEADER = "X-ZHY-Install-Session"
SYSTEM_TABLES = (
    "sys_compat_flags",
    "sys_mode_config",
    "sys_config",
    "sys_install_state",
    "sys_log_security",
    "sys_user_role",
    "sys_role_permission",
    "sys_role",
    "sys_permission",
    "account_session",
    "account_user",
    "alembic_version",
)
DSN_ENV = {"postgresql": "ZHY_TEST_PG_DSN", "mysql": "ZHY_TEST_MYSQL_DSN"}


@pytest.fixture(autouse=True)
def _cleanup() -> Iterator[None]:
    compat.reset()
    yield
    compat.reset()


def _config(tmp_path: Path) -> Config:
    return load_config(
        environ={
            "ZHY_ENV": "dev",
            "ZHY_DB_URL": f"sqlite:///{tmp_path / 'install.db'}",
            "ZHY_DB_FLAVOR": "sqlite",
            "ZHY_INSTALL_TOKEN": TOKEN,
            "ZHY_JWT_PRIVATE_KEY_PATH": str(tmp_path / "keys" / "jwt_private.pem"),
            "ZHY_JWT_PUBLIC_KEY_PATH": str(tmp_path / "keys" / "jwt_public.pem"),
        },
        sys_config={},
    )


def _session(client) -> str:  # type: ignore[no-untyped-def]
    resp = client.post("/api/v1/install/token", json={"token": TOKEN})
    assert resp.status_code == 200, resp.get_json()
    return str(resp.get_json()["data"]["session"])


def _headers(session: str) -> dict[str, str]:
    return {SESSION_HEADER: session}


def _engine(config: Config) -> Engine:
    return create_db_engine(config.get_str("db.url"), config.get_str("db.flavor"))


def _drop_system_tables(engine: Engine) -> None:
    existing = set(inspect(engine).get_table_names())
    for name in SYSTEM_TABLES:
        if name not in existing:
            continue
        with engine.begin() as conn:
            Table(name, MetaData(), autoload_with=conn).drop(conn)


# ── 令牌与会话 ────────────────────────────────────────────────────────────


def test_install_token_generated_when_absent(tmp_path: Path) -> None:
    config = load_config(
        environ={"ZHY_ENV": "dev", "ZHY_DB_URL": f"sqlite:///{tmp_path / 'x.db'}"},
        sys_config={},
    )
    first = install_token_from_config(config)
    second = install_token_from_config(config)
    assert len(first) >= 24
    assert first != second
    assert config.get_str("install.token") == ""
    assert config.redacted()["install.token"] == ""


def test_token_rejects_invalid_and_missing(tmp_path: Path) -> None:
    client = create_app(config=_config(tmp_path)).test_client()
    assert client.post("/api/v1/install/token", json={"token": "bad"}).status_code == 401
    assert client.post("/api/v1/install/token", json={}).status_code == 401
    resp = client.post("/api/v1/install/db/test")
    assert resp.status_code == 401
    assert resp.get_json()["code"] == INSTALL_TOKEN_INVALID


def test_token_rate_limited(tmp_path: Path) -> None:
    client = create_app(config=_config(tmp_path)).test_client()
    for _ in range(5):
        resp = client.post("/api/v1/install/token", json={"token": "bad"})
        assert resp.status_code == 401
    blocked = client.post("/api/v1/install/token", json={"token": "bad"})
    assert blocked.status_code == 429
    assert blocked.get_json()["code"] == int(Code.RATE_LIMITED)


# ── 5 步向导（SQLite 实跑）───────────────────────────────────────────────


def test_full_wizard_sqlite(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    bp = Blueprint("m15_ping", __name__)
    bp.add_url_rule("/api/v1/_t/ping", "ping", lambda: ok({"pong": True}))
    monkeypatch.setattr(
        app_module, "register_business_blueprints", lambda app: app.register_blueprint(bp)
    )

    config = _config(tmp_path)
    client = create_app(config=config).test_client()

    state = client.get("/api/v1/install/state").get_json()["data"]
    assert state["step"] == InstallStep.START.value
    assert state["steps"] == ["start", "db", "admin", "mode", "finish"]
    assert client.get("/api/v1/_t/ping").status_code == 409  # 装配门禁仍在

    session = _session(client)
    tested = client.post("/api/v1/install/db/test", headers=_headers(session)).get_json()["data"]
    assert tested["ok"] is True
    assert tested["flavor"] == "sqlite"
    assert tested["dialect"] == "sqlite"
    assert tested["blockers"] == []
    assert tested["caps"][Cap.RETURNING.value] is True

    setup = client.post("/api/v1/install/db/setup", headers=_headers(session))
    assert setup.status_code == 200, setup.get_json()
    assert setup.get_json()["data"]["step"] == InstallStep.ADMIN.value
    assert setup.get_json()["data"]["already"] is False

    # db/test 与 setup 均未固化能力位；finish 才写 sys_compat_flags
    engine = _engine(config)
    try:
        assert inspect(engine).has_table("sys_compat_flags")
        assert load_flags(engine) == {}
    finally:
        engine.dispose()

    admin = client.post(
        "/api/v1/install/admin",
        json={"username": "root", "password": "S3cret-pass"},
        headers=_headers(session),
    )
    assert admin.status_code == 200, admin.get_json()
    assert admin.get_json()["data"]["step"] == InstallStep.MODE.value

    mode = client.post("/api/v1/install/mode", json={"mode": "external"}, headers=_headers(session))
    assert mode.status_code == 200, mode.get_json()
    assert mode.get_json()["data"]["step"] == InstallStep.FINISH.value

    finished = client.post("/api/v1/install/finish", headers=_headers(session))
    assert finished.status_code == 200, finished.get_json()
    assert finished.get_json()["data"]["installed"] is True
    assert finished.get_json()["data"]["admin"] == "root"

    # 安装完成后：状态 done、healthz READY；业务路由随重启装配（Flask 首请求后禁止注册）
    final = client.get("/api/v1/install/state").get_json()["data"]
    assert final["step"] == InstallStep.DONE.value
    assert final["is_installed"] is True
    assert client.get("/healthz").get_json()["status"] == "ok"
    gated = client.get("/api/v1/_t/ping")
    assert gated.status_code == 409
    assert gated.get_json()["data"]["reason"] == "install_restart_required"

    engine = _engine(config)
    try:
        flags = load_flags(engine)
        assert len(flags) == len(Cap)
        assert flags["returning"] is True
        with Session(engine) as session_db:
            row = session_db.get(SysInstallState, 1)
            assert row is not None
            assert row.install_step == InstallStep.DONE.value
            assert row.is_installed is True
            assert row.installed_at is not None
            assert row.schema_rev == "0013_lesson_duration"
            mode_row = session_db.get(SysModeConfig, ("external", "active"))
            assert mode_row is not None
            assert "returning" in mode_row.compat_flags
            # 首个超管已在 finish 落到账号表并获得 super_admin，暂存键被清理
            admin_user = session_db.execute(
                select(AccountUser).where(AccountUser.username == "root")
            ).scalar_one_or_none()
            assert admin_user is not None
            assert admin_user.password_hash.startswith("$argon2")
            assert admin_user.status == "active"
            assert admin_user.token_version == 1
            assert session_db.get(SysUserRole, (admin_user.id, "super_admin")) is not None
            assert session_db.get(SysConfig, "install.admin.username") is None
            assert session_db.get(SysConfig, "install.admin.password_hash") is None
    finally:
        engine.dispose()

    # 一次性令牌：finish 后会话/令牌均失效，重放返回 ALREADY
    replay = client.post("/api/v1/install/finish", headers=_headers(session))
    assert replay.status_code == 409
    assert replay.get_json()["code"] == INSTALL_ALREADY_DONE
    assert client.post("/api/v1/install/token", json={"token": TOKEN}).status_code == 409

    # 重启后按固化能力位装配：业务路由开放、安装入口保持关闭
    restarted = create_app(
        config=config,
        probe=StaticProbe(ProbeResult(ProbeState.READY, flavor="sqlite", capabilities=flags)),
    ).test_client()
    assert restarted.get("/api/v1/_t/ping").status_code == 200
    assert restarted.post("/api/v1/install/token", json={"token": TOKEN}).status_code == 409


def test_step_order_enforced(tmp_path: Path) -> None:
    client = create_app(config=_config(tmp_path)).test_client()
    session = _session(client)

    admin = client.post(
        "/api/v1/install/admin",
        json={"username": "root", "password": "S3cret-pass"},
        headers=_headers(session),
    )
    assert admin.status_code == 409
    assert admin.get_json()["code"] == INSTALL_STEP_INVALID

    mode = client.post("/api/v1/install/mode", json={"mode": "internal"}, headers=_headers(session))
    assert mode.status_code == 409
    assert mode.get_json()["code"] == INSTALL_STEP_INVALID

    finished = client.post("/api/v1/install/finish", headers=_headers(session))
    assert finished.status_code == 409
    assert finished.get_json()["code"] == INSTALL_STEP_INVALID


def test_steps_are_idempotent(tmp_path: Path) -> None:
    client = create_app(config=_config(tmp_path)).test_client()
    session = _session(client)

    first_setup = client.post("/api/v1/install/db/setup", headers=_headers(session))
    assert first_setup.status_code == 200
    second_setup = client.post("/api/v1/install/db/setup", headers=_headers(session))
    assert second_setup.status_code == 200
    assert second_setup.get_json()["data"]["already"] is True

    payload = {"username": "root", "password": "S3cret-pass"}
    assert (
        client.post("/api/v1/install/admin", json=payload, headers=_headers(session)).status_code
        == 200
    )
    again = client.post("/api/v1/install/admin", json=payload, headers=_headers(session))
    assert again.status_code == 200
    assert again.get_json()["data"]["already"] is True

    conflicting = client.post(
        "/api/v1/install/admin",
        json={"username": "other", "password": "S3cret-pass"},
        headers=_headers(session),
    )
    assert conflicting.status_code == 409
    assert conflicting.get_json()["code"] == INSTALL_STEP_INVALID

    assert (
        client.post(
            "/api/v1/install/mode", json={"mode": "internal"}, headers=_headers(session)
        ).status_code
        == 200
    )
    again_mode = client.post(
        "/api/v1/install/mode", json={"mode": "internal"}, headers=_headers(session)
    )
    assert again_mode.status_code == 200
    assert again_mode.get_json()["data"]["already"] is True


def test_admin_and_mode_validation(tmp_path: Path) -> None:
    client = create_app(config=_config(tmp_path)).test_client()
    session = _session(client)
    client.post("/api/v1/install/db/setup", headers=_headers(session))

    short = client.post(
        "/api/v1/install/admin",
        json={"username": "root", "password": "short"},
        headers=_headers(session),
    )
    assert short.status_code == 422
    assert short.get_json()["code"] == VALIDATION_FAILED

    bad_name = client.post(
        "/api/v1/install/admin",
        json={"username": "a b", "password": "S3cret-pass"},
        headers=_headers(session),
    )
    assert bad_name.status_code == 422

    client.post(
        "/api/v1/install/admin",
        json={"username": "root", "password": "S3cret-pass"},
        headers=_headers(session),
    )
    bad_mode = client.post(
        "/api/v1/install/mode", json={"mode": "unknown"}, headers=_headers(session)
    )
    assert bad_mode.status_code == 422
    assert bad_mode.get_json()["code"] == VALIDATION_FAILED


# ── 诊断分类（blockers）与覆盖保护 ────────────────────────────────────────


def _blocked_probe() -> ProbeResult:
    return ProbeResult(
        ProbeState.FAILED,
        flavor="kingbase",
        capabilities={},
        blockers=(
            "kingbase 要求 database_mode=pg，实际 'oracle'（不符禁止安装）",
            "kingbase 建库字符集为 'gbk'，要求 UTF8/utf8mb4（非 UTF8 禁止安装）",
        ),
    )


def test_blockers_block_next_step_and_are_classified(tmp_path: Path) -> None:
    service = InstallService(_config(tmp_path), probe_runner=_blocked_probe, token=TOKEN)

    tested = service.db_test()
    assert tested["ok"] is False
    assert tested["flavor"] == "kingbase"
    assert tested["dialect"] == "pg_family"
    assert len(tested["blockers"]) == 2

    with pytest.raises(AppError) as excinfo:
        service.db_setup()
    assert excinfo.value.code == INSTALL_BLOCKED
    assert excinfo.value.data["blockers"] == list(_blocked_probe().blockers)


def test_existing_tables_without_alembic_version_refused(tmp_path: Path) -> None:
    config = _config(tmp_path)
    engine = _engine(config)
    try:
        SysInstallState.__table__.create(engine)
    finally:
        engine.dispose()

    ready = ProbeResult(ProbeState.READY, flavor="sqlite", capabilities={"returning": True})
    service = InstallService(config, probe_runner=lambda: ready, token=TOKEN)
    with pytest.raises(AppError) as excinfo:
        service.db_setup()
    assert excinfo.value.code == INSTALL_BLOCKED
    assert "alembic_version" in excinfo.value.data["blockers"][0]


def test_status_reports_unreachable_database(tmp_path: Path) -> None:
    config = load_config(
        environ={
            "ZHY_ENV": "dev",
            "ZHY_DB_URL": f"sqlite:///{tmp_path / 'missing' / 'x.db'}",
            "ZHY_DB_FLAVOR": "sqlite",
            "ZHY_INSTALL_TOKEN": TOKEN,
            "ZHY_JWT_PRIVATE_KEY_PATH": str(tmp_path / "keys" / "jwt_private.pem"),
            "ZHY_JWT_PUBLIC_KEY_PATH": str(tmp_path / "keys" / "jwt_public.pem"),
        },
        sys_config={},
    )
    status = InstallService(config).status()
    assert status.step is InstallStep.START
    assert status.is_installed is False
    assert status.blockers


def test_write_state_row_created_when_missing(tmp_path: Path) -> None:
    config = _config(tmp_path)
    service = InstallService(config, token=TOKEN)
    engine = _engine(config)
    try:
        SysInstallState.__table__.create(engine)
        service._write_state(  # noqa: SLF001 - 覆盖单行状态兜底路径
            engine, schema_rev="x", last_good_rev="x", step=InstallStep.DB
        )
        with Session(engine) as session:
            row = session.get(SysInstallState, 1)
            assert row is not None
            assert row.install_step == InstallStep.DB.value
            assert row.updated_at is not None
            assert row.updated_at <= utcnow()
    finally:
        engine.dispose()


# ── 真实 PG / MySQL（DSN 门控）────────────────────────────────────────────


@pytest.mark.dialect
@pytest.mark.parametrize(("flavor", "env"), sorted(DSN_ENV.items()))
def test_wizard_dsn(flavor: str, env: str) -> None:
    dsn = os.environ.get(env, "").strip()
    if not dsn:
        pytest.skip(f"未设置 {env}，跳过 {flavor} 安装向导实跑")
    config = load_config(
        environ={
            "ZHY_ENV": "dev",
            "ZHY_DB_URL": dsn,
            "ZHY_DB_FLAVOR": flavor,
            "ZHY_INSTALL_TOKEN": TOKEN,
        },
        sys_config={},
    )
    engine = create_db_engine(dsn, flavor)
    try:
        _drop_system_tables(engine)
        service = InstallService(config)
        tested = service.db_test()
        assert tested["blockers"] == [], tested["blockers"]
        assert tested["ok"] is True

        assert service.db_setup()["step"] == InstallStep.ADMIN.value
        assert service.set_admin("root", "S3cret-pass")["step"] == InstallStep.MODE.value
        assert service.set_mode("internal")["step"] == InstallStep.FINISH.value
        finished = service.finish()
        assert finished["installed"] is True
        assert compat.current().source == "install"
        assert len(load_flags(engine)) == len(Cap)
    finally:
        _drop_system_tables(engine)
        engine.dispose()
