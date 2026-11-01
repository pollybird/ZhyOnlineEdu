"""方言裸 SQL 黑名单（CI 与插件安装校验共用同一份，§5.2 红线 / 附录 E）。

- `scripts/check_dialect_compat.py` 用本模块扫描仓库源码；
- 插件 manifest / 迁移源码在安装期用同一黑名单扫描，命中即拒装，
  避免"插件把方言 SQL 藏进字符串拼接"。

黑名单只描述 token，不负责解释上下文；调用方负责剔除注释与 `.decode()` 等库调用。
"""

from __future__ import annotations

import re

# 裸写方言 SQL 的 token（大小写不敏感）
BANNED_SQL = re.compile(
    r"ON\s+DUPLICATE\s+KEY\s+UPDATE|ON\s+CONFLICT|\bROWNUM\b|\bNVL\b|\bDECODE\b|\(\+\)",
    re.IGNORECASE,
)
# 仅当 RETURNING 后紧跟列标识符/`*` 时才算裸 SQL；Python 标识符与说明文字不算
BANNED_RETURNING = re.compile(r"\bRETURNING\s+[A-Za-z_\"*]", re.IGNORECASE)
# Python 的 `bytes.decode(...)` / PyJWT 的 `jwt.decode(...)` 与 Oracle DECODE 同名，属库调用
PY_DECODE = re.compile(r"\.decode\b", re.IGNORECASE)
# 常量定义行（如 ZHY_CAP_RETURNING = "returning"）不算 SQL
CONST_DEF = re.compile(r'^\s*[A-Z_][A-Z0-9_]*\s*=\s*["\']')


def strip_comments(text: str) -> list[str]:
    """去掉 `#` 注释与 docstring，返回逐行"有效代码"（保留行号对齐）。"""
    out: list[str] = []
    in_doc = False
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.count('"""') % 2 == 1 or stripped.count("'''") % 2 == 1:
            in_doc = not in_doc
            out.append("")
            continue
        out.append("" if in_doc else line.split("#", 1)[0])
    return out


def banned_lines(text: str) -> list[tuple[int, str]]:
    """返回命中黑名单的行 `(行号, 原文)`；行号从 1 开始，与文件行号对齐。"""
    hits: list[tuple[int, str]] = []
    for lineno, line in enumerate(strip_comments(text), 1):
        scrubbed = PY_DECODE.sub("", line)
        if not BANNED_SQL.search(scrubbed) and not BANNED_RETURNING.search(scrubbed):
            continue
        if CONST_DEF.match(line):
            continue
        hits.append((lineno, line.strip()))
    return hits


def contains_banned_sql(text: str) -> bool:
    return bool(banned_lines(text))
