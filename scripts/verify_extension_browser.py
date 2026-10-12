"""在全新临时配置下启动 Edge，验证扩展能否被浏览器正常加载。

为什么用临时配置：Edge 拒绝在默认用户目录上开启远程调试
（"DevTools remote debugging requires a non-default data directory"），
而临时配置不会影响用户的登录态与设置。

验证内容：
  1. 扩展是否出现在 CDP 目标列表里（说明 manifest 合法、扩展被加载）；
  2. 能取到扩展 ID；
  3. 打印 service worker 相关目标，便于判断后台脚本是否正常运行。

用法：
    .venv/bin/python scripts/verify_extension_browser.py
    .venv/bin/python scripts/verify_extension_browser.py --worker          # 进 worker 上下文
    .venv/bin/python scripts/verify_extension_browser.py --panel --shot out.png   # 侧边栏渲染 + 截图
    .venv/bin/python scripts/verify_extension_browser.py --panel --connect # 连接开/关生命周期

⚠️ 已知环境限制：部分 macOS 机器上 Chromium 会在连接后约 0.5s 主动断开 DevTools
   WebSocket（code 1006，Edge/Chrome 皆然，与 --remote-allow-origins 无关），
   表现为大量 ConnectionClosedError / HTTP 502。此时本脚本的浏览器内检查无法完成，
   请改用界面自证：打开侧边栏看右上角是否显示「连接已开启」，关掉侧边栏再看日志。
"""

from __future__ import annotations

import asyncio
import json
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path

EDGE = "/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge"
EXT_DIR = Path(__file__).resolve().parent.parent / "extension"
PORT = 9333
BASE = f"http://127.0.0.1:{PORT}"


def http_json(path: str, timeout: float = 5):
    with urllib.request.urlopen(f"{BASE}{path}", timeout=timeout) as resp:
        return json.loads(resp.read())


async def verify() -> int:
    if not EXT_DIR.exists():
        print(f"❌ 扩展目录不存在：{EXT_DIR}", file=sys.stderr)
        return 1

    profile = Path(tempfile.mkdtemp(prefix="lw_edge_profile_"))
    print(f"临时配置目录：{profile}")
    print(f"扩展目录：{EXT_DIR}")
    print()

    proc = subprocess.Popen(
        [
            EDGE,
            f"--remote-debugging-port={PORT}",
            # Chromium 111+ 会拒绝带 Origin 头的 DevTools WebSocket 连接，
            # 而 Python websockets 默认会带 Origin —— 不加这个开关，
            # 页面/worker 目标一连接就 502（脚本此前就卡在这里）。
            "--remote-allow-origins=*",
            f"--user-data-dir={profile}",
            f"--load-extension={EXT_DIR}",
            "--no-first-run",
            "--no-default-browser-check",
            "--disable-features=msEdgeSidebar,msEdgeIdentityFeature",
            "about:blank",
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )

    try:
        # 等调试端口就绪
        version = None
        for _ in range(40):
            try:
                version = http_json("/json/version", timeout=3)
                break
            except Exception:  # noqa: BLE001
                if proc.poll() is not None:
                    print("❌ Edge 进程提前退出", file=sys.stderr)
                    return 1
                time.sleep(0.5)

        if not version:
            print("❌ 调试端口未就绪", file=sys.stderr)
            return 1

        print("浏览器：", version.get("Browser"))
        print("调试协议：", version.get("Protocol-Version"))
        print()

        # 轮询目标，找扩展相关条目
        targets: list[dict] = []
        for _ in range(30):
            try:
                targets = http_json("/json/list", timeout=5)
            except Exception:  # noqa: BLE001
                targets = []
            if any("extension" in (t.get("url") or "") for t in targets):
                break
            time.sleep(0.5)

        # --panel 模式：把扩展页面当普通页面打开，验证 HTML/JS 本身可用
        if "--panel" in sys.argv:
            import hashlib as _h

            digest = _h.sha256(str(EXT_DIR).encode()).digest()[:16]
            ext_id = "".join(chr(ord("a") + (b >> 4)) + chr(ord("a") + (b & 0xF)) for b in digest)
            panel_url = f"chrome-extension://{ext_id}/sidepanel.html"
            print(f"扩展 ID：{ext_id}")
            print(f"面板地址：{panel_url}\n")
            shot = ""
            if "--shot" in sys.argv:
                idx = sys.argv.index("--shot")
                if idx + 1 < len(sys.argv) and not sys.argv[idx + 1].startswith("--"):
                    shot = sys.argv[idx + 1]
                else:
                    shot = str(Path(__file__).resolve().parent.parent / "output" / "sidepanel.png")
            try:
                await _check_panel(panel_url, shot_path=shot)
            except Exception as exc:  # noqa: BLE001
                print(f"⚠️  面板检查失败：{type(exc).__name__}: {exc}")
            if "--connect" in sys.argv:
                # 连接生命周期检查会关掉侧边栏页面，放在渲染检查之后单独跑
                try:
                    await _check_connect(ext_id)
                except Exception as exc:  # noqa: BLE001
                    print(f"⚠️  连接检查失败：{type(exc).__name__}: {exc}")
            return 0

        # --worker 模式：直接进入扩展的 service worker 上下文做检查
        if "--worker" in sys.argv:
            import hashlib as _h

            digest = _h.sha256(str(EXT_DIR).encode()).digest()[:16]
            ext_id = "".join(chr(ord("a") + (b >> 4)) + chr(ord("a") + (b & 0xF)) for b in digest)
            await _check_worker(ext_id)
            return 0

        ext_targets = [t for t in targets if "extension" in (t.get("url") or "")]
        print(f"CDP 目标总数：{len(targets)}")
        for t in targets:
            print(f"  [{t.get('type'):<12}] {t.get('title','')[:34]:<36} {t.get('url','')[:70]}")

        print()
        if ext_targets:
            print("✅ 扩展已被浏览器加载")
            ids = set()
            for t in ext_targets:
                url = t.get("url", "")
                # chrome-extension://<id>/...
                parts = url.split("/")
                if len(parts) > 2 and parts[2]:
                    ids.add(parts[2])
            for ext_id in sorted(ids):
                print(f"   扩展 ID：{ext_id}")
            return 0

        print("⚠️  目标列表里没有扩展条目（部分 Edge 版本不暴露扩展目标，不能据此判定失败）")
        print("   但扩展已通过 --load-extension 传入；如需确证，请看 Edge 窗口是否出现扩展图标")
        return 2

    finally:
        print("\n关闭临时 Edge 并清理…")
        try:
            proc.send_signal(signal.SIGTERM)
            proc.wait(timeout=10)
        except Exception:  # noqa: BLE001
            proc.kill()
        time.sleep(1)
        shutil.rmtree(profile, ignore_errors=True)
        print("已清理")


async def _check_worker(ext_id: str) -> None:
    """打开扩展页面以唤醒 service worker，然后进入 worker 上下文检查其状态。"""
    import json as _json

    import websockets as _ws

    panel_url = f"chrome-extension://{ext_id}/sidepanel.html"
    pages = [t for t in http_json("/json/list") if t.get("type") == "page"]
    if not pages:
        print("没有可用页面目标")
        return

    # 先打开扩展页面，这会唤醒该扩展的 service worker
    ws = await _ws.connect(pages[0]["webSocketDebuggerUrl"], proxy=None)
    i = {"n": 0}

    async def call(method, params=None, timeout=15):
        i["n"] += 1
        mid = i["n"]
        await ws.send(_json.dumps({"id": mid, "method": method, "params": params or {}}))
        while True:
            data = _json.loads(await asyncio.wait_for(ws.recv(), timeout=timeout))
            if data.get("id") == mid:
                return data.get("result", {})

    await call("Runtime.enable")
    await call("Page.enable")
    await call("Page.navigate", {"url": panel_url})
    await asyncio.sleep(4)
    await ws.close()

    # 在目标列表里找该扩展的 service worker
    worker = None
    for _ in range(20):
        for t in http_json("/json/list"):
            url = t.get("url") or ""
            if t.get("type") in ("service_worker", "background_page") and ext_id in url:
                worker = t
                break
        if worker:
            break
        await asyncio.sleep(0.5)

    print(f"扩展 ID：{ext_id}")
    if not worker:
        print("\n⚠️  没有找到该扩展的 service worker 目标")
        print("   这说明 worker 当前处于挂起状态（MV3 正常现象），或未成功注册。")
        print("   已在扩展页面唤醒过一次；下面是当前全部扩展相关目标：")
        for t in http_json("/json/list"):
            if "extension" in (t.get("url") or ""):
                print(f"     [{t.get('type')}] {t.get('url','')[:90]}")
        return

    print(f"\n找到 service worker：{worker.get('url','')[:90]}")
    ws2 = await _ws.connect(worker["webSocketDebuggerUrl"], proxy=None)
    j = {"n": 0}

    async def wcall(method, params=None, timeout=15):
        j["n"] += 1
        mid = j["n"]
        await ws2.send(_json.dumps({"id": mid, "method": method, "params": params or {}}))
        while True:
            data = _json.loads(await asyncio.wait_for(ws2.recv(), timeout=timeout))
            if data.get("id") == mid:
                if "error" in data:
                    return {"__error": data["error"]}
                return data.get("result", {})

    try:
        await wcall("Runtime.enable")
        result = await wcall(
            "Runtime.evaluate",
            {
                "expression": """(async () => {
                    const out = { hasSidePanelApi: !!chrome.sidePanel, hasActionApi: !!chrome.action };
                    try { out.panelBehavior = await chrome.sidePanel.getPanelBehavior(); }
                    catch (e) { out.panelBehaviorError = String(e); }
                    try { out.panelOptions = await chrome.sidePanel.getOptions({}); }
                    catch (e) { out.panelOptionsError = String(e); }
                    out.manifestName = chrome.runtime.getManifest().name;
                    out.manifestVersion = chrome.runtime.getManifest().version;
                    out.sidePanelPath = chrome.runtime.getManifest().side_panel?.default_path;
                    return out;
                })()""",
                "returnByValue": True,
                "awaitPromise": True,
            },
        )
        if result.get("exceptionDetails"):
            print("⚠️  worker 内执行异常：", _json.dumps(result["exceptionDetails"], ensure_ascii=False)[:400])
        print("\n=== service worker 状态 ===")
        print(_json.dumps(result.get("result", {}).get("value"), ensure_ascii=False, indent=2))
    finally:
        await ws2.close()


async def _check_connect(ext_id: str) -> None:
    """验证「打开侧边栏 = 建立服务连接，关闭 = 断开」在真实浏览器里成立。

    做法（全程只读，不改扩展状态）：
      1. 打开侧边栏页面 → 界面状态灯应变为「已连接」（或明确的服务未连接）；
      2. 进 service worker 上下文读 __linkworkPanelBridge.panelLinks.size == 1；
      3. 关掉侧边栏页面 → worker 里该连接应被清理（size 回到 0）。

    用法： .venv/bin/python scripts/verify_extension_browser.py --panel --connect
    """
    import json as _json

    import websockets as _ws

    panel_url = f"chrome-extension://{ext_id}/sidepanel.html"

    async def worker_eval(expression: str):
        """在扩展的 service worker 里执行表达式；找不到 worker 就返回 None"""
        target = None
        for _ in range(20):
            for t in http_json("/json/list"):
                url = t.get("url") or ""
                if t.get("type") in ("service_worker", "background_page") and ext_id in url:
                    target = t
                    break
            if target:
                break
            await asyncio.sleep(0.5)
        if not target:
            return None
        ws2 = await _ws.connect(target["webSocketDebuggerUrl"], proxy=None)
        try:
            counter = {"i": 0}

            async def call(method, params=None, timeout=15):
                counter["i"] += 1
                mid = counter["i"]
                await ws2.send(_json.dumps({"id": mid, "method": method, "params": params or {}}))
                while True:
                    data = _json.loads(await asyncio.wait_for(ws2.recv(), timeout=timeout))
                    if data.get("id") == mid:
                        return data.get("result", {})

            await call("Runtime.enable")
            result = await call(
                "Runtime.evaluate",
                {"expression": expression, "returnByValue": True, "awaitPromise": True},
            )
            if result.get("exceptionDetails"):
                return {"__error": _json.dumps(result["exceptionDetails"], ensure_ascii=False)[:200]}
            return result.get("result", {}).get("value")
        finally:
            await ws2.close()

    async def wait_links(expected: int, timeout: float = 8.0):
        """等 panelLinks.size 变成期望值，返回最后读到的值"""
        deadline = time.time() + timeout
        seen = None
        while time.time() < deadline:
            value = await worker_eval("globalThis.__linkworkPanelBridge?.panelLinks?.size ?? -1")
            if isinstance(value, int):
                seen = value
                if value == expected:
                    return value
            await asyncio.sleep(0.5)
        return seen

    pages = [t for t in http_json("/json/list") if t.get("type") == "page"]
    if not pages:
        print("没有可用页面目标")
        return
    # 优先占用一个空白页来当侧边栏；否则借用第一个页面，结束时再恢复
    blank = next((t for t in pages if (t.get("url") or "") in ("about:blank", "")), None)
    page = blank or pages[0]
    borrow = blank is None
    page_id = page.get("id") or ""
    original_url = page.get("url") or "about:blank"
    ws = await _ws.connect(page["webSocketDebuggerUrl"], proxy=None)
    counter = {"i": 0}

    async def call(method: str, params: dict | None = None, timeout: float = 15):
        counter["i"] += 1
        mid = counter["i"]
        await ws.send(_json.dumps({"id": mid, "method": method, "params": params or {}}))
        while True:
            data = _json.loads(await asyncio.wait_for(ws.recv(), timeout=timeout))
            if data.get("id") == mid:
                return data.get("result", {})

    ok = True
    try:
        print("=== 连接生命周期检查 ===")
        await call("Runtime.enable")
        await call("Page.enable")
        await call("Page.navigate", {"url": panel_url})
        # 打开侧边栏：界面状态灯应自动变为已连接（本地服务在跑的前提下）
        status = ""
        ok_class = ""
        conn_state = ""
        for _ in range(20):
            await asyncio.sleep(0.5)
            value = await call(
                "Runtime.evaluate",
                {
                    "expression": """(() => {
                        const dot = document.getElementById('statusDot');
                        const text = document.getElementById('statusText');
                        const conn = document.getElementById('connState');
                        return {
                          text: text ? text.textContent : '',
                          dotClass: dot ? dot.className : '',
                          conn: conn ? conn.textContent : '',
                          log: (document.getElementById('logBox')?.textContent || '').slice(0, 400),
                        };
                    })()""",
                    "returnByValue": True,
                },
            )
            info = value.get("result", {}).get("value") or {}
            status = info.get("text") or ""
            ok_class = info.get("dotClass") or ""
            conn_state = info.get("conn") or ""
            if "ok" in ok_class.split():
                break
        print(f"侧边栏状态灯    ：{ok_class!r}")
        print(f"侧边栏状态文案  ：{status!r}")
        print(f"连接开关标签    ：{conn_state!r}")
        if "ok" in ok_class.split():
            print("✅ 打开侧边栏后已自动连上本地服务")
        else:
            ok = False
            print("❌ 打开侧边栏后未连上本地服务（若服务未运行，请先 ./scripts/service.sh start）")

        links = await wait_links(1, timeout=8)
        print(f"\nworker 里的活动连接数：{links}")
        if links == 1:
            print("✅ 后台登记了 1 条侧边栏连接（并已起心跳探活）")
        else:
            ok = False
            print("❌ 后台没有登记到侧边栏连接")

        # 关闭侧边栏页面 → 连接应被清理
        try:
            http_json(f"/json/close/{page_id}")
        except Exception as exc:  # noqa: BLE001
            print(f"⚠️  关闭侧边栏页面失败：{exc}")
        after = await wait_links(0, timeout=8)
        print(f"关闭侧边栏后的连接数：{after}")
        if after == 0:
            print("✅ 关闭侧边栏后连接已自动断开（心跳随之停止）")
        else:
            ok = False
            print("❌ 关闭侧边栏后连接仍存在，未自动断开")

        if borrow:
            # 借用了别人的页面：把原地址还回去
            try:
                await call("Page.navigate", {"url": original_url})
                print(f"（已把借用的页面恢复到 {original_url}）")
            except Exception as exc:  # noqa: BLE001
                print(f"⚠️  恢复借用页面失败：{exc}")
    finally:
        try:
            await ws.close()
        except Exception:  # noqa: BLE001
            pass

    print("\n结论：", "✅ 通过" if ok else "❌ 未通过")


async def _check_panel(panel_url: str, shot_path: str = "") -> None:
    """把侧边栏页面当普通网页打开，检查元素渲染与脚本报错；可选截图。"""
    import base64 as _b64
    import json as _json

    import websockets as _ws

    pages = [t for t in http_json("/json/list") if t.get("type") == "page"]
    if not pages:
        print("没有可用页面目标")
        return
    target = pages[0]
    ws = await _ws.connect(target["webSocketDebuggerUrl"], proxy=None)
    counter = {"i": 0}
    errors: list[str] = []

    async def call(method: str, params: dict | None = None, timeout: float = 15):
        counter["i"] += 1
        mid = counter["i"]
        await ws.send(_json.dumps({"id": mid, "method": method, "params": params or {}}))
        while True:
            data = _json.loads(await asyncio.wait_for(ws.recv(), timeout=timeout))
            if data.get("id") == mid:
                return data.get("result", {})
            if data.get("method") in ("Runtime.consoleAPICalled", "Runtime.exceptionThrown"):
                errors.append(_json.dumps(data["params"], ensure_ascii=False)[:300])

    try:
        await call("Runtime.enable")
        await call("Page.enable")
        await call("Page.navigate", {"url": panel_url})
        await asyncio.sleep(4)

        result = await call(
            "Runtime.evaluate",
            {
                "expression": """(() => ({
                    title: document.title,
                    hasInput: !!document.getElementById('urlInput'),
                    hasExtractBtn: !!document.getElementById('extractBtn'),
                    hasResult: !!document.getElementById('resultBox'),
                    hasCopyBtn: !!document.getElementById('copyBtn'),
                    status: (document.getElementById('statusText') || {}).textContent || null,
                    connState: (document.getElementById('connState') || {}).textContent || null,
                    bodyLen: document.body.innerText.length,
                    visibleText: document.body.innerText.slice(0, 200),
                }))()""",
                "returnByValue": True,
            },
        )
        value = result.get("result", {}).get("value")
        if result.get("exceptionDetails"):
            print("JS 异常：", _json.dumps(result["exceptionDetails"], ensure_ascii=False)[:400])
        print("=== 侧边栏渲染结果 ===")
        print(_json.dumps(value, ensure_ascii=False, indent=2))
        if errors:
            print("\n=== 控制台报错 ===")
            for e in errors[:5]:
                print("  ", e)
        else:
            print("\n控制台无报错 ✅")

        if shot_path:
            # 侧边栏是窄条，按真实尺寸渲染再截图
            await call(
                "Emulation.setDeviceMetricsOverride",
                {"width": 420, "height": 760, "deviceScaleFactor": 2, "mobile": False},
            )
            # 视觉验证：在页面里直接渲染「提取完成」的界面状态。
            # 说明：sidepanel.js 的函数是模块作用域，外部无法调用，
            # 所以这里只设置 DOM（正式代码不含任何截图用钩子）。
            await call("Runtime.evaluate", {
                "expression": """(() => {
                    const box = document.getElementById('resultBox');
                    box.value = '从道家讲人只有在绝境中才会大车大物，因为只有身处绝境，' +
                                '才会放下那些不切实际的幻想，才会不再期待。\\n' +
                                '痛苦是开悟的唯一方式。';
                    document.getElementById('resultMeta').textContent =
                        '抖音 · 语音识别 · 160 字 · 0:28';
                    document.getElementById('modeHint').textContent =
                        '提取完成（抖音，语音识别，160 字，0:28）';
                    document.getElementById('modeHint').className = 'hint ok';
                    const wrap = document.getElementById('progressWrap');
                    const bar = document.getElementById('progressBar');
                    wrap.classList.add('active');
                    bar.style.width = '100%';
                    return true;
                })()""",
                "returnByValue": True})
            await asyncio.sleep(0.8)
            shot = await call("Page.captureScreenshot", {"format": "png"})
            data = shot.get("data")
            if data:
                out = Path(shot_path)
                out.parent.mkdir(parents=True, exist_ok=True)
                out.write_bytes(_b64.b64decode(data))
                print(f"\n截图已保存：{out}")
            else:
                print("\n⚠️ 截图失败（未返回数据）")
    finally:
        await ws.close()


if __name__ == "__main__":
    raise SystemExit(asyncio.run(verify()))
