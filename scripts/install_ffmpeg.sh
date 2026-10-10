#!/usr/bin/env bash
# 可选：安装系统级 ffmpeg。
# 注意：LinkWork 默认使用 imageio-ffmpeg 自带的静态 ffmpeg，
# 只有在需要自行处理超大视频时才需要安装系统 ffmpeg。
set -euo pipefail
if command -v brew >/dev/null 2>&1; then
  brew install ffmpeg
else
  echo "未检测到 Homebrew，可改为： pip install imageio-ffmpeg（默认已安装）" >&2
  exit 1
fi
