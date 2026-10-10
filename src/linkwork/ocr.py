"""系统级图片文字识别（macOS Vision 框架，中文支持良好）。

用于小红书等「图文笔记」：把图片里的文字提取出来。
依赖：pip install pyobjc-framework-Vision pyobjc-framework-Quartz
"""

from __future__ import annotations

import logging
from pathlib import Path

log = logging.getLogger(__name__)


def ocr_available() -> bool:
    try:
        import Quartz  # noqa: F401, PLC0415
        import Vision  # noqa: F401, PLC0415

        return True
    except Exception as exc:  # noqa: BLE001
        log.debug("Vision OCR 不可用：%s", exc)
        return False


def unavailable_reason() -> str:
    return "缺少 pyobjc Vision 框架：pip install pyobjc-framework-Vision pyobjc-framework-Quartz"


def recognize_image_text(path: str | Path, languages: tuple[str, ...] = ("zh-Hans", "zh-Hant", "en-US")) -> str:
    """识别单张图片中的文字。"""
    if not ocr_available():
        raise RuntimeError(unavailable_reason())

    import Quartz  # noqa: PLC0415
    import Vision  # noqa: PLC0415
    from Foundation import NSURL  # noqa: PLC0415

    url = NSURL.fileURLWithPath_(str(path))
    source = Quartz.CGImageSourceCreateWithURL(url, None)
    if source is None:
        log.warning("无法读取图片：%s", path)
        return ""
    image = Quartz.CGImageSourceCreateImageAtIndex(source, 0, None)
    if image is None:
        return ""

    # 实测：Vision 的 accurate 模式（level=1）对中文常返回空结果，
    # 而 fast 模式（level=0）能正确识别中文。因此先用 fast 识别，
    # 结果过少时再用 accurate 复跑一次取更完整的文本。
    fast = _run_recognition(image, level=0, languages=languages, correct=True)
    if len(fast.replace("\n", "").strip()) >= 4:
        return fast

    accurate = _run_recognition(image, level=1, languages=languages, correct=True)
    if len(accurate.replace("\n", "").strip()) > len(fast.replace("\n", "").strip()):
        if fast.strip():
            # 两者都有内容时合并，保留各自的识别行
            merged = fast.rstrip() + "\n" + accurate.lstrip()
            return merged.strip()
        return accurate
    return fast


def _run_recognition(image, *, level: int, languages: tuple[str, ...], correct: bool) -> str:
    """执行一次 Vision 文字识别，返回按行拼接的文本。"""
    import Vision  # noqa: PLC0415

    request = Vision.VNRecognizeTextRequest.alloc().init()
    request.setRecognitionLevel_(level)
    request.setUsesLanguageCorrection_(correct)
    try:
        if languages:
            request.setRecognitionLanguages_(list(languages))
    except Exception:  # pragma: no cover
        pass

    handler = Vision.VNImageRequestHandler.alloc().initWithCGImage_options_(image, None)
    try:
        success, error = handler.performRequests_error_([request], None)
    except Exception as exc:  # noqa: BLE001
        log.debug("OCR 请求异常（level=%s）：%s", level, exc)
        return ""
    if not success:
        log.debug("OCR 失败（level=%s）：%s", level, error)
        return ""

    lines: list[str] = []
    for observation in request.results() or []:
        candidates = observation.topCandidates_(1)
        if candidates and len(candidates) > 0:
            text = str(candidates[0].string() or "").strip()
            if text:
                lines.append(text)
    return "\n".join(lines)


def recognize_images(paths: list[str | Path]) -> tuple[str, list[str]]:
    """批量识别图片，返回 (合并文本, 失败列表)。"""
    texts: list[str] = []
    failed: list[str] = []
    for path in paths:
        try:
            text = recognize_image_text(path)
            if text:
                texts.append(text)
        except Exception as exc:  # noqa: BLE001
            log.debug("图片 OCR 失败 %s：%s", path, exc)
            failed.append(str(path))
    return "\n\n".join(texts), failed
