<div align="center">

# SkillForge

**Agent Skill 自进化元 Agent 系统**

*基于 [hello-agents](https://pypi.org/project/hello-agents/) 扩展的元 Agent 工厂 —— 让 Skill 成为可评测、可版本管理、可自动改进的一流工程实体*

![Python](https://img.shields.io/badge/python-3.10%2B-3776ab?logo=python&logoColor=white)
![Tests](https://img.shields.io/badge/tests-316%2F316%20passing-brightgreen?logo=pytest&logoColor=white)
![Recall@1](https://img.shields.io/badge/router%20Recall%401-98%25-success)
![Phase](https://img.shields.io/badge/phase-5%20(P0--P2)%20complete-blue)
![Code](https://img.shields.io/badge/python-~5000%20lines%20core-informational)
![License](https://img.shields.io/badge/license-MIT-lightgrey)

</div>

---

## 1. 一句话定位与核心差异化

> **"生产 Skill 的元 Agent 系统"** —— 大多数 Agent 项目聚焦于"用 Skill 干活"；本项目聚焦让 Skill 本身**可评测、可版本管理、可自动改进**的工业级工程闭环。

6 周业余时间独立完成 · Phase 1-4 基础闭环 + **Phase 5（P0 可信地基 → P1 受控回环 → P2 自生成/拆分/轨迹提用例/LangGraph 旁路）** · CLI 一键复现所有关键数字 · **316 tests 全绿**。

### 3 个核心差异化

1. **让 Skill 成为一流工程实体（可评测 · 可版本管理 · 可自动改进）**  
   拒绝把 Prompt 当作一次性黑盒文本。Skill 具备独立工程生命周期：元数据与 Body 双层渐进式披露、八维沙箱基线评测、确定性语义 diff 分级（L1 自动 / L2-L3 建议）、Git 源码版本追踪与 SQLite 状态机原子发布。
2. **先可信再自进化（防自嗨工程哲学）**  
   市面上多数"自进化"系统容易陷入"模型重写 Prompt、同一模型打满分"的虚假闭环。SkillForge 坚持"先装刹车再踩油门"：构建包含**真实性快照绑定、数据物理边界隔离、客观 Token 膨胀硬限、规范化指纹熔断**等 8 重深度防御防线。宁可不发布，绝不让模型自嗨固化假改进。
3. **真实 LLM 对照实验与诚实边界验证**  
   拒绝纯单元测试的"玩具级演示"。系统基于真实 DeepSeek 模型完成了 20 次端到端跑批（基线 C vs 根因反思 RB 各 10 次真实对照），实证了发布门 DECLINED 3→0 的机制收敛性；同时主动交代小样本（n=20）下统计不显著的客观边界，展现严谨的工程与科研素养。

---

## 2. 30 秒 TL;DR 能力清单

| 能力维度 | 核心机制与指标 | 典型命令 / 复现入口 |
|---|---|---|
| **路由检索** | 规则（0.01ms）→ BGE 检索卡片（50ms）→ LLM 兜底三层级联；50 条硬负例校准；**Recall@1 = 98% · Recall@3 = 100%** | `python scripts/eval_router.py --use-llm` |
| **沙箱评估** | 八维评估器（结构 40 + 效果 60）；配对比较 Judge（INVALID fail-closed）；客观 Token 效率度量；样本级 11 字段轨迹落盘 | `skillforge evaluate --skill explain_regex` |
| **受控进化** | 两轮受控回环（失败收集 → A2 根因定位 → 定向候选生成 → 8 防线裁决）；默认 shadow 隔离；彻底杜绝死循环 | `skillforge evolve --skill explain_regex` |
| **生态繁衍** | Skill 自动生成器（BGE 0.70 冲突拦截）+ 三维耦合分析拆分器（weather 正确拒拆）+ 轨迹自动提取 badcase 闭环（14 条 auto 入库） | `python scripts/generate_skills_p2a.py`<br/>`python scripts/extract_cases_from_traces.py` |
| **状态图旁路** | LangGraph 状态图旁路（7 节点 14 边，SqliteCheckpointer 断点持久化）；通过适配器完全复用原子节点，与主链 **100% 行为等价** | `python scripts/demo_langgraph_p2d.py`<br/>`python scripts/dual_run_p2d.py` |
| **工程底座** | **316/316 tests 全绿（8.8s）**；SQLite 状态机原子发布；Git commit 完整审计归因；运行日志全流程可回放 | `pytest tests/ -q` |

---

## 3. 故事线：从"自进化信号不可信"到 Phase 5 三幕演进

### 3.1 Phase 1-4 基础闭环与遭遇的深层瓶颈

在 Phase 1-4 阶段，项目快速搭建了 5 组件骨架、三层路由、八维评估器与 L1 分级发布流水线。但在实际运行端到端自进化闭环时，暴露了核心技术瓶颈——**"自进化的信号不可信"**：
- **Judge 幻觉盲区**：在无真实工具调用的场景下，旧 Judge 在 task 维度的分歧率高达 62%，常常把模型自编的无凭证幻觉打出高分；
- **等级自报越权**：缺乏 AST 级语义 diff 计算，模型改写了核心 Instructions 却声称只是 L1 frontmatter 微调；
- **评测数据泄漏**：测试集未物理隔离，评估用例面临被反思提示词直接逆向学习并过拟合的风险；
- **评估偶发崩溃**：单条 Case 遭遇网络抖动或不可解析，导致整个 12 条评估批次彻底报废（旧版报废率高达 7/12）。

### 3.2 Phase 5 三幕演进（2026-09-02 ~ 2026-09-06）

围绕**"让 AI 自我改进可信"**的技术命题，Phase 5 推进了三幕深度改造：

```
Phase 1-4 基础 ──► 发现瓶颈（信号不可信/无防线）
                     │
                     ▼
  ┌─────────────────────────────────────────────────────────────┐
  │ Phase 5 第一幕 · P0 可信地基（装刹车与仪表盘，94→143 tests）  │
  │ • semantic diff 可信化 + computed_level 拦截模型自报降级      │
  │ • 验证器按改动面精准咬合（改 metadata 验路由，改 Body 验行为）│
  │ • Judge 增加 INVALID fail-closed 态 + Truth Sentinel 哨兵   │
  │ • 数据划分三层（repair/holdout/final_audit）+ P0 发布门     │
  └──────────────────────────────┬──────────────────────────────┘
                                 │
                                 ▼
  ┌─────────────────────────────────────────────────────────────┐
  │ Phase 5 第二幕 · P1 受控回环（控方向与真实验证，143→247 tests）│
  │ • 双轮反思回环（失败收集→根因分支→定向候选→8 防线裁决）     │
  │ • 全局 LLMLedger 预算硬帽 + Prompt Bloat 膨胀门槛 + 指纹熔断 │
  │ • 评估链容错修复（Case 级跳过）+ C vs RB 各 10 次真实对照实验 │
  │ • 实验结论：RB 78.6 vs C 71.7（+6.9）；DECLINED 3→0；收敛停止 │
  └──────────────────────────────┬──────────────────────────────┘
                                 │
                                 ▼
  ┌─────────────────────────────────────────────────────────────┐
  │ Phase 5 第三幕 · P2 自生成与生态（促繁衍与工程探索，247→316） │
  │ • P2-A Skill 生成器：需求输入→SKILL.md+初始集，BGE 0.70 拦截│
  │ • P2-B 拆分器：三维耦合量化裁决，weather 同源多意图正确拒拆   │
  │ • P2-C 轨迹提用例：样本级审计轨迹经 3 道质量门自动入 repair  │
  │ • P2-D LangGraph 旁路：7 节点 14 边状态图，7 场景双跑 100% 等价 │
  └─────────────────────────────────────────────────────────────┘
```

---

## 4. 全景系统架构

```mermaid
flowchart TB
    subgraph AgentLayer [Agent 运行时与加载]
        User([用户 / 面试官]) --> Agent[hello-agents ReActAgent]
        Agent -->|"1. use_skill(name, reason)"| Router{"IntentRouter<br/>(三层级联路由)"}
        Skills[("skills/ 知识库<br/>(种子 Skill + 生成 Skill)")]
        Router -->|"2. 规则 / BGE / LLM 检索"| Skills
        Skills -->|"3. SQLite→Git 注入 Body"| Agent
    end

    subgraph EvolveMainChain [Phase 5 主链：受控反思回环 (SkillEvolver 驱动)]
        direction TB
        Step1["1. Baseline 沙箱评测<br/>(P0 Fail-Closed 门禁)"]
        Step2["2. 失败用例收集<br/>(提取退步/异常)"]
        Step3["3. A2 根因分析<br/>(路由/行为/依赖分支)"]
        Step4["4. 定向候选生成<br/>(反思纠偏 + L1/L2 Patch)"]
        DefenseGate{"8 重防线裁决<br/>(棘轮/膨胀/泄漏/指纹等)"}
        Outcome["分级终态裁决<br/>(L1 auto / L2-L3 REVIEW / DECLINED)"]

        Step1 -->|"存在有效失败"| Step2
        Step2 --> Step3
        Step3 --> Step4
        Step4 --> DefenseGate
        DefenseGate -->|"未通过 / 触发 Round 2 反思"| Step4
        DefenseGate -->|"达标通过 / 轮次耗尽"| Outcome
    end

    subgraph P0Defenses [P0/P1 代表性防线与护栏]
        direction TB
        Def1["数据边界隔离<br/>(holdout 严禁泄入 repair)"]
        Def2["预算硬帽 Ledger<br/>(Token/调用超限硬停)"]
        Def3["真实性快照绑定<br/>(生成与验证同源响应)"]
        Def4["指纹熔断防御<br/>(SHA-256 重复候选熔断)"]
    end

    DefenseGate -.-> P0Defenses

    subgraph EvalClosedLoop [评估链自闭环 (数据飞轮)]
        Traces[("runs/eval_traces/<br/>样本级审计轨迹 (11 字段)")]
        Extractor["badcase 提取器<br/>(3 道质量门 + 冲突检测)"]
        RepairSet[("evaluation_sets/repair_set.json<br/>(_auto_ manifest 动态集)")]

        Step1 -.->|"落盘全量轨迹"| Traces
        Traces --> Extractor
        Extractor -->|"自动扩充修复用例"| RepairSet
        RepairSet -.->|"驱动下一轮评估"| Step1
    end

    subgraph P2Ecosystem [P2 自生成与生态繁衍]
        direction TB
        Generator["Skill 生成器 (P2-A)<br/>(需求→SKILL.md + 初始集)"]
        Splitter["Skill 拆分器 (P2-B)<br/>(三维耦合分析裁决)"]
        SubSkills[("拆分子 Skill<br/>(解耦独立发布)")]

        Generator -->|"0.70 BGE 冲突拦截通过"| Skills
        Splitter -->|"高耦合: 正确拒拆 (如 weather)"| Skills
        Splitter -->|"低耦合: 事务化拆分"| SubSkills
        SubSkills --> Skills
    end

    subgraph LangGraphSidecar [LangGraph 旁路 (P2-D · Shadow 隔离)]
        direction TB
        LGGraph["StateGraph 状态图<br/>(7 节点 / 14 边拓扑流转)"]
        LGCheckpointer[("SqliteCheckpointer<br/>(Durable 崩溃断点恢复)")]
        LGGraph --- LGCheckpointer
    end

    Outcome -->|"发布生效"| Skills
    EvolveMainChain -.->|"节点复用 / 双跑验证等价 (Shadow 隔离)"| LangGraphSidecar

    classDef sidecar fill:#f3e8ff,stroke:#9333ea,stroke-width:2px,stroke-dasharray: 5 5;
    classDef defense fill:#fef2f2,stroke:#ef4444,stroke-width:1px;
    classDef loop fill:#eff6ff,stroke:#3b82f6,stroke-width:1px;
    classDef eco fill:#f0fdf4,stroke:#22c55e,stroke-width:1px;

    class LangGraphSidecar sidecar;
    class P0Defenses defense;
    class EvolveMainChain,EvalClosedLoop loop;
    class P2Ecosystem eco;
```

> **图注与架构取舍：主链 for 循环 vs 旁路 LangGraph**
> 1. **主链（for 循环驱动，`evolver.py`）**：采用两轮受控回环（`max_rounds=2`）。其优势在于**极简无侵入、单进程零额外黑盒依赖、执行路径完全透明、便于单元测试与硬门禁拦截**。它是系统的生产稳定基线。
> 2. **旁路（LangGraph StateGraph，`langgraph_loop.py`）**：作为实验性 **Shadow 旁路**，利用 LangGraph 将状态黑板显式化为 7 节点 14 边的有向状态图，并结合 `SqliteCheckpointer` 带来进程崩溃断点恢复（Durable Execution）能力。
> 3. **工程纪律与节点复用**：旁路**完全复用**主链的原子底层组件，主链零语义变更；并通过 `dual_run_p2d.py` 在 7 类场景下验证与主链 100% 行为等价。详见 [docs/langgraph_loop.md](docs/langgraph_loop.md)。

---

## 5. 快速上手（Quick Start）

### 5.1 环境准备（约 5 分钟）

```bash
git clone https://github.com/SuperGODOG/skillforge.git && cd skillforge

# 1. 创建虚拟环境并安装依赖（阿里源规避哈希冲突）
python3 -m venv .venv
./.venv/bin/pip install --index-url https://mirrors.aliyun.com/pypi/simple/ -e .

# 2. bge-small 模型下载（国内走 modelscope 极速通道）
./.venv/bin/pip install modelscope
./.venv/bin/python -c "from modelscope import snapshot_download; snapshot_download('AI-ModelScope/bge-small-zh-v1.5', cache_dir='./models')"

# 3. 配置 DeepSeek API 密钥
cat > .env <<'EOF'
LLM_API_KEY=sk-填你的-key
LLM_MODEL_ID=deepseek-chat
LLM_BASE_URL=https://api.deepseek.com/v1
# Judge 走独立 client 会话；可使用同一 key 或独立模型
JUDGE_LLM_API_KEY=sk-填你的-judge-key
JUDGE_LLM_MODEL_ID=deepseek-chat
JUDGE_LLM_BASE_URL=https://api.deepseek.com/v1
EOF
```

**自检验证**：执行 `./.venv/bin/pytest tests/ -q` 应全部通过（**316 passed**）。

---

### 5.2 核心 CLI 体验

<details>
<summary><b>1. 运行时加载</b>：<code>skillforge demo</code> · Agent 主动 use_skill 全链路</summary>

```bash
./.venv/bin/skillforge demo --query "上海明天会下雨吗"
```
预期输出：Agent 依据查询显式调用 `use_skill('weather_query', ...)`，全链路通过 SQLite→Git 读取 Body，并将带有 reason 的归因审计写入 `router.jsonl`。
</details>

<details>
<summary><b>2. 三层级联路由</b>：<code>skillforge route</code> · 规则 / BGE / LLM 级联决策</summary>

```bash
./.venv/bin/skillforge route "帮我写一个正则匹配所有邮箱" --use-llm
# 命中 Not For 拒绝，返回 chosen=None（硬负例：代码生成 ≠ 正则讲解）
```
- `rule`：keyword 排序信号命中（0.01ms）
- `embed`：top1 相似度 ≥ 0.75 且 margin ≥ 0.10（~50ms）
- `llm`：置信度中间地带由 LLM 语义兜底二选一（~500ms）
</details>

<details>
<summary><b>3. 八维沙箱评估</b>：<code>skillforge evaluate</code> · 结构分 + 效果分 + 棘轮</summary>

```bash
./.venv/bin/skillforge evaluate --skill explain_regex --eval-set baseline_dev --verbose
```
结构分 40 分（表单完整性校验，不阻断发布）；效果分 60 分（task/robust/readability/efficiency，决定发布门槛）；全量通过 P0 测试。
</details>

<details>
<summary><b>4. 受控反思进化</b>：<code>skillforge evolve</code> · 元 Agent 闭环迭代</summary>

```bash
./.venv/bin/skillforge evolve --skill explain_regex --max-candidates 3
```
执行 Baseline 评测 → 收集失败样本 → A2 根因定位 → 定向候选生成 → 8 防线裁决 → 产出新版本或归档建议至 `runs/suggestions/`。
</details>

---

### 5.3 Phase 5 核心实验与功能复现

以下命令可在本地直接复现 Phase 5 的关键成果与数字：

```bash
# 1. 运行全量测试套件（8.8s 验证 316 条单测）
./.venv/bin/pytest tests/ -q

# 2. 路由评测（50 条硬负例校准，验证 R@1=98% / R@3=100%）
./.venv/bin/python scripts/eval_router.py --use-llm

# 3. LangGraph 旁路演示（打印 7 节点 14 边状态图拓扑与流转序列）
./.venv/bin/python scripts/demo_langgraph_p2d.py

# 4. 双跑行为等价验证（7 场景全绿验证主链与 LangGraph 100% 等价）
./.venv/bin/python scripts/dual_run_p2d.py

# 5. Skill 自动生成器体验（自然语言生成 1 个标准 Skill，含 BGE 冲突拦截）
./.venv/bin/python scripts/generate_skills_p2a.py

# 6. 从审计轨迹自动提取 badcase 入库（体验 3 道质量门筛选）
./.venv/bin/python scripts/extract_cases_from_traces.py
```

---

## 6. 证据与数字对账表

### 6.1 硬指标对账

| 指标维度 | 门槛 / 承诺 | 实测结果 | 达标情况 | 事实依据 / 验证方式 |
|---|---|---|---|---|
| **路由 Recall@1** | ≥ 80% | **98%** | ✅ 超额达标 | 50 条硬负例评测集，`scripts/eval_router.py` |
| **路由 Recall@3** | ≥ 90% | **100%** | ✅ 超额达标 | 同上 |
| **pytest 测试套件** | Phase 1 交付 10 条 | **316/316 · 8.8s** | ✅ 全绿通过 | 覆盖 P0 可信地基至 P2 全链，`pytest tests/ -q` |
| **P1-I 真实对照 (C vs RB ×10)** | RB ≥ C | **baseline 78.6 vs 71.7 (+6.9)**<br/>发布门 DECLINED 3→0 | ⚠️ 机制收敛生效<br/>样本统计不显著 | 真实 DeepSeek 跑批 20 次；反思在生成侧把劣质候选拦下；出现达标停止信号 |
| **P2 生态与自生成闭环** | 生成 / 提取 / 拆分 | **2 skill 真实生成 (87.0 baseline)**<br/>**14 auto case 自动提取入库**<br/>weather 紧耦合**正确拒拆** | ✅ 全链跑通 | `generate_skills_p2a.py`、`extract_cases_from_traces.py`、`test_p2b_splitter.py` |
| **LangGraph 旁路等价性** | 行为等价 | **7 场景双跑 100% 对齐** | ✅ 等价通过 | 覆盖发布、超帽、反思、异常熔断等，`dual_run_p2d.py` |
| **Judge / 人工分歧** | < 30% 交付 | **rejudge 门 6/21 压线通过**<br/>truth sentinel 违背为 0 | ✅ 协议化受控 | 引入 INVALID 态、Fail-Closed 与 A/B 均衡协议 |
| **代码工程量** | 初始预估 ~900 行 | **核心 ~5000 行 + 测试/脚本 ~8000 行** | ✅ 工程交付扎实 | 包含 15 个核心模块与 8 重防线实现 |

### 6.2 诚实边界清单（面试被追问时主动交代）

1. **P1-I 效果统计显著性**：在 20 次样本下，Welch's t-test 的 p 值约为 0.27（未达到 p<0.05 统计显著水平）。我们展示的是**方向性占优（+6.9 分）与明确的机制行为证据（DECLINED 3→0、达标自动停止）**，而非绝对结论；下一步补全 R 与 B 独立归因组。
2. **Skill 生成器范式**：当前 P2-A 生成器专注于**文档型 / 知识型 Skill**；工具型 Skill（涉及动态 API 契约与沙箱依赖）是 v2 的模板域扩展方向。
3. **轨迹自动提取源**：当前 P2-C 提取的轨迹来自 SkillEvolver 自身的演化沙箱运行日志；接入真实用户生产对话流（具备 S2/S3 显式反馈信号）是 v2 迭代目标。
4. **人工一致性度量**：保底盲评采用 21 对样本的 Proxy 盲评协议，尚未实施跨多人的 Cohen's Kappa 统计；但协议已规范化（INVALID 拒判）。

---

## 7. 高频技术问答（FAQ 三问）

### Q1: 为什么不让 LLM 直接自由改写 Skill，搞这么多复杂的防线？
> **答**：自进化系统最大的技术风险不是"改不动"，而是**"改错之后模型依然逻辑自洽，并将错误固化为知识"**。  
> 若无门禁，一次偶然的幻觉改动或用例过拟合就会污染整个技能库。SkillForge 设立 8 重防线（diff 等级校验、真实性快照绑定、数据边界隔离、指纹熔断等），核心不是为了限制改进，而是为了**"先装刹车再踩油门，拦截所有假改进与自嗨"**。

### Q2: A1 反思回环在统计学上不显著，为什么还要保留在系统中？
> **答**：应将**机制行为证据**与**统计显著性**客观区分：  
> 1. **机制行为完全成立**：反思回环在生成侧把发布门 DECLINED 从 3 次降至 0 次，并首次出现了"达到最优自动停止"的收敛信号，证明反思确实抑制了劣质候选；  
> 2. **工程设计安全**：回环默认运行在 Shadow 隔离模式下，不直接影响生产主库，安全无害；  
> 3. **统计样本诚实**：20 次真实 LLM 端到端调用耗时约 5 小时，受限于实验成本导致样本量有限，这是客观事实而非设计缺陷。

### Q3: Skill 拆分什么时候是有害的？为什么 weather 意图不拆？
> **答**：当多个子意图之间存在**数据依赖同构**（高度共享底层外部工具或数据源）或**流程逻辑纠缠**时，强行拆分会带来严重反伤：  
> 1. 拆分后会导致同一底层 API 契约被多个子 Skill 重复维护与冗余声明；  
> 2. 路由层在细粒度近义意图之间的选择冲突率剧增；  
> 3. 评测集被过度稀释。  
> SkillForge 构建了三维耦合分析（数据耦合、流程耦合、评测集耦合），实测将 weather 的 3 个意图（相对日期、降水、温差）判定为强耦合并**正确拒拆**，把架构直觉变成了机器可量化裁决的严谨流程。

---

## 8. 项目结构、Roadmap 与文档地图

### 8.1 完整项目结构

```
skillforge/
├── src/skillforge/
│   ├── __init__.py              组件与数据模型顶层暴露
│   ├── models.py                Pydantic 与 dataclass（EvolveBudget / EvolveContext / 护栏契约）
│   ├── registry.py              SkillRegistry（继承 hello_agents.ToolRegistry，双层加载）
│   ├── router/                  三层级联路由（rule / embed / llm / cascade）
│   ├── evaluator/               八维评估器（structure / judge / metrics / ratchet / fixtures）
│   ├── evolver.py               SkillEvolver 受控回环引擎（8 重防线 / 预算硬帽 / 轨迹落盘）
│   ├── eval_tracer.py           P2-C 样本级审计轨迹记录器（11 字段全样本可审计）
│   ├── skill_generator.py       P2-A Skill 自动生成器（BGE 0.70 冲突拦截 / 原子注册）
│   ├── skill_splitter.py        P2-B 技能拆分裁决器（三维耦合度量化 / 事务化发布）
│   ├── langgraph_loop.py        P2-D LangGraph 状态图旁路（7 节点 14 边 / SqliteCheckpointer）
│   ├── data_partition.py        P0-D 三层评测数据集物理划分与边界校验
│   ├── diff.py                  P0-A 确定性语义 diff 与 computed_level 分级计算器
│   ├── state_machine.py         ReleaseStateMachine SQLite 发布状态机 + 24h Watchdog
│   ├── storage/                 SQLite 存储、Git 操作封装与 JSONL 审计
│   └── cli.py                   CLI 子命令入口
│
├── skills/                      Skill 库（3 种子技能 + 2 生成技能：explain_http_status / markdown_syntax_cheatsheet）
├── evaluation_sets/             评测数据集（手工金标 + 动态 _auto_ manifest）
│   ├── repair_set.json          回归与修复用例集（22 基础用例 + 14 条 auto 提取用例）
│   ├── experiment_holdout.json  实验留出集（9 条严格隔离，禁止参与反思）
│   ├── final_audit.json         终审评测集（9 条终极发布门槛）
│   ├── p0_cases.json            核心链路 P0 用例集（10 条 core 流程）
│   └── router_negatives.json    路由硬负例集（含自动互斥注册负例）
│
├── scripts/                     评测、盲评、生成、拆分、双跑与轨迹提取脚本
├── runs/                        运行时产物（*.db / *.jsonl / eval_traces/，已 gitignore）
│   ├── failures/                元 Agent 演化 DECLINED 补丁（负样本库）
│   ├── suggestions/             L2/L3 REVIEW 建议补丁归档
│   └── eval_traces/             P2-C 样本级详细审计轨迹（badcase 提取源）
├── tests/                       pytest 测试套件（316 tests 全绿）
├── docs/
│   └── langgraph_loop.md        LangGraph 旁路设计说明书（状态机映射、拓扑与 durable 机制）
│
├── ARCHITECTURE.md              完整架构视图（C4 两级模型 + 15 条 ADR + §10/§11 实施修订）
└── README.md                    项目主说明文档（本文件）
```

---

### 8.2 文档导航地图

| 文档路径 | 核心内容 | 面试推荐阅读位置 |
|---|---|---|
| [ARCHITECTURE.md](ARCHITECTURE.md) | C4 两级架构视图、15 条精简 ADR 决策记录、§10 Phase 4 实施差异、**§11 Phase 5 实施修订** | §8 ADR（追设计取舍）、§11（Phase 5 全链防线与数据流） |
| [docs/langgraph_loop.md](docs/langgraph_loop.md) | LangGraph 7 节点 14 边拓扑流转、for 循环等价映射表、SqliteCheckpointer 恢复机制 | §1 图拓扑、§2 等价映射表（展示新技术引入纪律） |
| `projects/项目文档留痕/skillForge/` | P1-I 收官报告、P2 各卡 agy 实现与 codex 评审报告全链证据 | 真实 LLM 跑批数据与技术审计链 |

---

### 8.3 未来演进路线（Roadmap）

Phase 5 主体能力已全量交付（P0 四卡 → P1 六卡 → P2 四卡，共 316 tests）。未来技术路线：

1. **R 与 B 独立归因组实验**：补全只有反思（R）与只有根因（B）的跑批，进一步明确两项机制在效果层面的独立贡献率。
2. **工具型 Skill 自动生成**：从当前的文档型 Skill 扩展至工具型 Skill，引入外部 Python 纯函数沙箱与参数 Mock 自动生成。
3. **生产对话流实时接入**：将 P2-C 轨迹提取器接入真实对话流（基于显式点赞/点踩与下游调用成功率 S2/S3 信号），实现生产环境的自闭环进化。
4. **真人独立盲评（Cohen's Kappa > 0.6）**：将保底盲评协议升级为多标注员盲评，产出标准 Kappa 一致性系数。
5. **模型上下文协议（MCP）集成**：将 SkillForge 的 Skill 导出与注册机制对接 Anthropic MCP 协议，成为跨 Agent 生态的通用 Skill 枢纽。

---

## License & Acknowledgements

- **License**: MIT License · 本项目为个人学习与面试展示工程，欢迎交流探讨。
- **致谢开源生态**：
  - [hello-agents](https://pypi.org/project/hello-agents/) — 提供极简且可扩展的 ReActAgent 基座抽象
  - [BAAI/bge-small-zh-v1.5](https://huggingface.co/BAAI/bge-small-zh-v1.5) — 优秀的轻量中文文本嵌入模型
  - [DeepSeek](https://api.deepseek.com/) — 强大的主推理与 Judge 驱动大模型
  - [ModelScope](https://modelscope.cn/) — 提供稳定的模型分发通道
