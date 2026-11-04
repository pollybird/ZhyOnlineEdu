"""sample_hello：最小可加载插件（M3.7）。

演示三件事：
1. `HANDLERS` 的 key 必须与 manifest `routes` 一致（内核加载时双向校验）；
2. handler 返回值只描述 `data`，统一信封由内核包裹；
3. `setup()` 可选：本插件无需订阅事件，故省略。
"""

from __future__ import annotations

from typing import Any

from zhyonlineedu.core import sdk


def hello() -> dict[str, Any]:
    """只读能力示例：`sdk.compat.flavor()` 证明分发已绑定插件作用域。"""
    return {
        "message": "hello from sample_hello",
        "plugin": "sample_hello",
        "flavor": sdk.compat.flavor(),
    }


HANDLERS = {"hello": hello}
