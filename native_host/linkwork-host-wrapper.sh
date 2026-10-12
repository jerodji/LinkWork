#!/bin/sh
# LinkWork native messaging host 启动包装。
#
# 浏览器会用「可执行文件 + 参数 chrome-extension://<id>/」的方式调用本脚本，
# 本脚本只负责找到项目根目录，然后用项目虚拟环境里的 Python 运行 host 主体。
#
# 为什么需要这一层：
#   1. 浏览器的 manifest 里必须是一个可执行文件（.py 不能直接执行）；
#   2. 浏览器进程的环境变量很干净，不能指望 PATH 里有 python；
#   3. 项目根目录固定在这里，避免 host 里再去猜路径。
#
# 自诊断：浏览器启动失败时只会给扩展一句「Native host has exited」，看不到任何细节，
# 所以本脚本先落一行日志（被调用了 + 参数 + Python 能不能跑）再 exec，
# 且把 stderr 也接到日志里，避免 Python 侧的报错被浏览器吞掉。
# 排查入口：~/Library/Logs/LinkWork-native-host.log（写不了则退到 native_host/logs/）
#
# 由 scripts/install_native_host.py 安装。

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
PY="$ROOT/.venv/bin/python"
HOST="$ROOT/native_host/linkwork_host.py"

LOG_DIR="$HOME/Library/Logs"
mkdir -p "$LOG_DIR" 2>/dev/null || true
if ! ( : >> "$LOG_DIR/LinkWork-native-host.log" ) 2>/dev/null; then
  LOG_DIR="$ROOT/native_host/logs"
  mkdir -p "$LOG_DIR" 2>/dev/null || true
fi
HOST_LOG="$LOG_DIR/LinkWork-native-host.log"

log() {
  printf '%s [wrapper] %s\n' "$(date '+%Y-%m-%d %H:%M:%S')" "$1" >> "$HOST_LOG" 2>/dev/null || true
}

log "被浏览器调用：arg1=${1:-（无）} root=$ROOT"

if [ ! -x "$PY" ]; then
  log "找不到可执行的解释器：$PY（请先运行 ./scripts/setup.sh）"
  # 只能写 stderr：stdout 是 native messaging 协议通道，写坏会让浏览器断开
  echo "LinkWork: 找不到虚拟环境解释器 $PY，请先运行 ./scripts/setup.sh" >&2
  exit 1
fi

log "解释器：$("$PY" -V 2>&1)，即将 exec host"

exec "$PY" "$HOST" "$@" 2>>"$HOST_LOG"
