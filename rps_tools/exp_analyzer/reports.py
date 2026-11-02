"""Report builders. Each returns a dict that the renderer turns into markdown."""
from __future__ import annotations
from collections import defaultdict, deque
from .solution import SolutionView
from .topology import Topology


# ---------------------------------------------------------------- R1: KPIs
def report_r1_kpis(base: SolutionView, mod: SolutionView) -> dict:
    fields = [
        ("OBJ",              base.obj,              mod.obj),
        ("material_revenue", base.material_revenue, mod.material_revenue),
        ("material_cost",    base.material_cost,    mod.material_cost),
        ("utility_revenue",  base.utility_revenue,  mod.utility_revenue),
        ("utility_cost",     base.utility_cost,     mod.utility_cost),
        ("material_error",   base.material_error,   mod.material_error),
        ("utility_error",    base.utility_error,    mod.utility_error),
        ("iter_time",        base.iter_time,        mod.iter_time),
        ("iter_count",       base.iter_count,       mod.iter_count),
    ]
    rows = []
    for name, a, b in fields:
        delta = b - a
        pct = (delta / a * 100.0) if a not in (0, 0.0) else None
        rows.append({"name": name, "base": a, "mod": b, "delta": delta, "pct": pct})
    return {
        "rows": rows,
        "base_status": base.solver_status,
        "mod_status":  mod.solver_status,
    }


# -------------------------------------------------- R2: OBJ contribution diff
def report_r2_obj_contributions(base: SolutionView, mod: SolutionView,
                                topo_base: Topology, topo_mod: Topology,
                                min_abs_delta: float = 1.0) -> dict:
    """Itemized OBJ contribution change.

    Returns three result groups:
        material  : per-material SELL / PURC contribution rows (all materials)
        summary   : top-level KPI deltas (utility / fix_cost / inventory / transfer)
        reconciled: how (Σ material + Σ summary) compares to total ΔOBJ
                   — useful to spot OBJ pieces not yet attributed
    """
    rows = []
    all_mats = set(base.material) | set(mod.material)
    for code in all_mats:
        bm = base.material.get(code, {})
        mm = mod.material.get(code, {})
        s_price = mm.get("sell_price") or bm.get("sell_price")
        p_price = mm.get("purchase_price") or bm.get("purchase_price")

        if s_price is not None:
            v_b = bm.get("sell_value", 0.0) or 0.0
            v_m = mm.get("sell_value", 0.0) or 0.0
            if abs(v_b) > 1e-12 or abs(v_m) > 1e-12:
                rows.append({
                    "kind":   "SELL",
                    "code":   code,
                    "price":  s_price,
                    "base_qty": v_b, "mod_qty": v_m, "delta_qty": v_m - v_b,
                    "base_obj": s_price * v_b,
                    "mod_obj":  s_price * v_m,
                    "delta_obj": s_price * (v_m - v_b),
                    "mb_base": bm.get("sell_mb", 0.0),
                    "mb_mod":  mm.get("sell_mb", 0.0),
                })

        if p_price is not None:
            v_b = bm.get("purc_value", 0.0) or 0.0
            v_m = mm.get("purc_value", 0.0) or 0.0
            if abs(v_b) > 1e-12 or abs(v_m) > 1e-12:
                rows.append({
                    "kind":   "PURC",
                    "code":   code,
                    "price":  p_price,
                    "base_qty": v_b, "mod_qty": v_m, "delta_qty": v_m - v_b,
                    "base_obj": -p_price * v_b,
                    "mod_obj":  -p_price * v_m,
                    "delta_obj": -p_price * (v_m - v_b),
                    "mb_base": bm.get("purc_mb", 0.0),
                    "mb_mod":  mm.get("purc_mb", 0.0),
                })

    rows.sort(key=lambda r: -abs(r["delta_obj"]))

    # Filter out small deltas (|Δ| < min_abs_delta) for the "interesting" view
    interesting = [r for r in rows if abs(r["delta_obj"]) >= min_abs_delta]
    n_filtered = len(rows) - len(interesting)

    # OBJ summary diffs (utility / fix_cost / inventory / transfer)
    summary = []
    def _row(name, getter):
        a = float(getter(base) or 0.0)
        b = float(getter(mod)  or 0.0)
        summary.append({
            "name": name, "base": a, "mod": b, "delta": b - a,
        })
    # SolutionView fields
    _row("material_revenue", lambda v: v.material_revenue)
    _row("material_cost",    lambda v: -v.material_cost)  # cost contributes negatively
    _row("utility_revenue",  lambda v: v.utility_revenue)
    _row("utility_cost",     lambda v: -v.utility_cost)
    summary_total = sum(s["delta"] for s in summary)

    # Reconciliation: SELL/PURC sum should ≈ material_revenue - material_cost change
    mat_sell_purc_total = sum(r["delta_obj"] for r in rows)
    obj_delta = mod.obj - base.obj
    util_delta = (mod.utility_revenue - base.utility_revenue) - (mod.utility_cost - base.utility_cost)
    unaccounted = obj_delta - mat_sell_purc_total - util_delta

    return {
        "rows":       rows,                # full list
        "interesting": interesting,        # filtered |Δ| >= min_abs_delta
        "n_filtered": n_filtered,
        "min_abs_delta": min_abs_delta,
        "summary":    summary,             # KPI-level deltas (utility + material rev/cost)
        "total_base": sum(r["base_obj"] for r in rows),
        "total_mod":  sum(r["mod_obj"]  for r in rows),
        "total_delta": mat_sell_purc_total,
        "reconcile": {
            "obj_delta":             obj_delta,
            "sell_purc_delta_total": mat_sell_purc_total,
            "utility_delta_total":   util_delta,
            "unaccounted":           unaccounted,
        },
    }


# ------------------------------------------------- R3 (and R4) chain BFS
def _bfs_chain(start_id: str, direction: str,
               topo: Topology, sol: SolutionView,
               max_depth: int, min_flow: float,
               hub_cutoff_producers: int = 8,
               hub_cutoff_consumers: int = 8,
               existing_nodes: dict = None,
               existing_edges: dict = None) -> tuple:
    """Directional BFS from one start node. Returns (nodes_dict, edges_dict).

    If existing_nodes/existing_edges are passed, they're updated in place.
    """
    nodes = existing_nodes if existing_nodes is not None else {}
    edges_dedup = existing_edges if existing_edges is not None else {}
    visited = set((nid, d) for nid in nodes for d in ("up", "down"))   # we don't track dir here actually
    visited.clear()
    visited = set()
    queue = deque()

    if direction == "upstream":
        seeds = [("up",)]
    elif direction == "downstream":
        seeds = [("down",)]
    else:
        seeds = [("up",), ("down",)]
    for (d,) in seeds:
        queue.append((start_id, 0, d))
        visited.add((start_id, d))

    def _node(id_, kind, code, label, extra=None):
        if id_ not in nodes:
            nodes[id_] = {
                "id": id_, "kind": kind, "code": code,
                "label": label, "extra": extra or {},
            }

    def _push(id_, depth, d):
        key = (id_, d)
        if key in visited:
            return
        visited.add(key)
        queue.append((id_, depth, d))

    def _add_edge(src, dst, flow, kind):
        key = (src, dst, kind)
        if key not in edges_dedup:
            edges_dedup[key] = {"src": src, "dst": dst, "flow": flow, "kind": kind}

    while queue:
        nid, depth, dir_tag = queue.popleft()
        if depth > max_depth:
            continue
        kind, code = nid.split(":", 1)

        if kind == "material":
            ms = sol.material.get(code, {})
            sell_val = ms.get("sell_value", 0.0)
            purc_val = ms.get("purc_value", 0.0)
            _node(nid, "material", code,
                  label=f"{code}\nsell={sell_val:.4f}, purc={purc_val:.4f}",
                  extra={"sell_value": sell_val, "purc_value": purc_val,
                         "sell_price": ms.get("sell_price"),
                         "purc_price": ms.get("purchase_price"),
                         "marginal_benefit": ms.get("marginal_benefit")})

            if dir_tag == "down" and abs(sell_val) > min_flow:
                sid = f"sell:{code}"
                _node(sid, "sell", code,
                      label=f"SELL {code}\n{sell_val:.4f} × ¥{ms.get('sell_price')}")
                _add_edge(nid, sid, sell_val, "sell")
            if dir_tag == "up" and abs(purc_val) > min_flow:
                pid = f"purc:{code}"
                _node(pid, "purc", code,
                      label=f"PURC {code}\n{purc_val:.4f} × ¥{ms.get('purchase_price')}")
                _add_edge(pid, nid, purc_val, "purc")

            producers = topo.producers_of.get(code, []) or []
            consumers = topo.consumers_of.get(code, []) or []
            is_hub_up   = len(producers) > hub_cutoff_producers
            is_hub_down = len(consumers) > hub_cutoff_consumers

            if dir_tag == "up":
                if is_hub_up:
                    nodes[nid]["extra"]["hub_up"] = len(producers)
                    continue
                for prod in producers:
                    bid = f"block:{prod}"
                    b = sol.block.get(prod, {})
                    flow = (b.get("output_flows") or {}).get(code) or _manu_yield(b, code)
                    if not flow or abs(flow) <= min_flow:
                        continue
                    _node(bid, "block", prod,
                          label=f"{prod}\nthr={b.get('throughput',0):.4f}",
                          extra={"section": b.get("section")})
                    _add_edge(bid, nid, flow, "produce")
                    _push(bid, depth + 1, "up")
            elif dir_tag == "down":
                if is_hub_down:
                    nodes[nid]["extra"]["hub_down"] = len(consumers)
                    continue
                for cons in consumers:
                    bid = f"block:{cons}"
                    b = sol.block.get(cons, {})
                    flow = (b.get("input_flows") or {}).get(code) or _manu_in(b, code)
                    if not flow or abs(flow) <= min_flow:
                        continue
                    _node(bid, "block", cons,
                          label=f"{cons}\nthr={b.get('throughput',0):.4f}",
                          extra={"section": b.get("section")})
                    _add_edge(nid, bid, flow, "consume")
                    _push(bid, depth + 1, "down")

        elif kind == "block":
            info = topo.get_block(code)
            b = sol.block.get(code, {})
            if not info:
                continue
            if dir_tag == "up":
                for mat in info.inputs:
                    mid = f"material:{mat}"
                    flow = (b.get("input_flows") or {}).get(mat) or _manu_in(b, mat)
                    if not flow or abs(flow) <= min_flow:
                        continue
                    _add_edge(mid, nid, flow, "consume")
                    _push(mid, depth + 1, "up")
            elif dir_tag == "down":
                for mat in info.outputs:
                    mid = f"material:{mat}"
                    flow = (b.get("output_flows") or {}).get(mat) or _manu_yield(b, mat)
                    if not flow or abs(flow) <= min_flow:
                        continue
                    _add_edge(nid, mid, flow, "produce")
                    _push(mid, depth + 1, "down")

    return nodes, edges_dedup


def report_r3_chain(topo_mod: Topology, mod: SolutionView,
                    entry_point: dict, direction: str,
                    max_depth: int, min_flow: float,
                    hub_cutoff_producers: int = 8,
                    hub_cutoff_consumers: int = 8) -> dict:
    """Directional BFS from the entry point through input/output graph.

    Each visited node carries a direction tag ('up'/'down'):
      - 'up'   : we got here by going upstream from the entry; only walk further up
      - 'down' : we got here by going downstream; only walk further down
    At the entry, we walk both ways (if requested).

    Returns:
        nodes: list of {id, kind, code, value, label}
        edges: list of {src, dst, flow, kind} — kind = 'consume'/'produce'/'sell'/'purc'
    """
    ep_type = entry_point.get("type")
    ep_code = entry_point.get("code")
    start_id = f"{ep_type}:{ep_code}"

    nodes = {}                       # id -> node dict
    edges_dedup = {}                 # (src, dst, kind) -> edge dict
    visited = set()                  # (id, dir) tuples
    queue = deque()

    # initial direction(s) at entry
    if direction == "upstream":
        seeds = [("up",)]
    elif direction == "downstream":
        seeds = [("down",)]
    else:
        seeds = [("up",), ("down",)]
    for (d,) in seeds:
        queue.append((start_id, 0, d))
        visited.add((start_id, d))

    def _node(id_, kind, code, label, extra=None):
        if id_ not in nodes:
            nodes[id_] = {
                "id": id_, "kind": kind, "code": code,
                "label": label, "extra": extra or {},
            }

    def _push(id_, depth, d):
        key = (id_, d)
        if key in visited:
            return
        visited.add(key)
        queue.append((id_, depth, d))

    def _add_edge(src, dst, flow, kind):
        key = (src, dst, kind)
        if key in edges_dedup:
            return
        edges_dedup[key] = {"src": src, "dst": dst, "flow": flow, "kind": kind}

    # Same BFS body as _bfs_chain; kept inline here so this function stays self-contained
    while queue:
        nid, depth, dir_tag = queue.popleft()
        if depth > max_depth:
            continue
        kind, code = nid.split(":", 1)

        if kind == "material":
            ms = mod.material.get(code, {})
            sell_val = ms.get("sell_value", 0.0)
            purc_val = ms.get("purc_value", 0.0)
            _node(nid, "material", code,
                  label=f"{code}\nsell={sell_val:.4f}, purc={purc_val:.4f}",
                  extra={"sell_value": sell_val, "purc_value": purc_val,
                         "sell_price": ms.get("sell_price"),
                         "purc_price": ms.get("purchase_price"),
                         "marginal_benefit": ms.get("marginal_benefit")})

            # SELL/PURC terminal nodes
            if dir_tag == "down" and abs(sell_val) > min_flow:
                sid = f"sell:{code}"
                _node(sid, "sell", code,
                      label=f"SELL {code}\n{sell_val:.4f} × ¥{ms.get('sell_price')}")
                _add_edge(nid, sid, sell_val, "sell")
            if dir_tag == "up" and abs(purc_val) > min_flow:
                pid = f"purc:{code}"
                _node(pid, "purc", code,
                      label=f"PURC {code}\n{purc_val:.4f} × ¥{ms.get('purchase_price')}")
                _add_edge(pid, nid, purc_val, "purc")

            # Walk further (with hub cutoff: stop expanding past materials with too many ties)
            producers = topo_mod.producers_of.get(code, []) or []
            consumers = topo_mod.consumers_of.get(code, []) or []
            is_hub_up   = len(producers) > hub_cutoff_producers
            is_hub_down = len(consumers) > hub_cutoff_consumers

            if dir_tag == "up":
                if is_hub_up:
                    # mark node as hub, but don't expand further
                    nodes[nid]["extra"]["hub_up"] = len(producers)
                    nodes[nid]["label"] += f"<br/>[hub: {len(producers)} producers, 截断]"
                    continue
                for prod in producers:
                    bid = f"block:{prod}"
                    b = mod.block.get(prod, {})
                    flow = (b.get("output_flows") or {}).get(code) or _manu_yield(b, code)
                    if not flow or abs(flow) <= min_flow:
                        continue
                    _node(bid, "block", prod,
                          label=f"{prod}\nthr={b.get('throughput',0):.4f}",
                          extra={"section": b.get("section")})
                    _add_edge(bid, nid, flow, "produce")
                    _push(bid, depth + 1, "up")
            elif dir_tag == "down":
                if is_hub_down:
                    nodes[nid]["extra"]["hub_down"] = len(consumers)
                    nodes[nid]["label"] += f"<br/>[hub: {len(consumers)} consumers, 截断]"
                    continue
                for cons in consumers:
                    bid = f"block:{cons}"
                    b = mod.block.get(cons, {})
                    flow = (b.get("input_flows") or {}).get(cons) or _manu_in(b, code)
                    if not flow or abs(flow) <= min_flow:
                        continue
                    _node(bid, "block", cons,
                          label=f"{cons}\nthr={b.get('throughput',0):.4f}",
                          extra={"section": b.get("section")})
                    _add_edge(nid, bid, flow, "consume")
                    _push(bid, depth + 1, "down")

        elif kind == "block":
            info = topo_mod.get_block(code)
            b = mod.block.get(code, {})
            if not info:
                continue
            if dir_tag == "up":
                # to its inputs (further upstream)
                for mat in info.inputs:
                    mid = f"material:{mat}"
                    flow = (b.get("input_flows") or {}).get(mat) or _manu_in(b, mat)
                    if not flow or abs(flow) <= min_flow:
                        continue
                    _add_edge(mid, nid, flow, "consume")
                    _push(mid, depth + 1, "up")
            elif dir_tag == "down":
                # to its outputs (further downstream)
                for mat in info.outputs:
                    mid = f"material:{mat}"
                    flow = (b.get("output_flows") or {}).get(mat) or _manu_yield(b, mat)
                    if not flow or abs(flow) <= min_flow:
                        continue
                    _add_edge(nid, mid, flow, "produce")
                    _push(mid, depth + 1, "down")

    return {
        "entry":  start_id,
        "nodes":  list(nodes.values()),
        "edges":  list(edges_dedup.values()),
    }


def _manu_yield(block_sol: dict, material_code: str):
    """For manuBlock, sum over active cases: throughput * yield_ratio."""
    total = 0.0
    found = False
    for c in block_sol.get("cases", []):
        for r in c.get("material_ratio", []) or []:
            if r.get("type") == "out" and r.get("material") == material_code:
                total += float(c.get("value", 0.0) or 0.0) * float(r.get("ratio") or 0.0)
                found = True
    return total if found else None


def _manu_in(block_sol: dict, material_code: str):
    total = 0.0
    found = False
    for c in block_sol.get("cases", []):
        for r in c.get("material_ratio", []) or []:
            if r.get("type") == "in" and r.get("material") == material_code:
                total += float(c.get("value", 0.0) or 0.0) * float(r.get("ratio") or 0.0)
                found = True
    return total if found else None


def _get_flow(sol_view, edge_kind: str, src_id: str, dst_id: str):
    """Look up flow value for an edge in a given solution view.

    edge_kind tells us which side is the block:
        'produce'  : block → material  (src=block, dst=material)
        'consume'  : material → block  (src=material, dst=block)
        'sell'     : material → SELL terminal
        'purc'     : PURC terminal → material
    """
    if edge_kind == "sell":
        code = src_id.split(":", 1)[1]
        return float((sol_view.material.get(code) or {}).get("sell_value", 0.0) or 0.0)
    if edge_kind == "purc":
        code = dst_id.split(":", 1)[1]
        return float((sol_view.material.get(code) or {}).get("purc_value", 0.0) or 0.0)

    # block ↔ material; figure out which side is which
    if src_id.startswith("block:"):
        block_code = src_id.split(":", 1)[1]
        mat_code   = dst_id.split(":", 1)[1]
        which = "output"
    else:
        block_code = dst_id.split(":", 1)[1]
        mat_code   = src_id.split(":", 1)[1]
        which = "input"
    b = sol_view.block.get(block_code, {}) or {}
    table = b.get(f"{which}_flows", {}) or {}
    if mat_code in table:
        return float(table[mat_code] or 0.0)
    # fall back to manuBlock yield computation if block isn't a spliter/blender/etc.
    fn = _manu_yield if which == "output" else _manu_in
    v = fn(b, mat_code)
    return float(v) if v else 0.0


# ---------- R3d: delta chain (only nodes whose STATE changed)
def report_r3d_delta_chain(chain: dict, base: SolutionView, mod: SolutionView,
                           min_abs_delta: float = 1.0,
                           min_rel_delta: float = 0.5,
                           require_state_change: bool = True,
                           on_off_only: bool = False,
                           topo_mod: Topology = None,
                           extend_from_activated: bool = True,
                           extension_max_depth: int = 6,
                           extension_min_flow: float = 1e-6,
                           hub_cutoff_producers: int = 8,
                           hub_cutoff_consumers: int = 8) -> dict:
    """Filter chain to nodes where state CHANGED between base and mod.

    A node's "state" is:
        material : (sell_value, purc_value)
        block    : throughput
        sell/purc: terminal sink/source amount

    A node is *significant* if at least one of these holds:
        (a) ON/OFF transition: base ≈ 0 and mod > epsilon (or vice versa)
        (b) |Δ| ≥ min_abs_delta
        (c) |Δ| / max(|base|, |mod|) ≥ min_rel_delta

    Edges are kept only if BOTH endpoints are significant (when require_state_change=True),
    or if just the edge's flow change is significant (else).
    """
    eps = 1e-6

    def _block_change(code):
        a = float((base.block.get(code) or {}).get("throughput", 0.0) or 0.0)
        b = float((mod.block.get(code)  or {}).get("throughput", 0.0) or 0.0)
        d = b - a
        on_off = (abs(a) < eps and abs(b) > eps) or (abs(a) > eps and abs(b) < eps)
        if on_off_only:
            return on_off, a, b
        big_abs = abs(d) >= min_abs_delta
        big_rel = (abs(d) / (max(abs(a), abs(b)) or 1.0)) >= min_rel_delta
        return (on_off or big_abs or big_rel), a, b

    def _terminal_change(kind, code):
        if kind == "sell":
            a = float((base.material.get(code) or {}).get("sell_value", 0.0) or 0.0)
            b = float((mod.material.get(code)  or {}).get("sell_value", 0.0) or 0.0)
        elif kind == "purc":
            a = float((base.material.get(code) or {}).get("purc_value", 0.0) or 0.0)
            b = float((mod.material.get(code)  or {}).get("purc_value", 0.0) or 0.0)
        else:
            return False, 0.0, 0.0
        d = b - a
        on_off = (abs(a) < eps and abs(b) > eps) or (abs(a) > eps and abs(b) < eps)
        big_abs = abs(d) >= min_abs_delta
        big_rel = (abs(d) / (max(abs(a), abs(b)) or 1.0)) >= min_rel_delta
        return (on_off or big_abs or big_rel), a, b

    # ── algorithm ──
    # 1. Identify "core" nodes:
    #    - blocks with significant throughput change
    #    - SELL/PURC terminals with significant value change
    core = set()
    block_state = {}     # block_id -> (base_thr, mod_thr)
    term_state  = {}     # term_id  -> (base_val, mod_val)
    for n in chain["nodes"]:
        if n["kind"] == "block":
            sig, a, b = _block_change(n["code"])
            block_state[n["id"]] = (a, b)
            if sig:
                core.add(n["id"])
        elif n["kind"] in ("sell", "purc"):
            sig, a, b = _terminal_change(n["kind"], n["code"])
            term_state[n["id"]] = (a, b)
            if sig:
                core.add(n["id"])

    # 2. Build edge flows; keep edges where either endpoint is core
    #    OR where the edge itself has significant flow change
    edge_records = []
    for e in chain["edges"]:
        bf = _get_flow(base, e["kind"], e["src"], e["dst"])
        mf = _get_flow(mod,  e["kind"], e["src"], e["dst"])
        d  = mf - bf
        denom = max(abs(bf), abs(mf)) or 1.0
        edge_sig = (abs(d) >= min_abs_delta or abs(d) / denom >= min_rel_delta)
        edge_records.append({
            **e, "base_flow": bf, "mod_flow": mf, "delta_flow": d,
            "flow": mf, "edge_sig": edge_sig,
        })

    if require_state_change:
        # An edge is kept if at least one endpoint is core AND flow changed significantly
        kept_edges = [
            e for e in edge_records
            if (e["src"] in core or e["dst"] in core) and e["edge_sig"]
        ]
    else:
        kept_edges = [e for e in edge_records if e["edge_sig"]]

    # 3. Materials are kept implicitly because they touch a kept edge
    touched = {e["src"] for e in kept_edges} | {e["dst"] for e in kept_edges}
    kept_nodes = [n for n in chain["nodes"] if n["id"] in touched]
    kept_nodes_by_id = {n["id"]: n for n in kept_nodes}

    # 4. For each activated block in the kept set, BFS downstream (and upstream for OFF blocks)
    #    to discover SELL terminals and feed sources beyond the original entry-point BFS.
    if extend_from_activated and topo_mod is not None:
        for n in list(kept_nodes):
            if n["kind"] != "block":
                continue
            code = n["code"]
            base_thr, mod_thr = block_state.get(n["id"], (0.0, 0.0))
            went_on  = abs(base_thr) < eps and abs(mod_thr) > eps
            went_off = abs(base_thr) > eps and abs(mod_thr) < eps
            if not (went_on or went_off):
                continue

            ext_nodes, ext_edges = {}, {}
            # Only walk DOWN from the activated block — the entry-point BFS already
            # covered the upstream direction. Walking up here would re-explore
            # everything and explode the graph.
            view = mod if went_on else base
            _bfs_chain(n["id"], "downstream", topo_mod, view,
                       max_depth=extension_max_depth,
                       min_flow=extension_min_flow,
                       hub_cutoff_producers=hub_cutoff_producers,
                       hub_cutoff_consumers=hub_cutoff_consumers,
                       existing_nodes=ext_nodes, existing_edges=ext_edges)

            # Merge extension into the main chain.
            # Keep only edges where the FLOW changed significantly.
            # (Even SELL/PURC edges must have changed; we don't want to drag in unchanged
            # downstream products just because they share a block.)
            existing_edge_keys = {(ee["src"], ee["dst"], ee["kind"]) for ee in kept_edges}
            for eid, e in ext_edges.items():
                if eid in existing_edge_keys:
                    continue
                src, dst = e["src"], e["dst"]
                bf = _get_flow(base, e["kind"], src, dst)
                mf = _get_flow(mod,  e["kind"], src, dst)
                d  = mf - bf
                denom = max(abs(bf), abs(mf)) or 1.0
                ed_sig = (abs(d) >= min_abs_delta) or (abs(d) / denom >= min_rel_delta)
                if not ed_sig:
                    continue
                kept_edges.append({**e, "base_flow": bf, "mod_flow": mf,
                                   "delta_flow": d, "flow": mf, "edge_sig": True})
                touched.add(src); touched.add(dst)
            for nid, nn in ext_nodes.items():
                if nid in kept_nodes_by_id:
                    continue
                if nid in touched:
                    kept_nodes.append(nn)
                    kept_nodes_by_id[nid] = nn
    # Relabel kept nodes with before/after state for the narrative
    def _fmt_money(v):
        s = "+" if v >= 0 else "-"
        v = abs(v)
        if v >= 1e6: return f"{s}¥{v/1e6:.2f}M"
        if v >= 1e3: return f"{s}¥{v/1e3:.1f}K"
        if v >= 1:   return f"{s}¥{v:.0f}"
        return f"{s}¥{v:.2f}"

    for n in kept_nodes:
        code = n["code"]
        if n["kind"] == "block":
            a, b = block_state.get(n["id"], (
                float((base.block.get(code) or {}).get("throughput", 0.0) or 0.0),
                float((mod.block.get(code)  or {}).get("throughput", 0.0) or 0.0),
            ))
            n["label"] = f"{code}<br/>thr {a:.3g} → {b:.3g}"
        elif n["kind"] == "material":
            bm = base.material.get(code, {}) or {}
            mm = mod.material.get(code, {}) or {}
            bs = float(bm.get("sell_value", 0.0) or 0.0); ms = float(mm.get("sell_value", 0.0) or 0.0)
            bp = float(bm.get("purc_value", 0.0) or 0.0); mp = float(mm.get("purc_value", 0.0) or 0.0)
            parts = [code]
            if bs > 1e-9 or ms > 1e-9: parts.append(f"sell {bs:.3g}→{ms:.3g}")
            if bp > 1e-9 or mp > 1e-9: parts.append(f"purc {bp:.3g}→{mp:.3g}")
            n["label"] = "<br/>".join(parts)
        elif n["kind"] == "sell":
            # Rich SELL terminal label: base_qty → mod_qty × ¥price = Δ OBJ
            mm = mod.material.get(code, {}) or {}
            bm = base.material.get(code, {}) or {}
            price = mm.get("sell_price") or bm.get("sell_price") or 0.0
            bq = float(bm.get("sell_value", 0.0) or 0.0)
            mq = float(mm.get("sell_value", 0.0) or 0.0)
            delta = (mq - bq) * float(price)
            n["label"] = (
                f"SELL {code}<br/>"
                f"{bq:.3g} → {mq:.3g}<br/>"
                f"× ¥{price:g} = {_fmt_money(delta)}"
            )
        elif n["kind"] == "purc":
            mm = mod.material.get(code, {}) or {}
            bm = base.material.get(code, {}) or {}
            price = mm.get("purchase_price") or bm.get("purchase_price") or 0.0
            bq = float(bm.get("purc_value", 0.0) or 0.0)
            mq = float(mm.get("purc_value", 0.0) or 0.0)
            delta = -(mq - bq) * float(price)   # PURC contributes negatively
            n["label"] = (
                f"PURC {code}<br/>"
                f"{bq:.3g} → {mq:.3g}<br/>"
                f"× ¥{price:g} = {_fmt_money(delta)}"
            )

    return {
        "entry":  chain["entry"],
        "nodes":  kept_nodes,
        "edges":  kept_edges,
        "min_abs_delta": min_abs_delta,
        "min_rel_delta": min_rel_delta,
        "n_filtered_edges": len(chain["edges"]) - len(kept_edges),
        "n_filtered_nodes": len(chain["nodes"]) - len(kept_nodes),
    }


# ---------- R3d minimal (trigger-via path-based, value-chain extraction)
def report_r3d_path_minimal(topo_mod: Topology, base: SolutionView, mod: SolutionView,
                            entry_point: dict,
                            top_n_terminals: int = 12,
                            min_abs_delta_obj: float = 100.0,
                            min_rel_flow_change: float = 0.10,
                            min_abs_flow_change: float = 1e-6,
                            include_entry_terminal: bool = True,
                            trigger_min_throughput_change: float = 0.10,
                            up_producer_hub_cutoff: int = 3) -> dict:
    """Extract the minimal value chain via the *trigger-block* pattern:

      1. BFS UPSTREAM from entry through edges with significant flow change →
         collect every state-changed manuBlock reached (these are 'triggers').
      2. For each trigger, BFS DOWNSTREAM (through changed edges) → find which
         significant SELL/PURC terminals it can reach.
      3. For each terminal, pick the trigger giving the shortest combined path
         entry → trigger → terminal. This guarantees the chain represents the
         REAL causal story: every product change is shown as flowing from the
         activated manu block, not via some unrelated short-cut.

    Falls back to plain shortest-path for the entry's own SELL/PURC.
    """
    # ----- 1. Significant terminals
    terminals = []
    for code, m in mod.material.items():
        bm = base.material.get(code, {}) or {}
        sp = m.get("sell_price") or bm.get("sell_price")
        if sp is not None:
            vb = float(bm.get("sell_value", 0.0) or 0.0)
            vm = float(m.get("sell_value", 0.0) or 0.0)
            d  = (vm - vb) * float(sp)
            if abs(d) >= min_abs_delta_obj:
                terminals.append({"kind": "sell", "code": code, "delta_obj": d,
                                  "base_qty": vb, "mod_qty": vm, "price": float(sp)})
        pp = m.get("purchase_price") or bm.get("purchase_price")
        if pp is not None:
            vb = float(bm.get("purc_value", 0.0) or 0.0)
            vm = float(m.get("purc_value", 0.0) or 0.0)
            d  = -(vm - vb) * float(pp)
            if abs(d) >= min_abs_delta_obj:
                terminals.append({"kind": "purc", "code": code, "delta_obj": d,
                                  "base_qty": vb, "mod_qty": vm, "price": float(pp)})
    terminals.sort(key=lambda x: -abs(x["delta_obj"]))
    terminals = terminals[:top_n_terminals]

    ep_code = entry_point.get("code")
    if include_entry_terminal and ep_code:
        em = mod.material.get(ep_code, {}) or {}; ebm = base.material.get(ep_code, {}) or {}
        sp = em.get("sell_price") or ebm.get("sell_price")
        if sp is not None and not any(t["kind"] == "sell" and t["code"] == ep_code for t in terminals):
            vb = float(ebm.get("sell_value", 0.0) or 0.0); vm = float(em.get("sell_value", 0.0) or 0.0)
            terminals.append({"kind": "sell", "code": ep_code, "delta_obj": (vm - vb) * float(sp),
                              "base_qty": vb, "mod_qty": vm, "price": float(sp)})

    # ----- 2. Build DIRECTIONAL topology graph
    #   fwd[X] = downstream nodes (X produces / is consumed by)
    #   bwd[X] = upstream nodes (X is produced by / consumes)
    fwd = defaultdict(list); bwd = defaultdict(list)
    edge_meta = {}  # (src, dst) -> {kind, base, mod, delta}

    def _add_edge(src, dst, kind, fb, fm):
        if (src, dst) in edge_meta:
            if abs(edge_meta[(src, dst)]["delta"]) < abs(fm - fb):
                edge_meta[(src, dst)] = {"kind": kind, "base": fb, "mod": fm, "delta": fm - fb}
        else:
            edge_meta[(src, dst)] = {"kind": kind, "base": fb, "mod": fm, "delta": fm - fb}
            fwd[src].append(dst); bwd[dst].append(src)

    for blk_code, info in topo_mod.blocks.items():
        b_id = f"block:{blk_code}"
        base_blk = base.block.get(blk_code, {}); mod_blk = mod.block.get(blk_code, {})
        # consume edge: material -> block
        for mat in info.inputs:
            m_id = f"material:{mat}"
            fb = (base_blk.get("input_flows") or {}).get(mat) or _manu_in(base_blk, mat) or 0.0
            fm = (mod_blk.get("input_flows")  or {}).get(mat) or _manu_in(mod_blk,  mat) or 0.0
            _add_edge(m_id, b_id, "consume", float(fb), float(fm))
        # produce edge: block -> material
        for mat in info.outputs:
            m_id = f"material:{mat}"
            fb = (base_blk.get("output_flows") or {}).get(mat) or _manu_yield(base_blk, mat) or 0.0
            fm = (mod_blk.get("output_flows")  or {}).get(mat) or _manu_yield(mod_blk,  mat) or 0.0
            _add_edge(b_id, m_id, "produce", float(fb), float(fm))

    # SELL/PURC terminal edges (directional)
    for code, m in mod.material.items():
        bm = base.material.get(code, {}) or {}
        sv_b = float(bm.get("sell_value", 0.0) or 0.0); sv_m = float(m.get("sell_value", 0.0) or 0.0)
        if abs(sv_b) > 1e-12 or abs(sv_m) > 1e-12:
            _add_edge(f"material:{code}", f"sell:{code}", "sell", sv_b, sv_m)
        pv_b = float(bm.get("purc_value", 0.0) or 0.0); pv_m = float(m.get("purc_value", 0.0) or 0.0)
        if abs(pv_b) > 1e-12 or abs(pv_m) > 1e-12:
            _add_edge(f"purc:{code}", f"material:{code}", "purc", pv_b, pv_m)

    def _edge_active(u, v):
        em = edge_meta.get((u, v))
        if not em: return False
        d = abs(em["delta"])
        if d < min_abs_flow_change: return False
        denom = max(abs(em["base"]), abs(em["mod"]), 1e-12)
        return (d / denom) >= min_rel_flow_change

    def _is_trigger_manu(block_id: str) -> bool:
        if not block_id.startswith("block:"): return False
        code = block_id.split(":", 1)[1]
        section = (mod.block.get(code) or {}).get("section", "")
        if section != "manuBlock": return False
        tb = float((base.block.get(code) or {}).get("throughput", 0.0) or 0.0)
        tm = float((mod.block.get(code) or {}).get("throughput", 0.0) or 0.0)
        if abs(tm - tb) < 1e-9: return False
        denom = max(abs(tb), abs(tm), 1e-12)
        return abs(tm - tb) / denom >= trigger_min_throughput_change

    ep_type = entry_point.get("type", "material")
    entry_id = f"{ep_type}:{ep_code}"

    # ----- 3. BFS UP from entry → collect "triggers" (state-changed manuBlocks)
    #         IMPORTANT: hub-cutoff on material producers. Materials with many
    #         producers (fuel-gas pools, H2S, regen loops, etc.) are "shared pools"
    #         — walking up through their producers pollutes the trigger set with
    #         unrelated chains. So we VISIT a hub material (it stays in chain) but
    #         DON'T enumerate its producers.
    def _is_producer_hub(mat_id):
        if not mat_id.startswith("material:"): return False
        code = mat_id.split(":", 1)[1]
        return len(topo_mod.producers_of.get(code, [])) > up_producer_hub_cutoff

    up_parents = {entry_id: None}
    triggers   = []
    # If the entry node itself is a state-changed manuBlock (e.g., user toggled
    # _status of a block directly), it IS the trigger.
    if _is_trigger_manu(entry_id):
        triggers.append(entry_id)
    q = deque([entry_id])
    while q:
        u = q.popleft()
        if _is_producer_hub(u):
            continue   # hub material: visited but don't walk to its many producers
        for v in bwd.get(u, []):                  # walk UP: predecessors
            if v in up_parents: continue
            if not _edge_active(v, u): continue
            up_parents[v] = u
            q.append(v)
            if _is_trigger_manu(v):
                triggers.append(v)

    # Sort triggers by upstream distance from entry (closer first)
    def _up_dist(node):
        d = 0; cur = node
        while up_parents.get(cur) is not None:
            cur = up_parents[cur]; d += 1
        return d
    triggers.sort(key=_up_dist)

    # ----- 4. For each trigger, BFS DOWN through active edges → cache reachability
    down_parents = {}    # trigger_id -> {node: parent}
    for t in triggers:
        dp = {t: None}
        q = deque([t])
        while q:
            u = q.popleft()
            for v in fwd.get(u, []):              # walk DOWN: successors
                if v in dp: continue
                if not _edge_active(u, v): continue
                dp[v] = u
                q.append(v)
        down_parents[t] = dp

    # Also: cache entry's direct downstream (for entry's own SELL/PURC)
    entry_down = {entry_id: None}
    q = deque([entry_id])
    while q:
        u = q.popleft()
        for v in fwd.get(u, []):
            if v in entry_down: continue
            if not _edge_active(u, v): continue
            entry_down[v] = u
            q.append(v)

    def _reconstruct(parents, node):
        path = []; cur = node
        while cur is not None:
            path.append(cur); cur = parents[cur]
        return list(reversed(path))

    # ----- 5. For each terminal, find via-trigger path (shortest combined)
    kept_nodes = {entry_id}; kept_edges_set = set()
    reached, unreached = [], []
    used_triggers = set()
    for t in terminals:
        # SELL: walk down to sell:{code}, normal path.
        # PURC: purc:{code} is a SOURCE (purc -> material edge); material:{code}
        #       is a pure input with no producer, so down-BFS can never reach it.
        #       Instead, target one of the BLOCKS that consume the material, then
        #       graft purc:{code} -> material:{code} -> that block onto the chain.
        if t["kind"] == "sell":
            target = f"sell:{t['code']}"
            graft_purc = None
        else:
            consumer_blocks = [f"block:{c}" for c in topo_mod.consumers_of.get(t["code"], [])]
            target = None  # found per-trigger below
            graft_purc = t["code"]

        direct_path = None
        best_via = None; best_trigger = None

        if t["kind"] == "sell":
            direct_path = _reconstruct(entry_down, target) if target in entry_down else None
            for trig in triggers:
                if target not in down_parents[trig]: continue
                if trig not in up_parents:           continue
                p_up   = _reconstruct(up_parents, trig)
                p_down = _reconstruct(down_parents[trig], target)
                total  = p_up + p_down[1:]
                if best_via is None or len(total) < len(best_via):
                    best_via = total; best_trigger = trig
        else:  # PURC: probe each consumer block
            for trig in triggers:
                if trig not in up_parents: continue
                p_up = _reconstruct(up_parents, trig)
                for cb in consumer_blocks:
                    if cb not in down_parents[trig]: continue
                    p_down = _reconstruct(down_parents[trig], cb)
                    total  = p_up + p_down[1:]
                    if best_via is None or len(total) < len(best_via):
                        best_via = total; best_trigger = trig
            # Direct path: maybe a consumer block is downstream of entry directly
            for cb in consumer_blocks:
                if cb in entry_down:
                    cand = _reconstruct(entry_down, cb)
                    if direct_path is None or len(cand) < len(direct_path):
                        direct_path = cand

        # Prefer the via-trigger path
        if best_via is not None:
            chosen, trig_used = best_via, best_trigger
        elif direct_path is not None:
            chosen, trig_used = direct_path, None
        else:
            unreached.append(t); continue

        reached.append({**t, "via_trigger": trig_used, "path_len": len(chosen) - 1})
        if trig_used is not None:
            used_triggers.add(trig_used)
        for u in chosen: kept_nodes.add(u)
        for i in range(len(chosen) - 1):
            kept_edges_set.add((chosen[i], chosen[i+1]))

        # Graft the purc terminal: purc:CODE -> material:CODE -> consumer_block (last node in chosen)
        if graft_purc is not None:
            mat_id  = f"material:{graft_purc}"
            purc_id = f"purc:{graft_purc}"
            consumer_block = chosen[-1]
            kept_nodes.add(mat_id); kept_nodes.add(purc_id)
            kept_edges_set.add((mat_id, consumer_block))
            kept_edges_set.add((purc_id, mat_id))

    # Trim triggers to only those actually used in a kept path
    triggers = sorted(used_triggers, key=_up_dist)

    # ----- 6. Reconstruct edge dicts. path[i] -> path[i+1] from BFS reconstruction
    #         may be either the topology direction (when walking DOWN from trigger /
    #         entry) or the reverse (when walking UP from entry to trigger). edge_meta
    #         only has the topology direction, so we try both and use whichever exists.
    kept_edges = []
    for (a, b) in kept_edges_set:
        if (a, b) in edge_meta:
            src, dst = a, b
        elif (b, a) in edge_meta:
            src, dst = b, a
        else:
            continue   # shouldn't happen
        em = edge_meta[(src, dst)]
        kept_edges.append({"src": src, "dst": dst, "kind": em["kind"],
                           "flow": em["mod"], "base_flow": em["base"],
                           "mod_flow": em["mod"], "delta_flow": em["delta"]})

    # 6. Rich node labels
    def _money(v):
        s = "+" if v >= 0 else "-"; v = abs(v)
        if v >= 1e6: return f"{s}¥{v/1e6:.2f}M"
        if v >= 1e3: return f"{s}¥{v/1e3:.1f}K"
        if v >= 1:   return f"{s}¥{v:.0f}"
        return f"{s}¥{v:.2f}"

    nodes = []
    for nid in kept_nodes:
        kind, code = nid.split(":", 1)
        if kind == "material":
            bm = base.material.get(code, {}) or {}; mm = mod.material.get(code, {}) or {}
            bs = float(bm.get("sell_value", 0.0) or 0.0); ms = float(mm.get("sell_value", 0.0) or 0.0)
            bp = float(bm.get("purc_value", 0.0) or 0.0); mp = float(mm.get("purc_value", 0.0) or 0.0)
            parts = [code]
            if bs > 1e-9 or ms > 1e-9: parts.append(f"sell {bs:.3g}→{ms:.3g}")
            if bp > 1e-9 or mp > 1e-9: parts.append(f"purc {bp:.3g}→{mp:.3g}")
            label = "<br/>".join(parts)
        elif kind == "block":
            tb = float((base.block.get(code) or {}).get("throughput", 0.0) or 0.0)
            tm = float((mod.block.get(code)  or {}).get("throughput", 0.0) or 0.0)
            section = (mod.block.get(code) or base.block.get(code) or {}).get("section", "")
            label = f"{code}<br/>{section}<br/>thr {tb:.3g} → {tm:.3g}"
        elif kind == "sell":
            mm = mod.material.get(code, {}) or {}; bm = base.material.get(code, {}) or {}
            price = mm.get("sell_price") or bm.get("sell_price") or 0.0
            bq = float(bm.get("sell_value", 0.0) or 0.0); mq = float(mm.get("sell_value", 0.0) or 0.0)
            d = (mq - bq) * float(price)
            label = f"SELL {code}<br/>{bq:.3g} → {mq:.3g}<br/>× ¥{price:g} = {_money(d)}"
        else:
            mm = mod.material.get(code, {}) or {}; bm = base.material.get(code, {}) or {}
            price = mm.get("purchase_price") or bm.get("purchase_price") or 0.0
            bq = float(bm.get("purc_value", 0.0) or 0.0); mq = float(mm.get("purc_value", 0.0) or 0.0)
            d = -(mq - bq) * float(price)
            label = f"PURC {code}<br/>{bq:.3g} → {mq:.3g}<br/>× ¥{price:g} = {_money(d)}"
        nodes.append({"id": nid, "kind": kind, "code": code, "label": label})

    return {
        "entry":   entry_id,
        "nodes":   nodes,
        "edges":   kept_edges,
        "triggers": triggers,
        "terminals_considered": terminals,
        "terminals_reached":    reached,
        "terminals_unreached":  unreached,
        "filter": {
            "top_n_terminals":     top_n_terminals,
            "min_abs_delta_obj":   min_abs_delta_obj,
            "min_rel_flow_change": min_rel_flow_change,
            "min_abs_flow_change": min_abs_flow_change,
            "trigger_min_throughput_change": trigger_min_throughput_change,
        },
    }


# ---------------------------------------------------- R5: block activation diff
def report_r5_block_activation(base: SolutionView, mod: SolutionView,
                               topo_mod: Topology) -> dict:
    """Find blocks/capacity items whose value changed between base and mod."""
    rows = []
    all_blocks = set(base.block) | set(mod.block)
    for code in all_blocks:
        b = base.block.get(code, {})
        m = mod.block.get(code, {})
        t_b = float(b.get("throughput", 0.0) or 0.0)
        t_m = float(m.get("throughput", 0.0) or 0.0)
        if abs(t_b - t_m) <= 1e-6:
            continue
        info = topo_mod.get_block(code)
        rows.append({
            "code":  code,
            "section": (info.section if info else m.get("section") or b.get("section") or "?"),
            "base":  t_b,
            "mod":   t_m,
            "delta": t_m - t_b,
            "transition": ("ON" if t_b == 0 and t_m > 0 else
                           "OFF" if t_b > 0 and t_m == 0 else
                           "Δ"),
        })
    rows.sort(key=lambda r: -abs(r["delta"]))
    return {"rows": rows}


# ---------------------------------------------------- R7: shadow prices
def report_r7_shadow_prices(mod: SolutionView, topo_mod: Topology,
                            top_n: int = 30) -> dict:
    """Binding constraints in modified solution, grouped by kind.

    Returns a dict with sub-tables:
        cap     : capacity constraints with shadow price ≠ 0 (CAP 绑死)
        sell_ub : sell.ub constraints binding
        purc_ub : purchase.ub constraints binding
        wbal    : material balance duals (informational; these are the market values
                  of each material — often huge but they're not "binding" decisions)
    """
    cap_rows, sell_rows, purc_rows, wbal_rows = [], [], [], []

    for code, m in mod.material.items():
        if m.get("sell_mb") and abs(m["sell_mb"]) > 1e-6:
            sell_rows.append({
                "kind": "SELL.ub", "code": code,
                "mb": m["sell_mb"],
                "value": m.get("sell_value"),
                "ub": m.get("sell_ub"),
            })
        if m.get("purc_mb") and abs(m["purc_mb"]) > 1e-6:
            purc_rows.append({
                "kind": "PURC.ub", "code": code,
                "mb": m["purc_mb"],
                "value": m.get("purc_value"),
                "ub": m.get("purchase_ub"),
            })
        if m.get("marginal_benefit") and abs(m["marginal_benefit"]) > 1e-6:
            wbal_rows.append({
                "kind": "WBAL", "code": code,
                "mb": m["marginal_benefit"],
            })

    for code, b in mod.block.items():
        for cap in b.get("capacities", []):
            if cap.get("mb") and abs(cap["mb"]) > 1e-6:
                cap_rows.append({
                    "kind": "CAP", "code": f"{code}/{cap['code']}",
                    "mb":    cap["mb"],
                    "value": cap.get("value"),
                    "ub":    cap.get("ub"),
                    "lb":    cap.get("lb"),
                })

    for rows in (cap_rows, sell_rows, purc_rows, wbal_rows):
        rows.sort(key=lambda x: -abs(x["mb"]))

    return {
        "cap":     cap_rows[:top_n],
        "sell_ub": sell_rows[:top_n],
        "purc_ub": purc_rows[:top_n],
        "wbal":    wbal_rows[:top_n],
        "total": {
            "cap":     len(cap_rows),
            "sell_ub": len(sell_rows),
            "purc_ub": len(purc_rows),
            "wbal":    len(wbal_rows),
        },
    }


# ---------------------------------------------------- R8: block-level OBJ attribution
def report_r8_block_obj(base: SolutionView, mod: SolutionView,
                        topo_mod: Topology) -> dict:
    """For each block, OBJ contribution = Σ(product · sell_price) − Σ(feed · purchase_price)
    Only counts direct SELL revenue from a block's outputs and PURC costs of its inputs."""
    def attribute(view: SolutionView, topo: Topology):
        agg = {}
        for code, b in view.block.items():
            info = topo.get_block(code)
            if not info:
                continue
            in_cost = 0.0
            out_rev = 0.0

            # Outputs: only count if material has sell_price AND is the *value*
            # we keep this purely directional (does NOT account for downstream resale)
            for mat in info.outputs:
                ms = view.material.get(mat, {})
                if ms.get("sell_price") is None:
                    continue
                # use flow through this block's output_flows or manuBlock yield
                flow = (b.get("output_flows", {}) or {}).get(mat) or _manu_yield(b, mat) or 0.0
                # sell_value is for the material as a whole; not per-block. Skip.
                # heuristic: count product flow × sell_price; this only matches reality
                # when sell_value == flow (single producer & terminal). Otherwise just
                # informative.
                # We avoid this and use sell_value × price for the *material* split
                # below, attributing the SELL revenue back to the sole producer.

            # Cleaner: attribute material's SELL revenue proportionally to producers
            # by their output share.
            agg[code] = {"section": info.section, "throughput": b.get("throughput", 0.0)}
        # OBJ_revenue per material → split across its producers by share
        for mat_code, ms in view.material.items():
            sell_price = ms.get("sell_price")
            if sell_price is None: continue
            qty = ms.get("sell_value", 0.0)
            if qty <= 1e-9: continue
            producers = topo.producers_of.get(mat_code, [])
            if not producers: continue
            shares = []
            for pc in producers:
                b = view.block.get(pc, {})
                f = (b.get("output_flows", {}) or {}).get(mat_code) or _manu_yield(b, mat_code) or 0.0
                shares.append((pc, f))
            tot = sum(s for _, s in shares)
            if tot <= 0: continue
            for pc, f in shares:
                if pc not in agg: continue
                agg[pc]["revenue"] = agg[pc].get("revenue", 0.0) + sell_price * qty * (f / tot)
        # purchase cost per material → split across consumers by share
        for mat_code, ms in view.material.items():
            purc_price = ms.get("purchase_price")
            if purc_price is None: continue
            qty = ms.get("purc_value", 0.0)
            if qty <= 1e-9: continue
            consumers = topo.consumers_of.get(mat_code, [])
            if not consumers: continue
            shares = []
            for cc in consumers:
                b = view.block.get(cc, {})
                f = (b.get("input_flows", {}) or {}).get(mat_code) or _manu_in(b, mat_code) or 0.0
                shares.append((cc, f))
            tot = sum(s for _, s in shares)
            if tot <= 0: continue
            for cc, f in shares:
                if cc not in agg: continue
                agg[cc]["cost"] = agg[cc].get("cost", 0.0) + purc_price * qty * (f / tot)
        for d in agg.values():
            d["net"] = d.get("revenue", 0.0) - d.get("cost", 0.0)
        return agg

    base_agg = attribute(base, topo_mod)   # use mod topology (assumed same)
    mod_agg  = attribute(mod,  topo_mod)
    rows = []
    for code in set(base_agg) | set(mod_agg):
        b = base_agg.get(code, {})
        m = mod_agg.get(code,  {})
        rows.append({
            "code":  code,
            "section": (b.get("section") or m.get("section")),
            "base_revenue": b.get("revenue", 0.0),
            "mod_revenue":  m.get("revenue", 0.0),
            "base_cost":    b.get("cost", 0.0),
            "mod_cost":     m.get("cost", 0.0),
            "base_net":     b.get("net", 0.0),
            "mod_net":      m.get("net", 0.0),
            "delta_net":    m.get("net", 0.0) - b.get("net", 0.0),
        })
    rows.sort(key=lambda r: -abs(r["delta_net"]))
    return {"rows": rows}


# ---------------------------------------------------- R9: critical path
def report_r9_critical_path(chain: dict, base: SolutionView, mod: SolutionView,
                            threshold_pct: float) -> dict:
    """Highlight the subset of chain that explains ≥ threshold_pct of Δ OBJ.

    Strategy: rank edges by their associated "Δ contribution" — for SELL/PURC
    edges that's direct; for other edges, propagate downstream from the entry
    point and weight by the SELL revenue delta they eventually feed.

    Simple heuristic: collect all SELL-edge endpoints, compute |delta_obj|;
    the path from each high-delta SELL back to the entry is "critical".
    """
    delta_total = sum(  # use the rows from R2
        (mod.material.get(n["code"], {}).get("sell_value", 0.0)
         - base.material.get(n["code"], {}).get("sell_value", 0.0))
        * (mod.material.get(n["code"], {}).get("sell_price") or 0.0)
        for n in chain["nodes"] if n["kind"] == "material"
    )
    threshold_abs = abs(delta_total) * threshold_pct / 100.0

    # Walk from each SELL node backwards through edges to find ancestor blocks/materials.
    # Build reverse adjacency
    rev = defaultdict(list)
    for e in chain["edges"]:
        rev[e["dst"]].append(e)

    critical_nodes = set()
    critical_edges = []
    for n in chain["nodes"]:
        if n["kind"] != "sell":
            continue
        mat = n["code"]
        s_price = mod.material.get(mat, {}).get("sell_price") or 0.0
        d = (mod.material.get(mat, {}).get("sell_value", 0.0)
             - base.material.get(mat, {}).get("sell_value", 0.0)) * s_price
        if abs(d) < threshold_abs:
            continue
        # BFS backward
        q = deque([n["id"]])
        while q:
            cur = q.popleft()
            if cur in critical_nodes:
                continue
            critical_nodes.add(cur)
            for e in rev.get(cur, []):
                critical_edges.append(e)
                q.append(e["src"])

    return {
        "delta_total":   delta_total,
        "threshold_pct": threshold_pct,
        "critical_nodes": list(critical_nodes),
        "critical_edges": critical_edges,
    }


# ---------------------------------------------------- R11: property-limit binding
def report_r11_property_limits(mod: SolutionView, tol: float = 1e-4) -> dict:
    """Find blender property_limit constraints that are binding (value at lb or ub)."""
    rows = []
    for code, b in mod.block.items():
        if b.get("section") != "blenderBlock":
            continue
        for pl in b.get("property_limits", []) or []:
            val = pl.get("value")
            if val is None or val != val:   # NaN
                continue
            at_lb = pl.get("lb") is not None and abs(val - pl["lb"]) < tol
            at_ub = pl.get("ub") is not None and abs(val - pl["ub"]) < tol
            if at_lb or at_ub or abs(pl.get("mb", 0.0)) > 1e-9:
                rows.append({
                    "block":  code,
                    "prop":   pl.get("prop"),
                    "output": pl.get("output"),
                    "value":  val,
                    "lb":     pl.get("lb"),
                    "ub":     pl.get("ub"),
                    "mb":     pl.get("mb"),
                    "binding": ("lb" if at_lb else "ub" if at_ub else "—"),
                })
    rows.sort(key=lambda r: -abs(r.get("mb") or 0.0))
    return {"rows": rows}


# ---------------------------------------------------- R12: cycles in chain
def report_r12_cycles(chain: dict) -> dict:
    """Detect directed cycles in the BFS chain graph (block ↔ material loops)."""
    g = defaultdict(list)
    for e in chain["edges"]:
        g[e["src"]].append((e["dst"], e["flow"]))
    cycles = []
    color = {}  # 0 unvisited, 1 in-stack, 2 done
    stack = []

    def dfs(u):
        color[u] = 1
        stack.append(u)
        for v, flow in g.get(u, []):
            if color.get(v, 0) == 1:
                # cycle from index_of(v) ... end of stack
                if v in stack:
                    idx = stack.index(v)
                    cycles.append(stack[idx:] + [v])
            elif color.get(v, 0) == 0:
                dfs(v)
        stack.pop()
        color[u] = 2

    for n in [nn["id"] for nn in chain["nodes"]]:
        if color.get(n, 0) == 0:
            dfs(n)

    # dedupe (cycles found multiple times)
    seen = set()
    uniq = []
    for c in cycles:
        key = tuple(c[c.index(min(c)):] + c[:c.index(min(c))])
        if key in seen: continue
        seen.add(key)
        uniq.append(c)
    return {"cycles": uniq}


# ---------------------------------------------------- R13: balance validation
def report_r13_validation(base: SolutionView, mod: SolutionView) -> dict:
    return {
        "base": {
            "material_error": base.material_error,
            "utility_error":  base.utility_error,
            "solver_status":  base.solver_status,
        },
        "mod": {
            "material_error": mod.material_error,
            "utility_error":  mod.utility_error,
            "solver_status":  mod.solver_status,
        },
        "tol": 1e-4,
    }
