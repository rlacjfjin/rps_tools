"""统一命令行入口：python -m rps_tools <command>

  serve                      启动 Web 服务
  tools                      列出所有工具及参数
  run <tool_id> k=v ...      在当前进程里直接跑一个工具（与 Web 用的是同一份实现）
  diff <A> <B> [k=v ...]     文件对比的快捷方式，结果另存 CSV
  regress list|snapshot|check  本地真实数据回归（基线不进仓库）

原有入口仍然可用：
  python -m rps_tools.exp_analyzer <experiment_dir>
  python -m rps_tools.diagnose.material_trace --material X ...
"""
from __future__ import annotations

import argparse
import csv
import json
import sys
from datetime import datetime
from pathlib import Path


def _utf8_console() -> None:
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            try:
                stream.reconfigure(encoding="utf-8")
            except Exception:  # noqa: BLE001
                pass


def cmd_serve(args) -> None:
    import uvicorn

    from rps_tools.config import Settings, default_data_roots
    from rps_tools.web.app import create_app

    settings = Settings(host=args.host, port=args.port, workers=args.workers,
                        retention_days=args.retention_days,
                        data_roots=default_data_roots() + [Path(p) for p in args.data_root],
                        restrict_paths=True if args.restrict else None,
                        **({"workspace": Path(args.workspace)} if args.workspace else {}))
    print(f"rps_tools Web 服务启动中 …")
    print(f"  本机访问:  http://127.0.0.1:{args.port}")
    if args.host not in ("127.0.0.1", "localhost"):
        print(f"  局域网访问: http://{_lan_ip()}:{args.port}")
    print(f"  接口文档:  http://127.0.0.1:{args.port}/docs")
    print(f"  任务目录:  {settings.jobs_dir}")
    print(f"  数据目录:  {', '.join(str(p) for p in settings.browse_roots())}"
          + ("（仅允许访问这些目录）" if settings.is_restricted else ""))
    uvicorn.run(create_app(settings), host=args.host, port=args.port, log_level="warning")


def _lan_ip() -> str:
    import socket
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("8.8.8.8", 80))
        return s.getsockname()[0]
    except OSError:
        return "127.0.0.1"
    finally:
        s.close()


def cmd_tools(args) -> None:
    from rps_tools.core.tooling import GROUPS, all_tools
    titles = {g["id"]: g["title"] for g in GROUPS}
    for tool in all_tools():
        if args.tool and tool.id != args.tool:
            continue
        print(f"\n{tool.id}    [{titles[tool.group]}] {tool.title}")
        print(f"    {tool.description}")
        if args.tool or args.verbose:
            for p in tool.params:
                flag = "*" if p.required else " "
                default = f" = {p.default!r}" if p.default not in (None, "", []) else ""
                print(f"   {flag} {p.name:<34} {p.type:<7} {p.label}{default}")


def _parse_kv(pairs: list[str]) -> dict:
    out: dict = {}
    for item in pairs:
        if "=" not in item:
            raise SystemExit(f"参数格式应为 name=value：{item}")
        k, v = item.split("=", 1)
        if k in out:   # multi 类型可以重复给
            out[k] = (out[k] if isinstance(out[k], list) else [out[k]]) + [v]
        else:
            out[k] = v
    return out


def _run_tool(tool_id: str, raw: dict, out: str | None) -> tuple[Path, dict]:
    from rps_tools.config import ROOT
    from rps_tools.core.tooling import JobContext, get_tool

    tool = get_tool(tool_id)
    for p in tool.params:   # 文件/目录参数转成绝对路径
        if p.type in ("file", "dir") and raw.get(p.name):
            raw[p.name] = str(Path(raw[p.name]).resolve())
    params = tool.coerce_params(raw)
    job_dir = Path(out) if out else ROOT / "workspace" / "cli" / datetime.now().strftime("%Y%m%d-%H%M%S")
    job_dir.mkdir(parents=True, exist_ok=True)
    result = tool.run(JobContext(job_dir), params) or {}
    (job_dir / "result.json").write_text(json.dumps(result, ensure_ascii=False, indent=2, default=str),
                                         encoding="utf-8")
    return job_dir, result


def cmd_run(args) -> None:
    job_dir, _ = _run_tool(args.tool, _parse_kv(args.params), args.out)
    print(f"\n产物目录: {job_dir / 'output'}")


def cmd_diff(args) -> None:
    from rps_tools.core.resultdb import KIND_LABELS, DiffStore
    raw = _parse_kv(args.params)
    raw.update({"file_a": args.file_a, "file_b": args.file_b, "format": args.format})
    job_dir, result = _run_tool("diff.compare", raw, args.out)
    csv_path = Path(args.csv) if args.csv else job_dir / "output" / "diff.csv"
    store = DiffStore(job_dir / "output" / "diff.sqlite")
    with open(csv_path, "w", encoding="utf-8-sig", newline="") as fp:
        w = csv.writer(fp)
        w.writerow(["类型", "路径", "旧值", "新值", "差值", "旧行号", "新行号"])
        for r in store.iter_rows():
            w.writerow([KIND_LABELS.get(r["kind"], r["kind"]), r["path"], r["old"], r["new"],
                        r["delta"], r["line_a"], r["line_b"]])
    print(f"\nCSV: {csv_path}")
    sys.exit(1 if result.get("total") else 0)


def main(argv=None) -> None:
    _utf8_console()
    ap = argparse.ArgumentParser(prog="python -m rps_tools", description="rps_solver 诊断工具集")
    sub = ap.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("serve", help="启动 Web 服务")
    s.add_argument("--host", default="127.0.0.1", help="监听地址；0.0.0.0 = 允许局域网访问（此时只能读数据目录）")
    s.add_argument("--port", type=int, default=8765)
    s.add_argument("--workers", type=int, default=2, help="同时运行的任务数")
    s.add_argument("--data-root", action="append", default=[], help="额外允许读取的数据目录，可多次指定")
    s.add_argument("--workspace", help="任务产物目录，默认 <repo>/workspace")
    s.add_argument("--retention-days", type=int, default=14, help="自动清理多少天前的任务，0 = 不清理")
    s.add_argument("--restrict", action="store_true", help="即使只监听本机也只允许读数据目录")
    s.set_defaults(func=cmd_serve)

    t = sub.add_parser("tools", help="列出工具")
    t.add_argument("tool", nargs="?", help="只看某个工具的参数")
    t.add_argument("-v", "--verbose", action="store_true", help="显示全部参数")
    t.set_defaults(func=cmd_tools)

    r = sub.add_parser("run", help="直接运行某个工具")
    r.add_argument("tool")
    r.add_argument("params", nargs="*", help="name=value，参数名见 `tools <tool_id>`")
    r.add_argument("--out", help="产物目录，默认 workspace/cli/<时间>")
    r.set_defaults(func=cmd_run)

    d = sub.add_parser("diff", help="文件对比")
    d.add_argument("file_a")
    d.add_argument("file_b")
    d.add_argument("params", nargs="*", help="对比选项，例 json.abs_tol=1e-6 json.ignore_keys=materialId,dataId")
    d.add_argument("--format", default="auto")
    d.add_argument("--csv", help="CSV 输出路径")
    d.add_argument("--out", help="产物目录")
    d.set_defaults(func=cmd_diff)

    from rps_tools import regress
    regress.add_parser(sub)

    args = ap.parse_args(argv)
    args.func(args)


if __name__ == "__main__":
    main()
