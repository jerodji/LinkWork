"""通用网页解析器：图文文章、公众号文章、纯文案网页。

对以视频为主的页面主动放弃（交给 video 解析器做口播稿提取）。
"""

from __future__ import annotations

import json
import logging
import re
from urllib.parse import urljoin


from ..errors import UnsupportedContentError
from ..models import ExtractResult
from ..utils import host_of
from .base import BaseResolver, ResolveContext

log = logging.getLogger(__name__)

#: 明显以视频为唯一主体的页面（交给视频解析器）
_ARTICLE_HINT_PATTERNS = (
    re.compile(r'"@type"\s*:\s*"(?:NewsArticle|Article|BlogPosting|SocialMediaPosting)"', re.I),
    re.compile(r'property=["\']og:type["\']\s+content=["\']article', re.I),
    re.compile(r"<article[\s>]", re.I),
)

_NOISE_TAGS = ("script", "style", "noscript", "iframe", "svg", "form", "nav", "footer", "header", "aside", "button")

_WHITESPACE_RE = re.compile(r"[ \t\u00a0\u3000]+")


class WebArticleResolver(BaseResolver):
    """抓取网页正文；支持公众号、知乎专栏、头条等图文页面。"""

    name = "网页图文"
    priority = 90
    domains: tuple[str, ...] = ()  # 匹配所有 http(s) 链接，作为兜底

    def resolve(self, url: str, ctx: ResolveContext) -> ExtractResult:
        ctx.report("抓取页面", 0.0)
        final_url, html = self.fetch_html(url, ctx)
        html = html or ""

        meta = extract_meta(html, final_url)

        if self._is_video_dominant(html, meta):
            if meta.get("video_url"):
                # 保留视频直链，供上层媒体流程使用
                raise UnsupportedContentError("该页面主体是视频，转交视频解析器处理")
            raise UnsupportedContentError("该页面主体是视频且未找到可下载地址，无法提取文字")

        body = self._extract_body(html, final_url)
        if not body.strip():
            body = self._extract_visible_text(html)
        body = clean_text(body)

        # 附加元信息（话题标签、动态文案）
        extra: dict[str, object] = {}
        if meta.get("description"):
            extra["description"] = meta["description"]
        tags = tags_from_html(html)
        if tags:
            extra["tags"] = tags[:30]

        text = body
        if meta.get("description") and meta["description"] not in body:
            text = f"{body}\n\n【简介】{meta['description']}".strip()

        if len(text.strip()) < 20:
            raise UnsupportedContentError("页面正文过短，可能是动态加载页面或需要登录")

        return ExtractResult(
            url=final_url,
            kind="article",
            method="html",
            title=meta.get("title", ""),
            author=meta.get("author", ""),
            platform=meta.get("site_name") or host_of(final_url),
            text=text,
            extra=extra,
        )

    # ------------------------------------------------------------------ #
    def _is_video_dominant(self, html: str, meta: dict) -> bool:
        head = html[:200000]
        if meta.get("video_url") or meta.get("og_type") in ("video.other", "video.movie", "video.episode"):
            # 若同时存在明显文章特征，则按文章处理
            if any(p.search(head) for p in _ARTICLE_HINT_PATTERNS):
                return False
            return True
        return False

    def _extract_body(self, html: str, base_url: str) -> str:
        # 1) 公众号等平台的结构化容器
        for selector in (
            "#js_content",  # 微信公众号
            "article",
            ".article-content",
            ".rich_media_content",
            ".Post-RichText",
            ".content",
            "#content",
            ".main-content",
            "main",
        ):
            text = self._text_of_selector(html, selector, base_url)
            if text and len(text) > 120:
                return text

        # 2) readability 兜底
        try:
            from readability import Document  # noqa: PLC0415

            doc = Document(html)
            summary = doc.summary(html_partial=True)
            text = self._text_from_html_fragment(summary, base_url)
            if text and len(text) > 120:
                return text
        except Exception as exc:  # noqa: BLE001
            log.debug("readability 解析失败：%s", exc)

        return ""

    def _text_of_selector(self, html: str, selector: str, base_url: str) -> str:
        from bs4 import BeautifulSoup  # noqa: PLC0415

        try:
            soup = BeautifulSoup(html, "lxml")
        except Exception:  # pragma: no cover
            soup = BeautifulSoup(html, "html.parser")
        node = soup.select_one(selector)
        if node is None:
            return ""
        return self._node_text(node, base_url)

    def _node_text(self, node, base_url: str) -> str:
        for tag in node.find_all(_NOISE_TAGS):
            tag.decompose()
        # 图片转换为标记，保留「有图」信息
        for img in node.find_all("img"):
            alt = (img.get("alt") or "").strip()
            img.replace_with(f"[图片{': ' + alt if alt else ''}]")
        return clean_text(node.get_text("\n"))

    def _text_from_html_fragment(self, fragment: str, base_url: str) -> str:
        from bs4 import BeautifulSoup  # noqa: PLC0415

        try:
            node = BeautifulSoup(fragment, "lxml")
        except Exception:  # pragma: no cover
            node = BeautifulSoup(fragment, "html.parser")
        return self._node_text(node, base_url)

    def _extract_visible_text(self, html: str) -> str:
        from bs4 import BeautifulSoup  # noqa: PLC0415

        try:
            soup = BeautifulSoup(html, "lxml")
        except Exception:  # pragma: no cover
            soup = BeautifulSoup(html, "html.parser")
        body = soup.body or soup
        return self._node_text(body, "")


# --------------------------------------------------------------------------- #
# 工具函数
# --------------------------------------------------------------------------- #
_META_RE = {
    "og:title": re.compile(r'<meta[^>]+property=["\']og:title["\'][^>]+content=["\']([^"\']*)', re.I),
    "og:description": re.compile(r'<meta[^>]+property=["\']og:description["\'][^>]+content=["\']([^"\']*)', re.I),
    "og:type": re.compile(r'<meta[^>]+property=["\']og:type["\'][^>]+content=["\']([^"\']*)', re.I),
    "og:video": re.compile(r'<meta[^>]+property=["\']og:video(?::url)?["\'][^>]+content=["\']([^"\']*)', re.I),
    "description": re.compile(r'<meta[^>]+name=["\']description["\'][^>]+content=["\']([^"\']*)', re.I),
    "og:site_name": re.compile(r'<meta[^>]+property=["\']og:site_name["\'][^>]+content=["\']([^"\']*)', re.I),
    "author": re.compile(r'<meta[^>]+name=["\']author["\'][^>]+content=["\']([^"\']*)', re.I),
    "title": re.compile(r"<title[^>]*>([^<]*)</title>", re.I),
}


def extract_meta(html: str, base_url: str = "") -> dict[str, str]:
    """提取标题/作者/站点/视频地址等元信息。"""
    meta: dict[str, str] = {}
    head = html[:300000]
    for key, pattern in _META_RE.items():
        match = pattern.search(head)
        if match:
            value = unescape(match.group(1))
            if value:
                meta[key] = value
    if not meta.get("description"):
        # 很多站点只提供 og/twitter 描述
        for pattern in (
            r'<meta[^>]+property=["\']og:description["\'][^>]+content=["\']([^"\']*)',
            r'<meta[^>]+property=["\']twitter:description["\'][^>]+content=["\']([^"\']*)',
            r'<meta[^>]+name=["\']twitter:description["\'][^>]+content=["\']([^"\']*)',
        ):
            alt = re.search(pattern, head, re.I)
            if alt:
                value = unescape(alt.group(1)).strip()
                if value:
                    meta["description"] = value
                    break
    if not meta.get("author"):
        alt = re.search(r'var\s+nickname\s*=\s*["\']([^"\']+)', head)
        if alt:
            meta["author"] = alt.group(1)
    if base_url and meta.get("og:video", "").startswith("/"):
        meta["video_url"] = urljoin(base_url, meta["og:video"])
    elif meta.get("og:video"):
        meta["video_url"] = meta["og:video"]

    # JSON-LD 补充
    for block in re.findall(
        r'<script[^>]+type=["\']application/ld\+json["\'][^>]*>(.*?)</script>', head, re.S | re.I
    )[:5]:
        try:
            data = json.loads(block.strip())
        except Exception:
            continue
        for item in data if isinstance(data, list) else [data]:
            if not isinstance(item, dict):
                continue
            for key, target in (("headline", "title"), ("author", "author"), ("description", "description")):
                if meta.get(target):
                    continue
                value = item.get(key)
                if isinstance(value, dict):
                    value = value.get("name")
                if isinstance(value, list) and value:
                    value = value[0].get("name") if isinstance(value[0], dict) else value[0]
                if isinstance(value, str) and value.strip():
                    meta[target] = value.strip()
            if item.get("@type") in ("VideoObject",) and item.get("contentUrl"):
                meta.setdefault("video_url", str(item["contentUrl"]))
    return meta


#: 话题标签的噪声来源：CSS 色值、样式片段
_COLOR_LIKE_RE = re.compile(r"^[0-9a-fA-F]{3,8}$")
_TAG_STOP_RE = re.compile(r"[;:{}()\[\],.!?/\\\s]")
_STYLE_BLOCK_RE = re.compile(r"<(style|script)[^>]*>.*?</\1>", re.S | re.I)


def is_valid_tag(tag: str) -> bool:
    """判断 #xxx# 是否像真正的话题标签（排除 CSS 色值与样式片段）。"""
    value = (tag or "").strip()
    if not (1 < len(value) <= 24):
        return False
    if _COLOR_LIKE_RE.match(value):  # fff / aeaeae
        return False
    if _TAG_STOP_RE.search(value):  # 含标点/样式分隔符
        return False
    if value.startswith(("rgb", "rgba", "hsl")):
        return False
    return True


def tags_from_html(html: str) -> list[str]:
    """提取话题标签（#xxx# 或标签 meta），并过滤 CSS/脚本噪声。"""
    tags: list[str] = []
    # 先剔除 <style>/<script>，否则 #fff;color:#xxx 之类会被误判成话题
    cleaned = _STYLE_BLOCK_RE.sub(" ", html[:300000])
    for match in re.finditer(r"#([^#\s<>{}]{1,24})#", cleaned):
        tag = match.group(1).strip()
        if is_valid_tag(tag) and tag not in tags:
            tags.append(tag)
    for match in re.finditer(r'<meta[^>]+property=["\']article:tag["\'][^>]+content=["\']([^"\']+)', cleaned, re.I):
        tag = unescape(match.group(1)).strip()
        if is_valid_tag(tag) and tag not in tags:
            tags.append(tag)
    return tags


def clean_text(text: str) -> str:
    """规范化空白，去掉重复空行。"""
    if not text:
        return ""
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    lines = [_WHITESPACE_RE.sub(" ", line).strip() for line in text.split("\n")]
    out: list[str] = []
    blank = 0
    for line in lines:
        if not line:
            blank += 1
            if blank > 1:
                continue
        else:
            blank = 0
        out.append(line)
    return "\n".join(out).strip()


def unescape(value: str) -> str:
    import html as html_module  # noqa: PLC0415

    return html_module.unescape(value or "")
