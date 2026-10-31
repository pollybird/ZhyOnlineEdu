"""HBT-006 对账：`gw_seq` 无缺口、`seq_no` 连续、投影 `watch_seconds` 与客户端累计误差 0。

用法::

    source var/env.sh
    .venv/bin/python tests/perf/check_hbt006.py --expected var/perf/progress_expected.json

默认读取 `ZHY_DB_URL`（未设置时用 `sqlite:///./var/zhy.db`），仅支持 SQLite（本地压测窗口）。
脚本会等待 flush worker 把压测流水全部落库（默认最多 90s），再做四项断言。
"""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
import time
from pathlib import Path


def _sqlite_path(url: str) -> Path:
    prefix = "sqlite:///"
    if not url.startswith(prefix):
        raise SystemExit("仅支持 SQLite（本地压测窗口），请设置 ZHY_DB_URL=sqlite:///...")
    return Path(url[len(prefix) :])


def _connect(path: Path) -> sqlite3.Connection:
    connection = sqlite3.connect(path, timeout=30)
    connection.row_factory = sqlite3.Row
    return connection


def main() -> int:
    parser = argparse.ArgumentParser(description="HBT-006 压测结果对账")
    parser.add_argument("--expected", default="var/perf/progress_expected.json")
    parser.add_argument("--db-url", default=os.getenv("ZHY_DB_URL", "sqlite:///./var/zhy.db"))
    parser.add_argument("--flush-timeout", type=float, default=90.0)
    args = parser.parse_args()

    expected = json.loads(Path(args.expected).read_text(encoding="utf-8"))
    lesson_id = int(expected["lesson_id"])
    users: dict[str, dict[str, int]] = expected["users"]
    accepted_total = int(expected["accepted_total"])
    if not users:
        raise SystemExit("expected 文件为空：locust 是否真的跑过？")

    connection = _connect(_sqlite_path(args.db_url))
    marks = ",".join("?" * len(users))
    ids = {
        row["username"]: int(row["id"])
        for row in connection.execute(
            f"SELECT id, username FROM account_user WHERE username IN ({marks})",  # noqa: S608
            tuple(users),
        )
    }
    missing = sorted(set(users) - set(ids))
    if missing:
        raise SystemExit(f"数据库里找不到压测账号：{missing[:5]} ...")

    deadline = time.monotonic() + args.flush_timeout
    pending = 1
    while pending > 0 and time.monotonic() < deadline:
        pending = int(
            connection.execute(
                "SELECT COUNT(*) FROM learn_progress_log WHERE lesson_id = ? "  # noqa: S608
                f"AND flushed_at IS NULL AND user_id IN ({marks})",
                (lesson_id, *ids.values()),
            ).fetchone()[0]
        )
        if pending:
            time.sleep(2)

    failures: list[str] = []

    rows = list(
        connection.execute(
            "SELECT user_id, gw_seq, seq_no, played_seconds, quarantined, source "  # noqa: S608
            f"FROM learn_progress_log WHERE lesson_id = ? AND user_id IN ({marks}) "
            "ORDER BY gw_seq",
            (lesson_id, *ids.values()),
        )
    )
    if len(rows) != accepted_total:
        failures.append(f"log 行数 {len(rows)} != 客户端 accepted {accepted_total}")

    span = int(
        connection.execute(
            "SELECT MAX(gw_seq) - MIN(gw_seq) FROM learn_progress_log "
            "WHERE gw_seq BETWEEN ? AND ?",
            (
                min(int(row["gw_seq"]) for row in rows) if rows else 0,
                max(int(row["gw_seq"]) for row in rows) if rows else 0,
            ),
        ).fetchone()[0]
        or 0
    )
    global_count = int(
        connection.execute(
            "SELECT COUNT(*) FROM learn_progress_log WHERE gw_seq BETWEEN ? AND ?",
            (
                min(int(row["gw_seq"]) for row in rows) if rows else 0,
                max(int(row["gw_seq"]) for row in rows) if rows else 0,
            ),
        ).fetchone()[0]
    )
    if rows and span + 1 != global_count:
        failures.append(f"gw_seq 存在缺口：span={span + 1} 实际 {global_count}")

    by_user: dict[int, list[int]] = {}
    for row in rows:
        by_user.setdefault(int(row["user_id"]), []).append(int(row["seq_no"]))
    for user_id, seqs in by_user.items():
        if sorted(seqs) != list(range(1, len(seqs) + 1)):
            failures.append(f"user={user_id} seq_no 不连续（{len(seqs)} 条）")

    quarantined = sum(1 for row in rows if int(row["quarantined"] or 0) != 0)
    if quarantined:
        failures.append(f"存在 {quarantined} 条隔离流水")

    for username, user_id in ids.items():
        row = connection.execute(
            "SELECT watch_seconds FROM learn_progress WHERE user_id = ? AND lesson_id = ?",
            (user_id, lesson_id),
        ).fetchone()
        want = int(users[username]["expected_watch"])
        got = int(row["watch_seconds"]) if row is not None else 0
        if got != want:
            failures.append(f"{username}: watch_seconds {got} != 期望 {want}")

    print(
        f"用户 {len(ids)} / 上报 {accepted_total} 条 / 未落库 {pending} 条 / "
        f"gw_seq 跨度 {span + 1} / 投影对账 {'OK' if not failures else 'FAIL'}"
    )
    if failures:
        for line in failures[:20]:
            print(f"  - {line}", file=sys.stderr)
        return 1
    print("HBT-006 PASS：gw_seq 无缺口、seq_no 连续、watch_seconds 误差 0")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
