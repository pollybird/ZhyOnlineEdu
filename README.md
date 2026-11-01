# 钟毓在线教育系统（ZhyOnlineEdu）

面向**私有化部署**的在线教育系统内核：同一份代码 + 同一套迁移可在
SQLite / MySQL / PostgreSQL / openGauss（5.x / 6.x / 7.0）/ KingbaseES V9 下运行；
课程、媒体、播放、学习进度、题库、会员与双场景模式均已落地，进度网关在
多实例 / 崩溃 / 重放场景下不丢数据。

- 后端：Python 3.11 · Flask 3.1 · SQLAlchemy 2 · Alembic · PyJWT(RS256) · Argon2 · gunicorn
- 前端：Vue 3 · TypeScript · Vite · Pinia · Element Plus · hls.js
- 媒体：FFmpeg（bwrap / cgroup 沙箱）+ AES-128 HLS + 三层 TTL 票据
- 当前版本：**0.3.4-alpha**（命名规则见 [版本号规则](#版本号规则)）
- 许可证：Apache-2.0（见 [LICENSE](LICENSE)）

## 能力概览

| 能力 | 说明 |
|---|---|
| 安装向导 | 一次令牌 + 5 步状态机（DB 探测/建库 → 管理员 → 场景模式 → 完成）；安装完成前 `/api/v1/**` 一律 `409 INSTALL_REQUIRED`，业务路由按能力探测结果装配 |
| 多库适配 | 安装期探测并固化能力位（`sys_compat_flags`），UPSERT 按方言三路编译 + 应用层兜底，禁止 `flush()` 取自增 id |
| 课程与素材 | 学科/课程/章节/课时，软删哨兵，课时时长由视频派生（上传探测 + 转码发布校正） |
| 媒体流水线 | 分片上传（幂等、续传、tmp 回收）→ 沙箱转码（魔数校验、无网络、只读、seccomp、cgroup、输出二次探测）→ 多档 HLS |
| 播放分发 | 票据（ticket）与 kid 解耦：m3u8 可长缓存进 CDN，取 key 走 ksession + nonce；ts/playlist/key 三层 TTL；播放器 403 自动续签 |
| 进度网关 | HTTP 只追加 `learn_progress_log`（`gw_seq` 全局单调、`seq_no` 按用户+课时连续、30s 位置桶去重）；单写者 flush 按短事务提交，绝对值重算 + `GREATEST` 不回退，崩溃后从未落库行补齐 |
| 学习档案 | `/my/progress`、课程维度档案、`/my/summary`；预览流水不计入统计 |
| 题库判分 | 题库 CRUD、答题幂等、交卷判分与及格线 |
| 用户与会员 | 用户/班级/导入（冲突策略）、账号与会员过期作业、会员等级/开通/订单与支付回调 |
| 双场景模式 | `preview` / 正式一键切换：只改单行状态（epoch+1），全站 ≤30s 生效，切换窗口流水隔离 |
| 可观测 | 统一信封 `{code, message, data, request_id}`、`/healthz` `/readyz`、54 个错误码、OpenAPI 生成物受 CI 校验 |

## 目录结构

```
zhyonlineedu/
  api/          # Flask Blueprint（auth/install/catalog/media/learn/quiz/users/commerce/system）
  core/         # 配置、DB 适配与能力探测、认证/RBAC、媒体、进度网关、安装、OpenAPI
  modules/      # 业务服务层（catalog/media/learn/assess/users/commerce/config）
  workers/      # progress_flush（单写者）/ transcode（沙箱）/ expire / celery
migrations/     # Alembic 迁移（0001 ~ 0013）
scripts/        # 红线自检与 OpenAPI 导出
tests/          # 后端测试（含 dialect/hbt/media/sandbox 标记）
tests/perf/     # 压测与对账脚本（不进 pytest 收集）
web/            # Vue 3 前端（学员端 + 管理端 + 安装向导）
deploy/ffmpeg/  # 沙箱启动器、seccomp、FFmpeg SBOM
spike/          # 关键技术验证与实测记录（spike/RESULTS.md）
plan/           # 技术开发计划书 + 开发规划与里程碑（随仓库入库）
wsgi.py         # WSGI 入口（flask / gunicorn）
manage.py       # 运维入口：db-migrate / tmp-reaper
```

> `plan/` 为项目基线与任务分解文档，与代码同步维护；`var/` 为本地运行目录
> （数据库、密钥、存储、上传、压测产物），不入库。

## 快速开始

### 0. 前置依赖

- Python **3.11**、Node **22**、`ffmpeg`/`ffprobe`（转码与时长探测需要）
- 推荐用 [uv](https://docs.astral.sh/uv/) 安装依赖（仓库带 `uv.lock`）

### 1. 安装依赖

```bash
uv sync --frozen --group dev          # 后端（含 pytest / locust / mypy）
cd web && npm ci && cd ..             # 前端
```

### 2. 配置

进程**只读取环境变量**（没有自动加载 `.env` 的魔法），先复制模板再注入：

```bash
cp .env.example .env
set -a; . ./.env; set +a
```

开发档最小可用配置（也可以直接 export）：

```bash
export ZHY_ENV=dev
export ZHY_DB_URL=sqlite:///./var/zhy.db
export ZHY_JWT_PRIVATE_KEY_PATH=./var/keys/private.pem
export ZHY_JWT_PUBLIC_KEY_PATH=./var/keys/public.pem
export ZHY_PROGRESS_RING_DIR=./var/ring
export ZHY_STORAGE_LOCAL_DIR=./var/storage
export ZHY_UPLOAD_TMP_ROOT=./var/uploads
```

> 非 `dev` 档启动前会做配置断言：沙箱不得 `disabled`、`upload.require_ffprobe` 必须为真、
> `media.aes_key_hex` 必须是 32 字节 hex、`pay.callback_secret` 必须替换。完整键表见 `.env.example`。

### 3. 启动后端

```bash
# 开发
.venv/bin/flask --app wsgi run --port 5000
# 生产形态（计划书 §13：4 worker × 16 gthread）
.venv/bin/gunicorn -w 4 -k gthread --threads 16 -b 127.0.0.1:5000 wsgi:app
```

健康检查：`GET /healthz`（能力探测状态）、`GET /readyz`（未 READY 返回 503）。

### 4. 启动后台 worker

```bash
.venv/bin/python -m zhyonlineedu.workers.progress_flush   # 进度单写者（必须）
.venv/bin/python -m zhyonlineedu.workers.transcode        # 转码（沙箱）
.venv/bin/python -m zhyonlineedu.workers.expire           # 每日账号/会员过期
```

`progress_flush --once` 供联调/对账使用；跨实例部署时由 `learn_progress_gate` 租约保证单写者。

### 5. 启动前端

```bash
cd web && npm run dev      # http://127.0.0.1:5173，/api 已代理到 127.0.0.1:5000
```

### 6. 完成安装

首次访问会跳转 `/install`。安装令牌取后端日志 `安装令牌（仅本次安装有效）：…`
或由 `ZHY_INSTALL_TOKEN` 注入；向导按 **DB 探测 → 建库（Alembic）→ 管理员 → 场景模式 → 完成**
推进，完成后生成 RSA 密钥对并解锁业务接口。

> 安装完成前业务接口返回 `409 INSTALL_REQUIRED` 属于预期行为，不是故障。

## API

- 统一信封：`{code, message, data, request_id}`；错误码共 54 个（`zhyonlineedu/core/codes.py`）
- 规格：`GET /api/v1/openapi.json`（74 paths / 90 operations / 132 schemas）
- 前端生成物 `web/src/api/openapi.json` 与 `web/src/api/types.ts` 由
  `scripts/export_openapi.py` 产出，CI 用 `--check` 校验漂移

## 测试与质量门禁

```bash
# 后端（当前基线：382 passed / 34 skipped）
.venv/bin/python -m pytest
.venv/bin/python -m pytest -m hbt        # 进度网关 HBT-001~006
.venv/bin/python -m pytest -m media      # 票据与播放 MEDIA-001~008
.venv/bin/python -m pytest -m dialect    # 方言矩阵
.venv/bin/python -m pytest -m sandbox    # 转码沙箱 SANDBOX-001~006

# 静态检查与红线
.venv/bin/ruff check zhyonlineedu tests scripts
.venv/bin/black --check zhyonlineedu tests scripts
.venv/bin/mypy zhyonlineedu                        # strict
.venv/bin/python scripts/check_dialect_compat.py   # 方言裸 SQL 红线
.venv/bin/python scripts/check_progress_monotonic.py  # 进度单调红线
.venv/bin/python scripts/check_migration_caps.py   # 迁移能力矩阵
.venv/bin/python scripts/check_version.py          # 版本号规则与一致性
.venv/bin/python scripts/export_openapi.py --check

# 前端
cd web && npm test && npm run build
```

CI 见 `.github/workflows/ci.yml`（quality + web + 汇总门禁）；本地 `pre-commit` 已配置同样门禁。

## 压测与对账

```bash
.venv/bin/python tests/perf/seed_progress.py --users 120
ZHY_PERF_USER_PREFIX=perf7 .venv/bin/locust -f tests/perf/locustfile_progress.py \
    --headless -u 120 -r 20 -t 5m --csv var/perf/progress --stop-timeout 20
ZHY_PERF_USER_PREFIX=perf7 .venv/bin/python tests/perf/check_hbt006.py \
    --expected var/perf/progress_expected.json
```

M2.16 实测（gunicorn 4×16 + 单写者 flush，120 用户 × 5 分钟）：
`POST /learn/progress` **117.1 RPS / 0 失败**（P50 23ms、P95 100ms、P99 560ms），
对账 `gw_seq` 无缺口、`seq_no` 连续、`watch_seconds` 误差 0、未落库 0。
细节与踩坑口径见 `tests/perf/README.md`、`spike/RESULTS.md`。

## 运维

```bash
.venv/bin/python manage.py db-migrate                    # Alembic upgrade head（并记录 last_good_rev）
.venv/bin/python manage.py db-migrate --rollback-to-last-good
.venv/bin/python manage.py tmp-reaper                    # 清理过期上传会话与未消费临时文件
```

> MySQL 的 DDL 非事务：升级中途失败时 `downgrade` 未必能还原，必须从备份恢复。

部署编排骨架见 `docker-compose.yml`（T1 档：api / progress-flush 单实例 / transcode / worker / expire / db），
FFmpeg 沙箱资产见 `deploy/ffmpeg/`（`bwrap-launcher.sh`、`seccomp.json`、SBOM）。

## 版本号规则

正式发布（`1.0.0`）之前，**大版本号恒为 `0`**，版本号与《开发规划与里程碑》的里程碑一一对应：

```
0.<里程碑主号>.<里程碑次号>-alpha
```

| 阶段 | 版本号 |
|---|---|
| 完成 M2.15 | `0.2.15-alpha` |
| 完成 M3.4 | `0.3.4-alpha`（当前） |
| 完成 M3.6 | `0.3.6-alpha` |
| 进入 Beta 阶段 | `0.4.0-beta` |
| 正式发布 | `1.0.0` |

- **唯一来源**：`zhyonlineedu/__init__.py` 的 `__version__`。`pyproject.toml`（PEP 440 归一为
  `0.3.4a0`）、`web/package.json`、`web/package-lock.json`、`web/src/api/openapi.json` 必须同步，
  禁止在代码里散落版本字符串。
- **自动校验**：`scripts/check_version.py` 检查「命名规则 + 各处一致」，CI quality 作业与
  `pre-commit` 均执行；OpenAPI 规格的 `info.version` 由 `scripts/export_openapi.py` 从
  `__version__` 生成。
- **运行时暴露**：`GET /healthz` 与 `GET /api/v1/openapi.json` 返回同一版本号；镜像与部署用
  `ZHY_VERSION`（`docker-compose.yml` 默认 `dev`，发版时传 `0.3.4-alpha`）保持同一标签。

## 当前进度

M0（技术验证）~ M2（安装、课程、媒体、播放、进度、题库、会员、双模式与学员/管理端）已完成并
通过 HBT-001~006 与 MEDIA-001~008 用例；M2.16 压测窗口已收尾。M3.1~3.4（插件 manifest / 生命周期
/ 独立迁移链 / SDK 收窄）已完成，当前版本 **0.3.4-alpha**。
M3 剩余 3.5~3.9 进入后续联调。
实测记录、缺陷与修复过程见 [`spike/RESULTS.md`](spike/RESULTS.md)。
