#!/usr/bin/env python3
"""CI 红线：进度单调性扫描（§4.2 红线 2 · F2）。

learn_progress / learn_course_progress 的写入必须形如
  UPDATE ... SET watch_seconds = GREATEST(watch_seconds, :pos)
出现裸 `= :pos` 赋值的迁移与代码直接失败。
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
# 命中"进度表"且出现裸赋值（无 GREATEST 包裹）即违规
TABLE = re.compile(r"learn_(course_)?progress\b", re.IGNORECASE)
BARE_ASSIGN = re.compile(r"\b(watch_seconds|last_position)\s*=\s*:(\w+)\b", re.IGNORECASE)
GREATEST = re.compile(r"GREATEST\s*\(", re.IGNORECASE)


def scan() -> list[str]:
    hits: list[str] = []
    for path in list(ROOT.glob("zhyonlineedu/**/*.py")) + list(ROOT.glob("migrations/**/*.py")):
        text = path.read_text(encoding="utf-8")
        if not TABLE.search(text):
            continue
        for lineno, line in enumerate(text.splitlines(), 1):
            if BARE_ASSIGN.search(line) and not GREATEST.search(line):
                hits.append(f"{path.relative_to(ROOT)}:{lineno}: {line.strip()}")
    return hits


if __name__ == "__main__":
    bad = scan()
    if bad:
        print("进度单调性红线命中（必须 GREATEST 幂等写入）：")
        print("\n".join(bad))
        sys.exit(1)
    print("check_progress_monotonic: OK")
