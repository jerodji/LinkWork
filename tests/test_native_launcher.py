"""C 版启动器的测试。

为什么要有这个二进制：浏览器在 macOS 上执行 shell 脚本会被系统安全策略拦掉，
浏览器只会给扩展一句「Native host has exited」，连日志都写不出来。
这台机器上能正常工作的其它原生程序（Codex、夸克）都是 Mach-O 可执行文件。
所以注册脚本会优先把 linkwork-launcher.c 编译成二进制并使用它。

这些用例在临时目录里自行编译一份启动器来跑，因此不依赖仓库里是否已经编译过。
"""

from __future__ import annotations

import importlib.util
import json
import shutil
import struct
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
LAUNCHER_SRC = ROOT / "native_host" / "linkwork-launcher.c"
HOST_SCRIPT = ROOT / "native_host" / "linkwork_host.py"
INSTALLER = ROOT / "scripts" / "install_native_host.py"

COMPILER = shutil.which("cc") or shutil.which("clang")
requires_compiler = pytest.mark.skipif(COMPILER is None, reason="没有可用的 C 编译器（cc/clang）")


@pytest.fixture(scope="module")
def installer():
    spec = importlib.util.spec_from_file_location("linkwork_installer", INSTALLER)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def launcher(tmp_path_factory):
    """在临时目录编译一份启动器 + host.conf，返回 (二进制路径, 日志路径)。"""
    if COMPILER is None:
        pytest.skip("没有可用的 C 编译器")
    workdir = tmp_path_factory.mktemp("launcher")
    binary = workdir / "linkwork-host"
    build = subprocess.run(
        [COMPILER, "-O2", "-Wall", "-o", str(binary), str(LAUNCHER_SRC)],
        capture_output=True,
        text=True,
    )
    assert build.returncode == 0, build.stderr
    log = workdir / "host.log"
    (workdir / "host.conf").write_text(
        "\n".join([
            f"python={sys.executable}",
            f"host={HOST_SCRIPT}",
            f"log={log}",
        ]) + "\n",
        encoding="utf-8",
    )
    return binary, log


def call_host(binary: Path, payload: dict, timeout: int = 30) -> dict:
    body = json.dumps(payload).encode("utf-8")
    frame = struct.pack("<I", len(body)) + body
    result = subprocess.run(
        [str(binary), "chrome-extension://test-extension-id/"],
        input=frame,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=timeout,
    )
    raw = result.stdout
    assert len(raw) >= 4, f"没有协议帧输出（rc={result.returncode}, stderr={result.stderr[:200]!r}）"
    (length,) = struct.unpack("<I", raw[:4])
    return json.loads(raw[4:4 + length].decode("utf-8"))


@requires_compiler
def test_launcher_produces_macho_binary(launcher):
    binary, _ = launcher
    assert binary.exists() and binary.stat().st_size > 0
    # macOS 上必须是真正的可执行文件，而不是脚本
    with binary.open("rb") as fh:
        magic = fh.read(4)
    assert magic[:4] in (b"\xcf\xfa\xed\xfe", b"\xce\xfa\xed\xfe", b"\xca\xfe\xba\xbe"), magic.hex()


@requires_compiler
def test_launcher_forwards_protocol(launcher):
    binary, _ = launcher
    response = call_host(binary, {"action": "ping"})
    assert response["ok"] is True
    assert response["action"] == "ping"


@requires_compiler
def test_launcher_rejects_unknown_action(launcher):
    binary, _ = launcher
    response = call_host(binary, {"action": "rm -rf /"})
    assert response["ok"] is False
    assert "不支持的动作" in response["error"]


@requires_compiler
def test_launcher_writes_log(launcher):
    binary, log = launcher
    call_host(binary, {"action": "ping"})
    assert log.exists(), "启动器必须留下日志，否则排查时什么都看不到"
    assert "启动 host" in log.read_text(encoding="utf-8")


@requires_compiler
def test_launcher_without_conf_exits_cleanly(tmp_path):
    """没有 host.conf 时必须明确报错退出，而不是挂住。"""
    if COMPILER is None:
        pytest.skip("没有可用的 C 编译器")
    binary = tmp_path / "lonely-host"
    subprocess.run([COMPILER, "-O2", "-o", str(binary), str(LAUNCHER_SRC)], check=True, capture_output=True)
    result = subprocess.run([str(binary), "chrome-extension://x/"], input=b"", capture_output=True, timeout=15)
    assert result.returncode != 0
    assert b"host.conf" in result.stderr


# ------------------------------------------------------------------ 注册脚本


def test_installer_prefers_compiled_launcher(installer):
    """注册脚本必须优先编译二进制；manifest 的 path 要指向它。"""
    assert installer.LAUNCHER_SRC.exists(), "缺少 C 启动器源码"
    manifest = installer.build_manifest(["abc"], Path("/tmp/linkwork-host"))
    assert manifest["path"] == "/tmp/linkwork-host"
    assert manifest["allowed_origins"] == ["chrome-extension://abc/"]
    assert manifest["type"] == "stdio"
    assert manifest["name"] == installer.HOST_NAME


def test_installer_conf_contains_paths_and_log_fallbacks(installer, tmp_path, monkeypatch):
    conf = tmp_path / "host.conf"
    monkeypatch.setattr(installer, "CONF", conf)
    installer.write_launcher_conf()
    text = conf.read_text(encoding="utf-8")
    assert f"python={installer.VENV_PYTHON}" in text
    assert f"host={installer.HOST_SCRIPT}" in text
    # 日志必须有多个落点：被浏览器以受限身份启动时 ~/Library/Logs 可能写不进去
    assert text.count("log=") >= 2


def test_installer_falls_back_to_wrapper_without_compiler(installer, monkeypatch):
    monkeypatch.setattr(installer.shutil, "which", lambda name: None)
    assert installer.build_launcher() is None


def test_detects_extension_loaded_in_any_profile(installer, tmp_path, monkeypatch):
    """扩展可能装在 Profile 1 / Profile 2，而不是 Default；必须全部扫到。"""
    support = tmp_path / "Edge"
    for profile, ext_id in (("Default", "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"),
                            ("Profile 1", "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb")):
        (support / profile).mkdir(parents=True)
        (support / profile / "Preferences").write_text(json.dumps({
            "extensions": {"settings": {
                ext_id: {"manifest": {"name": "LinkWork 文案提取"}, "path": "/somewhere/extension"},
                "cccccccccccccccccccccccccccccccc": {"manifest": {"name": "别的扩展"}, "path": "/other"},
            }},
        }), encoding="utf-8")

    found = installer.loaded_extension_ids(support)
    assert sorted(found) == ["aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa", "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"]


def test_tolerates_broken_or_missing_preferences(installer, tmp_path):
    support = tmp_path / "Edge"
    support.mkdir()
    assert installer.loaded_extension_ids(support) == []      # 什么都没有
    (support / "Default").mkdir()
    (support / "Default" / "Preferences").write_text("{ 不是 JSON", encoding="utf-8")
    assert installer.loaded_extension_ids(support) == []      # 坏文件不能让脚本崩


def test_scan_covers_all_browsers(installer):
    result = installer.scan_browser_extension_ids()
    assert set(result) == set(installer.BROWSERS)
    assert all(isinstance(v, list) for v in result.values())


# ------------------------------------------------------------------ 一键安装脚本


def test_one_click_script_is_runnable_and_safe():
    script = ROOT / "scripts" / "install_linkwork.sh"
    assert script.exists(), "缺少一键安装脚本"
    assert script.stat().st_mode & 0o111, "一键脚本缺少可执行位"
    text = script.read_text(encoding="utf-8")

    # 语法必须能被本机 shell 解析
    syntax = subprocess.run(["bash", "-n", str(script)], capture_output=True, text=True)
    assert syntax.returncode == 0, syntax.stderr

    assert text.startswith("#!/usr/bin/env bash")
    # 出错要立刻停，避免「注册失败但脚本继续跑」产生误导性输出
    assert "set -euo pipefail" in text
    assert "--skip-deps" in text                   # 已装过依赖时能跳过
    assert "install_native_host.py" in text        # 会去注册本机启动器
    assert "Load unpacked" in text                 # 明确告诉用户手动加载扩展的步骤
    assert "LINKWORK_REPO_URL" in text             # 支持 curl | bash 的下载路径


def test_one_click_script_quotes_vars_before_cjk():
    """`$VAR（` 会被 shell 当成变量名的一部分（曾导致 unbound variable）。

    所有紧挨着中文标点的变量都必须写成 ${VAR} 形式。
    """
    import re

    text = (ROOT / "scripts" / "install_linkwork.sh").read_text(encoding="utf-8")
    bad = re.findall(r"\$[A-Za-z_][A-Za-z0-9_]*[（）：，、！？；]", text)
    # 排除已经用花括号包起来的写法（那些不会出现在本正则里）
    assert bad == [], f"这些变量后面紧跟中文标点，会被解析错：{bad}"
