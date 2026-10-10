#!/usr/bin/env bash
# 在浏览器开发者模式下加载 LinkWork 扩展（Edge / Chrome）
#
#   ./scripts/install_extension.sh            打开 Edge 扩展页
#   ./scripts/install_extension.sh chrome     打开 Chrome 扩展页
#
# 说明：扩展以「加载已解压的扩展程序」方式安装。
# 本项目只使用一个目录作为唯一来源，避免出现两份不同步的副本：
#     <项目根目录>/extension
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
EXT_DIR="$ROOT/extension"
TARGET_BROWSER="${1:-edge}"

if [ ! -f "$EXT_DIR/manifest.json" ]; then
  echo "❌ 找不到扩展清单：$EXT_DIR/manifest.json" >&2
  exit 1
fi

case "$TARGET_BROWSER" in
  edge|Edge|EDGE)   APP_NAME="Microsoft Edge";  EXT_URL="edge://extensions/" ;;
  chrome|Chrome|CHROME) APP_NAME="Google Chrome"; EXT_URL="chrome://extensions/" ;;
  *) echo "❌ 未知浏览器：$TARGET_BROWSER（支持 edge / chrome）" >&2; exit 2 ;;
esac

APP_PATH="/Applications/$APP_NAME.app"
[ -d "$APP_PATH" ] || { echo "❌ 未找到 $APP_NAME" >&2; exit 1; }

echo "==> 打开 $APP_NAME 扩展页"
open -a "$APP_NAME" "$EXT_URL" || true
sleep 3
osascript <<APPLESCRIPT >/dev/null 2>&1 || true
tell application "$APP_NAME"
  activate
  if (count of windows) = 0 then make new window
  set URL of active tab of front window to "$EXT_URL"
end tell
APPLESCRIPT

cat <<MSG

────────────────────────────────────────────────────────────
在扩展页完成（只需一次）：

1. 打开右上角【开发人员模式 / Developer mode】
2. 点击【加载解压缩的扩展 / Load unpacked】
3. 选择这个目录（可在对话框按 Cmd+Shift+G 粘贴）：

   $EXT_DIR

之后点击工具栏的 LinkWork 图标，右侧即打开提取面板。

· 以后每次改完代码，在扩展页点该卡片的【重新加载】即可生效
· 也可运行 ./scripts/check_extension.py --reload 触发自动重载
────────────────────────────────────────────────────────────
MSG
