"""媒体层：ffmpeg 定位、音频下载、音频转码、字幕解析。

设计要点：
- 不依赖 Homebrew / 系统 ffmpeg：优先 imageio-ffmpeg 自带的静态 ffmpeg 二进制；
- 不使用 yt-dlp 的 FFmpegExtractAudio 后处理器（它需要 ffprobe），
  而是直接下载音轨后用 ffmpeg 自行转成 16kHz 单声道 WAV，供语音识别使用；
- 支持从视频链接直接获取平台自带字幕（比 ASR 更快更准）。
"""

from __future__ import annotations

import json
import logging
import os
import re
import shutil
import subprocess
from collections.abc import Callable
from pathlib import Path
from typing import Any

from .errors import FetchError, FFmpegMissingError, MediaError
from .utils import default_ua

log = logging.getLogger(__name__)

ProgressCb = Callable[[str, float], None]

#: yt-dlp 最近一次失败原因（用于判断是否属于「需要 cookies」等场景）
LAST_YTDLP_ERROR: str = ""

_FFMPEG_CACHE: str | None = None

#: yt-dlp 音频格式，按音质从高到低尝试
#: 注意：抖音/B 站等平台会提供多条音轨，低码率那条有时是静音或纯伴奏，
#: 因此这里按码率降序排列，并在下载后做信号校验。
FORMAT_AUDIO_TIERS: tuple[tuple[str, str], ...] = (
    ("bestaudio[abr>=128]/bestaudio[ext=m4a]/bestaudio", "高码率音频流"),
    ("bestaudio", "默认音频流"),
    ("best[acodec!=none]/best", "视频流（提取音轨）"),
)

_VIDEO_EXTS = {".mp4", ".mkv", ".webm", ".flv", ".mov", ".m4v", ".ts", ".avi", ".wmv", ".mpg", ".mpeg", ".3gp"}
_AUDIO_EXTS = {".m4a", ".mp3", ".aac", ".wav", ".opus", ".ogg", ".flac", ".wma", ".weba"}


# --------------------------------------------------------------------------- #
# ffmpeg / ffprobe 定位
# --------------------------------------------------------------------------- #
def _from_env() -> str | None:
    env = os.environ.get("LINKWORK_FFMPEG") or os.environ.get("FFMPEG_BINARY")
    return env if env and Path(env).exists() else None


def _from_path(names: tuple[str, ...]) -> str | None:
    for name in names:
        found = shutil.which(name)
        if found:
            return found
    return None


def _from_bundled() -> str | None:
    try:  # pragma: no cover - 取决于安装情况
        import imageio_ffmpeg

        return imageio_ffmpeg.get_ffmpeg_exe()
    except Exception as exc:  # pragma: no cover
        log.debug("imageio-ffmpeg 不可用: %s", exc)
        return None


def _from_common_paths() -> str | None:
    candidates = [
        "/opt/homebrew/bin/ffmpeg",
        "/usr/local/bin/ffmpeg",
        "/usr/bin/ffmpeg",
        str(Path.home() / "bin" / "ffmpeg"),
    ]
    for path in candidates:
        if Path(path).exists():
            return path
    return None


def find_ffmpeg(required: bool = True) -> str:
    """定位可用的 ffmpeg 可执行文件。"""
    global _FFMPEG_CACHE
    if _FFMPEG_CACHE and Path(_FFMPEG_CACHE).exists():
        return _FFMPEG_CACHE
    for resolver in (_from_env, _from_path, _from_bundled, _from_common_paths):
        if resolver is _from_path:
            found = _from_path(("ffmpeg",))
        else:
            found = resolver()  # type: ignore[operator]
        if found:
            _FFMPEG_CACHE = found
            log.debug("使用 ffmpeg: %s", found)
            return found
    if required:
        raise FFmpegMissingError("未找到 ffmpeg，无法处理音频/视频。")
    return ""


def ffmpeg_available() -> bool:
    try:
        return bool(find_ffmpeg(required=False))
    except Exception:
        return False


def run_ffmpeg(args: list[str], timeout: int = 3600) -> subprocess.CompletedProcess[str]:
    exe = find_ffmpeg()
    cmd = [exe, "-hide_banner", "-loglevel", "error", "-y", *args]
    log.debug("执行: %s", " ".join(cmd))
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired as exc:  # pragma: no cover
        raise MediaError(f"ffmpeg 处理超时（>{timeout}s）") from exc
    if proc.returncode != 0:
        tail = (proc.stderr or "").strip().splitlines()[-6:]
        raise MediaError("ffmpeg 执行失败：" + " / ".join(tail))
    return proc


def media_duration(path: str | Path) -> float | None:
    """用 ffmpeg 解析媒体时长（无 ffprobe 时可用）。"""
    try:
        proc = subprocess.run(
            [find_ffmpeg(), "-hide_banner", "-i", str(path)],
            capture_output=True,
            text=True,
            timeout=60,
        )
    except Exception:
        return None
    output = (proc.stderr or "") + (proc.stdout or "")
    match = re.search(r"Duration:\s*(\d+):(\d+):(\d+\.?\d*)", output)
    if not match:
        return None
    hours, minutes, seconds = match.groups()
    return int(hours) * 3600 + int(minutes) * 60 + float(seconds)


# --------------------------------------------------------------------------- #
# 音频提取 / 转码
# --------------------------------------------------------------------------- #
def to_asr_wav(src: str | Path, dest_dir: str | Path | None = None, sample_rate: int = 16000) -> Path:
    """把任意音视频转成 ASR 友好的 16kHz 单声道 WAV。"""
    src = Path(src)
    if not src.exists():
        raise MediaError(f"源文件不存在：{src}")
    dest_dir = Path(dest_dir) if dest_dir else src.parent
    dest_dir.mkdir(parents=True, exist_ok=True)
    out = dest_dir / f"{src.stem}.asr.wav"
    run_ffmpeg(
        [
            "-i",
            str(src),
            "-vn",
            "-ac",
            "1",
            "-ar",
            str(sample_rate),
            "-acodec",
            "pcm_s16le",
            "-f",
            "wav",
            str(out),
        ]
    )
    if not out.exists() or out.stat().st_size < 1024:
        raise MediaError("音频转码失败：输出为空，可能该链接不含音轨。")
    return out


def audio_signal_stats(audio_path: str | Path, max_seconds: float = 180.0) -> dict[str, float]:
    """返回音频的峰值/RMS/时长，用于判断音轨是否有效。"""
    import wave  # noqa: PLC0415

    import numpy as np  # noqa: PLC0415

    audio_path = Path(audio_path)
    try:
        with wave.open(str(audio_path), "rb") as handle:
            rate = handle.getframerate() or 16000
            channels = handle.getnchannels()
            limit = int(max_seconds * rate)
            frames = handle.readframes(min(handle.getnframes(), limit))
    except Exception as exc:  # noqa: BLE001
        log.debug("读取音频失败：%s", exc)
        return {"peak": 0.0, "rms": 0.0, "duration": 0.0, "samples": 0.0}

    samples = np.frombuffer(frames, dtype=np.int16).astype(np.float32) / 32768.0
    if channels > 1 and samples.size:
        samples = samples.reshape(-1, channels).mean(axis=1)
    if samples.size == 0:
        return {"peak": 0.0, "rms": 0.0, "duration": 0.0, "samples": 0.0}
    return {
        "peak": float(np.abs(samples).max()),
        "rms": float(np.sqrt((samples**2).mean())),
        "duration": samples.size / float(rate),
        "samples": float(samples.size),
    }


def is_silent_audio(audio_path: str | Path, peak_threshold: float = 0.01, rms_threshold: float = 0.002) -> bool:
    """音轨是否为静音（峰值与均方根都极低）。"""
    stats = audio_signal_stats(audio_path)
    if stats["samples"] <= 0:
        return True
    return stats["peak"] < peak_threshold or stats["rms"] < rms_threshold


# --------------------------------------------------------------------------- #
# yt-dlp 下载
# --------------------------------------------------------------------------- #
def _import_yt_dlp():
    try:
        import yt_dlp  # noqa: PLC0415

        return yt_dlp
    except ImportError as exc:  # pragma: no cover
        raise MediaError("未安装 yt-dlp，无法下载视频音轨。请运行 scripts/setup.sh") from exc


def build_ydl_opts(
    *,
    outtmpl: str,
    cookies: str | None = None,
    proxy: str | None = None,
    quiet: bool = True,
    progress_hook: Callable[[dict[str, Any]], None] | None = None,
    extra: dict[str, Any] | None = None,
) -> dict[str, Any]:
    opts: dict[str, Any] = {
        "outtmpl": outtmpl,
        "quiet": quiet,
        "no_warnings": quiet,
        "noprogress": True,
        "noplaylist": True,
        "retries": 3,
        "fragment_retries": 3,
        "socket_timeout": 30,
        "nocheckcertificate": True,
        "http_headers": {"User-Agent": default_ua()},
        # 不使用 ffmpeg 后处理器，避免依赖 ffprobe
        "postprocessors": [],
        "prefer_ffmpeg": False,
        "writethumbnail": False,
        "writesubtitles": False,
        "writeautomaticsub": False,
    }
    if cookies:
        if cookies.startswith("browser:"):
            browser = cookies.split(":", 1)[1]
            opts["cookiesfrombrowser"] = (browser, None, None, None)
        else:
            opts["cookiefile"] = cookies
    if proxy:
        opts["proxy"] = proxy
    if progress_hook:
        opts["progress_hooks"] = [progress_hook]
    if extra:
        opts.update(extra)
    return opts


def _make_progress_adapter(progress: ProgressCb | None, stage: str):
    if not progress:
        return None

    def hook(data: dict[str, Any]) -> None:
        status = data.get("status")
        if status == "downloading":
            total = data.get("total_bytes") or data.get("total_bytes_estimate") or 0
            done = data.get("downloaded_bytes") or 0
            ratio = (done / total) if total else 0.0
            progress(stage, min(max(ratio, 0.0), 1.0))
        elif status == "finished":
            progress(stage, 1.0)

    return hook


def probe_info(url: str, *, cookies: str | None = None, proxy: str | None = None) -> dict[str, Any]:
    """只抓取元信息（标题、时长、字幕清单），不下载媒体。"""
    yt_dlp = _import_yt_dlp()
    opts = build_ydl_opts(outtmpl="%(id)s", cookies=cookies, proxy=proxy, extra={"skip_download": True})
    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(url, download=False)
    except Exception as exc:
        global LAST_YTDLP_ERROR
        LAST_YTDLP_ERROR = str(exc)
        raise FetchError(f"无法解析该视频链接：{_short_error(exc)}") from exc
    if info and "entries" in info:  # 播放列表/图文合集
        entries = [e for e in (info.get("entries") or []) if e]
        if entries:
            info = entries[0]
    return info or {}


def download_media(
    url: str,
    dest_dir: str | Path,
    *,
    cookies: str | None = None,
    proxy: str | None = None,
    progress: ProgressCb | None = None,
    fmt: str | None = None,
    audio_only: bool = True,
    tier_limit: int | None = None,
) -> tuple[Path, dict[str, Any]]:
    """按音质从高到低尝试下载音频，返回 (本地文件路径, 元信息)。

    注意：音轨是否有声需要转成 WAV 后才能判断，因此静音校验放在
    :mod:`linkwork.transcribe` 的流程里（见 ``download_and_transcribe``）。
    """
    yt_dlp = _import_yt_dlp()
    dest_dir = Path(dest_dir)
    dest_dir.mkdir(parents=True, exist_ok=True)
    template = str(dest_dir / "%(id)s.%(ext)s")

    if fmt:
        attempts: list[tuple[str, str]] = [(fmt, "指定格式")]
    elif audio_only:
        attempts = list(FORMAT_AUDIO_TIERS)
    else:
        attempts = [("best[ext=mp4]/best", "视频流（提取音轨）")]
    if tier_limit is not None:
        attempts = attempts[:tier_limit]

    last_error: Exception | None = None
    for index, (format_spec, label) in enumerate(attempts):
        stage = "下载音频" if index == 0 else f"下载音频（{label}）"
        opts = build_ydl_opts(
            outtmpl=template,
            cookies=cookies,
            proxy=proxy,
            progress_hook=_make_progress_adapter(progress, stage),
            extra={"format": format_spec, "overwrites": True},
        )
        try:
            log.info("尝试以%s下载：%s", label, url)
            with yt_dlp.YoutubeDL(opts) as ydl:
                info = ydl.extract_info(url, download=True)
            if info and "entries" in info:
                entries = [e for e in (info.get("entries") or []) if e]
                if entries:
                    info = entries[0]
            path = _locate_downloaded(info, dest_dir, ydl)
            if not path:
                last_error = MediaError("下载完成但未找到媒体文件")
                continue
            return path, info
        except Exception as exc:  # noqa: BLE001
            last_error = exc
            log.warning("%s下载失败：%s", label, exc)
            if progress:
                progress("下载失败，重试中", 0.0)
            continue

    raise MediaError(f"媒体下载失败：{last_error}")


def _locate_downloaded(info: dict[str, Any], dest_dir: Path, ydl: Any = None) -> Path | None:
    """在下载目录中定位实际文件。"""
    candidates: list[Path] = []
    for key in ("requested_downloads",):
        for item in info.get(key) or []:
            fp = item.get("filepath")
            if fp:
                candidates.append(Path(fp))
    if info.get("filepath"):
        candidates.append(Path(info["filepath"]))
    if info.get("_filename"):
        candidates.append(Path(info["_filename"]))
    for path in candidates:
        if path.exists():
            return path

    media_id = str(info.get("id") or "")
    if media_id:
        for path in sorted(dest_dir.glob(f"{media_id}*")):
            if path.suffix.lower() in _AUDIO_EXTS | _VIDEO_EXTS and not path.name.endswith(".part"):
                return path
    files = [
        p
        for p in dest_dir.iterdir()
        if p.is_file() and p.suffix.lower() in _AUDIO_EXTS | _VIDEO_EXTS and not p.name.endswith(".part")
    ]
    if files:
        return max(files, key=lambda p: p.stat().st_mtime)
    return None


# --------------------------------------------------------------------------- #
# 字幕（平台自带字幕优先于语音识别）
# --------------------------------------------------------------------------- #
#: 字幕语言优先级：中文优先（本工具面向中文口播稿）
_SUB_LANGS = ["zh-Hans", "zh-CN", "zh", "zh-Hant", "zh-TW", "en", "en-US"]

_TAG_RE = re.compile(r"<[^>]+>")
_VTT_TIME_RE = re.compile(
    r"(\d{1,2}:\d{2}:\d{2}[.,]\d{1,3}|\d{1,2}:\d{2}[.,]\d{1,3})\s*-->\s*(\d{1,2}:\d{2}:\d{2}[.,]\d{1,3}|\d{1,2}:\d{2}[.,]\d{1,3})"
)


def pick_subtitle_track(info: dict[str, Any], prefer_auto: bool = True) -> tuple[str, str, str] | None:
    """从元信息中挑选最合适的字幕轨，返回 (url, ext, lang)。"""
    manual = info.get("subtitles") or {}
    auto = info.get("automatic_captions") or {}
    groups = [manual] + ([auto] if prefer_auto else [])
    for group in groups:
        if not group:
            continue
        langs = list(group.keys())
        # 先精确匹配（zh-Hans 优先于 zh），再前缀匹配（zh-Hans-CN 等变体）
        ordered: list[str] = []
        lowered = {lang.lower(): lang for lang in langs}
        for want in _SUB_LANGS:
            actual = lowered.get(want.lower())
            if actual:
                ordered.append(actual)
        for want in _SUB_LANGS:
            for lang in langs:
                if lang not in ordered and lang.lower().startswith(want.lower()):
                    ordered.append(lang)
        for lang in ordered:
            tracks = group[lang] or []
            # 优先 vtt（无需转换），其次 srt/json3
            for ext in ("vtt", "srt", "json3"):
                for track in tracks:
                    if track.get("ext") == ext and track.get("url"):
                        return track["url"], ext, lang
    return None


def download_subtitle(
    url: str, dest_path: str | Path, *, cookies: str | None = None, proxy: str | None = None
) -> Path:
    import requests  # noqa: PLC0415

    dest_path = Path(dest_path)
    dest_path.parent.mkdir(parents=True, exist_ok=True)
    headers = {"User-Agent": default_ua()}
    proxies = {"http": proxy, "https": proxy} if proxy else None
    resp = requests.get(url, headers=headers, timeout=30, proxies=proxies)
    resp.raise_for_status()
    dest_path.write_bytes(resp.content)
    return dest_path


def _parse_time(value: str) -> float:
    value = value.replace(",", ".")
    parts = value.split(":")
    if len(parts) == 3:
        return int(parts[0]) * 3600 + int(parts[1]) * 60 + float(parts[2])
    if len(parts) == 2:
        return int(parts[0]) * 60 + float(parts[1])
    return float(parts[0])


def parse_subtitle_file(path: str | Path) -> list[tuple[float, float, str]]:
    """解析 vtt/srt 字幕为 [(start, end, text)]。"""
    path = Path(path)
    raw = path.read_text(encoding="utf-8", errors="ignore")
    if path.suffix == ".json3" or raw.lstrip().startswith("{"):
        return _parse_json3(raw)
    return _parse_vtt_srt(raw)


def _parse_vtt_srt(raw: str) -> list[tuple[float, float, str]]:
    out: list[tuple[float, float, str]] = []
    blocks = re.split(r"\n\s*\n", raw.replace("\r\n", "\n").replace("\r", "\n"))
    for block in blocks:
        lines = [ln for ln in block.split("\n") if ln.strip()]
        if not lines:
            continue
        time_idx = None
        match = None
        for idx, line in enumerate(lines):
            match = _VTT_TIME_RE.search(line)
            if match:
                time_idx = idx
                break
        if match is None or time_idx is None:
            continue
        start = _parse_time(match.group(1))
        end = _parse_time(match.group(2))
        text_lines = lines[time_idx + 1 :]
        text = " ".join(_TAG_RE.sub("", ln).strip() for ln in text_lines).strip()
        text = re.sub(r"\s+", " ", text)
        if text and not text.startswith("WEBVTT"):
            out.append((start, end, text))
    return _dedupe(out)


def _parse_json3(raw: str) -> list[tuple[float, float, str]]:
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return []
    out: list[tuple[float, float, str]] = []
    for event in data.get("events") or []:
        segs = event.get("segs") or []
        text = "".join(s.get("utf8", "") for s in segs).strip()
        text = re.sub(r"\s+", " ", text)
        if not text:
            continue
        start = (event.get("tStartMs") or 0) / 1000
        dur = (event.get("dDurationMs") or 0) / 1000
        out.append((start, start + dur, text))
    return _dedupe(out)


def _dedupe(items: list[tuple[float, float, str]]) -> list[tuple[float, float, str]]:
    """去掉滚动字幕造成的重复行。"""
    out: list[tuple[float, float, str]] = []
    for start, end, text in items:
        if out and out[-1][2] == text:
            continue
        if out and text in out[-1][2] and start < out[-1][1] + 0.5:
            continue
        out.append((start, end, text))
    return out


def _short_error(exc: Exception | str, limit: int = 160) -> str:
    """压缩错误信息：截断超长 URL，但保留域名（用于判断平台与给出提示）。"""
    from urllib.parse import urlparse  # noqa: PLC0415

    def replace(match: re.Match) -> str:
        url = match.group(0)
        try:
            host = urlparse(url).hostname or ""
        except ValueError:
            host = ""
        return f"<URL:{host}>" if host else "<URL>"

    text = re.sub(r"https?://\S{40,}", replace, str(exc))
    text = re.sub(r"\s+", " ", text).strip()
    return text if len(text) <= limit else text[: limit - 1] + "…"


def temp_workspace(prefix: str = "linkwork_") -> Path:
    """创建本次任务的工作区（放在可回收的缓存目录下，便于统一清理）。"""
    from . import cache  # noqa: PLC0415

    label = prefix[len(cache.WORKSPACE_PREFIX):] or "job" if prefix.startswith(cache.WORKSPACE_PREFIX) else prefix
    return cache.new_workspace(label.rstrip("_") or "job")
