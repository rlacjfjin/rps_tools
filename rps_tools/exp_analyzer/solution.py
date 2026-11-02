"""Extract numerical solution values from a rps_solver output JSON."""
from __future__ import annotations
from dataclasses import dataclass, field


def _safe_dict(x):
    return x if isinstance(x, dict) else {}


def _val(d, k, default=0.0):
    v = d.get(k)
    if v is None:
        return default
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


@dataclass
class SolutionView:
    """Solution numbers indexed by (material code, block code, case code)."""
    obj: float = 0.0
    material_revenue: float = 0.0
    material_cost: float = 0.0
    utility_revenue: float = 0.0
    utility_cost: float = 0.0
    material_error: float = 0.0
    utility_error: float = 0.0
    iter_time: float = 0.0
    iter_count: int = 0
    solver_status: str = ""

    # material[code] -> dict of {sell_value, sell_mb, purc_value, purc_mb, dumping, mb}
    material: dict = field(default_factory=dict)

    # block[code]   -> dict (per-block summary)
    #   throughput: total mass_flow value
    #   capacities: list of {code, value, ub, mb}
    #   cases:      list of {code, value, mb}
    #   input_flows:  {material_code -> value}    (for spliter/blender/mixer)
    #   output_flows: {material_code -> value}    (for spliter)
    #   output_block_flows: {target_block -> {value, ratio}}
    block: dict = field(default_factory=dict)


def parse_solution(output_json: dict) -> SolutionView:
    sv = SolutionView()
    sv.obj              = _val(output_json, "OBJ")
    sv.material_revenue = _val(output_json, "material_revenue")
    sv.material_cost    = _val(output_json, "material_cost")
    sv.utility_revenue  = _val(output_json, "utility_revenue")
    sv.utility_cost     = _val(output_json, "utility_cost")
    sv.material_error   = _val(output_json, "material_error")
    sv.utility_error    = _val(output_json, "utility_error")
    sv.iter_time        = _val(output_json, "iter_time")
    sv.iter_count       = int(_val(output_json, "iter_count"))
    sv.solver_status    = str(output_json.get("solver_status") or "")

    # Materials
    for m in output_json.get("materialList", []) or []:
        code = m.get("code")
        if not code:
            continue
        sell = _safe_dict(m.get("sell"))
        purc = _safe_dict(m.get("purchase"))
        sv.material[code] = {
            "sell_value":     _val(sell, "value"),
            "sell_mb":        _val(sell, "marginal_benefit"),
            "purc_value":     _val(purc, "value"),
            "purc_mb":        _val(purc, "marginal_benefit"),
            "dumping":        _val(m,   "dumping"),
            "marginal_benefit": _val(m, "marginal_benefit"),
            "sell_price":     sell.get("price"),
            "purchase_price": purc.get("price"),
            "sell_ub":        sell.get("ub"),
            "sell_lb":        sell.get("lb"),
            "purchase_ub":    purc.get("ub"),
            "type":           m.get("type", ""),
        }

    # Blocks
    block_sections = (
        "cduBlock", "manuBlock", "blenderBlock", "mixerBlock",
        "spliterBlock", "customBlock", "processBlock",
    )
    for section in block_sections:
        for b in output_json.get(section, []) or []:
            code = b.get("code")
            if not code:
                continue
            entry = {
                "section": section,
                "throughput": 0.0,
                "capacities": [],
                "cases": [],
                "input_flows": {},
                "output_flows": {},
                "output_block_flows": {},
                "property_limits": [],
            }

            # manuBlock cases (yield-fixed manu units)
            for c in b.get("cases", []) or []:
                mf = _safe_dict(c.get("mass_flow"))
                v  = _val(mf, "value")
                entry["cases"].append({
                    "code": c.get("code"),
                    "value": v,
                    "mb":   _val(mf, "marginal_benefit"),
                    "status": c.get("status"),
                    "material_ratio": c.get("material_ratio") or [],
                })
                entry["throughput"] += v

            # mass_flow_capacity
            for cap in b.get("mass_flow_capacity", []) or []:
                entry["capacities"].append({
                    "code": cap.get("code"),
                    "value": _val(cap, "value"),
                    "ub":    cap.get("ub"),
                    "lb":    cap.get("lb"),
                    "mb":    _val(cap, "marginal_benefit"),
                })

            # input_material / output_material flows
            for im in b.get("input_material", []) or []:
                if isinstance(im, dict):
                    entry["input_flows"][im.get("code")] = _val(im, "value")
            for om in b.get("output_material", []) or []:
                if isinstance(om, dict):
                    entry["output_flows"][om.get("code")] = _val(om, "value")

            # If block has no manuBlock-style cases (e.g., spliter/blender/mixer),
            # use the sum of input flows as a fallback throughput.
            if entry["throughput"] == 0.0 and entry["input_flows"]:
                entry["throughput"] = sum(abs(v or 0.0) for v in entry["input_flows"].values())
            if entry["throughput"] == 0.0 and entry["output_flows"]:
                entry["throughput"] = sum(abs(v or 0.0) for v in entry["output_flows"].values())

            # output_block (for spliter)
            for ob in b.get("output_block", []) or []:
                if isinstance(ob, dict):
                    entry["output_block_flows"][ob.get("code")] = {
                        "value": _val(ob, "value"),
                        "ratio": _val(ob, "ratio"),
                        "type":  ob.get("type"),
                        "mb":    _val(ob, "marginal_benefit"),
                    }

            # blender property_limit (for R11)
            for pl in b.get("property_limit", []) or []:
                if isinstance(pl, dict):
                    entry["property_limits"].append({
                        "prop":   pl.get("prop") or pl.get("code"),
                        "output": pl.get("output"),
                        "lb":     pl.get("lb"),
                        "ub":     pl.get("ub"),
                        "value":  _val(pl, "value", default=float("nan")),
                        "mb":     _val(pl, "marginal_benefit"),
                    })

            sv.block[code] = entry

    return sv
