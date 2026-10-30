"""M1.2 安装期探测：能力位冒烟、sys_compat_flags 固化、装配门禁。"""

from __future__ import annotations

import os
from collections.abc import Iterator

import pytest
from sqlalchemy import text
from zhyonlineedu.core.capability import ProbeState, StaticProbe
from zhyonlineedu.core.config import load_config
from zhyonlineedu.core.db import compat
from zhyonlineedu.core.db.compat import Cap
from zhyonlineedu.core.db.engine import create_db_engine, dispose_database
from zhyonlineedu.core.db.probe import InstallProbe, load_flags, run_probe


@pytest.fixture(autouse=True)
def _cleanup() -> Iterator[None]:
    compat.reset()
    yield
    dispose_database()
    compat.reset()


def test_probe_sqlite_ready_and_persisted() -> None:
    engine = create_db_engine("sqlite://")
    result = run_probe(engine, "sqlite")
    assert result.state is ProbeState.READY
    assert result.blockers == ()
    assert result.flavor == "sqlite"
    assert result.capabilities["returning"] is True
    assert result.capabilities["upsert_conflict_clause"] is True
    assert result.capabilities["upsert_on_duplicate"] is False

    flags = load_flags(engine)
    assert flags["returning"] is True
    assert len(flags) == len(Cap)

    # 安装期探测后运行时只读能力位即可，不再探测
    assert compat.cap(Cap.RETURNING) is True
    assert compat.dialect().value == "sqlite"

    # 重跑幂等（超管 /system/probe/rerun，§15.2）
    again = run_probe(engine, "sqlite")
    assert again.state is ProbeState.READY
    assert load_flags(engine)["returning"] is True
    engine.dispose()


def test_probe_unreachable_db_failed_with_hint() -> None:
    engine = create_db_engine("sqlite:////nonexistent-zhy-dir/zhy.db", "sqlite")
    result = run_probe(engine, "sqlite")
    assert result.state is ProbeState.FAILED
    assert result.blockers
    assert "sqlite" in result.blockers[0].lower() or "driver" in " ".join(result.blockers)
    engine.dispose()


def test_install_probe_from_config() -> None:
    config = load_config(environ={"ZHY_DB_URL": "sqlite://", "ZHY_DB_FLAVOR": "sqlite"})
    result = InstallProbe(config).probe()
    assert result.state is ProbeState.READY
    assert result.flavor == "sqlite"

    from zhyonlineedu.app import create_app

    app = create_app(config, probe=StaticProbe(result))
    client = app.test_client()
    health = client.get("/healthz")
    assert health.status_code == 200
    assert health.get_json()["caps"]["flavor"] == "sqlite"
    # 探测 READY 后业务路由才注册；未注册路径走统一 404 信封
    missing = client.get("/api/v1/not-registered")
    assert missing.status_code == 404
    body = missing.get_json()
    assert body["code"] == "NOT_FOUND"


def test_flags_table_absent_returns_empty() -> None:
    engine = create_db_engine("sqlite://")
    assert load_flags(engine) == {}
    engine.dispose()


def test_probe_table_cleaned_up() -> None:
    engine = create_db_engine("sqlite://")
    run_probe(engine, "sqlite")
    with engine.connect() as conn:
        count = conn.execute(
            text("SELECT COUNT(*) FROM sqlite_master WHERE name='zhy_probe_caps'")
        ).scalar()
    assert count == 0
    engine.dispose()


REMOTE_DSN_ENV = {
    "postgresql": "ZHY_TEST_PG_DSN",
    "mysql": "ZHY_TEST_MYSQL_DSN",
}


@pytest.mark.dialect
@pytest.mark.parametrize(("flavor", "env"), REMOTE_DSN_ENV.items())
def test_probe_remote_ready_and_idempotent(flavor: str, env: str) -> None:
    """真实 PG/MySQL：探测 → 固化 flags → 重跑幂等（覆盖冲突更新路径）。"""
    dsn = os.environ.get(env, "").strip()
    if not dsn:
        pytest.skip(f"未设置 {env}")
    engine = create_db_engine(dsn, flavor)
    try:
        with engine.begin() as conn:
            conn.execute(text("DROP TABLE IF EXISTS sys_compat_flags"))
        first = run_probe(engine, flavor)
        assert first.state is ProbeState.READY, first.blockers
        flags = load_flags(engine)
        assert len(flags) == len(Cap)
        assert flags["upsert_conflict_clause"] is (flavor == "postgresql")
        assert flags["upsert_on_duplicate"] is (flavor == "mysql")

        second = run_probe(engine, flavor)
        assert second.state is ProbeState.READY, second.blockers
        assert load_flags(engine) == flags
    finally:
        with engine.begin() as conn:
            conn.execute(text("DROP TABLE IF EXISTS sys_compat_flags"))
        engine.dispose()
