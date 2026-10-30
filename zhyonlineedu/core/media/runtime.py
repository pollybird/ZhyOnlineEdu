"""FFmpeg 沙箱封装（§10.3.2.3 · F7 · M2.4）。

八层：检疫(魔数) / 降权(非 root) / 无网 / seccomp / 资源(RLIMIT + cgroup) /
输出白名单 / 密钥隔离 / 失败处置。

红线（§4.2）：FFmpeg 只能从 `workers/transcode.py` → `TranscodeService` → 本模块调用，
argv 全部由枚举与路径派生，禁止拼接任意字符串；输出目录与源文件必须落在允许根内。

三类后端：
- `bwrap`：`--unshare-all`（含网络/用户/pid 命名空间）+ `--die-with-parent` + 只读挂载；
- `cgroup_systemd`：`systemd-run --scope` 施加 CPU/内存/任务数上限；
- `disabled`：仅 dev，配置交叉校验禁止在 t0/t1/t2 使用。

资源限额同时用 `preexec_fn` 设置 RLIMIT_CPU/AS/NPROC/FSIZE/NOFILE，
即便后端不提供 cgroup 也有底线上限。所有失败都不回吐 stderr 给前端。
"""

from __future__ import annotations

import os
import re
import resource
import shutil
import subprocess
import time
from collections.abc import Sequence
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Literal, Protocol

STDERR_TAIL_BYTES = 2048
VERSION_TIMEOUT_SEC = 5
FFMPEG_NAME = "ffmpeg"
BWRAP_NAME = "bwrap"
SYSTEMD_RUN_NAME = "systemd-run"

PROFILE_HEIGHTS: dict[str, int] = {"360": 360, "720": 720, "1080": 1080}
ALLOWED_PROFILES: tuple[str, ...] = tuple(PROFILE_HEIGHTS)

# 输出字节上限（RLIMIT_FSIZE）：单课时 4TB 规模下，单任务 8GB 已远高于实际需求
OUTPUT_FSIZE_BYTES = 8 * 1024**3
NOFILE_LIMIT = 512

_UNSAFE = re.compile(r"[\x00\n\r]")


class SandboxBackend(StrEnum):
    BWRAP = "bwrap"
    CGROUP_SYSTEMD = "cgroup_systemd"
    DISABLED = "disabled"  # 仅 dev（ZHY_ENV=dev）


class SandboxError(Exception):
    """沙箱不可用/参数不合法/路径越界；`reason` 为机器可读原因。"""

    def __init__(self, reason: str) -> None:
        self.reason = reason
        super().__init__(reason)


class TranscodeTimeout(Exception):
    """超过任务超时被杀；由服务层映射为 ENC-1006。"""


@dataclass(frozen=True, slots=True)
class KeyMaterial:
    """AES-128 密钥注入材料：`0400` 临时文件，任务结束后由服务层 `shred`。"""

    kid: str
    key_path: Path
    info_path: Path
    uri: str


@dataclass(frozen=True, slots=True)
class RenderSpec:
    kind: Literal["hls", "poster"]
    source: Path
    out_dir: Path
    segment_sec: int
    duration_sec: int
    profile: str = "720"
    poster_sec: int = 3
    key: KeyMaterial | None = None


@dataclass(frozen=True, slots=True)
class RunResult:
    returncode: int
    stderr_tail: str
    elapsed_sec: float
    profile: dict[str, object]


class TranscodeRunner(Protocol):
    """便于单测注入假 runner；实现必须返回 `RunResult`，失败以 returncode 表达。"""

    def run(self, spec: RenderSpec) -> RunResult: ...


def _height(profile: str) -> int:
    try:
        return PROFILE_HEIGHTS[profile]
    except KeyError:
        raise SandboxError("unknown_profile") from None


def _check_paths(spec: RenderSpec, allowed_roots: Sequence[Path]) -> None:
    for path in (spec.source, spec.out_dir):
        text = str(path)
        if _UNSAFE.search(text):
            raise SandboxError("unsafe_path")
    roots = [root.resolve() for root in allowed_roots]
    source = spec.source.resolve(strict=False)
    out = spec.out_dir.resolve(strict=False)
    if not any(root == source or root in source.parents for root in roots):
        raise SandboxError("source_outside_allowed_root")
    if not any(root == out or root in out.parents for root in roots):
        raise SandboxError("output_outside_allowed_root")
    if spec.key is not None:
        for material in (spec.key.key_path, spec.key.info_path):
            resolved = material.resolve(strict=False)
            if not any(root == resolved or root in resolved.parents for root in roots):
                raise SandboxError("key_outside_allowed_root")


def build_hls_command(spec: RenderSpec) -> list[str]:
    if spec.key is None:
        raise SandboxError("missing_key_material")
    height = _height(spec.profile)
    return [
        FFMPEG_NAME,
        "-hide_banner",
        "-nostdin",
        "-y",
        "-i",
        str(spec.source),
        "-map",
        "0:v:0",
        "-map",
        "0:a?",
        "-vf",
        f"scale=-2:{height}",
        "-c:v",
        "libx264",
        "-preset",
        "veryfast",
        "-crf",
        "23",
        "-profile:v",
        "main",
        "-c:a",
        "aac",
        "-b:a",
        "96k",
        "-ar",
        "44100",
        "-f",
        "hls",
        "-hls_time",
        str(spec.segment_sec),
        "-hls_list_size",
        "0",
        "-hls_playlist_type",
        "vod",
        "-hls_segment_filename",
        "seg_%03d.ts",
        "-hls_key_info_file",
        str(spec.key.info_path),
        "index.m3u8",
    ]


def build_poster_command(spec: RenderSpec) -> list[str]:
    height = _height(spec.profile)
    return [
        FFMPEG_NAME,
        "-hide_banner",
        "-nostdin",
        "-y",
        "-ss",
        str(spec.poster_sec),
        "-i",
        str(spec.source),
        "-frames:v",
        "1",
        "-vf",
        f"scale=-2:{height}",
        "poster.jpg",
    ]


def tail(text: str) -> str:
    """stderr 只保留尾部 2KB（§10.3.2.3 ⑧），前端永远看不到。"""
    encoded = text.encode("utf-8", "replace")
    if len(encoded) <= STDERR_TAIL_BYTES:
        return text
    return encoded[-STDERR_TAIL_BYTES:].decode("utf-8", "replace")


class SandboxRunner:
    """真实执行器；测试直接注入 `TranscodeRunner` 替身，不触达 ffmpeg。"""

    def __init__(
        self,
        *,
        backend: str = SandboxBackend.BWRAP,
        cpu_max_percent: int = 200,
        mem_max_mb: int = 2048,
        pids_max: int = 16,
        timeout_sec: int = 3600,
        allowed_roots: Sequence[Path] = (),
        ffmpeg_path: str | None = None,
        bwrap_path: str | None = None,
        systemd_run_path: str | None = None,
    ) -> None:
        try:
            self.backend = SandboxBackend(backend)
        except ValueError:
            raise SandboxError("unknown_backend") from None
        self.cpu_max_percent = cpu_max_percent
        self.mem_max_mb = mem_max_mb
        self.pids_max = pids_max
        self.timeout_sec = timeout_sec
        self.allowed_roots = tuple(allowed_roots)
        self._ffmpeg = ffmpeg_path
        self._bwrap = bwrap_path
        self._systemd_run = systemd_run_path
        self._version: str | None = None

    # ── 公共入口 ──────────────────────────────────────────────────────────

    def run(self, spec: RenderSpec) -> RunResult:
        self._check(spec)
        argv = self._wrap(self._inner_command(spec), spec)
        started = time.monotonic()
        try:
            completed = subprocess.run(  # noqa: S603 - argv 由枚举+校验路径派生，禁 shell
                argv,
                capture_output=True,
                check=False,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=self.timeout_sec,
                cwd=str(spec.out_dir),
                env=self._env(),
                preexec_fn=self._limits if os.name == "posix" else None,
            )
        except subprocess.TimeoutExpired as exc:
            raise TranscodeTimeout("transcode_timeout") from exc
        except FileNotFoundError as exc:
            raise SandboxError("sandbox_unavailable") from exc
        except OSError as exc:
            raise SandboxError("sandbox_start_failed") from exc
        return RunResult(
            returncode=completed.returncode,
            stderr_tail=tail(completed.stderr or ""),
            elapsed_sec=round(time.monotonic() - started, 3),
            profile=self._profile(),
        )

    # ── 内部：命令派生 ────────────────────────────────────────────────────

    def _inner_command(self, spec: RenderSpec) -> list[str]:
        command = build_hls_command(spec) if spec.kind == "hls" else build_poster_command(spec)
        # argv[0] 必须是实际二进制绝对路径（build_* 只给出逻辑名）
        command[0] = self.ffmpeg_path()
        return command

    def _check(self, spec: RenderSpec) -> None:
        _check_paths(spec, self.allowed_roots)
        if not spec.source.is_file():
            raise SandboxError("source_missing")
        spec.out_dir.mkdir(parents=True, exist_ok=True)
        if self.backend is SandboxBackend.BWRAP and self._bwrap_binary() is None:
            raise SandboxError("sandbox_unavailable")
        if self.backend is SandboxBackend.CGROUP_SYSTEMD and self._systemd_binary() is None:
            raise SandboxError("sandbox_unavailable")

    def _wrap(self, inner: list[str], spec: RenderSpec) -> list[str]:
        if self.backend is SandboxBackend.DISABLED:
            return inner
        if self.backend is SandboxBackend.CGROUP_SYSTEMD:
            systemd = self._systemd_binary()
            assert systemd is not None  # _check 已确认
            return [
                systemd,
                "--scope",
                "--quiet",
                "-p",
                f"CPUQuota={self.cpu_max_percent}%",
                "-p",
                f"MemoryMax={self.mem_max_mb}M",
                "-p",
                f"TasksMax={self.pids_max}",
                "--",
                *inner,
            ]
        bwrap = self._bwrap_binary()
        assert bwrap is not None  # _check 已确认
        argv = [
            bwrap,
            "--unshare-user",
            "--unshare-pid",
            "--unshare-net",
            "--unshare-ipc",
            "--unshare-uts",
            "--die-with-parent",
            "--new-session",
            "--uid",
            "65534",
            "--gid",
            "65534",
            "--ro-bind",
            "/usr",
            "/usr",
            "--ro-bind",
            "/lib",
            "/lib",
            "--ro-bind",
            "/lib64",
            "/lib64",
            "--ro-bind",
            "/bin",
            "/bin",
            "--proc",
            "/proc",
            "--dev",
            "/dev",
            "--tmpfs",
            "/tmp",  # noqa: S108 - bwrap 内私有 tmpfs，不落宿主
        ]
        if Path("/etc/alternatives").exists():
            # M0 实测：动态库软链依赖 /etc/alternatives（见 deploy/ffmpeg/bwrap-launcher.sh）
            argv += ["--tmpfs", "/etc", "--ro-bind", "/etc/alternatives", "/etc/alternatives"]
        argv += ["--ro-bind", str(spec.source), str(spec.source)]
        if spec.key is not None:
            argv += [
                "--ro-bind",
                str(spec.key.key_path),
                str(spec.key.key_path),
                "--ro-bind",
                str(spec.key.info_path),
                str(spec.key.info_path),
            ]
        argv += [
            "--bind",
            str(spec.out_dir),
            str(spec.out_dir),
            "--chdir",
            str(spec.out_dir),
            "--",
            *inner,
        ]
        return argv

    def _env(self) -> dict[str, str]:
        """只保留最小环境；禁止把主密钥/数据库 URL/JWT 路径带进沙箱（第⑦层）。"""
        return {
            "PATH": "/usr/local/bin:/usr/bin:/bin",
            "LANG": "C.UTF-8",
            "HOME": "/tmp",  # noqa: S108 - 沙箱内私有命名空间
        }

    def _limits(self) -> None:
        cpu_sec = max(1, self.timeout_sec)
        resource.setrlimit(resource.RLIMIT_CPU, (cpu_sec, cpu_sec))
        memory = self.mem_max_mb * 1024 * 1024
        resource.setrlimit(resource.RLIMIT_AS, (memory, memory))
        if self.backend is not SandboxBackend.DISABLED:
            # RLIMIT_NPROC 按**真实 uid 的全部线程**计数：生产用容器专属用户才可用 16；
            # dev 的 disabled 后端与宿主桌面用户共享 uid，设置过低会让 ffmpeg 线程创建
            # 直接 EAGAIN（cgroup TasksMax 仍由部署侧兜底）。仅 dev 放宽，不降低生产口径。
            resource.setrlimit(resource.RLIMIT_NPROC, (self.pids_max, self.pids_max))
        resource.setrlimit(resource.RLIMIT_FSIZE, (OUTPUT_FSIZE_BYTES, OUTPUT_FSIZE_BYTES))
        resource.setrlimit(resource.RLIMIT_NOFILE, (NOFILE_LIMIT, NOFILE_LIMIT))
        resource.setrlimit(resource.RLIMIT_CORE, (0, 0))

    # ── 内部：二进制与版本 ────────────────────────────────────────────────

    def _binary(self, attribute: str, name: str, explicit: str | None) -> str | None:
        if explicit is not None:
            return explicit if Path(explicit).exists() else None
        found = shutil.which(name)
        setattr(self, attribute, found)
        return found

    def _bwrap_binary(self) -> str | None:
        return self._binary("_bwrap", BWRAP_NAME, self._bwrap)

    def _systemd_binary(self) -> str | None:
        return self._binary("_systemd_run", SYSTEMD_RUN_NAME, self._systemd_run)

    def ffmpeg_path(self) -> str:
        path = self._binary("_ffmpeg", FFMPEG_NAME, self._ffmpeg)
        if path is None:
            raise SandboxError("ffmpeg_unavailable")
        return path

    def ffmpeg_version(self) -> str:
        if self._version is not None:
            return self._version
        try:
            completed = subprocess.run(  # noqa: S603 - 常量参数
                [self.ffmpeg_path(), "-version"],
                capture_output=True,
                check=False,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=VERSION_TIMEOUT_SEC,
            )
        except (OSError, subprocess.TimeoutExpired):
            self._version = "unknown"
            return self._version
        first = (completed.stdout or "").splitlines()
        self._version = first[0].strip() if first else "unknown"
        return self._version

    def _profile(self) -> dict[str, object]:
        return {
            "backend": str(self.backend),
            "network": "none" if self.backend is not SandboxBackend.DISABLED else "host",
            "uid": 65534 if self.backend is SandboxBackend.BWRAP else None,
            "cpu_max_percent": self.cpu_max_percent,
            "mem_max_mb": self.mem_max_mb,
            "pids_max": self.pids_max,
            "timeout_sec": self.timeout_sec,
            "ffmpeg_version": (
                self.ffmpeg_version() if self.backend is not SandboxBackend.DISABLED else "unknown"
            ),
        }


__all__ = [
    "ALLOWED_PROFILES",
    "PROFILE_HEIGHTS",
    "KeyMaterial",
    "RenderSpec",
    "RunResult",
    "SandboxBackend",
    "SandboxError",
    "SandboxRunner",
    "TranscodeRunner",
    "TranscodeTimeout",
    "build_hls_command",
    "build_poster_command",
    "tail",
]
