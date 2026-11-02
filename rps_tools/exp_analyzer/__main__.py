"""CLI entry: python -m rps_tools.exp_analyzer <experiment_dir>"""
from __future__ import annotations
import argparse
import json
import sys
from pathlib import Path

from . import load, topology, solution, reports, render


def main(argv=None):
    parser = argparse.ArgumentParser(
        prog="exp_analyzer",
        description="Analyze a rps_solver experiment (baseline vs modified).",
    )
    parser.add_argument("exp_dir", help="Experiment directory containing patch.json")
    parser.add_argument(
        "--out", default=None,
        help="Output directory (defaults to <exp_dir>).",
    )
    args = parser.parse_args(argv)

    ctx = load.load_experiment(args.exp_dir)
    out_dir = Path(args.out or ctx.exp_dir).resolve()
    run_analysis(ctx, out_dir)


def _no_progress(pct, msg=""):
    pass


def run_analysis(ctx, out_dir, progress=_no_progress) -> dict:
    """Run every requested report for an already-loaded experiment.

    Writes report.md / report.json / *.mmd / *.html / *.csv into out_dir and
    returns a small summary dict (used by the web job view).
    progress(pct, msg) is called between stages; pct in [0, 100].
    """
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    print(f"[exp_analyzer] Loaded experiment: {ctx.name}")
    print(f"               exp_dir: {ctx.exp_dir}")
    print(f"               out_dir: {out_dir}")
    print(f"               reports requested: {ctx.reports}")

    progress(10, "构建拓扑")
    topo_base = topology.build_topology(ctx.base_input)
    topo_mod  = topology.build_topology(ctx.mod_input)
    base = solution.parse_solution(ctx.base_output)
    mod  = solution.parse_solution(ctx.mod_output)

    out = {}
    asked = set(ctx.reports) if ctx.reports else {"R1","R2","R3","R3d","R4","R5","R7","R8","R9","R11","R12","R13"}
    # R3d is the curated narrative chain — always produce if R3 is requested.
    if "R3" in asked: asked.add("R3d")

    if "R1"  in asked: out["R1"]  = reports.report_r1_kpis(base, mod)
    if "R13" in asked: out["R13"] = reports.report_r13_validation(base, mod)
    if "R2"  in asked: out["R2"]  = reports.report_r2_obj_contributions(base, mod, topo_base, topo_mod)
    progress(25, "R3 链条 BFS")
    if "R3"  in asked or "R4" in asked or "R9" in asked or "R12" in asked or "R3d" in asked:
        chain = reports.report_r3_chain(
            topo_mod, mod,
            entry_point = ctx.entry_point,
            direction   = ctx.entry_point.get("direction", "both"),
            max_depth   = ctx.bfs_max_depth,
            min_flow    = ctx.min_flow_threshold,
            hub_cutoff_producers = ctx.hub_cutoff_producers,
            hub_cutoff_consumers = ctx.hub_cutoff_consumers,
        )
        out["R3"] = chain
    progress(45, "R3d trigger-via 路径")
    if "R3d" in asked and "R3" in out:
        # R3d: path-based extraction with LOOSER thresholds — wider story, more terminals.
        out["R3d"] = reports.report_r3d_path_minimal(
            topo_mod, base, mod,
            entry_point        = ctx.entry_point,
            top_n_terminals    = int(ctx.patch.get("analysis", {}).get("delta_chain_top_n_terminals", 30)),
            min_abs_delta_obj  = float(ctx.patch.get("analysis", {}).get("delta_chain_min_abs_delta_obj", 50.0)),
            min_rel_flow_change= float(ctx.patch.get("analysis", {}).get("delta_chain_min_rel_flow_change", 0.03)),
        )
        # R3d_minimal: STRICTER thresholds — tight, focused, story-shaped.
        out["R3d_minimal"] = reports.report_r3d_path_minimal(
            topo_mod, base, mod,
            entry_point        = ctx.entry_point,
            top_n_terminals    = int(ctx.patch.get("analysis", {}).get("minimal_top_n_terminals", 12)),
            min_abs_delta_obj  = float(ctx.patch.get("analysis", {}).get("minimal_min_abs_delta_obj", 100.0)),
            min_rel_flow_change= float(ctx.patch.get("analysis", {}).get("minimal_min_rel_flow_change", 0.05)),
        )
    progress(65, "R5-R13 其余报告")
    if "R5"  in asked: out["R5"]  = reports.report_r5_block_activation(base, mod, topo_mod)
    if "R7"  in asked: out["R7"]  = reports.report_r7_shadow_prices(mod, topo_mod)
    if "R8"  in asked: out["R8"]  = reports.report_r8_block_obj(base, mod, topo_mod)
    if "R9"  in asked and "R3" in out:
        out["R9"]  = reports.report_r9_critical_path(out["R3"], base, mod, ctx.critical_path_threshold_pct)
    if "R11" in asked: out["R11"] = reports.report_r11_property_limits(mod)
    if "R12" in asked and "R3" in out:
        out["R12"] = reports.report_r12_cycles(out["R3"])

    # ---- Render outputs
    progress(80, "渲染图与报告")
    critical_edge_set = set()
    if "R9" in out:
        critical_edge_set = {(e["src"], e["dst"]) for e in out["R9"]["critical_edges"]}

    if "R3" in out:
        mmd_full = render.render_mermaid(out["R3"], critical_edge_set=None)
        (out_dir / "chain_graph.mmd").write_text(mmd_full, encoding="utf-8")
        (out_dir / "chain_graph.html").write_text(
            render.render_mermaid_html(mmd_full, title=f"Chain graph — {ctx.name}"),
            encoding="utf-8",
        )

        if "R9" in out:
            crit_nodes = set(out["R9"]["critical_nodes"])
            crit_chain = {
                "nodes": [n for n in out["R3"]["nodes"] if n["id"] in crit_nodes],
                "edges": [e for e in out["R3"]["edges"]
                          if (e["src"] in crit_nodes) and (e["dst"] in crit_nodes)],
            }
            crit_mmd = render.render_mermaid(crit_chain, critical_edge_set)
            (out_dir / "critical_path.mmd").write_text(crit_mmd, encoding="utf-8")
            (out_dir / "critical_path.html").write_text(
                render.render_mermaid_html(crit_mmd, title=f"Critical path — {ctx.name}"),
                encoding="utf-8",
            )

        if "R3d" in out:
            delta_mmd = render.render_mermaid(out["R3d"], critical_edge_set=None)
            (out_dir / "delta_chain.mmd").write_text(delta_mmd, encoding="utf-8")
            (out_dir / "delta_chain.html").write_text(
                render.render_mermaid_html(
                    delta_mmd,
                    title=f"Delta chain (narrative) — {ctx.name}",
                ),
                encoding="utf-8",
            )
        if "R3d_minimal" in out:
            mini_mmd = render.render_mermaid(out["R3d_minimal"], critical_edge_set=None)
            (out_dir / "delta_chain_minimal.mmd").write_text(mini_mmd, encoding="utf-8")
            (out_dir / "delta_chain_minimal.html").write_text(
                render.render_mermaid_html(
                    mini_mmd,
                    title=f"Delta chain (minimal, ON/OFF only) — {ctx.name}",
                ),
                encoding="utf-8",
            )

        out["R4"] = mmd_full

    # CSVs
    if "R2" in out:
        _write_csv_r2(out_dir / "obj_contributions.csv", out["R2"]["rows"])
    if "R5" in out:
        _write_csv_r5(out_dir / "block_activation.csv", out["R5"]["rows"])

    report_md = render.render_report(ctx, base, mod, topo_base, topo_mod, out)
    (out_dir / "report.md").write_text(report_md, encoding="utf-8")
    (out_dir / "report.json").write_text(
        json.dumps(_jsonable(out), ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    print(f"[exp_analyzer] Wrote: report.md, report.json, chain_graph.mmd"
          + (", critical_path.mmd" if "R9" in out else "")
          + ", obj_contributions.csv, block_activation.csv")
    return _summary(ctx, out)


def _summary(ctx, out: dict) -> dict:
    kpis = {}
    for row in (out.get("R1") or {}).get("rows", []):
        kpis[row["name"]] = {"base": row["base"], "mod": row["mod"],
                             "delta": row["delta"], "pct": row["pct"]}
    return {
        "experiment_name": ctx.name,
        "description": ctx.patch.get("description", ""),
        "entry_point": ctx.entry_point,
        "reports": sorted(k for k in out if k != "R4"),
        "kpis": _jsonable(kpis),
    }


def _jsonable(o):
    if isinstance(o, dict):
        return {k: _jsonable(v) for k, v in o.items()}
    if isinstance(o, (list, tuple, set)):
        return [_jsonable(x) for x in o]
    if isinstance(o, (str, int, float, bool)) or o is None:
        return o
    return str(o)


def _write_csv_r2(path: Path, rows: list):
    """
    R2: per-material SELL/PURC contribution to OBJ.
      kind/code/price : 物料 + 类型 + 单价
      base_qty/mod_qty/delta_qty : 数量 (base, modified, 差)
      base_obj/mod_obj/delta_obj : OBJ 贡献 (= price × qty, PURC 为负)
      mb_base/mb_mod  : 该变量上界的影子价 (shadow price of sell.ub / purc.ub)
    File encoding: utf-8-sig (Excel 直接打开不乱码)
    """
    import csv
    fields = ["kind","code","price","base_qty","mod_qty","delta_qty",
              "base_obj","mod_obj","delta_obj","mb_base","mb_mod"]
    with open(path, "w", encoding="utf-8-sig", newline="") as fp:
        w = csv.DictWriter(fp, fieldnames=fields)
        w.writeheader()
        for r in rows:
            w.writerow({k: r.get(k, "") for k in fields})


def _write_csv_r5(path: Path, rows: list):
    """
    R5: block activation / throughput diff between baseline and modified.
      transition : ON  = base=0, mod>0    (装置在 modified 中启动)
                   OFF = base>0, mod=0    (装置在 modified 中停车)
                   Δ   = base>0, mod>0 但吞吐不同
      code       : block code
      section    : manuBlock / spliterBlock / blenderBlock / mixerBlock / cduBlock / customBlock / processBlock
      base/mod   : 装置吞吐量 (manuBlock = Σ cases.mass_flow.value)
      delta      : mod - base
    File encoding: utf-8-sig
    """
    import csv
    fields = ["transition","code","section","base","mod","delta"]
    with open(path, "w", encoding="utf-8-sig", newline="") as fp:
        w = csv.DictWriter(fp, fieldnames=fields)
        w.writeheader()
        for r in rows:
            w.writerow({k: r.get(k, "") for k in fields})


if __name__ == "__main__":
    main()
