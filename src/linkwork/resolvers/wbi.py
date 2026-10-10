"""B 站 WBI 签名工具。

B 站部分接口（如播放器信息、字幕列表）要求 wbi 签名：
    mixin_key = 打乱(nav.wbi_img.img_url + sub_url)
    参数按 key 排序 + wts 时间戳 → 拼接 query → md5(query + mixin_key) = w_rid
"""

from __future__ import annotations

import hashlib
import logging
import time
import urllib.parse

log = logging.getLogger(__name__)

#: 官方前端使用的重排表
MIXIN_KEY_ENC_TAB = [
    46, 47, 18, 2, 53, 8, 23, 32, 15, 50, 10, 31, 58, 3, 45, 35, 27, 43, 5, 49,
    33, 9, 42, 19, 29, 28, 14, 39, 12, 38, 41, 13, 37, 48, 7, 16, 24, 55, 40, 61,
    26, 17, 0, 1, 60, 51, 30, 4, 22, 25, 54, 21, 56, 59, 6, 63, 57, 62, 11, 36,
    20, 34, 44, 52,
]

_NAV_API = "https://api.bilibili.com/x/web-interface/nav"
_SPI_API = "https://api.bilibili.com/x/frontend/finger/spi"


def _filename_part(url: str) -> str:
    return url.rsplit("/", 1)[-1].split(".")[0] if url else ""


def get_mixin_key(raw: str) -> str:
    """按重排表生成 mixin key（取前 32 位）。"""
    return "".join(raw[i] for i in MIXIN_KEY_ENC_TAB if i < len(raw))[:32]


class WbiSigner:
    """缓存 wbi 密钥并给请求参数签名。"""

    KEY_TTL = 3600

    def __init__(self) -> None:
        self._mixin_key: str = ""
        self._fetched_at: float = 0.0

    def _fetch(self, session) -> str:
        resp = session.get(_NAV_API, timeout=10)
        data = resp.json()
        wbi = (data.get("data") or {}).get("wbi_img") or {}
        raw = _filename_part(wbi.get("img_url", "")) + _filename_part(wbi.get("sub_url", ""))
        if len(raw) < 64:
            raise RuntimeError("未获取到 B 站 wbi 密钥")
        return get_mixin_key(raw)

    def mixin_key(self, session) -> str:
        if self._mixin_key and (time.time() - self._fetched_at) < self.KEY_TTL:
            return self._mixin_key
        self._mixin_key = self._fetch(session)
        self._fetched_at = time.time()
        return self._mixin_key

    def sign(self, params: dict, session) -> dict:
        """在参数中加入 wts 与 w_rid。"""
        params = {k: v for k, v in params.items() if v not in (None, "")}
        params["wts"] = int(time.time())
        params = dict(sorted(params.items()))
        cleaned = {k: "".join(ch for ch in str(v) if ch not in "!'()*") for k, v in params.items()}
        query = urllib.parse.urlencode(cleaned)
        params["w_rid"] = hashlib.md5((query + self.mixin_key(session)).encode()).hexdigest()
        return params


def ensure_buvid(session) -> None:
    """确保会话里有 buvid3/buvid4（部分接口需要）。"""
    try:
        if session.cookies.get("buvid3"):
            return
        resp = session.get(_SPI_API, timeout=10)
        data = (resp.json().get("data") or {})
        if data.get("b_3"):
            session.cookies.set("buvid3", data["b_3"], domain=".bilibili.com")
        if data.get("b_4"):
            session.cookies.set("buvid4", data["b_4"], domain=".bilibili.com")
    except Exception as exc:  # noqa: BLE001
        log.debug("获取 buvid 失败：%s", exc)
