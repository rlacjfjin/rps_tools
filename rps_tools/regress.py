"""本地真实数据回归：python -m rps_tools regress {list,snapshot,check}

真实数据不能进仓库，所以回归基线也只存在本地：private/regress/（已 gitignore）。
  snapshot   用当前代码跑一遍所有用例，结果存为基线（改算法前先跑）
  check      再跑一遍，与基线逐文件对比；有差异 / 缺文件 / 运行失败时退出码为 1
  list       查看自动发现的用例和基线状态

用例从本地数据自动发现：
  exp.analyze                   experiments/ 下 4 个文件齐全的每个实验
  diagnose.compare_scenarios    export/ 中前两组 (input, output)
  diagnose.scan_export          export/ 中每个 *proto_expression*.txt（配同名前缀的 input/output）
  diagnose.parse_expressions    同上
对比方式：*.json 用 JSON 结构化对比（列表按主键匹配），其余 .md/.txt/.csv/.mmd 用文本对比。
每个用例在独立子进程里跑，并固定 PYTHONHASHSEED，保证集合遍历顺序稳定、文本输出可比。
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from rps_tools.config import ROOT

REGRESS_DIR = ROOT / "private" / "regress"
COMPARE_SUFFIXES = {".json", ".md", ".txt", ".csv", ".mmd"}
SKIP_FILES = {"patch.json"}   # 由参数生成，含绝对路径


@dataclass
class Case:
    name: str
    tool: str
    params: dict = field(default_factory=dict)


# ---------------------------------------------------------------- discovery
def find_experiments() -> list[Path]:
    """experiments/ 下 patch.json 引用的 4 个文件都在的实验目录，按数据量从小到大。"""
    found = []
    for patch in (ROOT / "experiments").glob("*/patch.json"):
        try:
            files = json.loads(patch.read_text(encoding="utf-8")).get("files", {})
            paths = [patch.parent / v for v in files.values()]
        except (ValueError, OSError):
            continue
        if len(paths) == 4 and all(p.is_file() for p in paths):
            found.append((sum(p.stat().st_size for p in paths), patch.parent))
    return [p for _, p in sorted(found)]


def find_export_pairs() -> list[tuple[Path, Path]]:
    """export/ 里成对的 (input, output)：output 文件名里的 output 换成 input 即为对应的 input。"""
    pairs = []
    for out in sorted((ROOT / "export").glob("*output*.json")):
        inp = out.with_name(out.name.replace("output", "input"))
        if inp.is_file():
            pairs.append((inp, out))
    return pairs


def find_proto_sets() -> list[tuple[Path, Path | None, Path | None]]:
    """export/ 里的 proto_expression.txt，以及同前缀的 model_input / model_output（可能缺）。"""
    out = []
    for proto in sorted((ROOT / "export").glob("*proto_expression*.txt")):
        prefix = proto.name.split("proto_expression")[0]
        inp = next(iter(sorted(proto.parent.glob(f"{prefix}*model_input*.json"))), None)
        outp = next(iter(sorted(proto.parent.glob(f"{prefix}*model_output*.json"))), None)
        out.append((proto, inp, outp))
    return out


def _slug(s: str) -> str:
    return re.sub(r"[^\w.#-]+", "_", s).strip("_") or "case"


def discover() -> list[Case]:
    cases = [Case(f"exp-{_slug(d.name)}", "exp.analyze", {"exp_dir": str(d)}) for d in find_experiments()]
    pairs = find_export_pairs()
    if len(pairs) >= 2:
        (in_a, out_a), (in_b, out_b) = pairs[:2]
        cases.append(Case(f"scenarios-{_slug(out_a.stem)}--{_slug(out_b.stem)}", "diagnose.compare_scenarios",
                          {"before": str(out_a), "after": str(out_b), "before_in": str(in_a), "after_in": str(in_b)}))
    for proto, inp, outp in find_proto_sets():
        cases.append(Case(f"expr-{_slug(proto.stem)}", "diagnose.parse_expressions", {"proto": str(proto)}))
        if inp and outp:
            cases.append(Case(f"scan-{_slug(proto.stem)}", "diagnose.scan_export",
                              {"proto": str(proto), "input": str(inp), "output": str(outp)}))
    return cases


# ---------------------------------------------------------------- run & compare
def run_case(case: Case, dest: Path) -> tuple[bool, float, str]:
    if dest.exists():
        shutil.rmtree(dest)
    env = dict(os.environ, PYTHONHASHSEED="0", PYTHONIOENCODING="utf-8")
    cmd = [sys.executable, "-m", "rps_tools", "run", case.tool,
           *[f"{k}={v}" for k, v in case.params.items()], "--out", str(dest)]
    t0 = time.time()
    proc = subprocess.run(cmd, cwd=ROOT, env=env, capture_output=True, text=True,
                          encoding="utf-8", errors="replace")
    dest.mkdir(parents=True, exist_ok=True)
    (dest / "run.log").write_text(proc.stdout + "\n" + proc.stderr, encoding="utf-8")
    tail = "\n".join((proc.stderr or proc.stdout).strip().splitlines()[-5:])
    return proc.returncode == 0, time.time() - t0, tail


def _artifacts(out_dir: Path) -> dict[str, Path]:
    base = out_dir / "output"
    if not base.is_dir():
        return {}
    return {p.relative_to(base).as_posix(): p for p in sorted(base.rglob("*"))
            if p.is_file() and p.suffix.lower() in COMPARE_SUFFIXES and p.name not in SKIP_FILES}


def compare_file(a: Path, b: Path, work: Path) -> tuple[int, list[str]]:
    """返回 (差异条数, 前几条差异的可读描述)。"""
    from rps_tools.core.resultdb import DiffSink, DiffStore
    from rps_tools.diff import COMPARATORS
    fmt = "json" if a.suffix.lower() == ".json" else "text"
    cmp = COMPARATORS[fmt]
    opts = {o.name: o.default for o in cmp.options}
    db = work / f"{fmt}-{abs(hash((str(a), str(b))))}.sqlite"
    sink = DiffSink(db, preview_len=120)
    cmp.compare(a, b, opts, sink, lambda pct, msg="": None)
    total = sink.close()["total"]
    samples = []
    for r in DiffStore(db).query(limit=5)["items"]:
        where = r["path"] or f"L{r['line_a'] or '-'}/L{r['line_b'] or '-'}"
        samples.append(f"{r['kind']:<8} {where}: {r['old']} → {r['new']}")
    return total, samples


# ---------------------------------------------------------------- commands
def _git_head() -> str:
    try:
        return subprocess.run(["git", "rev-parse", "--short", "HEAD"], cwd=ROOT, capture_output=True,
                              text=True).stdout.strip()
    except OSError:
        return ""


def _select(cases: list[Case], only: str | None) -> list[Case]:
    return [c for c in cases if not only or only in c.name]


def cmd_list(args) -> int:
    cases = _select(discover(), args.only)
    if not cases:
        print("没有发现可用的本地数据（experiments/、export/ 为空）。")
        return 0
    for c in cases:
        has = (REGRESS_DIR / "baseline" / c.name / "output").is_dir()
        print(f"  {'✓' if has else '·'} {c.name:<48} {c.tool}")
    print("\n✓ = 已有基线")
    return 0


def cmd_snapshot(args) -> int:
    cases = _select(discover(), args.only)
    if not cases:
        print("没有发现可用的本地数据。")
        return 1
    failed = 0
    for c in cases:
        ok, sec, tail = run_case(c, REGRESS_DIR / "baseline" / c.name)
        print(f"  {'OK ' if ok else 'ERR'} {c.name:<48} {sec:6.1f}s")
        if not ok:
            failed += 1
            print("      " + tail.replace("\n", "\n      "))
    manifest = {"created": datetime.now().isoformat(timespec="seconds"), "git": _git_head(),
                "cases": [c.__dict__ for c in cases]}
    (REGRESS_DIR / "baseline").mkdir(parents=True, exist_ok=True)
    (REGRESS_DIR / "baseline" / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n基线已保存到 {REGRESS_DIR / 'baseline'}（git {manifest['git'] or '-'}）")
    return 1 if failed else 0


def cmd_check(args) -> int:
    cases = _select(discover(), args.only)
    if not cases:
        print("没有发现可用的本地数据。")
        return 0
    lines = [f"# 回归检查 {datetime.now():%Y-%m-%d %H:%M}（git {_git_head() or '-'}）", ""]
    bad = 0
    with tempfile.TemporaryDirectory() as tmp:
        work = Path(tmp)
        for c in cases:
            base = REGRESS_DIR / "baseline" / c.name
            cur = REGRESS_DIR / "current" / c.name
            if not (base / "output").is_dir():
                print(f"  ?   {c.name:<48} 没有基线，先运行 regress snapshot")
                lines.append(f"- ? `{c.name}` 没有基线")
                bad += 1
                continue
            ok, sec, tail = run_case(c, cur)
            if not ok:
                print(f"  ERR {c.name:<48} 运行失败\n      " + tail.replace("\n", "\n      "))
                lines += [f"- ERR `{c.name}` 运行失败", "```", tail, "```"]
                bad += 1
                continue
            fa, fb = _artifacts(base), _artifacts(cur)
            problems = []
            for rel in sorted(set(fa) | set(fb)):
                if rel not in fb:
                    problems.append((rel, "当前结果缺少该文件", []))
                elif rel not in fa:
                    problems.append((rel, "基线中没有该文件（新增产物）", []))
                else:
                    n, samples = compare_file(fa[rel], fb[rel], work)
                    if n:
                        problems.append((rel, f"{n} 处差异", samples))
            status = "DIFF" if problems else "OK  "
            print(f"  {status} {c.name:<48} {sec:6.1f}s  {len(fa)} 个产物")
            lines.append(f"- {status.strip()} `{c.name}`（{c.tool}，{len(fa)} 个产物）")
            for rel, msg, samples in problems:
                print(f"       {rel}: {msg}")
                lines.append(f"  - `{rel}`：{msg}")
                for s in samples:
                    print(f"         {s}")
                    lines.append(f"    - `{s}`")
            bad += bool(problems)
    report = REGRESS_DIR / "last_check.md"
    report.parent.mkdir(parents=True, exist_ok=True)
    report.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"\n{'全部一致' if not bad else f'{bad} 个用例需要关注'}。详情：{report}")
    if bad and not args.keep:
        print("确认差异是预期内的改进后，运行 regress snapshot 更新基线。")
    return 1 if bad else 0


def add_parser(sub) -> None:
    r = sub.add_parser("regress", help="本地真实数据回归（基线存于 private/regress，不进仓库）")
    rs = r.add_subparsers(dest="regress_cmd", required=True)
    for name, func, help_ in (("list", cmd_list, "查看用例"), ("snapshot", cmd_snapshot, "生成 / 更新基线"),
                              ("check", cmd_check, "与基线对比")):
        p = rs.add_parser(name, help=help_)
        p.add_argument("--only", help="只跑名字包含该字符串的用例")
        if name == "check":
            p.add_argument("--keep", action="store_true", help="不提示更新基线")
        p.set_defaults(func=lambda args, f=func: sys.exit(f(args)))
