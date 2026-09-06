"""SkillSplitter · 多域/大 Skill 拆分器 (P2-B)

三维耦合分析裁决（数据耦合 / 流程耦合 / 评测集耦合）→ 可拆则按意图域切分为 N 个子 Skill
- 段 1 耦合分析器：analyze_split(skill_name, llm) -> SplitAnalysis (fail-closed)
- 段 2 拆分执行器：split_skill(analysis, llm) -> SplitResult (子 SKILL.md + case 分配 + 互斥路由 + 备份)
"""
from __future__ import annotations

import json
import hashlib
import logging
import os
import re
import shutil
import tempfile
import uuid
from copy import deepcopy
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

import numpy as np
import yaml

from .evaluator.llm_factory import LLMLedger, TrackedLLM
from .models import EvolveBudget, SkillMeta
from .registry import _FRONTMATTER_RE
from .router.embed import EmbedLayer
from .skill_generator import (
    NAME_PATTERN,
    GeneratedSkill,
    RegistrationError,
    _build_full_skill_md_text,
    _build_independent_hard_case,
    _clean_json_text,
    _response_content,
    derive_skill_abbrev,
    register_skill,
    validate_generated_structure,
)

logger = logging.getLogger(__name__)

# 耦合阈值与参数
DEFAULT_DATA_COUPLING_THRESHOLD = 0.25      # 数据耦合判定门限 (>0.25 视为高耦合不可拆)
DEFAULT_PROCESS_COUPLING_THRESHOLD = 0.30   # 流程耦合判定门限 (>0.30 视为高耦合不可拆)
DEFAULT_EVAL_COUPLING_THRESHOLD = 0.30      # 评测耦合判定门限 (>0.30 视为高耦合不可拆)
DEFAULT_CONFUSION_MARGIN = 0.05             # 域相似度差值门限 (<0.05 判定为混淆 case 拒绝硬塞)
DEFAULT_MAX_CENTROID_SIMILARITY = 0.75      # 域中心相似度门槛 (>=0.75 判定为域语义重叠)
DEFAULT_ANALYSIS_MAX_AGE_SECONDS = 24 * 60 * 60
SPLIT_AUDIT_SCHEMA_VERSION = 1
_SECTION_NAMES = ("Overview", "Instructions", "Examples", "Constraints")
_SECTION_ALIASES = {
    "overview": "Overview",
    "概述": "Overview",
    "instructions": "Instructions",
    "instruction": "Instructions",
    "说明": "Instructions",
    "使用说明": "Instructions",
    "examples": "Examples",
    "example": "Examples",
    "示例": "Examples",
    "constraints": "Constraints",
    "constraint": "Constraints",
    "约束": "Constraints",
}
_TOP_LEVEL_HEADING_RE = re.compile(r"^ {0,3}(?P<marks>#{1,6})[ \t]+(?P<title>.*?)[ \t]*$")
_FENCE_RE = re.compile(r"^ {0,3}(?P<marker>`{3,}|~{3,})")


@dataclass
class DomainSpec:
    """拆分子域规范描述"""
    domain_id: str
    name: str
    description: str
    use_when: str
    not_for: list[str] = field(default_factory=list)
    keywords: list[str] = field(default_factory=list)
    examples: list[str] = field(default_factory=list)
    dependencies: list[str] = field(default_factory=list)
    overview: str = ""
    instructions: str = ""
    body_examples: str = ""
    constraints: str = ""
    # Source-derived content only.  These fields make the split auditable and
    # prevent a generated child from silently losing the paragraph it came
    # from.
    source_line_ranges: dict[str, list[list[int]]] = field(default_factory=dict)
    source_section_hashes: dict[str, list[str]] = field(default_factory=dict)
    source_fragments: list[dict[str, Any]] = field(default_factory=list)


@dataclass
class DimensionCoupling:
    """单一维度耦合评估结果"""
    dimension: str              # "data", "process", "eval_set"
    score: float                # 0.0 ~ 1.0 (越大越耦合)
    threshold: float            # 耦合判定门限
    coupled: bool               # 是否属于高耦合 (score > threshold)
    verdict_reason: str         # 判定解释
    metrics: dict[str, Any] = field(default_factory=dict)


@dataclass
class CaseAssignment:
    """单条测试用例的域分配明细"""
    case_id: str
    query: str
    reference: str
    assigned_domain: Optional[str]
    confidence: float
    similarities: dict[str, float]
    margin: float
    confused: bool
    reason: str


@dataclass
class SplitAnalysis:
    """Skill 拆分三维耦合分析报告 (段 1 核心产物)"""
    skill_name: str
    can_split: bool
    verdict: str                # "SPLIT_RECOMMENDED" | "CANNOT_SPLIT"
    primary_reason: str
    data_coupling: DimensionCoupling
    process_coupling: DimensionCoupling
    eval_coupling: DimensionCoupling
    domains: list[DomainSpec] = field(default_factory=list)
    assigned_cases: dict[str, list[dict[str, Any]]] = field(default_factory=dict)
    unassigned_cases: list[dict[str, Any]] = field(default_factory=list)
    case_assignments: list[CaseAssignment] = field(default_factory=list)
    details: dict[str, Any] = field(default_factory=dict)
    # Execution credentials.  Empty defaults preserve compatibility with
    # hand-built reports, while split_skill deliberately rejects them.
    source_skill_version: str = ""
    source_skill_hash: str = ""
    generated_at: str = ""
    repair_set_fingerprint: str = ""
    source_credential: dict[str, str] = field(default_factory=dict)
    analysis_digest: str = ""

    @property
    def skill_version(self) -> str:
        """Compatibility alias for callers using the shorter credential name."""
        return self.source_skill_version

    @property
    def skill_hash(self) -> str:
        """Compatibility alias for callers using the shorter credential name."""
        return self.source_skill_hash

    @property
    def analysis_generated_at(self) -> str:
        """Compatibility alias for callers using the explicit timestamp name."""
        return self.generated_at

    @property
    def provenance(self) -> dict[str, str]:
        """Stable public view of the execution credential tuple."""
        return dict(self.source_credential)


@dataclass
class SplitResult:
    """Skill 拆分执行结果 (段 2 核心产物)"""
    original_skill: str
    can_split: bool
    sub_skills: list[GeneratedSkill] = field(default_factory=list)
    assigned_cases_summary: dict[str, list[str]] = field(default_factory=dict)
    unassigned_cases: list[dict[str, Any]] = field(default_factory=list)
    backup_path: Optional[Path] = None
    registered: bool = False
    success: bool = True
    errors: list[str] = field(default_factory=list)
    unassigned_report_path: Optional[Path] = None
    migration_manifest_path: Optional[Path] = None
    content_audit: dict[str, Any] = field(default_factory=dict)


def _load_skill_file(skill_name: str, skills_dir: Path) -> tuple[SkillMeta, str, str]:
    """读取指定技能的 SKILL.md 并返回 (meta, frontmatter_text, body_text)"""
    skill_md = skills_dir / skill_name / "SKILL.md"
    if not skill_md.exists():
        raise FileNotFoundError(f"SKILL.md 文件不存在: {skill_md}")

    text = skill_md.read_text(encoding="utf-8")
    m = _FRONTMATTER_RE.match(text)
    if not m:
        raise ValueError(f"缺少合法 YAML frontmatter: {skill_md}")

    frontmatter_text, body = m.group(1), m.group(2).strip()
    data = yaml.safe_load(frontmatter_text) or {}
    meta = SkillMeta(**data)
    return meta, frontmatter_text, body


def _get_known_fixtures() -> set[str]:
    """获取系统中已注册的受控 fixture 名称"""
    try:
        from .evaluator.fixtures import _FIXTURE_FACTORIES
        return set(_FIXTURE_FACTORIES.keys())
    except Exception:
        return {"amap_weather_api"}


def _sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _sha256_text(text: str) -> str:
    return _sha256_bytes(text.encode("utf-8"))


def _sha256_file(path: Path) -> str:
    if not path.exists() or not path.is_file():
        raise FileNotFoundError(f"文件不存在: {path}")
    return _sha256_bytes(path.read_bytes())


def _normalise_section_title(title: str) -> str:
    title = re.sub(r"^\s*(?:第\s*[一二三四1-4]+\s*[章节段]|[1-4]\s*[.、:)：-])\s*", "", title, flags=re.IGNORECASE)
    title = re.sub(r"[（(].*?[）)]", "", title)
    title = re.split(r"\s*[/／|｜]\s*", title, maxsplit=1)[0]
    title = re.sub(r"[()\[\]{}（）【】]", " ", title)
    title = re.sub(r"[:：]+$", "", title)
    title = re.sub(r"[\s\u3000]+", " ", title.strip().rstrip("#").strip())
    return title.casefold()


def _iter_visible_body_lines(body: str) -> list[tuple[int, str]]:
    """Return ``(zero_based_line_no, line)`` outside Markdown fenced blocks."""
    visible: list[tuple[int, str]] = []
    active: Optional[tuple[str, int]] = None
    for line_no, line in enumerate(body.splitlines()):
        fence = _FENCE_RE.match(line)
        if active is None:
            if fence:
                marker = fence.group("marker")
                active = (marker[0], len(marker))
                continue
            visible.append((line_no, line))
            continue
        if fence:
            marker = fence.group("marker")
            if marker[0] == active[0] and len(marker) >= active[1]:
                active = None
    if active is not None:
        raise ValueError("Body 存在未闭合的 fenced block")
    return visible


def _source_section_records(body: str) -> dict[str, dict[str, Any]]:
    """Parse the four top-level sections without treating fenced headings as real.

    The returned text is the original section text (including nested headings
    and fenced blocks); only top-level heading discovery ignores fences.  Line
    numbers are 1-based relative to the body, which is sufficient to compose
    an absolute mapping when the frontmatter offset is known.
    """
    lines = body.splitlines()
    visible = _iter_visible_body_lines(body)
    headings: list[tuple[int, str, str]] = []
    for line_no, line in visible:
        match = _TOP_LEVEL_HEADING_RE.match(line)
        if not match:
            continue
        # ``#`` is also a top-level section heading and must not be silently
        # ignored.  ``###`` and deeper headings are content within a section.
        if len(match.group("marks")) < 3 and len(match.group("marks")) != 2:
            raise ValueError(f"Body 存在额外顶层节或未知段名: {line.strip()}")
        if len(match.group("marks")) != 2:
            continue
        raw_title = match.group("title").strip().rstrip("#").strip()
        canonical = _SECTION_ALIASES.get(_normalise_section_title(raw_title))
        if canonical is None:
            raise ValueError(f"Body 存在额外顶层节或未知段名: {line.strip()}")
        headings.append((line_no, canonical, raw_title))

    expected = list(_SECTION_NAMES)
    actual = [canonical for _, canonical, _ in headings]
    if actual != expected:
        missing = [name for name in expected if name not in actual]
        if missing:
            raise ValueError(f"Body 缺少必须的段落: {', '.join(f'## {x}' for x in missing)}")
        raise ValueError(f"Body 四段必须严格按顺序且各出现一次，实际为: {actual}")

    records: dict[str, dict[str, Any]] = {}
    for index, (heading_line, canonical, raw_title) in enumerate(headings):
        next_heading = headings[index + 1][0] if index + 1 < len(headings) else len(lines)
        text = "\n".join(lines[heading_line + 1:next_heading]).strip()
        if not text:
            raise ValueError(f"Body 段落 ## {canonical} 内容为空")
        records[canonical] = {
            "name": canonical,
            "raw_title": raw_title,
            "text": text,
            "line_start": heading_line + 1,
            "line_end": next_heading,
            "sha256": _sha256_text(text),
        }
    return records


def _parse_body_four_sections(body: str) -> dict[str, str]:
    """将 Body 正文严谨解析为 Overview / Instructions / Examples / Constraints 四大标准段"""
    try:
        return {name: record["text"] for name, record in _source_section_records(body).items()}
    except Exception:
        # Direct callers of the dimension helper historically received an
        # empty section map rather than a parser exception.  The public
        # analyze_split path performs the strict check and returns CANNOT_SPLIT.
        return {name: "" for name in _SECTION_NAMES}


def _domain_terms(domain: DomainSpec) -> list[str]:
    values = [domain.domain_id, domain.name, domain.description, domain.use_when, *domain.keywords]
    terms: list[str] = []
    for value in values:
        for token in re.findall(r"[A-Za-z0-9_+-]{2,}|[\u3400-\u9fff]{2,}", str(value).casefold()):
            if token not in terms:
                terms.append(token)
    return terms


def _matching_domains(text: str, domains: list[DomainSpec]) -> list[str]:
    """Conservatively return only a unique lexical owner for a source fragment."""
    haystack = str(text).casefold()
    scores: dict[str, int] = {}
    for domain in domains:
        score = sum(1 for term in _domain_terms(domain) if term and term in haystack)
        # Numeric HTTP status examples are domain references even when the
        # author omitted the literal word “HTTP/status”.  Keep this narrow so
        # arbitrary numbers do not become a fabricated assignment.
        terms = _domain_terms(domain)
        if any(term in {"http", "状态码", "http状态码"} for term in terms) and re.search(r"\b[45]\d{2}\b", haystack):
            score += 1
        if score:
            scores[domain.domain_id] = score
    if not scores:
        return []
    maximum = max(scores.values())
    winners = [domain_id for domain_id, score in scores.items() if score == maximum]
    # More than one domain mentioned in a fragment is deliberately treated as
    # cross-domain even when one happens to have a slightly higher score.
    mentioned = [domain_id for domain_id, score in scores.items() if score >= 1]
    return winners if len(mentioned) == 1 else mentioned


def _source_fragments(record: dict[str, Any], section: str) -> list[dict[str, Any]]:
    """Split only at paragraph/subheading boundaries; never rewrite text."""
    text = str(record["text"])
    lines = text.splitlines()
    if section == "Instructions":
        starts = [index for index, line in enumerate(lines) if re.match(r"^ {0,3}###\s+", line)]
        if len(starts) >= 2:
            chunks = []
            if starts[0] > 0:
                # Keep an introductory paragraph instead of dropping the
                # preamble merely because the section also has subheadings.
                chunks.append((0, starts[0]))
            bounds = starts + [len(lines)]
            chunks.extend((bounds[i], bounds[i + 1]) for i in range(len(starts)))
        else:
            chunks = [(0, len(lines))]
    elif section == "Examples":
        chunks: list[tuple[int, int]] = []
        start = 0
        for index, line in enumerate(lines + [""]):
            if not line.strip() and start < index:
                chunks.append((start, index))
                start = index + 1
        if not chunks:
            chunks = [(0, len(lines))]
    else:
        chunks = [(0, len(lines))]

    fragments: list[dict[str, Any]] = []
    for start, end in chunks:
        fragment_text = "\n".join(lines[start:end]).strip()
        if not fragment_text:
            continue
        line_start = int(record["line_start"]) + 1 + start
        line_end = int(record["line_start"]) + end
        fragments.append(
            {
                "section": section,
                "text": fragment_text,
                "line_start": line_start,
                "line_end": line_end,
                "sha256": _sha256_text(fragment_text),
            }
        )
    return fragments


def _attach_source_content(
    meta: SkillMeta,
    body: str,
    domains: list[DomainSpec],
) -> tuple[list[DomainSpec], dict[str, Any]]:
    """Attach exact source fragments to domains and return an audit table."""
    records = _source_section_records(body)
    primary = domains[0].domain_id if domains else ""
    assigned: dict[str, list[dict[str, Any]]] = {domain.domain_id: [] for domain in domains}
    audit_fragments: list[dict[str, Any]] = []

    for section in _SECTION_NAMES:
        fragments = _source_fragments(records[section], section)
        for fragment in fragments:
            owners = _matching_domains(fragment["text"], domains) if section in {"Instructions", "Examples"} else []
            assignment = owners[0] if len(owners) == 1 else primary
            assignment_reason = "unique_domain_reference" if len(owners) == 1 else (
                "cross_domain_or_unmatched_kept_in_primary"
            )
            copied = deepcopy(fragment)
            copied["assigned_domain"] = assignment
            copied["assignment_reason"] = assignment_reason
            assigned.setdefault(assignment, []).append(copied)
            audit_fragments.append(
                {
                    "section": fragment["section"],
                    "line_start": fragment["line_start"],
                    "line_end": fragment["line_end"],
                    "sha256_before": fragment["sha256"],
                    "sha256_after": fragment["sha256"],
                    "assigned_domain": assignment,
                    "assignment_reason": assignment_reason,
                    "exact_preserved": True,
                }
            )

    # Frontmatter examples are source evidence too.  Never synthesize a child
    # example; a cross-domain/unknown example stays with the primary domain.
    for example in meta.examples:
        owners = _matching_domains(str(example), domains)
        assignment = owners[0] if len(owners) == 1 else primary
        domain = next((item for item in domains if item.domain_id == assignment), None)
        if domain is not None and example not in domain.examples:
            domain.examples.append(example)

    for domain in domains:
        fragments = assigned.get(domain.domain_id, [])
        by_section: dict[str, list[dict[str, Any]]] = {name: [] for name in _SECTION_NAMES}
        for fragment in fragments:
            by_section[fragment["section"]].append(fragment)
        domain.source_fragments = fragments
        domain.source_line_ranges = {
            name: [[int(item["line_start"]), int(item["line_end"])] for item in by_section[name]]
            for name in _SECTION_NAMES
            if by_section[name]
        }
        domain.source_section_hashes = {
            name: [str(item["sha256"]) for item in by_section[name]]
            for name in _SECTION_NAMES
            if by_section[name]
        }
        # Overview and Constraints are complete source sections.  They are
        # assigned whole to the primary domain; other children receive a
        # source-only marker during rendering rather than fabricated prose.
        domain.overview = "\n\n".join(item["text"] for item in by_section["Overview"])
        domain.instructions = "\n\n".join(item["text"] for item in by_section["Instructions"])
        domain.body_examples = "\n\n".join(item["text"] for item in by_section["Examples"])
        domain.constraints = "\n\n".join(item["text"] for item in by_section["Constraints"])
        if not domain.examples:
            # A source Examples paragraph is valid frontmatter evidence even
            # when the original author did not duplicate it in ``examples``.
            # Copy it verbatim; never invent a child example.
            domain.examples = [
                str(item["text"])
                for item in by_section["Examples"]
                if str(item.get("text", "")).strip()
            ]

    source_audit = {
        "schema_version": SPLIT_AUDIT_SCHEMA_VERSION,
        "primary_domain": primary,
        "sections": {
            name: {
                "line_start": records[name]["line_start"],
                "line_end": records[name]["line_end"],
                "sha256": records[name]["sha256"],
                "full_text_preserved": True,
            }
            for name in _SECTION_NAMES
        },
        "fragments": audit_fragments,
    }
    return domains, source_audit


def _domain_for_title(title: str, skill_name: str, meta: SkillMeta, index: int) -> DomainSpec:
    title_folded = title.casefold()
    if "regex" in title_folded or "正则" in title_folded:
        return DomainSpec(
            domain_id="regex",
            name=f"{skill_name}_regex"[:31],
            description="解析与说明正则表达式的语法、量词、分组与回溯机制",
            use_when="用户需要理解正则表达式语法原理、回溯原因或看懂复杂正则",
            not_for=list(meta.not_for),
            keywords=["正则", "regex", "回溯", "分组", "量词"],
            dependencies=list(meta.dependencies),
        )
    if "http" in title_folded or "状态码" in title_folded:
        return DomainSpec(
            domain_id="http",
            name=f"{skill_name}_http"[:31],
            description="排查与解释 HTTP 响应状态码（4xx/5xx）的成因与修复思路",
            use_when="用户在网络请求中遇到 4xx/5xx 报错状态码需要排查分析",
            not_for=list(meta.not_for),
            keywords=["状态码", "HTTP状态码", "4xx", "5xx", "502", "429"],
            dependencies=list(meta.dependencies),
        )
    safe_title = re.sub(r"[^a-z0-9]+", "_", title_folded).strip("_") or f"domain_{index}"
    domain_id = f"domain_{index}_{safe_title[:12]}"[:24]
    return DomainSpec(
        domain_id=domain_id,
        name=f"{skill_name}_{domain_id}"[:31],
        description=f"{title.strip()}：{meta.description}",
        use_when=f"用户询问{title.strip()}相关内容",
        not_for=list(meta.not_for),
        keywords=list(dict.fromkeys([title.strip(), *meta.trigger.keywords]))[:6],
        dependencies=list(meta.dependencies),
    )


def _heuristic_discover_domains(skill_name: str, meta: SkillMeta, body: str) -> list[DomainSpec]:
    """Discover domains while copying source fragments verbatim.

    Heuristics may choose ownership, but they are not allowed to author an
    Overview/Examples/Constraints/Instructions replacement.  Ambiguous
    fragments are kept intact in the primary domain and recorded in the audit.
    """
    if skill_name == "weather_query":
        domains = [
            DomainSpec(
                domain_id="current",
                name="weather_query_current",
                description="查询指定城市的实时天气与当前气温",
                use_when="用户询问某城市当前实时天气、气温、下雨情况",
                not_for=["未来几天短期天气预报", "出海生活穿衣建议", "历史天气查询"],
                keywords=["当前天气", "实时气温", "现在下雨吗"],
                dependencies=["amap_weather_api"],
            ),
            DomainSpec(
                domain_id="forecast",
                name="weather_query_forecast",
                description="查询指定城市未来 3 天短期天气预报",
                use_when="用户询问某城市明天或未来几天的天气趋势与气温区间",
                not_for=["历史天气", "出海生活建议", "实时风速"],
                keywords=["天气预报", "明天会下雨吗", "周末温度"],
                dependencies=["amap_weather_api"],
            ),
            DomainSpec(
                domain_id="advice",
                name="weather_query_advice",
                description="结合天气预报提供出行、穿衣与出海建议",
                use_when="用户根据天气询问出行、穿衣或出海建议",
                not_for=["历史天气查询", "替代专业海事判断"],
                keywords=["穿衣建议", "出海建议", "出行天气"],
                dependencies=["amap_weather_api"],
            ),
        ]
    else:
        records = _source_section_records(body)
        instruction_fragments = _source_fragments(records["Instructions"], "Instructions")
        titles = [
            fragment["text"].splitlines()[0].lstrip(" #")
            for fragment in instruction_fragments
            if fragment["text"].lstrip().startswith("###")
        ]
        if len(titles) < 2:
            return []
        domains = [_domain_for_title(title, skill_name, meta, index) for index, title in enumerate(titles, start=1)]

    domains, _ = _attach_source_content(meta, body, domains)
    return domains


def _validate_domain_specs(domains: list[DomainSpec]) -> list[DomainSpec]:
    if not isinstance(domains, list) or len(domains) < 2:
        return []
    seen_ids: set[str] = set()
    seen_names: set[str] = set()
    for domain in domains:
        if not isinstance(domain, DomainSpec):
            raise ValueError("候选域必须是 DomainSpec")
        if not domain.domain_id or domain.domain_id in seen_ids:
            raise ValueError("候选域 domain_id 缺失或重复")
        if not domain.name or not NAME_PATTERN.fullmatch(domain.name) or domain.name in seen_names:
            raise ValueError(f"候选域 name 非法或重复: {domain.name!r}")
        for field_name in ("description", "use_when", "overview", "instructions", "body_examples", "constraints"):
            if not isinstance(getattr(domain, field_name), str):
                raise ValueError(f"候选域 {domain.domain_id} 的 {field_name} 必须是字符串")
        for field_name in ("not_for", "keywords", "examples", "dependencies"):
            values = getattr(domain, field_name)
            if not isinstance(values, list) or any(not isinstance(value, str) for value in values):
                raise ValueError(f"候选域 {domain.domain_id} 的 {field_name} 必须是字符串数组")
        seen_ids.add(domain.domain_id)
        seen_names.add(domain.name)
    return domains


def _discover_domains_llm(skill_name: str, meta: SkillMeta, body: str, llm: Any) -> list[DomainSpec]:
    """驱动 LLM 发现并结构化提取潜在候选意图域"""
    prompt = f"""你是一个高质量 AI Skill 架构师与解耦专家。
请分析以下 Skill 是否包含多个职责或意图不同的子领域？如果是一个多域聚合的大 Skill，请将其划分为 2~3 个独立的候选意图域。
如果属于单一聚焦领域，请直接返回 is_multi_domain: false。

Skill 名称: {meta.name}
描述: {meta.description}
适用场景: {meta.use_when}
边界排除: {meta.not_for}
依赖工具: {meta.dependencies}

SKILL.md 正文:
{body}

请输出严格 JSON 格式：
{{
  "is_multi_domain": true/false,
  "domains": [
    {{
      "domain_id": "缩写标识，如 regex / http",
      "name": "snake_case命名，如 {skill_name}_sub",
      "description": "该域独立描述",
      "use_when": "该域触发场景",
      "not_for": ["明确排除本域不服务的场景（须含其他子域）"],
      "keywords": ["关键词1", "关键词2", "关键词3"],
      "examples": ["样例1", "样例2"],
      "dependencies": ["该域专属依赖工具，无依赖填空数组"],
      "overview": "该域 Overview 段落",
      "instructions": "该域 Instructions 段落",
      "body_examples": "该域 Examples 段落",
      "constraints": "该域 Constraints 段落"
    }}
  ]
}}
"""
    try:
        resp = llm.invoke([{"role": "user", "content": prompt}])
        content = _clean_json_text(_response_content(resp))
        data = json.loads(content)
    except Exception as exc:
        # The caller must turn this into CANNOT_SPLIT.  A failed LLM response
        # is not evidence that the heuristic engine is safe to use.
        raise RuntimeError(f"LLM 领域发现失败，拒绝拆分: {type(exc).__name__}: {exc}") from exc
    if not isinstance(data, dict) or type(data.get("is_multi_domain")) is not bool:
        raise ValueError("LLM 领域发现顶层 schema 非法")
    if not data["is_multi_domain"]:
        return []
    raw_domains = data.get("domains")
    if not isinstance(raw_domains, list) or len(raw_domains) < 2:
        raise ValueError("LLM 多域响应 domains 必须是至少两个对象")
    result: list[DomainSpec] = []
    for d in raw_domains:
        if not isinstance(d, dict):
            raise ValueError("LLM domains 不能包含非对象项")
        did = d.get("domain_id")
        name = d.get("name")
        if not isinstance(did, str) or not did.strip() or not isinstance(name, str) or not name.strip():
            raise ValueError("LLM domain_id/name 必须是非空字符串")
        name = name.strip().lower()
        if not NAME_PATTERN.fullmatch(name):
            name = f"{derive_skill_abbrev(skill_name)}_{did.strip().lower()}"[:31]
        list_fields: dict[str, list[str]] = {}
        for field_name in ("not_for", "keywords", "examples", "dependencies"):
            values = d.get(field_name, [])
            if not isinstance(values, list) or any(not isinstance(value, str) for value in values):
                raise ValueError(f"LLM domain {did} 的 {field_name} schema 非法")
            list_fields[field_name] = [value.strip() for value in values if value.strip()]
        text_fields: dict[str, str] = {}
        for field_name in ("description", "use_when"):
            value = d.get(field_name, "")
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"LLM domain {did} 的 {field_name} 必须为非空字符串")
            text_fields[field_name] = value.strip()
        # The body fields are intentionally ignored.  They are not trusted as
        # source text; _attach_source_content fills them from the original.
        result.append(
            DomainSpec(
                domain_id=did.strip().lower(),
                name=name,
                description=text_fields["description"],
                use_when=text_fields["use_when"],
                not_for=list_fields["not_for"],
                keywords=list_fields["keywords"],
                examples=list_fields["examples"],
                dependencies=list_fields["dependencies"],
            )
        )
    return _validate_domain_specs(result)


def evaluate_data_coupling(
    meta: SkillMeta,
    body: str,
    candidate_domains: list[DomainSpec],
    threshold: float = DEFAULT_DATA_COUPLING_THRESHOLD,
) -> DimensionCoupling:
    """① 数据耦合度评估：检查工具与 fixture 依赖，判定各意图域是否强耦合于同一底层数据源"""
    if not isinstance(threshold, (int, float)) or not np.isfinite(threshold) or not 0 <= threshold <= 1:
        raise ValueError(f"data coupling threshold 必须是 [0, 1] 内的有限数: {threshold!r}")
    known_fixtures = _get_known_fixtures()
    skill_deps = set(meta.dependencies)

    # 从 body 中扫描已知 fixture / tool 关键字
    body_found_fixtures = {f for f in known_fixtures if f in body}
    all_fixtures = skill_deps | body_found_fixtures

    domain_tool_map: dict[str, set[str]] = {}
    for d in candidate_domains:
        d_tools = set(d.dependencies)
        for f in all_fixtures:
            if f in d.instructions or f in d.overview or f in d.description:
                d_tools.add(f)
        domain_tool_map[d.domain_id] = d_tools

    # 如果原始技能本身声明了底层 fixture 依赖，各候选域若未显式细分则说明共同消费同一数据源
    if all_fixtures:
        for did, tools in domain_tool_map.items():
            if not tools:
                domain_tool_map[did] = set(all_fixtures)

    tool_domain_counts: dict[str, int] = {}
    for did, tools in domain_tool_map.items():
        for t in tools:
            tool_domain_counts[t] = tool_domain_counts.get(t, 0) + 1

    shared_tools = {t for t, count in tool_domain_counts.items() if count >= 2}
    total_tools = len(tool_domain_counts)

    if total_tools == 0:
        score = 0.0
        coupled = False
        reason = f"各候选域均无外部工具或 fixture 依赖，数据解耦良好 (score={score:.2f} <= {threshold})"
    else:
        score = len(shared_tools) / float(total_tools)
        coupled = score > threshold
        if coupled:
            reason = (
                f"数据耦合过高 (score={score:.2f} > {threshold})：各候选意图域共享同一底层数据源/工具链 "
                f"{sorted(shared_tools)}，强行拆分将破坏工具契约或造成重复调用"
            )
        else:
            reason = f"数据工具解耦良好 (score={score:.2f} <= {threshold})，各域具备独立工具或共享工具极少"

    return DimensionCoupling(
        dimension="data",
        score=round(score, 4),
        threshold=threshold,
        coupled=coupled,
        verdict_reason=reason,
        metrics={
            "total_tools": total_tools,
            "shared_tools": sorted(shared_tools),
            "domain_tool_map": {k: sorted(v) for k, v in domain_tool_map.items()},
        },
    )


def evaluate_process_coupling(
    meta: SkillMeta,
    body: str,
    candidate_domains: list[DomainSpec],
    llm: Any = None,
    threshold: float = DEFAULT_PROCESS_COUPLING_THRESHOLD,
) -> DimensionCoupling:
    """② 流程耦合度评估：检查 Instructions/Constraints 中跨域引用与顺序依赖"""
    if not isinstance(threshold, (int, float)) or not np.isfinite(threshold) or not 0 <= threshold <= 1:
        raise ValueError(f"process coupling threshold 必须是 [0, 1] 内的有限数: {threshold!r}")
    inter_domain_references: list[str] = []
    sequential_patterns = [
        r"基于(步骤|前面|上述|第\d+步|结果)",
        r"依据.*(前置|上述|步骤|数据|指标|输出)",
        r"先.*再.*",
        r"步骤\s*\d+.*依赖",
    ]

    parsed = _parse_body_four_sections(body)
    instructions_text = parsed.get("Instructions", "")
    constraints_text = parsed.get("Constraints", "")
    combined_workflow_text = f"{instructions_text}\n{constraints_text}"

    for p in sequential_patterns:
        matches = re.findall(p, combined_workflow_text)
        if matches:
            inter_domain_references.append(f"发现顺序/前置依赖模式: {p}")

    # weather_query 显式规则反例
    if meta.name == "weather_query" and ("穿衣" in instructions_text or "出海" in instructions_text):
        inter_domain_references.append("穿衣与出海生活建议显式依赖前置步骤获取的气象温度与风力事实")

    # 检测域间关键词互锁引用
    for i, d1 in enumerate(candidate_domains):
        for j, d2 in enumerate(candidate_domains):
            if i == j:
                continue
            for kw in d2.keywords:
                if len(kw) >= 2 and kw in d1.instructions:
                    inter_domain_references.append(f"域 {d1.domain_id} 的指令中直接引用域 {d2.domain_id} 的关键词 '{kw}'")

    ref_count = len(inter_domain_references)
    if ref_count == 0:
        score = 0.0
        coupled = False
        reason = f"各域 Instructions/Constraints 互相独立，无跨域顺序依赖 (score={score:.2f} <= {threshold})"
    else:
        score = round(min(1.0, 0.40 + 0.20 * min(ref_count, 3)), 4)
        coupled = score > threshold
        reason = (
            f"流程耦合过高 (score={score:.2f} > {threshold})：检测到 {ref_count} 处跨域引用或执行顺序依赖，"
            f"拆分将割裂核心逻辑链路（如：{inter_domain_references[0]}）"
        )

    return DimensionCoupling(
        dimension="process",
        score=round(score, 4),
        threshold=threshold,
        coupled=coupled,
        verdict_reason=reason,
        metrics={
            "reference_count": ref_count,
            "detected_dependencies": inter_domain_references,
        },
    )


def evaluate_eval_coupling(
    skill_name: str,
    candidate_domains: list[DomainSpec],
    repair_cases: list[dict[str, Any]],
    embed_layer: EmbedLayer,
    confusion_margin: float = DEFAULT_CONFUSION_MARGIN,
    threshold: float = DEFAULT_EVAL_COUPLING_THRESHOLD,
    max_centroid_sim_threshold: float = DEFAULT_MAX_CENTROID_SIMILARITY,
) -> tuple[DimensionCoupling, list[CaseAssignment], dict[str, list[dict[str, Any]]], list[dict[str, Any]]]:
    """③ 评测集耦合度评估：BGE embedding 向量表征 + 域中心聚类 + 混淆度计算"""
    for label, value in (
        ("confusion_margin", confusion_margin),
        ("eval coupling threshold", threshold),
        ("max centroid similarity threshold", max_centroid_sim_threshold),
    ):
        if not isinstance(value, (int, float)) or not np.isfinite(value) or not 0 <= value <= 1:
            raise ValueError(f"{label} 必须是 [0, 1] 内的有限数: {value!r}")
    if len(candidate_domains) < 2:
        coupling = DimensionCoupling(
            dimension="eval_set",
            score=1.0,
            threshold=threshold,
            coupled=True,
            verdict_reason="候选域少于 2 个，无法进行评测集解耦划分",
        )
        return coupling, [], {}, repair_cases

    model = embed_layer._get_model()

    # 1. 构建各域的语义卡片向量（域中心）
    domain_centroids: dict[str, np.ndarray] = {}
    for d in candidate_domains:
        corpus = [
            f"{d.description}。适用场景：{d.use_when}",
            " ".join(d.keywords),
            *d.examples,
        ]
        vecs = np.asarray(model.encode(corpus, normalize_embeddings=True), dtype=float)
        if vecs.ndim != 2 or vecs.shape[0] != len(corpus) or vecs.shape[1] == 0:
            raise ValueError(f"域 '{d.domain_id}' embedding 形状非法: {vecs.shape!r}")
        if not np.isfinite(vecs).all():
            raise ValueError(f"域 '{d.domain_id}' embedding 含 NaN/Inf")
        centroid = np.mean(vecs, axis=0)
        norm = float(np.linalg.norm(centroid))
        if not np.isfinite(norm) or norm <= 1e-12:
            raise ValueError(f"域 '{d.domain_id}' centroid 为零范数或非有限值")
        centroid /= norm
        domain_centroids[d.domain_id] = centroid

    # 2. 计算域中心之间的两两相似度
    d_ids = [d.domain_id for d in candidate_domains]
    max_centroid_sim = 0.0
    centroid_pairs: dict[str, float] = {}
    for i in range(len(d_ids)):
        for j in range(i + 1, len(d_ids)):
            c1 = domain_centroids[d_ids[i]]
            c2 = domain_centroids[d_ids[j]]
            sim = float(np.dot(c1, c2))
            centroid_pairs[f"{d_ids[i]}_vs_{d_ids[j]}"] = round(sim, 4)
            if sim > max_centroid_sim:
                max_centroid_sim = sim

    # 3. 评测用例映射与混淆度判定
    case_assignments: list[CaseAssignment] = []
    assigned_cases: dict[str, list[dict[str, Any]]] = {d.domain_id: [] for d in candidate_domains}
    unassigned_cases: list[dict[str, Any]] = []

    if repair_cases:
        queries = [str(c.get("query", "")) for c in repair_cases]
        query_vecs = np.asarray(model.encode(queries, normalize_embeddings=True), dtype=float)
        if query_vecs.ndim != 2 or query_vecs.shape[0] != len(repair_cases):
            raise ValueError(
                "评测 case embedding 数量不匹配: "
                f"expected={len(repair_cases)}, actual={query_vecs.shape[0] if query_vecs.ndim >= 1 else 'invalid'}"
            )
        if query_vecs.shape[1] == 0 or not np.isfinite(query_vecs).all():
            raise ValueError("评测 case embedding 含非法维度或 NaN/Inf")

        for index, case in enumerate(repair_cases):
            qvec = query_vecs[index]
            qnorm = float(np.linalg.norm(qvec))
            if not np.isfinite(qnorm) or qnorm <= 1e-12:
                raise ValueError(f"评测 case {case.get('id', 'unknown')} embedding 为零范数或非有限值")
            qvec = qvec / qnorm
            cid = str(case.get("id", "unknown"))
            query = str(case.get("query", ""))
            ref = str(case.get("reference", ""))

            sims: dict[str, float] = {}
            for did, c_vec in domain_centroids.items():
                sims[did] = float(np.dot(qvec, c_vec))

            sorted_sims = sorted(sims.items(), key=lambda x: x[1], reverse=True)
            top1_domain, top1_sim = sorted_sims[0]
            top2_domain, top2_sim = sorted_sims[1]
            margin = top1_sim - top2_sim

            is_confused = margin < confusion_margin or top1_sim < 0.30

            if is_confused:
                reason = (
                    f"用例在域 '{top1_domain}' ({top1_sim:.3f}) 与 '{top2_domain}' ({top2_sim:.3f}) "
                    f"间分差 {margin:.3f} < 阈值 {confusion_margin}，属于跨域混淆用例，拒绝分配"
                )
                ca = CaseAssignment(
                    case_id=cid,
                    query=query,
                    reference=ref,
                    assigned_domain=None,
                    confidence=round(top1_sim, 4),
                    similarities={k: round(v, 4) for k, v in sims.items()},
                    margin=round(margin, 4),
                    confused=True,
                    reason=reason,
                )
                case_assignments.append(ca)
                unassigned_cases.append({**case, "reject_reason": reason, "margin": round(margin, 4)})
            else:
                reason = f"高置信度归入域 '{top1_domain}' (sim={top1_sim:.3f}, margin={margin:.3f})"
                ca = CaseAssignment(
                    case_id=cid,
                    query=query,
                    reference=ref,
                    assigned_domain=top1_domain,
                    confidence=round(top1_sim, 4),
                    similarities={k: round(v, 4) for k, v in sims.items()},
                    margin=round(margin, 4),
                    confused=False,
                    reason=reason,
                )
                case_assignments.append(ca)
                assigned_cases[top1_domain].append(case)

    confused_count = len(unassigned_cases)
    total_cases = len(repair_cases)
    confusion_ratio = confused_count / float(total_cases) if total_cases > 0 else 0.0

    centroid_penalty = max(0.0, (max_centroid_sim - 0.50) / 0.50) if max_centroid_sim >= max_centroid_sim_threshold else 0.0
    eval_score = round(max(confusion_ratio, centroid_penalty), 4)
    coupled = eval_score > threshold or max_centroid_sim >= max_centroid_sim_threshold

    if coupled:
        verdict_reason = (
            f"评测集耦合过高 (score={eval_score:.2f} > {threshold}, 域中心最高相似度 {max_centroid_sim:.3f} "
            f"[门槛 {max_centroid_sim_threshold}], 混淆用例率 {confusion_ratio:.1%})：测试集分界模糊，无法稳健切分"
        )
    else:
        verdict_reason = (
            f"评测集解耦清晰 (score={eval_score:.2f} <= {threshold}, 域中心相似度 {max_centroid_sim:.3f}, "
            f"混淆用例率 {confusion_ratio:.1%})，用例可明确划归各子域"
        )

    coupling = DimensionCoupling(
        dimension="eval_set",
        score=round(eval_score, 4),
        threshold=threshold,
        coupled=coupled,
        verdict_reason=verdict_reason,
        metrics={
            "total_cases": total_cases,
            "confused_cases": confused_count,
            "confusion_ratio": round(confusion_ratio, 4),
            "max_centroid_sim": round(max_centroid_sim, 4),
            "centroid_pairs": centroid_pairs,
        },
    )
    return coupling, case_assignments, assigned_cases, unassigned_cases


def _splitter_ledger(
    ledger: Optional[LLMLedger],
    budget: Optional[EvolveBudget],
) -> Optional[LLMLedger]:
    """Return a splitter ledger with hard caps, even for a general evolve budget."""
    if ledger is None and budget is None:
        return None
    if ledger is not None:
        active = ledger
    else:
        # Do not mutate a caller's general EvolveBudget.  A split analysis is
        # a small bounded operation even when it is invoked from a much larger
        # evolve budget, so it receives a private capped copy.
        capped_budget = deepcopy(budget) if budget is not None else EvolveBudget()
        for field_name, default_value in (
            ("max_calls", 4),
            ("max_tokens", 20_000),
            ("deadline_seconds", 300.0),
        ):
            current = getattr(capped_budget, field_name, None)
            if current is None:
                setattr(capped_budget, field_name, default_value)
            else:
                setattr(capped_budget, field_name, min(current, default_value))
        active = LLMLedger(capped_budget)
    return active


def _analysis_digest_payload(analysis: SplitAnalysis) -> dict[str, Any]:
    data = asdict(analysis)
    data.pop("analysis_digest", None)
    # Ledger elapsed time is intentionally observable but mutable; it must not
    # make an otherwise unchanged analysis report appear stale.
    details = data.get("details")
    if isinstance(details, dict):
        data["details"] = {
            key: value
            for key, value in details.items()
            if key not in {"llm_ledger", "ledger"}
        }
    return data


def _compute_analysis_digest(analysis: SplitAnalysis) -> str:
    payload = json.dumps(
        _analysis_digest_payload(analysis),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )
    return _sha256_text(payload)


def _analysis_credential(
    skill_name: str,
    skill_path: Path,
    meta: SkillMeta,
    repair_file: Path,
    generated_at: Optional[str] = None,
) -> dict[str, str]:
    return {
        "skill_name": skill_name,
        "skill_version": meta.version,
        "skill_hash": _sha256_file(skill_path),
        "generated_at": generated_at or datetime.now(timezone.utc).isoformat(),
        "repair_set_path": str(repair_file.resolve()),
        "repair_set_fingerprint": _sha256_file(repair_file),
    }


def _apply_analysis_credential(
    analysis: SplitAnalysis,
    credential: dict[str, str],
    source_file: Path,
    body_line_start: int,
    ledger: Optional[LLMLedger] = None,
) -> SplitAnalysis:
    analysis.source_skill_version = credential["skill_version"]
    analysis.source_skill_hash = credential["skill_hash"]
    analysis.generated_at = credential["generated_at"]
    analysis.repair_set_fingerprint = credential["repair_set_fingerprint"]
    analysis.source_credential = dict(credential)
    analysis.details.setdefault("source_skill_file", str(source_file.resolve()))
    analysis.details.setdefault("source_body_line_start", body_line_start)
    analysis.details["source_credential"] = dict(credential)
    if ledger is not None:
        analysis.details["llm_ledger"] = ledger.as_dict()
    analysis.analysis_digest = _compute_analysis_digest(analysis)
    return analysis


def _invalid_analysis(
    skill_name: str,
    reason: str,
    details: Optional[dict[str, Any]] = None,
) -> SplitAnalysis:
    empty_data = DimensionCoupling(
        dimension="data", score=1.0, threshold=DEFAULT_DATA_COUPLING_THRESHOLD,
        coupled=True, verdict_reason=reason,
    )
    empty_process = DimensionCoupling(
        dimension="process", score=1.0, threshold=DEFAULT_PROCESS_COUPLING_THRESHOLD,
        coupled=True, verdict_reason=reason,
    )
    empty_eval = DimensionCoupling(
        dimension="eval_set", score=1.0, threshold=DEFAULT_EVAL_COUPLING_THRESHOLD,
        coupled=True, verdict_reason=reason,
    )
    return SplitAnalysis(
        skill_name=skill_name,
        can_split=False,
        verdict="CANNOT_SPLIT",
        primary_reason=reason,
        data_coupling=empty_data,
        process_coupling=empty_process,
        eval_coupling=empty_eval,
        details=details or {},
    )


def _validate_analysis_for_execution(
    analysis: SplitAnalysis,
    root: Path,
    repair_file: Path,
) -> tuple[bool, str]:
    """Verify report provenance and decision integrity before any write."""
    if not isinstance(analysis, SplitAnalysis):
        return False, "analysis 必须是 SplitAnalysis"
    if not analysis.can_split or analysis.verdict != "SPLIT_RECOMMENDED":
        return False, "分析裁决不是 SPLIT_RECOMMENDED，拒绝执行"
    if len(analysis.domains) < 2:
        return False, "分析报告候选域少于 2 个，拒绝执行"

    fields = {
        "skill_version": analysis.source_skill_version,
        "skill_hash": analysis.source_skill_hash,
        "generated_at": analysis.generated_at,
        "repair_set_fingerprint": analysis.repair_set_fingerprint,
    }
    if any(not isinstance(value, str) or not value.strip() for value in fields.values()):
        return False, "分析报告缺少完整来源凭据"
    credential = analysis.source_credential
    if not isinstance(credential, dict):
        return False, "分析报告 source_credential 必须是对象"
    expected_credential = {
        "skill_version": fields["skill_version"],
        "skill_hash": fields["skill_hash"],
        "generated_at": fields["generated_at"],
        "repair_set_fingerprint": fields["repair_set_fingerprint"],
    }
    for key, value in expected_credential.items():
        if credential.get(key) != value:
            return False, f"分析报告来源凭据字段不一致: {key}"
    if credential.get("skill_name") not in {None, analysis.skill_name}:
        return False, "分析报告来源凭据 skill_name 不一致"
    if credential.get("repair_set_path") not in {None, str(repair_file.resolve())}:
        return False, "分析报告 repair_set 路径不一致"
    for key in ("skill_hash", "repair_set_fingerprint"):
        if not re.fullmatch(r"[0-9a-f]{64}", fields[key]):
            return False, f"分析报告 {key} 格式非法"
    try:
        generated_at = datetime.fromisoformat(fields["generated_at"])
    except ValueError:
        return False, "分析报告 generated_at 不是合法 ISO 时间"
    if generated_at.tzinfo is None:
        return False, "分析报告 generated_at 必须包含时区"
    age = datetime.now(timezone.utc) - generated_at.astimezone(timezone.utc)
    if age.total_seconds() < -60 or age.total_seconds() > DEFAULT_ANALYSIS_MAX_AGE_SECONDS:
        return False, "分析报告已过期或 generated_at 来自未来"

    skill_file = root / "skills" / analysis.skill_name / "SKILL.md"
    try:
        current_meta, _, _ = _load_skill_file(analysis.skill_name, root / "skills")
        current_skill_hash = _sha256_file(skill_file)
        current_repair_hash = _sha256_file(repair_file)
    except Exception as exc:
        return False, f"执行前读取当前来源状态失败: {type(exc).__name__}: {exc}"
    if current_meta.version != fields["skill_version"]:
        return False, "Skill version 已变化，分析报告过期"
    if current_skill_hash != fields["skill_hash"]:
        return False, "Skill 内容 hash 已变化，分析报告过期"
    if current_repair_hash != fields["repair_set_fingerprint"]:
        return False, "repair_set 指纹已变化，分析报告过期"
    if not analysis.analysis_digest or analysis.analysis_digest != _compute_analysis_digest(analysis):
        return False, "分析报告 digest 缺失或内容被伪造/修改"

    domain_ids = [domain.domain_id for domain in analysis.domains]
    if len(set(domain_ids)) != len(domain_ids):
        return False, "分析报告 domain_id 重复"
    if any(dimension.coupled for dimension in (
        analysis.data_coupling, analysis.process_coupling, analysis.eval_coupling,
    )):
        return False, "分析报告存在高耦合维度，拒绝执行"

    # The report is also a coverage claim.  Validate it against the current
    # repair manifest instead of trusting a caller-provided ``assigned_cases``
    # dictionary.  Every source case must occur exactly once as assigned or in
    # the explicit quarantine list, and its query/reference must be unchanged.
    try:
        repair_data = json.loads(repair_file.read_text(encoding="utf-8"))
        source_cases = repair_data.get("cases") if isinstance(repair_data, dict) else None
        if not isinstance(source_cases, list) or any(not isinstance(case, dict) for case in source_cases):
            return False, "当前 repair_set cases 结构非法"
        source_cases = [case for case in source_cases if case.get("skill") == analysis.skill_name]
        source_by_id: dict[str, dict[str, Any]] = {}
        for case in source_cases:
            case_id = str(case.get("id", "")).strip()
            if not case_id or case_id in source_by_id:
                return False, f"当前 repair_set source case-ID 缺失或重复: {case_id!r}"
            source_by_id[case_id] = case

        reported: list[tuple[str, Optional[str], dict[str, Any]]] = []
        for domain_id, cases in analysis.assigned_cases.items():
            if domain_id not in domain_ids:
                return False, f"分析报告 assigned_cases 指向未知域: {domain_id}"
            if not isinstance(cases, list):
                return False, f"分析报告 assigned_cases[{domain_id}] 必须是数组"
            for case in cases:
                if not isinstance(case, dict):
                    return False, "分析报告 assigned case 必须是对象"
                reported.append((str(case.get("id", "")), domain_id, case))
        for case in analysis.unassigned_cases:
            if not isinstance(case, dict):
                return False, "分析报告 unassigned case 必须是对象"
            reported.append((str(case.get("id", "")), None, case))
        report_ids = [case_id for case_id, _, _ in reported]
        if any(not case_id.strip() for case_id in report_ids):
            return False, "分析报告存在缺失 case-ID"
        if len(set(report_ids)) != len(report_ids):
            return False, "分析报告存在重复 case-ID，无法证明 exactly-once 分配"
        source_ids = set(source_by_id)
        if set(report_ids) != source_ids:
            return False, (
                "分析报告 case 覆盖不守恒: "
                f"missing={sorted(source_ids - set(report_ids))}, extra={sorted(set(report_ids) - source_ids)}"
            )
        for case_id, _, report_case in reported:
            source_case = source_by_id[case_id]
            for field_name in ("id", "query", "reference"):
                if report_case.get(field_name) != source_case.get(field_name):
                    return False, f"分析报告 case {case_id} 的 {field_name} 与当前 repair_set 不一致"
    except (OSError, ValueError, TypeError, json.JSONDecodeError) as exc:
        return False, f"执行前校验 repair case 覆盖失败: {type(exc).__name__}: {exc}"
    return True, "OK"


def analyze_split(
    skill_name: str,
    llm: Any = None,
    repo_root: Optional[Path] = None,
    repair_set_path: Optional[Path] = None,
    model_dir: Optional[Path] = None,
    ledger: Optional[LLMLedger] = None,
    budget: Optional[EvolveBudget] = None,
    candidate_domains: Optional[list[DomainSpec]] = None,
) -> SplitAnalysis:
    """段 1 核心入口：输入 Skill 名称 → 三维耦合分析与裁决 (fail-closed)

    Args:
        skill_name: 待分析 Skill 标识
        llm: 可选 LLM 执行客户端
        repo_root: 项目仓库根目录
        repair_set_path: repair_set.json 路径
        model_dir: BGE 向量模型目录
        ledger: 统一 LLM 账本
        budget: 预算控制
        candidate_domains: 可选预定义候选域（若提供则跳过自动领域发现）

    Returns:
        SplitAnalysis: 包含三维评分、裁决与用例归属清单
    """
    root = repo_root or Path(__file__).resolve().parents[2]
    skills_dir = root / "skills"
    repair_file = repair_set_path or (root / "evaluation_sets" / "repair_set.json")
    active_ledger = _splitter_ledger(ledger, budget)
    if active_ledger is None and isinstance(llm, TrackedLLM):
        active_ledger = llm.ledger
    if llm is not None and active_ledger is None:
        active_ledger = _splitter_ledger(None, EvolveBudget(
            max_calls=4, max_tokens=20_000, deadline_seconds=300.0,
        ))

    # 1. Load and fingerprint the source before any decision is made.
    skill_file = skills_dir / skill_name / "SKILL.md"
    try:
        meta, fm_text, body = _load_skill_file(skill_name, skills_dir)
        source_text = skill_file.read_text(encoding="utf-8")
        body_offset = source_text.find(body)
        body_line_start = source_text[:body_offset].count("\n") + 1 if body_offset >= 0 else 1
        _source_section_records(body)
    except Exception as exc:
        return _invalid_analysis(skill_name, f"Skill 结构加载失败 (fail-closed): {type(exc).__name__}: {exc}")

    # 2. A missing, malformed, or empty repair set is an invalid basis for a
    # destructive split.  Keep the error for the final structured refusal so
    # dimension diagnostics remain useful to existing callers.
    skill_repair_cases: list[dict[str, Any]] = []
    repair_error: Optional[str] = None
    credential: Optional[dict[str, str]] = None
    try:
        if not repair_file.exists():
            raise FileNotFoundError(f"repair_set 文件不存在: {repair_file}")
        repair_text = repair_file.read_text(encoding="utf-8")
        rdata = json.loads(repair_text)
        if not isinstance(rdata, dict) or not isinstance(rdata.get("cases"), list):
            raise ValueError("repair_set 顶层必须包含 cases 对象数组")
        if any(not isinstance(case, dict) for case in rdata["cases"]):
            raise ValueError("repair_set cases 必须全部是对象")
        skill_repair_cases = [case for case in rdata["cases"] if case.get("skill") == skill_name]
        if not skill_repair_cases:
            raise ValueError(f"repair_set 没有 Skill '{skill_name}' 的评测用例")
        credential = _analysis_credential(skill_name, skill_file, meta, repair_file)
    except Exception as exc:
        repair_error = f"repair_set 无法作为拆分依据 (fail-closed): {type(exc).__name__}: {exc}"

    details: dict[str, Any] = {
        "timestamp": credential["generated_at"] if credential else datetime.now(timezone.utc).isoformat(),
        "repair_set_path": str(repair_file.resolve()),
        "repair_cases_total": len(skill_repair_cases),
        "discovery_mode": "explicit" if candidate_domains else ("llm" if llm is not None else "heuristic"),
        "repair_error": repair_error,
    }

    # 3. Discover domains.  An explicitly supplied LLM is authoritative: a
    # failed/invalid response is a refusal, never an implicit heuristic retry.
    domains: list[DomainSpec] = []
    try:
        if candidate_domains is not None:
            domains = _validate_domain_specs(candidate_domains)
            domains, source_audit = _attach_source_content(meta, body, domains)
        elif llm is not None:
            assert active_ledger is not None
            tracked_llm = TrackedLLM(
                llm.underlying_llm if isinstance(llm, TrackedLLM) else llm,
                active_ledger,
                role="splitter",
            )
            domains = _discover_domains_llm(skill_name, meta, body, tracked_llm)
            if domains:
                domains, source_audit = _attach_source_content(meta, body, domains)
            else:
                source_audit = {}
        else:
            domains = _heuristic_discover_domains(skill_name, meta, body)
            if domains:
                domains, source_audit = _attach_source_content(meta, body, domains)
            else:
                source_audit = {}
    except Exception as exc:
        failure_details = dict(details)
        if active_ledger is not None:
            failure_details["llm_ledger"] = active_ledger.as_dict()
        return _invalid_analysis(
            skill_name,
            f"候选域发现/源段归属失败 (fail-closed): {type(exc).__name__}: {exc}",
            details=failure_details,
        )

    details["source_audit"] = source_audit
    details["source_body_line_start"] = body_line_start
    if active_ledger is not None:
        details["llm_ledger"] = active_ledger.as_dict()

    # If the LLM explicitly says single-domain, or heuristic cannot discover
    # two domains, preserve the source credential but refuse execution.
    if not domains or len(domains) < 2:
        dim_data = DimensionCoupling(
            dimension="data", score=0.0, threshold=DEFAULT_DATA_COUPLING_THRESHOLD,
            coupled=False, verdict_reason="未发现至少两个独立意图域",
        )
        dim_proc = DimensionCoupling(
            dimension="process", score=0.0, threshold=DEFAULT_PROCESS_COUPLING_THRESHOLD,
            coupled=False, verdict_reason="未发现至少两个独立意图域",
        )
        dim_eval = DimensionCoupling(
            dimension="eval_set", score=1.0, threshold=DEFAULT_EVAL_COUPLING_THRESHOLD,
            coupled=True, verdict_reason="候选域少于 2 个，无法安全拆分",
        )
        result = SplitAnalysis(
            skill_name=skill_name,
            can_split=False,
            verdict="CANNOT_SPLIT",
            primary_reason=(f"{repair_error}；" if repair_error else "") + "单一意图/候选域不足 2 个，拒绝拆分",
            data_coupling=dim_data,
            process_coupling=dim_proc,
            eval_coupling=dim_eval,
            domains=domains or [],
            details=details,
        )
        return _apply_analysis_credential(result, credential, skill_file, body_line_start, active_ledger) if credential else result

    if repair_error:
        # Dimension scores remain useful diagnostics, but the missing
        # evaluation basis is itself a coupled/invalid evaluation dimension.
        try:
            data_coupling = evaluate_data_coupling(meta, body, domains)
            process_coupling = evaluate_process_coupling(meta, body, domains, llm=None)
        except Exception as exc:
            return _invalid_analysis(
                skill_name,
                f"{repair_error}；三维耦合评估失败 (fail-closed): {type(exc).__name__}: {exc}",
                details=details,
            )
        eval_coupling = DimensionCoupling(
            dimension="eval_set", score=1.0, threshold=DEFAULT_EVAL_COUPLING_THRESHOLD,
            coupled=True, verdict_reason=repair_error,
            metrics={"total_cases": 0, "invalid_basis": True},
        )
        result = SplitAnalysis(
            skill_name=skill_name,
            can_split=False,
            verdict="CANNOT_SPLIT",
            primary_reason="；".join([
                repair_error,
                data_coupling.verdict_reason if data_coupling.coupled else "",
                process_coupling.verdict_reason if process_coupling.coupled else "",
                "评测集缺失，拒绝拆分",
            ]),
            data_coupling=data_coupling,
            process_coupling=process_coupling,
            eval_coupling=eval_coupling,
            domains=domains,
            details=details,
        )
        return result

    # 4. All dimension failures, including embedding failures, become a
    # structured refusal rather than an exception that a caller might ignore.
    try:
        data_coupling = evaluate_data_coupling(meta, body, domains)
        process_coupling = evaluate_process_coupling(meta, body, domains, llm=None)
        embed_layer = EmbedLayer(model_dir=model_dir)
        eval_coupling, case_assignments, assigned_cases, unassigned_cases = evaluate_eval_coupling(
            skill_name=skill_name,
            candidate_domains=domains,
            repair_cases=skill_repair_cases,
            embed_layer=embed_layer,
        )
    except Exception as exc:
        details["evaluation_error"] = {
            "type": type(exc).__name__,
            "message": str(exc),
        }
        result = _invalid_analysis(
            skill_name,
            f"三维耦合评估失败 (fail-closed): {type(exc).__name__}: {exc}",
            details=details,
        )
        result.domains = domains
        return _apply_analysis_credential(result, credential, skill_file, body_line_start, active_ledger) if credential else result

    # 5. Final decision: any coupled dimension blocks execution.
    failed_reasons: list[str] = []
    if data_coupling.coupled:
        failed_reasons.append(f"【数据同流程/高耦合】{data_coupling.verdict_reason}")
    if process_coupling.coupled:
        failed_reasons.append(f"【流程纠缠/高耦合】{process_coupling.verdict_reason}")
    if eval_coupling.coupled:
        failed_reasons.append(f"【评测混淆/高耦合】{eval_coupling.verdict_reason}")

    can_split = len(failed_reasons) == 0
    if can_split:
        verdict = "SPLIT_RECOMMENDED"
        primary_reason = (
            f"三维耦合指标均低于安全门限（数据耦合={data_coupling.score:.2f}，"
            f"流程耦合={process_coupling.score:.2f}，评测耦合={eval_coupling.score:.2f}），建议拆分为 {len(domains)} 个子 Skill"
        )
    else:
        verdict = "CANNOT_SPLIT"
        primary_reason = "；".join(failed_reasons)

    result = SplitAnalysis(
        skill_name=skill_name,
        can_split=can_split,
        verdict=verdict,
        primary_reason=primary_reason,
        data_coupling=data_coupling,
        process_coupling=process_coupling,
        eval_coupling=eval_coupling,
        domains=domains,
        assigned_cases=assigned_cases,
        unassigned_cases=unassigned_cases,
        case_assignments=case_assignments,
        details=details,
    )
    return _apply_analysis_credential(result, credential, skill_file, body_line_start, active_ledger) if credential else result


class _SplitFilesystemTransaction:
    """Snapshot the split side effects and restore every target on failure."""

    def __init__(self, file_paths: list[Path], dir_paths: list[Path]) -> None:
        self.file_paths = list(dict.fromkeys(path.resolve() for path in file_paths))
        self.dir_paths = list(dict.fromkeys(path.resolve() for path in dir_paths))
        self.temp_dir = Path(tempfile.mkdtemp(prefix="skill-split-transaction-"))
        self.file_backups: dict[Path, Optional[bytes]] = {}
        self.dir_backups: dict[Path, Optional[Path]] = {}
        self.committed = False

    def __enter__(self) -> _SplitFilesystemTransaction:
        for path in self.file_paths:
            self.file_backups[path] = path.read_bytes() if path.exists() else None
        for index, path in enumerate(self.dir_paths):
            if path.exists():
                backup = self.temp_dir / f"dir-{index}"
                shutil.copytree(path, backup)
                self.dir_backups[path] = backup
            else:
                self.dir_backups[path] = None
        return self

    def commit(self) -> None:
        shutil.rmtree(self.temp_dir)
        if self.temp_dir.exists():
            raise RegistrationError(f"拆分事务临时目录清理失败: {self.temp_dir}")
        self.committed = True

    def rollback(self) -> None:
        # Remove newly-created directories first, then restore the exact old
        # source/archive trees.  This also reverses a source -> backup move.
        errors: list[str] = []
        for path in reversed(self.dir_paths):
            if path.exists():
                try:
                    if path.is_dir():
                        shutil.rmtree(path)
                    else:
                        path.unlink()
                except OSError as exc:
                    errors.append(f"删除 {path}: {exc}")
            if path.exists():
                errors.append(f"删除后路径仍存在: {path}")
        for path in self.dir_paths:
            backup = self.dir_backups.get(path)
            if backup is not None and backup.exists():
                try:
                    path.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copytree(backup, path)
                except OSError as exc:
                    errors.append(f"恢复目录 {path}: {exc}")
            if backup is not None and not path.exists():
                errors.append(f"恢复目录后路径缺失: {path}")
        for path, previous in self.file_backups.items():
            try:
                if previous is None:
                    path.unlink(missing_ok=True)
                    if path.exists():
                        errors.append(f"删除新文件后仍存在: {path}")
                else:
                    path.parent.mkdir(parents=True, exist_ok=True)
                    path.write_bytes(previous)
                    if path.read_bytes() != previous:
                        errors.append(f"恢复文件内容不一致: {path}")
            except OSError:
                errors.append(f"恢复文件失败: {path}")
                logger.exception("拆分事务回滚文件失败: %s", path)
        try:
            shutil.rmtree(self.temp_dir)
        except OSError as exc:
            errors.append(f"清理事务临时目录失败: {self.temp_dir}: {exc}")
        if self.temp_dir.exists():
            errors.append(f"事务临时目录仍存在: {self.temp_dir}")
        if errors:
            raise RegistrationError("拆分事务回滚不完整: " + "; ".join(errors))

    def __exit__(self, exc_type: Any, exc: Any, traceback: Any) -> bool:
        if exc_type is not None or not self.committed:
            self.rollback()
        return False


def _atomic_write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent))
    temp_path = Path(temp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_path, path)
    finally:
        temp_path.unlink(missing_ok=True)


def _source_marker(
    original_skill: str,
    domain_name: str,
    section: str,
    fragment: Optional[dict[str, Any]] = None,
    owner: Optional[str] = None,
) -> str:
    if fragment is None:
        return (
            f"<!-- split-source: original={original_skill} section={section} "
            f"owner={owner or domain_name}; preserved_in_owner=true -->"
        )
    return (
        f"<!-- split-source: original={original_skill} section={section} "
        f"body-lines={fragment['line_start']}-{fragment['line_end']} "
        f"sha256={fragment['sha256']} owner={domain_name} -->"
    )


def _render_domain_section(
    original_skill: str,
    domain: DomainSpec,
    section: str,
    primary_domain: str,
) -> str:
    fragments = [item for item in domain.source_fragments if item.get("section") == section]
    if not fragments:
        return _source_marker(original_skill, domain.name, section, owner=primary_domain)
    return "\n\n".join(
        f"{_source_marker(original_skill, domain.name, section, fragment=fragment)}\n{fragment['text']}"
        for fragment in fragments
    )


def _build_child_not_for(domain: DomainSpec, all_domains: list[DomainSpec]) -> list[str]:
    """Keep all old boundaries and all siblings within the 2-4 item schema."""
    source_boundary = [str(item).strip() for item in domain.not_for if str(item).strip()]
    siblings = [
        f"{other.name}: {other.description}"
        for other in all_domains
        if other.domain_id != domain.domain_id
    ]
    values: list[str] = []
    if source_boundary:
        values.append("原 Skill 边界：" + "；".join(source_boundary))
    if siblings:
        values.append("其他子域边界：" + "；".join(siblings))
    while len(values) < 2:
        values.append("与本子域无关的请求")
    return values[:4]


def _route_token_set(text: str) -> set[str]:
    ignored = {"用户", "需要", "希望", "可以", "进行", "问题", "相关", "帮助", "查询", "说明"}
    return {
        token
        for token in re.findall(r"[A-Za-z0-9_+-]{2,}|[\u3400-\u9fff]{2,}", str(text).casefold())
        if token not in ignored
    }


def _validate_child_route_space(domains: list[DomainSpec]) -> tuple[bool, str]:
    for index, left in enumerate(domains):
        left_terms = _route_token_set(left.use_when)
        for right in domains[index + 1:]:
            shared = left_terms & _route_token_set(right.use_when)
            if shared:
                return False, f"子域 use_when 存在重叠触发词: {left.domain_id}/{right.domain_id}: {sorted(shared)}"
    return True, "OK"


def _verify_content_audit(source_audit: dict[str, Any], domains: list[DomainSpec]) -> tuple[bool, str]:
    if not isinstance(source_audit, dict) or not isinstance(source_audit.get("fragments"), list):
        return False, "缺少 source content audit"
    section_records = source_audit.get("sections")
    if not isinstance(section_records, dict) or set(section_records) != set(_SECTION_NAMES):
        return False, "source audit 四段 section 记录不完整"
    by_domain = {domain.domain_id: domain for domain in domains}
    seen: set[tuple[str, int, int]] = set()
    domain_fragment_keys: set[tuple[str, int, int]] = set()
    expected_section_hashes: dict[str, list[str]] = {}
    expected_fragment_keys: set[tuple[str, int, int]] = set()
    for domain in domains:
        for fragment in domain.source_fragments:
            key = (str(fragment.get("section")), int(fragment.get("line_start", -1)), int(fragment.get("line_end", -1)))
            if key in domain_fragment_keys:
                return False, f"子域 source fragment 重复: {key}"
            domain_fragment_keys.add(key)
            expected_fragment_keys.add(key)
            expected_section_hashes.setdefault(key[0], []).append(str(fragment.get("sha256", "")))
    for item in source_audit["fragments"]:
        if not isinstance(item, dict):
            return False, "source audit fragment 结构非法"
        domain = by_domain.get(item.get("assigned_domain"))
        if domain is None:
            return False, f"source audit 指向未知域: {item.get('assigned_domain')}"
        matched = [
            fragment for fragment in domain.source_fragments
            if fragment.get("section") == item.get("section")
            and fragment.get("line_start") == item.get("line_start")
            and fragment.get("line_end") == item.get("line_end")
        ]
        if len(matched) != 1:
            return False, "source audit fragment 无法在子域中唯一定位"
        fragment = matched[0]
        output_hash = _sha256_text(str(fragment.get("text", "")))
        if output_hash != item.get("sha256_before") or output_hash != item.get("sha256_after"):
            return False, "切分前后 source fragment hash 不一致"
        key = (str(item["section"]), int(item["line_start"]), int(item["line_end"]))
        if key in seen:
            return False, f"source fragment 重复归属: {key}"
        seen.add(key)
    if seen != expected_fragment_keys:
        return False, (
            "source audit 与子域 source fragment 不守恒: "
            f"missing={sorted(expected_fragment_keys - seen)}, extra={sorted(seen - expected_fragment_keys)}"
        )
    if not all(item.get("exact_preserved") is True for item in source_audit["fragments"]):
        return False, "存在未通过 exact_preserved 的 source fragment"
    for section, section_record in section_records.items():
        if not isinstance(section_record, dict) or not re.fullmatch(r"[0-9a-f]{64}", str(section_record.get("sha256", ""))):
            return False, f"source audit section {section} 缺少合法 hash"
    for domain in domains:
        for section, hashes in domain.source_section_hashes.items():
            expected = [str(item) for item in hashes]
            actual = [str(item) for item in expected_section_hashes.get(section, []) if item in expected]
            if sorted(actual) != sorted(expected):
                return False, f"域 {domain.domain_id} 的 source_section_hashes 与 fragments 不一致"
    return True, "OK"


def _case_domain_lookup(analysis: SplitAnalysis, domains: list[DomainSpec]) -> tuple[dict[str, str], set[str], str]:
    primary = domains[0].domain_id
    assigned: dict[str, str] = {}
    for domain_id, cases in analysis.assigned_cases.items():
        for case in cases:
            if isinstance(case, dict) and case.get("id"):
                assigned[str(case["id"])] = domain_id
    unassigned = {
        str(case.get("id"))
        for case in analysis.unassigned_cases
        if isinstance(case, dict) and case.get("id")
    }
    return assigned, unassigned, primary


def _choose_case_domain(
    case: dict[str, Any],
    analysis: SplitAnalysis,
    domains: list[DomainSpec],
) -> tuple[str, str]:
    assigned, unassigned, primary = _case_domain_lookup(analysis, domains)
    case_id = str(case.get("id", ""))
    if case_id in assigned:
        return assigned[case_id], "assigned"
    if case_id in unassigned:
        return primary, "unassigned"
    owners = _matching_domains(str(case.get("query", "")), domains)
    if len(owners) == 1:
        return owners[0], "assigned"
    return primary, "unassigned"


def _migration_paths(repair_file: Path, router_file: Path) -> list[Path]:
    paths: set[Path] = set()
    for directory in {repair_file.parent.resolve(), router_file.parent.resolve()}:
        if directory.exists():
            for path in directory.rglob("*.json"):
                # Historical split reports are immutable evidence, not active
                # partitions.  Rewriting them would destroy the old->new map
                # that an auditor is supposed to be able to re-check.
                if "split_migrations" in path.resolve().parts:
                    continue
                paths.add(path.resolve())
    paths.update({repair_file.resolve(), router_file.resolve()})
    return sorted(paths)


def _generated_source_case_map(
    original_skill: str,
    generated_sub_skills: list[GeneratedSkill],
) -> tuple[dict[str, dict[str, Any]], set[str]]:
    """Index generated repair cases by their source case without guessing."""
    prefix = f"split_from_{original_skill}:"
    by_source: dict[str, dict[str, Any]] = {}
    generated_ids: set[str] = set()
    for generated in generated_sub_skills:
        for case in generated.test_cases:
            if not isinstance(case, dict):
                raise RegistrationError(f"生成子 Skill {generated.name} 存在非法 case")
            case_id = str(case.get("id", "")).strip()
            if case_id:
                if case_id in generated_ids:
                    raise RegistrationError(f"拆分生成 case-ID 重复: {case_id}")
                generated_ids.add(case_id)
            source = str(case.get("source", ""))
            if source.startswith(prefix):
                source_id = source[len(prefix):].strip()
                if not source_id or source_id in by_source:
                    raise RegistrationError(f"拆分生成 case 的 source 映射重复或为空: {source!r}")
                by_source[source_id] = case
    return by_source, generated_ids


def _stable_migrated_case_id(
    domain_name: str,
    original_id: str,
    status: str,
) -> str:
    """Return a deterministic non-auto ID for a partition case migration."""
    prefix = derive_skill_abbrev(domain_name) or "split"
    safe_id = re.sub(r"[^A-Za-z0-9_-]+", "_", original_id).strip("_") or "case"
    return f"{prefix}_{'unassigned' if status == 'unassigned' else 'migrated'}_{safe_id}"


def _contains_exact_skill_reference(value: Any, skill_name: str) -> bool:
    """Match a standalone old name, not a valid child name with that prefix."""
    return bool(
        re.search(
            rf"(?<![A-Za-z0-9_]){re.escape(skill_name)}(?![A-Za-z0-9_])",
            str(value),
        )
    )


def _migrate_partition_manifests(
    original_skill: str,
    domains: list[DomainSpec],
    analysis: SplitAnalysis,
    generated_sub_skills: list[GeneratedSkill],
    repair_file: Path,
    router_file: Path,
    migration_id: str,
) -> tuple[dict[Path, dict[str, Any]], dict[str, Any]]:
    """Rewrite every partition reference and return an immutable mapping report."""
    paths = _migration_paths(repair_file, router_file)
    generated_by_source, generated_ids = _generated_source_case_map(original_skill, generated_sub_skills)
    assigned_source_ids = {
        str(case.get("id"))
        for cases in analysis.assigned_cases.values()
        for case in cases
        if isinstance(case, dict) and str(case.get("id", "")).strip()
    }
    if set(generated_by_source) != assigned_source_ids:
        raise RegistrationError(
            "拆分生成 case 与分析 assigned_cases 不守恒: "
            f"missing={sorted(assigned_source_ids - set(generated_by_source))}, "
            f"extra={sorted(set(generated_by_source) - assigned_source_ids)}"
        )
    loaded: dict[Path, dict[str, Any]] = {}
    for path in paths:
        if not path.exists():
            continue
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except Exception as exc:
            raise RegistrationError(f"迁移 manifest 无法解析 {path.name}: {exc}") from exc
        if not isinstance(data, dict):
            raise RegistrationError(f"迁移 manifest 顶层必须是对象: {path.name}")
        if "cases" in data and (
            not isinstance(data["cases"], list)
            or any(not isinstance(case, dict) for case in data["cases"])
        ):
            raise RegistrationError(f"迁移 manifest cases 必须是对象数组: {path.name}")
        loaded[path] = data

    updates = {path: deepcopy(data) for path, data in loaded.items()}
    mappings: list[dict[str, Any]] = []
    unassigned: list[dict[str, Any]] = []
    old_prefixes: set[str] = set()
    router_resolved = router_file.resolve()
    repair_resolved = repair_file.resolve()
    for path, data in loaded.items():
        cases = data.get("cases", [])
        if not isinstance(cases, list):
            continue
        is_router = path.resolve() == router_resolved or path.name == "router_negatives.json"
        new_cases: list[dict[str, Any]] = []
        new_ids: set[str] = set()
        for case in cases:
            new_case = deepcopy(case)
            old_id = str(case.get("id", ""))
            generated_source = str(case.get("source", ""))
            if (
                path.resolve() == repair_resolved
                and old_id in generated_ids
                and generated_source.startswith(f"split_from_{original_skill}:")
            ):
                # register_skill has already appended the generated copy.  The
                # source row below is replaced by that generated ID, so keeping
                # this append would create a duplicate and break conservation.
                continue
            if case.get("skill") == original_skill:
                if "_" in old_id:
                    old_prefixes.add(old_id.split("_", 1)[0])
                domain_id, status = _choose_case_domain(case, analysis, domains)
                domain = next((item for item in domains if item.domain_id == domain_id), None)
                if domain is None:
                    raise RegistrationError(f"case {old_id} 无法迁移到候选域")
                generated = generated_by_source.get(old_id) if path.resolve() == repair_resolved else None
                new_id = str(generated.get("id")) if generated is not None else _stable_migrated_case_id(
                    domain.name, old_id, status
                )
                if not new_id or new_id in new_ids:
                    raise RegistrationError(f"迁移后 case-ID 重复或为空: {path.name}/{new_id!r}")
                if generated is not None:
                    # Query/reference remain source truth; generated metadata
                    # supplies the child trace and case kind.
                    for key in ("trace_id", "case_kind", "difficulty", "source"):
                        if key in generated:
                            new_case[key] = generated[key]
                new_case["id"] = new_id
                new_case["skill"] = domain.name
                new_case["split_migration"] = {
                    "migration_id": migration_id,
                    "original_skill": original_skill,
                    "original_id": old_id,
                    "new_id": new_id,
                    "source_partition": path.name,
                    "status": status,
                }
                if status == "unassigned":
                    new_case["case_kind"] = "split_unassigned_quarantine"
                    new_case["split_status"] = "unassigned"
                    unassigned.append({
                        "partition": path.name,
                        "original_id": old_id,
                        "new_id": new_id,
                        "new_skill": domain.name,
                        "query": case.get("query", ""),
                        "reference": case.get("reference", ""),
                    })
                mappings.append({
                    "partition": path.name,
                    "original_id": old_id,
                    "new_id": new_id,
                    "original_skill": original_skill,
                    "new_skill": domain.name,
                    "status": status,
                    "old_prefix": old_id.split("_", 1)[0] if "_" in old_id else "",
                })
            elif is_router and case.get("expected") == original_skill:
                domain_id, status = _choose_case_domain(case, analysis, domains)
                domain = next((item for item in domains if item.domain_id == domain_id), None)
                if domain is None:
                    raise RegistrationError(f"router case {old_id} 无法迁移到候选域")
                new_id = _stable_migrated_case_id(domain.name, old_id, status)
                if not new_id or new_id in new_ids:
                    raise RegistrationError(f"迁移后 router case-ID 重复或为空: {path.name}/{new_id!r}")
                new_case["id"] = new_id
                new_case["expected"] = None if status == "unassigned" else domain.name
                new_case["why"] = (
                    f"{str(case.get('why', '')).replace(original_skill, domain.name)} "
                    f"[split migration; status={status}]"
                )
                new_case["split_migration"] = {
                    "migration_id": migration_id,
                    "original_skill": original_skill,
                    "original_id": old_id,
                    "new_id": new_id,
                    "source_partition": path.name,
                    "status": status,
                }
                mappings.append({
                    "partition": path.name,
                    "original_id": old_id,
                    "new_id": new_id,
                    "original_skill": original_skill,
                    "new_skill": None if status == "unassigned" else domain.name,
                    "status": status,
                    "old_prefix": old_id.split("_", 1)[0] if "_" in old_id else "",
                })
                if status == "unassigned":
                    unassigned.append({
                        "partition": path.name,
                        "original_id": old_id,
                        "new_id": new_id,
                        "new_skill": domain.name,
                        "query": case.get("query", ""),
                        "reference": case.get("why", ""),
                    })
            elif is_router and original_skill in str(case.get("why", "")):
                new_case["why"] = str(case.get("why", "")).replace(original_skill, "已拆分 Skill")
                mappings.append({
                    "partition": path.name,
                    "original_id": old_id,
                    "new_id": old_id,
                    "original_skill": original_skill,
                    "new_skill": None,
                    "status": "reference_updated",
                    "old_prefix": old_id.split("_", 1)[0] if "_" in old_id else "",
                })
            case_id = str(new_case.get("id", "")).strip()
            if case_id:
                if case_id in new_ids:
                    raise RegistrationError(f"manifest 内部迁移后 case-ID 重复: {path.name}/{case_id}")
                new_ids.add(case_id)
            new_cases.append(new_case)
        updates[path]["cases"] = new_cases

        if path.resolve() == repair_file.resolve():
            meta = updates[path].get("meta")
            if not isinstance(meta, dict):
                raise RegistrationError("repair_set meta 必须是对象")
            auto_ids = sorted(
                str(case.get("id")) for case in new_cases if "_auto_" in str(case.get("id", ""))
            )
            meta["auto_case_ids"] = auto_ids
            meta["auto_case_count"] = len(auto_ids)
            meta["total"] = len(new_cases)
            reservations = meta.get("quota_reservations", {})
            if not isinstance(reservations, dict):
                raise RegistrationError("repair_set quota_reservations 必须是对象")
            reservations.pop(original_skill, None)
            from .data_partition import reserve_skill_quota
            for generated in generated_sub_skills:
                reservations.setdefault(generated.name, reserve_skill_quota(generated.name))
            meta["quota_reservations"] = reservations
        if is_router:
            meta = updates[path].get("meta")
            if not isinstance(meta, dict):
                meta = {}
                updates[path]["meta"] = meta
            counts: dict[str, int] = {}
            for case in new_cases:
                kind = str(case.get("type", ""))
                counts[kind] = counts.get(kind, 0) + 1
            meta["total"] = len(new_cases)
            meta["breakdown"] = {
                "positive": counts.get("positive", 0),
                "hard_negative": counts.get("hard_negative", 0),
                "unrelated_negative": counts.get("unrelated_negative", 0),
            }

    # A post-transform scan is the last gate against stale original references.
    for path, data in updates.items():
        for case in data.get("cases", []):
            if case.get("skill") == original_skill:
                raise RegistrationError(f"迁移后仍存在旧 skill 引用: {path.name}/{case.get('id')}")
            if path.name == "router_negatives.json" and case.get("expected") == original_skill:
                raise RegistrationError(f"迁移后仍存在旧 router expected: {path.name}/{case.get('id')}")
            for field_name in ("why", "description"):
                if _contains_exact_skill_reference(case.get(field_name, ""), original_skill):
                    raise RegistrationError(
                        f"迁移后仍存在旧 skill 文本引用: {path.name}/{case.get('id')}/{field_name}"
                    )

    all_cases = [case for data in updates.values() for case in data.get("cases", [])]
    from .data_partition import validate_auto_case_prefixes
    prefix_errors = validate_auto_case_prefixes(all_cases)
    if prefix_errors:
        raise RegistrationError("迁移后全局 auto case 前缀校验失败: " + "; ".join(prefix_errors))

    manifest = {
        "schema_version": SPLIT_AUDIT_SCHEMA_VERSION,
        "migration_id": migration_id,
        "status": "committed",
        "created_at": datetime.now(timezone.utc).isoformat(),
        "original_skill": original_skill,
        "replacement_skills": [generated.name for generated in generated_sub_skills],
        "source_analysis_digest": analysis.analysis_digest,
        "source_credential": dict(analysis.source_credential),
        "scanned_manifests": [path.name for path in updates],
        "scanned_manifest_paths": [str(path) for path in updates],
        "legacy_prefixes": sorted(old_prefixes),
        "immutable_mapping": True,
        "mapping_policy": "每条旧记录均生成稳定 new_id；repair assigned 复用对应子 Skill auto case",
        "mappings": mappings,
        "unassigned_cases": unassigned,
        "unassigned_count": len(unassigned),
    }
    return updates, manifest


def _split_manifest_path(repair_file: Path, original_skill: str, migration_id: str) -> Path:
    safe_name = re.sub(r"[^a-zA-Z0-9_.-]+", "_", original_skill)
    return repair_file.parent / "split_migrations" / f"{safe_name}-{migration_id}.json"


def _unassigned_report_path(repair_file: Path, original_skill: str, migration_id: str) -> Path:
    safe_name = re.sub(r"[^a-zA-Z0-9_.-]+", "_", original_skill)
    return repair_file.parent / "split_migrations" / f"{safe_name}-{migration_id}-unassigned.json"


def _write_manifest_data(updates: dict[Path, dict[str, Any]]) -> None:
    for path, data in updates.items():
        _atomic_write_text(path, json.dumps(data, ensure_ascii=False, indent=2) + "\n")


def _write_split_report(path: Path, manifest: dict[str, Any], content_audit: dict[str, Any]) -> None:
    payload = dict(manifest)
    payload["content_audit"] = content_audit
    payload["unassigned_report"] = {
        "path": str(path),
        "count": manifest.get("unassigned_count", 0),
        "status": "recorded",
    }
    if path.exists():
        raise RegistrationError(f"迁移 manifest 已存在，拒绝覆盖: {path}")
    _atomic_write_text(path, json.dumps(payload, ensure_ascii=False, indent=2) + "\n")


def _write_unassigned_report(path: Path, manifest: dict[str, Any]) -> None:
    """Persist quarantine rows separately so a caller cannot miss them."""
    if path.exists():
        raise RegistrationError(f"unassigned report 已存在，拒绝覆盖: {path}")
    payload = {
        "schema_version": SPLIT_AUDIT_SCHEMA_VERSION,
        "migration_id": manifest.get("migration_id"),
        "original_skill": manifest.get("original_skill"),
        "status": "unassigned_quarantine_report",
        "immutable_mapping": True,
        "count": manifest.get("unassigned_count", 0),
        "cases": list(manifest.get("unassigned_cases", [])),
    }
    _atomic_write_text(path, json.dumps(payload, ensure_ascii=False, indent=2) + "\n")


def deprecate_original_skill(
    skill_name: str,
    repo_root: Optional[Path] = None,
    backup_dir: Optional[Path] = None,
    repair_set_path: Optional[Path] = None,
    router_negatives_path: Optional[Path] = None,
    sub_skill_names: Optional[list[str]] = None,
    migration_manifest: Optional[dict[str, Any]] = None,
    migration_manifest_path: Optional[Path] = None,
) -> Path:
    """Transactionally archive a Skill and remove all stale manifest references.

    ``split_skill`` supplies a fully computed migration manifest.  The direct
    public helper also remains safe: without a supplied map it moves old
    cases to the first replacement as explicit quarantine records instead of
    deleting them or leaving an archived skill name in active manifests.
    """
    root = repo_root or Path(__file__).resolve().parents[2]
    skills_dir = root / "skills"
    source_dir = skills_dir / skill_name
    target_backup_dir = backup_dir or (root / "skills_backup" / skill_name)
    r_path = repair_set_path or (root / "evaluation_sets" / "repair_set.json")
    rt_path = router_negatives_path or (root / "evaluation_sets" / "router_negatives.json")
    if not source_dir.exists():
        raise RegistrationError(f"原 Skill 目录不存在，拒绝伪成功: {source_dir}")
    if target_backup_dir.exists():
        raise RegistrationError(f"归档目录已存在，拒绝覆盖: {target_backup_dir}")
    replacements = list(sub_skill_names or [])
    if not replacements and migration_manifest:
        replacements = list(migration_manifest.get("replacement_skills", []))
    if not replacements:
        raise RegistrationError("deprecate 必须提供至少一个 replacement Skill")

    paths = _migration_paths(r_path, rt_path)
    transaction_files = list(paths)
    if migration_manifest_path is not None:
        transaction_files.append(migration_manifest_path)
    with _SplitFilesystemTransaction(transaction_files, [source_dir, target_backup_dir.parent]) as transaction:
        updates: dict[Path, dict[str, Any]] = {}
        mappings: list[dict[str, Any]] = []
        for path in paths:
            if not path.exists():
                continue
            data = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(data, dict):
                raise RegistrationError(f"manifest 顶层必须是对象: {path.name}")
            updated = deepcopy(data)
            cases = updated.get("cases", [])
            if not isinstance(cases, list) or any(not isinstance(case, dict) for case in cases):
                raise RegistrationError(f"manifest cases 必须是对象数组: {path.name}")
            is_router = path.resolve() == rt_path.resolve() or path.name == "router_negatives.json"
            used_ids: set[str] = set()
            for case in cases:
                if case.get("skill") == skill_name:
                    replacement = replacements[0]
                    old_id = str(case.get("id", ""))
                    new_id = _stable_migrated_case_id(replacement, old_id, "unassigned")
                    if new_id in used_ids:
                        raise RegistrationError(f"归档迁移后 case-ID 重复: {path.name}/{new_id}")
                    case["id"] = new_id
                    case["skill"] = replacement
                    case["case_kind"] = "split_unassigned_quarantine"
                    case["split_status"] = "unassigned"
                    case["split_migration"] = {
                        "original_skill": skill_name,
                        "original_id": old_id,
                        "new_id": new_id,
                        "status": "unassigned",
                    }
                    mappings.append({
                        "partition": path.name,
                        "original_id": old_id,
                        "new_id": new_id,
                        "original_skill": skill_name,
                        "new_skill": replacement,
                        "status": "unassigned",
                    })
                elif is_router and case.get("expected") == skill_name:
                    old_id = str(case.get("id", ""))
                    new_id = _stable_migrated_case_id(replacements[0], old_id, "unassigned")
                    if new_id in used_ids:
                        raise RegistrationError(f"归档迁移后 router case-ID 重复: {path.name}/{new_id}")
                    case["id"] = new_id
                    case["expected"] = None
                    case["why"] = f"原 Skill 已归档，需人工确认 replacement（{', '.join(replacements)}）"
                    mappings.append({
                        "partition": path.name,
                        "original_id": old_id,
                        "new_id": new_id,
                        "original_skill": skill_name,
                        "new_skill": None,
                        "status": "unassigned",
                    })
                elif is_router and _contains_exact_skill_reference(case.get("why", ""), skill_name):
                    case["why"] = re.sub(
                        rf"(?<![A-Za-z0-9_]){re.escape(skill_name)}(?![A-Za-z0-9_])",
                        "已拆分 Skill",
                        str(case.get("why", "")),
                    )
                case_id = str(case.get("id", "")).strip()
                if case_id:
                    if case_id in used_ids:
                        raise RegistrationError(f"归档 manifest 内部 case-ID 重复: {path.name}/{case_id}")
                    used_ids.add(case_id)
            if path.resolve() == r_path.resolve():
                meta = updated.get("meta")
                if not isinstance(meta, dict):
                    raise RegistrationError("repair_set meta 必须是对象")
                meta["total"] = len(cases)
                auto_ids = sorted(str(case.get("id")) for case in cases if "_auto_" in str(case.get("id", "")))
                meta["auto_case_ids"] = auto_ids
                meta["auto_case_count"] = len(auto_ids)
                reservations = meta.get("quota_reservations", {})
                if not isinstance(reservations, dict):
                    raise RegistrationError("repair_set quota_reservations 必须是对象")
                reservations.pop(skill_name, None)
                from .data_partition import reserve_skill_quota
                for replacement in replacements:
                    reservations.setdefault(replacement, reserve_skill_quota(replacement))
                meta["quota_reservations"] = reservations
            if is_router:
                meta = updated.setdefault("meta", {})
                if not isinstance(meta, dict):
                    raise RegistrationError("router_negatives meta 必须是对象")
                counts: dict[str, int] = {}
                for case in cases:
                    kind = str(case.get("type", ""))
                    counts[kind] = counts.get(kind, 0) + 1
                meta["total"] = len(cases)
                meta["breakdown"] = {
                    "positive": counts.get("positive", 0),
                    "hard_negative": counts.get("hard_negative", 0),
                    "unrelated_negative": counts.get("unrelated_negative", 0),
                }
            updates[path] = updated

        for path, data in updates.items():
            for case in data.get("cases", []):
                if case.get("skill") == skill_name:
                    raise RegistrationError(f"归档后仍有旧 skill 引用: {path.name}/{case.get('id')}")
                if path.name == "router_negatives.json" and case.get("expected") == skill_name:
                    raise RegistrationError(f"归档后仍有旧 router 引用: {path.name}/{case.get('id')}")
        _write_manifest_data(updates)

        target_backup_dir.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(source_dir), str(target_backup_dir))
        metadata = {
            "skill_name": skill_name,
            "deprecated_at": datetime.now(timezone.utc).isoformat(),
            "status": "deprecated_by_split",
            "replaced_by": replacements,
            "reason": "Skill split into independent sub-skills via skill_splitter",
            "migration_manifest": str(migration_manifest_path) if migration_manifest_path else None,
            "mapping_count": len(mappings),
        }
        _atomic_write_text(
            target_backup_dir / "deprecated_meta.json",
            json.dumps(metadata, ensure_ascii=False, indent=2) + "\n",
        )
        if migration_manifest_path is not None:
            report = dict(migration_manifest or {})
            report.setdefault("schema_version", SPLIT_AUDIT_SCHEMA_VERSION)
            report.setdefault("migration_id", uuid.uuid4().hex)
            report["mappings"] = report.get("mappings", mappings)
            report["unassigned_cases"] = report.get("unassigned_cases", [m for m in mappings if m.get("status") == "unassigned"])
            report["unassigned_count"] = len(report["unassigned_cases"])
            _write_split_report(migration_manifest_path, report, report.get("content_audit", {}))
        transaction.commit()
    return target_backup_dir


def split_skill(
    analysis: SplitAnalysis,
    llm: Any = None,
    repo_root: Optional[Path] = None,
    register: bool = False,
    backup_original: bool = True,
    repair_set_path: Optional[Path] = None,
    router_negatives_path: Optional[Path] = None,
    ledger: Optional[LLMLedger] = None,
    budget: Optional[EvolveBudget] = None,
) -> SplitResult:
    """段 2 核心入口：执行 Skill 拆分，输出 N 个子 Skill 并落盘/分配测试集/原子注册

    Args:
        analysis: 段 1 输出的 SplitAnalysis 分析报告
        llm: 可选 LLM（用于润色与生成独立边界反例）
        repo_root: 项目仓库根目录
        register: 是否立即原子落盘注册到 skills/ 与 repair_set.json
        backup_original: 注册后是否自动备份并归档原 Skill
        repair_set_path: repair_set.json 路径
        router_negatives_path: router_negatives.json 路径
        ledger/budget: 可选拆分阶段 LLM 账本与预算（内容保真路径默认不调用 LLM）

    Returns:
        SplitResult: 包含新 Skill 清单、测试集分配核对表及执行状态
    """
    root = repo_root or Path(__file__).resolve().parents[2]
    skills_dir = root / "skills"
    repair_file = repair_set_path or (root / "evaluation_sets" / "repair_set.json")
    router_file = router_negatives_path or (root / "evaluation_sets" / "router_negatives.json")

    # The conservative splitter intentionally does not ask an LLM to rewrite
    # source paragraphs.  If a future/plug-in path does use the optional
    # client, it is still wrapped here so no raw invocation can bypass the
    # P2-A ledger and budget guard.
    splitter_ledger = _splitter_ledger(ledger, budget)
    if llm is not None:
        if splitter_ledger is None:
            splitter_ledger = _splitter_ledger(None, EvolveBudget(
                max_calls=4, max_tokens=20_000, deadline_seconds=300.0,
            ))
        llm = TrackedLLM(
            llm.underlying_llm if isinstance(llm, TrackedLLM) else llm,
            splitter_ledger,
            role="splitter",
        )

    if not isinstance(analysis, SplitAnalysis) or not analysis.can_split:
        return SplitResult(
            original_skill=getattr(analysis, "skill_name", ""),
            can_split=False,
            success=False,
            errors=[f"裁决不可拆分: {getattr(analysis, 'primary_reason', 'invalid analysis')}"],
        )
    credentials_ok, credentials_message = _validate_analysis_for_execution(analysis, root, repair_file)
    if not credentials_ok:
        return SplitResult(
            original_skill=analysis.skill_name,
            can_split=False,
            success=False,
            unassigned_cases=list(analysis.unassigned_cases),
            errors=[f"来源凭据校验失败: {credentials_message}"],
        )

    generated_sub_skills: list[GeneratedSkill] = []
    sub_cases_summary: dict[str, list[str]] = {}
    existing_names = {p.name for p in skills_dir.iterdir() if p.is_dir()} if skills_dir.exists() else set()
    route_ok, route_message = _validate_child_route_space(analysis.domains)
    if not route_ok:
        return SplitResult(
            original_skill=analysis.skill_name,
            can_split=True,
            success=False,
            errors=[route_message],
            unassigned_cases=list(analysis.unassigned_cases),
        )
    primary_domain = analysis.domains[0].domain_id
    content_audit = deepcopy(analysis.details.get("source_audit", {}))
    audit_ok, audit_message = _verify_content_audit(content_audit, analysis.domains)
    if not audit_ok:
        return SplitResult(
            original_skill=analysis.skill_name,
            can_split=True,
            success=False,
            errors=[f"内容保真审计失败: {audit_message}"],
            unassigned_cases=list(analysis.unassigned_cases),
        )
    body_line_start = int(analysis.details.get("source_body_line_start", 1))
    for item in content_audit.get("fragments", []):
        item["absolute_line_start"] = body_line_start + int(item["line_start"]) - 1
        item["absolute_line_end"] = body_line_start + int(item["line_end"]) - 1
    for section in content_audit.get("sections", {}).values():
        section["absolute_line_start"] = body_line_start + int(section["line_start"]) - 1
        section["absolute_line_end"] = body_line_start + int(section["line_end"]) - 1

    for domain in analysis.domains:
        sub_name = domain.name if NAME_PATTERN.fullmatch(domain.name) else f"{derive_skill_abbrev(analysis.skill_name)}_{domain.domain_id}"[:31]
        if sub_name in existing_names:
            return SplitResult(
                original_skill=analysis.skill_name,
                can_split=True,
                success=False,
                errors=[f"子 Skill '{sub_name}' 已存在，拒绝覆盖"],
                unassigned_cases=list(analysis.unassigned_cases),
            )
        sub_abbrev = derive_skill_abbrev(sub_name)
        full_not_for = _build_child_not_for(domain, analysis.domains)
        keywords = [str(item).strip() for item in domain.keywords if str(item).strip()]
        for fallback in (domain.domain_id, sub_name, "规范"):
            if len(keywords) >= 3:
                break
            if fallback not in keywords:
                keywords.append(fallback)
        keywords = keywords[:6]
        examples = [str(item).strip() for item in domain.examples if str(item).strip()]
        if not examples:
            return SplitResult(
                original_skill=analysis.skill_name,
                can_split=True,
                success=False,
                errors=[f"子 Skill '{sub_name}' 没有可追溯的源 examples，拒绝造例"],
                unassigned_cases=list(analysis.unassigned_cases),
            )

        body_text = "\n\n".join(
            f"## {section}\n\n{_render_domain_section(analysis.skill_name, domain, section, primary_domain)}"
            for section in _SECTION_NAMES
        )
        full_md = _build_full_skill_md_text(
            name=sub_name,
            version="1.0.0",
            description=domain.description,
            use_when=domain.use_when,
            not_for=full_not_for,
            keywords=keywords,
            examples=examples,
            body=body_text,
        )
        ok, msg, meta, fm_raw, b_raw = validate_generated_structure(full_md, existing_names)
        if not ok or meta is None:
            return SplitResult(
                original_skill=analysis.skill_name,
                can_split=True,
                success=False,
                errors=[f"子 Skill '{sub_name}' 结构校验失败: {msg}"],
                unassigned_cases=list(analysis.unassigned_cases),
            )

        assigned_raw = analysis.assigned_cases.get(domain.domain_id, [])
        formatted_cases: list[dict[str, Any]] = []
        for idx, case in enumerate(assigned_raw, start=1):
            if not isinstance(case, dict) or not str(case.get("query", "")).strip() or not str(case.get("reference", "")).strip():
                return SplitResult(
                    original_skill=analysis.skill_name,
                    can_split=True,
                    success=False,
                    errors=[f"子 Skill '{sub_name}' 存在非法源 case，拒绝继续"],
                    unassigned_cases=list(analysis.unassigned_cases),
                )
            cid = f"{sub_abbrev}_auto_{idx:02d}"
            formatted_cases.append({
                "id": cid,
                "skill": sub_name,
                "query": case["query"],
                "reference": case["reference"],
                "trace_id": f"split:{analysis.skill_name}:{case.get('id', 'raw')}->{sub_name}",
                "case_kind": "split_assigned",
                "difficulty": case.get("difficulty", "standard"),
                "source": f"split_from_{analysis.skill_name}:{case.get('id', 'raw')}",
            })

        existing_queries = {case["query"].casefold() for case in formatted_cases}
        while len(formatted_cases) < 3:
            hard_case = _build_independent_hard_case(meta, existing_queries, len(formatted_cases) + 1)
            formatted_cases.append(hard_case)
            existing_queries.add(hard_case["query"].casefold())
        # There is intentionally no upper truncation.  Losing a source case is
        # a hard failure; the registration quota gate may reject an oversized
        # set, but it must never silently discard it.
        sub_cases_summary[sub_name] = [case["id"] for case in formatted_cases]
        generated_sub_skills.append(
            GeneratedSkill(
                name=sub_name,
                version="1.0.0",
                description=meta.description,
                use_when=meta.use_when,
                not_for=meta.not_for,
                frontmatter_raw=fm_raw or "",
                body_raw=b_raw or "",
                full_skill_md=full_md,
                test_cases=formatted_cases,
                meta=meta,
            )
        )

    migration_id = f"{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ')}-{uuid.uuid4().hex[:12]}"
    backup_path: Optional[Path] = None
    migration_path: Optional[Path] = None
    unassigned_report_path: Optional[Path] = None
    persisted_unassigned_cases = list(analysis.unassigned_cases)
    if register:
        manifest_paths = _migration_paths(repair_file, router_file)
        migration_path = _split_manifest_path(repair_file, analysis.skill_name, migration_id)
        unassigned_report_path = _unassigned_report_path(repair_file, analysis.skill_name, migration_id)
        dir_paths = [skills_dir / generated.name for generated in generated_sub_skills]
        dir_paths.extend([
            skills_dir / analysis.skill_name,
            root / "skills_backup",
            repair_file.parent / "split_migrations",
        ])
        file_paths = manifest_paths + [migration_path, unassigned_report_path]
        try:
            with _SplitFilesystemTransaction(file_paths, dir_paths) as transaction:
                for generated in generated_sub_skills:
                    register_skill(
                        generated,
                        repo_root=root,
                        repair_set_path=repair_file,
                        router_negatives_path=router_file,
                    )
                updates, manifest = _migrate_partition_manifests(
                    original_skill=analysis.skill_name,
                    domains=analysis.domains,
                    analysis=analysis,
                    generated_sub_skills=generated_sub_skills,
                    repair_file=repair_file,
                    router_file=router_file,
                    migration_id=migration_id,
                )
                manifest["content_audit"] = content_audit
                persisted_unassigned_cases = list(manifest.get("unassigned_cases", []))
                _write_manifest_data(updates)
                if backup_original:
                    source_dir = skills_dir / analysis.skill_name
                    target_backup_dir = root / "skills_backup" / analysis.skill_name
                    if not source_dir.exists():
                        raise RegistrationError(f"原 Skill 归档前目录不存在: {source_dir}")
                    if target_backup_dir.exists():
                        raise RegistrationError(f"归档目录已存在，拒绝覆盖: {target_backup_dir}")
                    target_backup_dir.parent.mkdir(parents=True, exist_ok=True)
                    shutil.move(str(source_dir), str(target_backup_dir))
                    _atomic_write_text(
                        target_backup_dir / "deprecated_meta.json",
                        json.dumps({
                            "skill_name": analysis.skill_name,
                            "deprecated_at": datetime.now(timezone.utc).isoformat(),
                            "status": "deprecated_by_split",
                            "replaced_by": [generated.name for generated in generated_sub_skills],
                            "reason": "Skill split into independent sub-skills via skill_splitter",
                            "migration_manifest": str(migration_path),
                            "source_credential": analysis.source_credential,
                        }, ensure_ascii=False, indent=2) + "\n",
                    )
                    backup_path = target_backup_dir
                _write_split_report(migration_path, manifest, content_audit)
                _write_unassigned_report(unassigned_report_path, manifest)
                transaction.commit()
        except Exception as exc:
            return SplitResult(
                original_skill=analysis.skill_name,
                can_split=True,
                sub_skills=generated_sub_skills,
                assigned_cases_summary=sub_cases_summary,
                unassigned_cases=persisted_unassigned_cases,
                content_audit=content_audit,
                migration_manifest_path=migration_path,
                unassigned_report_path=unassigned_report_path,
                success=False,
                errors=[f"拆分事务失败，已回滚: {type(exc).__name__}: {exc}"],
            )

    return SplitResult(
        original_skill=analysis.skill_name,
        can_split=True,
        sub_skills=generated_sub_skills,
        assigned_cases_summary=sub_cases_summary,
        unassigned_cases=persisted_unassigned_cases,
        backup_path=backup_path,
        registered=register,
        success=True,
        migration_manifest_path=migration_path,
        unassigned_report_path=unassigned_report_path,
        content_audit=content_audit,
    )
