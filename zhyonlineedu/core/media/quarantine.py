"""上传输入检疫（§10.3.1 / §10.3.2.3 第①层 · F7 第一道门）。

原则：输入是攻击者提供的字节流，FFmpeg 是解析器。检疫必须在 **Web 进程** 的
`complete` 阶段完成，不能等转码 worker：

1. 先做魔数判定（廉价、fail-closed）：命中已知危险容器（PE/ELF/ZIP/PDF）即拒；
2. 再跑 `ffprobe` 校验 `format_name` 白名单 + 时长/尺寸上限；
3. ffprobe 不可用且 `require_ffprobe=false` 时退化为"仅魔数"（dev 便利），
   非 dev 环境由配置交叉校验强制 `require_ffprobe=true`。

`prober` 可注入，便于单测覆盖时长/尺寸越界而不依赖真实媒体文件。
"""

from __future__ import annotations

import json
import shutil
import subprocess
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path

FFPROBE_TIMEOUT_SEC = 15
_MAGIC_READ_BYTES = 64

# ffprobe 的 format_name 是逗号分隔的容器族（如 "mov,mp4,m4a,3gp,3g2,mj2"）；
# 把项目白名单映射成"可接受的 token 集合"再做交集。
_FORMAT_TOKENS: dict[str, frozenset[str]] = {
    "mp4": frozenset({"mp4", "m4v", "m4a", "3gp", "3g2", "mj2"}),
    "mov": frozenset({"mov", "mp4", "m4a", "3gp", "3g2", "mj2"}),
    # mkv/webm 同属 matroska 族；ffprobe 无法区分时按族放行（默认两者都在白名单）
    "mkv": frozenset({"matroska"}),
    "webm": frozenset({"matroska", "webm"}),
    "flv": frozenset({"flv"}),
    "mp3": frozenset({"mp3"}),
    "aac": frozenset({"aac"}),
}


class MediaRejected(Exception):
    """检疫拒绝：`reason` 为机器可读原因，`format_name` 为识别到的容器（若有）。"""

    def __init__(self, reason: str, *, format_name: str | None = None) -> None:
        self.reason = reason
        self.format_name = format_name
        super().__init__(reason)


@dataclass(frozen=True, slots=True)
class MediaInfo:
    format_name: str
    duration_sec: int
    size_bytes: int
    width: int | None
    height: int | None
    source: str  # ffprobe | magic


Prober = Callable[[Path], MediaInfo | None]


def detect_magic(head: bytes) -> str | None:
    """按文件头识别容器/可执行格式；无法识别返回 None（调用方按 fail-closed 处理）。"""
    if head.startswith(b"\x7fELF"):
        return "elf"
    if head.startswith(b"MZ"):
        return "pe"
    if head.startswith(b"PK\x03\x04") or head.startswith(b"PK\x05\x06"):
        return "zip"
    if head.startswith(b"%PDF"):
        return "pdf"
    if len(head) >= 12 and head[4:8] == b"ftyp":
        return "mp4"
    if head.startswith(b"\x1aE\xdf\xa3"):  # EBML
        return "webm" if b"webm" in head else "mkv"
    if head.startswith(b"FLV\x01"):
        return "flv"
    if head.startswith(b"ID3"):
        return "mp3"
    if len(head) >= 2 and (head[1] & 0xF6) == 0xF0:  # ADTS sync 0xFFF0~0xFFF9
        return "aac"
    if (
        len(head) >= 2
        and head[0] == 0xFF
        and (head[1] & 0xE0) == 0xE0
        and ((head[1] >> 1) & 0x03)  # layer != 0：MP3 帧头
    ):
        return "mp3"
    return None


def _allowed_tokens(allowed_formats: Sequence[str]) -> frozenset[str]:
    tokens: set[str] = set()
    for name in allowed_formats:
        tokens.update(_FORMAT_TOKENS.get(name, {name}))
    return frozenset(tokens)


def _ffprobe_allowed(format_name: str, allowed_formats: Sequence[str]) -> bool:
    tokens = _allowed_tokens(allowed_formats)
    return any(part.strip() in tokens for part in format_name.split(",") if part.strip())


def _as_int(value: object) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value)
    if isinstance(value, str):
        try:
            return int(float(value))
        except ValueError:
            return None
    return None


def _parse_ffprobe(payload: object, path: Path) -> MediaInfo:
    if not isinstance(payload, dict):
        raise MediaRejected("probe_failed")
    raw = payload.get("format")
    if not isinstance(raw, dict):
        raise MediaRejected("probe_failed")
    format_name = raw.get("format_name")
    if not isinstance(format_name, str) or format_name == "":
        raise MediaRejected("probe_failed")
    duration = _as_int(raw.get("duration")) or 0
    size = _as_int(raw.get("size")) or path.stat().st_size
    width: int | None = None
    height: int | None = None
    streams = payload.get("streams")
    if isinstance(streams, list):
        for stream in streams:
            if not isinstance(stream, dict) or stream.get("codec_type") != "video":
                continue
            w = _as_int(stream.get("width"))
            h = _as_int(stream.get("height"))
            if w is not None:
                width = w if width is None else max(width, w)
            if h is not None:
                height = h if height is None else max(height, h)
    return MediaInfo(
        format_name=format_name,
        duration_sec=duration,
        size_bytes=size,
        width=width,
        height=height,
        source="ffprobe",
    )


def ffprobe_probe(path: Path) -> MediaInfo | None:
    """默认 prober：ffprobe 不可用返回 None；解析失败/超时按拒绝处理（fail-closed）。"""
    binary = shutil.which("ffprobe")
    if binary is None:
        return None
    command = [
        binary,
        "-v",
        "error",
        "-show_entries",
        "format=format_name,duration,size:stream=codec_type,width,height",
        "-of",
        "json",
        str(path),
    ]
    try:
        completed = subprocess.run(  # noqa: S603 - 参数列表 + 绝对路径，禁 shell
            command,
            capture_output=True,
            check=False,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=FFPROBE_TIMEOUT_SEC,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise MediaRejected("probe_failed") from exc
    if completed.returncode != 0:
        raise MediaRejected("probe_failed")
    try:
        payload: object = json.loads(completed.stdout)
    except json.JSONDecodeError as exc:
        raise MediaRejected("probe_failed") from exc
    return _parse_ffprobe(payload, path)


def probe_media(
    path: Path,
    *,
    allowed_formats: Sequence[str],
    max_duration_sec: int,
    max_dimension: int,
    require_ffprobe: bool,
    prober: Prober | None = None,
) -> MediaInfo:
    """魔数 + ffprobe 双层检疫；任何不合规抛 `MediaRejected`。"""
    with path.open("rb") as handle:
        head = handle.read(_MAGIC_READ_BYTES)
    magic = detect_magic(head)
    if magic is not None and magic not in allowed_formats:
        raise MediaRejected("format_not_allowed", format_name=magic)

    info = (prober or ffprobe_probe)(path)
    if info is None:
        if require_ffprobe:
            raise MediaRejected("ffprobe_unavailable")
        if magic is None:
            raise MediaRejected("unrecognized_format")
        return MediaInfo(
            format_name=magic,
            duration_sec=0,
            size_bytes=path.stat().st_size,
            width=None,
            height=None,
            source="magic",
        )

    if not _ffprobe_allowed(info.format_name, allowed_formats):
        raise MediaRejected("format_not_allowed", format_name=info.format_name)
    if info.duration_sec > max_duration_sec:
        raise MediaRejected("duration_exceeded", format_name=info.format_name)
    for value in (info.width, info.height):
        if value is not None and value > max_dimension:
            raise MediaRejected("dimension_exceeded", format_name=info.format_name)
    return info


def probe_codec(path: Path, *, timeout: int = FFPROBE_TIMEOUT_SEC) -> str | None:
    """转码产物二次校验用：读取首个流编码名。

    - ffprobe 不存在 → None（调用方跳过编码白名单，不让 dev 环境因缺工具失败）；
    - ffprobe 存在但解析失败 → "unknown"（会被白名单拒绝，fail-closed）。
    """
    binary = shutil.which("ffprobe")
    if binary is None:
        return None
    command = [
        binary,
        "-v",
        "error",
        "-select_streams",
        "v:0",
        "-show_entries",
        "stream=codec_name",
        "-of",
        "json",
        str(path),
    ]
    try:
        completed = subprocess.run(  # noqa: S603 - 参数列表 + 绝对路径，禁 shell
            command,
            capture_output=True,
            check=False,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
        )
    except (OSError, subprocess.TimeoutExpired):
        return "unknown"
    if completed.returncode != 0:
        return "unknown"
    try:
        payload: object = json.loads(completed.stdout)
    except json.JSONDecodeError:
        return "unknown"
    if not isinstance(payload, dict):
        return "unknown"
    streams = payload.get("streams")
    if not isinstance(streams, list) or not streams:
        return "unknown"
    first = streams[0]
    if not isinstance(first, dict):
        return "unknown"
    codec = first.get("codec_name")
    return codec if isinstance(codec, str) else "unknown"


__all__ = [
    "MediaInfo",
    "MediaRejected",
    "Prober",
    "detect_magic",
    "ffprobe_probe",
    "probe_codec",
    "probe_media",
]
