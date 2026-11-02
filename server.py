# -*- coding: utf-8 -*-
"""HTTP 服务：POST /ask 返回 SQL + 结果 + 图表配置 + 结论。

两种运行方式（都不强制装第三方包）
1. 装了 fastapi + uvicorn：uvicorn server:app --reload --port 8010
2. 没装：python server.py  → 用标准库 http.server 提供同样的路由

之所以做标准库兜底：这个项目的评测和 CI 都跑在裸环境里，
如果引入 web 框架就变成"想跑通先装 6 个包"，成本和收益不匹配。
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

from src.agent import DataAnalystAgent  # noqa: E402

DB = ROOT / "data" / "shop.db"


def _make_agent() -> DataAnalystAgent:
    llm = None
    if os.getenv("DEEPSEEK_API_KEY") or os.getenv("OPENAI_API_KEY"):
        try:
            from src.llm import SQLGenerator
            llm = SQLGenerator()
        except Exception:
            llm = None
    return DataAnalystAgent(str(DB), llm=llm)


AGENT = _make_agent()

# ------------------------- FastAPI 版本 -------------------------
try:
    from fastapi import FastAPI
    from pydantic import BaseModel

    app = FastAPI(title="Data Analyst Agent", version="1.0")

    class AskIn(BaseModel):
        question: str

    @app.get("/health")
    def health() -> dict:
        return {"ok": True, "as_of": AGENT.as_of.isoformat(),
                "llm": bool(AGENT.llm), "tables": list(AGENT.schema.tables)}

    @app.get("/schema")
    def schema() -> dict:
        return {"prompt": AGENT.schema.full_prompt()}

    @app.post("/ask")
    def ask(req: AskIn) -> dict:
        return AGENT.run(req.question).to_dict()

except Exception:  # pragma: no cover - 没装 fastapi 时走标准库
    app = None


# ------------------------- 标准库版本 -------------------------
def serve_stdlib(port: int = 8010) -> None:
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

    class Handler(BaseHTTPRequestHandler):
        def _json(self, code: int, payload: dict) -> None:
            body = json.dumps(payload, ensure_ascii=False, default=str).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self) -> None:  # noqa: N802
            if self.path == "/health":
                self._json(200, {"ok": True, "as_of": AGENT.as_of.isoformat(), "llm": bool(AGENT.llm)})
            elif self.path == "/schema":
                self._json(200, {"prompt": AGENT.schema.full_prompt()})
            else:
                self._json(404, {"error": "not found"})

        def do_POST(self) -> None:  # noqa: N802
            if self.path != "/ask":
                self._json(404, {"error": "not found"})
                return
            n = int(self.headers.get("Content-Length") or 0)
            try:
                data = json.loads(self.rfile.read(n) or b"{}")
                q = (data.get("question") or "").strip()
                if not q:
                    self._json(400, {"error": "question 不能为空"})
                    return
                self._json(200, AGENT.run(q).to_dict())
            except Exception as exc:
                self._json(500, {"error": str(exc)})

        def log_message(self, *a) -> None:  # 安静点
            pass

    print(f"标准库 HTTP 服务已启动：http://127.0.0.1:{port}/ask  (Ctrl+C 退出)")
    ThreadingHTTPServer(("127.0.0.1", port), Handler).serve_forever()


if __name__ == "__main__":
    if app is not None:
        try:
            import uvicorn
            uvicorn.run(app, host="127.0.0.1", port=8010)
        except ImportError:
            serve_stdlib()
    else:
        serve_stdlib()
