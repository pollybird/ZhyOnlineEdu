"""M1.3 Alembic 能力感知迁移（§5.6 / D34 / D19a）。

覆盖：
- SQLite 实跑 upgrade head + downgrade base；
- PG / MySQL 由 DSN 环境变量门控实跑；
- 国产 flavor COMPILE_ONLY（离线生成 SQL + batch 选项断言）；
- `x-zhy-cap` 头声明、四种 flavor 的 batch 开关、直接 ALTER TYPE 守卫；
- 软删复合 UK 注入与 fail-fast 断言。
"""

from __future__ import annotations

import os
from collections.abc import Iterator
from pathlib import Path

import manage
import pytest
import sqlalchemy as sa
from alembic import command
from alembic.config import Config as AlembicConfig
from alembic.operations import ops as alembic_ops
from sqlalchemy import Engine, Integer, MetaData, String, UniqueConstraint, inspect
from sqlalchemy.orm import Mapped, mapped_column
from zhyonlineedu.core.db import compat
from zhyonlineedu.core.db.base import Base
from zhyonlineedu.core.db.engine import create_db_engine
from zhyonlineedu.core.db.migration import (
    MigrationCapError,
    assert_four_step_chain,
    context_options,
    guard_revision_directives,
    parse_caps_header,
    validate_revision_file,
)
from zhyonlineedu.core.db.types import SoftDeleteMixin

ROOT = Path(__file__).resolve().parent.parent
ALEMBIC_INI = ROOT / "alembic.ini"
VERSIONS = ROOT / "migrations" / "versions"
SYSTEM_TABLES = {
    "sys_install_state",
    "sys_config",
    "sys_mode_config",
    "sys_compat_flags",
    "sys_mode_state",
}
AUTH_TABLES = {
    "account_user",
    "account_session",
    "sys_role",
    "sys_permission",
    "sys_user_role",
    "sys_role_permission",
    "sys_log_security",
}
CATALOG_TABLES = {
    "edu_subject",
    "edu_course",
    "edu_course_chapter",
    "edu_course_lesson",
    "media_asset",
}
UPLOAD_TABLES = {"media_upload_session", "media_chunk"}
TRANSCODE_TABLES = {"media_transcode_job"}
LEARN_TABLES = {
    "learn_progress_log",
    "learn_progress",
    "learn_progress_gate",
    "learn_course_progress",
}
KEYSESSION_TABLES = {"media_keysession"}
QUIZ_TABLES = {"quiz_question", "quiz_attempt"}
USERS_TABLES = {"edu_class", "edu_class_member", "sys_import_batch"}
COMMERCE_TABLES = {"member_level", "pay_order", "member_subscription", "pay_event"}
PLUGIN_TABLES = {"plugin_registry"}
ALL_TABLES = (
    SYSTEM_TABLES
    | AUTH_TABLES
    | CATALOG_TABLES
    | UPLOAD_TABLES
    | TRANSCODE_TABLES
    | LEARN_TABLES
    | KEYSESSION_TABLES
    | QUIZ_TABLES
    | USERS_TABLES
    | COMMERCE_TABLES
    | PLUGIN_TABLES
)
HEAD_REV = "0015_plugin_web_entry"
DSN_ENV = {"postgresql": "ZHY_TEST_PG_DSN", "mysql": "ZHY_TEST_MYSQL_DSN"}


@pytest.fixture(autouse=True)
def _cleanup() -> Iterator[None]:
    compat.reset()
    yield
    compat.reset()


def _alembic_cfg(url: str, flavor: str) -> AlembicConfig:
    cfg = AlembicConfig(str(ALEMBIC_INI))
    cfg.set_main_option("sqlalchemy.url", url)
    cfg.set_main_option("zhy.flavor", flavor)
    return cfg


def _drop_system_tables(engine: Engine) -> None:
    existing = set(inspect(engine).get_table_names())
    for name in (*ALL_TABLES, "alembic_version"):
        if name not in existing:
            continue
        with engine.begin() as conn:
            sa.Table(name, MetaData(), autoload_with=conn).drop(conn)


# ── 实跑：SQLite + PG/MySQL（DSN 门控）──────────────────────────────────────


def test_sqlite_upgrade_downgrade(tmp_path: Path) -> None:
    url = f"sqlite:///{tmp_path / 'migrate.db'}"
    cfg = _alembic_cfg(url, "sqlite")
    engine = create_db_engine(url, "sqlite")
    try:
        command.upgrade(cfg, "head")
        tables = set(inspect(engine).get_table_names())
        assert tables >= ALL_TABLES
        # 模型与迁移不得漂移（列名逐一比对，D37）
        inspector = inspect(engine)
        for table in Base.metadata.tables.values():
            actual = {column["name"] for column in inspector.get_columns(table.name)}
            assert actual == {column.name for column in table.columns}, table.name
        with engine.connect() as conn:
            row = conn.execute(
                sa.text("SELECT schema_rev, last_good_rev FROM sys_install_state WHERE id = 1")
            ).one()
        assert tuple(row) == (HEAD_REV, HEAD_REV)

        command.downgrade(cfg, "base")
        remaining = set(inspect(engine).get_table_names())
        assert ALL_TABLES.isdisjoint(remaining)
    finally:
        engine.dispose()


@pytest.mark.parametrize(("flavor", "env"), sorted(DSN_ENV.items()))
def test_dsn_upgrade_downgrade(flavor: str, env: str) -> None:
    dsn = os.environ.get(env, "").strip()
    if not dsn:
        pytest.skip(f"未设置 {env}，跳过 {flavor} 运行期迁移用例")
    cfg = _alembic_cfg(dsn, flavor)
    engine = create_db_engine(dsn, flavor)
    try:
        _drop_system_tables(engine)
        command.upgrade(cfg, "head")
        assert set(inspect(engine).get_table_names()) >= ALL_TABLES
        command.downgrade(cfg, "base")
        assert ALL_TABLES.isdisjoint(set(inspect(engine).get_table_names()))
    finally:
        _drop_system_tables(engine)
        engine.dispose()


# ── ci:domestic：COMPILE_ONLY（介质到位后替换为实跑）──────────────────────────


@pytest.mark.parametrize("flavor", ["opengauss_5", "opengauss_6", "opengauss_7", "kingbase"])
def test_domestic_compile_only(flavor: str, capsys: pytest.CaptureFixture[str]) -> None:
    cfg = _alembic_cfg("postgresql://zhy@127.0.0.1:1/zhy", flavor)
    assert context_options(flavor)["render_as_batch"] is True
    command.upgrade(cfg, "head", sql=True)
    out = capsys.readouterr().out
    assert "CREATE TABLE sys_install_state" in out
    assert "CREATE TABLE sys_compat_flags" in out
    # 首个 revision 无类型变更；生成 SQL 不得出现直接类型变更
    assert "ALTER COLUMN" not in out.upper()


# ── revision 能力声明与守卫 ────────────────────────────────────────────────


def test_revisions_declare_caps_and_four_step() -> None:
    files = sorted(VERSIONS.glob("*.py"))
    assert files
    for path in files:
        caps = parse_caps_header(path.read_text(encoding="utf-8"))
        assert caps["returning"] in {"required", "optional", "none"}
        assert caps["alter_type_online"] in {"true", "false"}
        validate_revision_file(path)


def test_missing_cap_header_rejected(tmp_path: Path) -> None:
    path = tmp_path / "bad.py"
    path.write_text("revision = 'x'\n", encoding="utf-8")
    with pytest.raises(MigrationCapError):
        validate_revision_file(path)


def test_direct_type_change_requires_batch(tmp_path: Path) -> None:
    path = tmp_path / "bad_type.py"
    path.write_text(
        "# x-zhy-cap: returning:optional, alter_type_online:false\n"
        "def upgrade():\n"
        "    op.alter_column('t', 'c', type_=sa.String())\n",
        encoding="utf-8",
    )
    with pytest.raises(MigrationCapError):
        validate_revision_file(path)


def test_type_change_declared_but_not_batched_rejected(tmp_path: Path) -> None:
    path = tmp_path / "declared_only.py"
    path.write_text(
        "# x-zhy-cap: returning:optional, alter_type_online:true\n"
        "def upgrade():\n"
        "    op.alter_column('t', 'c', type_=sa.String())\n",
        encoding="utf-8",
    )
    with pytest.raises(MigrationCapError):
        validate_revision_file(path)


def test_type_change_in_batch_with_declaration_passes(tmp_path: Path) -> None:
    path = tmp_path / "ok_type.py"
    path.write_text(
        "# x-zhy-cap: returning:optional, alter_type_online:true\n"
        "def upgrade():\n"
        "    with op.batch_alter_table('t') as batch_op:\n"
        "        batch_op.alter_column('c', type_=sa.String())\n",
        encoding="utf-8",
    )
    validate_revision_file(path)


@pytest.mark.parametrize(
    ("flavor", "needs_batch"),
    [
        ("sqlite", True),
        ("mysql", False),
        ("postgresql", False),
        ("opengauss_5", True),
        ("opengauss_6", True),
        ("opengauss_7", True),
        ("kingbase", True),
    ],
)
def test_context_options_by_flavor(flavor: str, needs_batch: bool) -> None:
    options = context_options(flavor)
    assert options["render_as_batch"] is needs_batch
    assert options["compare_type"] is True


def _script_with_direct_type_change() -> alembic_ops.MigrationScript:
    alter = alembic_ops.AlterColumnOp("t_demo", "c", modify_type=String(10))
    upgrade = alembic_ops.UpgradeOps([alembic_ops.ModifyTableOps("t_demo", [alter])])
    return alembic_ops.MigrationScript("rev", upgrade, alembic_ops.DowngradeOps([]))


@pytest.mark.parametrize(
    "flavor", ["opengauss_5", "opengauss_6", "opengauss_7", "kingbase", "sqlite"]
)
def test_guard_blocks_direct_type_change(flavor: str) -> None:
    with pytest.raises(MigrationCapError):
        guard_revision_directives([_script_with_direct_type_change()], flavor)


@pytest.mark.parametrize("flavor", ["postgresql", "mysql"])
def test_guard_allows_online_type_change(flavor: str) -> None:
    guard_revision_directives([_script_with_direct_type_change()], flavor)


def test_four_step_chain_rejects_online_revision_for_domestic(tmp_path: Path) -> None:
    (tmp_path / "0001_demo.py").write_text(
        "# x-zhy-cap: returning:optional, alter_type_online:true\n", encoding="utf-8"
    )
    for flavor in ("opengauss_5", "opengauss_6", "opengauss_7", "kingbase"):
        with pytest.raises(MigrationCapError):
            assert_four_step_chain(tmp_path, flavor)
    for flavor in ("sqlite", "postgresql", "mysql"):
        assert_four_step_chain(tmp_path, flavor)


def test_four_step_chain_accepts_current_revisions() -> None:
    for flavor in ("opengauss_5", "opengauss_6", "opengauss_7", "kingbase"):
        assert_four_step_chain(VERSIONS, flavor)


# ── 软删复合 UK 注入（D19a / F6）───────────────────────────────────────────


class _TestBase(Base):
    __abstract__ = True

    metadata = MetaData()


class _SoftDemo(SoftDeleteMixin, _TestBase):
    __tablename__ = "t_soft_demo"
    __soft_delete_uq__ = ("org_id", "user_no")

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    org_id: Mapped[int] = mapped_column(Integer, nullable=False)
    user_no: Mapped[str] = mapped_column(String(32), nullable=False)


def test_soft_delete_uk_injected() -> None:
    table = _SoftDemo.__table__
    uniques = {
        tuple(column.name for column in constraint.columns)
        for constraint in table.constraints
        if isinstance(constraint, UniqueConstraint)
    }
    assert ("org_id", "user_no", "delete_seq") in uniques
    # 断言工具对合法 metadata 放行
    compat.ensure_soft_delete_uk(_TestBase.metadata)


def test_soft_delete_uk_missing_fails_fast() -> None:
    class _BadBase(Base):
        __abstract__ = True

        metadata = MetaData()

    class _BadRow(_BadBase):
        __tablename__ = "t_soft_bad"

        id: Mapped[int] = mapped_column(Integer, primary_key=True)
        delete_seq: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    assert _BadRow.__table__.name == "t_soft_bad"
    with pytest.raises(compat.SoftDeleteUKError):
        compat.ensure_soft_delete_uk(_BadBase.metadata)


# ── manage.py：last_good_rev 记录与回滚前置条件 ──────────────────────────────


def test_manage_migrate_records_state(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    url = f"sqlite:///{tmp_path / 'manage.db'}"
    monkeypatch.setenv("ZHY_DB_URL", url)
    monkeypatch.setenv("ZHY_ENV", "dev")
    assert manage.migrate() == 0
    engine = create_db_engine(url, "sqlite")
    try:
        assert manage.current_revision(engine) == HEAD_REV
        assert manage.read_last_good_rev(engine) == HEAD_REV
        # 已在 last_good_rev：回滚为空操作
        assert manage.migrate(rollback=True) == 0
    finally:
        engine.dispose()


def test_manage_rollback_without_state_refuses(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    url = f"sqlite:///{tmp_path / 'empty.db'}"
    monkeypatch.setenv("ZHY_DB_URL", url)
    monkeypatch.setenv("ZHY_ENV", "dev")
    assert manage.migrate(rollback=True) == 2
