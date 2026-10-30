"""ProgressBus 抽象（§12.5 · F2）。

- T0/T1（无 Redis）：`LocalBus` —— 生产者把指令追加到 `ZHY_PROGRESS_RING_DIR`
  下的 `gw-<pid>.jsonl`（0600），flush 单写者扫全部文件、按 `gw_seq` 归并消费，
  `ack` 后删除已消费行（原子替换），进程崩溃只会留下未 ack 的尾行，不丢进度；
- T2（有 Redis）：`RedisBus` 用 Stream 单消费者组（M3 档才有 Redis 依赖，本轮保留
  显式未实现错误，装配时由 `get_bus` 拒绝而非静默降级——避免"以为有 Redis 实际没有"）。

**事实源是 DB 的 `learn_progress_log`**：`ProgressGateway.record` 先追加 log 再 publish，
因此即使总线文件损坏，flush 仍能从未 flush 的 log 行重放（HBT-002 的兜底路径）。
"""

from __future__ import annotations

import fcntl
import json
import logging
import os
from abc import ABC, abstractmethod
from collections.abc import Iterator
from dataclasses import asdict, dataclass
from pathlib import Path

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class ProgressCmd:
    """一条进度指令（网关分配 gw_seq 后入队）。"""

    gw_seq: int
    user_id: int
    lesson_id: int
    course_id: int
    seq_no: int
    position_sec: int
    played_seconds: int
    speed_bps: int
    source: str  # "play" | "preview"（preview 永不进统计）
    client_ts_ms: int

    def as_dict(self) -> dict[str, object]:
        return asdict(self)

    @classmethod
    def from_dict(cls, raw: object) -> ProgressCmd | None:
        if not isinstance(raw, dict):
            return None
        try:
            return cls(
                gw_seq=_as_int(raw["gw_seq"]),
                user_id=_as_int(raw["user_id"]),
                lesson_id=_as_int(raw["lesson_id"]),
                course_id=_as_int(raw["course_id"]),
                seq_no=_as_int(raw.get("seq_no", raw["gw_seq"])),
                position_sec=_as_int(raw["position_sec"]),
                played_seconds=_as_int(raw["played_seconds"]),
                speed_bps=_as_int(raw.get("speed_bps", 100)),
                source=str(raw.get("source", "play")),
                client_ts_ms=_as_int(raw.get("client_ts_ms", 0)),
            )
        except (KeyError, TypeError, ValueError):
            return None


class RingFull(Exception):
    """ring 达到上限：丢弃总线副本，但 DB log 已落盘，进度不丢（flush 会重放）。"""


def _as_int(value: object) -> int:
    """严格转换（bool/非数字一律 ValueError，由 from_dict 统一忽略坏行）。"""
    if isinstance(value, bool) or not isinstance(value, (int, str)):
        raise TypeError("progress ring 字段必须是整数")
    return int(value)


class ProgressBus(ABC):
    @abstractmethod
    def publish(self, cmd: ProgressCmd) -> None: ...

    @abstractmethod
    def consume(self, batch: int) -> list[ProgressCmd]: ...

    @abstractmethod
    def ack(self, up_to_gw_seq: int) -> None: ...


class LocalBus(ProgressBus):
    """零外部依赖档：JSONL ring + 单写者归并（进程崩溃安全）。"""

    FILE_PREFIX = "gw-"
    FILE_SUFFIX = ".jsonl"
    MAX_RING_BYTES = 32 * 1024 * 1024  # 单文件上限；flush 持续消费，正常不会触顶

    def __init__(self, ring_dir: str | Path | None = None) -> None:
        default_dir = os.environ.get("ZHY_PROGRESS_RING_DIR", "/run/zhy/progress")
        self.ring_dir = Path(ring_dir or default_dir)
        self._own = self.ring_dir / f"{self.FILE_PREFIX}{os.getpid()}{self.FILE_SUFFIX}"

    # ── 生产者 ────────────────────────────────────────────────────────────

    def _ensure_dir(self) -> None:
        self.ring_dir.mkdir(parents=True, exist_ok=True)

    def publish(self, cmd: ProgressCmd) -> None:
        self._ensure_dir()
        if self._own.is_file() and self._own.stat().st_size >= self.MAX_RING_BYTES:
            raise RingFull(str(self._own))
        line = json.dumps(cmd.as_dict(), ensure_ascii=False, separators=(",", ":")) + "\n"
        fd = os.open(self._own, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "a", encoding="utf-8") as handle:
            handle.write(line)
            handle.flush()
            os.fsync(handle.fileno())  # 崩溃安全：先落盘再返回 202

    # ── 消费者（仅 flush 单写者调用）──────────────────────────────────────

    def _files(self) -> list[Path]:
        if not self.ring_dir.is_dir():
            return []
        return sorted(
            path
            for path in self.ring_dir.glob(f"*{self.FILE_SUFFIX}")
            if path.name.startswith(self.FILE_PREFIX)
        )

    def _locked_lines(self, path: Path) -> Iterator[tuple[Path, int, str]]:
        with path.open("r+", encoding="utf-8") as handle:
            try:
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
            except OSError:
                return
            try:
                for lineno, line in enumerate(handle.read().splitlines(), 1):
                    yield path, lineno, line
            finally:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)

    def consume(self, batch: int) -> list[ProgressCmd]:
        commands: list[ProgressCmd] = []
        for path in self._files():
            for _path, _lineno, line in self._locked_lines(path):
                cmd = ProgressCmd.from_dict(self._parse(line))
                if cmd is not None:
                    commands.append(cmd)
        commands.sort(key=lambda item: item.gw_seq)
        return commands[: max(1, batch)]

    @staticmethod
    def _parse(line: str) -> object:
        if not line.strip():
            return None
        try:
            return json.loads(line)
        except json.JSONDecodeError:
            logger.warning("progress ring 行损坏，已跳过")
            return None

    def ack(self, up_to_gw_seq: int) -> None:
        for path in self._files():
            self._rewrite_without(path, up_to_gw_seq)

    def _rewrite_without(self, path: Path, up_to_gw_seq: int) -> None:
        with path.open("r+", encoding="utf-8") as handle:
            try:
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
            except OSError:
                return
            try:
                kept: list[str] = []
                for line in handle.read().splitlines():
                    parsed = self._parse(line)
                    seq = parsed.get("gw_seq") if isinstance(parsed, dict) else None
                    if isinstance(seq, int) and seq <= up_to_gw_seq:
                        continue
                    if line.strip():
                        kept.append(line)
                handle.seek(0)
                handle.truncate()
                if kept:
                    handle.write("\n".join(kept) + "\n")
                handle.flush()
                os.fsync(handle.fileno())
            finally:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


class RedisBus(ProgressBus):
    """T2 档：Redis Stream 单消费者组（M3 引入，装配期显式拒绝）。"""

    def __init__(self, url: str = "") -> None:
        self.url = url

    def _unavailable(self) -> None:
        raise RuntimeError("ZHY_PROGRESS_BUS=redis-stream 需 M3 的 Redis 依赖，当前版本未提供")

    def publish(self, cmd: ProgressCmd) -> None:
        self._unavailable()

    def consume(self, batch: int) -> list[ProgressCmd]:
        self._unavailable()
        return []

    def ack(self, up_to_gw_seq: int) -> None:
        self._unavailable()


def get_bus(
    kind: str = "local", *, ring_dir: str | Path | None = None, url: str = ""
) -> ProgressBus:
    """按 `progress.bus` 装配：local | redis-stream（默认 local）。"""
    if kind == "redis-stream":
        return RedisBus(url)
    return LocalBus(ring_dir)
