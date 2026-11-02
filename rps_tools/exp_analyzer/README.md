# exp_analyzer

把一对 rps_solver 求解结果（baseline vs modified）变成一份带链条图的诊断报告。**不调求解器，只读你已经跑好的 input/output JSON**。

## 用法

```
python -m rps_tools.exp_analyzer <experiment_dir>
```

`<experiment_dir>` 里要有 5 个文件：

```
my_experiment/
├── patch.json                ← 实验描述（schema 见下）
├── base_input.json           ← 你跑 baseline 用的 rps_solver input
├── base_output.json          ← baseline 跑完后 export 出来的 model_output
├── modified_input.json       ← 改了之后的 input
└── modified_output.json      ← 改后跑出来的 model_output
```

跑完产出会落在同一个目录里：

```
my_experiment/
├── report.md                 ← 主报告 (md + 嵌入 mermaid)
├── report.json               ← 所有数据的机器可读版
├── chain_graph.mmd           ← Mermaid 源码（完整链）
├── critical_path.mmd         ← 仅含贡献 ≥ 阈值的关键路径子图
├── obj_contributions.csv     ← R2 表 (Excel 可打开)
└── block_activation.csv      ← R5 表
```

## patch.json schema

参考 [sample_patch.json](sample_patch.json)，最简版本：

```json
{
  "experiment_name": "MAT-A_sell_ub_0_to_0.1",
  "description":     "raise MAT-A.sell.ub from 0 to 0.1",

  "modifications": [
    {"target":"material:MAT-A", "field":"sell.ub", "from":0.0, "to":0.1}
  ],

  "analysis": {
    "entry_point": {"type":"material", "code":"MAT-A", "direction":"both"},
    "reports":     ["R1","R2","R3","R4","R5","R7","R8","R9","R11","R12","R13"],
    "bfs_max_depth": 12,
    "min_flow_threshold": 1e-6,
    "critical_path_threshold_pct": 1.0
  },

  "files": {
    "base_input":      "base_input.json",
    "base_output":     "base_output.json",
    "modified_input":  "modified_input.json",
    "modified_output": "modified_output.json"
  }
}
```

| 字段 | 说明 |
|------|------|
| `modifications` | 仅做元数据展示用 (告诉报告"改了啥")，脚本本身不重放改动 |
| `entry_point.type` | `material` / `manu_block` / `spliter_block` / `blender_block` / `sell` / `purc` |
| `entry_point.direction` | `upstream` / `downstream` / `both` |
| `bfs_max_depth` | BFS 最大深度。叶物料（生产者/消费者很少的物料）一般 8-12 够 |
| `min_flow_threshold` | 小于这个量的边不画到图上 |
| `critical_path_threshold_pct` | R9 关键路径阈值。贡献占 Δ OBJ ≥ 此 % 的路径才高亮 |
| `reports` | 要哪些报告。缺省 = 全部 |

## 报告说明

| ID | 报告 |
|----|------|
| R1 | KPI 表 (OBJ / material_revenue / material_cost / iter / status) |
| R2 | 每个 SELL/PURC 变量的 OBJ 贡献对比, 按 \|Δ\| 排序 |
| R3 | 从 entry_point 沿物料平衡等式做有向 BFS 的链条 (按方向分上下游) |
| R4 | R3 的 mermaid 图 (嵌入 report.md + 独立 .mmd) |
| R5 | 装置启停/吞吐量 diff |
| R7 | 影子价格 (CAP / SELL.ub / PURC.ub / WBAL) 分组展示 |
| R8 | 装置级 OBJ 归因 (按产物 SELL 分摊回上游 producer) |
| R9 | 关键路径 — 贡献 ≥ 阈值的子图 |
| R11 | 调和器 property_limit 绑死分析 |
| R12 | 物料链中的回炼/闭环 |
| R13 | 物料/工具平衡校验 (material_error, utility_error) |

## 已知限制

- 双 case 必须给齐，目前不支持单 case 分析（缺 baseline 没法对比）。
- R6 (RFV 预处理结构 diff) 未实现 — 需要额外给两份 `*.xmps` 文件。
- R10 (反事实探测) 未实现 — 需要多轮迭代跑求解器。
- R8 装置级归因用比例分摊法，下游再加工的价值不计入产物原始 producer。

需要 R6 / R10 时回来补。
