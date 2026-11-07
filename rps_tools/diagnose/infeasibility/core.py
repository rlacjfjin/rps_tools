"""Find and verify contradictions in the original linear necessary conditions.

The auxiliary LP searches for a nonnegative combination of inequalities.
Its status or residuals alone never produce a proven finding. Every candidate
is replayed with Fraction arithmetic against original rows and variable bounds.
Nonlinear hard rows are omitted in their entirety, yielding a relaxation.
"""
from collections import defaultdict
from fractions import Fraction
import math
import time

from .propagation import forcing_chain_conflict, replay
from .snapshot import business_labels, snapshot_from_proto

# Propagation is linear in the model size, so it runs before the certificate LP
# and never takes the whole budget away from it.
PROPAGATION_BUDGET_SECONDS = 5.0


class UnsupportedRow(ValueError):
    pass


def _number(value):
    if isinstance(value, bool) or value is None:
        raise ValueError(f"不是有效数值：{value!r}")
    result = Fraction(str(value))
    return result


def _limit(value, side):
    if value is None:
        return None
    number = float(value)
    # MPProto uses NaN for absent bounds.
    if math.isnan(number) or number == (-math.inf if side == "lb" else math.inf):
        return None
    return _number(value)


def _add(target, source, factor=Fraction(1)):
    for code, coefficient in source.items():
        target[code] = target.get(code, Fraction(0)) + factor * coefficient
        if not target[code]:
            del target[code]


def _prepare(snapshot):
    if not isinstance(snapshot, dict) or not isinstance(snapshot.get("bounds"), dict) or not isinstance(snapshot.get("rows"), list):
        raise ValueError("快照必须包含 bounds 字典和 rows 列表")
    bounds, warnings = {}, []
    for code, meta in snapshot["bounds"].items():
        if not isinstance(code, str) or not isinstance(meta, dict):
            raise ValueError("变量编码必须是字符串，边界必须是字典")
        lb, ub = _limit(meta.get("lb"), "lb"), _limit(meta.get("ub"), "ub")
        if meta.get("semi_continuous"):
            # A semi-continuous variable may be zero even with a positive lb.
            lb = min(lb, Fraction(0)) if lb is not None else None
            ub = max(ub, Fraction(0)) if ub is not None else None
            warnings.append({"code": code, "reason": "半连续变量按包含零的区间松弛"})
        bounds[code] = (lb, ub)
    records = {}
    for row in snapshot["rows"]:
        if not isinstance(row, dict) or not isinstance(row.get("code"), str) or not isinstance(row.get("vars"), (list, tuple)):
            raise ValueError("约束必须包含字符串 code 和 vars 列表")
        if row.get("type") in ("FX", "LB", "UB") and snapshot.get("bound_semantics") != "mpproto_validation":
            raise ValueError("含 FX/LB/UB 的快照必须先通过 snapshot_from_proto 还原生效边界")
        if row["code"] in records:
            raise ValueError(f"约束编码重复：{row['code']}")
        records[row["code"]] = row
    cache = {}

    def expand_symbol(code, stack):
        if code in bounds:
            return {code: Fraction(1)}, Fraction(0), set()
        if code not in records:
            raise ValueError(f"引用了未定义的变量或表达式：{code}")
        row = records[code]
        if row.get("type") != "N" or row.get("status", 1) == 0:
            raise UnsupportedRow(f"未展开的非线性或未启用表达式：{code}")
        if code in stack:
            raise UnsupportedRow(f"循环表达式：{code}")
        if code not in cache:
            coefficients, constant, sources = expand_terms(row["vars"], stack | {code})
            # N denotes sum(terms) - RHS in MPProto.
            cache[code] = coefficients, constant - _number(row.get("rhs", 0)), sources | {code}
        return cache[code]

    def expand_terms(terms, stack):
        coefficients, constant, sources = {}, Fraction(0), set()
        for term in terms:
            if not term:
                raise ValueError("约束中存在空项")
            coefficient = _number(term[-1])
            symbols = []
            for factor in term[:-1]:
                if isinstance(factor, str):
                    symbols.append(factor)
                else:
                    coefficient *= _number(factor)
            if not coefficient:
                continue
            if len(symbols) > 1:
                raise UnsupportedRow("包含变量乘积，整行不纳入线性必要条件")
            if not symbols:
                constant += coefficient
                continue
            expanded, offset, used = expand_symbol(symbols[0], stack)
            _add(coefficients, expanded, coefficient)
            constant += coefficient * offset
            sources.update(used)
        return coefficients, constant, sources

    rows, ignored = {}, defaultdict(int)
    for code, row in records.items():
        sense = row.get("type")
        if row.get("status", 1) == 0:
            ignored["inactive"] += 1
            continue
        if sense not in ("E", "L", "G"):
            category = "expression" if sense in ("N", "X") else "bound_directive" if sense in ("FX", "LB", "UB") else "unsupported_type"
            ignored[category] += 1
            continue
        try:
            coefficients, constant, sources = expand_terms(row["vars"], {code})
        except UnsupportedRow as exc:
            ignored["nonlinear_or_unexpanded"] += 1
            warnings.append({"code": code, "reason": str(exc)})
            continue
        rows[code] = {"code": code, "sense": sense, "coefficients": coefficients,
                      "rhs": _number(row.get("rhs", 0)) - constant,
                      "definitions": sorted(sources)}
    return bounds, rows, warnings, dict(ignored)


def _proof(rows, bounds, multipliers, tolerance):
    """Exact bound proof for a linear combination, independent of LP output values."""
    coefficients, rhs, definitions = {}, Fraction(0), set()
    weights = {}
    for code, raw_weight in multipliers.items():
        weight = _number(raw_weight)
        if not weight:
            continue
        row = rows[code]
        if (row["sense"] == "L" and weight < 0) or (row["sense"] == "G" and weight > 0):
            return None
        _add(coefficients, row["coefficients"], weight)
        rhs += weight * row["rhs"]
        definitions.update(row["definitions"])
        weights[code] = str(weight)
    minimum, terms = Fraction(0), []
    for code, coefficient in sorted(coefficients.items()):
        side = "lb" if coefficient > 0 else "ub"
        bound = bounds[code][0 if side == "lb" else 1]
        if bound is None:
            return None
        contribution = coefficient * bound
        minimum += contribution
        terms.append({"variable": code, "bound_type": side, "bound": float(bound),
                      "coefficient": float(coefficient), "contribution": float(contribution),
                      "exact_bound": str(bound), "exact_coefficient": str(coefficient)})
    gap = minimum - rhs
    # This is a gap between necessary bounds, not a point-wise constraint residual.
    if not weights or gap <= _number(tolerance) * max(Fraction(1), abs(rhs), abs(minimum)):
        return None
    return {"rule_id": "LINEAR_CONSTRAINT_CONFLICT", "confidence": "proven",
            "method": "verified_linear_combination", "row_multipliers": weights,
            "source_row_count": len(weights), "expression_definitions": sorted(definitions),
            "bound_evidence": terms,
            "combined_constraint": {"coefficients": {k: str(v) for k, v in sorted(coefficients.items())},
                                    "sense": "L", "rhs": float(rhs), "exact_rhs": str(rhs)},
            "minimum_lhs_from_bounds": float(minimum), "gap_lower_bound": float(gap),
            "exact_gap": str(gap)}


def verify_conflict(model, finding, tolerance=1e-8):
    """Recheck a finding against the supplied model; never trusts a stored gap."""
    try:
        if not math.isfinite(tolerance) or tolerance <= 0:
            return False
        snapshot = model if isinstance(model, dict) else snapshot_from_proto(model)
        bounds, rows, _, _ = _prepare(snapshot)
        if finding.get("rule_id") == "BOUND_CONFLICT":
            lb, ub = bounds[finding["variable"]]
            return lb is not None and ub is not None and lb > ub
        if finding.get("rule_id") == "FORCING_CHAIN_CONFLICT":
            return replay(bounds, rows, finding["steps"], finding["conflict"])
        return _proof(rows, bounds, finding["row_multipliers"], tolerance) is not None
    except (AttributeError, KeyError, TypeError, ValueError, ZeroDivisionError, OverflowError, RecursionError):
        return False


def detect_infeasible(model, input_data=None, *, time_limit=30.0, tolerance=1e-8, use_lp=False):
    """Return a JSON-safe report from MPProto or {bounds, rows}.

    Finds direct bound conflicts, single-row range contradictions, then (if
    requested) searches general linear combinations with scipy's bundled HiGHS.
    No input, proto, start, result, or global configuration is modified.
    A no-finding result is explicitly NOT a proof of nonlinear feasibility.
    """
    if not math.isfinite(time_limit) or time_limit <= 0:
        raise ValueError("time_limit 必须为正的有限秒数")
    if not math.isfinite(tolerance) or tolerance <= 0:
        raise ValueError("tolerance 必须为正的有限数值")
    started = time.perf_counter()
    report = {"schema_version": 1, "status": "inconclusive", "proven_infeasible": False,
              "stage": "presolve", "optimization_solver_called": False, "auxiliary_lp_called": False,
              "findings": [], "warnings": [], "coverage": {}, "timing": {}}

    def finish(status):
        for finding in report["findings"]:
            codes = [finding["variable"]] if finding["rule_id"] == "BOUND_CONFLICT" else [t["variable"] for t in finding["bound_evidence"]]
            finding["bound_sources"] = {code: snapshot["bounds"][code].get("bound_sources", {}) for code in codes
                                        if snapshot["bounds"][code].get("bound_sources")}
        report["status"] = status
        report["proven_infeasible"] = status == "proven_infeasible"
        report["timing"]["total_seconds"] = time.perf_counter() - started
        return report

    try:
        snapshot = model if isinstance(model, dict) else snapshot_from_proto(model)
        bounds, rows, warnings, ignored = _prepare(snapshot)
    except (AttributeError, KeyError, TypeError, ValueError, ZeroDivisionError, OverflowError, RecursionError) as exc:
        report["warnings"].append({"reason": f"模型数据无法规范化：{exc}"})
        return finish("invalid_model")
    report["warnings"] = warnings
    try:
        report["labels"] = business_labels(input_data)
    except (AttributeError, KeyError, TypeError, ValueError) as exc:
        report["labels"] = {}
        report["warnings"].append({"reason": f"业务名称映射失败（不影响数学检查）：{exc}"})
    report["coverage"] = {"variables": len(bounds), "linear_hard_rows": len(rows),
                          "ignored_rows": ignored, "integer_restrictions_relaxed": True,
                          "all_conflicts_enumerated": False}
    for code, (lb, ub) in bounds.items():
        if lb is not None and ub is not None and lb > ub:
            report["findings"].append({"rule_id": "BOUND_CONFLICT", "confidence": "proven",
                                       "variable": code, "lb": float(lb), "ub": float(ub),
                                       "gap_lower_bound": float(lb - ub)})
    if report["findings"]:
        return finish("proven_infeasible")
    for code, row in rows.items():
        for sign in ((1, -1) if row["sense"] == "E" else ((1,) if row["sense"] == "L" else (-1,))):
            finding = _proof(rows, bounds, {code: sign}, tolerance)
            if finding:
                report["findings"].append(finding)
                return finish("proven_infeasible")
    # Forcing chains explain themselves step by step, so they are searched before
    # the weighted-combination certificate even though both are exact proofs.
    propagation_start = time.perf_counter()
    try:
        finding, stop, steps = forcing_chain_conflict(
            bounds, rows,
            deadline=propagation_start + min(PROPAGATION_BUDGET_SECONDS, time_limit))
    except (KeyError, TypeError, ValueError, ZeroDivisionError, OverflowError) as exc:
        finding, stop, steps = None, "failed", 0
        report["warnings"].append({"reason": f"强制赋值链检查未完成（不影响其余检查）：{exc}"})
    report["timing"]["propagation_seconds"] = time.perf_counter() - propagation_start
    report["coverage"]["forcing_chain_search"] = stop
    report["coverage"]["propagation_steps"] = steps
    if finding:
        report["findings"].append(finding)
        return finish("proven_infeasible")
    if not use_lp or not rows:
        report["coverage"]["linear_combination_search"] = "disabled" if not use_lp else "no_rows"
        if stop != "completed":
            # An unfinished search found nothing only because it stopped looking.
            # Put this first: readers see just the head of the warning list.
            report["warnings"].insert(0, {"reason": f"强制赋值链检查未完成（{stop}），不能据此认为没有冲突"})
            return finish("inconclusive")
        return finish("no_conflict_found")
    try:
        import numpy as np
        from scipy.optimize import linprog
        from scipy.sparse import coo_matrix
    except ImportError as exc:
        report["warnings"].append({"reason": f"线性组合诊断需要 scipy：{exc}"})
        return finish("inconclusive")

    # A*x <= b includes both directions of E and finite bounds. The alternative
    # LP minimizes b*y with A.T*y=0, sum(y)=1, y>=0. It finds a certificate, not
    # an alleged feasible allocation for the business model.
    inequalities = []
    for code, row in rows.items():
        signs = (1, -1) if row["sense"] == "E" else ((1,) if row["sense"] == "L" else (-1,))
        for sign in signs:
            inequalities.append((code, sign, row["coefficients"], row["rhs"]))
    used = sorted({code for row in rows.values() for code in row["coefficients"]})
    indices = {code: i for i, code in enumerate(used)}
    business_count = len(inequalities)
    for code in used:
        lb, ub = bounds[code]
        if ub is not None:
            inequalities.append((None, 1, {code: Fraction(1)}, ub))
        if lb is not None:
            inequalities.append((None, -1, {code: Fraction(1)}, lb))
    ii, jj, vv, costs, scales = [], [], [], [], []
    try:
        for col, (_, sign, coefficients, rhs) in enumerate(inequalities):
            scale = max([Fraction(1), abs(rhs), *map(abs, coefficients.values())])
            scales.append(scale)
            for code, coefficient in coefficients.items():
                ii.append(indices[code]); jj.append(col); vv.append(float(sign * coefficient / scale))
            ii.append(len(used)); jj.append(col); vv.append(1.0)
            costs.append(float(sign * rhs / scale))
        matrix = coo_matrix((vv, (ii, jj)), shape=(len(used) + 1, len(inequalities))).tocsc()
        target = np.zeros(len(used) + 1)
        target[-1] = 1.0
        lp_start = time.perf_counter()
        report["auxiliary_lp_called"] = True
        result = linprog(costs, A_eq=matrix, b_eq=target, bounds=(0, None), method="highs",
                         options={"time_limit": float(time_limit)})
        report["timing"]["certificate_search_seconds"] = time.perf_counter() - lp_start
        report["coverage"]["linear_combination_search"] = "completed" if result.status in (0, 2) else "incomplete"
        report["certificate_search"] = {"status_code": int(result.status), "message": str(result.message)}
    except (ValueError, RuntimeError, OverflowError) as exc:
        report["warnings"].append({"reason": f"线性组合诊断未完成：{exc}"})
        return finish("inconclusive")
    if result.x is not None:
        raw = defaultdict(float)
        for i in range(business_count):
            code, sign, _, _ = inequalities[i]
            raw[code] += sign * float(result.x[i]) / float(scales[i])
        scale = max(map(abs, raw.values()), default=0.0)
        if scale and math.isfinite(scale):
            for denominator in (1000, 1000000, 1000000000):
                weights = {code: str(Fraction(value / scale).limit_denominator(denominator))
                           for code, value in raw.items() if value}
                finding = _proof(rows, bounds, weights, tolerance)
                if finding:
                    report["findings"].append(finding)
                    return finish("proven_infeasible")
    if result.status == 0 and result.fun is not None and result.fun < -tolerance:
        report["warnings"].append({"reason": "诊断 LP 提示冲突，但精确约束组合核验未通过，不能认定为已证明"})
        return finish("inconclusive")
    return finish("no_conflict_found" if result.status in (0, 2) else "inconclusive")
