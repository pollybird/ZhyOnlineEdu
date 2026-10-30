"""进度落库 worker（§12.5 单写者 · F2 · M2.5）。

compose: entrypoint ["python","-m","zhyonlineedu.workers.progress_flush"]
启动先 `replay_unflushed`（崩溃补齐），随后按 `progress.flush_interval_sec` 周期落库。
`--once` 供联调/CI；写者身份 `host:pid` 由租约保证互斥。
"""

from __future__ import annotations

import argparse
import logging
import time

from zhyonlineedu.core.config import load_config
from zhyonlineedu.core.logging import configure_logging
from zhyonlineedu.core.progress.flush import ProgressFlush, replay_unflushed

logger = logging.getLogger(__name__)


def run_flush(flush: ProgressFlush, *, once: bool = False, interval_sec: int | None = None) -> int:
    interval = (
        flush.config.get_int("progress.flush_interval_sec")
        if interval_sec is None
        else interval_sec
    )
    recovered = replay_unflushed(flush, rounds=3)
    if recovered:
        logger.info("progress flush 启动补齐 %s 行", recovered)
    if once:
        return recovered

    total = recovered
    while True:
        try:
            total += flush.flush_once()
        except Exception:  # pragma: no cover - 保持 worker 存活
            logger.exception("progress flush 失败，下一轮重试")
        time.sleep(max(1, interval))


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="学习进度落库 worker（单写者）")
    parser.add_argument("--once", action="store_true", help="只补齐一轮后退出（联调/CI）")
    args = parser.parse_args(argv)

    config = load_config()
    configure_logging(config.get_str("env"))
    flush = ProgressFlush(config)
    logger.info("progress flush worker start writer=%s", flush.writer)
    try:
        run_flush(flush, once=args.once)
    except KeyboardInterrupt:  # pragma: no cover - 信号退出
        logger.info("progress flush worker stopped by signal")


if __name__ == "__main__":
    main()
