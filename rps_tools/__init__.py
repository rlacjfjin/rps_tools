"""rps_tools —— rps_solver 配套的事后诊断工具集。

三条功能线，统一以「工具 (Tool)」的形式注册，CLI 与 Web 服务共用同一份实现：

  diff        文件差异对比（JSON 结构化 / 文本按行 / MPS 结构化，可扩展）
  exp         实验级因果链分析（exp_analyzer, R1-R13 报告 + 因果图）
  diagnose    物料 / 约束级诊断（material_trace / scan_export / parse_expressions / compare_scenarios）

入口：
  python -m rps_tools serve            启动 Web 服务
  python -m rps_tools tools            列出所有工具
  python -m rps_tools run <tool> k=v   命令行直接跑某个工具
  python -m rps_tools diff A B         命令行文件对比
"""

__version__ = "0.2.0"
