"""Apple 系统语音识别引擎（macOS 原生 Speech 框架）。

优点：零模型下载、离线、速度快。
限制：
- 需要在「系统设置 → 隐私与安全性 → 语音识别」中授权；
- 在非 App（终端/命令行）进程里，授权回调不会自动触发，
  因此这里不主动弹窗，而是检测到未授权时直接给出明确提示；
- 单次请求时长建议 < 60s（本模块会自动分段处理）。

依赖：pip install pyobjc-framework-Speech pyobjc-framework-AVFoundation
"""

from __future__ import annotations

import logging
import tempfile
import time
from pathlib import Path

from ..errors import ASREngineUnavailableError, ASRError
from ..models import Segment
from ..textnorm import normalize_cjk
from .base import TranscriptionOutput

log = logging.getLogger(__name__)

#: 单段最大时长（秒），超过则切分后逐段识别
CHUNK_SECONDS = 55

AUTH_NOT_DETERMINED = 0
AUTH_AUTHORIZED = 3

_LOCALE_MAP = {
    "zh": "zh-CN",
    "zh-cn": "zh-CN",
    "zh-hans": "zh-CN",
    "zh-tw": "zh-TW",
    "zh-hant": "zh-TW",
    "en": "en-US",
    "ja": "ja-JP",
    "ko": "ko-KR",
    "yue": "zh-HK",
}


class AppleSpeechEngine:
    name = "apple"

    def __init__(
        self,
        locale: str = "zh-CN",
        model_size: str = "",
        on_device: bool = False,
        timeout_per_chunk: float = 120.0,
    ) -> None:
        self.locale = locale
        self.model_size = model_size or "系统内置模型"
        #: 默认关闭「仅设备端识别」，因为设备端模型未下载时会直接返回空结果
        self.on_device = on_device
        self.timeout_per_chunk = timeout_per_chunk

    # -- 可用性 ----------------------------------------------------------- #
    def is_available(self) -> bool:
        try:
            import AVFoundation  # noqa: F401, PLC0415
            import Speech  # noqa: F401, PLC0415
        except Exception as exc:  # noqa: BLE001
            log.debug("Speech 框架不可用：%s", exc)
            return False
        return True

    def unavailable_reason(self) -> str:
        return "缺少 pyobjc Speech 框架：pip install pyobjc-framework-Speech pyobjc-framework-AVFoundation"

    def authorization_status(self) -> int:
        if not self.is_available():
            return -1
        import Speech  # noqa: PLC0415

        return int(Speech.SFSpeechRecognizer.authorizationStatus())

    # -- 识别 ------------------------------------------------------------- #
    def transcribe(
        self,
        audio_path: str | Path,
        *,
        language: str | None = None,
        on_progress=None,
    ) -> TranscriptionOutput:
        if not self.is_available():
            raise ASREngineUnavailableError(self.unavailable_reason())

        import Speech  # noqa: PLC0415

        status = self.authorization_status()
        if status != AUTH_AUTHORIZED:
            raise ASRError(
                "系统语音识别尚未授权。\n"
                "请打开 LinkWork 图形界面运行一次，在系统弹窗中点击「好」，"
                "再到「系统设置 → 隐私与安全性 → 语音识别」中确认已允许，然后重试；"
                "也可改用默认的 faster-whisper 引擎。",
                hint="在终端中直接运行时无法弹出授权窗口，这是 macOS 的限制。",
            )

        audio_path = Path(audio_path)
        locale = self._resolve_locale(language)
        recognizer = Speech.SFSpeechRecognizer.alloc().initWithLocale_(self._ns_locale(locale))
        if recognizer is None or not recognizer.isAvailable():
            raise ASRError(
                f"系统语音识别当前不可用（locale={locale}）。"
                "请在「系统设置 → 键盘 → 听写」中启用听写功能。"
            )

        chunks = self._split_audio(audio_path)
        all_segments: list[Segment] = []
        offset = 0.0
        for index, (chunk_path, chunk_start, chunk_duration) in enumerate(chunks):
            log.info("系统语音识别第 %d/%d 段…", index + 1, len(chunks))
            text = self._recognize_file(recognizer, chunk_path)
            if text:
                all_segments.append(
                    Segment(start=chunk_start, end=chunk_start + chunk_duration, text=normalize_cjk(text))
                )
            offset = chunk_start + chunk_duration
            if on_progress:
                try:
                    on_progress((index + 1) / max(len(chunks), 1))
                except Exception:  # pragma: no cover
                    pass
            if chunk_path != audio_path and chunk_path.exists():
                try:
                    chunk_path.unlink()
                except OSError:  # pragma: no cover
                    pass

        if not all_segments:
            log.warning("系统语音识别未返回任何文本")

        return TranscriptionOutput(
            segments=all_segments,
            language=locale,
            language_probability=1.0 if all_segments else 0.0,
            duration=offset,
            engine=self.name,
            model=f"Apple Speech ({locale})",
        )

    # -- 内部实现 --------------------------------------------------------- #
    def _resolve_locale(self, language: str | None) -> str:
        if not language:
            return self.locale
        return _LOCALE_MAP.get(language.lower(), language if "-" in language else self.locale)

    def _ns_locale(self, locale: str):
        from Foundation import NSLocale  # noqa: PLC0415

        return NSLocale.localeWithLocaleIdentifier_(locale)

    def _drain_run_loop(self, should_stop, timeout: float) -> bool:
        """等待回调期间驱动当前 run loop，返回是否在超时前完成。

        直接 sleep 等待是不可靠的：Speech 框架的回调需要 run loop 才能派发。
        """
        from Foundation import NSDate, NSDefaultRunLoopMode, NSRunLoop  # noqa: PLC0415

        loop = NSRunLoop.currentRunLoop()
        deadline = time.monotonic() + timeout
        while not should_stop():
            if time.monotonic() > deadline:
                return False
            loop.runMode_beforeDate_(NSDefaultRunLoopMode, NSDate.dateWithTimeIntervalSinceNow_(0.05))
        return True

    def _split_audio(self, audio_path: Path) -> list[tuple[Path, float, float]]:
        """按 CHUNK_SECONDS 切分为多个 wav 片段。"""
        from ..media import media_duration, run_ffmpeg  # noqa: PLC0415

        duration = media_duration(audio_path) or 0.0
        if duration and duration <= CHUNK_SECONDS:
            return [(audio_path, 0.0, duration)]

        tmpdir = Path(tempfile.mkdtemp(prefix="linkwork_apple_"))
        out_pattern = tmpdir / "chunk_%03d.wav"
        run_ffmpeg(
            [
                "-i",
                str(audio_path),
                "-f",
                "segment",
                "-segment_time",
                str(CHUNK_SECONDS),
                "-ac",
                "1",
                "-ar",
                "16000",
                str(out_pattern),
            ],
            timeout=1800,
        )
        chunks: list[tuple[Path, float, float]] = []
        for idx, path in enumerate(sorted(tmpdir.glob("chunk_*.wav"))):
            start = idx * CHUNK_SECONDS
            length = min(CHUNK_SECONDS, max(duration - start, 1.0)) if duration else CHUNK_SECONDS
            chunks.append((path, float(start), float(length)))
        if not chunks:  # pragma: no cover
            return [(audio_path, 0.0, duration)]
        return chunks

    def _recognize_file(self, recognizer, path: Path) -> str:
        import Speech  # noqa: PLC0415
        from Foundation import NSURL  # noqa: PLC0415

        url = NSURL.fileURLWithPath_(str(path))
        request = Speech.SFSpeechURLRecognitionRequest.alloc().initWithURL_(url)
        try:
            request.setShouldReportPartialResults_(False)
        except Exception:  # pragma: no cover
            pass
        if self.on_device:
            try:
                if recognizer.supportsOnDeviceRecognition():
                    request.setRequiresOnDeviceRecognition_(True)
            except Exception:  # pragma: no cover
                pass

        state: dict[str, object] = {"text": "", "error": None, "final": False}

        def handler(result, error):
            if error is not None:
                state["error"] = error
                state["final"] = True
                return
            if result is not None:
                transcription = result.bestTranscription()
                if transcription is not None:
                    state["text"] = str(transcription.formattedString() or "")
                if result.isFinal():
                    state["final"] = True

        task = recognizer.recognitionTaskWithRequest_resultHandler_(request, handler)
        finished = self._drain_run_loop(lambda: bool(state["final"]), self.timeout_per_chunk)
        if not finished:
            try:
                task.cancel()
            except Exception:  # pragma: no cover
                pass
            raise ASRError(
                f"系统语音识别超时（>{int(self.timeout_per_chunk)}s）。"
                "可尝试缩短音频、改用 faster-whisper 引擎，或检查网络（系统识别可能依赖在线模型）。"
            )
        if state["error"] is not None:
            raise ASRError(f"系统语音识别失败：{state['error']}")
        return str(state["text"] or "").strip()
