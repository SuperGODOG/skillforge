"""Actionable Diagnostics and Narrow Local Repair Module

Inspired by Archify's operable diagnostics pattern:
- Generates JSON-serializable ValidationReceipt with stable rule_code, path, expected/actual,
  responsibility layer, retryable flag, supported_fixes, and deterministic fingerprints.
- Enforces strict content fingerprint and validator config binding.
- Enables narrow-domain local repair bounded by application CorrectionPolicy (max 2 attempts).
- Respects AgentRuntime lifecycle, budget, timeout, and cancellation guards.
- Strictly separates single artifact repair from long-term Skill evolution.
"""
from __future__ import annotations

import copy
import hashlib
import json
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable, Literal, Optional

ResponsibilityLayer = Literal["skill", "tool", "policy", "evaluator", "unknown"]
FixCategory = Literal["structural", "semantic", "environmental", "unfixable"]


def compute_artifact_fingerprint(content: Any) -> str:
    """Compute deterministic SHA-256 fingerprint of artifact content."""
    if isinstance(content, (dict, list)):
        payload = json.dumps(content, sort_keys=True, ensure_ascii=False)
    else:
        payload = str(content)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


@dataclass
class ValidationDiagnostic:
    """Individual actionable diagnostic issue."""

    rule_code: str
    subject: str
    expected: Any
    actual: Any
    evidence: str
    responsibility_layer: ResponsibilityLayer = "tool"
    retryable: bool = True
    supported_fixes: list[dict[str, Any]] = field(default_factory=list)
    fix_category: FixCategory = "structural"

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class ValidationReceipt:
    """JSON-serializable receipt certifying verification outcome and diagnostics."""

    receipt_id: str
    status: Literal["PASS", "FAIL"]
    diagnostics: list[ValidationDiagnostic]
    content_fingerprint: str
    validator_id: str
    validator_version: str
    validator_config_hash: str
    task_id: str = ""
    run_id: str = ""
    skill_version: Optional[str] = None
    created_at: str = field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )

    def to_dict(self) -> dict[str, Any]:
        return {
            "receipt_id": self.receipt_id,
            "status": self.status,
            "diagnostics": [d.to_dict() for d in self.diagnostics],
            "content_fingerprint": self.content_fingerprint,
            "validator_id": self.validator_id,
            "validator_version": self.validator_version,
            "validator_config_hash": self.validator_config_hash,
            "task_id": self.task_id,
            "run_id": self.run_id,
            "skill_version": self.skill_version,
            "created_at": self.created_at,
        }

    def is_valid_for(self, content: Any, validator: Any) -> bool:
        """Verify that this receipt faithfully matches the exact content and validator."""
        eff_fp = compute_artifact_fingerprint(content)
        if self.content_fingerprint != eff_fp:
            return False
        if getattr(validator, "validator_id", None) != self.validator_id:
            return False
        if getattr(validator, "validator_version", None) != self.validator_version:
            return False
        if getattr(validator, "config_hash", None) != self.validator_config_hash:
            return False
        return True


class JsonConfigValidator:
    """Controlled artifact validator for structured JSON configurations.

    Enforces fixed ordering:
    1. Structural issues first (missing required fields, type/range violations).
    2. Semantic cross-field constraints second (e.g. timeout_seconds >= retry_seconds).
    """

    def __init__(
        self,
        validator_id: str = "json_config_validator",
        validator_version: str = "1.0.0",
        required_fields: Optional[list[str]] = None,
        min_workers: int = 1,
        max_workers: int = 64,
        min_timeout: int = 1,
        max_timeout: int = 3600,
        min_retry: int = 0,
        max_retry: int = 300,
    ):
        self.validator_id = validator_id
        self.validator_version = validator_version
        self.required_fields = (
            list(required_fields)
            if required_fields is not None
            else ["name", "workers", "timeout_seconds", "retry_seconds"]
        )
        self.min_workers = min_workers
        self.max_workers = max_workers
        self.min_timeout = min_timeout
        self.max_timeout = max_timeout
        self.min_retry = min_retry
        self.max_retry = max_retry

    @property
    def config_hash(self) -> str:
        """Deterministic fingerprint of validator configuration."""
        cfg = {
            "required_fields": sorted(self.required_fields),
            "min_workers": self.min_workers,
            "max_workers": self.max_workers,
            "min_timeout": self.min_timeout,
            "max_timeout": self.max_timeout,
            "min_retry": self.min_retry,
            "max_retry": self.max_retry,
        }
        return hashlib.sha256(
            json.dumps(cfg, sort_keys=True).encode("utf-8")
        ).hexdigest()

    def validate(
        self,
        content: Any,
        task_id: str = "",
        run_id: str = "",
        skill_version: Optional[str] = None,
    ) -> ValidationReceipt:
        """Execute validation against structured JSON content."""
        content_fp = compute_artifact_fingerprint(content)
        receipt_id = f"rcpt_{hashlib.sha256(f'{run_id}_{content_fp}'.encode('utf-8')).hexdigest()[:12]}"
        now_iso = datetime.now(timezone.utc).isoformat()

        diagnostics: list[ValidationDiagnostic] = []

        # Environmental / Infrastructure sentinel check: permissions, sandbox, or dependency failure
        env_error_type = None
        env_error_msg = ""
        if hasattr(content, "status") and getattr(content, "status") == "REJECTED":
            env_error_type = getattr(content, "error_type", None) or "REJECTED"
            env_error_msg = getattr(content, "error_message", None) or "Tool call was rejected by broker"
        elif isinstance(content, dict):
            if (
                content.get("status") == "REJECTED"
                or content.get("error_type") in ("PERMISSION_DENIED", "SANDBOX_UNAVAILABLE", "DEPENDENCY_MISSING")
                or content.get("_permission_denied")
            ):
                env_error_type = content.get("error_type") or ("PERMISSION_DENIED" if content.get("_permission_denied") else "REJECTED")
                env_error_msg = content.get("error_message") or "Tool call rejected due to permissions or environment"

        if env_error_type:
            diagnostics.append(
                ValidationDiagnostic(
                    rule_code=env_error_type,
                    subject="$.environment",
                    expected="authorized and available",
                    actual=env_error_type,
                    evidence=env_error_msg,
                    responsibility_layer="policy",
                    retryable=False,
                    supported_fixes=[],
                    fix_category="environmental",
                )
            )
            return ValidationReceipt(
                receipt_id=receipt_id,
                status="FAIL",
                diagnostics=diagnostics,
                content_fingerprint=content_fp,
                validator_id=self.validator_id,
                validator_version=self.validator_version,
                validator_config_hash=self.config_hash,
                task_id=task_id,
                run_id=run_id,
                skill_version=skill_version,
                created_at=now_iso,
            )

        if not isinstance(content, dict):
            diagnostics.append(
                ValidationDiagnostic(
                    rule_code="INVALID_PAYLOAD_TYPE",
                    subject="$",
                    expected="dict",
                    actual=type(content).__name__,
                    evidence="Content must be a JSON dictionary",
                    responsibility_layer="tool",
                    retryable=False,
                    supported_fixes=[],
                    fix_category="unfixable",
                )
            )
            return ValidationReceipt(
                receipt_id=receipt_id,
                status="FAIL",
                diagnostics=diagnostics,
                content_fingerprint=content_fp,
                validator_id=self.validator_id,
                validator_version=self.validator_version,
                validator_config_hash=self.config_hash,
                task_id=task_id,
                run_id=run_id,
                skill_version=skill_version,
                created_at=now_iso,
            )

        # 1. Structural Checks First
        for req in self.required_fields:
            if req not in content or content[req] is None:
                diagnostics.append(
                    ValidationDiagnostic(
                        rule_code="MISSING_REQUIRED_FIELD",
                        subject=f"$.{req}",
                        expected=f"present: {req}",
                        actual="missing",
                        evidence=f"Required field '{req}' is missing from JSON payload",
                        responsibility_layer="tool",
                        retryable=True,
                        supported_fixes=[
                            {
                                "path": f"$.{req}",
                                "op": "set",
                                "rule_code": "MISSING_REQUIRED_FIELD",
                            }
                        ],
                        fix_category="structural",
                    )
                )

        if "workers" in content and content["workers"] is not None:
            w = content["workers"]
            if not isinstance(w, int):
                diagnostics.append(
                    ValidationDiagnostic(
                        rule_code="INVALID_FIELD_TYPE",
                        subject="$.workers",
                        expected="integer",
                        actual=type(w).__name__,
                        evidence=f"Field 'workers' must be integer, got {type(w).__name__}",
                        responsibility_layer="tool",
                        retryable=True,
                        supported_fixes=[
                            {
                                "path": "$.workers",
                                "op": "set",
                                "rule_code": "INVALID_FIELD_TYPE",
                            }
                        ],
                        fix_category="structural",
                    )
                )
            elif not (self.min_workers <= w <= self.max_workers):
                diagnostics.append(
                    ValidationDiagnostic(
                        rule_code="VALUE_OUT_OF_RANGE",
                        subject="$.workers",
                        expected=f"[{self.min_workers}, {self.max_workers}]",
                        actual=w,
                        evidence=f"Field 'workers' value {w} out of allowed bounds [{self.min_workers}, {self.max_workers}]",
                        responsibility_layer="tool",
                        retryable=True,
                        supported_fixes=[
                            {
                                "path": "$.workers",
                                "op": "set",
                                "rule_code": "VALUE_OUT_OF_RANGE",
                            }
                        ],
                        fix_category="structural",
                    )
                )

        if "timeout_seconds" in content and content["timeout_seconds"] is not None:
            t = content["timeout_seconds"]
            if not isinstance(t, int) or not (self.min_timeout <= t <= self.max_timeout):
                diagnostics.append(
                    ValidationDiagnostic(
                        rule_code="VALUE_OUT_OF_RANGE",
                        subject="$.timeout_seconds",
                        expected=f"[{self.min_timeout}, {self.max_timeout}]",
                        actual=t,
                        evidence=f"Field 'timeout_seconds' value {t} out of allowed bounds [{self.min_timeout}, {self.max_timeout}]",
                        responsibility_layer="tool",
                        retryable=True,
                        supported_fixes=[
                            {
                                "path": "$.timeout_seconds",
                                "op": "set",
                                "rule_code": "VALUE_OUT_OF_RANGE",
                            }
                        ],
                        fix_category="structural",
                    )
                )

        if "retry_seconds" in content and content["retry_seconds"] is not None:
            r = content["retry_seconds"]
            if not isinstance(r, int) or not (self.min_retry <= r <= self.max_retry):
                diagnostics.append(
                    ValidationDiagnostic(
                        rule_code="VALUE_OUT_OF_RANGE",
                        subject="$.retry_seconds",
                        expected=f"[{self.min_retry}, {self.max_retry}]",
                        actual=r,
                        evidence=f"Field 'retry_seconds' value {r} out of allowed bounds [{self.min_retry}, {self.max_retry}]",
                        responsibility_layer="tool",
                        retryable=True,
                        supported_fixes=[
                            {
                                "path": "$.retry_seconds",
                                "op": "set",
                                "rule_code": "VALUE_OUT_OF_RANGE",
                            }
                        ],
                        fix_category="structural",
                    )
                )

        # 2. Semantic Cross-field Constraint Second (timeout_seconds >= retry_seconds)
        t_val = content.get("timeout_seconds")
        r_val = content.get("retry_seconds")
        if (
            isinstance(t_val, int)
            and isinstance(r_val, int)
            and t_val < r_val
        ):
            diagnostics.append(
                ValidationDiagnostic(
                    rule_code="CROSS_FIELD_CONSTRAINT",
                    subject="$.timeout_seconds",
                    expected=f">= retry_seconds ({r_val})",
                    actual=t_val,
                    evidence=f"Cross-field violation: timeout_seconds ({t_val}) must be >= retry_seconds ({r_val})",
                    responsibility_layer="tool",
                    retryable=True,
                    supported_fixes=[
                        {
                            "path": "$.timeout_seconds",
                            "op": "set",
                            "rule_code": "CROSS_FIELD_CONSTRAINT",
                        }
                    ],
                    fix_category="semantic",
                )
            )

        status = "PASS" if not diagnostics else "FAIL"
        return ValidationReceipt(
            receipt_id=receipt_id,
            status=status,
            diagnostics=diagnostics,
            content_fingerprint=content_fp,
            validator_id=self.validator_id,
            validator_version=self.validator_version,
            validator_config_hash=self.config_hash,
            task_id=task_id,
            run_id=run_id,
            skill_version=skill_version,
            created_at=now_iso,
        )


@dataclass
class FixAction:
    """Deterministic, actionable modification to a JSON artifact."""

    path: str
    op: Literal["set", "replace"]
    value: Any
    rule_code: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class CorrectionPolicy:
    """Application-defined governance policy for narrow artifact repairs."""

    enabled: bool = True
    max_corrections: int = 2
    allowed_paths: set[str] = field(
        default_factory=lambda: {
            "$.name",
            "$.workers",
            "$.timeout_seconds",
            "$.retry_seconds",
        }
    )
    allowed_ops: set[str] = field(default_factory=lambda: {"set", "replace"})


class DeterministicJsonFixer:
    """Fixture/Rule-based fixer proposing narrow local fixes in fixed order."""

    def __init__(
        self,
        default_name: str = "default_service",
        default_workers: int = 4,
        default_timeout: int = 60,
        default_retry: int = 10,
        custom_action_provider: Optional[Callable[[ValidationReceipt, dict[str, Any]], list[FixAction]]] = None,
    ):
        self.default_name = default_name
        self.default_workers = default_workers
        self.default_timeout = default_timeout
        self.default_retry = default_retry
        self.custom_action_provider = custom_action_provider
        self.call_count = 0

    def suggest_fixes(
        self,
        receipt: ValidationReceipt,
        current_content: dict[str, Any],
    ) -> list[FixAction]:
        """Suggest candidate fix actions strictly adhering to diagnostic ordering."""
        self.call_count += 1
        if self.custom_action_provider:
            return self.custom_action_provider(receipt, current_content)

        # Sort diagnostics: structural first, then semantic
        sorted_diags = sorted(
            receipt.diagnostics,
            key=lambda d: 0 if d.fix_category == "structural" else (1 if d.fix_category == "semantic" else 2),
        )

        actions: list[FixAction] = []
        for d in sorted_diags:
            if not d.retryable or not d.supported_fixes or d.responsibility_layer != "tool":
                continue

            if d.rule_code == "MISSING_REQUIRED_FIELD":
                field_name = d.subject.lstrip("$.")
                val: Any = self.default_name
                if field_name == "workers":
                    val = self.default_workers
                elif field_name == "timeout_seconds":
                    val = self.default_timeout
                elif field_name == "retry_seconds":
                    val = self.default_retry
                actions.append(FixAction(path=d.subject, op="set", value=val, rule_code=d.rule_code))
                break  # Address first missing structural element per attempt

            elif d.rule_code == "VALUE_OUT_OF_RANGE":
                field_name = d.subject.lstrip("$.")
                val = current_content.get(field_name)
                if field_name == "workers":
                    val = self.default_workers
                elif field_name == "timeout_seconds":
                    val = self.default_timeout
                elif field_name == "retry_seconds":
                    val = self.default_retry
                actions.append(FixAction(path=d.subject, op="set", value=val, rule_code=d.rule_code))
                break

            elif d.rule_code == "INVALID_FIELD_TYPE":
                field_name = d.subject.lstrip("$.")
                val = self.default_workers if field_name == "workers" else 0
                actions.append(FixAction(path=d.subject, op="set", value=val, rule_code=d.rule_code))
                break

            elif d.rule_code == "CROSS_FIELD_CONSTRAINT":
                # Ensure timeout_seconds >= retry_seconds
                r_val = current_content.get("retry_seconds", self.default_retry)
                actions.append(
                    FixAction(
                        path="$.timeout_seconds",
                        op="set",
                        value=r_val * 2 if r_val > 0 else 60,
                        rule_code=d.rule_code,
                    )
                )
                break

        return actions


def apply_action_to_content(
    content: dict[str, Any],
    action: FixAction,
) -> dict[str, Any]:
    """Safely apply a validated FixAction to target JSON structure."""
    new_content = copy.deepcopy(content)
    if action.path.startswith("$."):
        key = action.path[2:]
        if action.op in ("set", "replace"):
            new_content[key] = action.value
    return new_content


def get_default_ab_cases() -> list[dict[str, Any]]:
    """Return standard deterministic test cases for offline controlled A/B comparison."""
    return [
        {
            "id": "case_1_missing_field",
            "name": "Missing required field 'name'",
            "category": "fixable_structural",
            "initial_content": {"workers": 4, "timeout_seconds": 60, "retry_seconds": 10},
        },
        {
            "id": "case_2_invalid_range",
            "name": "Workers out of range (0 < min 1)",
            "category": "fixable_structural",
            "initial_content": {"name": "svc_test", "workers": 0, "timeout_seconds": 60, "retry_seconds": 10},
        },
        {
            "id": "case_3_cross_field",
            "name": "Cross-field constraint violation (timeout 10 < retry 30)",
            "category": "fixable_semantic",
            "initial_content": {"name": "svc_test", "workers": 4, "timeout_seconds": 10, "retry_seconds": 30},
        },
        {
            "id": "case_4_sequential_both",
            "name": "Missing field AND cross-field violation",
            "category": "fixable_sequential",
            "initial_content": {"workers": 4, "timeout_seconds": 10, "retry_seconds": 30},
        },
        {
            "id": "case_5_unfixable_rule",
            "name": "Unfixable payload type",
            "category": "unfixable",
            "initial_content": "NOT_A_DICT_STRING",
        },
        {
            "id": "case_6_policy_denied",
            "name": "Environmental policy/permission rejection",
            "category": "environmental_policy",
            "initial_content": {
                "name": "svc_test",
                "workers": 4,
                "timeout_seconds": 60,
                "retry_seconds": 10,
                "_permission_denied": True,
                "error_message": "Access denied by security policy",
            },
        },
        {
            "id": "case_7_no_progress",
            "name": "Fixer makes no progress",
            "category": "no_progress",
            "initial_content": {"name": "svc_test", "workers": -1, "timeout_seconds": 60, "retry_seconds": 10},
            "custom_fixer": DeterministicJsonFixer(
                custom_action_provider=lambda r, c: [
                    FixAction(path="$.workers", op="set", value=-1, rule_code="NO_OP")
                ]
            ),
        },
        {
            "id": "case_8_out_of_bounds",
            "name": "Fixer attempts out-of-bounds mutation",
            "category": "out_of_bounds",
            "initial_content": {"name": "svc_test", "workers": 0, "timeout_seconds": 60, "retry_seconds": 10},
            "custom_fixer": DeterministicJsonFixer(
                custom_action_provider=lambda r, c: [
                    FixAction(path="$.unauthorized_privilege", op="set", value="root", rule_code="PRIVILEGE_ESCALATION")
                ]
            ),
        },
    ]


def run_artifact_ab_comparison(
    runtime_factory: Callable[[], Any],
    cases: Optional[list[dict[str, Any]]] = None,
) -> dict[str, Any]:
    """Execute offline controlled A/B comparison across identical inputs.

    Group A: Original flow (correction disabled).
    Group B: Receipt + narrow local repair enabled.

    Counts raw observable metrics without fabricating LLM cost or ROI figures.
    """
    test_cases = cases if cases is not None else get_default_ab_cases()
    validator = JsonConfigValidator()

    def _run_group(enabled: bool) -> dict[str, Any]:
        runtime = runtime_factory()
        policy = CorrectionPolicy(enabled=enabled, max_corrections=2)
        start_t = time.perf_counter()

        final_pass_count = 0
        false_positive_count = 0
        validator_call_count = 0
        fixer_call_count = 0
        no_progress_count = 0
        human_intervention_terminal_count = 0

        case_results = []

        for idx, case in enumerate(test_cases):
            run_id = f"run_{'grp_b' if enabled else 'grp_a'}_{idx:02d}"
            task_id = case["id"]
            runtime.start_run(run_id=run_id, task_id=task_id, purpose="evaluation")

            initial_content = case["initial_content"]
            fixer = case.get("custom_fixer") or DeterministicJsonFixer()

            init_fixer_calls = fixer.call_count
            final_content, final_rcpt, history, stop_reason = runtime.repair_artifact(
                run_id=run_id,
                initial_content=initial_content,
                validator=validator,
                fixer=fixer,
                correction_policy=policy,
            )
            # Count validator calls: 1 initial + each history attempt
            v_calls = 1 + len(history)
            validator_call_count += v_calls
            f_calls = fixer.call_count - init_fixer_calls
            fixer_call_count += f_calls

            if stop_reason == "NO_PROGRESS":
                no_progress_count += 1

            is_pass = final_rcpt.status == "PASS"
            if is_pass:
                # Independent strict verification against truth
                if not validator.validate(final_content).status == "PASS":
                    false_positive_count += 1
                else:
                    final_pass_count += 1
            else:
                human_intervention_terminal_count += 1

            runtime.finalize_run(
                run_id=run_id,
                verification_evidence={
                    "independent_pass": is_pass,
                    "receipt": final_rcpt.to_dict(),
                    "content": final_content,
                },
            )

            case_results.append({
                "case_id": case["id"],
                "category": case["category"],
                "status": final_rcpt.status,
                "stop_reason": stop_reason,
                "corrections": len(history),
            })

        elapsed = time.perf_counter() - start_t
        return {
            "group": "Group B (Receipt + Narrow Repair)" if enabled else "Group A (Original Baseline)",
            "correction_enabled": enabled,
            "total_cases": len(test_cases),
            "final_pass_count": final_pass_count,
            "false_positive_count": false_positive_count,
            "validator_call_count": validator_call_count,
            "fixer_call_count": fixer_call_count,
            "no_progress_count": no_progress_count,
            "human_intervention_terminal_count": human_intervention_terminal_count,
            "elapsed_seconds": round(elapsed, 4),
            "token_count": None,  # Offline deterministic fixture; no remote LLM tokens consumed
            "cost_usd": None,     # Offline deterministic fixture; cost is null
            "case_results": case_results,
        }

    group_a = _run_group(enabled=False)
    group_b = _run_group(enabled=True)

    return {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "group_a_baseline": group_a,
        "group_b_narrow_repair": group_b,
        "delta": {
            "pass_increase": group_b["final_pass_count"] - group_a["final_pass_count"],
            "false_positive_delta": group_b["false_positive_count"] - group_a["false_positive_count"],
            "fixer_calls_spent": group_b["fixer_call_count"],
            "validator_calls_delta": group_b["validator_call_count"] - group_a["validator_call_count"],
        },
        "disclaimer": (
            "Mechanical gains in fixture benchmarks do NOT establish real-world user ROI or LLM token savings. "
            "Token/cost figures are strictly reported as null."
        ),
    }

