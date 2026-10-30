"""Alembic 环境（§5.6 能力感知迁移）。

- 连接串/flavor：`-x` 覆盖 → alembic.ini（测试注入）→ 三层配置（defaults → 环境变量）；
- `render_as_batch` / `rendered_item_type_requires_batch` 按 flavor 注入（D34）；
- 生成期守卫：需要四步模板的 flavor 不允许直接 ALTER COLUMN TYPE；
- 启动即断言软删复合 UK（D19a），缺则 fail-fast。
"""

from __future__ import annotations

from logging.config import fileConfig
from pathlib import Path
from typing import Any

from alembic import context
from zhyonlineedu.core.config import load_config
from zhyonlineedu.core.db import compat
from zhyonlineedu.core.db import models as db_models
from zhyonlineedu.core.db.base import Base
from zhyonlineedu.core.db.engine import create_db_engine
from zhyonlineedu.core.db.migration import (
    assert_four_step_chain,
    context_options,
    guard_revision_directives,
)

# 导入模型包即注册进 Base.metadata（唯一 DDL 来源）
_ = db_models

config = context.config
if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = Base.metadata


def _resolve_target() -> tuple[str, compat.Flavor]:
    """优先级：`-x url=/-x flavor=` → alembic.ini → 三层配置（defaults → 环境变量）。"""
    x_args = context.get_x_argument(as_dictionary=True)
    url = (x_args.get("url") or config.get_main_option("sqlalchemy.url") or "").strip()
    raw_flavor = (x_args.get("flavor") or config.get_main_option("zhy.flavor") or "").strip()
    if not url or not raw_flavor:
        cfg = load_config()
        url = url or cfg.get_str("db.url")
        raw_flavor = raw_flavor or (cfg.get_str("db.flavor") or "")
    if not url:
        raise RuntimeError("未提供数据库连接串：请用 -x url=... 或设置 ZHY_DB_URL")
    flavor = compat.Flavor(raw_flavor) if raw_flavor else compat.detect_flavor(url)
    return url, flavor


def _process_revision_directives(_context: Any, _revision: Any, directives: list[Any]) -> None:
    guard_revision_directives(directives, _flavor)


URL, _flavor = _resolve_target()
compat.configure(url=URL, flavor=_flavor, source="migration")
# 国产库：整条链必须四步模板（D34）
assert_four_step_chain(Path(config.get_main_option("script_location")) / "versions", _flavor)
# 软删表缺复合 UK 时禁止迁移（D19a / F6）
compat.ensure_soft_delete_uk(target_metadata)

_OPTIONS = context_options(_flavor)


def run_migrations_offline() -> None:
    """离线模式生成 SQL（CI 国产库 COMPILE_ONLY 用）。"""
    context.configure(
        url=URL,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        include_schemas=False,
        process_revision_directives=_process_revision_directives,
        **_OPTIONS,
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    engine = create_db_engine(URL, _flavor)
    try:
        with engine.connect() as connection:
            context.configure(
                connection=connection,
                target_metadata=target_metadata,
                include_schemas=False,
                process_revision_directives=_process_revision_directives,
                **_OPTIONS,
            )
            with context.begin_transaction():
                context.run_migrations()
    finally:
        engine.dispose()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
