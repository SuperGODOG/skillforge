"""规则层：keyword 命中 + 权重排序"""
from __future__ import annotations


class RuleLayer:
    """基于 SkillMeta.trigger.keywords 的规则匹配"""

    def match(self, query: str, skills: list) -> dict[str, float]:
        """Returns: {skill_name: score}"""
        raise NotImplementedError("Phase 2 implements")
