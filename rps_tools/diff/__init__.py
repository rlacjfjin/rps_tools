"""文件差异对比。导入下面的模块即完成对比器注册（顺序 = 页面上的显示顺序）；新增格式在这里加一行 import。"""
from rps_tools.diff import json_cmp  # noqa: F401
from rps_tools.diff import text_cmp  # noqa: F401
from rps_tools.diff import mps_cmp  # noqa: F401
from rps_tools.diff.base import COMPARATORS, detect_format  # noqa: F401
