"""Tests for SkillForge P1-H: Controlled Reflection Loop & Anti-Reward Hacking Guardrails.

Covers:
1. Minimum Blackboard: EvolveContext / AttemptRecord / AttemptFeedback (14 exact fields per codex2 §4.2).
2. Rounds State Machine:
   - max_rounds=2, round 1 <= 3 candidates, round 2 = 1 candidate, total cap <= 4.
   - PASS / REVIEW in round 1 stops immediately with no retry.
   - Counter-example (a): Two rounds failed cleanly stops (stop_reason="ROUNDS_EXHAUSTED", no infinite loop).
3. Reflection Input & Isolation:
   - Counter-example (d): Holdout/audit case IDs, queries, outputs strictly filtered out from feedback.
   - Direct breach detection via assert_reflection_isolation.
   - Judge INVALID, TIMEOUT, FIXTURE_ERROR excluded from reflection.
4. Anti-Reward Hacking 8 Defense Lines:
   - Counter-example (b): Repeated fingerprint circuit breaker stops with DECLINED and REPEATED_FINGERPRINT_STOP.
   - Nonce fixture (AmapWeatherFixture nonce variations and check_mock_hardcoding).
   - Neighbor variants generation.
   - Patch leakage scanner (case IDs, queries, references, fixture constants).
5. Shadow Mode & Feature Flag:
   - Counter-example (c): Shadow archive contains attempt_no, round_no, reason_codes, candidate_digest, shadow_mode=true (real file assertions).
   - Feature flag enable_reflection defaults to False, shadow_mode defaults to True.
"""
from __future__ import annotations
import json
from pathlib import Path
from types import SimpleNamespace
import pytest

from skillforge.models import (
    SkillMeta,
    Trigger,
    EvalResult,
    RatchetVerdict,
    Patch,
    EvolveBudget,
    EvolveContext,
    AttemptRecord,
    AttemptFeedback,
)
from skillforge.evolver import (
    SkillEvolver,
    compute_skill_fingerprint,
    scan_patch_leakage,
    generate_neighbor_variants,
    assert_reflection_isolation,
    _select_reflection_candidate,
    _build_attempt_feedback,
    _archive_suggestion,
    _archive_failure,
    _publish_patch,
    _validate_patch,
    _load_dataset_layer_ids,
    is_forbidden_eval_set,
    assert_eval_set_for_iter,
    is_candidate_eval_invalid,
    run_final_audit_gate,
    validate_neighbor_variants,
)
from skillforge.evaluator.fixtures import (
    AmapWeatherFixture,
    create_nonce_weather_fixture,
    check_mock_hardcoding,
    _FIXTURE_FACTORIES,
)


class CapturingFakeLLM:
    """Fake LLM that responds with canned responses and records incoming prompts."""

    def __init__(self, responses: list[str], model: str = "test-model"):
        self.responses = list(responses)
        self.model = model
        self.invocations: list[list[dict]] = []

    def invoke(self, messages, **kwargs):
        self.invocations.append(messages)
        content = self.responses.pop(0) if self.responses else "{}"
        return SimpleNamespace(content=content, usage={"total_tokens": 120})


def _make_skill_meta(name: str = "weather_query") -> SkillMeta:
    return SkillMeta(
        name=name,
        version="1.0.0",
        description="天气查询",
        use_when="用户询问天气",
        not_for=["新闻"],
        dependencies=["amap_weather_api"],
        trigger=Trigger(keywords=["天气", "气温", "下雨"]),
        examples=["北京今天天气怎么样"],
    )


# =========================================================================
# 1. 最小黑板数据结构验收（AttemptFeedback 14 字段完整性）
# =========================================================================

def test_attempt_feedback_14_fields():
    """验证 AttemptFeedback 严格包含 codex2 §4.2 定稿要求的全部 14 个强类型字段。"""
    feedback = AttemptFeedback(
        attempt_no=1,
        original_skill_digest="abc12345",
        candidate_digest="def67890",
        declared_level="L1",
        computed_level="L1",
        strategy="routing_metadata",
        candidate_diff="--- old\n+++ new",
        failed_cases=[{"case_id": "repair_01", "query": "查询天气"}],
        ratchet_reasons=["任务完成度退步"],
        p0_status=True,
        authenticity_status=True,
        prompt_budget_status="PASS",
        repeated_patch_fingerprints=["abc12345"],
        remaining_budget={"calls_remaining": 5, "tokens_remaining": 1000},
    )

    assert feedback.attempt_no == 1
    assert feedback.original_skill_digest == "abc12345"
    assert feedback.candidate_digest == "def67890"
    assert feedback.declared_level == "L1"
    assert feedback.computed_level == "L1"
    assert feedback.strategy == "routing_metadata"
    assert "--- old" in feedback.candidate_diff
    assert len(feedback.failed_cases) == 1
    assert feedback.ratchet_reasons == ["任务完成度退步"]
    assert feedback.p0_status is True
    assert feedback.authenticity_status is True
    assert feedback.prompt_budget_status == "PASS"
    assert feedback.repeated_patch_fingerprints == ["abc12345"]
    assert feedback.remaining_budget["calls_remaining"] == 5


def test_attempt_record_and_context():
    """验证 AttemptRecord 与 EvolveContext 黑板跟踪状态。"""
    rec = AttemptRecord(
        attempt_no=1,
        strategy="routing_metadata",
        candidate_digest="cand1",
        computed_level="L1",
        verdict="DECLINED",
        reason_codes=["RAT_DROP"],
        round_no=1,
    )
    assert rec.attempt_no == 1
    assert rec.round_no == 1
    assert rec.reason_codes == ["RAT_DROP"]

    ctx = EvolveContext(
        skill_name="weather_query",
        original_digest="orig1",
        round_no=1,
        attempts=[rec],
        seen_fingerprints={"orig1", "cand1"},
    )
    assert len(ctx.attempts) == 1
    assert "cand1" in ctx.seen_fingerprints


# =========================================================================
# 2. 反例 6.(b): Repeated Fingerprint 熔断
# =========================================================================

def test_repeated_fingerprint_circuit_breaker(tmp_path: Path):
    """反例 6.(b): 当候选 patch 指纹与已有版本重复时，熔断机制直接停止，不进入重试。"""
    from skillforge.evolver import _reconstruct_skill_md
    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    skills_dir = repo_root / "skills" / "weather_query"
    skills_dir.mkdir(parents=True)
    meta = _make_skill_meta("weather_query")
    body = "## Instructions\n查询天气"
    baseline_skill_md = _reconstruct_skill_md(meta, body)
    (skills_dir / "SKILL.md").write_text(baseline_skill_md, encoding="utf-8")

    class MockRegistry:
        def __init__(self):
            self.repo_root = repo_root
            self.skills_dir = repo_root / "skills"
            self._bodies = {"weather_query": body}
        def get_meta(self, name):
            return meta

    base_eval = EvalResult(
        release_id="base_0",
        structure_score={"schema": 10.0},
        effect_score={"task": 10.0},
        objective_metrics={},
        p0_pass=True,
        case_verdicts=[{"case_id": "r1", "task_completion": "B_better"}],
        case_outputs=[{"case_id": "r1", "query": "北京天气", "reference": "晴", "output_skill": "雨", "output_baseline": "晴"}],
        valid=True,
    )

    class MockEvaluator:
        def __init__(self):
            self.llm = None
        def evaluate_skill(self, *args, **kwargs):
            return base_eval

    identical_patch_md = baseline_skill_md.replace("version: 1.0.0", "version: 1.0.1")
    identical_patch_json = json.dumps([{
        "level": "L2",
        "new_skill_md": identical_patch_md,
        "rationale": "未做实质修改仅bump版本",
    }])

    llm = CapturingFakeLLM([
        json.dumps({"prompt_vague": {"prob": 0.8, "why": "提示词不明确"}}),
        identical_patch_json,
    ])

    evolver = SkillEvolver(
        registry=MockRegistry(),
        evaluator=MockEvaluator(),
        llm=llm,
    )

    outcome = evolver.evolve_full(
        skill_name="weather_query",
        eval_set_for_iter="repair_set",
        max_candidates=1,
        budget=EvolveBudget(enable_reflection=True, shadow_mode=True),
        verbose=False,
    )

    assert len(outcome.patches_declined) == 1
    assert len(outcome.attempts) == 1
    attempt = outcome.attempts[0]
    assert attempt.verdict == "DECLINED"
    assert any("REPEATED_FINGERPRINT" in r for r in attempt.reason_codes)
    assert outcome.rounds_executed == 1


# =========================================================================
# 3. 反例 6.(a): 同 case 两轮失败后不再反思（rounds 耗尽停止）
# =========================================================================

def test_two_rounds_failure_stops_cleanly(tmp_path: Path):
    """反例 6.(a): 同 case 在 Round 1 与 Round 2 均失败后，rounds 耗尽停止，不进入死循环。"""
    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    eval_dir = repo_root / "evaluation_sets"
    eval_dir.mkdir(parents=True)
    (eval_dir / "repair_set.json").write_text(
        json.dumps({"cases": [{"id": "r1", "query": "查询北京天气"}]}),
        encoding="utf-8",
    )
    skills_dir = repo_root / "skills" / "weather_query"
    skills_dir.mkdir(parents=True)
    baseline_md = "---\nname: weather_query\nversion: 1.0.0\ndescription: 查询天气\nuse_when: 查天气\nnot_for: []\ndependencies: []\ntrigger:\n  keywords: [天气]\nexamples: []\n---\n\n## Instructions\n查天气"
    (skills_dir / "SKILL.md").write_text(baseline_md, encoding="utf-8")

    class MockRegistry:
        def __init__(self):
            self.repo_root = repo_root
            self.skills_dir = repo_root / "skills"
            self._bodies = {"weather_query": "## Instructions\n查天气"}
        def get_meta(self, name):
            return _make_skill_meta(name)

    base_eval = EvalResult(
        release_id="base_0",
        structure_score={"schema": 10.0},
        effect_score={"task": 10.0},
        objective_metrics={},
        p0_pass=True,
        case_verdicts=[{"case_id": "r1", "task_completion": "B_better"}],
        case_outputs=[{"case_id": "r1", "query": "查询北京天气", "reference": "晴", "output_skill": "雨", "output_baseline": "晴"}],
        valid=True,
    )

    declined_eval = EvalResult(
        release_id="cand_eval",
        structure_score={"schema": 10.0},
        effect_score={"task": 5.0},
        objective_metrics={},
        p0_pass=True,
        case_verdicts=[{"case_id": "r1", "task_completion": "B_better"}],
        case_outputs=[{"case_id": "r1", "query": "查询北京天气", "reference": "晴", "output_skill": "雨", "output_baseline": "晴"}],
        valid=True,
    )

    class MockEvaluator:
        def __init__(self):
            self.llm = None
        def evaluate_skill(self, *args, **kwargs):
            return base_eval

    patch1_md = baseline_md.replace("version: 1.0.0", "version: 1.0.1").replace("description: 查询天气", "description: 查询天气方案一")
    patch2_md = baseline_md.replace("version: 1.0.0", "version: 1.0.1").replace("description: 查询天气", "description: 查询天气方案二反思")

    llm = CapturingFakeLLM([
        json.dumps({"prompt_vague": {"prob": 0.8, "why": "提示词不明确"}}),
        json.dumps([{"level": "L1", "new_skill_md": patch1_md, "rationale": "Round 1 尝试"}]),
        json.dumps([{"level": "L1", "new_skill_md": patch2_md, "rationale": "Round 2 反思尝试"}]),
    ])

    evolver = SkillEvolver(
        registry=MockRegistry(),
        evaluator=MockEvaluator(),
        llm=llm,
    )

    from unittest.mock import patch as mock_patch
    with mock_patch("skillforge.evolver._validate_patch") as mocked_validate:
        mocked_validate.return_value = (
            declined_eval,
            RatchetVerdict(decision="DECLINED", reasons=["总分退步"]),
        )
        outcome = evolver.evolve_full(
            skill_name="weather_query",
            eval_set_for_iter="repair_set",
            max_candidates=1,
            budget=EvolveBudget(max_rounds=2, enable_reflection=True, shadow_mode=True),
            verbose=False,
        )

    assert outcome.rounds_executed == 2
    assert outcome.context.stop_reason == "ROUNDS_EXHAUSTED"
    assert len(outcome.attempts) == 2
    assert outcome.attempts[0].round_no == 1
    assert outcome.attempts[1].round_no == 2
    assert outcome.attempts[0].verdict == "DECLINED"
    assert outcome.attempts[1].verdict == "DECLINED"
    assert len(outcome.patches_declined) == 2


# =========================================================================
# 4. 反例 6.(c): Shadow 归档含 attempt_no / reason_codes / shadow_mode
# =========================================================================

def test_shadow_archive_contents(tmp_path: Path):
    """反例 6.(c): 验证 shadow 归档在真实文件系统落盘，并完整包含 attempt_no / round_no / reason_codes / candidate_digest / shadow_mode。"""
    repo_root = tmp_path / "repo"
    patch = Patch(
        skill_name="weather_query",
        level="L1",
        diff="---\nname: weather_query\n---\n\nbody",
        rationale="优化关键词",
        computed_level="L1",
        unified_diff="--- old\n+++ new",
    )
    verdict = RatchetVerdict(decision="PASS", reasons=["指标达标"])
    new_result = EvalResult(
        release_id="r1",
        structure_score={"schema": 15.0},
        effect_score={"task": 80.0},
        objective_metrics={},
        p0_pass=True,
    )

    sug_path = _archive_suggestion(
        repo_root=repo_root,
        skill_name="weather_query",
        patch=patch,
        verdict=verdict,
        new_result=new_result,
        attempt_no=1,
        round_no=1,
        reason_codes=["METRIC_PASS"],
        shadow_mode=True,
        candidate_digest="digest_sha256_1234",
    )

    assert sug_path.exists()
    sug_text = sug_path.read_text(encoding="utf-8")
    assert "[SHADOW / PASS / L1] weather_query" in sug_text
    assert "- attempt_no: 1" in sug_text
    assert "- round_no: 1" in sug_text
    assert "- candidate_digest: digest_sha256_1234" in sug_text
    assert "- shadow_mode: true" in sug_text
    assert "- reason_codes:" in sug_text
    assert "METRIC_PASS" in sug_text

    fail_verdict = RatchetVerdict(decision="DECLINED", reasons=["PROMPT_BLOAT_DETECTED", "TASK_REGRESSION"])
    fail_path = _archive_failure(
        repo_root=repo_root,
        skill_name="weather_query",
        patch=patch,
        verdict=fail_verdict,
        new_result=new_result,
        error=None,
        attempt_no=2,
        round_no=2,
        reason_codes=["PROMPT_BLOAT_DETECTED", "TASK_REGRESSION"],
        shadow_mode=True,
        candidate_digest="digest_sha256_5678",
    )

    assert fail_path.exists()
    fail_text = fail_path.read_text(encoding="utf-8")
    assert "[DECLINED / PROMPT_BLOAT / L1] weather_query" in fail_text
    assert "- attempt_no: 2" in fail_text
    assert "- round_no: 2" in fail_text
    assert "- candidate_digest: digest_sha256_5678" in fail_text
    assert "- shadow_mode: true" in fail_text
    assert "- reason_codes:" in fail_text
    assert "PROMPT_BLOAT_DETECTED" in fail_text
    assert "TASK_REGRESSION" in fail_text


# =========================================================================
# 5. 反例 6.(d): 泄漏防护与数据隔离
# =========================================================================

def test_reflection_isolation_filters_holdout_and_audit():
    """反例 6.(d)-1: 评测数据中若混入 holdout/audit case，构造反思输入时自动过滤。"""
    base_result = EvalResult(
        release_id="base",
        structure_score={},
        effect_score={},
        objective_metrics={},
        p0_pass=True,
        case_verdicts=[
            {"case_id": "repair_c01", "task_completion": "A_better"},
            {"case_id": "holdout_h01", "task_completion": "A_better"},
        ],
        case_outputs=[
            {"case_id": "repair_c01", "query": "公开repair用例", "reference": "ref1", "output_skill": "out1"},
            {"case_id": "holdout_h01", "query": "秘密holdout用例", "reference": "ref2", "output_skill": "out2"},
        ],
    )

    cand_result = EvalResult(
        release_id="cand",
        structure_score={},
        effect_score={},
        objective_metrics={},
        p0_pass=True,
        case_verdicts=[
            {"case_id": "repair_c01", "task_completion": "B_better"},
            {"case_id": "holdout_h01", "task_completion": "B_better"},
            {"case_id": "repair_inv02", "verdict": "INVALID", "reason_codes": ["INVALID"]},
        ],
        case_outputs=[
            {"case_id": "repair_c01", "query": "公开repair用例", "reference": "ref1", "output_skill": "bad1"},
            {"case_id": "holdout_h01", "query": "秘密holdout用例", "reference": "ref2", "output_skill": "bad2"},
            {"case_id": "repair_inv02", "query": "超时用例", "reference": "ref3", "output_skill": "bad3"},
        ],
    )

    patch = Patch(
        skill_name="s",
        level="L1",
        diff="diff",
        rationale="r",
        computed_level="L1",
        unified_diff="udiff",
    )

    feedback = _build_attempt_feedback(
        attempt_no=1,
        original_skill_md="original",
        candidate_patch=patch,
        baseline_result=base_result,
        candidate_result=cand_result,
        ratchet_verdict=RatchetVerdict(decision="DECLINED", reasons=["退步"]),
        repair_case_ids={"repair_c01", "repair_inv02"},
        holdout_case_ids={"holdout_h01"},
        audit_case_ids={"audit_a01"},
    )

    cids_in_feedback = [fc["case_id"] for fc in feedback.failed_cases]
    assert cids_in_feedback == ["repair_c01"]
    assert "holdout_h01" not in cids_in_feedback
    assert "repair_inv02" not in cids_in_feedback

    assert_reflection_isolation(
        feedback,
        holdout_ids={"holdout_h01"},
        audit_ids={"audit_a01"},
        holdout_queries={"秘密holdout用例"},
        audit_queries=set(),
    )


def test_assert_reflection_isolation_raises_on_breach():
    """反例 6.(d)-2: 若 AttemptFeedback 意外混入 holdout/audit 字段，断言函数立即阻断并抛出异常。"""
    leaked_feedback = AttemptFeedback(
        attempt_no=1,
        original_skill_digest="d1",
        candidate_digest="d2",
        declared_level="L1",
        computed_level="L1",
        strategy="routing_metadata",
        candidate_diff="diff",
        failed_cases=[
            {"case_id": "holdout_case_999", "query": "这是一条绝密评测query"},
        ],
        ratchet_reasons=[],
        p0_status=True,
        authenticity_status=True,
        prompt_budget_status="PASS",
        repeated_patch_fingerprints=[],
        remaining_budget={},
    )

    with pytest.raises(AssertionError, match="ISOLATION_BREACH.*holdout_case_999"):
        assert_reflection_isolation(
            leaked_feedback,
            holdout_ids={"holdout_case_999"},
            audit_ids=set(),
        )

    leaked_query_feedback = AttemptFeedback(
        attempt_no=1,
        original_skill_digest="d1",
        candidate_digest="d2",
        declared_level="L1",
        computed_level="L1",
        strategy="routing_metadata",
        candidate_diff="diff",
        failed_cases=[
            {"case_id": "repair_case_01", "query": "绝密holdout问题query"},
        ],
        ratchet_reasons=[],
        p0_status=True,
        authenticity_status=True,
        prompt_budget_status="PASS",
        repeated_patch_fingerprints=[],
        remaining_budget={},
    )

    with pytest.raises(AssertionError, match="ISOLATION_BREACH.*query"):
        assert_reflection_isolation(
            leaked_query_feedback,
            holdout_ids=set(),
            audit_ids=set(),
            holdout_queries={"绝密holdout问题query"},
        )


def test_scan_patch_leakage_blocks_leaked_content():
    """反例 6.(d)-3: scan_patch_leakage 扫描 patch 内容，拦截泄露的 holdout query / case ID / fixture 常量。"""
    leaked_patch = """---
name: weather_query
---
## Examples
- experiment_holdout_042: 请查询绝密测试集的气温与降水概率
"""
    has_leak, reasons = scan_patch_leakage(
        leaked_patch,
        forbidden_case_ids={"experiment_holdout_042"},
        forbidden_queries=["请查询绝密测试集的气温与降水概率"],
    )
    assert has_leak is True
    assert any("experiment_holdout_042" in r for r in reasons)
    assert any("请查询绝密测试集的气温与降水概率" in r for r in reasons)


# =========================================================================
# 6. 防自嗨防线 4 & 5 (Nonce Fixture & Neighbor Variants)
# =========================================================================

def test_nonce_weather_fixture():
    """验证 Nonce Fixture: AmapWeatherFixture 携带运行时 nonce 导致结果动态变化，防 mock 固化。"""
    fix1 = create_nonce_weather_fixture(nonce="nonce_run_1")
    fix2 = create_nonce_weather_fixture(nonce="nonce_run_2")

    res1 = fix1.run({"city": "北京市", "extensions": "all"})
    res2 = fix2.run({"city": "北京市", "extensions": "all"})

    assert res1.status.value == "success"
    assert res2.status.value == "success"
    data1 = res1.data
    data2 = res2.data

    assert data1["forecasts"][0]["casts"][0]["nonce"] == "nonce_run_1"
    assert data2["forecasts"][0]["casts"][0]["nonce"] == "nonce_run_2"

    patch_with_hardcode = "如果查询北京市，直接输出气温 28℃ nonce_run_1"
    violations = check_mock_hardcoding(patch_with_hardcode, ["nonce_run_1"])
    assert len(violations) == 1
    assert "MOCK_HARDCODING_DETECTED" in violations[0]


def test_neighbor_variants_generation():
    """验证邻近变体集生成（城市、日期等维度的变体，防过拟合单条 query）。"""
    q = "请查询北京市今天的天气"
    variants = generate_neighbor_variants(q, skill_name="weather_query")
    assert len(variants) >= 2
    assert any("今天" not in v or "北京市" not in v for v in variants)


# =========================================================================
# 7. Rounds 状态机：PASS/REVIEW 不重试追分
# =========================================================================

def test_rounds_state_machine_pass_no_retry(tmp_path: Path):
    """验证 Rounds 状态机：Round 1 产生 PASS 候选时立即停止，绝不重试追分。"""
    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    skills_dir = repo_root / "skills" / "weather_query"
    skills_dir.mkdir(parents=True)
    baseline_md = "---\nname: weather_query\nversion: 1.0.0\ndescription: 查询天气\nuse_when: 查天气\nnot_for: []\ndependencies: []\ntrigger:\n  keywords: [天气]\nexamples: []\n---\n\n## Instructions\n查天气"
    (skills_dir / "SKILL.md").write_text(baseline_md, encoding="utf-8")

    class MockRegistry:
        def __init__(self):
            self.repo_root = repo_root
            self.skills_dir = repo_root / "skills"
            self._bodies = {"weather_query": "## Instructions\n查天气"}
        def get_meta(self, name):
            return _make_skill_meta(name)

    base_eval = EvalResult(
        release_id="base_0",
        structure_score={"schema": 10.0},
        effect_score={"task": 10.0},
        objective_metrics={},
        p0_pass=True,
        case_verdicts=[{"case_id": "r1", "task_completion": "B_better"}],
        case_outputs=[{"case_id": "r1", "query": "查询北京天气", "reference": "晴", "output_skill": "雨", "output_baseline": "晴"}],
        valid=True,
    )

    pass_eval = EvalResult(
        release_id="cand_pass",
        structure_score={"schema": 10.0},
        effect_score={"task": 30.0},
        objective_metrics={},
        p0_pass=True,
        case_verdicts=[{"case_id": "r1", "task_completion": "A_better"}],
        case_outputs=[{"case_id": "r1", "query": "查询北京天气", "reference": "晴", "output_skill": "晴", "output_baseline": "晴"}],
        valid=True,
    )

    class MockEvaluator:
        def __init__(self):
            self.llm = None
        def evaluate_skill(self, *args, **kwargs):
            return base_eval

    patch1_md = baseline_md.replace("version: 1.0.0", "version: 1.0.1").replace("description: 查询天气", "description: 查询天气优化")
    llm = CapturingFakeLLM([
        json.dumps({"prompt_vague": {"prob": 0.8, "why": "提示词不明确"}}),
        json.dumps([{"level": "L1", "new_skill_md": patch1_md, "rationale": "Round 1 优化"}]),
    ])

    evolver = SkillEvolver(
        registry=MockRegistry(),
        evaluator=MockEvaluator(),
        llm=llm,
    )

    from unittest.mock import patch as mock_patch
    with mock_patch("skillforge.evolver._validate_patch") as mocked_validate:
        mocked_validate.return_value = (
            pass_eval,
            RatchetVerdict(decision="PASS", reasons=[]),
        )
        outcome = evolver.evolve_full(
            skill_name="weather_query",
            eval_set_for_iter="repair_set",
            max_candidates=1,
            budget=EvolveBudget(max_rounds=2, enable_reflection=True, shadow_mode=True),
            verbose=False,
        )

    assert outcome.rounds_executed == 1
    assert outcome.context.stop_reason == "ACCEPTABLE_CANDIDATE_FOUND"
    assert len(outcome.attempts) == 1
    assert outcome.attempts[0].verdict == "PASS"


# =========================================================================
# 7. Codex 审计缺陷闭环专项反例与边界测试 (P0-1 ~ P2-2)
# =========================================================================


def test_p0_1_forbidden_eval_set_fails_closed(tmp_path: Path):
    """【P0-1 数据边界】验证 eval_set_for_iter 指向 holdout/final_audit 时硬拒绝并归档诊断。"""
    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    skills_dir = repo_root / "skills" / "weather_query"
    skills_dir.mkdir(parents=True)
    baseline_md = "---\nname: weather_query\nversion: 1.0.0\ndescription: 查询天气\nuse_when: 查天气\nnot_for: []\ndependencies: []\ntrigger:\n  keywords: [天气]\nexamples: []\n---\n\n## Instructions\n查天气"
    (skills_dir / "SKILL.md").write_text(baseline_md, encoding="utf-8")

    class MockRegistry:
        def __init__(self):
            self.repo_root = repo_root
            self.skills_dir = repo_root / "skills"
            self._bodies = {"weather_query": "## Instructions\n查天气"}
        def get_meta(self, name):
            return _make_skill_meta(name)

    class EvaluatorMustNotRun:
        def evaluate_skill(self, *args, **kwargs):
            raise AssertionError("evaluator must not run on forbidden eval sets!")

    llm = CapturingFakeLLM([])
    evolver = SkillEvolver(
        registry=MockRegistry(),
        evaluator=EvaluatorMustNotRun(),
        llm=llm,
    )

    # 1. 尝试传入 experiment_holdout
    outcome_holdout = evolver.evolve_full(
        skill_name="weather_query",
        eval_set_for_iter="experiment_holdout",
        verbose=False,
    )
    assert outcome_holdout.error is not None
    assert "FORBIDDEN_EVAL_SET" in outcome_holdout.error
    assert len(outcome_holdout.patches_review) == 1
    diag_file = Path(outcome_holdout.patches_review[0])
    assert diag_file.exists()
    diag_content = diag_file.read_text(encoding="utf-8")
    assert "experiment_holdout" in diag_content
    assert "REVIEW" in diag_content

    # 2. 尝试传入 final_audit
    outcome_audit = evolver.evolve_full(
        skill_name="weather_query",
        eval_set_for_iter="final_audit",
        verbose=False,
    )
    assert outcome_audit.error is not None
    assert "FORBIDDEN_EVAL_SET" in outcome_audit.error

    # 3. 底层断言函数验证
    assert is_forbidden_eval_set("experiment_holdout") is True
    assert is_forbidden_eval_set("final_audit") is True
    assert is_forbidden_eval_set("repair_set") is False
    with pytest.raises(ValueError, match="FORBIDDEN_EVAL_SET"):
        assert_eval_set_for_iter("experiment_holdout")


def test_p0_1_reflection_isolation_deep_check():
    """【P0-1 数据边界】验证 assert_reflection_isolation 深度检查 reference, output, diff, reasons。"""
    # 正常隔离 feedback
    clean_fb = AttemptFeedback(
        attempt_no=1,
        original_skill_digest="dig1",
        candidate_digest="dig2",
        declared_level="L1",
        computed_level="L1",
        strategy="routing_metadata",
        candidate_diff="--- old\n+++ new\n+正常优化",
        failed_cases=[{"case_id": "repair_01", "query": "北京天气", "reference": "北京晴天"}],
        ratchet_reasons=["未达到目标分数"],
        p0_status=True,
        authenticity_status=True,
        prompt_budget_status="PASS",
        repeated_patch_fingerprints=[],
        remaining_budget={},
    )
    assert_reflection_isolation(
        clean_fb,
        holdout_ids={"holdout_01"},
        audit_ids=set(),
        holdout_queries={"绝密测试query"},
        holdout_references={"绝密reference"},
    )

    # 1. 深度检查：reference 泄露进 failed_cases
    leak_ref_fb = AttemptFeedback(
        attempt_no=1,
        original_skill_digest="dig1",
        candidate_digest="dig2",
        declared_level="L1",
        computed_level="L1",
        strategy="routing_metadata",
        candidate_diff="--- old\n+++ new",
        failed_cases=[{"case_id": "repair_01", "query": "北京天气", "reference": "包含 绝密reference 内容"}],
        ratchet_reasons=["未达到目标分数"],
        p0_status=True,
        authenticity_status=True,
        prompt_budget_status="PASS",
        repeated_patch_fingerprints=[],
        remaining_budget={},
    )
    with pytest.raises(AssertionError, match="ISOLATION_BREACH"):
        assert_reflection_isolation(
            leak_ref_fb,
            holdout_ids={"holdout_01"},
            audit_ids=set(),
            holdout_queries={"绝密测试query"},
            holdout_references={"绝密reference"},
        )

    # 2. 深度检查：holdout query 泄露进 candidate_diff
    leak_diff_fb = AttemptFeedback(
        attempt_no=1,
        original_skill_digest="dig1",
        candidate_digest="dig2",
        declared_level="L1",
        computed_level="L1",
        strategy="routing_metadata",
        candidate_diff="--- old\n+++ new\n+泄露了 绝密测试query",
        failed_cases=[{"case_id": "repair_01", "query": "北京天气"}],
        ratchet_reasons=["未达到目标分数"],
        p0_status=True,
        authenticity_status=True,
        prompt_budget_status="PASS",
        repeated_patch_fingerprints=[],
        remaining_budget={},
    )
    with pytest.raises(AssertionError, match="ISOLATION_BREACH"):
        assert_reflection_isolation(
            leak_diff_fb,
            holdout_ids={"holdout_01"},
            audit_ids=set(),
            holdout_queries={"绝密测试query"},
            holdout_references={"绝密reference"},
        )

    # 3. 深度检查：holdout ID 泄露进 ratchet_reasons
    leak_reason_fb = AttemptFeedback(
        attempt_no=1,
        original_skill_digest="dig1",
        candidate_digest="dig2",
        declared_level="L1",
        computed_level="L1",
        strategy="routing_metadata",
        candidate_diff="--- old\n+++ new",
        failed_cases=[{"case_id": "repair_01", "query": "北京天气"}],
        ratchet_reasons=["在用例 holdout_01 上发生退步"],
        p0_status=True,
        authenticity_status=True,
        prompt_budget_status="PASS",
        repeated_patch_fingerprints=[],
        remaining_budget={},
    )
    with pytest.raises(AssertionError, match="ISOLATION_BREACH"):
        assert_reflection_isolation(
            leak_reason_fb,
            holdout_ids={"holdout_01"},
            audit_ids=set(),
            holdout_queries={"绝密测试query"},
            holdout_references={"绝密reference"},
        )


def test_p0_2_infrastructure_invalid_fails_closed():
    """【P0-2 基础设施 fail-closed】验证候选 INVALID/TIMEOUT/FIXTURE_ERROR 在反思入口硬停机。"""
    # 1. 维度 INVALID
    dim_invalid_eval = EvalResult(
        release_id="c_inv",
        structure_score={},
        effect_score={},
        objective_metrics={},
        p0_pass=True,
        valid=False,
        invalid_reasons=["评测维度 INVALID"],
        case_verdicts=[{"case_id": "r1", "task_completion": "INVALID"}],
    )
    is_inv, reason = is_candidate_eval_invalid(dim_invalid_eval)
    assert is_inv is True
    assert "INVALID" in reason

    # 2. 工具 TIMEOUT
    timeout_eval = EvalResult(
        release_id="c_time",
        structure_score={},
        effect_score={},
        objective_metrics={},
        p0_pass=True,
        valid=True,
        case_verdicts=[{"case_id": "r1", "task_completion": "TIMEOUT"}],
    )
    is_inv, reason = is_candidate_eval_invalid(timeout_eval)
    assert is_inv is True
    assert "TIMEOUT" in reason

    # 3. 依赖 FIXTURE_ERROR
    fix_err_eval = EvalResult(
        release_id="c_fix",
        structure_score={},
        effect_score={},
        objective_metrics={},
        p0_pass=True,
        valid=True,
        case_verdicts=[{"case_id": "r1", "safety_boundaries": "FIXTURE_ERROR"}],
    )
    is_inv, reason = is_candidate_eval_invalid(fix_err_eval)
    assert is_inv is True
    assert "FIXTURE_ERROR" in reason

    # 4. judge_audit 异常
    audit_err_eval = EvalResult(
        release_id="c_audit",
        structure_score={},
        effect_score={},
        objective_metrics={},
        p0_pass=True,
        valid=True,
        case_verdicts=[{
            "case_id": "r1",
            "judge_audit": {
                "task_completion": {
                    "canonical_verdict": "INVALID",
                    "reason_codes": ["INFRASTRUCTURE_ERROR: 503 service unavailable"],
                }
            },
        }],
    )
    is_inv, reason = is_candidate_eval_invalid(audit_err_eval)
    assert is_inv is True
    assert "INVALID" in reason


def test_p0_2_candidate_invalid_stops_reflection_in_evolve_full(tmp_path: Path):
    """【P0-2 基础设施 fail-closed】在 evolve_full 中，当首轮候选评估为 INVALID 时，绝对不触发第二轮反思。"""
    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    skills_dir = repo_root / "skills" / "weather_query"
    skills_dir.mkdir(parents=True)
    baseline_md = "---\nname: weather_query\nversion: 1.0.0\ndescription: 查询天气\nuse_when: 查天气\nnot_for: []\ndependencies: []\ntrigger:\n  keywords: [天气]\nexamples: []\n---\n\n## Instructions\n查天气"
    (skills_dir / "SKILL.md").write_text(baseline_md, encoding="utf-8")

    class MockRegistry:
        def __init__(self):
            self.repo_root = repo_root
            self.skills_dir = repo_root / "skills"
            self._bodies = {"weather_query": "## Instructions\n查天气"}
        def get_meta(self, name):
            return _make_skill_meta(name)

    base_eval = EvalResult(
        release_id="base_0",
        structure_score={"schema": 10.0},
        effect_score={"task": 10.0},
        objective_metrics={},
        p0_pass=True,
        case_verdicts=[{"case_id": "r1", "task_completion": "B_better"}],
        case_outputs=[{"case_id": "r1", "query": "查询北京天气", "reference": "晴", "output_skill": "雨", "output_baseline": "晴"}],
        valid=True,
    )

    invalid_cand_eval = EvalResult(
        release_id="cand_inv",
        structure_score={"schema": 10.0},
        effect_score={"task": 10.0},
        objective_metrics={},
        p0_pass=True,
        valid=False,
        invalid_reasons=["Judge execution TIMEOUT"],
        case_verdicts=[{"case_id": "r1", "task_completion": "TIMEOUT"}],
    )

    class MockEvaluator:
        def __init__(self):
            self.llm = None
        def evaluate_skill(self, *args, **kwargs):
            return base_eval

    patch1_md = baseline_md.replace("version: 1.0.0", "version: 1.0.1").replace("description: 查询天气", "description: 尝试修复")
    # 只提供 Round 1 的 mock 响应，如果进入 Round 2 反思生成就会报错或取空
    llm = CapturingFakeLLM([
        json.dumps({"prompt_vague": {"prob": 0.8, "why": "提示词不明确"}}),
        json.dumps([{"level": "L1", "new_skill_md": patch1_md, "rationale": "Round 1 尝试"}]),
    ])

    evolver = SkillEvolver(
        registry=MockRegistry(),
        evaluator=MockEvaluator(),
        llm=llm,
    )

    from unittest.mock import patch as mock_patch
    with mock_patch("skillforge.evolver._validate_patch") as mocked_validate:
        mocked_validate.return_value = (
            invalid_cand_eval,
            RatchetVerdict(decision="DECLINED", reasons=["CANDIDATE_EVAL_INVALID: TIMEOUT"]),
        )
        outcome = evolver.evolve_full(
            skill_name="weather_query",
            eval_set_for_iter="repair_set",
            max_candidates=1,
            budget=EvolveBudget(max_rounds=2, enable_reflection=True, shadow_mode=True),
            verbose=False,
        )

    # 验证反思入口 fail-closed：停在 Round 1，不发起第二轮反思生成
    assert outcome.rounds_executed == 1
    assert outcome.context.stop_reason == "CANDIDATE_INVALID_FAIL_CLOSED"
    assert len(outcome.attempts) == 1
    assert outcome.attempts[0].verdict == "DECLINED"
    assert len(llm.invocations) == 2


def test_p1_1_nonce_fixture_and_mock_hardcoding_in_real_chain():
    """【P1-1 防线真接】验证 Nonce Fixture 运行时变化与 mock-hardcoding 接入真实验证链。"""
    # 1. 验证工厂调用产生具有动态 nonce 的 fixture
    fix_factory = _FIXTURE_FACTORIES["amap_weather_api"]
    f1 = fix_factory()
    f2 = fix_factory()
    assert f1.nonce is not None
    assert f2.nonce is not None
    assert f1.nonce != f2.nonce

    # 2. 验证 check_mock_hardcoding 能识别出 hardcode 了 mock 值的 patch
    hardcoded_patch_body = f"如果查询北京天气，直接固定返回 {f1.nonce} 和 25°C 晴天"
    reasons = check_mock_hardcoding(hardcoded_patch_body, runtime_fixture=f1)
    assert len(reasons) > 0
    assert any("MOCK_HARDCODING_DETECTED" in r for r in reasons)

    # 3. 验证 validate_dependency_patch 真实接线：存在 mock hardcoding 时判定不通过
    from skillforge.evaluator.validators import validate_dependency_patch
    class FakeReg:
        def __init__(self, body):
            self.skills_dir = None
            self._bodies = {"weather_query": body}
            self.repo_root = Path(".")
        def get_meta(self, name):
            m = _make_skill_meta(name)
            m.dependencies = ["amap_weather_api"]
            return m

    old_reg = FakeReg("正常基线")
    new_reg = FakeReg(hardcoded_patch_body)
    class FakeLLMWithTool:
        def invoke(self, *a, **k):
            return SimpleNamespace(content='{"name": "amap_weather_api", "arguments": {"city": "北京市", "extensions": "all"}}')

    verdict, provenances = validate_dependency_patch(old_reg, new_reg, "weather_query", llm=FakeLLMWithTool())
    assert verdict.decision == "DECLINED"
    assert any("MOCK_HARDCODING_DETECTED" in r for r in verdict.reasons)


def test_p1_1_scan_patch_leakage_real_holdout_references_and_fixtures():
    """【P1-1 防线真接】验证 scan_patch_leakage 真实加载 holdout references 与 fixture constants 并阻断。"""
    # 1. 泄露真实 experiment_holdout 的 reference 关键字
    patch_with_holdout_ref = """---
name: explain_regex
---
## Examples
参考以下解析规则：
定义 + 小例子对比（如 <.*> vs <.*?>）+ 陷阱提示
"""
    has_leak, reasons = scan_patch_leakage(patch_with_holdout_ref, repo_root=Path("."))
    assert has_leak is True
    assert any("reference" in r.lower() or "引用" in r for r in reasons)

    # 2. 泄露 fixture 内部常量
    patch_with_fixture_const = """---
name: weather_query
---
## Instructions
在执行时调用 create_nonce_weather_fixture 来模拟响应
"""
    has_leak2, reasons2 = scan_patch_leakage(patch_with_fixture_const, repo_root=Path("."))
    assert has_leak2 is True
    assert any("fixture 常量" in r for r in reasons2)


def test_p1_1_neighbor_variants_in_validation_chain():
    """【P1-1 防线真接】验证 validate_neighbor_variants 对失败 query 生成变体并执行路由检验。"""
    class MockRouterSuccess:
        def route(self, q):
            return SimpleNamespace(chosen="weather_query")

    class MockRouterFail:
        def route(self, q):
            return SimpleNamespace(chosen="other_skill")

    # 路由成功场景
    passed, reasons = validate_neighbor_variants(
        registry=None,
        skill_name="weather_query",
        failed_queries=["北京今天天气怎么样"],
        router=MockRouterSuccess(),
    )
    assert passed is True
    assert len(reasons) == 0

    # 路由失败场景（变体未命中本技能）
    passed_fail, reasons_fail = validate_neighbor_variants(
        registry=None,
        skill_name="weather_query",
        failed_queries=["北京今天天气怎么样"],
        router=MockRouterFail(),
    )
    assert passed_fail is False
    assert any("NEIGHBOR_VARIANT_MISMATCH" in r for r in reasons_fail)


def test_p1_1_neighbor_variants_uses_production_case_verdict_shape(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """真实 EvalResult 只有维度判定时，_validate_patch 仍必须触发邻近变体链。"""
    from skillforge.evolver import _reconstruct_skill_md

    skills_dir = tmp_path / "skills" / "weather_query"
    skills_dir.mkdir(parents=True)
    meta = _make_skill_meta("weather_query")
    body = "## Instructions\n调用天气工具并如实回答"
    skill_md = _reconstruct_skill_md(meta, body)
    (skills_dir / "SKILL.md").write_text(skill_md, encoding="utf-8")

    registry = SimpleNamespace(
        repo_root=tmp_path,
        skills_dir=tmp_path / "skills",
        _bodies={"weather_query": body},
    )
    old_result = EvalResult(
        release_id="base",
        structure_score={"schema": 10.0},
        effect_score={"task": 10.0},
        objective_metrics={},
        p0_pass=True,
        valid=True,
        case_verdicts=[
            {
                "case_id": "repair_01",
                "query": "北京今天天气怎么样",
                "task_completion": "B_better",
                "robustness": "tied",
                "readability": "tied",
            }
        ],
    )
    patch = Patch(
        skill_name="weather_query",
        level="L1",
        computed_level="L1",
        diff=skill_md,
        rationale="metadata check",
        changed_frontmatter=[],
        changed_body_sections=[],
    )
    seen: list[str] = []

    def fake_neighbor(_registry, _skill_name, failed_queries, router=None):
        seen.extend(failed_queries)
        return False, ["NEIGHBOR_VARIANT_MISMATCH: test"]

    monkeypatch.setattr("skillforge.evolver.validate_neighbor_variants", fake_neighbor)
    result, verdict = _validate_patch(
        SimpleNamespace(router=SimpleNamespace()),
        registry,
        "weather_query",
        patch,
        old_result,
        "repair_set",
    )

    assert seen == ["北京今天天气怎么样"]
    assert result.validation_channels == ["neighbor_variants"]
    assert verdict.decision == "REVIEW"


def test_p1_2_final_audit_gate_and_publish_flag(tmp_path: Path):
    """【P1-2 final_audit 门】验证独立终审门与 auto-publish flag 约束力。"""
    patch = Patch(
        skill_name="weather_query",
        level="L1",
        computed_level="L1",
        diff="---\nname: weather_query\nversion: 1.0.1\n---\n## Instructions\n查天气",
        rationale="add examples",
        downgrade_attempt=False,
    )
    verdict = RatchetVerdict(decision="PASS", reasons=[])
    eval_result = EvalResult(
        release_id="rel_01",
        structure_score={"s": 20.0},
        effect_score={"e": 30.0},
        objective_metrics={},
        p0_pass=True,
    )

    class MockSM:
        def __init__(self):
            self.published = False
        def begin_release(self, *a): return "rel_1"
        def write_commit(self, *a): pass
        def append_evaluation(self, *a): pass
        def commit_release(self, *a): self.published = True

    # 1. 验证 Codex 反例：shadow_mode=False, auto_publish_enabled=False, round_no=1
    sm = MockSM()
    outc = _publish_patch(
        repo_root=tmp_path,
        registry=None,
        state_machine=sm,
        skill_name="weather_query",
        patch=patch,
        verdict=verdict,
        new_result=eval_result,
        shadow_mode=False,
        auto_publish_enabled=False,
        round_no=1,
    )
    assert outc["status"] != "PUBLISHED"
    assert sm.published is False

    # 2. 验证 shadow_mode 下运行 final_audit 门并在归档记录门结果
    sm2 = MockSM()
    outc_shadow = _publish_patch(
        repo_root=tmp_path,
        registry=None,
        state_machine=sm2,
        skill_name="weather_query",
        patch=patch,
        verdict=verdict,
        new_result=eval_result,
        shadow_mode=True,
        round_no=1,
    )
    assert outc_shadow["status"] == "SHADOW"
    archive_path = Path(outc_shadow["path"])
    assert archive_path.exists()
    content = archive_path.read_text(encoding="utf-8")
    assert "final_audit_gate_passed" in content
    assert "shadow_mode: true" in content

    # 3. 验证 auto_publish_enabled=True 时，final_audit 门失败硬阻断发布
    class FailingEvaluator:
        def evaluate_skill(self, *args, **kwargs):
            return EvalResult(
                release_id="audit_fail",
                structure_score={},
                effect_score={},
                objective_metrics={},
                p0_pass=False,  # P0 失败
                valid=True,
            )

    (tmp_path / "evaluation_sets").mkdir(parents=True, exist_ok=True)
    (tmp_path / "evaluation_sets" / "final_audit.json").write_text(
        json.dumps({"cases": [{"id": "audit_1", "skill": "weather_query", "query": "q", "reference": "r"}]}),
        encoding="utf-8"
    )

    sm3 = MockSM()
    outc_blocked = _publish_patch(
        repo_root=tmp_path,
        registry=None,
        state_machine=sm3,
        skill_name="weather_query",
        patch=patch,
        verdict=verdict,
        new_result=eval_result,
        shadow_mode=False,
        auto_publish_enabled=True,
        round_no=1,
        evaluator=FailingEvaluator(),
    )
    assert outc_blocked["status"] == "DECLINED"
    assert sm3.published is False
    archive_text = Path(outc_blocked["path"]).read_text(encoding="utf-8")
    assert "FINAL_AUDIT_GATE_FAILED" in archive_text
    assert "final_audit_gate_passed: false" in archive_text


def test_p1_2_final_audit_missing_or_sandbox_failure_is_fail_closed(tmp_path: Path):
    """缺 final_audit 或候选沙箱失败时，auto-publish 均不得放行。"""
    patch = Patch(
        skill_name="weather_query",
        level="L1",
        computed_level="L1",
        diff="---\nname: weather_query\nversion: 1.0.1\n---\n## Instructions\n查天气",
        rationale="audit gate",
    )
    result = EvalResult(
        release_id="candidate",
        structure_score={"s": 20.0},
        effect_score={"e": 30.0},
        objective_metrics={},
        p0_pass=True,
        valid=True,
    )
    class PassingEvaluator:
        def evaluate_skill(self, *args, **kwargs):
            return result

    class MockSM:
        def commit_release(self, *args):
            raise AssertionError("must not publish")

    missing = _publish_patch(
        repo_root=tmp_path,
        registry=SimpleNamespace(skills_dir=tmp_path / "missing-skills"),
        state_machine=MockSM(),
        skill_name="weather_query",
        patch=patch,
        verdict=RatchetVerdict(decision="PASS"),
        new_result=result,
        shadow_mode=False,
        auto_publish_enabled=True,
        evaluator=PassingEvaluator(),
    )
    assert missing["status"] == "DECLINED"
    assert "FINAL_AUDIT_GATE_FAILED" in Path(missing["path"]).read_text(encoding="utf-8")

    audit_dir = tmp_path / "evaluation_sets"
    audit_dir.mkdir()
    (audit_dir / "final_audit.json").write_text(json.dumps({"cases": []}), encoding="utf-8")
    sandbox_failed = _publish_patch(
        repo_root=tmp_path,
        registry=SimpleNamespace(skills_dir=tmp_path / "missing-skills"),
        state_machine=MockSM(),
        skill_name="weather_query",
        patch=patch,
        verdict=RatchetVerdict(decision="PASS"),
        new_result=result,
        shadow_mode=False,
        auto_publish_enabled=True,
        evaluator=PassingEvaluator(),
    )
    assert sandbox_failed["status"] == "DECLINED"


def test_p2_1_load_dataset_layer_ids_fails_closed_on_corrupt_json(tmp_path: Path):
    """【P2-1 数据加载 fail-closed】验证 JSON 损坏时显式抛出 DATASET_PARSE_ERROR，绝不静默吞错变空集。"""
    eval_dir = tmp_path / "evaluation_sets"
    eval_dir.mkdir()
    corrupt_file = eval_dir / "repair_set.json"
    corrupt_file.write_text("{ unclosed json: ", encoding="utf-8")

    with pytest.raises(ValueError, match="DATASET_PARSE_ERROR"):
        _load_dataset_layer_ids(tmp_path)


def test_p2_2_attempt_record_calls_tokens_and_archive_uniqueness(tmp_path: Path):
    """【P2-2】验证 AttemptRecord.calls/tokens 接入真实 ledger，且同秒归档命名唯一不覆盖。"""
    # 1. AttemptRecord 默认值为 None（预留字段，不使用默认 0 假装真值）
    rec = AttemptRecord(
        attempt_no=1,
        strategy="strat",
        candidate_digest="c1",
        computed_level="L1",
        verdict="DECLINED",
    )
    assert rec.calls is None
    assert rec.tokens is None

    # 2. 连续归档同秒 attempt 1 和 attempt 2，断言生成独立文件不覆盖
    patch = Patch(
        skill_name="weather_query",
        level="L1",
        computed_level="L1",
        diff="diff",
        rationale="r",
        downgrade_attempt=False,
    )
    p1 = _archive_failure(
        repo_root=tmp_path,
        skill_name="weather_query",
        patch=patch,
        verdict=RatchetVerdict(decision="DECLINED"),
        new_result=None,
        error=None,
        attempt_no=1,
        candidate_digest="cand1111",
    )
    p2 = _archive_failure(
        repo_root=tmp_path,
        skill_name="weather_query",
        patch=patch,
        verdict=RatchetVerdict(decision="DECLINED"),
        new_result=None,
        error=None,
        attempt_no=2,
        candidate_digest="cand2222",
    )
    assert p1 != p2
    assert p1.exists()
    assert p2.exists()
    assert "att1" in p1.name
    assert "att2" in p2.name
    content1 = p1.read_text(encoding="utf-8")
    content2 = p2.read_text(encoding="utf-8")
    assert "attempt_no: 1" in content1
    assert "attempt_no: 2" in content2

    # 3. ledger 注入时记录本次 attempt 增量，而非全局累计值
    evolver = object.__new__(SkillEvolver)
    evolver.ledger = SimpleNamespace(total_calls=2, total_tokens=30)
    ctx = EvolveContext(skill_name="weather_query")
    first = evolver._create_attempt_record(
        ctx, "strat", "c1", "L1", "DECLINED", [], 1
    )
    evolver.ledger.total_calls = 5
    evolver.ledger.total_tokens = 80
    second = evolver._create_attempt_record(
        ctx, "strat", "c2", "L1", "DECLINED", [], 1
    )
    assert (first.calls, first.tokens) == (2, 30)
    assert (second.calls, second.tokens) == (3, 50)
    assert (ctx.calls_used, ctx.tokens_used) == (5, 80)
