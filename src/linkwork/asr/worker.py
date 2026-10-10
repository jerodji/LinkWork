"""语音识别的子进程执行器。

为什么需要子进程：ctranslate2 偶发原生层死锁（Python 线程永久阻塞、CPU 0%），
`threading` 无法中断这种阻塞。把识别放进独立进程后，父进程可以在超时后
直接 kill 掉它，服务本身永远不会被拖死。

协议：
    输入  --audio <wav> --model <size> [--language zh] [--no-vad]
    输出  stdout 为 JSON 行：{"type":"progress","ratio":0.42}
                             {"type":"result", ...}  /  {"type":"error","message":...}
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path


_OUT_PATH: Path | None = None


def _emit(payload: dict) -> None:
    """把一行 JSON 追加写入结果文件（父进程轮询读取，不依赖管道）。"""
    if _OUT_PATH is None:  # pragma: no cover
        sys.stdout.write(json.dumps(payload, ensure_ascii=False) + "\n")
        sys.stdout.flush()
        return
    with _OUT_PATH.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(payload, ensure_ascii=False) + "\n")
        fh.flush()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="LinkWork ASR 子进程")
    parser.add_argument("--audio", required=True)
    parser.add_argument("--model", default="small")
    parser.add_argument("--language", default="")
    parser.add_argument("--no-vad", action="store_true")
    parser.add_argument("--out", default="", help="结果写入的 JSONL 文件（父进程轮询读取）")
    parser.add_argument("--log", default="", help="父进程的任务日志文件，追加写入子进程细节")
    args = parser.parse_args(argv)

    global _OUT_PATH
    if args.out:
        _OUT_PATH = Path(args.out)
        _OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
        _OUT_PATH.write_text("", encoding="utf-8")

    audio = Path(args.audio)
    if not audio.exists():
        _emit({"type": "error", "message": f"音频文件不存在：{audio}"})
        return 2

    def on_progress(ratio: float) -> None:
        _emit({"type": "progress", "ratio": round(float(ratio), 4)})
        _stage_progress(ratio)

    tlog = None

    def _stage_progress(ratio: float) -> None:
        """每达到 25% 在任务日志里记一笔，便于判断识别阶段卡在哪。"""
        if tlog is None:
            return
        bucket = int(ratio * 4)
        if bucket > _stage_progress.last:
            _stage_progress.last = bucket
            tlog.note("进度", f"{bucket * 25}%")

    _stage_progress.last = -1  # type: ignore[attr-defined]

    if args.log:
        try:
            from ..diagnostics import TaskLog

            tlog = TaskLog.append_to(args.log, prefix="识别子进程")
            tlog.note("PID", os.getpid())
            tlog.note("音频文件", f"{audio.name}（{audio.stat().st_size / 1048576:.2f} MB）")
            tlog.note("模型", args.model)
            tlog.note("VAD", "关闭" if args.no_vad else "开启")
            tlog.note("语言", args.language or "自动检测")
        except Exception:  # noqa: BLE001
            tlog = None

    def stage(name: str) -> None:
        if tlog is not None:
            tlog.stage(name)

    try:
        from .faster_whisper_engine import FasterWhisperEngine

        stage("加载模型")
        engine = FasterWhisperEngine(
            model_size=args.model,
            vad_filter=not args.no_vad,
        )
        stage("识别中")
        output = engine.transcribe(
            audio,
            language=args.language or None,
            on_progress=on_progress,
        )
        if tlog is not None:
            tlog.note("识别语言", f"{output.language} ({output.language_probability:.2f})")
            tlog.note("音频时长", f"{output.duration:.1f}s")
            tlog.note("分段数", len(output.segments))
            tlog.note("引擎/模型", f"{output.engine} / {output.model}")
    except Exception as exc:  # noqa: BLE001
        if tlog is not None:
            tlog.error(f"子进程识别失败：{type(exc).__name__}", exc)
        _emit({"type": "error", "message": f"{type(exc).__name__}: {exc}"})
        return 1

    _emit(
        {
            "type": "result",
            "language": output.language,
            "language_probability": output.language_probability,
            "duration": output.duration,
            "engine": output.engine,
            "model": output.model,
            "segments": [
                {"start": s.start, "end": s.end, "text": s.text} for s in output.segments
            ],
        }
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
