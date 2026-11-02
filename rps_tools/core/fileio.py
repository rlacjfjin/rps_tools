"""文件读取：编码自动识别 + 快速 JSON 解析。"""
from __future__ import annotations

import json
from pathlib import Path

try:
    import orjson as _orjson
except ImportError:  # orjson 是可选依赖，没装就退回标准库
    _orjson = None

ENCODINGS = ("utf-8-sig", "gb18030", "latin-1")


def decode_bytes(raw: bytes) -> str:
    for enc in ENCODINGS:
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", errors="replace")


def read_text(path: str | Path) -> str:
    return decode_bytes(Path(path).read_bytes())


def load_json(path: str | Path):
    """读 JSON。优先 orjson（快 3-5 倍、峰值内存更低），非 UTF-8 文件退回标准库。"""
    raw = Path(path).read_bytes()
    if raw.startswith(b"\xef\xbb\xbf"):
        raw = raw[3:]
    if _orjson is not None:
        try:
            return _orjson.loads(raw)
        except _orjson.JSONDecodeError:
            pass  # 可能是 GBK 编码或 NaN/Infinity 之类的非标准 JSON，交给标准库
    return json.loads(decode_bytes(raw))


def dumps_sorted(obj) -> str:
    """稳定序列化（键排序），用于把 JSON 元素当作可比较/可哈希的整体。"""
    if _orjson is not None:
        try:
            return _orjson.dumps(obj, option=_orjson.OPT_SORT_KEYS).decode("utf-8")
        except TypeError:
            pass
    return json.dumps(obj, sort_keys=True, ensure_ascii=False, default=str)


def human_size(n: int) -> str:
    size = float(n)
    for unit in ("B", "KB", "MB", "GB"):
        if size < 1024 or unit == "GB":
            return f"{size:.0f} {unit}" if unit == "B" else f"{size:.1f} {unit}"
        size /= 1024
    return f"{n} B"
