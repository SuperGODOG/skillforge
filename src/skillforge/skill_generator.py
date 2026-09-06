"""SkillGenerator · 文档型/无工具域 Skill 生成器 (P2-A)

输入自然语言需求 → 输出可注册可评估的 SKILL.md + 初始测试集，完成评测注册闭环。
- 段 1 生成器：LLM 生成、结构校验 (fail-closed)、路由冲突校验 (embedding/keyword/LLM)
- 段 2 注册接入：落盘 skills/<name>/SKILL.md 与 repair_set.json
"""
from __future__ import annotations

import json
import os
import re
import tempfile
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

import yaml

from .evaluator.llm_factory import LLMLedger, TrackedLLM
from .models import BudgetExceededError, EvolveBudget, SkillMeta
from .registry import _FRONTMATTER_RE, SkillRegistry


NAME_PATTERN = re.compile(r"^[a-z][a-z0-9_]{2,31}$")
SECTION_OVERVIEW_RE = re.compile(r"^\s*##\s*Overview\s*$", re.MULTILINE | re.IGNORECASE)
SECTION_INSTRUCTIONS_RE = re.compile(r"^\s*##\s*Instructions\s*$", re.MULTILINE | re.IGNORECASE)
SECTION_EXAMPLES_RE = re.compile(r"^\s*##\s*Examples\s*$", re.MULTILINE | re.IGNORECASE)
SECTION_CONSTRAINTS_RE = re.compile(r"^\s*##\s*(Constraints?|约束)\s*$", re.MULTILINE | re.IGNORECASE)

DEFAULT_SIMILARITY_THRESHOLD = 0.70
DEFAULT_GENERATOR_MAX_CALLS = 4
DEFAULT_GENERATOR_MAX_TOKENS = 20_000
DEFAULT_GENERATOR_DEADLINE_SECONDS = 300.0
DEFAULT_GENERATOR_MAX_RETRIES = 1
MAX_REPAIR_AUTO_RATIO = 0.50

_ALLOWED_CONFLICT_METHODS = frozenset({"embedding", "llm"})
_FRONTMATTER_FIELDS = frozenset(
    {
        "name",
        "version",
        "description",
        "use_when",
        "not_for",
        "dependencies",
        "trigger",
        "examples",
        "evaluation",
    }
)
_SECTION_ALIASES = {
    "overview": "Overview",
    "概述": "Overview",
    "overview 概述": "Overview",
    "instruction": "Instructions",
    "instructions": "Instructions",
    "说明": "Instructions",
    "使用说明": "Instructions",
    "instructions 说明": "Instructions",
    "example": "Examples",
    "examples": "Examples",
    "示例": "Examples",
    "examples 示例": "Examples",
    "constraint": "Constraints",
    "constraints": "Constraints",
    "约束": "Constraints",
    "constraints 约束": "Constraints",
}
_HEADING_RE = re.compile(r"^ {0,3}(?P<marks>#{1,6})[ \t]+(?P<title>.*?)[ \t]*$")
_FENCE_RE = re.compile(r"^ {0,3}(?P<marker>`{3,}|~{3,})(?:[^`~]*)$")
_SETEXT_RE = re.compile(r"^ {0,3}(?:=+|-+)\s*$")
_HTML_HEADING_RE = re.compile(r"^ {0,3}<h[1-6](?:\s|>)", re.IGNORECASE)


class RegistrationError(RuntimeError):
    """Raised when a Skill registration cannot be committed safely."""


@dataclass
class GeneratedSkill:
    """生成成功的 Skill 产物"""
    name: str
    version: str
    description: str
    use_when: str
    not_for: list[str]
    frontmatter_raw: str
    body_raw: str
    full_skill_md: str
    test_cases: list[dict[str, Any]]
    meta: SkillMeta
    skill_dir: Optional[Path] = None
    skill_file: Optional[Path] = None
    registered: bool = False
    success: bool = True

    def is_success(self) -> bool:
        return self.success


@dataclass
class GenerationFailure:
    """生成或校验失败结果 (fail-closed)"""
    reason: str  # CONFLICT, INVALID_STRUCTURE, DUPLICATE_NAME, LLM_ERROR
    message: str
    details: Optional[dict[str, Any]] = None
    success: bool = False

    def is_success(self) -> bool:
        return False


def derive_skill_abbrev(name: str) -> str:
    """从 skill 蛇形命名生成测试用例前缀缩写，如 http_status_code -> hsc"""
    parts = [p for p in name.split("_") if p]
    if len(parts) >= 2:
        return "".join(p[0] for p in parts)
    return name[:3] if len(name) >= 3 else name


def _case_prefix(case_id: str) -> Optional[str]:
    """Return the stable prefix before ``_auto_`` or ``None`` for source IDs."""
    if not isinstance(case_id, str) or "_auto_" not in case_id:
        return None
    prefix, suffix = case_id.split("_auto_", 1)
    if not prefix or not suffix or not suffix.isdigit():
        return None
    return prefix


def _clean_json_text(text: str) -> str:
    """去除 LLM 回复外层 Markdown 代码块，并拒绝未闭合的 fenced response。"""
    if not isinstance(text, str):
        raise TypeError("LLM 响应 content 必须是字符串")
    s = text.strip()
    if not s.startswith("```"):
        return s

    lines = s.splitlines()
    if not lines or not re.fullmatch(r"\s*```(?:json)?\s*", lines[0], re.IGNORECASE):
        raise ValueError("LLM JSON fenced block 的起始标记不合法")
    if len(lines) < 3 or not re.fullmatch(r"\s*```\s*", lines[-1]):
        raise ValueError("LLM JSON fenced block 未闭合")
    return "\n".join(lines[1:-1]).strip()


def _strip_fenced_blocks(text: str) -> str:
    """Remove fenced Markdown blocks for structural heading inspection only."""
    visible: list[str] = []
    active: Optional[tuple[str, int]] = None
    for line in text.splitlines():
        match = _FENCE_RE.match(line)
        if active is None:
            if match:
                marker = match.group("marker")
                active = (marker[0], len(marker))
                continue
            visible.append(line)
            continue

        if match:
            marker = match.group("marker")
            if marker[0] == active[0] and len(marker) >= active[1]:
                active = None
        # Contents inside a fence are deliberately invisible to the parser.
    if active is not None:
        # Keep an explicit sentinel so a body containing only an unclosed fence
        # cannot accidentally pass as four valid sections.
        visible.append("__UNTERMINATED_FENCED_BLOCK__")
    return "\n".join(visible)


def _normalise_section_title(title: str) -> str:
    title = re.sub(r"[()\[\]{}（）【】]", " ", title)
    title = re.sub(r"[:：]+$", "", title)
    title = re.sub(r"[\s\u3000]+", " ", title.strip().rstrip("#").strip())
    return title.casefold()


def _validate_body_sections(body: str) -> tuple[bool, str]:
    """Parse real top-level headings, accepting only explicit canonical aliases."""
    visible_body = _strip_fenced_blocks(body)
    if "__UNTERMINATED_FENCED_BLOCK__" in visible_body:
        return False, "Body 存在未闭合的 fenced block"
    headings: list[tuple[int, int, str, str]] = []
    visible_lines = visible_body.splitlines()
    for line_no, line in enumerate(visible_lines):
        match = _HEADING_RE.match(line)
        if not match:
            # Setext and raw HTML headings are intentionally not accepted as
            # section syntax.  Otherwise a fake top-level section can evade
            # the exact four-section contract while looking like Markdown.
            if _HTML_HEADING_RE.match(line):
                return False, f"Body 存在额外顶层节或未知段名: {line.strip()}"
            if line_no + 1 < len(visible_lines) and line.strip() and _SETEXT_RE.match(visible_lines[line_no + 1]):
                return False, f"Body 存在不支持的 setext 顶层节: {line.strip()}"
            continue
        level = len(match.group("marks"))
        if level > 2:
            continue
        raw_title = match.group("title").strip().rstrip("#").strip()
        canonical = _SECTION_ALIASES.get(_normalise_section_title(raw_title))
        if level != 2 or canonical is None:
            return False, f"Body 存在额外顶层节或未知段名: {line.strip()}"
        headings.append((line_no, level, canonical, raw_title))

    expected = ["Overview", "Instructions", "Examples", "Constraints"]
    actual = [heading[2] for heading in headings]
    if actual != expected:
        missing = [name for name in expected if name not in actual]
        if missing:
            return False, f"Body 缺少必须的段落: {', '.join(f'## {x}' for x in missing)}"
        return False, f"Body 四段必须严格按顺序且各出现一次，实际为: {actual}"

    for index, (line_no, _, canonical, _) in enumerate(headings):
        next_line = headings[index + 1][0] if index + 1 < len(headings) else len(visible_lines)
        content = "\n".join(visible_lines[line_no + 1:next_line]).strip()
        if not content or content == "__UNTERMINATED_FENCED_BLOCK__":
            return False, f"Body 段落 ## {canonical} 内容为空"
    return True, "OK"


def check_route_conflict_embedding(
    candidate_meta: SkillMeta,
    existing_metas: list[SkillMeta],
    threshold: float = DEFAULT_SIMILARITY_THRESHOLD,
    model_dir: Optional[Path] = None,
) -> tuple[bool, str]:
    """使用 bge-small-zh-v1.5 编码检索卡片，判定候选 skill 是否与现有 skill 发生路由语义冲突。

    Returns:
        (has_conflict, reason_message)
    """
    if not existing_metas:
        return False, "无现有 skill，无需冲突检测"

    try:
        from .router.embed import EmbedLayer

        embed = EmbedLayer(model_dir=model_dir)
        embed.index_skills(existing_metas)
        candidate_card = embed.encode_card(candidate_meta)
        search_results = embed.search(candidate_card, top_k=3)

        if not search_results:
            return True, "Embedding 检查未返回结果，按 fail-closed 拒绝生成"

        top_skill, top_sim = search_results[0]
        if top_sim >= threshold:
            return (
                True,
                f"与现有 Skill '{top_skill}' 存在语义路由冲突 (相似度 {top_sim:.4f} >= 阈值 {threshold})",
            )
        return False, f"语义相似度正常 (最高与 '{top_skill}' 相似度 {top_sim:.4f} < {threshold})"
    except Exception as exc:
        return True, f"Embedding 检查异常，按 fail-closed 拒绝生成: {type(exc).__name__}: {exc}"


def check_route_conflict_keywords(
    candidate_meta: SkillMeta,
    existing_metas: list[SkillMeta],
) -> tuple[bool, str]:
    """判定 candidate 的 trigger keywords 是否与现有 skills 严重撞车"""
    cand_kws = {kw.strip().lower() for kw in candidate_meta.trigger.keywords if kw and kw.strip()}
    for ex in existing_metas:
        ex_kws = {kw.strip().lower() for kw in ex.trigger.keywords if kw and kw.strip()}
        overlap = cand_kws & ex_kws
        if overlap:
            return True, f"Trigger 关键词与现有 Skill '{ex.name}' 重叠冲突: {sorted(overlap)}"
    return False, "关键词无重叠"


def check_route_conflict_llm(
    candidate_meta: SkillMeta,
    existing_metas: list[SkillMeta],
    llm: Any,
) -> tuple[bool, str]:
    """LLM 判定候选 skill 是否与现有 skills 产生意图冲突"""
    existing_info = []
    for s in existing_metas:
        existing_info.append(
            f"- Name: {s.name}\n  Description: {s.description}\n  Use When: {s.use_when}\n  Not For: {', '.join(s.not_for)}"
        )
    candidate_info = (
        f"Name: {candidate_meta.name}\nDescription: {candidate_meta.description}\n"
        f"Use When: {candidate_meta.use_when}\nNot For: {', '.join(candidate_meta.not_for)}"
    )

    prompt = f"""现有已注册技能：
{chr(10).join(existing_info)}

新生成的候选技能：
{candidate_info}

请判断新技能与现有技能是否在核心职责、路由意图（use_when）上存在实质性冲突或重大重叠？
只回答 JSON：{{"conflict": true/false, "reason": "简述原因"}}
"""
    try:
        resp = llm.invoke([{"role": "user", "content": prompt}])
        content = _clean_json_text(_response_content(resp))
        parsed = json.loads(content)
        if not isinstance(parsed, dict) or type(parsed.get("conflict")) is not bool:
            return True, "LLM 冲突响应 schema 非法，按 fail-closed 拒绝生成"
        reason = parsed.get("reason", "")
        if not isinstance(reason, str):
            return True, "LLM 冲突响应 reason 类型非法，按 fail-closed 拒绝生成"
        return parsed["conflict"], reason or "LLM 明确判定无冲突"
    except Exception as e:
        return True, f"LLM 冲突判定异常，按 fail-closed 拒绝生成: {type(e).__name__}: {e}"


def check_conflict(
    candidate_meta: SkillMeta,
    existing_metas: list[SkillMeta],
    method: str = "embedding",
    threshold: float = DEFAULT_SIMILARITY_THRESHOLD,
    llm: Any = None,
    model_dir: Optional[Path] = None,
) -> tuple[bool, str]:
    """统一冲突检测入口：关键词硬校验 + (Embedding 或 LLM 校验)"""
    if method not in _ALLOWED_CONFLICT_METHODS:
        return True, f"未知冲突判定方法 '{method}'，按 fail-closed 拒绝生成"

    # 1. 关键词硬碰撞校验
    try:
        kw_conflict, kw_reason = check_route_conflict_keywords(candidate_meta, existing_metas)
    except Exception as exc:
        return True, f"关键词冲突检测异常，按 fail-closed 拒绝生成: {type(exc).__name__}: {exc}"
    if kw_conflict:
        return True, kw_reason

    # 2. Embedding 判定
    if method == "embedding":
        return check_route_conflict_embedding(candidate_meta, existing_metas, threshold=threshold, model_dir=model_dir)

    # 3. LLM 判定
    if method == "llm":
        if llm is None:
            return True, "conflict_method='llm' 缺少 llm 实例，按 fail-closed 拒绝生成"
        return check_route_conflict_llm(candidate_meta, existing_metas, llm)

    # The method whitelist above makes this unreachable; retain a defensive
    # fail-closed return if the dispatch is changed in the future.
    return True, "冲突判定未覆盖，按 fail-closed 拒绝生成"


def _build_full_skill_md_text(
    name: str,
    version: str,
    description: str,
    use_when: str,
    not_for: list[str],
    keywords: list[str],
    examples: list[str],
    body: str,
) -> str:
    """拼接合规的 YAML frontmatter + Body"""
    front_dict = {
        "name": name,
        "version": version,
        "description": description,
        "use_when": use_when,
        "not_for": not_for,
        "dependencies": [],
        "trigger": {"keywords": keywords},
        "examples": examples,
        "evaluation": {"last_score": None, "last_release_id": None},
    }
    front_yaml = yaml.dump(front_dict, allow_unicode=True, sort_keys=False).strip()
    return f"---\n{front_yaml}\n---\n\n{body.strip()}\n"


def validate_generated_structure(
    full_skill_md: str,
    existing_names: set[str],
) -> tuple[bool, str, Optional[SkillMeta], Optional[str], Optional[str]]:
    """Fail-closed 结构与命名校验。

    Returns:
        (is_valid, error_message, skill_meta, frontmatter_text, body_text)
    """
    if not isinstance(full_skill_md, str):
        return False, "SKILL.md 必须是字符串", None, None, None

    m = _FRONTMATTER_RE.match(full_skill_md)
    if not m:
        return False, "缺少 YAML frontmatter (--- ... ---)", None, None, None

    frontmatter_text, body = m.group(1), m.group(2).strip()

    try:
        data = yaml.safe_load(frontmatter_text) or {}
    except Exception as exc:
        return False, f"YAML frontmatter 解析失败: {exc}", None, None, None

    if not isinstance(data, dict):
        return False, "YAML frontmatter 必须为键值映射", None, None, None

    unknown_fields = sorted(
        (str(key) for key in data if key not in _FRONTMATTER_FIELDS),
        key=str,
    )
    if unknown_fields:
        return False, f"frontmatter 存在未知字段: {', '.join(unknown_fields)}", None, None, None

    missing_fields = sorted(_FRONTMATTER_FIELDS - set(data))
    if missing_fields:
        return False, f"frontmatter 缺少必填字段 '{missing_fields[0]}'", None, None, None

    for field_name in _FRONTMATTER_FIELDS:
        if data[field_name] is None:
            return False, f"frontmatter 字段 '{field_name}' 不得为 null", None, None, None

    string_fields = ("name", "version", "description", "use_when")
    if any(not isinstance(data[name], str) for name in string_fields):
        return False, "frontmatter 的 name/version/description/use_when 必须是字符串", None, None, None

    name = data["name"].strip()
    version = data["version"].strip()
    description = data["description"].strip()
    use_when = data["use_when"].strip()
    not_for = data.get("not_for")

    if not NAME_PATTERN.fullmatch(name):
        return (
            False,
            f"name '{name}' 不合法：必须匹配 ^[a-z][a-z0-9_]{{2,31}}$",
            None,
            None,
            None,
        )

    if name in existing_names:
        return False, f"name '{name}' 与现有 Skill 重名冲突", None, None, None

    if version != "1.0.0":
        return False, f"新生成 Skill 的 version 必须为 '1.0.0'，当前为 '{version}'", None, None, None

    if len(description) < 5:
        return False, "description 过短 (需 >= 5 字符)", None, None, None

    if len(use_when) < 5:
        return False, "use_when 过短 (需 >= 5 字符)", None, None, None

    if not isinstance(not_for, list) or not (2 <= len(not_for) <= 4) or any(
        not isinstance(item, str) or not item.strip() for item in not_for
    ):
        return False, "not_for 必须包含 2-4 个非空字符串", None, None, None

    dependencies = data.get("dependencies")
    if not isinstance(dependencies, list) or dependencies != []:
        return False, "dependencies 必须严格为 []（文档型 Skill 不得声明运行时依赖）", None, None, None

    trigger = data.get("trigger")
    keywords = trigger.get("keywords") if isinstance(trigger, dict) else None
    if not isinstance(trigger, dict) or not isinstance(keywords, list):
        return False, "trigger.keywords 必须是字符串列表", None, None, None
    if not (3 <= len(keywords) <= 6) or any(
        not isinstance(keyword, str) or not keyword.strip() for keyword in keywords
    ):
        return False, "trigger.keywords 必须包含 3-6 个非空字符串", None, None, None

    examples = data.get("examples")
    if not isinstance(examples, list) or not examples or any(
        not isinstance(example, str) or not example.strip() for example in examples
    ):
        return False, "examples 必须是非空字符串列表", None, None, None

    evaluation = data.get("evaluation")
    if not isinstance(evaluation, dict):
        return False, "evaluation 必须是对象", None, None, None
    missing_evaluation = {"last_score", "last_release_id"} - set(evaluation)
    if missing_evaluation:
        return False, "evaluation 缺少必填字段 'last_score' 或 'last_release_id'", None, None, None

    # Body 校验必须在剥离 fenced block 后进行；只认显式的段名别名，
    # 任何额外 ##/# 顶层节都拒绝，避免 LLM 用标题文本伪造结构。
    sections_ok, sections_msg = _validate_body_sections(body)
    if not sections_ok:
        return False, sections_msg, None, None, None

    try:
        meta = SkillMeta(**data)
    except Exception as exc:
        return False, f"SkillMeta 模型校验失败: {exc}", None, None, None

    return True, "OK", meta, frontmatter_text, body


def _response_content(resp: Any) -> str:
    """Extract a textual LLM response without allowing representation leakage."""
    if isinstance(resp, dict):
        content = resp.get("content")
    else:
        content = getattr(resp, "content", None)
    if not isinstance(content, str):
        raise TypeError("LLM 响应必须包含字符串 content")
    return content


def _parse_generation_payload(resp: Any) -> tuple[dict[str, Any], str]:
    raw_text = _response_content(resp)
    payload = json.loads(_clean_json_text(raw_text))
    if not isinstance(payload, dict):
        raise ValueError("LLM JSON 顶层必须是对象")
    if "skill_md" in payload and payload["skill_md"] is not None and not isinstance(payload["skill_md"], str):
        raise ValueError("skill_md 必须是字符串")
    return payload, raw_text


def _default_generator_budget() -> EvolveBudget:
    """Generator-specific hard caps; evolve's unlimited defaults are not reused here."""
    return EvolveBudget(
        max_calls=DEFAULT_GENERATOR_MAX_CALLS,
        max_tokens=DEFAULT_GENERATOR_MAX_TOKENS,
        deadline_seconds=DEFAULT_GENERATOR_DEADLINE_SECONDS,
    )


def _ledger_details(ledger: LLMLedger) -> dict[str, Any]:
    return {"ledger": ledger.as_dict(), "budget": ledger.budget.__dict__.copy()}


def _build_independent_hard_case(
    meta: SkillMeta,
    existing_queries: set[str],
    index: int,
) -> dict[str, Any]:
    """Add a deterministic, policy-authored not_for boundary case.

    This case is intentionally not copied from the LLM response, so a generated
    skill always has one difficulty-hard negative/reference pair independent of
    its self-authored main-intent examples.
    """
    boundary = next((item.strip() for item in meta.not_for if item.strip()), "超出本技能服务范围的请求")
    query = f"请直接帮我处理：{boundary}。"
    if query.casefold() in existing_queries:
        query = f"请直接帮我处理：{boundary}（越过本技能边界）。"
    return {
        "id": f"{derive_skill_abbrev(meta.name)}_auto_{index:02d}",
        "skill": meta.name,
        "query": query,
        "reference": (
            f"拒绝并划界：该请求属于“{boundary}”，不在 {meta.name} 的主意图范围内；"
            "只能说明边界，不能把它当作主意图作答或编造结果。"
        ),
        "trace_id": f"generator:{meta.name}:{derive_skill_abbrev(meta.name)}_auto_{index:02d}",
        "case_kind": "independent_hard_boundary",
        "difficulty": "hard",
        "source": "generator_policy_not_for",
    }


def generate_skill(
    request: str,
    llm: Any = None,
    repo_root: Optional[Path] = None,
    register: bool = False,
    conflict_method: str = "embedding",
    similarity_threshold: float = DEFAULT_SIMILARITY_THRESHOLD,
    model_dir: Optional[Path] = None,
    ledger: Optional[LLMLedger] = None,
    budget: Optional[EvolveBudget] = None,
    max_retries: int = DEFAULT_GENERATOR_MAX_RETRIES,
) -> GeneratedSkill | GenerationFailure:
    """P2-A Skill 生成器主入口 (文档型/无工具域)。

    Args:
        request: 自然语言需求描述
        llm: HelloAgentsLLM 实例（若为 None 则尝试从 llm_factory 构建）
        repo_root: 项目仓库根目录
        register: 是否在通过全部校验后直接落盘注册
        conflict_method: 冲突检测方式 ('embedding' | 'llm')
        similarity_threshold: embedding 冲突判定阈值 (默认 0.70)
        model_dir: bge 模型目录 (默认使用系统内置)
        ledger: 可选的统一 LLM 账本；未提供时自动创建带硬帽的账本
        budget: 生成阶段预算；未提供时使用 4 calls/20k tokens/300s
        max_retries: 对 LLM 异常或 JSON/schema 错误的有限重试次数

    Returns:
        GeneratedSkill 或 GenerationFailure
    """
    root = repo_root or Path(__file__).resolve().parents[2]
    skills_dir = root / "skills"

    if max_retries < 0:
        return GenerationFailure(reason="INVALID_ARGUMENT", message="max_retries 不能为负数")

    active_ledger = ledger or LLMLedger(budget=budget or _default_generator_budget())
    if ledger is not None and budget is not None and ledger.total_calls == 0:
        ledger.budget = budget
    # A caller may pass the general evolve budget whose cost fields default to
    # unlimited.  Generation itself always retains a hard ceiling unless a
    # tighter explicit value was supplied.
    for field_name, default_value in (
        ("max_calls", DEFAULT_GENERATOR_MAX_CALLS),
        ("max_tokens", DEFAULT_GENERATOR_MAX_TOKENS),
        ("deadline_seconds", DEFAULT_GENERATOR_DEADLINE_SECONDS),
    ):
        if getattr(active_ledger.budget, field_name, None) is None:
            setattr(active_ledger.budget, field_name, default_value)

    if llm is None:
        try:
            from .evaluator.llm_factory import build_execution_llm
            llm = build_execution_llm(ledger=active_ledger)
        except Exception as exc:
            return GenerationFailure(
                reason="LLM_ERROR",
                message=f"无法初始化执行端 LLM: {exc}",
                details={"stage": "llm_init", "error_type": type(exc).__name__, **_ledger_details(active_ledger)},
            )
    elif isinstance(llm, TrackedLLM):
        if llm.ledger is not active_ledger:
            llm = TrackedLLM(llm.underlying_llm, active_ledger, role="generator")
    else:
        # Always track supplied clients too, including lightweight test clients;
        # generation retries and conflict checks therefore share one hard cap.
        llm = TrackedLLM(llm, active_ledger, role="generator")

    # 1. 获取现有 Skills
    existing_registry = SkillRegistry(
        db_path=root / "runs" / "skillforge.db",
        skills_dir=skills_dir,
        repo_root=root,
    )
    try:
        existing_registry.load_skills_from_dir()
    except Exception as exc:
        return GenerationFailure(
            reason="REGISTRY_ERROR",
            message=f"现有 Skill Registry 读取失败: {exc}",
            details={"stage": "registry", "error_type": type(exc).__name__, **_ledger_details(active_ledger)},
        )
    existing_names = set(existing_registry.list_names())
    existing_metas = [existing_registry.get_meta(n) for n in existing_names]

    # 2. 提示词构造
    prompt = f"""你是一个高质量 AI Skill 工程师与架构师。
当前任务：针对用户自然语言需求，生成一个纯文档型、无工具依赖的合法 SKILL.md，并配发 3-5 条初始测试用例。

【用户需求】
{request}

【Seed Skill 结构范本 (skills/explain_regex/SKILL.md)】
```markdown
---
name: explain_regex
version: 1.0.0
description: 讲解正则表达式的原理与匹配过程（不是帮用户写正则）
use_when: 用户想理解正则表达式的语法、匹配机制、回溯原理，或看懂别人写的正则
not_for:
  - 用户要求"写一个正则匹配 XX"（那是代码生成任务）
  - 正则性能调优（超出教学范围）
  - 特定正则库（re / regex / PCRE）的 API 差异
dependencies: []
trigger:
  keywords:
    - 正则
    - regex
    - 讲解
    - 匹配过程
    - 回溯
examples:
  - 讲一下 (a|b)*c 是怎么匹配的
  - 为什么 .* 会回溯这么慢
  - 这个正则 \\d{{4}}-\\d{{2}}-\\d{{2}} 什么意思
evaluation:
  last_score: null
  last_release_id: null
---

## Overview

从原理层面讲解正则表达式：**字符类 / 量词 / 分组 / 锚点 / 回溯**。目标是让用户读得懂、说得清，而不是写出可用的正则。

## Instructions

1. 识别用户问的是...
2. 按识别到的类型走对应讲解模板...
3. 优先用小例子演示...

## Examples

**Q**：讲一下 `.*?` 是什么意思？
**A**：...

## Constraints

- 用户要求超范围任务时明确告知边界并引导...
- 不给出可以直接跑的正则字符串作为主结果...
```

【硬性规范】
1. name: 必须符合正则 ^[a-z][a-z0-9_]{{2,31}}$，不得与现有技能重名。
2. version: 恒为 1.0.0。
3. description & use_when: 明确表达适用范围与核心能力（>= 5 字符）。
4. not_for: 必须提供 2-4 条明确的越界或不服务场景。
5. dependencies: 必须为 []。
6. trigger.keywords: 3-6 个精准触发词。
7. Body 必须包含完整的四级二级标题：## Overview, ## Instructions, ## Examples, ## Constraints。
8. 初始测试集 3-5 条：覆盖 use_when 主意图 + 1 条边界用例 + 1 条 not_for 越界反例（reference 需指明拒绝/划界理由）。

【输出格式】
严格仅输出一个合法的 JSON 对象，格式如下：
{{
  "name": "snake_case_name",
  "version": "1.0.0",
  "description": "...",
  "use_when": "...",
  "not_for": ["...", "..."],
  "keywords": ["...", "..."],
  "examples": ["...", "..."],
  "body": "## Overview\\n...\\n\\n## Instructions\\n...\\n\\n## Examples\\n...\\n\\n## Constraints\\n...",
  "test_cases": [
    {{"query": "...", "reference": "..."}},
    {{"query": "...", "reference": "..."}},
    {{"query": "...", "reference": "..."}}
  ]
}}
"""

    payload: Optional[dict[str, Any]] = None
    raw_text: Optional[str] = None
    last_error: Optional[Exception] = None
    attempts = max_retries + 1
    messages = [{"role": "user", "content": prompt}]
    for attempt in range(1, attempts + 1):
        try:
            resp = llm.invoke(messages)
            payload, raw_text = _parse_generation_payload(resp)
            break
        except BudgetExceededError as exc:
            return GenerationFailure(
                reason="BUDGET_EXCEEDED",
                message=f"生成阶段预算硬帽触发: {exc}",
                details={"stage": "generation", "attempt": attempt, "attempts": attempts, **_ledger_details(active_ledger)},
            )
        except Exception as exc:
            last_error = exc
            if attempt < attempts:
                messages = [
                    {
                        "role": "user",
                        "content": (
                            f"{prompt}\n\n上一次响应未通过 JSON/schema 校验（{type(exc).__name__}）。"
                            "请重试并只输出顶层为对象的合法 JSON，不要输出解释文字。"
                        ),
                    }
                ]

    if payload is None:
        return GenerationFailure(
            reason="LLM_ERROR",
            message=f"LLM 响应在 {attempts} 次尝试后仍无效: {last_error}",
            details={
                "stage": "generation_response",
                "error_type": type(last_error).__name__ if last_error else "UnknownError",
                "attempts": attempts,
                "raw_excerpt": raw_text[:2000] if isinstance(raw_text, str) else None,
                **_ledger_details(active_ledger),
            },
        )

    # 3. 组装 candidate skill_md
    try:
        skill_md = payload.get("skill_md")
        if isinstance(skill_md, str) and skill_md.strip().startswith("---"):
            full_skill_md = skill_md.strip()
        else:
            if skill_md is not None:
                raise ValueError("skill_md 若提供必须是带 frontmatter 的字符串")
            name = payload.get("name", "")
            version = payload.get("version", "1.0.0")
            description = payload.get("description", "")
            use_when = payload.get("use_when", "")
            not_for = payload.get("not_for", [])
            keywords = payload.get("keywords", [])
            examples = payload.get("examples", [])
            body = payload.get("body", "")
            if not all(isinstance(value, str) for value in (name, version, description, use_when, body)):
                raise ValueError("name/version/description/use_when/body 必须是字符串")
            if not isinstance(not_for, list) or not isinstance(keywords, list) or not isinstance(examples, list):
                raise ValueError("not_for/keywords/examples 必须是数组")
            full_skill_md = _build_full_skill_md_text(
                name=name.strip(),
                version=version.strip(),
                description=description.strip(),
                use_when=use_when.strip(),
                not_for=not_for,
                keywords=keywords,
                examples=examples,
                body=body.strip(),
            )
    except Exception as exc:
        return GenerationFailure(
            reason="INVALID_STRUCTURE",
            message=f"候选 Skill 结构组装失败: {exc}",
            details={"stage": "candidate_assembly", "error_type": type(exc).__name__, **_ledger_details(active_ledger)},
        )

    # 4. 结构校验 (fail-closed)
    ok_struct, struct_msg, meta, fm_raw, body_raw = validate_generated_structure(
        full_skill_md=full_skill_md,
        existing_names=existing_names,
    )
    if not ok_struct or meta is None:
        return GenerationFailure(
            reason="INVALID_STRUCTURE",
            message=struct_msg,
            details={"stage": "structure", "full_skill_md": full_skill_md[:8000], **_ledger_details(active_ledger)},
        )

    # 5. 测试用例校验
    raw_cases = payload.get("test_cases", [])
    if not isinstance(raw_cases, list) or not (3 <= len(raw_cases) <= 5):
        return GenerationFailure(
            reason="INVALID_STRUCTURE",
            message=f"测试用例数量必须在 3-5 条之间，实际为 {len(raw_cases) if isinstance(raw_cases, list) else '非列表'}",
            details={"stage": "test_cases", **_ledger_details(active_ledger)},
        )

    abbrev = derive_skill_abbrev(meta.name)
    formatted_cases: list[dict[str, Any]] = []
    for idx, c in enumerate(raw_cases, start=1):
        if not isinstance(c, dict):
            return GenerationFailure(
                reason="INVALID_STRUCTURE",
                message=f"测试用例第 {idx} 项非对象结构",
            )
        q_raw = c.get("query")
        ref_raw = c.get("reference")
        if not isinstance(q_raw, str) or not isinstance(ref_raw, str):
            return GenerationFailure(
                reason="INVALID_STRUCTURE",
                message=f"测试用例第 {idx} 项 query/reference 必须是字符串",
                details={"stage": "test_cases", **_ledger_details(active_ledger)},
            )
        q = q_raw.strip()
        ref = ref_raw.strip()
        if not q or not ref:
            return GenerationFailure(
                reason="INVALID_STRUCTURE",
                message=f"测试用例第 {idx} 项 query 或 reference 为空",
                details={"stage": "test_cases", **_ledger_details(active_ledger)},
            )
        case_id = f"{abbrev}_auto_{idx:02d}"
        formatted_cases.append({
            "id": case_id,
            "skill": meta.name,
            "query": q,
            "reference": ref,
            "trace_id": f"generator:{meta.name}:{case_id}",
            "case_kind": "llm_generated",
            "difficulty": "standard",
            "source": "llm_response",
        })

    existing_queries = {str(case["query"]).casefold() for case in formatted_cases}
    formatted_cases.append(_build_independent_hard_case(meta, existing_queries, len(formatted_cases) + 1))

    # 6. 路由冲突校验 (fail-closed)
    has_conflict, conflict_msg = check_conflict(
        candidate_meta=meta,
        existing_metas=existing_metas,
        method=conflict_method,
        threshold=similarity_threshold,
        llm=llm,
        model_dir=model_dir,
    )
    if has_conflict:
        return GenerationFailure(
            reason="CONFLICT",
            message=f"路由冲突校验未通过: {conflict_msg}",
            details={"stage": "conflict", "conflict_reason": conflict_msg, "candidate": meta.name, **_ledger_details(active_ledger)},
        )

    generated = GeneratedSkill(
        name=meta.name,
        version=meta.version,
        description=meta.description,
        use_when=meta.use_when,
        not_for=meta.not_for,
        frontmatter_raw=fm_raw or "",
        body_raw=body_raw or "",
        full_skill_md=full_skill_md,
        test_cases=formatted_cases,
        meta=meta,
    )

    if register:
        try:
            register_skill(generated, repo_root=root)
        except Exception as exc:
            return GenerationFailure(
                reason="REGISTER_ERROR",
                message=f"Skill 注册未提交: {exc}",
                details={"stage": "register", "error_type": type(exc).__name__, **_ledger_details(active_ledger)},
            )

    return generated


def _load_registration_json(path: Path, label: str) -> dict[str, Any]:
    if not path.exists():
        raise RegistrationError(f"{label} 文件不存在: {path}")
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        raise RegistrationError(f"{label} JSON 解析失败: {exc}") from exc
    if not isinstance(data, dict):
        raise RegistrationError(f"{label} 顶层必须是对象")
    return data


def _validate_global_auto_case_prefixes(
    manifest_paths: set[Path],
    incoming_cases: list[dict[str, Any]],
) -> None:
    """Check auto-case prefixes across every on-disk evaluation manifest.

    Registration receives a repair/router path for testability, but a prefix
    collision in holdout, audit, or a source manifest is just as dangerous.
    Scan the complete sibling manifest directory before any write so a new
    registration cannot create a cross-layer collision.
    """
    from .data_partition import validate_auto_case_prefixes

    all_cases: list[dict[str, Any]] = []
    loaded: dict[Path, dict[str, Any]] = {}
    for path in sorted({item.resolve() for item in manifest_paths}):
        if not path.exists() or not path.is_file():
            continue
        loaded[path] = _load_registration_json(path, path.name)
    for data in loaded.values():
        if "cases" not in data:
            continue
        cases = data["cases"]
        if not isinstance(cases, list) or any(not isinstance(case, dict) for case in cases):
            raise RegistrationError("评估 manifest cases 必须是对象数组")
        all_cases.extend(cases)
    existing_ids = {
        str(case["id"])
        for case in all_cases
        if isinstance(case, dict) and isinstance(case.get("id"), str)
    }
    incoming_ids = [case.get("id") for case in incoming_cases if isinstance(case, dict)]
    incoming_id_set = {case_id for case_id in incoming_ids if isinstance(case_id, str)}
    if len(incoming_id_set) != len(incoming_ids):
        raise RegistrationError("新 Skill 测试用例存在重复或非法 case-ID")
    collisions = sorted(incoming_id_set & existing_ids)
    if collisions:
        raise RegistrationError(f"case-ID 与其他 manifest 冲突，拒绝静默跳过: {collisions}")
    all_cases.extend(incoming_cases)
    errors = validate_auto_case_prefixes(all_cases)
    if errors:
        raise RegistrationError("全局 auto case 前缀校验失败: " + "; ".join(errors))


def _validate_repair_registration(
    repair_data: dict[str, Any],
    generated: GeneratedSkill,
) -> tuple[dict[str, Any], set[str]]:
    cases = repair_data.get("cases")
    meta = repair_data.get("meta")
    if not isinstance(cases, list) or any(not isinstance(case, dict) for case in cases):
        raise RegistrationError("repair_set cases 必须是对象数组")
    if not isinstance(meta, dict):
        raise RegistrationError("repair_set meta 必须是对象")

    existing_ids: set[str] = set()
    prefixes: dict[str, str] = {}
    actual_auto_ids: set[str] = set()
    for case in cases:
        case_id = case.get("id")
        if not isinstance(case_id, str) or not case_id.strip():
            raise RegistrationError("repair_set 存在缺失或非法 case-ID")
        if case_id in existing_ids:
            raise RegistrationError(f"repair_set 已存在重复 case-ID: {case_id}")
        existing_ids.add(case_id)
        prefix = _case_prefix(case_id)
        if "_auto_" in case_id:
            if prefix is None:
                raise RegistrationError(f"repair_set auto case-ID 格式非法: {case_id}")
            actual_auto_ids.add(case_id)
            for field_name in ("skill", "query", "reference", "trace_id"):
                if not isinstance(case.get(field_name), str) or not case[field_name].strip():
                    raise RegistrationError(f"repair_set auto case {case_id} 缺失合法 {field_name}")
            previous_skill = prefixes.setdefault(prefix, str(case.get("skill", "")))
            if previous_skill != str(case.get("skill", "")):
                raise RegistrationError(
                    f"case 前缀 '{prefix}' 已被多个 skill 使用: {previous_skill!r} 与 {case.get('skill')!r}"
                )

    declared = meta.get("auto_case_ids")
    if not isinstance(declared, list) or any(not isinstance(item, str) for item in declared):
        raise RegistrationError("repair_set meta.auto_case_ids 必须是字符串列表")
    declared_ids = set(declared)
    if len(declared_ids) != len(declared):
        raise RegistrationError("repair_set meta.auto_case_ids 存在重复 ID")
    if actual_auto_ids != declared_ids:
        raise RegistrationError(
            "repair_set _auto_ 与 meta.auto_case_ids 不一致: "
            f"actual={sorted(actual_auto_ids)}, declared={sorted(declared_ids)}"
        )
    if type(meta.get("auto_case_count")) is not int or meta["auto_case_count"] != len(declared_ids):
        raise RegistrationError("repair_set meta.auto_case_count 与 manifest 不一致")
    if type(meta.get("total")) is not int or meta["total"] != len(cases):
        raise RegistrationError("repair_set meta.total 与 cases 数量不一致")

    incoming_ids: set[str] = set()
    expected_prefix = f"{derive_skill_abbrev(generated.name)}_auto_"
    if not generated.test_cases:
        raise RegistrationError("新 Skill 至少需要一条测试用例")
    for case in generated.test_cases:
        if not isinstance(case, dict):
            raise RegistrationError("新 Skill 测试用例必须是对象")
        case_id = case.get("id")
        if not isinstance(case_id, str) or not re.fullmatch(re.escape(expected_prefix) + r"\d+", case_id):
            raise RegistrationError(
                f"新 Skill case-ID 必须使用全局前缀 '{expected_prefix}': {case_id!r}"
            )
        if case_id in incoming_ids:
            raise RegistrationError(f"新 Skill 内部存在重复 case-ID: {case_id}")
        if case_id in existing_ids:
            raise RegistrationError(f"case-ID 已存在，拒绝静默跳过: {case_id}")
        if case.get("skill") != generated.name:
            raise RegistrationError(f"case {case_id} 的 skill 与候选 Skill 不一致")
        for field_name in ("query", "reference", "trace_id"):
            if not isinstance(case.get(field_name), str) or not case[field_name].strip():
                raise RegistrationError(f"case {case_id} 缺失合法 {field_name}")
        incoming_ids.add(case_id)

    existing_prefix = prefixes.get(derive_skill_abbrev(generated.name))
    if existing_prefix is not None:
        raise RegistrationError(
            f"case 前缀 '{derive_skill_abbrev(generated.name)}' 已被 skill {existing_prefix!r} 使用，拒绝注册"
        )

    projected_total = len(cases) + len(incoming_ids)
    projected_auto = len(actual_auto_ids) + len(incoming_ids)
    projected_ratio = projected_auto / projected_total if projected_total else 1.0
    if projected_ratio > MAX_REPAIR_AUTO_RATIO:
        raise RegistrationError(
            f"repair auto case 占比 {projected_ratio:.1%} 超过上限 {MAX_REPAIR_AUTO_RATIO:.1%}，拒绝新注册"
        )

    quota_reservations = meta.get("quota_reservations", {})
    if not isinstance(quota_reservations, dict):
        raise RegistrationError("repair_set meta.quota_reservations 必须是对象")
    for skill, reservation in quota_reservations.items():
        if not isinstance(skill, str) or not skill.strip() or not isinstance(reservation, dict):
            raise RegistrationError("repair_set 存在非法 quota reservation")
        if type(reservation.get("experiment_holdout")) is not int or reservation["experiment_holdout"] <= 0:
            raise RegistrationError(f"Skill {skill} 的 experiment_holdout 配额非法")
        if type(reservation.get("final_audit")) is not int or reservation["final_audit"] <= 0:
            raise RegistrationError(f"Skill {skill} 的 final_audit 配额非法")
        if reservation.get("status") not in {"reserved", "fulfilled"}:
            raise RegistrationError(f"Skill {skill} 的 quota reservation status 非法")
    # Import locally to keep the generator usable without loading the partition
    # CLI at module import time.
    from .data_partition import reserve_skill_quota

    new_data = json.loads(json.dumps(repair_data, ensure_ascii=False))
    new_cases = new_data["cases"]
    new_meta = new_data["meta"]
    new_cases.extend(generated.test_cases)
    auto_ids = sorted(case["id"] for case in new_cases if "_auto_" in str(case.get("id", "")))
    new_meta["auto_case_ids"] = auto_ids
    new_meta["auto_case_count"] = len(auto_ids)
    new_meta["total"] = len(new_cases)
    new_meta.setdefault("quota_reservations", {})[generated.name] = reserve_skill_quota(generated.name)
    return new_data, incoming_ids


def _build_router_cases(meta: SkillMeta) -> list[dict[str, Any]]:
    """Build positive and explicit not_for negative cases for the router manifest."""
    examples = [example.strip() for example in meta.examples if isinstance(example, str) and example.strip()]
    positive_queries = examples[:3] or [meta.use_when.strip()]
    prefix = derive_skill_abbrev(meta.name)
    rows: list[dict[str, Any]] = []
    for index, query in enumerate(positive_queries, start=1):
        rows.append(
            {
                "id": f"{prefix}_router_p_{index:02d}",
                "query": query,
                "expected": meta.name,
                "type": "positive",
                "why": "由已注册 Skill manifest 的 examples 自动生成正例",
            }
        )
    boundary = next((item.strip() for item in meta.not_for if isinstance(item, str) and item.strip()), None)
    if boundary:
        rows.append(
            {
                "id": f"{prefix}_router_n_01",
                "query": f"请直接帮我处理：{boundary}。",
                "expected": None,
                "type": "hard_negative",
                "why": f"由 {meta.name}.not_for 自动生成边界反例",
            }
        )
    return rows


def _validate_router_registration(
    router_data: dict[str, Any],
    meta: SkillMeta,
) -> dict[str, Any]:
    cases = router_data.get("cases")
    if not isinstance(cases, list) or any(not isinstance(case, dict) for case in cases):
        raise RegistrationError("router_negatives cases 必须是对象数组")
    existing_ids: set[str] = set()
    for case in cases:
        case_id = case.get("id")
        if not isinstance(case_id, str) or not case_id:
            raise RegistrationError("router_negatives 存在缺失或非法 case-ID")
        if case_id in existing_ids:
            raise RegistrationError(f"router_negatives 已存在重复 case-ID: {case_id}")
        existing_ids.add(case_id)

    additions = _build_router_cases(meta)
    for case in additions:
        if case["id"] in existing_ids:
            raise RegistrationError(f"router case-ID 已存在，拒绝静默跳过: {case['id']}")
        existing_ids.add(case["id"])

    new_data = json.loads(json.dumps(router_data, ensure_ascii=False))
    new_cases = new_data["cases"]
    new_cases.extend(additions)
    meta_block = new_data.setdefault("meta", {})
    if not isinstance(meta_block, dict):
        raise RegistrationError("router_negatives meta 必须是对象")
    breakdown = Counter(str(case.get("type", "")) for case in new_cases)
    meta_block["total"] = len(new_cases)
    meta_block["breakdown"] = {
        "positive": breakdown.get("positive", 0),
        "hard_negative": breakdown.get("hard_negative", 0),
        "unrelated_negative": breakdown.get("unrelated_negative", 0),
    }
    return new_data


def _commit_atomic_registration(
    targets: list[tuple[Path, str]],
    skill_dir: Path,
) -> None:
    """Stage all outputs, replace them as one guarded batch, and roll back on error."""
    staged: list[tuple[Path, Path]] = []
    backups: dict[Path, Optional[bytes]] = {}
    created_dir = False
    try:
        if not skill_dir.exists():
            skill_dir.mkdir(parents=True, exist_ok=False)
            created_dir = True
        for target, content in targets:
            target.parent.mkdir(parents=True, exist_ok=True)
            fd, temp_name = tempfile.mkstemp(prefix=f".{target.name}.", suffix=".tmp", dir=str(target.parent))
            stage = Path(temp_name)
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                handle.write(content)
                handle.flush()
                os.fsync(handle.fileno())
            staged.append((target, stage))
            backups[target] = target.read_bytes() if target.exists() else None

        for target, stage in staged:
            os.replace(stage, target)
        for parent in {target.parent for target, _ in staged}:
            try:
                dir_fd = os.open(parent, os.O_RDONLY)
                try:
                    os.fsync(dir_fd)
                finally:
                    os.close(dir_fd)
            except OSError:
                # Directory fsync is best effort on filesystems that do not
                # expose an fd; file content was already fsynced before replace.
                pass
    except Exception as exc:
        for target, previous in backups.items():
            try:
                if previous is None:
                    target.unlink(missing_ok=True)
                else:
                    target.write_bytes(previous)
            except OSError:
                pass
        if created_dir:
            try:
                skill_dir.rmdir()
            except OSError:
                pass
        raise RegistrationError(f"注册批量提交失败，已回滚: {exc}") from exc
    finally:
        for _, stage in staged:
            stage.unlink(missing_ok=True)


def register_skill(
    generated: GeneratedSkill,
    repo_root: Optional[Path] = None,
    repair_set_path: Optional[Path] = None,
    router_negatives_path: Optional[Path] = None,
) -> Path:
    """Atomically register SKILL.md, repair manifest, and router cases.

    All schema, duplicate, prefix, quota, and path checks happen before any
    write.  A new skill never overwrites an existing skill and a colliding case
    ID is an explicit error rather than a silently skipped sample.
    """
    if not isinstance(generated, GeneratedSkill):
        raise RegistrationError("generated 必须是 GeneratedSkill")
    root = repo_root or Path(__file__).resolve().parents[2]
    if not isinstance(generated.name, str) or not NAME_PATTERN.fullmatch(generated.name):
        raise RegistrationError(f"Skill name 非法: {generated.name!r}")

    skill_dir = root / "skills" / generated.name
    skill_file = skill_dir / "SKILL.md"
    if skill_dir.exists() or skill_file.exists():
        raise RegistrationError(f"Skill 已存在，拒绝覆盖: {skill_dir}")
    if not isinstance(generated.full_skill_md, str):
        raise RegistrationError("full_skill_md 必须是字符串")

    skills_dir = root / "skills"
    existing_names = {path.name for path in skills_dir.iterdir() if path.is_dir()} if skills_dir.exists() else set()
    ok, message, parsed_meta, _, _ = validate_generated_structure(generated.full_skill_md, existing_names)
    if not ok or parsed_meta is None:
        raise RegistrationError(f"注册前 SKILL.md 结构校验失败: {message}")
    if not isinstance(generated.meta, SkillMeta):
        raise RegistrationError("生成产物 meta 必须是 SkillMeta")
    if parsed_meta.name != generated.name or generated.meta.name != generated.name:
        raise RegistrationError("生成产物的 name 与 frontmatter/meta 不一致")
    if generated.meta.model_dump() != parsed_meta.model_dump():
        raise RegistrationError("生成产物 meta 与 SKILL.md frontmatter 全字段不一致")
    generated_fields = {
        "version": generated.version,
        "description": generated.description,
        "use_when": generated.use_when,
        "not_for": generated.not_for,
    }
    parsed_fields = {
        field_name: getattr(parsed_meta, field_name)
        for field_name in generated_fields
    }
    if generated_fields != parsed_fields:
        raise RegistrationError("生成产物摘要字段与 SKILL.md frontmatter 不一致")

    repair_file = repair_set_path or (root / "evaluation_sets" / "repair_set.json")
    router_file = router_negatives_path or (root / "evaluation_sets" / "router_negatives.json")
    repair_data = _load_registration_json(repair_file, "repair_set")
    router_data = _load_registration_json(router_file, "router_negatives")
    new_repair_data, _ = _validate_repair_registration(repair_data, generated)
    new_router_data = _validate_router_registration(router_data, parsed_meta)

    evaluation_dir = repair_file.parent
    manifest_paths = set(evaluation_dir.glob("*.json"))
    manifest_paths.update({repair_file, router_file})
    _validate_global_auto_case_prefixes(manifest_paths, generated.test_cases)

    targets = [
        (skill_file, generated.full_skill_md.rstrip() + "\n"),
        (repair_file, json.dumps(new_repair_data, ensure_ascii=False, indent=2) + "\n"),
        (router_file, json.dumps(new_router_data, ensure_ascii=False, indent=2) + "\n"),
    ]
    if len({target for target, _ in targets}) != len(targets):
        raise RegistrationError("SKILL.md、repair_set 与 router manifest 目标路径必须互不相同")
    _commit_atomic_registration(targets, skill_dir)

    generated.skill_dir = skill_dir
    generated.skill_file = skill_file
    generated.registered = True
    return skill_file


def load_repair_manifest(path: Path) -> dict[str, Any]:
    """Load and minimally validate repair_set manifest for report generation."""
    data = _load_registration_json(path, "repair_set")
    cases = data.get("cases")
    meta = data.get("meta")
    if not isinstance(cases, list) or any(not isinstance(case, dict) for case in cases) or not isinstance(meta, dict):
        raise RegistrationError("repair_set manifest 必须包含对象 meta 与 cases 数组")
    declared = meta.get("auto_case_ids")
    if not isinstance(declared, list) or any(not isinstance(item, str) for item in declared):
        raise RegistrationError("repair_set manifest auto_case_ids 非法")
    if len(set(declared)) != len(declared):
        raise RegistrationError("repair_set manifest auto_case_ids 存在重复 ID")
    actual = [case.get("id") for case in cases if isinstance(case, dict) and "_auto_" in str(case.get("id", ""))]
    if len(set(actual)) != len(actual):
        raise RegistrationError("repair_set manifest 实际 auto case 存在重复 ID")
    if sorted(actual) != sorted(declared):
        raise RegistrationError("repair_set manifest 与实际 auto case 不一致")
    if type(meta.get("auto_case_count")) is not int or meta["auto_case_count"] != len(declared):
        raise RegistrationError("repair_set manifest auto_case_count 不一致")
    if type(meta.get("total")) is not int or meta["total"] != len(cases):
        raise RegistrationError("repair_set manifest total 不一致")
    from .data_partition import validate_auto_case_prefixes

    prefix_errors = validate_auto_case_prefixes(cases)
    if prefix_errors:
        raise RegistrationError("repair_set auto case 前缀校验失败: " + "; ".join(prefix_errors))
    return data


def build_manifest_report(
    root: Path,
    skill_names: Optional[list[str]] = None,
) -> dict[str, Any]:
    """Build an auditable summary exclusively from the on-disk manifest."""
    data = load_repair_manifest(root / "evaluation_sets" / "repair_set.json")
    requested = set(skill_names or [])
    cases = data["cases"]
    manifest_auto_ids = set(data["meta"]["auto_case_ids"])
    selected = [
        case for case in cases
        if isinstance(case, dict)
        and case.get("id") in manifest_auto_ids
        and (not requested or case.get("skill") in requested)
    ]
    scoped_auto_ids = {str(case["id"]) for case in selected}
    by_skill: dict[str, list[str]] = {}
    for case in selected:
        by_skill.setdefault(str(case.get("skill", "")), []).append(str(case["id"]))
    if requested and not requested.issubset(by_skill):
        missing = sorted(requested - set(by_skill))
        raise RegistrationError(f"manifest 缺少请求 Skill 的 auto case: {missing}")
    return {
        "manifest": "evaluation_sets/repair_set.json",
        "manifest_total": data["meta"].get("total"),
        "auto_case_count": len(scoped_auto_ids),
        "auto_case_ids": sorted(scoped_auto_ids),
        "manifest_auto_case_count": len(manifest_auto_ids),
        "skills": {skill: sorted(ids) for skill, ids in sorted(by_skill.items())},
        "cases": [
            {
                key: case.get(key)
                for key in ("id", "skill", "query", "reference", "trace_id", "case_kind", "difficulty", "source")
                if key in case
            }
            for case in sorted(selected, key=lambda item: str(item.get("id", "")))
        ],
    }


def render_manifest_report(report: dict[str, Any]) -> str:
    """Render a compact report whose case list comes from manifest data."""
    lines = [
        "# Skill generation manifest report",
        "",
        f"- Manifest: `{report['manifest']}`",
        f"- Repair total: {report['manifest_total']}",
        f"- Auto cases: {report['auto_case_count']}",
        "",
        "## Auto cases by skill",
        "",
    ]
    for skill, ids in report["skills"].items():
        lines.append(f"- `{skill}` ({len(ids)}): {', '.join(f'`{case_id}`' for case_id in ids)}")
    if report.get("cases"):
        lines.extend(["", "## Manifest case details", ""])
        for case in report["cases"]:
            lines.append(f"- `{case['id']}` `{case['skill']}`: {case.get('query', '')}")
    return "\n".join(lines) + "\n"
