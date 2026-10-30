#!/usr/bin/env python3
"""导出 OpenAPI 3.1 与前端类型（§15.4「接口文档生成物不可手写」）。

用法：
    uv run python scripts/export_openapi.py          # 重新生成 web/src/api/openapi.json + types.ts
    uv run python scripts/export_openapi.py --check  # CI：生成物与代码不一致时退出码 1

路由与 schema 的唯一来源是 Flask url_map + `zhyonlineedu/api/schemas.py`；
本脚本只做序列化，禁止手工编辑产物。
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
API_DIR = ROOT / "web" / "src" / "api"
SPEC_FILE = API_DIR / "openapi.json"
TYPES_FILE = API_DIR / "types.ts"

TYPES_HEADER = (
    "// 由 scripts/export_openapi.py 从后端路由与 Pydantic schema 生成，禁止手改。\n"
    "// 重新生成：uv run python scripts/export_openapi.py\n"
    "/* eslint-disable */\n\n"
)

ENVELOPE = (
    "/** 统一信封：成功与失败同构，失败时 data 携带原因（§15.1）。 */\n"
    "export interface ApiEnvelope<T> {\n"
    "  code: number | string;\n"
    "  message?: string;\n"
    "  data: T;\n"
    "  request_id: string;\n"
    "}\n"
)

_IDENT = re.compile(r"^[A-Za-z_$][A-Za-z0-9_$]*$")
_PRIMITIVES = {
    "integer": "number",
    "number": "number",
    "string": "string",
    "boolean": "boolean",
    "null": "null",
}


def _build_spec() -> dict[str, Any]:
    if str(ROOT) not in sys.path:
        sys.path.insert(0, str(ROOT))
    from zhyonlineedu.core.openapi import build_spec, documentation_app

    return build_spec(documentation_app())


def _literal(value: Any) -> str:
    if isinstance(value, str):
        return json.dumps(value, ensure_ascii=False)
    if isinstance(value, bool):
        return "true" if value else "false"
    if value is None:
        return "null"
    return json.dumps(value)


def _one_line(text: str) -> str:
    return " ".join(text.replace("*/", "* /").split())


def _ts_type(schema: dict[str, Any]) -> str:
    """JSON Schema → TypeScript 类型表达式（仅覆盖 Pydantic 生成的结构）。"""
    if "$ref" in schema:
        return str(schema["$ref"]).rsplit("/", 1)[-1]
    if "const" in schema:
        return _literal(schema["const"])
    if "enum" in schema:
        return " | ".join(_literal(item) for item in schema["enum"])
    for combiner in ("anyOf", "oneOf"):
        if combiner in schema:
            parts: list[str] = []
            for item in schema[combiner]:
                part = _ts_type(item)
                if part not in parts:
                    parts.append(part)
            return " | ".join(parts)
    kind = schema.get("type")
    if kind == "array":
        item = _ts_type(schema.get("items", {}))
        return f"({item})[]" if " | " in item else f"{item}[]"
    if kind == "object" or "properties" in schema:
        properties = schema.get("properties")
        if not properties:
            extra = schema.get("additionalProperties")
            if isinstance(extra, dict):
                return f"Record<string, {_ts_type(extra)}>"
            return "Record<string, unknown>"
        fields = []
        for name, prop in properties.items():
            optional = "" if name in schema.get("required", ()) else "?"
            key = name if _IDENT.match(name) else json.dumps(name, ensure_ascii=False)
            fields.append(f"{key}{optional}: {_ts_type(prop)}")
        return "{ " + "; ".join(fields) + " }"
    if isinstance(kind, str):
        return _PRIMITIVES.get(kind, "unknown")
    return "unknown"


def _render_interface(name: str, schema: dict[str, Any]) -> str:
    properties: dict[str, Any] = schema.get("properties", {})
    required = set(schema.get("required", ()))
    lines = [f"export interface {name} {{"]
    if not properties:
        lines.append("  [key: string]: unknown;")
    for prop, prop_schema in properties.items():
        description = prop_schema.get("description")
        if isinstance(description, str) and description:
            lines.append(f"  /** {_one_line(description)} */")
        optional = "" if prop in required else "?"
        key = prop if _IDENT.match(prop) else json.dumps(prop, ensure_ascii=False)
        lines.append(f"  {key}{optional}: {_ts_type(prop_schema)};")
    lines.append("}")
    return "\n".join(lines)


def _render_error_codes(spec: dict[str, Any]) -> str:
    codes = spec.get("x-zhy-error-codes", {})
    numeric = sorted(int(key) for key in codes if str(key).lstrip("-").isdigit())
    strings = sorted(key for key in codes if not str(key).lstrip("-").isdigit())
    members = [str(item) for item in numeric]
    members += [json.dumps(item, ensure_ascii=False) for item in strings]
    if not members:
        return "export type ErrorCode = number | string;\n"
    body = "\n".join(f"  | {item}" for item in members)
    return f"export type ErrorCode =\n{body};\n"


def _render_types(spec: dict[str, Any]) -> str:
    schemas: dict[str, Any] = spec.get("components", {}).get("schemas", {})
    blocks = [ENVELOPE, _render_error_codes(spec)]
    for name in sorted(schemas):
        blocks.append(_render_interface(name, schemas[name]) + "\n")
    return TYPES_HEADER + "\n".join(blocks)


def main(argv: list[str]) -> int:
    spec = _build_spec()
    targets = {
        SPEC_FILE: json.dumps(spec, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        TYPES_FILE: _render_types(spec),
    }
    check = "--check" in argv
    stale: list[Path] = []
    for path, content in targets.items():
        if check:
            current = path.read_text(encoding="utf-8") if path.exists() else None
            if current != content:
                stale.append(path)
            continue
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
        print(f"写入 {path.relative_to(ROOT)}")
    if not check:
        return 0
    if stale:
        for path in stale:
            print(f"生成物过期：{path.relative_to(ROOT)}", file=sys.stderr)
        print("请运行 uv run python scripts/export_openapi.py 重新生成", file=sys.stderr)
        return 1
    print("OpenAPI 生成物与后端代码一致")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
