"""YouTube 解析器：优先官方/自动字幕，其次语音识别。"""

from __future__ import annotations

import logging

from ..models import ExtractResult
from ..transcribe import download_and_transcribe
from .base import BaseResolver, ResolveContext

log = logging.getLogger(__name__)


class YouTubeResolver(BaseResolver):
    name = "YouTube"
    priority = 12
    domains = ("youtube.com", "youtu.be", "m.youtube.com")

    def resolve(self, url: str, ctx: ResolveContext) -> ExtractResult:
        outcome = download_and_transcribe(url, ctx)
        info = outcome.get("info") or {}
        description = str(info.get("description") or "").strip()
        return ExtractResult(
            url=str(info.get("webpage_url") or url),
            kind="video",
            method=outcome["method"],
            title=str(info.get("title") or ""),
            author=str(info.get("uploader") or info.get("channel") or ""),
            platform="YouTube",
            segments=outcome["segments"],
            duration=outcome.get("duration"),
            engine=outcome.get("engine", ""),
            artifacts=outcome.get("artifacts", {}),
            extra={"description": description} if description else {},
        )
