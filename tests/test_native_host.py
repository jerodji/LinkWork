"""native messaging host 的单元测试。

覆盖三件事：
  1. 协议：4 字节小端长度前缀 + JSON，读写都要对得上；
  2. 动作白名单：只认 ping / start，别的一律拒绝（扩展被注入也拿不到 shell）；
  3. 缓存兜底：默认缓存目录不可写时，必须换到可写目录并给出说明
     —— 真实踩过：服务被沙箱启动时，提取会报
        PermissionError: [Errno 1] Operation not permitted: .../Caches/LinkWork/media/...
"""

from __future__ import annotations

import importlib.util
import io
import json
import struct
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
HOST_PATH = ROOT / "native_host" / "linkwork_host.py"


@pytest.fixture(scope="module")
def host():
    """按文件路径加载 host 模块（它不是包的一部分）。"""
    spec = importlib.util.spec_from_file_location("linkwork_native_host", HOST_PATH)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def frame(payload: dict) -> bytes:
    data = json.dumps(payload).encode("utf-8")
    return struct.pack("<I", len(data)) + data


# --------------------------------------------------------------------- 协议


def test_reads_length_prefixed_message(host):
    host.sys.stdin = type("FakeStdin", (), {"buffer": io.BytesIO(frame({"action": "ping"}))})()
    assert host.read_message() == {"action": "ping"}


def test_write_message_is_length_prefixed(host):
    buffer = io.BytesIO()
    host.sys.stdout = type("FakeStdout", (), {"buffer": buffer})()
    host.write_message({"ok": True, "action": "ping"})
    raw = buffer.getvalue()
    (length,) = struct.unpack("<I", raw[:4])
    assert length == len(raw) - 4
    assert json.loads(raw[4:].decode("utf-8")) == {"ok": True, "action": "ping"}


def test_rejects_oversized_message(host, monkeypatch):
    oversized = struct.pack("<I", host.MAX_MESSAGE + 1)
    host.sys.stdin = type("FakeStdin", (), {"buffer": io.BytesIO(oversized)})()
    assert host.read_message() is None


# ----------------------------------------------------------- 动作白名单


def test_only_ping_and_start_are_allowed(host, monkeypatch):
    monkeypatch.setattr(host, "is_listening", lambda port, timeout=0.35: False)
    result = host.handle({"action": "rm -rf /"})
    assert result["ok"] is False
    assert "不支持的动作" in result["error"]


def test_ping_reports_running_state(host, monkeypatch):
    monkeypatch.setattr(host, "is_listening", lambda port, timeout=0.35: True)
    result = host.handle({"action": "ping"})
    assert result == {"ok": True, "action": "ping", "running": True, "port": host.DEFAULT_PORT}


def test_start_does_not_spawn_when_already_running(host, monkeypatch):
    monkeypatch.setattr(host, "is_listening", lambda port, timeout=0.35: True)
    called = []
    monkeypatch.setattr(host.subprocess, "Popen", lambda *a, **k: called.append(a) or None)
    result = host.handle({"action": "start"})
    assert result["ok"] is True
    assert result["alreadyRunning"] is True
    assert called == [], "服务已在跑时不应重复拉起"


def test_port_comes_from_message_then_env(host, monkeypatch):
    assert host.port_of({"port": 12345}) == 12345
    monkeypatch.setenv("LINKWORK_PORT", "18000")
    assert host.port_of({}) == 18000
    assert host.port_of({"port": "不是端口"}) == 18000
    monkeypatch.delenv("LINKWORK_PORT", raising=False)
    assert host.port_of({}) == host.DEFAULT_PORT


# ----------------------------------------------------------- 缓存兜底


def test_default_cache_dir_is_used_when_writable(host, tmp_path, monkeypatch):
    good = tmp_path / "cache"
    monkeypatch.setattr(host, "_cache_root", lambda: good)
    chosen, note = host.pick_cache_dir()
    assert chosen == good
    assert note == ""


def test_unwritable_cache_dir_falls_back(host, tmp_path, monkeypatch):
    """默认目录不可写时：必须换目录，并且说明原因（而不是让它到提取时才炸）。"""
    blocked = tmp_path / "blocked"
    blocked.mkdir()
    blocked.chmod(0o500)  # 只读
    monkeypatch.setattr(host, "_cache_root", lambda: blocked)
    monkeypatch.setattr(host.tempfile, "gettempdir", lambda: str(tmp_path / "fallback-tmp"))

    chosen, note = host.pick_cache_dir()

    if chosen == blocked:  # 以 root 运行时只读目录仍可写，跳过这条断言
        pytest.skip("当前用户可写只读目录（很可能以 root 运行）")
    assert "不可写" in note
    assert chosen != blocked
    assert host._dir_writable(chosen), "兜底目录必须真的可写"


def test_env_override_is_restored(host, monkeypatch):
    monkeypatch.delenv("LINKWORK_CACHE_DIR", raising=False)
    with host.env_overrides(LINKWORK_CACHE_DIR="/tmp/whatever"):
        assert host.os.environ["LINKWORK_CACHE_DIR"] == "/tmp/whatever"
    assert "LINKWORK_CACHE_DIR" not in host.os.environ
