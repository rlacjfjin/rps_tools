"""工具注册表：每个功能声明成一个 Tool（参数表 + run 函数），CLI / Web 自动复用。

新增一个工具只需要：
  1. 写一个 run(ctx, params) -> dict 函数（在独立的工作进程里执行，print 的内容会进任务日志）
  2. 用 register(Tool(...)) 声明参数
  3. 把模块加进 TOOL_MODULES
Web 页面会根据参数表自动生成表单，无需改前端。
"""
from __future__ import annotations

import importlib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

# 三条功能线（Web 首页的三张卡片 / 顶部导航）
GROUPS = [
    {"id": "diff", "title": "文件差异对比",
     "description": "两份文件逐项对比：JSON 结构化（按主键匹配列表）、文本按行、MPS 按矩阵元素。大文件后台执行，结果分页查询。"},
    {"id": "exp", "title": "实验级因果链分析",
     "description": "baseline vs modified 两次求解结果：回答「改了 A，OBJ 变化的因果链路是什么」。输出 R1-R13 报告 + 因果图。"},
    {"id": "diagnose", "title": "物料 / 约束级诊断",
     "description": "回答「为什么 X 物料被甩货 / 锁 0 / 凭空消耗」，以及两次场景的经济性差异。"},
]

TOOL_MODULES = (
    "rps_tools.diff.tool",
    "rps_tools.exp_analyzer.tool",
    "rps_tools.diagnose.tool",
)

PARAM_TYPES = {"file", "dir", "str", "text", "int", "float", "bool", "choice", "multi"}


class ParamError(ValueError):
    pass


@dataclass
class Param:
    name: str
    label: str
    type: str = "str"
    required: bool = False
    default: Any = None
    help: str = ""
    choices: list[tuple[str, str]] | None = None   # [(value, label)]
    accept: list[str] | None = None                # file: 建议的扩展名，如 [".json"]
    section: str = ""                              # 表单分组标题
    advanced: bool = False                         # 折叠到「高级选项」
    placeholder: str = ""
    show_if: dict[str, list[str]] | None = None    # 仅当其它参数取这些值时才显示，例 {"format": ["json"]}

    def __post_init__(self):
        if self.type not in PARAM_TYPES:
            raise ValueError(f"unknown param type {self.type!r} for {self.name}")

    def spec(self) -> dict:
        return {
            "name": self.name, "label": self.label, "type": self.type,
            "required": self.required, "default": self.default, "help": self.help,
            "choices": [{"value": v, "label": l} for v, l in (self.choices or [])],
            "accept": self.accept or [], "section": self.section,
            "advanced": self.advanced, "placeholder": self.placeholder,
            "show_if": self.show_if,
        }

    def coerce(self, raw: Any) -> Any:
        """把表单 / 命令行传来的原始值转成目标类型；空值返回 default。"""
        if self.type == "multi":
            if raw is None or raw == "" or raw == []:
                values = []
            elif isinstance(raw, str):
                values = [v.strip() for v in raw.split(",") if v.strip()]
            else:
                values = [str(v) for v in raw if str(v).strip()]
            if not values:
                values = list(self.default or [])
            if self.choices:
                allowed = {v for v, _ in self.choices}
                bad = [v for v in values if v not in allowed]
                if bad:
                    raise ParamError(f"「{self.label}」不支持的取值：{bad}")
            return values
        if self.type == "bool":
            if isinstance(raw, bool):
                return raw
            if raw is None or raw == "":
                return bool(self.default)
            return str(raw).strip().lower() in ("1", "true", "on", "yes", "y")
        if raw is None or (isinstance(raw, str) and raw.strip() == ""):
            return self.default
        if self.type == "int":
            try:
                return int(float(raw))
            except ValueError:
                raise ParamError(f"「{self.label}」需要整数，收到 {raw!r}")
        if self.type == "float":
            try:
                return float(raw)
            except ValueError:
                raise ParamError(f"「{self.label}」需要数字，收到 {raw!r}")
        if self.type == "choice":
            value = str(raw)
            if self.choices and value not in {v for v, _ in self.choices}:
                raise ParamError(f"「{self.label}」不支持的取值：{value!r}")
            return value
        return str(raw) if self.type != "text" else str(raw).replace("\r\n", "\n")


@dataclass
class Tool:
    id: str
    group: str
    title: str
    description: str
    params: list[Param]
    run: Callable[["JobContext", dict], dict | None]
    view: str = "text"        # 结果页渲染方式：diff / report / text
    order: int = 0
    validate: Callable[[dict], None] | None = None   # 提交前的跨字段校验，抛 ParamError
    ui: dict = field(default_factory=dict)            # 给前端的额外提示（可选）

    def spec(self) -> dict:
        return {"id": self.id, "group": self.group, "title": self.title,
                "description": self.description, "view": self.view, "ui": self.ui,
                "params": [p.spec() for p in self.params]}

    def coerce_params(self, raw: dict) -> dict:
        out = {}
        for p in self.params:
            value = p.coerce(raw.get(p.name))
            if p.required and (value is None or value == "" or value == []):
                raise ParamError(f"缺少必填参数「{p.label}」")
            out[p.name] = value
        if self.validate:
            self.validate(out)
        return out


class JobContext:
    """传给 Tool.run 的上下文：产物目录 + 进度上报。"""

    def __init__(self, job_dir: Path, emit: Callable[[dict], None] | None = None):
        self.job_dir = Path(job_dir)
        self.out_dir = self.job_dir / "output"
        self.out_dir.mkdir(parents=True, exist_ok=True)
        self._emit = emit
        self._last_msg = None

    def progress(self, pct: float | None, msg: str = "") -> None:
        if msg and msg != self._last_msg:
            print(f"[进度 {pct:.0f}%] {msg}" if pct is not None else f"[进度] {msg}", flush=True)
            self._last_msg = msg
        if self._emit:
            self._emit({"type": "progress", "pct": pct, "msg": msg})


_REGISTRY: dict[str, Tool] = {}
_loaded = False


def register(tool: Tool) -> Tool:
    if tool.group not in {g["id"] for g in GROUPS}:
        raise ValueError(f"unknown group {tool.group!r}")
    _REGISTRY[tool.id] = tool
    return tool


def load_all() -> dict[str, Tool]:
    global _loaded
    if not _loaded:
        for mod in TOOL_MODULES:
            importlib.import_module(mod)
        _loaded = True
    return _REGISTRY


def get_tool(tool_id: str) -> Tool:
    tools = load_all()
    if tool_id not in tools:
        raise KeyError(tool_id)
    return tools[tool_id]


def all_tools() -> list[Tool]:
    group_order = {g["id"]: i for i, g in enumerate(GROUPS)}
    return sorted(load_all().values(), key=lambda t: (group_order[t.group], t.order, t.id))
