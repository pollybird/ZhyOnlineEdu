#!/usr/bin/env python3
"""CI 红线：方言裸 SQL 扫描（§5.2 红线 / §4.2 红线 3）。

迁移或业务代码中出现以下 token 直接失败：
ON DUPLICATE KEY UPDATE / ON CONFLICT / RETURNING（裸写）/ ROWNUM / NVL / DECODE / (+)
白名单：core/db/upsert.py 内部允许按能力位生成；黑名单定义与插件安装校验共用
`zhyonlineedu/core/db/sql_guard.py`（附录 E：同一份黑名单，避免两处漂移）。
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from zhyonlineedu.core.db.sql_guard import banned_lines  # noqa: E402

WHITELIST = {str(ROOT / "zhyonlineedu/core/db/upsert.py")}


def scan() -> list[str]:
    hits: list[str] = []
    for path in list(ROOT.glob("zhyonlineedu/**/*.py")) + list(ROOT.glob("migrations/**/*.py")):
        if str(path) in WHITELIST:
            continue
        text = path.read_text(encoding="utf-8")
        for lineno, line in banned_lines(text):
            hits.append(f"{path.relative_to(ROOT)}:{lineno}: {line}")
    return hits


if __name__ == "__main__":
    bad = scan()
    if bad:
        print("方言红线命中（裸 SQL 禁止，一律走 core.db.upsert.upsert()）：")
        print("\n".join(bad))
        sys.exit(1)
    print("check_dialect_compat: OK")
