"""解析器基础设施：上下文、HTTP 会话、解析器协议。"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Protocol

from ..models import ExtractResult
from ..utils import MOBILE_UA, default_ua, detect_platform, host_of

log = logging.getLogger(__name__)

ProgressCb = Callable[[str, float], None]


@dataclass
class ResolveContext:
    """一次提取任务的可配置上下文。"""

    workdir: Path
    """本次任务的临时工作目录。"""

    cookies: str | None = None
    """cookies 文件路径，或 "browser:chrome" 形式。"""

    proxy: str | None = None
    timeout: int = 20
    #: 任务级诊断日志（可写各阶段细节；缺省为无操作对象）
    tasklog: Any = None

    #: 是否跳过 ASR（只做网页/字幕提取时使用）
    asr_engine: str = "auto"
    asr_model: str = "small"
    language: str | None = None
    progress: ProgressCb | None = None
    user_agent: str = field(default_factory=default_ua)
    mobile_ua: str = MOBILE_UA
    extra_headers: dict[str, str] = field(default_factory=dict)

    #: 复用的 HTTP 会话（保持 cookies，如 B 站 buvid）
    _session: Any = field(default=None, init=False, repr=False)

    @property
    def session(self):
        """懒加载 requests.Session，跨请求共享 cookies。"""
        if self._session is None:
            import requests  # noqa: PLC0415

            self._session = requests.Session()
            self._session.headers.update({"User-Agent": self.user_agent, "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8"})
            if self.proxy:
                self._session.proxies.update({"http": self.proxy, "https": self.proxy})
            if self.cookies and not self.cookies.startswith("browser:"):
                jar = _load_cookiejar(self.cookies)
                if jar is not None:
                    self._session.cookies.update(jar)
        return self._session

    def report(self, stage: str, ratio: float = 0.0) -> None:
        if self.progress:
            try:
                self.progress(stage, ratio)
            except Exception:  # pragma: no cover - 进度回调不应影响主流程
                log.debug("进度回调异常", exc_info=True)

    def http_headers(self, mobile: bool = False) -> dict[str, str]:
        headers = {
            "User-Agent": self.mobile_ua if mobile else self.user_agent,
            "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        }
        headers.update(self.extra_headers)
        return headers


class Resolver(Protocol):
    """解析器协议。"""

    name: str
    priority: int

    def matches(self, url: str) -> bool: ...

    def resolve(self, url: str, ctx: ResolveContext) -> ExtractResult: ...


class BaseResolver:
    """提供 HTTP 会话等公共能力。"""

    name = "base"
    priority = 100
    domains: tuple[str, ...] = ()

    def matches(self, url: str) -> bool:
        host = host_of(url)
        if not host:
            return False
        if not self.domains:
            return True
        return any(host == d or host.endswith("." + d) for d in self.domains)

    # -- HTTP ------------------------------------------------------------- #
    def request(
        self,
        url: str,
        ctx: ResolveContext,
        *,
        mobile: bool = False,
        method: str = "GET",
        allow_redirects: bool = True,
        timeout: int | None = None,
        **kwargs: Any,
    ):
        import requests  # noqa: PLC0415

        headers = ctx.http_headers(mobile=mobile)
        headers.update(kwargs.pop("headers", {}) or {})
        proxies = {"http": ctx.proxy, "https": ctx.proxy} if ctx.proxy and not ctx.session.proxies else None
        return ctx.session.request(
            method,
            url,
            headers=headers,
            timeout=timeout or ctx.timeout,
            allow_redirects=allow_redirects,
            proxies=proxies,
            **kwargs,
        )

    def fetch_html(self, url: str, ctx: ResolveContext, *, mobile: bool = False) -> tuple[str, str]:
        """抓取 HTML，返回 (最终 URL, 文本)。"""
        resp = self.request(url, ctx, mobile=mobile)
        # 部分站点不声明编码，交给 requests 猜测后再兜底 utf-8
        if not resp.encoding or resp.encoding.lower() == "iso-8859-1":
            resp.encoding = resp.apparent_encoding or "utf-8"
        return resp.url, resp.text

    def base_result(self, url: str, **kwargs: Any) -> ExtractResult:
        return ExtractResult(url=url, platform=detect_platform(url), **kwargs)


def _load_cookiejar(path: str):
    """支持 Netscape cookies.txt 与浏览器导出的 JSON。"""
    try:
        import http.cookiejar as cookiejar  # noqa: PLC0415

        jar = cookiejar.MozillaCookieJar(path)
        jar.load(ignore_discard=True, ignore_expires=True)
        return jar
    except Exception:
        pass
    try:
        import json  # noqa: PLC0415

        from requests.cookies import RequestsCookieJar  # noqa: PLC0415

        data = json.loads(Path(path).read_text(encoding="utf-8"))
        jar = RequestsCookieJar()
        if isinstance(data, list):
            for item in data:
                if isinstance(item, dict) and item.get("name"):
                    jar.set(item["name"], item.get("value", ""), domain=item.get("domain"), path=item.get("path", "/"))
        elif isinstance(data, dict):
            for key, value in data.items():
                if isinstance(value, str):
                    jar.set(key, value)
        return jar
    except Exception as exc:
        log.debug("cookies 解析失败：%s", exc)
        return None
