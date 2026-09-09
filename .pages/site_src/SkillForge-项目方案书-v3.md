# SkillForge

**Agent Skill 自进化工厂 —— 项目方案书 v3**

基于 hello-agents 框架扩展 · 面向 Agent 工程师面试备考

---

## 目录

- 一、项目概述
- 二、面试引导策略
- 三、技术选型
- 四、架构设计（5 个核心组件）
- 五、前置知识
- 六、实施计划（4 周 × 4 Phase）
- 七、面试应答模板（深度版）
- 附录：框架扩展关系 + 一致性口径清单

---

## 一、项目概述

SkillForge 是一个**元 Agent 系统**——它不是"用 Skill 干活的 Agent"，而是"**生产 Skill 的 Agent**"。核心闭环是：**收集失败案例 → 分析根因 → 生成候选改进 → 评估 → 棘轮机制卡不退步 → 分级发布**，让 Agent Skill（SKILL.md）像软件资产一样可评测、可演化、可版本管理。

**一句话定位**：做一个"造 Skill 的 Agent"，而不是"用 Skill 干活的 Agent"。

**项目规模**：约 900 行 Python，4 周业余时间；仅依赖 hello-agents + 标准库 + sentence-transformers；无外部 DB 服务；CLI 模式即可完整演示。

**面试价值**：项目位于 Meta 层（不是应用型），与业务型项目（如旅游助手、客服 Agent）是**正交定位**——两类项目考察不同能力，无高低之分。面试引导采用三层递进：**第一层抛概念**（Skill vs function call / Agent 主导渐进式披露 / 元 Agent 半自动闭环），**第二层展开实现**（use_skill 机制、三层路由、棘轮机制），**第三层展示取舍**（评估可信度、跨存储一致性、Judge 偏见）。目标覆盖 **20 分钟深度技术讨论**。

**关键工程原则**（贯穿全文）：

1. **Agent 主导，框架不拦截 prompt** —— 渐进式披露由 Agent 通过 `use_skill(name, reason)` 显式发起，加载行为可归因、可评估
2. **规则优先，Embedding 补充，LLM 兜底** —— 三层路由级联，不写死覆盖率百分比
3. **结构分不阻断发布，效果分才是发布门槛** —— 八维评估分工明确
4. **元 Agent 是候选生成器，不是最终决策者** —— L1 自动、L2 REVIEW、L3 只出建议
5. **SQLite 是唯一发布事实源** —— 三存储写入按状态机固定顺序，任一步失败旧版本继续生效
6. **评估边界诚实声明** —— 相对回归筛选器，不是绝对质量证明器

---

## 二、面试引导策略

按"三层递进"设计话术：第一层抛钩子引诱追问，第二层展开实现细节，第三层展示工程取舍。目标覆盖 20 分钟深度讨论。

---

### 2.1 第一层：30 秒开场（抛钩子）

面试官问"介绍一下项目"时的标准回答。三句话对应三个钩子，每个钩子都留有可追问的技术缺口。

| 你主动说 | 埋的钩子（面试官可能追问） |
|---|---|
| 我做了一个叫 SkillForge 的元 Agent 系统，它不是"用 Skill 干活的 Agent"，而是"生产 Skill 的 Agent"。 | Skill 和 function call / tool 有什么区别？为什么值得单独抽象？ |
| Skill 的加载走渐进式披露，只有两层：元数据索引随 system prompt 一起注入，完整说明书按需返回。加载不是框架自动展开的，是 Agent 在 ReAct 循环里自己调 `use_skill(name)` 加载的。 | 那 Skill 是怎么被 Agent 看到的？框架有没有拦截 prompt？多个候选怎么选？ |
| 生成新 Skill 走一条半自动闭环：元 Agent 提候选 → 八维评估器打分 → 棘轮机制卡不退步 → 按 L1/L2/L3 分级发布，只有低风险的 L1 是自动发布。 | 评估怎么保证不是自吹自擂？棘轮会不会卡死改进？元 Agent 成功率多少？ |

---

### 2.2 第二层：追问后展开实现

前三个是核心技术钩子，任何一个都足以聊 5 分钟。

| 面试官追问 | 你的回答（展开技术细节） |
|---|---|
| Skill 是怎么注入上下文的？框架帮 Agent 展开了吗？ | 只注入元数据索引，不预注入 Body。每个已注册 Skill 我提取 `name + description + use_when` 三个字段，拼成一段文字索引，随 system prompt 一次性给 Agent。**注意这不是 Anthropic 工具协议里的 tool 列表，就是一段文字索引**。同时我注册一个特殊工具 `use_skill(name, reason)`，Agent 在 ReAct 循环里读到索引后自己判断需要哪个 Skill，主动调用 `use_skill`，框架把该 Skill 的完整 Body 作为 tool 结果拼回 conversation，之后 Agent 用返回的说明书继续正常 ReAct。**框架不拦截 prompt、不静默修改 system_prompt**，加载完全由 Agent 决策，每次 `use_skill` 都写路由日志便于事后归因。 |
| 多个 Skill 用同一个 MCP 工具怎么选？会不会打架？ | 走三层路由级联去重：先规则层按 keyword + 权重命中，top-1 领先 top-2 大于阈值就直接返回；否则进 embedding 层，Skill 描述用"检索卡片"结构化编码（capability + use_when + examples + not_for 四段拼接）返回 top-K；还定不下来才进 LLM 层做二选一。**同一意图下最终只放行一个 Skill**，避免两个 Skill 抢同一次调用。工具本身是共享的，Skill 是使用工具的"套路 + 边界"，路由选中的是套路。 |
| 三层路由每一层的 fallback 条件到底是什么？为什么这么切？ | 规则层 fallback 条件是"top-1 与 top-2 分差 < 阈值"或"没有 keyword 命中"，触发就落到 embedding；embedding 层 fallback 条件是"top-K 里没有明显领先者"或"最高相似度低于置信阈值"，触发就落到 LLM。切法背后有个成本-延迟考虑：规则层 <5ms 覆盖高置信路径，embedding ~50ms 覆盖语义相近但没有关键词命中的路径，LLM ~500ms 只用在真正歧义的少数场景。**真实分布 Phase 2 结束基于路由日志统计**，我不写死百分比。 |

---

### 2.3 第三层：展示工程取舍

四个问题都是面试官用来试深度的，需要给出取舍的理由，而不是只报结论。

| 面试官追问 | 你的回答（展开取舍） |
|---|---|
| 为什么不直接上纯 embedding？三层是不是过度设计？ | 纯 embedding 有两个成本我不想吃：一是延迟，规则层能秒杀的高频路径没必要过一次编码；二是可控性，embedding 在"语义相近但意图不同"的 Skill 之间容易误命中，硬负例评测集里这种 case 特别多。规则层帮我承接高置信路径，embedding 承接语义相似路径，LLM 兜底歧义。**分层是为路由日志能归因**——出错时能定位是哪一层选错了、要补哪一层。纯 embedding 一个黑盒选错了没法归因。 |
| 棘轮机制会不会导致改进停滞？改一下就说"退步了"卡住？ | 会有这个风险，所以我做了两层门槛。硬门槛是不可退步项：总分不退、效果分不退、任务完成度和鲁棒性下降 ≥ 5%、任一 P0 用例由通过变失败——这四类自动阻断。软门槛是变化 ≥ 10% 触发人工审核 REVIEW，**不阻断，只是要人看一眼**。另外 Judge 是配对比较（新旧输出让 Judge 判"A 好 / 持平 / B 好"）不是打绝对分，能一定程度对抗分数漂移。**如果十次全被卡也是有价值的信号**——说明我的候选生成策略需要重新想，而不是把棘轮放宽。 |
| 元 Agent 成功率就 30%，这有什么价值？为啥不搞成全自动？ | 30% 是我坦诚的预期数字（10 次迭代约 3 次通过棘轮），价值不在替代人做发布决策，在于**自动过滤明显错的改动**。三个具体收益：一是失败 patch 全归档到 `runs/failures/` 附根因分析，等于给未来的候选生成攒了负样本；二是通过棘轮的候选人工只需要 review 1-2 个，而不是 review 全部 3-5 个；三是自动跑评估集这一步的工时被完全省掉。**全自动发布我明确不做**——评估集只有 40 条，Judge 又有自评偏见风险，这个可信度不足以支持全自动化，元 Agent 的定位是"候选生成器"不是"最终决策者"。 |
| 三处写一半崩了怎么办？你 Git、SQLite、JSONL 三个存储怎么保证一致性？ | 我把 SQLite 当**唯一发布事实源**，只有 `skills.current_release_id` 指向哪个 release，那个才是线上生效版本。写入协议固定四步：SQLite 插 PREPARING 行 → Git commit 回写 commit_hash → JSONL 追加评估记录 → SQLite 原子 UPDATE PREPARING → PUBLISHED 同时更新 `current_release_id`。**任何一步崩，SQLite 保留 PREPARING、`current_release_id` 不变，旧版本继续生效**；Git commit 和 JSONL 记录留着供审计。加一个 Watchdog 定时把超 24h 未 PUBLISHED 的行改成 ABANDONED。release_id 是 UUID 全局唯一，第四步 UPDATE 加了状态条件（`WHERE status='PREPARING'`），重放整个流程幂等。 |


---

## 三、技术选型

### 3.1 框架

| 组件 | 选型 | 理由 | 是否已掌握 |
|---|---|---|---|
| Agent 基类 | `SimpleAgent` + `ReActAgent`（hello-agents Lesson 1-2） | 已在课程练习中跑通，元 Agent 直接继承 `SimpleAgent`，无需引入新框架 | ✅ |
| 工具注册 | `ToolRegistry`（hello-agents） | 特殊工具 `use_skill(name, reason)` 走同一注册机制，与已有工具协议一致 | ✅ |
| 记忆 | `MemoryTool`（hello-agents） | 用于元 Agent 迭代过程中的短期上下文，不引入向量记忆 | ✅ |
| 元 Agent | `SkillEvolver` 继承 `SimpleAgent` | 复用 ReAct 循环，只在系统提示和工具集上定制；不新造调度框架 | ✅（基类）/ 🟡（本项目新写） |
| LLM | `HelloAgentsLLM`（DeepSeek，已有 API Key） | 无需申请新账号，成本可控，支持长上下文 | ✅ |
| Judge | DeepSeek（同上 LLM），采用新旧输出**配对比较** | 复用现有 API；配对协议降低分数漂移；自评偏见通过人工盲评校准兜底 | ✅（API）/ 🟡（配对协议） |
| Embedding | `bge-small-zh-v1.5` 本地起（sentence-transformers）；`bge-m3` 精度模式可选 | 中文 Skill 描述场景表现好；本地跑无外部依赖；小模型 CPU 可推 | 🟡（需补 sentence-transformers 本地服务） |

---

### 3.2 存储

| 数据 | 方案 | 理由 |
|---|---|---|
| Skill 源码（SKILL.md） | Git 本地仓库 | 天然 diff 审计、版本回滚，与开发者习惯一致，无需自造版本系统 |
| 元数据 + 发布状态 + `current_release_id` | SQLite | **唯一发布事实源**；单文件零运维；支持事务，天然实现状态机原子切换 |
| 评估轨迹（每条评估一行 JSON） | JSONL | 追加写、易 `grep`/`jq` 分析；不承担一致性责任，只作为审计流水 |

#### 跨存储一致性协议

三个存储承担不同职责，**发布状态以 SQLite 为唯一事实源**：Git 只是源码库，JSONL 只是审计流水，两者都不代表"当前生效版本"。当外部（路由层、Agent、评估器）询问"某 Skill 当前发布版本是什么"时，答案始终来自 SQLite 的 `skills.current_release_id`。这样即使 Git 或 JSONL 中间步骤失败，也不会污染线上版本。

写入采用固定顺序的状态机，`release_id`（UUID）在第 1 步生成并贯穿全流程作为幂等键：

1. **SQLite 插入 release row**：状态置为 `PREPARING`，写入 `release_id`
2. **Git 提交候选**：生成 `commit_hash` 回写 SQLite 对应 release row
3. **JSONL 追加评估记录**：每条记录携带 `release_id`
4. **SQLite 原子更新**：`PREPARING → PUBLISHED`，同时把 `skills.current_release_id` 指向新 release_id（`UPDATE ... WHERE status='PREPARING'`，非该状态不生效）

**失败语义**：任一步失败则 SQLite 状态停留在 `PREPARING`，`current_release_id` 不变，旧版本继续生效对外服务。已写入的 Git commit 和 JSONL 记录**不回滚**，保留供审计（发布失败本身也是有价值的历史）。

**Watchdog**：后台定时任务清理超过 24 小时仍未 `PUBLISHED` 的 `PREPARING` 行，状态改为 `ABANDONED`；Git 与 JSONL 记录仍不清理。

**幂等**：因第 4 步 `UPDATE` 带有 `WHERE status='PREPARING'` 条件，整套流程可安全重放 —— 已 `PUBLISHED` 的行不会被二次修改，中断后重试从任意步骤重入都收敛到同一结果。

---

### 3.3 评估

| 维度 | 方法 | 工具 |
|---|---|---|
| 结构分静态检查 | Pydantic 校验 SKILL.md 的 YAML Schema：`name` / `description` / `dependencies` / `use_when` / `trigger.keywords` / `not_for` 齐全性，依赖工具可访问性；**不阻断发布，仅完整性检查** | Pydantic + 自写校验器 |
| 效果分 Judge 配对比较 | 给 Judge 输入同一场景 + 新旧两次输出，让其判 "A 更好 / 持平 / B 更好"，不评绝对分；覆盖任务完成度、鲁棒性、可读性三个维度 | DeepSeek（HelloAgentsLLM） + 配对 prompt 模板 |
| 客观指标采集 | 运行时埋点采集调用轮数、token 数、端到端延迟；效率维度**不用 LLM 主观打分** | Python 装饰器 + SQLite/JSONL 记录 |
| 棘轮机制 | 双层门槛 + 分级阻断：**硬门槛**（总分不退步、效果分不退步、任务完成度/鲁棒性下降 ≥ 5%、任一 P0 用例由通过转失败）自动阻断发布；**软门槛**（任一维度变化 ≥ 10%，含上升）触发人工 REVIEW | 自写规则引擎，读取评估 JSONL |
| 评估集构造 | 40 条基线（**32 条开发集**可迭代可见 + **8 条隐藏集**发布前跑、不告诉元 Agent），从中标注 **10 条 P0** 覆盖核心链路；路由层另备 **50 条硬负例**做 Recall 评测 | 手工标注 + JSONL 存储；Phase 3 追加 ≥ 20 条人工盲评校准 Judge 偏差 |

**评估可信度声明**：本评估器仅适用于固定场景下的相对回归与候选筛选，不足以证明绝对质量，不足以支持高风险改动的全自动发布，分数不宣称与用户真实满意度相关；能力边界通过每 20 次评估抽 1 条的人工盲评逐步验证，Phase 3 出具一次 Judge/人工偏差报告。


---

## 四、架构设计（5 个核心组件）

五个组件按数据流串联：**SKILL.md 规范**（定义 Skill 结构） → **渐进式披露引擎**（Agent 主动加载） → **三层路由**（多候选选一） → **八维评估器 + 棘轮**（版本准入门槛） → **SkillEvolver 元 Agent**（生成候选改进）。

架构关系图（文字版）：

```
Agent ReAct 循环
   │
   ├── 读元数据索引（4.1 SKILL.md → 4.2 索引层）
   ├── 走三层路由挑候选（4.3 规则 → embedding → LLM）
   ├── 调 use_skill(name, reason)（4.2 特殊工具）
   │      └── 返回完整 Body 拼回 conversation
   └── 执行任务

评估闭环（后台异步）
   │
   ├── 收集运行日志
   ├── 八维评估（4.4：结构分 + 效果分 + 客观指标）
   ├── 棘轮门槛（4.4：硬门槛阻断 + 软门槛 REVIEW）
   └── SkillEvolver 生成候选（4.5：L1 自动 / L2 REVIEW / L3 建议）
```

---

### 4.1 SKILL.md 定义规范

每个 Skill 是一个 Markdown 文件，YAML frontmatter + Markdown body。YAML 字段既支撑索引层元数据、又支撑路由层的结构化检索卡片编码。

**YAML 字段**：

| 字段 | 类型 | 说明 | 示例 |
|---|---|---|---|
| `name` | string | Skill 标识（snake_case，全局唯一） | `weather_query` |
| `version` | semver | 语义版本号 | `1.0.0` |
| `description` | string | 一句话描述，索引层可见 | `查询任意城市实时天气` |
| `use_when` | string | 触发场景描述，索引层 + 检索卡片使用 | `用户询问某城市当前/未来天气` |
| `not_for` | list[string] | 明确不适用的场景（提高路由准确率） | `["历史天气查询", "气候趋势分析"]` |
| `dependencies` | list[string] | 依赖的工具/MCP 服务器 | `["amap_mcp_server"]` |
| `trigger.keywords` | list[string] | 候选关键词（路由排序信号，不做自动展开） | `["天气", "温度", "下雨"]` |
| `examples` | list[string] | 少量正例（检索卡片编码使用） | `["北京今天天气", "上海会下雨吗"]` |
| `evaluation.last_score` | float | 上次评估总分（元数据） | `8.2` |
| `evaluation.last_release_id` | uuid | 上次发布的 release_id | `550e8400-...` |

**Markdown Body 结构**（`use_skill` 返回的完整内容）：

```markdown
## Overview
[Skill 概览：解决什么问题、边界在哪]

## Instructions
[具体使用指导：如何调用依赖工具、参数如何构造、结果如何解读]

## Examples
[完整正例：输入 → 期望 Agent 行为 → 输出]

## Constraints
[约束条件：不能做什么、什么情况下要拒绝、边界处理]
```

**设计要点**：
- **去掉旧版 `disclosure_level` 字段**：Agent 主导后不再需要分层配置，一律走"元数据索引 → use_skill 拉 Body"两层
- **`use_when` 与 `not_for` 成对出现**：Agent 判断需要哪个 Skill 时同时看正反例，路由 embedding 编码也用两者
- **`trigger.keywords` 降级为排序信号**：只影响规则层匹配分数与索引展示顺序，不做自动展开

---

### 4.2 渐进式披露引擎（Agent 主导）

**核心决策**：只有两层，**Agent 主动加载，框架不拦截 prompt**。

| 层级 | 加载内容 | Token 消耗 | 加载时机 |
|---|---|---|---|
| **元数据索引层** | 所有已注册 Skill 的 `name + description + use_when` 拼成的文字索引 | 单 Skill ~80 token，10 个 Skill 约 800 token | 系统初始化时随 system prompt 一次性注入 |
| **完整实现层** | 该 Skill 的完整 Body（Overview + Instructions + Examples + Constraints） | ~2-5K token / Skill | Agent 主动调 `use_skill(name, reason)` 时按需返回 |

**注意**：元数据索引是 **一段文字**，不是 Anthropic 工具协议里的 `tools` 参数——不占 tool slot，也不受 tools 数量限制。

**特殊工具签名**：

```python
def use_skill(name: str, reason: str) -> str:
    """
    加载指定 Skill 的完整说明书。
    
    Args:
        name: SKILL 标识（必须在索引里存在）
        reason: 加载理由（必填，用于路由日志归因）
    
    Returns:
        Skill 完整 Body 文本；找不到返回错误信息，Agent 应降级到只用 description
    """
```

**加载流程**：

```
1. 系统启动
   ├── 扫描 skills/ 目录，Pydantic 解析所有 SKILL.md
   ├── 从 SQLite 读每个 Skill 的 current_release_id 对应的 commit_hash
   ├── 拼元数据索引 → 追加到 system prompt
   └── 注册 use_skill 到 ToolRegistry

2. Agent ReAct 循环
   ├── 读到索引，判断"这次该用哪个 Skill"
   ├── 主动调 use_skill("weather_query", "用户问北京今天天气")
   ├── 框架从 Git 读该 Skill 的完整 Body，作为 tool 结果拼回 conversation
   ├── 写路由日志（含 name / reason / 命中层次 / 延迟）
   └── Agent 用返回的说明书继续正常 ReAct
```

**为什么这么做**（面试可讲）：

1. **加载器拦截 prompt 会破坏 ReAct 可解释性** —— Agent 不知道自己看到的 tools 为什么变化，评估器也无法归因
2. **`use_skill` 是显式 action，可被评估器直接观察** —— 每次加载都有 `reason` 归因，路由日志可分析
3. **trigger 自动展开在语义相近 Skill 之间容易误触发** —— 比如"写周报"和"写会议纪要"，靠 trigger 自动展开会双双命中

**降级策略**：
- `use_skill` 参数错误（name 不在索引）：返回错误信息，Agent 降级到只用 description 完成任务或明确告知用户
- `use_skill` 加载 Body 失败（Git 读文件异常）：返回降级信息 + description，Agent 决定是否重试

---

### 4.3 三层路由（去掉硬编码百分比）

**级联结构**（不写死覆盖率百分比）：

| 层级 | 方法 | 延迟 | 触发下一层的 fallback 条件 |
|---|---|---|---|
| **规则层** | keyword 命中 + 权重排序 | <5ms | top-1 与 top-2 分差 < 20；或没有任何 keyword 命中 |
| **Embedding 层** | 结构化检索卡片编码 + 余弦相似度 top-K | ~50ms（本地 bge-small） | top-K 无明显领先者；或最高相似度低于置信阈值 |
| **LLM 层** | 把 top-K 候选描述交给 LLM 做二选一 | ~500ms | —（兜底层，不再向下） |

**结构化检索卡片编码规则**（Embedding 层核心）：

每个 Skill 编码为一段结构化文本，四段拼接：

```
[Capability] {description}
[Use When] {use_when}
[Examples] {examples[0]} | {examples[1]} | ...
[Not For] {not_for[0]} | {not_for[1]} | ...
```

`Not For` 段的作用：让"语义相近但不该匹配"的场景在向量空间中被主动推远。比如"写周报"的 not_for 里明确列出"写会议纪要"，两者向量距离就会拉开。

**Embedding 模型选择**：
- 默认：`bge-small-zh-v1.5`（本地 CPU 可推，中文场景表现好，模型体积 ~100MB）
- 精度模式：`bge-m3`（多语言 + 更强表征，体积 ~2GB，需 GPU 或耐心）
- 加载方式：sentence-transformers 直接 `SentenceTransformer(model_name)`

**LLM 层调用 prompt**（简化）：

```
用户查询：{query}
候选 Skill（按 embedding 相似度排序）：
1. {skill_a.name}: {skill_a.description} (相似度: 0.85)
2. {skill_b.name}: {skill_b.description} (相似度: 0.82)
3. {skill_c.name}: {skill_c.description} (相似度: 0.79)

请判断哪个 Skill 最匹配这个查询，只输出 name。如果都不匹配，输出 NONE。
```

**准入门槛**（Phase 2 交付验证）：
- 硬负例评测集 50 条（务实规模，独立开发者可造）
- **Recall@1 ≥ 80%、Recall@3 ≥ 90%**
- 达不到就退回补规则或补检索卡片，而不是硬顶指标

**真实覆盖率分布**：Phase 2 完成后基于路由日志统计（不预设 80/15/5）。

---

### 4.4 八维评估器 + 棘轮机制

#### 八维定义（结构分 40% + 效果分 60%）

**结构分（40%，Pydantic 静态检查，不阻断发布）**：

| 维度 | 检查项 | 权重 |
|---|---|---|
| Schema 完整性 | `name / description / dependencies / use_when` 齐全 | 15% |
| Trigger 质量 | `keywords` 精确无歧义，`not_for` 明确 | 10% |
| Prompt 健壮性 | Body 中包含边界条件说明（Constraints 段） | 10% |
| 依赖可用性 | 声明的工具/MCP 可访问（启动时探测） | 5% |

结构分**只用于发现表单问题**，不作为发布阻断门槛。目的是让开发者能收到"你 SKILL.md 没写完整"的反馈，但不因此卡住发布。

**效果分（60%，Judge 配对 + 客观指标，发布门槛）**：

| 维度 | 评判标准 | 权重 | 数据源 |
|---|---|---|---|
| 任务完成度 | 能否正确完成任务 | 25% | **Judge 配对比较** |
| 鲁棒性 | 异常输入时降级或明确拒绝 | 15% | **Judge 配对比较** |
| 效率 | 调用轮数 / token / 端到端延迟 | 10% | **客观指标采集（非 LLM 打分）** |
| 可读性 | SKILL.md 描述清晰度 | 10% | Judge 配对比较 |

#### 棘轮机制（双层门槛 + 分级阻断）

**硬门槛（自动阻断发布，任一触发即挡）**：

1. 总分退步
2. 效果分退步
3. 任务完成度下降 ≥ 5%
4. 鲁棒性下降 ≥ 5%
5. 任一 P0 用例由通过变失败

**软门槛（触发人工审核 REVIEW，不阻断）**：

- 任一维度变化 ≥ 10%（含上升——变好也要看一眼，防 Judge 被表面漂亮的输出骗过）

#### Judge 配对比较协议

不评绝对分，评相对：

```
输入：同一场景 + 新版本输出 A + 旧版本输出 B
输出：A 更好 / 持平 / B 更好（三选一）+ 理由

覆盖维度：任务完成度、鲁棒性、可读性（不评效率，效率走客观指标）
```

好处是能对抗 Judge 分数漂移（绝对分容易被措辞、长度带偏），代价是每次评估要跑两次输出。

#### 评估集构造

- **40 条基线**：
  - 32 条**开发集**（元 Agent 可见，迭代反馈）
  - 8 条**隐藏集**（发布前跑，不告诉元 Agent，防过拟合）
  - 从中标注 **10 条 P0**（覆盖核心链路，硬阻断标准）
- **50 条硬负例**（路由层专用，测 Recall）

#### Judge 模型选择与偏见缓解

- **主 Judge**：DeepSeek（HelloAgentsLLM，已有 API）
- **自评偏见风险坦诚**：被评 Skill 若也走 DeepSeek，存在自评偏见
- **缓解**：
  1. 配对比较代替绝对打分
  2. 每 20 次评估抽 1 条**人工盲评校准**
  3. Judge 与人工分歧 > 30% 调 Judge prompt
  4. Phase 3 保底跑一次 ≥ 20 条人工校准，出偏差报告

#### 评估可信度声明（必须写进方案书）

1. 本评估器**仅适用于固定场景下的相对回归 + 候选筛选**
2. **不足以证明绝对质量**
3. **不足以支持高风险改动的全自动发布**
4. **分数不宣称与用户真实满意度相关**
5. 能力边界通过人工盲评逐步验证，Phase 3 出偏差报告

---

### 4.5 SkillEvolver 元 Agent（半自动）

**定位**：**候选生成器 + 初筛器，不是最终决策者**。继承 `SimpleAgent`，职责是根据失败案例自动生成 Skill 改进候选。

**六步流程**（① 完整不省略）：

```
① 收集失败案例
   ├── 从评估 JSONL 拉出：分数低于阈值 / 棘轮拦截 / P0 挂掉的迭代
   └── 聚类去重（相似失败合并处理）

② 分析根因
   ├── LLM 判断失败类型：
   │      trigger 不准（路由拿错）
   │      prompt 模糊（Instructions 不清晰）
   │      依赖失效（工具挂了）
   │      边界处理缺失（Constraints 没覆盖）
   └── 输出根因标签 + 置信度

③ 生成候选改进
   ├── 一次生成 3-5 个 patch（不同角度尝试）
   ├── 每个 patch 附推理链
   └── 标记 patch 级别（L1/L2/L3）

④ 验证
   ├── 每个候选跑完整评估集（含隐藏集）
   ├── 跑棘轮门槛
   └── 采集客观指标

⑤ 分级发布
   ├── L1（补充 examples / not_for / 完善描述）
   │      └── 验证通过 → 自动发布
   ├── L2（修改 trigger / instructions）
   │      └── 验证通过 → 挂 REVIEW，人工确认后发布
   └── L3（修改工具权限 / 安全约束）
   │      └── 只生成建议 patch，不自动改，人工重写

⑥ 归档
   ├── 成功 patch → Git commit + SQLite PUBLISHED
   └── 失败 patch → runs/failures/<release_id>.md（含根因分析）
          └── 不进主分支，作为负样本沉淀
```

**分级标准明细**：

| 级别 | 改动范围 | 风险 | 自动化程度 |
|---|---|---|---|
| L1 | `examples` / `not_for` / `description` 补充 | 低（不改语义边界） | 验证通过自动发布 |
| L2 | `trigger.keywords` / Instructions Body 修改 | 中（可能影响路由和行为） | 验证通过挂 REVIEW，人工确认 |
| L3 | `dependencies` 修改 / Constraints 安全约束 | 高（工具权限/安全边界） | 只生成建议，人工重写并复核 |

**预期成功率坦诚**：**~30%**（10 次迭代约 3 次通过棘轮）。价值不在替代人做发布决策，在于：

1. **自动淘汰明显错的改动**——70% 被挡在门外，人只需 review 通过棘轮的 30%
2. **负样本沉淀**——失败 patch 全归档附根因，等于给未来的候选生成攒模式库
3. **评估闭环压测**——反复冲击评估集，暴露 Judge 偏见与评估集覆盖漏洞

**Phase 4 交付要求**：至少跑 **10 次真实迭代**，成功/失败案例全部留档，形成"元 Agent 效果证据链"（面试必问）。

---

## 五、前置知识

| 知识模块 | 状态 | 说明 |
|---|---|---|
| hello-agents Lesson 1-5 | ✅ | 课程练习均已完成，理解 Agent 循环、工具调用、记忆机制 |
| ToolRegistry / SimpleAgent / MemoryTool | ✅ | 已在课程练习中直接使用，元 Agent 与特殊工具 `use_skill` 均基于此扩展 |
| Git 基本命令 | ✅ | 熟悉 add/commit/diff/log，够用于程序化调 `subprocess` 管理 Skill 源码 |
| sentence-transformers 本地起 embedding 服务 | 🟡 | Phase 2 前需补：`bge-small-zh-v1.5` 加载、批量编码、余弦相似度检索、CPU 内存占用测试 |
| SQLite 事务与状态机 | 🟡 | Phase 3 前需补：`BEGIN IMMEDIATE` 与条件 `UPDATE` 的原子性、跨进程锁、Watchdog 定时任务的实现方式 |
| 硬负例评测集构造方法 | 🟡 | Phase 2 前需补：如何针对 Skill 描述系统性构造语义邻近但不该命中的用例，避免 Recall 指标虚高 |


---

## 六、实施计划

| 阶段 | 内容 | 交付门槛 | 代码量 | 时间 |
|---|---|---|---|---|
| **Phase 1** | SKILL.md YAML Schema（Pydantic）+ Body 解析；Skill 索引层注入 system prompt；特殊工具 `use_skill(name, reason)`；Body 按需返回 | 10 条基础链路测试通过：① `use_skill` 正常调用 ② 索引里没有的 skill_name 返回错误 ③ 多候选排序稳定 ④ Body 加载失败降级到只用 description ⑤ 参数错误的 `use_skill` 优雅处理 | ~150 行 | 第 1 周 |
| **Phase 2** | 规则引擎（keyword + 权重）；本地 `bge-small-zh-v1.5` embedding；结构化检索卡片编码（`capability + use_when + examples + not_for`）；LLM 兜底 top-K 二选一；50 条硬负例评测集 | Recall@1 ≥ 80% AND Recall@3 ≥ 90%；30 条路由边界测试通过（规则命中 / embedding 命中 / LLM 兜底 / 无匹配拒绝 / 硬负例区分） | ~250 行 | 第 2 周 |
| **Phase 3** | Pydantic 结构分静态检查；Judge 新旧配对比较；客观指标采集器（轮数/token/延迟）；棘轮硬门槛 + 软门槛；SQLite 状态机 + 三存储写入协议；40 条基线（32 开发 + 8 隐藏隔离）；≥ 20 条人工盲评校准 | 可跑通一次完整"评估 → 棘轮 → 发布"闭环；产出 Judge / 人工偏差报告；发布状态在任一中间步骤失败时旧版本仍生效 | ~300 行 | 第 3 周 |
| **Phase 4** | `SkillEvolver` 继承 `SimpleAgent`：根因分析 → 3-5 个候选 patch 生成 → 验证 → L1/L2/L3 分级发布；失败归档 `runs/failures/<release_id>.md`；端到端演示脚本 | 完成 ≥ 10 次真实迭代，全部留档；产出 ≥ 3 条成功案例 + ≥ 5 条失败案例可现场展示；演示脚本一键复现 | ~200 行 | 第 4 周 |

**总计**：~900 行代码，4 周业余时间。

### 排期风险与降级预案

4 周业余时间偏紧张，评估集手工标注、人工盲评校准、元 Agent 十次迭代都是消耗性动作，任一环节延误都会挤压后续阶段。降级顺序按"评估闭环 > 面试演示完整度 > 元 Agent 自动化程度"排序：

- **Phase 1-3 是硬底线**：渐进式披露引擎、三层路由、评估器 + 棘轮 + 跨存储一致性必须保交付。这三层撑起面试可讲的核心技术叙事，任何一层缺失都会让方案沦为"idea 展示"。
- **Phase 4 允许收敛到 L1**：若第 4 周只剩 3-4 天，元 Agent 只跑 L1 分级（补 `examples` / `not_for` / 完善描述，验证通过自动发布），L2（改 trigger/instructions 挂 REVIEW）与 L3（工具权限/安全约束只出建议）暂缓；迭代次数从 10 次降到 5 次，但成功/失败案例仍需完整归档。
- **评估集数量可下调但结构不能改**：40 条基线可临时压到 30 条（开发 24 + 隐藏 6），P0 数量维持 10 条不动，隐藏集比例不低于 20%；硬负例 50 条可压到 30 条，但不能取消（否则 Recall 指标失去意义）。
- **人工盲评校准不可省**：即使总样本量减半（≥ 20 条降到 ≥ 10 条），Judge/人工偏差报告仍需在 Phase 3 结束时产出，这是评估可信度声明的唯一支撑证据。
- **代码量透支的止损点**：任一 Phase 超 1.5 倍预估代码量（如 Phase 3 超过 450 行）立即回顾是否越界；越界内容优先砍"漂亮但非必要"的功能（如可视化 dashboard、CLI 交互美化）。

---

## 七、面试应答模板（深度版）

这一章是把第二章的三层递进按"演练手册"展开——每个问答独立成小节，答案更完整，可以直接照着念熟。分四个子章：抛出概念、展开实现、展示取舍、必备预案。

---

### 7.1 第一层：抛出概念

#### 标准开场话术（30 秒）

"我做了一个叫 SkillForge 的项目，定位是**元 Agent 系统——生产 Skill 的 Agent，不是用 Skill 干活的 Agent**。基于 hello-agents 框架，独立开发，代码量约 900 行。

它做三件事：第一，把 Skill 抽象成有完整生命周期的对象——描述、触发、实现、评估、版本都在一起；第二，Skill 的加载走渐进式披露，只有两层，且**由 Agent 主动调用 `use_skill` 加载，框架不拦截 prompt**；第三，新 Skill 的生成走一条半自动闭环——元 Agent 提候选，八维评估器打分，棘轮机制卡不退步，按 L1/L2/L3 分级发布。

目标不是 demo，是把评估闭环 + 跨存储一致性这些工程细节讲清楚。"

#### 埋的 3 个钩子

1. **Skill vs function call 的抽象差别**：钩子在"完整生命周期"。面试官若追问，就展开 Skill 是"套路 + 边界 + 说明书 + 评估分 + 版本记录"，function call 只是"工具函数签名"。
2. **Agent 主导渐进式披露**：钩子在"只有两层"和"框架不拦截 prompt"。面试官若追问就展开 `use_skill` 机制。
3. **元 Agent 半自动闭环**：钩子在"半自动"和"L1/L2/L3 分级"。面试官若追问就展开棘轮机制 + 分级发布策略 + 成功率坦诚。

---

### 7.2 第二层：展开实现细节

#### Q: Skill 是怎么注入上下文的？框架帮 Agent 展开了吗？

只注入元数据索引，Body 不预注入。我为每个已注册 Skill 提取 `name + description + use_when` 三个字段，拼成一段文字索引跟 system prompt 一起给 Agent。**特别注意，这只是一段文字，不是 Anthropic 工具协议里那个 tool 列表**——不占 tool slot。

然后我注册一个特殊工具 `use_skill(name: str, reason: str) -> str`。Agent 在 ReAct 循环里读到索引后自己判断"这次该用哪个 Skill"，主动调用 `use_skill("skill_x", "用户在问……")`，框架把该 Skill 的完整 Body（Instructions + Examples + Constraints）作为 tool 结果拼回 conversation。之后 Agent 拿着说明书继续正常 ReAct。

**框架不拦截 prompt、不静默改 system_prompt**。这么做的理由有三个：一是加载器拦截 prompt 会破坏 ReAct 的可解释性，Agent 不知道自己看到的东西为什么变了；二是 `use_skill` 是显式 action，评估器可直接观察归因；三是 trigger.keywords 自动展开在语义相近的 Skill 之间容易误触发。所以 keywords 我只用作路由排序的推荐信号，不做自动展开。

#### Q: 多个 Skill 用同一个 MCP 工具怎么选？会不会打架？

工具是共享资源，Skill 是"使用工具的套路 + 边界"，路由选出来的是套路，不是工具。我走三层路由级联去重：

- 规则层按 keyword + 权重命中，top-1 领先 top-2 大于阈值直接返回；
- 定不下来进 embedding 层，Skill 描述用**结构化检索卡片**（`capability + use_when + examples + not_for` 四段拼接）编码，返回 top-K；
- 还有歧义进 LLM 层，把 top-K 候选描述交给 LLM 二选一。

**同一意图下最终只放行一个 Skill**，避免两个 Skill 抢同一次调用。检索卡片里的 `not_for` 字段专门用来在 embedding 阶段就把"看起来像但其实不该匹配"的场景推开。路由结果全部写日志，事后归因是哪一层选中的、有没有走到 LLM 兜底。

#### Q: 渐进式披露具体怎么做？跟"把所有 tools 描述一次给 LLM"有什么本质不同？

本质不同在于**决策权归属**和**加载时机**。

传统做法是把所有 tools 的 schema + description 全塞进 tools 参数一次给 LLM，LLM 每轮都要在完整列表里选。这在 tools 少的时候没问题，但 Skill 是有 Instructions + Examples + Constraints 的完整说明书，全塞进去 token 撑不住。

我的做法是两层：**第一层**只塞元数据（`name + description + use_when`）——只是文字索引，一次注入；**第二层**是完整 Body，不预注入，等 Agent 自己判断需要时调 `use_skill(name)` 拉。加载动作是 Agent 主动发起的 tool call，不是框架偷偷改 prompt。

好处有三：token 只在真正用到时才付；`use_skill` 是显式 action 可归因；路由日志里能看到 Agent 每次"选择加载什么"背后的推理（`reason` 参数强制填）。代价是 Agent 得学会看索引做选择，好在 ReAct + 结构化索引对现代 LLM 不算难任务。

---

### 7.3 第三层：展示工程取舍

#### Q: 为什么不用纯 embedding？三层是不是过度设计？

不是过度设计，是分摊延迟 + 分层归因。

纯 embedding 有两个成本我不想吃。一是**延迟**：规则层能命中的高频路径 <5ms，走 embedding 得 ~50ms，白白多一个数量级；二是**可控性**：embedding 在"语义相近但意图不同"的 Skill 之间容易误命中——比如"写周报"和"写会议纪要"向量距离很近但触发场景完全不同，硬负例评测集里这种 case 特别多。

三层的分工是：规则层承接高置信精确匹配；embedding 承接语义相似但没有明确关键词的路径；LLM 只在真正歧义时兜底。**分层的另一个价值是归因**——路由日志里能看到"这次命中是规则层的哪个 keyword"、"embedding 相似度多少"、"有没有走到 LLM"。纯 embedding 一个黑盒选错了没法定位是描述写坏了、卡片结构不对、还是模型不适合。

准入门槛也务实：50 条硬负例（不是 100-200，独立开发者造不动），Recall@1 ≥ 80%、Recall@3 ≥ 90%。达不到就退回补规则或者补检索卡片，而不是硬顶指标。

#### Q: 棘轮机制会不会导致改进停滞？改一下就说"退步了"卡住怎么办？

会有停滞风险，我用**双层门槛 + 分级阻断 + 配对比较**三重设计缓解。

双层门槛：**硬门槛**（自动阻断发布）只卡四类——总分退步、效果分退步、任务完成度或鲁棒性下降 ≥ 5%、任一 P0 用例由通过变失败。其它所有变化都不阻断。**软门槛**（触发人工审核 REVIEW，不阻断）：任一维度变化 ≥ 10% 含上升——变好也要看一眼，防止 Judge 被表面漂亮的输出骗过。

Judge 用**配对比较**：给同一场景 + 新旧两次输出，让 Judge 判"A 好 / 持平 / B 好"，不评绝对分。这样能对抗 Judge 分数漂移——绝对分很容易被措辞、格式带偏，相对判断稳定得多。

**如果十次全被卡，也是有价值的信号**——说明候选生成策略需要重想（比如根因分析错了、patch 幅度太大），而不是把棘轮放宽。放宽棘轮等于把评估闭环拆了。

#### Q: 你这套跟 LangChain Agent / Anthropic 官方 Skills 什么关系？

**跟 LangChain 是不同抽象层**。LangChain 是 Agent 编排框架，解决"怎么让 LLM 调用工具、串工作流、管记忆"这一层；我这套是往上一层——**"怎么生产可复用、有评估、能自进化的 Skill"这个元层**。LangChain 里你写好一个 chain 就上线了，SkillForge 里一个 Skill 上线前要过八维评估 + 棘轮 + 分级发布。用 LangChain 也完全可以承接我的产出物；两者是正交关系，不是替代关系。

**跟 Anthropic 官方 Skills 是"参考规范但聚焦不同"**。我借鉴了官方 SKILL.md 的 YAML frontmatter 结构（`name / description / dependencies` 这些字段），但官方主要提供"Skill 怎么定义和加载"的规范，我聚焦"Skill 怎么被生产出来、怎么评估、怎么在多版本间安全切换"。跟 MCP 也是正交关系——MCP 定义"怎么用工具"，我在上一层定义"用工具的套路怎么迭代出来"。所以官方 Skills 和 MCP 我都能接，不冲突。

#### Q: LLM-as-a-Judge 怎么保证没有偏见？

**我不假装没偏见，我讲缓解措施**。四条：

一，**配对比较代替绝对打分**。Judge 拿到"场景 + 新输出 + 旧输出"三份材料，只判"A 好 / 持平 / B 好"。绝对分容易被格式、措辞、长度带偏，相对判断稳定得多。

二，**Judge 与被评 Skill 潜在同源**——我 Judge 用 DeepSeek，被评 Skill 也可能走 DeepSeek，自评偏见风险真实存在，我在方案书里明写了这一条。缓解手段是每 20 次评估抽 1 条做**人工盲评校准**，Judge 与人工分歧 > 30% 就调 Judge prompt。

三，**Phase 3 保底一次 ≥ 20 条人工校准**，出偏差报告，不是随口说"我校准过"。

四，**评估可信度声明**四条写进方案书：只适用于固定场景下的相对回归 + 候选筛选、不能证明绝对质量、不支持高风险改动全自动发布、分数不宣称与用户满意度相关。

一句话总结：**我不是在做"客观质量评估器"，我在做"相对回归筛选器"**，边界画清楚，Judge 才有用。

---

### 7.4 三个必备预案

#### Q: 多给你 4 周你会先加什么？

**优先补评估可信度这一块，不是加新功能**。三个具体动作：

一，扩人工盲评样本，从 Phase 3 的 ≥ 20 条扩到 100 条以上，覆盖各种类型的 Skill 修改，形成一个 Judge 校准的稳定基线。

二，扩隐藏集，从 8 条扩到 30 条以上，并按场景类型分层（trigger 类、instructions 类、边界处理类），防止元 Agent 在开发集上过拟合。

三，跑更多元 Agent 迭代（30+ 次），系统性归因失败原因，反过来优化根因分析 prompt 和候选生成策略——这一步只有量够了才能沉淀出模式。

之所以不加新功能：新功能会拉低现有闭环的可信度，评估基线不稳定的时候加东西是把自己搞糊涂。这个项目最大的护城河是"评估闭环讲得清、经得起追问",不是功能多。

#### Q: 元 Agent 成功率大概多少？

**~30%，坦诚给数字**。10 次迭代大约 3 次能通过棘轮进到 L1/L2 发布。

这个数字有价值在三点：**一是自动淘汰**——70% 明显错的改动被自动挡在门外，人只需要 review 3 次而不是 review 10 次；**二是负样本沉淀**——失败的 7 次 patch 全归档到 `runs/failures/<release_id>.md` 附根因分析，等于给未来的候选生成攒了模式；**三是评估闭环压测**——跑 10 次意味着评估集被反复冲击 10 次，能暴露 Judge 偏见、评估集覆盖漏洞。

**我明确不做全自动发布**——评估集只有 40 条、Judge 有自评偏见风险，可信度不足以支持全自动。元 Agent 的定位是"候选生成器 + 初筛器",不是"最终决策者"。这个边界清楚了，30% 就是可交付的数字，不是需要遮掩的短板。

#### Q: 这个项目最大的技术难点是什么？

**评估闭环的可信度**,不是路由、也不是元 Agent。

三个具体难点叠加：**一是 Judge 偏见**——DeepSeek 做 Judge 又评自己家 API 出来的 Skill，自评偏见是真的，我只能靠配对比较 + 人工盲评校准缓解，不能消除；**二是元 Agent 生成质量**——候选 patch 是 LLM 生成的,可能"表面看起来合理但实际引入 regression",光靠评估分不一定能立刻抓到；**三是小测试集叠加风险**——40 条基线 + 8 条隐藏集,统计意义有限,Judge 一次 flip 分数就可能翻盘。

三个问题**互相放大**:小测试集让 Judge 单次偏见影响变大;元 Agent 迭代次数多了会在小测试集上过拟合;Judge 又偷偷学到了元 Agent 的风格偏好。三者环环相扣。

所以我把闭环设计成保守的:硬门槛卡死不退步;L1 自动、L2 REVIEW、L3 只出建议;Phase 3 保底跑一次人工校准出偏差报告。**难点不是"消除风险",而是"承认风险 + 把边界画清楚 + 用工程手段兜底"**。这才是能拿到面试上讲的东西。

---

## 附录

### A.1 框架扩展关系

所有新组件均在 hello-agents 现有抽象上扩展，不引入新框架。

| 新组件 | 扩展自 | 职责 |
|---|---|---|
| `SkillRegistry` | `ToolRegistry` | Skill 版本注册 + 元数据索引拼装 + `use_skill` 特殊工具注册 |
| `SkillEvaluator` | `Tool` | 八维评估器：结构分静态检查 + Judge 配对比较 + 客观指标采集 + 棘轮门槛判定 |
| `IntentRouter` | `Tool` | 三层路由：规则 → embedding → LLM 级联 + 路由日志 |
| `SkillEvolver` | `SimpleAgent` | 元 Agent：失败收集 → 根因分析 → 候选生成 → 验证 → 分级发布 |
| `ReleaseStateMachine` | 自写（无扩展） | SQLite 状态机：PREPARING → PUBLISHED 原子切换 + Watchdog |

### A.2 一致性口径清单（v3 唯一真源）

面试或后续修改时以此清单校对，避免叙述漂移。

**必须坚守的口径**：

- ✅ 渐进式披露 **只有两层**：元数据索引层 + 完整实现层
- ✅ 加载由 **Agent 主动调 `use_skill(name, reason)`** 发起，**框架不拦截 prompt**
- ✅ `trigger.keywords` 只作为**路由排序信号**，不做自动展开
- ✅ 三层路由为 **"规则优先、Embedding 补充、LLM 兜底"**，**不写死百分比**
- ✅ Embedding 使用 **结构化检索卡片**（capability + use_when + examples + not_for）
- ✅ 硬负例评测集 **50 条**，Recall@1 ≥ **80%**、Recall@3 ≥ **90%**
- ✅ 结构分 40% **不阻断发布**，效果分 60% 才是发布门槛
- ✅ 效率维度用**客观指标**（轮数/token/延迟），**不用 LLM 主观打分**
- ✅ Judge 采用 **新旧输出配对比较**，不评绝对分
- ✅ 棘轮硬门槛五条，软门槛 **≥ 10% 触发 REVIEW**
- ✅ 元 Agent 是**候选生成器**，不是最终决策者，**成功率 ~30%**
- ✅ **L1 自动 / L2 REVIEW / L3 只出建议**
- ✅ **SQLite 是唯一发布事实源**，写入协议四步固定顺序
- ✅ `release_id` UUID 幂等，Watchdog 24h 清理 PREPARING
- ✅ 评估可信度声明四条（相对回归 / 非绝对质量 / 非全自动发布 / 非用户满意度）

**必须避免的叙述**：

- ❌ "加载器在 Agent.run() 之前拦截 prompt 拼接"（与 Agent 主导冲突）
- ❌ "加载器自动展开第 2 层 / 第 3 层"（v3 只有两层，且不自动展开）
- ❌ 硬编码 80% / 15% / 5% 覆盖率
- ❌ "LangChain 所有工具一次塞进 prompt"（不准确，且拉踩）
- ❌ "天然比 XX 助手高一个抽象级"（拉踩，改为"正交定位"）
- ❌ 承诺元 Agent 全自动发布高风险改动
- ❌ 声称评估分与用户真实满意度相关

### A.3 关键指标一览

| 指标 | 目标值 | Phase | 备注 |
|---|---|---|---|
| Recall@1（路由） | ≥ 80% | Phase 2 交付门槛 | 50 条硬负例评测集 |
| Recall@3（路由） | ≥ 90% | Phase 2 交付门槛 | 同上 |
| 元 Agent 成功率 | ~30% | Phase 4 观测 | 10 次迭代约 3 次通过棘轮 |
| Judge/人工分歧 | < 30% | Phase 3 保底 | ≥ 20 条人工盲评校准 |
| 硬门槛：任务完成度下降 | ≥ 5% 阻断 | 全程 | 棘轮硬门槛 |
| 硬门槛：鲁棒性下降 | ≥ 5% 阻断 | 全程 | 棘轮硬门槛 |
| 软门槛：任一维度变化 | ≥ 10% 触发 REVIEW | 全程 | 含上升 |
| 隐藏集比例 | ≥ 20%（8/40） | Phase 3 | 防元 Agent 过拟合 |
| Watchdog 清理阈值 | 24 h | 全程 | PREPARING → ABANDONED |

### A.4 项目规模

- **代码量**：约 900 行 Python
- **依赖**：hello-agents + pydantic + sentence-transformers + 标准库（sqlite3 / subprocess）
- **外部服务**：无（bge-small-zh 本地起，DeepSeek API 已有 Key）
- **演示方式**：CLI 一键复现，无需部署
- **时间**：4 周业余时间（Phase 1-3 硬底线，Phase 4 允许收敛到 L1）

---

> **v3 修订说明**：本方案书 v3 基于多 Agent 评审 + 十轮 grill-me 决策 + v2 问题清单，全面重写。核心修订：
> 
> 1. 渐进式披露改为 Agent 主导（v2 部分章节残留加载器拦截旧版）
> 2. 补齐跨存储一致性协议（v2 完全缺失）
> 3. 三层路由去掉硬编码百分比 + 补 Embedding 模型选型
> 4. 八维评估器补配对比较 + 客观指标 + 分级门槛
> 5. 元 Agent 半自动化（L1/L2/L3 + 30% 成功率坦诚）
> 6. Phase 门槛按 Phase 归位（v2 表格错位到 Phase 1）
> 7. 删除 LangChain 拉踩 + "高一个抽象级" 话术
> 8. 前置知识诚实标记（v2 全打 ✅ 不可信）
> 
> 修订日期：2026-07-26

---

## 附录 B：简历项目介绍写法

### B.1 推荐版本（8 行紧凑版，5-6 秒可扫读）

```
SkillForge · Agent Skill 自进化工厂 | 个人项目 | 2026.07 – 2026.08
基于 hello-agents 框架扩展，定位为「生产 Skill 的元 Agent 系统」。约 900 行 Python，CLI 完整演示。

• 渐进式披露引擎：Agent 主导加载，注册特殊工具 use_skill(name, reason)；只暴露元数据索引，
  完整 Body 按需返回。框架不拦截 prompt，加载动作 100% 可归因。
• 三层路由（规则→bge-small-zh→LLM 兜底）：结构化检索卡片（capability/use_when/examples/not_for）
  编码；50 条硬负例评测集，Recall@1 ≥ 80%、Recall@3 ≥ 90%。
• 八维评估器 + 棘轮机制：Judge 采用新旧输出配对比较（非绝对分）；效率维度走客观指标；
  硬门槛五条自动阻断、软门槛 10% 触发人工 REVIEW。
• SkillEvolver 元 Agent（半自动）：L1 自动 / L2 REVIEW / L3 只出建议；成功率约 30%，失败 patch
  归档 runs/failures/ 沉淀负样本；跨存储采用 SQLite 状态机 PREPARING→PUBLISHED 原子切换。
```

### B.2 每一行的钩子设计（面试引导用）

每条 bullet 都对应一个可展开的面试话题——**面试官扫简历大概率会挑一条追问**，你按方案书第 7 章的答案深展开。

| 简历行 | 埋的钩子 | 面试展开位置 |
|---|---|---|
| `use_skill(name, reason)`、"框架不拦截 prompt" | 为什么不让加载器自动展开？ReAct 可解释性怎么保证？ | 方案书 §7.2 Q1 |
| `Recall@1 ≥ 80%` + `硬负例评测集` | 为什么不用纯 embedding？硬负例怎么造？ | §7.3 Q1 |
| `配对比较（非绝对分）` + `硬门槛五条` | Judge 偏见怎么处理？棘轮会不会导致改进停滞？ | §7.3 Q2、Q4 |
| `L1 自动 / L2 REVIEW / L3 只出建议` + `成功率 30%` | 为什么不搞全自动？30% 有什么价值？ | §7.4 Q2 |
| `SQLite 状态机 PREPARING→PUBLISHED` | 三处存储写一半崩了怎么办？ | §2.3 Q4 |

### B.3 一句话版本（用于个人简介 / LinkedIn Headline）

> 独立设计并实现"生产 Skill 的元 Agent 系统"：基于 hello-agents 的 900 行 Python 实现，涵盖 Agent 主导渐进式披露、三层路由（规则+bge/embedding+LLM）、八维评估器+棘轮、SkillEvolver 半自动闭环，跨存储采用 SQLite 状态机保证一致性。

### B.4 三行超短版（用于表格式简历，如"项目经历"栏位空间受限）

- **SkillForge · 元 Agent 系统**（个人 · 900 行 Python）
- 核心组件：渐进式披露引擎（`use_skill` Agent 主导） / 三层路由（规则+bge-small+LLM，Recall@1 ≥ 80%） / 八维评估器+棘轮（Judge 配对比较、硬门槛五条） / SkillEvolver 半自动闭环（L1/L2/L3 分级 + SQLite 状态机）
- 交付：40 条基线评估集 + 50 条硬负例 + 10 次元 Agent 真实迭代日志，全 CLI 可复现

### B.5 写简历的三条硬规则（v3 方案书的一致性延伸）

**规则 1：所有数字都要能兑现**
- 简历写 `Recall@1 ≥ 80%` 意味着你的 Phase 2 交付物必须真的跑到这个数
- 简历写 `10 次元 Agent 真实迭代` 意味着 Phase 4 必须真的有 10 次留档
- **写之前先问自己："如果面试官说'把日志给我看一眼'，我拿得出来吗？"** 拿不出来就删掉那个数字

**规则 2：不写不能追问的空话**
- ❌ "使用先进的 LLM 技术构建 Agent 系统" → 什么都没说
- ❌ "提高了 XX 性能" → 没数字等于没说
- ✅ "配对比较代替绝对分" → 一个具体决策，能被追问原因
- ✅ "L1 自动 / L2 REVIEW / L3 只出建议" → 明确策略，面试官能追问"L3 为什么不自动"

**规则 3：与方案书 A.2 违禁清单一致**
- ❌ 简历里不写 "80% 走规则、15% embedding、5% LLM"（硬编码百分比 v3 已删）
- ❌ 简历里不写 "基于 LangChain 但更好"（拉踩）
- ❌ 简历里不写 "全自动 Skill 演化"（承诺不了）
- ✅ 简历里写 "规则优先、embedding 补充、LLM 兜底" 这类描述性表达
- ✅ 简历里写 "元 Agent 半自动闭环，成功率约 30%" 这类坦诚数字

### B.6 STAR 展开（如需要 3-5 句话叙述性版本，如求职信/自我介绍）

> **Situation**：Agent Skill 生态里大部分工作聚焦"用 Skill 干活"，缺少一套让 Skill 本身可评测、可版本管理、可自动改进的工程闭环。
>
> **Task**：4 周业余时间独立设计并实现一个"生产 Skill 的元 Agent 系统"，作为技术面试的深度讨论素材，目标覆盖 20 分钟工程细节问答。
>
> **Action**：以 hello-agents 为底座扩展 5 个核心组件——SKILL.md 规范、Agent 主导的渐进式披露引擎（use_skill 特殊工具）、三层级联路由（规则+bge-small+LLM）、八维评估器+棘轮机制（Judge 配对比较+客观指标）、SkillEvolver 元 Agent（L1/L2/L3 分级发布）。跨存储用 SQLite 状态机保证一致性，评估集 40 条基线（32 开发+8 隐藏）+ 50 条硬负例。
>
> **Result**：约 900 行 Python，CLI 可完整复现；Phase 2 路由 Recall@1 ≥ 80%，Phase 3 产出 Judge/人工偏差报告，Phase 4 完成 10+ 次真实迭代（成功/失败 patch 全归档）。设计过程中做了多次评审 + 十轮结构化 grill-me 决策，修订出 v3 方案书含一致性口径清单。

---

> **最后提醒**：简历上的每一行都是"面试话题订单"。**你希望被追问什么，就往简历上放什么**。如果你不希望被问 `bge-m3` 具体原理，就只写 `bge-small`；如果不希望被问 P0 用例挑选标准，就把 "P0 用例" 换成 "关键场景"。方案书 §7 是你所有钩子的完整答案库——**简历埋的钩子必须与方案书答案一一对应**，别在简历里埋一个方案书里没准备答案的钩子。
