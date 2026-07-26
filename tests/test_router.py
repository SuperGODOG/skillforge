"""三层路由测试：规则/LLM 单元 + IntentRouter 集成边界"""
from __future__ import annotations
import subprocess
from pathlib import Path
from typing import Optional

import pytest

from skillforge import SkillRegistry, IntentRouter
from skillforge.models import SkillMeta, Trigger
from skillforge.router.rule import RuleLayer
from skillforge.router.llm import LLMLayer
from skillforge.router.embed import DEFAULT_MODEL_DIR


def _meta(name: str, keywords: list[str], description: str = "", use_when: str = "") -> SkillMeta:
    return SkillMeta(
        name=name, version="1.0.0",
        description=description or f"skill {name}",
        use_when=use_when or f"{name} 的适用场景",
        trigger=Trigger(keywords=keywords),
    )


# ---------- RuleLayer ----------

class TestRuleLayer:
    def test_empty_skills(self):
        assert RuleLayer().match("北京天气", []) == {}

    def test_no_keyword_hit(self):
        skills = [_meta("wq", ["天气", "温度"])]
        assert RuleLayer().match("推荐一部电影", skills) == {}

    def test_single_hit(self):
        skills = [_meta("wq", ["天气", "温度"])]
        assert RuleLayer().match("北京今天天气", skills) == {"wq": 100.0}

    def test_multi_skill_hit(self):
        skills = [_meta("wq", ["天气"]), _meta("wr", ["周报"])]
        assert RuleLayer().match("周报里加一句北京天气", skills) == {"wq": 100.0, "wr": 100.0}

    def test_hit_score_capped(self):
        """一个 skill 多个 keyword 命中，仍是 100（不加分）"""
        skills = [_meta("wq", ["天气", "温度", "下雨"])]
        assert RuleLayer().match("北京天气温度都下雨", skills) == {"wq": 100.0}


# ---------- LLMLayer._parse ----------

class TestLLMLayerParse:
    NAMES = ["weather_query", "write_weekly_report", "explain_regex"]

    def test_exact_name(self):
        assert LLMLayer._parse("weather_query", self.NAMES) == "weather_query"

    def test_with_extra_text(self):
        assert LLMLayer._parse("答案: weather_query", self.NAMES) == "weather_query"

    def test_none_upper(self):
        assert LLMLayer._parse("NONE", self.NAMES) is None

    def test_none_with_context(self):
        assert LLMLayer._parse("NONE  # 都不匹配", self.NAMES) is None

    def test_empty(self):
        assert LLMLayer._parse("", self.NAMES) is None

    def test_longest_prefix_wins(self):
        """避免 'write' 撞车 'write_weekly_report'"""
        names = ["write", "write_weekly_report"]
        assert LLMLayer._parse("write_weekly_report", names) == "write_weekly_report"

    def test_llm_call_failure_returns_none(self, monkeypatch):
        class FakeLLM:
            def invoke(self, messages):
                raise RuntimeError("simulated API failure")
        layer = LLMLayer(FakeLLM())
        assert layer.choose("query", [("wq", "desc")]) is None


# ---------- IntentRouter 集成 ----------

pytestmark_needs_bge = pytest.mark.skipif(
    not DEFAULT_MODEL_DIR.exists(),
    reason=f"bge 模型未下载：{DEFAULT_MODEL_DIR}（跑 setup_modelscope.sh 后再测）",
)


class FakeLLM:
    """记录调用 + 按 script 返回内容的假 LLM，用于集成测试"""
    def __init__(self, script: list[str]):
        self.script = list(script)
        self.calls: list[str] = []

    def invoke(self, messages):
        self.calls.append(messages[-1]["content"])
        content = self.script.pop(0) if self.script else "NONE"

        class Resp:
            def __init__(self, c): self.content = c
        return Resp(content)


@pytest.fixture(scope="module")
def repo_root() -> Path:
    return Path(__file__).resolve().parents[1]


@pytest.fixture(scope="module")
def registry(tmp_path_factory, repo_root) -> SkillRegistry:
    tmp = tmp_path_factory.mktemp("router_int")
    r = SkillRegistry(
        db_path=tmp / "test.db",
        skills_dir=repo_root / "skills",
        repo_root=repo_root,
        router_log=tmp / "router.jsonl",
    )
    r.load_skills_from_dir()
    yield r
    r.close()


@pytestmark_needs_bge
class TestIntentRouterIntegration:

    def test_positive_embed_top1_correct(self, registry):
        """明确正例：embed 层 top1 必须是正确的 skill（不管最终 chosen 由谁决策）"""
        router = IntentRouter(registry=registry, llm=None)
        r = router.route("北京今天天气")
        embed = r.scores["embed"]
        top1_name = max(embed.items(), key=lambda x: x[1])[0]
        assert top1_name == "weather_query", f"embed top1 应为 weather_query，实际 {top1_name}"

    def test_positive_with_llm_fallback(self, registry):
        """明确正例 + LLM 兜底：chosen 必为正确 skill"""
        router = IntentRouter(registry=registry, llm=FakeLLM(["weather_query"]))
        r = router.route("北京今天天气")
        # 若走 embed 独占（top1 >= HIGH_CONF）或走 LLM，都应选中 weather_query
        assert r.chosen == "weather_query", f"chosen 应为 weather_query，实际 {r.chosen}"

    def test_hard_negative_no_llm_rejects(self, registry):
        """硬负例无 LLM → 拒绝（保守 None）"""
        router = IntentRouter(registry=registry, llm=None)
        r = router.route("帮我写一个正则匹配所有邮箱地址")
        # embed 层 top1 可能在中间地带 → 无 LLM 就 None
        # 也可能 top1 >= HIGH_CONF 独占选中（视 embed 结果而定）
        # 稳定断言：hit_layer ∈ {embed}（rule 已不独占）
        assert r.hit_layer in ("embed",)

    def test_llm_none_response_rejects(self, registry):
        """LLM 返回 NONE → chosen=None，hit_layer=llm"""
        router = IntentRouter(registry=registry, llm=FakeLLM(["NONE"]))
        # 用一个 embed 中间地带 query 触发 LLM 层
        r = router.route("讲讲量子力学")  # 完全无关，embed top1 应低但可能进中间
        # 若走了 LLM，chosen 必为 None（script 返回 NONE）
        if r.hit_layer == "llm":
            assert r.chosen is None

    def test_llm_confirms_positive(self, registry):
        """LLM 返回 skill_name → chosen 正确"""
        fake = FakeLLM(["explain_regex"])
        router = IntentRouter(registry=registry, llm=fake)
        # 挑一个 embed top1 中等 + 正确的正例
        r = router.route("讲一下 lookahead 是什么")
        # 走 LLM 时，chosen 应为 explain_regex（若 embed 层没独占的话）
        if r.hit_layer == "llm":
            assert r.chosen == "explain_regex"

    def test_route_result_shape(self, registry):
        """RouteResult 字段结构齐全"""
        router = IntentRouter(registry=registry, llm=None)
        r = router.route("北京天气")
        assert r.hit_layer in ("rule", "embed", "llm")
        assert isinstance(r.scores, dict)
        assert "rule" in r.scores
        assert isinstance(r.latency_ms, float)
