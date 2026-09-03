"""Embedding 层：结构化检索卡片 + bge-small-zh-v1.5 编码

结构化检索卡片（拉开"字面相近但意图不同"的 skill 在向量空间的距离）：
    [Capability] {description}
    [Use When]   {use_when}
    [Examples]   {ex1} | {ex2} | ...
    [Not For]    {nf1} | {nf2} | ...

Not For 段是关键：让 write_weekly_report 的 not_for=["会议纪要","月报"]
在向量空间主动排斥"帮我写会议纪要"这类硬负例。

模型来源：modelscope 下载到本地 models/ 目录（Phase 2 T0 结论，见 ARCHITECTURE §10.2 差异 7）。
"""
from __future__ import annotations
from pathlib import Path
from typing import Optional

import numpy as np


# 默认本地路径：<repo_root>/models/models/AI-ModelScope--bge-small-zh-v1.5/snapshots/master
DEFAULT_MODEL_DIR = (
    Path(__file__).resolve().parents[3]
    / "models" / "models" / "AI-ModelScope--bge-small-zh-v1.5" / "snapshots" / "master"
)


class EmbedLayer:
    def __init__(self, model_dir: Optional[Path] = None):
        self.model_dir = Path(model_dir) if model_dir else DEFAULT_MODEL_DIR
        self._model = None
        self._skill_vecs: dict[str, np.ndarray] = {}

    def _get_model(self):
        if self._model is None:
            from sentence_transformers import SentenceTransformer
            if not self.model_dir.exists():
                raise FileNotFoundError(
                    f"bge 模型目录不存在：{self.model_dir}\n"
                    f"请先跑：bash scripts/setup_modelscope.sh 或"
                    f" 参考 README「环境准备」的 modelscope 步骤"
                )
            self._model = SentenceTransformer(str(self.model_dir))
        return self._model

    @staticmethod
    def encode_card(skill) -> str:
        """把 SkillMeta 编码成结构化检索卡片文本"""
        parts = [
            f"[Capability] {skill.description}",
            f"[Use When] {skill.use_when}",
        ]
        if skill.examples:
            parts.append(f"[Examples] {' | '.join(skill.examples)}")
        if skill.not_for:
            parts.append(f"[Not For] {' | '.join(skill.not_for)}")
        return "\n".join(parts)

    def index_skills(self, skills: list) -> None:
        """一次性编码所有 skill 的检索卡片，缓存归一化后向量（后续 search 直接查表）"""
        if not skills:
            self._skill_vecs = {}
            return
        cards = [self.encode_card(s) for s in skills]
        vecs = self._get_model().encode(cards, normalize_embeddings=True)
        self._skill_vecs = {s.name: np.asarray(v) for s, v in zip(skills, vecs)}

    def search(self, query: str, top_k: int = 5) -> list[tuple[str, float]]:
        """
        Args:
            query: 用户查询
            top_k: 返回前 K 个

        Returns:
            [(skill_name, cosine_similarity), ...] 按 sim 降序
        """
        if not self._skill_vecs:
            return []
        qvec = self._get_model().encode([query], normalize_embeddings=True)[0]
        qvec = np.asarray(qvec)
        # 已归一化，dot = cosine
        scored = [(name, float(np.dot(qvec, vec))) for name, vec in self._skill_vecs.items()]
        scored.sort(key=lambda x: x[1], reverse=True)
        return scored[:top_k]
