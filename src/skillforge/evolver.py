"""SkillEvolver：候选生成器 + 初筛器（不是最终决策者）

继承 hello_agents.SimpleAgent
六步流程：失败样本收集 → LLM 根因分析 → 候选生成 → 沙箱验证 → 分级发布 → 归档
分级：L1 自动 / L2 REVIEW / L3 只出建议
成功率坦诚约 30%（10 次迭代约 3 次通过棘轮）

Phase 4 实现，参见方案书 §4.5、ARCHITECTURE §4-D
"""
from __future__ import annotations
from typing import List, Optional

from hello_agents import SimpleAgent, HelloAgentsLLM

from .models import Patch


SYSTEM_PROMPT_DEFAULT = (
    "你是 SkillForge 的元 Agent。"
    "任务：分析 Skill 的失败样本、找出根因、生成 3-5 个改进候选 patch。\n"
    "改动分级：\n"
    "  L1 = 补充 examples / not_for / description（不改语义边界）\n"
    "  L2 = 修改 trigger / instructions（可能影响路由与行为）\n"
    "  L3 = 修改 dependencies / 安全 constraints（改动权限或安全边界）\n"
    "每个候选必须给出 diff 与推理链（rationale）。"
)


class SkillEvolver(SimpleAgent):
    def __init__(self, llm: HelloAgentsLLM, system_prompt: Optional[str] = None):
        super().__init__(
            name="skill_evolver",
            llm=llm,
            system_prompt=system_prompt or SYSTEM_PROMPT_DEFAULT,
        )

    def evolve(self, skill_name: str, max_candidates: int = 5) -> List[Patch]:
        """对指定 Skill 生成 max_candidates 个改进候选"""
        raise NotImplementedError("Phase 4 implements")
