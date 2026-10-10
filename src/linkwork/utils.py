"""通用工具：日志、文件名、UA、URL 判定。"""

from __future__ import annotations

import logging
import os
import re
import sys
import unicodedata
from pathlib import Path
from urllib.parse import urlparse

LOG_FORMAT = "%(asctime)s [%(levelname)s] %(name)s: %(message)s"

_DEFAULT_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
)

MOBILE_UA = (
    "Mozilla/5.0 (iPhone; CPU iPhone OS 17_5 like Mac OS X) AppleWebKit/605.1.15 "
    "(KHTML, like Gecko) Version/17.5 Mobile/15E148 Safari/604.1"
)


def setup_logging(level: int | str = logging.INFO, quiet: bool = False) -> None:
    """初始化全局日志。"""
    if quiet:
        level = logging.ERROR
    root = logging.getLogger()
    if not root.handlers:
        handler = logging.StreamHandler(sys.stderr)
        handler.setFormatter(logging.Formatter(LOG_FORMAT))
        root.addHandler(handler)
    root.setLevel(level)
    # 第三方库日志降噪
    for noisy in ("urllib3", "requests", "yt_dlp", "filelock", "httpx"):
        logging.getLogger(noisy).setLevel(logging.WARNING)


def default_ua() -> str:
    return os.environ.get("LINKWORK_USER_AGENT", _DEFAULT_UA)


def sanitize_filename(name: str, fallback: str = "linkwork", max_length: int = 60) -> str:
    """把标题转成安全的文件名。"""
    if not name:
        return fallback
    name = unicodedata.normalize("NFKC", name)
    name = re.sub(r"[\x00-\x1f\x7f]", "", name)
    name = re.sub(r"[\\/:*?\"<>|#%&{}$!'@+`=~\[\]^;,\s]+", "_", name)
    name = re.sub(r"_+", "_", name).strip("_.")
    if len(name) > max_length:
        name = name[:max_length].rstrip("_.")
    return name or fallback


def unique_path(directory: Path, stem: str, suffix: str) -> Path:
    """返回不冲突的文件路径。"""
    directory.mkdir(parents=True, exist_ok=True)
    candidate = directory / f"{stem}{suffix}"
    index = 2
    while candidate.exists():
        candidate = directory / f"{stem}_{index}{suffix}"
        index += 1
    return candidate


def normalize_url(raw: str) -> str:
    """从分享文本中抠出 URL，并做基本清洗。"""
    raw = (raw or "").strip()
    if not raw:
        return ""
    match = re.search(r"https?://[^\s，。、）)】\]\"'<>]+", raw)
    url = match.group(0) if match else raw
    url = url.rstrip("，。、；,.;)")
    return url


def host_of(url: str) -> str:
    try:
        host = urlparse(url).hostname or ""
    except ValueError:
        return ""
    return host.lower().lstrip(".")


#: 各平台域名特征 -> 平台标识
PLATFORM_RULES: list[tuple[str, tuple[str, ...]]] = [
    ("抖音", ("douyin.com", "iesdouyin.com")),
    ("快手", ("kuaishou.com", "chenzhongtech.com")),
    ("小红书", ("xiaohongshu.com", "xhslink.com")),
    ("B站", ("bilibili.com", "b23.tv")),
    ("微信", ("mp.weixin.qq.com", "weixin.qq.com")),
    ("微博", ("weibo.com", "weibo.cn")),
    ("知乎", ("zhihu.com",)),
    ("今日头条", ("toutiao.com", "ixigua.com")),
    ("视频号", ("channels.weixin.qq.com", "finder.video.qq.com")),
    ("YouTube", ("youtube.com", "youtu.be")),
    ("TikTok", ("tiktok.com",)),
    ("Instagram", ("instagram.com",)),
    ("X", ("twitter.com", "x.com")),
    ("Vimeo", ("vimeo.com",)),
    ("腾讯视频", ("v.qq.com",)),
    ("优酷", ("youku.com",)),
    ("爱奇艺", ("iqiyi.com",)),
    ("西瓜视频", ("ixigua.com",)),
    ("豆瓣", ("douban.com",)),
    ("简书", ("jianshu.com",)),
    ("CSDN", ("csdn.net",)),
    ("百家号", ("baijiahao.baidu.com",)),
]


def detect_platform(url: str) -> str:
    host = host_of(url)
    if not host:
        return ""
    for name, domains in PLATFORM_RULES:
        if any(host == d or host.endswith("." + d) for d in domains):
            return name
    return host


