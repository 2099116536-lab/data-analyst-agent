# -*- coding: utf-8 -*-
"""评测：执行准确率（结果级比对）、边界识别率、表命中率、延迟分位。

为什么用"执行结果指纹"而不是比对 SQL 文本
- 同一口径可以写出多条等价 SQL，比文本会冤枉正确答案；
- 结果指纹同时能抓住"SQL 能跑但口径错"（比如漏了状态过滤）这类最危险的问题。
"""
from __future__ import annotations

import json
import re
import statistics
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .agent import DataAnalystAgent
from .db import Database, SqlError

ROOT = Path(__file__).resolve().parent.parent


@dataclass
class Case:
    id: str
    question: str
    gold_sql: str
    type: str = "agg"
    split: str = "dev"
    tables: list[str] = field(default_factory=list)


@dataclass
class CaseResult:
    case: Case
    passed: bool
    status: str
    sql: str
    latency_ms: float
    reason: str = ""


def load_cases(*paths: str | Path) -> list[Case]:
    cases: list[Case] = []
    for p in paths:
        p = Path(p)
        if not p.exists():
            continue
        for line in p.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            d = json.loads(line)
            cases.append(Case(
                id=d.get("id", ""), question=d["question"], gold_sql=d.get("gold_sql", ""),
                type=d.get("type", "agg"), split=d.get("split", "dev"),
                tables=d.get("tables", []),
            ))
    return cases


def run_eval(agent: DataAnalystAgent, cases: list[Case]) -> dict[str, Any]:
    db = agent.db
    results: list[CaseResult] = []
    for c in cases:
        t0 = time.perf_counter()
        st = agent.run(c.question)
        ms = (time.perf_counter() - t0) * 1000

        if c.type in ("reject", "unsupported"):
            passed = st.status in ("rejected", "clarify")
            results.append(CaseResult(c, passed, st.status, st.sql, ms,
                                      "" if passed else f"未识别边界，状态={st.status}"))
            continue

        if st.status in ("rejected", "clarify", "failed"):
            results.append(CaseResult(c, False, st.status, st.sql, ms, f"未作答：{st.status}"))
            continue
        if st.result is None or not c.gold_sql:
            results.append(CaseResult(c, False, st.status, st.sql, ms, "无结果"))
            continue
        try:
            gold = db.run(c.gold_sql)
        except SqlError as exc:
            results.append(CaseResult(c, False, st.status, st.sql, ms, f"gold SQL 错误：{exc}"))
            continue
        same = st.result.fingerprint() == gold.fingerprint()
        results.append(CaseResult(c, same, st.status, st.sql, ms,
                                  "" if same else f"结果不一致（生成 {st.result.rowcount} 行 / 标准 {gold.rowcount} 行）"))

    # ---------- 汇总 ----------
    def acc(rs: list[CaseResult]) -> float:
        return round(sum(r.passed for r in rs) / len(rs) * 100, 1) if rs else 0.0

    by_split: dict[str, list[CaseResult]] = {}
    for r in results:
        by_split.setdefault(r.case.split, []).append(r)

    lat = sorted(r.latency_ms for r in results)
    p95 = lat[min(len(lat) - 1, int(len(lat) * 0.95))] if lat else 0.0

    # 表命中：schema 检索召回的表是否覆盖 gold 需要的表
    hit = [r for r in results if r.case.tables]
    table_hit = 0
    for r in hit:
        got = set()
        try:
            got = set(r.case.tables) & set(_tables_of(r.sql))
        except Exception:
            pass
        table_hit += len(got) == len(set(r.case.tables))

    full_tk = len(agent.schema.full_prompt())
    sub_tk = statistics.mean(
        [len(agent.schema.subset_prompt(c.question)[0]) for c in cases]) if cases else 0

    summary = {
        "total": len(results),
        "exec_accuracy": {
            k: {"acc": acc(v), "n": len(v)} for k, v in sorted(by_split.items())
        },
        "overall_acc": acc([r for r in results if r.case.type not in ("reject", "unsupported")]),
        "boundary_acc": acc([r for r in results if r.case.type in ("reject", "unsupported")]),
        "table_hit_rate": round(table_hit / len(hit) * 100, 1) if hit else 0.0,
        "schema_chars": {"full": full_tk, "retrieved_avg": round(sub_tk),
                         "saved_pct": round((1 - sub_tk / full_tk) * 100, 1) if full_tk else 0},
        "latency_ms": {"avg": round(statistics.mean(lat), 1) if lat else 0,
                       "p95": round(p95, 1)},
        "status_dist": _count(r.status for r in results),
        "failures": [{"id": r.case.id, "q": r.case.question, "why": r.reason}
                     for r in results if not r.passed],
    }
    return {"summary": summary, "details": [asdict_result(r) for r in results]}


def _tables_of(sql: str) -> list[str]:
    """从 SQL 文本抽表名（比解析 EXPLAIN detail 稳，不受别名/扫描方式影响）。"""
    if not sql:
        return []
    return list(dict.fromkeys(t.lower() for t in re.findall(r"\b(?:from|join)\s+([A-Za-z_]\w*)", sql, re.I)))


def _count(it) -> dict[str, int]:
    out: dict[str, int] = {}
    for v in it:
        out[v] = out.get(v, 0) + 1
    return out


def asdict_result(r: CaseResult) -> dict[str, Any]:
    return {"id": r.case.id, "split": r.case.split, "type": r.case.type,
            "question": r.case.question, "passed": r.passed, "status": r.status,
            "latency_ms": round(r.latency_ms, 1), "reason": r.reason, "sql": r.sql}
