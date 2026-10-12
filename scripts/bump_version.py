#!/usr/bin/env python3
"""统一递增版本号（一处命令，四处同步）。

每次改动代码后运行一次：

    .venv/bin/python scripts/bump_version.py            # 补丁号 +1（0.2.1 → 0.2.2）
    .venv/bin/python scripts/bump_version.py --minor    # 次版本 +1（0.2.1 → 0.3.0）
    .venv/bin/python scripts/bump_version.py --major    # 主版本 +1（0.2.1 → 1.0.0）
    .venv/bin/python scripts/bump_version.py --set 0.4.0  # 直接指定

同步的位置（全部由 VERSION 文件派生）：
    VERSION                             版本唯一来源
    pyproject.toml                      Python 包版本
    src/linkwork/__init__.py            __version__
    extension/manifest.json             浏览器扩展版本
    extension/extractor.js              取数逻辑 build 序号（每次改动 +1）
    README.md                           「当前版本」一行（给使用者看的版本号）
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
VERSION_FILE = ROOT / "VERSION"
MANIFEST = ROOT / "extension" / "manifest.json"
PYPROJECT = ROOT / "pyproject.toml"
INIT = ROOT / "src" / "linkwork" / "__init__.py"
EXTRACTOR = ROOT / "extension" / "extractor.js"
README = ROOT / "README.md"


def read_version() -> str:
    return VERSION_FILE.read_text(encoding="utf-8").strip()


def bump(version: str, part: str) -> str:
    major, minor, patch = (int(x) for x in version.split(".")[:3])
    if part == "major":
        return f"{major + 1}.0.0"
    if part == "minor":
        return f"{major}.{minor + 1}.0"
    return f"{major}.{minor}.{patch + 1}"


def write_all(version: str, *, bump_build: bool = True) -> dict:
    """把版本写到所有位置，返回各位置的写入结果。"""
    VERSION_FILE.write_text(version + "\n", encoding="utf-8")
    changed: dict[str, str] = {"VERSION": version}

    # pyproject.toml
    text = PYPROJECT.read_text(encoding="utf-8")
    new = re.sub(r'(?m)^version = "[^"]+"', f'version = "{version}"', text, count=1)
    PYPROJECT.write_text(new, encoding="utf-8")
    changed["pyproject.toml"] = version

    # __init__.py
    text = INIT.read_text(encoding="utf-8")
    new = re.sub(r'__version__ = "[^"]+"', f'__version__ = "{version}"', text, count=1)
    INIT.write_text(new, encoding="utf-8")
    changed["src/linkwork/__init__.py"] = version

    # manifest.json
    data = json.loads(MANIFEST.read_text(encoding="utf-8"))
    data["version"] = version
    MANIFEST.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    changed["extension/manifest.json"] = version

    # extractor build
    text = EXTRACTOR.read_text(encoding="utf-8")
    match = re.search(r"const EXTRACTOR_BUILD = (\d+);", text)
    if match:
        current = int(match.group(1))
        nxt = current + 1 if bump_build else current
        text = text.replace(f"const EXTRACTOR_BUILD = {current};", f"const EXTRACTOR_BUILD = {nxt};", 1)
        EXTRACTOR.write_text(text, encoding="utf-8")
        changed["extension/extractor.js (build)"] = str(nxt)

    # README 的「当前版本」一行（没有该行就跳过，不让它阻塞版本递增）
    text = README.read_text(encoding="utf-8")
    new_text, count = re.subn(
        r"(> \*\*当前版本\*\*：)[0-9]+\.[0-9]+\.[0-9]+", rf"\g<1>{version}", text, count=1
    )
    if count:
        README.write_text(new_text, encoding="utf-8")
        changed["README.md"] = version

    return changed


def main() -> int:
    args = sys.argv[1:]
    current = read_version()

    if "--set" in args:
        idx = args.index("--set")
        if idx + 1 >= len(args):
            print("用法： --set 0.4.0", file=sys.stderr)
            return 2
        target = args[idx + 1].strip()
        if not re.fullmatch(r"\d+\.\d+\.\d+", target):
            print(f"版本格式应为 x.y.z，收到：{target}", file=sys.stderr)
            return 2
    else:
        part = "patch"
        if "--major" in args:
            part = "major"
        elif "--minor" in args:
            part = "minor"
        target = bump(current, part)

    # --set 视为显式同步：不递增 build（避免仅仅为了对齐版本就消耗一个 build 号）
    changed = write_all(target, bump_build="--set" not in args)
    print(f"版本：{current} → {target}")
    for path, value in changed.items():
        print(f"  ✅ {path}: {value}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
