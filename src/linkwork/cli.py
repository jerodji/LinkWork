"""LinkWork 命令行入口。

用法示例：
    linkwork "https://www.bilibili.com/video/BV1xx411c7mD"
    linkwork serve                 # 启动本地服务（供浏览器插件调用）
    linkwork doctor
    linkwork --format md,txt,srt --model small "https://v.douyin.com/xxxx/"
"""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

from . import __version__
from .errors import LinkWorkError
from .exporters import build_text
from .pipeline import DEFAULT_FORMATS, ExtractOptions, PipelineOutput, extract_many

from .service import DEFAULT_HOST, DEFAULT_PORT, ServiceConfig, run_service
from .utils import setup_logging

log = logging.getLogger("linkwork.cli")

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="linkwork",
        description="LinkWork - 从链接（视频/口播/图文/文案）中提文案、生成口播稿",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument("inputs", nargs="*", help="一个或多个链接（也支持直接粘贴的文案）")
    parser.add_argument("-o", "--out", default=None, help="输出目录，默认 ./output")
    parser.add_argument(
        "-f",
        "--format",
        default=",".join(DEFAULT_FORMATS),
        help="导出格式，逗号分隔：md,txt,json,srt（默认 md,txt）",
    )
    parser.add_argument("--engine", default="auto", choices=["auto", "faster-whisper", "apple", "none"], help="语音识别引擎")
    parser.add_argument("--model", default=None, help="Whisper 模型：tiny/base/small/medium/large-v3（默认 small）")
    parser.add_argument("--lang", default=None, help="语言代码，如 zh、en；默认自动检测")
    parser.add_argument("--cookies", default=None, help="cookies 文件路径，或 browser:chrome 形式")
    parser.add_argument("--proxy", default=None, help="代理，如 http://127.0.0.1:7890")
    parser.add_argument("--subtitles-only", action="store_true", help="只提取平台字幕/网页文本，不做语音识别")
    parser.add_argument("--keep-temp", action="store_true", help="保留临时音频文件（调试用）")
    parser.add_argument("--stdin", action="store_true", help="从标准输入逐行读取链接")
    parser.add_argument("--print", dest="print_text", action="store_true", help="把提取结果打印到终端")
    parser.add_argument("--doctor", action="store_true", help="检查运行环境（ffmpeg / yt-dlp / 语音识别引擎）")
    parser.add_argument("--serve", action="store_true", help="启动本地服务（供浏览器插件调用）")
    parser.add_argument("--host", default=None, help=f"服务监听地址（默认 {DEFAULT_HOST}）")
    parser.add_argument("--port", type=int, default=None, help=f"服务端口（默认 {DEFAULT_PORT}）")
    parser.add_argument("-v", "--verbose", action="store_true", help="输出详细日志")
    parser.add_argument("-V", "--version", action="version", version=f"LinkWork {__version__}")
    return parser

def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    # 便捷子命令：linkwork doctor / linkwork serve
    if argv and argv[0] in ("doctor", "doctor!", "selfcheck"):
        argv = ["--doctor"] + argv[1:]
    elif argv and argv[0] == "serve":
        argv = ["--serve"] + argv[1:]

    parser = build_parser()
    args = parser.parse_args(argv)
    setup_logging(logging.INFO if args.verbose else logging.WARNING)

    if args.doctor:
        return doctor()
    if args.serve:
        return serve(host=args.host, port=args.port)

    inputs = list(args.inputs)
    if args.stdin:
        inputs.extend(line.strip() for line in sys.stdin if line.strip())
    if not inputs:
        parser.print_help()
        return 2

    options = ExtractOptions(
        formats=tuple(f.strip() for f in args.format.split(",") if f.strip()),
        cookies=args.cookies,
        proxy=args.proxy,
        asr_engine=args.engine,
        language=args.lang,
        subtitles_only=args.subtitles_only,
        keep_temp=args.keep_temp,
        quiet=not args.verbose,
    )
    if args.out:
        options.out_dir = Path(args.out).expanduser()
    if args.model:
        options.asr_model = args.model

    outputs = extract_many(inputs, options, save=True, continue_on_error=True)

    failed = 0
    for index, output in enumerate(outputs, start=1):
        if isinstance(output, Exception):
            failed += 1
            print(f"[{index}] 失败：{_error_text(output)}", file=sys.stderr)
            continue
        print(_summary(output, index, print_text=args.print_text))

    total = len(outputs)
    ok = total - failed
    print(f"\n完成：成功 {ok} / {total}" + (f"，失败 {failed}" if failed else ""), file=sys.stderr)
    return 1 if failed and ok == 0 else 0

def _error_text(exc: Exception) -> str:
    if isinstance(exc, LinkWorkError):
        return exc.user_message()
    return str(exc)

def _summary(output: PipelineOutput, index: int, *, print_text: bool = False) -> str:
    result = output.result
    lines = [
        f"[{index}] ✅ {result.title or '未命名'}",
        f"    平台：{result.platform or '-'}    方式：{result.method}    字数：{result.char_count}",
    ]
    if result.engine:
        lines.append(f"    引擎：{result.engine}")
    for fmt, path in output.files.items():
        lines.append(f"    {fmt.upper()}：{path}")
    for warning in result.warnings:
        lines.append(f"    ⚠️  {warning}")
    if print_text:
        lines.append("")
        lines.append(build_text(result))
    return "\n".join(lines)

def serve(host: str | None = None, port: int | None = None) -> int:
    """启动本地服务，供浏览器插件调用。"""
    config = ServiceConfig(
        host=host or DEFAULT_HOST,
        port=port or DEFAULT_PORT,
        out_dir=Path.cwd() / "output",
    )
    try:
        return run_service(config)
    except OSError as exc:
        print(f"启动失败：{exc}", file=sys.stderr)
        print(f"提示：端口 {config.port} 可能被占用，可用 --port 指定其他端口。", file=sys.stderr)
        return 1

def doctor() -> int:
    """环境自检。"""
    print(f"LinkWork {__version__} 环境自检")
    print("=" * 52)

    ok = True

    # Python
    print(f"Python        : {sys.version.split()[0]}  ({sys.executable})")

    # ffmpeg
    from . import media  # noqa: PLC0415

    try:
        ffmpeg = media.find_ffmpeg(required=False)
    except Exception as exc:  # noqa: BLE001
        ffmpeg = ""
        print(f"ffmpeg        : ❌ {exc}")
    if ffmpeg:
        print(f"ffmpeg        : ✅ {ffmpeg}")
    else:
        print("ffmpeg        : ❌ 未找到，音频转码不可用（pip install imageio-ffmpeg）")
        ok = False

    # yt-dlp
    try:
        import yt_dlp  # noqa: PLC0415

        print(f"yt-dlp        : ✅ {yt_dlp.version.__version__}")
    except Exception as exc:  # noqa: BLE001
        print(f"yt-dlp        : ❌ 未安装（{exc}）")
        ok = False

    # ASR 引擎
    from .asr import available_engines  # noqa: PLC0415

    for engine in available_engines():
        mark = "✅" if engine["available"] else "❌"
        detail = engine.get("default_model") or engine.get("detail") or ""
        print(f"ASR {engine['name']:<10}: {mark} {detail}")
        if engine["name"] == "faster-whisper" and not engine["available"]:
            ok = False
        if engine["name"] == "apple" and engine["available"]:
            # 系统语音识别需要用户授权，未授权时给出明确指引
            try:
                from .asr.apple_speech import AUTH_AUTHORIZED, AppleSpeechEngine  # noqa: PLC0415

                status = AppleSpeechEngine().authorization_status()
                if status != AUTH_AUTHORIZED:
                    print(
                        "               ⚠️  尚未授权系统语音识别，请先打开 GUI 运行一次并在弹窗中允许"
                        "（或在「系统设置 → 隐私与安全性 → 语音识别」中开启）"
                    )
            except Exception as exc:  # noqa: BLE001
                print(f"               ⚠️  无法读取授权状态：{exc}")

    # OCR
    try:
        from .ocr import ocr_available, unavailable_reason  # noqa: PLC0415

        if ocr_available():
            print("图片 OCR      : ✅ macOS Vision")
        else:
            print(f"图片 OCR      : ⚠️  {unavailable_reason()}")
    except Exception as exc:  # noqa: BLE001
        print(f"图片 OCR      : ⚠️  {exc}")

    print("=" * 52)
    print("结论：环境就绪 ✅" if ok else "结论：存在缺失项 ❌，请参考 README 安装依赖")
    return 0 if ok else 1

if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
