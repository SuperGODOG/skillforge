"""规则层：keyword 子串命中

策略：**命中即等分**（不加权），命中数决定下一步：
    - 命中 skill 数 == 1  → 规则层可决策，直接选
    - 命中 skill 数 == 0  → fallback 到 embed 层
    - 命中 skill 数  > 1  → 并列多义，fallback 到 embed 层

不用"命中数 * 30"这种加分：避免开发者往 trigger 里塞冗余 keyword 刷分。
"""
from __future__ import annotations
from ..models import SkillMeta


class RuleLayer:
    def match(self, query: str, skills: list[SkillMeta]) -> dict[str, float]:
        """
        Args:
            query: 用户查询原文
            skills: 所有已注册 SkillMeta 列表

        Returns:
            {skill_name: 100.0}，只含至少有一个 keyword 命中的 skill
        """
        scores: dict[str, float] = {}
        for meta in skills:
            for kw in meta.trigger.keywords:
                if kw and kw in query:
                    scores[meta.name] = 100.0
                    break
        return scores
