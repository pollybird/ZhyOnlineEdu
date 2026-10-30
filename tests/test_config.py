"""M1.1 T12~T16 + T20：配置三层、bootstrap 例外、交叉校验、脱敏、键表一致性。"""

from __future__ import annotations

from pathlib import Path

import pytest
from zhyonlineedu.core import constants
from zhyonlineedu.core.config import ENV_TO_KEY, ConfigError, load_config

ROOT = Path(__file__).resolve().parents[1]


def _load(environ: dict[str, str] | None = None, sys_config: dict[str, str] | None = None):  # type: ignore[no-untyped-def]
    return load_config(environ=environ or {}, sys_config=sys_config or {})


def _non_dev_environ() -> dict[str, str]:
    """非 dev 档同时满足沙箱 / ffprobe / 媒体密钥的既有交叉校验。"""
    return {
        "ZHY_ENV": "t1",
        "ZHY_TRANSCODE_SANDBOX": "bwrap",
        "ZHY_UPLOAD_REQUIRE_FFPROBE": "true",
        "ZHY_MEDIA_AES_KEY_HEX": "ab" * 16,
    }


def test_defaults_from_toml() -> None:
    cfg = _load()
    assert cfg.get_int("media.ttl_key") == 120
    assert cfg.get_str("env") == "dev"
    assert cfg.get_list("transcode.allowed_formats")[:2] == ["mp4", "mov"]
    assert cfg.sources()["media.ttl_key"] == "default"


def test_env_overrides_default() -> None:
    cfg = _load({"ZHY_MEDIA_TTL_KEY_SEC": "60"})
    assert cfg.get_int("media.ttl_key") == 60
    assert cfg.sources()["media.ttl_key"] == "env"


def test_empty_env_means_unset() -> None:
    assert _load({"ZHY_MEDIA_TTL_KEY_SEC": ""}).get_int("media.ttl_key") == 120


def test_sys_config_beats_env() -> None:
    cfg = _load({"ZHY_MEDIA_TTL_KEY_SEC": "60"}, {"media.ttl_key": "90"})
    assert cfg.get_int("media.ttl_key") == 90
    assert cfg.sources()["media.ttl_key"] == "sys_config"


def test_bootstrap_key_ignores_sys_config() -> None:
    cfg = _load({"ZHY_DB_URL": "sqlite:///a.db"}, {"db.url": "sqlite:///b.db"})
    assert cfg.get_str("db.url") == "sqlite:///a.db"
    assert cfg.sources()["db.url"] == "env"


def test_bad_type_fails_fast() -> None:
    with pytest.raises(ConfigError, match="media.ttl_key"):
        _load({"ZHY_MEDIA_TTL_KEY_SEC": "abc"})


def test_cross_constraint_rejects_slack_ge_key_ttl() -> None:
    with pytest.raises(ConfigError, match="ticket_slack"):
        _load({"ZHY_MEDIA_TTL_KEY_SEC": "100", "ZHY_MEDIA_TICKET_SLACK_SEC": "100"})


def test_non_dev_rejects_disabled_sandbox() -> None:
    with pytest.raises(ConfigError, match="sandbox"):
        _load({"ZHY_ENV": "t1", "ZHY_TRANSCODE_SANDBOX": "disabled"})


def test_non_dev_rejects_dev_pay_callback_secret() -> None:
    environ = _non_dev_environ()
    environ["ZHY_PAY_CALLBACK_SECRET"] = "dev-only-callback-secret"  # noqa: S105 - 占位密钥
    with pytest.raises(ConfigError, match="pay.callback_secret"):
        _load(environ)


def test_non_dev_accepts_custom_pay_callback_secret() -> None:
    environ = _non_dev_environ()
    environ["ZHY_PAY_CALLBACK_SECRET"] = "p" * 32
    cfg = _load(environ)
    assert cfg.redacted()["pay.callback_secret"] == "***"  # noqa: S105 - 脱敏占位符


def test_unknown_env_rejected_in_prod_but_tolerated_in_dev() -> None:
    _load({"ZHY_MEDIA_TTL_KE": "60"})
    with pytest.raises(ConfigError, match="未知环境变量"):
        _load({"ZHY_ENV": "t1", "ZHY_MEDIA_TTL_KE": "60"})


def test_secrets_are_redacted() -> None:
    cfg = _load(
        {
            "ZHY_DB_URL": "postgresql://user:pass@db:5432/zhy",
            "ZHY_MEDIA_AES_KEY_HEX": "ab" * 16,
        }
    )
    redacted = cfg.redacted()
    assert redacted["db.url"] == "postgresql://user:***@db:5432/zhy"
    assert redacted["media.aes_key_hex"] == "***"
    assert "pass" not in repr(cfg)
    assert "abab" not in repr(cfg)


def test_reload_only_hot_keys() -> None:
    cfg = _load()
    updated = cfg.reload({"media.ttl_key": 60})
    assert updated.get_int("media.ttl_key") == 60
    assert updated.sources()["media.ttl_key"] == "runtime"
    with pytest.raises(ConfigError, match="不允许热更新"):
        cfg.reload({"db.url": "sqlite:///x.db"})


def test_defaults_match_frozen_constants() -> None:
    cfg = _load()
    pairs = {
        "progress.flush_batch": constants.ZHY_PROGRESS_FLUSH_BATCH,
        "progress.flush_interval_sec": constants.ZHY_PROGRESS_FLUSH_INTERVAL_SEC,
        "progress.lease_ttl_sec": constants.ZHY_PROGRESS_LEASE_TTL_SEC,
        "progress.ring_dir": constants.ZHY_PROGRESS_RING_DIR,
        "progress.recovery_max_gap": constants.ZHY_PROGRESS_RECOVERY_MAX_GAP,
        "progress.completion_threshold": constants.ZHY_PROGRESS_COMPLETION_THRESHOLD,
        "quiz.pass_score": constants.ZHY_QUIZ_PASS_SCORE,
        "mode.cache_ttl_sec": constants.ZHY_MODE_CACHE_TTL_SEC,
        "mode.quarantine_window_sec": constants.ZHY_MODE_QUARANTINE_WINDOW_SEC,
        "mode.ticket_warn_threshold": constants.ZHY_MODE_TICKET_WARN_THRESHOLD,
        "users.on_conflict_action": constants.ZHY_USERS_ON_CONFLICT_ACTION,
        "users.import_max_rows": constants.ZHY_USERS_IMPORT_MAX_ROWS,
        "users.expire_hour": constants.ZHY_USERS_EXPIRE_HOUR,
        "users.expire_batch_size": constants.ZHY_USERS_EXPIRE_BATCH_SIZE,
        "pay.callback_secret": constants.ZHY_PAY_CALLBACK_SECRET,
        "pay.order_ttl_sec": constants.ZHY_PAY_ORDER_TTL_SEC,
        "pay.max_open_orders": constants.ZHY_PAY_MAX_OPEN_ORDERS,
        "media.ttl_ts": constants.ZHY_MEDIA_TTL_TS_SEC,
        "media.ttl_playlist": constants.ZHY_MEDIA_TTL_PLAYLIST_SEC,
        "media.ttl_key": constants.ZHY_MEDIA_TTL_KEY_SEC,
        "media.ticket_slack": constants.ZHY_MEDIA_TICKET_SLACK_SEC,
        "media.ksession_strict_ip": constants.ZHY_MEDIA_KSESSION_STRICT_IP,
        "media.cdn_base": constants.ZHY_MEDIA_CDN_BASE,
        "media.nonce_reuse_window_sec": constants.ZHY_MEDIA_NONCE_REUSE_WINDOW_SEC,
        "media.superseded_grace_sec": constants.ZHY_MEDIA_SUPERSEDED_GRACE_SEC,
        "transcode.sandbox": constants.ZHY_TRANSCODE_SANDBOX,
        "transcode.cpu_max_percent": constants.ZHY_TRANSCODE_CPU_MAX_PERCENT,
        "transcode.mem_max_mb": constants.ZHY_TRANSCODE_MEM_MAX_MB,
        "transcode.pids_max": constants.ZHY_TRANSCODE_PIDS_MAX,
        "transcode.input_max_duration_sec": constants.ZHY_TRANSCODE_INPUT_MAX_DURATION_SEC,
        "transcode.input_max_dimension": constants.ZHY_TRANSCODE_INPUT_MAX_DIM,
        "transcode.segment_sec": constants.ZHY_TRANSCODE_SEGMENT_SEC,
        "transcode.poster_sec": constants.ZHY_TRANSCODE_POSTER_SEC,
        "transcode.max_attempts": constants.ZHY_TRANSCODE_MAX_ATTEMPTS,
        "transcode.retry_backoff_sec": constants.ZHY_TRANSCODE_RETRY_BACKOFF_SEC,
        "transcode.worker_poll_sec": constants.ZHY_TRANSCODE_WORKER_POLL_SEC,
        "transcode.queue_batch": constants.ZHY_TRANSCODE_QUEUE_BATCH,
        "transcode.ffprobe_timeout_sec": constants.ZHY_TRANSCODE_FFPROBE_TIMEOUT_SEC,
        "transcode.task_timeout_sec": constants.ZHY_TRANSCODE_TASK_TIMEOUT_SEC,
        "transcode.paused": constants.ZHY_TRANSCODE_PAUSED,
        "upload.tmp_root": constants.ZHY_UPLOAD_TMP_ROOT,
        "upload.chunk_size": constants.ZHY_UPLOAD_CHUNK_SIZE,
        "upload.max_file_bytes": constants.ZHY_UPLOAD_MAX_FILE_BYTES,
        "upload.max_chunks": constants.ZHY_UPLOAD_MAX_CHUNKS,
        "upload.session_ttl_sec": constants.ZHY_UPLOAD_SESSION_TTL_SEC,
        "upload.tmp_ttl_sec": constants.ZHY_UPLOAD_TMP_TTL_SEC,
        "upload.require_ffprobe": constants.ZHY_UPLOAD_REQUIRE_FFPROBE,
        "ffmpeg.cve_pause_on_hit": constants.ZHY_FFMPEG_CVE_PAUSE_ON_HIT,
    }
    for key, expected in pairs.items():
        assert cfg.get(key) == expected, key
    assert (
        tuple(cfg.get_list("transcode.allowed_formats")) == constants.ZHY_TRANSCODE_ALLOWED_FORMATS
    )
    assert tuple(cfg.get_list("transcode.profiles")) == constants.ZHY_TRANSCODE_PROFILES


def test_env_example_matches_key_table() -> None:
    names: set[str] = set()
    for line in (ROOT / ".env.example").read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        name = stripped.split("=", 1)[0].strip()
        if name.startswith("ZHY_"):
            names.add(name)
    assert names == set(ENV_TO_KEY)
