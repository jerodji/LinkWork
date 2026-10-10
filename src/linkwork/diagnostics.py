"""任务级诊断日志。

服务主日志是「所有任务混在一起」的流水账，排查单个失败任务时噪音很大。
这里为每个任务单独落一个日志文件，记录：
  - 运行环境（版本、Python、平台）
  - 任务输入（链接、参数，敏感字段脱敏）
  - 各阶段耗时
  - 失败时的完整异常栈

用法：
    with task_log(job_id, kind="transcribe", inputs={...}) as tlog:
        tlog.stage("下载媒体")
        ...
        tlog.note("媒体大小", "3.2 MB")
"""

from __future__ import annotations

import json
import logging
import os
import platform
import sys
import time
import traceback
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

from . import __version__

log = logging.getLogger(__name__)

#: 每个任务保留的日志文件数上限（超出后清理最旧的）
KEEP_FILES = 60

_SENSITIVE_KEYS = ("cookie", "token", "secret", "signature", "authorization", "password")


def logs_root() -> Path:
    """任务日志目录（与媒体缓存同根，便于一起清理）。"""
    from .cache import cache_root

    root = cache_root().parent / "tasklogs"
    root.mkdir(parents=True, exist_ok=True)
    return root


def scrub(payload: dict[str, Any]) -> dict[str, Any]:
    """脱敏：cookie/token 等只保留长度，避免日志泄漏凭据。"""
    out: dict[str, Any] = {}
    for key, value in payload.items():
        lowered = key.lower()
        if any(s in lowered for s in _SENSITIVE_KEYS):
            out[key] = f"<{type(value).__name__} 已脱敏，长度 {len(str(value))}>"
        elif isinstance(value, dict):
            out[key] = scrub(value)
        elif isinstance(value, str) and len(value) > 300:
            out[key] = value[:300] + f"…（共 {len(value)} 字符）"
        else:
            out[key] = value
    return out


def environment_lines() -> list[str]:
    """运行环境快照。"""
    lines = [
        f"LinkWork {__version__}",
        f"Python {sys.version.split()[0]} ({platform.machine()})",
        f"平台 {platform.platform()}",
        f"PID {os.getpid()}",
        f"工作目录 {os.getcwd()}",
    ]
    try:
        from .media import find_ffmpeg

        lines.append(f"ffmpeg {find_ffmpeg() or '未找到'}")
    except Exception as exc:  # noqa: BLE001
        lines.append(f"ffmpeg 探测失败：{exc}")
    try:
        from .asr import available_engines

        engines = ", ".join(f"{e['name']}({'可用' if e['available'] else '不可用'})" for e in available_engines())
        lines.append(f"ASR 引擎 {engines}")
    except Exception as exc:  # noqa: BLE001
        lines.append(f"ASR 探测失败：{exc}")
    return lines


class TaskLog:
    """单个任务的诊断日志。"""

    def __init__(self, job_id: str, kind: str, inputs: dict[str, Any] | None = None) -> None:
        self.job_id = job_id
        self.kind = kind
        self.started = time.monotonic()
        #: 阶段耗时（阶段名 → 累计到该阶段开始时的秒数），完成时落盘
        self._stages: list[tuple[str, float]] = []
        self._last_stage = ""
        self.path = logs_root() / f"{kind}_{job_id}.log"
        # 先清空（新任务重新开始），再以纯 O_APPEND 打开。
        # 注意不能把 O_TRUNC 和 O_APPEND 同时用：父进程以追加模式打开后，
        # O_TRUNC 语义会让子进程的追加写入被截掉（实测丢行）。
        self.path.write_text("", encoding="utf-8")
        # O_APPEND + 单次 write 保证父/子进程的每次写入都是原子追加，不会交错。
        self._fd = os.open(self.path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o644)

    @classmethod
    def append_to(cls, path: str | Path, prefix: str = "subprocess") -> "TaskLog":
        """子进程用：以追加方式写父进程创建的那份任务日志。"""
        obj = cls.__new__(cls)  # 绕过 __init__（不重建文件），但字段必须补齐
        obj.job_id = Path(path).stem
        obj.kind = prefix
        obj.started = time.monotonic()
        obj.path = Path(path)
        obj._stages: list[tuple[str, float]] = []
        obj._last_stage = ""
        obj._fd = os.open(obj.path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o644)
        obj._write("")
        obj._write("-" * 68)
        obj._write(f"▼ 另一个进程（{prefix}）开始写入，时间轴从 0 重新计")
        obj._write("-" * 68)
        return obj

    def _write(self, text: str) -> None:
        elapsed = time.monotonic() - self.started
        # 带上墙钟时间：父进程与子进程写的是同一个文件，
        # 两边各自的相对时间轴会让人误判先后顺序。
        stamp = time.strftime("%H:%M:%S")
        line = f"[{stamp} +{elapsed:6.2f}s] {text}\n".encode("utf-8")
        try:
            os.write(self._fd, line)  # O_APPEND + 单次 write ⇒ 原子追加
        except OSError:  # pragma: no cover
            pass

    def header(self, inputs: dict[str, Any] | None) -> None:
        self._write("=" * 68)
        self._write(f"任务 {self.job_id}（{self.kind}）")
        self._write("=" * 68)
        for line in environment_lines():
            self._write(f"  {line}")
        if inputs:
            self._write("--- 输入 ---")
            for key, value in scrub(inputs).items():
                self._write(f"  {key}: {value}")

    def stage(self, name: str) -> None:
        if name != self._last_stage:
            self._stages.append((name, time.monotonic() - self.started))
            self._last_stage = name
        self._write(f"▶ {name}")

    @property
    def stages(self) -> list[str]:
        """形如 ["下载媒体@0.1s", "语音识别@5.0s"]，供摘要与日志末尾使用。"""
        return [f"{name}@{elapsed:.1f}s" for name, elapsed in self._stages]

    def note(self, label: str, value: Any) -> None:
        self._write(f"    {label}: {value}")

    def warn(self, message: str) -> None:
        self._write(f"    ⚠ {message}")

    def error(self, message: str, exc: BaseException | None = None) -> None:
        self._write(f"    ✖ {message}")
        if self._stages:
            # 失败时同样写出阶段耗时：既能看出卡在哪一步，也让摘要能读到
            self._write(f"    阶段耗时：{' → '.join(self.stages)}")
        if exc is not None:
            for line in traceback.format_exception(type(exc), exc, exc.__traceback__):
                for sub in line.rstrip().splitlines():
                    self._write(f"      {sub}")

    def done(self, summary: dict[str, Any] | None = None) -> None:
        self._write(f"✔ 完成，耗时 {time.monotonic() - self.started:.2f}s")
        if self._stages:
            self._write(f"  阶段耗时：{' → '.join(self.stages)}")
        if summary:
            for key, value in summary.items():
                self._write(f"  {key}: {value}")

    def close(self) -> None:
        if self._stages:
            _STAGE_REGISTRY[self.job_id] = self.stages
            # 简单限流，避免长期运行内存增长
            while len(_STAGE_REGISTRY) > 200:
                _STAGE_REGISTRY.pop(next(iter(_STAGE_REGISTRY)))
        try:
            os.close(self._fd)
        except Exception:  # noqa: BLE001, pragma: no cover
            pass


@contextmanager
def task_log(
    job_id: str,
    *,
    kind: str,
    inputs: dict[str, Any] | None = None,
) -> Iterator[TaskLog]:
    """打开一个任务日志；无论成功失败都会落盘并保留完整信息。"""
    tlog: TaskLog | None = None
    try:
        tlog = TaskLog(job_id, kind, inputs)
        tlog.header(inputs)
    except Exception as exc:  # noqa: BLE001
        log.debug("创建任务日志失败（不影响主流程）：%s", exc)
        tlog = None  # type: ignore[assignment]

    try:
        if tlog is None:
            # 退化为空实现，避免调用方到处判空
            yield _NullTaskLog()  # type: ignore[misc]
        else:
            yield tlog
    except Exception as exc:  # noqa: BLE001
        if tlog is not None:
            tlog.error("任务失败", exc)
        raise
    finally:
        if tlog is not None:
            tlog.close()
            _prune()


class _NullTaskLog:
    """写日志失败时的占位对象（保证主流程不受影响）。"""

    path = Path("/dev/null")

    def stage(self, name: str) -> None: 
        """空实现。"""

    def note(self, label: str, value: Any) -> None:
        """空实现。"""

    def warn(self, message: str) -> None:
        """空实现。"""

    def error(self, message: str, exc: BaseException | None = None) -> None:
        """空实现。"""

    def done(self, summary: dict[str, Any] | None = None) -> None:
        """空实现。"""


def _prune() -> None:
    """只保留最近的若干日志文件。"""
    try:
        root = logs_root()
        files = sorted(root.glob("*.log"), key=lambda p: p.stat().st_mtime, reverse=True)
        for old in files[KEEP_FILES:]:
            old.unlink(missing_ok=True)
    except Exception:  # noqa: BLE001, pragma: no cover
        pass


#: 运行中/刚结束的任务阶段耗时（避免依赖日志解析）
_STAGE_REGISTRY: dict[str, list[str]] = {}


def summarize(path: Path, max_scan_bytes: int = 60_000) -> dict[str, Any]:
    """从任务日志里提炼出「结果 / 失败原因 / 阶段耗时」，供列表直接展示。

    只读文件前 60KB（足够覆盖关键行），避免大日志拖慢列表接口。
    """
    info: dict[str, Any] = {"outcome": "unknown", "detail": "", "stages": [], "job_id": "", "kind": ""}
    try:
        with path.open("r", encoding="utf-8", errors="replace") as fh:
            head = fh.read(max_scan_bytes)
    except OSError:  # pragma: no cover
        return info

    lines = head.splitlines()
    # 阶段耗时可能出现在「完成」行或失败行
    for line in lines:
        if "阶段耗时：" in line:
            tail = line.split("阶段耗时：", 1)[1].strip()
            info["stages"] = [seg.strip() for seg in tail.split("→") if seg.strip()]
    for line in lines:
        if "任务失败" in line or "✖" in line:
            # 失败行：优先取异常摘要（下一行通常是 traceback 的最后一句）
            info["outcome"] = "error"
            text = line.split("] ", 1)[-1].strip()
            if text and text not in ("任务失败",):
                info["detail"] = text
        if "✖" in line and "任务失败" not in line:
            info["outcome"] = "error"
            info["detail"] = line.split("✖", 1)[-1].strip() or info["detail"]

    # 末尾几行找异常类型（如 LinkWorkError: xxx）
    for line in reversed(lines[-40:]):
        stripped = line.split("] ", 1)[-1].strip()
        if stripped and ("Error" in stripped or "错误" in stripped) and not stripped.startswith("子进程"):
            info["detail"] = stripped
            info["outcome"] = "error"
            break

    if any("✔ 完成" in line for line in lines):
        info["outcome"] = "done"
        for line in reversed(lines):
            if "✔ 完成" in line:
                info["detail"] = line.split("] ", 1)[-1].strip()
                break

    # 阶段耗时：优先用内存注册表（最准），否则从日志行解析
    if info["job_id"] and _STAGE_REGISTRY.get(info["job_id"]):
        info["stages"] = list(_STAGE_REGISTRY[info["job_id"]])
    else:
        for line in lines:
            if "阶段耗时：" in line:
                tail = line.split("阶段耗时：", 1)[1].strip()
                info["stages"] = [seg.strip() for seg in tail.split("→") if seg.strip()]
                break

    if not info["job_id"]:
        for line in lines[:5]:
            if "任务 " in line and "（" in line:
                m = line.split("任务 ", 1)[1]
                info["job_id"] = m.split("（", 1)[0].strip()
                info["kind"] = m.split("（", 1)[1].split("）", 1)[0].strip() if "（" in m else ""
                break
    return info


def list_task_logs(limit: int = 20, with_summary: bool = True) -> list[dict[str, Any]]:
    """列出最近的任务日志（供 /api/diagnostics 使用）。"""
    try:
        root = logs_root()
    except Exception:  # noqa: BLE001
        return []
    out: list[dict[str, Any]] = []
    for path in sorted(root.glob("*.log"), key=lambda p: p.stat().st_mtime, reverse=True)[:limit]:
        try:
            stat = path.stat()
        except OSError:  # pragma: no cover
            continue
        item: dict[str, Any] = {
            "name": path.name,
            "bytes": stat.st_size,
            "modified": stat.st_mtime,
        }
        if with_summary:
            item.update(summarize(path))
        out.append(item)
    return out


def read_task_log(name: str, max_bytes: int = 200_000) -> str:
    """读取指定任务日志（做路径校验，避免越权读取）。"""
    root = logs_root().resolve()
    target = (root / Path(name).name).resolve()
    if target.parent != root or not target.exists():
        raise FileNotFoundError(name)
    text = target.read_text(encoding="utf-8", errors="replace")
    return text[-max_bytes:]


# --------------------------------------------------------------------------- #
# 当前任务日志的全局访问点（线程安全）
# 各处理阶段不必逐层传参，直接 current_task_log() 即可写入本任务的日志。
# --------------------------------------------------------------------------- #
import contextvars  # noqa: E402

_CURRENT: contextvars.ContextVar["TaskLog | _NullTaskLog | None"] = contextvars.ContextVar(
    "linkwork_task_log", default=None
)


def set_current_task_log(tlog: "TaskLog | _NullTaskLog | None") -> object:
    """设置当前线程/上下文的任务日志，返回可用于恢复的 token。"""
    return _CURRENT.set(tlog)


def reset_current_task_log(token: object) -> None:
    """恢复之前的任务日志。"""
    try:
        _CURRENT.reset(token)  # type: ignore[arg-type]
    except Exception:  # noqa: BLE001, pragma: no cover
        pass


def current_task_log() -> "TaskLog | _NullTaskLog":
    """取当前任务日志；没有则返回空实现（调用方无需判空）。"""
    got = _CURRENT.get()
    return got if got is not None else _NullTaskLog()


def service_log_path() -> Path:
    """本地服务主日志路径（scripts/service.sh 使用同一位置）。"""
    return Path.home() / "Library" / "Logs" / "LinkWork-service.log"


#: 本次服务进程的启动时刻（用于过滤掉历史噪音）
_SERVICE_STARTED_AT = time.strftime("%Y-%m-%d %H:%M:%S")

#: 这些第三方日志对排查本项目问题没有价值，只会淹没关键信息
_NOISY_TOKENS = (
    "faster_whisper: Processing audio",
    "faster_whisper: VAD filter removed",
    "faster_whisper: Detected language",
    "resource_tracker",
    "warnings.warn",
    "huggingface.co 不可达",
)


def tail_service_log(max_lines: int = 120, since_start: bool = True) -> list[str]:
    """读取服务主日志尾部，供 /api/diagnostics 一并展示。

    `since_start=True` 时只返回本次进程启动之后的记录——否则会把上次运行的
    历史报错也带出来，让人误以为当前仍在发生。
    """
    path = service_log_path()
    try:
        with path.open("r", encoding="utf-8", errors="replace") as fh:
            lines = [l.rstrip("\n") for l in fh]
    except OSError:
        return []

    out: list[str] = []
    in_current_run = not since_start  # 无过滤时视为全部可见
    for line in lines:
        stamp = _line_timestamp(line)
        if stamp is not None:
            # 只有带时间戳的行才能判断属于哪一次运行；
            # traceback 的续行没有时间戳，沿用上一行的时间戳归属。
            in_current_run = (not since_start) or stamp >= _SERVICE_STARTED_AT
        if not in_current_run:
            continue
        if any(tok in line for tok in _NOISY_TOKENS):
            continue  # 第三方刷屏日志
        out.append(line)
    return out[-max_lines:]


def _line_timestamp(line: str) -> str | None:
    """取出日志行开头的时间戳（形如 2026-10-08 17:34:45,123）。"""
    if len(line) >= 19 and line[4] == "-" and line[13] == ":" and line[10] == " ":
        return line[:19]
    return None
