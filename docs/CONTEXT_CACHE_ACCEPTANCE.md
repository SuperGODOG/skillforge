# SkillForge Harness Context Compression & Prefix Cache Acceptance

本文档作为 SkillForge 上下文压缩 Hook 与 Prefill / 前缀缓存（Prefix Cache）优化（F1 ~ F5 修复闭环）的技术验收规范，明确核心概念辨析、架构取舍、实测基准数据、验证边界与复现命令。

---

## 1. 核心概念辨析：Prefill / KV Cache 与 上下文压缩

在长周期 Agent 运行架构中，Prefill / KV Cache 与上下文压缩分别解决不同维度的性能与正确性问题：

| 维度 | Prefill / KV Cache 客户端保障 | 上下文分层压缩 (Context Compression) |
| :--- | :--- | :--- |
| **核心目标** | 消除多轮请求中固定前缀的重复 Prefill 计算与首字延迟 | 控制上下文总长度，防止超出模型窗口或稀释指令遵循能力 |
| **客户端机制** | 保持 System 规则与 Tool Schema 严格冻结；动态状态移入 Context 尾部，使前部序列可被重用 | 分层将大 Observation 下沉为 Artifact 引用；对闭合历史单元进行结构化摘要，重构 Context |
| **上下文长度** | 不主动减少 Context Token 总量（多轮会自然递增追加） | 主动减少 Token 总量（将 Context 维持在安全高低水位线以内） |
| **Cache 影响** | 最大化客户端送出静态前缀（System + Tools）的指纹一致性 | 发生摘要重构（Compaction）时，推进 Context Epoch，重构历史层 |
| **协同关系** | **同 Epoch 内极速推进**：稳定前缀保证高前缀重用潜力；**跨 Epoch 控长防爆**：超阈值时受控压缩并推进版本，两者分工互补 |

> [!NOTE]
> 客户端（SkillForge）不直接管理 GPU 显存中的 KV 张量，亦不声称实现了服务端的张量淘汰策略。客户端的责任是保证请求装配的确定性与前缀稳定性，消除由于客户端无序序列化、时间戳抖动或动态参数污染导致的非必要 Cache Miss。真实的服务端 KV Cache 命中需要远端 Provider 物理支持并在遥测中回传，本地测试指标为确定性前缀重合度，不可冒称为服务端真实命中（Server Hit）。

---

## 2. 核心架构与请求装配设计

```
[HarnessTaskSpec / Initial State]
         │
         ▼
 ┌─────────────────────────────────────────────────────────────┐
 │ AsyncHarnessLoop._run_loop                                  │
 │                                                             │
 │  1. Authoritative State Synchronization                     │
 │     - 保持 _base_system_prompt 与冻结 Skill 正文完全不变    │
 │     - protected_state 仅在内容 hash 变化时向尾部追加指令     │
 │     - 避免每轮更新时间戳、步数、临时状态导致 Prefix Churn   │
 │                                                             │
 │  2. Deterministic Tool Schema Serialization                 │
 │     - 按字母表升序排序 application_allowlist                │
 │     - 递归规范化 properties 与 required 字段排序            │
 │                                                             │
 │  3. Canonical Prefix Fingerprint Generation                 │
 │     - SHA-256(system_prompt.strip() + tool_schemas)         │
 │     - epoch 单列跟踪，不混入静态前缀指纹                     │
 │     - 跟踪 prefix_change_layer (initial/system/tools/none)   │
 │                                                             │
 │  4. Model Invocation (_call_model)                          │
 │     - 流式输出仅在首个非空内容 Token 触发计算 ttft_ms       │
 │     - 统一保留 tool_calls 与 usage 结构                     │
 │     - 严格归一化 Provider Usage，不混淆 0 与 None           │
 │                                                             │
 │  5. Candidate Copy Isolated Compression Ladder              │
 │     - 创建 candidate 消息与状态工作副本，禁止原地改写       │
 │     - 并发状态漂移检测 (Intent/Goal/Steps/Messages 校验)    │
 │     - 默认内置权威 Must-Keep 规则校验                       │
 │     - Level 0: Large Observation Offloading -> Artifact     │
 │     - Level 1: Historical Tool Observation Stubbing         │
 │     - Level 2: Structured Summarization (基于 Tool-ID 闭包) │
 │     - 全流程无 await 原子提交至真实 state，推进 epoch       │
 └─────────────────────────────────────────────────────────────┘
```

### 2.1 设计取舍 (Design Trade-offs)

1. **静态规则前缀 vs 动态目标更新**：
   - *取舍*：高频变化的数据（当前步骤计数、动态时间戳、新任务目标）严禁覆写 System Prompt 前部，改由历史消息尾部追加权威指令；`_base_system_prompt` 与冻结 Skill 正文全程受保护。
2. **候选工作副本隔离 vs 原地改写回滚**：
   - *取舍*：压缩与摘要流程在独立的候选内存副本中推进，只有当所有分层处理、约束检查与 Token 缩减验证通过后，才在单个同步阶段内原子提交给 live state。遇到异常、取消、无进展或校验拒绝时，原上下文完全保持未修改状态。
3. **基于 Tool Call ID 的完整闭包切分**：
   - *取舍*：`find_safe_split_index` 逐一配对 `assistant.tool_calls` ID 与 `tool.tool_call_id`。对于多 Call 部分返回或未闭合调用的块，整体保留在活跃区，严禁切断或移除原参数以伪造协议。
4. **真实遥测归一化 vs 模拟收益伪造**：
   - *取舍*：移除早前测试中 `prompt_tokens * 0.75` 等人为伪造的“模拟缓存节省”，如实上报 Provider 返回的字段。对于未返回缓存信息的请求，`cache_read_tokens` 与 `cache_hit_ratio` 显式保留为 `None`，不伪造成 0.0。

---

## 3. 实测对比基准数据 (基于输入 Token 本地估算)

依据标准物流核验任务（短 Trace 4 轮 / 长 Trace 8 轮），在隔离临时环境运行 `scripts/generate_context_cache_fixtures.py`，落盘于 `docs/fixtures/context_cache_samples.json`。

> [!NOTE]
> **重要计量与术语边界限定声明**：
> 1. **收益性质限定**：本文档中的“控费潜力/压缩收益”严格限定为基于 `tiktoken:cl100k_base` 计算的**客户端输入 Token 估算**。末轮输入减少 27.0%（2,322 -> 1,696 tokens）属于输入序列长度的理论压缩，**不代表云厂商真实计费账单或实际 Provider 扣费**。
> 2. **总成本声明**：长 Trace 全任务主调用输入估算从 9,668 tokens 降至 8,588 tokens（净减少 1,080 tokens，降低 11.17%）；若启用外部 LLM 摘要，其独立的 Summary 调用开销需另行累计计算，**在此不声称端到端总调用成本必然下降**。
> 3. **前缀匹配定义**：表格中的“连续公共前缀”未包含远端 Provider 物理底层的专有 Chat Template，严格定义为**客户端规范消息共同前缀（Client Canonical Common Prefix）**，不可等同于服务端的真实 Token Prefix 或物理 KV Cache 命中。
> 4. **摘要调用与真实 Usage**：当前基准采用确定性规则提取（Deterministic Rule-based Consolidation），未调用外部模型，因此摘要调用次数记为 `0`，真实摘要 Usage 显式标记为 `null`，严禁编造模拟数据。
> 5. **旧版误算更正**：早期报告曾使用固定 0.75 乘率伪造 Provider 缓存字段并得出“节省 1,746/1,337 tokens”，该数据系脱离实际的误算，现已完全剔除。

### 3.1 短 Trace 对比基准 (4 轮交互，3 次工具调用)

| 指标 | Policy 1: `dynamic_prefix` | Policy 2: `stable_prefix` | Policy 3: `stable_prefix_with_compression` |
| :--- | :--- | :--- | :--- |
| **任务执行状态** | COMPLETED | COMPLETED | COMPLETED |
| **交互总轮次** | 4 轮 | 4 轮 | 4 轮 |
| **主调用总输入 Token 估算** | 2,593 tokens | 2,277 tokens | 2,777 tokens |
| **末轮输入 Token 估算** | 1,190 tokens | 1,045 tokens | 1,067 tokens |
| **终态 Context 消息 Token** | 1,023 tokens | 909 tokens | **957 tokens** (结构 Overhead 可见) |
| **静态前缀匹配次数** | **0** (每轮变化) | **3** (静态规则冻结) | **3** (静态规则冻结) |
| **前缀变更次数** | **3** | **0** | **3** (Compaction 升级 Epoch) |
| **客户端规范消息共同前缀总和** | 1,145 tokens | 1,013 tokens | 661 tokens |
| **压缩 Commit / Reject 次数** | 0 / 0 | 0 / 0 | **3 次 commit** / 0 次 reject |
| **压缩累计开销 (Overhead)** | 0.00 ms | 0.00 ms | 7.11 ms |
| **摘要调用数及独立 Usage** | 0 (null, 确定性无LLM) | 0 (null, 确定性无LLM) | 0 (null, 确定性无LLM) |
| **工具 Handler 实际调用次数**| 3 次 | 3 次 | 3 次 |
| **独立 Oracle 最终判定** | TRUE_POSITIVE | TRUE_POSITIVE | TRUE_POSITIVE |

**短 Trace 结构膨胀解析**：
在短交互（4 轮）中，`stable_prefix_with_compression` 终态 Context Token 为 957，显式高于未压缩策略（909 tokens）。这是由于分层上下文架构（HierarchicalContext）必须注入 `ProtectedState`、`WorkSummary` 和 `MemoryIndex` 的结构化 YAML 骨架（约 200 tokens）。在短轮次且 Observation 规模较小时，结构元数据开销超过了小结果裁剪所节省的 Token，呈现出明确的结构 Overhead。

---

### 3.2 长 Trace 对比基准 (8 轮交互，7 次大 Observation 工具调用)

| 指标 | Policy 1: `dynamic_prefix` | Policy 2: `stable_prefix` | Policy 3: `stable_prefix_with_compression` |
| :--- | :--- | :--- | :--- |
| **任务执行状态** | COMPLETED | COMPLETED | COMPLETED |
| **交互总轮次** | 8 轮 | 8 轮 | 8 轮 |
| **主调用总输入 Token 估算** | 10,883 tokens | 9,668 tokens | **8,588 tokens** (主调用输入估算净降 1,080 / 11.17%) |
| **末轮输入 Token 估算** | 2,618 tokens | 2,322 tokens | **1,696 tokens** (末轮输入估算净降 626 / 27.0%) |
| **终态 Context 消息 Token** | 2,325 tokens | 2,082 tokens | **1,574 tokens** (净降 508 tokens) |
| **静态前缀匹配次数** | **0** (每轮变化) | **7** | **7** |
| **前缀变更次数** | **7** | **0** | **7** |
| **客户端规范消息共同前缀总和** | 7,677 tokens | 6,835 tokens | 4,264 tokens |
| **压缩 Commit / Reject 次数** | 0 / 0 | 0 / 0 | **7 次 commit** / 0 次 reject |
| **压缩累计开销 (Overhead)** | 0.00 ms | 0.00 ms | 20.82 ms |
| **摘要调用数及独立 Usage** | 0 (null, 确定性无LLM) | 0 (null, 确定性无LLM) | 0 (null, 确定性无LLM) |
| **工具 Handler 实际调用次数**| 7 次 | 7 次 | 7 次 |
| **独立 Oracle 最终判定** | TRUE_POSITIVE | TRUE_POSITIVE | TRUE_POSITIVE |

**长 Trace 压缩收敛解析**：
随着轮次和 Observation 累积，未压缩策略的历史消息线性膨胀至 2,082 tokens（末轮输入 2,322 tokens）。而压缩策略通过将陈旧大结果下沉为 Artifact、提取结构化工作摘要，将末轮输入压制在 1,696 tokens（输入估算降 27.0%），主调用总输入估算减少 1,080 tokens（降 11.17%），同时保持了工具调用不重复（均为 7 次）和 Oracle 验证一致通过。需注意：若引入异步大模型摘要，摘要调用的自身 Token 消耗须另行独立核算，不应宣称为整体计费成本下降。

---

## 4. 验证与未验证边界清单

### 4.1 审计缺陷修复与验证 (Verified via 9 Fine-Grained Tests)

> [!NOTE]
> 前序任务首轮 5 项专项与 36 项回归通过，未覆盖上一只读审计所指出的关键缺陷。本轮通过拆解为 9 项命名清晰的细粒度定向测试，逐一完成复现与断言闭环：

1. **F1-1: 规范化静态前缀与 Strip 一致性** (`test_f1_canonical_prefix_invariance_and_strip_normalization`)
   - 验证发送端与指纹计算使用同一份 `.strip()` 字符串，排除空白符差异；
   - 验证 `epoch` 不混入静态前缀指纹，同内容不同 epoch 静态指纹保持一致；
   - 验证工具 required 数组排序保持语义一致。
2. **F1-2: 动态状态更新保护基础规则与冻结 Skill** (`test_f1_base_rules_and_frozen_skill_body_retention_across_protected_state_updates`)
   - 验证 `protected_state` 更新不会覆写抹掉原始 `_base_system_prompt` 与 Skill 正文；
   - 动态目标重定向通过用户消息尾部追加权威指令生效。
3. **F1-3: 流式 TTFT 与批处理一致性** (`test_f1_streaming_ttft_and_multiturn_batch_consistency`)
   - 验证流式输出仅在首个非空文本 Token 到达时记录 `ttft_ms`；
   - 批处理与纯工具调用 `ttft_ms` 正确返回 `None`；
   - 流式响应完整保留 `tool_calls` 与 `usage`。
4. **F2-1: 压缩候选副本隔离与原子提交** (`test_f2_candidate_working_copy_isolation_and_atomic_commit`)
   - 验证压缩全流程在候选工作副本中执行，未 commit 前原 state 消息与索引 0 修改；
   - 校验通过后原子替换原 state 并推进 epoch。
5. **F2-2: 并发状态漂移废弃陈旧摘要** (`test_f2_stale_summary_rejection_on_concurrent_state_divergence`)
   - 验证在摘要生成期间若外部并发追加消息、修改目标或增减工具记录，压缩器准确识别版本偏离并返回 `rejected_stale_intent`，不覆写并发新值。
6. **F2-3: 默认内置 Must-Keep 校验与正向集成** (`test_f2_default_must_keep_and_real_summary_integration`)
   - 默认校验无需测试注入 lambda；
   - 候选摘要若缺失核心目标或违背约束，直接触发 `rejected_invalid_summary`；
   - 校验通过的摘要真实进入后续模型请求。
7. **F2-4: 硬容量限制与有界暂停** (`test_f2_hard_capacity_and_bounded_pause_under_no_progress`)
   - 连续无进展尝试受限；
   - 超出硬容量限制时触发 `BUDGET_EXHAUSTED` 有界暂停，阻止超额 Provider 请求。
8. **F3-1: Tool-Call ID 成对闭合安全切分** (`test_f3_tool_call_closure_and_safe_split_index_edge_cases`)
   - 逐一匹配 assistant 与 tool 的 call_id；
   - 未闭合调用（如 2 calls 仅 1 result）整体保留在活跃区；
   - 严格支持 `recent_turns_to_keep=0` 边界，负数输入抛出 `ValueError`。
9. **F4-1: Provider 遥测归一化与缺失值保真** (`test_f4_provider_usage_normalization_and_missing_fields`)
   - Anthropic 模式：`total_input = uncached + cache_read + cache_creation`；
   - OpenAI 模式：避免对已包含 cached_tokens 的总输入重复累加；
   - 缺失字段显式保留为 `None`，不伪造成 0。
10. **F5-1: 端到端 Pause/Resume 重建、Manifest 安全与 Oracle 防御** (`test_f5_end_to_end_pause_resume_manifest_security_and_oracle_defense`)
    - 验证 Review Pause 挂起后由新 Runtime 重建恢复，Tool Handler 不发生重复执行，Budget 正确累加；
    - 验证压缩状态下安全下钻合法 Manifest Artifact，跨 Run 隔离防护生效；
    - 验证业务 Oracle 不会被压缩摘要或伪造 Key 绕过。

### 4.2 未验证边界 (Unverified / Production Scope)

- **服务端物理显存分配**：真实云厂商 GPU 节点上的 PagedAttention / RadixAttention 显存逐出逻辑属服务侧黑盒，客户端仅能保证送出的 Prefix 一致；
- **全模型精确 Tokenizer**：本地测试统一采用 `tiktoken:cl100k_base`，真实生产环境各模型分词器（如 Claude-3.5 Sonnet / Gemini 2.0 / DeepSeek）存在分词切分差异；
- **商业 API 计费账单校对**：本轮基准采用本地 Mock 与捕获请求，未连接外部计费云端 API。

---

## 5. 复现命令与验证步骤

所有测试均在本地隔离虚拟环境中执行，不依赖外网与付费接口：

```bash
# 1. 执行本次前缀缓存与上下文压缩 9 项细粒度定向专项测试
.venv/bin/pytest tests/test_context_cache_and_compression.py -v

# 2. 重新生成并验证对照基准落盘文件 (含 4 轮与 8 轮双 Trace)
.venv/bin/python3 scripts/generate_context_cache_fixtures.py

# 3. 执行核心回归套件 (全量 40 项)
.venv/bin/pytest \
  tests/test_context_engine.py \
  tests/test_continuous_compression_and_restart.py \
  tests/test_harness_loop.py \
  tests/test_harness_business_hooks.py \
  tests/test_context_cache_and_compression.py -v
```
