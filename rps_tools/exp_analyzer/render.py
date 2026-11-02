"""Markdown + Mermaid rendering for the experiment analyzer."""
from __future__ import annotations
from pathlib import Path


# --------- shared formatting helpers
def _fmt_num(v, p=4):
    if v is None:
        return "—"
    try:
        f = float(v)
    except (TypeError, ValueError):
        return str(v)
    if f != f:
        return "NaN"
    if abs(f) >= 1e6 or (0 < abs(f) < 1e-3):
        return f"{f:.{p}e}"
    return f"{f:,.{p}f}"


def _md_table(headers, rows):
    head = "| " + " | ".join(headers) + " |"
    sep  = "| " + " | ".join("---" for _ in headers) + " |"
    body = "\n".join("| " + " | ".join(map(str, r)) + " |" for r in rows)
    return "\n".join([head, sep, body])


# ----------- Mermaid graph (R4)
def render_mermaid(chain: dict, critical_edge_set=None) -> str:
    nodes  = chain["nodes"]
    edges  = chain["edges"]
    critical = critical_edge_set or set()

    lines = [
        "%%{init: {'theme':'default','flowchart':{'nodeSpacing':30,'rankSpacing':50,'curve':'basis'}}}%%",
        "flowchart TD",
        "    classDef material fill:#e8f5e6,stroke:#2c7c2e,color:#000",
        "    classDef block    fill:#e1ecff,stroke:#1f4abf,stroke-width:1.5px,color:#000",
        "    classDef sell     fill:#fff4d6,stroke:#c08400,stroke-width:2px,color:#000",
        "    classDef purc     fill:#ffe0e0,stroke:#b03333,color:#000",
        "    classDef entry    fill:#fff0a0,stroke:#d40000,stroke-width:4px,color:#000,font-weight:bold",
        "    classDef trigger  fill:#d8e8ff,stroke:#0040c0,stroke-width:3px,color:#000,font-weight:bold",
    ]

    shape_open  = {"material": '(["', "block": '["',  "sell": '(["', "purc": '(["', "entry": '(["', "trigger": '['}
    shape_close = {"material": '"])', "block": '"]',  "sell": '"])', "purc": '"])', "entry": '"])', "trigger": ']'}

    entry_id = chain.get("entry") if isinstance(chain, dict) else None
    triggers = set(chain.get("triggers", []) or []) if isinstance(chain, dict) else set()

    for n in nodes:
        nid = _id(n["id"])
        label = (n["label"] or "")
        label = label.replace('"', "'").replace("\r", "").replace("\n", "<br/>")
        is_entry   = (entry_id is not None and n["id"] == entry_id)
        is_trigger = (n["id"] in triggers and not is_entry)
        kind = "entry" if is_entry else ("trigger" if is_trigger else n["kind"])
        if is_entry:
            label = "★ ENTRY ★<br/>" + label
        elif is_trigger:
            label = "▶ TRIGGER ◀<br/>" + label
        if is_trigger:
            # Plain block shape but special class
            s_open, s_close = '["', '"]'
        else:
            s_open  = shape_open.get(kind,  '["')
            s_close = shape_close.get(kind, '"]')
        lines.append(f'    {nid}{s_open}{label}{s_close}:::{kind}')

    # Edge label format: NO thousand-separator (commas break mermaid's edge label parser
    # even inside double quotes); keep 4 sig figures or compact scientific.
    def _edge_label(v):
        try:
            f = float(v)
        except (TypeError, ValueError):
            return str(v)
        if f != f:
            return "NaN"
        if abs(f) >= 1e6 or (0 < abs(f) < 1e-3):
            return f"{f:.3e}"   # 1.23e+06
        if abs(f) >= 100:
            return f"{f:.1f}"   # 9946.2
        if abs(f) >= 1:
            return f"{f:.3f}"   # 3.155
        return f"{f:.4f}"       # 0.0081

    for i, e in enumerate(edges):
        src = _id(e["src"])
        dst = _id(e["dst"])
        edge_label = _edge_label(e["flow"])
        if e["kind"] == "purc":
            lines.append(f'    {src} -.->|"{edge_label}"| {dst}')
        else:
            lines.append(f'    {src} -->|"{edge_label}"| {dst}')
        if (e["src"], e["dst"]) in critical:
            lines.append(f"    linkStyle {i} stroke:#d00,stroke-width:3px;")
    return "\n".join(lines)


def render_mermaid_html(mmd_text: str, title: str = "Chain graph") -> str:
    """Standalone HTML with pan/zoom controls.

    Features:
        - drag to pan, mouse wheel to zoom
        - +/−/reset/fit buttons in a floating toolbar
        - F (or button) for fullscreen
        - graph fills the viewport
    """
    # Embed mermaid source as a JS template literal; escape backticks and ${ } to be safe.
    safe = mmd_text.replace("\\", "\\\\").replace("`", "\\`").replace("${", "\\${")

    return f"""<!doctype html>
<html lang="en">
<head>
    <meta charset="utf-8">
    <title>{title}</title>
    <style>
        html, body {{ margin:0; padding:0; height:100%; overflow:hidden;
                       font-family: -apple-system, system-ui, "Segoe UI", Helvetica, Arial, sans-serif; }}
        #header {{ position:fixed; top:0; left:0; right:0; padding:8px 16px;
                    background:rgba(255,255,255,0.92); border-bottom:1px solid #e0e0e0;
                    z-index:10; display:flex; align-items:center; gap:12px; }}
        #title  {{ font-size:14px; font-weight:600; color:#333; flex:1; }}
        #controls button {{ padding:4px 10px; margin:0 2px; cursor:pointer;
                              border:1px solid #ccc; background:#fff; border-radius:4px;
                              font-size:13px; min-width:32px; }}
        #controls button:hover {{ background:#f0f0f0; }}
        #zoom-pct {{ display:inline-block; min-width:48px; text-align:center;
                      font-size:12px; color:#666; }}
        #diagram {{ position:absolute; top:42px; left:0; right:0; bottom:0;
                     background:#fafafa; }}
        #diagram > svg {{ width:100%; height:100%; }}
        #error {{ padding:16px; color:#c00; font-family:monospace; white-space:pre-wrap; }}
        .hint {{ font-size:11px; color:#999; margin-left:8px; }}
    </style>
</head>
<body>
    <div id="header">
        <div id="title">{title}</div>
        <span id="zoom-pct">100%</span>
        <div id="controls">
            <button id="btn-zoom-out" title="缩小 (滚轮向下)">−</button>
            <button id="btn-zoom-in"  title="放大 (滚轮向上)">+</button>
            <button id="btn-reset"    title="复位">⟲</button>
            <button id="btn-fit"      title="自适应窗口">⛶ Fit</button>
            <button id="btn-fullscreen" title="全屏 (F)">⤢ 全屏</button>
        </div>
        <span class="hint">拖动平移, 滚轮缩放</span>
    </div>
    <div id="diagram"></div>
    <div id="error"></div>

    <script src="https://cdn.jsdelivr.net/npm/svg-pan-zoom@3.6.1/dist/svg-pan-zoom.min.js"></script>
    <script type="module">
      import mermaid from 'https://cdn.jsdelivr.net/npm/mermaid@10/dist/mermaid.esm.min.mjs';

      mermaid.initialize({{
        startOnLoad: false,
        theme: 'default',
        securityLevel: 'loose',
        maxTextSize: 500000,
        flowchart: {{ htmlLabels: true, useMaxWidth: false, nodeSpacing: 30, rankSpacing: 50, curve: 'basis' }},
      }});

      const code = `{safe}`;

      async function main() {{
        try {{
          const {{ svg }} = await mermaid.render('diagramSvg', code);
          document.getElementById('diagram').innerHTML = svg;

          const svgEl = document.querySelector('#diagram svg');
          // remove fixed sizing so it fills the container
          svgEl.removeAttribute('width');
          svgEl.removeAttribute('height');
          svgEl.style.width  = '100%';
          svgEl.style.height = '100%';
          svgEl.style.maxWidth = 'none';
          svgEl.style.maxHeight = 'none';

          const pz = svgPanZoom(svgEl, {{
            zoomEnabled: true, panEnabled: true,
            controlIconsEnabled: false, fit: true, center: true,
            minZoom: 0.05, maxZoom: 30, zoomScaleSensitivity: 0.3,
            dblClickZoomEnabled: false,
          }});

          const updatePct = () => {{
            document.getElementById('zoom-pct').textContent =
              Math.round(pz.getZoom() * 100) + '%';
          }};
          pz.setOnZoom(updatePct); updatePct();

          document.getElementById('btn-zoom-in').onclick  = () => pz.zoomIn();
          document.getElementById('btn-zoom-out').onclick = () => pz.zoomOut();
          document.getElementById('btn-reset').onclick    = () => {{ pz.resetZoom(); pz.center(); pz.fit(); updatePct(); }};
          document.getElementById('btn-fit').onclick      = () => {{ pz.fit(); pz.center(); updatePct(); }};
          document.getElementById('btn-fullscreen').onclick = () => {{
            const el = document.documentElement;
            if (document.fullscreenElement) document.exitFullscreen();
            else if (el.requestFullscreen) el.requestFullscreen();
          }};
          document.addEventListener('keydown', (e) => {{
            if (e.key === 'f' || e.key === 'F') document.getElementById('btn-fullscreen').click();
            else if (e.key === '0' || e.key === 'r' || e.key === 'R') document.getElementById('btn-reset').click();
            else if (e.key === '+' || e.key === '=') pz.zoomIn();
            else if (e.key === '-' || e.key === '_') pz.zoomOut();
          }});

          // Refit on window resize
          window.addEventListener('resize', () => {{ pz.resize(); pz.fit(); pz.center(); updatePct(); }});
        }} catch (e) {{
          document.getElementById('error').textContent = 'Mermaid render failed:\\n' + (e && e.message ? e.message : e);
        }}
      }}
      main();
    </script>
</body>
</html>
"""


import re as _re
_ID_SAFE = _re.compile(r"[^A-Za-z0-9_]")


def _id(node_id: str) -> str:
    """Convert any string to a Mermaid-safe ID.

    Mermaid only allows [A-Za-z0-9_] in identifiers; we whitelist those
    and replace everything else with '_'. We also keep '#' readable via
    a 'h' substitution first, so common refinery codes like '2#ABC'
    stay legible as '2hABC' instead of becoming all underscores.
    """
    s = node_id.replace("#", "h")          # readability for '#'
    s = _ID_SAFE.sub("_", s)               # whitelist sanitize
    # leading digit isn't allowed → prefix with underscore
    if s and s[0].isdigit():
        s = "_" + s
    return s


# ----------- main markdown
def render_report(ctx, base, mod, topo_base, topo_mod, reports: dict) -> str:
    """Assemble the main report.md.

    reports: dict of report_id -> data; missing ids are skipped silently.
    """
    sections = []
    sections.append(_section_header(ctx))

    if "R1" in reports:
        sections.append(_section_r1(reports["R1"]))
    if "R13" in reports:
        sections.append(_section_r13(reports["R13"]))
    if "R2" in reports:
        sections.append(_section_r2(reports["R2"]))
    if "R3d_minimal" in reports:
        sections.append(_section_r3d_minimal(reports["R3d_minimal"]))
    if "R3" in reports:
        sections.append(_section_r3(reports["R3"], reports.get("R9")))
    if "R4" in reports:
        sections.append(_section_r4(reports["R4"]))
    if "R3d" in reports:
        sections.append(_section_r3d(reports["R3d"]))
    if "R5" in reports:
        sections.append(_section_r5(reports["R5"]))
    if "R7" in reports:
        sections.append(_section_r7(reports["R7"]))
    if "R8" in reports:
        sections.append(_section_r8(reports["R8"]))
    if "R9" in reports:
        sections.append(_section_r9(reports["R9"]))
    if "R11" in reports:
        sections.append(_section_r11(reports["R11"]))
    if "R12" in reports:
        sections.append(_section_r12(reports["R12"]))

    return "\n\n".join(sections) + "\n"


def _section_header(ctx) -> str:
    mods = ctx.patch.get("modifications", [])
    mod_lines = []
    for m in mods:
        mod_lines.append(
            f"- **{m.get('target')}** · `{m.get('field')}`: "
            f"`{m.get('from')}` → `{m.get('to')}`"
        )
    desc = ctx.patch.get("description", "")
    return (
        f"# Experiment: {ctx.name}\n\n"
        f"{desc}\n\n"
        f"### Modifications\n\n" + ("\n".join(mod_lines) if mod_lines else "_(none)_")
        + "\n\n"
        + "### 文件\n\n"
        + "- `chain_graph.html` — 链条图 (浏览器打开看 mermaid 图)\n"
        + "- `critical_path.html` — 关键路径图\n"
        + "- `chain_graph.mmd` / `critical_path.mmd` — Mermaid 源码\n"
        + "- `obj_contributions.csv` — R2 表 (Excel 可打开, utf-8 BOM)\n"
        + "- `block_activation.csv` — R5 表\n"
        + "- `report.json` — 全部数据的机器可读版\n\n"
        + "### 报告索引\n\n"
        + "| ID | 内容 |\n|---|---|\n"
        + "| R1  | OBJ / status / 收入成本 / iter 等顶层 KPI 对比 |\n"
        + "| R13 | 物料 / 工具平衡偏差校验（数学正确性兜底） |\n"
        + "| R2  | OBJ 逐项贡献对比：2a 顶层 KPI 分块, 2b 逐物料 SELL/PURC |\n"
        + "| R3  | 从 entry_point 沿物料平衡的有向 BFS 链条 |\n"
        + "| R4  | R3 的 mermaid 图（看 `chain_graph.html`） |\n"
        + "| R5  | 装置启停 / 吞吐量 diff — ON/OFF/Δ 三种状态 |\n"
        + "| R7  | 影子价格分组：CAP / SELL.ub / PURC.ub / WBAL |\n"
        + "| R8  | 装置级 OBJ 归因 — 按物料 SELL 收入分摊到 producer |\n"
        + "| R9  | 关键路径 — Δ OBJ 贡献过阈值的子图 |\n"
        + "| R11 | 调和器 property_limit 绑死分析 |\n"
        + "| R12 | 物料链回炼/闭环检测 |\n"
    )


# ---------- R1
def _section_r1(r1) -> str:
    rows = []
    for r in r1["rows"]:
        pct = "" if r["pct"] is None else f" ({r['pct']:+.2f}%)"
        rows.append([
            r["name"], _fmt_num(r["base"]), _fmt_num(r["mod"]),
            f"{_fmt_num(r['delta'])}{pct}"
        ])
    return (
        "## R1 · KPIs\n\n"
        f"- baseline solver_status: **{r1['base_status']}**\n"
        f"- modified solver_status: **{r1['mod_status']}**\n\n"
        + _md_table(["指标", "base", "modified", "Δ"], rows)
    )


# ---------- R13
def _section_r13(r13) -> str:
    return (
        "## R13 · 物料/工具平衡校验\n\n"
        f"- base : material_error={_fmt_num(r13['base']['material_error'])}  "
        f"utility_error={_fmt_num(r13['base']['utility_error'])}  status={r13['base']['solver_status']}\n"
        f"- mod  : material_error={_fmt_num(r13['mod']['material_error'])}  "
        f"utility_error={_fmt_num(r13['mod']['utility_error'])}  status={r13['mod']['solver_status']}\n"
        f"- 阈值: {_fmt_num(r13['tol'])}\n"
    )


# ---------- R2
def _section_r2(r2, top_n: int = 60) -> str:
    out = ["## R2 · OBJ 逐项贡献对比\n"]

    # --- 2a. KPI-level summary (material/utility revenue/cost diff)
    out.append("### 2a · OBJ 大块组成对比 (顶层 KPI)\n")
    summary_rows = []
    for s in r2["summary"]:
        summary_rows.append([
            s["name"], _fmt_num(s["base"], 2), _fmt_num(s["mod"], 2),
            _fmt_num(s["delta"], 2),
        ])
    out.append(_md_table(["分项", "base", "mod", "Δ"], summary_rows))

    rec = r2["reconcile"]
    out.append(
        "\n**对账**:\n"
        f"- Δ OBJ (top-level):           **{_fmt_num(rec['obj_delta'], 2)}**\n"
        f"- Σ SELL/PURC 物料贡献 Δ:      {_fmt_num(rec['sell_purc_delta_total'], 2)}\n"
        f"- Σ utility (revenue−cost) Δ: {_fmt_num(rec['utility_delta_total'], 2)}\n"
        f"- 未归因残差 (应近 0):         {_fmt_num(rec['unaccounted'], 2)}\n"
    )

    # --- 2b. Per-material SELL/PURC contributions, filtered |Δ| >= threshold
    title = (
        f"### 2b · 逐物料 SELL/PURC 贡献 "
        f"(过滤 |Δ| < {_fmt_num(r2['min_abs_delta'], 2)}; "
        f"剩 {len(r2['interesting'])} 项, 隐藏 {r2['n_filtered']} 项)\n"
    )
    out.append(title)

    if not r2["interesting"]:
        out.append("_所有 SELL/PURC 项的 |Δ| 都小于阈值, 即整个产销组合没有显著变化._\n")
    else:
        rows = []
        for r in r2["interesting"][:top_n]:
            rows.append([
                r["kind"], r["code"], _fmt_num(r["price"], 2),
                _fmt_num(r["base_qty"]), _fmt_num(r["mod_qty"]),
                _fmt_num(r["base_obj"], 2), _fmt_num(r["mod_obj"], 2),
                _fmt_num(r["delta_obj"], 2),
            ])
        out.append(_md_table(
            ["kind", "code", "price", "base_qty", "mod_qty",
             "base_obj", "mod_obj", "Δ obj"],
            rows,
        ))
        out.append(
            f"\n- 累计 SELL/PURC base OBJ: {_fmt_num(r2['total_base'], 2)}\n"
            f"- 累计 SELL/PURC mod  OBJ: {_fmt_num(r2['total_mod'], 2)}\n"
            f"- 全量 Σ Δ obj (SELL/PURC): **{_fmt_num(r2['total_delta'], 2)}**"
        )
    return "\n".join(out)


# ---------- R3d minimal
def _section_r3d_minimal(r3m) -> str:
    n_block = sum(1 for n in r3m["nodes"] if n["kind"] == "block")
    n_mat   = sum(1 for n in r3m["nodes"] if n["kind"] == "material")
    n_sell  = sum(1 for n in r3m["nodes"] if n["kind"] == "sell")
    n_purc  = sum(1 for n in r3m["nodes"] if n["kind"] == "purc")
    reached = r3m.get("terminals_reached", []) or []
    unreached = r3m.get("terminals_unreached", []) or []
    flt = r3m.get("filter", {}) or {}
    out = [
        "## R3d_minimal · 最小价值链 (路径式提取)\n",
        f"_从入口节点出发, 对每个 |Δobj| ≥ ¥{flt.get('min_abs_delta_obj', 100):g} "
        f"的 SELL/PURC 终端, BFS 找一条所有边流量变化 ≥ {flt.get('min_rel_flow_change', 0.1)*100:g}% "
        f"的最短路径; 取并集. 这是『贯通的价值链』, 不含『撒网』噪声._\n",
        f"- 节点: **{len(r3m['nodes'])}** (block={n_block}, material={n_mat}, sell={n_sell}, purc={n_purc})",
        f"- 边: **{len(r3m['edges'])}**",
        f"- 命中终端: {len(reached)}, 未达: {len(unreached)}",
        "",
    ]
    if reached:
        out.append("### 已纳入的销售/采购终端 (按 |Δobj| 降序)\n")
        rows = []
        for t in sorted(reached, key=lambda x: -abs(x.get("delta_obj", 0))):
            rows.append([t["kind"].upper(), t["code"], _fmt_num(t["price"], 2),
                         _fmt_num(t["base_qty"]), _fmt_num(t["mod_qty"]),
                         _fmt_num(t["delta_obj"], 2), t.get("path_len", "—")])
        out.append(_md_table(["kind", "code", "price", "base_qty", "mod_qty", "Δobj", "path_len"], rows))
    if unreached:
        out.append("\n### 未达终端 (有显著 OBJ 变化但与入口无贯通变化路径, 多由 LP 全局再分配产生)\n")
        rows = [[t["kind"].upper(), t["code"], _fmt_num(t["price"], 2),
                 _fmt_num(t["base_qty"]), _fmt_num(t["mod_qty"]),
                 _fmt_num(t["delta_obj"], 2)] for t in sorted(unreached, key=lambda x: -abs(x.get("delta_obj", 0)))]
        out.append(_md_table(["kind", "code", "price", "base_qty", "mod_qty", "Δobj"], rows))
    out.append("\n> 打开 [delta_chain_minimal.html](delta_chain_minimal.html) 查看可缩放图.")
    return "\n".join(out)


# ---------- R3
def _section_r3(r3, r9=None) -> str:
    lines = [
        "## R3 · 链条 BFS 报告\n",
        f"- 入口节点: `{r3['entry']}`",
        f"- 节点数: {len(r3['nodes'])}, 边数: {len(r3['edges'])}",
        "",
        "### 边一览 (按流量降序, top 60)",
        "",
    ]
    edges_sorted = sorted(r3["edges"], key=lambda e: -abs(e["flow"]))[:60]
    rows = [[e["src"], e["dst"], _fmt_num(e["flow"]), e["kind"]] for e in edges_sorted]
    lines.append(_md_table(["src", "dst", "flow", "kind"], rows))
    return "\n".join(lines)


# ---------- R4 (mermaid graph reference)
def _section_r4(mmd: str) -> str:
    return (
        "## R4 · 链条图 (Mermaid)\n\n"
        "> **直接看图**: 用浏览器打开同目录的 [chain_graph.html](chain_graph.html). "
        "Typora 用户也能渲染下面这块（已修复换行符语法）. "
        "如果还是看不到图, 可以把整段 ```mermaid``` 代码粘到 https://mermaid.live/ 在线渲染.\n\n"
        "_这是完整 BFS 图，节点多。要看精简版（只含真正变化的部分），看 R3d / `delta_chain.html`._\n\n"
        "```mermaid\n" + mmd + "\n```"
    )


# ---------- R3d (delta chain, "narrative" — wider story than minimal)
def _section_r3d(r3d: dict) -> str:
    n_block = sum(1 for n in r3d["nodes"] if n["kind"] == "block")
    n_mat   = sum(1 for n in r3d["nodes"] if n["kind"] == "material")
    n_sell  = sum(1 for n in r3d["nodes"] if n["kind"] == "sell")
    n_purc  = sum(1 for n in r3d["nodes"] if n["kind"] == "purc")
    reached   = r3d.get("terminals_reached",   []) or []
    unreached = r3d.get("terminals_unreached", []) or []
    flt = r3d.get("filter", {}) or {}
    out = [
        "## R3d · 变化子图 (delta chain, 中等范围故事)\n",
        "> **推荐第一眼看的图**: 用浏览器打开同目录的 [delta_chain.html](delta_chain.html). "
        f"和 R3d_minimal 同算法 (path-based), 但更宽松的阈值: "
        f"top {flt.get('top_n_terminals', 30)} 个终端, "
        f"|Δobj| ≥ ¥{flt.get('min_abs_delta_obj', 50)}, "
        f"边流量变化 ≥ {flt.get('min_rel_flow_change', 0.03)*100:g}%.\n\n",
        f"- 节点: **{len(r3d['nodes'])}** (block={n_block}, material={n_mat}, sell={n_sell}, purc={n_purc})",
        f"- 边: **{len(r3d['edges'])}**",
        f"- 命中终端: {len(reached)}, 未达: {len(unreached)}",
    ]
    if unreached:
        out.append(f"\n未达的显著终端 ({len(unreached)} 个, 由 LP 全局再分配产生, 与入口无贯通变化路径):")
        for u in sorted(unreached, key=lambda x: -abs(x.get('delta_obj', 0)))[:10]:
            out.append(f"  - {u['kind'].upper()} {u['code']} Δobj={_fmt_num(u['delta_obj'], 0)}")
    return "\n".join(out)


# ---------- R5
def _section_r5(r5) -> str:
    rows = []
    n_on = sum(1 for r in r5["rows"] if r["transition"] == "ON")
    n_off = sum(1 for r in r5["rows"] if r["transition"] == "OFF")
    n_chg = sum(1 for r in r5["rows"] if r["transition"] == "Δ")
    for r in r5["rows"][:60]:
        rows.append([
            r["transition"], r["code"], r["section"],
            _fmt_num(r["base"]), _fmt_num(r["mod"]), _fmt_num(r["delta"]),
        ])
    return (
        "## R5 · 装置启停 / 吞吐量 diff (top 60, 按 |Δ| 排序)\n\n"
        f"_装置吞吐量在两个 case 间的变化. "
        f"**ON**={n_on} 装置在 base 关停但在 modified 启动; "
        f"**OFF**={n_off} 反之; "
        f"**Δ**={n_chg} 两 case 都在运行但吞吐量不同. "
        f"对 manuBlock 吞吐量 = Σ cases.mass_flow.value._\n\n"
        + _md_table(["状态", "code", "section", "base", "mod", "Δ"], rows)
    )


# ---------- R7
def _section_r7(r7) -> str:
    out = ["## R7 · 影子价格 (binding constraints, grouped by kind)\n"]

    def _tbl(title, rows, headers, fields, total):
        if not rows:
            out.append(f"### {title}  (无)\n")
            return
        out.append(f"### {title}  (top {len(rows)} of {total})\n")
        body = []
        for r in rows:
            body.append([
                r.get(f) if f != "mb" and f not in ("value","ub","lb")
                else _fmt_num(r.get(f))
                for f in fields
            ])
        out.append(_md_table(headers, body))
        out.append("")

    _tbl("装置容量绑死 (CAP, 真正卡产能的地方)",
         r7.get("cap", []),
         ["code", "shadow price", "value", "ub", "lb"],
         ["code", "mb", "value", "ub", "lb"],
         r7.get("total", {}).get("cap", 0))
    _tbl("SELL.ub 绑死",
         r7.get("sell_ub", []),
         ["material", "shadow price", "value", "ub"],
         ["code", "mb", "value", "ub"],
         r7.get("total", {}).get("sell_ub", 0))
    _tbl("PURC.ub 绑死",
         r7.get("purc_ub", []),
         ["material", "shadow price", "value", "ub"],
         ["code", "mb", "value", "ub"],
         r7.get("total", {}).get("purc_ub", 0))
    _tbl("物料 WBAL 影子价 (≈ 边际价值, 信息性)",
         r7.get("wbal", []),
         ["material", "shadow price"],
         ["code", "mb"],
         r7.get("total", {}).get("wbal", 0))
    return "\n".join(out)


# ---------- R8
def _section_r8(r8) -> str:
    rows = []
    for r in r8["rows"][:40]:
        rows.append([
            r["code"], r.get("section") or "",
            _fmt_num(r["base_net"], 2), _fmt_num(r["mod_net"], 2),
            _fmt_num(r["delta_net"], 2),
            _fmt_num(r["base_revenue"], 2), _fmt_num(r["mod_revenue"], 2),
            _fmt_num(r["base_cost"], 2),    _fmt_num(r["mod_cost"], 2),
        ])
    return (
        "## R8 · 装置级 OBJ 归因 (top 40, 按 |Δnet|)\n\n"
        "_净收益 = Σ(产物·售价×分摊) − Σ(进料·采购价×分摊)；下游再加工的价值不计_\n\n"
        + _md_table(
            ["block", "section", "base net", "mod net", "Δ net",
             "base revenue", "mod revenue", "base cost", "mod cost"],
            rows,
        )
    )


# ---------- R9
def _section_r9(r9) -> str:
    return (
        "## R9 · 关键路径\n\n"
        f"- Δ OBJ (SELL 合计估计): **{_fmt_num(r9['delta_total'], 2)}**\n"
        f"- 阈值: ≥ {r9['threshold_pct']}% 单条贡献\n"
        f"- 关键节点数: {len(r9['critical_nodes'])}\n"
        f"- 关键边数: {len(r9['critical_edges'])}\n\n"
        "_critical_path.mmd 已单独导出, 只含贡献过阈值的子图._"
    )


# ---------- R11
def _section_r11(r11) -> str:
    if not r11["rows"]:
        return "## R11 · 调和器性质上下界绑死分析\n\n_无绑紧约束_"
    rows = []
    for r in r11["rows"][:40]:
        rows.append([
            r["block"], r["output"], r["prop"],
            _fmt_num(r["value"]), _fmt_num(r.get("lb")), _fmt_num(r.get("ub")),
            r["binding"], _fmt_num(r.get("mb")),
        ])
    return (
        "## R11 · 调和器性质上下界绑死 (top 40, 按 |dual|)\n\n"
        + _md_table(
            ["block", "output", "prop", "value", "lb", "ub", "binding", "dual"],
            rows,
        )
    )


# ---------- R12
def _section_r12(r12) -> str:
    if not r12["cycles"]:
        return "## R12 · 物料链中的回炼/闭环\n\n_未检出闭环_"
    lines = ["## R12 · 物料链中的回炼/闭环", ""]
    for i, c in enumerate(r12["cycles"][:20], 1):
        lines.append(f"{i}. " + " → ".join(c))
    return "\n".join(lines)
