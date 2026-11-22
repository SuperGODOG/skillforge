"""SkillForge Pi Runtime & Backend Assembly Helper.

Provides:
- validate_pi_scenario_and_dependencies: Strict upfront validation of supported
  scenarios and declared skill dependencies.
- assemble_pi_resources: Context-managed lifecycle helper bundling AgentRuntime,
  ToolBroker, ExperienceCollector, EpisodeStore, CandidateStore, SkillRegistry,
  and PiEvaluationBackend.

Guarantees:
- Upfront rejection of unsupported scenarios and incompatible skill dependencies
  before file creation, runtime initialization, or model calls.
- Isolated resource management and safe cleanup of DB connections, SQLite stores,
  and Pi runner child processes on success, failure, or exception.
- Reusable assembly helper suited for both evaluation (Phase 4A1) and evolution (Phase 4A2).
"""
from __future__ import annotations

import os
import sys
from contextlib import ExitStack, contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Iterator, Optional, Set

from .collector import ExperienceCollector
from .episode import CandidateStore, EpisodeStore
from .models import SkillMeta
from .pi_evaluation import PiEvaluationBackend
from .registry import SkillRegistry
from .runtime import AgentRuntime, ToolBroker

SUPPORTED_PI_SCENARIOS: set[str] = {"logistics"}

SCENARIO_TOOL_ALLOWLISTS: dict[str, set[str]] = {
    "logistics": {"query_order_packages", "query_package_tracking"},
}

# Test-only hook for injecting mock responses or parameters without public CLI exposure
_TEST_MODEL_CONFIG_HOOK: Optional[Callable[[dict[str, Any]], dict[str, Any]]] = None


def validate_pi_scenario_and_dependencies(
    scenario: str,
    declared_dependencies: Optional[list[str]] = None,
) -> set[str]:
    """Validate scenario is supported and declared dependencies are compatible.

    Returns the fixed allowlist of tools for the scenario.
    Raises ValueError if scenario is unsupported or dependencies are incompatible.
    """
    if scenario not in SUPPORTED_PI_SCENARIOS:
        raise ValueError(
            f"Unsupported Pi scenario '{scenario}'. "
            f"Only {sorted(SUPPORTED_PI_SCENARIOS)} are currently supported."
        )
    allowlist = SCENARIO_TOOL_ALLOWLISTS[scenario]
    if declared_dependencies:
        incompatible = set(declared_dependencies) - allowlist
        if incompatible:
            raise ValueError(
                f"Skill declares incompatible dependencies: {sorted(incompatible)}. "
                f"Scenario '{scenario}' only supports: {sorted(allowlist)}"
            )
    return allowlist


def register_scenario_tools(broker: ToolBroker, scenario: str) -> list[str]:
    """Register supported tools onto ToolBroker according to scenario."""
    if scenario == "logistics":
        from .scenarios.logistics import (
            QueryOrderPackagesTool,
            QueryPackageTrackingTool,
        )

        broker.register_tool(QueryOrderPackagesTool())
        broker.register_tool(QueryPackageTrackingTool())
        return ["query_order_packages", "query_package_tracking"]
    raise ValueError(f"No tool registrations for scenario '{scenario}'")


@dataclass
class PiResourceBundle:
    """Encapsulates context-managed Pi runtime and backend resources."""

    scenario: str
    registry: SkillRegistry
    episode_store: EpisodeStore
    candidate_store: CandidateStore
    broker: ToolBroker
    collector: ExperienceCollector
    runtime: AgentRuntime
    backend: PiEvaluationBackend
    tool_names: list[str]

    def close(self) -> None:
        """Clean up runtime, stores, and registry, and ensure Pi child processes are reaped."""
        with ExitStack() as resources:
            for resource in (self.registry, self.episode_store, self.candidate_store, self.runtime):
                resources.callback(resource.close)


@contextmanager
def assemble_pi_resources(
    scenario: str,
    provider: str,
    model: str,
    repo_root: Path,
    skill_meta: Optional[SkillMeta] = None,
    db_dir: Optional[Path] = None,
    budget_max: int = 10,
    test_mode: Optional[bool] = None,
    model_config_extra: Optional[dict[str, Any]] = None,
    system_prompt_header: str = "",
) -> Iterator[PiResourceBundle]:
    """Assemble and context-manage runtime, broker, collector, stores, registry, and Pi backend.

    Guarantees:
    - Upfront scenario and dependency validation before resource allocation or file creation.
    - Cleanup on normal exit, invalid evaluation, or exception.
    - Reaping of Pi child processes.
    """
    repo_root = Path(repo_root).resolve()

    # 1. Validate scenario and dependencies upfront BEFORE creating any files
    declared_deps = list(getattr(skill_meta, "dependencies", []) or []) if skill_meta else []
    allowlist = validate_pi_scenario_and_dependencies(scenario, declared_deps)

    # 2. Setup paths
    runs_dir = db_dir if db_dir is not None else (repo_root / "runs")
    runs_dir.mkdir(parents=True, exist_ok=True)
    runtime_db = runs_dir / "runtime.db"
    episodes_db = runs_dir / "episodes.db"
    candidates_db = runs_dir / "candidates.db"
    skills_db = runs_dir / "skillforge.db"
    skills_dir = repo_root / "skills"

    with ExitStack() as resources:
        # 3. Initialize components
        registry = SkillRegistry(db_path=skills_db, skills_dir=skills_dir, repo_root=repo_root)
        resources.callback(registry.close)
        episode_store = EpisodeStore(db_path=episodes_db)
        resources.callback(episode_store.close)
        candidate_store = CandidateStore(db_path=candidates_db, episode_store=episode_store)
        resources.callback(candidate_store.close)
        collector = ExperienceCollector(episode_store=episode_store, registry=registry)

        broker = ToolBroker(application_allowlist=allowlist)
        tool_names = register_scenario_tools(broker, scenario)

        runtime = AgentRuntime(
            db_path=runtime_db,
            tool_broker=broker,
            registry=registry,
            collector=collector,
            episode_store=episode_store,
            candidate_store=candidate_store,
        )
        resources.callback(runtime.close)

        # 4. Construct model_config
        model_config: dict[str, Any] = {
            "provider": provider,
            "model_id": model,
        }
        if _TEST_MODEL_CONFIG_HOOK is not None:
            model_config = _TEST_MODEL_CONFIG_HOOK(model_config)
        if model_config_extra:
            model_config.update(model_config_extra)

        is_test = test_mode
        if is_test is None:
            is_test = bool(
                "pytest" in sys.modules
                or os.environ.get("SKILLFORGE_ENV") == "test"
                or (model_config and (model_config.get("is_test") or model_config.get("test_mode")))
            )

        backend = PiEvaluationBackend(
            runtime=runtime,
            tool_names=tool_names,
            model_config=model_config,
            test_mode=is_test,
            budget_max=budget_max,
            system_prompt_header=system_prompt_header,
        )

        bundle = PiResourceBundle(
            scenario=scenario,
            registry=registry,
            episode_store=episode_store,
            candidate_store=candidate_store,
            broker=broker,
            collector=collector,
            runtime=runtime,
            backend=backend,
            tool_names=tool_names,
        )

        yield bundle
