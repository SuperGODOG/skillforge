# SkillForge 架构文档

> 本文档是 SkillForge 的架构视图：讲"组件怎么划分、数据怎么流、接口长什么样"；设计取舍与面试口径见 README（五大核心设计决策 + 数字对账）。
>
> 初版：2026-07-26（Phase 1 前预估）· 修订：2026-07-27（Phase 4 全部完成，末尾 §10 记实施差异）
>
> **文档使用指南**：§1-§8 是**架构基线**（Phase 1 前设计），§10 是**实施差异修订**（哪些改了、为什么改）。两者共同构成完整架构视图，冲突处以 §10 为准。

---

## 1. 目标与约束

**目标**：4 周业余时间做出一个"生产 Skill 的元 Agent 系统"，代码规模 ~900 行 Python，CLI 完整演示。

**硬约束**：
- 无外部 DB 服务（SQLite 单文件）
- 无云依赖（bge-small 本地跑，DeepSeek API 唯一外部调用）
- 单进程模型（不做多 Worker、不做队列，保持可读性）
- Skill 加载由 Agent 主导，框架不拦截 prompt

**软目标**：
- 每个组件独立可测（单元测试 + 集成测试）
- 路由/评估/发布全流程日志可归因
- 关键决策的失败态优雅（旧版本继续生效）

---

## 2. 系统上下文（C4 Level 1）

```mermaid
graph LR
    User([面试演示者<br/>/ 用户])
    Skill[(Skills 源码仓库<br/>本地 Git)]
    DS[[LLM API<br/>执行 client + 独立 Judge client]]
    Bge[[bge-small-zh-v1.5<br/>本地 embedding]]

    User -->|CLI 命令 / 交互输入| SF
    SF[SkillForge 系统]
    SF -->|读写 SKILL.md| Skill
    SF -->|LLM 推理 / Judge| DS
    SF -->|文本编码| Bge

    style SF fill:#dcecff,stroke:#4a90e2,stroke-width:2px
    style User fill:#fff5d6
    style Skill fill:#e6f3e6
    style DS fill:#f3e6e6
    style Bge fill:#f3e6e6
```

**外部依赖清单**：
| 依赖 | 用途 | 部署位置 | 失败降级 |
|---|---|---|---|
| Git（CLI） | Skill 版本管理 + diff | 本地二进制 | 无（硬依赖） |
| SQLite | 元数据 + 发布状态 | 本地单文件 | 无（硬依赖） |
| LLM API | 执行推理 + 独立 Judge client | 云端 | Judge 失败产生 INVALID 并阻断；执行失败不伪装成评估结果 |
| bge-small-zh-v1.5 | Skill 检索卡片编码 | 本地 CPU | 加载失败退化到规则+LLM 两层路由 |

---

## 3. 组件划分（C4 Level 2）

### 3.1 5 个核心组件 + 数据流

```mermaid
graph TB
    subgraph Agent 层
        Agent[hello-agents ReActAgent]
    end

    subgraph "SkillForge 核心（~900 行）"
        SR[SkillRegistry<br/>索引层 + use_skill]
        IR[IntentRouter<br/>三层级联路由]
        SE[SkillEvaluator<br/>八维评估 + 棘轮]
        EV[SkillEvolver<br/>元 Agent 半自动闭环]
        SM[ReleaseStateMachine<br/>SQLite 状态机]
    end

    subgraph 存储层
        GIT[(Git<br/>Skill 源码)]
        DB[(SQLite<br/>元数据 + 发布状态<br/>唯一事实源)]
        JL[(JSONL<br/>评估轨迹 / 路由日志)]
    end

    Agent -->|读元数据索引| SR
    Agent -->|use_skill| SR
    Agent -.->|多候选| IR
    SR -->|读 Body| GIT
    SR -->|读 current_release_id| DB
    IR -->|路由日志| JL

    Agent -->|运行日志| SE
    SE -->|评估记录| JL
    SE -->|判定结果| SM

    EV -->|读失败案例| JL
    EV -->|生成候选| GIT
    EV -->|调评估| SE
    EV -->|走发布协议| SM

    SM -->|状态机四步| DB
    SM -->|写 commit| GIT
    SM -->|写评估流水| JL

    style SR fill:#dcecff
    style IR fill:#dcecff
    style SE fill:#dcecff
    style EV fill:#dcecff
    style SM fill:#dcecff
    style DB fill:#ffe6cc,stroke:#f39c12,stroke-width:3px
```

**责任边界**：
- **SkillRegistry**：管 "Agent 能看到什么 Skill、加载哪一个 Body"
- **IntentRouter**：管 "多个候选时选一个"
- **SkillEvaluator**：管 "新版本能不能发"
- **SkillEvolver**：管 "怎么生成候选新版本"
- **ReleaseStateMachine**：管 "版本切换的原子性"

### 3.2 继承 / 依赖关系

```mermaid
classDiagram
    class Tool {
        <<hello-agents>>
        +name: str
        +description: str
        +call(args)
    }
    class ToolRegistry {
        <<hello-agents>>
        +register(tool)
        +get(name)
    }
    class SimpleAgent {
        <<hello-agents>>
        +run(query)
    }

    class SkillRegistry {
        +load_skills_from_dir(path)
        +build_index() str
        +use_skill(name, reason) str
        +get_current_release(name) Release
    }
    class IntentRouter {
        +route(query, candidates) RouteResult
        -_rule_match(query, candidates)
        -_embed_match(query, candidates)
        -_llm_choose(query, top_k)
    }
    class SkillEvaluator {
        +evaluate(release_id) EvalResult
        +check_ratchet(old, new) RatchetVerdict
        -_structure_score(skill_md)
        -_effect_score(new_output, old_output)
        -_objective_metrics(run_log)
    }
    class SkillEvolver {
        +evolve(failure_cases)
        -_analyze_root_cause(failures)
        -_generate_patches(root_cause)
        -_classify_level(patch) L1_L2_L3
    }
    class ReleaseStateMachine {
        +begin_release(skill_name) release_id
        +write_commit(release_id, patch)
        +append_evaluation(release_id, result)
        +commit_release(release_id)
        +watchdog_sweep()
    }

    ToolRegistry <|-- SkillRegistry : 扩展
    Tool <|-- IntentRouter : 扩展
    Tool <|-- SkillEvaluator : 扩展
    SimpleAgent <|-- SkillEvolver : 扩展

    SkillRegistry ..> ReleaseStateMachine : 查询当前版本
    SkillEvolver ..> SkillEvaluator : 验证候选
    SkillEvolver ..> ReleaseStateMachine : 走发布协议
    SkillEvaluator ..> ReleaseStateMachine : 提交判定
```

---

## 4. 运行时视图（关键场景）

### 4.1 场景 A：Agent 主导渐进式披露 —— `use_skill` 调用

```mermaid
sequenceDiagram
    actor U as 用户
    participant A as ReActAgent
    participant R as SkillRegistry
    participant S as SQLite
    participant G as Git

    Note over A: 系统初始化时索引层<br/>已随 system prompt 注入
    U->>A: "北京今天天气怎么样"
    activate A
    A->>A: 读元数据索引<br/>判断需要 weather_query
    A->>R: use_skill("weather_query",<br/>"用户询问北京当日天气")
    activate R
    R->>S: SELECT current_release_id<br/>FROM skills WHERE name='weather_query'
    S-->>R: release_id + commit_hash
    R->>G: git show {commit_hash}:skills/weather_query/SKILL.md
    G-->>R: Skill Body 全文
    R->>R: 追加路由日志<br/>(name, reason, latency, hit_layer)
    R-->>A: Body 字符串（Overview + Instructions + Examples + Constraints）
    deactivate R
    A->>A: Body 作为 tool 结果拼回 conversation
    A->>A: 按 Body 里的 Instructions 调用 amap_mcp_server
    A-->>U: "北京今天晴，最高 32°C"
    deactivate A
```

**关键点**：
- 加载完全由 Agent 决策，`use_skill` 是显式 tool call，不是框架偷改 prompt
- `reason` 参数强制填写，路由日志可归因
- Body 只在 Agent 主动调用时才占 token

### 4.2 场景 B：三层路由级联判决

```mermaid
flowchart TB
    Q[查询进入<br/>query + candidates]
    R1{规则层<br/>keyword + 权重}
    R1a{top-1 领先 top-2<br/>≥ 20 分？}
    E1{Embedding 层<br/>bge-small 检索卡片<br/>余弦相似度 top-K}
    E1a{top-K 有明显领先<br/>且 > 置信阈值？}
    L1[LLM 层<br/>top-K 让 LLM 二选一]
    L1a{LLM 输出<br/>是否 NONE？}
    OK([返回 top-1])
    NONE([无匹配拒绝])
    LOG[写路由日志<br/>hit_layer + latency]

    Q --> R1
    R1 --> R1a
    R1a -->|Yes| OK
    R1a -->|No| E1
    E1 --> E1a
    E1a -->|Yes| OK
    E1a -->|No| L1
    L1 --> L1a
    L1a -->|No| OK
    L1a -->|Yes| NONE
    OK --> LOG
    NONE --> LOG

    style R1 fill:#e6f3e6
    style E1 fill:#fff5d6
    style L1 fill:#ffe6e6
    style LOG fill:#dcecff
```

**延迟预算**：规则 <5ms · Embedding ~50ms · LLM ~500ms（三层累计上限约 555ms）

### 4.3 场景 C：跨存储写入协议（SQLite 状态机）

```mermaid
sequenceDiagram
    participant Client as SkillEvolver / 手工发布
    participant SM as ReleaseStateMachine
    participant DB as SQLite
    participant Git
    participant JL as JSONL

    Note over Client,JL: release_id 全流程幂等键（UUID）

    Client->>SM: begin_release("weather_query")
    activate SM
    SM->>DB: INSERT releases(release_id, skill_name,<br/>status='PREPARING', ...)
    DB-->>SM: OK
    SM-->>Client: release_id
    deactivate SM

    Client->>SM: write_commit(release_id, patch)
    activate SM
    SM->>Git: git add + git commit
    Git-->>SM: commit_hash
    SM->>DB: UPDATE releases SET commit_hash=?<br/>WHERE release_id=?
    deactivate SM

    Client->>SM: append_evaluation(release_id, eval_result)
    activate SM
    SM->>JL: append_line({release_id, metrics, verdict, ...})
    deactivate SM

    Client->>SM: commit_release(release_id)
    activate SM
    SM->>DB: BEGIN IMMEDIATE
    SM->>DB: UPDATE releases SET status='PUBLISHED', published_at=NOW()<br/>WHERE release_id=? AND status='PREPARING'
    Note over DB: 状态条件保证幂等：<br/>已 PUBLISHED 的行不会二次更新
    SM->>DB: UPDATE skills SET current_release_id=?<br/>WHERE name=?
    SM->>DB: COMMIT
    DB-->>SM: OK
    SM-->>Client: PUBLISHED
    deactivate SM

    Note over Client,JL: 任一步失败：<br/>SQLite 停在 PREPARING，current_release_id 不变<br/>旧版本继续生效，Git commit + JSONL 记录保留供审计
```

**Watchdog**：后台定时扫描 `WHERE status='PREPARING' AND created_at < NOW() - 24h`，标记为 `ABANDONED`。Git 与 JSONL 不动。

### 4.4 场景 D：SkillEvolver 元 Agent 六步流程

```mermaid
flowchart LR
    F1[① 收集失败案例<br/>低分/棘轮拦截/P0 挂掉]
    F2[② 分析根因<br/>trigger 不准 / prompt 模糊<br/>依赖失效 / 边界缺失]
    F3[③ 生成候选<br/>3-5 个 patch<br/>每个附推理链]
    F4[④ 验证<br/>跑评估集 + 棘轮 + 客观指标]
    F5{⑤ 分级发布}
    L1[L1 自动发布<br/>examples/not_for/描述补充]
    L2[L2 挂 REVIEW<br/>trigger/instructions 修改]
    L3[L3 只出建议<br/>工具权限/安全约束]
    F6a[⑥ 归档到 Git<br/>runs/success/]
    F6b[⑥ 归档失败<br/>runs/failures/release_id.md<br/>含根因分析]

    F1 --> F2 --> F3 --> F4 --> F5
    F5 -->|L1| L1 --> F6a
    F5 -->|L2| L2 --> F6a
    F5 -->|L3| L3
    F4 -.->|验证失败| F6b

    style L1 fill:#e6f3e6
    style L2 fill:#fff5d6
    style L3 fill:#ffe6e6
    style F6b fill:#f3e6e6
```

### 4.5 场景 E：棘轮门槛判定

```mermaid
flowchart TB
    IN[输入：新旧评估分]
    H1{总分退步？}
    H2{效果分退步？}
    H3{任务完成度<br/>下降 ≥ 5%？}
    H4{鲁棒性<br/>下降 ≥ 5%？}
    H5{任一 P0 用例<br/>由通过转失败？}
    S1{任一维度<br/>变化 ≥ 10%?}
    BLOCK([阻断：DECLINED])
    REVIEW([挂 REVIEW])
    PASS([通过：可发布])

    IN --> H1
    H1 -->|Yes| BLOCK
    H1 -->|No| H2
    H2 -->|Yes| BLOCK
    H2 -->|No| H3
    H3 -->|Yes| BLOCK
    H3 -->|No| H4
    H4 -->|Yes| BLOCK
    H4 -->|No| H5
    H5 -->|Yes| BLOCK
    H5 -->|No| S1
    S1 -->|Yes| REVIEW
    S1 -->|No| PASS

    style BLOCK fill:#ffe6e6
    style REVIEW fill:#fff5d6
    style PASS fill:#e6f3e6
```

---

## 5. 数据模型

### 5.1 SQLite Schema

```sql
-- skills：Skill 主表，每个 Skill 一行
CREATE TABLE skills (
    name                TEXT PRIMARY KEY,
    current_release_id  TEXT,           -- FK → releases.release_id
    description         TEXT NOT NULL,  -- 冗余缓存，索引层拼接用
    use_when            TEXT NOT NULL,  -- 冗余缓存
    created_at          TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at          TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (current_release_id) REFERENCES releases(release_id)
);

-- releases：每次发布尝试一行（含失败和 ABANDONED）
CREATE TABLE releases (
    release_id          TEXT PRIMARY KEY,     -- UUID v4
    skill_name          TEXT NOT NULL,
    version             TEXT NOT NULL,        -- semver
    commit_hash         TEXT,                 -- Git commit（第 2 步回写）
    status              TEXT NOT NULL,        -- PREPARING / PUBLISHED / ABANDONED
    level               TEXT,                 -- L1 / L2 / L3 / MANUAL
    triggered_by        TEXT,                 -- evolver / manual
    eval_summary_json   TEXT,                 -- JSON 快照
    created_at          TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    published_at        TIMESTAMP,
    FOREIGN KEY (skill_name) REFERENCES skills(name)
);

CREATE INDEX idx_releases_status ON releases(status);
CREATE INDEX idx_releases_skill ON releases(skill_name, status);
```

**关键不变量**：
- `skills.current_release_id` 必须指向 `status='PUBLISHED'` 的行（外键 + 应用层校验）
- 同一 Skill 同时存在的 `PREPARING` 行不超过 1 条（Watchdog 兜底）

### 5.2 JSONL 结构

**evaluations.jsonl**（每次评估一行）：
```json
{
  "ts": "2026-07-26T14:30:00Z",
  "release_id": "550e8400-e29b-41d4-a716-446655440000",
  "skill_name": "weather_query",
  "eval_set": "baseline_dev",
  "structure_score": {"schema": 15, "trigger": 10, "prompt": 8, "deps": 5},
  "effect_score": {"task": 22, "robust": 13, "efficiency": 9, "readability": 9},
  "objective_metrics": {"turns": 3, "tokens": 850, "latency_ms": 1200},
  "judge_verdicts": [{"case_id": "c001", "verdict": "A_better", "reason": "..."}],
  "p0_pass": true
}
```

**router.jsonl**（每次路由一行）：
```json
{
  "ts": "2026-07-26T14:30:05Z",
  "query": "北京今天天气",
  "candidates": ["weather_query", "aqi_query", "typhoon_alert"],
  "hit_layer": "rule",
  "chosen": "weather_query",
  "scores": {"rule": {"weather_query": 90, "aqi_query": 30}},
  "latency_ms": 3
}
```

### 5.3 SKILL.md 规范（YAML frontmatter + Body）

规范要点：frontmatter 承载结构化元数据（Trigger/Evaluation 等），Body 承载 Instructions 与 Examples。此处只列 Pydantic Schema：

```python
class Trigger(BaseModel):
    keywords: list[str]

class Evaluation(BaseModel):
    last_score: float | None = None
    last_release_id: str | None = None

class SkillMeta(BaseModel):
    name: str  # snake_case
    version: str  # semver
    description: str
    use_when: str
    not_for: list[str] = []
    dependencies: list[str] = []
    trigger: Trigger
    examples: list[str] = []
    evaluation: Evaluation = Evaluation()
```

---

## 6. 目录结构

```
skillForge/
├── ARCHITECTURE.md                # 本文件
├── README.md                      # 快速上手 + CLI 用法
├── pyproject.toml
│
├── src/skillforge/
│   ├── __init__.py
│   ├── cli.py                     # CLI 入口：evolve / evaluate / route / publish
│   ├── models.py                  # Pydantic Schemas（SkillMeta, EvalResult, ...）
│   ├── registry.py                # SkillRegistry（继承 ToolRegistry）
│   ├── router/
│   │   ├── __init__.py
│   │   ├── rule.py                # 规则层
│   │   ├── embed.py               # bge-small 编码 + 检索
│   │   ├── llm.py                 # LLM 兜底
│   │   └── cascade.py             # IntentRouter 组装三层
│   ├── evaluator/
│   │   ├── __init__.py
│   │   ├── structure.py           # 结构分静态检查
│   │   ├── judge.py               # Judge 配对比较
│   │   ├── metrics.py             # 客观指标采集
│   │   └── ratchet.py             # 棘轮门槛判定
│   ├── evolver.py                 # SkillEvolver（继承 SimpleAgent）
│   ├── state_machine.py           # ReleaseStateMachine（SQLite 状态机）
│   └── storage/
│       ├── db.py                  # SQLite 封装
│       ├── git_ops.py             # Git 命令包装
│       └── jsonl.py               # JSONL 追加/查询
│
├── skills/                        # Skill 源码（Git 子目录，独立版本管理）
│   ├── weather_query/
│   │   └── SKILL.md
│   └── ...
│
├── evaluation_sets/               # 评估集（手工构造）
│   ├── baseline_dev.json          # 32 条开发集
│   ├── baseline_hidden.json       # 8 条已降级回归集（原 hidden 已降级为 seen regression；防过拟合由 holdout/audit 承接）
│   ├── baseline_seen_regression.json # 8 条已降级回归集（元 Agent 可见）
│   ├── repair_set.json            # 22 条迭代修复集（10 P0 + 8 seen regression + 4 boundary）
│   ├── experiment_holdout.json    # 9 条实验留出集（严格隔离黑盒比对）
│   ├── final_audit.json           # 9 条终审评测集（严格隔离发布终审）
│   ├── p0_cases.json              # 10 条 P0（core 链路棘轮硬门槛）
│   └── router_negatives.json      # 50 条硬负例
│
├── runs/                          # 运行时数据（Git ignore）
│   ├── evaluations.jsonl
│   ├── router.jsonl
│   ├── failures/                  # 元 Agent 失败候选归档
│   │   └── <release_id>.md
│   └── skillforge.db              # SQLite 单文件
│
└── tests/
    ├── test_registry.py           # Phase 1: 10 条基础链路
    ├── test_router.py             # Phase 2: 30 条路由边界
    ├── test_evaluator.py          # Phase 3: 评估器单测
    ├── test_state_machine.py      # Phase 3: 状态机 + 幂等
    └── test_evolver.py            # Phase 4: 元 Agent 集成
```

---

## 7. 关键接口签名（Python）

```python
# ===== SkillRegistry =====
class SkillRegistry(ToolRegistry):
    def load_skills_from_dir(self, path: Path) -> None: ...
    def build_index(self) -> str:
        """拼接所有 Skill 的 name+description+use_when 为一段文字索引"""
    def use_skill(self, name: str, reason: str) -> str:
        """特殊工具入口：加载 Skill Body 并写路由日志"""
    def get_current_release(self, name: str) -> Release | None: ...


# ===== IntentRouter =====
@dataclass
class RouteResult:
    chosen: str | None       # Skill name, None 表示拒绝
    hit_layer: Literal["rule", "embed", "llm"]
    scores: dict
    latency_ms: float

class IntentRouter(Tool):
    def route(self, query: str, candidates: list[str] | None = None) -> RouteResult: ...


# ===== SkillEvaluator =====
@dataclass
class EvalResult:
    release_id: str
    structure_score: dict[str, float]
    effect_score: dict[str, float]
    objective_metrics: dict[str, float]
    p0_pass: bool

@dataclass
class RatchetVerdict:
    decision: Literal["PASS", "REVIEW", "DECLINED"]
    reasons: list[str]        # 触发的门槛条目

class SkillEvaluator(Tool):
    def evaluate(self, release_id: str, eval_set: str = "baseline_dev") -> EvalResult: ...
    def check_ratchet(self, old: EvalResult, new: EvalResult) -> RatchetVerdict: ...


# ===== SkillEvolver =====
@dataclass
class Patch:
    skill_name: str
    level: Literal["L1", "L2", "L3"]
    diff: str
    rationale: str

class SkillEvolver(SimpleAgent):
    def evolve(self, skill_name: str, max_candidates: int = 5) -> list[Patch]: ...


# ===== ReleaseStateMachine =====
class ReleaseStateMachine:
    def begin_release(self, skill_name: str, version: str, level: str) -> str:
        """返回 release_id (UUID)。SQLite 插入 PREPARING 行"""
    def write_commit(self, release_id: str, patch: Patch) -> str:
        """Git commit → 回写 commit_hash 到 SQLite。返回 commit_hash"""
    def append_evaluation(self, release_id: str, result: EvalResult) -> None: ...
    def commit_release(self, release_id: str) -> None:
        """原子切换 PREPARING → PUBLISHED 并更新 current_release_id"""
    def watchdog_sweep(self, threshold_hours: int = 24) -> int:
        """清理过期 PREPARING → ABANDONED，返回清理数量"""
```

---

## 8. 架构决策记录（ADR 精简版）

| # | 决策 | 备选方案 | 选择理由 |
|---|---|---|---|
| ADR-01 | Agent 主导渐进式披露（use_skill 显式加载） | 加载器自动拦截 prompt 展开 | 可归因、可评估；不破坏 ReAct 可解释性 |
| ADR-02 | 三层路由级联（规则→embed→LLM）| 纯 embedding；纯规则；纯 LLM | 延迟-准确率-成本三角平衡；分层可归因 |
| ADR-03 | Judge 用配对比较不评绝对分 | 绝对打分；多 Judge 投票 | 对抗 Judge 分数漂移；单 Judge 成本可控 |
| ADR-04 | 结构分不阻断发布，只做完整性检查 | 结构分参与硬门槛 | 结构分本质是"表单校验"，不应决定质量 |
| ADR-05 | 元 Agent 分级发布 L1/L2/L3 | 全自动；全人工 | 风险分层：低风险自动化收益大，高风险坚决人工 |
| ADR-06 | SQLite 是唯一发布事实源 | Git 为源；文件锁 | 单事务原子性 + 幂等 UPDATE；单文件零运维 |
| ADR-07 | bge-small-zh-v1.5 本地 embedding | bge-m3；API embedding；无 embedding | 小模型 CPU 可推、中文场景足够、零外部依赖 |
| ADR-08 | Watchdog 清理 PREPARING 而非回滚 Git | 事务回滚 Git commit | Git commit 保留可审计价值；Watchdog 简单 |
| ADR-09 | 硬负例评测集降到 50 条（非 100-200） | 100+ 条 | 独立开发者手工造数据 4 周内可行的规模 |
| ADR-10 | 单进程模型（不引入队列/Worker） | Celery + Redis | 900 行预算 + CLI 演示定位不需要 |

---

## 9. 与方案书的关系（方案书已归档）

方案书 v3（含 A.2 一致性口径清单）已从仓库删除并归档，本文件自含完整架构视图：§1-§8 为设计基线，§10 记录实施差异。README 承载设计取舍与面试口径。

---

## 10. Phase 4 完成后的实际实现修订（2026-07-27）

**核心结论**：架构基线 §1-§8 与实际实现**高度一致**。以下是 8 处实施差异，按类型分组。

### 10.1 结构差异（4 处）

| # | 预估（§1-§9） | 实际实现 | 原因 |
|---|---|---|---|
| 1 | `evolver/` 拆包（failure_collector / root_cause / patch_generator / validator / publisher 5 文件） | **单文件 `evolver.py` 400+ 行**，内部函数 `_collect_failures / _analyze_root_cause / _generate_patches / _validate_patch / _publish_patch / _archive_failure` | 单文件更适合"六步 pipeline"的强连贯性；避免过度拆分。等有多个演化策略再拆包。 |
| 2 | `runs/failures/` 一个目录归档所有失败 | `runs/failures/` + **新增 `runs/suggestions/`** 分类归档 | L1 DECLINED 归档失败 / L2/L3 归档只出建议——两者语义不同应分开。 |
| 3 | `EvalResult` 只含 4 项汇总（structure/effect/objective/p0_pass） | **加 `case_verdicts` 与 `case_outputs` 两个 list 字段** | Phase 4 元 Agent 需要每 case 明细定位失败样本，不能只看汇总。 |
| 4 | `evaluation_sets/` 只列 4 个手工评测集 | **新增 `runs/blind_eval_samples.json`**（21 条盲评样本 + golden verdicts） | Phase 3 保底盲评产物；含 skill/baseline 输出对 + Judge/human proxy 双判定，属项目交付物应入库。 |

### 10.2 参数与阈值调优（3 处）

| # | 预估（§4-B） | 实际实现 | 原因 |
|---|---|---|---|
| 5 | 路由 embed `HIGH_CONF=?`，未定 | **`HIGH_CONF=0.75, MARGIN=0.10, LOW_CONF=0.35`** | 50 条硬负例评测集调优后的值；R@1 从 62% → 98%。 |
| 6 | Watchdog `threshold_hours=24` | 同（默认）但 **SQL 从 Python isoformat 改为 SQLite 内建 `datetime('now', '-Nh')`** | Python 带时区 isoformat 与 SQLite 无时区 CURRENT_TIMESTAMP 比较不匹配；改用 SQLite 内建时间函数。 |
| 7 | bge-small-zh-v1.5 走 HuggingFace | **走 modelscope**（阿里模型库） | 国内 hf-mirror 不稳（curl 通但 python-requests 不通），modelscope 40s 下完。`embed.py` `DEFAULT_MODEL_DIR` 硬编码本地路径。 |

### 10.3 已知缺陷与改进方向

| # | 缺陷 | 影响 | 改进路径 |
|---|---|---|---|
| 8 | Phase 3 冻结基线中 task_completion 分歧为 62% | 旧 Judge 将无 provenance 的天气数值判 A_better | P0-C 已加入 truth sentinel、INVALID/fail-closed、A/B 均衡和独立 Judge；使用 `rejudge_frozen.py` 只重判冻结输出后再验收。 |

### 10.4 数字对账

| 指标 | §1 设计预估 | 实际 |
|---|---|---|
| 代码量（Python） | ~900 行核心 | **1600 行核心 + 3300 行 tests/scripts** = 4907 行合计 |
| 路由 Recall@1 | ≥ 80% | **98%** |
| 路由 Recall@3 | ≥ 90% | **100%** |
| pytest 数量 | Phase 1 交付 10 | **74 全绿** |
| 元 Agent 成功率 | ~30% | **1/3=33%**（1 次真实迭代，L1 DECLINED 2 / L2 REVIEW 1 +4.90 分） |
| Judge/人工分歧 | < 30% 交付 | robustness 19% ✓, readability 24% ✓, **task_completion 62% ❌**（已知缺陷） |

### 10.5 一致性口径清单校对

**全部 14 条口径遵守**（100% 一致，Phase 4 完成后逐条校对）：
- ✅ 渐进式披露只有两层（元数据 + 完整 Body）
- ✅ Agent 主动调 `use_skill(name, reason)`，框架不拦截 prompt
- ✅ `trigger.keywords` 只作路由排序信号不做自动展开（Phase 2 首版违反，62%→98% 是修回口径的结果）
- ✅ 三层路由不写死百分比（`HIGH_CONF/MARGIN/LOW_CONF` 是置信度门槛而非覆盖率）
- ✅ Embedding 用结构化检索卡片（`[Capability][Use When][Examples][Not For]`）
- ✅ 硬负例评测集 50 条、Recall@1 ≥ 80% / Recall@3 ≥ 90%
- ✅ 结构分 40% 不阻断，效果分 60% 才是门槛
- ✅ 效率维度用客观 token 比不用 LLM 打分
- ✅ Judge 配对比较 A/tied/B/INVALID；异常与证据不足 fail-closed
- ✅ 棘轮硬 5 条 + 软 10%
- ✅ 元 Agent 是候选生成器不是最终决策者，成功率 ~30%
- ✅ L1 自动 / L2 REVIEW / L3 只出建议
- ✅ SQLite 唯一发布事实源，四步固定顺序
- ✅ 评估可信度声明四条（相对回归/非绝对/非全自动/非用户满意度）

**必须避免的叙述**也全部遵守（未硬编码百分比、未拉踩 LangChain、未承诺全自动等）。

---

## 11. Phase 5 实施修订（2026-09-06，P0-P2 全链）

### 11.1 与 §10 的衔接说明

§10 记录了 Phase 4 完成后的实施差异，确立了六步流水线、分级发布与 SQLite 唯一事实源等基线架构。但在后续实际演化实验中，系统暴露了深层瓶颈：**自进化信号不可信**（Judge 盲评在 task 维度分歧达 62%、缺乏防线拦截、测试集面临被反思提示词污染的数据泄漏风险、偶发网络抖动导致整批评估报废）。

Phase 5（2026-09-02 至 2026-09-06）围绕"让 AI 自我改进可信"推进三幕改造：
1. **P0 可信地基**：引入 semantic diff、改动面校验、fail-closed Judge 协议化与三层数据物理隔离，先装刹车与仪表盘；
2. **P1 受控回环**：构建双轮反思回环、预算 Ledger 与 8 重防自嗨体系，并在修复评估链容错后完成真实 LLM 对照跑批（P1-I）；
3. **P2 自生成生态**：引入 Skill 自动生成器、三维耦合拆分器、审计轨迹自动提取 badcase 闭环，以及用于状态黑板解耦的 LangGraph 旁路。

测试套件从 Phase 4 的 74 条扩展至 **316 条全绿**（8.8s），代码规模扩展至核心约 5000 行 + 辅助与测试约 8000 行。本节作为 Phase 5 实施基准修订，记录新增组件、防线体系、数据流变更及架构决策（ADR-11 至 ADR-15）。

---

### 11.2 新增组件与核心模块变更

| 模块 / 组件 | 路径 | 核心职责与架构设计 |
|---|---|---|
| **eval_tracer** | `src/skillforge/eval_tracer.py` | **样本级全量审计轨迹**：落盘包含 11 个核心字段（用例元数据、输入输出、八维明细得分、判定结论、延迟、Token 消耗等）的结构化日志；为 badcase 提取与反思闭环提供不可篡改的事实底稿。 |
| **skill_generator** | `src/skillforge/skill_generator.py` | **P2-A Skill 自动生成器**：自然语言需求驱动生成标准化 `SKILL.md` 与初始验证集；内嵌 BGE 向量距离 0.70 冲突阈值 fail-closed 拦截，支持原子注册入库与路由负例自动生成。 |
| **skill_splitter** | `src/skillforge/skill_splitter.py` | **P2-B 技能拆分裁决器**：基于数据依赖同构、流程逻辑纠缠、评测集交叉的三维耦合矩阵量化拆分收益；支持事务化发布与软弃用，实测对紧耦合场景（如 weather 同 fixture 多意图）执行正确拒拆。 |
| **langgraph_loop** | `src/skillforge/langgraph_loop.py` | **P2-D 受控回环 LangGraph 状态图旁路**：构建 7 节点 14 边的 `StateGraph`，提供状态黑板解耦与 `SqliteCheckpointer` 断点恢复（Durable Execution）；作为 shadow 旁路与主链双跑 100% 等价。详见 [docs/langgraph_loop.md](docs/langgraph_loop.md)。 |
| **data_partition** | `src/skillforge/data_partition.py` | **三层评测数据物理隔离**：严格划分 `repair_set`、`experiment_holdout`、`final_audit`；运行时校验物理边界，防止评测数据逆向渗透进反思提示词。 |
| **diff** | `src/skillforge/diff.py` | **确定性语义 diff 与等级计算**：解析 frontmatter 与 Body AST；计算 `computed_level`（L1/L2/L3），白名单校验修改字段，防止模型自报降级与越权改写。 |
| **models 变更** | `src/skillforge/models.py` | **护栏与上下文模型扩充**：新增 `EvolveBudget`（Token/调用硬帽与实时扣减）、`EvolveContext` / `AttemptFeedback`（两轮反思最小黑板上下文）、`DatasetLayerBundle` 等核心数据契约。 |
| **evolver 变更** | `src/skillforge/evolver.py` | **受控回环重构**：六步线性流水线升级为双轮受控反思回环（`max_rounds=2`）；内嵌 P0 发布门拦截、8 重防线裁决、A2 根因分支（路由/行为/依赖）与 shadow 隔离归档。 |

---

### 11.3 防自嗨体系清单（8 重防线）

自进化系统的核心风险不是"无法生成改动"，而是"改错后模型自洽并固化错误"。SkillForge 构建了 8 重深度防御防线：

| # | 防线名称 | 核心一句话机制 | 核心代码位置 |
|---|---|---|---|
| 1 | **Diff Policy（等级约束防线）** | 严格解析 AST 生成语义 diff 并重算 `computed_level`，L1 仅限特定白名单字段，禁止模型自报等级与降级越权。 | `src/skillforge/diff.py` (`compute_semantic_diff`, `classify_patch_level`) |
| 2 | **Provenance（真实性快照绑定）** | 验证时强绑定真实调用或合法 Fixture 响应凭证及其内容哈希，断言实时数据无凭证直接判 INVALID 阻断。 | `src/skillforge/evaluator/fixtures.py` (`ExecutionProvenance`), `evolver.py` |
| 3 | **Nonce Fixture（抗记忆防线）** | 工具模拟注入动态 Nonce 与时效随机戳，防止元 Agent 逆向硬编码 Mock 结果到 Skill Body 中自欺欺人。 | `src/skillforge/evaluator/fixtures.py` (`generate_nonce_fixture`, `verify_provenance`) |
| 4 | **按改动面咬合验证器** | 依据语义改动面精准挂接验证通道：改 metadata 强验 Router 负例，改 Body 强验行为集，改依赖进人工 REVIEW。 | `src/skillforge/evolver.py` (`_validate_patch`, `validation_channels`) |
| 5 | **邻近变体验证（泛化防线）** | 对失败 Query 自动衍生同构城市、日期、措辞等邻近变体集一并参测，防止仅针对单一失败用例局部过拟合。 | `src/skillforge/evolver.py` (`generate_neighbor_variants`) |
| 6 | **数据泄漏扫描（边界防线）** | 候选 Patch 进沙箱前执行文本扫描，严禁出现 Case ID、评测 Query 原句、标准答案片段或 Fixture 独有常量。 | `src/skillforge/evolver.py` (`scan_patch_leakage`) |
| 7 | **独立审计集隔离（物理防线）** | 建立 `repair`、`experiment_holdout`、`final_audit` 三层数据物理墙，Holdout 与终审数据永不可见且禁止参与反思。 | `src/skillforge/data_partition.py` (`validate_dataset_partition`), `evolver.py` |
| 8 | **指纹熔断（死循环防线）** | 基于规范化 SHA-256 维护 `seen_fingerprints` 集合，检测到与历史基线或上一轮候选完全同质的 Patch 时直接熔断停止。 | `src/skillforge/evolver.py` (`compute_skill_fingerprint`), `models.py` |

---

### 11.4 数据流变更（评估链自闭环与 `_auto_` Manifest）

在 Phase 5 之前，评测用例全量依赖人工设计；Phase 5 实现了**从运行轨迹到修复集的全自动闭环数据流**：

```mermaid
flowchart LR
    EvolRun["SkillEvolver<br/>真实迭代运行"] -->|"全量写入"| Traces[("runs/eval_traces/*.jsonl<br/>11 字段全样本轨迹")]
    Traces -->|"读取分析"| Extractor["scripts/extract_cases_from_traces.py<br/>badcase 提取器"]
    Extractor -->|"三道质量门过滤"| QualityGate{"质量门过滤<br/>①有效失败白名单<br/>②Provenance 凭证<br/>③Query 距离防重"}
    QualityGate -->|"合规 badcase"| ManifestWriter["_auto_ Manifest 写入器"]
    ManifestWriter -->|"追加写入"| RepairSet[("evaluation_sets/repair_set.json<br/>meta.auto_case_ids 清单")]
    RepairSet -.->|"驱动下一轮基线评测"| EvolRun
```

1. **样本级轨迹落盘**：`eval_tracer.py` 将每次沙箱评估的每条 Case 详细轨迹结构化落盘（`runs/eval_traces/*.jsonl`），实现执行与判定的完全可审计与可回放。
2. **三道质量门严格筛选**：`extract_cases_from_traces.py` 提取失败用例时实施 Fail-Closed 过滤：
   - 过滤环境异常与网络错误，仅收录符合有效失败白名单（`_is_effective_failure`）的业务逻辑缺陷；
   - 必须通过 Provenance 真实性凭证校验与 Content Hash 校验；
   - 执行 Query 归一化与相似度去重，防止低质量冗余样本稀释测试集。
3. **`_auto_` Manifest 严格对账机制**：
   - 所有自动化提取或生成的用例 ID 强制符合 `*_auto_*` 命名规范（如 `wq_auto_01`）；
   - `repair_set.json` 的 `meta` 元数据中必须显式维护 `auto_case_ids` 数组清单与计数；
   - 架构层强制保证：**文件内实际包含的 `_auto_` 用例集合与 `meta.auto_case_ids` 清单必须严格一致**，任何手动篡改或不一致均触发 Fail-Closed 拒绝加载。

---

### 11.5 新增架构决策记录（ADR-11 至 ADR-15）

| # | 决策 | 备选方案 | 选择理由（精简 ≤4 行） |
|---|---|---|---|
| ADR-11 | **Fail-Closed 容错粒度下沉至 Case 级** | 发生单条异常即整批报废；或将异常转 tied 冒充正常 | 单 Case 超时或不可解析按 INVALID 独立剔除；若整批 INVALID 率 >20% 则熔断阻断，在堵住假发布的同时将偶发网络报废率由 7/12 降至 1/10。 |
| ADR-12 | **真实性快照绑定（Provenance Binding）** | 候选验证重新发起外部请求获取动态数据 | 候选验证强绑定与基线同源的工具响应快照与哈希，彻底杜绝因外部数据漂移造成的虚假收益或误报退步。 |
| ADR-13 | **有效失败白名单（Effective Failure Allowlist）** | 所有未满分或异常均视作自进化目标 | 仅将外部事实不符、逻辑违背等实质业务缺陷纳为有效失败；严格将 Judge 基础设施崩溃、限流、网络抖动等环境噪声排除在自进化反思之外。 |
| ADR-14 | **三维耦合分析裁决 Skill 拆分** | 凭 Prompt 长度或单一直觉关键词拆分 | 基于数据依赖同构、流程编排纠缠、评测集交叉三维矩阵量化收益；避免盲目拆分导致数据源重复维护与工具契约分裂（实测精准拒拆 weather）。 |
| ADR-15 | **LangGraph 旁路独立探索不侵入主链** | 废弃 evolver.py 主循环并全量迁移至 LangGraph | 主链保持零语义变更的轻量确定性 for 循环守住生产稳定性；LangGraph 以 Shadow 旁路引入，复用原子节点并通过 7 场景双跑证明 100% 行为等价。 |
