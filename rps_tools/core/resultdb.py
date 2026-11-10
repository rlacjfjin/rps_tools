"""差异结果存储：边算边写 SQLite，页面按需分页 / 筛选 / 导出。

旧实现把全部差异攒在一个 list 里、整体 json.dump 到缓存、每次翻页再整体读回 ——
差异一多（几十万条）内存和响应时间都会爆。这里改成：
  * 对比器每产生一条差异就 sink.add()，按批 executemany 落盘，内存只保留一个批次；
  * 计数（按类型 / 按顶层 section / 按字段名）在写入时累计，超过 max_items 后只计数不落盘；
  * 读取端按 offset/limit + WHERE 查询，导出 CSV / JSON 用游标流式输出；
  * old/new 是截断后的展示文本，old_json/new_json 存完整原始值（JSON 文本），只有导出 JSON 时才读取。
"""
from __future__ import annotations

import json
import math
import sqlite3
from collections import Counter
from pathlib import Path
from typing import Iterator

MISSING = object()   # 某一侧不存在（新增/删除），区别于值为 null

KIND_LABELS = {
    "changed": "修改",
    "added": "新增",
    "removed": "删除",
    "type_changed": "类型变化",
}

_COLUMNS = ("kind", "path", "section", "field", "old", "new", "delta", "absdelta", "rel",
            "line_a", "line_b", "note")
_RAW_COLUMNS = ("old_json", "new_json")
_INSERT_COLUMNS = _COLUMNS + _RAW_COLUMNS
_PREVIEW_SELECT = ",".join(("id",) + _COLUMNS)   # 分页和 CSV 只读展示列，避免把完整大对象读进来

_SCHEMA = """
CREATE TABLE items (
    id INTEGER PRIMARY KEY,
    kind TEXT NOT NULL,
    path TEXT,
    section TEXT,
    field TEXT,
    old TEXT,
    new TEXT,
    delta REAL,
    absdelta REAL,
    rel REAL,
    line_a INTEGER,
    line_b INTEGER,
    note TEXT,
    old_json TEXT,
    new_json TEXT
);
"""


def _is_num(v) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool)


def preview(v, limit: int) -> str | None:
    if v is MISSING:
        return None
    if v is None:
        s = "null"
    elif isinstance(v, bool):
        s = "true" if v else "false"
    elif isinstance(v, str):
        s = v
    elif _is_num(v):
        s = repr(v)
    else:
        s = json.dumps(v, ensure_ascii=False, default=str)
        if isinstance(v, dict):
            s = f"{{{len(v)} 个键}} " + s
        elif isinstance(v, list):
            s = f"[{len(v)} 个元素] " + s
    if len(s) > limit:
        s = s[:limit] + f"…(共 {len(s)} 字符)"
    return s


def raw_json(v) -> str | None:
    """完整原始值的 JSON 文本；某一侧不存在时返回 None。NaN/Infinity 不是合法 JSON，转成字符串保存。"""
    if v is MISSING:
        return None
    try:
        return json.dumps(v, ensure_ascii=False, default=str, allow_nan=False)
    except ValueError:
        return json.dumps(str(v), ensure_ascii=False)


def export_item(row) -> str:
    """把一行差异拼成 JSON 对象文本。旧任务的库没有完整值，退回到展示文本；不存在的一侧不输出该键。"""
    columns = row.keys()
    dumps = lambda v: json.dumps(v, ensure_ascii=False)   # noqa: E731
    parts = [f'"{k}":{dumps(row[k])}' for k in ("kind", "path", "section", "field") if row[k] is not None]
    for side in ("old", "new"):
        raw = row[f"{side}_json"] if f"{side}_json" in columns else (
            None if row[side] is None else dumps(row[side]))
        if raw is not None:
            parts.append(f'"{side}":{raw}')
    parts += [f'"{k}":{dumps(row[k])}' for k in ("delta", "rel", "line_a", "line_b", "note") if row[k] is not None]
    return "{" + ",".join(parts) + "}"


class DiffSink:
    def __init__(self, db_path: Path, max_items: int = 2_000_000, preview_len: int = 300,
                 batch_size: int = 5000):
        self.db_path = Path(db_path)
        if self.db_path.exists():
            self.db_path.unlink()
        self.conn = sqlite3.connect(self.db_path)
        self.conn.execute("PRAGMA journal_mode=OFF")
        self.conn.execute("PRAGMA synchronous=OFF")
        self.conn.executescript(_SCHEMA)
        self.max_items = max_items
        self.preview_len = preview_len
        self.batch_size = batch_size
        self._batch: list[tuple] = []
        self.total = 0
        self.stored = 0
        self.by_kind: Counter = Counter()
        self.by_section: Counter = Counter()
        self.by_field: Counter = Counter()

    def add(self, kind: str, path: str = "", old=MISSING, new=MISSING, *, section: str | None = None,
            field: str | None = None, delta: float | None = None, line_a: int | None = None,
            line_b: int | None = None, note: str | None = None) -> None:
        self.total += 1
        self.by_kind[kind] += 1
        if section is not None:
            self.by_section[section] += 1
        if field is not None:
            self.by_field[field] += 1
        if self.stored >= self.max_items:
            return
        rel = None
        if delta is None and _is_num(old) and _is_num(new):
            delta = new - old
        if delta is not None:
            if math.isnan(delta) or math.isinf(delta):
                delta = None
        absdelta = abs(delta) if delta is not None else None
        if absdelta is not None and _is_num(old) and _is_num(new):
            scale = max(abs(old), abs(new))
            rel = absdelta / scale if scale else None
        self._batch.append((kind, path, section, field, preview(old, self.preview_len),
                            preview(new, self.preview_len), delta, absdelta, rel, line_a, line_b, note,
                            raw_json(old), raw_json(new)))
        self.stored += 1
        if len(self._batch) >= self.batch_size:
            self._flush()

    def _flush(self) -> None:
        if self._batch:
            self.conn.executemany(
                f"INSERT INTO items ({','.join(_INSERT_COLUMNS)}) VALUES ({','.join('?' * len(_INSERT_COLUMNS))})",
                self._batch)
            self._batch.clear()

    def close(self) -> dict:
        self._flush()
        for col in ("kind", "section", "field", "absdelta"):
            self.conn.execute(f"CREATE INDEX idx_{col} ON items({col})")
        self.conn.commit()
        self.conn.close()
        return self.summary()

    def summary(self) -> dict:
        return {
            "total": self.total,
            "stored": self.stored,
            "truncated": self.total > self.stored,
            "max_items": self.max_items,
            "by_kind": dict(self.by_kind.most_common()),
            "by_section": dict(self.by_section.most_common(300)),
            "by_field": dict(self.by_field.most_common(100)),
            "n_sections": len(self.by_section),
            "n_fields": len(self.by_field),
        }


SORTS = {
    "id": "id ASC",
    "absdelta": "absdelta IS NULL, absdelta DESC, id ASC",
    "rel": "rel IS NULL, rel DESC, id ASC",
    "path": "path ASC, id ASC",
}


class DiffStore:
    """只读查询端。"""

    def __init__(self, db_path: Path):
        self.db_path = Path(db_path)
        if not self.db_path.is_file():
            raise FileNotFoundError(db_path)

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(f"file:{self.db_path.as_posix()}?mode=ro", uri=True, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        return conn

    @staticmethod
    def _where(kind=None, section=None, field=None, q=None) -> tuple[str, list]:
        clauses, args = [], []
        if kind:
            kinds = [k for k in kind.split(",") if k]
            clauses.append(f"kind IN ({','.join('?' * len(kinds))})")
            args += kinds
        if section:
            clauses.append("section = ?")
            args.append(section)
        if field:
            clauses.append("field = ?")
            args.append(field)
        if q:
            like = f"%{q}%"
            clauses.append("(path LIKE ? OR old LIKE ? OR new LIKE ?)")
            args += [like, like, like]
        return ("WHERE " + " AND ".join(clauses)) if clauses else "", args

    def query(self, kind=None, section=None, field=None, q=None, sort="id",
              offset=0, limit=200) -> dict:
        where, args = self._where(kind, section, field, q)
        order = SORTS.get(sort, SORTS["id"])
        limit = max(1, min(int(limit), 2000))
        # 注意：sqlite3 连接的 with 只管事务不关连接，必须显式 close，否则 Windows 上文件一直被占用、任务目录删不掉
        conn = self._connect()
        try:
            total = conn.execute(f"SELECT COUNT(*) FROM items {where}", args).fetchone()[0]
            rows = conn.execute(
                f"SELECT {_PREVIEW_SELECT} FROM items {where} ORDER BY {order} LIMIT ? OFFSET ?",
                args + [limit, max(0, int(offset))]).fetchall()
        finally:
            conn.close()
        return {"total": total, "offset": offset, "limit": limit, "items": [dict(r) for r in rows]}

    def iter_rows(self, kind=None, section=None, field=None, q=None, sort="id",
                  full: bool = False) -> Iterator[sqlite3.Row]:
        """full=True 时带上完整原始值（old_json/new_json，旧任务的库没有这两列）。"""
        where, args = self._where(kind, section, field, q)
        order = SORTS.get(sort, SORTS["id"])
        columns = "*" if full else _PREVIEW_SELECT
        conn = self._connect()
        try:
            yield from conn.execute(f"SELECT {columns} FROM items {where} ORDER BY {order}", args)
        finally:
            conn.close()

    def has_full_values(self) -> bool:
        conn = self._connect()
        try:
            return "old_json" in {r["name"] for r in conn.execute("PRAGMA table_info(items)")}
        finally:
            conn.close()
