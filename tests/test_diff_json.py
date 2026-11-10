import json
import random

import pytest

from conftest import find_export_pairs
from rps_tools.diff.json_cmp import PathPattern, split_path

EXPORT_PAIRS = find_export_pairs()


def write(tmp_path, name, obj):
    p = tmp_path / name
    p.write_text(json.dumps(obj, ensure_ascii=False), encoding="utf-8")
    return p


def test_keyed_lists_ignore_order_and_insertions(tmp_path, run_compare):
    a = {"materialList": [{"code": "A", "v": 1}, {"code": "B", "v": 2}, {"code": "C", "v": 3}]}
    b = {"materialList": [{"code": "X", "v": 9}, {"code": "C", "v": 3}, {"code": "A", "v": 1.5}]}
    s, rows = run_compare("json", write(tmp_path, "a.json", a), write(tmp_path, "b.json", b))
    got = {(r["kind"], r["path"]) for r in rows}
    assert got == {
        ("changed", "materialList[code=A]/v"),
        ("removed", "materialList[code=B]"),
        ("added", "materialList[code=X]"),
    }
    changed = next(r for r in rows if r["kind"] == "changed")
    assert changed["delta"] == pytest.approx(0.5)
    assert changed["section"] == "materialList" and changed["field"] == "v"


def test_tolerance_and_ignore(tmp_path, run_compare):
    a = {"x": 1.0, "y": 100.0, "materialId": "u1", "log": ["a"], "m": [{"code": "Q", "dumping": 1}]}
    b = {"x": 1.0000001, "y": 100.5, "materialId": "u2", "log": ["b"], "m": [{"code": "Q", "dumping": 2}]}
    s, rows = run_compare("json", write(tmp_path, "a.json", a), write(tmp_path, "b.json", b),
                          abs_tol=1e-6, ignore_paths="log\nm/*/dumping")
    assert [r["path"] for r in rows] == ["y"]


def test_relative_tolerance(tmp_path, run_compare):
    s, rows = run_compare("json", write(tmp_path, "a.json", {"big": 1e6, "small": 1.0}),
                          write(tmp_path, "b.json", {"big": 1e6 + 0.5, "small": 1.5}), rel_tol=1e-5)
    assert [r["path"] for r in rows] == ["small"]


def test_inferred_composite_key_with_duplicates(tmp_path, run_compare):
    edges = [{"src": f"s{i % 5}", "dst": f"d{i}", "kind": "k", "flow": float(i)} for i in range(20)]
    edges.append(dict(edges[3]))          # 真正重复的一条边
    shuffled = edges[:]
    random.Random(0).shuffle(shuffled)
    s, rows = run_compare("json", write(tmp_path, "a.json", {"edges": edges}),
                          write(tmp_path, "b.json", {"edges": shuffled}))
    assert s["total"] == 0


def test_id_string_lists_are_sets_but_text_lists_are_ordered(tmp_path, run_compare):
    a = {"props": ["SUL", "DEN", "VAN"], "log": ["step 1 ok", "step 2 ok"]}
    b = {"props": ["VAN", "SUL", "DEN"], "log": ["step 2 ok", "step 1 ok"]}
    s, rows = run_compare("json", write(tmp_path, "a.json", a), write(tmp_path, "b.json", b))
    assert {r["section"] for r in rows} == {"log"}


def test_key_field_with_one_null_is_still_a_key(tmp_path, run_compare):
    a = {"out": [{"code": None, "ub": None}, {"code": "U1", "ub": 1.0}, {"code": "U2", "ub": 2.0}]}
    b = {"out": [{"code": "U2", "ub": 3.0}, {"code": None, "ub": None}, {"code": "U1", "ub": 1.0}]}
    s, rows = run_compare("json", write(tmp_path, "a.json", a), write(tmp_path, "b.json", b))
    assert [(r["kind"], r["path"]) for r in rows] == [("changed", "out[code=U2]/ub")]
    assert s["stats"]["list_keys_used"] == {"code": 1}


@pytest.mark.parametrize("a_codes, b_codes", [
    ([None], ["U1"]),                 # 单元素且为 null：不能当主键，否则会变成一删一增
    ([None, None, "U1"], [None, None, "U2"]),   # 多个 null：不能当主键
])
def test_null_heavy_field_is_not_a_key(tmp_path, run_compare, a_codes, b_codes):
    a = {"out": [{"code": c, "v": i * 1.5} for i, c in enumerate(a_codes)]}
    b = {"out": [{"code": c, "v": i * 1.5} for i, c in enumerate(b_codes)]}
    s, rows = run_compare("json", write(tmp_path, "a.json", a), write(tmp_path, "b.json", b))
    assert s["stats"]["list_keys_used"] == {}
    assert [r["kind"] for r in rows] == ["changed"] and rows[0]["field"] == "code"


def test_unkeyed_objects_with_duplicates_ignore_order(tmp_path, run_compare):
    a = {"con": [{"input": "M1", "lb": 0.1}, {"input": "M2", "lb": 0.5}, {"input": "M3", "lb": 0.4},
                 {"input": "M2", "lb": 0.5}, {"input": "M3", "lb": 0.4}, {"input": "M1", "lb": 0.1}]}
    b = {"con": [{"input": "M2", "lb": 0.5}, {"input": "M2", "lb": 0.5}, {"input": "M3", "lb": 0.4},
                 {"input": "M3", "lb": 0.4}, {"input": "M1", "lb": 0.1}, {"input": "M1", "lb": 0.1}]}
    s, rows = run_compare("json", write(tmp_path, "a.json", a), write(tmp_path, "b.json", b))
    assert s["total"] == 0


def test_unkeyed_changed_element_pairs_with_its_own_identity(tmp_path, run_compare):
    a = {"con": [{"input": "M1", "lb": 0.1}, {"input": "M1", "lb": 0.1}, {"input": "M2", "lb": 0.5},
                 {"input": "M2", "lb": 0.5}]}
    b = {"con": [{"input": "M2", "lb": 0.9}, {"input": "M2", "lb": 0.5}, {"input": "M1", "lb": 0.1},
                 {"input": "M1", "lb": 0.2}]}
    s, rows = run_compare("json", write(tmp_path, "a.json", a), write(tmp_path, "b.json", b))
    assert sorted((r["field"], r["old"], r["new"]) for r in rows) == [("lb", "0.1", "0.2"), ("lb", "0.5", "0.9")]


def test_unkeyed_objects_report_surplus_at_original_position(tmp_path, run_compare):
    base = [{"prop": "P1", "ub": None}, {"prop": "P2", "ub": None}]
    extra = {"prop": "P2", "ub": 1.5}
    s, rows = run_compare("json", write(tmp_path, "a.json", {"d": base}),
                          write(tmp_path, "b.json", {"d": [base[1], base[0], extra, dict(base[1])]}))
    assert [(r["kind"], r["path"]) for r in rows] == [("added", "d[2]"), ("added", "d[3]")]


def test_unkeyed_objects_respect_numeric_tolerance(tmp_path, run_compare):
    a = {"con": [{"input": "M1", "lb": 1.3}, {"input": "M1", "lb": 2.0}]}
    b = {"con": [{"input": "M1", "lb": 2.0}, {"input": "M1", "lb": 1.2999999999999998}]}
    s, rows = run_compare("json", write(tmp_path, "a.json", a), write(tmp_path, "b.json", b))
    assert s["total"] == 0


def test_list_fallback_index_keeps_positional_compare(tmp_path, run_compare):
    a = {"con": [{"input": "M1", "lb": 1.0}, {"input": "M1", "lb": 2.0}]}
    b = {"con": [{"input": "M1", "lb": 2.0}, {"input": "M1", "lb": 1.0}]}
    s, rows = run_compare("json", write(tmp_path, "a.json", a), write(tmp_path, "b.json", b),
                          list_fallback="index")
    assert {r["path"] for r in rows} == {"con[0]/lb", "con[1]/lb"}


def test_unkeyed_list_alignment(tmp_path, run_compare):
    a = {"v": [1, 2, 3, 4, 5]}
    b = {"v": [1, 2, 99, 3, 4, 5]}
    s, rows = run_compare("json", write(tmp_path, "a.json", a), write(tmp_path, "b.json", b))
    assert [(r["kind"], r["path"]) for r in rows] == [("added", "v[2]")]


def test_type_change_and_null(tmp_path, run_compare):
    s, rows = run_compare("json", write(tmp_path, "a.json", {"a": None, "b": "1", "c": [1]}),
                          write(tmp_path, "b.json", {"a": 1.0, "b": 1, "c": {"x": 1}}))
    kinds = {r["path"]: r["kind"] for r in rows}
    assert kinds == {"a": "changed", "b": "type_changed", "c": "type_changed"}


def test_explicit_list_rule(tmp_path, run_compare):
    a = {"rows": [{"name": "r1", "id": 1, "v": 1}, {"name": "r2", "id": 2, "v": 2}]}
    b = {"rows": [{"name": "r2", "id": 1, "v": 2}, {"name": "r1", "id": 2, "v": 1}]}
    # 自动会选 id（在候选键里排在 name 前面），显式规则改成按 name 匹配
    s, rows = run_compare("json", write(tmp_path, "a.json", a), write(tmp_path, "b.json", b),
                          list_keys="rows: name")
    assert {r["path"] for r in rows} == {"rows[name=r1]/id", "rows[name=r2]/id"}


def test_path_pattern():
    segs = split_path("materialList[code=MAT-A]/sell/ub")
    assert segs == ["materialList", "[code=MAT-A]", "sell", "ub"]
    assert PathPattern("materialList/*/sell/ub").match(segs)
    assert PathPattern("materialList[*]/sell/ub").match(segs)
    assert PathPattern("**/ub").match(segs)
    assert PathPattern("materialList[code=MAT*]/**").match(segs)
    assert not PathPattern("materialList/sell/ub").match(segs)
    assert not PathPattern("**/lb").match(segs)


@pytest.mark.skipif(len(EXPORT_PAIRS) < 2, reason="export/ 下没有两组本地样例数据（数据不进仓库）")
def test_real_output_files_reordered(tmp_path, run_compare):
    """真实 rps 输出：打乱 materialList 顺序、删一个装置，差异应该只多出那个装置。"""
    a_path, b_path = EXPORT_PAIRS[0][1], EXPORT_PAIRS[1][1]
    base, base_rows = run_compare("json", a_path, b_path)
    b = json.loads(b_path.read_text(encoding="utf-8"))
    if len(b.get("manuBlock") or []) < 4 or not b.get("materialList"):
        pytest.skip("样例不是 rps 输出结构")
    random.Random(2).shuffle(b["materialList"])
    gone = b["manuBlock"].pop(3)["code"]
    s, rows = run_compare("json", a_path, write(tmp_path, "b.json", b))
    prefix = f"manuBlock[code={gone}]"
    inside_base = sum(1 for r in base_rows if r["path"].startswith(prefix))
    assert [r["kind"] for r in rows if r["path"].startswith(prefix)] == ["removed"]
    assert s["total"] == base["total"] - inside_base + 1
