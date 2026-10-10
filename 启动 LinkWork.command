#!/usr/bin/env bash
# 双击启动：拉起本地服务并在 Edge 中打开扩展管理页
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$ROOT"

echo "==> LinkWork 启动器"
echo

if [ ! -x ".venv/bin/python" ]; then
  echo "首次运行，正在安装依赖（约 1-3 分钟，请勿关闭窗口）…"
  bash "$ROOT/scripts/setup.sh"
fi

echo "==> 启动本地服务"
bash "$ROOT/scripts/service.sh" start
echo

echo "==> 确保浏览器插件文件已就位"
bash "$ROOT/scripts/install_extension.sh" edge >/dev/null 2>&1 || true

cat <<'MSG'
────────────────────────────────────────────────────────────
本地服务已就绪。接下来在 Edge 里确认插件（只需一次）：

  1. 在扩展页面右上角打开【开发人员模式】
  2. 点击【加载解压缩的扩展】
  3. 选择： ~/Library/Application Support/LinkWork/extension

之后点击工具栏的 LinkWork 图标，右侧即会打开提取面板。
────────────────────────────────────────────────────────────
MSG
read -r -p "按回车键关闭此窗口…" _ || true
