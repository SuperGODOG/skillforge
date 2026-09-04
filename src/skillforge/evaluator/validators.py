"""Validators coupled to metadata and dependency change surfaces."""
from __future__ import annotations

import json
import tempfile
from pathlib import Path
from typing import Optional

from ..models import RatchetVerdict, ToolCallProvenance
from ..router import IntentRouter
from ..router.evaluation import evaluate_router_cases
from .fixtures import execute_dependency_fixtures


def validate_router_patch(
    base_registry,
    candidate_registry,
    skill_name: str,
    changed_frontmatter: Optional[list[str]] = None,
    computed_level: Optional[str] = None,
) -> RatchetVerdict:
    """Reject candidate routing regressions and every hard-negative capture."""
    eval_path = base_registry.repo_root / "evaluation_sets" / "router_negatives.json"
    data = json.loads(eval_path.read_text(encoding="utf-8"))
    cases = data.get("cases", [])
    target_positives = [case for case in cases if case.get("expected") == skill_name]
    hard_negatives = [case for case in cases if case.get("type") == "hard_negative"]
    if not target_positives or not hard_negatives:
        return RatchetVerdict(
            decision="DECLINED",
            reasons=["Router 验证集缺少目标 Skill 正例或硬负例，按 fail-closed 拒绝"],
        )

    # No LLM fallback: validation must not consume tokens or depend on a remote service.
    baseline = evaluate_router_cases(cases, IntentRouter(base_registry, llm=None))
    candidate = evaluate_router_cases(cases, IntentRouter(candidate_registry, llm=None))
    base_by_id = {item["id"]: item for item in baseline["case_results"]}

    reasons: list[str] = []
    if computed_level in {"L2", "L3"}:
        targets = data.get("meta", {}).get("targets", {})
        r1_target = float(targets.get("recall_at_1", 1.0))
        r3_target = float(targets.get("recall_at_3", 1.0))
        if candidate["retrieval_at_1"] < r1_target:
            reasons.append(
                f"Router 全量 retrieval@1={candidate['retrieval_at_1']:.2%} "
                f"低于门槛 {r1_target:.2%}"
            )
        if candidate["recall_at_3"] < r3_target:
            reasons.append(
                f"Router 全量 R@3={candidate['recall_at_3']:.2%} "
                f"低于门槛 {r3_target:.2%}"
            )
    positive_regressions = [
        item["id"]
        for item in candidate["case_results"]
        if item["expected"] == skill_name
        and base_by_id[item["id"]]["retrieval_r1"]
        and not item["retrieval_r1"]
    ]
    if positive_regressions:
        reasons.append(
            f"Router 正例召回退步: {', '.join(positive_regressions)}"
        )

    hard_negative_captures = [
        item["id"]
        for item in candidate["case_results"]
        if item["type"] == "hard_negative" and item["chosen"] is not None
    ]
    if hard_negative_captures:
        reasons.append(
            f"Router 硬负例误召回: {', '.join(hard_negative_captures)}"
        )

    cross_skill_regressions = [
        item["id"]
        for item in candidate["case_results"]
        if item["expected"] not in (None, skill_name)
        and base_by_id[item["id"]]["retrieval_r1"]
        and not item["retrieval_r1"]
    ]
    if cross_skill_regressions:
        reasons.append(
            f"Router 跨 Skill 正例退步: {', '.join(cross_skill_regressions)}"
        )
    new_cross_skill_hijacks = [
        item["id"]
        for item in candidate["case_results"]
        if item["expected"] not in (None, skill_name)
        and item["chosen"] == skill_name
        and base_by_id[item["id"]]["chosen"] != skill_name
    ]
    if new_cross_skill_hijacks:
        reasons.append(
            f"Router 新增跨 Skill 劫持: {', '.join(new_cross_skill_hijacks)}"
        )

    # trigger.keywords only controls the router's no-embedding degradation path.
    # Exercise that path explicitly; the normal BGE path does not consume triggers.
    if "trigger" in (changed_frontmatter or []):
        with tempfile.TemporaryDirectory(prefix="skillforge-router-fallback-") as root:
            missing_model = Path(root) / "missing-model"
            degraded_baseline = evaluate_router_cases(
                cases,
                IntentRouter(base_registry, llm=None, model_dir=missing_model),
            )
            degraded_candidate = evaluate_router_cases(
                cases,
                IntentRouter(candidate_registry, llm=None, model_dir=missing_model),
            )
        degraded_base_by_id = {
            item["id"]: item for item in degraded_baseline["case_results"]
        }
        new_fallback_captures = [
            item["id"]
            for item in degraded_candidate["case_results"]
            if item["type"] == "hard_negative"
            and item["chosen"] is not None
            and degraded_base_by_id[item["id"]]["chosen"] is None
        ]
        if new_fallback_captures:
            reasons.append(
                "Router 规则降级路径新增硬负例误召回: "
                + ", ".join(new_fallback_captures)
            )
        fallback_positive_regressions = [
            item["id"]
            for item in degraded_candidate["case_results"]
            if item["expected"] == skill_name
            and degraded_base_by_id[item["id"]]["retrieval_r1"]
            and not item["retrieval_r1"]
        ]
        if fallback_positive_regressions:
            reasons.append(
                "Router 规则降级路径正例召回退步: "
                + ", ".join(fallback_positive_regressions)
            )
        fallback_cross_skill_regressions = [
            item["id"]
            for item in degraded_candidate["case_results"]
            if item["expected"] not in (None, skill_name)
            and degraded_base_by_id[item["id"]]["retrieval_r1"]
            and not item["retrieval_r1"]
        ]
        if fallback_cross_skill_regressions:
            reasons.append(
                "Router 规则降级路径跨 Skill 正例退步: "
                + ", ".join(fallback_cross_skill_regressions)
            )
        fallback_new_hijacks = [
            item["id"]
            for item in degraded_candidate["case_results"]
            if item["expected"] not in (None, skill_name)
            and item["chosen"] == skill_name
            and degraded_base_by_id[item["id"]]["chosen"] != skill_name
        ]
        if fallback_new_hijacks:
            reasons.append(
                "Router 规则降级路径新增跨 Skill 劫持: "
                + ", ".join(fallback_new_hijacks)
            )

    if reasons:
        return RatchetVerdict(decision="DECLINED", reasons=reasons)
    return RatchetVerdict(decision="PASS", reasons=[])


def validate_dependency_patch(
    base_registry, candidate_registry, skill_name: str, llm=None
) -> tuple[RatchetVerdict, list[ToolCallProvenance]]:
    """Execute candidate dependencies against registered fixtures, failing closed."""
    old_dependencies = base_registry.get_meta(skill_name).dependencies
    dependencies = candidate_registry.get_meta(skill_name).dependencies
    removed = sorted(set(old_dependencies) - set(dependencies))
    candidate_body = candidate_registry._bodies.get(skill_name, "")
    passed, provenances, reasons = execute_dependency_fixtures(
        dependencies,
        removed,
        llm=llm,
        skill_body=candidate_body,
    )
    referenced_removed = [name for name in removed if name in candidate_body]
    if referenced_removed:
        passed = False
        reasons.append(
            "已移除的 dependency 仍被 Body 引用: " + ", ".join(referenced_removed)
        )
    if not provenances:
        passed = False
        reasons.append("dependency 改动未产生验证凭证，按 fail-closed 拒绝")
    return (
        RatchetVerdict(decision="PASS" if passed else "DECLINED", reasons=reasons),
        provenances,
    )
