"""Web 服务（FastAPI）。

页面：      GET  /                     单页应用（static/）
接口文档：  GET  /docs                 自动生成的 OpenAPI 文档，可直接在网页里调用接口
工具：      GET  /api/meta             功能分组 + 所有工具的参数表（前端据此生成表单）
            POST /api/tools/{id}/jobs  提交任务（multipart：文件字段直接上传，或 <name>__path 传服务器路径）
任务：      GET  /api/jobs[/{id}]      列表 / 详情；POST .../cancel；DELETE 删除
            GET  /api/jobs/{id}/log    增量日志（offset）
            GET  /api/jobs/{id}/files  产物列表；GET .../files/<path> 下载/预览；GET .../markdown/<path> 渲染
差异：      GET  /api/jobs/{id}/diff/items       分页 + 筛选查询
            GET  /api/jobs/{id}/diff/export.csv  按当前筛选流式导出
            GET  /api/jobs/{id}/diff/export.json 按当前筛选流式导出，old/new 是未截断的原始值
文件浏览：  GET  /api/fs/list?path=   在服务器上选文件（仅限允许的数据目录）
"""
from __future__ import annotations

import csv
import io
import json
import re
import shutil
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from starlette.datastructures import UploadFile

from rps_tools import __version__
from rps_tools.config import PathAccessError, Settings
from rps_tools.core.jobs import FINAL, JobManager
from rps_tools.core.resultdb import KIND_LABELS, DiffStore, export_item
from rps_tools.core.tooling import GROUPS, ParamError, all_tools, get_tool

STATIC = Path(__file__).parent / "static"
TEXT_SUFFIXES = {".md", ".mmd", ".txt", ".csv", ".json", ".log"}


def _safe_name(name: str) -> str:
    name = Path(name.replace("\\", "/")).name
    name = re.sub(r'[<>:"|?*\x00-\x1f]', "_", name).strip(" .")
    return name or "upload"


def _copy_upload(src, dest: Path) -> None:
    with open(dest, "wb") as out:
        shutil.copyfileobj(src, out, length=4 * 1024 * 1024)


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or Settings()
    manager = JobManager(settings.jobs_dir, max_workers=settings.workers,
                         retention_days=settings.retention_days)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        manager.start()
        yield
        manager.stop()

    app = FastAPI(title="rps_tools", version=__version__, lifespan=lifespan,
                  description="rps_solver 诊断工具集：文件差异对比 / 实验因果链分析 / 物料约束诊断")
    app.state.settings = settings
    app.state.jobs = manager
    app.mount("/static", StaticFiles(directory=STATIC), name="static")

    def job_or_404(job_id: str):
        job = manager.get(job_id)
        if not job:
            raise HTTPException(404, f"任务不存在：{job_id}")
        return job

    def output_path(job_id: str, rel: str) -> Path:
        job_or_404(job_id)
        base = (manager.job_dir(job_id) / "output").resolve()
        p = (base / rel).resolve()
        if base not in p.parents and p != base:
            raise HTTPException(400, "非法路径")
        if not p.is_file():
            raise HTTPException(404, f"文件不存在：{rel}")
        return p

    # ------------------------------------------------------------------ pages & meta
    @app.get("/", include_in_schema=False)
    def index():
        return FileResponse(STATIC / "index.html", headers={"Cache-Control": "no-cache"})

    @app.get("/api/meta")
    def meta():
        return {
            "version": __version__,
            "groups": GROUPS,
            "tools": [t.spec() for t in all_tools()],
            "kind_labels": KIND_LABELS,
            "data_roots": [str(p) for p in settings.browse_roots()],
            "restricted": settings.is_restricted,
            "workers": settings.workers,
        }

    # ------------------------------------------------------------------ submit
    @app.post("/api/tools/{tool_id}/jobs")
    async def submit(tool_id: str, request: Request):
        try:
            tool = get_tool(tool_id)
        except KeyError:
            raise HTTPException(404, f"未知工具：{tool_id}")
        form = await request.form()
        job_id, job_dir = manager.new_job_dir()
        raw, display = {}, {}
        try:
            for p in tool.params:
                if p.type == "file":
                    up = form.get(p.name)
                    server_path = form.get(f"{p.name}__path")
                    if isinstance(up, UploadFile) and up.filename:
                        dest_dir = job_dir / "inputs" / p.name   # 每个参数单独一个目录，避免同名覆盖
                        dest_dir.mkdir(parents=True, exist_ok=True)
                        dest = dest_dir / _safe_name(up.filename)
                        await run_in_threadpool(_copy_upload, up.file, dest)
                        raw[p.name] = str(dest)
                        display[p.name] = f"{up.filename}（上传）"
                    elif isinstance(server_path, str) and server_path.strip():
                        resolved = settings.resolve_path(server_path, "file")
                        raw[p.name] = str(resolved)
                        display[p.name] = str(resolved)
                elif p.type == "dir":
                    value = form.get(p.name)
                    if isinstance(value, str) and value.strip():
                        resolved = settings.resolve_path(value, "dir")
                        raw[p.name] = str(resolved)
                        display[p.name] = str(resolved)
                elif p.type == "multi":
                    raw[p.name] = [v for v in form.getlist(p.name) if isinstance(v, str)]
                    if raw[p.name]:
                        display[p.name] = ", ".join(raw[p.name])
                else:
                    value = form.get(p.name)
                    if isinstance(value, str):
                        raw[p.name] = value
            params = tool.coerce_params(raw)
            for p in tool.params:   # 页面上只展示改过的选项
                v = params.get(p.name)
                if p.name not in display and v not in (None, "", []) and v != p.default:
                    display[p.name] = ", ".join(v) if isinstance(v, list) else str(v)
        except (ParamError, PathAccessError) as e:
            shutil.rmtree(job_dir, ignore_errors=True)
            raise HTTPException(400, str(e))
        finally:
            await form.close()
        job = manager.submit(job_id, tool, params, display)
        return job.to_dict()

    # ------------------------------------------------------------------ jobs
    @app.get("/api/jobs")
    def list_jobs(limit: int = 200, group: str | None = None):
        return [j.to_dict() for j in manager.list(limit=limit, group=group)]

    @app.get("/api/jobs/{job_id}")
    def get_job(job_id: str):
        return job_or_404(job_id).to_dict()

    @app.post("/api/jobs/{job_id}/cancel")
    def cancel_job(job_id: str):
        job_or_404(job_id)
        return manager.cancel(job_id).to_dict()

    @app.delete("/api/jobs/{job_id}")
    def delete_job(job_id: str):
        job_or_404(job_id)
        manager.delete(job_id)
        return {"ok": True}

    @app.get("/api/jobs/{job_id}/log")
    def job_log(job_id: str, offset: int = 0, limit: int = 256 * 1024):
        job = job_or_404(job_id)
        path = manager.job_dir(job_id) / "log.txt"
        if not path.is_file():
            return {"offset": 0, "text": "", "done": job.status in FINAL}
        with open(path, "rb") as fp:
            size = fp.seek(0, 2)
            if offset > size:
                offset = 0
            fp.seek(offset)
            chunk = fp.read(limit)
        # 末尾可能截断在多字节字符中间：丢弃不完整的字节，offset 按实际解码的长度前进
        text = chunk.decode("utf-8", errors="ignore")
        new_offset = offset + len(text.encode("utf-8"))
        return {"offset": new_offset, "text": text, "size": size,
                "done": job.status in FINAL and new_offset >= size}

    @app.get("/api/jobs/{job_id}/files")
    def job_files(job_id: str):
        job_or_404(job_id)
        base = manager.job_dir(job_id) / "output"
        if not base.is_dir():
            return []
        return [{"path": p.relative_to(base).as_posix(), "size": p.stat().st_size}
                for p in sorted(base.rglob("*")) if p.is_file() and p.suffix != ".sqlite"]

    @app.get("/api/jobs/{job_id}/files/{rel:path}")
    def job_file(job_id: str, rel: str, download: bool = False):
        p = output_path(job_id, rel)
        media = "text/plain; charset=utf-8" if p.suffix.lower() in TEXT_SUFFIXES else None
        if p.suffix.lower() == ".json":
            media = "application/json; charset=utf-8"
        if download:
            return FileResponse(p, filename=p.name, media_type=media)
        return FileResponse(p, media_type=media)

    @app.get("/api/jobs/{job_id}/markdown/{rel:path}", response_class=HTMLResponse)
    def job_markdown(job_id: str, rel: str):
        import markdown
        p = output_path(job_id, rel)
        html = markdown.markdown(p.read_text(encoding="utf-8"),
                                 extensions=["markdown.extensions.tables", "markdown.extensions.fenced_code",
                                             "markdown.extensions.toc", "markdown.extensions.sane_lists"])
        # 报告里的大段 mermaid 源码默认折叠（图在 *.html 里看）
        html = re.sub(r'(<pre><code class="language-mermaid">.*?</code></pre>)',
                      r'<details class="mermaid-src"><summary>mermaid 源码（图请打开对应的 .html）</summary>\1</details>',
                      html, flags=re.S)
        return HTMLResponse(html)

    # ------------------------------------------------------------------ diff results
    def diff_store(job_id: str) -> DiffStore:
        job = job_or_404(job_id)
        if job.tool_id != "diff.compare":
            raise HTTPException(400, "不是文件对比任务")
        db = manager.job_dir(job_id) / "output" / "diff.sqlite"
        if not db.is_file():
            raise HTTPException(404, "结果尚未生成")
        return DiffStore(db)

    @app.get("/api/jobs/{job_id}/diff/items")
    def diff_items(job_id: str, kind: str | None = None, section: str | None = None,
                   field: str | None = None, q: str | None = None, sort: str = "id",
                   offset: int = 0, limit: int = Query(200, le=2000)):
        return diff_store(job_id).query(kind=kind, section=section, field=field, q=q,
                                        sort=sort, offset=offset, limit=limit)

    @app.get("/api/jobs/{job_id}/diff/export.csv")
    def diff_export(job_id: str, kind: str | None = None, section: str | None = None,
                    field: str | None = None, q: str | None = None, sort: str = "id"):
        store = diff_store(job_id)
        is_text = manager.get(job_id).result.get("format") == "text"

        def rows():
            buf = io.StringIO()
            w = csv.writer(buf)
            yield "﻿"   # BOM，Excel 直接打开不乱码
            if is_text:
                w.writerow(["类型", "旧文件行号", "新文件行号", "旧内容", "新内容", "最大数值差"])
            else:
                w.writerow(["类型", "路径", "顶层", "字段", "旧值", "新值", "差值", "相对差", "备注"])
            for n, r in enumerate(store.iter_rows(kind, section, field, q, sort)):
                label = KIND_LABELS.get(r["kind"], r["kind"])
                if is_text:
                    w.writerow([label, r["line_a"], r["line_b"], r["old"], r["new"], r["delta"]])
                else:
                    w.writerow([label, r["path"], r["section"], r["field"], r["old"], r["new"],
                                r["delta"], r["rel"], r["note"]])
                if n % 2000 == 0:
                    yield buf.getvalue()
                    buf.seek(0)
                    buf.truncate()
            yield buf.getvalue()

        return StreamingResponse(rows(), media_type="text/csv; charset=utf-8",
                                 headers={"Content-Disposition": f'attachment; filename="diff_{job_id}.csv"'})

    @app.get("/api/jobs/{job_id}/diff/export.json")
    def diff_export_json(job_id: str, kind: str | None = None, section: str | None = None,
                         field: str | None = None, q: str | None = None, sort: str = "id"):
        store = diff_store(job_id)
        result = manager.get(job_id).result or {}
        filters = {k: v for k, v in (("kind", kind), ("section", section), ("field", field), ("q", q)) if v}
        head = {
            "file_a": (result.get("file_a") or {}).get("name"),
            "file_b": (result.get("file_b") or {}).get("name"),
            "format": result.get("format"),
            "options": result.get("options"),
            "filters": filters,
            "total": result.get("total"),
            "exported": store.query(kind=kind, section=section, field=field, q=q, limit=1)["total"],
            "truncated": bool(result.get("truncated")),
            "complete_values": store.has_full_values(),
        }

        def chunks():
            yield json.dumps(head, ensure_ascii=False, default=str)[:-1] + ',"diffs":['
            buf = []
            for n, r in enumerate(store.iter_rows(kind, section, field, q, sort, full=True)):
                buf.append(("," if n else "") + "\n" + export_item(r))
                if len(buf) >= 2000:
                    yield "".join(buf)
                    buf.clear()
            yield "".join(buf) + "\n]}\n"

        return StreamingResponse(chunks(), media_type="application/json; charset=utf-8",
                                 headers={"Content-Disposition": f'attachment; filename="diff_{job_id}.json"'})

    # ------------------------------------------------------------------ file browser
    @app.get("/api/fs/list")
    def fs_list(path: str | None = None):
        roots = settings.browse_roots()
        if not path:
            return {"path": None, "parent": None,
                    "entries": [{"name": str(r), "path": str(r), "dir": True} for r in roots if r.exists()]}
        try:
            p = settings.resolve_path(path, "dir")
        except PathAccessError as e:
            raise HTTPException(400, str(e))
        entries = []
        try:
            children = sorted(p.iterdir(), key=lambda c: (not c.is_dir(), c.name.lower()))
        except PermissionError:
            raise HTTPException(403, f"没有权限读取：{p}")
        for c in children:
            if c.name.startswith(".") or c.name == "__pycache__":
                continue
            try:
                is_dir = c.is_dir()
                entries.append({"name": c.name, "path": str(c), "dir": is_dir,
                                "size": None if is_dir else c.stat().st_size,
                                "mtime": c.stat().st_mtime})
            except OSError:
                continue
        parent = None
        if p.parent != p:
            try:
                settings.resolve_path(str(p.parent), "dir")
                parent = str(p.parent)
            except PathAccessError:
                parent = ""   # 已到允许范围的顶层，返回根列表
        return {"path": str(p), "parent": parent, "entries": entries}

    @app.exception_handler(PathAccessError)
    async def _path_error(request: Request, exc: PathAccessError):
        return JSONResponse({"detail": str(exc)}, status_code=400)

    return app
