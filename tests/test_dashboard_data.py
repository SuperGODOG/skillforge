"""Evidence projections preserve provenance, unknowns and historical boundaries."""
import hashlib
import json
from pathlib import Path
import sqlite3
import shutil
import subprocess

import pytest

from skillforge.dashboard_data import build_snapshot, redact_sensitive
from skillforge.storage.db import SCHEMA_SQL


def write_json(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data), encoding="utf-8")


def test_empty_project_never_initializes_stores(tmp_path):
    data = build_snapshot(tmp_path)
    assert data["schema_version"] == 1
    assert data["runs"] == data["episodes"] == data["benchmarks"] == []
    assert list(tmp_path.iterdir()) == []


def test_real_schema_lineage_checkpoints_and_readonly_integrity(tmp_path):
    directory = tmp_path / "runs"
    directory.mkdir()
    db = directory / "state.db"
    with sqlite3.connect(db) as c:
        c.executescript(SCHEMA_SQL)
        c.execute("INSERT INTO runtime_runs (run_id,task_id,status,purpose,candidate_id,frozen_body) VALUES ('r1','t1','COMPLETED','evaluation','c1','frozen procedure')")
        c.execute("INSERT INTO runtime_tool_calls (call_id,run_id,tool_name,status,input_params_json,output_text) VALUES ('tc1','r1','query_order','EXECUTED','{}','two packages')")
        c.execute("INSERT INTO episodes (episode_id,task_id,run_id,skill_name,skill_version,environment_json,provenances_json,acceptance_json,verification_json,outcome) VALUES ('e1','t1','r1','logistics','1','{\"purpose\":\"evaluation\",\"apiKey\":\"private-env-value\"}','[]','{}','{\"verdict\":\"PASS\"}','success')")
        c.execute("INSERT INTO candidate_skills (candidate_id,skill_name,decision,source_episode_ids,status,meta_json,body_md,rationale,supersedes) VALUES ('c1','logistics','revise','[\"e1\"]','REVIEW','{\"version\":\"2\"}','check every package','preserve normal cases','c0')")
        c.execute("INSERT INTO releases (release_id,skill_name,version,status) VALUES ('release1','logistics','2','PUBLISHED')")
        c.execute("INSERT INTO validation_records (record_id,candidate_id,content_hash,ratchet_decision,promoted,release_id,config_hash,dataset_version) VALUES ('v1','c1','bound-hash','PASS',1,'release1','config1','dataset1')")
        c.execute("INSERT INTO repair_jobs (job_id,fingerprint,skill_name,baseline_version,source_episode_ids,responsibility_layer,status,attempts_json,max_attempts,current_attempt,diagnosis_json,latest_candidate_id) VALUES ('j1','fp1','logistics','1','[\"e1\"]','skill','COMPLETED','[]',2,1,'{\"reason\":\"coverage\"}','c1')")
        c.execute("INSERT INTO runtime_pi_checkpoints (checkpoint_id,run_id,messages_json,model_config_json,epoch) VALUES ('cp1','r1','[{\"thinking\":\"hidden-chain-text\"}]','{\"apiKey\":\"hidden-config-key\"}',3)")
    before = (hashlib.sha256(db.read_bytes()).hexdigest(), db.stat().st_mtime_ns, set(directory.iterdir()))
    d = build_snapshot(tmp_path)
    r = d["runs"][0]
    assert r["oracle"]["verdict"] == "PASS"
    assert r["frozen_body"] == "frozen procedure"
    assert r["tool_records"][0]["tool_name"] == "query_order"
    assert r["checkpoints"][0]["epoch"] == 3
    assert d["episodes"][0]["run_id"] == r["run_id"]
    assert d["episodes"][0]["scope"] == d["candidates"][0]["scope"] == "runs"
    assert d["candidates"][0]["source_episode_ids"] == ["e1"]
    assert d["candidates"][0]["parent_candidate_id"] == "c0"
    assert d["validations"][0]["config_hash"] == "config1"
    assert d["validations"][0]["release_id"] == d["releases"][0]["release_id"]
    assert d["repair_jobs"][0]["latest_candidate_id"] == "c1"
    assert all(s not in json.dumps(d) for s in ["hidden-chain-text", "hidden-config-key", "private-env-value", "messages_json", "model_config_json"])
    assert (hashlib.sha256(db.read_bytes()).hexdigest(), db.stat().st_mtime_ns, set(directory.iterdir())) == before


def test_old_schema_broken_sources_and_cross_scope_ids(tmp_path):
    for name in ["one", "two"]:
        p = tmp_path / "scratch" / name / "runtime.db"
        p.parent.mkdir(parents=True)
        with sqlite3.connect(p) as c:
            c.execute("CREATE TABLE runtime_runs (run_id TEXT, task_id TEXT, status TEXT)")
            c.execute("INSERT INTO runtime_runs VALUES ('same-id', ?, 'PAUSED')", (name,))
    write_json(tmp_path / "scratch" / "one" / "report.json", {"run_id": "same-id", "text": "answer from one", "status": "PAUSED"})
    (tmp_path / "scratch" / "broken.db").write_text("not a sqlite database")
    (tmp_path / "scratch" / "case_bad.json").write_text("{invalid json")
    d = build_snapshot(tmp_path)
    assert len(d["runs"]) == 2
    assert d["warnings"]
    one = next(r for r in d["runs"] if r["scope"] == "scratch/one")
    two = next(r for r in d["runs"] if r["scope"] == "scratch/two")
    assert one["response_text"] == "answer from one"
    assert two.get("response_text") is None
    assert len(one["source_ids"]) == 2
    assert one["budget_max"] is None


def test_roots_cannot_escape_or_follow_symlinks(tmp_path):
    root = tmp_path / "project"
    root.mkdir()
    external = tmp_path / "external"
    external.mkdir()
    write_json(external / "report.json", {"run_id": "must-not-read"})
    with pytest.raises(ValueError):
        build_snapshot(root, [external])
    (root / "alias").symlink_to(external, target_is_directory=True)
    with pytest.raises(ValueError):
        build_snapshot(root, [root / "alias"])
    (root / "scratch").mkdir()
    (root / "scratch" / "report.json").symlink_to(external / "report.json")
    assert build_snapshot(root)["runs"] == []
    with pytest.raises(ValueError):
        build_snapshot(root, [root / "missing"])


def real_report():
    return {
        "metadata": {"resolved_model": "test-real-model", "evidence_tier": "real_llm", "credential_source": "must-hide-path"},
        "skills": {
            "v1_draft": {"candidate_id": "v1", "body_preview": "name: logistics\ntruncated...", "content_hash": "full-body-hash", "source_type": "requirement"},
            "v2_repaired": {"candidate_id": "v2", "parent_candidate_id": "v1", "repaired_from_dev_failures": 2, "source_type": "episode"},
        },
        "partitions": {
            "DEV": {"sample_size": 3, "summary": {"A_passed": "1/3"}, "tasks": [
                {"task_id": "dev1", "task_family": "normal", "group": "B", "verdict": "PASS", "model_output": '<img src=x onerror="alert(1)">', "usage": {"total_tokens": 30, "cost": {"total": 0}}},
                {"task_id": "dev2", "task_family": "goal", "group": "B", "verdict": "FAIL"},
            ], "C_DEV": {"tasks": [{"task_id": "dev1", "verdict": "PASS"}, {"task_id": "dev2", "verdict": "PASS"}]}},
            "LOCKED_EVAL": {"sample_size": 2, "tasks": [
                {"task_id": "held1", "task_family": "fault", "A": {"passed": True}, "B": {"passed": True}, "C": {"passed": False}},
                {"task_id": "held2", "task_family": "policy", "A": {"passed": 1}, "B": {"passed": True}, "C": {"classification": "INFRA"}},
            ]},
        },
    }


def test_benchmark_unknowns_paired_regressions_and_version_summaries(tmp_path):
    write_json(tmp_path / "docs" / "p6_real_model_abc_raw_results.json", real_report())
    d = build_snapshot(tmp_path)
    b = d["benchmarks"][0]
    assert b["mode"] == "real_model"
    dev, held = b["partitions"]
    assert dev["computed_summary"]["A"] == {"passed": 0, "recorded": 0, "total": 3, "unknown": 3}
    assert dev["computed_summary"]["B"] == {"passed": 1, "recorded": 2, "total": 3, "unknown": 1}
    assert dev["computed_summary"]["paired_b_to_c"] == {"improved": 1, "degraded": 0, "unchanged": 1, "unknown": 1}
    assert held["computed_summary"]["paired_b_to_c"] == {"improved": 0, "degraded": 1, "unchanged": 0, "unknown": 1}
    assert held["rows"][1]["A"]["passed"] is None
    assert held["rows"][1]["C"]["classification"] == "INFRA"
    assert dev["rows"][0]["B"]["output_text"].startswith("<img")
    assert dev["rows"][0]["B"]["usage"]["cost"] is None
    assert dev["rows"][1]["B"]["usage"] is None
    assert d["candidates"][0]["body_is_preview"] is True
    assert d["candidates"][1]["parent_candidate_id"] == "v1"
    assert d["validations"] == d["releases"] == []
    assert "must-hide-path" not in json.dumps(d)


def test_d7_adds_fourth_arm_and_keeps_unknown_pair(tmp_path):
    path = tmp_path / "scratch" / "pilot" / "report.json"
    write_json(path, {
        "status": "COMPLETED",
        "metadata": {"experiment_name": "D7", "group_labels": {
            "A": "No history", "B": "Episode", "C": "Skill", "D": "Skill plus source",
        }},
        "partitions": {"BOUNDARY": {"sample_size": 2, "tasks": [
            {"task_id": "scope", "B": {"passed": True}, "C": {"passed": False},
             "D": {"passed": True, "run_id": "run-d", "source_lookup_ids": ["ep-1"]}},
            {"task_id": "outage", "C": {"passed": True}, "D": {"passed": None, "classification": "INFRA_ERROR"}},
        ]}},
    })
    benchmark = build_snapshot(tmp_path)["benchmarks"][0]
    part = benchmark["partitions"][0]
    assert benchmark["group_labels"]["D"] == "Skill plus source"
    assert part["computed_summary"]["paired_b_to_c"]["degraded"] == 1
    assert part["computed_summary"]["paired_c_to_d"] == {"improved": 1, "degraded": 0, "unchanged": 0, "unknown": 1}
    assert part["rows"][0]["D"]["source_lookup_ids"] == ["ep-1"]
    assert part["rows"][0]["D"]["run_id"] == "run-d"


def test_scripted_exploration_challenge_and_mirror_are_separate(tmp_path):
    main = real_report()
    row = {"task_id": "old1", "split": "LOCKED_EVAL", "task_family": "same-origin", "group_a": {"pass": False}, "group_b": {"pass": True}, "group_c_fresh": {"pass": False}}
    old = {"real_model_experiment": main}
    for key in ["prospective_family_isolated_benchmark", "exploratory_isomorphic_benchmark", "post_hoc_scripted_challenge_benchmark"]:
        old[key] = {"benchmark_status": "historical", "raw_task_results": [row]}
    write_json(tmp_path / "docs" / "p6_logistics_abc_raw_results.json", old)
    write_json(tmp_path / "docs" / "p6_real_model_abc_raw_results.json", main)
    d = build_snapshot(tmp_path)
    assert sorted(b["mode"] for b in d["benchmarks"]) == ["challenge", "exploratory", "real_model", "scripted"]
    scripted = next(b for b in d["benchmarks"] if b["mode"] == "scripted")
    assert scripted["partitions"][0]["computed_summary"]["paired_b_to_c"]["degraded"] == 1


def test_superseded_report_preserves_failure_and_links(tmp_path):
    folder = tmp_path / "scratch" / "run-original"
    write_json(folder / "report.json", {"run_id": "old-fault", "status": "COMPLETED", "tool_records": [{"call_id": "c1", "tool_name": "tracking", "error_type": "HANDLER_ERROR"}]})
    write_json(tmp_path / "scratch" / "acceptance_review.json", {"superseded_fault_sample": {"run_id": "old-fault", "source": str(folder / "report.json")}, "cases": [{"run_id": "replacement", "status": "COMPLETED", "episode": {"episode_id": "new-episode", "purpose": "evaluation", "outcome": "success"}}]})
    d = build_snapshot(tmp_path)
    original = next(r for r in d["runs"] if r["run_id"] == "old-fault")
    assert original["superseded"] is True
    assert original["tool_records"][0]["error_type"] == "HANDLER_ERROR"
    assert d["episodes"][0]["episode_id"] == "new-episode"


def test_sensitive_data_and_nonfinite_values_never_reach_json():
    x = redact_sensitive({"apiKey": "abc", "authorization": "secret", "thinking": "hidden", "nested": [{"reasoning_content": "private", "output_text": "Bearer abcdefghijklmnop sk-abcdefghijklmnop ark-abcdefghijklmnop"}], "total_tokens": 40, "latency_ms": float("nan")})
    assert x["total_tokens"] == 40
    assert x["latency_ms"] is None
    assert "hidden" not in json.dumps(x, allow_nan=False)
    assert "abcdefghijklmnop" not in json.dumps(x)


def test_file_and_row_limits_are_visible(tmp_path, monkeypatch):
    import skillforge.dashboard_data as module

    monkeypatch.setattr(module, "MAX_FILES", 4)
    monkeypatch.setattr(module, "MAX_ROWS", 2)
    for i in range(6):
        write_json(tmp_path / "scratch" / f"case_{i}.json", {"run_id": f"r{i}"})
    d = build_snapshot(tmp_path)
    assert len(d["sources"]) <= 4
    assert any("上限" in w for w in d["warnings"])
    (tmp_path / "runs").mkdir()
    with sqlite3.connect(tmp_path / "runs" / "state.db") as c:
        c.execute("CREATE TABLE runtime_runs (run_id TEXT)")
        c.executemany("INSERT INTO runtime_runs VALUES (?)", [(f"db{i}",) for i in range(5)])
    d = build_snapshot(tmp_path, [tmp_path / "runs"])
    assert len(d["runs"]) == 2
    assert any("截断" in w for w in d["warnings"])


def test_dashboard_browser_artifacts_are_not_execution_evidence(tmp_path):
    write_json(tmp_path / "scratch" / "dashboard-acceptance" / "browser-profile" / "report.json", {"run_id": "not-an-agent-run"})
    assert build_snapshot(tmp_path)["runs"] == []


def test_episode_retrieval_projection_excludes_private_environment(tmp_path):
    directory = tmp_path / "runs"
    directory.mkdir()
    env = {"purpose": "learning", "enable_reuse": True, "retrieval_hit": True,
           "selected_skill": "logistics", "selected_version": "2",
           "private_customer": "not-for-dashboard", "apiKey": "private-key"}
    with sqlite3.connect(directory / "episodes.db") as conn:
        conn.executescript(SCHEMA_SQL)
        conn.execute("INSERT INTO episodes (episode_id,task_id,run_id,skill_name,skill_version,environment_json,provenances_json,acceptance_json,outcome) VALUES ('e','t','r','logistics','2',?,'[]','{}','success')", (json.dumps(env),))
    data = build_snapshot(tmp_path)
    assert data["episodes"][0]["reuse"] == {
        "enable_reuse": True, "retrieval_hit": True,
        "selected_skill": "logistics", "selected_version": "2",
    }
    assert "not-for-dashboard" not in json.dumps(data)
    assert "private-key" not in json.dumps(data)


def test_page_lineage_requires_explicit_scope_version_body_and_retrieval():
    node = shutil.which("node")
    if node is None:
        pytest.skip("Node is required for the dashboard lineage JavaScript check")
    page = (Path(__file__).parents[1] / "src/skillforge/dashboard.html").read_text()
    logic = page.split("function candidateLinks(c){", 1)[1].split("\nfunction runButton", 1)[0]
    candidate = {"scope": "one", "candidate_id": "c", "skill_name": "logistics", "body": "new",
                 "meta": {"version": "2"}, "source_episode_ids": ["source", "missing"]}
    source = {"scope": "one", "episode_id": "source", "run_id": "base"}
    reuse_binding = {"enable_reuse": True, "retrieval_hit": True, "selected_skill": "logistics", "selected_version": "2"}
    data = {"validations": [{"scope": "one", "candidate_id": "c", "record_id": "v", "baseline_version": "1", "release_id": "rel", "promoted": 1}],
            "releases": [{"scope": "one", "release_id": "rel", "skill_name": "logistics", "version": "2"}],
            "episodes": [source], "runs": [], "repair_jobs": [], "candidates": [candidate]}
    data["runs"].append({"scope": "one", "run_id": "base", "skill_name": "logistics", "skill_version": "1", "frozen_body": "old"})
    for name, scope, body, binding in [
        ("good", "one", "new", reuse_binding),
        ("same-name-only", "one", "new", {}),
        ("wrong-body", "one", "other", reuse_binding),
        ("wrong-scope", "two", "new", reuse_binding),
        ("textual-true", "one", "new", {**reuse_binding, "retrieval_hit": "true"}),
    ]:
        data["runs"].append({"scope": scope, "run_id": name, "task_id": name, "skill_name": "logistics", "skill_version": "2", "frozen_body": body})
        data["episodes"].append({"scope": scope, "episode_id": name, "run_id": name, "reuse": binding})
    data["repair_jobs"] = [{"scope": scope, "job_id": scope, "latest_candidate_id": "c"} for scope in ("one", "two")]
    script = ("const state={data:" + json.dumps(data) + "};\n"
              "function sameScope(a,b){return a.scope!==undefined&&a.scope===b.scope}\n"
              "function candidateLinks(c){" + logic + "\n"
              "const c=state.data.candidates[0];const a=candidateLinks(c);"
              "if(a.sources.length!==2||a.sources[1].episode!==undefined||a.parent!==undefined)throw Error('invented source');"
              "if(a.baselines.map(x=>x.run_id).join()!=='base')throw Error('wrong baseline');"
              "if(a.reuse.map(x=>x.run_id).join()!=='good'||a.jobs.length!==1)throw Error('false reuse or cross scope');"
              "if(candidateLinks({...c,body_is_preview:true}).reuse.length)throw Error('preview as full body');"
              "state.data.releases[0].version='3';if(candidateLinks(c).release||candidateLinks(c).reuse.length)throw Error('wrong release version');"
              "process.stdout.write('PASS');")
    result = subprocess.run([node, "-e", script], capture_output=True, text=True, timeout=10)
    assert result.returncode == 0, result.stderr
    assert result.stdout == "PASS"


def test_benchmark_review_scoped_annotation_keeps_original_scores(tmp_path):
    report = {'metadata':{'experiment_name':'D5'}, 'partitions':{
        'DEV':{'sample_size':1,'tasks':[{'task_id':'t','B':{'passed':False}}]}}}
    for scope in ['one','two']:
        write_json(tmp_path/'scratch'/scope/'report.json', report)
    write_json(tmp_path/'scratch'/'one'/'acceptance_review.json', {'benchmark_review':{
        'source_report':'report.json','status':'ORACLE_FALSE_POSITIVE',
        'findings':['原判定保留，复核另存。']}})
    before = (tmp_path/'scratch'/'one'/'report.json').read_bytes()
    data = build_snapshot(tmp_path)
    one = next(b for b in data['benchmarks'] if b['scope']=='scratch/one')
    two = next(b for b in data['benchmarks'] if b['scope']=='scratch/two')
    assert one['status'] == 'ORACLE_FALSE_POSITIVE'
    assert one['notes'][-1] == '原判定保留，复核另存。'
    assert 'review' not in two
    assert one['partitions'][0]['computed_summary']['B']['passed'] == 0
    assert (tmp_path/'scratch'/'one'/'report.json').read_bytes() == before
