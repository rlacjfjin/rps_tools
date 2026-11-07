"""Read-only adapters and optional business labels; no solver imports."""
import math
import copy


def _bound(value, side):
    if value is None:
        return None
    number = float(value)
    if math.isnan(number) or number == (-math.inf if side == "lb" else math.inf):
        return None
    return number if math.isfinite(number) else str(number)


def effective_bounds_from_proto(proto, check=lambda: None):
    """Replay validation's bound updates on scalars, never on the live proto.

    MPProto sorts FX/LB/UB by code and assigns (not intersects) their bounds.
    Its validator applies these directives even when status is zero.
    """
    bounds = {}
    for v in proto.variable:
        check()
        effective = copy.copy(v)
        if callable(getattr(effective, "fix_bounds", None)):
            effective.fix_bounds()
        bounds[v.code] = {
            "lb": _bound(effective.lb, "lb"), "ub": _bound(effective.ub, "ub"),
            "integer": bool(getattr(v, "integer", False)),
            "semi_continuous": bool(getattr(v, "semi_continuous", False)),
            "bound_sources": {},
        }
    directives = []
    for c in proto.constraint:
        check()
        if c.con_type.name in ("FX", "LB", "UB") and c.vars:
            directives.append(c)
    for c in sorted(directives, key=lambda item: item.code):
        check()
        code = c.vars[0][0]
        if code not in bounds:
            raise ValueError(f"边界指令 {c.code} 引用了未定义变量 {code}")
        kind = c.con_type.name
        # Variable.fixed currently accepts float values only; mirror it exactly.
        if kind == "FX" and not isinstance(c.RHS, float):
            continue
        for side in (("lb", "ub") if kind == "FX" else ("lb" if kind == "LB" else "ub",)):
            bounds[code][side] = _bound(c.RHS, side)
            bounds[code]["bound_sources"][side] = c.code
    check()
    return bounds


def snapshot_from_proto(proto):
    """Capture constraints before destructive simplification, without changing proto."""
    return {
        "name": getattr(proto, "name", ""),
        "bounds": effective_bounds_from_proto(proto),
        "bound_semantics": "mpproto_validation",
        "rows": [
            {"code": c.code, "type": c.con_type.name, "rhs": c.RHS,
             "vars": [list(t) for t in c.vars], "status": getattr(c, "status", 1)}
            for c in proto.constraint
        ],
    }


def business_labels(data):
    """Names are presentation metadata, never evidence for a mathematical conflict."""
    labels = {}
    if not data:
        return labels
    suffix = str(data.get("plant") or "") + str(data.get("period") or "")

    def add(code, name, kind, path):
        labels[code] = {"name": name or code, "kind": kind, "input_path": path}

    for table, prefix, kind in (("customVariable", "", "custom_variable"),
                                ("customConstraint", "", "custom_constraint")):
        for item in data.get(table, []) or []:
            code = item["code"]
            add(code + suffix, item.get("name"), kind, f"{table}[code={code}]")
    for table in ("crude", "materialList", "utility"):
        for item in data.get(table, []) or []:
            code, name = item["code"], item.get("name") or item["code"]
            # Utility variables have the repository's additional _U suffix.
            tail = ("_U" if table == "utility" else "") + suffix
            for prefix, field in (("PURC", "purchase"), ("SELL", "sell")):
                add(prefix + code + tail, name, table + "_" + field,
                    f"{table}[code={code}].{field}")
            add(("UBAL" if table == "utility" else "WBAL") + code + tail,
                name + "平衡", "balance", f"{table}[code={code}]")
    for table in ("cduBlock", "manuBlock", "processBlock", "blenderBlock",
                  "mixerBlock", "multi_mixerBlock", "spliterBlock", "customBlock"):
        for block in data.get(table, []) or []:
            code = block.get("code")
            if not code:
                continue
            base = f"{table}[code={code}]"
            unit = block.get("name") or code
            for field in ("mass_flow_capacity", "capacity_balance"):
                for cap in block.get(field, []) or []:
                    if not isinstance(cap, dict) or not cap.get("code"):
                        continue
                    add("CCAP" + cap["code"] + suffix, cap.get("name"), "capacity",
                        f"{base}.{field}[code={cap['code']}]")
            # One activity variable per unit case/column, named S<unit><case>.
            # Derivation chains are mostly made of these, so they need names.
            for group in ("column", "cases"):
                for item in block.get(group, []) or []:
                    if not isinstance(item, dict) or not item.get("code"):
                        continue
                    label = f"{unit} 的 {item.get('name') or item['code']}"
                    where = f"{base}.{group}[code={item['code']}]"
                    add("S" + code + item["code"] + suffix, label, "unit_case", where)
                    add("NCASE" + code + item["code"] + suffix, label, "unit_case", where)
    return labels


def failure_input_context(proto, codes, data=None):
    """Map actual row terms to input locations; never infer a conflict from names.

    Input limits and effective variable limits are kept separate. Values at a
    failed iterate are deliberately excluded from this business-facing context.
    """
    data = data or {}
    labels = business_labels(data)
    suffix = str(data.get("plant") or "") + str(data.get("period") or "")
    references = {}
    material_names = {}

    def reference(code, name, kind, path, **extra):
        item = dict(code=code, name=name, kind=kind, input_path=path, **extra)
        references.setdefault(code, []).append(item)
        labels.setdefault(code, item)

    for table in ("materialList", "crude"):
        for material in data.get(table, []) or []:
            code = material["code"]
            name = material.get("name") or code
            material_names[code] = name
            for prop in material.get("property", []) or []:
                pcode = prop.get("code")
                if not pcode:
                    continue
                path = f"{table}[code={code}].property[code={pcode}]"
                meta = {key: prop[key] for key in ("lb", "ub", "start", "_status", "prop_status") if key in prop}
                pname = f"{name}的{prop.get('name') or pcode}"
                reference("Q" + pcode + code + suffix, pname, "property", path, input_settings=meta)
                reference("R" + pcode + code + suffix, pname + "组成平衡", "property_balance", path,
                          input_settings=meta)
    for table in ("manuBlock", "processBlock"):
        for block in data.get(table, []) or []:
            block_code = block.get("code")
            if not block_code:
                continue
            base = f"{table}[code={block_code}]"
            unit = block.get("name") or block_code
            for field in ("property_recursion", "recursion_balance"):
                for index, row in enumerate(block.get(field, []) or []):
                    material = row.get("code") if field == "property_recursion" else row.get("bind_code")
                    prop = row.get("prop") if field == "property_recursion" else row.get("bind_prop")
                    if not material or not prop:
                        continue
                    code = "R" + prop + material + suffix
                    path = f"{base}.{field}[{index}]"
                    reference(code, f"{material_names.get(material, material)}的{prop}组成平衡",
                              "property_recursion", path, unit=unit,
                              material_code=material, property_code=prop)
                    # An implicit property may have no materialList.property entry.
                    qcode = "Q" + prop + material + suffix
                    implicit = dict(references[code][-1], code=qcode, kind="implicit_property")
                    references.setdefault(qcode, []).append(implicit)
                    labels.setdefault(qcode, implicit)
            for field in ("input_material", "output_material", "material_balance"):
                for index, row in enumerate(block.get(field, []) or []):
                    material = row.get("code")
                    if not material:
                        continue
                    prefix = "S" if field == "input_material" else "R"
                    code = prefix + block_code + material + suffix
                    reference(code, f"{unit}的{material_names.get(material, material)}物流",
                              "material_flow", f"{base}.{field}[{index}]")

    variables = {v.code: v for v in proto.variable}
    constraints = {c.code: c for c in proto.constraint}
    context = {}
    for code in codes:
        row = constraints.get(code)
        terms = sorted({v for term in row.vars for v in term if isinstance(v, str) and v in variables}) if row else []
        linked, seen = [], set()
        for item_code in [code] + terms:
            items = references.get(item_code) or ([labels[item_code]] if item_code in labels else [])
            for item in items:
                path = item["input_path"]
                if path in seen:
                    continue
                seen.add(path)
                linked.append(dict(item, model_code=item_code))
        # Prefer direct row/property references over the potentially long list of activities.
        context[code] = {
            "name": labels.get(code, {}).get("name", code),
            "related_inputs": linked[:12], "related_input_count": len(linked),
            "variables": [dict(code=v, name=labels.get(v, {}).get("name", v),
                               lb=_bound(variables[v].lb, "lb"), ub=_bound(variables[v].ub, "ub"))
                          for v in ([code] if code in variables else terms)[:12]],
        }
    return context
