"""进度落库 worker（§12.5 单写者 · F2 · M2.5 / M3.7 事件派发）。

compose: entrypoint ["python","-m","zhyonlineedu.workers.progress_flush"]
启动先 `replay_unflushed`（崩溃补齐），随后按 `progress.flush_interval_sec` 周期落库。
`--once` 供联调/CI；写者身份 `host:pid` 由租约保证互斥。

**事件派发（M3.7）**：`lesson.finished` 由 flush 在「未完成 → 已完成」跃迁后发出，
而订阅关系属于插件进程内状态，因此本 worker 以**最小宿主 app 上下文**启动：
只注册 `zhy_config` / `zhy_events` 并挂载已启用插件（不装业务蓝图、不做能力探测）。
这样插件订阅在 worker 内同样生效，且插件 handler 永远不占用 HTTP 请求线程。
"""

from __future__ import annotations

import argparse
import logging
import time
from typing import TYPE_CHECKING

from flask import Flask

from zhyonlineedu.core.config import Config, load_config
from zhyonlineedu.core.events import EventBus
from zhyonlineedu.core.logging import configure_logging
from zhyonlineedu.core.progress.flush import ProgressFlush, replay_unflushed

if TYPE_CHECKING:
    from zhyonlineedu.modules.plugin.service import PluginService

logger = logging.getLogger(__name__)


def build_event_host(config: Config) -> tuple[Flask, EventBus, PluginService]:
    """构造只承载插件订阅的最小宿主 app（返回 host / 总线 / PluginService）。"""
    from zhyonlineedu.modules.plugin.service import PluginService

    host = Flask("zhy-event-host")
    host.extensions["zhy_config"] = config
    events = EventBus()
    host.extensions["zhy_events"] = events
    return host, events, PluginService(config, events=events)


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
    host, events, service = build_event_host(config)
    with host.app_context():
        try:
            service.boot()
        except Exception:  # pragma: no cover - 插件故障不能拖垮落库
            logger.exception("插件挂载失败，事件订阅可能缺失")
        flush = ProgressFlush(config, events=events)
        logger.info(
            "progress flush worker start writer=%s subscriptions=%s",
            flush.writer,
            len(events.subscriptions()),
        )
        try:
            run_flush(flush, once=args.once)
        except KeyboardInterrupt:  # pragma: no cover - 信号退出
            logger.info("progress flush worker stopped by signal")


if __name__ == "__main__":
    main()
