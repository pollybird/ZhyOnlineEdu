"""M3.9 三库回归：插件框架在 PostgreSQL / MySQL 上的真实生命周期。

SQLite 之外的库由 DSN 环境变量门控（与 `test_dialect_matrix` / `test_migrations` 同口径）：
`ZHY_TEST_PG_DSN` / `ZHY_TEST_MYSQL_DSN`；未设置即跳过，CI 的 `db` 作业注入容器地址。

覆盖点（三库一致的语义）：
1. 内核迁移链 `upgrade_head` 在真实库跑通；
2. 插件独立迁移链建表（`plg_<code>_` + `delete_seq`），`schema_rev` 落注册表；
3. 启用后 `setup()` 订阅生效，事件派发经 `sdk.store` 写入插件表；
4. 禁用摘订阅、`dispatch` 变 404；卸载把插件表降级删除（不影响内核表）。
"""

from __future__ import annotations

import contextlib
import os
import shutil
from collections.abc import Iterator
from pathlib import Path

import pytest
from flask import Flask
from sqlalchemy import MetaData, create_engine, inspect
from zhyonlineedu.core.auth.keys import clear_key_cache
from zhyonlineedu.core.codes import PLUGIN_NOT_FOUND
from zhyonlineedu.core.config import Config, load_config
from zhyonlineedu.core.db import compat
from zhyonlineedu.core.db.engine import ensure_database
from zhyonlineedu.core.db.migration import upgrade_head
from zhyonlineedu.core.errors import AppError
from zhyonlineedu.core.events import EVENT_LESSON_FINISHED, EventBus
from zhyonlineedu.modules.plugin.service import PluginService

DSN_ENV = {"postgresql": "ZHY_TEST_PG_DSN", "mysql": "ZHY_TEST_MYSQL_DSN"}
CODE = "reference_certificate"
CERT_TABLE = "plg_reference_certificate_cert"
USER_ID = 11
LESSON_ID = 22
COURSE_ID = 33


@pytest.fixture(autouse=True)
def _cleanup() -> Iterator[None]:
    clear_key_cache()
    yield
    compat.reset()
    clear_key_cache()


def _dsn(env: str) -> str:
    dsn = os.environ.get(env, "").strip()
    if not dsn:
        pytest.skip(f"未设置 {env}，跳过真实库插件回归")
    return dsn


def _config(tmp_path: Path, dsn: str, flavor: str) -> Config:
    plugin_root = tmp_path / "plugins"
    source = Path(__file__).resolve().parent.parent / "plugins"
    shutil.copytree(source / CODE, plugin_root / CODE)
    return load_config(
        environ={
            "ZHY_ENV": "dev",
            "ZHY_DB_URL": dsn,
            "ZHY_DB_FLAVOR": flavor,
            "ZHY_JWT_PRIVATE_KEY_PATH": str(tmp_path / "keys" / "jwt_private.pem"),
            "ZHY_JWT_PUBLIC_KEY_PATH": str(tmp_path / "keys" / "jwt_public.pem"),
            "ZHY_STORAGE_LOCAL_DIR": str(tmp_path / "storage"),
            "ZHY_PROGRESS_RING_DIR": str(tmp_path / "ring"),
            "ZHY_PLUGIN_DIR": str(plugin_root),
        },
        sys_config={},
    )


def _tables(config: Config) -> set[str]:
    engine = ensure_database(config).engine
    try:
        return set(inspect(engine).get_table_names())
    finally:
        engine.dispose()


def _reset(dsn: str) -> None:
    """清空真实库：多个用例共用同一 DSN，避免上一轮/其它套件残留表导致建表冲突。"""
    engine = create_engine(dsn)
    try:
        meta = MetaData()
        meta.reflect(bind=engine)
        meta.drop_all(bind=engine)
    finally:
        engine.dispose()


@pytest.mark.parametrize(("flavor", "env"), sorted(DSN_ENV.items()))
def test_plugin_lifecycle_on_real_db(tmp_path: Path, flavor: str, env: str) -> None:
    dsn = _dsn(env)
    _reset(dsn)
    upgrade_head(dsn, flavor)
    compat.configure(flavor=flavor)

    config = _config(tmp_path, dsn, flavor)
    bus = EventBus()
    service = PluginService(config, events=bus)
    app = Flask("plugin-multidb")
    app.extensions["zhy_config"] = config
    app.extensions["zhy_events"] = bus

    try:
        with app.app_context():
            installed = service.install(CODE)
            assert installed.schema_rev == "reference_certificate_0001"
            assert CERT_TABLE in _tables(config)

            service.enable(CODE)
            assert [sub.owner for sub in bus.subscriptions(EVENT_LESSON_FINISHED)] == [CODE]
            assert service.dispatch(CODE, "certificates") == {"items": [], "total": 0}

            payload = {
                "user": USER_ID,
                "lesson": LESSON_ID,
                "course": COURSE_ID,
                "percent": 100.0,
                "via_ticket_preview": True,
            }
            assert bus.emit(EVENT_LESSON_FINISHED, payload).ok is True
            assert service.dispatch(CODE, "certificates")["total"] == 0  # 试看票不发证

            report = bus.emit(EVENT_LESSON_FINISHED, {**payload, "via_ticket_preview": False})
            assert report.ok is True, report.as_dict()
            data = service.dispatch(CODE, "certificates")
            assert data["total"] == 1
            assert data["items"][0]["percent"] == "100.00"

            service.disable(CODE)
            assert bus.subscriptions(EVENT_LESSON_FINISHED) == ()
            with pytest.raises(AppError) as gone:
                service.dispatch(CODE, "certificates")
            assert gone.value.code == PLUGIN_NOT_FOUND

            service.uninstall(CODE)
            assert CERT_TABLE not in _tables(config)
    finally:
        # 卸载断言失败时也要回收插件表，避免污染后续 flavor 的运行
        with contextlib.suppress(AppError):
            service.uninstall(CODE)
