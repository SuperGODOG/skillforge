# Pi 架构重构与执行核心集成演进进度报告

> **执行范围**：Phase 1 执行桥接、Phase 2 上下文与持久化恢复、Phase 3 评测与演进接入及 CLI 展示  
> **文档位置**：`docs/PI_REFACTOR_PROGRESS.md`  
> **最新状态（2026-11-22）**：Phase 1–4 已完成限定范围验收，见第十九节。补充真实模型冒烟完成：本地 Pi 配置的 DeepSeek V4 Flash，经实际 Pi / Runtime / Broker / Collector 验证三类合成物流用例；Codex 复核并修正统计与故障夹具，累计12次模型请求，11个相关文件72项回归通过，见第二十一节。项目未提交或推送。下方过程记录保留阶段历史。

---

## 一、总阶段状态概览

| 阶段 | 阶段目标 | 当前状态 | 交付成果与边界 |
| :--- | :--- | :--- | :--- |
| **Phase 1** | **薄 TypeScript runner + Python 权威 Runtime/Broker/Collector 最小物流纵向链与缺口收口** | **COMPLETED** | 1. 官方发布包依赖锁定（`@earendil-works/pi-agent-core@0.85.1` + `@earendil-works/pi-ai@0.85.1`，Node >= 22.0.0）；<br>2. 原生 Pi Provider/Model 配置入口（生产禁用 faux/答案脚本）；<br>3. 权威运行状态与共享预算（Model Turn 与 Tool Call 共享现有步骤预算，超限即停，异常/取消禁止生成 success Episode，真实 deadline 过期拦截且 TIMED_OUT）。 |
| **Phase 2A** | **原生 Pi `transformContext` 挂载现有 ContextCompressor 权威压缩管道** | **COMPLETED** | 1. 通过原生 Pi `AgentOptions.transformContext` 在每次送模前下发 `context_request`；<br>2. 明确消息类型映射（`systemPrompt` 与 `AgentMessage[]` 语义分离，保留完整 toolCall/toolResult/isError）；<br>3. Python 复用现有 `HarnessState`/`ContextCompressor` 4 级阶梯、pre/post hooks 与冻结权威目标约束再注入；<br>4. 4 项专项真实场景通过（大输出下沉与 Broker 跨 Run 隔离、多级压缩与 epoch 递增、异步摘要意图漂移拒绝与取消停顿、低于水位透传与幂等去重）。 |
| **Phase 2B** | **持久化暂停与跨进程恢复** | **COMPLETED** | 新 Runtime/Pi 续跑，完整快照/压缩 epoch/原预算与期限保留；规范绑定与损坏检查点拒绝；工具 ADMITTED 日志与去重；未知动作 REVIEW_REQUIRED；取消不复活；真实 Pi 异步变向与 pre/post 压缩 Hook 阻断。 |
| **Phase 3A** | **SkillEvaluator 接入 Pi 执行 backend 独立子模块 & sandbox 克隆透传** | **COMPLETED** | 1. 唯一 `SkillEvaluator` 接入策略适配器 `PiEvaluationBackend`，保留既有 criteria、物流 Oracle、ratchet 与 `validate_candidate`；<br>2. 隔离沙箱克隆 `SkillEvaluator` 时完整透传 `execution_backend`；<br>3. Baseline 与 Candidate 独立运行，正文严格隔离至各自 Provider systemPrompt；<br>4. 真实 ToolBroker 派发（3 次工具调用）、Episode `purpose="evaluation"` 与 `outcome="unknown"` 权威记录；<br>5. 物流代码 Oracle 硬拦截（错误输出即使 Judge 放行也被置 critical_fail 与共同门禁 DECLINED）；<br>6. 缺失 usage 上报 `tokens=None`/`money=None`（非假 0），`validation_channels` 规范标识 `["pi_runtime", "broker"]`；<br>7. 正式 Registry 保持未变，3/3 专项测试通过。 |
| **Phase 3B** | **Episode → RepairJob → 共同验证 → 显式晋升 → 新任务检索复用** | **COMPLETED** | 1. 实际 Pi 失败 learning Episode 与独立 Oracle；<br>2. 真实 RepairJob 经 Pi 共同验证产出 READY 候选与规范验证记录；<br>3. 未确认拒绝晋升，确认后仅在测试临时库发布；<br>4. 新 Runtime 不手填 Skill ID，检索固定正式版本并实际执行 3 次工具调用，回收新成功 Episode。 |
| **Phase 3C** | **Phase 3C 无密钥 actual Pi CLI 演示与独立子目录闭环** | **COMPLETED** | 1. CLI 子命令 `skillforge.cli pi-demo --output-dir ...`；<br>2. 唯一运行子目录 `pi_demo_<uuid>` 隔离落地 `runtime.db`、`episodes.db`、`report.json` 与空 `skills/`；<br>3. 真实官方 Pi npm 进程 + faux 脚本 + AgentRuntime + ToolBroker (2 工具) + ExperienceCollector 4-turn 闭环；<br>4. 独立业务 Oracle 权威核验覆盖 2 包裹且判定 PASS；<br>5. 真实记录 7 预算消耗 (4 model + 3 tools)、3 条真实 provenances、SQLite Episode 确保持久化；<br>6. Pi 子进程优雅关闭且已回收；专项测试 1/1 通过。 |

---

## 二、Phase 1 & Phase 2A 核心架构与实装边界

1. **依赖真实性与官方发布包验证**
   - 依赖本地自包含于 `src/skillforge/pi_runner/package-lock.json`，确认为 npm 官方源 tarball（`https://registry.npmjs.org/@earendil-works/pi-agent-core/-/pi-agent-core-0.85.1.tgz` 与 `https://registry.npmjs.org/@earendil-works/pi-ai/-/pi-ai-0.85.1.tgz`），绝无 `file:/Users` 本地伪装链接。
   - `npm ls --all` 依赖树与 Node.js 22+ 校验通过；非 `PI_SOURCE_ROOT` 模式完全依赖 npm 安装发布包执行。

2. **前置小收口：真实 Deadline 过期与共享预算**
   - `src/skillforge/runtime.py` 中的 `consume_turn_budget` 严格校验 `deadline_ts`：一旦过期，原子跃迁为 `TIMED_OUT` 并触发 `collector.finish_run`，使得模型流与工具 Handler 均不再调用，结算结果绝对非 `success`。

3. **Phase 2A `transformContext` 双向通信与类型约束**
   - **TS 侧**（`src/skillforge/pi_runner/runner.ts`）：
     - 显式声明 `BridgeTextContent`, `BridgeThinkingContent`, `BridgeToolCall`, `BridgeUserMessage`, `BridgeAssistantMessage`, `BridgeToolResultMessage`, `BridgeMessage`, `BridgeContextResponseFrame`，关键协议使用显式类型；部分处理器仍使用 `any`，没有完成全文件无 `any` 验收。
     - 分离 `systemPrompt` 与 `AgentMessage[]`：禁止向 Pi 消息数组塞入伪 `role: "system"`，系统提示词通过 `activeSystemPrompt` 注入 `AgentState.systemPrompt` / `llmContext.systemPrompt`。
     - 在每次 `streamAssistantResponse` 前，通过 `config.transformContext` 异步发送 `context_request` 帧等待 Python 权威裁定。
     - `budgetedStreamFn` 捕获实际投递给 Provider 的 `systemPrompt` 与 `messages`（放入 `captured_provider_inputs`）。
   - **Python 侧**（`src/skillforge/runtime.py` & `src/skillforge/pi_adapter.py`）：
     - `handle_context_transform` 获取冻结权威契约（从 SQLite 取快照，不信任前端目标修改）；
     - 复用现有 `ContextCompressor.compress` 4 级阶梯（大输出下沉、过期轮次驱逐、结构化摘要、权威目标再注入）及 `pre_compress`/`post_compress` hooks；
     - 响应作为本次送模的工作副本，不污染持久历史，保留未配对的 tool result。

---

## 三、可复现测试命令与执行结果（18/18 全部通过）

### 执行命令（清除 `PI_SOURCE_ROOT`，完全基于官方发布包执行）：
```bash
cd /Users/caoruixin/Desktop/project/skillforge
env -u PI_SOURCE_ROOT /Users/caoruixin/Desktop/project/skillforge/.venv/bin/pytest -v tests/test_pi_bridge.py tests/test_pi_gaps.py tests/test_pi_phase2a_context.py
```
**进程退出码：`0`**

### 18 项专项测试完整矩阵：

| 测试套件文件 | 测试用例函数名 | 核心断言 / 场景 | 实际结果 |
| :--- | :--- | :--- | :--- |
| `test_pi_bridge.py` | `test_a_complete_multi_package_query_oracle_and_episode` | 完整多包裹只读查询 → 独立 Oracle → Episode 结算闭环 | **PASSED** |
| `test_pi_bridge.py` | `test_b_frozen_body_isolated_from_subsequent_draft_mutations` | 运行时初始正文快照隔离，后续内存修改不渗透已启动任务 | **PASSED** |
| `test_pi_bridge.py` | `test_c_unauthorized_refund_blocked_handler_zero_and_rejected_record` | 未授权高危工具越权拦截，Handler 0 执行且持久化 REJECTED | **PASSED** |
| `test_pi_bridge.py` | `test_d_subprocess_failure_or_error_frame_not_marked_success` | 子进程协议错误帧触发失败，任务绝非 COMPLETED | **PASSED** |
| `test_pi_bridge.py` | `test_e_cancellation_stops_dispatch_and_reaps_process` | 运行中取消联动下发 abort，拒收后续工具并清理子进程 | **PASSED** |
| `test_pi_bridge.py` | `test_f_duplicate_and_cross_run_messages_rejected_without_side_effects` | 跨 Run 与重复 Call ID 拒收，无二次执行副作用 | **PASSED** |
| `test_pi_gaps.py` | `test_gap_a1_node_version_and_source_root_validation` | Node 运行时版本 >= 22 校验；`PI_SOURCE_ROOT` 目录与 package.json 严格验证 | **PASSED** |
| `test_pi_gaps.py` | `test_gap_a2_production_rejects_faux_and_scripts_without_test_mode` | 生产模式（`test_mode=False`）严格拒绝 `faux` provider 和预写 answers 脚本 | **PASSED** |
| `test_pi_gaps.py` | `test_gap_a3_native_pi_provider_entrypoint_initialization` | 正常 Pi 官方 provider/model 配置入口，无 HTTP client 侵入 | **PASSED** |
| `test_pi_gaps.py` | `test_gap_a4_missing_usage_reports_null_not_pseudo_zero` | 缺省 usage 场景严格下发 `null`/`None`，断言禁止伪造 `{input: 0, output: 0}` | **PASSED** |
| `test_pi_gaps.py` | `test_gap_b1_shared_budget_exhaustion_stops_subsequent_handlers` | 小预算下 Model Turn 与 Tool Call 共享计数，超限后后续 handler 绝不继续 | **PASSED** |
| `test_pi_gaps.py` | `test_gap_b2_subprocess_failure_settles_and_never_success_episode` | 子进程崩溃/协议异常时记录 FAILED，Collector 结算为非 success Episode | **PASSED** |
| `test_pi_gaps.py` | `test_gap_b3_aborted_run_settles_cancelled_and_never_success_episode` | 运行中 abort/cancel 结算为 CANCELLED，Collector 结算为非 success Episode | **PASSED** |
| `test_pi_gaps.py` | `test_gap_b4_real_deadline_expiration_stops_stream_and_handler_settles_timed_out` | 真实 deadline 过期时停止 stream 与 handler，终态 TIMED_OUT 且非 success | **PASSED** |
| `test_pi_phase2a_context.py` | `test_phase2a_scenario1_large_output_offloaded_provider_input_and_inspect_artifact` | **(Scenario 1)** 大工具输出下沉后 provider 输入包含清单，Broker inspect_artifact 取回且跨 Run 拒绝 | **PASSED** |
| `test_pi_phase2a_context.py` | `test_phase2a_scenario2_multi_step_compression_epoch_frozen_state_client_tokens` | **(Scenario 2)** 两次压缩后 epoch 增至 3，冻结目标/禁写约束与完整 call-result 配对实际送模，客户端估算 token 无假 KV 命中 | **PASSED** |
| `test_pi_phase2a_context.py` | `test_phase2a_scenario3_async_summarizer_intent_drift_rejected_and_cancel_stops` | **(Scenario 3)** 异步摘要中意图变更拒绝旧候选且不污染新状态；取消后立即停止派发并落终态 | **PASSED** |
| `test_pi_phase2a_context.py` | `test_phase2a_scenario4_under_watermark_no_compression_no_duplicate_budget_hooks_active` | **(Scenario 4)** 低于水位不触发压缩，不重复扣预算，重复 Call ID 幂等去重，pre/post hooks 正常拦截执行 | **PASSED** |

---

## 四、Phase 2B 原子步骤 R3a 进展（Python 到 Pi continue 协议衔接）

1. **实际改动文件**：
   - `src/skillforge/pi_adapter.py`（L221-239 & L261-308）：
     - 在 `init_run` 中自动将 `initial_messages` 中既有的已完成 `toolCall`/`toolResult` 的 call ID 预注册进 `self._executed_call_ids`，杜绝历史工具重放风险；
     - 复用既有 `continue_run` 向 runner 发送 `type: "continue"` 协议帧并等待 `_prompt_future`，复用既有 JSONL 通信、结果解析与进程清理。
   - `tests/test_pi_phase2b_continue.py`（新建）：
     - 包含原子测试 `test_continue_from_existing_history_without_replaying_tools`。

2. **验证命令与结果**：
   ```bash
   env -u PI_SOURCE_ROOT .venv/bin/pytest -q tests/test_pi_phase2b_continue.py::test_continue_from_existing_history_without_replaying_tools
   ```
   **进程退出码：`0`**（`1 passed in 0.89s`）

3. **核心断言实测证据**：
   - `tool_handler_invocations == 0`：既有工具 Handler 执行计数为 0（已配对的工具历史未发生重复执行）；
   - `user_msg_count == 1`：Provider 收到的消息中未追加多余/伪造的用户 prompt；
   - `found_tool_call is True` & `found_tool_result is True`：Provider 捕获输入保留了旧 toolCall ID（`tc_history_001`）与对应执行结果；
   - `result.text == continuation_answer`：成功接收到模型续跑给出的最终文本回答；
   - `exit_code == 0` 且 `ProcessLookupError`：子进程获得正常优雅关闭与 SIGKILL 保底，无僵尸进程遗留。

4. **仍未验收的 Phase 2B 缺口（严禁提前声明全阶段完成）**：
   - Runtime 级 SQLite 断点恢复（`runtime.resume_run` 与 `arun_agent_with_pi` 的断点接驳）；
   - 检查点持久化反序列化、预算重载与意图漂移校验。

---

## 五、Phase 2B 原子步骤 B1 进展（协作式暂停与 Runtime 检查点持久化）

1. **实际改动文件**：
   - `src/skillforge/runtime.py`：
     - 新增 `request_pause_run(run_id: str) -> bool`：校验 Run 状态，活动 Run 标记入 `_pause_requested_runs`，幂等重复调用，明确拒绝不存在（KeyError）或终态（ValueError）；
     - 新增 `get_pi_checkpoint(run_id: str) -> Optional[PiCheckpointRecord]`：从 `runtime_pi_checkpoints` 读取最新检查点并 JOIN `runtime_runs` 权威预算/deadline 数据；
     - 在 `arun_agent_with_pi` 的 `handle_context_transform` 两条返回分支（无 compressor / 有 compressor）统一复用 `checkpoint_response` 本地函数；暂停请求触发时在一个事务中写入检查点与更新 Run 状态为 `PAUSED`，返回 `error_type: "PAUSED"` 驱动 Pi 协同停止；
     - 当收到 `stop_reason == "paused"` 时保持 `PAUSED` 终态隔离，回收子进程，严禁触发 `collector.finish_run` 或生成最终 Episode。
   - `tests/test_pi_phase2b_checkpoint.py`（新建）：
     - 包含 3 项原子与集成测试用例。

2. **验证命令与结果**：
   ```bash
   env -u PI_SOURCE_ROOT .venv/bin/pytest -q tests/test_pi_phase2b_checkpoint.py
   ```
   **进程退出码：`0`**（`3 passed in 1.00s`）

3. **核心断言实测证据**：
   - `tool_call_count == 1`：原生 Broker 物流包裹查询工具严格执行 1 次；
   - `last_res.stop_reason == "paused"`：Pi 适配器精准捕获协作式 `paused` 状态帧；
   - `len(last_res.captured_provider_inputs) == 1`：仅发生第 1 轮模型调用，第 2 轮送模被上下文暂停成功阻止；
   - `run_rec.status == "PAUSED"` 且 `run_rec.terminal_at is None`：任务安全停顿，非终态异常；
   - `collector._runs[run_id].completed is False` 且 `episode_store.get_episode(...) is None`：未生成伪 success/failed 最终 Episode；
   - `run_id not in runtime._active_pi_clients`：旧 Pi 子进程 PID 被优雅回收；
   - 独立新 `AgentRuntime` 实例加载相同 SQLite DB 调用 `get_pi_checkpoint(run_id)`：
     - `chk.frozen_body == candidate.body`（冻结正文快照一致）；
     - `chk.epoch > 1`（经过真实 `ContextCompressor` 压缩，epoch = 2）；
     - 历史消息中保留未配对的 toolCall ID（`tc_b1_packages_01`）与对应 `toolResult`；
     - `chk.budget_consumed == 2`（Turn 1 模型预算 1 + Tool 调用预算 1，未预扣 Turn 2）；
     - `chk.deadline_ts == deadline`（严格等于原始权威截止时间，无 float 容差浮动）；
     - `chk.model_config` 白名单安全序列化（仅保留 `provider`/`model_id`，剔除测试 responses 脚本与敏感凭据）；
   - `test_phase2b_b1_request_pause_repeated_is_idempotent`：重复请求幂等返回 True，flag 集合无多余项；
   - `test_phase2b_b1_request_pause_rejects_nonexistent_and_terminal`：不存在 Run 明确抛出 `KeyError`，已终止 Run 明确抛出 `ValueError`。

4. **仍未验收的 Phase 2B 缺口（严禁提前声明全阶段完成）**：
   - **B3**：断点恢复期间的意图漂移校验与沙箱文件系统再绑定。

---

## 六、Phase 2B 原子步骤 B2 进展（Runtime 级恢复执行与权威闭环）

1. **实际改动与实现文件**：
   - `src/skillforge/runtime.py`（`resume_agent_with_pi` / `aresume_agent_with_pi`）：
     - 从 `runtime_pi_checkpoints` 读取 `PiCheckpointRecord`，进行 CAS 状态跃迁（`PAUSED` -> `RUNNING`）；
     - 重建权威 `HarnessState`（包含 `ProtectedState`, `WorkSummary`, `MemoryIndex`），继承 `checkpoint.epoch`；
     - 恢复 `ExperienceCollector` 运行态上下文（保留前期 `provenances` 与 `action_summaries`）；
     - 将检查点历史消息注入 Pi runner 作为 `initial_messages`，通过 `continue_run` 恢复；
     - 严格保持已有 `budget_consumed` 与原始 `deadline_ts`，超限或过期原子终止；
     - `finalize_run` 结算为单一权威 Episode，重复调用幂等不写重复行。
   - `tests/test_pi_phase2b_recovery.py`（新建）：
     - 包含 3 项端到端恢复测试用例（正常恢复继续执行直至 Oracle PASS、预算耗尽防重置拒绝、截止时间过期拦截）。

2. **验证命令与结果**：
   ```bash
   env -u PI_SOURCE_ROOT .venv/bin/pytest -q tests/test_pi_phase2b_checkpoint.py tests/test_pi_phase2b_recovery.py
   ```
   **进程退出码：`0`**（`6 passed in 3.56s`）

3. **核心断言实测证据**：
   - `test_resume_new_runtime_and_pi_keeps_snapshot_budget_and_episode`：
     - `answer == FINAL`：恢复后成功调用未执行的 2 个 tracking 工具并生成最终结论；
     - `pids`：旧段 PID（如 8426）与恢复段 PID（如 8429）互不相同，两者均在完成各自段后通过 `assert_reaped` 确认干净回收（`ProcessLookupError`）；
     - `counts == {"order": 1, "track": 2}`：已停顿段的 `order` 工具调用未被重复执行；
     - `rec.budget_consumed == 7` 且 `rec.budget_max == 10`：初始段消耗 2（1 model + 1 tool），恢复段消耗 5（2 model turns + 2 tool calls + 1 final turn），累计 7；
     - `rec.deadline_ts == e["deadline"]`：恢复前后截止时间保持严格不变；
     - `state.context_epoch >= e["checkpoint"].epoch`：压缩轮次权威递增；
     - `rendered`：保留 `BODY` 快照，后续 Candidate 内存修改（`NEW_BODY_MUST_NOT_LEAK`）绝无泄漏；
     - `provs`：累计 3 个工具凭证，首个凭证签名与停顿前完全吻合；
     - `oracle["independent_pass"] is True`：独立物流业务 Oracle 判定 PASS；
     - `episode.outcome == "success"` 且 `len(episode.provenances) == 3`，数据库内 `episodes` 表严格记录 1 行。
   - `test_resume_does_not_reset_remaining_budget`：
     - 当总预算仅为 3 时，恢复段在耗尽剩余预算后立即抛出并落态 `BUDGET_EXHAUSTED`（`rec.budget_consumed == 3`），子进程干净回收，未生成 success Episode。
   - `test_resume_preserves_expired_original_deadline`：
     - 恢复时若已超过原始 deadline，直接抛出 `TimeoutError` 并落态 `TIMED_OUT`（`rec.budget_consumed == 2`），子进程不予拉起。

---

## 七、Phase 2B 原子步骤 B3a 进展（恢复绑定与损坏检查点拒绝）

1. **实际改动与实现文件**：
   - `src/skillforge/runtime.py`：
     - `get_pi_checkpoint`（L883-938）：重构 4 处 `json.loads` 回退逻辑为 fail-closed 强校验，格式版本非 1 或 JSON 损坏明确抛出 `ValueError("INVALID_CHECKPOINT: ...")`，且严格验证 `messages` 为 list、`working_state` 为 dict、`tools` 为 list、`model_config` 为 dict，杜绝以空历史代替损坏快照；
     - `checkpoint_response`（L2830-2880）：在 `working_state` 中注入权威 `recovery_binding`，包含规范 RunRecord（`run_id`, `task_id`, `intent_revision`, `task_spec_hash`, `content_hash`, `frozen_body` 的 sha256）、非秘密白名单 model_config（`provider`, `model_id`, `model`, `temperature`, `max_tokens`）、Broker 工具集合及参数 JSON Schema、`application_allowlist`、绑定 validator 信息及来自可选 `CandidateStore` 的权威 `TaskContext` 约束与指纹；
     - `arun_agent_with_pi`（L2197-2315）：
       - 强制按 `run_id` 从 Store 重读规范检查点，传入 caller-provided checkpoint 若字段不符直接抛出 `ValueError("INVALID_CHECKPOINT: ...")` 拒绝；
       - 恢复绑定与权威源比对严格在 `PAUSED -> RUNNING` CAS 更新与 `client.start()` 之前执行；
       - 漂移统一抛出清晰 `ValueError("STALE_CHECKPOINT: ...")`，绝不拉起新 PID、不执行工具 Handler、不延长截止时间、不重置或清零步骤预算；
       - 历史遗留或缺省 `recovery_binding` 的检查点 fail-closed 拒绝恢复；
     - `handle_context_transform`（L2680-2695 & L2998-3008）：在送模转换开始前及 `await compressor.compress` 之后执行权威 TaskContext 意图版本对比，阻断异步摘要期间 canonical 意图漂移旧快照混写。
   - `tests/test_pi_phase2b_recovery.py`：
     - `paused_factory` 扩展支持可选 `candidate_store` 与 `task_context` 依赖注入，默认行为完全兼容 B2。
   - `tests/test_pi_phase2b_safety.py`（新建）：
     - 包含 8 大核心测试函数（10 项场景），覆盖 JSON 损坏、format_version 异常、缺少 binding、caller 伪造 checkpoint、RunRecord 字段篡改（intent_revision, task_spec_hash, frozen_body）、model_id/temperature 漂移、ToolBroker allowlist 与参数 Schema 漂移、真实 `revise_task_context` API 变更拦截以及 Draft.body 仅内存修改仍保留 B2 正常恢复通过。

2. **验证命令与结果**：
   ```bash
   env -u PI_SOURCE_ROOT .venv/bin/pytest -q tests/test_pi_phase2b_checkpoint.py tests/test_pi_phase2b_recovery.py tests/test_pi_phase2b_safety.py
   ```
   **进程退出码：`0`**（`16 passed in 9.15s`）

3. **核心断言实测证据**：
   - 损坏 JSON 与 format_version != 1：直接被 `get_pi_checkpoint` 与 `resume_agent_with_pi` 拦截，抛出 `INVALID_CHECKPOINT`；
   - 伪造 caller checkpoint：篡改 messages 注入用户对话或篡改 frozen_body 均被拦截拒绝，抛出 `INVALID_CHECKPOINT`；
   - RunRecord 权威漂移（`intent_revision` 篡改、`task_spec_hash` 篡改、`frozen_body` 篡改）：在 CAS 发生前拦截，抛出 `STALE_CHECKPOINT`；
   - model_config 显式漂移（`model_id` 篡改、显式引入未绑定的 `temperature`）：在 CAS 发生前拦截，抛出 `STALE_CHECKPOINT`；
   - ToolBroker 漂移（白名单削减、参数 Schema 篡改）：在 CAS 发生前拦截，抛出 `STALE_CHECKPOINT`；
   - 真实 `revise_task_context` API 变更（TaskContext `intent_revision` 从 1 升至 2）：在 CAS 发生前拦截，抛出 `STALE_CHECKPOINT`；
   - **所有拒绝场景一致防线证据**：
     - `len(e["pids"]) == 1`：零新 PID 产生，无进程泄漏；
     - `e["counts"] == {"order": 1, "track": 0}`：未恢复执行后续工具；
     - `rec.budget_consumed == 2` 且 `rec.budget_max == 10`：预算严格锁定为 2，未归零；
     - `rec.deadline_ts == e["deadline"]`：原始截止时间保持不变；
     - `rec.status == "PAUSED"`：未跃迁至 RUNNING 或伪终态；
   - **B2 兼容性验证**：内存中修改 `candidate.body = "NEW_BODY_MUST_NOT_LEAK"` 后调用恢复，仍正常使用冻结快照 `BODY` 恢复并通过 Oracle 判定（`answer == FINAL`，累计消耗预算 7，生成唯一 success Episode）。

---

## 八、未做边界与后续阶段声明（Boundaries & Truthful Accounting）

1. **Phase 2A 与 Phase 2B (R3a, B1, B2, B3a) 已收敛内容**：
   - Pi `transformContext` 与 Python `ContextCompressor` 权威联动收工；
   - R3a（continue 协议通畅）、B1（checkpoint 持久化与协同暂停）、B2（Runtime 恢复联跑与完整业务结算）完全闭环；
   - B3a（恢复绑定、格式与类型 fail-closed 强校验、权威源比对与篡改/漂移全方位拦截）完全闭环并通过端到端测试；
   - 保证断点恢复时不重置预算、不重放工具、不泄漏新正文、不遗漏凭证，拦截时不拉起进程/不扣预算/不改状态。

2. **Phase 2B B3b 剩余待实现与待验收项**：
   - ADMITTED 工具不确定结果 REVIEW（工具调用产生未知或模糊结果时挂起待人工审查，不盲目恢复）；
   - 取消不复活（已 CANCELLED 的任务坚决禁止被 resume 调用复活）；
   - 压缩 Hook 真实阻断（`pre_compress` / `post_compress` 阻断决策在继续执行阶段的端到端生效）；
   - 异步 canonical TaskContext 变向（在长时间异步摘要运行期间，真实外部意图变向的现场阻断）。

3. **Phase 3 边界严格限定（严禁擅加平台功能）**：
   - Phase 3 尚未开始；
   - 将 Pi 产生的 Episode 接入既有流程：`Episode -> RepairJob -> 共同门禁 -> 显式晋升 -> 复用闭环`；
   - 严禁擅自引入 Pareto 前沿选择算法或批量自治治理平台。

## 最新 Phase 2B 收口证据（2026-11-21，Codex 亲自执行）

- B3b1：`tests/test_pi_phase2b_lifecycle.py` 4 项通过，包含持久 ADMITTED 崩溃注入、两连接同 call ID 去重、取消不可恢复与合同不可覆写。SQLite 事务加锁失败直接抛错；暂停状态不能消耗模型 ticket。受控崩溃注入不是实际宿主宕机，不声称跨业务系统 exactly-once。
- B3b2：`tests/test_pi_phase2b_context_safety.py` 4 项通过。真实 Pi 调用实际 L2 异步摘要期间，canonical revision/fingerprint 变化会阻断旧摘要送模；pre/post Hook 拒绝在真实恢复路径生效。Pi 返回 error/aborted 助手结果时 Runtime 显式转为异常并非成功结算；保留错误原因与进程回收。
- B3a 额外核验：绑定 TaskContext Store 不能通过省略该依赖绕过；使用实时 compute_fingerprint；验证器版本读取 validator_version。

```bash
env -u PI_SOURCE_ROOT .venv/bin/pytest -q tests/test_pi_bridge.py tests/test_pi_gaps.py tests/test_pi_phase2a_context.py tests/test_pi_phase2b_continue.py tests/test_pi_phase2b_checkpoint.py tests/test_pi_phase2b_recovery.py tests/test_pi_phase2b_safety.py tests/test_pi_phase2b_lifecycle.py tests/test_pi_phase2b_context_safety.py
```

实测：`44 passed in 21.99s`，exit 0。计数为联合执行结果，不叠加历史重叠测试。实际官方 Pi npm 进程、Runtime/Broker/Collector 与临时数据库；provider/摘要使用显式受控夹具。本轮未调用真实付费模型，未新增真实模型效果或 OS 沙箱结论。

---

## 九、Phase 3A 进展（SkillEvaluator 接入 Pi 执行 Backend 独立子模块）

### 1. 架构定位与改动清单
- **定位**：唯一 `SkillEvaluator` 接入策略适配器 `PiEvaluationBackend`。不新增第二评测器、不造新评分算法、不建外部治理平台，严格保留既有 criteria、物流代码 Oracle、ratchet 门禁与 `validate_candidate`；
- **文件改动**：
  - `src/skillforge/pi_evaluation.py`（新增）：
    - `PiEvaluationBackend` 持有应用注入的 `AgentRuntime`、`tool_names`、非秘密 `model_config`、显式 `test_mode` 与每 case 预算参数；
    - 生产拒绝 faux/预写脚本（`test_mode=False` 严格抛出 `ValueError`）；
    - `get_config_fingerprint()` 计算非秘密参数的确定性 16 字符哈希，排除 api_key 与 responses 脚本；
    - `run(query, body, meta, skill_name)`：为每次调用生成唯一 `child_run_id`，若有正文则在内存沙箱构建 `CandidateSkill` 快照作为输入注入 `runtime.start_run(..., purpose="evaluation")`，严禁从正式 Registry 读取覆盖；通过 `runtime.run_agent_with_pi` 经由 `ToolBroker` 真实派发；子 run 终态由 Runtime 权威结算为 `outcome="unknown"`（因尚未经过业务 Oracle）；聚合所有 assistant 轮次 usage，任一轮次缺失则 `tokens=None`/`money=None`（绝不伪造 0）。
  - `src/skillforge/evaluator/__init__.py`（分支接入）：
    - `SkillEvaluator.__init__` 支持可选 `execution_backend` 参数（默认 None 保持 legacy 兼容）；
    - `get_config_fingerprint()` 与 `get_config_hash()` 混合 backend 指纹，确保 Pi 与 legacy 指纹隔离；
    - `_run_bare` 与 `_run_with_skill` 在有 backend 时调用它执行，绕过旧输出缓存；
    - `_last_bare_provenances` 与 `_last_skill_provenances` 记录真实工具调用痕迹，criteria 评测 baseline 时使用真实 bare trace；
    - `validation_channels` 在有 backend 且具备凭证时标识为 `["pi_runtime", "broker"]`，杜绝冒充 OS 沙箱；
    - `_efficiency_score` 对 `tokens is None` 实行中性兼容（返回 5.0 中值），`avg_tokens_skill`/`avg_tokens_baseline` 缺失时置 None。
  - `src/skillforge/evolution_loop.py`（L535 透传）：
    - `validate_candidate` 在创建沙箱隔离的 `sandbox_evaluator = SkillEvaluator(...)` 时，完整透传 `execution_backend=getattr(evaluator, "execution_backend", None)`、`runtime` 与 `tool_broker`，确保沙箱内 candidate 评估沿用 Pi 路径。
  - `tests/test_pi_evaluation.py`（专项测试）：
    - 覆盖三大核心场景：完整沙箱隔离与 Oracle PASS 闭环、虚假送达断言拦截与 critical fail DECLINED 阻断、生产拒 faux 与缺 usage 上报 null 语义。

### 2. 验证命令与测试结果

#### 唯一验证命令：
```bash
env -u PI_SOURCE_ROOT .venv/bin/pytest -q tests/test_pi_evaluation.py
```
**进程退出码：`0`**（`3 passed in 8.58s`）

### 3. 核心断言实测证据
- **用例 1（`test_1_validate_candidate_with_pi_backend_isolated_clone_and_oracle_pass`）**：
  - 沙箱隔离与 Backend 透传：克隆后的 `sandbox_evaluator` 保留 `execution_backend`，调用通过非侵入 spy 记录 4 次执行（baseline bare、baseline with_skill、candidate bare、candidate with_skill）；
  - 正文严格隔离：`captured_provider_inputs` 确认 baseline Pi 调用的 systemPrompt 包含完整 baseline 正文且不含 candidate 正文；candidate Pi 调用的 systemPrompt 包含完整 candidate 正文且不含 baseline 正文；
  - 真实工具派发：`query_order_packages`、`query_package_tracking("PKG_101")`、`query_package_tracking("PKG_102")` 真实经由 `ToolBroker` 调度；
  - 权威 Episode 记录：生成子 run Episode 标记 `purpose="evaluation"`，终态为 `outcome="unknown"`（业务结算挂起）；
  - 独立业务 Oracle 与门禁：两包裹核查（PKG101 DELIVERED, PKG102 IN_TRANSIT）经现有物流代码 Oracle 判定 PASS，common ratchet 门禁判定 `decision="PASS"`；
  - 规范绑定：`ValidationRecord` 严格绑定 `candidate_id`、`content_hash`、`config_hash`（混入 backend 指纹）与 `dataset_version`；正式 Registry 版本严格停留在 `1.0.0`，正文保持原状未修改；
  - 规范渠道标记：`validation_channels` 确认为 `['pi_runtime', 'broker']`。
- **用例 2（`test_2_false_claim_all_delivered_blocked_by_logistics_code_oracle`）**：
  - 缺陷输出“全部签收”（仅查 PKG101，遗漏 PKG102，PKG102 实际在途）：
  - 即使 Judge LLM 受控给出宽松 PASS，物流代码 Oracle 立即判定 `ROBUST_EVIDENCE_FAITHFUL` 为 `critical=True, status="FAIL"`；
  - 评测结果置 `critical_fail=True`，common ratchet 门禁严格判定 `decision="DECLINED"`；
  - 权威门禁阻断发布，正式 Registry 未被篡改。
- **用例 3（`test_3_production_mode_rejects_faux_and_scripts`）**：
  - 生产拒绝：`test_mode=False` 时构造 `provider="faux"` 或包含预写 responses 脚本均抛出 `ValueError`；
  - 缺 usage 报告：未报告 usage 时 `tokens=None` 与 `money=None`，拒绝冒充 0 或伪造经济效益；
  - 多轮 usage 聚合：多轮 assistant 轮次具备 usage 时正确累加（不冒充仅末轮）。

### 4. 未测边界与后续阶段声明（Boundaries & Truthful Accounting）
- **未测边界**：
  - 本次仅完成 Phase 3A 独立评测执行 backend 适配，**不包含** Phase 3B 的全自动演进闭环（从线上生产 Episode 自动聚类挖掘 RepairJob、端到端 evolution_loop CLI 命令）；
  - 依赖真实官方 npm 发布包（`@earendil-works/pi-agent-core@0.85.1` 与 `@earendil-works/pi-ai@0.85.1`）以及 Node.js 22+ 运行时，但在测试中模型响应由受控 faux 夹具提供，**未调用真实付费商业大模型 API**，不可声称真实模型业务指标提升；
  - 测试全部运行在临时数据库与沙箱目录（`tmp_path`），无本地生产污染，真实项目 git 仓库未进行 commit 或 push。

---

## 十、Phase 3B 完整演进闭环实施进展汇报（2026-11-21）

### 1. 目标与交付清单
- **目标**：完成实际 Pi 失败 learning Episode → 真实 RepairJob → Pi 共同验证产出 READY 候选 → 显式人工晋升至正式基线 → 新 Runtime 无手填 Skill ID 检索与复用执行闭环。
- **交付清单**：
  - `tests/test_pi_evolution.py`：Phase 3B 完整演进闭环专项测试套件（含 `@pytest.fixture pi_repair_env`，覆盖 3B1 失败到修复、目的隔离与 3B2 显式晋升与未来复用共 3 项核心用例）。
  - `docs/PI_REFACTOR_PROGRESS.md`：记录 Phase 3B 整体完成证据、真实性审计与边界声明。

### 2. 验证命令与测试结果

#### 唯一验证命令：
```bash
env -u PI_SOURCE_ROOT .venv/bin/pytest -q tests/test_pi_evolution.py
```
**进程退出码：`0`**（`3 passed in 14.00s`）

### 3. 核心断言实测证据
- **用例 1（`test_pi_failure_to_repair_candidate`，3B1）**：
  - **实际 Pi 失败 learning Episode 生成**：真实 AgentRuntime 调度 Pi runner，执行订单查询和 PKG_101 查询后虚假声称全部送达（遗漏 PKG_102）；真实 ToolBroker 记录 2 次 tool records；`verify_logistics_fulfillment` 判定 `independent_pass is False`；`finalize_run` 生成不可变 canonical Episode，验证其 `purpose="learning"`、`outcome="failure"` 且具备 2 条真实工具调用来源；`EpisodeStore` 按 ID 读取一致。
  - **真实 RepairJob 执行与共同验证**：构造 `PiEvaluationBackend`（完整 4-turn 流程）与 `SkillEvaluator`；极简 `ScriptedPatcher` 准确接收包含实际 `outcome_reason` 的提示词并输出合规 1.0.1 `SKILL.md`；`repair_skill_failure` 驱动实际 `RepairJob.run_attempt` 走既有共同验证（沙箱双向评测与 common ratchet 门禁）。
  - **状态与哈希绑定**：Job 最终状态为 `READY`，尝试记录为 `PASS`，候选者版本为 `1.0.1` 且 `latest_content_hash` 准确绑定候选者内容哈希；正式 Registry 内基线版本严格维持 `1.0.0` 原状不变。
  - **评估子 Episode 语义隔离**：验证沙箱评估生成的子 Episode `purpose="evaluation"`、`outcome="unknown"`，且真实记录了 `query_order_packages` 与 `query_package_tracking` 工具调用。
- **用例 2（`test_pi_evaluation_episode_cannot_be_repaired`，3B1）**：
  - **A8 目的隔离不变式**：提取 `backend.run` 产生的评估子 Episode 作为输入调用 `repair_skill_failure`，严格抛出 `ValueError`（`only failed episodes may trigger repair` / `No valid learning episodes found`）。
  - **调用计数门禁**：新 patcher 调用次数严格为 0，杜绝从评估数据集中学习。
- **用例 3（`test_pi_explicit_promotion_and_future_reuse`，3B2）**：
  - **临时 Git 仓库与显式确认门禁**：`ReleaseStateMachine` 绑定 `tmp_path` 临时 Git 仓库（`git init` + baseline commit）；`caller_confirmed=False` 时调用 `promote_repaired_skill` 抛出 `ValueError`，正式基线严格保持 1.0.0 且正文未变，CandidateStore validation record `promoted=False`。
  - **受控发布与发布后幂等防二次 release**：`caller_confirmed=True` 调用成功，注册表升级至版本 `1.0.1` 且正文更新为 `patched_body`；CandidateStore record 标记 `promoted=True`；Job 状态变更为 `PROMOTED` 并记录 `release_id`；再次调用被严格拒绝。
  - **新 Runtime 无手填 Skill ID 检索与复用**：关闭旧 Runtime，启动新 Runtime 与 ExperienceCollector；`start_run` 启用 `enable_reuse=True, require_reuse=True` 且严禁传入 `skill_name`/`candidate`；ThreeTierMemoryManager 基于任务描述自主检索命中 `logistics_query` 1.0.1，冻结正文等于 `patched_body`。
  - **实际 Pi 送模与独立业务 Oracle PASS 闭环**：`run_agent_with_pi` 执行且不传 candidate；断言 `captured_provider_inputs` 的 `systemPrompt` 包含完整 1.0.1 `patched_body` 且旧 1.0.0 `baseline_body` 不被整体包含；执行 3 次真实工具派发（order, pkg101, pkg102）且产生 3 个凭据；独立物流代码 Oracle `verify_logistics_fulfillment` 判定 `independent_pass is True` 并保留完整 `checked_invariants`；`finalize_run` 产出不可变成功 Episode（`outcome="success"`, `skill_version="1.0.1"`）；多次 finalize 幂等不重复写库。

### 4. 边界与未完成工作声明（Boundaries & Truthful Accounting）
- **Phase 3B 整体已全面完成**：已闭环验证从实际 Pi 失败 learning Episode、RepairJob 共同验证、显式人工晋升到未来无手填 Skill ID 检索复用全生命周期。
- **Phase 3C 待完成**：CLI 交互演示（demo）及最终端到端全量回归测试套件未完成，留待 Phase 3C 处理。
- **真实性审计边界**：
  - 依赖实际官方 Pi npm 进程（`@earendil-works/pi-agent-core@0.85.1` 与 `@earendil-works/pi-ai@0.85.1`）、权威 AgentRuntime、ToolBroker 与 ExperienceCollector；
  - 模型生成与受控 patcher 采用本地 faux provider 脚本夹具模拟，**未调用任何外部付费大模型 API，不可声称真实生产环境业务指标改善**；
  - 临时 Git 操作完全限制在沙箱 `tmp_path`，项目主仓库未发生任何 commit 或 push。

---

## 十一、Phase 3C CLI 演示模块实施进展汇报（2026-11-21）

### 1. 目标与交付清单
- **目标**：提供无密钥 actual Pi CLI 演示模块与入口，支持通过命令行触发完整 4-turn 交互、ToolBroker 调度、独立物流 Oracle 裁定与 SQLite 产物落盘。
- **交付清单**：
  - `src/skillforge/scenarios/pi_demo.py`：实现 `run_pi_demo(output_dir: Path) -> dict`，在输出目录下创建唯一 `pi_demo_<uuid>` 隔离目录，落地 `runtime.db`、`episodes.db`、`report.json` 与空 `skills/`；调度实际官方 Pi npm 进程与受控 faux provider，派发 `query_order_packages` 与 `query_package_tracking`，由业务 Oracle 判定并结算；
  - `src/skillforge/cli.py`：新增 `pi-demo` 子命令，带必需参数 `--output-dir`，惰性导入 `run_pi_demo`，按 Oracle pass 与 Episode success 返回 0/1 退出码，标准输出纯净合法 JSON 报告；
  - `tests/test_pi_cli.py`：Phase 3C 专项集成测试（包含真实独立子进程调用、sentinel 标记防覆盖、磁盘 report.json 一致性、Oracle 覆盖两包裹、3 次工具调用与 3 条 provenances、预算 7 验证、Episode 存储校验、诚实标签与成本 null 标记、PID 回收检测）；
  - `docs/PI_REFACTOR_PROGRESS.md`：记录 Phase 3C 核心实装、命令输出与机制边界。

### 2. 验证命令与测试结果

#### 唯一验证命令：
```bash
env -u PI_SOURCE_ROOT .venv/bin/pytest -q tests/test_pi_cli.py
```
**进程退出码：`0`**（`1 passed in 1.50s`）

### 3. 核心断言实测证据
- **独立子进程真实调用与退出码**：通过 `subprocess.run` 启动真实 Python 进程执行 `python -m skillforge.cli pi-demo --output-dir <tmp_path>`，无任何 Python 内部 mock，返回退出码 `0`；
- **目录隔离与已有文件防护**：在 `output_dir` 预先创建的 `sentinel.txt` 文件内容保持原样未被篡改；产物落地于唯一的 `pi_demo_<uuid>` 子目录；
- **纯净 JSON 与落盘报告一致性**：标准输出解析为有效 JSON 且包含 `report_path`；读取磁盘上的 `report.json` 经对比与 stdout 输出完全一致；
- **独立业务 Oracle 权威裁定**：`verify_logistics_fulfillment` 裁定 `independent_pass is True` 且 `verdict == "PASS"`，准确覆盖订单 `ORD_2026_0902` 下的 `PKG_201`（SF_EXPRESS DELIVERED 已签收）与 `PKG_202`（ZTO_EXPRESS IN_TRANSIT 运输中），未发生任何虚假全部签收幻觉；
- **真实工具调度与凭据链**：真实 `ToolBroker` 执行 3 次只读工具调用（1 次订单查询 + 2 次包裹轨迹查询），产生 3 条真实 `ToolCallProvenance` 记录；
- **共享运行预算严格扣除**：4 个模型轮次与 3 次工具调用共享预算，消耗精确记录为 `7`（上限 `10`）；
- **Episode 权威存储持久化**：通过 `EpisodeStore` 读取落盘的 `episodes.db`，确认唯一 Episode ID 匹配、`outcome == "success"` 且包含完整 3 条 provenances；
- **诚实证据标签与成本记账**：证据标签包含 `execution="actual_pi_runtime_broker_collector"`, `model="faux"`, `data="synthetic"`, `os_sandbox="not_exercised"`；由于无真实 token 计量，严格上报 `tokens=None` 与 `money=None`，拒绝使用虚假零成本冒充；
- **Pi 子进程回收机制**：报告中的 Pi PID 在运行结束时已优雅终止并被内核回收，通过 `os.kill(pi_pid, 0)` 确认抛出 `ProcessLookupError`。

### 4. 边界与后续声明（Boundaries & Truthful Accounting）
- **Phase 3C CLI 入口与演示模块已完成实装与专项验证**；
- **严禁提前声明全量回归通过**：本轮仅执行 Phase 3C 专项测试验证；最终跨阶段（Phase 1/2/3A/3B/3C）全量回归将由 Codex 随后统一执行与验收；
- **模型与数据边界**：演示使用受控 faux provider 脚本及合成物流场景数据，未接入真实商业 LLM API 或真实物流数据库。

---

## 十二、Codex 最终验收（2026-11-21）

本轮约定的 Phase 1 / 2 / 3 工程范围完成。新路径复用官方 Pi Agent SDK 模型与工具循环，Python 保留权威任务状态、冻结正文、预算、Broker、上下文压缩、Episode 与晋升规则。原 AsyncHarnessLoop / Supervisor 路径保留兼容；本轮不宣称所有旧入口均已迁移到 Pi。

### 收口中实际修复的问题

1. **来源归因**：Oracle 的 `checked_invariants` 是检查项目录，不能因为其中含 `PERMISSION` 就判为实际权限错误。修复了 RepairJob 与有界恢复的共同错误，测试保留完整 Oracle 证据，不再删除元数据绕过归因。
2. **超时经历落盘**：工具超时分支先提交 Runtime 超时状态和工具记录，再通过 Collector 连接写 Episode，避免同一 SQLite 数据库写锁导致漏记；写入异常不再静默吞掉。
3. **幂等边界**：同一 call ID、运行、工具与参数的请求复用旧结果；同 ID 改参数返回 `IDEMPOTENCY_CONFLICT`，不泄漏旧结果、不重复执行或扣预算。旧专项已补齐这两种断言。
4. **实测口径**：usage 按全部 assistant 消息中的 Pi `totalTokens` 聚合，未知则为 null；配置指纹区分提示词、期限及默认工具与显式空工具；预算拒绝的模型轮次不进入实际 Provider 请求记录。

### 联合验证命令与结果

```bash
env -u PI_SOURCE_ROOT .venv/bin/pytest -q \
  tests/test_pi_bridge.py \
  tests/test_pi_gaps.py \
  tests/test_pi_phase2a_context.py \
  tests/test_pi_phase2b_continue.py \
  tests/test_pi_phase2b_checkpoint.py \
  tests/test_pi_phase2b_recovery.py \
  tests/test_pi_phase2b_safety.py \
  tests/test_pi_phase2b_lifecycle.py \
  tests/test_pi_phase2b_context_safety.py \
  tests/test_pi_evaluation.py \
  tests/test_pi_evolution.py \
  tests/test_pi_cli.py \
  tests/test_evaluator.py \
  tests/test_criteria_judge_and_scoring.py \
  tests/test_p5_langgraph_repair_integration.py \
  tests/test_p5_bounded_recovery_and_split.py \
  tests/test_failure_attribution_and_patching.py \
  tests/test_runtime_and_tool_broker.py \
  tests/test_p2_gate_and_lifecycle.py \
  tests/test_p3_goal_shift_and_revision.py \
  tests/test_source_snapshot_and_migration_closure.py \
  tests/test_e2_pairwise_selection.py
```

Codex 实际结果：**149 passed in 58.13s，exit 0**。这是 22 个相关测试文件的联合回归，不是全仓库所有测试，也不与前文重叠通过数累加。

### 独立 CLI 运行记录

```bash
env -u PI_SOURCE_ROOT .venv/bin/python -m skillforge.cli pi-demo \
  --output-dir scratch/pi-refactor-acceptance-20261121
```

Codex 通过独立 CLI 子进程执行同一命令并核对 stdout 与磁盘 JSON 一致：exit 0、Oracle PASS、2 个包裹、3 次工具调用、4 次模型轮次、预算消耗 7/10、Episode success。Pi PID 已回收，检查使用 `ProcessLookupError`，不把权限拒绝当成进程消失。

- 本地报告：`scratch/pi-refactor-acceptance-20261121/pi_demo_7339a35a4bc54609ad7b686f355b3089/report.json`
- Run：`run_7339a35a`；Episode：`ep_run_7339a35a`。
- CLI 演示任务内 Draft 试用；完整 RepairJob → 共同验证 → 显式晋升 → 新任务无手填 ID 复用由 `tests/test_pi_evolution.py` 的实际集成链验证。
- README 保留原有展示风格与三条核心设计主线，更新 Pi 分工、Node 依赖安装和 CLI 复现命令；项目描述同步为 Pi Agent SDK。

### 证据与责任边界

本轮运行了实际官方 Pi npm 进程、Runtime / Broker / Collector、临时 SQLite 与临时 Git 发布流程；模型、Judge 和 patcher 使用受控响应，物流数据为合成数据。未新增真实模型效果、服务端 KV Cache 命中或 OS 沙箱验证结论。运行恢复覆盖 Runtime 重建和新 Pi 进程，不等同于外部业务系统的 exactly-once；Git 与 SQLite 也不构成跨资源原子事务。没有修改正式 Skill 库、提交或推送项目代码，没有新增自动化轮询或付费模型调用。


---

## 十三、Phase 4 规划与 Phase 4A1 实施进展汇报（2026-11-21）

### 1. Phase 4 总体规划与状态追踪表

| 子阶段 | 目标与范围 | 状态 | 边界与交付成果 |
| :--- | :--- | :--- | :--- |
| **Phase 4A1** | **CLI evaluate 显式接入 Pi 后端与通用装配** | **PASSED** | 1. `evaluate` 子命令新增 `--execution-backend {legacy,pi}`（默认 `legacy` 向后兼容）；<br>2. 接入原生 Pi 配置（`--pi-provider`, `--pi-model`, `--pi-scenario`）；<br>3. 实现通用运行时装配上下文管理辅助模块 `src/skillforge/pi_assembly.py`，支持 scenario (`logistics`) 白名单校验与 Skill declared dependencies 前置拦截；<br>4. 统一 `_format_and_return_eval_result` 安全格式化 nullable 指标（`tokens`/`money` 为 unknown/null，绝不伪造 0.00）；<br>5. 异常与正常退出全链路清理 SQLite 与进程回收，无僵尸进程；<br>6. 专项新增初始化失败释放断言；Codex 复跑 12 passed in 12.11s，exit 0。 |
| **Phase 4A2** | **SkillEvolver / evolve CLI 接入 Pi 执行与共同门禁** | **PASSED** | 4A2a 统一单轮请求账本与 4A2b evolve 装配/克隆接入闭环完成，43 passed in 14.05s。 |
| **Phase 4B0** | **Pi 生命周期 Hook 兼容** | **PASSED** | 原生请求/回执、工具派发与最终返回接入已有 HarnessHooks，父 deny 优先，脱敏视图不改原始凭据，27 passed in 21.76s。 |
| **Phase 4B1** | **Supervisor Worker 显式 Pi 执行与权限预算收口** | **PASSED** | WorkerAgentSpec 接入 execution_backend="pi" 与显式 model_config；父子工具交集权限收敛（父为空不当无限权限）；独立 Runtime 连接与唯一 child run ID；权威 RunRecord.budget_consumed 增量扣除；单次绝对 deadline 继承与过期拦截；应用 verification_fn 证据裁定终态（缺 oracle 记 unknown，业务 fail 停机阻断）；级联取消与 PID 回收；6 passed in 4.33s。 |
| **Phase 4B2** | **跨进程编排 resume 与持久化恢复** | **PASSED** | Codex 补充恢复窗口反例，修复权威预算补记、损坏检查点拒绝及完整工具 schema 绑定；专项7项及相关17项通过。 |
| **Phase 4C** | **相关场景回归与文档收口** | **PASSED** | Codex 最终25文件173项执行治理回归、7文件79项既有业务门禁回归均exit0；README与本记录同步，见第十九节。 |

### 2. Phase 4A1 交付清单与文件变更
- **新增模块**：
  - `src/skillforge/pi_assembly.py`：
    - `validate_pi_scenario_and_dependencies`：严格前置校验场景与技能依赖；
    - `assemble_pi_resources`：上下文管理器，装配 AgentRuntime、ToolBroker（白名单工具）、ExperienceCollector、EpisodeStore、CandidateStore、SkillRegistry 与 PiEvaluationBackend；
    - `PiResourceBundle`：统一管理资源生命周期与优雅清理，子进程可靠回收。
  - `tests/test_pi_cli_evaluate.py`：包含 7 大核心场景的完整命令行 parser / main() 集成测试套件。
- **改动文件**：
  - `src/skillforge/cli.py`：
    - `evaluate` 子命令增加 `--execution-backend`, `--pi-provider`, `--pi-model`, `--pi-scenario`；
    - `cmd_evaluate` 实现 Pi 路径调度与 legacy 路径严格隔离，非法参数与不兼容依赖前置拦截（exit 2）；
    - `_format_and_return_eval_result` 修复 nullable 指标输出（拒绝假 0）并保持退出码语义（critical_fail 退出 1，invalid 退出 2）；
    - 统一资源 cleanup 与 Pi 子进程回收。
  - `src/skillforge/__init__.py`：
    - 导出 `PiEvaluationBackend`, `PiEvaluationRunResult`, `assemble_pi_resources`, `validate_pi_scenario_and_dependencies`, `PiResourceBundle`。
  - `src/skillforge/evaluator/__init__.py`：
    - `evaluate_skill` 默认继承实例绑定的 `self.runtime` 与 `self.tool_broker`。

### 3. 验证命令与测试结果

#### 唯一验证命令：
```bash
env -u PI_SOURCE_ROOT .venv/bin/pytest -q tests/test_pi_cli_evaluate.py tests/test_pi_cli.py tests/test_pi_evaluation.py
```
**进程退出码：`0`**
**测试输出：`11 passed in 11.81s`**

### 4. 专项测试矩阵（7/7 全部通过）

| 测试用例名 | 覆盖场景与核心断言 | 实际结果 |
| :--- | :--- | :--- |
| `test_pi_cli_evaluate_success_e2e` | 完整 CLI Pi evaluate 端到端：parser 入口、真实 Pi 子进程、3 次工具调用凭据、EpisodeStore 记录子 run `purpose='evaluation'` 与 `outcome='unknown'`、冻结正文注入、独立物流 Oracle PASS、nullable 指标 unknown 展示、正式 Registry 未篡改、Pi PID 干净回收 | **PASSED** |
| `test_pi_cli_evaluate_oracle_critical_failure_fails_nonzero` | 虚假送达断言（全部签收但 PKG_102 在途）：物流代码 Oracle 硬拦截，critical_fail 无法被 Judge 消除，CLI 退出码为 1，Registry 未被污染，PID 回收 | **PASSED** |
| `test_pi_cli_evaluate_legacy_default_no_pi_spawned` | 默认 legacy 模式走原评估器且零 Pi 进程；误传 Pi 参数返回 2，不能静默忽略 | **PASSED** |
| `test_pi_cli_evaluate_missing_or_invalid_args_fail_upfront` | 缺少必需参数（provider/model/scenario）或未知场景：在创建任何数据库文件或调用模型前前置拦截，退出码 2，无静默降级 | **PASSED** |
| `test_pi_cli_evaluate_incompatible_skill_deps_fail_upfront` | 技能声明不支持的外部依赖（如 `unsupported_payment_gateway`）：前置拒绝，不创建运行时数据库，退出码 2 | **PASSED** |
| `test_pi_cli_evaluate_production_rejects_faux_and_scripts` | 生产模式（非 test 环境）下使用 `provider='faux'`：严格报错并退出码 1，杜绝在生产中运行受控测试夹具 | **PASSED** |
| `test_pi_cli_evaluate_cleanup_on_exception` | 评估过程中抛出意外异常：触发完整 context manager `bundle.close()` 清理，SQLite 连接安全关闭，Pi 子进程回收，退出码 1 | **PASSED** |

### 5. 证据与责任边界（Boundaries & Truthful Accounting）
- **真实性审计边界**：
  - 本轮执行了真实官方 Pi npm 进程（Node >= 22）、AgentRuntime、ToolBroker（真实执行 logistics 包裹查询工具）、ExperienceCollector 与 SQLite 持久化；
  - 模型生成采用本地受控夹具，Judge 采用 FakeLLM，物流数据为合成测试数据；未产生任何真实商业付费大模型调用，未产生任何网络外部调用；
  - 严格遵守主目录保护与代码隔离原则，未执行 git commit / push / restore / reset / stash，主仓库代码保留在工作区暂存基线中；
  - Phase 4A1 范围已完全收敛，Phase 4A2（evolve 集成）、4B（Supervisor 迁移）与 4C 保持 PENDING，未越界执行。




### Codex 4A1 收口复核

- 用 ExitStack 在每个资源成功初始化后注册 close，覆盖 backend 初始化失败、尚未 yield 时的连接释放；去掉无 wait 的备用 terminate 和静默吞异常。新增初始化异常专项。
- legacy 下误传 Pi 参数返回 2，原默认无参数命令继续兼容。
- Codex 实际执行上方同一三文件命令：**12 passed in 12.11s，exit 0**；git diff --check 通过。覆盖机制执行，未产生真实模型调用。
- 4A2 因顶层预算不能漏计 Pi 模型请求，再分为预算桥接与 CLI / 克隆接线两项；4B/4C 未启动。

## 十四、Phase 4A2a：Pi 单轮请求接入统一账本（Codex 收口）

前两次派工已终止；其代码只作为未验收中间产物保留，未回滚已通过的 Phase 1–3 / 4A1。之后依据实际失败堆栈完成定点修复，不以步数或通过数量代替断言。

- Pi runner 使用原生 assistant message_end 发出 turn_receipt；Runtime 先检查本地模型/工具步骤预算，再执行可选顶层 admission；PiEvaluationBackend 将每个 run/turn 对应到同一个 LLMLedger 预留与结算记录。
- 已有 Judge 调用与 Pi 每轮请求共享 calls/token/deadline；超限前下一轮不送 Provider。未知 token 在报告为 null，若配置 token 上限则缺 usage 阻断后续调用。异常保留尝试记录；重复回执不重复记账。
- 修复 `prompt_tokens=None`：总 usage 未知不能覆盖已经测得的输入分量；输入/输出未知分别判断。
- 修复 `assert 287 == 120` 的错误期望：官方 faux Provider 以实际上下文重算 usage，账本须匹配原生回执，而非脚本预填的 120。删除用测试预填 usage 覆盖原生回执的中间方案；测试核对真实回执总量和超限后无第二个 Provider 请求。
- faux 响应通过构造参数保留原始 errorMessage；错误消息不再只剩通用 error。已结算 ID 的清理不能误取消其他 run 的 pending 记录；账本回执异常后不再处理后续派发。

Codex 执行：`env -u PI_SOURCE_ROOT .venv/bin/pytest -q tests/test_pi_budget_ledger.py` → **7 passed in 3.59s，exit 0**。actual Pi / Runtime / Broker，受控模型响应，无真实付费调用。4A2b evolve 装配与克隆仍待接入，4B/4C 尚未启动。

## 十五、Phase 4A2b：evolve CLI 与候选隔离评测（Codex 收口）

- `evolve --execution-backend pi --pi-provider … --pi-model … --pi-scenario logistics` 复用共同装配；默认 legacy 不启动 Pi。Pi 顶层默认 200 调用、600 秒，影子模式且不自动发布；显式预算参数正值校验，错误退出非零。
- SkillEvolver 将同一 LLMLedger 绑定到 Pi 后端、修补模型与 Judge；候选临时 evaluator 保留 execution_backend/runtime/tool_broker，构造失败不能退回 legacy。候选工具证据与依赖证据合并保留。
- AGY 因 Bridge 硬限停止后，Codex 核对实际失败：夹具给初版与裸模型相同正确答案，不存在可修业务失败；根因匹配还误截候选生成请求。修正为仅初版受控回答“全部签收”，由独立物流 Oracle 判错，裸模型和候选返回真实合成状态；可读性保持 tied，不放松产品判据。
- 新专项证明真实 CLI→SkillEvolver→候选临时克隆→官方 Pi 进程→Broker，候选正文被消费、正式正文不变、PID 回收。预算专项核对 Pi/patcher/Judge 账本同实例、cap=1 仅一个 Provider 输入、后续请求被拦截。
- Codex 执行 `env -u PI_SOURCE_ROOT .venv/bin/pytest -q tests/test_pi_cli_evolve.py tests/test_pi_cli_evaluate.py tests/test_pi_budget_ledger.py tests/test_budget_and_cache.py` → **43 passed in 14.05s，exit 0**；11 条外部 Pydantic 弃用提示。
- 边界：实际 Pi/Runtime/Broker，模型与 Judge 受控夹具，数据 synthetic；无付费模型或 OS 隔离新验证。4A 已完成；4B Worker 迁移及4C回归文档收口待实施。

## 十六、Phase 4B0：Pi 生命周期 Hook 兼容

- 在 Pi 原生请求/回执、工具派发和最终返回处接入已有 HarnessHooks，复用组合规则与审计；压缩 Hook 保留。模型回执先结算统一账本，再发送结构化观察，不新增思维流暴露。
- 父级 deny 优先、工具拒绝记录持久化；脱敏视图清空未过滤的结构化 data，原始工具凭据不变；review 中止排队工具，持久状态 INTERRUPTED/REVIEW_REQUIRED，不能被 abort 误写成 CANCELLED。task_end 在正常/异常各一次、进程回收。
- Bridge 硬限后 Codex 按真实断言修正两处夹具：组合审计包含父级及组合层；缺 usage 为未知，只有显式带 usage 容器时才使用官方 faux 的原生估算。未覆盖或编造真实 Provider usage。
- Codex 执行 `env -u PI_SOURCE_ROOT .venv/bin/pytest -q tests/test_pi_hooks.py tests/test_pi_budget_ledger.py tests/test_pi_cli_evolve.py tests/test_pi_phase2b_lifecycle.py tests/test_pi_phase2b_recovery.py` → **27 passed in 21.76s，exit 0**，4 条外部弃用提示。其中 Hook 专项9种情形，含两种脱敏视图与pre/post-tool review排队隔离。
- 实际 Pi/Runtime/Broker；本地受控模型，无真实模型新实验。4B1 Supervisor Worker 接线及4B2编排恢复待做。

## 十七、Phase 4B1：SkillForge Supervisor Worker 显式 Pi 执行与权限预算收口（2026-11-21）

### 1. 架构定位与改动清单

- **定位**：Supervisor 保持 Python 原生编排与状态路由，不重写 LangGraph；仅将 Worker 执行适配至原生 Pi Agent SDK 后端。
- **改动文件**：
  - `src/skillforge/supervisor.py`：
    1. `WorkerAgentSpec` 追加 `execution_backend: Literal["legacy", "pi"] = "legacy"`, `pi_model_config: Optional[dict] = None`, `pi_test_mode: Optional[bool] = None`, `pi_timeout: float = 30.0`；旧用法完全兼容；Pi 模式必须显式指定 `pi_model_config`，缺失或非法直接报错，不隐式 fallback。
    2. 工具权限交集约束：`scoped_allowlist = sorted(list(set(worker_spec.allowed_tools) & set(self.tool_broker.application_allowlist or set())))`。父白名单为空时不当无限权限；`ProtectedState.allowed_tools` 与模型暴露工具使用同一交集；只复制该集工具实例到 `scoped_broker`，禁止 `register_tool` 越权；继承父级 sandbox/prober，绝不修改父级 `tool_broker`。
    3. 独立 Child Runtime 与隔离会话：Pi Worker 建立独立 `AgentRuntime` 连接（同 `db_path`、`scoped_broker`，继承 registry/deployment_manager/collector/episode_store/candidate_store/memory_manager）；只 close 自身连接，不 close 父共享 store。每次派工分配全局唯一 `child_run_id` 并落盘。`start_run` 标记 `purpose="evaluation"`、`budget_max=min(worker_max, remaining)`，不重置父预算。
    4. 权威预算扣除与期限继承：以权威 `child RunRecord.budget_consumed`（包含 model turns + tool calls）增量计入父级 `global_budget_consumed`，超限立即停止；`SupervisorTaskSpec` 引入单次绝对 `deadline_seconds` 并计算 `SupervisorState.deadline_ts`，child 继承同一 deadline 且过期不派工。
    5. 结果裁定与业务门禁：Pi 返回文字后通过应用 `verification_fn(child_harness)` 裁定；未提供 oracle 则标记 outcome 为 `unknown`（严禁自报 SUCCESS）；业务验证失败立即将父级标记为 `FAILED` 并中断路由；异常/review/cancel 状态精准落盘并中断，不把失败当成功继续路由；`asyncio.CancelledError` 级联取消 Pi 进程，触发 PID 回收，并在 finally 中写入 checkpoint 与记录已消耗预算。
    6. Legacy 兼容：默认 `execution_backend="legacy"` 完全沿用既有 `AsyncHarnessLoop`，零 Pi 进程启动。
  - `tests/test_pi_supervisor.py`（新增）：
    - 覆盖 6 项核心紧凑场景：两 worker 合成物流读取与隔离闭环、越权工具 schema 过滤与 composed 拒绝、预算超限阻断后续 Provider 与 deadline 拦截、运行中取消级联与 PID 回收、verification_fn 真实业务裁定拦截虚假 PASS、legacy 默认零 Pi 进程兼容。

### 2. 唯一验证命令与测试结果

```bash
env -u PI_SOURCE_ROOT .venv/bin/pytest -q tests/test_pi_supervisor.py
```

**测试输出**：
```
......                                                                   [100%]
6 passed in 4.33s
```
**进程退出码：`0`**

### 3. 专项测试矩阵（6/6 全部通过）

| 测试用例函数名 | 覆盖场景与核心断言 | 实际结果 |
| :--- | :--- | :--- |
| `test_case1_two_workers_synthetic_logistics_isolation_and_unknown_episode` | 两 worker 实际读 synthetic 物流（order 与 tracking），Python 路由保留，约束与各 prompt/tool schemas 严格隔离，父 broker 对象与 allowlist 未变，独立 child run ID，无 oracle 时 child episode outcome 记为 unknown | **PASSED** |
| `test_case2_unauthorized_tool_decloaked_and_composed_hook_denial` | worker 声明超出父权限工具，模型 schema 彻底排除；即便 faux 尝试调用未授权工具 handler 0 执行；compose 父 deny + 子 allow 不绕过，REJECTED 记录落盘 | **PASSED** |
| `test_case3_shared_budget_exhaustion_stops_subsequent_provider_and_deadline` | 父总预算 5，worker1 消耗 3（model+tool+model），worker2 仅余 2，后续 provider 未送出，total consumed <= 5 且父状态 BUDGET_EXHAUSTED；过期 deadline 阻止派工（step_count==0） | **PASSED** |
| `test_case4_cancellation_cascades_to_pi_and_reaps_pid_with_checkpoint` | 等待中的 Pi 任务触发取消，父 CANCELLED、child CANCELLED、Pi 子进程 PID 回收（ProcessLookupError），已产生副作用保留且检查点持久化 | **PASSED** |
| `test_case5_verification_fn_controls_outcome_and_blocks_false_pass` | 模型声称“全部送达”（但 PKG_102 在途）：真实 verification_fn 判定失败，child 标记 failure，父状态清晰 FAILED 且中断路由，模型自报 PASS 被硬拦截 | **PASSED** |
| `test_case6_legacy_worker_default_does_not_launch_pi` | 默认 legacy 模式完全兼容原 AsyncHarnessLoop，零 Pi 客户端与子进程拉起 | **PASSED** |

### 4. 边界与未完成工作声明（Boundaries & Truthful Accounting）

- **Phase 4B1 已完成**：Supervisor Worker 显式 Pi 执行与权限/预算/取消收敛闭环。
- **Phase 4B2 已完成**：跨进程编排 resume 与持久化恢复收口（见第十八节）。
- **Phase 4C PENDING**：全流程回归与文档收口待实施。
- **真实性审计边界**：
  - 依赖实际官方 Pi npm 进程（Node >= 22）、AgentRuntime、ToolBroker 与 SQLite 持久化；
  - 模型生成采用本地受控 faux 夹具，物流数据为合成测试数据；未产生任何真实商业付费大模型调用，未产生任何网络外部调用；
  - 未执行 git commit / push / restore / reset / stash，主目录与代码边界严格受保护。

---

## 十八、Phase 4B2：SkillForge Supervisor 编排持久恢复与安全断点（2026-11-21）

### 1. 架构定位与改动清单

- **定位**：在保持原生 Python Supervisor 状态机与路由器的前提下，补全跨进程崩溃恢复与确定性断点重启机制。杜绝跨重启预算清零、历史丢失、已完成动作重跑或未知状态盲目重试。
- **核心文件改动**：
  - `src/skillforge/supervisor.py`：
    1. **显式 Resume 契约与防覆盖门禁**：
       - `run(spec: SupervisorTaskSpec, resume: bool = False)`：默认 `resume=False`，若检测到该 `task_id` 存在已运行未完检查点，显式报错拒绝静默覆盖并重置预算（要求显式 `resume=True` 或新 `task_id`）。
       - 检查点持久化增加 `version=1`，记录 `max_supervisor_steps`、单次计算的绝对 `deadline_ts`、全局已耗预算 `global_budget_consumed`、`worker_history`、`worker_results`、`active_child_run_id` 及 MemoryIndex 引用。
    2. **原子落盘与写失败强阻断**：
       - `_save_checkpoint` 采用 `tmp_file`（唯一临时文件名）写完后通过 `os.replace` 原子替换原文件；
       - 若落盘失败，显式抛出 `RuntimeError`，阻断后续 Worker 的执行（绝不单纯 `logger.warning` 后继续派发）。
    3. **契约指纹（Contract Fingerprint）防漂移校验**：
       - `_compute_contract_fingerprint` 对 `user_query`、`intent_version`、`global_constraints`、`global_budget_max`、`max_supervisor_steps` 以及所有 worker 的 `name`、`system_prompt`、`execution_backend`、生效工具交集 `scoped_tools`、工具参数 schemas、公开 non-secret `model_config` 进行 SHA-256 指纹；
       - resume 时强比对当前配置指纹；任何意图变更、约束变更、父权限收缩/扩展、工具参数 schema 变化、worker 提示词篡改、模型配置改动均严格抛出 `ValueError("STALE_SUPERVISOR_CHECKPOINT: ...")` 拒绝旧恢复；且绝对不重算 `deadline_ts` 或续期。
    4. **终态不可复活与期限超限拦截**：
       - 已进入 `COMPLETED`、`FAILED`、`CANCELLED`、`TIMED_OUT`、`BUDGET_EXHAUSTED`、`PAUSED_FOR_REVIEW` 终态的任务禁止恢复；
       - 恢复时若已超过单次绝对 `deadline_ts`，立即抛出 `TimeoutError` 并置为 `TIMED_OUT`。
    5. **派发前检查点与权威 SQLite 增量记账**：
       - 每次 Pi Worker 派发前先持久化记录 `active_child_run_id` 与决策；
       - Worker 退出或异常后以 child SQLite 的 `RunRecord.budget_consumed` 为基准累计父级预算，恢复后全局预算与路由步数不归零、不双扣；
       - 崩溃恢复时，若 child 处于 `COMPLETED`，从已存 Episode/持久工具调用记录回收产物与历史，不重新派发已完成 Worker。
    6. **In-Flight PAUSED 安全接续与审查兜底（Fail-Closed to Review）**：
       - 若 child 处于 `PAUSED` 且有有效 Pi 检查点，且无未决 `ADMITTED` 工具调用，调用 `aresume_agent_with_pi` 接续同一 `child_run_id`，父级仅计入增量消费差额 `delta = child_rec.budget_consumed - initial_consumed`，工具不重跑；
       - 若存在未决 `ADMITTED` 悬空工具调用、或 child 处于 `RUNNING`/`INTERRUPTED` 但无有效检查点：一律停在 `PAUSED_FOR_REVIEW` (`REVIEW_REQUIRED`)，绝不盲目重派或伪造完成；人工 Review 不可通过简单 resume 跳过；
       - Child runtime 共享父级 `_pause_requested_runs` 集合，支持外部对正在执行的 Pi Worker 发起协作暂停。

### 2. 唯一验证命令与测试结果

```bash
env -u PI_SOURCE_ROOT .venv/bin/pytest -q tests/test_pi_supervisor_recovery.py
```

**测试输出**：
```
.....                                                                    [100%]
5 passed in 3.01s
```
**进程退出码：`0`**

**联合回归命令（4B1 + 4B2 整体验证）**：
```bash
env -u PI_SOURCE_ROOT .venv/bin/pytest -q tests/test_pi_supervisor.py tests/test_pi_supervisor_recovery.py
```
**测试输出**：
```
...........                                                              [100%]
11 passed in 6.87s
```
**进程退出码：`0`**

### 3. 4B2 专项测试矩阵（5/5 全部通过）

| 测试用例函数名 | 覆盖场景与核心断言 | 实际结果 |
| :--- | :--- | :--- |
| `test_case1_interrupt_after_worker1_resume_runs_only_worker2` | 实际完成 worker1 后中断编排（关闭原 Runtime），新建 Runtime+Supervisor 同目录 resume：仅执行 worker2，worker1 handler 不重复、全局预算/路由步数不重置、原 deadline 严格一致、历史无损 | **PASSED** |
| `test_case2_in_flight_pi_worker_paused_resumes_with_same_child_id` | Pi worker 工具执行后协作请求 pause，Supervisor 安全停在 PAUSED 且记录 `active_child_run_id`；新实例 resume 保持同一 child id 调用 `aresume_agent_with_pi`，工具不重复调用，父级仅计增量预算，最终 COMPLETED | **PASSED** |
| `test_case3_contract_drift_and_tampering_rejected` | 用户 query/intent/constraints/父权限/worker prompt/工具 schema 发生变更均被契约指纹拒绝恢复；新 spec 不能扩展 budget 或续期 deadline；未恢复的检查点阻断无 resume 标志的二次执行 | **PASSED** |
| `test_case4_unsettled_admitted_tool_calls_halt_in_review` | 在途 `ADMITTED` 悬挂工具调用或无检查点状态强制停在 `PAUSED_FOR_REVIEW` (`REVIEW_REQUIRED`)，工具 handler 执行次数为 0，拒绝跨状态盲目重试 | **PASSED** |
| `test_case5_terminal_status_deadline_expired_and_write_failure` | 已进入终态的任务拒绝 resume；绝对 deadline 超期拒绝恢复并超时终结；检查点写入失败（只读路径）抛出 RuntimeError 且阻断 Worker 派发 | **PASSED** |

### 4. 边界与未完成工作声明（Boundaries & Truthful Accounting）

- **Phase 4B2 已完成**：Supervisor 编排跨进程持久恢复、契约指纹校验、安全暂停接续与审查兜底完全闭环。
- **Phase 4C PENDING**：全流程端到端回归与文档收口待实施。
- **真实性审计边界**：
  - 依赖实际官方 Pi npm 进程（Node >= 22）、AgentRuntime、ToolBroker 与 SQLite 持久化；
  - 模型生成采用本地受控 faux 夹具，物流数据为合成测试数据；未产生任何真实商业付费大模型调用，未产生任何网络外部调用；
  - 未执行 git commit / push / restore / reset / stash，主目录与代码边界严格受保护。

### Codex 4B2 补充复核

- 原5项覆盖正常保存后的恢复，未覆盖子任务完成/暂停但父账本尚未提交的窗口。新增每child已记账值，恢复先对照SQLite规范RunRecord补记，再续执行或收取结果；不会漏计或双扣。
- 原收取路径误读不存在的 Episode.model_output。改为在finalize前原子保存实际输出，再以规范Episode确认完成状态；缺任何确认即review，不重新调模型或工具。恢复history也标记已确认状态。
- 损坏JSON或缺失契约fingerprint不能静默覆盖/恢复。绑定完整工具JSON schema（含required）、模型公开端点与参数；实际内存索引对象覆盖率随检查点保存。
- Legacy维持原循环步骤口径，其Runtime只计工具；Pi以模型+工具权威步骤计数。两者不冒充统一的全部Provider token/费用账。
- Codex执行 `env -u PI_SOURCE_ROOT .venv/bin/pytest -q tests/test_pi_supervisor_recovery.py tests/test_pi_supervisor.py tests/test_supervisor_orchestration.py` → **17 passed in 7.88s，exit 0**。新恢复专项7项；实际新Runtime/新Pi进程，模型为faux。
- 本轮模拟协作暂停与进程重建、注入父子持久化窗口，不宣称真实断电、跨资源事务或任意崩溃ExactlyOnce；未决工具动作仍人工复核。

## 十九、Phase 4C：最终回归与 README 收口（2026-11-22）

**本次限定 Phase 4 已完成。** 4A 显式 CLI 与共享账本、4B 生命周期 Hook / Worker 执行 / 编排恢复全部接通；legacy 仍为兼容默认，不直接删除旧引擎。

### 最终验证命令与结果

执行、治理与兼容入口的 25 文件联合回归：

```bash
env -u PI_SOURCE_ROOT .venv/bin/pytest -q \
  tests/test_pi_bridge.py \
  tests/test_pi_phase2a_context.py \
  tests/test_pi_phase2b_checkpoint.py \
  tests/test_pi_phase2b_context_safety.py \
  tests/test_pi_phase2b_continue.py \
  tests/test_pi_phase2b_lifecycle.py \
  tests/test_pi_phase2b_recovery.py \
  tests/test_pi_phase2b_safety.py \
  tests/test_pi_evaluation.py \
  tests/test_pi_evolution.py \
  tests/test_pi_cli.py \
  tests/test_pi_gaps.py \
  tests/test_pi_cli_evaluate.py \
  tests/test_pi_budget_ledger.py \
  tests/test_pi_cli_evolve.py \
  tests/test_pi_hooks.py \
  tests/test_pi_supervisor.py \
  tests/test_pi_supervisor_recovery.py \
  tests/test_supervisor_orchestration.py \
  tests/test_runtime_and_tool_broker.py \
  tests/test_budget_and_cache.py \
  tests/test_evolver.py \
  tests/test_harness_business_hooks.py \
  tests/test_p5_langgraph_repair_integration.py \
  tests/test_p5_bounded_recovery_and_split.py
```

Codex 实际执行：**173 passed, 11 warnings in 79.67s，exit 0**。11 条为 hello_agents 的 Pydantic dict 弃用提示。原始输出 `scratch/pi-phase4-acceptance-20261122/pytest.log`；命令、时间、退出码记录 `scratch/pi-phase4-acceptance-20261122/test_receipt.json`（本地 scratch，未纳入提交）。

既有评测、修补、来源与用途隔离的 7 文件回归：

```bash
env -u PI_SOURCE_ROOT .venv/bin/pytest -q \
  tests/test_evaluator.py \
  tests/test_criteria_judge_and_scoring.py \
  tests/test_failure_attribution_and_patching.py \
  tests/test_p2_gate_and_lifecycle.py \
  tests/test_p3_goal_shift_and_revision.py \
  tests/test_source_snapshot_and_migration_closure.py \
  tests/test_e2_pairwise_selection.py
```

Codex 实际执行：**79 passed in 22.14s，exit 0**。两批共 32 个不同的相关测试文件，不是全仓库所有测试；不与前阶段重叠计数相加。

### 最后发现并修复的兼容缺口

首轮联合结果为 172 passed / 1 failed：旧 Python 循环把自身预算耗尽记录成 Runtime COMPLETED，导致显式追加预算的旧恢复测试被拒绝。仅在 legacy 循环的预算检查点保存 Runtime PAUSED，不提前产出完成 Episode；必须显式给出更大预算才能续，历史步骤、拒绝审计与期限保留。Runtime / Pi 的 CANCELLED、COMPLETED 等终态拒绝规则未放宽。

暂停后用于 Oracle 的 HarnessState 补入规范持久工具记录，父历史合并暂停前后的 Hook 审计；恢复专项验证已完成工具无需重复即可供独立谓词验证。对实际断电、未确认外部副作用不作恰好一次保证，无法确认即人工核对。

### 展示与边界

- README 保留项目三大设计与面试展示风格，更新 Pi 前后变化、evaluate/evolve 参数、Worker 显式配置、恢复语义与可选后续工作；本地链接检查无缺失，git diff --check 通过。
- 实际运行官方 Pi 进程、Python Runtime/Broker/Collector、SQLite、隔离候选与检查点；模型、Judge、修补生成使用本地受控响应，物流数据 synthetic。既有真实模型结果仍单列，不新增模型效果、Provider 缓存命中或 OS 隔离结论。
- 无付费模型调用、安装、正式技能库发布、项目 commit/push；无心跳轮询任务。可选后续为其他业务装配与单列的 Pi 真实模型实验，本阶段没有剩余必做子项。

---

## 二十、Phase 5A：Pi 本地真实模型冒烟与真实工具调用验证（2026-11-22）

**本次限定 Phase 5A 已完成。** 实现了 Pi 官方 SDK 本地自定义模型路由（基于 `~/.pi/agent/models.json`），完成了真实大模型（DeepSeek V4 Flash）在物流多包裹业务场景下的真实网络工具派发闭环、独立业务 Oracle 核验与完整治理账本。

### 1. 核心修改与实现（严格限定 2 个核心文件 + 1 个测试文件）

1. **执行桥接模型路由与参数校验（`src/skillforge/pi_runner/runner.ts`）**：
   - 保留原有内置模型 catalog 查找路径；内置未命中时从 `~/.pi/agent/models.json`（支持 `PI_AGENT_DIR`）精确匹配 provider 与 model_id，绝无静默 fallback。
   - 使用官方 Pi `streamSimple` 处理真实 OpenAI 兼容 API；继承 provider `api`、`baseUrl`、`headers`，缺失字段、未知 API 协议明确报错。
   - 凭据安全隔离：`apiKey` 仅在 Node 闭包中通过 `options.apiKey` / `getApiKey` 传递，绝不嵌入 `targetModel`、`provider_input`、桥帧或持久化数据库；明确拒绝 `'!'` 命令执行引用；环境变量引用（`$VAR`）安全解析。
   - 参数严格前置校验：`max_tokens`（正整数，默认 2048）、`temperature`（非负浮点）、`max_retries`（0）、`timeoutMs`（120000），禁止 NaN / 负数放行。
   - 推理设置：按模型定义支持情况选择 `minimal` 或 `off`；桥接帧中清洗 thinking 文本，严禁泄露完整 thinking 内容。

2. **真实场景冒烟模块（`src/skillforge/scenarios/pi_live_smoke.py`）**：
   - 独立入口 `python -m skillforge.scenarios.pi_live_smoke --output-dir scratch/...`，不在默认 pytest 中触发真实网络请求。
   - 严格三项固定用例：
     1. `ORD_2026_0901`：双包裹（PKG_101、PKG_102）DELIVERED 正常签收。
     2. `ORD_2026_0902`：部分签收（PKG_201 DELIVERED、PKG_202 IN_TRANSIT）。
     3. `ORD_2026_0902`：通过 `QueryPackageTrackingTool(simulated_failing_packages={"PKG_202"})` 故障注入，检验抗幻觉与客观报告能力。
   - 全局共享准入网关（`SharedSmokeAdmissionGate`）：最多 16 次网络请求，整轮绝对截止时间 <= 900s，单请求超时 120s，最大总输入 128,000 tokens / 输出 32,768 tokens。
   - 独立权威 Oracle（`verify_logistics_fulfillment`）核验 5 项硬性不变式与 `STATUS_ONLY` 意图约束。
   - 隔离落盘与原子报告：独立临时 SQLite 数据库，每项用例执行后立即原子保存单项记录，最终汇总输出 `report.json` 与 `report.jsonl`，费用权威记为 null（不伪造账单）。

3. **离线定点测试套件（`tests/test_pi_local_model.py`）**：
   - 基于本地 HTTP stub 与真实 SSE 事件流，测试官方 Pi SDK 自定义 API 选路、`max_tokens`/`temperature` 传递、密钥不外泄、非法配置拒绝（缺失模型、`!` 引用、未知 API、负数参数）及共享准入网关逻辑。

### 2. 验证执行命令与结果

#### (1) 定点测试套件回归（全离线 HTTP Stub）
```bash
env -u PI_SOURCE_ROOT .venv/bin/pytest -q tests/test_pi_local_model.py
```
**进程退出码：`0`**（`7 passed in 3.59s`）

#### (2) 真实模型场景冒烟执行（实际网络调用）
```bash
.venv/bin/python -m skillforge.scenarios.pi_live_smoke --output-dir scratch/pi_live_smoke_phase5a
```
**进程退出码：`0`**（耗时 `9.11s`）

**报告落地路径**：`scratch/pi_live_smoke_phase5a/pi_live_smoke_7034e996/report.json`

### 3. Phase 5A 首轮记录（故障协议与统计口径的复核修正见第二十一节）

本表 Token 数字是**最后一轮响应的 usage**，不是整项用例累计量。Case 3 实际遇到的是工具错误响应构造 TypeError，不能据此声称已验证预设的结构化物流服务故障；原始记录保留，正确故障协议已另行补测。

| 用例编号 | 订单号与场景 | 实际调用工具 | 模型最终回答状态 | Oracle 裁决 | 耗时 | 请求数 | Token 使用 |
| :--- | :--- | :--- | :--- | :--- | :--- | :--- | :--- |
| **Case 1** | `ORD_2026_0901`（两包裹全签收） | `query_order_packages`<br>`query_package_tracking(PKG_101)`<br>`query_package_tracking(PKG_102)` | 准确逐个列出 PKG_101 (DELIVERED)、PKG_102 (DELIVERED)，未给多余行动建议 | **PASS**（TRUE_POSITIVE，全部签收） | 3.22s | 3 次 | In: 168, Out: 56<br>(Cache: 896) |
| **Case 2** | `ORD_2026_0902`（部分签收） | `query_order_packages`<br>`query_package_tracking(PKG_201)`<br>`query_package_tracking(PKG_202)` | 准确逐个列出 PKG_201 (DELIVERED)、PKG_202 (IN_TRANSIT)，未越权声称全签收 | **PASS**（TRUE_POSITIVE，部分签收未冒充全部） | 3.25s | 3 次 | In: 169, Out: 52<br>(Cache: 896) |
| **Case 3** | `ORD_2026_0902`（PKG_202 注入故障） | `query_order_packages`<br>`query_package_tracking(PKG_201)`<br>`query_package_tracking(PKG_202)` (ERROR) | 准确报告 PKG_201 (DELIVERED)，客观陈述 PKG_202 查询失败/状态暂不可用，无幻觉编造 | **PASS**（TRUE_POSITIVE，抗幻觉成功） | 2.63s | 3 次 | In: 174, Out: 62<br>(Cache: 896) |

### 4. 统计汇总与真实性审计边界

- **总通过率**：3/3（100.0%）。
- **总请求消耗**：9 次（严格受控于 16 次上限，剩余 7 次额度）。
- **首轮 usage 口径修正**：初稿2,241仅是未缓存输入1,660与输出581之和，漏算缓存输入，不能称总 Token 消耗。原始文件未保存全部逐轮回执，完整总量保留 unknown/null，不补造历史。
- **费用记账**：`total_cost: null`（严格遵循真实原则，不伪造账单）。
- **Stub 与真实运行的区别**：
  - HTTP Stub 测试用于验证 Node.js 子进程网络参数映射与安全边界，不作为模型智能证据；
  - 冒烟脚本真实访问 `api.deepseek.com`，经由真实 `AgentRuntime` -> Node.js 子进程 -> Pi SDK `streamSimple` -> `ToolBroker` 派发 -> `ExperienceCollector` 落地 Episode。
- **进程与隔离管理**：
  - 每次运行使用独立 PID 的 Pi 子进程，每次运行结束后确认优雅回收与 reap；
  - 数据落地于 `scratch/pi_live_smoke_phase5a`，未触碰生产 SQLite 数据库与技能注册表；
  - 任务用途标定为 `evaluation`，严格与技能演化（learning）隔离。



## 二十一、Codex 真实模型冒烟复核与定向收口（2026-11-22）

**结论：三类业务场景通过真实模型冒烟验证。** 本地配置 provider `cc-switch-deep-seek` / model `deepseek-v4-flash`，实际访问 `api.deepseek.com`；Pi、Python Runtime/Broker/Collector 与 SQLite Episode 均实际执行。业务订单为 synthetic，本轮未测 OS 沙箱、真实电商平台接口、真实模型生成演进或统计效果提升。

### 直接核对与修正

- Codex 读取原始回答与逐工具记录，并查询 Episode SQLite：全部用途为 evaluation，未进入学习。正常订单逐包裹 DELIVERED；部分签收订单正确区分 PKG_201 DELIVERED 与 PKG_202 IN_TRANSIT。
- 首轮故障样本的真实错误是 `ToolResponse.error(data=...)` 与已安装 hello_agents 签名不兼容产生 TypeError，而非预设服务故障。统一修正物流工具四处错误构造为 `code/message/context`；测试断言真实错误码，保留原始样本，不改业务 Oracle。
- 用首轮剩余7次额度仅补跑故障项：实际3次请求，错误码 `LOGISTICS_SERVICE_UNAVAILABLE`，模型明确报告 PKG_202 暂不可用，没有编造状态；Oracle PASS，新 Episode 落盘。累计9+3=12次请求，未超过本轮16次上限。
- 报告按全部逐轮回执汇总，包含 cacheRead/cacheWrite，缺字段保留 null；未知回执阻断后续请求，重复回执不重复记账，失败尝试也计数。故障补测准确 usage 为未缓存输入561、缓存输入2304、输出204，总量3069；钱无账单为null。首轮全部缓存量无法追补，因此整个实验总 Token 为null，不能沿用初稿2241。
- 不把报告脱敏用于改写模型历史：恢复可信内部消息的完整往返；面向人的报告仅保存最终答案、工具与 usage，不展示完整 thinking。数字配置有限值校验、报告原子写入、实际 PID 不存在检查、失败 CLI 非零退出一并收口。
- 自定义本地模型的端点/公开配置尚未纳入 Runtime 完整冻结绑定，带历史的本地自定义模型恢复明确拒绝，不能暗示这次冒烟验证了自定义模型跨重启恢复。内置模型、faux机制测试的旧恢复路径保留。

### 最终相关回归

```bash
env -u PI_SOURCE_ROOT .venv/bin/pytest -q \
  tests/test_pi_local_model.py \
  tests/test_pi_bridge.py \
  tests/test_pi_gaps.py \
  tests/test_pi_budget_ledger.py \
  tests/test_pi_phase2b_recovery.py \
  tests/test_pi_phase2a_context.py \
  tests/test_p6_business_experiment_and_handoff.py \
  tests/test_pi_cli_evaluate.py \
  tests/test_pi_cli_evolve.py \
  tests/test_pi_hooks.py \
  tests/test_pi_supervisor_recovery.py
```

Codex 实际执行：**72 passed, 4 warnings in 35.71s，exit 0**。11个相关文件，包含新增本地模型专项10项；警告为 hello_agents 的 Pydantic dict 弃用提示。此为离线机制回归，与上述12次真实请求分开计数。

### 原始证据路径

- 复核总报告：`scratch/pi_live_smoke_phase5a/acceptance_review.json`，汇总三项验收与已知统计缺失。
- 首轮原样保留：`scratch/pi_live_smoke_phase5a/pi_live_smoke_7034e996/report.json`、JSONL 与 SQLite；复核报告记录其 SHA256，未覆盖原始结果。
- 修正故障协议后的完整回执：`scratch/pi_live_smoke_phase5a/fault_protocol_recheck/report.json`，含逐轮 usage、实际工具错误码、Oracle、Episode 与 PID。
- Codex 扫描27个本轮结果/数据库文件，未检出配置密钥原值；直接检查3个验收运行的 Pi PID 均已消失。

运行结果保留在本地 ignored scratch；未提交或推送 GitHub，未修改本地 Pi 配置，未使用真实用户订单或发布正式 Skill。
