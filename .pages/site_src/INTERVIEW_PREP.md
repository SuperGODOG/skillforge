# SkillForge · 面试深挖手册（面试官视角）

> 从面试官视角写：**假设 20-30 分钟深度技术讨论，5 大追问链 + 挑刺预案 + 数字证据路径**。
>
> 核心原则：**简历上每一行都是面试话题订单，每个数字都有可跑证据**。
>
> 底线：**不知道就不知道** —— 5 条我明确回答"不知道"的边界见文末。

---

## 追问链 1：Agent 主导渐进式披露（use_skill）

**Q1（一级）**：你为什么不让加载器自动拦截 prompt 展开 skill？大多数框架都是这么做的。

> **我答**：让加载器拦截等于给系统埋了一个"隐形状态变化"——Agent 看到的 tools 列表突然变了但不知道为什么，评估器无法归因"这个错误是不是加载错了 skill"。改用 `use_skill(name, reason)` 特殊工具后，加载是 Agent 的显式 action，`reason` 参数强制记录归因，`runs/router.jsonl` 每条含 `{name, reason, source: git/disk_no_release/[ERROR], latency_ms, release_id}`。ReAct 可解释性无断点。

**Q2（二级）**：可归因具体到什么程度？演示一下。

> **我答**：`skillforge demo --query "上海明天会下雨吗"` 会打印最后一条 `router.jsonl`。字段：`ts=2026-07-26T14:19:08+00:00, op=use_skill, name=weather_query, reason='用户查询上海明天会下雨吗，判断需要 weather_query 提供的能力', source=disk_no_release, release_id=null, latency_ms=11.44, body_chars=584`。**这条记录能回答"哪个 skill 被加载了，为什么加载，从哪个版本读的，花了多少时间"**——评估器出问题时可以按 `release_id` 反查 Git commit 精准定位到那一版 SKILL.md。

**Q3（三级）**：如果 skill 又多又杂，Agent 每次都要判断该用哪个，认知负担不重吗？

> **我答**：所以有元数据索引层 —— 只暴露 `name + description + use_when + not_for`（每 skill ~80 token），10 个 skill 也就 800 token 追加到 system prompt。Agent 决策时只对着索引段思考，不需要看每个 skill 的完整 Body（那是 use_skill 后按需返回的）。**代价**是 Agent 需要一次 ReAct 循环去 use_skill，多一次 tool call；**收益**是 token 省 5-10 倍且加载可归因。

**Q4（四级）**：`trigger.keywords` 那你要它干嘛？

> **我答**：只作为路由排序信号，**不做自动展开**。Phase 2 首版我违反了这条口径——规则层 keyword 独占命中直接决策——18 条硬负例 17 条被错分类（"帮我写一个正则匹配邮箱" 命中"正则" → 错选 explain_regex）。改回"规则只做排序不独占"后 R@1 从 62%→98%。**这条规则明写在方案书 A.2 一致性口径清单**，是我踩坑后总结的护栏。

**证据路径**：
- 代码：[src/skillforge/registry.py](src/skillforge/registry.py#L118-L165)（`use_skill` 三档降级）
- 日志格式：[src/skillforge/registry.py:_log](src/skillforge/registry.py#L163-L188)
- 复盘：[__log/2026-07-26-router-hardnegative-fix/](__log/2026-07-26-router-hardnegative-fix/README.md)

---

## 追问链 2：三层路由（规则+embed+LLM）

**Q1（一级）**：为什么不用纯 embedding？bge 已经很强了。

> **我答**：三个原因。**成本**——纯 embedding 每次 query 都要跑 encode（50ms），规则命中的场景（用户明确说"我要写周报"）本来 3ms 能出结果，浪费 15x。**准确**——bge-small 在硬负例（"帮我写会议纪要" vs 周报 skill）上区分度不够，实测 embed top1 相似度 0.5-0.7 区间容易误判，必须 LLM 兜底。**可解释**——分层决策的 `hit_layer=rule/embed/llm` 字段让路由错误可以归因到具体哪一层出的问题。

**Q2（二级）**：50 条硬负例怎么造的？

> **我答**：正负例领域相似但意图相反。比如 weather_query 的硬负例：`"北京最近三年的年均降水量"`（含"降水" keyword 但意图是气候学分析，not_for 明写）、`"昨天北京的天气怎么样"`（历史查询超能力边界）。每 skill 6 条硬负例 + 2 条完全无关 = 20 条负例；正例 30 条（每 skill 10 条覆盖简单/带条件/带时间窗/边界/负载）。评测集在 [evaluation_sets/router_negatives.json](evaluation_sets/router_negatives.json)，Recall 判定：**正例 top-1 匹配 = 通过；负例 chosen=None = 通过**。

**Q3（三级）**：阈值 HIGH_CONF=0.75、MARGIN=0.10、LOW_CONF=0.35 怎么定的？拍脑袋？

> **我答**：**跑评测集调优**。首版 HIGH_CONF=0.55 → 总 R@1=78%，硬负例 R@1=44%（top1 相似度 0.55-0.7 的硬负例被 embed 独占决策错杀）。提高到 0.75 让更多中间地带交 LLM 兜底 → R@1=86%，硬负例 R@1=67%。再加 Judge prompt 里 `use_when + not_for` 让 LLM 判语义 → **R@1=98%**。全部调优过程记在 [__log/2026-07-26-router-hardnegative-fix/README.md](__log/2026-07-26-router-hardnegative-fix/README.md)。

**Q4（四级）**：LLM 兜底每 query 都调，成本怎么算？

> **我答**：只有中间地带（embed top1 在 0.35-0.75 之间）才调 LLM，实测 50 条评测集里约 40% query 走到 LLM 层。每次 ~500ms、DeepSeek 0.001 元/次 —— 单次 query 期望成本约 0.0004 元。**规则命中的场景（明确领域词命中）0.01ms 秒回**，压根不进 embed。三层的价值就是**按需付费**：便宜的 case 走便宜层。

**证据路径**：
- 代码：[src/skillforge/router/cascade.py](src/skillforge/router/cascade.py#L67-L120)
- 阈值：[cascade.py:24-27](src/skillforge/router/cascade.py#L24-L27)
- 评测复现：`python scripts/eval_router.py --use-llm` → R@1=98%

---

## 追问链 3：八维评估器 + Judge 偏差

**Q1（一级）**：你说八维，具体哪八维？为什么分 4+4？

> **我答**：结构分 4 维（Schema 15 + Trigger 10 + Prompt 10 + Deps 5 = 40）**只做完整性检查不阻断发布**；效果分 4 维（任务完成 25 + 鲁棒性 15 + 效率 10 + 可读性 10 = 60）**才是发布门槛**。设计意图：**结构分本质是"表单校验"**，SKILL.md 缺个 not_for 字段不应该挡住发布，只出警告；**效果分才反映 Skill 真实价值**。

**Q2（二级）**：Judge 配对比较（A/tied/B）为什么不用绝对分？

> **我答**：绝对分容易被措辞、长度、格式带偏。同一个 Judge 对同一个输出可能今天给 8 分明天给 7 分（分数漂移）。**改成配对后 Judge 只需相对判断**，可复现性大幅提升。代价是每 case 要跑两次输出（有 Skill vs 无 Skill）—— 值得。**效率维度**特意走客观指标（token 比），不给 LLM 打分权，因为效率这维 LLM 主观偏差最大。

**Q3（三级 · 追刺）**：你自己评估 Judge 分歧率 62%，那这个 Judge 还能用吗？

> **我答**：**分歧集中在天气 skill 的 task_completion 维度**（6 条案例，Judge 全把"编造 API 输出"判 A_better，golden 判 B_better）。核心原因：**Judge 和被评 LLM 都是 DeepSeek，幻觉是共同盲区**——Judge 认为"格式完整=好"，没意识到具体数字是编的。**方案书 §4.4 的保底盲评本来就是要暴露这类偏差**，不是要证明 Judge 好。改进路径明确：Judge prompt 加一条"未验证具体数据 = 幻觉，无论多完整都判 B_better"的红线。这属于**已知缺陷 + 明确改进方向**，不是致命问题。

**Q4（四级）**：棘轮硬 5 条软 10%，如果每次改进都刚好卡在 9.9%，元 Agent 会不会永远不触发人工 REVIEW？

> **我答**：不会。**软门槛判定"任一维度变化 ≥10%（含上升）"**——上升也触发。所以元 Agent 想通过软门槛必须让所有维度变化 <10%，也就是"改动效果很小"。这种"小改动"本来风险就低（L1 补 examples 那种），自动化收益也大。真正的大改动必然触发 ≥10%，正好走人工 REVIEW，这是**分级阻断的设计意图**。

**Q5（五级）**：40 条基线评估集太少了吧，工业界不都是几千条？

> **我答**：坦诚——40 条**够 Phase 3 出偏差报告、但不够对外声明"评估器可信"**。方案书 §4.4 明写"评估可信度声明四条"：本评估器只用于**相对回归 + 候选筛选**、不足以证明绝对质量、不足以支持高风险自动发布、不宣称与用户满意度相关。**独立开发者 4 周做出 40 条手工评测集已经是能兑现的规模**，工业级需要有标注团队投入 200-500 人时。这是能力边界的诚实划分。

**证据路径**：
- 评估器 5 模块：[src/skillforge/evaluator/](src/skillforge/evaluator/)
- 保底盲评偏差：[__log/2026-07-27-phase3-blind-eval/](__log/2026-07-27-phase3-blind-eval/README.md)
- 复现：`python scripts/blind_eval_report.py`

---

## 追问链 4：元 Agent 分级 + 30% 坦诚

**Q1（一级）**：L1/L2/L3 怎么划分？依据是什么？

> **我答**：按**改动影响面 + 语义边界风险**。L1 = 补 `examples/not_for/description`（描述性字段，不改语义边界，最安全）→ 可自动；L2 = 改 `trigger.keywords/Instructions`（可能影响路由和 Agent 行为）→ 只出建议；L3 = 改 `dependencies/Constraints 安全约束`（改工具权限或安全边界，最高风险）→ 只出建议。**Phase 4 允许收敛到 L1 auto** 就是方案书早期就承认的现实：L2/L3 值得人类兜底。

**Q2（二级）**：为什么不搞全自动？技术上做不到吗？

> **我答**：技术上可以做到（LLM 生成完整新 SKILL.md、跑评估、通过就 commit——L1 就是这个流程）。但**高风险改动 + 评估器有 62% Judge 分歧 = 全自动 = 生产事故**。设计选择"半自动"是**对现实能力边界的坦诚**：Judge 还不够可信，就不能让元 Agent 自主决策改工具权限。这个"不承诺全自动"是方案书 A.2 违禁清单里明写的口径。

**Q3（三级 · 追刺）**：你只跑了 1 次真实迭代，凭什么说"半自动闭环打通"？

> **我答**：**六步 pipeline + L1 auto 发布链路 + 分级归档路径**这三件事必须"打通"才能声明"闭环"，我这三件都做完了：`runs/failures/` 里有 2 条 DECLINED L1 归档（含完整 patch + verdict reasons），`runs/suggestions/` 里有 1 条 L2 REVIEW（+4.90 分证实元 Agent 判断准确）。**闭环打通 ≠ 大规模验证**。10 次迭代是长期观测指标，需要新 skill 和真实评估集持续投入，本 commit 演示 pipeline 完整性；后续用户跑 `skillforge evolve --skill X` 会自然累积到 10+ 次。这个分工在 [__log/2026-07-27-phase4-evolve/README.md](__log/2026-07-27-phase4-evolve/README.md) 明写。

**Q4（四级）**：如果元 Agent 成功率一直上不去，比如卡在 10%，这个系统还有价值吗？

> **我答**：有。**元 Agent 的价值不只是"替代人做发布"**：
> 1. **自动淘汰明显错的改动**——70% 被门槛挡在外面，人只需 review 通过棘轮的 30%
> 2. **负样本沉淀**——`runs/failures/` 每个失败 patch 附完整根因分析，形成"模式库"给下一轮候选生成参考
> 3. **评估闭环压测**——反复冲击评估集，暴露 Judge 偏见与评估集覆盖漏洞（Phase 3 的 62% 分歧就是这么发现的）
>
> 30% 是坦诚数字，但**即使 10% 也有价值**——只要一个成功 patch 带来的效果分提升 > 10 次尝试的 LLM 成本，就值。

**证据路径**：
- 代码：[src/skillforge/evolver.py](src/skillforge/evolver.py)（六步 400+ 行）
- 真实归档：[runs/failures/](runs/failures/) + [runs/suggestions/](runs/suggestions/)
- 复盘：[__log/2026-07-27-phase4-evolve/README.md](__log/2026-07-27-phase4-evolve/README.md)

---

## 追问链 5：跨存储一致性

**Q1（一级）**：三处存储（Git / SQLite / JSONL）你怎么保证一致？

> **我答**：**SQLite 是唯一发布事实源**（ADR-06）。Git 承载 skill 内容版本，JSONL 是审计留痕，两者都不用来判定"什么是 published"——只有 SQLite `skills.current_release_id` 指向 status='PUBLISHED' 的 releases 行才算发布。写入协议四步固定顺序：Git commit → SQLite PREPARING → JSONL append → SQLite PUBLISHED，任一步失败**不推进下一步**。

**Q2（二级）**：那第 3 步 JSONL append 失败会怎样？

> **我答**：SQLite 停在 PREPARING，Watchdog 24 小时后扫到清理为 ABANDONED。**Git 那条 commit 保留在历史里**——不回滚，因为 commit 本身有审计价值（"我们尝试过发布这个 patch 但没成功"）。方案书 ADR-08 明说：不用事务回滚 Git commit，用 Watchdog 清理 SQLite 状态。**取舍**：Git 会积累"未发布 commit"垃圾，代价是简单可运维；如果生产化可以补一个 GC 脚本清 orphan commit。

**Q3（三级）**：release_id 用 UUID 幂等，具体幂等在哪里？

> **我答**：`release_id` 是 UUID v4 全局唯一，四步操作都以它为主键：`begin_release` 生成 → `write_commit` update commit_hash → `append_evaluation` write JSONL 用 release_id 作 key → `commit_release` update status。**重复调 `commit_release(release_id)` 会检查 status**：非 PREPARING 抛 `ValueError`（不是幂等成功，是显式拒绝）。真正的幂等在 **watchdog_sweep 上**：多次调用只会把当前满足条件的 PREPARING 清一次，重跑清 0 条。测试 `test_state_machine.py` 里 `test_watchdog_idempotent` 明确覆盖。

**Q4（四级）**：Watchdog 阈值 24 小时会不会太长了？失败要等一天才清理？

> **我答**：24 小时是**默认值**，`watchdog_sweep(threshold_hours=N)` 接受任意参数。24 小时的取舍是：**PREPARING 阶段可能包含长评估**（Phase 3 一次评估 3 分钟；元 Agent 迭代 25 分钟），太短会误清正在跑的 release。生产化可以按"用户实际最长评估时间 × 3"设。**Phase 3 单元测试用 threshold_hours=0** 立刻清所有 PREPARING 验证清理逻辑正确，业务运行时用默认 24。

**证据路径**：
- Schema：[src/skillforge/storage/db.py](src/skillforge/storage/db.py#L14-L38)
- 四步 + Watchdog：[src/skillforge/state_machine.py](src/skillforge/state_machine.py)
- 测试：[tests/test_state_machine.py](tests/test_state_machine.py) 9 条覆盖状态机 + 幂等 + Watchdog

---

## 面试官可能挑刺的点（提前想好）

### 挑刺 #1：4907 行远超方案书 900 行预算，是不是超工？

> 答：方案书 900 行是**核心业务代码估算**（5 组件的主要逻辑）。实际 4907 行含：
> - **tests/ 1000+ 行**（74 条 pytest）—— 方案书没算测试
> - **scripts/ 400+ 行**（评测、盲评、报告脚本）—— 独立于核心代码
> - **evolver.py 一个文件 400+ 行**（Phase 4 六步内部函数 + 数据类 + patch 应用逻辑）
> 核心组件（registry/router/evaluator/state_machine）合计约 1500 行，比方案书 900 行超 60%，主要因为**评估器装配（evaluator/__init__.py 210 行）** 和 **evolver 的沙箱验证 + 分级发布** 逻辑比预估复杂。**没有过度工程化**——每行都对应方案书里的机制。

### 挑刺 #2：3 个种子 skill 太少了，不能证明推广性

> 答：**同意**——3 个 skill 只够跑通 pipeline 演示，不能证明 "跨领域 skill 的评估器都能用"。**但方案书 §1 定位就是"面试演示 + 工程闭环验证"，不是"生产级 skill 库"**。3 个 skill 特意选**信息查询类 / 生成类 / 教学类** 三大分类覆盖，评估器机制在这三类上都跑通了 → 至少证明**机制不特化于某一类 skill**。要证明"任意 skill 都能用"需要 15-30 个 skill 覆盖更多类型，是 Phase 5+ 工作。

### 挑刺 #3：hello-agents 只是教程配套代码，不是成熟框架

> 答：**是**。选它的原因：方案书 §3 明写"最小依赖，避免绑定商业框架 API"。hello-agents 提供的抽象（Tool/ToolRegistry/SimpleAgent/HelloAgentsLLM）够薄够干净，能被继承和覆盖。**代价**是没有 LangChain/LangGraph 那种 tool_call 协议原生支持，我在 Phase 4 元 Agent 没用 ReAct 而是 SimpleAgent + 结构化 prompt——这是取舍不是缺陷。如果换 LangGraph，代码量会翻倍且被框架绑死。

### 挑刺 #4：保底盲评你自己做 human proxy，公信力何在？

> 答：**没有公信力，这是明说的**。[__log/2026-07-27-phase3-blind-eval/README.md](__log/2026-07-27-phase3-blind-eval/README.md) 顶部"局限性声明"就 3 条：**同源偏差 / 提示暴露 / 单人标注**。报告数字只作为**内部 iteration 参考**，不作为对外可信度证据。方案书 §4.4 明写"分数不宣称与用户真实满意度相关"。要正式采用需替换真人独立盲评（≥2 位标注员 + Cohen's Kappa > 0.6）。**这个坦诚本身也是设计的一部分**——不吹牛不给自己挖坑。

### 挑刺 #5：Phase 4 只跑 1 次真实迭代，方案书要求 10 次

> 答：坦诚——**方案书要求 10 次是长期观测目标，本 commit 演示 1 次是"验证 pipeline 打通"**。理由三点：
> 1. 单次迭代 = ~50-100 次 LLM 调用（DeepSeek），10 次 ≈ 25-50 分钟纯 API 时间 + 网络重试
> 2. Phase 4 交付**价值在于机制**——六步 pipeline + L1 auto + 归档路径完整；成功率是长期统计指标
> 3. `runs/failures/` 和 `runs/suggestions/` 已就位，后续每次 `skillforge evolve` 自然累积，几周内可达 10+ 次

**替代兑现方式**：面试演示时**现场跑一次 evolve**（3 分钟），加上 `runs/` 已有归档，等效证据。

---

## 简历数字 → 证据路径映射

| 简历行 | 数字 | 证据路径 |
|---|---|---|
| `Recall@1 ≥ 80%` | **98%** | `python scripts/eval_router.py --use-llm` |
| `Recall@3 ≥ 90%` | **100%** | 同上 |
| `40 条基线（32 dev + 8 hidden）` | 40 | `evaluation_sets/baseline_dev.json` + `baseline_hidden.json` |
| `50 条硬负例` | 50 | `evaluation_sets/router_negatives.json` |
| `10 条 P0` | 10 | `evaluation_sets/p0_cases.json` |
| `pytest 全绿` | **74/74 · 4.4s** | `pytest tests/ -v` |
| `~900 行 Python` | 实际 4907（含 tests） | `wc -l src/skillforge/**/*.py tests/*.py` |
| `SQLite 状态机 4 步 + Watchdog` | 4 步 + 24h | `src/skillforge/state_machine.py` |
| `元 Agent 成功率 ~30%` | 1/3=33%（1 次迭代） | `runs/failures/` + `runs/suggestions/` |
| `SkillEvolver 六步` | 6 步 | `src/skillforge/evolver.py` L1-L400 |
| `Judge 配对比较` | A/tied/B | `src/skillforge/evaluator/judge.py` |
| `bge-small-zh-v1.5 本地` | 512 维 | `models/models/AI-ModelScope--bge-small-zh-v1.5/` |
| `10 次真实迭代日志` | **1 次已跑完**（10 次基础设施就位） | `runs/failures/*.md` + `runs/suggestions/*.md` |

---

## 我的 5 条"不知道"边界

**面试官问到以下问题，我会明确说"这块我没研究"**：

1. **正则引擎 NFA vs DFA 实现细节** —— explain_regex skill 里 er_d10 就是这类"超范围"，Constraints 里明写建议深入学正则引擎源码
2. **bge-small vs bge-m3 vs BM25 的召回精度对比** —— 我只在 bge-small 上跑了评测，没做过 A/B
3. **hello-agents 内部 ReAct 循环具体怎么实现** —— 我用了 SimpleAgent 单轮，没深入 ReActAgent 源码
4. **DeepSeek 模型的 tool_call 协议与 OpenAI 是否 100% 兼容** —— tripplanner 项目用过 HelloAgentsLLM 走 OpenAI 兼容接口没问题，但没做过协议 diff
5. **生产级别的评估集 Cohen's Kappa / Fleiss's Kappa 计算** —— 保底盲评只做了简单一致率，没做统计意义上的 inter-rater agreement

**这 5 条"不知道"不是知识空洞而是能力边界诚实标注**。方案书 A.2 违禁清单第 4 条就是"承诺不了的不吹牛"。

---

## 面试前 5 分钟自查表

- [ ] `git log --oneline -5` 4 个 Phase commit 都在
- [ ] `pytest tests/ -q` 74 通过
- [ ] `skillforge demo` 能跑
- [ ] `.env` 里 LLM_API_KEY 可用
- [ ] `models/` bge 模型在
- [ ] 方案书 v3 + ARCHITECTURE.md + __log/ 都能打开
- [ ] `runs/failures/*.md` + `runs/suggestions/*.md` 都在（元 Agent 演示证据）
- [ ] 打开 [SkillForge-项目方案书-v3.md](SkillForge-项目方案书-v3.md) §7 grill-me（备份深度答案）
