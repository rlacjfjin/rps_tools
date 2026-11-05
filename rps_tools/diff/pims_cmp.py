"""Aspen PIMS Full Solution 报告（FullSolution*.htm）对比。

两层输出：
  1. 逐单元格差异 -> DiffSink（结果页分页 / 筛选 / CSV）。键 = 报告/子段/表/行代码/列：
       Material Purchases/Feedstock Purchases/MAT-A/Units/DAY
       Process Submodel Summary/U1/Process Yields/MAT-B/Wt%
     行描述的变化单独记在「(描述)」段，不影响数值行的匹配。
  2. 分析摘要 summary.md（见 pims_summary.py）：目标函数、经济汇总、输入参数核对、
     采购/销售变化、装置启停、约束紧松变化、调合配方、报告定义差异和解读提示。
"""
from __future__ import annotations

import math
from pathlib import Path

from rps_tools.core.resultdb import MISSING
from rps_tools.core.tooling import Param
from rps_tools.diff.base import Comparator, register_comparator
from rps_tools.diff.pims_report import BLANK, SUBTOTAL, Val, parse_pims

STANDARD_REPORTS = [
    "Material Purchases", "Material Sales", "Utility Purchases", "Utility Sales", "Economic Summary Analysis",
    "Bounded Variable Status", "Capacity Utilization Summary", "Process Limit Summary", "Specification Blend",
    "Stream Property Report", "Stream Disposition Map", "Utility Disposition Map", "Process Submodel Summary",
]
DESC_SECTION = "(描述)"


class ValueCompare:
    """单元格相等判断：容差 + 显示精度 + 空白视为 0。"""

    def __init__(self, abs_tol=0.0, rel_tol=0.0, ignore_rounding=True, blank_as_zero=True):
        self.abs_tol, self.rel_tol = float(abs_tol or 0), float(rel_tol or 0)
        self.ignore_rounding, self.blank_as_zero = bool(ignore_rounding), bool(blank_as_zero)

    def norm(self, v: Val | None) -> Val:
        v = v or BLANK
        if v.value is None and self.blank_as_zero:
            return Val(0.0, 0)
        return v

    def same(self, a: Val | None, b: Val | None) -> bool:
        a, b = self.norm(a), self.norm(b)
        x, y = a.value, b.value
        if not (isinstance(x, float) and isinstance(y, float)):
            return x == y
        if x == y or (math.isnan(x) and math.isnan(y)):
            return True
        d = abs(x - y)
        if d <= self.abs_tol or (self.rel_tol > 0 and d <= self.rel_tol * max(abs(x), abs(y))):
            return True
        if self.ignore_rounding and a.dec != b.dec:
            # 两边显示精度不同（5.12 vs 5.116）：在较粗的精度下相等就算相同
            return d <= 0.5 * 10 ** -min(a.dec, b.dec) * (1 + 1e-9)
        return False


def _raw(v: Val | None):
    if v is None:
        return MISSING
    return v.value


@register_comparator
class PimsComparator(Comparator):
    name = "pims"
    title = "Aspen PIMS 解报告对比"
    description = ("对比两份 PIMS FullSolution*.htm：逐单元格差异（行按代码匹配，与页面分页 / 行顺序 / 描述无关）"
                   "+ 分析摘要（目标函数、经济汇总、采购销售、装置启停、约束紧松、调合配方、模型定义差异）。")
    filename_patterns = ("*fullsolution*.htm", "*fullsolution*.html")
    options = [
        Param("abs_tol", "数值绝对容差", type="float", default=0.0),
        Param("rel_tol", "数值相对容差", type="float", default=0.0),
        Param("ignore_rounding", "忽略显示精度差异", type="bool", default=True,
              help="同一个数两边显示的小数位数不同（5.12 vs 5.116）时，按较粗的精度比较"),
        Param("blank_as_zero", "空白视为 0", type="bool", default=True,
              help="报告里空白单元格和 0 视为相同"),
        Param("skip_reports", "明细里跳过的报告", type="multi", default=[],
              choices=[(r, r) for r in STANDARD_REPORTS], advanced=True,
              help="只影响逐单元格明细，分析摘要不受影响。Stream Disposition Map 等大矩阵通常差异最多"),
        Param("summary_top", "摘要每张表最多列几行", type="int", default=25, advanced=True),
        Param("summary_rel_tol", "摘要忽略的相对变化", type="float", default=1e-4, advanced=True,
              help="采购 / 销售 / 负荷等摘要表里，相对变化小于此值的行不列出"),
    ]

    def compare(self, path_a: Path, path_b: Path, opts: dict, sink, progress) -> dict:
        from rps_tools.diff.pims_summary import build_summary, mark_changes

        cmpv = ValueCompare(opts.get("abs_tol"), opts.get("rel_tol"),
                            opts.get("ignore_rounding", True), opts.get("blank_as_zero", True))
        skip = set(opts.get("skip_reports") or [])
        progress(2, f"解析 {path_a.name}")
        pa = parse_pims(path_a)
        progress(40, f"解析 {path_b.name}")
        pb = parse_pims(path_b)
        progress(78, "逐单元格对比")

        def emit(kind, key, row, col, va, vb):
            rep, sub, table = key
            path = "/".join(p for p in (rep, sub, table, row, col) if p)
            desc = pa.labels.get(key, {}).get(row) or pb.labels.get(key, {}).get(row)
            sink.add(kind, path, va, vb, section=rep or "(无标题)", field=col, note=desc)

        for key in list(pa.tables) + [k for k in pb.tables if k not in pa.tables]:
            if key[0] in skip:
                continue
            ra, rb = pa.tables.get(key, {}), pb.tables.get(key, {})
            for row in list(ra) + [r for r in rb if r not in ra]:
                ca, cb = ra.get(row), rb.get(row)
                cols = list(ca or {}) + [c for c in (cb or {}) if c not in (ca or {})]
                for col in cols:
                    va = ca.get(col) if ca is not None else None
                    vb = cb.get(col) if cb is not None else None
                    if va is None and vb is not None:
                        if ca is not None and cmpv.same(BLANK, vb):
                            continue     # 行在、列缺（分页不同）且新值为空/0：不算差异
                        if ca is None and cmpv.norm(vb).value in (0.0, None) and row != SUBTOTAL:
                            continue     # 整行只在一侧且值为 0：PIMS 不输出零行，不是差异
                        emit("added", key, row, col, MISSING, _raw(vb))
                    elif vb is None:
                        if cb is not None and cmpv.same(va, BLANK):
                            continue
                        if cb is None and cmpv.norm(va).value in (0.0, None) and row != SUBTOTAL:
                            continue
                        emit("removed", key, row, col, _raw(va), MISSING)
                    elif not cmpv.same(va, vb):
                        emit("changed", key, row, col, _raw(va), _raw(vb))

        for code in sorted(set(pa.desc) & set(pb.desc)):
            if pa.desc[code] != pb.desc[code]:
                sink.add("changed", f"描述/{code}", pa.describe(code), pb.describe(code),
                         section=DESC_SECTION, field="描述")
        for key in sorted(set(pa.sub_titles) & set(pb.sub_titles)):
            if pa.sub_titles[key] != pb.sub_titles[key]:
                sink.add("changed", f"子段标题/{key[0]}/{key[1]}", pa.sub_titles[key], pb.sub_titles[key],
                         section=DESC_SECTION, field="子段标题")

        for (key, code), ma, mb in mark_changes(pa, pb):
            sink.add("changed", "行标记/" + "/".join(p for p in (*key, code) if p), ma or "（无）", mb or "（无）",
                     section=DESC_SECTION, field="行标记")

        progress(92, "生成分析摘要")
        summary_md = build_summary(pa, pb, cmpv, int(opts.get("summary_top") or 25),
                                   float(opts.get("summary_rel_tol") or 0))
        return {
            "a": {"tables": len(pa.tables), "cells": pa.n_cells, **_meta_json(pa.meta)},
            "b": {"tables": len(pb.tables), "cells": pb.n_cells, **_meta_json(pb.meta)},
            "summary_md": summary_md,
        }


def _meta_json(meta: dict) -> dict:
    return {k: meta.get(k) for k in ("model", "case", "status", "timestamp", "objective")}
