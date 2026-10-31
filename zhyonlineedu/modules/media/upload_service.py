"""分片上传服务（§10.3.1 · M2.3）。

流程：秒传 / 断点 / 分片 / 合并 / 入队（入队由 M2.4 转码 worker 消费 tmp 中的合并文件）。

安全口径（V2.3 加固，必须与测试 `tests/test_media_upload.py` 同步）：
- `upload_id` 32 位 hex 严格校验，直接作为 tmp 子目录名，禁止任何路径拼接；
- tmp 根目录与每会话目录 `0700`，分片/合并文件 `0600`，写入用 `O_CREAT|O_EXCL`；
- 属主为 root 时非 dev 环境直接拒绝（`tmp-reaper` 兜底清理）；
- 分片 md5 不符 → `50101`；complete 缺片 → `50102`（附缺失 index）；
- complete 先复核整文件 sha256，再跑魔数/ffprobe 检疫，不合规 → `50103` 并删除临时文件；
- 合并文件 10 分钟未消费由 `reap()`（`manage.py tmp-reaper`）清理。
"""

from __future__ import annotations

import hashlib
import logging
import os
import re
import secrets
import shutil
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, NoReturn, cast

from sqlalchemy import delete, func, select, update
from sqlalchemy.engine import CursorResult
from sqlalchemy.orm import Session

from zhyonlineedu.core.codes import (
    INTERNAL_ERROR,
    NOT_FOUND,
    VALIDATION_FAILED,
    Code,
)
from zhyonlineedu.core.config import Config
from zhyonlineedu.core.db.engine import Database, ensure_database
from zhyonlineedu.core.db.models.catalog import EduCourseLesson
from zhyonlineedu.core.db.models.media import (
    MediaAsset,
    MediaChunk,
    MediaTranscodeJob,
    MediaUploadSession,
)
from zhyonlineedu.core.errors import AppError
from zhyonlineedu.core.media.quarantine import MediaInfo, MediaRejected, Prober, probe_media

logger = logging.getLogger(__name__)

_UPLOAD_ID = re.compile(r"^[0-9a-f]{32}$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_MD5 = re.compile(r"^[0-9a-f]{32}$")

CHUNK_MIN_BYTES = 1024
CHUNK_MAX_BYTES = 67_108_864
MERGED_NAME = "merged.bin"
_COPY_BLOCK = 1024 * 1024


def _utcnow() -> datetime:
    return datetime.now(UTC)


def _iso(value: datetime | None) -> str | None:
    return value.isoformat(timespec="seconds") if value is not None else None


def _fail(message: str) -> NoReturn:
    raise AppError(VALIDATION_FAILED, data={"errors": [message]})


def _int(value: object, field: str, *, minimum: int, maximum: int | None = None) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        _fail(f"{field} 必须是整数")
    number = value
    if number < minimum:
        _fail(f"{field} 不能小于 {minimum}")
    if maximum is not None and number > maximum:
        _fail(f"{field} 不能大于 {maximum}")
    return number


def _hash_text(value: object, field: str, pattern: re.Pattern[str]) -> str:
    if not isinstance(value, str):
        _fail(f"{field} 必须是十六进制字符串")
    text = value.strip().lower()
    if not pattern.fullmatch(text):
        _fail(f"{field} 格式不合法")
    return text


def _safe_file_name(value: object) -> str:
    if not isinstance(value, str):
        _fail("file_name 必须是字符串")
    name = value.strip()
    if name == "" or len(name) > 255:
        _fail("file_name 长度必须在 1..255")
    if "\x00" in name or "/" in name or "\\" in name or name in {".", ".."}:
        _fail("file_name 不能包含路径分隔符")
    return name


def _query_index(value: object) -> int:
    if isinstance(value, int):
        return _int(value, "index", minimum=0)
    if isinstance(value, str) and value.isdigit():
        return _int(int(value), "index", minimum=0)
    _fail("index 必须是整数")


def _ensure_dir(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)
    os.chmod(path, 0o700)


def _write_exclusive(path: Path, data: bytes) -> None:
    """`O_CREAT|O_EXCL` + 0600：同名分片不会被静默覆盖。"""
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
    except BaseException:
        path.unlink(missing_ok=True)
        raise


def _file_md5(path: Path) -> str:
    digest = hashlib.md5(usedforsecurity=False)  # noqa: S324 - 分片完整性校验，非安全散列
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(_COPY_BLOCK), b""):
            digest.update(block)
    return digest.hexdigest()


class UploadService:
    """无状态服务：每个动作自建短会话，文件系统操作与 DB 状态在同一用例内完成。"""

    def __init__(
        self,
        config: Config,
        *,
        db: Database | None = None,
        prober: Prober | None = None,
        root: Path | None = None,
    ) -> None:
        self.config = config
        self._db = db
        self._prober = prober
        self._root = Path(root) if root is not None else Path(config.get_str("upload.tmp_root"))
        self._root_checked = False

    @property
    def db(self) -> Database:
        if self._db is None:
            self._db = ensure_database(self.config)
        return self._db

    @property
    def root(self) -> Path:
        return self._root

    # ── 初始化 ────────────────────────────────────────────────────────────

    def init(self, body: Mapping[str, Any]) -> dict[str, Any]:
        self._ensure_root()
        lesson_id = _int(body.get("lesson_id"), "lesson_id", minimum=1)
        file_name = _safe_file_name(body.get("file_name"))
        max_file = self.config.get_int("upload.max_file_bytes")
        file_size = _int(body.get("file_size"), "file_size", minimum=1, maximum=max_file)
        sha256 = _hash_text(body.get("sha256"), "sha256", _SHA256)
        raw_chunk = body.get("chunk_size")
        chunk_size = (
            self.config.get_int("upload.chunk_size")
            if raw_chunk is None
            else _int(raw_chunk, "chunk_size", minimum=CHUNK_MIN_BYTES, maximum=CHUNK_MAX_BYTES)
        )
        total_chunks = (file_size + chunk_size - 1) // chunk_size
        max_chunks = self.config.get_int("upload.max_chunks")
        if total_chunks > max_chunks:
            _fail(f"分片数 {total_chunks} 超过上限 {max_chunks}，请增大 chunk_size")

        now = _utcnow()
        expires_at = now + timedelta(seconds=self.config.get_int("upload.session_ttl_sec"))
        upload_id = secrets.token_hex(16)

        with Session(self.db.engine) as session:
            lesson = session.get(EduCourseLesson, lesson_id)
            if lesson is None or lesson.deleted_at is not None:
                raise AppError(NOT_FOUND, data={"lesson_id": lesson_id})
            session.add(
                MediaUploadSession(
                    upload_id=upload_id,
                    lesson_id=lesson_id,
                    file_name=file_name,
                    file_size=file_size,
                    sha256=sha256,
                    chunk_size=chunk_size,
                    total_chunks=total_chunks,
                    status="open",
                    duration_sec=0,
                    expires_at=expires_at,
                    created_at=now,
                    updated_at=now,
                )
            )
            session.commit()

        _ensure_dir(self._root / upload_id)
        return {
            "upload_id": upload_id,
            "status": "open",
            "chunk_size": chunk_size,
            "total_chunks": total_chunks,
            "expires_at": _iso(expires_at),
        }

    # ── 分片 ──────────────────────────────────────────────────────────────

    def chunk(self, upload_id: str, index: object, md5: object, data: bytes) -> dict[str, Any]:
        self._check_upload_id(upload_id)
        chunk_index = _query_index(index)
        digest = _hash_text(md5, "md5", _MD5)
        if len(data) == 0:
            _fail("分片内容为空")

        now = _utcnow()
        with Session(self.db.engine) as session:
            row = self._row(session, upload_id)
            self._ensure_open(session, row)
            if chunk_index >= row.total_chunks:
                _fail(f"index 必须在 0..{row.total_chunks - 1}")
            if len(data) > row.chunk_size:
                _fail(f"分片大小不能超过 chunk_size={row.chunk_size}")

            actual = hashlib.md5(data, usedforsecurity=False).hexdigest()  # noqa: S324
            if actual != digest:
                raise AppError(
                    Code.UPLOAD_CHUNK_MD5_MISMATCH,
                    data={"upload_id": upload_id, "index": chunk_index},
                )

            target = self._root / upload_id / f"chunk_{chunk_index:06d}.part"
            _ensure_dir(target.parent)
            duplicated = self._persist_chunk(target, data, digest)

            existing = session.execute(
                select(MediaChunk).where(
                    MediaChunk.session_id == row.id,
                    MediaChunk.chunk_index == chunk_index,
                )
            ).scalar_one_or_none()
            if existing is None:
                session.add(
                    MediaChunk(
                        session_id=row.id,
                        chunk_index=chunk_index,
                        md5=digest,
                        size_bytes=len(data),
                        created_at=now,
                    )
                )
            else:
                existing.md5 = digest
                existing.size_bytes = len(data)
            row.updated_at = now
            total_chunks = int(row.total_chunks)
            session.commit()
            received = session.execute(
                select(func.count()).select_from(MediaChunk).where(MediaChunk.session_id == row.id)
            ).scalar_one()

        return {
            "upload_id": upload_id,
            "index": chunk_index,
            "received": int(received),
            "total_chunks": total_chunks,
            "duplicated": duplicated,
        }

    # ── 状态 ──────────────────────────────────────────────────────────────

    def status(self, upload_id: str) -> dict[str, Any]:
        self._check_upload_id(upload_id)
        with Session(self.db.engine) as session:
            row = self._row(session, upload_id)
            self._ensure_not_expired(session, row)
            received = self._received_indexes(session, row)
            return self._status_payload(row, received)

    # ── 完成（合并 + sha256 复核 + 魔数检疫）──────────────────────────────

    def complete(self, upload_id: str) -> dict[str, Any]:
        self._check_upload_id(upload_id)
        now = _utcnow()
        with Session(self.db.engine) as session:
            row = self._row(session, upload_id)
            self._ensure_not_expired(session, row)
            if row.status == "completed":
                return self._completed_payload(session, row)
            if row.status == "rejected":
                raise AppError(
                    Code.UPLOAD_MALICIOUS,
                    data={"upload_id": upload_id, "reason": row.error_code or "rejected"},
                )
            if row.status == "processing":
                raise AppError(
                    "CONFLICT_RETRY",
                    data={"upload_id": upload_id, "status": row.status},
                )

            claimed = cast(
                CursorResult[Any],
                session.execute(
                    update(MediaUploadSession)
                    .where(
                        MediaUploadSession.id == row.id,
                        MediaUploadSession.status == "open",
                    )
                    .values(status="processing", updated_at=now)
                ),
            )
            session.commit()
            if claimed.rowcount != 1:
                raise AppError("CONFLICT_RETRY", data={"upload_id": upload_id})

            lesson_id = row.lesson_id
            total = row.total_chunks
            declared_sha = row.sha256

            received = self._received_indexes(session, row)
            missing = [index for index in range(total) if index not in received]
            if missing:
                self._reopen(session, row, now)
                raise AppError(
                    Code.UPLOAD_CHUNK_MISSING,
                    data={"upload_id": upload_id, "missing": missing},
                )

            directory = self._root / upload_id
            absent = [
                index
                for index in range(total)
                if not (directory / f"chunk_{index:06d}.part").is_file()
            ]
            if absent:
                self._reopen(session, row, now)
                raise AppError(
                    Code.UPLOAD_CHUNK_MISSING,
                    data={"upload_id": upload_id, "missing": absent},
                )

            merged = directory / MERGED_NAME
            merged.unlink(missing_ok=True)
            actual_sha = self._merge(directory, merged, total)
            if actual_sha != declared_sha:
                self._reject(session, row, "sha256_mismatch", None, now)
                raise AppError(
                    Code.UPLOAD_MALICIOUS,
                    data={"upload_id": upload_id, "reason": "sha256_mismatch"},
                )

            try:
                info = probe_media(
                    merged,
                    allowed_formats=self.config.get_list("transcode.allowed_formats"),
                    max_duration_sec=self.config.get_int("transcode.input_max_duration_sec"),
                    max_dimension=self.config.get_int("transcode.input_max_dimension"),
                    require_ffprobe=self.config.get_bool("upload.require_ffprobe"),
                    prober=self._prober,
                )
            except MediaRejected as exc:
                self._reject(session, row, exc.reason, exc.format_name, _utcnow())
                raise AppError(
                    Code.UPLOAD_MALICIOUS,
                    data={
                        "upload_id": upload_id,
                        "reason": exc.reason,
                        "format_name": exc.format_name,
                    },
                ) from None

            asset = self._touch_asset(session, lesson_id, info, now)
            row.status = "completed"
            row.error_code = None
            row.format_name = info.format_name
            row.duration_sec = info.duration_sec
            row.width = info.width
            row.height = info.height
            row.probe_source = info.source
            row.completed_at = now
            row.updated_at = now
            session.commit()
            payload = {
                "upload_id": upload_id,
                "status": "completed",
                "lesson_id": lesson_id,
                "asset_status": asset.status,
                "format_name": info.format_name,
                "duration_sec": info.duration_sec,
                "width": info.width,
                "height": info.height,
                "probe_source": info.source,
                "sha256": actual_sha,
                "ready_for_transcode": True,
            }

        logger.info(
            "upload completed lesson=%s upload_id=%s format=%s duration=%ss",
            lesson_id,
            upload_id,
            info.format_name,
            info.duration_sec,
        )
        return payload

    # ── 删除 / 回收 ───────────────────────────────────────────────────────

    def delete(self, upload_id: str) -> dict[str, Any]:
        self._check_upload_id(upload_id)
        with Session(self.db.engine) as session:
            row = self._row(session, upload_id)
            if row.status == "deleted":
                raise AppError(NOT_FOUND, data={"upload_id": upload_id})
            row.status = "deleted"
            row.updated_at = _utcnow()
            session.execute(delete(MediaChunk).where(MediaChunk.session_id == row.id))
            session.commit()
        self._remove_dir(upload_id)
        return {"upload_id": upload_id, "deleted": True}

    def reap(self, *, now: datetime | None = None) -> int:
        """tmp-reaper：清理过期会话与"合并后 10 分钟未消费"的临时目录。"""
        reference = now or _utcnow()
        tmp_ttl = self.config.get_int("upload.tmp_ttl_sec")
        removed = 0
        with Session(self.db.engine) as session:
            rows = (
                session.execute(
                    select(MediaUploadSession).where(
                        MediaUploadSession.status.in_(
                            ("open", "processing", "completed", "rejected")
                        )
                    )
                )
                .scalars()
                .all()
            )
            for row in rows:
                if row.status in ("open", "processing"):
                    deadline = row.expires_at
                else:
                    deadline = row.updated_at + timedelta(seconds=tmp_ttl)
                if deadline >= reference:
                    continue
                # 转码任务已入队但 worker 尚未搬走合并文件时，不能把源删了
                if self._consumed_by_job(session, row.upload_id):
                    continue
                row.status = "expired"
                row.updated_at = reference
                session.execute(delete(MediaChunk).where(MediaChunk.session_id == row.id))
                self._remove_dir(row.upload_id)
                removed += 1
            session.commit()
        return removed

    def _consumed_by_job(self, session: Session, upload_id: str) -> bool:
        merged = self._root / upload_id / MERGED_NAME
        if not merged.is_file():
            return False
        active = session.execute(
            select(func.count())
            .select_from(MediaTranscodeJob)
            .where(
                MediaTranscodeJob.upload_id == upload_id,
                MediaTranscodeJob.status.in_(("queued", "running")),
            )
        ).scalar_one()
        return int(active) > 0

    # ── 内部：DB ──────────────────────────────────────────────────────────

    def _row(self, session: Session, upload_id: str) -> MediaUploadSession:
        row = session.execute(
            select(MediaUploadSession).where(MediaUploadSession.upload_id == upload_id)
        ).scalar_one_or_none()
        if row is None:
            raise AppError(NOT_FOUND, data={"upload_id": upload_id})
        return row

    def _ensure_open(self, session: Session, row: MediaUploadSession) -> None:
        if row.status == "open":
            return
        if row.status == "rejected":
            raise AppError(
                Code.UPLOAD_MALICIOUS,
                data={"upload_id": row.upload_id, "reason": row.error_code or "rejected"},
            )
        if row.status in ("expired", "deleted"):
            raise AppError(NOT_FOUND, data={"upload_id": row.upload_id})
        _fail(f"上传会话状态为 {row.status}，不能继续接收分片")

    def _ensure_not_expired(self, session: Session, row: MediaUploadSession) -> None:
        if row.status != "open" or row.expires_at >= _utcnow():
            return
        row.status = "expired"
        row.updated_at = _utcnow()
        session.commit()
        self._remove_dir(row.upload_id)
        raise AppError(NOT_FOUND, data={"upload_id": row.upload_id})

    def _received_indexes(self, session: Session, row: MediaUploadSession) -> list[int]:
        values = (
            session.execute(
                select(MediaChunk.chunk_index)
                .where(MediaChunk.session_id == row.id)
                .order_by(MediaChunk.chunk_index)
            )
            .scalars()
            .all()
        )
        return [int(value) for value in values]

    def _status_payload(self, row: MediaUploadSession, received: Sequence[int]) -> dict[str, Any]:
        missing = [index for index in range(row.total_chunks) if index not in set(received)]
        return {
            "upload_id": row.upload_id,
            "status": row.status,
            "file_name": row.file_name,
            "file_size": row.file_size,
            "chunk_size": row.chunk_size,
            "total_chunks": row.total_chunks,
            "received_chunks": list(received),
            "missing_chunks": missing,
            "expires_at": _iso(row.expires_at),
            "error_code": row.error_code,
        }

    def _completed_payload(self, session: Session, row: MediaUploadSession) -> dict[str, Any]:
        asset = session.execute(
            select(MediaAsset).where(MediaAsset.lesson_id == row.lesson_id)
        ).scalar_one_or_none()
        return {
            "upload_id": row.upload_id,
            "status": "completed",
            "lesson_id": row.lesson_id,
            "asset_status": asset.status if asset is not None else "uploaded",
            "format_name": row.format_name or "",
            "duration_sec": row.duration_sec,
            "width": row.width,
            "height": row.height,
            "probe_source": row.probe_source or "ffprobe",
            "sha256": row.sha256,
            "ready_for_transcode": True,
        }

    def _reopen(self, session: Session, row: MediaUploadSession, now: datetime) -> None:
        row.status = "open"
        row.updated_at = now
        session.commit()

    def _reject(
        self,
        session: Session,
        row: MediaUploadSession,
        reason: str,
        format_name: str | None,
        now: datetime,
    ) -> None:
        row.status = "rejected"
        row.error_code = reason
        row.format_name = format_name
        row.updated_at = now
        session.commit()
        self._remove_dir(row.upload_id)
        logger.warning(
            "upload rejected upload_id=%s lesson=%s reason=%s format=%s",
            row.upload_id,
            row.lesson_id,
            reason,
            format_name,
        )

    def _touch_asset(
        self,
        session: Session,
        lesson_id: int,
        info: MediaInfo,
        now: datetime,
    ) -> MediaAsset:
        # 课时时长由视频探测结果派生，管理端不再人工填写（见 core/media/quarantine.py）。
        # 魔数兜底探测拿不到时长（0s）时保留原值，避免把已有课时时长清空。
        lesson = session.get(EduCourseLesson, lesson_id)
        if lesson is not None and info.duration_sec > 0:
            lesson.duration_sec = info.duration_sec
            lesson.updated_at = now
        asset = session.execute(
            select(MediaAsset).where(MediaAsset.lesson_id == lesson_id)
        ).scalar_one_or_none()
        if asset is None:
            asset = MediaAsset(
                lesson_id=lesson_id,
                status="uploaded",
                playlist_version=1,
                duration_sec=info.duration_sec,
                error_code=None,
                created_at=now,
                updated_at=now,
            )
            session.add(asset)
        else:
            asset.status = "uploaded"
            asset.duration_sec = info.duration_sec
            asset.error_code = None
            asset.updated_at = now
        return asset

    # ── 内部：文件系统 ────────────────────────────────────────────────────

    def _persist_chunk(self, target: Path, data: bytes, digest: str) -> bool:
        try:
            _write_exclusive(target, data)
            return False
        except FileExistsError:
            if _file_md5(target) == digest:
                return True
            target.unlink(missing_ok=True)
            _write_exclusive(target, data)
            return False

    def _merge(self, directory: Path, merged: Path, total: int) -> str:
        digest = hashlib.sha256()
        fd = os.open(merged, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        try:
            os.fchmod(fd, 0o600)
            with os.fdopen(fd, "wb") as out:
                for index in range(total):
                    with (directory / f"chunk_{index:06d}.part").open("rb") as source:
                        while True:
                            block = source.read(_COPY_BLOCK)
                            if not block:
                                break
                            digest.update(block)
                            out.write(block)
        except BaseException:
            merged.unlink(missing_ok=True)
            raise
        return digest.hexdigest()

    def _check_upload_id(self, upload_id: str) -> None:
        if not isinstance(upload_id, str) or not _UPLOAD_ID.fullmatch(upload_id):
            raise AppError(NOT_FOUND, data={"upload_id": str(upload_id)})

    def _ensure_root(self) -> None:
        _ensure_dir(self._root)
        if self._root_checked or os.name != "posix":
            self._root_checked = True
            return
        if os.geteuid() == 0 and self.config.get_str("env") != "dev":
            raise AppError(INTERNAL_ERROR, data={"reason": "upload_tmp_root_owned_by_root"})
        if os.geteuid() == 0:
            logger.warning("上传 tmp 目录属主为 root：仅 dev 环境允许（§10.3.1 加固要求非 root）")
        self._root_checked = True

    def _remove_dir(self, upload_id: str) -> None:
        if not _UPLOAD_ID.fullmatch(upload_id):
            return
        shutil.rmtree(self._root / upload_id, ignore_errors=True)


__all__ = ["CHUNK_MAX_BYTES", "CHUNK_MIN_BYTES", "MERGED_NAME", "UploadService"]
