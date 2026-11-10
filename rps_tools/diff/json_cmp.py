"""JSON 结构化对比。

和旧版（compare_file_tools/diff_core.py）的关键区别：
  * 列表按「主键」匹配，而不是按下标。rps_solver 的 materialList / manuBlock / cases ... 都有 code，
    旧版只要列表里插入/删除/换了一个元素，后面所有元素都会错位，报出成千上万条假差异；
  * 差异边算边写入 SQLite（见 core/resultdb.py），不在内存里攒；
  * 支持路径通配忽略、按路径指定列表主键、绝对/相对数值容差。

路径格式：  materialList[code=MAT-A]/sell/ub     cduBlock[code=1#U01]/cases[code=CASE1]/mass_flow/value
           列表元素选择器 [...] 算作一个独立层级，通配时可以写 materialList/*/sell 或 materialList[*]/sell
"""
from __future__ import annotations

import difflib
import fnmatch
import math
import re
from collections import Counter
from functools import lru_cache
from itertools import combinations
from pathlib import Path

from rps_tools.core.fileio import dumps_sorted, load_json
from rps_tools.core.resultdb import MISSING
from rps_tools.core.tooling import Param
from rps_tools.diff.base import Comparator, register_comparator

DEFAULT_AUTO_KEYS = "code,id,material,prop,name,crude,materialId,propertyId"
ALIGN_MAX_CELLS = 25_000_000   # 不等长且无主键的列表，元素数乘积超过这个就退化为按下标
INFER_MAX_FIELDS = 10          # 自动推断主键时最多尝试元素的前几个字段
INFER_MIN_UNIQUE = 0.9         # 推断出的主键允许少量重复（如真正重复的边），重复项按内容对齐
INFER_MAX_ITEMS = 200_000      # 超大列表不做主键推断（组合数 × 元素数太贵）
PAIR_MAX_CELLS = 250_000       # 无主键对象列表抵消后，剩余元素数乘积超过这个就不算相似度，按原顺序配对


def _is_num(v) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool)


def _split_csv(s: str) -> list[str]:
    return [x.strip() for x in (s or "").replace("，", ",").split(",") if x.strip()]


def _lines(s: str) -> list[str]:
    return [ln.strip() for ln in (s or "").splitlines() if ln.strip() and not ln.strip().startswith("#")]


# --------------------------------------------------------------------------- path pattern
_SEG_RE = re.compile(r"\[[^\]]*\]|[^/\[\]]+")


def split_path(path: str) -> list[str]:
    """'a/b[code=X]/c' -> ['a', 'b', '[code=X]', 'c']；兼容 '$.a.b' 风格的开头。"""
    path = path.strip()
    if path.startswith("$"):
        path = path[1:].replace(".", "/")
    return _SEG_RE.findall(path)


@lru_cache(maxsize=4096)
def _glob_re(pat: str) -> re.Pattern:
    return re.compile(fnmatch.translate(pat))


class PathPattern:
    def __init__(self, raw: str):
        self.raw = raw
        self.segs = split_path(raw)
        last = self.segs[-1] if self.segs else ""
        self._literal_last = last if last and not any(c in last for c in "*?[") else None

    @staticmethod
    def _seg_match(pat: str, seg: str) -> bool:
        if pat == "*":
            return True
        if pat.startswith("["):
            if not seg.startswith("["):
                return False
            inner = pat[1:-1]
            return inner == "*" or bool(_glob_re(inner).match(seg[1:-1]))
        if seg.startswith("["):
            return False
        return pat == seg or bool(_glob_re(pat).match(seg))

    def match(self, segs: list[str]) -> bool:
        if self._literal_last is not None and (not segs or segs[-1] != self._literal_last):
            return False   # 快速拒绝：绝大多数模式以固定键名结尾
        return self._match(0, segs, 0)

    def _match(self, pi: int, segs: list[str], si: int) -> bool:
        pats = self.segs
        while pi < len(pats):
            p = pats[pi]
            if p == "**":
                if pi == len(pats) - 1:
                    return True
                return any(self._match(pi + 1, segs, k) for k in range(si, len(segs) + 1))
            if si >= len(segs) or not self._seg_match(p, segs[si]):
                return False
            pi += 1
            si += 1
        return si == len(segs)


def join_path(segs: list[str]) -> str:
    out = []
    for s in segs:
        if s.startswith("[") or not out:
            out.append(s)
        else:
            out.append("/" + s)
    return "".join(out)


# --------------------------------------------------------------------------- differ
class JsonDiffer:
    def __init__(self, sink, ignore_keys=(), ignore_paths=(), list_rules=(), auto_keys=(),
                 abs_tol=0.0, rel_tol=0.0, list_fallback="align", str_lists_unordered=True):
        self.sink = sink
        self.ignore_keys = set(ignore_keys)
        self.ignore_paths = [PathPattern(p) for p in ignore_paths]
        self.list_rules = list(list_rules)            # [(PathPattern, mode, keys)]
        self.auto_keys = [k for k in auto_keys if k not in self.ignore_keys]
        self.abs_tol = float(abs_tol or 0)
        self.rel_tol = float(rel_tol or 0)
        self.list_fallback = list_fallback
        self.str_lists_unordered = str_lists_unordered
        self.path: list[str] = []
        self.nodes = 0
        self.lists_keyed = Counter()

    # ---------------------------------------------------------------- emit
    def _ignored(self) -> bool:
        return any(p.match(self.path) for p in self.ignore_paths)

    def emit(self, kind, old, new, note=None) -> None:
        if self.ignore_paths and self._ignored():
            return
        segs = self.path
        field = next((s for s in reversed(segs) if not s.startswith("[")), None)
        self.sink.add(kind, join_path(segs), old, new,
                      section=segs[0] if segs else "(root)", field=field, note=note)

    # ---------------------------------------------------------------- walk
    def walk(self, a, b) -> None:
        self.nodes += 1
        if self.ignore_paths and self._ignored():
            return
        ta, tb = type(a), type(b)
        if ta is dict and tb is dict:
            self._dict(a, b)
        elif ta is list and tb is list:
            self._list(a, b)
        elif _is_num(a) and _is_num(b):
            if not self._num_eq(a, b):
                self.emit("changed", a, b)
        elif a != b:
            if a is None or b is None or ta is tb:
                self.emit("changed", a, b)
            else:
                self.emit("type_changed", a, b, note=f"{ta.__name__} → {tb.__name__}")

    def _num_eq(self, a, b) -> bool:
        if a == b:
            return True
        if isinstance(a, float) and isinstance(b, float) and math.isnan(a) and math.isnan(b):
            return True
        d = abs(a - b)
        if d <= self.abs_tol:
            return True
        return self.rel_tol > 0 and d <= self.rel_tol * max(abs(a), abs(b))

    def _dict(self, a: dict, b: dict) -> None:
        ign = self.ignore_keys
        path = self.path
        for k, va in a.items():
            if k in ign:
                continue
            path.append(k)
            if k in b:
                self.walk(va, b[k])
            else:
                self.emit("removed", va, MISSING)
            path.pop()
        for k, vb in b.items():
            if k in ign or k in a:
                continue
            path.append(k)
            self.emit("added", MISSING, vb)
            path.pop()

    # ---------------------------------------------------------------- lists
    def _list(self, a: list, b: list) -> None:
        if not a and not b:
            return
        mode, keys = self._explicit_rule()
        if mode is None:
            mode, keys = self._auto_mode(a, b)
        if mode == "keyed":
            self.lists_keyed["+".join(keys)] += 1
            self._keyed(a, b, keys)
        elif mode == "multiset":
            self._multiset(a, b)
        elif mode == "align":
            self._align(a, b)
        elif mode == "content":
            self._content(a, b)
        else:
            self._index(a, b)

    def _explicit_rule(self):
        for pat, mode, keys in self.list_rules:
            if pat.match(self.path):
                return mode, keys
        return None, None

    def _auto_mode(self, a: list, b: list):
        if all(type(e) is dict for e in a) and all(type(e) is dict for e in b):
            keys = self._find_key(a, b, self.auto_keys, 2) or self._infer_key(a, b)
            if keys:
                return "keyed", keys
            if self.list_fallback == "align":
                return "content", None
        elif self.str_lists_unordered and self._is_id_list(a) and self._is_id_list(b):
            return "multiset", None
        if len(a) == len(b):
            return "index", None
        return self.list_fallback, None

    @staticmethod
    def _is_id_list(lst: list) -> bool:
        """短标识符组成的字符串列表（如 property_set、critical_nodes），顺序没有意义；日志之类的文本行不算。"""
        return all(type(e) is str and len(e) <= 64 and not any(c.isspace() for c in e) for e in lst)

    def _find_key(self, a: list, b: list, cands: list[str], max_fields: int):
        """按字段数从少到多，返回第一个在两边都严格唯一的键组合。"""
        for size in range(1, min(max_fields, len(cands)) + 1):
            for ks in combinations(cands, size):
                if self._distinct_ratio(a, ks) == 1.0 and self._distinct_ratio(b, ks) == 1.0:
                    return list(ks)
        return None

    def _infer_key(self, a: list, b: list):
        """候选键都不唯一时，从元素自身的字符串/整数字段里推断（例如 edges 的 src+dst+kind）。

        先找严格唯一的组合；找不到就取「不同取值占比」最高且 ≥ INFER_MIN_UNIQUE 的组合，
        少量真正重复的元素会在 _keyed 里按内容兜底匹配。
        """
        if len(a) + len(b) > INFER_MAX_ITEMS:
            return None
        sample = (a or b)[0]
        cands = [k for k, v in sample.items()
                 if k not in self.ignore_keys and (type(v) is str or type(v) is int)][:INFER_MAX_FIELDS]
        strict = self._find_key(a, b, cands, 3)
        if strict:
            return strict
        best, best_ratio = None, INFER_MIN_UNIQUE
        for size in range(1, min(3, len(cands)) + 1):
            for ks in combinations(cands, size):
                ratio = min(self._distinct_ratio(a, ks), self._distinct_ratio(b, ks))
                if ratio > best_ratio:
                    best, best_ratio = list(ks), ratio
        return best

    @staticmethod
    def _distinct_ratio(lst: list, keys: tuple) -> float:
        """不同取值数 / 元素数；有元素缺键或值不可哈希时返回 0。空列表视为 1。

        允许一个元素的键值为 null（如分流块的默认去向 code 为空）；多个 null 或整个列表都是 null 时不能当主键。
        """
        if not lst:
            return 1.0
        seen = set()
        nulls = 0
        for e in lst:
            try:
                v = tuple(e[k] for k in keys)
            except KeyError:
                return 0.0
            if any(isinstance(x, (dict, list)) for x in v):
                return 0.0
            if any(x is None for x in v):
                nulls += 1
                if nulls > 1 or len(lst) == 1:
                    return 0.0
            seen.add(v)
        return len(seen) / len(lst)

    @staticmethod
    def _selector(keys, vals) -> str:
        return "[" + ",".join(f"{k}={v}" for k, v in zip(keys, vals)) + "]"

    def _keyed(self, a: list, b: list, keys: list) -> None:
        def index(lst):
            m, rest = {}, []
            for e in lst:
                try:
                    k = tuple(e[x] for x in keys) if type(e) is dict else None
                except KeyError:
                    k = None
                if k is None or any(isinstance(x, (dict, list)) for x in k) or k in m:
                    rest.append(e)
                else:
                    m[k] = e
            return m, rest

        ma, ra = index(a)
        mb, rb = index(b)
        path = self.path
        for k, ea in ma.items():
            path.append(self._selector(keys, k))
            if k in mb:
                self.walk(ea, mb[k])
            else:
                self.emit("removed", ea, MISSING)
            path.pop()
        for k, eb in mb.items():
            if k not in ma:
                path.append(self._selector(keys, k))
                self.emit("added", MISSING, eb)
                path.pop()
        if ra or rb:   # 缺主键 / 主键重复的元素：先抵消内容完全相同的，剩下的按顺序对齐
            self._residual(ra, rb)

    def _unmatched(self, a: list, b: list) -> tuple[list[int], list[int]]:
        """不看顺序抵消两边内容完全相同的元素，返回各自剩下元素的下标。"""
        sa = [dumps_sorted(self._strip(e)) for e in a]
        sb = [dumps_sorted(self._strip(e)) for e in b]
        left_b = Counter(sb)
        keep_a = []
        for i, s in enumerate(sa):
            if left_b[s] > 0:
                left_b[s] -= 1
            else:
                keep_a.append(i)
        left_a = Counter(sa)
        keep_b = []
        for j, s in enumerate(sb):
            if left_a[s] > 0:
                left_a[s] -= 1
            else:
                keep_b.append(j)
        return keep_a, keep_b

    def _residual(self, a: list, b: list) -> None:
        keep_a, keep_b = self._unmatched(a, b)
        if keep_a or keep_b:
            self._align([a[i] for i in keep_a], [b[j] for j in keep_b], label="?")

    def _content(self, a: list, b: list) -> None:
        """没有主键的对象列表：顺序不算差异。内容相同的元素直接抵消，其余按相似度配对后深比较。"""
        keep_a, keep_b = self._unmatched(a, b)
        pairs = self._pair_similar(a, b, keep_a, keep_b)
        path = self.path
        for i, j in pairs:
            path.append(f"[{i}]" if i == j else f"[{i}→{j}]")
            self.walk(a[i], b[j])
            path.pop()
        used_a, used_b = {i for i, _ in pairs}, {j for _, j in pairs}
        for i in keep_a:
            if i not in used_a:
                path.append(f"[{i}]")
                self.emit("removed", a[i], MISSING)
                path.pop()
        for j in keep_b:
            if j not in used_b:
                path.append(f"[{j}]")
                self.emit("added", MISSING, b[j])
                path.pop()

    def _pair_similar(self, a: list, b: list, keep_a: list[int], keep_b: list[int]) -> list[tuple[int, int]]:
        """全局贪心：最相似的两个元素先配对；相似度相同时保持原有先后。"""
        if len(keep_a) * len(keep_b) > PAIR_MAX_CELLS:
            return list(zip(keep_a, keep_b))
        ranked = sorted((self._similarity(a[i], b[j]), i, j) for i in keep_a for j in keep_b)
        used_a, used_b, pairs = set(), set(), []
        for _, i, j in ranked:
            if i not in used_a and j not in used_b:
                used_a.add(i)
                used_b.add(j)
                pairs.append((i, j))
        return sorted(pairs)

    def _similarity(self, x: dict, y: dict) -> tuple[int, int]:
        """相同的非空字段数，取负后用于升序排序；字符串/整数字段更像标识，优先于其他取值。"""
        ids = vals = 0
        for k, v in x.items():
            if k in self.ignore_keys or v is None or k not in y or v != y[k]:
                continue
            if type(v) is str or type(v) is int:
                ids += 1
            else:
                vals += 1
        return -ids, -vals

    def _index(self, a: list, b: list) -> None:
        path = self.path
        n = min(len(a), len(b))
        for i in range(n):
            path.append(f"[{i}]")
            self.walk(a[i], b[i])
            path.pop()
        for i in range(n, len(a)):
            path.append(f"[{i}]")
            self.emit("removed", a[i], MISSING)
            path.pop()
        for i in range(n, len(b)):
            path.append(f"[{i}]")
            self.emit("added", MISSING, b[i])
            path.pop()

    def _strip(self, v):
        if not self.ignore_keys:
            return v
        if type(v) is dict:
            return {k: self._strip(x) for k, x in v.items() if k not in self.ignore_keys}
        if type(v) is list:
            return [self._strip(x) for x in v]
        return v

    def _multiset(self, a: list, b: list) -> None:
        ca = Counter(dumps_sorted(self._strip(e)) for e in a)
        cb = Counter(dumps_sorted(self._strip(e)) for e in b)
        sa = {dumps_sorted(self._strip(e)): e for e in a}
        sb = {dumps_sorted(self._strip(e)): e for e in b}
        self.path.append("[*]")
        for s in (ca - cb).elements():
            self.emit("removed", sa[s], MISSING, note="multiset")
        for s in (cb - ca).elements():
            self.emit("added", MISSING, sb[s], note="multiset")
        self.path.pop()

    def _align(self, a: list, b: list, label: str = "") -> None:
        """不等长、无主键的列表：先按元素整体内容做序列对齐，再把替换块逐个深比较。"""
        if len(a) * len(b) > ALIGN_MAX_CELLS:
            return self._index(a, b)
        ka = [dumps_sorted(self._strip(e)) for e in a]
        kb = [dumps_sorted(self._strip(e)) for e in b]
        sm = difflib.SequenceMatcher(None, ka, kb, autojunk=False)
        path = self.path

        def sel(i, j=None):
            if j is None or i == j:
                return f"[{label}{i}]"
            return f"[{label}{i}→{j}]"

        for tag, i1, i2, j1, j2 in sm.get_opcodes():
            if tag == "equal":
                continue
            n = min(i2 - i1, j2 - j1) if tag == "replace" else 0
            for t in range(n):
                path.append(sel(i1 + t, j1 + t))
                self.walk(a[i1 + t], b[j1 + t])
                path.pop()
            for i in range(i1 + n, i2):
                path.append(sel(i))
                self.emit("removed", a[i], MISSING)
                path.pop()
            for j in range(j1 + n, j2):
                path.append(sel(j))
                self.emit("added", MISSING, b[j])
                path.pop()


def parse_list_rules(text: str):
    """每行 `路径模式: key1,key2`；或 `路径模式: @index / @multiset / @align`。"""
    rules = []
    for line in _lines(text):
        if ":" not in line:
            raise ValueError(f"列表主键规则格式错误（缺少冒号）：{line}")
        pat, spec = line.rsplit(":", 1)
        spec = spec.strip()
        if spec.startswith("@"):
            mode = spec[1:]
            if mode not in ("index", "multiset", "align"):
                raise ValueError(f"未知列表模式 {spec}（可选 @index / @multiset / @align）")
            rules.append((PathPattern(pat.strip()), mode, None))
        else:
            keys = _split_csv(spec)
            if not keys:
                raise ValueError(f"列表主键规则缺少主键：{line}")
            rules.append((PathPattern(pat.strip()), "keyed", keys))
    return rules


@register_comparator
class JsonComparator(Comparator):
    name = "json"
    title = "JSON 结构化对比"
    description = "逐个键对比；列表自动按主键（code/id/...）匹配，避免插入/删除元素导致的错位。"
    extensions = (".json",)
    options = [
        Param("ignore_keys", "忽略的键名", default="materialId",
              help="逗号分隔；任意层级出现的同名键都不参与对比。常见的还有 dataId、propertyId"),
        Param("abs_tol", "数值绝对容差", type="float", default=0.0, help="|a-b| ≤ 容差视为相等，0 = 精确比较"),
        Param("rel_tol", "数值相对容差", type="float", default=1e-9,
              help="|a-b| ≤ 容差 × max(|a|,|b|) 视为相等。默认 1e-9 只滤掉浮点舍入噪声（如 1.2999999999999998 vs 1.3），0 = 完全精确"),
        Param("ignore_paths", "忽略的路径", type="text", default="",
              placeholder="**/log\nmaterialList/*/marginal_benefit\ntotal_time",
              help="每行一个，支持 * (一层) 和 ** (任意层)，列表元素 [..] 算一层"),
        Param("list_keys", "列表匹配主键", type="text", default="", advanced=True,
              placeholder="manuBlock/*/cases: code\n**/res_defence: prop\nlog: @multiset",
              help="每行 `路径: 主键1,主键2`，或 `路径: @index/@multiset/@align`。不配置时自动从下方候选键里找唯一键"),
        Param("auto_keys", "自动主键候选", default=DEFAULT_AUTO_KEYS, advanced=True,
              help="按顺序尝试，第一个在两边都唯一的键作为列表主键（再找不到就尝试两两组合）"),
        Param("list_fallback", "无主键列表的匹配方式", type="choice", default="align", advanced=True,
              choices=[("align", "按内容对齐（推荐）"), ("index", "按下标"), ("multiset", "当作无序集合")],
              help="仅对找不到主键的列表生效。按内容对齐时，对象列表忽略顺序：内容相同的直接抵消，其余按相似度配对；"
                   "数字等其他列表等长时按下标、不等长时按序列对齐"),
        Param("str_lists_unordered", "标识符列表忽略顺序", type="bool", default=True, advanced=True,
              help="由短标识符（无空格）组成的字符串列表，如 property_set、critical_nodes，按集合比较；日志/数字列表仍按顺序"),
    ]

    def compare(self, path_a: Path, path_b: Path, opts: dict, sink, progress) -> dict:
        progress(5, f"解析 {path_a.name}")
        a = load_json(path_a)
        progress(20, f"解析 {path_b.name}")
        b = load_json(path_b)
        differ = JsonDiffer(
            sink,
            ignore_keys=_split_csv(opts.get("ignore_keys", "")),
            ignore_paths=_lines(opts.get("ignore_paths", "")),
            list_rules=parse_list_rules(opts.get("list_keys", "")),
            auto_keys=_split_csv(opts.get("auto_keys") or DEFAULT_AUTO_KEYS),
            abs_tol=opts.get("abs_tol") or 0.0,
            rel_tol=opts.get("rel_tol") or 0.0,
            list_fallback=opts.get("list_fallback") or "align",
            str_lists_unordered=opts.get("str_lists_unordered", True),
        )
        progress(35, "逐项对比")
        if type(a) is dict and type(b) is dict:
            # 顶层逐键推进，顺便汇报进度（rps 文件顶层是 materialList / manuBlock ... 这些大块）
            keys = [k for k in a if k not in differ.ignore_keys]
            keys += [k for k in b if k not in a and k not in differ.ignore_keys]
            for n, k in enumerate(keys):
                progress(35 + 60 * n / max(1, len(keys)), f"对比 {k}")
                differ.path.append(k)
                if k in a and k in b:
                    differ.walk(a[k], b[k])
                elif k in a:
                    differ.emit("removed", a[k], MISSING)
                else:
                    differ.emit("added", MISSING, b[k])
                differ.path.pop()
        else:
            differ.walk(a, b)
        progress(97, "写入结果")
        return {
            "nodes_compared": differ.nodes,
            "list_keys_used": dict(differ.lists_keyed.most_common(20)),
        }
