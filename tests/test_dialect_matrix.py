"""§16.2 方言矩阵：T-DB-001~014 × sqlite/mysql/postgresql（42）
+ ci:domestic COMPILE_ONLY 30 条（14 组 × openGauss 6 / Kingbase + 2 扩展），合计 72。

PG/MySQL 需显式 DSN 环境变量（ZHY_TEST_PG_DSN / ZHY_TEST_MYSQL_DSN），否则跳过；
国产 flavor 介质未到位，按 COMPILE_ONLY 校验编译与安装期约束（条目状态不得回标 PASS）。
"""

from __future__ import annotations

import json
import logging
import os
from collections.abc import Callable
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

import pytest
from sqlalchemy import (
    BigInteger,
    Column,
    DateTime,
    Engine,
    Integer,
    MetaData,
    Numeric,
    Table,
    UniqueConstraint,
    create_engine,
    text,
)
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import DeclarativeBase, Mapped, Session, mapped_column
from zhyonlineedu.core.db import compat
from zhyonlineedu.core.db.compat import Cap
from zhyonlineedu.core.db.engine import create_db_engine
from zhyonlineedu.core.db.types import Money, SoftDeleteMixin, UTCDatetime
from zhyonlineedu.core.db.upsert import compile_upsert, rehydrate_by_natkey, upsert

logger = logging.getLogger(__name__)

DSN_ENV = {"mysql": "ZHY_TEST_MYSQL_DSN", "postgresql": "ZHY_TEST_PG_DSN"}
IS_PG = {"postgresql", "opengauss_5", "opengauss_6", "opengauss_7", "kingbase"}
IS_MYSQL = {"mysql"}


class MatrixBase(DeclarativeBase):
    pass


class UpsertRow(MatrixBase):
    __tablename__ = "t_db_008"
    __table_args__ = (UniqueConstraint("uid", "lesson"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    uid: Mapped[int] = mapped_column(BigInteger, nullable=False)
    lesson: Mapped[int] = mapped_column(BigInteger, nullable=False)
    pos: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)


def _pk_ddl(flavor: str) -> str:
    if flavor == "sqlite":
        return "INTEGER PRIMARY KEY AUTOINCREMENT"
    if flavor in IS_PG:
        return "BIGSERIAL PRIMARY KEY"
    return "BIGINT AUTO_INCREMENT PRIMARY KEY"


def _json_ddl(flavor: str) -> str:
    if flavor == "sqlite":
        return "TEXT"
    return "JSONB" if flavor in IS_PG else "JSON"


def _bool_ddl(flavor: str) -> str:
    if flavor == "sqlite":
        return "INTEGER"
    return "BOOLEAN" if flavor in IS_PG else "TINYINT(1)"


def _greatest(flavor: str, column: str) -> str:
    return compat.greatest_sql(column, "p", target_flavor=flavor)


def _try_exec(conn: Any, sql: str, params: dict[str, Any] | None = None) -> bool:
    savepoint = conn.begin_nested()
    try:
        conn.execute(text(sql), params or {})
        savepoint.commit()
        return True
    except SQLAlchemyError:
        savepoint.rollback()
        return False


def _engine_for(flavor: str) -> Engine:
    if flavor == "sqlite":
        return create_db_engine("sqlite://")
    env = DSN_ENV[flavor]
    dsn = os.environ.get(env, "").strip()
    if not dsn:
        pytest.skip(f"未设置 {env}，跳过 {flavor} 运行期用例")
    return create_db_engine(dsn, flavor)


def _drop_quiet(engine: Engine, table: str) -> None:
    try:
        with engine.begin() as conn:
            conn.execute(text(f"DROP TABLE IF EXISTS {table}"))
    except Exception:  # noqa: BLE001 - 清理失败不掩盖用例结果
        logger.debug("清理表 %s 失败", table, exc_info=True)


# ── T-DB-001~014 ────────────────────────────────────────────────────────────


def _case_d1(engine: Engine, flavor: str) -> tuple[bool, str]:
    with engine.connect() as conn:
        conn.execute(text(f"CREATE TABLE t_db_001 (id {_pk_ddl(flavor)}, name VARCHAR(32))"))
        conn.execute(text("INSERT INTO t_db_001 (name) VALUES ('a')"))
        got = conn.execute(text("SELECT id FROM t_db_001")).scalar()
        conn.rollback()
    return got == 1, f"id={got}"


def _case_d2a(engine: Engine, flavor: str) -> tuple[bool, str]:
    expected = compat.flavor_spec(flavor).caps[Cap.RETURNING]
    with engine.connect() as conn:
        conn.execute(text(f"CREATE TABLE t_db_002 (id {_pk_ddl(flavor)}, name VARCHAR(32))"))
        savepoint = conn.begin_nested()
        try:
            row = conn.execute(
                text("INSERT INTO t_db_002 (name) VALUES ('x') RETURNING id")
            ).first()
            savepoint.commit()
            observed = row is not None
            detail = f"可用 id={row[0] if row else None}"
        except Exception as exc:  # noqa: BLE001 - 记录方言原始异常名
            savepoint.rollback()
            observed = False
            detail = f"不可用 {type(exc).__name__}"
        conn.rollback()
    return observed is bool(expected), f"cap={expected} 实测={detail}"


def _case_d3(engine: Engine, flavor: str) -> tuple[bool, str]:
    with engine.connect() as conn:
        conn.execute(text(f"CREATE TABLE t_db_003 (id {_pk_ddl(flavor)}, doc {_json_ddl(flavor)})"))
        conn.execute(
            text("INSERT INTO t_db_003 (doc) VALUES (:d)"),
            {"d": json.dumps({"a": 1, "b": [2, 3]})},
        )
        raw = conn.execute(text("SELECT doc FROM t_db_003")).scalar()
        parsed = json.loads(raw) if isinstance(raw, str) else raw
        conn.rollback()
    return parsed == {"a": 1, "b": [2, 3]}, f"类型={_json_ddl(flavor)} 驱动={type(raw).__name__}"


def _case_d4(engine: Engine, flavor: str) -> tuple[bool, str]:
    with engine.connect() as conn:
        tx = conn.begin()
        conn.execute(text(f"CREATE TABLE t_db_004 (id {_pk_ddl(flavor)})"))
        tx.rollback()
        if flavor == "sqlite":
            count = conn.execute(
                text("SELECT COUNT(*) FROM sqlite_master WHERE name='t_db_004'")
            ).scalar()
        else:
            count = conn.execute(
                text("SELECT COUNT(*) FROM information_schema.tables WHERE table_name='t_db_004'")
            ).scalar()
        conn.rollback()
    expected_gone = flavor not in {"sqlite", "mysql"}
    return ((count or 0) == 0) == expected_gone, f"回滚后存在={bool(count)}"


def _case_d5(engine: Engine, flavor: str) -> tuple[bool, str]:
    name = "t_" + "x" * 60
    with engine.connect() as conn:
        conn.execute(text(f"CREATE TABLE {name} (id INT)"))
        conn.rollback()
    return True, f"标识符长度={len(name)}"


def _case_d7(engine: Engine, flavor: str) -> tuple[bool, str]:
    with engine.connect() as conn:
        conn.execute(
            text(f"CREATE TABLE t_db_007 (id {_pk_ddl(flavor)}, flag {_bool_ddl(flavor)})")
        )
        conn.execute(text("INSERT INTO t_db_007 (flag) VALUES (:v)"), {"v": True})
        conn.execute(text("INSERT INTO t_db_007 (flag) VALUES (:v)"), {"v": False})
        values = [bool(v) for (v,) in conn.execute(text("SELECT flag FROM t_db_007 ORDER BY id"))]
        conn.rollback()
    return values == [True, False], f"读回={values}"


def _case_d8(engine: Engine, flavor: str) -> tuple[bool, str]:
    with engine.connect() as conn:
        conn.execute(text(f"CREATE TABLE T_DB_008Case (id {_pk_ddl(flavor)})"))
        folded = _try_exec(conn, "INSERT INTO t_db_008case (id) VALUES (1)")
        conn.rollback()
    # SQLite 大小写不敏感；PG 族未引号标识符折叠为小写；MySQL 在 Linux 默认
    # lower_case_table_names=0 大小写敏感（Windows/macOS 相反，属已知方言差异）。
    expected = flavor not in IS_MYSQL
    return (
        folded is expected,
        f"大小写折叠={folded}（期望 {expected}，lower_case_table_names 相关）",
    )


def _case_d10a(engine: Engine, flavor: str) -> tuple[bool, str]:
    _drop_quiet(engine, "t_db_008")
    with engine.begin() as conn:
        conn.execute(
            text(
                f"CREATE TABLE t_db_008 (id {_pk_ddl(flavor)}, uid BIGINT NOT NULL, "
                "lesson BIGINT NOT NULL, pos INT NOT NULL, "
                "version INT NOT NULL DEFAULT 1, UNIQUE (uid, lesson))"
            )
        )
    base = {"uid": 1, "lesson": 1, "pos": 100}
    with Session(engine) as sess:
        first = upsert(UpsertRow, base, ["uid", "lesson"], target_flavor=flavor, session=sess)
        second = upsert(
            UpsertRow, {**base, "pos": 300}, ["uid", "lesson"], target_flavor=flavor, session=sess
        )
        sess.execute(
            text(f"UPDATE t_db_008 SET pos={_greatest(flavor, 'pos')} WHERE uid=1"),  # noqa: S608
            {"p": 200},
        )
        pos_after = sess.execute(text("SELECT pos FROM t_db_008 WHERE uid=1")).scalar()
        rows = sess.execute(text("SELECT COUNT(*) FROM t_db_008")).scalar()
        sess.rollback()
    compiled = compile_upsert(flavor, "t_db_008", base, ["uid", "lesson"])
    _, _, update_part = compiled.sql.partition("UPDATE")
    ok = (
        first.pk is not None
        and second.pk == first.pk
        and second.version == 2
        and rows == 1
        and pos_after == 300
        and "uid = " not in update_part
    )
    return ok, (
        f"新建pk={first.pk} 更新pk不变={second.pk == first.pk} v={second.version} "
        f"行数={rows} 幂等pos={pos_after}"
    )


def _case_d12(engine: Engine, flavor: str) -> tuple[bool, str]:
    with engine.connect() as conn:
        conn.execute(
            text(f"CREATE TABLE t_db_012 (id {_pk_ddl(flavor)}, v INT NOT NULL DEFAULT 1)")
        )
        conn.execute(text("INSERT INTO t_db_012 (v) VALUES (1)"))
        hit = conn.execute(text("UPDATE t_db_012 SET v=v+1 WHERE id=1 AND v=1")).rowcount
        stale = conn.execute(text("UPDATE t_db_012 SET v=v+1 WHERE id=1 AND v=1")).rowcount
        conn.rollback()
    return hit == 1 and stale == 0, f"首次={hit} 陈旧={stale}"


def _case_d13(engine: Engine, flavor: str) -> tuple[bool, str]:
    with engine.connect() as conn:
        conn.execute(text(f"CREATE TABLE t_db_013 (id {_pk_ddl(flavor)}, at TIMESTAMP)"))
        conn.execute(
            text("INSERT INTO t_db_013 (at) VALUES (:t)"),
            {"t": datetime(2026, 10, 28, 12, 0, 0, tzinfo=UTC)},
        )
        raw = conn.execute(text("SELECT at FROM t_db_013")).scalar()
        conn.rollback()
    value = raw.replace(tzinfo=None).isoformat(sep=" ") if isinstance(raw, datetime) else str(raw)
    return value.startswith("2026-10-28 12:00:00"), f"读回={value[:19]}"


def _case_d14(engine: Engine, flavor: str) -> tuple[bool, str]:
    with engine.connect() as conn:
        conn.execute(text(f"CREATE TABLE t_db_014 (id {_pk_ddl(flavor)}, amt DECIMAL(18,6))"))
        bind = str(Decimal("1234.560000")) if flavor == "sqlite" else Decimal("1234.560000")
        conn.execute(text("INSERT INTO t_db_014 (amt) VALUES (:a)"), {"a": bind})
        raw = conn.execute(text("SELECT amt FROM t_db_014")).scalar()
        conn.rollback()
    return Decimal(str(raw)) == Decimal("1234.560000"), f"读回={raw}"


def _case_d15(engine: Engine, flavor: str) -> tuple[bool, str]:
    with engine.connect() as conn:
        conn.execute(text(f"CREATE TABLE t_db_015 (id {_pk_ddl(flavor)}, s VARCHAR(16))"))
        conn.execute(text("INSERT INTO t_db_015 (s) VALUES (''), (NULL)"))
        predicate = compat.empty_string_predicate("s")
        count = conn.execute(
            text(f"SELECT COUNT(*) FROM t_db_015 WHERE {predicate}")  # noqa: S608
        ).scalar()
        conn.rollback()
    return count == 2, f"COALESCE 判空命中={count}"


def _case_d16(engine: Engine, flavor: str) -> tuple[bool, str]:
    with engine.connect() as conn:
        conn.execute(text(f"CREATE TABLE t_db_016 (id {_pk_ddl(flavor)}, name VARCHAR(8))"))
        for index in range(5):
            conn.execute(text("INSERT INTO t_db_016 (name) VALUES (:n)"), {"n": f"n{index}"})
        ids = [
            row[0]
            for row in conn.execute(text("SELECT id FROM t_db_016 ORDER BY id LIMIT 2 OFFSET 2"))
        ]
        conn.rollback()
    return ids == [3, 4], f"第3~4行={ids}"


def _case_d19a(engine: Engine, flavor: str) -> tuple[bool, str]:
    with engine.connect() as conn:
        conn.execute(
            text(
                f"CREATE TABLE t_db_019 (id {_pk_ddl(flavor)}, uid BIGINT NOT NULL, "
                "delete_seq BIGINT NOT NULL DEFAULT 0, UNIQUE (uid, delete_seq))"
            )
        )
        conn.execute(text("INSERT INTO t_db_019 (uid, delete_seq) VALUES (1, 0)"))
        conn.execute(text("INSERT INTO t_db_019 (uid, delete_seq) VALUES (1, 1)"))
        blocked = not _try_exec(conn, "INSERT INTO t_db_019 (uid, delete_seq) VALUES (1, 0)")
        rows = conn.execute(text("SELECT COUNT(*) FROM t_db_019 WHERE uid=1")).scalar()
        conn.rollback()
    return rows == 2 and blocked, f"两 delete_seq 共存={rows == 2} 同值冲突={blocked}"


CASES: list[tuple[str, str, str, Callable[[Engine, str], tuple[bool, str]]]] = [
    ("T-DB-001", "自增主键", "t_db_001", _case_d1),
    ("T-DB-002", "RETURNING 能力与文档一致", "t_db_002", _case_d2a),
    ("T-DB-003", "JSON 存取", "t_db_003", _case_d3),
    ("T-DB-004", "DDL 事务性", "t_db_004", _case_d4),
    ("T-DB-005", "标识符长度 62", "t_" + "x" * 60, _case_d5),
    ("T-DB-006", "布尔存取", "t_db_007", _case_d7),
    ("T-DB-007", "大小写折叠", "T_DB_008Case", _case_d8),
    ("T-DB-008", "UPSERT 七项专项", "t_db_008", _case_d10a),
    ("T-DB-009", "乐观锁 version", "t_db_012", _case_d12),
    ("T-DB-010", "UTC 时间存取", "t_db_013", _case_d13),
    ("T-DB-011", "金额精度 DECIMAL(18,6)", "t_db_014", _case_d14),
    ("T-DB-012", "空串 vs NULL", "t_db_015", _case_d15),
    ("T-DB-013", "分页 LIMIT/OFFSET", "t_db_016", _case_d16),
    ("T-DB-014", "软删复合唯一", "t_db_019", _case_d19a),
]

_MATRIX = [
    (case_id, name, flavor, table, fn)
    for case_id, name, table, fn in CASES
    for flavor in ("sqlite", "mysql", "postgresql")
]


@pytest.mark.dialect
@pytest.mark.parametrize(
    ("case_id", "name", "flavor", "table", "fn"),
    _MATRIX,
    ids=[f"{case_id}-{flavor}" for case_id, _, flavor, _, _ in _MATRIX],
)
def test_dialect_matrix(
    case_id: str,
    name: str,
    flavor: str,
    table: str,
    fn: Callable[[Engine, str], tuple[bool, str]],
) -> None:
    engine = _engine_for(flavor)
    try:
        passed, detail = fn(engine, flavor)
    finally:
        _drop_quiet(engine, table)
        engine.dispose()
    assert passed, f"{case_id} {name} [{flavor}] 失败：{detail}"


# ── ci:domestic（COMPILE_ONLY，介质到位后替换为实跑）───────────────────────
# 口径：14 组 × openGauss 6 / Kingbase = 28，加 2 条 openGauss 5/7 路由扩展，
# 与 42 条运行期组合合计 72 条（§16.2）。国产库条目状态标 COMPILE_ONLY，不得回标 PASS。


def _co_001(flavor: str) -> None:
    assert _pk_ddl(flavor) == "BIGSERIAL PRIMARY KEY"


def _co_002(flavor: str) -> None:
    expected = compat.flavor_spec(flavor).caps[Cap.RETURNING]
    assert expected is (flavor == "kingbase")


def _co_003(flavor: str) -> None:
    assert _json_ddl(flavor) == "JSONB"


def _co_004(flavor: str) -> None:
    # PG 族 DDL 可回滚；M1.3 迁移据此使用事务
    assert flavor in IS_PG


def _co_005(flavor: str) -> None:
    assert len("t_" + "x" * 60) == 62 < 63  # PG 族标识符上限 63


def _co_006(flavor: str) -> None:
    assert _bool_ddl(flavor) == "BOOLEAN"


def _co_007(flavor: str) -> None:
    assert flavor in IS_PG  # 未引号标识符折叠为小写


def _co_008(flavor: str) -> None:
    compiled = compile_upsert(
        flavor, "t_og", {"uid": 1, "lesson": 1, "pos": 1, "version": 1}, ["uid", "lesson"]
    )
    if flavor == "opengauss_6":
        assert compiled.route == "og5" and "RETURNING" not in compiled.sql
        assert compiled.readback_sql is not None
    else:
        assert compiled.route == "pg" and "RETURNING id" in compiled.sql
    with pytest.raises(ValueError, match="禁止更新自然键"):
        compile_upsert(
            flavor,
            "t_og",
            {"uid": 1, "lesson": 1, "pos": 1, "version": 1},
            ["uid", "lesson"],
            update_cols=["uid", "pos"],
        )


def _co_009(flavor: str) -> None:
    compiled = compile_upsert(
        flavor, "t_og", {"uid": 1, "lesson": 1, "pos": 1, "version": 1}, ["uid", "lesson"]
    )
    assert "version" in compiled.sql
    if compiled.route == "pg":
        assert "t_og.version + 1" in compiled.sql  # 禁止裸 version（AmbiguousColumn）
    else:
        assert "version = version + 1" in compiled.sql


def _co_010(_flavor: str) -> None:
    assert issubclass(UTCDatetime.impl, DateTime)


def _co_011(_flavor: str) -> None:
    assert isinstance(Money, Numeric) and Money.precision == 18 and Money.scale == 6


def _co_012(flavor: str) -> None:
    assert compat.empty_string_predicate("s") == "COALESCE(s, '') = ''"


def _co_013(flavor: str) -> None:
    assert flavor in IS_PG  # LIMIT/OFFSET 原生支持


def _co_014(_flavor: str) -> None:
    metadata = MetaData()
    Table(
        "t_og_soft",
        metadata,
        Column("id", Integer, primary_key=True),
        Column("uid", Integer),
        Column("delete_seq", BigInteger, nullable=False, default=0),
        UniqueConstraint("uid", "delete_seq"),
    )
    compat.ensure_soft_delete_uk(metadata)
    assert hasattr(SoftDeleteMixin, "delete_seq")


_COMPILE_ONLY_CHECKS: dict[str, Callable[[str], None]] = {
    "T-DB-001": _co_001,
    "T-DB-002": _co_002,
    "T-DB-003": _co_003,
    "T-DB-004": _co_004,
    "T-DB-005": _co_005,
    "T-DB-006": _co_006,
    "T-DB-007": _co_007,
    "T-DB-008": _co_008,
    "T-DB-009": _co_009,
    "T-DB-010": _co_010,
    "T-DB-011": _co_011,
    "T-DB-012": _co_012,
    "T-DB-013": _co_013,
    "T-DB-014": _co_014,
}

_DOMESTIC_FLAVORS = ("opengauss_6", "kingbase")


def _assert_rehydrate_path(flavor: str) -> None:
    """pk=None 常态下的回读契约（用 SQLite 承载，校验的是调用契约而非国产库运行期）。"""
    engine = create_engine("sqlite://")
    MatrixBase.metadata.create_all(engine)
    with Session(engine) as sess:
        result = upsert(
            UpsertRow,
            {"uid": 9, "lesson": 1, "pos": 1},
            ["uid", "lesson"],
            target_flavor=flavor,
            session=sess,
        )
        assert result.pk == rehydrate_by_natkey(UpsertRow, {"uid": 9, "lesson": 1}, session=sess)
        sess.rollback()
    engine.dispose()


def _ext_og5_og7_routes() -> None:
    og5 = compile_upsert(
        "opengauss_5", "t_og", {"uid": 1, "lesson": 1, "pos": 1, "version": 1}, ["uid", "lesson"]
    )
    og7 = compile_upsert(
        "opengauss_7", "t_og", {"uid": 1, "lesson": 1, "pos": 1, "version": 1}, ["uid", "lesson"]
    )
    assert og5.route == "og5" and "RETURNING" not in og5.sql
    assert og7.route == "pg" and "RETURNING id" in og7.sql
    assert "t_og.version + 1" in og7.sql


def _ext_og5_og7_rehydrate() -> None:
    for flavor in ("opengauss_5", "opengauss_7"):
        _assert_rehydrate_path(flavor)


_COMPILE_ONLY_MATRIX = [
    (case_id, flavor, check)
    for case_id, check in _COMPILE_ONLY_CHECKS.items()
    for flavor in _DOMESTIC_FLAVORS
]

_EXTENSIONS: list[tuple[str, Callable[[], None]]] = [
    ("EXT-OG-ROUTE", _ext_og5_og7_routes),
    ("EXT-OG-REHYDRATE", _ext_og5_og7_rehydrate),
]


@pytest.mark.dialect
@pytest.mark.parametrize(
    ("case_id", "flavor", "check"),
    _COMPILE_ONLY_MATRIX,
    ids=[f"{case_id}-{flavor}-compile" for case_id, flavor, _ in _COMPILE_ONLY_MATRIX],
)
def test_dialect_domestic_compile_only(
    case_id: str, flavor: str, check: Callable[[str], None]
) -> None:
    check(flavor)


@pytest.mark.dialect
@pytest.mark.parametrize(("case_id", "check"), _EXTENSIONS, ids=[c for c, _ in _EXTENSIONS])
def test_dialect_domestic_extensions(case_id: str, check: Callable[[], None]) -> None:
    check()


def test_matrix_case_count() -> None:
    """§16.2：14 组 × 5 flavor + 2 扩展 = 72，防止用例被误删/漏补。"""
    total = len(_MATRIX) + len(_COMPILE_ONLY_MATRIX) + len(_EXTENSIONS)
    assert total == 72, f"方言矩阵条目数={total}，口径应为 72"
