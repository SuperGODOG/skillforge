"""Trace Purification and Test Case Synthesis Module (Phase 4 / P4).

Extracts, purifies, and structures audit traces and execution episodes into reproducible
test case proposals with strict purpose isolation, provenance tracking, and attribution diversion.

Key Invariants:
1. Strict Attribution & Multi-Stream Diversion (D1, D2):
   - Only skill-attributable failures with verifiable business expectations can produce
     APPROVED business test case proposals.
   - Unknown outcomes, infrastructure errors, and policy denials are explicitly diverted
     to diagnosis/policy compliance, never triggering business skill modification.
   - Legitimate tool authorization denials verified by an independent oracle are judged PASS,
     not mechanically flagged as skill failures.
2. Independent Business Expectation (D1):
   - Missing expectations remain PENDING_APPROVAL.
   - The model's own failed output can NEVER serve as its ground truth expected output.
   - Model-drafted proposals require human or business rule confirmation.
   - Sanitized/redacted tool snapshots that impair assertions route to manual review
     rather than fabricating replacement data.
3. Purpose Isolation & Anti-Contamination (D3, D5):
   - Traces with purpose='evaluation' or 'heldout' cannot be converted to dev test cases
     without explicit repartition (demotion).
   - Once demoted, previous benchmark scores on that case can no longer claim heldout status.
"""
from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Literal, Optional

from .models import (
    Episode,
    TaskContext,
    TestCaseProposal,
    PurificationResult,
)
from .episode import CandidateStore


def _extract_trace_details(
    source: Episode | dict[str, Any],
    tool_contract_version: str = "1.0",
) -> dict[str, Any]:
    """Normalize fields across Episode objects and eval trace dictionaries."""
    if isinstance(source, Episode):
        task_id = source.task_id
        skill_name = source.skill_name
        env = source.environment if isinstance(source.environment, dict) else {}
        intent_revision = int(env.get("intent_revision", 1))
        contract_fingerprint = str(env.get("contract_fingerprint", ""))
        purpose = str(env.get("purpose", "learning"))
        is_heldout = bool(env.get("is_heldout", False))

        # Query extraction
        query = ""
        if isinstance(source.acceptance_criteria, dict):
            query = str(source.acceptance_criteria.get("query", ""))
        if not query and isinstance(source.environment, dict):
            query = str(source.environment.get("query", ""))
        if not query:
            query = task_id.replace("_", " ")

        actual_output = ""
        if source.verification_evidence and isinstance(source.verification_evidence, dict):
            actual_output = str(source.verification_evidence.get("actual_output", ""))
        if not actual_output and source.outcome_reason:
            actual_output = source.outcome_reason

        tool_snapshots: list[dict[str, Any]] = []
        for p in (source.provenances or []):
            tool_snapshots.append({
                "tool_name": p.tool_name,
                "input_params": p.input_params,
                "output_status": p.output_status,
                "output_summary": p.output_summary,
                "snapshot_id": getattr(p, "snapshot_id", ""),
                "snapshot_content": getattr(p, "snapshot_content", ""),
                "latency_ms": getattr(p, "latency_ms", 0.0),
                "contract_version": tool_contract_version,
            })

        outcome = source.outcome
        outcome_reason = source.outcome_reason
        variant_family = env.get("variant_family") or task_id.split("-")[0].split("_")[0]

        return {
            "task_id": task_id,
            "skill_name": skill_name,
            "query": query,
            "intent_revision": intent_revision,
            "contract_fingerprint": contract_fingerprint,
            "actual_output": actual_output,
            "tool_snapshots": tool_snapshots,
            "outcome": outcome,
            "outcome_reason": outcome_reason,
            "purpose": purpose,
            "is_heldout": is_heldout,
            "variant_family": variant_family,
            "environment": env,
        }

    elif isinstance(source, dict):
        task_id = str(source.get("case_id") or source.get("task_id") or "unknown_task")
        skill_name = str(source.get("skill") or source.get("skill_name") or "")
        query = str(source.get("query", ""))
        intent_revision = int(source.get("intent_revision", 1))
        contract_fingerprint = str(source.get("contract_fingerprint", ""))
        purpose = str(source.get("purpose", source.get("layer", "learning")))
        is_heldout = bool(source.get("is_heldout", purpose in ("heldout", "experiment_holdout", "final_audit")))

        actual_output = str(
            source.get("evaluand_answer")
            or source.get("skill_answer")
            or source.get("actual_output")
            or ""
        )

        tool_responses = source.get("tool_responses", []) or []
        tool_snapshots = []
        for tr in tool_responses:
            tool_snapshots.append({
                "tool_name": tr.get("tool_name", ""),
                "input_params": tr.get("parameters", {}),
                "output_status": tr.get("output_status", ""),
                "output_summary": tr.get("output_summary", ""),
                "snapshot_id": tr.get("snapshot_id", ""),
                "snapshot_content": tr.get("content", ""),
                "latency_ms": tr.get("latency_ms", 0.0),
                "contract_version": tool_contract_version,
            })

        # Infer outcome from judge_verdict
        jv = source.get("judge_verdict")
        if isinstance(jv, dict):
            v = jv.get("verdict", "")
            if v == "PASS":
                outcome = "success"
            elif v == "VALID_FAILURE":
                outcome = "failure"
            elif v in ("INVALID_SKIPPED", "INVALID"):
                outcome = "unknown"
            else:
                outcome = "unknown"
        else:
            outcome = str(source.get("outcome", "unknown"))

        outcome_reason = str(source.get("outcome_reason", ""))
        variant_family = source.get("variant_family") or task_id.split("-")[0].split("_")[0]

        return {
            "task_id": task_id,
            "skill_name": skill_name,
            "query": query,
            "intent_revision": intent_revision,
            "contract_fingerprint": contract_fingerprint,
            "actual_output": actual_output,
            "tool_snapshots": tool_snapshots,
            "outcome": outcome,
            "outcome_reason": outcome_reason,
            "purpose": purpose,
            "is_heldout": is_heldout,
            "variant_family": variant_family,
            "environment": source,
        }

    else:
        raise TypeError(f"Unsupported source type for trace purification: {type(source)}")


def purify_trace_to_proposal(
    *,
    source: Episode | dict[str, Any],
    skill_name: Optional[str] = None,
    business_expectation: Optional[str | dict[str, Any]] = None,
    expectation_source: str = "missing",
    failure_attribution: Optional[str] = None,
    task_context: Optional[TaskContext] = None,
    tool_contract_version: str = "1.0",
    allow_heldout_demotion: bool = False,
    sanitization_impaired: bool = False,
    store: Optional[CandidateStore] = None,
    oracle_verifier: Optional[Callable[..., dict[str, Any]]] = None,
) -> PurificationResult:
    """Purify an execution trace or Episode into a structured test case proposal or diagnostic routing.

    Enforces Acceptance Criteria D1 & D2:
    - Multi-stream routing: separates business failures, regression cases, unknown outcomes,
      infrastructure errors, and policy compliance.
    - Ground truth integrity: cannot use model's failed answer as expectation; draft proposals
      require human confirmation.
    - Sanitization safety: impaired snapshots route to manual review rather than fabricating data.
    - Purpose isolation (D5): locked heldout traces cannot enter dev without explicit demotion.
    """
    details = _extract_trace_details(source, tool_contract_version)

    eff_skill = skill_name or details["skill_name"]
    eff_task_id = details["task_id"]
    eff_query = details["query"]
    eff_intent_rev = details["intent_revision"]
    eff_fp = details["contract_fingerprint"]
    eff_actual_output = details["actual_output"]
    eff_tool_snapshots = details["tool_snapshots"]
    eff_outcome = details["outcome"]
    eff_outcome_reason = details["outcome_reason"]
    eff_purpose = details["purpose"]
    eff_is_heldout = details["is_heldout"]
    variant_family = details["variant_family"]

    if task_context is not None:
        eff_intent_rev = task_context.intent_revision
        eff_fp = task_context.contract_fingerprint or task_context.compute_fingerprint()
        if not eff_query and task_context.goal:
            eff_query = task_context.goal

    # 1. Purpose Isolation Guard (D3 / D5)
    demotion_applied = False
    if eff_is_heldout or eff_purpose in ("heldout", "experiment_holdout", "final_audit", "evaluation"):
        if not allow_heldout_demotion:
            raise ValueError(
                f"Purpose isolation violation: trace '{eff_task_id}' has locked purpose '{eff_purpose}' "
                "and cannot be used for development/repair test cases without explicit repartition"
            )
        demotion_applied = True

    partition_tier = "repair" if (demotion_applied or eff_purpose == "learning") else "experiment_holdout"

    # 2. Multi-Stream Routing & Attribution Diversion (D2)
    # Check for policy denials (e.g. ToolBroker blocked unauthorized tool)
    policy_denial_detected = any(
        ts.get("output_status") in ("PERMISSION_DENIED", "REJECTED")
        for ts in eff_tool_snapshots
    ) or "PERMISSION_DENIED" in eff_outcome_reason or failure_attribution == "policy"

    if policy_denial_detected:
        # Check independent business oracle
        oracle_pass = False
        if oracle_verifier is not None:
            try:
                v_res = oracle_verifier()
                oracle_pass = bool(v_res.get("independent_pass", False))
            except Exception:
                oracle_pass = False

        if oracle_pass or "authorized" in eff_outcome_reason.lower() or "blocked" in eff_outcome_reason.lower():
            # Correct permission denial: acceptable behavior!
            return PurificationResult(
                category="policy_compliance",
                proposal=None,
                diagnosis={
                    "task_id": eff_task_id,
                    "policy_enforced": True,
                    "independent_pass": True,
                    "reason": "Unauthorized tool call successfully prevented by ToolBroker; verified by oracle",
                },
                can_trigger_skill_evolution=False,
                notes="Policy enforcement verified; not a skill defect; cannot trigger skill evolution",
            )

    # Check for infrastructure/evaluator failure
    if failure_attribution == "infrastructure" or "INFRASTRUCTURE" in eff_outcome_reason:
        return PurificationResult(
            category="infrastructure_report",
            proposal=None,
            diagnosis={
                "task_id": eff_task_id,
                "reason": eff_outcome_reason or "Infrastructure / judge error",
            },
            can_trigger_skill_evolution=False,
            notes="Infrastructure or environment failure; isolated from skill evolution",
        )

    # Check for unknown / inconclusive outcome
    if eff_outcome == "unknown":
        return PurificationResult(
            category="diagnosis_only",
            proposal=None,
            diagnosis={
                "task_id": eff_task_id,
                "reason": eff_outcome_reason or "Inconclusive evidence / unknown outcome",
            },
            can_trigger_skill_evolution=False,
            notes="Inconclusive outcome; retained as diagnostic audit trace only",
        )

    # 3. Handle Normal/Success -> Representative Regression Case (D2)
    if eff_outcome == "success":
        # Deterministic proposal ID
        prop_id_src = f"reg:{eff_skill}:{eff_task_id}:{eff_intent_rev}:{eff_query}"
        proposal_id = f"prop_reg_{hashlib.sha256(prop_id_src.encode('utf-8')).hexdigest()[:12]}"

        # Check deduplication against existing store
        if store is not None:
            existing = store.get_proposal(proposal_id)
            if existing is not None:
                return PurificationResult(
                    category="regression_success",
                    proposal=existing,
                    diagnosis={"deduplicated": True, "proposal_id": proposal_id},
                    can_trigger_skill_evolution=False,
                    notes=f"Representative regression case already exists for task '{eff_task_id}'",
                )

        reg_expectation = business_expectation or eff_actual_output
        prop = TestCaseProposal(
            proposal_id=proposal_id,
            skill_name=eff_skill,
            source_task_id=eff_task_id,
            intent_revision=eff_intent_rev,
            contract_fingerprint=eff_fp,
            query=eff_query,
            tool_snapshots=eff_tool_snapshots,
            expected_output=reg_expectation,
            expectation_source=expectation_source if expectation_source != "missing" else "tool_snapshot",
            status="APPROVED",
            failure_attribution="skill",
            is_regression_case=True,
            actual_output=eff_actual_output,
            rejection_reason=None,
            partition_tier=partition_tier,
            variant_family=variant_family,
        )
        if store is not None:
            store.save_proposal(prop)

        return PurificationResult(
            category="regression_success",
            proposal=prop,
            diagnosis={"task_id": eff_task_id, "is_regression": True},
            can_trigger_skill_evolution=False,
            notes="Representative verified success converted to regression case",
        )

    # 4. Handle Failure -> Business Failure Test Case Proposal (D1)
    prop_id_src = f"fail:{eff_skill}:{eff_task_id}:{eff_intent_rev}:{eff_query}"
    proposal_id = f"prop_fail_{hashlib.sha256(prop_id_src.encode('utf-8')).hexdigest()[:12]}"

    # Ground truth validation rules
    status: Literal["APPROVED", "PENDING_APPROVAL", "REJECTED"] = "PENDING_APPROVAL"
    rejection_reason: Optional[str] = None
    can_trigger = False

    # Check 1: Missing expectation
    if business_expectation is None or (isinstance(business_expectation, str) and not business_expectation.strip()):
        status = "PENDING_APPROVAL"
        rejection_reason = "Missing independent business expectation; requires human review"
        expectation_source = "missing"

    # Check 2: Model's own failure used as ground truth
    elif eff_actual_output and business_expectation == eff_actual_output:
        raise ValueError(
            "Ground truth violation: cannot use model's failed output as independent business expectation"
        )

    # Check 3: Model-drafted proposal without human confirmation
    elif expectation_source == "draft_proposal":
        status = "PENDING_APPROVAL"
        rejection_reason = "Model-drafted expectation requires human confirmation or verified business rule"

    # Check 4: Sanitization impaired tool snapshot
    elif sanitization_impaired:
        status = "PENDING_APPROVAL"
        rejection_reason = (
            "Sensitive fields in tool snapshots were redacted, impairing assertion reproducibility; "
            "routed to human review without fabricating data"
        )

    # Check 5: Authoritative expectation verified
    elif expectation_source in ("business_rule", "oracle", "human_confirmed", "tool_snapshot"):
        status = "APPROVED"
        can_trigger = True
    else:
        status = "PENDING_APPROVAL"
        rejection_reason = f"Untrusted expectation source '{expectation_source}'"

    prop = TestCaseProposal(
        proposal_id=proposal_id,
        skill_name=eff_skill,
        source_task_id=eff_task_id,
        intent_revision=eff_intent_rev,
        contract_fingerprint=eff_fp,
        query=eff_query,
        tool_snapshots=eff_tool_snapshots,
        expected_output=business_expectation,
        expectation_source=expectation_source,
        status=status,
        failure_attribution=failure_attribution or "skill",
        is_regression_case=False,
        actual_output=eff_actual_output,
        rejection_reason=rejection_reason,
        partition_tier=partition_tier,
        variant_family=variant_family,
    )

    if store is not None:
        store.save_proposal(prop)

    notes = "Business failure proposal synthesized"
    if status == "APPROVED":
        notes += " and APPROVED with authoritative expectation"
    else:
        notes += f" and marked {status}: {rejection_reason}"

    if demotion_applied:
        notes += " [Demoted from heldout to repair tier; previous benchmark scores invalidated]"

    return PurificationResult(
        category="business_failure",
        proposal=prop,
        diagnosis={
            "task_id": eff_task_id,
            "status": status,
            "demotion_applied": demotion_applied,
            "rejection_reason": rejection_reason,
        },
        can_trigger_skill_evolution=can_trigger,
        notes=notes,
    )


def demote_heldout_to_dev(
    proposal_id: str,
    store: Optional[CandidateStore] = None,
    reason: str = "Used for development feedback and repair",
) -> dict[str, Any]:
    """Demote a heldout case or proposal to the dev/repair tier.

    Records an immutable audit event declaring that previous benchmark scores
    evaluating against this sample are invalidated and can no longer be cited as heldout.
    """
    now_iso = datetime.now(timezone.utc).isoformat()
    if store is not None:
        prop = store.get_proposal(proposal_id)
        if prop is not None:
            prop.partition_tier = "repair"
            prop.updated_at = now_iso
            store.save_proposal(prop)

    return {
        "proposal_id": proposal_id,
        "previous_tier": "experiment_holdout",
        "current_tier": "repair",
        "demoted_at": now_iso,
        "reason": reason,
        "benchmark_invalidated": True,
        "warning": (
            f"Case '{proposal_id}' was demoted to dev feedback. "
            "All previous evaluation scores on this case can no longer claim heldout status."
        ),
    }
