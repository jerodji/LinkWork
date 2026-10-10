"""数据模型：提取结果与内部结构。"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Literal

SourceKind = Literal["video", "audio", "article", "image_text", "text"]
Method = Literal["asr", "subtitle", "html", "metadata", "manual"]


@dataclass
class Segment:
    """一段带时间戳的文案（来自字幕或语音识别）。"""

    start: float
    end: float
    text: str

    def to_dict(self) -> dict[str, Any]:
        return {"start": round(self.start, 2), "end": round(self.end, 2), "text": self.text}


@dataclass
class ExtractResult:
    """一次链接提取的完整结果。"""

    url: str
    kind: SourceKind
    method: Method
    title: str = ""
    author: str = ""
    platform: str = ""
    #: 提取到的正文/口播稿
    text: str = ""
    #: ASR 得到的分段（带时间戳），网页类为空
    segments: list[Segment] = field(default_factory=list)
    duration: float | None = None
    #: 网页类链接的附加上下文（如动态文案、简介、话题标签）
    extra: dict[str, Any] = field(default_factory=dict)
    #: 中间产物路径（音频、字幕文件等）
    artifacts: dict[str, str] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)
    engine: str = ""

    @property
    def spoken_script(self) -> str:
        """口播稿：优先使用分段结果重建的自然段落。"""
        if self.segments:
            return join_segments(self.segments)
        return self.text

    @property
    def char_count(self) -> int:
        """字数：中文按字计，同时忽略空白（英文句子也会得到非零计数）。"""
        source = self.text or ""
        if not source and self.segments:
            source = "".join(seg.text for seg in self.segments)
        return len(re.sub(r"\s+", "", source))

    @property
    def word_count(self) -> int:
        return len(self.text.split())

    def to_dict(self) -> dict[str, Any]:
        return {
            "url": self.url,
            "kind": self.kind,
            "method": self.method,
            "platform": self.platform,
            "title": self.title,
            "author": self.author,
            "engine": self.engine,
            "duration": round(self.duration, 1) if self.duration else None,
            "char_count": self.char_count,
            "text": self.text,
            "segments": [s.to_dict() for s in self.segments],
            "extra": self.extra,
            "artifacts": self.artifacts,
            "warnings": self.warnings,
        }


#: 句末标点，用于把 ASR 碎片拼接成自然段落
_SENTENCE_END = "。！？!?…；;"
_CJK_RE = re.compile(r"[\u4e00-\u9fff\u3400-\u4dbf\u3040-\u30ff\uac00-\ud7af]")


def _is_cjk_dominant(text: str) -> bool:
    """文本是否以中日韩字符为主（决定拼接时是否加空格）。"""
    if not text:
        return True
    cjk = len(_CJK_RE.findall(text))
    return cjk >= max(1, len(text) * 0.15)


def _ends_sentence(text: str) -> bool:
    return bool(text) and text[-1] in _SENTENCE_END


def join_segments(segments: list[Segment], max_chars_per_para: int = 90) -> str:
    """把带时间戳的分段拼接成可读的口播稿。

    - 中文（CJK）按停顿与长度断句并补全角句号；
    - 英文等拉丁文本用空格拼接，并在明显句读处断行；
    - 长停顿（> 0.9s）换行分段。
    """
    if not segments:
        return ""

    body = " ".join(seg.text.strip() for seg in segments if seg.text and seg.text.strip())
    latin = not _is_cjk_dominant(body)

    out_lines: list[str] = []
    buf = ""
    for index, seg in enumerate(segments):
        text = (seg.text or "").strip()
        if not text:
            continue
        if buf:
            if latin:
                buf += " "
            elif _ends_sentence(buf):
                out_lines.append(buf.strip())
                buf = ""
        buf += text
        nxt = segments[index + 1] if index + 1 < len(segments) else None
        gap = (nxt.start - seg.end) if nxt else 99.0
        long_pause = gap >= 0.9
        too_long = len(buf) >= max_chars_per_para
        last = index == len(segments) - 1
        # 拉丁文本：本句已收尾且下一句以大写字母开头时断行
        boundary = latin and bool(nxt) and _ends_sentence(buf) and (nxt.text or "")[:1].isupper()
        if long_pause or too_long or boundary or last:
            if buf.strip():
                out_lines.append(buf.strip())
            buf = ""

    text = "\n".join(line for line in out_lines if line)
    return text if latin else _punctuate(text, max_chars_per_para)


def _punctuate(text: str, chunk: int) -> str:
    """对没有标点的长句做适度断句，便于阅读。"""
    if not text or any(ch in text for ch in "。！？!?"):
        return text
    if all(ord(ch) < 128 or ch.isspace() for ch in text):
        # 英文等拉丁文本：不强行处理
        return text
    lines = []
    for line in text.split("\n"):
        if len(line) <= chunk:
            lines.append(line + "。" if line and not line.endswith("。") else line)
            continue
        start = 0
        while start < len(line):
            piece = line[start : start + chunk]
            lines.append(piece + "。" if not piece.endswith("。") else piece)
            start += chunk
    return "\n".join(lines)
