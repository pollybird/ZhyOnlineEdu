"""HLS 产物二次校验（§10.3.2.3 第⑥层 · M2.4）。

产物同样不可信：转码进程可能被恶意输入带偏、也可能只写出了半截文件。
本模块只做**结构 + 一致性**校验，不依赖 ffmpeg：

- master 必须只引用预期的 profile 变体，URI 必须是相对路径、无 `..`、无外链；
- 每个变体必须声明 `METHOD=AES-128`，key URI 只能是 `/api/v1/media/key/{kid}`；
- 所有分片必须是同目录下的 `*.ts`、字节数 > 0、无外部引用；
- 变体总时长与源时长误差 ≤ max(2s, 2%)；
- 可选 codec 白名单：由注入的 `codec_prober` 探测首个分片（ffprobe 不可用时跳过）。
  HLS 分片是 AES-128 密文，调用方需先用 `#EXT-X-KEY` 的 IV 解密再探测
  （`Variant.iv` 由本模块解析提供）。

任何一条不满足都抛 `HlsVerifyError`，服务层据此把 asset 标 `failed`，
且**不**递增 `playlist_version`（旧产物继续可用）。
"""

from __future__ import annotations

import re
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path

MASTER_NAME = "master.m3u8"
VARIANT_NAME = "index.m3u8"
ALLOWED_CODECS = ("h264", "hevc")
DURATION_ABS_TOLERANCE_SEC = 2.0
DURATION_REL_TOLERANCE = 0.02

_SEGMENT = re.compile(r"^[A-Za-z0-9._-]+\.ts$")
_KEY_URI = re.compile(r"^/api/v1/media/key/[0-9a-f]{32}$")
_SAFE_URI = re.compile(r"^[A-Za-z0-9._-]+(/[A-Za-z0-9._-]+)*$")
_EXTINF = re.compile(r"^#EXTINF:([0-9]+(?:\.[0-9]+)?)")
_KEY = re.compile(r'^#EXT-X-KEY:METHOD=([A-Z0-9-]+),URI="([^"]+)"(?:,IV=0x([0-9A-Fa-f]{32}))?')
_IV_ATTR = ",IV="


class HlsVerifyError(Exception):
    def __init__(self, reason: str) -> None:
        self.reason = reason
        super().__init__(reason)


@dataclass(frozen=True, slots=True)
class Variant:
    profile: str
    playlist: Path
    key_uri: str
    segments: tuple[Path, ...]
    duration_sec: float
    iv: bytes | None = None


@dataclass(frozen=True, slots=True)
class HlsInspection:
    variants: tuple[Variant, ...]
    duration_sec: float
    segment_count: int


def _read(path: Path) -> list[str]:
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise HlsVerifyError("playlist_unreadable") from exc
    return [line.strip() for line in text.splitlines() if line.strip()]


def _relative_uri(uri: str) -> bool:
    if uri == "" or uri.startswith("/") or "://" in uri or uri.startswith("//"):
        return False
    if "\\" in uri or ".." in uri.split("/"):
        return False
    return bool(_SAFE_URI.fullmatch(uri))


def parse_master(root: Path) -> dict[str, str]:
    """返回 {profile: 变体 uri}；master 不存在或结构不合规即拒绝。"""
    master = root / MASTER_NAME
    if not master.is_file():
        raise HlsVerifyError("master_missing")
    lines = _read(master)
    if not lines or lines[0] != "#EXTM3U":
        raise HlsVerifyError("master_invalid")
    variants: dict[str, str] = {}
    index = 0
    while index < len(lines):
        line = lines[index]
        if line.startswith("#EXT-X-STREAM-INF"):
            if index + 1 >= len(lines):
                raise HlsVerifyError("master_invalid")
            uri = lines[index + 1]
            if not _relative_uri(uri):
                raise HlsVerifyError("external_reference")
            profile = Path(uri).parent.name
            if profile == "" or profile in variants:
                raise HlsVerifyError("master_invalid")
            variants[profile] = uri
            index += 2
            continue
        if not line.startswith("#"):
            raise HlsVerifyError("master_invalid")
        index += 1
    if not variants:
        raise HlsVerifyError("master_invalid")
    return variants


def parse_variant(root: Path, profile: str, uri: str) -> Variant:
    playlist = root / uri
    if not playlist.is_file():
        raise HlsVerifyError("variant_missing")
    lines = _read(playlist)
    if not lines or lines[0] != "#EXTM3U":
        raise HlsVerifyError("variant_invalid")
    key_uri: str | None = None
    iv: bytes | None = None
    segments: list[Path] = []
    duration = 0.0
    for line in lines:
        if line.startswith("#EXT-X-KEY"):
            match = _KEY.match(line)
            if match is None or (_IV_ATTR in line and match.group(3) is None):
                raise HlsVerifyError("key_declaration_invalid")
            method, key, iv_hex = match.groups()
            if method != "AES-128":
                raise HlsVerifyError("key_method_invalid")
            if not _KEY_URI.fullmatch(key):
                raise HlsVerifyError("key_uri_invalid")
            key_uri = key
            iv = bytes.fromhex(iv_hex) if iv_hex else None
            continue
        if line.startswith("#EXTINF"):
            match = _EXTINF.match(line)
            if match is None:
                raise HlsVerifyError("extinf_invalid")
            duration += float(match.group(1))
            continue
        if line.startswith("#"):
            continue
        if not _SEGMENT.fullmatch(line):
            raise HlsVerifyError("segment_reference_invalid")
        segment = playlist.parent / line
        if not segment.is_file() or segment.stat().st_size == 0:
            raise HlsVerifyError("segment_missing")
        segments.append(segment)
    if key_uri is None:
        raise HlsVerifyError("key_declaration_missing")
    if not segments:
        raise HlsVerifyError("segments_empty")
    return Variant(
        profile=profile,
        playlist=playlist,
        key_uri=key_uri,
        segments=tuple(segments),
        duration_sec=round(duration, 3),
        iv=iv,
    )


def verify_hls(
    root: Path,
    *,
    profiles: Sequence[str],
    expected_duration_sec: int = 0,
    codec_prober: Callable[[Path], str | None] | None = None,
) -> HlsInspection:
    """校验 staging 目录里的完整 HLS 产物；返回聚合结果供落库。"""
    expected = list(profiles)
    found = parse_master(root)
    if sorted(found) != sorted(expected):
        raise HlsVerifyError("profile_mismatch")

    variants = tuple(parse_variant(root, profile, found[profile]) for profile in expected)
    keys = {variant.key_uri for variant in variants}
    if len(keys) != 1:
        raise HlsVerifyError("kid_mismatch")

    durations = [variant.duration_sec for variant in variants]
    if any(value <= 0 for value in durations):
        raise HlsVerifyError("duration_invalid")
    baseline = expected_duration_sec if expected_duration_sec > 0 else max(durations)
    tolerance = max(DURATION_ABS_TOLERANCE_SEC, baseline * DURATION_REL_TOLERANCE)
    if any(abs(value - baseline) > tolerance for value in durations):
        raise HlsVerifyError("duration_mismatch")

    if codec_prober is not None:
        for variant in variants:
            codec = codec_prober(variant.segments[0])
            if codec is not None and codec not in ALLOWED_CODECS:
                raise HlsVerifyError("codec_not_allowed")

    return HlsInspection(
        variants=variants,
        duration_sec=round(sum(durations) / len(durations), 3),
        segment_count=sum(len(variant.segments) for variant in variants),
    )


__all__ = [
    "ALLOWED_CODECS",
    "HlsInspection",
    "HlsVerifyError",
    "MASTER_NAME",
    "VARIANT_NAME",
    "Variant",
    "parse_master",
    "parse_variant",
    "verify_hls",
]
