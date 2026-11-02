# -*- coding: utf-8 -*-
"""单元测试（标准库 unittest，零第三方依赖）：python -m unittest discover -s tests -v"""
from __future__ import annotations

import sys
import unittest
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from src.agent import DataAnalystAgent  # noqa: E402
from src.chart import build_chart  # noqa: E402
from src.compiler import build_sql, compile_plan  # noqa: E402
from src.db import Database, SqlError  # noqa: E402
from src.schema import SchemaIndex, est_tokens  # noqa: E402

DB = ROOT / "data" / "shop.db"
AS_OF = date(2026, 11, 1)


class TestGuard(unittest.TestCase):
    """只读护栏：Agent 生成的 SQL 不能把库写坏。"""

    def setUp(self) -> None:
        self.db = Database(DB)

    def test_reject_write(self) -> None:
        for sql in ["DELETE FROM orders", "UPDATE orders SET status='x'",
                    "DROP TABLE orders", "INSERT INTO orders VALUES (1)"]:
            with self.assertRaises(SqlError):
                self.db.check(sql)

    def test_reject_multi_statement(self) -> None:
        with self.assertRaises(SqlError):
            self.db.check("SELECT 1; SELECT 2")

    def test_allow_select(self) -> None:
        self.assertEqual(self.db.check("select 1"), "select 1")

    def test_dry_run_catches_bad_column(self) -> None:
        with self.assertRaises(SqlError):
            self.db.dry_run("SELECT no_such_col FROM orders")


class TestCompiler(unittest.TestCase):
    def setUp(self) -> None:
        self.schema = SchemaIndex()

    def plan(self, q: str):
        return compile_plan(q, AS_OF, self.schema.out_of_scope)

    def test_group_by_city(self) -> None:
        sql = build_sql(self.plan("各城市的销售额是多少？"))
        self.assertIn("JOIN customers", sql)
        self.assertIn("GROUP BY", sql)
        self.assertIn("已支付", sql)

    def test_order_count_no_items_join(self) -> None:
        """订单数不该 JOIN 明细表，否则多算行数。"""
        sql = build_sql(self.plan("一共多少笔有效订单？"))
        self.assertNotIn("order_items", sql)
        self.assertIn("COUNT(DISTINCT o.id)", sql)

    def test_metrics_mutually_exclusive(self) -> None:
        """回归：早先多个独立 if 会让"卖了多少钱"同时命中 gmv 和 qty。"""
        plan = self.plan("上个月一共卖了多少钱？")
        self.assertEqual(plan.metrics, ["gmv"])

    def test_reject_out_of_scope(self) -> None:
        self.assertTrue(self.plan("现在库存还剩多少？").reject)

    def test_relax_status(self) -> None:
        plan = self.plan("已退款订单有多少笔？")
        self.assertTrue(plan.relaxed_status)
        self.assertIn("o.status='已退款'", plan.filters)

    def test_recent_days_inclusive(self) -> None:
        plan = self.plan("最近30天的销售额")
        self.assertEqual(plan.time_cond, "o.order_date >= '2026-10-03'")

    def test_two_months_in(self) -> None:
        plan = self.plan("2026年9月和10月的销售额对比")
        self.assertIn("IN ('2026-09','2026-10')", plan.time_cond)


class TestSchemaSearch(unittest.TestCase):
    def setUp(self) -> None:
        self.schema = SchemaIndex()

    def test_retrieval_hits_right_table(self) -> None:
        _, names = self.schema.subset_prompt("各品类的销售额是多少？")
        self.assertIn("products", names)
        self.assertIn("order_items", names)

    def test_retrieval_saves_tokens(self) -> None:
        full = est_tokens(self.schema.full_prompt())
        sub = est_tokens(self.schema.subset_prompt("各城市的订单数")[0])
        self.assertLess(sub, full)


class TestAgent(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.agent = DataAnalystAgent(str(DB), llm=None)

    def test_happy_path(self) -> None:
        st = self.agent.run("各城市的销售额是多少？")
        self.assertEqual(st.status, "ok")
        self.assertIsNotNone(st.result)
        self.assertIn("SQL", self.agent.report(st).upper())

    def test_boundary_forecast(self) -> None:
        st = self.agent.run("帮我预测下个月的销售额")
        self.assertEqual(st.status, "clarify")

    def test_boundary_multi_metric(self) -> None:
        st = self.agent.run("各城市的订单数和客单价分别是多少？")
        self.assertEqual(st.status, "clarify")

    def test_reject_stock(self) -> None:
        st = self.agent.run("现在库存还剩多少？")
        self.assertEqual(st.status, "rejected")

    def test_repair_no_such_column(self) -> None:
        """自修复：把不存在的列条件去掉后应能跑通。"""
        st = self.agent.run("全站累计销售额是多少？")
        bad = st.sql + " AND o.no_such = 'x'"
        fixed = self.agent._patch(bad, "no such column: o.no_such")
        self.assertIsNotNone(fixed)
        self.assertNotIn("no_such", fixed)
        self.agent.db.dry_run(fixed)

    def test_as_of_from_data(self) -> None:
        self.assertEqual(self.agent.as_of, AS_OF)


class TestChart(unittest.TestCase):
    def setUp(self) -> None:
        self.db = Database(DB)

    def test_kpi_for_single_value(self) -> None:
        r = self.db.run("SELECT ROUND(SUM(amount),2) AS gmv FROM order_items")
        self.assertEqual(build_chart(r)["type"], "kpi")

    def test_line_for_monthly(self) -> None:
        r = self.db.run("""SELECT strftime('%Y-%m', order_date) AS ym, COUNT(*) AS c
                           FROM orders GROUP BY ym""")
        self.assertEqual(build_chart(r)["type"], "line")

    def test_bar_for_city(self) -> None:
        r = self.db.run("SELECT city, COUNT(*) AS c FROM customers GROUP BY city")
        self.assertIn(build_chart(r)["type"], ("bar", "bar_horizontal"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
