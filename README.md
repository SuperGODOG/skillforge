# SkillForge · Agent Skill 自进化元 Agent 系统

> 定位为「**生产 Skill 的元 Agent 系统**」——基于 [hello-agents](https://pypi.org/project/hello-agents/) 扩展的元 Agent 工厂：Agent 主导加载、结构化路由、八维评估、元 Agent 半自动闭环。
>
> 4 周业余时间独立完成 · 4907 行 Python · pytest 74/74 全绿 · CLI 一键复现

---

## 4 阶段交付

| Phase | 内容 | 硬指标 | 结果 |
|---|---|---|---|
| **P1** 骨架 | 5 组件 + `use_skill` 三档降级 + SQLite 状态机 | 10 单测通过 | ✅ 10/10 |
| **P2** 路由 | 规则 + bge-small + LLM 三层级联 + 50 硬负例评测 | R@1≥80%, R@3≥90% | ✅ **R@1=98%, R@3=100%** |
| **P3** 评估 | 结构分 40 + 效果分 60（Judge 配对 + 客观 efficiency）+ 棘轮门槛 + 21 条保底盲评 | 出偏差报告 | ✅ 3 维 2 通过, task_completion 62% 分歧定位 Judge 幻觉盲区 |
| **P4** 元 Agent | 六步流程（收集→根因→生成→验证→分级→归档）+ L1 auto | 至少 L1 auto 打通 | ✅ 1 次真迭代产 L2 REVIEW +4.90 分 |

---

## Quick Start

### 环境（Ubuntu 24.04 / Python 3.12）

```bash
git clone https://github.com/SuperGODOG/skillforge.git
cd skillforge

# venv + 依赖
python3 -m venv .venv
./.venv/bin/pip install --index-url https://mirrors.aliyun.com/pypi/simple/ -e .

# bge-small 通过 modelscope 下载（国内网络 hf-mirror 不稳）
./.venv/bin/pip install modelscope
./.venv/bin/python -c "from modelscope import snapshot_download; snapshot_download('AI-ModelScope/bge-small-zh-v1.5', cache_dir='./models')"

# .env（DeepSeek API Key，见 tripplanner/backend/venv/.env 模板）
cat > .env <<'EOF'
LLM_API_KEY=sk-xxx
LLM_MODEL_ID=deepseek-chat
LLM_BASE_URL=https://api.deepseek.com/v1
EOF
```

### CLI（4 个命令覆盖 4 个 Phase）

```bash
# Phase 1: Agent 主动 use_skill 完整链路
./.venv/bin/skillforge demo --query "上海明天会下雨吗"

# Phase 2: 三层级联路由（默认规则+bge，加 --use-llm 走三层）
./.venv/bin/skillforge route "帮我写一个正则匹配所有邮箱" --use-llm
#   → chosen=None（硬负例被 LLM 判 not_for 拒绝）

# Phase 3: 八维评估器 + 棘轮
./.venv/bin/skillforge evaluate --skill explain_regex --eval-set baseline_dev --verbose
#   → 结构 40/40，效果 45.61/60，总 85.61/100，P0 全通

# Phase 4: 元 Agent 一次迭代
./.venv/bin/skillforge evolve --skill explain_regex --max-candidates 3
#   → 六步全跑通，patches 归档到 runs/failures/ 与 runs/suggestions/
```

### 复现 Phase 2/3 评测

```bash
# 路由 50 条硬负例评测（R@1=98% / R@3=100%）
./.venv/bin/python scripts/eval_router.py --use-llm

# Phase 3 保底盲评偏差报告
./.venv/bin/python scripts/blind_eval_report.py
```

### 跑测试

```bash
./.venv/bin/pytest tests/ -v
# 74 passed in 4s（10 registry + 18 router + 22 evaluator + 9 state_machine + 15 evolver）
```

---

## 项目结构

```
skillforge/
├── src/skillforge/
│   ├── __init__.py              # 5 组件 + 8 数据模型顶层暴露
│   ├── models.py                # Pydantic + dataclass
│   ├── registry.py              # SkillRegistry（继承 hello_agents.ToolRegistry）
│   ├── router/                  # 三层路由（rule / embed / llm / cascade）
│   ├── evaluator/               # 八维评估器（structure / judge / metrics / ratchet + __init__.py 装配）
│   ├── evolver.py               # SkillEvolver 六步闭环
│   ├── state_machine.py         # ReleaseStateMachine 4 步 + Watchdog
│   ├── storage/                 # db / git_ops / jsonl
│   └── cli.py                   # 4 子命令
│
├── skills/                      # 3 种子 skill（各含 SKILL.md YAML frontmatter）
│   ├── weather_query/           #   信息查询类
│   ├── write_weekly_report/     #   生成类
│   └── explain_regex/           #   教学类
│
├── evaluation_sets/             # 手工评估集
│   ├── baseline_dev.json        #   32 条开发集（元 Agent 可见）
│   ├── baseline_hidden.json     #   8 条隐藏集（防过拟合）
│   ├── p0_cases.json            #   10 条 P0（core 链路）
│   └── router_negatives.json    #   50 条硬负例（正/负例意图相反）
│
├── scripts/                     # 独立评测 / 盲评脚本
│   ├── eval_router.py           #   路由 Recall@1/@3
│   ├── blind_eval_run.py        #   保底盲评采样
│   └── blind_eval_report.py     #   Judge/人工偏差报告
│
├── runs/                        # 运行时（gitignore *.db / *.jsonl，保留 failures/suggestions）
│   ├── skillforge.db            #   SQLite 唯一发布事实源
│   ├── router.jsonl             #   路由归因日志
│   ├── evaluations.jsonl        #   评估审计日志
│   ├── blind_eval_samples.json  #   Phase 3 盲评样本（含 Claude 打的 golden）
│   ├── failures/                #   元 Agent DECLINED patches
│   └── suggestions/             #   元 Agent L2/L3 REVIEW patches
│
├── tests/                       # pytest 74 条
├── __log/                       # 4 周踩坑复盘
├── models/                      # bge-small 本地缓存（gitignore）
├── ARCHITECTURE.md              # C4 两级架构图 + 数据模型 + 接口签名
├── SkillForge-项目方案书-v3.md  # 方案书（含一致性口径清单 + 面试 Q&A）
├── ISSUES_LOG.md                # 4 周问题清单
├── INTERVIEW_PREP.md            # 面试深挖手册（面试官视角）
└── README.md                    # 本文件
```

---

## 核心设计决策（详见方案书 §4 + `__log/`）

1. **Agent 主导渐进式披露**：拒绝加载器拦截 prompt。注册 `use_skill(name, reason)` 特殊工具由 Agent 显式调用；每次加载写 `router.jsonl` 含 reason 归因链 —— **归因 100% 可回放，ReAct 可解释性无断点**。
2. **三层路由不写死百分比**：规则（`trigger.keywords` 只是排序信号，**不独占决策**）→ bge-small 结构化检索卡片（`[Capability][Use When][Examples][Not For]`，`Not For` 段在向量空间主动推远硬负例）→ LLM 兜底二选一。阈值 `HIGH_CONF=0.75 / MARGIN=0.10` 由 50 条硬负例评测集校准。
3. **Judge 配对比较（A/tied/B）**：对抗 Judge 分数漂移；效率维度走客观 token 比不用 LLM 打分；棘轮硬 5 条自动 DECLINED + 软门槛任一维度变化 ≥10% 触发 REVIEW（含上升，防"表面漂亮"）。
4. **元 Agent 分级 L1/L2/L3**：L1 = 只改 examples/not_for/description，可自动发布；L2 = 改 trigger/Instructions，只出建议；L3 = 改 dependencies/Constraints，只出建议。**成功率坦诚约 30%**；失败 patch 归档 `runs/failures/` 沉淀负样本。
5. **SQLite 唯一发布事实源**：Git（skill 内容版本）+ SQLite（发布状态）+ JSONL（评估/路由审计）三处异构存储，通过 SQLite 状态机 `PREPARING → PUBLISHED` 原子切换 + `release_id` UUID 幂等 + Watchdog 24h 清理孤儿。

---

## 关键文档

| 文档 | 用途 |
|---|---|
| [方案书 v3](SkillForge-项目方案书-v3.md) | 5W1H + 面试 Q&A 深展开 + A.2 一致性口径清单 |
| [ARCHITECTURE.md](ARCHITECTURE.md) | C4 两级架构 + 数据模型 + 接口签名 + 10 ADR |
| [ISSUES_LOG.md](ISSUES_LOG.md) | 4 周遇到的 9 个问题 + 定位 + 修复 + 教训 |
| [INTERVIEW_PREP.md](INTERVIEW_PREP.md) | 面试官视角深挖手册（20-30 min 追问链） |
| [__log/](__log/) | 3 个重大事件复盘（bge 下载 / 路由硬负例 / Phase 4 迭代） |

---

## 演化说明

- 方案书 v3 是**设计冻结点**（写于 Phase 1 前）
- ARCHITECTURE.md 是**架构基线**（Phase 4 完成后加了 §10 实际实现修订）
- `__log/` 与 `runs/failures/` `runs/suggestions/` 是**真实执行证据**（不是设计文档）

面试时看简历数字 → 简历对应本 README 各 CLI 命令 → 命令输出对应 `runs/` 里的真实文件。**所有数字都可被 `pytest` 或 `scripts/` 复现**。
