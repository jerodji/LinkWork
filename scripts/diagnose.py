#!/usr/bin/env python3
"""一键查看诊断信息。

用法：
    .venv/bin/python scripts/diagnose.py            # 打印可读摘要
    .venv/bin/python scripts/diagnose.py --json     # 打印完整 JSON（可直接贴给我）
    .venv/bin/python scripts/diagnose.py --log 名称  # 打印某个任务的完整日志

也可以直接用浏览器打开：
    http://127.0.0.1:17890/api/diagnostics
"""

from __future__ import annotations

import argparse
import json
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
BASE = "http://127.0.0.1:17890"
SAVE_TO = ROOT / "output" / "diagnostics.json"


def fetch(path: str) -> dict:
    try:
        with urllib.request.urlopen(BASE + path, timeout=20) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.URLError as exc:
        print(f"❌ 无法连接本地服务（{exc}）。请先启动：./scripts/service.sh start", file=sys.stderr)
        raise SystemExit(1) from exc


def print_summary(data: dict) -> None:
    summary = data.get("summary", {})
    print("=" * 64)
    print(f"LinkWork {data.get('version')}   诊断摘要")
    print("=" * 64)
    for line in data.get("environment", []):
        print(f"  {line}")
    print()
    print(f"  任务日志目录：{data.get('log_dir')}")
    print(f"  服务主日志：  {data.get('service_log')}")
    print()
    print(
        f"  最近任务：{summary.get('recent', 0)} 个"
        f"，失败 {summary.get('failed', 0)} 个"
    )
    last = summary.get("last_outcome", "none")
    mark = {"done": "✅", "error": "❌"}.get(last, "·")
    print(f"  最后一次：{mark} {last}  ({summary.get('last_at', '')})")
    if summary.get("last_detail"):
        print(f"            {summary['last_detail'][:80]}")
    print()

    logs = data.get("logs", [])
    if logs:
        print("-" * 64)
        print("最近任务（新的在前）：")
        for item in logs:
            mark = {"done": "✅", "error": "❌"}.get(item.get("outcome"), "·")
            print(f"  {mark} {item.get('name')}")
            print(f"       {str(item.get('detail', ''))[:76]}")
            stages = item.get("stages") or []
            if stages:
                print(f"       阶段: {' → '.join(stages)}")
    print()

    tail = data.get("service_log_tail", [])
    if tail:
        print("-" * 64)
        print("服务日志（本次启动后）：")
        for line in tail[-25:]:
            print(f"  {line[:110]}")
    print()
    print("=" * 64)
    print(f"完整 JSON 已保存：{SAVE_TO}")
    print("把这个文件发我即可排查。")


def main() -> int:
    parser = argparse.ArgumentParser(description="查看 LinkWork 诊断信息")
    parser.add_argument("--json", action="store_true", help="只打印完整 JSON")
    parser.add_argument("--log", metavar="名称", help="打印指定任务的完整日志")
    args = parser.parse_args()

    if args.log:
        data = fetch(f"/api/diagnostics/log?name={urllib.parse.quote(args.log)}")
        print(data.get("text", ""))
        return 0

    data = fetch("/api/diagnostics")
    try:
        SAVE_TO.parent.mkdir(parents=True, exist_ok=True)
        SAVE_TO.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    except OSError:
        pass

    if args.json:
        print(json.dumps(data, ensure_ascii=False, indent=2))
    else:
        print_summary(data)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
