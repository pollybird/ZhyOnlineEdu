# M0 技术验证 Spike · 结果报告

> 依据《技术开发计划书 V2.3》§17.2（M0 17 人日）与 §16 用例编号。
> 环境：Linux x86_64 · Python 3.11 · SQLite 3.46 · PostgreSQL 16 · MySQL 8.4 · FFmpeg 6.1 · bubblewrap 0.11
> 复跑：`uv run pytest spike -q` 与 `uv run python spike/sandbox/run_sandbox.py --transcode`

## 一、结论摘要

| 关卡 | 判定 | 依据 |
|---|---|---|
| **F1** 方言 upsert 三路编译 | **PASS（三库实测）** | SQLite 16/16、PG 16/16、MySQL 13 PASS + 3 预期差异 |
| **F1'** 国产库实测 | **BLOCKED（待介质）** | openGauss 5/6/7、Kingbase 共 64 条 COMPILE_ONLY 通过；无镜像/介质 |
| **F2** 进度网关 LocalBus | **PASS** | HBT-001~007 全绿，含 kill -9 补齐与单写者互斥 |
| **F3/F4** 票据模型 | **PASS（服务端）** | MEDIA-001~008 全绿 |
| **F5** 插件加载 | **PASS** | 加载/禁用/能力拒绝/前缀校验/observe 副本，6 项全绿 |
| **F7** 转码沙箱 | **PASS** | SANDBOX-001~006 全绿，482MB→84 片 AES-128 HLS |
| **真机** iOS/Android 取 key | **BLOCKED** | 无设备，需 M2.5 前借调（见 §四） |

**M0 放行判定：有条件通过** —— 代码级契约已验证；openGauss/Kingbase 与真机两项因外部资源未到位记 BLOCKED。
M1.2 结束前介质仍未到，国产库 30 条维持 COMPILE_ONLY，顺延至 M5 国产化承诺前补实测。

## 二、方言矩阵（72 条目标 / 本次 16 组 × 7 flavor）

### 2.1 实测结果

| flavor | 模式 | route | PASS | DIFF | 说明 |
|---|---|---|---|---|---|
| sqlite | RUNTIME | pg | 16 | 0 | DDL 回滚不可用（pysqlite legacy 事务控制）已记录 |
| postgresql | RUNTIME | pg | 16 | 0 | — |
| mysql | RUNTIME | mysql | 13 | 3 | D2a 无 RETURNING、D8 大小写敏感、X1 批量不返 id |
| opengauss_5 | COMPILE_ONLY | og5 | 16 编译通过 | — | 需介质补实测 |
| opengauss_6 | COMPILE_ONLY | og5 | 16 编译通过 | — | 需介质补实测 |
| opengauss_7 | COMPILE_ONLY | pg | 16 编译通过 | — | 需介质补实测 |
| kingbase | COMPILE_ONLY | pg | 16 编译通过 | — | 需介质补实测 |

### 2.2 实测暴露的 4 个契约缺陷（已回写编译器）

1. **PG `version = version + 1` 报 AmbiguousColumn**（D10a）
   `ON CONFLICT DO UPDATE SET` 右侧的 `version` 同时命中目标表与 EXCLUDED。
   契约：version 自增必须以表名限定 `version = <table>.version + 1`。
2. **SQLite 无 `GREATEST`**（D10a/U7）
   进度单调写入在 SQLite 必须降级为标量 `max(a,b)`；业务 SQL 不得裸写 `GREATEST`，
   由 `core.db` 兼容层统一生成（与 §4.2 红线 2 的检查脚本配套）。
3. **SQLite 裸绑定 `Decimal` 抛 ProgrammingError**（D14）
   生产必须使用 SQLAlchemy `Numeric` 类型装饰器，不能 `text()` 直绑 Decimal。
4. **SQLite DDL 不在事务内**（D4）
   `pysqlite` legacy 模式只在 DML 前 BEGIN，`CREATE TABLE` 实际自动提交 →
   Alembic 迁移在 SQLite 上不可回滚（须用 batch 模式 + 前置备份）。

### 2.3 路由表（冻结）

```
sqlite / postgresql / kingbase / opengauss_7  → pg   （ON CONFLICT + RETURNING）
mysql                                         → mysql（ON DUPLICATE KEY UPDATE）
opengauss_5 / opengauss_6                     → og5   （MySQL 风，无 RETURNING，禁更自然键）
```

### 2.4 M1.2 生产化回验（`tests/test_dialect_matrix.py`）

| 组合 | 状态 | 结果 |
|---|---|---|
| SQLite / PostgreSQL 16 / MySQL 8.0 | RUNTIME | 各 14 组，共 42 条 **PASS**（PG/MySQL 由 `ZHY_TEST_PG_DSN` / `ZHY_TEST_MYSQL_DSN` 驱动容器实跑） |
| openGauss 6 / Kingbase（各 14 组） | COMPILE_ONLY | 28 条编译与安装期约束通过；**不得回标 PASS**，介质到位后替换为实跑 |
| openGauss 5/7 路由 + 回读扩展 | COMPILE_ONLY | 2 条通过（5 走 og5 无返回；7 走 pg + RETURNING） |
| 合计 | — | **72 passed**（`pytest -m dialect`） |

M1.2 新增并已回写契约的 2 个缺陷：

5. **SQLite savepoint 释放时游标未消费**（upsert 原生路径）
   `RETURNING` 结果在 `begin_nested()` 之外才 `first()`，pysqlite 在 RELEASE 时报
   `cannot open savepoint - SQL statements in progress`，并触发错误的兜底路径。
   契约：结果必须在 savepoint 内消费并 `close()`。
6. **`sys_compat_flags` 无 `id/version` 列**
   通用编译默认 `RETURNING id, version` 导致安装期固化失败。
   契约：`compile_upsert(..., pk_column=..., version_column=...)` 必须可声明表主键/版本列，
   无版本列时不生成自增子句。

另：MySQL 在 Linux 默认 `lower_case_table_names=0`（大小写敏感），D8 期望值以实跑为准；
Windows/macOS 行为相反，属已知方言差异，不作为缺陷。

### 2.5 M1.3 能力感知迁移回验（`tests/test_migrations.py`）

| 组合 | 状态 | 结果 |
|---|---|---|
| SQLite 文件库 | RUNTIME | `upgrade head` 建 4 张系统表；`downgrade base` 全清；`sys_install_state` 落 `schema_rev` / `last_good_rev` = `0001_system_schema` |
| PostgreSQL 16 / MySQL 8.0 | RUNTIME（DSN 门控） | 升级 → 降级往返 PASS（容器实跑，M1.3 共 32 条） |
| openGauss 5/6/7 + Kingbase | COMPILE_ONLY | 离线 `--sql` 生成 PASS；`render_as_batch=True`；首个 revision 无 `ALTER COLUMN`；链上出现 `alter_type_online:true` 时直接拒绝 |

M1.3 新增并已回写契约的 2 个缺陷：

7. **软删复合 UK 注入时机**（D19a）
   `Base.__init_subclass__` 里必须在 `super().__init_subclass__` **之前**改写 `__table_args__`；
   `super()` 返回后声明式表已构建，追加约束会静默丢失（断言工具才会在启动时报错）。
8. **Alembic 1.16 无 `rendered_item_type_requires_batch`**（1.20+ 才有）
   只能用 `render_as_batch` + `compare_type` 等价表达；且 `BatchOperations` 需从
   `alembic.operations.base` 导入（`ops` 未导出）。守卫按 **upgrade 方向**遍历，
   downgrade 由 revision 手写、不参与生成期校验。

## 三、进度网关（F2）

| 用例 | 结果 | 关键数据 |
|---|---|---|
| HBT-001 正常链路（2 worker × 50） | PASS | 日志 100 条，位置取最新 59，version=50 |
| HBT-002 乱序 + 重放 + 二次 flush | PASS | 日志不重复，位置取最大值 180，时长 20 |
| HBT-003 kill -9 后重启补齐 | PASS | 200/200 条全部补齐（msync=True 口径） |
| HBT-004 preview 隔离 | PASS | preview 不入统计（pos=500/watch=60） |
| HBT-005 双 flush 并发 | PASS | 租约被占时不写；释放后接管写 30 条 |
| HBT-006 CRC 坏槽 | PASS | 丢弃 1 坏槽，保留 9 条 |
| HBT-007 ring 覆盖写 | PASS | 仅保留最近 4096 条，位置不倒退 |

**工程结论**：
- gw_seq = `毫秒时间戳<<22 | pid<<12 | 进程内计数器`，41+10+12 位，跨进程时间有序、免中心分配、免 fsync。
- 单写者用 `flock` 而非 TTL 租约：进程被 SIGKILL 时内核自动释放，无"租约到期才接管"的空窗。
- `msync=True` 每条落盘 → kill -9 零丢失；生产可改为批量 msync（丢失 ≤ 一个 flush 周期）。
- ring 覆盖写只影响重复条目，不影响正确性（`gw_seq` 去重 + `GREATEST` 幂等投影）。

## 四、票据与沙箱

### 4.1 票据（MEDIA-001~008，服务端）

| 用例 | 结果 |
|---|---|
| MEDIA-001 开票 + playlist 可取 | PASS（ts TTL 3d / key TTL 120s / slack 20s） |
| MEDIA-002 playlist 与用户无关 | PASS（同课同 URL，可 CDN 长缓存，不泄漏身份） |
| MEDIA-003 无 ksession / nonce 伪造 | PASS（403） |
| MEDIA-004 key 单次 nonce 重放 | PASS（**40206**，审计留痕，不提示密码错） |
| MEDIA-005 串课取 key | PASS（**40203**） |
| MEDIA-006 ts/key 过期 | PASS（**40201**） |
| MEDIA-007 风控吊销 | PASS（**40204**） |
| MEDIA-008 篡改签名 | PASS |

**暴露的缺陷**：key URL 的 `exp` 初版未进签名 → 篡改后仍可取 key。
已改为 `sig` 覆盖 `exp` 并校验与 ksession 到期时间一致。**M2.5 实现须沿用。**

### 4.2 沙箱（SANDBOX-001~006）

| 用例 | 结果 | 细节 |
|---|---|---|
| SANDBOX-001 网络隔离 | PASS | 沙箱内 TCP 连接 OSError |
| SANDBOX-002 /etc 不可见 | PASS | `test -e /etc/passwd` 失败 |
| SANDBOX-003 宿主进程不可见 | PASS | 宿主 pid 无泄漏，沙箱内仅 4 个进程 |
| SANDBOX-004 宿主只读 | PASS | 沙箱内写 /usr 失败，宿主无文件 |
| SANDBOX-005 父死子亡 | PASS | 父进程 SIGKILL 后 0.25s 内退出 |
| SANDBOX-006 482MB→HLS 加密 | PASS | 63s、84 分片、EXTINF=84、AES-128、无 key 不可解析 |

**沙箱实测的 3 个部署要点（已回写 deploy/ffmpeg/bwrap-launcher.sh）**：
1. `bwrap --seccomp` 只接受**原始 BPF**，不能喂 JSON → seccomp 由容器运行时下发
   （`security_opt: seccomp=deploy/ffmpeg/seccomp.json`）；bwrap 负责命名空间与只读挂载。
2. `/etc` 用 tmpfs 屏蔽后，ffmpeg 动态库软链（`libblas.so.3` 等）失效 →
   必须单独 `--ro-bind /etc/alternatives`。
3. `-hls_key_info_file` 第 2 行是**沙箱内路径**，写宿主绝对路径会导致 muxer 打不开密钥。
4. `--die-with-parent` 存在启动竞态：bwrap 完成 `PR_SET_PDEATHSIG` 前杀父进程会留下孤儿，
   生产侧需在拉起后等待就绪（M2.4 worker 需 register-ready 信号）。

## 五、插件（F5）

6 项全绿：加载可服务、禁用即 404、`direct_write` 拒绝（PLUGIN_CAP_DENIED）、
表前缀强制 `plg_<key>_`、observe 只拿副本（改副本不影响内核）、manifest JSON 往返。
**结论**：SDK 收窄（删 direct_write / direct session）在运行时可控，M3 可照此实现。

## 六、阻塞项与下一步

| 项 | 状态 | 解除条件 | 截止 |
|---|---|---|---|
| openGauss 5/6/7 实测 | BLOCKED | 客户/厂商提供镜像或环境 | M5 国产化承诺前（M1.2/M1.3 已 COMPILE_ONLY） |
| Kingbase V9 实测 | BLOCKED | 厂商介质 + libkci | M5.2 前 |
| iOS/Android 原生 HLS 取 key 真机 | BLOCKED | 借调设备 | 已顺延至 M2.14 学员端联调 |
| HBT-006 100 QPS 压测 | **PASS（M2.16 实测）** | `tests/perf/`：gunicorn 4×16 gthread，120 用户 5 分钟 117.5 QPS / 0 失败 / `gw_seq` 无缺口 | 已完成 2026-10-31 |
| 多码率 ladder 与 CDN 回源比 | 未做 | 属 M2.4/M4.2 范围 | — |
| 题库判分自动化（答案不下发 / 重试不双成绩） | **已自动化** | M2.6 `tests/test_quiz.py` 23 条覆盖 | — |

**M1 可立即开工的依赖已就绪**：路由表、能力位、upsert 契约、Alembic 迁移链（`x-zhy-cap` +
四步模板守卫 + `last_good_rev` 回滚）、进度 ring 格式、票据签名口径、沙箱启动参数、插件能力白名单。

## 七、产物清单

```
spike/dialect/upsert_compiler.py   三路编译原型（M1.2 直接演进为 core.db.upsert）
spike/dialect/run_matrix.py        14 组 × 7 flavor 矩阵运行器
spike/progress/ring.py             mmap ring（M2.5 演进为 LocalBus）
spike/progress/flush.py            单写者 + 幂等投影（M2.5 演进为 progress-flush）
spike/progress/test_hbt.py         HBT-001~007
spike/plugin/test_plugin.py        PLUGIN-001~006
spike/ticket/media.py              票据签名/ksession/nonce（M2.5 演进为 core.media.signer）
spike/ticket/test_media.py         MEDIA-001~008
spike/sandbox/run_sandbox.py       SANDBOX-001~006
spike/output/*.json                原始结果（已 gitignore）
```

M1.3 生产化产物（非 spike）：`alembic.ini`、`migrations/`（env.py + 0001 系统表 + 0002 安装步骤）、
`manage.py db-migrate`、`zhyonlineedu/core/db/{base,migration}.py`、`scripts/check_migration_caps.py`
（以上已在主仓，随 M1 后续 revision 扩展）。

M1.5 生产化产物：`zhyonlineedu/core/install.py`（状态机 / 一次性令牌 / 诊断分类）、
`zhyonlineedu/api/install.py`（7 端点）、`core/db/probe.py` 的字符集 blocker、
`tests/test_install.py`（13 条，含 PG/MySQL DSN 全流程）。

M1.6 生产化产物：`migrations/versions/0003_auth_rbac.py`（账号/会话/RBAC/安全日志 + 种子）、
`zhyonlineedu/core/auth/`（口令/RSA 密钥/JWT/RBAC/服务/守卫）、`zhyonlineedu/api/auth.py`、
`tests/test_auth.py`（14 条，覆盖 RT 重放与 tv 窗口）。

M1.7 生产化产物：`zhyonlineedu/api/schemas.py`（schema 唯一来源）、`zhyonlineedu/core/openapi.py`
（url_map → OpenAPI 3.1，缺文档即报错）、`zhyonlineedu/app.py` 的 `GET /api/v1/openapi.json`、
`scripts/export_openapi.py`（`--check` 进 CI/pre-commit）、`tests/test_openapi.py`（11 条，含模型漂移）、
`web/src/api/{request,auth,install,types,openapi.json}`、`web/src/stores/{auth,install}`、
`web/src/router/index.ts`（守卫）、`web/src/components/AppShell.vue`、`web/src/views/{Install,Login,Home}View.vue`
（M1.7 验收：安装 + 登录 + 框架页）。

M2.1~2.2 生产化产物：`migrations/versions/0004_catalog.py`（学科树 + 课程三层 + 最小 `media_asset`，
软删复合唯一键 `UNIQUE(uq_cols, delete_seq)`）、`zhyonlineedu/core/db/models/{catalog,media}.py`、
`zhyonlineedu/core/db/query.py`（`not_deleted` 唯一软删写法）、`zhyonlineedu/modules/catalog/service.py`
（深度/环/重名校验 + 乐观锁 + 上架 asset 就绪）、`zhyonlineedu/api/catalog.py`（18 端点）、
`tests/test_catalog.py`（11 条）；前端 `web/src/api/catalog.ts`、`web/src/stores/catalog.ts`、
`web/src/views/CatalogView.vue`（学科树 + 课程/章/课时编辑器）、`web/src/stores/catalog.test.ts`。
打磨点：Pydantic 响应模型去掉默认值后，生成 `types.ts` 才无 `?` 噪声（`vue-tsc` 严格模式一次通过）。

M2.3 生产化产物：`migrations/versions/0005_media_upload.py`（上传会话 + 分片元数据表）、
`zhyonlineedu/core/db/models/media.py`（`media_upload_session`/`media_chunk`）、
`zhyonlineedu/core/media/quarantine.py`（魔数 + ffprobe 白名单 + 时长/尺寸上限，prober 可注入）、
`zhyonlineedu/modules/media/upload_service.py`（`O_CREAT|O_EXCL` / 0700 / 0600 / 50101~50103 / tmp-reaper）、
`zhyonlineedu/api/media.py`（5 端点）、`manage.py tmp-reaper`、`tests/test_media_upload.py`（17 条）。
打磨点：ffprobe 调用改 `text=True`，避免 `.decode(` 触发"裸 DECODE"方言红线误报。

M2.4 生产化产物（转码沙箱化，F7）：`migrations/versions/0006_transcode.py`（`media_transcode_job` +
`media_asset` 的 kid/profiles/poster/sandbox 列）、`zhyonlineedu/core/media/runtime.py`
（bwrap/cgroup/disabled 三后端 + 无网 + RLIMIT + 路径根校验 + `RenderSpec` 命令枚举）、
`zhyonlineedu/core/media/hls.py`（master/变体/AES key URI/分片/时长/编码二次校验）、
`zhyonlineedu/modules/media/transcode_service.py`（入队固化 0400 源、条件更新抢占、逐档 staging、
key 派生与 shred、校验后原子发布、失败重试与 ENC-1008 回收）、`zhyonlineedu/api/transcode.py`（4 端点）、
`zhyonlineedu/workers/transcode.py`（`--once` / 批量消费 / stale 回收）、`tests/test_transcode.py`（21 条）。
打磨点：① `RLIMIT_NPROC` 按真实 uid 全线程计数，disabled（仅 dev）后端不再设置，避免宿主共享 uid 下
ffmpeg 线程创建 EAGAIN（生产仍由 bwrap/cgroup TasksMax 兜底）；② 方言红线把任意 `.decode(` 方法调用
视为库调用剔除，裸 `DECODE(col, ...)` 仍命中，消除 `tail()` 的误报。

M2.5 生产化产物（TicketedHls + 进度网关，F2/F3/F4）：`migrations/versions/0007_learn_progress.py`
（`learn_progress_log` / `learn_progress` / `learn_progress_gate` + `media_keysession`）、
`zhyonlineedu/core/db/models/learn.py`、`zhyonlineedu/core/media/{keys,playlist}.py`（主密钥派生、
票据签名、m3u8 重写与档位过滤）、`zhyonlineedu/modules/learn/playback.py`（票据/playlist/key/分片四链路）、
`zhyonlineedu/core/progress/{bus,gateway,flush}.py`（LocalBus JSONL ring + 单写者租约 + `GREATEST` 幂等投影）、
`zhyonlineedu/api/learn.py`（6 端点）、`zhyonlineedu/workers/progress_flush.py`、`tests/test_progress.py`（36 条）。
验收口径「无 ticket 拿不到 key」端到端闭合（缺参/伪造签名/串课/过期/重放分别 40201/40203/40206）。
打磨点：① spike 的 `gw_seq` 时间戳编码改为 gate 行原子自增，避免跨机器时钟回拨；② 单写者由 flock 改为
DB 租约（多进程共享同一 ring 卷时仍只有一个 flush 写者）；③ key/playlist/分片不套统一信封，`@documented`
新增 `media_type` 以在 OpenAPI 中如实声明原始媒体类型；④ `RedisBus` 在 M2.5 显式拒绝，`progress.bus=redis-stream`
由配置交叉校验直接报错（M3 再接）。

M2.6 生产化产物（题库判分 + Idempotency-Key，F5）：`migrations/versions/0008_quiz.py`（`quiz_question`
含 D19a 软删复合唯一键、`quiz_attempt` 含 `attempt_uid` 唯一索引）、`zhyonlineedu/core/db/models/quiz.py`、
`zhyonlineedu/core/idempotency.py`（头校验 + 规范化 payload 指纹）、
`zhyonlineedu/modules/assess/service.py`（快照冻结判分 + 条件更新幂等）、`zhyonlineedu/api/quiz.py`（8 端点）、
`tests/test_quiz.py`（23 条）。验收口径闭合：① 学员端响应递归扫描无 `answer|answer_key|correct_answer`；
② 同 key 同 payload 重放返回首次成绩（`created=false`），同 key 异 payload / 异 key 二次提交 409，并发
`rowcount=0` 时回读首次结果。打磨点：① 幂等键即 `attempt_uid`，与服务端唯一索引同源，无需额外幂等表；
② 判分只读 `answer_key` 快照，改题/软删不影响在途作答；③ 判断题选项由服务端固定 T/F，杜绝"双正确"。

M2.7 生产化产物（用户 / 班级 / 导入 / 过期）：`migrations/versions/0009_users_classes.py`
（`account_user` 补 `org_id`/`user_no`/`expire_at`/乐观锁/软删列 + `uq_account_user_no_deleted`，
新增 `edu_class`/`edu_class_member`/`sys_import_batch`）、`zhyonlineedu/core/db/models/{account,users}.py`、
`zhyonlineedu/modules/users/{service,importer}.py`（乐观锁、停用/过期吊销、班级成员授权、report_only 导入）、
`zhyonlineedu/api/users.py`（13 端点）、`zhyonlineedu/workers/expire.py`、`tests/test_users.py`（16 条）。
验收口径闭合：① 13 接口统一 `user:manage`，teacher/student 逐一 403 `PERMISSION_DENIED`；
② 默认 `report_only` 冲突行只记 `skipped`，原 `display_name`/`password_hash`/`version` 不变，显式 `overwrite`
才更新；③ `class` 课程授权以任一启用班级成员判定，退班/停班使 `access_hash` 失配，旧票据 40204。
打磨点：① 迁移回填历史 `user_no` 改为在线集合式 UPDATE（离线 COMPILE_ONLY 无连接且依赖现有数据，不生成该步）；
② `drop_constraint` 的类型参数改位置传参，避免迁移红线脚本把约束类型误判为列类型变更；
③ 停用/过期先吊销会话，刷新因此返回 `AUTH_TOKEN_REVOKED`（登录仍返回 `ACCOUNT_DISABLED`/`ACCOUNT_EXPIRED`）。

M2.10~2.11 生产化产物（双模式 + 切换向导，§9）：`migrations/versions/0010_modes.py`（`sys_mode_state`
单行 epoch + `account_user.registration_mode`/`migration_batch_id` + `learn_progress_log.quarantined` 与索引，
纯新增）、`zhyonlineedu/core/mode/{context,policy}.py`（`ModeContext` 请求内只读一次 + 进程 TTL 缓存、
18 条 `DECISION_TABLE` 与 `decide_access`/`decide_ticket`）、
`zhyonlineedu/modules/{learn/playback,assess/service}.py` 与 `core/progress/gateway.py`（播放/测验/进度三处
统一走判定表，课程 mode 不匹配一律 `MODE_COURSE_MISMATCH`）、`zhyonlineedu/modules/config/service.py`
（预检 8 项 + 切换事务：epoch+1、preview 流水隔离、自注册账号按批次停用、审计、会话吊销）、
`zhyonlineedu/api/system.py`（3 端点，`system:manage`）、`tests/test_modes.py`（19 条）。
验收口径闭合：① `tests/test_modes.py` 参数化覆盖 MODE-001~018，`DECISION_TABLE` 行数断言 18（未进表=未实现）；
② MODE-FLIP-001 在同一请求内直接改库模拟"他进程切换"，断言仍用旧 epoch 完成、下一请求读新 epoch；
③ 存量票据跨切换仍可取 key（TTL 内不失效），新签发按新模式策略；
④ 切换报告含 `quarantined_logs`/`affected_users`/`revoked_sessions`，epoch 过期 409 `MODE_SWITCH_BLOCKED`
（`reason=epoch_conflict`），compat 漂移阻断并要求 `manage.py db probe --persist`。
打磨点：① archived 在 `playback.issue()` 的 preview 早退会返回 403，删除早退统一由 MODE-013 判 404；
② `ticket.code` 是 `Code` 数值成员，`TicketDecision.code` 用 `Code | str | None`，与 40203/40206 信封同源；
③ 预检 compat 只在"已固化 caps 与运行时冲突"时阻断，缺键不阻断，避免空库测试恒阻断；
④ 模式策略 TTL 与全局 TTL 取小（只收紧不放宽），既有 MEDIA 用例不改口径。

M2.12 生产化产物（会员订单 / 支付回调 / 订阅授权，状态不倒退，§10.6）：
`migrations/versions/0011_commerce.py`（`member_level` 软删复合 UK、`pay_order` 订单快照、
`member_subscription`（`order_no` 唯一）、`pay_event`（`event_id` 唯一）+ `member:manage` 种子）、
`zhyonlineedu/core/db/models/commerce.py`（`Money(18,6)` + `dec()`，金额全链路十进制字符串）、
`zhyonlineedu/modules/commerce/service.py`（`active_sub()`/`active_level()`/`grant_level()` 与
`CommerceService` 档位/订单/回调/补偿/过期）、`zhyonlineedu/api/commerce.py`（12 端点）、
`zhyonlineedu/modules/{learn/playback,assess/service}.py`（会员等级进入 `AccessSpec` 与 `access_hash`）、
`zhyonlineedu/workers/expire.py`（订单/订阅过期接线）、`tests/test_commerce.py`（17 条）。
验收口径「状态不倒退」闭合：回调与查单补偿共用 `_apply_event/_transition`，条件更新
`WHERE status IN ('created','paying') AND version=:v`，`rowcount=0` 回读判「重复成功」或 409
`PAY_STATE_INVALID`——`paid` 不可回退 `paying`（PAY-STATE-001~004）；`event_id` 唯一 + `order_no`
唯一保证重复/并发回调只发一次会员。打磨点：① ORM-enabled UPDATE 默认 `synchronize_session`
会把 `version` 表达式回写到内存对象，随后再手工 `version+1` 会在 flush 时双增（数据库出现
3 而非 2），改为 `synchronize_session=False` + 显式同步内存值；② 回调金额先量化为 6 位小数
再参与签名与比较，避免 `"30"` / `"30.000000"` 口径不一致；③ 会员到期/升级使 `access_hash`
中的 `|m{level}` 失配，旧票据立即 40204，无需轮换会话。

M2.13 生产化产物（学习档案 / 课程投影 / 口径版本，§10.7 / §15.2 / §23.5）：
`migrations/versions/0012_learning_profile.py`（新增 `learn_course_progress`（`UNIQUE(user_id, course_id)`）
并按存量 `learn_progress` + `edu_course_lesson` 在线回算一次档案，离线 COMPILE_ONLY 跳过依赖数据的回填）、
`zhyonlineedu/core/db/models/learn.py`（`LearnCourseProgress`）、
`zhyonlineedu/core/progress/flush.py`（单课投影后聚合课程投影，log 聚合追加 `quarantined = false`，
`watch_seconds/speed_time` 仍只走 `GREATEST`）、`zhyonlineedu/modules/learn/profile.py`
（`ProfileService`：列表/详情/汇总/水位自诊 + `freshness_seconds`）、`zhyonlineedu/api/learn.py`
（`GET /my/progress`、`GET /my/progress/{course}`、`GET /my/summary`、`GET /my/progress/gate`）、
`tests/test_profile.py`（7 条）+ `tests/test_progress.py`（36 条，既有用例扩写课程投影断言）。
验收口径闭合：① 预览不进任何统计——`source='preview'` 只打 `flushed_at` 不投影，被切换隔离的 preview
流水同样被 `quarantined = false` 过滤，超管预览后课程档案为空；② 四个读接口与票据响应均带
`metric_version="zhy-progress-v2"`，课程投影的 `percent = 完成课时 / 总课时`、`completed = 全课时完成`；
③ `freshness_seconds` = 最老未落库有效流水距今秒数（无积压为 0），前端据此标注「更新于 X 分钟前」；
④ `GET /my/progress/gate` 为 `system:manage` 自诊口，返 `last_gw_seq`/`flushed_max_gw_seq`/`pending_logs`/
`gap`/`lag_seconds`/`healthy`（容差 60s 或 3×`flush_interval_sec`）。打磨点：① 逐课投影写库后按
`(user, course)` 去重再聚合课程投影，同一批流水只算一次；② 详情只下发已学课时，避免向学员泄露未学结构；
③ 迁移回填必须判 `ctx.as_sql`（离线脚本无可执行连接，与 M2.7 `user_no` 回填同口径）；
④ 今日时长直查 `learn_progress_log` 并显式 `source='play' AND quarantined=false`，不依赖投影新鲜度。

M2.14~2.15 生产化产物（管理端 + 学员端 + 移动五态，§14 / §23）：
`zhyonlineedu/modules/learn/catalog.py`（`StudentCatalog`：仅 `published` + 当前模式可见，叠加课程投影与
`access_ok`）、`zhyonlineedu/api/{learn,commerce}.py`（`GET /learn/courses`、`GET /learn/courses/{id}`、
`GET /member/offers`）、`web/src/views/`（学员端 6 页 + 管理端 5 页）、
`web/src/media/{TicketedHls,ProgressReporter}.ts`（换票/原生 query 票据/30s 心跳 + `pagehide` keepalive）、
`web/src/components/`（`StateView` 五态 / `PreviewBadge` / `SourceTag` / `ShowcaseTips` / `AdminMobileGate`）、
`tests/test_learn_catalog.py`（3 条）、前端 48 条（7 files）。
验收口径闭合：① `/admin` <640px 只提示并可复制桌面链接，不重定向；学员端五态齐全、首屏 ≤2 请求；
② 课程目录只含 `published` 且当前模式可见课程，进度与 `access_ok` 与档案同源；draft 仅 `course:edit` 预览；
③ 后端 372 passed + 34 skipped、前端 48 passed、`npm run build` 通过（仅 chunk 体积警告）。

**本地实测记录（2026-10-30，M2.14 收尾）**：临时 SQLite + 真实安装向导（token → db/test → db/setup →
admin → mode → finish）后以 `InstallProbe` 重启进入 READY；建学科/课程/章/课时 → 分片上传
（sha256/md5/断点）→ 检疫合并 → 转码 worker → 上架 → 学员取票 → master/variant/key/segment 全链路
→ 进度上报 + `progress_flush --once` → `/my/summary`、`/my/progress`、`/my/progress/1`、
`/my/progress/gate` → 题库 3 题作答（单选/多选/判断）与同 key 重放（200 `created=false`，成绩不重算）
→ 会员档位/订单创建/列表/取消 → 课程切 `member` + 课时 `is_free=false` 后取票 403 `ACCESS_LEVEL_REQUIRED`
→ `POST /member/grant` 后取票成功且课程卡 `access_ok=true`。结果：`ALL E2E CHECKS PASSED: 37 steps`
（逐项结果存 `/tmp/zhy_m214/e2e-results.json`，样例素材为 ffmpeg 合成 6s/640x360 视频）。本机 bwrap
创建 namespace 报 `Resource temporarily unavailable`，故按 `transcode.sandbox=disabled`（仅 dev 允许）执行。

**M2.4 缺陷回写（实测暴露并修复）**：HLS 分片是 AES-128-CBC 密文，`ffprobe` 直接探测恒为 `unknown`，
产物二次校验据此判 `ENC-1003 codec_not_allowed`，转码永远失败。修复：
`core/media/hls.py::parse_variant` 解析 `#EXT-X-KEY` 的 `IV`（`Variant.iv`，半截声明
`key_declaration_invalid`）；`core/media/keys.py::decrypt_aes128_cbc`；
`modules/media/transcode_service.py` 用任务注入 key + IV 解密首片到 `codec-probe/` 供探测并在
`finally` 删除（发布目录实测仅 `360/720/poster/master.m3u8`）。回归：`tests/test_transcode.py`
新增 IV 解析 / 半截 IV / 解密往返 / 解密探测 / 加密产物端到端转码 5 条。

**M2.4 缺陷回写二（1080p 转码 `ENC-1002`，实测暴露并修复）**：上传 92MB / 1920×1080 / 144s 视频，
两次转码均在首帧初始化失败（`ENC-1002`，stderr 尾部 `x264 [error]: malloc of size 11453504 failed`，
退出码 -11/SIGSEGV）。复现与归因：`SandboxRunner(mem_max_mb=2048, backend=disabled)` 跑 1080 档
0.5s 即崩；同一命令在 shell 里 `ulimit -v 2097152` 也崩，加 `-threads 4/6/8` 均成功，
缺省线程（宿主 8 核 → x264 12 线程）失败；把地址空间抬到 2560MiB 亦成功。根因：
`_limits()` 恒设 `RLIMIT_AS = mem_max_mb × 1MiB`，而 ffmpeg 按**宿主核数**派生 x264 线程，
1080p 帧缓冲/前瞻队列随线程数膨胀，最终撞上 2GiB 地址空间上限。修复：
`core/media/runtime.py` 新增 `MAX_ENCODE_THREADS = 8` 与 `encode_threads(cpu_max_percent)`
（每 50% CPU 配额 1 线程、封顶 8，默认 200% → 4），`build_hls_command(spec, threads=...)` 在
`-profile:v main` 后注入 `-threads`（越界值钳制），`SandboxRunner._inner_command` 按 CPU 配额传入；
poster 命令维持原样（实测 2048MiB 内正常）。实测：4 线程 1080p 全片 50s 完成（2048MiB 内）；
服务层重跑三档 100.5s 成功，`asset=encoded`、`playlist_version=3`、master 三档齐全、
variant `#EXT-X-KEY` 带显式 `IV`。回归：`tests/test_transcode.py` 新增线程推导 / 命令注入与钳制 /
runner 派生 3 条（整文件 29 条）。

**播放缺陷回写（学员端「网络连接失败」，实测暴露并修复）**：访问
`/learn/lessons/1?course=1` 后页面进入 error 态并显示「网络连接失败」。复现：后端 5000 / Vite 5173
均在监听、代理正常，课程详情与 `POST /media/tickets` 均 200；按 hls.js 的请求顺序直连：
`master(200) → variant[360](200) → key(200) → segment(200)`，但**再次请求 `variant[720]`（ABR 切档，
直接复用 master 里的 URI）返回 `401 {"code":40201,"data":{"reason":"playlist_stale"}}`**。
根因：`PlaybackService.playlist()` 旧实现每次请求都 `_rotate()` 轮换 nonce，而 master 一次下发
三档 URI、播放器切档不会重新拉 master，导致 master 中除第一档外的 URI 立即失效；hls.js 将其归为
hls.js `networkError` → `TicketedHls.refresh()`（最多 2 次）→ 耗尽后 `fail()` → `describeError`
映射为「网络连接失败」。二次缺陷：前端 `refreshes` 计数永不复位，长视频每 100s 定时续签计一次，
第三次续签即被误判为不可恢复。
修复：① `playback.playlist()` 在 playlist 有效期内固定 `nonce = row.nonce`，各档位/重复拉取签名一致；
`#EXT-X-KEY` 的 `exp` 固定为 `issued_at + ttl_key`（不再用请求时刻），同 ksession 下 key URI 字节一致，
hls.js 可按 URI 缓存密钥；删除 `_rotate()`；更新模块 docstring。② `web/src/media/TicketedHls.ts`
新增 `refresh({scheduled})`：定时续签不计入 `MAX_REFRESH`；错误换票记录位置，`timeupdate` 中
`readyState>=3 && currentTime > refreshAtPosition + 5` 时把预算复位。
回归与实测：`tests/test_modes.py` 新增 `test_master_variant_urls_survive_abr_switch`（master 三档 URI
+ master 重复拉取 + key 复用均 200）；`tests/test_progress.py::test_playlist_url_reusable_and_old_ticket_expires`
改写为「同票 URL 可复用 + 新票 supersede 后宽限期外 401 `session_superseded`」；前端
`ticketed-hls.test.ts` 新增 3 条（定时续签不耗预算 / 错误换票耗尽 / 恢复 5s 复位）。真实环境重启后端后
直连验证：新票 master 200 → 360/720/1080 变体 200 → master 重复 200 → key 200 且窗口内重复 200（16 字节）
→ 三档 `seg_000.ts` 200（573KB / 1.39MB / 2.75MB）。

**体验缺陷回写（清晰度切换缺失 + 前端技术文案外泄，人工验收暴露并修复）**：播放页只有播放控制，
无法选择清晰度；同时多个页面把《技术开发计划书》口径直接呈现在普通用户面前（登录页
「AT 仅存内存，刷新页面由 HttpOnly Cookie 中的 RT 静默恢复（M1.6）」、模式切换页
「切换只改 `sys_mode_state` 单行（epoch+1），不改表结构；请求内只读一次 epoch，在途请求不半 half」、
管理端首页「权限来自 `sys_role_permission`；`progress:direct` 不授予内置角色（计划书 §7.3）」等）。
修复：①**清晰度**——`web/src/media/TicketedHls.ts` 新增 `preferredProfile`（`null` = 自动）、
`supportsProfileSwitch`（原生 HLS 路径不支持，返回 false）、`setProfile()` 与 `applyPreferred()`
（按 `level.height === Number(profile)` 映射到 hls.js 档位索引，找不到回落自动），在
`MANIFEST_PARSED` 回调内 `resume → applyPreferred → play` 顺序套用，并在 `refresh()` 换票重载后
再次套用同一选择（用户选择不丢）；`PlayerView.vue` 状态栏新增「清晰度」`el-radio-group`
（自动 / `profiles` 各档位 `N P`），`switchSupported && profiles.length > 0` 时才渲染，
`destroy` 时复位。②**文案**——删除仅用于演示的「框架页」（`HomeView.vue` 及其路由与导航）；
新增 `web/src/utils/labels.ts` 统一中文化（角色 / 模式 / 课程模式 / 资产状态 / 用户状态 / 订单 /
订阅 / 题型 / 批次 / 导入冲突与行动作）；`ModeSwitchView` 去掉 epoch tag、迁移批次、票据 TTL 与
`告警` 术语，检查项走 `checkLabel/checkStatusLabel`（ok → 通过、warn → 提醒、blocked → 阻断）；
`Login` / `AppShell` / `AdminHome` / `MyProgress` / `Catalog` / `AdminMedia` / `AdminUsers` /
`AdminQuiz` / `AdminMember` / `Member` / `Quiz` / `Install` / `LearnHome` / `AdminMobileGate` /
`CourseView` / `PlayerView` 同步改为业务语言（`媒体资产`→`媒体文件`、`查单补偿`→`订单查询`、
`挂载资产`→`上传视频`、`播放票据`→`播放凭证`）；后端 `modules/config/service.py` 预检 8 条消息
同步中文化（去掉 `mode_supported` / `compat_flags` 等内部标识与 `blockers` 术语）。
验证：`npm test` 9 files / 63 tests 通过（新增 `labels.test.ts` 5 条、`ticketed-hls.test.ts` 切档 2 条、`system.test.ts` 检查项文案 2 条、`format.test.ts` 断言同步
「播放凭证」）；`npm run build` 通过；`.venv/bin/python -m pytest` 全量 376 passed / 34 skipped
（127.9s）；`ruff` / `black` / `mypy` 对改动后端文件通过。

**数据一致性回写（课时时长改为视频派生，人工输入下线）**：此前「新增课时」要求管理端手填时长
（默认 9 分钟，前端乘 60 提交 `duration_sec`），与真实视频无关，直接污染学习完成率（进度 gateway
用 `lesson.duration_sec` 作为位置上限、`progress-flush` 用它算完成率）。修复：①**上传即派生**——
`modules/media/upload_service.py::_touch_asset` 在合并与探测成功后把 `info.duration_sec` 写入
`edu_course_lesson.duration_sec`（魔数兜底探测为 0 时保留原值，避免清空历史值）；②**转码发布校正**——
`modules/media/transcode_service.py` 发布成功时用 `media_asset.duration_sec` 覆盖课时时长，
覆盖历史人工值；③**入口下线**——`modules/catalog/service.py` 的 `create_lesson` / `update_lesson`
不再读取 `duration_sec`（创建固定 0，编辑忽略），`api/schemas.py` 的 `LessonCreateIn` /
`LessonUpdateIn` 同步移除该字段并重新生成 `web/src/api/openapi.json` 与 `types.ts`；
④**前端**——`CatalogView` 删除时长输入框，课时列表改为「时长」列（`formatDuration`，无视频显示 `—`），
`stores/catalog.ts::addLesson` 去掉时长参数，成功提示改为「已上传视频后自动识别时长」；
⑤**存量回填**——新增迁移 `0013_lesson_duration`（数据迁移，无表结构变更），按
`media_asset.duration_sec > 0` 回写未删除课时，并同步 `schema_rev`。
回归与实测：`tests/test_catalog.py` 断言「回传 `duration_sec: 600` 仍为 0」；
`tests/test_media_upload.py` 断言上传完成后课时时长 = 42（魔数兜底时保留 0）；
`tests/test_transcode.py` 新增 `test_publish_rewrites_legacy_manual_lesson_duration`
（历史人工值 999 → 发布后回到 8）；迁移测试 `HEAD_REV` 更新为 `0013_lesson_duration`
（`check_migration_caps` 13 个 revision OK）。全量 `.venv/bin/python -m pytest`：**377 passed / 34 skipped**；
前端 `npm test` **63 passed**、`npm run build` 通过；本地实测 `var/zhy.db` 升级到 0013 后，
课时 2 的时长由人工 480 变为视频探测的 144，`GET /courses/1` 返回 `duration_sec=144`。

## 十一、M2.16 压测窗口与 M2 收尾验收（2026-10-31）

**环境**：Linux x86_64 · Python 3.11 · SQLite 3.46（WAL + `synchronous=NORMAL` + `busy_timeout=5000`，
`connect_args timeout=15`）· gunicorn 23.0.0 `-w 4 -k gthread --threads 16`（入口 `var/wsgi.py`，
`var/` 已 gitignore）· `python -m zhyonlineedu.workers.progress_flush` 单写者独立进程。

### 11.1 用例（F2 / F3+F4 关闭条件）

| 组 | 命令 | 结果 |
|---|---|---|
| HBT-001~006 | `.venv/bin/python -m pytest -m hbt -q` | **20 passed**（双网关收敛、kill -9 后 DB 补齐、lease 单写者、重放幂等、乱序取大、100 QPS 对账） |
| MEDIA-001~008 | `.venv/bin/python -m pytest -m media -q` | **19 passed**（playlist TTL 换票、key nonce 重放、ksession 换课 MISMATCH、preview 不计入、fallback、分片长 TTL、原生 HLS 取 key、会员降级收缩） |

### 11.2 HBT-006 实测（§16.4 用例⑥）

```bash
ZHY_PERF_USER_PREFIX=perf7 .venv/bin/python tests/perf/seed_progress.py --users 120
ZHY_PERF_USER_PREFIX=perf7 .venv/bin/locust -f tests/perf/locustfile_progress.py \
    --headless -u 120 -r 20 -t 5m --csv var/perf/progress_perf7 --stop-timeout 20
ZHY_PERF_USER_PREFIX=perf7 .venv/bin/python tests/perf/check_hbt006.py \
    --expected var/perf/progress_expected.json
```

- `POST /learn/progress`：**35122 请求 / 0 失败 / 117.1 RPS**，P50 23ms · P95 100ms · P99 560ms
- `POST /auth/login`：120 请求 / 0 失败（`-r 20` 分摊；Argon2 并发下 P50 3.1s）
- 对账：`用户 120 / 上报 35122 条 / 未落库 0 条 / gw_seq 跨度 35122 / 投影对账 OK` → **HBT-006 PASS**
  （`gw_seq` 无缺口、每用户 `seq_no` 连续、`learn_progress.watch_seconds` 与客户端累计误差 0）
- 压测期间日志仍出现 `progress 写入等锁超时，重试 1/3`（3 次）与 `登录写会话等锁，重试 1/3`（2 次），
  全部由退避重试吸收，**对外失败 0**。

### 11.3 播放回源（`locustfile_play.py`）

`-u 60 -r 20 -t 1m`：3591 请求 / 0 失败 / 60 RPS；分片 2988 次 P50 6ms · P95 12ms · P99 60ms，
取票 60 次 P50 610ms。分片是 `public + 长 TTL`，生产中由 CDN 承载，**回源比需在部署环境按 CDN 日志统计**
（本地不伪造该指标）。

### 11.4 压测暴露并修复的缺陷（4 个）+ 客户端计量修正

1. **flush 单事务持锁 > 5s → 上报 500**（100/150 用户各 18 次 `database is locked`）。根因：
   `ProgressFlush.flush_once` 用一个事务处理 1000 行并逐用户重算投影，SQLite 写锁持有超 5s，
   网关 `UPDATE learn_progress_gate` 等锁 5.36s 后超时。修复：按 `(user, lesson, course)` 分组、
   **每组一个短事务**（同一 flush 内同用户多行只重算一次投影）；「绝对值重算 + GREATEST 不回退 +
   replay 安全」的不变式不变（`core/progress/flush.py`）。
2. **默认 `flush_batch=200 / flush_interval_sec=15` 吞吐不足**：200 行/15s ≈ 13 行/s，
   100 QPS 上报 5 分钟积压 14417 行未落库。改 `1000 / 5s`（200 行/s，2 倍余量），
   `core/constants.py` 冻结常量与 `defaults.toml` 同步。
3. **gunicorn 多 worker 启动期能力探测自伤**（seed 从第 30 个账号起 `409 INSTALL_REQUIRED`）：
   4 个 worker 共用探测表 `zhy_probe_caps`，A 探测完 drop 表会打断 B 正在执行的 UPSERT 冒烟，
   能力位被误判「不可用」。改为按进程隔离 `probe_table_name()`（`zhy_probe_caps_<pid>`），各自建表/清理
   （`core/db/probe.py`，`tests/test_probe.py` 断言同步）。
4. **登录 `database is locked` → 500**（120 次登录 3 次失败）：`login()` 把
   「SELECT user → Argon2（≈0.5s）→ INSERT account_session」放在同一读事务里，期间任何写者提交
   都会让事务升级为写时直接 `SQLITE_BUSY_SNAPSHOT`（`busy_timeout` 对升级**无效**）。修复：
   「短读会话取 `id/password_hash` → 事务外 Argon2 → 短写会话复查状态后签发」，写会话撞锁退避重试 3 次；
   `change_password` 同样两段式；锁判定抽到 `core/db/engine.py::is_lock_error`，网关复用同一实现。
5. **压测客户端计量失真**（对账「每用户 +1」的假缺口）：locust 退出时仍有在途请求（服务端已提交、
   快照未累计）。修复：`_INFLIGHT` 计数 + `quitting` 事件等待在途请求收干 + `--stop-timeout 20`；
   并把 `constant(1)` 改 `constant_pacing(1)`（100 用户才真到 100 QPS）、瞬时登录改 `-r 20` 分摊 +
   失败退避重试 3 次（只停该虚拟用户，不再 `runner.quit()` 终止整场）。

### 11.5 结论

§13 的「gunicorn 4×16 gthread ≈ 250 QPS」在 SQLite WAL 下未实测到：本机瓶颈先落在 flush 写锁与
Argon2 登录，稳定值 ≈117 QPS（>100 QPS 目标）；250 QPS 属 PostgreSQL/MySQL + 独立 worker 口径，
M3 联调时复测。M2 收尾（F1~F4）用例全绿（HBT 20 / MEDIA 19），红线三件套
（方言兼容·进度单调·迁移能力 13 revision）与 `export_openapi.py --check` 一并通过。
