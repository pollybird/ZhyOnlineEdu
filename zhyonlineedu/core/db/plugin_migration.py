"""插件独立迁移链（§5.6 / §11 · M3.3）。

口径：
- 插件 DDL **不进**内核 `alembic_version`，每插件一条链，当前 revision 落
  `plugin_registry.schema_rev`（独立迁移链，互不阻塞）；
- 表名强校验：插件新建的每张表必须带 `plg_<code>_` 前缀，否则
  `PLUGIN_TABLE_PREFIX_INVALID`；
- 软删强校验：插件表必须含 `delete_seq` 列（D19a），缺失即
  `PLUGIN_MIGRATION_INVALID`，并在事务内回滚/尽力降级，杜绝半成品表；
- 迁移源文本复用 `core.db.sql_guard` 黑名单（与 CI 同一份），命中即拒装；
- 加载插件 DDL 时注入 batch 选项（SQLite / 国产库走重建表语义），国产库红线不变。
"""

from __future__ import annotations

import importlib.util
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from alembic.operations import Operations
from alembic.runtime.migration import MigrationContext
from sqlalchemy import Engine, inspect

from zhyonlineedu.core.codes import PLUGIN_MIGRATION_INVALID, PLUGIN_TABLE_PREFIX_INVALID
from zhyonlineedu.core.db import compat
from zhyonlineedu.core.db.sql_guard import banned_lines
from zhyonlineedu.core.errors import AppError

MIGRATIONS_DIR = "migrations"
SOFT_DELETE_COLUMN = "delete_seq"
_FUNC_NAMES = ("upgrade", "downgrade")


@dataclass(frozen=True, slots=True)
class PluginMigration:
    revision: str
    down_revision: str | None
    upgrade: Callable[[Operations], None]
    downgrade: Callable[[Operations], None]
    path: Path


def _invalid(errors: list[str]) -> AppError:
    """迁移链/表结构校验失败（统一信封码）。"""
    return AppError(PLUGIN_MIGRATION_INVALID, data={"errors": errors})


def _load_module(path: Path, code: str) -> Any:
    name = f"zhy_plugin_mig_{code}_{path.stem}"
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:  # pragma: no cover - 解释器异常
        raise _invalid([f"{path.name}: 无法加载迁移模块"])
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def load_chain(plugin_dir: Path, code: str) -> list[PluginMigration]:
    """读取 `<plugin_dir>/migrations/*.py`，校验单链并返回 base → head 顺序。"""
    migrations_dir = plugin_dir / MIGRATIONS_DIR
    if not migrations_dir.exists():
        return []
    files = sorted(
        path for path in migrations_dir.glob("*.py") if not path.name.startswith(("_", "."))
    )
    modules: list[PluginMigration] = []
    for path in files:
        source = path.read_text(encoding="utf-8")
        hits = banned_lines(source)
        if hits:
            raise _invalid([f"{path.name} 含方言裸写令牌（第 {hits[0][0]} 行），插件禁止裸 SQL"])
        module = _load_module(path, code)
        revision = getattr(module, "revision", None)
        down_revision = getattr(module, "down_revision", None)
        if not isinstance(revision, str) or not revision.strip():
            raise _invalid([f"{path.name}: 缺少 revision"])
        if down_revision is not None and not isinstance(down_revision, str):
            raise _invalid([f"{path.name}: down_revision 必须是字符串或 null"])
        for func_name in _FUNC_NAMES:
            if not callable(getattr(module, func_name, None)):
                raise _invalid([f"{path.name}: 缺少 {func_name}()"])
        modules.append(
            PluginMigration(
                revision=revision.strip(),
                down_revision=down_revision.strip() if isinstance(down_revision, str) else None,
                upgrade=module.upgrade,
                downgrade=module.downgrade,
                path=path,
            )
        )

    if not modules:
        return []
    by_revision = {item.revision: item for item in modules}
    if len(by_revision) != len(modules):
        raise _invalid(["插件迁移链存在重复 revision"])
    for item in modules:
        if item.down_revision is not None and item.down_revision not in by_revision:
            raise _invalid([f"{item.path.name}: down_revision={item.down_revision} 不存在"])
    heads = [item for item in modules if item.revision not in {m.down_revision for m in modules}]
    if len(heads) != 1:
        raise _invalid([f"插件迁移链必须单头，当前 head={[h.revision for h in heads]}"])

    ordered: list[PluginMigration] = []
    current: PluginMigration | None = heads[0]
    while current is not None:
        ordered.append(current)
        current = by_revision[current.down_revision] if current.down_revision is not None else None
    ordered.reverse()
    return ordered


def head_revision(chain: list[PluginMigration]) -> str | None:
    return chain[-1].revision if chain else None


def _applied_index(chain: list[PluginMigration], current: str | None) -> int:
    """当前 revision 在链中的位置；未安装为 -1，链失配即拒绝。"""
    if current is None:
        return -1
    for index, item in enumerate(chain):
        if item.revision == current:
            return index
    raise _invalid([f"已记录的插件 revision={current} 不在当前迁移链中"])


def _table_names(engine_or_conn: Any) -> set[str]:
    return set(inspect(engine_or_conn).get_table_names())


def _needs_batch(engine: Engine) -> bool:
    try:
        flavor = compat.flavor()
    except compat.CompatError:  # 未装配兼容层（纯脚本/测试）时按 DSN 推断
        flavor = compat.detect_flavor(str(engine.url))
    return compat.flavor_needs_batch(flavor)


@contextmanager
def _operations(conn: Any, *, needs_batch: bool) -> Iterator[Operations]:
    options: dict[str, Any] = {
        "compare_type": False,
        "include_schemas": False,
        "render_as_batch": needs_batch,
    }
    with Operations.context(MigrationContext.configure(conn, opts=options)) as op:
        yield op


def _run(conn: Any, module: PluginMigration, direction: str, *, needs_batch: bool) -> None:
    with _operations(conn, needs_batch=needs_batch) as op:
        getattr(module, direction)(op)


def _assert_plugin_tables(conn: Any, created: set[str], prefix: str) -> None:
    """强校验：新建表必须带插件前缀且含 delete_seq（事务内执行）。"""
    bad_prefix = sorted(name for name in created if not name.startswith(prefix))
    if bad_prefix:
        raise AppError(
            PLUGIN_TABLE_PREFIX_INVALID,
            data={"tables": bad_prefix, "expected_prefix": prefix},
        )
    inspector = inspect(conn)
    existing = _table_names(conn)
    missing = sorted(
        name
        for name in sorted(created & existing)
        if SOFT_DELETE_COLUMN not in {column["name"] for column in inspector.get_columns(name)}
    )
    if missing:
        raise _invalid([f"插件表必须含 {SOFT_DELETE_COLUMN}（软删 D19a）：" + ", ".join(missing)])


def upgrade(
    engine: Engine,
    plugin_dir: Path,
    code: str,
    *,
    current: str | None,
) -> str | None:
    """执行插件迁移链到 head，返回新 revision（无迁移时返回 current）。"""
    chain = load_chain(plugin_dir, code)
    applied = _applied_index(chain, current)
    pending = chain[applied + 1 :]
    if not pending:
        return current
    prefix = f"plg_{code}_"
    needs_batch = _needs_batch(engine)
    before = _table_names(engine)
    try:
        with engine.begin() as conn:
            for module in pending:
                _run(conn, module, "upgrade", needs_batch=needs_batch)
            _assert_plugin_tables(conn, _table_names(conn) - before, prefix)
    except Exception:
        # 尽力回收：SQLite/PG 事务已回滚；MySQL DDL 不可事务，按链倒序降级
        _best_effort_downgrade(engine, pending, code, needs_batch=needs_batch)
        raise
    return head_revision(chain)


def downgrade(
    engine: Engine,
    plugin_dir: Path,
    code: str,
    *,
    current: str | None,
    target: str | None = None,
) -> str | None:
    """回退插件迁移链（默认回到 target=None 即 base），返回落库后的 revision。"""
    chain = load_chain(plugin_dir, code)
    applied = _applied_index(chain, current)
    target_index = _applied_index(chain, target) if target is not None else -1
    if target_index >= applied:
        return current
    needs_batch = _needs_batch(engine)
    with engine.begin() as conn:
        for module in reversed(chain[target_index + 1 : applied + 1]):
            _run(conn, module, "downgrade", needs_batch=needs_batch)
    return chain[target_index].revision if target_index >= 0 else None


def _best_effort_downgrade(
    engine: Engine, modules: list[PluginMigration], code: str, *, needs_batch: bool
) -> None:
    try:
        with engine.begin() as conn:
            for module in reversed(modules):
                _run(conn, module, "downgrade", needs_batch=needs_batch)
    except Exception:  # pragma: no cover - 回收失败不掩盖首因异常
        import logging

        logging.getLogger(__name__).warning("plugin %s 迁移回收失败", code, exc_info=True)


def discover_plugin_dirs(root: Path) -> list[Path]:
    """列出插件根目录下含 manifest 的子目录（安装/列表用；空目录返回空列表）。"""
    if not root.exists():
        return []
    return [
        entry
        for entry in sorted(root.iterdir(), key=lambda item: item.name)
        if entry.is_dir() and (entry / "manifest.json").is_file()
    ]
