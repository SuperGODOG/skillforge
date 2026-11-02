# SkillForge Lifecycle Business Hooks & Security Gate Acceptance

本文档作为 SkillForge 业务约束 Hook（H1 ~ H2B）的技术验收规范，明确核心机制、入口签名、测试映射与证据边界。

---

## 1. 核心架构与拦截机制

SkillForge 在原生异步主循环 `AsyncHarnessLoop` 中提供统一的生命周期 Hook 拦截与观察能力：

```
[User Query / TaskSpec]
         │
         ▼
 ┌─────────────────────────────────────────────────────────────┐
 │ AsyncHarnessLoop._run_loop                                  │
 │                                                             │
 │  1. pre_model_call (HookEntry)                              │
 │  2. Model Inference (_call_model)                           │
 │  3. post_model_call (HookEntry)                             │
 │                                                             │
 │  [Tool Calls Requested?]                                    │
 │    ├─► YES:                                                 │
 │    │     For each tool call:                                │
 │    │       a. pre_tool_exec (BeforeTool Gate)               │
 │    │          ├─ deny   -> handler_count=0, HOOK_DENIED     │
 │    │          ├─ review -> PAUSED_FOR_REVIEW, save chk      │
 │    │          └─ allow  -> dispatch via ToolBroker          │
 │    │       b. Runtime.execute_tool (Broker final authority) │
 │    │       c. post_tool_exec (AfterTool Safe View)          │
 │    │          ├─ sanitize -> inject observation_view        │
 │    │          ├─ offload  -> sanitized artifact to disk     │
 │    │          └─ failclosed on critical error               │
 │    │                                                        │
 │    └─► NO (Final Answer Proposed):                          │
 │          a. before_finalize (Custom Hook Gate)              │
 │             ├─ deny/review -> immediate stop                │
 │             └─ allow -> proceed to Oracle                   │
 │          b. task_spec.verification_fn (Independent Oracle)  │
 │             ├─ evaluates invariants (Logistics Oracle)      │
 │             ├─ caches verdict to prevent double eval        │
 │             └─ vetoes custom allow if invariants fail       │
 │                                                             │
 │  4. Context Compression (pre/post_compress)                 │
 │  5. Checkpoint Persist (_save_checkpoint)                   │
 └───────────────────────────────┬─────────────────────────────┘
                                 │
                            [finally:]
                                 ├─ finalize_run (skip if PAUSED_FOR_REVIEW)
                                 ├─ task_end (Observer Hook)
                                 └─ terminal checkpoint persist
```

### 1.1 裁决数据结构与协议
- **`HookDecision`**：
  - `action`: `"allow" | "deny" | "review"`
  - `reason_code`: 诊断码（如 `"READ_ONLY_VIOLATION"`, `"ORACLE_HALLUCINATION_DETECTED"`）
  - `details`: 结构化上下文字典
  - `evidence_refs`: 引用追踪（格式：`["run:{id}", "task:{id}", "intent:{ver}", "tool:{name}"]`）
  - `observation_view`: 可选字段，提供给模型的脱敏视图字符串。
- **`HookAuditRecord`**：
  - 记录每次 Hook 调用的 `run_id`, `task_id`, `event`, `hook_id`, `action`, `reason_code`, `latency_ms`, `error`, `is_timeout`。
  - 持久化至 `HarnessState.hook_audits`、`checkpoint.json` 及 SQLite 审计跟踪。

### 1.2 异常、超时与脱敏错误隔离模型
- **Critical Hooks**（如安全只读检查、敏感数据脱敏、最终交付校验）：
  - 默认或配置 `critical=True`。
  - 发生异常或超时立即 **Fail-Closed**（返回 `deny`），杜绝回退到未经脱敏的原始数据。
  - 公开错误消息（如注入到模型上下文中的提示）仅包含安全摘要与 reason code（如 `[POST_TOOL_HOOK_DENIED]: SENSITIVE_REDACTION_ERROR`），未经脱敏的原始异常追踪与敏感 details 严格仅持久化于受信的 `HookAuditRecord`。
- **Observer Hooks**（如遥测、日志、task_end 清理）：
  - `critical=False`。
  - 异常被安全捕获并记录在审计中，不破坏主任务结果和已归档 Episode。
- **取消优先**：
  - `asyncio.CancelledError` 绝不吞并，取消优先退出，清理未执行工具。

---

## 2. AfterTool 安全观察面与授权工件下钻

1. **原始可信数据隔离**：
   - `ToolCallRecord.output_data` / `output_text` 与 SQLite `runtime_tool_calls` 表永久保留完整未脱敏原始证据。
2. **脱敏视图注入**：
   - `create_sensitive_data_redaction_hook` 对指定字段或正则匹配项（如合成邮箱、Token）进行脱敏，生成的字符串写入 `HookDecision.observation_view`。
   - `observation_view` 替换进入 `state.messages`（`role: tool`），模型上下文无法触及原始秘密。
3. **超限工件下沉与严格授权清单限制**：
   - 超过 1500 字符的输出由 `LargeOutputOffloader` 处理：优先使用脱敏后的视图写入 `artifacts/{call_id}_{tool_name}.json` 或 `.log`。
   - 模型在上下文中接收到 `[OFFLOADED_TEXT_ARTIFACT]` 提示与 `fs://` URI，工件同步登记至当前 run 的 `memory_index`。
   - 模型调用 `InspectArtifactTool(ref_id=..., object_id=...)` 进行下钻时：
     - 工具强制校验目标工件是否属于当前 run 授权工件清单。
     - 严禁越权读取 `checkpoint.json`、SQLite 数据库文件、其他 run 的工件或路径逃逸文件（返回 `UNAUTHORIZED_ARTIFACT` 错误）。
     - 读取的工件文件内容来自经过脱敏的下沉文件，无论概要还是下钻均杜绝凭据泄漏。

---

## 3. BeforeFinalize 业务 Oracle 与交付双重拦截

1. **防虚假签收与业务幻觉**：
   - 复用物流 Oracle `verify_logistics_fulfillment`：多包裹任务中，存在未查包裹或运输中包裹却声称全部签收时，Oracle 判定为 `is_hallucination=True`。
   - `BeforeFinalize` 阻断公共输出（`[DELIVERY_BLOCKED]: ORACLE_HALLUCINATION_DETECTED`），保留拟答于 `working_scratchpad["proposed_output"]` 供追溯，Collector 归档 Episode 为 `failure`。
2. **双重校验防覆盖与独立重验**：
   - 若配置了自定义应用 `before_finalize` Hook 且返回 `allow`，只要任务存在权威 `task_spec.verification_fn`，主循环依然强制在交付前执行 Oracle。
   - 杜绝仅通过预填 `scratchpad["verification_evidence"]` 绕过校验：每次实际拟交付均对 `effective_output` 进行独立 Oracle 校验；仅在同一次交付尝试中函数与待验文本完全一致时复用，在 review 恢复、新拟答或交付内容变更时强制重新触发 Oracle。
   - Oracle 的否决裁决直接一票否决自定义 `allow`，杜绝应用 Hook 越权放行错误结果。
3. **单次受控校验**：
   - 在单次拟交付流程中，Oracle 校验仅执行一次，校验裁决在当前 attempt 的 `finally:` 阶段受控复用，避免产生多余重复开销。
4. **诚实失败与通用任务诚实表达**：
   - 合格拒绝（`TRUE_NEGATIVE`）与诚实报告工具故障（未捏造签收事实）获准交付。
   - 无独立 Oracle 的通用任务正常交付输出，但 Episode 结果标为 `unknown`，绝不因 Hook 默认 allow 伪造业务成功。

---

## 4. 状态暂停与完整恢复 (Review Pause & Resume)

1. **Review 暂停语义**：
   - `pre_tool_exec` 返回 `review` 时，`state.status` 设为 `PAUSED_FOR_REVIEW`。
   - 立即写入 `checkpoint.json`，跳过不可逆终态 `finalize_run`，`EpisodeStore` 不提前封口。
2. **进程/Runtime 级真实恢复**：
   - 重新从 SQLite DB 实例化 `AgentRuntime`、`ToolBroker`、`ExperienceCollector` 与 `AsyncHarnessLoop`。
   - 调用 `resume(run_id=...)` 时：
     - 已执行工具不盲目重跑。
     - 步数与预算消费继续累加，旧预算不重置。
     - 待执行工具重新经过 ToolBroker 权限校验。
     - 终态 `task_end` 抛异常依然落盘到 `checkpoint.json`，Episode 最终仅封口一次。

---

## 5. Supervisor 继承与父子 Hook 组合

1. **约束自动下发**：
   - `SupervisorOrchestrator` 将顶层全局安全 Hooks 透传给 subordinate worker 的 `AsyncHarnessLoop`。
2. **`compose_hooks` 管道与安全防线机制**：
   - **控制门（`pre_tool_exec`, `before_finalize` 等）**：强制父级 Hook 优先执行。父级负向约束（如只读检查、审批暂停）优先裁决；父级拒绝或要求评审时，子级 Hook 绝不调用，杜绝子级 Worker 意外丢弃上层安全门禁。裁决优先级满足 `deny > review > allow`。
   - **数据观察管道（`post_tool_exec`）**：采用 `子级处理器 -> 父级防线` 的后处理流。
     - 每个处理器通过不可污染原始记录的浅拷贝传递独立 `observation_view`；
     - 父级安全脱敏作为最后一道防线，能有效检测并二次抹除子级处理器意外暴露或新引入的敏感文本；
     - 任何后处理器的拒绝或失败不会被后续 allow 掩盖，底层 SQLite 永远保留未污染的真实 raw 数据。
3. **安全边界说明**：
   - 可信 Python Hook 属于应用进程内的观察通道控制，非 OS 级别的沙箱隔离；它严格保护声明的观察与交付通道，防止大模型上下文和下钻工件泄漏原始敏感数据。

---

## 6. 测试用例映射矩阵 (35 Tests)

| 阶段 | 测试函数名 | 验证核心行为与断言 |
| :--- | :--- | :--- |
| **H1** | `test_t1_legacy_hook_compatibility_and_none_return` | 旧可信 callable 返回 None 兼容为 allow，执行顺序保持 |
| **H1** | `test_t2_before_tool_business_constraint_denial_real_loop` | BeforeTool 拦截只读任务中的写动作，`handler_count=0`，模型收结构化拒绝 |
| **H1** | `test_t3_hook_allow_does_not_bypass_broker_permission` | Hook allow 无法绕过 Broker 白名单，未授权工具真实记录 `PERMISSION_DENIED` |
| **H1** | `test_t4_critical_hook_failclosed_and_observation_fault_tolerance` | Critical Hook 异常 Fail-closed；普通观察 Hook 异常主任务正常完成且记录错误 |
| **H1** | `test_t5_cancellation_during_hook_and_sync_timeout_boundary` | 异步 CancelledError 优先响应；同步线程超时采用隔离快照 |
| **H1** | `test_t6_repeated_denial_step_bounded_and_checkpoint_resume_audit` | 重复拒绝受步数预算约束中止；断点恢复后保留拒绝审计记录 |
| **H2A** | `test_h2a_group_a_before_finalize_oracle_fulfillment_pass` | 真实物流全签收任务 Oracle 通过，单次归档成功 Episode，去重不重验 |
| **H2A** | `test_h2a_group_a_before_finalize_hallucination_denied_delivery_blocked` | 漏查/在途声称全签收被 BeforeFinalize 阻断为 `[DELIVERY_BLOCKED]`，Episode 为 failure |
| **H2A** | `test_h2a_group_a_generic_without_oracle_unknown_and_failclosed` | 无 Oracle 任务返回结果但 Episode 为 unknown；Critical 异常 Fail-closed |
| **H2A** | `test_h2a_group_b_review_pause_no_premature_episode_resume_preserves_tools` | Review 暂停不封口 Episode；恢复续跑不重复执行已完成工具 |
| **H2A** | `test_h2a_group_b_task_end_error_preserves_main_and_persists_audit` | `task_end` 错误不破坏主结果，错误审计落盘至 checkpoint |
| **H2A** | `test_h2a_group_b_tool_denials_recorded_in_sqlite_and_tool_records` | Hook 拒绝与 Broker 拒绝在 SQLite 及 state 中明确区分来源与状态 |
| **H2B** | `test_h2b_aftertool_safe_observation_view_and_large_artifact_drilldown` | AfterTool 脱敏视图注入；超大工件脱敏下沉，Inspect 工具限制授权清单下钻无密钥泄露，未授权工件与 checkpoint 访问均被阻断，SQLite 保留原文 |
| **H2B** | `test_h2b_supervisor_hook_composition_and_worker_inheritance` | Supervisor 透传父级只读 Hook；`compose_hooks` 拦截写动作（assert HOOK_DENIED 且 handler_count=0），审计带完整 run/task/intent 引用 |
| **H2B** | `test_h2b_supervisor_hook_composition_child_view_sanitized_by_parent` | Worker 子级 Hook 改写 view 泄漏秘密时，父级脱敏 Hook 作为最终防线二次脱敏，且父级 deny 不会被子级 allow 覆盖 |
| **H2B** | `test_h2b_custom_before_finalize_allow_vetoed_by_failing_oracle` | 自定义 BeforeFinalize allow 无法覆盖失败的 Oracle，交付被阻断且 Oracle 仅单次执行校验（防伪造 evidence 绕过，review 恢复新拟答重新验证） |
| **H2B** | `test_h2b_review_resume_rebuilding_runtime_from_sqlite` | 完全重建 Runtime 与 SQLite 连接恢复执行，步数累加，预算上限不重置，Episode 单次归档，最终 checkpoint 审计包含 task_end |
| **H2B** | `test_h2b_review_resume_pending_unauthorized_rejected_by_broker` | Review 暂停从 SQLite 恢复后，若恢复后的 Broker 撤销未授权工具权限，未执行的 pending 工具真实记录 PERMISSION_DENIED 且 handler_count=0 |
| **基线** | `tests/test_harness_loop.py` (5 tests) | 主循环步数、超时、取消、工具调度回归通过 |
| **基线** | `tests/test_runtime_and_tool_broker.py` (8 tests) | Runtime 状态转换、Broker 白名单、幂等性、审计重开回归通过 |
| **基线** | `tests/test_supervisor_orchestration.py` (4 tests) | Supervisor 全局预算、约束透传、LangGraph 状态图回归通过 |

---

## 7. 一键运行与重放指令

```bash
# 1. 运行所有相关测试套件 (35 passed)
.venv/bin/pytest tests/test_harness_business_hooks.py tests/test_harness_loop.py tests/test_runtime_and_tool_broker.py tests/test_supervisor_orchestration.py -v

# 2. 重新生成并验证 5 类标准化业务 Hook 样本
.venv/bin/python3 scripts/generate_harness_hooks_fixtures.py
```
