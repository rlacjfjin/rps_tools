"""文本按行对比（适用于 .txt / .log / .xmps / proto_expression 等任意文本）。

旧版直接用 difflib.SequenceMatcher 做整文件对齐，复杂度接近 O(N²)：
40 万行的文件、几百处零散改动就要跑好几分钟甚至卡死。这里改用 patience diff：
  1. 去掉首尾公共部分；
  2. 以「两边都只出现一次的行」为锚点，取最长递增子序列（O(n log n)），把问题切成很多小段；
  3. 每段递归处理；没有唯一行的段（高度重复的结构）用 Myers 算法，改动多时再退回 difflib。
数值容差模式下，先把行里的数字替换成占位符再对齐，对齐后再逐个比较数字是否在容差内。
"""
from __future__ import annotations

import bisect
import difflib
import re
from pathlib import Path

from rps_tools.core.fileio import read_text
from rps_tools.core.resultdb import MISSING
from rps_tools.core.tooling import Param
from rps_tools.diff.base import Comparator, register_comparator

DIFFLIB_MAX_CELLS = 4_000_000
MYERS_MAX_D = 2000

# 独立的数字（不匹配标识符里的数字，如 x12 / R3d）
NUM_RE = re.compile(r"(?<![A-Za-z_\d.])[-+]?(?:\d+\.?\d*|\.\d+)(?:[eE][-+]?\d+)?(?![A-Za-z_\d])")
_WS_RE = re.compile(r"\s+")


def _lis(pairs: list[tuple[int, int]]) -> list[tuple[int, int]]:
    """pairs 按第一维升序；返回第二维严格递增的最长子序列。"""
    tails: list[int] = []        # tails[k] = 长度 k+1 的子序列末尾的 b 下标
    tails_idx: list[int] = []
    prev = [-1] * len(pairs)
    for n, (_, bj) in enumerate(pairs):
        k = bisect.bisect_left(tails, bj)
        if k == len(tails):
            tails.append(bj)
            tails_idx.append(n)
        else:
            tails[k] = bj
            tails_idx[k] = n
        prev[n] = tails_idx[k - 1] if k > 0 else -1
    out = []
    n = tails_idx[-1] if tails_idx else -1
    while n >= 0:
        out.append(pairs[n])
        n = prev[n]
    out.reverse()
    return out


def _myers(a, b, alo, ahi, blo, bhi, max_d):
    """Myers O((N+M)·D) 最短编辑脚本；编辑距离超过 max_d 时返回 None。"""
    n, m = ahi - alo, bhi - blo
    max_d = min(max_d, n + m)
    off = max_d + 1
    v = [0] * (2 * max_d + 3)
    trace = []
    for d in range(max_d + 1):
        trace.append(v[off - d - 1: off + d + 2])   # 只保存本轮会读到的 k 范围
        for k in range(-d, d + 1, 2):
            if k == -d or (k != d and v[off + k - 1] < v[off + k + 1]):
                x = v[off + k + 1]
            else:
                x = v[off + k - 1] + 1
            y = x - k
            while x < n and y < m and a[alo + x] == b[blo + y]:
                x += 1
                y += 1
            v[off + k] = x
            if x >= n and y >= m:
                return _myers_backtrack(trace, d, n, m, alo, blo)
    return None


def _myers_backtrack(trace, dmax, n, m, alo, blo):
    out = []
    x, y = n, m
    for d in range(dmax, -1, -1):
        saved = trace[d]            # saved[i] 对应 k = i - d - 1

        def V(k):
            return saved[k + d + 1]
        k = x - y
        prev_k = k + 1 if (k == -d or (k != d and V(k - 1) < V(k + 1))) else k - 1
        prev_x = V(prev_k)
        prev_y = prev_x - prev_k
        while x > prev_x and y > prev_y:
            x -= 1
            y -= 1
            out.append((alo + x, blo + y))
        x, y = prev_x, prev_y
    return out


def match_sequences(a: list[int], b: list[int]) -> list[tuple[int, int]]:
    """patience diff：返回两边相等元素的对应关系 [(i, j)]，i、j 均严格递增。"""
    matches: list[tuple[int, int]] = []
    stack = [(0, len(a), 0, len(b))]
    while stack:
        alo, ahi, blo, bhi = stack.pop()
        while alo < ahi and blo < bhi and a[alo] == b[blo]:
            matches.append((alo, blo))
            alo += 1
            blo += 1
        while alo < ahi and blo < bhi and a[ahi - 1] == b[bhi - 1]:
            ahi -= 1
            bhi -= 1
            matches.append((ahi, bhi))
        if alo >= ahi or blo >= bhi:
            continue
        ua: dict[int, int] = {}
        for i in range(alo, ahi):
            v = a[i]
            ua[v] = -1 if v in ua else i
        ub: dict[int, int] = {}
        for j in range(blo, bhi):
            v = b[j]
            ub[v] = -1 if v in ub else j
        pairs = [(i, ub[v]) for v, i in ua.items() if i >= 0 and ub.get(v, -1) >= 0]
        if not pairs:
            # 没有唯一行（高度重复的结构，如格式化后的 JSON）：先试 Myers（改动少时很快），
            # 改动太多再退回 difflib；两者都太贵就整段当作替换
            found = _myers(a, b, alo, ahi, blo, bhi, MYERS_MAX_D)
            if found is not None:
                matches.extend(found)
            elif (ahi - alo) * (bhi - blo) <= DIFFLIB_MAX_CELLS:
                sm = difflib.SequenceMatcher(None, a[alo:ahi], b[blo:bhi], autojunk=False)
                for blk in sm.get_matching_blocks():
                    for t in range(blk.size):
                        matches.append((alo + blk.a + t, blo + blk.b + t))
            continue
        pairs.sort()
        pa, pb = alo, blo
        for i, j in _lis(pairs):
            matches.append((i, j))
            if i > pa and j > pb:
                stack.append((pa, i, pb, j))
            pa, pb = i + 1, j + 1
        if pa < ahi and pb < bhi:
            stack.append((pa, ahi, pb, bhi))
    matches.sort()
    return matches


class _Side:
    def __init__(self, path: Path, opts: dict, intern: dict):
        text = read_text(path)
        ws = opts.get("ignore_ws") or "trailing"
        case = bool(opts.get("ignore_case"))
        blank = bool(opts.get("ignore_blank"))
        regex = opts.get("ignore_regex") or ""
        rx = re.compile(regex) if regex else None
        self.numeric = (opts.get("numeric_tol") or 0) > 0 or (opts.get("rel_tol") or 0) > 0
        self.lines: list[str] = []
        self.lineno: list[int] = []
        self.keys: list[int] = []
        self.total_lines = 0
        for n, line in enumerate(text.splitlines(), start=1):
            self.total_lines = n
            if blank and not line.strip():
                continue
            if rx is not None and rx.search(line):
                continue
            k = line
            if ws == "trailing":
                k = k.rstrip()
            elif ws == "all":
                k = _WS_RE.sub(" ", k).strip()
            if case:
                k = k.lower()
            if self.numeric:
                k = NUM_RE.sub("\0", k)
            self.lines.append(line)
            self.lineno.append(n)
            self.keys.append(intern.setdefault(k, len(intern)))


def _numbers_close(la: str, lb: str, abs_tol: float, rel_tol: float):
    """返回 (是否在容差内, 最大绝对差)。调用前两行的「非数字骨架」已经相同。"""
    na, nb = NUM_RE.findall(la), NUM_RE.findall(lb)
    if len(na) != len(nb):
        return False, None
    worst = 0.0
    ok = True
    for x, y in zip(na, nb):
        try:
            fx, fy = float(x), float(y)
        except ValueError:
            return False, None
        d = abs(fx - fy)
        worst = max(worst, d)
        if d > abs_tol and not (rel_tol > 0 and d <= rel_tol * max(abs(fx), abs(fy))):
            ok = False
    return ok, worst


@register_comparator
class TextComparator(Comparator):
    name = "text"
    title = "文本按行对比"
    description = "任意文本文件按行对齐（patience diff，大文件也不会卡死），可忽略空白、数值精度噪声。"
    extensions = (".txt", ".log", ".xmps", ".csv", ".lp", ".md", ".py", ".ini", ".cfg", ".yaml", ".yml")
    options = [
        Param("ignore_ws", "空白处理", type="choice", default="trailing",
              choices=[("none", "严格比较"), ("trailing", "忽略行尾空白"), ("all", "忽略所有空白差异")]),
        Param("numeric_tol", "数值绝对容差", type="float", default=0.0,
              help="两行仅数字不同、且每个数字差值 ≤ 容差时视为相同（过滤求解精度噪声）"),
        Param("rel_tol", "数值相对容差", type="float", default=0.0, advanced=True),
        Param("ignore_case", "忽略大小写", type="bool", default=False, advanced=True),
        Param("ignore_blank", "忽略空行", type="bool", default=False, advanced=True),
        Param("ignore_regex", "忽略匹配该正则的行", default="", advanced=True,
              placeholder=r"^\s*#|time:", help="例如时间戳、注释行"),
    ]

    def compare(self, path_a: Path, path_b: Path, opts: dict, sink, progress) -> dict:
        intern: dict[str, int] = {}
        progress(5, f"读取 {path_a.name}")
        A = _Side(path_a, opts, intern)
        progress(20, f"读取 {path_b.name}")
        B = _Side(path_b, opts, intern)
        del intern
        progress(35, f"对齐 {len(A.keys):,} / {len(B.keys):,} 行")
        matches = match_sequences(A.keys, B.keys)
        progress(75, "输出差异")

        abs_tol = float(opts.get("numeric_tol") or 0)
        rel_tol = float(opts.get("rel_tol") or 0)
        within_tol = 0

        def gap(i0, i1, j0, j1):
            n = min(i1 - i0, j1 - j0)
            for t in range(n):
                sink.add("changed", "", A.lines[i0 + t], B.lines[j0 + t],
                         line_a=A.lineno[i0 + t], line_b=B.lineno[j0 + t])
            for i in range(i0 + n, i1):
                sink.add("removed", "", A.lines[i], MISSING, line_a=A.lineno[i])
            for j in range(j0 + n, j1):
                sink.add("added", "", MISSING, B.lines[j], line_b=B.lineno[j])

        ia = ib = 0
        for i, j in matches:
            if i > ia or j > ib:
                gap(ia, i, ib, j)
            if A.numeric and A.lines[i] != B.lines[j]:
                ok, worst = _numbers_close(A.lines[i], B.lines[j], abs_tol, rel_tol)
                if ok:
                    within_tol += 1 if worst else 0   # worst == 0：只是空白/大小写差异
                else:
                    sink.add("changed", "", A.lines[i], B.lines[j], delta=worst,
                             line_a=A.lineno[i], line_b=B.lineno[j], note="数值差异")
            ia, ib = i + 1, j + 1
        if ia < len(A.lines) or ib < len(B.lines):
            gap(ia, len(A.lines), ib, len(B.lines))

        return {
            "lines_a": A.total_lines, "lines_b": B.total_lines,
            "compared_a": len(A.lines), "compared_b": len(B.lines),
            "matched_lines": len(matches),
            "ignored_within_tolerance": within_tol,
        }
