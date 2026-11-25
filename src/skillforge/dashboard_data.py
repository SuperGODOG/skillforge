"""Bounded, read-only projections of existing evidence; never initialize a Store."""
from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
import math
import os
from pathlib import Path
import re
import sqlite3
from typing import Any

MAX_FILES = 150
MAX_ROWS = 500
MAX_BYTES = 10 * 1024 * 1024
SECRET_KEYS = re.compile(r"^(api_?key|authorization|password|secret|access_token|credential_source|thinking|reasoning|reasoning_content)$", re.I)
SECRET_TEXT = re.compile(r"\bBearer\s+[^\s\"']+|\b(?:sk-|ark-)[a-zA-Z0-9_-]{12,}", re.I)
TABLES = {
    "runtime_runs": ("runs", "run_id"),
    "episodes": ("episodes", "episode_id"),
    "candidate_skills": ("candidates", "candidate_id"),
    "validation_records": ("validations", "record_id"),
    "releases": ("releases", "release_id"),
    "repair_jobs": ("repair_jobs", "job_id"),
}


def redact_sensitive(value: Any) -> Any:
    if isinstance(value, dict):
        return {str(k): redact_sensitive(v) for k, v in value.items() if not SECRET_KEYS.match(str(k))}
    if isinstance(value, list):
        return [redact_sensitive(v) for v in value]
    if isinstance(value, str):
        return SECRET_TEXT.sub("[REDACTED]", value)
    if isinstance(value, float) and not math.isfinite(value):
        return None
    if isinstance(value, (bytes, bytearray)):
        return "[binary omitted]"
    return value


def _decode(value: Any, fallback: Any = None) -> Any:
    if not isinstance(value, str):
        return value if value is not None else fallback
    try:
        return json.loads(value)
    except (ValueError, TypeError):
        return fallback


def _pick(row: dict, names: str) -> dict:
    return {key: row.get(key) for key in names.split()}


def _usage(raw: Any) -> dict | None:
    if not isinstance(raw, dict):
        return None
    result = {k: v for k, v in raw.items() if k in {
        "input", "output", "cacheRead", "cacheWrite", "totalTokens", "input_including_cache",
        "prompt_tokens", "completion_tokens", "total_tokens", "calls",
        "cache_creation_input_tokens", "cache_read_input_tokens",
    }}
    result["cost"] = None  # SDK price placeholders are not a billing receipt.
    return redact_sensitive(result)


def _tool(row: dict) -> dict:
    result = _pick(row, "call_id run_id tool_name status output_text error_type error_message latency_ms created_at")
    result["input_params"] = _decode(row.get("input_params_json", row.get("input_params")))
    result["output_data"] = _decode(row.get("output_data_json", row.get("output_data")))
    return result


def _group(raw: Any) -> dict:
    if not isinstance(raw, dict):
        return {"passed": None}
    passed = raw.get("passed", raw.get("pass"))
    if not isinstance(passed, bool):
        verdict = raw.get("verdict")
        passed = True if verdict == "PASS" else False if verdict in {"FAIL", "FAILED"} else None
    calls = raw.get("tool_calls_count", raw.get("tool_calls"))
    if isinstance(calls, list):
        calls = len(calls)
    return {
        "passed": passed,
        "output_text": raw.get("output_text", raw.get("model_output", raw.get("text"))),
        "classification": raw.get("classification"),
        "failure_reason": raw.get("failure_reason", raw.get("error")),
        "tool_calls": calls,
        "latency_ms": raw.get("latency_ms"),
        "usage": _usage(raw.get("usage")),
        "run_id": raw.get("run_id"),
        "selected_episode_ids": raw["selected_episode_ids"] if isinstance(raw.get("selected_episode_ids"), list) else None,
        "source_lookup_ids": raw["source_lookup_ids"] if isinstance(raw.get("source_lookup_ids"), list) else None,
    }


def _partition(name: str, raw: dict, extra_rows: list | None = None, lineage: Any = None, groups: str = "ABC") -> dict:
    rows: dict[str, dict] = {}
    aliases_by_group = {"A": ("A", "group_a"), "B": ("B", "group_b"),
                        "C": ("C", "group_c_fresh", "group_c"), "D": ("D",)}

    def ingest(items: list, force_group: str | None = None) -> None:
        for item in items[:MAX_ROWS]:
            if not isinstance(item, dict) or not item.get("task_id"):
                continue
            if str(item["task_id"]) not in rows and len(rows) >= MAX_ROWS:
                continue
            row = rows.setdefault(str(item["task_id"]), {
                "task_id": item["task_id"], "family": item.get("task_family") or item.get("family") or item.get("root_family"),
                "root_family": item.get("root_family"), "partition": name,
                "prompt": item.get("user_query", item.get("prompt", item.get("query"))),
                **{group: {"passed": None} for group in groups},
            })
            for group in groups:
                for alias in aliases_by_group[group]:
                    if alias in item:
                        row[group] = _group(item[alias])
                        break
            label = str(item.get("group", "")).upper()
            group = force_group or {"A_NO_SKILL": "A", "B_V1": "B", "C_V2": "C"}.get(label, label)
            if group and group in groups and len(group) == 1:
                row[group] = _group(item)

    ingest(raw.get("tasks", raw.get("rows", [])) or [])
    c_dev = raw.get("C_DEV", {})
    if isinstance(c_dev, dict):
        ingest(c_dev.get("tasks", []) or [], "C")
    ingest(extra_rows or [])
    declared = raw.get("sample_size", raw.get("total_tasks", len(rows)))
    declared = declared if isinstance(declared, int) and not isinstance(declared, bool) and declared >= 0 else len(rows)
    total = max(len(rows), declared)
    computed: dict[str, Any] = {}
    for group in groups:
        known = [r[group]["passed"] for r in rows.values() if isinstance(r[group]["passed"], bool)]
        computed[group] = {"passed": sum(known), "recorded": len(known), "total": total, "unknown": total - len(known)}
    pairs = [("B", "C"), ("C", "D")] if "D" in groups else [("B", "C")]
    for before, after in pairs:
        paired = {"improved": 0, "degraded": 0, "unchanged": 0, "unknown": total - len(rows)}
        for row in rows.values():
            previous, current = row[before]["passed"], row[after]["passed"]
            if previous is None or current is None:
                paired["unknown"] += 1
            elif previous == current:
                paired["unchanged"] += 1
            else:
                paired["improved" if current else "degraded"] += 1
        computed[f"paired_{before.lower()}_to_{after.lower()}"] = paired
    summary = raw.get("summary") or {k: v for k, v in raw.items() if k not in {"tasks", "rows", "task_families", "C_DEV"}}
    if c_dev:
        summary = {"reported": summary, "C_DEV": {k: v for k, v in c_dev.items() if k != "tasks"}}
    return {
        "name": name, "task_families": raw.get("task_families") or list(dict.fromkeys(r["family"] for r in rows.values() if r["family"])),
        "reported_summary": summary, "rows": list(rows.values()), "computed_summary": computed,
        "lineage": lineage,
    }


class _Reader:
    def __init__(self, root: Path):
        self.root = root
        self.data = {"schema_version": 1, "generated_at": datetime.now(timezone.utc).isoformat(), "project_name": root.name}
        self.data.update({key: [] for key in ["sources", "runs", "episodes", "candidates", "validations", "releases", "repair_jobs", "benchmarks", "warnings"]})
        self.run_index: dict[tuple[str, str], dict] = {}
        self.episode_index: dict[tuple[str, str], dict] = {}
        self.superseded: set[tuple[str, str]] = set()
        self.benchmark_reviews: dict[tuple[str, str], dict] = {}

    def warn(self, message: str) -> None:
        self.data["warnings"].append(message)

    def entity(self, source: dict, row: dict) -> dict:
        return {**row, "source_id": source["id"], "scope": source["scope"]}

    def run(self, source: dict, row: dict) -> None:
        if not row.get("run_id"):
            return
        key = (source["scope"], row["run_id"])
        prior = self.run_index.get(key)
        row = self.entity(source, row)
        if prior is None:
            row["source_ids"] = [source["id"]]
            self.run_index[key] = row
            self.data["runs"].append(row)
        else:
            refs = list(dict.fromkeys(prior["source_ids"] + [source["id"]]))
            calls = {t.get("call_id", str(i)): t for i, t in enumerate(prior.get("tool_records", []))}
            calls.update({t.get("call_id", str(i)): t for i, t in enumerate(row.get("tool_records", []))})
            prior.update({k: v for k, v in row.items() if v is not None and v != []})
            prior["tool_records"] = list(calls.values())
            prior["source_ids"] = refs

    def episode(self, source: dict, row: dict) -> None:
        if not row.get("episode_id"):
            return
        key = (source["scope"], row["episode_id"])
        if key not in self.episode_index:
            item = self.entity(source, row)
            self.episode_index[key] = item
            self.data["episodes"].append(item)

    def db_rows(self, conn: sqlite3.Connection, table: str, source: dict, fields: str | None = None) -> list[dict]:
        # All identifiers come from the fixed internal table list, never HTTP parameters.
        try:
            if not conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)).fetchone():
                return []
            projection = "*"
            if fields:
                available = {r[1] for r in conn.execute(f'PRAGMA table_info("{table}")')}
                allowed = [name for name in fields.split() if name in available]
                if not allowed:
                    return []
                projection = ", ".join(f'"{name}"' for name in allowed)
            rows = conn.execute(f'SELECT {projection} FROM "{table}" LIMIT {MAX_ROWS + 1}').fetchall()
            if len(rows) > MAX_ROWS:
                self.warn(f"{source['path']} / {table} 超过 {MAX_ROWS} 条，已截断")
            return [dict(r) for r in rows[:MAX_ROWS]]
        except sqlite3.Error:
            self.warn(f"{source['path']} / {table} 读取失败或旧库不兼容")
            return []

    def database(self, path: Path, source: dict) -> None:
        conn = sqlite3.connect(path.as_uri() + "?mode=ro", uri=True, timeout=0.5)
        try:
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA query_only=ON")
            tools: dict[str, list] = {}
            for raw in self.db_rows(conn, "runtime_tool_calls", source):
                tools.setdefault(raw.get("run_id"), []).append(_tool(raw))
            checkpoints: dict[str, list] = {}
            for raw in self.db_rows(conn, "runtime_pi_checkpoints", source, "checkpoint_id run_id epoch intent_version created_at"):
                checkpoints.setdefault(raw.get("run_id"), []).append(_pick(raw, "checkpoint_id run_id epoch intent_version created_at"))
            for table, (bucket, primary_key) in TABLES.items():
                for raw in self.db_rows(conn, table, source):
                    if not raw.get(primary_key):
                        self.warn(f"{source['path']} / {table} 缺少主键，跳过记录")
                        continue
                    if table == "runtime_runs":
                        row = _pick(raw, "run_id task_id skill_name skill_version candidate_id status purpose budget_max budget_consumed deadline_ts intent_revision content_hash frozen_body created_at error_type error_message")
                        row.update(tool_records=tools.get(raw["run_id"], []), checkpoints=checkpoints.get(raw["run_id"], []), record_kind="database")
                        self.run(source, row)
                        continue
                    if table == "episodes":
                        row = _pick(raw, "episode_id task_id run_id skill_name skill_version outcome outcome_reason created_at")
                        env = _decode(raw.get("environment_json"), {})
                        row.update(purpose=env.get("purpose") if isinstance(env, dict) else None, oracle=_decode(raw.get("verification_json")), record_kind="database")
                        row["reuse"] = _pick(env, "enable_reuse retrieval_hit selected_skill selected_version") if isinstance(env, dict) else {}
                        self.episode(source, row)
                        continue
                    if table == "candidate_skills":
                        row = _pick(raw, "candidate_id skill_name status decision source_type source_requirement rationale created_at intent_revision")
                        row.update(source_episode_ids=_decode(raw.get("source_episode_ids"), []), parent_candidate_id=raw.get("supersedes"), body=raw.get("body_md"), content_hash=raw.get("content_hash"), meta=_decode(raw.get("meta_json")), record_kind="database")
                    elif table == "validation_records":
                        row = _pick(raw, "record_id candidate_id content_hash baseline_version ratchet_decision promoted release_id scope_hash config_hash dataset_version created_at updated_at")
                        row.update(eval_result=_decode(raw.get("eval_result_json")), ratchet_verdict=_decode(raw.get("ratchet_verdict_json")), verification_eids=_decode(raw.get("verification_eids_json"), []))
                    elif table == "releases":
                        row = _pick(raw, "release_id skill_name version status level triggered_by content_hash body_md created_at published_at")
                        row.update(source_lineage=_decode(raw.get("source_lineage_json")), eval_summary=_decode(raw.get("eval_summary_json")))
                    else:
                        row = _pick(raw, "job_id skill_name baseline_version responsibility_layer strategy status max_attempts current_attempt latest_candidate_id latest_content_hash stop_reason release_id created_at updated_at")
                        row.update(source_episode_ids=_decode(raw.get("source_episode_ids"), []), attempts=_decode(raw.get("attempts_json")), diagnosis=_decode(raw.get("diagnosis_json")))
                    self.data[bucket].append(self.entity(source, row))
        finally:
            conn.close()

    def report_run(self, source: dict, raw: dict, parent: dict) -> None:
        if not raw.get("run_id"):
            return
        row = _pick(raw, "run_id task_id skill_name skill_version candidate_id purpose deadline_ts intent_revision content_hash frozen_body error_type error_message created_at")
        budget = raw.get("budget") or {}
        row.update({
            "status": raw.get("run_status", raw.get("status")),
            "response_text": raw.get("response_text", raw.get("text")),
            "budget_max": raw.get("budget_max", budget.get("budget_max")),
            "budget_consumed": raw.get("budget_consumed", budget.get("budget_consumed")),
            "content_hash": raw.get("content_hash", raw.get("frozen_body_hash")),
            "tool_records": [_tool(t) for t in raw.get("tool_records", []) if isinstance(t, dict)],
            "oracle": raw.get("oracle"), "model": raw.get("model", parent.get("model")),
            "evidence": raw.get("evidence", raw.get("evidence_labels", parent.get("evidence"))),
            "real_llm": raw.get("real_llm"), "usage": _usage(raw.get("usage")), "record_kind": "report",
        })
        self.run(source, row)
        episode = raw.get("episode") or {}
        episode_id = raw.get("episode_id", episode.get("episode_id"))
        if episode_id:
            self.episode(source, {"episode_id": episode_id, "run_id": raw["run_id"], "task_id": raw.get("task_id"), "purpose": raw.get("purpose", episode.get("purpose")), "outcome": raw.get("episode_outcome", episode.get("outcome")), "oracle": raw.get("oracle"), "record_kind": "report"})

    def real_benchmark(self, source: dict, raw: dict) -> None:
        meta = raw.get("metadata", {})
        labels = meta.get("group_labels")
        four_groups = isinstance(labels, dict) and set(labels) == set("ABCD")
        groups = "ABCD" if four_groups else "ABC"
        benchmark = self.entity(source, {
            "id": source["id"] + ":real", "name": meta.get("experiment_name") or "P6 · 真实模型 A/B/C", "mode": "real_model",
            "status": "历史实验记录 · 明细完整度单列", "model": meta.get("resolved_model", meta.get("requested_model")),
            "evidence": _pick(meta, "evidence_tier os_sandbox_status protocol"),
            "notes": ["历史模型实验，业务数据为 synthetic；本看板不重新运行评测。", "没有明细的组保留未知；SDK 占位费用不作为实际账单。"],
            "partitions": [],
        })
        if four_groups:
            benchmark["group_labels"] = {group: str(labels[group]) for group in groups}
            benchmark["status"] = raw.get("status") or benchmark["status"]
            benchmark["notes"] = ["D7 对照使用合成履约任务。历史 Episode 和 Skill 来自 D4 隔离演示。"]
        for name, part in raw.get("partitions", {}).items():
            if isinstance(part, dict):
                lineage = {"task_families": part.get("task_families"), "zero_family_overlap_verified_reported": part.get("zero_family_overlap_verified"), "evidence_note": "保留原报告声明，不把当前导入当作修订前冻结的证明。"}
                if four_groups:
                    lineage = {"protocol_hash": meta.get("protocol_hash"), "historical_source": meta.get("source")}
                partition = _partition(name, part, lineage=lineage, groups=groups)
                benchmark["partitions"].append(partition)
                for group in groups:
                    missing = partition["computed_summary"][group]["unknown"]
                    if missing:
                        self.warn(f"{source['path']} / {name} / {group} 缺少 {missing} 项逐任务判定，保留未知")
        self.data["benchmarks"].append(benchmark)
        for label, skill in raw.get("skills", {}).items():
            if not isinstance(skill, dict) or not skill.get("candidate_id"):
                continue
            if any(c["scope"] == source["scope"] and c["candidate_id"] == skill["candidate_id"] and c.get("record_kind") == "database" for c in self.data["candidates"]):
                continue
            body = skill.get("body", skill.get("body_preview"))
            name_match = re.search(r"(?m)^name:\s*([^\n]+)", body or "")
            item = _pick(skill, "candidate_id parent_candidate_id content_hash source_type source_requirement version rationale repaired_from_dev_failures")
            item.update(skill_name=skill.get("skill_name") or (name_match.group(1).strip() if name_match else label), status="RECORDED", decision="revise" if skill.get("parent_candidate_id") else "create", source_episode_ids=skill.get("source_episode_ids", []), body=body, body_is_preview="body" not in skill, record_kind="experiment_summary", note="实验报告版本摘要；不代替当前 CandidateStore、验证记录或正式发布凭据。")
            self.data["candidates"].append(self.entity(source, item))

    def scripted_benchmarks(self, source: dict, raw: dict) -> None:
        definitions = {
            "prospective_family_isolated_benchmark": ("P6 · 前瞻性 scripted 对照", "scripted"),
            "post_hoc_scripted_challenge_benchmark": ("P6 · 事后挑战", "challenge"),
            "exploratory_posthoc_repartition_benchmark": ("P6 · 事后重分区探索", "exploratory"),
            "exploratory_isomorphic_benchmark": ("P6 · 同构样本探索", "exploratory"),
        }
        for key, (name, mode) in definitions.items():
            b = raw.get(key)
            if not isinstance(b, dict):
                continue
            grouped: dict[str, list] = {}
            tasks_raw = b.get("raw_task_results", [])
            if len(tasks_raw) > MAX_ROWS:
                self.warn(f"{source['path']} / {key} 超过 {MAX_ROWS} 条任务，已截断")
            for task in tasks_raw[:MAX_ROWS]:
                if isinstance(task, dict):
                    grouped.setdefault(task.get("split", "UNSPECIFIED"), []).append(task)
            partitions = []
            counts = b.get("split_counts", {})
            for partition_name, tasks in grouped.items():
                summary = b.get("dev_summary" if partition_name == "DEV" else "locked_eval_summary", {})
                count = counts.get(partition_name, len(tasks)) if isinstance(counts, dict) else len(tasks)
                partitions.append(_partition(partition_name, {"tasks": tasks, "sample_size": count, "summary": summary}, lineage=b.get("pre_registration_metadata", b.get("partition_criteria"))))
            self.data["benchmarks"].append(self.entity(source, {
                "id": source["id"] + ":" + key, "name": name, "mode": mode,
                "status": b.get("benchmark_status"), "model": "scripted / behavioral simulation",
                "notes": [b.get("status_note") or "受控业务夹具；不等同真实模型效果。"],
                "partitions": partitions, "evidence": {"model": "scripted", "data": "synthetic"},
            }))

    def read(self, path: Path) -> None:
        relative = path.relative_to(self.root).as_posix()
        source = {"id": hashlib.sha256(relative.encode()).hexdigest()[:16], "path": relative, "scope": path.parent.relative_to(self.root).as_posix(), "kind": "sqlite" if path.suffix in {".db", ".sqlite", ".sqlite3"} else "json", "status": "OK"}
        self.data["sources"].append(source)
        try:
            if source["kind"] == "sqlite":
                self.database(path, source)
                return
            if path.stat().st_size > MAX_BYTES:
                raise ValueError("文件超过大小上限")
            raw = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(raw, dict):
                raise ValueError("不支持的报告结构")
            self.report_run(source, raw, raw)
            cases = raw.get("cases", [])
            if len(cases) > MAX_ROWS:
                self.warn(f"{relative} 超过 {MAX_ROWS} 条运行，已截断")
            for case in cases[:MAX_ROWS]:
                if isinstance(case, dict):
                    self.report_run(source, case, raw)
            if isinstance(raw.get("partitions"), dict) and isinstance(raw.get("metadata"), dict):
                self.real_benchmark(source, raw)
            self.scripted_benchmarks(source, raw)
            review = raw.get("benchmark_review")
            if path.name == "acceptance_review.json" and isinstance(review, dict):
                # Reviews annotate an exact report in this scope, never replace raw scores.
                target = review.get("source_report")
                if target == "report.json" and isinstance(review.get("findings"), list):
                    self.benchmark_reviews[(source["scope"], target)] = review
            superseded = raw.get("superseded_fault_sample", {})
            if isinstance(superseded, dict) and superseded.get("run_id") and superseded.get("source"):
                p = Path(superseded["source"])
                if not p.is_absolute():
                    p = self.root / p
                try:
                    scope = p.parent.resolve().relative_to(self.root).as_posix()
                    self.superseded.add((scope, superseded["run_id"]))
                except ValueError:
                    pass
        except (OSError, ValueError, TypeError, sqlite3.Error, AttributeError) as exc:
            source.update(status="ERROR", error=type(exc).__name__)
            self.warn(f"{relative} 读取失败（{type(exc).__name__}），其余来源继续展示")


def build_snapshot(root: Path, data_dirs: list[Path] | None = None) -> dict:
    root = Path(root).resolve(strict=True)
    if not root.is_dir():
        raise ValueError("root must be an existing directory")
    reader = _Reader(root)
    directories = data_dirs if data_dirs is not None else [root / "runs", root / "scratch"]
    candidates: set[Path] = set()
    for directory in directories:
        if len(candidates) >= MAX_FILES - 2:
            break
        original = Path(directory) if Path(directory).is_absolute() else root / directory
        resolved = original.resolve()
        if not resolved.is_relative_to(root):
            raise ValueError("Data directory must stay inside project root")
        if any(part == ".." for part in original.relative_to(root).parts):
            raise ValueError("Parent traversal is not allowed")
        if any(root.joinpath(*original.relative_to(root).parts[:i]).is_symlink() for i in range(1, len(original.relative_to(root).parts) + 1)):
            raise ValueError("Symlink data directory is not allowed")
        if not resolved.exists():
            if data_dirs is not None:
                raise ValueError("Data directory does not exist")
            continue
        if not resolved.is_dir():
            raise ValueError("Data root must be a directory")
        for current, dirs, names in os.walk(resolved, followlinks=False):
            here = Path(current)
            depth = len(here.relative_to(resolved).parts)
            if depth >= 5 and dirs:
                reader.warn(f"{here.relative_to(root)} 达到扫描深度 5，未扫描更深目录")
            dirs[:] = sorted(d for d in dirs if d not in {".git", ".venv", "node_modules", "__pycache__", "dashboard-acceptance"} and not (here / d).is_symlink()) if depth < 5 else []
            for name in sorted(names):
                p = here / name
                if p.is_symlink() or not p.resolve().is_relative_to(root):
                    continue
                if p.suffix in {".db", ".sqlite", ".sqlite3"} or name in {"report.json", "acceptance_review.json"} or (name.startswith("case_") and name.endswith(".json")):
                    candidates.add(p)
                if len(candidates) >= MAX_FILES - 2:
                    reader.warn(f"扫描达到 {MAX_FILES} 个文件上限，部分记录未载入")
                    break
            if len(candidates) >= MAX_FILES - 2:
                break
    fixed = [root / "docs" / name for name in ["p6_logistics_abc_raw_results.json", "p6_real_model_abc_raw_results.json"]]
    for path in fixed:
        if path.is_file() and not path.is_symlink() and path.resolve().is_relative_to(root):
            candidates.add(path)
    # DB first, rich JSON second, acceptance review last within its scope.
    for path in sorted(candidates, key=lambda p: (p.suffix == ".json", p.name == "acceptance_review.json", str(p)))[:MAX_FILES]:
        reader.read(path)
    source_paths = {s["id"]: s["path"] for s in reader.data["sources"]}
    for benchmark in reader.data["benchmarks"]:
        name = Path(source_paths[benchmark["source_id"]]).name
        review = reader.benchmark_reviews.get((benchmark["scope"], name))
        if review:
            benchmark["review"] = review
            benchmark["status"] = review.get("status") or benchmark["status"]
            benchmark["notes"].extend(str(item) for item in review["findings"])
    for run in reader.data["runs"]:
        if (run["scope"], run["run_id"]) in reader.superseded:
            run["superseded"] = True
            run["note"] = "此故障样本已被复核报告标为替代记录；保留原始结果，不计作最终补测。"
        episodes = [e for e in reader.data["episodes"] if e["scope"] == run["scope"] and e.get("run_id") == run["run_id"]]
        if not run.get("oracle") and episodes:
            run["oracle"] = episodes[0].get("oracle")
    return redact_sensitive(reader.data)
