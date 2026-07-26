"""客观指标：轮数 / token / 端到端延迟

**不用 LLM 打分**——效率维度靠客观计量避免 Judge 主观偏见。

Phase 3 简化：SimpleAgent 单轮响应，所以 turns=1。
Phase 4+ 若接入 ReActAgent 或多轮对话，从消息序列里计。
"""
from __future__ import annotations
from typing import Any


def collect_objective_metrics(run_log: dict[str, Any]) -> dict[str, float]:
    """
    从单次运行日志采集三项客观指标。

    Args:
        run_log: {
            "messages": list  # [{"role": ..., "content": ...}, ...]
            "usage":    dict  # LLMResponse.usage：{"prompt_tokens": .., "completion_tokens": ..}
            "latency_ms": float
        }

    Returns:
        {"turns": <int>, "tokens": <int>, "latency_ms": <float>}
    """
    messages = run_log.get("messages") or []
    # turns = assistant 消息数（=ReAct 里 Agent 决策/回复次数；单轮为 1）
    turns = sum(1 for m in messages if m.get("role") == "assistant")
    if turns == 0:
        turns = 1  # 兜底：至少算 1 轮

    usage = run_log.get("usage") or {}
    tokens = float(
        usage.get("total_tokens")
        or (usage.get("prompt_tokens", 0) + usage.get("completion_tokens", 0))
        or 0
    )

    latency_ms = float(run_log.get("latency_ms", 0.0))

    return {
        "turns": float(turns),
        "tokens": tokens,
        "latency_ms": latency_ms,
    }
