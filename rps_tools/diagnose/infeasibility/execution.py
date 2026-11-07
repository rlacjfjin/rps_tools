"""Read saved native evidence; no process launches or output capture."""
import json
import math
import os
import time


def inconclusive(reason):
    return {"schema_version": 1, "status": "inconclusive", "proven_infeasible": False,
            "stage": "presolve", "optimization_solver_called": False,
            "findings": [], "warnings": [{"reason": reason}], "coverage": {}, "timing": {}}


def _finite_number(value):
    if isinstance(value, bool):
        return None
    try:
        value = float(value)
        return value if math.isfinite(value) else None
    except (TypeError, ValueError, OverflowError):
        return None


def read_iteration_residuals(path, row_names, tolerance, *, max_bytes=8 * 1024 * 1024,
                             time_limit=1.0):
    """Read a bounded tail and locate a useful observed residual for diagnosis.

    Unknown/partial/scaled records are not model evidence. No point is restored
    into MPProto. A better intermediate observation is explicitly marked as
    such, never passed off as a terminal point or a proof of infeasibility.
    """
    result = {"source": "native_iteration_log", "rows": [], "truncated": False,
              "timed_out": False, "malformed_lines": 0, "unknown_rows": 0}
    latest = {}
    best_observed = {}
    deadline = time.perf_counter() + time_limit
    if not path:
        return result
    try:
        with open(path, "rb") as stream:
            size = stream.seek(0, os.SEEK_END)
            start = max(0, size - max_bytes)
            stream.seek(start)
            result["truncated"] = bool(start)
            if start:
                stream.readline(max_bytes)  # Bound even a malformed, newline-free file.
            content = stream.read(max_bytes)
        for ordinal, line in enumerate(content.splitlines()):
            if time.perf_counter() >= deadline:
                result["timed_out"] = True
                break
            if len(line) > 128 * 1024:
                result["malformed_lines"] += 1
                continue
            try:
                event = json.loads(line)
                if not isinstance(event, dict) or event.get("event") != "iteration":
                    continue
                if event.get("scaled") is not False:
                    continue
                profile = event.get("profile")
                if not isinstance(profile, str) or "generator" in profile.lower():
                    continue
                iteration = _finite_number(event.get("iteration"))
                total = _finite_number(event.get("infeas"))
                if iteration is None or iteration < 0 or total is None or total < 0:
                    continue
                rows = []
                for item in event.get("top_violations") or []:
                    if not isinstance(item, dict):
                        continue
                    native_code = item.get("name")
                    code = row_names.get(native_code) if isinstance(native_code, str) else None
                    if not code:
                        result["unknown_rows"] += 1
                        continue
                    violation = _finite_number(item.get("violation"))
                    if violation is None or violation <= tolerance:
                        continue
                    rows.append({"code": code, "native_code": native_code,
                                 "violation": violation,
                                 "activity": _finite_number(item.get("activity")),
                                 "lb": _finite_number(item.get("lb")),
                                 "ub": _finite_number(item.get("ub"))})
                # Retain zero-residual endpoints too: they invalidate earlier bad points.
                key = (str(event.get("pid", "")), profile)
                order = (_finite_number(event.get("timestamp_ms")) or 0,
                         _finite_number(event.get("sequence")) or ordinal, ordinal)
                entry = {"order": order, "profile": profile, "iteration": int(iteration),
                         "total_violation": total, "terminal_record": event.get("note") == "final",
                         "rows": sorted(rows, key=lambda r: (-r["violation"], r["code"]))[:10]}
                if key not in latest or order > latest[key]["order"]:
                    latest[key] = entry
                previous = best_observed.get(key)
                if previous is None or (total, -ordinal) < (previous["total_violation"], -previous["order"][2]):
                    best_observed[key] = entry
            except (ValueError, TypeError, AttributeError):
                result["malformed_lines"] += 1
        # A feasible latest observation invalidates that profile's earlier bad
        # points. Otherwise an early low-residual point can reveal a bottleneck
        # hidden by a subsequent numerical runaway.
        candidates = [(key, best_observed[key]) for key, end in latest.items()
                      if end["total_violation"] > tolerance and best_observed[key]["rows"]
                      and best_observed[key]["total_violation"] > tolerance]
        if candidates:
            key, best = min(candidates, key=lambda pair: (pair[1]["total_violation"], pair[1]["profile"]))
            result.update({k: v for k, v in best.items() if k != "order"})
            end = latest[key]
            result["observation_kind"] = "latest" if best["order"] == end["order"] else "intermediate"
            result["latest_profile_observation"] = {k: end[k] for k in
                ("profile", "iteration", "total_violation", "terminal_record")}
        result["selection"] = "lowest_residual_observation_in_profiles_without_feasible_endpoint"
    except OSError:
        result["unavailable"] = True
    return result


def capture_failure_residuals(proto, output, row_decoder, code_encoder, trace_path, tolerance):
    """Read-only evidence collection, called after native selection has finished."""
    try:
        diagnostics = getattr(proto, "solve_diagnostics", {}) or {}
        if not diagnostics.get("failed") or diagnostics.get("solution_source"):
            return
        rows = {c.code: c for c in proto.constraint
                if getattr(c.con_type, "name", "") in ("E", "L", "G") and c.vars and getattr(c,"status",1) != 0}
        mapping = dict(row_decoder.items()) if code_encoder else {code: code for code in rows}
        mapping = {key: value for key, value in mapping.items() if value in rows}
        # Native solution JSON contains original-space activities even on some failures.
        violations = []
        activities = {} if output.get("scaled") is True else (output.get("equ") or {})
        for native_code, values in activities.items():
            code = mapping.get(native_code)
            if not code or not isinstance(values, (list, tuple)) or not values:
                continue
            activity, rhs = _finite_number(values[0]), _finite_number(rows[code].RHS)
            if activity is None or rhs is None:
                continue
            kind = rows[code].con_type.name
            violation = abs(activity-rhs) if kind == "E" else max(0, rhs-activity if kind == "G" else activity-rhs)
            if violation > tolerance:
                violations.append({"code": code, "native_code": native_code,
                                   "violation": violation, "activity": activity,
                                   "lb": rhs if kind in ("E", "G") else None,
                                   "ub": rhs if kind in ("E", "L") else None})
        if violations:
            evidence = {"source": "native_solution_activities", "profile": output.get("profile"),
                        "iteration": output.get("iterations"),
                        "rows": sorted(violations, key=lambda r: (-r["violation"], r["code"]))[:10]}
        else:
            evidence = read_iteration_residuals(trace_path, mapping, tolerance)
        proto._failure_residuals = evidence
    except Exception:
        # Parsing diagnostics must never change native status or solution availability.
        proto._failure_residuals = {"source": "unavailable", "rows": [], "unavailable": True}
