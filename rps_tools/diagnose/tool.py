"""把 4 个诊断脚本注册成 Web/CLI 工具。

脚本本身保持「命令行 + print 文本报告」的形态不变（仍可 python -m rps_tools.diagnose.xxx 单独跑）；
这里只负责：参数表 → argv，并把打印输出另存一份 output.txt 作为结果。
后续把某个脚本改成返回结构化数据时，只需要改它对应的 run 函数和 view。
"""
from __future__ import annotations

import importlib
import sys
from contextlib import redirect_stdout

from rps_tools.core.tooling import JobContext, Param, Tool, register

S_FILES = "数据文件"
EXPORT_DIR_HELP = "rps_solver 的 export 目录。填了它，下面没指定的文件会自动取目录里最新的 *_model_input / *_model_output / *_proto_expression"


class _Tee:
    def __init__(self, *streams):
        self.streams = streams

    def write(self, s):
        for st in self.streams:
            st.write(s)
        return len(s)

    def flush(self):
        for st in self.streams:
            st.flush()

    def reconfigure(self, **kwargs):   # 脚本 import 时会调 sys.stdout.reconfigure
        pass


def _argv(p: dict, mapping: list[tuple[str, str]]) -> list[str]:
    argv = []
    for name, flag in mapping:
        v = p.get(name)
        if v is None or v == "":
            continue
        argv += [flag, str(v)]
    return argv


def _make_runner(module: str, mapping: list[tuple[str, str]]):
    def run(ctx: JobContext, p: dict) -> dict:
        mod = importlib.import_module(f"rps_tools.diagnose.{module}")
        argv = _argv(p, mapping)
        print(f"$ python -m rps_tools.diagnose.{module} {' '.join(argv)}\n", flush=True)
        ctx.progress(10, f"运行 {module}")
        out_path = ctx.out_dir / "output.txt"
        with open(out_path, "w", encoding="utf-8") as fp, redirect_stdout(_Tee(sys.stdout, fp)):
            mod.main(argv)
        text = out_path.read_text(encoding="utf-8")
        lines = text.splitlines()
        return {
            "output": "output.txt",
            "lines": len(lines),
            "warnings": sum(1 for ln in lines if "⚠" in ln),
            "highlights": sum(1 for ln in lines if "★" in ln),
        }
    return run


def _files(*names_labels) -> list[Param]:
    out = [Param("export_dir", "export 目录", type="dir", section=S_FILES, help=EXPORT_DIR_HELP)]
    for name, label, accept in names_labels:
        out.append(Param(name, label, type="file", accept=accept, section=S_FILES))
    return out


JSON = [".json"]
TXT = [".txt"]

register(Tool(
    id="diagnose.scan_export", group="diagnose", order=1,
    title="全量扫描 scan_export",
    description="扫描所有物料，列出 dumping≠0 / 属性被锁 0 / property 为空等异常，并自动分类 [DEAD-SIDE] / [PUSH] / [MIXED]。接到新故障先跑这个。",
    params=_files(("input", "model_input", JSON), ("output", "model_output", JSON),
                  ("proto", "proto_expression.txt", TXT)) + [
        Param("limit", "每类最多显示", type="int", default=50),
    ],
    run=_make_runner("scan_export", [("export_dir", "--export-dir"), ("input", "--input"),
                                     ("output", "--output"), ("proto", "--proto"), ("limit", "--limit")]),
))

register(Tool(
    id="diagnose.material_trace", group="diagnose", order=2,
    title="单物料追根因 material_trace",
    description="针对一个物料：生产者/消费者、blend 下游、属性变量是否被锁、上游 property 是否缺失，覆盖 dead-side 和 push 两类甩货。",
    params=[Param("material", "物料 code", required=True, placeholder="物料 code")] + _files(
        ("input", "model_input", JSON), ("output", "model_output", JSON), ("proto", "proto_expression.txt", TXT)) + [
        Param("depth", "上游递归深度", type="int", default=3),
    ],
    run=_make_runner("material_trace", [("export_dir", "--export-dir"), ("input", "--input"),
                                        ("output", "--output"), ("proto", "--proto"),
                                        ("material", "--material"), ("depth", "--depth")]),
))

register(Tool(
    id="diagnose.parse_expressions", group="diagnose", order=3,
    title="表达式结构分析 parse_expressions",
    description="只解析 proto_expression.txt：锁死变量闭包、WBAL 单侧死区（静默甩货指纹）、退化等式、bound 冲突。",
    params=[
        Param("export_dir", "export 目录", type="dir", section=S_FILES, help=EXPORT_DIR_HELP),
        Param("proto", "proto_expression.txt", type="file", accept=TXT, section=S_FILES),
        Param("material", "聚焦物料（可选）", placeholder="物料 code"),
        Param("limit", "每类最多显示", type="int", default=20),
    ],
    run=_make_runner("parse_expressions", [("export_dir", "--export-dir"), ("proto", "--proto"),
                                           ("limit", "--limit"), ("material", "--material")]),
))

register(Tool(
    id="diagnose.compare_scenarios", group="diagnose", order=4,
    title="双场景对比 compare_scenarios",
    description="两次求解的经济性差异：OBJ 分解、customVariable 变化、甩货翻转、case 撞顶翻转、V_path 路径价值、敏感性排行。",
    params=[
        Param("before", "before model_output", type="file", accept=JSON, required=True, section=S_FILES),
        Param("after", "after model_output", type="file", accept=JSON, required=True, section=S_FILES),
        Param("before_in", "before model_input", type="file", accept=JSON, section=S_FILES,
              help="不填时自动在 output 同目录找 *_model_input*（仅「服务器路径」方式有效，上传方式请一起上传）"),
        Param("after_in", "after model_input", type="file", accept=JSON, section=S_FILES),
        Param("material", "聚焦物料（可选）"),
        Param("top", "敏感性 Top N", type="int", default=15),
        Param("dump_threshold", "甩货阈值", type="float", default=1e-4, advanced=True),
    ],
    run=_make_runner("compare_scenarios", [("before", "--before"), ("after", "--after"),
                                           ("before_in", "--before-in"), ("after_in", "--after-in"),
                                           ("material", "--material"), ("top", "--top"),
                                           ("dump_threshold", "--dump-threshold")]),
))
