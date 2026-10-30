#!/usr/bin/env python3
"""打印指定 flavor 的 UPSERT 编译结果（§5.4 契约第 5 条：可文本比对）。

用法：
    python scripts/explain_upsert.py sqlite
    python scripts/explain_upsert.py opengauss_5

输出含 route、SQL、参数名、能力位依据，供评审肉眼核对，禁止黑盒拼接。
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from zhyonlineedu.core.db.compat import Cap, Flavor, flavor_spec  # noqa: E402
from zhyonlineedu.core.db.upsert import compile_upsert  # noqa: E402

CANONICAL = {
    "table": "learn_progress",
    "values": {"uid": 1, "lesson": 1, "pos": 100, "version": 1},
    "natkey": ["uid", "lesson"],
}


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("flavor", choices=[f.value for f in Flavor])
    parser.add_argument("--table", default=CANONICAL["table"])
    args = parser.parse_args()

    target = Flavor(args.flavor)
    spec = flavor_spec(target)
    compiled = compile_upsert(
        target,
        args.table,
        dict(CANONICAL["values"]),
        list(CANONICAL["natkey"]),
    )
    print(f"flavor={target.value} dialect={spec.dialect.value} route={compiled.route}")
    print("capabilities=")
    for capability in Cap:
        print(f"  {capability.value}={spec.caps[capability]}")
    print("sql:")
    print(compiled.sql)
    print(f"params={sorted(compiled.params)}")
    print(f"returning={compiled.returning}")
    if compiled.readback_sql:
        print(f"readback={compiled.readback_sql}")
    for note in compiled.notes:
        print(f"note: {note}")


if __name__ == "__main__":
    main()
