#!/usr/bin/env bash
# LinkWork 本地服务管理脚本（供浏览器插件后台调用）
#
#   ./scripts/service.sh start     启动（后台运行，日志写入 ~/Library/Logs/LinkWork-service.log）
#   ./scripts/service.sh stop      停止
#   ./scripts/service.sh status    查看状态
#   ./scripts/service.sh restart   重启
#   ./scripts/service.sh foreground  前台运行（调试用，Ctrl+C 结束）
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PY="$ROOT/.venv/bin/python"
PORT="${LINKWORK_PORT:-17890}"
PID_FILE="$HOME/Library/Logs/LinkWork-service.pid"
LOG_FILE="$HOME/Library/Logs/LinkWork-service.log"
mkdir -p "$(dirname "$LOG_FILE")"

if [ ! -x "$PY" ]; then
  echo "未找到虚拟环境，请先运行： ./scripts/setup.sh" >&2
  exit 1
fi

running() {
  [ -f "$PID_FILE" ] && kill -0 "$(cat "$PID_FILE")" 2>/dev/null
}

case "${1:-start}" in
  start)
    if running; then
      echo "服务已在运行（PID $(cat "$PID_FILE")，端口 ${PORT}）"
      exit 0
    fi
    echo "==> 启动 LinkWork 本地服务（端口 ${PORT}）"
    nohup "$PY" -m linkwork serve --port "$PORT" >>"$LOG_FILE" 2>&1 &
    echo $! > "$PID_FILE"
    sleep 2
    if running; then
      echo "✅ 已启动（PID $(cat "$PID_FILE")）"
      echo "   日志： tail -f $LOG_FILE"
      curl -s -m 5 "http://127.0.0.1:${PORT}/api/health" >/dev/null && echo "   健康检查：通过" || echo "   ⚠️ 健康检查失败，请查看日志"
    else
      echo "❌ 启动失败，日志末尾：" >&2
      tail -20 "$LOG_FILE" >&2
      exit 1
    fi
    ;;
  stop)
    if running; then
      kill "$(cat "$PID_FILE")" && echo "已停止（PID $(cat "$PID_FILE")）"
      rm -f "$PID_FILE"
    else
      echo "服务未在运行"
      rm -f "$PID_FILE"
    fi
    ;;
  status)
    if running; then
      echo "运行中（PID $(cat "$PID_FILE")，端口 ${PORT}）"
      curl -s -m 5 "http://127.0.0.1:${PORT}/api/health" | head -c 300
      echo
    else
      echo "未运行"
    fi
    ;;
  restart)
    "$0" stop || true
    sleep 1
    "$0" start
    ;;
  foreground)
    exec "$PY" -m linkwork serve --port "$PORT"
    ;;
  *)
    echo "用法： $0 {start|stop|status|restart|foreground}" >&2
    exit 2
    ;;
esac
