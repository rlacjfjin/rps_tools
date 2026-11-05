# rps_tools

为 [rps_solver](../rps_solver) 配套的**事后诊断工具集**，带 Web 界面。它不调用求解器，只读已经跑好的 input / output / proto_expression 文件。

| # | 功能 | 回答的问题 | 代码 |
|---|---|---|---|
| 1 | **文件差异对比** | 两份 JSON / 文本 / MPS / Aspen PIMS 解报告到底哪里不一样 | `rps_tools/diff/` |
| 2 | **实验级因果链分析** | 改了 A，OBJ 变化的因果链路是什么 | `rps_tools/exp_analyzer/` |
| 3 | **物料 / 约束级诊断** | 为什么 X 物料被甩货 / 锁 0 / 凭空消耗；两次场景差在哪 | `rps_tools/diagnose/` |

---

## 快速开始

```bash
python -m venv .venv
```

```bash
.venv\Scripts\python -m pip install -r requirements.txt
```

```bash
.venv\Scripts\python -m rps_tools serve
```

浏览器打开 http://127.0.0.1:8765 。在 Windows 上也可以直接双击 `start_server.bat`，第一次运行会自动建好 `.venv`。

- **局域网共享**：加上 `--host 0.0.0.0`。这时「服务器路径」只能读数据目录（experiments/、export/、../rps_solver/export、`--data-root` 指定的目录），不能读整台机器。
- **接口文档**：http://127.0.0.1:8765/docs ，可以直接在网页里调接口。
- **任务产物**：放在 `workspace/jobs/<任务ID>/`，默认保留 14 天（`--retention-days` 可调）。

### 命令行（和 Web 用的是同一份实现）

```bash
python -m rps_tools tools                       # 列出所有工具
python -m rps_tools tools diff.compare          # 看某个工具的全部参数
python -m rps_tools diff A.json B.json json.abs_tol=1e-6 --csv diff.csv
python -m rps_tools diff FullSolution_A.htm FullSolution_B.htm   # PIMS 解报告，产物里另有 summary.md
python -m rps_tools run exp.analyze exp_dir=experiments/<实验名>
python -m rps_tools run diagnose.material_trace material=<物料code> export_dir=../rps_solver/export
```

原来的入口仍然可用：`python -m rps_tools.exp_analyzer <dir>`、`python -m rps_tools.diagnose.scan_export --export-dir export` 等。

### Aspen PIMS 解报告对比

在「文件差异对比」里选两份 PIMS 导出的 `FullSolution*.htm`（文件名含 FullSolution 时自动识别，也可以手动选「Aspen PIMS 解报告对比」）。结果页有两部分：

- **分析摘要**（`summary.md`）：解读提示、概览（模型 / Case / 目标函数）、经济汇总、输入参数核对（两边都有的行里上下限 / 价格是否一致）、采购 / 销售 / 公用工程变化、装置启停与进料变化、约束紧松变化（新卡住 / 不再卡住 / 边际值变化）、调合配方、装置负荷、报告定义差异（行描述、子段标题、能力罚项 `*` 标记、只在一侧出现的物性列）。
- **逐单元格差异**：路径是 `报告/子段/表/行代码/列`，可以按报告、列名筛选，也能导出 CSV。

匹配规则：

- 行按代码匹配，不看描述和行顺序，也不受分页影响（宽矩阵同一行分在几张表里也能对上）。描述的变化单独记在「(描述)」里。
- 空白和 0 视为相同。同一个数两边显示的小数位数不同（5.12 和 5.116）时，按较粗的精度比较。
- 红色标记（`*` 表示递推物性或能力罚项，`+` 表示 PCALC）和数值分开存。

13 MB 的报告解析一份大约 6 秒。

---

## 架构

```
浏览器 (static/app.js)  ──HTTP──▶  FastAPI (web/app.py)
   表单由参数表自动生成                │  POST /api/tools/{id}/jobs：保存上传文件 / 校验服务器路径
                                      ▼
                             JobManager (core/jobs.py)
                                      │  每个任务一个独立工作进程（spawn），最多 N 个并发
                                      ▼
                    Tool.run(ctx, params)  ←── 工具注册表 (core/tooling.py)
                       ├─ diff.compare          → 对比器 (diff/*.py) → 差异边算边写 SQLite
                       ├─ exp.analyze           → exp_analyzer.run_analysis → report.md / *.html / *.csv
                       └─ diagnose.*            → 原诊断脚本 main(argv) → output.txt
```

- **工具 = 参数表 + run 函数**。参数表同时驱动 Web 表单、HTTP 接口校验和 CLI 的 `k=v` 解析。新增一个工具不需要改前端。
- **任务都在后台进程里跑**。页面只做提交、轮询进度、看日志和查询结果。任务可以取消；进程即使因为内存不足被系统杀掉，也只是这个任务失败，服务本身不受影响。
- **两种输入方式**：
  - 上传：文件流式落盘，不整体读进内存。
  - 服务器路径：服务和 rps_solver 在同一台机器上时直接读文件，省掉上传。

### 大文件为什么以前会崩，现在怎么处理

| 问题（旧 compare_file_tools） | 现在 |
|---|---|
| 在 HTTP 请求里同步计算，大文件一跑几分钟，浏览器/代理超时，页面看起来就是"崩了" | 提交后立即返回任务 ID，后台进程计算，页面轮询进度和日志 |
| 文本对比用 `difflib.SequenceMatcher` 整文件对齐，接近 O(N²)。43 万行的文件跑了 150 秒还没出结果 | patience diff + Myers 兜底：同样的输入 **1.8 秒**，结果精确 |
| JSON 列表按下标比较：materialList 顺序一变、或插入/删除一个装置，后面所有元素错位，报出成千上万条假差异 | 列表按主键匹配（code/id/...，找不到时从元素字段自动推断组合主键，例如 `src+dst+kind`） |
| 全部差异攒在内存 list → 整体写成一个 JSON 缓存 → 每次翻页再整体读回 | 差异边算边写 SQLite；页面分页、筛选、排序都在服务端做，CSV 流式导出 |
| JSON 用标准库解析 | 优先用 orjson（快 3-5 倍，内存更省），没装就自动退回标准库 |

结论：**HTTP 本身没问题，问题在于同步处理和结果一次性加载**。改成「异步任务 + 服务器路径 + 分页查询」之后，几十到几百 MB 的文件都能稳定处理。

---

## 扩展

### 新增一种文件对比格式（比如 proto_expression.txt 的结构化对比）

1. 在 `rps_tools/diff/` 下新建模块，继承 `Comparator`。参考 `mps_cmp.py`，它是最简单的完整示例：
   ```python
   @register_comparator
   class ProtoComparator(Comparator):
       name = "proto"
       title = "proto_expression 结构化对比"
       filename_patterns = ("*proto_expression*.txt",)   # 自动识别
       options = [Param("abs_tol", "数值绝对容差", type="float", default=0.0)]

       def compare(self, path_a, path_b, opts, sink, progress):
           ...  # 每发现一处差异调用 sink.add(kind, path, old, new, section=..., field=...)
           return {...}   # 额外统计，显示在结果页
   ```
2. 在 `rps_tools/diff/__init__.py` 里加一行 import。
3. 完成。Web 的「对比方式」下拉框、自动识别、选项表单、结果分页、CSV 导出都会自动生效。

### 新增 / 改造一个分析工具

在对应功能目录的 `tool.py` 里 `register(Tool(...))`：

- `params`：参数表
- `run(ctx, params) -> dict`：在工作进程里执行。`print` 的内容会进任务日志，`ctx.progress(pct, msg)` 用来更新进度条，产物写进 `ctx.out_dir`。
- `view`：结果页的渲染方式。`diff` 是分页差异表，`report` 是 KPI + 产物按钮 + 渲染后的 report.md，`text` 是可搜索的文本输出。

诊断脚本以后改成返回结构化数据时，只需要改 `diagnose/tool.py` 里对应的 run 函数和 view。

---

## 目录

```
rps_tools/
├── README.md / requirements.txt / pyproject.toml
├── start_server.bat / run_server.py      启动脚本
├── rps_tools/
│   ├── cli.py                            统一命令行
│   ├── config.py                         工作目录 / 数据目录 / 路径访问控制
│   ├── core/  tooling.py jobs.py resultdb.py fileio.py
│   ├── diff/  base.py json_cmp.py text_cmp.py mps_cmp.py tool.py
│   │          pims_report.py（PIMS htm 解析）pims_cmp.py（对比器）pims_summary.py（分析摘要）
│   ├── exp_analyzer/   原 tools/exp_analyzer
│   ├── diagnose/       原 tools/diagnose
│   └── web/  app.py  static/{index.html, app.js, app.css}
├── tests/                                pytest：对比算法 + 端到端 HTTP
├── scripts/privacy_check.py              提交前数据隐私检查（pre-commit 钩子）
├── .claude/agents/                       4 个角色定义（领域专家 / 架构师 / 回归测试 / 代码审查）
├── private/                              本地专用：回归基线、敏感词缓存（不进仓库）
├── experiments/                          本地实验数据（不进仓库，见下方「数据隐私」）
├── export/                               本地 rps_solver 导出数据（不进仓库）
├── pims-results/                         本地 Aspen PIMS 解报告 FullSolution*.htm（不进仓库）
└── workspace/                            任务产物（可随时删除）
```

各功能的详细说明：

- [rps_tools/exp_analyzer/README.md](rps_tools/exp_analyzer/README.md)：patch.json schema、R1-R13 报告说明

下面这些文档包含真实案例（物料/装置代码、经济数值），**只保存在本地，不进仓库**（见 `.gitignore`）：
`rps_tools/exp_analyzer/TRIAGE.md`（看结果的 SOP）、`rps_tools/exp_analyzer/PROJECT.md`（算法与设计决策）、
`rps_tools/diagnose/README.md`（物料诊断方法论）、`rps_tools/diagnose/CAPABILITIES.md`（诊断工具能力边界）。

## 测试

```bash
.venv\Scripts\python -m pytest -q
```

测试覆盖以下内容：

- JSON 主键匹配、容差、忽略规则、重复元素
- 文本对比在大文件和高度重复内容上的精度与速度
- MPS 结构化对比
- PIMS 解报告对比：两行表头、红色标记、分页宽矩阵、按代码匹配、分析摘要（合成报告；`pims-results/` 有真实报告时再跑一遍冒烟）
- 通过 HTTP 跑通三类工具（`experiments/`、`export/` 有本地数据时自动使用，没有就跳过这几项）
- 工作进程报错时能正确回报

### 本地真实数据回归

真实数据不进仓库，所以「改完以后结果有没有悄悄变了」靠本地回归来保证。基线保存在 `private/regress/`（已忽略）：

```bash
python -m rps_tools regress list        # 自动发现的用例：experiments/ 每个完整实验、export/ 的场景对比等
python -m rps_tools regress snapshot    # 改算法之前：用当前代码生成基线
python -m rps_tools regress check       # 改完之后：重新跑一遍，与基线逐文件对比
```

`report.json` 用 JSON 结构化对比（列表按主键匹配），其余 `.md/.txt/.csv/.mmd` 用文本对比。每个用例在子进程里跑并固定 `PYTHONHASHSEED`，输出顺序是稳定的。
有差异时退出码为 1，详情写在 `private/regress/last_check.md`。确认差异都是预期内的改进后，再 `snapshot` 更新基线。

## 数据隐私

仓库是公开的，**任何求解数据和由数据生成的产物都不能提交**。`.gitignore` 做了三层防护：

1. `experiments/`、`export/`、`pims-results/`、`workspace/`、`private/` 整目录忽略（只保留目录说明 README）；
2. 全局按扩展名兜底：`*.json` `*.mps` `*.xmps` `*.lp` `*.txt` `*.csv` `*.xlsx` `*.html` `*.htm` `*.mmd` `*.sqlite` 等，放错目录也不会被提交；
   项目自身需要的少数文件单独白名单（`requirements.txt`、`web/static/index.html`、脱敏后的 `sample_patch.json`）；
3. 含真实案例的内部文档只留本地。

另外有一个**提交前自动检查**（pre-commit 钩子），clone 之后先装上：

```bash
python scripts/privacy_check.py --install
```

每次 `git commit` 都会检查暂存区，命中任意一项就拦下提交：

- 数据目录、数据类型文件（包括 `git add -f` 强制加的）、超过 1 MB 的文件
- 只留本地的内部文档
- **本地数据里出现过的真实物料/装置代码和 OBJ 等数值**：从 `experiments/`、`export/` 自动提取，缓存在 `private/`
- 本机用户目录路径、QQ 邮箱，以及非 GitHub noreply 的提交邮箱

如果命中的是通用词（比如 SUL、DEN 这类性质代码），把它加进 `scripts/privacy_allowlist.txt`。也可以手动跑 `python scripts/privacy_check.py --all` 检查所有已跟踪文件。
代码、注释、测试里的示例一律用 `MAT-A`、`U1` 这类虚构代码。

## 角色分工（Claude Code 子代理）

`.claude/agents/` 下定义了 4 个角色，在 Claude Code 里按名字调用，或者由主会话按描述自动分派：

| 角色 | 什么时候用 | 产出 | 权限 |
|---|---|---|---|
| `domain-expert` 领域专家 | 改了诊断/归因算法，或对报告结论有疑问 | 逐条判定「成立 / 存疑 / 不成立」，附理论依据和验证方法 | 只读 |
| `architect` 架构师 | 加新功能或做大重构之前 | 落在现有扩展点上的设计方案、改动步骤、测试计划 | 只读 |
| `regression-tester` 回归测试 | 改完、提交之前 | pytest 和 `regress check` 结果，判断每处差异是预期改进还是回退；用合成数据补测试 | 可运行命令，只写 `tests/` |
| `code-reviewer` 代码审查 | 提交之前 | 按严重程度排序的问题清单 | 只读 |

开发由主会话完成，需求和优先级由使用者自己定。推荐流程：

```
需求 → architect 出方案（大改动才需要）→ 开发
     → regression-tester（pytest + regress check）→ domain-expert（算法改动时）
     → code-reviewer → pre-commit 隐私检查 → 提交
```

小改动直接走「开发 → 测试 → 审查」。

## 数据来源

- `*_model_input.json` / `*_model_output.json`：rps_solver export 出来的拓扑和解
- `*_proto_expression.txt`：rps_solver mp_interface 导出的 LP 数学表达式

服务会自动把 `../rps_solver/export`（如果存在）加入可浏览的数据目录。其它位置用 `--data-root` 或环境变量 `RPS_TOOLS_DATA_ROOTS` 添加。
