"""在子进程中执行语音识别，父进程负责超时与击杀。

隔离的原因见 `worker.py`：ctranslate2 的原生死锁无法从 Python 层中断，
只有把识别放到独立进程才能既保服务存活、又给用户明确的失败提示。

实现要点：**不使用管道读取**。子进程把进度与结果写进一个文件，父进程轮询
该文件与进程状态。这样父进程的任何一次读取都不会阻塞，超时判定必然生效
（早期用 `for line in proc.stdout` 的版本会永久卡在 read 上）。
"""

from __future__ import annotations

import json
import logging
import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Callable

from ..errors import ASRError
from ..models import Segment

log = logging.getLogger(__name__)

#: 整体超时（秒）
DEFAULT_TIMEOUT = 600.0
#: 多久没有任何新输出就判定卡死（秒）
SILENCE_TIMEOUT = 90.0
#: 轮询间隔（秒）
POLL_INTERVAL = 0.3

#: 进度里程碑（避免每条进度都写日志）
_LAST_BUCKET: dict[str, int] = {}


class IsolatedResult:
    """子进程返回的识别结果。"""

    def __init__(self, payload: dict) -> None:
        self.language: str = payload.get("language") or ""
        self.language_probability: float = float(payload.get("language_probability") or 0.0)
        self.duration: float = float(payload.get("duration") or 0.0)
        self.engine: str = payload.get("engine") or "faster-whisper"
        self.model: str = payload.get("model") or ""
        self.segments: list[Segment] = [
            Segment(start=float(s["start"]), end=float(s["end"]), text=str(s["text"]))
            for s in (payload.get("segments") or [])
        ]


def transcribe_isolated(
    audio_path: str | Path,
    *,
    model_size: str = "small",
    language: str | None = None,
    vad_filter: bool = True,
    on_progress: Callable[[float], None] | None = None,
    timeout: float = DEFAULT_TIMEOUT,
    silence_timeout: float = SILENCE_TIMEOUT,
    env_extra: dict[str, str] | None = None,
    workdir: Path | None = None,
    tasklog_path: str | None = None,
) -> IsolatedResult:
    """在子进程中做语音识别；超时或卡死则杀掉并抛 ASRError。"""
    audio_path = Path(audio_path)
    if not audio_path.exists():
        raise ASRError(f"音频文件不存在：{audio_path}")

    out_dir = Path(workdir) if workdir else audio_path.parent
    out_file = out_dir / f"asr_subprocess_{os.getpid()}_{int(time.time() * 1000)}.jsonl"

    cmd = [
        sys.executable,
        "-m",
        "linkwork.asr.worker",
        "--audio",
        str(audio_path),
        "--model",
        model_size,
        "--out",
        str(out_file),
    ]
    if language:
        cmd += ["--language", language]
    if not vad_filter:
        cmd += ["--no-vad"]
    if tasklog_path:
        # 让子进程把「加载模型 / VAD / 分段」等细节写进同一份任务日志
        cmd += ["--log", str(tasklog_path)]

    env = dict(os.environ)
    if env_extra:
        env.update(env_extra)
    env.setdefault("PYTHONUNBUFFERED", "1")

    log.info(
        "启动识别子进程（超时 %.0fs，静默 %.0fs）：%s",
        timeout,
        silence_timeout,
        audio_path.name,
    )
    log.debug("子进程命令：%s", " ".join(cmd))
    log.debug("子进程环境：HF_ENDPOINT=%s", env.get("HF_ENDPOINT", "(未设置)"))
    started = time.monotonic()
    stderr_file = out_dir / (out_file.stem + ".stderr")
    with stderr_file.open("wb") as err_fh:
        proc = subprocess.Popen(  # noqa: S603
            cmd,
            stdout=subprocess.DEVNULL,
            stderr=err_fh,
            env=env,
        )
    log.info("识别子进程已启动 PID=%s（结果文件 %s）", proc.pid, out_file.name)

    result_payload: dict | None = None
    error_message = ""
    _LAST_BUCKET["v"] = -1
    seen = 0
    last_output = time.monotonic()
    timed_out_reason = ""

    def drain() -> None:
        """把新增的 JSONL 行读进来（不阻塞）。"""
        nonlocal result_payload, error_message, seen, last_output
        if not out_file.exists():
            return
        try:
            # 注意：不能用 `for raw in fh` —— 文件迭代会禁用 tell()，
            # 抛 OSError: telling position disabled by next() call。
            with out_file.open("r", encoding="utf-8") as fh:
                fh.seek(seen)
                while True:
                    raw = fh.readline()
                    if not raw:
                        break
                    line = raw.strip()
                    if not line:
                        seen = fh.tell()
                        continue
                    try:
                        msg = json.loads(line)
                    except json.JSONDecodeError:
                        break  # 写了一半，下轮重读（不推进 seen）
                    seen = fh.tell()
                    last_output = time.monotonic()
                    kind = msg.get("type")
                    if kind == "progress":
                        ratio = float(msg.get("ratio") or 0.0)
                        if on_progress:
                            try:
                                on_progress(ratio)
                            except Exception:  # pragma: no cover
                                pass
                        # 每跨过 25% 打一条，便于判断卡在哪一段
                        bucket = int(ratio * 4)
                        if bucket > _LAST_BUCKET.get("v", -1):
                            _LAST_BUCKET["v"] = bucket
                            log.info("识别进度 %d%%（%.1fs）", bucket * 25, time.monotonic() - started)
                    elif kind == "result":
                        result_payload = msg
                    elif kind == "error":
                        error_message = str(msg.get("message") or "未知错误")
        except OSError:  # pragma: no cover
            pass

    try:
        while True:
            drain()
            if result_payload or error_message:
                break
            if proc.poll() is not None:
                # 进程已退出：再读一次尾部——结果往往就是在退出前刚写完的
                drain()
                if not result_payload and not error_message:
                    log.warning("识别子进程提前退出，退出码=%s", proc.returncode)
                break

            now = time.monotonic()
            if now - started > timeout:
                timed_out_reason = f"识别超过 {int(timeout)}s 未完成"
                break
            if now - last_output > silence_timeout:
                timed_out_reason = f"识别无响应超过 {int(silence_timeout)}s"
                break
            time.sleep(POLL_INTERVAL)
    finally:
        if proc.poll() is None:
            _kill(proc)

    # 注意：先读 stderr，再清理文件（否则错误信息会一起被删掉，只剩"未返回结果"）
    stderr_detail = _stderr_tail(stderr_file)
    elapsed = time.monotonic() - started
    if timed_out_reason:
        log.error("识别子进程超时被终止：%s（PID=%s，耗时 %.1fs）", timed_out_reason, proc.pid, elapsed)
        if stderr_detail:
            log.error("子进程 stderr：%s", stderr_detail)
    elif error_message:
        log.error("识别子进程报错（耗时 %.1fs）：%s", elapsed, error_message)
        if stderr_detail:
            log.error("子进程 stderr：%s", stderr_detail)
    elif result_payload is not None:
        log.info(
            "识别子进程完成（耗时 %.1fs，退出码 %s，分段 %d）",
            elapsed,
            proc.returncode,
            len(result_payload.get("segments") or []),
        )
    else:
        log.error("识别子进程无结果（退出码 %s，耗时 %.1fs）stderr=%s", proc.returncode, elapsed, stderr_detail or "空")
    _unlink(out_file)
    _unlink(stderr_file)

    if timed_out_reason:
        raise ASRError(
            f"语音识别卡死已终止（{timed_out_reason}）。这通常是模型解码异常，请重试；"
            "若反复出现可换用更小的模型或系统识别引擎。"
        )
    if error_message:
        raise ASRError(f"语音识别失败：{error_message}")
    if result_payload is None:
        raise ASRError(f"语音识别未返回结果（{stderr_detail or '子进程无输出'}）")

    return IsolatedResult(result_payload)


def _stderr_tail(path: Path) -> str:
    """进程结束后读取 stderr 尾部（此时文件已无写入者）。"""
    try:
        text = path.read_text(encoding="utf-8", errors="replace").strip()
    except OSError:
        return ""
    if not text:
        return ""
    return "；".join(text.splitlines()[-3:])


def _unlink(path: Path) -> None:
    try:
        path.unlink(missing_ok=True)
    except OSError:  # pragma: no cover
        pass


def _kill(proc: subprocess.Popen) -> None:
    if proc.poll() is not None:
        return
    log.warning("终止卡死的识别子进程 PID=%s", proc.pid)
    try:
        proc.kill()
        proc.wait(timeout=10)
    except Exception:  # noqa: BLE001, pragma: no cover
        pass
