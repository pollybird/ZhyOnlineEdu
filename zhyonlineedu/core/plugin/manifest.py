"""插件 manifest 加载与校验（§11 / 附录 E · M3.1）。

口径：
- 必填字段 `code/name/version/core_api_min/entry_server/supported_modes/capabilities`，
  缺失或格式不符一律 `PLUGIN_MANIFEST_INVALID`，**不进启用态**；
- `code` 正则 `^[a-z][a-z0-9_]{2,63}$`，派生表前缀 `plg_<code>_`（插件表隔离的强约束）；
- `capabilities` 只能是内核白名单枚举；历史直写类能力（`direct_write` / `direct_session`
  等）不在白名单，出现即 `PLUGIN_CAP_DENIED`；
- `capabilities_v2.*`（`progress_direct_write` / `db_ddl` / `ffmpeg_direct` / `upsert_raw_sql`）
  仅允许 `false`，声明 `true` 即 `PLUGIN_CAP_DENIED`（内核团队签名机制未落地前一律拒绝）；
- manifest 源文本出现方言裸写令牌（黑名单见 `core.db.sql_guard`）直接拒绝，与 CI 共用同一份。
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from zhyonlineedu import __version__
from zhyonlineedu.core.codes import (
    PLUGIN_CAP_DENIED,
    PLUGIN_MANIFEST_INVALID,
    PLUGIN_TABLE_PREFIX_INVALID,
)
from zhyonlineedu.core.db.sql_guard import banned_lines
from zhyonlineedu.core.errors import AppError

CODE_PATTERN = re.compile(r"^[a-z][a-z0-9_]{2,63}$")
VERSION_PATTERN = re.compile(r"^\d+\.\d+\.\d+$")
ROUTE_PATTERN = re.compile(r"^[a-z0-9_]{1,64}$")
_VERSION_TRIPLE = re.compile(r"\d+")

REQUIRED_FIELDS: tuple[str, ...] = (
    "code",
    "name",
    "version",
    "core_api_min",
    "entry_server",
    "supported_modes",
    "capabilities",
)
SUPPORTED_MODES: tuple[str, ...] = ("internal", "external")
ALLOWED_CAPABILITIES: frozenset[str] = frozenset(
    {
        "progress:read",
        "progress:report",
        "media:ticket:issue",
        "access:evaluate",
        "config:read",
        "log:read",
    }
)
CAPABILITIES_V2_KEYS: frozenset[str] = frozenset(
    {"progress_direct_write", "db_ddl", "ffmpeg_direct", "upsert_raw_sql"}
)
MANIFEST_FILE = "manifest.json"


@dataclass(frozen=True, slots=True)
class PluginManifest:
    code: str
    name: str
    version: str
    core_api_min: str
    entry_server: str
    supported_modes: tuple[str, ...]
    capabilities: tuple[str, ...]
    capabilities_v2: Mapping[str, bool]
    table_prefix: str
    routes: tuple[str, ...]
    menu: tuple[Mapping[str, Any], ...]

    def as_dict(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "name": self.name,
            "version": self.version,
            "core_api_min": self.core_api_min,
            "entry_server": self.entry_server,
            "supported_modes": list(self.supported_modes),
            "capabilities": list(self.capabilities),
            "capabilities_v2": dict(self.capabilities_v2),
            "table_prefix": self.table_prefix,
            "routes": list(self.routes),
            "menu": [dict(item) for item in self.menu],
        }


def expected_table_prefix(code: str) -> str:
    return f"plg_{code}_"


def parse_version(text: str) -> tuple[int, int, int] | None:
    """`0.2.15-alpha` / `1.2.3` → (0, 2, 15)；不可解析返回 None。"""
    parts = _VERSION_TRIPLE.findall(text or "")
    if len(parts) < 3:
        return None
    return int(parts[0]), int(parts[1]), int(parts[2])


def kernel_version() -> str:
    return __version__


def _invalid(errors: list[str]) -> AppError:
    return AppError(PLUGIN_MANIFEST_INVALID, data={"errors": errors})


def _require_text(raw: Mapping[str, Any], field: str, errors: list[str]) -> str:
    value = raw.get(field)
    if not isinstance(value, str) or not value.strip():
        errors.append(f"{field} 必须是非空字符串")
        return ""
    return value.strip()


def _text_list(raw: Mapping[str, Any], field: str, errors: list[str]) -> list[str]:
    value = raw.get(field)
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        errors.append(f"{field} 必须是字符串数组")
        return []
    return [item.strip() for item in value]


def _relative_entry(path: str, errors: list[str]) -> str:
    normalized = path.replace("\\", "/")
    if normalized.startswith("/") or ".." in Path(normalized).parts:
        errors.append("entry_server 必须是插件目录内的相对路径")
        return normalized
    if not normalized.endswith(".py"):
        errors.append("entry_server 必须是 .py 文件")
    return normalized


def validate_manifest(raw: Mapping[str, Any]) -> PluginManifest:
    """按附录 E 校验并返回不可变 view；失败抛 `AppError`（已注册错误码）。"""
    if not isinstance(raw, Mapping):
        raise _invalid(["manifest 根节点必须是 JSON 对象"])

    errors: list[str] = []
    missing = [field for field in REQUIRED_FIELDS if field not in raw]
    if missing:
        errors.append("缺少必填字段：" + ", ".join(missing))

    code = _require_text(raw, "code", errors)
    if code and not CODE_PATTERN.match(code):
        errors.append("code 必须匹配 ^[a-z][a-z0-9_]{2,63}$")

    name = _require_text(raw, "name", errors)

    version = _require_text(raw, "version", errors)
    if version and not VERSION_PATTERN.match(version):
        errors.append("version 必须匹配 ^\\d+\\.\\d+\\.\\d+$")

    core_api_min = _require_text(raw, "core_api_min", errors)
    if core_api_min and not VERSION_PATTERN.match(core_api_min):
        errors.append("core_api_min 必须匹配 ^\\d+\\.\\d+\\.\\d+$")
    elif core_api_min:
        required = parse_version(core_api_min)
        current = parse_version(kernel_version())
        if required is None or current is None or required > current:
            errors.append(f"core_api_min={core_api_min} 高于内核 {kernel_version()}")

    entry_server = _relative_entry(_require_text(raw, "entry_server", errors), errors)

    modes = _text_list(raw, "supported_modes", errors)
    unknown_modes = [mode for mode in modes if mode not in SUPPORTED_MODES]
    if unknown_modes:
        errors.append("supported_modes 仅允许 internal/external：" + ", ".join(unknown_modes))
    if not modes:
        errors.append("supported_modes 至少声明一种模式")

    caps = _text_list(raw, "capabilities", errors)

    table_prefix = raw.get("table_prefix")
    if table_prefix is not None and not isinstance(table_prefix, str):
        errors.append("table_prefix 必须是字符串")
        table_prefix = None
    prefix = table_prefix.strip() if isinstance(table_prefix, str) else expected_table_prefix(code)
    if code and prefix != expected_table_prefix(code):
        raise AppError(
            PLUGIN_TABLE_PREFIX_INVALID,
            data={
                "code": code,
                "table_prefix": prefix,
                "expected": expected_table_prefix(code),
            },
        )

    routes_raw = raw.get("routes", [])
    if not isinstance(routes_raw, list) or not all(isinstance(item, str) for item in routes_raw):
        errors.append("routes 必须是字符串数组")
        routes_raw = []
    routes = [item.strip() for item in routes_raw]
    for route in routes:
        if not ROUTE_PATTERN.match(route):
            errors.append(f"route 不合法：{route!r}")

    menu_raw = raw.get("menu", [])
    if not isinstance(menu_raw, list):
        errors.append("menu 必须是数组")
        menu_raw = []
    menu: list[Mapping[str, Any]] = []
    for item in menu_raw:
        if not isinstance(item, Mapping) or not isinstance(item.get("title"), str):
            errors.append("menu 每项必须是含 title 的对象")
            break
        menu.append(dict(item))

    if errors:
        raise _invalid(errors)

    denied_caps = sorted(cap for cap in caps if cap not in ALLOWED_CAPABILITIES)
    if denied_caps:
        raise AppError(
            PLUGIN_CAP_DENIED,
            data={"code": code, "denied": denied_caps, "allowed": sorted(ALLOWED_CAPABILITIES)},
        )

    caps_v2_raw = raw.get("capabilities_v2", {})
    if not isinstance(caps_v2_raw, Mapping):
        raise _invalid(["capabilities_v2 必须是对象"])
    unknown_v2 = sorted(key for key in caps_v2_raw if key not in CAPABILITIES_V2_KEYS)
    if unknown_v2:
        raise _invalid(["capabilities_v2 未声明能力：" + ", ".join(unknown_v2)])
    enabled_v2 = sorted(
        key for key, value in caps_v2_raw.items() if not isinstance(value, bool) or value
    )
    if enabled_v2:
        raise AppError(PLUGIN_CAP_DENIED, data={"code": code, "denied": enabled_v2})

    return PluginManifest(
        code=code,
        name=name,
        version=version,
        core_api_min=core_api_min,
        entry_server=entry_server,
        supported_modes=tuple(modes),
        capabilities=tuple(caps),
        capabilities_v2=dict.fromkeys(sorted(caps_v2_raw), False),
        table_prefix=prefix,
        routes=tuple(routes),
        menu=tuple(menu),
    )


def load_manifest(plugin_dir: Path) -> PluginManifest:
    """读取 `<plugin_dir>/manifest.json` 并校验；目录/入口文件缺失同样拒绝。"""
    path = plugin_dir / MANIFEST_FILE
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise AppError(
            PLUGIN_MANIFEST_INVALID,
            data={"errors": [f"无法读取 {path.name}：{exc.strerror or exc}"]},
        ) from exc
    try:
        raw = json.loads(text)
    except json.JSONDecodeError as exc:
        raise _invalid([f"manifest 不是合法 JSON：{exc.msg}"]) from exc

    hits = banned_lines(text)
    if hits:
        raise _invalid(["manifest 源文本含方言裸写令牌（附录 E 黑名单）：" + f"第 {hits[0][0]} 行"])

    manifest = validate_manifest(raw)
    entry = plugin_dir / manifest.entry_server
    if not entry.is_file():
        raise _invalid([f"entry_server 不存在：{manifest.entry_server}"])
    return manifest
