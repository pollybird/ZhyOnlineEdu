"""内核暴露给插件的**稳定 SDK 白名单**（§11.9 · M3.4）。

只暴露以下入口，且全部限定在插件自有数据与降权能力内：

| 入口 | 能力声明 | 说明 |
|---|---|---|
| `sdk.store.upsert` | —（仅限 `plg_<code>_` 自有表） | 唯一写入路径，走内核 UPSERT 契约 |
| `sdk.store.fetch` | —（仅限 `plg_<code>_` 自有表） | 只读自有表（自动过滤软删行、行数 ≤200） |
| `sdk.progress.record` | `progress:report` | 等同 `ProgressGateway.record` |
| `sdk.media.issue_ticket` | `media:ticket:issue` | 降权试看票（`preview=true`、≤360、TTL≤300s） |
| `sdk.access.evaluate` | `access:evaluate` | 只读访问判定 |
| `sdk.compat.flavor/cap/caps` | — | 只读方言/能力位 |
| `sdk.events.subscribe` | `event:subscribe` | 只读订阅内核事件（不能派发、不能改写 payload） |

**明确不暴露**（收窄红线）：
- 无 `sdk.db` / `sdk.session`（插件拿不到 ORM 会话，不能绕过 upsert 契约）；
- 无 `sdk.progress.record_raw` / `progress.direct_write`（必须走进度网关）；
- 无 `sdk.compat.configure/reset`（能力位只读，插件不能自造方言分支）；
- 无 `sdk.events.emit`（插件不能伪造 `lesson.finished` 绕过进度唯一写入入口）；
- 无任何 DDL / FFmpeg / 裸 SQL 入口（建表走插件独立迁移链，转码复用内核沙箱）。

SDK 只能在 `/api/v1/plugin/<code>/...` 处理内调用：`PluginService.dispatch`
会用 manifest 的 `code` 与 `capabilities` 绑定作用域（`context.using`），
脱离作用域调用一律 `PLUGIN_CAP_DENIED`；插件 `setup()` 挂载阶段同样由内核绑定作用域。
"""

from __future__ import annotations

from . import access, compat, events, media, progress, store

__all__ = ["access", "compat", "events", "media", "progress", "store"]
