"""M2.1 学科树 + 课程三层（§10.1 / §10.2 / §9.4）。

覆盖：
- 学科树：增删改查、同级重名、最大深度、环移动、软删后同名可重建；
- 乐观锁：`version` 缺失/过期统一 409 CONFLICT_RETRY；
- 课程三层：章/课时 CRUD、章与课时排序、删除级联软删；
- 上架前置：无课时、资产未 encoded、学科停用三类拒绝，资产就绪后成功；
- RBAC：学员角色（仅 lesson:play）访问目录接口 403 PERMISSION_DENIED；
- schema 漂移：响应字段与 Pydantic 模型逐一比对。
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from pydantic import BaseModel
from sqlalchemy.orm import Session
from zhyonlineedu.api.schemas import (
    ChapterOut,
    CourseDetailOut,
    CourseListOut,
    CourseOut,
    CoursePublishOut,
    DeleteOut,
    LessonOut,
    SubjectListOut,
    SubjectOut,
)
from zhyonlineedu.app import create_app
from zhyonlineedu.core.auth.keys import clear_key_cache, ensure_keypair
from zhyonlineedu.core.auth.passwords import hash_password
from zhyonlineedu.core.capability import ProbeResult, ProbeState, StaticProbe
from zhyonlineedu.core.config import Config, load_config
from zhyonlineedu.core.db import compat
from zhyonlineedu.core.db.engine import create_db_engine
from zhyonlineedu.core.db.migration import upgrade_head
from zhyonlineedu.core.db.models.account import AccountUser, SysUserRole, utcnow
from zhyonlineedu.core.db.models.media import MediaAsset

PASSWORD = "S3cret-pass"  # noqa: S105 - 测试用固定口令
READY = ProbeResult(ProbeState.READY, flavor="sqlite", capabilities={"returning": True})


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
            "ZHY_DB_URL": f"sqlite:///{tmp_path / 'catalog.db'}",
            "ZHY_DB_FLAVOR": "sqlite",
            "ZHY_JWT_PRIVATE_KEY_PATH": str(tmp_path / "keys" / "jwt_private.pem"),
            "ZHY_JWT_PUBLIC_KEY_PATH": str(tmp_path / "keys" / "jwt_public.pem"),
        },
        sys_config={},
    )


def _seed(tmp_path: Path) -> Config:
    """建库 + 密钥 + 两个账号：root(super_admin) / student(lesson:play)。"""
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
            session.commit()
    finally:
        engine.dispose()
    return config


def _client(config: Config) -> Any:
    return create_app(config=config, probe=StaticProbe(READY)).test_client()


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


def _error(response: Any, status: int, code: str) -> dict[str, Any]:
    body = response.get_json()
    assert response.status_code == status, body
    assert body["code"] == code, body
    assert "request_id" in body
    return dict(body.get("data") or {})


def _assert_matches(model: type[BaseModel], payload: dict[str, Any]) -> None:
    assert set(payload) == set(model.model_fields), (
        f"{model.__name__} 与实现漂移：schema={sorted(model.model_fields)} "
        f"实现={sorted(payload)}"
    )


@pytest.fixture()
def client(tmp_path: Path) -> Any:
    return _client(_seed(tmp_path))


@pytest.fixture()
def auth(client: Any) -> dict[str, str]:
    return _headers(client)


@pytest.fixture()
def subject(client: Any, auth: dict[str, str]) -> dict[str, Any]:
    return _data(client.post("/api/v1/subjects", json={"name": "数学"}, headers=auth))


# ── 学科树 ────────────────────────────────────────────────────────────────


def test_subject_crud_and_schema(
    client: Any, auth: dict[str, str], subject: dict[str, Any]
) -> None:
    _assert_matches(SubjectOut, subject)
    assert subject["parent_id"] is None
    assert subject["sort_order"] == 1024

    listed = _data(client.get("/api/v1/subjects", headers=auth))
    _assert_matches(SubjectListOut, listed)
    assert listed["total"] == 1

    child = _data(
        client.post(
            "/api/v1/subjects", json={"name": "代数", "parent_id": subject["id"]}, headers=auth
        )
    )
    assert child["parent_id"] == subject["id"]

    renamed = _data(
        client.patch(
            f"/api/v1/subjects/{child['id']}",
            json={"version": child["version"], "name": "代数初步"},
            headers=auth,
        )
    )
    assert renamed["name"] == "代数初步"
    assert renamed["version"] == child["version"] + 1

    deleted = _data(
        client.delete(
            f"/api/v1/subjects/{renamed['id']}", json={"version": renamed["version"]}, headers=auth
        )
    )
    _assert_matches(DeleteOut, deleted)
    assert deleted == {"id": renamed["id"], "deleted": True}

    # 软删后同名可重建（uq(parent_id, name, delete_seq)）
    again = _data(
        client.post(
            "/api/v1/subjects", json={"name": "代数初步", "parent_id": subject["id"]}, headers=auth
        )
    )
    assert again["id"] != renamed["id"]


def test_subject_rejects_duplicate_and_missing_version(
    client: Any, auth: dict[str, str], subject: dict[str, Any]
) -> None:
    _error(
        client.post("/api/v1/subjects", json={"name": "数学"}, headers=auth),
        422,
        "VALIDATION_FAILED",
    )
    _error(
        client.patch(f"/api/v1/subjects/{subject['id']}", json={"name": "语文"}, headers=auth),
        422,
        "VALIDATION_FAILED",
    )


def test_subject_depth_limit_and_cycle(client: Any, auth: dict[str, str]) -> None:
    parent_id: int | None = None
    nodes: list[dict[str, Any]] = []
    for index in range(1, 6):  # 深度 5：根 → 第 2 层 … 第 5 层
        body: dict[str, Any] = {"name": f"层级{index}"}
        if parent_id is not None:
            body["parent_id"] = parent_id
        node = _data(client.post("/api/v1/subjects", json=body, headers=auth))
        nodes.append(node)
        parent_id = node["id"]

    _error(
        client.post(
            "/api/v1/subjects", json={"name": "第6层", "parent_id": parent_id}, headers=auth
        ),
        422,
        "VALIDATION_FAILED",
    )

    second, fifth = nodes[1], nodes[4]
    # 把第 2 层（含 4 层子树）挂到第 5 层下 → 超过深度 5
    _error(
        client.patch(
            f"/api/v1/subjects/{second['id']}",
            json={"version": second["version"], "parent_id": fifth["id"]},
            headers=auth,
        ),
        422,
        "VALIDATION_FAILED",
    )
    # 根挂到自己的子节点下 → 环
    root, child = nodes[0], nodes[1]
    _error(
        client.patch(
            f"/api/v1/subjects/{root['id']}",
            json={"version": root["version"], "parent_id": child["id"]},
            headers=auth,
        ),
        422,
        "VALIDATION_FAILED",
    )


def test_subject_move_version_conflict(
    client: Any, auth: dict[str, str], subject: dict[str, Any]
) -> None:
    sibling = _data(client.post("/api/v1/subjects", json={"name": "物理"}, headers=auth))
    moved = _data(
        client.patch(
            f"/api/v1/subjects/{sibling['id']}",
            json={"version": sibling["version"], "before_id": subject["id"]},
            headers=auth,
        )
    )
    assert moved["sort_order"] == 1024
    # 旧 version 再写 → 409
    data = _error(
        client.patch(
            f"/api/v1/subjects/{sibling['id']}",
            json={"version": sibling["version"], "name": "物理2"},
            headers=auth,
        ),
        409,
        "CONFLICT_RETRY",
    )
    assert data["resource"] == "subject"


def test_subject_delete_requires_empty(
    client: Any, auth: dict[str, str], subject: dict[str, Any]
) -> None:
    _data(
        client.post(
            "/api/v1/subjects", json={"name": "几何", "parent_id": subject["id"]}, headers=auth
        )
    )
    _error(
        client.delete(
            f"/api/v1/subjects/{subject['id']}", json={"version": subject["version"]}, headers=auth
        ),
        409,
        "CATALOG_NOT_EMPTY",
    )


# ── 课程三层 ──────────────────────────────────────────────────────────────


def _course(client: Any, auth: dict[str, str], subject_id: int, title: str = "初一数学") -> dict:
    return _data(
        client.post(
            "/api/v1/courses",
            json={"subject_id": subject_id, "title": title, "mode": "internal"},
            headers=auth,
        )
    )


def test_course_three_layers(client: Any, auth: dict[str, str], subject: dict[str, Any]) -> None:
    course = _course(client, auth, subject["id"])
    _assert_matches(CourseOut, course)
    assert course["status"] == "draft"
    assert course["chapter_count"] == 0

    listed = _data(client.get(f"/api/v1/courses?subject_id={subject['id']}", headers=auth))
    _assert_matches(CourseListOut, listed)
    assert listed["total"] == 1
    assert listed["page"] == 1

    chapter = _data(
        client.post(
            f"/api/v1/courses/{course['id']}/chapters", json={"title": "第一章"}, headers=auth
        )
    )
    _assert_matches(ChapterOut, chapter)
    lesson = _data(
        client.post(
            f"/api/v1/chapters/{chapter['id']}/lessons",
            # 时长不再接受人工输入：即使回传 duration_sec 也会被忽略，等上传视频后派生。
            json={"title": "1.1 有理数", "duration_sec": 600},
            headers=auth,
        )
    )
    _assert_matches(LessonOut, lesson)
    assert lesson["duration_sec"] == 0
    assert lesson["course_id"] == course["id"]
    assert lesson["asset_status"] is None

    detail = _data(client.get(f"/api/v1/courses/{course['id']}", headers=auth))
    _assert_matches(CourseDetailOut, detail)
    assert [item["id"] for item in detail["chapters"]] == [chapter["id"]]
    assert detail["chapters"][0]["lessons"][0]["id"] == lesson["id"]

    # 章下仍有课时 → 拒绝删章
    _error(
        client.delete(
            f"/api/v1/chapters/{chapter['id']}",
            json={"version": chapter["version"]},
            headers=auth,
        ),
        409,
        "CATALOG_NOT_EMPTY",
    )


def test_reorder_lessons_and_chapters(
    client: Any, auth: dict[str, str], subject: dict[str, Any]
) -> None:
    course = _course(client, auth, subject["id"])
    first = _data(
        client.post(f"/api/v1/courses/{course['id']}/chapters", json={"title": "一"}, headers=auth)
    )
    second = _data(
        client.post(f"/api/v1/courses/{course['id']}/chapters", json={"title": "二"}, headers=auth)
    )
    reordered = _data(
        client.post(
            f"/api/v1/courses/{course['id']}/reorder",
            json={"version": course["version"], "ordered_ids": [second["id"], first["id"]]},
            headers=auth,
        )
    )
    assert reordered["ordered_ids"] == [second["id"], first["id"]]
    detail = _data(client.get(f"/api/v1/courses/{course['id']}", headers=auth))
    assert [item["id"] for item in detail["chapters"]] == [second["id"], first["id"]]
    # 父级 version 已 +1，旧版本重排 → 409
    _error(
        client.post(
            f"/api/v1/courses/{course['id']}/reorder",
            json={"version": course["version"], "ordered_ids": [first["id"], second["id"]]},
            headers=auth,
        ),
        409,
        "CONFLICT_RETRY",
    )
    # 集合并集不一致 → 422
    _error(
        client.post(
            f"/api/v1/courses/{course['id']}/reorder",
            json={"version": detail["version"], "ordered_ids": [first["id"]]},
            headers=auth,
        ),
        422,
        "VALIDATION_FAILED",
    )

    lessons = [
        _data(
            client.post(
                f"/api/v1/chapters/{first['id']}/lessons",
                json={"title": f"课时{index}"},
                headers=auth,
            )
        )
        for index in (1, 2)
    ]
    chapter_now = detail["chapters"][1]
    _data(
        client.post(
            f"/api/v1/chapters/{first['id']}/reorder",
            json={
                "version": chapter_now["version"],
                "ordered_ids": [lessons[1]["id"], lessons[0]["id"]],
            },
            headers=auth,
        )
    )
    refreshed = _data(client.get(f"/api/v1/courses/{course['id']}", headers=auth))
    assert [item["id"] for item in refreshed["chapters"][1]["lessons"]] == [
        lessons[1]["id"],
        lessons[0]["id"],
    ]


def test_course_update_conflict_and_delete_flow(
    client: Any, auth: dict[str, str], subject: dict[str, Any]
) -> None:
    course = _course(client, auth, subject["id"])
    updated = _data(
        client.patch(
            f"/api/v1/courses/{course['id']}",
            json={"version": course["version"], "title": "初二数学", "access_type": "member"},
            headers=auth,
        )
    )
    assert updated["title"] == "初二数学"
    assert updated["access_type"] == "member"
    _error(
        client.patch(
            f"/api/v1/courses/{course['id']}",
            json={"version": course["version"], "title": "初三数学"},
            headers=auth,
        ),
        409,
        "CONFLICT_RETRY",
    )

    deleted = _data(
        client.delete(
            f"/api/v1/courses/{course['id']}", json={"version": updated["version"]}, headers=auth
        )
    )
    assert deleted["deleted"] is True
    _error(client.get(f"/api/v1/courses/{course['id']}", headers=auth), 404, "NOT_FOUND")
    # 软删后同科同名可重建
    assert _course(client, auth, subject["id"], "初二数学")["id"] != course["id"]


# ── 上架前置 ──────────────────────────────────────────────────────────────


def _publish(client: Any, auth: dict[str, str], course_id: int, version: int) -> Any:
    return client.post(
        f"/api/v1/courses/{course_id}/publish", json={"version": version}, headers=auth
    )


def test_publish_requires_encoded_assets(
    client: Any, auth: dict[str, str], subject: dict[str, Any]
) -> None:
    course = _course(client, auth, subject["id"])
    chapter = _data(
        client.post(
            f"/api/v1/courses/{course['id']}/chapters", json={"title": "第一章"}, headers=auth
        )
    )
    lessons = [
        _data(
            client.post(
                f"/api/v1/chapters/{chapter['id']}/lessons",
                json={"title": f"课时{index}"},
                headers=auth,
            )
        )
        for index in (1, 2)
    ]

    body = _error(
        _publish(client, auth, course["id"], course["version"]), 409, "COURSE_ASSET_NOT_READY"
    )
    assert body["reason"] == "asset_not_ready"
    assert {item["lesson_id"] for item in body["not_ready"]} == {item["id"] for item in lessons}

    config = client.application.config["ZHY_CONFIG"]
    engine = create_db_engine(config.get_str("db.url"), "sqlite")
    try:
        with Session(engine) as session:
            now = utcnow()
            session.add(
                MediaAsset(
                    lesson_id=lessons[0]["id"],
                    status="encoded",
                    playlist_version=1,
                    duration_sec=600,
                    created_at=now,
                    updated_at=now,
                )
            )
            session.commit()
        body = _error(
            _publish(client, auth, course["id"], course["version"]), 409, "COURSE_ASSET_NOT_READY"
        )
        assert [item["lesson_id"] for item in body["not_ready"]] == [lessons[1]["id"]]

        with Session(engine) as session:
            session.add(
                MediaAsset(
                    lesson_id=lessons[1]["id"],
                    status="encoded",
                    playlist_version=1,
                    duration_sec=600,
                    created_at=now,
                    updated_at=now,
                )
            )
            session.commit()
    finally:
        engine.dispose()

    published = _data(_publish(client, auth, course["id"], course["version"]))
    _assert_matches(CoursePublishOut, published)
    assert published["ready"] is True
    assert published["course"]["status"] == "published"
    assert published["published_at"] is not None

    # 已发布课程不能直接删除
    _error(
        client.delete(
            f"/api/v1/courses/{course['id']}",
            json={"version": published["course"]["version"]},
            headers=auth,
        ),
        422,
        "VALIDATION_FAILED",
    )

    unpublished = _data(
        client.post(
            f"/api/v1/courses/{course['id']}/unpublish",
            json={"version": published["course"]["version"]},
            headers=auth,
        )
    )
    assert unpublished["course"]["status"] == "draft"
    assert unpublished["course"]["published_at"] is not None

    deleted = _data(
        client.delete(
            f"/api/v1/courses/{course['id']}",
            json={"version": unpublished["course"]["version"]},
            headers=auth,
        )
    )
    assert deleted["deleted"] is True
    _error(
        client.patch(f"/api/v1/lessons/{lessons[0]['id']}", json={"version": 1}, headers=auth),
        404,
        "NOT_FOUND",
    )


def test_publish_rejects_empty_course_and_disabled_subject(
    client: Any, auth: dict[str, str], subject: dict[str, Any]
) -> None:
    empty = _course(client, auth, subject["id"])
    body = _error(
        _publish(client, auth, empty["id"], empty["version"]), 409, "COURSE_ASSET_NOT_READY"
    )
    assert body["reason"] == "no_lessons"

    disabled = _data(
        client.patch(
            f"/api/v1/subjects/{subject['id']}",
            json={"version": subject["version"], "is_enabled": False},
            headers=auth,
        )
    )
    assert disabled["is_enabled"] is False
    course = _course(client, auth, subject["id"], "停用学科课程")
    _error(_publish(client, auth, course["id"], course["version"]), 403, "SUBJECT_DISABLED")


# ── RBAC ─────────────────────────────────────────────────────────────────


def test_catalog_requires_course_edit_permission(client: Any) -> None:
    headers = _headers(client, "student")
    _error(client.get("/api/v1/subjects", headers=headers), 403, "PERMISSION_DENIED")
    _error(
        client.post("/api/v1/subjects", json={"name": "数学"}, headers=headers),
        403,
        "PERMISSION_DENIED",
    )
    _error(client.get("/api/v1/courses", headers=headers), 403, "PERMISSION_DENIED")
    _error(client.get("/api/v1/subjects"), 401, "AUTH_TOKEN_EXPIRED")
