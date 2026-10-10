"""faster-whisper 引擎实现（默认引擎，纯 CPU 可用）。"""

from __future__ import annotations

import logging
import os
import threading
import time
from pathlib import Path

from ..errors import ASREngineUnavailableError, ASRError
from ..media import media_duration
from ..models import Segment
from .base import TranscriptionOutput

log = logging.getLogger(__name__)

DEFAULT_MODEL = os.environ.get("LINKWORK_WHISPER_MODEL", "small")

_ENGINE_CACHE: dict[str, object] = {}


class FasterWhisperEngine:
    """基于 CTranslate2 的 Whisper 实现。"""

    name = "faster-whisper"

    def __init__(
        self,
        model_size: str = DEFAULT_MODEL,
        device: str = "auto",
        compute_type: str = "auto",
        download_root: str | Path | None = None,
        beam_size: int = 5,
        vad_filter: bool = True,
        cpu_threads: int | None = None,
    ) -> None:
        self.model_size = model_size or DEFAULT_MODEL
        self.device = device
        self.compute_type = compute_type
        self.download_root = str(download_root) if download_root else None
        self.beam_size = beam_size
        self.vad_filter = vad_filter
        self.cpu_threads = cpu_threads or max(2, (os.cpu_count() or 4) // 2)
        self._model = None

    # -- 可用性 ----------------------------------------------------------- #
    def is_available(self) -> bool:
        try:
            import faster_whisper  # noqa: F401, PLC0415
        except Exception as exc:  # noqa: BLE001
            log.debug("faster-whisper 导入失败：%s", exc)
            return False
        return True

    def unavailable_reason(self) -> str:
        return "未安装 faster-whisper，请运行 scripts/setup.sh 或 pip install faster-whisper"

    # -- 模型 ------------------------------------------------------------- #
    def _resolve_device(self) -> tuple[str, str]:
        device = self.device
        compute = self.compute_type
        if device == "auto":
            device = "cpu"
            # Apple Silicon 上 CPU + int8 是性价比最高的选择
        if compute == "auto":
            compute = "int8" if device == "cpu" else "float16"
        return device, compute

    def load(self):
        if self._model is not None:
            return self._model
        if not self.is_available():
            raise ASREngineUnavailableError(self.unavailable_reason())

        # 关键：在导入/下载模型之前确定可用的模型端点（国内网络自动走镜像）
        from ..net import prepare_asr_download  # noqa: PLC0415

        prepare_asr_download()

        # 规避 faster-whisper 与新版 PyAV 的参数不兼容（metadata_errors）
        from ..audio_decode import install_decoder_patch  # noqa: PLC0415

        install_decoder_patch()

        from faster_whisper import WhisperModel  # noqa: PLC0415

        device, compute = self._resolve_device()
        cache_key = f"{self.model_size}|{device}|{compute}|{self.download_root}"
        if cache_key in _ENGINE_CACHE:
            self._model = _ENGINE_CACHE[cache_key]
            return self._model

        attempts = [(device, compute)]
        if compute != "float32":
            attempts.append((device, "float32"))

        last_error: Exception | None = None
        for dev, comp in attempts:
            try:
                log.info(
                    "加载 faster-whisper 模型 %s（device=%s, compute_type=%s），首次使用会下载模型…",
                    self.model_size,
                    dev,
                    comp,
                )
                model = WhisperModel(
                    self.model_size,
                    device=dev,
                    compute_type=comp,
                    download_root=self.download_root,
                    cpu_threads=self.cpu_threads,
                )
                _ENGINE_CACHE[cache_key] = model
                self._model = model
                self._device_used = (dev, comp)
                return model
            except Exception as exc:  # noqa: BLE001
                last_error = exc
                log.warning("以 %s/%s 加载模型失败：%s", dev, comp, exc)
        raise ASRError(f"加载语音识别模型失败：{last_error}")

    # -- 识别 ------------------------------------------------------------- #
    def transcribe(
        self,
        audio_path: str | Path,
        *,
        language: str | None = None,
        on_progress=None,
    ) -> TranscriptionOutput:
        audio_path = Path(audio_path)
        if not audio_path.exists():
            raise ASRError(f"音频文件不存在：{audio_path}")

        model = self.load()
        duration = media_duration(audio_path) or 0.0

        return self._transcribe_locked(model, audio_path, language, duration, on_progress)

    def _transcribe_locked(
        self,
        model,
        audio_path: Path,
        language: str | None,
        duration: float,
        on_progress,
    ) -> TranscriptionOutput:
        log.info("开始语音识别：%s（时长约 %.1fs）", audio_path.name, duration)

        try:
            segments, info = self._run(model, audio_path, language, duration, self.vad_filter, on_progress)
        except Exception as exc:  # noqa: BLE001
            raise ASRError(f"语音识别失败：{exc}") from exc

        # VAD 在「音乐/人声混合」的音轨上可能把全部音频判为非语音（例如 MV、BGM 口播），
        # 表现为 VAD 过滤掉整段音频。此时自动关闭 VAD 重试。
        if self.vad_filter and duration > 5 and self._speech_ratio(segments, duration) < 0.15:
            log.warning(
                "VAD 过滤后仅覆盖 %.0f%% 音频，可能是音乐类音轨，改为不使用 VAD 重试…",
                self._speech_ratio(segments, duration) * 100,
            )
            try:
                retry_segments, retry_info = self._run(model, audio_path, language, duration, False, on_progress)
            except Exception as exc:  # noqa: BLE001
                log.warning("关闭 VAD 重试失败：%s", exc)
            else:
                if len(retry_segments) > len(segments):
                    segments, info = retry_segments, retry_info
                    log.info("关闭 VAD 后识别到 %d 段内容", len(segments))

        if on_progress:
            try:
                on_progress(1.0)
            except Exception:  # pragma: no cover
                pass

        if not segments:
            log.warning("未识别到任何语音内容")

        detected_lang = getattr(info, "language", "") or (language or "")
        prob = float(getattr(info, "language_probability", 0.0) or 0.0)
        dev, comp = getattr(self, "_device_used", self._resolve_device())
        return TranscriptionOutput(
            segments=segments,
            language=detected_lang,
            language_probability=prob,
            duration=float(getattr(info, "duration", 0.0) or duration or 0.0),
            engine=self.name,
            model=f"{self.model_size} ({dev}/{comp})",
        )

    def _run(
        self,
        model,
        audio_path: Path,
        language: str | None,
        duration: float,
        vad_filter: bool,
        on_progress,
    ) -> tuple[list[Segment], object]:
        """执行一次识别，返回 (分段列表, info)。"""
        from ..textnorm import normalize_cjk  # noqa: PLC0415

        segments_iter, info = model.transcribe(
            str(audio_path),
            language=language or None,
            beam_size=self.beam_size,
            vad_filter=vad_filter,
            vad_parameters={"min_silence_duration_ms": 500} if vad_filter else None,
            condition_on_previous_text=False,
            word_timestamps=False,
        )

        # 看门狗：正常识别时分段会持续产出；若长时间没有任何新分段，
        # 说明原生层可能卡死（ctranslate2 偶发），主动中断而不是无限等待。
        stall_budget = max(120.0, duration * 3) if duration else 300.0
        last_progress = time.monotonic()

        segments: list[Segment] = []
        for seg in segments_iter:
            now = time.monotonic()
            # 注意：只有在"拿到新分段"时才会走到这里；若生成器本身被原生层卡住，
            # 本循环根本不会执行，所以看门狗只能覆盖"每个分段之间耗时过长"的情况。
            stalled = now - last_progress
            if stalled > stall_budget:
                raise ASRError(
                    f"语音识别停滞超过 {int(stall_budget)}s，已中断（可能遇到解码死锁）。"
                    "请重启本地服务后重试：./scripts/service.sh restart"
                )
            last_progress = now
            text = normalize_cjk((seg.text or "").strip())
            if not text:
                continue
            segments.append(Segment(start=float(seg.start), end=float(seg.end), text=text))
            if on_progress and duration:
                try:
                    on_progress(min(float(seg.end) / duration, 1.0) * (0.95 if vad_filter else 1.0))
                except Exception:  # pragma: no cover
                    pass
        return segments, info

    @staticmethod
    def _speech_ratio(segments: list[Segment], duration: float) -> float:
        """识别内容覆盖的时长比例。"""
        if not segments or duration <= 0:
            return 0.0
        return min(segments[-1].end / duration, 1.0)
