"""M0 Spike · 插件加载可行性（§11 · F5）。

验证点（最小内核）：
  PLUGIN-001 加载：manifest 校验 → 注册路由/菜单 → 可访问
  PLUGIN-002 禁用：路由 404，内核无残留（不重启）
  PLUGIN-003 能力越权：manifest 声明 direct_write（V2.3 已删除）→ 拒绝加载 PLUGIN_CAP_DENIED
  PLUGIN-004 表前缀隔离：插件迁移建表必须带插件前缀，否则拒绝
  PLUGIN-005 事件订阅只读：插件通过 observe 拿副本，改副本不影响内核

运行：uv run pytest spike/plugin -q
"""

from __future__ import annotations

import json
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

ALLOWED_CAPS = {
    "route",
    "menu",
    "event_observe",
    "event_intercept",
    "issue_ticket",
    "evaluate",
    "compat_readonly",
    "storage_read",
    "storage_write_own",
}
DENIED_CAPS = {"direct_write", "direct_session", "raw_db", "bypass_license"}  # §11 SDK 收窄


@dataclass
class Plugin:
    key: str
    version: str
    caps: set[str]
    table_prefix: str
    routes: list[str] = field(default_factory=list)
    enabled: bool = True


class Kernel:
    """极简内核：路由表 + 事件总线 + 插件注册表。"""

    def __init__(self) -> None:
        self.routes: dict[str, str] = {}  # path -> plugin_key
        self.plugins: dict[str, Plugin] = {}
        self.audit: list[dict[str, Any]] = []

    def load(self, manifest: dict[str, Any]) -> Plugin:
        key = manifest["key"]
        caps = set(manifest.get("capabilities", []))
        denied = caps & DENIED_CAPS
        if denied:
            self.audit.append({"action": "load_denied", "key": key, "reason": sorted(denied)})
            raise PermissionError("PLUGIN_CAP_DENIED")
        unknown = caps - ALLOWED_CAPS
        if unknown:
            raise PermissionError(f"PLUGIN_CAP_UNKNOWN: {sorted(unknown)}")
        prefix = manifest.get("table_prefix", "")
        if not prefix.startswith(f"plg_{key.replace('-', '_')}_"):
            self.audit.append({"action": "load_denied", "key": key, "reason": "table_prefix"})
            raise ValueError("PLUGIN_TABLE_PREFIX_INVALID")
        plugin = Plugin(key=key, version=manifest["version"], caps=caps, table_prefix=prefix)
        for path in manifest.get("routes", []):
            self.routes[path] = key
            plugin.routes.append(path)
        self.plugins[key] = plugin
        self.audit.append({"action": "load", "key": key, "version": plugin.version})
        return plugin

    def disable(self, key: str) -> None:
        plugin = self.plugins.get(key)
        if not plugin:
            return
        for path in plugin.routes:
            self.routes.pop(path, None)
        plugin.enabled = False
        self.audit.append({"action": "disable", "key": key})

    def request(self, path: str) -> tuple[int, str]:
        owner = self.routes.get(path)
        if not owner or not self.plugins[owner].enabled:
            return (404, "NOT_FOUND")
        return (200, f"hello from {owner}")

    def emit(self, name: str, payload: dict[str, Any]) -> dict[str, Any]:
        """observe 语义：插件只拿副本，不得写回内核状态（§11）。"""
        out = dict(payload)
        for plugin in self.plugins.values():
            if not plugin.enabled or "event_observe" not in plugin.caps:
                continue
            handler = self._handler_for(plugin)
            if handler:
                handler(dict(out))  # 深一层的副本
        return out

    def _handler_for(self, plugin: Plugin):
        return None  # spike 用外部注入的 handler 代替


def manifest(
    key: str = "hello",
    caps: list[str] | None = None,
    prefix: str | None = None,
    routes: list[str] | None = None,
) -> dict[str, Any]:
    return {
        "key": key,
        "version": "0.1.0",
        "capabilities": caps if caps is not None else ["route", "menu", "event_observe"],
        "table_prefix": prefix if prefix is not None else f"plg_{key.replace('-', '_')}_",
        "routes": routes if routes is not None else [f"/plugins/{key}/hello"],
    }


# ── 用例 ─────────────────────────────────────────────────────────────


def test_plugin_001_load_and_serve() -> None:
    k = Kernel()
    k.load(manifest())
    assert k.request("/plugins/hello/hello") == (200, "hello from hello")
    assert k.plugins["hello"].enabled


def test_plugin_002_disable_removes_routes() -> None:
    k = Kernel()
    k.load(manifest())
    k.disable("hello")
    assert k.request("/plugins/hello/hello") == (404, "NOT_FOUND")
    assert any(a["action"] == "disable" for a in k.audit), "审计留痕"


def test_plugin_003_denied_capability() -> None:
    k = Kernel()
    try:
        k.load(manifest(caps=["route", "direct_write"]))
    except PermissionError as exc:
        assert str(exc) == "PLUGIN_CAP_DENIED"
    else:
        raise AssertionError("direct_write 必须被拒绝（§11 SDK 收窄）")
    assert "hello" not in k.plugins
    assert k.audit[-1]["reason"] == ["direct_write"]


def test_plugin_004_table_prefix_enforced() -> None:
    k = Kernel()
    for bad_prefix in ("", "hello_", "plg_other_"):
        try:
            k.load(manifest(prefix=bad_prefix))
        except ValueError as exc:
            assert str(exc) == "PLUGIN_TABLE_PREFIX_INVALID"
        else:
            raise AssertionError(f"前缀 {bad_prefix!r} 应被拒绝")


def test_plugin_005_observe_is_copy() -> None:
    k = Kernel()
    plugin = k.load(manifest())
    seen: list[dict[str, Any]] = []

    def handler(copy: dict[str, Any]) -> None:
        copy["position_sec"] = 99999  # 试图改内核状态
        seen.append(copy)

    k._handler_for = lambda _p: handler  # type: ignore[method-assign]
    core_state = {"user_id": 1, "position_sec": 42}
    result = k.emit("progress.reported", core_state)
    assert result["position_sec"] == 42, "插件写副本不得影响内核"
    assert seen and seen[0]["position_sec"] == 99999
    assert plugin.enabled


def test_plugin_006_manifest_json_roundtrip(tmp_path: Path) -> None:
    k = Kernel()
    path = tmp_path / "manifest.json"
    path.write_text(json.dumps(manifest(), ensure_ascii=False), encoding="utf-8")
    k.load(json.loads(path.read_text(encoding="utf-8")))
    assert k.request("/plugins/hello/hello")[0] == 200
