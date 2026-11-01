"""`sdk.compat`：方言与能力位的**只读**视图（M3.4 · §11.9）。

插件可据此做 `Kingbase` / `openGauss` 分支，但**没有** `configure()` / `reset()`，
也不允许插件执行 DDL——建表只能走插件独立迁移链（M3.3）。
"""

from __future__ import annotations

from collections.abc import Mapping

from zhyonlineedu.core.db import compat
from zhyonlineedu.core.sdk.context import current

__all__ = ["cap", "caps", "flavor"]


def flavor() -> str:
    """细粒度方言名（`sqlite` / `mysql` / `postgresql` / `opengauss_*` / `kingbase`）。"""
    current()
    return compat.flavor().value


def cap(name: str) -> bool:
    """能力位（安装期固化；只读）。"""
    current()
    return compat.cap(name)


def caps() -> Mapping[str, bool]:
    """全部能力位快照（用于插件一次性分支判断）。"""
    current()
    runtime = compat.current()
    return {key.value: value for key, value in runtime.caps.items()}
