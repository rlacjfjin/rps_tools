"""Aspen PIMS「Full Solution」HTML 报告（FullSolution*.htm）解析。

报告结构（PIMS 19.x 导出）：
  * 每个报告段以  <a name=Material_Purchases dname="Material Purchases">  开头；
    子模型 / 调合产品是  <a name=Submodel_XXX dname="XXX 描述">、<a name=SpecBlend_XXX dname="...">；
  * 数据都在  <table class=data>  里：表头行的单元格 class 为 lh/ch/rh（可能连续两行表头），
    数据行里左对齐（lg/lt/ls/ln/...）的是行标签：代码、描述、单位，其余是数值；
  * 宽矩阵（Stream Disposition Map / Stream Property Report）按列分页，同一行会出现在多张表里。

解析结果按  (报告, 子模型/产品, 表名) -> {(行代码): {列名: Val}}  组织：
  * 行只按「代码」（第一个标签）匹配，描述单独收集。两次运行的描述不同（比如切割物流描述变了）
    不会让整行变成"新增 + 删除"；
  * 数值记录显示的小数位数，对比时可以忽略显示精度造成的差异（5.12 vs 5.116）。
"""
from __future__ import annotations

import html
import re
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path

from rps_tools.core.fileio import read_text

SUB_PREFIXES = ("Submodel_", "SpecBlend_")   # 这些锚点是某个报告下的子段（子模型 / 调合产品）
HEADER_CLASSES = {"lh", "ch", "rh"}
LABEL_CLASSES = {"lg", "lt", "ls", "ln", "l0", "bl", "ml"}
SUBTOTAL = "(小计)"   # 没有任何行标签的合计行

_TOKEN = re.compile(
    r'<a\s+name=["\']?(?P<aname>[^\s"\'>]+)["\']?[^>]*?dname="(?P<dname>[^"]*)"[^>]*>'
    r'|<table\s+class=["\']?data[^>]*>(?P<table>.*?)</table>', re.S | re.I)
_ROW = re.compile(r"<tr[^>]*>(.*?)</tr>", re.S | re.I)
_CELL = re.compile(r"<td(?P<attrs>[^>]*)>(?P<body>.*?)</td>", re.S | re.I)
_CLASS = re.compile(r'class=["\']?([\w-]+)', re.I)
_SPAN = re.compile(r'colspan=["\']?(\d+)', re.I)
_TAG = re.compile(r"<[^>]+>")
_WS = re.compile(r"\s+")


@dataclass(frozen=True)
class Val:
    """一个单元格。

    value: float / str / None（空白）
    dec:   显示的小数位数
    mark:  红色标记 * / +（Stream Property Report 里 * = 递推物性、+ = PCALC 计算），不参与数值比较
    """
    value: float | str | None
    dec: int = 0
    pct: bool = False
    mark: str = ""

    def text(self) -> str:
        v = self.value
        if v is None:
            return self.mark
        if isinstance(v, float):
            return f"{v:,.{self.dec}f}" + ("%" if self.pct else "") + self.mark
        return v + self.mark


BLANK = Val(None)
_MARK = re.compile(r"\s*([*+]+)$")


def split_mark(text: str) -> tuple[str, str]:
    m = _MARK.search(text)
    return (text[:m.start()], m.group(1)) if m and m.start() > 0 else (text, "")


def parse_value(text: str) -> Val:
    t, mark = split_mark(text.strip())
    if not t:
        return Val(None, mark=mark) if mark else BLANK
    s = t.replace(",", "")
    pct = s.endswith("%")
    if pct:
        s = s[:-1]
    try:
        v = float(s)
    except ValueError:
        return Val(t, mark=mark)
    dec = len(s.split(".", 1)[1]) if "." in s and "e" not in s.lower() else 0
    return Val(v, dec, pct, mark)


def _cell_text(body: str) -> str:
    return _WS.sub(" ", html.unescape(_TAG.sub(" ", body))).strip()


TableKey = tuple[str, str, str]          # (报告, 子段代码, 表名)
Rows = dict[str, dict[str, Val]]         # 行代码 -> {列名: Val}


@dataclass
class PimsSolution:
    path: Path
    meta: dict = field(default_factory=dict)
    tables: dict[TableKey, Rows] = field(default_factory=dict)
    columns: dict[TableKey, list[str]] = field(default_factory=dict)   # 表的列（按首次出现顺序）
    desc: dict[str, set[str]] = field(default_factory=lambda: defaultdict(set))  # 行代码 -> 描述
    labels: dict[TableKey, dict[str, str]] = field(default_factory=dict)  # 行代码 -> 描述（本表内）
    sub_titles: dict[tuple[str, str], str] = field(default_factory=dict)  # (报告, 子段) -> 标题
    # 行代码后的红色标记，例如 Capacity Utilization Summary 里 "C1*" 的 * = Has Active Capacity Penalty
    row_marks: dict[tuple[TableKey, str], str] = field(default_factory=dict)
    reports: list[str] = field(default_factory=list)

    @property
    def n_cells(self) -> int:
        return sum(len(cols) for rows in self.tables.values() for cols in rows.values())

    def rows(self, report: str, table: str | None = None, sub: str | None = None) -> dict[tuple, dict[str, Val]]:
        """{(子段, 表名, 行代码): {列: Val}}，可按表名 / 子段过滤。"""
        out = {}
        for (rep, sb, tb), rows in self.tables.items():
            if rep != report or (table is not None and tb != table) or (sub is not None and sb != sub):
                continue
            for code, cols in rows.items():
                out[(sb, tb, code)] = cols
        return out

    def subs(self, report: str) -> set[str]:
        return {sb for (rep, sb, _) in self.tables if rep == report and sb}

    def describe(self, code: str) -> str:
        return " / ".join(sorted(self.desc.get(code, ())))


class NotPimsReport(ValueError):
    pass


def _meta(head: str) -> dict:
    def grab(pat: str):
        m = re.search(pat, head, re.S | re.I)
        return html.unescape(m.group(1)).replace("\xa0", " ").strip() if m else None

    meta = {
        "model": grab(r"Model:\s*([^<]+?)\s*<br"),
        "case": grab(r"Case:\s*([^<]+?)\s*</strong>"),
        "status": grab(r"Solution Status:(?:&nbsp;|\s)*([^<]+)"),
        "timestamp": grab(r"<td class=bl>\s*(\d{4}/\d{1,2}/\d{1,2}\s+[\d:]+)"),
        "version": grab(r"Version\s+([\d.]+)"),
    }
    obj = grab(r"Obj Fn:(?:&nbsp;|\s)*([-+\d.,Ee]+)")
    meta["objective"] = float(obj.replace(",", "")) if obj else None
    meta["objective_text"] = obj
    return meta


def parse_pims(path: Path) -> PimsSolution:
    text = read_text(path)
    head = text[:20000]
    if "PIMS" not in head or "class=data" not in text:
        raise NotPimsReport(f"{Path(path).name} 不像 Aspen PIMS 的 Full Solution 报告（没找到 PIMS 标题或数据表）")
    sol = PimsSolution(Path(path), meta=_meta(head))
    report, sub = "", ""
    for m in _TOKEN.finditer(text):
        if m.group("aname"):
            name, dname = m.group("aname"), html.unescape(m.group("dname")).strip()
            if name.startswith(SUB_PREFIXES):
                sub = dname.split()[0] if dname else name.split("_", 1)[1]
                sol.sub_titles[(report, sub)] = dname
            else:
                report, sub = dname, ""
                if report not in sol.reports:
                    sol.reports.append(report)
            continue
        _parse_table(sol, report, sub, m.group("table"))
    return sol


def _parse_table(sol: PimsSolution, report: str, sub: str, body: str) -> None:
    header: list[str] = []
    header_spans: list[int] = []
    label_cols: set[int] = set()   # 表头是左对齐（lh）的列：标签列，空白值不算数据
    tname = ""
    prev_header = False
    for rm in _ROW.finditer(body):
        cells = []
        for cm in _CELL.finditer(rm.group(1)):
            attrs = cm.group("attrs")
            cls = (_CLASS.search(attrs) or [None, ""])[1].lower()
            span = int((_SPAN.search(attrs) or [None, "1"])[1])
            cells.append((cls, span, _cell_text(cm.group("body"))))
        if not cells:
            continue
        if any(c[0] in HEADER_CLASSES for c in cells):
            expanded = [t for _, span, t in cells for t in [t] * span]
            # 分组标题：跨多列，或居中（ch，例如只有一个质量列时的 "Inspections for Limiting Qualities"）
            spans = [2 if (span > 1 or cls == "ch") else 1 for cls, span, _ in cells for _ in range(span)]
            lh = {i for i, cls in enumerate(c for c, span, _ in cells for c in [c] * span) if cls == "lh"}
            if prev_header:
                # 连续的表头行：上一行是分组标题（"Inspections for ..."）时用下一行的列名（P03）；
                # 上一行是单列标题时拼起来（"Marg Val" + "$/Units" -> "Marg Val $/Units"）
                n = max(len(header), len(expanded))
                header += [""] * (n - len(header))
                header_spans += [1] * (n - len(header_spans))
                header = [(f"{h} {e}" if h and header_spans[i] == 1 else e) if e else h
                          for i, (h, e) in enumerate(zip(header, expanded + [""] * (n - len(expanded))))]
                label_cols &= lh | {i for i, t in enumerate(expanded) if not t}
            else:
                header, header_spans, tname = expanded, spans, expanded[0] if expanded else ""
                label_cols = lh
            prev_header = True
            continue
        prev_header = False
        label, values, pos = [], [], 0
        for cls, span, t in cells:
            if cls in LABEL_CLASSES:
                if t:
                    label.append(t)
            elif t or pos not in label_cols:
                values.append((pos, t))
            pos += span
        if not label and all(not t for _, t in values):
            continue
        key = (report, sub, tname)
        rows = sol.tables.setdefault(key, {})
        cols_order = sol.columns.setdefault(key, [])
        code, mark = split_mark(label[0]) if label else (SUBTOTAL, "")
        if code in rows and not set(rows[code]).isdisjoint(_colnames(header, values)):
            n = 2
            while f"{code}#{n}" in rows and not set(rows[f"{code}#{n}"]).isdisjoint(_colnames(header, values)):
                n += 1
            code = f"{code}#{n}"
        row = rows.setdefault(code, {})
        if mark:
            sol.row_marks[(key, code)] = mark
        if len(label) >= 2:
            sol.desc[code].add(label[1])
            sol.labels.setdefault(key, {})[code] = label[1]
        for i, t in values:
            col = header[i] if i < len(header) and header[i] else f"#{i}"
            row[col] = parse_value(t)
            if col not in cols_order:
                cols_order.append(col)


def _colnames(header: list[str], values: list[tuple[int, str]]) -> set[str]:
    return {header[i] if i < len(header) and header[i] else f"#{i}" for i, _ in values}
