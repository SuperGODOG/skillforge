"""客观指标：轮数 / token / 端到端延迟（不用 LLM 打分）"""
from __future__ import annotations


def collect_objective_metrics(run_log: dict) -> dict[str, float]:
    """
    从单次运行日志采集客观指标。

    Returns:
        {"turns": <int>, "tokens": <int>, "latency_ms": <float>}
    """
    raise NotImplementedError("Phase 3 implements")
