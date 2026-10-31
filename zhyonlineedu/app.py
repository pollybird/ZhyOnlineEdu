"""Flask 应用工厂（M1.1 工程骨架）。

装配顺序（§4.3 [V2.3]）：
1. 配置三层（默认 → env → sys_config）；
2. DB 适配层能力探测；
3. 探测 READY 后才注册业务 Blueprint；否则 /healthz 返回 warn:cap_probe_pending，
   /api/v1/** 统一 409 INSTALL_REQUIRED，安装向导不开放"开始安装"。
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from importlib.metadata import PackageNotFoundError, version
from typing import Any

from flask import Flask, Response, current_app, g, jsonify, request

from zhyonlineedu.api.auth import bp as auth_bp
from zhyonlineedu.api.catalog import bp as catalog_bp
from zhyonlineedu.api.commerce import bp as commerce_bp
from zhyonlineedu.api.install import bp as install_bp
from zhyonlineedu.api.learn import bp as learn_bp
from zhyonlineedu.api.media import bp as media_bp
from zhyonlineedu.api.quiz import bp as quiz_bp
from zhyonlineedu.api.system import bp as system_bp
from zhyonlineedu.api.transcode import bp as transcode_bp
from zhyonlineedu.api.users import bp as users_bp
from zhyonlineedu.core.auth.service import AuthService
from zhyonlineedu.core.capability import CapabilityProbe, PendingProbe, ProbeResult, ProbeState
from zhyonlineedu.core.codes import INSTALL_REQUIRED
from zhyonlineedu.core.config import Config, load_config
from zhyonlineedu.core.constants import ZHY_HEADER_REQUEST_ID
from zhyonlineedu.core.db.engine import ensure_database
from zhyonlineedu.core.errors import AppError, register_error_handlers
from zhyonlineedu.core.install import InstallService
from zhyonlineedu.core.logging import configure_logging
from zhyonlineedu.core.mode import ModeContext
from zhyonlineedu.core.openapi import build_spec, documentation_app
from zhyonlineedu.core.progress.gateway import ProgressGateway
from zhyonlineedu.core.request_id import resolve_request_id
from zhyonlineedu.modules.assess.service import AssessService
from zhyonlineedu.modules.catalog.service import CatalogService
from zhyonlineedu.modules.commerce.service import CommerceService
from zhyonlineedu.modules.config.service import ModeService
from zhyonlineedu.modules.learn.catalog import StudentCatalog
from zhyonlineedu.modules.learn.playback import PlaybackService
from zhyonlineedu.modules.learn.profile import ProfileService
from zhyonlineedu.modules.media.transcode_service import TranscodeService
from zhyonlineedu.modules.media.upload_service import UploadService
from zhyonlineedu.modules.users.service import UsersService

_DETAIL: dict[ProbeState, str] = {
    ProbeState.PENDING: "cap_probe_pending",
    ProbeState.READY: "cap_probe_ready",
    ProbeState.FAILED: "cap_probe_failed",
}
_STATUS: dict[ProbeState, str] = {
    ProbeState.PENDING: "warn",
    ProbeState.READY: "ok",
    ProbeState.FAILED: "error",
}
_DB_CHECK: dict[ProbeState, str] = {
    ProbeState.PENDING: "pending",
    ProbeState.READY: "ok",
    ProbeState.FAILED: "failed",
}

OPENAPI_PATH = "/api/v1/openapi.json"
_OPENAPI_CACHE: dict[str, Any] | None = None


def openapi_spec() -> dict[str, Any]:
    """进程内缓存的 OpenAPI 文档（§15.4：由路由与 schema 生成，禁止手写）。"""
    global _OPENAPI_CACHE
    if _OPENAPI_CACHE is None:
        _OPENAPI_CACHE = build_spec(documentation_app(), version=_version())
    return _OPENAPI_CACHE


@dataclass(slots=True)
class _AppState:
    """进程内可变装配状态：安装 finish 后由 InstallService 回调置 READY。"""

    probe: ProbeResult


def _version() -> str:
    try:
        return version("zhyonlineedu")
    except PackageNotFoundError:  # pragma: no cover - 源码直跑
        return "0.1.0"


def register_business_blueprints(app: Flask) -> None:
    """业务 Blueprint 注册位：仅 probe READY 时调用（§4.3 装配门禁）。

    M1.6 起注册认证/RBAC；M2.1 起注册学科树/课程三层；M2.3 起注册分片上传；
    M2.4 起注册转码资产；M2.5 起注册播放票据与进度；M2.6 起注册题库与作答；
    M2.7 起注册用户/班级/导入；M2.10 起注册双模式状态与切换向导；M2.12 起注册会员/订单/
    支付回调；后续模块沿用同一入口，禁止在安装期注册。
    """
    app.register_blueprint(auth_bp)
    app.register_blueprint(catalog_bp)
    app.register_blueprint(media_bp)
    app.register_blueprint(transcode_bp)
    app.register_blueprint(learn_bp)
    app.register_blueprint(quiz_bp)
    app.register_blueprint(users_bp)
    app.register_blueprint(system_bp)
    app.register_blueprint(commerce_bp)


def _assign_request_id() -> None:
    g.request_id = resolve_request_id(request.headers.get(ZHY_HEADER_REQUEST_ID))
    g.request_started = time.perf_counter()


def _finalize_response(response: Response) -> Response:
    request_id = str(getattr(g, "request_id", "") or resolve_request_id(None))
    response.headers[ZHY_HEADER_REQUEST_ID] = request_id
    started = getattr(g, "request_started", None)
    duration_ms = (time.perf_counter() - started) * 1000 if isinstance(started, float) else 0.0
    current_app.logger.info(
        "request method=%s path=%s status=%s duration_ms=%.2f",
        request.method,
        request.path,
        response.status_code,
        duration_ms,
    )
    return response


def _install_gate() -> None:
    path = request.path
    if not path.startswith("/api/v1/") or path.startswith("/api/v1/install"):
        return
    # 接口文档属于生成物，安装期也需可读（前端/联调先拉 schema 再走安装）
    if path == OPENAPI_PATH:
        return
    state: _AppState = current_app.extensions["zhy_state"]
    if current_app.extensions.get("zhy_business_registered"):
        return
    # Flask 不允许首个请求后再注册 Blueprint：安装 finish 只落库，业务路由随重启装配。
    reason = (
        "install_restart_required" if state.probe.state is ProbeState.READY else "cap_probe_pending"
    )
    raise AppError(
        INSTALL_REQUIRED,
        data={
            "probe_state": state.probe.state.value,
            "reason": reason,
            "blockers": list(state.probe.blockers),
        },
    )


def _health_payload(result: ProbeResult) -> dict[str, object]:
    payload: dict[str, object] = {
        "status": _STATUS[result.state],
        "detail": _DETAIL[result.state],
        "version": _version(),
        "checks": {"config": "ok", "db": _DB_CHECK[result.state]},
    }
    caps: dict[str, object] = {"blockers": list(result.blockers)}
    if result.state is ProbeState.READY:
        caps["flavor"] = result.flavor
        caps["capabilities"] = dict(result.capabilities)
        payload["caps"] = caps
    elif result.state is ProbeState.FAILED:
        payload["caps"] = caps
    return payload


def create_app(config: Config | None = None, probe: CapabilityProbe | None = None) -> Flask:
    cfg = load_config() if config is None else config
    configure_logging(cfg.get_str("env"))

    app = Flask(__name__)
    app.config["ZHY_ENV"] = cfg.get_str("env")
    app.config["ZHY_CONFIG"] = cfg

    result = (PendingProbe() if probe is None else probe).probe()
    state = _AppState(probe=result)
    app.extensions["zhy_config"] = cfg
    app.extensions["zhy_probe"] = result
    app.extensions["zhy_state"] = state
    app.extensions["zhy_business_registered"] = False

    def _mark_ready(ready: ProbeResult) -> None:
        state.probe = ready
        app.extensions["zhy_probe"] = ready

    # 安装期注册向导路由；已 READY 的进程把 token 置空 = 安装入口关闭（幂等拒绝）。
    token = None if result.state is not ProbeState.READY else ""
    install = InstallService(cfg, on_ready=_mark_ready, token=token)
    app.extensions["zhy_install"] = install
    app.register_blueprint(install_bp)

    app.before_request(_assign_request_id)
    app.before_request(_install_gate)
    app.after_request(_finalize_response)
    register_error_handlers(app)

    @app.get("/healthz")
    def healthz() -> tuple[Response, int]:
        return jsonify(_health_payload(state.probe)), 200

    @app.get(OPENAPI_PATH)
    def openapi_json() -> tuple[Response, int]:
        return jsonify(openapi_spec()), 200

    @app.get("/readyz")
    def readyz() -> tuple[Response, int]:
        ready = state.probe.state is ProbeState.READY
        return jsonify(_health_payload(state.probe)), 200 if ready else 503

    if result.state is ProbeState.READY:
        # READY 才装配业务依赖：认证服务需要可用连接与已固化的 RSA 密钥
        ensure_database(cfg)
        mode_context = ModeContext(cfg)
        app.extensions["zhy_mode"] = mode_context
        app.extensions["zhy_auth"] = AuthService(cfg)
        app.extensions["zhy_catalog"] = CatalogService(cfg)
        app.extensions["zhy_upload"] = UploadService(cfg)
        app.extensions["zhy_transcode"] = TranscodeService(cfg)
        app.extensions["zhy_playback"] = PlaybackService(cfg)
        app.extensions["zhy_progress"] = ProgressGateway(cfg)
        app.extensions["zhy_profile"] = ProfileService(cfg)
        app.extensions["zhy_student_catalog"] = StudentCatalog(cfg)
        app.extensions["zhy_quiz"] = AssessService(cfg)
        app.extensions["zhy_users"] = UsersService(cfg)
        app.extensions["zhy_mode_service"] = ModeService(cfg)
        app.extensions["zhy_commerce"] = CommerceService(cfg)
        register_business_blueprints(app)
        app.extensions["zhy_business_registered"] = True

    return app
