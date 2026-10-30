"""媒体资产与上传会话（§6.7 / §10.3.1 · M2.1 + M2.3 + M2.5）。

- `media_asset`：一课时一行，`encoded` 是唯一"可上架"完成态（M2.1）；
- `media_upload_session` / `media_chunk`：分片上传（M2.3），完整文件先落 tmp，
  `complete` 阶段做整体 sha256 复核 + ffprobe 魔数检疫后才允许进转码队列；
- `media_keysession`：播放票据会话（M2.5），kid 与用户解耦，key 接口只认会话；
- 临时文件的安全属性在服务层强制（`O_CREAT|O_EXCL`、0600、目录 0700、非 root），
  表里不保存任何绝对路径，避免路径注入与信息泄露。
"""

from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import BigInteger, Boolean, Integer, String, Text, UniqueConstraint, false
from sqlalchemy.orm import Mapped, mapped_column

from ..base import Base
from ..types import UTCDatetime


def utcnow() -> datetime:
    return datetime.now(UTC)


ASSET_STATUSES: tuple[str, ...] = ("pending", "uploaded", "transcoding", "encoded", "failed")
ASSET_READY = "encoded"

# 上传会话状态：open 接收分片 → processing 合并检疫 → completed/rejected
# expired/deleted 由 tmp-reaper 或显式删除置位，均不可再接收分片。
UPLOAD_STATUSES: tuple[str, ...] = (
    "open",
    "processing",
    "completed",
    "rejected",
    "expired",
    "deleted",
)

# 转码任务状态：queued → running → succeeded/failed；失败可自动回队列重试（attempt+1）
JOB_STATUSES: tuple[str, ...] = ("queued", "running", "succeeded", "failed", "canceled")


class MediaAsset(Base):
    """课时媒体资产：一课时一行（M2.4 若引入多版本，再按 revision 扩展）。"""

    __tablename__ = "media_asset"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    lesson_id: Mapped[int] = mapped_column(Integer, nullable=False, unique=True)
    status: Mapped[str] = mapped_column(
        String(16), nullable=False, default="pending", server_default="pending", index=True
    )
    # 转码产物变更 +1；续签时前端据此判断是否强制换 master url（§6.7）
    playlist_version: Mapped[int] = mapped_column(
        Integer, nullable=False, default=1, server_default="1"
    )
    duration_sec: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    error_code: Mapped[str | None] = mapped_column(String(32), nullable=True)
    # 转码产物（M2.4）：kid 与用户解耦（§10.3.3）；AES key 由主密钥 + kid 派生，不落库。
    kid: Mapped[str | None] = mapped_column(String(32), nullable=True, unique=True)
    profiles: Mapped[str | None] = mapped_column(Text, nullable=True)  # JSON: ["360","720"]
    poster_path: Mapped[str | None] = mapped_column(String(255), nullable=True)
    # JSON：ffmpeg 版本、沙箱后端/配额、输入 format_name 等事故回溯信息（绝不含密钥）
    sandbox_profile: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(UTCDatetime, nullable=False, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(UTCDatetime, nullable=False, default=utcnow)


class MediaUploadSession(Base):
    """分片上传会话（秒传/断点/分片/合并/入队）。

    `upload_id` 是客户端可见的唯一标识（32 位随机 hex），同时作为 tmp 子目录名；
    服务层用正则严格校验，禁止任何路径拼接注入。
    """

    __tablename__ = "media_upload_session"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    upload_id: Mapped[str] = mapped_column(String(32), nullable=False, unique=True)
    lesson_id: Mapped[int] = mapped_column(Integer, nullable=False, index=True)
    file_name: Mapped[str] = mapped_column(String(255), nullable=False)
    file_size: Mapped[int] = mapped_column(BigInteger, nullable=False)
    # 客户端声明的整文件 sha256（complete 阶段复核，不一致视为篡改）
    sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    chunk_size: Mapped[int] = mapped_column(Integer, nullable=False)
    total_chunks: Mapped[int] = mapped_column(Integer, nullable=False)
    status: Mapped[str] = mapped_column(
        String(16), nullable=False, default="open", server_default="open", index=True
    )
    error_code: Mapped[str | None] = mapped_column(String(32), nullable=True)
    # complete 阶段的检疫结果（事故回溯用，不外泄路径）
    format_name: Mapped[str | None] = mapped_column(String(64), nullable=True)
    duration_sec: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0"
    )
    width: Mapped[int | None] = mapped_column(Integer, nullable=True)
    height: Mapped[int | None] = mapped_column(Integer, nullable=True)
    probe_source: Mapped[str | None] = mapped_column(String(16), nullable=True)
    expires_at: Mapped[datetime] = mapped_column(UTCDatetime, nullable=False, index=True)
    completed_at: Mapped[datetime | None] = mapped_column(UTCDatetime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(UTCDatetime, nullable=False, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(UTCDatetime, nullable=False, default=utcnow)


class MediaChunk(Base):
    """分片元数据：只记录 md5/大小，分片字节落 tmp（目录 0700、文件 0600）。"""

    __tablename__ = "media_chunk"
    __table_args__ = (
        UniqueConstraint("session_id", "chunk_index", name="uq_media_chunk_session_index"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    session_id: Mapped[int] = mapped_column(Integer, nullable=False, index=True)
    chunk_index: Mapped[int] = mapped_column(Integer, nullable=False)
    md5: Mapped[str] = mapped_column(String(32), nullable=False)
    size_bytes: Mapped[int] = mapped_column(BigInteger, nullable=False)
    created_at: Mapped[datetime] = mapped_column(UTCDatetime, nullable=False, default=utcnow)


class MediaKeySession(Base):
    """播放密钥会话（§10.3.3 · F3/F4 · M2.5）。

    - kid 与用户解耦：`kid` 取自 asset，因此 m3u8/ts 可上 CDN；
    - 用户身份只绑定在这里：`ksession_id` 进 playlist 的 `#EXT-X-KEY` URI，
      key 接口校验归属 + 课时匹配 + nonce 单次 + access_hash 未变；
    - `nonce`/`nonce_used_at`/`nonce_expires_at` 三重约束防重放（40206）；
    - 旧会话在新会话签发后 `superseded_at` 保留一个宽限期，避免在途 key 请求失败。
    """

    __tablename__ = "media_keysession"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    ksession_id: Mapped[str] = mapped_column(String(48), nullable=False, unique=True)
    user_id: Mapped[int] = mapped_column(Integer, nullable=False, index=True)
    lesson_id: Mapped[int] = mapped_column(Integer, nullable=False, index=True)
    course_id: Mapped[int] = mapped_column(Integer, nullable=False)
    kid: Mapped[str] = mapped_column(String(32), nullable=False)
    profiles: Mapped[str] = mapped_column(Text, nullable=False)  # JSON: ["360","720"]
    # 超管预览：ticket 带 preview=true，进度落 log 但标 source=preview，不进统计
    preview: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default=false()
    )
    # 只记 /24(v4) 或 /56(v6) 网段；默认只作软信号（F8）
    ip_family: Mapped[str] = mapped_column(String(24), nullable=False, default="")
    # 授权判定指纹：策略变化（会员到期/课程下架）时换票即失效
    access_hash: Mapped[str] = mapped_column(String(32), nullable=False, default="")
    nonce: Mapped[str] = mapped_column(String(32), nullable=False, default="")
    nonce_used_at: Mapped[datetime | None] = mapped_column(UTCDatetime, nullable=True)
    nonce_expires_at: Mapped[datetime | None] = mapped_column(UTCDatetime, nullable=True)
    issued_at: Mapped[datetime] = mapped_column(UTCDatetime, nullable=False, default=utcnow)
    expires_at: Mapped[datetime] = mapped_column(UTCDatetime, nullable=False, index=True)
    superseded_at: Mapped[datetime | None] = mapped_column(UTCDatetime, nullable=True)
    revoked_at: Mapped[datetime | None] = mapped_column(UTCDatetime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(UTCDatetime, nullable=False, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(UTCDatetime, nullable=False, default=utcnow)


class MediaTranscodeJob(Base):
    """转码任务（M2.4）：DB-backed 队列，worker 用 `status='queued'` 条件更新抢占。

    - `err` 只存 stderr 尾部 2KB，且仅供管理员排障，绝不回吐前端（§10.3.2.3 ⑧）；
    - `sandbox` 记录本次实际沙箱参数与 ffmpeg 版本，供 CVE 追溯；
    - 失败自动重试时回到 queued 并 `attempt+1`，`run_after` 做退避。
    """

    __tablename__ = "media_transcode_job"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    asset_id: Mapped[int] = mapped_column(Integer, nullable=False, index=True)
    lesson_id: Mapped[int] = mapped_column(Integer, nullable=False, index=True)
    upload_id: Mapped[str | None] = mapped_column(String(32), nullable=True)
    status: Mapped[str] = mapped_column(
        String(16), nullable=False, default="queued", server_default="queued", index=True
    )
    attempt: Mapped[int] = mapped_column(Integer, nullable=False, default=1, server_default="1")
    profiles: Mapped[str] = mapped_column(Text, nullable=False)
    error_code: Mapped[str | None] = mapped_column(String(32), nullable=True)
    err: Mapped[str | None] = mapped_column(String(2048), nullable=True)
    sandbox: Mapped[str | None] = mapped_column(Text, nullable=True)
    run_after: Mapped[datetime | None] = mapped_column(UTCDatetime, nullable=True, index=True)
    started_at: Mapped[datetime | None] = mapped_column(UTCDatetime, nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(UTCDatetime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(UTCDatetime, nullable=False, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(UTCDatetime, nullable=False, default=utcnow)
