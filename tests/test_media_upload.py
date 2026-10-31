"""M2.3 分片上传与输入检疫（§10.3.1 / §10.3.2.3 第①层 · F7）。

验收口径：**异常清单 + MALICIOUS 用例**。覆盖：
- tmp 安全：目录 0700、分片/合并文件 0600、`O_CREAT|O_EXCL`、非法 upload_id/file_name；
- 异常清单：`50101` 分片 md5 不符、`50102` 缺片（附 missing）、`50103` 整文件 sha256 不符；
- 魔数检疫：ELF/PE 等危险容器直接拒、时长/尺寸越界拒、ffprobe 缺失时 fail-closed；
- 正常链路：init → chunk → complete → `media_asset=uploaded`，断点状态与幂等重传；
- tmp-reaper：过期会话与临时目录清理；
- RBAC：学员角色 403；响应模型漂移比对。
"""

from __future__ import annotations

import hashlib
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.orm import Session
from zhyonlineedu.api.schemas import (
    UploadChunkOut,
    UploadCompleteOut,
    UploadDeleteOut,
    UploadInitOut,
    UploadStatusOut,
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
from zhyonlineedu.core.db.models.media import MediaAsset, MediaUploadSession, utcnow
from zhyonlineedu.core.media.quarantine import MediaInfo, Prober, detect_magic
from zhyonlineedu.modules.media.upload_service import UploadService

PASSWORD = "S3cret-pass"  # noqa: S105 - 测试用固定口令
READY = ProbeResult(ProbeState.READY, flavor="sqlite", capabilities={"returning": True})
LESSON_ID = 1


@pytest.fixture(autouse=True)
def _cleanup() -> Iterator[None]:
    compat.reset()
    clear_key_cache()
    yield
    compat.reset()
    clear_key_cache()


def _config(tmp_path: Path) -> Config:
    return load_config(
        environ={
            "ZHY_ENV": "dev",
            "ZHY_DB_URL": f"sqlite:///{tmp_path / 'upload.db'}",
            "ZHY_DB_FLAVOR": "sqlite",
            "ZHY_JWT_PRIVATE_KEY_PATH": str(tmp_path / "keys" / "jwt_private.pem"),
            "ZHY_JWT_PUBLIC_KEY_PATH": str(tmp_path / "keys" / "jwt_public.pem"),
            "ZHY_UPLOAD_TMP_ROOT": str(tmp_path / "uploads"),
        },
        sys_config={},
    )


def _seed(tmp_path: Path) -> Config:
    config = _config(tmp_path)
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
                    duration_sec=0,
                    is_free=False,
                    sort_order=1024,
                )
            )
            session.commit()
    finally:
        engine.dispose()
    return config


def _ok_prober(**overrides: Any) -> Prober:
    def probe(path: Path) -> MediaInfo:
        return MediaInfo(
            format_name=str(overrides.get("format_name", "mov,mp4,m4a,3gp,3g2,mj2")),
            duration_sec=int(overrides.get("duration_sec", 42)),
            size_bytes=path.stat().st_size,
            width=overrides.get("width", 1280),
            height=overrides.get("height", 720),
            source="ffprobe",
        )

    return probe


def _missing_prober(path: Path) -> MediaInfo | None:
    """模拟 ffprobe 不存在：require_ffprobe=false 时退化为仅魔数。"""
    return None


def _client(config: Config, prober: Prober | None = None) -> Any:
    app = create_app(config=config, probe=StaticProbe(READY))
    app.extensions["zhy_upload"] = UploadService(config, prober=prober)
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


def _mp4(size: int = 2048) -> bytes:
    header = b"\x00\x00\x00\x18ftypisom\x00\x00\x02\x00isomiso2avc1mp41"
    assert len(header) <= size
    return header + b"\x00" * (size - len(header))


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _md5(data: bytes) -> str:
    return hashlib.md5(data, usedforsecurity=False).hexdigest()  # noqa: S324 - 测试校验


def _init(
    client: Any,
    headers: dict[str, str],
    payload: bytes,
    *,
    chunk_size: int = 1024,
    sha256: str | None = None,
    file_name: str = "lesson.mp4",
    lesson_id: int = LESSON_ID,
) -> dict[str, Any]:
    response = client.post(
        "/api/v1/media/uploads",
        json={
            "lesson_id": lesson_id,
            "file_name": file_name,
            "file_size": len(payload),
            "sha256": _sha256(payload) if sha256 is None else sha256,
            "chunk_size": chunk_size,
        },
        headers=headers,
    )
    return _data(response)


def _put_chunk(
    client: Any,
    headers: dict[str, str],
    upload_id: str,
    index: int,
    data: bytes,
    md5: str | None = None,
) -> Any:
    digest = _md5(data) if md5 is None else md5
    return client.post(
        f"/api/v1/media/uploads/{upload_id}/chunks?index={index}&md5={digest}",
        data=data,
        headers={**headers, "Content-Type": "application/octet-stream"},
    )


def _upload_all(
    client: Any,
    headers: dict[str, str],
    payload: bytes,
    *,
    chunk_size: int = 1024,
    sha256: str | None = None,
) -> dict[str, Any]:
    upload = _init(client, headers, payload, chunk_size=chunk_size, sha256=sha256)
    for index in range(upload["total_chunks"]):
        block = payload[index * chunk_size : (index + 1) * chunk_size]
        _data(_put_chunk(client, headers, upload["upload_id"], index, block))
    return upload


def _complete(client: Any, headers: dict[str, str], upload_id: str) -> dict[str, Any]:
    return _data(client.post(f"/api/v1/media/uploads/{upload_id}/complete", headers=headers))


def _session_row(tmp_path: Path, upload_id: str) -> MediaUploadSession:
    config = _config(tmp_path)
    engine = create_db_engine(config.get_str("db.url"), "sqlite")
    try:
        with Session(engine) as session:
            row = session.execute(
                select(MediaUploadSession).where(MediaUploadSession.upload_id == upload_id)
            ).scalar_one()
            session.expunge(row)
            return row
    finally:
        engine.dispose()


def _session_dir(tmp_path: Path, upload_id: str) -> Path:
    return tmp_path / "uploads" / upload_id


# ── tmp 安全 ──────────────────────────────────────────────────────────────


def test_init_creates_private_session(tmp_path: Path) -> None:
    config = _seed(tmp_path)
    client = _client(config, _ok_prober())
    upload = _init(client, _headers(client), _mp4())

    assert upload["total_chunks"] == 2
    directory = _session_dir(tmp_path, upload["upload_id"])
    assert directory.is_dir()
    assert directory.stat().st_mode & 0o777 == 0o700
    assert _session_row(tmp_path, upload["upload_id"]).status == "open"


def test_init_rejects_bad_inputs(tmp_path: Path) -> None:
    config = _seed(tmp_path)
    client = _client(config, _ok_prober())
    headers = _headers(client)
    payload = _mp4()

    unknown = client.post(
        "/api/v1/media/uploads",
        json={
            "lesson_id": 999,
            "file_name": "a.mp4",
            "file_size": len(payload),
            "sha256": _sha256(payload),
        },
        headers=headers,
    )
    _error(unknown, 404, "NOT_FOUND")

    for body, message in (
        ({"file_name": "../evil.mp4"}, "路径分隔符"),
        ({"sha256": "zz"}, "sha256"),
        ({"file_size": 10**13}, "file_size"),
    ):
        merged = {
            "lesson_id": LESSON_ID,
            "file_name": "a.mp4",
            "file_size": len(payload),
            "sha256": _sha256(payload),
        }
        merged.update(body)
        response = client.post("/api/v1/media/uploads", json=merged, headers=headers)
        data = _error(response, 422, "VALIDATION_FAILED")
        assert any(message in item for item in data["errors"]), data


def test_upload_id_path_traversal_is_404(tmp_path: Path) -> None:
    config = _seed(tmp_path)
    client = _client(config, _ok_prober())
    headers = _headers(client)
    for upload_id in ("not-hex", "0" * 31, "..%2F..%2Fetc"):
        response = client.get(f"/api/v1/media/uploads/{upload_id}", headers=headers)
        assert response.status_code == 404, response.get_json()


def test_chunk_rejects_empty_body_and_bad_params(tmp_path: Path) -> None:
    config = _seed(tmp_path)
    client = _client(config, _ok_prober())
    headers = _headers(client)
    payload = _mp4()
    upload = _init(client, headers, payload)

    empty = client.post(
        f"/api/v1/media/uploads/{upload['upload_id']}/chunks?index=0&md5={_md5(b'')}",
        data=b"",
        headers={**headers, "Content-Type": "application/octet-stream"},
    )
    _error(empty, 422, "VALIDATION_FAILED")

    out_of_range = _put_chunk(client, headers, upload["upload_id"], 5, payload[:1024])
    _error(out_of_range, 422, "VALIDATION_FAILED")

    bad_md5 = _put_chunk(client, headers, upload["upload_id"], 0, payload[:1024], md5="zz")
    _error(bad_md5, 422, "VALIDATION_FAILED")


# ── 异常清单 ──────────────────────────────────────────────────────────────


def test_chunk_md5_mismatch_50101_leaves_no_file(tmp_path: Path) -> None:
    config = _seed(tmp_path)
    client = _client(config, _ok_prober())
    headers = _headers(client)
    payload = _mp4()
    upload = _init(client, headers, payload)

    response = _put_chunk(client, headers, upload["upload_id"], 0, payload[:1024], md5="0" * 32)
    data = _error(response, 409, Code.UPLOAD_CHUNK_MD5_MISMATCH)
    assert data["index"] == 0
    assert list(_session_dir(tmp_path, upload["upload_id"]).glob("*.part")) == []


def test_chunk_is_idempotent_and_private(tmp_path: Path) -> None:
    config = _seed(tmp_path)
    client = _client(config, _ok_prober())
    headers = _headers(client)
    payload = _mp4()
    upload = _init(client, headers, payload)
    block = payload[:1024]

    first = _data(_put_chunk(client, headers, upload["upload_id"], 0, block))
    assert first["duplicated"] is False
    second = _data(_put_chunk(client, headers, upload["upload_id"], 0, block))
    assert second["duplicated"] is True
    assert second["received"] == 1

    part = _session_dir(tmp_path, upload["upload_id"]) / "chunk_000000.part"
    assert part.stat().st_mode & 0o777 == 0o600


def test_complete_missing_chunk_50102(tmp_path: Path) -> None:
    config = _seed(tmp_path)
    client = _client(config, _ok_prober())
    headers = _headers(client)
    payload = _mp4()
    upload = _init(client, headers, payload)
    _data(_put_chunk(client, headers, upload["upload_id"], 0, payload[:1024]))

    response = client.post(f"/api/v1/media/uploads/{upload['upload_id']}/complete", headers=headers)
    data = _error(response, 409, Code.UPLOAD_CHUNK_MISSING)
    assert data["missing"] == [1]
    # 缺片不销毁会话：补传后可继续（断点续传）
    assert _session_row(tmp_path, upload["upload_id"]).status == "open"
    _data(_put_chunk(client, headers, upload["upload_id"], 1, payload[1024:]))
    assert _complete(client, headers, upload["upload_id"])["status"] == "completed"


def test_complete_sha256_mismatch_50103(tmp_path: Path) -> None:
    config = _seed(tmp_path)
    client = _client(config, _ok_prober())
    headers = _headers(client)
    declared = _mp4(2048)
    tampered = bytearray(declared)
    tampered[-1] = 0x7F
    upload = _init(client, headers, declared)

    payload = bytes(tampered)
    for index in range(upload["total_chunks"]):
        block = payload[index * 1024 : (index + 1) * 1024]
        _data(_put_chunk(client, headers, upload["upload_id"], index, block))

    response = client.post(f"/api/v1/media/uploads/{upload['upload_id']}/complete", headers=headers)
    data = _error(response, 415, Code.UPLOAD_MALICIOUS)
    assert data["reason"] == "sha256_mismatch"
    assert not _session_dir(tmp_path, upload["upload_id"]).exists()


def test_complete_rejects_executable_magic(tmp_path: Path) -> None:
    config = _seed(tmp_path)
    client = _client(config, _ok_prober())
    headers = _headers(client)
    payload = b"\x7fELF" + b"\x00" * 2044
    assert detect_magic(payload) == "elf"

    upload = _upload_all(client, headers, payload)
    response = client.post(f"/api/v1/media/uploads/{upload['upload_id']}/complete", headers=headers)
    data = _error(response, 415, Code.UPLOAD_MALICIOUS)
    assert data["reason"] == "format_not_allowed"
    assert data["format_name"] == "elf"
    assert not _session_dir(tmp_path, upload["upload_id"]).exists()
    assert _asset_rows(tmp_path) == []


def test_complete_rejects_duration_and_dimension(tmp_path: Path) -> None:
    for overrides, reason in (
        ({"duration_sec": 14401}, "duration_exceeded"),
        ({"width": 4097}, "dimension_exceeded"),
    ):
        case_dir = tmp_path / reason
        case_dir.mkdir()
        config = _seed(case_dir)
        client = _client(config, _ok_prober(**overrides))
        headers = _headers(client)
        upload = _upload_all(client, headers, _mp4())
        response = client.post(
            f"/api/v1/media/uploads/{upload['upload_id']}/complete", headers=headers
        )
        data = _error(response, 415, Code.UPLOAD_MALICIOUS)
        assert data["reason"] == reason
        assert not _session_dir(case_dir, upload["upload_id"]).exists()


def test_complete_requires_ffprobe_when_configured(tmp_path: Path) -> None:
    config = load_config(
        environ={
            "ZHY_ENV": "dev",
            "ZHY_DB_URL": f"sqlite:///{tmp_path / 'upload.db'}",
            "ZHY_DB_FLAVOR": "sqlite",
            "ZHY_JWT_PRIVATE_KEY_PATH": str(tmp_path / "keys" / "jwt_private.pem"),
            "ZHY_JWT_PUBLIC_KEY_PATH": str(tmp_path / "keys" / "jwt_public.pem"),
            "ZHY_UPLOAD_TMP_ROOT": str(tmp_path / "uploads"),
            "ZHY_UPLOAD_REQUIRE_FFPROBE": "true",
        },
        sys_config={},
    )
    _seed(tmp_path)
    client = _client(config, _missing_prober)
    headers = _headers(client)
    upload = _upload_all(client, headers, _mp4())
    response = client.post(f"/api/v1/media/uploads/{upload['upload_id']}/complete", headers=headers)
    data = _error(response, 415, Code.UPLOAD_MALICIOUS)
    assert data["reason"] == "ffprobe_unavailable"


# ── 正常链路 ──────────────────────────────────────────────────────────────


def test_complete_success_marks_asset_uploaded(tmp_path: Path) -> None:
    config = _seed(tmp_path)
    client = _client(config, _ok_prober())
    headers = _headers(client)
    payload = _mp4()
    upload = _upload_all(client, headers, payload)

    completed = _complete(client, headers, upload["upload_id"])
    _assert_matches(UploadCompleteOut, completed)
    assert completed["asset_status"] == "uploaded"
    assert completed["duration_sec"] == 42
    assert completed["width"] == 1280
    assert completed["probe_source"] == "ffprobe"
    assert completed["sha256"] == _sha256(payload)
    assert completed["ready_for_transcode"] is True

    # 幂等：重复 complete 返回同一结论，不重复合并
    assert _complete(client, headers, upload["upload_id"]) == completed

    asset = _asset_rows(tmp_path)
    assert len(asset) == 1
    assert asset[0]["status"] == "uploaded"
    assert asset[0]["duration_sec"] == 42
    # 课时时长由视频探测结果派生，不再由管理端手工填写。
    assert _lesson_duration(tmp_path) == 42

    status = _data(client.get(f"/api/v1/media/uploads/{upload['upload_id']}", headers=headers))
    _assert_matches(UploadStatusOut, status)
    assert status["status"] == "completed"
    assert status["missing_chunks"] == []
    assert status["received_chunks"] == [0, 1]


def test_complete_magic_fallback_when_ffprobe_missing(tmp_path: Path) -> None:
    config = _seed(tmp_path)
    client = _client(config, _missing_prober)
    headers = _headers(client)
    upload = _upload_all(client, headers, _mp4())

    completed = _complete(client, headers, upload["upload_id"])
    assert completed["probe_source"] == "magic"
    assert completed["format_name"] == "mp4"
    assert completed["duration_sec"] == 0
    # 拿不到时长时不清空既有课时时长（本用例初始为 0）。
    assert _lesson_duration(tmp_path) == 0


def test_status_and_delete(tmp_path: Path) -> None:
    config = _seed(tmp_path)
    client = _client(config, _ok_prober())
    headers = _headers(client)
    payload = _mp4()
    upload = _init(client, headers, payload)
    chunk = _data(_put_chunk(client, headers, upload["upload_id"], 0, payload[:1024]))
    _assert_matches(UploadChunkOut, chunk)

    status = _data(client.get(f"/api/v1/media/uploads/{upload['upload_id']}", headers=headers))
    assert status["received_chunks"] == [0]
    assert status["missing_chunks"] == [1]

    deleted = _data(client.delete(f"/api/v1/media/uploads/{upload['upload_id']}", headers=headers))
    _assert_matches(UploadDeleteOut, deleted)
    assert deleted["deleted"] is True
    assert not _session_dir(tmp_path, upload["upload_id"]).exists()

    # 删除后仍可读到终态（便于前端提示），但不再接收分片
    after = _data(client.get(f"/api/v1/media/uploads/{upload['upload_id']}", headers=headers))
    assert after["status"] == "deleted"
    rejected = _put_chunk(client, headers, upload["upload_id"], 1, payload[1024:])
    _error(rejected, 404, "NOT_FOUND")


# ── tmp-reaper ────────────────────────────────────────────────────────────


def test_reaper_removes_expired_sessions(tmp_path: Path) -> None:
    config = _seed(tmp_path)
    app = create_app(config=config, probe=StaticProbe(READY))
    service = UploadService(config, prober=_ok_prober())
    app.extensions["zhy_upload"] = service
    client = app.test_client()
    headers = _headers(client)

    expired = _init(client, headers, _mp4())
    fresh = _init(client, headers, _mp4())
    engine = create_db_engine(config.get_str("db.url"), "sqlite")
    try:
        with Session(engine) as session:
            row = session.execute(
                select(MediaUploadSession).where(
                    MediaUploadSession.upload_id == expired["upload_id"]
                )
            ).scalar_one()
            row.expires_at = datetime.now(UTC) - timedelta(minutes=1)
            session.commit()
    finally:
        engine.dispose()

    assert service.reap() == 1
    assert not _session_dir(tmp_path, expired["upload_id"]).exists()
    assert _session_dir(tmp_path, fresh["upload_id"]).exists()
    assert _session_row(tmp_path, expired["upload_id"]).status == "expired"


# ── RBAC 与模型漂移 ───────────────────────────────────────────────────────


def test_student_cannot_upload(tmp_path: Path) -> None:
    config = _seed(tmp_path)
    client = _client(config, _ok_prober())
    headers = _headers(client, "student")
    payload = _mp4()
    response = client.post(
        "/api/v1/media/uploads",
        json={
            "lesson_id": LESSON_ID,
            "file_name": "a.mp4",
            "file_size": len(payload),
            "sha256": _sha256(payload),
        },
        headers=headers,
    )
    _error(response, 403, "PERMISSION_DENIED")


def test_init_model_matches_response(tmp_path: Path) -> None:
    config = _seed(tmp_path)
    client = _client(config, _ok_prober())
    upload = _init(client, _headers(client), _mp4())
    _assert_matches(UploadInitOut, upload)


def _asset_rows(tmp_path: Path) -> list[dict[str, Any]]:
    config = _config(tmp_path)
    engine = create_db_engine(config.get_str("db.url"), "sqlite")
    try:
        with Session(engine) as session:
            rows = session.execute(select(MediaAsset)).scalars().all()
            return [
                {"status": row.status, "duration_sec": row.duration_sec, "lesson_id": row.lesson_id}
                for row in rows
            ]
    finally:
        engine.dispose()


def _lesson_duration(tmp_path: Path) -> int:
    config = _config(tmp_path)
    engine = create_db_engine(config.get_str("db.url"), "sqlite")
    try:
        with Session(engine) as session:
            lesson = session.get(EduCourseLesson, LESSON_ID)
            assert lesson is not None
            return int(lesson.duration_sec)
    finally:
        engine.dispose()
