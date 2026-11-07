"""Domain propagation over the linear relaxation, with a replayable derivation chain.

One row at a time: compute how small and how large its left-hand side can get
inside the current variable bounds. If an end of that range already equals the
right-hand side, every term is pinned to the bound that produced it; if the range
misses the right-hand side entirely, the model is infeasible. Repeating until
nothing changes is how a modeller reads a model by hand, and the ordered steps
are the explanation a business reader can follow.

The rule knows nothing about materials, utilities or capacities, so one
implementation covers a balance with no consumer, a product with no outlet, a
capacity squeezed to zero under a positive lower bound, and interlocked custom
constraints alike.

Nonlinear rows never enter here. The result is therefore a necessary condition of
the original model: a contradiction proves the original model infeasible, while
the absence of one proves nothing.
"""
from collections import deque
from fractions import Fraction
import time

# Non-fixing tightenings can converge without ever terminating (x <= y/2 and
# y <= x/2). Correctness never depends on reaching the fixed point, so both a
# per-bound and a global cap are safe ways to stay inside the budget.
TIGHTENINGS_PER_BOUND = 3


def _extreme(coefficient, lb, ub, minimum):
    """The smallest (or largest) value coefficient*x can take in [lb, ub]."""
    bound = (lb if coefficient > 0 else ub) if minimum else (ub if coefficient > 0 else lb)
    return None if bound is None else coefficient * bound


def _side_used(coefficient, minimum):
    if minimum:
        return "lb" if coefficient > 0 else "ub"
    return "ub" if coefficient > 0 else "lb"


def _activity(row, bounds, minimum):
    """Finite part of the extreme activity plus which variable makes it infinite."""
    finite, unbounded, witness = Fraction(0), 0, None
    for code, coefficient in row["coefficients"].items():
        lb, ub = bounds[code]
        value = _extreme(coefficient, lb, ub, minimum)
        if value is None:
            unbounded += 1
            witness = code
            if unbounded > 1:
                return finite, unbounded, None
        else:
            finite += value
    return finite, unbounded, witness


def _activities(row, bounds):
    """Both extreme activities in one pass; rows can carry hundreds of terms."""
    low, low_open, low_witness = Fraction(0), 0, None
    high, high_open, high_witness = Fraction(0), 0, None
    for code, coefficient in row["coefficients"].items():
        lb, ub = bounds[code]
        small = lb if coefficient > 0 else ub
        large = ub if coefficient > 0 else lb
        if small is None:
            low_open += 1
            low_witness = code
        else:
            low += coefficient * small
        if large is None:
            high_open += 1
            high_witness = code
        else:
            high += coefficient * large
    return ((low, low_open, low_witness if low_open == 1 else None),
            (high, high_open, high_witness if high_open == 1 else None))


def _implied(row, bounds, variable, minimum):
    """Bound on `variable` implied by this row alone, or None when unavailable.

    Uses the minimum activity for the `<=` reading of E/L rows and the maximum
    activity for the `>=` reading of E/G rows.
    """
    if minimum and row["sense"] not in ("E", "L"):
        return None
    if not minimum and row["sense"] not in ("E", "G"):
        return None
    coefficient = row["coefficients"].get(variable)
    if not coefficient:
        return None
    finite, unbounded, witness = _activity(row, bounds, minimum)
    if unbounded > 1 or (unbounded == 1 and witness != variable):
        return None
    if unbounded == 0:
        lb, ub = bounds[variable]
        finite -= _extreme(coefficient, lb, ub, minimum)
    limit = (row["rhs"] - finite) / coefficient
    # Dividing by a negative coefficient flips the direction of the inequality.
    side = "ub" if (coefficient > 0) == minimum else "lb"
    return side, limit


def _uses(row, variable, minimum):
    """The bounds this derivation leaned on: every other term's extreme side."""
    return [(code, _side_used(coefficient, minimum))
            for code, coefficient in row["coefficients"].items() if code != variable]


def _row_conflict(code, row, bounds, activities=None):
    """A row whose reachable range misses its right-hand side entirely."""
    activities = activities or _activities(row, bounds)
    for minimum in (True, False):
        if minimum and row["sense"] not in ("E", "L"):
            continue
        if not minimum and row["sense"] not in ("E", "G"):
            continue
        finite, unbounded, _ = activities[0 if minimum else 1]
        if unbounded:
            continue
        gap = finite - row["rhs"] if minimum else row["rhs"] - finite
        if gap > 0:
            return {"kind": "row_activity", "row": code, "sense": row["sense"],
                    "direction": "min" if minimum else "max", "activity": finite,
                    "rhs": row["rhs"], "gap": gap,
                    "uses": [(name, _side_used(coefficient, minimum))
                             for name, coefficient in row["coefficients"].items()]}
    return None


def propagate(bounds, rows, *, deadline=None, max_steps=None):
    """Tighten bounds to a fixed point; stop early on a contradiction or budget.

    Returns (conflict, ordered_steps, stop_reason). `bounds` is not modified; a
    private copy carries the derived values.

    A bound can be tightened several times, and a step may rest on an earlier
    value of it. So every step and conflict records in `sources`, parallel to
    `uses`, which step held each bound at that moment (None: the original bound).
    Looking dependencies up by bound alone would keep only the last tightening
    and break the replay of whatever was built on an earlier one.
    """
    current = dict(bounds)
    ordered = []
    latest = {}  # (variable, side) -> order of the step holding that bound now.
    counts = {}
    # Insertion-ordered, not a set: iterating this decides which row is examined
    # next, and string sets iterate in PYTHONHASHSEED order. With a set, the same
    # model proved a different (equally valid) conflict in every process, so the
    # same input produced a different explanation each run.
    by_variable = {}
    for code, row in rows.items():
        for variable in row["coefficients"]:
            by_variable.setdefault(variable, {})[code] = None
    queue = deque(rows)
    queued = set(rows)
    budget = max_steps if max_steps is not None else 8 * len(current) + 1000
    stop = "completed"
    while queue:
        if deadline is not None and time.perf_counter() >= deadline:
            return None, ordered, "budget_exhausted"
        if len(ordered) >= budget:
            return None, ordered, "step_limit"
        code = queue.popleft()
        queued.discard(code)
        row = rows[code]
        # One activity pass per row, reused for the conflict test and for every
        # variable. Recomputing it per variable would be quadratic in row width.
        entry = {name: current[name] for name in row["coefficients"]}
        # Everything below reads `entry`, so these are the steps it really rests on.
        held = {key: latest[key] for name in row["coefficients"]
                for key in ((name, "lb"), (name, "ub")) if key in latest}
        activities = _activities(row, entry)
        conflict = _row_conflict(code, row, entry, activities)
        if conflict:
            conflict["sources"] = [held.get(key) for key in conflict["uses"]]
            return conflict, ordered, stop
        for variable, coefficient in row["coefficients"].items():
            lb, ub = current[variable]
            if lb is not None and ub is not None and lb == ub:
                continue
            for minimum in (True, False):
                if row["sense"] not in (("E", "L") if minimum else ("E", "G")):
                    continue
                finite, unbounded, witness = activities[0 if minimum else 1]
                if unbounded > 1 or (unbounded == 1 and witness != variable):
                    continue
                residual = finite
                if unbounded == 0:
                    # Subtract the very contribution the cached activity used.
                    # Bounds tightened earlier in this row only make the cached
                    # activity weaker, so the implication stays valid.
                    residual -= _extreme(coefficient, *entry[variable], minimum)
                limit = (row["rhs"] - residual) / coefficient
                side = "ub" if (coefficient > 0) == minimum else "lb"
                lb, ub = current[variable]
                previous = ub if side == "ub" else lb
                if previous is not None and (limit >= previous if side == "ub" else limit <= previous):
                    continue
                other = lb if side == "ub" else ub
                fixes = other is not None and other == limit
                key = (variable, side)
                if not fixes:
                    counts[key] = counts.get(key, 0) + 1
                    if counts[key] > TIGHTENINGS_PER_BOUND:
                        continue
                current[variable] = (lb, limit) if side == "ub" else (limit, ub)
                uses = _uses(row, variable, minimum)
                step = {"order": len(ordered), "row": code, "sense": row["sense"],
                        "rhs": row["rhs"], "variable": variable, "side": side,
                        "direction": "min" if minimum else "max", "previous": previous,
                        "value": limit, "fixes": fixes,
                        # The rest of the row could only contribute zero, so this
                        # term has to be zero too: the readable "nothing can
                        # offset it" case that business users recognise.
                        "zero_forced": fixes and not limit and not residual,
                        "uses": uses, "sources": [held.get(item) for item in uses]}
                latest[key] = step["order"]
                ordered.append(step)
                lb, ub = current[variable]
                if lb is not None and ub is not None and lb > ub:
                    return ({"kind": "empty_domain", "variable": variable, "lb": lb, "ub": ub,
                             "gap": lb - ub, "uses": [(variable, "lb"), (variable, "ub")],
                             "sources": [latest.get((variable, "lb")), latest.get((variable, "ub"))]},
                            ordered, stop)
                for affected in by_variable.get(variable, ()):  # revisit dependents
                    if affected not in queued:
                        queued.add(affected)
                        queue.append(affected)
    return None, ordered, stop


def chain(conflict, ordered):
    """The derivation steps the conflict actually depends on, in derivation order.

    Also returns the original bounds the derivation ends on. One bound can appear
    as both: as a leaf for an early step and as a derived value for a later one.
    """
    pending = deque([conflict])
    needed, leaves = set(), set()
    while pending:
        record = pending.popleft()
        for key, source in zip(record["uses"], record["sources"]):
            if source is None:
                leaves.add(key)
            elif source not in needed:
                needed.add(source)
                pending.append(ordered[source])
    return [ordered[order] for order in sorted(needed)], sorted(leaves)


def replay(bounds, rows, steps, conflict):
    """Recompute every step from the original rows and bounds, in order."""
    current = dict(bounds)
    for step in steps:
        row = rows.get(step["row"])
        if row is None or step["variable"] not in current:
            return False
        implied = _implied(row, current, step["variable"], step["direction"] == "min")
        if implied is None:
            return False
        side, limit = implied
        claimed = Fraction(step["exact_value"])
        # A replay may derive a tighter bound than recorded; never a weaker one.
        if side != step["side"] or (limit > claimed if side == "ub" else limit < claimed):
            return False
        lb, ub = current[step["variable"]]
        current[step["variable"]] = (lb, claimed) if side == "ub" else (claimed, ub)
    if conflict["kind"] == "empty_domain":
        lb, ub = current[conflict["variable"]]
        return lb is not None and ub is not None and lb > ub
    row = rows.get(conflict["row"])
    if row is None:
        return False
    replayed = _row_conflict(conflict["row"], row, current)
    return bool(replayed and replayed["direction"] == conflict["direction"])


def _emit_step(step):
    return {"order": step["order"], "row": step["row"], "sense": step["sense"],
            "rhs": float(step["rhs"]), "exact_rhs": str(step["rhs"]),
            "variable": step["variable"], "side": step["side"],
            "direction": step["direction"], "fixes": bool(step["fixes"]),
            "zero_forced": bool(step["zero_forced"]),
            "previous": None if step["previous"] is None else float(step["previous"]),
            "value": float(step["value"]), "exact_value": str(step["value"]),
            "uses": [list(item) for item in step["uses"]]}


def forcing_chain_conflict(bounds, rows, *, deadline=None, max_steps=None):
    """Run propagation and package a proven finding, or report why there is none."""
    conflict, ordered, stop = propagate(bounds, rows, deadline=deadline, max_steps=max_steps)
    if conflict is None:
        return None, stop, len(ordered)
    steps, leaves = chain(conflict, ordered)
    emitted = [_emit_step(step) for step in steps]
    if not replay(bounds, rows, emitted, conflict):
        return None, "replay_failed", len(ordered)
    evidence = []
    for code, side in leaves:
        bound = bounds[code][0 if side == "lb" else 1]
        if bound is None:
            continue
        evidence.append({"variable": code, "bound_type": side, "bound": float(bound),
                         "exact_bound": str(bound)})
    definitions = sorted({name for step in steps for name in rows[step["row"]]["definitions"]}
                         | set(rows[conflict["row"]]["definitions"] if conflict["kind"] == "row_activity" else ()))
    detail = ({"kind": "row_activity", "row": conflict["row"], "sense": conflict["sense"],
               "direction": conflict["direction"], "activity": float(conflict["activity"]),
               "exact_activity": str(conflict["activity"]), "rhs": float(conflict["rhs"]),
               "exact_rhs": str(conflict["rhs"])}
              if conflict["kind"] == "row_activity" else
              {"kind": "empty_domain", "variable": conflict["variable"],
               "lb": float(conflict["lb"]), "ub": float(conflict["ub"]),
               "exact_lb": str(conflict["lb"]), "exact_ub": str(conflict["ub"])})
    return {"rule_id": "FORCING_CHAIN_CONFLICT", "confidence": "proven",
            "method": "verified_bound_propagation", "steps": emitted,
            "conflict": detail, "source_row_count": len({step["row"] for step in steps}
                                                        | ({conflict["row"]} if conflict["kind"] == "row_activity" else set())),
            "rows_used": sorted({step["row"] for step in steps}
                                | ({conflict["row"]} if conflict["kind"] == "row_activity" else set())),
            "expression_definitions": definitions, "bound_evidence": evidence,
            "propagation_steps": len(ordered),
            "gap_lower_bound": float(conflict["gap"]), "exact_gap": str(conflict["gap"])}, stop, len(ordered)
