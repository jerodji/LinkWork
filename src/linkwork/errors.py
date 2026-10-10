"""LinkWork 异常定义。"""

from __future__ import annotations


class LinkWorkError(Exception):
    """所有业务异常的基类。"""

    code = "linkwork_error"
    hint = ""

    def __init__(self, message: str, *, hint: str = "") -> None:
        super().__init__(message)
        self.message = message
        if hint:
            self.hint = hint

    def user_message(self) -> str:
        if self.hint:
            return f"{self.message}\n提示：{self.hint}"
        return self.message


class InvalidURLError(LinkWorkError):
    code = "invalid_url"


class FetchError(LinkWorkError):
    """网络请求失败（超时、403、被反爬拦截等）。"""

    code = "fetch_error"
    hint = "该链接可能需要登录或有反爬限制，可在设置中提供 cookies 文件后重试。"


class UnsupportedContentError(LinkWorkError):
    code = "unsupported_content"
    hint = "暂不支持该链接类型，可直接把文案文本粘贴到「粘贴文案」模式。"

    def __init__(self, message: str, *, hint: str = "", final: bool = False) -> None:
        super().__init__(message, hint=hint)
        #: 为 True 时表示「已确定无法处理」，不再让其他解析器继续尝试，
        #: 避免用通用的网页抽取把错误页/导航文案当成结果返回。
        self.final = final


class MediaError(LinkWorkError):
    """媒体下载/转码失败。"""

    code = "media_error"


class SilentAudioError(MediaError):
    """下载到的音轨是静音（常见于会员/版权限制返回的伴奏轨）。"""

    code = "silent_audio"


class FFmpegMissingError(MediaError):
    code = "ffmpeg_missing"
    hint = "运行 scripts/install_ffmpeg.sh 安装 ffmpeg，或 pip install imageio-ffmpeg。"


class ASRError(LinkWorkError):
    code = "asr_error"


class ASREngineUnavailableError(ASRError):
    code = "asr_engine_unavailable"
    hint = "运行 scripts/setup.sh 安装 faster-whisper，或改用 --engine apple 使用系统语音识别。"
