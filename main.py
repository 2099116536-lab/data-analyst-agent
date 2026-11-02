# -*- coding: utf-8 -*-
"""CLI 入口。

    python main.py ask "各城市销售额是多少？"
    python main.py ask "哪个城市卖得最好？" --json
    python main.py eval
    python main.py eval --out eval/results.json
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from src.agent import DataAnalystAgent
from src.eval import load_cases, run_eval

ROOT = Path(__file__).resolve().parent
DB = ROOT / "data" / "shop.db"
SETS = [ROOT / "eval" / "nl2sql_set.jsonl",
        ROOT / "eval" / "nl2sql_holdout.jsonl",
        ROOT / "eval" / "nl2sql_unseen.jsonl",
        ROOT / "eval" / "nl2sql_boundary.jsonl"]


def get_llm():
    """有 key 就用模型，没有就返回 None（规则基线兜底，链路不中断）。"""
    import os
    if not (os.getenv("OPENAI_API_KEY") or os.getenv("DEEPSEEK_API_KEY") or os.getenv("DASHSCOPE_API_KEY")):
        return None
    try:
        from src.llm import SQLGenerator
        return SQLGenerator()
    except Exception as exc:
        print(f"[warn] 模型不可用，使用规则基线：{exc}", file=sys.stderr)
        return None


def main() -> int:
    ap = argparse.ArgumentParser(description="Data Analyst Agent (NL2SQL + 自修复)")
    ap.add_argument("command", choices=["ask", "eval", "schema"])
    ap.add_argument("question", nargs="?", default="")
    ap.add_argument("--json", action="store_true", help="输出完整 JSON（含 SQL / 图表配置 / trace）")
    ap.add_argument("--out", default="", help="评测结果写入文件")
    ap.add_argument("--no-llm", action="store_true")
    args = ap.parse_args()

    agent = DataAnalystAgent(str(DB), llm=None if args.no_llm else get_llm())

    if args.command == "schema":
        print(agent.schema.full_prompt())
        return 0

    if args.command == "ask":
        if not args.question:
            print("需要提供问题，例如：python main.py ask \"各城市销售额是多少？\"")
            return 2
        st = agent.run(args.question)
        if args.json:
            print(json.dumps(st.to_dict(), ensure_ascii=False, indent=2, default=str))
        else:
            print(agent.report(st))
        return 0 if st.status in ("ok", "repaired") else 0

    result = run_eval(agent, load_cases(*SETS))
    print(json.dumps(result["summary"], ensure_ascii=False, indent=2))
    if args.out:
        Path(args.out).parent.mkdir(parents=True, exist_ok=True)
        Path(args.out).write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"\n明细已写入 {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
