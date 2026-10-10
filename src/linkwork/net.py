"""网络环境适配：镜像探测、socket 超时、HTTP 会话配置。

国内网络常无法直连 huggingface.co，导致语音识别模型下载卡死。
这里在真正下载模型前做一次快速探测，自动切换到可用镜像（hf-mirror.com）。
"""

from __future__ import annotations

import logging
import os
import socket

log = logging.getLogger(__name__)

#: 候选模型下载端点（按顺序探测）
HF_ENDPOINTS = (
    "https://huggingface.co",
    "https://hf-mirror.com",
)

_PROBE_CACHE: dict[str, tuple[bool, float]] = {}
_APPLIED = False

DEFAULT_SOCKET_TIMEOUT = 15.0


def configure_socket_defaults() -> None:
    """给全局 socket 设置默认超时，避免网络被墙时无限期卡住。"""
    try:
        socket.setdefaulttimeout(float(os.environ.get("LINKWORK_SOCKET_TIMEOUT", DEFAULT_SOCKET_TIMEOUT)))
    except Exception as exc:  # pragma: no cover
        log.debug("设置 socket 默认超时失败：%s", exc)


def probe(url: str, timeout: float = 3.0) -> bool:
    """快速探测某个 HTTPS 站点是否可达（带缓存）。"""
    key = url
    if key in _PROBE_CACHE:
        ok, _ = _PROBE_CACHE[key]
        return ok
    import ssl  # noqa: PLC0415
    import urllib.parse  # noqa: PLC0415

    parsed = urllib.parse.urlparse(url)
    host = parsed.hostname or ""
    port = parsed.port or 443
    ok = False
    try:
        with socket.create_connection((host, port), timeout=timeout):
            ok = True
    except Exception as exc:  # noqa: BLE001
        log.debug("探测 %s 失败：%s", host, exc)
    _PROBE_CACHE[key] = (ok, timeout)
    return ok


def ensure_model_endpoint(force: bool = False) -> str:
    """确保 HF_ENDPOINT 指向可用端点，返回最终端点。

    优先级：
    1. 用户已显式设置的 HF_ENDPOINT（除非 force）；
    2. 直连 huggingface.co 可用时用它；
    3. 否则使用国内镜像 hf-mirror.com。
    """
    global _APPLIED
    existing = os.environ.get("HF_ENDPOINT", "").strip()
    if existing and not force:
        log.debug("沿用用户设置的 HF_ENDPOINT=%s", existing)
        return existing

    for endpoint in HF_ENDPOINTS:
        if probe(endpoint):
            os.environ["HF_ENDPOINT"] = endpoint
            _APPLIED = True
            if endpoint != HF_ENDPOINTS[0]:
                log.info("huggingface.co 不可达，已切换模型下载镜像：%s", endpoint)
            return endpoint

    log.warning("所有模型下载端点均不可达，模型下载可能失败")
    return os.environ.get("HF_ENDPOINT", HF_ENDPOINTS[0])


def prepare_asr_download() -> str:
    """在加载 ASR 模型前调用：设置端点与超时。"""
    configure_socket_defaults()
    return ensure_model_endpoint()


def environment_summary() -> dict[str, object]:
    """供 doctor / GUI 展示的网络环境信息。"""
    return {
        "hf_endpoint": os.environ.get("HF_ENDPOINT", ""),
        "hf_reachable": probe("https://huggingface.co"),
        "mirror_reachable": probe("https://hf-mirror.com"),
        "socket_timeout": socket.getdefaulttimeout(),
    }
