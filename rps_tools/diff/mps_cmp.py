"""MPS 结构化对比：按「矩阵元素」而不是按行比较。

把 MPS 解析成  键 -> 值  的映射后逐键对比，所以行/列的输出顺序不同不会产生假差异：
  ROWS/<row>                 约束类型 (N/E/L/G)
  COLUMNS/<col>/<row>        系数
  INTEGER/<col>              整数变量标记 (MARKER INTORG ... INTEND)
  RHS/<row>   RANGES/<row>   右端项 / 区间
  BOUNDS/<col>/<type>        变量界 (UP/LO/FX/MI/PL/BV/...)
  QUADOBJ/<c1>/<c2> 等       二次项
RHS / RANGES / BOUNDS 的集合名不参与比较。只支持名字中不含空格的写法（free MPS 及常见的 fixed MPS）。
"""
from __future__ import annotations

import math
from pathlib import Path

from rps_tools.core.fileio import read_text
from rps_tools.core.resultdb import MISSING
from rps_tools.core.tooling import Param
from rps_tools.diff.base import Comparator, register_comparator

HEADERS = {"NAME", "OBJSENSE", "OBJSENS", "ROWS", "COLUMNS", "RHS", "RANGES", "BOUNDS", "SOS",
           "QUADOBJ", "QMATRIX", "QSECTION", "QCMATRIX", "INDICATORS", "ENDATA"}
NO_VALUE_BOUNDS = {"FR", "MI", "PL", "BV"}


def _num(s: str):
    try:
        return float(s)
    except ValueError:
        return s


def parse_mps(path: Path) -> tuple[dict, dict]:
    entries: dict[tuple, object] = {}
    section = None
    in_int = False
    qsection = None
    for raw in read_text(path).splitlines():
        if not raw.strip() or raw.lstrip().startswith("*"):
            continue
        tok = raw.split()
        head = tok[0].upper()
        if not raw[0].isspace() and head in HEADERS:
            section = head
            if head == "NAME":
                entries[("NAME",)] = " ".join(tok[1:])
            elif head in ("OBJSENSE", "OBJSENS") and len(tok) > 1:
                entries[("OBJSENSE",)] = tok[1].upper()
            elif head in ("QSECTION", "QCMATRIX") and len(tok) > 1:
                qsection = tok[1]
            if head == "ENDATA":
                break
            continue
        if section in ("OBJSENSE", "OBJSENS"):
            entries[("OBJSENSE",)] = head
        elif section == "ROWS":
            entries[("ROWS", tok[1])] = tok[0].upper()
        elif section == "COLUMNS":
            if len(tok) >= 3 and tok[1].strip("'\"").upper() == "MARKER":
                marker = tok[2].strip("'\"").upper()
                in_int = marker == "INTORG" if marker in ("INTORG", "INTEND") else in_int
                continue
            col = tok[0]
            if in_int:
                entries[("INTEGER", col)] = True
            for k in range(1, len(tok) - 1, 2):
                entries[("COLUMNS", col, tok[k])] = _num(tok[k + 1])
        elif section in ("RHS", "RANGES"):
            pairs = tok[1:] if len(tok) % 2 == 1 else tok
            for k in range(0, len(pairs) - 1, 2):
                entries[(section, pairs[k])] = _num(pairs[k + 1])
        elif section == "BOUNDS":
            btype, rest = tok[0].upper(), tok[1:]
            if btype in NO_VALUE_BOUNDS:
                # 可能的写法：[col] / [set col] / [col val] / [set col val]
                if len(rest) == 1:
                    col, val = rest[0], True
                elif len(rest) == 2:
                    col, val = (rest[0], _num(rest[1])) if _is_number(rest[1]) else (rest[1], True)
                else:
                    col, val = rest[1], _num(rest[2])
            else:
                col, val = (rest[1], rest[2]) if len(rest) >= 3 else (rest[0], rest[1])
                val = _num(val)
            entries[("BOUNDS", col, btype)] = val
        elif section in ("QUADOBJ", "QMATRIX", "QSECTION", "QCMATRIX") and len(tok) >= 3:
            key = (section if not qsection else f"{section}:{qsection}", tok[0], tok[1])
            entries[key] = _num(tok[2])
        elif section is not None:
            entries[(section, raw.strip())] = True
    stats = {
        "rows": sum(1 for k in entries if k[0] == "ROWS"),
        "columns": len({k[1] for k in entries if k[0] == "COLUMNS"}),
        "nonzeros": sum(1 for k in entries if k[0] == "COLUMNS"),
        "integers": sum(1 for k in entries if k[0] == "INTEGER"),
        "bounds": sum(1 for k in entries if k[0] == "BOUNDS"),
    }
    return entries, stats


def _is_number(s: str) -> bool:
    try:
        float(s)
        return True
    except ValueError:
        return False


@register_comparator
class MpsComparator(Comparator):
    name = "mps"
    title = "MPS 结构化对比"
    description = "按 行类型 / 系数 / RHS / 界 逐元素对比，与行列输出顺序无关。"
    extensions = (".mps", ".freemps")
    options = [
        Param("abs_tol", "数值绝对容差", type="float", default=0.0),
        Param("rel_tol", "数值相对容差", type="float", default=0.0),
    ]

    def compare(self, path_a: Path, path_b: Path, opts: dict, sink, progress) -> dict:
        abs_tol = float(opts.get("abs_tol") or 0)
        rel_tol = float(opts.get("rel_tol") or 0)
        progress(5, f"解析 {path_a.name}")
        ea, sa = parse_mps(path_a)
        progress(40, f"解析 {path_b.name}")
        eb, sb = parse_mps(path_b)
        progress(75, "逐元素对比")

        def same(x, y) -> bool:
            if isinstance(x, float) and isinstance(y, float):
                if x == y or (math.isnan(x) and math.isnan(y)):
                    return True
                d = abs(x - y)
                return d <= abs_tol or (rel_tol > 0 and d <= rel_tol * max(abs(x), abs(y)))
            return x == y

        def add(kind, key, old, new):
            field = key[2] if key[0] == "BOUNDS" and len(key) > 2 else None
            sink.add(kind, "/".join(key), old, new, section=key[0], field=field)

        for key, va in ea.items():
            vb = eb.get(key, MISSING)
            if vb is MISSING:
                add("removed", key, va, MISSING)
            elif not same(va, vb):
                add("changed", key, va, vb)
        for key, vb in eb.items():
            if key not in ea:
                add("added", key, MISSING, vb)
        return {"a": sa, "b": sb}
