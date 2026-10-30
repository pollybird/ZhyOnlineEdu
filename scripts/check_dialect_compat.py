#!/usr/bin/env python3
"""CI 红线：方言裸 SQL 扫描（§5.2 [V2.3] 红线 / §4.2 红线 3）。

迁移或业务代码中出现以下 token 直接失败：
ON DUPLICATE KEY UPDATE / ON CONFLICT / RETURNING（裸写）/ ROWNUM / NVL / DECODE / (+)
白名单：core/db/upsert.py 内部允许按能力位生成；core/db/probe.py 仅安装期探测
（连接测试 + 临时表冒烟），复用 upsert 编译产物，不得被业务路径调用。
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
BANNED = re.compile(
    r"ON\s+DUPLICATE\s+KEY\s+UPDATE|ON\s+CONFLICT|\bROWNUM\b|\bNVL\b|\bDECODE\b|\(\+\)",
    re.IGNORECASE,
)
# 仅当 RETURNING 后紧跟列标识符时才算裸 SQL；Python 标识符
# （Cap.RETURNING / returning=True / 说明文字）不算。
BANNED_RETURNING = re.compile(r"\bRETURNING\s+[A-Za-z_\"*]", re.IGNORECASE)
# Python 的 `bytes.decode(...)` / PyJWT 的 `jwt.decode(...)` 与 Oracle DECODE 同名，
# 属库调用而非 SQL（带点号的方法调用统一剔除；裸 `DECODE(col, ...)` 仍会命中）
PY_DECODE = re.compile(r"\.decode\b", re.IGNORECASE)
# 常量定义行（如 ZHY_CAP_RETURNING = "returning"）
CONST_DEF = re.compile(r'^\s*[A-Z_][A-Z0-9_]*\s*=\s*["\']')
WHITELIST = {str(ROOT / "zhyonlineedu/core/db/upsert.py")}


def _strip_comments_docstrings(text: str) -> list[str]:
    """去掉注释与 docstring，返回逐行"有效代码"（保留行号对齐）。"""
    out: list[str] = []
    in_doc = False
    for line in text.splitlines():
        s = line.strip()
        if s.count('"""') % 2 == 1 or s.count("'''") % 2 == 1:
            in_doc = not in_doc
            out.append("")
            continue
        out.append("" if in_doc else line.split("#", 1)[0])
    return out


def scan() -> list[str]:
    hits: list[str] = []
    for path in list(ROOT.glob("zhyonlineedu/**/*.py")) + list(ROOT.glob("migrations/**/*.py")):
        if str(path) in WHITELIST:
            continue
        for lineno, line in enumerate(
            _strip_comments_docstrings(path.read_text(encoding="utf-8")), 1
        ):
            scrubbed = PY_DECODE.sub("", line)
            if not BANNED.search(scrubbed) and not BANNED_RETURNING.search(scrubbed):
                continue
            if CONST_DEF.match(line):
                continue
            # 字符串内 token 需伴随 SQL 动词才判定为 SQL 片段；标识符位置出现直接命中
            hits.append(f"{path.relative_to(ROOT)}:{lineno}: {line.strip()}")
    return hits


if __name__ == "__main__":
    bad = scan()
    if bad:
        print("方言红线命中（裸 SQL 禁止，一律走 core.db.upsert.upsert()）：")
        print("\n".join(bad))
        sys.exit(1)
    print("check_dialect_compat: OK")
