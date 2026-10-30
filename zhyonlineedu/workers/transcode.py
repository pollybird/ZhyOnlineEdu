"""转码 worker 入口（§4.1 · 沙箱化，F7 · M2.4）。

compose: entrypoint ["python","-m","zhyonlineedu.workers.transcode"]
安全约束：read_only 容器 + seccomp 策略 + tmpfs /tmp/transcode + cgroup 限额（§24.3）。
FFmpeg 只能经 `core.media.runtime.SandboxRunner` 调用，命令由 `RenderSpec` 枚举派生。

运行口径：
- 启动先 `recover_stale()` 回收崩溃 worker 遗留的 running 任务（ENC-1008）；
- 每轮最多消费 `transcode.queue_batch` 个任务（单 worker 串行），空队列后休眠
  `transcode.worker_poll_sec`；
- 单个任务异常不让进程退出（DB 抖动由下一轮重试），仅记录日志；
- `--once` 供联调/CI 使用：处理至多一轮后退出。
"""

from __future__ import annotations

import argparse
import logging
import time

from zhyonlineedu.core.config import load_config
from zhyonlineedu.core.logging import configure_logging
from zhyonlineedu.modules.media.transcode_service import TranscodeService

logger = logging.getLogger(__name__)


def run_worker(
    service: TranscodeService,
    *,
    once: bool = False,
    poll_sec: int | None = None,
    max_rounds: int | None = None,
) -> int:
    """主循环；返回已处理任务数（`--once` 便于断言）。"""
    poll = service.config.get_int("transcode.worker_poll_sec") if poll_sec is None else poll_sec
    batch = max(1, service.config.get_int("transcode.queue_batch"))
    try:
        service.recover_stale()
    except Exception:  # pragma: no cover - 依赖 DB，启动期只记录
        logger.exception("transcode recover_stale failed; continue")

    processed = 0
    rounds = 0
    while True:
        rounds += 1
        for _ in range(batch):
            try:
                job_id = service.run_next()
            except Exception:  # pragma: no cover - 保持 worker 存活
                logger.exception("transcode run_next failed")
                job_id = None
            if job_id is None:
                break
            processed += 1
        if once or (max_rounds is not None and rounds >= max_rounds):
            return processed
        time.sleep(poll)


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="转码 worker（沙箱内执行 ffmpeg）")
    parser.add_argument("--once", action="store_true", help="只消费一轮后退出（联调/CI）")
    args = parser.parse_args(argv)

    config = load_config()
    configure_logging(config.get_str("env"))
    service = TranscodeService(config)
    logger.info(
        "transcode worker start sandbox=%s paused=%s",
        config.get_str("transcode.sandbox"),
        config.get_bool("transcode.paused"),
    )
    try:
        run_worker(service, once=args.once)
    except KeyboardInterrupt:  # pragma: no cover - 信号退出
        logger.info("transcode worker stopped by signal")


if __name__ == "__main__":
    main()
