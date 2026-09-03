"""SkillForge · Agent Skill 自进化元 Agent 系统

顶层暴露 5 大组件 + 数据模型。

参见：
- README.md（设计取舍 + 面试口径）
- ARCHITECTURE.md（组件划分 + 接口签名 + 数据流）
"""
__version__ = "0.1.0"

from .models import (
    SkillMeta,
    Trigger,
    Evaluation,
    RouteResult,
    EvalResult,
    RatchetVerdict,
    Patch,
    Release,
)
from .registry import SkillRegistry
from .router import IntentRouter
from .evaluator import SkillEvaluator
from .evolver import SkillEvolver
from .state_machine import ReleaseStateMachine

__all__ = [
    "__version__",
    "SkillMeta", "Trigger", "Evaluation",
    "RouteResult", "EvalResult", "RatchetVerdict", "Patch", "Release",
    "SkillRegistry",
    "IntentRouter",
    "SkillEvaluator",
    "SkillEvolver",
    "ReleaseStateMachine",
]
