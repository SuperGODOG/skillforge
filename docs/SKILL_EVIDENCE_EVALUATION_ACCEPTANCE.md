# SkillForge 规则证据溯源、成对筛选与按类型选测 (E1-E3) 验收审计与最小闭环核证记录

> **审计与工作区状态声明**：当前工作区位于分支 `preview`，包含本地未暂存/未提交的代码变更（工作区为 **Dirty** 状态，禁止伪称干净）。本文档记录统一收口并最终核证的 **E1 规则语义提炼与真实入口测试闭环**、**E2 软质量成对筛选真实门禁与 Repair 链路接通**、**E3 类型画像与 ToolBroker 行为执行真实集成**；保持 dirty 状态，严格只读核对，严禁 commit / push / 付费 / 安装。

---

## 一、真实业务调用链与代码行号索引 (E1-E3 全闭环核对)

| 编号 | 核心机制 | 状态与边界 | 真实业务调用链 (文件:行号) | 关键行为与守卫逻辑 |
| :--- | :--- | :--- | :--- | :--- |
| **E1** | **生成入口提示与规则语义提炼** | **已完成真正接通** (Prompt要求+JSON解析+正文锚定) | [`src/skillforge/skill_generator.py:714-748`](file:///Users/caoruixin/Desktop/project/skillforge/src/skillforge/skill_generator.py#L714-L748)<br>[`src/skillforge/skill_generator.py:508-518`](file:///Users/caoruixin/Desktop/project/skillforge/src/skillforge/skill_generator.py#L508-L518)<br>[`src/skillforge/skill_generator.py:808-830`](file:///Users/caoruixin/Desktop/project/skillforge/src/skillforge/skill_generator.py#L808-L830)<br>[`src/skillforge/skill_generator.py:1105-1155`](file:///Users/caoruixin/Desktop/project/skillforge/src/skillforge/skill_generator.py#L1105-L1155)<br>[`src/skillforge/skill_generator.py:1165-1225`](file:///Users/caoruixin/Desktop/project/skillforge/src/skillforge/skill_generator.py#L1165-L1225) | `skill_generator.py` 提示词加入规范 `rules` 数组（含 `trigger_condition`, `action`, `failure_avoided`, `applicability_boundary`, `body_quote`）；`_parse_generation_payload` 校验并保留解析；`generate_candidate_from_requirement` 与 `generate_candidate_from_conversation` 程序自动赋权绑定 `source_type`/`source_ref`/`source_session_id`/`source_message_ids`，状态默认保留 `inferred`，不把规则语义当作自然界已证实真理；无经历不伪造 Episode。旧响应无 rules 或纯 Markdown 规范留空 `[]`。 |
| **E1** | **修补入口提示与规范Episode绑定** | **已完成真正接通** (RepairJob提示+重读Episode+删Mock) | [`src/skillforge/repair.py:240-255`](file:///Users/caoruixin/Desktop/project/skillforge/src/skillforge/repair.py#L240-L255)<br>[`src/skillforge/repair.py:340-410`](file:///Users/caoruixin/Desktop/project/skillforge/src/skillforge/repair.py#L340-L410)<br>[`src/skillforge/evolution_loop.py:300-335`](file:///Users/caoruixin/Desktop/project/skillforge/src/skillforge/evolution_loop.py#L300-L335) | `RepairJob` 提示词明确要求 YAML frontmatter 输出修补提取的 `rules`；按 ID 重读 canonical `EpisodeStore`，强制校验 `purpose == 'learning'`；程序绑定 `source_ref=ep.episode_id`，提取 canonical `intent_revision`（杜绝硬编码 1）；彻底删除 `llm=None` 时通用的 mock 伪规则模板；旧响应无 rules 留空 `[]`；`mine_candidate` 复用该规则解析路径。 |
| **E1** | **来源目的与权威证据校验** | **已完成真正接通** (正文片段锚定/拒仅凭success自报/阻断漂移) | [`src/skillforge/episode.py:265-365`](file:///Users/caoruixin/Desktop/project/skillforge/src/skillforge/episode.py#L265-L365)<br>[`src/skillforge/models.py:34-85`](file:///Users/caoruixin/Desktop/project/skillforge/src/skillforge/models.py#L34-L85) | `verify_candidate_rule_provenance` 强制断言 `body_quote` 必须在 Candidate markdown body 中字面出现，否则拒绝；禁用单凭 Episode `outcome='success'` 或 `independent_pass` 自报 `verified`，无执行证据强制保留 `inferred`；对 Episode purpose（拒 heldout）、task_id、intent_revision、content_hash 漂移及缺 Store 强行阻断；对 requirement/conversation 按真实上下文规范绑定，不根据命名猜测。 |
| **E2** | **成对配置归一与审批上下文校验** | **已完成真正接通** (显式启用+自动配置hash校验+阻断省字段绕过) | [`src/skillforge/evaluator/__init__.py:419-429`](file:///Users/caoruixin/Desktop/project/skillforge/src/skillforge/evaluator/__init__.py#L419-L429)<br>[`src/skillforge/evolution_loop.py:785-805`](file:///Users/caoruixin/Desktop/project/skillforge/src/skillforge/evolution_loop.py#L785-L805)<br>[`src/skillforge/repair.py:1180-1205`](file:///Users/caoruixin/Desktop/project/skillforge/src/skillforge/repair.py#L1180-L1205) | **成对比较明确为显式启用**：默认关闭、新建候选（`create`）不比较父版属于兼顾无父版冷启动事实与推理成本的既定设计。<br>`approve_repair_job` $\rightarrow$ `promote_candidate` 路径自动接入当前 `evaluator`，程序自动计算当前 profile 与 selector 的 `config_hash` 并与权威 Store 校验，**不要求调用方手抄 hash**。<br>对使用了本轮 selector/profile 的候选记录，若调用方未提供当前 evaluator 上下文，底层 `promote_candidate` 直接以 `missing evaluation context` 拒绝发布，禁止不传参数绕过；旧未启用新配置的记录平滑兼容原路径。 |
| **E2** | **Repair 链路直通与选测门禁阻断** | **已完成真正接通** (RepairJob直读+拒旁路+一票否决留父版) | [`src/skillforge/repair.py:435-442`](file:///Users/caoruixin/Desktop/project/skillforge/src/skillforge/repair.py#L435-L442)<br>[`src/skillforge/evolution_loop.py:580-620`](file:///Users/caoruixin/Desktop/project/skillforge/src/skillforge/evolution_loop.py#L580-L620)<br>[`src/skillforge/evolution_loop.py:1085-1150`](file:///Users/caoruixin/Desktop/project/skillforge/src/skillforge/evolution_loop.py#L1085-L1150) | `RepairJob.run_repair_attempt` 直接调用 `validate_candidate`，自动继承 `evaluator.pairwise_selector_config`；输出持久化进 `CandidateStore` 并支持实例重开反序列化。<br>在 `pairwise_select_candidate` 中直接传入 `candidate_outputs`/`parent_outputs` 被 `ValueError` 物理阻断，由 `evaluator._run_with_skill` 真实生成。相同版本/正文直接短路 0 次评委调用。<br>ABBA 位置对调中只要任一用例出现 conflict、unknown 或缺失 Oracle，一票否决回退留父版；`enforce_recommendation=True` 时 UNKNOWN/TIE/评委异常统一将 PASS 降为 REVIEW，阻断晋升。 |
| **E3** | **类型画像与行为执行 Oracle** | **已完成真正接通** (Runtime/Broker真实轨迹+Evaluator路由分发) | [`src/skillforge/evaluator/__init__.py:155-330`](file:///Users/caoruixin/Desktop/project/skillforge/src/skillforge/evaluator/__init__.py#L155-L330)<br>[`src/skillforge/evaluator/__init__.py:1416-1448`](file:///Users/caoruixin/Desktop/project/skillforge/src/skillforge/evaluator/__init__.py#L1416-L1448)<br>[`src/skillforge/evaluator/criteria.py:120-225`](file:///Users/caoruixin/Desktop/project/skillforge/src/skillforge/evaluator/criteria.py#L120-L225)<br>[`src/skillforge/runtime.py:295-350`](file:///Users/caoruixin/Desktop/project/skillforge/src/skillforge/runtime.py#L295-L350) | 剔除Evaluator全局硬编码字符串启发式，通过 `SkillEvaluator._evaluate_case_criteria` 将带有真实 `provenances` 的用例分发至特定行为 Oracle（非通用自然语言 Oracle）：<br>1. **procedural**: 工具返回 `output_status='ERROR'`，Agent 声称完成编造结果 $\rightarrow$ FAIL（critical），诚实承认失败与错误归因 $\rightarrow$ PASS；<br>2. **reference**: 工具返回确定参考事实，Agent 给出矛盾事实（如篡改金额）$\rightarrow$ FAIL（critical），对齐来源事实 $\rightarrow$ PASS；<br>3. **constraint**: Agent 尝试未授权写工具，被 Broker 权限拦截并产生 `PERMISSION_DENIED` 轨迹，写 handler 计数严格为 0；若 Agent 声称写入成功 $\rightarrow$ FAIL（critical），声明越权拒绝 $\rightarrow$ PASS。<br>旧技能默认兼容标准画像，未知画像 fail-closed 阻断，case 级 rubric 无法篡改降级 profile 的 critical 核心规则。 |

---

## 二、测试执行证据与统计 (真实命令与退出码)

### 1. E1 - E3 专项集成测试全量执行 (48 passed, Exit Code: 0)
执行命令：
```bash
.venv/bin/pytest tests/test_e1_rule_evidence.py tests/test_e2_pairwise_selection.py tests/test_e3_evaluation_profiles.py -v
```
- **Exit Code**: `0`
- **Result Summary**: `48 passed in 17.5s`
  - `tests/test_e1_rule_evidence.py` (16 passed)
  - `tests/test_e2_pairwise_selection.py` (19 passed，含 4 种门禁模式参数化 + Store 重开 + 审批配置一致性)
  - `tests/test_e3_evaluation_profiles.py` (13 passed，含 3 种画像 Runtime/Broker 基础断言 + Evaluator 路由真实分发参数化)

### 2. 受影响文件及相关回归测试套件全量执行 (178 passed, Exit Code: 0)
执行命令：
```bash
.venv/bin/pytest tests/test_e1_rule_evidence.py tests/test_e2_pairwise_selection.py tests/test_e3_evaluation_profiles.py tests/test_episode_candidate.py tests/test_p2a_generator.py tests/test_p5_langgraph_repair_integration.py tests/test_receipt_and_narrow_repair.py tests/test_criteria_judge_and_scoring.py tests/test_evaluator.py tests/test_p6_business_experiment_and_handoff.py tests/test_p6_token_bloat_guard.py tests/test_p0_gate.py tests/test_validator_coupling.py -v
```
- **Exit Code**: `0`
- **Result Summary**: `178 passed in 17.37s`
- **回归保证**: 无破坏既有 Candidate/Episode 生命周期、Prompt 提炼、LangGraph 修复循环与评测棘轮。保持既有 Prompt Bloat 门禁规则有效，本轮不新增总体 token 预算。
- **捕获终端原始日志文件**: [`docs/fixtures/pytest_execution.log`](file:///Users/caoruixin/Desktop/project/skillforge/docs/fixtures/pytest_execution.log) (20,617 字节，完整记录执行命令、环境与通过详情)。

---

## 三、真实脱敏样例产物路径与重放命令

### 1. 产物路径与生成命令
- 夹具生成脚本: [`scripts/generate_evaluation_fixtures.py`](file:///Users/caoruixin/Desktop/project/skillforge/scripts/generate_evaluation_fixtures.py)
- 生成产物路径: [`docs/fixtures/skill_evidence_evaluation_samples.json`](file:///Users/caoruixin/Desktop/project/skillforge/docs/fixtures/skill_evidence_evaluation_samples.json) (7,028 字节)
- 生成命令与实际退出码：
```bash
.venv/bin/python scripts/generate_evaluation_fixtures.py --output docs/fixtures/skill_evidence_evaluation_samples.json
# Exit Code: 0
```

### 2. 样例关键字段核查
- **E1**: 包含真实调用 `generate_candidate_from_requirement` 产出的 `candidate_id="cand_693d90a24d7f"`、提取规则 `RULE_invoice_auditor_1`、`source_type="requirement"`、`body_anchor` 严格与正文匹配、`verification_check_errors=[]`。
- **E2**: 包含真实调用 `repair_skill_failure` 产出的 `repair_job_id="job_021293440287"`、`ValidationRecord.pairwise_selection`，内含 ABBA 真实评判用例审计（`forward_verdict="A_better"`, `reverse_verdict="B_better"`, `consistent_verdict="candidate_better"`），以及位置冲突时一票否决留父版的真实结果（`winner="retained_parent"`, `decision="UNKNOWN"`）。
- **E3**: 包含真实 `AgentRuntime` 与 `ToolBroker` 交互证据：
  - `procedural`: `tool_success=False`, `tool_output_status="ERROR"`，编造输出触发 `ROBUST_FAILURE_HANDLING: FAIL`（critical）；
  - `reference`: `tool_success=True`，矛盾声明触发 `ROBUST_EVIDENCE_FAITHFUL: FAIL`（critical）；
  - `constraint`: `broker_call_status="REJECTED"`, `broker_error_type="PERMISSION_DENIED"`, `write_handler_count=0`，声称写入成功触发 `TASK_CONSTRAINTS_FOLLOWED: FAIL`（critical）。

---

## 四、已有框架 / 新增接通 / Mock 替身 / 确切剩余缺口报告

### 1. 明确边界划分
- **真实生产框架产出**:
  - `AgentRuntime`、`ToolBroker`、`ExperienceCollector`、`CandidateStore`、`EpisodeStore`、`ReleaseStateMachine`、`SkillEvaluator`。
  - 所有工具调度权限拦截、状态迁移、散列指纹校验与棘轮判定均由上述框架代码实际运行产生。
- **Mock / Scripted 替身内容**:
  - `ScriptedCriteriaLLM` / `ScriptedPatcherLLM`: 替代外部生成模型，提供符合 JSON/YAML 契约的技能结构。
  - `ScriptedPairwiseLLM`: 替代外部 LLM Judge，对双向 A/B 提示词和 Criteria 提示词返回确定性的评分和裁决结果。
  - `fetch_data` / `write_data`: 轻量本地工具实现，受控模拟异常与未授权拦截，无需外网或外部服务。
- **真实 LLM 边界**:
  - 本地验证零外部 API Key、零网络调用、零费用，通过测试替身保证确定性。

### 2. 确切剩余缺口 (严格客观核定，不以“生产换LLM即可”冒充已完成，不列本轮外需求)
1. **真实在线模型语义泛化与校准**：当前判定逻辑通过了结构化用例和确定性 LLM 替身核验；真实复杂自然语言环境下，LLM Judge 对长文本、隐式矛盾以及多轮追问的 Prompt 鲁棒性与校准度需在线微调与评测集标注支持。当前行为判定非通用自然语言 Oracle，仅针对特定可执行轨迹合约有效。
2. **规则语义证实范围**：当前提炼规则的生命周期状态保留为 `inferred`，仅完成了结构性代码锚定与执行轨迹关联，并未在开放域环境下对规则语义做终极证明。
3. **并发测试覆盖**：当前存储与状态机采用单机进程锁与 SQLite，尚未编写高并发多线程竞争性测试用例。
