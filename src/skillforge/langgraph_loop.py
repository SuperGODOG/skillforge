"""LangGraph StateGraph 受控回环旁路 Shadow 版 (P2-D)

面试技术展示与技术演进储备：
用 LangGraph StateGraph 表达 evolve 受控回环状态机，主链 for 循环不动，
通过旁路双跑验证行为与终态严格等价。

架构特性：
1. 节点复用：失败分析、候选生成、验证、防线裁决、rounds 状态机 100% 复用 evolver.py 实现函数。
2. 边分支语义：成功发布、需修下一轮、熔断停止、预算耗尽停止，逐一对齐 for 版。
3. 状态黑板：EvolveContext / AttemptRecord / AttemptFeedback + ledger 记账。
4. Durable 检查点：提供 InMemory Checkpointer 与 SQLite Durable Checkpointer（重启后可断点续跑）。
"""
from __future__ import annotations

import json
import copy
import hashlib
import os
import pickle
import shutil
import sqlite3
import tempfile
import threading
import time
import uuid
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, List, Literal, Optional, Set, Tuple, TypedDict, Union

from langgraph.graph import StateGraph, START, END
from langgraph.types import RunnableConfig
from langgraph.checkpoint.memory import MemorySaver
from langgraph.checkpoint.serde.jsonplus import JsonPlusSerializer

from .models import (
    SkillMeta,
    Trigger,
    Evaluation,
    RouteResult,
    EvalResult,
    RatchetVerdict,
    Patch,
    BudgetExceededError,
    EvolveBudget,
    EvolveContext,
    EvolveRecord,
    AttemptRecord,
    AttemptFeedback,
    BodySectionStats,
    ToolCallProvenance,
)
from .diff import compute_semantic_diff, _parse_skill_document
from .evaluator.prompt_bloat import compute_body_section_stats
from .evaluator.root_cause_prompts import extract_relevant_body_sections
from . import evolver as evolver_mod
from .evolver import (
    SkillEvolver,
    Failure,
    RootCause,
    EvolveOutcome,
    _collect_failures,
    _analyze_root_cause,
    _reconstruct_skill_md,
    compute_skill_fingerprint,
    is_forbidden_eval_set,
    is_candidate_eval_invalid,
    _is_judge_infrastructure_error,
    _is_effective_failure,
    _build_tool_trace_from_eval_result,
    _load_dataset_layer_ids,
)

# Dynamic delegation to evolver module so unittest.mock.patch('skillforge.evolver.*') intercepts both for and graph
def is_dependency_issue(*args, **kwargs):
    return evolver_mod.is_dependency_issue(*args, **kwargs)

def _archive_dependency_diagnostic(*args, **kwargs):
    return evolver_mod._archive_dependency_diagnostic(*args, **kwargs)

def _archive_isolation_diagnostic(*args, **kwargs):
    return evolver_mod._archive_isolation_diagnostic(*args, **kwargs)

def _generate_patches(*args, **kwargs):
    return evolver_mod._generate_patches(*args, **kwargs)

def _validate_patch(*args, **kwargs):
    return evolver_mod._validate_patch(*args, **kwargs)

def _publish_patch(*args, **kwargs):
    return evolver_mod._publish_patch(*args, **kwargs)

def _select_reflection_candidate(*args, **kwargs):
    return evolver_mod._select_reflection_candidate(*args, **kwargs)

def _build_attempt_feedback(*args, **kwargs):
    return evolver_mod._build_attempt_feedback(*args, **kwargs)

def assert_reflection_isolation(*args, **kwargs):
    return evolver_mod.assert_reflection_isolation(*args, **kwargs)

def _generate_reflection_patch(*args, **kwargs):
    return evolver_mod._generate_reflection_patch(*args, **kwargs)

def _archive_failure(*args, **kwargs):
    return evolver_mod._archive_failure(*args, **kwargs)


ALLOWED_MSGPACK_MODULES = [
    ('skillforge.models', 'EvolveContext'),
    ('skillforge.models', 'AttemptRecord'),
    ('skillforge.models', 'AttemptFeedback'),
    ('skillforge.models', 'EvolveBudget'),
    ('skillforge.models', 'Patch'),
    ('skillforge.models', 'EvalResult'),
    ('skillforge.models', 'RatchetVerdict'),
    ('skillforge.models', 'SkillMeta'),
    ('skillforge.models', 'BodySectionStats'),
    ('skillforge.models', 'EvolveRecord'),
    ('skillforge.models', 'Trigger'),
    ('skillforge.models', 'Evaluation'),
    ('skillforge.models', 'RouteResult'),
    ('skillforge.models', 'ToolCallProvenance'),
    ('skillforge.evolver', 'Failure'),
    ('skillforge.evolver', 'RootCause'),
    ('skillforge.evolver', 'EvolveOutcome'),
]


def create_jsonplus_serde() -> JsonPlusSerializer:
    """创建已显式注册 SkillForge 数据类型的 JsonPlusSerializer，消除反序列化拦截。"""
    return JsonPlusSerializer(pickle_fallback=True, allowed_msgpack_modules=ALLOWED_MSGPACK_MODULES)


def to_plain_dict(d: Any) -> Any:
    """递归将 defaultdict 转换为普通 dict，便于无障碍序列化。"""
    if isinstance(d, (defaultdict, dict)):
        return {k: to_plain_dict(v) for k, v in d.items()}
    return d


class SqliteCheckpointer(MemorySaver):
    """基于 SQLite 的事务级 Durable Checkpointer。

    ``MemorySaver`` 的内存字典是 LangGraph 的运行时索引，SQLite 只保存一
    个完整快照。多个进程各自持有旧快照时，写入会在 ``BEGIN IMMEDIATE``
    事务中先读最新值再合并，避免 cp2 把 cp1 新建的 thread 静默覆盖掉。
    损坏快照不再被吞掉：恢复失败必须显式抛出，调用方才能 fail-closed。
    """

    _PROCESS_LOCK = threading.RLock()

    def __init__(self, db_path: Union[str, Path], serde: Optional[JsonPlusSerializer] = None) -> None:
        effective_serde = serde or create_jsonplus_serde()
        super().__init__(serde=effective_serde)
        self.db_path = str(db_path)
        self._lock = threading.RLock()
        self._init_db()
        self._load_from_db()

    def _init_db(self) -> None:
        Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)
        with self._PROCESS_LOCK, self._lock, sqlite3.connect(self.db_path, timeout=30.0) as conn:
            conn.execute('PRAGMA busy_timeout=30000')
            conn.execute('PRAGMA journal_mode=WAL')
            conn.execute(
                'CREATE TABLE IF NOT EXISTS langgraph_checkpoints (key TEXT PRIMARY KEY, val BLOB)'
            )
            conn.commit()

    @staticmethod
    def _decode_payload(payload: Any) -> dict[str, Any]:
        try:
            data = pickle.loads(bytes(payload))
        except Exception as exc:
            raise RuntimeError(f'CHECKPOINT_RESTORE_FAILED: sqlite payload decode failed: {exc}') from exc
        if not isinstance(data, dict) or not all(k in data for k in ('storage', 'blobs', 'writes')):
            raise RuntimeError('CHECKPOINT_RESTORE_FAILED: sqlite payload schema is invalid')
        if not all(isinstance(data[k], dict) for k in ('storage', 'blobs', 'writes')):
            raise RuntimeError('CHECKPOINT_RESTORE_FAILED: sqlite payload fields are not mappings')
        return data

    @staticmethod
    def _merge_maps(base: Any, incoming: Any) -> Any:
        if not isinstance(base, dict) or not isinstance(incoming, dict):
            return incoming
        merged = dict(base)
        for key, value in incoming.items():
            if key in merged and isinstance(merged[key], dict) and isinstance(value, dict):
                merged[key] = SqliteCheckpointer._merge_maps(merged[key], value)
            else:
                merged[key] = value
        return merged

    def _snapshot_memory(self) -> dict[str, Any]:
        return {
            'storage': to_plain_dict(self.storage),
            'blobs': dict(self.blobs),
            'writes': to_plain_dict(self.writes),
        }

    def _restore_memory(self, data: dict[str, Any]) -> None:
        self.storage.clear()
        for thread_id, namespaces in data.get('storage', {}).items():
            if not isinstance(namespaces, dict):
                raise RuntimeError('CHECKPOINT_RESTORE_FAILED: storage namespace is invalid')
            for namespace, checkpoints in namespaces.items():
                if not isinstance(checkpoints, dict):
                    raise RuntimeError('CHECKPOINT_RESTORE_FAILED: checkpoint index is invalid')
                self.storage[thread_id][namespace].update(checkpoints)
        self.blobs.clear()
        self.blobs.update(data.get('blobs', {}))
        self.writes.clear()
        for key, writes in data.get('writes', {}).items():
            if not isinstance(writes, dict):
                raise RuntimeError('CHECKPOINT_RESTORE_FAILED: writes index is invalid')
            self.writes[key].update(writes)

    @classmethod
    def _read_payload(cls, conn: sqlite3.Connection) -> Optional[dict[str, Any]]:
        cur = conn.execute('SELECT val FROM langgraph_checkpoints WHERE key = ?', ('state',))
        row = cur.fetchone()
        return cls._decode_payload(row[0]) if row else None

    def _load_from_db(self) -> None:
        with self._PROCESS_LOCK, self._lock, sqlite3.connect(self.db_path, timeout=30.0) as conn:
            conn.execute('PRAGMA busy_timeout=30000')
            data = self._read_payload(conn)
            if data is not None:
                self._restore_memory(data)

    def _refresh_from_db(self) -> None:
        """Refresh reads so a long-lived process observes another process's commit."""
        self._load_from_db()

    def _save_to_db(self) -> None:
        with self._PROCESS_LOCK, self._lock, sqlite3.connect(self.db_path, timeout=30.0) as conn:
            local_data = self._snapshot_memory()
            conn.execute('PRAGMA busy_timeout=30000')
            conn.execute('BEGIN IMMEDIATE')
            current_data = self._read_payload(conn)
            # Merge at the namespace/checkpoint-map level.  This preserves
            # writes from stale checkpointer instances on different threads;
            # same-key writes are serialized by SQLite and the latest writer
            # is authoritative for that exact checkpoint key.
            merged = self._merge_maps(current_data or {'storage': {}, 'blobs': {}, 'writes': {}}, local_data)
            payload = pickle.dumps(merged)
            conn.execute(
                'INSERT OR REPLACE INTO langgraph_checkpoints (key, val) VALUES (?, ?)',
                ('state', payload),
            )
            conn.commit()
            self._restore_memory(merged)

    def get_tuple(self, config: Any) -> Any:
        self._refresh_from_db()
        return super().get_tuple(config)

    def list(self, *args, **kwargs):
        self._refresh_from_db()
        return super().list(*args, **kwargs)

    def put(self, config: Any, checkpoint: Any, metadata: Any, new_versions: Any) -> Any:
        with self._lock:
            res = super().put(config, checkpoint, metadata, new_versions)
        self._save_to_db()
        return res

    def put_writes(self, config: Any, writes: Any, task_id: Any, task_path: str = '') -> None:
        with self._lock:
            super().put_writes(config, writes, task_id, task_path)
        self._save_to_db()


def create_default_checkpointer(db_path: Optional[Union[str, Path]] = None) -> MemorySaver:
    """根据是否提供 db_path 创建 SQLite Durable Checkpointer 或 In-Memory MemorySaver。"""
    serde = create_jsonplus_serde()
    if db_path:
        return SqliteCheckpointer(db_path=db_path, serde=serde)
    return MemorySaver(serde=serde)


class EvolveLoopState(TypedDict, total=False):
    skill_name: str
    max_candidates: int
    eval_set_for_iter: str
    verbose: bool
    budget: Optional[EvolveBudget]
    enable_reflection: Optional[bool]
    shadow_mode: Optional[bool]
    auto_publish_enabled: Optional[bool]
    enable_a2: Optional[bool]
    run_id: Optional[str]
    trace_file: Optional[str]
    shadow_root: Optional[str]
    shadow_paths: dict[str, str]
    graph_shadow_only: bool
    resume: bool

    active_budget: Optional[EvolveBudget]
    effective_run_id: str
    effective_trace_file: str
    eff_enable_reflection: bool
    eff_shadow_mode: bool
    eff_auto_publish: bool
    eff_enable_a2: bool
    candidate_cap: Optional[int]

    outcome: Optional[EvolveOutcome]
    context: Optional[EvolveContext]
    old_result: Optional[EvalResult]
    skipped_cases: list[dict[str, Any]]
    effective_failed_cases: set[str]
    failures: list[Failure]
    meta: Optional[SkillMeta]
    body: str
    baseline_stats: dict[str, int]
    original_skill_md: str
    original_digest: str
    root_causes: list[RootCause]

    round_no: int
    has_acceptable: bool
    declined_evaluations: list[tuple[Patch, EvalResult, RatchetVerdict, float]]
    pending_patches: list[Patch]
    current_patch: Optional[Patch]
    current_eval_result: Optional[EvalResult]
    current_verdict: Optional[RatchetVerdict]
    current_cand_fp: str
    current_attempt_feedback: Optional[AttemptFeedback]

    validation_status: str
    last_status: Optional[str]
    action: str
    stop_reason: Optional[str]
    transition_history: list[dict[str, Any]]


def _is_relative_to(path: Path, parent: Path) -> bool:
    try:
        path.relative_to(parent)
        return True
    except ValueError:
        return False


def _prepare_shadow_root(
    evolver: SkillEvolver,
    requested_root: Optional[Union[str, Path]],
) -> tuple[Path, dict[str, str]]:
    """Create the graph's mandatory sidecar root.

    A graph run is never allowed to use ``evolver.repo_root`` as an implicit
    output root.  An explicit root must be outside the live repository; an
    omitted root is a persistent temp directory so traces remain inspectable.
    Read-only evaluation/skill inputs are copied only when the shadow side
    does not already contain them (important for checkpoint resume).
    """
    live_root = Path(
        getattr(evolver, 'live_repo_root', getattr(evolver, 'repo_root', Path.cwd()))
    ).resolve()
    if requested_root is None:
        root = Path(tempfile.mkdtemp(prefix='skillforge-langgraph-shadow-')).resolve()
    else:
        root = Path(requested_root).expanduser().resolve()
        if root == live_root or _is_relative_to(root, live_root):
            raise ValueError(
                f'SHADOW_ROOT_MUST_BE_ISOLATED: {root} is inside live repo {live_root}'
            )
        root.mkdir(parents=True, exist_ok=True)

    runs = root / 'runs'
    paths = {
        'root': str(root),
        'traces': str(runs / 'eval_traces'),
        'failures': str(runs / 'failures'),
        'suggestions': str(runs / 'suggestions'),
        'router': str(runs / 'router.jsonl'),
        'db': str(runs / 'skillforge.db'),
        'skills': str(root / 'skills'),
    }
    for directory in (runs, Path(paths['traces']), Path(paths['failures']), Path(paths['suggestions']), root / 'router'):
        directory.mkdir(parents=True, exist_ok=True)

    # Final-audit and sandbox helpers read these assets by repo-relative path.
    # Copying is isolated and only initializes a new shadow root; it never
    # writes into the live chain.
    for dirname in ('evaluation_sets', 'skills'):
        source = live_root / dirname
        target = root / dirname
        if source.exists() and not target.exists():
            shutil.copytree(source, target)
    return root, paths


def _make_shadow_evolver(
    source: SkillEvolver,
    shadow_root: Path,
    shadow_paths: dict[str, str],
) -> SkillEvolver:
    """Clone runtime wiring while preserving the source LLM/ledger object.

    The graph must not mutate the caller's registry, evaluator cache, router
    log, or release DB.  A shallow object clone keeps the exact SkillEvolver
    operation methods/LLM while redirecting every mutable I/O owner to the
    prepared root.  The durable ledger itself is intentionally shared so the
    caller sees the same accounting object after the run.
    """
    runtime = copy.copy(source)
    runtime.live_repo_root = Path(getattr(source, 'repo_root', Path.cwd())).resolve()
    runtime.repo_root = shadow_root
    runtime.state_machine = None
    runtime.shadow_root = shadow_root

    try:
        shadow_registry = copy.copy(source.registry)
        shadow_registry.repo_root = shadow_root
        shadow_registry.skills_dir = shadow_root / 'skills'
        shadow_registry.db_path = Path(shadow_paths['db'])
        shadow_registry.router_log = Path(shadow_paths['router'])
        if hasattr(shadow_registry, '_sm'):
            shadow_registry._sm = None
        runtime.registry = shadow_registry
    except Exception as exc:
        raise RuntimeError(f'SHADOW_REGISTRY_INIT_FAILED: {exc}') from exc

    if source.evaluator is not None:
        try:
            shadow_evaluator = copy.copy(source.evaluator)
            shadow_evaluator.registry = runtime.registry
            judge = getattr(shadow_evaluator, 'judge', None)
            if judge is not None:
                shadow_evaluator.judge = copy.copy(judge)
            cache = getattr(shadow_evaluator, 'output_cache', None)
            if cache is not None:
                shadow_cache = copy.copy(cache)
                if hasattr(cache, '_cache'):
                    shadow_cache._cache = dict(cache._cache)
                shadow_cache.hits = 0
                shadow_cache.misses = 0
                shadow_evaluator.output_cache = shadow_cache
            router = getattr(shadow_evaluator, 'router', None)
            if router is not None:
                shadow_router = copy.copy(router)
                shadow_router.registry = runtime.registry
                for component in ('rule', 'embed', 'llm'):
                    value = getattr(shadow_router, component, None)
                    if value is not None:
                        setattr(shadow_router, component, copy.copy(value))
                shadow_evaluator.router = shadow_router
            runtime.evaluator = shadow_evaluator
        except Exception as exc:
            raise RuntimeError(f'SHADOW_EVALUATOR_INIT_FAILED: {exc}') from exc
    return runtime


def _rebind_checkpoint_ledger(evolver: SkillEvolver, state: EvolveLoopState) -> None:
    """Reattach a deserialized durable ledger before any resumed LLM call."""
    outcome = state.get('outcome')
    persisted = getattr(outcome, 'ledger', None) if outcome is not None else None
    if persisted is None:
        return
    active_budget = state.get('active_budget')
    if active_budget is None:
        context = state.get('context')
        active_budget = getattr(context, 'active_budget', None) if context is not None else None
    if active_budget is not None:
        # Do not reset records or start_time: the durable budget is cumulative
        # across process boundaries and must continue from its old deadline.
        persisted.budget = active_budget
    evolver.budget = active_budget or getattr(persisted, 'budget', evolver.budget)
    if evolver.ledger is not persisted:
        evolver._bind_ledger(persisted)
    else:
        # Idempotent: wrap_with_ledger returns an existing same-ledger wrapper.
        evolver._bind_ledger(persisted)


def _shadow_manifest(root: Union[str, Path]) -> list[dict[str, Any]]:
    """Return deterministic file fingerprints for side-effect auditing."""
    base = Path(root).resolve()
    records: list[dict[str, Any]] = []
    if not base.exists():
        return records
    scan_root = base / 'runs' if (base / 'runs').exists() else base
    for path in sorted(p for p in scan_root.rglob('*') if p.is_file()):
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        records.append({
            'path': str(path.relative_to(base)),
            'sha256': digest,
            'size': path.stat().st_size,
        })
    return records


def _safe_record_terminal_trace(
    trace_file: Union[str, Path],
    run_id: str,
    skill_name: str,
    evaluand_type: str,
    reason: str,
    verbose: bool = False,
) -> None:
    try:
        from .eval_tracer import record_invalid_skipped_trace
        record_invalid_skipped_trace(
            trace_file=trace_file,
            run_id=run_id,
            skill=skill_name,
            evaluand_type=evaluand_type,
            reason=reason,
        )
    except Exception as trace_exc:
        if verbose:
            print(f'  ⚠️ terminal 轨迹落盘异常: {trace_exc}')


def _safe_ensure_evaluand_trace(
    trace_file: Union[str, Path],
    run_id: str,
    skill_name: str,
    evaluand_type: str,
    eval_result: Any,
    verbose: bool = False,
) -> None:
    if not trace_file or getattr(eval_result, '_eval_trace_written', False):
        return
    try:
        from .eval_tracer import (
            derive_eval_case_annotations,
            record_eval_traces_from_eval_result,
        )
        skipped, effective = derive_eval_case_annotations(eval_result)
        records = record_eval_traces_from_eval_result(
            trace_file=trace_file,
            run_id=run_id,
            skill=skill_name,
            eval_result=eval_result,
            evaluand_type=evaluand_type,
            skipped_cases=skipped,
            effective_failed_cases=effective,
        )
        if not records:
            _safe_record_terminal_trace(
                trace_file, run_id, skill_name, evaluand_type, '评估结果无 case-level 轨迹，按 INVALID_SKIPPED 归档', verbose
            )
        setattr(eval_result, '_eval_trace_written', True)
    except Exception as trace_exc:
        if verbose:
            print(f'  ⚠️ {evaluand_type} 轨迹落盘异常: {trace_exc}')


def _record_transition(
    state: EvolveLoopState,
    from_node: str,
    to_node: str,
    reason: str,
    decision: str = '',
) -> None:
    entry = {
        'from_node': from_node,
        'to_node': to_node,
        'round_no': state.get('round_no', 1),
        'decision': decision,
        'reason': reason,
        'timestamp': time.time(),
    }
    hist = list(state.get('transition_history') or [])
    hist.append(entry)
    state['transition_history'] = hist

    outcome = state.get('outcome')
    if outcome is not None:
        trans = getattr(outcome, 'graph_transitions', None)
        if trans is None:
            trans = []
            setattr(outcome, 'graph_transitions', trans)
        trans.append(entry)



def node_failure_analysis(state: EvolveLoopState, config: Optional[RunnableConfig] = None) -> dict:
    cfg_data = config.get('configurable', {}) if config else {}
    evolver: SkillEvolver = cfg_data.get('evolver')
    if evolver is None:
        raise ValueError("Configurable 'evolver' (SkillEvolver instance) is required.")

    skill_name = state['skill_name']
    verbose = state.get('verbose', False)
    active_budget = state.get('budget') or evolver.budget

    shadow_root, shadow_paths = _prepare_shadow_root(evolver, state.get('shadow_root'))
    # A resumed state already owns the durable ledger.  Reset is allowed only
    # for a genuinely new run; resetting here would silently erase budget
    # usage after a process restart.
    if state.get('outcome') is not None and getattr(state['outcome'], 'ledger', None) is not None:
        _rebind_checkpoint_ledger(evolver, state)
        active_budget = state.get('active_budget') or getattr(evolver.ledger, 'budget', active_budget)
    else:
        evolver.ledger.reset(budget=active_budget)
        evolver._bind_ledger(evolver.ledger)

    trace_ts = datetime.now(timezone.utc).strftime('%Y%m%d_%H%M%S_%f')
    trace_nonce = f'{time.time_ns()}-{uuid.uuid4().hex}'
    effective_run_id = state.get('run_id') or f'evolve-{skill_name}-{trace_ts}-{trace_nonce}'
    requested_trace = Path(state['trace_file']).expanduser().resolve() if state.get('trace_file') else None
    if requested_trace is not None and not _is_relative_to(requested_trace, shadow_root):
        raise ValueError(
            f'SHADOW_TRACE_OUTSIDE_ROOT: trace_file {requested_trace} is not under {shadow_root}'
        )
    effective_trace_file = requested_trace or (
        shadow_root / 'runs' / 'eval_traces' / f'{trace_ts}-{trace_nonce}.jsonl'
    )

    eff_enable_reflection = (
        state['enable_reflection']
        if state.get('enable_reflection') is not None
        else getattr(active_budget, 'enable_reflection', False)
    )
    # Preserve the requested workflow flag in the terminal context so the
    # semantic outcome remains comparable with evolve_full.  Physical graph
    # publication is independently forced through the shadow guard below;
    # ``graph_shadow_only`` records that stronger execution property.
    eff_shadow_mode = (
        state['shadow_mode']
        if state.get('shadow_mode') is not None
        else getattr(active_budget, 'shadow_mode', True)
    )
    eff_auto_publish = (
        state['auto_publish_enabled']
        if state.get('auto_publish_enabled') is not None
        else getattr(active_budget, 'auto_publish_enabled', False)
    )
    eff_enable_a2 = (
        state['enable_a2']
        if state.get('enable_a2') is not None
        else getattr(active_budget, 'enable_a2', True)
    )

    outcome = EvolveOutcome(
        skill_name=skill_name,
        baseline_score=0.0,
        patches_generated=0,
        ledger=evolver.ledger,
        rounds_executed=1,
        run_id=effective_run_id,
        trace_file=str(effective_trace_file),
        shadow_root=str(shadow_root),
        shadow_paths=shadow_paths,
    )

    max_candidates = state.get('max_candidates', 3)
    eval_set_for_iter = state.get('eval_set_for_iter', 'repair_set')
    candidate_cap = active_budget.get_effective_candidate_limit(round_index=0, candidates_so_far=0)

    if (
        candidate_cap is not None
        and max_candidates > candidate_cap
        and active_budget.on_candidate_overflow == 'reject'
    ):
        outcome.error = (
            f'候选生成预算硬帽超限：CANDIDATE_LIMIT_EXCEEDED: '
            f'requested {max_candidates} candidates > clamp {candidate_cap}'
        )
        _safe_record_terminal_trace(effective_trace_file, effective_run_id, skill_name, 'candidate', outcome.error, verbose)
        return {
            'outcome': outcome,
            'stop_reason': outcome.error,
            'action': 'stop',
            'round_no': 1,
        }

    if is_forbidden_eval_set(eval_set_for_iter):
        reason_msg = f"FORBIDDEN_EVAL_SET: eval_set_for_iter '{eval_set_for_iter}' 禁止指向 experiment_holdout 或 final_audit"
        diag_path = evolver.archive_isolation_diagnostic(
            repo_root=shadow_root,
            skill_name=skill_name,
            eval_set=eval_set_for_iter,
            reason=reason_msg,
        )
        outcome.error = reason_msg
        outcome.patches_review.append(str(diag_path))
        meta = evolver.registry.get_meta(skill_name)
        body = evolver.registry._bodies.get(skill_name, '')
        baseline_stats = compute_body_section_stats(body)
        outcome.records.append(EvolveRecord(
            skill_name=skill_name,
            patch=None,
            baseline_body=body,
            candidate_body='',
            baseline_body_stats=baseline_stats,
            candidate_body_stats={},
            bloat_verdict=None,
            bloat_reasons=[reason_msg],
            distillation_prompt=None,
            status='REVIEW',
        ))
        return {
            'outcome': outcome,
            'stop_reason': reason_msg,
            'action': 'stop',
            'round_no': 1,
        }

    if verbose:
        print(); print(f'▶ [Graph/1-baseline] 跑 baseline 评估 {skill_name} on {eval_set_for_iter}')
    if hasattr(evolver.evaluator, 'judge') and hasattr(evolver.evaluator.judge, 'max_retries'):
        evolver.evaluator.judge.max_retries = getattr(active_budget, 'judge_max_retries', 2)

    try:
        old_result = evolver.evaluator.evaluate_skill(
            skill_name, eval_set=eval_set_for_iter, verbose=False,
        )
    except BudgetExceededError as e:
        outcome.error = f'baseline 评估预算硬帽超限：{e.reason}'
        _safe_record_terminal_trace(effective_trace_file, effective_run_id, skill_name, 'baseline', outcome.error, verbose)
        return {'outcome': outcome, 'stop_reason': outcome.error, 'action': 'stop', 'round_no': 1}
    except Exception as e:
        outcome.error = f'baseline 评估失败：{e}'
        _safe_record_terminal_trace(effective_trace_file, effective_run_id, skill_name, 'baseline', outcome.error, verbose)
        return {'outcome': outcome, 'stop_reason': outcome.error, 'action': 'stop', 'round_no': 1}

    route_error = getattr(old_result, 'route_error', None)
    if route_error:
        outcome.error = f'路由判定不可用，停止迭代：{route_error}'
        _safe_record_terminal_trace(effective_trace_file, effective_run_id, skill_name, 'baseline', outcome.error, verbose)
        return {'outcome': outcome, 'stop_reason': outcome.error, 'action': 'stop', 'round_no': 1}

    if not old_result.valid and eff_enable_a2:
        tool_trace = _build_tool_trace_from_eval_result(old_result)
        is_dep, dep_reason = evolver.dependency_issue([], tool_trace)
        if is_dep:
            if verbose:
                print(); print(f'▶ [Graph/1-dependency] baseline 评测因外部依赖异常中断 (REVIEW): {dep_reason}')
            meta = evolver.registry.get_meta(skill_name)
            body = evolver.registry._bodies.get(skill_name, '')
            baseline_stats = compute_body_section_stats(body)
            diag_path = evolver.archive_dependency_diagnostic(
                repo_root=shadow_root,
                skill_name=skill_name,
                root_causes=[RootCause(label='deps_broken', prob=1.0, why=dep_reason)],
                tool_trace=tool_trace,
                dep_reason=dep_reason,
                meta=meta,
                body=body,
                failures=[],
            )
            outcome.patches_generated = 0
            outcome.patches_review.append(str(diag_path))
            outcome.records.append(EvolveRecord(
                skill_name=skill_name,
                patch=None,
                baseline_body=body,
                candidate_body='',
                baseline_body_stats=baseline_stats,
                candidate_body_stats={},
                bloat_verdict=None,
                bloat_reasons=[f'DEPENDENCY_ISSUE: {dep_reason}'],
                distillation_prompt=None,
                status='REVIEW',
            ))
            _safe_record_terminal_trace(effective_trace_file, effective_run_id, skill_name, 'baseline', f'DEPENDENCY_ISSUE: {dep_reason}', verbose)
            return {'outcome': outcome, 'stop_reason': f'DEPENDENCY_ISSUE: {dep_reason}', 'action': 'stop', 'round_no': 1}

    case_verdicts = getattr(old_result, 'case_verdicts', []) or []
    total_cases = len(case_verdicts)

    critical_cids = set(getattr(active_budget, 'critical_case_ids', None) or [])
    if not critical_cids and evolver.repo_root:
        p0_file = evolver.repo_root / 'evaluation_sets' / 'p0_cases.json'
        if p0_file.exists():
            try:
                p0_data = json.loads(p0_file.read_text(encoding='utf-8'))
                critical_cids = set(p0_data.get('p0_ids', []))
            except Exception:
                pass

    skipped_cases: list[dict[str, Any]] = []
    effective_failed_cases: set[str] = set()

    for cv in case_verdicts:
        cid = cv.get('case_id', '')
        ja = cv.get('judge_audit', {})
        case_is_infra_bad = False
        infra_reasons: list[str] = []

        for dim in ('task_completion', 'robustness', 'readability'):
            val = cv.get(dim)
            dim_audit = ja.get(dim, {}) if isinstance(ja, dict) else {}
            codes = [str(c) for c in dim_audit.get('reason_codes', [])]

            if val == 'INVALID' or val in ('TIMEOUT', 'FIXTURE_ERROR'):
                if _is_judge_infrastructure_error(codes, val, dim_audit):
                    case_is_infra_bad = True
                    infra_reasons.append(f"{dim}: {','.join(codes) or val}")
                elif _is_effective_failure(codes, val, dim_audit):
                    effective_failed_cases.add(cid)
                else:
                    case_is_infra_bad = True
                    infra_reasons.append(f"{dim}: {','.join(codes) or val}")

        if case_is_infra_bad:
            skipped_cases.append({
                'case_id': cid,
                'reasons': infra_reasons,
            })

    skipped_cids = {sc['case_id'] for sc in skipped_cases}
    effective_failed_cases = effective_failed_cases - skipped_cids
    outcome.skipped_cases = skipped_cases

    try:
        from .eval_tracer import record_eval_traces_from_eval_result
        record_eval_traces_from_eval_result(
            trace_file=effective_trace_file,
            run_id=effective_run_id,
            skill=skill_name,
            eval_result=old_result,
            evaluand_type='baseline',
            skipped_cases=skipped_cases,
            effective_failed_cases=effective_failed_cases,
        )
    except Exception as _trace_exc:
        if verbose:
            print(f'  ⚠️ baseline 轨迹落盘异常: {_trace_exc}')

    if not old_result.valid or skipped_cases:
        if getattr(active_budget, 'p0_fail_on_invalid', True):
            for sc in skipped_cases:
                sc_cid = sc['case_id']
                if sc_cid in critical_cids:
                    reasons_str = '; '.join(sc['reasons'])
                    outcome.error = f"baseline 评估无效，P0 关键用例 '{sc_cid}' 发生 Judge/基础设施异常: {reasons_str}"
                    _safe_record_terminal_trace(effective_trace_file, effective_run_id, skill_name, 'baseline', outcome.error, verbose)
                    return {'outcome': outcome, 'stop_reason': outcome.error, 'action': 'stop', 'round_no': 1}

        max_allowed = (
            active_budget.max_invalid_cases
            if getattr(active_budget, 'max_invalid_cases', None) is not None
            else int(total_cases * getattr(active_budget, 'invalid_case_ratio_threshold', 0.20))
        )
        if len(skipped_cids) > max_allowed:
            ratio = len(skipped_cids) / max(1, total_cases)
            outcome.error = (
                f'baseline 评估无效用例数超阈值 ({len(skipped_cids)}/{total_cases} = {ratio:.1%} '
                f'> {active_budget.invalid_case_ratio_threshold:.0%})，停止迭代: '
                + '; '.join(f"{s['case_id']}({','.join(s['reasons'])})" for s in skipped_cases)
            )
            _safe_record_terminal_trace(effective_trace_file, effective_run_id, skill_name, 'baseline', outcome.error, verbose)
            return {'outcome': outcome, 'stop_reason': outcome.error, 'action': 'stop', 'round_no': 1}

        if not old_result.valid and not case_verdicts:
            details = '; '.join(old_result.invalid_reasons) or '未知 INVALID 原因'
            outcome.error = f'baseline 评估无效，停止迭代：{details}'
            _safe_record_terminal_trace(effective_trace_file, effective_run_id, skill_name, 'baseline', outcome.error, verbose)
            return {'outcome': outcome, 'stop_reason': outcome.error, 'action': 'stop', 'round_no': 1}

    outcome.baseline_score = sum(old_result.structure_score.values()) + sum(old_result.effect_score.values())

    failures = evolver.collect_failures(
        old_result,
        effective_failed_case_ids=effective_failed_cases,
        skipped_case_ids=skipped_cids,
    )
    if not failures:
        outcome.error = '无 B_better 失败样本，Skill 已达最优 → 跳过迭代'
        return {'outcome': outcome, 'stop_reason': outcome.error, 'action': 'stop', 'round_no': 1}

    meta = evolver.registry.get_meta(skill_name)
    body = evolver.registry._bodies.get(skill_name, '')
    baseline_stats = compute_body_section_stats(body)
    original_skill_md = _reconstruct_skill_md(meta, body)
    original_digest = compute_skill_fingerprint(original_skill_md)

    context = EvolveContext(
        skill_name=skill_name,
        original_digest=original_digest,
        repair_set=eval_set_for_iter,
        baseline_result=old_result,
        failures=failures,
        baseline_meta=meta,
        baseline_body=body,
        baseline_body_stats=baseline_stats,
        active_budget=active_budget,
        round_no=1,
        seen_fingerprints={original_digest},
        shadow_mode=eff_shadow_mode,
        enable_a2=eff_enable_a2,
    )
    outcome.context = context

    root_causes: list[RootCause] = []
    if eff_enable_a2:
        relevant_body = extract_relevant_body_sections(body)
        route_result = getattr(old_result, 'route_result', None)
        route_trace = None
        if route_result is not None:
            route_trace = {
                'hit_layer': route_result.hit_layer,
                'chosen_skill': route_result.chosen,
                'scores': route_result.scores,
                'latency_ms': route_result.latency_ms,
                'trigger_keywords': getattr(getattr(meta, 'trigger', None), 'keywords', []),
                'matched_keywords': route_result.matched_keywords,
                'use_when': getattr(meta, 'use_when', ''),
                'not_for': getattr(meta, 'not_for', []),
                'routing_notes': route_result.routing_notes,
            }
        tool_trace = _build_tool_trace_from_eval_result(old_result)

        try:
            root_causes = evolver.analyze_root_cause(
                meta,
                body,
                failures,
                relevant_body_sections=relevant_body,
                route_trace=route_trace,
                tool_trace=tool_trace,
            )
        except BudgetExceededError as e:
            outcome.error = f'根因分析预算硬帽超限：{e.reason}'
            _safe_record_terminal_trace(effective_trace_file, effective_run_id, skill_name, 'baseline', outcome.error, verbose)
            return {'outcome': outcome, 'stop_reason': outcome.error, 'action': 'stop', 'round_no': 1}
        except Exception as e:
            outcome.error = f'根因分析失败：{e}'
            _safe_record_terminal_trace(effective_trace_file, effective_run_id, skill_name, 'baseline', outcome.error, verbose)
            return {'outcome': outcome, 'stop_reason': outcome.error, 'action': 'stop', 'round_no': 1}

        is_dep, dep_reason = evolver.dependency_issue(root_causes, tool_trace)
        if is_dep:
            diag_path = evolver.archive_dependency_diagnostic(
                repo_root=shadow_root,
                skill_name=skill_name,
                root_causes=root_causes,
                tool_trace=tool_trace,
                dep_reason=dep_reason,
                meta=meta,
                body=body,
                failures=failures,
            )
            outcome.patches_generated = 0
            outcome.patches_review.append(str(diag_path))
            record = EvolveRecord(
                skill_name=skill_name,
                patch=None,
                baseline_body=body,
                candidate_body='',
                baseline_body_stats=baseline_stats,
                candidate_body_stats={},
                bloat_verdict=None,
                bloat_reasons=[f'DEPENDENCY_ISSUE: {dep_reason}'],
                distillation_prompt=None,
                status='REVIEW',
            )
            outcome.records.append(record)
            _safe_record_terminal_trace(effective_trace_file, effective_run_id, skill_name, 'baseline', f'DEPENDENCY_ISSUE: {dep_reason}', verbose)
            return {'outcome': outcome, 'stop_reason': f'DEPENDENCY_ISSUE: {dep_reason}', 'action': 'stop', 'round_no': 1}

    return {
        'active_budget': active_budget,
        'effective_run_id': effective_run_id,
        'effective_trace_file': str(effective_trace_file),
        'shadow_root': str(shadow_root),
        'shadow_paths': shadow_paths,
        'graph_shadow_only': True,
        'eff_enable_reflection': eff_enable_reflection,
        'eff_shadow_mode': eff_shadow_mode,
        'eff_auto_publish': eff_auto_publish,
        'eff_enable_a2': eff_enable_a2,
        'candidate_cap': candidate_cap,
        'outcome': outcome,
        'context': context,
        'old_result': old_result,
        'skipped_cases': skipped_cases,
        'effective_failed_cases': effective_failed_cases,
        'failures': failures,
        'meta': meta,
        'body': body,
        'baseline_stats': baseline_stats,
        'original_skill_md': original_skill_md,
        'original_digest': original_digest,
        'root_causes': root_causes,
        'round_no': 1,
        'has_acceptable': False,
        'declined_evaluations': [],
        'pending_patches': [],
        'action': 'proceed',
    }


def node_candidate_generation(state: EvolveLoopState, config: Optional[RunnableConfig] = None) -> dict:
    cfg_data = config.get('configurable', {}) if config else {}
    evolver: SkillEvolver = cfg_data.get('evolver')
    if evolver is None:
        raise ValueError("Configurable 'evolver' is required.")
    _rebind_checkpoint_ledger(evolver, state)

    round_no = state.get('round_no', 1)
    outcome: EvolveOutcome = state['outcome']
    context: EvolveContext = state['context']
    active_budget: EvolveBudget = state['active_budget']
    meta = state['meta']
    body = state['body']
    verbose = state.get('verbose', False)
    effective_run_id = state['effective_run_id']
    effective_trace_file = state['effective_trace_file']

    if round_no == 1:
        candidate_cap = state.get('candidate_cap')
        clamp_r1 = min(candidate_cap, 3) if candidate_cap is not None else 3
        effective_max = min(state.get('max_candidates', 3), clamp_r1)
        if verbose:
            print(); print(f'▶ [Graph/Round 1-generate] LLM 生成 {effective_max} 个候选 patch')

        try:
            patches = evolver.generate_patches(
                meta,
                body,
                state['failures'],
                state.get('root_causes', []),
                max_candidates=effective_max,
                clamp_limit=clamp_r1,
                on_candidate_overflow=active_budget.on_candidate_overflow,
                budget=active_budget,
            )
        except BudgetExceededError as e:
            outcome.error = f'候选生成预算硬帽超限：{e.reason}'
            _safe_record_terminal_trace(effective_trace_file, effective_run_id, state['skill_name'], 'candidate', outcome.error, verbose)
            return {'outcome': outcome, 'stop_reason': outcome.error, 'action': 'stop'}
        except Exception as e:
            outcome.error = f'候选生成失败：{e}'
            _safe_record_terminal_trace(effective_trace_file, effective_run_id, state['skill_name'], 'candidate', outcome.error, verbose)
            return {'outcome': outcome, 'stop_reason': outcome.error, 'action': 'stop'}

        outcome.patches_generated = len(patches)
        if not patches:
            outcome.error = 'LLM 未生成有效 patch（可能 JSON 解析失败）'
            _safe_record_terminal_trace(effective_trace_file, effective_run_id, state['skill_name'], 'candidate', outcome.error, verbose)
            return {'outcome': outcome, 'stop_reason': outcome.error, 'action': 'stop'}

        return {
            'outcome': outcome,
            'pending_patches': list(patches),
            'current_patch': None,
            'action': 'validate',
        }

    else:
        candidates_so_far = outcome.patches_generated
        effective_cap_r2 = active_budget.get_effective_candidate_limit(
            round_index=1,
            candidates_so_far=candidates_so_far,
        )
        if effective_cap_r2 is not None and effective_cap_r2 <= 0:
            if verbose:
                print(); print('  ❌ 已达总候选上限，停止迭代')
            context.stop_reason = 'TOTAL_CANDIDATES_EXCEEDED'
            _safe_record_terminal_trace(effective_trace_file, effective_run_id, state['skill_name'], 'attempt', context.stop_reason, verbose)
            return {'outcome': outcome, 'stop_reason': context.stop_reason, 'action': 'stop'}

        outcome.rounds_executed = 2
        context.round_no = 2
        declined_evaluations = state.get('declined_evaluations', [])

        try:
            best_patch, best_result, best_verdict, best_score = evolver.select_reflection_candidate(
                declined_evaluations, outcome.baseline_score
            )
            bundle = _load_dataset_layer_ids(evolver.repo_root)
            repair_ids, holdout_ids, audit_ids, holdout_q, audit_q = bundle[:5]

            feedback = evolver.build_attempt_feedback(
                attempt_no=len(context.attempts),
                original_skill_md=state['original_skill_md'],
                candidate_patch=best_patch,
                baseline_result=state['old_result'],
                candidate_result=best_result,
                ratchet_verdict=best_verdict,
                repair_case_ids=repair_ids,
                holdout_case_ids=holdout_ids,
                audit_case_ids=audit_ids,
                repeated_fingerprints=list(context.seen_fingerprints),
                remaining_budget={
                    'calls_remaining': getattr(evolver.ledger, 'calls_remaining', None),
                    'tokens_remaining': getattr(evolver.ledger, 'tokens_remaining', None),
                },
                strategy='execution_behavior' if best_patch.level != 'L1' else 'routing_metadata',
            )
            assert_reflection_isolation(
                feedback,
                holdout_ids=bundle.holdout_ids,
                audit_ids=bundle.audit_ids,
                holdout_queries=bundle.holdout_queries,
                audit_queries=bundle.audit_queries,
                holdout_references=bundle.holdout_references,
                audit_references=bundle.audit_references,
            )
        except Exception as exc:
            outcome.error = f'反思准备/隔离失败：{exc}'
            context.stop_reason = f'REFLECTION_PREP_ERROR: {exc}'
            _safe_record_terminal_trace(effective_trace_file, effective_run_id, state['skill_name'], 'attempt', context.stop_reason, verbose)
            return {'outcome': outcome, 'stop_reason': context.stop_reason, 'action': 'stop'}

        try:
            reflection_patches = evolver.generate_reflection_patch(
                meta,
                body,
                feedback,
                budget=active_budget,
            )
        except BudgetExceededError as e:
            outcome.error = f'反思候选生成预算硬帽超限：{e.reason}'
            context.stop_reason = f'BUDGET_EXCEEDED: {e.reason}'
            _safe_record_terminal_trace(effective_trace_file, effective_run_id, state['skill_name'], 'attempt', context.stop_reason, verbose)
            return {'outcome': outcome, 'stop_reason': context.stop_reason, 'action': 'stop'}
        except Exception as e:
            outcome.error = f'反思候选生成失败：{e}'
            context.stop_reason = f'REFLECTION_EXCEPTION: {e}'
            _safe_record_terminal_trace(effective_trace_file, effective_run_id, state['skill_name'], 'attempt', context.stop_reason, verbose)
            return {'outcome': outcome, 'stop_reason': context.stop_reason, 'action': 'stop'}

        if not reflection_patches:
            outcome.error = 'LLM 反思未生成有效 patch'
            context.stop_reason = 'REFLECTION_EMPTY_PATCH'
            _safe_record_terminal_trace(effective_trace_file, effective_run_id, state['skill_name'], 'attempt', context.stop_reason, verbose)
            return {'outcome': outcome, 'stop_reason': context.stop_reason, 'action': 'stop'}

        outcome.patches_generated += len(reflection_patches)
        return {
            'outcome': outcome,
            'pending_patches': list(reflection_patches),
            'current_patch': None,
            'current_attempt_feedback': feedback,
            'action': 'validate',
        }


def node_validation(state: EvolveLoopState, config: Optional[RunnableConfig] = None) -> dict:
    cfg_data = config.get('configurable', {}) if config else {}
    evolver: SkillEvolver = cfg_data.get('evolver')
    if evolver is None:
        raise ValueError("Configurable 'evolver' is required.")
    _rebind_checkpoint_ledger(evolver, state)

    pending_patches = list(state.get('pending_patches') or [])
    if not pending_patches:
        return {'action': 'round_done'}

    patch = pending_patches.pop(0)
    round_no = state.get('round_no', 1)
    outcome: EvolveOutcome = state['outcome']
    context: EvolveContext = state['context']
    active_budget: EvolveBudget = state['active_budget']
    skill_name = state['skill_name']
    eval_set_for_iter = state.get('eval_set_for_iter', 'repair_set')
    effective_run_id = state['effective_run_id']
    effective_trace_file = state['effective_trace_file']
    eff_shadow_mode = state.get('eff_shadow_mode', True)
    verbose = state.get('verbose', False)
    evaluand_type = 'candidate' if round_no == 1 else 'attempt'

    cand_fp = compute_skill_fingerprint(patch.diff)

    if cand_fp in context.seen_fingerprints:
        verdict = RatchetVerdict(
            decision='DECLINED',
            reasons=[
                f"REPEATED_FINGERPRINT_STOP: {'候选' if round_no == 1 else '反思候选'}指纹与已有版本重复 ({cand_fp[:8]})，熔断停止"
            ],
        )
        feedback = state.get('current_attempt_feedback')
        strategy = (feedback.strategy if feedback else ('routing_metadata' if patch.level == 'L1' else 'execution_behavior'))
        attempt_record = evolver._create_attempt_record(
            context,
            strategy=strategy,
            candidate_digest=cand_fp,
            computed_level=patch.computed_level,
            verdict='DECLINED',
            reason_codes=verdict.reasons,
            round_no=round_no,
            status='DECLINED',
            patch=patch,
        )
        context.attempts.append(attempt_record)
        outcome.attempts.append(attempt_record)
        path = evolver.archive_failure(
            state.get('shadow_root'), skill_name, patch, verdict, None,
            error='REPEATED_FINGERPRINT_STOP',
            attempt_no=attempt_record.attempt_no,
            round_no=round_no,
            reason_codes=verdict.reasons,
            shadow_mode=eff_shadow_mode,
            candidate_digest=cand_fp,
        )
        outcome.patches_declined.append(str(path))
        context.stop_reason = 'REPEATED_FINGERPRINT_STOP'
        _safe_record_terminal_trace(effective_trace_file, effective_run_id, skill_name, evaluand_type, context.stop_reason, verbose)
        return {
            'outcome': outcome,
            'context': context,
            'current_patch': patch,
            'pending_patches': pending_patches,
            'validation_status': 'repeated_fingerprint',
        }

    context.seen_fingerprints.add(cand_fp)

    try:
        new_result, verdict = evolver.validate_patch(
            skill_name, patch,
            state['old_result'], eval_set_for_iter,
            budget=active_budget,
            trace_file=effective_trace_file,
            run_id=effective_run_id,
            evaluand_type=evaluand_type,
        )
        _safe_ensure_evaluand_trace(effective_trace_file, effective_run_id, skill_name, evaluand_type, new_result, verbose)
    except BudgetExceededError as e:
        verdict = RatchetVerdict(decision='DECLINED', reasons=[f'BUDGET_EXCEEDED: {e.reason}'])
        _safe_record_terminal_trace(effective_trace_file, effective_run_id, skill_name, evaluand_type, f'BUDGET_EXCEEDED: {e.reason}', verbose)
        feedback = state.get('current_attempt_feedback')
        strategy = (feedback.strategy if feedback else ('routing_metadata' if patch.level == 'L1' else 'execution_behavior'))
        attempt_record = evolver._create_attempt_record(
            context,
            strategy=strategy,
            candidate_digest=cand_fp,
            computed_level=patch.computed_level,
            verdict='DECLINED',
            reason_codes=verdict.reasons,
            round_no=round_no,
            status='DECLINED',
            patch=patch,
        )
        context.attempts.append(attempt_record)
        outcome.attempts.append(attempt_record)
        path = evolver.archive_failure(
            state.get('shadow_root'), skill_name, patch, verdict, None, str(e),
            attempt_no=attempt_record.attempt_no,
            round_no=round_no,
            reason_codes=verdict.reasons,
            shadow_mode=eff_shadow_mode,
            candidate_digest=cand_fp,
        )
        outcome.patches_declined.append(str(path))
        if round_no == 1:
            outcome.error = f'沙箱验证预算硬帽超限：{e.reason}'
        else:
            context.stop_reason = f'BUDGET_EXCEEDED: {e.reason}'
        return {
            'outcome': outcome,
            'context': context,
            'current_patch': patch,
            'pending_patches': pending_patches,
            'validation_status': 'budget_exceeded',
        }
    except Exception as e:
        verdict = RatchetVerdict(decision='DECLINED', reasons=[f'VALIDATION_EXCEPTION: {e}'])
        _safe_record_terminal_trace(effective_trace_file, effective_run_id, skill_name, evaluand_type, f'VALIDATION_EXCEPTION: {e}', verbose)
        feedback = state.get('current_attempt_feedback')
        strategy = (feedback.strategy if feedback else ('routing_metadata' if patch.level == 'L1' else 'execution_behavior'))
        attempt_record = evolver._create_attempt_record(
            context,
            strategy=strategy,
            candidate_digest=cand_fp,
            computed_level=patch.computed_level,
            verdict='DECLINED',
            reason_codes=verdict.reasons,
            round_no=round_no,
            status='DECLINED',
            patch=patch,
        )
        context.attempts.append(attempt_record)
        outcome.attempts.append(attempt_record)
        path = evolver.archive_failure(
            state.get('shadow_root'), skill_name, patch, None, None, str(e),
            attempt_no=attempt_record.attempt_no,
            round_no=round_no,
            reason_codes=verdict.reasons,
            shadow_mode=eff_shadow_mode,
            candidate_digest=cand_fp,
        )
        outcome.patches_declined.append(str(path))
        if round_no == 2:
            context.stop_reason = f'VALIDATION_EXCEPTION: {e}'
        return {
            'outcome': outcome,
            'context': context,
            'current_patch': patch,
            'pending_patches': pending_patches,
            'validation_status': 'exception',
        }

    return {
        'outcome': outcome,
        'context': context,
        'current_patch': patch,
        'pending_patches': pending_patches,
        'current_eval_result': new_result,
        'current_verdict': verdict,
        'current_cand_fp': cand_fp,
        'validation_status': 'ok',
    }


def node_defense_adjudication(state: EvolveLoopState, config: Optional[RunnableConfig] = None) -> dict:
    cfg_data = config.get('configurable', {}) if config else {}
    evolver: SkillEvolver = cfg_data.get('evolver')
    if evolver is None:
        raise ValueError("Configurable 'evolver' is required.")
    _rebind_checkpoint_ledger(evolver, state)

    patch: Patch = state['current_patch']
    new_result: EvalResult = state['current_eval_result']
    verdict: RatchetVerdict = state['current_verdict']
    cand_fp: str = state['current_cand_fp']
    round_no = state.get('round_no', 1)
    outcome: EvolveOutcome = state['outcome']
    context: EvolveContext = state['context']
    active_budget: EvolveBudget = state['active_budget']
    skill_name = state['skill_name']
    effective_run_id = state['effective_run_id']
    effective_trace_file = state['effective_trace_file']
    eff_shadow_mode = state.get('eff_shadow_mode', True)
    eff_auto_publish = state.get('eff_auto_publish', False)
    verbose = state.get('verbose', False)
    evaluand_type = 'candidate' if round_no == 1 else 'attempt'
    attempt_no = len(context.attempts) + 1

    try:
        outc = evolver.publish_patch(
            repo_root=state.get('shadow_root'),
            # A graph run is always a shadow sidecar.  Passing None here is a
            # deliberate isolation guard: _publish_patch cannot mutate the
            # live release state machine even if the input budget asks for it.
            state_machine=None,
            skill_name=skill_name,
            patch=patch,
            verdict=verdict,
            new_result=new_result,
            budget=active_budget,
            root_causes=state.get('root_causes', []),
            attempt_no=attempt_no,
            round_no=round_no,
            reason_codes=verdict.reasons,
            shadow_mode=True,
            auto_publish_enabled=eff_auto_publish,
            candidate_digest=cand_fp,
            evaluator=evolver.evaluator,
        )
    except BudgetExceededError as exc:
        outcome.error = f"{'候选' if round_no == 1 else '反思候选'}发布门预算硬帽超限：{exc.reason}"
        if round_no == 2:
            context.stop_reason = f'BUDGET_EXCEEDED: {exc.reason}'
        _safe_record_terminal_trace(effective_trace_file, effective_run_id, skill_name, evaluand_type, outcome.error, verbose)
        return {'outcome': outcome, 'stop_reason': outcome.error, 'action': 'stop'}
    except Exception as exc:
        outcome.error = f"{'候选' if round_no == 1 else '反思候选'}发布门失败：{exc}"
        if round_no == 2:
            context.stop_reason = f'PUBLISH_EXCEPTION: {exc}'
        _safe_record_terminal_trace(effective_trace_file, effective_run_id, skill_name, evaluand_type, outcome.error, verbose)
        return {'outcome': outcome, 'stop_reason': outcome.error, 'action': 'stop'}

    feedback = state.get('current_attempt_feedback')
    strategy = (feedback.strategy if feedback else ('routing_metadata' if patch.level == 'L1' else 'execution_behavior'))
    attempt_record = evolver._create_attempt_record(
        context,
        strategy=strategy,
        candidate_digest=cand_fp,
        computed_level=patch.computed_level,
        verdict=verdict.decision,
        reason_codes=verdict.reasons,
        round_no=round_no,
        patch=patch,
    )
    attempt_record.status = outc['status']
    context.attempts.append(attempt_record)
    outcome.attempts.append(attempt_record)

    try:
        cand_body = _parse_skill_document(patch.diff).body
    except Exception:
        cand_body = ''

    record = EvolveRecord(
        skill_name=skill_name,
        patch=patch,
        baseline_body=state['body'],
        candidate_body=cand_body,
        baseline_body_stats=patch.baseline_body_stats or state['baseline_stats'],
        candidate_body_stats=patch.candidate_body_stats,
        bloat_verdict=patch.bloat_verdict,
        bloat_reasons=patch.bloat_reasons,
        distillation_prompt=patch.distillation_prompt,
        status=outc['status'],
    )
    outcome.records.append(record)

    if outc['status'] == 'PUBLISHED':
        outcome.patches_published.append(outc['release_id'])
    elif outc['status'] in ('REVIEW', 'SHADOW'):
        outcome.patches_review.append(outc['path'])
    else:
        outcome.patches_declined.append(outc['path'])

    has_acceptable = state.get('has_acceptable', False)
    if verdict.decision in ('PASS', 'REVIEW'):
        has_acceptable = True

    declined_evaluations = list(state.get('declined_evaluations') or [])
    is_cand_inv, inv_why = is_candidate_eval_invalid(new_result, verdict)
    if is_cand_inv:
        attempt_record.reason_codes.append(f'INFRASTRUCTURE_FAIL_CLOSED: {inv_why}')
    elif verdict.decision == 'DECLINED':
        new_score = sum(new_result.structure_score.values()) + sum(new_result.effect_score.values())
        declined_evaluations.append((patch, new_result, verdict, new_score))

    return {
        'outcome': outcome,
        'context': context,
        'has_acceptable': has_acceptable,
        'declined_evaluations': declined_evaluations,
        'last_status': outc['status'],
    }


def node_rounds_state_machine(state: EvolveLoopState, config: Optional[RunnableConfig] = None) -> dict:
    round_no = state.get('round_no', 1)
    context: EvolveContext = state['context']
    outcome: EvolveOutcome = state['outcome']
    if outcome is not None:
        outcome.context = context
    active_budget: EvolveBudget = state['active_budget']
    eff_enable_reflection = state.get('eff_enable_reflection', False)
    has_acceptable = state.get('has_acceptable', False)
    old_result: EvalResult = state['old_result']
    declined_evaluations = state.get('declined_evaluations', [])
    effective_run_id = state['effective_run_id']
    effective_trace_file = state['effective_trace_file']
    skill_name = state['skill_name']
    verbose = state.get('verbose', False)

    if round_no >= 2:
        # Round-2 validation exits are terminal in evolver.py.  Preserve the
        # source reason (VALIDATION_EXCEPTION / REPEATED_FINGERPRINT_STOP /
        # BUDGET_EXCEEDED) instead of overwriting it with a generic label.
        if not context.stop_reason:
            context.stop_reason = 'ROUNDS_EXHAUSTED'
        return {'context': context, 'outcome': outcome, 'action': 'stop', 'stop_reason': context.stop_reason}

    # Round 1: 已有可接受候选 (PASS / REVIEW / PUBLISHED)，不重试追分 (与 evolver.py 1318 逐字对齐)
    if has_acceptable or outcome.patches_published:
        context.stop_reason = 'ACCEPTABLE_CANDIDATE_FOUND'
        return {'context': context, 'outcome': outcome, 'action': 'stop', 'stop_reason': context.stop_reason}

    max_rounds = active_budget.max_rounds if eff_enable_reflection else 1
    if not eff_enable_reflection or max_rounds < 2:
        context.stop_reason = 'REFLECTION_DISABLED_OR_MAX_ROUNDS_REACHED'
        return {'context': context, 'outcome': outcome, 'action': 'stop', 'stop_reason': context.stop_reason}

    if not old_result.valid:
        context.stop_reason = 'BASELINE_INVALID'
        _safe_record_terminal_trace(effective_trace_file, effective_run_id, skill_name, 'baseline', context.stop_reason, verbose)
        return {'context': context, 'outcome': outcome, 'action': 'stop', 'stop_reason': context.stop_reason}

    if not declined_evaluations:
        if not context.stop_reason:
            context.stop_reason = 'CANDIDATE_INVALID_FAIL_CLOSED'
        _safe_record_terminal_trace(effective_trace_file, effective_run_id, skill_name, 'candidate', context.stop_reason, verbose)
        return {'context': context, 'outcome': outcome, 'action': 'stop', 'stop_reason': context.stop_reason}

    candidates_so_far = outcome.patches_generated
    effective_cap_r2 = active_budget.get_effective_candidate_limit(
        round_index=1,
        candidates_so_far=candidates_so_far,
    )
    if effective_cap_r2 is not None and effective_cap_r2 <= 0:
        context.stop_reason = 'TOTAL_CANDIDATES_EXCEEDED'
        _safe_record_terminal_trace(effective_trace_file, effective_run_id, skill_name, 'attempt', context.stop_reason, verbose)
        return {'context': context, 'outcome': outcome, 'action': 'stop', 'stop_reason': context.stop_reason}

    return {
        'context': context,
        'outcome': outcome,
        'round_no': 2,
        'action': 'reflect',
    }


def route_after_failure_analysis(state: EvolveLoopState) -> str:
    outcome = state.get('outcome')
    if (outcome and outcome.error) or state.get('stop_reason') or state.get('action') == 'stop':
        _record_transition(state, 'failure_analysis', END, 'Baseline/RootCause 终止')
        return END
    _record_transition(state, 'failure_analysis', 'candidate_generation', '失败分析完成，准备生成候选')
    return 'candidate_generation'


def route_after_candidate_generation(state: EvolveLoopState) -> str:
    outcome = state.get('outcome')
    if (outcome and outcome.error) or state.get('stop_reason') or state.get('action') == 'stop':
        _record_transition(state, 'candidate_generation', END, '候选生成终止或超出硬帽')
        return END
    pending = state.get('pending_patches') or []
    if not pending:
        _record_transition(state, 'candidate_generation', END, '无有效待验证 patch')
        return END
    _record_transition(state, 'candidate_generation', 'validation', '候选生成完成，开始验证')
    return 'validation'


def route_after_validation(state: EvolveLoopState) -> str:
    status = state.get('validation_status', 'ok')
    round_no = state.get('round_no', 1)
    pending = state.get('pending_patches') or []

    if status == 'ok':
        _record_transition(state, 'validation', 'defense_adjudication', '沙箱评测通过，进入防线裁决')
        return 'defense_adjudication'

    if round_no >= 2 and status in {'exception', 'repeated_fingerprint', 'budget_exceeded'}:
        reason = getattr(state.get('context'), 'stop_reason', None) or state.get('stop_reason') or status
        _record_transition(state, 'validation', END, f'Round 2 终止：{reason}')
        return END

    if status == 'budget_exceeded':
        if round_no == 1:
            _record_transition(state, 'validation', 'rounds_state_machine', '评测预算硬帽超限，Round 1 中断转入 rounds 状态机')
            return 'rounds_state_machine'
        _record_transition(state, 'validation', END, '评测预算硬帽超限，熔断终止')
        return END

    if round_no == 1 and pending:
        _record_transition(state, 'validation', 'validation', f'{status}，尝试本轮下一个候选')
        return 'validation'

    _record_transition(state, 'validation', 'rounds_state_machine', f'{status}，本轮候选处理完毕，转入状态机')
    return 'rounds_state_machine'


def route_after_defense_adjudication(state: EvolveLoopState) -> str:
    outcome = state.get('outcome')
    if (outcome and outcome.error) or state.get('action') == 'stop':
        _record_transition(state, 'defense_adjudication', END, '裁决过程遭遇硬预算超限或异常')
        return END

    last_status = state.get('last_status')
    pending = state.get('pending_patches') or []

    if last_status == 'PUBLISHED':
        _record_transition(state, 'defense_adjudication', 'rounds_state_machine', 'L1 自动发布成功，本轮迭代结束')
        return 'rounds_state_machine'

    if pending:
        _record_transition(state, 'defense_adjudication', 'validation', '验证本轮下一候选')
        return 'validation'

    _record_transition(state, 'defense_adjudication', 'rounds_state_machine', '本轮候选裁决完毕，进入 rounds 状态机')
    return 'rounds_state_machine'


def route_after_rounds_state_machine(state: EvolveLoopState) -> str:
    action = state.get('action')
    if action == 'reflect':
        _record_transition(state, 'rounds_state_machine', 'candidate_generation', '触发定向受控反思回环 (Round 2)')
        return 'candidate_generation'

    _record_transition(state, 'rounds_state_machine', END, f"受控回环正常终态退出 ({state.get('stop_reason')})")
    return END


def build_evolve_state_graph(
    evolver: Optional[SkillEvolver] = None,
    checkpointer: Optional[Any] = None,
    interrupt_before: Optional[List[str]] = None,
    interrupt_after: Optional[List[str]] = None,
) -> Any:
    builder = StateGraph(EvolveLoopState)

    builder.add_node('failure_analysis', node_failure_analysis)
    builder.add_node('candidate_generation', node_candidate_generation)
    builder.add_node('validation', node_validation)
    builder.add_node('defense_adjudication', node_defense_adjudication)
    builder.add_node('rounds_state_machine', node_rounds_state_machine)

    builder.add_edge(START, 'failure_analysis')

    builder.add_conditional_edges(
        'failure_analysis',
        route_after_failure_analysis,
        {
            'candidate_generation': 'candidate_generation',
            END: END,
        },
    )

    builder.add_conditional_edges(
        'candidate_generation',
        route_after_candidate_generation,
        {
            'validation': 'validation',
            END: END,
        },
    )

    builder.add_conditional_edges(
        'validation',
        route_after_validation,
        {
            'defense_adjudication': 'defense_adjudication',
            'validation': 'validation',
            'rounds_state_machine': 'rounds_state_machine',
            END: END,
        },
    )

    builder.add_conditional_edges(
        'defense_adjudication',
        route_after_defense_adjudication,
        {
            'validation': 'validation',
            'rounds_state_machine': 'rounds_state_machine',
            END: END,
        },
    )

    builder.add_conditional_edges(
        'rounds_state_machine',
        route_after_rounds_state_machine,
        {
            'candidate_generation': 'candidate_generation',
            END: END,
        },
    )

    effective_checkpointer = checkpointer
    compiled = builder.compile(
        checkpointer=effective_checkpointer,
        interrupt_before=interrupt_before,
        interrupt_after=interrupt_after,
    )
    return compiled


def run_evolve_langgraph(
    evolver: SkillEvolver,
    skill_name: str,
    max_candidates: int = 3,
    eval_set_for_iter: str = 'repair_set',
    verbose: bool = True,
    budget: Optional[EvolveBudget] = None,
    enable_reflection: Optional[bool] = None,
    shadow_mode: Optional[bool] = None,
    auto_publish_enabled: Optional[bool] = None,
    enable_a2: Optional[bool] = None,
    run_id: Optional[str] = None,
    trace_file: Optional[Union[Path, str]] = None,
    checkpointer: Optional[Any] = None,
    thread_id: Optional[str] = None,
    shadow_root: Optional[Union[Path, str]] = None,
    resume: bool = False,
    interrupt_before: Optional[List[str]] = None,
    interrupt_after: Optional[List[str]] = None,
) -> EvolveOutcome:
    live_repo = Path(getattr(evolver, 'repo_root', Path.cwd())).resolve()
    live_runs_before = _shadow_manifest(live_repo / 'runs')
    effective_shadow_root, shadow_paths = _prepare_shadow_root(evolver, shadow_root)
    runtime_evolver = _make_shadow_evolver(evolver, effective_shadow_root, shadow_paths)

    if checkpointer is None:
        # The graph sidecar always has a durable, isolated DB by default.  A
        # caller may still inject MemorySaver for a deliberately ephemeral
        # unit test, but it can never cause a live-repo DB fallback.
        active_checkpointer = SqliteCheckpointer(db_path=shadow_paths['db'])
    else:
        active_checkpointer = checkpointer
        if isinstance(active_checkpointer, SqliteCheckpointer):
            db_path = Path(active_checkpointer.db_path).resolve()
            if not _is_relative_to(db_path, effective_shadow_root):
                raise ValueError(
                    f'SHADOW_CHECKPOINTER_OUTSIDE_ROOT: {db_path} is not under {effective_shadow_root}'
                )
    graph = build_evolve_state_graph(
        evolver=runtime_evolver,
        checkpointer=active_checkpointer,
        interrupt_before=interrupt_before,
        interrupt_after=interrupt_after,
    )

    eff_thread_id = thread_id or f'thread-evolve-{skill_name}-{time.time_ns()}-{uuid.uuid4().hex}'
    config = {
        'configurable': {
            'thread_id': eff_thread_id,
            'evolver': runtime_evolver,
        }
    }

    initial_state: EvolveLoopState = {
        'skill_name': skill_name,
        'max_candidates': max_candidates,
        'eval_set_for_iter': eval_set_for_iter,
        'verbose': verbose,
        'budget': budget,
        'enable_reflection': enable_reflection,
        'shadow_mode': shadow_mode,
        'auto_publish_enabled': auto_publish_enabled,
        'enable_a2': enable_a2,
        'run_id': run_id,
        'trace_file': str(trace_file) if trace_file else None,
        'shadow_root': str(effective_shadow_root),
        'shadow_paths': shadow_paths,
        'graph_shadow_only': True,
        'resume': resume,
        'transition_history': [],
    }

    if resume:
        checkpoint_state = graph.get_state(config)
        checkpoint_values = getattr(checkpoint_state, 'values', None)
        if not checkpoint_values or checkpoint_values.get('outcome') is None:
            raise RuntimeError(
                f'CHECKPOINT_RESTORE_FAILED: no resumable state for thread {eff_thread_id}'
            )
        _rebind_checkpoint_ledger(runtime_evolver, checkpoint_values)
        next_nodes = tuple(getattr(checkpoint_state, 'next', ()) or ())
        if not next_nodes:
            raise RuntimeError(
                f'CHECKPOINT_RESTORE_FAILED: thread {eff_thread_id} has no pending graph node'
            )
        final_state = graph.invoke(None, config)
    else:
        final_state = graph.invoke(initial_state, config)
    outcome: EvolveOutcome = final_state['outcome']

    if not getattr(outcome, 'graph_transitions', None):
        setattr(outcome, 'graph_transitions', final_state.get('transition_history', []))
    setattr(outcome, 'thread_id', eff_thread_id)
    outcome.shadow_root = str(effective_shadow_root)
    outcome.shadow_paths = shadow_paths
    outcome.side_effect_manifest = _shadow_manifest(effective_shadow_root)
    live_runs_after = _shadow_manifest(live_repo / 'runs')
    if live_runs_before != live_runs_after:
        raise RuntimeError(
            'SHADOW_ISOLATION_BREACH: graph execution changed the live repository runs tree'
        )
    outcome.live_repo_unchanged = True
    # The ledger is shared by design; make the handoff explicit for callers
    # that inspect the original evolver after a shadow run/resume.
    evolver.ledger = runtime_evolver.ledger
    evolver.budget = runtime_evolver.budget
    return outcome


def resume_evolve_langgraph(
    evolver: SkillEvolver,
    skill_name: str,
    *,
    thread_id: str,
    shadow_root: Union[Path, str],
    checkpointer: Any,
    verbose: bool = False,
) -> EvolveOutcome:
    """Explicit new-process-friendly resume entry point.

    All durable state (including the ledger and the remaining budget) comes
    from the checkpoint; no budget argument is accepted here, preventing a
    resume caller from accidentally resetting the cap.
    """
    return run_evolve_langgraph(
        evolver,
        skill_name,
        verbose=verbose,
        checkpointer=checkpointer,
        thread_id=thread_id,
        shadow_root=shadow_root,
        resume=True,
    )


def get_graph_metadata(compiled_graph: Any = None) -> dict[str, Any]:
    g = compiled_graph or build_evolve_state_graph()
    drawable = g.get_graph()
    nodes = list(drawable.nodes.keys())
    edges = [(e.source, e.target, e.conditional) for e in drawable.edges]

    return {
        'node_count': len(nodes),
        'nodes': nodes,
        'edge_count': len(edges),
        'edges': edges,
        'conditional_edge_count': sum(1 for e in edges if e[2]),
        'mermaid': drawable.draw_mermaid(),
    }
