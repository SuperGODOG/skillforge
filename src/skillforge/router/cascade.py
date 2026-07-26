"""IntentRouter：三层路由编排（规则 → embed → LLM 兜底）

不写死覆盖率百分比，靠 fallback 条件级联：
    规则 top1-top2 分差 < 20 → embed
    embed 最高相似度 < 阈值 → llm

Phase 2 实现，参见方案书 §4.3、ARCHITECTURE §4-B
"""
from __future__ import annotations
from typing import Optional

from hello_agents.tools import Tool, ToolParameter

from ..models import RouteResult


class IntentRouter(Tool):
    def __init__(self):
        super().__init__(
            name="intent_router",
            description="三层级联路由：规则 → embedding → LLM 兜底",
        )

    def get_parameters(self) -> list[ToolParameter]:
        return [
            ToolParameter(
                name="query", type="string",
                description="用户查询", required=True,
            ),
        ]

    def run(self, parameters: dict) -> str:
        result = self.route(parameters["query"])
        return result.chosen or "NONE"

    def route(self, query: str, candidates: Optional[list[str]] = None) -> RouteResult:
        raise NotImplementedError("Phase 2 implements")
