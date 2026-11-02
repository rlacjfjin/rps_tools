"""后台任务管理：每个任务一个独立工作进程。

为什么不在 HTTP 请求里直接算：
  * 大文件对比/分析动辄几十秒，同步请求会被浏览器/代理超时打断，页面看起来就是"崩了"；
  * 解析大 JSON 很吃内存，放在独立进程里跑，结束后内存完整归还；进程被 OOM 杀掉也不影响服务本身；
  * CPU 密集计算不会卡住 Web 线程（GIL），页面仍然可以浏览其它结果。

任务目录结构（workspace/jobs/<job_id>/）：
  job.json     任务元数据 + 状态 + 结果摘要（只有主进程写）
  log.txt      工作进程的 stdout/stderr
  inputs/      上传的文件
  output/      工具产物（报告、CSV、diff.sqlite ...）
"""
from __future__ import annotations

import json
import multiprocessing as mp
import os
import queue as queue_mod
import shutil
import sys
import threading
import time
import traceback
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path

QUEUED, RUNNING, SUCCEEDED, FAILED, CANCELLED, INTERRUPTED = (
    "queued", "running", "succeeded", "failed", "cancelled", "interrupted")
FINAL = {SUCCEEDED, FAILED, CANCELLED, INTERRUPTED}


@dataclass
class Job:
    id: str
    tool_id: str
    title: str
    group: str
    status: str = QUEUED
    created: float = field(default_factory=time.time)
    started: float | None = None
    finished: float | None = None
    progress: float | None = None
    message: str = ""
    error: str = ""
    params: dict = field(default_factory=dict)          # 实际传给工具的参数
    display_params: dict = field(default_factory=dict)  # 页面展示用（上传文件显示原文件名）
    result: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        d = asdict(self)
        end = self.finished or (time.time() if self.started else None)
        d["duration"] = (end - self.started) if (self.started and end) else None
        return d


def new_job_id() -> str:
    return datetime.now().strftime("%Y%m%d-%H%M%S-") + uuid.uuid4().hex[:6]


def _worker_main(job_id: str, tool_id: str, params: dict, job_dir: str, events) -> None:
    """工作进程入口（spawn 方式启动，必须是模块级函数）。"""
    job_path = Path(job_dir)
    log = open(job_path / "log.txt", "a", encoding="utf-8", buffering=1)
    sys.stdout = sys.stderr = log
    os.chdir(job_path)
    sys.setrecursionlimit(20000)

    def emit(ev: dict) -> None:
        ev["job"] = job_id
        events.put(ev)

    try:
        from rps_tools.core.tooling import JobContext, get_tool
        tool = get_tool(tool_id)
        emit({"type": "started", "pid": os.getpid()})
        result = tool.run(JobContext(job_path, emit), params) or {}
        emit({"type": "done", "result": result})
    except SystemExit as e:  # 诊断脚本用 sys.exit("原因") 报错
        if e.code in (None, 0):
            emit({"type": "done", "result": {}})
        else:
            print(f"\n[失败] {e.code}")
            emit({"type": "failed", "error": str(e.code)})
    except BaseException as e:  # noqa: BLE001  —— 任何异常都要回报给主进程
        traceback.print_exc()
        emit({"type": "failed", "error": f"{type(e).__name__}: {e}"})
    finally:
        log.flush()


class JobManager:
    def __init__(self, jobs_dir: Path, max_workers: int = 2, retention_days: int = 14):
        self.jobs_dir = Path(jobs_dir)
        self.jobs_dir.mkdir(parents=True, exist_ok=True)
        self.max_workers = max(1, max_workers)
        self.retention_days = retention_days
        self._mp = mp.get_context("spawn")
        self._events = self._mp.Queue()
        self._jobs: dict[str, Job] = {}
        self._procs: dict[str, mp.Process] = {}
        self._lock = threading.RLock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._last_persist: dict[str, float] = {}

    # ------------------------------------------------------------------ lifecycle
    def start(self) -> None:
        self._load_existing()
        self._cleanup_old()
        self._thread = threading.Thread(target=self._supervise, name="job-supervisor", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        with self._lock:
            for job_id, proc in list(self._procs.items()):
                if proc.is_alive():
                    proc.terminate()
                job = self._jobs.get(job_id)
                if job and job.status not in FINAL:
                    self._finish(job, INTERRUPTED, error="服务停止，任务被中断")
        if self._thread:
            self._thread.join(timeout=3)

    def _load_existing(self) -> None:
        for meta in self.jobs_dir.glob("*/job.json"):
            try:
                data = json.loads(meta.read_text(encoding="utf-8"))
                data.pop("duration", None)
                job = Job(**data)
            except Exception:  # noqa: BLE001  损坏的 job.json 直接跳过
                continue
            if job.status not in FINAL:
                job.status = INTERRUPTED
                job.error = job.error or "服务重启，任务被中断"
                job.finished = job.finished or time.time()
                self._persist(job)
            self._jobs[job.id] = job

    def _cleanup_old(self) -> None:
        if self.retention_days <= 0:
            return
        cutoff = time.time() - self.retention_days * 86400
        for job in list(self._jobs.values()):
            if job.status in FINAL and job.created < cutoff:
                self.delete(job.id)

    # ------------------------------------------------------------------ api
    def new_job_dir(self) -> tuple[str, Path]:
        job_id = new_job_id()
        path = self.jobs_dir / job_id
        (path / "inputs").mkdir(parents=True)
        return job_id, path

    def job_dir(self, job_id: str) -> Path:
        return self.jobs_dir / job_id

    def submit(self, job_id: str, tool, params: dict, display_params: dict) -> Job:
        job = Job(id=job_id, tool_id=tool.id, title=tool.title, group=tool.group,
                  params=params, display_params=display_params, message="排队中")
        with self._lock:
            self._jobs[job_id] = job
            self._persist(job)
        return job

    def get(self, job_id: str) -> Job | None:
        return self._jobs.get(job_id)

    def list(self, limit: int = 200, group: str | None = None) -> list[Job]:
        jobs = [j for j in self._jobs.values() if not group or j.group == group]
        jobs.sort(key=lambda j: j.created, reverse=True)
        return jobs[:limit]

    def cancel(self, job_id: str) -> Job | None:
        with self._lock:
            job = self._jobs.get(job_id)
            if not job or job.status in FINAL:
                return job
            proc = self._procs.pop(job_id, None)
            if proc and proc.is_alive():
                proc.terminate()
            self._finish(job, CANCELLED, error="已取消")
            return job

    def delete(self, job_id: str) -> bool:
        with self._lock:
            job = self._jobs.get(job_id)
            if job and job.status not in FINAL:
                self.cancel(job_id)
            self._jobs.pop(job_id, None)
        path = self.job_dir(job_id)
        if path.is_dir():
            shutil.rmtree(path, ignore_errors=True)
        return job is not None

    # ------------------------------------------------------------------ supervisor
    def _supervise(self) -> None:
        while not self._stop.is_set():
            self._drain_events(timeout=0.3)
            with self._lock:
                self._reap()
                self._launch()

    def _drain_events(self, timeout: float) -> None:
        try:
            ev = self._events.get(timeout=timeout)
        except queue_mod.Empty:
            return
        except (EOFError, OSError):
            return
        while ev is not None:
            self._handle(ev)
            try:
                ev = self._events.get_nowait()
            except (queue_mod.Empty, EOFError, OSError):
                ev = None

    def _handle(self, ev: dict) -> None:
        with self._lock:
            job = self._jobs.get(ev.get("job"))
            if not job or job.status in FINAL:
                return
            kind = ev.get("type")
            if kind == "started":
                job.message = "运行中"
                self._persist(job)
            elif kind == "progress":
                if ev.get("pct") is not None:
                    job.progress = float(ev["pct"])
                if ev.get("msg"):
                    job.message = ev["msg"]
                self._persist(job, throttle=True)
            elif kind == "done":
                job.result = ev.get("result") or {}
                job.progress = 100.0
                self._finish(job, SUCCEEDED, message="完成")
                self._procs.pop(job.id, None)
            elif kind == "failed":
                self._finish(job, FAILED, error=ev.get("error") or "未知错误")
                self._procs.pop(job.id, None)

    def _reap(self) -> None:
        for job_id, proc in list(self._procs.items()):
            if proc.is_alive():
                continue
            proc.join(timeout=0.1)
            # 进程已退出：先把管道里残留的事件读完，再判断是不是异常退出
            self._drain_events(timeout=0.05)
            self._procs.pop(job_id, None)
            job = self._jobs.get(job_id)
            if job and job.status not in FINAL:
                self._finish(job, FAILED, error=(
                    f"工作进程异常退出 (exitcode={proc.exitcode})。"
                    "常见原因：文件过大导致内存不足，或进程被系统终止。"))

    def _launch(self) -> None:
        running = sum(1 for p in self._procs.values() if p.is_alive())
        if running >= self.max_workers:
            return
        queued = sorted((j for j in self._jobs.values() if j.status == QUEUED), key=lambda j: j.created)
        for job in queued[: self.max_workers - running]:
            proc = self._mp.Process(
                target=_worker_main, name=f"job-{job.id}", daemon=True,
                args=(job.id, job.tool_id, job.params, str(self.job_dir(job.id)), self._events))
            job.status = RUNNING
            job.started = time.time()
            job.message = "启动工作进程"
            self._persist(job)
            try:
                proc.start()
            except Exception as e:  # noqa: BLE001
                self._finish(job, FAILED, error=f"无法启动工作进程：{e}")
                continue
            self._procs[job.id] = proc

    # ------------------------------------------------------------------ persistence
    def _finish(self, job: Job, status: str, message: str = "", error: str = "") -> None:
        job.status = status
        job.finished = time.time()
        if message:
            job.message = message
        if error:
            job.error = error
            job.message = {FAILED: "失败", CANCELLED: "已取消"}.get(status, job.message)
        self._persist(job)

    def _persist(self, job: Job, throttle: bool = False) -> None:
        now = time.time()
        if throttle and now - self._last_persist.get(job.id, 0) < 2:
            return
        self._last_persist[job.id] = now
        path = self.job_dir(job.id)
        if not path.is_dir():
            return
        tmp = path / "job.json.tmp"
        tmp.write_text(json.dumps(job.to_dict(), ensure_ascii=False, indent=1, default=str), encoding="utf-8")
        for _ in range(5):  # Windows 下目标文件被占用时 replace 会短暂失败
            try:
                os.replace(tmp, path / "job.json")
                return
            except PermissionError:
                time.sleep(0.05)
