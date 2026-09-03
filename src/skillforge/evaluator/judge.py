"""Judge 配对比较：新旧输出 A/tied/B（不评绝对分）

对抗 Judge 分数漂移——绝对分容易被措辞、长度、格式带偏。
配对比较让 Judge 只做相对判断。

维度覆盖：task_completion / robustness / readability
（efficiency 走客观指标，不用 LLM 打分）

Phase 3 保底：每 20 次抽 1 条人工盲评校准；Judge/人工分歧 > 30% 调 prompt。
参见 ARCHITECTURE §4-E
"""
from __future__ import annotations
import logging
from typing import Literal, Optional


Verdict = Literal["A_better", "tied", "B_better"]


DIMENSION_HINTS = {
    "task_completion": "任务是否被真正解决（不是文字漂亮而是问题解决了）",
    "robustness": "异常输入是否降级或明确拒绝；是否幻觉/编造未知信息",
    "readability": "结构清晰、有帮助、易读；不啰嗦不空话",
}


_PROMPT_TEMPLATE = """你是严格的评审员。判断两个 Agent 回答在 **{dimension}** 维度上哪个更好。

维度定义：{dimension_hint}

用户查询：{query}
{reference_block}
方案 A：
{output_a}

方案 B：
{output_b}

只输出以下三个词之一（不要解释）：
- A_better    A 更好
- tied         两者相当
- B_better    B 更好

评审："""


class PairwiseJudge:
    def __init__(self, llm):
        self.llm = llm
        self._log = logging.getLogger(__name__)

    def compare(
        self,
        query: str,
        output_a: str,
        output_b: str,
        dimension: str,
        reference: Optional[str] = None,
    ) -> Verdict:
        """
        Returns: "A_better" / "tied" / "B_better"
        """
        ref_block = f"参考期望：{reference}\n" if reference else ""
        prompt = _PROMPT_TEMPLATE.format(
            dimension=dimension,
            dimension_hint=DIMENSION_HINTS.get(dimension, dimension),
            query=query,
            reference_block=ref_block,
            output_a=output_a.strip(),
            output_b=output_b.strip(),
        )

        try:
            resp = self.llm.invoke([{"role": "user", "content": prompt}])
        except Exception as e:
            self._log.warning("Judge 调用失败：%s；返回 tied 兜底", e)
            return "tied"

        content = getattr(resp, "content", resp) or ""
        return self._parse(str(content))

    @staticmethod
    def _parse(text: str) -> Verdict:
        """从 LLM 响应里提取判定"""
        t = text.strip()
        low = t.lower()
        if "a_better" in low or "a 更好" in t or (low.startswith("a") and "b" not in low[:3]):
            return "A_better"
        if "b_better" in low or "b 更好" in t or (low.startswith("b") and "a" not in low[:3]):
            return "B_better"
        return "tied"
