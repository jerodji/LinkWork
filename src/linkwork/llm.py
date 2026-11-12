"""文案校准：调用智谱 GLM-4.5-Flash 修正错别字、标点与分段。

只做最小改动，不改写、不润色、不增删内容。模型不可用时由调用方降级为原文。
"""

from __future__ import annotations

import logging
import os
import re
import time

log = logging.getLogger(__name__)

DEFAULT_API_URL = "https://open.bigmodel.cn/api/paas/v4/chat/completions"
DEFAULT_MODEL = "glm-4.5-flash"

#: 单次请求的文本上限（字符），超长文本按段落/句子切分
MAX_CHUNK_CHARS = 4000
#: 少于该字数不做校准（没有意义且浪费请求）
MIN_CHARS = 10
#: 可重试的业务错误码（模型繁忙/限流）
RETRYABLE_CODES = {1302, 1303, 1305}
MAX_ATTEMPTS = 4

SYSTEM_PROMPT = (
    "你是一名严格的中文文本校对员。你的唯一任务是校对用户给出的文本，"
    "并且只允许做以下三件事：\n"
    "1. 修正错别字、同音字、形近字等明显的录入错误；\n"
    "2. 修正标点符号（中英文标点混用、缺失或用错）；\n"
    "3. 在不改变语义和语序的前提下，按语义合理分段（用换行分隔）。\n"
    "绝对禁止：增删、改写、润色、概括或扩写任何句子；调整语序；替换同义词；"
    "改变口语风格或语气；修改数字、人名、地名、专有名词、链接和代码；"
    "删除或改写原文中的标题符号（如 #）、列表符号、括号标记（如【】）、"
    "来源标注等结构标记；添加标题、解释、注释、Markdown 标记或任何额外内容。\n"
    "无法确定时保持原样。只输出校对后的文本本身，不要输出任何说明。"
)


class CalibrationError(Exception):
    """文案校准失败（网络、鉴权、模型繁忙或返回异常）。"""


def calibrate_text(
    text: str,
    *,
    api_key: str | None = None,
    model: str | None = None,
    timeout: float | None = None,
) -> str:
    """校准文案，返回修正后的文本。

    Args:
        text: 待校准的完整文案。
        api_key: 智谱 API Key，默认读取环境变量 LINKWORK_ZHIPU_API_KEY / ZHIPU_API_KEY。
        model: 模型名，默认 glm-4.5-flash。
        timeout: 单次请求超时秒数。

    Raises:
        CalibrationError: 校准失败，调用方应降级使用原文。
    """
    body = (text or "").strip()
    if len(re.sub(r"\s+", "", body)) < MIN_CHARS:
        return text

    key = (api_key or _api_key()).strip()
    if not key:
        raise CalibrationError("未配置智谱 API Key（请在扩展设置中填写，或设置 LINKWORK_ZHIPU_API_KEY）")

    model = (model or os.environ.get("LINKWORK_LLM_MODEL", DEFAULT_MODEL)).strip() or DEFAULT_MODEL
    timeout = timeout or _timeout()

    fixed_chunks: list[str] = []
    for chunk in _split_chunks(body, MAX_CHUNK_CHARS):
        fixed_chunks.append(_calibrate_chunk(chunk, key=key, model=model, timeout=timeout))
    return "\n".join(fixed_chunks)


def _calibrate_chunk(chunk: str, *, key: str, model: str, timeout: float) -> str:
    import requests  # noqa: PLC0415

    source_len = len(re.sub(r"\s+", "", chunk))
    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": chunk},
        ],
        "temperature": 0.1,
        "top_p": 0.1,
        "max_tokens": max(2048, min(65536, source_len * 2 + 1024)),
        # 校对不需要深度思考，关闭后响应更快、更稳定
        "thinking": {"type": "disabled"},
    }
    headers = {"Authorization": f"Bearer {key}", "Content-Type": "application/json"}

    last_error = ""
    for attempt in range(1, MAX_ATTEMPTS + 1):
        try:
            resp = requests.post(DEFAULT_API_URL, json=payload, headers=headers, timeout=timeout)
        except Exception as exc:  # noqa: BLE001
            last_error = f"网络请求失败：{exc}"
        else:
            if resp.status_code == 200:
                content = _extract_content(resp)
                if content:
                    cleaned = _clean_output(content, chunk)
                    if cleaned:
                        return cleaned
                last_error = "模型返回内容为空"
            else:
                detail = _error_detail(resp)
                code = detail[0]
                retryable = resp.status_code == 429 or resp.status_code >= 500 or code in RETRYABLE_CODES
                if not retryable:
                    raise CalibrationError(f"校准请求失败：HTTP {resp.status_code}{detail[1]}")
                last_error = f"模型繁忙（HTTP {resp.status_code}{detail[1]}）"
        if attempt < MAX_ATTEMPTS:
            time.sleep(3 * attempt)

    raise CalibrationError(last_error or "校准请求失败")


def _api_key() -> str:
    for name in ("LINKWORK_ZHIPU_API_KEY", "ZHIPU_API_KEY"):
        value = os.environ.get(name, "").strip()
        if value:
            return value
    return ""


def _timeout() -> float:
    try:
        return float(os.environ.get("LINKWORK_LLM_TIMEOUT", "60"))
    except ValueError:
        return 60.0


def _extract_content(resp) -> str:
    try:
        data = resp.json()
    except ValueError as exc:
        raise CalibrationError(f"校准返回不是合法 JSON：{exc}") from exc
    choices = data.get("choices") or []
    if not choices:
        return ""
    message = choices[0].get("message") or {}
    content = message.get("content")
    if isinstance(content, list):
        content = "".join(
            str(part.get("text", "")) for part in content if isinstance(part, dict)
        )
    return str(content or "").strip()


def _error_detail(resp) -> tuple[int, str]:
    """解析错误码与消息，返回 (code, 可读后缀)。"""
    try:
        error = (resp.json() or {}).get("error") or {}
    except ValueError:
        return 0, ""
    try:
        code = int(error.get("code") or 0)
    except (TypeError, ValueError):
        code = 0
    message = str(error.get("message") or "").strip()
    return code, (f"：{message}" if message else "")


def _clean_output(output: str, source: str) -> str:
    """去掉模型可能添加的代码围栏，并做基本的异常长度校验。"""
    text = output.strip()
    text = re.sub(r"^```[A-Za-z0-9_-]*\s*\n?", "", text)
    text = re.sub(r"\n?```$", "", text).strip()
    if not text:
        return ""
    source_len = len(re.sub(r"\s+", "", source))
    if source_len and len(re.sub(r"\s+", "", text)) < source_len * 0.5:
        raise CalibrationError("校准结果明显短于原文，已放弃本次校准")
    return text


def _split_chunks(text: str, limit: int) -> list[str]:
    """按段落/句子把长文本切成不超过 limit 字符的块。"""
    if len(text) <= limit:
        return [text]

    chunks: list[str] = []
    current = ""
    for paragraph in text.splitlines(keepends=True):
        if len(paragraph) > limit:
            if current:
                chunks.append(current)
                current = ""
            chunks.extend(_split_long_line(paragraph, limit))
            continue
        if current and len(current) + len(paragraph) > limit:
            chunks.append(current)
            current = paragraph
        else:
            current += paragraph
    if current:
        chunks.append(current)
    return [chunk for chunk in chunks if chunk.strip()]


def _split_long_line(line: str, limit: int) -> list[str]:
    """把超长单行按句末标点切开；仍超长则硬切。"""
    pieces: list[str] = []
    current = ""
    for sentence in re.split(r"(?<=[。！？!?；;…])", line):
        if not sentence:
            continue
        if len(sentence) > limit:
            if current:
                pieces.append(current)
                current = ""
            for start in range(0, len(sentence), limit):
                pieces.append(sentence[start : start + limit])
            continue
        if current and len(current) + len(sentence) > limit:
            pieces.append(current)
            current = sentence
        else:
            current += sentence
    if current:
        pieces.append(current)
    return pieces
