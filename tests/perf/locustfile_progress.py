"""HBT-006 · 进度上报 100 QPS 压测（M2.16 压测窗口）。

用法（先 `source var/env.sh` 并确保后端在跑）::

    .venv/bin/python tests/perf/seed_progress.py --users 120
    .venv/bin/locust -f tests/perf/locustfile_progress.py --headless \
        -u 120 -r 20 -t 5m --csv var/perf/progress --only-summary --stop-timeout 20
    .venv/bin/python tests/perf/check_hbt006.py --expected var/perf/progress_expected.json

登录按 `-r 20` 分摊启动（瞬时 120 个 Argon2 并发登录会挤占 CPU 并撞 SQLite 写锁），
登录失败自动退避重试 3 次；仍失败只停该虚拟用户，不终止整场压测。

口径（§16.4 用例 ⑥）：
- 每个虚拟用户独占一个账号（`ZHY_PERF_USER_PREFIX` + 序号），`on_start` 登录取 AT；
- `wait_time = constant_pacing(1)`：每个用户每 1s 发起一次上报，**100 用户即 100 QPS 目标**；
  位置按 1s 递增，到课时时长后回绕；
- 服务端按 30s 位置桶去重，客户端据响应里的 `deduped` 累计「应计入时长」，
  `quitting` 时写 `ZHY_PERF_OUT/progress_expected.json`，由 `check_hbt006.py` 对账；
- 必须单进程运行（账号序号在进程内分配）；`--stop-timeout` 保证收尾任务完成。

环境变量：
- `ZHY_PERF_BASE`（默认 `http://127.0.0.1:5000`）
- `ZHY_PERF_LESSON_ID`（默认 `2`，需为已上架课程的已编码课时）
- `ZHY_PERF_LESSON_DURATION`（默认 `144`，用于位置回绕；不会超过服务端上限）
- `ZHY_PERF_USER_PREFIX`（默认 `perf`）
- `ZHY_PERF_PASSWORD`（默认 `Perf-pass-2026`，与 seed 脚本一致）
"""

from __future__ import annotations

import itertools
import json
import os
import threading
import time
from pathlib import Path
from typing import Any

from locust import HttpUser, constant_pacing, events, task
from locust.exception import StopUser

BASE_URL = os.getenv("ZHY_PERF_BASE", "http://127.0.0.1:5000")
LESSON_ID = int(os.getenv("ZHY_PERF_LESSON_ID", "2"))
LESSON_DURATION = int(os.getenv("ZHY_PERF_LESSON_DURATION", "144"))
PREFIX = os.getenv("ZHY_PERF_USER_PREFIX", "perf")
PASSWORD = os.getenv("ZHY_PERF_PASSWORD", "Perf-pass-2026")  # noqa: S105 - 压测账号约定口令
OUT_DIR = Path(os.getenv("ZHY_PERF_OUT", "var/perf"))
DRAIN_TIMEOUT_SEC = float(os.getenv("ZHY_PERF_DRAIN_TIMEOUT", "15"))
LOGIN_RETRY = 3

_LOCK = threading.Lock()
_NEXT = itertools.count(1)
_ACTIVE: dict[str, Any] = {}
_ACCEPTED_TOTAL = 0
_INFLIGHT = 0


def _next_username() -> str:
    with _LOCK:
        return f"{PREFIX}{next(_NEXT):04d}"


def _take_username() -> str:
    return _next_username()


class ProgressUser(HttpUser):
    """登录 → 每用户每 1s 发起一次上报（100 用户 = 100 QPS 目标）。"""

    host = BASE_URL
    wait_time = constant_pacing(1)

    def on_start(self) -> None:
        self.username = _take_username()
        self.token = ""
        self.position = 0
        self.expected_watch = 0
        self.accepted = 0
        if not self.login():
            raise StopUser
        with _LOCK:
            _ACTIVE[self.username] = self

    def login(self) -> bool:
        """登录失败只退避重试，不拖垮整个压测（瞬时并发登录会撞 SQLite 写锁）。"""
        for attempt in range(1, LOGIN_RETRY + 1):
            with self.client.post(
                "/api/v1/auth/login",
                json={"username": self.username, "password": PASSWORD},
                name="POST /auth/login",
                catch_response=True,
            ) as response:
                if response.status_code == 200:
                    self.token = str(response.json()["data"]["access_token"])
                    return True
                response.failure(f"login {response.status_code}")
                if attempt == LOGIN_RETRY:
                    return False
            time.sleep(0.5 * attempt)
        return False

    def _headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.token}"}

    def _report(self) -> None:
        global _ACCEPTED_TOTAL, _INFLIGHT
        self.position = self.position + 1 if self.position < LESSON_DURATION else 0
        body: dict[str, Any] = {
            "lesson_id": LESSON_ID,
            "position_sec": self.position,
            "played_seconds": 1,
            "speed": 1.0,
            "client_ts_ms": 0,
        }
        with _LOCK:
            _INFLIGHT += 1
        try:
            with self.client.post(
                "/api/v1/learn/progress",
                json=body,
                headers=self._headers(),
                name="POST /learn/progress",
                catch_response=True,
            ) as response:
                if response.status_code == 401:
                    response.failure("401 重新登录，本条作废")
                    if not self.login():
                        raise StopUser
                    return
                if response.status_code != 202:
                    response.failure(f"{response.status_code} {response.text[:120]}")
                    return
                data = response.json()["data"]
                self.accepted += 1
                with _LOCK:
                    _ACCEPTED_TOTAL += 1
                if not data.get("deduped", False):
                    self.expected_watch += 1
        finally:
            with _LOCK:
                _INFLIGHT -= 1

    @task
    def report(self) -> None:
        self._report()


@events.quitting.add_listener
def _dump_expected(environment: Any, **_kwargs: Any) -> None:
    """把客户端口径写盘，供 check_hbt006.py 与投影表逐用户对账。

    先等在途请求收干（`--stop-timeout` 之外的双保险），否则最后一次上报可能已
    在服务端落库，却没进入本快照，导致「log 行数比 accepted 多几条」的假告警。
    """
    deadline = time.monotonic() + DRAIN_TIMEOUT_SEC
    while time.monotonic() < deadline:
        with _LOCK:
            if _INFLIGHT == 0:
                break
        time.sleep(0.1)
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    users = {
        name: {"expected_watch": user.expected_watch, "accepted": user.accepted}
        for name, user in _ACTIVE.items()
    }
    payload = {
        "lesson_id": LESSON_ID,
        "lesson_duration": LESSON_DURATION,
        "accepted_total": _ACCEPTED_TOTAL,
        "users": users,
    }
    target = OUT_DIR / "progress_expected.json"
    target.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"expected 已写入 {target}（用户 {len(users)}，accepted={_ACCEPTED_TOTAL}）")
