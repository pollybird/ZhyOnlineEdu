"""系统级常量（计划书 §26.3，V2.3 冻结）。

命名红线：常量全部 `ZHY_` 前缀，≤64 字符；表/索引名 ≤24 字符（§26.4~26.7）。
"""

# ── 进度网关（F2）──
ZHY_PROGRESS_REPORT_INTERVAL_SEC = 30
ZHY_PROGRESS_FLUSH_BATCH = 200
ZHY_PROGRESS_FLUSH_INTERVAL_SEC = 15
ZHY_PROGRESS_RING_DIR = "/run/zhy/progress"  # 可被 ZHY_PROGRESS_RING_DIR 覆盖
ZHY_PROGRESS_RECOVERY_MAX_GAP = 10_000  # gw_seq 缺口告警阈值
ZHY_PROGRESS_LEASE_TTL_SEC = 30
ZHY_PROGRESS_SOURCE_PREVIEW = "preview"  # 不计入统计
ZHY_PROGRESS_METRIC_VERSION = "zhy-progress-v2"
ZHY_PROGRESS_COMPLETION_THRESHOLD = 90  # percent ≥ 90 且位置连续到结尾才判完成

# ── 题库判分（F5 · M2.6）──
ZHY_QUIZ_PASS_SCORE = 60  # 及格线（百分比）；交卷判 `passed` 的唯一来源

# ── 双模式（§9.3 / §9.6 · M2.10）──
ZHY_MODE_CACHE_TTL_SEC = 30  # epoch 快照缓存窗口（全站生效 ≤30s）
ZHY_MODE_QUARANTINE_WINDOW_SEC = 3600  # 切换窗口内 preview 流水隔离范围
ZHY_MODE_TICKET_WARN_THRESHOLD = 1000  # 有效票据数超过则切换报告告警

# ── 用户 / 班级 / 导入 / 过期（M2.7）──
ZHY_USERS_ON_CONFLICT_ACTION = "report_only"  # report_only | overwrite；默认不覆盖
ZHY_USERS_IMPORT_MAX_ROWS = 5000
ZHY_USERS_EXPIRE_HOUR = 2  # 每日 02:00 执行账号/会员过期
ZHY_USERS_EXPIRE_BATCH_SIZE = 500

# ── 会员订单 / 支付回调（M2.12 · §10.6）──
ZHY_PAY_CALLBACK_SECRET = "dev-only-callback-secret"  # noqa: S105 - dev 占位密钥，非真实凭据
ZHY_PAY_ORDER_TTL_SEC = 1800  # 订单支付窗口；超时由 worker 置 expired
ZHY_PAY_MAX_OPEN_ORDERS = 3  # 单用户未关闭订单上限（防刷单）

# ── 票据与三层 TTL（F3/F4/F8）──
ZHY_TICKET_KSESSION_BYTES = 16  # 128 bit
ZHY_TICKET_NONCE_BYTES = 12
ZHY_TICKET_SIGN_KEY_BYTES = 32  # 与媒体主密钥 HKDF 派生的票据签名密钥
ZHY_MEDIA_TTL_TS_SEC = 86400 * 3
ZHY_MEDIA_TTL_PLAYLIST_SEC = 900
ZHY_MEDIA_TTL_KEY_SEC = 120
ZHY_MEDIA_TICKET_SLACK_SEC = 20  # 前端预换票提前量
ZHY_MEDIA_KSESSION_STRICT_IP = False  # F8：默认软信号
ZHY_MEDIA_CDN_BASE = ""  # 为空时 playlist 走应用相对路径（无 CDN 部署）
ZHY_MEDIA_NONCE_REUSE_WINDOW_SEC = 30  # key nonce 复用窗口（容忍播放器重复请求）
ZHY_MEDIA_SUPERSEDED_GRACE_SEC = 120  # 旧 keysession 在换票后的宽限期

# ── 转码沙箱（F7）──
ZHY_TRANSCODE_SANDBOX = "bwrap"  # bwrap | cgroup_systemd | disabled(仅 dev)
ZHY_TRANSCODE_CPU_MAX_PERCENT = 200  # cgroup v2
ZHY_TRANSCODE_MEM_MAX_MB = 2048
ZHY_TRANSCODE_PIDS_MAX = 16
ZHY_TRANSCODE_INPUT_MAX_DURATION_SEC = 14400
ZHY_TRANSCODE_INPUT_MAX_DIM = 4096
ZHY_TRANSCODE_ALLOWED_FORMATS = ("mp4", "mov", "mkv", "flv", "webm", "mp3", "aac")
ZHY_TRANSCODE_PROFILES = ("360", "720")
ZHY_TRANSCODE_SEGMENT_SEC = 4
ZHY_TRANSCODE_POSTER_SEC = 3
ZHY_TRANSCODE_MAX_ATTEMPTS = 2
ZHY_TRANSCODE_RETRY_BACKOFF_SEC = 60
ZHY_TRANSCODE_WORKER_POLL_SEC = 3
ZHY_TRANSCODE_QUEUE_BATCH = 4
ZHY_TRANSCODE_FFPROBE_TIMEOUT_SEC = 15
ZHY_TRANSCODE_TASK_TIMEOUT_SEC = 3600  # 单任务上限；崩溃回收也以此为准
ZHY_TRANSCODE_PAUSED = False  # CVE 命中/人工停任务：只拦新入队
ZHY_FFMPEG_CVE_PAUSE_ON_HIT = True
ZHY_FFMPEG_SBOM_PATH = "./deploy/ffmpeg/zhy-ffmpeg-sbom.json"

# ── 分片上传与 tmp 安全（§10.3.1）──
ZHY_UPLOAD_TMP_ROOT = "./var/tmp/uploads"
ZHY_UPLOAD_CHUNK_SIZE = 8 * 1024 * 1024
ZHY_UPLOAD_MAX_FILE_BYTES = 8 * 1024**3
ZHY_UPLOAD_MAX_CHUNKS = 4096
ZHY_UPLOAD_SESSION_TTL_SEC = 86400
ZHY_UPLOAD_TMP_TTL_SEC = 600  # 合并后 10 分钟未消费由 tmp-reaper 清理
ZHY_UPLOAD_REQUIRE_FFPROBE = False  # 非 dev 环境强制为 true

# ── 方言能力位（与 sys_compat_flags.cap 一一对应，§5.4）──
ZHY_CAP_RETURNING = "returning"
ZHY_CAP_UPSERT_CONFLICT_CLAUSE = "upsert_conflict_clause"
ZHY_CAP_UPSERT_ON_DUPLICATE = "upsert_on_duplicate"
ZHY_CAP_SOFT_DELETE_COMPOSITE = "soft_delete_composite"
ZHY_CAP_NATIVE_JSON = "native_json"
ZHY_CAP_PARTIAL_INDEX = "partial_index"
ZHY_CAP_ALTER_TYPE_ONLINE = "alter_type_online"
ZHY_CAP_BULK_RETURNING_IDS = "bulk_returning_ids"

# ── HTTP ──
ZHY_HEADER_REQUEST_ID = "X-ZHY-Request-Id"
ZHY_HEADER_INSTALL_SESSION = "X-ZHY-Install-Session"
