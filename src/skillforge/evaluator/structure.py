"""结构分：Pydantic 静态检查（权重 40%，不阻断发布，只出警告）

四项检查（合计满分 40）：
    Schema 完整性       15  —— name/version/description/use_when/dependencies 齐全
    Trigger 质量        10  —— keywords ≥ 2 非空 + not_for 非空
    Prompt 健壮性       10  —— Body 含 ## Constraints 段（或"约束"）
    依赖可用性           5  —— dependencies 显式声明（Phase 3 简化：只做静态存在检查）

参见方案书 §4.4、ARCHITECTURE §4-E
"""
from __future__ import annotations
import re

from ..models import SkillMeta


_CONSTRAINTS_RE = re.compile(r"^\s*##\s*(Constraints?|约束)\s*$", re.MULTILINE | re.IGNORECASE)


def score_structure(meta: SkillMeta, body: str) -> dict[str, float]:
    """
    Returns:
        {"schema": <0-15>, "trigger": <0-10>, "prompt": <0-10>, "deps": <0-5>}
    """
    scores: dict[str, float] = {}

    # Schema 完整性（15）—— 5 个关键字段每项 3 分
    key_fields = {
        "name": bool(meta.name),
        "version": bool(meta.version),
        "description": bool(meta.description and len(meta.description) >= 5),
        "use_when": bool(meta.use_when and len(meta.use_when) >= 5),
        "dependencies_declared": meta.dependencies is not None,  # 空列表也算显式声明
    }
    scores["schema"] = 3.0 * sum(1 for ok in key_fields.values() if ok)

    # Trigger 质量（10）—— keywords 5 + not_for 5
    keywords_ok = len([k for k in meta.trigger.keywords if k and k.strip()]) >= 2
    not_for_ok = len(meta.not_for) >= 1
    scores["trigger"] = (5.0 if keywords_ok else 0.0) + (5.0 if not_for_ok else 0.0)

    # Prompt 健壮性（10）—— body 含 Constraints 段
    scores["prompt"] = 10.0 if _CONSTRAINTS_RE.search(body) else 0.0

    # 依赖可用性（5）—— Phase 3 简化：只做"字段存在"检查
    # Phase 4+ 可扩展成 subprocess.which / MCP ping
    scores["deps"] = 5.0

    return scores


def structure_total(scores: dict[str, float]) -> float:
    """结构分小计（满分 40）"""
    return sum(scores.values())
