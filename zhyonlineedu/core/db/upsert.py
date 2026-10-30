"""跨方言 UPSERT 契约（§5.4 · F1 核心）。

契约（docstring 即测试依据）：
1. 不抛 IntegrityError 给调用方；冲突重试最多 2 次，仍冲突返 UpsertResult(conflict=True, pk=None)；
2. 绝不更新自然键列与主键列（openGauss 5/6 硬限制）；
3. version 列存在时自动 +1，用于进度对账（F2）；
4. 批量模式下单条失败不中断整批，失败行进 UpsertBatchReport 供告警；
5. 生成 SQL 必须可被 scripts/explain_upsert.py <flavor> 文本比对，禁止黑盒拼接。

三路编译（§5.2 D10a）：
- _pg：    ON CONFLICT ... DO UPDATE [返回 id, version]  （pg / kingbase / opengauss_7）
- _mysql： ON DUPLICATE KEY UPDATE                        （mysql / opengauss_5 / opengauss_6）
- _og5：   同上但不返回行 id，且不得更新唯一键/主键列
- 兜底：   select → update/insert + 冲突重试

本文件是唯一允许出现方言 UPSERT 关键字的位置（§5.2 红线白名单）。
"""

from __future__ import annotations

import logging
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from typing import Any, cast

from sqlalchemy import Table, inspect, select, text
from sqlalchemy.engine import CursorResult
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from sqlalchemy.orm import Mapper, Session

from . import compat
from .compat import Cap, Flavor, Route
from .engine import current_session

logger = logging.getLogger(__name__)

_MAX_CONFLICT_RETRIES = 2  # 首次 + 重试 2 次


@dataclass(frozen=True)
class UpsertResult:
    """pk 可能为 None（openGauss 5/6 常态）——调用方必须 rehydrate_by_natkey() 回读。"""

    created: bool
    pk: Any | None
    version: int = 1
    conflict: bool = False


@dataclass(frozen=True)
class UpsertBatchReport:
    results: list[UpsertResult]
    failed_rows: list[dict[str, Any]]  # 单条失败不中断整批，供告警


@dataclass(frozen=True)
class CompiledUpsert:
    """可文本比对的编译产物（§5.4 第 5 条）。"""

    flavor: Flavor
    route: Route
    table: str
    sql: str
    params: dict[str, Any]
    returning: bool
    returning_cols: tuple[str, ...] = ()
    version_column: str | None = "version"
    readback_sql: str | None = None
    notes: tuple[str, ...] = ()


def compile_upsert(
    flavor: Flavor | str,
    table: str,
    values: dict[str, Any],
    natkey: Sequence[str],
    update_cols: Sequence[str] | None = None,
    *,
    pk_column: str | None = "id",
    version_column: str | None = "version",
    route: Route | None = None,
) -> CompiledUpsert:
    """生成方言 UPSERT 语句；update_cols=None 表示除自然键/主键/version 外全列更新。

    pk_column / version_column 传 None 表示该表无此列（如 sys_compat_flags 以 cap 为主键）。
    """
    target = Flavor(flavor)
    target_route: Route = route or compat.route_for(target)
    cols = list(values)
    missing = [c for c in natkey if c not in values]
    if missing:
        raise ValueError(f"自然键列缺少值：{missing}")

    has_version = bool(version_column and version_column in values)
    returning_cols: tuple[str, ...] = tuple(
        c for c in (pk_column, version_column if has_version else None) if c
    )
    protected = {c for c in (pk_column, version_column) if c} | set(natkey)

    if update_cols is None:
        updatable = [c for c in cols if c not in protected]
    else:
        hit = [c for c in update_cols if c in protected]
        if hit:
            raise ValueError(f"契约 2：禁止更新自然键/主键列：{hit}")
        updatable = [c for c in update_cols if c in values]

    col_sql = ", ".join(cols)
    val_sql = ", ".join(f":{c}" for c in cols)
    natkey_where = " AND ".join(f"{c} = :{c}" for c in natkey)
    returning_sql = f"\nRETURNING {', '.join(returning_cols)}" if returning_cols else ""

    if target_route == "pg":
        set_parts = [f"{c} = EXCLUDED.{c}" for c in updatable]
        if has_version:
            # M0 实测：右侧裸 version 在 PG 会 AmbiguousColumn（目标表与 EXCLUDED 同名）。
            set_parts.append(f"{version_column} = {table}.{version_column} + 1")
        conflict = ", ".join(natkey)
        set_sql = ",\n    ".join(set_parts)
        sql = (
            f"INSERT INTO {table} ({col_sql})\nVALUES ({val_sql})\n"
            f"ON CONFLICT ({conflict}) DO UPDATE SET\n    {set_sql}{returning_sql}"
        )
        return CompiledUpsert(
            flavor=target,
            route=target_route,
            table=table,
            sql=sql,
            params=dict(values),
            returning=bool(returning_cols),
            returning_cols=returning_cols,
            version_column=version_column if has_version else None,
            notes=("version 自增必须表名限定",),
        )

    set_parts = [f"{c} = VALUES({c})" for c in updatable]
    if has_version:
        set_parts.append(f"{version_column} = {version_column} + 1")
    set_sql = ",\n    ".join(set_parts)
    sql = (
        f"INSERT INTO {table} ({col_sql})\nVALUES ({val_sql})\n"
        f"ON DUPLICATE KEY UPDATE\n    {set_sql}"
    )
    readback = (
        # 标识符来自 ORM 元数据、值走绑定参数，无外部输入拼接（S608 白名单）
        f"SELECT {', '.join(returning_cols)} FROM {table} WHERE {natkey_where}"  # noqa: S608
        if returning_cols
        else None
    )
    notes: tuple[str, ...] = (
        "无行级返回，必须按自然键回读",
        "不得更新自然键/主键列",
    )
    return CompiledUpsert(
        flavor=target,
        route=target_route,
        table=table,
        sql=sql,
        params=dict(values),
        returning=False,
        returning_cols=returning_cols,
        version_column=version_column if has_version else None,
        readback_sql=readback,
        notes=notes,
    )


def _model_table(model: type) -> Table:
    mapper: Any = inspect(model)
    if not isinstance(mapper, Mapper):
        raise TypeError(f"upsert 需要 ORM 模型类，收到：{model!r}")
    table = mapper.local_table
    if not isinstance(table, Table):
        raise TypeError(f"无法解析模型 {model!r} 的表")
    return table


def _pk_column(table: Table) -> str:
    pk = list(table.primary_key.columns)
    if len(pk) != 1:
        raise ValueError(f"upsert 仅支持单列主键：{table.name}")
    return str(pk[0].name)


def _natkey_conditions(table: Table, natkey: Sequence[str], values: dict[str, Any]) -> list[Any]:
    return [table.c[name] == values[name] for name in natkey]


def _readback(session: Session, compiled: CompiledUpsert, natkey: Sequence[str]) -> Any | None:
    params = {name: compiled.params[name] for name in natkey}
    if compiled.readback_sql is None:  # pragma: no cover - 编译期保证
        return None
    return session.execute(text(compiled.readback_sql), params).first()


def _result_from_row(
    compiled: CompiledUpsert, row: Any, *, created_hint: bool | None
) -> UpsertResult:
    if row is None:
        return UpsertResult(created=False, pk=None, conflict=True)
    mapped = dict(zip(compiled.returning_cols, row, strict=False))
    pk = mapped.get(compiled.returning_cols[0]) if compiled.returning_cols else None
    version_value = mapped.get(compiled.version_column) if compiled.version_column else None
    provided = int(compiled.params.get(compiled.version_column or "", 1) or 1)
    version = int(version_value) if version_value is not None else provided
    created = version == provided if created_hint is None else created_hint
    return UpsertResult(created=created, pk=pk, version=version)


def _execute_native(
    session: Session,
    compiled: CompiledUpsert,
    natkey: Sequence[str],
) -> UpsertResult:
    # 结果必须在 savepoint 内消费/关闭：SQLite 驱动若在 RELEASE 时仍有未取完的游标，
    # 会抛 "cannot open savepoint - SQL statements in progress"（M1.2 实测）。
    with session.begin_nested():
        result = session.execute(text(compiled.sql), compiled.params)
        row = result.first() if compiled.returning else _readback(session, compiled, natkey)
        rowcount = getattr(result, "rowcount", -1)
        result.close()

    if compiled.returning:
        return _result_from_row(compiled, row, created_hint=None)
    created_hint = (rowcount == 1) if rowcount in (1, 2) else None
    return _result_from_row(compiled, row, created_hint=created_hint)


def _execute_fallback(
    session: Session,
    table: Table,
    values: dict[str, Any],
    natkey: Sequence[str],
    update_cols: Sequence[str] | None,
    pk_column: str,
) -> UpsertResult:
    """应用兜底：select → update / insert；冲突最多重试 2 次。"""
    columns = set(table.c.keys())
    payload = {k: v for k, v in values.items() if k in columns}
    protected = {pk_column, *natkey, "version"}
    if update_cols is None:
        updatable = [c for c in payload if c not in protected]
    else:
        updatable = [c for c in update_cols if c in payload and c not in protected]
    has_version = "version" in columns
    conditions = _natkey_conditions(table, natkey, values)

    for attempt in range(_MAX_CONFLICT_RETRIES + 1):
        try:
            with session.begin_nested():
                existing = session.execute(
                    select(
                        table.c[pk_column],
                        table.c["version"] if has_version else table.c[pk_column],
                    )
                    .where(*conditions)
                    .limit(1)
                ).first()
                if existing is not None:
                    sets: dict[str, Any] = {c: payload[c] for c in updatable}
                    if has_version:
                        sets["version"] = table.c["version"] + 1
                    session.execute(table.update().where(*conditions).values(**sets))
                    current_version = int(existing[1]) if has_version else 0
                    return UpsertResult(
                        created=False,
                        pk=existing[0],
                        version=current_version + 1 if has_version else 1,
                    )
                cursor = cast(CursorResult[Any], session.execute(table.insert().values(**payload)))
            inserted = list(cursor.inserted_primary_key) if cursor.inserted_primary_key else []
            pk = inserted[0] if inserted else None
            if pk is None:
                existing = session.execute(select(table.c[pk_column]).where(*conditions)).first()
                pk = existing[0] if existing else None
            return UpsertResult(
                created=True,
                pk=pk,
                version=int(payload.get("version", 1)),
            )
        except IntegrityError:
            logger.warning(
                "upsert 冲突重试 table=%s natkey=%s attempt=%s",
                table.name,
                natkey,
                attempt + 1,
            )
    return UpsertResult(created=False, pk=None, conflict=True)


def upsert(
    model: type,
    values: dict[str, Any],
    natkey_cols: Sequence[str],
    update_cols: Sequence[str] | None = None,
    target_flavor: str | None = None,
    *,
    session: Session | None = None,
) -> UpsertResult:
    """唯一合法 upsert 入口。裸写 ON CONFLICT / ON DUPLICATE KEY UPDATE 被 CI 拦截。"""
    table = _model_table(model)
    pk_column = _pk_column(table)
    payload = dict(values)
    if "version" in table.c and "version" not in payload:
        payload["version"] = 1

    flavor = Flavor(target_flavor) if target_flavor else compat.flavor()
    compiled = compile_upsert(
        flavor, table.name, payload, natkey_cols, update_cols, pk_column=pk_column
    )
    sess = session or current_session()
    try:
        return _execute_native(sess, compiled, natkey_cols)
    except SQLAlchemyError as exc:
        logger.warning(
            "方言 UPSERT 原生路径失败，转应用兜底：flavor=%s table=%s error=%s",
            compiled.flavor,
            table.name,
            type(exc).__name__,
        )
        return _execute_fallback(sess, table, payload, natkey_cols, update_cols, pk_column)


def upsert_many(
    model: type,
    rows: Iterable[dict[str, Any]],
    natkey_cols: Sequence[str],
    batch: int = 200,
    *,
    session: Session | None = None,
) -> UpsertBatchReport:
    sess = session or current_session()
    results: list[UpsertResult] = []
    failed: list[dict[str, Any]] = []
    chunk: list[dict[str, Any]] = []
    for row in rows:
        chunk.append(row)
        if len(chunk) >= batch:
            _flush_chunk(sess, model, chunk, natkey_cols, results, failed)
            chunk = []
    if chunk:
        _flush_chunk(sess, model, chunk, natkey_cols, results, failed)
    return UpsertBatchReport(results=results, failed_rows=failed)


def _flush_chunk(
    session: Session,
    model: type,
    chunk: list[dict[str, Any]],
    natkey_cols: Sequence[str],
    results: list[UpsertResult],
    failed: list[dict[str, Any]],
) -> None:
    for row in chunk:
        try:
            results.append(upsert(model, row, natkey_cols, session=session))
        except Exception as exc:  # noqa: BLE001 - 契约 4：单条失败不中断整批
            failed.append({"row": row, "error": f"{type(exc).__name__}: {exc}"})


def rehydrate_by_natkey(
    model: type, natkey: dict[str, Any], *, session: Session | None = None
) -> Any | None:
    """唯一合法"补 id"入口（UpsertResult.pk=None 时调用）。"""
    table = _model_table(model)
    pk_column = _pk_column(table)
    conditions = _natkey_conditions(table, list(natkey), natkey)
    sess = session or current_session()
    row = sess.execute(select(table.c[pk_column]).where(*conditions).limit(1)).first()
    return row[0] if row else None


def supports_capability(target: Flavor | str, capability: Cap) -> bool:
    """编译期能力判断（供 probe 与 explain 脚本复用，不触碰运行时状态）。"""
    return bool(compat.flavor_spec(target).caps[capability])
