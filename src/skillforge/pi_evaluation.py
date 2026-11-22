"""SkillForge Pi Evaluation Backend.

Provides:
- PiEvaluationBackend: Pluggable execution backend for SkillEvaluator adapting runs
  to the authoritative AgentRuntime and native Pi Runner.
- PiEvaluationRunResult: Encapsulates execution text, objective metrics, real provenances,
  and child run_id.

Guarantees:
- Sole evaluator invariant: SkillEvaluator remains the unique evaluator, oracle judge,
  and ratchet gate; PiEvaluationBackend only provides agent execution strategy.
- Snapshot isolation: Candidate evaluation strictly constructs and passes a candidate
  snapshot (body + meta) from the active registry to Runtime.start_run, never secretly
  reading or overwriting from formal registry.
- Authoritative execution: Mediates all tool calls through ToolBroker; child runs
  are finalized as 'unknown' pending business oracle evaluation in SkillEvaluator.
- Fail-closed metrics: Tokens and money report None when usage is absent or incomplete;
  never fabricates 0 or pseudo-parity.
- Secret sanitization: Non-secret provider/model/parameters are hashed in fingerprint;
  API keys and response scripts are excluded.
"""
from __future__ import annotations

import copy
import hashlib
import json
import os
import sys
import time
import uuid
from dataclasses import dataclass
from typing import Any, Optional

from .models import CandidateSkill, SkillMeta, ToolCallProvenance
from .pi_adapter import PiExecutionResult
from .runtime import AgentRuntime
from .evaluator.llm_factory import LLMLedger


@dataclass
class PiEvaluationRunResult:
    """Encapsulates the output and metadata of an evaluation run via Pi."""

    text: str
    metrics: dict[str, Any]
    provenances: list[ToolCallProvenance]
    run_id: str
    execution_result: Optional[PiExecutionResult] = None


class PiEvaluationBackend:
    """Execution backend adapter for SkillEvaluator using the Pi Agent SDK."""

    def __init__(
        self,
        runtime: AgentRuntime,
        tool_names: Optional[list[str]] = None,
        model_config: Optional[dict[str, Any]] = None,
        test_mode: Optional[bool] = None,
        budget_max: int = 10,
        deadline_seconds: Optional[float] = None,
        timeout: float = 30.0,
        system_prompt_header: str = "",
        ledger: Optional[LLMLedger] = None,
    ) -> None:
        self.runtime = runtime
        self.tool_names = list(tool_names) if tool_names is not None else None
        self.model_config = dict(model_config) if model_config is not None else {"provider": "anthropic", "model_id": "claude-haiku-4-5"}
        self.test_mode = test_mode
        self.budget_max = budget_max
        self.deadline_seconds = deadline_seconds
        self.timeout = timeout
        self.system_prompt_header = system_prompt_header
        self.ledger = ledger

        # Invariant: faux provider and pre-written response scripts restricted to explicit test mode
        is_test = bool(
            test_mode
            or (test_mode is None and (
                os.environ.get("SKILLFORGE_ENV") == "test"
                or "pytest" in sys.modules
                or (self.model_config and (self.model_config.get("is_test") or self.model_config.get("test_mode")))
            ))
        )
        provider = self.model_config.get("provider", "anthropic")
        if provider == "faux" and not is_test:
            raise ValueError("Faux provider is restricted to explicit test mode (test_mode=True)")
        if "responses" in self.model_config and not is_test:
            raise ValueError("Pre-written response scripts are restricted to explicit test mode (test_mode=True)")

    def bind_ledger(self, ledger: Optional[LLMLedger]) -> None:
        """Bind an optional shared LLMLedger for budget accounting across evaluation runs."""
        self.ledger = ledger

    def get_config_fingerprint(self) -> str:
        """Deterministic SHA-256 fingerprint of backend identity and public model config.

        Strictly excludes secrets, api keys, and pre-written responses scripts.
        """
        mc = self.model_config or {}
        clean_mc = {
            "provider": str(mc.get("provider", "anthropic")),
            "model_id": str(mc.get("model_id") or mc.get("model", "")),
            "temperature": float(mc.get("temperature", 0.0) or 0.0),
            "max_tokens": mc.get("max_tokens"),
            "api": mc.get("api"),
            "base_url": mc.get("base_url"),
            "reasoning": mc.get("reasoning"),
        }
        payload = {
            "backend": "pi_evaluation",
            "model_config": clean_mc,
            "tools": sorted(self.tool_names) if self.tool_names is not None else None,
            "budget_max": int(self.budget_max),
            "test_mode": bool(self.test_mode),
            "deadline_seconds": self.deadline_seconds,
            "timeout": self.timeout,
            "system_prompt_header": self.system_prompt_header,
        }
        raw = json.dumps(payload, sort_keys=True)
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]

    def run(
        self,
        query: str,
        body: str = "",
        meta: Optional[SkillMeta] = None,
        skill_name: Optional[str] = None,
    ) -> PiEvaluationRunResult:
        """Execute a single query in an isolated run via Pi Agent runner."""
        child_run_id = f"eval_pi_{'skill' if body else 'bare'}_{uuid.uuid4().hex[:12]}"
        deadline_ts = (time.time() + self.deadline_seconds) if self.deadline_seconds else None

        candidate_snapshot: Optional[CandidateSkill] = None
        if body:
            eff_name = skill_name or (meta.name if meta else "eval_skill")
            eff_meta = meta or SkillMeta(
                name=eff_name,
                version="0.1.0-eval",
                dependencies=list(self.tool_names or []),
            )
            candidate_snapshot = CandidateSkill(
                candidate_id=f"snap_{child_run_id}",
                skill_name=eff_name,
                decision="revise",
                body=body,
                meta=eff_meta,
                source_requirement="Evaluation candidate snapshot",
            )
            self.runtime.start_run(
                run_id=child_run_id,
                task_id=f"eval_case_{child_run_id}",
                skill_name=eff_name,
                candidate=candidate_snapshot,
                purpose="evaluation",
                budget_max=self.budget_max,
                deadline_ts=deadline_ts,
            )
        else:
            self.runtime.start_run(
                run_id=child_run_id,
                task_id=f"eval_case_{child_run_id}",
                skill_name=None,
                candidate=None,
                purpose="evaluation",
                budget_max=self.budget_max,
                deadline_ts=deadline_ts,
            )

        model_cfg = copy.deepcopy(self.model_config)
        if self.test_mode and "responses" in model_cfg and isinstance(model_cfg["responses"], list):
            run_suffix = child_run_id[-8:]
            for resp in model_cfg["responses"]:
                if isinstance(resp, dict):
                    for blk in resp.get("content", []):
                        if isinstance(blk, dict) and blk.get("type") == "toolCall" and "id" in blk:
                            blk["id"] = f"{blk['id']}_{run_suffix}"

        # Per-run local reserved ids run_id:turn_index map
        reserved_call_ids: dict[tuple[str, int], str] = {}

        def admission_callback(r_id: str, t_idx: int) -> dict[str, Any]:
            if not self.ledger:
                return {"allowed": True}
            if r_id != child_run_id:
                return {
                    "allowed": False,
                    "error_type": "CROSS_RUN_MISMATCH",
                    "error": f"Cross-run turn admission rejected: {r_id} != {child_run_id}",
                }
            key = (r_id, t_idx)
            if key not in reserved_call_ids:
                cid = f"{r_id}:{t_idx}"
                # Admission reserves one existing LLMLedger call BEFORE provider
                self.ledger.reserve_turn(role="agent", call_id=cid)
                reserved_call_ids[key] = cid
            return {"allowed": True}

        def receipt_callback(frame: dict[str, Any]) -> None:
            if not self.ledger:
                return
            r_id = frame.get("run_id")
            t_idx = frame.get("turn_index")
            if r_id != child_run_id or t_idx is None:
                # duplicate/cross-run receipt cannot reserve new call or update other record
                return
            key = (r_id, t_idx)
            cid = reserved_call_ids.pop(key, None)
            if not cid:
                # Either already settled (duplicate receipt) or never reserved
                return
            usage = frame.get("usage")
            status = frame.get("status", "success")
            error_type = frame.get("error")
            # Receipt settles that same id with real usage/status (no double count)
            self.ledger.settle_turn(
                call_id=cid,
                usage=usage,
                status=status,
                error_type=error_type,
            )

        t0 = time.perf_counter()
        try:
            text = self.runtime.run_agent_with_pi(
                run_id=child_run_id,
                input_text=query,
                candidate=candidate_snapshot,
                tools=self.tool_names,
                system_prompt_header=self.system_prompt_header,
                model_config=model_cfg,
                test_mode=self.test_mode,
                timeout=self.timeout,
                model_turn_admission=admission_callback if self.ledger else None,
                model_turn_receipt=receipt_callback if self.ledger else None,
            )
        finally:
            if self.ledger and reserved_call_ids:
                # End/error cleanup mark ONLY this run's still pending ids error unknown; totals retain failed attempt
                for cid in list(reserved_call_ids.values()):
                    self.ledger.cancel_pending(call_id=cid, error_type="RUN_TERMINATED_UNKNOWN")
                reserved_call_ids.clear()

        # Afterrun check_post_run_budget ensures last response over token/deadline cannot return valid
        if self.ledger:
            self.ledger.check_post_run_budget()
        latency_ms = (time.perf_counter() - t0) * 1000

        run_rec = self.runtime.get_run(child_run_id)
        if run_rec and run_rec.status in ("FAILED", "TIMED_OUT", "BUDGET_EXHAUSTED", "CANCELLED", "INTERRUPTED"):
            raise RuntimeError(f"Pi evaluation run failed with status '{run_rec.status}': {run_rec.error_message}")

        provs = self.runtime.get_provenances(child_run_id)
        pi_res = self.runtime._last_pi_results.get(child_run_id)

        # Finalize child run with unknown outcome (business oracle happens in Evaluator)
        self.runtime.finalize_run(
            run_id=child_run_id,
            model_output=text,
            verification_evidence={
                "independent_pass": None,
                "reason": "child evaluation run completed, business oracle pending",
            },
        )

        # Extract turns from assistant message count
        turns = 1.0
        if pi_res and pi_res.messages:
            asst_count = sum(1 for m in pi_res.messages if m.get("role") == "assistant")
            if asst_count > 0:
                turns = float(asst_count)
        elif pi_res and pi_res.captured_provider_inputs:
            turns = float(len(pi_res.captured_provider_inputs))

        # Token usage aggregation: aggregate across all assistant messages; if any is missing, None
        tokens: Optional[float] = None
        if pi_res and pi_res.messages:
            if pi_res.usage is None or any(m.get("_missingUsage") for m in pi_res.messages):
                tokens = None
            else:
                assistant_msgs = [m for m in pi_res.messages if m.get("role") == "assistant"]
                if assistant_msgs:
                    all_have_usage = True
                    acc_tokens = 0
                    for m in assistant_msgs:
                        u = m.get("usage")
                        if not u or not isinstance(u, dict):
                            all_have_usage = False
                            break
                        tok = u.get("totalTokens", u.get("total_tokens"))
                        if tok is None:
                            in_tok = u.get("input_tokens", u.get("input", u.get("prompt_tokens")))
                            out_tok = u.get("output_tokens", u.get("output", u.get("completion_tokens")))
                            if in_tok is not None and out_tok is not None:
                                tok = in_tok + out_tok
                        if tok is None:
                            all_have_usage = False
                            break
                        acc_tokens += int(tok)
                    if all_have_usage:
                        tokens = float(acc_tokens)

        metrics = {
            "turns": turns,
            "tokens": tokens,
            "latency_ms": latency_ms,
            "tool_calls": float(len(provs)),
            "money": None,
        }

        return PiEvaluationRunResult(
            text=text,
            metrics=metrics,
            provenances=provs,
            run_id=child_run_id,
            execution_result=pi_res,
        )
