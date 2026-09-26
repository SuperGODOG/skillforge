# SkillForge Evolution Loop - Milestone 1: Episode & Candidate Storage Guide

## 1. 概述 (Overview)
Milestone 1 (M1) 建立了经验（Episode）与候选技能（Candidate Skill）的受控数据契约与持久化存储：
- **运行凭据绑定**：将运行过程中的工具调用记录（`ToolCallProvenance`）、运行环境指纹、独立验收标准与结构化验证证据固化为 `Episode`。
- **字段完整性检查**：`outcome='success'` 要求必须显式附带 `verification_evidence` 且非单纯自称；此检查属于**契约字段完整性校验**，并非外部权威或密码学层面的可信来源认证，不能防止对抗性伪造输入。
- **候选严格隔离**：基于 `source_episode_ids` 关联来源的候选技能保存在隔离存储（`CandidateStore`），绝不混入在线活跃技能检索（`SkillRegistry`）。

---

## 2. 核心数据契约 (Data Contracts)

### 2.1 Episode
```python
from skillforge import Episode, ToolCallProvenance

episode = Episode(
    episode_id="ep_20260925_001",
    task_id="task_calc_sum",
    run_id="run_001",
    skill_name="math_calculator",
    skill_version="1.0.0",
    environment={"python": "3.13", "os": "darwin"},
    provenances=[
        ToolCallProvenance(
            tool_name="eval_expression",
            fixture_case_id="case_1",
            call_index=0,
            call_count=1,
            is_fixture=True,
            tool_required=True,
            tool_called=True,
            tool_success=True,
            authenticity_pass=True,
            input_params={"expr": "1 + 1"},
            output_status="SUCCESS",
            output_summary="2",
            latency_ms=12.5,
            timestamp="2026-09-25T11:00:00Z",
            signature="sig_abc",
        )
    ],
    acceptance_criteria={"expected_output": "2"},
    verification_evidence={"independent_pass": True, "checker": "assertion"},
    outcome="success",  # "success" | "failure" | "unknown"
    outcome_reason="Independent assertion verified output is 2",
)
```
> **字段完整性校验**：若 `outcome="success"` 但缺少 `verification_evidence` 或显式标为模型自评（`self_asserted=True` 且无 `independent_pass`），将直接触发 `ValueError`。此逻辑仅核验字段存在性与标志契约，不构成外部信任根。

### 2.2 CandidateSkill
```python
from skillforge import CandidateSkill, SkillMeta, Trigger

candidate = CandidateSkill(
    candidate_id="cand_20260925_001",
    skill_name="math_calculator",
    decision="revise",  # "create" | "revise" | "abandon"
    source_episode_ids=["ep_20260925_001"],
    meta=SkillMeta(
        name="math_calculator",
        version="1.0.1",
        description="Math expression evaluator",
        use_when="Calculate arithmetic",
        not_for=["Symbolic calculus"],
        trigger=Trigger(keywords=["add", "calc"]),
    ),
    body="## Instructions\nUse eval_expression with verified bounds.",
    rationale="Fixed division edge case found in source episode",
)
```
> **来源引用存在性校验**：`CandidateStore.save_candidate()` 会校验 `source_episode_ids` 在 `EpisodeStore` 中必须非空且真实存在；引用不存在的 episode_id 会被直接拒识（`KeyError`）。

---

## 3. 存储与隔离 (Storage & Isolation)

```python
from pathlib import Path
from skillforge import EpisodeStore, CandidateStore, SkillRegistry

db_path = Path("runs/skillforge.db")
ep_store = EpisodeStore(db_path)
cand_store = CandidateStore(db_path, episode_store=ep_store)

# 1. 保存 Episode（重复 ID 策略：on_conflict="error" 抛出异常，或 "ignore" 幂等保留）
ep_store.save_episode(episode, on_conflict="error")

# 2. 保存候选技能（与 active 隔离）
cand_store.save_candidate(candidate)

# 3. 活跃注册表隔离性保证：
# SkillRegistry 只读取已发布的 skills/*/SKILL.md，
# CandidateStore 中的候选技能绝对不会进入 build_index()、list_names() 或 use_skill()！
```

---

## 4. Milestone 2: 挖掘、沙箱验证与受控晋升 (Mining & Promotion)

### 4.1 编排与测试替身边界说明 (Orchestration vs Test Double)
- **真实业务编排**：
  - `mine_candidate`：应用强行绑定输入 Episode ID，严格校验来源有效性；独立评测集/heldout 绝不送入 miner prompt；若全部 Episode 均为 unknown 或无证据则安全放弃（`abandon`）。
  - `validate_candidate`：在沙箱临时注册表中运行真实 `SkillEvaluator` 与 `check_ratchet` 棘轮门控，计算候选内容哈希（`content_hash`）与基线版本。
  - `promote_candidate`：仅允许 `PASS` 判定且调用方显式确认（`caller_confirmed=True`）时晋升；任何 REVIEW/DECLINED/异常均阻断；内容哈希或基线版本不一致直接失效；重复调用拒绝二次发布。通过既有 `ReleaseStateMachine` 走 Git 提交与 SQLite 原子状态切换。
- **FakeLLM 测试替身**：
  - 仅用于在离线测试中确定性模拟 LLM 输出文本与 Judge 判定结果，不调用外部收费 API。
  - 门控逻辑、状态流转、哈希校验与 Git 事务均为真实执行，未做任何门禁绕过；不宣称任何模型能力或实际胜率提升。

### 4.2 离线可运行示例 (Offline Example)
```python
from skillforge import (
    EpisodeStore,
    CandidateStore,
    SkillRegistry,
    ReleaseStateMachine,
    mine_candidate,
    validate_candidate,
    promote_candidate,
)

# 1. 从已持久化的真实 Episode 挖掘候选技能
mining_res = mine_candidate(
    episodes=[ep1, ep2],
    target_skill_name="text_cleaner",
    llm=my_llm,
    candidate_store=cand_store,
    registry=registry,
)
candidate = mining_res.candidate

# 2. 隔离沙箱评测（评测集仅供验证，绝不泄露给挖掘器）
val_rec = validate_candidate(
    candidate=candidate,
    evaluator=evaluator,
    registry=registry,
    eval_cases=eval_cases,
)

# 3. 受控晋升（必须同时满足 PASS 与调用方显式授权）
if val_rec.ratchet_decision == "PASS":
    release = promote_candidate(
        candidate=candidate,
        validation_record=val_rec,
        state_machine=state_machine,
        registry=registry,
        candidate_store=cand_store,
        caller_confirmed=True,  # 显式确认授权
    )
    print(f"Skill '{candidate.skill_name}' successfully promoted to version {release.version}")
```

---

## 5. 整体自演化闭环演进路线图 (Evolution Roadmap)

SkillForge 整体自演化目标为：`运行 ➔ 经验池 ➔ 模式挖掘 ➔ 候选 ➔ 验证 ➔ 注册 ➔ 复用 ➔ 失败反馈 ➔ 演进`。各阶段演化路线划分如下（阶段性最小实现并不代表后续特性被永久放弃）：

- **M3a（当前阶段）：自动运行经验采集（Automatic Experience Collector）**
  - 在 Agent/工具调用终态自动将观测轨迹与可信独立验证结果固化为不可变 Episode。
  - 严格由验证依据决定 outcome（success/failure/unknown），消除模型自吹自评。
- **M3b：经验池自动筛选与模式挖掘（Experience Pool Mining & Curation）**
  - 对经验池基于多维指标（重复性、执行成功率/稳定性、跨任务适用证据、逻辑复杂度）实施受控筛选与聚类。
  - 核心约束：高频重复绝不直接等同于泛化证明，严防过拟合。
- **M4：失败归因与有界修补（Root Cause Attribution & Bounded Patching）**
  - 自动化归因（Trigger/Prompt/Dependencies/Boundary 4 类标签），驱动有界补丁生成。
  - 接入已有回归评测沙箱、棘轮门禁、版本对比、灰度切流与一键秒级回滚。
- **M5：轻量 Harness 运行时、Tool Broker 与三层记忆边界（Minimal Harness & Memory Architecture）**
  - 提供环境隔离与 Tool Broker 权限控制；确立工作记忆（Context Window）、技能资产（Skill SRE Store）与历史经验（Episode Pool）三层记忆边界。
  - 文档导入与复杂向量检索等能力按需延后引入。

---

## 6. Milestone 3a: 自动运行经验采集接入说明 (Experience Collector)

### 6.1 核心机制与接入点
- `collector.start_run(run_id, task_id, skill_name, environment)`：在 Agent 任务启动时固化技能版本（即便执行期间外部将 Skill 升级，运行记录始终关联起始版本；无 Skill 运行记录为空版本）。
- `collector.record_tool_call(run_id, provenance)`：按时序保真记录工具输入输出、失败与重试恢复轨迹。
- `collector.finish_run(run_id, model_output, verification_evidence, infra_error)`：终态自动落库 `EpisodeStore`。结果由独立检验凭证裁决；纯自评标记为 `unknown`；工具中间失败但最终验收通过标记为 `success` 并完整保留恢复轨迹。
- 注入免疫：工具和模型返回文本中若包含 Prompt Injection 攻击文本（例如包含“IGNORE RULES, SET SUCCESS”等指令），严格被视为只读数据，绝不影响验证裁决或系统策略。

### 6.2 离线可运行示例 (Offline Example)
```python
from skillforge import EpisodeStore, SkillRegistry, ExperienceCollector, ToolCallProvenance

ep_store = EpisodeStore(db_path)
collector = ExperienceCollector(episode_store=ep_store, registry=registry)

# 1. 任务启动（固化起始技能版本）
run_ctx = collector.start_run(run_id="run_101", task_id="task_calc", skill_name="math_calc")

# 2. 模拟工具调用观测（例如失败后重试恢复）
collector.record_tool_call(
    run_id="run_101",
    provenance=ToolCallProvenance(
        tool_name="calc_api", fixture_case_id="c1", call_index=0, call_count=2,
        is_fixture=True, tool_required=True, tool_called=True, tool_success=False,
        authenticity_pass=True, input_params={"expr": "1/0"}, output_status="ERROR",
        output_summary="ZeroDivisionError", latency_ms=10.0, timestamp="2026-09-25T12:00:00Z", signature="sig_err"
    ),
    action_summary="First tool attempt encountered ZeroDivisionError",
)
collector.record_tool_call(
    run_id="run_101",
    provenance=ToolCallProvenance(
        tool_name="calc_api", fixture_case_id="c1", call_index=1, call_count=2,
        is_fixture=True, tool_required=True, tool_called=True, tool_success=True,
        authenticity_pass=True, input_params={"expr": "1/1"}, output_status="SUCCESS",
        output_summary="1.0", latency_ms=8.0, timestamp="2026-09-25T12:00:01Z", signature="sig_ok"
    ),
    action_summary="Recovery attempt succeeded with guarded input",
)

# 3. 终态自动持久化（基于可信业务断言裁定 success）
episode = collector.finish_run(
    run_id="run_101",
    model_output="Calculation recovered successfully",
    verification_evidence={"independent_pass": True, "checker": "unit_assertion"},
)
assert episode.outcome == "success"
assert len(episode.provenances) == 2
```

---

## 7. Milestone 3b: 经验池自动筛选与模式挖掘 (Episode Pool Pattern Mining)

### 7.1 核心设计与非协商约束 (Core Invariants)
- **严格 A8 隔离 (Purpose Isolation)**：
  - 扫描经验池时，仅保留 `environment.purpose == "learning"` 的 Episode；
  - `purpose` 为 `"evaluation"`、`"heldout"` 以及缺失/未指定的记录，在聚类、向量化及 LLM 提示词合成前被**严格剔除**；
  - 评测集密文/sentinel 绝不泄漏到挖掘器提示词中。
- **启发式候选发现代理 (Heuristic Proxy, Not Proven Generalization)**：
  - 高频重复与语义相似仅作为候选发现的启发式代理条件，**不能直接等同于跨领域泛化已成立**；
  - 最终泛化能力必须交由 M2 的独立测试用例沙箱评测与棘轮门控（Ratchet Gate）验证。
- **任务幂等去重 (Task Deduplication)**：
  - 多次执行同一 `task_id` 不会虚增独立支持度，算法按 `(created_at, run_id)` 确定性选取最新单次运行作为代表样本。
- **保守多维阈值过滤 (Conservative Thresholds)**：
  - `min_support >= 3`：至少 3 个独立任务支撑；
  - `min_success_rate >= 0.8`：已知结果中成功率需达标，失败案例作为反例保留送入 Miner 提示词；
  - `min_coverage >= 0.8`：有效结果占比 `(success + failure) / total` 需达标，未知结果（`unknown`）过多时主动放弃（`abstain`）；
  - `min_expressions >= 2`：归一化任务文本表达需具备多样性，防止单一固定句式过拟合；
  - `min_steps >= 2`：可观测复杂度代理（包含工具调用 Provenances 或文档类任务的显式 action steps）。
- **批次幂等与持久化记账 (Batch Idempotency & Persistence)**：
  - 基于排好序的 source episode IDs、目标技能名称、基线版本以及策略参数计算 SHA-256 指纹 `batch_fingerprint`，写入 SQLite `mined_batches` 表；
  - 相同输入再次调用直接返回缓存结果，0 额外 LLM 调用；重新打开 DB 依然生效；目标技能版本升级自动失效旧缓存。
- **候选严格隔离**：
  - 挖掘生成的候选技能以 `DRAFT` 状态存入 `CandidateStore`，绝不直接发布或修改在线注册表 `SkillRegistry`。

### 7.2 API 与配置参数 (API & Configuration)
```python
from skillforge import PatternMiningConfig, mine_pending

# 阈值配置
config = PatternMiningConfig(
    min_support=3,              # 聚类最小独立任务数
    min_success_rate=0.8,       # 最小成功率 (success / (success + failure))
    min_coverage=0.8,           # 最小已知结果覆盖度 ((success + failure) / total)
    min_expressions=2,          # 最小归一化任务表达多样性数量
    min_steps=2,                # 最小可观测步骤数（工具调用或显式动作）
    similarity_threshold=0.80,  # 任务聚类余弦相似度阈值
)

# 批次挖掘入口
report = mine_pending(
    episode_store=episode_store,
    candidate_store=candidate_store,
    registry=registry,          # 可选，用于版本绑定与 revise 判定
    llm=llm,                    # FakeLLM 或生产 LLM 客户端
    embedder=embedder,          # 可选，自定义向量化函数 (默认自动探测 EmbedLayer / 词袋哈希)
    config=config,
)
```

### 7.3 返回结构 (MiningBatchReport)
- `report.total_episodes_scanned`: 扫描的总 Episode 数量。
- `report.learning_episodes_count`: 筛选出的合法学习 Episode 数量。
- `report.filtered_evaluation_episodes`: 被 A8 边界隔离的评估/heldout/未知用途 Episode 数量。
- `report.unique_tasks`: 去重后的独立任务数量。
- `report.clusters`: 聚类详情列表（含支持度、成功率、覆盖度、反例 IDs、决策、候选引用等）。
- `report.candidates_created`: 本批次新建的候选技能列表。
- `report.candidates_revised`: 本批次修订的候选技能列表。
- `report.abstained_clusters`: 因不满足阈值或 LLM 放弃的聚类报告列表。

### 7.4 验证场景 D1 - D8 覆盖说明 (Verification Evidence)
在 `tests/test_pattern_mining.py` 中全量覆盖并通过以下 8 项验收场景：
- **D1 (`test_scenario_d1_auto_cluster_and_candidate_creation`)**：3 个独立任务（>= 2 步、>= 2 表达、高相似度）自动聚类，FakeLLM 生成 1 个候选（DRAFT），来源 ID 严格匹配，在线注册表完全隔离。
- **D2 (`test_scenario_d2_conservative_threshold_abstain`)**：保守阈值拦截：
  - D2a: 独立任务数不足 (< 3) 拦截；
  - D2b: 单任务重复执行经去重后 (< 3) 拦截；
  - D2c: 表达多样性不足 (< 2) 拦截；
  - D2d: 可观测步骤复杂度不足 (< 2) 拦截；所有拦截均发生于 LLM 调用前（0 LLM 调用，0 候选）。
- **D3 (`test_scenario_d3_success_rate_and_coverage_filtering`)**：指标过滤：
  - D3a: 4 成功 1 失败（80% 成功率，100% 覆盖度）通过门控，失败案例保留在 `counter_example_episode_ids`；
  - D3b: 3 成功 2 失败（60% 成功率 < 80%）因成功率过低放弃；
  - D3c: 2 成功 3 unknown（40% 覆盖度 < 80%）因未知过多放弃。
- **D4 (`test_scenario_d4_semantic_separation_and_document_workflow`)**：语义解耦与文档任务复杂度：
  - D4a: 共享基础文件工具的两类语义任务（代码审查 vs SQL 优化）被准确分流为 2 个独立聚类并各自生成候选；
  - D4b: 无工具调用的纯文档任务通过显式 `steps` 动作流满足复杂度门控并顺利生成候选。
- **D5 (`test_scenario_d5_evaluation_heldout_isolation`)**：严格 A8 隔离：带有机密 sentinel 字符串的评测/heldout 与未知用途任务在预处理阶段全量过滤，sentinel 绝对不进入 LLM 提示词。
- **D6 (`test_scenario_d6_batch_idempotency_and_persistence`)**：批次幂等与持久化记账：
  - 重复调用直接复用 `mined_batches` 记录（0 额外 LLM 调用）；
  - 关闭并重新连接 SQLite 后缓存有效性保持；
  - 加入新任务后指纹变更，自动触发增量评估且不覆盖已有候选。
- **D7 (`test_scenario_d7_revise_existing_skill_and_target_version_drift`)**：已存在技能修订与基线漂移：
  - D7a: 已在注册表中技能触发 `revise` 决策并绑定旧版本；
  - D7b: LLM 输出非法结构时安全记录 `abandon` 而不崩溃；
  - D7c: 注册表中目标技能升级版本后，批次指纹自动变更，不复用旧缓存。
- **D8 (`test_scenario_d8_e2e_m3a_collector_to_m3b_mining`)**：端到端连通：通过真实的 `SkillEvaluator.evaluate_skill(..., purpose="learning", collector=collector)` 运行并落库，直接驱动 `mine_pending` 挖掘出隔离的 DRAFT 候选。

### 7.5 局限性与后续演进建议 (Limitations & Next Steps)
- **当前局限性**：
  - 任务文本嵌入优先使用本地模型或确定性词袋哈希，对于语义差异极细微但词汇高度重合的任务可能需要更精细的特征工程；
  - 阈值为固定保守规则，尚未支持随经验池规模自适应调节动态门槛；
  - 模式挖掘只产出候选，不证明泛化成立。
- **下一步（Roadmap）**：
  - 推进 **Milestone 4a（M4a）**：失败归因（Failure Attribution）、有界修补（Bounded Patch）与回归防退化；
  - 推进 **Milestone 4b（M4b）**：版本对比（Version Diff）、安全回滚（Rollback）与灰度发布（Canary）；
  - 推进 **Milestone 5（M5）**：轻量级 Harness 运行时、Tool Broker 权限与三层记忆边界落地。

---

## 8. Milestone 4a: 失败归因、有界修补与回归晋升 (Failure Attribution & Bounded Repair)

### 8.1 概述 (Overview)
Milestone 4a (M4a) 实现了闭环演化回路中的核心自愈机制：**从失败经验中归因责任、有界生成语义补丁、通过沙箱回归与棘轮门控验证、在人工确认下安全晋升**。
- **责任层严格界定**：将失败根因划分为 6 大责任层（`skill` | `tool` | `policy` | `planner` | `evaluator` | `unknown`）。高置信度结构化信号（工具崩溃、权限拦截、评测语法异常、规划缺失）优先覆盖 LLM 猜测；证据冲突或不足时保守回退为 `unknown`。
- **有界修补范围与责任边界**：
  - 仅归因为 `skill` 层的失败允许触发修补候选生成，限定于 4 种修补策略（`trigger`, `prompt`, `dependencies`, `boundary`）；
  - 非 skill 层缺陷（安全策略、工具底层、评测用例）输出清晰 handoff/needs_review 诊断，严禁擅自修改 Policy、Tool 或 Evaluator。
- **受控状态机与预算持久化**：
  - SQLite `repair_jobs` 表持久化跟踪指纹、基线版本、尝试预算与去重哈希，跨 DB 重开预算保持有效；
  - 仅有效评测判定为 `DECLINED` 允许在预算（默认 2 次）内自动重试；
  - 软门槛触发 `REVIEW` 即刻停止并等待人工复核（`AWAITING_REVIEW`）；
  - 评测基础设施或用例异常标记为 `BLOCKED`，不归咎于技能；
  - 生成重复补丁哈希立即终止（`DECLINED`），避免无谓评测消耗。
- **严格隔离与可控晋升**：
  - A8 目的隔离：评测/heldout 机密 Sentinel 绝不流入 patcher 提示词；
  - 候选技能仅以 `DRAFT` 状态保存在沙箱与 `CandidateStore`，未发布前对在线活跃注册表 `SkillRegistry` 完全隐形；
  - 晋升必须显式声明 `caller_confirmed=True`，并严格校验基线版本漂移与候选内容篡改。

### 8.2 归因责任层 vs 修补策略对照表 (Responsibility Layers vs Patch Strategies)

| 责任层 (`responsibility_layer`) | 触发特征 / 证据来源 | 允许修补策略 (`strategy`) | 处理动作 | 责任边界保证 |
| :--- | :--- | :--- | :--- | :--- |
| **`skill`** | 工具执行正常，但模型业务输出未达标、边界未处理 | `trigger` / `prompt` / `dependencies` / `boundary` | 启动有界修补循环，生成 semver patch 候选 | 仅修改候选 SKILL.md，active 保持只读 |
| **`tool`** | 工具抛出 500、ConnectionRefused、Timeout、BrokenPipe 等底层基础设施异常 | 无 (`None`) | 立即阻断（`BLOCKED`），输出工具运维 handoff | 严禁篡改工具代码或掩盖故障 |
| **`policy`** | 403 Forbidden、Unauthorized、安全策略违规拦截 | 无 (`None`) | 立即停止等待安全审批（`AWAITING_REVIEW`） | 严禁自动越权修改安全白名单或策略 |
| **`planner`** | 规划器漏传必选参数、路由分发错误 | 无 (`None`) | 输出规划器调整建议（`DECLINED`） | 不把规划器缺陷归咎于下游技能 |
| **`evaluator`** | 评测用例集为空、测试脚本语法错误、Judge 响应异常 | 无 (`None`) | 立即阻断（`BLOCKED`），输出用例修复 handoff | 评测异常不扣减技能分数 |
| **`unknown`** | 证据冲突、无 Provenance 凭证或缺少有效 Learning 失败记录 | 无 (`None`) | 保守拒绝（`DECLINED`），要求补充可验证凭据 | 不凭空猜测未观测根因 |

### 8.3 状态机转换与重试/停止规则表 (State Machine & Retry/Stop Rules)

| 当前状态 | 触发事件 / 评测判定 | 下一状态 | 允许自动重试？ | 说明 / 保护机制 |
| :--- | :--- | :--- | :---: | :--- |
| **`IN_PROGRESS`** | 归因非 `skill` 层 | `BLOCKED` / `AWAITING_REVIEW` / `DECLINED` | 否 | 非技能缺陷即刻交接，0 patcher 调用 |
| **`IN_PROGRESS`** | 棘轮判定 `PASS` | **`READY`** | 否（已就绪） | 产出合规修补候选，等待调用方显式确认 |
| **`IN_PROGRESS`** | 棘轮软门槛 `REVIEW` | **`AWAITING_REVIEW`** | 否 | 单维度变化 ≥ 10% 须人工审核，禁止自动重试掩盖漂移 |
| **`IN_PROGRESS`** | 评测器异常 (`valid=False`) | **`BLOCKED`** | 否 | 基础设施或用例无效阻断，不消耗重试预算 |
| **`IN_PROGRESS`** | 生成与前次相同 patch hash | **`DECLINED`** | 否 | 立即检测并终止，避免冗余评测开销 |
| **`IN_PROGRESS`** | 棘轮判定 `DECLINED` (attempt < max) | `IN_PROGRESS` | **是** | 结构化错误反馈传入 Patcher，消耗 1 次预算继续迭代 |
| **`IN_PROGRESS`** | 棘轮判定 `DECLINED` (attempt >= max) | **`EXHAUSTED`** | 否 | 预算耗尽保护，防止发散无限死循环 |
| **`READY`** | 调用方未确认 (`caller_confirmed=False`) | `READY` | 否 | 拦截未受控晋升，抛出明确确认要求 |
| **`READY`** | 基线版本漂移 / 候选内容篡改 | `READY` (报错) | 否 | 严格阻断脏发布，在线活跃基线不受破坏 |
| **`READY`** | 调用方显式确认 (`caller_confirmed=True`) | **`PROMOTED`** | 否 | 通过 `ReleaseStateMachine` 发布，写入 active 注册表与 Git |

### 8.4 API 使用示例 (API Usage)

```python
from pathlib import Path
from skillforge import (
    EpisodeStore, CandidateStore, SkillRegistry, SkillEvaluator,
    ReleaseStateMachine, attribute_failure, repair_skill_failure,
    promote_repaired_skill,
)

# 1. 根因归因
diagnosis = attribute_failure(episodes=[failed_ep], skill_name="math_tool")
if diagnosis.responsibility_layer == "skill":
    print(f"Attributed strategy: {diagnosis.strategy}, reason: {diagnosis.reason}")

# 2. 有界修补执行回路
job = repair_skill_failure(
    episodes=[failed_ep],
    skill_name="math_tool",
    episode_store=ep_store,
    candidate_store=cand_store,
    registry=reg,
    evaluator=evaluator,
    eval_cases=regression_cases,
    llm=patcher_llm,
    max_attempts=2,
)

# 3. 受控晋升发布
if job.status == "READY":
    release = promote_repaired_skill(
        job=job,
        candidate_store=cand_store,
        registry=reg,
        state_machine=state_machine,
        caller_confirmed=True,  # 必须显式人工确认
    )
    print(f"Successfully promoted to version {release.version}")
```

### 8.5 验收场景 E1 - E8 覆盖说明 (Verification Evidence)
在 `tests/test_failure_attribution_and_patching.py` 中全量覆盖并通过以下 8 项验收场景：
- **E1 (`test_scenario_e1_attribution_responsibility_layers_and_non_skill_handoff`)**：6 大责任层归因阶梯；非 skill 信号产生清晰 handoff 信息且 0 patcher 调用，不修改 Policy/Tool；成功/未知结果禁止进入修补。
- **E2 (`test_scenario_e2_skill_failure_directional_patch_candidate_and_contract_guards`)**：有效 skill 失败生成绑定 baseline 版本的 patch 递增候选（1.0.0 -> 1.0.1）；active 源码在晋升前严格保持原样；非法归因与伪造凭证安全回退为 unknown。
- **E3 (`test_scenario_e3_two_round_bounded_reflection_with_decline_and_pass`)**：真实 2 轮反思修补：第 1 轮真实评测 DECLINED，反馈注入提示词，第 2 轮真实评测 PASS；两轮候选 ID 与 Hash 严格独立；评测 heldout sentinel 绝对不泄漏给 patcher。
- **E4 (`test_scenario_e4_attempt_budget_exhaustion_duplicate_hash_and_db_reopen`)**：预算与去重守卫：连续失败在达到 `max_attempts` 后终止为 EXHAUSTED；生成相同 patch hash 即刻终止 DECLINED 且跳过冗余评测；关闭并重开 DB 后预算与完成状态完整保持。
- **E5 (`test_scenario_e5_stop_conditions_review_security_and_evaluator_blocked`)**：非可重试终止条件：软门槛 REVIEW 终止为 AWAITING_REVIEW；非法 YAML/安全违规终止；评测基础设施/空用例集标记为 BLOCKED；active 保持只读。
- **E6 (`test_scenario_e6_ready_pass_requires_confirmation_and_promotes_with_lineage`)**：受控晋升与血缘：READY 候选未经确认拒绝晋升；显式确认后通过 `ReleaseStateMachine` 完成原子发布并可立即由 `SkillRegistry` 检索，重复发布拒绝。
- **E7 (`test_scenario_e7_invalidation_on_baseline_drift_or_candidate_mutation`)**：基线漂移与哈希篡改失效：活跃基线升级或候选内容在评测后被外部篡改时拒绝晋升并报错，新基线不受破坏。
- **E8 (`test_scenario_e8_purpose_isolation_and_untrusted_prompt_injection_immunity`)**：A8 用途隔离与提示注入免疫：仅 learning 失败进入修补回路，evaluation/heldout 失败在入口被严格过滤；工具输出中的对抗性系统指令不影响归因、评测与发布门禁。

### 8.6 局限性与路线下一步 (Roadmap: Milestone 4b)
- **当前局限性**：
  - 失败归因采用高置信度规则与结构化校验启发式代理，无法替代全面的人工代码审查；
  - 修补预算为固定尝试上限，尚未支持基于 token 成本或风险评级的动态自适应预算；
  - 当前修复以单一活跃版本作为 baseline 进行一对一棘轮对比。
---

## 9. 版本对比、受控回滚与灰度路由 (Milestone 4b)

### 9.1 架构与设计原则

Milestone 4b 在 SkillForge 中引入了生产级的版本生命周期治理能力，包括只读版本对比、受控灰度准入门禁、确定性分流与执行快照冻结、以及事务原子回滚与全量审计追溯。

```mermaid
flowchart TD
    Candidate["M4a Repaired Candidate<br/>(READY / PASS)"]
    Gate{"Canary Admission Gate<br/>(ratchet_decision==PASS<br/>caller_confirmed==True<br/>hash & baseline verified)"}
    Canary["Canary Deployment<br/>(stable=v1, canary=v2, share=N%)"]
    Router{"Deterministic SHA256<br/>hash(skill:rollout:run_id)%100"}
    Freeze["Freeze Binding in DB<br/>(run_version_bindings)"]
    Collector["ExperienceCollector<br/>(records exact version & hash)"]
    Promote["Promote Canary<br/>(stable=v2, canary=None)"]
    Rollback["Controlled Rollback<br/>(CAS expected_rev, audit logged)"]

    Candidate --> Gate
    Gate -->|Passed| Canary
    Gate -->|Rejected| StableOnly["Stable Only (v1)"]
    Canary --> Router
    Router -->|bucket < share| Freeze
    Router -->|bucket >= share| Freeze
    Freeze --> Collector
    Canary -->|Verified in Canary| Promote
    Promote -->|Production Degraded| Rollback
```

#### 9.1.1 版本快照与只读对比 (Snapshot & Comparison)
- **不可变快照 (`VersionSnapshot`)**：每个已发布版本拥有强内容哈希 SHA-256 校验。读取历史版本快照时直接返回该版本提交历史的规范内容，杜绝以磁盘上最新未提交或未验证的变动冒充历史版本。
- **Fail-Closed 闭环保护**：若快照内容哈希与记录不一致，系统直接抛出 `ValueError("Snapshot corrupted")` 拒绝提供服务，杜绝静默降级或读入污染内容。
- **只读比对 (`VersionComparison`)**：支持跨版本的统一文本 diff、结构化元数据 diff、工具依赖变更 diff 以及评测基准指标比较。若评测协议或数据集不一致，显式标记为 `incomparable`；缺失分数标记为 `N/A`；比对过程绝对不修改任何数据库或 Git 状态。

#### 9.1.2 灰度准入门禁 (Canary Admission Gate)
- **准入门槛**：仅接受经真实沙箱评测为 `PASS`、且基线版本与当前 stable 一致、内容哈希未被篡改的候选技能（或已发布的历史版本）。`DRAFT`、`DECLINED`、`REVIEW` 状态候选一律严加拦截。
- **显式确认原则**：必须由调用方显式传递 `caller_confirmed=True`，严防系统隐式切流。
- **乐观并发控制 (CAS)**：所有发布状态变更支持 `expected_revision` 校验。并发冲突时抛出 `ConcurrencyError`，防止并发写覆盖。

#### 9.1.3 确定性哈希分流与执行快照冻结 (Deterministic Routing & Run Freezing)
- **确定性 SHA256 桶分配**：基于标准库 SHA256 实现 `hash(f"{skill}:{rollout_id}:{run_id}") % 100`。在指定的 `share`（0~100）区间内，相同 `run_id` 的调用在进程重启或数据库重开后分流结果绝对幂等一致。
- **运行绑定冻结 (`run_version_bindings`)**：运行实例（Run）首次解析路由时，立即在数据库中冻结分配的版本号、内容哈希与完整正文。后续灰度比例调整、晋升或紧急回滚均**不影响正在执行或已绑定的旧 Run**，新启动的 Run 则按最新部署状态路由。
- **采集器版本一致性**：`SkillRegistry.use_skill` 与 `ExperienceCollector` 严格记录运行绑定的真实版本与哈希，确保后续生成的 Episode 血缘可信。

#### 9.1.4 受控回滚与全量审计追溯 (Controlled Rollback & Audit Trail)
- **受控回滚**：回滚到目标历史版本需要 `caller_confirmed=True` 与审计原因。目标版本必须是已验证的有效发布，且运行环境满足目标版本声明的外部工具依赖。回滚会停用当前灰度并原子重置 stable 指针。
- **幂等与审计 (`deployment_audit_events`)**：支持 `operation_id` 防重幂等，系统完整记录每一次 `SET_CANARY`、`CHANGE_SHARE`、`PROMOTE_CANARY`、`ROLLBACK` 操作的操作前后状态、修订版本号、变更原因与时间戳。

---

### 9.2 核心数据模型与 API

#### 9.2.1 数据模型
- `VersionSnapshot`：技能版本的不可变快照（版本号、内容哈希、元数据、正文、发布状态、依赖项列表、评测摘要、来源血缘）。
- `VersionComparison`：版本间统一比对结果（文本 diff、元数据 diff、依赖变动、评测分数对比、可比性标识）。
- `Deployment`：部署状态（stable 版本/ReleaseID、canary 版本/ReleaseID/分流比例、rollout_id、修订版本号 revision）。
- `DeploymentAuditEvent`：部署审计事件记录（事件 ID、操作 ID、动作类型、变更前/后版本与比例、原因、修订号变更）。
- `RunVersionBinding`：运行实例与版本的绑定记录（运行 ID、技能名称、分配版本号、内容哈希、是否灰度、冻结正文）。

#### 9.2.2 核心管理接口 (`DeploymentManager`)
- `get_version_snapshot(skill_name, version) -> VersionSnapshot`
- `compare_versions(skill_name, version_a, version_b) -> VersionComparison`
- `get_deployment(skill_name) -> Deployment`
- `set_canary(skill_name, candidate_or_version, validation_record=None, share=10, caller_confirmed=False, ...) -> Deployment`
- `change_canary_share(skill_name, share, caller_confirmed=False, ...) -> Deployment`
- `promote_canary_to_stable(skill_name, caller_confirmed=False, ...) -> Deployment`
- `rollback_deployment(skill_name, target_version, reason, caller_confirmed=False, ...) -> Deployment`
- `route_version(skill_name, run_id=None, cohort_key=None) -> tuple[version, content_hash, is_canary]`
- `get_run_body(run_id, skill_name) -> str`
- `list_audit_events(skill_name=None) -> list[DeploymentAuditEvent]`

---

### 9.3 快速使用示例

```python
from pathlib import Path
from skillforge import SkillRegistry, DeploymentManager, ExperienceCollector, EpisodeStore

db_path = Path("skillforge.db")
skills_dir = Path("skills")
ep_store = EpisodeStore(db_path)
reg = SkillRegistry(db_path=db_path, skills_dir=skills_dir)
reg.load_skills_from_dir()

dm = DeploymentManager(db_path=db_path, skills_dir=skills_dir, registry=reg)
reg._deployment_manager = dm

# 1. 版本对比
comp = dm.compare_versions("math_tool", "1.0.0", "1.1.0")
print(f"Diff lines:\n{comp.content_diff}")
print(f"Dependencies delta: {comp.dependencies_diff}")

# 2. 灰度发布 (30% 流量分配给 v1.1.0)
dm.set_canary(
    "math_tool",
    candidate_or_version="1.1.0",
    share=30,
    caller_confirmed=True,
)

# 3. 运行路由与执行快照冻结
version, content_hash, is_canary = dm.route_version("math_tool", run_id="run_101")
body = reg.use_skill("math_tool", reason="execute calc", run_id="run_101")

# 4. 经验采集器记录精确绑定的版本与哈希
collector = ExperienceCollector(episode_store=ep_store, registry=reg)
collector.start_run(run_id="run_101", task_id="t_1", skill_name="math_tool", skill_version=version)
collector.finish_run(run_id="run_101", model_output=body, verification_evidence={"independent_pass": True})

# 5. 灰度完成，全量晋升
dm.promote_canary_to_stable("math_tool", caller_confirmed=True)

# 6. 观测到异常，受控紧急回滚到历史版本 1.0.0
dm.rollback_deployment(
    "math_tool",
    target_version="1.0.0",
    reason="Regression observed in production canary cohort",
    caller_confirmed=True,
)
```

---

### 9.4 验收场景 F1 - F8 覆盖说明 (Verification Evidence)

在 `tests/test_version_rollback_and_canary.py` 中全量覆盖并通过以下 8 项验收场景：

- **F1 (`test_scenario_f1_version_comparison_and_snapshot_integrity`)**：版本快照与只读比对：准确提取 Git/SQLite 历史快照；比对 metadata/依赖/正文 diff 及评测 delta；评测协议不匹配标记为 incomparable；读取历史版本绝对不返回磁盘最新未提交变动。
- **F2 (`test_scenario_f2_canary_admission_gate`)**：灰度准入守卫：DRAFT、DECLINED、REVIEW 候选均不可作为灰度；未确认的 PASS 候选不可作为灰度；基线漂移或内容篡改即刻拦截并报错；经显式确认的 PASS 候选可安全注册为灰度，stable 保持不变。
- **F3 (`test_scenario_f3_deterministic_hash_routing_and_boundaries`)**：确定性哈希分流与边界条件：`share=0` 保证 100% stable，`share=100` 保证 100% canary；SHA256 桶分配在重复调用与 DB 重开后严格确定；非法 share 拒绝且无副作用；未配灰度时默认回退 stable。
- **F4 (`test_scenario_f4_execution_snapshot_binding_and_collector_integration`)**：执行快照冻结与经验采集器联动：运行实例绑定版本后，后续灰度变更、晋升或回滚绝不篡改已有 Run 的冻结快照；新 Run 依最新状态路由；ExperienceCollector 准确记录绑定的版本号与哈希。
- **F5 (`test_scenario_f5_controlled_rollback_cas_and_idempotency`)**：受控回滚、CAS 并发与幂等性：回滚至历史版本停用灰度并更新 stable 指针；完整保留所有历史与血缘；完整记录审计事件；支持 `operation_id` 防重幂等；stale CAS revision 拒绝；DB 重开后状态完好。
- **F6 (`test_scenario_f6_rollback_target_validation_and_dependency_guards`)**：回滚目标校验与依赖守卫：目标版本不存在、属于其他 skill、未发布/草稿、内容哈希损坏、或运行环境缺少必要依赖时拒绝回滚；当前部署状态保持不变；历史不全标记为 UNAVAILABLE。
- **F7 (`test_scenario_f7_switch_consistency_transactional_rollback_and_fail_closed`)**：原子一致性与 Fail-Closed 保护：模拟存储异常触发事务完整回滚，无中间半切换状态；损坏的快照读取时 fail-closed 抛错，杜绝回退到未经检验的磁盘正文。
- **F8 (`test_scenario_f8_end_to_end_lifecycle_from_m4a_to_canary_promotion_and_rollback`)**：端到端完整生命周期闭环：M4a 失败修复 -> 生成 READY PASS 候选 -> 灰度准入拦截直接切流 -> 确认灰度发布 -> Run 执行灰度并采集 Episode -> 确认晋升灰度为 stable -> 紧急回滚至上个版本；旧运行保持冻结，血缘与审计链条完整。

---

### 9.5 局限性与路线下一步 (Roadmap: Milestone 5)

- **当前局限性**：
  - 分流路由基于单节点 SQLite 与本地进程内存映射，不具备分布式全局服务网格（Service Mesh）与动态外部流控网关能力；
  - 依赖校验基于当前环境显式声明的集合与命名约定，尚未接入自动化沙箱虚拟环境或容器镜像级别的前置依赖解析器；
  - 灰度指标监控依赖手动触发或单元测试模拟，尚未包含基于实时 PromQL / 统计显著性检定的自动熔断与自动回滚控制器。
- **路线下一步（Milestone 5）**：
  - **M5.1 评测 Harness 与基准矩阵**：建立多领域多模态标准化 Evaluation Harness，支持自动化冷启动评估与基准集分层管理；
  - **M5.2 工具代理人与沙箱环境隔离 (Tool Broker & Sandbox)**：引入隔离的工具执行代理与环境沙箱，实现依赖的安全隔离与动态按需加载；
  - **M5.3 三层记忆架构 (Three-tier Memory Architecture)**：整合 Episodic Memory、Semantic Memory 与 Working Memory，打通从经验采集、语义聚类、策略演化到运行路由的自洽长期演进飞轮。





---

## 10. Runtime 生命周期与 Tool Broker 统一执行 (Milestone 5a)

### 10.1 核心设计与架构原则

Milestone 5a 为 SkillForge 提供了统一的应用层运行生命周期管理与安全受控的工具调用中介（Tool Broker），阻断 Agent 直接随意执行外部动作，建立确定性的权限边界与执行预算守卫。

```mermaid
flowchart TD
    User["Agent / Task Invocation"] --> Runtime["AgentRuntime.start_run()<br/>(binds canary snapshot, purpose, budget, deadline)"]
    Runtime --> Dispatch["AgentRuntime.execute_tool()"]
    
    subgraph BudgetGuard["Atomic Budget & Lifecycle Guard"]
        CheckTerm{"Is Run Terminal?<br/>(COMPLETED/FAILED/CANCELLED/TIMED_OUT)"}
        CheckDeadline{"Has Deadline Expired?<br/>(now > deadline_ts)"}
        CheckBudget{"Budget Remaining?<br/>(consumed < budget_max)"}
    end
    
    Dispatch --> CheckTerm
    CheckTerm -->|No| CheckDeadline
    CheckTerm -->|Yes| RejTerm["REJECTED: DISPATCH_AFTER_TERMINAL"]
    CheckDeadline -->|No| CheckBudget
    CheckDeadline -->|Yes| SetTimeout["Mark TIMED_OUT & Finish Episode (outcome=unknown)"]
    CheckBudget -->|No| SetExhausted["Mark BUDGET_EXHAUSTED"]
    CheckBudget -->|Yes| ReserveTicket["Atomically Pre-consume Budget Ticket"]
    
    subgraph BrokerBoundary["Tool Broker Authorization"]
        ReserveTicket --> AllowlistCheck{"Tool in Allowlist?<br/>(application_allowlist & skill_required)"}
        AllowlistCheck -->|No| RejPerm["REJECTED: PERMISSION_DENIED (Handler Call Count = 0)"]
        AllowlistCheck -->|Yes| SchemaCheck{"Parameter Schema Valid?"}
        SchemaCheck -->|No| RejSchema["REJECTED: SCHEMA_VALIDATION_ERROR"]
        SchemaCheck -->|Yes| Exec["Execute Tool Handler (with cooperative timeout)"]
    end
    
    Exec --> Trace["Generate Signed ToolCallProvenance & Persist in SQLite"]
    Trace --> RecordCollector["Forward to ExperienceCollector"]
    
    Runtime --> Finalize["AgentRuntime.finalize_run()<br/>(verification_evidence determines outcome)"]
    Finalize --> EpisodeStore["Persist Immutable Episode (outcome: success / failure / unknown)"]
```

#### 10.1.1 运行状态与业务 Outcome 严格分离 (Status vs Outcome Separation)
- **运行终态 (`RuntimeStatus`)**：反映系统执行生命周期：
  - `RUNNING`：正在执行中；
  - `COMPLETED`：正常执行结束并完成终态收尾；
  - `FAILED`：发生未捕获的基础设施或系统层异常；
  - `CANCELLED`：调用方主动触发取消；
  - `TIMED_OUT`：超过任务截止时间或工具执行超时；
  - `BUDGET_EXHAUSTED`：工具调用次数触达上限；
  - `INTERRUPTED`：系统重启或崩溃后重开恢复的未完成运行。
- **业务结果 (`EpisodeOutcome`)**：仅包含 `success`、`failure` 与 `unknown`。
  - **核心守卫**：模型输出中的自夸文本（如“任务已完美完成，结果完全正确”）**绝对不能**作为业务成功的依据。
  - 只有外部独立的受信任验证器提供了 `independent_pass=True` 的凭证，Episode 才能标记为 `success`；无验证凭证或凭证不确定一律为 `unknown`；验证不通过则为 `failure`。

#### 10.1.2 应用层授权与技能依赖交集机制 (Permission Boundary)
- **权限正交性**：
  - 宿主应用通过 `application_allowlist` 明确授权当前运行环境所允许调用的安全工具清单；
  - 技能通过元数据中的 `dependencies` 声明所需的工具；
  - 实际执行时的有效授权清单为二者的交集：`effective_allowlist = application_allowlist & skill_required_tools`。
- **防提权保证**：技能无论如何声明高危依赖（如 `privileged_rm`），只要未在宿主应用的 `application_allowlist` 中显式允许，Broker 均即刻拦截，底层 Handler 调用次数严格保持为 0。
- **参数类型契约与敏感脱敏**：在派发前严格校验参数必填项与数据类型，并将 `token`、`key`、`secret`、`password` 等敏感字段以 `***REDACTED***` 脱敏保存，生成带哈希防伪签名的 `ToolCallProvenance`。

#### 10.1.3 预算预占与合作式超时/取消 (Budget & Cancellation)
- **拒绝计入预算**：被拦截的未授权调用或非法 Schema 调用同样计入请求预算消费，有效防止恶意或失控 Agent 疯狂试探未授权接口造成系统拒绝服务。
- **并发原子预占**：多线程并发调用时，在排他锁下预先扣减预算票据，确保在仅剩 1 次额度时，多线程并发请求最多仅有 1 次成功派发执行，其余立即返回 `BUDGET_EXHAUSTED`。
- **终态幂等性**：重复对同一 `run_id` 触发 `start_run` 或 `finalize_run` 绝不重置预算或覆盖终态 Episode；带相同 `call_id` 的工具调用直接返回缓存的执行结果；DB 重开后完整还原终态与调用痕迹。

#### 10.1.4 边界限制说明 (Current Limitations)
- **同步 Handler 协作式超时限制**：当前超时检测基于调用前时间戳校验与异步超时封装。若底层第三方工具直接调用了阻塞性的死循环 C 扩展或挂起的系统调用，在不具备进程级强杀抢占能力的单 Python 进程内无法强制中断正在卡死的同步函数。这需要在后续 M5b 引入子进程/容器隔离沙箱来彻底解决。

---

### 10.2 API 使用示例 (API Usage)

```python
from pathlib import Path
from skillforge import (
    ToolBroker, AgentRuntime, ExperienceCollector, EpisodeStore, DeploymentManager
)
from hello_agents.tools import Tool, ToolParameter, ToolResponse

# 1. 注册工具与定义应用安全白名单
class CalcTool(Tool):
    def __init__(self):
        super().__init__(name="calculator", description="Basic math")
    def get_parameters(self):
        return [
            ToolParameter(name="a", type="integer", required=True, description="Operand A"),
            ToolParameter(name="b", type="integer", required=True, description="Operand B"),
        ]
    def run(self, parameters):
        return ToolResponse.success(text="Result: 42", data={"result": 42})

broker = ToolBroker(application_allowlist={"calculator"})
broker.register_tool("calculator", CalcTool())

# 2. 初始化 Runtime
db_path = Path("skillforge.db")
ep_store = EpisodeStore(db_path)
collector = ExperienceCollector(episode_store=ep_store)
runtime = AgentRuntime(db_path=db_path, tool_broker=broker, collector=collector)

# 3. 启动生命周期受控的运行实例（Run）
run_rec = runtime.start_run(
    run_id="run_2026_001",
    task_id="task_calc",
    skill_name="math_skill",
    purpose="learning",
    budget_max=3,
)

# 4. 通过 Broker 统一受控执行工具（自动脱敏、签名与计费）
tool_rec = runtime.execute_tool(
    run_id="run_2026_001",
    tool_name="calculator",
    parameters={"a": 10, "b": 32, "api_key": "secret_xyz"},
)
print(f"Tool status: {tool_rec.status}, Output: {tool_rec.output_data}")

# 5. 终态收尾：业务 Outcome 严格依独立评测凭据决定
run_term, ep = runtime.finalize_run(
    run_id="run_2026_001",
    model_output="Result is 42",
    verification_evidence={"checker": "math_verifier", "independent_pass": True},
)
print(f"Terminal run status: {run_term.status}, Episode outcome: {ep.outcome}")
```

---

### 10.3 验收场景 G1 - G8 覆盖说明 (Verification Evidence)

在 `tests/test_runtime_and_tool_broker.py` 中全量覆盖并通过以下 8 项验收场景：

- **G1 (`test_g1_allowed_tool_and_outcome_separation`)**：正常允许工具调用执行一次，持久化至 `runtime_tool_calls` 并生成带签名凭证的 Episode；独立验证通过产出 `outcome='success'`，仅凭模型自夸输出在无凭据时严格判定为 `unknown`。
- **G2 (`test_g2_unauthorized_tool_and_schema_validation`)**：未授权工具、未知工具以及参数类型/必填项校验失败均被 Broker 拦截；底层 Handler 调用次数严格为 0；技能声明依赖无法单方面突破应用层白名单。
- **G3 (`test_g3_budget_enforcement_and_concurrency`)**：严格预算上限控制；第 3 次调用在派发前被拒绝 (`BUDGET_EXHAUSTED`)；被拒绝的尝试同样消耗请求配额；并发争抢单张余票最多仅允许 1 次执行；终态后拒绝任何派发。
- **G4 (`test_g4_timeout_deadline_and_cancellation`)**：任务截止时间超时拒绝执行并转入 `TIMED_OUT`；显式调用 `cancel_run` 转入 `CANCELLED` 并持久化单条终态 Episode (`outcome='unknown'`)；取消后迟到的结果被安全忽略。
- **G5 (`test_g5_idempotency_and_db_reopen`)**：重复调用 `start_run` 或 `finalize_run` 绝不重置预算或复制 Episode；相同 `call_id` 命中幂等缓存且不重复触发底层工具；数据库关闭并重开后完整保留终态与所有调用痕迹。
- **G6 (`test_g6_canary_routing_and_snapshot_binding`)**：运行实例启动时冻结绑定灰度版本（v2）快照；运行中途部署发生回滚或下线时，正在运行的实例继续执行 v2 冻结正文并在 Episode 中记录 v2；新启动的 Run 正确路由至最新部署（v1）。
- **G7 (`test_g7_adversarial_injection_and_purpose_isolation`)**：工具返回文本中包含的高危对抗性越狱与系统指令覆盖载荷绝对无法修改 Runtime 策略、配额或验证结果；`purpose='evaluation'` 与 `purpose='learning'` 的运行在 EpisodeStore 中严格隔离。
- **G8 (`test_g8_e2e_runtime_broker_to_mining_and_promotion`)**：小闭环端到端集成：Runtime + Broker 产生 ≥ 3 条带工具凭证的 learning Episode，成功触发 M3b 批量挖掘 (`mine_pending`) 生成候选并在 M2 沙箱中验证通过，验证后未获显式确认前绝不上线至 active 注册表。

---

### 10.4 局限性与路线下一步 (Roadmap: Milestone 5b & 5c)

- **当前局限性**：
  - 工具在中介层共享进程内存执行，缺少操作系统级别的 cgroups / 容器 / 虚拟子进程资源隔离；
  - 依赖校验基于当前 Python 运行时的 import 可用性，缺少针对 `pip` / 原生二进制环境的按需构建与沙箱挂载能力。
- **路线下一步**：
  - **Milestone 5b: Sandbox 执行隔离与物理依赖验证**：引入 Subprocess / Container 级沙箱环境，隔离第三方工具执行副作用，拦截未隔离的文件系统与网络读写；
  - **Milestone 5c: 三层记忆责任边界 (Semantic / Episodic / Procedural)**：明确区分 Semantic Memory（长期结构化知识与领域事实）、Episodic Memory（真实执行时序回放与调用凭证）与 Procedural Memory（沉淀演化的可执行技能），建立三层记忆相互验证、分级归档与检索演进闭环（而非单纯以临时 Working Memory 替代可执行 Procedural 技能）。

---

## 11. Sandbox 执行隔离与物理依赖验证 (Milestone 5b)

### 11.1 核心设计与隔离架构 (Sandbox Architecture & Boundaries)

Milestone 5b 沿 M5a `ToolBroker` 接入了真实的操作系统级沙箱执行后端（`MacSeatbeltSandbox`，基于 macOS `/usr/bin/sandbox-exec`），对需要独立子进程运行的应用工具实施物理级文件、网络、进程树与环境变量隔离，同时保持应用层白名单、Schema 校验、调用预算、版本绑定与经验采集链条不被绕过。

```mermaid
flowchart TD
    Invoke["Broker.dispatch() / Runtime.execute_tool()"] --> AllowCheck{"Tool in Allowlist?"}
    AllowCheck -->|No| RejPerm["REJECTED: PERMISSION_DENIED"]
    AllowCheck -->|Yes| SchemaCheck{"Schema Valid & No Tamper Key?"}
    SchemaCheck -->|No| RejSchema["REJECTED: SCHEMA_VALIDATION_ERROR"]
    SchemaCheck -->|Yes| IsSandboxed{"Tool Registered as Sandboxed?"}
    
    IsSandboxed -->|No| InProcess["Execute In-process (preserves M5a behavior)"]
    IsSandboxed -->|Yes| BackendCheck{"Sandbox Backend Available?<br/>(Fail-Closed Guard)"}
    
    BackendCheck -->|No| FailClosed["REJECTED: SANDBOX_UNAVAILABLE<br/>(Host Handler Call Count = 0)"]
    BackendCheck -->|Yes| ProbeCheck{"Dependency Probe Passed in Sandbox?"}
    
    ProbeCheck -->|No| RejDep["REJECTED: DEPENDENCY_MISSING<br/>(Host Handler Call Count = 0)"]
    ProbeCheck -->|Yes| SetupSB["Generate Ephemeral Workspace & Seatbelt Profile"]
    
    subgraph SeatbeltIsolation["macOS Seatbelt Kernel Isolation"]
        Profile["(deny network*)<br/>(deny file-write*)<br/>(allow file-write* subpath workspace)<br/>(deny file-read* denied_paths)"]
        Spawn["subprocess.Popen(start_new_session=True)<br/>Scrubbed Env (no secrets)"]
        TreeKill["Process Tree Graceful Termination<br/>(SIGTERM -> SIGKILL)"]
        Truncate["Output Max Bytes Hard Cap<br/>(... [TRUNCATED])"]
    end
    
    SetupSB --> Profile --> Spawn --> TreeKill --> Truncate
    Truncate --> Record["ToolCallRecord with Backend, Fingerprint & Signed Provenance"]
```

#### 11.1.1 隔离保障范围 (What is Guaranteed)
1. **工作区写隔离**：为单次执行动态分配临时工作区 `workspace_dir`，内核规则仅允许写入工作区自身和 `/dev` 设备，任何试图通过绝对路径、`../` 相对路径或符号链接逃逸写外部宿主哨兵文件（Sentinel）的操作均被内核强制阻断（`PermissionError`），宿主文件保持未修改。
2. **只读访问拦截**：支持通过 `denied_read_paths` 显式注入禁止访问的敏感路径，违规读取在底层直接返回拒绝。
3. **默认网络完全关断**：通过 `(deny network*)` 关断进程所有套接字创建和网络连接能力，连接本地环回或外部端口均产生系统级拒绝，防止数据外溢。
4. **环境变量清洗**：仅继承极少数无害系统变量（`PATH`、`TMPDIR`、`LANG` 等），包含 `SECRET`、`KEY`、`TOKEN`、`PASSWORD`、`AUTH` 等特征的宿主凭据被强力过滤，不流入沙箱执行单元。
5. **真实进程组回收**：通过 `start_new_session=True` 建立全新进程组。遇超时或主动取消时，先发送 `SIGTERM` 并在宽限期后发送 `SIGKILL` 强杀整棵进程树，防止后台孤儿任务继续占用计算资源。
6. **输出大小硬上限**：超过 `max_output_bytes` 的输出在字符边界硬截断并追加 `... [TRUNCATED]` 标记，防范日志拒绝服务攻击。
7. **严格 Fail-Closed**：若操作系统缺少沙箱工具或探针检测失败，工具派发立即拒绝，底层 Handler 调用次数严格为 0，绝对不静默回退为裸宿主进程执行。
8. **防提权参数校验**：工具入参通过 Broker Schema 严格校验，任何以下划线开头的提权或参数篡改字段（如 `_allow_network`, `_mount_path`）均在派发前被直接拒绝。

#### 11.1.2 不保障范围与工程边界说明 (What is NOT Guaranteed)
- **非跨平台通用框架**：当前后端选用 macOS 原生提供的 Seatbelt (`/usr/bin/sandbox-exec`)，在 Linux 或 Windows 系统上需要对应的内核机制（如 Linux Bubblewrap/cgroups 或 Windows AppContainer），本项目不额外造跨平台庞大抽象层；
- **非硬件级虚拟机**：Seatbelt 为操作系统内核级进程沙箱，并非物理隔离虚拟机（VM），不能防御底层硬件微架构侧信道漏洞或内核级 0-day 漏洞提权；
- **系统共有只读依赖共享**：在 `(allow default)` 基础策略下，沙箱内可访问系统共享的标准库、二进制命令（如 `/bin/sh`, `/usr/bin/python3`）及只读依赖文件。

---

### 11.2 实际依赖验证机制 (In-Sandbox Dependency Probe)

```mermaid
flowchart LR
    Spec["Declared Tool / Version Dependency"] --> Probe["DependencyProbe.probe_dependency()"]
    Probe --> FPCheck{"Fingerprint Changed?"}
    FPCheck -->|Yes| Invalidate["Invalidate Cache"] --> RunProbe
    FPCheck -->|No| CacheCheck{"Hit Cache?"}
    CacheCheck -->|Yes| ReturnCache["Return Cached ProbeResult"]
    CacheCheck -->|No| RunProbe["Execute Safe Probe Script INSIDE Sandbox"]
    
    RunProbe --> CheckType{"Dependency Type"}
    CheckType -->|Module| PyImport["importlib.util.find_spec() & Version Compare"]
    CheckType -->|Binary| Which["shutil.which() in PATH"]
    
    PyImport --> Result["Structured ProbeResult(satisfied, detected_version, error_reason)"]
    Which --> Result
    
    Result --> RollbackGate{"M4b Rollback Gate"}
    RollbackGate -->|Unsatisfied| BlockRollback["Block Rollback with ValueError<br/>(Deployment untouched)"]
    RollbackGate -->|Satisfied| PassRollback["Allow Rollback to Target Version"]
```

- **同一隔离环境探针**：探针不在宿主环境跑，而是将轻量安全的无副作用探测脚本注入同一沙箱中执行，检验模块能否真正被 import 或二进制是否存在于隔离环境中；
- **版本区间判定**：支持解析 `python3>=3.10`、`bin:git`、`json`、`yaml>=5.0` 等标准语义，输出检测到的物理版本；
- **环境指纹失效**：依据后端名称、操作系统类型、Python 解释器版本、工作区基准与依赖特征计算 SHA256 环境指纹 (`environment_fingerprint`)。一旦环境参数或依赖规格变化，历史缓存即刻失效并重新执行真实物理探测；
- **与 M4b 受控回滚深度集成**：`DeploymentManager.rollback_deployment` 接收 `dependency_prober` 参数。当目标版本声明了未就绪或损坏的依赖（如 `missing_ml_lib_v9`）时，回滚事务在提交前被即刻阻断并抛出 `ValueError`，现有部署版本与灰度比例完整保持不变。

---

### 11.3 API 使用示例 (API Usage)

```python
from pathlib import Path
import sys
from skillforge import (
    ToolBroker, AgentRuntime, MacSeatbeltSandbox, SandboxedToolSpec,
    DependencyProbe, DeploymentManager, ExperienceCollector, EpisodeStore,
    ToolParameter
)

# 1. 实例化真实的 macOS Seatbelt 沙箱后端与探针
sandbox = MacSeatbeltSandbox()
assert sandbox.is_available(), "macOS /usr/bin/sandbox-exec must be present"
prober = DependencyProbe(backend=sandbox)

# 2. 注册可子进程沙箱执行的工具
broker = ToolBroker(
    application_allowlist={"workspace_file_processor"},
    sandbox_backend=sandbox,
    dependency_prober=prober,
)

tool_script = (
    "import sys, json\n"
    "params = json.load(sys.stdin)\n"
    "filename = params['filename']\n"
    "data = params['data']\n"
    "with open(filename, 'w') as f: f.write(data)\n"
    "with open(filename, 'r') as f: content = f.read()\n"
    "print(json.dumps({'status': 'ok', 'bytes': len(content), 'read_back': content}))\n"
)

broker.register_sandboxed_tool(
    SandboxedToolSpec(
        name="workspace_file_processor",
        command_template=[sys.executable, "-c", tool_script],
        description="Processes files strictly within the isolated sandbox workspace",
        parameters=[
            ToolParameter(name="filename", type="string", description="File name", required=True),
            ToolParameter(name="data", type="string", description="File content", required=True),
        ],
        required_dependencies=["json"],
        timeout_seconds=5.0,
        max_output_bytes=1024,
    )
)

# 3. 运行 AgentRuntime，受控执行工具
db_path = Path("skillforge.db")
ep_store = EpisodeStore(db_path)
collector = ExperienceCollector(episode_store=ep_store)
runtime = AgentRuntime(db_path=db_path, tool_broker=broker, collector=collector)

runtime.start_run(run_id="run_sb_001", task_id="task_proc", skill_name="file_processor")
rec = runtime.execute_tool(
    run_id="run_sb_001",
    tool_name="workspace_file_processor",
    parameters={"filename": "local.txt", "data": "Sandboxed Execution OK"},
)

print(f"Status: {rec.status}")
print(f"Backend: {rec.output_data.get('backend')}")
print(f"Exit code: {rec.output_data.get('exit_code')}")
print(f"Read back: {rec.output_data.get('read_back')}")

# 4. 终态结转生成经验（附带沙箱环境指纹）
final_run, ep = runtime.finalize_run(
    run_id="run_sb_001",
    verification_evidence={"independent_pass": True, "evidence": "Verified locally in sandbox"},
)
print(f"Episode outcome: {ep.outcome}, Fingerprint: {ep.environment.get('environment_fingerprint')}")
```

---

### 11.4 验收场景 H1 - H8 覆盖说明 (Verification Evidence)

在 `tests/test_sandbox_execution.py` 中全量覆盖并通过以下 8 项验收场景：

- **H1 (`test_h1_sandboxed_tool_execution_via_broker_and_runtime`)**：现有执行入口 -> M5a Broker -> 选定实际 macOS Seatbelt 后端运行受控工具，在沙箱工作区读写成功；原 allowlist/schema/预算全面生效；trace 与 Episode 记录正确的 `backend='macos_seatbelt'`、环境指纹、exit code 与技能版本。
- **H2 (`test_h2_host_sentinel_protection_and_restricted_read_denial`)**：在临时工作区外放置外部宿主哨兵文件，尝试绝对路径、`../` 相对路径与符号链接越界写入，均被内核规则拦截且宿主哨兵内容绝对保持不变；明确禁止的外部读取配置同样被拒绝。
- **H3 (`test_h3_network_isolation_secret_scrubbing_and_policy_tamper_denial`)**：默认网络隔离下，沙箱内尝试连接本地已监听的测试 TCP 端口失败；宿主环境变量中的秘密 Token 被强力过滤；入参试图通过下划线字段篡改策略或扩权被即刻拒绝。
- **H4 (`test_h4_lifecycle_timeout_cancellation_and_output_truncation`)**：正常退出、超时与显式取消均走真实系统生命周期；子进程超时或取消后进程树被完整收割终止；终态后拒绝继续写入或派发；输出达到硬上限时正确截断并追加 `... [TRUNCATED]`。
- **H5 (`test_h5_actual_dependency_probe_in_sandbox`)**：在同一沙箱内进行真实物理依赖探针；已安装依赖通过，缺失依赖或版本不匹配在进入业务 Handler 之前被结构化拒绝 (`DEPENDENCY_MISSING`)，底层宿主 Handler 调用计数严格为 0。
- **H6 (`test_h6_fingerprint_invalidation_and_m4b_rollback_integration`)**：环境或配置指纹变更后旧探测缓存即刻失效；M4b 受控回滚接入物理探针，回滚至依赖不可用的历史版本被拒且部署状态完全不变，合法版本正常通过回滚。
- **H7 (`test_h7_fail_closed_on_missing_backend_no_fallback`)**：沙箱后端缺失或不可用时实施 Fail-Closed 拦截，宿主 Handler 调用计数为 0，绝对不静默回退至裸宿主进程；上下游 Runtime 准确记录终态与 Episode，不伪造成功。
- **H8 (`test_h8_end_to_end_runtime_sandbox_probe_and_episode_learning_pool`)**：端到端完整闭环：Runtime + Broker + Sandbox + Probe 协同，受控工具成功执行并获得独立验证凭据生成 `outcome='success'` 的 learning Episode 汇入挖掘池；被拒或失败的运行产出失败记录并被排除在正向模式挖掘池外。

---

### 11.5 局限性与路线下一步 (Roadmap: Milestone 5c)

- **当前局限性**：
  - 沙箱基于单节点 macOS Seatbelt 机制，针对 Linux 部署环境尚需在未来适配基于 OCI 容器（如 runc）或 Linux 命名空间（Bubblewrap）的对应隔离实现；
  - 依赖探测当前支持模块导入和基础命令可执行性检测，尚未支持全自动的隔离环境虚拟环境构建（如在容器内动态挂载独立的 wheel 包）。
- **路线下一步（Milestone 5c）**：
  - **Milestone 5c: Semantic 事实 / Episodic 经历 / Procedural 技能 三层记忆责任边界**：已于第 12 节全面落地实现。

---

## 12. Milestone 5c: Semantic 事实、Episodic 经历与 Procedural 技能三层记忆边界 (Three-Tier Memory Architecture)

### 12.1 概述与设计边界 (Overview & Architecture Boundaries)
Milestone 5c (M5c) 为 SkillForge 确立了**语义事实 (Semantic)、偶发经历 (Episodic) 与程序技能 (Procedural)** 三层记忆的清晰责任边界与来源溯源（Provenance Linking）：
- **Semantic Memory (语义事实)**：
  - 存储附带来源标识 (`source_id`) 与上下文作用域 (`scope`) 的事实与观测；
  - ID 采用强类型前缀 `fact_`；
  - **无条件事实防冒名守卫**：单次执行产生的观测（来源为 `run_...`、`ep_...` 或带有 `single_execution` 标记）严禁静默标记为全域无条件事实 (`is_universal=True`)，强行标记抛出 `ValueError`；
  - **冲突保留而非静默裁决**：当不同来源对同一主题/属性（`topic`）观测到不同结果时，系统完整保留所有观测条目，并通过 `detect_conflicts()` 显式暴露冲突 (`SemanticConflict`)，杜绝静默覆盖或盲目裁决。
- **Episodic Memory (经历记忆)**：
  - 承载不可变的单次执行经历（`Episode`），由既有执行入口（`AgentRuntime` + `ToolBroker` + `ExperienceCollector`）产生；
  - ID 采用强类型前缀 `ep_`；
  - 严格记录 `task_id`、创建时间戳、技能版本、验证结果（`outcome`）与工具调用凭证（`provenances`）；
  - **成败明确区分**：成功与失败经历严格界定。失败经历（`outcome='failure'`）绝不被当作无条件事实，也不得直接晋升为正式技能。
  - **存储不可变性**：已持久化的 `episode_id` 拒绝二次覆盖修改。
- **Procedural Memory (程序技能)**：
  - 承载可执行的候选技能（`CandidateSkill`，ID 前缀 `cand_`）与已发布正式技能（`SkillRegistry` / `DeploymentManager`）；
  - **受控挖掘与晋升守卫**：候选技能必须经过沙箱验证棘轮门禁（`ratchet_decision='PASS'`）且必须获得调用方显式确认（`caller_confirmed=True`）方可发布；未经验证或被拒绝的候选严禁进入正式库；
  - 终身保留与支撑该技能的源经历（`source_episode_ids`）与版本脉络关联。
- **数据流向隔离守卫**：
  - **A8 评测目的隔离**：`purpose='evaluation'` 的评测数据严禁回流到候选挖掘或程序性技能学习回路；
  - **失败经验不制造成功程序**：纯失败经历不得用来合成全新的可执行技能。
- **类型分明检索与跨层来源追溯**：
  - `ThreeTierMemoryManager.search(query, tier=...)`：按层检索完全隔离，类型与 ID 前缀分明，绝不混淆；
  - `ThreeTierMemoryManager.trace_lineage(procedural_id)`：打通 Procedural 候选 -> 支撑 Episodic 经历 -> 原始底层 Semantic 事实与观测的完整双向溯源链。

### 12.2 三层记忆对比与责任边界表 (Memory Tiers Comparison & Boundary Matrix)

| 记忆层 (Tier) | 数据模型 | ID 规范 | 存储介质 | 写入条件与守卫 | 读取与检索方式 | 冲突/成败处理规则 |
| :--- | :--- | :--- | :--- | :--- | :--- | :--- |
| **Semantic** (语义事实) | `SemanticFact` | `fact_...` | SQLite `semantic_facts` | 必须提供 `source_id` 与 `scope`；单次运行观测禁止标记 `is_universal=True` | `list_facts(source_id, scope, topic)`；`search(..., tier='semantic')` | 矛盾观测共存保留，通过 `detect_conflicts` 显式呈现，不静默覆盖 |
| **Episodic** (执行经历) | `Episode` | `ep_...` | SQLite `episodes` | 必须由真实 Runtime/Collector 产生并附带独立检验凭据；持久化后只读不可变 | `get_episode(id)`；`list_episodes(...)`；`search(..., tier='episodic')` | 成功/失败严格分离；`evaluation` 严禁回流学习；失败经历不当作事实或技能 |
| **Procedural** (程序技能) | `CandidateSkill` / `VersionSnapshot` | `cand_...` / SemVer | SQLite `candidate_skills` + `skills/` | 必须关联 `source_episode_ids`；发布须棘轮 `PASS` 且 `caller_confirmed=True` | `list_candidates()`；`get_meta()`；`search(..., tier='procedural')` | 未验证或被拒候选阻断晋升；不直接由失败经历合成新技能 |

### 12.3 API 使用示例 (API Usage)

```python
from pathlib import Path
from skillforge import (
    SemanticStore, SemanticFact, EpisodeStore, CandidateStore,
    ThreeTierMemoryManager, SkillRegistry
)

db_path = Path("skillforge.db")
mem_mgr = ThreeTierMemoryManager(db_path=db_path)

# 1. 语义事实录入（附带来源与作用域）
fact = SemanticFact(
    fact_id="fact_db_timeout_east",
    statement="Database connection timeout is 15 seconds",
    source_id="run_perf_probe_001",
    scope="vpc_east",
    topic="db_timeout",
)
mem_mgr.semantic_store.save_fact(fact)

# 2. 跨层分级检索（类型严格隔离，绝不混淆）
semantic_hits = mem_mgr.search("timeout", tier="semantic")  # list[SemanticFact]
episodic_hits = mem_mgr.search("timeout", tier="episodic")  # list[Episode]
procedural_hits = mem_mgr.search("timeout", tier="procedural")  # list[CandidateSkill | VersionSnapshot]

# 3. 来源脉络全链路追溯（候选 -> 经历 -> 原始事实）
lineage = mem_mgr.trace_lineage("cand_timeout_handler_01")
print(f"Candidate: {lineage.procedural_id}")
print(f"Supporting Episodes: {[e.episode_id for e in lineage.supporting_episodes]}")
print(f"Source Facts: {[f.fact_id for f in lineage.source_facts]}")

# 4. 冲突观测显式暴露
conflicts = mem_mgr.detect_conflicts(topic="db_timeout")
for c in conflicts:
    print(f"Conflict on {c.topic}: {c.description}")
```

### 12.4 验收场景 C1 - C5 覆盖说明 (Verification Evidence)

在 `tests/test_three_tier_memory.py` 中全量覆盖并通过以下 5 项验收场景：

- **C1 (`test_scenario_c1_semantic_fact_access_and_source_lineage`)**：写入至少 2 条包含 source_id 与 scope 的事实；单次执行结果（`run_...`、`ep_...` 或带 `single_execution` 标记）严禁标记为无条件普遍事实；按 source_id/scope 查询正确过滤并保留来源；强制校验 `fact_` 前缀。
- **C2 (`test_scenario_c2_episodic_immutability_and_success_failure_separation`)**：由既有执行入口（`AgentRuntime` + `ToolBroker` + `ExperienceCollector`）分别生成 1 条成功与 1 条失败经历；校验持久化不可变性（覆写报错）；校验 task_id、时间、版本、结果与工具调用凭证；失败记录明确标记为 failure，绝不被当作事实或注册进正式技能库。
- **C3 (`test_scenario_c3_procedural_generation_and_controlled_promotion`)**：基于学习经历挖掘生成 `CandidateSkill`；未验证或 ratchet 判定 `DECLINED` 严格阻断；`caller_confirmed=False` 阻断晋升；仅在通过验证门禁且调用方显式确认后方可晋升为正式 Skill；候选保留与支撑经历的完整关联。
- **C4 (`test_scenario_c4_three_tier_memory_boundaries_and_lineage_tracing`)**：构造相同关键词在 Semantic、Episodic、Procedural 各自存在的数据；按类型读取互不混淆，ID 前缀（`fact_`、`ep_`、`cand_`）与类型分明；`trace_lineage` 从程序性候选向上完整追溯至支撑它的经历及原始事实。
- **C5 (`test_scenario_c5_isolation_and_conflict_preservation`)**：`purpose='evaluation'` 经历尝试回流学习被结构化拦截（抛出 `ValueError`）；纯失败经历不得用来合成新的程序技能；对同一属性存在不同来源的相互矛盾观察时，两者在存储中完整共存，并通过 `detect_conflicts` 显式呈现冲突，绝不静默覆盖或偏袒裁决。

---

### 12.5 架构全景与未来规划 (Architecture Status & Future Roadmap)
至此，SkillForge 已完整落地：
1. **M1**: Episode 与 CandidateSkill 数据规范、独立验收证据校验与存储隔离；
2. **M2**: 演化回路受控挖掘、内容哈希防篡改与棘轮门控（PASS + caller_confirmed 晋升）；
3. **M3a**: ExperienceCollector 自动化经验采集、评测与学习隔离、签名凭据；
4. **M3b**: 经验池模式挖掘（mine_pending）、启发式聚类、去重与幂等记账；
5. **M4a**: 失败责任归因（6 大责任层）、有界修补与防退化回归；
6. **M4b**: 版本对比、安全原子回滚与受控灰度路由（Canary）；
7. **M5a**: AgentRuntime 生命周期管理、Tool Broker 权限与预算拦截；
8. **M5b**: macOS Seatbelt 沙箱后端执行隔离与物理依赖探针；
9. **M5c**: Semantic / Episodic / Procedural 三层记忆边界确立、ID 隔离、来源全链路追溯与冲突保真；
10. **Phase P2**: Document → Skill 候选输入扩展、不可信数据安全隔离、片段级行号哈希锚定与版本修订独立性。

---

## 13. Document → Skill 候选输入扩展与全链路来源追溯 (Phase P2)

### 13.1 概述与设计边界 (Overview & Architecture Boundaries)

Phase P2 在 M1–M5c 的基础上，扩展了候选技能（CandidateSkill）的输入来源，允许将本地规范纯文本/Markdown 文档转化为可操作的技能草稿，复用既有候选、验证、晋升、版本快照与三层记忆链路，同时严密恪守以下六大设计边界：

1. **本地纯文本/Markdown 优先，零新增外部依赖**：
   - 仅支持应用显式提供的本地 Markdown 与纯文本输入及元数据；
   - 不引入任何外部 PDF 解析、OCR、多模态模型、远程爬虫或云文档服务；全量沿用 Python 标准库与 SQLite 机制。
2. **独立强类型来源实体 (Independent Typed Source)**：
   - 文档作为独立强类型实体 `DocumentSource`（ID 前缀 `doc_`）以及片段实体 `DocumentSnippet`（ID 前缀 `snip_`）存储在 `document_sources` 与 `document_snippets` 表中；
   - 记录精确 1-indexed 行号范围（`start_line`, `end_line`）与 SHA-256 内容哈希；
   - **绝不伪造执行 Episode**：文档导入与解析绝不在 `episodes` 表中伪造任何虚假执行记录（`source_episode_ids` 显式为空列表 `[]`）。真实的执行经历只能由真实执行产生。
3. **不可信数据安全边界 (Untrusted Data Boundary)**：
   - 文档正文被视为不可信的外部输入，绝无权限直接分配或提升工具权限、执行任意宿主命令、修改沙箱配置、挂载文件系统、配置网络或自发触发发布；
   - 内置对抗提示词过滤机制（`_filter_adversarial_claims`），自动剥离并审计诸如 `skip verification`、`grant sudo`、`disable sandbox`、`direct publish` 等特权逃逸指令；
   - 提取生成的技能实体恒为 `status='DRAFT'`，绝无可能绕过验证自动入库生效。
4. **幂等导入与版本修订独立性 (Idempotency & Revision Independence)**：
   - 重复导入相同 `(doc_id, version)` 具有严格幂等性，复用既有草稿候选，不产生冗余数据；
   - 文档版本更新（如从 `1.0.0` 修订至 `2.0.0`）生成独立的 `DocumentSource` 记录和全新 `DRAFT` 候选，既有旧版候选及已发布的历史快照保持不可变；
   - 新版本候选必须经过独立的全新物理验证，严禁直接套用旧版本的验证凭据或评测结论。
5. **步骤质量守卫与冲突显式保真 (Step Quality & Conflict Preservation)**：
   - 当文档包含相互矛盾的可操作步骤（如既要求写入某文件又严禁写入该文件）时，返回 `status='conflict'`，拒绝生成不可靠候选，原始冲突片段完整保留供用户审阅；
   - 当文档步骤含糊不清（如出现缺乏可操作性的模糊表述）或显式声明缺失前置依赖时，返回 `status='rejected'` 并记录结构化拒由；
   - 杜绝为了“看起来能用”而强行拼凑草率的不可行技能。
6. **真实工具验证与调用方显式确认 (Real Tool Verification & Caller Confirmation)**：
   - 候选技能必须在真实受控工具（如通过 `ToolBroker` 注册的执行工具）中执行验证，产生真实的验证经历 `Episode`（`verification_episode_ids`）；
   - 验证失败（`ratchet_decision != 'PASS'`）绝对无法晋升；
   - 验证通过后仍必须由调用方显式确认（`caller_confirmed=True`）方可发布；发布后 `releases` 表的 `source_lineage_json` 完整锚定文档、片段与验证经历。

---

### 13.2 数据模型与 Schema 设计 (Models & Schema Matrix)

| 数据模型 | 标识前缀 | 存储表名 | 核心属性与校验 | 来源追溯关联 |
| :--- | :--- | :--- | :--- | :--- |
| `DocumentSource` | `doc_` | `document_sources` | `(doc_id, version)` 复合主键，`title`, `content_hash`, `content`, `metadata_json` | 与多个 `DocumentSnippet` 构成版本树 |
| `DocumentSnippet` | `snip_` | `document_snippets` | `snippet_id`, `section_title`, `start_line`, `end_line`, `content_hash` | 锚定至特定文档行号与段落 |
| `CandidateSkill` (扩展) | `cand_doc_` / `cand_` | `candidate_skills` | 新增 `source_doc_id`, `source_doc_version`, `source_snippet_ids` 字段；`status='DRAFT'` | 若由文档生成，`source_episode_ids=[]` |
| `DocumentExtractionResult` | 无 | 内存传输 | `status` ('success'/'rejected'/'conflict'), `rejection_reasons`, `conflicts`, `raw_claims_filtered` | 承载提取过程的审计明细 |
| `MemoryLineage` (扩展) | 无 | 跨层查询 | 包含 `source_document: DocumentSource` 与 `source_snippets: list[DocumentSnippet]` | 实现 Procedural -> Episodic -> Semantic / Document 全链路回溯 |

---

### 13.3 核心 API 使用示例 (API Usage)

```python
from pathlib import Path
import hashlib
from skillforge import (
    ThreeTierMemoryManager,
    DocumentSource,
    parse_markdown_snippets,
    ToolBroker,
    AgentRuntime,
    ExperienceCollector,
    EpisodeStore,
    ValidationRecord,
    RatchetVerdict,
    compute_candidate_hash,
)

db_path = Path("skillforge.db")
mem_mgr = ThreeTierMemoryManager(db_path=db_path)

# 1. 结构化文档与片段解析
doc_text = """# Math Tool Procedure
Standard addition guide.

## Instructions
1. Call tool calculator with a=10 and b=20.
2. Confirm output equals 30.
"""
doc_id = "doc_math_addition"
doc_ver = "1.0.0"
snippets = parse_markdown_snippets(doc_text, doc_id=doc_id, doc_version=doc_ver)

doc = DocumentSource(
    doc_id=doc_id,
    title="Math Tool Procedure",
    version=doc_ver,
    content=doc_text,
    content_hash=hashlib.sha256(doc_text.encode("utf-8")).hexdigest(),
    snippets=snippets,
)
mem_mgr.ingest_document(doc)

# 2. 从可操作步骤提取 DRAFT 候选（无虚假 Episode）
ext_res = mem_mgr.extract_candidate_from_document(doc, target_skill_name="math_addition")
candidate = ext_res.candidate
assert candidate.status == "DRAFT"
assert candidate.source_doc_id == doc_id
assert candidate.source_episode_ids == []

# 3. 真实受控工具执行验证
broker = ToolBroker(application_allowlist={"calculator"})
# (注册真实工具后...)
ep_store = EpisodeStore(db_path)
runtime = AgentRuntime(db_path=db_path, tool_broker=broker, episode_store=ep_store)
run = runtime.start_run("run_v1", "task_v1", skill_name="math_addition", purpose="verification")
runtime.execute_tool(run.run_id, "calculator", {"a": 10, "b": 20, "op": "add"})
_, verify_ep = runtime.finalize_run(
    run.run_id,
    model_output="30",
    verification_evidence={"independent_pass": True, "result": 30},
    acceptance_criteria={"expected": 30},
)

# 4. 显式确认后受控晋升发布
val_rec = ValidationRecord(
    candidate_id=candidate.candidate_id,
    content_hash=compute_candidate_hash(candidate),
    baseline_version=None,
    ratchet_decision="PASS",
    eval_result=None,
    ratchet_verdict=RatchetVerdict(decision="PASS", reasons=["Verification execution verified"]),
    verification_episode_ids=[verify_ep.episode_id],
)
release = mem_mgr.promote_candidate(
    candidate_id=candidate.candidate_id,
    validation_record=val_rec,
    state_machine=sm,
    caller_confirmed=True,
)

# 5. 全链路追溯：同时追溯至原始文档片段与验证 Episode
lineage = mem_mgr.trace_lineage(release.release_id)
print(f"Source Document: {lineage.source_document.title} (v{lineage.source_document.version})")
print(f"Supporting Snippets: {[s.section_title for s in lineage.source_snippets]}")
print(f"Verification Episodes: {[e.episode_id for e in lineage.supporting_episodes]}")
```

---

### 13.4 验收场景 D1 - D6 覆盖说明 (Verification Evidence)

在 `tests/test_document_to_skill.py` 中全量覆盖并通过以下 6 项验收场景：

- **D1 (`test_scenario_d1_ingest_markdown_document_and_snippet_provenance`)**：本地 Markdown 文档摄取与片段来源：准确提取文档标题、版本与正文；自动切分并标记各片段的精确行号与 SHA-256 内容哈希；`SkillRegistry` 保持完全未触碰；`EpisodeStore` 保持 0 条记录，绝对不伪造虚假经历。
- **D2 (`test_scenario_d2_extract_candidate_from_operable_steps`)**：操作性步骤候选提取：从文档操作片段提取生成候选；明确关联 `source_doc_id`、`source_doc_version` 与 `source_snippet_ids`；`source_episode_ids` 严格为空；候选状态恒为 `DRAFT`；正式技能库不受影响。
- **D3 (`test_scenario_d3_candidate_verification_controlled_tool_and_promotion`)**：候选真实验证与受控晋升：使用受控真实工具执行验证；验证执行失败阻断晋升；验证通过但未经调用方显式确认（`caller_confirmed=False`）阻断晋升；显式确认后成功发布；发布实体与候选均可追溯至原始文档片段及真实验证 Episode。
- **D4 (`test_scenario_d4_adversarial_injection_isolation_and_draft_boundary`)**：对抗性注入隔离与草稿边界：文档中包含 `skip verification`、`grant sudo`、`disable sandbox`、`direct publish` 等特权逃逸指令时，指令被有效过滤并留痕审计；候选严格保持 `DRAFT` 状态；不产生任何越权或自发发布副作用。
- **D5 (`test_scenario_d5_idempotency_and_revision_independence`)**：幂等性与版本修订独立性：重复导入相同版本幂等复用；新版本修订（v2.0）生成独立的文档源与全新 `DRAFT` 候选，旧版候选及已发布的历史快照保持不可变；新版候选必须重新执行独立验证，不得盗用旧版验证记录。
- **D6 (`test_scenario_d6_quality_guardrails_contradiction_vagueness_and_isolation`)**：质量守卫（矛盾、含糊、前置缺失）与失败隔离：相互矛盾的操作步骤返回 `conflict` 状态；模糊无具体操作或显式声明缺失依赖的文档返回 `rejected` 状态；所有拒绝均完整保留片段供排查；非同源单次成功 Episode 严禁用于伪造文档验证或绕过发布门禁。

---

## 14. Phase P3: Future Retrieval / Memory 检索 (Task-Context Memory Retrieval)

### 14.1 核心设计与架构边界 (Overview & Architectural Invariants)

Phase P3 在 M1–M5c 与 P2 Document→Skill 的坚实基础上，为未来任务提供了**有界、只读、任务上下文感知**的统一记忆检索入口（`FutureMemoryRetriever` 与 `ThreeTierMemoryManager.retrieve()`），帮助未来任务发现可复用的正式 Skill 及其支撑证据。

```mermaid
flowchart TD
    Task["Future Task Request<br/>(query, task_id, run_id, scope, allowed_tools, dependencies)"] --> Ret["FutureMemoryRetriever.retrieve()"]
    
    subgraph ContextGuards["Read-Only Context Guards"]
        Binding["1. Task Version Binding<br/>(run_version_bindings check)"]
        ActiveDep["2. Active Deployment State<br/>(rejects rolled-back versions)"]
        PermGuard["3. Tool Permission & Dep Check<br/>(allowed_tools & available_dependencies)"]
        IsoGuard["4. Eval & Failure Isolation<br/>(rejects evaluation & failure as positive evidence)"]
        DraftGuard["5. Candidate Status Guard<br/>(DRAFT candidates cannot be formal skills)"]
    end
    
    Ret --> ContextGuards
    ContextGuards --> Scoring["Deterministic Multi-Word Scoring<br/>(exact name, terms, keywords, metadata, body)"]
    Scoring --> Provenance["Lineage & Conflict Resolution<br/>(trace_lineage: flags broken chains & surfaces fact conflicts)"]
    
    Provenance --> Result["FutureRetrievalResult<br/>(skills, evidence_episodes, evidence_facts, conflicts, filtered_out, empty_reason)"]
```

#### 核心不变式与责任边界：
1. **只读保证与零副作用 (Read-Only & Zero Side-Effects)**：
   - 检索过程绝对不调用底层外部工具，不修改 `SkillRegistry`，不晋升候选，不切换版本配置，不写入任何新 `Episode`；
   - 检索返回的是可审查的正式技能建议（`SkillRecommendation`）与事实凭证，后续执行必须经由 `AgentRuntime`、`ToolBroker`、沙箱及独立验证门禁。
2. **三层记忆类型与 ID 严密保持 (Strict Tier Typing & ID Integrity)**：
   - 单次成功经历 `Episode`（`ep_...`）绝非正式可执行技能；
   - 未经验证确认的 `CandidateSkill`（`cand_...`，状态为 `DRAFT` 或 `REJECTED`）严禁冒充正式技能出现在推荐列表；
   - 来源链严格按真实链路追溯：文档链为 `DocumentSnippet -> Candidate -> 验证 Episode -> 正式版本`；挖掘链为 `Episode -> Candidate -> 正式版本`；绝不伪造虚假来源。
3. **任务上下文感知与安全权限过滤 (Task Context & Permission Filtering)**：
   - **版本固定感知**：若任务 `run_id` 在 M4b `run_version_bindings` 中已冻结版本，检索严格返回该冻结版本；
   - **失效/回滚防护**：已回滚或非活跃版本绝不作为新任务的可执行推荐；
   - **权限与依赖拦截**：技能若声明超出调用方 `allowed_tools` 的工具或缺失 `available_dependencies`，直接结构化拦截并记录于 `filtered_out`，严禁发出越权建议。
4. **评测与失败隔离守护 (Evaluation & Failure Isolation)**：
   - `purpose='evaluation'` 的评测数据绝对不作为学习或推荐的正面证据；
   - 失败经历（`outcome='failure'`）绝对不能记作成功证据；
   - 仅真实验证通过且为学习/验证目的的成功经历可作为正向凭证。
5. **断链与冲突显式保真 (Explicit Broken Lineage & Conflict Preservation)**：
   - 若候选或技能引用的支撑经历或源文档在底层存储中缺失/损坏，显式标记 `lineage_broken=True` 并输出缺失原因，杜绝凭空杜撰；
   - 存在互相矛盾的语义事实时，保留各自来源（`source_id`）并通过 `conflicts` 显式呈现，杜绝静默裁决。
6. **确定性多词打分与有界输出 (Deterministic Scoring & Bounded Output)**：
   - 基于多词 Token 在技能名称、触发词、描述、使用场景与正文中的分级权重打分，排序稳定可复现；
   - 严格遵循 `limit` 数量上限，防止无界扫描与内存膨胀；
   - 过滤为空或无匹配时提供结构化可解释的 `empty_reason`。

---

### 14.2 数据模型与接口定义 (Models & Signatures)

#### 1. `RetrievalContext` (输入上下文)
```python
@dataclass
class RetrievalContext:
    task_id: Optional[str] = None
    run_id: Optional[str] = None
    assigned_version: Optional[str] = None
    allowed_tools: Optional[set[str]] = None
    available_dependencies: Optional[set[str]] = None
    scope: Optional[str] = None
    limit: int = 10
    include_evidence: bool = True
```

#### 2. `SkillRecommendation` (正式技能建议)
```python
@dataclass
class SkillRecommendation:
    skill_name: str
    version: str
    content_hash: str
    meta: Optional[SkillMeta]
    body: str
    relevance_score: float
    match_reasons: list[str]
    lineage: Optional[MemoryLineage]
    source_type: Literal["episode_mined", "document_derived", "manual_or_unknown"]
    verification_episodes: list[Episode]
    is_verified: bool
    is_canary: bool
    dependencies: list[str]
    lineage_broken: bool
    broken_reasons: list[str]
```

#### 3. `FutureRetrievalResult` (完整检索响应)
```python
@dataclass
class FutureRetrievalResult:
    query: str
    skills: list[SkillRecommendation]
    evidence_episodes: list[Episode]
    evidence_facts: list[SemanticFact]
    candidates: list[CandidateSkill]
    conflicts: list[SemanticConflict]
    filtered_out: list[dict[str, Any]]
    empty_reason: Optional[str]
```

---

### 14.3 API 使用示例 (API Usage)

```python
from pathlib import Path
from skillforge import ThreeTierMemoryManager, RetrievalContext

db_path = Path("skillforge.db")
mem_mgr = ThreeTierMemoryManager(db_path=db_path)

# 1. 任务上下文感知检索（带工具权限、依赖、固定运行ID）
ctx = RetrievalContext(
    run_id="run_task_2026_01",
    allowed_tools={"calculator", "safe_read"},
    available_dependencies={"python", "numpy"},
    scope="prod_cluster",
    limit=5,
)

# 2. 发起跨层多词确定性检索
res = mem_mgr.retrieve("arithmetic calculation fast", context=ctx)

# 3. 消费推荐与审计理由
for rec in res.skills:
    print(f"Recommended Skill: {rec.skill_name} (v{rec.version})")
    print(f"Relevance Score: {rec.relevance_score}, Match Reasons: {rec.match_reasons}")
    print(f"Source Type: {rec.source_type}, Lineage Broken: {rec.lineage_broken}")
    if rec.verification_episodes:
        print(f"Verification Episode IDs: {[e.episode_id for e in rec.verification_episodes]}")

# 4. 检查被过滤条目与空结果原因
if not res.skills:
    print(f"Empty Reason: {res.empty_reason}")

for item in res.filtered_out:
    print(f"Filtered {item.get('skill_name') or item.get('item_id')}: {item['reason']}")

# 5. 检查语义事实冲突
for c in res.conflicts:
    print(f"Conflict on topic {c.topic}: {c.description}")
```

---

### 14.4 验收场景 R1 - R6 覆盖说明 (Verification Evidence)

在 `tests/test_future_retrieval.py` 中全量覆盖并通过以下 6 项验收场景：

- **R1 (`test_scenario_r1_tiered_and_hybrid_retrieval_boundary`)**：三层放置相同关键词；按 `tier='semantic'`、`tier='episodic'`、`tier='procedural'` 查询精确只返回对应层数据；混合查询时完整保留原数据类型与 ID 前缀（`fact_`、`ep_`、`cand_`）；`Episode` 绝对不被包装或伪装成正式 `Skill`。
- **R2 (`test_scenario_r2_provenance_chains_and_conflict_preservation`)**：来源链真实追溯与断链/冲突暴露：准确识别 `Episode -> Candidate -> Formal Skill`（`source_type='episode_mined'`）与 `DocumentSnippet -> Candidate -> 验证 Episode -> Formal Skill`（`source_type='document_derived'`）；缺失支撑经历或源文档时显式标记 `lineage_broken=True` 并列明原因；相互矛盾的语义事实保留各自 `source_id` 并结构化暴露为 `SemanticConflict`。
- **R3 (`test_scenario_r3_version_binding_rollback_and_permission_dependency_guards`)**：上下文版本绑定、回滚与权限守卫：同一技能存在历史版与灰度版时，绑定 `run_id` 准确推荐其冻结版本；回滚后新任务不推荐已失效版本；技能所需工具超出 `allowed_tools` 或缺少 `available_dependencies` 时，结构化移入 `filtered_out`，杜绝返回越权或不可用建议。
- **R4 (`test_scenario_r4_evaluation_and_failure_isolation_and_unpromoted_candidates`)**：评测与失败隔离及草稿守卫：`purpose='evaluation'` 经历严禁进入正向推荐证据；失败经历绝不记为成功凭证；未晋升的 `DRAFT` 候选绝不出现在正式技能推荐列表中。
- **R5 (`test_scenario_r5_deterministic_scoring_ranking_and_empty_reasons`)**：确定性多词打分、排序与可解释空原因：基于名称、触发词、描述、正文分级打分；平分时按 SemVer 与名称确定性 tie-break；严格遵守 `limit`；无匹配或全部被过滤时提供精确可复现的 `empty_reason`。
- **R6 (`test_scenario_r6_read_only_and_zero_side_effects_guarantee`)**：只读与零副作用绝对保证：命中正式技能后仅返回可审查建议；底层工具调用数保持为 0，候选晋升数为 0，正式技能与磁盘修改数为 0，部署版本切换数为 0，新增执行 `Episode` 保持为 0。

---

## 15. 复用模式、检索命中、版本固定、降级与归因闭环 (Retrieval Execution Loop)

### 15.1 核心设计与执行闭环原则 (Execution Loop Principles)

在 Phase P3 完成跨层记忆检索之后，本模块在现有的任务执行入口（`AgentRuntime.start_run`）实施**最薄集成**，打通“新任务 -> 自动检索 -> 版本冻结 -> 安全调度与沙箱隔离 -> 真实经验采集 -> 失败受控归因与修复”的完整自主闭环。

```mermaid
flowchart TD
    TaskStart["AgentRuntime.start_run(enable_reuse=True, task_description=...)"] --> CheckReuse{"enable_reuse?<br/>& skill_name is None"}
    CheckReuse -->|No| DirectRun["Direct Task Run<br/>(bind explicit skill or empty)"]
    CheckReuse -->|Yes| QueryP3["ThreeTierMemoryManager.retrieve(query, context)"]
    
    QueryP3 --> EvalSkills{"res.skills has hits?"}
    EvalSkills -->|Yes: Pick Top 1| BindVer["Freeze Top Qualified Skill<br/>(assigned_version, content_hash)<br/>INSERT INTO run_version_bindings"]
    EvalSkills -->|No / Filtered Out| FallbackCheck{"require_reuse?"}
    
    FallbackCheck -->|True| TerminalReject["Terminal State: FAILED<br/>error_type: NO_REUSABLE_SKILL / PERMISSION_DENIED<br/>tool_executions = 0"]
    FallbackCheck -->|False| NormalFallback["Proceed Standard Run<br/>retrieval_hit=False<br/>skill_name=None"]
    
    BindVer --> ToolDispatch["runtime.execute_tool()<br/>via ToolBroker -> Sandbox"]
    NormalFallback --> ToolDispatch
    
    ToolDispatch --> RunFinish["runtime.finalize_run()<br/>Outcome strictly derived from independent evidence"]
    RunFinish --> EpSave["ExperienceCollector.finish_run()<br/>Persist immutable Episode<br/>(records task, fixed version, retrieval_hit, reasons, backend)"]
    
    EpSave --> OutcomeCheck{"ep.outcome == 'failure'?"}
    OutcomeCheck -->|No: success/unknown| EndSuccess["Completed Run"]
    OutcomeCheck -->|Yes: failure| AttributeFailure["M4a attribute_failure([ep])<br/>Categorize: skill / tool / policy / planner / evaluator"]
    
    AttributeFailure --> JobCreate["create_repair_job_for_run()<br/>Create bounded RepairJob<br/>No silent version publishing!"]
```

#### 六大核心不变量 (Core Invariants):
1. **最薄入口集成与应用主导 (Thin Integration & App Driven)**：
   - 复用模式严格由应用层参数 `enable_reuse=True` 开启，模型或技能正文无法自行篡改工具白名单、命令、网络策略或沙箱配置；
   - 启用复用模式后调用方无需手动传入 `skill_name`，由运行时自动检索合格已晋升的正式技能并选用首条确定性推荐；未晋升的 `DRAFT` 候选、未定型的 `Episode` 或文档原文绝对不作为正式技能执行。
2. **启动时版本快照冻结与灰度/回滚免疫 (Snapshot Binding & Rollback Immunity)**：
   - 任务在 `start_run` 选定技能版本后，立即在 `run_version_bindings` 与 `runtime_runs` 中原子冻结版本号、内容哈希与正文快照；
   - 运行中即使发生外部灰度切流、全量晋升或紧急回滚，在运行任务始终保持最初绑定的版本执行与归档；后续新任务自动路由至生效的最新部署指针；两者在 `Episode` 中分别如实记录对应版本。
3. **严格权限与依赖安全拦截 (Security & Dependency Interception)**：
   - 技能所需工具超出 `ToolBroker.application_allowlist` 或沙箱探测缺失必要物理依赖时，在调度前直接拒绝（`PERMISSION_DENIED` 或 `DEPENDENCY_MISSING`）；
   - 宿主与业务工具的真实执行次数严格保持为 0，终端状态记录为拒绝/失败，杜绝伪造成功。
4. **确定性降级与杜绝伪造 (Deterministic Fallback without Forgery)**：
   - 检索落空或全部被安全规则过滤时，若 `require_reuse=False` 则安全回退至无技能绑定的普通任务执行；若 `require_reuse=True` 则显式置为非复用终态；
   - `Episode.environment` 真实记录 `retrieval_hit=False` 及 `retrieval_empty_reason`，严禁捏造命中、伪造 Skill 或静默晋升候选。
5. **失败证据保真与 M4a 有界归因修复 (Failure Fidelity & M4a Attribution)**：
   - 命中正式技能但业务工具执行报错时，`Episode` 完整记录真实失败证据；
   - 运行时提供 `attribute_run_failure` 桥接 M4a 责任层归因（`skill`、`tool`、`policy` 等），并由 `create_repair_job_for_run` 构造有界持久化的 `RepairJob`，严禁在未经验证前直接推发布正式新版本。
6. **评测数据隔离与检索零副作用 (Purpose Isolation & Zero Side-Effects)**：
   - `purpose='evaluation'` 的评测任务在复用执行后，其生成的 `Episode` 严格被模式挖掘（`mine_pending`）过滤排除，防止评测数据污染进化学习池；
   - 检索过程完全只读，执行前后数据库无写操作副作用。

---

### 15.2 API 签名与调用示例 (API Signatures & Usage Example)

#### 1. 运行时扩展入口
```python
class AgentRuntime:
    def start_run(
        self,
        run_id: str,
        task_id: str,
        skill_name: Optional[str] = None,
        purpose: Literal["evaluation", "learning"] = "evaluation",
        budget_max: int = 10,
        deadline_ts: Optional[float] = None,
        skill_required_tools: Optional[set[str]] = None,
        enable_reuse: bool = False,
        task_description: Optional[str] = None,
        retrieval_context: Optional[RetrievalContext] = None,
        memory_manager: Optional[Any] = None,
        require_reuse: bool = False,
    ) -> RunRecord:
        ...

    def get_retrieval_result(self, run_id: str) -> Optional[FutureRetrievalResult]:
        ...

    def attribute_run_failure(
        self,
        run_id: str,
        llm: Any = None,
    ) -> Optional[AttributionDiagnosis]:
        ...

    def create_repair_job_for_run(
        self,
        run_id: str,
        candidate_store: Optional[CandidateStore] = None,
        llm: Any = None,
        max_attempts: int = 2,
    ) -> Optional[RepairJob]:
        ...
```

#### 2. 调用方代码示例
```python
from pathlib import Path
from skillforge import AgentRuntime, ToolBroker, ThreeTierMemoryManager

db_path = Path("skillforge.db")
broker = ToolBroker(application_allowlist={"calculator"})
runtime = AgentRuntime(db_path=db_path, tool_broker=broker)

# 1. 新任务只提供 task_description 并开启复用模式
run = runtime.start_run(
    run_id="run_2026_09",
    task_id="task_calc_sum",
    purpose="learning",
    enable_reuse=True,
    task_description="Calculate sum of mathematical values",
)
print(f"Auto-selected Skill: {run.skill_name} v{run.skill_version}")

# 2. 经 Broker 与沙箱安全执行工具
call_rec = runtime.execute_tool(
    run_id="run_2026_09",
    tool_name="calculator",
    parameters={"a": 10, "b": 32},
)
print(f"Tool Output: {call_rec.output_data}")

# 3. 终态基于独立验证凭证生成 Episode
run_final, ep = runtime.finalize_run(
    run_id="run_2026_09",
    verification_evidence={"independent_pass": True, "result": 42},
)
print(f"Episode: {ep.episode_id}, outcome={ep.outcome}, retrieval_hit={ep.environment['retrieval_hit']}")

# 4. 若业务工具执行失败，一键触发归因与修复作业
if ep.outcome == "failure":
    diag = runtime.attribute_run_failure(run_id="run_2026_09")
    print(f"Diagnosed layer: {diag.responsibility_layer}, reason: {diag.reason}")
    repair_job = runtime.create_repair_job_for_run(run_id="run_2026_09")
    print(f"Created RepairJob: {repair_job.job_id} (status={repair_job.status})")
```

---

### 15.3 验收场景 U1 - U6 覆盖说明 (Verification Evidence U1 - U6)

在 `tests/test_retrieval_execution_loop.py` 中全量覆盖并通过以下 6 项离线验收场景：

- **U1 (`test_u1_auto_retrieval_and_sandboxed_execution_success`)**：预置验证通过的已晋升正式 Skill 及注册工具；新任务仅提供 `task_description` 并开启 `enable_reuse=True`；自动命中并选择合格版本；经 Runtime -> Broker -> Sandbox 执行成功；Episode 记录 task、固定版本、检索命中原因、backend 与成功结果。
- **U2 (`test_u2_in_flight_version_binding_immune_to_canary_and_rollback`)**：运行中任务绑定版本快照；任务执行期间外部发生灰度切换或回滚，在运行任务始终保持原绑定版本执行并如实记录；回滚后新任务自动使用最新稳定版本；两次 Episode 分别准确记录对应版本。
- **U3 (`test_u3_security_and_dependency_boundary_blocks_unauthorized_tool`)**：检索关键词匹配但工具缺失权限或缺失依赖；被安全过滤或 Broker 拦截；宿主/业务工具真实执行次数严格为 0；终端状态为拒绝/失败；不捏造成功也不触发无关代码修复（归因为 policy）。
- **U4 (`test_u4_retrieval_miss_or_filtered_clean_fallback`)**：检索无结果或被过滤；`require_reuse=False` 时安全回退到无复用执行；`require_reuse=True` 时显式置为非复用终态；Episode 记录 `retrieval_hit=False` 及实际执行结果；不伪造 Skill 生成。
- **U5 (`test_u5_formal_skill_failure_routes_to_m4a_attribution_and_bounded_repair`)**：命中正式 Skill 但业务工具执行返回失败；Episode 记录真实失败证据；正确触发 M4a `attribute_failure` 归因并生成持久化 `RepairJob`；不静默晋升新版本。
- **U6 (`test_u6_evaluation_purpose_isolation_and_zero_retrieval_side_effects`)**：`purpose='evaluation'` 目的任务执行并记录 evaluation 状态；模式挖掘扫描时被严格过滤排除；尝试用于候选生成抛出错误；检索本身无写操作副作用，数据库表行数保持严格一致。

---

## 16. 端到端演化闭环全链路离线受控验收 (Full-Chain Offline Acceptance F1 - F4)

### 16.1 演化闭环完整架构全景 (Evolution Loop Architecture)

在经历了 M1–M5c、P2 文档转技能、P3 记忆检索、U1–U6 实际复用入口等阶段后，SkillForge 构建起一条完整的自主演进与安全管控自洽闭环：

```mermaid
flowchart TD
    subgraph Learning["1. 经验采集与沉淀 (Learning & Experience)"]
        Task1["Task 1: Runtime.start_run()"] --> Call1["Runtime.execute_tool()"]
        Task2["Task 2: Runtime.start_run()"] --> Call2["Runtime.execute_tool()"]
        Call1 --> Fin1["Runtime.finalize_run()<br/>(Verification Evidence)"]
        Call2 --> Fin2["Runtime.finalize_run()<br/>(Verification Evidence)"]
        Fin1 --> EpStore["Immutable EpisodeStore<br/>(ep_1, ep_2)"]
        Fin2 --> EpStore
    end

    subgraph Mining["2. 模式挖掘与准入守卫 (Mining & Promotion Gate)"]
        EpStore --> Mine["mine_pending()<br/>(Cosine / Embed Clustering)"]
        Mine --> Cand["CandidateSkill (DRAFT)<br/>(Fixed Source IDs, Registry Untouched)"]
        Cand --> Gate{"Gate Validation<br/>(Ratchet & Deterministic Tests)"}
        Gate -->|Unconfirmed / Fail| Reject["Hold in CandidateStore (v1 Not Active)"]
        Gate -->|caller_confirmed=True| ReleaseV1["Promote to Formal Release v1<br/>(SkillRegistry & DeploymentManager)"]
    end

    subgraph Reuse["3. 上下文感知检索与安全复用 (Retrieval & Execution)"]
        NewTask["New Task: start_run(enable_reuse=True)"] --> P3Retrieve["P3 Future Memory Retrieval<br/>(Active Version, Scope, Deps, Perms)"]
        ReleaseV1 -.-> P3Retrieve
        P3Retrieve --> AutoSelect["Auto-Select Qualified v1"]
        AutoSelect --> BrokerExec["ToolBroker -> Sandbox Isolation Backend"]
        BrokerExec --> ReuseEp["Record Immutable Reuse Episode<br/>(version=v1, hit=True, backend=MacSeatbelt)"]
    end

    subgraph Attribution["4. 失败归因与有界修复 (Attribution & Bounded Repair)"]
        ToolFail["Controlled Tool Error"] --> AttributionCheck["attribute_failure()<br/>(Classify: Skill / Tool / Policy)"]
        AttributionCheck -->|Skill / Tool| Repair["Create Bounded RepairJob<br/>(max_attempts=2, SQLite Ledger)"]
        AttributionCheck -->|Policy| NoRepair["Signal Override: No Skill Repair Triggered"]
        Repair --> PatchCand["Draft Patch Candidate (v2)"]
        PatchCand --> RegressGate{"Regression Evaluation Gate"}
        RegressGate -->|Declined / Unconfirmed| BlockPatch["Block v2, Stable v1 Remains Active"]
        RegressGate -->|PASS + Confirmed| AdmitCanary["Admit as Canary v2 (share=100)"]
    end

    subgraph Evolution["5. 版本灰度、运行绑定与受控回滚 (Canary & Rollback)"]
        AdmitCanary --> FlightRun["In-Flight Task Binds Canary v2<br/>(Frozen Snapshot)"]
        FlightRun --> RollbackTrigger["Emergency Rollback Triggered<br/>(target_version=1.0.0)"]
        RollbackTrigger --> StableRestored["Deployment Restores Stable v1, Canary Disabled"]
        FlightRun --> FlightFinish["In-Flight Task Completes on Bound v2"]
        SubsequentRun["Subsequent New Tasks"] --> RoutesStable["Route to Active Stable v1"]
        RoutesStable --> LineageTrace["Lineage & Provenance Intact<br/>(Zero Tampering with Historic Snapshots)"]
    end

    Learning --> Mining
    Mining --> Reuse
    Reuse --> Attribution
    Attribution --> Evolution
```

### 16.2 离线验收场景 F1 - F4 覆盖说明 (Verification Evidence F1 - F4)

在 `tests/test_end_to_end_evolution_loop.py` 中全量覆盖并通过以下 4 项离线受控全链路验收场景：

- **F1 (`test_f1_forward_generation_to_reuse`)**：正向生成到复用：
  - 从现有实际入口运行两个同模式 learning 任务，留下不可变 Episode；
  - 经真实 Mining 形成单一候选并保留精确来源 Episode ID（活跃 Registry 严格保持未触碰）；
  - 验证 Gate 校验通过但 `caller_confirmed=False` 拦截晋升；显式确认 `caller_confirmed=True` 后发布正式 v1；
  - 新任务仅传入 `task_description` 与 `enable_reuse=True`（不传 `skill_name`）；自动检索选中 v1；
  - 经 Runtime -> Broker -> Sandbox 执行，终态生成包含固定版本、检索命中证据、沙箱后端与校验通过的真实复用 Episode。
- **F2 (`test_f2_failure_attribution_and_gate`)**：失败归因与守卫：
  - 复用 v1 时在工具执行中受控触发业务错误；终态 Episode 记录真实失败；
  - M4a `attribute_failure` 准确将根因归因为 skill/tool，生成 SQLite 持久化的有界 `RepairJob`；
  - 修补候选未经验证或回归评测 DECLINED 时，拒绝发布并保持稳定版本 v1 不变；
  - 触发未授权工具时精确被策略拦截（宿主执行 0 次），归因层标记为 `policy`，绝对不错误触发技能代码修补。
- **F3 (`test_f3_patch_verification_canary_and_rollback`)**：修补验证、灰度与回滚：
  - 修复候选通过回归评测并显式确认生成正式 v2；
  - 配置灰度路由（100% 流量给 v2）；在运行任务 `run_inflight` 启动时绑定并冻结 v2；
  - 执行途中触发紧急回滚（目标版本 1.0.0，清空灰度）；在运行任务继续按原绑定 v2 完成工具执行与 Episode 沉淀；
  - 回滚生效后发起的新任务自动路由回稳定版本 1.0.0；
  - 完整保留历史 Episodes、Release 历史及血缘追踪（`trace_lineage` 不断链、历史快照无损坏）。
- **F4 (`test_f4_isolation_and_fail_closed_safety`)**：隔离性与 Fail-Closed 安全底线：
  - `purpose='evaluation'` 评测用例 Episode 严格排除在模式挖掘之外（0 候选产生，直接引用抛出 ValueError）；
  - 未晋升的 DRAFT 候选绝不允许假冒为可执行正式技能进入检索推荐（在 `filtered_out` 中被 `unpromoted_candidate` 拦截）；
  - 检索无匹配结果时：`require_reuse=False` 干净降级为普通执行；`require_reuse=True` 立即转入 `FAILED(NO_REUSABLE_SKILL)` 终态；
  - 未授权或依赖缺失的技能被阻断，底层宿主真实工具执行次数严格为 0。

