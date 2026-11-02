# -*- coding: utf-8 -*-
"""SQLite 访问层：只读校验、执行、结果归一化与指纹。

设计要点
- 只允许单条 SELECT / WITH 语句，避免 Agent 生成的 SQL 把库写坏
- 执行前用 EXPLAIN 预检语法，语法错直接抛 SqlError，交给 Agent 的修复节点
- 结果归一化后再比对：浮点保留 2 位、行排序、None → ""，避免"数值对但顺序不同"被误判
"""
from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

WRITE_RX = re.compile(r"\b(insert|update|delete|drop|alter|create|replace|attach|pragma|vacuum)\b", re.I)


class SqlError(Exception):
    """SQL 语法或语义错误（由 Agent 捕获并触发重写）。"""


@dataclass
class QueryResult:
    sql: str
    columns: list[str] = field(default_factory=list)
    rows: list[tuple] = field(default_factory=list)
    elapsed_ms: float = 0.0

    @property
    def rowcount(self) -> int:
        return len(self.rows)

    def to_dict(self) -> dict[str, Any]:
        return {
            "columns": self.columns,
            "rows": [list(r) for r in self.rows[:200]],
            "rowcount": self.rowcount,
            "elapsed_ms": round(self.elapsed_ms, 2),
        }

    def fingerprint(self, limit: int = 50) -> str:
        """结果指纹：用于评测时比对两条 SQL 是否等价（不比对 SQL 文本）。"""
        norm = normalize_rows(self.rows)[:limit]
        payload = json.dumps({"cols": len(self.columns), "rows": norm}, ensure_ascii=False, sort_keys=True)
        return hashlib.sha256(payload.encode()).hexdigest()[:16]


def normalize_rows(rows: list[tuple], ndigits: int = 2) -> list[list[Any]]:
    out = []
    for r in rows:
        row = []
        for v in r:
            if isinstance(v, float):
                row.append(round(v, ndigits))
            elif v is None:
                row.append("")
            else:
                row.append(v)
        out.append(row)
    out.sort(key=lambda x: [str(i) for i in x])
    return out


class Database:
    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        if not self.path.exists():
            raise FileNotFoundError(f"数据库不存在：{self.path}，先跑 python data/make_db.py")

    def _conn(self) -> sqlite3.Connection:
        conn = sqlite3.connect(f"file:{self.path}?mode=ro", uri=True)
        conn.row_factory = None
        return conn

    @staticmethod
    def check(sql: str) -> str:
        """静态校验：只允许单条只读语句。"""
        s = sql.strip().rstrip(";")
        if not s:
            raise SqlError("SQL 为空")
        if ";" in s:
            raise SqlError("不允许多条语句")
        if not re.match(r"^(select|with)\b", s, re.I):
            raise SqlError("只允许 SELECT / WITH 查询")
        if WRITE_RX.search(s):
            raise SqlError("检测到写操作关键字")
        return s

    def dry_run(self, sql: str) -> None:
        """语法预检：EXPLAIN 不执行查询，只做编译，能提前暴露列名/表名错误。"""
        sql = self.check(sql)
        conn = self._conn()
        try:
            conn.execute("EXPLAIN " + sql)
        except sqlite3.Error as exc:
            raise SqlError(f"{exc}") from exc
        finally:
            conn.close()

    def run(self, sql: str, max_rows: int = 5000) -> QueryResult:
        sql = self.check(sql)
        t0 = time.perf_counter()
        conn = self._conn()
        try:
            cur = conn.execute(sql)
            cols = [d[0] for d in (cur.description or [])]
            rows = cur.fetchmany(max_rows)
        except sqlite3.Error as exc:
            raise SqlError(f"{exc}") from exc
        finally:
            conn.close()
        return QueryResult(sql=sql, columns=cols, rows=[tuple(r) for r in rows],
                           elapsed_ms=(time.perf_counter() - t0) * 1000)
