"""learn 模块对外唯一入口（§4.1 / §4.2 红线 1）。

`api/` 只允许 `from modules.learn.gateway import record`；`modules.learn.repo`
仅供 workers/progress_flush.py（单写者）使用，禁止在 api 层 import。

真正的进度网关实现在 `core/progress/gateway.py`（无状态、按请求自建短事务）；
本模块只做应用装配解引用，避免 api 层直接触碰 `current_app.extensions`。
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime
from typing import Any, cast

from flask import current_app

from zhyonlineedu.core.progress.gateway import ProgressGateway


def gateway() -> ProgressGateway:
    return cast(ProgressGateway, current_app.extensions["zhy_progress"])


def record(
    *,
    user_id: int,
    lesson_id: int,
    body: Mapping[str, Any],
    can_preview: bool,
    now: datetime | None = None,
) -> dict[str, Any]:
    return gateway().record(
        user_id=user_id,
        lesson_id=lesson_id,
        body=body,
        can_preview=can_preview,
        now=now,
    )


__all__ = ["gateway", "record"]
