import random
import time

from rps_tools.diff.base import detect_format
from rps_tools.diff.text_cmp import _myers, match_sequences


def _lcs_len(a, b):
    dp = [0] * (len(b) + 1)
    for x in a:
        prev = 0
        for j, y in enumerate(b):
            cur = dp[j + 1]
            dp[j + 1] = prev + 1 if x == y else max(dp[j + 1], dp[j])
            prev = cur
    return dp[-1]


def test_myers_is_optimal_and_match_sequences_is_consistent():
    rng = random.Random(0)
    for _ in range(300):
        a = [rng.randint(0, 3) for _ in range(rng.randint(0, 30))]
        b = [rng.randint(0, 3) for _ in range(rng.randint(0, 30))]
        m = sorted(_myers(a, b, 0, len(a), 0, len(b), 100))
        assert len(m) == _lcs_len(a, b)
        for seq in (m, match_sequences(a, b)):
            assert all(a[i] == b[j] for i, j in seq)
            assert all(seq[k][0] < seq[k + 1][0] and seq[k][1] < seq[k + 1][1] for k in range(len(seq) - 1))


def test_text_large_repetitive_file_is_exact_and_fast(tmp_path, run_compare):
    # 高度重复的结构（像格式化后的 JSON），旧版 difflib 在这种输入上会卡死
    block = ['  {', '    "lb": 0.0,', '    "ub": null,', '    "value": 1.0', '  },']
    la = block * 40000
    lb = list(la)
    rng = random.Random(1)
    changed = sorted(rng.sample(range(len(lb)), 200))
    for i in changed:
        lb[i] = lb[i] + " x"
    del lb[1000:1010]
    lb[5000:5000] = ["new"] * 7
    (tmp_path / "a.txt").write_text("\n".join(la), encoding="utf-8")
    (tmp_path / "b.txt").write_text("\n".join(lb), encoding="utf-8")
    t = time.time()
    s, rows = run_compare("text", tmp_path / "a.txt", tmp_path / "b.txt")
    assert time.time() - t < 30
    # 删除区间里如果恰好有被修改的行，它算作删除；所以修改数 ≤ 200
    assert s["by_kind"].get("added") == 7
    assert s["by_kind"].get("removed") == 10
    assert 190 <= s["by_kind"].get("changed") <= 200


def test_text_numeric_tolerance_and_whitespace(tmp_path, run_compare):
    (tmp_path / "a.txt").write_text("x1 = 1.000000\nobj 1234.567\nname R3d\ntail  \n", encoding="utf-8")
    (tmp_path / "b.txt").write_text("x1 = 1.0000004\nobj 1235.6\nname R4d\ntail\n", encoding="utf-8")
    s, rows = run_compare("text", tmp_path / "a.txt", tmp_path / "b.txt", numeric_tol=1e-5)
    assert [(r["line_a"], r["kind"]) for r in rows] == [(2, "changed"), (3, "changed")]
    assert rows[0]["delta"] > 1
    assert s["stats"]["ignored_within_tolerance"] == 1


def test_text_ignore_regex_and_case(tmp_path, run_compare):
    (tmp_path / "a.txt").write_text("# time: 10:00\nHello\n", encoding="utf-8")
    (tmp_path / "b.txt").write_text("# time: 11:00\nhello\n", encoding="utf-8")
    s, _ = run_compare("text", tmp_path / "a.txt", tmp_path / "b.txt", ignore_regex="^#", ignore_case=True)
    assert s["total"] == 0


MPS_A = """NAME          TEST
ROWS
 N  obj
 L  c1
 G  c2
COLUMNS
    x1        obj       1.0        c1        2.0
    MARKER    'MARKER'  'INTORG'
    x2        obj       3.0        c2        1.0
    MARKER    'MARKER'  'INTEND'
RHS
    RHS       c1        10.0       c2        1.0
BOUNDS
 UP BND       x1        4.0
 BV BND       x2
ENDATA
"""

# 同一个模型：列顺序打乱、系数一处变化、新增约束、界变化
MPS_B = """NAME          TEST
ROWS
 N  obj
 G  c2
 L  c1
 E  c3
COLUMNS
    MARKER    'MARKER'  'INTORG'
    x2        c2        1.0        obj       3.0
    MARKER    'MARKER'  'INTEND'
    x1        obj       1.0        c1        2.5
    x1        c3        1.0
RHS
    RHS       c2        1.0        c1        10.0
BOUNDS
 UP BND       x1        5.0
 BV BND       x2
ENDATA
"""


def test_mps_structural(tmp_path, run_compare):
    (tmp_path / "a.mps").write_text(MPS_A, encoding="utf-8")
    (tmp_path / "b.mps").write_text(MPS_B, encoding="utf-8")
    s, rows = run_compare("mps", tmp_path / "a.mps", tmp_path / "b.mps")
    got = {(r["kind"], r["path"]) for r in rows}
    assert got == {
        ("changed", "COLUMNS/x1/c1"),
        ("changed", "BOUNDS/x1/UP"),
        ("added", "ROWS/c3"),
        ("added", "COLUMNS/x1/c3"),
    }
    assert s["stats"]["a"]["integers"] == 1 and s["stats"]["b"]["rows"] == 4


def test_detect_format():
    assert detect_format("a.json", "b.json") == "json"
    assert detect_format("model.mps", "model2.MPS") == "mps"
    assert detect_format("x_proto_expression.txt", "y.txt") == "text"
    assert detect_format("a.xmps", "b.xmps") == "text"
    assert detect_format("a.unknown", "b.unknown") == "text"
