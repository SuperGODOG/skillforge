"""结构分：Pydantic 静态检查（权重 40%，不阻断发布，只出警告）

四项检查：
    Schema 完整性       15%
    Trigger 质量        10%
    Prompt 健壮性       10%（Constraints 段存在）
    依赖可用性           5%
"""
from __future__ import annotations


def score_structure(meta, body: str) -> dict[str, float]:
    """
    Returns:
        {"schema": <0-15>, "trigger": <0-10>, "prompt": <0-10>, "deps": <0-5>}
    """
    raise NotImplementedError("Phase 3 implements")
