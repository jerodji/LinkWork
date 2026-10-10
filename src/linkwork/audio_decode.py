"""音频解码兼容层。

faster-whisper 默认用 PyAV (``av.open(..., metadata_errors="ignore")``) 解码音频，
但 PyAV 19 移除了该参数，导致 ``TypeError: open() got an unexpected keyword argument``。

不过本项目在送入识别前，已经用 ffmpeg 把音频统一转成 16 kHz 单声道 PCM WAV，
因此这里提供一个基于标准库 ``wave`` 的解码实现：
- 不依赖 av，读取更快；
- 通过 ``install_decoder_patch()`` 挂载到 faster_whisper.audio，消除版本冲突。
"""

from __future__ import annotations

import logging
import wave
from pathlib import Path

import numpy as np

log = logging.getLogger(__name__)


def wav_to_float32(path: str | Path, sampling_rate: int = 16000) -> np.ndarray:
    """读取 16-bit PCM WAV，返回 [-1, 1] 的 float32 单声道数组。"""
    with wave.open(str(path), "rb") as handle:
        channels = handle.getnchannels()
        width = handle.getsampwidth()
        rate = handle.getframerate()
        frames = handle.readframes(handle.getnframes())

    if width != 2:
        raise ValueError(f"仅支持 16-bit PCM WAV（当前 {width * 8}-bit）")
    audio = np.frombuffer(frames, dtype=np.int16).astype(np.float32) / 32768.0
    if channels > 1:
        audio = audio.reshape(-1, channels).mean(axis=1)
    if rate != sampling_rate:
        audio = _resample(audio, rate, sampling_rate)
    return np.ascontiguousarray(audio, dtype=np.float32)


def _resample(audio: np.ndarray, source_rate: int, target_rate: int) -> np.ndarray:
    """线性插值重采样（仅作兜底，正常流程已由 ffmpeg 转成 16 kHz）。"""
    if source_rate == target_rate or audio.size == 0:
        return audio
    duration = audio.size / float(source_rate)
    target_size = int(round(duration * target_rate))
    if target_size <= 0:
        return audio
    source_index = np.linspace(0.0, audio.size - 1, num=target_size)
    return np.interp(source_index, np.arange(audio.size), audio).astype(np.float32)


def is_wav_file(path: str | Path) -> bool:
    try:
        with wave.open(str(path), "rb"):
            return True
    except Exception:  # noqa: BLE001
        return False


def pyav_incompatible() -> bool:
    """检测 PyAV 与 faster-whisper 是否版本不兼容。"""
    try:
        import av  # noqa: PLC0415
    except Exception:  # noqa: BLE001
        return True
    try:
        import inspect  # noqa: PLC0415

        sig = inspect.signature(av.open)
        return "metadata_errors" not in sig.parameters
    except (ValueError, TypeError):
        # 内置函数无法取签名（正是 PyAV 19 的情况）
        return True
    except Exception:  # noqa: BLE001
        return False


def install_decoder_patch() -> bool:
    """让 faster_whisper 使用本模块的 WAV 解码器。返回是否完成替换。

    注意：``faster_whisper.transcribe`` 里是 ``from .audio import decode_audio``，
    模块内持有独立引用，因此两个命名空间都要替换。
    """
    try:
        import faster_whisper.audio as fw_audio  # noqa: PLC0415
    except Exception as exc:  # noqa: BLE001
        log.debug("无法导入 faster_whisper.audio：%s", exc)
        return False

    if getattr(fw_audio, "_linkwork_patched", False):
        return True

    original = getattr(fw_audio, "decode_audio", None)

    def decode_audio(input_file, sampling_rate: int = 16000, split_stereo: bool = False):
        if isinstance(input_file, (str, Path)) and is_wav_file(input_file):
            try:
                audio = wav_to_float32(input_file, sampling_rate)
                if split_stereo:
                    return audio, audio
                return audio
            except Exception as exc:  # noqa: BLE001
                log.debug("内置 WAV 解码失败，回退 PyAV：%s", exc)
        if original is None:  # pragma: no cover
            raise RuntimeError("无可用的音频解码器")
        return original(input_file, sampling_rate=sampling_rate, split_stereo=split_stereo)

    fw_audio.decode_audio = decode_audio
    fw_audio._linkwork_patched = True

    # transcribe 模块内的独立引用同样需要替换
    try:
        import faster_whisper.transcribe as fw_transcribe  # noqa: PLC0415

        if getattr(fw_transcribe, "decode_audio", None) is not decode_audio:
            fw_transcribe.decode_audio = decode_audio
    except Exception as exc:  # noqa: BLE001
        log.debug("无法替换 transcribe.decode_audio：%s", exc)

    log.debug("已启用内置 WAV 解码器（绕过 PyAV 兼容问题）")
    return True


def preflight(patch: bool = True) -> dict[str, object]:
    """返回音频解码环境信息，供 doctor 展示。"""
    info: dict[str, object] = {"pyav_incompatible": pyav_incompatible()}
    try:
        import av  # noqa: PLC0415

        info["pyav_version"] = getattr(av, "__version__", "?")
    except Exception:  # noqa: BLE001
        info["pyav_version"] = ""
    info["patched"] = install_decoder_patch() if patch else False
    return info
