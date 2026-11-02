"""把所有已注册的对比器包装成一个 Web/CLI 工具：diff.compare。"""
from __future__ import annotations

import time
from pathlib import Path

from rps_tools.core.fileio import human_size
from rps_tools.core.resultdb import DiffSink
from rps_tools.core.tooling import JobContext, Param, ParamError, Tool, register
from rps_tools.diff import COMPARATORS, detect_format

DB_NAME = "diff.sqlite"


def _params() -> list[Param]:
    params = [
        Param("file_a", "旧文件（基准）", type="file", required=True),
        Param("file_b", "新文件（对比）", type="file", required=True),
        Param("format", "对比方式", type="choice", default="auto",
              choices=[("auto", "自动（按扩展名）")] + [(c.name, c.title) for c in COMPARATORS.values()]),
    ]
    for cmp in COMPARATORS.values():
        for opt in cmp.options:
            params.append(Param(
                f"{cmp.name}.{opt.name}", opt.label, type=opt.type, default=opt.default, help=opt.help,
                choices=opt.choices, placeholder=opt.placeholder, advanced=opt.advanced,
                section=f"{cmp.title} 选项", show_if={"format": [cmp.name]}))
    params += [
        Param("max_items", "最多保存差异条数", type="int", default=2_000_000, advanced=True,
              section="结果", help="超过后只计数不保存明细，防止磁盘/查询过慢"),
        Param("preview_len", "单个值最多显示字符", type="int", default=300, advanced=True, section="结果"),
    ]
    return params


def run(ctx: JobContext, p: dict) -> dict:
    path_a, path_b = Path(p["file_a"]), Path(p["file_b"])
    fmt = p.get("format") or "auto"
    if fmt == "auto":
        fmt = detect_format(path_a.name, path_b.name)
    if fmt not in COMPARATORS:
        raise ParamError(f"未知对比方式 {fmt}")
    cmp = COMPARATORS[fmt]
    opts = {o.name: p.get(f"{fmt}.{o.name}", o.default) for o in cmp.options}

    print(f"对比方式: {cmp.title}")
    print(f"旧文件: {path_a}  ({human_size(path_a.stat().st_size)})")
    print(f"新文件: {path_b}  ({human_size(path_b.stat().st_size)})")
    print(f"选项: {opts}")

    sink = DiffSink(ctx.out_dir / DB_NAME, max_items=int(p.get("max_items") or 2_000_000),
                    preview_len=int(p.get("preview_len") or 300))
    t0 = time.time()
    try:
        stats = cmp.compare(path_a, path_b, opts, sink, ctx.progress) or {}
    finally:
        summary = sink.close()
    seconds = time.time() - t0
    print(f"完成：差异 {summary['total']:,} 条，用时 {seconds:.1f}s")
    for kind, n in summary["by_kind"].items():
        print(f"  {kind}: {n:,}")
    return {
        "format": fmt,
        "format_title": cmp.title,
        "file_a": {"name": path_a.name, "size": path_a.stat().st_size},
        "file_b": {"name": path_b.name, "size": path_b.stat().st_size},
        "options": opts,
        "seconds": round(seconds, 2),
        "stats": stats,
        **summary,
    }


register(Tool(
    id="diff.compare",
    group="diff",
    title="文件差异对比",
    description="上传或选择两份文件。JSON 按结构对比（列表按 code 等主键匹配），文本按行对比，MPS 按矩阵元素对比。",
    params=_params(),
    run=run,
    view="diff",
    # 前端在「自动」模式下按文件名猜格式，只展示对应格式的选项
    ui={"format_detect": {
        "param": "format", "files": ["file_a", "file_b"], "fallback": "text",
        "extensions": {ext: c.name for c in COMPARATORS.values() for ext in c.extensions},
        "patterns": {pat: c.name for c in COMPARATORS.values() for pat in c.filename_patterns},
    }},
))
