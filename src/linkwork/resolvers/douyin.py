"""抖音 / 快手等短视频平台解析器。

yt-dlp 对抖音分享链接的支持不稳定，这里做两级兜底：
1. 走通用视频流程（字幕 → 语音识别）；
2. 失败时抓分享页/接口，返回标题 + 口播文案 + 话题标签。
"""

from __future__ import annotations

import json
import logging
import re
from urllib.parse import urlparse

from ..errors import UnsupportedContentError
from ..models import ExtractResult
from .base import ResolveContext
from .video import VideoResolver
from .web import tags_from_html

log = logging.getLogger(__name__)


class DouyinResolver(VideoResolver):
    name = "抖音/快手"
    priority = 8
    domains = (
        "douyin.com",
        "iesdouyin.com",
        "douyinpic.com",
        "kuaishou.com",
        "chenzhongtech.com",
        "huoshan.com",
    )

    def resolve(self, url: str, ctx: ResolveContext) -> ExtractResult:
        try:
            return super().resolve(url, ctx)
        except UnsupportedContentError as exc:
            fallback = self._share_page_fallback(url, ctx)
            if fallback is not None:
                return fallback
            raise exc

    # ------------------------------------------------------------------ #
    def _share_page_fallback(self, url: str, ctx: ResolveContext) -> ExtractResult | None:
        """抓取分享页 HTML 中的标题/文案/话题。"""
        final_url = url
        try:
            resp = self.request(url, ctx, mobile=True, allow_redirects=True)
            final_url = resp.url
            html = resp.text
        except Exception as exc:  # noqa: BLE001
            log.debug("分享页抓取失败：%s", exc)
            return None

        data = self._extract_share_json(html)
        title = str(data.get("title") or "").strip()
        description = str(data.get("description") or "").strip()
        author = str(data.get("author") or "").strip()
        tags = data.get("tags") or []

        text_parts = [p for p in (title, description) if p]
        text = "\n".join(text_parts).strip()
        if tags:
            text += ("\n\n【话题】" + " ".join(f"#{t}#" for t in tags[:20])).strip()

        if len(text) < 5:
            return None

        # 平台判断
        host = urlparse(final_url).hostname or ""
        platform = "抖音" if "douyin" in host else ("快手" if "kuaishou" in host or "chenzhongtech" in host else "")
        return ExtractResult(
            url=final_url,
            kind="video",
            method="metadata",
            title=title,
            author=author,
            platform=platform or "短视频",
            text=text,
            engine="分享页文案",
            warnings=["未能下载视频音轨做语音识别，已提取分享页中的文案信息（可能不含完整口播内容）。"],
            extra={"tags": tags} if tags else {},
        )

    def _extract_share_json(self, html: str) -> dict:
        """从分享页里尽量挖出文案信息。

        抖音的分享页改版多次，且不同渲染路径结构不同，这里按可靠性依次尝试：
        1. ``window._ROUTER_DATA``（新版 SSR 数据，含 videoInfo）；
        2. ``RENDER_DATA``（旧版，URL 编码的 JSON）；
        3. meta / og 标签（兜底）。
        并过滤掉「来抖音，记录美好生活」这类站点通用文案。
        """
        out: dict[str, object] = {}

        payload = self._parse_router_data(html)
        if payload:
            out.update(self._walk_douyin(payload))

        if not out.get("description"):
            match = re.search(r'id="RENDER_DATA"[^>]*>(.*?)</script>', html, re.S)
            if match:
                from urllib.parse import unquote  # noqa: PLC0415

                try:
                    legacy = json.loads(unquote(match.group(1)))
                    out.update({k: v for k, v in self._walk_douyin(legacy).items() if v})
                except Exception as exc:  # noqa: BLE001
                    log.debug("RENDER_DATA 解析失败：%s", exc)

        from .web import extract_meta  # noqa: PLC0415

        meta = extract_meta(html)
        if not out.get("title"):
            candidate = meta.get("og:title") or meta.get("title", "")
            if candidate and not _is_boilerplate(candidate):
                out["title"] = candidate
        if not out.get("description"):
            candidate = meta.get("og:description") or meta.get("description", "")
            if candidate and not _is_boilerplate(candidate):
                out["description"] = candidate
        if not out.get("tags"):
            out["tags"] = tags_from_html(html)

        # 清洗：去掉站点通用文案
        for key in ("title", "description"):
            value = str(out.get(key) or "").strip()
            if not value or _is_boilerplate(value):
                out.pop(key, None)
        return out

    def _parse_router_data(self, html: str) -> dict | None:
        """解析 ``window._ROUTER_DATA``（按大括号配平截取，避免正则贪婪问题）。"""
        marker = "_ROUTER_DATA"
        idx = html.find(marker)
        if idx < 0:
            return None
        start = html.find("{", idx)
        if start < 0:
            return None
        depth = 0
        in_string = False
        escaped = False
        for pos in range(start, min(len(html), start + 4_000_000)):
            char = html[pos]
            if in_string:
                if escaped:
                    escaped = False
                elif char == "\\":
                    escaped = True
                elif char == '"':
                    in_string = False
                continue
            if char == '"':
                in_string = True
            elif char == "{":
                depth += 1
            elif char == "}":
                depth -= 1
                if depth == 0:
                    try:
                        return json.loads(html[start : pos + 1])
                    except Exception as exc:  # noqa: BLE001
                        log.debug("_ROUTER_DATA 解析失败：%s", exc)
                        return None
        return None

    def _walk_douyin(self, payload) -> dict:
        """从抖音 SSR 数据（_ROUTER_DATA / RENDER_DATA）中挖出标题/文案/作者/话题。"""
        result: dict[str, object] = {}
        stack = [payload]
        while stack:
            node = stack.pop()
            if isinstance(node, dict):
                for key, value in node.items():
                    if key in ("desc", "shareTitle", "title") and isinstance(value, str) and not result.get("description"):
                        cleaned = value.strip()
                        # 作者昵称等字段也常叫 title，需过滤站点通用文案
                        if cleaned and not _is_boilerplate(cleaned):
                            result["description"] = cleaned
                    elif key in ("nickname", "authorName") and isinstance(value, str) and not result.get("author"):
                        result["author"] = value.strip()
                    elif key in ("text_extra", "cha_list") and isinstance(value, list) and not result.get("tags"):
                        tags = []
                        for item in value:
                            if isinstance(item, dict):
                                tag = item.get("hashtag_name") or item.get("cha_name")
                                if tag:
                                    tags.append(str(tag))
                        if tags:
                            result["tags"] = tags
                    else:
                        stack.append(value)
            elif isinstance(node, list):
                stack.extend(node)
        if not result.get("title") and result.get("description"):
            result["title"] = str(result["description"])[:60]
        return result


#: 抖音分享页的站点通用文案（不含具体视频信息，不应作为文案返回）
_BOILERPLATE_PATTERNS = (
    "来抖音，记录美好生活",
    "来抖音,记录美好生活",
    "抖音短视频-记录美好生活",
    "记录美好生活",
    "抖音官网",
    "抖音短视频",
    "在抖音记录美好生活",
    "发现更多精彩视频",
)


def _is_boilerplate(text: str) -> bool:
    """判断是否为平台通用文案（非用户内容）。"""
    value = (text or "").strip()
    if not value or len(value) < 4:
        return True
    return any(pattern in value for pattern in _BOILERPLATE_PATTERNS)
