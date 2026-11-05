"""提交前的数据隐私检查。仓库是公开的，求解数据和由数据派生的任何内容都不能进仓库。

    python scripts/privacy_check.py             检查暂存区（pre-commit 钩子调用的就是这个）
    python scripts/privacy_check.py --all       检查所有已跟踪文件
    python scripts/privacy_check.py --install   安装 .git/hooks/pre-commit

检查项（任一命中即退出码 1，提交被拦下）：
  1. 数据目录   experiments/ export/ workspace/ private/ 下除 README.md 外的文件
  2. 数据类型   *.json *.mps *.txt *.csv *.sqlite ... 且不在白名单（防止 git add -f）
  3. 大文件     单个文件 > 1 MB
  4. 内部文档   含真实案例、只留本地的 4 份文档
  5. 真实代码   从本地 experiments/、export/ 数据里提取的物料/装置 code、name 和 OBJ 等数值，
               逐个在待提交内容里做整词匹配（结果缓存在 private/privacy_terms.txt）
  6. 个人信息   本机用户目录路径、QQ 邮箱；提交邮箱必须是 GitHub noreply
误报处理：确认是通用词（如 SUL、DEN 这类性质代码）后，加进 scripts/privacy_allowlist.txt。
只依赖标准库，不需要激活虚拟环境。
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
ALLOWLIST = ROOT / "scripts" / "privacy_allowlist.txt"
TERMS_CACHE = ROOT / "private" / "privacy_terms.txt"

DATA_DIRS = ("experiments/", "export/", "workspace/", "private/", "pims-results/")
DATA_SUFFIXES = {".json", ".jsonl", ".mps", ".xmps", ".freemps", ".lp", ".txt", ".log", ".csv", ".tsv",
                 ".xls", ".xlsx", ".mmd", ".html", ".htm", ".jsx", ".sqlite", ".sqlite3", ".db", ".parquet",
                 ".pkl", ".npy", ".npz", ".zip", ".7z", ".rar", ".tar", ".tgz", ".gz", ".pptx", ".docx", ".pdf"}
ALLOWED_FILES = {"requirements.txt", "rps_tools/web/static/index.html",
                 "rps_tools/exp_analyzer/sample_patch.json", "scripts/privacy_allowlist.txt",
                 "experiments/README.md", "export/README.md"}
INTERNAL_DOCS = {"rps_tools/diagnose/CAPABILITIES.md", "rps_tools/diagnose/README.md",
                 "rps_tools/exp_analyzer/PROJECT.md", "rps_tools/exp_analyzer/TRIAGE.md"}
MAX_BYTES = 1024 * 1024
CODE_KEYS = {"code", "name", "material", "crude", "prop", "blend_to"}
NUMBER_KEYS = ("OBJ", "material_revenue", "material_cost", "utility_revenue", "utility_cost")
PERSONAL_RE = r"[A-Za-z]:[\\/]+Users[\\/]+[^\\/\s\"']+|/home/[a-z][\w-]+/|[0-9]{5,}@qq\.com"

HOOK = """#!/bin/sh
# 由 scripts/privacy_check.py --install 生成：提交前检查是否带入了数据 / 真实业务信息
ROOT="$(git rev-parse --show-toplevel)"
PY="$ROOT/.venv/Scripts/python.exe"
[ -x "$PY" ] || PY="$ROOT/.venv/bin/python"
[ -x "$PY" ] || PY="python"
exec "$PY" "$ROOT/scripts/privacy_check.py"
"""


def git(*args: str) -> str:
    return subprocess.run(["git", *args], cwd=ROOT, capture_output=True, check=True).stdout.decode("utf-8", "replace")


# ---------------------------------------------------------------- 敏感词表
def _data_files() -> list[Path]:
    files = [p for p in (ROOT / "export").glob("*.json")]
    files += [p for p in (ROOT / "experiments").glob("*/*.json") if "input" in p.name or "output" in p.name]
    return sorted(files)


def _collect(obj, codes: set[str]) -> None:
    stack = [obj]
    while stack:
        o = stack.pop()
        if isinstance(o, dict):
            for k, v in o.items():
                if k in CODE_KEYS and isinstance(v, str):
                    codes.add(v.strip())
                elif isinstance(v, (dict, list)):
                    stack.append(v)
        elif isinstance(o, list):
            stack.extend(x for x in o if isinstance(x, (dict, list)))


def sensitive_terms() -> list[str]:
    files = _data_files()
    if not files:
        return []
    fingerprint = hashlib.sha1("|".join(f"{p}:{p.stat().st_size}:{p.stat().st_mtime_ns}" for p in files).encode()).hexdigest()
    allow = load_allowlist()
    if TERMS_CACHE.is_file():
        head, _, body = TERMS_CACHE.read_text(encoding="utf-8").partition("\n")
        if head == fingerprint:
            return [t for t in body.splitlines() if t and t not in allow]
    codes: set[str] = set()
    numbers: set[str] = set()
    for p in files:
        try:
            data = json.loads(p.read_text(encoding="utf-8-sig"))
        except (ValueError, OSError):
            continue
        _collect(data, codes)
        if isinstance(data, dict):
            for k in NUMBER_KEYS:
                v = data.get(k)
                if isinstance(v, (int, float)) and abs(v) >= 1000:
                    numbers.add(str(int(abs(v))))
                    numbers.add(f"{int(abs(v)):,}")
    terms = sorted(c for c in codes if len(c) >= 3 and not re.fullmatch(r"[\d.\-+eE]+", c)) + sorted(numbers)
    TERMS_CACHE.parent.mkdir(parents=True, exist_ok=True)
    TERMS_CACHE.write_text(fingerprint + "\n" + "\n".join(terms), encoding="utf-8")
    return [t for t in terms if t not in allow]


def load_allowlist() -> set[str]:
    if not ALLOWLIST.is_file():
        return set()
    return {ln.strip() for ln in ALLOWLIST.read_text(encoding="utf-8").splitlines()
            if ln.strip() and not ln.lstrip().startswith("#")}


# ---------------------------------------------------------------- 检查
def check(all_files: bool) -> list[str]:
    if all_files:
        paths = [p for p in git("ls-files", "-z").split("\0") if p]
    else:
        paths = [p for p in git("diff", "--cached", "--name-only", "--diff-filter=ACMR", "-z").split("\0") if p]
    problems: list[str] = []
    if not paths:
        return problems

    flagged: set[str] = set()   # 已经因为路径/类型/大小被拦的文件，不再扫内容（大 JSON 单行几 MB，扫起来很慢）
    for p in paths:
        low = p.lower()
        if p in ALLOWED_FILES:
            continue
        if low.startswith(DATA_DIRS):
            problems.append(f"[数据目录] {p}")
            flagged.add(p)
        elif Path(low).suffix in DATA_SUFFIXES:
            problems.append(f"[数据类型] {p}（如确需提交，先加进 .gitignore 白名单和本脚本的 ALLOWED_FILES）")
            flagged.add(p)
        if p in INTERNAL_DOCS:
            problems.append(f"[内部文档] {p}（含真实案例，只留本地）")
            flagged.add(p)
    sizes = git("ls-files", "-s", "--", *paths)   # 取暂存区 blob 大小
    for line in sizes.splitlines():
        meta, path = line.split("\t", 1)
        blob = meta.split()[1]
        size = int(git("cat-file", "-s", blob).strip())
        if size > MAX_BYTES:
            problems.append(f"[大文件] {path}（{size / 1024 / 1024:.1f} MB）")
            flagged.add(path)

    scan = [p for p in paths if p not in flagged]
    terms = sensitive_terms()
    if terms and scan:
        pat = ROOT / "private" / ".privacy_patterns.tmp"
        pat.write_text("\n".join(terms), encoding="utf-8")
        try:
            # -o 只输出命中的词本身：path:行号:词
            out = subprocess.run(["git", "grep", "--cached", "-n", "-o", "-I", "-w", "-F", "-f", str(pat),
                                  "--", *scan], cwd=ROOT, capture_output=True).stdout.decode("utf-8", "replace")
        finally:
            pat.unlink(missing_ok=True)
        by_loc: dict[str, set[str]] = {}
        for line in out.splitlines():
            path, lineno, term = line.split(":", 2)
            by_loc.setdefault(f"{path}:{lineno}", set()).add(term)
        for loc, hits in list(by_loc.items())[:50]:
            problems.append(f"[真实代码/数值] {loc}  命中 {', '.join(sorted(hits)[:5])}")
        if len(by_loc) > 50:
            problems.append(f"[真实代码/数值] …… 另有 {len(by_loc) - 50} 处")


    out = subprocess.run(["git", "grep", "--cached", "-n", "-I", "-E", PERSONAL_RE, "--", *scan],
                         cwd=ROOT, capture_output=True).stdout.decode("utf-8", "replace") if scan else ""
    for line in out.splitlines():
        problems.append(f"[个人信息] {':'.join(line.split(':', 2)[:2])}")

    email = subprocess.run(["git", "config", "user.email"], cwd=ROOT, capture_output=True, text=True).stdout.strip()
    if not email.endswith("@users.noreply.github.com"):
        problems.append(f"[提交邮箱] 当前 user.email = {email or '(未设置)'}，会公开在提交记录里；"
                        "请在本仓库设置 GitHub noreply 邮箱：git config user.email <id>+<user>@users.noreply.github.com")
    return problems


def install() -> None:
    hook = ROOT / ".git" / "hooks" / "pre-commit"
    hook.parent.mkdir(parents=True, exist_ok=True)
    hook.write_text(HOOK, encoding="utf-8", newline="\n")
    hook.chmod(0o755)
    print(f"已安装 {hook}")


def main(argv=None) -> int:
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8")
    ap = argparse.ArgumentParser(description="提交前数据隐私检查")
    ap.add_argument("--all", action="store_true", help="检查所有已跟踪文件，而不是只看暂存区")
    ap.add_argument("--install", action="store_true", help="安装 pre-commit 钩子")
    args = ap.parse_args(argv)
    if args.install:
        install()
        return 0
    problems = check(args.all)
    if problems:
        print("✗ 隐私检查未通过，提交已拦下：\n")
        for p in problems:
            print("  " + p)
        print("\n处理方式：把数据文件移出暂存区（git restore --staged <file>），示例改用 MAT-A / U1 这类虚构代码；"
              "确属通用词的误报加进 scripts/privacy_allowlist.txt。")
        return 1
    print("✓ 隐私检查通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
