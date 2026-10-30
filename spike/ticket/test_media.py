"""M0 Spike · 票据模型用例（MEDIA-001~008 · F3/F4）。

运行：uv run pytest spike/ticket -q
"""

from __future__ import annotations

import sys
from pathlib import Path
from urllib.parse import parse_qs, urlparse

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

from media import CODE, TICKET_KEY_TTL, TICKET_TS_TTL, MediaServer  # noqa: E402


def q(url: str) -> dict[str, str]:
    return {k: v[0] for k, v in parse_qs(urlparse(url).query).items()}


def server() -> MediaServer:
    s = MediaServer()
    for lesson in (1, 2):
        s.keys[lesson] = b"0123456789abcdef"
    return s


def test_media_001_issue_and_playlist() -> None:
    s = server()
    t = s.issue_ticket(1, user_id=7)
    assert t["ticket_ttl_sec"] == TICKET_TS_TTL
    assert t["key_ttl_sec"] == TICKET_KEY_TTL
    assert t["ticket_refresh_slack_sec"] == 20
    code, body = s.get_playlist(1, q(t["playlist_url"]))
    assert code == 200
    assert "#EXT-X-KEY:METHOD=AES-128" in body


def test_media_002_playlist_is_user_agnostic() -> None:
    """同课不同用户 → playlist URL 完全一致（可 CDN 长缓存，不泄漏身份）。"""
    s = server()
    a, b = s.issue_ticket(1, 7), s.issue_ticket(1, 8)
    assert a["playlist_url"] == b["playlist_url"]
    assert "sid" not in a["playlist_url"] and "user" not in a["playlist_url"]


def test_media_003_key_requires_valid_ksession() -> None:
    s = server()
    t = s.issue_ticket(1, 7)
    assert s.get_key(1, {}) == (403, "NO_SESSION")
    query = q(t["key_url"])
    query["nonce"] = "forged"
    assert s.get_key(1, query) == (403, "NONCE_INVALID")


def test_media_004_key_single_use_replay() -> None:
    s = server()
    query = q(s.issue_ticket(1, 7)["key_url"])
    assert s.get_key(1, query)[0] == 200
    code, msg = s.get_key(1, query)
    assert code == CODE["replay"], "重放返回 40206，不提示密码错"
    assert s.audit[-1]["event"] == "replay"


def test_media_005_cross_lesson_mismatch() -> None:
    s = server()
    query = q(s.issue_ticket(1, 7)["key_url"])
    code, msg = s.get_key(2, query)  # 用 1 课的票取 2 课的 key
    assert code == CODE["mismatch"], msg
    assert s.audit[-1]["asked"] == 2


def test_media_006_expired_ticket() -> None:
    s = server()
    t = s.issue_ticket(1, 7)
    s.current_time = s._now() + TICKET_TS_TTL + 1
    code, msg = s.get_playlist(1, q(t["playlist_url"]))
    assert code == CODE["expired"], msg
    code, msg = s.get_key(1, q(t["key_url"]))
    assert code == CODE["expired"], msg


def test_media_007_revocation() -> None:
    s = server()
    query = q(s.issue_ticket(1, 7)["key_url"])
    s.revoked_users.add(7)
    code, msg = s.get_key(1, query)
    assert code == CODE["revoked"], msg
    assert s.audit[-1]["event"] == "revoked"


def test_media_008_tampered_signature() -> None:
    s = server()
    query = q(s.issue_ticket(1, 7)["key_url"])
    query["exp"] = "9999999999"
    assert s.get_key(1, query) == (403, "SIG_INVALID")
    playlist = q(s.issue_ticket(1, 7)["playlist_url"])
    playlist["kid"] = "deadbeef"
    assert s.get_playlist(1, playlist)[0] == 403
