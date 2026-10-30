"""M0 Spike · 方言 upsert 三路编译原型（§5.2 D10a / §5.4）。

目的：在 M1.2 落地前，用最小实现验证三路编译 + 兜底的**契约形态**与
`UpsertResult(created, pk, version)` 语义，特别是 openGauss 5/6 的 `pk=None` 常态。

路由（§5.1）：
  sqlite / postgresql / kingbase / opengauss_7  → pg   (ON CONFLICT ... DO UPDATE)
  mysql                                          → mysql(ON DUPLICATE KEY UPDATE)
  opengauss_5 / opengauss_6                      → og5  (MySQL 风，无 RETURNING，
                                                        且不得更新唯一键/主键列)
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any, Literal

Route = Literal["pg", "mysql", "og5"]

ROUTE: dict[str, Route] = {
    "sqlite": "pg",
    "postgresql": "pg",
    "opengauss_7": "pg",
    "kingbase": "pg",
    "mysql": "mysql",
    "opengauss_5": "og5",
    "opengauss_6": "og5",
}

# 能力位（§5.1 表；keybase/OG 未实测，按文档写预期，spike 记录差异）
CAPS: dict[str, dict[str, bool]] = {
    "sqlite": {
        "returning": True,
        "conflict": True,
        "on_duplicate": False,
        "bulk_returning_ids": False,
    },
    "mysql": {
        "returning": False,
        "conflict": False,
        "on_duplicate": True,
        "bulk_returning_ids": False,
    },
    "postgresql": {
        "returning": True,
        "conflict": True,
        "on_duplicate": False,
        "bulk_returning_ids": True,
    },
    "opengauss_5": {
        "returning": False,
        "conflict": False,
        "on_duplicate": True,
        "bulk_returning_ids": False,
    },
    "opengauss_6": {
        "returning": False,
        "conflict": False,
        "on_duplicate": True,
        "bulk_returning_ids": False,
    },
    "opengauss_7": {
        "returning": True,
        "conflict": True,
        "on_duplicate": True,
        "bulk_returning_ids": False,
    },
    "kingbase": {
        "returning": True,
        "conflict": True,
        "on_duplicate": False,
        "bulk_returning_ids": True,
    },
}


@dataclass(frozen=True)
class UpsertResult:
    created: bool
    pk: Any | None
    version: int = 1
    conflict: bool = False


@dataclass
class CompiledUpsert:
    flavor: str
    route: Route
    sql: str
    params: dict[str, Any]
    readback_sql: str | None = None  # mysql/og5 需按自然键回读
    upsert: UpsertResult | None = None
    notes: list[str] = field(default_factory=list)


def compile_upsert(
    flavor: str,
    table: str,
    values: dict[str, Any],
    natkey: Sequence[str],
    update_cols: Sequence[str] | None = None,
) -> CompiledUpsert:
    """生成方言 UPSERT 语句；update_cols=None 表示除自然键/主键/version 外全列更新。"""
    route = ROUTE[flavor]
    cols = list(values)
    updatable = (
        list(update_cols)
        if update_cols is not None
        else [c for c in cols if c not in natkey and c != "id" and c != "version"]
    )
    if route == "og5":
        # openGauss 5/6 硬限制：被更新行不得含唯一键/主键列
        bad = [c for c in updatable if c in natkey or c == "id"]
        if bad:
            raise ValueError(f"openGauss 5/6 不允许更新自然键/主键列：{bad}")

    col_sql = ", ".join(cols)
    val_sql = ", ".join(f":{c}" for c in cols)

    if route == "pg":
        conflict = ", ".join(natkey)
        set_sql = ",\n    ".join(f"{c} = EXCLUDED.{c}" for c in updatable)
        if "version" in cols:
            # 发现 1（PG RUNTIME 实测）：SET version = version + 1 报
            # AmbiguousColumn —— 右侧 version 同时命中目标表与 EXCLUDED。
            # 契约：version 自增必须以表名限定。
            set_sql += f",\n    version = {table}.version + 1"
        sql = (
            f"INSERT INTO {table} ({col_sql})\nVALUES ({val_sql})\n"
            f"ON CONFLICT ({conflict}) DO UPDATE SET\n    {set_sql}\nRETURNING id, version"
        )
        return CompiledUpsert(flavor=flavor, route=route, sql=sql, params=dict(values))

    if route == "mysql":
        set_sql = ",\n    ".join(f"{c} = VALUES({c})" for c in updatable)
        if "version" in cols:
            set_sql += ",\n    version = version + 1"
        sql = (
            f"INSERT INTO {table} ({col_sql})\nVALUES ({val_sql})\n"
            f"ON DUPLICATE KEY UPDATE\n    {set_sql}"
        )
        # LAST_INSERT_ID() 不含被更新行 id（D10a）→ 一律按自然键回读
        where = " AND ".join(f"{c} = :{c}" for c in natkey)
        readback = f"SELECT id, version FROM {table} WHERE {where}"
        return CompiledUpsert(
            flavor=flavor,
            route=route,
            sql=sql,
            params=dict(values),
            readback_sql=readback,
            notes=["id 走自然键回读"],
        )

    # og5：MySQL 风，禁止 RETURNING
    set_sql = ",\n    ".join(f"{c} = VALUES({c})" for c in updatable)
    if "version" in cols:
        set_sql += ",\n    version = version + 1"
    sql = (
        f"INSERT INTO {table} ({col_sql})\nVALUES ({val_sql})\n"
        f"ON DUPLICATE KEY UPDATE\n    {set_sql}"
    )
    where = " AND ".join(f"{c} = :{c}" for c in natkey)
    readback = f"SELECT id, version FROM {table} WHERE {where}"
    return CompiledUpsert(
        flavor=flavor,
        route=route,
        sql=sql,
        params=dict(values),
        readback_sql=readback,
        notes=["NO RETURNING", "自然键/主键列不可更新", "id 走自然键回读"],
    )
