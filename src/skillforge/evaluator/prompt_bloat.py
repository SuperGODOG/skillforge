"""Prompt Bloat 护栏（P1-E / A4 / Token 1000-AND Policy）

门控规则（ARCHITECTURE §4-E / 1000-Token 准则定稿）：
1. 标准 Tokenizer 口径：
   - 采用固定可复现的 Policy Tokenizer（默认 tiktoken:cl100k_base:版本）。
   - 明确为规范 Policy Tokenizer，不冒充 GLM 原生精确 token，更不混同 provider 请求的 HTTP usage。
   - 严禁字符/4或英文词数估算；真实计数不可用或未知 tokenizer 时 fail-closed 阻断进入 REVIEW。
2. 双重条件严格 AND 判定（防小增量误报与大膨胀漏网）：
   - 单段软门槛：任一 changed_section 相对 baseline 增长 > 25% AND 绝对净增 > 1000 tokens。
   - 全 Body 倍数门：整 Body token 数超过 baseline 的设定倍数（默认 > 1.20x 即相对增长 > 20%）AND 绝对净增 > 1000 tokens。
   - 两个条件必须同时满足才触发 REVIEW；仅相对超或仅绝对超（或刚好等于 1000 tokens）均不触发长度门阻断。
3. 文本剥离与结构审计：
   - 规范剥离 Frontmatter 元数据，前言实质正文计入 Preamble 与 Total。
   - 新增章节若无基线对应，纳入全文 token 统计；防拆段分摊绕过总门禁。
4. 冷启动与空基线：
   - 初始无基线 Draft 仍保留独立可配置的 3000 字符冷启动绝对上限（max_body_chars 或 3000）。
   - 非冷启动但空基线时，以净增 > 1000 tokens 作为替代绝对防分摊门。
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Callable, Literal, Optional

import tiktoken

from ..diff import split_markdown_sections
from ..models import EvolveBudget, RatchetVerdict

STANDARD_SECTIONS = ("Overview", "Instructions", "Examples", "Constraints")
_FRONTMATTER_RE = re.compile(r"^---\s*\n(.*?)\n---\s*\n(.*)$", re.DOTALL)

DEFAULT_POLICY_TOKENIZER = "tiktoken:cl100k_base"
DEFAULT_POLICY_TOKENIZER_VERSION = f"tiktoken:cl100k_base:{getattr(tiktoken, '__version__', 'unknown')}"
DEFAULT_POLICY_VERSION = "v2_token_1000_and"


def strip_frontmatter_if_present(text: str) -> str:
    cleaned = (text or "").strip()
    m = _FRONTMATTER_RE.match(cleaned)
    if m:
        return m.group(2).strip()
    return cleaned


def canonical_section_name(name: str) -> str:
    """归一化段落名称，匹配四段式 Instructions / Constraints / Examples / Overview"""
    raw = re.sub(r"#\d+$", "", name).strip()
    raw_lower = raw.casefold()
    if "instruction" in raw_lower or "说明" in raw or "指令" in raw or "步骤" in raw:
        return "Instructions"
    if "constraint" in raw_lower or "约束" in raw or "限制" in raw or "边界" in raw:
        return "Constraints"
    if "example" in raw_lower or "示例" in raw or "例子" in raw:
        return "Examples"
    if "overview" in raw_lower or "概述" in raw:
        return "Overview"
    return raw


def count_tokens(
    text: str,
    tokenizer_name: str = DEFAULT_POLICY_TOKENIZER,
    tokenizer_callable: Optional[Callable[[str], int]] = None,
) -> tuple[Optional[int], str, Optional[str]]:
    """使用已安装的标准 Policy Tokenizer 统计 token 数量。

    返回: (count, tokenizer_id_with_version, error_or_unknown_reason)
    严格要求：不使用字符/4或简单wordcount伪造token，未知/不支持的tokenizer返回error由上层fail-closed。
    """
    if tokenizer_callable is not None:
        try:
            val = int(tokenizer_callable(text or ""))
            return val, "custom_callable", None
        except Exception as e:
            return None, "custom_callable", f"TOKENIZER_CALLABLE_ERROR: {e}"

    target_name = (tokenizer_name or DEFAULT_POLICY_TOKENIZER).strip()
    if target_name in (DEFAULT_POLICY_TOKENIZER, "cl100k_base", DEFAULT_POLICY_TOKENIZER_VERSION):
        try:
            enc = tiktoken.get_encoding("cl100k_base")
            return len(enc.encode(text or "")), DEFAULT_POLICY_TOKENIZER_VERSION, None
        except Exception as e:
            return None, DEFAULT_POLICY_TOKENIZER_VERSION, f"TIKTOKEN_ENCODING_ERROR: {e}"

    # 支持其他已知 tiktoken 编码（如 o200k_base 等）
    try:
        enc = tiktoken.get_encoding(target_name)
        ver = f"tiktoken:{target_name}:{getattr(tiktoken, '__version__', 'unknown')}"
        return len(enc.encode(text or "")), ver, None
    except Exception as e:
        return None, target_name, f"UNKNOWN_TOKENIZER: Cannot load tokenizer '{target_name}' ({e})"


def compute_body_section_stats(body: str) -> dict[str, int]:
    """统计 Body 各段字符数及整体字符数（保留向后兼容接口）。"""
    cleaned_body = strip_frontmatter_if_present(body)
    if not cleaned_body:
        return {
            "Overview": 0,
            "Instructions": 0,
            "Examples": 0,
            "Constraints": 0,
            "Preamble": 0,
            "total": 0,
        }

    sections = split_markdown_sections(cleaned_body)
    stats: dict[str, int] = {
        "Overview": 0,
        "Instructions": 0,
        "Examples": 0,
        "Constraints": 0,
        "Preamble": 0,
        "total": len(cleaned_body),
    }

    for raw_name, content in sections.items():
        if raw_name == "__preamble__":
            preamble_len = len(content.strip())
            stats["__preamble__"] = preamble_len
            stats["Preamble"] = preamble_len
            continue
        if raw_name == "__full_body__":
            stats[raw_name] = len(content.strip())
            continue
        cname = canonical_section_name(raw_name)
        char_count = len(content.strip())
        stats[cname] = stats.get(cname, 0) + char_count
        if cname != raw_name:
            stats[raw_name] = stats.get(raw_name, 0) + char_count

    return stats


def compute_body_section_token_stats(
    body: str,
    tokenizer_name: str = DEFAULT_POLICY_TOKENIZER,
    tokenizer_callable: Optional[Callable[[str], int]] = None,
) -> tuple[dict[str, int], str, Optional[str]]:
    """统计 Body 各段 token 数及整体 token 数（规范 Policy Tokenizer）。

    返回: (token_stats_dict, tokenizer_id_with_version, error_string)
    """
    cleaned_body = strip_frontmatter_if_present(body)
    if not cleaned_body:
        return {
            "Overview": 0,
            "Instructions": 0,
            "Examples": 0,
            "Constraints": 0,
            "Preamble": 0,
            "total": 0,
        }, DEFAULT_POLICY_TOKENIZER_VERSION, None

    total_tokens, tok_id, err = count_tokens(cleaned_body, tokenizer_name, tokenizer_callable)
    if err is not None:
        return {}, tok_id, err

    sections = split_markdown_sections(cleaned_body)
    stats: dict[str, int] = {
        "Overview": 0,
        "Instructions": 0,
        "Examples": 0,
        "Constraints": 0,
        "Preamble": 0,
        "total": total_tokens or 0,
    }

    for raw_name, content in sections.items():
        if raw_name in ("__body_order_or_whitespace__",):
            continue
        c_toks, _, s_err = count_tokens(content.strip(), tokenizer_name, tokenizer_callable)
        if s_err is not None:
            return {}, tok_id, s_err

        if raw_name == "__preamble__":
            stats["__preamble__"] = c_toks or 0
            stats["Preamble"] = c_toks or 0
            continue
        if raw_name == "__full_body__":
            stats[raw_name] = c_toks or 0
            continue

        cname = canonical_section_name(raw_name)
        stats[cname] = stats.get(cname, 0) + (c_toks or 0)
        if cname != raw_name:
            stats[raw_name] = stats.get(raw_name, 0) + (c_toks or 0)

    return stats, tok_id, None


@dataclass
class PromptBloatResult:
    passed: bool
    decision: Literal["PASS", "REVIEW", "DECLINED"]
    reasons: list[str] = field(default_factory=list)
    distillation_prompt: Optional[str] = None
    baseline_stats: dict[str, int] = field(default_factory=dict)
    candidate_stats: dict[str, int] = field(default_factory=dict)
    section_deltas: dict[str, dict[str, Any]] = field(default_factory=dict)
    tokenizer_name: str = DEFAULT_POLICY_TOKENIZER_VERSION
    policy_version: str = DEFAULT_POLICY_VERSION
    baseline_token_stats: dict[str, int] = field(default_factory=dict)
    candidate_token_stats: dict[str, int] = field(default_factory=dict)

    def to_ratchet_verdict(self) -> RatchetVerdict:
        return RatchetVerdict(decision=self.decision, reasons=list(self.reasons))


def check_prompt_bloat(
    old_body: str,
    new_body: str,
    budget: Optional[EvolveBudget] = None,
    changed_sections: Optional[list[str]] = None,
    cold_start: bool = False,
    tokenizer_callable: Optional[Callable[[str], int]] = None,
    tokenizer_name: Optional[str] = None,
) -> PromptBloatResult:
    """执行 Prompt Bloat 门控检查（基于 1000-Token AND 准则）。"""
    budget = budget or EvolveBudget()
    section_growth_ratio = getattr(budget, "section_growth_ratio", 0.25)
    section_growth_tokens = getattr(budget, "section_growth_tokens", 1000)
    max_body_multiplier = getattr(budget, "max_body_multiplier", 1.20)
    max_body_delta_tokens = getattr(budget, "max_body_delta_tokens", 1000)
    max_body_chars = getattr(budget, "max_body_chars", None)
    on_body_bloat = getattr(budget, "on_body_bloat", "REVIEW")
    effective_tokenizer_name = tokenizer_name or getattr(budget, "tokenizer_name", DEFAULT_POLICY_TOKENIZER)

    baseline_char_stats = compute_body_section_stats(old_body)
    candidate_char_stats = compute_body_section_stats(new_body)

    b_tokens, b_tok_id, b_err = compute_body_section_token_stats(old_body, effective_tokenizer_name, tokenizer_callable)
    c_tokens, c_tok_id, c_err = compute_body_section_token_stats(new_body, effective_tokenizer_name, tokenizer_callable)

    tok_err = b_err or c_err
    if tok_err is not None:
        return PromptBloatResult(
            passed=False,
            decision="REVIEW",
            reasons=[f"PROMPT_BLOAT: Tokenizer 未知或计数不可用 ({tok_err})，无法验证 1000-token 长度门，按 fail-closed 阻断进入 REVIEW。"],
            distillation_prompt=f"Tokenizer 计数失败 ({tok_err})，请确认 tokenizer 配置或使用支持的 policy tokenizer。",
            baseline_stats=baseline_char_stats,
            candidate_stats=candidate_char_stats,
            section_deltas={},
            tokenizer_name=effective_tokenizer_name,
            policy_version=DEFAULT_POLICY_VERSION,
        )

    old_cleaned = strip_frontmatter_if_present(old_body)
    new_cleaned = strip_frontmatter_if_present(new_body)

    # 无任何改动直接放行
    if old_cleaned == new_cleaned:
        return PromptBloatResult(
            passed=True,
            decision="PASS",
            reasons=[],
            distillation_prompt=None,
            baseline_stats=baseline_char_stats,
            candidate_stats=candidate_char_stats,
            section_deltas={},
            tokenizer_name=b_tok_id,
            policy_version=DEFAULT_POLICY_VERSION,
            baseline_token_stats=b_tokens,
            candidate_token_stats=c_tokens,
        )

    old_raw_sections = split_markdown_sections(old_cleaned)
    new_raw_sections = split_markdown_sections(new_cleaned)

    detected_changed: set[str] = set()
    if changed_sections is not None and len(changed_sections) > 0:
        for s in changed_sections:
            if s not in ("__body_order_or_whitespace__",):
                cname = "Preamble" if s == "__preamble__" else canonical_section_name(s)
                detected_changed.add(cname)

    all_keys = set(old_raw_sections) | set(new_raw_sections)
    for k in all_keys:
        if k in ("__body_order_or_whitespace__",):
            continue
        if old_raw_sections.get(k, "").strip() != new_raw_sections.get(k, "").strip():
            cname = "Preamble" if k == "__preamble__" else canonical_section_name(k)
            detected_changed.add(cname)

    reasons: list[str] = []
    distillation_prompts: list[str] = []
    section_deltas: dict[str, dict[str, Any]] = {}
    decision: Literal["PASS", "REVIEW", "DECLINED"] = "PASS"

    # 1. 软门槛：任一 changed_section > 25% 增长 AND 绝对净增 > 1000 tokens (非 cold_start 时有效)
    if not cold_start:
        for sec in sorted(detected_changed):
            if sec in ("__body_order_or_whitespace__",):
                continue
            old_t = b_tokens.get(sec, 0)
            new_t = c_tokens.get(sec, 0)
            delta_t = new_t - old_t
            ratio_t = (delta_t / old_t) if old_t > 0 else (1.0 if delta_t > 0 else 0.0)

            old_c = baseline_char_stats.get(sec, 0)
            new_c = candidate_char_stats.get(sec, 0)
            delta_c = new_c - old_c
            ratio_c = (delta_c / old_c) if old_c > 0 else (1.0 if delta_c > 0 else 0.0)

            section_deltas[sec] = {
                "old": old_t,
                "new": new_t,
                "delta": delta_t,
                "ratio": ratio_t,
                "tokens": delta_t,
                "chars": delta_c,
                "old_tokens": old_t,
                "new_tokens": new_t,
                "delta_tokens": delta_t,
                "ratio_tokens": ratio_t,
                "old_chars": old_c,
                "new_chars": new_c,
                "delta_chars": delta_c,
                "ratio_chars": ratio_c,
            }

            # 严格双条件 AND：相对增长 > section_growth_ratio 且 净增 > section_growth_tokens
            if ratio_t > section_growth_ratio and delta_t > section_growth_tokens:
                decision = "REVIEW"
                pct_str = f"{ratio_t * 100:.1f}%"
                reason_msg = (
                    f"PROMPT_BLOAT: 段落 '{sec}' 相对基线增长 {pct_str} (+{delta_t} tokens)，"
                    f"同时超过相对门槛（> {section_growth_ratio * 100:.0f}%）与绝对净增门槛（> {section_growth_tokens} tokens）。"
                    f"distillation 提示: 建议精简收敛 '{sec}' 段，去除冗余描述并保持核心语义。"
                )
                reasons.append(reason_msg)
                distillation_prompts.append(
                    f"段落 '{sec}' 超限：增长 +{delta_t} tokens（{pct_str}，基线 {old_t}→{new_t} tokens），"
                    f"触发软门槛（>{section_growth_ratio * 100:.0f}% 且 >{section_growth_tokens} tokens）；"
                    f"建议精简收敛 '{sec}' 段，去除冗余说明并保持核心语义约束。"
                )

    # 2. 全 Body 倍数门（防塞字绕检）与绝对上限
    old_total_t = b_tokens.get("total", 0)
    new_total_t = c_tokens.get("total", 0)
    total_delta_t = new_total_t - old_total_t
    total_multiplier_t = (new_total_t / old_total_t) if old_total_t > 0 else (1.0 if new_total_t > 0 else 0.0)

    old_total_c = baseline_char_stats.get("total", 0)
    new_total_c = candidate_char_stats.get("total", 0)
    total_delta_c = new_total_c - old_total_c
    total_multiplier_c = (new_total_c / old_total_c) if old_total_c > 0 else (1.0 if new_total_c > 0 else 0.0)

    section_deltas["total"] = {
        "old": old_total_t,
        "new": new_total_t,
        "delta": total_delta_t,
        "multiplier": total_multiplier_t,
        "tokens": total_delta_t,
        "chars": total_delta_c,
        "old_tokens": old_total_t,
        "new_tokens": new_total_t,
        "delta_tokens": total_delta_t,
        "multiplier_tokens": total_multiplier_t,
        "old_chars": old_total_c,
        "new_chars": new_total_c,
        "delta_chars": total_delta_c,
        "multiplier_chars": total_multiplier_c,
    }

    if old_total_t > 0:
        # 严格双条件 AND：整 Body 倍数 > max_body_multiplier 且 净增 > max_body_delta_tokens
        if (new_total_t > old_total_t * max_body_multiplier) and total_delta_t > max_body_delta_tokens:
            if decision != "DECLINED":
                decision = on_body_bloat  # 默认 "REVIEW"
            mult_str = f"{total_multiplier_t:.2f}x"
            limit_str = f"{max_body_multiplier:.2f}x"
            reason_msg = (
                f"PROMPT_BLOAT: 整 Body token 数由 {old_total_t} 增至 {new_total_t}（倍数 {mult_str} > 设定倍数 {limit_str}，净增 {total_delta_t} tokens > 阈值 {max_body_delta_tokens} tokens），"
                f"触发全 Body 膨胀门控（防多段均摊绕检）。"
                f"distillation 提示: 整体 Body 文本膨胀超限，建议跨段落综合精简压缩。"
            )
            reasons.append(reason_msg)
            distillation_prompts.append(
                f"整体 Body 文本膨胀超限：净增 +{total_delta_t} tokens（{mult_str}，基线 {old_total_t}→{new_total_t} tokens），"
                f"触发全 Body 倍数门（>{limit_str} 且净增 >{max_body_delta_tokens} tokens）；"
                "建议跨段落综合精简压缩。"
            )
    elif old_total_t <= 0 and not reasons:
        if cold_start:
            effective_max = max_body_chars or 3000
            if new_total_c > effective_max:
                if decision != "DECLINED":
                    decision = on_body_bloat
                reason_msg = (
                    f"PROMPT_BLOAT: 冷启动新建 Skill 正文长度为 {new_total_c} 字符，"
                    f"超过冷启动绝对上限（> {effective_max} 字符）。"
                    "distillation 提示: 新建 Skill 体量过大，建议收敛正文规模。"
                )
                reasons.append(reason_msg)
                distillation_prompts.append(
                    f"冷启动新建超限：正文 {new_total_c} 字符，超过冷启动上限 {effective_max} 字符；建议精简收敛。"
                )
        elif new_total_t > max_body_delta_tokens:
            if decision != "DECLINED":
                decision = on_body_bloat
            reason_msg = (
                f"PROMPT_BLOAT: 基线 Body 为空，候选 Body 为 {new_total_t} tokens，"
                f"超过替代绝对门（> {max_body_delta_tokens} tokens），触发空基线防分摊门控。"
                "distillation 提示: 基线缺失时仍需控制整体 Body 体量。"
            )
            reasons.append(reason_msg)
            distillation_prompts.append(
                f"整 Body 基线为空，增长 +{new_total_t} tokens（无可用倍数），"
                f"触发替代绝对门（>{max_body_delta_tokens} tokens）；建议综合精简压缩。"
            )

    # 绝对上限检查（字符数硬件上限，若配置）
    if max_body_chars is not None and new_total_c > max_body_chars and not any("超过设定绝对上限" in r for r in reasons):
        if decision != "DECLINED":
            decision = on_body_bloat
        reason_msg = (
            f"PROMPT_BLOAT: 整 Body 字符数 {new_total_c} 超过设定绝对上限 {max_body_chars} 字符。"
            f"distillation 提示: 整体 Body 文本超过绝对上限，建议综合精简压缩。"
        )
        reasons.append(reason_msg)
        distillation_prompts.append(
            f"整 Body 超限：当前 {new_total_c} 字符（上限 {max_body_chars} 字符），"
            f"触发 max_body_chars 绝对门；建议综合精简压缩。"
        )

    passed = len(reasons) == 0
    final_distillation = "; ".join(distillation_prompts) if distillation_prompts else None

    return PromptBloatResult(
        passed=passed,
        decision=decision,
        reasons=reasons,
        distillation_prompt=final_distillation,
        baseline_stats=baseline_char_stats,
        candidate_stats=candidate_char_stats,
        section_deltas=section_deltas,
        tokenizer_name=b_tok_id,
        policy_version=DEFAULT_POLICY_VERSION,
        baseline_token_stats=b_tokens,
        candidate_token_stats=c_tokens,
    )
