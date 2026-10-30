#!/usr/bin/env bash
# ZhyOnlineEdu · FFmpeg bwrap 启动器（F7 · §10.3.2.3）
# 八层沙箱：检疫/降权/无网/seccomp/资源/输出白名单/密钥隔离/失败处置。
# 仅允许经 zhyonlineedu.core.media.runtime.run_sandboxed 调用（§4.2 红线 4）。
#
# M0 实测结论（见 spike/RESULTS.md）：
#  - bwrap 的 --seccomp 接收的是**原始 BPF**，不能直接喂 JSON；
#    seccomp 由容器运行时下发（compose security_opt: seccomp=deploy/ffmpeg/seccomp.json）。
#  - /etc 用 tmpfs 屏蔽后，ffmpeg 的动态库软链（libblas 等）需要
#    /etc/alternatives 单独只读挂入，否则报 "cannot open shared object file"。
set -euo pipefail

INPUT="$1"      # 已规范化路径（魔数检疫通过、白名单格式）
OUTPUT_DIR="$2" # tmpfs /tmp/transcode/<task_id>
PROFILE="$3"    # 枚举：360p/720p/1080p/audio

case "$PROFILE" in
  360p)  VF="-vf scale=-2:360" ;;
  720p)  VF="-vf scale=-2:720" ;;
  1080p) VF="-vf scale=-2:1080" ;;
  audio) VF="-vn" ;;
  *) echo "profile 非法: $PROFILE" >&2; exit 64 ;;
esac

exec bwrap \
  --unshare-user --unshare-pid --unshare-net --unshare-ipc --unshare-uts \
  --die-with-parent --new-session \
  --ro-bind /usr /usr --ro-bind /lib /lib --ro-bind /lib64 /lib64 --ro-bind /bin /bin \
  --proc /proc --dev /dev --tmpfs /tmp --tmpfs /etc \
  --ro-bind /etc/alternatives /etc/alternatives \
  --ro-bind "$INPUT" /in/input \
  --bind "$OUTPUT_DIR" /out \
  --chdir /out \
  -- /usr/bin/ffmpeg -hide_banner -nostdin -y \
     -i /in/input $VF -profile:v main -preset veryfast \
     -f hls -hls_time 6 -hls_playlist_type vod /out/master.m3u8
