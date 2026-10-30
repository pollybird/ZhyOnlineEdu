#!/usr/bin/env python3
"""运维入口（M1.3）：Alembic 迁移与回滚。

用法：
    python manage.py db-migrate                     # upgrade head
    python manage.py db-migrate --revision <rev>    # upgrade 到指定版本
    python manage.py db-migrate --rollback-to-last-good

回滚前提（§5.6）：迁移前 `sys_install_state.last_good_rev` 已记录。
MySQL DDL 不可事务：upgrade 中途失败时 downgrade 未必能还原，必须从备份恢复。
"""

from __future__ import annotations

import argparse
import sys

from alembic import command
from alembic.config import Config as AlembicConfig
from sqlalchemy import Engine, inspect
from sqlalchemy.orm import Session
from zhyonlineedu.core.config import Config, load_config
from zhyonlineedu.core.db import compat
from zhyonlineedu.core.db.engine import create_db_engine
from zhyonlineedu.core.db.migration import alembic_config_for, current_revision
from zhyonlineedu.core.db.models.system import SysInstallState, utcnow
from zhyonlineedu.modules.media.upload_service import UploadService

STATE_TABLE = SysInstallState.__tablename__
STATE_ID = 1


def _alembic_config(url: str, flavor: compat.Flavor) -> AlembicConfig:
    return alembic_config_for(url, flavor)


def read_last_good_rev(engine: Engine) -> str | None:
    """未迁移/未记录时返回 None（禁止猜测回滚目标）。"""
    if not inspect(engine).has_table(STATE_TABLE):
        return None
    with Session(engine) as session:
        row = session.get(SysInstallState, STATE_ID)
        return row.last_good_rev if row is not None else None


def write_state(engine: Engine, *, schema_rev: str, last_good_rev: str) -> None:
    with Session(engine) as session:
        row = session.get(SysInstallState, STATE_ID)
        if row is None:
            row = SysInstallState(
                id=STATE_ID,
                is_installed=False,
                installed_at=None,
                version=1,
                updated_at=utcnow(),
            )
            session.add(row)
        row.schema_rev = schema_rev
        row.last_good_rev = last_good_rev
        row.updated_at = utcnow()
        session.commit()


def migrate(
    *,
    revision: str = "head",
    rollback: bool = False,
    config: Config | None = None,
) -> int:
    """执行迁移；返回进程退出码（0 成功，1 失败，2 用法/前置条件不满足）。"""
    cfg = load_config() if config is None else config
    url = cfg.get_str("db.url")
    explicit = cfg.get_str("db.flavor") or None
    flavor = compat.Flavor(explicit) if explicit else compat.detect_flavor(url)
    compat.configure(url=url, flavor=flavor, source="manage")
    alembic_cfg = _alembic_config(url, flavor)
    engine = create_db_engine(url, flavor)
    try:
        if rollback:
            return _rollback(engine, alembic_cfg, flavor)
        return _upgrade(engine, alembic_cfg, revision, flavor)
    finally:
        engine.dispose()


def _upgrade(
    engine: Engine, alembic_cfg: AlembicConfig, revision: str, flavor: compat.Flavor
) -> int:
    try:
        command.upgrade(alembic_cfg, revision)
    except Exception as exc:  # noqa: BLE001 - CLI 必须给出可读失败与回滚提示
        print(f"[db-migrate] 升级失败：{type(exc).__name__}: {exc}", file=sys.stderr)
        print(
            "[db-migrate] 回滚：python manage.py db-migrate --rollback-to-last-good；"
            "MySQL DDL 非事务，若回滚失败请从备份恢复。",
            file=sys.stderr,
        )
        return 1
    new_rev = current_revision(engine)
    if new_rev is None:
        print("[db-migrate] 升级后未读到 revision，状态未更新", file=sys.stderr)
        return 1
    write_state(engine, schema_rev=new_rev, last_good_rev=new_rev)
    print(f"[db-migrate] {flavor.value} 已到 {new_rev}（last_good_rev={new_rev}）")
    return 0


def _rollback(engine: Engine, alembic_cfg: AlembicConfig, flavor: compat.Flavor) -> int:
    target = read_last_good_rev(engine)
    if not target:
        print(
            "[db-migrate] 无 last_good_rev 记录，禁止自动回滚；请从备份恢复"
            "（MySQL DDL 非事务，downgrade 可能留下半成品）。",
            file=sys.stderr,
        )
        return 2
    current = current_revision(engine)
    if current == target:
        print(f"[db-migrate] 当前已在 last_good_rev={target}，无需回滚")
        return 0
    if flavor is compat.Flavor.MYSQL:
        print(
            "[db-migrate] WARNING：MySQL DDL 自动提交，downgrade 失败时必须从备份恢复。",
            file=sys.stderr,
        )
    try:
        command.downgrade(alembic_cfg, target)
    except Exception as exc:  # noqa: BLE001 - CLI 失败给出可读提示
        print(f"[db-migrate] 回滚失败：{type(exc).__name__}: {exc}", file=sys.stderr)
        print("[db-migrate] 请立即从备份恢复（MySQL DDL 非事务）。", file=sys.stderr)
        return 1
    write_state(engine, schema_rev=target, last_good_rev=target)
    print(f"[db-migrate] {flavor.value} 已回滚到 {target}")
    return 0


def reap_tmp(config: Config | None = None) -> int:
    """tmp-reaper（§10.3.1）：清理过期上传会话与合并后 10 分钟未消费的临时文件。"""
    cfg = load_config() if config is None else config
    try:
        removed = UploadService(cfg).reap()
    except Exception as exc:  # noqa: BLE001 - CLI 必须给出可读失败
        print(f"[tmp-reaper] 清理失败：{type(exc).__name__}: {exc}", file=sys.stderr)
        return 1
    print(f"[tmp-reaper] 已清理 {removed} 个过期上传会话")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="manage.py", description="钟毓在线教育系统运维入口")
    sub = parser.add_subparsers(dest="command", required=True)
    migrate_parser = sub.add_parser("db-migrate", help="Alembic 升级 / 回滚")
    migrate_parser.add_argument("--revision", default="head", help="目标 revision（默认 head）")
    migrate_parser.add_argument(
        "--rollback-to-last-good",
        action="store_true",
        help="回滚到 sys_install_state.last_good_rev",
    )
    sub.add_parser("tmp-reaper", help="清理过期上传会话与未消费临时文件（§10.3.1）")
    args = parser.parse_args(argv)

    if args.command == "db-migrate":
        return migrate(revision=str(args.revision), rollback=bool(args.rollback_to_last_good))
    if args.command == "tmp-reaper":
        return reap_tmp()
    parser.error(f"未知命令：{args.command}")
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
