"""Skill / Route / Evaluation / Release 数据模型

- SkillMeta 系列：Pydantic BaseModel（需要 YAML frontmatter 解析 + 校验）
- Route/Eval/Ratchet/Patch/Release：dataclass（组件间内部传递够用）

参见 ARCHITECTURE §5。
"""
from __future__ import annotations
from dataclasses import dataclass, field
from typing import Literal, Optional
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


@dataclass
class Release:
    release_id: str
    skill_name: str
    version: str
    commit_hash: Optional[str]
    status: Literal["PREPARING", "PUBLISHED", "ABANDONED"]
    level: Optional[str]
