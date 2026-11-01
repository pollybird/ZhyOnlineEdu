"""`sdk.store`：插件唯一合法的数据写入入口（M3.4 · §11.9）。

口径：
- **不暴露** ORM session / 裸 SQL：插件只能通过本函数走内核 `upsert` 契约
  （自然键不更新、version 自增、跨方言三路编译 + 应用兜底）；
- **只能写自己的表**：表名必须以 `plg_<code>_` 开头（code 取自当前 manifest），
  写内核表或他人插件表一律 `PLUGIN_TABLE_PREFIX_INVALID`；
- 表必须含 `delete_seq`（与迁移期强校验一致），否则拒绝写入；
- 会话由 SDK 自建短事务并提交，插件拿不到会话对象。
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from flask import current_app
from sqlalchemy import Table, inspect
from sqlalchemy.orm import Mapper

from zhyonlineedu.core.codes import PLUGIN_MIGRATION_INVALID, PLUGIN_TABLE_PREFIX_INVALID
from zhyonlineedu.core.db.engine import Database, ensure_database
from zhyonlineedu.core.db.upsert import UpsertResult
from zhyonlineedu.core.db.upsert import upsert as _upsert
from zhyonlineedu.core.errors import AppError
from zhyonlineedu.core.plugin.manifest import expected_table_prefix
from zhyonlineedu.core.sdk.context import current

__all__ = ["upsert"]


def _mapped_table(model: Any) -> Table:
    mapper: Any = inspect(model)
    if not isinstance(mapper, Mapper):
        raise AppError(
            PLUGIN_TABLE_PREFIX_INVALID,
            data={"reason": "model_required", "model": repr(model)},
        )
    table = mapper.local_table
    if not isinstance(table, Table):  # pragma: no cover - ORM 元数据异常
        raise AppError(PLUGIN_TABLE_PREFIX_INVALID, data={"reason": "table_unresolved"})
    return table


def _database() -> Database:
    config = current_app.extensions["zhy_config"]
    return ensure_database(config)


def upsert(
    model: Any,
    values: dict[str, Any],
    natkey_cols: Sequence[str],
    update_cols: Sequence[str] | None = None,
) -> UpsertResult:
    """按自然键 UPSERT 插件自有表；返回 `UpsertResult`（`pk` 可能需回读）。"""
    scope = current()
    table = _mapped_table(model)
    prefix = expected_table_prefix(scope.code)
    if not table.name.startswith(prefix):
        raise AppError(
            PLUGIN_TABLE_PREFIX_INVALID,
            data={"reason": "foreign_table", "table": table.name, "expected_prefix": prefix},
        )
    if "delete_seq" not in table.c:
        raise AppError(
            PLUGIN_MIGRATION_INVALID,
            data={"reason": "missing_delete_seq", "table": table.name},
        )

    db = _database()
    with db.session_scope() as session:
        result = _upsert(model, dict(values), natkey_cols, update_cols, session=session)
        session.commit()
    return result
