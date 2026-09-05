"""LLM 兜底层：把 embedding top-K 候选交给 LLM 做二选一

只在规则 + embedding 都无明确胜者时才触发（延迟 ~500ms，成本最高）。
prompt 短小专注：只让 LLM 输出 skill_name 或 NONE，不引导它解释。
"""
from __future__ import annotations
import logging
from typing import Optional


_PROMPT_TEMPLATE = """你是严格的意图路由器。判断用户查询最匹配哪个 Skill；不匹配就明确输出 NONE。

用户查询：{query}

候选 Skill（按向量相似度排序，含每个 Skill 的适用与不适用场景）：
{candidates_block}

判定规则：
1. 用户查询若命中某 Skill 的"不适用场景"（not_for），**必须**输出 NONE
2. 用户查询与所有 Skill 的"适用场景"（use_when）都不真正匹配，输出 NONE
3. 只有当查询明确落在某 Skill 的 use_when 且不触发其 not_for 时，才选该 Skill
4. 宁可 NONE 也不硬选（错选比拒绝代价更高）

只输出候选中的一个 skill_name（原样，不加引号），或输出 NONE。不要任何解释。

答案："""


class LLMLayer:
    def __init__(self, llm):
        """
        Args:
            llm: hello_agents.HelloAgentsLLM 实例（或任何有 .invoke(messages) 方法的对象）
        """
        self.llm = llm
        self._log = logging.getLogger(__name__)

    def choose(
        self,
        query: str,
        candidates,
    ) -> Optional[str]:
        """
        Args:
            query:      用户查询
            candidates: 两种形态
                - list[tuple[str, str]]  (legacy, 只有 name+description)
                - list[SkillMeta]        (推荐，含 use_when + not_for，判定更准)

        Returns:
            chosen skill_name；所有候选都不匹配返回 None
        """
        if not candidates:
            return None

        # 兼容两种入参形态
        block_lines = []
        candidate_names: list[str] = []
        for i, c in enumerate(candidates):
            if isinstance(c, tuple):
                name, desc = c
                candidate_names.append(name)
                block_lines.append(f"{i + 1}. {name}\n   描述: {desc}")
            else:
                # SkillMeta 对象
                candidate_names.append(c.name)
                block_lines.append(f"{i + 1}. {c.name}")
                block_lines.append(f"   描述: {c.description}")
                block_lines.append(f"   适用: {c.use_when}")
                if c.not_for:
                    block_lines.append(f"   不适用: {', '.join(c.not_for)}")
        block = "\n".join(block_lines)

        prompt = _PROMPT_TEMPLATE.format(query=query, candidates_block=block)

        try:
            resp = self.llm.invoke([{"role": "user", "content": prompt}])
        except Exception as e:
            self._log.warning("LLM 调用失败：%s；返回 None（相当于拒绝路由）", e)
            return None

        # hello-agents >=1.0 返回 LLMResponse 对象；旧版返回 str
        content = getattr(resp, "content", resp) or ""
        return self._parse(str(content), candidate_names)

    @staticmethod
    def _parse(resp: str, candidate_names: list[str]) -> Optional[str]:
        """从 LLM 响应里提取 skill_name

        容错策略（LLM 可能有解释/引号/换行）：
        - 明确 NONE / 拒绝表达 → None
        - 精确子串匹配任一 candidate name（最长优先，避免前缀撞车）
        """
        text = (resp or "").strip()
        upper = text.upper()

        if not text:
            return None
        if "NONE" in upper.split()[:3] or upper.startswith("NONE"):
            return None

        names = sorted(candidate_names, key=len, reverse=True)
        for name in names:
            if name in text:
                return name

        return None
