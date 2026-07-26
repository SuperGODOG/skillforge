"""八维评估器：结构分 40%（不阻断）+ 效果分 60%（发布门槛）+ 棘轮机制

参见方案书 §4.4、ARCHITECTURE §4-E
"""
from __future__ import annotations
from typing import Optional

from hello_agents.tools import Tool, ToolParameter

from ..models import EvalResult, RatchetVerdict


class SkillEvaluator(Tool):
    def __init__(self, llm=None):
        super().__init__(
            name="skill_evaluator",
            description="八维评估器 + 棘轮门槛判定",
        )
        self.llm = llm

    def get_parameters(self) -> list[ToolParameter]:
        return [
            ToolParameter(
                name="release_id", type="string",
                description="待评估的 release_id", required=True,
            ),
            ToolParameter(
                name="eval_set", type="string",
                description="评估集名（默认 baseline_dev）",
                required=False, default="baseline_dev",
            ),
        ]

    def run(self, parameters: dict) -> str:
        result = self.evaluate(
            parameters["release_id"],
            parameters.get("eval_set", "baseline_dev"),
        )
        return f"eval OK, p0_pass={result.p0_pass}"

    def evaluate(self, release_id: str, eval_set: str = "baseline_dev") -> EvalResult:
        raise NotImplementedError("Phase 3 implements")

    def check_ratchet(
        self,
        old: Optional[EvalResult],
        new: EvalResult,
    ) -> RatchetVerdict:
        raise NotImplementedError("Phase 3 implements")


__all__ = ["SkillEvaluator"]
