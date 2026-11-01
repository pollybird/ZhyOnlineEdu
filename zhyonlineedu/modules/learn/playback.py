"""播放票据与密钥会话（§10.3.3 / §10.3.4 · F3/F4/F8 · M2.5）。

**核心口径：没有 ticket 就拿不到 key。**
- `kid` 只标识资产（转码时生成），与用户无关，因此 m3u8/ts 可上 CDN；
- 用户身份绑定在 `media_keysession`：playlist 的 `#EXT-X-KEY` URI 携带
  `ks`（会话）+ `n`（nonce）+ `exp`（失效 Unix 秒）+ `sig`（HMAC），key 端点逐项校验；
- 三层 TTL：keysession（`media.ttl_ts`）> playlist（`ttl_playlist`）> key（`ttl_key`）；
  playlist 有效期内**不轮换 nonce**：master 下发的各档位 URI 必须可复用，否则
  hls.js/原生 HLS 的 ABR 切换（直接使用 master 里的 URI）必然 40201 `playlist_stale`；
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
    Decision,
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

    def issue(
        self,
        request: TicketRequest,
        *,
        now: datetime | None = None,
        max_profile: str | None = None,
        ttl_override: int | None = None,
    ) -> dict[str, Any]:
        """签发播放票据。

        `max_profile` / `ttl_override` 只服务于插件降权 SDK（§11.9）：
        档位向上裁剪、TTL 与模式策略取小，**不会**放宽授权。
        """
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
        decision, state, class_member, member_level = self._decide(request, lesson, course)
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

        profiles = self._profiles(asset, max_profile=max_profile)
        if not profiles:
            raise AppError(Code.MEDIA_NOT_READY, data={"reason": "profiles_empty"})

        ttl_ts = self.config.get_int("media.ttl_ts")
        ttl_playlist, ttl_key = self._ttls(state)
        if ttl_override is not None:
            ttl_ts = min(ttl_ts, ttl_override)
            ttl_playlist = min(ttl_playlist, ttl_override)
            ttl_key = min(ttl_key, ttl_override)
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

    def evaluate_lesson(
        self, *, user_id: int, lesson_id: int, can_preview: bool = False
    ) -> dict[str, Any]:
        """只读访问判定（§11.9 SDK）：不签票、不写进度，复用同一 `decide_access`。"""
        lesson, course = self._load_lesson(lesson_id)
        request = TicketRequest(
            user_id=user_id, username="", lesson_id=lesson_id, can_preview=can_preview
        )
        decision, state, _, _ = self._decide(request, lesson, course)
        return {
            "allowed": decision.allowed,
            "code": decision.code,
            "reason": decision.reason,
            "rule": decision.rule,
            "preview": decision.preview,
            "lesson_id": int(lesson_id),
            "course_id": int(course.id),
            "mode": state.mode,
            "epoch": state.epoch,
        }

    def _decide(
        self, request: TicketRequest, lesson: EduCourseLesson, course: EduCourse
    ) -> tuple[Decision, ModeState, bool, int | None]:
        """统一访问判定（§9.4 决策表 1~14）：票据与 SDK 只读视图共用。"""
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
        return decision, state, class_member, member_level

    @staticmethod
    def _watermark(username: str, ip: str) -> dict[str, Any]:
        return {
            "text": username,
            "opacity": 0.12,
            "position": "br",
        }

    @staticmethod
    def _profiles(asset: MediaAsset, *, max_profile: str | None = None) -> list[str]:
        try:
            raw = json.loads(str(asset.profiles or "[]"))
        except json.JSONDecodeError:
            return []
        profiles = [item for item in raw if item in PROFILE_NAMES]
        if max_profile is None:
            return profiles
        if max_profile not in PROFILE_NAMES:
            raise AppError(
                Code.MEDIA_TICKET_MISMATCH,
                data={"requested_profile": max_profile},
            )
        ceiling = PROFILE_NAMES.index(max_profile)
        capped = [item for item in profiles if PROFILE_NAMES.index(item) <= ceiling]
        # 资产没有低档位时取现有最低档，保证降权票仍可播放
        return capped or profiles[:1]

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

    def _load_lesson(self, lesson_id: int) -> tuple[EduCourseLesson, EduCourse]:
        with Session(self.db.engine) as session:
            lesson = session.get(EduCourseLesson, lesson_id)
            if lesson is None or lesson.deleted_at is not None:
                raise AppError(NOT_FOUND, data={"lesson_id": lesson_id})
            course = session.get(EduCourse, lesson.course_id)
            if course is None or course.deleted_at is not None:
                raise AppError(NOT_FOUND, data={"lesson_id": lesson_id})
            session.expunge_all()
            return lesson, course

    def _load(self, lesson_id: int) -> tuple[EduCourseLesson, EduCourse, MediaAsset]:
        lesson, course = self._load_lesson(lesson_id)
        with Session(self.db.engine) as session:
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
        ttl_playlist, ttl_key = self._ttls(state)
        root = self.hls_dir(int(row.lesson_id))
        # nonce 固定为票据签发时的值：一次 master 拉取会同时下发多个档位 URI，
        # 播放器 ABR 切换会直接复用这些 URI；若每次请求轮换 nonce，切换必然 40201。
        nonce = str(row.nonce)
        playlist_exp = int(
            min(moment + timedelta(seconds=ttl_playlist), row.expires_at).timestamp()
        )
        ctx = self._ctx(str(row.kid), ks, nonce, playlist_exp)
        try:
            if profile is None:
                text = (root / MASTER_NAME).read_text(encoding="utf-8")
                return rewrite_master(text, root, ctx=ctx, allowed=allowed)
            # key 过期时间固定为「票据签发时刻 + key TTL」：同一 ksession 下各档位、
            # 各次拉取得到的 key URI 字节一致，hls.js 才能按 URI 缓存密钥，
            # ABR 切档不会因 exp 变化触发 40206；到期由播放器换票续签。
            key_exp = int(
                min(row.issued_at + timedelta(seconds=ttl_key), row.expires_at).timestamp()
            )
            key_url = f"{ctx.base}{KEY_PREFIX}/{row.kid}?" + self._query(
                "key",
                ks,
                str(row.kid),
                nonce,
                key_exp,
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
