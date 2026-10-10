"""链接解析器注册表：按域名把链接分派给合适的解析器。"""

from __future__ import annotations

import logging

from ..errors import FetchError, InvalidURLError, LinkWorkError, MediaError, UnsupportedContentError
from ..models import ExtractResult
from .base import ResolveContext, Resolver

log = logging.getLogger(__name__)


def _build_default_resolvers() -> list[Resolver]:
    from .bilibili import BilibiliResolver
    from .douyin import DouyinResolver
    from .video import VideoResolver
    from .web import WebArticleResolver
    from .xiaohongshu import XiaohongshuResolver
    from .youtube import YouTubeResolver

    resolvers: list[Resolver] = [
        # 平台专用解析器优先级最高：先尝试抓平台自带字幕 / 图文文案
        BilibiliResolver(),
        YouTubeResolver(),
        DouyinResolver(),
        XiaohongshuResolver(),
        # 通用视频（含直链）与网页图文兜底
        VideoResolver(),
        WebArticleResolver(),
    ]
    return resolvers


class ResolverRegistry:
    """按优先级串行尝试各解析器，第一个成功的结果即返回。"""

    def __init__(self, resolvers: list[Resolver] | None = None) -> None:
        self.resolvers = resolvers if resolvers is not None else _build_default_resolvers()

    def resolve(self, url: str, ctx: ResolveContext) -> ExtractResult:
        if not url or not url.startswith(("http://", "https://")):
            raise InvalidURLError(f"不是合法的链接：{url}")

        errors: list[tuple[str, Exception]] = []
        for resolver in self.resolvers:
            if not resolver.matches(url):
                continue
            log.info("使用解析器 %s 处理链接", resolver.name)
            try:
                result = resolver.resolve(url, ctx)
            except UnsupportedContentError as exc:
                if exc.final:
                    # 已确定无法处理（例如页面明确是 404），不要再让兜底解析器
                    # 把错误页的导航文案当成正文返回
                    log.info("%s 判定为不可处理：%s", resolver.name, exc.message)
                    raise
                log.info("%s 不适用：%s", resolver.name, exc.message)
                errors.append((resolver.name, exc))
                continue
            except Exception as exc:  # noqa: BLE001
                log.warning("%s 解析失败：%s", resolver.name, exc)
                errors.append((resolver.name, exc))
                continue
            # 判定解析是否有效：正文、分段（口播稿）或可下载地址三者之一
            if result and (result.text.strip() or result.segments or result.extra.get("download_url")):
                return result
            errors.append((resolver.name, UnsupportedContentError("未提取到任何文案内容")))

        if errors:
            name, exc = self._most_actionable(errors)
            detail = exc.user_message() if hasattr(exc, "user_message") else str(exc)
            # 底层错误本身已带可操作提示（如 cookies、ffmpeg）时直接抛出，
            # 避免被包装成笼统的「不支持该链接类型」而丢失关键指引
            if isinstance(exc, LinkWorkError) and exc.hint:
                raise exc
            raise UnsupportedContentError(f"无法从该链接提取文案（{name}：{detail}）")
        raise UnsupportedContentError("没有可用于该链接的解析器。")

    @staticmethod
    def _most_actionable(errors: list[tuple[str, Exception]]) -> tuple[str, Exception]:
        """从多个解析器的失败原因里挑出最有指导意义的一个。

        登录/cookies 类问题 > 媒体/引擎问题 > 通用「不适用」。
        """

        def rank(item: tuple[str, Exception]) -> int:
            exc = item[1]
            if isinstance(exc, FetchError):
                return 3
            if isinstance(exc, MediaError):
                return 2
            if isinstance(exc, LinkWorkError) and exc.hint:
                return 1
            return 0

        return max(errors, key=rank)


__all__ = ["ResolverRegistry", "Resolver", "ResolveContext"]
