"""M1.1 T17：错误码注册表冻结（附录 A 唯一来源）。"""

from __future__ import annotations

from zhyonlineedu.core.codes import (
    ACCESS_CLASS_REQUIRED,
    ACCESS_LEVEL_REQUIRED,
    ACCOUNT_DISABLED,
    ACCOUNT_EXPIRED,
    AUTH_INVALID_CREDENTIALS,
    AUTH_REFRESH_REPLAY,
    AUTH_TOKEN_EXPIRED,
    AUTH_TOKEN_REVOKED,
    CATALOG_NOT_EMPTY,
    CONFLICT_RETRY,
    COURSE_ASSET_NOT_READY,
    COURSE_NOT_PUBLISHED,
    ENC_FFMPEG,
    ENC_INPUT,
    ENC_INTERRUPTED,
    ENC_KEY,
    ENC_OUTPUT,
    ENC_POSTER,
    ENC_SANDBOX,
    ENC_TIMEOUT,
    ERROR_SPECS,
    INSTALL_ALREADY_DONE,
    INSTALL_BLOCKED,
    INSTALL_REQUIRED,
    INSTALL_STEP_INVALID,
    INSTALL_TOKEN_INVALID,
    INTERNAL_ERROR,
    METHOD_NOT_ALLOWED,
    MODE_ACCESS_DENIED,
    MODE_COURSE_MISMATCH,
    MODE_SWITCH_BLOCKED,
    MODE_UNCHANGED,
    NOT_FOUND,
    OK,
    PAY_AMOUNT_MISMATCH,
    PAY_CHANNEL_MISMATCH,
    PAY_SIGNATURE_INVALID,
    PAY_STATE_INVALID,
    PERMISSION_DENIED,
    PLUGIN_CAP_DENIED,
    PLUGIN_LOAD_FAILED,
    PLUGIN_MANIFEST_INVALID,
    PLUGIN_MIGRATION_INVALID,
    PLUGIN_NOT_FOUND,
    PLUGIN_STATE_INVALID,
    PLUGIN_TABLE_PREFIX_INVALID,
    REGISTRATION_CLOSED,
    SUBJECT_DISABLED,
    TRANSCODE_PAUSED,
    VALIDATION_FAILED,
    Code,
    spec_for,
)

# 显式快照：任何增删改都必须同步修改本表与计划书附录 A。
EXPECTED: dict[Code | str, tuple[int, str]] = {
    Code.PROGRESS_ACCEPTED: (202, "none"),
    Code.RATE_LIMITED: (429, "backoff"),
    Code.MEDIA_TICKET_EXPIRED: (401, "refresh_ticket"),
    Code.MEDIA_NOT_READY: (409, "retry"),
    Code.MEDIA_TICKET_MISMATCH: (403, "stop_playback"),
    Code.MEDIA_ACCESS_REVOKED: (403, "stop_playback"),
    Code.MEDIA_PLAYBACK_UNRECOVERABLE: (410, "contact_support"),
    Code.MEDIA_TICKET_REPLAY: (403, "stop_playback"),
    Code.UPLOAD_CHUNK_MD5_MISMATCH: (409, "retry"),
    Code.UPLOAD_CHUNK_MISSING: (409, "retry"),
    Code.UPLOAD_MALICIOUS: (415, "stop_playback"),
    OK: (200, "none"),
    NOT_FOUND: (404, "none"),
    METHOD_NOT_ALLOWED: (405, "none"),
    VALIDATION_FAILED: (422, "none"),
    INTERNAL_ERROR: (500, "contact_support"),
    AUTH_TOKEN_EXPIRED: (401, "relogin"),
    AUTH_TOKEN_REVOKED: (401, "relogin"),
    AUTH_REFRESH_REPLAY: (401, "relogin"),
    AUTH_INVALID_CREDENTIALS: (401, "none"),
    PERMISSION_DENIED: (403, "none"),
    ACCOUNT_EXPIRED: (401, "relogin"),
    ACCOUNT_DISABLED: (401, "relogin"),
    ACCESS_LEVEL_REQUIRED: (403, "none"),
    ACCESS_CLASS_REQUIRED: (403, "none"),
    COURSE_NOT_PUBLISHED: (403, "none"),
    SUBJECT_DISABLED: (403, "none"),
    REGISTRATION_CLOSED: (403, "none"),
    INSTALL_REQUIRED: (409, "install"),
    INSTALL_ALREADY_DONE: (409, "none"),
    INSTALL_STEP_INVALID: (409, "none"),
    INSTALL_TOKEN_INVALID: (401, "none"),
    INSTALL_BLOCKED: (409, "none"),
    CONFLICT_RETRY: (409, "retry"),
    PLUGIN_CAP_DENIED: (403, "none"),
    PLUGIN_MANIFEST_INVALID: (422, "none"),
    PLUGIN_TABLE_PREFIX_INVALID: (409, "none"),
    PLUGIN_MIGRATION_INVALID: (409, "none"),
    PLUGIN_NOT_FOUND: (404, "none"),
    PLUGIN_STATE_INVALID: (409, "none"),
    PLUGIN_LOAD_FAILED: (500, "contact_support"),
    COURSE_ASSET_NOT_READY: (409, "none"),
    CATALOG_NOT_EMPTY: (409, "none"),
    ENC_INPUT: (500, "contact_support"),
    ENC_FFMPEG: (500, "contact_support"),
    ENC_OUTPUT: (500, "contact_support"),
    ENC_KEY: (500, "contact_support"),
    ENC_POSTER: (500, "contact_support"),
    ENC_TIMEOUT: (500, "contact_support"),
    ENC_SANDBOX: (500, "contact_support"),
    ENC_INTERRUPTED: (500, "contact_support"),
    TRANSCODE_PAUSED: (409, "none"),
    MODE_ACCESS_DENIED: (403, "none"),
    MODE_COURSE_MISMATCH: (403, "none"),
    MODE_SWITCH_BLOCKED: (409, "none"),
    MODE_UNCHANGED: (409, "none"),
    PAY_STATE_INVALID: (409, "none"),
    PAY_SIGNATURE_INVALID: (401, "none"),
    PAY_AMOUNT_MISMATCH: (409, "contact_support"),
    PAY_CHANNEL_MISMATCH: (422, "none"),
}


def test_registry_matches_frozen_snapshot() -> None:
    assert set(ERROR_SPECS) == set(EXPECTED)
    for code, (http, action) in EXPECTED.items():
        spec = spec_for(code)
        assert spec.http == http, code
        assert spec.action == action, code


def test_numeric_codes_are_unique() -> None:
    values = [int(code) for code in Code]
    assert len(values) == len(set(values))


def test_every_code_has_registry_entry() -> None:
    for code in Code:
        assert code in ERROR_SPECS


def test_unknown_code_raises() -> None:
    try:
        spec_for("NO_SUCH_CODE")
    except KeyError as exc:
        assert "NO_SUCH_CODE" in str(exc)
    else:  # pragma: no cover
        raise AssertionError("未注册错误码应抛 KeyError")
