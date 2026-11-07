# 离线冲突与性质诊断

本模块从 RPS 的诊断模块及入参排查 skill 迁移，接入同一套 CLI/Web 工具注册表。只读取选定文件，不修改输入、初值、状态、目标、多初值或稳定性；三个入口均不调用业务求解器或辅助 LP，不依赖 Redis、RPS Model、NumPy/Pandas。

```bash
python -m rps_tools run diagnose.input_feasibility input=input.json --out workspace/check-input
python -m rps_tools run diagnose.failure_analysis input=input.json output=output.json --out workspace/check-failure
python -m rps_tools run diagnose.property_bounds input=input.json output=output.json --out workspace/check-properties
```

用 `python -m rps_tools tools <工具ID>` 查看可选文件参数。Web 的“物料 / 约束级诊断”提供相同功能，每个任务生成 `report.md` 和 `report.json`。

| 入口 | 能力 | 证据不足时 |
|---|---|---|
| input_feasibility | 上下界、供料总量、单工况 CDU 供给/性质、公用工程、联产品出口、停工断供、自定义总量与关闭路径 | 线索为 candidate；没有命中不等于全模型可行 |
| failure_analysis | 优先展示内嵌失败诊断；可读取同次原生活动值/迭代日志，关联性质与装置入参 | 只保留有来源的疑点，不用初值或失败输出流量补归因 |
| property_bounds | 优先展示内嵌提示；也可从已保存的求解后模型读取实际值、有效边界、流量和边际 | 不以 start/零补值；缺失边际来源为 null，固定性质单列 |

`snapshot` 可选原始模型的 `bounds/rows` 字典，用 Fraction 复核边界矛盾、单行可达范围和强制赋值链。非线性整行、无法展开的表达式和整数限制等按报告中的覆盖范围处理。接口始终传 `use_lp=False`；内核保留的可选 SciPy 线性组合证书搜索不暴露到这些入口，也不是默认依赖。

## 输入对应关系

不按“最新时间”自动拼接文件。需要同次运行的显式路径或上传文件；有不一致的 `run_id/request_id` 或 SHA256 时停止归因。可选 `manifest` 按角色列 `files.<role>.sha256`，只验证已选择的文件，不跟随清单中的路径。

`model` 接受完整 MPProto `variable/constarint`（也接受 `constraint`）JSON；性质诊断必须使用求解后的实际点。原始 `snapshot` 使用独立的有效边界契约，不能把丢失半连续类型的旧 dump 直接认作完整证明模型。旧表达式文本不能替代完整 N/X 定义、边界覆盖和实际点元数据。

原生 `native` JSON 的活动值需要明确 `scaled:false`。有编码行名时提供 `row_map`；未知行不猜测。迭代日志最多读末尾 8 MiB，标明截断、超时、中间观测/最后观测及 profile。残差是观测证据，不是应放宽的幅度。

实际字段契约见仓库 skill 的 `references/artifact-contract.md`。

## 结论含义

- `confirmed_conflicts`：本次用原始快照复核的数学冲突。
- `reported_conflicts`：输出中已有的冲突记录，保留来源，不冒充本次独立证明。
- `suspected_constraints` / `input_hints`：失败残差或原始输入线索；可能受其他约束影响。
- 原业务状态只记录到 `solver_reference`。诊断不新增业务状态、不复活求解后 feasibility 翻判。
- `input_scans`：`ran=true` 表示该项扫描走完。`ran=false` 时 `outcome` 为 `skipped`、`timed_out` 或 `data_error`，并写入 `warnings`；此时没有命中不代表这一项没问题。
- `model_proof.status`：`no_conflict_found` 表示检查范围内走完且未发现冲突；检查中断时为 `inconclusive`，并在 `coverage.forcing_chain_search` 记录原因。

成功性质须有正流量，采用模型实际边界；`lb==ub` 的固定性质单列。贴边仅为显示阈值，不改变求解容差。N/X 计算值与硬约束对偶区分；边际符号沿用报表，仅作局部参考，不能解释成确定放宽收益。无法确认重算来源/漂移的边际不显示。

## 维护与回归

通用 skill 在 `.agents/skills/rps-input-diagnosis/`；可由支持仓库 skills 的客户端发现，其他客户端可直接读取 SKILL.md。原始内部 skill、案例手册及迁移版本依据放 `private/diagnosis_migration/`，不进入公开仓库。

纯内核与读文件适配分离；通过 `core/tooling.py` 注册，通用报告视图增加单值诊断指标，旧实验对比 KPI 保持原样。原有物料追溯、表达式扫描、经济对比入口不改行为。新测试覆盖证明复核、启停和路径守卫、失败证据、性质卡边、无优化调用、只读性、CLI 和 Web。

```bash
python -m pytest -q
python -m rps_tools regress check
```

真实用例只做本地保存结果的回放，不作为初值，不复制求解器二进制。任何新增规则都须区分确定冲突与候选，并附上不适用条件。
