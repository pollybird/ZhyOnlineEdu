"""M3.1~M3.3 插件框架：manifest / 生命周期 / 独立迁移链（§11 · 附录 E）。

契约口径（PLUGIN-001~012）：
001 安装 → 启用 → 路由可访问（统一入口 `/api/v1/plugin/<code>/<subpath>`）；
002 禁用即时生效：残留路由 404，内核无需重启；
003 能力越权（不在白名单 / `capabilities_v2` 置 true）→ `PLUGIN_CAP_DENIED`，不进注册表；
004 表前缀隔离：manifest 声明与实际建表都必须是 `plg_<code>_`；
005 插件表必须含 `delete_seq`（D19a），缺失即拒装并回滚；
006 迁移源码命中方言黑名单 → 拒装（与 CI 同一份 `core.db.sql_guard`）；
007 迁移幂等：重复安装不重复建表；卸载先降级再删注册表行；
008 enabled 态直接卸载 → `PLUGIN_STATE_INVALID`；
009 鉴权：管理接口无令牌 401、学生 403；分发接口同样要求登录态；
010 重启自动挂载 state=enabled 的插件；manifest 篡改后标记 failed 且内核照常启动；
011 manifest 非法（缺字段 / `core_api_min` 过高 / 入口缺失 / 裸写令牌）→ 422；
012 未知子路径 404；未安装插件 404（不泄露是否存在）。
"""

from __future__ import annotations

import json
import shutil
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import Engine, inspect, select
from sqlalchemy.orm import Session
from zhyonlineedu.app import create_app
from zhyonlineedu.core.auth.keys import clear_key_cache, ensure_keypair
from zhyonlineedu.core.auth.passwords import hash_password
from zhyonlineedu.core.capability import ProbeResult, ProbeState, StaticProbe
from zhyonlineedu.core.codes import (
    AUTH_TOKEN_EXPIRED,
    NOT_FOUND,
    PERMISSION_DENIED,
    PLUGIN_CAP_DENIED,
    PLUGIN_LOAD_FAILED,
    PLUGIN_MANIFEST_INVALID,
    PLUGIN_MIGRATION_INVALID,
    PLUGIN_NOT_FOUND,
    PLUGIN_STATE_INVALID,
    PLUGIN_TABLE_PREFIX_INVALID,
)
from zhyonlineedu.core.config import Config, load_config
from zhyonlineedu.core.db import compat
from zhyonlineedu.core.db.engine import create_db_engine
from zhyonlineedu.core.db.migration import upgrade_head
from zhyonlineedu.core.db.models.account import AccountUser, SysLogSecurity, SysUserRole
from zhyonlineedu.core.db.models.plugin import PluginRegistry
from zhyonlineedu.core.db.models.system import utcnow
from zhyonlineedu.core.errors import AppError
from zhyonlineedu.core.plugin.manifest import load_manifest, validate_manifest

PASSWORD = "S3cret-pass"  # noqa: S105 - 测试用固定口令
READY = ProbeResult(ProbeState.READY, flavor="sqlite", capabilities={"returning": True})

SERVER_SOURCE = """\
from zhyonlineedu.core import sdk


def _hello():
    return {"message": "hello from plugin"}


def _flavor():
    # 验证 dispatch 已绑定 SDK 作用域（manifest 身份源自内核装配）
    return {"flavor": sdk.compat.flavor()}


HANDLERS = {"hello": _hello, "flavor": _flavor}
"""

PLUGIN_CODE = "demo"
OTHER_CODE = "plain"


@pytest.fixture(autouse=True)
def _cleanup() -> Iterator[None]:
    compat.reset()
    clear_key_cache()
    yield
    compat.reset()
    clear_key_cache()


# ── 装配 ──────────────────────────────────────────────────────────────────


def _config(tmp_path: Path, plugin_root: Path, **overrides: str) -> Config:
    environ = {
        "ZHY_ENV": "dev",
        "ZHY_DB_URL": f"sqlite:///{tmp_path / 'plugin.db'}",
        "ZHY_DB_FLAVOR": "sqlite",
        "ZHY_JWT_PRIVATE_KEY_PATH": str(tmp_path / "keys" / "jwt_private.pem"),
        "ZHY_JWT_PUBLIC_KEY_PATH": str(tmp_path / "keys" / "jwt_public.pem"),
        "ZHY_STORAGE_LOCAL_DIR": str(tmp_path / "storage"),
        "ZHY_PROGRESS_RING_DIR": str(tmp_path / "ring"),
        "ZHY_PLUGIN_DIR": str(plugin_root),
    }
    environ.update(overrides)
    return load_config(environ=environ, sys_config={})


def _seed(config: Config) -> None:
    ensure_keypair(config.get_str("jwt.private_key_path"), config.get_str("jwt.public_key_path"))
    url = config.get_str("db.url")
    upgrade_head(url, "sqlite")
    engine = create_db_engine(url, "sqlite")
    try:
        with Session(engine) as session:
            now = utcnow()
            for username, role in (("root", "super_admin"), ("student", "student")):
                user = AccountUser(
                    username=username,
                    password_hash=hash_password(PASSWORD),
                    status="active",
                    token_version=1,
                    registration_mode="admin",
                    created_at=now,
                    updated_at=now,
                )
                session.add(user)
                session.flush()
                session.add(SysUserRole(user_id=user.id, role_code=role))
            session.commit()
    finally:
        engine.dispose()


def _engine(config: Config) -> Engine:
    return create_db_engine(config.get_str("db.url"), "sqlite")


def _client(config: Config) -> Any:
    return create_app(config=config, probe=StaticProbe(READY)).test_client()


def _login(client: Any, username: str) -> dict[str, str]:
    response = client.post("/api/v1/auth/login", json={"username": username, "password": PASSWORD})
    assert response.status_code == 200, response.get_json()
    return {"Authorization": f"Bearer {response.get_json()['data']['access_token']}"}


# ── 插件夹具 ──────────────────────────────────────────────────────────────


def _manifest(code: str = PLUGIN_CODE, **overrides: Any) -> dict[str, Any]:
    data: dict[str, Any] = {
        "code": code,
        "name": f"演示插件 {code}",
        "version": "1.0.0",
        "core_api_min": "0.2.0",
        "entry_server": "server.py",
        "supported_modes": ["internal", "external"],
        "capabilities": ["progress:read"],
        "routes": ["hello", "flavor"],
        "menu": [{"title": "插件演示", "path": "/system/plugins"}],
    }
    data.update(overrides)
    return data


def _migration_source(
    code: str,
    *,
    index: int = 1,
    table: str | None = None,
    down_revision: str | None | object = ...,
    delete_seq: bool = True,
) -> str:
    table_name = table or f"plg_{code}_note"
    if down_revision is ...:
        down = None if index == 1 else f"{code}_000{index - 1}"
    else:
        down = down_revision
    columns = [
        '        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),',
        '        sa.Column("body", sa.String(length=64), nullable=False),',
        '        sa.Column("version", sa.Integer(), server_default="1", nullable=False),',
    ]
    if delete_seq:
        columns.append(
            '        sa.Column("delete_seq", sa.BigInteger(), server_default="0", nullable=False),'
        )
    columns.append('        sa.PrimaryKeyConstraint("id"),')
    lines = [
        "import sqlalchemy as sa",
        "",
        f'revision = "{code}_000{index}"',
        f"down_revision = {down!r}",
        "",
        "",
        "def upgrade(op):",
        "    op.create_table(",
        f'        "{table_name}",',
        *columns,
        "    )",
        "",
        "",
        "def downgrade(op):",
        f'    op.drop_table("{table_name}")',
        "",
    ]
    return "\n".join(lines)


def _write_plugin(
    root: Path,
    code: str = PLUGIN_CODE,
    *,
    manifest: dict[str, Any] | None = None,
    server: str = SERVER_SOURCE,
    migrations: dict[str, str] | None = None,
    entry: str = "server.py",
) -> Path:
    plugin_dir = root / code
    plugin_dir.mkdir(parents=True, exist_ok=True)
    data = manifest if manifest is not None else _manifest(code, entry_server=entry)
    (plugin_dir / "manifest.json").write_text(
        json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    (plugin_dir / entry).write_text(server, encoding="utf-8")
    for name, source in (migrations or {}).items():
        migrations_dir = plugin_dir / "migrations"
        migrations_dir.mkdir(exist_ok=True)
        (migrations_dir / name).write_text(source, encoding="utf-8")
    return plugin_dir


def _setup(tmp_path: Path, *, with_migration: bool = True) -> tuple[Config, dict[str, Path]]:
    plugin_root = tmp_path / "plugins"
    plugin_root.mkdir()
    migrations = {"0001_init.py": _migration_source(PLUGIN_CODE)} if with_migration else None
    dirs = {
        PLUGIN_CODE: _write_plugin(plugin_root, PLUGIN_CODE, migrations=migrations),
        OTHER_CODE: _write_plugin(plugin_root, OTHER_CODE, migrations=None),
    }
    config = _config(tmp_path, plugin_root)
    _seed(config)
    return config, dirs


def _registry(config: Config, code: str) -> PluginRegistry | None:
    engine = _engine(config)
    try:
        with Session(engine) as session:
            return session.get(PluginRegistry, code)
    finally:
        engine.dispose()


def _tables(config: Config) -> set[str]:
    engine = _engine(config)
    try:
        return set(inspect(engine).get_table_names())
    finally:
        engine.dispose()


def _events(config: Config) -> list[str]:
    engine = _engine(config)
    try:
        with Session(engine) as session:
            return list(
                session.execute(select(SysLogSecurity.event).order_by(SysLogSecurity.id)).scalars()
            )
    finally:
        engine.dispose()


# ── 001 / 002：安装 → 启用 → 分发 → 禁用 ───────────────────────────────────


def test_plugin_install_enable_dispatch_disable(tmp_path: Path) -> None:
    config, _ = _setup(tmp_path)
    client = _client(config)
    admin = _login(client, "root")
    student = _login(client, "student")
    path = f"/api/v1/plugin/{PLUGIN_CODE}/hello"

    installed = client.post(f"/api/v1/system/plugins/{PLUGIN_CODE}/install", headers=admin)
    assert installed.status_code == 200, installed.get_json()
    data = installed.get_json()["data"]
    assert data["state"] == "disabled"
    assert data["schema_rev"] == f"{PLUGIN_CODE}_0001"
    assert data["table_prefix"] == f"plg_{PLUGIN_CODE}_"
    assert f"plg_{PLUGIN_CODE}_note" in _tables(config)
    assert data["routes"] == ["hello", "flavor"]

    # 未启用：分发入口 404（不泄露插件是否存在）
    gated = client.get(path, headers=student)
    assert gated.status_code == 404
    assert gated.get_json()["code"] == PLUGIN_NOT_FOUND

    enabled = client.post(f"/api/v1/system/plugins/{PLUGIN_CODE}/enable", headers=admin)
    assert enabled.status_code == 200, enabled.get_json()
    assert enabled.get_json()["data"]["state"] == "enabled"

    served = client.get(path, headers=student)
    assert served.status_code == 200, served.get_json()
    assert served.get_json()["data"]["message"] == "hello from plugin"

    # 分发入口已绑定 SDK 作用域（sdk.compat 只读可用）
    flavor = client.get(f"/api/v1/plugin/{PLUGIN_CODE}/flavor", headers=student)
    assert flavor.status_code == 200, flavor.get_json()
    assert flavor.get_json()["data"]["flavor"] == "sqlite"

    disabled = client.post(f"/api/v1/system/plugins/{PLUGIN_CODE}/disable", headers=admin)
    assert disabled.status_code == 200, disabled.get_json()
    assert disabled.get_json()["data"]["state"] == "disabled"

    # 禁用即时生效（不重启）：残留路由 404，插件表仍在（禁用不动数据）
    gone = client.get(path, headers=student)
    assert gone.status_code == 404
    assert gone.get_json()["code"] == PLUGIN_NOT_FOUND
    assert f"plg_{PLUGIN_CODE}_note" in _tables(config)

    events = _events(config)
    assert "PLUGIN_INSTALL" in events
    assert "PLUGIN_ENABLE" in events
    assert "PLUGIN_DISABLE" in events


def test_plugin_dispatch_unknown_subpath_is_404(tmp_path: Path) -> None:
    config, _ = _setup(tmp_path)
    client = _client(config)
    admin = _login(client, "root")
    client.post(f"/api/v1/system/plugins/{PLUGIN_CODE}/install", headers=admin)
    client.post(f"/api/v1/system/plugins/{PLUGIN_CODE}/enable", headers=admin)

    response = client.get(f"/api/v1/plugin/{PLUGIN_CODE}/unknown", headers=admin)
    assert response.status_code == 404
    assert response.get_json()["code"] == NOT_FOUND


# ── 008 / 007：卸载与迁移幂等 ──────────────────────────────────────────────


def test_plugin_uninstall_requires_disable(tmp_path: Path) -> None:
    config, _ = _setup(tmp_path)
    client = _client(config)
    admin = _login(client, "root")
    url = f"/api/v1/system/plugins/{PLUGIN_CODE}"

    client.post(f"{url}/install", headers=admin)
    client.post(f"{url}/enable", headers=admin)

    blocked = client.delete(url, headers=admin)
    assert blocked.status_code == 409
    assert blocked.get_json()["code"] == PLUGIN_STATE_INVALID

    client.post(f"{url}/disable", headers=admin)
    removed = client.delete(url, headers=admin)
    assert removed.status_code == 200, removed.get_json()
    assert removed.get_json()["data"] == {"code": PLUGIN_CODE, "uninstalled": True}

    assert _registry(config, PLUGIN_CODE) is None
    assert f"plg_{PLUGIN_CODE}_note" not in _tables(config)
    assert "PLUGIN_UNINSTALL" in _events(config)


def test_plugin_install_is_idempotent_for_migrations(tmp_path: Path) -> None:
    config, _ = _setup(tmp_path)
    client = _client(config)
    admin = _login(client, "root")
    url = f"/api/v1/system/plugins/{PLUGIN_CODE}"

    first = client.post(f"{url}/install", headers=admin)
    assert first.status_code == 200, first.get_json()
    second = client.post(f"{url}/install", headers=admin)
    assert second.status_code == 200, second.get_json()
    assert second.get_json()["data"]["schema_rev"] == first.get_json()["data"]["schema_rev"]

    engine = _engine(config)
    try:
        with Session(engine) as session:
            assert len(session.execute(select(PluginRegistry)).scalars().all()) == 1
    finally:
        engine.dispose()
    assert f"plg_{PLUGIN_CODE}_note" in _tables(config)

    # 无 migrations 目录的插件：schema_rev 为空，可正常安装/卸载
    plain = client.post(f"/api/v1/system/plugins/{OTHER_CODE}/install", headers=admin)
    assert plain.status_code == 200, plain.get_json()
    assert plain.get_json()["data"]["schema_rev"] is None
    assert client.delete(f"/api/v1/system/plugins/{OTHER_CODE}", headers=admin).status_code == 200


def test_plugin_uninstall_with_missing_dir_is_rejected(tmp_path: Path) -> None:
    config, dirs = _setup(tmp_path)
    client = _client(config)
    admin = _login(client, "root")
    url = f"/api/v1/system/plugins/{PLUGIN_CODE}"

    client.post(f"{url}/install", headers=admin)
    shutil.rmtree(dirs[PLUGIN_CODE])

    rejected = client.delete(url, headers=admin)
    assert rejected.status_code == 409
    assert rejected.get_json()["code"] == PLUGIN_MIGRATION_INVALID
    assert _registry(config, PLUGIN_CODE) is not None


def test_plugin_reinstall_keeps_enabled_state_and_remounts(tmp_path: Path) -> None:
    config, _ = _setup(tmp_path)
    client = _client(config)
    admin = _login(client, "root")
    url = f"/api/v1/system/plugins/{PLUGIN_CODE}"

    client.post(f"{url}/install", headers=admin)
    client.post(f"{url}/enable", headers=admin)
    again = client.post(f"{url}/install", headers=admin)
    assert again.status_code == 200, again.get_json()
    assert again.get_json()["data"]["state"] == "enabled"
    assert client.get(f"/api/v1/plugin/{PLUGIN_CODE}/hello", headers=admin).status_code == 200


# ── 009：鉴权 ──────────────────────────────────────────────────────────────


def test_plugin_management_requires_permission(tmp_path: Path) -> None:
    config, _ = _setup(tmp_path)
    client = _client(config)
    student = _login(client, "student")

    anonymous = client.get("/api/v1/system/plugins")
    assert anonymous.status_code == 401
    assert anonymous.get_json()["code"] == AUTH_TOKEN_EXPIRED

    denied = client.get("/api/v1/system/plugins", headers=student)
    assert denied.status_code == 403
    assert denied.get_json()["code"] == PERMISSION_DENIED

    install_denied = client.post(f"/api/v1/system/plugins/{PLUGIN_CODE}/install", headers=student)
    assert install_denied.status_code == 403

    # 分发入口同样要求登录态
    assert client.get(f"/api/v1/plugin/{PLUGIN_CODE}/hello").status_code == 401


def test_plugin_list_reports_registry(tmp_path: Path) -> None:
    config, _ = _setup(tmp_path)
    client = _client(config)
    admin = _login(client, "root")
    client.post(f"/api/v1/system/plugins/{PLUGIN_CODE}/install", headers=admin)

    listed = client.get("/api/v1/system/plugins", headers=admin)
    assert listed.status_code == 200, listed.get_json()
    data = listed.get_json()["data"]
    assert data["total"] == 1
    assert data["items"][0]["code"] == PLUGIN_CODE
    assert data["items"][0]["state"] == "disabled"
    assert data["plugin_dir"].endswith("plugins")


# ── 010：重启自动挂载与篡改检测 ────────────────────────────────────────────


def test_plugin_boot_mounts_enabled_plugin(tmp_path: Path) -> None:
    config, _ = _setup(tmp_path)
    first = _client(config)
    admin = _login(first, "root")
    first.post(f"/api/v1/system/plugins/{PLUGIN_CODE}/install", headers=admin)
    first.post(f"/api/v1/system/plugins/{PLUGIN_CODE}/enable", headers=admin)

    # 新进程装配：boot() 依据 plugin_registry 自动挂载，无需再次 enable
    second = _client(config)
    admin2 = _login(second, "root")
    served = second.get(f"/api/v1/plugin/{PLUGIN_CODE}/hello", headers=admin2)
    assert served.status_code == 200, served.get_json()


def test_plugin_boot_marks_tampered_manifest_failed(tmp_path: Path) -> None:
    config, dirs = _setup(tmp_path)
    first = _client(config)
    admin = _login(first, "root")
    first.post(f"/api/v1/system/plugins/{PLUGIN_CODE}/install", headers=admin)
    first.post(f"/api/v1/system/plugins/{PLUGIN_CODE}/enable", headers=admin)

    manifest_path = dirs[PLUGIN_CODE] / "manifest.json"
    payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    payload["name"] = "被篡改的插件"
    manifest_path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")

    second = _client(config)
    admin2 = _login(second, "root")

    row = _registry(config, PLUGIN_CODE)
    assert row is not None
    assert row.state == "failed"
    assert row.error_message
    assert "PLUGIN_LOAD_FAILED" in _events(config)
    assert second.get(f"/api/v1/plugin/{PLUGIN_CODE}/hello", headers=admin2).status_code == 404


def test_plugin_enable_load_failure_marks_failed(tmp_path: Path) -> None:
    config, dirs = _setup(tmp_path)
    client = _client(config)
    admin = _login(client, "root")
    url = f"/api/v1/system/plugins/{PLUGIN_CODE}"

    client.post(f"{url}/install", headers=admin)
    # 入口在安装后损坏：enable 失败返回 PLUGIN_LOAD_FAILED，注册表落 failed，内核继续可用
    (dirs[PLUGIN_CODE] / "server.py").write_text("HANDLERS = {}\n", encoding="utf-8")

    failed = client.post(f"{url}/enable", headers=admin)
    assert failed.status_code == 500
    assert failed.get_json()["code"] == PLUGIN_LOAD_FAILED
    row = _registry(config, PLUGIN_CODE)
    assert row is not None and row.state == "failed"
    # 内核其它接口不受影响
    assert client.get("/healthz").status_code == 200


# ── 003 / 011：manifest 拒装 ───────────────────────────────────────────────


@pytest.mark.parametrize(
    ("payload", "code"),
    [
        ({"version": ""}, PLUGIN_MANIFEST_INVALID),
        ({"code": "bad-code"}, PLUGIN_MANIFEST_INVALID),
        ({"core_api_min": "9.9.9"}, PLUGIN_MANIFEST_INVALID),
        ({"entry_server": "../escape.py"}, PLUGIN_MANIFEST_INVALID),
        ({"supported_modes": ["cloud"]}, PLUGIN_MANIFEST_INVALID),
        ({"capabilities": ["direct_write"]}, PLUGIN_CAP_DENIED),
        ({"capabilities_v2": {"db_ddl": True}}, PLUGIN_CAP_DENIED),
        ({"capabilities_v2": {"raw_sql": False}}, PLUGIN_MANIFEST_INVALID),
    ],
)
def test_plugin_manifest_rejected(tmp_path: Path, payload: dict[str, Any], code: str) -> None:
    plugin_root = tmp_path / "plugins"
    plugin_root.mkdir()
    manifest = _manifest(PLUGIN_CODE)
    manifest.update(payload)
    _write_plugin(plugin_root, PLUGIN_CODE, manifest=manifest)
    config = _config(tmp_path, plugin_root)
    _seed(config)
    client = _client(config)
    admin = _login(client, "root")

    response = client.post(f"/api/v1/system/plugins/{PLUGIN_CODE}/install", headers=admin)
    assert response.status_code == (403 if code == PLUGIN_CAP_DENIED else 422), response.get_json()
    assert response.get_json()["code"] == code
    assert _registry(config, PLUGIN_CODE) is None


def test_plugin_manifest_banned_token_rejected(tmp_path: Path) -> None:
    plugin_root = tmp_path / "plugins"
    plugin_root.mkdir()
    manifest = _manifest(PLUGIN_CODE, description="INSERT ... ON CONFLICT DO NOTHING")
    _write_plugin(plugin_root, PLUGIN_CODE, manifest=manifest)
    config = _config(tmp_path, plugin_root)
    _seed(config)
    client = _client(config)
    admin = _login(client, "root")

    response = client.post(f"/api/v1/system/plugins/{PLUGIN_CODE}/install", headers=admin)
    assert response.status_code == 422
    assert response.get_json()["code"] == PLUGIN_MANIFEST_INVALID


def test_plugin_manifest_code_must_match_dir(tmp_path: Path) -> None:
    plugin_root = tmp_path / "plugins"
    plugin_root.mkdir()
    _write_plugin(plugin_root, PLUGIN_CODE, manifest=_manifest(OTHER_CODE))
    config = _config(tmp_path, plugin_root)
    _seed(config)
    client = _client(config)
    admin = _login(client, "root")

    response = client.post(f"/api/v1/system/plugins/{PLUGIN_CODE}/install", headers=admin)
    assert response.status_code == 422
    assert response.get_json()["code"] == PLUGIN_MANIFEST_INVALID


def test_plugin_install_missing_dir_is_404(tmp_path: Path) -> None:
    config, _ = _setup(tmp_path)
    client = _client(config)
    admin = _login(client, "root")
    response = client.post("/api/v1/system/plugins/ghost/install", headers=admin)
    assert response.status_code == 404
    assert response.get_json()["code"] == PLUGIN_NOT_FOUND


# ── 004 / 005 / 006 / 012：迁移链强校验 ────────────────────────────────────


def test_plugin_migration_table_prefix_enforced(tmp_path: Path) -> None:
    plugin_root = tmp_path / "plugins"
    plugin_root.mkdir()
    _write_plugin(
        plugin_root,
        PLUGIN_CODE,
        migrations={"0001_init.py": _migration_source(PLUGIN_CODE, table="demo_note")},
    )
    config = _config(tmp_path, plugin_root)
    _seed(config)
    client = _client(config)
    admin = _login(client, "root")

    response = client.post(f"/api/v1/system/plugins/{PLUGIN_CODE}/install", headers=admin)
    assert response.status_code == 409
    assert response.get_json()["code"] == PLUGIN_TABLE_PREFIX_INVALID
    assert "demo_note" not in _tables(config)
    assert _registry(config, PLUGIN_CODE) is None


def test_plugin_manifest_table_prefix_enforced(tmp_path: Path) -> None:
    plugin_root = tmp_path / "plugins"
    plugin_root.mkdir()
    _write_plugin(
        plugin_root,
        PLUGIN_CODE,
        manifest=_manifest(PLUGIN_CODE, table_prefix="plg_other_"),
        migrations={"0001_init.py": _migration_source(PLUGIN_CODE)},
    )
    config = _config(tmp_path, plugin_root)
    _seed(config)
    client = _client(config)
    admin = _login(client, "root")

    response = client.post(f"/api/v1/system/plugins/{PLUGIN_CODE}/install", headers=admin)
    assert response.status_code == 409
    assert response.get_json()["code"] == PLUGIN_TABLE_PREFIX_INVALID


def test_plugin_migration_requires_delete_seq(tmp_path: Path) -> None:
    plugin_root = tmp_path / "plugins"
    plugin_root.mkdir()
    _write_plugin(
        plugin_root,
        PLUGIN_CODE,
        migrations={"0001_init.py": _migration_source(PLUGIN_CODE, delete_seq=False)},
    )
    config = _config(tmp_path, plugin_root)
    _seed(config)
    client = _client(config)
    admin = _login(client, "root")

    response = client.post(f"/api/v1/system/plugins/{PLUGIN_CODE}/install", headers=admin)
    assert response.status_code == 409
    assert response.get_json()["code"] == PLUGIN_MIGRATION_INVALID
    assert f"plg_{PLUGIN_CODE}_note" not in _tables(config)
    assert _registry(config, PLUGIN_CODE) is None


def test_plugin_migration_banned_sql_rejected(tmp_path: Path) -> None:
    plugin_root = tmp_path / "plugins"
    plugin_root.mkdir()
    source = (
        _migration_source(PLUGIN_CODE)
        + '\n\ndef seed(op):\n    op.execute("INSERT ... ON CONFLICT DO NOTHING")\n'
    )
    _write_plugin(plugin_root, PLUGIN_CODE, migrations={"0001_init.py": source})
    config = _config(tmp_path, plugin_root)
    _seed(config)
    client = _client(config)
    admin = _login(client, "root")

    response = client.post(f"/api/v1/system/plugins/{PLUGIN_CODE}/install", headers=admin)
    assert response.status_code == 409
    assert response.get_json()["code"] == PLUGIN_MIGRATION_INVALID


@pytest.mark.parametrize(
    "migrations",
    [
        # 双 head
        {
            "0001_a.py": _migration_source(PLUGIN_CODE, table="plg_demo_a"),
            "0002_b.py": _migration_source(PLUGIN_CODE, table="plg_demo_b"),
        },
        # 悬空 down_revision
        {
            "0001_a.py": _migration_source(
                PLUGIN_CODE, table="plg_demo_a", down_revision="demo_9999"
            )
        },
    ],
)
def test_plugin_migration_chain_invalid(tmp_path: Path, migrations: dict[str, str]) -> None:
    plugin_root = tmp_path / "plugins"
    plugin_root.mkdir()
    _write_plugin(plugin_root, PLUGIN_CODE, migrations=migrations)
    config = _config(tmp_path, plugin_root)
    _seed(config)
    client = _client(config)
    admin = _login(client, "root")

    response = client.post(f"/api/v1/system/plugins/{PLUGIN_CODE}/install", headers=admin)
    assert response.status_code == 409
    assert response.get_json()["code"] == PLUGIN_MIGRATION_INVALID


# ── manifest 单元口径（不依赖 Flask）────────────────────────────────────────


def test_manifest_requires_all_fields() -> None:
    with pytest.raises(AppError) as exc:
        validate_manifest({"code": PLUGIN_CODE})
    assert exc.value.code == PLUGIN_MANIFEST_INVALID
    assert "缺少必填字段" in str(exc.value.data)


def test_manifest_entry_file_must_exist(tmp_path: Path) -> None:
    plugin_dir = tmp_path / PLUGIN_CODE
    plugin_dir.mkdir()
    (plugin_dir / "manifest.json").write_text(
        json.dumps(_manifest(PLUGIN_CODE, entry_server="missing.py"), ensure_ascii=False),
        encoding="utf-8",
    )
    with pytest.raises(AppError) as exc:
        load_manifest(plugin_dir)
    assert exc.value.code == PLUGIN_MANIFEST_INVALID


def test_manifest_capabilities_v2_snapshot_is_false_only() -> None:
    manifest = validate_manifest(
        _manifest(PLUGIN_CODE, capabilities_v2={"progress_direct_write": False, "db_ddl": False})
    )
    assert dict(manifest.capabilities_v2) == {"db_ddl": False, "progress_direct_write": False}
    assert manifest.table_prefix == f"plg_{PLUGIN_CODE}_"
