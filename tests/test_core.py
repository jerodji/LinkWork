"""核心逻辑测试（不依赖网络）。"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from linkwork import exporters, media, ocr
from linkwork.errors import InvalidURLError, UnsupportedContentError
from linkwork.models import ExtractResult, Segment, join_segments
from linkwork.pipeline import ExtractOptions, extract, extract_text
from linkwork.resolvers import ResolverRegistry
from linkwork.resolvers.base import BaseResolver, ResolveContext
from linkwork.resolvers.bilibili import BilibiliResolver
from linkwork.resolvers.douyin import DouyinResolver
from linkwork.resolvers.video import VideoResolver
from linkwork.resolvers.web import WebArticleResolver, clean_text, extract_meta, tags_from_html
from linkwork.resolvers.wbi import MIXIN_KEY_ENC_TAB, get_mixin_key
from linkwork.resolvers.xiaohongshu import XiaohongshuResolver
from linkwork.utils import detect_platform, normalize_url, sanitize_filename


# --------------------------------------------------------------------------- #
# 工具函数
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "raw,expected",
    [
        ("https://v.douyin.com/abc123/", "https://v.douyin.com/abc123/"),
        ("看看这个 https://www.bilibili.com/video/BV1xx411c7mD 很有意思", "https://www.bilibili.com/video/BV1xx411c7mD"),
        ("  https://x.com/a/b）。 ", "https://x.com/a/b"),
    ],
)
def test_normalize_url(raw, expected):
    assert normalize_url(raw) == expected


@pytest.mark.parametrize(
    "url,platform",
    [
        ("https://www.bilibili.com/video/BV1GJ411x7h7", "B站"),
        ("https://b23.tv/abcd", "B站"),
        ("https://v.douyin.com/iRx/", "抖音"),
        ("https://www.xiaohongshu.com/explore/abc", "小红书"),
        ("https://mp.weixin.qq.com/s/abc", "微信"),
        ("https://www.youtube.com/watch?v=dQw4w9WgXcQ", "YouTube"),
        ("https://example.com/post/1", "example.com"),
    ],
)
def test_detect_platform(url, platform):
    assert detect_platform(url) == platform


def test_sanitize_filename():
    assert sanitize_filename("标题/带:非法*字符?") == "标题_带_非法_字符"
    assert sanitize_filename("") == "linkwork"
    assert len(sanitize_filename("长" * 200)) <= 60


def test_join_segments_adds_punctuation_and_paragraphs():
    segments = [
        Segment(0, 2, "大家好"),
        Segment(2.1, 4, "今天聊聊视频文案提取"),
        Segment(6, 8, "第一点先说工具"),
    ]
    text = join_segments(segments)
    assert "大家好今天聊聊视频文案提取。" in text
    assert "\n" in text  # 长停顿换行
    assert text.count("。") >= 2


def test_join_segments_keeps_existing_punctuation():
    text = join_segments([Segment(0, 1, "你好。"), Segment(1, 2, "世界！")])
    assert "你好。" in text and "世界！" in text


# --------------------------------------------------------------------------- #
# 模型与导出
# --------------------------------------------------------------------------- #
def test_extract_text_and_save(tmp_path: Path):
    result = extract_text("这是第一行文案\n这是第二行")
    assert result.kind == "text"
    assert result.char_count > 5

    files = exporters.save_result(result, tmp_path, ("md", "txt", "json"))
    assert set(files) == {"md", "txt", "json"}
    assert files["md"].exists() and files["txt"].exists()
    payload = json.loads(files["json"].read_text(encoding="utf-8"))
    assert payload["method"] == "manual"
    assert "第一行文案" in payload["text"]
    assert "第一行文案" in files["txt"].read_text(encoding="utf-8")


def test_save_result_srt_requires_segments(tmp_path: Path):
    result = ExtractResult(url="", kind="text", method="manual", text="纯文本")
    files = exporters.save_result(result, tmp_path, ("srt",))
    assert files == {}
    result.segments = [Segment(1.5, 3.0, "有字幕")]
    files = exporters.save_result(result, tmp_path, ("srt",))
    content = files["srt"].read_text(encoding="utf-8")
    assert "00:00:01,500 --> 00:00:03,000" in content
    assert "有字幕" in content


def test_markdown_contains_metadata(tmp_path: Path):
    result = ExtractResult(
        url="https://example.com/a",
        kind="video",
        method="asr",
        title="测试标题",
        author="作者A",
        platform="B站",
        segments=[Segment(0, 3, "口播内容。")],
        duration=213,
        engine="faster-whisper small",
    )
    md = exporters.build_markdown(result)
    assert "# 测试标题" in md
    assert "口播稿" in md
    assert "03:33" in md  # 时长格式化
    assert "https://example.com/a" in md


def test_extract_with_options_saves(monkeypatch, tmp_path: Path):
    options = ExtractOptions(out_dir=tmp_path, formats=("txt",))
    output = extract("  一些直接粘贴的文案内容  ", options)
    assert output.files["txt"].exists()
    assert output.result.text.strip() == "一些直接粘贴的文案内容"


def test_extract_empty_input():
    with pytest.raises(UnsupportedContentError):
        extract("   ")


# --------------------------------------------------------------------------- #
# 解析器分派
# --------------------------------------------------------------------------- #
def test_registry_matches_expected_resolvers():
    registry = ResolverRegistry()
    pairs = {
        "https://www.bilibili.com/video/BV1GJ411x7h7": "B站",
        "https://v.douyin.com/abc/": "抖音/快手",
        "https://www.xiaohongshu.com/explore/123": "小红书",
        "https://www.youtube.com/watch?v=abc": "YouTube",
        "https://example.com/post/1": "网页图文",
        "https://cdn.example.com/a.mp4": "通用视频",
    }
    for url, name in pairs.items():
        matched = [r.name for r in registry.resolvers if r.matches(url)]
        assert name in matched, f"{url} 未匹配到 {name}（实际 {matched}）"


def test_registry_rejects_invalid_url():
    registry = ResolverRegistry()
    with pytest.raises(InvalidURLError):
        registry.resolve("not-a-url", ResolveContext(workdir=Path("/tmp")))


def test_web_resolver_skips_video_pages():
    resolver = WebArticleResolver()
    html = '<html><head><meta property="og:video" content="https://x/v.mp4"></head><body><video src="a.mp4"></video></body></html>'
    assert resolver._is_video_dominant(html, {"video_url": "https://x/v.mp4"}) is True
    html_article = (
        '<html><head><meta property="og:type" content="article">'
        '<script type="application/ld+json">{"@type":"NewsArticle"}</script></head><body></body></html>'
    )
    assert resolver._is_video_dominant(html_article, {"og_type": "article"}) is False


def test_xiaohongshu_state_parsing():
    resolver = XiaohongshuResolver()
    html = """
    <html><head><title>笔记标题</title></head><body>
    <script>window.__INITIAL_STATE__={"note":{"noteDetailMap":{"abc":{"note":{"title":"标题","desc":"正文内容",
      "type":"normal","user":{"nickname":"小红薯"},"imageList":[{"urlDefault":"https://cdn.xhscdn.com/a.jpg"}]}}}}}</script>
    </body></html>
    """
    state = resolver._extract_state(html)
    assert state["description"] == "正文内容"
    assert state["author"] == "小红薯"
    assert state["is_video"] is False
    assert state["images"] == ["https://cdn.xhscdn.com/a.jpg"]


def test_douyin_share_page_parsing():
    resolver = DouyinResolver()
    html = '<html><head><title>抖音短视频</title><meta name="description" content="这是文案内容"></head></html>'
    data = resolver._extract_share_json(html)
    assert data["description"] == "这是文案内容"


def test_video_resolver_matches_direct_media_links():
    resolver = VideoResolver()
    assert resolver.matches("https://cdn.example.com/video.mp4")
    assert resolver.matches("https://cdn.example.com/live.m3u8")
    assert not resolver.matches("https://example.com/post/1")


# --------------------------------------------------------------------------- #
# 网页解析辅助
# --------------------------------------------------------------------------- #
def test_extract_meta_prefers_og():
    html = """
    <html><head>
    <title>原标题</title>
    <meta property="og:title" content="OG 标题">
    <meta property="og:description" content="OG 描述">
    <meta property="og:site_name" content="示例站">
    <meta name="author" content="作者">
    </head><body></body></html>
    """
    meta = extract_meta(html)
    assert meta["og:title"] == "OG 标题"
    assert meta["description"] == "OG 描述"
    assert meta["og:site_name"] == "示例站"
    assert meta["author"] == "作者"


def test_extract_meta_jsonld_and_video():
    html = """
    <html><head><script type="application/ld+json">
    {"@type":"VideoObject","headline":"视频标题","contentUrl":"https://x/v.mp4"}
    </script></head><body></body></html>
    """
    meta = extract_meta(html)
    assert meta["title"] == "视频标题"
    assert meta["video_url"] == "https://x/v.mp4"


def test_tags_from_html():
    tags = tags_from_html('<div>#视频文案# 和 #效率工具#</div>')
    assert "视频文案" in tags and "效率工具" in tags


def test_clean_text_collapses_blank_lines():
    assert clean_text("a\n\n\n\nb   c\n") == "a\n\nb c"


# --------------------------------------------------------------------------- #
# 字幕解析
# --------------------------------------------------------------------------- #
def test_parse_vtt(tmp_path: Path):
    path = tmp_path / "a.vtt"
    path.write_text(
        "WEBVTT\n\n00:00:01.000 --> 00:00:03.000\n大家好\n\n"
        "00:00:03.000 --> 00:00:05.500\n今天聊聊<b>文案提取</b>\n",
        encoding="utf-8",
    )
    rows = media.parse_subtitle_file(path)
    assert len(rows) == 2
    assert rows[0][0] == 1.0 and rows[0][2] == "大家好"
    assert rows[1][2] == "今天聊聊文案提取"


def test_parse_srt_dedupes_rolling_lines(tmp_path: Path):
    path = tmp_path / "a.srt"
    path.write_text(
        "1\n00:00:01,000 --> 00:00:02,000\n你好\n\n"
        "2\n00:00:02,000 --> 00:00:03,000\n你好\n\n"
        "3\n00:00:03,000 --> 00:00:04,000\n你好世界\n",
        encoding="utf-8",
    )
    rows = media.parse_subtitle_file(path)
    assert [r[2] for r in rows] == ["你好", "你好世界"]


def test_parse_json3(tmp_path: Path):
    path = tmp_path / "a.json3"
    path.write_text(
        json.dumps({"events": [{"tStartMs": 1000, "dDurationMs": 2000, "segs": [{"utf8": "你好"}, {"utf8": "世界"}]}]}),
        encoding="utf-8",
    )
    rows = media.parse_subtitle_file(path)
    assert rows == [(1.0, 3.0, "你好世界")]


def test_pick_subtitle_track_prefers_manual_and_chinese():
    # 有官方字幕时优先官方字幕（即使语言不同），而不是自动字幕
    info = {
        "subtitles": {"en": [{"ext": "vtt", "url": "https://x/en.vtt"}]},
        "automatic_captions": {"zh-Hans": [{"ext": "json3", "url": "https://x/zh.json3"}]},
    }
    track = media.pick_subtitle_track(info)
    assert track == ("https://x/en.vtt", "vtt", "en")


def test_pick_subtitle_track_prefers_chinese_within_group():
    # 同一组内中文优先于英文
    info = {
        "automatic_captions": {
            "en": [{"ext": "vtt", "url": "https://x/en.vtt"}],
            "zh-Hans": [{"ext": "json3", "url": "https://x/zh.json3"}],
        }
    }
    track = media.pick_subtitle_track(info)
    assert track == ("https://x/zh.json3", "json3", "zh-Hans")


def test_pick_subtitle_track_prefix_match_for_variants():
    info = {"subtitles": {"zh-Hans-CN": [{"ext": "srt", "url": "https://x/a.srt"}]}}
    assert media.pick_subtitle_track(info) == ("https://x/a.srt", "srt", "zh-Hans-CN")


def test_pick_subtitle_track_none_when_empty():
    assert media.pick_subtitle_track({}) is None


# --------------------------------------------------------------------------- #
# WBI 签名
# --------------------------------------------------------------------------- #
def test_wbi_mixin_key_matches_known_value():
    # 取自 B 站前端真实数据：img_key + sub_key → mixin key
    raw = "7cd084941338484aae1ad9425b84077c4932caff0ff746eab6f01bf08b70ac45"
    assert get_mixin_key(raw) == "ea1db124af3c7062474693fa704f4ff8"
    assert len(MIXIN_KEY_ENC_TAB) == 64


def test_wbi_signer_builds_w_rid(monkeypatch):
    from linkwork.resolvers import wbi

    signer = wbi.WbiSigner()
    monkeypatch.setattr(signer, "mixin_key", lambda session: "ea1db124af3c7062474693fa704f4ff8")
    params = signer.sign({"bvid": "BV1GJ411x7h7", "cid": 137649199}, session=None)
    assert "w_rid" in params and len(params["w_rid"]) == 32
    assert "wts" in params


# --------------------------------------------------------------------------- #
# 媒体工具
# --------------------------------------------------------------------------- #
def test_media_duration_parses_ffmpeg_output(tmp_path: Path):
    assert media.ffmpeg_available() is True
    ffmpeg = media.find_ffmpeg()
    import subprocess

    wav = tmp_path / "tone.wav"
    subprocess.run(
        [ffmpeg, "-hide_banner", "-loglevel", "error", "-y", "-f", "lavfi", "-i", "sine=frequency=440:duration=2.5", str(wav)],
        check=True,
        capture_output=True,
    )
    duration = media.media_duration(wav)
    assert duration is not None and 2.0 < duration < 3.0


def test_to_asr_wav_produces_16k_mono(tmp_path: Path):
    import subprocess

    ffmpeg = media.find_ffmpeg()
    src = tmp_path / "src.wav"
    subprocess.run(
        [ffmpeg, "-hide_banner", "-loglevel", "error", "-y", "-f", "lavfi", "-i", "sine=frequency=440:duration=1", str(src)],
        check=True,
        capture_output=True,
    )
    out = media.to_asr_wav(src, tmp_path)
    assert out.exists() and out.stat().st_size > 1024


def test_ffmpeg_missing_error_message(monkeypatch):
    monkeypatch.setattr(media, "_FFMPEG_CACHE", None)
    monkeypatch.setattr(media, "_from_env", lambda: None)
    monkeypatch.setattr(media, "_from_path", lambda names: None)
    monkeypatch.setattr(media, "_from_bundled", lambda: None)
    monkeypatch.setattr(media, "_from_common_paths", lambda: None)
    from linkwork.errors import FFmpegMissingError

    with pytest.raises(FFmpegMissingError):
        media.find_ffmpeg()


def test_ocr_module_reports_availability():
    assert isinstance(ocr.ocr_available(), bool)
    if not ocr.ocr_available():
        assert "pyobjc" in ocr.unavailable_reason()


# --------------------------------------------------------------------------- #
# ASR 引擎选择
# --------------------------------------------------------------------------- #
def test_asr_engine_factory_none():
    from linkwork.asr import get_engine

    engine = get_engine("none")
    assert engine.name == "none"
    assert engine.is_available() is True


def test_asr_engine_factory_auto_picks_available():
    from linkwork.asr import available_engines

    engines = {e["name"]: e["available"] for e in available_engines()}
    assert "faster-whisper" in engines
    assert engines["faster-whisper"] is True


def test_base_resolver_http_headers():
    ctx = ResolveContext(workdir=Path("/tmp"))
    headers = ctx.http_headers(mobile=True)
    assert "iPhone" in headers["User-Agent"]
    headers_desktop = ctx.http_headers()
    assert "Macintosh" in headers_desktop["User-Agent"]


def test_bilibili_bvid_extraction():
    resolver = BilibiliResolver()
    assert resolver._bvid("https://www.bilibili.com/video/BV1GJ411x7h7/?share=1") == "BV1GJ411x7h7"
    assert resolver._bvid("https://b23.tv/abc") == ""
    assert resolver._aid("https://www.bilibili.com/video/av80433022") == "80433022"


# --------------------------------------------------------------------------- #
# 文本规范化 / 音频解码兼容层
# --------------------------------------------------------------------------- #
def test_normalize_cjk_simplifies_and_punctuates():
    from linkwork.textnorm import normalize_cjk

    out = normalize_cjk("歡迎來到本期視頻,今天我們來聊一聊如何從鏈接裡提取文案")
    assert "欢迎来到本期视频" in out
    assert "，" in out and "," not in out
    assert "链接" in out


def test_normalize_cjk_leaves_english_alone():
    from linkwork.textnorm import normalize_cjk

    assert normalize_cjk("Hello world, this is a test.") == "Hello world, this is a test."


def test_normalize_cjk_spacing_between_cjk_and_latin():
    from linkwork.textnorm import normalize_cjk

    assert normalize_cjk("用Python處理100條鏈接") == "用 Python 处理 100 条链接"


def test_audio_decode_reads_16k_wav(tmp_path: Path):
    import subprocess

    import numpy as np

    from linkwork.audio_decode import install_decoder_patch, is_wav_file, wav_to_float32

    ffmpeg = media.find_ffmpeg()
    src = tmp_path / "tone.wav"
    subprocess.run(
        [ffmpeg, "-hide_banner", "-loglevel", "error", "-y", "-f", "lavfi", "-i", "sine=frequency=440:duration=1", str(src)],
        check=True,
        capture_output=True,
    )
    wav = media.to_asr_wav(src, tmp_path)
    assert is_wav_file(wav)
    audio = wav_to_float32(wav)
    assert audio.dtype == np.float32
    assert audio.ndim == 1
    assert abs(audio.size - 16000) < 200  # 约 1 秒 @16kHz


def test_install_decoder_patch_replaces_transcribe_reference():
    from linkwork.audio_decode import install_decoder_patch

    assert install_decoder_patch() is True
    import faster_whisper.audio as fa
    import faster_whisper.transcribe as ft

    assert fa.decode_audio.__module__ == "linkwork.audio_decode"
    assert ft.decode_audio.__module__ == "linkwork.audio_decode"


def test_preflight_reports_pyav_state():
    from linkwork.audio_decode import preflight

    info = preflight()
    assert "pyav_incompatible" in info and "patched" in info


def test_audio_signal_stats_detects_silence_and_sound(tmp_path: Path):
    import subprocess

    from linkwork.media import audio_signal_stats, is_silent_audio

    ffmpeg = media.find_ffmpeg()
    silent = tmp_path / "silent.wav"
    subprocess.run(
        [ffmpeg, "-hide_banner", "-loglevel", "error", "-y", "-f", "lavfi", "-i", "anullsrc=r=16000:cl=mono", "-t", "1", str(silent)],
        check=True,
        capture_output=True,
    )
    loud = tmp_path / "loud.wav"
    subprocess.run(
        [ffmpeg, "-hide_banner", "-loglevel", "error", "-y", "-f", "lavfi", "-i", "sine=frequency=440:duration=1", "-ac", "1", "-ar", "16000", str(loud)],
        check=True,
        capture_output=True,
    )
    assert is_silent_audio(silent) is True
    assert is_silent_audio(loud) is False
    stats = audio_signal_stats(loud)
    assert stats["peak"] > 0.1 and stats["samples"] > 0
    assert audio_signal_stats(tmp_path / "missing.wav")["samples"] == 0


def test_audio_format_tiers_ordered_by_quality():
    from linkwork.media import FORMAT_AUDIO_TIERS

    assert "abr>=128" in FORMAT_AUDIO_TIERS[0][0]
    assert len(FORMAT_AUDIO_TIERS) >= 3


# --------------------------------------------------------------------------- #
# 回归测试：仅含分段的结果必须被接受；字数统计对英文同样有效
# --------------------------------------------------------------------------- #
def test_registry_accepts_segment_only_results():
    """修复回归：只带 segments（无 text）的结果此前被误判为空而丢弃。"""

    class FakeResolver:
        name = "fake"
        priority = 1
        domains = ("example.com",)

        def matches(self, url: str) -> bool:
            return True

        def resolve(self, url: str, ctx: ResolveContext) -> ExtractResult:
            return ExtractResult(
                url=url,
                kind="video",
                method="asr",
                title="仅有分段的视频",
                segments=[Segment(0.0, 2.0, "这是一段口播内容。")],
            )

    registry = ResolverRegistry([FakeResolver()])
    result = registry.resolve("https://example.com/v/1", ResolveContext(workdir=Path("/tmp")))
    assert result.title == "仅有分段的视频"
    assert result.segments and result.spoken_script.strip()


def test_registry_skips_truly_empty_results():
    class EmptyResolver:
        name = "empty"
        priority = 1
        domains = ("example.com",)

        def matches(self, url: str) -> bool:
            return True

        def resolve(self, url: str, ctx: ResolveContext) -> ExtractResult:
            return ExtractResult(url=url, kind="article", method="html", text="   ")

    with pytest.raises(UnsupportedContentError):
        ResolverRegistry([EmptyResolver()]).resolve("https://example.com/a", ResolveContext(workdir=Path("/tmp")))


def test_char_count_works_for_english_and_chinese():
    english = ExtractResult(
        url="", kind="video", method="asr", segments=[Segment(0, 3, "We're no strangers to love")]
    )
    assert english.char_count == len("We'renostrangerstolove")

    chinese = ExtractResult(url="", kind="article", method="html", text="你好 世界\n第二行")
    assert chinese.char_count == 7  # 你好世界第二行


def test_join_segments_inserts_spaces_for_latin_text():
    segments = [Segment(0, 2, "Never gonna give you up"), Segment(2, 4, "Never gonna let you down")]
    text = join_segments(segments)
    assert "up Never" in text  # 英文分段之间必须有空格
    assert "upNever" not in text


def test_join_segments_no_space_for_chinese():
    segments = [Segment(0, 2, "大家好"), Segment(2, 4, "欢迎收看")]
    text = join_segments(segments)
    assert "大家好欢迎收看" in text


def test_duration_format_handles_zero_and_none():
    from linkwork.exporters import _format_duration

    assert _format_duration(0) == "00:00"
    assert _format_duration(27) == "00:27"
    assert _format_duration(213) == "03:33"
    assert _format_duration(3725) == "1:02:05"
    assert _format_duration(None) == "未知"


# --------------------------------------------------------------------------- #
# Apple 语音识别：未授权时必须快速失败（而不是挂死）
# --------------------------------------------------------------------------- #
def test_apple_speech_fails_fast_without_authorization(monkeypatch, tmp_path: Path):
    from linkwork.asr import apple_speech

    engine = apple_speech.AppleSpeechEngine()
    if not engine.is_available():  # pragma: no cover - 非 macOS 环境
        pytest.skip("当前环境没有 Speech 框架")
    monkeypatch.setattr(engine, "authorization_status", lambda: apple_speech.AUTH_NOT_DETERMINED)
    wav = tmp_path / "a.wav"
    wav.write_bytes(b"RIFF")
    with pytest.raises(Exception) as excinfo:
        engine.transcribe(wav)
    assert "授权" in str(excinfo.value)


def test_apple_speech_locale_mapping():
    from linkwork.asr.apple_speech import AppleSpeechEngine

    engine = AppleSpeechEngine()
    assert engine._resolve_locale("zh") == "zh-CN"
    assert engine._resolve_locale("zh-tw") == "zh-TW"
    assert engine._resolve_locale("en") == "en-US"
    assert engine._resolve_locale("fr-FR") == "fr-FR"
    assert engine._resolve_locale(None) == engine.locale


# --------------------------------------------------------------------------- #
# 图片 OCR：Vision 的 accurate 模式对中文常返回空，必须先走 fast 模式
# --------------------------------------------------------------------------- #
def _install_fake_vision(monkeypatch, tmp_path: Path, mode_results: dict):
    """把 Vision/Quartz 替换成假的实现，便于离线测试 OCR 的双模式逻辑。"""
    import sys
    import types
    from linkwork import ocr

    calls: list[int] = []

    def fake_recognition(img, *, level, languages, correct):
        calls.append(level)
        return mode_results.get(level, "")

    fake_quartz = types.ModuleType("Quartz")
    fake_quartz.CGImageSourceCreateWithURL = lambda url, options: object()
    fake_quartz.CGImageSourceCreateImageAtIndex = lambda source, index, options: object()

    fake_vision = types.ModuleType("Vision")
    fake_foundation = types.ModuleType("Foundation")
    fake_foundation.NSURL = types.SimpleNamespace(fileURLWithPath_=lambda p: ("url", p))

    monkeypatch.setitem(sys.modules, "Quartz", fake_quartz)
    monkeypatch.setitem(sys.modules, "Vision", fake_vision)
    monkeypatch.setitem(sys.modules, "Foundation", fake_foundation)
    monkeypatch.setattr(ocr, "_run_recognition", fake_recognition)
    monkeypatch.setattr(ocr, "ocr_available", lambda: True)
    return calls


def test_ocr_prefers_fast_mode_for_chinese(monkeypatch, tmp_path: Path):
    from linkwork import ocr

    image = tmp_path / "x.png"
    image.write_bytes(b"\x89PNG")
    calls = _install_fake_vision(monkeypatch, tmp_path, {0: "Bai du 百度", 1: ""})

    text = ocr.recognize_image_text(image)
    assert "百度" in text
    assert calls[0] == 0  # 先尝试 fast 模式，命中即返回
    assert calls == [0]


def test_ocr_falls_back_to_accurate_when_fast_is_sparse(monkeypatch, tmp_path: Path):
    from linkwork import ocr

    image = tmp_path / "x.png"
    image.write_bytes(b"\x89PNG")
    calls = _install_fake_vision(
        monkeypatch, tmp_path, {0: "短", 1: "这是一段较长的中文识别结果"}
    )

    text = ocr.recognize_image_text(image)
    assert calls == [0, 1]
    assert "较长的中文识别结果" in text


# --------------------------------------------------------------------------- #
# 回归测试：话题标签噪声、错误页判定、cookies 提示
# --------------------------------------------------------------------------- #
def test_tags_from_html_ignores_css_colors():
    html = '<style>.a{color:#fff;background:#aeaeae}</style><div>#真话题# #效率工具# #ff5267,#fff</div>'
    tags = tags_from_html(html)
    assert tags == ["真话题", "效率工具"]
    assert "fff" not in tags and "aeaeae" not in tags


def test_is_valid_tag_rejects_noise():
    from linkwork.resolvers.web import is_valid_tag

    assert is_valid_tag("效率工具") is True
    assert is_valid_tag("fff") is False
    assert is_valid_tag("ff5267,") is False
    assert is_valid_tag("a;color") is False
    assert is_valid_tag("") is False
    assert is_valid_tag("x" * 30) is False


def test_xiaohongshu_detects_error_page():
    from linkwork.resolvers.xiaohongshu import XiaohongshuResolver

    resolver = XiaohongshuResolver()
    html = "<html><head><title>小红书 - 你访问的页面不见了</title></head><body>RED 关于我们</body></html>"
    state = {"title": "小红书 - 你访问的页面不见了", "description": "", "images": []}
    assert resolver._is_error_page(html, "https://www.xiaohongshu.com/404?source=/404", state) is True
    # 正常笔记不应被判为错误页
    ok_state = {"title": "三分钟学会文案提取", "description": "正文内容", "images": ["https://x/a.jpg"]}
    assert resolver._is_error_page("<html>...</html>", "https://www.xiaohongshu.com/explore/abc", ok_state) is False


def test_unsupported_error_final_flag_skips_fallback():
    """final=True 的错误必须直接抛出，不能落到兜底解析器。"""

    class FinalResolver:
        name = "final"
        priority = 1
        domains = ("example.com",)

        def matches(self, url: str) -> bool:
            return True

        def resolve(self, url: str, ctx: ResolveContext) -> ExtractResult:
            raise UnsupportedContentError("页面不存在（404）", final=True)

    class FallbackResolver:
        name = "fallback"
        priority = 99
        domains = ("example.com",)

        def matches(self, url: str) -> bool:
            return True

        def resolve(self, url: str, ctx: ResolveContext) -> ExtractResult:
            raise AssertionError("不应调用兜底解析器")

    registry = ResolverRegistry([FinalResolver(), FallbackResolver()])
    with pytest.raises(UnsupportedContentError) as excinfo:
        registry.resolve("https://example.com/x", ResolveContext(workdir=Path("/tmp")))
    assert "404" in str(excinfo.value)


def test_needs_cookies_detects_platform_blocks():
    from linkwork.transcribe import needs_cookies

    assert needs_cookies("ERROR: [Douyin] xxx: Fresh cookies (not necessarily logged in) are needed")
    assert needs_cookies("Unsupported URL: https://www.iesdouyin.com/share/video/123")
    assert needs_cookies("HTTP Error 403: Forbidden")
    assert needs_cookies("Please sign in to continue")
    assert not needs_cookies("HTTP Error 404: Not Found")


def test_short_error_keeps_host_for_diagnosis():
    from linkwork.media import _short_error

    long_url = "https://www.iesdouyin.com/share/video/7230000000000000000/?a=" + "b" * 120
    text = _short_error(f"Unsupported URL: {long_url}")
    assert "iesdouyin.com" in text  # 保留域名，便于判断平台
    assert len(text) <= 160


# --------------------------------------------------------------------------- #
# 集成测试：小红书图文笔记 → 正文 + 图片文字（真实图片走 Vision OCR）
# --------------------------------------------------------------------------- #
XHS_SAMPLE_IMAGE = Path(__file__).parent / "assets" / "xhs_sample.png"


def test_xiaohongshu_image_text_integration_with_real_ocr(monkeypatch, tmp_path: Path):
    """用真实中文图片验证「图文笔记 → 图片文字」整条链路。"""
    from linkwork import ocr
    from linkwork.resolvers.xiaohongshu import XiaohongshuResolver

    if not ocr.ocr_available():  # pragma: no cover
        pytest.skip("当前环境没有 Vision OCR")
    if not XHS_SAMPLE_IMAGE.exists():  # pragma: no cover
        pytest.skip("缺少测试图片")

    image_bytes = XHS_SAMPLE_IMAGE.read_bytes()

    class FakeResponse:
        content = image_bytes
        status_code = 200

        def raise_for_status(self):
            return None

    html = (
        "<html><head><title>三分钟学会文案提取</title></head><body>"
        '<script>window.__INITIAL_STATE__={"note":{"noteDetailMap":{"x":{"note":'
        '{"title":"三分钟学会文案提取","desc":"今天分享一个超好用的链接文案提取方法 #效率工具#",'
        '"type":"normal","user":{"nickname":"效率小张"},'
        '"imageList":[{"urlDefault":"https://sns-webpic-qc.xhscdn.com/a.jpg"}]}}}}}'
        "</script></body></html>"
    )

    resolver = XiaohongshuResolver()
    ctx = ResolveContext(workdir=tmp_path, timeout=10)
    monkeypatch.setattr(resolver, "fetch_html", lambda url, ctx, mobile=False: (url, html))
    monkeypatch.setattr(resolver, "request", lambda *a, **k: FakeResponse())

    result = resolver.resolve("https://www.xiaohongshu.com/explore/abc", ctx)

    assert result.kind == "image_text"
    assert "三分钟学会文案提取" in result.text
    assert "效率工具" in result.text
    assert "图片文字" in result.text, "应包含 OCR 得到的图片文字段落"
    # 图片里是中文文案，OCR 后应能命中其中关键词
    assert any(kw in result.text for kw in ("小红书", "图文", "文案", "链接", "Markdown")), result.text


# --------------------------------------------------------------------------- #
# 版本一致性：四处版本号必须由 VERSION 文件统一派生
# --------------------------------------------------------------------------- #
def test_version_is_consistent_everywhere():
    """VERSION / pyproject / __init__ / manifest 必须完全一致。

    每次改动代码后用 .venv/bin/python scripts/bump_version.py 统一递增；
    本测试防止只改了其中一处（曾出现过 manifest 0.2.0 而包版本仍是 0.1.0）。
    """
    import json
    import re

    root = Path(__file__).resolve().parent.parent
    version = (root / "VERSION").read_text(encoding="utf-8").strip()
    assert re.fullmatch(r"\d+\.\d+\.\d+", version), f"VERSION 格式错误：{version!r}"

    pyproject = (root / "pyproject.toml").read_text(encoding="utf-8")
    m = re.search(r'(?m)^version = "([^"]+)"', pyproject)
    assert m and m.group(1) == version, f"pyproject.toml 版本不一致：{m and m.group(1)} != {version}"

    init = (root / "src" / "linkwork" / "__init__.py").read_text(encoding="utf-8")
    m = re.search(r'__version__ = "([^"]+)"', init)
    assert m and m.group(1) == version, f"__init__.py 版本不一致：{m and m.group(1)} != {version}"

    manifest = json.loads((root / "extension" / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["version"] == version, f"manifest.json 版本不一致：{manifest['version']} != {version}"

    # 导入的包版本也要一致
    import linkwork

    assert linkwork.__version__ == version, f"导入版本不一致：{linkwork.__version__} != {version}"


def test_extractor_build_marker_is_inside_function():
    """EXTRACTOR_BUILD 必须在 pageExtractor 函数体内。

    该函数会被 chrome.scripting 序列化后单独执行，引用模块级变量会 ReferenceError。
    """
    import re

    root = Path(__file__).resolve().parent.parent
    src = (root / "extension" / "extractor.js").read_text(encoding="utf-8")
    start = src.index("function pageExtractor()")
    # 找到函数体结束（花括号配平）
    depth = 0
    started = False
    end = -1
    for i in range(start, len(src)):
        if src[i] == "{":
            depth += 1
            started = True
        elif src[i] == "}":
            depth -= 1
            if started and depth == 0:
                end = i + 1
                break
    body = src[start:end]
    assert re.search(r"const EXTRACTOR_BUILD = \d+;", body), "EXTRACTOR_BUILD 未声明在函数体内"
    assert "result.build = EXTRACTOR_BUILD;" in body, "build 未写入返回结果"
