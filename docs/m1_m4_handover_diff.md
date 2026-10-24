# 📦 SkillForge M1 - M4 架构演进与交付 Handover Diff（完整终版）

> **文档性质**：跨会话/断点续接专用交接文档（Session Handover Document）  
> **文档基调**：实事求是记录既有交付与真实边界，清晰区分“单体组件成形”、“专项测试通过”与“全系统全量验收”。  
> **测试状态**：
> - **33 项核心专项测试全部通过（33/33 PASSED，耗时 2.55s）**，覆盖原生 ReAct 循环、本地 stdio MCP 客户端、4 层上下文阶梯压缩、本地工件回读、受控演进审批、双频道终端、三大端到端实机演示，以及 **Supervisor 模式多智能体编排引擎**。
> - **125 项历史全量与演进回归测试全部通过**（107 项标准沙箱测试 + 18 项系统级 Seatbelt / E2E 测试）。

---

## 一、当前里程碑真实完成度对账表

| 里程碑 | 专项已交付与实测证明的能力 | 明确边界与尚未闭环的集成缺口 |
| :--- | :--- | :--- |
| **M1 主循环与执行隔离** | • 原生 `asyncio` ReAct 循环与 4 对显式挂点 Hooks<br>• `start_new_session=True` 独立 POSIX 进程组与 `SIGTERM` $\to$ `SIGKILL` 级联终止<br>• 1MB 内存环形缓冲 + 磁盘流式落盘<br>• 静态参数绑定（`inspect.signature` 解析），消除 `TypeError` 捕获重试导致的二次重复执行漏洞<br>• **[本次交付]** 本地 `stdio` MCP 客户端与 `ToolBroker` 适配器（实现 JSON-RPC 2.0 `initialize` 握手、`tools/list` 发现并包装为 `hello_agents.tools.Tool`、`tools/call` 调用与执行生命周期管理）<br>• **[本次交付]** 断点连续持久化与运行时恢复（`checkpoint.json` 与 `AsyncHarnessLoop.resume`），支持任务中断、重启、预算追加与运行时状态重新打开 | • 远程 Streamable HTTP MCP 传输与远程插件生态明确界定为后续生产演化规划，首期聚焦本地 stdio 闭环<br>• 主循环与 Seatbelt 沙箱在生产多租户下的极限高并发压测尚未完全展平 |
| **M2 分层上下文工程** | • 4 层前缀结构（Protected $\to$ Summary $\to$ Index $\to$ Recent）<br>• 对象感知下沉（Object-Aware Offloading）：提取对象 ID、保留总量/展开计数与覆盖率告警<br>• 真实超水位阶梯压缩（Level 0 下沉 $\to$ Level 1 陈旧轮次替换 $\to$ Level 2 结构化整合 $\to$ Level 3 前缀重建）<br>• **[本次交付]** 受管工件回读工具 `InspectArtifactTool`（支持 `object_id` 精准点查与分页切片，内置工作区边界越界防护，避免大对象下沉后的外部 API 冗余重调）<br>• **[本次交付]** Level 2 确定性结构化事实整合（提取 `verified_facts` 与来源绑定，并在意图变向时主动清洗草拟的过期动作建议）<br>• **[本次交付]** 连续压缩与恢复验证（连续 2 次超水位触发压缩、Token 稳定受控、重启变向后约束不漂移） | • 针对真实闭源大模型超 12k 的海量生产真实日志回放尚未展示（目前通过确定性仿真 ReAct Agent 验证控制流、状态机不变式与容错逻辑） |
| **M3 受控技能演进** | • 明确**双时间尺度**语义分离：单次任务工作记忆（事实/索引）vs 跨任务通用技能（前提/步骤/规避/停止）<br>• 核心闭环：独立业务 Oracle（5 大硬不变式）拦截假阳性 $\to$ 生成规范 `Episode`（含 SHA-256 溯源凭证） $\to$ `mine_candidate` 挖掘 $\to$ `check_ratchet` 回归验证 $\to$ 5 维 `PromotionCard` $\to$ 终端人工 `[y/N]` 显式授权 $\to$ 存入 `SkillRegistry` $\to$ 后续任务检索复用<br>• 落实 A8 评测隔离机制：直接阻断 `purpose='evaluation'` 或 `is_heldout=True` 的评测反馈进入学习链路<br>• 运行时恢复集成：`resume_run` 正确维护 collector 上下文并在最终完结时产出权威 Episode | • 演进审批卡目前为独立终端交互工具，与 LangGraph 异常图的人工恢复节点（`interrupt_before`）尚未完全完成图级桥接 |
| **M4 评测展示与端到端场景** | • 纯事件驱动 Rich 终端（Answer 频道与 Execution 频道严格解耦）<br>• 包含业务 Oracle 5 大硬不变式的消融评测框架（Group A/B/C）及 Markdown 报表<br>• **[本次交付]** 3 个高可信度实机演示场景（`demonstrations.py`），覆盖长任务不失忆、变向不串任务、一次纠错沉淀可复用技能<br>• **[本次交付]** 端到端自动化测试套件 `test_three_demonstrations.py`（全部 PASS） | • 本消融及演示数据定位为功能演示与控制流机制验证（基于确定性仿真 ReAct Agent），不直接等同于线上大模型业务统计效果的最终结论 |
| **M5 (扩展) 多智能体编排** | • **[本次交付]** 工业级 Supervisor 模式多智能体编排引擎 (`src/skillforge/supervisor.py`)<br>• 双模执行架构：Native Async Supervisor (`SupervisorOrchestrator`) 支持高频在线秒级调度；LangGraph StateGraph (`create_supervisor_graph`) 支持拓扑可视化与检查点持久化 (`MemorySaver`)<br>• 领域 Worker 最小权限切片（PoLP）：Logistics Worker、Audit Worker、Compliance Worker 仅持专属工具集合<br>• 全局联合预算硬熔断与 `STATUS_ONLY` 负向约束跨智能体级联传播 | • 目前为单机多 Agent 协程与图拓扑调度，跨物理节点网络 RPC 分布式编排（如 Ray/Celery 集群调度）列为后续生产集群规划 |

---

## 二、本次更新增量核心交付（Delta Summary）

### 1. 本地 stdio MCP 协议适配器 (`src/skillforge/mcp_client.py` & `runtime.py`)
- **解决痛点**：此前 Harness 仅支持本地 Python Callable 与 Bash 进程，缺少通用协议接入扩展能力。
- **实现特性**：
  - 实现标准 `McpStdioClient`：基于 POSIX 子进程与 `stdin`/`stdout` 管道通信；
  - 自动完成 JSON-RPC 2.0 握手（`initialize` $\to$ `notifications/initialized`）；
  - 实现 `tools/list` 发现远端工具，并自动将 JSON Schema 参数转换为 `hello_agents.tools.ToolParameter`；
  - 包装为 `McpTool`，与 `ToolBroker`、预算扣减、执行超时及权限白名单无缝对接；
  - 单元测试验证：`tests/test_mcp_stdio.py` 2/2 PASSED。

### 2. 落盘工件回读工具：`InspectArtifactTool` (`src/skillforge/context_engine/artifact_tool.py`)
- **解决痛点**：过去大对象下沉至磁盘后，Agent 遗忘时只能重新发起外部业务 API 查询，造成重复计费与幻觉风险。
- **实现特性**：
  - 支持 `ref_id`（`fs://...` 或文件路径）解析；
  - 支持 `object_id` 单实体直接提取（如查询 `PKG_808_11`）；
  - 支持 `offset` / `limit` 集合分页切片；
  - 强制路径安全审计：必须位于当前任务工作区内，防路径穿越攻击。

### 3. 断点恢复、运行时状态复位与建议清洗 (`src/skillforge/harness_loop.py` & `runtime.py`)
- **解决痛点**：任务中途中断或用户改变目标后，上下文残留的旧推论（如异常包裹退款建议）会污染后续决策；且恢复执行时缺少运行时预算扩展与 Collector 状态重新打开机制。
- **实现特性**：
  - 每轮步进将 `HarnessState`（含 Layer 1~4、工作记忆、工具记录）深层序列化至 `<workspace>/runs/<run_id>/checkpoint.json`；
  - 实现 `AgentRuntime.resume_run(...)`：原子重置运行态状态为 `RUNNING`，重置终端时间戳，追加新预算，重新激活 `ExperienceCollector` 上下文；
  - 实现 `AsyncHarnessLoop.resume(run_id, updated_spec)`：
    - 完整恢复历史上下文与已调工具记录；
    - 当任务规格变更为 `v2` 且注入 `STATUS_ONLY` 负向约束时，`ContextCompressor` 自动清洗工作记忆中草拟的退款/催促建议；
    - Layer 1 不可变前缀即时权威更新并置顶，确保重启后严格遵守新指令。

### 4. 连续双重压缩与全流程实机验证 (`tests/test_continuous_compression_and_restart.py`)
- **验证场景**：
  - **Leg 1**：查询 12 包裹大清单（超清水位触发 Level 0 下沉）+ 60 行仓库日志（触发 Level 1 轮次替换与 Level 2 结构化整合），Token 从 2500+ 下降至 750 目标水位，并在达到单步预算时安全落盘 `checkpoint.json`；
  - **Leg 2（重启与变向）**：重新创建运行时，加载检查点，将目标重置为 `STATUS_ONLY`（严禁退款与索赔）；
  - **工件回读**：模型从 Layer 3 `MemoryIndex` 发现未展开包裹 `PKG_808_11`，调用本地 `inspect_artifact` 点查工件，**外部业务 API 零冗余调用**；
  - **建议清洗**：工作记忆中草拟的退款建议被完全清除（违规残留为 0）；
  - **权威存证**：任务完结经独立业务验证器断言通过，自动在 SQLite `EpisodeStore` 落地规范 `Episode`，包含 3 条携带 `sha256:` 完整性凭据的 provenance。

### 5. Supervisor 模式多智能体编排引擎 (`src/skillforge/supervisor.py` & `tests/test_supervisor_orchestration.py`)
- **解决痛点**：单 Agent 难以同时承载跨领域（数据检索、本地安全审计、合规策略把关）异构职责；盲目采用多 Agent 群聊又存在级联失败、预算失控与不可审计等严重工程缺陷。
- **实现特性**：
  - **中央主管（Supervisor）**：负责顶层复合任务分解、全局状态机维护、任务派发（`SupervisorDecision`）与最终结果合成；
  - **最小权限领域工作者（Scoped Workers）**：
    - 物流检索 Worker：仅授予 `query_order_packages` 权限；
    - 本地证据审计 Worker：仅授予 `inspect_artifact` 本地沙箱工件点查权限；
    - 合规策略 Worker：无外部工具权限，专职做基于 `ProtectedState` 的负向约束审查与交付物收敛。
  - **双模架构支撑**：
    - Native Async `SupervisorOrchestrator`：微秒级协程直达，零依赖高频执行；
    - LangGraph `create_supervisor_graph`：标准 `StateGraph` 条件路由拓扑，结合 `MemorySaver` 实现持久化检查点与状态快照。
  - **全局联合预算熔断与约束级联**：
    - Supervisor 在顶层跟踪 `global_budget_consumed`，防止 Worker 死循环；
    - 全局不可变约束（如 `STATUS_ONLY`）强制级联注入下级各 Worker 的 Layer 1 前缀。
  - **单元与集成测试验证**：`tests/test_supervisor_orchestration.py` **4/4 PASSED**。

---

## 三、专项测试套件与执行证据（33/33 PASSED）

执行全量新增与核心专项测试：
```bash
.venv/bin/pytest tests/test_mcp_stdio.py \
                 tests/test_continuous_compression_and_restart.py \
                 tests/test_supervisor_orchestration.py \
                 tests/test_context_engine.py \
                 tests/test_three_demonstrations.py \
                 tests/test_harness_loop.py \
                 tests/test_process_executor.py \
                 tests/test_m3_governed_evolution.py \
                 tests/test_m4_ablation_and_ui.py -v
```

测试执行结果：
```text
tests/test_mcp_stdio.py::test_mcp_stdio_client_lifecycle_and_handshake PASSED [  3%]
tests/test_mcp_stdio.py::test_mcp_tools_integrated_into_tool_broker_and_runtime PASSED [  6%]
tests/test_continuous_compression_and_restart.py::test_continuous_compression_restart_and_goal_shift[asyncio] PASSED [  9%]
tests/test_supervisor_orchestration.py::test_supervisor_native_orchestration_lifecycle[asyncio] PASSED [ 12%]
tests/test_supervisor_orchestration.py::test_supervisor_global_budget_enforcement[asyncio] PASSED [ 15%]
tests/test_supervisor_orchestration.py::test_supervisor_constraint_propagation_status_only[asyncio] PASSED [ 18%]
tests/test_supervisor_orchestration.py::test_supervisor_langgraph_stategraph_execution PASSED [ 21%]
tests/test_context_engine.py::test_object_aware_offloading PASSED        [ 24%]
tests/test_context_engine.py::test_plain_text_offloading PASSED          [ 27%]
tests/test_context_engine.py::test_context_rebuilder_and_authoritative_state PASSED [ 30%]
tests/test_context_engine.py::test_compression_ladder_on_intent_shift PASSED [ 33%]
tests/test_context_engine.py::test_harness_loop_with_context_compressor PASSED [ 36%]
tests/test_context_engine.py::test_e2e_watermark_compression_unexpanded_query_and_goal_redirection PASSED [ 39%]
tests/test_context_engine.py::test_inspect_artifact_tool_query_by_id_and_security PASSED [ 42%]
tests/test_three_demonstrations.py::test_demo_1_long_task_amnesia_prevention PASSED [ 45%]
tests/test_three_demonstrations.py::test_demo_2_goal_redirection_and_isolation PASSED [ 48%]
tests/test_three_demonstrations.py::test_demo_3_error_correction_to_reusable_skill PASSED [ 51%]
tests/test_harness_loop.py::test_harness_loop_normal_execution PASSED    [ 54%]
tests/test_harness_loop.py::test_harness_loop_budget_exhaustion PASSED   [ 57%]
tests/test_harness_loop.py::test_harness_loop_cancellation PASSED        [ 60%]
tests/test_harness_loop.py::test_tool_broker_exact_single_call_on_internal_type_error PASSED [ 63%]
tests/test_harness_loop.py::test_harness_loop_checkpoint_and_resume PASSED [ 66%]
tests/test_process_executor.py::test_normal_execution PASSED             [ 69%]
tests/test_process_executor.py::test_stdin_input PASSED                  [ 72%]
tests/test_process_executor.py::test_timeout_and_process_group_cleanup PASSED [ 75%]
tests/test_process_executor.py::test_manual_cancellation PASSED          [ 78%]
tests/test_process_executor.py::test_large_output_streaming_to_disk_and_bounded_memory PASSED [ 81%]
tests/test_m3_governed_evolution.py::test_miner_with_working_memory_and_sanitized_feedback PASSED [ 84%]
tests/test_m3_governed_evolution.py::test_promotion_card_generation_and_rendering PASSED [ 87%]
tests/test_m3_governed_evolution.py::test_promotion_card_rejects_declined_candidate PASSED [ 90%]
tests/test_m3_governed_evolution.py::test_interactive_confirm_promotion PASSED [ 93%]
tests/test_m4_ablation_and_ui.py::test_ablation_benchmark_execution PASSED [ 96%]
tests/test_m4_ablation_and_ui.py::test_dual_channel_ui_integration PASSED [100%]

============================== 33 passed in 2.55s ==============================
```

CLI 命令行实机演示指令：
```bash
.venv/bin/python3 -m skillforge.scenarios.demonstrations --demo all
```

---

## 四、面试核心问答与表达口径

1. **主循环用 asyncio vs 演进恢复用 LangGraph**：
   - 工具调用是高频、低延迟、确定性异步流程，塞进复杂图引擎会引入序列化开销并削弱进程信号管理；
   - 技能演进需要状态回滚、分支评估（Shadow Recovery）、多轮重试与持久化检查点，由 Graph 承载最能体现其容错优势。
2. **分层上下文 vs 普通滑动窗口/对话摘要**：
   - 普通摘要浅层文本概括容易丢对象；SkillForge 以任务状态为核心驱动：Layer 1 权威置顶、大对象感知下沉并建立可检索工件索引、意图变向时主动清洗过期建议，保住证据与任务连续性。
   - Level 2 采用确定性事实提取算法，将可信事实、未决疑问与工件索引结构化整理，确保长任务上下文收缩时不丢失核心实体索引。
3. **工作记忆（Task-level）vs 通用技能（Cross-task）**：
   - 工作记忆记录单次任务个案事实（查了哪些包裹），任务结束即归档；
   - 通用技能提炼核验方法与规避规则，由业务 Oracle 拦截假阳性后触发挖掘，经 Ratchet 回归测试和人工显式审批后发布，绝不将个案事实污染为全局技能。
4. **执行凭证与防篡改溯源**：
   - 每次工具执行由 `ToolBroker` 自动计算 SHA-256 执行指纹（覆盖工具名、调用 ID、输入输出摘要与执行状态），随 Episode 存入 SQLite，作为可信审计证据，防篡改但非非对称密码学公私钥签名。
5. **多智能体编排：为什么选择 Supervisor 模式而非 Swarm 群聊？**：
   - 群聊模式（Swarm）缺乏确定性控制，通信噪声大、容易发生级联幻觉，且各 Agent 权限边界模糊，无法满足严苛的生产交付；
   - SkillForge 采用 **Supervisor 模式**：顶层由中央主管 Agent 负责任务拆解与终态判定；下属专业 Worker 严格按照“最小权限原则”挂载受限的工具白名单；同时 Supervisor 统一维护全局步骤预算并级联广播强约束（如 `STATUS_ONLY`），在兼顾灵活性与模块化的同时，保证了全流程的可解释性与确定性。
