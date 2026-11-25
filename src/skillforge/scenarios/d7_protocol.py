"""D7a: Frozen experiment protocol schema, validation, freeze, and load API.

Locks the corpus, distilled skill, isolated synthetic tasks, logistics oracle
digest, model configuration, and safety budget before any D7 A/B/C/D pilot run.

Note: Near-duplicate semantic review is the supervisor's responsibility. This
validator enforces structural lineage disjointness and a small normalized exact
query counterexample check (between tasks and between tasks and episode trace
queries) without claiming to detect all near-duplicates automatically.
"""
from __future__ import annotations

import hashlib
import json
import math
import os
from pathlib import Path
from typing import Any
import uuid

from skillforge.pi_text_client import FORBIDDEN_CONFIG_KEYS, PiTextClient
from skillforge.scenarios import d7_oracle, logistics


PROTOCOL_FILENAME = "protocol.json"
SHA256_FILENAME = "protocol.sha256"

D5_MAX_PROVIDER_CALLS = 200
D5_MAX_TOTAL_TOKENS = 400_000
D5_MAX_DEADLINE_SECONDS = 3600

# Public fields actually read and retained by PiTextClient (pi_text_client.py).
ALLOWED_MODEL_CONFIG_KEYS = frozenset({
    "provider",
    "model_id",
    "reasoning",
    "temperature",
    "max_tokens",
    "max_retries",
    "timeout_ms",
})

REQUIRED_TOP_LEVEL_KEYS = frozenset({
    "schema_version",
    "model_config",
    "system_prompt_header",
    "budget",
    "oracle_sha256",
    "grading_sha256",
    "skill",
    "episodes",
    "tasks",
})

REQUIRED_BUDGET_KEYS = frozenset({
    "max_provider_calls",
    "max_total_tokens",
    "deadline_seconds",
    "per_run_steps",
    "memory_max_bytes",
    "timeout_seconds",
})

REQUIRED_SKILL_KEYS = frozenset({"name", "version", "body", "source_episode_ids"})

REQUIRED_EPISODE_KEYS = frozenset({
    "episode_id",
    "source_task_id",
    "root_family",
    "derived_from",
    "purpose",
    "outcome",
    "trace",
})

REQUIRED_TASK_KEYS = frozenset({
    "task_id",
    "source_task_id",
    "root_family",
    "derived_from",
    "kind",
    "query",
    "order_id",
    "tool_snapshot",
})
OPTIONAL_TASK_KEYS = frozenset({"intent_constraint", "failing_package_ids", "scope_package_ids", "requires_order_lookup"})


def get_logistics_oracle_sha256() -> str:
    """Return the raw-bytes SHA256 hex digest of local logistics.py."""
    oracle_path = Path(logistics.__file__).resolve()
    return hashlib.sha256(oracle_path.read_bytes()).hexdigest()


def _require_non_empty_str(value: Any, field_name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"'{field_name}' must be a non-empty string")
    return value


def _require_str_list(value: Any, field_name: str, *, allow_empty: bool = True, unique: bool = False) -> list[str]:
    if not isinstance(value, list):
        raise ValueError(f"'{field_name}' must be a list of strings")
    if not allow_empty and not value:
        raise ValueError(f"'{field_name}' must not be empty")
    items: list[str] = []
    for idx, item in enumerate(value):
        items.append(_require_non_empty_str(item, f"{field_name}[{idx}]"))
    if unique and len(set(items)) != len(items):
        raise ValueError(f"'{field_name}' must not contain duplicate entries")
    return items


def _require_positive_int(value: Any, field_name: str, *, upper_bound: int | None = None) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ValueError(f"'{field_name}' must be a positive integer")
    if upper_bound is not None and value > upper_bound:
        raise ValueError(f"'{field_name}' exceeds D5 upper bound: {value} > {upper_bound}")
    return value


def _require_positive_number(value: Any, field_name: str, *, upper_bound: float | None = None) -> float:
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not math.isfinite(value)
        or value <= 0
    ):
        raise ValueError(f"'{field_name}' must be a finite positive number")
    if upper_bound is not None and value > upper_bound:
        raise ValueError(f"'{field_name}' exceeds D5 upper bound: {value} > {upper_bound}")
    return float(value)


def _check_exact_keys(mapping: Any, required: frozenset[str], label: str, *, optional: frozenset[str] = frozenset()) -> dict[str, Any]:
    if not isinstance(mapping, dict):
        raise ValueError(f"'{label}' must be a dictionary")
    keys = set(mapping.keys())
    missing = sorted(required - keys)
    if missing:
        raise ValueError(f"'{label}' missing required keys: {missing}")
    unknown = sorted(keys - (required | optional))
    if unknown:
        raise ValueError(f"'{label}' contains unknown keys: {unknown}")
    return mapping


def _normalize_query(text: str) -> str:
    return " ".join(text.strip().lower().split())


def _validate_model_config(model_cfg: Any) -> None:
    if not isinstance(model_cfg, dict):
        raise ValueError("'model_config' must be a dictionary")
    for k in model_cfg:
        if not isinstance(k, str):
            raise ValueError("'model_config' keys must be strings")
        if k.lower() in FORBIDDEN_CONFIG_KEYS:
            raise ValueError(f"Forbidden secret or endpoint key in model_config: '{k}'")

    unknown = sorted(set(model_cfg.keys()) - ALLOWED_MODEL_CONFIG_KEYS)
    if unknown:
        raise ValueError(f"Unsupported or unknown keys in model_config: {unknown}")

    _require_non_empty_str(model_cfg.get("provider"), "model_config.provider")
    _require_non_empty_str(model_cfg.get("model_id"), "model_config.model_id")

    # Guard bools where PiTextClient uses isinstance(..., int) (bool subclasses int).
    for int_key in ("max_tokens", "max_retries", "timeout_ms"):
        if int_key in model_cfg and isinstance(model_cfg[int_key], bool):
            raise ValueError(f"'model_config.{int_key}' must not be a boolean")

    # Validates PiTextClient bounds/constraints without starting a model or subprocess.
    PiTextClient(dict(model_cfg))


def _validate_budget(budget: Any) -> None:
    cfg = _check_exact_keys(budget, REQUIRED_BUDGET_KEYS, "budget")
    _require_positive_int(cfg["max_provider_calls"], "budget.max_provider_calls", upper_bound=D5_MAX_PROVIDER_CALLS)
    _require_positive_int(cfg["max_total_tokens"], "budget.max_total_tokens", upper_bound=D5_MAX_TOTAL_TOKENS)
    _require_positive_number(cfg["deadline_seconds"], "budget.deadline_seconds", upper_bound=D5_MAX_DEADLINE_SECONDS)
    _require_positive_int(cfg["per_run_steps"], "budget.per_run_steps")
    _require_positive_int(cfg["memory_max_bytes"], "budget.memory_max_bytes")
    _require_positive_number(cfg["timeout_seconds"], "budget.timeout_seconds")


def _validate_episodes(raw_episodes: Any) -> tuple[set[str], set[str], set[str], dict[str, str]]:
    if not isinstance(raw_episodes, list) or not raw_episodes:
        raise ValueError("'episodes' must be a non-empty list")

    ep_ids: set[str] = set()
    ep_lineage: set[str] = set()
    ep_roots: set[str] = set()
    ep_trace_queries: dict[str, str] = {}

    for idx, ep in enumerate(raw_episodes):
        label = f"episodes[{idx}]"
        rec = _check_exact_keys(ep, REQUIRED_EPISODE_KEYS, label)
        ep_id = _require_non_empty_str(rec["episode_id"], f"{label}.episode_id")
        if ep_id in ep_ids:
            raise ValueError(f"Duplicate episode_id: '{ep_id}'")
        ep_ids.add(ep_id)

        source_id = _require_non_empty_str(rec["source_task_id"], f"{label}.source_task_id")
        root_family = _require_non_empty_str(rec["root_family"], f"{label}.root_family")
        derived = _require_str_list(rec["derived_from"], f"{label}.derived_from", allow_empty=True)

        if rec["purpose"] != "learning":
            raise ValueError(f"Polluted episode purpose at {label}: expected 'learning', got {rec['purpose']!r}")
        if rec["outcome"] not in ("success", "failure"):
            raise ValueError(f"Invalid episode outcome at {label}: {rec['outcome']!r}")

        trace = rec["trace"]
        if not isinstance(trace, dict) or not trace:
            raise ValueError(f"'{label}.trace' must be a non-empty dictionary")

        for q_key in ("query", "user_query"):
            q_val = trace.get(q_key)
            if isinstance(q_val, str) and q_val.strip():
                ep_trace_queries[_normalize_query(q_val)] = ep_id
                break

        ep_roots.add(root_family)
        ep_lineage.update((ep_id, source_id, *derived))

    return ep_ids, ep_roots, ep_lineage, ep_trace_queries


def _validate_tool_snapshot(snapshot: Any, order_id: str, failing_pkg_ids: list[str] | None, label: str) -> None:
    snap = _check_exact_keys(snapshot, frozenset({"order", "packages"}), f"{label}.tool_snapshot")
    order = snap["order"]
    packages = snap["packages"]

    if not isinstance(order, dict) or not order:
        raise ValueError(f"'{label}.tool_snapshot.order' must be a non-empty dictionary")
    if order.get("order_id") != order_id:
        raise ValueError(
            f"'{label}.tool_snapshot.order.order_id' mismatch: {order.get('order_id')!r} != {order_id!r}"
        )

    order_pkg_ids = _require_str_list(
        order.get("packages"), f"{label}.tool_snapshot.order.packages", allow_empty=False, unique=True
    )
    _require_non_empty_str(order.get("order_status"), f"{label}.tool_snapshot.order.order_status")

    if not isinstance(packages, dict) or not packages:
        raise ValueError(f"'{label}.tool_snapshot.packages' must be a non-empty dictionary")

    if set(packages.keys()) != set(order_pkg_ids):
        raise ValueError(
            f"'{label}.tool_snapshot' package set mismatch between order.packages and packages keys"
        )

    for pkg_id, pkg_rec in packages.items():
        if not isinstance(pkg_rec, dict):
            raise ValueError(f"'{label}.tool_snapshot.packages[{pkg_id!r}]' must be a dictionary")
        if pkg_rec.get("package_id") != pkg_id:
            raise ValueError(
                f"'{label}.tool_snapshot.packages[{pkg_id!r}].package_id' mismatch: {pkg_rec.get('package_id')!r}"
            )
        _require_non_empty_str(pkg_rec.get("status"), f"{label}.tool_snapshot.packages[{pkg_id!r}].status")
        _require_non_empty_str(pkg_rec.get("carrier"), f"{label}.tool_snapshot.packages[{pkg_id!r}].carrier")

    if failing_pkg_ids is not None:
        unknown_outage = sorted(set(failing_pkg_ids) - set(packages.keys()))
        if unknown_outage:
            raise ValueError(
                f"'{label}.failing_package_ids' references packages not in order snapshot: {unknown_outage}"
            )


def _validate_tasks(raw_tasks: Any, ep_trace_queries: dict[str, str]) -> tuple[set[str], set[str]]:
    if not isinstance(raw_tasks, list) or not raw_tasks:
        raise ValueError("'tasks' must be a non-empty list")

    task_ids: set[str] = set()
    task_roots: set[str] = set()
    task_lineage: set[str] = set()
    seen_kinds: set[str] = set()
    seen_queries: dict[str, str] = {}

    for idx, task in enumerate(raw_tasks):
        label = f"tasks[{idx}]"
        rec = _check_exact_keys(task, REQUIRED_TASK_KEYS, label, optional=OPTIONAL_TASK_KEYS)

        task_id = _require_non_empty_str(rec["task_id"], f"{label}.task_id")
        if task_id in task_ids:
            raise ValueError(f"Duplicate task_id: '{task_id}'")
        task_ids.add(task_id)

        source_id = _require_non_empty_str(rec["source_task_id"], f"{label}.source_task_id")
        root_family = _require_non_empty_str(rec["root_family"], f"{label}.root_family")
        derived = _require_str_list(rec["derived_from"], f"{label}.derived_from", allow_empty=True)

        kind = rec["kind"]
        if kind not in ("transfer", "boundary"):
            raise ValueError(f"Invalid task kind at {label}: {kind!r}")
        seen_kinds.add(kind)

        query = _require_non_empty_str(rec["query"], f"{label}.query")
        norm_query = _normalize_query(query)
        if norm_query in seen_queries:
            raise ValueError(
                f"Exact normalized query collision between tasks '{seen_queries[norm_query]}' and '{task_id}'"
            )
        if norm_query in ep_trace_queries:
            raise ValueError(
                f"Exact normalized query collision between task '{task_id}' and episode '{ep_trace_queries[norm_query]}'"
            )
        seen_queries[norm_query] = task_id

        order_id = _require_non_empty_str(rec["order_id"], f"{label}.order_id")

        if "intent_constraint" in rec and rec["intent_constraint"] is not None:
            if rec["intent_constraint"] != "STATUS_ONLY":
                raise ValueError(
                    f"Invalid intent_constraint at {label}: {rec['intent_constraint']!r} (only 'STATUS_ONLY' supported)"
                )

        failing_pkg_ids: list[str] | None = None
        if "failing_package_ids" in rec and rec["failing_package_ids"] is not None:
            failing_pkg_ids = _require_str_list(
                rec["failing_package_ids"], f"{label}.failing_package_ids", allow_empty=True, unique=True
            )

        _validate_tool_snapshot(rec["tool_snapshot"], order_id, failing_pkg_ids, label)
        scope = []
        if "scope_package_ids" in rec:
            scope = _require_str_list(rec["scope_package_ids"], f"{label}.scope_package_ids", allow_empty=False, unique=True)
            if not set(scope) <= set(rec["tool_snapshot"]["packages"]):
                raise ValueError(f"'{label}.scope_package_ids' references packages outside the order")
        requires_order_lookup = rec.get("requires_order_lookup", True)
        if not isinstance(requires_order_lookup, bool):
            raise ValueError(f"'{label}.requires_order_lookup' must be a boolean")
        if not requires_order_lookup and (len(scope) != 1 or scope[0] not in query):
            raise ValueError(f"'{label}' can skip order lookup only for a single package named in the request")

        task_roots.add(root_family)
        task_lineage.update((task_id, source_id, *derived))

    missing_kinds = sorted({"transfer", "boundary"} - seen_kinds)
    if missing_kinds:
        raise ValueError(f"Protocol tasks must include both 'transfer' and 'boundary'; missing: {missing_kinds}")

    return task_roots, task_lineage


def validate_protocol(protocol: dict[str, Any]) -> None:
    """Validate D7 protocol schema, PiTextClient config, budget, oracle digest, snapshots, and lineage isolation."""
    proto = _check_exact_keys(protocol, REQUIRED_TOP_LEVEL_KEYS, "protocol", optional=frozenset({"source"}))

    schema_version = proto["schema_version"]
    if isinstance(schema_version, bool) or not isinstance(schema_version, int) or schema_version != 1:
        raise ValueError(f"Unsupported schema_version: {schema_version!r} (expected 1)")

    _validate_model_config(proto["model_config"])
    _require_non_empty_str(proto["system_prompt_header"], "system_prompt_header")
    _validate_budget(proto["budget"])

    expected_oracle = _require_non_empty_str(proto["oracle_sha256"], "oracle_sha256")
    actual_oracle = get_logistics_oracle_sha256()
    if expected_oracle != actual_oracle:
        raise ValueError(f"Oracle source hash mismatch: expected {expected_oracle}, actual {actual_oracle}")
    if proto["grading_sha256"] != hashlib.sha256(Path(d7_oracle.__file__).read_bytes()).hexdigest():
        raise ValueError("D7 grading source hash mismatch")

    ep_ids, ep_roots, ep_lineage, ep_trace_queries = _validate_episodes(proto["episodes"])

    skill = _check_exact_keys(proto["skill"], REQUIRED_SKILL_KEYS, "skill")
    _require_non_empty_str(skill["name"], "skill.name")
    _require_non_empty_str(skill["version"], "skill.version")
    _require_non_empty_str(skill["body"], "skill.body")
    source_ep_ids = _require_str_list(skill["source_episode_ids"], "skill.source_episode_ids", allow_empty=False, unique=True)
    dangling = sorted(set(source_ep_ids) - ep_ids)
    if dangling:
        raise ValueError(f"Dangling skill.source_episode_ids not in frozen episodes corpus: {dangling}")

    task_roots, task_lineage = _validate_tasks(proto["tasks"], ep_trace_queries)

    root_overlap = sorted(ep_roots & task_roots)
    if root_overlap:
        raise ValueError(f"Cross-side root_family leakage between episodes and tasks: {root_overlap}")

    lineage_overlap = sorted(ep_lineage & task_lineage)
    if lineage_overlap:
        raise ValueError(f"Cross-side source/derived_from lineage leakage between episodes and tasks: {lineage_overlap}")

    cross_root_lineage = sorted((ep_roots & task_lineage) | (task_roots & ep_lineage))
    if cross_root_lineage:
        raise ValueError(f"Cross-side root/lineage identifier collision: {cross_root_lineage}")

    # Ensure the entire protocol is strictly JSON-serializable without NaN/Infinity.
    try:
        json.dumps(proto, ensure_ascii=False, allow_nan=False)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"Protocol must be strictly JSON-serializable without NaN/Infinity: {exc}") from exc


def freeze_protocol(directory: str | Path, protocol: dict[str, Any]) -> str:
    """Validate and atomically freeze protocol.json + protocol.sha256 into directory without overwriting."""
    dir_path = Path(directory)
    json_path = dir_path / PROTOCOL_FILENAME
    sha_path = dir_path / SHA256_FILENAME

    if json_path.exists() or json_path.is_symlink() or sha_path.exists() or sha_path.is_symlink():
        raise FileExistsError(f"Refusing to overwrite existing protocol files in {dir_path}")

    validate_protocol(protocol)

    raw_bytes = json.dumps(protocol, indent=2, ensure_ascii=False, allow_nan=False).encode("utf-8")
    digest = hashlib.sha256(raw_bytes).hexdigest()

    dir_path.mkdir(parents=True, exist_ok=True)
    if json_path.exists() or json_path.is_symlink() or sha_path.exists() or sha_path.is_symlink():
        raise FileExistsError(f"Refusing to overwrite existing protocol files in {dir_path}")

    token = uuid.uuid4().hex[:12]
    tmp_json = dir_path / f".{PROTOCOL_FILENAME}.{token}.tmp"
    tmp_sha = dir_path / f".{SHA256_FILENAME}.{token}.tmp"
    placed_json = False

    try:
        tmp_json.write_bytes(raw_bytes)
        tmp_sha.write_text(f"{digest}\n", encoding="utf-8")
        os.link(tmp_json, json_path)
        placed_json = True
        os.link(tmp_sha, sha_path)
    except BaseException:
        if placed_json:
            json_path.unlink(missing_ok=True)
        raise
    finally:
        tmp_json.unlink(missing_ok=True)
        tmp_sha.unlink(missing_ok=True)

    return digest


def load_protocol(directory: str | Path) -> tuple[dict[str, Any], str]:
    """Load protocol.json + protocol.sha256, verify raw-bytes SHA256, and re-validate schema."""
    dir_path = Path(directory)
    json_path = dir_path / PROTOCOL_FILENAME
    sha_path = dir_path / SHA256_FILENAME

    if not json_path.is_file():
        raise FileNotFoundError(f"Missing frozen protocol file: {json_path}")
    if not sha_path.is_file():
        raise FileNotFoundError(f"Missing frozen protocol sha256 file: {sha_path}")

    raw_bytes = json_path.read_bytes()
    actual_sha = hashlib.sha256(raw_bytes).hexdigest()
    expected_sha = sha_path.read_text(encoding="utf-8").strip()

    if actual_sha != expected_sha:
        raise ValueError(f"Protocol rawbytes hash verification failed: actual {actual_sha} != expected {expected_sha}")

    try:
        data = json.loads(raw_bytes.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"Invalid protocol JSON: {exc}") from exc

    validate_protocol(data)
    return data, actual_sha
