# -*- coding: utf-8 -*-
"""LLM SQL 生成（可选路径，零第三方依赖，用标准库 urllib 调 OpenAI 兼容接口）。

约定
- 只让模型做"把问题翻成 SQL"这一件事；口径写在 system prompt 里，不让模型自由发挥
- 输出必须能通过 db.check（只读单句），否则当成失败，由 Agent 降级到规则编译器
- 没配 key 时 get_llm() 返回 None，整条链路继续用规则基线跑（评测、CI 都靠这个）
"""
from __future__ import annotations

import json
import os
import re
import urllib.request

DEFAULT_PROMPT = """你是资深数据分析师，把用户问题翻译成一条 SQLite 查询。

规则：
1. 只输出 SQL，不要解释，不要 Markdown 代码块。
2. 只能查询，不能写入；不允许 UPDATE/DELETE/INSERT/DROP。
3. 金额口径：只统计 orders.status IN ('已支付','已完成')；用户明确问退款/待支付时除外。
4. 销售额 = SUM(order_items.amount)；订单数 = COUNT(DISTINCT orders.id)；
   客单价 = 销售额 / 订单数；销量 = SUM(order_items.qty)。
5. 时间字段是 TEXT（YYYY-MM-DD），按月用 strftime('%Y-%m', order_date)。
6. 表连接：order_items.order_id = orders.id，customers.id = orders.customer_id，
   products.id = order_items.product_id。
"""


class SQLGenerator:
    def __init__(self, api_key: str | None = None, base_url: str | None = None,
                 model: str | None = None, timeout: int = 30) -> None:
        self.api_key = api_key or os.getenv("OPENAI_API_KEY") or os.getenv("DEEPSEEK_API_KEY") or os.getenv("DASHSCOPE_API_KEY")
        self.base_url = (base_url or os.getenv("OPENAI_BASE_URL")
                         or ("https://api.deepseek.com/v1" if os.getenv("DEEPSEEK_API_KEY") else "https://api.openai.com/v1")).rstrip("/")
        self.model = model or os.getenv("LLM_MODEL") or ("deepseek-chat" if os.getenv("DEEPSEEK_API_KEY") else "gpt-4o-mini")
        self.timeout = timeout
        if not self.api_key:
            raise RuntimeError("未配置 API key")

    @property
    def name(self) -> str:
        return f"{self.model} @ {self.base_url}"

    def generate_sql(self, question: str, schema_hint: str, as_of=None) -> str:
        system = DEFAULT_PROMPT
        if as_of is not None:
            # 教训：不注入基准日期时，模型会写 date('now')，而「最近30天」应以数据截止日为准，
            # 否则静态快照库上会差一天，评测里就表现为"SQL 能跑但结果对不上"。
            system += (f"\n7. 数据截止日期是 {as_of}（库里最新订单日）。"
                       f"\n8. 提到「最近N天/本月/上月/今年」时，一律以 {as_of} 为基准，"
                       f"写死日期常量（例如 '{as_of}'），不要用 date('now')。")
        payload = {
            "model": self.model,
            "temperature": 0,
            "max_tokens": 400,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": f"可用表结构：\n{schema_hint}\n\n问题：{question}"},
            ],
        }
        req = urllib.request.Request(
            f"{self.base_url}/chat/completions",
            data=json.dumps(payload).encode(),
            headers={"Content-Type": "application/json", "Authorization": f"Bearer {self.api_key}"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=self.timeout) as resp:
            data = json.loads(resp.read())
        text = data["choices"][0]["message"]["content"]
        return self._clean(text)

    @staticmethod
    def _clean(text: str) -> str:
        text = text.strip()
        text = re.sub(r"^```(?:sql)?\s*|\s*```$", "", text, flags=re.I | re.M)
        m = re.search(r"(select|with)\b.*", text, re.I | re.S)
        return (m.group(0) if m else text).strip().rstrip(";")
