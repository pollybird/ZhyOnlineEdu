"""M0 Spike · 方言能力矩阵（§16.2 目标 72 条 = 14 组 × 5 flavor + 2 扩展）。

用法：
    python spike/dialect/run_matrix.py --flavor sqlite --dsn sqlite:////tmp/zhy_spike.db
    python spike/dialect/run_matrix.py --flavor postgresql --dsn postgresql+psycopg://...
    python spike/dialect/run_matrix.py --flavor mysql --dsn mysql+pymysql://...
    # openGauss 5/6/7、Kingbase：介质缺失时 COMPILE_ONLY（仍验证三路编译不抛错）

状态：PASS / DIFF（符合文档预期的方言差异）/ FAIL / ERROR / COMPILE_ONLY
结果：spike/output/dialect-<flavor>.json
"""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Callable
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

from sqlalchemy import create_engine, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.pool import StaticPool

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
sys.path.insert(0, str(HERE))

from upsert_compiler import CAPS, ROUTE, compile_upsert  # noqa: E402

IS_PG = {"postgresql", "opengauss_5", "opengauss_6", "opengauss_7", "kingbase"}
IS_MYSQL = {"mysql"}


def pk_ddl(flavor: str) -> str:
    if flavor == "sqlite":
        return "INTEGER PRIMARY KEY AUTOINCREMENT"
    if flavor in IS_PG:
        return "BIGSERIAL PRIMARY KEY"
    return "BIGINT AUTO_INCREMENT PRIMARY KEY"


def json_ddl(flavor: str) -> str:
    return "TEXT" if flavor == "sqlite" else ("JSONB" if flavor in IS_PG else "JSON")


def bool_ddl(flavor: str) -> str:
    return "INTEGER" if flavor == "sqlite" else ("BOOLEAN" if flavor in IS_PG else "TINYINT(1)")


def greatest(f: str, col: str, param: str) -> str:
    """D 新增差异：SQLite 无 GREATEST，标量 max(a,b) 等价；MySQL/PG 族用 GREATEST。
    这是 F2 的关键发现——进度单调写入必须由兼容层抹平，业务 SQL 不能裸写 GREATEST。
    """
    return f"max({col}, :{param})" if f == "sqlite" else f"GREATEST({col}, :{param})"


def try_exec(conn, sql: str, params: dict[str, Any] | None = None) -> bool:
    """savepoint 包裹单条语句：唯一约束冲突只回滚该条（U5 批量不中断依赖此）。"""
    sp = conn.begin_nested()
    try:
        conn.execute(text(sql), params or {})
        sp.commit()
        return True
    except IntegrityError:
        sp.rollback()
        return False


CASES: list[tuple[str, str, Callable[..., tuple[bool, str]]]] = []


def case(cid: str, name: str):
    def deco(fn: Callable[..., tuple[bool, str]]):
        CASES.append((cid, name, fn))
        return fn

    return deco


@case("D1", "自增主键")
def d1(conn, f: str) -> tuple[bool, str]:
    conn.execute(text(f"CREATE TABLE g01 (id {pk_ddl(f)}, name VARCHAR(32))"))
    conn.execute(text("INSERT INTO g01 (name) VALUES ('a')"))
    got = conn.execute(text("SELECT id FROM g01")).scalar()
    return (got == 1, f"id={got}")


@case("D2a", "RETURNING 取 id")
def d2a(conn, f: str) -> tuple[bool, str]:
    conn.execute(text(f"CREATE TABLE g02 (id {pk_ddl(f)}, name VARCHAR(32))"))
    sp = conn.begin_nested()
    try:
        row = conn.execute(text("INSERT INTO g02 (name) VALUES ('x') RETURNING id")).first()
        sp.commit()
        return (row is not None, f"RETURNING 可用 id={row[0] if row else None}")
    except Exception as exc:  # noqa: BLE001 - 需记录方言原始异常名
        sp.rollback()
        return (False, f"RETURNING 不可用: {type(exc).__name__}")


@case("D3", "JSON 存取")
def d3(conn, f: str) -> tuple[bool, str]:
    conn.execute(text(f"CREATE TABLE g03 (id {pk_ddl(f)}, doc {json_ddl(f)})"))
    conn.execute(
        text("INSERT INTO g03 (doc) VALUES (:d)"), {"d": json.dumps({"a": 1, "b": [2, 3]})}
    )
    raw = conn.execute(text("SELECT doc FROM g03")).scalar()
    parsed = json.loads(raw) if isinstance(raw, str) else raw
    return (parsed == {"a": 1, "b": [2, 3]}, f"列类型={json_ddl(f)} 驱动返回={type(raw).__name__}")


@case("D4", "DDL 事务性（回滚后表是否残留）")
def d4(conn, f: str) -> tuple[bool, str]:
    tx = conn.begin()
    conn.execute(text(f"CREATE TABLE g04 (id {pk_ddl(f)})"))
    tx.rollback()
    if f == "sqlite":
        n = conn.execute(text("SELECT COUNT(*) FROM sqlite_master WHERE name='g04'")).scalar()
    else:
        n = conn.execute(
            text("SELECT COUNT(*) FROM information_schema.tables WHERE table_name='g04'")
        ).scalar()
    expected_gone = f not in {"mysql", "sqlite"}
    return (
        ((n or 0) == 0) == expected_gone,
        f"回滚后存在={bool(n)}；sqlite 因 pysqlite legacy 事务控制不在 DDL 前 BEGIN",
    )


@case("D5", "标识符长度 62")
def d5(conn, _f: str) -> tuple[bool, str]:
    name = "t_" + "x" * 60
    conn.execute(text(f"CREATE TABLE {name} (id INT)"))
    return (True, f"长度={len(name)} 创建成功")


@case("D7", "布尔存取")
def d7(conn, f: str) -> tuple[bool, str]:
    conn.execute(text(f"CREATE TABLE g07 (id {pk_ddl(f)}, flag {bool_ddl(f)})"))
    conn.execute(text("INSERT INTO g07 (flag) VALUES (:v)"), {"v": True})
    conn.execute(text("INSERT INTO g07 (flag) VALUES (:v)"), {"v": False})
    vals = [bool(v) for (v,) in conn.execute(text("SELECT flag FROM g07 ORDER BY id"))]
    return (vals == [True, False], f"读回={vals}")


@case("D8", "大小写折叠（未引号建表 + 小写访问）")
def d8(conn, f: str) -> tuple[bool, str]:
    conn.execute(text(f"CREATE TABLE G08Case (id {pk_ddl(f)})"))
    ok = try_exec(conn, "INSERT INTO g08case (id) VALUES (1)")
    return (ok, "小写可访问" if ok else "大小写敏感：小写访问失败")


@case("D10a", "UPSERT 七项专项")
def d10a(conn, f: str) -> tuple[bool, str]:
    caps = CAPS[f]
    natkey = ["uid", "lesson"]
    conn.execute(
        text(
            f"CREATE TABLE g10 (id {pk_ddl(f)}, uid BIGINT NOT NULL, lesson BIGINT NOT NULL, "
            "pos INT NOT NULL, version INT NOT NULL DEFAULT 1, UNIQUE (uid, lesson))"
        )
    )
    notes: list[str] = []
    ok = True

    def upsert(uid: int, pos: int, *, fallback: bool = False) -> dict[str, Any]:
        if fallback:
            found = conn.execute(
                text("SELECT id, version FROM g10 WHERE uid=:u AND lesson=1"), {"u": uid}
            ).first()
            if found:
                conn.execute(
                    text(
                        f"UPDATE g10 SET pos={greatest(f, 'pos', 'p')}, version=version+1 "
                        "WHERE uid=:u"
                    ),
                    {"u": uid, "p": pos},
                )
                return {"created": False, "pk": found[0], "version": found[1] + 1}
            conn.execute(
                text("INSERT INTO g10 (uid, lesson, pos, version) VALUES (:u,1,:p,1)"),
                {"u": uid, "p": pos},
            )
            row = conn.execute(
                text("SELECT id, version FROM g10 WHERE uid=:u AND lesson=1"), {"u": uid}
            ).first()
            return {"created": True, "pk": row[0] if row else None, "version": row[1] if row else 1}

        compiled = compile_upsert(
            f, "g10", {"uid": uid, "lesson": 1, "pos": pos, "version": 1}, natkey
        )
        if compiled.route == "pg" and caps["returning"]:
            row = conn.execute(text(compiled.sql), compiled.params).first()
            return {"created": True, "pk": row[0], "version": row[1]}
        conn.execute(text(compiled.sql), compiled.params)
        row = conn.execute(
            text("SELECT id, version FROM g10 WHERE uid=:u AND lesson=1"), {"u": uid}
        ).first()
        return {"created": row[1] == 1, "pk": row[0], "version": row[1]}

    r1 = upsert(1, 100)
    ok &= r1["pk"] is not None
    notes.append(f"U1新建 pk={r1['pk']}")
    r2 = upsert(1, 300)
    ok &= r2["pk"] == r1["pk"] and r2["version"] == 2
    notes.append(f"U2更新 pk不变={r2['pk'] == r1['pk']} v={r2['version']}")
    r3 = upsert(2, 50, fallback=True)
    ok &= r3["pk"] is not None
    notes.append(f"U3兜底回读 pk={r3['pk']}")

    conn.execute(text(f"CREATE TABLE g10c (id {pk_ddl(f)}, code VARCHAR(16) UNIQUE, v INT)"))
    conn.execute(text("INSERT INTO g10c (code, v) VALUES ('dup', 1)"))
    blocked = not try_exec(conn, "INSERT INTO g10c (code, v) VALUES ('dup', 2)")
    ok &= blocked
    notes.append(f"U4冲突可捕获={blocked}")

    conn.execute(text(f"CREATE TABLE g10d (id {pk_ddl(f)}, uid BIGINT, pos INT)"))
    ok_cnt = sum(
        try_exec(conn, "INSERT INTO g10d (uid, pos) VALUES (:u, :p)", {"u": u, "p": p})
        for u, p in ((10, 5), (11, 7), (12, 9))
    )
    failed_cnt = 0 if try_exec(conn, "INSERT INTO g10d (uid, pos) VALUES (13, 11)") else 1
    ok &= ok_cnt == 3 and failed_cnt == 0
    notes.append(f"U5批量 ok={ok_cnt}")

    compiled = compile_upsert(f, "g10", {"uid": 1, "lesson": 1, "pos": 400, "version": 1}, natkey)
    head, _, tail = compiled.sql.partition("DO UPDATE")
    ok &= "id =" not in head and "id =" not in tail and "uid =" not in tail
    notes.append("U6不更新id/uid")

    before = conn.execute(text("SELECT pos FROM g10 WHERE uid=1")).scalar()
    conn.execute(text(f"UPDATE g10 SET pos={greatest(f, 'pos', 'p')} WHERE uid=1"), {"p": 300})
    after = conn.execute(text("SELECT pos FROM g10 WHERE uid=1")).scalar()
    ok &= after == before
    notes.append(f"U7重复幂等 pos={after}")
    return (ok, " | ".join(notes))


@case("D12", "乐观锁 version")
def d12(conn, f: str) -> tuple[bool, str]:
    conn.execute(text(f"CREATE TABLE g12 (id {pk_ddl(f)}, v INT NOT NULL DEFAULT 1)"))
    conn.execute(text("INSERT INTO g12 (v) VALUES (1)"))
    hit = conn.execute(text("UPDATE g12 SET v=v+1 WHERE id=1 AND v=1")).rowcount
    stale = conn.execute(text("UPDATE g12 SET v=v+1 WHERE id=1 AND v=1")).rowcount
    return (hit == 1 and stale == 0, f"首次={hit} 陈旧={stale}")


@case("D13", "UTC 时间存取")
def d13(conn, f: str) -> tuple[bool, str]:
    conn.execute(text(f"CREATE TABLE g13 (id {pk_ddl(f)}, at TIMESTAMP)"))
    conn.execute(
        text("INSERT INTO g13 (at) VALUES (:t)"),
        {"t": datetime(2026, 10, 28, 12, 0, 0, tzinfo=UTC)},
    )
    raw = conn.execute(text("SELECT at FROM g13")).scalar()
    text_val = (
        raw.replace(tzinfo=None).isoformat(sep=" ") if isinstance(raw, datetime) else str(raw)
    )
    return (text_val.startswith("2026-10-28 12:00:00"), f"读回={text_val[:19]}")


@case("D14", "金额精度 DECIMAL(12,2)")
def d14(conn, f: str) -> tuple[bool, str]:
    conn.execute(text(f"CREATE TABLE g14 (id {pk_ddl(f)}, amt DECIMAL(12,2))"))
    # 发现：裸 text() 绑定 Decimal 在 sqlite 驱动下抛 ProgrammingError →
    # 生产必须用 SQLAlchemy Numeric 类型装饰器（core.db.types 的 Money），不能裸绑定。
    bind = str(Decimal("1234.56")) if f == "sqlite" else Decimal("1234.56")
    conn.execute(text("INSERT INTO g14 (amt) VALUES (:a)"), {"a": bind})
    raw = conn.execute(text("SELECT amt FROM g14")).scalar()
    return (Decimal(str(raw)) == Decimal("1234.56"), f"读回={raw}")


@case("D15", "空串 vs NULL（COALESCE 判空）")
def d15(conn, f: str) -> tuple[bool, str]:
    conn.execute(text(f"CREATE TABLE g15 (id {pk_ddl(f)}, s VARCHAR(16))"))
    conn.execute(text("INSERT INTO g15 (s) VALUES (''), (NULL)"))
    n = conn.execute(text("SELECT COUNT(*) FROM g15 WHERE COALESCE(s,'')=''")).scalar()
    return (n == 2, f"命中={n}")


@case("D16", "分页 LIMIT/OFFSET")
def d16(conn, f: str) -> tuple[bool, str]:
    conn.execute(text(f"CREATE TABLE g16 (id {pk_ddl(f)}, name VARCHAR(8))"))
    for i in range(5):
        conn.execute(text("INSERT INTO g16 (name) VALUES (:n)"), {"n": f"n{i}"})
    ids = [r[0] for r in conn.execute(text("SELECT id FROM g16 ORDER BY id LIMIT 2 OFFSET 2"))]
    return (ids == [3, 4], f"第3~4行={ids}")


@case("D19a", "软删复合唯一(uid, delete_seq)")
def d19a(conn, f: str) -> tuple[bool, str]:
    conn.execute(
        text(
            f"CREATE TABLE g19 (id {pk_ddl(f)}, uid BIGINT NOT NULL, "
            "delete_seq BIGINT NOT NULL DEFAULT 0, UNIQUE (uid, delete_seq))"
        )
    )
    conn.execute(text("INSERT INTO g19 (uid, delete_seq) VALUES (1, 0)"))
    conn.execute(text("INSERT INTO g19 (uid, delete_seq) VALUES (1, 1)"))
    blocked = not try_exec(conn, "INSERT INTO g19 (uid, delete_seq) VALUES (1, 0)")
    rows = conn.execute(text("SELECT COUNT(*) FROM g19 WHERE uid=1")).scalar()
    return (rows == 2 and blocked, f"两 delete_seq 共存={rows == 2} 同值冲突={blocked}")


@case("X1", "批量插入 id 可见性")
def x1(conn, f: str) -> tuple[bool, str]:
    conn.execute(text(f"CREATE TABLE g21 (id {pk_ddl(f)}, name VARCHAR(8))"))
    sp = conn.begin_nested()
    try:
        rows = conn.execute(
            text("INSERT INTO g21 (name) VALUES ('a'),('b'),('c') RETURNING id")
        ).all()
        sp.commit()
        return (len(rows) == 3, f"批量 RETURNING={len(rows)}")
    except Exception:  # noqa: BLE001
        sp.rollback()
        conn.execute(text("INSERT INTO g21 (name) VALUES ('a'),('b'),('c')"))
        return (False, "批量 RETURNING 不可用 → 需两阶段回查")


@case("X2", "四步式列变更（加列→回填→改名→删旧）")
def x2(conn, f: str) -> tuple[bool, str]:
    conn.execute(text(f"CREATE TABLE g22 (id {pk_ddl(f)}, name VARCHAR(8))"))
    conn.execute(text("INSERT INTO g22 (name) VALUES ('old')"))
    conn.execute(text("ALTER TABLE g22 ADD COLUMN name_v2 VARCHAR(32)"))
    conn.execute(text("UPDATE g22 SET name_v2 = name"))
    conn.execute(text("ALTER TABLE g22 DROP COLUMN name"))
    conn.execute(text("ALTER TABLE g22 RENAME COLUMN name_v2 TO name"))
    val = conn.execute(text("SELECT name FROM g22")).scalar()
    return (val == "old", f"四步后值={val}")


# 该 flavor 上"失败才符合文档预期"的用例（D2a/D8/X1）
EXPECT_FAIL: dict[str, set[str]] = {
    "sqlite": set(),
    "postgresql": set(),
    "kingbase": set(),
    "opengauss_7": set(),
    "mysql": {"D2a", "D8", "X1"},
    "opengauss_5": {"D2a", "D8", "X1"},
    "opengauss_6": {"D2a", "D8", "X1"},
}


def run(flavor: str, dsn: str | None) -> dict[str, Any]:
    compile_only = dsn is None
    target = flavor
    engine = None
    if not compile_only:
        target = "postgresql" if flavor in IS_PG else flavor
        engine = (
            create_engine(dsn, poolclass=StaticPool, connect_args={"check_same_thread": False})
            if dsn.startswith("sqlite")
            else create_engine(dsn, pool_pre_ping=True)
        )

    results: list[dict[str, str]] = []
    for cid, name, fn in CASES:
        if compile_only:
            try:
                compile_upsert(
                    flavor,
                    "spike_probe",
                    {"uid": 1, "lesson": 1, "pos": 1, "version": 1},
                    ["uid", "lesson"],
                )
                results.append(
                    {
                        "case": cid,
                        "name": name,
                        "status": "COMPILE_ONLY",
                        "detail": f"route={ROUTE[flavor]}",
                    }
                )
            except Exception as exc:  # noqa: BLE001
                results.append(
                    {"case": cid, "name": name, "status": "FAIL", "detail": f"编译失败: {exc}"}
                )
            continue
        try:
            with engine.connect() as conn:  # type: ignore[union-attr]
                passed, detail = fn(conn, target)
                conn.rollback()
        except Exception as exc:  # noqa: BLE001
            passed, detail = False, f"{type(exc).__name__}: {exc}"[:200]
        if passed:
            status = "PASS"
        elif cid in EXPECT_FAIL.get(flavor, set()):
            status = "DIFF"
        else:
            status = "FAIL"
        results.append({"case": cid, "name": name, "status": status, "detail": detail})

    summary: dict[str, int] = {}
    for r in results:
        summary[r["status"]] = summary.get(r["status"], 0) + 1
    return {
        "flavor": flavor,
        "caps": CAPS[flavor],
        "route": ROUTE[flavor],
        "mode": "COMPILE_ONLY" if compile_only else "RUNTIME",
        "results": results,
        "summary": summary,
    }


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--flavor", required=True, choices=sorted(CAPS))
    ap.add_argument("--dsn", default=None, help="省略时进入 COMPILE_ONLY 模式")
    ap.add_argument("--out", default=str(ROOT / "spike" / "output"))
    args = ap.parse_args()

    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)
    report = run(args.flavor, args.dsn)
    path = out_dir / f"dialect-{args.flavor}.json"
    path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")

    print(
        f"{args.flavor:14} mode={report['mode']:13} route={report['route']:6} "
        f"summary={report['summary']}"
    )
    for r in report["results"]:
        if r["status"] not in {"PASS", "COMPILE_ONLY"}:
            print(f"  {r['case']:5} {r['status']:6} {r['detail']}")


if __name__ == "__main__":
    main()
