"""SkillForge · Agent Skill 自进化元 Agent 系统

顶层暴露 5 大组件 + 数据模型。

参见：
- README.md（设计取舍 + 面试口径）
- ARCHITECTURE.md（组件划分 + 接口签名 + 数据流）
"""
__version__ = "0.1.0"

from .models import (
    SkillMeta,
    Trigger,
    Evaluation,
    RouteResult,
    EvalResult,
    RatchetVerdict,
    Patch,
    PatchStatus,
    Release,
    EvolveBudget,
    BudgetExceededError,
    BodySectionStats,
    EvolveContext,
    EvolveRecord,
    AttemptRecord,
    AttemptFeedback,
)
from .registry import SkillRegistry
from .router import IntentRouter
from .evaluator import (
    SkillEvaluator,
    EvaluatorOutputCache,
    PromptBloatResult,
    check_prompt_bloat,
    compute_body_section_stats,
)
from .evaluator.llm_factory import LLMLedger
from .evolver import SkillEvolver
from .state_machine import ReleaseStateMachine
from .skill_generator import (
    generate_skill,
    register_skill,
    GeneratedSkill,
    GenerationFailure,
    derive_skill_abbrev,
    validate_generated_structure,
    check_conflict,
    RegistrationError,
    build_manifest_report,
    render_manifest_report,
)
from .skill_splitter import (
    analyze_split,
    split_skill,
    deprecate_original_skill,
    SplitAnalysis,
    SplitResult,
    DomainSpec,
    DimensionCoupling,
    CaseAssignment,
)

__all__ = [
    "__version__",
    "SkillMeta", "Trigger", "Evaluation",
    "RouteResult", "EvalResult", "RatchetVerdict", "Patch", "PatchStatus", "Release",
    "EvolveBudget", "BudgetExceededError", "BodySectionStats", "EvolveContext", "EvolveRecord",
    "AttemptRecord", "AttemptFeedback",
    "LLMLedger", "EvaluatorOutputCache",
    "PromptBloatResult", "check_prompt_bloat", "compute_body_section_stats",
    "SkillRegistry",
    "IntentRouter",
    "SkillEvaluator",
    "SkillEvolver",
    "ReleaseStateMachine",
    "generate_skill",
    "register_skill",
    "GeneratedSkill",
    "GenerationFailure",
    "derive_skill_abbrev",
    "validate_generated_structure",
    "check_conflict",
    "RegistrationError",
    "build_manifest_report",
    "render_manifest_report",
    "analyze_split",
    "split_skill",
    "deprecate_original_skill",
    "SplitAnalysis",
    "SplitResult",
    "DomainSpec",
    "DimensionCoupling",
    "CaseAssignment",
]
