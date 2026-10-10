"""中文文本规范化。

Whisper 对中文经常输出繁体字与半角标点，这里统一成简体 + 全角标点，
并清理中英文之间的多余空格，让口播稿可以直接使用。
"""

from __future__ import annotations

import logging
import re

log = logging.getLogger(__name__)

_CJK = r"\u4e00-\u9fff\u3400-\u4dbf"
_T2S_TABLE: dict[int, str] | None = None
_T2S_READY = False

#: 常见繁简差异（在缺少 opencc 等依赖时兜底）
_FALLBACK_T2S = str.maketrans(
    {
        "歡": "欢", "迎": "迎", "來": "来", "頻": "频",
        "鏈": "链", "裡": "里", "這": "这", "們": "们", "個": "个",
        "說": "说", "話": "话", "麼": "么", "為": "为", "對": "对",
        "開": "开", "關": "关", "時": "时", "間": "间", "樣": "样",
        "點": "点", "樂": "乐", "產": "产", "業": "业", "網": "网",
        "頁": "页", "軟": "软", "體": "体", "發": "发", "覺": "觉",
        "學": "学", "習": "习", "實": "实", "現": "现", "內": "内",
        "觀": "观", "眾": "众", "號": "号", "導": "导",
        "圖": "图", "書": "书", "過": "过", "還": "还", "讓": "让",
        "無": "无", "該": "该", "幾": "几", "準": "准",
        "備": "备", "錄": "录", "製": "制", "轉": "转", "換": "换",
        "難": "难", "簡": "简", "單": "单", "臺": "台", "灣": "湾",
        "聲": "声", "據": "据", "處": "处", "總": "总", "結": "结",
        "構": "构", "標": "标", "題": "题", "選": "选", "擇": "择",
        "語": "语", "詞": "词", "認": "认", "識": "识", "別": "别",
        "種": "种", "類": "类", "幫": "帮", "夠": "够", "興": "兴",
        "萬": "万", "億": "亿", "長": "长", "東": "东",
        "風": "风", "氣": "气", "車": "车", "進": "进", "運": "运",
        "費": "费", "買": "买", "賣": "卖", "價": "价", "錢": "钱",
        "務": "务", "係": "系", "統": "统", "與": "与", "從": "从",
        "將": "将", "會": "会", "後": "后", "產": "产", "經": "经",
    }
)


def _load_opencc():
    """尝试加载 opencc（若已安装），否则返回 None。"""
    global _T2S_TABLE, _T2S_READY
    if _T2S_READY:
        return _T2S_TABLE
    _T2S_READY = True
    try:
        from opencc import OpenCC  # noqa: PLC0415

        converter = OpenCC("t2s")

        def convert(text: str) -> str:
            return converter.convert(text)

        _T2S_TABLE = {"converter": convert}  # type: ignore[assignment]
        log.debug("使用 opencc 做繁简转换")
    except Exception:  # noqa: BLE001
        _T2S_TABLE = None
    return _T2S_TABLE


def traditional_to_simplified(text: str) -> str:
    """繁体转简体（优先 opencc，缺失时用内置常用字表）。"""
    if not text:
        return text
    table = _load_opencc()
    if table and "converter" in table:  # type: ignore[operator]
        try:
            return table["converter"](text)  # type: ignore[index]
        except Exception as exc:  # noqa: BLE001
            log.debug("opencc 转换失败：%s", exc)
    return text.translate(_FALLBACK_T2S)


_HALF_TO_FULL = str.maketrans({",": "，", "?": "？", "!": "！", ";": "；", ":": "：", "(": "（", ")": "）"})


def normalize_punctuation(text: str) -> str:
    """中文语境下的标点规范化。"""
    if not text:
        return text
    # 仅当中文占比可观时才转换，避免影响英文内容
    cjk_count = len(re.findall(f"[{_CJK}]", text))
    if cjk_count and cjk_count >= max(2, len(text) * 0.15):
        text = re.sub(r"(?<=[{}])\.(?=\s|$)".format(_CJK), "。", text)
        text = re.sub(r"(?<=[{}])[,]".format(_CJK), "，", text)
        text = re.sub(r"(?<=[{}])[?]".format(_CJK), "？", text)
        text = re.sub(r"(?<=[{}])[!]".format(_CJK), "！", text)
        text = text.translate(_HALF_TO_FULL)
    # 中文之间不应有空格（注意不能吃掉换行，否则段落会被合并）
    text = re.sub(f"(?<=[{_CJK}])[^\\S\\n]+(?=[{_CJK}])", "", text)
    # 中文与英文/数字之间保留一个空格
    text = re.sub(f"(?<=[{_CJK}])(?=[A-Za-z0-9])", " ", text)
    text = re.sub(f"(?<=[A-Za-z0-9])(?=[{_CJK}])", " ", text)
    # 行内多余空格压缩，但保留换行与段落结构
    text = re.sub(r"[ \t]{2,}", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def normalize_cjk(text: str) -> str:
    """完整的口播稿文本规范化。"""
    return normalize_punctuation(traditional_to_simplified(text or ""))
