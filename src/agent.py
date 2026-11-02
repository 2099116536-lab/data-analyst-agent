# -*- coding: utf-8 -*-
"""数据分析 Agent：问题 → 意图路由 → schema 检索 → 生成 SQL → 校验 → 执行 → 自检 → 修复 → 结论 + 图表。

状态机（节点名即 trace 里的 span 名）
    route → retrieve → generate → validate → execute → inspect ─┬→ summarize → chart
                     ↑                        │                  │
                     └──────── repair ←───────┴──────────────────┘（最多 2 轮）

设计取舍
1. 先"能答/不能答"再"怎么答"：预测、归因、多指标、大区粒度这些问法先判为不支持并给出澄清，
   而不是硬生成一条看起来对、实际口径错的 SQL —— 数据分析场景里"答错"比"不答"代价大得多。
2. 校验前置：EXPLAIN 预检能挡住列名/表名错误，避免执行期才发现问题、白跑一轮修复。
3. 修复有上限：repair 最多 2 轮，仍失败就如实报错并把失败 SQL 一起返回，方便人接手。
4. 结论不交给模型编：结论由结果数据按模板生成（Top1 是谁、占比多少），模型只在"生成 SQL"这一步介入。
"""
from __future__ import annotations

import re
import time
from dataclasses import asdict, dataclass, field
from datetime import date
from typing import Any

from .chart import build_chart, render_table
from .compiler import Plan, build_sql, compile_plan
from .db import Database, QueryResult, SqlError
from .schema import SchemaIndex

MAX_REPAIR = 2

UNSUPPORTED = [
    (r"预测|预估|预计|未来|下个月会|明年会|forecast", "预测类问题：当前 Agent 只做历史数据的确定性查询，不做预测建模"),
    (r"为什么|原因|造成|导致|归因|下降的原因", "归因类问题：结论需要实验/对照数据支撑，当前库只有订单流水，给不出可信归因"),
    (r"华东|华南|华北|华中|东北|西北|西南|省份|大区|区域划分", "地理粒度不足：库里只有城市字段，没有大区/省份映射"),
]


@dataclass
class Span:
    name: str
    ms: float
    detail: str = ""


@dataclass
class AgentState:
    question: str
    status: str = "ok"                 # ok / rejected / clarify / repaired / empty / failed
    sql: str = ""
    plan: dict[str, Any] | None = None
    result: QueryResult | None = None
    chart: dict[str, Any] | None = None
    answer: str = ""
    tables: list[str] = field(default_factory=list)
    attempts: int = 0
    repairs: list[str] = field(default_factory=list)
    trace: list[Span] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["result"] = self.result.to_dict() if self.result else None
        d["trace"] = [asdict(s) for s in self.trace]
        return d


class DataAnalystAgent:
    def __init__(self, db_path: str, schema_path: str | None = None, llm=None) -> None:
        self.db = Database(db_path)
        self.schema = SchemaIndex(schema_path) if schema_path else SchemaIndex()
        self.llm = llm
        self.as_of: date = self._detect_as_of()

    def _detect_as_of(self) -> date:
        """以库里最新一天作为"今天"，避免静态快照数据与真实日期错位。"""
        try:
            r = self.db.run("SELECT MAX(order_date) FROM orders")
            v = r.rows[0][0]
            y, m, d = (int(x) for x in str(v).split("-"))
            return date(y, m, d)
        except Exception:
            return date.today()

    # ---------- 各节点 ----------
    def node_route(self, st: AgentState) -> AgentState:
        t0 = time.perf_counter()
        q = st.question
        if not self.schema.in_scope(q):
            hit = next((w for w in self.schema.out_of_scope if w in q), "")
            st.status = "rejected"
            st.answer = f"知识库里没有「{hit}」相关数据，当前只能回答订单/客户/商品/金额口径的问题。"
            st.trace.append(Span("route", (time.perf_counter() - t0) * 1000, "拒绝：库外主题"))
            return st

        for rx, why in UNSUPPORTED:
            if re.search(rx, q):
                st.status = "clarify"
                st.answer = f"这个问题的类型我暂时答不了 —— {why}。可以换成：按城市/品类/渠道/月份看销售额、订单数、客单价。"
                st.trace.append(Span("route", (time.perf_counter() - t0) * 1000, "澄清：超出能力边界"))
                return st

        # 多指标：两个指标词 + 连接词 → 当前编译器一次只出一种指标
        metrics = re.findall(r"销售额|订单数|客单价|销量|客户数|下单客户数", q)
        if len(set(metrics)) >= 2 and re.search(r"和|与|、|分别", q):
            st.status = "clarify"
            st.answer = f"一次只能算一个指标（识别到 {'、'.join(sorted(set(metrics)))}）。拆成两个问题问，或者告诉我要优先哪个。"
            st.trace.append(Span("route", (time.perf_counter() - t0) * 1000, "澄清：多指标"))
            return st

        years = set(re.findall(r"(\d{4})\s*年", q))
        if len(years) >= 2:
            st.status = "clarify"
            st.answer = "跨年对比需要两步：先分别算各年销售额。当前一次查询只支持同一年内的月份对比。"
            st.trace.append(Span("route", (time.perf_counter() - t0) * 1000, "澄清：跨年对比"))
            return st

        st.trace.append(Span("route", (time.perf_counter() - t0) * 1000, "通过"))
        return st

    def node_retrieve(self, st: AgentState) -> AgentState:
        t0 = time.perf_counter()
        subset, names = self.schema.subset_prompt(st.question)
        st.tables = names
        full_tk = len(self.schema.full_prompt())
        sub_tk = len(subset)
        st.trace.append(Span("retrieve", (time.perf_counter() - t0) * 1000,
                             f"表命中 {names}；schema 提示 {full_tk}→{sub_tk} 字符"))
        return st

    def node_generate(self, st: AgentState) -> AgentState:
        t0 = time.perf_counter()
        if self.llm is not None:
            try:
                sql = self.llm.generate_sql(st.question, self.schema.subset_prompt(st.question)[0],
                                            as_of=self.as_of)
                st.sql = sql.strip().rstrip(";")
                st.plan = {"source": "llm"}
                st.trace.append(Span("generate", (time.perf_counter() - t0) * 1000, "LLM 生成"))
                return st
            except Exception as exc:  # 模型失败要能降级，不能把整条链路拖死
                st.repairs.append(f"LLM 生成失败降级到规则编译器：{exc}")
        plan: Plan = compile_plan(st.question, self.as_of, self.schema.out_of_scope)
        if plan.reject:
            st.status = "rejected"
            st.answer = plan.reason
            st.trace.append(Span("generate", (time.perf_counter() - t0) * 1000, "规则：拒答"))
            return st
        st.plan = {"metrics": plan.metrics, "dims": plan.dims, "filters": plan.filters,
                   "time": plan.time_cond, "limit": plan.limit, "source": "rule"}
        st.sql = build_sql(plan)
        st.trace.append(Span("generate", (time.perf_counter() - t0) * 1000, "规则编译器"))
        return st

    def node_validate(self, st: AgentState) -> AgentState:
        t0 = time.perf_counter()
        try:
            self.db.dry_run(st.sql)
            st.trace.append(Span("validate", (time.perf_counter() - t0) * 1000, "EXPLAIN 通过"))
        except SqlError as exc:
            st.repairs.append(f"校验失败：{exc}")
            st = self.node_repair(st, str(exc))
        return st

    def node_execute(self, st: AgentState) -> AgentState:
        if st.status in ("clarify", "rejected", "failed"):
            return st
        t0 = time.perf_counter()
        try:
            st.result = self.db.run(st.sql)
            st.trace.append(Span("execute", (time.perf_counter() - t0) * 1000,
                                 f"{st.result.rowcount} 行 / {st.result.elapsed_ms:.1f} ms"))
        except SqlError as exc:
            st.repairs.append(f"执行失败：{exc}")
            st = self.node_repair(st, str(exc))
        return st

    def node_inspect(self, st: AgentState) -> AgentState:
        """结果自检：空结果不做重试，直接如实说没数据（避免"为了有结果"放宽条件编答案）。"""
        if st.result is None:
            return st
        if st.result.rowcount == 0:
            st.status = "empty"
            st.answer = "查询能执行，但该条件下没有数据。可以放宽时间范围或去掉某个筛选再试。"
            st.trace.append(Span("inspect", 0.0, "空结果"))
        return st

    def node_repair(self, st: AgentState, err: str) -> AgentState:
        """最多 2 轮修复：先去掉报错的列/表条件，再不行就失败退出。"""
        t0 = time.perf_counter()
        for _ in range(MAX_REPAIR):
            if st.attempts >= MAX_REPAIR:
                break
            st.attempts += 1
            fixed = self._patch(st.sql, err)
            if not fixed or fixed == st.sql:
                break
            try:
                self.db.dry_run(fixed)
                st.sql = fixed
                st.status = "repaired"
                st.repairs.append(f"第 {st.attempts} 轮修复成功")
                st.trace.append(Span("repair", (time.perf_counter() - t0) * 1000,
                                     f"第 {st.attempts} 轮：去掉报错条件"))
                st.result = self.db.run(st.sql)
                return st
            except SqlError:
                continue
        st.status = "failed"
        st.answer = f"SQL 没能自动修好，已记录失败语句便于排查。错误：{err[:160]}"
        st.trace.append(Span("repair", (time.perf_counter() - t0) * 1000, "修复失败，退出"))
        return st

    @staticmethod
    def _patch(sql: str, err: str) -> str | None:
        """按 SQLite 报错信息做最小修补：no such column / no such table。"""
        m = re.search(r"no such column:\s*([\w.]+)", err)
        if m:
            col = re.escape(m.group(1).split(".")[-1])
            out = re.sub(rf"\s+AND\s+[\w.]*{col}\s*=\s*'[^']*'", "", sql)
            out = re.sub(rf"\s+AND\s+[\w.]*{col}\s+IN\s*\([^)]*\)", "", out)
            return out if out != sql else None
        m = re.search(r"no such table:\s*(\w+)", err)
        if m:
            t = m.group(1)
            out = re.sub(rf"\nJOIN\s+{t}\s+\w+\s+ON[^\n]*", "", sql)
            return out if out != sql else None
        return None

    def node_summarize(self, st: AgentState) -> AgentState:
        if st.status in ("clarify", "rejected", "failed", "empty"):
            return st
        t0 = time.perf_counter()
        st.answer = self._narrate(st)
        st.trace.append(Span("summarize", (time.perf_counter() - t0) * 1000, "模板化结论"))
        return st

    def node_chart(self, st: AgentState) -> AgentState:
        if st.result is None or st.status in ("clarify", "rejected", "failed"):
            return st
        t0 = time.perf_counter()
        st.chart = build_chart(st.result)
        st.trace.append(Span("chart", (time.perf_counter() - t0) * 1000,
                             st.chart.get("type", "none") if st.chart else "none"))
        return st

    # ---------- 结论生成 ----------
    @staticmethod
    def _narrate(st: AgentState) -> str:
        r = st.result
        if r is None:
            return ""
        cols, rows = r.columns, r.rows
        if len(cols) == 1 and len(rows) == 1:
            v = rows[0][0]
            name = {"gmv": "销售额", "orders": "订单数", "aov": "客单价", "qty": "销量", "customers": "下单客户数"}
            label = name.get(str(cols[0]).lower(), cols[0])
            if isinstance(v, (int, float)) and "gmv" in str(cols[0]).lower():
                return f"{label}：¥{v:,.2f}"
            if isinstance(v, (int, float)):
                return f"{label}：{v:,}"
            return f"{label}：{v}"

        if len(cols) >= 2 and rows:
            dim, val = 0, 1
            try:
                ordered = sorted(rows, key=lambda x: (x[val] is None, x[val]), reverse=True)
            except TypeError:
                ordered = rows
            top = ordered[0]
            total = sum(x[val] for x in rows if isinstance(x[val], (int, float)))
            share = ""
            if total:
                share = f"，占总量的 {top[val] / total * 100:.1f}%"
            unit = "元" if "gmv" in str(cols[val]).lower() or "aov" in str(cols[val]).lower() else ""
            head = f"{top[dim]} 最高：{top[val]:,.2f}{unit}{share}" if unit else f"{top[dim]} 最高：{top[val]:,}{share}"
            tail = ""
            if len(ordered) >= 2:
                tail = f"；第二是 {ordered[1][dim]}（{ordered[1][val]:,.2f}{unit}）" if unit else f"；第二是 {ordered[1][dim]}（{ordered[1][val]:,}）"
            return f"{len(rows)} 个分组，{head}{tail}。"
        return "查询已完成。"

    # ---------- 主入口 ----------
    def run(self, question: str) -> AgentState:
        st = AgentState(question=question)
        for node in (self.node_route, self.node_retrieve, self.node_generate,
                     self.node_validate, self.node_execute, self.node_inspect,
                     self.node_summarize, self.node_chart):
            st = node(st)
            if st.status in ("rejected", "clarify", "failed"):
                break
        return st

    def report(self, st: AgentState) -> str:
        """终端友好的完整输出。"""
        out = [f"问题：{st.question}", f"状态：{st.status}"]
        if st.sql:
            out.append("SQL：\n" + st.sql)
        if st.result:
            out.append(render_table(st.result))
        if st.answer:
            out.append("结论：" + st.answer)
        if st.repairs:
            out.append("修复记录：" + "；".join(st.repairs))
        out.append("耗时：" + " → ".join(f"{s.name} {s.ms:.1f}ms" for s in st.trace))
        return "\n".join(out)
