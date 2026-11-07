"""Cooperatively budgeted bounds-only presolve check, without a full snapshot."""
import time

from .core import _limit
from .execution import inconclusive
from .snapshot import effective_bounds_from_proto


def check_bounds(proto, time_limit=0.05):
    started = time.perf_counter()
    deadline = started + time_limit

    def check():
        if time.perf_counter() >= deadline:
            raise TimeoutError

    report = inconclusive("轻量检查未完成")
    try:
        # All overrides must be read before any contradiction can be reported.
        bounds = effective_bounds_from_proto(proto, check)
        count = 0
        for code, meta in bounds.items():
            check()
            lb, ub = _limit(meta["lb"], "lb"), _limit(meta["ub"], "ub")
            count += 1
            if meta["semi_continuous"]:
                lb = min(lb, 0) if lb is not None else None
                ub = max(ub, 0) if ub is not None else None
            if lb is not None and ub is not None and lb > ub:
                report["findings"].append({"rule_id": "BOUND_CONFLICT", "confidence": "proven",
                    "variable": code, "lb": float(lb), "ub": float(ub),
                    "gap_lower_bound": float(lb - ub), "bound_sources": {code: meta["bound_sources"]}})
                break
        check()
        report.update(status="proven_infeasible" if report["findings"] else "no_conflict_found",
                      proven_infeasible=bool(report["findings"]), warnings=[])
        report["coverage"] = {"variables": count, "linear_hard_rows": 0,
                              "linear_combination_search": "disabled", "bounds_only": True,
                              "all_conflicts_enumerated": False}
    except TimeoutError:
        report = inconclusive("轻量检查达到时间预算，已让出主求解流程；检查未完成")
        report["timed_out"] = True
    except Exception as exc:
        report = inconclusive(f"轻量检查未完成：{exc}")
    report["timing"] = {"total_seconds": time.perf_counter() - started, "budget_seconds": time_limit}
    return report
