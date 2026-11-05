"""Aspen PIMS 解报告对比：用合成的 FullSolution*.htm（虚构代码）测试解析、匹配和分析摘要。"""
from pathlib import Path

import pytest

from rps_tools.diff.base import detect_format
from rps_tools.diff.pims_report import NotPimsReport, parse_pims, parse_value
from rps_tools.diff.pims_summary import build_summary
from rps_tools.diff.pims_cmp import ValueCompare

ROOT = Path(__file__).resolve().parent.parent


def _row(*cells):
    out = []
    for c in cells:
        if isinstance(c, tuple):          # (class, text) 或 (class, text, colspan)
            cls, text, *span = c
            out.append(f"<td class={cls}" + (f" colspan={span[0]}" if span else "") + f">{text}</td>")
        else:
            out.append(f"<td>{c}</td>")
    return "<tr>" + "".join(out) + "</tr>\n"


def _table(*rows):
    return ("<table class=data align=center border=1 bordercolor=white cellpadding=1 cellspacing=1 width=\"100%\">\n"
            + "".join(rows) + "</table>\n")


def _anchor(name, dname):
    return f'<a name={name} dname="{dname}"></a>\n<p class=mt>{dname}</p>\n'


def make_report(path: Path, *, obj="1000.00", case="", buy_a="100", buy_b=None, price="12.50",
                desc_s="S1&yield", penalty=False, cap_act="80", cap_mv="&nbsp;", unit2=True,
                prop_val="0.7500", extra_prop=False, sub_feed="100") -> Path:
    head = (
        "﻿<html><head><title>PIMS MODEL SOLUTION REPORT</title></head><body>\n"
        "<tr><td class=bl>2026/1/1 10:00:00</td>\n"
        "<p class=bc>\nVersion 19.10.37\n"
        f"<strong><p align=center>Model: DEMO<br>\n{('Case: ' + case) if case else ''}</strong>\n"
        f"<p class=c0>\nObj Fn:&nbsp;&nbsp;&nbsp;{obj}<br>\nSolution Status:&nbsp;&nbsp;&nbsp;Optimal</p>\n"
    )
    hdr = _row(("lh", "Feedstock Purchases", 2), ("lh", "Units"), ("rh", "Units/DAY"), ("rh", "Minimum"),
               ("rh", "Maximum"), ("rh", "$/Unit"), ("rh", "$/DAY"), ("rh", "Marg Val"))
    rows = [_row(("lg", "MAT-A"), ("lg", "Crude A"), ("lg", "TONS"), buy_a, "0", "&nbsp;", price,
                 f"{float(buy_a) * float(price):,.0f}", "&nbsp;")]
    if buy_b:
        rows.append(_row(("lg", "MAT-B"), ("lg", "Crude B"), ("lg", "TONS"), buy_b, "0", "&nbsp;", "20.000",
                         f"{float(buy_b) * 20:,.0f}", "&nbsp;"))
    rows.append(_row(("rg", "&nbsp;"), ("lg", "Total Purchases"), ("rg", "&nbsp;"), ("rg", buy_a), ("rg", "&nbsp;"),
                     ("rg", "&nbsp;"), ("rg", "&nbsp;"), ("rg", "1"), ("rg", "&nbsp;")))
    purchases = _anchor("Material_Purchases", "Material Purchases") + _table(hdr, *rows)

    econ = _anchor("Economic_Summary_Analysis", "Economic Summary Analysis") + _table(
        _row(("lh", "Economic Summary"), ("rh", "$/DAY")),
        _row(("lg", "Net Operating Margin"), obj.replace(".", "").rstrip("0") or "0"))

    cap_code = "CAP1<font color=red>*</font>" if penalty else "CAP1"
    capacity = _anchor("Capacity_Utilization_Summary", "Capacity Utilization Summary") + _table(
        _row(("lh", "Process Capacity", 2), ("rh", "Activity"), ("rh", "Minimum"), ("rh", "Maximum"),
             ("rh", "% Maximum"), ("rh", "Marg Val")),
        _row(("lg", cap_code), ("lg", "U1 feed"), cap_act, "0", "100", f"{float(cap_act):.3f}", cap_mv))

    # 宽矩阵分两页：同一行出现在两张表里，列不同
    disp = _anchor("Stream_Disposition_Map", "Stream Disposition Map")
    for cols in (("U1", "U2"), ("U3",)):
        disp += _table(_row(("lh", "Process Stream", 2), ("lh", "Units"), *[("rh", c) for c in cols]),
                       _row(("lg", "S1"), ("lg", desc_s), ("lg", "TONS"), *(["5"] + ["&nbsp;"] * (len(cols) - 1))))

    props = _anchor("Stream_Property_Report", "Stream Property Report") + _table(
        _row(("lh", "Process Stream", 2), ("rh", "SPG"), *([("rh", "SPV")] if extra_prop else [])),
        _row(("lg", "S1"), ("lg", desc_s), f"{prop_val}<font color=red>*</font>",
             *(["1.3333"] if extra_prop else [])))

    blend = _anchor("Specification_Blend", "Specification Blend") + '<a name=SpecBlend_PRD dname="PRD Product"></a>\n' + _table(
        _row(("lh", "Component to Blend", 2), ("rh", "TONS/DAY"), ("rh", "Wt%"), ("ch", "Inspections", 2)),
        _row(("lh", "&nbsp;", 2), ("lh", "&nbsp;"), ("lh", "&nbsp;"), ("rh", "P1"), ("rh", "P2")),
        _row(("lg", "S1"), ("lg", desc_s), "10", "100.00", "0.5000", "1.0000"),
        _row(("lg", "&nbsp;"), ("lg", "Total"), "10", "100.00", "&nbsp;", "&nbsp;")) + _table(
        _row(("lh", "Component Excluded", 2), ("rh", "Marg Val"), ("ch", "Inspections")),
        _row(("lh", "&nbsp;", 2), ("rh", "$/Units"), ("rh", "P1")),
        _row(("lg", "S9"), ("lg", "Other"), "-1.500", "0.2000"))

    subs = _anchor("Process_Submodel_Summary", "Process Submodel Summary")
    for code, on in (("U1", True), ("U2", unit2)):
        if not on:
            continue
        subs += f'<a name=Submodel_{code} dname="{code} Unit {code}" indent=4></a>\n' + _table(
            _row(("lh", "Process Feeds", 2), ("lh", "Units"), ("rh", "Units/DAY"), ("rh", "Wt%")),
            _row(("lg", "MAT-A"), ("lg", "Crude A"), ("lg", "TONS"), sub_feed, "100.00"),
            _row(("lg", "&nbsp;"), ("lg", "Total"), ("lg", "&nbsp;"), sub_feed, "100.00"))

    path.write_text(head + purchases + econ + capacity + disp + props + blend + subs + "</body></html>",
                    encoding="utf-8")
    return path


def test_parse_structure(tmp_path):
    sol = parse_pims(make_report(tmp_path / "FullSolution_a.htm", case="base case", penalty=True))
    assert sol.meta["objective"] == 1000.0 and sol.meta["case"] == "base case" and sol.meta["status"] == "Optimal"
    # 两行表头：跨列分组标题被下一行列名替换；单列标题与下一行拼接
    assert sol.columns[("Specification Blend", "PRD", "Component to Blend")] == ["TONS/DAY", "Wt%", "P1", "P2"]
    assert sol.columns[("Specification Blend", "PRD", "Component Excluded")] == ["Marg Val $/Units", "P1"]
    # 行按代码索引；红色标记从代码和数值里剥离
    assert sol.row_marks[(("Capacity Utilization Summary", "", "Process Capacity"), "CAP1")] == "*"
    spg = sol.tables[("Stream Property Report", "", "Process Stream")]["S1"]["SPG"]
    assert spg.value == 0.75 and spg.mark == "*"
    # 标签列下的空白（Total 行的第一列）不会变成数据列
    assert "Component to Blend" not in sol.columns[("Specification Blend", "PRD", "Component to Blend")]
    assert sol.describe("S1") == "S1&yield"
    assert sol.subs("Process Submodel Summary") == {"U1", "U2"}


def test_parse_value_and_compare_rules():
    v = parse_value("1,234.50")
    assert (v.value, v.dec) == (1234.5, 2)
    assert parse_value("102.5%").pct and parse_value("&nbsp;".replace("&nbsp;", "")).value is None
    cmp = ValueCompare()
    assert cmp.same(parse_value(""), parse_value("0"))                     # 空白 = 0
    assert cmp.same(parse_value("5.12"), parse_value("5.116"))             # 显示精度不同
    assert not cmp.same(parse_value("5.120"), parse_value("5.116"))        # 同精度不放过
    assert not ValueCompare(blank_as_zero=False).same(parse_value(""), parse_value("0"))
    assert ValueCompare(rel_tol=1e-3).same(parse_value("1000"), parse_value("1000.5"))


def test_not_pims_report(tmp_path):
    p = tmp_path / "x.htm"
    p.write_text("<html><body>hello</body></html>", encoding="utf-8")
    with pytest.raises(NotPimsReport):
        parse_pims(p)


def test_detect_format():
    assert detect_format("FullSolution_1.htm", "FullSolution000001_2.htm") == "pims"
    assert detect_format("page.htm", "other.htm") != "pims"


def test_identical_reports_have_no_diff(tmp_path, run_compare):
    a = make_report(tmp_path / "FullSolution_a.htm")
    b = make_report(tmp_path / "FullSolution_b.htm")
    s, rows = run_compare("pims", a, b)
    assert s["total"] == 0, rows
    assert "# PIMS 解报告对比摘要" in s["stats"]["summary_md"]


def test_cell_diffs_match_by_code(tmp_path, run_compare):
    a = make_report(tmp_path / "FullSolution_a.htm")
    b = make_report(tmp_path / "FullSolution_b.htm", obj="900.00", case="base case", buy_a="90", buy_b="5",
                    desc_s="SWING&S1", prop_val="0.7600", unit2=False, extra_prop=True)
    s, rows = run_compare("pims", a, b)
    by_path = {r["path"]: r for r in rows}
    # 描述变了但行仍按代码匹配：S1 不会出现新增 / 删除，只有一条描述差异
    assert not [r for r in rows if "/S1/" in r["path"] and r["kind"] in ("added", "removed")
                and not r["path"].endswith("/SPV")]   # SPV 是 B 新增的物性列，确实是新增
    assert by_path["描述/S1"]["old"] == "S1&yield" and by_path["描述/S1"]["new"] == "SWING&S1"
    # 分页宽矩阵、空白 vs 0 不产生假差异
    assert not [r for r in rows if r["section"] == "Stream Disposition Map"]
    # 数值变化 / 新增行 / 停开的子模型
    assert by_path["Material Purchases/Feedstock Purchases/MAT-A/Units/DAY"]["delta"] == -10
    assert by_path["Material Purchases/Feedstock Purchases/MAT-B/Units/DAY"]["kind"] == "added"
    assert by_path["Process Submodel Summary/U2/Process Feeds/MAT-A/Units/DAY"]["kind"] == "removed"
    assert by_path["Stream Property Report/Process Stream/S1/SPG"]["kind"] == "changed"
    assert by_path["Stream Property Report/Process Stream/S1/SPV"]["kind"] == "added"


def test_summary_sections(tmp_path, run_compare):
    a = make_report(tmp_path / "FullSolution_a.htm", penalty=True, cap_act="100", cap_mv="12.500", price="12.500")
    b = make_report(tmp_path / "FullSolution_b.htm", obj="900.00", case="base case", buy_a="90", buy_b="5",
                    desc_s="SWING&S1", unit2=False, extra_prop=True, cap_act="70", price="12.499")
    s, rows = run_compare("pims", a, b)
    md = s["stats"]["summary_md"]
    assert "-100.00（-10.00%）" in md                         # 目标函数 Δ
    assert "Case 不同" in md
    assert "没有明显不同的上下限 / 价格" in md and "只在小数尾数上不同" in md   # 12.500 -> 12.499 只是尾数
    assert "| MAT-B | Crude B | — | 5 |" in md and "新增" in md   # 新采购
    assert "| U2 | U2 Unit U2 | 100 | — | 只在 A 运行" in md     # 子模型停开
    assert "B 不再卡住（1）" in md and "能力 · CAP1" in md        # 约束紧松
    assert "行标记变化（1）" in md and "能力罚项" in md            # 罚项 * 标记
    assert "| Stream Property Report | Process Stream | SPV | 只在 B |" in md
    assert "SWING&S1" in md                                     # 描述变化


def test_summary_builder_standalone(tmp_path):
    a = parse_pims(make_report(tmp_path / "FullSolution_a.htm"))
    b = parse_pims(make_report(tmp_path / "FullSolution_b.htm", buy_a="150"))
    md = build_summary(a, b, ValueCompare(), top=1, rtol=0)
    assert "## 4. 采购 / 销售 / 公用工程变化" in md and "+50" in md


def _pims_pairs():
    files = sorted((ROOT / "pims-results").glob("*.htm")) if (ROOT / "pims-results").is_dir() else []
    return files[:2] if len(files) >= 2 else None


@pytest.mark.skipif(_pims_pairs() is None, reason="本地没有 pims-results/ 下的两份 PIMS 报告")
def test_real_reports_smoke(run_compare):
    a, b = _pims_pairs()
    s, _ = run_compare("pims", a, b)
    md = s["stats"]["summary_md"]
    assert s["stats"]["a"]["objective"] is not None and s["stats"]["b"]["objective"] is not None
    assert "## 1. 概览" in md and "## 3. 输入参数核对" in md
    # 解析结果里不应残留带标记的字符串数值
    assert s["stats"]["a"]["cells"] > 1000
