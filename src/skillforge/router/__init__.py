"""三层级联路由（规则 → embedding → LLM 兜底）"""
from .cascade import IntentRouter

__all__ = ["IntentRouter"]
