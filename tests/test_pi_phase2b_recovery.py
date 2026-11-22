"""Real Pi subprocess resume through AgentRuntime; provider only is scripted."""
from __future__ import annotations
import os
import time
from pathlib import Path
import pytest
from skillforge import pi_adapter
from skillforge.collector import ExperienceCollector
from skillforge.context_engine.compressor import ContextCompressor
from skillforge.episode import EpisodeStore
from skillforge.models import CandidateSkill, SkillMeta
from skillforge.registry import SkillRegistry
from skillforge.runtime import AgentRuntime, ToolBroker
from skillforge.scenarios.logistics import (
    QueryOrderPackagesTool, QueryPackageTrackingTool, verify_logistics_fulfillment,
)

ORDER = "ORD_2026_0901"
BODY = "FROZEN_LOGISTICS_TASK_RULE_V1_DO_NOT_ALTER"
FINAL = ("Order ORD_2026_0901 has 2 packages: PKG_101 is DELIVERED via "
         "Carrier_Alpha, and PKG_102 is IN_TRANSIT via Carrier_Beta.")

def tc(cid, name, args):
    return {"role": "assistant", "content": [
        {"type": "toolCall", "id": cid, "name": name, "arguments": args}
    ], "stopReason": "toolUse"}

def cfg(responses):
    return {"provider": "faux", "model_id": "faux-unit-test-model", "responses": responses}

def remaining():
    return [
        tc("recover_track_101", "query_package_tracking", {"package_id": "PKG_101"}),
        tc("recover_track_102", "query_package_tracking", {"package_id": "PKG_102"}),
        {"role": "assistant", "content": [{"type": "text", "text": FINAL}], "stopReason": "stop"},
    ]

def assert_reaped(pid):
    with pytest.raises(ProcessLookupError):
        os.kill(pid, 0)

@pytest.fixture
def paused_factory(tmp_path, monkeypatch):
    pids, runtimes = [], []
    original_start = pi_adapter.PiRunnerClient.start
    async def observed_start(client):
        await original_start(client)
        pids.append(client.pid)
    monkeypatch.setattr(pi_adapter.PiRunnerClient, "start", observed_start)
    def make(budget=10, candidate_store=None, task_context=None):
        counts = {"order": 0, "track": 0}
        holder = {"pause": True}
        run_id = "run_phase2b_resume"
        deadline = time.time() + 300
        skills = tmp_path / "skills"
        skills.mkdir(exist_ok=True)
        def build(cand_store=None):
            episodes = EpisodeStore(db_path=tmp_path / "episodes.db")
            registry = SkillRegistry(db_path=tmp_path / "skills.db", skills_dir=skills)
            broker = ToolBroker(application_allowlist={"query_order_packages", "query_package_tracking"})
            collector = ExperienceCollector(episode_store=episodes, registry=registry)
            runtime = AgentRuntime(db_path=tmp_path / "runtime.db", tool_broker=broker,
                                   registry=registry, collector=collector, episode_store=episodes,
                                   candidate_store=cand_store)
            class OrderTool(QueryOrderPackagesTool):
                def run(self, *args, **kwargs):
                    counts["order"] += 1
                    result = super().run(*args, **kwargs)
                    if holder["pause"]:
                        holder["pause"] = False
                        runtime.request_pause_run(run_id)
                    return result
            class TrackingTool(QueryPackageTrackingTool):
                def run(self, *args, **kwargs):
                    counts["track"] += 1
                    return super().run(*args, **kwargs)
            broker.register_tool(OrderTool())
            broker.register_tool(TrackingTool())
            runtimes.append(runtime)
            return runtime, collector, episodes
        compressor = ContextCompressor(
            trigger_watermark_tokens=20, target_watermark_tokens=10,
            hard_capacity_tokens=5000, offload_threshold_chars=50,
            artifacts_dir=tmp_path / "artifacts",
        )
        candidate = CandidateSkill(
            candidate_id="cand_recovery", skill_name="package_tracker", decision="create",
            source_requirement="Query both packages", body=BODY,
            meta=SkillMeta(name="package_tracker", version="0.1.0",
                          description="Package tracking", use_when="Logistics",
                          dependencies=["query_order_packages", "query_package_tracking"]),
        )
        if candidate_store and task_context:
            candidate_store.save_task_context(task_context)
        old, collector, episodes = build(cand_store=candidate_store)
        old.start_run(run_id=run_id, task_id="recovery_task", candidate=candidate,
                      purpose="evaluation", budget_max=budget, deadline_ts=deadline)
        old.run_agent_with_pi(
            run_id=run_id, input_text="Check all packages for " + ORDER,
            tools=["query_order_packages", "query_package_tracking"],
            model_config=cfg([tc("old_order_call", "query_order_packages", {"order_id": ORDER})]),
            test_mode=True, context_compressor=compressor, workspace_dir=tmp_path, timeout=15,
        )
        checkpoint = old.get_pi_checkpoint(run_id)
        assert old.get_run(run_id).status == "PAUSED"
        assert checkpoint.budget_consumed == 2
        assert checkpoint.epoch > 1
        assert checkpoint.deadline_ts == deadline
        assert counts == {"order": 1, "track": 0}
        assert not collector._runs[run_id].completed
        assert episodes.get_episode("ep_" + run_id) is None
        assert_reaped(pids[0])
        old_signature = collector._runs[run_id].provenances[0].signature
        old.close()
        candidate.body = "NEW_BODY_MUST_NOT_LEAK"
        new, new_collector, new_episodes = build(cand_store=candidate_store)
        return dict(runtime=new, collector=new_collector, episodes=new_episodes,
                    run_id=run_id, deadline=deadline, checkpoint=checkpoint,
                    counts=counts, pids=pids, compressor=compressor,
                    old_signature=old_signature, candidate_store=candidate_store,
                    task_context=task_context, candidate=candidate)
    yield make
    for runtime in runtimes:
        runtime.close()

def test_resume_new_runtime_and_pi_keeps_snapshot_budget_and_episode(paused_factory, tmp_path):
    e = paused_factory()
    rt, rid = e["runtime"], e["run_id"]
    answer = rt.resume_agent_with_pi(
        run_id=rid, model_config=cfg(remaining()), test_mode=True,
        context_compressor=e["compressor"], workspace_dir=tmp_path, timeout=20,
    )
    assert answer == FINAL
    assert len(e["pids"]) == 2 and e["pids"][0] != e["pids"][1]
    assert_reaped(e["pids"][1])
    assert e["counts"] == {"order": 1, "track": 2}
    rec = rt.get_run(rid)
    assert rec.budget_consumed == 7 and rec.budget_max == 10
    assert rec.deadline_ts == e["deadline"]
    state = rt.get_harness_state(rid)
    assert state.context_epoch >= e["checkpoint"].epoch
    provider_input = rt.get_last_pi_result(rid).captured_provider_inputs[0]
    rendered = str(provider_input)
    assert BODY in rendered and "NEW_BODY_MUST_NOT_LEAK" not in rendered
    assert "old_order_call" in rendered and "ATTACHMENTS_INDEX" in rendered
    assert sum(m.get("role") == "user" for m in provider_input["messages"]) == 1
    provs = e["collector"]._runs[rid].provenances
    assert len(provs) == 3 and provs[0].signature == e["old_signature"]
    oracle = verify_logistics_fulfillment(answer, ORDER, rt.list_tool_calls(rid))
    assert oracle["independent_pass"] is True, oracle
    _, episode = rt.finalize_run(rid, model_output=answer, verification_evidence=oracle)
    assert episode.outcome == "success" and len(episode.provenances) == 3
    _, again = rt.finalize_run(rid, model_output=answer, verification_evidence=oracle)
    assert again.episode_id == episode.episode_id
    count = e["episodes"]._get_conn().execute(
        "SELECT COUNT(*) FROM episodes WHERE episode_id = ?", ("ep_" + rid,)
    ).fetchone()[0]
    assert count == 1

def test_resume_does_not_reset_remaining_budget(paused_factory, tmp_path):
    e = paused_factory(budget=3)
    rt, rid = e["runtime"], e["run_id"]
    try:
        rt.resume_agent_with_pi(run_id=rid, model_config=cfg(remaining()),
                                test_mode=True, workspace_dir=tmp_path, timeout=15)
    except Exception as exc:
        assert "budget" in str(exc).lower(), str(exc)
    rec = rt.get_run(rid)
    assert rec.status == "BUDGET_EXHAUSTED"
    assert rec.budget_consumed == 3 and rec.budget_max == 3
    assert rec.deadline_ts == e["deadline"]
    assert e["counts"] == {"order": 1, "track": 0}
    assert_reaped(e["pids"][-1])
    ep = e["episodes"].get_episode("ep_" + rid)
    assert ep is None or ep.outcome != "success"

def test_resume_preserves_expired_original_deadline(paused_factory, tmp_path, monkeypatch):
    e = paused_factory()
    rt, rid = e["runtime"], e["run_id"]
    import skillforge.runtime as runtime_module
    monkeypatch.setattr(runtime_module.time, "time", lambda: e["deadline"] + 1)
    with pytest.raises(TimeoutError, match="[Dd]eadline"):
        rt.resume_agent_with_pi(run_id=rid, model_config=cfg(remaining()),
                                test_mode=True, workspace_dir=tmp_path, timeout=15)
    rec = rt.get_run(rid)
    assert rec.status == "TIMED_OUT" and rec.deadline_ts == e["deadline"]
    assert rec.budget_consumed == 2
    assert len(e["pids"]) == 1 and e["counts"] == {"order": 1, "track": 0}
