"""通用视频/音频链接解析器：抖音、快手、视频号直链、直链 mp4/m3u8 等。

策略：
1. 用 yt-dlp 解析元信息；
2. 若有平台字幕则直接用字幕；
3. 否则下载音轨并做语音识别，得到口播稿；
4. 若无法下载媒体但能拿到标题/简介/话题，则降级返回「视频文案信息」。
"""

from __future__ import annotations

import logging

from ..errors import ASRError, LinkWorkError, MediaError, UnsupportedContentError
from ..models import ExtractResult
from ..transcribe import download_and_transcribe
from ..utils import detect_platform, host_of
from .base import BaseResolver, ResolveContext

log = logging.getLogger(__name__)

_VIDEO_DOMAINS = (
    "douyin.com",
    "iesdouyin.com",
    "kuaishou.com",
    "chenzhongtech.com",
    "ixigua.com",
    "toutiao.com",
    "weibo.com",
    "weibo.cn",
    "instagram.com",
    "tiktok.com",
    "twitter.com",
    "x.com",
    "vimeo.com",
    "v.qq.com",
    "youku.com",
    "iqiyi.com",
    "sohu.com",
    "miaopai.com",
    "pearvideo.com",
    "huya.com",
    "douyu.com",
    "zhihu.com",
    "toutiaoimg.com",
    "bcy.net",
    "pipix.com",
    "6.cn",
    "meipai.com",
    "acfun.cn",
    "le.com",
    "mgtv.com",
    "pptv.com",
    "yizhibo.com",
    "inke.tv",
    "huoshan.com",
    "xiaokaxiu.com",
    "douyinpic.com",
    "kg.qq.com",
)


class VideoResolver(BaseResolver):
    """面向「视频/音频主体」链接的通用解析器。"""

    name = "通用视频"
    priority = 40
    domains = _VIDEO_DOMAINS

    def matches(self, url: str) -> bool:
        if super().matches(url):
            return True
        # 直链媒体文件
        path = url.split("?")[0].lower()
        return path.endswith((".mp4", ".mov", ".m3u8", ".flv", ".mkv", ".mp3", ".m4a", ".wav", ".aac", ".webm"))

    def resolve(self, url: str, ctx: ResolveContext) -> ExtractResult:
        platform = detect_platform(url) or host_of(url)
        try:
            outcome = download_and_transcribe(url, ctx)
        except (MediaError, ASRError, LinkWorkError) as exc:
            log.warning("媒体处理失败：%s", exc)
            return self._metadata_fallback(url, ctx, platform, exc)

        info = outcome.get("info") or {}
        description = str(info.get("description") or "").strip()
        title = str(info.get("title") or "")
        return ExtractResult(
            url=str(info.get("webpage_url") or url),
            kind="video",
            method=outcome["method"],
            title=title,
            author=str(info.get("uploader") or info.get("channel") or info.get("creator") or ""),
            platform=platform,
            segments=outcome["segments"],
            duration=outcome.get("duration"),
            engine=outcome.get("engine", ""),
            artifacts=outcome.get("artifacts", {}),
            extra={"description": description} if description else {},
        )

    # ------------------------------------------------------------------ #
    def _metadata_fallback(self, url: str, ctx: ResolveContext, platform: str, exc: Exception) -> ExtractResult:
        """下载/识别失败时，尽量给出标题/简介/话题组成的「视频文案信息」。"""
        from ..errors import FetchError  # noqa: PLC0415
        from ..transcribe import cookie_hint, metadata_fallback, needs_cookies  # noqa: PLC0415

        # 风控 / 需要登录：给出可操作提示，不继续抛底层错误
        if needs_cookies(exc):
            raise FetchError(f"{platform or '该平台'}拒绝匿名访问（{exc}）", hint=cookie_hint()) from exc

        result = metadata_fallback(url, ctx, platform=platform, reason=str(exc))
        if len(result.text.strip()) < 5:
            if needs_cookies(str(result.warnings)):
                raise FetchError(f"{platform or '该平台'}需要登录后才能访问", hint=cookie_hint()) from exc
            raise UnsupportedContentError(
                f"无法下载该视频，也未能获取到文字信息：{exc}。"
                "可尝试：更新 yt-dlp、提供 cookies 或改用「粘贴文案」模式。"
            )
        return result
