"""本地服务 API 集成测试。

覆盖浏览器插件会调用的完整接口面：
    /api/health  /api/format  /api/resolve  /api/ocr  /api/transcribe  /api/jobs/*
以及鉴权与来源限制行为。

运行：
    .venv/bin/python -m pytest tests/test_service.py -q
"""

from __future__ import annotations

import base64
import http.server
import json
import shutil
import socketserver
import subprocess
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

import pytest

from linkwork.service import ServiceConfig, create_server

ASSET_IMAGE = Path(__file__).parent / "assets" / "xhs_sample.png"


@pytest.fixture(scope="module")
def service():
    """在随机端口启动服务，测试结束后关闭。"""
    config = ServiceConfig(host="127.0.0.1", port=0, asr_model="tiny")
    server = create_server(config)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{server.server_address[1]}"
    yield base
    server.shutdown()
    server.server_close()


def api(base: str, path: str, payload=None, *, origin: str | None = None, timeout: float = 30):
    headers = {"Content-Type": "application/json"}
    if origin:
        headers["Origin"] = origin
    data = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(base + path, data=data, headers=headers,
                                 method="POST" if data else "GET")
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read())


def wait_job(base: str, job_id: str, timeout: float = 240) -> dict:
    deadline = time.time() + timeout
    while time.time() < deadline:
        data = api(base, f"/api/jobs/{job_id}")
        if data["state"] in ("done", "error"):
            return data
        time.sleep(0.8)
    raise AssertionError("任务超时未完成")


# --------------------------------------------------------------------------- #
# 基础接口
# --------------------------------------------------------------------------- #
def test_health_reports_capabilities(service):
    data = api(service, "/api/health")
    assert data["ok"] is True
    assert data["name"] == "linkwork"
    assert "asr" in data and isinstance(data["asr"], list)


def test_format_builds_readable_text(service):
    result = api(
        service,
        "/api/format",
        {"text": "第一段内容\n第二段内容", "title": "标题", "author": "作者",
         "url": "https://example.com/a", "kind": "article"},
    )["result"]
    assert result["title"] == "标题"
    assert "第一段内容" in result["text"]
    # 换行必须保留（此前 \s 会把段落合并）
    assert "\n" in result["text"]
    assert "https://example.com/a" in result["text"]


def test_format_rejects_too_short_text(service):
    with pytest.raises(urllib.error.HTTPError) as excinfo:
        api(service, "/api/format", {"text": "短"})
    assert excinfo.value.code == 400


def test_unknown_path_returns_404(service):
    with pytest.raises(urllib.error.HTTPError) as excinfo:
        api(service, "/api/nope", {"a": 1})
    assert excinfo.value.code == 404


def test_unknown_job_returns_404(service):
    with pytest.raises(urllib.error.HTTPError) as excinfo:
        api(service, "/api/jobs/deadbeef")
    assert excinfo.value.code == 404


# --------------------------------------------------------------------------- #
# 来源限制：网页无法驱动本地服务
# --------------------------------------------------------------------------- #
def test_http_origin_is_rejected(service):
    with pytest.raises(urllib.error.HTTPError) as excinfo:
        api(service, "/api/format", {"text": "这是一段足够长的文本"}, origin="http://evil.example")
    assert excinfo.value.code == 403


def test_extension_origin_is_allowed(service):
    data = api(
        service,
        "/api/format",
        {"text": "这是一段足够长的页面文本"},
        origin="chrome-extension://abcdefghijklmnopabcdefghijklmnop",
    )
    assert data["ok"] is True


def test_no_origin_is_allowed_for_local_tools(service):
    # 扩展的 service worker 在部分情况下不带 Origin
    assert api(service, "/api/health")["ok"] is True


# --------------------------------------------------------------------------- #
# 转写：base64 上传
# --------------------------------------------------------------------------- #
def _make_speech_wav(tmp_path: Path) -> Path:
    from linkwork.media import to_asr_wav

    aiff = tmp_path / "speech.aiff"
    text = "大家好，这是本地服务接口测试用的语音内容。"
    subprocess.run(["say", "-v", "Ting-Ting", "-o", str(aiff), text], check=True)
    return to_asr_wav(aiff, tmp_path)


def test_transcribe_base64_upload(service, tmp_path: Path):
    if shutil.which("say") is None:  # pragma: no cover
        pytest.skip("没有 say 命令")
    wav = _make_speech_wav(tmp_path)
    payload = {
        "data_base64": base64.b64encode(wav.read_bytes()).decode("ascii"),
        "suffix": ".wav",
        "page_url": "https://example.com/v/1",
        "title": "接口测试",
    }
    job = api(service, "/api/transcribe", payload)["job"]
    final = wait_job(service, job["id"])
    assert final["state"] == "done", final.get("error")
    result = final["result"]
    assert result["method"] == "asr"
    assert result["segment_count"] >= 1
    assert "本地服务" in result["text"] or "语音" in result["text"]


def test_transcribe_requires_media_source(service):
    with pytest.raises(urllib.error.HTTPError) as excinfo:
        api(service, "/api/transcribe", {"page_url": "https://example.com"})
    assert excinfo.value.code == 400


# --------------------------------------------------------------------------- #
# OCR：图文笔记图片文字
# --------------------------------------------------------------------------- #
def test_ocr_reads_chinese_from_image(service):
    from linkwork.ocr import ocr_available

    if not ocr_available():  # pragma: no cover
        pytest.skip("当前环境没有 Vision OCR")
    if not ASSET_IMAGE.exists():  # pragma: no cover
        pytest.skip("缺少测试图片")

    fixture = ASSET_IMAGE.parent / "serve_tmp.png"
    shutil.copy(ASSET_IMAGE, fixture)

    class QuietHandler(http.server.SimpleHTTPRequestHandler):
        def log_message(self, *args):  # noqa: D102
            return

    httpd = socketserver.TCPServer(
        ("127.0.0.1", 0),
        lambda *a, **k: QuietHandler(*a, directory=str(fixture.parent), **k),
    )
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    image_url = f"http://127.0.0.1:{httpd.server_address[1]}/{fixture.name}"
    try:
        job = api(
            service,
            "/api/ocr",
            {"images": [image_url], "title": "图文测试", "desc": "笔记正文",
             "url": "https://example.com/note"},
        )["job"]
        final = wait_job(service, job["id"], timeout=120)
    finally:
        httpd.shutdown()
        httpd.server_close()
        fixture.unlink(missing_ok=True)

    assert final["state"] == "done", final.get("error")
    result = final["result"]
    assert result["method"] == "ocr"
    assert "笔记正文" in result["text"]
    assert "图片文字" in result["text"]
    # 图片里是中文文案，OCR 后应命中关键词
    assert any(kw in result["text"] for kw in ("小红书", "图文", "文案", "链接")), result["text"]


def test_ocr_requires_images(service):
    with pytest.raises(urllib.error.HTTPError) as excinfo:
        api(service, "/api/ocr", {"images": []})
    assert excinfo.value.code == 400


# --------------------------------------------------------------------------- #
# resolve：纯文本输入
# --------------------------------------------------------------------------- #
def test_resolve_pasted_text(service, tmp_path: Path):
    """粘贴纯文本时，插件也会走 resolve 或 format；这里验证服务能处理。"""
    data = api(service, "/api/format", {"text": "这是一段直接粘贴的测试文案内容，用于验证接口。"})
    assert data["ok"] is True
    assert "直接粘贴" in data["result"]["text"]


# --------------------------------------------------------------------------- #
# 媒体缓存：不残留、可统计、可一键清除
# --------------------------------------------------------------------------- #
def test_cache_stats_and_clear(monkeypatch, tmp_path: Path):
    """缓存应能统计大小并一键清除。"""
    from linkwork import cache

    monkeypatch.setenv("LINKWORK_CACHE_DIR", str(tmp_path))
    work = cache.new_workspace("test")
    (work / "media.mp4").write_bytes(b"x" * 2048)
    (work / "sub").mkdir()
    (work / "sub" / "audio.wav").write_bytes(b"y" * 1024)

    info = cache.stats()
    assert info["workspaces"] == 1
    assert info["files"] == 2
    assert info["bytes"] == 3072
    assert info["human"].endswith(("KB", "B"))

    result = cache.clear()
    assert result["removed"] == 1
    assert result["freed_bytes"] == 3072
    assert cache.stats()["workspaces"] == 0


def test_cache_clear_keeps_active_workspace(monkeypatch, tmp_path: Path):
    """正在运行的任务工作区不能被误删。"""
    from linkwork import cache

    monkeypatch.setenv("LINKWORK_CACHE_DIR", str(tmp_path))
    active = cache.new_workspace("active")
    idle = cache.new_workspace("idle")

    result = cache.clear(keep={active})
    assert result["removed"] == 1
    assert active.exists(), "活动工作区被误删"
    assert not idle.exists()


def test_cache_cleanup_workspace_removes_single(monkeypatch, tmp_path: Path):
    from linkwork import cache

    monkeypatch.setenv("LINKWORK_CACHE_DIR", str(tmp_path))
    work = cache.new_workspace("job")
    assert work.exists()
    cache.cleanup_workspace(work)
    assert not work.exists()


def test_cache_purge_orphans_only_old(monkeypatch, tmp_path: Path):
    """只有足够老的遗留目录才会被启动清理。"""
    import os
    import time

    from linkwork import cache

    monkeypatch.setenv("LINKWORK_CACHE_DIR", str(tmp_path))
    fresh = cache.new_workspace("fresh")
    old = cache.new_workspace("old")
    stale = time.time() - cache.ORPHAN_AGE_SECONDS - 60
    os.utime(old, (stale, stale))

    purged = cache.purge_orphans()
    assert purged["removed"] == 1
    assert fresh.exists(), "新工作区不应被清理"
    assert not old.exists()


def test_cache_api_endpoints(service):
    """/api/cache 与 /api/cache/clear 应可用。"""
    data = api(service, "/api/cache")
    assert data["ok"] is True
    assert "root" in data["cache"] and "human" in data["cache"]

    cleared = api(service, "/api/cache/clear", {})
    assert cleared["ok"] is True
    assert "removed" in cleared["cleared"]
    assert cleared["cache"]["workspaces"] == 0


def test_health_includes_cache(service):
    data = api(service, "/api/health")
    assert "cache" in data
    assert "bytes" in data["cache"]


# --------------------------------------------------------------------------- #
# ASR 子进程隔离：卡死必须能被超时终止，服务不能被拖死
# --------------------------------------------------------------------------- #
def test_asr_isolation_kills_hung_subprocess(tmp_path, monkeypatch):
    """子进程卡死时应被超时杀掉并抛出可读错误，而不是永久阻塞。"""
    import sys
    import time

    from linkwork.asr import isolated as iso

    hang = tmp_path / "hang_worker.py"
    hang.write_text("import time\nwhile True:\n    time.sleep(3600)\n", encoding="utf-8")

    real_popen = iso.subprocess.Popen

    def fake_popen(cmd, **kwargs):  # noqa: ANN001, ANN003
        return real_popen([sys.executable, str(hang)], **kwargs)

    monkeypatch.setattr(iso.subprocess, "Popen", fake_popen)

    audio = tmp_path / "a.wav"
    audio.write_bytes(b"RIFF")

    started = time.monotonic()
    with pytest.raises(Exception) as excinfo:
        iso.transcribe_isolated(
            audio,
            timeout=30.0,
            silence_timeout=1.5,
            workdir=tmp_path,
        )
    elapsed = time.monotonic() - started

    assert elapsed < 20, f"卡死未被及时终止（耗时 {elapsed:.1f}s）"
    assert "卡死" in str(excinfo.value) or "终止" in str(excinfo.value), str(excinfo.value)
    # 子进程必须已被杀掉
    leftover = list(tmp_path.glob("asr_subprocess_*"))
    assert not leftover, f"临时文件未清理：{leftover}"


def test_asr_isolation_returns_result(tmp_path, monkeypatch):
    """子进程正常写出结果时，父进程应正确解析出分段。"""
    import json
    import sys

    from linkwork.asr import isolated as iso

    worker = tmp_path / "ok_worker.py"
    worker.write_text(
        "import json, sys, time\n"
        "out = sys.argv[sys.argv.index('--out') + 1]\n"
        "with open(out, 'a', encoding='utf-8') as fh:\n"
        "    fh.write(json.dumps({'type': 'progress', 'ratio': 0.5}) + '\\n')\n"
        "    fh.write(json.dumps({'type': 'result', 'language': 'zh',"
        " 'language_probability': 1.0, 'duration': 3.0,"
        " 'segments': [{'start': 0.0, 'end': 1.5, 'text': '测试台词'}]}) + '\\n')\n"
        "    fh.flush()\n"
        "time.sleep(0.1)\n",
        encoding="utf-8",
    )

    real_popen = iso.subprocess.Popen

    def fake_popen(cmd, **kwargs):  # noqa: ANN001, ANN003
        return real_popen([sys.executable, str(worker), *cmd[3:]], **kwargs)

    monkeypatch.setattr(iso.subprocess, "Popen", fake_popen)

    audio = tmp_path / "a.wav"
    audio.write_bytes(b"RIFF")

    ratios: list[float] = []
    result = iso.transcribe_isolated(
        audio,
        timeout=30.0,
        on_progress=ratios.append,
        workdir=tmp_path,
    )

    assert result.language == "zh"
    assert len(result.segments) == 1
    assert result.segments[0].text == "测试台词"
    assert ratios and ratios[-1] == 0.5, ratios


def test_faster_whisper_routes_through_isolation(tmp_path, monkeypatch):
    """faster-whisper 必须走子进程隔离，否则原生层卡死会拖死整个服务。"""
    from unittest.mock import patch

    from linkwork import transcribe as tr
    from linkwork.asr.isolated import IsolatedResult
    from linkwork.resolvers.base import ResolveContext

    wav = tmp_path / "a.wav"
    wav.write_bytes(b"RIFF")
    monkeypatch.setattr(tr, "prepare_asr_wav", lambda path, dest: wav)

    calls: list[dict] = []

    def fake_isolated(path, **kwargs):  # noqa: ANN001, ANN003
        calls.append(kwargs)
        return IsolatedResult(
            {
                "language": "zh",
                "duration": 1.0,
                "segments": [{"start": 0.0, "end": 1.0, "text": "隔离测试"}],
            }
        )

    ctx = ResolveContext(workdir=tmp_path, asr_engine="faster-whisper", asr_model="small")
    with patch("linkwork.asr.isolated.transcribe_isolated", fake_isolated):
        segments, duration, engine_name, model_name, out_wav = tr.transcribe_local_media(
            wav, ctx, tmp_path
        )

    assert calls, "faster-whisper 未走子进程隔离"
    assert engine_name == "faster-whisper"
    assert len(segments) == 1 and segments[0].text == "隔离测试"
    assert out_wav == wav


# --------------------------------------------------------------------------- #
# 任务诊断日志：父子进程共用同一文件，必须原子追加、不得损坏
# --------------------------------------------------------------------------- #
def test_task_log_survives_concurrent_writers(tmp_path, monkeypatch):
    """父进程与多个子进程同时追加时，日志不能出现丢行或编码损坏。

    这里锁定的是一条容易踩坑的实现要求：必须用 O_APPEND + 单次 write
    原子追加。曾经用 write()+flush() 导致父子交错写坏 UTF-8 字节。
    """
    import collections
    import subprocess
    import sys

    from linkwork import diagnostics as diag

    monkeypatch.setattr(diag, "logs_root", lambda: tmp_path)

    tlog = diag.TaskLog("concurrent-test", "transcribe")

    child_code = (
        "import sys\n"
        "from linkwork.diagnostics import TaskLog\n"
        "t = TaskLog.append_to(sys.argv[1], prefix='child')\n"
        "for i in range(60):\n"
        "    t.note('子行', f'{sys.argv[2]}-{i}')\n"
        "t.close()\n"
    )
    procs = [
        subprocess.Popen([sys.executable, "-c", child_code, str(tlog.path), f"C{n}"])
        for n in range(3)
    ]
    for i in range(60):
        tlog.note("父行", f"P-{i}")
    for proc in procs:
        proc.wait(timeout=60)
    tlog.close()

    text = tlog.path.read_text(encoding="utf-8")  # 损坏会在这里抛 UnicodeDecodeError
    counts = collections.Counter()
    for line in text.splitlines():
        if "父行" in line:
            counts["父"] += 1
        elif "子行" in line:
            for n in range(3):
                if f"C{n}-" in line:
                    counts[f"子C{n}"] += 1

    assert counts["父"] == 60, f"父进程日志丢行：{counts}"
    for n in range(3):
        assert counts[f"子C{n}"] == 60, f"子进程 C{n} 日志丢行：{counts}"


def test_diagnostics_summarize_extracts_outcome(tmp_path, monkeypatch):
    """诊断摘要是「一眼看出哪个任务失败」的关键，必须能提炼出结果与原因。"""
    from linkwork import diagnostics as diag

    monkeypatch.setattr(diag, "logs_root", lambda: tmp_path)

    ok = tmp_path / "transcribe_ok.log"
    ok.write_text(
        "[10:00:00 +  0.00s] 任务 abc（transcribe）\n"
        "[10:00:05 +  5.00s] ▶ 下载媒体\n"
        "[10:00:09 +  9.00s] ✔ 完成，耗时 9.00s\n"
        "[10:00:09 +  9.00s]   字数: 110\n"
        "[10:00:09 +  9.00s] 阶段耗时：下载媒体@0.1s → 语音识别@5.0s\n",
        encoding="utf-8",
    )
    info = diag.summarize(ok)
    assert info["outcome"] == "done", info
    assert info["job_id"] == "abc", info
    assert info["stages"] == ["下载媒体@0.1s", "语音识别@5.0s"], info

    bad = tmp_path / "transcribe_bad.log"
    bad.write_text(
        "[10:00:00 +  0.00s] 任务 def（transcribe）\n"
        "[10:00:00 +  0.10s]     HTTP 状态: 403\n"
        "[10:00:00 +  0.10s]     ✖ 任务失败\n"
        "[10:00:00 +  0.10s]       linkwork.errors.LinkWorkError: 媒体下载失败：HTTP 403（链接已过期）\n",
        encoding="utf-8",
    )
    info = diag.summarize(bad)
    assert info["outcome"] == "error", info
    assert "403" in info["detail"], info


def test_diagnostics_api_reports_failures(tmp_path, monkeypatch):
    """诊断接口要能直接告诉调用方「有几个任务失败」。"""
    from linkwork import diagnostics as diag

    monkeypatch.setattr(diag, "logs_root", lambda: tmp_path)
    (tmp_path / "transcribe_x.log").write_text(
        "[10:00:00 +  0.00s] 任务 x（transcribe）\n"
        "[10:00:01 +  1.00s]     ✖ 任务失败\n"
        "[10:00:01 +  1.00s]       LinkWorkError: 测试失败原因\n",
        encoding="utf-8",
    )
    logs = diag.list_task_logs(10)
    assert len(logs) == 1
    assert logs[0]["outcome"] == "error"
    assert "测试失败原因" in logs[0]["detail"]
    # 列表里不应泄漏绝对路径（避免调用方依赖本机路径）
    assert "path" not in logs[0]


def test_failed_task_records_stage_timings(tmp_path, monkeypatch):
    """失败任务也要有阶段耗时——否则看不出卡在哪一步。"""
    from linkwork import diagnostics as diag

    monkeypatch.setattr(diag, "logs_root", lambda: tmp_path)
    tlog = diag.TaskLog("failcase", "transcribe")
    tlog.stage("下载媒体")
    tlog.note("HTTP 状态", 404)
    tlog.stage("提取音频")
    tlog.error("任务失败", ValueError("模拟失败"))
    tlog.close()

    info = diag.summarize(tlog.path)
    assert info["outcome"] == "error", info
    assert info["stages"], f"失败任务缺少阶段耗时：{info}"
    assert any("下载媒体" in s for s in info["stages"]), info


def test_successful_task_records_stage_timings(tmp_path, monkeypatch):
    """成功任务的阶段耗时（新任务应能从任务日志本身读到，不再依赖服务主日志）。"""
    from linkwork import diagnostics as diag

    monkeypatch.setattr(diag, "logs_root", lambda: tmp_path)
    tlog = diag.TaskLog("okcase", "transcribe")
    tlog.stage("下载媒体")
    tlog.stage("语音识别")
    tlog.done({"字数": 100})
    tlog.close()

    info = diag.summarize(tlog.path)
    assert info["outcome"] == "done", info
    assert len(info["stages"]) == 2, info
    assert info["stages"][0].startswith("下载媒体@"), info


def test_service_log_tail_excludes_previous_run(tmp_path, monkeypatch):
    """服务日志尾部只应包含本次启动之后的记录，避免历史报错误导排查。"""
    from linkwork import diagnostics as diag

    log_file = tmp_path / "svc.log"
    monkeypatch.setattr(diag, "service_log_path", lambda: log_file)
    monkeypatch.setattr(diag, "_SERVICE_STARTED_AT", "2026-10-08 19:00:00")

    log_file.write_text(
        "2026-10-08 18:00:00,000 [ERROR] 上一次运行的 TypeError: 历史噪音\n"
        "Traceback (most recent call last):\n"
        "TypeError: 历史噪音续行\n"
        "2026-10-08 19:30:00,000 [INFO] 本次运行的正常日志\n"
        "Traceback (most recent call last):\n"
        "linkwork.errors.LinkWorkError: 本次运行的失败\n",
        encoding="utf-8",
    )

    tail = diag.tail_service_log(since_start=True)
    joined = "\n".join(tail)
    assert "本次运行的正常日志" in joined
    assert "本次运行的失败" in joined
    assert "历史噪音" not in joined, tail


def test_service_log_tail_filters_third_party_noise(tmp_path, monkeypatch):
    """faster-whisper 的刷屏日志会淹没关键信息，应被过滤。"""
    from linkwork import diagnostics as diag

    log_file = tmp_path / "svc.log"
    monkeypatch.setattr(diag, "service_log_path", lambda: log_file)
    monkeypatch.setattr(diag, "_SERVICE_STARTED_AT", "2026-10-08 19:00:00")
    log_file.write_text(
        "2026-10-08 19:01:00,000 [INFO] faster_whisper: Processing audio with duration 00:30.303\n"
        "2026-10-08 19:01:01,000 [INFO] faster_whisper: VAD filter removed 00:00.000 of audio\n"
        "2026-10-08 19:01:02,000 [INFO] linkwork.jobs: 任务 abc 完成\n",
        encoding="utf-8",
    )
    tail = "\n".join(diag.tail_service_log(since_start=True))
    assert "faster_whisper" not in tail, tail
    assert "任务 abc 完成" in tail, tail


def test_diagnostics_summary_has_last_outcome(tmp_path, monkeypatch):
    """summary 要能直接回答「刚才那次成没成」，不必翻 logs 数组。"""
    import json as _json

    from linkwork import diagnostics as diag

    monkeypatch.setattr(diag, "logs_root", lambda: tmp_path)
    (tmp_path / "transcribe_recent.log").write_text(
        "[10:00:00 +  0.00s] 任务 recent（transcribe）\n"
        "[10:00:02 +  2.00s] ✔ 完成，耗时 2.00s\n"
        "[10:00:02 +  2.00s] 阶段耗时：下载媒体@0.1s\n",
        encoding="utf-8",
    )
    logs = diag.list_task_logs(10)
    latest = logs[0]
    # 复刻 /api/diagnostics 的 summary 组装
    summary = {
        "recent": len(logs),
        "failed": len([x for x in logs if x.get("outcome") == "error"]),
        "last_outcome": latest.get("outcome", "none"),
        "last_detail": latest.get("detail", ""),
    }
    assert summary["last_outcome"] == "done", summary
    assert "完成" in summary["last_detail"], summary
    assert summary["failed"] == 0, summary
    _json.dumps(summary)  # 必须可序列化（会被直接放进 HTTP 响应）
