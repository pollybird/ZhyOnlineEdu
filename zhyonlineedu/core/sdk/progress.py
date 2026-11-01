"""`sdk.progress`：进度上报的唯一入口（M3.4 · §11.9）。

口径：
- **不暴露** `record_raw` / `direct_write`：插件必须走 `ProgressGateway.record`，
  与学员端完全同一套校验（课时存在、模式、试看、位置上限、播放速率）；
- 调用需要 manifest 声明 `progress:report`；
- 插件无法写入 `learn_progress` 或绕过 `progress_log` 网关。
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime
from typing import Any

from flask import current_app

from zhyonlineedu.core.progress.gateway import ProgressGateway
from zhyonlineedu.core.sdk.context import require

__all__ = ["record"]


def record(
    *,
    user_id: int,
    lesson_id: int,
    body: Mapping[str, Any],
    can_preview: bool,
    now: datetime | None = None,
) -> dict[str, Any]:
    """等同 `ProgressGateway.record`（需 `progress:report` 能力）。"""
    require("progress:report")
    gateway = current_app.extensions["zhy_progress"]
    if not isinstance(gateway, ProgressGateway):  # pragma: no cover - 装配异常
        raise RuntimeError("进度网关未装配")
    return gateway.record(
        user_id=user_id,
        lesson_id=lesson_id,
        body=body,
        can_preview=can_preview,
        now=now,
    )
