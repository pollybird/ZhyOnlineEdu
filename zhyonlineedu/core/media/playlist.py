"""m3u8 重写（§10.3.4 · F3 · M2.5）。

存储里的 m3u8 是**离线产物**（相对 URI、`/api/v1/media/key/{kid}` 静态密钥地址）。
对客户端下发前必须重写：

- master：只保留票据允许的档位，变体 URI 指向带签名的 playlist 端点；
- variant：`#EXT-X-KEY` 的 URI 换成带 `ks/n/exp/sig` 的 key 端点，
  `#EXTINF` 行保持原值（播放器据此算时长）；
- 分片行改指 `/media/seg/{kid}/{profile}/<name>`（内容已 AES-128 加密，
  泄漏分片地址拿不到明文；真正的门在 key 端点）。

同时做**纵深校验**：档位集合必须 ⊆ 票据允许集合；分片名必须匹配
`^[A-Za-z0-9._-]+\\.ts$` 且实际存在于磁盘（避免路径注入与过期产物）。
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

from zhyonlineedu.core.media.hls import HlsVerifyError, parse_master, parse_variant

_SEGMENT_NAME = re.compile(r"^[A-Za-z0-9._-]+\.ts$")
_KEY_LINE = re.compile(r"^#EXT-X-KEY:(?P<attrs>.+)$")


@dataclass(frozen=True, slots=True)
class RewriteContext:
    """一次下发所需的全部地址片段（由服务层拼好，重写函数只做纯文本替换）。"""

    kid: str
    base: str  # "" 或 https://cdn.example.com（不含结尾斜杠）
    playlist_prefix: str  # e.g. /api/v1/media/playlist
    segment_prefix: str  # e.g. /api/v1/media/seg
    query: str  # 已签名的 "ks=..&n=..&exp=..&sig=.."（不含 ?）

    def playlist_url(self, profile: str) -> str:
        return f"{self.base}{self.playlist_prefix}/{self.kid}/{profile}/index.m3u8?{self.query}"

    def master_url(self) -> str:
        return f"{self.base}{self.playlist_prefix}/{self.kid}/master.m3u8?{self.query}"

    def segment_url(self, profile: str, name: str) -> str:
        return f"{self.base}{self.segment_prefix}/{self.kid}/{profile}/{name}"

    def key_url(self) -> str:
        return f"{self.base}/api/v1/media/key/{self.kid}?{self.query}"


def _reject_external(text: str) -> None:
    if ".." in text or any("://" in line for line in text.splitlines()):
        raise HlsVerifyError("external_reference")


def rewrite_master(text: str, root: Path, *, ctx: RewriteContext, allowed: tuple[str, ...]) -> str:
    """过滤档位并改写变体 URI；`allowed` 为空则全部放行（预览/管理端）。"""
    _reject_external(text)
    parsed = parse_master(root)
    for profile in parsed:
        if allowed and profile not in allowed:
            continue
        parse_variant(root, profile, parsed[profile])  # 结构二次校验（分片/key URI）
    keep = {p for p in parsed if not allowed or p in allowed}
    if not keep:
        raise HlsVerifyError("profile_mismatch")

    lines: list[str] = []
    pending_stream = False
    for raw in text.splitlines():
        line = raw.strip()
        if line == "":
            continue
        if line.startswith("#EXT-X-STREAM-INF"):
            pending_stream = True
            lines.append(line)
            continue
        if line.startswith("#"):
            lines.append(line)
            continue
        if not pending_stream:
            raise HlsVerifyError("unexpected_uri")
        profile = line.split("/", 1)[0].split("?", 1)[0].strip()
        if profile in keep:
            lines.append(ctx.playlist_url(profile))
        else:
            lines.pop()  # 丢弃该档位的 #EXT-X-STREAM-INF
        pending_stream = False
    return "\n".join(lines) + "\n"


def rewrite_variant(
    text: str, root: Path, *, profile: str, ctx: RewriteContext, key_url: str
) -> str:
    _reject_external(text)
    variant = parse_variant(root, profile, f"{profile}/index.m3u8")
    names = {segment.name for segment in variant.segments}
    key_count = 0
    out: list[str] = []
    for raw in text.splitlines():
        line = raw.strip()
        if line == "":
            continue
        match = _KEY_LINE.match(line)
        if match:
            key_count += 1
            out.append(f'#EXT-X-KEY:METHOD=AES-128,URI="{key_url}"')
            continue
        if line.startswith("#"):
            out.append(line)
            continue
        name = line.split("?", 1)[0]
        if not _SEGMENT_NAME.match(name) or name not in names:
            raise HlsVerifyError("segment_missing")
        out.append(ctx.segment_url(profile, name))
    if key_count != 1:
        raise HlsVerifyError("key_missing")
    return "\n".join(out) + "\n"
