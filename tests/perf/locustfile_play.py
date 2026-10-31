"""播放链路压测（M2.16 · MEDIA/CDN 口径）。

用法::

    source var/env.sh
    .venv/bin/python tests/perf/seed_progress.py --users 100   # 复用同一批学员账号
    .venv/bin/locust -f tests/perf/locustfile_play.py --headless \
        -u 300 -r 30 -t 2m --csv var/perf/play --only-summary

说明：
- 每个虚拟用户登录后取一次票，从 master 发现变体与分片，然后按 9:1 拉分片/播放列表；
- 分片是 `public, max-age` 长缓存（设计上由 CDN 承载），本脚本只测**回源**能力，
  CDN 命中率需在部署环境用 CDN mock/日志统计（本地不伪造该指标）；
- 3000 并发需多机或多进程，本机建议先跑 300 并发观察 P95 与错误率。

环境变量：`ZHY_PERF_BASE`、`ZHY_PERF_LESSON_ID`、`ZHY_PERF_USER_PREFIX`、
`ZHY_PERF_PASSWORD`（与 `seed_progress.py` 一致）。
"""

from __future__ import annotations

import itertools
import os
import threading
import time

from locust import HttpUser, constant, task
from locust.exception import StopUser

BASE_URL = os.getenv("ZHY_PERF_BASE", "http://127.0.0.1:5000")
LESSON_ID = int(os.getenv("ZHY_PERF_LESSON_ID", "2"))
PREFIX = os.getenv("ZHY_PERF_USER_PREFIX", "perf")
PASSWORD = os.getenv("ZHY_PERF_PASSWORD", "Perf-pass-2026")  # noqa: S105
MAX_SEGMENT_FALLBACK = 8
LOGIN_RETRY = 3

_LOCK = threading.Lock()
_NEXT = itertools.count(1)


def _next_username() -> str:
    with _LOCK:
        return f"{PREFIX}{next(_NEXT):04d}"


class PlayUser(HttpUser):
    """登录 → 取票 → 按 master 发现的分片循环拉取。"""

    host = BASE_URL
    wait_time = constant(1)

    def on_start(self) -> None:
        self.username = _next_username()
        self.token = ""
        self.kid = ""
        self.segments: list[str] = []
        self.master_url = ""
        self.cursor = 0
        if not self.login():
            raise StopUser
        self.fetch_ticket()

    def login(self) -> bool:
        """登录失败只退避重试，仍失败仅停该虚拟用户（不终止整场压测）。"""
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

    def fetch_ticket(self) -> None:
        headers = {"Authorization": f"Bearer {self.token}"}
        with self.client.post(
            "/api/v1/media/tickets",
            json={"lesson_id": LESSON_ID},
            headers=headers,
            name="POST /media/tickets",
            catch_response=True,
        ) as response:
            if response.status_code != 200:
                response.failure(f"ticket {response.status_code}")
                return
            data = response.json()["data"]
            self.kid = str(data["kid"])
            self.master_url = str(data["playlist_url"])
            profile = str((data.get("profiles") or ["360"])[0])
        with self.client.get(
            self.master_url, name="GET /media/playlist (master)", catch_response=True
        ) as master:
            if master.status_code != 200:
                master.failure(f"master {master.status_code}")
                return
            variants = [
                line for line in master.text.splitlines() if line.startswith("/api/v1/media/")
            ]
        if not variants:
            return
        with self.client.get(
            variants[0], name="GET /media/playlist (variant)", catch_response=True
        ) as variant:
            if variant.status_code != 200:
                variant.failure(f"variant {variant.status_code}")
                return
            self.segments = [
                line for line in variant.text.splitlines() if line.startswith("/api/v1/media/seg/")
            ]
        if not self.segments:
            self.segments = [
                f"/api/v1/media/seg/{self.kid}/{profile}/seg_{index:03d}.ts"
                for index in range(MAX_SEGMENT_FALLBACK)
            ]

    @task(9)
    def segment(self) -> None:
        if not self.segments:
            return
        path = self.segments[self.cursor % len(self.segments)]
        self.cursor += 1
        with self.client.get(path, name="GET /media/seg (origin)", catch_response=True) as response:
            if response.status_code not in (200, 206):
                response.failure(f"{response.status_code}")

    @task(1)
    def playlist(self) -> None:
        if not self.master_url:
            return
        with self.client.get(
            self.master_url, name="GET /media/playlist (master)", catch_response=True
        ) as response:
            if response.status_code != 200:
                response.failure(f"{response.status_code}")
