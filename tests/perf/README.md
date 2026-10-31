# `tests/perf/` —— M2.16 压测窗口

本地/预发环境的压测脚本，**不参与 `pytest` 收集**（文件名不是 `test_*.py`）。

| 文件 | 用途 | 验收 |
|---|---|---|
| `seed_progress.py` | 幂等创建 N 个压测学员账号（admin API） | 账号可登录 |
| `locustfile_progress.py` | 100 QPS 进度上报（每用户 1 次/秒，30s 桶去重） | HBT-006 |
| `check_hbt006.py` | 对账：`gw_seq` 无缺口、`seq_no` 连续、`watch_seconds` 误差 0 | HBT-006 |
| `locustfile_play.py` | 播放链路回源压测（master/variant/ts） | MEDIA / CDN 口径 |

## 100 QPS 进度上报（HBT-006）

先用生产形态 WSGI 入口（gunicorn 4 worker × 16 gthread）起后端，Flask dev server
单进程实测只有 ~85 QPS，达不到 100 QPS 目标：

```bash
source var/env.sh
# 1) 后端：4 进程 × 16 线程
.venv/bin/gunicorn -w 4 -k gthread --threads 16 -b 127.0.0.1:5000 var.wsgi:app
# 2) 进度落库单写者（另一个终端）
.venv/bin/python -m zhyonlineedu.workers.progress_flush

# 3) 压测账号（换前缀可避免历史行污染基线）
ZHY_PERF_USER_PREFIX=perf7 .venv/bin/python tests/perf/seed_progress.py --users 120

# 4) 120 用户 × 5 分钟：pacing 1s ⇒ 100+ QPS，登录按 -r 20 分摊
ZHY_PERF_USER_PREFIX=perf7 .venv/bin/locust -f tests/perf/locustfile_progress.py \
    --headless -u 120 -r 20 -t 5m --csv var/perf/progress_perf7 --stop-timeout 20

# 5) 对账（等 flush 排空，默认最多等 90s，可 `--flush-timeout` 改）
ZHY_PERF_USER_PREFIX=perf7 .venv/bin/python tests/perf/check_hbt006.py \
    --expected var/perf/progress_expected.json
```

要点（M2.16 实测踩过的坑）：

- **`wait_time = constant_pacing(1)`**：固定 `constant(1)` 含请求耗时，100 用户只有 ~66 QPS；
  pacing 按「开始时刻」对齐，120 用户实测 117 QPS。
- **`-r 20` 而非 `-r 120`**：瞬时 120 个 Argon2 登录会挤占 CPU；登录失败脚本自动退避重试 3 次，
  仍失败只停该虚拟用户（不终止整场）。
- **`--stop-timeout 20`**：locust 退出时会等在途请求收干再写 `progress_expected.json`，
  否则「服务端已提交、客户端未累计」会差出 N 条（每用户 +1 的假缺口）。
- 必须单进程运行（账号序号在进程内分配）；`var/wsgi.py` 只是本地压测入口（`var/` 已 gitignore）。
- 对账前请确保 `progress-flush` worker 在跑；若超时仍 `未落库 > 0`，先看 worker 日志再重跑对账。
- 压测用户默认前缀 `perf`、口令 `Perf-pass-2026`；可用环境变量覆盖（见各脚本 docstring）。

## 播放回源压测

```bash
.venv/bin/locust -f tests/perf/locustfile_play.py --headless \
    -u 300 -r 30 -t 2m --csv var/perf/play --only-summary
```

- 分片是 `public + 长 TTL`，生产中由 CDN 承载；本脚本只测**回源**吞吐。
  「回源 ≤5%」需在部署环境按 CDN 日志统计，本地不做伪造。
- 3000 并发建议多机/多进程；单机先用 300 并发观察 P95 与错误率。

## 产物

`var/perf/`（已被 `.gitignore` 忽略）：locust CSV 与 `progress_expected.json`。
