"""结果导出：TXT / Markdown / JSON / SRT。"""

from __future__ import annotations

import json
import logging
from datetime import datetime
from pathlib import Path

from .models import ExtractResult
from .utils import sanitize_filename, unique_path

log = logging.getLogger(__name__)

_BAR = "=" * 60


def default_stem(result: ExtractResult) -> str:
    base = result.title or result.platform or "linkwork"
    return sanitize_filename(f"{base}", fallback="linkwork")


def build_markdown(result: ExtractResult) -> str:
    lines: list[str] = []
    title = result.title or "未命名"
    lines.append(f"# {title}")
    lines.append("")
    meta_rows = [
        ("来源链接", result.url),
        ("平台", result.platform or "-"),
        ("类型", _kind_label(result.kind)),
        ("提取方式", _method_label(result.method)),
        ("作者", result.author or "-"),
    ]
    if result.duration:
        lines.append(f"> 时长：{_format_duration(result.duration)}")
    if result.engine:
        lines.append(f"> 识别引擎：{result.engine}")
    lines.append("")
    lines.append("| 项目 | 内容 |")
    lines.append("| --- | --- |")
    for key, value in meta_rows:
        lines.append(f"| {key} | {value} |")
    lines.append("")
    lines.append(f"> 字数：{result.char_count}")
    lines.append("")

    if result.segments:
        lines.append("## 口播稿")
        lines.append("")
        lines.append(result.spoken_script)
        lines.append("")
        lines.append("## 带时间戳原文")
        lines.append("")
        for seg in result.segments:
            lines.append(f"- `[{_format_duration(seg.start)}]` {seg.text}")
        lines.append("")
    else:
        lines.append("## 文案正文")
        lines.append("")
        lines.append(result.text)
        lines.append("")
        extra_desc = result.extra.get("description")
        if extra_desc and extra_desc not in result.text:
            lines.append("## 简介")
            lines.append("")
            lines.append(str(extra_desc))
            lines.append("")
        tags = result.extra.get("tags")
        if tags:
            lines.append("## 话题标签")
            lines.append("")
            lines.append(" ".join(f"#{t}#" for t in tags))
            lines.append("")

    if result.warnings:
        lines.append("## 提示")
        lines.append("")
        for warning in result.warnings:
            lines.append(f"- {warning}")
        lines.append("")

    lines.append("---")
    lines.append(f"由 LinkWork 生成于 {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    return "\n".join(lines)


def build_text(result: ExtractResult) -> str:
    lines: list[str] = []
    lines.append(_BAR)
    if result.title:
        lines.append(f"标题：{result.title}")
    lines.append(f"链接：{result.url}")
    lines.append(f"平台：{result.platform or '-'}    类型：{_kind_label(result.kind)}    方式：{_method_label(result.method)}")
    if result.author:
        lines.append(f"作者：{result.author}")
    if result.duration:
        lines.append(f"时长：{_format_duration(result.duration)}")
    if result.engine:
        lines.append(f"引擎：{result.engine}")
    lines.append(f"字数：{result.char_count}")
    lines.append(_BAR)
    lines.append("")
    lines.append(result.spoken_script if result.segments else result.text)
    extra_desc = result.extra.get("description")
    if extra_desc and extra_desc not in result.text and not result.segments:
        lines.append("")
        lines.append("【简介】")
        lines.append(str(extra_desc))
    tags = result.extra.get("tags")
    if tags:
        lines.append("")
        lines.append("【话题】" + " ".join(f"#{t}#" for t in tags))
    if result.warnings:
        lines.append("")
        lines.append("【提示】")
        for warning in result.warnings:
            lines.append(f"- {warning}")
    return "\n".join(lines)


def build_json(result: ExtractResult) -> str:
    return json.dumps(result.to_dict(), ensure_ascii=False, indent=2)


def _format_srt_time(seconds: float) -> str:
    total_ms = int(round(seconds * 1000))
    hours, remainder = divmod(total_ms, 3600_000)
    minutes, remainder = divmod(remainder, 60_000)
    secs, millis = divmod(remainder, 1000)
    return f"{hours:02d}:{minutes:02d}:{secs:02d},{millis:03d}"


def build_srt(result: ExtractResult) -> str:
    blocks: list[str] = []
    for index, seg in enumerate(result.segments, start=1):
        blocks.append(str(index))
        blocks.append(f"{_format_srt_time(seg.start)} --> {_format_srt_time(seg.end)}")
        blocks.append(seg.text)
        blocks.append("")
    return "\n".join(blocks)


def save_result(
    result: ExtractResult,
    out_dir: str | Path,
    formats: tuple[str, ...] = ("md", "txt"),
    stem: str | None = None,
) -> dict[str, Path]:
    """按指定格式保存结果，返回 {格式: 路径}。"""
    out_dir = Path(out_dir).expanduser()
    out_dir.mkdir(parents=True, exist_ok=True)
    stem = stem or default_stem(result)
    written: dict[str, Path] = {}
    for fmt in formats:
        fmt = fmt.lower().lstrip(".")
        if fmt == "md":
            path = unique_path(out_dir, stem, ".md")
            path.write_text(build_markdown(result), encoding="utf-8")
        elif fmt in ("txt", "text"):
            path = unique_path(out_dir, stem, ".txt")
            path.write_text(build_text(result), encoding="utf-8")
        elif fmt == "json":
            path = unique_path(out_dir, stem, ".json")
            path.write_text(build_json(result), encoding="utf-8")
        elif fmt == "srt":
            if not result.segments:
                log.debug("无时间戳分段，跳过 SRT 导出")
                continue
            path = unique_path(out_dir, stem, ".srt")
            path.write_text(build_srt(result), encoding="utf-8")
        else:
            log.warning("未知导出格式：%s", fmt)
            continue
        written[fmt] = path
    return written


def _kind_label(kind: str) -> str:
    return {
        "video": "视频/口播",
        "audio": "音频",
        "article": "图文/文章",
        "image_text": "图文笔记",
        "text": "粘贴文案",
    }.get(kind, kind)


def _method_label(method: str) -> str:
    return {
        "asr": "语音识别",
        "subtitle": "平台字幕",
        "html": "网页正文",
        "metadata": "页面文案",
        "manual": "手动粘贴",
    }.get(method, method)


def _format_duration(seconds: float | None) -> str:
    if seconds is None:
        return "未知"
    seconds = int(seconds)
    hours, remainder = divmod(seconds, 3600)
    minutes, secs = divmod(remainder, 60)
    if hours:
        return f"{hours}:{minutes:02d}:{secs:02d}"
    return f"{minutes:02d}:{secs:02d}"
