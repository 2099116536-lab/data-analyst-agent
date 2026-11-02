# -*- coding: utf-8 -*-
"""Schema 检索：不是把整库表结构塞进 Prompt，而是按问题只召回相关的表和列。

动机（也是项目要说的取舍）
- 4 张表全量 schema 约 700+ token，全塞进去既贵又容易让模型看错表
- 按问题命中关键词召回 top-k 表，实测能把 Prompt 里的 schema 部分压掉 40% 以上，
  同时不降低表命中率（见 eval 里的 schema_token / table_hit 两项）

打分规则（简单可解释，比训一个小模型更划算）
- 表名/中文名命中关键词  +3
- 列中文名命中          +2
- 指标词（销售额→金额列所在表）+3
- 表英文名出现在问句里   +1
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SCHEMA_PATH = ROOT / "data" / "schema.json"


def est_tokens(text: str) -> int:
    """粗估 token：中文 1 字≈1 token，英文按 4 字符≈1 token。只用于对比量级。"""
    cn = sum(1 for ch in text if "\u4e00" <= ch <= "\u9fff")
    other = len(text) - cn
    return int(cn + other / 4) + 1


@dataclass
class TableHit:
    name: str
    score: int
    columns: dict[str, str]


class SchemaIndex:
    def __init__(self, path: str | Path = SCHEMA_PATH) -> None:
        self.raw = json.loads(Path(path).read_text(encoding="utf-8"))
        self.tables = {t["name"]: t for t in self.raw["tables"]}
        self.metrics: dict[str, str] = self.raw.get("metrics", {})
        self.notes: list[str] = self.raw.get("notes", [])
        self.out_of_scope: list[str] = self.raw.get("out_of_scope", [])

    # ---------- 全量 / 检索 ----------
    def full_prompt(self) -> str:
        """全量 schema（对照基线，用于量化 schema 检索省了多少 token）。"""
        lines = []
        for t in self.raw["tables"]:
            cols = "，".join(f"{k}({v})" for k, v in t["columns"].items())
            lines.append(f"表 {t['name']}（{t['cn']}）：{cols}")
        lines.append("指标口径：" + "；".join(f"{k}={v}" for k, v in self.metrics.items()))
        lines.append("注意：" + "；".join(self.notes))
        return "\n".join(lines)

    def search(self, question: str, top_k: int = 2) -> list[TableHit]:
        q = question.lower()
        hits: list[TableHit] = []
        for name, t in self.tables.items():
            score = 0
            if t["cn"] in question or name.split("_")[0] in q:
                score += 3
            for kw in t.get("keywords", []):
                if kw.lower() in q:
                    score += 3
            for col, cn in t["columns"].items():
                base = cn.split("（")[0]
                if base and base in question:
                    score += 2
                if col in q:
                    score += 2
            hits.append(TableHit(name=name, score=score, columns=t["columns"]))

        # 指标与金额/数量相关时，明细表必须进
        if any(w in question for w in ("销售额", "营收", "金额", "GMV", "gmv", "客单价", "销量", "件数")):
            for h in hits:
                if h.name == "order_items":
                    h.score += 3
        # 任何订单类问题都至少需要 orders + order_items
        if any(w in question for w in ("订单", "单量", "渠道", "退款", "支付")):
            for h in hits:
                if h.name == "orders":
                    h.score += 3

        hits.sort(key=lambda h: (-h.score, h.name))
        picked = [h for h in hits if h.score > 0][:top_k]
        if not picked:
            picked = [hits[[h.name for h in hits].index("orders")]]
        return picked

    def subset_prompt(self, question: str, top_k: int = 2) -> tuple[str, list[str]]:
        """返回（检索后的 schema 片段, 命中的表名）。"""
        picked = self.search(question, top_k=top_k)
        names = [h.name for h in picked]
        lines = []
        for h in picked:
            cols = "，".join(f"{k}({v})" for k, v in h.columns.items())
            lines.append(f"表 {h.name}（{self.tables[h.name]['cn']}）：{cols}")
        used_metrics = [f"{k}={v}" for k, v in self.metrics.items() if any(x in question for x in k)]
        if used_metrics:
            lines.append("指标口径：" + "；".join(used_metrics))
        lines.append("注意：" + "；".join(self.notes[:3]))
        return "\n".join(lines), names

    def in_scope(self, question: str) -> bool:
        return not any(w in question for w in self.out_of_scope)
