#!/usr/bin/env python3
"""LinkWork native messaging host。

浏览器扩展本身没有权限执行 shell 命令，唯一受支持的通道就是这个
native messaging host：浏览器按 manifest 注册的路径启动本脚本，
双方用「4 字节小端长度 + JSON」在 stdin/stdout 上对话。

本脚本只做一件小事，且只做白名单动作：

    {"action": "ping"}   → 报告服务是否在跑
    {"action": "start"}  → 没跑就按 scripts/service.sh 的同样方式拉起来，等到健康为止

安全边界：
  - 只允许上述动作，不接受任意命令/路径（扩展被注入也拿不到 shell）；
  - 启动命令固定为 `<项目根>/.venv/bin/python -m linkwork serve --port <端口>`；
  - 端口只接受整数，且通过环境变量传给它自己的子进程。

协议细节（Chrome/Edge 要求）：
  - stdout 只能写协议帧，任何日志必须走 stderr 或日志文件；
  - 消息长度上限 1MB，超过即视为非法连接。

用法（由浏览器调用，不需要手动执行）：
    linkwork-host-wrapper.sh chrome-extension://<id>/
"""

from __future__ import annotations

import json
import os
import socket
import struct
import subprocess
import sys
import tempfile
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

HOST_DIR = Path(__file__).resolve().parent
try:
    ROOT = (HOST_DIR / "..").resolve(strict=True)
except OSError:
    ROOT = HOST_DIR.parent

PYTHON = ROOT / ".venv" / "bin" / "python"
LOG_DIR = Path.home() / "Library" / "Logs"
HOST_LOG = LOG_DIR / "LinkWork-native-host.log"
SERVICE_LOG = LOG_DIR / "LinkWork-service.log"
PID_FILE = LOG_DIR / "LinkWork-service.pid"
DEFAULT_PORT = 17890
MAX_MESSAGE = 1024 * 1024
# 日志实际落点：默认 ~/Library/Logs，写不了就退到 native_host/logs/
# （被浏览器以受限身份启动时，~ 可能不可写，此时「没有任何日志」会非常难查）
def _resolve_log_file() -> Path:
    candidates = [HOST_LOG, HOST_DIR / "logs" / "LinkWork-native-host.log"]
    for candidate in candidates:
        try:
            candidate.parent.mkdir(parents=True, exist_ok=True)
            with candidate.open("a", encoding="utf-8"):
                pass
            return candidate
        except OSError:
            continue
    return HOST_LOG


LOG_FILE = _resolve_log_file()


def log(message: str) -> None:
    """写日志到文件（stderr 在浏览器里看不到，stdout 被协议占用）。"""
    try:
        LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
        stamp = time.strftime("%Y-%m-%d %H:%M:%S")
        with LOG_FILE.open("a", encoding="utf-8") as fh:
            fh.write(f"{stamp} {message}\n")
    except OSError:
        pass


# ------------------------------------------------------------------ 协议


def read_message() -> dict[str, Any] | None:
    raw = sys.stdin.buffer.read(4)
    if len(raw) < 4:
        return None
    (length,) = struct.unpack("<I", raw)
    if length == 0 or length > MAX_MESSAGE:
        log(f"非法消息长度：{length}")
        return None
    payload = sys.stdin.buffer.read(length)
    if len(payload) < length:
        return None
    try:
        return json.loads(payload.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        log(f"消息解析失败：{exc}")
        return None


def write_message(message: dict[str, Any]) -> None:
    data = json.dumps(message, ensure_ascii=False).encode("utf-8")
    sys.stdout.buffer.write(struct.pack("<I", len(data)) + data)
    sys.stdout.buffer.flush()


# ------------------------------------------------------------------ 服务状态


def port_of(message: dict[str, Any] | None = None) -> int:
    """端口优先级：消息 > 环境变量 LINKWORK_PORT > 默认 17890（与 service.sh 一致）"""
    candidates: list[Any] = []
    if message:
        candidates.append(message.get("port"))
    candidates.append(os.environ.get("LINKWORK_PORT"))
    candidates.append(DEFAULT_PORT)
    for value in candidates:
        try:
            port = int(str(value).strip())
        except (TypeError, ValueError):
            continue
        if 1 <= port <= 65535:
            return port
    return DEFAULT_PORT


def is_listening(port: int, timeout: float = 0.35) -> bool:
    """用 TCP 连一下判断服务在不在——不需要 token，也不受自定义头限制。"""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.settimeout(timeout)
        return sock.connect_ex(("127.0.0.1", port)) == 0


def running_pid() -> int | None:
    try:
        pid = int(PID_FILE.read_text(encoding="utf-8").strip())
    except (OSError, ValueError):
        return None
    try:
        os.kill(pid, 0)
    except OSError:
        return None
    return pid


# ---------------------------------------------------------------- 缓存目录
# 服务的下载/识别工作区必须真正可写，否则提取会在
#   PermissionError: [Errno 1] Operation not permitted: .../Caches/LinkWork/media/...
# 上失败。这类失败通常来自「服务被某个受限环境（沙箱/IDE 终端）启动」。
# 与其让用户在提取时才发现，不如启动前就探测：默认目录不可写就换一个可写的，
# 并把实际用的目录回报给界面（服务日志里也会打印）。

def _cache_root() -> Path:
    """与 linkwork.cache 的默认值保持一致：优先 LINKWORK_CACHE_DIR，其次 ~/Library/Caches/LinkWork/media"""
    env = os.environ.get("LINKWORK_CACHE_DIR", "").strip()
    if env:
        return Path(env).expanduser()
    return Path.home() / "Library" / "Caches" / "LinkWork" / "media"


def _dir_writable(path: Path) -> bool:
    try:
        path.mkdir(parents=True, exist_ok=True)
        probe = Path(tempfile.mkdtemp(prefix="linkwork_probe_", dir=str(path)))
        probe.rmdir()
        return True
    except OSError:
        return False


def pick_cache_dir() -> tuple[Path, str]:
    """返回 (可写的缓存根目录, 说明)。默认目录可写就用默认的。"""
    default = _cache_root()
    if _dir_writable(default):
        return default, ""
    reason = f"默认缓存目录不可写：{default}"
    candidates = [
        Path.home() / "Library" / "Caches" / "LinkWork-media",
        Path(tempfile.gettempdir()) / "LinkWork-media",
    ]
    for candidate in candidates:
        if _dir_writable(candidate):
            note = f"{reason}，已改用 {candidate}"
            log(note)
            return candidate, note
    return default, f"{reason}，且找不到可替代目录"


@contextmanager
def env_overrides(**values: str) -> Iterator[None]:
    """临时给子进程加环境变量（不影响 host 自己）。"""
    backup: dict[str, str | None] = {key: os.environ.get(key) for key in values}
    os.environ.update({k: v for k, v in values.items() if v})
    try:
        yield
    finally:
        for key, old in backup.items():
            if old is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = old


def start_service(port: int, wait_seconds: float = 40.0) -> dict[str, Any]:
    """拉起本地服务并等到端口可连；已在跑则直接返回。"""
    if is_listening(port):
        return {"started": False, "alreadyRunning": True, "pid": running_pid()}

    if not PYTHON.exists():
        return {
            "started": False,
            "error": f"找不到虚拟环境解释器：{PYTHON}。请先在项目目录运行 ./scripts/setup.sh",
        }

    cache_dir, cache_note = pick_cache_dir()
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    command = [str(PYTHON), "-m", "linkwork", "serve", "--port", str(port)]
    log(f"启动本地服务：{' '.join(command)}（工作目录 {ROOT}，缓存 {cache_dir}）")
    try:
        with env_overrides(LINKWORK_CACHE_DIR=str(cache_dir)):
            with SERVICE_LOG.open("a", encoding="utf-8") as logfile:
                # start_new_session：让服务脱离浏览器进程组，浏览器退出不会连带杀掉它
                proc = subprocess.Popen(  # noqa: S603 - 命令固定，无用户输入
                    command,
                    cwd=str(ROOT),
                    stdin=subprocess.DEVNULL,
                    stdout=logfile,
                    stderr=subprocess.STDOUT,
                    start_new_session=True,
                )
    except OSError as exc:
        return {"started": False, "error": f"启动失败：{exc}"}

    try:
        PID_FILE.write_text(f"{proc.pid}\n", encoding="utf-8")
    except OSError as exc:
        log(f"写 pid 文件失败（不影响服务运行）：{exc}")

    deadline = time.time() + wait_seconds
    while time.time() < deadline:
        if proc.poll() is not None:
            return {
                "started": False,
                "pid": proc.pid,
                "cacheDir": str(cache_dir),
                "error": f"服务进程已退出（退出码 {proc.returncode}），请查看日志：{SERVICE_LOG}",
            }
        if is_listening(port):
            log(f"服务已就绪（PID {proc.pid}，端口 {port}，缓存 {cache_dir}）")
            return {
                "started": True,
                "pid": proc.pid,
                "cacheDir": str(cache_dir),
                "cacheNote": cache_note,
                "waitedMs": int((wait_seconds - (deadline - time.time())) * 1000),
            }
        time.sleep(0.3)

    return {
        "started": False,
        "pid": proc.pid,
        "cacheDir": str(cache_dir),
        "error": f"等待 {int(wait_seconds)}s 后端口 {port} 仍未就绪，请查看日志：{SERVICE_LOG}",
    }


# ------------------------------------------------------------------ 入口


def handle(message: dict[str, Any]) -> dict[str, Any]:
    action = str(message.get("action") or "").strip()
    port = port_of(message)
    if action == "ping":
        return {"ok": True, "action": action, "running": is_listening(port), "port": port}
    if action == "start":
        result = start_service(port)
        return {"ok": "error" not in result, "action": action, "port": port, **result}
    return {"ok": False, "error": f"不支持的动作：{action!r}（只允许 ping / start）"}


def main() -> int:
    log(
        f"host 启动（pid {os.getpid()}，日志 {LOG_FILE}，参数 {sys.argv[1:] or '无'}）"
    )
    message = read_message()
    if message is None:
        # 收到坏帧/空帧时必须回一条消息再退出。
        # 直接 return 的话浏览器只会告诉扩展「Native host has exited」，
        # 而真正的原因（stdin 被提前关掉、帧头长度不对）就完全看不见了。
        log("没有收到合法消息，返回错误帧后退出")
        write_message({
            "ok": False,
            "error": "本机启动器没有收到合法的消息帧（stdin 可能被提前关闭）。"
                     f"详情见日志：{LOG_FILE}",
        })
        return 0
    try:
        response = handle(message)
    except Exception as exc:  # noqa: BLE001 - native host 必须永远回一条消息，不能崩
        log(f"处理 {message!r} 时异常：{type(exc).__name__}: {exc}")
        response = {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
    log(f"{message.get('action')!r} → {json.dumps(response, ensure_ascii=False)}")
    write_message(response)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
