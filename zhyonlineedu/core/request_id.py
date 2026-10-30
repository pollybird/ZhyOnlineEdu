"""X-ZHY-Request-Id 生成与校验（§26.1 冻结头名）。

规则：上游（Nginx）传入且合法则采信，否则服务端生成 `r_<16 hex>`。
"""

from __future__ import annotations

import re
import uuid

REQUEST_ID_PATTERN = re.compile(r"^[A-Za-z0-9._-]{8,64}$")


def new_request_id() -> str:
    return f"r_{uuid.uuid4().hex[:16]}"


def resolve_request_id(incoming: str | None) -> str:
    if incoming and REQUEST_ID_PATTERN.fullmatch(incoming):
        return incoming
    return new_request_id()
