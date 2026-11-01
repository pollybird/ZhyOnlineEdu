"""`sdk.access`：只读访问判定（M3.4 · §11.9）。

插件问"这个用户能否看这节课"时只允许调用本函数：
- 复用内核唯一入口 `decide_access`（§9.4 决策表），插件不得自造权限逻辑；
- **只读**：不签票、不写进度、不发事件；
- 需要 manifest 声明 `access:evaluate`。
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from flask import current_app

from zhyonlineedu.core.sdk.context import require

if TYPE_CHECKING:  # pragma: no cover - 仅类型
    from zhyonlineedu.modules.learn.playback import PlaybackService

__all__ = ["evaluate"]


def evaluate(user_id: int, lesson_id: int, *, can_preview: bool = False) -> dict[str, Any]:
    """只读判定结果 `{allowed, code, reason, rule, preview, mode, epoch, ...}`。"""
    require("access:evaluate")
    playback = current_app.extensions["zhy_playback"]
    service: PlaybackService = playback
    return service.evaluate_lesson(user_id=user_id, lesson_id=lesson_id, can_preview=can_preview)
