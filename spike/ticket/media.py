"""M0 Spike · 票据模型 POC（§21.4 · F3/F4/F8）。

核心主张：
  1. playlist 用 kid + ts 签名，**不绑用户** → 可 CDN 长缓存，同课同 URL 对所有用户一致；
  2. 用户身份只在取 key 的 ksession 上校验（单次 nonce，TTL 120s）；不进 JWT；
  3. ts / playlist / key 三层 TTL 独立；
  4. 串课、过期、重放、吊销各有独立错误码（40201~40206）。
"""

from __future__ import annotations

import hashlib
import hmac
import secrets
import time
from dataclasses import dataclass, field
from typing import Any

TICKET_TS_TTL = 3 * 24 * 3600
TICKET_PLAYLIST_TTL = 900
TICKET_KEY_TTL = 120
TICKET_SLACK = 20  # 前端预换票提前量

CODE = {
    "expired": 40201,
    "not_ready": 40202,
    "mismatch": 40203,
    "revoked": 40204,
    "unrecoverable": 40205,
    "replay": 40206,
}


@dataclass
class KSession:
    session_id: str
    user_id: int
    lesson_id: int
    nonce: str
    expires_at: float
    used: bool = False
    revoked: bool = False


@dataclass
class MediaServer:
    """无框架最小实现：验证 playlist / key 两层票据。"""

    secret: bytes = field(default_factory=lambda: secrets.token_bytes(32))
    keys: dict[int, bytes] = field(default_factory=dict)  # lesson_id -> 16B AES key
    sessions: dict[str, KSession] = field(default_factory=dict)
    revoked_users: set[int] = field(default_factory=set)
    current_time: float | None = None  # 测试可注入时钟
    audit: list[dict[str, Any]] = field(default_factory=list)

    # ── 签名 ──
    def _now(self) -> float:
        return self.current_time if self.current_time is not None else time.time()

    def _sig(self, kind: str, payload: str) -> str:
        return hmac.new(self.secret, f"{kind}:{payload}".encode(), hashlib.sha256).hexdigest()[:32]

    def _kid(self, lesson_id: int) -> str:
        return hashlib.sha256(f"kid:{lesson_id}:{self.secret.hex()[:8]}".encode()).hexdigest()[:16]

    # ── 开票 ──
    def issue_ticket(self, lesson_id: int, user_id: int) -> dict[str, Any]:
        ts = int(self._now())
        playlist_exp = ts + TICKET_TS_TTL
        kid = self._kid(lesson_id)
        playlist_sig = self._sig("pl", f"{lesson_id}:{ts}:{playlist_exp}")
        sid = secrets.token_urlsafe(12)
        nonce = secrets.token_urlsafe(9)
        self.sessions[sid] = KSession(
            session_id=sid,
            user_id=user_id,
            lesson_id=lesson_id,
            nonce=nonce,
            expires_at=self._now() + TICKET_KEY_TTL,
        )
        key_sig = self._sig(
            "key", f"{lesson_id}:{sid}:{nonce}:{int(self.sessions[sid].expires_at)}"
        )
        return {
            "playlist_url": f"/media/pl/{lesson_id}/index.m3u8?kid={kid}&ts={ts}"
            f"&exp={playlist_exp}&sig={playlist_sig}",
            "key_url": f"/media/key/{lesson_id}?kid={kid}&sid={sid}&nonce={nonce}"
            f"&exp={int(self.sessions[sid].expires_at)}&sig={key_sig}",
            "ticket_ttl_sec": TICKET_TS_TTL,
            "key_ttl_sec": TICKET_KEY_TTL,
            "ticket_refresh_slack_sec": TICKET_SLACK,
        }

    # ── 播放入口（匿名，可缓存）──
    def get_playlist(self, lesson_id: int, query: dict[str, str]) -> tuple[int, str]:
        expected = self._sig("pl", f"{lesson_id}:{query.get('ts')}:{query.get('exp')}")
        if not hmac.compare_digest(expected, query.get("sig", "")):
            return (403, "SIG_INVALID")
        if query.get("kid") != self._kid(lesson_id):
            return (403, "KID_INVALID")
        if int(query.get("exp", 0)) < self._now():
            return (CODE["expired"], "TICKET_EXPIRED")
        return (200, f'#EXTM3U\n#EXT-X-KEY:METHOD=AES-128,URI="/media/key/{lesson_id}?k=1"')

    # ── 取 key（用户身份唯一校验点，nonce 单次）──
    def get_key(self, lesson_id: int, query: dict[str, str]) -> tuple[int, bytes | str]:
        sid = query.get("sid", "")
        session = self.sessions.get(sid)
        if session is None:
            return (403, "NO_SESSION")
        if session.revoked or session.user_id in self.revoked_users:
            self.audit.append({"event": "revoked", "sid": sid, "user": session.user_id})
            return (CODE["revoked"], "ACCESS_REVOKED")
        if session.lesson_id != lesson_id:
            self.audit.append({"event": "mismatch", "sid": sid, "asked": lesson_id})
            return (CODE["mismatch"], "TICKET_MISMATCH")
        if session.expires_at < self._now():
            return (CODE["expired"], "TICKET_EXPIRED")
        # exp 必须在签名内，否则被篡改后仍可通过（MEDIA-008 暴露的缺口）
        expected = self._sig("key", f"{lesson_id}:{sid}:{session.nonce}:{query.get('exp', '')}")
        if not hmac.compare_digest(expected, query.get("sig", "")):
            return (403, "SIG_INVALID")
        if query.get("exp") != str(int(session.expires_at)):
            return (CODE["expired"], "TICKET_EXPIRED")
        if query.get("nonce") != session.nonce:
            return (403, "NONCE_INVALID")
        if session.used:
            self.audit.append({"event": "replay", "sid": sid, "user": session.user_id})
            return (CODE["replay"], "TICKET_REPLAY")
        session.used = True
        return (200, self.keys[lesson_id])
