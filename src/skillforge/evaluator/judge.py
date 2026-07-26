"""Judge 配对比较：新旧输出 A/tied/B（不评绝对分）

对抗 Judge 分数漂移：绝对分容易被措辞、长度带偏。
覆盖维度：任务完成度、鲁棒性、可读性（效率走客观指标，不用 LLM 打分）。

每 20 次评估抽 1 条人工盲评校准；Phase 3 保底 20 条人工校准出偏差报告。
"""
from __future__ import annotations
from typing import Literal


Verdict = Literal["A_better", "tied", "B_better"]


class PairwiseJudge:
    def __init__(self, llm):
        self.llm = llm

    def compare(
        self,
        query: str,
        output_a: str,
        output_b: str,
        dimension: str,
    ) -> Verdict:
        raise NotImplementedError("Phase 3 implements")
