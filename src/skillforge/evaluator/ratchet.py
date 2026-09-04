"""棘轮门槛：硬门槛 5 条自动阻断 + 软门槛 ≥ 10% 触发 REVIEW

硬门槛（任一触发 → DECLINED）：
    1. 总分退步
    2. 效果分退步
    3. 任务完成度下降 ≥ 5%
    4. 鲁棒性下降 ≥ 5%
    5. 任一 P0 用例由通过变失败

软门槛（任一维度变化 ≥ 10%，含上升 → REVIEW）：
    上升也要看一眼，防 Judge 被表面漂亮的输出骗过

首次评估（old=None） → PASS（无对比基线）

参见 ARCHITECTURE §4-E
"""
from __future__ import annotations
from typing import Optional

from ..models import EvalResult, RatchetVerdict


HARD_TASK_DROP = 0.05    # 任务完成度下降门槛
HARD_ROBUST_DROP = 0.05  # 鲁棒性下降门槛
SOFT_ANY_CHANGE = 0.10   # 软门槛：任一维度变化


def _total(result: EvalResult) -> float:
    """合计得分：结构分（40）+ 效果分（60）= 满分 100"""
    return sum(result.structure_score.values()) + sum(result.effect_score.values())


def _effect_total(result: EvalResult) -> float:
    return sum(result.effect_score.values())


def _rel_change(old_v: float, new_v: float) -> float:
    """相对变化率（new - old）/ max(old, 1e-9)。正为改进，负为退步"""
    if old_v == 0:
        return 1.0 if new_v > 0 else 0.0
    return (new_v - old_v) / abs(old_v)


def check_ratchet(old: Optional[EvalResult], new: EvalResult) -> RatchetVerdict:
    """棘轮判定，返回 DECLINED / REVIEW / PASS"""
    if old is not None and not old.valid:
        reasons = ["历史基线评估无效，不能用于棘轮比较"]
        reasons.extend(old.invalid_reasons)
        return RatchetVerdict(decision="DECLINED", reasons=reasons)
    if not new.valid:
        reasons = ["评估无效，按 fail-closed 拒绝"]
        reasons.extend(new.invalid_reasons)
        return RatchetVerdict(decision="DECLINED", reasons=reasons)
    # 首次评估：无基线，默认放行
    if old is None:
        return RatchetVerdict(decision="PASS", reasons=["首次评估，无历史基线"])

    reasons_hard: list[str] = []

    # 硬门槛 1：总分退步
    if _total(new) < _total(old):
        reasons_hard.append(f"总分退步：{_total(old):.2f} → {_total(new):.2f}")

    # 硬门槛 2：效果分退步
    if _effect_total(new) < _effect_total(old):
        reasons_hard.append(f"效果分退步：{_effect_total(old):.2f} → {_effect_total(new):.2f}")

    # 硬门槛 3：任务完成度下降 ≥ 5%
    task_old = old.effect_score.get("task", 0.0)
    task_new = new.effect_score.get("task", 0.0)
    task_drop = _rel_change(task_old, task_new)
    if task_drop <= -HARD_TASK_DROP:
        reasons_hard.append(f"任务完成度下降 {task_drop:.1%}（≥ {HARD_TASK_DROP:.0%}）")

    # 硬门槛 4：鲁棒性下降 ≥ 5%
    robust_old = old.effect_score.get("robust", 0.0)
    robust_new = new.effect_score.get("robust", 0.0)
    robust_drop = _rel_change(robust_old, robust_new)
    if robust_drop <= -HARD_ROBUST_DROP:
        reasons_hard.append(f"鲁棒性下降 {robust_drop:.1%}（≥ {HARD_ROBUST_DROP:.0%}）")

    # 硬门槛 5：P0 由通过变失败
    if old.p0_pass and not new.p0_pass:
        reasons_hard.append("P0 用例由通过变失败")

    if reasons_hard:
        return RatchetVerdict(decision="DECLINED", reasons=reasons_hard)

    # 软门槛：任一维度变化 ≥ 10%
    reasons_soft: list[str] = []
    all_dims = {
        **{f"struct.{k}": v for k, v in old.structure_score.items()},
        **{f"effect.{k}": v for k, v in old.effect_score.items()},
    }
    all_dims_new = {
        **{f"struct.{k}": v for k, v in new.structure_score.items()},
        **{f"effect.{k}": v for k, v in new.effect_score.items()},
    }
    for dim, ov in all_dims.items():
        nv = all_dims_new.get(dim, 0.0)
        change = _rel_change(ov, nv)
        if abs(change) >= SOFT_ANY_CHANGE:
            direction = "上升" if change > 0 else "下降"
            reasons_soft.append(f"{dim} {direction} {abs(change):.1%}（≥ {SOFT_ANY_CHANGE:.0%}）")

    if reasons_soft:
        return RatchetVerdict(decision="REVIEW", reasons=reasons_soft)

    return RatchetVerdict(decision="PASS", reasons=["全部维度变化 < 10%，无门槛触发"])
