"""M4.1 安全渗透自测（§13.3 · S-TRANSCODE / S-TICKET / S-PROGRESS）。

三组：
- SANDBOX-PEN：沙箱逃逸（argv 白名单、环境隔离、路径越界、命令注入、seccomp、真实 bwrap）；
- TICKET-PEN：票据重放（签名/串课/过期 ksession/日志与 Referer 泄漏）；
- PROGRESS-PEN：进度篡改（越界字段、伪造身份/来源、接口配额、投影不回退）。

本文件复用 `tests/test_progress.py` 的装配夹具，避免重复造数。
"""

from __future__ import annotations

import json
import logging
import shutil
import subprocess
from collections.abc import Iterator
from dataclasses import replace
from datetime import timedelta
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

import pytest
from sqlalchemy import select, update
from sqlalchemy.orm import Session
from zhyonlineedu.core.auth.keys import clear_key_cache
from zhyonlineedu.core.codes import Code
from zhyonlineedu.core.db import compat
from zhyonlineedu.core.db.models.learn import LearnProgress, LearnProgressLog, utcnow
from zhyonlineedu.core.db.models.media import MediaKeySession
from zhyonlineedu.core.media import runtime as runtime_module
from zhyonlineedu.core.media.keys import load_master_secret
from zhyonlineedu.core.media.runtime import (
    MAX_ENCODE_THREADS,
    KeyMaterial,
    RenderSpec,
    SandboxError,
    SandboxRunner,
    build_hls_command,
    encode_threads,
)
from zhyonlineedu.core.progress.flush import ProgressFlush

from tests.test_progress import (
    DURATION,
    KID1,
    L_OTHER,
    L_PUBLIC,
    _client,
    _engine,
    _login,
    _post_progress,
    _record,
    _seed,
    _signed,
    _ticket,
    _user_id,
)

REPO_ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture(autouse=True)
def _reset_state() -> Iterator[None]:
    compat.reset()
    clear_key_cache()
    yield
    compat.reset()
    clear_key_cache()


# ── A. 沙箱逃逸 ───────────────────────────────────────────────────────────


def _sandbox_runner(tmp_path: Path, *, bwrap: str | None = None) -> tuple[SandboxRunner, Path]:
    root = tmp_path / "sandbox"
    root.mkdir()
    fake = tmp_path / "bwrap"
    fake.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    fake.chmod(0o755)
    return SandboxRunner(backend="bwrap", allowed_roots=[root], bwrap_path=bwrap or str(fake)), root


def _sandbox_spec(
    root: Path,
    *,
    source: Path | None = None,
    out_dir: Path | None = None,
    key: KeyMaterial | None = None,
) -> RenderSpec:
    src = source or root / "input.mp4"
    if not src.parent.exists():
        src.parent.mkdir(parents=True)
    src.write_bytes(b"not-a-real-video")
    out = out_dir or root / "out"
    out.mkdir(parents=True, exist_ok=True)
    if key is None:
        key_path = root / "media.key"
        key_path.write_bytes(b"k" * 16)
        info = root / "media.info"
        info.write_text(f"k\n{key_path}\n", encoding="utf-8")
        key = KeyMaterial(kid="a" * 32, key_path=key_path, info_path=info, uri="/media.key")
    return RenderSpec(kind="hls", source=src, out_dir=out, segment_sec=4, duration_sec=60, key=key)


@pytest.mark.security
def test_sandbox_argv_denies_network_and_host_writes(tmp_path: Path) -> None:
    runner, root = _sandbox_runner(tmp_path)
    spec = _sandbox_spec(root)
    argv = runner._wrap(["/bin/true"], spec)

    for flag in (
        "--unshare-user",
        "--unshare-pid",
        "--unshare-net",
        "--unshare-ipc",
        "--unshare-uts",
        "--die-with-parent",
        "--new-session",
    ):
        assert flag in argv
    assert "--share-net" not in argv
    assert argv[argv.index("--uid") + 1] == "65534"
    assert argv[argv.index("--gid") + 1] == "65534"

    source_at = argv.index(str(spec.source))
    assert argv[source_at - 1] == "--ro-bind"
    assert argv[argv.index(str(spec.out_dir)) - 1] == "--bind"
    assert spec.key is not None
    for material in (spec.key.key_path, spec.key.info_path):
        assert argv[argv.index(str(material)) - 1] == "--ro-bind"
    assert "--bind" in argv and "--ro-bind" in argv


@pytest.mark.security
def test_sandbox_env_never_leaks_secrets(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    runner, _ = _sandbox_runner(tmp_path)
    monkeypatch.setenv("ZHY_DB_URL", "postgresql+psycopg://user:secret@db/zhy")
    monkeypatch.setenv("ZHY_JWT_PRIVATE_KEY_PATH", "/run/keys/private.pem")
    env = runner._env()
    assert env == {
        "PATH": "/usr/local/bin:/usr/bin:/bin",
        "LANG": "C.UTF-8",
        "HOME": "/tmp",  # noqa: S108 - 沙箱内私有命名空间，与 runtime._env 保持同源
    }
    assert all("secret" not in value and "ZHY_" not in key for key, value in env.items())


@pytest.mark.security
def test_sandbox_rejects_path_escape(tmp_path: Path) -> None:
    runner, root = _sandbox_runner(tmp_path)
    outside_source = tmp_path / "outside.mp4"
    outside_source.write_bytes(b"x")
    outside_out = tmp_path / "outside-out"
    outside_out.mkdir()
    outside_key = tmp_path / "outside.key"
    outside_key.write_bytes(b"k" * 16)
    outside_info = tmp_path / "outside.info"
    outside_info.write_text("k\n", encoding="utf-8")
    unsafe_source = root / "bad\nname.mp4"
    unsafe_source.write_bytes(b"x")

    cases: list[tuple[RenderSpec, str]] = [
        (_sandbox_spec(root, source=outside_source), "source_outside_allowed_root"),
        (_sandbox_spec(root, out_dir=outside_out), "output_outside_allowed_root"),
        (
            _sandbox_spec(
                root,
                key=KeyMaterial(
                    kid="b" * 32,
                    key_path=outside_key,
                    info_path=outside_info,
                    uri="/outside.key",
                ),
            ),
            "key_outside_allowed_root",
        ),
        (_sandbox_spec(root, source=unsafe_source), "unsafe_path"),
    ]
    for spec, reason in cases:
        with pytest.raises(SandboxError) as excinfo:
            runner.run(spec)
        assert excinfo.value.reason == reason


@pytest.mark.security
def test_sandbox_command_is_enum_derived_not_concatenated(tmp_path: Path) -> None:
    runner, root = _sandbox_runner(tmp_path)
    spec = _sandbox_spec(root)
    malicious = replace(spec, profile="720; rm -rf /")
    with pytest.raises(SandboxError) as excinfo:
        build_hls_command(malicious)
    assert excinfo.value.reason == "unknown_profile"

    assert encode_threads(1_000_000) == MAX_ENCODE_THREADS
    argv = build_hls_command(spec)
    assert all(isinstance(item, str) for item in argv)
    assert str(spec.source) in argv
    source = Path(runtime_module.__file__).read_text(encoding="utf-8")
    assert "shell=True" not in source
    assert "os.system" not in source


@pytest.mark.security
def test_container_seccomp_profile_is_enforced() -> None:
    compose = (REPO_ROOT / "docker-compose.yml").read_text(encoding="utf-8")
    assert "seccomp=./deploy/ffmpeg/seccomp.json" in compose
    profile = json.loads((REPO_ROOT / "deploy/ffmpeg/seccomp.json").read_text(encoding="utf-8"))
    assert profile["defaultAction"] == "SCMP_ACT_ERRNO"
    assert profile["defaultAction"] != "SCMP_ACT_ALLOW"


def _bwrap_or_skip() -> str:
    bwrap = shutil.which("bwrap")
    if bwrap is None:
        pytest.skip("本机未安装 bwrap")
    probe = subprocess.run(  # noqa: S603 - 常量 argv，无 shell
        [
            bwrap,
            "--unshare-user",
            "--unshare-pid",
            "--unshare-net",
            "--ro-bind",
            "/",
            "/",
            "--",
            "/bin/true",
        ],
        capture_output=True,
        check=False,
        timeout=15,
    )
    if probe.returncode != 0:
        pytest.skip(f"bwrap 不可用（unprivileged userns 被禁用）：{probe.stderr[-200:]!r}")
    return bwrap


@pytest.mark.security
def test_real_bwrap_blocks_host_files(tmp_path: Path) -> None:
    bwrap = _bwrap_or_skip()
    runner, root = _sandbox_runner(tmp_path, bwrap=bwrap)
    spec = _sandbox_spec(root)
    argv = runner._wrap(["/bin/sh", "-c", "cat /etc/passwd"], spec)
    completed = subprocess.run(  # noqa: S603 - argv 已由沙箱封装，无 shell 拼接
        argv,
        capture_output=True,
        check=False,
        text=True,
        timeout=20,
        env=runner._env(),
    )
    assert completed.returncode != 0 or "root:" not in completed.stdout


@pytest.mark.security
def test_real_bwrap_blocks_network(tmp_path: Path) -> None:
    bwrap = _bwrap_or_skip()
    python = "/usr/bin/python3"
    if not Path(python).exists():
        pytest.skip("本机缺少 /usr/bin/python3")
    runner, root = _sandbox_runner(tmp_path, bwrap=bwrap)
    spec = _sandbox_spec(root)
    code = (
        "import socket;"
        "socket.create_connection(('169.254.169.254',80),timeout=1);"
        "print('NETWORK_OK')"
    )
    argv = runner._wrap([python, "-c", code], spec)
    completed = subprocess.run(  # noqa: S603 - argv 已由沙箱封装，无 shell 拼接
        argv,
        capture_output=True,
        check=False,
        text=True,
        timeout=20,
        env=runner._env(),
    )
    assert completed.returncode != 0
    assert "NETWORK_OK" not in completed.stdout


# ── B. 票据重放 ───────────────────────────────────────────────────────────


def _key_url(client: Any, ticket: dict[str, Any]) -> str:
    master = client.get(str(ticket["playlist_url"]))
    assert master.status_code == 200, master.get_json()
    variant_url = next(
        line for line in master.get_data(as_text=True).splitlines() if "/360/" in line
    )
    variant = client.get(variant_url)
    assert variant.status_code == 200, variant.get_json()
    key_line = next(
        line
        for line in variant.get_data(as_text=True).splitlines()
        if line.startswith("#EXT-X-KEY")
    )
    return key_line.split('URI="', 1)[1].rstrip('"')


@pytest.mark.security
@pytest.mark.media
def test_key_material_absent_from_logs_and_referrer(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    config = _seed(tmp_path)
    client = _client(config)
    ticket = _ticket(client, _login(client, "student"), L_PUBLIC)
    args = _signed(ticket)
    key_url = _key_url(client, ticket)

    with caplog.at_level(logging.INFO):
        master = client.get(str(ticket["playlist_url"]))
        key = client.get(key_url)
        replay = client.get(key_url)
    assert master.headers["Referrer-Policy"] == "no-referrer"
    assert key.headers["Referrer-Policy"] == "no-referrer"
    assert master.headers["Cache-Control"].startswith("no-store")
    assert key.status_code == 200
    # 默认 30s 复用窗口内，播放器对同一 key URI 的重复请求允许（非重放攻击）
    assert replay.status_code == 200
    assert args["ks"] not in caplog.text
    assert args["sig"] not in caplog.text

    secret = load_master_secret(config, Path(config.get_str("storage.local_dir")))
    playlist_text = master.get_data(as_text=True)
    assert secret.hex() not in playlist_text
    assert secret not in key.get_data()
    assert len(key.get_data()) == 16


@pytest.mark.security
@pytest.mark.media
def test_key_replay_after_first_use_is_rejected(tmp_path: Path) -> None:
    config = _seed(tmp_path, ZHY_MEDIA_NONCE_REUSE_WINDOW_SEC="0")
    client = _client(config)
    ticket = _ticket(client, _login(client, "student"), L_PUBLIC)
    key_url = _key_url(client, ticket)

    assert client.get(key_url).status_code == 200
    for _ in range(2):
        replay = client.get(key_url)
        assert replay.status_code == 403
        body = replay.get_json()
        assert body["code"] == int(Code.MEDIA_TICKET_REPLAY)
        assert body["data"]["reason"] == "nonce_replay"
        assert len(replay.get_data()) < 512


@pytest.mark.security
@pytest.mark.media
def test_cross_ticket_key_url_is_rejected(tmp_path: Path) -> None:
    client = _client(_seed(tmp_path))
    headers = _login(client, "student")
    ticket_a = _ticket(client, headers, L_PUBLIC)
    ticket_b = _ticket(client, headers, L_OTHER)
    key_a = _key_url(client, ticket_a)
    query_a = parse_qs(urlparse(key_a).query)
    forged = {
        "ks": _signed(ticket_b)["ks"],
        "n": query_a["n"][0],
        "exp": query_a["exp"][0],
        "sig": query_a["sig"][0],
    }
    response = client.get(f"/api/v1/media/key/{KID1}", query_string=forged)
    assert response.status_code == 403
    body = response.get_json()
    assert body["code"] == int(Code.MEDIA_TICKET_MISMATCH)
    assert body["data"]["reason"] == "kid_not_in_session"


@pytest.mark.security
@pytest.mark.media
def test_expired_ksession_cannot_fetch_key(tmp_path: Path) -> None:
    config = _seed(tmp_path)
    client = _client(config)
    ticket = _ticket(client, _login(client, "student"), L_PUBLIC)
    key_url = _key_url(client, ticket)
    args = _signed(ticket)

    engine = _engine(config)
    try:
        with Session(engine) as session:
            session.execute(
                update(MediaKeySession)
                .where(MediaKeySession.ksession_id == args["ks"])
                .values(expires_at=utcnow() - timedelta(seconds=1))
            )
            session.commit()
    finally:
        engine.dispose()

    response = client.get(key_url)
    assert response.status_code == 401
    body = response.get_json()
    assert body["code"] == int(Code.MEDIA_TICKET_EXPIRED)
    assert body["data"]["reason"] == "session_expired"


# ── C. 进度篡改 ───────────────────────────────────────────────────────────


def _log_count(config: Any) -> int:
    engine = _engine(config)
    try:
        with Session(engine) as session:
            return len(list(session.execute(select(LearnProgressLog.id)).scalars()))
    finally:
        engine.dispose()


@pytest.mark.security
@pytest.mark.hbt
def test_progress_quota_rejects_third_report_per_minute(tmp_path: Path) -> None:
    config = _seed(tmp_path)
    client = _client(config)
    headers = _login(client, "student")
    assert _post_progress(client, headers, position_sec=5).status_code == 202
    assert _post_progress(client, headers, position_sec=15).status_code == 202

    third = _post_progress(client, headers, position_sec=25)
    assert third.status_code == 429
    body = third.get_json()
    assert body["code"] == int(Code.RATE_LIMITED)
    assert body["data"]["scope"] == "progress:upsert"
    assert 1 <= body["data"]["retry_after"] <= 60
    assert _log_count(config) == 2

    engine = _engine(config)
    try:
        with Session(engine) as session:
            assert list(session.execute(select(LearnProgress)).scalars()) == []
    finally:
        engine.dispose()


@pytest.mark.security
@pytest.mark.hbt
def test_progress_quota_can_be_disabled(tmp_path: Path) -> None:
    config = _seed(tmp_path, ZHY_PROGRESS_RATE_LIMIT_PER_MIN="0")
    client = _client(config)
    headers = _login(client, "student")
    for position in (5, 15, 25, 35):
        assert _post_progress(client, headers, position_sec=position).status_code == 202
    assert _log_count(config) == 4


@pytest.mark.security
@pytest.mark.hbt
def test_progress_quota_is_scoped_per_lesson(tmp_path: Path) -> None:
    config = _seed(tmp_path)
    client = _client(config)
    headers = _login(client, "student")
    assert _post_progress(client, headers, position_sec=5).status_code == 202
    assert _post_progress(client, headers, position_sec=15).status_code == 202
    other = _post_progress(client, headers, lesson_id=L_OTHER, position_sec=5)
    assert other.status_code == 202
    assert _log_count(config) == 3


@pytest.mark.security
@pytest.mark.hbt
def test_progress_forged_identity_and_source_ignored(tmp_path: Path) -> None:
    config = _seed(tmp_path)
    client = _client(config)
    student_id = _user_id(config, "student")
    response = _post_progress(
        client,
        _login(client, "student"),
        position_sec=12,
        played_seconds=10,
        user_id=999,
        source="preview",
        gw_seq=999,
        seq_no=999,
    )
    assert response.status_code == 202
    body = response.get_json()["data"]
    assert body["gw_seq"] == 1

    engine = _engine(config)
    try:
        with Session(engine) as session:
            log = session.execute(select(LearnProgressLog)).scalar_one()
            assert (log.user_id, log.lesson_id, log.source) == (student_id, L_PUBLIC, "play")
            assert (log.gw_seq, log.seq_no) == (1, 1)
    finally:
        engine.dispose()


@pytest.mark.security
@pytest.mark.hbt
def test_progress_tampered_payload_leaves_no_log(tmp_path: Path) -> None:
    config = _seed(tmp_path)
    client = _client(config)
    headers = _login(client, "student")
    for body in (
        {"position_sec": DURATION + 100},
        {"position_sec": -1},
        {"played_seconds": 3601},
        {"speed": 4.0},
        {"client_ts_ms": "x"},
        {"lesson_id": True},
    ):
        response = _post_progress(client, headers, **body)
        assert response.status_code == 422, body
    assert _log_count(config) == 0


@pytest.mark.security
@pytest.mark.hbt
def test_direct_gateway_records_not_blocked_by_http_quota(tmp_path: Path) -> None:
    """配额是接口级（§13.3），SDK/内部直连不受影响。"""
    config = _seed(tmp_path)
    for _ in range(3):
        _record(config, position=12, played=10)
    assert _log_count(config) == 3


@pytest.mark.security
@pytest.mark.hbt
def test_low_position_report_cannot_reduce_watch_seconds(tmp_path: Path) -> None:
    config = _seed(tmp_path)
    _record(config, position=30, played=20)
    assert ProgressFlush(config).flush_once() == 1

    engine = _engine(config)
    try:
        with Session(engine) as session:
            session.execute(update(LearnProgress).values(watch_seconds=50))
            session.commit()
    finally:
        engine.dispose()

    _record(config, position=5, played=1)
    ProgressFlush(config).flush_once()

    engine = _engine(config)
    try:
        with Session(engine) as session:
            row = session.execute(select(LearnProgress)).scalar_one()
            assert row.watch_seconds == 50
    finally:
        engine.dispose()


@pytest.mark.security
@pytest.mark.media
def test_key_response_does_not_echo_kid_material_bytes(tmp_path: Path) -> None:
    """取 key 只返回 16 字节密钥；kid/ksession 不随响应体二次下发。"""
    config = _seed(tmp_path)
    client = _client(config)
    ticket = _ticket(client, _login(client, "student"), L_PUBLIC)
    key_url = _key_url(client, ticket)
    response = client.get(key_url)
    assert response.status_code == 200
    args = parse_qs(urlparse(key_url).query)
    text = response.get_data().decode("latin-1")
    assert args["ks"][0] not in text
    assert args["n"][0] not in text
    assert KID1 not in text
