"""Construct isolated execution and Judge clients from separate config points."""
from __future__ import annotations

import os


def build_execution_llm():
    from hello_agents import HelloAgentsLLM

    return HelloAgentsLLM(
        api_key=os.environ["LLM_API_KEY"],
        model=os.environ["LLM_MODEL_ID"],
        base_url=os.environ["LLM_BASE_URL"],
        temperature=float(os.environ.get("LLM_TEMPERATURE", "0")),
    )


def build_judge_llm():
    from hello_agents import HelloAgentsLLM

    return HelloAgentsLLM(
        api_key=os.environ["JUDGE_LLM_API_KEY"],
        model=os.environ["JUDGE_LLM_MODEL_ID"],
        base_url=os.environ["JUDGE_LLM_BASE_URL"],
        temperature=float(os.environ.get("JUDGE_LLM_TEMPERATURE", "0")),
    )


def build_llm_pair():
    """Return distinct, deterministic execution and Judge client instances."""
    return build_execution_llm(), build_judge_llm()
