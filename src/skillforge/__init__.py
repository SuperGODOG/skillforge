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
    ToolCallProvenance,
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
    Episode,
    CandidateSkill,
    EpisodeOutcome,
    SemanticFact,
    SemanticConflict,
    MemoryLineage,
    DocumentSnippet,
    DocumentSource,
    DocumentExtractionResult,
    RetrievalContext,
    SkillRecommendation,
    FutureRetrievalResult,
    VersionSnapshot,
    VersionComparison,
    Deployment,
    DeploymentAuditEvent,
    RunVersionBinding,
    RuntimeStatus,
    ToolCallRecord,
    RunRecord,
    LineageBinding,
    RecoveryBudget,
    BoundedRecoveryResult,
)
from .retrieval import FutureMemoryRetriever
from .runtime import (
    ToolBroker,
    AgentRuntime,
    BrokeredTool,
    sanitize_params,
    validate_parameter_schema,
)
from .sandbox import (
    SandboxConfig,
    SandboxResult,
    SandboxBackend,
    MacSeatbeltSandbox,
    DependencyProbe,
    ProbeResult,
    SandboxedToolSpec,
)
from .memory import (
    SemanticStore,
    ThreeTierMemoryManager,
)
from .documents import (
    DocumentStore,
    parse_markdown_snippets,
    extract_candidate_from_document,
)
from .episode import EpisodeStore, CandidateStore
from .evolution_loop import (
    MiningResult,
    ValidationRecord,
    mine_candidate,
    validate_candidate,
    promote_candidate,
    compute_candidate_hash,
)
from .pattern_mining import (
    PatternMiningConfig,
    ClusterReport,
    MiningBatchReport,
    mine_pending,
)
from .repair import (
    AttributionDiagnosis,
    RepairAttemptRecord,
    RepairJob,
    attribute_failure,
    repair_skill_failure,
    promote_repaired_skill,
)
from .deployments import (
    DeploymentManager,
    ConcurrencyError,
    compute_content_hash,
)
from .receipt import (
    ValidationDiagnostic,
    ValidationReceipt,
    JsonConfigValidator,
    FixAction,
    CorrectionPolicy,
    DeterministicJsonFixer,
    compute_artifact_fingerprint,
    apply_action_to_content,
    get_default_ab_cases,
    run_artifact_ab_comparison,
)
from .collector import ExperienceCollector, RunContext
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
    "RouteResult", "ToolCallProvenance", "EvalResult", "RatchetVerdict", "Patch", "PatchStatus", "Release",
    "EvolveBudget", "BudgetExceededError", "BodySectionStats", "EvolveContext", "EvolveRecord",
    "AttemptRecord", "AttemptFeedback",
    "Episode", "CandidateSkill", "CandidateDecision", "CandidateStatus", "EpisodeOutcome",
    "SemanticFact", "SemanticConflict", "MemoryLineage",
    "VersionSnapshot", "VersionComparison", "Deployment", "DeploymentAuditEvent", "RunVersionBinding",
    "DocumentSnippet", "DocumentSource", "DocumentExtractionResult",
    "DocumentStore", "parse_markdown_snippets", "extract_candidate_from_document",
    "RetrievalContext", "SkillRecommendation", "FutureRetrievalResult", "FutureMemoryRetriever",
    "SandboxConfig", "SandboxResult", "SandboxBackend", "MacSeatbeltSandbox", "DependencyProbe", "ProbeResult", "SandboxedToolSpec",
    "EpisodeStore", "CandidateStore", "SemanticStore", "ThreeTierMemoryManager",
    "MiningResult", "ValidationRecord", "mine_candidate", "validate_candidate", "promote_candidate", "compute_candidate_hash",
    "PatternMiningConfig", "ClusterReport", "MiningBatchReport", "mine_pending",
    "AttributionDiagnosis", "RepairAttemptRecord", "RepairJob", "attribute_failure", "repair_skill_failure", "promote_repaired_skill",
    "DeploymentManager", "ConcurrencyError", "compute_content_hash",
    "ValidationDiagnostic", "ValidationReceipt", "JsonConfigValidator", "FixAction", "CorrectionPolicy",
    "DeterministicJsonFixer", "compute_artifact_fingerprint", "apply_action_to_content",
    "get_default_ab_cases", "run_artifact_ab_comparison",
    "ExperienceCollector", "RunContext",
    "ToolBroker", "AgentRuntime", "BrokeredTool", "ToolCallRecord", "RunRecord", "RuntimeStatus",
    "sanitize_params", "validate_parameter_schema",
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
    "SplitProposal",
    "suggest_skill_split",
    "DomainSpec",
    "DimensionCoupling",
    "CaseAssignment",
    "run_evolve_langgraph",
    "resume_evolve_langgraph",
    "build_evolve_state_graph",
    "SqliteCheckpointer",
    "EvolveLoopState",
    "LineageBinding",
    "RecoveryBudget",
    "BoundedRecoveryResult",
    "run_bounded_recovery",
    "recover_bloated_candidate",
    "check_non_recoverable_blockers",
]

from .langgraph_loop import (
    run_evolve_langgraph,
    resume_evolve_langgraph,
    build_evolve_state_graph,
    SqliteCheckpointer,
    EvolveLoopState,
)
from .skill_splitter import (
    SplitProposal,
    suggest_skill_split,
)
from .bounded_recovery import (
    run_bounded_recovery,
    recover_bloated_candidate,
    check_non_recoverable_blockers,
)

