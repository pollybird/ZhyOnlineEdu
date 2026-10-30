"""M1.7 OpenAPI 生成物（§15.4）：结构、扩展、错误码、生成物一致与模型漂移。

覆盖：
- `/api/v1/**` 全部路由都有文档，缺失即报错（防止新接口漏文档）；
- 统一信封 / 安全方案 / `x-zhy-*` 扩展与附录 A 错误码表；
- `web/src/api/openapi.json` + `types.ts` 与代码一致（生成物不可手写）；
- Pydantic 模型字段与实现返回值漂移比对（安装向导 + 认证全链路实跑）。
"""

from __future__ import annotations

import importlib.util
import json
import sys
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from flask import Blueprint, Flask
from pydantic import BaseModel
from sqlalchemy.orm import Session
from zhyonlineedu.api.schemas import (
    AuthUserOut,
    BootstrapOut,
    DbSetupOut,
    DbTestOut,
    InstallAdminOut,
    InstallFinishOut,
    InstallModeOut,
    InstallStateOut,
    InstallTokenOut,
    LoginOut,
    LogoutOut,
    PasswordOut,
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
from zhyonlineedu.core.db.models.system import utcnow
from zhyonlineedu.core.envelope import ok
from zhyonlineedu.core.openapi import (
    PROGRESS_VERSION,
    OpenApiError,
    build_spec,
    documentation_app,
)

ROOT = Path(__file__).resolve().parent.parent
SPEC_FILE = ROOT / "web" / "src" / "api" / "openapi.json"
TYPES_FILE = ROOT / "web" / "src" / "api" / "types.ts"

TOKEN = "openapi-install-token"  # noqa: S105 - 测试用固定安装令牌
PASSWORD = "S3cret-pass"  # noqa: S105 - 测试用固定口令
SESSION_HEADER = "X-ZHY-Install-Session"
READY = ProbeResult(ProbeState.READY, flavor="sqlite", capabilities={"returning": True})

EXPECTED_PATHS: dict[str, set[str]] = {
    "/api/v1/install/state": {"get"},
    "/api/v1/install/token": {"post"},
    "/api/v1/install/db/test": {"post"},
    "/api/v1/install/db/setup": {"post"},
    "/api/v1/install/admin": {"post"},
    "/api/v1/install/mode": {"post"},
    "/api/v1/install/finish": {"post"},
    "/api/v1/auth/login": {"post"},
    "/api/v1/auth/refresh": {"post"},
    "/api/v1/auth/logout": {"post"},
    "/api/v1/auth/password": {"patch"},
    "/api/v1/auth/bootstrap": {"get"},
    "/api/v1/subjects": {"get", "post"},
    "/api/v1/subjects/{subject_id}": {"patch", "delete"},
    "/api/v1/courses": {"get", "post"},
    "/api/v1/courses/{course_id}": {"get", "patch", "delete"},
    "/api/v1/courses/{course_id}/publish": {"post"},
    "/api/v1/courses/{course_id}/unpublish": {"post"},
    "/api/v1/courses/{course_id}/reorder": {"post"},
    "/api/v1/courses/{course_id}/chapters": {"post"},
    "/api/v1/chapters/{chapter_id}": {"patch", "delete"},
    "/api/v1/chapters/{chapter_id}/reorder": {"post"},
    "/api/v1/chapters/{chapter_id}/lessons": {"post"},
    "/api/v1/lessons/{lesson_id}": {"patch", "delete"},
    "/api/v1/media/uploads": {"post"},
    "/api/v1/media/uploads/{upload_id}": {"get", "delete"},
    "/api/v1/media/uploads/{upload_id}/chunks": {"post"},
    "/api/v1/media/uploads/{upload_id}/complete": {"post"},
    "/api/v1/media/assets": {"get"},
    "/api/v1/media/assets/{lesson_id}": {"get", "delete"},
    "/api/v1/media/assets/{lesson_id}/retranscode": {"post"},
    "/api/v1/media/tickets": {"post"},
    "/api/v1/media/playlist/{kid}/master.m3u8": {"get"},
    "/api/v1/media/playlist/{kid}/{profile}/index.m3u8": {"get"},
    "/api/v1/media/key/{kid}": {"get"},
    "/api/v1/media/seg/{kid}/{profile}/{name}": {"get"},
    "/api/v1/learn/progress": {"post"},
    "/api/v1/my/progress": {"get"},
    "/api/v1/my/progress/gate": {"get"},
    "/api/v1/my/progress/{course_id}": {"get"},
    "/api/v1/my/summary": {"get"},
    "/api/v1/quiz/questions": {"get", "post"},
    "/api/v1/quiz/questions/{question_id}": {"patch", "delete"},
    "/api/v1/lessons/{lesson_id}/quiz": {"get"},
    "/api/v1/quiz/attempts": {"post"},
    "/api/v1/quiz/attempts/{attempt_id}": {"get"},
    "/api/v1/quiz/attempts/{attempt_id}/submit": {"post"},
    "/api/v1/my/quiz/attempts": {"get"},
    "/api/v1/users": {"get", "post"},
    "/api/v1/users/import": {"post"},
    "/api/v1/users/imports": {"get"},
    "/api/v1/users/imports/{batch_no}": {"get"},
    "/api/v1/users/{user_id}": {"get", "patch"},
    "/api/v1/users/{user_id}/disable": {"post"},
    "/api/v1/users/{user_id}/expire": {"post"},
    "/api/v1/classes": {"get", "post"},
    "/api/v1/classes/{class_id}/members": {"post"},
    "/api/v1/classes/{class_id}/members/{user_id}": {"delete"},
    "/api/v1/system/mode": {"get"},
    "/api/v1/system/mode/precheck": {"post"},
    "/api/v1/system/mode/switch": {"post"},
    "/api/v1/member/levels": {"get", "post"},
    "/api/v1/member/levels/{level_id}": {"patch"},
    "/api/v1/member/me": {"get"},
    "/api/v1/member/subscriptions": {"get"},
    "/api/v1/member/grant": {"post"},
    "/api/v1/pay/orders": {"get", "post"},
    "/api/v1/pay/orders/{order_no}": {"get"},
    "/api/v1/pay/orders/{order_no}/cancel": {"post"},
    "/api/v1/pay/orders/{order_no}/reconcile": {"post"},
    "/api/v1/pay/{channel}/notify": {"post"},
}
RAW_PATHS = {
    "/api/v1/media/playlist/{kid}/master.m3u8": "application/vnd.apple.mpegurl",
    "/api/v1/media/playlist/{kid}/{profile}/index.m3u8": "application/vnd.apple.mpegurl",
    "/api/v1/media/key/{kid}": "application/octet-stream",
    "/api/v1/media/seg/{kid}/{profile}/{name}": "video/mp2t",
}
PROTECTED = {
    ("/api/v1/auth/password", "patch"),
    ("/api/v1/auth/bootstrap", "get"),
    ("/api/v1/subjects", "get"),
    ("/api/v1/subjects", "post"),
    ("/api/v1/subjects/{subject_id}", "patch"),
    ("/api/v1/subjects/{subject_id}", "delete"),
    ("/api/v1/courses", "get"),
    ("/api/v1/courses", "post"),
    ("/api/v1/courses/{course_id}", "get"),
    ("/api/v1/courses/{course_id}", "patch"),
    ("/api/v1/courses/{course_id}", "delete"),
    ("/api/v1/courses/{course_id}/publish", "post"),
    ("/api/v1/courses/{course_id}/unpublish", "post"),
    ("/api/v1/courses/{course_id}/reorder", "post"),
    ("/api/v1/courses/{course_id}/chapters", "post"),
    ("/api/v1/chapters/{chapter_id}", "patch"),
    ("/api/v1/chapters/{chapter_id}", "delete"),
    ("/api/v1/chapters/{chapter_id}/reorder", "post"),
    ("/api/v1/chapters/{chapter_id}/lessons", "post"),
    ("/api/v1/lessons/{lesson_id}", "patch"),
    ("/api/v1/lessons/{lesson_id}", "delete"),
    ("/api/v1/media/uploads", "post"),
    ("/api/v1/media/uploads/{upload_id}", "get"),
    ("/api/v1/media/uploads/{upload_id}", "delete"),
    ("/api/v1/media/uploads/{upload_id}/chunks", "post"),
    ("/api/v1/media/uploads/{upload_id}/complete", "post"),
    ("/api/v1/media/assets", "get"),
    ("/api/v1/media/assets/{lesson_id}", "get"),
    ("/api/v1/media/assets/{lesson_id}", "delete"),
    ("/api/v1/media/assets/{lesson_id}/retranscode", "post"),
    ("/api/v1/media/tickets", "post"),
    ("/api/v1/learn/progress", "post"),
    ("/api/v1/my/progress", "get"),
    ("/api/v1/my/progress/gate", "get"),
    ("/api/v1/my/progress/{course_id}", "get"),
    ("/api/v1/my/summary", "get"),
    ("/api/v1/quiz/questions", "get"),
    ("/api/v1/quiz/questions", "post"),
    ("/api/v1/quiz/questions/{question_id}", "patch"),
    ("/api/v1/quiz/questions/{question_id}", "delete"),
    ("/api/v1/lessons/{lesson_id}/quiz", "get"),
    ("/api/v1/quiz/attempts", "post"),
    ("/api/v1/quiz/attempts/{attempt_id}", "get"),
    ("/api/v1/quiz/attempts/{attempt_id}/submit", "post"),
    ("/api/v1/my/quiz/attempts", "get"),
    ("/api/v1/users", "get"),
    ("/api/v1/users", "post"),
    ("/api/v1/users/import", "post"),
    ("/api/v1/users/imports", "get"),
    ("/api/v1/users/imports/{batch_no}", "get"),
    ("/api/v1/users/{user_id}", "get"),
    ("/api/v1/users/{user_id}", "patch"),
    ("/api/v1/users/{user_id}/disable", "post"),
    ("/api/v1/users/{user_id}/expire", "post"),
    ("/api/v1/classes", "get"),
    ("/api/v1/classes", "post"),
    ("/api/v1/classes/{class_id}/members", "post"),
    ("/api/v1/classes/{class_id}/members/{user_id}", "delete"),
    ("/api/v1/system/mode", "get"),
    ("/api/v1/system/mode/precheck", "post"),
    ("/api/v1/system/mode/switch", "post"),
    ("/api/v1/member/levels", "get"),
    ("/api/v1/member/levels", "post"),
    ("/api/v1/member/levels/{level_id}", "patch"),
    ("/api/v1/member/me", "get"),
    ("/api/v1/member/subscriptions", "get"),
    ("/api/v1/member/grant", "post"),
    ("/api/v1/pay/orders", "get"),
    ("/api/v1/pay/orders", "post"),
    ("/api/v1/pay/orders/{order_no}", "get"),
    ("/api/v1/pay/orders/{order_no}/cancel", "post"),
    ("/api/v1/pay/orders/{order_no}/reconcile", "post"),
}


@pytest.fixture(autouse=True)
def _cleanup() -> Iterator[None]:
    compat.reset()
    clear_key_cache()
    yield
    compat.reset()
    clear_key_cache()


@pytest.fixture(scope="module")
def spec() -> dict[str, Any]:
    return build_spec(documentation_app())


def _operation(spec: dict[str, Any], path: str, method: str = "post") -> dict[str, Any]:
    return dict(spec["paths"][path][method])


# ── 结构 ──────────────────────────────────────────────────────────────────


def test_version_and_paths(spec: dict[str, Any]) -> None:
    assert spec["openapi"] == "3.1.0"
    assert set(spec["paths"]) == set(EXPECTED_PATHS)
    for path, methods in EXPECTED_PATHS.items():
        assert set(spec["paths"][path]) == methods, path


def test_every_operation_documented(spec: dict[str, Any]) -> None:
    for path, methods in EXPECTED_PATHS.items():
        for method in methods:
            operation = _operation(spec, path, method)
            assert operation["summary"], f"{method} {path} 缺 summary"
            assert operation["operationId"].endswith(f".{method}")
            assert operation["tags"], f"{method} {path} 缺 tags"
            for status in ("200", "401", "403", "409", "422", "500"):
                assert status in operation["responses"], f"{method} {path} 缺 {status}"
            if path in RAW_PATHS:
                # 二进制/流式端点不套信封：声明原始媒体类型即可（key/分片/m3u8）
                content = operation["responses"]["200"]["content"]
                assert set(content) == {RAW_PATHS[path]}, f"{method} {path}"
                assert "schema" in content[RAW_PATHS[path]]
            else:
                envelope = operation["responses"]["200"]["content"]["application/json"]["schema"]
                assert {"code", "data", "request_id"} <= set(envelope["properties"])
                assert "$ref" in envelope["properties"]["data"], f"{method} {path} 缺响应模型"
            failure = operation["responses"]["401"]["content"]["application/json"]["schema"]
            assert failure["$ref"] == "#/components/schemas/ErrorEnvelope"


def test_security_follows_auth_flag(spec: dict[str, Any]) -> None:
    for path, methods in EXPECTED_PATHS.items():
        for method in methods:
            operation = _operation(spec, path, method)
            expected = [{"bearerAuth": []}] if (path, method) in PROTECTED else []
            assert operation["security"] == expected, f"{method} {path}"
    scheme = spec["components"]["securitySchemes"]["bearerAuth"]
    assert scheme["type"] == "http"
    assert scheme["scheme"] == "bearer"


def test_request_models_and_idempotency(spec: dict[str, Any]) -> None:
    body = _operation(spec, "/api/v1/install/admin")["requestBody"]
    assert body["content"]["application/json"]["schema"]["$ref"].endswith("/InstallAdminIn")
    login = _operation(spec, "/api/v1/auth/login")["requestBody"]
    assert login["content"]["application/json"]["schema"]["$ref"].endswith("/LoginIn")
    assert "requestBody" not in _operation(spec, "/api/v1/install/finish")
    assert "requestBody" not in _operation(spec, "/api/v1/auth/refresh")

    for path in (
        "/api/v1/install/db/test",
        "/api/v1/install/db/setup",
        "/api/v1/install/admin",
        "/api/v1/install/mode",
    ):
        assert _operation(spec, path)["x-zhy-idempotent"] is True, path
    assert "x-zhy-idempotent" not in _operation(spec, "/api/v1/install/finish")


def test_undocumented_route_is_rejected() -> None:
    app = Flask("doc-check")
    bp = Blueprint("undoc", __name__, url_prefix="/api/v1")
    bp.add_url_rule("/naked", "naked", lambda: ok({"pong": True}))
    app.register_blueprint(bp)
    with pytest.raises(OpenApiError, match="/api/v1/naked"):
        build_spec(app)


def test_openapi_route_served_before_install(tmp_path: Path) -> None:
    """文档属生成物，安装期也需可读；业务接口仍被装配门禁拦截。"""
    client = create_app(config=_install_config(tmp_path)).test_client()
    response = client.get("/api/v1/openapi.json")
    assert response.status_code == 200
    assert response.get_json()["openapi"] == "3.1.0"
    assert response.get_json()["info"]["x-zhy-progress-version"] == PROGRESS_VERSION

    gated = client.post("/api/v1/auth/login", json={"username": "root", "password": "x"})
    assert gated.status_code == 409
    assert gated.get_json()["code"] == "INSTALL_REQUIRED"


# ── 扩展与错误码 ──────────────────────────────────────────────────────────


def test_root_extensions(spec: dict[str, Any]) -> None:
    info = spec["info"]
    assert info["x-zhy-progress-version"] == PROGRESS_VERSION == "zhy-progress-v2"
    assert info["x-zhy-progress-anti-replay"] is True
    assert spec["x-zhy-envelope"] == ["code", "message", "data", "request_id"]

    ttl = spec["x-zhy-ticket-ttl"]
    assert ttl["config_keys"] == ["media.ttl_ts", "media.ttl_playlist", "media.ttl_key"]
    assert ttl["refresh_before_sec"] == 20


def test_error_code_table_covers_registry(spec: dict[str, Any]) -> None:
    codes = spec["x-zhy-error-codes"]
    for code in Code:
        assert str(int(code)) in codes, f"错误码 {int(code)} 未导出"
    assert codes["40201"] == {
        "http": 401,
        "message": "播放票据已过期",
        "action": "refresh_ticket",
    }
    for name in (
        "AUTH_TOKEN_EXPIRED",
        "AUTH_REFRESH_REPLAY",
        "AUTH_INVALID_CREDENTIALS",
        "INSTALL_REQUIRED",
        "INSTALL_TOKEN_INVALID",
        "PERMISSION_DENIED",
    ):
        assert name in codes, name


# ── 生成物一致性（CI：过期即失败）────────────────────────────────────────


def _export_module() -> Any:
    path = ROOT / "scripts" / "export_openapi.py"
    spec = importlib.util.spec_from_file_location("export_openapi", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules["export_openapi"] = module
    spec.loader.exec_module(module)
    return module


def test_exported_artifacts_are_up_to_date() -> None:
    module = _export_module()
    generated = module._build_spec()
    assert generated == build_spec(documentation_app())
    assert json.loads(SPEC_FILE.read_text(encoding="utf-8")) == json.loads(
        json.dumps(generated, ensure_ascii=False, sort_keys=True)
    ), "web/src/api/openapi.json 已过期，请运行 scripts/export_openapi.py"
    assert TYPES_FILE.read_text(encoding="utf-8") == module._render_types(
        generated
    ), "web/src/api/types.ts 已过期，请运行 scripts/export_openapi.py"


# ── 模型漂移比对（实现返回值 ↔ Pydantic schema）──────────────────────────


def _assert_matches(model: type[BaseModel], payload: dict[str, Any]) -> None:
    assert set(payload) == set(model.model_fields), (
        f"{model.__name__} 与实现漂移："
        f"schema={sorted(model.model_fields)} 实现={sorted(payload)}"
    )


def _install_config(tmp_path: Path) -> Config:
    return load_config(
        environ={
            "ZHY_ENV": "dev",
            "ZHY_DB_URL": f"sqlite:///{tmp_path / 'openapi-install.db'}",
            "ZHY_DB_FLAVOR": "sqlite",
            "ZHY_INSTALL_TOKEN": TOKEN,
            "ZHY_JWT_PRIVATE_KEY_PATH": str(tmp_path / "keys" / "jwt_private.pem"),
            "ZHY_JWT_PUBLIC_KEY_PATH": str(tmp_path / "keys" / "jwt_public.pem"),
        },
        sys_config={},
    )


def test_install_models_match_responses(tmp_path: Path) -> None:
    client = create_app(config=_install_config(tmp_path)).test_client()

    state = client.get("/api/v1/install/state").get_json()["data"]
    _assert_matches(InstallStateOut, state)

    token = client.post("/api/v1/install/token", json={"token": TOKEN}).get_json()["data"]
    _assert_matches(InstallTokenOut, token)
    headers = {SESSION_HEADER: str(token["session"])}

    tested = client.post("/api/v1/install/db/test", headers=headers).get_json()["data"]
    _assert_matches(DbTestOut, tested)

    setup = client.post("/api/v1/install/db/setup", headers=headers).get_json()["data"]
    _assert_matches(DbSetupOut, setup)

    admin = client.post(
        "/api/v1/install/admin",
        json={"username": "root", "password": PASSWORD},
        headers=headers,
    ).get_json()["data"]
    _assert_matches(InstallAdminOut, admin)

    mode = client.post(
        "/api/v1/install/mode", json={"mode": "internal"}, headers=headers
    ).get_json()["data"]
    _assert_matches(InstallModeOut, mode)

    finish = client.post("/api/v1/install/finish", headers=headers).get_json()["data"]
    _assert_matches(InstallFinishOut, finish)


def test_auth_models_match_responses(tmp_path: Path) -> None:
    config = load_config(
        environ={
            "ZHY_ENV": "dev",
            "ZHY_DB_URL": f"sqlite:///{tmp_path / 'openapi-auth.db'}",
            "ZHY_DB_FLAVOR": "sqlite",
            "ZHY_JWT_PRIVATE_KEY_PATH": str(tmp_path / "keys" / "jwt_private.pem"),
            "ZHY_JWT_PUBLIC_KEY_PATH": str(tmp_path / "keys" / "jwt_public.pem"),
        },
        sys_config={},
    )
    ensure_keypair(config.get_str("jwt.private_key_path"), config.get_str("jwt.public_key_path"))
    url = config.get_str("db.url")
    upgrade_head(url, "sqlite")
    engine = create_db_engine(url, "sqlite")
    try:
        with Session(engine) as session:
            now = utcnow()
            user = AccountUser(
                username="root",
                password_hash=hash_password(PASSWORD),
                status="active",
                token_version=1,
                created_at=now,
                updated_at=now,
            )
            session.add(user)
            session.flush()
            session.add(SysUserRole(user_id=user.id, role_code="super_admin"))
            session.commit()
    finally:
        engine.dispose()

    client = create_app(config=config, probe=StaticProbe(READY)).test_client()
    login = client.post("/api/v1/auth/login", json={"username": "root", "password": PASSWORD})
    data = login.get_json()["data"]
    _assert_matches(LoginOut, data)
    _assert_matches(AuthUserOut, data["user"])

    headers = {"Authorization": f"Bearer {data['access_token']}"}
    boot = client.get("/api/v1/auth/bootstrap", headers=headers).get_json()["data"]
    _assert_matches(BootstrapOut, boot)
    _assert_matches(AuthUserOut, boot["user"])

    changed = client.patch(
        "/api/v1/auth/password",
        json={"old_password": PASSWORD, "new_password": "N3w-secret-pass"},
        headers=headers,
    ).get_json()["data"]
    _assert_matches(PasswordOut, changed)

    logout = client.post("/api/v1/auth/logout", headers=headers).get_json()["data"]
    _assert_matches(LogoutOut, logout)
