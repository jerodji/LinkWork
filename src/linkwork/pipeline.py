"""编排层：把「链接 / 粘贴文本」变成最终文案结果。"""

from __future__ import annotations

import logging
import os
import shutil
import tempfile
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from .errors import LinkWorkError, UnsupportedContentError
from .exporters import save_result
from .models import ExtractResult
from .resolvers import ResolverRegistry
from .resolvers.base import ResolveContext
from .utils import detect_platform, normalize_url, setup_logging

log = logging.getLogger(__name__)

ProgressCb = Callable[[str, float], None]

DEFAULT_OUTPUT_DIR = Path(os.environ.get("LINKWORK_OUTPUT_DIR", Path.cwd() / "output"))
DEFAULT_FORMATS = ("md", "txt")


@dataclass
class ExtractOptions:
    """一次提取任务的全部可调参数。"""

    out_dir: Path = field(default_factory=lambda: DEFAULT_OUTPUT_DIR)
    formats: tuple[str, ...] = DEFAULT_FORMATS
    cookies: str | None = None
    proxy: str | None = None
    asr_engine: str = "auto"
    asr_model: str = field(default_factory=lambda: os.environ.get("LINKWORK_WHISPER_MODEL", "small"))
    language: str | None = None
    timeout: int = 20
    #: 只提取字幕/网页文本，不做语音识别
    subtitles_only: bool = False
    #: 保留临时音频文件（默认任务结束后删除）
    keep_temp: bool = False
    #: 自定义工作目录（默认系统临时目录）
    workdir: Path | None = None
    progress: ProgressCb | None = None
    quiet: bool = True

    def engine_name(self) -> str:
        return "none" if self.subtitles_only else self.asr_engine


@dataclass
class PipelineOutput:
    """提取 + 保存的最终产物。"""

    result: ExtractResult
    files: dict[str, Path] = field(default_factory=dict)

    @property
    def text(self) -> str:
        return self.result.spoken_script if self.result.segments else self.result.text

    def primary_file(self) -> Path | None:
        for key in ("md", "txt", "json", "srt"):
            if key in self.files:
                return self.files[key]
        return None


def extract(
    url_or_text: str,
    options: ExtractOptions | None = None,
    *,
    save: bool = True,
) -> PipelineOutput:
    """提取入口：自动识别链接或纯文本。

    Args:
        url_or_text: 分享链接（可含分享文案）或直接粘贴的文案。
        options: 提取参数。
        save: 是否把结果写入 out_dir。
    """
    options = options or ExtractOptions()
    if not options.quiet:
        setup_logging(logging.INFO)
    else:
        setup_logging(logging.WARNING)

    # 统一网络策略：socket 超时 + 模型镜像探测，避免被墙时无限期卡住
    from .net import configure_socket_defaults, ensure_model_endpoint

    configure_socket_defaults()
    ensure_model_endpoint()

    raw = (url_or_text or "").strip()
    if not raw:
        raise UnsupportedContentError("输入为空，请提供链接或文案内容。")

    url = normalize_url(raw)
    if url.startswith(("http://", "https://")):
        result = extract_url(url, options)
    else:
        result = extract_text(raw)

    files: dict[str, Path] = {}
    if save:
        files = save_result(result, options.out_dir, options.formats)
        for fmt, path in files.items():
            log.info("已保存 %s：%s", fmt.upper(), path)
    return PipelineOutput(result=result, files=files)


def extract_url(url: str, options: ExtractOptions | None = None) -> ExtractResult:
    """仅做提取，不保存文件。"""
    options = options or ExtractOptions()
    workdir = Path(options.workdir) if options.workdir else Path(tempfile.mkdtemp(prefix="linkwork_"))
    workdir.mkdir(parents=True, exist_ok=True)

    ctx = ResolveContext(
        workdir=workdir,
        cookies=options.cookies,
        proxy=options.proxy,
        timeout=options.timeout,
        asr_engine=options.engine_name(),
        asr_model=options.asr_model,
        language=options.language,
        progress=options.progress,
    )

    try:
        registry = ResolverRegistry()
        result = registry.resolve(url, ctx)
    except LinkWorkError:
        if not options.keep_temp:
            _cleanup(workdir)
        raise
    except Exception as exc:  # noqa: BLE001
        if not options.keep_temp:
            _cleanup(workdir)
        raise LinkWorkError(f"提取失败：{exc}") from exc

    _finalize(result)
    if not options.keep_temp:
        # 保留工作目录中的媒体文件路径信息后清理临时目录
        _cleanup(workdir)
    return result


def extract_text(text: str, title: str = "") -> ExtractResult:
    """处理直接粘贴的文案。"""
    body = (text or "").strip()
    if not body:
        raise UnsupportedContentError("粘贴内容为空。")
    lines = [line.strip() for line in body.splitlines()]
    first = next((line for line in lines if line), "")
    guess_title = title or (first[:40] if first else "粘贴文案")
    return ExtractResult(
        url="",
        kind="text",
        method="manual",
        title=guess_title,
        platform="手动粘贴",
        text=body,
        engine="用户输入",
    )


def extract_many(
    items: list[str],
    options: ExtractOptions | None = None,
    *,
    save: bool = True,
    continue_on_error: bool = True,
) -> list[PipelineOutput | Exception]:
    """批量提取；默认单条失败不影响其他条目。"""
    options = options or ExtractOptions()
    outputs: list[PipelineOutput | Exception] = []
    total = len(items)
    for index, item in enumerate(items, start=1):
        log.info("=== 处理第 %d/%d 条 ===", index, total)
        if options.progress:
            try:
                options.progress(f"任务 {index}/{total}", 0.0)
            except Exception:  # pragma: no cover
                pass
        try:
            outputs.append(extract(item, options, save=save))
        except Exception as exc:  # noqa: BLE001
            log.error("第 %d 条失败：%s", index, exc)
            if not continue_on_error:
                raise
            outputs.append(exc)
    return outputs


# --------------------------------------------------------------------------- #
def _finalize(result: ExtractResult) -> None:
    """结果收尾：补全标题、正文，统一平台名。"""
    from .textnorm import normalize_cjk

    if result.segments:
        result.text = result.text or ""
    if not result.platform and result.url:
        result.platform = detect_platform(result.url)
    if not result.title:
        result.title = result.platform or "未命名"
    # 去掉分段里可能重复的空格，并统一简繁/标点
    cleaned = []
    for seg in result.segments:
        seg.text = normalize_cjk(" ".join(seg.text.split()))
        if seg.text:
            cleaned.append(seg)
    result.segments = cleaned


def _cleanup(workdir: Path) -> None:
    try:
        shutil.rmtree(workdir, ignore_errors=True)
    except Exception as exc:  # noqa: BLE001
        log.debug("临时目录清理失败：%s", exc)
