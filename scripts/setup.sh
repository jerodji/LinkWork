#!/usr/bin/env bash
# LinkWork 一键安装（macOS）
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

echo "==> LinkWork 安装向导"
echo "项目目录：$ROOT"

# 1) 找一个 >= 3.10 且带 tkinter 的 Python
PY=""
if command -v uv >/dev/null 2>&1; then
  echo "==> 检测到 uv，使用 uv 创建虚拟环境（推荐）"
  # uv 会按需下载带 tkinter 的 CPython（python-build-standalone 自带 Tk）
  uv venv --python 3.12 .venv
  PY="$ROOT/.venv/bin/python"
else
  for candidate in python3.12 python3.11 python3.10 python3; do
    if command -v "$candidate" >/dev/null 2>&1; then
      if "$candidate" - <<'PYCHK' >/dev/null 2>&1
import sys, tkinter
assert sys.version_info >= (3, 10)
PYCHK
      then
        PY="$candidate"
        break
      fi
    fi
  done
  if [ -z "$PY" ]; then
    echo "❌ 未找到可用的 Python（需 >=3.10 且包含 tkinter）。" >&2
    echo "   建议先安装 uv：curl -LsSf https://astral.sh/uv/install.sh | sh" >&2
    exit 1
  fi
  echo "==> 使用 $PY 创建虚拟环境"
  "$PY" -m venv .venv
  PY="$ROOT/.venv/bin/python"
fi

echo "==> 虚拟环境 Python：$("$PY" -V)"

# 2) 安装依赖
if command -v uv >/dev/null 2>&1; then
  uv pip install --python "$PY" -e ".[asr,article,apple]"
else
  "$PY" -m pip install --upgrade pip
  "$PY" -m pip install -e ".[asr,article,apple]"
fi

# 3) 自检
echo
"$PY" -m linkwork doctor || true

cat <<'MSG'

==> 安装完成 ✅

接下来三步：
  1) 启动本地服务：   ./scripts/service.sh start
  2) 安装浏览器插件： ./scripts/install_extension.sh edge
  3) 在 Edge 扩展页开启「开发人员模式」→「加载解压缩的扩展」→ 选择
     ~/Library/Application Support/LinkWork/extension

也可以直接双击项目根目录的「启动 LinkWork.command」自动完成 1、2 步。
命令行用法： .venv/bin/linkwork "视频或文章链接"
MSG
