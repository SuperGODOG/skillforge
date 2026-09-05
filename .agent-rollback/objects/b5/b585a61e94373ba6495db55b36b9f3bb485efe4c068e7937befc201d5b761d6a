"""SkillEvolver 元 Agent：候选生成器 + 初筛器（非最终决策者）

六步流程（ARCHITECTURE §4-D）:
    1. 收集失败样本（从 EvalResult.case_verdicts 取 B_better）
    2. LLM 根因分析（4 类标签：trigger/prompt/deps/boundary）
    3. LLM 生成候选 patch（3-5 个，标 L1/L2/L3）
    4. 沙箱验证（临时 skill dir + evaluator + 棘轮）
    5. 分级发布：L1 + PASS → 自动；L2/L3 或 REVIEW → 只出建议
    6. 归档：成功 → SQLite PUBLISHED；失败 → runs/failures/

分级标准（ARCHITECTURE §4-D）:
    L1 = 补 examples / not_for / description（不改语义边界）→ 可自动
    L2 = 改 trigger / Instructions（可能影响路由与行为）→ REVIEW
    L3 = 改 dependencies / 安全 Constraints（改权限/安全边界）→ 只建议

成功率坦诚约 30%：10 次迭代约 3 次通过棘轮。价值在负样本沉淀 + 评估闭环压测。
"""
from __future__ import annotations
import json
import re
import shutil
import tempfile
import time
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal, Optional

from hello_agents import SimpleAgent, HelloAgentsLLM

from .diff import compute_semantic_diff, _parse_skill_document
from .models import (
    Patch,
    EvalResult,
    RatchetVerdict,
    BudgetExceededError,
    EvolveBudget,
    EvolveContext,
    EvolveRecord,
    BodySectionStats,
)
from .evaluator.validators import validate_dependency_patch, validate_router_patch
from .evaluator.llm_factory import LLMLedger, wrap_with_ledger
from .evaluator.prompt_bloat import check_prompt_bloat, compute_body_section_stats, PromptBloatResult


# =============== 数据结构（evolver 内部） ===============


@dataclass
class Failure:
    case_id: str
    query: str
    reference: str
    output_skill: str
    output_baseline: str
    losing_dims: list[str] = field(default_factory=list)


@dataclass
class RootCause:
    label: Literal["trigger_inaccurate", "prompt_vague", "deps_broken", "boundary_missing"]
    prob: float
    why: str


@dataclass
class EvolveOutcome:
    """一次 evolve() 调用的产出汇总"""
    skill_name: str
    baseline_score: float
    patches_generated: int
    patches_published: list[str] = field(default_factory=list)  # release_ids
    patches_review: list[str] = field(default_factory=list)     # suggestion file paths
    patches_declined: list[str] = field(default_factory=list)   # failure log paths
    error: Optional[str] = None
    ledger: Optional[LLMLedger] = None
    context: Optional[EvolveContext] = None
    records: list[EvolveRecord] = field(default_factory=list)


# =============== SkillEvolver 主类 ===============


DEFAULT_SYSTEM_PROMPT = (
    "你是 SkillForge 的元 Agent。任务是分析 Skill 的失败样本，"
    "找出根因、生成 3-5 个改进候选 patch。改动分级：\n"
    "  L1 = 补 examples / not_for / description（不改语义边界）\n"
    "  L2 = 改 trigger / Instructions（可能影响路由与行为）\n"
    "  L3 = 改 dependencies / 安全 Constraints（改权限或安全边界）\n"
    "宁可不改也不冒进——低风险 L1 优先。"
)


class SkillEvolver(SimpleAgent):
    def __init__(
        self,
        registry,
        evaluator,
        llm: HelloAgentsLLM,
        state_machine=None,
        system_prompt: Optional[str] = None,
        budget: Optional[EvolveBudget] = None,
        ledger: Optional[LLMLedger] = None,
    ):
        """
        Args:
            registry:      SkillRegistry，读磁盘 body + 拿 meta
            evaluator:     SkillEvaluator，跑评估
            llm:           元 Agent 的 LLM（生成 patch + 根因分析）
            state_machine: ReleaseStateMachine，L1 自动发布用；None 时跳过发布只归档
            budget:        EvolveBudget，预算与随机性护栏配置（P1-F）
            ledger:        LLMLedger，统一记账单例（可选，未传入时从 budget 初始化）
        """
        super().__init__(
            name="skill_evolver",
            llm=llm,
            system_prompt=system_prompt or DEFAULT_SYSTEM_PROMPT,
        )
        self.registry = registry
        self.evaluator = evaluator
        self.state_machine = state_machine
        self.repo_root = registry.repo_root
        self.budget = budget or EvolveBudget()
        self.ledger = ledger or LLMLedger(budget=self.budget)
        self._bind_ledger(self.ledger)

    def _bind_ledger(self, ledger: LLMLedger) -> None:
        self.ledger = ledger
        self.llm = wrap_with_ledger(self.llm, ledger, role="evolver")
        if self.evaluator is not None:
            if hasattr(self.evaluator, "llm") and self.evaluator.llm is not None:
                self.evaluator.llm = wrap_with_ledger(self.evaluator.llm, ledger, role="agent")
            if (
                hasattr(self.evaluator, "judge")
                and self.evaluator.judge is not None
                and hasattr(self.evaluator.judge, "llm")
                and self.evaluator.judge.llm is not None
            ):
                self.evaluator.judge.llm = wrap_with_ledger(self.evaluator.judge.llm, ledger, role="judge")

    # -------- ARCHITECTURE §7 签名 --------
    def evolve(self, skill_name: str, max_candidates: int = 3) -> list[Patch]:
        outcome = self.evolve_full(skill_name, max_candidates=max_candidates)
        # 返回发布/建议的 patch 列表（此方法主要为兼容签名；实际请用 evolve_full）
        return outcome  # type: ignore

    # -------- 完整流程（推荐入口）--------
    def evolve_full(
        self,
        skill_name: str,
        max_candidates: int = 3,
        eval_set_for_iter: str = "repair_set",
        verbose: bool = True,
        budget: Optional[EvolveBudget] = None,
    ) -> EvolveOutcome:
        """
        对指定 Skill 跑完整六步迭代。

        Args:
            skill_name:         目标 skill
            max_candidates:     一次生成候选数（3-5）
            eval_set_for_iter:  迭代评估用哪个集（默认 repair_set 22 条，含 P0 与 seen regression）
            verbose:            打印进度
            budget:             可选单次运行覆盖的预算配置

        Returns:
            EvolveOutcome：本次迭代所有产出汇总
        """
        active_budget = budget or self.budget
        self.ledger.reset(budget=active_budget)
        self._bind_ledger(self.ledger)

        outcome = EvolveOutcome(
            skill_name=skill_name,
            baseline_score=0.0,
            patches_generated=0,
            ledger=self.ledger,
        )

        candidate_cap = active_budget.get_effective_candidate_limit(round_index=0, candidates_so_far=0)
        if (
            candidate_cap is not None
            and max_candidates > candidate_cap
            and active_budget.on_candidate_overflow == "reject"
        ):
            outcome.error = (
                f"候选生成预算硬帽超限：CANDIDATE_LIMIT_EXCEEDED: "
                f"requested {max_candidates} candidates > clamp {candidate_cap}"
            )
            return outcome

        # ---- 前置：跑一次 baseline 评估拿失败样本 ----
        if verbose:
            print(f"\n▶ [Evolve/1-baseline] 跑 baseline 评估 {skill_name} on {eval_set_for_iter}")
        try:
            old_result = self.evaluator.evaluate_skill(
                skill_name, eval_set=eval_set_for_iter, verbose=False,
            )
        except BudgetExceededError as e:
            outcome.error = f"baseline 评估预算硬帽超限：{e.reason}"
            return outcome
        except Exception as e:
            outcome.error = f"baseline 评估失败：{e}"
            return outcome
        if not old_result.valid:
            details = "; ".join(old_result.invalid_reasons) or "未知 INVALID 原因"
            outcome.error = f"baseline 评估无效，停止迭代：{details}"
            return outcome
        outcome.baseline_score = sum(old_result.structure_score.values()) + sum(old_result.effect_score.values())
        if verbose:
            print(f"  baseline 总分 = {outcome.baseline_score:.2f}")

        # ---- Step 1：收集失败 ----
        failures = _collect_failures(old_result)
        if verbose:
            print(f"\n▶ [Evolve/2-collect] 收集失败样本 {len(failures)} 条")
        if not failures:
            outcome.error = "无 B_better 失败样本，Skill 已达最优 → 跳过迭代"
            return outcome

        # ---- Step 2：根因分析 ----
        meta = self.registry.get_meta(skill_name)
        body = self.registry._bodies.get(skill_name, "")
        baseline_stats = compute_body_section_stats(body)
        context = EvolveContext(
            skill_name=skill_name,
            baseline_meta=meta,
            baseline_body=body,
            baseline_body_stats=baseline_stats,
            active_budget=active_budget,
        )
        outcome.context = context
        if verbose:
            print(f"\n▶ [Evolve/3-root_cause] LLM 根因分析")
        try:
            root_causes = _analyze_root_cause(self.llm, meta, body, failures)
        except BudgetExceededError as e:
            outcome.error = f"根因分析预算硬帽超限：{e.reason}"
            return outcome
        if verbose:
            for rc in root_causes:
                print(f"  {rc.label}: prob={rc.prob:.2f}, why={rc.why[:60]}")

        # ---- Step 3：生成候选 ----
        effective_max = (
            min(max_candidates, candidate_cap)
            if candidate_cap is not None
            else max_candidates
        )
        if verbose:
            print(f"\n▶ [Evolve/4-generate] LLM 生成 {effective_max} 个候选 patch")
        try:
            patches = _generate_patches(
                self.llm,
                meta,
                body,
                failures,
                root_causes,
                max_candidates=effective_max,
                clamp_limit=candidate_cap,
                on_candidate_overflow=active_budget.on_candidate_overflow,
                budget=active_budget,
            )
        except BudgetExceededError as e:
            outcome.error = f"候选生成预算硬帽超限：{e.reason}"
            return outcome
        outcome.patches_generated = len(patches)
        if verbose:
            print(f"  实际生成 {len(patches)} 个 patch")
            for p in patches:
                level_label = p.level
                if p.computed_level != p.level:
                    level_label = f"{p.level}→{p.computed_level}"
                print(f"    [{level_label}] {p.rationale[:60]}")
        if not patches:
            outcome.error = "LLM 未生成有效 patch（可能 JSON 解析失败）"
            return outcome

        # ---- Step 4-6：逐 patch 验证 + 发布 ----
        for i, patch in enumerate(patches):
            if verbose:
                print(f"\n▶ [Evolve/5-validate #{i + 1}] 沙箱验证 {patch.level} patch")
            try:
                new_result, verdict = _validate_patch(
                    self.evaluator, self.registry, skill_name, patch,
                    old_result, eval_set_for_iter,
                    budget=active_budget,
                )
            except BudgetExceededError as e:
                if verbose:
                    print(f"  ❌ 预算硬帽超限：{e.reason}")
                verdict = RatchetVerdict(decision="DECLINED", reasons=[f"BUDGET_EXCEEDED: {e.reason}"])
                path = _archive_failure(self.repo_root, skill_name, patch, verdict, None, str(e))
                outcome.patches_declined.append(str(path))
                outcome.error = f"沙箱验证预算硬帽超限：{e.reason}"
                break
            except Exception as e:
                if verbose:
                    print(f"  ❌ 验证异常：{e}")
                path = _archive_failure(self.repo_root, skill_name, patch, None, None, str(e))
                outcome.patches_declined.append(str(path))
                continue

            new_score = sum(new_result.structure_score.values()) + sum(new_result.effect_score.values())
            if verbose:
                print(f"  new_score={new_score:.2f}, verdict={verdict.decision}")
                for r in verdict.reasons[:3]:
                    print(f"    · {r}")

            # 分级发布
            outc = _publish_patch(
                repo_root=self.repo_root,
                registry=self.registry,
                state_machine=self.state_machine,
                skill_name=skill_name,
                patch=patch,
                verdict=verdict,
                new_result=new_result,
                budget=active_budget,
            )
            if verbose:
                print(f"  → {outc['status']}: {outc['path']}")

            try:
                cand_body = _parse_skill_document(patch.diff).body
            except Exception:
                cand_body = ""
            record = EvolveRecord(
                skill_name=skill_name,
                patch=patch,
                baseline_body=body,
                candidate_body=cand_body,
                baseline_body_stats=patch.baseline_body_stats or baseline_stats,
                candidate_body_stats=patch.candidate_body_stats,
                bloat_verdict=patch.bloat_verdict,
                bloat_reasons=patch.bloat_reasons,
                distillation_prompt=patch.distillation_prompt,
                status=outc["status"],
            )
            outcome.records.append(record)

            if outc["status"] == "PUBLISHED":
                outcome.patches_published.append(outc["release_id"])
            elif outc["status"] == "REVIEW":
                outcome.patches_review.append(outc["path"])
            else:  # DECLINED / SUGGESTION
                outcome.patches_declined.append(outc["path"])

            # L1 自动发布成功后，跳出（当轮已升级 skill；后续 patch 基于新版本得重跑）
            if outc["status"] == "PUBLISHED":
                if verbose:
                    print(f"\n  ⚡ L1 自动发布成功，本轮迭代结束")
                break

        return outcome


# =============== Step 1: 收集失败 ===============


def _collect_failures(result: EvalResult) -> list[Failure]:
    """从 EvalResult.case_verdicts 拉出 skill 版被判 B_better 的 case"""
    fail_list: list[Failure] = []
    outputs_by_id = {c["case_id"]: c for c in (result.case_outputs or [])}

    for v in (result.case_verdicts or []):
        losing = [
            dim for dim in ("task_completion", "robustness", "readability")
            if v.get(dim) == "B_better"
        ]
        if not losing:
            continue
        out = outputs_by_id.get(v["case_id"], {})
        fail_list.append(Failure(
            case_id=v["case_id"],
            query=v.get("query") or out.get("query", ""),
            reference=out.get("reference", ""),
            output_skill=out.get("output_skill", ""),
            output_baseline=out.get("output_baseline", ""),
            losing_dims=losing,
        ))
    return fail_list


# =============== Step 2: 根因分析 ===============


_ROOT_CAUSE_PROMPT = """你是 SkillForge 的元 Agent。分析 Skill 的失败样本，判定四类根因概率。

**Skill 定义**:
- name: {name}
- description: {desc}
- use_when: {use_when}
- not_for: {not_for}
- trigger.keywords: {kws}

**失败样本**（Skill 版被 baseline 打败的 case）:
{failures_block}

**四类根因**:
1. trigger_inaccurate: 触发词或 use_when 不准，Agent 弄错该何时用
2. prompt_vague: Instructions/Overview 模糊，Agent 不知具体怎么做
3. deps_broken: 声明的工具/依赖失效或缺失
4. boundary_missing: Constraints 未覆盖边界（历史查询、超范围数据、幻觉编造）

只输出严格 JSON（不加代码块标记）：
{{"trigger_inaccurate": {{"prob": <0-1>, "why": "..."}},
  "prompt_vague":        {{"prob": <0-1>, "why": "..."}},
  "deps_broken":         {{"prob": <0-1>, "why": "..."}},
  "boundary_missing":    {{"prob": <0-1>, "why": "..."}}}}
"""


def _analyze_root_cause(llm, meta, body: str, failures: list[Failure]) -> list[RootCause]:
    fb = "\n".join(
        f"[{f.case_id}] query='{f.query}'\n"
        f"  losing_dims={f.losing_dims}\n"
        f"  skill_out(200): {f.output_skill[:200]}\n"
        f"  baseline(200): {f.output_baseline[:200]}\n"
        for f in failures[:5]
    )
    prompt = _ROOT_CAUSE_PROMPT.format(
        name=meta.name, desc=meta.description, use_when=meta.use_when,
        not_for=meta.not_for, kws=meta.trigger.keywords,
        failures_block=fb,
    )
    resp = llm.invoke([{"role": "user", "content": prompt}])
    text = str(getattr(resp, "content", resp) or "")
    text = _strip_code_fence(text)
    try:
        data = json.loads(text)
    except Exception:
        return []
    causes = []
    for label, item in data.items():
        if label in ("trigger_inaccurate", "prompt_vague", "deps_broken", "boundary_missing"):
            causes.append(RootCause(
                label=label,  # type: ignore
                prob=float(item.get("prob", 0)),
                why=str(item.get("why", "")),
            ))
    causes.sort(key=lambda x: -x.prob)
    return causes


# =============== Step 3: 生成候选 ===============


_PATCH_GEN_PROMPT = """你是 SkillForge 的元 Agent。为 Skill 生成 {n} 个改进候选。

**当前 SKILL.md**:
```markdown
{skill_md}
```

**根因分析 top**:
{root_causes_block}

**失败样本**（供参考）:
{failures_block}

**改动分级规则（严格遵守）**:
- L1: 只允许改 YAML 里的 `examples` / `not_for` / `description` 三选一（不动 use_when / trigger / dependencies）；不改 Body 内容
- L2: 允许改 `trigger.keywords` 或 Body 的 `## Instructions` 段
- L3: 允许改 `dependencies` 或 Body 的 `## Constraints` 段

要求：
- 每个候选都必须是完整的、可直接落盘的 SKILL.md（frontmatter + Body 全）
- `name` 保持不变，`version` bump patch 段（如 1.0.0 → 1.0.1）
- 至少 2 个 L1（低风险优先）
- 每个 patch 附一句 rationale

只输出严格 JSON 数组（不加代码块标记），schema：
[
  {{"level": "L1|L2|L3", "rationale": "...", "new_skill_md": "---\\nname: ...\\n---\\n\\n## Overview\\n..."}},
  ...
]
"""


def _generate_patches(
    llm,
    meta,
    body: str,
    failures: list[Failure],
    root_causes: list[RootCause],
    max_candidates: int,
    clamp_limit: Optional[int] = None,
    on_candidate_overflow: Literal["truncate", "reject"] = "truncate",
    budget: Optional[EvolveBudget] = None,
) -> list[Patch]:
    effective_limit = min(max_candidates, clamp_limit) if clamp_limit is not None else max_candidates
    # 拼当前 SKILL.md
    skill_md = _reconstruct_skill_md(meta, body)
    fb = "\n".join(
        f"[{f.case_id}] {f.query} (losing: {','.join(f.losing_dims)})"
        for f in failures[:5]
    )
    rc = "\n".join(f"- {c.label}: prob={c.prob:.2f} - {c.why[:100]}" for c in root_causes[:3])

    prompt = _PATCH_GEN_PROMPT.format(
        n=effective_limit, skill_md=skill_md,
        root_causes_block=rc, failures_block=fb,
    )
    resp = llm.invoke([{"role": "user", "content": prompt}])
    text = _strip_code_fence(str(getattr(resp, "content", resp) or ""))
    try:
        data = json.loads(text)
    except Exception:
        return []
    if not isinstance(data, list):
        return []

    if len(data) > effective_limit:
        if on_candidate_overflow == "reject":
            raise BudgetExceededError(
                f"CANDIDATE_LIMIT_EXCEEDED: LLM returned {len(data)} candidates > clamp {effective_limit}",
                cap_type="candidate",
                limit=effective_limit,
                current=len(data),
            )
        data = data[:effective_limit]

    patches = []
    for item in data:
        if not isinstance(item, dict):
            continue
        raw_level = item.get("level", "")
        if not isinstance(raw_level, str):
            continue
        level = raw_level.upper()
        if level not in ("L1", "L2", "L3"):
            continue
        raw_new_md = item.get("new_skill_md", "")
        if not isinstance(raw_new_md, str):
            continue
        new_md = raw_new_md.strip()
        if not new_md or not new_md.startswith("---"):
            continue
        semantic_diff = compute_semantic_diff(skill_md, new_md, level)
        if not semantic_diff.is_valid:
            continue

        cand_parsed = _parse_skill_document(new_md)
        bloat_result = check_prompt_bloat(
            body,
            cand_parsed.body,
            budget=budget,
            changed_sections=semantic_diff.changed_body_sections,
        )
        patches.append(Patch(
            skill_name=meta.name,
            level=level,  # type: ignore
            diff=new_md,  # 存完整新 SKILL.md 作为 patch payload
            rationale=str(item.get("rationale", ""))[:200],
            computed_level=semantic_diff.computed_level,
            unified_diff=semantic_diff.unified_diff,
            downgrade_attempt=semantic_diff.downgrade_attempt,
            changed_frontmatter=semantic_diff.changed_frontmatter,
            changed_body_sections=semantic_diff.changed_body_sections,
            baseline_body_stats=bloat_result.baseline_stats,
            candidate_body_stats=bloat_result.candidate_stats,
            bloat_verdict=bloat_result.decision,
            bloat_reasons=bloat_result.reasons,
            distillation_prompt=bloat_result.distillation_prompt,
        ))

    if len(patches) > effective_limit:
        if on_candidate_overflow == "reject":
            raise BudgetExceededError(
                f"CANDIDATE_LIMIT_EXCEEDED: parsed {len(patches)} valid patches > clamp {effective_limit}",
                cap_type="candidate",
                limit=effective_limit,
                current=len(patches),
            )
        patches = patches[:effective_limit]

    return patches


# =============== Step 4: 沙箱验证 ===============


def _validate_patch(
    evaluator, registry, skill_name: str, patch: Patch,
    old_result: EvalResult, eval_set: str,
    budget: Optional[EvolveBudget] = None,
) -> tuple[EvalResult, RatchetVerdict]:
    """Validate a candidate through channels selected by its change surface."""
    from .registry import SkillRegistry
    from .evaluator.ratchet import check_ratchet

    repo_root = registry.repo_root
    metadata_changed = bool(
        set(patch.changed_frontmatter) - {"dependencies"}
    )
    body_changed = bool(patch.changed_body_sections)
    dependencies_changed = "dependencies" in patch.changed_frontmatter
    channels: list[str] = []
    provenances = []

    # 提取基线与候选 Body 并计算分段统计与 Prompt Bloat 护栏
    old_body = getattr(registry, "_bodies", {}).get(skill_name, "")
    if not old_body and hasattr(registry, "skills_dir"):
        skill_file = registry.skills_dir / skill_name / "SKILL.md"
        if skill_file.exists():
            try:
                old_body = _parse_skill_document(skill_file.read_text(encoding="utf-8")).body
            except Exception:
                pass

    try:
        cand_body = _parse_skill_document(patch.diff).body
    except Exception:
        cand_body = ""

    bloat_res = check_prompt_bloat(
        old_body,
        cand_body,
        budget=budget,
        changed_sections=patch.changed_body_sections,
    )
    patch.baseline_body_stats = bloat_res.baseline_stats
    patch.candidate_body_stats = bloat_res.candidate_stats
    patch.bloat_verdict = bloat_res.decision
    patch.bloat_reasons = bloat_res.reasons
    patch.distillation_prompt = bloat_res.distillation_prompt

    if bloat_res.decision == "DECLINED":
        result = replace(
            old_result,
            release_id="candidate",
            validation_channels=channels,
            provenances=list(provenances),
        )
        return result, RatchetVerdict(decision="DECLINED", reasons=bloat_res.reasons)

    with tempfile.TemporaryDirectory(prefix="skillforge-validator-") as temp_dir:
        sandbox_root = Path(temp_dir)
        sandbox_skills = sandbox_root / "skills"
        shutil.copytree(registry.skills_dir, sandbox_skills)
        (sandbox_skills / skill_name / "SKILL.md").write_text(
            patch.diff, encoding="utf-8"
        )

        temp_reg = SkillRegistry(
            db_path=sandbox_root / "sandbox.db",
            skills_dir=sandbox_skills,
            # Evaluation sets remain read-only assets from the real repository.
            repo_root=repo_root,
            router_log=sandbox_root / "router.jsonl",
        )
        temp_reg.load_skills_from_dir()
        try:
            if metadata_changed:
                channels.append("router")
                router_verdict = validate_router_patch(
                    registry,
                    temp_reg,
                    skill_name,
                    changed_frontmatter=patch.changed_frontmatter,
                    computed_level=patch.computed_level,
                )
                if router_verdict.decision != "PASS":
                    result = replace(
                        old_result,
                        release_id="candidate",
                        validation_channels=channels,
                    )
                    return result, router_verdict

            if dependencies_changed:
                channels.append("dependency_fixture")
                dependency_verdict, provenances = validate_dependency_patch(
                    registry, temp_reg, skill_name, llm=evaluator.llm
                )
                patch.provenances = list(provenances)
                if dependency_verdict.decision != "PASS":
                    result = replace(
                        old_result,
                        release_id="candidate",
                        validation_channels=channels,
                        provenances=list(provenances),
                    )
                    return result, dependency_verdict

            if body_changed:
                channels.append("behavior")
                from .evaluator import SkillEvaluator

                judge_llm = getattr(getattr(evaluator, "judge", None), "llm", None)
                cache_obj = getattr(evaluator, "output_cache", None)
                try:
                    temp_eval = SkillEvaluator(
                        registry=temp_reg,
                        llm=getattr(evaluator, "llm", None),
                        judge_llm=judge_llm,
                        output_cache=cache_obj,
                    )
                except TypeError:
                    temp_eval = SkillEvaluator(
                        registry=temp_reg,
                        llm=getattr(evaluator, "llm", None),
                        judge_llm=judge_llm,
                    )
                    if hasattr(temp_eval, "output_cache"):
                        temp_eval.output_cache = cache_obj
                cases = temp_eval._load_cases(eval_set, skill_name)
                new_result = temp_eval.evaluate_skill(
                    skill_name,
                    eval_set=eval_set,
                    cases=cases,
                    verbose=False,
                )
                new_result.validation_channels = channels
                new_result.provenances = list(provenances)

                from .evaluator.p0_gate import (
                    evaluate_p0_gate,
                    check_p0_ratchet_verdict,
                    merge_p0_into_eval_result,
                )
                p0_result = evaluate_p0_gate(temp_eval, skill_name, repo_root=repo_root, verbose=False)
                new_result = merge_p0_into_eval_result(new_result, p0_result)
                p0_verdict = check_p0_ratchet_verdict(p0_result)
                if p0_verdict.decision != "PASS":
                    return new_result, p0_verdict

                ratchet_verdict = check_ratchet(old_result, new_result)
                if bloat_res.decision == "REVIEW":
                    if ratchet_verdict.decision == "DECLINED":
                        combined_reasons = list(ratchet_verdict.reasons) + bloat_res.reasons
                        return new_result, RatchetVerdict(decision="DECLINED", reasons=combined_reasons)
                    combined_reasons = list(ratchet_verdict.reasons) + bloat_res.reasons
                    return new_result, RatchetVerdict(decision="REVIEW", reasons=combined_reasons)

                return new_result, ratchet_verdict

            result = replace(
                old_result,
                release_id="candidate",
                validation_channels=channels,
                provenances=list(provenances),
            )
            if not channels:
                return result, RatchetVerdict(
                    decision="DECLINED",
                    reasons=["未识别到可验证的改动面，按 fail-closed 拒绝"],
                )
            if not getattr(old_result, "valid", True) or not getattr(old_result, "p0_pass", True):
                return result, RatchetVerdict(
                    decision="DECLINED",
                    reasons=["基线未通过 P0 门或评估无效，按 fail-closed 拒绝放行 L1 补丁"],
                )

            # L1 metadata-only 路径接入独立 P0 门：不再仅复用 old_result.p0_pass
            if metadata_changed:
                p0_gate_result = getattr(old_result, "p0_gate_result", None)
                is_valid_p0_proof = False
                if (
                    p0_gate_result is not None
                    and getattr(p0_gate_result, "valid", False)
                    and getattr(p0_gate_result, "p0_pass", False)
                    and getattr(p0_gate_result, "expected_count", 0) > 0
                ):
                    try:
                        from .evaluator.p0_gate import load_p0_cases
                        expected_cases = load_p0_cases(repo_root, skill_name=skill_name)
                        expected_ids = {c["id"] for c in expected_cases}
                        executed_ids = {
                            v.get("case_id")
                            for v in getattr(p0_gate_result, "case_verdicts", [])
                            if v.get("case_id")
                        }
                        if (
                            expected_ids
                            and expected_ids.issubset(executed_ids)
                            and getattr(p0_gate_result, "executed_count", 0) >= len(expected_ids)
                        ):
                            is_valid_p0_proof = True
                    except Exception:
                        is_valid_p0_proof = False

                if is_valid_p0_proof:
                    from .evaluator.p0_gate import check_p0_ratchet_verdict, merge_p0_into_eval_result
                    result = merge_p0_into_eval_result(result, p0_gate_result)
                    p0_verdict = check_p0_ratchet_verdict(p0_gate_result)
                    if p0_verdict.decision != "PASS":
                        return result, p0_verdict
                else:
                    from .evaluator import SkillEvaluator
                    from .evaluator.p0_gate import (
                        evaluate_p0_gate,
                        check_p0_ratchet_verdict,
                        merge_p0_into_eval_result,
                    )
                    judge_llm = getattr(getattr(evaluator, "judge", None), "llm", None)
                    cache_obj = getattr(evaluator, "output_cache", None)
                    try:
                        temp_eval = SkillEvaluator(
                            registry=temp_reg,
                            llm=getattr(evaluator, "llm", None),
                            judge_llm=judge_llm,
                            output_cache=cache_obj,
                        )
                    except TypeError:
                        temp_eval = SkillEvaluator(
                            registry=temp_reg,
                            llm=getattr(evaluator, "llm", None),
                            judge_llm=judge_llm,
                        )
                        if hasattr(temp_eval, "output_cache"):
                            temp_eval.output_cache = cache_obj
                    p0_result = evaluate_p0_gate(temp_eval, skill_name, repo_root=repo_root, verbose=False)
                    result = merge_p0_into_eval_result(result, p0_result)
                    p0_verdict = check_p0_ratchet_verdict(p0_result)
                    if p0_verdict.decision != "PASS":
                        return result, p0_verdict

            if bloat_res.decision == "REVIEW":
                return result, RatchetVerdict(decision="REVIEW", reasons=bloat_res.reasons)
            return result, RatchetVerdict(decision="PASS", reasons=[])

        finally:
            temp_reg.close()


# =============== Step 5: 分级发布 + Step 6: 归档 ===============


def _publish_patch(
    repo_root: Path,
    registry,
    state_machine,
    skill_name: str,
    patch: Patch,
    verdict: RatchetVerdict,
    new_result: EvalResult,
    budget: Optional[EvolveBudget] = None,
) -> dict:
    """
    分级发布：
      declared L1 + computed L1 + no downgrade + PASS → 自动发布
      L1 + REVIEW → 挂 REVIEW（保留建议不覆盖 skill）
      L2/L3      → 无论 PASS/REVIEW 都只出建议（Phase 4 收敛到 L1 auto）
      任何 DECLINED → 归档到 runs/failures/

    Returns: {"status": "PUBLISHED/REVIEW/SUGGESTION/DECLINED", "path/release_id": ...}
    """
    if verdict.decision == "DECLINED":
        path = _archive_failure(repo_root, skill_name, patch, verdict, new_result, None)
        patch.status = "DECLINED"
        return {"status": "DECLINED", "path": str(path)}

    bloat_active = (
        getattr(patch, "bloat_verdict", None) == "REVIEW"
        or any("PROMPT_BLOAT" in (r or "") for r in verdict.reasons)
    )

    # PASS 或 REVIEW，看声明等级与确定性计算等级的联合门禁。
    if (
        patch.level == "L1"
        and patch.computed_level == "L1"
        and not patch.downgrade_attempt
        and verdict.decision == "PASS"
        and not bloat_active
        and state_machine is not None
    ):
        # 自动发布
        try:
            release_id = _apply_and_publish_L1(
                repo_root, registry, state_machine, skill_name, patch, new_result,
            )
            patch.status = "PUBLISHED"
            return {"status": "PUBLISHED", "release_id": release_id, "path": ""}
        except Exception as e:
            path = _archive_failure(repo_root, skill_name, patch, verdict, new_result, f"发布失败: {e}")
            patch.status = "DECLINED"
            return {"status": "DECLINED", "path": str(path)}

    # 其他情况：只出建议
    path = _archive_suggestion(repo_root, skill_name, patch, verdict, new_result)
    status = (
        "REVIEW"
        if (
            verdict.decision == "REVIEW"
            or bloat_active
            or patch.downgrade_attempt
            or patch.computed_level == "L3"
        )
        else "SUGGESTION"
    )
    patch.status = status
    return {"status": status, "path": str(path)}


def _apply_and_publish_L1(
    repo_root: Path, registry, state_machine,
    skill_name: str, patch: Patch, new_result: EvalResult,
) -> str:
    """L1 自动发布：写新 SKILL.md 到 skills/<name>/ + state_machine 4 步"""
    # 恢复 patch.diff 里的 name 为原 skill_name（validator 里改成 candidate 了；这里回改）
    new_md = re.sub(
        r"^name:\s*.+$", f"name: {skill_name}",
        patch.diff, count=1, flags=re.MULTILINE,
    )
    skill_md_path = repo_root / "skills" / skill_name / "SKILL.md"
    skill_md_path.write_text(new_md, encoding="utf-8")

    # state_machine 4 步
    # 提取新版本号
    m = re.search(r"^version:\s*(\S+)", new_md, flags=re.MULTILINE)
    version = m.group(1) if m else "1.0.1"

    rid = state_machine.begin_release(skill_name, version, "L1")
    state_machine.write_commit(rid, patch)
    # 更新 EvalResult 的 release_id
    new_result.release_id = rid
    state_machine.append_evaluation(rid, new_result)
    state_machine.commit_release(rid)
    return rid


def _archive_suggestion(
    repo_root: Path, skill_name: str, patch: Patch,
    verdict: RatchetVerdict, new_result: EvalResult,
) -> Path:
    sug_dir = repo_root / "runs" / "suggestions"
    sug_dir.mkdir(parents=True, exist_ok=True)
    fname = f"{datetime.now(timezone.utc).strftime('%Y%m%d-%H%M%S')}-{skill_name}-{patch.level}.md"
    path = sug_dir / fname
    total = sum(new_result.structure_score.values()) + sum(new_result.effect_score.values())
    title = f"[{patch.level} 建议] {skill_name}"
    if patch.downgrade_attempt:
        title = (
            f"[REVIEW / 降级拦截 / {patch.level}→{patch.computed_level}] "
            f"{skill_name}"
        )
    elif getattr(patch, "bloat_verdict", None) == "REVIEW" or any("PROMPT_BLOAT" in (r or "") for r in verdict.reasons):
        title = f"[REVIEW / PROMPT_BLOAT / {patch.level}] {skill_name}"
    path.write_text(_render_archive_md(
        title=title,
        patch=patch, verdict=verdict, new_score=total, error=None,
    ), encoding="utf-8")
    return path


def _archive_failure(
    repo_root: Path, skill_name: str, patch: Patch,
    verdict: Optional[RatchetVerdict], new_result: Optional[EvalResult],
    error: Optional[str],
) -> Path:
    fail_dir = repo_root / "runs" / "failures"
    fail_dir.mkdir(parents=True, exist_ok=True)
    fname = f"{datetime.now(timezone.utc).strftime('%Y%m%d-%H%M%S')}-{skill_name}-{patch.level}.md"
    path = fail_dir / fname
    total = None
    if new_result is not None:
        total = sum(new_result.structure_score.values()) + sum(new_result.effect_score.values())
    title = f"[DECLINED / {patch.level}] {skill_name}"
    if any("BUDGET_EXCEEDED" in (r or "") for r in (verdict.reasons if verdict else [])):
        title = f"[DECLINED / BUDGET_EXCEEDED / {patch.level}] {skill_name}"
    elif any("PROMPT_BLOAT" in (r or "") for r in (verdict.reasons if verdict else [])):
        title = f"[DECLINED / PROMPT_BLOAT / {patch.level}] {skill_name}"
    path.write_text(_render_archive_md(
        title=title,
        patch=patch, verdict=verdict, new_score=total, error=error,
    ), encoding="utf-8")
    return path


def _render_archive_md(
    title: str, patch: Patch,
    verdict: Optional[RatchetVerdict], new_score: Optional[float], error: Optional[str],
) -> str:
    lines = [f"# {title}", ""]
    lines.append(f"- ts: {datetime.now(timezone.utc).isoformat()}")
    lines.append(f"- level: {patch.level}")
    lines.append(f"- declared_level: {patch.level}")
    lines.append(f"- computed_level: {patch.computed_level}")
    lines.append(f"- downgrade_attempt: {str(patch.downgrade_attempt).lower()}")
    lines.append(f"- level_decision: {_level_decision_reason(patch)}")
    lines.append(f"- rationale: {patch.rationale}")
    if new_score is not None:
        lines.append(f"- new_score: {new_score:.2f} / 100")
    if verdict is not None:
        lines.append(f"- ratchet: {verdict.decision}")
        if verdict.reasons:
            lines.append("- reasons:")
            for r in verdict.reasons:
                lines.append(f"  - {r}")
    if getattr(patch, "distillation_prompt", None):
        lines.append(f"- distillation_prompt: {patch.distillation_prompt}")
    if getattr(patch, "baseline_body_stats", None) or getattr(patch, "candidate_body_stats", None):
        lines.append(f"- baseline_body_stats: {json.dumps(patch.baseline_body_stats, ensure_ascii=False)}")
        lines.append(f"- candidate_body_stats: {json.dumps(patch.candidate_body_stats, ensure_ascii=False)}")
    if error:
        lines.append(f"- error: {error}")
    lines.append("")
    if patch.provenances:
        lines.append("## 工具调用存据 (Tool Call Provenance)")
        lines.append("")
        for provenance in patch.provenances:
            params = json.dumps(
                provenance.input_params, ensure_ascii=False, sort_keys=True
            )
            lines.append(
                f"- 工具: `{provenance.tool_name}` "
                f"(Fixture: {str(provenance.is_fixture).lower()})"
            )
            lines.append(f"  - Fixture case: `{provenance.fixture_case_id}`")
            lines.append(
                f"  - 调用序号: {provenance.call_index}/{provenance.call_count}"
            )
            lines.append(
                "  - 执行事实: "
                f"required={str(provenance.tool_required).lower()}, "
                f"called={str(provenance.tool_called).lower()}, "
                f"success={str(provenance.tool_success).lower()}, "
                f"authenticity={str(provenance.authenticity_pass).lower()}"
            )
            lines.append(f"  - 参数: `{params}`")
            lines.append(
                f"  - 状态: {provenance.output_status} "
                f"({provenance.latency_ms:.2f} ms)"
            )
            lines.append(f"  - 时间: {provenance.timestamp}")
            lines.append(f"  - 输出摘要: {provenance.output_summary}")
            lines.append(f"  - 签名: `sha256:{provenance.signature}`")
        lines.append("")
    unified_fence = _markdown_fence(patch.unified_diff)
    lines.append("## 差异对比 (Unified Diff)")
    lines.append(f"{unified_fence}diff")
    lines.append(patch.unified_diff or "（无可用 unified diff）")
    lines.append(unified_fence)
    lines.append("")
    markdown_fence = _markdown_fence(patch.diff)
    lines.append("## 完整新 SKILL.md")
    lines.append(f"{markdown_fence}markdown")
    lines.append(patch.diff)
    lines.append(markdown_fence)
    return "\n".join(lines) + "\n"


def _level_decision_reason(patch: Patch) -> str:
    if patch.downgrade_attempt:
        return (
            f"实际改动为 {patch.computed_level}，高于模型声明 {patch.level}；"
            "已阻断自动发布并转人工复核"
        )
    if patch.computed_level == patch.level:
        return f"模型声明与确定性计算一致（{patch.computed_level}）"
    if patch.computed_level == "INVALID":
        return "缺少可信语义分级；按 fail-closed 禁止自动发布"
    return (
        f"模型声明 {patch.level} 高于实际计算 {patch.computed_level}；"
        "保留较审慎的声明等级"
    )


def _markdown_fence(content: str) -> str:
    longest_run = max(
        (len(match.group(0)) for match in re.finditer(r"`+", content)),
        default=0,
    )
    return "`" * max(4, longest_run + 1)


# =============== 工具函数 ===============


def _reconstruct_skill_md(meta, body: str) -> str:
    """把 SkillMeta + body 拼回完整 SKILL.md（供 LLM 参考）"""
    import yaml
    fm = {
        "name": meta.name,
        "version": meta.version,
        "description": meta.description,
        "use_when": meta.use_when,
        "not_for": meta.not_for,
        "dependencies": meta.dependencies,
        "trigger": {"keywords": meta.trigger.keywords},
        "examples": meta.examples,
    }
    return f"---\n{yaml.safe_dump(fm, allow_unicode=True, sort_keys=False).strip()}\n---\n\n{body}"


def _strip_code_fence(text: str) -> str:
    """LLM 输出常带 ```json ... ``` 包裹，去掉外围"""
    text = text.strip()
    m = re.match(r"^```(?:json|markdown)?\s*\n(.*)\n```\s*$", text, flags=re.DOTALL)
    return m.group(1) if m else text
