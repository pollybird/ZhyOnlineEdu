"""OpenAPI 3.1 生成（§15.4「生成物不可手写」）。

设计：
- 路由来自 Flask url_map（`/api/v1/**` 必须全部登记文档，缺失即报错，防止新接口漏文档）；
- schema 来自 `zhyonlineedu/api/schemas.py` 的 Pydantic 模型，自动解引用为 components；
- 统一信封、错误码表（附录 A）、安全方案、`x-zhy-*` 扩展一并注入；
- `web/src/api/openapi.json` 与 `types.ts` 由 `scripts/export_openapi.py` 生成，CI 校验是否过期。
"""

from __future__ import annotations

import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import Any

from flask import Flask
from pydantic import BaseModel

from zhyonlineedu import __version__
from zhyonlineedu.core.codes import ERROR_SPECS, Code

OPENAPI_VERSION = "3.1.0"
API_PREFIX = "/api/v1/"
PROGRESS_VERSION = "zhy-progress-v2"
PROGRESS_ANTI_REPLAY = True
DOC_ATTR = "__zhy_doc__"
_SKIP_METHODS = frozenset({"HEAD", "OPTIONS"})
_PARAM = re.compile(r"<(?:([^:<>]+):)?([^<>]+)>")
_CONVERTER_TYPE = {"int": "integer", "float": "number", "path": "string", "uuid": "string"}

# 前端据此在 key_ttl 前 20s 预换票（§15.3 / §21.3）
TICKET_TTL_CONTRACT: Mapping[str, Any] = {
    "config_keys": ["media.ttl_ts", "media.ttl_playlist", "media.ttl_key"],
    "refresh_before_sec": 20,
}

TAG_DESCRIPTIONS: Mapping[str, str] = {
    "install": "安装向导（免鉴权，一次性令牌换取安装会话）",
    "auth": "认证与 RBAC（AT 走 Bearer，RT 走 HttpOnly Cookie）",
    "catalog": "学科树 + 课程三层（写操作一律乐观锁 version）",
    "media": (
        "分片上传 + 输入检疫（tmp 0700 / O_EXCL / 魔数白名单 · §10.3.1）"
        " + 转码资产（沙箱 / HLS / AES-128 · §10.3.2.3）"
    ),
    "learn": (
        "播放票据（三层 TTL + nonce 单次 · §10.3.3）"
        " + 进度网关（202 / gw_seq / 单写者落库 · §12.5）"
    ),
    "quiz": ("题库与判分（答案不下发 · §10.4） + 交卷幂等（Idempotency-Key = attempt_uid）"),
    "users": (
        "用户 / 班级 / 批量导入（越权 403 · 导入默认 report_only 不覆盖原值 · §10.5）"
        " + 账号过期（每日 02:00 + 鉴权兜底）"
    ),
    "system": ("双模式状态 / 预检 / 切换向导（epoch 乐观锁 · 切换不改表 · §9.3 / §9.6）"),
    "member": ("会员档位 / 我的会员 / 直接开通（金额快照 6 位小数 · 订阅叠加不吞时长 · §10.6）"),
    "pay": ("订单 / 回调 / 查单补偿（状态只前进不后退 · event_id 幂等 · §10.6）"),
    "plugin": ("插件生命周期与路由分发（manifest 校验 / 独立迁移链 / 禁用即 404 · §11）"),
}


class OpenApiError(RuntimeError):
    """接口缺少文档或 schema 非法：CI 直接失败。"""


@dataclass(frozen=True, slots=True)
class OperationDoc:
    summary: str
    description: str = ""
    request_model: type[BaseModel] | None = None
    response_model: type[BaseModel] | None = None
    tags: tuple[str, ...] = ()
    auth: bool = True
    # 非 JSON 响应（m3u8 / AES key / 加密分片）不套统一信封，单独声明媒体类型
    media_type: str = "application/json"
    extensions: Mapping[str, Any] = field(default_factory=dict)


def documented(
    summary: str,
    *,
    description: str = "",
    request: type[BaseModel] | None = None,
    response: type[BaseModel] | None = None,
    tags: tuple[str, ...] = (),
    auth: bool = True,
    media_type: str = "application/json",
    extensions: Mapping[str, Any] | None = None,
) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
    """给视图函数挂文档；`extensions` 原样写入 operation（如 `x-zhy-idempotent`）。"""
    doc = OperationDoc(
        summary=summary,
        description=description,
        request_model=request,
        response_model=response,
        tags=tags,
        auth=auth,
        media_type=media_type,
        extensions=dict(extensions or {}),
    )

    def decorate(view: Callable[..., Any]) -> Callable[..., Any]:
        setattr(view, DOC_ATTR, doc)
        return view

    return decorate


def _clean(schema: dict[str, Any]) -> dict[str, Any]:
    schema.pop("$defs", None)
    schema.pop("title", None)
    return schema


def _register_model(model: type[BaseModel], components: dict[str, Any]) -> dict[str, Any]:
    name = model.__name__
    if name not in components:
        raw = model.model_json_schema(ref_template="#/components/schemas/{model}")
        for def_name, def_schema in raw.pop("$defs", {}).items():
            components.setdefault(def_name, _clean(dict(def_schema)))
        components[name] = _clean(raw)
    return {"$ref": f"#/components/schemas/{name}"}


def _envelope(data_schema: dict[str, Any] | None) -> dict[str, Any]:
    return {
        "type": "object",
        "required": ["code", "data", "request_id"],
        "properties": {
            "code": {"oneOf": [{"type": "string"}, {"type": "integer"}]},
            "message": {"type": "string"},
            "data": data_schema if data_schema is not None else {},
            "request_id": {"type": "string"},
        },
    }


def _error_envelope() -> dict[str, Any]:
    schema = _envelope(None)
    schema["description"] = "统一失败信封（无堆栈）"
    return schema


def _openapi_path(rule: str) -> tuple[str, list[dict[str, Any]]]:
    params: list[dict[str, Any]] = []

    def repl(match: re.Match[str]) -> str:
        converter = match.group(1) or "string"
        name = match.group(2)
        params.append(
            {
                "name": name,
                "in": "path",
                "required": True,
                "schema": {"type": _CONVERTER_TYPE.get(converter, "string")},
            }
        )
        return "{" + name + "}"

    return _PARAM.sub(repl, rule), params


def _error_code_table() -> dict[str, Any]:
    table: dict[str, Any] = {}
    for code, spec in ERROR_SPECS.items():
        key = str(int(code)) if isinstance(code, Code) else str(code)
        table[key] = {"http": spec.http, "message": spec.message, "action": spec.action}
    return table


def _all_api_blueprints() -> tuple[Any, ...]:
    from zhyonlineedu.api.auth import bp as auth_bp
    from zhyonlineedu.api.catalog import bp as catalog_bp
    from zhyonlineedu.api.commerce import bp as commerce_bp
    from zhyonlineedu.api.install import bp as install_bp
    from zhyonlineedu.api.learn import bp as learn_bp
    from zhyonlineedu.api.media import bp as media_bp
    from zhyonlineedu.api.plugin import bp as plugin_bp
    from zhyonlineedu.api.quiz import bp as quiz_bp
    from zhyonlineedu.api.system import bp as system_bp
    from zhyonlineedu.api.transcode import bp as transcode_bp
    from zhyonlineedu.api.users import bp as users_bp

    return (
        install_bp,
        auth_bp,
        catalog_bp,
        media_bp,
        transcode_bp,
        learn_bp,
        quiz_bp,
        users_bp,
        system_bp,
        commerce_bp,
        plugin_bp,
    )


def documentation_app() -> Flask:
    """只登记 API 路由的文档用应用：不连库、不装能力探测。"""
    app = Flask("zhyonlineedu-openapi")
    for blueprint in _all_api_blueprints():
        app.register_blueprint(blueprint)
    return app


def build_spec(app: Flask, *, version: str | None = None) -> dict[str, Any]:
    """生成 OpenAPI 3.1 规格；`version` 缺省取 `zhyonlineedu.__version__`。"""
    spec_version = version or __version__
    components: dict[str, Any] = {"schemas": {}, "securitySchemes": {}}
    paths: dict[str, Any] = {}
    tags_used: set[str] = set()
    missing: list[str] = []

    for rule in sorted(app.url_map.iter_rules(), key=lambda item: str(item)):
        if not str(rule).startswith(API_PREFIX):
            continue
        view = app.view_functions.get(rule.endpoint)
        doc = getattr(view, DOC_ATTR, None)
        if not isinstance(doc, OperationDoc):
            missing.append(str(rule))
            continue
        path, path_params = _openapi_path(str(rule))
        path_item: dict[str, Any] = paths.setdefault(path, {})
        for method in sorted((rule.methods or set()) - _SKIP_METHODS):
            responses: dict[str, Any] = {}
            if doc.media_type != "application/json":
                kind = "binary" if doc.media_type == "application/octet-stream" else "string"
                responses["200"] = {
                    "description": "原始响应（不套统一信封）",
                    "content": {doc.media_type: {"schema": {"type": kind}}},
                }
            else:
                data_schema = (
                    _register_model(doc.response_model, components["schemas"])
                    if doc.response_model is not None
                    else None
                )
                responses["200"] = {
                    "description": "统一成功信封",
                    "content": {"application/json": {"schema": _envelope(data_schema)}},
                }
            for status in ("401", "403", "409", "422", "500"):
                responses[status] = {
                    "description": "统一失败信封",
                    "content": {
                        "application/json": {
                            "schema": {"$ref": "#/components/schemas/ErrorEnvelope"}
                        }
                    },
                }
            operation: dict[str, Any] = {
                "tags": list(doc.tags),
                "summary": doc.summary,
                "operationId": f"{rule.endpoint}.{method.lower()}",
                "responses": responses,
                "security": [{"bearerAuth": []}] if doc.auth else [],
            }
            if doc.description:
                operation["description"] = doc.description
            if doc.request_model is not None:
                operation["requestBody"] = {
                    "required": True,
                    "content": {
                        "application/json": {
                            "schema": _register_model(doc.request_model, components["schemas"])
                        }
                    },
                }
            operation.update(doc.extensions)
            if path_params:
                operation["parameters"] = path_params
            path_item[method.lower()] = operation
            tags_used.update(doc.tags)

    if missing:
        raise OpenApiError("以下 /api/v1 路由缺少 @documented 文档：" + ", ".join(missing))

    components["schemas"]["ErrorEnvelope"] = _error_envelope()
    components["securitySchemes"]["bearerAuth"] = {
        "type": "http",
        "scheme": "bearer",
        "bearerFormat": "JWT",
        "description": "RS256 Access Token；RT 由 HttpOnly Cookie 携带",
    }
    return {
        "openapi": OPENAPI_VERSION,
        "info": {
            "title": "钟毓在线教育系统 API",
            "version": spec_version,
            "description": "统一信封 {code, message, data, request_id}；错误码见 x-zhy-error-codes",
            "x-zhy-progress-version": PROGRESS_VERSION,
            "x-zhy-progress-anti-replay": PROGRESS_ANTI_REPLAY,
        },
        "servers": [{"url": "/"}],
        "tags": [
            {"name": tag, "description": TAG_DESCRIPTIONS.get(tag, "")} for tag in sorted(tags_used)
        ],
        "paths": paths,
        "components": components,
        "x-zhy-envelope": ["code", "message", "data", "request_id"],
        "x-zhy-ticket-ttl": dict(TICKET_TTL_CONTRACT),
        "x-zhy-error-codes": _error_code_table(),
    }
