"""共享的音视频 → 口播稿流程（先行字幕，兜底语音识别）。"""

from __future__ import annotations

import logging
from pathlib import Path

from . import media
from .asr import get_engine
from .errors import ASRError, MediaError, SilentAudioError
from .models import Segment
from .resolvers.base import ResolveContext
from .utils import sanitize_filename

log = logging.getLogger(__name__)


def segments_from_subtitle_track(
    info: dict,
    dest_dir: Path,
    ctx: ResolveContext,
) -> list[Segment] | None:
    """若平台提供字幕，下载并解析成 Segment 列表。"""
    track = media.pick_subtitle_track(info)
    if not track:
        return None
    url, ext, lang = track
    dest_dir.mkdir(parents=True, exist_ok=True)
    target = dest_dir / f"subtitle.{lang}.{ext}"
    try:
        ctx.report(f"下载平台字幕（{lang}）", 0.0)
        media.download_subtitle(url, target, cookies=ctx.cookies, proxy=ctx.proxy)
        rows = media.parse_subtitle_file(target)
    except Exception as exc:  # noqa: BLE001
        log.warning("字幕下载/解析失败，将改用语音识别：%s", exc)
        return None
    if not rows:
        return None
    log.info("平台字幕解析成功：%d 条", len(rows))
    return [Segment(start=s, end=e, text=t) for s, e, t in rows]


def prepare_asr_wav(path: str | Path, dest_dir: Path) -> Path:
    """把本地音视频转成 16kHz 单声道 WAV，并校验音轨不是静音。"""
    wav = media.to_asr_wav(path, dest_dir)
    stats = media.audio_signal_stats(wav)
    if stats["samples"] <= 0 or stats["peak"] < 0.01 or stats["rms"] < 0.002:
        raise SilentAudioError(
            f"音轨为静音（peak={stats['peak']:.4f}, rms={stats['rms']:.4f}），"
            "该平台可能返回了无音轨/伴奏轨",
            hint="可尝试提供 cookies（登录后可见的音频）或换用其他链接。",
        )
    return wav


def transcribe_local_media(
    path: str | Path, ctx: ResolveContext, dest_dir: Path | None = None
) -> tuple[list[Segment], float | None, str, str, Path]:
    """对本地音视频做语音识别，返回 (segments, duration, engine, model, wav)。"""
    path = Path(path)
    dest_dir = Path(dest_dir) if dest_dir else path.parent
    from .diagnostics import current_task_log  # noqa: PLC0415

    tlog = ctx.tasklog or current_task_log()
    tlog.stage("提取音频")
    try:
        src_size = Path(path).stat().st_size / 1048576
        tlog.note("源文件", f"{Path(path).name}（{src_size:.2f} MB）")
    except OSError:  # pragma: no cover
        pass
    ctx.report("提取音频", 0.0)
    wav = prepare_asr_wav(path, dest_dir)
    try:
        tlog.note("ASR 输入", f"{wav.name}（{wav.stat().st_size / 1048576:.2f} MB，16k 单声道）")
    except OSError:  # pragma: no cover
        pass

    def on_progress(ratio: float) -> None:
        ctx.report("语音识别", ratio)

    output = _transcribe_with_isolation(wav, ctx, on_progress)
    return (
        output.segments,
        output.duration or media.media_duration(path),
        output.engine,
        output.model,
        wav,
    )


def _transcribe_with_isolation(wav: Path, ctx: ResolveContext, on_progress) -> object:
    """在子进程里执行识别。

    为什么隔离：ctranslate2 偶发原生层死锁（线程永久阻塞、CPU 0%），
    Python 无法中断它。子进程方案让父进程可以超时 kill，服务永不卡死。
    """
    from .asr.isolated import transcribe_isolated

    engine = get_engine(ctx.asr_engine, model_size=ctx.asr_model)
    if not engine.is_available():
        raise ASRError(engine.unavailable_reason() or f"语音识别引擎 {engine.name} 不可用")

    # 非 faster-whisper 引擎（如 macOS 系统识别）无法子进程化，直接本地调用
    if getattr(engine, "name", "") != "faster-whisper":
        return engine.transcribe(wav, language=ctx.language or None, on_progress=on_progress)

    env_extra: dict[str, str] = {}
    try:
        from .net import prepare_asr_download  # noqa: PLC0415

        # 让子进程沿用父进程探测好的模型端点（国内网络自动走镜像）
        endpoint = prepare_asr_download()
        if endpoint:
            env_extra["HF_ENDPOINT"] = endpoint
    except Exception:  # noqa: BLE001
        env_extra = {}

    from .diagnostics import current_task_log  # noqa: PLC0415

    tlog = ctx.tasklog or current_task_log()
    tlog.stage("语音识别（子进程）")
    log_path = str(tlog.path) if getattr(tlog, "path", None) else None
    if log_path:
        tlog.note("子进程日志", "与父进程共用本文件")
    return transcribe_isolated(
        wav,
        model_size=ctx.asr_model or "small",
        language=ctx.language or None,
        vad_filter=True,
        on_progress=on_progress,
        env_extra=env_extra,
        tasklog_path=log_path,
    )


def download_and_transcribe(
    url: str,
    ctx: ResolveContext,
    *,
    info: dict | None = None,
    title_hint: str = "",
) -> dict:
    """下载链接音轨并转写；返回包含 segments/text/artifacts 的字典。

    处理顺序：
    1. 平台自带字幕（最快最准）；
    2. 按音质从高到低下载音频 → 转 16kHz WAV → 校验不是静音 → 语音识别；
       若某条音轨是静音，自动换下一条音轨重试。
    """
    workdir = Path(ctx.workdir) / "media" / sanitize_filename(title_hint or "clip", max_length=30)
    workdir.mkdir(parents=True, exist_ok=True)

    info = info if info is not None else media.probe_info(url, cookies=ctx.cookies, proxy=ctx.proxy)
    duration = info.get("duration")

    # 1) 平台自带字幕优先
    segments = segments_from_subtitle_track(info, workdir, ctx)
    if segments:
        return {
            "segments": segments,
            "method": "subtitle",
            "duration": duration,
            "engine": "平台字幕",
            "model": "",
            "artifacts": {"subtitle_dir": str(workdir)},
            "info": info,
        }

    # 2) 下载媒体 + 语音识别（静音音轨自动降级重试）
    tiers = len(media.FORMAT_AUDIO_TIERS)
    last_error: Exception | None = None
    for tier in range(1, tiers + 1):
        try:
            media_path, info = media.download_media(
                url,
                workdir,
                cookies=ctx.cookies,
                proxy=ctx.proxy,
                progress=ctx.progress,
                tier_limit=tier,
            )
            if not media_path.exists():
                media_path = _highest_tier_file(workdir)
            log.info("媒体已下载：%s", media_path)
            segments, wav_duration, engine_name, model_name, wav = transcribe_local_media(media_path, ctx, workdir)
            return {
                "segments": segments,
                "method": "asr",
                "duration": wav_duration or duration,
                "engine": f"{engine_name} {model_name}".strip(),
                "model": model_name,
                "artifacts": {"media": str(media_path), "wav": str(wav)},
                "info": info,
            }
        except SilentAudioError as exc:
            last_error = exc
            log.warning("第 %d 档音轨为静音，尝试下一档音质…", tier)
            if ctx.progress:
                ctx.progress("音轨静音，尝试其他音质", 0.0)
            continue
        except MediaError as exc:
            # 转码失败（文件损坏/无音轨）同样尝试下一档
            last_error = exc
            log.warning("第 %d 档音轨处理失败：%s", tier, exc)
            continue

    if last_error is not None:
        raise last_error
    raise MediaError("未能获取到可用的音轨")


def _highest_tier_file(workdir: Path) -> Path:
    """取出该目录中最新下载的媒体文件（避免误用上一次的中间产物）。"""
    suffix = media._AUDIO_EXTS | media._VIDEO_EXTS
    files = [
        p
        for p in workdir.iterdir()
        if p.is_file() and p.suffix.lower() in suffix and not p.name.endswith(".part")
    ]
    if not files:
        raise MediaError("下载目录中没有找到媒体文件")
    return max(files, key=lambda p: p.stat().st_mtime)


def needs_cookies(error: Exception | str) -> bool:
    """判断失败原因是否属于「需要 cookies / 被风控拦截」。"""
    message = str(error).lower()
    # yt-dlp 对抖音等平台的匿名请求常报 Unsupported URL（真实原因仍是缺少 cookies）
    if "unsupported url" in message and any(
        host in message for host in ("douyin", "iesdouyin", "kuaishou", "xiaohongshu", "xhslink", "weixin")
    ):
        return True
    markers = (
        "fresh cookies",
        "cookies are needed",
        "sign in",
        "login required",
        "not logged in",
        "需要登录",
        "风控",
        "http error 403",
        "captcha",
    )
    return any(marker in message for marker in markers)


def cookie_hint() -> str:
    return (
        "该平台要求登录态才能访问。请提供 cookies 后重试：\n"
        "  · 方式一：linkwork --cookies browser:chrome \"链接\"（需先在该浏览器登录）\n"
        "  · 方式二：导出 Netscape 格式 cookies.txt，再用 --cookies cookies.txt\n"
        "抖音/小红书/视频号在未登录时通常只返回通用页面，无法提取口播内容。"
    )


def metadata_fallback(
    url: str,
    ctx: ResolveContext,
    *,
    platform: str = "",
    info: dict | None = None,
    reason: str = "",
) -> "ExtractResult":
    """无法做语音识别（或媒体下载失败）时的降级结果：返回标题/简介/话题。

    保证「拿到链接至少有东西可用」，而不是直接报错。
    """
    from .models import ExtractResult  # noqa: PLC0415
    from .utils import detect_platform  # noqa: PLC0415

    if info is None:
        try:
            info = media.probe_info(url, cookies=ctx.cookies, proxy=ctx.proxy)
        except Exception as exc:  # noqa: BLE001
            # 风控/需要登录时探测也会失败，这里记录原因并返回空信息，
            # 由上层根据 needs_cookies 决定是否给出 cookies 提示
            log.debug("元信息抓取失败：%s", exc)
            info = {}
            if not reason:
                reason = str(exc)

    title = str(info.get("title") or "").strip()
    description = str(info.get("description") or "").strip()
    tags = info.get("tags") or []
    author = str(info.get("uploader") or info.get("channel") or info.get("creator") or "").strip()

    parts = [p for p in (title, description) if p]
    text = "\n".join(parts).strip()
    if isinstance(tags, list) and tags:
        text += "\n\n【话题】" + " ".join(f"#{t}#" for t in tags[:20])

    warning = "未能提取口播内容"
    if reason:
        warning += f"（{reason}）"
    warning += "，以下为页面标题与简介信息。可尝试：安装语音识别模型、提供 cookies 或更新 yt-dlp。"

    return ExtractResult(
        url=url,
        kind="video",
        method="metadata",
        title=title,
        author=author,
        platform=platform or detect_platform(url),
        text=text.strip(),
        duration=info.get("duration"),
        engine="页面元信息",
        warnings=[warning],
        extra={"description": description, "tags": tags} if tags else {"description": description},
    )


