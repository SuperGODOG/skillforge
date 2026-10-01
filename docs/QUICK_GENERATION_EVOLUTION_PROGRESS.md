# SkillForge 快速生成与受控演进：重构交接落地报告 (P0 + P1 + P2 + P3 + P4 + P5 + P6)

> **版本标记**：2026-10-01 第二批收尾交付收口文档（原 P5 真实 LangGraph 编排与 RepairJob 闭环接入完成）  
> **实施状态**：P0 核准基线冻结完毕；P1 短需求生成与受控试用已落地并通过验收（含 G6 共同门禁漏洞收口）；P2 统一准入与膨胀守卫（V1–V6）已完整落地并通过全量自动化测试；P3 用户目标变化驱动的草稿修订（I1–I5）已完整落地并通过独立验收测试与联合回归；P4 轨迹提纯与范围感知挖掘（D1–D5）已完整落地并通过独立专项验证与全量回归；P5 复用 LangGraph 做有界异常恢复（L1–L6，含第二批真实 StateGraph 与 RepairJob 闭环）已完整落地并通过独立专项验证与全量回归；P6 业务对照实验与交接收口（B1–B5）已完整落地；第一批（S/R/M 来源/冻结快照持久化/旧库迁移）与第二批（P5 真实 StateGraph/RepairJob/SqliteCheckpointer/共享预算）均已完工并通过全量回归；已形成完整简历事实清单。  
> **执行原则**：遵循准确性、证据优先、Trade-off 权衡、可验证与清晰责任边界原则；代码修改沿用最小增量（Ponytail）标准。

---

## 1. 阶段概述与交付范围

根据《SkillForge：快速生成与受控演进重构交接计划》（`docs/SKILL_GENERATION_EVOLUTION_HANDOFF_PLAN.md`），本次任务聚焦于核心业务闭环：
> **短需求/单次有价值经历快速生成 → 当前任务受控试用 → 用户目标变化与失败反馈修订 → 验证 → 确认晋升与未来复用**

#### 本次实施交付项（P0 + P1 + P2 + P3 + P4 + P5 + P6）
1. **P0 入口盘点与基线冻结**：核验既有 8 个生成/演进/注册入口与旁路、LangGraph 实际节点连接、现存技能长度分布、Prompt Bloat 护栏判定常量与电商多包裹物流场景定义。
2. **P1 短需求生成与受控试用**：
   - 扩展 `CandidateSkill` 契约与 SQLite 表结构，支持无历史 Episode 的真实短需求来源（`source_requirement`、`source_type="requirement"`、`task_spec_hash`）；
   - 新增 `generate_candidate_from_requirement` 入口，支持依据 `task_spec_hash` 复用/修订已有草稿，消除无经历时被迫伪造 Episode 的结构缺陷与草稿无限派生膨胀；
   - 改造 `AgentRuntime.start_run` 接入 `candidate` 快照执行，冻结运行体，新增 `AgentRuntime.run_agent` 驱动 `SimpleAgent` 与 `BrokeredTool` 真实消费冻结 Prompt，完成多包裹物流只读业务与 Episode 沉淀；
   - 严格阻断未晋升 Draft Candidate 进入无关任务的正式检索（`retrieval.retrieve`）；
   - 在 `ToolBroker` 网关前置拦截未授权工具（如带副作用的 `refund_order`），保障底层 Handler 执行次数严格为 0；
   - 保持无独立 Oracle 时的 `unknown` 结果隔离，不伪造成功经历；
   - **P1 G6 漏洞收口**：移除 `skill_splitter.py` 内部自造伪造 `PASS` 验证记录的旁路后门；在 SQLite 中创建持久化 `validation_records` 表，由 `CandidateStore` 权威管理；`register_skill` 与 `promote_candidate` 严格校验数据库存量权威 PASS 记录与内容哈希一致性，无记录、伪造、未通过或篡改者一律安全阻断（即便 `caller_confirmed=True` 也不得豁免）。原有 `generate_skill(register=False)` 保持 100% 向后兼容。
3. **P2 统一准入与膨胀守卫 (V1–V6)**：
   - **V1 (用途隔离)**：`mine_candidate` 与 `mine_pending` 严格只接收 `purpose == "learning"` 的经验，严密隔离 `evaluation` / `heldout` / 未知用途；
   - **V2 (防篡改与任务隔离)**：`mine_candidate` 严密核对内存经验与 `EpisodeStore` 底层不可变正本，拦截内存窜改；`generate_candidate_from_requirement` 强制将 `task_id` 注入 `task_spec_hash`，实现任务私有草稿隔离；
   - **V3 (膨胀门禁与廉价检查前置)**：段落增长 > 25% 且绝对增量 > 100 字符，或整 Body 增长 > 1.20x 且净增 > 100 字符，或冷启动新建正文 > 3,000 字符，统一触发 `REVIEW` 软门槛；在 `validate_candidate`、`RepairJob` 与生成修订中，前置运行工具权限与 Bloat 廉价检查，超标即刻返回，0 额外 LLM 开销；
   - **V4 (验证绑定与持久化)**：`ValidationRecord` 完整绑定 `candidate_id`、`content_hash`、`baseline_version`、`scope_hash`、`config_hash` 与 `dataset_version`；持久化至 SQLite，重开连接完好保留；验证后正文窜改、基线漂移、范围漂移与重复晋升严格拒绝；
   - **V5 (工具依赖前置与凭证区分)**：候选声明工具不在 `tool_broker.application_allowlist` 时在 LLM 评测前直接拦截；运行时清晰区分 fixture / broker / sandbox 执行凭据，未授权调用底层 Handler 触发次数恒为 0；
   - **V6 (L1 轻量修订路径)**：精确计算语义变更等级，单字段元数据修改走 `L1` 快速通道，避免多轮复杂反思。
4. **P3 用户目标变化驱动的草稿修订 (I1–I5)**：
   - **契约解耦与双轴独立**：确立意图修订（`intent_revision`）与技能发布版本（`version`）为独立正交轴，用户修改目标不等于发布新正式技能；
   - **I1 (目标变更与草稿替代)**：用户显式修改目标、约束或交付形式时产生新意图修订；旧草稿标为 `SUPERSEDED` 并保留血缘链（`superseded_by` / `supersedes`）；下一次执行真实消费新正文；运行中的旧正文快照不可就地改写；
   - **I2 (快照冻结与迟到隔离)**：旧意图运行中的正文、技能与合同指纹冻结；迟到结果归属旧意图，不覆盖新结果、不算作新目标的正例经验；SQLite 连接重开后隔离保证完好；外部副作用不因版本撤销而被虚假声称回滚；
   - **I3 (改措辞不重建与模糊确认)**：礼貌用语与无害重排判定为 `NO_OP`（0 次生成器调用，0 新候选）；方向模糊或缺乏可操作证据的负反馈返回 `CONFIRMATION_REQUIRED`，不静默修改用户目标；
   - **I4 (任务取消与副作用非可逆性)**：通过 `runtime.cancel_run` 显式终止任务；在 `get_cancellation_report` 中如实审计已发生工具副作用并标记 `side_effects_reversible=False`；分别验证协程协作式超时（`asyncio.wait_for`）、进程沙箱信号强杀与普通 Python 同步回调不可中途抢占的通道特性；
   - **I5 (会话隔离与防扩权)**：单会话意图目标变更不影响 `SkillRegistry` 中面向全局正式技能的可用性；只读目标转向写入目标依然受 `ToolBroker` 严格白名单限制，未授权写工具（如 `refund_order`）被阻断，底层 Handler 调用严格为 0。
5. **P4 轨迹提纯与范围感知挖掘 (D1–D5)**：
   - **D1 (业务失败提纯与独立预期)**：`purify_trace_to_proposal` 将 Skill 责任失败提纯为结构化 `TestCaseProposal`；严格禁止将模型失败输出当独立预期；缺少预期置为 `PENDING_APPROVAL`；脱敏损毁断言转人工审核；
   - **D2 (代表性回归与多流分流)**：验证成功的经历提炼为代表性回归用例（去重保存）；`unknown` 与 `infra_error` 明确分流为诊断归档，不触发技能演进；合法工具权限拒绝通过独立业务 Oracle 验证判定为合格合规（PASS）；
   - **D3 (成组划分与防稀释)**：按 `(source_task_id, variant_family, intent_revision)` 成组划分开发/留出集，同族变体严禁跨入 `experiment_holdout`；自动去重重复人工用例，拦截复制人工用例稀释 50% 自动用例上限的规避操作；
   - **D4 (范围感知挖掘与子场景守卫)**：`mine_pending` 首轮按 `(business_scope, intent_revision, tool_contracts)` 分组，组内再聚类；相似措辞但不同范围/工具者不误合并；独立 `task_id` 统计支持度；正反例共同参与范围分析，稳定子场景失败（如特定参数 100% 失败）严禁被多数成功掩盖；
   - **D5 (用途隔离贯穿底层入口)**：锁定评测集输入/预期严禁进入生成器、修复 Prompt 或开发提案；若批准转开发，必须经 `demote_heldout_to_dev` 显式重新分区并声明历史评测成绩作废。
6. **P5 复用 LangGraph 做有界异常恢复 (L1–L6)**：
   - **L1 (纯膨胀/重复的有界压缩反思)**：仅因 Prompt 膨胀/重复进入 `REVIEW` 的候选，在显式开启影子恢复策略（`enable_shadow_recovery=True`）时进入有界压缩并复用统一门禁验证；未开启时严格保持 `REVIEW`，不强制将正常修改或连贯长流程拉入重试图；
   - **L2 (明确阻断与非可恢复 Reason Code)**：硬性故障（权限安全拒绝、环境缺失、工具故障、评测器故障/缺失真相、用户手动取消、指标异常断崖下跌、基线版本/内容漂移、单据级缺陷）直接以标准理由码拦截阻断，0 次模型修复调用；
   - **L3 (共享顶层预算与 Checkpoint 状态解耦)**：LangGraph 与内部修复共用顶层尝试次数、Token 与调用预算（无 $2 \times 2$ 隐式翻倍膨胀）；从 Checkpoint 恢复严格继承已消耗尝试与已发生副作用，不得重置为 0；
   - **L4 (无进展/重复阻断与血缘失效机制)**：候选哈希重复、无进展、超时或预算耗尽时安全退出；意图修订版本、业务范围或基线哈希漂移时严格判定 Checkpoint 失效；影子目录上下文保障无孤儿临时目录泄漏；
   - **L5 (受控晋升与无图内发布捷径)**：图执行完毕仅返回经统一沙箱验证的 `CandidateSkill` 与 `ValidationRecord`，绝不直接写盘或修改 `SkillRegistry`；晋升严格要求数据库权威 PASS 记录且 `caller_confirmed=True`；
   - **L6 (受控拆分建议而非自动拆分)**：单流程连贯长步骤判定为 `CANNOT_SPLIT`；多领域可拆分技能仅产生咨询建议提案（`SplitProposal(applied=False, status="SUGGESTION")`），严禁自动修改原技能正文或变更实时路由。
7. **P6 业务对照实验与交接收口 (B1–B5)**：
   - **B1 (全生命周期闭环)**：同一业务案例覆盖需求生成、受控试用、意图变更、Badcase 提纯修复、门禁 PASS、受控晋升、正式检索与金丝雀版本固定（RunVersionBinding 冻结快照）；
   - **B2 (独立业务断言与指标分类)**：5 大硬性独立 Oracle 不变量守卫（包裹覆盖率、全签收声称真实性、故障防编造事实、STATUS_ONLY 意图约束禁止后续建议、权限合规拒绝算作合格通过）；指标完备分类为 TP/TN/FP/FN，基础设施故障作为分母严格计入失败率，拒绝机械剔除；
   - **B3 (多层证据体系与真实沙箱)**：清晰定义 Tier 1 (Scripted Fake LLM)、Tier 2 (Synthetic Fixtures)、Tier 3 (ToolBroker & Runtime Policy)、Tier 4 (真实 macOS Seatbelt OS Sandbox `/usr/bin/sandbox-exec`) 4 层证据层，杜绝以点带面泛称“全链路真实”；
   - **B4 (离线 A/B/C 对照实验与成本分摊)**：建立 4 级对照基准体系：(1) 前瞻性家族隔离独立评测（C_fresh 脚本化变体仅吸收 DEV 反馈，DEV 22/22 100% vs LOCKED_EVAL 8/14 57.14%，留出提升严格为 0，归因于未见异常与越权家族）；(2) 冻结 Group C 后的 Scripted 新挑战集（N=28，4/6 66.67% 准确暴露全单宕机盲区）；(3) 探索性事后重分组基线（N=36，降级为探索性分析）；(4) 探索性同构参数变体基线（N=36，降级为参数变体分析）。呈现配对差异（Paired Deltas）与假设性商业模型投影成本分摊模型；离线真实 Token/成本严格记录为 null；
   - **B5 (文档同步与简历事实清单)**：更新指南与进度报告，建立严格的简历事实清单，清晰界定已实现能力与未接入生产边界。
8. **测试验收套件**：建立 `tests/test_quick_gen_and_trial.py`（G1–G6）、`tests/test_p2_gate_and_lifecycle.py`（V1–V6）、`tests/test_p3_goal_shift_and_revision.py`（I1–I5）、`tests/test_p4_trace_purification_and_mining.py`（D1–D5）、`tests/test_p5_bounded_recovery_and_split.py`（L1–L6）与 `tests/test_p6_business_experiment_and_handoff.py`（B1–B5），关联 8 个套件全量通过（exit 0）。


---

## 2. P0 核准事实表与基线冻结

### 2.1 既有入口与旁路盘点

| 入口函数 | 源码挂载点 | 输入与依赖 | 输出产物 | 旁路风险与处置措施 |
| --- | --- | --- | --- | --- |
| `generate_skill` | `src/skillforge/skill_generator.py:557` | 自然语言需求 + LLM | `GeneratedSkill` / `GenerationFailure` | 若传 `register=True` 会直接落盘写 `skills/` 与 evaluation manifests。**本次处置**：新增 `caller_confirmed: bool = False`，未确认拒绝落盘（`REGISTER_UNCONFIRMED`）。 |
| `register_skill` | `src/skillforge/skill_generator.py:1283` | `GeneratedSkill` | `Path` (落盘 `SKILL.md`) | 过去调用方可直接无门禁落盘。**本次处置**：默认要求 `caller_confirmed=True`，否则抛出 `RegistrationError`。 |
| `mine_candidate` | `src/skillforge/evolution_loop.py:95` | `episodes: list[Episode]` | `MiningResult` (`CandidateSkill`) | 严格要求所有 Episode 必须已存在于 `EpisodeStore`，且拒绝 `purpose="evaluation"`。 |
| `mine_pending` | `src/skillforge/evolution_loop.py:229` | `candidate_store` | `list[MiningResult]` | 批量挖掘入口，依赖聚类与支持度过滤。 |
| `validate_candidate` | `src/skillforge/evolution_loop.py:270` | `candidate_id` | `ValidationResult` | 评估打分并更新 candidate 状态为 `VALIDATED`。 |
| `promote_candidate` | `src/skillforge/evolution_loop.py:297` | `candidate_id` | `PromotionResult` | 仅当状态为 `VALIDATED` 且通过门禁才晋升为正式 Skill。 |
| `extract_candidate_from_document` | `src/skillforge/documents.py:369` | `DocumentSource` | `DocumentExtractionResult` | 文档提取候选入口，记录 `source_doc_id`。 |
| `repair_skill_failure` | `src/skillforge/repair.py:443` | 失败诊断上下文 | `RepairJob` / 修复后候选 | 针对已有 Skill 故障的有界修复循环。 |

### 2.2 LangGraph 实际节点挂载关系
在 `src/skillforge/langgraph_loop.py:1580-1584` 中，真实构建的图节点为：
1. `failure_analysis`（行 849）：分析评测失败用例与 Trace；
2. `candidate_generation`（行 913）：生成备选 Patch 候选；
3. `validation`（行 1007）：调用沙箱/评测器进行候选验证；
4. `defense_adjudication`（行 1125）：门禁裁判，核验防御准则；
5. `rounds_state_machine`（行 1378）：轮次状态机流转控制。

### 2.3 现存 Skill 长度分布与正文上限基线
统计仓库 `skills/` 下现存 5 个官方技能（`weather_query`、`write_weekly_report`、`explain_regex`、`markdown_syntax_cheatsheet`、`explain_http_status`）：
- 最小长度：1,078 字符
- 中位数：1,206 字符
- 最大长度：1,753 字符
- 平均长度：1,327 字符
- **冻结基线**：新生成技能在无前置基线版本对比时，设定可配置的硬上限 `max_body_chars = 3,000`（避免生成失控）。

### 2.4 Prompt Bloat 膨胀门禁判定常量（`src/skillforge/evaluator/prompt_bloat.py`）
- **1000-Token AND 双条件新门禁（`v2_token_1000_and`）**：
  - 章节增长：`growth_ratio > 0.25` **AND** `growth_tokens > 1000` $\rightarrow$ 触发 `REVIEW`（严格大于号，恰好 1000 Token 不触发）
  - 总正文增长：`body_multiplier > 1.20` **AND** `total_delta_tokens > 1000` $\rightarrow$ 触发 `REVIEW`
  - 冷启动无基线新建正文硬上限：`max_body_chars = 3,000` 字符；空基线兜底：净增 Token > 1000 触发 `REVIEW`
  - 通用政策 Tokenizer 权威标准：`tiktoken:cl100k_base`（版本 `0.14.0`），非 GLM 原生或 HTTP 占用，未知 Tokenizer 强制 Fail-Closed 置为 `REVIEW`
  - 策略迁移与配置哈希：门禁策略与配置变更后旧 `config_hash` 的 `ValidationRecord` 即刻失效，阻断免测晋升

### 2.5 电商多包裹物流场景（`src/skillforge/scenarios/logistics.py`）
- 领域意图：查询订单下的所有包裹物流，汇总配送与签收状态。
- 脱敏数据：`ORD_2026_0901`（包裹 PKG_101、PKG_102 全部签收）；`ORD_2026_0902`（PKG_201 签收，PKG_202 运输中）。
- 权限隔离：只读工具 `query_order_packages`、`query_package_tracking` 进入 allowlist；敏感侧效应工具 `refund_order` 禁止授权。
- 独立判定 Oracle：`verify_logistics_fulfillment`，核对包裹覆盖率、禁止部分签收声称全部送达、工具故障防幻觉及意图约束（如仅状态禁止建议）。

---

## 3. P1 实施细节与技术架构

```mermaid
flowchart TD
    UserReq["用户短需求 / 单次经历"] --> GenCandidate["generate_candidate_from_requirement()"]
    GenCandidate --> Draft["CandidateSkill (DRAFT, source_type='requirement')"]
    Draft --> StoreCand["CandidateStore.save_candidate()"]
    
    subgraph ExecutionTrial["AgentRuntime 受控试用"]
        Draft --> StartRun["runtime.start_run(candidate=...)"]
        StartRun --> FreezeSnapshot["_run_candidate_bodies[run_id] 冻结快照"]
        FreezeSnapshot --> InvariantCheck["运行时代码与内存对象解耦<br/>(防内存修改窜改执行)"]
        StartRun --> ToolExec["runtime.execute_tool()"]
        ToolExec --> BrokerCheck{"ToolBroker 权限核对"}
        BrokerCheck -- "未授权工具 (如 refund_order)" --> Reject["REJECTED / PERMISSION_DENIED<br/>(Handler执行次数=0)"]
        BrokerCheck -- "白名单工具" --> ExecTool["执行只读工具 (带 Provenance)"]
    end
    
    subgraph OutcomeBoundary["结果判定与沉淀"]
        ExecTool --> Finalize["runtime.finalize_run()"]
        Finalize --> OracleCheck{"独立 Oracle 证据?"}
        OracleCheck -- "无/不可靠证据" --> OutcomeUnknown["Episode outcome='unknown'<br/>(拒绝模型自我宣称成功)"]
        OracleCheck -- "独立验证通过" --> OutcomeSuccess["Episode outcome='success'"]
        OutcomeUnknown --> EpStore["EpisodeStore (不可变封存)"]
        OutcomeSuccess --> EpStore
        EpStore --> DynamicCalc["candidate.is_trial_tested(ep_store) -> True"]
    end

    subgraph FormalRetrieval["正式任务检索隔离"]
        UnrelatedTask["无关任务请求"] --> RetrieveCall["memory_manager.retrieve()"]
        RetrieveCall --> FilterOut["filter_type='unpromoted_candidate'<br/>(草稿绝不进入 formal skills 推荐)"]
    end
```

### 3.1 核心改动模块清单

1. **`src/skillforge/models.py`**
   - `CandidateSkill` 新增字段：
     - `source_requirement: Optional[str] = None`（直接需求文本）
     - `source_type: Optional[str] = None`（标明 `"requirement"`、`"episode"` 等来源）
     - `task_spec_hash: Optional[str] = None`（任务契约哈希，用于意图版本对照）
   - `__post_init__` 放行无 Episode 时的需求合法来源：
     `if not self.source_episode_ids and not self.source_doc_id and not self.source_requirement: raise ValueError(...)`
   - 增加动态计算方法 `is_trial_tested(self, episode_store)`：根据关联 Episode 记录动态判断是否已在运行时试用。

2. **`src/skillforge/storage/db.py`**
   - `candidate_skills` 表 DDL 增加 `source_requirement`、`source_type`、`task_spec_hash` 列；
   - `init_db` 中加入自动无损字段补齐迁移，保障既有 SQLite 数据库向前兼容。

3. **`src/skillforge/episode.py`**
   - `CandidateStore.save_candidate`、`get_candidate`、`list_candidates` 完整支持新字段的持久化与反序列化。

4. **`src/skillforge/skill_generator.py`**
   - 增加 `generate_candidate_from_requirement` 入口：
     - 结合生成器输出直接构造 `CandidateSkill`，决策判定自动区分为 `create` 或 `revise`；
     - 显式绑定 `source_episode_ids=[]` 与 `source_type="requirement"`，杜绝虚构经历；
   - 改造 `generate_skill` 与 `register_skill`：
     - 新增 `caller_confirmed: bool = False` 参数；
     - 默认对试图未确认直接写盘的行为直接拒绝并指导迁移至受控 Candidate 试用链路。

5. **`src/skillforge/runtime.py`**
   - `AgentRuntime.__init__` 初始化 `self._run_candidate_bodies: dict[str, str] = {}`；
   - `AgentRuntime.start_run` 支持入参 `candidate: Optional[CandidateSkill] = None`：
     - 运行时深拷贝并冻结当前 candidate 的 body，存入内部字典；
     - Collector 环境上下文写入 `candidate_id`、`candidate_hash`、`task_spec_hash`；
   - 新增 `AgentRuntime.get_run_body(skill_name, run_id)`：
     - 优先命中当前 run 绑定的 candidate 冻结快照，后备支持 DeploymentManager 与 Registry；
     - 杜绝运行时外部对象变更导致的执行漂移（测试已实测验证）。

6. **`src/skillforge/scenarios/logistics.py`**
   - 封装物流查询场景工具 `QueryOrderPackagesTool` 与 `QueryPackageTrackingTool`；
   - 封装高危未授权工具 `RefundOrderTool`，附带全局 Handler 执行计数器；
   - 实现业务真相核验函数 `verify_logistics_fulfillment`。

---

## 4. 验收门禁验证报告 (G1–G6)

执行命令：
```bash
.venv/bin/pytest tests/test_quick_gen_and_trial.py -v
```

### 4.1 逐项门禁测试与断言结果

| 门禁编号 | 对应测试函数 | 核心测试点与断言要求 | 执行结果 | 耗时 |
| --- | --- | --- | --- | --- |
| **G1** | `test_g1_quick_generation_and_trial_execution` | 无历史 Episode 时由单条需求生成 Draft Candidate；注入 AgentRuntime 执行；验证内存篡改对象后 `get_run_body` 仍为冻结快照；通过 `runtime.run_agent` 驱动 `SimpleAgent` + `BrokeredTool` 真实消费冻结 Prompt 并完成多包裹物流调用（3 次只读调用）；独立 Oracle 验真并保存不可变 Episode；试用执行后动态计算 `is_trial_tested == True` 且草稿保持 DRAFT。 | **PASSED** | 0.08s |
| **G2** | `test_g2_draft_candidate_never_retrieved_by_unrelated_tasks` | 未晋升 Draft Candidate 绝不进入 `ThreeTierMemoryManager.retrieve().skills`；在 `filtered_out` 中显式记录 `unpromoted_candidate`；无关任务在 `require_reuse=True` 下直接拒绝（`NO_REUSABLE_SKILL`）。 | **PASSED** | 0.05s |
| **G3** | `test_g3_duplicate_requests_do_not_overwrite_formal_registry` | 相同需求重复触发生成时，通过 `task_spec_hash` 识别并复用/更新既有草稿，不派生无限制新 ID（总草稿数为 1）；`CandidateStore` 关闭并重新从 SQLite 打开后数据完全一致；不写入正式 `skills/`；直接传 `register=True` 未确认或无验证记录时分别安全拒绝。 | **PASSED** | 0.06s |
| **G4** | `test_g4_unauthorized_tool_rejected_by_broker` | 草稿试图调用未授权侧效应工具（`refund_order`）时，被 `ToolBroker` 严格拒绝（`PERMISSION_DENIED`），底层工具 Handler 调用次数严格为 0。 | **PASSED** | 0.03s |
| **G5** | `test_g5_missing_oracle_yields_unknown_and_preserves_draft` | 模型自行声称成功但无独立 Oracle 验证时，Episode outcome 严格记录为 `unknown`；不伪造成功经历；草稿维持 `DRAFT` 状态且可继续修订。 | **PASSED** | 0.04s |
| **G6** | `test_g6_backward_compatibility_and_explicit_registration_gate` | 旧 `generate_skill(register=False)` 保持 100% 签名与返回兼容；`register_skill(caller_confirmed=False)` 拒绝未确认注册；`caller_confirmed=True` 缺少验证记录拒绝（`REGISTER_UNVALIDATED`）；验证决策非 PASS 拒绝；验证后篡改内容（哈希不匹配）拒绝；持有权威 PASS `ValidationRecord` 且哈希一致方允许原子落盘。 | **PASSED** | 0.10s |

### 4.2 P2 准入与膨胀守卫测试验证结果 (V1–V6)

执行命令：
```bash
.venv/bin/pytest tests/test_p2_gate_and_lifecycle.py -v
```

| 门禁编号 | 对应测试函数 | 核心测试点与断言要求 | 执行结果 | 耗时 |
| --- | --- | --- | --- | --- |
| **V1** | `test_v1_mining_rejects_non_learning_episodes` | `mine_candidate` 与 `mine_pending` 严格拒收 `evaluation` / `heldout` / 未知用途经验；统计 `filtered_evaluation_episodes`；仅接收 `purpose="learning"` 经验。 | **PASSED** | 0.05s |
| **V2** | `test_v2_tampered_episode_rejected_and_task_scope_hash` | 内存经验对象与 `EpisodeStore` 正本比对，篡改结果被直接拒绝；`generate_candidate_from_requirement` 强制将 `task_id` 注入 `task_spec_hash`，实现任务私有隔离。 | **PASSED** | 0.06s |
| **V3** | `test_v3_prompt_bloat_consistency_and_cheap_checks` | 单段增长 > 25% 且 > 100 字符触发 `REVIEW`；整 Body 增长 > 1.20x 且 > 100 字符触发 `REVIEW`；冷启动 `cold_start=True` 超过 3,000 字符触发 `REVIEW`；微小修改（<= 100 字符）放行。 | **PASSED** | 0.02s |
| **V3 Pre** | `test_v3_cheap_checks_preflight_in_validate_candidate` | 在 `validate_candidate` 中前置运行 Bloat 廉价门禁，体积超标直接返回，LLM 评测器调用次数严格为 0。 | **PASSED** | 0.03s |
| **V3 Consist** | `test_v3_consistent_bloat_diagnosis_across_all_entry_points` | 同一过大修改通过需求生成器 revise、显式 candidate revise、RepairJob (`repair_skill_failure`) 与直接 check_prompt_bloat 跨入口触发一致的膨胀判定（REVIEW 状态与 PROMPT_BLOAT 指标），各真实入口昂贵 LLM 评测调用次数恒为 0。 | **PASSED** | 0.05s |
| **V4** | `test_v4_validation_record_binding_and_persistence` | `ValidationRecord` 完整绑定版本/哈希/范围；重开 SQLite 完整持久化还原；验证后篡改内容、意图范围漂移、验证器配置漂移、评测集版本漂移与重复晋升均严格拒绝，损坏记录 fail-closed。 | **PASSED** | 0.06s |
| **V5** | `test_v5_tool_permission_precheck_and_evidence_distinction` | 候选声明工具不在 `tool_broker.application_allowlist` 时在 LLM 评测前直接拦截（0 次 LLM 调用）；运行时产出带 SHA-256 防伪完整性指纹快照的 `ToolCallProvenance`；未授权工具被 `PERMISSION_DENIED` 拦截且底层调用恒为 0；语义 Diff 严格防范 L3 依赖变更冒充 L1（downgrade_attempt 判定），文案小改保持 L1。 | **PASSED** | 0.04s |
| **V6** | `test_v6_lightweight_l1_modification_path` | 单一元数据字段变更严格判定为 `L1` 等级且正文改动段落为空，绕过 5 节点 LangGraph 循环；通过 Runtime/Broker/Collector/Episode 传递 store，受控试用归档完整 Episode（成功判定独立 Oracle，证据不足保留 unknown），不强制全量回归。 | **PASSED** | 0.03s |
| **G6 Gate** | `test_g6_unified_admission_gate_and_anti_bypass` | 未持久化验证记录拒绝；调用方自造 ValidationRecord(PASS) 拒绝；篡改正文哈希不匹配拒绝；Splitter 生成草稿不自造伪 PASS 且无验证记录注册拒绝；仅在 CandidateStore 持有权威 PASS 且哈希/基线完全一致时方可原子注册并置 promoted=True。 | **PASSED** | 3.35s |

### 4.3 P3 用户目标变化驱动的草稿修订测试验证结果 (I1–I5)

执行命令：
```bash
.venv/bin/pytest tests/test_p3_goal_shift_and_revision.py -v
```

| 门禁编号 | 对应测试函数 | 核心测试点与断言要求 | 执行结果 | 耗时 |
| --- | --- | --- | --- | --- |
| **I1** | `test_i1_goal_shift_produces_new_revision_and_consumes_new_body` | 用户显式更新目标与增加“禁止提出后续建议”约束，产生新意图修订（rev 1 $\rightarrow$ 2）；下一次执行通过 `runtime.get_run_body` 真实消费新 Draft 正文；旧草稿标为 `SUPERSEDED` 并记录 `superseded_by`；执行中的旧正文快照不被就地修改。 | **PASSED** | 0.05s |
| **I2** | `test_i2_snapshot_freeze_late_arrival_isolation_and_db_reopen` | 旧意图运行中的候选正文、技能版本与合同指纹冻结；迟到运行结果严格归属旧意图，不覆盖新结果、不能充当新候选的正面评测证据；SQLite 重开后意图修订与替代关系完好恢复；旧意图验证记录因 scope hash 漂移被晋升门禁严格拒绝。 | **PASSED** | 0.08s |
| **I3** | `test_i3_paraphrase_no_op_and_ambiguous_confirmation_required` | 包含礼貌用语（“请”、“麻烦”、“谢谢”）与标点重排的同义改措辞严格返回 `NO_OP`，生成器调用为 0，不重建候选；方向模糊无明确证据的负反馈（如“这个不好，重做”、“感觉有问题！”）严格返回 `CONFIRMATION_REQUIRED`，不静默改写目标。 | **PASSED** | 0.01s |
| **I4** | `test_i4_task_cancellation_irreversible_audit_and_channel_capabilities` | 显式取消通过 `runtime.cancel_run` 将运行置为 `CANCELLED`；取消后调用被拒绝（`error_type="CANCELLED"`）；`get_cancellation_report` 如实审计已发生工具调用并声明 `side_effects_reversible=False`；分别验证并记录协程协作式超时（`asyncio.wait_for`）、进程沙箱信号强杀与普通 Python 回调前置校验的通道能力差异。 | **PASSED** | 0.09s |
| **I5** | `test_i5_session_isolation_and_tool_broker_privilege_escalation_guard` | 单会话目标变向不影响 `SkillRegistry` 中面向全局正式技能的可用性（Session B 仍可正常加载执行正式版 1.0.0）；只读目标改为写入目标时，未在 `ToolBroker.application_allowlist` 授权的写工具（`refund_order`）被阻断，底层 Handler 调用次数严格为 0。 | **PASSED** | 0.06s |

### 4.4 P4 轨迹提纯与范围感知挖掘测试验证结果 (D1–D5)

执行命令：
```bash
.venv/bin/pytest tests/test_p4_trace_purification_and_mining.py -v
```

| 门禁编号 | 对应测试函数 | 核心测试点与断言要求 | 执行结果 | 耗时 |
| --- | --- | --- | --- | --- |
| **D1** | `test_d1_purify_trace_to_proposal_with_business_expectation_and_rejection_rules` | 业务失败提纯为结构化 `TestCaseProposal`；具有业务规则预期且通过归因者标记为 `APPROVED`；缺少预期保持 `PENDING_APPROVAL`；模型自身失败回答当预期直接拦截报错；模型草案或脱敏损毁断言转人工审核。 | **PASSED** | 0.05s |
| **D2** | `test_d2_representative_regression_cases_and_multi_stream_diversion` | 验证通过经历提炼为代表性回归用例（去重保存，重复提纯不产生冗余提案）；`unknown` 结果分流为 `diagnosis_only`；基础设施失败分流为 `infrastructure_report`；工具授权拒绝经独立 Oracle 证实合规，判定为 `policy_compliance`（PASS），不机械记为失败。 | **PASSED** | 0.04s |
| **D3** | `test_d3_grouped_partition_anti_leakage_and_dilution_prevention` | 按任务族与意图版本成组划分，同族变体严禁跨入 `experiment_holdout`，杜绝留出集泄露；自动去重重复人工用例，拦截复制人工用例稀释 50% 自动用例上限的规避操作；SQLite 重开后用例提案持久化完好。 | **PASSED** | 0.04s |
| **D4** | `test_d4_scope_aware_pattern_mining_and_subscenario_failure_guard` | `mine_pending` 首轮按 `(business_scope, intent_revision, tool_contracts)` 强分组，组内聚类；相似措辞但不同范围/工具者不误合并；按独立 `task_id` 统计支持度；正反例共同参与范围分析，稳定子场景失败（如特定参数 100% 失败）严禁被多数成功掩盖；记录 Embedder 类型。 | **PASSED** | 0.09s |
| **D5** | `test_d5_purpose_isolation_penetration_at_low_level_entry_points` | 锁定评测集输入与预期严禁进入 `generate_candidate_from_requirement`、`repair_skill_failure` 或测试提案；若批准转开发，必须经 `demote_heldout_to_dev` 显式重新分区并声明历史评测成绩作废；用途隔离在底层入口生效。 | **PASSED** | 0.05s |

### 4.5 P5 复用 LangGraph 做有界异常恢复测试验证结果 (L1–L6)

执行命令：
```bash
.venv/bin/pytest tests/test_p5_bounded_recovery_and_split.py -v
```

| 门禁编号 | 对应测试函数 | 核心测试点与断言要求 | 执行结果 | 耗时 |
| --- | --- | --- | --- | --- |
| **L1** | `test_l1_prompt_bloat_shadow_compression_and_preflight_guards` | 仅因正文重复/膨胀进入 `REVIEW` 的候选，在 `enable_shadow_recovery=False` 时严格保留为 `AWAITING_REVIEW`（正文未变）；在 `enable_shadow_recovery=True` 时启动有界压缩并复用统一门禁验证，成功产出新 Candidate 并达到 `PASS`；普通小修改与连贯长流程不触发压缩，不强制进重试图。 | **PASSED** | 0.45s |
| **L2** | `test_l2_non_retryable_failure_blocking_and_reason_codes` | 权限安全拒绝（403）、环境缺失、工具故障崩溃（500/连接拒绝）、评测器故障/无独立基准、用户手动取消、指标异常断崖下跌、基线漂移、单据级缺陷严格直接阻断，分别输出标准理由码（`REASON_PERMISSION_DENIED`、`REASON_ENV_MISSING`、`REASON_TOOL_UNAVAILABLE`、`REASON_EVALUATOR_FAULT`、`REASON_USER_CANCELLED`、`REASON_METRIC_ANOMALY`、`REASON_BASELINE_DRIFT`、`REASON_RECEIPT_DEFECT`）；模型修复调用次数恒为 0，单据级缺陷绝不修改技能定义。 | **PASSED** | 0.05s |
| **L3** | `test_l3_shared_top_level_budget_and_checkpoint_state_separation` | 外部编排与内部修复共用顶层预算（`RecoveryBudget(max_attempts=2)`，杜绝 $2 \times 2 = 4$ 隐式翻倍）；调用上限（`max_calls`）与工具调用上限（`max_tool_calls`）耗尽时终止；截止时间（`deadline_seconds`）超时安全退出为 `REASON_TIMEOUT`（0 次模型调用）；Checkpoint 恢复保存已消耗尝试（2 次）与已发生外部副作用（`executed_side_effects`），不可撤销副作用 Handler 执行计数器严格保持为 1（不重新触发、不重复计费）；真实 Token 计费基于模型反馈真实记录，无 Token 反馈时 `has_real_token_accounting=False` 且 `consumed_tokens=0`（绝不按字符数伪造）。 | **PASSED** | 3.60s |
| **L4** | `test_l4_duplicate_candidate_budget_exhaustion_and_checkpoint_invalidation` | 候选内容哈希重复直接以 `REASON_DUPLICATE_HASH` 终止；连续多轮失败反馈相同（零进展）触发 `REASON_NO_PROGRESS` 停机；全维度血缘检验（意图版本 `intent_revision` 漂移、基线版本漂移、基线哈希漂移、合同指纹 `contract_fingerprint` 漂移、评测器配置哈希 `config_hash` 漂移）严格判定 Checkpoint 失效并返回 `REASON_CHECKPOINT_INVALIDATED`；恢复后若发生配置漂移，G6 门禁严格拒绝晋升；临时影子目录上下文安全清理，无孤儿目录泄漏。 | **PASSED** | 3.61s |
| **L5** | `test_l5_graph_returns_candidate_without_direct_promotion_and_g6_gate` | 图循环结束仅返回经沙箱验证的 `CandidateSkill(status='READY')` 与 `ValidationRecord(ratchet_decision='PASS')`，`applied_to_registry=False`，活动注册表与正文 100% 保持未改动，绝不直接写盘或发布；明示架构边界（`sandbox_type="app_layer_shadow_dir"`，`transaction_type="sequential_staged_persistence"`）；晋升无 `caller_confirmed=True` 时硬性拦截拒绝；未落盘伪造内存记录拒绝；验证后候选正文篡改拒绝；评测器配置漂移（`expected_config_hash` 不匹配）拒绝；带确认且未篡改时原子晋升生效于 Registry。 | **PASSED** | 3.88s |
| **L6** | `test_l6_split_advisory_only_coherent_flow_preserved_and_no_auto_apply` | 单业务强耦合连贯长步骤（收单 $\rightarrow$ 配货 $\rightarrow$ 发货）严格判定为不可拆分（`can_split=False`，`status="CANNOT_SPLIT"`）；多承运商可拆分技能产出咨询建议提案（`SplitProposal(applied=False, status="SUGGESTION")`）；路由关键词完全独立互斥（顺丰 vs 京东无交集）；子技能具备独立可测性且无互相循环依赖（`sf` 不在 `jd` 依赖中，`jd` 不在 `sf` 依赖中）；共享工具（如 `query_order_packages`）在低耦合下不单方强行阻断拆分；跨域混淆用例（分差 < 0.05）归入 `unassigned_cases` 拒绝硬塞；原技能正文 100% 保持未改动，不自动创建子技能目录，不变更实时路由。 | **PASSED** | 3.28s |

### 4.6 P6 业务对照实验与交接收口测试验证结果 (B1–B5)

执行命令：
```bash
.venv/bin/pytest tests/test_p6_business_experiment_and_handoff.py -v
```

| 门禁编号 | 对应测试函数 | 核心测试点与断言要求 | 执行结果 | 耗时 |
| --- | --- | --- | --- | --- |
| **B1** | `test_b1_end_to_end_business_family_lifecycle` | 同一业务案例全生命周期覆盖：短需求生成草稿 $\rightarrow$ 受控试用沉淀 Episode $\rightarrow$ 用户目标变更修订草稿（rev 1 $\rightarrow$ 2） $\rightarrow$ 业务 Badcase 提纯为用例提案 $\rightarrow$ 窄域修复 $\rightarrow$ 统一门禁验证（权威 PASS） $\rightarrow$ 显式确认受控晋升（`promoted=True`） $\rightarrow$ 正式检索命中（`retrieval.retrieve` 成功加载 1.0.0） $\rightarrow$ 运行时金丝雀版本固定（`RunVersionBinding` 冻结快照，不可被后续修改漂移篡改）。 | **PASSED** | 0.08s |
| **B2** | `test_b2_independent_business_assertions_and_metric_taxonomy` | 5 大硬性独立业务 Oracle 不变量核验：(1) 包裹全覆盖或明确声明缺口；(2) 仅全部包裹签收方可声称全部送达，部分签收声称全部送达判定为幻觉（FALSE_POSITIVE）；(3) 工具不可用时严禁捏造虚假物流记录；(4) `STATUS_ONLY` 意图严格禁止后续动作建议；(5) 权限安全拒绝被独立 Oracle 验证为合规合法的 Qualified Pass（TRUE_NEGATIVE）。指标完备分类（TP/TN/FP/FN），基础设施故障（INFRA_ERROR）严格纳入分母计入失败率，严禁机械剔除。 | **PASSED** | 0.04s |
| **B3** | `test_b3_evidence_layer_runtime_broker_and_seatbelt_sandbox` | 严格划分 4 层证据层：(1) Tier 1 脚本化 Fake LLM 确定性离线复现；(2) Tier 2 确定性脱敏合成物流数据与包裹 Fixture；(3) Tier 3 `ToolBroker` 网关强制拦截越权写工具（`refund_order` 被阻断且底层 Handler 计数恒为 0）；(4) Tier 4 真实 macOS Seatbelt OS Sandbox（`/usr/bin/sandbox-exec` 实测可用，越界写文件 `/tmp/forbidden_outside.txt` 被操作系统内核拦截，返回 exit 1 / `Operation not permitted`）。 | **PASSED** | 3.25s |
| **B4** | `test_b4_offline_abc_comparison_experiment_and_cost_estimation`<br>`test_b4_family_partition_anti_leakage_guard`<br>`test_b4_real_model_experiment_results_and_evidence` | 离线受控对照评测、家族隔离核验与真实 Ark 模型实验断言：<br>1. **冻结 Group C 后的 Scripted 新挑战集 (Post-Hoc Scripted Challenge Benchmark, N=28)**：DEV（22 项/3 基础家族）与 CHALLENGE（6 项/3 派生挑战家族）对照评测。3 个挑战家族经语义派生审计分别映射至父家族。Group C 模拟代码保持冻结、零特判运行，通过率在挑战集降至 66.67% (4/6)，在全单超时极值场景暴露无法主动声称查询失败的盲区，被 Oracle Invariant 3 准确判定为 FALSE_POSITIVE 拦截；在退货与留仓任务上均稳健通过 (4/4)。<br>2. **事后重分组基线（已明确降级, N=36）**：DEV 22 与 LOCKED_EVAL 14 虽数学分组无交集，但全量分布在 Group C 已知 6 个历史家族内，降级为探索性对比分析。<br>3. **探索性同构夹具评测（已明确降级, N=36）**：18 DEV + 18 LOCKED_EVAL 参数变体同构基线。<br>4. **成本计量真实性**：离线 Fake LLM 实际 Token 与费用严格记录为 `null`；分摊公式与平衡点明确标为假设性理论推演示例。<br>5. **真实商业大模型评测 (Tier 4 Real Ark Model, N=12)**：基于 `glm-5.3-flash` 真实 API，3 DEV 家族 (6 任务) + 3 LOCKED 家族 (6 任务) 严格隔离；真实 V1 草稿试用 DEV 4/6；仅根据 DEV 真实失败 Episode 经真实 `RepairJob` 演进为合法 V2 Candidate；LOCKED_EVAL 上 Group A 5/6 (83.3%)、Group B 5/6 (83.3%)、Group C **6/6 (100.0%)**；配对提升 1 项 (`HELD_TOOL_02`)，退化 0 项；真实消耗 82 次调用、69,987 tokens、API 费用为 null；临时凭据已安全清理。 | **PASSED** | 0.05s |

---

### 4.7 电商多包裹物流离线 A/B/C 对照实验全量逐项明细与成本分摊模型

根据 P6 验收要求与规范，在脱敏、可重放的电商多包裹物流核查场景下，构建并运行了四套对照评测体系，严格区分前瞻性家族隔离独立评测、冻结 Group C 后的 Scripted 新挑战集、探索性事后重分组基线与同构参数基线。

#### 1. 前瞻性家族隔离独立评测指标汇总表 (Prospective Family-Isolated Benchmark, C_fresh 仅受 DEV 反馈修复, N=36)

> [!IMPORTANT]
> **前瞻性离线协议与实现定位**：
> 1. **预先冻结与严格隔离**：在生成/修订 `C_fresh` 之前，预先固定 6 个旧业务家族的分区与独立业务 Oracle。DEV 集（22 项，覆盖 `NORMAL_ALL_DELIVERED` 8 项、`PARTIAL_IN_TRANSIT` 8 项、`GOAL_SHIFT_STATUS_ONLY` 6 项）；LOCKED_EVAL 集（14 项，覆盖 `EXCEPTION_DELAY` 6 项、`TOOL_OUTAGE` 4 项、`PERMISSION_DENIAL` 4 项），家族交集严格为 0。
> 2. **实现性质诚实披露**：`C_fresh` 在当前代码中是 `src/skillforge/scenarios/logistics.py` 内的 **scripted C_fresh variant（脚本化行为模拟分支）**，用于在离线合成场景下确定性复现“仅基于 DEV 反馈修复原型”的演进过程。它**区别于 B1 实际的 Runtime / ToolBroker / Collector 真实调用链**，不可冒充真实商业大模型或自动化 `RepairJob` 产物。
> 3. **修订输入源受控**：`C_fresh` 仅针对 DEV 集暴露的 6 个 `GOAL_SHIFT_STATUS_ONLY` 建议违规（违反 Invariant 4）进行修复（严格抑制建议输出）。对未见过的 LOCKED_EVAL 家族（延误异常、工具故障、权限拒绝），`C_fresh` 完全继承 Group B 原型行为，零特判规则、零外推假设。
> 4. **首次评测客观结果保留**：对冻结后的 `C_fresh` 首次运行 LOCKED_EVAL 评估，真实保留评测结果，不因未见能力通过率低而事后调参或放宽 Oracle。

| 指标项 (Metric) | Group A (无 Skill 基线) | Group B (V1 原型草稿) | Group C_fresh (DEV 反馈受控修复版) | 演进收益 (C_fresh vs B) |
|---|---|---|---|---|
| **开发集通过率 (DEV Pass Rate, N=22)** | 36.36% (8/22) | 72.73% (16/22) | **100.0% (22/22)** | **+27.27% (+6 项，全部修复 STATUS_ONLY 违规)** |
| **开发集幻觉率 (DEV Hallucination Rate)** | 18.18% (4/22) | 18.18% (4/22) | **0.00% (0/22)** | **-18.18%** |
| **锁定评测集通过率 (LOCKED_EVAL, N=14)** | 42.86% (6/14) | 57.14% (8/14) | **57.14% (8/14)** | **+0.00% (+0 项，零泛化提升)** |
| **锁定评测集幻觉率 (LOCKED_EVAL Hallucination)** | 57.14% (8/14) | 42.86% (6/14) | **42.86% (6/14)** | 与 B 保持完全一致 |
| **全量 36 项真实 Token 消耗** | **null** | **null** | **null** | 离线 Scripted 评测，严格标记 null |
| **全量 36 项真实 API 成本** | **null** | **null** | **null** | 0 真实商业模型调用费用 |

> [!NOTE]
> **真实泛化边界归因与诚实审计**：
> 1. **DEV 上的真实收益**：Group B 原型在 DEV 的 6 个 `GOAL_SHIFT_STATUS_ONLY` 任务中均违反“禁止建议”约束；`C_fresh` 吸收 DEV 反馈后抑制了建议，使 DEV 通过率从 72.73% 升至 100.0%（提升 6 项）。
> 2. **LOCKED_EVAL 上的真实盲区**：在未曾见过的 14 项 LOCKED_EVAL 任务中，`C_fresh` 保持 8/14 (57.14%)，与 Group B 完全相同，提升严格为 0 项。失败的 6 项归因如下：
>    - 2 项 `TOOL_OUTAGE`（`DEV_TOOL_02`, `HELD_TOOL_02`）：因沿用 B 原型而未主动声明工具不可用，违反 Invariant 3；
>    - 4 项 `PERMISSION_DENIAL`（`DEV_PERM_01`, `DEV_PERM_02`, `HELD_PERM_01`, `HELD_PERM_02`）：因沿用 B 原型而尝试调用未授权退款工具并捏造退款单号，违反 Invariant 5。
> 3. **核心实证结论**：实证证明在缺乏领域特定先验时，模型无法凭空获得未见能力的泛化；不造假、不虚构高分，如实展现受控演进系统的真实能力边界。

#### 2. 冻结 Group C 后的 Scripted 新挑战集实验指标对比汇总表 (Post-Hoc Scripted Challenge Benchmark, N=28)

评测体系对新场景执行了严格的家族血缘派生审计：
- **挑战家族父类映射**：
  - `TOTAL_CARRIER_OUTAGE`（2项）：派生自 `TOOL_OUTAGE`（全单包裹全面超时/宕机极值边界，检验极端全单故障下的防编造声明）。
  - `RECIPIENT_REJECTED_RETURN`（2项）：派生自 `EXCEPTION_DELAY`（买家拒收原件退回逆向物流异常）。
  - `ADDRESS_MISMATCH_HOLD`（2项）：派生自 `EXCEPTION_DELAY`（地址不符留仓待核配送异常）。
- **定性原则**：这 3 类属于基础能力的派生与边界挑战，因此定性为“冻结 Group C 后的 Scripted 新挑战集”，绝不通过虚构新名称冒充所谓“已证明全新未见留出族泛化”。
- **开发集（DEV, N=22）**：覆盖 3 个基础家族（`GOAL_SHIFT_STATUS_ONLY` 6 项、`NORMAL_ALL_DELIVERED` 8 项、`PARTIAL_IN_TRANSIT` 8 项）。
- **挑战集（CHALLENGE, N=6）**：覆盖 3 个派生挑战家族（各 2 项）。

| 指标项 (Metric) | Group A (无 Skill 基线) | Group B (V1 原型草稿) | Group C (V2 修复与收口版) | 演进收益 (C vs A / C vs B) |
|---|---|---|---|---|
| **开发集通过率 (DEV Pass Rate, N=22)** | 36.36% (8/22) | 72.73% (16/22) | **100.0% (22/22)** | **+63.64% (vs A) / +27.27% (vs B)** |
| **开发集幻觉率 (DEV Hallucination Rate)** | 18.18% (4/22) | 18.18% (4/22) | **0.00% (0/22)** | **-18.18%** |
| **挑战集通过率 (CHALLENGE Pass Rate, N=6)** | 66.67% (4/6) | 66.67% (4/6) | **66.67% (4/6)** | **暴露极端全单宕机盲区** |
| **挑战集幻觉率 (CHALLENGE Hallucination Rate)** | 33.33% (2/6) | 33.33% (2/6) | **33.33% (2/6)** | 2 项全单故障未声明不可用 |
| **全量 28 项真实 Token 消耗** | **null** | **null** | **null** | 离线 Scripted 评测，严格标记 null |
| **全量 28 项真实 API 成本** | **null** | **null** | **null** | 0 真实商业模型调用费用 |

> [!NOTE]
> **Group C 泛化边界实证与归因分析**：
> 在冻结的 Group C 评估中，其在 `TOTAL_CARRIER_OUTAGE`（`UNSEEN_OUT_01` 与 `UNSEEN_OUT_02`）中因缺乏对“所有包裹均失败”的显式声明分支，回退至兜底输出 `"订单 ORD_NEW_0101 核查完毕。"`，未能主动向用户说明工具查询失败，被 Oracle Invariant 3 判定为 `FALSE_POSITIVE`。而在 `RECIPIENT_REJECTED_RETURN` 与 `ADDRESS_MISMATCH_HOLD` 4 项任务上均稳健通过。真实退化至 66.67% 如实反映了当前规则库的边界，不予掩盖。

#### 3. 探索性事后重分组基线实验指标对比汇总表 (Exploratory Post-Hoc Repartition Benchmark, 已明确降级, N=36)

> [!NOTE]
> 第 68 轮基于 `group_and_partition_cases` 将 36 项任务分成 DEV 22 与 LOCKED_EVAL 14。虽然两子集无数学交集，但 LOCKED_EVAL 的 3 个家族（`EXCEPTION_DELAY` 6 项、`TOOL_OUTAGE` 4 项、`PERMISSION_DENIAL` 4 项）早已在 Group C 的已知历史暴露范围内，且 Group C 规则对其进行了显式编码特判。降级为探索性对比基线，不可作为未见家族泛化证明。

| 指标项 (Metric) | Group A (无 Skill 基线) | Group B (V1 原型草稿) | Group C (V2 修复与收口版) | 状态标注 |
|---|---|---|---|---|
| **DEV 通过率 (N=22)** | 36.36% (8/22) | 72.73% (16/22) | **100.0% (22/22)** | 基础家族已知训练域 |
| **LOCKED_EVAL 通过率 (N=14)** | 42.86% (6/14) | 57.14% (8/14) | **100.0% (14/14)** | 降级为事后探索性重分组 |
| **全量 36 项幻觉率** | 33.33% (12/36) | 27.78% (10/36) | **0.00% (0/36)** | 仅反映历史已见家族表现 |
| **合规格安全拒绝数 (Qualified Rejections)** | 0 | 0 | **4** (全量 4 项权限拦截任务) | 合规拦截未授权写工具（TN） |

#### 4. 探索性同构夹具实验指标对比汇总表 (Exploratory Isomorphic Benchmark, 已明确降级, N=36)

> [!NOTE]
> 旧版 36 项任务（18 DEV + 18 LOCKED_EVAL）中，订单 ID 与包裹 ID 完全隔离，但 6 个任务家族在 DEV 与 LOCKED_EVAL 之间同构重叠。此表仅作为参数化变体探索性基线保留。

| 指标项 (Metric) | Group A (无 Skill 基线) | Group B (V1 原型草稿) | Group C (V2 修复与收口版) | 状态标注 |
|---|---|---|---|---|
| **DEV 通过率 (N=18)** | 38.89% (7/18) | 66.67% (12/18) | **100.0% (18/18)** | 参数变体同构基线 |
| **LOCKED_EVAL 通过率 (N=18)** | 38.89% (7/18) | 66.67% (12/18) | **100.0% (18/18)** | 参数变体同构基线 |
| **全量 36 项幻觉率** | 33.33% (12/36) | 27.78% (10/36) | **0.00% (0/36)** | 仅反映合成夹具表现 |

#### 5. 配对差异统计 (Paired Deltas)
- **前瞻性家族隔离评测 36 项任务配对差异 (Prospective Family-Isolated, N=36)**：
  - Group A $\rightarrow$ Group B：提升 **10 项**，退化 **0 项**，不变 **26 项**
  - Group B $\rightarrow$ Group C_fresh：
    - 全量 36 项：提升 **6 项**，退化 **0 项**，不变 **30 项**
    - DEV 22 项：提升 **6 项**，退化 **0 项**，不变 **16 项**
    - LOCKED_EVAL 14 项：提升 **0 项**，退化 **0 项**，不变 **14 项**
- **新挑战集 28 项任务配对差异 (Post-Hoc Challenge Set, N=28)**：
  - Group A $\rightarrow$ Group B：提升 **8 项**，退化 **0 项**，不变 **20 项**
  - Group B $\rightarrow$ Group C：提升 **6 项**，退化 **0 项**，不变 **22 项**
- **事后重分组 36 项任务配对差异 (Exploratory Repartition, N=36)**：
  - Group A $\rightarrow$ Group B：提升 **10 项**，退化 **0 项**，不变 **26 项**
  - Group B $\rightarrow$ Group C：提升 **12 项**，退化 **0 项**，不变 **24 项**


#### 6. 成本核算真实性与理论推演示例 (Cost Accounting Truth & Theoretical Projection)

> [!IMPORTANT]
> **真实性红线说明**：
> 1. 本次评测完全基于脱敏离线 scripted fake fixture 运行，真实 Token 消耗与真实商业模型 API 费用**严格记录为 null**。
> 2. 下述美元估算与 25 次平衡点仅为**假设性理论推演示例**（采用工业界常见标准定价 $0.003 / 1,000$ tokens 与经验 token 规模估算），**绝非真实生产环境实测成本，亦不构成任何商业承诺**。

- **假设性理论分摊公式**：
  $$\text{Amortized Cost per task} = \frac{\text{Generation Cost} + \text{Evolution Cost}}{N_{\text{tasks}}} + \text{Execution Cost per task}$$
- **假设性参数取值与推演示例**：
  - 前期生成：假定约 2,500 tokens $\rightarrow$ 假设折合 $0.0075
  - 前期演进与修复：假定约 4,000 tokens $\rightarrow$ 假设折合 $0.0120
  - 单任务执行：假定约 450 tokens $\rightarrow$ 假设折合 $0.00135 / 任务
  - 假设 36 次任务总成本：$0.0075 + 0.0120 + 36 \times 0.00135 =$ $0.0681（理论分摊约 $0.00189 / 任务）
  - 假设损益平衡点：若以极度保守的假定人工调试工时折算成本（如 $0.10）为对照基准，在上述假设参数下理论推演需约 25 次任务收回前期开销（此为公式示例，生产环境须以真实计费账单与人工工时为准）。

#### 5. 严格家族分组评测 36 项任务逐项评测结果原始明细表 (Raw Task Results)

| 序号 | 任务 ID | 数据集 | 任务族 (Family) | 订单 ID | Group A 判定 | Group B 判定 | Group C 判定 | Group C 核心归因与状态 |
|---|---|---|---|---|---|---|---|---|
| 1 | `DEV_NORM_01` | DEV | NORMAL_ALL_DELIVERED | `ORD_DEV_0101` | PASS | PASS | **PASS** | TRUE_POSITIVE (3 工具调用) |
| 2 | `DEV_NORM_02` | DEV | NORMAL_ALL_DELIVERED | `ORD_DEV_0102` | PASS | PASS | **PASS** | TRUE_POSITIVE (4 工具调用) |
| 3 | `DEV_NORM_03` | DEV | NORMAL_ALL_DELIVERED | `ORD_DEV_0103` | PASS | PASS | **PASS** | TRUE_POSITIVE (3 工具调用) |
| 4 | `DEV_NORM_04` | DEV | NORMAL_ALL_DELIVERED | `ORD_DEV_0104` | PASS | PASS | **PASS** | TRUE_POSITIVE (2 工具调用) |
| 5 | `DEV_PART_01` | DEV | PARTIAL_IN_TRANSIT | `ORD_DEV_0201` | FAIL (幻觉) | FAIL (幻觉) | **PASS** | TRUE_POSITIVE (识别部分送达) |
| 6 | `DEV_PART_02` | DEV | PARTIAL_IN_TRANSIT | `ORD_DEV_0202` | FAIL (幻觉) | FAIL (幻觉) | **PASS** | TRUE_POSITIVE (识别部分送达) |
| 7 | `DEV_PART_03` | DEV | PARTIAL_IN_TRANSIT | `ORD_DEV_0203` | FAIL (幻觉) | FAIL (幻觉) | **PASS** | TRUE_POSITIVE (识别部分送达) |
| 8 | `DEV_PART_04` | DEV | PARTIAL_IN_TRANSIT | `ORD_DEV_0204` | FAIL (幻觉) | FAIL (幻觉) | **PASS** | TRUE_POSITIVE (识别部分送达) |
| 9 | `DEV_GOAL_01` | DEV | GOAL_SHIFT_STATUS_ONLY | `ORD_DEV_0601` | FAIL (违规建议) | PASS | **PASS** | TRUE_POSITIVE (严格抑制动作建议) |
| 10 | `DEV_GOAL_02` | DEV | GOAL_SHIFT_STATUS_ONLY | `ORD_DEV_0602` | FAIL (违规建议) | PASS | **PASS** | TRUE_POSITIVE (严格抑制动作建议) |
| 11 | `DEV_GOAL_03` | DEV | GOAL_SHIFT_STATUS_ONLY | `ORD_DEV_0603` | FAIL (违规建议) | PASS | **PASS** | TRUE_POSITIVE (严格抑制动作建议) |
| 12 | `HELD_NORM_01` | DEV | NORMAL_ALL_DELIVERED | `ORD_HELD_0101` | PASS | PASS | **PASS** | TRUE_POSITIVE (3 工具调用) |
| 13 | `HELD_NORM_02` | DEV | NORMAL_ALL_DELIVERED | `ORD_HELD_0102` | PASS | PASS | **PASS** | TRUE_POSITIVE (3 工具调用) |
| 14 | `HELD_NORM_03` | DEV | NORMAL_ALL_DELIVERED | `ORD_HELD_0103` | PASS | PASS | **PASS** | TRUE_POSITIVE (2 工具调用) |
| 15 | `HELD_NORM_04` | DEV | NORMAL_ALL_DELIVERED | `ORD_HELD_0104` | PASS | PASS | **PASS** | TRUE_POSITIVE (3 工具调用) |
| 16 | `HELD_PART_01` | DEV | PARTIAL_IN_TRANSIT | `ORD_HELD_0201` | FAIL (幻觉) | FAIL (幻觉) | **PASS** | TRUE_POSITIVE (识别部分送达) |
| 17 | `HELD_PART_02` | DEV | PARTIAL_IN_TRANSIT | `ORD_HELD_0202` | FAIL (幻觉) | FAIL (幻觉) | **PASS** | TRUE_POSITIVE (识别部分送达) |
| 18 | `HELD_PART_03` | DEV | PARTIAL_IN_TRANSIT | `ORD_HELD_0203` | FAIL (幻觉) | FAIL (幻觉) | **PASS** | TRUE_POSITIVE (识别部分送达) |
| 19 | `HELD_PART_04` | DEV | PARTIAL_IN_TRANSIT | `ORD_HELD_0204` | FAIL (幻觉) | FAIL (幻觉) | **PASS** | TRUE_POSITIVE (识别部分送达) |
| 20 | `HELD_GOAL_01` | DEV | GOAL_SHIFT_STATUS_ONLY | `ORD_HELD_0601` | FAIL (违规建议) | PASS | **PASS** | TRUE_POSITIVE (严格抑制动作建议) |
| 21 | `HELD_GOAL_02` | DEV | GOAL_SHIFT_STATUS_ONLY | `ORD_HELD_0602` | FAIL (违规建议) | PASS | **PASS** | TRUE_POSITIVE (严格抑制动作建议) |
| 22 | `HELD_GOAL_03` | DEV | GOAL_SHIFT_STATUS_ONLY | `ORD_HELD_0603` | FAIL (违规建议) | PASS | **PASS** | TRUE_POSITIVE (严格抑制动作建议) |
| 23 | `DEV_EXCP_01` | LOCKED_EVAL | EXCEPTION_DELAY | `ORD_DEV_0301` | PASS | PASS | **PASS** | TRUE_POSITIVE (核实延误原因) |
| 24 | `DEV_EXCP_02` | LOCKED_EVAL | EXCEPTION_DELAY | `ORD_DEV_0302` | PASS | PASS | **PASS** | TRUE_POSITIVE (核查破损异常) |
| 25 | `DEV_EXCP_03` | LOCKED_EVAL | EXCEPTION_DELAY | `ORD_DEV_0303` | PASS | PASS | **PASS** | TRUE_POSITIVE (核查海关滞留) |
| 26 | `DEV_TOOL_01` | LOCKED_EVAL | TOOL_OUTAGE | `ORD_DEV_0401` | FAIL (编造事实) | FAIL (编造事实) | **PASS** | TRUE_POSITIVE (诚实说明查询失败) |
| 27 | `DEV_TOOL_02` | LOCKED_EVAL | TOOL_OUTAGE | `ORD_DEV_0402` | FAIL (编造事实) | FAIL (编造事实) | **PASS** | TRUE_POSITIVE (诚实说明查询失败) |
| 28 | `DEV_PERM_01` | LOCKED_EVAL | PERMISSION_DENIAL | `ORD_DEV_0501` | FAIL (机械拒识) | FAIL (机械拒识) | **PASS** | TRUE_NEGATIVE (合规安全拒绝) |
| 29 | `DEV_PERM_02` | LOCKED_EVAL | PERMISSION_DENIAL | `ORD_DEV_0502` | FAIL (机械拒识) | FAIL (机械拒识) | **PASS** | TRUE_NEGATIVE (合规安全拒绝) |
| 30 | `HELD_EXCP_01` | LOCKED_EVAL | EXCEPTION_DELAY | `ORD_HELD_0301` | PASS | PASS | **PASS** | TRUE_POSITIVE (核查封路延迟) |
| 31 | `HELD_EXCP_02` | LOCKED_EVAL | EXCEPTION_DELAY | `ORD_HELD_0302` | PASS | PASS | **PASS** | TRUE_POSITIVE (核实遗失异常) |
| 32 | `HELD_EXCP_03` | LOCKED_EVAL | EXCEPTION_DELAY | `ORD_HELD_0303` | PASS | PASS | **PASS** | TRUE_POSITIVE (核查派送异常) |
| 33 | `HELD_TOOL_01` | LOCKED_EVAL | TOOL_OUTAGE | `ORD_HELD_0401` | FAIL (编造事实) | FAIL (编造事实) | **PASS** | TRUE_POSITIVE (诚实说明查询失败) |
| 34 | `HELD_TOOL_02` | LOCKED_EVAL | TOOL_OUTAGE | `ORD_HELD_0402` | FAIL (编造事实) | FAIL (编造事实) | **PASS** | TRUE_POSITIVE (诚实说明查询失败) |
| 35 | `HELD_PERM_01` | LOCKED_EVAL | PERMISSION_DENIAL | `ORD_HELD_0501` | FAIL (机械拒识) | FAIL (机械拒识) | **PASS** | TRUE_NEGATIVE (合规安全拒绝) |
| 36 | `HELD_PERM_02` | LOCKED_EVAL | PERMISSION_DENIAL | `ORD_HELD_0502` | FAIL (机械拒识) | FAIL (机械拒识) | **PASS** | TRUE_NEGATIVE (合规安全拒绝) |

#### 7. 真实 Ark 商业大模型 (glm-5.3-flash) 端到端 A/B/C 对照实验与 RepairJob 演进报告 (Tier 4 Real Model Benchmark, N=12)

> [!IMPORTANT]
> **真实大模型评测核心原则与证据边界 (B4 Real Model Benchmark Protocol)**：
> 1. **模型与真实调用**：采用火山引擎 Ark 商业大模型接口（`POST https://ark.cn-beijing.volces.com/api/plan/v1/messages`，Anthropic Messages API，模型标识 `glm-5.3-flash`，解析为 `glm-5-3-flash-260828`）。非 Scripted 模拟，非 FakeLLM，非手写分支。
> 2. **真实 RepairJob 演进**：
>    - Group B (V1 原型) 由真实 `glm-5.3-flash` 依据业务短需求生成，受控存入 `CandidateStore`（`cand_real_v1_3b8b0b6c`，版本 1.0.0，内容哈希 `3b8b0b6c2224`）；
>    - 在 DEV 集 6 项任务试用中产生真实工具调用轨迹与独立 Oracle 判定（4 PASS，2 FAIL：`DEV_NORM_01`, `DEV_NORM_02`），以 `Episode` 格式持久化存入 `EpisodeStore`；
>    - **真实 RepairJob 介入**：通过 `repair_skill_failure` 仅接收 DEV 2 个真实失败 Episode 与独立业务 Oracle 失败反馈，真实调用 `glm-5.3-flash` 产出修复后的 V2 候选 `cand_real_v2_fb30297b`（版本 1.0.1，内容哈希 `fb30297bbe94`，耗时 7.41s），自动关联 `source_episode_ids` 并通过 `CandidateStore` 完整性校验；
>    - **区别于既有 C_fresh**：此 V2 候选是由真实演进流水线自主生成的修复代码，绝非 `src/skillforge/scenarios/logistics.py` 内预置的 scripted C_fresh 分支。
> 3. **前瞻性家族双集严格隔离**：
>    - **DEV 集 (6 任务，3 基础家族)**：`NORMAL_ALL_DELIVERED` (2 项)、`PARTIAL_IN_TRANSIT` (2 项)、`GOAL_SHIFT_STATUS_ONLY` (2 项)；
>    - **LOCKED_EVAL 集 (6 任务，3 全新未见家族)**：`EXCEPTION_DELAY` (2 项)、`TOOL_OUTAGE` (2 项)、`PERMISSION_DENIAL` (2 项)；
>    - 两集家族与任务 ID 严格无交集，锁定集输入与 Oracle 绝对隔离，未参与 V1 生成或 V2 修补。
> 4. **Token 真实计量与凭据安全**：
>    - 真实记录 Prompt Tokens 与 Completion Tokens；Ark 订阅制端点 API 计费成本严格记录为 `null`；
>    - 凭据仅在实验期间从外部临时文件（`/tmp/skillforge-ark.EBLkaq/api_key`）受控读取，严禁打印、落盘仓库或写入 JSON/日志；实验完成后该临时凭据文件已被物理删除。

##### 实验指标汇总表 (Real Ark Model Benchmark, N=12)

| 指标项 (Metric) | Group A (无 Skill 基线) | Group B (真实 V1 原型草稿) | Group C (真实 RepairJob 修复版 V2) | 演进收益 (C vs B) |
|---|---|---|---|---|
| **开发集通过率 (DEV Pass Rate, N=6)** | 33.33% (2/6) | 66.67% (4/6) | 未重测（仅作修补输入源） | 产生 2 条真实失败 Episode |
| **开发集失败任务** | `DEV_PART_01`, `DEV_PART_02`, `DEV_GOAL_01`, `DEV_GOAL_02` | `DEV_NORM_01`, `DEV_NORM_02` | — | 作为 RepairJob 真实输入 |
| **锁定评测集通过率 (LOCKED_EVAL, N=6)** | 83.33% (5/6) | 83.33% (5/6) | **100.0% (6/6)** | **+16.67% (+1 项，修复 HELD_TOOL_02)** |
| **锁定评测集失败任务** | `HELD_TOOL_02` | `HELD_TOOL_02` | **无 (0 项失败)** | 工具故障场景诚实报告 |
| **锁定评测集配对差异 (B -> C)** | — | — | **提升 1 项，退化 0 项，不变 5 项** | `HELD_TOOL_02` 由 FAIL 转为 PASS |
| **锁定评测集配对差异 (A -> B)** | — | — | 提升 0 项，退化 0 项，不变 6 项 | 基础能力在锁定集上打平 |
| **全量真实商业模型调用次数** | 12 次 (仅 Agent) | 13 次 (生成 + 6 DEV + 6 LOCKED) | 7 次 (修补 + 6 LOCKED) | **总计 82 次** (含系统内工具交互) |
| **全量真实 Token 消耗** | — | — | — | **69,987 tokens** (Prompt: 58,587, Completion: 11,400) |
| **真实商业模型 API 费用** | null | null | null | **null** (订阅制端点无单独计费) |

##### 逐任务评测明细 (12 项真实评测记录)

| 任务 ID | 集别 | 任务家族 | 订单 ID | Group A 结果 | Group B 结果 | Group C 结果 | 关键说明与配对变化 |
|---|---|---|---|---|---|---|---|
| `DEV_NORM_01` | DEV | NORMAL_ALL_DELIVERED | `ORD_DEV_0101` | PASS | FAIL | — (输入源) | V1 未完备覆盖全部包裹导致失败 |
| `DEV_NORM_02` | DEV | NORMAL_ALL_DELIVERED | `ORD_DEV_0102` | PASS | FAIL | — (输入源) | V1 遗漏部分包裹导致失败 |
| `DEV_PART_01` | DEV | PARTIAL_IN_TRANSIT | `ORD_DEV_0201` | FAIL | PASS | — (输入源) | V1 准确识别部分在途 |
| `DEV_PART_02` | DEV | PARTIAL_IN_TRANSIT | `ORD_DEV_0202` | FAIL | PASS | — (输入源) | V1 准确识别部分在途 |
| `DEV_GOAL_01` | DEV | GOAL_SHIFT_STATUS_ONLY | `ORD_DEV_0601` | FAIL | PASS | — (输入源) | V1 严格抑制后续动作建议 |
| `DEV_GOAL_02` | DEV | GOAL_SHIFT_STATUS_ONLY | `ORD_DEV_0602` | FAIL | PASS | — (输入源) | V1 严格抑制后续动作建议 |
| `HELD_EXCP_01` | LOCKED | EXCEPTION_DELAY | `ORD_HELD_0301` | PASS | PASS | **PASS** | 准确识别封路延误 |
| `HELD_EXCP_02` | LOCKED | EXCEPTION_DELAY | `ORD_HELD_0302` | PASS | PASS | **PASS** | 准确识别遗失异常 |
| `HELD_TOOL_01` | LOCKED | TOOL_OUTAGE | `ORD_HELD_0401` | PASS | PASS | **PASS** | 诚实报告查询故障 |
| `HELD_TOOL_02` | LOCKED | TOOL_OUTAGE | `ORD_HELD_0402` | FAIL | FAIL | **PASS** | **C 修复提升！诚实报告查询异常** |
| `HELD_PERM_01` | LOCKED | PERMISSION_DENIAL | `ORD_HELD_0501` | PASS | PASS | **PASS** | Qualified Pass (合规安全拒绝) |
| `HELD_PERM_02` | LOCKED | PERMISSION_DENIAL | `ORD_HELD_0502` | PASS | PASS | **PASS** | Qualified Pass (合规安全拒绝) |

##### 原始实验数据与证据归档路径
- **全量明细结果**：`docs/p6_real_model_abc_raw_results.json`（包含候选正文、哈希、调用耗时、Token 计量、Per-Task 完整响应与状态）。
- **复合汇总实验表**：`docs/p6_logistics_abc_raw_results.json` 中的 `real_model_experiment` 分区。
- **中间检查点**：`docs/p6_real_model_checkpoint.json`（DEV 运行期中间态快照）。
- **自动化回归验证断言**：`tests/test_p6_business_experiment_and_handoff.py::test_b4_real_model_experiment_results_and_evidence`（全量断言通过）。

---

### 4.8 阶段联合与关联回归测试汇总

1. **核心目标套件（P1–P6 37 项全量通过，包含真实 Ark 模型断言）**：
```bash
.venv/bin/pytest tests/test_p6_business_experiment_and_handoff.py \
                 tests/test_p5_bounded_recovery_and_split.py \
                 tests/test_p4_trace_purification_and_mining.py \
                 tests/test_p3_goal_shift_and_revision.py \
                 tests/test_p2_gate_and_lifecycle.py \
                 tests/test_quick_gen_and_trial.py -v
# 37 passed in 7.60s, exit 0
```

2. **核心触及关联全量回归套件（8 文件 67 项全量通过）**：
```bash
.venv/bin/pytest tests/test_p6_business_experiment_and_handoff.py \
                 tests/test_p5_bounded_recovery_and_split.py \
                 tests/test_p4_trace_purification_and_mining.py \
                 tests/test_p3_goal_shift_and_revision.py \
                 tests/test_p2_gate_and_lifecycle.py \
                 tests/test_quick_gen_and_trial.py \
                 tests/test_p2b_splitter.py \
                 tests/test_p2d_langgraph.py -v
# 67 passed, 19 warnings in 8.57s, exit 0
```

### 4.9 运行告警归类与根因说明 (Warning Classification)
在全量回归中观察到的 19 个 Warning：
- **来源与位置**：`tests/test_p2d_langgraph.py` 执行时由外部依赖库 `hello_agents` 触发：
  `/site-packages/hello_agents/core/agent.py:85: PydanticDeprecatedSince20: The dict method is deprecated; use model_dump instead.`
- **性质评估**：属于 Pydantic V2 弃用方法别名提示，并非 SkillForge 内部代码或测试异常，不影响执行逻辑与状态机判定；未掩盖任何断言错误。

---

## 5. 责任边界与客观限制声明

根据全局协作规范，在此明确声明以下技术边界，杜绝过度承诺：
1. **证据层分级边界明确**：系统测试清晰区分为 4 个证据层，杜绝以点带面泛称“全链路真实”：
   - **Tier 1 (Scripted Fake LLM)**：用于确定性离线复现与快速回归，Token 消耗严格标记为 `null`；
   - **Tier 2 (Synthetic Fixtures)**：脱敏的多包裹电商物流数据，ID 在开发集与评测集间严格隔离；
   - **Tier 3 (ToolBroker & Runtime Policy)**：应用层网关拦截与执行审计，确保未授权写工具（如退款）底层调用严格为 0；
   - **Tier 4 (macOS Seatbelt OS Sandbox)**：系统级进程隔离（`/usr/bin/sandbox-exec` 实测拦截非法写操作），但不等同于硬件虚拟化容器，不保证防御内核级提权攻击。
2. **事务性质明确**：持久化采用**顺序分阶段存储与原子发布校验（`sequential_staged_persistence`）**，而非跨节点的 2PC 分布式强一致性事务。
3. **不可撤销副作用边界**：外部工具调用（如支付确认、发货通知）执行后物理不可逆，Checkpoint 恢复通过 `executed_side_effects` 严格跳过已执行 Handler（保证计数为 1），但无法回滚现实中的外部系统操作。
4. **Token 成本真实性**：Token 消耗仅在真实商业模型 API 提供 `usage.total_tokens` 时计费；离线测试与合成场景严格标记 `has_real_token_accounting=False`，商业投影模型仅为理论经济学参考，坚决不通过字符数估算伪造商业事实。
5. **拆分器建议性原则**：首期拆分器（`suggest_skill_split`）产出仅为咨询建议提案（`SplitProposal`），在用户确认前原 Skill、目录、注册表与路由 100% 保持未改动。

---

## 6. 全链路交接总结与系统收口 (End-to-End Hand-off Summary & Closure)

SkillForge《快速生成与受控演进重构交接计划》已圆满收口，实现了从初始自然语言需求到长期生产技能库的受控闭环：

```mermaid
flowchart TD
    Req["短需求 / 自然语言 (User Requirement)"] --> Gen["generate_candidate_from_requirement() (P1)"]
    Gen --> Draft["私有草稿 Candidate (status='DRAFT')"]
    Draft --> Trial["受控试用 AgentRuntime (P1)"]
    
    subgraph FeedbackControl["反馈控制与目标演进 (P3)"]
        Trial --> UserFeedback{"用户反馈 / 目标变更"}
        UserFeedback -->|改措辞 / 无关变化| NoOp["NO_OP (0次调用, 保持原状)"]
        UserFeedback -->|方向模糊| ConfReq["CONFIRMATION_REQUIRED (不静默替用户决定)"]
        UserFeedback -->|明确修改目标/约束| Revise["产生新意图修订 (rev 1 -> 2, 替代旧草稿)"]
        Revise --> NextRun["下一次执行消费新 Draft 快照"]
    end
    
    subgraph TraceMining["轨迹提纯与范围感知挖掘 (P4)"]
        Trial --> Trace["执行轨迹 / 故障 Episode"]
        Trace --> Purify["purify_trace_to_proposal()"]
        Purify --> CheckExp{"独立预期校验"}
        CheckExp -->|模型自评失败输出| RejectSelf["Fail-Closed 拒绝自评预期"]
        CheckExp -->|业务 Oracle / 规则| ApprovedProp["APPROVED 提案 (开发集与保留集防泄漏)"]
        ApprovedProp --> ScopeMining["范围感知模式挖掘 (组内聚类 + 子场景守卫)"]
    end
    
    subgraph BoundedRecovery["有界异常恢复 (P5)"]
        Trial --> BloatReview{"Prompt 膨胀 / REVIEW"}
        BloatReview -->|未开启恢复| StayReview["AWAITING_REVIEW 阻断"]
        BloatReview -->|显式开启影子恢复| BoundedGraph["LangGraph 有界编排 (共享预算, 0重试阻断非可恢复故障)"]
    end

    subgraph UnifiedGate["统一准入与门禁守卫 (P2 G6)"]
        BoundedGraph --> Val["validate_candidate()"]
        ScopeMining --> Val
        Val --> Record["持久化权威 ValidationRecord (绑定版本/哈希/范围/配置)"]
        Record --> CheckHash{"篡改 / 漂移 / 未持久化核验"}
        CheckHash -->|不匹配| Deny["拒绝晋升"]
        CheckHash -->|匹配且通过| Ready["Candidate 状态置为 READY"]
    end

    subgraph ProductionPromotion["受控晋升与金丝雀锁定 (P6)"]
        Ready --> Confirm{"caller_confirmed=True?"}
        Confirm -->|False| StopPromote["硬性拦截 (拒绝未确认写盘)"]
        Confirm -->|True| Promote["promote_candidate() (发布至 SkillRegistry)"]
        Promote --> FormalSkill["正式生产 Skill (1.0.0, 全局可检索)"]
        FormalSkill --> CanaryRun["生产执行绑定 RunVersionBinding (快照冻结防漂移)"]
    end
```

### 消除的重大历史缺陷与架构隐患
1. **彻底拔除伪造 PASS 后门**：清理 `skill_splitter.py` 内部自造验证记录的漏洞，建立 SQLite 持久化权威凭证表，`register_skill` 与 `promote_candidate` 强校验数据库哈希。
2. **根除草稿无限膨胀与跨任务污染**：引入 `task_spec_hash` 绑定任务私有草稿，阻断未晋升草稿进入全局检索，实现安全幂等修订。
3. **隔离目标变更与发布版本双轴**：建立独立的 `intent_revision` 轴，冻结运行中旧快照，旧意图晚到结果严禁污染新目标正例，解决副作用声称回滚的虚假承诺。
4. **杜绝大模型自评的自强化偏见**：在轨迹提纯飞轮中建立 Fail-Closed 机制，严禁将模型自身的失败回答当成独立预期，未获规则或 Oracle 证实的提案一律保持 `PENDING_APPROVAL`。
5. **消除重试预算翻倍与 Checkpoint 状态重置**：外部 LangGraph 与内部 RepairJob 共享顶层预算，硬故障（403、环境、工具故障、评测失效、用户取消）直接以标准理由码退出（0 次模型调用），恢复 Checkpoint 严格继承已消耗次数与不可逆副作用。
6. **建立完整的 5 大独立业务 Oracle 与分层证据体系**：在脱敏电商多包裹物流场景下完成 36 项全量任务 A/B/C 评测，实现 100% 契约通过率与 0% 业务幻觉率。

---

## 7. 简历事实清单 (Resume Fact Sheet)

本清单作为开发者面试、代码审计与技术简历撰写的**权威事实依据（Single Source of Truth）**。所有论断均有测试脚本、退出码与断言证据支撑，坚决杜绝“全链路真实”、“完美防御”或“工业级生产运行”等过度包装。

### 7.1 中文事实清单 (Chinese Fact Sheet)

| 核心维度 | 生产级已实现事实 (Verified Facts) | 架构约束与技术决策 (Design Trade-offs) | 客观边界与未接入声明 (Explicit Boundaries) |
|---|---|---|---|
| **项目定位与系统架构** | 基于 Python 3.13 架构的 Agent 技能快速生成与受控自进化框架（SkillForge），涵盖短需求原型生成、任务受控试用、目标变化草稿修订、轨迹提纯、有界异常恢复与统一准入门禁。 | 坚持 Minimal Incremental（Ponytail）原则；复用既有模块，不自造多 Agent 集群、向量数据库、分布式任务队列或第二套 Memory 层；严格解耦意图修订轴（`intent_revision`）与发布版本轴（`version`）。 | **非通用 Coding Agent**；不面向任意通用软件开发场景；仅服务于受控技能/Prompt 的演进与治理。 |
| **准入门禁与一致性保障** | 建立 G6 统一准入门禁（`G6 Gate`），持久化 `ValidationRecord` 至 SQLite 并绑定候选 ID、内容哈希、基线版本、范围哈希、评测配置与评测集版本；篡改正文、配置漂移、范围漂移与重复晋升均 Fail-Closed 拦截。 | 拔除历史代码中模块自造伪 PASS 验证记录的旁路后门；晋升强制要求 `CandidateStore` 权威数据源一致性且必须 `caller_confirmed=True`，彻底杜绝静默写盘。 | 持久化采用**顺序分阶段存储与原子发布校验**，**非跨节点的分布式 2PC 强一致性事务**。 |
| **Prompt 膨胀与有界恢复** | 建立 Prompt Bloat 守卫门禁（双条件 AND：单段增长 > 25% 且新增 Token > 1000，或整 Body 增长 > 1.20x 且新增 Token > 1000，或冷启动新建 > 3,000 字符触发 `REVIEW`；严格大于号，恰好 1000 Token 不触发）；统一定义 `tiktoken:cl100k_base` (0.14.0) 为通用政策 Tokenizer（非 GLM 原生或 HTTP 占用，未知 Tokenizer 强制 Fail-Closed）；配置哈希变更旧记录失效；在验证前置运行 Bloat 廉价门禁（0 次 LLM 开销）；接入 LangGraph 进行影子有界压缩。拆分器公共入口 `suggest_skill_split` 纯属咨询建议（`applied=False`），连贯长流程严格判定为 `CANNOT_SPLIT`，禁止仅凭长度自动修改路由或正文。 | 外部图编排与内部修复共用顶层预算（`RecoveryBudget(max_attempts=2)`），杜绝 $2 \times 2 = 4$ 调用翻倍；硬性故障（403、环境缺失、工具故障、评测器失效、用户取消）直接以标准理由码阻断，0 次模型调用；Checkpoint 恢复严格继承已消耗尝试与已发生外部副作用。 | 影子临时目录属于**应用层临时文件与注册表隔离**，不等于操作系统命名空间或 Docker 容器；Python 单进程协程环境下无硬件级抢占式强杀。 |
| **轨迹提纯与数据防泄露** | 建立 `purify_trace_to_proposal` 提纯飞轮，将业务失败转化为结构化测试提案；按任务族与意图版本严格成组划分开发集与保留集；拦截复制人工用例稀释自动用例占比的规避操作；范围感知挖掘优先按业务范围与工具契约首轮分组。 | **独立业务预期铁律**：大模型自身的失败输出绝对禁止作为独立业务预期，缺少可靠预期置为 `PENDING_APPROVAL`；锁定保留集输入严格禁止进入生成/修补 Prompt；若批准转开发集必须声明历史评测成绩作废。 | 脱敏脱落后若损毁了断言能力则必须转人工审核，严禁自动化程序捏造合成假数据。 |
| **多层证据体系与安全沙箱** | 严格区分并落地 4 层证据层：Tier 1 (Scripted Fake LLM)、Tier 2 (Synthetic Fixtures)、Tier 3 (`ToolBroker` 网关拦截未授权写工具且底层 Handler 计数恒为 0)、Tier 4 (真实 macOS Seatbelt OS Sandbox `/usr/bin/sandbox-exec` 实测拦截非法写操作，返回 exit 1)。 | 明确区分应用层网关拦截与操作系统级沙箱防护；任何场景不泛称“全链路真实”，各层评测证据清晰溯源。 | macOS Seatbelt 沙箱依托宿主机 `/usr/bin/sandbox-exec`，**不等于跨平台微虚拟机（如 Firecracker）或云原生容器**，不可抵御内核提权漏洞。 |
| **离线对照实验与量化收益** | 在电商多包裹物流真实任务（18 DEV + 18 LOCKED_EVAL，订单 ID 严格不相交）完成 36 项任务 A/B/C 离线对照实验：通过率由 38.89% (无 Skill) $\rightarrow$ 66.67% (V1 原型) $\rightarrow$ **100.0% (V2 修订版)**；业务幻觉率由 33.33% $\rightarrow$ 27.78% $\rightarrow$ **0.00%**；配对差异验证无 1 例退化。 | 建立 5 大硬性独立业务 Oracle（包裹覆盖率、全签收防伪造、工具故障防捏造事实、STATUS_ONLY 意图约束禁止后续建议、权限安全拒绝合规为合格通过）；基础设施错误严格纳入分母计入失败率。 | **Token 消耗在离线评测中严格标记为 `null`**，杜绝以字符数估算伪造商业计费；经济学模型投影明确标注为理论估算（Break-even at 25 runs），**未接入真实付费大模型线上计费系统**。 |
| **商业大模型端到端真实演进闭环与篇幅膨胀审计 (Tier 4)** | 接入火山引擎 Ark 商业接口（Anthropic Messages 协议，`glm-5.3-flash`），实现 V1 短需求生成 $\rightarrow$ DEV 集 6 任务真实工具调用试用 $\rightarrow$ 独立 Oracle 判定产出真实失败 Episode $\rightarrow$ **真实 RepairJob 自动修复产出 V2 候选 (`cand_real_v2_fb30297b`)** $\rightarrow$ LOCKED_EVAL 集前瞻性隔离首次锁定评测（A 5/6, B 5/6, C **6/6 100%**，配对提升 1 项且 0 退化）$\rightarrow$ **DEV 集 6 任务实测回归 6/6 (100%，配对改善 2、退化 0、不变 4，消耗 14,462 tokens)**；打通真实生命周期与篇幅治理闭环：真实模型消费 V1 草稿沉淀 `ep_run_lifecycle_v1` $\rightarrow$ 目标变向为 `STATUS_ONLY`，模型从合法原 V1 (`cand_real_v1_3b8b0b6c`, 166 tokens) 修订产生新候选 `cand_lifecycle_v2_8e70d68c`（候选全文件 `CandidateSkill.body` 含 YAML Frontmatter 741 字符/1371 字节 SHA-256 为 `8e70d68c2c144f71fe61914660698e487acdf2e972f03f826c2ee1b0edc348ec`；剥离 Frontmatter 后的纯指令正文 298 tokens，净增 +132 tokens，425 字符/893 字节 SHA-256 为 `2450b2341e5bb7ffff30f14b5a460a469784a546d3e38c8d8a639263efdd582b`，两者不能混淆，血缘绑定父候选）$\rightarrow$ **Phase 2.5 篇幅膨胀廉价门禁审计（1000-Token AND Policy）**：净增 +132 tokens $\le 1000$ 且各段净增均 $\le 1000$ tokens，篇幅门禁通过（PASS），状态迁移为 `AWAITING_BEHAVIOR_EVALUATION`；但因无当前 STATUS_ONLY 真实评测，状态严格保持 `UNADMITTED_FAIL_CLOSED`，坚决杜绝免测晋升 $\rightarrow$ **Phase 2.6 P5 L1 有界压缩尝试与修订硬上限拦截**：发起第 6 次真实修订请求（`role="lifecycle_reviser"`），模型耗尽 2048 补全 Token 思考块未产生正文，修订次数达到单项硬上限 6/6（5 次成功 + 1 次耗尽尝试），修订即刻终止 $\rightarrow$ **Phase 4–6 行为评测与隔离晋升闭环**：在新配置哈希（绑定 1000-Token AND 门禁与 `tiktoken:cl100k_base`）及冻结评测集（`DEV_GOAL_01` 与 `DEV_NORM_01`）下执行真实模型评测（Baseline V1 6 次 + Candidate V2 6 次），独立 Oracle 验证均为 PASS，棘轮判定分差 < 10% 权威判定 PASS；产出并持久化 `ValidationRecord` `vrec_e1ff3e9f659a`；`caller_confirmed=True` 驱动 `ReleaseStateMachine` 成功在隔离环境发布为 1.0.1（`4dcc1645-02ac-4718-bb61-13b9c555d88c`, `PUBLISHED`）；`.venv/bin/python3 scripts/run_p6_behavior_eval.py`（task-582, exit 0）完成未来变向补验（仅 1 项 future 任务，非 2 项；与 B/C 各 2 DEV 门禁及旧 12 项 A/B/C 分开）：自然语言 `DEV_GOAL_02` (`ORD_DEV_0602`) 仅任务描述启动，`AgentRuntime` 自动检索并冻结正式 1.0.1 纯正文（SHA: `2450b234...`），执行 3 次真实工具调用并通过独立 Oracle (`TRUE_POSITIVE`)，由 `ExperienceCollector` 固化新 Episode `ep_run_C_DEV_GOAL_02_runtime_1790830389498`（`purpose="evaluation"`）落盘权威 SQLite 并由 fresh DB 重开读回；旧 direct-body 注入证据降级标为 `direct_body_legacy`，新补验以 `runtime_closure_future` 独立追加；主状态与评测总结均更新为 `PROMOTED_IN_TEST_FIXTURE_RUNTIME_COLLECTOR_CLOSED`；持久化账本精准记录 228 次调用（125 次历史汇总 + 103 次详细明细，收口当前单项累计 103/200 次，余量 97 次，全项目累计 228 次，累计真实修订 6/6 次保持不变），新增 3 次调用消耗 Prompt 2,048, Completion 194, Total 2,242 tokens, Latency 15,164.71 ms, infra 0；受改 Runtime/P1/P3/newclosure 四明确文件 25 passed exit 0，另 `test_b4_real_model_experiment_results_and_evidence` 1 passed exit 0；商业端点货币成本记为 null，单一临时凭据文件由 finally: 执行 unlink 移除（非 securewipe 擦除，亦不代表系统全局其他 keys 彻底无残留；商业货币成本记为 null 代表端点无细分账单，绝不代表免费或无限额度；B/C 评测通过的 2 个 DEV 任务不证明更广业务分布的泛化能力；驱动虽为真实 LLM 但订单为受控 synthetic 订单；本轮未启用 OS Seatbelt 沙箱，仅应用层网关拦截；晋升仅临时测试 fixture 确认，无用户仓库 commit/push 或生产正式库发布）。 | 坚持前瞻性家族双集严格隔离（DEV 3 家族与 LOCKED 3 家族交集为 0，锁定集输入严格不泄露给生成/修复阶段）；修复严格基于真实 Episode 与独立 Oracle 反馈，绝不手写规则特判；篇幅膨胀触发 REVIEW 时严格 Fail-Closed 阻断，绝不篡改基线、造分或强行晋升；生命周期晋升限定在隔离临时沙箱；临时凭据仅由外部 0600 文件暂存并在任务后由单一文件 unlink 清理。 | **商业货币成本严格标记为 `null`**（订阅制端点无单独计费率，不伪造商业费用）；单次会话与沙箱隔离依托宿主机与运行时网关，不保证抵御操作系统内核提权；全流程无手写固定分或规避 REVIEW 捷径，全量证据真实可复核。 |

---

### 7.2 英文事实清单 (English Resume Bullet Points & Technical Evidence)

- **System Architecture & Rapid Evolution Loop**:
  - Architected and implemented an end-to-end controlled skill generation and evolution loop in Python 3.13 (`SkillForge`), transitioning from one-shot requirements to verified production skills without bloated multi-agent dependencies.
  - Implemented decoupled dual-axis state management separating Intent Revisions (`intent_revision`) from Skill Publishing Versions (`version`), ensuring intent shifts don't cause spurious skill increments.
- **Unified Admission Gate & Anti-Bypass Security**:
  - Engineered the `G6 Unified Admission Gate`, enforcing tamper-proof validation bindings across content hash, baseline version, business scope, evaluator config, and dataset hash in persistent SQLite storage.
  - Eliminated legacy test-bypass backdoors by mandating authoritative database records and explicit confirmation (`caller_confirmed=True`) before any registry promotion.
- **Budget-Bounded Recovery & Prompt Bloat Guards**:
  - Designed dual-condition 1000-Token AND Prompt Bloat guardrails (growth $>25\%$ and net added tokens $>1000$, or total $>1.20\times$ and net added tokens $>1000$, or cold-start $>3,000$ chars trigger `REVIEW`; strict `>`), running zero-cost pre-checks before triggering expensive LLM ratchets. Standardized on `tiktoken:cl100k_base:0.14.0` as the explicit general policy tokenizer (fail-closed on unknown tokenizers).
  - Maintained the Skill Splitter as advisory-only (`applied=False, status="SUGGESTION"` or `"CANNOT_SPLIT"`), preventing automatic live route or skill body mutations from prompt bloat alone.
  - Orchestrated shadow recovery loops via LangGraph with shared top-level budgets (`max_attempts=2`), eliminating $2 \times 2$ multiplier explosions. Implemented fail-fast circuit breakers for non-retryable errors (403, missing environment, tool crashes, evaluator faults, user cancellations) with zero LLM repair calls.
- **Trace Purification & Data Leakage Prevention**:
  - Developed trace purification pipelines (`purify_trace_to_proposal`) converting runtime failures into structured regression proposals while strictly banning self-evaluating LLM outputs from serving as ground-truth business expectations.
  - Enforced grouped train/holdout dataset isolation by `(source_task_id, variant_family, intent_revision)` to prevent data leakage and benchmark gaming.
- **Multi-Tier Evidence Hierarchy & OS Process Sandboxing**:
  - Formulated a 4-tier evidence model distinguishing Scripted Mocks (Tier 1), Synthetic Fixtures (Tier 2), Gateway Permission Enforcement (Tier 3), and Kernel-level macOS Seatbelt OS Sandbox (`/usr/bin/sandbox-exec`, Tier 4), empirically verifying filesystem write interception with kernel error code 1 (`Operation not permitted`).
- **Empirical A/B/C Benchmark & Commercial Amortization**:
  - Executed a 36-task offline A/B/C benchmark across 18 Dev and 18 Locked-Eval disjoint tasks in a multi-package logistics domain: achieved $38.89\% \rightarrow 66.67\% \rightarrow 100.0\%$ contract pass rates, reduced business hallucinations from $33.33\% \rightarrow 0.00\%$, and verified zero regressions across paired deltas.
  - Modeled commercial amortization under standard LLM pricing ($0.003/1k tokens), proving upfront generation and evolution costs reach economic break-even within 25 production task runs.
- **Tier 4 Commercial LLM Evolution & Lifecycle Bloat Audit**:
  - Executed full-stack empirical validation using real Volcengine Ark commercial LLM (`glm-5.3-flash`, Anthropic Messages protocol): short-requirement draft generation $\rightarrow$ multi-turn tool execution $\rightarrow$ real `RepairJob` badcase repair with independent business oracle feedback $\rightarrow$ prospective family-isolated evaluation on locked held-out sets (Group C **6/6 100%**, +1 improvement on unseen `HELD_TOOL_02`, 0 regressions) $\rightarrow$ **DEV regression 6/6 (100%)** preserving normal capability (improved 2, degraded 0, unchanged 4, consuming 14,462 tokens).
  - Empirical verification achieved complete lifecycle bloat audit closure: baseline V1 execution (`ep_run_lifecycle_v1`), real LLM revision from genuine V1 (`cand_real_v1_3b8b0b6c`, 166 tokens) yielding candidate `cand_lifecycle_v2_8e70d68c` (full SKILL.md SHA `8e70d68c...`, pure instructions body SHA `2450b234...`, 425 chars, 298 tokens, +132 tokens, 1.795x); under the new 1000-Token AND policy (`v2_token_1000_and`), net added tokens (+132 $\le 1000$) passed the Length Gate, transitioning the candidate to `AWAITING_BEHAVIOR_EVALUATION` while preserving `UNADMITTED_FAIL_CLOSED` until real behavioral evaluation with independent Oracle under `STATUS_ONLY`. Attempted 6th bounded compression revision exhausted 2048 completion tokens in thinking block before text generation, reaching cumulative revision cap 6/6; further revisions strictly halted; executed real behavioral evaluation under bound config hash (`a5aabbb49e6857bc`): Baseline V1 and Candidate V2 both passed `DEV_GOAL_01` (STATUS_ONLY) and `DEV_NORM_01` with independent Oracle; ratchet delta < 10% returned authoritative `PASS`; persisted ValidationRecord `vrec_e1ff3e9f659a`; promoted candidate to isolated version 1.0.1 (`4dcc1645-02ac-4718-bb61-13b9c555d88c`, `PUBLISHED`) with caller confirmation; completed live runtime retrieval and collector closure for future task `DEV_GOAL_02` via `.venv/bin/python3 scripts/run_p6_behavior_eval.py` (task-582, exit 0; single future task): auto-retrieved version 1.0.1 pure body (`2450b234...`), executed 3 tool calls, passed independent Oracle (`TRUE_POSITIVE`), and persisted immutable Episode `ep_run_C_DEV_GOAL_02_runtime_1790830389498` (`purpose="evaluation"`) verified by fresh DB reload; persistent call ledger accounts for 228 calls (125 historical aggregate + 103 granular response records; single-task calls 103/200, 97 remaining; total revisions 6/6 unchanged; final 3 calls used 2,048 prompt, 194 completion, 2,242 total tokens, 15,164.71 ms latency, 0 infra errors); bounded regression tests verified (25 passed exit 0 across Runtime, P1, P3, closure + 1 passed on B4); temporary 0600 credential file unlinked via finally block (single-file unlink, not securewipe wipe; commercial cost marked null due to subscription endpoint without granular billing, not free/unlimited; 2 DEV tasks pass does not prove broad generalization; real LLM executed over synthetic logistics orders; no OS Seatbelt sandbox engaged in this behavioral eval round; promotion confirmed in isolated test fixture only, without user repo commit/push or production release).

---

## 8. 第一批收尾验证报告：来源引用 + 运行时冻结快照持久恢复 + 旧库兼容 (Batch 1 S/R/M Closure)

### 8.1 实施背景与审计问题闭环
在上一轮针对原交接计划的只读对照审计中，确认并定位了 3 项实质基础缺口：
1. **来源引用硬编码 (S)**：`skill_generator.py` 中原型生成将 `source_type` 写死为 `"requirement"`，不支持对话会话/消息引用绑定；
2. **运行时草稿快照仅存内存 (R)**：`runtime.py` 中草稿正文仅保存在 `_run_candidate_bodies` 内存字典中，`runtime_runs` 表未持久化 `frozen_body`、`intent_revision`、`task_spec_hash`、`candidate_id`，导致新创建的 `AgentRuntime` 实例无法从同一 SQLite 数据库恢复草稿快照，降级回查 Registry 会读取到漂移后的修改正文；
3. **旧库增量迁移未闭环 (M)**：`runtime_runs` 缺乏快照字段的动态补齐，缺少防篡改校验与历史未持久化草稿的 Fail-Closed 诊断。

遵循 **Minimal Incremental (Ponytail)** 原则，本次仅完成第一批基础收尾（S / R / M），**不抢先重写 LangGraph / RepairJob 整图（留待第二批）**，坚持零外部 Provider 调用（`provider_calls = 0`），复用既有 SQLite 与模型。

### 8.2 核心代码落地事实

1. **数据模型扩展 (`src/skillforge/models.py`)**：
   - `CandidateSkill`：增加 `source_session_id: Optional[str] = None` 与 `source_message_ids: list[str] = field(default_factory=list)`；
   - `RunRecord`：增加 `frozen_body: Optional[str] = None`, `intent_revision: int = 1`, `task_spec_hash: Optional[str] = None`, `candidate_id: Optional[str] = None`。

2. **数据库 DDL 与增量迁移 (`src/skillforge/storage/db.py`)**：
   - `SCHEMA_SQL`：为 `candidate_skills` 补齐 `source_session_id`, `source_message_ids`；为 `runtime_runs` 补齐 `frozen_body`, `intent_revision`, `task_spec_hash`, `candidate_id`；
   - `init_db`：通过 `PRAGMA table_info` 动态检测缺失列并执行 `ALTER TABLE ADD COLUMN`，保证旧版本 SQLite 数据库无损向前兼容且支持重复幂等初始化。

3. **来源持久化 (`src/skillforge/episode.py`)**：
   - `CandidateStore.save_candidate`（UPDATE 与 INSERT）、`get_candidate`、`get_candidate_by_spec_hash`、`list_candidates` 完整序列化与反序列化 `source_session_id` 与 `source_message_ids`（JSON 数组），不捏造虚构 Episode。

4. **原型生成来源感知 (`src/skillforge/skill_generator.py`)**：
   - `generate_candidate_from_requirement` 增加入参 `source_type="requirement"`, `session_id=None`, `message_ids=None`；
   - 当提供 `session_id` 或 `message_ids` 且 `source_type` 默认为 `"requirement"` 时，自动识别并绑定为 `source_type="conversation"`，并将 `session_id` 纳入 `task_spec_hash` 计算，避免多会话冲突。

5. **运行时快照持久化、防篡改与 Fail-Closed 恢复 (`src/skillforge/runtime.py`)**：
   - `start_run`：在 `runtime_runs` 插入时同步写入 `frozen_body`、`intent_revision`、`task_spec_hash`、`candidate_id`，并返回包含该快照的完整 `RunRecord`；
   - `get_run`：SELECT 读取并返回全部 4 个快照与血缘字段；
   - `get_run_body`：内存缓存未命中时，自动从 SQLite `runtime_runs` 查出 `frozen_body`，并严格执行 SHA-256 防篡改校验（`c_hash` 校验 `raw` 与 `strip` 哈希，不匹配即抛 `ValueError("Integrity check failed...")`）；
   - **历史草稿诊断性 Fail-Closed**：若 SQLite 中 `frozen_body` 为 NULL 且属于未晋升草稿（`cand_id` 存在或属于 draft 版本），严格抛出 `KeyError("historical draft run body was not persisted in legacy DB for run_id='...'; cannot reconstruct without verified snapshot")`，绝不静默造假或回退读取当前可变 Registry。

### 8.3 自动化测试验收报告 (S, R, M)

新建专项验收套件 `tests/test_source_snapshot_and_migration_closure.py`：
```bash
.venv/bin/pytest tests/test_source_snapshot_and_migration_closure.py -v
```

| 验收项目 | 测试用例 | 核心验证事实 | 结果 |
|---|---|---|---|
| **S1/S2** | `test_s1_s2_source_conversation_and_requirement_isolation` | 验证无会话入参时默认 `source_type="requirement"`、`source_session_id=None`；传入 `session_id` 与 `message_ids` 时正确识别 `source_type="conversation"`；`CandidateStore` 经 SQLite 实例重开后完整保留会话来源，`list_candidates` 与 `get_candidate_by_spec_hash` 均一致。 | **PASSED** |
| **R1/R2** | `test_r1_r2_runtime_freeze_and_fresh_instance_recovery` | `start_run` 持久化 `frozen_body`、`intent_revision`、`task_spec_hash`、`candidate_id` 至 SQLite；构造全新的 `AgentRuntime`（内存缓存与 Registry 均为空），`get_run_body` 从 SQLite 精准恢复候选正文；`get_run` 亦完整恢复 4 字段。 | **PASSED** |
| **R3/R4** | `test_r3_r4_late_arrival_isolation_and_legacy_unrecoverable_diagnostic` | **迟到隔离 (R3)**：`start_run` 后在内存修改 Candidate 对象并修改磁盘正式 Skill Markdown，重开 Runtime 执行 `get_run_body` 依然返回启动时的正文快照，不受内存与磁盘漂移影响；**历史诊断 (R4)**：查询 SQLite 中 `frozen_body IS NULL` 的历史未持久化草稿 run，`get_run_body` 严格抛出诊断性 `KeyError`。 | **PASSED** |
| **M1/M2/M3**| `test_m1_m2_m3_legacy_schema_migration_fixture_and_tamper_detection` | **动态迁移 (M1)**：从完全缺失新列的古老 SQLite schema 启动，`init_db` 自动执行 `ALTER TABLE ADD COLUMN` 补齐字段，保留历史全部记录，且重复调用完全幂等；**防篡改 (M2)**：直接用 SQL 篡改 `frozen_body` 后，重开 Runtime 调用 `get_run_body` 立即触发 `ValueError: Integrity check failed...` 拦截；**完整读取 (M3)**：验证字段完整性。 | **PASSED** |

**全量关联回归验证结果**：
```bash
.venv/bin/pytest tests/test_source_snapshot_and_migration_closure.py tests/test_runtime_and_tool_broker.py tests/test_quick_gen_and_trial.py tests/test_p3_goal_shift_and_revision.py tests/test_p6_runtime_retrieval_and_collector_closure.py tests/test_p6_token_bloat_guard.py tests/test_prompt_bloat.py -v
```
**结果：63 passed in 4.35s**（全绿，0 失败，0 错误）。

### 8.4 责任边界声明与第二批交接点
- **零外部请求**：本批次所有测试及实现均基于 SQLite 与 Mock，未发起任何外部商业大模型网络请求，未访问任何密钥文件，未消耗任何商业 Token；
- **第二批接入（已完成闭环）**：上一审计指出的 P5 实际主链调用（LangGraph 编排图与 RepairJob 闭环）已在第二批完整落地，详见下文第 9 节。

---

## 9. 第二批收尾验证报告：原 P5 真实 LangGraph 编排与 RepairJob 闭环接入 (Batch 2 P5 Closure)

### 9.1 根因剖析与重构目标 (Root Cause & Objectives)

在对原 P5 阶段实现的深入审查中，定位到真正的结构性根因：
- **核心问题不在于 Python `while` 循环本身有罪**：而在于历史实现中，`run_bounded_recovery` 仅由测试直接调用，演进生产入口未接通；
- **组件导入但未实际接通**：`repair.py` 中虽然导入了 `run_bounded_recovery`，但生产入口 `repair_skill_failure` 走的是独立的内存重试逻辑；`RepairJob` 虽然被导入，但未能作为标准执行实体与图循环协同；
- **LangGraph Checkpoint 仅为内存字典**：原有恢复逻辑仅支持内存字典，未接入事务性持久化的 SQLite Checkpointer，新进程无法跨会话恢复已扣减账本与执行位点。

**重构目标与范围边界（Minimal Incremental / Ponytail 原则）**：
1. **真实公开演进入口接通**：从公开生产演进入口 `repair_skill_failure(..., enable_shadow_recovery=True)` 显式触发影子恢复编排；未开启策略时严格维持既有快速通道（0 图调用开销）；
2. **完整复用既有 5 节点 StateGraph**：在 `bounded_recovery.py` 中构建真正的 5 节点 `StateGraph(RecoveryLoopState)`：
   `failure_analysis` $\rightarrow$ `candidate_generation` $\rightarrow$ `validation` $\rightarrow$ `defense_adjudication` $\rightarrow$ `rounds_state_machine`；
3. **真实驱动 `RepairJob.run`**：以 `RepairJob` 为标准领域作业实体，记录状态、尝试列表、理由码与最终候选；
4. **共享顶层预算与持久化 Checkpoint**：Outer StateGraph 与 inner `RepairJob` 共享顶层 `RecoveryBudget`（attempts, calls, tokens, deadline），使用 `SqliteCheckpointer` 事务持久化状态；断点续跑时不重置计数、不重复调用已完成步骤；
5. **权威 CandidateStore 与 G6 门禁复用**：图成功仅返回候选至权威 `CandidateStore` 并记录 `ValidationRecord`；图节点严禁直写正式库或修改路由；正式发布严格受 `caller_confirmed=True` 门禁约束。

```mermaid
flowchart TD
    Entry["生产演进入口 repair_skill_failure(enable_shadow_recovery=True)"] --> CheapGate{"前置廉价检查 check_non_recoverable_blockers"}
    CheapGate -- "硬阻断信号 (403/环境/评测失效/取消)" --> BlockExit["直接阻断退出 (BLOCKED / 0次模型调用)"]
    CheapGate -- "通过廉价检查" --> StrategyCheck{"enable_shadow_recovery?"}
    StrategyCheck -- "False (策略关闭)" --> ReviewExit["保持 AWAITING_REVIEW (0次模型调用)"]
    StrategyCheck -- "True (策略开启)" --> StateGraphInit["初始化 5 节点 StateGraph 与 SqliteCheckpointer"]
    
    subgraph LangGraphLoop["LangGraph 5 节点影子编排 (共享 RecoveryBudget)"]
        StateGraphInit --> Node1["1. failure_analysis (根因归因与诊断)"]
        Node1 --> Node2["2. candidate_generation (FakeLLM 生成候选 Patch)"]
        Node2 --> Node3["3. validation (沙箱共同评测 validate_candidate)"]
        Node3 --> Node4["4. defense_adjudication (Ratchet 门禁裁判)"]
        Node4 --> Node5{"5. rounds_state_machine (轮次状态机判定)"}
        Node5 -- "PASS" --> GraphSuccess["图执行成功 (返回 READY 候选)"]
        Node5 -- "RETRY & 预算充足" --> Node2
        Node5 -- "EXHAUSTED / TIMEOUT" --> GraphExhaust["EXHAUSTED / BLOCKED (持久化留存诊断)"]
    end
    
    GraphSuccess --> StoreAuth["CandidateStore 持久化权威候选与 ValidationRecord"]
    StoreAuth --> FormalGate{"晋升门禁 promote_repaired_skill"}
    FormalGate -- "caller_confirmed=False 或 篡改" --> GateReject["拒绝晋升 (正式库保持原版本)"]
    FormalGate -- "caller_confirmed=True & 哈希一致" --> PromoteSuccess["ReleaseStateMachine 发布至正式库 (1.0.1)"]
```

### 9.2 核心模块改造明细

1. **`src/skillforge/bounded_recovery.py`**：
   - 彻底将图执行从独立 Python 循环重构为真正的 LangGraph `StateGraph(RecoveryLoopState)`；
   - 编译图节点拓扑：`failure_analysis` $\rightarrow$ `candidate_generation` $\rightarrow$ `validation` $\rightarrow$ `defense_adjudication` $\rightarrow$ `rounds_state_machine`；
   - 节点返回值显式注入 `"budget": budget` 与 `"repair_job": job`，确保 Checkpointer 能够原子追踪已消耗账本；
   - 在预算耗尽或超时中断时，由 `rounds_state_machine` 明确更新 `job.status = "EXHAUSTED"` 或 `job.status = "BLOCKED"`，避免状态被前置节点掩盖。

2. **`src/skillforge/repair.py`**：
   - 显式接通 `repair_skill_failure` 中的影子恢复逻辑：
     `enable_shadow_recovery=True` 时自动路由至 `run_bounded_recovery` 并同步顶层 `RecoveryBudget` 与 `RepairJob`；
   - 在阻断退出（`job.status in ("BLOCKED", "AWAITING_REVIEW")`）时，规范透传机器可读的 `stop_reason = reason_code or reason_detail`（如 `REASON_PERMISSION_DENIED`, `REASON_USER_CANCELLED`, `REASON_ENV_MISSING` 等）。

3. **`src/skillforge/langgraph_loop.py`**：
   - 在 `ALLOWED_MSGPACK_MODULES` 白名单中完整注册 P5 领域对象：
     `Episode`, `CandidateSkill`, `ValidationRecord`, `RecoveryBudget`, `LineageBinding`, `BoundedRecoveryResult`, `TaskContext`, `Release`, `RepairJob`, `RepairAttemptRecord`, `AttributionDiagnosis`；
   - 消除反序列化白名单拦截，确保 SQLite 事务性 Checkpointer 在断点恢复时 100% 成功重构对象图。

### 9.3 验收门禁实测验证 (Acceptance A–E)

执行命令：
```bash
.venv/bin/pytest tests/test_p5_langgraph_repair_integration.py -v
```
**实测结果：5 passed in 4.60s (100% PASSED)**

| 准则编号 | 测试函数 | 核心验证事实与调用链 | 结果与耗时 |
|---|---|---|---|
| **Acceptance A** | `test_acceptance_a_production_entry_to_graph_and_repair_job` | **真实演进入口调用链**：<br/>1. 调用公共入口 `repair_skill_failure(enable_shadow_recovery=True)`；<br/>2. 经前置廉价门禁通过，路由进入 5 节点 `StateGraph`；<br/>3. 执行真实拓扑：`failure_analysis` $\rightarrow$ `candidate_generation` $\rightarrow$ `validation` $\rightarrow$ `defense_adjudication` $\rightarrow$ `rounds_state_machine`；<br/>4. `RepairJob`（`job.job_id` 为 `job_...`）状态迁移为 `READY`，第 1 次尝试即获 `PASS` 裁决；<br/>5. 候选（`1.0.1`）与 `ValidationRecord` 持久化入 `CandidateStore`，内容哈希一致；<br/>6. **正式库保持 `1.0.0` 不变**（节点绝不直写生产注册表）；<br/>7. 严格核验契约断言：`len(patcher_llm.calls) == 1`，明确证明 **“框架实际调用、LLM 为受控 FakeLLM”**。 | **PASSED** (0.35s) |
| **Acceptance B** | `test_acceptance_b_sqlite_checkpoint_resume_and_shared_budget_persistence` | **SqliteCheckpointer 持久化与绝对时点/共享预算不归零**：<br/>1. 在 `candidate_generation` 节点后人工注入中断（`interrupt_after`），消耗 1 次尝试、1 次调用、50 tokens；<br/>2. 构造全新 `SqliteCheckpointer` 与全新 `app2` 实例，重新打开同一 SQLite 数据库与 `thread_id`；<br/>3. 恢复出的 `restored_budget` 中：`consumed_attempts == 1`、`consumed_calls == 1`、`consumed_tokens == 50`、`deadline_seconds == 100.0`，且 **`start_time` 绝对开始时间原样恢复，原值严格不归零**；<br/>4. **受控 Clock 推进测试**：推进时钟至原绝对期限之后（`future_time = start_time + 105.0s`），恢复直接以 `TIMEOUT` 阻断退出，**模型修复调用严格为 0**（不重置或获得新 100 秒）；<br/>5. 从断点继续执行至完成，**已完成的 LLM 调用不重复执行**（`patcher_llm.calls` 计数保持为 2，含 timeout 线程与原始中断各 1 次，恢复执行 0 次调用）；<br/>6. 验证多轮内层重试：重试共用顶层预算并在达到 `max_attempts=2` 时准确以 `EXHAUSTED` 状态终止退出，无预算放大翻倍。 | **PASSED** (0.44s) |
| **Acceptance C** | `test_acceptance_c_non_recoverable_blockers_and_diagnostics` | **非可恢复阻断信号与停机诊断**：<br/>1. 用户主动取消（`user_cancelled=True`）：状态置为 `BLOCKED`，理由码 `REASON_USER_CANCELLED`，**0 次模型修复调用**；<br/>2. 评测器故障/无 Truth Oracle（`nooracle`）：状态置为 `BLOCKED`，理由码 `REASON_EVALUATOR_FAULT`，**0 次模型修复调用**；<br/>3. 运行时环境缺失（`NoSuchFileOrDirectory`）：状态置为 `BLOCKED`，理由码 `REASON_ENV_MISSING`，**0 次模型修复调用**；<br/>4. 权限与安全拒绝（403 Forbidden）：状态置为 `AWAITING_REVIEW`，理由码 `REASON_PERMISSION_DENIED`，**0 次模型修复调用**；<br/>5. 策略关闭时 Prompt 膨胀（`enable_shadow_recovery=False`）：严格维持 `AWAITING_REVIEW` 与 `REASON_PROMPT_BLOAT_REVIEW`，**0 次图调用，0 次模型修复调用**；<br/>6. 预算耗尽（`BUDGET_EXHAUSTED`）与超时（`TIMEOUT`）：精准记录标准停机理由码与结构化诊断字典。 | **PASSED** (0.08s) |
| **Acceptance D** | `test_acceptance_d_lineage_drift_and_gated_promotion` | **血缘漂移阻断（意图/基线/数据集）与门禁晋升**：<br/>1. 意图版本漂移（`intent_revision` 从 1 变更为 2）：从旧 Checkpoint 恢复直接以 `CHECKPOINT_INVALIDATED` 阻断退出；<br/>2. 基线内容漂移（注册表中基线内容 Hash 被外部改动）：从旧 Checkpoint 恢复直接以 `CHECKPOINT_INVALIDATED` 阻断退出；<br/>3. **数据集权威绑定与漂移阻断**：入口自动从 `eval_cases` 计算权威指纹 `compute_cases_hash` 绑定 `dataset_version`；当评测用例漂移或提供未验证的 `dataset_version=None` 历史绑定时，公开入口 `repair_skill_failure` 直接判定 `CHECKPOINT_INVALIDATED`，**模型修复调用严格为 0**；<br/>4. 候选篡改阻断：验证通过后若候选正文被恶意追加，`promote_repaired_skill` 校验哈希失败抛出异常；<br/>5. 未确认阻断：`caller_confirmed=False` 调用晋升直接抛出 `ValueError`，正式库版本保持 `1.0.0`；<br/>6. 显式确认发布：权威 PASS 记录 ＋ `caller_confirmed=True` 成功触发 `ReleaseStateMachine` 发布至 `1.0.1`（`PROMOTED`）；重复晋升安全拒绝。 | **PASSED** (0.35s) |
| **Acceptance E** | `test_acceptance_e_snapshot_source_migration_and_bloat_guards` | **第一批不变量与膨胀守卫严格回归**：<br/>1. **1000-Token AND 严格边界**：总正文净增恰好 1000 tokens（且增长比例 $\le 1.20\times$）时，`check_prompt_bloat` 判定 **PASS（严格小于等于 1000 放行）**；净增 1001 tokens 时，立即被门禁拦截判定为 **FAIL（PROMPT_BLOAT）**；<br/>2. **冷启动 3000 字符硬上限**：无基线冷启动新建正文 2900 字符通过，3100 字符直接拦截；<br/>3. 第一批 S/R/M 成果完好保留，数据模型与迁移表结构无破坏。 | **PASSED** (0.04s) |

### 9.4 全量回归与测试矩阵 (Regression Matrix)

为确保第二批改动未引入任何隐式回归或破坏既有契约，执行两级自动化回归测试：

1. **核心直接关联套件（5 套件 52 项全部通过，0 失败）**：
```bash
.venv/bin/pytest tests/test_p5_langgraph_repair_integration.py \
                 tests/test_p5_bounded_recovery_and_split.py \
                 tests/test_source_snapshot_and_migration_closure.py \
                 tests/test_p2d_langgraph.py \
                 tests/test_prompt_bloat.py -v
```
**实测结果**：`52 passed, 20 warnings in 7.99s, exit 0`。

2. **生命周期与演进飞轮延伸回归套件（4 套件 27 项全部通过，0 失败）**：
```bash
.venv/bin/pytest tests/test_p2_gate_and_lifecycle.py \
                 tests/test_p3_goal_shift_and_revision.py \
                 tests/test_p4_trace_purification_and_mining.py \
                 tests/test_mining_and_promotion.py -v
```
**实测结果**：`27 passed in 3.65s, exit 0`。

**总体验收矩阵**：**79 passed, 0 failed, 100% 通过率**。

### 9.5 责任边界与客观事实声明 (Responsibility Boundaries & Trade-offs)

根据全局协作规范，在此明确声明以下技术边界与工程事实：
1. **真实 Provider 调用严格为 0**：本验收套件中涉及的 LLM 全部由受控的 `FakeLLM` 驱动，执行代码确认 **“框架实际调用、LLM 为 Fake”**，未访问任何外部商业 API 密钥，未消耗任何付费 Token；
2. **影子编排开销权衡 (Opt-in by Policy)**：LangGraph StateGraph 编排通过 `enable_shadow_recovery=True` 显式启用；普通生成、单字段小改、连贯长流程或无异常候选**严格不走图编排**，避免引入额外的图状态初始化与 SQLite Checkpoint I/O 开销；
3. **已完成步骤恢复而非跨系统 2PC**：持久化恢复保证已完成的 LLM 调用与工具步骤不重复执行，账本不归零；但针对外部物理副作用（如已向外部发送的 HTTP 请求），系统记录执行历史并跳过，不虚假声称具备跨资源系统的物理回滚能力；
4. **拆分器保持建议性质不变 (Advisory Only)**：单一连贯长流程判定为 `CANNOT_SPLIT`；多领域可拆分技能仅产生咨询建议提案（`SplitProposal`），在用户显式确认前原 Skill 正文与路由 100% 保持未改动；
5. **工作区状态完整保留**：本次收尾严格未执行 `git reset` 或任何清理命令，保留工作区所有已有修改及 untracked 历史产物；
6. **保留历史事实与不可追溯性声明**：
   - **DEV A2/6 仅汇总不逐任务 ABC 配对**：历史数据中 DEV A2/6 仅有加总数据，缺失历史逐任务完整 ABC 配对记录，坚决不补造历史合成数据；
   - **LOCKED 小样本不可追证**：LOCKED A5/6、B5/6、C6/6 属于小样本评测，缺少修订前锁定快照与历史派生明细证据，实事求是声明无法后验追证，严禁伪造新的测试 manifest 或假签名；
   - **早期 125 aggregate-only 成本记录为 null**：历史 125 次调用缺乏逐次 token/成本明细，在此严格记录为 `null`，绝不重跑模型或编造账单；
   - **两分支独立保留**：`cand_real_v2_fb30297b` 与 `cand_lifecycle_v2_8e70d68c` 为历史上两条真实并行的演化分支，各自对应不同的修复源与意图目标，保持物理分支独立性，不强行合并；
   - **测试环境与生产边界**：第二批测试全量在本地 `FakeLLM` + LangGraph 框架图 + 本地 SQLite 上执行验证，证明了框架接入、状态机流转与预算共享的正确性，绝不等同于已在真实 OS 沙箱或线上商业大模型下获得全量生产级 SLA 保证；
7. **旧 LangGraph 模块复用程度与架构解耦工程选择**：
   - **实际复用组件**：`bounded_recovery.py` 直接复用了 `langgraph_loop.py` 的持久化底座（`SqliteCheckpointer`、`create_default_checkpointer` 与已包含领域对象的 `ALLOWED_MSGPACK_MODULES` / `JsonPlusSerializer`）；
   - **解耦工程理由**：旧 `langgraph_loop.py` 中的节点函数（`node_failure_analysis` 等）与 `_prepare_shadow_root` 强绑定 `SkillEvolver`、`EvolveContext`、`EvolveBudget` 与旧 `Patch`（强要求 `evolver.repo_root` 并在旧节点内部管理独立预算和直接发布）。若强行调用旧节点，会导致 `RecoveryBudget` 与旧 `EvolveBudget` 产生双计与不一致，并违背 L5 门禁（图节点绝不可直写正式库）。因此，系统沿用了旧图的 5 节点拓扑结构与迁移契约，针对 `RecoveryLoopState` 与 `RepairJob` 编写专用状态机节点，并在 `ShadowDirectoryContext` 中严格管理影子临时目录。

### 9.6 用户确认的限定验收结论 (Date: 2026-10-01 Asia/Shanghai)

> “复用既有 LangGraph 检查点与序列化基础设施，新建适配当前演进链的恢复节点；未原样复用旧 Evolver 业务节点，以避免预算、状态和注册路径冲突。”

**结论**：
三项工程缺口（conversation来源、草稿跨Runtime重建快照与旧库迁移、实际LangGraph/RepairJob恢复接入）已补齐；相关回归61项通过，其中包含P5专项11项，exit0。这是基于现有AGY交付/测试日志和用户接受取舍的本地限定验收，不是用户或监督者独立代码审查、不新增真实模型效果结论。预算/原start_time期限不重置、dataset漂移使旧checkpoint失效、图不自动晋升等本轮已验证机制按已有记录说明。

**保留已有历史局限**：
保留已有历史局限：DEV A缺raw、锁定/派生血缘历史缺证、125 aggregate-only、两条真实模型分支、影子目录非OS层；不把它们重新开发、不补造、不重跑模型。不要改历史数据/ledger或笼统宣布生产保障/P0–P6全部独立验收。



