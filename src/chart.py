# -*- coding: utf-8 -*-
"""结果渲染：终端表格 + 前端可直接用的 ECharts option。

图型选择规则（简单但够用）
- 单值 → KPI 卡片
- 维度是日期/月份且行数 > 3 → 折线（趋势）
- 其他分组 → 柱状（≤8 个分组）或横向柱状（>8 个，避免标签挤在一起）
"""
from __future__ import annotations

from typing import Any

from .db import QueryResult

CN_UNIT = {
    "gmv": "元", "aov": "元", "orders": "单", "qty": "件", "customers": "人",
}


def _width(s: str) -> int:
    """中文按 2 个字符宽度算，保证表格对齐。"""
    return sum(2 if "\u4e00" <= ch <= "\u9fff" else 1 for ch in s)


def _pad(s: str, w: int) -> str:
    return s + " " * max(0, w - _width(s))


def render_table(result: QueryResult, limit: int = 15) -> str:
    if not result.columns:
        return "（无结果）"
    rows = [["" if v is None else (f"{v:,.2f}" if isinstance(v, float) else str(v)) for v in r]
            for r in result.rows[:limit]]
    cols = list(result.columns)
    widths = [max(_width(c), *(_width(r[i]) for r in rows)) if rows else _width(c)
              for i, c in enumerate(cols)]
    line = "  ".join(_pad(c, widths[i]) for i, c in enumerate(cols))
    sep = "  ".join("-" * widths[i] for i in range(len(cols)))
    body = "\n".join("  ".join(_pad(r[i], widths[i]) for i in range(len(cols))) for r in rows)
    more = f"\n… 共 {result.rowcount} 行" if result.rowcount > limit else ""
    return "\n".join([line, sep, body]) + more


def build_chart(result: QueryResult) -> dict[str, Any] | None:
    if not result.rows or not result.columns:
        return None
    cols = [c.lower() for c in result.columns]

    # 单值 → KPI
    if len(cols) == 1 and len(result.rows) == 1:
        v = result.rows[0][0]
        unit = next((u for k, u in CN_UNIT.items() if k in cols[0]), "")
        return {"type": "kpi", "title": result.columns[0], "value": v, "unit": unit}

    if len(cols) < 2:
        return None

    dim, val = 0, 1
    labels = [str(r[dim]) for r in result.rows]
    values = [r[val] for r in result.rows]
    unit = next((u for k, u in CN_UNIT.items() if k in cols[val]), "")

    is_time = any(k in cols[dim] for k in ("date", "month", "ym", "day"))
    if is_time and len(labels) > 3:
        ctype, series_type = "line", "line"
    elif len(labels) > 8:
        ctype, series_type = "bar_horizontal", "bar"
    else:
        ctype, series_type = "bar", "bar"

    option = {
        "type": ctype,
        "title": {"text": f"{result.columns[dim]} × {result.columns[val]}"},
        "tooltip": {"trigger": "axis"},
        "grid": {"left": 40, "right": 24, "top": 48, "bottom": 36},
        "xAxis": {"type": "category", "data": labels if ctype != "bar_horizontal" else None,
                  "axisLabel": {"rotate": 0}},
        "yAxis": {"type": "value", "name": unit,
                  "data": None if ctype != "bar_horizontal" else labels},
        "series": [{"type": series_type, "data": values, "name": result.columns[val],
                    "smooth": True}],
    }
    if ctype == "bar_horizontal":
        option["xAxis"] = {"type": "value", "name": unit}
        option["yAxis"] = {"type": "category", "data": labels}
        option["series"][0]["type"] = "bar"
    return option
