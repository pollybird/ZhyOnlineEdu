"""M2.5 TicketedHls + 进度网关（§10.3.3 / §10.3.4 / §12.5 · F2/F3/F4）。

验收口径：**没有 ticket 就拿不到 key**。
本文件覆盖：
- 票据：登录必需、encoded 才可签、未发布仅 course:edit（preview）、member 暂无会员 → 403；
- playlist：master 档位过滤 + URI 改写、variant 的 key/分片改写、票据 URL 可复用、
  旧 ksession 宽限期后失效；
- key：缺参/无效签名/过期/串课/重放分别 40201/40203/40206，正常返回 16 字节并按 kid 派生；
- 分片：路径白名单 + kid 归属 + Range/206 + 跨目录 404；
- 进度：HTTP 202 + 只写 log（投影仍空）、30s 桶去重、越界校验、preview 不计统计；
- flush：单写者租约、绝对值重算 + GREATEST 不回退、completed、崩溃期 log 重放（HBT-002）；
- 总线：LocalBus 追加/归并/ack、RingFull、redis-stream 显式拒绝。
"""

from __future__ import annotations

import json
import shutil
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

import pytest
from sqlalchemy import Engine, select, update
from sqlalchemy.orm import Session
from zhyonlineedu.api.schemas import ProgressOut, TicketOut
from zhyonlineedu.app import create_app
from zhyonlineedu.core.auth.keys import clear_key_cache, ensure_keypair
from zhyonlineedu.core.auth.passwords import hash_password
from zhyonlineedu.core.capability import ProbeResult, ProbeState, StaticProbe
from zhyonlineedu.core.codes import Code
from zhyonlineedu.core.config import Config, load_config
from zhyonlineedu.core.constants import ZHY_PROGRESS_METRIC_VERSION
from zhyonlineedu.core.db import compat
from zhyonlineedu.core.db.compat import Dialect
from zhyonlineedu.core.db.engine import create_db_engine
from zhyonlineedu.core.db.migration import upgrade_head
from zhyonlineedu.core.db.models.account import AccountUser, SysUserRole
from zhyonlineedu.core.db.models.catalog import (
    EduCourse,
    EduCourseChapter,
    EduCourseLesson,
    EduSubject,
)
from zhyonlineedu.core.db.models.learn import (
    DEFAULT_SHARD,
    LearnCourseProgress,
    LearnProgress,
    LearnProgressGate,
    LearnProgressLog,
    utcnow,
)
from zhyonlineedu.core.db.models.media import MediaAsset, MediaKeySession
from zhyonlineedu.core.media.keys import derive_aes_key, load_master_secret, sign_query
from zhyonlineedu.core.progress.bus import (
    LocalBus,
    ProgressCmd,
    RedisBus,
    RingFull,
    get_bus,
)
from zhyonlineedu.core.progress.flush import ProgressFlush, greatest_sql, replay_unflushed
from zhyonlineedu.core.progress.gateway import ProgressGateway
from zhyonlineedu.workers.progress_flush import run_flush

PASSWORD = "S3cret-pass"  # noqa: S105 - 测试用固定口令
READY = ProbeResult(ProbeState.READY, flavor="sqlite", capabilities={"returning": True})

L_PUBLIC = 1  # published/public，资产 encoded（kid=KID1，360+720），HLS 已落盘
L_MEMBER = 2  # published/member（当前无会员表 → ACCESS_LEVEL_REQUIRED）
L_DRAFT = 3  # draft/public，资产 encoded（kid=KID3）
L_UPLOADING = 4  # published/public，资产 uploaded（40202）
L_OTHER = 5  # published/public，另一门课，资产 encoded（kid=KID4，用于串课）
DURATION = 100

KID1 = "b" * 32
KID2 = "c" * 32
KID3 = "d" * 32
KID4 = "e" * 32


@pytest.fixture(autouse=True)
def _cleanup() -> Iterator[None]:
    compat.reset()
    clear_key_cache()
    yield
    compat.reset()
    clear_key_cache()


# ── 装配 ──────────────────────────────────────────────────────────────────


def _config(tmp_path: Path, **overrides: str) -> Config:
    environ = {
        "ZHY_ENV": "dev",
        "ZHY_DB_URL": f"sqlite:///{tmp_path / 'progress.db'}",
        "ZHY_DB_FLAVOR": "sqlite",
        "ZHY_JWT_PRIVATE_KEY_PATH": str(tmp_path / "keys" / "jwt_private.pem"),
        "ZHY_JWT_PUBLIC_KEY_PATH": str(tmp_path / "keys" / "jwt_public.pem"),
        "ZHY_STORAGE_LOCAL_DIR": str(tmp_path / "storage"),
        "ZHY_PROGRESS_RING_DIR": str(tmp_path / "ring"),
        "ZHY_MEDIA_NONCE_REUSE_WINDOW_SEC": "30",
    }
    environ.update(overrides)
    return load_config(environ=environ, sys_config={})


def _write_hls(storage: Path, lesson_id: int, kid: str, profiles: tuple[str, ...]) -> None:
    root = storage / "media" / str(lesson_id) / "hls"
    root.mkdir(parents=True, exist_ok=True)
    master = ["#EXTM3U", "#EXT-X-VERSION:3"]
    for profile in profiles:
        master.append("#EXT-X-STREAM-INF:BANDWIDTH=1000000,RESOLUTION=640x360")
        master.append(f"{profile}/index.m3u8")
    (root / "master.m3u8").write_text("\n".join(master) + "\n", encoding="utf-8")
    for profile in profiles:
        directory = root / profile
        directory.mkdir(parents=True, exist_ok=True)
        for name in ("seg_000.ts", "seg_001.ts"):
            (directory / name).write_bytes(b"ts-" + profile.encode() + b"-" + name.encode())
        variant = [
            "#EXTM3U",
            "#EXT-X-VERSION:3",
            "#EXT-X-TARGETDURATION:4",
            "#EXT-X-MEDIA-SEQUENCE:0",
            f'#EXT-X-KEY:METHOD=AES-128,URI="/api/v1/media/key/{kid}"',
            "#EXTINF:4.000000,",
            "seg_000.ts",
            "#EXTINF:4.000000,",
            "seg_001.ts",
            "#EXT-X-ENDLIST",
        ]
        (directory / "index.m3u8").write_text("\n".join(variant) + "\n", encoding="utf-8")


def _seed(tmp_path: Path, **overrides: str) -> Config:
    config = _config(tmp_path, **overrides)
    ensure_keypair(config.get_str("jwt.private_key_path"), config.get_str("jwt.public_key_path"))
    url = config.get_str("db.url")
    upgrade_head(url, "sqlite")
    engine = create_db_engine(url, "sqlite")
    storage = Path(config.get_str("storage.local_dir"))
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

            def add_course(title: str, status: str, access_type: str) -> EduCourse:
                row = EduCourse(
                    subject_id=subject.id,
                    title=title,
                    mode="both",
                    access_type=access_type,
                    status=status,
                    sort_order=1024,
                )
                session.add(row)
                session.flush()
                return row

            def add_lesson(
                course: EduCourse, lesson_id: int, *, duration: int = DURATION
            ) -> EduCourseLesson:
                chapter = EduCourseChapter(
                    course_id=course.id, title=f"章{lesson_id}", sort_order=1024
                )
                session.add(chapter)
                session.flush()
                lesson = EduCourseLesson(
                    id=lesson_id,
                    chapter_id=chapter.id,
                    course_id=course.id,
                    title=f"课{lesson_id}",
                    kind="video",
                    duration_sec=duration,
                    is_free=False,
                    sort_order=1024,
                )
                session.add(lesson)
                return lesson

            def add_asset(
                lesson_id: int,
                *,
                status: str,
                kid: str | None = None,
                profiles: tuple[str, ...] | None = None,
            ) -> None:
                session.add(
                    MediaAsset(
                        lesson_id=lesson_id,
                        status=status,
                        playlist_version=1,
                        duration_sec=DURATION,
                        kid=kid,
                        profiles=json.dumps(list(profiles)) if profiles else None,
                        created_at=now,
                        updated_at=now,
                    )
                )

            public = add_course("公开课", "published", "public")
            add_lesson(public, L_PUBLIC)
            add_asset(L_PUBLIC, status="encoded", kid=KID1, profiles=("360", "720"))

            member = add_course("会员课", "published", "member")
            add_lesson(member, L_MEMBER)
            add_asset(L_MEMBER, status="encoded", kid=KID2, profiles=("360",))

            draft = add_course("草稿课", "draft", "public")
            add_lesson(draft, L_DRAFT)
            add_asset(L_DRAFT, status="encoded", kid=KID3, profiles=("360",))

            uploading = add_course("转码中", "published", "public")
            add_lesson(uploading, L_UPLOADING)
            add_asset(L_UPLOADING, status="uploaded")

            other = add_course("另一门课", "published", "public")
            add_lesson(other, L_OTHER)
            add_asset(L_OTHER, status="encoded", kid=KID4, profiles=("360",))

            session.commit()
    finally:
        engine.dispose()

    _write_hls(storage, L_PUBLIC, KID1, ("360", "720"))
    return config


def _engine(config: Config) -> Engine:
    return create_db_engine(config.get_str("db.url"), "sqlite")


def _client(config: Config) -> Any:
    return create_app(config=config, probe=StaticProbe(READY)).test_client()


def _login(client: Any, username: str) -> dict[str, str]:
    response = client.post("/api/v1/auth/login", json={"username": username, "password": PASSWORD})
    assert response.status_code == 200, response.get_json()
    return {"Authorization": f"Bearer {response.get_json()['data']['access_token']}"}


def _user_id(config: Config, username: str) -> int:
    engine = _engine(config)
    try:
        with Session(engine) as session:
            value = session.execute(
                select(AccountUser.id).where(AccountUser.username == username)
            ).scalar_one()
        return int(value)
    finally:
        engine.dispose()


def _signed(ticket: dict[str, Any]) -> dict[str, str]:
    """返回票据 URL 上的原样查询串（`n` 即 nonce）。"""
    query = parse_qs(urlparse(str(ticket["playlist_url"])).query)
    return {
        "ks": query["ks"][0],
        "n": query["n"][0],
        "exp": query["exp"][0],
        "sig": query["sig"][0],
    }


def _ticket(client: Any, headers: dict[str, str], lesson_id: int) -> dict[str, Any]:
    response = client.post("/api/v1/media/tickets", json={"lesson_id": lesson_id}, headers=headers)
    assert response.status_code == 200, response.get_json()
    data = response.get_json()["data"]
    assert set(data) == set(TicketOut.model_fields)
    assert data["metric_version"] == ZHY_PROGRESS_METRIC_VERSION
    return data


# ── 票据签发 ──────────────────────────────────────────────────────────────


@pytest.mark.media
def test_ticket_requires_login(tmp_path: Path) -> None:
    client = _client(_seed(tmp_path))
    response = client.post("/api/v1/media/tickets", json={"lesson_id": L_PUBLIC})
    assert response.status_code == 401
    assert response.get_json()["code"] == "AUTH_TOKEN_EXPIRED"


@pytest.mark.media
def test_ticket_only_for_encoded_asset(tmp_path: Path) -> None:
    client = _client(_seed(tmp_path))
    headers = _login(client, "root")
    response = client.post(
        "/api/v1/media/tickets", json={"lesson_id": L_UPLOADING}, headers=headers
    )
    assert response.status_code == 409
    body = response.get_json()
    assert body["code"] == int(Code.MEDIA_NOT_READY)
    assert body["data"]["asset_status"] == "uploaded"
    assert body["data"]["retry_after"] == 30


@pytest.mark.media
def test_ticket_draft_requires_preview_permission(tmp_path: Path) -> None:
    client = _client(_seed(tmp_path))
    student = _login(client, "student")
    denied = client.post("/api/v1/media/tickets", json={"lesson_id": L_DRAFT}, headers=student)
    assert denied.status_code == 403
    assert denied.get_json()["code"] == "COURSE_NOT_PUBLISHED"

    root = _login(client, "root")
    preview = client.post("/api/v1/media/tickets", json={"lesson_id": L_DRAFT}, headers=root)
    assert preview.status_code == 200
    data = preview.get_json()["data"]
    assert data["preview"] is True
    assert data["kid"] == KID3


@pytest.mark.media
def test_ticket_member_needs_level(tmp_path: Path) -> None:
    client = _client(_seed(tmp_path))
    headers = _login(client, "student")
    response = client.post("/api/v1/media/tickets", json={"lesson_id": L_MEMBER}, headers=headers)
    assert response.status_code == 403
    body = response.get_json()
    assert body["code"] == "ACCESS_LEVEL_REQUIRED"
    assert body["data"]["access_type"] == "member"


@pytest.mark.media
def test_ticket_unknown_lesson_is_404(tmp_path: Path) -> None:
    client = _client(_seed(tmp_path))
    response = client.post(
        "/api/v1/media/tickets", json={"lesson_id": 999}, headers=_login(client, "student")
    )
    assert response.status_code == 404
    assert response.get_json()["code"] == "NOT_FOUND"


@pytest.mark.media
def test_ticket_payload_validated(tmp_path: Path) -> None:
    client = _client(_seed(tmp_path))
    headers = _login(client, "student")
    for body in ({}, {"lesson_id": "1"}, {"lesson_id": 0}, {"lesson_id": True}):
        response = client.post("/api/v1/media/tickets", json=body, headers=headers)
        assert response.status_code == 422
        assert response.get_json()["code"] == "VALIDATION_FAILED"


# ── 验收口径：无 ticket 拿不到 key ────────────────────────────────────────


@pytest.mark.media
def test_key_requires_ticket(tmp_path: Path) -> None:
    client = _client(_seed(tmp_path))
    bare = client.get(f"/api/v1/media/key/{KID1}")
    assert bare.status_code == 401
    body = bare.get_json()
    assert body["code"] == int(Code.MEDIA_TICKET_EXPIRED)
    assert body["data"]["reason"] == "ticket_required"
    assert bare.headers["Cache-Control"].startswith("no-store")

    partial = client.get(f"/api/v1/media/key/{KID1}?ks={'a' * 32}")
    assert partial.status_code == 401
    assert partial.get_json()["data"]["reason"] == "ticket_required"

    forged = client.get(f"/api/v1/media/key/{KID1}?ks={'a' * 32}&n=x&exp=1&sig=x")
    assert forged.status_code == 401
    assert forged.get_json()["data"]["reason"] == "session_missing"


@pytest.mark.media
def test_key_rejects_bad_signature(tmp_path: Path) -> None:
    client = _client(_seed(tmp_path))
    ticket = _ticket(client, _login(client, "student"), L_PUBLIC)
    args = _signed(ticket)
    bad = dict(args, sig="0" * 32)
    response = client.get(f"/api/v1/media/key/{KID1}", query_string=bad)
    assert response.status_code == 403
    assert response.get_json()["code"] == int(Code.MEDIA_TICKET_MISMATCH)


@pytest.mark.media
def test_key_rejects_expired_url(tmp_path: Path) -> None:
    config = _seed(tmp_path)
    client = _client(config)
    ticket = _ticket(client, _login(client, "student"), L_PUBLIC)
    args = _signed(ticket)
    secret = load_master_secret(config, Path(config.get_str("storage.local_dir")))
    exp = int(datetime.now(UTC).timestamp()) - 10
    sig = sign_query(secret, "key", args["ks"], KID1, args["n"], str(exp))
    response = client.get(
        f"/api/v1/media/key/{KID1}",
        query_string={"ks": args["ks"], "n": args["n"], "exp": exp, "sig": sig},
    )
    assert response.status_code == 401
    body = response.get_json()
    assert body["code"] == int(Code.MEDIA_TICKET_EXPIRED)
    assert body["data"]["reason"] == "url_expired"


@pytest.mark.media
def test_key_rejects_other_kid(tmp_path: Path) -> None:
    client = _client(_seed(tmp_path))
    ticket = _ticket(client, _login(client, "student"), L_PUBLIC)
    args = _signed(ticket)
    # 串课：票据签名针对 KID1，却拿 KID4 的 key
    response = client.get(f"/api/v1/media/key/{KID4}", query_string=args)
    assert response.status_code == 403
    body = response.get_json()
    assert body["code"] == int(Code.MEDIA_TICKET_MISMATCH)
    assert body["data"]["reason"] == "kid_not_in_session"


@pytest.mark.media
def test_key_nonce_single_use(tmp_path: Path) -> None:
    config = _seed(tmp_path, ZHY_MEDIA_NONCE_REUSE_WINDOW_SEC="0")
    client = _client(config)
    ticket = _ticket(client, _login(client, "student"), L_PUBLIC)
    master = client.get(str(ticket["playlist_url"]))
    assert master.status_code == 200
    variant_url = next(
        line for line in master.get_data(as_text=True).splitlines() if "/360/" in line
    )
    variant = client.get(variant_url)
    assert variant.status_code == 200
    key_line = next(
        line
        for line in variant.get_data(as_text=True).splitlines()
        if line.startswith("#EXT-X-KEY")
    )
    key_url = key_line.split('URI="', 1)[1].rstrip('"')

    first = client.get(key_url)
    assert first.status_code == 200
    assert len(first.get_data()) == 16
    assert first.headers["X-Content-Type-Options"] == "nosniff"

    replay = client.get(key_url)
    assert replay.status_code == 403
    body = replay.get_json()
    assert body["code"] == int(Code.MEDIA_TICKET_REPLAY)
    assert body["data"]["reason"] == "nonce_replay"


# ── playlist 重写 ─────────────────────────────────────────────────────────


@pytest.mark.media
def test_master_and_variant_are_rewritten(tmp_path: Path) -> None:
    config = _seed(tmp_path)
    client = _client(config)
    ticket = _ticket(client, _login(client, "student"), L_PUBLIC)
    master = client.get(str(ticket["playlist_url"]))
    assert master.status_code == 200
    assert master.headers["Content-Type"].startswith("application/vnd.apple.mpegurl")
    assert master.headers["Cache-Control"].startswith("no-store")
    text = master.get_data(as_text=True)
    uris = [line for line in text.splitlines() if line.startswith("/api/v1/media/playlist/")]
    assert len(uris) == 2  # 360 + 720 均在票据允许集合内
    assert all("ks=" in uri and "sig=" in uri for uri in uris)
    assert "://" not in text

    variant = client.get(uris[0])
    assert variant.status_code == 200
    body = variant.get_data(as_text=True)
    key_lines = [line for line in body.splitlines() if line.startswith("#EXT-X-KEY")]
    assert len(key_lines) == 1
    assert f"/api/v1/media/key/{KID1}?ks=" in key_lines[0]
    segments = [line for line in body.splitlines() if line.startswith("/api/v1/media/seg/")]
    assert segments == [
        f"/api/v1/media/seg/{KID1}/360/seg_000.ts",
        f"/api/v1/media/seg/{KID1}/360/seg_001.ts",
    ]


@pytest.mark.media
def test_master_filters_profiles(tmp_path: Path) -> None:
    config = _seed(tmp_path)
    engine = _engine(config)
    try:
        with Session(engine) as session:
            session.execute(
                update(MediaAsset)
                .where(MediaAsset.lesson_id == L_PUBLIC)
                .values(profiles=json.dumps(["360"]))
            )
            session.commit()
    finally:
        engine.dispose()
    client = _client(config)
    ticket = _ticket(client, _login(client, "student"), L_PUBLIC)
    assert ticket["profiles"] == ["360"]
    master = client.get(str(ticket["playlist_url"]))
    assert master.status_code == 200
    text = master.get_data(as_text=True)
    assert text.count("#EXT-X-STREAM-INF") == 1
    assert "/360/" in text
    assert "/720/" not in text

    args = _signed(ticket)
    denied = client.get(f"/api/v1/media/playlist/{KID1}/720/index.m3u8", query_string=args)
    assert denied.status_code == 403
    assert denied.get_json()["data"]["reason"] == "profile_not_allowed"


@pytest.mark.media
def test_playlist_url_reusable_and_old_ticket_expires(tmp_path: Path) -> None:
    """同一票据的 playlist URL 在有效期内可重复拉取（ABR 切档依赖）。

    nonce 不随拉取轮换，因此旧 URL 在有效期内一直可用；旧票失效改由
    「同用户/课时签发新票 → 旧 ksession superseded」保证，宽限期后 40201。
    """
    config = _seed(tmp_path)
    client = _client(config)
    headers = _login(client, "student")
    ticket = _ticket(client, headers, L_PUBLIC)
    assert client.get(str(ticket["playlist_url"])).status_code == 200
    assert client.get(str(ticket["playlist_url"])).status_code == 200

    _ticket(client, headers, L_PUBLIC)  # 新票把旧 ksession 标记 superseded
    # 宽限期内旧 URL 仍可用（换票与 ABR 切档存在竞态）
    assert client.get(str(ticket["playlist_url"])).status_code == 200

    engine = _engine(config)
    try:
        with Session(engine) as session:
            session.execute(
                update(MediaKeySession).values(
                    superseded_at=utcnow() - timedelta(seconds=600),
                    updated_at=utcnow() - timedelta(seconds=600),
                )
            )
            session.commit()
    finally:
        engine.dispose()

    stale = client.get(str(ticket["playlist_url"]))
    assert stale.status_code == 401
    body = stale.get_json()
    assert body["code"] == int(Code.MEDIA_TICKET_EXPIRED)
    assert body["data"]["reason"] == "session_superseded"


@pytest.mark.media
def test_playlist_rejects_external_reference(tmp_path: Path) -> None:
    config = _seed(tmp_path)
    root = Path(config.get_str("storage.local_dir")) / "media" / str(L_PUBLIC) / "hls"
    (root / "master.m3u8").write_text(
        "#EXTM3U\n#EXT-X-STREAM-INF:BANDWIDTH=1\nhttps://evil.example/360/index.m3u8\n",
        encoding="utf-8",
    )
    client = _client(config)
    ticket = _ticket(client, _login(client, "student"), L_PUBLIC)
    response = client.get(str(ticket["playlist_url"]))
    assert response.status_code == 410
    assert response.get_json()["code"] == int(Code.MEDIA_PLAYBACK_UNRECOVERABLE)


# ── 分片 ──────────────────────────────────────────────────────────────────


@pytest.mark.media
def test_segment_served_with_range(tmp_path: Path) -> None:
    client = _client(_seed(tmp_path))
    response = client.get(f"/api/v1/media/seg/{KID1}/360/seg_000.ts")
    assert response.status_code == 200
    assert response.headers["Content-Type"] == "video/mp2t"
    assert response.headers["Cache-Control"].startswith("public")

    partial = client.get(f"/api/v1/media/seg/{KID1}/360/seg_000.ts", headers={"Range": "bytes=0-5"})
    assert partial.status_code == 206
    assert partial.get_data() == b"ts-360"


@pytest.mark.media
def test_segment_path_whitelist(tmp_path: Path) -> None:
    client = _client(_seed(tmp_path))
    for path in (
        f"/api/v1/media/seg/{KID1}/360/master.m3u8",
        f"/api/v1/media/seg/{KID1}/999/seg_000.ts",
        f"/api/v1/media/seg/{KID1}/360/seg_999.ts",
        f"/api/v1/media/seg/{'z' * 32}/360/seg_000.ts",
    ):
        response = client.get(path)
        assert response.status_code == 404, path
        assert response.get_json()["code"] == "NOT_FOUND"


# ── 进度网关（HTTP）───────────────────────────────────────────────────────


def _post_progress(client: Any, headers: dict[str, str], **body: Any) -> Any:
    payload: dict[str, Any] = {"lesson_id": L_PUBLIC, "position_sec": 12, "played_seconds": 10}
    payload.update(body)
    return client.post("/api/v1/learn/progress", json=payload, headers=headers)


@pytest.mark.hbt
def test_progress_returns_202_and_only_logs(tmp_path: Path) -> None:
    config = _seed(tmp_path)
    client = _client(config)
    response = _post_progress(client, _login(client, "student"))
    assert response.status_code == 202
    body = response.get_json()
    assert body["code"] == int(Code.PROGRESS_ACCEPTED) == 202
    assert set(body["data"]) == set(ProgressOut.model_fields)
    assert body["data"]["gw_seq"] == 1
    assert body["data"]["deduped"] is False
    assert body["request_id"]

    engine = _engine(config)
    try:
        with Session(engine) as session:
            logs = list(
                session.execute(select(LearnProgressLog).order_by(LearnProgressLog.id)).scalars()
            )
            progress_rows = list(session.execute(select(LearnProgress)).scalars())
        assert len(logs) == 1
        log = logs[0]
        assert (log.user_id, log.lesson_id, log.course_id) == (
            _user_id(config, "student"),
            L_PUBLIC,
            1,
        )
        assert log.seq_no == 1
        assert log.gw_seq == 1
        assert log.position == 12
        assert log.played_seconds == 10
        assert log.source == "play"
        assert log.write_mode == "buffered"
        assert log.flushed_at is None
        # 红线：HTTP 路径绝不写投影表
        assert progress_rows == []
    finally:
        engine.dispose()


@pytest.mark.hbt
def test_progress_bucket_dedup(tmp_path: Path) -> None:
    config = _seed(tmp_path)
    client = _client(config)
    headers = _login(client, "student")
    first = _post_progress(client, headers)
    assert first.get_json()["data"]["deduped"] is False
    second = _post_progress(client, headers, position_sec=20)
    assert second.get_json()["data"]["deduped"] is True
    assert second.get_json()["data"]["gw_seq"] == 2

    engine = _engine(config)
    try:
        with Session(engine) as session:
            logs = list(
                session.execute(select(LearnProgressLog).order_by(LearnProgressLog.id)).scalars()
            )
        assert [log.played_seconds for log in logs] == [10, 0]
        assert [log.position_bucket for log in logs] == [0, 0]
    finally:
        engine.dispose()


@pytest.mark.hbt
def test_progress_validation_bounds(tmp_path: Path) -> None:
    client = _client(_seed(tmp_path))
    headers = _login(client, "student")
    for body in (
        {"position_sec": DURATION + 100},
        {"position_sec": -1},
        {"played_seconds": 3601},
        {"speed": 4.0},
        {"speed": 0.4},
        {"client_ts_ms": "x"},
    ):
        response = _post_progress(client, headers, **body)
        assert response.status_code == 422, body
        assert response.get_json()["code"] == "VALIDATION_FAILED"


@pytest.mark.hbt
def test_progress_rejects_draft_without_preview(tmp_path: Path) -> None:
    client = _client(_seed(tmp_path))
    response = _post_progress(client, _login(client, "student"), lesson_id=L_DRAFT)
    assert response.status_code == 403
    assert response.get_json()["code"] == int(Code.MEDIA_TICKET_MISMATCH)


@pytest.mark.hbt
def test_progress_preview_not_counted(tmp_path: Path) -> None:
    config = _seed(tmp_path)
    client = _client(config)
    response = _post_progress(client, _login(client, "root"), lesson_id=L_DRAFT)
    assert response.status_code == 202

    engine = _engine(config)
    try:
        with Session(engine) as session:
            log = session.execute(select(LearnProgressLog)).scalar_one()
            assert log.source == "preview"
            assert log.played_seconds > 0
    finally:
        engine.dispose()

    ProgressFlush(config).flush_once()
    engine = _engine(config)
    try:
        with Session(engine) as session:
            rows = list(session.execute(select(LearnProgress)).scalars())
            courses = list(session.execute(select(LearnCourseProgress)).scalars())
            flushed = session.execute(select(LearnProgressLog.flushed_at)).scalar_one()
        assert rows == []
        assert courses == []
        assert flushed is not None
    finally:
        engine.dispose()


@pytest.mark.hbt
def test_progress_survives_ring_full(tmp_path: Path) -> None:
    config = _seed(tmp_path)
    app = create_app(config=config, probe=StaticProbe(READY))
    gateway = app.extensions["zhy_progress"]

    class _FullBus:
        def publish(self, cmd: ProgressCmd) -> None:
            raise RingFull("full")

        def consume(self, batch: int) -> list[ProgressCmd]:
            return []

        def ack(self, up_to_gw_seq: int) -> None:
            return None

    gateway._bus = _FullBus()
    client = app.test_client()
    response = _post_progress(client, _login(client, "student"))
    assert response.status_code == 202
    assert response.get_json()["data"]["gw_seq"] == 1

    engine = _engine(config)
    try:
        with Session(engine) as session:
            assert session.execute(select(LearnProgressLog)).scalar_one().gw_seq == 1
    finally:
        engine.dispose()


# ── 单写者 flush ──────────────────────────────────────────────────────────


def _record(config: Config, *, lesson_id: int = L_PUBLIC, position: int, played: int) -> None:
    ProgressGateway(config).record(
        user_id=_user_id(config, "student"),
        lesson_id=lesson_id,
        body={
            "lesson_id": lesson_id,
            "position_sec": position,
            "played_seconds": played,
            "speed": 1.0,
            "client_ts_ms": 0,
        },
        can_preview=False,
    )


def _progress_row(config: Config) -> LearnProgress | None:
    engine = _engine(config)
    try:
        with Session(engine) as session:
            return session.execute(select(LearnProgress)).scalar_one_or_none()
    finally:
        engine.dispose()


def _course_row(config: Config) -> LearnCourseProgress | None:
    engine = _engine(config)
    try:
        with Session(engine) as session:
            return session.execute(select(LearnCourseProgress)).scalar_one_or_none()
    finally:
        engine.dispose()


@pytest.mark.hbt
def test_flush_projects_absolute_values(tmp_path: Path) -> None:
    config = _seed(tmp_path)
    _record(config, position=12, played=10)
    _record(config, position=45, played=10)
    assert _progress_row(config) is None

    flush = ProgressFlush(config)
    assert flush.flush_once() == 2

    row = _progress_row(config)
    assert row is not None
    assert (row.watch_seconds, row.last_position) == (20, 45)
    assert float(row.percent) == 20.0
    assert row.completed is False
    assert row.source == "recovery"
    assert row.version >= 1

    course = _course_row(config)
    assert course is not None
    assert (course.watch_seconds, course.completed_lessons) == (20, 0)
    assert course.total_lessons == 1
    assert float(course.percent) == 0.0
    assert (course.resume_lesson_id, course.resume_position) == (L_PUBLIC, 45)


@pytest.mark.hbt
def test_flush_greatest_never_regresses(tmp_path: Path) -> None:
    config = _seed(tmp_path)
    _record(config, position=12, played=10)
    _record(config, position=45, played=10)
    ProgressFlush(config).flush_once()

    engine = _engine(config)
    try:
        with Session(engine) as session:
            session.execute(update(LearnProgress).values(watch_seconds=50))
            session.commit()
    finally:
        engine.dispose()

    _record(config, position=60, played=5)
    ProgressFlush(config).flush_once()

    row = _progress_row(config)
    assert row is not None
    # GREATEST 红线：重算值 25 < 库内 50，不得回退
    assert row.watch_seconds == 50
    assert row.last_position == 60
    # 首次 flush 为 INSERT（version=1），第二次为 UPDATE 递增
    assert row.version == 2


@pytest.mark.hbt
def test_flush_marks_completed(tmp_path: Path) -> None:
    config = _seed(tmp_path)
    _record(config, position=95, played=DURATION)
    assert ProgressFlush(config).flush_once() == 1

    row = _progress_row(config)
    assert row is not None
    assert row.completed is True
    assert row.completed_at is not None
    assert float(row.percent) == 100.0
    assert row.duration_sec == DURATION

    course = _course_row(config)
    assert course is not None
    assert course.completed is True
    assert course.completed_lessons == 1
    assert float(course.percent) == 100.0


@pytest.mark.hbt
def test_flush_lease_is_single_writer(tmp_path: Path) -> None:
    config = _seed(tmp_path)
    _record(config, position=12, played=10)

    engine = _engine(config)
    try:
        with Session(engine) as session:
            session.execute(
                update(LearnProgressGate)
                .where(LearnProgressGate.shard == DEFAULT_SHARD)
                .values(writer_id="other-host:1", leased_until=utcnow() + timedelta(minutes=5))
            )
            session.commit()
    finally:
        engine.dispose()

    assert ProgressFlush(config).flush_once() == 0
    assert _progress_row(config) is None

    engine = _engine(config)
    try:
        with Session(engine) as session:
            session.execute(
                update(LearnProgressGate)
                .where(LearnProgressGate.shard == DEFAULT_SHARD)
                .values(writer_id="", leased_until=utcnow() - timedelta(seconds=1))
            )
            session.commit()
    finally:
        engine.dispose()

    assert ProgressFlush(config).flush_once() == 1
    assert _progress_row(config) is not None


@pytest.mark.hbt
def test_replay_unflushed_uses_db_log_when_ring_lost(tmp_path: Path) -> None:
    config = _seed(tmp_path)
    _record(config, position=12, played=10)
    _record(config, position=45, played=10)
    # 模拟总线文件丢失/损坏：事实源仍是 learn_progress_log（HBT-002）
    shutil.rmtree(Path(config.get_str("progress.ring_dir")), ignore_errors=True)

    flush = ProgressFlush(config)
    assert replay_unflushed(flush, rounds=2) == 2
    assert flush.flush_once() == 0
    row = _progress_row(config)
    assert row is not None and row.watch_seconds == 20


@pytest.mark.hbt
def test_worker_once_replays_on_start(tmp_path: Path) -> None:
    config = _seed(tmp_path)
    _record(config, position=12, played=10)
    assert run_flush(ProgressFlush(config), once=True) == 1
    assert _progress_row(config) is not None


@pytest.mark.hbt
def test_greatest_sql_per_dialect() -> None:
    assert greatest_sql(Dialect.SQLITE, "watch_seconds", "watch") == "MAX(watch_seconds, :watch)"
    assert (
        greatest_sql(Dialect.PG_FAMILY, "watch_seconds", "watch")
        == "GREATEST(watch_seconds, :watch)"
    )


# ── 总线 ──────────────────────────────────────────────────────────────────


def _cmd(gw_seq: int, **overrides: Any) -> ProgressCmd:
    values: dict[str, Any] = {
        "gw_seq": gw_seq,
        "user_id": 1,
        "lesson_id": L_PUBLIC,
        "course_id": 1,
        "seq_no": gw_seq,
        "position_sec": 12,
        "played_seconds": 10,
        "speed_bps": 100,
        "source": "play",
        "client_ts_ms": 0,
    }
    values.update(overrides)
    return ProgressCmd(**values)


@pytest.mark.hbt
def test_local_bus_publish_consume_ack(tmp_path: Path) -> None:
    bus = LocalBus(tmp_path / "ring")
    bus.publish(_cmd(2))
    bus.publish(_cmd(1))
    assert [cmd.gw_seq for cmd in bus.consume(10)] == [1, 2]
    assert [cmd.gw_seq for cmd in bus.consume(1)] == [1]
    bus.ack(2)
    assert bus.consume(10) == []
    assert all(path.stat().st_size == 0 for path in (tmp_path / "ring").glob("gw-*.jsonl"))


@pytest.mark.hbt
def test_local_bus_ring_full(tmp_path: Path) -> None:
    bus = LocalBus(tmp_path / "ring")
    bus.publish(_cmd(1))
    bus.MAX_RING_BYTES = 1  # 实例覆盖：模拟 ring 触顶
    with pytest.raises(RingFull):
        bus.publish(_cmd(2))


@pytest.mark.hbt
def test_redis_bus_is_explicitly_unavailable() -> None:
    bus = get_bus("redis-stream", url="redis://localhost:6379/0")
    assert isinstance(bus, RedisBus)
    with pytest.raises(RuntimeError):
        bus.publish(_cmd(1))


@pytest.mark.hbt
def test_progress_log_uk_prefers_latest_seq(tmp_path: Path) -> None:
    """同一 (user, lesson) 的 seq_no 连续递增，审计可据此发现缺口。"""
    config = _seed(tmp_path)
    for _ in range(3):
        _record(config, position=12, played=10)
    engine = _engine(config)
    try:
        with Session(engine) as session:
            logs = list(
                session.execute(select(LearnProgressLog).order_by(LearnProgressLog.id)).scalars()
            )
        assert [log.seq_no for log in logs] == [1, 2, 3]
        assert [log.gw_seq for log in logs] == [1, 2, 3]
        assert logs[-1].position_bucket == 0
    finally:
        engine.dispose()


@pytest.mark.media
def test_key_session_bound_to_user_and_lesson(tmp_path: Path) -> None:
    config = _seed(tmp_path)
    client = _client(config)
    ticket = _ticket(client, _login(client, "student"), L_PUBLIC)
    engine = _engine(config)
    try:
        with Session(engine) as session:
            row = session.execute(select(MediaKeySession)).scalar_one()
        assert row.lesson_id == L_PUBLIC
        assert row.kid == KID1
        assert row.preview is False
        assert row.user_id == _user_id(config, "student")
        assert row.nonce == _signed(ticket)["n"]
        assert row.access_hash
    finally:
        engine.dispose()


@pytest.mark.media
def test_key_derivation_is_kid_bound(tmp_path: Path) -> None:
    config = _seed(tmp_path)
    secret = load_master_secret(config, Path(config.get_str("storage.local_dir")))
    assert derive_aes_key(secret, KID1) != derive_aes_key(secret, KID4)
    assert len(derive_aes_key(secret, KID1)) == 16


# ── HBT-001~005 验收（§16.4 压测窗口 M2.16 固化）────────────────────────
# HBT-002（崩溃重放）见 test_replay_unflushed_uses_db_log_when_ring_lost；
# HBT-003（单写者租约）见 test_flush_lease_is_single_writer。


@pytest.mark.hbt
def test_hbt001_two_gateways_same_user_converge_to_max(tmp_path: Path) -> None:
    """HBT-001：双实例/双 worker 并发上报同一用户同一课时，最终取最大值。"""
    config = _seed(tmp_path)
    student = _user_id(config, "student")

    def report(position: int) -> None:
        ProgressGateway(config).record(
            user_id=student,
            lesson_id=L_PUBLIC,
            body={
                "lesson_id": L_PUBLIC,
                "position_sec": position,
                "played_seconds": 10,
                "speed": 1.0,
                "client_ts_ms": 0,
            },
            can_preview=False,
        )

    report(30)
    report(130)
    assert _progress_row(config) is None

    assert ProgressFlush(config).flush_once() == 2
    row = _progress_row(config)
    assert row is not None
    assert row.watch_seconds == 20
    assert row.last_position == 130

    engine = _engine(config)
    try:
        with Session(engine) as session:
            seqs = [
                int(value)
                for value in session.execute(
                    select(LearnProgressLog.seq_no).order_by(LearnProgressLog.id)
                ).scalars()
            ]
            gw = [
                int(value)
                for value in session.execute(
                    select(LearnProgressLog.gw_seq).order_by(LearnProgressLog.gw_seq)
                ).scalars()
            ]
        assert seqs == [1, 2]
        assert gw == sorted(gw) and gw[-1] - gw[0] + 1 == len(gw)
    finally:
        engine.dispose()


@pytest.mark.hbt
def test_hbt004_replayed_flush_is_idempotent(tmp_path: Path) -> None:
    """HBT-004：同一批 log 被重放（模拟并发 flush / 版本冲突），不翻倍、不报错、最终一致。"""
    config = _seed(tmp_path)
    _record(config, position=12, played=10)
    assert ProgressFlush(config).flush_once() == 1
    first = _progress_row(config)
    assert first is not None and first.watch_seconds == 10

    engine = _engine(config)
    try:
        with Session(engine) as session:
            session.execute(update(LearnProgressLog).values(flushed_at=None))
            session.commit()
    finally:
        engine.dispose()

    assert ProgressFlush(config).flush_once() == 1
    row = _progress_row(config)
    assert row is not None
    assert row.watch_seconds == 10  # 绝对值重算：重放不双计
    assert row.last_position == 12
    assert row.version == 2  # 覆盖写入一次，无回退


@pytest.mark.hbt
def test_hbt005_out_of_order_positions_keep_max(tmp_path: Path) -> None:
    """HBT-005：乱序到达（600 先于 300），位置与累计时长仍取最大/求和。"""
    config = _seed(tmp_path)
    _record(config, position=120, played=10)
    _record(config, position=30, played=10)
    assert ProgressFlush(config).flush_once() == 2

    row = _progress_row(config)
    assert row is not None
    assert row.last_position == 120
    assert row.watch_seconds == 20
