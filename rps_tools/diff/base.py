"""对比器基类与注册表。

新增一种文件格式的对比（比如 proto_expression.txt 的结构化对比）：

    from rps_tools.diff.base import Comparator, register_comparator
    from rps_tools.core.tooling import Param

    @register_comparator
    class ProtoComparator(Comparator):
        name = "proto"
        title = "proto_expression 结构化对比"
        extensions = ()                       # 自动识别用的扩展名，可空
        filename_patterns = ("*proto_expression*.txt",)
        options = [Param("abs_tol", "数值绝对容差", type="float", default=0.0)]

        def compare(self, path_a, path_b, opts, sink, progress):
            ...  # 每发现一处差异调用 sink.add(kind, path, old, new, section=..., field=...)
            return {"stats": "..."}           # 额外统计信息，会显示在结果页

然后在 rps_tools/diff/__init__.py 里 import 这个模块即可，Web 表单会自动出现新格式和它的选项。
"""
from __future__ import annotations

import fnmatch
from pathlib import Path
from typing import Callable

from rps_tools.core.resultdb import DiffSink
from rps_tools.core.tooling import Param

Progress = Callable[[float | None, str], None]


class Comparator:
    name: str = ""
    title: str = ""
    description: str = ""
    extensions: tuple[str, ...] = ()
    filename_patterns: tuple[str, ...] = ()   # 比扩展名优先，例如 "*proto_expression*.txt"
    options: list[Param] = []

    def compare(self, path_a: Path, path_b: Path, opts: dict, sink: DiffSink, progress: Progress) -> dict:
        raise NotImplementedError

    def matches(self, filename: str) -> int:
        """自动识别打分：0 = 不匹配，2 = 文件名模式匹配，1 = 扩展名匹配。"""
        low = filename.lower()
        if any(fnmatch.fnmatch(low, pat.lower()) for pat in self.filename_patterns):
            return 2
        if any(low.endswith(ext) for ext in self.extensions):
            return 1
        return 0


COMPARATORS: dict[str, Comparator] = {}
FALLBACK = "text"


def register_comparator(cls: type[Comparator]) -> type[Comparator]:
    inst = cls()
    if not inst.name:
        raise ValueError(f"{cls.__name__} 没有 name")
    COMPARATORS[inst.name] = inst
    return cls


def detect_format(name_a: str, name_b: str) -> str:
    best, best_score = FALLBACK, 0
    for cmp in COMPARATORS.values():
        score = min(cmp.matches(name_a), cmp.matches(name_b)) or max(cmp.matches(name_a), cmp.matches(name_b)) * 0.5
        if score > best_score:
            best, best_score = cmp.name, score
    return best
