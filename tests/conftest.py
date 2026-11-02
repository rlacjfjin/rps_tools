import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from rps_tools.core.resultdb import DiffSink, DiffStore  # noqa: E402
from rps_tools.diff import COMPARATORS  # noqa: E402
from rps_tools.regress import find_experiments, find_export_pairs  # noqa: E402,F401


def find_experiment():
    """数据量最小的完整本地实验；没有返回 None。

    数据目录不进仓库，所以依赖真实数据的测试在干净 clone 上会自动跳过。
    """
    found = find_experiments()
    return found[0] if found else None


@pytest.fixture
def run_compare(tmp_path):
    """run_compare(fmt, path_a, path_b, **opts) -> (summary, rows)"""
    def _run(fmt, a, b, **opts):
        cmp = COMPARATORS[fmt]
        o = {p.name: p.default for p in cmp.options}
        o.update(opts)
        sink = DiffSink(tmp_path / f"{fmt}.sqlite")
        stats = cmp.compare(Path(a), Path(b), o, sink, lambda pct, msg="": None)
        summary = sink.close()
        summary["stats"] = stats
        rows = [dict(r) for r in DiffStore(tmp_path / f"{fmt}.sqlite").iter_rows()]
        return summary, rows
    return _run
