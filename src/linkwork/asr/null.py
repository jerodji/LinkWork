"""空引擎：只使用平台字幕/网页文本，不做语音识别。"""

from __future__ import annotations

from pathlib import Path

from ..errors import ASRError
from .base import TranscriptionOutput


class NullEngine:
    name = "none"
    model_size = ""

    def is_available(self) -> bool:
        return True

    def unavailable_reason(self) -> str:
        return ""

    def transcribe(self, audio_path: str | Path, *, language: str | None = None, on_progress=None) -> TranscriptionOutput:
        raise ASRError("当前为「仅字幕/仅网页」模式，未启用语音识别。")
