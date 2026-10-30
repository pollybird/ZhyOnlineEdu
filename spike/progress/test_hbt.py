"""M0 Spike · HBT 用例（§12.5 / §16：HBT-001~003/005）。

运行：uv run pytest spike/progress -q
用例：
  HBT-001 正常链路：N 条上报 → flush → 位置/时长正确，日志条数 = N
  HBT-002 乱序与重放：打乱 gw_seq、重复重放 → 位置不倒退、日志不重复
  HBT-003 kill -9：写满 N 条后被 SIGKILL → 重启 flush 全部补齐（msync=True 口径）
  HBT-004 preview 隔离：source=preview 不进统计
  HBT-005 单写者：两个 flush 并发，仅一个持租约写入
  HBT-006 覆盖/损坏槽：CRC 校验丢弃坏槽，不影响其余条目
"""

from __future__ import annotations

import os
import random
import signal
import sqlite3
import subprocess
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

from flush import flush_once  # noqa: E402
from ring import SLOT_COUNT, SOURCE_PLAY, Cmd, Ring, _new_seq  # noqa: E402


def write_cmds(
    ring_dir: Path, pids: int, count: int, base_pos: int = 10, msync: bool = True
) -> None:
    """pid 个 ring 各写 count 条（位置递增），模拟多 web worker。"""
    for p in range(pids):
        path = ring_dir / f"{1000 + p}.ring"
        with Ring(path) as ring:
            for i in range(count):
                ring.append(
                    Cmd(
                        gw_seq=_new_seq(),
                        user_id=1 + p,
                        lesson_id=1,
                        position_sec=base_pos + i,
                        watch_delta=1,
                        source=SOURCE_PLAY,
                        ts_ms=int(time.time() * 1000),
                    ),
                    msync=msync,
                )


def progress_row(db: Path, user_id: int = 1, lesson_id: int = 1) -> tuple[int, int, int]:
    with sqlite3.connect(db) as conn:
        row = conn.execute(
            "SELECT position_sec, watch_seconds, version FROM learn_progress "
            "WHERE user_id=? AND lesson_id=?",
            (user_id, lesson_id),
        ).fetchone()
    return row if row else (0, 0, 0)


def log_count(db: Path) -> int:
    with sqlite3.connect(db) as conn:
        return conn.execute("SELECT COUNT(*) FROM learn_progress_log").fetchone()[0]


def test_hbt_001_normal(tmp_path: Path) -> None:
    ring_dir, db = tmp_path / "ring", tmp_path / "p.db"
    ring_dir.mkdir()
    write_cmds(ring_dir, pids=2, count=50)
    stats = flush_once(ring_dir, db)
    assert stats is not None
    assert log_count(db) == 100
    pos, watch, ver = progress_row(db)
    assert pos == 59, "位置取最新上报"
    assert watch == 50
    assert ver == 50


def test_hbt_002_out_of_order_and_replay(tmp_path: Path) -> None:
    ring_dir, db = tmp_path / "ring", tmp_path / "p.db"
    ring_dir.mkdir()
    cmds = [
        Cmd(_new_seq(), 7, 1, 100 if i % 2 else 10 * i, 1, SOURCE_PLAY, int(time.time() * 1000))
        for i in range(20)
    ]
    random.shuffle(cmds)
    path = ring_dir / "2000.ring"
    with Ring(path) as ring:
        for cmd in cmds:
            ring.append(cmd)
        for cmd in cmds:  # 重放
            ring.append(cmd)
    flush_once(ring_dir, db)
    flush_once(ring_dir, db)  # 二次 flush 不应重复
    assert log_count(db) == 20
    pos, watch, _ = progress_row(db, 7, 1)
    assert pos == max(c.position_sec for c in cmds), "位置取最大值，不倒退"
    assert watch == 20


def test_hbt_003_kill9_recovery(tmp_path: Path) -> None:
    """子进程写满后 SIGKILL（next() 落盘口径）→ 重启 flush 补齐所有条目。"""
    ring_dir, db = tmp_path / "ring", tmp_path / "p.db"
    ring_dir.mkdir()
    child = subprocess.Popen(
        [
            sys.executable,
            "-c",
            f"""
import sys, time
sys.path.insert(0, {str(HERE)!r})
from pathlib import Path
from ring import Ring, Cmd, SOURCE_PLAY, _new_seq
with Ring(Path({str(ring_dir)!r}) / "3000.ring") as ring:
    for i in range(200):
        ring.append(Cmd(_new_seq(), 9, 1, i, 1, SOURCE_PLAY, int(time.time()*1000)))
print("READY", flush=True)
time.sleep(30)
""",
        ],
        stdout=subprocess.PIPE,
        text=True,
    )
    try:
        assert child.stdout is not None
        assert child.stdout.readline().strip() == "READY"
        os.kill(child.pid, signal.SIGKILL)  # kill -9，无 atexit
    finally:
        child.wait(timeout=5)
    stats = flush_once(ring_dir, db)
    assert stats is not None
    assert log_count(db) == 200, f"SIGKILL 后补齐条数={log_count(db)}"
    pos, _, _ = progress_row(db, 9, 1)
    assert pos == 199


def test_hbt_004_preview_excluded(tmp_path: Path) -> None:
    from ring import SOURCE_PREVIEW

    ring_dir, db = tmp_path / "ring", tmp_path / "p.db"
    ring_dir.mkdir()
    with Ring(ring_dir / "4000.ring") as ring:
        ring.append(Cmd(_new_seq(), 3, 1, 500, 60, SOURCE_PLAY, int(time.time() * 1000)))
        ring.append(Cmd(_new_seq(), 3, 1, 800, 900, SOURCE_PREVIEW, int(time.time() * 1000)))
    stats = flush_once(ring_dir, db)
    assert stats is not None
    assert stats["preview_skipped"] == 1
    pos, watch, _ = progress_row(db, 3, 1)
    assert (pos, watch) == (500, 60), "preview 不进入统计"


def test_hbt_005_single_writer(tmp_path: Path) -> None:
    ring_dir, db = tmp_path / "ring", tmp_path / "p.db"
    ring_dir.mkdir()
    write_cmds(ring_dir, pids=1, count=30)

    holder = subprocess.Popen(
        [
            sys.executable,
            "-c",
            f"""
import sys, time, fcntl
sys.path.insert(0, {str(HERE)!r})
from pathlib import Path
from flush import Lease
lease = Lease(Path({str(ring_dir)!r}) / "flush.lock")
assert lease.acquire()
print("HELD", flush=True)
time.sleep(5)
""",
        ],
        stdout=subprocess.PIPE,
        text=True,
    )
    try:
        assert holder.stdout is not None
        assert holder.stdout.readline().strip() == "HELD"
        assert flush_once(ring_dir, db) is None, "租约被占时不得写入"
        assert not db.exists() or log_count(db) == 0
    finally:
        holder.kill()
        holder.wait(timeout=5)
    assert flush_once(ring_dir, db) is not None, "租约释放后接管"
    assert log_count(db) == 30


def test_hbt_006_corrupt_slot_dropped(tmp_path: Path) -> None:
    ring_dir, db = tmp_path / "ring", tmp_path / "p.db"
    ring_dir.mkdir()
    path = ring_dir / "5000.ring"
    with Ring(path) as ring:
        for i in range(10):
            ring.append(Cmd(_new_seq(), 5, 1, i, 1, SOURCE_PLAY, int(time.time() * 1000)))
    # 人为破坏第 3 槽的 CRC（模拟掉电半写）
    with path.open("r+b") as fh:
        fh.seek(32 + 3 * 48 + 10)
        fh.write(b"\xff\xff")
    stats = flush_once(ring_dir, db)
    assert stats is not None
    assert log_count(db) == 9, "坏槽被丢弃，其余条目保留"


def test_hbt_007_ring_wrap(tmp_path: Path) -> None:
    """超出 slot_count 后覆盖写：只保留最近 SLOT_COUNT 条，且不倒退。"""
    ring_dir, db = tmp_path / "ring", tmp_path / "p.db"
    ring_dir.mkdir()
    with Ring(ring_dir / "6000.ring") as ring:
        for i in range(SLOT_COUNT + 50):
            ring.append(Cmd(_new_seq(), 6, 1, i, 1, SOURCE_PLAY, int(time.time() * 1000)))
        assert len(ring.read_all()) == SLOT_COUNT
    flush_once(ring_dir, db)
    pos, _, _ = progress_row(db, 6, 1)
    assert pos == SLOT_COUNT + 49
