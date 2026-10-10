"""媒体缓存目录：统一管理下载的临时媒体与识别中间产物。

设计目标（避免占空间）：
1. **不再残留**：每个任务结束后立即清理自己的工作区；
2. **落到可回收位置**：默认放 ``~/Library/Caches/LinkWork/media``
   —— macOS 的缓存目录（比系统临时目录更语义正确，系统空间紧张时可回收）；
3. **可一键清除**：对外提供统计与清理接口，供插件调用；
4. **启动自愈**：服务启动时清理上次遗留的孤儿目录。

环境变量：
    LINKWORK_CACHE_DIR  自定义缓存根目录
"""

from __future__ import annotations

import logging
import os
import shutil
import tempfile
import time
from pathlib import Path

log = logging.getLogger(__name__)

#: 工作区目录名前缀（用于识别与清理）
WORKSPACE_PREFIX = "linkwork_"

#: 孤儿工作区判定年龄（秒）：超过该时长且非活动任务，视为可清理
ORPHAN_AGE_SECONDS = 6 * 3600


def cache_root() -> Path:
    """返回媒体缓存根目录（不存在则创建）。"""
    env = os.environ.get("LINKWORK_CACHE_DIR", "").strip()
    if env:
        root = Path(env).expanduser()
    else:
        # macOS 的 ~/Library/Caches 是系统认可的缓存位置
        root = Path.home() / "Library" / "Caches" / "LinkWork" / "media"
    root.mkdir(parents=True, exist_ok=True)
    return root


def new_workspace(label: str = "job") -> Path:
    """创建本次任务的独立工作区。"""
    root = cache_root()
    path = Path(tempfile.mkdtemp(prefix=f"{WORKSPACE_PREFIX}{label}_", dir=str(root)))
    log.debug("工作区：%s", path)
    return path


def _dir_size(path: Path) -> int:
    total = 0
    try:
        for item in path.rglob("*"):
            if item.is_file():
                try:
                    total += item.stat().st_size
                except OSError:
                    pass
    except OSError:
        pass
    return total


def list_workspaces() -> list[Path]:
    """列出所有工作区目录。"""
    root = cache_root()
    try:
        return sorted(p for p in root.iterdir() if p.is_dir() and p.name.startswith(WORKSPACE_PREFIX))
    except OSError:
        return []


def stats() -> dict:
    """缓存统计：目录、文件数、占用字节。"""
    root = cache_root()
    spaces = list_workspaces()
    files = 0
    total = 0
    for space in spaces:
        try:
            for item in space.rglob("*"):
                if item.is_file():
                    files += 1
                    total += item.stat().st_size
        except OSError:
            continue
    return {
        "root": str(root),
        "workspaces": len(spaces),
        "files": files,
        "bytes": total,
        "human": human_size(total),
    }


def human_size(num: int) -> str:
    size = float(num)
    for unit in ("B", "KB", "MB", "GB"):
        if size < 1024 or unit == "GB":
            return f"{size:.1f} {unit}" if unit != "B" else f"{int(size)} B"
        size /= 1024
    return f"{size:.1f} GB"


def clear(keep: set[Path] | None = None) -> dict:
    """清除所有工作区（keep 里的活动工作区会被跳过）。"""
    keep = {p.resolve() for p in (keep or set())}
    removed = 0
    freed = 0
    for space in list_workspaces():
        if space.resolve() in keep:
            continue
        freed += _dir_size(space)
        try:
            shutil.rmtree(space, ignore_errors=True)
            removed += 1
        except Exception as exc:  # noqa: BLE001
            log.warning("清理失败 %s：%s", space, exc)
    log.info("已清理 %d 个工作区，释放 %s", removed, human_size(freed))
    return {"removed": removed, "freed_bytes": freed, "freed_human": human_size(freed)}


def cleanup_workspace(path: Path | None) -> None:
    """删除单个工作区（任务结束后调用）。"""
    if not path:
        return
    try:
        if path.exists() and path.name.startswith(WORKSPACE_PREFIX):
            shutil.rmtree(path, ignore_errors=True)
            log.debug("已清理工作区：%s", path)
    except Exception as exc:  # noqa: BLE001
        log.debug("清理工作区失败 %s：%s", path, exc)


def purge_orphans(active: set[Path] | None = None) -> dict:
    """清理老旧的孤儿工作区（服务启动时调用，避免上次崩溃留下垃圾）。"""
    active = {p.resolve() for p in (active or set())}
    now = time.time()
    removed = 0
    freed = 0
    for space in list_workspaces():
        if space.resolve() in active:
            continue
        try:
            age = now - space.stat().st_mtime
        except OSError:
            continue
        if age < ORPHAN_AGE_SECONDS:
            continue
        freed += _dir_size(space)
        shutil.rmtree(space, ignore_errors=True)
        removed += 1
    if removed:
        log.info("启动清理：移除 %d 个遗留工作区，释放 %s", removed, human_size(freed))
    return {"removed": removed, "freed_bytes": freed, "freed_human": human_size(freed)}
