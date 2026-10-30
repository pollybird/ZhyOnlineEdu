"""播放票据与密钥会话（§10.3.3 / §10.3.4 · F3/F4/F8 · M2.5）。

**核心口径：没有 ticket 就拿不到 key。**
- `kid` 只标识资产（转码时生成），与用户无关，因此 m3u8/ts 可上 CDN；
- 用户身份绑定在 `media_keysession`：playlist 的 `#EXT-X-KEY` URI 携带
  `ks`（会话）+ `n`（nonce）+ `exp`（失效 Unix 秒）+ `sig`（HMAC），key 端点逐项校验；
- 三层 TTL：keysession（`media.ttl_ts`）> playlist（`ttl_playlist`）> key（`ttl_key`）；
  playlist 每次拉取轮换 nonce，旧 playlist 里的 key URI 随即失效；
- nonce 单次使用，带 `media.nonce_reuse_window_sec` 复用窗口（容忍播放器在窗口内
  重复请求同一 key）；窗口外即 40206；
- 档位授权不进入签名，而在服务端按 `media_keysession.profiles` 校验（改策略无需换签）；
- `media.ksession_strict_ip=true` 时才把网段当硬条件，默认只作软信号（F8 防误伤）。

权限口径（T0）：
- `public` 或 `lesson.is_free` → 直接放行；
- `member` → 需要有效会员订阅（M2.12 `active_sub()`，等级 ≥ 课程要求）；
- `class` → 需要任一启用班级的成员关系（M2.7），否则 `ACCESS_CLASS_REQUIRED`；
  member 等级与成员位纳入 `access_hash`，会员到期 / 退班 / 停班后旧票据统一 40204；
- 课程非 `published` → 仅 `course:edit` 可预览（`preview=true`，进度不计入统计）。
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
import secrets
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, cast

from sqlalchemy import select, update
from sqlalchemy.engine import CursorResult
from sqlalchemy.orm import Session

from zhyonlineedu.core.codes import (
    ACCESS_LEVEL_REQUIRED,
    NOT_FOUND,
    Code,
)
from zhyonlineedu.core.config import Config
from zhyonlineedu.core.constants import ZHY_PROGRESS_METRIC_VERSION
from zhyonlineedu.core.db.engine import Database, ensure_database
from zhyonlineedu.core.db.models.catalog import EduCourse, EduCourseLesson
from zhyonlineedu.core.db.models.learn import LearnProgress
from zhyonlineedu.core.db.models.media import MediaAsset, MediaKeySession
from zhyonlineedu.core.errors import AppError
from zhyonlineedu.core.media.hls import MASTER_NAME, HlsVerifyError
from zhyonlineedu.core.media.keys import (
    MediaKeyError,
    derive_aes_key,
    load_master_secret,
    sign_query,
    verify_query,
)
from zhyonlineedu.core.media.playlist import RewriteContext, rewrite_master, rewrite_variant
from zhyonlineedu.core.mode import (
    AccessSpec,
    ModeState,
    TicketSpec,
    decide_access,
    decide_ticket,
    get_mode_context,
    policy_for,
)
from zhyonlineedu.modules.commerce.service import active_level
from zhyonlineedu.modules.users.service import is_class_member

logger = logging.getLogger(__name__)

PLAYLIST_PREFIX = "/api/v1/media/playlist"
SEGMENT_PREFIX = "/api/v1/media/seg"
KEY_PREFIX = "/api/v1/media/key"
NONCE_BYTES = 12
_KSESSION = re.compile(r"^[0-9a-f]{32}$")
_KID = re.compile(r"^[0-9a-f]{32}$")
_SEGMENT = re.compile(r"^[A-Za-z0-9._-]+\.ts$")
PROFILE_NAMES = ("360", "720", "1080")


@dataclass(frozen=True, slots=True)
class TicketRequest:
    user_id: int
    username: str
    lesson_id: int
    can_preview: bool = False
    ip: str = ""


def _iso(value: datetime) -> str:
    return value.isoformat(timespec="seconds")


def _ip_family(ip: str) -> str:
    """只保留 /24(v4) 或 /56(v6) 网段，避免存储完整 IP。"""
    parts = ip.split(".")
    if len(parts) == 4:
        return ".".join(parts[:3]) + ".0/24"
    if ":" in ip:
        blocks = ip.split(":")
        return ":".join(blocks[:4]) + "::/56"
    return ""


class PlaybackService:
    def __init__(
        self,
        config: Config,
        *,
        db: Database | None = None,
        storage_root: Path | None = None,
    ) -> None:
        self.config = config
        self._db = db
        self._storage = (
            Path(storage_root).resolve()
            if storage_root is not None
            else Path(config.get_str("storage.local_dir")).resolve()
        )

    @property
    def db(self) -> Database:
        if self._db is None:
            self._db = ensure_database(self.config)
        return self._db

    def hls_dir(self, lesson_id: int) -> Path:
        return self._storage / "media" / str(lesson_id) / "hls"

    # ── 签发 ──────────────────────────────────────────────────────────────

    def issue(self, request: TicketRequest, *, now: datetime | None = None) -> dict[str, Any]:
        moment = now or datetime.now(UTC)
        lesson, course, asset = self._load(request.lesson_id)
        if asset.status != "encoded" or not asset.kid:
            raise AppError(
                Code.MEDIA_NOT_READY,
                data={
                    "lesson_id": request.lesson_id,
                    "asset_status": asset.status,
                    "error_code": asset.error_code,
                    "retry_after": 30,
                },
            )

        # 统一访问判定（§9.4 决策表 1~14）：模式 / 上架态 / 授权方式只有这一个入口
        state = get_mode_context(self.config, db=self.db).snapshot()
        class_member = (
            False
            if request.can_preview or lesson.is_free or course.access_type != "class"
            else self._class_member(request.user_id)
        )
        member_level = (
            None
            if request.can_preview or lesson.is_free or course.access_type != "member"
            else self._member_level(request.user_id)
        )
        decision = decide_access(
            AccessSpec(
                mode=state.mode,
                epoch=state.epoch,
                course_mode=course.mode,
                course_status=course.status,
                access_type=course.access_type,
                lesson_is_free=bool(lesson.is_free),
                can_preview=request.can_preview,
                class_member=class_member,
                member_level=member_level,
                required_level=1,
            )
        )
        if not decision.allowed:
            raise AppError(
                decision.code or ACCESS_LEVEL_REQUIRED,
                data={
                    "course_id": int(course.id),
                    "access_type": course.access_type,
                    "mode": state.mode,
                    "epoch": state.epoch,
                    "reason": decision.reason,
                },
            )
        preview = decision.preview

        profiles = self._profiles(asset)
        if not profiles:
            raise AppError(Code.MEDIA_NOT_READY, data={"reason": "profiles_empty"})

        ttl_ts = self.config.get_int("media.ttl_ts")
        ttl_playlist, ttl_key = self._ttls(state)
        expires = moment + timedelta(seconds=ttl_ts)
        url_exp = int((moment + timedelta(seconds=ttl_playlist)).timestamp())
        nonce = secrets.token_hex(NONCE_BYTES)
        ksession_id = secrets.token_hex(16)
        access_hash = self._access_hash(
            course, request.user_id, preview, class_member, member_level
        )

        with Session(self.db.engine) as session:
            session.execute(
                update(MediaKeySession)
                .where(
                    MediaKeySession.user_id == request.user_id,
                    MediaKeySession.lesson_id == request.lesson_id,
                    MediaKeySession.revoked_at.is_(None),
                    MediaKeySession.superseded_at.is_(None),
                )
                .values(superseded_at=moment, updated_at=moment)
            )
            session.add(
                MediaKeySession(
                    ksession_id=ksession_id,
                    user_id=request.user_id,
                    lesson_id=request.lesson_id,
                    course_id=int(course.id),
                    kid=str(asset.kid),
                    profiles=json.dumps(profiles),
                    preview=preview,
                    ip_family=_ip_family(request.ip),
                    access_hash=access_hash,
                    nonce=nonce,
                    nonce_expires_at=moment + timedelta(seconds=ttl_playlist),
                    issued_at=moment,
                    expires_at=expires,
                    created_at=moment,
                    updated_at=moment,
                )
            )
            session.commit()

        ctx = self._ctx(str(asset.kid), ksession_id, nonce, url_exp)
        return {
            "lesson_id": request.lesson_id,
            "course_id": int(course.id),
            "kid": str(asset.kid),
            "asset_status": asset.status,
            "preview": preview,
            "profiles": profiles,
            "playlist_url": ctx.master_url(),
            "playlist_ttl": ttl_playlist,
            "key_ttl": ttl_key,
            "ticket_expires_at": _iso(expires),
            "resume_second": self._resume(request.user_id, request.lesson_id),
            "watermark": self._watermark(request.username, request.ip),
            "metric_version": ZHY_PROGRESS_METRIC_VERSION,
        }

    @staticmethod
    def _watermark(username: str, ip: str) -> dict[str, Any]:
        return {
            "text": username,
            "opacity": 0.12,
            "position": "br",
        }

    @staticmethod
    def _profiles(asset: MediaAsset) -> list[str]:
        try:
            raw = json.loads(str(asset.profiles or "[]"))
        except json.JSONDecodeError:
            return []
        return [item for item in raw if item in PROFILE_NAMES]

    @staticmethod
    def _access_hash(
        course: EduCourse,
        user_id: int,
        preview: bool,
        class_member: bool,
        member_level: int | None = None,
    ) -> str:
        raw = (
            f"{int(course.id)}|{course.status}|{course.access_type}|{user_id}"
            f"|{int(preview)}|{int(class_member)}"
        )
        # 会员等级是授权输入：到期/升级都会让旧票据失配（40204）
        if course.access_type == "member":
            raw += f"|m{int(member_level or 0)}"
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:32]

    def _ttls(self, state: ModeState) -> tuple[int, int]:
        """模式策略锁定的票据 TTL：与全局配置取小，保证只收紧不放宽。"""
        policy = policy_for(state)
        playlist = min(self.config.get_int("media.ttl_playlist"), max(60, policy.ttl_playlist))
        key = min(self.config.get_int("media.ttl_key"), max(30, policy.ttl_key))
        return playlist, key

    def _class_member(self, user_id: int) -> bool:
        with Session(self.db.engine) as session:
            return is_class_member(session, user_id)

    def _member_level(self, user_id: int) -> int | None:
        with Session(self.db.engine) as session:
            return active_level(session, user_id)

    def _resume(self, user_id: int, lesson_id: int) -> int:
        with Session(self.db.engine) as session:
            value = session.execute(
                select(LearnProgress.last_position).where(
                    LearnProgress.user_id == user_id, LearnProgress.lesson_id == lesson_id
                )
            ).scalar()
        return int(value or 0)

    def _load(self, lesson_id: int) -> tuple[EduCourseLesson, EduCourse, MediaAsset]:
        with Session(self.db.engine) as session:
            lesson = session.get(EduCourseLesson, lesson_id)
            if lesson is None or lesson.deleted_at is not None:
                raise AppError(NOT_FOUND, data={"lesson_id": lesson_id})
            course = session.get(EduCourse, lesson.course_id)
            if course is None or course.deleted_at is not None:
                raise AppError(NOT_FOUND, data={"lesson_id": lesson_id})
            asset = session.execute(
                select(MediaAsset).where(MediaAsset.lesson_id == lesson_id)
            ).scalar_one_or_none()
            if asset is None:
                raise AppError(Code.MEDIA_NOT_READY, data={"lesson_id": lesson_id})
            session.expunge_all()
            return lesson, course, asset

    # ── playlist ──────────────────────────────────────────────────────────

    def playlist(
        self,
        kid: str,
        profile: str | None,
        *,
        ks: str,
        nonce: str,
        exp: str,
        sig: str,
        now: datetime | None = None,
    ) -> str:
        moment = now or datetime.now(UTC)
        row = self._verify_session(ks, kid=kid, moment=moment)
        allowed = tuple(json.loads(str(row.profiles)))
        ticket = decide_ticket(
            TicketSpec(
                nonce_state="first",
                requested_profile=profile,
                allowed_profiles=allowed,
                preview=bool(row.preview),
            )
        )
        if not ticket.allowed:
            raise AppError(
                ticket.code or Code.MEDIA_TICKET_MISMATCH,
                data={"kid": kid, "reason": ticket.reason},
            )
        allowed = ticket.profiles
        if not verify_query(self._sign_secret(), sig, "pl", ks, kid, nonce, exp):
            raise AppError(
                Code.MEDIA_TICKET_MISMATCH, data={"kid": kid, "reason": "signature_invalid"}
            )
        if self._int(exp) <= int(moment.timestamp()):
            raise AppError(Code.MEDIA_TICKET_EXPIRED, data={"kid": kid, "reason": "url_expired"})
        if nonce != row.nonce:
            raise AppError(Code.MEDIA_TICKET_EXPIRED, data={"kid": kid, "reason": "playlist_stale"})
        self._ensure_access(row)

        state = get_mode_context(self.config, db=self.db).snapshot()
        _ttl_playlist, ttl_key = self._ttls(state)
        root = self.hls_dir(int(row.lesson_id))
        new_nonce, new_exp = self._rotate(row, moment)
        ctx = self._ctx(str(row.kid), ks, new_nonce, new_exp)
        try:
            if profile is None:
                text = (root / MASTER_NAME).read_text(encoding="utf-8")
                return rewrite_master(text, root, ctx=ctx, allowed=allowed)
            key_url = f"{ctx.base}{KEY_PREFIX}/{row.kid}?" + self._query(
                "key",
                ks,
                str(row.kid),
                new_nonce,
                int((moment + timedelta(seconds=ttl_key)).timestamp()),
            )
            text = (root / profile / "index.m3u8").read_text(encoding="utf-8")
            return rewrite_variant(text, root, profile=profile, ctx=ctx, key_url=key_url)
        except OSError as exc:
            raise AppError(Code.MEDIA_PLAYBACK_UNRECOVERABLE, data={"kid": kid}) from exc
        except HlsVerifyError as exc:
            raise AppError(
                Code.MEDIA_PLAYBACK_UNRECOVERABLE, data={"kid": kid, "reason": exc.reason}
            ) from exc

    # ── key ───────────────────────────────────────────────────────────────

    def key(
        self,
        kid: str,
        *,
        ks: str,
        nonce: str,
        exp: str,
        sig: str,
        now: datetime | None = None,
    ) -> bytes:
        moment = now or datetime.now(UTC)
        row = self._verify_session(ks, kid=kid, moment=moment)
        if not verify_query(self._sign_secret(), sig, "key", ks, kid, nonce, exp):
            raise AppError(
                Code.MEDIA_TICKET_MISMATCH, data={"kid": kid, "reason": "signature_invalid"}
            )
        if self._int(exp) <= int(moment.timestamp()):
            raise AppError(Code.MEDIA_TICKET_EXPIRED, data={"kid": kid, "reason": "url_expired"})
        if nonce != row.nonce:
            raise AppError(Code.MEDIA_TICKET_EXPIRED, data={"kid": kid, "reason": "playlist_stale"})
        self._ensure_access(row)
        self._consume_nonce(row, moment)
        try:
            secret = load_master_secret(self.config, storage_root=self._storage)
        except MediaKeyError as exc:
            raise AppError(Code.MEDIA_PLAYBACK_UNRECOVERABLE, data={"reason": exc.reason}) from exc
        return derive_aes_key(secret, kid)

    # ── 分片 ──────────────────────────────────────────────────────────────

    def segment_path(self, kid: str, profile: str, name: str) -> Path:
        """返回分片绝对路径；名字/档位/目录全部按白名单校验（防路径穿越）。"""
        if not _KID.fullmatch(kid):
            raise AppError(NOT_FOUND, data={"reason": "kid_invalid"})
        if profile not in PROFILE_NAMES:
            raise AppError(NOT_FOUND, data={"reason": "profile_invalid"})
        if not _SEGMENT.fullmatch(name) or name.startswith("."):
            raise AppError(NOT_FOUND, data={"reason": "segment_invalid"})
        with Session(self.db.engine) as session:
            lesson_id = session.execute(
                select(MediaAsset.lesson_id).where(
                    MediaAsset.kid == kid, MediaAsset.status == "encoded"
                )
            ).scalar_one_or_none()
        if lesson_id is None:
            raise AppError(NOT_FOUND, data={"reason": "kid_unknown"})
        path = self._storage / "media" / str(int(lesson_id)) / "hls" / profile / name
        if not path.is_file():
            raise AppError(NOT_FOUND, data={"reason": "segment_missing"})
        return path

    # ── 会话校验/轮换 ─────────────────────────────────────────────────────

    def _verify_session(self, ks: str, *, kid: str, moment: datetime) -> MediaKeySession:
        if not _KSESSION.fullmatch(ks) or not _KID.fullmatch(kid):
            raise AppError(Code.MEDIA_TICKET_MISMATCH, data={"kid": kid, "reason": "malformed"})
        with Session(self.db.engine) as session:
            row = session.execute(
                select(MediaKeySession).where(MediaKeySession.ksession_id == ks)
            ).scalar_one_or_none()
            if row is None:
                raise AppError(Code.MEDIA_TICKET_EXPIRED, data={"reason": "session_missing"})
            session.expunge_all()
        if row.revoked_at is not None or row.expires_at <= moment:
            raise AppError(Code.MEDIA_TICKET_EXPIRED, data={"reason": "session_expired"})
        grace = self.config.get_int("media.superseded_grace_sec")
        if row.superseded_at is not None and row.superseded_at + timedelta(seconds=grace) < moment:
            raise AppError(Code.MEDIA_TICKET_EXPIRED, data={"reason": "session_superseded"})
        if str(row.kid) != kid:
            raise AppError(
                Code.MEDIA_TICKET_MISMATCH, data={"kid": kid, "reason": "kid_not_in_session"}
            )
        return row

    def _ensure_access(self, row: MediaKeySession) -> None:
        _lesson, course, _asset = self._load(int(row.lesson_id))
        # 会员/班级策略变化会让 access_hash 失配；退班与停班立即 40204
        class_member = (
            False
            if bool(row.preview) or course.access_type != "class"
            else self._class_member(int(row.user_id))
        )
        member_level = (
            None
            if bool(row.preview) or course.access_type != "member"
            else self._member_level(int(row.user_id))
        )
        expected = self._access_hash(
            course, int(row.user_id), bool(row.preview), class_member, member_level
        )
        if expected != str(row.access_hash):
            raise AppError(
                Code.MEDIA_ACCESS_REVOKED,
                data={"lesson_id": int(row.lesson_id), "reason": "policy_changed"},
            )

    def _rotate(self, row: MediaKeySession, moment: datetime) -> tuple[str, int]:
        nonce = secrets.token_hex(NONCE_BYTES)
        state = get_mode_context(self.config, db=self.db).snapshot()
        ttl = self._ttls(state)[0]
        expires_at = int((moment + timedelta(seconds=ttl)).timestamp())
        with Session(self.db.engine) as session:
            session.execute(
                update(MediaKeySession)
                .where(MediaKeySession.ksession_id == row.ksession_id)
                .values(
                    nonce=nonce,
                    nonce_used_at=None,
                    nonce_expires_at=moment + timedelta(seconds=ttl),
                    updated_at=moment,
                )
            )
            session.commit()
        return nonce, expires_at

    def _consume_nonce(self, row: MediaKeySession, moment: datetime) -> None:
        """nonce 单次消费：窗口内允许播放器重复拉取，窗口外一律 40206。"""
        window = self.config.get_int("media.nonce_reuse_window_sec")
        cutoff = moment - timedelta(seconds=window)
        with Session(self.db.engine) as session:
            current = session.get(MediaKeySession, row.id)
            if current is None:
                raise AppError(Code.MEDIA_TICKET_EXPIRED, data={"reason": "session_missing"})
            used = current.nonce_used_at
            within = used is not None and window > 0 and used > cutoff
            decision = decide_ticket(
                TicketSpec(
                    nonce_state="first" if used is None or within else "replay",
                    preview=bool(current.preview),
                )
            )
            if not decision.allowed:
                raise AppError(
                    decision.code or Code.MEDIA_TICKET_REPLAY,
                    data={"kid": str(row.kid), "reason": decision.reason},
                )
            if used is not None:
                return  # 复用窗口内（播放器对同一 key URI 的重复请求）
            result = cast(
                CursorResult[Any],
                session.execute(
                    update(MediaKeySession)
                    .where(MediaKeySession.id == row.id)
                    .where(MediaKeySession.nonce == row.nonce)
                    .where(MediaKeySession.nonce_used_at.is_(None))
                    .values(nonce_used_at=moment, updated_at=moment)
                ),
            )
            session.commit()
            if not result.rowcount:
                raise AppError(
                    Code.MEDIA_TICKET_REPLAY,
                    data={"kid": str(row.kid), "reason": "nonce_replay"},
                )

    # ── 签名 ──────────────────────────────────────────────────────────────

    def _sign_secret(self) -> bytes:
        try:
            return load_master_secret(self.config, storage_root=self._storage)
        except MediaKeyError as exc:
            raise AppError(Code.MEDIA_PLAYBACK_UNRECOVERABLE, data={"reason": exc.reason}) from exc

    def _query(self, purpose: str, ks: str, kid: str, nonce: str, exp: int) -> str:
        sig = sign_query(self._sign_secret(), purpose, ks, kid, nonce, str(exp))
        return f"ks={ks}&n={nonce}&exp={exp}&sig={sig}"

    def _ctx(self, kid: str, ks: str, nonce: str, exp: int) -> RewriteContext:
        return RewriteContext(
            kid=kid,
            base=self.config.get_str("media.cdn_base").rstrip("/"),
            playlist_prefix=PLAYLIST_PREFIX,
            segment_prefix=SEGMENT_PREFIX,
            query=self._query("pl", ks, kid, nonce, exp),
        )

    @staticmethod
    def _int(value: str) -> int:
        try:
            return int(value)
        except ValueError as exc:
            raise AppError(Code.MEDIA_TICKET_MISMATCH, data={"reason": "exp_invalid"}) from exc


__all__ = ["PLAYLIST_PREFIX", "SEGMENT_PREFIX", "PlaybackService", "TicketRequest"]
