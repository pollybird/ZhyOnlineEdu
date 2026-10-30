"""M2.7 用户 / 班级 / 导入 / 过期（§6.5 / §10.5）。

验收口径：
1. **越权** —— 13 个用户/班级/导入接口全部 `user:manage`；teacher / student 一律
   403 `PERMISSION_DENIED`，管理动作绝不被讲师资历或播放权限绕过；
2. **冲突不覆盖** —— 导入默认 `report_only`：username / (org_id,user_no) 冲突行只记
   `skipped`，原 display_name / password_hash / version 全部不变；显式 `overwrite` 才更新。

另覆盖：创建/列表/详情/乐观锁、停用与过期（登录/刷新/worker 三处口径）、
班级成员授权（退班 / 停班立即 40204 且再次取票 403）、Excel/CSV 文件导入与批次复核。
"""

from __future__ import annotations

import io
import json
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from openpyxl import Workbook
from sqlalchemy import Engine, select
from sqlalchemy.orm import Session
from zhyonlineedu.api.schemas import (
    ClassMembersOut,
    ClassOut,
    ImportBatchListOut,
    ImportBatchOut,
    UserListOut,
    UserOut,
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
from zhyonlineedu.core.db.models.account import AccountSession, AccountUser, SysUserRole
from zhyonlineedu.core.db.models.catalog import (
    EduCourse,
    EduCourseChapter,
    EduCourseLesson,
    EduSubject,
)
from zhyonlineedu.core.db.models.media import MediaAsset
from zhyonlineedu.core.db.models.quiz import QuizQuestion
from zhyonlineedu.core.db.models.users import EduClass, EduClassMember
from zhyonlineedu.workers.expire import expire_once

PASSWORD = "S3cret-pass"  # noqa: S105 - 测试用固定口令
NEW_PASSWORD = "N3w-secret-pass"  # noqa: S105 - 测试用覆盖口令
READY = ProbeResult(ProbeState.READY, flavor="sqlite", capabilities={"returning": True})

L_CLASS = 1  # published/class，资产 encoded（kid=KID），含 1 道题
KID = "c" * 32

# 13 个接口：全部要求 user:manage（teacher / student 逐一 403）
ENDPOINTS: tuple[tuple[str, str, dict[str, Any] | None], ...] = (
    ("get", "/api/v1/users", None),
    ("post", "/api/v1/users", {"username": "hack", "password": NEW_PASSWORD}),
    ("get", "/api/v1/users/1", None),
    ("patch", "/api/v1/users/1", {"version": 1}),
    ("post", "/api/v1/users/1/disable", {"version": 1}),
    ("post", "/api/v1/users/1/expire", {"version": 1}),
    ("get", "/api/v1/classes", None),
    ("post", "/api/v1/classes", {"name": "越权班"}),
    ("post", "/api/v1/classes/1/members", {"user_ids": [1]}),
    ("delete", "/api/v1/classes/1/members/1", None),
    (
        "post",
        "/api/v1/users/import",
        {"rows": [{"username": "hack", "password": NEW_PASSWORD}]},
    ),
    ("get", "/api/v1/users/imports", None),
    ("get", "/api/v1/users/imports/IMP-UNKNOWN", None),
)


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
        "ZHY_DB_URL": f"sqlite:///{tmp_path / 'users.db'}",
        "ZHY_DB_FLAVOR": "sqlite",
        "ZHY_JWT_PRIVATE_KEY_PATH": str(tmp_path / "keys" / "jwt_private.pem"),
        "ZHY_JWT_PUBLIC_KEY_PATH": str(tmp_path / "keys" / "jwt_public.pem"),
        "ZHY_STORAGE_LOCAL_DIR": str(tmp_path / "storage"),
        "ZHY_PROGRESS_RING_DIR": str(tmp_path / "ring"),
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
            now = datetime.now(UTC)
            users: dict[str, AccountUser] = {}
            for username, role in (
                ("root", "super_admin"),
                ("teacher", "teacher"),
                ("student", "student"),
                ("outsider", "student"),
                ("blocked", "student"),
            ):
                user = AccountUser(
                    org_id=1,
                    user_no=f"S{len(users) + 1:04d}",
                    username=username,
                    password_hash=hash_password(PASSWORD),
                    display_name="张三" if username == "student" else None,
                    status="active",
                    token_version=1,
                    version=1,
                    created_at=now,
                    updated_at=now,
                )
                session.add(user)
                session.flush()
                session.add(SysUserRole(user_id=user.id, role_code=role))
                users[username] = user

            subject = EduSubject(name="数学", sort_order=1024, is_enabled=True)
            session.add(subject)
            session.flush()
            course = EduCourse(
                subject_id=subject.id,
                title="班级课",
                mode="both",
                access_type="class",
                status="published",
                sort_order=1024,
            )
            session.add(course)
            session.flush()
            chapter = EduCourseChapter(course_id=course.id, title="第一章", sort_order=1024)
            session.add(chapter)
            session.flush()
            lesson = EduCourseLesson(
                id=L_CLASS,
                chapter_id=chapter.id,
                course_id=course.id,
                title="班级课第一讲",
                kind="video",
                duration_sec=100,
                is_free=False,
                sort_order=1024,
            )
            session.add(lesson)
            session.add(
                MediaAsset(
                    lesson_id=L_CLASS,
                    status="encoded",
                    playlist_version=1,
                    duration_sec=100,
                    kid=KID,
                    profiles=json.dumps(["360"]),
                    created_at=now,
                    updated_at=now,
                )
            )
            session.add(
                QuizQuestion(
                    course_id=course.id,
                    lesson_id=L_CLASS,
                    kind="single",
                    stem="1+1=?",
                    options=json.dumps(
                        [{"key": "A", "text": "2"}, {"key": "B", "text": "3"}],
                        ensure_ascii=False,
                    ),
                    answer=json.dumps(["A"]),
                    explanation=None,
                    score=5,
                    sort_order=1024,
                    is_enabled=True,
                    created_at=now,
                    updated_at=now,
                )
            )

            enabled_class = EduClass(
                org_id=1,
                name="一班",
                teacher_id=users["teacher"].id,
                is_enabled=True,
                sort_order=1024,
                version=1,
                created_at=now,
                updated_at=now,
            )
            disabled_class = EduClass(
                org_id=1,
                name="停用班",
                teacher_id=None,
                is_enabled=False,
                sort_order=2048,
                version=1,
                created_at=now,
                updated_at=now,
            )
            session.add_all([enabled_class, disabled_class])
            session.flush()
            session.add_all(
                [
                    EduClassMember(
                        class_id=enabled_class.id, user_id=users["student"].id, created_at=now
                    ),
                    EduClassMember(
                        class_id=disabled_class.id, user_id=users["outsider"].id, created_at=now
                    ),
                ]
            )
            session.commit()
    finally:
        engine.dispose()

    _write_hls(storage, L_CLASS, KID, ("360",))
    return config


def _engine(config: Config) -> Engine:
    return create_db_engine(config.get_str("db.url"), "sqlite")


def _client(config: Config) -> Any:
    return create_app(config=config, probe=StaticProbe(READY)).test_client()


def _login(client: Any, username: str, password: str = PASSWORD) -> dict[str, str]:
    response = client.post("/api/v1/auth/login", json={"username": username, "password": password})
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


def _user_row(config: Config, username: str) -> dict[str, Any]:
    engine = _engine(config)
    try:
        with Session(engine) as session:
            row = session.execute(
                select(AccountUser).where(AccountUser.username == username)
            ).scalar_one()
            return {
                "id": int(row.id),
                "user_no": str(row.user_no),
                "display_name": row.display_name,
                "password_hash": str(row.password_hash),
                "status": str(row.status),
                "version": int(row.version),
                "token_version": int(row.token_version),
            }
    finally:
        engine.dispose()


def _class_id(config: Config, name: str) -> int:
    engine = _engine(config)
    try:
        with Session(engine) as session:
            value = session.execute(select(EduClass.id).where(EduClass.name == name)).scalar_one()
        return int(value)
    finally:
        engine.dispose()


def _xlsx(headers: list[str], rows: list[list[object]]) -> bytes:
    workbook = Workbook()
    sheet = workbook.active
    sheet.append(headers)
    for row in rows:
        sheet.append(row)
    buffer = io.BytesIO()
    workbook.save(buffer)
    workbook.close()
    return buffer.getvalue()


def _csv(headers: list[str], rows: list[list[object]]) -> bytes:
    lines = [",".join(headers)]
    lines.extend(",".join(str(cell) for cell in row) for row in rows)
    return ("\n".join(lines) + "\n").encode("utf-8-sig")


def _assert_no_secret(value: object, path: str = "$") -> None:
    """逐行结果不得携带口令或哈希。"""
    if isinstance(value, dict):
        leaked = {"password", "password_hash", "hash"} & set(value)
        assert not leaked, f"{path} 泄露敏感字段 {leaked}"
        for key, item in value.items():
            _assert_no_secret(item, f"{path}.{key}")
    elif isinstance(value, list):
        for index, item in enumerate(value):
            _assert_no_secret(item, f"{path}[{index}]")


# ── 验收口径一：越权 ──────────────────────────────────────────────────────


@pytest.mark.parametrize("username", ["teacher", "student"])
def test_all_management_endpoints_deny_non_admin(tmp_path: Path, username: str) -> None:
    config = _seed(tmp_path)
    client = _client(config)
    headers = _login(client, username)
    for method, path, body in ENDPOINTS:
        response = client.open(path, method=method.upper(), json=body, headers=headers)
        assert response.status_code == 403, f"{method} {path} -> {response.get_json()}"
        assert response.get_json()["code"] == "PERMISSION_DENIED"
        assert response.get_json()["data"]["required"] == "user:manage"


def test_admin_role_can_manage_users(tmp_path: Path) -> None:
    config = _seed(tmp_path)
    engine = create_db_engine(config.get_str("db.url"), "sqlite")
    try:
        with Session(engine) as session:
            user = AccountUser(
                username="manager",
                password_hash=hash_password(PASSWORD),
                status="active",
                token_version=1,
                created_at=datetime.now(UTC),
                updated_at=datetime.now(UTC),
            )
            session.add(user)
            session.flush()
            session.add(SysUserRole(user_id=user.id, role_code="admin"))
            session.commit()
    finally:
        engine.dispose()
    client = _client(config)
    headers = _login(client, "manager")
    response = client.get("/api/v1/users", headers=headers)
    assert response.status_code == 200, response.get_json()
    assert response.get_json()["data"]["total"] >= 1


# ── 用户 CRUD / 乐观锁 ────────────────────────────────────────────────────


def test_create_list_and_validate_users(tmp_path: Path) -> None:
    config = _seed(tmp_path)
    client = _client(config)
    headers = _login(client, "root")

    created = client.post(
        "/api/v1/users",
        json={
            "username": "newteacher",
            "password": NEW_PASSWORD,
            "user_no": "T2001",
            "display_name": "李老师",
            "roles": ["teacher"],
            "expire_at": (datetime.now(UTC) + timedelta(days=30)).isoformat(timespec="seconds"),
        },
        headers=headers,
    )
    assert created.status_code == 201, created.get_json()
    data = created.get_json()["data"]
    assert set(data) == set(UserOut.model_fields)
    assert data["username"] == "newteacher"
    assert data["user_no"] == "T2001"
    assert data["roles"] == ["teacher"]
    assert data["status"] == "active"
    assert data["version"] == 1
    assert "password" not in data and "password_hash" not in data

    default_role = client.post(
        "/api/v1/users",
        json={"username": "plain", "password": NEW_PASSWORD},
        headers=headers,
    ).get_json()["data"]
    assert default_role["roles"] == ["student"]
    assert default_role["user_no"].startswith("U")

    listed = client.get("/api/v1/users?keyword=newteacher", headers=headers)
    body = listed.get_json()["data"]
    assert set(body) == set(UserListOut.model_fields)
    assert body["total"] == 1
    assert body["items"][0]["username"] == "newteacher"
    assert body["items"][0]["class_count"] == 0

    role_filtered = client.get("/api/v1/users?role=teacher", headers=headers).get_json()["data"]
    assert {"teacher", "newteacher"} <= {item["username"] for item in role_filtered["items"]}

    duplicate_name = client.post(
        "/api/v1/users", json={"username": "newteacher", "password": NEW_PASSWORD}, headers=headers
    )
    assert duplicate_name.status_code == 422
    assert duplicate_name.get_json()["code"] == "VALIDATION_FAILED"
    assert "用户名已存在" in duplicate_name.get_json()["data"]["errors"]

    duplicate_no = client.post(
        "/api/v1/users",
        json={"username": "another", "password": NEW_PASSWORD, "user_no": "T2001"},
        headers=headers,
    )
    assert duplicate_no.status_code == 422
    assert "学号/工号已存在" in duplicate_no.get_json()["data"]["errors"]

    for bad in (
        {"username": "x", "password": "short"},
        {"username": "x", "password": NEW_PASSWORD, "roles": ["ghost"]},
        {"username": "", "password": NEW_PASSWORD},
    ):
        response = client.post("/api/v1/users", json=bad, headers=headers)
        assert response.status_code == 422, bad
        assert response.get_json()["code"] == "VALIDATION_FAILED"

    missing = client.get("/api/v1/users/99999", headers=headers)
    assert missing.status_code == 404
    assert missing.get_json()["code"] == "NOT_FOUND"


def test_update_user_optimistic_lock_and_expire_at(tmp_path: Path) -> None:
    config = _seed(tmp_path)
    client = _client(config)
    headers = _login(client, "root")
    user_id = _user_id(config, "blocked")

    patched = client.patch(
        f"/api/v1/users/{user_id}",
        json={"version": 1, "display_name": "改名", "roles": ["teacher"]},
        headers=headers,
    )
    assert patched.status_code == 200, patched.get_json()
    data = patched.get_json()["data"]
    assert data["version"] == 2
    assert data["display_name"] == "改名"
    assert data["roles"] == ["teacher"]

    stale = client.patch(
        f"/api/v1/users/{user_id}", json={"version": 1, "display_name": "再改"}, headers=headers
    )
    assert stale.status_code == 409
    assert stale.get_json()["code"] == "CONFLICT_RETRY"

    past = (datetime.now(UTC) - timedelta(seconds=5)).isoformat(timespec="seconds")
    expired = client.patch(
        f"/api/v1/users/{user_id}",
        json={"version": 2, "expire_at": past},
        headers=headers,
    )
    assert expired.status_code == 200, expired.get_json()
    assert expired.get_json()["data"]["status"] == "expired"
    assert expired.get_json()["data"]["version"] == 3

    login = client.post("/api/v1/auth/login", json={"username": "blocked", "password": PASSWORD})
    assert login.status_code == 401
    assert login.get_json()["code"] == "ACCOUNT_EXPIRED"


def test_disable_user_revokes_sessions_and_blocks_login(tmp_path: Path) -> None:
    config = _seed(tmp_path)
    user_client = _client(config)
    _login(user_client, "outsider")
    admin = _client(config)
    headers = _login(admin, "root")
    user_id = _user_id(config, "outsider")
    version = _user_row(config, "outsider")["version"]

    disabled = admin.post(
        f"/api/v1/users/{user_id}/disable", json={"version": version}, headers=headers
    )
    assert disabled.status_code == 200, disabled.get_json()
    data = disabled.get_json()["data"]
    assert data["status"] == "disabled"
    assert data["version"] == version + 1

    again = admin.post(
        f"/api/v1/users/{user_id}/disable", json={"version": data["version"]}, headers=headers
    )
    assert again.status_code == 200
    assert again.get_json()["data"]["status"] == "disabled"
    assert again.get_json()["data"]["version"] == data["version"]  # 幂等：不再 bump

    refresh = user_client.post("/api/v1/auth/refresh")
    assert refresh.status_code == 401
    # 停用会先吊销全部会话，刷新时命中会话已吊销（早于 ACCOUNT_DISABLED 判定）
    assert refresh.get_json()["code"] == "AUTH_TOKEN_REVOKED"

    login = user_client.post(
        "/api/v1/auth/login", json={"username": "outsider", "password": PASSWORD}
    )
    assert login.status_code == 401
    assert login.get_json()["code"] == "ACCOUNT_DISABLED"

    engine = _engine(config)
    try:
        with Session(engine) as session:
            sessions = session.execute(
                select(AccountSession).where(AccountSession.user_id == user_id)
            ).scalars()
            assert all(row.revoked_at is not None for row in sessions)
    finally:
        engine.dispose()


def test_manual_expire_endpoint(tmp_path: Path) -> None:
    config = _seed(tmp_path)
    client = _client(config)
    headers = _login(client, "root")
    user_id = _user_id(config, "blocked")

    response = client.post(f"/api/v1/users/{user_id}/expire", json={"version": 1}, headers=headers)
    assert response.status_code == 200, response.get_json()
    data = response.get_json()["data"]
    assert data["status"] == "expired"
    assert data["version"] == 2
    assert data["expire_at"] is not None

    login = client.post("/api/v1/auth/login", json={"username": "blocked", "password": PASSWORD})
    assert login.status_code == 401
    assert login.get_json()["code"] == "ACCOUNT_EXPIRED"


# ── 过期口径：鉴权侧兜底 + 每日 worker ────────────────────────────────────


def test_expire_worker_and_auth_fallback(tmp_path: Path) -> None:
    config = _seed(tmp_path)
    client = _client(config)
    headers = _login(client, "root")
    user_id = _user_id(config, "blocked")
    past = (datetime.now(UTC) - timedelta(minutes=5)).isoformat(timespec="seconds")

    created = client.post(
        "/api/v1/users",
        json={"username": "late", "password": PASSWORD, "expire_at": past},
        headers=headers,
    )
    assert created.status_code == 201, created.get_json()

    # worker 未跑：鉴权侧按 expire_at 兜底，不给出窗口期
    login = client.post("/api/v1/auth/login", json={"username": "late", "password": PASSWORD})
    assert login.status_code == 401
    assert login.get_json()["code"] == "ACCOUNT_EXPIRED"

    assert expire_once(config) >= 1
    row = _user_row(config, "late")
    assert row["status"] == "expired"
    assert row["token_version"] >= 2
    assert expire_once(config) == 0  # 幂等：无新行

    stale_user = _user_row(config, "blocked")
    assert stale_user["status"] == "active"
    assert user_id > 0


# ── 验收口径二：导入冲突不覆盖 ────────────────────────────────────────────


def test_import_report_only_keeps_original(tmp_path: Path) -> None:
    config = _seed(tmp_path)
    client = _client(config)
    headers = _login(client, "root")
    before = _user_row(config, "student")
    assert before["display_name"] == "张三"

    response = client.post(
        "/api/v1/users/import",
        json={
            "rows": [
                {"username": "student", "password": NEW_PASSWORD, "display_name": "李四"},
                {"username": "imported", "password": PASSWORD, "user_no": "S9001"},
            ]
        },
        headers=headers,
    )
    assert response.status_code == 201, response.get_json()
    body = response.get_json()["data"]
    assert set(body) == set(ImportBatchOut.model_fields)
    assert body["on_conflict_action"] == "report_only"
    assert body["status"] == "partial"
    assert body["created_rows"] == 1
    assert body["updated_rows"] == 0
    assert body["skipped_rows"] == 1
    assert body["invalid_rows"] == 0
    assert [item["action"] for item in body["rows"]] == ["skipped", "created"]
    assert body["rows"][0]["reason"] == "conflict_keep_original"
    for item in body["rows"]:
        assert set(item) == {"row", "username", "user_no", "action", "reason"}
    _assert_no_secret(body)

    after = _user_row(config, "student")
    assert after["display_name"] == "张三"
    assert after["password_hash"] == before["password_hash"]
    assert after["version"] == before["version"]
    assert after["status"] == "active"

    old_login = client.post(
        "/api/v1/auth/login", json={"username": "student", "password": PASSWORD}
    )
    assert old_login.status_code == 200
    new_login = client.post(
        "/api/v1/auth/login", json={"username": "student", "password": NEW_PASSWORD}
    )
    assert new_login.status_code == 401
    assert new_login.get_json()["code"] == "AUTH_INVALID_CREDENTIALS"


def test_import_overwrite_updates_existing(tmp_path: Path) -> None:
    config = _seed(tmp_path)
    client = _client(config)
    headers = _login(client, "root")
    before = _user_row(config, "student")
    rows = [
        {"username": "student", "password": NEW_PASSWORD, "display_name": "李四"},
        {"username": "imported", "password": PASSWORD, "user_no": "S9001"},
    ]
    first = client.post("/api/v1/users/import", json={"rows": rows}, headers=headers)
    assert first.status_code == 201

    response = client.post(
        "/api/v1/users/import",
        json={"rows": rows, "on_conflict_action": "overwrite"},
        headers=headers,
    )
    assert response.status_code == 201, response.get_json()
    body = response.get_json()["data"]
    assert body["on_conflict_action"] == "overwrite"
    assert body["status"] == "completed"
    assert body["updated_rows"] == 2
    assert body["created_rows"] == 0
    assert body["skipped_rows"] == 0
    assert [item["action"] for item in body["rows"]] == ["updated", "updated"]

    after = _user_row(config, "student")
    assert after["display_name"] == "李四"
    assert after["password_hash"] != before["password_hash"]

    login = client.post(
        "/api/v1/auth/login", json={"username": "student", "password": NEW_PASSWORD}
    )
    assert login.status_code == 200


def test_import_rejects_reserved_username_and_invalid_rows(tmp_path: Path) -> None:
    config = _seed(tmp_path)
    client = _client(config)
    headers = _login(client, "root")
    response = client.post(
        "/api/v1/users/import",
        json={
            "rows": [
                {"username": "bad", "password": "short"},
                {"username": "oknew", "password": PASSWORD},
                {"username": "oknew", "password": PASSWORD},
            ]
        },
        headers=headers,
    )
    assert response.status_code == 201, response.get_json()
    body = response.get_json()["data"]
    assert body["status"] == "partial"
    assert body["invalid_rows"] == 2
    assert body["created_rows"] == 1
    assert body["rows"][0]["reason"]
    assert body["rows"][2]["reason"] == "duplicate_in_batch"


# ── 文件导入 + 批次复核 ──────────────────────────────────────────────────


def test_import_xlsx_and_csv(tmp_path: Path) -> None:
    config = _seed(tmp_path)
    client = _client(config)
    headers = _login(client, "root")
    headers_xlsx = _xlsx(
        ["username", "password", "user_no", "display_name"],
        [["xlsx1", PASSWORD, "X0001", "表格一"], ["xlsx2", PASSWORD, "X0002", "表格二"]],
    )

    response = client.post(
        "/api/v1/users/import",
        data={"file": (io.BytesIO(headers_xlsx), "users.xlsx")},
        headers=headers,
        content_type="multipart/form-data",
    )
    assert response.status_code == 201, response.get_json()
    body = response.get_json()["data"]
    assert body["status"] == "completed"
    assert body["created_rows"] == 2
    assert {row["username"] for row in body["rows"]} == {"xlsx1", "xlsx2"}

    csv_bytes = _csv(["username", "password", "user_no"], [["csv1", PASSWORD, "C0001"]])
    response = client.post(
        "/api/v1/users/import",
        data={"file": (io.BytesIO(csv_bytes), "users.csv")},
        headers=headers,
        content_type="multipart/form-data",
    )
    assert response.status_code == 201, response.get_json()
    body = response.get_json()["data"]
    assert body["created_rows"] == 1
    assert body["rows"][0]["username"] == "csv1"

    bad = client.post(
        "/api/v1/users/import",
        data={"file": (io.BytesIO(b'{"username":"x"}'), "users.json")},
        headers=headers,
        content_type="multipart/form-data",
    )
    assert bad.status_code == 422
    assert bad.get_json()["code"] == "VALIDATION_FAILED"

    missing_header = client.post(
        "/api/v1/users/import",
        data={"file": (io.BytesIO(_csv(["name", "password"], [["x", PASSWORD]])), "bad.csv")},
        headers=headers,
        content_type="multipart/form-data",
    )
    assert missing_header.status_code == 422
    assert "表头必须包含 username" in missing_header.get_json()["data"]["errors"][0]


def test_import_batches_are_queryable(tmp_path: Path) -> None:
    config = _seed(tmp_path)
    client = _client(config)
    headers = _login(client, "root")
    created = client.post(
        "/api/v1/users/import",
        json={"rows": [{"username": "batchuser", "password": PASSWORD}]},
        headers=headers,
    ).get_json()["data"]
    batch_no = created["batch_no"]

    listed = client.get("/api/v1/users/imports", headers=headers).get_json()["data"]
    assert set(listed) == set(ImportBatchListOut.model_fields)
    assert listed["total"] >= 1
    assert listed["items"][0]["batch_no"] == batch_no
    assert listed["items"][0]["rows"] is None

    detail = client.get(f"/api/v1/users/imports/{batch_no}", headers=headers).get_json()["data"]
    assert set(detail) == set(ImportBatchOut.model_fields)
    assert detail["rows"] is not None
    assert detail["rows"][0]["action"] == "created"
    _assert_no_secret(detail)

    assert client.get("/api/v1/users/imports/IMP-UNKNOWN", headers=headers).status_code == 404


# ── 班级 ─────────────────────────────────────────────────────────────────


def test_class_crud_and_members(tmp_path: Path) -> None:
    config = _seed(tmp_path)
    client = _client(config)
    headers = _login(client, "root")
    teacher_id = _user_id(config, "teacher")

    created = client.post(
        "/api/v1/classes",
        json={"name": "三班", "teacher_id": teacher_id},
        headers=headers,
    )
    assert created.status_code == 201, created.get_json()
    data = created.get_json()["data"]
    assert set(data) == set(ClassOut.model_fields)
    assert data["name"] == "三班"
    assert data["teacher_id"] == teacher_id
    assert data["member_count"] == 0
    assert data["sort_order"] > 0

    duplicate = client.post("/api/v1/classes", json={"name": "三班"}, headers=headers)
    assert duplicate.status_code == 422
    assert "班级名称已存在" in duplicate.get_json()["data"]["errors"]

    outsider_id = _user_id(config, "outsider")
    added = client.post(
        f"/api/v1/classes/{data['id']}/members",
        json={"user_ids": [outsider_id]},
        headers=headers,
    )
    assert added.status_code == 201, added.get_json()
    body = added.get_json()["data"]
    assert set(body) == set(ClassMembersOut.model_fields)
    assert body["added"] == 1
    assert [item["user_id"] for item in body["members"]] == [outsider_id]

    again = client.post(
        f"/api/v1/classes/{data['id']}/members",
        json={"user_ids": [outsider_id]},
        headers=headers,
    )
    assert again.status_code == 200
    assert again.get_json()["data"]["added"] == 0

    missing = client.post(
        f"/api/v1/classes/{data['id']}/members", json={"user_ids": [99999]}, headers=headers
    )
    assert missing.status_code == 404
    assert missing.get_json()["code"] == "NOT_FOUND"

    filtered = client.get(f"/api/v1/users?class_id={data['id']}", headers=headers).get_json()[
        "data"
    ]
    assert [item["username"] for item in filtered["items"]] == ["outsider"]
    assert filtered["items"][0]["class_count"] == 2

    listed = client.get("/api/v1/classes?keyword=三班", headers=headers).get_json()["data"]
    assert listed["total"] == 1
    assert listed["items"][0]["member_count"] == 1

    removed = client.delete(f"/api/v1/classes/{data['id']}/members/{outsider_id}", headers=headers)
    assert removed.status_code == 200
    assert removed.get_json()["data"]["removed"] is True
    twice = client.delete(f"/api/v1/classes/{data['id']}/members/{outsider_id}", headers=headers)
    assert twice.get_json()["data"]["removed"] is False


# ── 班级授权：成员可看，退班 / 停班立即失权 ──────────────────────────────


def test_class_course_access_follows_membership(tmp_path: Path) -> None:
    config = _seed(tmp_path)
    client = _client(config)
    student = _login(client, "student")
    outsider = _login(client, "outsider")

    allowed = client.post("/api/v1/media/tickets", json={"lesson_id": L_CLASS}, headers=student)
    assert allowed.status_code == 200, allowed.get_json()
    ticket = allowed.get_json()["data"]
    assert ticket["kid"] == KID
    assert ticket["preview"] is False

    denied = client.post("/api/v1/media/tickets", json={"lesson_id": L_CLASS}, headers=outsider)
    assert denied.status_code == 403
    assert denied.get_json()["code"] == "ACCESS_CLASS_REQUIRED"

    entry = client.get(f"/api/v1/lessons/{L_CLASS}/quiz", headers=student)
    assert entry.status_code == 200, entry.get_json()
    assert entry.get_json()["data"]["question_count"] == 1
    blocked = client.get(f"/api/v1/lessons/{L_CLASS}/quiz", headers=outsider)
    assert blocked.status_code == 403
    assert blocked.get_json()["code"] == "ACCESS_CLASS_REQUIRED"

    # 先按正常播放链路取到 key URL（含 key 专属签名），再退班验证旧票据 40204
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
    key_url = key_line.split('URI="', 1)[1].rstrip('"')

    # 退班：旧票据授权指纹失配 → 40204；再次取票 → 403
    admin = _client(config)
    headers = _login(admin, "root")
    class_id = _class_id(config, "一班")
    student_id = _user_id(config, "student")
    removed = admin.delete(f"/api/v1/classes/{class_id}/members/{student_id}", headers=headers)
    assert removed.get_json()["data"]["removed"] is True

    key = client.get(key_url)
    assert key.status_code == 403
    assert key.get_json()["code"] == int(Code.MEDIA_ACCESS_REVOKED)
    assert key.get_json()["data"]["reason"] == "policy_changed"

    again = client.post("/api/v1/media/tickets", json={"lesson_id": L_CLASS}, headers=student)
    assert again.status_code == 403
    assert again.get_json()["code"] == "ACCESS_CLASS_REQUIRED"


def test_disabled_class_revokes_access(tmp_path: Path) -> None:
    config = _seed(tmp_path)
    assert _class_id(config, "停用班") > 0
    client = _client(config)
    outsider = _login(client, "outsider")
    response = client.post("/api/v1/media/tickets", json={"lesson_id": L_CLASS}, headers=outsider)
    assert response.status_code == 403
    assert response.get_json()["code"] == "ACCESS_CLASS_REQUIRED"
