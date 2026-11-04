"""`sdk.store`：插件唯一合法的自有表读写入口（M3.4 / M3.7 · §11.9）。

口径：
- **不暴露** ORM session / 裸 SQL：写入只能走内核 `upsert` 契约
  （自然键不更新、version 自增、跨方言三路编译 + 应用兜底），读取只能走 `fetch`；
- **只能读写自己的表**：表名必须以 `plg_<code>_` 开头（code 取自当前 manifest），
  访问内核表或他人插件表一律 `PLUGIN_TABLE_PREFIX_INVALID`；
- 表必须含 `delete_seq`（与迁移期强校验一致），否则拒绝访问；`fetch` 固定只读
  `delete_seq = 0` 的行（软删行对插件不可见）；
- 会话由 SDK 自建短事务，插件拿不到会话对象；`fetch` 行数上限 200，防单次拖垮进程。
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import date, datetime
from decimal import Decimal
from typing import Any

from flask import current_app
from sqlalchemy import Table, inspect, select
from sqlalchemy.orm import Mapper

from zhyonlineedu.core.codes import (
    PLUGIN_MIGRATION_INVALID,
    PLUGIN_TABLE_PREFIX_INVALID,
    VALIDATION_FAILED,
)
from zhyonlineedu.core.db.engine import Database, ensure_database
from zhyonlineedu.core.db.upsert import UpsertResult
from zhyonlineedu.core.db.upsert import upsert as _upsert
from zhyonlineedu.core.errors import AppError
from zhyonlineedu.core.plugin.manifest import expected_table_prefix
from zhyonlineedu.core.sdk.context import current

__all__ = ["MAX_FETCH_ROWS", "fetch", "upsert"]

MAX_FETCH_ROWS = 200


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


def _own_table(model: Any) -> Table:
    """解析模型表并强校验「本插件自有表 + 含 delete_seq」。"""
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
    return table


def _database() -> Database:
    config = current_app.extensions["zhy_config"]
    return ensure_database(config)


def _cell(value: Any) -> Any:
    """行值转 JSON 友好：日期 → ISO 字符串、Decimal → 字符串（避免浮点丢精度）。"""
    if isinstance(value, datetime):
        return value.isoformat(timespec="seconds")
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, Decimal):
        return str(value)
    return value


def upsert(
    model: Any,
    values: dict[str, Any],
    natkey_cols: Sequence[str],
    update_cols: Sequence[str] | None = None,
) -> UpsertResult:
    """按自然键 UPSERT 插件自有表；返回 `UpsertResult`（`pk` 可能需回读）。"""
    _own_table(model)
    db = _database()
    with db.session_scope() as session:
        result = _upsert(model, dict(values), natkey_cols, update_cols, session=session)
        session.commit()
    return result


def fetch(
    model: Any,
    *,
    where: Mapping[str, Any] | None = None,
    limit: int = 50,
    order_by: str = "id",
    descending: bool = True,
) -> list[dict[str, Any]]:
    """按列等值过滤读取插件自有表（只读 soft-delete 未命中的行）。

    - `where` 的键必须是真实列名（防拼列名注入），值走绑定参数；
    - `order_by` 必须是真实列名，默认按主键倒序（最近写入优先）；
    - `limit` 1..`MAX_FETCH_ROWS`，超过即拒（避免插件单次读取拖垮进程）。
    """
    table = _own_table(model)
    if not isinstance(limit, int) or isinstance(limit, bool) or not 1 <= limit <= MAX_FETCH_ROWS:
        raise AppError(
            VALIDATION_FAILED,
            data={"errors": [f"limit 必须在 1..{MAX_FETCH_ROWS}"]},
        )
    filters = dict(where or {})
    unknown = sorted(set(filters) - set(table.c.keys()))
    if unknown:
        raise AppError(VALIDATION_FAILED, data={"errors": ["未知列：" + ", ".join(unknown)]})
    if order_by not in table.c:
        raise AppError(VALIDATION_FAILED, data={"errors": [f"未知排序列：{order_by}"]})

    conditions: list[Any] = [table.c["delete_seq"] == 0]
    conditions.extend(table.c[name] == value for name, value in filters.items())
    column = table.c[order_by]
    order = column.desc() if descending else column.asc()

    db = _database()
    with db.session_scope() as session:
        rows = session.execute(select(table).where(*conditions).order_by(order).limit(limit)).all()
    columns = list(table.c.keys())
    return [{name: _cell(row._mapping[name]) for name in columns} for row in rows]
