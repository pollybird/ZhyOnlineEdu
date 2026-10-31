"""HBT-006 压测账号准备（幂等）。

用法::

    source var/env.sh
    .venv/bin/python tests/perf/seed_progress.py --users 100

环境变量：`ZHY_PERF_BASE`、`ZHY_PERF_ADMIN_USER`、`ZHY_PERF_ADMIN_PASSWORD`、
`ZHY_PERF_USER_PREFIX`、`ZHY_PERF_PASSWORD`（与 locustfile 保持一致）。
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request

BASE_URL = os.getenv("ZHY_PERF_BASE", "http://127.0.0.1:5000")
ADMIN_USER = os.getenv("ZHY_PERF_ADMIN_USER", "admin")
ADMIN_PASSWORD = os.getenv("ZHY_PERF_ADMIN_PASSWORD", "Admin@Zhy2026")  # noqa: S105
PREFIX = os.getenv("ZHY_PERF_USER_PREFIX", "perf")
PASSWORD = os.getenv("ZHY_PERF_PASSWORD", "Perf-pass-2026")  # noqa: S105


def _call(
    method: str,
    path: str,
    *,
    token: str | None = None,
    body: dict[str, object] | None = None,
) -> tuple[int, dict[str, object]]:
    data = None if body is None else json.dumps(body).encode("utf-8")
    request = urllib.request.Request(  # noqa: S310 - 仅本机压测地址
        f"{BASE_URL}{path}",
        data=data,
        method=method,
        headers={
            "Content-Type": "application/json",
            **({"Authorization": f"Bearer {token}"} if token else {}),
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=10) as response:  # noqa: S310
            return response.status, json.loads(response.read() or b"{}")
    except urllib.error.HTTPError as error:
        return error.code, json.loads(error.read() or b"{}")


def _login(username: str, password: str) -> str:
    status, payload = _call(
        "POST", "/api/v1/auth/login", body={"username": username, "password": password}
    )
    if status != 200:
        raise SystemExit(f"登录失败 {username}: {status} {payload}")
    return str(payload["data"]["access_token"])


def _exists(token: str, username: str) -> bool:
    query = urllib.parse.urlencode({"keyword": username, "page_size": 50})
    status, payload = _call("GET", f"/api/v1/users?{query}", token=token)
    if status != 200:
        raise SystemExit(f"查询用户失败: {status} {payload}")
    items = payload["data"]["items"]
    return any(item.get("username") == username for item in items)


def main() -> int:
    parser = argparse.ArgumentParser(description="创建 HBT-006 压测学员账号")
    parser.add_argument("--users", type=int, default=100)
    args = parser.parse_args()

    token = _login(ADMIN_USER, ADMIN_PASSWORD)
    created = 0
    skipped = 0
    for index in range(1, args.users + 1):
        username = f"{PREFIX}{index:04d}"
        if _exists(token, username):
            skipped += 1
            continue
        status, payload = _call(
            "POST",
            "/api/v1/users",
            token=token,
            body={"username": username, "password": PASSWORD, "roles": ["student"]},
        )
        if status != 201:
            raise SystemExit(f"创建 {username} 失败: {status} {payload}")
        created += 1
    print(
        f"压测账号就绪：新建 {created}，已存在 {skipped}（前缀 {PREFIX}，共 {args.users}）",
        file=sys.stdout,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
