"""`sdk.media`：插件签发**降权票据**（M3.4 · §11.9）。

口径（只降不升）：
- 插件票一律 `preview=true`（不计入统计、带水印），无法签"正式票"；
- 档位最高 `360`（`max_profile` 固定，插件无法申请 720/1080）；
- TTL 覆盖上限 300 秒，且始终与模式策略 / 全局配置取小；
- 访问授权仍走内核 `decide_access`（未授权就该拒绝，不因插件调用而放宽）；
- 需要 manifest 声明 `media:ticket:issue`。
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from flask import current_app

from zhyonlineedu.core.sdk.context import require

if TYPE_CHECKING:  # pragma: no cover - 仅类型
    from zhyonlineedu.modules.learn.playback import PlaybackService

__all__ = ["MAX_PROFILE", "MAX_TTL_OVERRIDE", "issue_ticket"]

MAX_PROFILE = "360"
MAX_TTL_OVERRIDE = 300


def issue_ticket(
    lesson_id: int,
    *,
    user_id: int,
    username: str = "",
    ip: str = "",
    ttl_override: int | None = None,
) -> dict[str, Any]:
    """签发插件降权试看票；`ttl_override` 会被夹到 `30..300` 秒。"""
    require("media:ticket:issue")
    ttl = MAX_TTL_OVERRIDE if ttl_override is None else min(max(int(ttl_override), 30), 300)
    playback = current_app.extensions["zhy_playback"]
    service: PlaybackService = playback
    from zhyonlineedu.modules.learn.playback import TicketRequest

    request = TicketRequest(
        user_id=user_id,
        username=username,
        lesson_id=lesson_id,
        can_preview=True,
        ip=ip,
    )
    return service.issue(request, max_profile=MAX_PROFILE, ttl_override=ttl)
