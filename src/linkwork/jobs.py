"""本地服务：后台任务队列与状态管理。

扩展（浏览器插件）通过 HTTP 提交任务，任务在后台线程里跑，
扩展轮询状态即可拿到结果——避免长请求超时。
"""

from __future__ import annotations

import logging
import threading
import time
import uuid
from collections import OrderedDict
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, Literal

log = logging.getLogger(__name__)

JobState = Literal["pending", "running", "done", "error"]

#: 最多保留的历史任务数（防止内存无限增长）
MAX_JOBS = 200


@dataclass
class Job:
    id: str
    kind: str
    state: JobState = "pending"
    stage: str = ""
    progress: float = 0.0
    result: dict[str, Any] | None = None
    error: str = ""
    error_code: str = ""
    #: 该任务的工作区（媒体中间产物所在目录），清理缓存时需要排除运行中的任务
    workdir: str = ""
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)

    def to_dict(self) -> dict[str, Any]:
        data: dict[str, Any] = {
            "id": self.id,
            "kind": self.kind,
            "state": self.state,
            "stage": self.stage,
            "progress": round(self.progress, 4),
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }
        if self.state == "done":
            data["result"] = self.result
        if self.state == "error":
            data["error"] = self.error
            data["error_code"] = self.error_code
        return data


class JobManager:
    """线程安全的任务管理器（每个任务跑在独立线程里）。"""

    def __init__(self, max_jobs: int = MAX_JOBS) -> None:
        self._jobs: OrderedDict[str, Job] = OrderedDict()
        self._lock = threading.Lock()
        self._max_jobs = max_jobs

    def create(self, kind: str) -> Job:
        job = Job(id=uuid.uuid4().hex[:16], kind=kind)
        with self._lock:
            self._jobs[job.id] = job
            while len(self._jobs) > self._max_jobs:
                self._jobs.popitem(last=False)
        return job

    def active_workdirs(self) -> list[str]:
        """返回仍在进行中的任务工作区（用于清理时保护）。"""
        with self._lock:
            return [
                job.workdir
                for job in self._jobs.values()
                if job.workdir and job.state in ("pending", "running")
            ]

    def get(self, job_id: str) -> Job | None:
        with self._lock:
            return self._jobs.get(job_id)

    def update(
        self,
        job_id: str,
        *,
        state: JobState | None = None,
        stage: str | None = None,
        progress: float | None = None,
    ) -> None:
        with self._lock:
            job = self._jobs.get(job_id)
            if job is None:
                return
            if state is not None:
                job.state = state
            if stage is not None:
                job.stage = stage
            if progress is not None:
                job.progress = max(0.0, min(float(progress), 1.0))
            job.updated_at = time.time()

    def finish(self, job_id: str, result: dict[str, Any]) -> None:
        with self._lock:
            job = self._jobs.get(job_id)
            if job is None:
                return
            job.state = "done"
            job.result = result
            job.progress = 1.0
            job.stage = "完成"
            job.updated_at = time.time()

    def fail(self, job_id: str, message: str, code: str = "error") -> None:
        with self._lock:
            job = self._jobs.get(job_id)
            if job is None:
                return
            job.state = "error"
            job.error = message
            job.error_code = code
            job.updated_at = time.time()

    def run_async(self, job: Job, work: Callable[[Callable[[str, float], None]], dict[str, Any]]) -> None:
        """在后台线程执行 work；work 通过回调上报进度。"""

        def progress(stage: str, ratio: float) -> None:
            self.update(job.id, stage=stage, progress=ratio)

        def runner() -> None:
            self.update(job.id, state="running", stage="开始处理", progress=0.0)
            log.info("任务 %s（%s）开始", job.id, job.kind)
            started = time.time()
            stage_marks: list[tuple[str, float]] = []
            last_stage = {"name": ""}

            def timed_progress(stage: str, ratio: float) -> None:
                if stage != last_stage["name"]:
                    now = time.time()
                    stage_marks.append((stage, now - started))
                    last_stage["name"] = stage
                    log.info("任务 %s 阶段：%s（%.1fs）", job.id, stage, now - started)
                progress(stage, ratio)

            try:
                result = work(timed_progress)
            except Exception as exc:  # noqa: BLE001
                elapsed = time.time() - started
                log.exception("任务 %s 失败（耗时 %.1fs，阶段=%s）", job.id, elapsed, last_stage["name"])
                code = getattr(exc, "code", "error")
                message = exc.user_message() if hasattr(exc, "user_message") else str(exc)
                self.fail(job.id, message, code)
                return
            elapsed = time.time() - started
            stages = " → ".join(f"{name}@{t:.1f}s" for name, t in stage_marks)
            log.info("任务 %s 完成（耗时 %.1fs）阶段耗时：%s", job.id, elapsed, stages or "（无阶段）")
            self.finish(job.id, result)

        threading.Thread(target=runner, name=f"linkwork-job-{job.id}", daemon=True).start()
