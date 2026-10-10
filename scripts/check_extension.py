#!/usr/bin/env python3
"""确认 LinkWork 扩展是否已在 Edge 中加载。

Edge 在手动「加载解压缩的扩展」后，会把扩展写进 Preferences 的
extensions.settings，其中 location=4（LOAD_UNPACKED）、path 指向扩展目录。
本脚本据此判断加载是否成功，并顺带检查本地服务是否就绪。

用法：
    .venv/bin/python scripts/check_extension.py            # 检查一次
    .venv/bin/python scripts/check_extension.py --watch     # 等待加载（最多 5 分钟）
"""

from __future__ import annotations

import json
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

EDGE_PREFS = Path.home() / "Library/Application Support/Microsoft Edge/Default/Preferences"
CHROME_PREFS = Path.home() / "Library/Application Support/Google/Chrome/Default/Preferences"
INSTALL_DIR = Path(__file__).resolve().parent.parent / "extension"
SERVICE_URL = "http://127.0.0.1:17890"

OK = "✅"
NO = "❌"
WAIT = "⏳"


def load_prefs() -> list[tuple[str, dict]]:
    """返回 [(浏览器名, extensions.settings), ...]

    注意：浏览器**运行中不会**把解压扩展写回 Preferences（退出时才落盘），
    所以本函数在浏览器开着时通常读不到结果——这不代表扩展没加载。
    想确认加载状态，请以 edge://extensions 页面显示为准，
    或用 scripts/verify_extension_browser.py --worker 做离线验证。
    """
    found: list[tuple[str, dict]] = []
    for name, path in (("Edge", EDGE_PREFS), ("Chrome", CHROME_PREFS)):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            continue
        found.append((name, data.get("extensions", {}).get("settings", {}) or {}))
    return found


def find_linkwork() -> list[tuple[str, str, str]]:
    """查找已加载的 LinkWork 扩展，返回 [(浏览器, 扩展ID, 名称), ...]"""
    hits: list[tuple[str, str, str]] = []
    for browser, settings in load_prefs():
        for ext_id, info in settings.items():
            if not isinstance(info, dict):
                continue
            name = str((info.get("manifest") or {}).get("name") or "")
            path = str(info.get("path") or "")
            if "LinkWork" in name or "LinkWork" in path:
                hits.append((browser, ext_id, name or path))
    return hits


def service_ok() -> tuple[bool, str]:
    try:
        with urllib.request.urlopen(f"{SERVICE_URL}/api/health", timeout=5) as resp:
            data = json.loads(resp.read())
        engines = [e["name"] for e in data.get("asr", []) if e.get("available")]
        return True, f"v{data.get('version')}，ASR：{', '.join(engines) or '无'}"
    except Exception as exc:  # noqa: BLE001
        return False, str(exc)


def report() -> bool:
    print("LinkWork 扩展加载检查")
    print("=" * 54)

    print(f"\n[插件源目录] {INSTALL_DIR}")
    if INSTALL_DIR.exists():
        files = sorted(p.name for p in INSTALL_DIR.iterdir())
        print(f"  {OK} 已就位：{', '.join(files)}")
    else:
        print(f"  {NO} 目录不存在，请先运行： ./scripts/install_extension.sh edge")
        return False

    print("\n[本地服务]")
    ok, detail = service_ok()
    if ok:
        print(f"  {OK} 运行中（{detail}）")
    else:
        print(f"  {NO} 未就绪：{detail}")
        print("     请运行： ./scripts/service.sh start")

    print("\n[浏览器扩展]")
    hits = find_linkwork()
    if hits:
        for browser, ext_id, name in hits:
            print(f"  {OK} {browser} 已加载：{name}")
            print(f"     扩展 ID：{ext_id}")
        print()
        print("下一步：点击 Edge 工具栏的 LinkWork 图标 → 右侧面板打开 → 粘贴链接提取")
        return True

    print(f"  {WAIT} 配置中未读到扩展记录")
    print("     说明：浏览器运行期间不会把解压扩展写回配置文件，退出时才落盘。")
    print("     请直接看 edge://extensions 页面是否已列出 LinkWork（这才是准的）。")
    print()
    print("如何确认扩展状态：")
    print("  · 打开 edge://extensions，看是否列出 LinkWork（以页面显示为准）")
    print("  · 离线验证后台脚本与侧边栏页面：")
    print("      .venv/bin/python scripts/verify_extension_browser.py --worker")
    print("      .venv/bin/python scripts/verify_extension_browser.py --panel")
    print()
    print("若尚未加载，在 edge://extensions 完成：")
    print("  1. 打开右上角「开发人员模式」")
    print("  2. 点「加载解压缩的扩展」")
    print(f"  3. 选择目录：{INSTALL_DIR}")
    return False


def touch_manifest() -> bool:
    """Touch 扩展目录里的文件，触发 Edge/Chrome 自动重新加载解压扩展。"""
    if not INSTALL_DIR.exists():
        return False
    touched = []
    for name in ("manifest.json", "background.js", "sidepanel.html"):
        f = INSTALL_DIR / name
        if f.exists():
            f.touch()
            touched.append(name)
    return bool(touched)


def main() -> int:
    if "--reload" in sys.argv:
        ok = touch_manifest()
        print("==> 已触发扩展自动重新加载" if ok else "==> 扩展目录不存在，无法重载")
        print("    若 Edge 没有自动生效，请在 edge://extensions 上点 LinkWork 卡片的「重新加载」")
        return 0 if ok else 1

    watch = "--watch" in sys.argv
    deadline = time.time() + 300
    while True:
        if report():
            return 0
        if not watch or time.time() > deadline:
            return 1
        print("\n（等待加载中，每 5 秒重试… Ctrl+C 退出）")
        time.sleep(5)
        print("\033[2J\033[H", end="")  # 清屏


if __name__ == "__main__":
    raise SystemExit(main())
