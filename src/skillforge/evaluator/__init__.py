"""八维评估器装配：结构分 40 + 效果分 60 + 客观指标 + P0 门槛

flow（evaluate_skill）：
    1. 结构分：SkillMeta 静态检查（不阻断，只出警告）
    2. 效果分：对每个 case 跑「无 Skill vs 有 Skill」两版本 → Judge 配对
       → 任务完成度 / 鲁棒性 / 可读性 3 维配对；效率维用客观 token 比
    3. 客观指标：turns / tokens / latency 平均
    4. P0：P0 case 中 task_completion 维度 B_better（对照更好）视为 P0 fail

参见 ARCHITECTURE §4-E、§7
"""
from __future__ import annotations
import hashlib
import time
import re
from pathlib import Path
from statistics import mean
from typing import Any, Optional, Literal

from hello_agents.tools import Tool, ToolParameter

from ..models import EvalResult, RatchetVerdict, RouteResult, ToolCallProvenance
from .structure import score_structure, structure_total
from .judge import PairwiseJudge, invert_verdict, skill_is_presented_as_a
from .criteria import (
    CRITERIA_POLICY_VERSION,
    CRITERIA_PROMPT_VERSION,
    DEFAULT_RUBRIC_V1,
    RuleDefinition,
    RuleFinding,
    compute_case_scores,
    derive_pairwise_verdict,
    evaluate_semantic_criteria,
    validate_rubric,
)
from .metrics import collect_objective_metrics
from .ratchet import check_ratchet as _check_ratchet
from .p0_gate import (
    P0GateError,
    P0LoadError,
    P0EmptyCasesError,
    P0GateResult,
    load_p0_cases,
    evaluate_p0_gate,
    check_p0_ratchet_verdict,
    merge_p0_into_eval_result,
)
from .prompt_bloat import (
    PromptBloatResult,
    check_prompt_bloat,
    compute_body_section_stats,
    canonical_section_name,
)
from .root_cause_prompts import (
    PromptFragment,
    ROUTING_METADATA,
    ROUTING_METADATA_SCHEMA,
    EXECUTION_BEHAVIOR,
    EXECUTION_BEHAVIOR_SCHEMA,
    format_routing_metadata,
    format_execution_behavior,
    extract_relevant_body_sections,
    format_body_sections,
    validate_payload_against_schema,
)


class EvaluatorOutputCache:
    """Cache for bare and current outputs to eliminate baseline drift across evaluations."""

    def __init__(self) -> None:
        self._cache: dict[str, tuple[Any, ...]] = {}
        self.hits: int = 0
        self.misses: int = 0

    def get_bare(self, query: str, config_fingerprint: str) -> Optional[tuple[str, dict]]:
        key = self._make_bare_key(query, config_fingerprint)
        val = self._cache.get(key)
        if val is not None:
            self.hits += 1
            return val
        self.misses += 1
        return None

    def set_bare(self, query: str, config_fingerprint: str, output: str, metrics: dict) -> None:
        key = self._make_bare_key(query, config_fingerprint)
        self._cache[key] = (output, metrics)

    def get_with_skill(
        self,
        query: str,
        body: str,
        config_fingerprint: str,
        dependencies: Optional[list[str]] = None,
    ) -> Optional[Any]:
        key = self._make_skill_key(query, body, config_fingerprint, dependencies)
        val = self._cache.get(key)
        if val is not None:
            self.hits += 1
            return val
        self.misses += 1
        return None

    def set_with_skill(
        self,
        query: str,
        body: str,
        config_fingerprint: str,
        output: str,
        metrics: dict,
        provenances: Optional[list[Any]] = None,
        dependencies: Optional[list[str]] = None,
    ) -> None:
        key = self._make_skill_key(query, body, config_fingerprint, dependencies)
        self._cache[key] = (output, metrics, list(provenances or []))

    @staticmethod
    def _make_bare_key(query: str, config_fingerprint: str) -> str:
        q_hash = hashlib.sha256(query.encode("utf-8")).hexdigest()
        return f"bare:{config_fingerprint}:{q_hash}"

    @staticmethod
    def _make_skill_key(
        query: str,
        body: str,
        config_fingerprint: str,
        dependencies: Optional[list[str]] = None,
    ) -> str:
        q_hash = hashlib.sha256(query.encode("utf-8")).hexdigest()
        b_hash = hashlib.sha256(body.encode("utf-8")).hexdigest()
        from .fixtures import DEPENDENCY_FIXTURE_CONTRACT_VERSIONS

        dep_key = ",".join(
            f"{dependency}:{DEPENDENCY_FIXTURE_CONTRACT_VERSIONS.get(dependency, 'v1')}"
            for dependency in sorted(set(dependencies or []))
        )
        d_hash = hashlib.sha256(dep_key.encode("utf-8")).hexdigest()
        return f"skill:{config_fingerprint}:{d_hash}:{b_hash}:{q_hash}"

    def clear(self) -> None:
        self._cache.clear()
        self.hits = 0
        self.misses = 0

    def __len__(self) -> int:
        return len(self._cache)


DEFAULT_SYSTEM_PROMPT_HEADER = (
    "你是一个 Agent。以下是你要遵守的 Skill 说明书；请严格按 Instructions "
    "执行、按 Constraints 拒绝越界请求：\n\n"
)


class SkillEvaluator(Tool):
    def __init__(
        self,
        registry,
        llm,
        judge_llm=None,
        output_cache: Optional[EvaluatorOutputCache] = None,
        ledger=None,
        router=None,
        scoring_policy: str = "criteria_v1",
        rubric: Optional[dict[str, RuleDefinition]] = None,
    ):
        """
        Args:
            registry: SkillRegistry 实例（提供 get_meta / get body）
            llm:      运行 Agent 的 LLM（模拟"用户调用 Skill"）
            judge_llm: Judge 专用 LLM。必须是与执行端不同的 client 实例。
            output_cache: 可选，缓存 bare/current 输出以消除随机漂移
            ledger: 可选，挂载统一 LLM ledger
            router: 可选，IntentRouter 实例；不传时若 registry 具备 list_names 则自动构造
            scoring_policy: 评分策略，默认 "criteria_v1"（独立规则判定与确定性计分）
            rubric: 自定义评测规则字典（默认使用 DEFAULT_RUBRIC_V1）
        """
        super().__init__(
            name="skill_evaluator",
            description="八维评估器：结构分 40 + 效果分 60 + 客观指标 + 棘轮门槛",
        )
        self.registry = registry
        self.scoring_policy = scoring_policy
        if scoring_policy == "criteria_v1":
            self.rubric = rubric if rubric is not None else dict(DEFAULT_RUBRIC_V1)
            validate_rubric(self.rubric)
        else:
            self.rubric = rubric or {}

        if ledger is not None:
            from .llm_factory import wrap_with_ledger

            llm = wrap_with_ledger(llm, ledger, role="agent")
            judge_llm = wrap_with_ledger(judge_llm, ledger, role="judge")
        self.llm = llm
        if judge_llm is None:
            raise ValueError("judge_llm 必须显式配置，不能回退到执行 LLM")
        if judge_llm is llm:
            raise ValueError("执行 LLM 与 Judge 必须使用不同 client/session 实例")
        self.judge = PairwiseJudge(judge_llm)
        self.judge_llm = judge_llm
        self.ledger = ledger
        self.output_cache = output_cache if output_cache is not None else EvaluatorOutputCache()
        self.router_init_error: Optional[str] = None
        if router is not None:
            self.router = router
        elif hasattr(registry, "list_names"):
            try:
                from ..router import IntentRouter
                self.router = IntentRouter(registry=registry, llm=None)
            except Exception as exc:
                self.router = None
                self.router_init_error = (
                    f"ROUTER_INIT_ERROR: {type(exc).__name__}: {exc}"
                )
        else:
            self.router = None

    def get_config_fingerprint(self) -> str:
        """Return configuration fingerprint including execution and Judge settings."""
        from .llm_factory import compute_evaluator_fingerprint

        judge_llm = getattr(getattr(self, "judge", None), "llm", None) or getattr(self, "judge_llm", None)
        return compute_evaluator_fingerprint(
            self.llm,
            judge_llm,
            scoring_policy=self.scoring_policy,
            rubric=self.rubric,
        )

    def get_config_hash(self) -> str:
        return self.get_config_fingerprint()

    def get_parameters(self) -> list[ToolParameter]:
        return [
            ToolParameter(name="skill_name", type="string",
                          description="要评估的 skill_name", required=True),
            ToolParameter(name="eval_set", type="string",
                          description="评估集名（对应 evaluation_sets/<name>.json）",
                          required=False, default="baseline_dev"),
        ]

    def run(self, parameters: dict) -> str:
        result = self.evaluate_skill(
            parameters["skill_name"],
            parameters.get("eval_set", "baseline_dev"),
        )
        if not result.valid:
            return "valid=false, reasons=" + "; ".join(result.invalid_reasons)
        return f"valid=true, total={structure_total(result.structure_score) + sum(result.effect_score.values()):.2f}, p0={result.p0_pass}"

    # ARCHITECTURE §7 签名
    def evaluate(self, release_id: str, eval_set: str = "baseline_dev") -> EvalResult:
        """按 release_id 走：从 SQLite 反查 skill_name 再走 evaluate_skill"""
        rel = self.registry.get_current_release(release_id) if hasattr(self.registry, "get_current_release_by_id") else None
        # Phase 3 简化：直接从 release 表读 skill_name
        sm = self.registry._get_sm()
        row = sm.get_release(release_id)
        if not row:
            raise KeyError(f"release_id 不存在：{release_id}")
        return self.evaluate_skill(row["skill_name"], eval_set, release_id=release_id)

    def check_ratchet(self, old: Optional[EvalResult], new: EvalResult) -> RatchetVerdict:
        return _check_ratchet(old, new)

    def evaluate_p0_gate(
        self,
        skill_name: str,
        p0_cases: Optional[list[dict]] = None,
        repo_root: Optional[Path] = None,
        verbose: bool = False,
    ) -> P0GateResult:
        """运行独立 P0 门控评估"""
        return evaluate_p0_gate(
            self,
            skill_name=skill_name,
            p0_cases=p0_cases,
            repo_root=repo_root,
            verbose=verbose,
        )

    def check_p0_ratchet_verdict(self, p0_result: Optional[P0GateResult]) -> RatchetVerdict:
        return check_p0_ratchet_verdict(p0_result)

    # ----------------- 核心 -----------------

    def evaluate_skill(
        self,
        skill_name: str,
        eval_set: str = "baseline_dev",
        release_id: str = "",
        cases: Optional[list[dict]] = None,
        p0_ids: Optional[list[str]] = None,
        verbose: bool = False,
        collector: Optional[Any] = None,
        run_id: Optional[str] = None,
        task_id: Optional[str] = None,
        purpose: Literal["evaluation", "learning"] = "evaluation",
        runtime: Optional[Any] = None,
        tool_broker: Optional[Any] = None,
    ) -> EvalResult:
        """
        跑评估的核心方法（不强依赖 release_id）。

        Args:
            skill_name: 要评估的 skill
            eval_set:   评估集文件名（不含后缀），默认 baseline_dev
            release_id: 可选，写进 EvalResult；单跑评估时可空
            cases:      可选，直接传 case 列表（供测试注入）
            p0_ids:     可选，P0 case ID 列表（不传则从 p0_cases.json 读）
            verbose:    True 时逐 case 打印进度
            collector:  可选，ExperienceCollector 实例，托管自动运行采集生命周期
            run_id:     可选，指定 run_id（不传则自动生成）
            task_id:    可选，指定 task_id（不传则使用 eval_set:skill_name）
            purpose:    用途标记，默认 "evaluation"（heldout/评估隔离），显式声明 "learning" 时可用于候选挖掘
            runtime:    可选，AgentRuntime 实例，统一管理运行生命周期与 Broker 工具调度
            tool_broker: 可选，ToolBroker 实例，工具权限与模式校验
        """
        effective_run_id = run_id or f"eval_{skill_name}_{int(time.time() * 1000)}"
        effective_task_id = task_id or (release_id or f"{eval_set}:{skill_name}")

        meta = None
        try:
            meta = self.registry.get_meta(skill_name)
        except Exception:
            pass
        body = self.registry._bodies.get(skill_name, "") if hasattr(self.registry, "_bodies") else ""

        if runtime is not None:
            if collector is None and getattr(runtime, "collector", None) is not None:
                collector = runtime.collector
            runtime.start_run(
                run_id=effective_run_id,
                task_id=effective_task_id,
                skill_name=skill_name,
                purpose=purpose,
            )
        elif collector is not None:
            skill_ver = meta.version if meta is not None else ""
            collector.start_run(
                run_id=effective_run_id,
                task_id=effective_task_id,
                skill_name=skill_name,
                skill_version=skill_ver,
                environment={
                    "eval_set": eval_set,
                    "release_id": release_id,
                    "purpose": purpose,
                },
            )

        try:
            if meta is None:
                meta = self.registry.get_meta(skill_name)

            if cases is None:
                cases = self._load_cases(eval_set, skill_name)

            # 1. 结构分
            struct = score_structure(meta, body)

            # 空用例集 fail-closed 拦截：不允许伪造空评估全通
            if not cases:
                empty_res = EvalResult(
                    release_id=release_id,
                    structure_score=struct,
                    effect_score={"task": 0.0, "robust": 0.0, "readability": 0.0, "efficiency": 0.0},
                    objective_metrics={},
                    p0_pass=False,
                    case_verdicts=[],
                    case_outputs=[],
                    valid=False,
                    invalid_reasons=[f"用例集为空 (eval_set={eval_set}, skill={skill_name})，按 fail-closed 判为 INVALID"],
                    hit_layer="unknown",
                    verdict="EMPTY_CASES",
                    matched_keywords=[],
                    routing_notes="用例集为空，未执行路由评估",
                    route_result=None,
                    route_error="ROUTER_NOT_RUN: empty evaluation case set",
                    scoring_policy=self.scoring_policy,
                    criteria_findings=[],
                    critical_fail=False,
                    critical_reasons=[],
                )
                evidence = {
                    "independent_pass": None,
                    "is_invalid_eval": True,
                    "failure_reason": f"用例集为空 (eval_set={eval_set}, skill={skill_name})，按 fail-closed 判为 INVALID",
                    "valid": False,
                    "p0_pass": False,
                }
                if runtime is not None:
                    runtime.finalize_run(
                        run_id=effective_run_id,
                        model_output="",
                        verification_evidence=evidence,
                        acceptance_criteria={"eval_set": eval_set, "cases_count": 0},
                    )
                elif collector is not None:
                    collector.finish_run(
                        run_id=effective_run_id,
                        model_output="",
                        verification_evidence=evidence,
                        acceptance_criteria={"eval_set": eval_set, "cases_count": 0},
                    )
                return empty_res

            if p0_ids is None:
                p0_ids = self._load_p0_ids()

            dependencies = list(getattr(meta, "dependencies", []) or [])
            all_provenances: list[Any] = []

            # 2. 效果分
            base_metrics_list = []
            skill_metrics_list = []
            p0_pass = True
            invalid_reasons: list[str] = []
            ordering_run_id = release_id or f"{eval_set}:{skill_name}"

            case_verdicts: list[dict] = []  # Phase 4 元 Agent 输入
            case_outputs: list[dict] = []
            all_criteria_findings: list[dict] = []
            has_critical_fail = False
            all_critical_reasons: list[str] = []

            if self.scoring_policy == "criteria_v1":
                task_scores = []
                robust_scores = []
                readability_verdicts = []

                for i, case in enumerate(cases):
                    query = case["query"]
                    ref = case.get("reference")
                    case_id = case["id"]

                    if verbose:
                        print(f"  [{i + 1}/{len(cases)}] {case_id}: {query[:40]}...")

                    base_out, base_m = self._run_bare(query)
                    skill_out, skill_m = self._run_with_skill(
                        query,
                        body,
                        dependencies=dependencies,
                        skill_name=skill_name,
                        runtime=runtime,
                        tool_broker=tool_broker,
                        run_id=effective_run_id,
                    )
                    case_provs = list(getattr(self, "_last_skill_provenances", []) or [])
                    all_provenances.extend(case_provs)
                    if collector is not None and (runtime is None or getattr(runtime, "collector", None) is not collector):
                        for prov in case_provs:
                            collector.record_tool_call(
                                run_id=effective_run_id,
                                provenance=prov,
                                action_summary=f"Case {case_id} tool execution",
                            )
                    base_metrics_list.append(base_m)
                    skill_metrics_list.append(skill_m)

                    case_rubric = case.get("rubric") or self.rubric

                    # Step A: Evaluate Skill and Baseline against criteria
                    skill_findings, skill_invals = self._evaluate_case_criteria(
                        query=query,
                        answer=skill_out,
                        reference=ref,
                        rubric=case_rubric,
                        provenances=case_provs,
                        case=case,
                        target="skill",
                        run_id=effective_run_id,
                    )
                    base_findings, base_invals = self._evaluate_case_criteria(
                        query=query,
                        answer=base_out,
                        reference=ref,
                        rubric=case_rubric,
                        provenances=[],
                        case=case,
                        target="baseline",
                        run_id=effective_run_id,
                    )

                    for inv in skill_invals:
                        invalid_reasons.append(f"{case_id}/skill: {inv}")
                    for inv in base_invals:
                        invalid_reasons.append(f"{case_id}/baseline: {inv}")

                    # Step B: Compute deterministic scores & check critical fail
                    c_task, c_robust, c_valid, c_crit, c_reasons = compute_case_scores(skill_findings, case_rubric)
                    task_scores.append(c_task)
                    robust_scores.append(c_robust)
                    if not c_valid:
                        invalid_reasons.append(f"{case_id}/skill: UNKNOWN_CRITERIA_RULE")
                    if c_crit:
                        has_critical_fail = True
                        all_critical_reasons.extend(c_reasons)

                    b_task, b_robust, b_valid, _, _ = compute_case_scores(base_findings, case_rubric)
                    if not b_valid:
                        invalid_reasons.append(f"{case_id}/baseline: UNKNOWN_CRITERIA_RULE")

                    # Step C: Readability dimension - preserves balanced PairwiseJudge
                    read_skill_as_a = skill_is_presented_as_a(i, 2, ordering_run_id)
                    if read_skill_as_a:
                        judged_read = self.judge.compare_detailed(
                            query, skill_out, base_out, "readability", reference=ref,
                            tool_evidence_a=case_provs, tool_evidence_b=None,
                        )
                        read_verdict = judged_read.verdict
                        read_order = {"A": "skill", "B": "baseline"}
                    else:
                        judged_read = self.judge.compare_detailed(
                            query, base_out, skill_out, "readability", reference=ref,
                            tool_evidence_a=None, tool_evidence_b=case_provs,
                        )
                        read_verdict = invert_verdict(judged_read.verdict)
                        read_order = {"A": "baseline", "B": "skill"}

                    readability_verdicts.append((case_id, read_verdict))
                    if read_verdict == "INVALID":
                        invalid_reasons.append(
                            f"{case_id}/readability: "
                            + (",".join(judged_read.reason_codes) or "INVALID_JUDGE_RESULT")
                        )

                    # Step D: Derive pairwise verdicts for backward compatibility & RepairJob
                    derived_task = derive_pairwise_verdict(skill_findings, base_findings, "task_completion")
                    derived_robust = derive_pairwise_verdict(skill_findings, base_findings, "robustness")

                    per_case = {
                        "case_id": case_id,
                        "query": query,
                        "task_completion": derived_task,
                        "robustness": derived_robust,
                        "readability": read_verdict,
                        "skill_score": {"task": c_task, "robust": c_robust},
                        "baseline_score": {"task": b_task, "robust": b_robust},
                        "skill_findings": {k: f.to_dict() for k, f in skill_findings.items()},
                        "baseline_findings": {k: f.to_dict() for k, f in base_findings.items()},
                        "critical_fail": c_crit,
                        "critical_reasons": c_reasons,
                        "judge_audit": {
                            "readability": {
                                "presented_order": read_order,
                                "raw_verdict": judged_read.verdict,
                                "canonical_verdict": read_verdict,
                                "reason_codes": list(judged_read.reason_codes),
                                "evidence_summary": judged_read.evidence_summary,
                                "source": judged_read.source,
                            }
                        },
                    }
                    case_verdicts.append(per_case)

                    for f in skill_findings.values():
                        all_criteria_findings.append({**f.to_dict(), "case_id": case_id, "target": "skill"})
                    for f in base_findings.values():
                        all_criteria_findings.append({**f.to_dict(), "case_id": case_id, "target": "baseline"})

                    case_outputs.append({
                        "case_id": case_id,
                        "query": query,
                        "reference": ref,
                        "output_skill": skill_out,
                        "output_baseline": base_out,
                        "provenances": [
                            p.to_dict() if hasattr(p, "to_dict") else vars(p)
                            for p in case_provs
                        ],
                    })

                    # P0 check: critical fail, low score, or regression breaks P0
                    if case_id in p0_ids:
                        if c_crit or c_task < 15.0 or derived_task in ("B_better", "INVALID"):
                            p0_pass = False

                if has_critical_fail:
                    p0_pass = False

                effect = {
                    "task": round(mean(task_scores) if task_scores else 0.0, 2),
                    "robust": round(mean(robust_scores) if robust_scores else 0.0, 2),
                    "readability": self._dim_score_or_zero(readability_verdicts, max_score=10.0),
                    "efficiency": self._efficiency_score(base_metrics_list, skill_metrics_list),
                }
            else:
                verdicts = {"task_completion": [], "robustness": [], "readability": []}
                for i, case in enumerate(cases):
                    query = case["query"]
                    ref = case.get("reference")
                    case_id = case["id"]

                    if verbose:
                        print(f"  [{i + 1}/{len(cases)}] {case_id}: {query[:40]}...")

                    base_out, base_m = self._run_bare(query)
                    skill_out, skill_m = self._run_with_skill(
                        query,
                        body,
                        dependencies=dependencies,
                        skill_name=skill_name,
                        runtime=runtime,
                        tool_broker=tool_broker,
                        run_id=effective_run_id,
                    )
                    case_provs = list(getattr(self, "_last_skill_provenances", []) or [])
                    all_provenances.extend(case_provs)
                    if collector is not None and (runtime is None or getattr(runtime, "collector", None) is not collector):
                        for prov in case_provs:
                            collector.record_tool_call(
                                run_id=effective_run_id,
                                provenance=prov,
                                action_summary=f"Case {case_id} tool execution",
                            )
                    base_metrics_list.append(base_m)
                    skill_metrics_list.append(skill_m)

                    per_case = {"case_id": case_id, "query": query}
                    for dim_index, dim in enumerate(verdicts):
                        skill_as_a = skill_is_presented_as_a(i, dim_index, ordering_run_id)
                        if skill_as_a:
                            judged = self.judge.compare_detailed(
                                query,
                                skill_out,
                                base_out,
                                dim,
                                reference=ref,
                                tool_evidence_a=case_provs,
                                tool_evidence_b=None,
                            )
                            v = judged.verdict
                            presented_order = {"A": "skill", "B": "baseline"}
                        else:
                            judged = self.judge.compare_detailed(
                                query,
                                base_out,
                                skill_out,
                                dim,
                                reference=ref,
                                tool_evidence_a=None,
                                tool_evidence_b=case_provs,
                            )
                            v = invert_verdict(judged.verdict)
                            presented_order = {"A": "baseline", "B": "skill"}
                        verdicts[dim].append((case_id, v))
                        per_case[dim] = v
                        per_case.setdefault("judge_audit", {})[dim] = {
                            "presented_order": presented_order,
                            "raw_verdict": judged.verdict,
                            "canonical_verdict": v,
                            "reason_codes": list(judged.reason_codes),
                            "evidence_summary": judged.evidence_summary,
                            "source": judged.source,
                            "raw_response": judged.raw_response,
                            "ordering_run_id": ordering_run_id,
                        }
                        if v == "INVALID":
                            invalid_reasons.append(
                                f"{case_id}/{dim}: "
                                + (",".join(judged.reason_codes) or "INVALID_JUDGE_RESULT")
                            )
                        # P0 语义：task 维度上 skill 版被判 B_better（不如 baseline） → P0 fail
                        if (
                            case_id in p0_ids
                            and dim == "task_completion"
                            and v in {"B_better", "INVALID"}
                        ):
                            p0_pass = False
                    case_verdicts.append(per_case)
                    case_outputs.append({
                        "case_id": case_id,
                        "query": query,
                        "reference": ref,
                        "output_skill": skill_out,
                        "output_baseline": base_out,
                        "provenances": [
                            p.to_dict() if hasattr(p, "to_dict") else vars(p)
                            for p in case_provs
                        ],
                    })

                # 效果分：胜=1 平=0.5 负=0 加权
                effect = {
                    "task": self._dim_score_or_zero(verdicts["task_completion"], max_score=25.0),
                    "robust": self._dim_score_or_zero(verdicts["robustness"], max_score=15.0),
                    "readability": self._dim_score_or_zero(verdicts["readability"], max_score=10.0),
                    "efficiency": self._efficiency_score(base_metrics_list, skill_metrics_list),
                }

            # 3. 客观指标平均
            obj = {
                "avg_turns_skill": round(mean(m["turns"] for m in skill_metrics_list), 2),
                "avg_tokens_skill": round(mean(m["tokens"] for m in skill_metrics_list), 2),
                "avg_tokens_baseline": round(mean(m["tokens"] for m in base_metrics_list), 2),
                "avg_latency_ms_skill": round(mean(m["latency_ms"] for m in skill_metrics_list), 2),
            }

            # P0-1: 真实路由判定链计算与保存
            routed_results: list[tuple[dict, RouteResult]] = []
            route_error: Optional[str] = None
            if getattr(self, "router", None) is None:
                route_error = (
                    getattr(self, "router_init_error", None)
                    or "ROUTER_UNAVAILABLE: IntentRouter is not configured"
                )
            elif cases:
                for c in cases:
                    try:
                        q = c.get("query", "")
                        res = self.router.route(q)
                        routed_results.append((c, res))
                    except Exception as exc:
                        route_error = f"ROUTER_ERROR: {type(exc).__name__}: {exc}"
                        break

            route_result: Optional[RouteResult] = None
            hit_layer = "unknown"
            verdict_str = "ROUTE_UNEVALUATED"
            matched_kws: list[str] = []
            routing_notes = ""

            if route_error:
                hit_layer = "unknown"
                verdict_str = "ROUTE_UNAVAILABLE"
                matched_kws = []
                routing_notes = route_error
                route_result = None
            elif routed_results:
                target_pair = next((p for p in routed_results if p[1].chosen == skill_name), None)
                if target_pair:
                    _, route_result = target_pair
                    verdict_str = "ROUTE_MATCH"
                else:
                    _, route_result = routed_results[0]
                    verdict_str = "ROUTE_REJECT" if route_result.chosen is None else f"ROUTE_MISMATCH_{route_result.chosen}"
                hit_layer = route_result.hit_layer
                matched_kws = list(route_result.matched_keywords)
                routing_notes = route_result.routing_notes
            else:
                route_error = "ROUTER_UNAVAILABLE: no route result was produced"
                hit_layer = "unknown"
                verdict_str = "ROUTE_UNAVAILABLE"
                routing_notes = route_error

            eval_res = EvalResult(
                release_id=release_id,
                structure_score=struct,
                effect_score=effect,
                objective_metrics=obj,
                p0_pass=p0_pass,
                case_verdicts=case_verdicts,
                case_outputs=case_outputs,
                validation_channels=["dependency_fixtures"] if all_provenances else [],
                provenances=all_provenances,
                valid=not invalid_reasons,
                invalid_reasons=invalid_reasons,
                hit_layer=hit_layer,
                verdict=verdict_str,
                matched_keywords=matched_kws,
                routing_notes=routing_notes,
                route_result=route_result,
                route_error=route_error,
                scoring_policy=self.scoring_policy,
                criteria_findings=all_criteria_findings,
                critical_fail=has_critical_fail,
                critical_reasons=all_critical_reasons,
            )

            if runtime is not None or collector is not None:
                is_valid = eval_res.valid
                is_p0_pass = eval_res.p0_pass
                evidence = {
                    "checker": "SkillEvaluator.evaluate_skill",
                    "valid": is_valid,
                    "p0_pass": is_p0_pass,
                    "structure_score": eval_res.structure_score,
                    "effect_score": eval_res.effect_score,
                    "verdict": eval_res.verdict,
                }
                if not is_valid:
                    evidence["independent_pass"] = None
                    evidence["is_invalid_eval"] = True
                    evidence["failure_reason"] = "; ".join(eval_res.invalid_reasons) or "Evaluation invalid"
                elif not is_p0_pass:
                    evidence["independent_pass"] = False
                    evidence["failure_reason"] = "P0 gate failed: task completion regressed against baseline"
                else:
                    evidence["independent_pass"] = True

                last_output = ""
                if case_outputs:
                    last_output = str(case_outputs[-1].get("skill", ""))

                criteria = {
                    "eval_set": eval_set,
                    "p0_required": True,
                    "min_cases": len(cases),
                }

                if runtime is not None:
                    runtime.finalize_run(
                        run_id=effective_run_id,
                        model_output=last_output,
                        verification_evidence=evidence,
                        acceptance_criteria=criteria,
                    )
                elif collector is not None:
                    collector.finish_run(
                        run_id=effective_run_id,
                        model_output=last_output,
                        verification_evidence=evidence,
                        acceptance_criteria=criteria,
                    )

            return eval_res
        except Exception as exc:
            if runtime is not None:
                runtime.finalize_run(
                    run_id=effective_run_id,
                    infra_error=f"{type(exc).__name__}: {exc}",
                )
            elif collector is not None:
                collector.finish_run(
                    run_id=effective_run_id,
                    infra_error=f"{type(exc).__name__}: {exc}",
                )
            raise

    # ----------------- 辅助 -----------------

    def _load_cases(self, eval_set: str, skill_name: str) -> list[dict]:
        """从 evaluation_sets/<name>.json 加载 skill 相关的 case"""
        repo_root = self.registry.repo_root
        path = repo_root / "evaluation_sets" / f"{eval_set}.json"
        if not path.exists():
            raise FileNotFoundError(f"评估集不存在：{path}")
        import json
        data = json.loads(path.read_text(encoding="utf-8"))
        cases = [c for c in data.get("cases", []) if c.get("skill") == skill_name]
        return cases

    def _load_p0_ids(self) -> list[str]:
        """加载 p0_cases.json 的 case ID 列表"""
        path = self.registry.repo_root / "evaluation_sets" / "p0_cases.json"
        if not path.exists():
            return []
        import json
        data = json.loads(path.read_text(encoding="utf-8"))
        return data.get("p0_ids", [])

    def _run_bare(self, query: str) -> tuple[str, dict]:
        """无 Skill 的 Agent 跑 baseline"""
        fingerprint = self.get_config_fingerprint()
        if self.output_cache is not None:
            cached = self.output_cache.get_bare(query, fingerprint)
            if cached is not None:
                return cached

        started = time.perf_counter()
        messages = [{"role": "user", "content": query}]
        resp = self.llm.invoke(messages)
        latency_ms = (time.perf_counter() - started) * 1000

        content = str(getattr(resp, "content", resp) or "")
        usage = self._extract_usage(resp)
        run_log = {
            "messages": messages + [{"role": "assistant", "content": content}],
            "usage": usage,
            "latency_ms": latency_ms,
        }
        res = content, collect_objective_metrics(run_log)
        if self.output_cache is not None:
            self.output_cache.set_bare(query, fingerprint, res[0], res[1])
        return res

    def _run_with_skill(
        self,
        query: str,
        body: str,
        dependencies: Optional[list[str]] = None,
        skill_name: Optional[str] = None,
        runtime: Optional[Any] = None,
        tool_broker: Optional[Any] = None,
        run_id: Optional[str] = None,
    ) -> tuple[str, dict]:
        """有 Skill 的 Agent 跑：若具备依赖且支持工具调用，则挂载受控 fixture 或 BrokeredTool 生成 provenance"""
        fingerprint = self.get_config_fingerprint()
        if self.output_cache is not None:
            cached = self.output_cache.get_with_skill(query, body, fingerprint, dependencies)
            if cached is not None:
                if len(cached) >= 3:
                    self._last_skill_provenances = cached[2]
                else:
                    self._last_skill_provenances = []
                return cached[0], cached[1]

        if getattr(self, "_injected_provenances", None) is not None:
            self._last_skill_provenances = list(self._injected_provenances)
        else:
            self._last_skill_provenances = []
        started = time.perf_counter()

        if dependencies and hasattr(self.llm, "invoke_with_tools"):
            from hello_agents import SimpleAgent
            from hello_agents.core.config import Config
            from hello_agents.tools import ToolRegistry
            from .fixtures import _FIXTURE_FACTORIES, build_provenances_for_fixture

            active_fixtures: dict[str, Any] = {}
            tool_registry = ToolRegistry()
            effective_broker = tool_broker or (runtime.tool_broker if runtime else None)
            prov_count_before = (
                len(runtime.get_provenances(run_id))
                if (runtime and run_id and hasattr(runtime, "get_provenances"))
                else 0
            )

            for dep in dependencies:
                factory = _FIXTURE_FACTORIES.get(dep)
                if runtime is not None:
                    if effective_broker is not None and effective_broker.get_tool(dep) is None:
                        if factory is not None:
                            effective_broker.register_tool(dep, factory())
                    brokered = runtime.create_brokered_tool(dep, run_id=run_id or "eval_run")
                    tool_registry.register_tool(brokered)
                    active_fixtures[dep] = brokered
                else:
                    if factory is not None:
                        fixture = factory()
                        active_fixtures[dep] = fixture
                        tool_registry.register_tool(fixture)

            if active_fixtures:
                agent = SimpleAgent(
                    name=f"skill_agent_{skill_name or 'eval'}",
                    llm=self.llm,
                    system_prompt=DEFAULT_SYSTEM_PROMPT_HEADER + body,
                    config=Config(
                        trace_enabled=False,
                        skills_enabled=False,
                        session_enabled=False,
                        subagent_enabled=False,
                        todowrite_enabled=False,
                        devlog_enabled=False,
                    ),
                    tool_registry=tool_registry,
                    enable_tool_calling=True,
                    max_tool_iterations=3,
                )
                try:
                    content = agent.run(query)
                except BudgetExceededError:
                    raise
                except Exception as exc:
                    content = f"AGENT_EXEC_ERROR: {type(exc).__name__}: {exc}"

                latency_ms = (time.perf_counter() - started) * 1000

                if runtime is not None and run_id and hasattr(runtime, "get_provenances"):
                    case_provs = runtime.get_provenances(run_id)[prov_count_before:]
                else:
                    case_provs = []
                    for dep, fix in active_fixtures.items():
                        provs = build_provenances_for_fixture(
                            dependency=dep,
                            fixture=fix,
                            agent_output=content,
                            skill_body=body,
                            query=query,
                        )
                        case_provs.extend(provs)

                self._last_skill_provenances = case_provs

                run_log = {
                    "messages": [
                        {"role": "user", "content": query},
                        {"role": "assistant", "content": content},
                    ],
                    "usage": {},
                    "latency_ms": latency_ms,
                }
                res = content, collect_objective_metrics(run_log)
                if self.output_cache is not None:
                    self.output_cache.set_with_skill(
                        query,
                        body,
                        fingerprint,
                        res[0],
                        res[1],
                        provenances=self._last_skill_provenances,
                        dependencies=dependencies,
                    )
                return res

        messages = [
            {"role": "system", "content": DEFAULT_SYSTEM_PROMPT_HEADER + body},
            {"role": "user", "content": query},
        ]
        resp = self.llm.invoke(messages)
        latency_ms = (time.perf_counter() - started) * 1000

        content = str(getattr(resp, "content", resp) or "")
        usage = self._extract_usage(resp)
        run_log = {
            "messages": messages + [{"role": "assistant", "content": content}],
            "usage": usage,
            "latency_ms": latency_ms,
        }
        res = content, collect_objective_metrics(run_log)
        if self.output_cache is not None:
            self.output_cache.set_with_skill(
                query,
                body,
                fingerprint,
                res[0],
                res[1],
                provenances=[],
                dependencies=dependencies,
            )
        return res

    def _evaluate_case_criteria(
        self,
        query: str,
        answer: str,
        reference: Optional[str],
        rubric: dict[str, RuleDefinition],
        provenances: list[Any],
        case: dict,
        target: str,
        run_id: str,
    ) -> tuple[dict[str, RuleFinding], list[str]]:
        """Evaluate a single agent response against the rubric.

        Executes deterministic code checks (logistics oracle, empty answer, truth sentinel)
        before falling back to the Judge LLM for semantic criteria.
        """
        findings: dict[str, RuleFinding] = {}
        invalid_codes: list[str] = []

        # Check 1: Logistics Oracle (Code Oracle)
        oracle_name = str(case.get("oracle") or case.get("trusted_oracle") or case.get("scenario") or "")
        is_logistics = (
            "logistics" in oracle_name.lower()
            or "ORD_" in query
            or case.get("order_id") is not None
        )
        if is_logistics:
            from ..scenarios.logistics import verify_logistics_fulfillment_as_findings
            from ..models import ToolCallRecord

            order_id = case.get("order_id")
            if not order_id:
                m = re.search(r"ORD_\d{4}_\d{4}", query)
                order_id = m.group(0) if m else "ORD_2026_0901"

            records: list[ToolCallRecord] = []
            for p in provenances:
                if isinstance(p, ToolCallRecord):
                    records.append(p)
                elif hasattr(p, "tool_name"):
                    records.append(
                        ToolCallRecord(
                            call_id=f"call_{getattr(p, 'call_index', 0)}",
                            run_id=run_id,
                            tool_name=getattr(p, "tool_name", ""),
                            status="EXECUTED" if getattr(p, "tool_success", True) else "ERROR",
                            input_params=getattr(p, "input_params", {}),
                            output_text=getattr(p, "output_summary", ""),
                            provenance=p if isinstance(p, ToolCallProvenance) else None,
                        )
                    )

            oracle_findings = verify_logistics_fulfillment_as_findings(
                model_output=answer,
                order_id=order_id,
                tool_records=records,
                intent_constraint=case.get("intent_constraint"),
                expected_permission_denial=bool(case.get("expected_permission_denial", False)),
                expect_tool_failure=bool(case.get("expect_tool_failure", False)),
                failing_package_ids=case.get("failing_package_ids"),
                infra_error=case.get("infra_error"),
                expected_packages=case.get("expected_packages"),
            )
            # Reconcile with rubric
            for rid, rule in rubric.items():
                if not rule.applicable:
                    continue
                if rid in oracle_findings:
                    f = oracle_findings[rid]
                    findings[rid] = RuleFinding(
                        rule_id=rid,
                        dimension=rule.dimension,
                        status=f.status,
                        weight=rule.weight,
                        critical=bool(rule.critical or getattr(f, "critical", False)),
                        evidence=f.evidence,
                        reason=f.reason,
                        source="code_oracle",
                        deduction=0.0 if f.status == "PASS" else float(rule.weight),
                    )
                else:
                    findings[rid] = RuleFinding(
                        rule_id=rid,
                        dimension=rule.dimension,
                        status="UNKNOWN",
                        weight=rule.weight,
                        critical=rule.critical,
                        evidence="",
                        reason=f"Code oracle did not cover rule '{rid}'",
                        source="infrastructure",
                        deduction=float(rule.weight),
                    )
            return findings, invalid_codes

        # Check 2: Deterministic checks for non-logistics cases
        # 2a: Empty answer
        if not answer.strip() or answer.strip() == "(空回答)":
            has_failure_context = bool(
                case.get("expect_tool_failure")
                or case.get("expected_permission_denial")
                or case.get("infra_error")
            )
            for rid, rule in rubric.items():
                if not rule.applicable:
                    continue
                if rule.dimension == "task_completion":
                    findings[rid] = RuleFinding(
                        rule_id=rid,
                        dimension=rule.dimension,
                        status="FAIL",
                        weight=rule.weight,
                        critical=rule.critical,
                        evidence="(空回答)",
                        reason="候选回答为空，未完成任何任务目标或约束",
                        source="deterministic_gate",
                        deduction=float(rule.weight),
                    )
                else:
                    if rid == "ROBUST_FAILURE_HANDLING" and has_failure_context:
                        findings[rid] = RuleFinding(
                            rule_id=rid,
                            dimension=rule.dimension,
                            status="FAIL",
                            weight=rule.weight,
                            critical=True,
                            evidence="(空回答)",
                            reason="故障上下文下候选回答为空，未进行降级说明或故障处理",
                            source="deterministic_gate",
                            deduction=float(rule.weight),
                        )
                    else:
                        findings[rid] = RuleFinding(
                            rule_id=rid,
                            dimension=rule.dimension,
                            status="PASS",
                            weight=rule.weight,
                            critical=rule.critical,
                            evidence="(空回答)",
                            reason="空回答未编造未验证事实且无未处理故障",
                            source="deterministic_gate",
                            deduction=0.0,
                        )
            return findings, invalid_codes

        # 2b: Truth Sentinel for unverified facts
        from .judge import has_unverified_realtime_numeric_claim
        has_unverified = has_unverified_realtime_numeric_claim(
            query,
            answer,
            reference=reference,
            has_tool_evidence=provenances,
        )
        if has_unverified and "ROBUST_EVIDENCE_FAITHFUL" in rubric:
            rf = rubric["ROBUST_EVIDENCE_FAITHFUL"]
            findings["ROBUST_EVIDENCE_FAITHFUL"] = RuleFinding(
                rule_id="ROBUST_EVIDENCE_FAITHFUL",
                dimension="robustness",
                status="FAIL",
                weight=rf.weight,
                critical=rf.critical,
                evidence="包含实时或数值断言但缺乏经过核验的工具快照凭据",
                reason="UNVERIFIED_EXTERNAL_FACT: 给出实时数值但没有工具 provenance",
                source="truth_sentinel",
                deduction=float(rf.weight),
            )

        # 2c: Evaluate remaining rules via batched judge LLM call
        remaining_rubric = {
            rid: r for rid, r in rubric.items()
            if r.applicable and rid not in findings
        }
        if remaining_rubric:
            llm_findings, invals = evaluate_semantic_criteria(
                judge_llm=self.judge_llm,
                query=query,
                answer=answer,
                reference=reference,
                rubric=remaining_rubric,
                tool_provenances=provenances,
            )
            findings.update(llm_findings)
            invalid_codes.extend(invals)

        return findings, invalid_codes

    @staticmethod
    def _extract_usage(resp) -> dict:
        u = getattr(resp, "usage", None)
        if u is None:
            return {}
        if isinstance(u, dict):
            return u
        # LLMResponse.usage 可能是自定义对象；转 dict 取常见字段
        return {
            "prompt_tokens": getattr(u, "prompt_tokens", 0),
            "completion_tokens": getattr(u, "completion_tokens", 0),
            "total_tokens": getattr(u, "total_tokens", 0),
        }

    @staticmethod
    def _dim_score(case_verdicts: list[tuple[str, str]], max_score: float) -> float:
        """配对判定 → 维度分：胜 1 / 平 0.5 / 负 0，加权到 max_score"""
        if not case_verdicts:
            return 0.0
        if any(v == "INVALID" for _, v in case_verdicts):
            raise ValueError("INVALID Judge verdict 不能折算为效果分")
        n = len(case_verdicts)
        weighted = sum(
            1.0 if v == "A_better" else 0.5 if v == "tied" else 0.0
            for _, v in case_verdicts
        )
        return round(weighted / n * max_score, 2)

    @staticmethod
    def _dim_score_or_zero(case_verdicts: list[tuple[str, str]], max_score: float) -> float:
        """Conservative placeholder for an explicitly invalid evaluation."""
        if any(v == "INVALID" for _, v in case_verdicts):
            return 0.0
        return SkillEvaluator._dim_score(case_verdicts, max_score)

    @staticmethod
    def _efficiency_score(base_metrics: list[dict], skill_metrics: list[dict]) -> float:
        """效率维度：skill token 平均 / baseline token 平均
           ratio=1 → 10；ratio=2 → 5；ratio=3 → 0（clamp [0, 10]）
           无有效数据 → 5（居中）
        """
        base_tokens = [m["tokens"] for m in base_metrics if m["tokens"] > 0]
        skill_tokens = [m["tokens"] for m in skill_metrics if m["tokens"] > 0]
        if not base_tokens or not skill_tokens:
            return 5.0
        ratio = mean(skill_tokens) / mean(base_tokens)
        score = 10.0 - 5.0 * (ratio - 1.0)
        return round(max(0.0, min(10.0, score)), 2)


__all__ = [
    "SkillEvaluator",
    "EvaluatorOutputCache",
    "P0GateError",
    "P0LoadError",
    "P0EmptyCasesError",
    "P0GateResult",
    "load_p0_cases",
    "evaluate_p0_gate",
    "check_p0_ratchet_verdict",
    "merge_p0_into_eval_result",
    "PromptFragment",
    "ROUTING_METADATA",
    "ROUTING_METADATA_SCHEMA",
    "EXECUTION_BEHAVIOR",
    "EXECUTION_BEHAVIOR_SCHEMA",
    "format_routing_metadata",
    "format_execution_behavior",
    "extract_relevant_body_sections",
    "format_body_sections",
    "validate_payload_against_schema",
]
