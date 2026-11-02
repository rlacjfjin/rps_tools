import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from rps_tools.core.resultdb import DiffSink, DiffStore  # noqa: E402
from rps_tools.diff import COMPARATORS  # noqa: E402


def find_experiment():
    """experiments/ 下 patch.json 引用的 4 个文件都在的实验目录，取数据量最小的一个；没有返回 None。

    数据目录不进仓库，所以依赖真实数据的测试在干净 clone 上会自动跳过。
    """
    import json
    found = []
    for patch in (ROOT / "experiments").glob("*/patch.json"):
        try:
            files = json.loads(patch.read_text(encoding="utf-8")).get("files", {})
            paths = [patch.parent / v for v in files.values()]
        except (ValueError, OSError):
            continue
        if len(paths) == 4 and all(p.is_file() for p in paths):
            found.append((sum(p.stat().st_size for p in paths), patch.parent))
    return min(found)[1] if found else None


def find_export_pairs():
    """export/ 里成对的 (input, output)：output 文件名里的 output 换成 input 即为对应的 input。"""
    pairs = []
    for out in sorted((ROOT / "export").glob("*output*.json")):
        inp = out.with_name(out.name.replace("output", "input"))
        if inp.is_file():
            pairs.append((inp, out))
    return pairs


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
