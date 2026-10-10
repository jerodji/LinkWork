"""语音识别引擎层。

支持的引擎：
- ``faster-whisper``（默认，PyPI 可装，CPU 即可跑，首次运行自动下载模型）
- ``apple``（macOS 系统 Speech 框架，零模型下载，需 pyobjc-framework-Speech）
- ``none``（只取字幕，不做语音识别）
"""

from __future__ import annotations

import logging

from ..errors import ASREngineUnavailableError
from ..models import Segment
from .base import ASREngine, TranscriptionOutput

log = logging.getLogger(__name__)

__all__ = ["ASREngine", "TranscriptionOutput", "Segment", "get_engine", "available_engines"]


def get_engine(name: str = "auto", **kwargs) -> ASREngine:
    """按名称创建引擎；``auto`` 会选当前环境可用的最优引擎。"""
    name = (name or "auto").lower()
    if name in ("none", "off", "skip"):
        from .null import NullEngine

        return NullEngine()

    if name in ("auto", "faster-whisper", "whisper", "fw"):
        from .faster_whisper_engine import FasterWhisperEngine

        engine = FasterWhisperEngine(**kwargs)
        if engine.is_available() or name not in ("auto",):
            return engine
        log.info("faster-whisper 不可用，尝试其他引擎")
        return get_engine("apple", **kwargs) if _apple_available() else NullEngine()

    if name in ("apple", "macos", "system"):
        from .apple_speech import AppleSpeechEngine

        return AppleSpeechEngine(**kwargs)

    raise ASREngineUnavailableError(f"未知的语音识别引擎：{name}")


def available_engines() -> list[dict[str, object]]:
    """列出各引擎可用性，供 GUI/CLI 展示。"""
    out: list[dict[str, object]] = []
    for name in ("faster-whisper", "apple"):
        try:
            engine = get_engine(name)
            ok = engine.is_available()
            detail = engine.unavailable_reason() if not ok else ""
        except Exception as exc:  # noqa: BLE001
            ok, detail = False, str(exc)
        out.append(
            {
                "name": name,
                "available": ok,
                "detail": detail,
                "default_model": getattr(engine, "model_size", "") if ok else "",
            }
        )
    return out


def _apple_available() -> bool:
    try:
        from .apple_speech import AppleSpeechEngine

        return AppleSpeechEngine().is_available()
    except Exception:  # noqa: BLE001
        return False
