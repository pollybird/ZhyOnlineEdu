#!/usr/bin/env python3
"""版本号规则与一致性门禁。

规则（正式发布 1.0.0 之前）：
- 大版本号恒为 0（不对外承诺稳定 API）；
- 版本号 = ``0.<里程碑主号>.<里程碑次号>-<阶段>``，与《开发规划与里程碑》一一对应，
  例如完成里程碑 M2.15 → ``0.2.15-alpha``；
- 阶段后缀只允许 alpha / beta / rc；正式发布时改为 1.0.0；
- 唯一来源是 ``zhyonlineedu/__init__.py`` 的 ``__version__``，其余登记处必须同步。

用法：
    uv run python scripts/check_version.py
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
INIT_FILE = ROOT / "zhyonlineedu" / "__init__.py"
PYPROJECT = ROOT / "pyproject.toml"
PACKAGE_JSON = ROOT / "web" / "package.json"
PACKAGE_LOCK = ROOT / "web" / "package-lock.json"
OPENAPI = ROOT / "web" / "src" / "api" / "openapi.json"
UV_LOCK = ROOT / "uv.lock"

# 0.<里程碑主号>.<里程碑次号>-<阶段>；次号与阶段号可省略阶段序号
RULE = re.compile(r"^0\.(\d+)\.(\d+)-(alpha|beta|rc)(?:\.(\d+))?$")
INIT_RE = re.compile(r'^__version__\s*=\s*"([^"]+)"', re.MULTILINE)
PYPROJECT_RE = re.compile(r'^version\s*=\s*"([^"]+)"', re.MULTILINE)
# uv.lock 里项目自身条目（editable 包）；版本变更后必须 uv lock，否则 CI 的 --frozen 直接失败
UV_LOCK_ENTRY_RE = re.compile(r'^name = "zhyonlineedu"\nversion = "([^"]+)"', re.MULTILINE)


def normalize(version: str) -> str:
    """把展示串归一为 PEP 440 形式：``0.2.15-alpha`` → ``0.2.15a0``。"""
    matched = re.match(r"^(\d+\.\d+\.\d+)-(alpha|beta|rc)(?:\.(\d+))?$", version)
    if not matched:
        return version
    tag = {"alpha": "a", "beta": "b", "rc": "rc"}[matched.group(2)]
    return f"{matched.group(1)}{tag}{matched.group(3) or 0}"


def main() -> int:
    problems: list[str] = []

    matched = INIT_RE.search(INIT_FILE.read_text(encoding="utf-8"))
    if not matched:
        print(f"check_version: 未在 {INIT_FILE} 找到 __version__", file=sys.stderr)
        return 1
    canonical = matched.group(1)

    # 1. 命名规则
    rule = RULE.match(canonical)
    if not rule:
        problems.append(
            f"版本号 {canonical!r} 不符合规则：正式发布前应为 "
            "0.<里程碑主号>.<里程碑次号>-alpha|beta|rc（如 0.2.15-alpha）"
        )

    # 2. pyproject.toml（PEP 440 归一后比较，0.2.15-alpha 与 0.2.15a0 等价）
    pyproject = PYPROJECT_RE.search(PYPROJECT.read_text(encoding="utf-8"))
    if not pyproject:
        problems.append("pyproject.toml 缺少 [project].version")
    elif normalize(pyproject.group(1)) != normalize(canonical):
        problems.append(f"pyproject.toml version={pyproject.group(1)!r} 与 {canonical!r} 不一致")

    # 3. web/package.json 与 lockfile
    for path in (PACKAGE_JSON, PACKAGE_LOCK):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:  # pragma: no cover - 文件损坏
            problems.append(f"{path.relative_to(ROOT)} 读取失败：{exc}")
            continue
        actual = data.get("version") or data.get("packages", {}).get("", {}).get("version")
        if actual != canonical:
            problems.append(f"{path.relative_to(ROOT)} version={actual!r} 与 {canonical!r} 不一致")

    # 4. 前端 OpenAPI 生成物
    try:
        spec = json.loads(OPENAPI.read_text(encoding="utf-8"))
        spec_version = spec.get("info", {}).get("version")
    except (OSError, json.JSONDecodeError) as exc:  # pragma: no cover
        problems.append(f"{OPENAPI.relative_to(ROOT)} 读取失败：{exc}")
    else:
        if spec_version != canonical:
            problems.append(
                f"web/src/api/openapi.json info.version={spec_version!r} 与 {canonical!r} 不一致；"
                "请运行 scripts/export_openapi.py"
            )

    # 5. uv.lock 里的项目自身条目（CI 用 `uv sync --frozen`，版本不同步会直接失败）
    try:
        lock_text = UV_LOCK.read_text(encoding="utf-8")
    except OSError as exc:  # pragma: no cover - 文件缺失
        problems.append(f"uv.lock 读取失败：{exc}")
    else:
        entry = UV_LOCK_ENTRY_RE.search(lock_text)
        if not entry:
            problems.append("uv.lock 缺少 zhyonlineedu 条目")
        elif normalize(entry.group(1)) != normalize(canonical):
            problems.append(
                f"uv.lock zhyonlineedu version={entry.group(1)!r} 与 {canonical!r} 不一致；"
                "请运行 uv lock"
            )

    if problems:
        for item in problems:
            print(f"check_version: {item}", file=sys.stderr)
        return 1

    milestone = f"M{rule.group(1)}.{rule.group(2)}" if rule else "?"
    print(f"check_version: {canonical} 一致（对应里程碑 {milestone}）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
