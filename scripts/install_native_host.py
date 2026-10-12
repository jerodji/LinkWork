#!/usr/bin/env python3
"""把 LinkWork native messaging host 注册到 Edge / Chrome。

为什么需要这一步：浏览器只允许「已在它自己配置里登记过、且指名允许某个扩展 ID」
的本机程序通过 native messaging 通信。所以要把一个 manifest（JSON）写到浏览器的
NativeMessagingHosts 目录，里面写明：

    path                     ← 要启动的本机程序（我们的 shell 包装脚本）
    allowed_origins          ← 允许调用它的扩展 ID（本项目是解压加载，ID 由目录路径决定）

用法：
    .venv/bin/python scripts/install_native_host.py            # 注册到 Edge（自动识别扩展 ID）
    .venv/bin/python scripts/install_native_host.py chrome     # 注册到 Chrome
    .venv/bin/python scripts/install_native_host.py edge --print   # 只看不写
    .venv/bin/python scripts/install_native_host.py edge --add-id <扩展ID>

扩展 ID 的来源（按优先级）：
    1. --add-id 显式指定；
    2. 从浏览器 Preferences 里读「已加载的解压扩展」（最准，推荐先按 README 加载扩展再跑本脚本）；
    3. 按本项目 extension/ 目录路径推导（Chrome 对解压扩展就是这么算 ID 的）。

注册后需要重启浏览器（或至少重新加载扩展）才会生效。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

HOST_NAME = "com.linkwork.host"
ROOT = Path(__file__).resolve().parent.parent
WRAPPER = ROOT / "native_host" / "linkwork-host-wrapper.sh"
LAUNCHER_SRC = ROOT / "native_host" / "linkwork-launcher.c"
LAUNCHER_BIN = ROOT / "native_host" / "linkwork-host"
CONF = ROOT / "native_host" / "host.conf"
HOST_SCRIPT = ROOT / "native_host" / "linkwork_host.py"
VENV_PYTHON = ROOT / ".venv" / "bin" / "python"
EXT_DIR = ROOT / "extension"

BROWSERS = {
    "edge": {
        "app": "Microsoft Edge",
        "support": Path.home() / "Library/Application Support/Microsoft Edge",
    },
    "chrome": {
        "app": "Google Chrome",
        "support": Path.home() / "Library/Application Support/Google Chrome",
    },
}

OK = "✅"
NO = "❌"
INFO = "·"


def extension_id_from_path(path: Path) -> str:
    """复刻 Chrome 对解压扩展的 ID 算法：sha256(绝对路径) 前 16 字节，每字节拆成两个 a-p。"""
    digest = hashlib.sha256(str(path).encode("utf-8")).digest()[:16]
    return "".join(chr(ord("a") + (b >> 4)) + chr(ord("a") + (b & 0xF)) for b in digest)


def loaded_extension_ids(support_dir: Path) -> list[str]:
    """从浏览器的 Preferences 里找已加载的 LinkWork 扩展 ID。

    为什么要扫所有 profile：一个浏览器可能有 Default / Profile 1 / Profile 2，
    扩展装在不常用的那个里；而且浏览器**运行中不会把解压扩展写回 Preferences**
    （退出时才落盘），所以读不到属正常，此时调用方会退回「按目录推导」。
    """
    found: list[str] = []
    try:
        candidates = sorted(support_dir.glob("*/Preferences"))
    except OSError:
        return found
    for prefs in candidates:
        try:
            data = json.loads(prefs.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        settings = (data.get("extensions") or {}).get("settings") or {}
        for ext_id, info in settings.items():
            if not isinstance(info, dict):
                continue
            name = str((info.get("manifest") or {}).get("name") or "")
            path = str(info.get("path") or "")
            if ("LinkWork" in name or "LinkWork" in path) and ext_id not in found:
                found.append(ext_id)
    return found


def scan_browser_extension_ids() -> dict[str, list[str]]:
    """扫 Edge 与 Chrome 的所有配置，返回 {浏览器: [已加载的 LinkWork 扩展 ID]}。"""
    return {key: loaded_extension_ids(spec["support"]) for key, spec in BROWSERS.items()}


def build_manifest(ext_ids: list[str], program: Path) -> dict:
    return {
        "name": HOST_NAME,
        "description": "LinkWork 本地服务启动器（供浏览器扩展调用）",
        "path": str(program),
        "type": "stdio",
        "allowed_origins": [f"chrome-extension://{ext_id}/" for ext_id in ext_ids],
    }


def write_launcher_conf() -> None:
    """写给 C 启动器读的配置：解释器、host 脚本、日志落点（按优先级）。"""
    log_candidates = [
        Path.home() / "Library" / "Logs" / "LinkWork-native-host.log",
        ROOT / "native_host" / "logs" / "LinkWork-native-host.log",
        Path(tempfile.gettempdir()) / "LinkWork-native-host.log",
    ]
    lines = [
        f"python={VENV_PYTHON}",
        f"host={HOST_SCRIPT}",
        *(f"log={path}" for path in log_candidates),
    ]
    CONF.write_text("\n".join(lines) + "\n", encoding="utf-8")


def build_launcher() -> Path | None:
    """把 C 启动器编译成二进制。

    为什么非要编译：浏览器在 macOS 上执行 shell 脚本会被系统安全策略拦掉，
    浏览器只报「Native host has exited」，连日志都没有。编译成真正的可执行文件
    才能走通（这台机器上能正常工作的原生程序都是 Mach-O 二进制）。
    编译失败就退回到 shell 包装脚本（至少还能用在别的环境下）。
    """
    if not LAUNCHER_SRC.exists():
        return None
    compiler = shutil.which("cc") or shutil.which("clang")
    if not compiler:
        return None
    if LAUNCHER_BIN.exists():
        try:
            LAUNCHER_BIN.unlink()  # 重新编译，避免用到旧版本
        except OSError:
            pass
    result = subprocess.run(
        [compiler, "-O2", "-Wall", "-o", str(LAUNCHER_BIN), str(LAUNCHER_SRC)],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        print(f"{INFO} 编译启动器失败（将退回 shell 包装脚本）：{result.stderr.strip()[:200]}")
        return None
    if result.stderr.strip():
        print(f"{INFO} 编译告警：{result.stderr.strip()[:200]}")
    LAUNCHER_BIN.chmod(0o755)
    return LAUNCHER_BIN


def installed_browsers() -> list[str]:
    """本机装了哪些受支持的浏览器（按 Edge → Chrome 顺序）。"""
    return [
        key for key, spec in BROWSERS.items()
        if Path(f"/Applications/{spec['app']}.app").exists() or spec["support"].exists()
    ]


def ensure_launcher() -> Path:
    """编译启动器（失败则退回 shell 包装脚本），返回要写进 manifest 的程序路径。"""
    launcher = build_launcher()
    if launcher:
        print(f"{OK} 已编译启动器：{launcher}")
        write_launcher_conf()
        print(f"{OK} 已写入配置：{CONF}")
        for line in CONF.read_text(encoding="utf-8").splitlines():
            print(f"    {line}")
        return launcher
    print(f"{INFO} 没有可用的 C 编译器（cc/clang），退回 shell 包装脚本")
    print(f"    {INFO} ⚠️ macOS 上浏览器很可能无法执行它（表现为 Native host has exited）")
    WRAPPER.chmod(0o755)
    return WRAPPER


def resolve_extension_ids(browser_key: str, extra_ids: list[str]) -> tuple[list[str], bool]:
    """确定要给哪个浏览器放行哪些扩展 ID。

    返回 (扩展 ID 列表, 是否读到了真实 ID)。
    真实 ID 优先（从浏览器配置读），读不到就按本项目 extension/ 目录推导。
    """
    support_dir = BROWSERS[browser_key]["support"]
    ext_ids: list[str] = []
    for item in extra_ids:
        item = item.strip()
        if item and item not in ext_ids:
            ext_ids.append(item)

    loaded = loaded_extension_ids(support_dir)
    for ext_id in loaded:
        if ext_id not in ext_ids:
            ext_ids.append(ext_id)
    if loaded:
        print(f"{OK} 从 {BROWSERS[browser_key]['app']} 配置读到已加载的扩展 ID：{', '.join(loaded)}")
    else:
        print(f"{INFO} 没能从 {BROWSERS[browser_key]['app']} 配置读到扩展 ID"
              "（浏览器运行中不落盘，属正常；插件其实也还没装）")

    derived = extension_id_from_path(EXT_DIR)
    if not ext_ids:
        ext_ids.append(derived)
        print(f"{INFO} 先按扩展目录推导 ID：{derived}")
        print(f"    {INFO} 装好插件后，把 edge://extensions 上看到的真实 ID 用 --add-id 传进来重跑一次即可")
    elif derived not in ext_ids:
        ext_ids.append(derived)
        print(f"{INFO} 同时保留按扩展目录推导的 ID：{derived}")
    return ext_ids, bool(loaded)


def register_one(browser_key: str, extra_ids: list[str], *, dry_run: bool) -> int:
    """把 native host 注册到指定浏览器。"""
    spec = BROWSERS[browser_key]
    support_dir = spec["support"]
    app_installed = Path(f"/Applications/{spec['app']}.app").exists()

    print(f"\n=== 注册到 {spec['app']} ===")
    if not app_installed:
        print(f"{INFO} 未检测到 /Applications/{spec['app']}.app（仍会写入注册文件，装了浏览器后即可生效）")

    ext_ids, _ = resolve_extension_ids(browser_key, extra_ids)
    manifest = build_manifest(ext_ids, program_for_manifest())
    target_dir = support_dir / "NativeMessagingHosts"
    target = target_dir / f"{HOST_NAME}.json"

    print(f"{INFO} 注册文件：{target}")
    if dry_run:
        print(json.dumps(manifest, ensure_ascii=False, indent=2))
        print(f"{INFO} --print 模式：未写入任何文件")
        return 0

    try:
        target_dir.mkdir(parents=True, exist_ok=True)
        target.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    except OSError as exc:
        print(f"{NO} 写入失败：{exc}", file=sys.stderr)
        print(f"    {INFO} 需要在有权限的终端里运行（不要放在受限的沙箱/IDE 终端里）", file=sys.stderr)
        return 1
    print(f"{OK} 已写入：{target}")
    print(f"    放行的扩展 ID：{', '.join(ext_ids)}")
    return 0


# 启动器编译一次即可复用到多个浏览器
_PROGRAM: Path | None = None


def program_for_manifest() -> Path:
    global _PROGRAM
    if _PROGRAM is None:
        _PROGRAM = ensure_launcher()
    return _PROGRAM


def main() -> int:
    parser = argparse.ArgumentParser(description="注册 LinkWork native messaging host")
    parser.add_argument(
        "browser", nargs="?", default=None, choices=sorted(BROWSERS),
        help="目标浏览器（默认：装了哪些就注册哪些）",
    )
    parser.add_argument("--add-id", action="append", default=[], help="额外允许的扩展 ID（可重复）")
    parser.add_argument("--print", dest="dry_run", action="store_true", help="只打印将写入的内容，不落盘")
    args = parser.parse_args()

    print("LinkWork native host 注册")
    print("=" * 58)
    print(f"{INFO} 项目根目录：{ROOT}")
    if not HOST_SCRIPT.exists():
        print(f"{NO} host 脚本不存在：{HOST_SCRIPT}", file=sys.stderr)
        return 1
    if not VENV_PYTHON.exists():
        print(f"{NO} 虚拟环境解释器不存在：{VENV_PYTHON}；请先运行 ./scripts/setup.sh", file=sys.stderr)
        return 1

    targets = [args.browser] if args.browser else installed_browsers()
    if not targets:
        print(f"{INFO} 没有检测到 Edge / Chrome；默认按 Edge 写入注册文件")
        targets = ["edge"]

    # 先编译一次（不依赖浏览器），失败会退回 shell 包装脚本
    global _PROGRAM
    _PROGRAM = ensure_launcher()

    failures = 0
    for browser_key in targets:
        failures += register_one(browser_key, args.add_id, dry_run=args.dry_run)

    if args.dry_run or failures:
        return 1 if failures else 0

    print()
    print("接下来（只需一次）：")
    print("  1. 完全退出并重开浏览器（native host 只在浏览器启动时读取注册文件）")
    print("  2. 打开 LinkWork 侧边栏：服务没跑时会自动拉起，右上角出现绿色「连接已开启」")
    print(f"  3. 排查问题看日志：{Path.home() / 'Library/Logs/LinkWork-native-host.log'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
