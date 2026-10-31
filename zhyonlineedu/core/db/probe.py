"""安装期能力探测（§5.6 · F1）。

流程：连接测试 → 建 sys_probe 临时表 → 逐项冒烟（RETURNING / UPSERT 两路 /
厂商参数 `SHOW`）→ 写入 sys_compat_flags → 拆表。
运行时禁止逐请求探测；POST /system/probe/rerun 允许超管重跑（§15.2）。

探测结果一经写入即冻结为能力位（§5.2 红线：运行时只读 sys_compat_flags）。
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import cast

from sqlalchemy import (
    Column,
    Engine,
    Integer,
    MetaData,
    String,
    Table,
    inspect,
    select,
    text,
)
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from zhyonlineedu.core.capability import ProbeResult, ProbeState
from zhyonlineedu.core.config import Config

from . import compat
from .compat import Cap, Flavor, Route
from .models.system import SysCompatFlags
from .upsert import compile_upsert

logger = logging.getLogger(__name__)

PROBE_TABLE = "zhy_probe_caps"


def probe_table_name(pid: int | None = None) -> str:
    """探测表按进程隔离（M2.16 多 worker 启动实测）。

    gunicorn 多 worker 并行启动时若共用 `zhy_probe_caps`，A 进程探测完成后
    drop 会打断 B 进程正在执行的 UPSERT 冒烟，导致能力位被误判为不可用。
    每进程一张表，各自创建与清理，互不影响；安装期只读 `sys_compat_flags`。
    """
    return f"{PROBE_TABLE}_{os.getpid() if pid is None else pid}"


SYS_COMPAT_FLAGS: Table = cast(Table, SysCompatFlags.__table__)

# 厂商参数期望值见 compat.expected_settings（§5.5 / D35）。


def _probe_table() -> Table:
    return Table(
        probe_table_name(),
        MetaData(),
        Column("id", Integer, primary_key=True),
        Column("probe_key", String(64), nullable=False, unique=True),
        Column("version", Integer, nullable=False, default=1),
    )


def _now() -> str:
    return datetime.now(UTC).replace(tzinfo=None).isoformat(timespec="seconds")


def load_flags(engine: Engine) -> dict[str, bool]:
    """读取安装期固化的能力位；表不存在（未安装）返回空。"""
    if not inspect(engine).has_table(SYS_COMPAT_FLAGS.name):
        return {}
    with engine.connect() as conn:
        rows = conn.execute(select(SYS_COMPAT_FLAGS.c.cap, SYS_COMPAT_FLAGS.c.value)).all()
    return {str(cap): bool(value) for cap, value in rows}


def persist_flags(engine: Engine, flavor: Flavor, caps: dict[str, bool]) -> None:
    """按 flavor 走统一 upsert 写入 sys_compat_flags（安装期唯一写入口）。"""
    SYS_COMPAT_FLAGS.create(engine, checkfirst=True)
    probed_at = _now()
    with Session(engine) as session:
        for cap_name, value in caps.items():
            compiled = compile_upsert(
                flavor,
                SYS_COMPAT_FLAGS.name,
                {
                    "cap": cap_name,
                    "flavor": flavor.value,
                    "value": int(value),
                    "probed_at": probed_at,
                    "detail": "",
                },
                ["cap"],
                ["flavor", "value", "probed_at", "detail"],
                pk_column="cap",
                version_column=None,
            )
            session.execute(text(compiled.sql), compiled.params).close()
        session.commit()


def _probe_upsert_route(engine: Engine, flavor: Flavor, route: Route, *, require_row: bool) -> bool:
    try:
        with engine.connect() as conn:
            table = _probe_table()
            table.create(conn, checkfirst=True)
            compiled = compile_upsert(
                flavor,
                probe_table_name(),
                {"probe_key": "cap", "version": 1},
                ["probe_key"],
                route=route,
            )
            result = conn.execute(text(compiled.sql), compiled.params)
            if require_row:
                return result.first() is not None
            conn.rollback()
            return True
    except SQLAlchemyError as exc:
        logger.info("能力探测失败 route=%s flavor=%s: %s", route, flavor, type(exc).__name__)
        return False


def probe_capabilities(engine: Engine, flavor: Flavor) -> dict[str, bool]:
    """真实冒烟 UPSERT 两类语法；其余能力位以文档基线为准（介质到位后按矩阵补测）。"""
    spec = compat.flavor_spec(flavor)
    caps: dict[str, bool] = {cap.value: spec.caps[cap] for cap in Cap}

    try:
        with engine.connect() as conn:
            _probe_table().create(conn, checkfirst=True)
    except SQLAlchemyError:
        return caps

    try:
        pg_ok = _probe_upsert_route(engine, flavor, "pg", require_row=True)
        on_dup_ok = _probe_upsert_route(engine, flavor, "mysql", require_row=False)
        caps[Cap.RETURNING.value] = pg_ok
        caps[Cap.UPSERT_CONFLICT_CLAUSE.value] = pg_ok
        caps[Cap.UPSERT_ON_DUPLICATE.value] = on_dup_ok
    finally:
        # DDL 事务性因方言而异（SQLite/MySQL 自动提交），清理必须显式提交。
        with engine.begin() as conn:
            _probe_table().drop(conn, checkfirst=True)
    return caps


_UTF8_CHARSETS = frozenset({"utf8", "utf8mb4", "utf-8", "unicode"})


def _charset_blocker(engine: Engine, flavor: Flavor) -> str | None:
    """§8.5 blocker 4：建库字符集必须 UTF8（SQLite 天然 UTF-8，跳过）。"""
    if flavor is Flavor.SQLITE:
        return None
    sql = "SELECT @@character_set_database" if flavor is Flavor.MYSQL else "SHOW server_encoding"
    try:
        with engine.connect() as conn:
            value = conn.execute(text(sql)).scalar()
    except SQLAlchemyError as exc:
        return f"{flavor.value} 无法确认建库字符集（{sql}）：{type(exc).__name__}；要求 UTF8"
    name = str(value).strip().lower()
    if name not in _UTF8_CHARSETS:
        return f"{flavor.value} 建库字符集为 {value!r}，要求 UTF8/utf8mb4（非 UTF8 禁止安装）"
    return None


def _check_settings(engine: Engine, flavor: Flavor) -> list[str]:
    expected = compat.expected_settings(flavor)
    problems: list[str] = []
    for setting in compat.flavor_spec(flavor).required_settings:
        try:
            with engine.connect() as conn:
                value = conn.execute(text(f"SHOW {setting}")).scalar()
        except SQLAlchemyError as exc:
            problems.append(
                f"{flavor.value} 无法读取 {setting}: {type(exc).__name__}；"
                + compat.driver_hint(flavor)
            )
            continue
        want = expected.get(setting)
        if want is not None and str(value).strip().lower() != want:
            problems.append(f"{flavor.value} 要求 {setting}={want}，实际 {value!r}（不符禁止安装）")
    return problems


def run_probe(
    engine: Engine, flavor: Flavor | str | None = None, *, persist: bool = True
) -> ProbeResult:
    """执行全量探测并固化能力位。blockers 非空时安装向导禁止"开始安装"。"""
    target = Flavor(flavor) if flavor is not None else compat.detect_flavor(str(engine.url))
    try:
        with engine.connect() as conn:
            conn.execute(text("SELECT 1"))
    except SQLAlchemyError as exc:
        blocker = compat.driver_hint(target, f"{type(exc).__name__}: {exc}")
        return ProbeResult(state=ProbeState.FAILED, flavor=target.value, blockers=(blocker,))

    blockers = _check_settings(engine, target)
    charset = _charset_blocker(engine, target)
    if charset is not None:
        blockers.append(charset)
    caps = probe_capabilities(engine, target)
    if not caps[Cap.UPSERT_CONFLICT_CLAUSE.value] and not caps[Cap.UPSERT_ON_DUPLICATE.value]:
        blockers.append(f"{target.value} 两路 UPSERT 均不可用，适配层无法保证幂等写入")
    if target is Flavor.SQLITE and not caps[Cap.RETURNING.value]:
        blockers.append("SQLite < 3.35 不支持 MATCH 行级返回，请升级到 3.35+（WAL 同要求）")

    flags = {cap.value: bool(caps[cap.value]) for cap in Cap}
    if persist and not blockers:
        try:
            persist_flags(engine, target, flags)
        except SQLAlchemyError as exc:
            blockers.append(f"sys_compat_flags 写入失败：{type(exc).__name__}: {exc}")

    state = ProbeState.FAILED if blockers else ProbeState.READY
    if not blockers:
        compat.configure(url=str(engine.url), flavor=target, caps=flags, source="probe")
    return ProbeResult(
        state=state,
        flavor=target.value,
        capabilities=flags,
        blockers=tuple(blockers),
    )


def probe_all() -> dict[str, bool]:
    """执行全量探测，返回 {cap: bool}（兼容 M1.1 占位契约）。"""
    from .engine import database

    db = database()
    return dict(run_probe(db.engine, db.flavor).capabilities)


@dataclass(frozen=True, slots=True)
class InstallProbe:
    """应用工厂 / 安装向导注入的能力探测实现（§4.3 装配门禁）。"""

    config: Config
    persist: bool = True

    def probe(self) -> ProbeResult:
        from .engine import init_database

        try:
            db = init_database(self.config)
            return run_probe(db.engine, db.flavor, persist=self.persist)
        except Exception as exc:  # noqa: BLE001 - 装配失败必须落为可读 blockers 而非崩溃
            return ProbeResult(
                state=ProbeState.FAILED,
                blockers=(f"能力探测装配失败：{type(exc).__name__}: {exc}",),
            )
