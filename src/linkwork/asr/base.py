"""ASR 引擎接口定义。"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol

from ..models import Segment


@dataclass
class TranscriptionOutput:
    """一次语音识别的结果。"""

    segments: list[Segment] = field(default_factory=list)
    language: str = ""
    language_probability: float = 0.0
    duration: float = 0.0
    engine: str = ""
    model: str = ""

    @property
    def text(self) -> str:
        from ..models import join_segments

        return join_segments(self.segments)


class ASREngine(Protocol):
    """语音识别引擎协议。"""

    name: str
    model_size: str

    def is_available(self) -> bool: ...

    def unavailable_reason(self) -> str: ...

    def transcribe(
        self,
        audio_path: str | Path,
        *,
        language: str | None = None,
        on_progress=None,
    ) -> TranscriptionOutput: ...
