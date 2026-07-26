"""棘轮门槛：硬门槛 5 条自动阻断 + 软门槛 ≥10% 触发 REVIEW

硬门槛（任一触发 → DECLINED）：
    1. 总分退步
    2. 效果分退步
    3. 任务完成度下降 ≥ 5%
    4. 鲁棒性下降 ≥ 5%
    5. 任一 P0 用例由通过变失败

软门槛（任一维度变化 ≥ 10%，含上升 → REVIEW）：
    含上升是防 Judge 被表面漂亮的输出骗过。
"""
from __future__ import annotations
from typing import Optional

from ..models import EvalResult, RatchetVerdict


HARD_TASK_DROP = 0.05
HARD_ROBUST_DROP = 0.05
SOFT_ANY_CHANGE = 0.10


def check_ratchet(old: Optional[EvalResult], new: EvalResult) -> RatchetVerdict:
    """棘轮判定。首次评估（old=None）默认 PASS。"""
    raise NotImplementedError("Phase 3 implements")
