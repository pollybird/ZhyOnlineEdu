"""账号 / 会员 / 订单过期 worker（§10.5 · §10.6 · M2.7 / M2.12）。

compose: entrypoint ["python","-m","zhyonlineedu.workers.expire"]
- 每日 `users.expire_hour`（UTC，默认 02:00）执行一次；
- 账号：条件更新 `status='active' AND expire_at <= now` → `expired`，并 `token_version+1`、
  吊销全部会话、写安全日志；
- 会员订阅：`status='active' AND expire_at <= now` → `expired`（到期后旧票据 40204）；
- 订单：`created/paying AND expire_at <= now` → `expired`，不倒退已支付订单；
- 批大小 `users.expire_batch_size`，循环处理到无过期账号；
- `--once` 供联调/CI 立即执行一轮。
"""

from __future__ import annotations

import argparse
import logging
import time
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from typing import Any, cast

from sqlalchemy import select, update
from sqlalchemy.engine import CursorResult
from sqlalchemy.orm import Session

from zhyonlineedu.core.config import Config, load_config
from zhyonlineedu.core.db.engine import ensure_database
from zhyonlineedu.core.db.models.account import AccountSession, AccountUser, SysLogSecurity
from zhyonlineedu.core.db.query import not_deleted
from zhyonlineedu.core.logging import configure_logging
from zhyonlineedu.modules.commerce.service import CommerceService

logger = logging.getLogger(__name__)


def expire_once(
    config: Config, *, now: datetime | None = None, batch_size: int | None = None
) -> int:
    """把已到期账号置 expired 并吊销会话；返回处理行数。"""
    moment = now or datetime.now(UTC)
    size = batch_size or config.get_int("users.expire_batch_size")
    db = ensure_database(config)
    total = 0
    with Session(db.engine) as session:
        while True:
            ids = (
                session.execute(
                    select(AccountUser.id)
                    .where(
                        not_deleted(AccountUser),
                        AccountUser.status == "active",
                        AccountUser.expire_at.is_not(None),
                        AccountUser.expire_at <= moment,
                    )
                    .limit(size)
                )
                .scalars()
                .all()
            )
            if not ids:
                break
            session.execute(
                update(AccountUser)
                .where(
                    AccountUser.id.in_(list(ids)),
                    AccountUser.status == "active",
                    not_deleted(AccountUser),
                )
                .values(
                    status="expired",
                    token_version=AccountUser.token_version + 1,
                    version=AccountUser.version + 1,
                    updated_at=moment,
                )
            )
            revoked = cast(
                CursorResult[Any],
                session.execute(
                    update(AccountSession)
                    .where(
                        AccountSession.user_id.in_(list(ids)),
                        AccountSession.revoked_at.is_(None),
                    )
                    .values(
                        revoked_at=moment,
                        revoked_by="system",
                        revoke_reason="expired",
                    )
                ),
            )
            session.add(
                SysLogSecurity(
                    event="USER_EXPIRED",
                    user_id=None,
                    detail=f"source=worker;users={len(ids)};sessions={revoked.rowcount}",
                    created_at=moment,
                )
            )
            session.commit()
            total += len(ids)
            logger.info("账号过期批次 user_rows=%s sessions=%s", len(ids), revoked.rowcount)

    commerce = CommerceService(config, db=db)
    expired_orders = commerce.expire_orders(now=moment, batch_size=size)
    expired_members = commerce.expire_memberships(now=moment, batch_size=size)
    logger.info("会员/订单过期 orders=%s subscriptions=%s", expired_orders, expired_members)
    return total


def _next_run(now: datetime, hour: int) -> datetime:
    target = now.replace(hour=hour, minute=0, second=0, microsecond=0)
    if target <= now:
        target += timedelta(days=1)
    return target


def run_expire(config: Config, *, once: bool = False, hour: int | None = None) -> int:
    expire_hour = config.get_int("users.expire_hour") if hour is None else hour
    if once:
        return expire_once(config)
    while True:
        now = datetime.now(UTC)
        wait = (_next_run(now, expire_hour) - now).total_seconds()
        logger.info("下次账号过期任务在 %.0f 秒后（UTC %02d:00）", wait, expire_hour)
        time.sleep(max(1, wait))
        try:
            expire_once(config)
        except Exception:  # pragma: no cover - 保持 worker 存活
            logger.exception("账号过期任务失败，下一轮重试")


def main(argv: Sequence[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="账号 / 会员过期 worker")
    parser.add_argument("--once", action="store_true", help="立即执行一轮后退出（联调/CI）")
    args = parser.parse_args(argv)

    config = load_config()
    configure_logging(config.get_str("env"))
    logger.info("账号过期 worker start hour=%s", config.get_int("users.expire_hour"))
    try:
        run_expire(config, once=args.once)
    except KeyboardInterrupt:  # pragma: no cover - 信号退出
        logger.info("账号过期 worker stopped by signal")


if __name__ == "__main__":
    main()
