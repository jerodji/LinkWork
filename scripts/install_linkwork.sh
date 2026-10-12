#!/usr/bin/env bash
#
# LinkWork 一键安装（macOS）
#
# 做四件事：
#   1. 装 Python 依赖（自动建 .venv，含 ffmpeg 与 faster-whisper）
#   2. 注册「本机启动器」（native messaging host）：编译 C 启动器 + 写浏览器注册文件
#   3. 提示你在浏览器里加载插件（这一步浏览器不允许自动化，只能手动点两下）
#   4. 之后打开右侧面板就会自动启动本地服务，不用再开终端
#
# 用法（在项目目录里）：
#     ./scripts/install_linkwork.sh              # 自动检测 Edge / Chrome
#     ./scripts/install_linkwork.sh edge         # 只注册 Edge
#     ./scripts/install_linkwork.sh chrome       # 只注册 Chrome
#     ./scripts/install_linkwork.sh --skip-deps  # 跳过依赖安装（已装过）
#
# 也可以不在项目里跑（会自动下载到 ~/LinkWork）：
#     curl -fsSL <repo>/scripts/install_linkwork.sh | bash
#
set -euo pipefail

REPO_URL="${LINKWORK_REPO_URL:-}"
DEFAULT_DIR="$HOME/LinkWork"

say()  { printf '\n==> %s\n' "$1"; }
ok()   { printf '✅ %s\n' "$1"; }
info() { printf '·  %s\n' "$1"; }
warn() { printf '⚠️  %s\n' "$1" >&2; }
die()  { printf '❌ %s\n' "$1" >&2; exit 1; }

# 允许用环境变量强制指定项目目录
if [ -n "${LINKWORK_DIR:-}" ]; then
  cd "$LINKWORK_DIR" 2>/dev/null || die "LINKWORK_DIR 不存在：$LINKWORK_DIR"
fi

SKIP_DEPS=0
BROWSER=""
for arg in "$@"; do
  case "$arg" in
    --skip-deps) SKIP_DEPS=1 ;;
    -h|--help) sed -n '2,20p' "$0"; exit 0 ;;
    edge|chrome) BROWSER="$arg" ;;
    *) warn "忽略未知参数：$arg" ;;
  esac
done

say "LinkWork 一键安装"

# ---------------------------------------------------------------- 1. 找项目
ROOT=""
if [ -f "./pyproject.toml" ] && [ -d "./extension" ] && [ -d "./native_host" ]; then
  ROOT="$(pwd)"
  ok "在项目目录里运行：$ROOT"
elif [ -f "$(dirname "$0")/../pyproject.toml" ]; then
  ROOT="$(cd "$(dirname "$0")/.." && pwd)"
  ok "从脚本位置定位到项目：$ROOT"
  cd "$ROOT"
else
  # 通过 curl | bash 运行：先下载项目
  if [ -z "$REPO_URL" ]; then
    die "当前不在 LinkWork 项目目录里。请先 git clone 项目后进入目录运行，或设置 LINKWORK_REPO_URL=<仓库地址> 再执行本脚本。"
  fi
  command -v git >/dev/null 2>&1 || die "需要 git 才能下载项目"
  if [ -d "$DEFAULT_DIR/.git" ]; then
    say "更新已有项目：$DEFAULT_DIR"
    git -C "$DEFAULT_DIR" pull --ff-only
  else
    say "下载项目到：$DEFAULT_DIR"
    git clone --depth 1 "$REPO_URL" "$DEFAULT_DIR"
  fi
  ROOT="$DEFAULT_DIR"
  cd "$ROOT"
fi

PY="$ROOT/.venv/bin/python"
SETUP_SH="$ROOT/scripts/setup.sh"
REGISTER_PY="$ROOT/scripts/install_native_host.py"

# ------------------------------------------------------------ 2. 依赖（.venv）
if [ ! -x "$PY" ]; then
  if [ "$SKIP_DEPS" = "1" ]; then
    die "没有找到虚拟环境 $PY。请先运行 ./scripts/setup.sh（或去掉 --skip-deps）"
  fi
  [ -f "$SETUP_SH" ] || die "找不到 $SETUP_SH"
  say "首次安装：装 Python 依赖（约 1–3 分钟，请勿关闭窗口）"
  bash "$SETUP_SH"
else
  ok "虚拟环境已就绪：${PY}（$("$PY" -V 2>&1)）"
fi
[ -x "$PY" ] || die "依赖安装后仍找不到 $PY"

# ------------------------------------------- 3. 注册本机启动器（让面板能启动服务）
[ -f "$REGISTER_PY" ] || die "找不到 $REGISTER_PY"
say "注册本机启动器（编译 C 启动器 + 写浏览器注册文件）"
if [ -n "$BROWSER" ]; then
  "$PY" "$REGISTER_PY" "$BROWSER"
else
  "$PY" "$REGISTER_PY"
fi

# ------------------------------------------------------------ 4. 下一步提示
EXT_DIR="$ROOT/extension"
say "最后一步：在浏览器里加载插件（浏览器不允许脚本代劳，需要你点两下）"
cat <<MSG

  1. 打开扩展管理页
       Edge：   edge://extensions/
       Chrome： chrome://extensions/
  2. 打开右上角的【开发人员模式 / Developer mode】
  3. 点【加载解压缩的扩展 / Load unpacked】，选择这个目录（对话框中按 Cmd+Shift+G 可粘贴路径）：

       $EXT_DIR

  4. 点击工具栏的 LinkWork 图标，右侧面板打开时若服务没跑，会自动把它启动起来
     （右上角出现绿色「连接已开启」= 一切就绪）

  注意：注册本机启动器后必须**完全退出并重开浏览器**一次才生效（Cmd+Q，不是关窗口）。

MSG

# 顺手打开扩展页，省得你自己找
if command -v open >/dev/null 2>&1; then
  case "${BROWSER:-edge}" in
    chrome) open -a "Google Chrome" "chrome://extensions/" 2>/dev/null || true ;;
    *)      open -a "Microsoft Edge" "edge://extensions/" 2>/dev/null || true ;;
  esac
fi

ok "安装流程结束。若面板显示「服务未连接」，请看：$HOME/Library/Logs/LinkWork-native-host.log"
