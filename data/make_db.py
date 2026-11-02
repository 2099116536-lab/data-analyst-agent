# -*- coding: utf-8 -*-
"""生成用于 NL2SQL Agent 的零售业务示例库（SQLite，固定随机种子，可复现）。

    python data/make_db.py            # 生成 data/shop.db
    python data/make_db.py --rows 8000

表：
    customers    客户（城市 / 会员等级 / 注册时间）
    products     商品（品类 / 单价）
    orders       订单（客户 / 下单日期 / 状态 / 渠道）
    order_items  订单明细（商品 / 数量 / 成交单价 / 金额）
"""
from __future__ import annotations

import argparse
import random
import sqlite3
from datetime import date, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent
DB = ROOT / "shop.db"

CITIES = ["成都", "重庆", "西安", "昆明", "贵阳", "北京", "上海", "广州", "杭州", "武汉"]
LEVELS = ["普通", "银卡", "金卡", "黑卡"]
CHANNELS = ["APP", "小程序", "门店", "直播"]
STATUS = ["已支付", "已完成", "已退款", "待支付"]
CATEGORIES = {
    "家电": [("空气炸锅", 399), ("扫地机器人", 2199), ("电饭煲", 459), ("净水器", 1899)],
    "数码": [("蓝牙耳机", 599), ("智能手表", 1299), ("移动电源", 129), ("键盘", 349)],
    "食品": [("坚果礼盒", 129), ("咖啡豆", 89), ("牛奶", 59), ("自热火锅", 39)],
    "服饰": [("冲锋衣", 899), ("运动鞋", 499), ("T恤", 129), ("羽绒服", 1099)],
    "美妆": [("面霜", 399), ("口红", 219), ("精华", 599), ("面膜", 149)],
    "母婴": [("纸尿裤", 159), ("婴儿奶粉", 329), ("辅食机", 699), ("婴儿车", 1599)],
}
SURNAMES = "赵钱孙李周吴郑王冯陈褚卫蒋沈韩杨朱秦尤许何吕施张孔曹严华金魏陶姜"
GIVEN = ["伟", "芳", "娜", "敏", "静", "强", "磊", "洋", "艳", "勇", "军", "杰", "娟", "涛", "明", "超", "秀英", "霞", "平", "刚"]


def daterange(days: int) -> list[str]:
    today = date(2026, 11, 1)
    return [(today - timedelta(days=i)).isoformat() for i in range(days)]


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--rows", type=int, default=4000, help="订单条数")
    ap.add_argument("--seed", type=int, default=20261101)
    args = ap.parse_args()

    rnd = random.Random(args.seed)
    if DB.exists():
        DB.unlink()
    conn = sqlite3.connect(DB)
    cur = conn.cursor()

    cur.executescript(
        """
        CREATE TABLE customers (
            id INTEGER PRIMARY KEY,
            name TEXT NOT NULL,
            city TEXT NOT NULL,
            level TEXT NOT NULL,
            created_at TEXT NOT NULL
        );
        CREATE TABLE products (
            id INTEGER PRIMARY KEY,
            name TEXT NOT NULL,
            category TEXT NOT NULL,
            price REAL NOT NULL
        );
        CREATE TABLE orders (
            id INTEGER PRIMARY KEY,
            customer_id INTEGER NOT NULL,
            order_date TEXT NOT NULL,
            status TEXT NOT NULL,
            channel TEXT NOT NULL
        );
        CREATE TABLE order_items (
            id INTEGER PRIMARY KEY,
            order_id INTEGER NOT NULL,
            product_id INTEGER NOT NULL,
            qty INTEGER NOT NULL,
            unit_price REAL NOT NULL,
            amount REAL NOT NULL
        );
        CREATE INDEX idx_orders_date ON orders(order_date);
        CREATE INDEX idx_items_order ON order_items(order_id);
        """
    )

    # 客户 600 人
    cust = []
    for i in range(1, 601):
        cust.append((
            i,
            rnd.choice(SURNAMES) + rnd.choice(GIVEN),
            rnd.choice(CITIES),
            rnd.choices(LEVELS, weights=[60, 25, 11, 4])[0],
            (date(2023, 1, 1) + timedelta(days=rnd.randint(0, 1000))).isoformat(),
        ))
    cur.executemany("INSERT INTO customers VALUES (?,?,?,?,?)", cust)

    # 商品
    prods, pid = [], 1
    for cat, items in CATEGORIES.items():
        for name, price in items:
            prods.append((pid, name, cat, float(price)))
            pid += 1
    cur.executemany("INSERT INTO products VALUES (?,?,?,?)", prods)

    # 订单：覆盖 2025-01-01 ~ 2026-11-01，近 90 天单量更大（便于做同比/环比题）
    orders, oitems, iid = [], [], 1
    start, end = date(2025, 1, 1), date(2026, 11, 1)
    span = (end - start).days
    for oid in range(1, args.rows + 1):
        # 30% 落在最近 60 天
        if rnd.random() < 0.30:
            d = end - timedelta(days=rnd.randint(0, 59))
        else:
            d = start + timedelta(days=rnd.randint(0, span))
        cid = rnd.randint(1, 600)
        st = rnd.choices(STATUS, weights=[58, 30, 6, 6])[0]
        ch = rnd.choices(CHANNELS, weights=[45, 30, 15, 10])[0]
        orders.append((oid, cid, d.isoformat(), st, ch))

        for _ in range(rnd.randint(1, 3)):
            p = rnd.choice(prods)
            qty = rnd.choices([1, 2, 3, 5], weights=[70, 18, 8, 4])[0]
            # 成交价：9 折附近浮动
            up = round(p[3] * rnd.uniform(0.85, 1.0), 2)
            # 退款订单金额照记（后续口径按 status 过滤）
            oitems.append((iid, oid, p[0], qty, up, round(up * qty, 2)))
            iid += 1

    cur.executemany("INSERT INTO orders VALUES (?,?,?,?,?)", orders)
    cur.executemany("INSERT INTO order_items VALUES (?,?,?,?,?,?)", oitems)
    conn.commit()

    for t in ("customers", "products", "orders", "order_items"):
        n = cur.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
        print(f"{t:12s} {n:>6d} 行")
    print("日期范围：", cur.execute("SELECT MIN(order_date), MAX(order_date) FROM orders").fetchone())
    print("写入", DB)
    conn.close()


if __name__ == "__main__":
    main()
