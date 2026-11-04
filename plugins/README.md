# 参考插件

本目录是两个**可直接安装**的参考插件（M3.7），用于验证插件框架与 SDK 契约：

| 目录 | code | 说明 |
|---|---|---|
| `sample_hello/` | `sample_hello` | 最小可加载插件：manifest + `HANDLERS` + 前端 ESM 页面，演示统一入口与统一信封 |
| `reference_certificate/` | `reference_certificate` | 结业证书：**只订阅 `lesson.finished`** 发证书，不直写进度；演示独立迁移链、`sdk.store`、`sdk.events` |

> 目录名必须与 manifest 里的 `code` 完全一致（安装时强校验）；`code` 只允许
> `^[a-z][a-z0-9_]{2,63}$`，因此目录名用下划线而非连字符。

## 安装与启用

```bash
# 后端配置指向本目录（默认取 ZHY_PLUGIN_DIR，见 .env.example）
export ZHY_PLUGIN_DIR=./plugins

# 管理端（需 system:manage）：先安装，再启用
curl -X POST -H "Authorization: Bearer $ADMIN_TOKEN" \
  http://127.0.0.1:8000/api/v1/system/plugins/sample_hello/install
curl -X POST -H "Authorization: Bearer $ADMIN_TOKEN" \
  http://127.0.0.1:8000/api/v1/system/plugins/sample_hello/enable

# 也可以直接在前端 /admin/plugins 页面点击「安装 / 启用」
```

## 契约要点

- 插件路由统一走 `/api/v1/plugin/<code>/<subpath>`，子路径必须在 manifest `routes` 中声明；
- 插件表必须以 `plg_<code>_` 开头且包含 `delete_seq`（软删，D19a）；
- 写数据只能 `sdk.store.upsert`，读数据只能 `sdk.store.fetch`，拿不到 ORM session；
- 事件**只能订阅不能派发**：`sdk.events.subscribe()` 需 manifest 声明 `event:subscribe`，
  订阅在 `setup()` 内注册，禁用/卸载/加载失败时内核按 code 全量摘除；
- `lesson.finished` 载荷 `{user, lesson, course, percent, via_ticket_preview}`：
  `via_ticket_preview=true`（试看票）时所有订阅方必须跳过统计/发证书。
