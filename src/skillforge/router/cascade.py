"""IntentRouter：三层级联路由编排（规则 → embed → LLM 兜底）

fallback 条件（不写死覆盖率百分比，靠置信度判定）：
    **规则层：只做记账，不独占决策**（trigger.keywords 是排序信号，不做自动展开）
    Embed 层：top1 >= HIGH_CONF 且 top1-top2 >= MARGIN → 决策
              top1 < LOW_CONF                          → 直接拒绝（不值得跑 LLM）
              中间地带                                 → fallback LLM
    LLM 层：候选 top-K 交给 LLM 二选一 → 决策（可为 None）
    无 LLM 且中间地带                              → 保守拒绝（None）

设计缘由：早期版本让"规则独占命中"直接决策，导致 18 条硬负例里 17 条被误 route
（比如"帮我写一个正则匹配邮箱"命中'正则' keyword → explain_regex）。
改为规则不独占后，硬负例交给 embed + LLM 用语义识别意图冲突。
调优三步（62%→98% R@1）详见 __log/2026-07-26-router-hardnegative-fix/

参见方案书 §4.3、§4.1、ARCHITECTURE §4-B
"""
from __future__ import annotations
import time
from pathlib import Path
from typing import Optional

from hello_agents.tools import Tool, ToolParameter

from ..models import RouteResult
from .rule import RuleLayer
from .embed import EmbedLayer
from .llm import LLMLayer


# 阈值——按 50 条硬负例评测集调优后的值
HIGH_CONF = 0.75   # embed 独占决策门槛（提高避免硬负例被 embed 误选，交 LLM 校验）
MARGIN = 0.10      # embed top1-top2 分差门槛
LOW_CONF = 0.35    # 低于此直接拒绝，不进 LLM


class IntentRouter(Tool):
    def __init__(self, registry, llm=None, model_dir: Optional[Path] = None):
        """
        Args:
            registry: SkillRegistry 实例（提供 list_names / get_meta）
            llm:      hello_agents.HelloAgentsLLM 实例（可选；None → 无 LLM 兜底）
            model_dir: bge 模型本地路径（可选；None → 用 embed.DEFAULT_MODEL_DIR）
        """
        super().__init__(
            name="intent_router",
            description="三层级联路由：规则 → embedding → LLM 兜底",
        )
        self.registry = registry
        self.rule = RuleLayer()
        self.embed = EmbedLayer(model_dir=model_dir)
        self.llm = LLMLayer(llm) if llm else None
        self._indexed = False

    def get_parameters(self) -> list[ToolParameter]:
        return [
            ToolParameter(name="query", type="string", description="用户查询", required=True),
        ]

    def run(self, parameters: dict) -> str:
        r = self.route(parameters["query"])
        return r.chosen or "NONE"

    def _ensure_indexed(self) -> None:
        if not self._indexed:
            skills = [self.registry.get_meta(n) for n in self.registry.list_names()]
            self.embed.index_skills(skills)
            self._indexed = True

    def route(self, query: str, candidates: Optional[list[str]] = None) -> RouteResult:
        """三层级联决策；始终返回 RouteResult（chosen 可为 None 表示拒绝）"""
        started = time.perf_counter()
        skills = [self.registry.get_meta(n) for n in self.registry.list_names()]

        # === 规则层：只做记账（trigger.keywords 是信号，不独占决策）===
        rule_scores = self.rule.match(query, skills)

        # === Embed 层：始终跑（前提是模型可用）===
        try:
            self._ensure_indexed()
            embed_top = self.embed.search(query, top_k=5)
        except FileNotFoundError:
            # bge 模型未装：退化到"仅规则"，规则唯一命中才决策
            snapshot = {"rule": rule_scores, "embed": {}}
            if len(rule_scores) == 1:
                return self._done(next(iter(rule_scores)), "rule", snapshot, started)
            return self._done(None, "rule", snapshot, started)

        scores_snapshot: dict = {"rule": rule_scores, "embed": dict(embed_top)}

        if not embed_top:
            return self._done(None, "embed", scores_snapshot, started)

        top1_name, top1_sim = embed_top[0]
        top2_sim = embed_top[1][1] if len(embed_top) > 1 else 0.0
        margin = top1_sim - top2_sim

        # 高置信 + 明确分差 → embed 独占决策
        if top1_sim >= HIGH_CONF and margin >= MARGIN:
            return self._done(top1_name, "embed", scores_snapshot, started)

        # 极低置信 → 直接拒绝（不值得跑 LLM）
        if top1_sim < LOW_CONF:
            return self._done(None, "embed", scores_snapshot, started)

        # 中间地带 → LLM 兜底（传 SkillMeta，让 LLM 看到 use_when + not_for）
        if self.llm:
            candidate_metas = [self.registry.get_meta(name) for name, _ in embed_top]
            chosen = self.llm.choose(query, candidate_metas)
            scores_snapshot["llm_chosen"] = chosen
            return self._done(chosen, "llm", scores_snapshot, started)

        # 无 LLM 兜底且中间地带 → 保守拒绝
        # （旧版本硬选 top1 导致 17/18 硬负例被误 route）
        return self._done(None, "embed", scores_snapshot, started)

    @staticmethod
    def _done(
        chosen: Optional[str],
        hit_layer: str,
        scores: dict,
        started: float,
    ) -> RouteResult:
        latency_ms = (time.perf_counter() - started) * 1000
        return RouteResult(
            chosen=chosen,
            hit_layer=hit_layer,
            scores=scores,
            latency_ms=round(latency_ms, 2),
        )
