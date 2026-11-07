"""Read-only property-bound hints for the returned successful model solution.

No evaluation/optimization, result repair, or status adjudication belongs here.
In particular, a reporting property's fallback start is not solution evidence.
"""
import logging
import math
from collections import Counter


_SUCCESS = (1, 4, 25, 26)
_FLOW_TOL = 1e-9  # Existing model get_value() zero-flow convention.


def _number(value):
    if value is None or isinstance(value, bool):
        return None
    try:
        number = float(value)
        return number if math.isfinite(number) else None
    except (TypeError, ValueError, OverflowError):
        return None


def _kind(row):
    return getattr(getattr(row, "con_type", None), "name", "")


def _tolerance(value, bound):
    # Display-only, in this property's units. Never changes solver tolerances.
    return max(1e-9, 1e-6 * max(abs(value), abs(bound)))


def _point_value(code, variables, rows):
    variable = variables.get(code)
    if variable is not None:
        return _number(variable.value)
    row = rows.get(code)
    if row is not None and _kind(row) in ("N", "X"):
        # For N/X only, MPProto stores the evaluated expression in dual_value;
        # this is NOT an economic dual. Hard-row duals are never read as flows.
        return _number(getattr(row, "dual_value", None))
    return None


def _active_variables(variables, rows):
    pending = [c for c in rows.values() if getattr(c, "status", 1) != 0
               and ((_kind(c) in ("E", "L", "G") and c.vars) or c.code == "OBJ")]
    seen, active = set(), set()
    while pending:
        row = pending.pop()
        if row.code in seen:
            continue
        seen.add(row.code)
        for term in row.vars:
            for factor in term:
                if not isinstance(factor, str):
                    continue
                if factor in variables:
                    active.add(factor)
                elif factor in rows and factor not in seen:
                    pending.append(rows[factor])
    return active


def _flow_evidence(material, variables, rows):
    code = material.code
    names = ["PURC" + code, "SELL" + code, "BWBL" + code, "BVBL" + code]
    names += ["N" + unit + code for unit in list(getattr(material, "input", []) or [])
              + list(getattr(material, "output", []) or [])]
    known = []
    for name in dict.fromkeys(names):
        value = _point_value(name, variables, rows)
        if value is not None:
            known.append((name, value))
    positive = [(code, value) for code, value in known if value > _FLOW_TOL]
    if not positive:
        return None, "zero_flow" if known else "flow_unavailable"
    code, value = max(positive, key=lambda item: item[1])
    return dict(code=code, value=value), None


def _input_catalog(data):
    catalog = {}
    for table in ("materialList", "crude"):
        for material in data.get(table, []) or []:
            catalog[("root", table, material["code"])] = True
            for prop in material.get("property", []) or []:
                key = (table, material["code"], prop["code"])
                catalog[key] = (f"{table}[code={material['code']}].property[code={prop['code']}]", prop, "material_property")
    for block in data.get("blenderBlock", []) or []:
        for index, prop in enumerate(block.get("property_limit", []) or []):
            key = ("blend", prop.get("material"), prop.get("code"))
            catalog[key] = (f"blenderBlock[code={block['code']}].property_limit[{index}]", prop, "blend_specification")
    for prop in data.get("property", []) or []:
        catalog[("global", prop["code"])] = (f"property[code={prop['code']}]", prop, "global_property")
    return catalog


def _input_reference(catalog, table, material, prop, blender):
    keys = ([("blend", material, prop)] if blender else []) + [(table, material, prop), ("global", prop)]
    for key in keys:
        if key in catalog:
            return catalog[key]
    # Generated/inherited properties may have no literal property input entry.
    if ("root", table, material) in catalog:
        return f"{table}[code={material}]", {}, "model_derived_property"
    return None, {}, "input_unavailable"


def _marginal(proto, prop, source_code, *, blender, fixed, active):
    """Use an already available marginal only when its actual source agrees."""
    unavailable = dict(value=None, source_code=source_code, usable=False)
    if fixed:
        return dict(unavailable, reason="fixed_property_no_one_sided_marginal")
    quality = prop.get("marginal_quality") or {}
    value = _number(prop.get("marginal_benefit"))
    if quality.get("verdict") != "usable" or value is None or source_code is None:
        return dict(unavailable, reason=quality.get("reason") or "marginal_unavailable")
    from .quality import _row_verdict, _var_verdict
    if quality.get("reason") == "recomputed":
        original = proto.constraint.loc[source_code] if blender else proto.get_variable(source_code)
        if not hasattr(original, "slp_dual_value" if blender else "slp_reduced_cost"):
            return dict(unavailable, reason="recompute_provenance_missing")
    if blender:
        verdict, reason, _ = _row_verdict(proto, source_code)
        raw = _number(proto.constraint.loc[source_code].dual_value)
        raw = -raw if raw is not None else None  # Existing PropertyTable output convention.
    else:
        verdict, reason, _ = _var_verdict(proto, source_code, active)
        var = proto.get_variable(source_code)
        raw = _number(var.reduced_cost)
        drift = _number(getattr(var, "recompute_drift_exposure", None))
        if drift is not None and drift >= 1:
            return dict(unavailable, reason="drift_unreliable")
    if verdict != "usable" or raw is None:
        return dict(unavailable, reason=reason)
    # Existing output can round tiny marginals to zero. A different source or
    # sign must not be presented as a trustworthy marginal for this bound.
    if not math.isclose(raw, value, abs_tol=1e-6, rel_tol=1e-9):
        return dict(unavailable, reason="marginal_source_mismatch")
    return dict(value=value, source_code=source_code, usable=True, reason=reason,
                interpretation="沿用当前报表的边际单位和符号，仅作局部参考；不是放宽性质后的收益承诺")


def _message(report):
    bindings, fixed = report["binding_properties"], report["fixed_properties"]
    lines = [f"性质卡边提示：{len(bindings)} 项贴边，{len(fixed)} 项固定性质（单列，不计入卡边）。"]
    lines.append("上下限为模型实际生效值；对应原始设置见各条目的 input_settings。")
    for item in bindings[:10]:
        sides = {"lower": "贴下限", "upper": "贴上限", "both": "同时接近上下限"}
        lower = "无" if item["lb"] is None else f"{item['lb']:.10g}"
        upper = "无" if item["ub"] is None else f"{item['ub']:.10g}"
        marginal = item["marginal"]
        mb = f"已有边际={marginal['value']:.10g}（局部参考）" if marginal["usable"] else "可信边际暂不可用"
        lines.append(f"【{sides[item['side']]}】{item['material_name']}（{item['material_code']}）的"
                     f"{item['property_name']}（{item['property_code']}）：实际值={item['value']:.10g}，"
                     f"下限={lower}，上限={upper}；{mb}。入参：{item['input_path']}")
    for item in fixed[:3]:
        lines.append(f"【固定性质】{item['material_name']}（{item['material_code']}）的"
                     f"{item['property_name']}：实际值={item['value']:.10g}，"
                     f"下限=上限={item['lb']:.10g}。入参：{item['input_path']}")
    if len(bindings) > 10 or len(fixed) > 3:
        lines.append("其余条目见 property_bound_diagnostics。")
    lines.append("贴边只说明当前性质接近限制，不等于输入错误、不可行或已证实的收益瓶颈。")
    return "\n".join(lines)


def attach_property_bound_hints(model, result, input_data=None):
    """Annotate successful business results without touching any existing value."""
    if (result.get("solver_status_code") not in _SUCCESS or result.get("solution_available") is False
            or (result.get("solve_diagnostics") or {}).get("failed")
            or "property_bound_diagnostics" in result):
        return
    try:
        proto = model.mpproto
        variables = {v.code: v for v in proto.variable}
        rows = {c.code: c for c in proto.constraint}
        active = _active_variables(variables, rows)
        catalog = _input_catalog(input_data or {})
        report = dict(binding_properties=[], fixed_properties=[], skipped={}, extra_solver_calls=0)
        skipped = Counter()
        groups = (("materialList", getattr(model, "material_list", None), "material_table"),
                  ("crude", getattr(model, "crude_table", None), "crude_table"))
        for table, container, attr in groups:
            if container is None:
                continue
            outputs = {m["code"]: m for m in result.get(table, [])}
            for material in getattr(container, attr):
                out = outputs.get(material._code)
                if not out or not out.get("property"):
                    continue
                flow, reason = _flow_evidence(material, variables, rows)
                if flow is None:
                    skipped[reason] += len(out["property"])
                    continue
                blender = getattr(material.basi, "name", material.basi) == "BlenderOut"
                for prop in out["property"]:
                    pcode, mcode = prop["code"], material.code
                    code = ("XQ" if blender else "Q") + pcode + mcode
                    value = _point_value(code, variables, rows)
                    if value is None:
                        skipped["actual_value_unavailable"] += 1
                        continue
                    if blender:
                        sources = {"lb": "N" + pcode + mcode, "ub": "X" + pcode + mcode}
                        bounds = {side: _number(prop.get(side)) for side, row_code in sources.items()
                                  if row_code in rows and _kind(rows[row_code]) in ("E", "L", "G")
                                  and getattr(rows[row_code], "status", 1) != 0
                                  and rows[row_code].vars}
                    else:
                        variable = variables.get(code)
                        if variable is None:
                            skipped["not_a_property_variable"] += 1
                            continue
                        bounds = {side: _number(getattr(variable, side)) for side in ("lb", "ub")}
                        sources = {"lb": code, "ub": code}
                    lb, ub = bounds.get("lb"), bounds.get("ub")
                    fixed = lb is not None and ub is not None and lb == ub
                    if not blender and not fixed and code not in active:
                        skipped["unused_property"] += 1
                        continue
                    if lb is not None and ub is not None and lb > ub:
                        skipped["inconsistent_bounds"] += 1
                        continue
                    near = [side for side, bound in (("lb", lb), ("ub", ub))
                            if bound is not None and abs(value-bound) <= _tolerance(value, bound)]
                    if not near:
                        continue
                    side = "fixed" if fixed else "both" if len(near) == 2 else "lower" if near[0] == "lb" else "upper"
                    source = sources[near[0]] if len(near) == 1 else None
                    path, raw, scope = _input_reference(catalog, table, material._code, pcode, blender)
                    item = dict(material_code=material._code, material_name=material.name,
                                property_code=pcode, property_name=prop.get("name") or pcode,
                                value=value, lb=lb, ub=ub, side=side, value_source=code,
                                bound_sources={s: sources[s] for s in bounds if bounds[s] is not None},
                                flow_evidence=flow,
                                input_path=path, input_scope=scope, bounds_origin="effective_model",
                                related_inputs=list(dict.fromkeys(
                                    ([f"{table}[code={material._code}]"] if ("root", table, material._code) in catalog else [])
                                    + ([path] if path else []))),
                                input_settings={k: raw[k] for k in ("lb", "ub", "start", "_status") if k in raw},
                                tolerance={s: _tolerance(value, bounds[s]) for s in near},
                                marginal=_marginal(proto, prop, source, blender=blender, fixed=fixed, active=active))
                    report["fixed_properties" if fixed else "binding_properties"].append(item)
        report["skipped"] = dict(skipped)
        for field in ("binding_properties", "fixed_properties"):
            report[field].sort(key=lambda x: (x["input_scope"] not in ("blend_specification", "material_property"),
                                              not (x["marginal"]["usable"] and x["marginal"]["value"] != 0),
                                              x["material_code"], x["property_code"]))
        report["message"] = _message(report)
        result["property_bound_diagnostics"] = report
        if report["binding_properties"] or report["fixed_properties"]:
            logging.getLogger("rps").info(report["message"])
    except Exception:
        logging.getLogger("rps").debug("性质卡边提示未完成，保留原求解结果", exc_info=True)
