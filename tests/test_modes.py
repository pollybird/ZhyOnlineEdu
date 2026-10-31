"""M2.10~2.11 双模式（§9.3 / §9.4 / §9.5 / §9.6）。

验收口径：
1. **18 条决策表**（MODE-001~018）参数化钉死：每条规则都有可执行用例，未进表视为未实现；
2. **MODE-FLIP-001**：切换期间在途请求不半半——请求内只读一次 epoch，切换后下一个请求才读新值；
3. **切换向导**：前置预检（4 原条 + 有效票据 / preview 流水 / 能力位一致 + 自注册账号处置）、
   epoch 乐观锁、不改表结构、审计落 `sys_log_security`；
4. 切换后：模式不匹配的课程在播放 / 进度 / 测验三条链路统一 403，存量票据不失效。
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import Engine, select
from sqlalchemy.orm import Session
from zhyonlineedu.api.schemas import (
    ModeCountsOut,
    ModePrecheckOut,
    ModeSummaryOut,
    ModeSwitchOut,
)
from zhyonlineedu.app import create_app
from zhyonlineedu.core.auth.keys import clear_key_cache, ensure_keypair
from zhyonlineedu.core.auth.passwords import hash_password
from zhyonlineedu.core.capability import ProbeResult, ProbeState, StaticProbe
from zhyonlineedu.core.codes import (
    ACCESS_CLASS_REQUIRED,
    ACCESS_LEVEL_REQUIRED,
    COURSE_NOT_PUBLISHED,
    MODE_ACCESS_DENIED,
    MODE_COURSE_MISMATCH,
    MODE_SWITCH_BLOCKED,
    MODE_UNCHANGED,
    NOT_FOUND,
    Code,
)
from zhyonlineedu.core.config import Config, load_config
from zhyonlineedu.core.db import compat
from zhyonlineedu.core.db.engine import create_db_engine
from zhyonlineedu.core.db.migration import current_revision, upgrade_head
from zhyonlineedu.core.db.models.account import (
    AccountSession,
    AccountUser,
    SysLogSecurity,
    SysUserRole,
)
from zhyonlineedu.core.db.models.catalog import (
    EduCourse,
    EduCourseChapter,
    EduCourseLesson,
    EduSubject,
)
from zhyonlineedu.core.db.models.learn import LearnProgressLog, utcnow
from zhyonlineedu.core.db.models.media import MediaAsset
from zhyonlineedu.core.db.models.system import (
    SysCompatFlags,
    SysConfig,
    SysModeConfig,
    SysModeState,
)
from zhyonlineedu.core.db.models.users import EduClass, EduClassMember
from zhyonlineedu.core.mode import (
    DECISION_TABLE,
    AccessSpec,
    ModeContext,
    TicketSpec,
    decide_access,
    decide_ticket,
)

PASSWORD = "S3cret-pass"  # noqa: S105 - 测试用固定口令
READY = ProbeResult(ProbeState.READY, flavor="sqlite", capabilities={"returning": True})

L_PUBLIC = 1  # mode=both / published / public，资产 encoded（KID1，360+720）
L_EXTERNAL = 2  # mode=external / published / public
L_INTERNAL = 3  # mode=internal / published / public
L_CLASS = 4  # mode=both / published / class（student 是成员、outsider 不是）
L_DRAFT = 5  # mode=both / draft / public（管理员可预览）
L_ARCHIVED = 6  # mode=both / archived / public

KID1 = "b" * 32
KID2 = "c" * 32
KID3 = "d" * 32
KID4 = "e" * 32
KID5 = "f" * 32
KID6 = "a" * 32


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
        "ZHY_DB_URL": f"sqlite:///{tmp_path / 'modes.db'}",
        "ZHY_DB_FLAVOR": "sqlite",
        "ZHY_JWT_PRIVATE_KEY_PATH": str(tmp_path / "keys" / "jwt_private.pem"),
        "ZHY_JWT_PUBLIC_KEY_PATH": str(tmp_path / "keys" / "jwt_public.pem"),
        "ZHY_STORAGE_LOCAL_DIR": str(tmp_path / "storage"),
        "ZHY_PROGRESS_RING_DIR": str(tmp_path / "ring"),
        "ZHY_MODE_CACHE_TTL_SEC": "30",
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
            (directory / name).write_bytes(b"ts-" + profile.encode())
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
            users: dict[str, int] = {}
            for username, role, registration in (
                ("root", "super_admin", "admin"),
                ("admin", "admin", "admin"),
                ("student", "student", "admin"),
                ("outsider", "student", "admin"),
                ("selfuser", "student", "self"),
            ):
                user = AccountUser(
                    username=username,
                    password_hash=hash_password(PASSWORD),
                    status="active",
                    token_version=1,
                    registration_mode=registration,
                    created_at=now,
                    updated_at=now,
                )
                session.add(user)
                session.flush()
                session.add(SysUserRole(user_id=user.id, role_code=role))
                users[username] = int(user.id)

            subject = EduSubject(name="数学", sort_order=1024, is_enabled=True)
            session.add(subject)
            session.flush()

            def add_course(
                title: str, *, status: str, access_type: str, mode: str = "both"
            ) -> EduCourse:
                row = EduCourse(
                    subject_id=subject.id,
                    title=title,
                    mode=mode,
                    access_type=access_type,
                    status=status,
                    sort_order=1024,
                )
                session.add(row)
                session.flush()
                return row

            def add_lesson(course: EduCourse, lesson_id: int, kid: str) -> None:
                chapter = EduCourseChapter(
                    course_id=course.id, title=f"章{lesson_id}", sort_order=1024
                )
                session.add(chapter)
                session.flush()
                session.add(
                    EduCourseLesson(
                        id=lesson_id,
                        chapter_id=chapter.id,
                        course_id=course.id,
                        title=f"课{lesson_id}",
                        kind="video",
                        duration_sec=100,
                        is_free=False,
                        sort_order=1024,
                    )
                )
                session.add(
                    MediaAsset(
                        lesson_id=lesson_id,
                        status="encoded",
                        playlist_version=1,
                        duration_sec=100,
                        kid=kid,
                        profiles=json.dumps(["360", "720"]),
                        created_at=now,
                        updated_at=now,
                    )
                )

            add_lesson(
                add_course("公开课", status="published", access_type="public"), L_PUBLIC, KID1
            )
            add_lesson(
                add_course("对外课", status="published", access_type="public", mode="external"),
                L_EXTERNAL,
                KID2,
            )
            add_lesson(
                add_course("对内课", status="published", access_type="public", mode="internal"),
                L_INTERNAL,
                KID3,
            )
            add_lesson(add_course("班级课", status="published", access_type="class"), L_CLASS, KID4)
            add_lesson(add_course("草稿课", status="draft", access_type="public"), L_DRAFT, KID5)
            add_lesson(
                add_course("归档课", status="archived", access_type="public"), L_ARCHIVED, KID6
            )

            klass = EduClass(name="一班", sort_order=1024, created_at=now, updated_at=now)
            session.add(klass)
            session.flush()
            session.add(EduClassMember(class_id=klass.id, user_id=users["student"], created_at=now))
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
            return int(
                session.execute(
                    select(AccountUser.id).where(AccountUser.username == username)
                ).scalar_one()
            )
    finally:
        engine.dispose()


def _set_mode_state(config: Config, mode: str, *, epoch: int = 1) -> None:
    """直接改库模拟"另一个进程已完成切换"（不经过服务层，不 invalidate 缓存）。"""
    engine = _engine(config)
    try:
        with Session(engine) as session:
            row = session.get(SysConfig, "install.mode")
            if row is None:
                session.add(SysConfig(config_key="install.mode", value=mode, updated_at=utcnow()))
            else:
                row.value = mode
            state = session.get(SysModeState, 1)
            if state is None:
                session.add(
                    SysModeState(
                        id=1,
                        active_mode=mode,
                        epoch=epoch,
                        version=1,
                        updated_at=utcnow(),
                    )
                )
            else:
                state.active_mode = mode
                state.epoch = epoch
                state.version = int(state.version) + 1
                state.updated_at = utcnow()
            session.commit()
    finally:
        engine.dispose()


def _progress(client: Any, headers: dict[str, str], lesson_id: int) -> Any:
    return client.post(
        "/api/v1/learn/progress",
        json={"lesson_id": lesson_id, "position_sec": 10, "played_seconds": 5, "speed": 1.0},
        headers=headers,
    )


# ── 1. 18 条决策表（MODE-001~018）─────────────────────────────────────────

ACCESS_CASES: tuple[tuple[str, AccessSpec, bool, str | None, str | None], ...] = (
    (
        "MODE-001",
        AccessSpec(
            mode="internal", course_mode="both", course_status="published", access_type="public"
        ),
        True,
        None,
        "MODE-001",
    ),
    (
        "MODE-002",
        AccessSpec(
            mode="internal", course_mode="both", course_status="published", access_type="member"
        ),
        False,
        ACCESS_LEVEL_REQUIRED,
        "MODE-002",
    ),
    (
        "MODE-003",
        AccessSpec(
            mode="internal",
            course_mode="both",
            course_status="published",
            access_type="class",
            class_member=True,
        ),
        True,
        None,
        "MODE-003",
    ),
    (
        "MODE-004",
        AccessSpec(
            mode="internal",
            course_mode="both",
            course_status="published",
            access_type="class",
            class_member=False,
        ),
        False,
        ACCESS_CLASS_REQUIRED,
        "MODE-004",
    ),
    (
        "MODE-005",
        AccessSpec(
            mode="external", course_mode="both", course_status="published", access_type="public"
        ),
        True,
        None,
        "MODE-005",
    ),
    (
        "MODE-006",
        AccessSpec(
            mode="external",
            course_mode="both",
            course_status="published",
            access_type="member",
            member_level=2,
            required_level=1,
        ),
        True,
        None,
        "MODE-006",
    ),
    (
        "MODE-007",
        AccessSpec(
            mode="external",
            course_mode="both",
            course_status="published",
            access_type="class",
            class_member=True,
        ),
        False,
        MODE_ACCESS_DENIED,
        "MODE-007",
    ),
    (
        "MODE-008",
        AccessSpec(
            mode="internal",
            course_mode="external",
            course_status="published",
            access_type="public",
        ),
        False,
        MODE_COURSE_MISMATCH,
        "MODE-008",
    ),
    (
        "MODE-009",
        AccessSpec(
            mode="external",
            course_mode="internal",
            course_status="published",
            access_type="public",
        ),
        False,
        MODE_COURSE_MISMATCH,
        "MODE-009",
    ),
    (
        "MODE-010",
        AccessSpec(
            mode="internal", course_mode="both", course_status="published", access_type="public"
        ),
        True,
        None,
        None,
    ),
    (
        "MODE-011",
        AccessSpec(
            mode="external", course_mode="both", course_status="published", access_type="public"
        ),
        True,
        None,
        None,
    ),
    (
        "MODE-012",
        AccessSpec(
            mode="internal", course_mode="both", course_status="draft", access_type="public"
        ),
        False,
        COURSE_NOT_PUBLISHED,
        "MODE-012",
    ),
    (
        "MODE-013",
        AccessSpec(
            mode="internal", course_mode="both", course_status="archived", access_type="public"
        ),
        False,
        NOT_FOUND,
        "MODE-013",
    ),
    (
        "MODE-014",
        AccessSpec(
            mode="internal",
            course_mode="both",
            course_status="draft",
            access_type="public",
            can_preview=True,
        ),
        True,
        None,
        "MODE-014",
    ),
)

TICKET_CASES: tuple[tuple[str, TicketSpec, bool, Any, str | None], ...] = (
    ("MODE-015", TicketSpec(), True, None, "MODE-015"),
    (
        "MODE-016",
        TicketSpec(nonce_state="replay"),
        False,
        Code.MEDIA_TICKET_REPLAY,
        "MODE-016",
    ),
    (
        "MODE-017",
        TicketSpec(mode="external", allowed_profiles=("360", "720")),
        True,
        None,
        None,
    ),
    ("MODE-018", TicketSpec(preview=True), True, None, "MODE-018"),
)


def test_decision_table_has_eighteen_rows() -> None:
    rules = [row.rule for row in DECISION_TABLE]
    assert len(rules) == 18
    assert len(set(rules)) == 18
    assert set(rules) == {f"MODE-{index:03d}" for index in range(1, 19)}


@pytest.mark.parametrize(
    ("rule", "spec", "allowed", "code", "hit"), ACCESS_CASES, ids=[case[0] for case in ACCESS_CASES]
)
def test_mode_access_rules(
    rule: str, spec: AccessSpec, allowed: bool, code: str | None, hit: str | None
) -> None:
    decision = decide_access(spec)
    assert decision.allowed is allowed, rule
    assert decision.code == code, rule
    if hit is not None:
        assert decision.rule == hit, rule


@pytest.mark.parametrize(
    ("rule", "spec", "allowed", "code", "hit"),
    TICKET_CASES,
    ids=[case[0] for case in TICKET_CASES],
)
def test_mode_ticket_rules(
    rule: str, spec: TicketSpec, allowed: bool, code: Any, hit: str | None
) -> None:
    decision = decide_ticket(spec)
    assert decision.allowed is allowed, rule
    assert decision.code == code, rule
    if hit is not None:
        assert decision.rule == hit, rule


def test_mode_017_profile_is_cropped_server_side() -> None:
    """外部模式：只授权 720 时 master 里没有 1080，直接要 1080 即 40203。"""
    cropped = decide_ticket(
        TicketSpec(mode="external", requested_profile=None, allowed_profiles=("360", "720"))
    )
    assert cropped.allowed is True
    assert "1080" not in cropped.profiles

    denied = decide_ticket(
        TicketSpec(mode="external", requested_profile="1080", allowed_profiles=("360", "720"))
    )
    assert denied.allowed is False
    assert denied.code == Code.MEDIA_TICKET_MISMATCH
    assert denied.rule == "MODE-017"


def test_mode_018_preview_ticket_keeps_flag() -> None:
    decision = decide_ticket(TicketSpec(preview=True))
    assert decision.allowed is True
    assert decision.preview is True
    assert decision.rule == "MODE-018"


# ── 2. MODE-FLIP-001：请求内只读一次 ──────────────────────────────────────


def test_mode_flip_during_request_keeps_old_epoch(tmp_path: Path) -> None:
    """请求 A 读到 epoch=1 后，别处切到 epoch=2；A 仍用 1 完成，下一个请求读到 2。"""
    config = _seed(tmp_path)
    app = create_app(config=config, probe=StaticProbe(READY))
    context: ModeContext = app.extensions["zhy_mode"]

    with app.test_request_context(f"/api/v1/lessons/{L_PUBLIC}/quiz"):
        first = context.snapshot()
        assert (first.mode, first.epoch) == ("internal", 1)
        _set_mode_state(config, "external", epoch=2)
        second = context.snapshot()
        assert second == first  # 同一请求内只读一次（不半半）

    with app.test_request_context(f"/api/v1/lessons/{L_PUBLIC}/quiz"):
        assert context.snapshot().epoch == 2


def test_mode_process_cache_ttl_then_invalidate(tmp_path: Path) -> None:
    """无请求上下文时按 `mode.cache_ttl_sec` 命中缓存；invalidate 立即读新值。"""
    config = _seed(tmp_path)
    context = ModeContext(config)
    assert context.snapshot().epoch == 1

    _set_mode_state(config, "external", epoch=2)
    assert context.snapshot().epoch == 1  # TTL 内不重读

    context.invalidate()
    assert context.snapshot().epoch == 2


def test_inflight_ticket_survives_flip(tmp_path: Path) -> None:
    """切换前签发的票据在切换后继续可用（存量 ksession 不失效）。"""
    config = _seed(tmp_path)
    client = _client(config)
    student = _login(client, "student")
    issued = client.post("/api/v1/media/tickets", json={"lesson_id": L_PUBLIC}, headers=student)
    assert issued.status_code == 200, issued.get_json()
    ticket = issued.get_json()["data"]

    root = _login(client, "root")
    switched = client.post(
        "/api/v1/system/mode/switch",
        json={"target_mode": "external", "epoch": 1},
        headers=root,
    )
    assert switched.status_code == 200, switched.get_json()

    master = client.get(str(ticket["playlist_url"]))
    assert master.status_code == 200, master.get_json()
    assert "360/index.m3u8" in master.get_data(as_text=True)


def test_master_variant_urls_survive_abr_switch(tmp_path: Path) -> None:
    """master 下发的各档位 URI 在票据有效期内可复用。

    hls.js / 原生 HLS 的 ABR 切换直接使用 master 里的变体 URI，不会重新拉 master；
    旧实现每次 playlist 请求都轮换 nonce，第二次取变体必然 40201 `playlist_stale`，
    表现为播放几秒后「网络连接失败」。
    """
    config = _seed(tmp_path)
    client = _client(config)
    student = _login(client, "student")
    issued = client.post("/api/v1/media/tickets", json={"lesson_id": L_PUBLIC}, headers=student)
    assert issued.status_code == 200, issued.get_json()
    master_url = str(issued.get_json()["data"]["playlist_url"])

    master = client.get(master_url)
    assert master.status_code == 200, master.get_json()
    variants = [
        line
        for line in master.get_data(as_text=True).splitlines()
        if line != "" and not line.startswith("#")
    ]
    assert len(variants) == 2

    first = client.get(variants[0])
    assert first.status_code == 200, first.get_json()
    # 模拟 ABR 切档：仍用 master 里下发的另一个变体 URI
    second = client.get(variants[1])
    assert second.status_code == 200, second.get_json()
    # master 本身也可重复拉取（播放器网络错误重试路径）
    assert client.get(master_url).status_code == 200

    key_line = next(
        line for line in first.get_data(as_text=True).splitlines() if line.startswith("#EXT-X-KEY")
    )
    key_url = key_line.split('URI="', 1)[1].split('"', 1)[0]
    # nonce 复用窗口内允许播放器多次取 key（切档后 key URI 字节相同）
    assert client.get(key_url).status_code == 200
    assert client.get(key_url).status_code == 200


# ── 3. 切换向导 ──────────────────────────────────────────────────────────


def test_mode_wizard_requires_system_manage(tmp_path: Path) -> None:
    client = _client(_seed(tmp_path))
    student = _login(client, "student")

    assert client.get("/api/v1/system/mode").status_code == 401
    for method, path, body in (
        ("get", "/api/v1/system/mode", None),
        ("post", "/api/v1/system/mode/precheck", {"target_mode": "external"}),
        ("post", "/api/v1/system/mode/switch", {"target_mode": "external", "epoch": 1}),
    ):
        response = getattr(client, method)(path, json=body, headers=student)
        assert response.status_code == 403, response.get_json()
        assert response.get_json()["code"] == "PERMISSION_DENIED"


def test_mode_state_shape_and_defaults(tmp_path: Path) -> None:
    client = _client(_seed(tmp_path))
    root = _login(client, "root")
    data = client.get("/api/v1/system/mode", headers=root).get_json()["data"]
    assert set(data) == set(ModeSummaryOut.model_fields)
    assert data["mode"] == "internal"
    assert data["epoch"] == 1
    assert data["other_mode"] == "external"
    assert data["previous_mode"] is None
    assert data["migration_batch_id"] is None
    assert set(data["counts"]) == set(ModeCountsOut.model_fields)
    assert data["policy"]["preview_seconds"] == 0


def test_mode_precheck_reports_checks_and_blockers(tmp_path: Path) -> None:
    client = _client(_seed(tmp_path))
    root = _login(client, "root")

    same = client.post(
        "/api/v1/system/mode/precheck", json={"target_mode": "internal"}, headers=root
    )
    assert same.status_code == 409
    assert same.get_json()["code"] == MODE_UNCHANGED

    invalid = client.post(
        "/api/v1/system/mode/precheck", json={"target_mode": "internal2"}, headers=root
    )
    assert invalid.status_code == 422

    data = client.post(
        "/api/v1/system/mode/precheck", json={"target_mode": "external"}, headers=root
    ).get_json()["data"]
    assert set(data) == set(ModePrecheckOut.model_fields)
    assert data["ready"] is True
    assert data["blockers"] == []
    codes = {item["code"] for item in data["checks"]}
    assert codes == {
        "mode_supported",
        "transcode_idle",
        "quiz_idle",
        "course_visibility",
        "active_tickets",
        "preview_pollution",
        "compat_flags",
        "self_registered",
    }
    assert data["counts"]["courses_hidden"] == 1  # 对内课在对外模式不可见
    assert data["counts"]["affected_users"] == 0  # 切到对外不停用自注册账号


def test_mode_switch_epoch_lock_and_schema_unchanged(tmp_path: Path) -> None:
    config = _seed(tmp_path)
    client = _client(config)
    root = _login(client, "root")
    engine = _engine(config)
    try:
        revision = current_revision(engine)
    finally:
        engine.dispose()

    first = client.post(
        "/api/v1/system/mode/switch",
        json={"target_mode": "external", "epoch": 1, "reason": "暑期对外"},
        headers=root,
    )
    assert first.status_code == 200, first.get_json()
    body = first.get_json()["data"]
    assert set(body) == set(ModeSwitchOut.model_fields)
    assert body["mode"] == "external"
    assert body["epoch"] == 2
    assert body["previous_mode"] == "internal"
    assert body["switched_by"] is not None
    assert body["reason"] == "暑期对外"

    stale = client.post(
        "/api/v1/system/mode/switch",
        json={"target_mode": "internal", "epoch": 1},
        headers=root,
    )
    assert stale.status_code == 409
    assert stale.get_json()["code"] == MODE_SWITCH_BLOCKED
    assert stale.get_json()["data"]["reason"] == "epoch_conflict"
    assert stale.get_json()["data"]["epoch"] == 2

    back = client.post(
        "/api/v1/system/mode/switch",
        json={"target_mode": "internal", "epoch": 2},
        headers=root,
    )
    assert back.status_code == 200, back.get_json()
    assert back.get_json()["data"]["epoch"] == 3

    # 切换不改表结构（R5）：迁移版本与 sys_install_state.schema_rev 均不变
    engine = _engine(config)
    try:
        assert current_revision(engine) == revision
        with Session(engine) as session:
            assert session.get(SysConfig, "install.mode").value == "internal"  # type: ignore[union-attr]
            logs = session.execute(select(SysLogSecurity.event)).scalars().all()
    finally:
        engine.dispose()
    assert logs.count("MODE_SWITCH") == 2


def test_mode_switch_quarantines_preview_and_disables_self_registered(tmp_path: Path) -> None:
    config = _seed(tmp_path)
    _set_mode_state(config, "external", epoch=1)
    client = _client(config)
    root = _login(client, "root")
    student = _login(client, "student")
    selfuser = _login(client, "selfuser")
    selfuser_id = _user_id(config, "selfuser")

    # 切换窗口内的 preview 流水（管理员预览草稿课）+ 正常 play 流水
    preview = _progress(client, root, L_DRAFT)
    assert preview.status_code == 202, preview.get_json()
    play = _progress(client, student, L_PUBLIC)
    assert play.status_code == 202, play.get_json()

    switched = client.post(
        "/api/v1/system/mode/switch",
        json={"target_mode": "internal", "epoch": 1},
        headers=root,
    )
    assert switched.status_code == 200, switched.get_json()
    report = switched.get_json()["data"]
    assert report["epoch"] == 2
    assert report["counts"]["quarantined_logs"] == 1
    assert report["counts"]["affected_users"] == 1
    assert report["counts"]["revoked_sessions"] >= 1
    batch = report["migration_batch_id"]
    assert batch and batch.startswith("FLP")

    engine = _engine(config)
    try:
        with Session(engine) as session:
            sources = {
                row.source: bool(row.quarantined)
                for row in session.execute(select(LearnProgressLog)).scalars()
            }
            assert sources == {"preview": True, "play": False}

            user = session.get(AccountUser, selfuser_id)
            assert user is not None
            assert user.status == "disabled"
            assert user.token_version == 2
            assert user.migration_batch_id == batch

            sessions = (
                session.execute(select(AccountSession).where(AccountSession.user_id == selfuser_id))
                .scalars()
                .all()
            )
            assert sessions and all(row.revoked_at is not None for row in sessions)
            assert {row.revoke_reason for row in sessions} == {"mode_switch"}

            events = session.execute(select(SysLogSecurity.event)).scalars().all()
            assert "MODE_SWITCH" in events
            assert "MODE_SWITCH_ACCOUNT" in events
    finally:
        engine.dispose()

    # 被停用的自注册账号无法再用旧 AT 访问
    denied = client.get("/api/v1/system/mode", headers=selfuser)
    assert denied.status_code == 401, denied.get_json()


def test_mode_switch_blocked_by_compat_flags_drift(tmp_path: Path) -> None:
    config = _seed(tmp_path)
    engine = _engine(config)
    try:
        with Session(engine) as session:
            session.add(
                SysCompatFlags(
                    cap="returning",
                    flavor="sqlite",
                    value=1,
                    probed_at="2026-10-30T00:00:00",
                    detail="",
                )
            )
            session.add(
                SysModeConfig(
                    mode="internal",
                    config_key="active",
                    value="1",
                    compat_flags=json.dumps({"returning": False}),
                )
            )
            session.commit()
    finally:
        engine.dispose()

    client = _client(config)
    root = _login(client, "root")
    checked = client.post(
        "/api/v1/system/mode/precheck", json={"target_mode": "external"}, headers=root
    ).get_json()["data"]
    assert checked["ready"] is False
    assert checked["blockers"] == ["compat_flags"]

    blocked = client.post(
        "/api/v1/system/mode/switch",
        json={"target_mode": "external", "epoch": 1},
        headers=root,
    )
    assert blocked.status_code == 409
    assert blocked.get_json()["code"] == MODE_SWITCH_BLOCKED
    assert blocked.get_json()["data"]["blockers"] == ["compat_flags"]


# ── 4. 切换后的访问口径 ──────────────────────────────────────────────────


def test_mode_mismatch_denies_playback_progress_and_quiz(tmp_path: Path) -> None:
    config = _seed(tmp_path)
    client = _client(config)
    root = _login(client, "root")
    student = _login(client, "student")
    switched = client.post(
        "/api/v1/system/mode/switch",
        json={"target_mode": "external", "epoch": 1},
        headers=root,
    )
    assert switched.status_code == 200, switched.get_json()

    ticket = client.post("/api/v1/media/tickets", json={"lesson_id": L_INTERNAL}, headers=student)
    assert ticket.status_code == 403
    assert ticket.get_json()["code"] == MODE_COURSE_MISMATCH

    progress = _progress(client, student, L_INTERNAL)
    assert progress.status_code == 403
    assert progress.get_json()["code"] == MODE_COURSE_MISMATCH

    quiz = client.get(f"/api/v1/lessons/{L_INTERNAL}/quiz", headers=student)
    assert quiz.status_code == 403
    assert quiz.get_json()["code"] == MODE_COURSE_MISMATCH

    # 对外模式不启用班级：班级课即便有成员关系也拒绝
    class_ticket = client.post(
        "/api/v1/media/tickets", json={"lesson_id": L_CLASS}, headers=student
    )
    assert class_ticket.status_code == 403
    assert class_ticket.get_json()["code"] == MODE_ACCESS_DENIED

    # 对外课放行；管理员仍可跨模式预览
    external_ticket = client.post(
        "/api/v1/media/tickets", json={"lesson_id": L_EXTERNAL}, headers=student
    )
    assert external_ticket.status_code == 200, external_ticket.get_json()
    preview = client.post("/api/v1/media/tickets", json={"lesson_id": L_INTERNAL}, headers=root)
    assert preview.status_code == 200, preview.get_json()
    assert preview.get_json()["data"]["preview"] is True


def test_internal_mode_rejects_external_course(tmp_path: Path) -> None:
    client = _client(_seed(tmp_path))
    student = _login(client, "student")
    ticket = client.post("/api/v1/media/tickets", json={"lesson_id": L_EXTERNAL}, headers=student)
    assert ticket.status_code == 403
    assert ticket.get_json()["code"] == MODE_COURSE_MISMATCH
    assert ticket.get_json()["data"]["mode"] == "internal"
    assert ticket.get_json()["data"]["epoch"] == 1
    assert _progress(client, student, L_EXTERNAL).get_json()["code"] == MODE_COURSE_MISMATCH


def test_archived_and_draft_still_follow_old_codes(tmp_path: Path) -> None:
    client = _client(_seed(tmp_path))
    student = _login(client, "student")
    root = _login(client, "root")
    archived = client.post("/api/v1/media/tickets", json={"lesson_id": L_ARCHIVED}, headers=student)
    assert archived.status_code == 404
    assert archived.get_json()["code"] == NOT_FOUND
    draft = client.get(f"/api/v1/lessons/{L_DRAFT}/quiz", headers=student)
    assert draft.status_code == 403
    assert draft.get_json()["code"] == COURSE_NOT_PUBLISHED
    assert client.get(f"/api/v1/lessons/{L_DRAFT}/quiz", headers=root).status_code == 404


def test_mode_state_uses_install_mode_when_row_missing(tmp_path: Path) -> None:
    config = _seed(tmp_path)
    engine = _engine(config)
    try:
        with Session(engine) as session:
            session.add(
                SysConfig(config_key="install.mode", value="external", updated_at=datetime.now(UTC))
            )
            session.commit()
    finally:
        engine.dispose()

    client = _client(config)
    root = _login(client, "root")
    data = client.get("/api/v1/system/mode", headers=root).get_json()["data"]
    assert data["mode"] == "external"
    assert data["epoch"] == 1

    engine = _engine(config)
    try:
        with Session(engine) as session:
            assert session.get(SysModeState, 1).active_mode == "external"  # type: ignore[union-attr]
    finally:
        engine.dispose()


def test_preview_log_window_is_bounded(tmp_path: Path) -> None:
    """quarantine 窗口外的 preview 流水不隔离（避免误挡历史数据）。"""
    config = _seed(tmp_path, ZHY_MODE_QUARANTINE_WINDOW_SEC="60")
    _set_mode_state(config, "external", epoch=1)
    engine = _engine(config)
    try:
        with Session(engine) as session:
            session.add(
                LearnProgressLog(
                    user_id=1,
                    lesson_id=L_DRAFT,
                    course_id=1,
                    seq_no=1,
                    gw_seq=1,
                    position=10,
                    played_seconds=5,
                    speed_bps=100,
                    speed_time=0,
                    position_bucket=0,
                    source="preview",
                    write_mode="buffered",
                    payload_hash="0" * 64,
                    client_ts_ms=0,
                    received_at=datetime.now(UTC) - timedelta(hours=2),
                    flushed_at=None,
                )
            )
            session.commit()
    finally:
        engine.dispose()

    client = _client(config)
    root = _login(client, "root")
    report = client.post(
        "/api/v1/system/mode/switch",
        json={"target_mode": "internal", "epoch": 1},
        headers=root,
    ).get_json()["data"]
    assert report["counts"]["quarantined_logs"] == 0

    engine = _engine(config)
    try:
        with Session(engine) as session:
            rows = session.execute(select(LearnProgressLog)).scalars().all()
            assert all(row.quarantined is False for row in rows)
    finally:
        engine.dispose()
