"""M2.4 转码沙箱化（§10.3.2.3 · F7）。

验收口径：**SANDBOX-006 通过、无 ticket 拿不到 key**（key 端点属 M2.5）。
本文件覆盖：
- 产物二次校验：缺分片/外链/密钥 URI/时长/编码白名单（纯结构，不依赖 ffmpeg）；
- 沙箱路径红线：源/输出/密钥必须落在存储根内；
- 入队：暂停拒入队、tmp 合并文件固化（0400）、幂等、档位校验；
- 执行：假 runner 成功发布（asset=encoded、playlist_version+1、kid、sandbox 画像）；
- 失败：ENC-1002 重试 attempt+1、stderr 只落库尾部 2KB、最终 failed 不动旧版本；
- 崩溃回收：running 超时 → ENC-1008；
- API：列表/详情/重转码/删除 + RBAC + 模型漂移；
- 真实 ffmpeg 集成（`sandbox=disabled`，无 ffmpeg 自动跳过）。
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess  # noqa: S404 - 仅测试内生成样例素材
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.orm import Session
from zhyonlineedu.api.schemas import (
    AssetDeleteOut,
    AssetDetailOut,
    AssetListOut,
    AssetOut,
    RetranscodeOut,
    TranscodeJobOut,
)
from zhyonlineedu.app import create_app
from zhyonlineedu.core.auth.keys import clear_key_cache, ensure_keypair
from zhyonlineedu.core.auth.passwords import hash_password
from zhyonlineedu.core.capability import ProbeResult, ProbeState, StaticProbe
from zhyonlineedu.core.codes import Code
from zhyonlineedu.core.config import Config, load_config
from zhyonlineedu.core.db import compat
from zhyonlineedu.core.db.engine import create_db_engine
from zhyonlineedu.core.db.migration import upgrade_head
from zhyonlineedu.core.db.models.account import AccountUser, SysUserRole
from zhyonlineedu.core.db.models.catalog import (
    EduCourse,
    EduCourseChapter,
    EduCourseLesson,
    EduSubject,
)
from zhyonlineedu.core.db.models.media import (
    MediaAsset,
    MediaTranscodeJob,
    MediaUploadSession,
    utcnow,
)
from zhyonlineedu.core.media.hls import (
    HlsVerifyError,
    parse_master,
    verify_hls,
)
from zhyonlineedu.core.media.runtime import (
    KeyMaterial,
    RenderSpec,
    RunResult,
    SandboxError,
    SandboxRunner,
    build_hls_command,
    build_poster_command,
    tail,
)
from zhyonlineedu.modules.media.transcode_service import TranscodeService
from zhyonlineedu.workers.transcode import run_worker

PASSWORD = "S3cret-pass"  # noqa: S105 - 测试用固定口令
READY = ProbeResult(ProbeState.READY, flavor="sqlite", capabilities={"returning": True})
LESSON_ID = 1
UPLOAD_ID = "a" * 32
KID = "b" * 32
DURATION = 8


@pytest.fixture(autouse=True)
def _cleanup() -> Iterator[None]:
    compat.reset()
    clear_key_cache()
    yield
    compat.reset()
    clear_key_cache()


def _config(tmp_path: Path, **overrides: str) -> Config:
    environ = {
        "ZHY_ENV": "dev",
        "ZHY_DB_URL": f"sqlite:///{tmp_path / 'transcode.db'}",
        "ZHY_DB_FLAVOR": "sqlite",
        "ZHY_JWT_PRIVATE_KEY_PATH": str(tmp_path / "keys" / "jwt_private.pem"),
        "ZHY_JWT_PUBLIC_KEY_PATH": str(tmp_path / "keys" / "jwt_public.pem"),
        "ZHY_UPLOAD_TMP_ROOT": str(tmp_path / "uploads"),
        "ZHY_STORAGE_LOCAL_DIR": str(tmp_path / "storage"),
        "ZHY_TRANSCODE_RETRY_BACKOFF_SEC": "1",
    }
    environ.update(overrides)
    return load_config(environ=environ, sys_config={})


def _seed(tmp_path: Path, **overrides: str) -> Config:
    config = _config(tmp_path, **overrides)
    ensure_keypair(config.get_str("jwt.private_key_path"), config.get_str("jwt.public_key_path"))
    url = config.get_str("db.url")
    upgrade_head(url, "sqlite")
    engine = create_db_engine(url, "sqlite")
    try:
        with Session(engine) as session:
            now = utcnow()
            for username, role in (("root", "super_admin"), ("student", "student")):
                user = AccountUser(
                    username=username,
                    password_hash=hash_password(PASSWORD),
                    status="active",
                    token_version=1,
                    created_at=now,
                    updated_at=now,
                )
                session.add(user)
                session.flush()
                session.add(SysUserRole(user_id=user.id, role_code=role))
            subject = EduSubject(name="数学", sort_order=1024, is_enabled=True)
            session.add(subject)
            session.flush()
            course = EduCourse(
                subject_id=subject.id,
                title="高等数学",
                mode="both",
                access_type="public",
                status="draft",
                sort_order=1024,
            )
            session.add(course)
            session.flush()
            chapter = EduCourseChapter(course_id=course.id, title="第一章", sort_order=1024)
            session.add(chapter)
            session.flush()
            session.add(
                EduCourseLesson(
                    id=LESSON_ID,
                    chapter_id=chapter.id,
                    course_id=course.id,
                    title="第一课",
                    kind="video",
                    duration_sec=DURATION,
                    is_free=False,
                    sort_order=1024,
                )
            )
            session.add(
                MediaAsset(
                    lesson_id=LESSON_ID,
                    status="uploaded",
                    playlist_version=1,
                    duration_sec=DURATION,
                    created_at=now,
                    updated_at=now,
                )
            )
            session.add(
                MediaUploadSession(
                    upload_id=UPLOAD_ID,
                    lesson_id=LESSON_ID,
                    file_name="lesson.mp4",
                    file_size=4096,
                    sha256="c" * 64,
                    chunk_size=1024,
                    total_chunks=4,
                    status="completed",
                    duration_sec=DURATION,
                    expires_at=now + timedelta(hours=1),
                    completed_at=now,
                    created_at=now,
                    updated_at=now,
                )
            )
            session.commit()
    finally:
        engine.dispose()

    merged = tmp_path / "uploads" / UPLOAD_ID / "merged.bin"
    merged.parent.mkdir(parents=True, exist_ok=True)
    merged.write_bytes(b"fake-source-bytes")
    return config


class FakeRunner:
    """假 runner：按 spec 写出合规 HLS/poster；可指定档位失败。"""

    def __init__(self, *, fail_profiles: set[str] | None = None, stderr: str = "boom") -> None:
        self.calls: list[RenderSpec] = []
        self.fail_profiles = fail_profiles or set()
        self.stderr = stderr

    def _profile(self) -> dict[str, object]:
        return {"backend": "fake", "network": "none", "uid": 65534, "ffmpeg_version": "fake-1"}

    def run(self, spec: RenderSpec) -> RunResult:
        self.calls.append(spec)
        if spec.kind == "poster":
            (spec.out_dir / "poster.jpg").write_bytes(b"\xff\xd8poster")
            return RunResult(0, "", 0.01, self._profile())
        if spec.profile in self.fail_profiles:
            return RunResult(1, self.stderr, 0.01, self._profile())
        assert spec.key is not None
        segment = spec.out_dir / "seg_000.ts"
        segment.write_bytes(b"tsdata-" + spec.profile.encode())
        playlist = "\n".join(
            [
                "#EXTM3U",
                "#EXT-X-VERSION:3",
                "#EXT-X-TARGETDURATION:4",
                f'#EXT-X-KEY:METHOD=AES-128,URI="{spec.key.uri}"',
                f"#EXTINF:{spec.duration_sec / 2:.2f},",
                "seg_000.ts",
                f"#EXTINF:{spec.duration_sec / 2:.2f},",
                "seg_000.ts",
                "#EXT-X-ENDLIST",
                "",
            ]
        )
        (spec.out_dir / "index.m3u8").write_text(playlist, encoding="utf-8")
        return RunResult(0, "", 0.01, self._profile())


def _service(config: Config, runner: Any = None) -> TranscodeService:
    return TranscodeService(
        config,
        runner=runner or FakeRunner(),
        codec_prober=lambda _path: None,
    )


def _client(config: Config, service: TranscodeService) -> Any:
    app = create_app(config=config, probe=StaticProbe(READY))
    app.extensions["zhy_transcode"] = service
    return app.test_client()


def _login(client: Any, username: str = "root") -> dict[str, Any]:
    response = client.post("/api/v1/auth/login", json={"username": username, "password": PASSWORD})
    assert response.status_code == 200, response.get_json()
    return dict(response.get_json()["data"])


def _headers(client: Any, username: str = "root") -> dict[str, str]:
    return {"Authorization": f"Bearer {_login(client, username)['access_token']}"}


def _data(response: Any) -> dict[str, Any]:
    body = response.get_json()
    assert response.status_code == 200, body
    assert body["code"] == "OK", body
    return dict(body["data"])


def _error(response: Any, status: int, code: Code | str) -> dict[str, Any]:
    body = response.get_json()
    expected = int(code) if isinstance(code, Code) else code
    assert response.status_code == status, body
    assert body["code"] == expected, body
    return dict(body["data"] or {})


def _assert_matches(model: type[BaseModel], payload: dict[str, Any]) -> None:
    assert set(payload) == set(model.model_fields), (
        f"{model.__name__} 与实现漂移："
        f"schema={sorted(model.model_fields)} 实现={sorted(payload)}"
    )


def _rows(tmp_path: Path, model: Any) -> list[Any]:
    engine = create_db_engine(_config(tmp_path).get_str("db.url"), "sqlite")
    try:
        with Session(engine) as session:
            rows = list(session.execute(select(model)).scalars().all())
            for row in rows:
                session.expunge(row)
            return rows
    finally:
        engine.dispose()


# ── 产物二次校验（§10.3.2.3 第⑥层）──────────────────────────────────────


def _write_hls(
    root: Path,
    *,
    profiles: tuple[str, ...] = ("360", "720"),
    kid: str = KID,
    segment: bool = True,
    master_uri: str | None = None,
    key_uri: str | None = None,
) -> None:
    root.mkdir(parents=True, exist_ok=True)
    lines = ["#EXTM3U", "#EXT-X-VERSION:3"]
    for profile in profiles:
        lines += [
            "#EXT-X-STREAM-INF:BANDWIDTH=800000,RESOLUTION=640x360",
            master_uri or f"{profile}/index.m3u8",
        ]
        variant_dir = root / profile
        variant_dir.mkdir(parents=True, exist_ok=True)
        if segment:
            (variant_dir / "seg_000.ts").write_bytes(b"0123456789")
        (variant_dir / "index.m3u8").write_text(
            "\n".join(
                [
                    "#EXTM3U",
                    "#EXT-X-VERSION:3",
                    f'#EXT-X-KEY:METHOD=AES-128,URI="{key_uri or f"/api/v1/media/key/{kid}"}"',
                    "#EXTINF:4.00,",
                    "seg_000.ts",
                    "#EXTINF:4.00,",
                    "seg_000.ts",
                    "#EXT-X-ENDLIST",
                    "",
                ]
            ),
            encoding="utf-8",
        )
    (root / "master.m3u8").write_text("\n".join(lines) + "\n", encoding="utf-8")


def test_verify_hls_accepts_valid_output(tmp_path: Path) -> None:
    _write_hls(tmp_path)
    inspection = verify_hls(
        tmp_path, profiles=("360", "720"), expected_duration_sec=8, codec_prober=lambda _p: "h264"
    )
    assert inspection.segment_count == 4
    assert inspection.duration_sec == pytest.approx(8.0)
    assert set(parse_master(tmp_path)) == {"360", "720"}


def test_verify_hls_rejects_missing_segment(tmp_path: Path) -> None:
    _write_hls(tmp_path, profiles=("360",))
    (tmp_path / "360" / "seg_000.ts").unlink()
    with pytest.raises(HlsVerifyError) as exc:
        verify_hls(tmp_path, profiles=("360",))
    assert exc.value.reason == "segment_missing"


def test_verify_hls_rejects_external_reference(tmp_path: Path) -> None:
    _write_hls(tmp_path, profiles=("360",), master_uri="https://evil.example/x.m3u8")
    with pytest.raises(HlsVerifyError) as exc:
        verify_hls(tmp_path, profiles=("360",))
    assert exc.value.reason == "external_reference"


def test_verify_hls_rejects_bad_key_uri_and_codec(tmp_path: Path) -> None:
    _write_hls(tmp_path, profiles=("360",), key_uri="https://evil.example/key")
    with pytest.raises(HlsVerifyError) as exc:
        verify_hls(tmp_path, profiles=("360",))
    assert exc.value.reason == "key_uri_invalid"

    shutil.rmtree(tmp_path)
    _write_hls(tmp_path, profiles=("360",))
    with pytest.raises(HlsVerifyError) as exc:
        verify_hls(tmp_path, profiles=("360",), codec_prober=lambda _p: "mpeg2video")
    assert exc.value.reason == "codec_not_allowed"


def test_verify_hls_enforces_duration_and_profile(tmp_path: Path) -> None:
    _write_hls(tmp_path, profiles=("360",))
    with pytest.raises(HlsVerifyError) as exc:
        verify_hls(tmp_path, profiles=("360",), expected_duration_sec=30)
    assert exc.value.reason == "duration_mismatch"

    with pytest.raises(HlsVerifyError) as exc:
        verify_hls(tmp_path, profiles=("360", "720"))
    assert exc.value.reason == "profile_mismatch"


# ── 沙箱命令与路径红线 ────────────────────────────────────────────────────


def test_commands_are_enum_derived(tmp_path: Path) -> None:
    key = KeyMaterial(
        kid=KID,
        key_path=tmp_path / "k.key",
        info_path=tmp_path / "k.info",
        uri=f"/api/v1/media/key/{KID}",
    )
    spec = RenderSpec(
        kind="hls",
        source=tmp_path / "source.bin",
        out_dir=tmp_path / "out",
        segment_sec=4,
        duration_sec=8,
        profile="720",
        key=key,
    )
    argv = build_hls_command(spec)
    assert argv[0] == "ffmpeg"
    assert "-hls_segment_filename" in argv
    assert argv[argv.index("-hls_segment_filename") + 1] == "seg_%03d.ts"
    assert argv[-1] == "index.m3u8"
    assert str(key.info_path) in argv

    poster = build_poster_command(spec)
    assert poster[-1] == "poster.jpg"
    assert "-ss" in poster


def test_sandbox_rejects_paths_outside_root(tmp_path: Path) -> None:
    storage = tmp_path / "storage"
    storage.mkdir()
    source = storage / "source.bin"
    source.write_bytes(b"x")
    runner = SandboxRunner(backend="disabled", allowed_roots=(storage,), timeout_sec=30)
    outside = RenderSpec(
        kind="poster",
        source=source,
        out_dir=tmp_path / "elsewhere",
        segment_sec=4,
        duration_sec=8,
    )
    with pytest.raises(SandboxError) as exc:
        runner.run(outside)
    assert exc.value.reason == "output_outside_allowed_root"


def test_tail_keeps_last_two_kb() -> None:
    text = "开头" + "x" * 5000
    result = tail(text)
    assert len(result.encode("utf-8")) <= 2048
    assert result.endswith("x")


# ── 入队 ──────────────────────────────────────────────────────────────────


def test_enqueue_moves_tmp_source_and_marks_transcoding(tmp_path: Path) -> None:
    config = _seed(tmp_path)
    service = _service(config)
    payload = service.enqueue(LESSON_ID)

    assert payload["created"] is True
    assert payload["job"]["status"] == "queued"
    assert payload["job"]["profiles"] == ["360", "720"]

    source = service.source_path(LESSON_ID)
    assert source.is_file()
    assert source.stat().st_mode & 0o777 == 0o400
    assert not (tmp_path / "uploads" / UPLOAD_ID / "merged.bin").exists()
    assert _rows(tmp_path, MediaAsset)[0].status == "transcoding"


def test_enqueue_is_idempotent_and_validates_profiles(tmp_path: Path) -> None:
    config = _seed(tmp_path)
    service = _service(config)
    first = service.enqueue(LESSON_ID, ["720"])
    second = service.enqueue(LESSON_ID, ["1080"])
    assert second["created"] is False
    assert second["job"]["id"] == first["job"]["id"]
    assert second["job"]["profiles"] == ["720"]

    with pytest.raises(Exception) as exc:
        service.enqueue(LESSON_ID, ["480p"])
    assert getattr(exc.value, "code", "") == "VALIDATION_FAILED"


def test_enqueue_prefers_new_upload_over_existing_source(tmp_path: Path) -> None:
    """重传后重新入队必须编码新源，而不是沿用旧的 source.bin。"""
    config = _seed(tmp_path)
    service = _service(config)
    service.enqueue(LESSON_ID)
    service.run_next()
    assert service.source_path(LESSON_ID).read_bytes() == b"fake-source-bytes"

    new_id = "d" * 32
    engine = create_db_engine(config.get_str("db.url"), "sqlite")
    try:
        with Session(engine) as session:
            now = utcnow()
            session.add(
                MediaUploadSession(
                    upload_id=new_id,
                    lesson_id=LESSON_ID,
                    file_name="lesson-v2.mp4",
                    file_size=1024,
                    sha256="e" * 64,
                    chunk_size=1024,
                    total_chunks=1,
                    status="completed",
                    duration_sec=DURATION,
                    expires_at=now + timedelta(hours=1),
                    completed_at=now,
                    created_at=now,
                    updated_at=now,
                )
            )
            session.commit()
    finally:
        engine.dispose()
    merged = tmp_path / "uploads" / new_id / "merged.bin"
    merged.parent.mkdir(parents=True, exist_ok=True)
    merged.write_bytes(b"new-source-bytes")

    assert service.enqueue(LESSON_ID)["created"] is True
    assert service.source_path(LESSON_ID).read_bytes() == b"new-source-bytes"
    assert not merged.exists()


def test_enqueue_refused_when_paused(tmp_path: Path) -> None:
    config = _seed(tmp_path, ZHY_TRANSCODE_PAUSED="true")
    client = _client(config, _service(config))
    response = client.post(
        f"/api/v1/media/assets/{LESSON_ID}/retranscode", json={}, headers=_headers(client)
    )
    data = _error(response, 409, "TRANSCODE_PAUSED")
    assert data["reason"] == "cve_pause_or_manual"


# ── 执行成功 / 失败 ───────────────────────────────────────────────────────


def test_run_job_publishes_asset(tmp_path: Path) -> None:
    config = _seed(tmp_path)
    runner = FakeRunner()
    service = _service(config, runner)
    service.enqueue(LESSON_ID, ["360", "720"])
    assert service.run_next() is not None

    asset = _rows(tmp_path, MediaAsset)[0]
    job = _rows(tmp_path, MediaTranscodeJob)[0]
    assert asset.status == "encoded"
    assert asset.playlist_version == 2
    assert asset.kid is not None and len(asset.kid) == 32
    assert asset.poster_path == "hls/poster/poster.jpg"
    assert json.loads(str(asset.profiles)) == ["360", "720"]
    assert json.loads(str(asset.sandbox_profile))["backend"] == "fake"
    assert job.status == "succeeded"
    assert job.error_code is None

    hls = service.hls_dir(LESSON_ID)
    assert (hls / "master.m3u8").is_file()
    assert (hls / "poster" / "poster.jpg").is_file()
    # 密钥材料只以 0400 临时文件存在，发布后不得残留
    assert list(hls.rglob("*.key")) == []
    assert list(hls.rglob("*.info")) == []


def test_run_job_retries_then_fails_without_bumping_version(tmp_path: Path) -> None:
    config = _seed(tmp_path)
    runner = FakeRunner(fail_profiles={"360"}, stderr="e" * 5000)
    service = _service(config, runner)
    service.enqueue(LESSON_ID, ["360"])
    service.run_next()

    job = _rows(tmp_path, MediaTranscodeJob)[0]
    assert job.status == "queued"
    assert job.attempt == 2
    assert job.error_code == "ENC-1002"
    assert job.err is not None and len(job.err) <= 2048
    assert _rows(tmp_path, MediaAsset)[0].status == "transcoding"

    later = datetime.now(UTC) + timedelta(seconds=30)
    service.run_next(now=later)

    job = _rows(tmp_path, MediaTranscodeJob)[0]
    assert job.status == "failed"
    assert job.error_code == "ENC-1002"
    asset = _rows(tmp_path, MediaAsset)[0]
    assert asset.status == "failed"
    assert asset.playlist_version == 1


def test_recover_stale_marks_interrupted(tmp_path: Path) -> None:
    config = _seed(tmp_path)
    service = _service(config)
    service.enqueue(LESSON_ID)
    stale = service.claim_next()
    assert stale is not None
    engine = create_db_engine(config.get_str("db.url"), "sqlite")
    try:
        with Session(engine) as session:
            row = session.get(MediaTranscodeJob, stale.id)
            assert row is not None
            row.started_at = datetime.now(UTC) - timedelta(hours=3)
            session.commit()
    finally:
        engine.dispose()

    assert service.recover_stale() == 1
    job = _rows(tmp_path, MediaTranscodeJob)[0]
    assert job.status == "failed"
    assert job.error_code == "ENC-1008"
    assert _rows(tmp_path, MediaAsset)[0].status == "failed"


def test_worker_once_drains_queue(tmp_path: Path) -> None:
    config = _seed(tmp_path)
    service = _service(config)
    service.enqueue(LESSON_ID)
    assert run_worker(service, once=True, poll_sec=0) == 1
    assert _rows(tmp_path, MediaTranscodeJob)[0].status == "succeeded"


# ── API / RBAC / 模型漂移 ─────────────────────────────────────────────────


def test_asset_api_list_detail_and_retranscode(tmp_path: Path) -> None:
    config = _seed(tmp_path)
    service = _service(config)
    client = _client(config, service)
    headers = _headers(client)

    listed = _data(client.get("/api/v1/media/assets", headers=headers))
    _assert_matches(AssetListOut, listed)
    assert listed["total"] == 1
    _assert_matches(AssetOut, listed["items"][0])
    assert listed["items"][0]["status"] == "uploaded"

    detail = _data(client.get(f"/api/v1/media/assets/{LESSON_ID}", headers=headers))
    _assert_matches(AssetDetailOut, detail)
    assert detail["job"] is None

    created = _data(
        client.post(
            f"/api/v1/media/assets/{LESSON_ID}/retranscode",
            json={"profiles": ["1080"]},
            headers=headers,
        )
    )
    _assert_matches(RetranscodeOut, created)
    assert created["created"] is True
    _assert_matches(TranscodeJobOut, created["job"])

    again = _data(
        client.post(
            f"/api/v1/media/assets/{LESSON_ID}/retranscode",
            json={"profiles": ["1080"]},
            headers=headers,
        )
    )
    assert again["created"] is False

    after = _data(client.get(f"/api/v1/media/assets/{LESSON_ID}", headers=headers))
    assert after["job"]["profiles"] == ["1080"]
    assert after["status"] == "transcoding"

    bad = client.post(
        f"/api/v1/media/assets/{LESSON_ID}/retranscode",
        json={"profiles": ["480p"]},
        headers=headers,
    )
    _error(bad, 422, "VALIDATION_FAILED")


def test_delete_asset_conflicts_until_queue_idle(tmp_path: Path) -> None:
    config = _seed(tmp_path)
    service = _service(config)
    client = _client(config, service)
    headers = _headers(client)

    _data(client.post(f"/api/v1/media/assets/{LESSON_ID}/retranscode", json={}, headers=headers))
    _error(
        client.delete(f"/api/v1/media/assets/{LESSON_ID}", headers=headers),
        409,
        "CONFLICT_RETRY",
    )

    engine = create_db_engine(config.get_str("db.url"), "sqlite")
    try:
        with Session(engine) as session:
            row = session.execute(select(MediaTranscodeJob)).scalar_one()
            row.status = "failed"
            session.commit()
    finally:
        engine.dispose()

    deleted = _data(client.delete(f"/api/v1/media/assets/{LESSON_ID}", headers=headers))
    _assert_matches(AssetDeleteOut, deleted)
    assert deleted["deleted"] is True
    assert not service.asset_dir(LESSON_ID).exists()
    assert _rows(tmp_path, MediaAsset) == []


def test_unknown_asset_is_404(tmp_path: Path) -> None:
    config = _seed(tmp_path)
    client = _client(config, _service(config))
    headers = _headers(client)
    _error(client.get("/api/v1/media/assets/999", headers=headers), 404, "NOT_FOUND")
    _error(client.delete("/api/v1/media/assets/999", headers=headers), 404, "NOT_FOUND")


def test_student_cannot_manage_assets(tmp_path: Path) -> None:
    config = _seed(tmp_path)
    client = _client(config, _service(config))
    headers = _headers(client, "student")
    _error(client.get("/api/v1/media/assets", headers=headers), 403, "PERMISSION_DENIED")
    _error(
        client.post(f"/api/v1/media/assets/{LESSON_ID}/retranscode", json={}, headers=headers),
        403,
        "PERMISSION_DENIED",
    )


# ── 真实 ffmpeg 集成（无 ffmpeg 自动跳过）─────────────────────────────────


@pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="未安装 ffmpeg，跳过沙箱实跑")
def test_real_ffmpeg_transcode_disabled_backend(tmp_path: Path) -> None:
    config = _seed(tmp_path, ZHY_TRANSCODE_SANDBOX="disabled", ZHY_TRANSCODE_POSTER_SEC="0")
    service = _service(config)
    service._runner = SandboxRunner(  # noqa: SLF001 - 集成测试显式切到 disabled 后端
        backend="disabled", allowed_roots=(service.storage,), timeout_sec=120
    )
    service._codec_prober = lambda _path: None  # noqa: SLF001 - 假分片无需编码探测

    source = service.source_path(LESSON_ID)
    source.parent.mkdir(parents=True, exist_ok=True)
    ffmpeg = shutil.which("ffmpeg")
    assert ffmpeg is not None
    subprocess.run(  # noqa: S603 - 仅测试内生成 2 秒合成素材
        [
            ffmpeg,
            "-hide_banner",
            "-loglevel",
            "error",
            "-f",
            "lavfi",
            "-i",
            "testsrc=size=320x240:rate=15:duration=2",
            "-pix_fmt",
            "yuv420p",
            "-f",
            "mp4",
            "-y",
            str(source),
        ],
        check=True,
        capture_output=True,
        timeout=120,
    )
    os.chmod(source, 0o400)
    # 去掉 M2.3 夹具里的假合并文件：本用例直接以真实合成视频作为 source.bin
    (tmp_path / "uploads" / UPLOAD_ID / "merged.bin").unlink()
    engine = create_db_engine(config.get_str("db.url"), "sqlite")
    try:
        with Session(engine) as session:
            asset = session.execute(select(MediaAsset)).scalar_one()
            asset.duration_sec = 2
            session.commit()
    finally:
        engine.dispose()

    service.enqueue(LESSON_ID, ["360"])
    service.run_next()

    asset = _rows(tmp_path, MediaAsset)[0]
    assert asset.status == "encoded", _rows(tmp_path, MediaTranscodeJob)[0].err
    assert asset.playlist_version == 2
    assert (service.hls_dir(LESSON_ID) / "master.m3u8").is_file()
    assert list(service.hls_dir(LESSON_ID).rglob("*.key")) == []
