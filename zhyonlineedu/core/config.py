"""配置三层：默认值（defaults.toml）→ 环境变量（ZHY_*）→ sys_config（M1.5 落地）。

设计约束（§4.3）：
- 默认值写在代码外，随 wheel 打包；
- bootstrap 键只能来自环境变量（否则"连库的配置由库决定"会成环）；
- 启动即校验，失败抛 ConfigError 并逐条列出 key/source/expected；
- 不可变快照；仅白名单键允许 reload；
- 密钥与 DSN 口令在 repr/redacted 中脱敏。
"""

from __future__ import annotations

import logging
import os
import re
import tomllib
from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from importlib.resources import files
from typing import Protocol

from zhyonlineedu.core.constants import ZHY_PAY_CALLBACK_SECRET

logger = logging.getLogger(__name__)

_DEFAULTS_PACKAGE = "zhyonlineedu.core"
_DEFAULTS_FILE = "defaults.toml"
_SECRET_MASK = "***"  # noqa: S105 - 脱敏占位符，非密钥


class ConfigError(Exception):
    """配置校验失败。problems 为逐条可读错误，进程应以退出码 2 终止。"""

    def __init__(self, problems: list[str] | str) -> None:
        self.problems: list[str] = [problems] if isinstance(problems, str) else problems
        super().__init__("; ".join(self.problems))


class Kind(StrEnum):
    STR = "str"
    INT = "int"
    BOOL = "bool"
    LIST = "list"
    ENUM = "enum"


@dataclass(frozen=True, slots=True)
class ConfigKey:
    key: str
    env: str
    kind: Kind
    secret: bool = False
    bootstrap: bool = False
    hot: bool = False
    choices: tuple[str, ...] = ()
    minimum: int | None = None
    maximum: int | None = None


class ConfigSource(Protocol):
    """sys_config 只读源。M1.1 用 Null 源；M1.5 换成 DB 实现。"""

    def load(self) -> Mapping[str, str]: ...


# 键表（M1.1 冻结）：顺序即文档顺序。
KEY_SPECS: tuple[ConfigKey, ...] = (
    ConfigKey("env", "ZHY_ENV", Kind.ENUM, bootstrap=True, choices=("dev", "t0", "t1", "t2")),
    ConfigKey("db.url", "ZHY_DB_URL", Kind.STR, bootstrap=True),
    ConfigKey("db.flavor", "ZHY_DB_FLAVOR", Kind.STR, bootstrap=True),
    ConfigKey("db.og_version", "ZHY_DB_OG_VERSION", Kind.STR),
    # 安装向导一次性令牌（M1.5）：留空时启动期生成并打印；安装完成后作废。
    ConfigKey("install.token", "ZHY_INSTALL_TOKEN", Kind.STR, secret=True, bootstrap=True),
    ConfigKey("jwt.private_key_path", "ZHY_JWT_PRIVATE_KEY_PATH", Kind.STR, bootstrap=True),
    ConfigKey("jwt.public_key_path", "ZHY_JWT_PUBLIC_KEY_PATH", Kind.STR, bootstrap=True),
    ConfigKey("auth.at_ttl_sec", "ZHY_AT_TTL_SEC", Kind.INT, minimum=300),
    ConfigKey("progress.ring_dir", "ZHY_PROGRESS_RING_DIR", Kind.STR),
    ConfigKey(
        "progress.flush_batch",
        "ZHY_PROGRESS_FLUSH_BATCH",
        Kind.INT,
        hot=True,
        minimum=1,
        maximum=2000,
    ),
    ConfigKey(
        "progress.flush_interval_sec", "ZHY_PROGRESS_FLUSH_INTERVAL_SEC", Kind.INT, minimum=1
    ),
    ConfigKey("progress.lease_ttl_sec", "ZHY_PROGRESS_LEASE_TTL_SEC", Kind.INT, minimum=5),
    ConfigKey("progress.bus", "ZHY_PROGRESS_BUS", Kind.ENUM, choices=("local", "redis-stream")),
    ConfigKey("progress.recovery_max_gap", "ZHY_PROGRESS_RECOVERY_MAX_GAP", Kind.INT, minimum=100),
    ConfigKey(
        "progress.completion_threshold",
        "ZHY_PROGRESS_COMPLETION_THRESHOLD",
        Kind.INT,
        minimum=1,
        maximum=100,
    ),
    ConfigKey("redis.url", "ZHY_REDIS_URL", Kind.STR, secret=True),
    ConfigKey("quiz.pass_score", "ZHY_QUIZ_PASS_SCORE", Kind.INT, hot=True, minimum=0, maximum=100),
    # 双模式（M2.10）：切换后全站生效 ≤ cache_ttl_sec；切换窗口内 preview 流水隔离
    ConfigKey("mode.cache_ttl_sec", "ZHY_MODE_CACHE_TTL_SEC", Kind.INT, hot=True, maximum=300),
    ConfigKey(
        "mode.quarantine_window_sec",
        "ZHY_MODE_QUARANTINE_WINDOW_SEC",
        Kind.INT,
        maximum=86400,
    ),
    ConfigKey(
        "mode.ticket_warn_threshold",
        "ZHY_MODE_TICKET_WARN_THRESHOLD",
        Kind.INT,
        minimum=1,
        maximum=100_000,
    ),
    # 用户 / 班级 / 导入 / 过期（M2.7）：默认 report_only，绝不静默覆盖原值（§10.5）
    ConfigKey(
        "users.on_conflict_action",
        "ZHY_USERS_ON_CONFLICT_ACTION",
        Kind.ENUM,
        hot=True,
        choices=("report_only", "overwrite"),
    ),
    ConfigKey(
        "users.import_max_rows",
        "ZHY_USERS_IMPORT_MAX_ROWS",
        Kind.INT,
        minimum=1,
        maximum=100_000,
    ),
    ConfigKey("users.expire_hour", "ZHY_USERS_EXPIRE_HOUR", Kind.INT, minimum=0, maximum=23),
    ConfigKey(
        "users.expire_batch_size",
        "ZHY_USERS_EXPIRE_BATCH_SIZE",
        Kind.INT,
        minimum=1,
        maximum=10_000,
    ),
    # 会员订单 / 支付回调（M2.12）：secret 非 dev 必须替换；TTL 与未关闭订单上限可运维调整
    ConfigKey("pay.callback_secret", "ZHY_PAY_CALLBACK_SECRET", Kind.STR, secret=True),
    ConfigKey(
        "pay.order_ttl_sec", "ZHY_PAY_ORDER_TTL_SEC", Kind.INT, hot=True, minimum=60, maximum=86400
    ),
    ConfigKey(
        "pay.max_open_orders", "ZHY_PAY_MAX_OPEN_ORDERS", Kind.INT, hot=True, minimum=1, maximum=100
    ),
    ConfigKey("media.ttl_ts", "ZHY_MEDIA_TTL_TS_SEC", Kind.INT, hot=True, minimum=60),
    ConfigKey("media.ttl_playlist", "ZHY_MEDIA_TTL_PLAYLIST_SEC", Kind.INT, hot=True, minimum=60),
    ConfigKey(
        "media.ttl_key", "ZHY_MEDIA_TTL_KEY_SEC", Kind.INT, hot=True, minimum=30, maximum=600
    ),
    ConfigKey(
        "media.ticket_slack",
        "ZHY_MEDIA_TICKET_SLACK_SEC",
        Kind.INT,
        hot=True,
        minimum=5,
        maximum=120,
    ),
    ConfigKey("media.ksession_strict_ip", "ZHY_MEDIA_KSESSION_STRICT_IP", Kind.BOOL),
    ConfigKey("media.cdn_base", "ZHY_MEDIA_CDN_BASE", Kind.STR),
    ConfigKey(
        "media.nonce_reuse_window_sec", "ZHY_MEDIA_NONCE_REUSE_WINDOW_SEC", Kind.INT, minimum=0
    ),
    ConfigKey("media.superseded_grace_sec", "ZHY_MEDIA_SUPERSEDED_GRACE_SEC", Kind.INT, minimum=0),
    ConfigKey("media.aes_key_hex", "ZHY_MEDIA_AES_KEY_HEX", Kind.STR, secret=True),
    ConfigKey(
        "transcode.sandbox",
        "ZHY_TRANSCODE_SANDBOX",
        Kind.ENUM,
        hot=True,
        choices=("bwrap", "cgroup_systemd", "disabled"),
    ),
    ConfigKey("transcode.cpu_max_percent", "ZHY_TRANSCODE_CPU_MAX_PERCENT", Kind.INT, minimum=50),
    ConfigKey("transcode.mem_max_mb", "ZHY_TRANSCODE_MEM_MAX_MB", Kind.INT, minimum=256),
    ConfigKey("transcode.pids_max", "ZHY_TRANSCODE_PIDS_MAX", Kind.INT, minimum=1, maximum=256),
    ConfigKey(
        "transcode.input_max_duration_sec",
        "ZHY_TRANSCODE_INPUT_MAX_DURATION_SEC",
        Kind.INT,
        minimum=1,
    ),
    ConfigKey(
        "transcode.input_max_dimension", "ZHY_TRANSCODE_INPUT_MAX_DIM", Kind.INT, minimum=256
    ),
    ConfigKey("transcode.allowed_formats", "ZHY_TRANSCODE_ALLOWED_FORMATS", Kind.LIST),
    ConfigKey("transcode.profiles", "ZHY_TRANSCODE_PROFILES", Kind.LIST),
    ConfigKey(
        "transcode.segment_sec", "ZHY_TRANSCODE_SEGMENT_SEC", Kind.INT, minimum=1, maximum=30
    ),
    ConfigKey("transcode.poster_sec", "ZHY_TRANSCODE_POSTER_SEC", Kind.INT, minimum=0),
    ConfigKey("transcode.max_attempts", "ZHY_TRANSCODE_MAX_ATTEMPTS", Kind.INT, minimum=1),
    ConfigKey(
        "transcode.retry_backoff_sec", "ZHY_TRANSCODE_RETRY_BACKOFF_SEC", Kind.INT, minimum=1
    ),
    ConfigKey("transcode.worker_poll_sec", "ZHY_TRANSCODE_WORKER_POLL_SEC", Kind.INT, minimum=1),
    ConfigKey(
        "transcode.queue_batch", "ZHY_TRANSCODE_QUEUE_BATCH", Kind.INT, minimum=1, maximum=32
    ),
    ConfigKey(
        "transcode.ffprobe_timeout_sec", "ZHY_TRANSCODE_FFPROBE_TIMEOUT_SEC", Kind.INT, minimum=1
    ),
    ConfigKey("transcode.task_timeout_sec", "ZHY_TRANSCODE_TASK_TIMEOUT_SEC", Kind.INT, minimum=60),
    # CVE 命中/人工停任务时置 true：只拦截新任务入队，已上传文件保留（§10.3.2.3）
    ConfigKey("transcode.paused", "ZHY_TRANSCODE_PAUSED", Kind.BOOL, hot=True),
    # 分片上传（M2.3）：tmp_root 属 bootstrap 键，避免"连库前就要用的路径由库决定"成环。
    ConfigKey("upload.tmp_root", "ZHY_UPLOAD_TMP_ROOT", Kind.STR, bootstrap=True),
    ConfigKey(
        "upload.chunk_size",
        "ZHY_UPLOAD_CHUNK_SIZE",
        Kind.INT,
        minimum=1024,
        maximum=67_108_864,
    ),
    ConfigKey(
        "upload.max_file_bytes",
        "ZHY_UPLOAD_MAX_FILE_BYTES",
        Kind.INT,
        minimum=1024,
        maximum=1_099_511_627_776,
    ),
    ConfigKey("upload.max_chunks", "ZHY_UPLOAD_MAX_CHUNKS", Kind.INT, minimum=1, maximum=1_000_000),
    ConfigKey(
        "upload.session_ttl_sec",
        "ZHY_UPLOAD_SESSION_TTL_SEC",
        Kind.INT,
        minimum=60,
        maximum=604_800,
    ),
    ConfigKey(
        "upload.tmp_ttl_sec",
        "ZHY_UPLOAD_TMP_TTL_SEC",
        Kind.INT,
        minimum=60,
        maximum=86_400,
    ),
    ConfigKey("upload.require_ffprobe", "ZHY_UPLOAD_REQUIRE_FFPROBE", Kind.BOOL),
    ConfigKey("ffmpeg.cve_pause_on_hit", "ZHY_FFMPEG_CVE_PAUSE_ON_HIT", Kind.BOOL),
    ConfigKey("ffmpeg.sbom_path", "ZHY_FFMPEG_SBOM_PATH", Kind.STR),
    ConfigKey(
        "storage.driver",
        "ZHY_STORAGE_DRIVER",
        Kind.ENUM,
        bootstrap=True,
        choices=("local", "s3", "oss"),
    ),
    ConfigKey("storage.local_dir", "ZHY_STORAGE_LOCAL_DIR", Kind.STR, bootstrap=True),
)

KEY_BY_KEY: Mapping[str, ConfigKey] = {spec.key: spec for spec in KEY_SPECS}
ENV_TO_KEY: Mapping[str, str] = {spec.env: spec.key for spec in KEY_SPECS}
HOT_RELOADABLE: frozenset[str] = frozenset(spec.key for spec in KEY_SPECS if spec.hot)

_DSN_PASSWORD = re.compile(r"(://[^/@:]+):[^@/]*@")


def _flatten(table: Mapping[str, object], prefix: str = "") -> dict[str, object]:
    out: dict[str, object] = {}
    for name, value in table.items():
        key = f"{prefix}{name}"
        if isinstance(value, Mapping):
            out.update(_flatten(value, f"{key}."))
        else:
            out[key] = value
    return out


def load_defaults() -> dict[str, object]:
    resource = files(_DEFAULTS_PACKAGE).joinpath(_DEFAULTS_FILE)
    try:
        raw = tomllib.loads(resource.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError) as exc:  # pragma: no cover - 打包错误
        raise ConfigError(f"默认配置不可用 {_DEFAULTS_FILE}: {exc}") from exc
    return _flatten(raw)


DEFAULTS: Mapping[str, object] = load_defaults()


def _fmt(spec: ConfigKey, source: str, expected: str, raw: object) -> str:
    return f"配置键 {spec.key}（来源 {source}，环境变量 {spec.env}）期望 {expected}，实际 {raw!r}"


def _coerce(spec: ConfigKey, raw: object, source: str) -> object:
    if spec.kind is Kind.INT:
        if isinstance(raw, bool) or not isinstance(raw, str | int):
            raise ConfigError(_fmt(spec, source, "整数", raw))
        try:
            value = int(str(raw).strip())
        except ValueError as exc:
            raise ConfigError(_fmt(spec, source, "整数", raw)) from exc
        if spec.minimum is not None and value < spec.minimum:
            raise ConfigError(_fmt(spec, source, f"整数 ≥{spec.minimum}", raw))
        if spec.maximum is not None and value > spec.maximum:
            raise ConfigError(_fmt(spec, source, f"整数 ≤{spec.maximum}", raw))
        return value
    if spec.kind is Kind.BOOL:
        if isinstance(raw, bool):
            return raw
        if isinstance(raw, int):
            return bool(raw)
        if isinstance(raw, str):
            lowered = raw.strip().lower()
            if lowered in {"1", "true", "yes", "on"}:
                return True
            if lowered in {"0", "false", "no", "off", ""}:
                return False
        raise ConfigError(_fmt(spec, source, "布尔（true/false）", raw))
    if spec.kind is Kind.LIST:
        if isinstance(raw, str):
            items = [part.strip() for part in raw.split(",")]
        elif isinstance(raw, list | tuple):
            items = [str(part).strip() for part in raw]
        else:
            raise ConfigError(_fmt(spec, source, "逗号分隔列表", raw))
        values = [item for item in items if item]
        if not values:
            raise ConfigError(_fmt(spec, source, "非空列表", raw))
        return values
    if spec.kind is Kind.ENUM:
        if not isinstance(raw, str) or raw.strip() not in spec.choices:
            raise ConfigError(_fmt(spec, source, f"枚举 {list(spec.choices)}", raw))
        return raw.strip()
    if not isinstance(raw, str):
        raise ConfigError(_fmt(spec, source, "字符串", raw))
    return raw.strip()


def _validate_cross(values: Mapping[str, object]) -> list[str]:
    problems: list[str] = []
    slack = values.get("media.ticket_slack")
    ttl_key = values.get("media.ttl_key")
    if isinstance(slack, int) and isinstance(ttl_key, int) and slack >= ttl_key:
        problems.append("media.ticket_slack 必须小于 media.ttl_key（前端预换票需留出提前量）")
    if values.get("progress.bus") == "redis-stream" and not values.get("redis.url"):
        problems.append("progress.bus=redis-stream 时 redis.url 不得为空")
    if values.get("env") != "dev" and values.get("transcode.sandbox") == "disabled":
        problems.append("非 dev 环境禁止 transcode.sandbox=disabled（§10.3.2.3 沙箱红线）")
    if values.get("env") != "dev" and values.get("upload.require_ffprobe") is not True:
        problems.append(
            "非 dev 环境必须开启 upload.require_ffprobe（§10.3.1 魔数检疫在 Web 进程执行）"
        )
    aes = values.get("media.aes_key_hex")
    if values.get("env") != "dev" and (not isinstance(aes, str) or len(aes) != 32):
        problems.append("非 dev 环境必须配置 media.aes_key_hex（32 位 hex，勿用默认空值）")
    pay_secret = values.get("pay.callback_secret")
    if values.get("env") != "dev" and pay_secret == ZHY_PAY_CALLBACK_SECRET:
        problems.append(
            "非 dev 环境必须替换 pay.callback_secret（禁止沿用 dev 占位密钥，"
            "否则支付回调可被伪造）"
        )
    cdn = values.get("media.cdn_base")
    if isinstance(cdn, str) and cdn and not cdn.startswith(("http://", "https://")):
        problems.append("media.cdn_base 必须为空或 http(s) 绝对地址")
    ttl_key = values.get("media.ttl_key")
    ttl_playlist = values.get("media.ttl_playlist")
    ttl_ts = values.get("media.ttl_ts")
    if isinstance(ttl_key, int) and isinstance(ttl_playlist, int) and ttl_key > ttl_playlist:
        problems.append("media.ttl_key 不能大于 media.ttl_playlist（三层 TTL 必须逐层收敛）")
    if isinstance(ttl_playlist, int) and isinstance(ttl_ts, int) and ttl_playlist > ttl_ts:
        problems.append("media.ttl_playlist 不能大于 media.ttl_ts")
    if values.get("progress.bus") == "redis-stream":
        problems.append("progress.bus=redis-stream 依赖 M3 的 Redis，当前版本仅支持 local")
    profiles = values.get("transcode.profiles")
    allowed = {"360", "720", "1080"}
    if not isinstance(profiles, list) or not profiles:
        problems.append("transcode.profiles 不能为空")
    else:
        unknown = [item for item in profiles if not isinstance(item, str) or item not in allowed]
        if unknown:
            problems.append(f"transcode.profiles 含非法档位：{unknown}")
    return problems


def _redact_dsn(url: str) -> str:
    return _DSN_PASSWORD.sub(rf"\1:{_SECRET_MASK}@", url)


@dataclass(frozen=True, slots=True)
class Config:
    """不可变配置快照。"""

    _values: Mapping[str, object] = field(repr=False)
    _sources: Mapping[str, str] = field(repr=False)

    def __repr__(self) -> str:
        return f"Config(env={self.get_str('env')!r}, keys={len(self._values)})"

    def get(self, key: str) -> object:
        if key not in self._values:
            raise ConfigError(f"未知配置键: {key}")
        return self._values[key]

    def get_str(self, key: str) -> str:
        value = self.get(key)
        if not isinstance(value, str):
            raise ConfigError(f"配置键 {key} 不是字符串: {value!r}")
        return value

    def get_int(self, key: str) -> int:
        value = self.get(key)
        if isinstance(value, bool) or not isinstance(value, int):
            raise ConfigError(f"配置键 {key} 不是整数: {value!r}")
        return value

    def get_bool(self, key: str) -> bool:
        value = self.get(key)
        if not isinstance(value, bool):
            raise ConfigError(f"配置键 {key} 不是布尔: {value!r}")
        return value

    def get_list(self, key: str) -> list[str]:
        value = self.get(key)
        if not isinstance(value, list):
            raise ConfigError(f"配置键 {key} 不是列表: {value!r}")
        return list(value)

    def sources(self) -> dict[str, str]:
        return dict(self._sources)

    def redacted(self) -> dict[str, object]:
        """可安全写日志/健康检查的配置视图。"""
        out: dict[str, object] = {}
        for key, value in self._values.items():
            spec = KEY_BY_KEY[key]
            if spec.secret and value:
                out[key] = _SECRET_MASK
            elif key == "db.url":
                out[key] = _redact_dsn(str(value))
            else:
                out[key] = value
        return out

    def reload(self, patch: Mapping[str, object]) -> Config:
        """仅允许热更新白名单键（M1.5 由 sys_config 推送触发）。"""
        unknown = sorted(set(patch) - set(KEY_BY_KEY))
        if unknown:
            raise ConfigError([f"未知配置键: {key}" for key in unknown])
        frozen = sorted(key for key in patch if key not in HOT_RELOADABLE)
        if frozen:
            raise ConfigError([f"配置键 {key} 不允许热更新" for key in frozen])
        values = dict(self._values)
        sources = dict(self._sources)
        for key, raw in patch.items():
            values[key] = _coerce(KEY_BY_KEY[key], raw, "runtime")
            sources[key] = "runtime"
        problems = _validate_cross(values)
        if problems:
            raise ConfigError(problems)
        return Config(values, sources)


def load_config(
    environ: Mapping[str, str] | None = None,
    sys_config: Mapping[str, str] | None = None,
) -> Config:
    """加载三层配置。environ 为空串视为未设置（与 dotenv 语义一致）。"""
    env = os.environ if environ is None else environ
    sysc: Mapping[str, str] = {} if sys_config is None else sys_config

    values: dict[str, object] = dict(DEFAULTS)
    sources: dict[str, str] = dict.fromkeys(DEFAULTS, "default")

    for spec in KEY_SPECS:
        raw = env.get(spec.env)
        if raw is None or raw == "":
            continue
        values[spec.key] = _coerce(spec, raw, "env")
        sources[spec.key] = "env"

    for key, raw in sysc.items():
        sys_spec = KEY_BY_KEY.get(key)
        if sys_spec is None:
            raise ConfigError(f"sys_config 含未知键: {key}")
        if sys_spec.bootstrap:
            logger.warning("sys_config 不得覆盖 bootstrap 键 %s（保持环境变量值）", key)
            continue
        values[key] = _coerce(sys_spec, raw, "sys_config")
        sources[key] = "sys_config"

    problems = _validate_cross(values)
    unknown = sorted(name for name in env if name.startswith("ZHY_") and name not in ENV_TO_KEY)
    if unknown:
        message = f"未知环境变量: {', '.join(unknown)}"
        if values.get("env") != "dev":
            problems.append(message + "（非 dev 环境禁止拼写错误静默生效）")
        else:
            logger.warning("%s（dev 环境忽略）", message)
    if problems:
        raise ConfigError(problems)

    return Config(values, sources)
