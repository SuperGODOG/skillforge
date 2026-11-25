"""Standalone tests for D7a frozen pilot protocol schema, validation, freeze, and load."""
from __future__ import annotations

import copy
import hashlib
import json
import os
from pathlib import Path
from typing import Any

import pytest

from skillforge.scenarios import d7_oracle, d7_protocol
from skillforge.scenarios.d7_protocol import (
    freeze_protocol,
    get_logistics_oracle_sha256,
    load_protocol,
    validate_protocol,
)


def _valid_protocol() -> dict[str, Any]:
    return {
        "schema_version": 1,
        "model_config": {
            "provider": "openai",
            "model_id": "gpt-4o-mini",
            "reasoning": "off",
            "temperature": 0.0,
            "max_tokens": 1024,
            "max_retries": 0,
            "timeout_ms": 30000,
        },
        "system_prompt_header": "你是严谨的电商多包裹物流核查助手，只允许只读工具查询。",
        "budget": {
            "max_provider_calls": 100,
            "max_total_tokens": 200_000,
            "deadline_seconds": 1800,
            "per_run_steps": 12,
            "memory_max_bytes": 64 * 1024 * 1024,
            "timeout_seconds": 60,
        },
        "oracle_sha256": get_logistics_oracle_sha256(),
        "grading_sha256": hashlib.sha256(Path(d7_oracle.__file__).read_bytes()).hexdigest(),
        "skill": {
            "name": "logistics_multi_pkg_check",
            "version": "1.0.0",
            "body": "## Checklist\n1. Query order packages.\n2. Query each package tracking.",
            "source_episode_ids": ["EP_LEARN_01", "EP_LEARN_02"],
        },
        "episodes": [
            {
                "episode_id": "EP_LEARN_01",
                "source_task_id": "SRC_DEV_01",
                "root_family": "FAM_PARTIAL_TRANSIT",
                "derived_from": [],
                "purpose": "learning",
                "outcome": "success",
                "trace": {
                    "query": "核实测试学习订单 ORD_SYN_LEARN_01 的包裹状态",
                    "steps": [{"tool": "query_order_packages", "order_id": "ORD_SYN_LEARN_01"}],
                },
            },
            {
                "episode_id": "EP_LEARN_02",
                "source_task_id": "SRC_DEV_02",
                "root_family": "FAM_DELAY_EXPLAIN",
                "derived_from": ["ANC_DEV_00"],
                "purpose": "learning",
                "outcome": "failure",
                "trace": {
                    "query": "核实测试学习订单 ORD_SYN_LEARN_02 是否有异常",
                    "error": "missed second package",
                },
            },
        ],
        "tasks": [
            {
                "task_id": "D7_T_01",
                "source_task_id": "SRC_EVAL_T_01",
                "root_family": "FAM_EVAL_TRANSFER_01",
                "derived_from": [],
                "kind": "transfer",
                "query": "请核实合成订单 ORD_D7_SYN_901 的所有包裹最新物流状态。",
                "order_id": "ORD_D7_SYN_901",
                "tool_snapshot": {
                    "order": {
                        "order_id": "ORD_D7_SYN_901",
                        "buyer": "d7_syn_buyer_01",
                        "packages": ["PKG_D7_901A", "PKG_D7_901B"],
                        "order_status": "SHIPPED",
                    },
                    "packages": {
                        "PKG_D7_901A": {
                            "package_id": "PKG_D7_901A",
                            "carrier": "SF_EXPRESS",
                            "status": "DELIVERED",
                            "signed_by": "本人签收",
                        },
                        "PKG_D7_901B": {
                            "package_id": "PKG_D7_901B",
                            "carrier": "ZTO_EXPRESS",
                            "status": "IN_TRANSIT",
                            "location": "杭州转运中心",
                        },
                    },
                },
            },
            {
                "task_id": "D7_B_01",
                "source_task_id": "SRC_EVAL_B_01",
                "root_family": "FAM_EVAL_BOUNDARY_01",
                "derived_from": ["ANC_EVAL_01"],
                "kind": "boundary",
                "query": "核实合成订单 ORD_D7_SYN_902 物流状态，只列出状态，不要给任何建议。",
                "order_id": "ORD_D7_SYN_902",
                "intent_constraint": "STATUS_ONLY",
                "failing_package_ids": ["PKG_D7_902B"],
                "tool_snapshot": {
                    "order": {
                        "order_id": "ORD_D7_SYN_902",
                        "buyer": "d7_syn_buyer_02",
                        "packages": ["PKG_D7_902A", "PKG_D7_902B"],
                        "order_status": "SHIPPED",
                    },
                    "packages": {
                        "PKG_D7_902A": {
                            "package_id": "PKG_D7_902A",
                            "carrier": "JD_LOGISTICS",
                            "status": "DELIVERED",
                        },
                        "PKG_D7_902B": {
                            "package_id": "PKG_D7_902B",
                            "carrier": "YTO_EXPRESS",
                            "status": "TOOL_ERROR",
                            "error_reason": "NETWORK_TIMEOUT",
                        },
                    },
                },
            },
        ],
    }


def test_validate_freeze_and_load_roundtrip(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    # Ensure validate_protocol never launches a PiRunnerClient subprocess.
    def _fail_runner(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("PiRunnerClient should never be instantiated during protocol validation")

    monkeypatch.setattr("skillforge.pi_text_client.PiRunnerClient", _fail_runner)

    proto = _valid_protocol()
    validate_protocol(proto)

    target = tmp_path / "frozen_d7"
    digest = freeze_protocol(target, proto)
    assert len(digest) == 64
    assert (target / "protocol.json").is_file()
    assert (target / "protocol.sha256").read_text(encoding="utf-8").strip() == digest

    loaded, loaded_sha = load_protocol(target)
    assert loaded_sha == digest
    assert loaded == proto


def test_freeze_refuses_overwrite_on_either_file(tmp_path: Path):
    proto = _valid_protocol()
    freeze_dir = tmp_path / "already"
    freeze_protocol(freeze_dir, proto)

    # Both exist
    with pytest.raises(FileExistsError):
        freeze_protocol(freeze_dir, proto)

    # Only protocol.json exists -> must refuse and preserve existing bytes
    only_json_dir = tmp_path / "only_json"
    only_json_dir.mkdir()
    (only_json_dir / "protocol.json").write_bytes(b"old-json")
    with pytest.raises(FileExistsError):
        freeze_protocol(only_json_dir, proto)
    assert (only_json_dir / "protocol.json").read_bytes() == b"old-json"
    assert not (only_json_dir / "protocol.sha256").exists()

    # Only protocol.sha256 exists -> must refuse and preserve existing bytes
    only_sha_dir = tmp_path / "only_sha"
    only_sha_dir.mkdir()
    (only_sha_dir / "protocol.sha256").write_text("old-sha", encoding="utf-8")
    with pytest.raises(FileExistsError):
        freeze_protocol(only_sha_dir, proto)
    assert (only_sha_dir / "protocol.sha256").read_text(encoding="utf-8") == "old-sha"
    assert not (only_sha_dir / "protocol.json").exists()


def test_freeze_validates_before_writing_and_rolls_back_on_write_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    bad_proto = _valid_protocol()
    bad_proto["schema_version"] = 2
    bad_dir = tmp_path / "invalid_never_created"
    with pytest.raises(ValueError, match="schema_version"):
        freeze_protocol(bad_dir, bad_proto)
    assert not bad_dir.exists()

    # Simulate failure on publishing protocol.sha256: no partial pair may remain.
    proto = _valid_protocol()
    half_fail_dir = tmp_path / "half_fail"
    real_link = os.link
    calls = {"count": 0}

    def _failing_link(src: Any, dst: Any) -> None:
        calls["count"] += 1
        if calls["count"] == 2:
            raise OSError("Simulated disk full on protocol.sha256")
        real_link(src, dst)

    monkeypatch.setattr(d7_protocol.os, "link", _failing_link)
    with pytest.raises(OSError, match="Simulated disk full"):
        freeze_protocol(half_fail_dir, proto)

    assert not (half_fail_dir / "protocol.json").exists()
    assert not (half_fail_dir / "protocol.sha256").exists()


def test_freeze_preserves_concurrent_protocol(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    real_link = os.link

    def publish_after_competitor(src, dst):
        Path(dst).write_text("other writer", encoding="utf-8")
        real_link(src, dst)

    monkeypatch.setattr(d7_protocol.os, "link", publish_after_competitor)
    with pytest.raises(FileExistsError):
        freeze_protocol(tmp_path, _valid_protocol())
    assert (tmp_path / "protocol.json").read_text() == "other writer"
    assert not (tmp_path / "protocol.sha256").exists()


def test_order_lookup_can_be_skipped_only_for_a_named_package():
    protocol = _valid_protocol()
    task = protocol["tasks"][0]
    package_id = next(iter(task["tool_snapshot"]["packages"]))
    task.update(scope_package_ids=[package_id], requires_order_lookup=False)
    with pytest.raises(ValueError, match="single package named in the request"):
        validate_protocol(protocol)
    task["query"] += " " + package_id
    validate_protocol(protocol)


def test_load_rejects_half_pair_and_tampered_bytes(tmp_path: Path):
    proto = _valid_protocol()
    d = tmp_path / "tamper"
    freeze_protocol(d, proto)

    # Tamper 1 byte in protocol.json
    json_path = d / "protocol.json"
    original_bytes = json_path.read_bytes()
    json_path.write_bytes(original_bytes + b" ")
    with pytest.raises(ValueError, match="hash verification failed"):
        load_protocol(d)

    # Restore json, tamper sha256
    json_path.write_bytes(original_bytes)
    sha_path = d / "protocol.sha256"
    sha_path.write_text("0" * 64, encoding="utf-8")
    with pytest.raises(ValueError, match="hash verification failed"):
        load_protocol(d)

    # Half-pair: missing protocol.sha256
    sha_path.unlink()
    with pytest.raises(FileNotFoundError):
        load_protocol(d)

    # Half-pair: missing protocol.json
    sha_path.write_text(hashlib.sha256(original_bytes).hexdigest(), encoding="utf-8")
    json_path.unlink()
    with pytest.raises(FileNotFoundError):
        load_protocol(d)


def test_load_revalidates_schema_after_hash_check(tmp_path: Path):
    # Even if protocol.sha256 matches raw bytes, invalid content must still fail load_protocol.
    proto = _valid_protocol()
    proto["oracle_sha256"] = "deadbeef" * 8
    raw = json.dumps(proto, indent=2, ensure_ascii=False).encode("utf-8")
    d = tmp_path / "recheck"
    d.mkdir()
    (d / "protocol.json").write_bytes(raw)
    (d / "protocol.sha256").write_text(hashlib.sha256(raw).hexdigest(), encoding="utf-8")

    with pytest.raises(ValueError, match="Oracle source hash mismatch"):
        load_protocol(d)


def test_oracle_drift_rejected():
    proto = _valid_protocol()
    proto["oracle_sha256"] = "a" * 64
    with pytest.raises(ValueError, match="Oracle source hash mismatch"):
        validate_protocol(proto)


def test_model_config_rejects_secrets_unknown_fields_and_bools():
    for bad_cfg in [
        {"provider": "openai", "model_id": "gpt-4o", "apiKey": "sk-12345678901234"},
        {"provider": "openai", "model_id": "gpt-4o", "thinking_level": "minimal"},
        {"provider": "openai", "model_id": "gpt-4o", "unknown_flag": 1},
        {"provider": "faux", "model_id": "mock-model"},
        {"provider": "openai", "model_id": "gpt-4o", "is_test": True},
        {"provider": "openai", "model_id": "gpt-4o", "reasoning": "minimal"},
        {"provider": "openai", "model_id": "gpt-4o", "max_tokens": 8192},
        {"provider": "openai", "model_id": "gpt-4o", "max_retries": False},
    ]:
        proto = _valid_protocol()
        proto["model_config"] = bad_cfg
        with pytest.raises(ValueError):
            validate_protocol(proto)


@pytest.mark.parametrize(
    "field,value",
    [
        ("max_provider_calls", 0),
        ("max_provider_calls", -1),
        ("max_provider_calls", True),
        ("max_provider_calls", 201),
        ("max_total_tokens", 0),
        ("max_total_tokens", False),
        ("max_total_tokens", 400_001),
        ("deadline_seconds", 0),
        ("deadline_seconds", float("nan")),
        ("deadline_seconds", float("inf")),
        ("deadline_seconds", 3600.1),
        ("per_run_steps", 0),
        ("per_run_steps", True),
        ("memory_max_bytes", -100),
        ("timeout_seconds", float("nan")),
        ("timeout_seconds", False),
    ],
)
def test_budget_rejects_invalid_bool_nan_and_bounds(field: str, value: Any):
    proto = _valid_protocol()
    proto["budget"][field] = value
    with pytest.raises(ValueError):
        validate_protocol(proto)


def test_polluted_episode_purpose_rejected():
    proto = _valid_protocol()
    proto["episodes"][0]["purpose"] = "locked_eval"
    with pytest.raises(ValueError, match="Polluted episode purpose"):
        validate_protocol(proto)


def test_dangling_empty_and_duplicate_skill_source_rejected():
    # Dangling source episode
    p1 = _valid_protocol()
    p1["skill"]["source_episode_ids"] = ["EP_LEARN_01", "EP_MISSING"]
    with pytest.raises(ValueError, match="Dangling skill.source_episode_ids"):
        validate_protocol(p1)

    # Empty source_episode_ids
    p2 = _valid_protocol()
    p2["skill"]["source_episode_ids"] = []
    with pytest.raises(ValueError, match="must not be empty"):
        validate_protocol(p2)

    # Duplicate source_episode_ids
    p3 = _valid_protocol()
    p3["skill"]["source_episode_ids"] = ["EP_LEARN_01", "EP_LEARN_01"]
    with pytest.raises(ValueError, match="duplicate"):
        validate_protocol(p3)


def test_empty_either_task_kind_rejected():
    # Only transfer
    p1 = _valid_protocol()
    p1["tasks"] = [t for t in p1["tasks"] if t["kind"] == "transfer"]
    with pytest.raises(ValueError, match="boundary"):
        validate_protocol(p1)

    # Only boundary
    p2 = _valid_protocol()
    p2["tasks"] = [t for t in p2["tasks"] if t["kind"] == "boundary"]
    with pytest.raises(ValueError, match="transfer"):
        validate_protocol(p2)


def test_cross_side_same_root_different_intent_and_shared_derived_rejected():
    # Same root_family across episode & task even when intent_constraint differs (STATUS_ONLY vs none)
    p1 = _valid_protocol()
    p1["tasks"][1]["root_family"] = p1["episodes"][0]["root_family"]
    p1["tasks"][1]["intent_constraint"] = "STATUS_ONLY"
    with pytest.raises(ValueError, match="root_family leakage"):
        validate_protocol(p1)

    # Shared derived_from across episode and task
    p2 = _valid_protocol()
    p2["tasks"][0]["derived_from"] = ["ANC_DEV_00"]
    with pytest.raises(ValueError, match="lineage leakage"):
        validate_protocol(p2)

    # Task derived_from referencing episode_id
    p3 = _valid_protocol()
    p3["tasks"][0]["derived_from"] = ["EP_LEARN_01"]
    with pytest.raises(ValueError, match="lineage leakage"):
        validate_protocol(p3)

    # Shared source_task_id across episode and task
    p4 = _valid_protocol()
    p4["tasks"][0]["source_task_id"] = "SRC_DEV_01"
    with pytest.raises(ValueError, match="lineage leakage"):
        validate_protocol(p4)


def test_snapshot_bad_references_rejected():
    # 1. order.order_id != task.order_id
    p1 = _valid_protocol()
    p1["tasks"][0]["tool_snapshot"]["order"]["order_id"] = "ORD_OTHER"
    with pytest.raises(ValueError, match="order_id"):
        validate_protocol(p1)

    # 2. order.packages and packages keys mismatch
    p2 = _valid_protocol()
    p2["tasks"][0]["tool_snapshot"]["order"]["packages"].append("PKG_GHOST")
    with pytest.raises(ValueError, match="package set mismatch"):
        validate_protocol(p2)

    # 3. packages record primary key mismatch
    p3 = _valid_protocol()
    p3["tasks"][0]["tool_snapshot"]["packages"]["PKG_D7_901A"]["package_id"] = "PKG_WRONG"
    with pytest.raises(ValueError, match="package_id"):
        validate_protocol(p3)

    # 4. failing_package_ids references package outside this order
    p4 = _valid_protocol()
    p4["tasks"][1]["failing_package_ids"] = ["PKG_D7_901A"]
    with pytest.raises(ValueError, match="failing_package_ids"):
        validate_protocol(p4)


def test_normalized_exact_query_collision_counterexample():
    # Two tasks with different IDs but whitespace/case-normalized identical query
    p1 = _valid_protocol()
    p1["tasks"][1]["query"] = "  请核实合成订单 ord_d7_syn_901   的所有包裹最新物流状态。 "
    with pytest.raises(ValueError, match="query collision"):
        validate_protocol(p1)

    # Task query matching an episode trace query (normalized)
    p2 = _valid_protocol()
    p2["tasks"][0]["query"] = "核实测试学习订单 ord_syn_learn_01   的包裹状态"
    with pytest.raises(ValueError, match="query collision"):
        validate_protocol(p2)


def test_schema_version_and_unique_ids_and_intent_constraint():
    p1 = _valid_protocol()
    p1["schema_version"] = True
    with pytest.raises(ValueError, match="schema_version"):
        validate_protocol(p1)

    p2 = _valid_protocol()
    p2["tasks"][1]["task_id"] = p2["tasks"][0]["task_id"]
    with pytest.raises(ValueError, match="Duplicate task_id"):
        validate_protocol(p2)

    p3 = _valid_protocol()
    p3["episodes"][1]["episode_id"] = p3["episodes"][0]["episode_id"]
    with pytest.raises(ValueError, match="Duplicate episode_id"):
        validate_protocol(p3)

    p4 = _valid_protocol()
    p4["tasks"][0]["intent_constraint"] = "NO_REFUND"
    with pytest.raises(ValueError, match="intent_constraint"):
        validate_protocol(p4)

    # Confirm non-fixture order IDs remain valid without touching global logistics fixtures.
    p5 = copy.deepcopy(_valid_protocol())
    validate_protocol(p5)
