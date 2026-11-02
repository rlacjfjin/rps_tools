"""把 exp_analyzer 注册成 Web/CLI 工具：exp.analyze。

两种用法：
  * 选一个已有的实验目录（含 patch.json），表单里填了的字段会覆盖 patch.json 里的同名设置；
  * 不选目录，直接给 4 个 JSON + 入口物料，工具会在任务目录里生成 patch.json。
产物写在任务目录，不会覆盖实验目录里已有的报告。
"""
from __future__ import annotations

import json
from pathlib import Path

from rps_tools.core.tooling import JobContext, Param, ParamError, Tool, register

FILE_KEYS = ("base_input", "base_output", "modified_input", "modified_output")
REPORTS = [("R1", "R1 KPI"), ("R2", "R2 OBJ 贡献"), ("R3", "R3 链条 BFS (+R3d)"), ("R4", "R4 mermaid 图"),
           ("R5", "R5 装置启停"), ("R7", "R7 影子价"), ("R8", "R8 装置归因"), ("R9", "R9 关键路径"),
           ("R11", "R11 调和性质绑死"), ("R12", "R12 回炼闭环"), ("R13", "R13 平衡校验")]
ENTRY_TYPES = ["material", "manu_block", "spliter_block", "blender_block", "sell", "purc"]
NUMERIC_ANALYSIS = {   # 表单字段 -> (patch.analysis 里的键, 类型)
    "bfs_max_depth": int,
    "min_flow_threshold": float,
    "critical_path_threshold_pct": float,
    "hub_cutoff_producers": int,
    "hub_cutoff_consumers": int,
}

S_SRC = "数据来源（二选一）"
S_DESC = "实验描述 / 入口"
S_ADV = "分析参数"

PARAMS = [
    Param("exp_dir", "已有实验目录", type="dir", section=S_SRC,
          help="包含 patch.json 的目录，例如 experiments/<实验名>。选了它，下面 4 个文件可以不填"),
    Param("base_input", "baseline input", type="file", accept=[".json"], section=S_SRC),
    Param("base_output", "baseline output", type="file", accept=[".json"], section=S_SRC),
    Param("modified_input", "modified input", type="file", accept=[".json"], section=S_SRC),
    Param("modified_output", "modified output", type="file", accept=[".json"], section=S_SRC),
    Param("experiment_name", "实验名称", section=S_DESC, placeholder="MAT-A_sell_ub_0_to_0.1"),
    Param("description", "说明", section=S_DESC, placeholder="raise MAT-A.sell.ub from 0 to 0.1"),
    Param("entry_type", "入口类型", type="choice", default="", section=S_DESC,
          choices=[("", "（沿用 patch.json / material）")] + [(t, t) for t in ENTRY_TYPES]),
    Param("entry_code", "入口 code", section=S_DESC, placeholder="MAT-A",
          help="改动的那个物料 / 装置 code。没有 patch.json 时必填"),
    Param("direction", "BFS 方向", type="choice", default="", section=S_DESC,
          choices=[("", "（沿用 patch.json / both）"), ("both", "both"), ("upstream", "upstream"),
                   ("downstream", "downstream")]),
    Param("modifications", "改动记录", type="text", section=S_DESC, advanced=True,
          placeholder="material:MAT-A sell.ub 0 0.1",
          help="仅用于报告展示：每行 `target field from to`"),
    Param("reports", "报告", type="multi", choices=REPORTS, section=S_ADV,
          help="不选 = 全部（或沿用 patch.json）"),
    Param("bfs_max_depth", "BFS 最大深度", type="int", section=S_ADV, advanced=True, placeholder="12"),
    Param("min_flow_threshold", "最小流量阈值", type="float", section=S_ADV, advanced=True, placeholder="1e-6"),
    Param("critical_path_threshold_pct", "关键路径阈值 %", type="float", section=S_ADV, advanced=True,
          placeholder="1.0"),
    Param("hub_cutoff_producers", "hub 生产者上限", type="int", section=S_ADV, advanced=True, placeholder="8"),
    Param("hub_cutoff_consumers", "hub 消费者上限", type="int", section=S_ADV, advanced=True, placeholder="8"),
    Param("analysis_json", "analysis 覆盖 (JSON)", type="text", section=S_ADV, advanced=True,
          placeholder='{"minimal_min_abs_delta_obj": 50}',
          help="直接合并进 patch.json 的 analysis 段，用于上面没列出的阈值"),
]


def _validate(p: dict) -> None:
    if not p.get("exp_dir"):
        missing = [k for k in FILE_KEYS if not p.get(k)]
        if missing:
            raise ParamError(f"没有选择实验目录时，需要提供全部 4 个文件（缺少：{', '.join(missing)}）")
        if not p.get("entry_code"):
            raise ParamError("没有 patch.json 时必须填写「入口 code」")
    if p.get("analysis_json"):
        try:
            if not isinstance(json.loads(p["analysis_json"]), dict):
                raise ValueError
        except ValueError:
            raise ParamError("「analysis 覆盖」必须是 JSON 对象")


def _parse_modifications(text: str) -> list[dict]:
    mods = []
    for line in (text or "").splitlines():
        parts = line.split()
        if not parts:
            continue
        mod = {"target": parts[0]}
        for key, val in zip(("field", "from", "to"), parts[1:]):
            try:
                mod[key] = float(val) if key != "field" else val
            except ValueError:
                mod[key] = val
        mods.append(mod)
    return mods


def build_patch(p: dict) -> dict:
    if p.get("exp_dir"):
        exp_dir = Path(p["exp_dir"])
        patch_path = exp_dir / "patch.json"
        if not patch_path.is_file():
            raise ParamError(f"{exp_dir} 下没有 patch.json")
        patch = json.loads(patch_path.read_text(encoding="utf-8"))
        files = patch.setdefault("files", {})
        for k, v in list(files.items()):
            if not Path(v).is_absolute():
                files[k] = str((exp_dir / v).resolve())
    else:
        patch = {"experiment_name": "", "modifications": [], "analysis": {}, "files": {}}

    for k in FILE_KEYS:
        if p.get(k):
            patch["files"][k] = str(p[k])
    for k in ("experiment_name", "description"):
        if p.get(k):
            patch[k] = p[k]
    patch["experiment_name"] = patch.get("experiment_name") or p.get("entry_code") or "experiment"
    if p.get("modifications"):
        patch["modifications"] = _parse_modifications(p["modifications"])
    patch.setdefault("modifications", [])

    analysis = patch.setdefault("analysis", {})
    entry = analysis.setdefault("entry_point", {})
    entry["type"] = p.get("entry_type") or entry.get("type") or "material"
    entry["code"] = p.get("entry_code") or entry.get("code")
    entry["direction"] = p.get("direction") or entry.get("direction") or "both"
    if p.get("reports"):
        analysis["reports"] = list(p["reports"])
    for name in NUMERIC_ANALYSIS:
        if p.get(name) is not None:
            analysis[name] = p[name]
    if p.get("analysis_json"):
        analysis.update(json.loads(p["analysis_json"]))
    return patch


def run(ctx: JobContext, p: dict) -> dict:
    from rps_tools.exp_analyzer import load
    from rps_tools.exp_analyzer.__main__ import run_analysis

    patch = build_patch(p)
    patch_path = ctx.job_dir / "patch.json"
    patch_path.write_text(json.dumps(patch, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"patch.json 已生成：{patch_path}")
    ctx.progress(2, "读取 4 个 JSON")
    exp = load.load_experiment(ctx.job_dir)
    summary = run_analysis(exp, ctx.out_dir, progress=ctx.progress)
    # 生成的 patch.json 也放进产物，方便下载后复跑
    (ctx.out_dir / "patch.json").write_text(patch_path.read_text(encoding="utf-8"), encoding="utf-8")
    ctx.progress(100, "完成")
    return summary


register(Tool(
    id="exp.analyze",
    group="exp",
    title="实验因果链分析 (exp_analyzer)",
    description="baseline + modified 两次求解的 input/output JSON → R1-R13 报告、delta_chain 因果图、关键路径。",
    params=PARAMS,
    run=run,
    view="report",
    validate=_validate,
))
