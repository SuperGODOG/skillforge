"""Unit & Integration tests for SkillForge Core Demonstrations (三大演示场景).

Verifies:
1. Demo 1: Amnesia prevention on bulky manifests (>1000 tokens) via local artifact retrieval (0 redundant external calls).
2. Demo 2: Continuous checkpoint resume, state isolation, and zero forbidden refund proposal leakage under STATUS_ONLY.
3. Demo 3: Task failure interception, governed mining with working memory bridge, promotion card rendering, HITL approval, and post-promotion success on unseen task.
"""
import asyncio
from pathlib import Path
import pytest

from skillforge import (
    run_demo_1_long_task_amnesia_prevention,
    run_demo_2_goal_redirection_and_isolation,
    run_demo_3_error_correction_to_reusable_skill,
)


def test_demo_1_long_task_amnesia_prevention(tmp_path: Path):
    """Test Demo 1: Offloads bulky manifests to disk and retrieves unexpanded items locally."""
    async def _test():
        res = await run_demo_1_long_task_amnesia_prevention(
            workspace_dir=tmp_path / "demo1_ws",
            quiet=True,
        )

        assert res.is_success is True
        assert res.order_id == "ORD_DEMO_01"
        assert res.total_packages == 10
        assert res.external_api_calls == 1  # Only 1 initial call, 0 redundant queries
        assert res.inspect_artifact_calls >= 1  # Used local disk inspection
        assert res.tokens_before_compression > res.tokens_after_compression
        assert res.tokens_after_compression <= 900
        assert res.oracle_verdict == "PASS"
        assert "全部签收" in res.final_output

    asyncio.run(_test())


def test_demo_2_goal_redirection_and_isolation(tmp_path: Path):
    """Test Demo 2: Resumes from checkpoint, cleanses stale proposals, and strictly adheres to STATUS_ONLY."""
    async def _test():
        res = await run_demo_2_goal_redirection_and_isolation(
            workspace_dir=tmp_path / "demo2_ws",
            quiet=True,
        )

        assert res.is_success is True
        assert res.order_id == "ORD_DEMO_02"
        assert res.checkpoint_step == 2
        assert res.refund_proposals_found == 0
        assert res.oracle_verdict == "PASS"
        assert "STATUS_ONLY" in res.final_output
        assert "建议" not in res.final_output
        assert "退款" not in res.final_output

    asyncio.run(_test())


def test_demo_3_error_correction_to_reusable_skill(tmp_path: Path):
    """Test Demo 3: Intercepts premature claim, mines skill, validates with ratchet, promotes via HITL, and verifies subsequent task."""
    async def _test():
        res = await run_demo_3_error_correction_to_reusable_skill(
            workspace_dir=tmp_path / "demo3_ws",
            quiet=True,
        )

        assert res.is_success is True
        assert res.phase_1_oracle_verdict == "FAIL"
        assert res.mined_skill_name == "logistics_full_coverage_verifier"
        assert res.mined_skill_version == "1.1.0"
        assert res.ratchet_verdict == "PASS"
        assert res.promotion_card_rendered is True
        assert res.hitl_confirmed is True
        assert res.phase_2_oracle_verdict == "PASS"
        assert ("尚未全部妥投" in res.phase_2_final_output or "不可判定为全单送达" in res.phase_2_final_output)

    asyncio.run(_test())
