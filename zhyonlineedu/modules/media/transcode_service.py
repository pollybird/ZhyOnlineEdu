"""转码编排服务（§10.3.2.3 · M2.4 · F7）。

职责边界：
- **入队**：把 M2.3 `complete` 留下的 tmp 合并文件固化到存储区（`source.bin`，0400），
  创建 `media_transcode_job`，asset 置 `transcoding`；CVE 暂停时拒绝入队；
- **执行**：`claim_next` 用条件更新抢占任务（多 worker 安全），`run_job` 在沙箱内
  按档位逐路渲染 → 写 master.m3u8 → 生成 poster → **产物二次校验**（`core/media/hls.py`）；
- **发布**：只有校验通过才原子替换 `hls/` 并递增 `playlist_version`；
  失败则 asset 标 `failed`、版本号不变（旧产物继续可播），stderr 尾部 2KB 只落库；
- **密钥**：AES-128 key 由主密钥 + kid 派生，以 0400 临时文件注入，任务结束 `shred`。

红线：所有 ffmpeg 调用只经 `TranscodeRunner`（生产为 `SandboxRunner`），
路径必须落在存储根内，命令由枚举派生，禁止拼接任意字符串。
"""

from __future__ import annotations

import json
import logging
import os
import secrets
import shutil
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, NoReturn, cast

from sqlalchemy import delete, func, or_, select, update
from sqlalchemy.engine import CursorResult
from sqlalchemy.orm import Session

from zhyonlineedu.core.codes import (
    CONFLICT_RETRY,
    ENC_FFMPEG,
    ENC_INPUT,
    ENC_INTERRUPTED,
    ENC_KEY,
    ENC_OUTPUT,
    ENC_POSTER,
    ENC_SANDBOX,
    ENC_TIMEOUT,
    NOT_FOUND,
    TRANSCODE_PAUSED,
    VALIDATION_FAILED,
)
from zhyonlineedu.core.config import Config
from zhyonlineedu.core.db.engine import Database, ensure_database
from zhyonlineedu.core.db.models.catalog import EduCourseLesson
from zhyonlineedu.core.db.models.media import (
    MediaAsset,
    MediaTranscodeJob,
    MediaUploadSession,
)
from zhyonlineedu.core.db.query import not_deleted
from zhyonlineedu.core.errors import AppError
from zhyonlineedu.core.media.hls import (
    MASTER_NAME,
    VARIANT_NAME,
    HlsInspection,
    HlsVerifyError,
    verify_hls,
)
from zhyonlineedu.core.media.keys import (
    MediaKeyError,
    derive_aes_key,
    load_master_secret,
)
from zhyonlineedu.core.media.quarantine import probe_codec
from zhyonlineedu.core.media.runtime import (
    ALLOWED_PROFILES,
    PROFILE_HEIGHTS,
    KeyMaterial,
    RenderSpec,
    SandboxError,
    SandboxRunner,
    TranscodeRunner,
    TranscodeTimeout,
)
from zhyonlineedu.modules.media.upload_service import MERGED_NAME

logger = logging.getLogger(__name__)

SOURCE_NAME = "source.bin"
STAGING_NAME = "staging"
HLS_NAME = "hls"
POSTER_NAME = "poster.jpg"
KEY_DIR_NAME = "keys"
KEY_URI_TEMPLATE = "/api/v1/media/key/{kid}"
BANDWIDTH = {"360": 800_000, "720": 2_500_000, "1080": 5_000_000}

_ENCODED = "encoded"
_TRANSCODING = "transcoding"
_FAILED = "failed"


def _utcnow() -> datetime:
    return datetime.now(UTC)


def _iso(value: datetime | None) -> str | None:
    return value.isoformat(timespec="seconds") if value is not None else None


def _fail_validation(message: str) -> NoReturn:
    raise AppError(VALIDATION_FAILED, data={"errors": [message]})


def _ensure_private_dir(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)
    os.chmod(path, 0o700)


def _write_private(path: Path, data: bytes, mode: int = 0o600) -> None:
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, mode)
    try:
        os.fchmod(fd, mode)
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
    except BaseException:
        path.unlink(missing_ok=True)
        raise


def _shred(path: Path) -> None:
    """尽力覆写后删除（密钥材料只以临时文件存在，§10.3.2.3 第⑦层）。"""
    try:
        size = path.stat().st_size
        with path.open("r+b") as handle:
            handle.write(b"\x00" * size)
            handle.flush()
            os.fsync(handle.fileno())
    except OSError:
        pass
    finally:
        path.unlink(missing_ok=True)


def _query_int(value: object, field: str, *, default: int, minimum: int, maximum: int) -> int:
    if value is None or value == "":
        return default
    if isinstance(value, int) and not isinstance(value, bool):
        number = value
    elif isinstance(value, str) and value.isdigit():
        number = int(value)
    else:
        _fail_validation(f"{field} 必须是整数")
    if number < minimum or number > maximum:
        _fail_validation(f"{field} 必须在 {minimum}..{maximum}")
    return number


@dataclass(frozen=True, slots=True)
class _JobSnapshot:
    job_id: int
    asset_id: int
    lesson_id: int
    attempt: int
    profiles: tuple[str, ...]
    duration_sec: int


class _TranscodeFailure(Exception):
    def __init__(self, code: str, tail: str = "") -> None:
        self.code = code
        self.tail = tail
        super().__init__(code)


class TranscodeService:
    def __init__(
        self,
        config: Config,
        *,
        db: Database | None = None,
        runner: TranscodeRunner | None = None,
        codec_prober: Callable[[Path], str | None] | None = None,
        storage_root: Path | None = None,
    ) -> None:
        self.config = config
        self._db = db
        self._runner = runner
        self._codec_prober: Callable[[Path], str | None] = codec_prober or probe_codec
        self._storage = (
            Path(storage_root).resolve()
            if storage_root is not None
            else Path(config.get_str("storage.local_dir")).resolve()
        )

    # ── 基础属性 ──────────────────────────────────────────────────────────

    @property
    def db(self) -> Database:
        if self._db is None:
            self._db = ensure_database(self.config)
        return self._db

    @property
    def storage(self) -> Path:
        return self._storage

    @property
    def runner(self) -> TranscodeRunner:
        if self._runner is None:
            cfg = self.config
            self._runner = SandboxRunner(
                backend=cfg.get_str("transcode.sandbox"),
                cpu_max_percent=cfg.get_int("transcode.cpu_max_percent"),
                mem_max_mb=cfg.get_int("transcode.mem_max_mb"),
                pids_max=cfg.get_int("transcode.pids_max"),
                timeout_sec=cfg.get_int("transcode.task_timeout_sec"),
                allowed_roots=(self._storage,),
            )
        return self._runner

    def asset_dir(self, lesson_id: int) -> Path:
        return self._storage / "media" / str(lesson_id)

    def source_path(self, lesson_id: int) -> Path:
        return self.asset_dir(lesson_id) / SOURCE_NAME

    def staging_dir(self, lesson_id: int) -> Path:
        return self.asset_dir(lesson_id) / STAGING_NAME

    def hls_dir(self, lesson_id: int) -> Path:
        return self.asset_dir(lesson_id) / HLS_NAME

    # ── 入队 ──────────────────────────────────────────────────────────────

    def enqueue(
        self,
        lesson_id: int,
        profiles: Sequence[object] | None = None,
        *,
        now: datetime | None = None,
    ) -> dict[str, Any]:
        if self.config.get_bool("transcode.paused"):
            raise AppError(TRANSCODE_PAUSED, data={"reason": "cve_pause_or_manual"})
        selected = self._profiles(profiles)
        reference = now or _utcnow()

        with Session(self.db.engine) as session:
            lesson = session.get(EduCourseLesson, lesson_id)
            asset = session.execute(
                select(MediaAsset).where(MediaAsset.lesson_id == lesson_id)
            ).scalar_one_or_none()
            if lesson is None or lesson.deleted_at is not None or asset is None:
                raise AppError(NOT_FOUND, data={"lesson_id": lesson_id})

            active = (
                session.execute(
                    select(MediaTranscodeJob)
                    .where(
                        MediaTranscodeJob.lesson_id == lesson_id,
                        MediaTranscodeJob.status.in_(("queued", "running")),
                    )
                    .order_by(MediaTranscodeJob.id.desc())
                    .limit(1)
                )
                .scalars()
                .first()
            )
            if active is not None:
                return {"job": self._job_payload(active), "created": False}

            upload = (
                session.execute(
                    select(MediaUploadSession)
                    .where(
                        MediaUploadSession.lesson_id == lesson_id,
                        MediaUploadSession.status == "completed",
                    )
                    .order_by(MediaUploadSession.id.desc())
                    .limit(1)
                )
                .scalars()
                .first()
            )

            disposable = self.asset_dir(lesson_id)
            _ensure_private_dir(disposable)
            source = self.source_path(lesson_id)
            # 有更新的合并文件就优先固化（重传课程视频时不能继续编码旧源）；
            # 没有新上传（纯 retranscode）则沿用已有 source.bin。
            merged: Path | None = None
            if upload is not None:
                candidate = (
                    Path(self.config.get_str("upload.tmp_root")) / upload.upload_id / MERGED_NAME
                )
                if candidate.is_file():
                    merged = candidate
            if merged is not None:
                os.replace(merged, source)
                os.chmod(source, 0o400)
            elif not source.is_file():
                if upload is None:
                    raise AppError(NOT_FOUND, data={"lesson_id": lesson_id, "reason": "no_upload"})
                raise AppError(NOT_FOUND, data={"lesson_id": lesson_id, "reason": "temp_file_gone"})

            staging = self.staging_dir(lesson_id)
            shutil.rmtree(staging, ignore_errors=True)
            _ensure_private_dir(staging)
            _ensure_private_dir(self.storage / "media")

            job = MediaTranscodeJob(
                asset_id=asset.id,
                lesson_id=lesson_id,
                upload_id=upload.upload_id if upload is not None else None,
                status="queued",
                attempt=1,
                profiles=json.dumps(list(selected)),
                run_after=reference,
                created_at=reference,
                updated_at=reference,
            )
            session.add(job)
            asset.status = _TRANSCODING
            asset.profiles = json.dumps(list(selected))
            asset.error_code = None
            asset.updated_at = reference
            session.commit()
            session.refresh(job)
            payload = {"job": self._job_payload(job), "created": True}

        logger.info("transcode enqueued lesson=%s job=%s profiles=%s", lesson_id, job.id, selected)
        return payload

    # ── 抢占与执行 ────────────────────────────────────────────────────────

    def claim_next(self, *, now: datetime | None = None) -> MediaTranscodeJob | None:
        reference = now or _utcnow()
        with Session(self.db.engine) as session:
            candidate = (
                session.execute(
                    select(MediaTranscodeJob)
                    .where(
                        MediaTranscodeJob.status == "queued",
                        or_(
                            MediaTranscodeJob.run_after.is_(None),
                            MediaTranscodeJob.run_after <= reference,
                        ),
                    )
                    .order_by(MediaTranscodeJob.id)
                    .limit(1)
                )
                .scalars()
                .first()
            )
            if candidate is None:
                return None
            claimed = cast(
                CursorResult[Any],
                session.execute(
                    update(MediaTranscodeJob)
                    .where(
                        MediaTranscodeJob.id == candidate.id,
                        MediaTranscodeJob.status == "queued",
                    )
                    .values(status="running", started_at=reference, updated_at=reference)
                ),
            )
            session.commit()
            if claimed.rowcount != 1:
                return None
            row = session.get(MediaTranscodeJob, candidate.id)
            if row is None:
                return None
            session.expunge(row)
            return row

    def run_next(self, *, now: datetime | None = None) -> int | None:
        job = self.claim_next(now=now)
        if job is None:
            return None
        self.run_job(job.id)
        return job.id

    def run_job(self, job_id: int) -> None:
        snapshot = self._snapshot(job_id)
        if snapshot is None:
            return
        try:
            kid, inspection, sandbox = self._render(snapshot)
        except _TranscodeFailure as failure:
            self._mark_failure(snapshot, failure.code, failure.tail)
            return
        self._publish(snapshot, kid, inspection, sandbox)

    def recover_stale(self, *, now: datetime | None = None, stale_sec: int | None = None) -> int:
        """回收崩溃 worker 遗留的 running 任务（ENC-1008）。"""
        reference = now or _utcnow()
        limit = stale_sec or self.config.get_int("transcode.task_timeout_sec")
        deadline = reference - timedelta(seconds=limit)
        recovered = 0
        with Session(self.db.engine) as session:
            rows = (
                session.execute(
                    select(MediaTranscodeJob).where(
                        MediaTranscodeJob.status == "running",
                        MediaTranscodeJob.started_at.is_not(None),
                        MediaTranscodeJob.started_at < deadline,
                    )
                )
                .scalars()
                .all()
            )
            for job in rows:
                job.status = "failed"
                job.error_code = ENC_INTERRUPTED
                job.finished_at = reference
                job.updated_at = reference
                asset = session.get(MediaAsset, job.asset_id)
                if asset is not None:
                    asset.status = _FAILED
                    asset.error_code = ENC_INTERRUPTED
                    asset.updated_at = reference
                recovered += 1
            session.commit()
        if recovered:
            logger.warning("transcode recovered %s stale jobs", recovered)
        return recovered

    # ── 查询 ──────────────────────────────────────────────────────────────

    def list_assets(
        self,
        *,
        course_id: object = None,
        lesson_id: object = None,
        page: object = None,
        page_size: object = None,
    ) -> dict[str, Any]:
        size = _query_int(page_size, "page_size", default=20, minimum=1, maximum=100)
        current = _query_int(page, "page", default=1, minimum=1, maximum=100_000)
        course_filter = (
            None
            if course_id in (None, "")
            else _query_int(course_id, "course_id", default=0, minimum=1, maximum=2**31 - 1)
        )
        lesson_filter = (
            None
            if lesson_id in (None, "")
            else _query_int(lesson_id, "lesson_id", default=0, minimum=1, maximum=2**31 - 1)
        )
        with Session(self.db.engine) as session:
            base = (
                select(MediaAsset, EduCourseLesson)
                .join(EduCourseLesson, EduCourseLesson.id == MediaAsset.lesson_id)
                .where(not_deleted(EduCourseLesson))
            )
            if course_filter is not None:
                base = base.where(EduCourseLesson.course_id == course_filter)
            if lesson_filter is not None:
                base = base.where(MediaAsset.lesson_id == lesson_filter)
            total = session.execute(select(func.count()).select_from(base.subquery())).scalar_one()
            rows = session.execute(
                base.order_by(MediaAsset.lesson_id).offset((current - 1) * size).limit(size)
            ).all()
            items = [self._asset_payload(asset, int(lesson.course_id)) for asset, lesson in rows]
        return {"items": items, "total": int(total)}

    def get_asset(self, lesson_id: int) -> dict[str, Any]:
        with Session(self.db.engine) as session:
            asset = session.execute(
                select(MediaAsset).where(MediaAsset.lesson_id == lesson_id)
            ).scalar_one_or_none()
            if asset is None:
                raise AppError(NOT_FOUND, data={"lesson_id": lesson_id})
            lesson = session.get(EduCourseLesson, lesson_id)
            course_id = int(lesson.course_id) if lesson is not None else None
            job = (
                session.execute(
                    select(MediaTranscodeJob)
                    .where(MediaTranscodeJob.lesson_id == lesson_id)
                    .order_by(MediaTranscodeJob.id.desc())
                    .limit(1)
                )
                .scalars()
                .first()
            )
            payload = self._asset_payload(asset, course_id)
            payload["job"] = self._job_payload(job) if job is not None else None
        return payload

    def delete_asset(self, lesson_id: int) -> dict[str, Any]:
        with Session(self.db.engine) as session:
            asset = session.execute(
                select(MediaAsset).where(MediaAsset.lesson_id == lesson_id)
            ).scalar_one_or_none()
            if asset is None:
                raise AppError(NOT_FOUND, data={"lesson_id": lesson_id})
            active = session.execute(
                select(func.count())
                .select_from(MediaTranscodeJob)
                .where(
                    MediaTranscodeJob.lesson_id == lesson_id,
                    MediaTranscodeJob.status.in_(("queued", "running")),
                )
            ).scalar_one()
            if int(active) > 0:
                raise AppError(CONFLICT_RETRY, data={"lesson_id": lesson_id})
            session.execute(
                delete(MediaTranscodeJob).where(MediaTranscodeJob.lesson_id == lesson_id)
            )
            session.delete(asset)
            session.commit()
        shutil.rmtree(self.asset_dir(lesson_id), ignore_errors=True)
        return {"lesson_id": lesson_id, "deleted": True}

    # ── 内部：渲染与发布 ──────────────────────────────────────────────────

    def _snapshot(self, job_id: int) -> _JobSnapshot | None:
        with Session(self.db.engine) as session:
            job = session.get(MediaTranscodeJob, job_id)
            if job is None or job.status != "running":
                return None
            asset = session.get(MediaAsset, job.asset_id)
            if asset is None:
                job.status = "failed"
                job.error_code = ENC_INPUT
                job.finished_at = _utcnow()
                job.updated_at = job.finished_at
                session.commit()
                return None
            try:
                profiles = tuple(json.loads(job.profiles))
            except (TypeError, ValueError):
                profiles = ()
            if not profiles:
                job.status = "failed"
                job.error_code = ENC_INPUT
                job.finished_at = _utcnow()
                job.updated_at = job.finished_at
                session.commit()
                return None
            return _JobSnapshot(
                job_id=int(job.id),
                asset_id=int(job.asset_id),
                lesson_id=int(job.lesson_id),
                attempt=int(job.attempt),
                profiles=profiles,
                duration_sec=int(asset.duration_sec),
            )

    def _render(self, snapshot: _JobSnapshot) -> tuple[str, HlsInspection, dict[str, object]]:
        lesson_id = snapshot.lesson_id
        source = self.source_path(lesson_id)
        if not source.is_file():
            raise _TranscodeFailure(ENC_INPUT, "source missing")
        staging = self.staging_dir(lesson_id)
        shutil.rmtree(staging, ignore_errors=True)
        _ensure_private_dir(staging)

        kid = secrets.token_hex(16)
        try:
            key = self._key_material(staging, kid)
        except OSError as exc:
            raise _TranscodeFailure(ENC_KEY, str(exc)) from exc

        segment_sec = self.config.get_int("transcode.segment_sec")
        sandbox: dict[str, object] = {}
        try:
            for profile in snapshot.profiles:
                out_dir = staging / profile
                _ensure_private_dir(out_dir)
                spec = RenderSpec(
                    kind="hls",
                    source=source,
                    out_dir=out_dir,
                    segment_sec=segment_sec,
                    duration_sec=snapshot.duration_sec,
                    profile=profile,
                    key=key,
                )
                result = self.runner.run(spec)
                sandbox = dict(result.profile)
                if result.returncode != 0:
                    raise _TranscodeFailure(ENC_FFMPEG, result.stderr_tail)

            self._write_master(staging, snapshot.profiles)

            poster_dir = staging / "poster"
            _ensure_private_dir(poster_dir)
            poster = self.runner.run(
                RenderSpec(
                    kind="poster",
                    source=source,
                    out_dir=poster_dir,
                    segment_sec=segment_sec,
                    duration_sec=snapshot.duration_sec,
                    profile=max(snapshot.profiles, key=lambda item: PROFILE_HEIGHTS[item]),
                    poster_sec=self.config.get_int("transcode.poster_sec"),
                )
            )
            sandbox = dict(poster.profile) or sandbox
            if poster.returncode != 0 or not (poster_dir / POSTER_NAME).is_file():
                raise _TranscodeFailure(ENC_POSTER, poster.stderr_tail)

            inspection = verify_hls(
                staging,
                profiles=snapshot.profiles,
                expected_duration_sec=snapshot.duration_sec,
                codec_prober=self._codec_prober,
            )
        except TranscodeTimeout as exc:
            raise _TranscodeFailure(ENC_TIMEOUT, str(exc)) from exc
        except SandboxError as exc:
            raise _TranscodeFailure(ENC_SANDBOX, exc.reason) from exc
        except HlsVerifyError as exc:
            raise _TranscodeFailure(ENC_OUTPUT, exc.reason) from exc
        finally:
            _shred(key.key_path)
            _shred(key.info_path)

        return kid, inspection, sandbox

    def _publish(
        self,
        snapshot: _JobSnapshot,
        kid: str,
        inspection: HlsInspection,
        sandbox: Mapping[str, object],
    ) -> None:
        lesson_id = snapshot.lesson_id
        staging = self.staging_dir(lesson_id)
        final = self.hls_dir(lesson_id)
        retired = self.asset_dir(lesson_id) / "hls.retired"
        shutil.rmtree(retired, ignore_errors=True)
        if final.exists():
            os.replace(final, retired)
        os.replace(staging, final)
        shutil.rmtree(retired, ignore_errors=True)

        now = _utcnow()
        with Session(self.db.engine) as session:
            job = session.get(MediaTranscodeJob, snapshot.job_id)
            asset = session.get(MediaAsset, snapshot.asset_id)
            if job is None or asset is None:
                return
            if job.status != "running":
                logger.warning("transcode job %s no longer running; skip publish", snapshot.job_id)
                return
            asset.status = _ENCODED
            asset.playlist_version = int(asset.playlist_version or 1) + 1
            asset.kid = kid
            asset.profiles = json.dumps(list(snapshot.profiles))
            asset.poster_path = f"{HLS_NAME}/poster/{POSTER_NAME}"
            asset.sandbox_profile = json.dumps(dict(sandbox), ensure_ascii=False)
            asset.error_code = None
            asset.updated_at = now
            job.status = "succeeded"
            job.error_code = None
            job.sandbox = json.dumps(dict(sandbox), ensure_ascii=False)
            job.finished_at = now
            job.updated_at = now
            session.commit()
        shutil.rmtree(staging, ignore_errors=True)
        logger.info(
            "transcode succeeded lesson=%s job=%s kid=%s segments=%s duration=%.2fs",
            lesson_id,
            snapshot.job_id,
            kid,
            inspection.segment_count,
            inspection.duration_sec,
        )

    def _mark_failure(self, snapshot: _JobSnapshot, code: str, tail: str) -> None:
        now = _utcnow()
        max_attempts = self.config.get_int("transcode.max_attempts")
        retry = snapshot.attempt < max_attempts
        with Session(self.db.engine) as session:
            job = session.get(MediaTranscodeJob, snapshot.job_id)
            asset = session.get(MediaAsset, snapshot.asset_id)
            if job is not None:
                job.error_code = code
                job.err = tail[-2048:] if tail else None
                job.updated_at = now
                if retry:
                    job.status = "queued"
                    job.attempt = snapshot.attempt + 1
                    job.run_after = now + timedelta(
                        seconds=self.config.get_int("transcode.retry_backoff_sec")
                    )
                    job.started_at = None
                    job.finished_at = None
                else:
                    job.status = "failed"
                    job.finished_at = now
            if asset is not None:
                asset.status = _TRANSCODING if retry else _FAILED
                asset.error_code = code
                asset.updated_at = now
            session.commit()
        shutil.rmtree(self.staging_dir(snapshot.lesson_id), ignore_errors=True)
        logger.warning(
            "transcode failed lesson=%s job=%s code=%s retry=%s tail=%r",
            snapshot.lesson_id,
            snapshot.job_id,
            code,
            retry,
            tail[-300:],
        )

    # ── 内部：产物与密钥 ──────────────────────────────────────────────────

    def _write_master(self, staging: Path, profiles: Sequence[str]) -> None:
        lines = ["#EXTM3U", "#EXT-X-VERSION:3"]
        for profile in profiles:
            height = PROFILE_HEIGHTS[profile]
            width = height * 16 // 9
            bandwidth = BANDWIDTH[profile]
            lines.append(f"#EXT-X-STREAM-INF:BANDWIDTH={bandwidth},RESOLUTION={width}x{height}")
            lines.append(f"{profile}/{VARIANT_NAME}")
        _write_private(staging / MASTER_NAME, ("\n".join(lines) + "\n").encode("utf-8"))

    def _key_material(self, staging: Path, kid: str) -> KeyMaterial:
        key_dir = staging / KEY_DIR_NAME
        _ensure_private_dir(key_dir)
        key_path = key_dir / f"{kid}.key"
        _write_private(key_path, self._derive_key(kid), mode=0o400)
        info_path = key_dir / f"{kid}.info"
        uri = KEY_URI_TEMPLATE.format(kid=kid)
        _write_private(info_path, f"{uri}\n{key_path}\n".encode(), mode=0o400)
        return KeyMaterial(kid=kid, key_path=key_path, info_path=info_path, uri=uri)

    def _derive_key(self, kid: str) -> bytes:
        """主密钥 + kid 派生 16 字节 AES key；主密钥缺失时（仅 dev）自动生成 0400 文件。"""
        try:
            secret = load_master_secret(self.config, self._storage)
        except MediaKeyError as exc:
            raise OSError(f"media.aes_key_hex 不可用：{exc.reason}") from exc
        return derive_aes_key(secret, kid)

    def _profiles(self, requested: Sequence[object] | None) -> list[str]:
        raw = (
            list(requested)
            if requested is not None
            else list(self.config.get_list("transcode.profiles"))
        )
        if not raw:
            _fail_validation("profiles 不能为空")
        normalized: list[str] = []
        for item in raw:
            if not isinstance(item, str) or item not in ALLOWED_PROFILES:
                _fail_validation(f"profiles 只能取 {list(ALLOWED_PROFILES)}")
            if item not in normalized:
                normalized.append(item)
        normalized.sort(key=lambda value: PROFILE_HEIGHTS[value])
        return normalized

    # ── 内部：序列化 ──────────────────────────────────────────────────────

    def _profiles_of(self, text: str | None) -> list[str]:
        if not text:
            return []
        try:
            values = json.loads(text)
        except ValueError:
            return []
        return [str(item) for item in values] if isinstance(values, list) else []

    def _sandbox_of(self, text: str | None) -> dict[str, Any] | None:
        if not text:
            return None
        try:
            values = json.loads(text)
        except ValueError:
            return None
        return values if isinstance(values, dict) else None

    def _asset_payload(self, asset: MediaAsset, course_id: int | None) -> dict[str, Any]:
        return {
            "lesson_id": int(asset.lesson_id),
            "course_id": course_id,
            "status": asset.status,
            "playlist_version": int(asset.playlist_version),
            "duration_sec": int(asset.duration_sec),
            "kid": asset.kid,
            "profiles": self._profiles_of(asset.profiles),
            "poster_path": asset.poster_path,
            "error_code": asset.error_code,
            "sandbox": self._sandbox_of(asset.sandbox_profile),
            "updated_at": _iso(asset.updated_at),
        }

    def _job_payload(self, job: MediaTranscodeJob) -> dict[str, Any]:
        return {
            "id": int(job.id),
            "lesson_id": int(job.lesson_id),
            "status": job.status,
            "attempt": int(job.attempt),
            "profiles": self._profiles_of(job.profiles),
            "error_code": job.error_code,
            "created_at": _iso(job.created_at),
            "started_at": _iso(job.started_at),
            "finished_at": _iso(job.finished_at),
        }


__all__ = [
    "HLS_NAME",
    "POSTER_NAME",
    "SOURCE_NAME",
    "STAGING_NAME",
    "TranscodeService",
]
