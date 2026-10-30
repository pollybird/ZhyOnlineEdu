"""能力感知迁移工具（§5.6 / D34 / F6）。

三件事：
1. `x-zhy-cap` 头声明解析与校验（每个 revision 必填）；
2. 按 flavor 生成 Alembic `context.configure` 选项：`render_as_batch` 与
   `rendered_item_type_requires_batch`（Alembic ≥1.20 支持，1.16 用 render_as_batch 等价）；
3. 守卫：需要四步模板的 flavor 禁止自动生成"直接 ALTER COLUMN TYPE"。
"""

from __future__ import annotations

import inspect
from collections.abc import Iterable
from pathlib import Path
from typing import Any

from alembic import command
from alembic.config import Config as AlembicConfig
from alembic.operations import ops as alembic_ops
from alembic.operations.base import BatchOperations
from alembic.runtime.environment import EnvironmentContext
from alembic.runtime.migration import MigrationContext
from sqlalchemy import Engine

from .compat import Flavor, flavor_needs_batch

CAP_HEADER_PREFIX = "# x-zhy-cap:"
REQUIRED_CAP_KEYS: tuple[str, ...] = ("returning", "alter_type_online")
RENDERED_ITEM_TYPE_PARAM = "rendered_item_type_requires_batch"
ALEMBIC_INI_NAME = "alembic.ini"
ROOT = Path(__file__).resolve().parents[3]


class MigrationCapError(RuntimeError):
    """迁移脚本缺能力声明，或生成了目标 flavor 不允许的直接类型变更。"""


def parse_caps_header(text: str) -> dict[str, str]:
    """解析 `# x-zhy-cap: returning:optional, alter_type_online:false`。"""
    for line in text.splitlines()[:40]:
        stripped = line.strip()
        if not stripped.startswith(CAP_HEADER_PREFIX):
            continue
        caps: dict[str, str] = {}
        for item in stripped[len(CAP_HEADER_PREFIX) :].split(","):
            key, sep, value = item.strip().partition(":")
            if not sep or not key.strip() or not value.strip():
                raise MigrationCapError(f"x-zhy-cap 条目非法：{item!r}")
            caps[key.strip()] = value.strip()
        missing = [key for key in REQUIRED_CAP_KEYS if key not in caps]
        if missing:
            raise MigrationCapError(f"x-zhy-cap 缺少声明：{', '.join(missing)}")
        return caps
    raise MigrationCapError(f"迁移脚本缺少 {CAP_HEADER_PREFIX} 头声明")


def read_revision_caps(path: Path) -> dict[str, str]:
    return parse_caps_header(path.read_text(encoding="utf-8"))


def validate_revision_file(path: Path) -> None:
    """静态校验：有头声明；类型变更必须声明 `alter_type_online:true` 且处于 batch 模板。"""
    text = path.read_text(encoding="utf-8")
    caps = parse_caps_header(text)
    lowered = text.lower()
    if "type_=" not in lowered:
        return
    if caps["alter_type_online"] != "true":
        raise MigrationCapError(
            f"{path.name}: 含类型变更却声明 alter_type_online:false；"
            "请改头声明，并为国产库提供四步模板分支"
        )
    if "batch_alter_table" not in lowered:
        raise MigrationCapError(
            f"{path.name}: 直接 ALTER COLUMN TYPE 会破坏国产库/ SQLite；"
            "请用 batch_alter_table 四步模板（加新列→回填→改名→删旧列）"
        )


FOUR_STEP_ONLY: frozenset[Flavor] = frozenset(
    {
        Flavor.OPENGAUSS_5,
        Flavor.OPENGAUSS_6,
        Flavor.OPENGAUSS_7,
        Flavor.KINGBASE,
    }
)


def assert_four_step_chain(revisions_dir: Path, target: Flavor | str) -> None:
    """国产库整条迁移链必须全部走四步模板（D34 强制执行）。

    revision 声明 `alter_type_online:true` 即表示含直接类型变更（含 batch 内直改，
    PG 系 batch 不会重建表），在 openGauss / Kingbase 上一律拒绝。
    """
    if Flavor(target) not in FOUR_STEP_ONLY:
        return
    for path in sorted(revisions_dir.glob("*.py")):
        if path.name.startswith("__"):
            continue
        caps = read_revision_caps(path)
        if caps["alter_type_online"] == "true":
            raise MigrationCapError(
                f"{path.name} 声明 alter_type_online:true：openGauss / Kingbase 禁止直接改类型，"
                "请改用四步模板（加新列→回填→改名→删旧列）后声明为 false"
            )


def supports_rendered_item_type_requires_batch() -> bool:
    """Alembic 1.20 起才提供该参数；1.16 用 render_as_batch 等价表达。"""
    try:
        params = inspect.signature(EnvironmentContext.configure).parameters
    except (TypeError, ValueError):  # pragma: no cover - 运行时不支持自省
        return False
    return RENDERED_ITEM_TYPE_PARAM in params


def context_options(target: Flavor | str) -> dict[str, Any]:
    """env.py 注入 MigrationContext 的选项（能力感知）。"""
    needs_batch = flavor_needs_batch(target)
    options: dict[str, Any] = {
        "render_as_batch": needs_batch,
        "compare_type": True,
    }
    if supports_rendered_item_type_requires_batch():  # pragma: no cover - 版本相关
        options[RENDERED_ITEM_TYPE_PARAM] = needs_batch
    return options


def _walk(container: Any, *, in_batch: bool = False) -> Iterable[tuple[Any, bool]]:
    if isinstance(container, BatchOperations):
        in_batch = True
    if isinstance(container, alembic_ops.MigrationScript):
        # 只守卫 upgrade 方向；downgrade 由 revision 手写
        yield from _walk(container.upgrade_ops, in_batch=in_batch)
        return
    for child in getattr(container, "ops", ()):
        yield child, in_batch
        yield from _walk(child, in_batch=in_batch)


def guard_revision_directives(directives: Iterable[Any], target: Flavor | str) -> None:
    """禁止需要四步模板的 flavor 执行直接类型变更（D34）。

    - PG / MySQL：允许在线改类型；
    - SQLite：仅允许 batch（重建表）；
    - openGauss / Kingbase：一律禁止（PG 系 batch 仍是直接 ALTER，不是四步模板）。

    作为 Alembic `process_revision_directives` 钩子使用：命中即中止。
    """
    flavor = Flavor(target)
    if flavor in {Flavor.POSTGRESQL, Flavor.MYSQL}:
        return
    allow_batch = flavor is Flavor.SQLITE
    for directive in directives:
        for operation, in_batch in _walk(directive):
            if not isinstance(operation, alembic_ops.AlterColumnOp):
                continue
            if operation.modify_type is None:
                continue
            if allow_batch and in_batch:
                continue
            raise MigrationCapError(
                f"{flavor.value} 禁止直接 ALTER COLUMN TYPE（{operation.table_name}."
                f"{operation.column_name}）：请使用四步模板（加新列→回填→改名→删旧列）"
            )


# ── 迁移执行入口（manage.py 与安装向导共用，避免两处 ini/URL 解析漂移）──────────


def alembic_config_for(url: str, target: Flavor | str) -> AlembicConfig:
    cfg = AlembicConfig(str(ROOT / ALEMBIC_INI_NAME))
    cfg.set_main_option("sqlalchemy.url", url)
    cfg.set_main_option("zhy.flavor", Flavor(target).value)
    return cfg


def current_revision(engine: Engine) -> str | None:
    with engine.connect() as conn:
        return MigrationContext.configure(conn).get_current_revision()


def upgrade_head(url: str, target: Flavor | str) -> str | None:
    """执行 `upgrade head` 并返回落库后的 revision（None 表示未读到）。"""
    from .engine import create_db_engine

    command.upgrade(alembic_config_for(url, target), "head")
    engine = create_db_engine(url, target)
    try:
        return current_revision(engine)
    finally:
        engine.dispose()
