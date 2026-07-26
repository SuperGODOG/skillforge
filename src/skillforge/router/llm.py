"""LLM 兜底层：候选二选一 prompt"""
from __future__ import annotations
from typing import Optional


class LLMLayer:
    def __init__(self, llm):
        self.llm = llm

    def choose(self, query: str, candidates: list[tuple[str, str]]) -> Optional[str]:
        """
        Args:
            candidates: [(skill_name, description), ...] 已按 embedding 相似度排序
        Returns:
            chosen skill_name；都不匹配则返回 None
        """
        raise NotImplementedError("Phase 2 implements")
