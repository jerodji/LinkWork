"""小红书解析器：区分视频笔记（语音识别）与图文笔记（正文 + 图片 OCR）。"""

from __future__ import annotations

import json
import logging
import re
from pathlib import Path

from ..errors import UnsupportedContentError
from ..models import ExtractResult
from ..ocr import ocr_available, recognize_images, unavailable_reason
from .base import BaseResolver, ResolveContext
from .web import clean_text, extract_meta, tags_from_html

log = logging.getLogger(__name__)

_IMAGE_EXTS = (".jpg", ".jpeg", ".png", ".webp")


class XiaohongshuResolver(BaseResolver):
    name = "小红书"
    priority = 10
    domains = ("xiaohongshu.com", "xhslink.com")

    #: 失效/错误页面的特征
    _ERROR_PAGE_MARKERS = ("你访问的页面不见了", "页面不存在", "笔记不存在", "当前笔记暂时无法浏览", "扫码查看")

    def resolve(self, url: str, ctx: ResolveContext) -> ExtractResult:
        final_url, html = self._fetch_note(url, ctx)
        state = self._extract_state(html) if html else {}
        if self._is_error_page(html, final_url, state):
            detail = self._error_page_detail(html, final_url, state)
            raise UnsupportedContentError(
                f"该小红书笔记无法访问：{detail}",
                hint="可能是链接过期/笔记被删除，或需要登录后查看；可尝试提供 cookies 后重试。",
                final=True,
            )

        if state.get("is_video"):
            return self._video_result(final_url, ctx, state)

        if html:
            result = self._image_text_result(final_url, html, state, ctx)
            if result is not None:
                return result

        # 页面抓取失败或结构未知：交给 yt-dlp 尝试
        return self._video_result(final_url, ctx, state)

    # ------------------------------------------------------------------ #
    def _fetch_note(self, url: str, ctx: ResolveContext) -> tuple[str, str]:
        try:
            final_url, html = self.fetch_html(url, ctx)
            return final_url, html
        except Exception as exc:  # noqa: BLE001
            log.debug("小红书页面抓取失败：%s", exc)
            return url, ""

    def _error_page_detail(self, html: str, url: str, state: dict) -> str:
        """给出错误页的具体原因，便于用户判断。"""
        title = str(state.get("title") or "")
        if "/404" in url:
            return "笔记页面不存在（404）"
        for marker in self._ERROR_PAGE_MARKERS:
            if marker in title or marker in html[:40000]:
                return marker
        return "页面为登录/错误提示页"

    def _is_error_page(self, html: str, url: str, state: dict) -> bool:
        """识别小红书的 404/失效页，避免把错误页当成笔记内容返回。"""
        if "/404" in url:
            return True
        title = str(state.get("title") or "")
        if any(marker in title for marker in self._ERROR_PAGE_MARKERS):
            return True
        # 无正文、无图片时，检查页面是否整体就是错误页
        if not state.get("description") and not state.get("images"):
            head = html[:40000]
            if any(marker in head for marker in self._ERROR_PAGE_MARKERS):
                return True
            # 小红书 404 页标题为通用站名且带大量页脚信息
            if "小红书" in title and "RED" in head and "关于我们" in head:
                return True
        return False

    def _extract_state(self, html: str) -> dict:
        """提取 __INITIAL_STATE__ 中的笔记信息。"""
        out: dict[str, object] = {}
        match = re.search(r"window\.__INITIAL_STATE__\s*=\s*(\{.*?\})\s*</script>", html, re.S)
        raw = match.group(1) if match else ""
        if raw:
            raw = raw.replace("undefined", "null")
            try:
                data = json.loads(raw)
                out.update(self._walk_state(data))
            except Exception as exc:  # noqa: BLE001
                log.debug("__INITIAL_STATE__ 解析失败：%s", exc)
        if not out.get("images"):
            out["images"] = self._images_from_html(html)
        if not out.get("description"):
            meta = extract_meta(html)
            out.setdefault("title", meta.get("og:title") or meta.get("title", ""))
            out.setdefault("description", meta.get("og:description") or meta.get("description", ""))
        if not out.get("tags"):
            out["tags"] = tags_from_html(html)
        return out

    def _walk_state(self, data) -> dict:
        result: dict[str, object] = {}
        stack = [data]
        while stack:
            node = stack.pop()
            if isinstance(node, dict):
                if node.get("noteDetailMap"):
                    stack.append(node["noteDetailMap"])
                    continue
                # 笔记详情节点特征
                if {"desc", "type"}.issubset(node.keys()) or {"title", "imageList"}.issubset(node.keys()):
                    desc = str(node.get("desc") or "").strip()
                    title = str(node.get("title") or "").strip()
                    note_type = str(node.get("type") or "")
                    if desc or title or node.get("imageList"):
                        result.setdefault("description", desc)
                        result.setdefault("title", title)
                        result.setdefault("is_video", note_type.lower() == "video" or bool(node.get("video")))
                        result.setdefault("author", str((node.get("user") or {}).get("nickname") or ""))
                        images = node.get("imageList") or []
                        urls: list[str] = []
                        for item in images:
                            if not isinstance(item, dict):
                                continue
                            candidates = [item.get("urlDefault"), item.get("urlPre"), item.get("url")]
                            info_list = item.get("infoList") or []
                            for info in info_list:
                                if isinstance(info, dict) and info.get("url"):
                                    candidates.append(info["url"])
                            for candidate in candidates:
                                if isinstance(candidate, str) and candidate.startswith("http"):
                                    urls.append(candidate)
                                    break
                        if urls:
                            result.setdefault("images", urls)
                    video = node.get("video") or {}
                    if isinstance(video, dict):
                        for key in ("url", "masterUrl"):
                            value = video.get(key)
                            if isinstance(value, str) and value.startswith("http"):
                                result.setdefault("video_url", value)
                for value in node.values():
                    if isinstance(value, (dict, list)):
                        stack.append(value)
            elif isinstance(node, list):
                stack.extend(node)
        return result

    def _images_from_html(self, html: str) -> list[str]:
        urls: list[str] = []
        for match in re.finditer(
            r'<meta[^>]+property=["\']og:image["\'][^>]+content=["\'](https?://[^"\']+)', html, re.I
        ):
            urls.append(match.group(1))
        segment = ""
        match = re.search(r'(<div[^>]+id=["\']noteContainer["\'].*?)(?:<footer|</body>)', html, re.S | re.I)
        if match:
            segment = match.group(1)
        if segment:
            for match in re.finditer(r'<img[^>]+src=["\'](https?://[^"\']+?)(?:![^"\']*)?["\']', segment, re.I):
                url = match.group(1)
                if any(ext in url.lower() for ext in _IMAGE_EXTS) or "sns-webpic" in url or "xhscdn" in url:
                    urls.append(url)
        # 去重且保持顺序
        seen: set[str] = set()
        out: list[str] = []
        for url in urls:
            key = url.split("?")[0]
            if key in seen:
                continue
            seen.add(key)
            out.append(url)
        return out

    # ------------------------------------------------------------------ #
    def _image_text_result(
        self, url: str, html: str, state: dict, ctx: ResolveContext
    ) -> ExtractResult | None:
        title = str(state.get("title") or "").strip()
        description = str(state.get("description") or "").strip()
        from .web import is_valid_tag  # noqa: PLC0415

        tags = [t for t in (state.get("tags") or []) if is_valid_tag(str(t))]
        if tags:
            state["tags"] = tags
        images: list[str] = [u for u in (state.get("images") or []) if isinstance(u, str)]

        warnings: list[str] = []
        ocr_text = ""
        if images:
            if ocr_available():
                ocr_text, failed = self._ocr_images(images, ctx)
                if failed:
                    warnings.append(f"有 {len(failed)} 张图片 OCR 失败。")
            else:
                warnings.append(f"检测到图文笔记含 {len(images)} 张图片，但未安装 OCR 组件（{unavailable_reason()}），已跳过图片文字提取。")

        sections: list[str] = []
        if title:
            sections.append(f"# {title}")
        if description:
            sections.append(description)
        if ocr_text.strip():
            sections.append("【图片文字】\n" + ocr_text.strip())
        if tags:
            sections.append("【话题】" + " ".join(f"#{t}#" for t in tags[:30]))

        text = clean_text("\n\n".join(sections))
        if len(text) < 10:
            return None

        kind = "image_text" if images else "article"
        method = "html"
        return ExtractResult(
            url=url,
            kind=kind,
            method=method,
            title=title,
            author=str(state.get("author") or ""),
            platform="小红书",
            text=text,
            engine="页面正文 + 图片OCR" if ocr_text else "页面正文",
            warnings=warnings,
            extra={"images": images[:20], "tags": tags} if tags or images else {},
        )

    def _ocr_images(self, urls: list[str], ctx: ResolveContext) -> tuple[str, list[str]]:
        download_dir = Path(ctx.workdir) / "xhs_images"
        download_dir.mkdir(parents=True, exist_ok=True)
        local: list[Path] = []
        for index, url in enumerate(urls[:20]):
            try:
                ctx.report(f"下载图片 {index + 1}/{min(len(urls), 20)}", index / max(min(len(urls), 20), 1))
                resp = self.request(url, ctx, headers={"Referer": "https://www.xiaohongshu.com/"})
                suffix = ".jpg"
                for ext in _IMAGE_EXTS:
                    if ext in url.lower():
                        suffix = ext
                        break
                path = download_dir / f"img_{index:02d}{suffix}"
                path.write_bytes(resp.content)
                local.append(path)
            except Exception as exc:  # noqa: BLE001
                log.debug("图片下载失败 %s：%s", url, exc)
        if not local:
            return "", [u for u in urls]
        ctx.report("识别图片文字", 0.5)
        return recognize_images(local)

    # ------------------------------------------------------------------ #
    def _video_result(self, url: str, ctx: ResolveContext, state: dict) -> ExtractResult:
        from ..errors import ASRError  # noqa: PLC0415
        from ..transcribe import download_and_transcribe  # noqa: PLC0415

        title = str(state.get("title") or "").strip()
        try:
            outcome = download_and_transcribe(url, ctx, title_hint=title)
        except (Exception, ASRError) as exc:  # noqa: BLE001
            # 视频下载失败：若还有图文信息则降级
            description = str(state.get("description") or "").strip()
            if description:
                return ExtractResult(
                    url=url,
                    kind="video",
                    method="metadata",
                    title=title,
                    author=str(state.get("author") or ""),
                    platform="小红书",
                    text=clean_text("\n".join(p for p in (title, description) if p)),
                    engine="页面文案",
                    warnings=[f"未能下载视频音轨（{exc}），以下为笔记文字内容。"],
                )
            raise UnsupportedContentError(f"小红书笔记解析失败：{exc}") from exc

        info = outcome.get("info") or {}
        description = str(info.get("description") or state.get("description") or "").strip()
        return ExtractResult(
            url=url,
            kind="video",
            method=outcome["method"],
            title=title or str(info.get("title") or ""),
            author=str(state.get("author") or info.get("uploader") or ""),
            platform="小红书",
            segments=outcome["segments"],
            duration=outcome.get("duration"),
            engine=outcome.get("engine", ""),
            artifacts=outcome.get("artifacts", {}),
            extra={"description": description} if description else {},
        )
