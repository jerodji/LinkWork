"""LinkWork - 链接文案提取器.

从给定链接（视频 / 口播 / 图文 / 纯文案网页）中提取文案内容，
视频类链接自动做语音识别，输出可直接使用的口播稿。
"""

from .models import ExtractResult, Segment
from .pipeline import ExtractOptions, extract

__version__ = "0.2.21"

__all__ = ["ExtractResult", "Segment", "ExtractOptions", "extract", "__version__"]
