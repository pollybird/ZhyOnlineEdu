#!/usr/bin/env python3
"""CI 门禁：迁移 revision 能力声明（§5.6 / D34）。

- 每个 revision 必须有 `# x-zhy-cap: returning:..., alter_type_online:...`；
- 直接 `ALTER COLUMN TYPE`（`type_=`）必须处于 batch 四步模板内。
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
VERSIONS = ROOT / "migrations" / "versions"


def main() -> int:
    sys.path.insert(0, str(ROOT))
    from zhyonlineedu.core.db.migration import MigrationCapError, validate_revision_file

    files = sorted(VERSIONS.glob("*.py"))
    if not files:
        print(f"check_migration_caps: {VERSIONS} 下无 revision", file=sys.stderr)
        return 1

    errors: list[str] = []
    for path in files:
        try:
            validate_revision_file(path)
        except MigrationCapError as exc:
            errors.append(str(exc))
    if errors:
        print("迁移能力声明/四步模板校验失败：", file=sys.stderr)
        print("\n".join(errors), file=sys.stderr)
        return 1
    print(f"check_migration_caps: OK（{len(files)} 个 revision）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
