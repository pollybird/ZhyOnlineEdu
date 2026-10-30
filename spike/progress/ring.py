"""M0 Spike · LocalBus 环形缓冲（§12.5 · F2）。

每个 web worker 独占一个 `<pid>.ring`：
  头部 64B：magic(8) ver(u32) slot_size(u32) slot_count(u32) head(u64) owned_pid(u64)
  槽位定长 56B：gw_seq u64 | user_id u64 | lesson_id u64 | position u32 |
                watch_delta u32 | source u8 | pad u8 | ts_ms u64 | crc32 u32

契约：
- 追加是无锁单写（每文件仅属主进程写）；消费方只读 head 之前的槽；
- 槽位覆盖写：消费方靠 `gw_seq` 判重 + 落库 `GREATEST` 保证不倒退（覆盖只影响重复，不影响正确性）；
- `msync=True` 每条落盘 → kill -9 零丢失；关闭则丢失 ≤ 一个 flush 周期（§12.5 承诺口径）。
"""

from __future__ import annotations

import mmap
import os
import struct
import threading
import time
import zlib
from dataclasses import dataclass
from pathlib import Path

SOURCE_PLAY, SOURCE_PREVIEW = 0, 1
_SEQ_LOCK = threading.Lock()
_SEQ_COUNTER = 0

MAGIC = b"ZHYRING1"
HEAD_FMT = "<8sIIIQQ"  # magic ver slot_size slot_count head owned_pid
HEAD_SIZE = struct.calcsize(HEAD_FMT)  # 32
SLOT_FMT = "<QQQII BB Q I".replace(" ", "")  # gw_seq uid lesson pos delta src pad ts crc
SLOT_SIZE = struct.calcsize(SLOT_FMT)  # 48
SLOT_COUNT = 4096


def _new_seq() -> int:
    """gw_seq 生成：41 位毫秒时间戳 << 22 | 进程号 10 位 << 12 | 进程内计数器 12 位。

    跨进程按时间有序、同毫秒内唯一，不依赖中心分配，免 fsync（落在 u64 内）。
    """
    global _SEQ_COUNTER
    with _SEQ_LOCK:
        _SEQ_COUNTER = (_SEQ_COUNTER + 1) & 0xFFF
        counter = _SEQ_COUNTER
    return (int(time.time() * 1000) << 22) | ((os.getpid() & 0x3FF) << 12) | counter


@dataclass(frozen=True)
class Cmd:
    gw_seq: int
    user_id: int
    lesson_id: int
    position_sec: int
    watch_delta: int
    source: int
    ts_ms: int


class Ring:
    def __init__(self, path: str | Path, create: bool = True) -> None:
        self.path = Path(path)
        self._fd = -1
        self._map: mmap.mmap | None = None
        if create:
            self._init_file()
        self._open()

    def _init_file(self) -> None:
        if self.path.exists() and self.path.stat().st_size >= HEAD_SIZE + SLOT_SIZE * SLOT_COUNT:
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("wb") as fh:
            fh.write(struct.pack(HEAD_FMT, MAGIC, 1, SLOT_SIZE, SLOT_COUNT, 0, os.getpid()))
            fh.write(b"\x00" * (SLOT_SIZE * SLOT_COUNT))
            fh.flush()
            os.fsync(fh.fileno())

    def _open(self) -> None:
        self._fd = os.open(self.path, os.O_RDWR)
        self._map = mmap.mmap(self._fd, HEAD_SIZE + SLOT_SIZE * SLOT_COUNT)

    @property
    def head(self) -> int:
        assert self._map is not None
        *_, head, _pid = struct.unpack(HEAD_FMT, self._map[:HEAD_SIZE])
        return head

    def append(self, cmd: Cmd, msync: bool = True) -> None:
        assert self._map is not None
        head = self.head
        payload = struct.pack(
            SLOT_FMT,
            cmd.gw_seq,
            cmd.user_id,
            cmd.lesson_id,
            cmd.position_sec,
            cmd.watch_delta,
            cmd.source,
            0,
            cmd.ts_ms,
            0,
        )
        body, crc = payload[:-4], zlib.crc32(payload[:-4]) & 0xFFFFFFFF
        slot = body + struct.pack("<I", crc)
        off = HEAD_SIZE + (head % SLOT_COUNT) * SLOT_SIZE
        self._map[off : off + SLOT_SIZE] = slot
        self._map[:HEAD_SIZE] = struct.pack(
            HEAD_FMT, MAGIC, 1, SLOT_SIZE, SLOT_COUNT, head + 1, os.getpid()
        )
        if msync:
            self._map.flush()
            os.fsync(self._fd)

    def read_all(self) -> list[Cmd]:
        """按 head 反向取最多 SLOT_COUNT 条；CRC 校验，覆盖导致的坏槽丢弃。"""
        assert self._map is not None
        head = self.head
        out: list[Cmd] = []
        start = max(0, head - SLOT_COUNT)
        for seq in range(start, head):
            off = HEAD_SIZE + (seq % SLOT_COUNT) * SLOT_SIZE
            raw = self._map[off : off + SLOT_SIZE]
            body = raw[:-4]
            crc = struct.unpack("<I", raw[-4:])[0]
            if zlib.crc32(body) & 0xFFFFFFFF != crc:
                continue
            gw, uid, lid, pos, delta, src, _pad, ts = struct.unpack(SLOT_FMT[:-1], body)
            if gw == 0:  # 未写入/已被覆盖
                continue
            out.append(Cmd(gw, uid, lid, pos, delta, src, ts))
        return out

    def close(self) -> None:
        if self._map is not None:
            self._map.flush()
            self._map.close()
            self._map = None
        if self._fd >= 0:
            os.close(self._fd)
            self._fd = -1

    def __enter__(self) -> Ring:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()
