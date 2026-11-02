# -*- coding: utf-8 -*-
"""规则式 NL→SQL 编译器（离线基线）。

为什么先写规则而不是直接上大模型
- Agent 的失败有两种：模型不会写 SQL、SQL 写对了但口径错。规则编译器把"口径"固化成代码，
  可以先把后一种错误按死，再让模型专注前一种；
- 没有 API key 时链路必须还能跑通（评测、CI、演示都需要），规则基线就是这个兜底；
- 规则命中的部分可以量化（命中率），模型只在未命中时接手，成本也可控。

输出的 Plan 是结构化的，SQL 由 build_sql 统一拼装，避免各处手写 SQL 拼字符串。
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date, timedelta

VALID_STATUS = ("'已支付'", "'已完成'")
CITIES = ["成都", "重庆", "西安", "昆明", "贵阳", "北京", "上海", "广州", "杭州", "武汉"]
LEVELS = ["普通", "银卡", "金卡", "黑卡"]
CATEGORIES = ["家电", "数码", "食品", "服饰", "美妆", "母婴"]
CHANNELS = ["APP", "小程序", "门店", "直播"]

CN_NUM = {"一": 1, "二": 2, "三": 3, "四": 4, "五": 5, "六": 6, "七": 7, "八": 8, "九": 9, "十": 10}


def cn2int(s: str) -> int | None:
    if s.isdigit():
        return int(s)
    if s in CN_NUM:
        return CN_NUM[s]
    if len(s) == 2 and s[0] == "十":
        return 10 + CN_NUM.get(s[1], 0)
    if len(s) == 2 and s[1] == "十":
        return CN_NUM.get(s[0], 0) * 10
    return None


@dataclass
class Plan:
    metrics: list[str] = field(default_factory=list)      # gmv / orders / aov / qty / customers
    dims: list[str] = field(default_factory=list)         # city / level / category / product / channel / month / quarter
    filters: list[str] = field(default_factory=list)      # 已渲染的 SQL 片段
    time_cond: str | None = None
    order_by: str | None = None
    limit: int | None = None
    relaxed_status: bool = False                          # 用户明确问退款/待支付/全部
    reject: bool = False
    reason: str = ""

    def tables(self) -> list[str]:
        need_items = bool({"gmv", "aov", "qty"} & set(self.metrics)) or bool(
            {"category", "product"} & set(self.dims))
        need_products = bool({"category", "product"} & set(self.dims))
        need_customers = bool({"city", "level"} & set(self.dims)) or any(
            f.startswith(("c.", "c2.")) for f in self.filters)
        t = ["orders"]
        if need_items:
            t.append("order_items")
        if need_products:
            t.append("products")
        if need_customers:
            t.append("customers")
        return t


METRIC_EXPR = {
    "gmv": "ROUND(SUM(oi.amount),2) AS gmv",
    "orders": "COUNT(DISTINCT o.id) AS orders",
    "aov": "ROUND(SUM(oi.amount)/COUNT(DISTINCT o.id),2) AS aov",
    "qty": "SUM(oi.qty) AS qty",
    "customers": "COUNT(DISTINCT o.customer_id) AS customers",
}
DIM_EXPR = {
    "city": "c.city",
    "level": "c.level",
    "category": "p.category",
    "product": "p.name",
    "channel": "o.channel",
    "month": "strftime('%Y-%m', o.order_date)",
    "quarter": "strftime('%Y', o.order_date) || '-Q' || ((CAST(strftime('%m', o.order_date) AS INTEGER)+2)/3)",
    "date": "o.order_date",
}


def _parse_period(q: str, as_of: date) -> tuple[str | None, bool]:
    """返回（时间条件 SQL, 是否属于"跨多个指定月份"）。"""
    # 年份 + 所有出现过的"N月"（"2026年9月和10月对比" 这种同年省略写法也要吃住）
    year_m = re.search(r"(\d{4})\s*年", q)
    months_all = [int(m) for m in re.findall(r"(\d{1,2})\s*月", q) if 1 <= int(m) <= 12]
    months_all = list(dict.fromkeys(months_all))
    if months_all:
        y = year_m.group(1) if year_m else str(as_of.year)
        if len(months_all) >= 2:
            vals = ",".join(f"'{y}-{m:02d}'" for m in months_all)
            return f"strftime('%Y-%m', o.order_date) IN ({vals})", True
        return f"strftime('%Y-%m', o.order_date)='{y}-{months_all[0]:02d}'", False

    # 近 N 天 / 最近 N 天
    m = re.search(r"(?:最近|近|过去)\s*(\d+|[一二三四五六七八九十]+)\s*(天|日|周|个月|月)", q)
    if m:
        n = cn2int(m.group(1)) or 30
        unit = m.group(2)
        if unit in ("天", "日"):
            start = as_of - timedelta(days=max(0, n - 1))  # 含基准日：11-01 往前 30 天 = 10-03
        elif unit == "周":
            start = as_of - timedelta(days=7 * n)
        else:
            start = as_of - timedelta(days=30 * n)
        return f"o.order_date >= '{start.isoformat()}'", False

    if "上个月" in q or "上月" in q:
        first = as_of.replace(day=1)
        last_month_end = first - timedelta(days=1)
        return f"strftime('%Y-%m', o.order_date)='{last_month_end.strftime('%Y-%m')}'", False
    if "本月" in q or "这个月" in q:
        return f"strftime('%Y-%m', o.order_date)='{as_of.strftime('%Y-%m')}'", False
    if "去年" in q:
        return f"strftime('%Y', o.order_date)='{as_of.year - 1}'", False
    if "今年" in q:
        return f"strftime('%Y', o.order_date)='{as_of.year}'", False

    if year_m:
        return f"strftime('%Y', o.order_date)='{year_m.group(1)}'", False

    # 季度
    qm = re.search(r"第?([一二三四1-4])\s*季度", q)
    if qm:
        qn = cn2int(qm.group(1)) or 1
        months_in_q = {1: ("01", "02", "03"), 2: ("04", "05", "06"),
                       3: ("07", "08", "09"), 4: ("10", "11", "12")}[qn]
        y = as_of.year
        vals = ",".join(f"'{y}-{m}'" for m in months_in_q)
        return f"strftime('%Y-%m', o.order_date) IN ({vals})", True
    return None, False


def compile_plan(question: str, as_of: date, out_of_scope: list[str] | None = None) -> Plan:
    q = question.strip().replace("　", " ")
    plan = Plan()

    # ---------- 1. 拒答：库外主题 ----------
    for w in (out_of_scope or []):
        if w in q:
            plan.reject = True
            plan.reason = f"问题涉及「{w}」，当前库没有对应数据"
            return plan

    # ---------- 2. 状态口径 ----------
    if any(w in q for w in ("退款", "已退款")):
        plan.relaxed_status = True
        plan.filters.append("o.status='已退款'")
        if "待支付" in q:
            plan.filters = ["o.status IN ('已退款','待支付')"]
    elif any(w in q for w in ("待支付", "未支付")):
        plan.relaxed_status = True
        plan.filters.append("o.status='待支付'")
    elif any(w in q for w in ("全部订单", "所有订单", "含退款")):
        plan.relaxed_status = False  # 不加状态过滤

    # ---------- 3. 时间 ----------
    plan.time_cond, multi_month = _parse_period(q, as_of)

    # ---------- 4. 维度 ----------
    if re.search(r"每天|每日|按天|按日|逐日|日趋势", q):
        plan.dims.append("date")
    elif re.search(r"各月|按月|每月|月度|趋势|逐月", q):
        plan.dims.append("month")
    if re.search(r"各城市|按城市|分城市|城市维度|哪些城市|哪个城市|\d+个城市", q):
        plan.dims.append("city")
    if re.search(r"各品类|按品类|分品类|各商品品类|每个品类|哪些品类|哪个品类|\d+个品类", q):
        plan.dims.append("category")
    if re.search(r"各渠道|按渠道|分渠道|哪个渠道|哪些渠道", q):
        plan.dims.append("channel")
    if re.search(r"各会员等级|按等级|各等级|会员等级|哪个等级", q):
        plan.dims.append("level")
    if re.search(r"各季度|按季度", q):
        plan.dims.append("quarter")
    if re.search(r"各商品|按商品|哪些商品|哪个商品|商品销量|销量最高|销售额最高.*商品", q):
        plan.dims.append("product")
    # "对比"类：即使没说"各月"，也要按月份出多行
    if multi_month and "month" not in plan.dims:
        plan.dims.append("month")

    # ---------- 5. 指标（互斥，按语义优先级） ----------
    # 教训：早先写成多个独立 if，结果"卖了多少钱"同时命中 gmv 和 qty，SELECT 出两列 → 全错。
    # 指标必须互斥，除非问句里明确出现"和/、"连接两个指标（当前评测集无此情况）。
    if re.search(r"客单价|平均每单|平均订单金额|笔单价|平均每笔", q):
        plan.metrics.append("aov")
    elif re.search(r"订单数|多少订单|多少单|多少笔|单量|订单量|笔数|几单", q):
        plan.metrics.append("orders")
    elif re.search(r"销量|多少件|卖了多少件|件数|多少个商品", q):
        plan.metrics.append("qty")
    elif re.search(r"多少个客户|多少客户|客户数|下单客户|用户数|多少人", q):
        plan.metrics.append("customers")
    elif re.search(r"销售额|营收|GMV|gmv|卖了多少钱|成交额|金额|业绩", q):
        plan.metrics.append("gmv")
    if not plan.metrics:
        plan.metrics.append("gmv")  # 兜底：默认看销售额

    # ---------- 6. 过滤值 ----------
    for c in CITIES:
        if c in q and not re.search(r"各城市|按城市", q):
            plan.filters.append(f"c.city='{c}'")
            break
    for lv in LEVELS:
        if f"{lv}卡" in q or f"{lv}会员" in q or f"{lv}用户" in q or f"{lv}等级" in q:
            plan.filters.append(f"c.level='{lv}'")
            break
    for cat in CATEGORIES:
        if cat in q and not re.search(r"各品类|按品类", q):
            plan.filters.append(f"p.category='{cat}'")
            break
    for ch in CHANNELS:
        if ch in q and not re.search(r"各渠道|按渠道", q):
            plan.filters.append(f"o.channel='{ch}'")
            break

    # ---------- 7. 排序与 TopN ----------
    if re.search(r"从高到低|降序|排名|排序", q):
        plan.order_by = "DESC"
    m = re.search(r"(?:前|top|TOP|最高的|最低的|最多的|最少的)\s*(\d+|[一二三四五六七八九十]+)\s*(?:个|名|位|大)?", q)
    if m:
        n = cn2int(m.group(1))
        if n:
            plan.limit = n
            plan.order_by = "ASC" if re.search(r"最低|最少|最差|最小", q) else "DESC"
    elif re.search(r"最高|最多|最好|最大|卖得最好", q) and plan.dims:
        plan.limit = 1
        plan.order_by = "DESC"
    elif re.search(r"最低|最少|最差|最小|卖得最差", q) and plan.dims:
        plan.limit = 1
        plan.order_by = "ASC"

    if plan.dims and plan.order_by is None and plan.limit is None:
        plan.order_by = "DESC"

    return plan


def build_sql(plan: Plan) -> str:
    if plan.reject:
        raise ValueError("reject plan has no sql")

    dims = list(dict.fromkeys(plan.dims))
    metrics = list(dict.fromkeys(plan.metrics))
    tbls = plan.tables()

    select = [DIM_EXPR[d] for d in dims]
    if dims:
        select += [METRIC_EXPR[m] for m in metrics]
    else:
        select = [METRIC_EXPR[m] for m in metrics]

    joins = []
    if "order_items" in tbls:
        joins.append("JOIN order_items oi ON oi.order_id = o.id")
    if "customers" in tbls:
        joins.append("JOIN customers c ON c.id = o.customer_id")
    if "products" in tbls:
        joins.append("JOIN products p ON p.id = oi.product_id")

    where = []
    if not plan.relaxed_status:
        where.append("o.status IN ('已支付','已完成')")
    where += plan.filters
    if plan.time_cond:
        where.append(plan.time_cond)

    sql = "SELECT " + ", ".join(select) + "\nFROM orders o"
    if joins:
        sql += "\n" + "\n".join(joins)
    if where:
        sql += "\nWHERE " + " AND ".join(where)
    if dims:
        sql += "\nGROUP BY " + ", ".join(str(i + 1) for i in range(len(dims)))
    if plan.order_by:
        # 有维度时按指标列排（dims 之后第一列），否则单值无需排序
        sort_col = str(len(dims) + 1) if dims else "1"
        sql += f"\nORDER BY {sort_col} {plan.order_by}"
    if plan.limit:
        sql += f"\nLIMIT {plan.limit}"
    return sql
