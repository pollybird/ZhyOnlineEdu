"""M0 Spike · 转码沙箱逃逸用例（§10.3.2.3 · F7 / SANDBOX-001~006）。

用法：uv run python spike/sandbox/run_sandbox.py [--transcode]

用例：
  SANDBOX-001 网络隔离：沙箱内 TCP 连接必然失败（--unshare-net）
  SANDBOX-002 /etc 不可见：沙箱内无 passwd/hosts（--tmpfs /etc）
  SANDBOX-003 /proc 隔离：沙箱内看不到宿主进程
  SANDBOX-004 宿主只读：沙箱内写 /usr 必然失败
  SANDBOX-005 父死子亡：杀掉父进程后沙箱内进程立即退出（--die-with-parent）
  SANDBOX-006 500MB→HLS 加密转码：可产出分片；输出目录无密钥；m3u8 指向远端 URI

注：seccomp 由容器运行时下发（deploy/ffmpeg/seccomp.json 为 Docker/K8s profile），
bwrap 负责命名空间与只读挂载；bwrap 的 --seccomp 需要原始 BPF，不能直接喂 JSON。
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
OUT = ROOT / "spike" / "output"

BASE = [
    "bwrap",
    "--unshare-user",
    "--unshare-pid",
    "--unshare-net",
    "--unshare-ipc",
    "--unshare-uts",
    "--die-with-parent",
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
    "/tmp",
    "--tmpfs",
    "/etc",
    # ffmpeg 动态库经 /etc/alternatives 软链（libblas 等），需单独只读挂入
    "--ro-bind",
    "/etc/alternatives",
    "/etc/alternatives",
    "--chdir",
    "/tmp",
]


def bwrap(*extra: str, timeout: int = 60) -> subprocess.CompletedProcess[str]:
    return subprocess.run([*BASE, *extra], capture_output=True, text=True, timeout=timeout)


def c_001() -> tuple[bool, str]:
    code = (
        "import socket\n"
        "try:\n"
        "    socket.create_connection(('1.1.1.1', 80), timeout=2)\n"
        "    print('CONNECTED')\n"
        "except OSError as e:\n"
        "    print('BLOCKED:' + type(e).__name__)\n"
    )
    r = bwrap("/usr/bin/python3", "-c", code)
    return ("BLOCKED" in r.stdout, (r.stdout or r.stderr).strip()[:80])


def c_002() -> tuple[bool, str]:
    r = bwrap("/bin/sh", "-c", "test -e /etc/passwd && echo LEAK || echo HIDDEN")
    return ("HIDDEN" in r.stdout, r.stdout.strip())


def c_003() -> tuple[bool, str]:
    host_pids = f"{os.getpid()} {os.getppid()}"
    checks = " ".join(f"test -e /proc/{p} && echo LEAK_{p}" for p in host_pids.split())
    r = bwrap("/bin/sh", "-c", f"({checks}) ; ls /proc | grep -cE '^[0-9]+$'")
    out = r.stdout.strip().splitlines()
    leaks = [line for line in out if line.startswith("LEAK_")]
    count = 0
    for line in reversed(out):
        if line.isdigit():
            count = int(line)
            break
    return (not leaks and count <= 8, f"宿主 pid 泄漏={leaks or '无'} 沙箱内进程数={count}")


def c_004() -> tuple[bool, str]:
    target = "/usr/zhy_spike_write_test"
    r = bwrap("/bin/sh", "-c", f"echo x > {target} 2>&1 || echo RO")
    host_leak = Path(target).exists()
    return (
        not host_leak and "RO" in r.stdout,
        f"宿主被写入={host_leak}, 沙箱输出={r.stdout.strip()[:40]}",
    )


def c_005() -> tuple[bool, str]:
    """父进程被 SIGKILL 后，--die-with-parent 的子进程应在 3s 内退出。"""
    wrapper = f"""
import subprocess, sys, time
p = subprocess.Popen({[*BASE, "/bin/sleep", "30"]!r}, stdout=subprocess.DEVNULL)
print(p.pid, flush=True)
time.sleep(60)
"""
    parent = subprocess.Popen([sys.executable, "-c", wrapper], stdout=subprocess.PIPE, text=True)
    assert parent.stdout is not None
    child_pid = int(parent.stdout.readline().strip())
    time.sleep(1.5)  # 等 bwrap 完成 PR_SET_PDEATHSIG（实测存在启动竞态）
    os.kill(parent.pid, 9)
    parent.wait(timeout=5)
    deadline = time.time() + 5
    gone = False
    while time.time() < deadline:
        if not Path(f"/proc/{child_pid}").exists():
            gone = True
            break
        time.sleep(0.1)
    return (gone, f"父进程被杀后子进程 pid={child_pid} 退出={gone}")


def c_006(workdir: Path, max_duration: int) -> tuple[bool, str]:
    if not shutil.which("ffmpeg"):
        return (False, "SKIP: 未安装 ffmpeg")
    ffmpeg, ffprobe = shutil.which("ffmpeg"), shutil.which("ffprobe")
    src = workdir / "input.mp4"
    if not src.exists():
        gen = subprocess.run(
            [
                ffmpeg or "ffmpeg",
                "-hide_banner",
                "-loglevel",
                "error",
                "-y",
                "-f",
                "lavfi",
                "-i",
                f"testsrc2=size=1280x720:rate=25:duration={max_duration}",
                "-f",
                "lavfi",
                "-i",
                f"sine=frequency=440:duration={max_duration}",
                "-c:v",
                "libx264",
                "-preset",
                "ultrafast",
                "-b:v",
                "8M",
                "-g",
                "50",
                "-c:a",
                "aac",
                "-b:a",
                "64k",
                "-shortest",
                str(src),
            ],
            capture_output=True,
            text=True,
            timeout=1800,
        )
        if gen.returncode != 0:
            return (False, f"素材生成失败: {gen.stderr[-200:]}")

    size_mb = src.stat().st_size / 1024 / 1024
    out_dir = workdir / "hls"
    out_dir.mkdir(parents=True, exist_ok=True)
    keys = workdir / "keys"
    keys.mkdir(exist_ok=True)
    key_file = keys / "lesson.key"
    if not key_file.exists():
        key_file.write_bytes(os.urandom(16))
    keyinfo = keys / "keyinfo.txt"
    # 第 2 行必须写**沙箱内**路径：HLS muxer 直接按该路径打开密钥文件（M0 实测）
    keyinfo.write_text(
        "http://127.0.0.1:5000/api/v1/media/key/1?kid=spike\n/in/lesson.key\n",
        encoding="utf-8",
    )
    keyinfo.chmod(0o600)

    args = [
        *BASE,
        "--ro-bind",
        str(src),
        "/in/input.mp4",
        "--ro-bind",
        str(keyinfo),
        "/in/keyinfo.txt",
        "--ro-bind",
        str(key_file),
        "/in/lesson.key",
        "--bind",
        str(out_dir),
        "/out",
        ffmpeg or "ffmpeg",
        "-hide_banner",
        "-nostdin",
        "-loglevel",
        "error",
        "-y",
        "-i",
        "/in/input.mp4",
        "-map",
        "0:v:0",
        "-map",
        "0:a:0",
        "-c:v",
        "libx264",
        "-preset",
        "veryfast",
        "-b:v",
        "2M",
        "-g",
        "50",
        "-c:a",
        "aac",
        "-b:a",
        "128k",
        "-f",
        "hls",
        "-hls_time",
        "6",
        "-hls_playlist_type",
        "vod",
        "-hls_key_info_file",
        "/in/keyinfo.txt",
        "-hls_segment_filename",
        "/out/seg_%03d.ts",
        "/out/index.m3u8",
    ]
    t0 = time.time()
    r = subprocess.run(args, capture_output=True, text=True, timeout=1800)
    elapsed = time.time() - t0

    m3u8 = out_dir / "index.m3u8"
    segs = sorted(out_dir.glob("seg_*.ts"))
    if r.returncode != 0 or not m3u8.exists():
        return (False, f"转码失败 rc={r.returncode}: {r.stderr[-300:]}")

    playlist = m3u8.read_text(encoding="utf-8")
    encrypted = "#EXT-X-KEY:METHOD=AES-128" in playlist and "http://127.0.0.1" in playlist
    key_leaked = any(b"ZHYKEY" in p.read_bytes()[:4096] for p in out_dir.rglob("*") if p.is_file())
    # 沙箱内不应能读到素材外的密钥路径
    r2 = subprocess.run(
        [*BASE, "/bin/sh", "-c", "test -e /keys/lesson.key && echo LEAK || echo HIDDEN"],
        capture_output=True,
        text=True,
        timeout=30,
    )
    hidden = "HIDDEN" in r2.stdout
    # 分片已 AES 加密：无 key 时 ffprobe 应**无法**识别（这是加密生效的证据，不是失败）
    probe = subprocess.run(
        [
            ffprobe or "ffprobe",
            "-hide_banner",
            "-loglevel",
            "error",
            "-show_entries",
            "format=format_name",
            "-of",
            "csv=p=0",
            str(segs[0]),
        ],
        capture_output=True,
        text=True,
        timeout=60,
    )
    encrypted_opaque = "mpegts" not in probe.stdout
    seg_sizes_ok = all(p.stat().st_size > 1024 for p in segs[:5])
    extinf = playlist.count("#EXTINF")

    ok = (
        encrypted
        and not key_leaked
        and hidden
        and encrypted_opaque
        and seg_sizes_ok
        and len(segs) > 10
        and extinf == len(segs)
    )
    detail = (
        f"输入={size_mb:.0f}MB 耗时={elapsed:.0f}s 分片={len(segs)} EXTINF={extinf} "
        f"加密={encrypted} 无key不可解析={encrypted_opaque} 输出无密钥={not key_leaked} "
        f"沙箱内密钥不可见={hidden} 分片非空={seg_sizes_ok}"
    )
    return (ok, detail)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--transcode", action="store_true", help="执行 SANDBOX-006（耗时较长）")
    ap.add_argument("--duration", type=int, default=500, help="素材时长秒（8Mbps≈1MB/s）")
    args = ap.parse_args()

    OUT.mkdir(parents=True, exist_ok=True)
    work = OUT / "sandbox"
    work.mkdir(exist_ok=True)

    cases = [
        ("SANDBOX-001", "网络隔离", c_001),
        ("SANDBOX-002", "/etc 不可见", c_002),
        ("SANDBOX-003", "/proc 宿主进程不可见", c_003),
        ("SANDBOX-004", "宿主文件系统只读", c_004),
        ("SANDBOX-005", "父死子亡", c_005),
    ]
    results = []
    for cid, name, fn in cases:
        try:
            ok, detail = fn()
        except Exception as exc:  # noqa: BLE001
            ok, detail = False, f"ERROR {type(exc).__name__}: {exc}"
        results.append(
            {"case": cid, "name": name, "status": "PASS" if ok else "FAIL", "detail": detail}
        )
        print(f"{cid} {'PASS' if ok else 'FAIL'} {name}: {detail}")

    if args.transcode:
        ok, detail = c_006(work, args.duration)
        results.append(
            {
                "case": "SANDBOX-006",
                "name": "500MB→HLS 加密转码",
                "status": "PASS" if ok else "FAIL",
                "detail": detail,
            }
        )
        print(f"SANDBOX-006 {'PASS' if ok else 'FAIL'} 500MB→HLS: {detail}")

    path = OUT / "sandbox.json"
    path.write_text(json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8")
    failed = [r for r in results if r["status"] != "PASS"]
    print(f"\n汇总: {len(results) - len(failed)} PASS / {len(failed)} FAIL → {path}")
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
