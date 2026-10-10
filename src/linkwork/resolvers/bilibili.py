"""B 站解析器：优先使用平台字幕（含 AI 字幕），否则下载音轨做语音识别。"""

from __future__ import annotations

import logging
import re
from urllib.parse import urlparse


from ..errors import ASRError, MediaError, UnsupportedContentError
from ..models import ExtractResult, Segment

from ..transcribe import download_and_transcribe
from .base import BaseResolver, ResolveContext
from .web import clean_text

log = logging.getLogger(__name__)

_BV_RE = re.compile(r"(BV[0-9A-Za-z]{8,12})")
_AV_RE = re.compile(r"av(\d+)", re.I)

_SUBTITLE_API = "https://api.bilibili.com/x/player/wbi/v2"
_VIEW_API = "https://api.bilibili.com/x/web-interface/view"


class BilibiliResolver(BaseResolver):
    name = "B站"
    priority = 10
    domains = ("bilibili.com", "b23.tv")

    def resolve(self, url: str, ctx: ResolveContext) -> ExtractResult:
        final_url = self._expand_short_link(url, ctx)
        bvid = self._bvid(final_url)

        if not bvid:
            raise UnsupportedContentError("无法从链接中识别 B 站视频号")

        title, meta, cid, aid = self._fetch_view(bvid, ctx)

        # 1) 直接调用 B 站字幕接口（含 AI 生成字幕）
        segments = self._fetch_subtitles(bvid, cid, aid, ctx)
        if segments:
            log.info("使用 B 站平台字幕：%d 条", len(segments))
            return ExtractResult(
                url=final_url,
                kind="video",
                method="subtitle",
                title=title,
                author=meta.get("author", ""),
                platform="B站",
                text="",
                segments=segments,
                duration=float(meta.get("duration") or 0) or None,
                engine="B站字幕",
                extra={"bvid": bvid, "stats": meta.get("stats", {})},
            )

        # 2) 交给 yt-dlp：先看它能否拿到字幕，否则下载音轨做 ASR
        try:
            outcome = download_and_transcribe(final_url, ctx, title_hint=title or bvid)
        except (MediaError, ASRError) as exc:
            from ..transcribe import metadata_fallback  # noqa: PLC0415

            log.warning("B 站媒体处理失败，降级为页面文案：%s", exc)
            result = metadata_fallback(
                final_url,
                ctx,
                platform="B站",
                info={"title": title, "description": meta.get("description", ""), "duration": meta.get("duration")},
                reason=f"{exc}",
            )
            result.author = meta.get("author", "")
            result.extra.setdefault("bvid", bvid)
            if len(result.text.strip()) < 5:
                raise UnsupportedContentError(f"B 站视频解析失败：{exc}") from exc
            return result

        info = outcome.get("info") or {}
        if not title:
            title = str(info.get("title") or "")
        if not meta.get("author"):
            meta["author"] = str(info.get("uploader") or info.get("channel") or "")

        return ExtractResult(
            url=final_url,
            kind="video",
            method=outcome["method"],
            title=title or bvid,
            author=meta.get("author", ""),
            platform="B站",
            segments=outcome["segments"],
            duration=outcome.get("duration"),
            engine=outcome.get("engine", ""),
            artifacts=outcome.get("artifacts", {}),
            extra={"bvid": bvid, "description": info.get("description", "")},
        )

    # ------------------------------------------------------------------ #
    def _expand_short_link(self, url: str, ctx: ResolveContext) -> str:
        if "b23.tv" not in url:
            # 去掉常见跟踪参数
            return url.split("?")[0] if "/video/" in url else url
        try:
            resp = self.request(url, ctx, allow_redirects=True)
            return resp.url
        except Exception as exc:  # noqa: BLE001
            log.debug("短链展开失败：%s", exc)
            return url

    def _bvid(self, url: str) -> str:
        match = _BV_RE.search(url)
        if match:
            return match.group(1)
        return ""

    def _aid(self, url: str) -> str:
        match = _AV_RE.search(url)
        return match.group(1) if match else ""

    def _fetch_view(self, bvid: str, ctx: ResolveContext) -> tuple[str, dict, int, int]:
        params = {"bvid": bvid} if bvid else {}
        try:
            resp = self.request(_VIEW_API, ctx, params=params)
            data = resp.json()
        except Exception as exc:  # noqa: BLE001
            log.debug("B 站 view 接口失败：%s", exc)
            return "", {}, 0, 0
        if data.get("code") != 0:
            log.debug("B 站 view 接口返回错误：%s", data.get("message"))
            return "", {}, 0, 0
        payload = data.get("data") or {}
        cid = int(payload.get("cid") or 0)
        aid = int(payload.get("aid") or 0)
        if not cid:
            pages = payload.get("pages") or []
            if pages:
                cid = int(pages[0].get("cid") or 0)
        meta = {
            "author": (payload.get("owner") or {}).get("name", ""),
            "duration": payload.get("duration") or 0,
            "stats": payload.get("stat") or {},
        }
        return str(payload.get("title") or ""), meta, cid, aid

    def _fetch_subtitles(self, bvid: str, cid: int, aid: int, ctx: ResolveContext) -> list[Segment] | None:
        if not cid:
            return None
        from .wbi import WbiSigner, ensure_buvid  # noqa: PLC0415

        session = ctx.session
        ensure_buvid(session)
        signer = WbiSigner()
        try:
            params = signer.sign({"bvid": bvid, "cid": cid, "aid": aid or ""}, session)
            resp = self.request(
                _SUBTITLE_API,
                ctx,
                params=params,
                headers={"Referer": f"https://www.bilibili.com/video/{bvid}"},
            )
            data = resp.json()
        except Exception as exc:  # noqa: BLE001
            log.debug("B 站字幕接口失败：%s", exc)
            return None
        if data.get("code") != 0:
            log.debug("B 站字幕接口返回：%s", data.get("message"))
            return None
        subtitle = (data.get("data") or {}).get("subtitle") or {}
        items = subtitle.get("subtitles") or []
        if not items:
            return None
        # 优先中文
        items.sort(key=lambda i: 0 if str(i.get("lan", "")).startswith("zh") else 1)
        for item in items:
            sub_url = item.get("subtitle_url") or ""
            if not sub_url:
                continue
            if sub_url.startswith("//"):
                sub_url = "https:" + sub_url
            if sub_url.startswith("/"):
                sub_url = "https://" + urlparse(_SUBTITLE_API).netloc + sub_url
            segments = self._parse_bcc(sub_url, ctx)
            if segments:
                return segments
        return None

    def _parse_bcc(self, sub_url: str, ctx: ResolveContext) -> list[Segment] | None:
        try:
            resp = self.request(sub_url, ctx, headers={"Referer": "https://www.bilibili.com/"})
            data = resp.json()
        except Exception as exc:  # noqa: BLE001
            log.debug("B 站字幕内容下载失败：%s", exc)
            return None
        body = (data.get("body") or []) if isinstance(data, dict) else []
        segments: list[Segment] = []
        for row in body:
            text = clean_text(str(row.get("content") or ""))
            if not text:
                continue
            segments.append(
                Segment(start=float(row.get("from") or 0), end=float(row.get("to") or 0), text=text)
            )
        return segments or None
