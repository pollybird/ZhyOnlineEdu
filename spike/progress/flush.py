"""M0 Spike · 单写者 flush + 幂等投影（§12.5 · F2）。

链路：all rings → 按 gw_seq 升序 → 追加 learn_progress_log（gw_seq 主键，INSERT OR IGNORE）
      → 投影 learn_progress（GREATEST 幂等）→ 记录 watermark。
租约：`flush.lock` 上的 flock(LOCK_EX|LOCK_NB)，保证同 shard 只有一名写者。
"""

from __future__ import annotations

import argparse
import fcntl
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from ring import SOURCE_PREVIEW, Cmd, Ring  # noqa: E402

SCHEMA = """
CREATE TABLE IF NOT EXISTS learn_progress (
  user_id     INTEGER NOT NULL,
  lesson_id   INTEGER NOT NULL,
  position_sec INTEGER NOT NULL DEFAULT 0,
  watch_seconds INTEGER NOT NULL DEFAULT 0,
  version     INTEGER NOT NULL DEFAULT 1,
  PRIMARY KEY (user_id, lesson_id)
);
CREATE TABLE IF NOT EXISTS learn_progress_log (
  gw_seq      INTEGER PRIMARY KEY,          -- 天然去重键
  user_id     INTEGER NOT NULL,
  lesson_id   INTEGER NOT NULL,
  position_sec INTEGER NOT NULL,
  watch_delta INTEGER NOT NULL,
  source      INTEGER NOT NULL,
  client_ts_ms INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS progress_watermark (
  shard     TEXT PRIMARY KEY,
  gw_seq    INTEGER NOT NULL,
  applied_at TEXT NOT NULL DEFAULT (datetime('now'))
);
"""


class Lease:
    """flock 租约；跨进程互斥，进程崩溃后内核自动释放（无过期时间依赖）。"""

    def __init__(self, path: Path) -> None:
        self.path = path
        self._fh = None

    def acquire(self) -> bool:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fh = self.path.open("a+")
        try:
            fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            fh.close()
            return False
        self._fh = fh
        return True

    def release(self) -> None:
        if self._fh is not None:
            fcntl.flock(self._fh.fileno(), fcntl.LOCK_UN)
            self._fh.close()
            self._fh = None


def connect(db: str | Path) -> sqlite3.Connection:
    conn = sqlite3.connect(db, isolation_level=None)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=FULL")
    conn.executescript(SCHEMA)
    return conn


def collect(ring_dir: Path) -> list[Cmd]:
    cmds: list[Cmd] = []
    for path in sorted(ring_dir.glob("*.ring")):
        try:
            with Ring(path, create=False) as ring:
                cmds.extend(ring.read_all())
        except (OSError, ValueError):
            continue  # 正在写入/被截断的文件跳过，下轮再读
    seen: set[int] = set()
    uniq: list[Cmd] = []
    for cmd in sorted(cmds, key=lambda c: c.gw_seq):
        if cmd.gw_seq in seen:
            continue
        seen.add(cmd.gw_seq)
        uniq.append(cmd)
    return uniq


def apply(conn: sqlite3.Connection, cmds: list[Cmd]) -> dict[str, int]:
    stats = {"log_inserted": 0, "progress_updated": 0, "preview_skipped": 0}
    for cmd in cmds:
        cur = conn.execute(
            "INSERT OR IGNORE INTO learn_progress_log "
            "(gw_seq,user_id,lesson_id,position_sec,watch_delta,source,client_ts_ms) "
            "VALUES (?,?,?,?,?,?,?)",
            (
                cmd.gw_seq,
                cmd.user_id,
                cmd.lesson_id,
                cmd.position_sec,
                cmd.watch_delta,
                cmd.source,
                cmd.ts_ms,
            ),
        )
        if cur.rowcount == 0:
            continue  # 重复上报，投影已含
        stats["log_inserted"] += 1
        if cmd.source == SOURCE_PREVIEW:
            stats["preview_skipped"] += 1
            continue
        # 幂等投影：GREATEST 语义（sqlite 用标量 max）保证位置与时长不倒退
        conn.execute(
            "INSERT INTO learn_progress (user_id,lesson_id,position_sec,watch_seconds,version) "
            "VALUES (?,?,?,?,1) "
            "ON CONFLICT(user_id,lesson_id) DO UPDATE SET "
            "position_sec = max(learn_progress.position_sec, excluded.position_sec), "
            "watch_seconds = learn_progress.watch_seconds + excluded.watch_seconds, "
            "version = learn_progress.version + 1",
            (cmd.user_id, cmd.lesson_id, cmd.position_sec, max(0, cmd.watch_delta)),
        )
        stats["progress_updated"] += 1
    if cmds:
        conn.execute(
            "INSERT INTO progress_watermark (shard, gw_seq) VALUES ('s0', ?) "
            "ON CONFLICT(shard) DO UPDATE SET gw_seq = excluded.gw_seq, "
            "applied_at = datetime('now')",
            (cmds[-1].gw_seq,),
        )
    return stats


def flush_once(ring_dir: str | Path, db: str | Path) -> dict[str, int] | None:
    ring_dir, db = Path(ring_dir), Path(db)
    lease = Lease(ring_dir / "flush.lock")
    if not lease.acquire():
        return None  # 已有写者（HBT-005）
    try:
        cmds = collect(ring_dir)
        conn = connect(db)
        try:
            conn.execute("BEGIN IMMEDIATE")
            stats = apply(conn, cmds)
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise
        finally:
            conn.close()
        return stats
    finally:
        lease.release()


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dir", required=True)
    ap.add_argument("--db", required=True)
    ap.add_argument("--loop", type=int, default=1, help="轮询次数（模拟常驻 worker）")
    args = ap.parse_args()
    for _ in range(args.loop):
        stats = flush_once(args.dir, args.db)
        print(stats)


if __name__ == "__main__":
    main()
