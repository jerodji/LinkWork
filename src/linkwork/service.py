"""LinkWork 本地服务：供浏览器插件调用的 HTTP API。

设计要点：
- 只绑定 127.0.0.1，不对外暴露；
- 所有 /api/* 请求必须带 ``X-LinkWork-Token`` 头（防跨站请求，本地网页无法伪造自定义头）；
- 耗时任务（ASR）走后台线程 + 任务轮询，避免长连接超时；
- 直接复用 LinkWork 现有能力：解析器、转写、文本规范化、导出。
"""

from __future__ import annotations

import base64
import json
import logging
import re
import time
from email.parser import BytesParser
from email.policy import default as email_policy
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import urlparse, parse_qs


from . import __version__, media
from .diagnostics import (
    current_task_log,
    reset_current_task_log,
    set_current_task_log,
    task_log,
)
from .errors import LinkWorkError
from .jobs import Job, JobManager
from .pipeline import ExtractOptions, extract_url
from .resolvers.base import ResolveContext
from .textnorm import normalize_cjk
from .transcribe import transcribe_local_media

from .utils import sanitize_filename, setup_logging

log = logging.getLogger(__name__)

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 17890

#: 允许跨域的来源（插件环境 + 本地调试页）
ALLOWED_ORIGIN_PATTERNS = (
    re.compile(r"^chrome-extension://[a-p]{32}$"),
    re.compile(r"^moz-extension://[0-9a-f-]+$"),
    re.compile(r"^http://127\.0\.0\.1(:\d+)?$"),
    re.compile(r"^http://localhost(:\d+)?$"),
)


class AuthError(Exception):
    pass


class ServiceConfig:
    """服务运行参数。"""

    def __init__(
        self,
        *,
        host: str = DEFAULT_HOST,
        port: int = DEFAULT_PORT,
        token: str = "",
        out_dir: Path | None = None,
        asr_engine: str = "auto",
        asr_model: str = "small",
        language: str | None = None,
        keep_temp: bool = False,
    ) -> None:
        self.host = host
        self.port = port
        self.token = token or ""
        self.out_dir = out_dir
        self.asr_engine = asr_engine
        self.asr_model = asr_model
        self.language = language
        self.keep_temp = keep_temp


def _dump_result(result, *, text: str | None = None) -> dict[str, Any]:
    """把 ExtractResult 转成给插件的精简结构。"""
    body = text if text is not None else (result.spoken_script or result.text)
    return {
        "text": body.strip(),
        "title": result.title,
        "author": result.author,
        "platform": result.platform,
        "kind": result.kind,
        "method": result.method,
        "engine": result.engine,
        "duration": result.duration,
        "char_count": len(re.sub(r"\s+", "", body or "")),
        "segment_count": len(result.segments),
        "url": result.url,
        "warnings": list(result.warnings),
    }


class LinkWorkService:
    """把 LinkWork 的能力包装成一组 JSON API。"""

    def __init__(self, config: ServiceConfig) -> None:
        self.config = config
        self.jobs = JobManager()
        self._started_at = time.time()

    # -- 工具 ---------------------------------------------------------- #
    def _check_token(self, provided: str | None) -> None:
        if not self.config.token:
            return
        if provided != self.config.token:
            raise AuthError("Token 不匹配：请确认插件与服务使用同一密钥")

    def _extract_options(self, *, quiet: bool = True) -> ExtractOptions:
        return ExtractOptions(
            out_dir=self.config.out_dir or (Path.cwd() / "output"),
            formats=("md", "txt"),
            asr_engine=self.config.asr_engine,
            asr_model=self.config.asr_model,
            language=self.config.language,
            keep_temp=self.config.keep_temp,
            quiet=quiet,
        )

    # -- API 实现 ------------------------------------------------------ #
    def health(self) -> dict[str, Any]:
        from .asr import available_engines  # noqa: PLC0415

        from . import cache  # noqa: PLC0415

        return {
            "ok": True,
            "name": "linkwork",
            "version": __version__,
            "ffmpeg": bool(media.find_ffmpeg(required=False)),
            "asr": available_engines(),
            "uptime": round(time.time() - self._started_at, 1),
            "cache": cache.stats(),
        }

    def start_resolve(self, payload: dict[str, Any]) -> Job:
        """提交链接解析任务（图文/视频/音频/文案）。"""
        url = str(payload.get("url") or "").strip()
        if not url:
            raise LinkWorkError("缺少 url 参数")
        job = self.jobs.create("resolve")

        def work(progress) -> dict[str, Any]:
            options = self._extract_options()
            options.progress = progress
            result = extract_url(url, options)
            return _dump_result(result)

        self.jobs.run_async(job, work)
        return job

    def start_transcribe(self, payload: dict[str, Any]) -> Job:
        """就近处理：提交一个页面上的媒体文件（base64）或媒体直链。"""
        media_url = str(payload.get("media_url") or "").strip()
        page_url = str(payload.get("page_url") or "").strip()
        title = str(payload.get("title") or "").strip()
        data_b64 = str(payload.get("data_base64") or "")
        if not media_url and not data_b64:
            raise LinkWorkError("缺少 media_url 或 data_base64")

        job = self.jobs.create("transcribe")
        workdir = Path(media.temp_workspace(prefix="linkwork_ext_"))
        job.workdir = str(workdir)

        def work(progress) -> dict[str, Any]:
            with task_log(
                job.id,
                kind="transcribe",
                inputs={
                    "media_url": media_url,
                    "page_url": page_url,
                    "title": title,
                    "uploaded_base64_len": len(data_b64),
                    "cookies": str(payload.get("cookie") or ""),
                },
            ) as tlog:
                token = set_current_task_log(tlog)
                try:
                    tlog.stage("准备媒体")
                    progress("准备媒体", 0.0)
                    if data_b64:
                        raw = base64.b64decode(data_b64)
                        if not raw:
                            raise LinkWorkError("上传的媒体数据为空")
                        suffix = str(payload.get("suffix") or ".mp4")
                        media_path = workdir / f"uploaded{suffix if suffix.startswith('.') else '.' + suffix}"
                        media_path.write_bytes(raw)
                        tlog.note("来源", "插件上传的字节")
                    else:
                        media_path = self._download_media_for_plugin(
                            media_url, page_url, workdir, payload, progress, tlog
                        )
                    tlog.note("媒体文件", f"{media_path.name}（{media_path.stat().st_size / 1048576:.2f} MB）")

                    # 复用 core 的「转 WAV + 校验 + 识别」流程
                    options = self._extract_options()
                    ctx = ResolveContext(
                        workdir=workdir,
                        asr_engine=options.engine_name(),
                        asr_model=options.asr_model,
                        language=options.language,
                        progress=progress,
                        tasklog=tlog,
                    )
                    tlog.note("识别配置", f"引擎={ctx.asr_engine} 模型={ctx.asr_model} 语言={ctx.language or '自动'}")
                    segments, duration, engine_name, model_name, wav = transcribe_local_media(
                        media_path, ctx, workdir
                    )
                    from .models import ExtractResult  # noqa: PLC0415

                    result = ExtractResult(
                        url=page_url or media_url,
                        kind="video",
                        method="asr",
                        title=title or sanitize_filename(Path(media_path).stem),
                        platform="浏览器插件",
                        segments=segments,
                        duration=duration,
                        engine=f"{engine_name} {model_name}".strip(),
                        artifacts={"media": str(media_path), "wav": str(wav)},
                    )
                    if not segments:
                        raise LinkWorkError("没有识别到任何语音内容（该媒体可能无人声）")
                    tlog.done(
                        {
                            "分段数": len(segments),
                            "字数": result.char_count,
                            "时长": f"{duration:.1f}s" if duration else "未知",
                        }
                    )
                    return _dump_result(result)
                finally:
                    reset_current_task_log(token)
                    # 任务结束即清理，避免媒体文件残留占用空间
                    from . import cache  # noqa: PLC0415

                    cache.cleanup_workspace(workdir)

        self.jobs.run_async(job, work)
        return job

    def _download_media_for_plugin(
        self,
        media_url: str,
        page_url: str,
        workdir: Path,
        payload: dict[str, Any],
        progress,
        tlog=None,
    ) -> Path:
        """按插件提供的媒体直链下载（带页面 Referer 与可选 Cookie）。"""
        import time as _time  # noqa: PLC0415

        import requests  # noqa: PLC0415

        log_ = tlog or current_task_log()
        cookie = str(payload.get("cookie") or "").strip()
        headers = {
            "User-Agent": str(payload.get("user_agent") or media.default_ua()),
            "Referer": page_url or "https://www.douyin.com/",
            "Accept": "*/*",
            "Accept-Language": "zh-CN,zh;q=0.9",
        }
        if cookie:
            headers["Cookie"] = cookie

        host = ""
        try:
            from urllib.parse import urlparse  # noqa: PLC0415

            host = urlparse(media_url).netloc
        except Exception:  # noqa: BLE001, pragma: no cover
            pass

        log_.stage("下载媒体")
        log_.note("媒体域名", host or "(未知)")
        log_.note("携带 Cookie", "是" if cookie else "否")
        log_.note("Referer", headers["Referer"])
        log_.note("User-Agent", headers["User-Agent"][:70])
        progress("下载媒体", 0.05)
        t0 = _time.monotonic()
        try:
            with requests.get(media_url, headers=headers, stream=True, timeout=60) as resp:
                log_.note("HTTP 状态", resp.status_code)
                log_.note("Content-Type", resp.headers.get("Content-Type") or "(无)")
                log_.note("Content-Length", resp.headers.get("Content-Length") or "(未提供)")
                if resp.status_code >= 400:
                    body = b""
                    try:
                        body = next(resp.iter_content(300), b"")
                    except Exception:  # noqa: BLE001, pragma: no cover
                        pass
                    log_.note("响应片段", body[:200].decode("utf-8", "replace"))
                    raise LinkWorkError(
                        f"媒体下载失败：HTTP {resp.status_code}（可能链接已过期或需要登录，请在页面上重新打开视频后重试）"
                    )
                total = int(resp.headers.get("Content-Length") or 0)
                suffix = ".mp4"
                ctype = (resp.headers.get("Content-Type") or "").lower()
                if "audio" in ctype:
                    suffix = ".m4a"
                elif "webm" in ctype:
                    suffix = ".webm"
                path = workdir / f"media{suffix}"
                done = 0
                with path.open("wb") as fh:
                    for chunk in resp.iter_content(chunk_size=1 << 18):
                        if not chunk:
                            continue
                        fh.write(chunk)
                        done += len(chunk)
                        if total:
                            progress("下载媒体", 0.05 + 0.35 * min(done / total, 1.0))
        except LinkWorkError:
            raise
        except Exception as exc:  # noqa: BLE001
            log_.error(f"下载异常：{type(exc).__name__}", exc)
            raise LinkWorkError(f"媒体下载失败：{exc}") from exc

        size = path.stat().st_size if path.exists() else 0
        log_.note("已保存", f"{path.name}（{size / 1048576:.2f} MB，耗时 {_time.monotonic() - t0:.1f}s）")
        if size < 4096:
            log_.warn(f"文件过小（{size} 字节），判定为被平台拒绝")
            raise LinkWorkError("媒体下载失败：文件为空或被平台拒绝")
        progress("下载完成", 0.4)
        return path

    def start_ocr(self, payload: dict[str, Any]) -> Job:
        """对图文笔记的图片做文字识别（复用 macOS Vision）。"""
        images = [str(u) for u in (payload.get("images") or []) if str(u).startswith("http")]
        if not images:
            raise LinkWorkError("缺少 images 参数（图片直链列表）")
        job = self.jobs.create("ocr")
        workdir = Path(media.temp_workspace(prefix="linkwork_ocr_"))
        job.workdir = str(workdir)

        def work(progress) -> dict[str, Any]:
          try:
            from .errors import ASREngineUnavailableError  # noqa: PLC0415
            from .ocr import ocr_available, recognize_images, unavailable_reason  # noqa: PLC0415

            if not ocr_available():
                raise LinkWorkError(f"图片文字识别不可用：{unavailable_reason()}")

            headers = {
                "User-Agent": str(payload.get("user_agent") or media.default_ua()),
                "Referer": str(payload.get("page_url") or "") or None,
                "Accept": "image/avif,image/webp,image/*,*/*;q=0.8",
            }
            headers = {k: v for k, v in headers.items() if v}
            cookie = str(payload.get("cookie") or "").strip()
            if cookie:
                headers["Cookie"] = cookie

            import requests  # noqa: PLC0415

            local: list[Path] = []
            total = min(len(images), 30)
            for index, url in enumerate(images[:30]):
                progress(f"下载图片 {index + 1}/{total}", index / max(total, 1) * 0.6)
                try:
                    resp = requests.get(url, headers=headers, timeout=30)
                    resp.raise_for_status()
                except Exception as exc:  # noqa: BLE001
                    log.warning("图片下载失败 %s：%s", url, exc)
                    continue
                if len(resp.content) < 512:
                    continue
                suffix = ".jpg"
                for ext in (".png", ".webp", ".jpeg", ".jpg"):
                    if ext in url.lower() or ext in (resp.headers.get("Content-Type") or "").lower():
                        suffix = ext
                        break
                path = workdir / f"img_{index:02d}{suffix}"
                path.write_bytes(resp.content)
                local.append(path)

            if not local:
                raise LinkWorkError("所有图片都下载失败，无法做文字识别（可能需要登录或图片已失效）")

            progress("识别图片文字", 0.7)
            ocr_text, failed = recognize_images(local)
            progress("整理文案", 0.95)

            title = str(payload.get("title") or "").strip()
            desc = normalize_cjk(str(payload.get("desc") or "").strip())
            url = str(payload.get("url") or "").strip()

            sections: list[str] = []
            if title:
                sections.append(f"# {title}")
            if desc:
                sections.append(desc)
            if ocr_text.strip():
                sections.append("【图片文字】\n" + normalize_cjk(ocr_text.strip()))
            body = "\n\n".join(sections).strip()
            if len(re.sub(r"\s+", "", body)) < 5:
                raise LinkWorkError("没有从图片或正文中识别到文字")

            warnings = []
            if failed:
                warnings.append(f"有 {len(failed)} 张图片识别失败")
            if url:
                body += f"\n\n来源：{url}"

            from .models import ExtractResult  # noqa: PLC0415

            result = ExtractResult(
                url=url,
                kind="image_text",
                method="ocr",
                title=title,
                author=str(payload.get("author") or ""),
                platform=str(payload.get("platform") or "图文"),
                text=body,
                engine="macOS Vision OCR",
                warnings=warnings,
            )
            return _dump_result(result)
          finally:
            from . import cache  # noqa: PLC0415

            cache.cleanup_workspace(workdir)

        self.jobs.run_async(job, work)
        return job

    # -- 媒体缓存 -------------------------------------------------------- #
    def cache_stats(self) -> dict[str, Any]:
        from . import cache  # noqa: PLC0415

        return cache.stats()

    def cache_clear(self) -> dict[str, Any]:
        """清除已下载的媒体与识别中间产物。"""
        from . import cache  # noqa: PLC0415

        # 保护正在运行的任务工作区（避免把进行中的任务删掉）
        active = {Path(w) for w in self._active_workdirs() if w}
        return cache.clear(keep=active)

    def _active_workdirs(self) -> list[str]:
        return self.jobs.active_workdirs()

    def format_text(self, payload: dict[str, Any]) -> dict[str, Any]:
        """把页面 DOM 提取的文本格式化成可复制文案（不跑 ASR）。"""
        text = str(payload.get("text") or "").strip()
        title = str(payload.get("title") or "").strip()
        author = str(payload.get("author") or "").strip()
        url = str(payload.get("url") or "").strip()
        kind = str(payload.get("kind") or "article")
        if len(re.sub(r"\s+", "", text)) < 10:
            raise LinkWorkError("页面文本过少，无法生成文案（可尝试在页面上滚动加载后重试）")

        body = normalize_cjk(text)
        header_bits = [bit for bit in (title, author) if bit]
        lines: list[str] = []
        if header_bits:
            lines.append(" — ".join(header_bits))
            lines.append("")
        lines.append(body)
        if url:
            lines.extend(["", f"来源：{url}"])
        return {
            "text": "\n".join(lines).strip(),
            "title": title,
            "author": author,
            "platform": str(payload.get("platform") or ""),
            "kind": kind,
            "method": "html",
            "engine": "页面提取",
            "char_count": len(re.sub(r"\s+", "", body)),
            "segment_count": 0,
            "url": url,
            "warnings": [],
        }


def _origin_allowed(origin: str | None) -> bool:
    if not origin:
        # 扩展的 service worker 在部分情况下不发 Origin；此时依赖 token 校验
        return True
    return any(pattern.match(origin) for pattern in ALLOWED_ORIGIN_PATTERNS)


def parse_multipart(body: bytes, content_type: str) -> tuple[dict[str, str], dict[str, bytes]]:
    """解析 multipart/form-data，返回 (普通字段, 文件字段)。"""
    message = BytesParser(policy=email_policy).parsebytes(
        b"Content-Type: " + content_type.encode() + b"\r\nMIME-Version: 1.0\r\n\r\n" + body
    )
    fields: dict[str, str] = {}
    files: dict[str, bytes] = {}
    if not message.is_multipart():
        return fields, files
    for part in message.iter_parts():
        name = part.get_param("name", header="content-disposition")
        if not name:
            continue
        payload = part.get_payload(decode=True) or b""
        filename = part.get_filename()
        if filename:
            files[name] = payload
        else:
            fields[name] = payload.decode("utf-8", "replace")
    return fields, files


class ServiceHandler(BaseHTTPRequestHandler):
    """HTTP 请求处理（由 ThreadingHTTPServer 并发调用）。"""

    server_version = f"LinkWork/{__version__}"
    service: LinkWorkService  # 由 create_server 注入

    # -- 基础 ---------------------------------------------------------- #
    def log_message(self, fmt: str, *args: Any) -> None:  # noqa: A003
        log.debug("%s - %s", self.address_string(), fmt % args)

    def _send_json(self, status: int, payload: dict[str, Any]) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        origin = self.headers.get("Origin")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        if origin and _origin_allowed(origin):
            self.send_header("Access-Control-Allow-Origin", origin)
            self.send_header("Vary", "Origin")
        self.send_header("Access-Control-Allow-Headers", "Content-Type, X-LinkWork-Token")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.end_headers()
        self.wfile.write(body)

    def _error(self, status: int, message: str, code: str = "error") -> None:
        self._send_json(status, {"ok": False, "error": message, "code": code})

    def _read_body(self) -> bytes:
        length = int(self.headers.get("Content-Length") or 0)
        if length <= 0:
            return b""
        return self.rfile.read(length)

    def _read_json(self) -> dict[str, Any]:
        raw = self._read_body()
        if not raw:
            return {}
        try:
            data = json.loads(raw.decode("utf-8"))
        except Exception as exc:  # noqa: BLE001
            raise LinkWorkError(f"请求体不是合法 JSON：{exc}") from exc
        if not isinstance(data, dict):
            raise LinkWorkError("请求体必须是 JSON 对象")
        return data

    def _guard(self) -> bool:
        """校验来源与 token。"""
        origin = self.headers.get("Origin")
        if not _origin_allowed(origin):
            self._error(HTTPStatus.FORBIDDEN, f"不允许的来源：{origin}", "origin_denied")
            return False
        try:
            self.service._check_token(self.headers.get("X-LinkWork-Token"))
        except AuthError as exc:
            self._error(HTTPStatus.UNAUTHORIZED, str(exc), "auth_failed")
            return False
        return True

    # -- 路由 ---------------------------------------------------------- #
    def do_OPTIONS(self) -> None:  # noqa: N802
        self._send_json(HTTPStatus.NO_CONTENT, {})

    def do_GET(self) -> None:  # noqa: N802
        if not self._guard():
            return
        parsed = urlparse(self.path)
        if parsed.path in ("/api/health", "/health"):
            self._send_json(HTTPStatus.OK, self.service.health())
            return
        if parsed.path == "/api/diagnostics":
            from . import __version__  # noqa: PLC0415
            from .diagnostics import (  # noqa: PLC0415
                environment_lines,
                list_task_logs,
                logs_root,
                service_log_path,
                tail_service_log,
            )

            logs = list_task_logs(30)
            failed = [x for x in logs if x.get("outcome") == "error"]
            latest = logs[0] if logs else None
            return self._send_json(
                HTTPStatus.OK,
                {
                    "ok": True,
                    "version": __version__,
                    "log_dir": str(logs_root()),
                    "service_log": str(service_log_path()),
                    "environment": environment_lines(),
                    "summary": {
                        "recent": len(logs),
                        "failed": len(failed),
                        #: 最近一次任务的结局与时间，便于「一看就知道刚才那次成没成」
                        "last_outcome": (latest or {}).get("outcome", "none"),
                        "last_detail": (latest or {}).get("detail", ""),
                        "last_at": (
                            time.strftime(
                                "%Y-%m-%d %H:%M:%S",
                                time.localtime(latest["modified"]),
                            )
                            if latest
                            else ""
                        ),
                    },
                    "logs": logs,
                    "service_log_tail": tail_service_log(),
                },
            )

        if parsed.path == "/api/diagnostics/log":
            from .diagnostics import read_task_log  # noqa: PLC0415

            name = (parse_qs(parsed.query).get("name") or [""])[0]
            if not name:
                return self._send_json(HTTPStatus.BAD_REQUEST, {"ok": False, "error": "缺少 name 参数"})
            try:
                text = read_task_log(name)
            except FileNotFoundError:
                return self._send_json(HTTPStatus.NOT_FOUND, {"ok": False, "error": f"日志不存在：{name}"})
            return self._send_json(HTTPStatus.OK, {"ok": True, "name": name, "text": text})

        if parsed.path == "/api/cache":
            self._send_json(HTTPStatus.OK, {"ok": True, "cache": self.service.cache_stats()})
            return
        match = re.match(r"^/api/jobs/([0-9a-f]+)$", parsed.path)
        if match:
            job = self.service.jobs.get(match.group(1))
            if job is None:
                self._error(HTTPStatus.NOT_FOUND, "任务不存在或已过期", "job_not_found")
                return
            self._send_json(HTTPStatus.OK, job.to_dict())
            return
        self._error(HTTPStatus.NOT_FOUND, f"未知路径：{parsed.path}", "not_found")

    def do_POST(self) -> None:  # noqa: N802
        if not self._guard():
            return
        parsed = urlparse(self.path)
        path = parsed.path
        try:
            if path == "/api/cache/clear":
                result = self.service.cache_clear()
                self._send_json(HTTPStatus.OK, {"ok": True, "cleared": result, "cache": self.service.cache_stats()})
                return
            if path == "/api/resolve":
                payload = self._read_json()
                job = self.service.start_resolve(payload)
            elif path == "/api/transcribe":
                payload = self._read_media_payload()
                job = self.service.start_transcribe(payload)
            elif path == "/api/ocr":
                payload = self._read_json()
                job = self.service.start_ocr(payload)
            elif path == "/api/format":
                payload = self._read_json()
                self._send_json(HTTPStatus.OK, {"ok": True, "result": self.service.format_text(payload)})
                return
            else:
                self._error(HTTPStatus.NOT_FOUND, f"未知路径：{path}", "not_found")
                return
        except LinkWorkError as exc:
            self._error(HTTPStatus.BAD_REQUEST, exc.user_message(), exc.code)
            return
        except Exception as exc:  # noqa: BLE001
            log.exception("处理 %s 失败", path)
            self._error(HTTPStatus.INTERNAL_SERVER_ERROR, f"服务内部错误：{exc}")
            return
        self._send_json(HTTPStatus.ACCEPTED, {"ok": True, "job": job.to_dict()})

    def _read_media_payload(self) -> dict[str, Any]:
        """/api/transcribe 同时支持 JSON 与 multipart 上传。"""
        ctype = (self.headers.get("Content-Type") or "").lower()
        if "multipart/form-data" in ctype:
            body = self._read_body()
            fields, files = parse_multipart(body, self.headers.get("Content-Type") or "")
            payload: dict[str, Any] = dict(fields)
            if files:
                name, data = next(iter(files.items()))
                payload["data_base64"] = base64.b64encode(data).decode("ascii")
                payload.setdefault("suffix", Path(name).suffix or ".mp4")
            return payload
        return self._read_json()


def create_server(config: ServiceConfig) -> ThreadingHTTPServer:
    service = LinkWorkService(config)

    class BoundHandler(ServiceHandler):
        pass

    BoundHandler.service = service
    server = ThreadingHTTPServer((config.host, config.port), BoundHandler)
    server.daemon_threads = True
    server.service = service  # type: ignore[attr-defined]
    return server


def run_service(config: ServiceConfig) -> int:
    """启动服务并阻塞运行。"""
    setup_logging(logging.INFO)
    from . import __version__, cache  # noqa: PLC0415
    from .diagnostics import environment_lines, logs_root  # noqa: PLC0415

    log.info("=" * 60)
    log.info("LinkWork 本地服务启动（端口 %s）", config.port)
    for line in environment_lines():
        log.info("  %s", line)
    log.info("  任务日志目录 %s", logs_root())
    log.info("=" * 60)

    purged = cache.purge_orphans()
    if purged["removed"]:
        log.info("已清理上次遗留的媒体缓存：%s 个目录，释放 %s", purged["removed"], purged["freed_human"])
    server = create_server(config)
    host, port = server.server_address[0], server.server_address[1]
    print(f"LinkWork 本地服务已启动： http://{host}:{port}")
    print(f"健康检查： curl http://{host}:{port}/api/health" + (f" -H 'X-LinkWork-Token: {config.token}'" if config.token else ""))
    if config.token:
        print(f"访问令牌： {config.token}")
    print("按 Ctrl+C 停止。")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n正在停止服务…")
    finally:
        server.shutdown()
        server.server_close()
    return 0
