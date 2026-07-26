"""Embedding 层：结构化检索卡片 + bge-small-zh-v1.5 编码

结构化检索卡片格式：
    [Capability] {description}
    [Use When] {use_when}
    [Examples] {examples[0]} | {examples[1]} | ...
    [Not For] {not_for[0]} | {not_for[1]} | ...

Not For 段的作用：让"语义相近但不该匹配"的场景在向量空间被主动推远。
"""
from __future__ import annotations


class EmbedLayer:
    def __init__(self, model_name: str = "BAAI/bge-small-zh-v1.5"):
        self.model_name = model_name
        self._model = None  # lazy load

    def encode_card(self, skill) -> str:
        """把 Skill 编码成结构化检索卡片"""
        raise NotImplementedError("Phase 2 implements")

    def search(self, query: str, top_k: int = 5) -> list[tuple[str, float]]:
        """Returns: [(skill_name, cosine_similarity), ...] 降序"""
        raise NotImplementedError("Phase 2 implements")
