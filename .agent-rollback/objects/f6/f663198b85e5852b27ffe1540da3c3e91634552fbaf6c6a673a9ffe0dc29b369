"""Skill / Route / Evaluation / Release 数据模型

- SkillMeta 系列：Pydantic BaseModel（需要 YAML frontmatter 解析 + 校验）
- Route/Eval/Ratchet/Patch/Release：dataclass（组件间内部传递够用）

参见 ARCHITECTURE §5。
"""
from __future__ import annotations
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


@dataclass(frozen=True)
class ToolCallProvenance:
    """Canonical SHA-256 integrity record of a dependency fixture invocation."""

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


@dataclass
class RatchetVerdict:
    decision: Literal["PASS", "REVIEW", "DECLINED"]
    reasons: list[str] = field(default_factory=list)


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
    """Configurable budget and randomness guardrails (P1-F)."""

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
