"""Skill / Route / Evaluation / Release 数据模型

- SkillMeta 系列：Pydantic BaseModel（需要 YAML frontmatter 解析 + 校验）
- Route/Eval/Ratchet/Patch/Release：dataclass（组件间内部传递够用）

参见 ARCHITECTURE §5。
"""
from __future__ import annotations
import time
from dataclasses import dataclass, field
from typing import Any, Literal, Optional
from pydantic import BaseModel, Field



class Trigger(BaseModel):
    keywords: list[str] = Field(default_factory=list)


class Evaluation(BaseModel):
    last_score: Optional[float] = None
    last_release_id: Optional[str] = None


class SkillMeta(BaseModel):
    """SKILL.md 的 YAML frontmatter 结构"""
    name: str
    version: str
    description: str
    use_when: str
    not_for: list[str] = Field(default_factory=list)
    dependencies: list[str] = Field(default_factory=list)
    trigger: Trigger = Field(default_factory=Trigger)
    examples: list[str] = Field(default_factory=list)
    evaluation: Evaluation = Field(default_factory=Evaluation)


@dataclass
class RouteResult:
    chosen: Optional[str]
    hit_layer: Literal["rule", "embed", "llm"]
    scores: dict
    latency_ms: float
    matched_keywords: list[str] = field(default_factory=list)
    routing_notes: str = ""


@dataclass(frozen=True)
class ToolCallProvenance:
    """Integrity record of a dependency fixture invocation and its response snapshot."""

    tool_name: str
    fixture_case_id: str
    call_index: int
    call_count: int
    is_fixture: bool
    tool_required: bool
    tool_called: bool
    tool_success: bool
    authenticity_pass: bool
    input_params: dict[str, Any]
    output_status: Literal["SUCCESS", "ERROR", "CIRCUIT_OPEN"]
    output_summary: str
    latency_ms: float
    timestamp: str
    signature: str
    # The Judge needs the actual immutable response bytes, not only a display hash.
    # Defaults preserve compatibility with hand-built non-fixture provenance records.
    snapshot_id: str = ""
    snapshot_content: str = ""


@dataclass
class EvalResult:
    release_id: str
    structure_score: dict[str, float]
    effect_score: dict[str, float]
    objective_metrics: dict[str, float]
    p0_pass: bool
    # Phase 4 元 Agent 需要每 case 明细定位失败样本
    case_verdicts: list[dict] = field(default_factory=list)
    # 保留 skill/baseline 输出对，供元 Agent 归因（可选，大数据量）
    case_outputs: list[dict] = field(default_factory=list)
    # P0-B: 实际执行过的验证通道及工具调用凭证。
    validation_channels: list[str] = field(default_factory=list)
    provenances: list[ToolCallProvenance] = field(default_factory=list)
    # P0-C: Judge/infrastructure invalidity must never masquerade as a score.
    valid: bool = True
    invalid_reasons: list[str] = field(default_factory=list)
    p0_gate_result: Optional[Any] = None
    # P0-1 路由判定链真实字段；等级字段属于 Patch/diff，不属于 Router。
    hit_layer: Optional[str] = None
    verdict: Optional[str] = None
    matched_keywords: list[str] = field(default_factory=list)
    routing_notes: Optional[str] = None
    route_result: Optional[RouteResult] = None
    route_error: Optional[str] = None


@dataclass
class RatchetVerdict:
    decision: Literal["PASS", "REVIEW", "DECLINED"]
    reasons: list[str] = field(default_factory=list)


@dataclass
class ValidationRecord:
    """Evaluation gate record binding candidate identity, content hash, and ratchet verdict."""

    candidate_id: str
    content_hash: str
    baseline_version: Optional[str] = None
    ratchet_decision: Literal["PASS", "REVIEW", "DECLINED"] = "DECLINED"
    eval_result: Optional[EvalResult] = None
    ratchet_verdict: Optional[RatchetVerdict] = None
    promoted: bool = False
    release_id: Optional[str] = None
    verification_episode_ids: list[str] = field(default_factory=list)
    record_id: Optional[str] = None
    scope_hash: Optional[str] = None
    config_hash: Optional[str] = None
    dataset_version: Optional[str] = None
    created_at: Optional[str] = None
    updated_at: Optional[str] = None


PatchStatus = Literal["PUBLISHED", "REVIEW", "SUGGESTION", "DECLINED", "PASS"]


@dataclass
class BodySectionStats:
    """Four-part body section character counts and total length."""

    overview: int = 0
    instructions: int = 0
    examples: int = 0
    constraints: int = 0
    total: int = 0
    by_section: dict[str, int] = field(default_factory=dict)

    def to_dict(self) -> dict[str, int]:
        d = {
            "Overview": self.overview,
            "Instructions": self.instructions,
            "Examples": self.examples,
            "Constraints": self.constraints,
            "total": self.total,
        }
        d.update(self.by_section)
        return d


@dataclass
class Patch:
    skill_name: str
    level: Literal["L1", "L2", "L3"]
    diff: str
    rationale: str
    # ``diff`` is the complete candidate SKILL.md for backward compatibility.
    # These fields carry the independently computed audit decision.
    computed_level: Literal["L1", "L2", "L3", "INVALID"] = "INVALID"
    unified_diff: str = ""
    downgrade_attempt: bool = False
    changed_frontmatter: list[str] = field(default_factory=list)
    changed_body_sections: list[str] = field(default_factory=list)
    provenances: list[ToolCallProvenance] = field(default_factory=list)
    # P1-E Prompt Bloat 护栏字段
    baseline_body_stats: dict[str, int] = field(default_factory=dict)
    candidate_body_stats: dict[str, int] = field(default_factory=dict)
    bloat_verdict: Optional[str] = None
    bloat_reasons: list[str] = field(default_factory=list)
    distillation_prompt: Optional[str] = None
    status: Optional[str] = None


@dataclass
class AttemptRecord:
    """Record of an evolution candidate attempt in an evolution round."""

    attempt_no: int
    strategy: str
    candidate_digest: str
    computed_level: str
    verdict: str
    reason_codes: list[str] = field(default_factory=list)
    calls: Optional[int] = None  # 接入 ledger 真实统计（未接入时明确标注为 None 预留字段，禁止默认 0 装真值）
    tokens: Optional[int] = None # 接入 ledger 真实统计（未接入时明确标注为 None 预留字段，禁止默认 0 装真值）
    round_no: int = 1
    status: Optional[str] = None
    patch: Optional[Patch] = None


@dataclass
class AttemptFeedback:
    """Strongly-typed feedback object passed into reflection per codex2 §4.2."""

    attempt_no: int
    original_skill_digest: str
    candidate_digest: str
    declared_level: str
    computed_level: str
    strategy: str
    candidate_diff: str
    failed_cases: list[dict[str, Any]]
    ratchet_reasons: list[str]
    p0_status: Any
    authenticity_status: Any
    prompt_budget_status: Any
    repeated_patch_fingerprints: list[str]
    remaining_budget: Any


@dataclass
class EvolveContext:
    """Runtime blackboard tracking evolution context and attempt history across rounds (P1-E / P1-H)."""

    skill_name: str
    original_digest: str = ""
    repair_set: str = "repair_set"
    baseline_result: Optional[EvalResult] = None
    failures: list[Any] = field(default_factory=list)
    attempts: list[AttemptRecord] = field(default_factory=list)
    calls_used: int = 0
    tokens_used: int = 0
    stop_reason: Optional[str] = None
    # P1-E fields
    baseline_meta: Optional[SkillMeta] = None
    baseline_body: str = ""
    baseline_body_stats: dict[str, int] = field(default_factory=dict)
    active_budget: Optional[EvolveBudget] = None
    # P1-H / P1-I fields
    round_no: int = 1
    seen_fingerprints: set[str] = field(default_factory=set)
    shadow_mode: bool = True
    enable_a2: bool = True


@dataclass
class EvolveRecord:
    """Record of a patch evolution attempt with prompt bloat and validation tracking."""

    skill_name: str
    patch: Optional[Patch] = None
    baseline_body: str = ""
    candidate_body: str = ""
    baseline_body_stats: dict[str, int] = field(default_factory=dict)
    candidate_body_stats: dict[str, int] = field(default_factory=dict)
    bloat_verdict: Optional[str] = None
    bloat_reasons: list[str] = field(default_factory=list)
    distillation_prompt: Optional[str] = None
    status: Optional[str] = None


@dataclass
class Release:
    release_id: str
    skill_name: str
    version: str
    commit_hash: Optional[str]
    status: Literal["PREPARING", "PUBLISHED", "ABANDONED"]
    level: Optional[str]


@dataclass
class EvolveBudget:
    """Configurable budget and randomness guardrails (P1-F), prompt bloat guardrails (P1-E), and controlled rounds (P1-H)."""

    max_candidates: Optional[int] = 3
    max_calls: Optional[int] = None
    max_tokens: Optional[int] = None
    deadline_seconds: Optional[float] = None
    on_candidate_overflow: Literal["truncate", "reject"] = "truncate"
    max_llm_calls: Optional[int] = None
    max_total_tokens: Optional[int] = None
    max_candidates_first_round: Optional[int] = None
    max_candidates_retry: Optional[int] = None
    max_candidates_total: Optional[int] = None

    # P1-H Controlled Reflection Loop Guardrails
    max_rounds: int = 2
    enable_reflection: bool = False
    shadow_mode: bool = True
    auto_publish_enabled: bool = False

    # P1-I A2 Root Cause Switch (C/R vs B/RB)
    enable_a2: bool = True

    # Prompt Bloat Guardrails (P1-E / Token 1000-AND Policy)
    section_growth_ratio: float = 0.25      # 单段相对 baseline 增长门槛 (>25%)
    section_growth_tokens: int = 1000       # 单段绝对 token 净增门槛 (>1000 tokens)
    section_growth_chars: int = 100         # 保留兼容字段（新门控下不再作为阻断阈值）
    max_body_multiplier: float = 1.20       # 整 Body 倍数门限 (>1.20x，即相对增长 > 20%)
    max_body_delta_tokens: int = 1000       # 整 Body 净增 token 门槛 (>1000 tokens)
    max_body_chars: Optional[int] = None    # 整 Body 绝对字符数上限（冷启动 Draft 仍保留原 3000 上界）
    on_body_bloat: Literal["REVIEW", "DECLINED"] = "REVIEW"  # 全 body 门控动作
    tokenizer_name: str = "tiktoken:cl100k_base"  # 规范 Policy Tokenizer 口径

    # P1-I Fail-Closed Granularity & Case-level Fault Tolerance (A/D)
    invalid_case_ratio_threshold: float = 0.20   # baseline invalid case 容错比例阈值（默认 >20% 停止）
    max_invalid_cases: Optional[int] = None      # baseline invalid case 容错最大数量（可选，优先级高于 ratio）
    p0_fail_on_invalid: bool = True              # P0 关键 case invalid 时是否整次停止（默认 True）
    critical_case_ids: Optional[list[str]] = None # 自定义关键 case ID 列表（默认读取 p0_ids）
    judge_max_retries: int = 2                   # Judge 偶发 MALFORMED 自动重试次数（默认 2 次）

    def __post_init__(self) -> None:
        if self.max_calls is None and self.max_llm_calls is not None:
            self.max_calls = self.max_llm_calls
        if self.max_tokens is None and self.max_total_tokens is not None:
            self.max_tokens = self.max_total_tokens
        if self.max_candidates_first_round is not None:
            if self.max_candidates is None or self.max_candidates == 3:
                self.max_candidates = self.max_candidates_first_round
        elif self.max_candidates is not None and self.max_candidates_first_round is None:
            self.max_candidates_first_round = self.max_candidates

    def get_effective_candidate_limit(
        self,
        round_index: int = 0,
        candidates_so_far: int = 0,
    ) -> Optional[int]:
        """Compute candidate clamp limit for a given round (0=first round, >0=retry/reflection),
        respecting round limits (first_round/retry) and cumulative total limit (max_candidates_total)."""
        if round_index == 0:
            round_limit = (
                self.max_candidates_first_round
                if self.max_candidates_first_round is not None
                else self.max_candidates
            )
        else:
            round_limit = (
                self.max_candidates_retry
                if self.max_candidates_retry is not None
                else self.max_candidates
            )

        if self.max_candidates_total is not None:
            remaining = max(0, self.max_candidates_total - candidates_so_far)
            if round_limit is not None:
                return min(round_limit, remaining)
            return remaining
        return round_limit


class BudgetExceededError(RuntimeError):
    """Raised when any LLM budget hard cap (token, call, deadline, candidate) is exceeded."""

    def __init__(
        self,
        reason: str,
        cap_type: str,
        limit: Any = None,
        current: Any = None,
    ) -> None:
        super().__init__(reason)
        self.reason = reason
        self.cap_type = cap_type  # "call", "token", "deadline", "candidate"
        self.limit = limit
        self.current = current


EpisodeOutcome = Literal["success", "failure", "unknown"]
CandidateDecision = Literal["create", "revise", "abandon"]
CandidateStatus = Literal["DRAFT", "EVALUATING", "APPROVED", "REJECTED", "ABANDONED", "SUPERSEDED"]


@dataclass
class Episode:
    """Execution experience record with environment, tool call provenances, and independent acceptance verification."""

    episode_id: str
    task_id: str
    run_id: str
    skill_name: str
    skill_version: str
    environment: dict[str, Any]
    provenances: list[ToolCallProvenance]
    acceptance_criteria: dict[str, Any] | str
    outcome: EpisodeOutcome
    verification_evidence: Optional[dict[str, Any]] = None
    outcome_reason: str = ""
    created_at: Optional[str] = None

    def __post_init__(self) -> None:
        self.validate()

    def validate(self) -> None:
        if self.outcome not in ("success", "failure", "unknown"):
            raise ValueError(f"Invalid outcome: '{self.outcome}'. Must be 'success', 'failure', or 'unknown'")

        # Non-negotiable constraint: model self-assertion alone cannot establish verification success
        if self.outcome == "success":
            if not self.verification_evidence:
                raise ValueError(
                    "Cannot mark Episode as 'success' without independent verification evidence "
                    "(model self-assertion is untrusted)"
                )
            if (
                self.verification_evidence.get("source") == "model_self_assertion"
                or self.verification_evidence.get("self_asserted") is True
            ):
                if not self.verification_evidence.get("independent_pass", False):
                    raise ValueError(
                        "Cannot mark Episode as 'success' solely on model self-assertion"
                    )


@dataclass
class CandidateSkill:
    """Isolated candidate skill generated or revised from source episodes or documents, pending evaluation."""

    candidate_id: str
    skill_name: str
    decision: CandidateDecision
    source_episode_ids: list[str] = field(default_factory=list)
    meta: SkillMeta = field(default_factory=lambda: SkillMeta(name="unnamed", version="1.0.0", description="", use_when=""))
    body: str = ""
    rationale: str = ""
    status: CandidateStatus = "DRAFT"
    source_doc_id: Optional[str] = None
    source_doc_version: Optional[str] = None
    source_snippet_ids: list[str] = field(default_factory=list)
    source_requirement: Optional[str] = None
    source_type: Optional[str] = None
    task_spec_hash: Optional[str] = None
    created_at: Optional[str] = None
    updated_at: Optional[str] = None
    intent_revision: int = 1
    superseded_by: Optional[str] = None
    supersedes: Optional[str] = None
    parent_candidate_id: Optional[str] = None
    source_session_id: Optional[str] = None
    source_message_ids: list[str] = field(default_factory=list)

    def __post_init__(self) -> None:
        if self.parent_candidate_id is None and self.supersedes is not None:
            self.parent_candidate_id = self.supersedes
        elif self.supersedes is None and self.parent_candidate_id is not None:
            self.supersedes = self.parent_candidate_id

        if self.decision not in ("create", "revise", "abandon"):
            raise ValueError(f"Invalid decision '{self.decision}'. Must be 'create', 'revise', or 'abandon'")
        if not self.source_episode_ids and not self.source_doc_id and not self.source_requirement:
            raise ValueError(
                "CandidateSkill must be linked to at least one valid source (source_episode_ids, source_doc_id, or source_requirement)"
            )

    def is_trial_tested(self, episode_store: Any) -> bool:
        """Calculate dynamically if this candidate has at least one recorded trial execution in EpisodeStore."""
        if episode_store is None:
            return False
        try:
            conn = episode_store._get_conn()
            row = conn.execute(
                "SELECT 1 FROM episodes WHERE json_extract(environment_json, '$.candidate_id') = ? LIMIT 1",
                (self.candidate_id,),
            ).fetchone()
            return row is not None
        except Exception:
            return False


@dataclass
class TaskContext:
    """Bounded task execution and intent context tracking user goals, constraints, and revisions."""

    task_id: str
    goal: str
    business_scope: str = ""
    constraints: list[str] = field(default_factory=list)
    acceptance_criteria: dict[str, Any] = field(default_factory=dict)
    intent_revision: int = 1
    contract_fingerprint: str = ""
    active_candidate_id: Optional[str] = None
    active_skill_name: Optional[str] = None
    active_skill_version: Optional[str] = None
    active_body_snapshot: Optional[str] = None
    superseded_candidate_ids: list[str] = field(default_factory=list)
    assumptions: list[str] = field(default_factory=list)
    created_at: Optional[str] = None
    updated_at: Optional[str] = None

    def compute_fingerprint(self) -> str:
        import hashlib
        payload = f"{self.task_id}:{self.goal.strip()}:{sorted(self.constraints)}:{self.business_scope.strip()}"
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]

    def to_dict(self) -> dict[str, Any]:
        return {
            "task_id": self.task_id,
            "goal": self.goal,
            "business_scope": self.business_scope,
            "constraints": self.constraints,
            "acceptance_criteria": self.acceptance_criteria,
            "intent_revision": self.intent_revision,
            "contract_fingerprint": self.contract_fingerprint or self.compute_fingerprint(),
            "active_candidate_id": self.active_candidate_id,
            "active_skill_name": self.active_skill_name,
            "active_skill_version": self.active_skill_version,
            "active_body_snapshot": self.active_body_snapshot,
            "superseded_candidate_ids": self.superseded_candidate_ids,
            "assumptions": self.assumptions,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> TaskContext:
        return cls(
            task_id=data["task_id"],
            goal=data.get("goal", ""),
            business_scope=data.get("business_scope", ""),
            constraints=list(data.get("constraints") or []),
            acceptance_criteria=dict(data.get("acceptance_criteria") or {}),
            intent_revision=int(data.get("intent_revision", 1)),
            contract_fingerprint=data.get("contract_fingerprint", ""),
            active_candidate_id=data.get("active_candidate_id"),
            active_skill_name=data.get("active_skill_name"),
            active_skill_version=data.get("active_skill_version"),
            active_body_snapshot=data.get("active_body_snapshot"),
            superseded_candidate_ids=list(data.get("superseded_candidate_ids") or []),
            assumptions=list(data.get("assumptions") or []),
            created_at=data.get("created_at"),
            updated_at=data.get("updated_at"),
        )


@dataclass
class VersionSnapshot:
    """Immutable snapshot of a skill version including metadata, body, hash, and verification status."""

    skill_name: str
    version: str
    content_hash: str
    meta: Optional[SkillMeta]
    body: str
    commit_hash: Optional[str] = None
    release_id: Optional[str] = None
    status: str = "PUBLISHED"  # "PUBLISHED", "READY", "UNVERIFIED", "UNAVAILABLE"
    is_verified: bool = True
    eval_summary: Optional[dict[str, Any]] = None
    source_lineage: Optional[list[str]] = None
    dependencies: list[str] = field(default_factory=list)


@dataclass
class VersionComparison:
    """Read-only comparison result between two versions of the same skill."""

    skill_name: str
    version_a: str
    version_b: str
    content_diff: str
    metadata_diff: dict[str, Any]
    dependencies_diff: dict[str, Any]
    eval_delta: dict[str, Any]
    is_comparable: bool = True
    incomparable_reason: Optional[str] = None


@dataclass
class Deployment:
    """Persistent deployment state mapping stable and optional canary version."""

    skill_name: str
    stable_version: str
    stable_release_id: Optional[str] = None
    canary_version: Optional[str] = None
    canary_release_id: Optional[str] = None
    canary_share: int = 0
    rollout_id: str = ""
    revision: int = 1
    updated_at: Optional[str] = None


@dataclass
class DeploymentAuditEvent:
    """Audit log entry tracking deployment changes (rollback, canary, share change)."""

    event_id: str
    operation_id: Optional[str]
    skill_name: str
    action: str  # 'SET_CANARY', 'CHANGE_SHARE', 'PROMOTE_CANARY', 'ROLLBACK'
    from_stable: Optional[str]
    to_stable: Optional[str]
    from_canary: Optional[str]
    to_canary: Optional[str]
    from_share: Optional[int]
    to_share: Optional[int]
    reason: str
    revision_before: int
    revision_after: int
    created_at: Optional[str] = None


@dataclass
class RunVersionBinding:
    """Frozen version binding for a specific execution run."""

    run_id: str
    skill_name: str
    assigned_version: str
    content_hash: str
    is_canary: bool
    frozen_body: str
    created_at: Optional[str] = None


RuntimeStatus = Literal[
    "PENDING",
    "RUNNING",
    "COMPLETED",
    "FAILED",
    "CANCELLED",
    "TIMED_OUT",
    "BUDGET_EXHAUSTED",
    "INTERRUPTED",
]


@dataclass
class ToolCallRecord:
    """Execution trace of an individual tool call dispatched via ToolBroker."""

    call_id: str
    run_id: str
    tool_name: str
    status: Literal["ADMITTED", "REJECTED", "EXECUTED", "ERROR", "TIMED_OUT", "CANCELLED"]
    input_params: dict[str, Any]
    output_text: str = ""
    output_data: dict[str, Any] = field(default_factory=dict)
    error_type: Optional[str] = None
    error_message: Optional[str] = None
    latency_ms: float = 0.0
    created_at: Optional[str] = None
    provenance: Optional[ToolCallProvenance] = None


@dataclass
class RunRecord:
    """Lifecycle and execution state of an AgentRuntime run."""

    run_id: str
    task_id: str
    skill_name: Optional[str]
    skill_version: Optional[str]
    content_hash: Optional[str]
    status: RuntimeStatus
    purpose: Literal["evaluation", "learning"]
    budget_max: int = 10
    budget_consumed: int = 0
    deadline_ts: Optional[float] = None
    error_type: Optional[str] = None
    error_message: Optional[str] = None
    created_at: Optional[str] = None
    updated_at: Optional[str] = None
    terminal_at: Optional[str] = None
    frozen_body: Optional[str] = None
    intent_revision: int = 1
    task_spec_hash: Optional[str] = None
    candidate_id: Optional[str] = None


@dataclass
class SemanticFact:
    """A scoped observation or fact tied to a verified source in Semantic Memory."""

    fact_id: str  # Must start with 'fact_'
    statement: str
    source_id: str  # Provenance source ID: e.g. 'run_...', 'ep_...', 'tool_...', 'manual_...'
    scope: str = "global"  # Context/environment scope
    topic: str = "general"  # Subject attribute or category, used for conflict detection
    is_universal: bool = False  # True indicates universal unconditional fact; False indicates scoped observation
    tags: list[str] = field(default_factory=list)
    created_at: Optional[str] = None

    def __post_init__(self) -> None:
        if not self.fact_id.startswith("fact_"):
            raise ValueError(
                f"SemanticFact fact_id must start with 'fact_', got '{self.fact_id}'"
            )
        if not self.statement or not self.statement.strip():
            raise ValueError("SemanticFact statement cannot be empty")
        if not self.source_id or not self.source_id.strip():
            raise ValueError("SemanticFact source_id cannot be empty")
        # Invariant: single execution observation cannot be marked as universal fact
        if self.is_universal and (
            self.source_id.startswith("run_")
            or self.source_id.startswith("ep_")
            or "single_execution" in self.tags
        ):
            raise ValueError(
                f"Single execution observation from '{self.source_id}' cannot be marked as an unconditional universal fact"
            )


@dataclass
class SemanticConflict:
    """Representation of conflicting observations on the same topic/attribute across different sources."""

    topic: str
    facts: list[SemanticFact]
    description: str = ""


@dataclass
class DocumentSnippet:
    """A verifiable text snippet from a source document."""

    snippet_id: str  # Must start with 'snip_'
    doc_id: str
    doc_version: str
    section_title: str
    start_line: int
    end_line: int
    content: str
    content_hash: str

    def __post_init__(self) -> None:
        if not self.snippet_id.startswith("snip_"):
            raise ValueError(f"DocumentSnippet snippet_id must start with 'snip_', got '{self.snippet_id}'")
        if self.start_line < 1 or self.end_line < self.start_line:
            raise ValueError(f"Invalid line range: {self.start_line}-{self.end_line}")


@dataclass
class DocumentSource:
    """An independent typed local document source providing candidate skill procedures."""

    doc_id: str  # Must start with 'doc_'
    title: str
    version: str
    content: str
    content_hash: str
    snippets: list[DocumentSnippet] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)
    created_at: Optional[str] = None

    def __post_init__(self) -> None:
        if not self.doc_id.startswith("doc_"):
            raise ValueError(f"DocumentSource doc_id must start with 'doc_', got '{self.doc_id}'")
        if not self.title or not self.title.strip():
            raise ValueError("DocumentSource title cannot be empty")
        if not self.version or not self.version.strip():
            raise ValueError("DocumentSource version cannot be empty")
        if not self.content_hash:
            raise ValueError("DocumentSource content_hash cannot be empty")


@dataclass
class DocumentExtractionResult:
    """Outcome of attempting to extract operable skill candidates from a DocumentSource."""

    doc_id: str
    doc_version: str
    status: Literal["success", "rejected", "conflict"]
    candidate: Optional[CandidateSkill] = None
    target_skill_name: str = ""
    rejection_reasons: list[str] = field(default_factory=list)
    conflicts: list[str] = field(default_factory=list)
    extracted_snippets: list[DocumentSnippet] = field(default_factory=list)
    raw_claims_filtered: list[str] = field(default_factory=list)


@dataclass
class MemoryLineage:
    """Cross-tier provenance trace linking procedural knowledge -> episodic experiences -> semantic facts -> documents."""

    procedural_id: str
    procedural_type: Literal["candidate", "release", "skill"]
    procedural_item: Any
    supporting_episodes: list[Episode] = field(default_factory=list)
    source_facts: list[SemanticFact] = field(default_factory=list)
    source_document: Optional[DocumentSource] = None
    source_snippets: list[DocumentSnippet] = field(default_factory=list)
    lineage_broken: bool = False
    broken_reasons: list[str] = field(default_factory=list)


@dataclass
class RetrievalContext:
    """Task execution context passed to retrieval for scope, version, and permission filtering."""

    task_id: Optional[str] = None
    run_id: Optional[str] = None
    assigned_version: Optional[str] = None
    allowed_tools: Optional[set[str]] = None
    available_dependencies: Optional[set[str]] = None
    scope: Optional[str] = None
    limit: int = 10
    include_evidence: bool = True


@dataclass
class SkillRecommendation:
    """A bounded, reviewable formal skill suggestion with provenance and verification status."""

    skill_name: str
    version: str
    content_hash: str
    meta: Optional[SkillMeta]
    body: str
    relevance_score: float
    match_reasons: list[str] = field(default_factory=list)
    lineage: Optional[MemoryLineage] = None
    source_type: Literal["episode_mined", "document_derived", "manual_or_unknown"] = "manual_or_unknown"
    verification_episodes: list[Episode] = field(default_factory=list)
    is_verified: bool = True
    is_canary: bool = False
    dependencies: list[str] = field(default_factory=list)
    lineage_broken: bool = False
    broken_reasons: list[str] = field(default_factory=list)


@dataclass
class FutureRetrievalResult:
    """Bounded, read-only multi-tier memory retrieval result."""

    query: str
    skills: list[SkillRecommendation] = field(default_factory=list)
    evidence_episodes: list[Episode] = field(default_factory=list)
    evidence_facts: list[SemanticFact] = field(default_factory=list)
    candidates: list[CandidateSkill] = field(default_factory=list)
    conflicts: list[SemanticConflict] = field(default_factory=list)
    filtered_out: list[dict[str, Any]] = field(default_factory=list)
    empty_reason: Optional[str] = None


@dataclass
class TestCaseProposal:
    """Structured, reproducible test case proposal synthesized from Episode/eval trace."""

    __test__ = False

    proposal_id: str
    skill_name: str
    source_task_id: str
    intent_revision: int = 1
    contract_fingerprint: str = ""
    query: str = ""
    tool_snapshots: list[dict[str, Any]] = field(default_factory=list)
    expected_output: Optional[str | dict[str, Any]] = None
    expectation_source: str = "missing"  # "business_rule" | "oracle" | "human_confirmed" | "tool_snapshot" | "draft_proposal" | "missing"
    status: Literal["APPROVED", "PENDING_APPROVAL", "REJECTED"] = "PENDING_APPROVAL"
    failure_attribution: Literal["skill", "infrastructure", "policy", "unknown"] = "unknown"
    is_regression_case: bool = False
    actual_output: Optional[str] = None
    rejection_reason: Optional[str] = None
    partition_tier: str = "repair"  # "repair" (dev) | "experiment_holdout" | "final_audit"
    variant_family: Optional[str] = None
    created_at: Optional[str] = None
    updated_at: Optional[str] = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "proposal_id": self.proposal_id,
            "skill_name": self.skill_name,
            "source_task_id": self.source_task_id,
            "intent_revision": self.intent_revision,
            "contract_fingerprint": self.contract_fingerprint,
            "query": self.query,
            "tool_snapshots": self.tool_snapshots,
            "expected_output": self.expected_output,
            "expectation_source": self.expectation_source,
            "status": self.status,
            "failure_attribution": self.failure_attribution,
            "is_regression_case": self.is_regression_case,
            "actual_output": self.actual_output,
            "rejection_reason": self.rejection_reason,
            "partition_tier": self.partition_tier,
            "variant_family": self.variant_family,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> TestCaseProposal:
        return cls(
            proposal_id=data["proposal_id"],
            skill_name=data.get("skill_name", ""),
            source_task_id=data.get("source_task_id", ""),
            intent_revision=int(data.get("intent_revision", 1)),
            contract_fingerprint=data.get("contract_fingerprint", ""),
            query=data.get("query", ""),
            tool_snapshots=list(data.get("tool_snapshots") or []),
            expected_output=data.get("expected_output"),
            expectation_source=data.get("expectation_source", "missing"),
            status=data.get("status", "PENDING_APPROVAL"),
            failure_attribution=data.get("failure_attribution", "unknown"),
            is_regression_case=bool(data.get("is_regression_case", False)),
            actual_output=data.get("actual_output"),
            rejection_reason=data.get("rejection_reason"),
            partition_tier=data.get("partition_tier", "repair"),
            variant_family=data.get("variant_family"),
            created_at=data.get("created_at"),
            updated_at=data.get("updated_at"),
        )


@dataclass
class PurificationResult:
    """Categorized trace purification outcome routing failures, regressions, and anomalies."""

    category: Literal[
        "business_failure",
        "regression_success",
        "diagnosis_only",
        "infrastructure_report",
        "policy_compliance",
    ]
    proposal: Optional[TestCaseProposal] = None
    diagnosis: dict[str, Any] = field(default_factory=dict)
    can_trigger_skill_evolution: bool = False
    notes: str = ""


@dataclass
class LineageBinding:
    """Lineage binding for bounded exception recovery and checkpoint invalidation (L4)."""

    skill_name: str
    baseline_version: str
    baseline_hash: str
    intent_revision: int = 1
    business_scope: str = "default"
    contract_fingerprint: str = ""
    task_id: Optional[str] = None
    config_hash: Optional[str] = None
    candidate_hash: Optional[str] = None
    dataset_version: Optional[str] = None

    def validate_match(self, current: LineageBinding) -> tuple[bool, str]:
        if self.skill_name != current.skill_name:
            return False, f"CHECKPOINT_INVALIDATED: skill_name mismatch ('{self.skill_name}' != '{current.skill_name}')"
        if self.baseline_version != current.baseline_version:
            return False, f"CHECKPOINT_INVALIDATED: baseline_version changed ('{self.baseline_version}' != '{current.baseline_version}')"
        if self.baseline_hash != current.baseline_hash:
            return False, f"CHECKPOINT_INVALIDATED: baseline_hash changed ('{self.baseline_hash}' != '{current.baseline_hash}')"
        if self.intent_revision != current.intent_revision:
            return False, f"CHECKPOINT_INVALIDATED: intent_revision changed ({self.intent_revision} != {current.intent_revision})"
        if self.business_scope != current.business_scope:
            return False, f"CHECKPOINT_INVALIDATED: business_scope changed ('{self.business_scope}' != '{current.business_scope}')"
        if (self.contract_fingerprint or current.contract_fingerprint) and self.contract_fingerprint != current.contract_fingerprint:
            return False, f"CHECKPOINT_INVALIDATED: contract_fingerprint changed ('{self.contract_fingerprint}' != '{current.contract_fingerprint}')"
        if (self.config_hash or current.config_hash) and self.config_hash != current.config_hash:
            return False, f"CHECKPOINT_INVALIDATED: validator config_hash changed ('{self.config_hash}' != '{current.config_hash}')"
        if (self.candidate_hash or current.candidate_hash) and self.candidate_hash != current.candidate_hash:
            return False, f"CHECKPOINT_INVALIDATED: candidate content_hash changed ('{self.candidate_hash}' != '{current.candidate_hash}')"
        if (self.dataset_version or current.dataset_version) and self.dataset_version != current.dataset_version:
            return False, f"CHECKPOINT_INVALIDATED: dataset_version changed ('{self.dataset_version}' != '{current.dataset_version}')"
        return True, ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "skill_name": self.skill_name,
            "baseline_version": self.baseline_version,
            "baseline_hash": self.baseline_hash,
            "intent_revision": self.intent_revision,
            "business_scope": self.business_scope,
            "contract_fingerprint": self.contract_fingerprint,
            "task_id": self.task_id,
            "config_hash": self.config_hash,
            "candidate_hash": self.candidate_hash,
            "dataset_version": self.dataset_version,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> LineageBinding:
        return cls(
            skill_name=data["skill_name"],
            baseline_version=data["baseline_version"],
            baseline_hash=data["baseline_hash"],
            intent_revision=int(data.get("intent_revision", 1)),
            business_scope=data.get("business_scope", "default"),
            contract_fingerprint=data.get("contract_fingerprint", ""),
            task_id=data.get("task_id"),
            config_hash=data.get("config_hash"),
            candidate_hash=data.get("candidate_hash"),
            dataset_version=data.get("dataset_version"),
        )


@dataclass
class RecoveryBudget:
    """Shared top-level budget tracker across graph and repair jobs (L3)."""

    max_attempts: int = 2
    consumed_attempts: int = 0
    max_tokens: int = 50_000
    consumed_tokens: int = 0
    max_calls: int = 10
    consumed_calls: int = 0
    max_tool_calls: int = 10
    consumed_tool_calls: int = 0
    deadline_seconds: Optional[float] = None
    start_time: float = field(default_factory=time.time)
    executed_side_effects: list[str] = field(default_factory=list)
    has_real_token_accounting: bool = False

    @property
    def remaining_attempts(self) -> int:
        return max(0, self.max_attempts - self.consumed_attempts)

    @property
    def remaining_calls(self) -> int:
        return max(0, self.max_calls - self.consumed_calls)

    @property
    def remaining_tokens(self) -> int:
        return max(0, self.max_tokens - self.consumed_tokens)

    @property
    def remaining_tool_calls(self) -> int:
        return max(0, self.max_tool_calls - self.consumed_tool_calls)

    def is_timed_out(self) -> bool:
        if self.deadline_seconds is None:
            return False
        return (time.time() - self.start_time) > self.deadline_seconds

    def can_attempt(self) -> bool:
        if self.consumed_attempts >= self.max_attempts:
            return False
        if self.consumed_calls >= self.max_calls:
            return False
        if self.consumed_tool_calls >= self.max_tool_calls:
            return False
        if self.consumed_tokens >= self.max_tokens:
            return False
        if self.is_timed_out():
            return False
        return True

    def consume(
        self,
        attempts: int = 1,
        calls: int = 1,
        tokens: Optional[int] = None,
        tool_calls: int = 0,
        side_effect: Optional[str] = None,
    ) -> None:
        self.consumed_attempts += attempts
        self.consumed_calls += calls
        self.consumed_tool_calls += tool_calls
        if tokens is not None:
            self.consumed_tokens += tokens
            self.has_real_token_accounting = True
        if side_effect and side_effect not in self.executed_side_effects:
            self.executed_side_effects.append(side_effect)

    def to_dict(self) -> dict[str, Any]:
        return {
            "max_attempts": self.max_attempts,
            "consumed_attempts": self.consumed_attempts,
            "max_tokens": self.max_tokens,
            "consumed_tokens": self.consumed_tokens,
            "max_calls": self.max_calls,
            "consumed_calls": self.consumed_calls,
            "max_tool_calls": self.max_tool_calls,
            "consumed_tool_calls": self.consumed_tool_calls,
            "deadline_seconds": self.deadline_seconds,
            "start_time": self.start_time,
            "executed_side_effects": list(self.executed_side_effects),
            "has_real_token_accounting": self.has_real_token_accounting,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> RecoveryBudget:
        return cls(
            max_attempts=int(data.get("max_attempts", 2)),
            consumed_attempts=int(data.get("consumed_attempts", 0)),
            max_tokens=int(data.get("max_tokens", 50_000)),
            consumed_tokens=int(data.get("consumed_tokens", 0)),
            max_calls=int(data.get("max_calls", 10)),
            consumed_calls=int(data.get("consumed_calls", 0)),
            max_tool_calls=int(data.get("max_tool_calls", 10)),
            consumed_tool_calls=int(data.get("consumed_tool_calls", 0)),
            deadline_seconds=data.get("deadline_seconds"),
            start_time=float(data.get("start_time", time.time())),
            executed_side_effects=list(data.get("executed_side_effects") or []),
            has_real_token_accounting=bool(data.get("has_real_token_accounting", False)),
        )


@dataclass
class BoundedRecoveryResult:
    """Outcome of bounded exception recovery run (L1–L5)."""

    status: Literal["SUCCESS", "AWAITING_REVIEW", "BLOCKED", "DECLINED", "EXHAUSTED", "STOPPED"]
    reason_code: Optional[str] = None
    candidate: Optional[CandidateSkill] = None
    validation_record: Optional[ValidationRecord] = None
    budget: Optional[RecoveryBudget] = None
    lineage: Optional[LineageBinding] = None
    attempts: list[dict[str, Any]] = field(default_factory=list)
    diagnostics: dict[str, Any] = field(default_factory=dict)
    shadow_root: Optional[str] = None
    applied_to_registry: bool = False
    repair_job: Optional[Any] = None
    transition_history: list[dict[str, Any]] = field(default_factory=list)





