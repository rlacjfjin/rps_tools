"""PIMS 解报告对比的分析摘要（summary.md）。

逐单元格差异动辄上万条，这里把它们按「问题」组织成几张表：
  1. 概览：模型 / Case / 时间 / 求解状态 / 目标函数
  2. 经济汇总（Economic Summary Analysis）
  3. 输入参数核对：两边都有的行里，上下限 / 价格是否一致（判断"改了输入"还是"解变了"）
  4. 采购 / 销售 / 公用工程变化（按金额变化排序，标出新增 / 消失）
  5. 装置启停与进料变化（Process Submodel Summary）
  6. 约束紧松变化（有边际值 = 卡住；新卡住 / 不再卡住 / 边际值变化）
  7. 调合配方变化（Specification Blend）
  8. 装置负荷变化（Capacity Utilization Summary，按 %Maximum 变化排序）
  9. 报告定义差异：行描述、子段标题、只在一侧出现的列（物性）/ 产品 / 报告
  解读提示（启发式，只给方向，不下结论）放在最前面
"""
from __future__ import annotations

from rps_tools.diff.pims_report import SUBTOTAL, PimsSolution, Val

ECON = "Economic Summary Analysis"
FLOW_REPORTS = [("Material Purchases", "原料采购"), ("Material Sales", "产品销售"),
                ("Utility Purchases", "公用工程采购"), ("Utility Sales", "公用工程外送")]
SUBMODELS = "Process Submodel Summary"
CAPACITY = "Capacity Utilization Summary"
SPEC_BLEND = "Specification Blend"
SHORT = {"Capacity Utilization Summary": "能力", "Process Limit Summary": "工艺限制",
         "Bounded Variable Status": "有界变量", "Specification Blend": "调合规格",
         "Material Purchases": "采购", "Material Sales": "销售",
         "Utility Purchases": "公用工程采购", "Utility Sales": "公用工程外送"}


# ------------------------------------------------------------------ 小工具
def num(v: Val | None) -> float | None:
    return v.value if v is not None and isinstance(v.value, float) else None


def esc(s) -> str:
    s = "" if s is None else str(s)
    for a, b in (("<", "&lt;"), ("|", "\\|"), ("*", "\\*"), ("_", "\\_"), ("`", "\\`")):
        s = s.replace(a, b)
    return s


def show(v: Val | None) -> str:
    if v is None:
        return "—"
    return esc(v.text())


def fnum(x: float | None, dec: int = 0, signed: bool = False) -> str:
    if x is None:
        return ""
    s = f"{x:+,.{dec}f}" if signed else f"{x:,.{dec}f}"
    return "0" if s in ("+0", "-0") else s


def delta(a: Val | None, b: Val | None) -> str:
    x, y = num(a), num(b)
    if x is None and y is None:
        return ""
    dec = max(a.dec if a else 0, b.dec if b else 0)
    return fnum((y or 0.0) - (x or 0.0), dec, signed=True)


def pct(x: float | None, y: float | None) -> str:
    if x is None or y is None or x == 0:
        return ""
    return f"{(y - x) / abs(x) * 100:+.2f}%"


def table(header: list[str], rows: list[list[str]]) -> list[str]:
    out = ["| " + " | ".join(header) + " |", "|" + "|".join("---" for _ in header) + "|"]
    out += ["| " + " | ".join(r) + " |" for r in rows]
    return out + [""]


class Ctx:
    def __init__(self, pa: PimsSolution, pb: PimsSolution, cmpv, top: int, rtol: float):
        self.pa, self.pb, self.cmpv, self.top, self.rtol = pa, pb, cmpv, max(1, top), rtol
        self.facts: dict = {}

    def significant(self, a: Val | None, b: Val | None) -> bool:
        """有差异，且相对变化不小于 rtol（摘要里过滤掉价格尾数之类的噪声）。"""
        if self.cmpv.same(a, b):
            return False
        x, y = num(self.cmpv.norm(a)), num(self.cmpv.norm(b))
        if x is None or y is None:
            return True
        scale = max(abs(x), abs(y))
        return scale == 0 or abs(y - x) / scale >= self.rtol

    def desc(self, code: str) -> str:
        return esc(self.pa.describe(code) or self.pb.describe(code))

    def more(self, n: int) -> list[str]:
        return [f"（另有 {n} 行未列出，可在下面的明细表里筛选）", ""] if n > 0 else []


# ------------------------------------------------------------------ 各节
def sec_overview(c: Ctx) -> list[str]:
    a, b = c.pa.meta, c.pb.meta
    oa, ob = a.get("objective"), b.get("objective")
    rows = [
        ["文件", esc(c.pa.path.name), esc(c.pb.path.name), ""],
        ["模型", esc(a.get("model")), esc(b.get("model")), ""],
        ["Case", esc(a.get("case") or "（无）"), esc(b.get("case") or "（无）"), ""],
        ["运行时间", esc(a.get("timestamp")), esc(b.get("timestamp")), ""],
        ["PIMS 版本", esc(a.get("version")), esc(b.get("version")), ""],
        ["求解状态", esc(a.get("status")), esc(b.get("status")), ""],
        ["**目标函数**", f"**{esc(a.get('objective_text'))}**", f"**{esc(b.get('objective_text'))}**",
         f"**{fnum(ob - oa, 2, True)}（{pct(oa, ob)}）**" if oa is not None and ob is not None else ""],
    ]
    return ["## 1. 概览", ""] + table(["", "A（基准）", "B（对比）", "Δ"], rows)


def sec_econ(c: Ctx) -> list[str]:
    ra, rb = c.pa.rows(ECON), c.pb.rows(ECON)
    rows = []
    for key in list(ra) + [k for k in rb if k not in ra]:
        ca, cb = ra.get(key, {}), rb.get(key, {})
        for col in list(ca) + [k for k in cb if k not in ca]:
            va, vb = ca.get(col), cb.get(col)
            if num(va) is None and num(vb) is None:
                continue
            rows.append([esc(key[2]), esc(col), show(va), show(vb), delta(va, vb), pct(num(va), num(vb))])
    if not rows:
        return []
    return ["## 2. 经济汇总", ""] + table(["项目", "列", "A", "B", "Δ", "Δ%"], rows)


PRICE_REPORTS = {r for r, _ in FLOW_REPORTS}


def _is_input_col(report: str, col: str) -> bool:
    """上下限在任何报告里都是输入；单价只在采购 / 销售报告里是输入
    （子模型 Utilities Used/Produced 的 $/Units 对自产公用工程是算出来的内部价）。"""
    if col in ("$/Unit", "$/Units"):
        return report in PRICE_REPORTS
    return col in ("Minimum", "Maximum") or (
        (col.startswith("Min") or col.startswith("Max")) and not col.startswith("% "))


def sec_inputs(c: Ctx) -> list[str]:
    checked, diffs = 0, []
    for key, ra in c.pa.tables.items():
        rb = c.pb.tables.get(key)
        if not rb:
            continue
        for row, ca in ra.items():
            cb = rb.get(row)
            if cb is None:
                continue
            for col in ca:
                if not _is_input_col(key[0], col) or col not in cb:
                    continue
                checked += 1
                if not c.cmpv.same(ca[col], cb[col]):
                    diffs.append((key, row, col, ca[col], cb[col]))
    big = [d for d in diffs if c.significant(d[3], d[4])]
    tiny = [d for d in diffs if not c.significant(d[3], d[4])]
    c.facts["inputs_checked"], c.facts["inputs_changed"], c.facts["inputs_tiny"] = checked, len(big), len(tiny)
    out = ["## 3. 输入参数核对（上下限 / 价格）", "",
           f"两边都出现的行里，共核对 **{checked:,}** 个上下限 / 价格单元格（只在一侧出现的行无法核对）：", ""]
    head = ["报告", "子段 · 表", "代码", "描述", "列", "A", "B"]
    fmt = lambda items: [[esc(SHORT.get(k[0], k[0])), esc(" · ".join(p for p in (k[1], k[2]) if p)), esc(row),  # noqa: E731
                          c.desc(row), esc(col), show(va), show(vb)] for k, row, col, va, vb in items[:c.top]]
    if big:
        out += [f"**{len(big)} 处明显不同**：", ""] + table(head, fmt(big)) + c.more(len(big) - c.top)
    else:
        out += ["- **没有明显不同的上下限 / 价格**。", ""]
    if tiny:
        out += [f"另有 {len(tiny)} 处只在小数尾数上不同（相对差 < {c.rtol:g}）。多半是显示取整，"
                "也可能是价格由公式 / 汇率算出、两次结果略有不同：", ""]
        out += table(head, fmt(tiny)) + c.more(len(tiny) - c.top)
    return out


def _cols(cols: list[str]) -> tuple[str | None, str | None]:
    qty = next((x for x in cols if x.startswith("Units/") or x.endswith("/DAY") and not x.startswith("$")), None)
    money = next((x for x in cols if x.startswith("$/") and not x.startswith("$/Unit")), None)
    return qty or (cols[0] if cols else None), money


def sec_flows(c: Ctx) -> list[str]:
    out = []
    for rep, title in FLOW_REPORTS:
        ra, rb = c.pa.rows(rep), c.pb.rows(rep)
        if not ra and not rb:
            continue
        lines, totals = [], []
        for key in list(ra) + [k for k in rb if k not in ra]:
            sub, tb, code = key
            ca, cb = ra.get(key), rb.get(key)
            cols = c.pa.columns.get((rep, sub, tb)) or c.pb.columns.get((rep, sub, tb)) or []
            qcol, mcol = _cols(cols)
            qa, qb = (ca or {}).get(qcol), (cb or {}).get(qcol)
            ma, mb = (ca or {}).get(mcol), (cb or {}).get(mcol)
            if not (c.significant(qa, qb) or (mcol and c.significant(ma, mb))):
                continue
            na, nb = num(c.cmpv.norm(qa)) or 0.0, num(c.cmpv.norm(qb)) or 0.0
            if qcol and na == 0 and nb == 0 and code != SUBTOTAL and not code.lower().startswith("total"):
                continue   # 两边量都是 0，只有金额尾数（取整）不同
            note = "新增" if na == 0 and nb != 0 else "消失" if nb == 0 and na != 0 else ""
            line = [esc(code), c.desc(code), show(qa), show(qb), delta(qa, qb), show(ma), show(mb), delta(ma, mb), note]
            mag = abs((num(mb) or 0) - (num(ma) or 0)) or abs(nb - na)
            (totals if code == SUBTOTAL or code.lower().startswith("total") else lines).append((mag, line))
        if not lines and not totals:
            continue
        lines.sort(key=lambda t: -t[0])
        out += [f"### {title}", ""]
        out += table(["代码", "描述", "A 量", "B 量", "Δ量", "A 金额", "B 金额", "Δ金额", "备注"],
                     [l for _, l in lines[:c.top]] + [l for _, l in totals if l[0] != SUBTOTAL])
        out += c.more(len(lines) - c.top)
    return (["## 4. 采购 / 销售 / 公用工程变化（按 |Δ金额| 排序）", ""] + out) if out else []


def _feed(sol: PimsSolution, sub: str) -> Val | None:
    rows = sol.tables.get((SUBMODELS, sub, "Process Feeds"), {})
    total = rows.get("Total") or {}
    return total.get("Units/DAY") or next(iter(total.values()), None)


def sec_submodels(c: Ctx) -> list[str]:
    sa, sb = c.pa.subs(SUBMODELS), c.pb.subs(SUBMODELS)
    only_a, only_b = sorted(sa - sb), sorted(sb - sa)
    c.facts["subs_only_a"], c.facts["subs_only_b"] = only_a, only_b
    out = ["## 5. 装置启停与进料变化", ""]
    if not sa and not sb:
        return []
    title = lambda sol, s: esc(sol.sub_titles.get((SUBMODELS, s), s))  # noqa: E731
    if only_a or only_b:
        rows = [[esc(s), title(c.pa, s), show(_feed(c.pa, s)), "—", "只在 A 运行（B 未输出）"] for s in only_a]
        rows += [[esc(s), title(c.pb, s), "—", show(_feed(c.pb, s)), "只在 B 运行（A 未输出）"] for s in only_b]
        out += ["PIMS 只输出有活动的子模型，所以「只在一侧出现」基本等于启停变化：", ""]
        out += table(["子模型", "标题", "A 进料", "B 进料", "说明"], rows)
    else:
        out += ["两边运行的子模型集合相同。", ""]
    changes = []
    for s in sa & sb:
        fa, fb = _feed(c.pa, s), _feed(c.pb, s)
        if c.significant(fa, fb):
            x, y = num(fa) or 0.0, num(fb) or 0.0
            changes.append((abs(y - x), [esc(s), title(c.pb, s), show(fa), show(fb), delta(fa, fb), pct(x, y)]))
    if changes:
        changes.sort(key=lambda t: -t[0])
        out += ["进料总量变化最大的子模型：", ""]
        out += table(["子模型", "标题", "A 进料", "B 进料", "Δ", "Δ%"], [r for _, r in changes[:c.top]])
        out += c.more(len(changes) - c.top)
    return out


def _bound_cols(cols: list[str]):
    mv = next((x for x in cols if x.startswith("Marg Val")), None)
    if not mv or not ("Minimum" in cols or "Maximum" in cols):
        return None
    skip = {"Minimum", "Maximum", mv}
    act = next((x for x in cols if x not in skip and not x.startswith("%") and not x.startswith("$")), None)
    return act, mv


def _state(row: dict | None, act: str, mv: str) -> str:
    if not row:
        return ""
    m = num(row.get(mv))
    if not m:
        return ""
    a, lo, hi = row.get(act), num(row.get("Minimum")), num(row.get("Maximum"))
    x = num(a)
    tol = 0.5 * 10 ** -(a.dec if a else 0) + 1e-9
    if x is not None and hi is not None and abs(x - hi) <= tol + 1e-6 * abs(hi):
        return "卡上限"
    if x is not None and lo is not None and abs(x - lo) <= tol + 1e-6 * abs(lo):
        return "卡下限"
    return "有边际值"


def sec_binding(c: Ctx) -> list[str]:
    new, freed, flipped, mv_moves = [], [], [], []
    keys = list(c.pa.tables) + [k for k in c.pb.tables if k not in c.pa.tables]
    for key in keys:
        cols = c.pa.columns.get(key) or c.pb.columns.get(key) or []
        bc = _bound_cols(cols)
        if not bc:
            continue
        act, mv = bc
        ra, rb = c.pa.tables.get(key, {}), c.pb.tables.get(key, {})
        for row in list(ra) + [r for r in rb if r not in ra]:
            if row == SUBTOTAL or row.lower().startswith("total"):
                continue
            ca, cb = ra.get(row), rb.get(row)
            sa, sb = _state(ca, act, mv), _state(cb, act, mv)
            name = " · ".join(p for p in (SHORT.get(key[0], key[0]), key[1], row) if p)
            g = lambda r, k: (r or {}).get(k)  # noqa: E731
            lim = lambda k: show(g(ca, k) or g(cb, k)) if c.cmpv.same(g(ca, k), g(cb, k)) else f"{show(g(ca, k))}→{show(g(cb, k))}"  # noqa: E731
            line = [esc(name), c.desc(row), show(g(ca, act)), show(g(cb, act)), lim("Minimum"), lim("Maximum"),
                    show(g(ca, mv)), show(g(cb, mv)), f"{sa or '—'} → {sb or '—'}"]
            dmv = abs((num(g(cb, mv)) or 0) - (num(g(ca, mv)) or 0))
            if sa == sb:
                if sa and c.significant(g(ca, mv), g(cb, mv)):
                    mv_moves.append((dmv, line))
            elif not sa:
                new.append((dmv, line))
            elif not sb:
                freed.append((dmv, line))
            else:
                flipped.append((dmv, line))
    c.facts["binding"] = (len(new), len(freed), len(flipped))
    if not (new or freed or flipped or mv_moves):
        return []
    head = ["约束", "描述", "A 活动", "B 活动", "下限", "上限", "A 边际值", "B 边际值", "状态 A → B"]
    out = ["## 6. 约束紧松变化", "",
           "以「边际值非空」判断约束是否起作用；卡住但边际值为 0 的退化约束不算。", ""]
    for title, items in (("B 新卡住", new), ("B 不再卡住", freed), ("换边 / 状态变化", flipped),
                         ("两边都卡住、边际值变化最大", mv_moves)):
        if items:
            items.sort(key=lambda t: -t[0])
            out += [f"### {title}（{len(items)}）", ""] + table(head, [r for _, r in items[:c.top]])
            out += c.more(len(items) - c.top)
    return out


def sec_blends(c: Ctx) -> list[str]:
    pa_subs, pb_subs = c.pa.subs(SPEC_BLEND), c.pb.subs(SPEC_BLEND)
    out = []
    for p in sorted(pa_subs | pb_subs):
        tb = next((k[2] for k in list(c.pa.tables) + list(c.pb.tables)
                   if k[0] == SPEC_BLEND and k[1] == p and k[2].startswith("Component to Blend")), None)
        if tb is None:
            continue
        key = (SPEC_BLEND, p, tb)
        ra, rb = c.pa.tables.get(key, {}), c.pb.tables.get(key, {})
        cols = c.pa.columns.get(key) or c.pb.columns.get(key) or []
        q = cols[0] if cols else None
        ta, tbv = (ra.get("Total") or {}).get(q), (rb.get("Total") or {}).get(q)
        comps = []
        for row in list(ra) + [r for r in rb if r not in ra]:
            if row == "Total":
                continue
            va, vb = (ra.get(row) or {}).get(q), (rb.get(row) or {}).get(q)
            if c.significant(va, vb):
                x, y = num(c.cmpv.norm(va)) or 0.0, num(c.cmpv.norm(vb)) or 0.0
                note = "新增组分" if x == 0 and y != 0 else "不再使用" if y == 0 and x != 0 else ""
                comps.append((abs(y - x), [esc(row), c.desc(row), show(va), show(vb), delta(va, vb), note]))
        if not comps and not c.significant(ta, tbv):
            continue
        where = "只在 A 有" if p not in pb_subs else "只在 B 有" if p not in pa_subs else ""
        out += [f"### {esc(p)} {c.desc(p)}：{show(ta)} → {show(tbv)}（{delta(ta, tbv)}） {where}", ""]
        comps.sort(key=lambda t: -t[0])
        if comps:
            out += table(["组分", "描述", "A", "B", "Δ", "备注"], [r for _, r in comps[:c.top]])
            out += c.more(len(comps) - c.top)
    return (["## 7. 调合配方变化", "", "规格是否卡住见第 6 节「调合规格」行。", ""] + out) if out else []


def sec_capacity(c: Ctx) -> list[str]:
    ra, rb = c.pa.rows(CAPACITY), c.pb.rows(CAPACITY)
    items = []
    for key in list(ra) + [k for k in rb if k not in ra]:
        ca, cb = ra.get(key) or {}, rb.get(key) or {}
        pa_, pb_ = num(ca.get("% Maximum")), num(cb.get("% Maximum"))
        if num(ca.get("Maximum")) is None and num(cb.get("Maximum")) is None:
            continue
        if not c.significant(ca.get("Activity"), cb.get("Activity")):
            continue
        d = (pb_ or 0.0) - (pa_ or 0.0)
        items.append((abs(d), [esc(key[2]), c.desc(key[2]), show(ca.get("Activity")), show(cb.get("Activity")),
                               show(ca.get("Maximum") or cb.get("Maximum")), show(ca.get("% Maximum")),
                               show(cb.get("% Maximum")), fnum(d, 1, True)]))
    if not items:
        return []
    items.sort(key=lambda t: -t[0])
    return (["## 8. 装置负荷变化（有上限的能力，按 |Δ%Maximum| 排序）", ""]
            + table(["能力", "描述", "A 活动", "B 活动", "上限", "A %", "B %", "Δ%"], [r for _, r in items[:c.top]])
            + c.more(len(items) - c.top))


def mark_changes(pa: PimsSolution, pb: PimsSolution) -> list[tuple[tuple, str, str]]:
    """行代码后的红色标记在两边不同（且这一行两边都存在）。"""
    out = []
    for key in sorted(set(pa.row_marks) | set(pb.row_marks)):
        tk, code = key
        if code not in pa.tables.get(tk, {}) or code not in pb.tables.get(tk, {}):
            continue
        ma, mb = pa.row_marks.get(key, ""), pb.row_marks.get(key, "")
        if ma != mb:
            out.append((key, ma, mb))
    return out


def sec_definitions(c: Ctx) -> list[str]:
    pa, pb = c.pa, c.pb
    out = []
    desc = [(k, pa.describe(k), pb.describe(k)) for k in sorted(set(pa.desc) & set(pb.desc)) if pa.desc[k] != pb.desc[k]]
    titles = [(k, pa.sub_titles[k], pb.sub_titles[k]) for k in sorted(set(pa.sub_titles) & set(pb.sub_titles))
              if pa.sub_titles[k] != pb.sub_titles[k]]
    c.facts["desc_changes"], c.facts["title_changes"] = len(desc), len(titles)
    marks = mark_changes(pa, pb)
    c.facts["mark_changes"] = marks
    if marks:
        out += [f"### 行标记变化（{len(marks)}）", "",
                "代码后面的红色标记。Capacity Utilization Summary 里 * = Has Active Capacity Penalty（能力罚项起作用）。", ""]
        out += table(["报告", "表", "代码", "描述", "A 标记", "B 标记"],
                     [[esc(k[0]), esc(" · ".join(p for p in (k[1], k[2]) if p)), esc(code), c.desc(code),
                       esc(ma or "—"), esc(mb or "—")] for (k, code), ma, mb in marks[:c.top]])
        out += c.more(len(marks) - c.top)
    if desc:
        out += [f"### 行描述变化（{len(desc)} 个代码）", ""]
        out += table(["代码", "A 描述", "B 描述"], [[esc(k), esc(a), esc(b)] for k, a, b in desc[:c.top]])
        out += c.more(len(desc) - c.top)
    if titles:
        out += [f"### 子段标题变化（{len(titles)}）", ""]
        out += table(["报告", "子段", "A 标题", "B 标题"],
                     [[esc(k[0]), esc(k[1]), esc(a), esc(b)] for k, a, b in titles[:c.top]])
    # 只在一侧出现的列（按 报告/表名 汇总，宽矩阵分页的不同列也算在一起）
    def colset(sol):
        cs: dict[tuple[str, str], set[str]] = {}
        for (rep, _sub, tb), cols in sol.columns.items():
            cs.setdefault((rep, tb), set()).update(cols)
        return cs
    ca, cb = colset(pa), colset(pb)
    col_rows, extra_cols = [], 0
    for key in sorted(set(ca) | set(cb)):
        if key not in ca or key not in cb:
            continue
        for side, cols, sol in (("A", ca[key] - cb[key], pa), ("B", cb[key] - ca[key], pb)):
            for col in sorted(cols):
                nz = sum(1 for (rep, _s, tb), rows in sol.tables.items() if (rep, tb) == key
                         for r in rows.values() if num(r.get(col)))
                extra_cols += 1
                col_rows.append([esc(key[0]), esc(key[1]), esc(col), f"只在 {side}", f"{nz:,}"])
    c.facts["extra_cols"] = extra_cols
    if col_rows:
        out += ["### 只在一侧出现的列（物性 / 装置列等）", ""]
        out += table(["报告", "表", "列", "出现在", "非零行数"], col_rows[:c.top * 2])
    rep_a, rep_b = set(pa.reports), set(pb.reports)
    if rep_a != rep_b:
        out += [f"- 只在 A 的报告段：{esc(', '.join(sorted(rep_a - rep_b))) or '无'}；"
                f"只在 B 的：{esc(', '.join(sorted(rep_b - rep_a))) or '无'}", ""]
    if not out:
        return []
    return ["## 9. 报告定义差异", "",
            "这些不是解的差异，而是两次运行加载的模型定义（描述、物性、报告配置）不同。", ""] + out


def sec_hints(c: Ctx) -> list[str]:
    a, b, f = c.pa.meta, c.pb.meta, c.facts
    oa, ob = a.get("objective"), b.get("objective")
    hints = []
    if a.get("status") != b.get("status"):
        hints.append(f"求解状态不同（{esc(a.get('status'))} vs {esc(b.get('status'))}），先确认两边都是最优解再比较数值。")
    if (a.get("case") or "") != (b.get("case") or ""):
        hints.append(f"两次运行的 Case 不同（A：{esc(a.get('case') or '无')}，B：{esc(b.get('case') or '无')}）。"
                     "Case 会改模型表，先看 CASE 表实际改了什么。")
    if oa is not None and ob is not None and oa != ob:
        if f.get("inputs_changed") == 0:
            tiny = f"，另有 {f['inputs_tiny']} 处只差小数尾数" if f.get("inputs_tiny") else ""
            hints.append(
                f"报告里能看到的上下限 / 价格两边没有明显不同（核对 {f.get('inputs_checked', 0):,} 处{tiny}），但目标函数差 "
                f"{fnum(ob - oa, 2, True)}（{pct(oa, ob)}）。可能原因：① 报告里看不到的模型数据不同"
                "（子模型系数、物性、CASE 改的表）；② PIMS 递推（DR/SLP）收敛到了不同的局部解。"
                "建议对比两次的 CASE 表和迭代日志，并用一边的解做初值重跑另一边。")
        else:
            hints.append(f"有 {f.get('inputs_changed')} 处上下限 / 价格不同（第 3 节），优先确认这些是不是预期的输入改动。")
    defs = f.get("desc_changes", 0) + f.get("title_changes", 0) + f.get("extra_cols", 0)
    if defs:
        hints.append(f"有 {f.get('desc_changes', 0)} 个行描述、{f.get('title_changes', 0)} 个子段标题、"
                     f"{f.get('extra_cols', 0)} 个列只在一侧或不同（第 9 节）：两次运行加载的模型表可能不一样。")
    pen = [m for m in f.get("mark_changes") or [] if m[0][0][0] == "Capacity Utilization Summary"]
    if pen:
        na = sum(1 for _, ma, _ in pen if "*" in ma)
        nb = sum(1 for _, _, mb in pen if "*" in mb)
        hints.append(f"能力罚项（Capacity Utilization 的 * 标记）有 {len(pen)} 处不同：只在 A 起作用 {na} 个，"
                     f"只在 B 起作用 {nb} 个（第 9 节）。罚项是否生效会直接改目标函数，值得优先确认。")
    only_a, only_b = f.get("subs_only_a") or [], f.get("subs_only_b") or []
    if only_a or only_b:
        hints.append(f"子模型启停变化：A 独有 {len(only_a)} 个，B 独有 {len(only_b)} 个（第 5 节）。"
                     "装置开停往往是物料路线变化的主线，可以从这里顺着采购 / 销售变化看。")
    nb = f.get("binding")
    if nb and any(nb):
        hints.append(f"约束紧松变化：B 新卡住 {nb[0]} 个、不再卡住 {nb[1]} 个、换边 {nb[2]} 个（第 6 节）。"
                     "新卡住的约束（尤其边际值大的）通常就是 B 目标函数受限的地方。")
    if not hints:
        return []
    return ["## 解读提示（启发式，只给方向）", ""] + [f"- {h}" for h in hints] + [""]


def build_summary(pa: PimsSolution, pb: PimsSolution, cmpv, top: int = 25, rtol: float = 1e-4) -> str:
    c = Ctx(pa, pb, cmpv, top, rtol)
    body: list[str] = []
    for fn in (sec_overview, sec_econ, sec_inputs, sec_flows, sec_submodels, sec_binding, sec_blends,
               sec_capacity, sec_definitions):
        body += fn(c)
    hints = sec_hints(c)   # 依赖前面各节收集的 facts，但放在最前面
    return "\n".join(["# PIMS 解报告对比摘要", ""] + hints + body)
