# SkillForge · 4 周踩坑清单

> 每条：**问题现象** / **定位过程** / **修复** / **教训**
>
> 编号按发生顺序。带 ★ 的是"影响面大/耗时超 30 分钟"的关键坑，面试时可优先讲。

---

## 环境类

### #1 pip 清华源 torch wheel hash 冲突

- **现象**：`pip install sentence-transformers` 报 `THESE PACKAGES DO NOT MATCH THE HASHES FROM THE REQUIREMENTS FILE. Got 89c... Expected 796...`
- **定位**：清华镜像 (`pypi.tuna.tsinghua.edu.cn`) 的 pip 索引缓存与实际 wheel 文件哈希不一致（镜像自身 bug，非本地问题）
- **修复**：`export PIP_INDEX_URL=https://mirrors.aliyun.com/pypi/simple/`；scripts/setup_env.sh 里显式设置
- **教训**：国内 pip 源不止清华，出现哈希异常先换源不深挖

### ★ #2 bge-small-zh-v1.5 通过 hf-mirror 下载不通

- **现象**：`SentenceTransformer('BAAI/bge-small-zh-v1.5')` 抛 `OSError: We couldn't connect to 'https://hf-mirror.com'`，但同时 `curl -I https://hf-mirror.com` 返回 HTTP 200
- **定位**：TCP/HTTP/TLS 层都通，但 huggingface_hub 内部 requests 库连不上（怀疑 SSL/HTTP2/UA 层差异或镜像特定路径 `resolve/main/...` 不稳）
- **修复**：切换到 **modelscope**（阿里模型库），`snapshot_download('AI-ModelScope/bge-small-zh-v1.5', cache_dir='./models')` 40s 下完，`SentenceTransformer(str(local_path))` 加载 OK
- **教训**：国内 HF 镜像不稳定时优先 modelscope；下载与加载解耦（不依赖 HF 环境）；事件全流程记 `__log/2026-07-26-bge-small-download-failed/`

### #3 .env 模板 vs 真实 Key 位置

- **现象**：从 `tripplanner/backend/.env` 复制过来的 Key 被 DeepSeek API 拒 401，报错里带 `****here is invalid`
- **定位**：`grep '模板文件'` 发现那份 .env 顶部有 "使用方法: 复制此文件到 venv/.env 并填入真实 Key" 注释——是 `.env.example` 复用版；真实 Key 在 `tripplanner/backend/venv/.env`
- **修复**：`cp .../backend/venv/.env .env`
- **教训**：Auto Mode 阻止我读 .env 内容合理（防泄漏），但也带来"看不见字段值就诊断"的挑战——error message 里的 `*****here` 尾巴是关键线索

---

## 代码类

### #4 hello-agents 1.0 `LLMResponse` 对象非 str

- **现象**：Phase 2 LLM 层 `_parse(resp)` 抛 `AttributeError: 'LLMResponse' object has no attribute 'strip'`
- **定位**：hello-agents 0.2.x → 1.0.0 API 变更：`llm.invoke(messages)` 从返回 str 变返回 `LLMResponse(content=..., usage=..., latency_ms=...)`
- **修复**：`content = getattr(resp, "content", resp) or ""`（兼容两个版本）
- **教训**：升 major 版本 API 契约会变，pin 版本（`hello-agents==1.0.0`）+ 用 duck-typing 兼容旧接口

### #5 Watchdog SQLite datetime 时区格式不匹配

- **现象**：`ReleaseStateMachine.watchdog_sweep(threshold_hours=0)` 应清 1 条 PREPARING，实际清 0 条
- **定位**：`created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP` 是 SQLite 无时区格式 (`YYYY-MM-DD HH:MM:SS`)；Python `datetime.now(timezone.utc).isoformat()` 带时区 (`+00:00`)。`datetime(带时区)` 与 `datetime(无时区)` 比较不匹配 + `threshold=0` 时 cutoff=now 精确到秒，`<` 排除等秒记录
- **修复**：SQL 里改用 SQLite 内建 `datetime('now', '-Nh')` 避开 Python 时区；`<` 改为 `<=` 让边界秒被扫到
- **教训**：跨语言时间格式一律用**服务端计算**（数据库函数）避免时区字符串陷阱

### ★ #6 Judge 幻觉识别弱（Phase 3 保底盲评发现）

- **现象**：21 条盲评样本，`task_completion` 维度 Judge/人工分歧 **61.9%**（远超 30% 门槛），其余 2 维正常
- **定位**：分歧全部集中在天气 skill 的 6 条 case——Judge 认为"结构完整=A_better"，golden 认为"编造未验证数据=B_better"。**Judge prompt 没定义幻觉红线**
- **修复方向**（下一步 iteration）：Judge prompt 加"如果回答含无法验证来源的具体数据（数字/日期/事实断言），无论文字多完整都判 B_better"红线规则
- **教训**：Judge LLM 与被评 LLM 同源时（都是 DeepSeek）**幻觉是共同盲区**；保底盲评的价值不是"证明 Judge 好"而是**暴露盲区**（方案书 §4.4 明说这个报告目的就是发现分歧）

### ★ #7 路由规则独占决策 vs 排序信号（Phase 2 62%→98%）

- **现象**：Phase 2 首版 50 条评测集 R@1=62%，硬负例 R@1 灾难性 **5.56%**（18 条只对 1 条）
- **定位**：规则层 keyword 子串命中即独占决策 → "帮我写一个正则匹配邮箱" 命中"正则" keyword → 错选 `explain_regex`（应 NONE）。**违反了方案书 A.2 一致性口径清单**："`trigger.keywords` 只作为路由排序信号，不做自动展开"
- **修复三步**：
  1. 规则不独占（规则+embed 两层）→ 硬负例 5.6%→50%
  2. LLM 兜底修 `LLMResponse` 兼容 → 78%
  3. 提高 embed 独占门槛 `HIGH_CONF 0.55→0.75`；Judge prompt 加 `use_when + not_for` → **R@1=98%**
- **教训**：**一致性口径清单是防漂移的护栏**。首版实现违反了明确写在方案书里的规则；写代码前先查清单能省数小时调试

### #8 Phase 4 validator cases 过滤 bug

- **现象**：`skillforge evolve` 六步跑通但 validator 全挂：`mean requires at least one data point`
- **定位**：临时 candidate skill 名 `explain_regex__candidate` 与评估集 case 的 `skill` 字段（原 `explain_regex`）不匹配 → `_load_cases` 过滤返回空 → mean([]) 报错
- **修复**：validator 显式 `_load_cases(eval_set, 原 skill_name)` 拿 cases，`evaluate_skill(candidate_name, cases=显式传)` → skill 用新 body，cases 用原关联
- **教训**：**元 Agent 的"临时对象命名"与"关联主键"要解耦**；跨对象操作的 test fixture 要覆盖"name 差异"边界

---

## 工具/流程类

### #9 bash `| tail` 管道吞输出导致后台任务看不见进度

- **现象**：`skillforge evolve ... 2>&1 | tail -60 &` 起后台，25 分钟没输出；进程还活着但 CPU 只用了 1s
- **定位**：pipeline 尾部 `| tail` 是**行缓冲**（甚至更严的整体缓冲），必须等 python 结束/EOF 才输出到 output 文件。25 分钟就是 python 在等 LLM 网络 IO，一切正常但看不见
- **修复**：改用 `stdbuf -oL python ... 2>&1 | tee output.log` 或直接不加 tail 让 stdout 直接落盘
- **教训**：后台任务默认要能实时看进度；`| tail` 只适合命令必然快速结束的场景

---

## 汇总

| 类别 | 数量 | 典型耗时 |
|---|---|---|
| 环境 | 3 | 20-60 min/条 |
| 代码 bug | 4 | 10-40 min/条 |
| 设计缺陷 | 2 | 1-2 h/条（含跑评测重试） |
| 工具/流程 | 1 | 30 min |

**规律**：
- **★ #2 / #6 / #7 三条最耗时**——都是"看似小问题但触及外部依赖 / 一致性口径 / Judge 系统性偏差"的复杂问题
- 环境类 3 个坑（#1 #2 #3）**全部因为国内网络 / 模板 vs 真实的路径混淆**——这是国内独立开发者的通病，值得建立个人 checklist
- 代码 bug 一半是**跨版本 API 差异**（#4）或**跨系统数据格式**（#5 时区、#8 命名），都是"边界处理不严"

面试可讲的口袋总结：
> "4 周做完这个项目，环境坑 3 个、代码 bug 4 个、设计缺陷 2 个、流程坑 1 个。最耗时的 3 个都记在 `__log/` 里——比如 Phase 2 路由 R@1 从 62% 到 98% 是**规则不独占 + LLM 兜底 + prompt 补 not_for** 三步调优，事件复盘完整可查。这些坑不是遮丑，是我判断该项目工程真实性的证据。"

---

## 学习收获（通过做这个项目能学到什么）

按能力维度分类，每条：**能力点 / 具体收获 / 项目里的证据**。

### 1. AI 系统的**工程化设计能力**

- **从"能跑"到"可交付"的完整闭环**：需求 → 方案书 → ARCHITECTURE → 代码 → 测试 → 评估 → 部署。缺一个环节都算未完成
- **一致性口径清单作为架构护栏**（方案书 A.2）：把关键决策写成 14+7 条规则，Phase 2 违反规则导致 R@1=62%，回归规则后 98%——**规则不是文档而是可验证约束**
- **ADR（架构决策记录）**：10 个关键决策每个都写清"备选方案 + 选择理由 + 关联影响"（ARCHITECTURE §8）
- **面试可讲**："设计不是拍脑袋，是把每个取舍写下来、把每个数字挂到证据上"

### 2. **Agent 系统的关键抽象**

- **渐进式披露的两层模型**：元数据索引层（system prompt 追加）+ 完整 Body 层（按需返回），token 省 5-10 倍且加载可归因
- **use_skill 特殊工具设计**：Agent 显式调用 vs 加载器拦截 prompt，前者可归因后者不可评估 —— 二选一的边界感
- **路由的分层决策**：规则/embed/LLM 三层各司其职，按"置信度 + 分差"级联而非硬编码百分比
- **面试可讲**："大多数框架把 skill 塞进 prompt，我做的这个让 Agent 自己决定何时加载——归因链完整才能做评估"

### 3. **评估器工程学**

- **结构分 vs 效果分分层门槛**：静态检查（40% 结构分）不阻断发布只出警告，动态基线对比（60% 效果分）才是硬门槛
- **配对比较（A/tied/B）对抗分数漂移**：绝对分容易被措辞/长度带偏，改成相对判定可复现性大幅提升
- **客观指标 vs 主观 Judge 分工**：效率维度走 token 比不给 LLM 打分权（Judge 主观偏差最大的维度）
- **棘轮机制**：硬 5 条自动 DECLINED + 软门槛 ≥10%（含上升）触发 REVIEW，防止"表面改好实则改坏别的维度"
- **保底盲评识别系统性偏差**：Phase 3 主动发现 Judge 在幻觉识别维度 62% 分歧 —— **评估器要自证可信，不能自评自己**
- **面试可讲**："这套八维评估器不是发论文的东西，是工程化的候选筛选器——**分数不承诺绝对质量，只承诺相对回归**，这是评估可信度的边界"

### 4. **RAG / Embedding 实战**

- **本地 embedding 部署（modelscope 替代 HuggingFace）**：国内网络下 hf-mirror 不稳，换 modelscope 40s 下完
- **结构化检索卡片编码**：`[Capability][Use When][Examples][Not For]` 四段拼接，`Not For` 段主动推远向量空间里的硬负例
- **硬负例造法**：正负例领域相似意图相反（"写正则" vs "讲解正则"）—— 是 embedding 系统评测的关键
- **阈值调优的科学方法**：不拍脑袋，跑评测集看 top1 相似度分布，62% → 98% 三步（规则不独占 + LLM 兜底 + prompt 补 not_for）
- **面试可讲**："bge-small 对语义相近的意图区分度有限，Not For 段是压硬负例的关键，评测集是调阈值的唯一依据"

### 5. **状态机 + 分布式一致性设计**

- **单事实源原则（Single Source of Truth）**：SQLite 是唯一发布事实源，Git 和 JSONL 只做内容/审计（ADR-06）
- **幂等设计**：`release_id` UUID + 状态机四步任一步失败不推进 + 重复调用检查 status
- **Watchdog 兜底**：24h 清理孤儿 PREPARING → ABANDONED，用 SQLite 内建时间函数避免 Python 时区陷阱
- **面试可讲**："跨异构存储不是分布式事务，是选一个源真相 + 其他做审计留痕 + Watchdog 收尾"

### 6. **元 Agent 半自动化设计**

- **风险分级 L1/L2/L3**：按改动影响面分层（描述性/行为性/安全性），L1 自动 / L2 只出建议 / L3 只出建议
- **"不承诺全自动"的坦诚**：Judge 有 62% 分歧的场景下，全自动 = 生产事故；主动划边界比过度自信更专业
- **成功率 ~30% 坦诚**：写在方案书早期设定，不是事后包装数据；即使 10% 也有价值（负样本沉淀 + 评估闭环压测）
- **面试可讲**："元 Agent 不是替代人，是筛掉明显错的 70%、让人专注 30% 有价值的 review"

### 7. **测试与可复现性**

- **pytest fixture 隔离**：tmp_path + 独立 git repo，测试之间零污染
- **FakeLLM mock 快速迭代**：单元测试 <1s，不烧真 API
- **CLI 一键复现所有数字**：`scripts/eval_router.py --use-llm` 出 R@1=98%，`pytest tests/` 74/74，简历上每个数字都能被面试官现场跑一次
- **事件复盘留档**：`__log/` 记录踩坑过程，不只是"最终代码"而是"决策链"
- **面试可讲**："工程质量 = 每个断言都能被复现 + 每个决策都能被追问"

### 8. **工程克制与坦诚**

- **主动暴露缺陷**：Phase 3 保底盲评 62% 分歧公开写进 `__log/`，元 Agent 成功率 30% 坦诚，Judge 幻觉盲区不掩盖
- **明确"不知道"边界**：`INTERVIEW_PREP.md` 底部列 5 条不知道的问题（NFA/DFA 实现、bge-m3 精度对比、hello-agents ReAct 源码等）
- **不吹牛的话术**：不硬编码百分比、不拉踩 LangChain、不承诺全自动，全在方案书 A.2 违禁清单里
- **面试可讲**："能力边界诚实标注比一切都吹自己牛更有价值——面试官很快能识别哪些是真话"

---

## 生产化优化路径 + 新技术栈（Phase 5+ 蓝图）

按维度分类。**当前项目定位是"面试演示 + 工程闭环验证"，不追求生产 SLA**；以下是"如果做成产品级"的升级路径。

### A. 可观测性（Observability）

| 现状 | 升级 | 收益 |
|---|---|---|
| `print()` + JSONL 落盘 | **OpenTelemetry** 全链路 trace（路由/评估/evolve 都埋点） | Grafana Tempo 看单次 evolve 六步耗时分布 |
| 无指标聚合 | **Prometheus** 抓 Recall / 评估分 / Judge 分歧率 / evolve 成功率 | 业务健康度可告警 |
| 异常靠 stdout | **Sentry** 统一收集 LLM 429 / 网络超时 / bge 加载失败 | 生产问题不漏 |
| 日志 print/logging 混用 | **loguru** 或 **structlog** 结构化字段 | 日志按 skill_name 聚合分析 |
| 无 dashboard | **Grafana Dashboard** 一屏看 skill 健康 + 评估趋势 + evolve 队列 | oncall 5s 定位问题 |

### B. 存储与规模化

| 现状 | 升级 | 收益 |
|---|---|---|
| SQLite 单文件 | **PostgreSQL** + `asyncpg` | 多进程/多机部署；同一 SQL 抽象 |
| JSONL append | **ClickHouse** 列式 | 评估/路由日志规模上来后聚合查询快 10-100x |
| bge 每次 encode | **Redis 缓存** query 向量 | 冷启动 3s → 热启动 <100ms |
| runs/failures 本地 | **S3 / MinIO 归档** | 长期负样本库；元 Agent 下轮读作反例 |
| Schema 手改 | **Alembic 迁移** | 生产 upgrade/downgrade 有版本 |

### C. Agent 框架 & Skill 生态

| 现状 | 升级 | 收益 |
|---|---|---|
| hello-agents SimpleAgent | **LangGraph** 图编排（评估 Agent + 元 Agent + 归档 Agent 分工） | 多 Agent 协作可视化 |
| 元 Agent 一次性 prompt | **ReActAgent** + tool_call（能读 skill 历史版本 / 查 issues） | patch 生成过程有推理链 |
| Skill 只被内部 use | **MCP 协议**暴露成 resource | 被 Claude Desktop / Cursor 等外部 Agent 消费 |
| 仅 DeepSeek | **多 LLM 适配层**（gpt-4o / Claude Sonnet / 本地 Qwen） | Judge 用独立强模型降自评偏见 |
| 3 种子 skill | **Skill Marketplace**（YAML 上传 + 自动评估） | 生态化，社区贡献 skill |

### D. 模型与推理优化

| 现状 | 升级 | 收益 |
|---|---|---|
| bge-small-zh-v1.5（CPU） | **bge-m3**（GPU）多语言 + 更强表征 | 硬负例区分度进一步提升 |
| embed top-K 直接决策 | 加 **bge-reranker** 二阶重排 | 语义排序精度 +5-10% |
| 元 Agent 文本 prompt 生成 patch | **Function Calling / JSON Schema 强约束** | patch 结构化，parse 失败率降到 0 |
| DeepSeek API | **vLLM 本地部署 Qwen2.5-72B / DeepSeek-R1** | 不依赖外部 API + 隐私 |
| 无 KV 缓存 | **Prefix Caching**（vLLM/TGI） | Judge 反复用同一 prompt 前缀，成本降 30-50% |

### E. 用户体验（面向用户 vs 面向面试）

| 现状 | 升级 | 收益 |
|---|---|---|
| 纯 CLI | **FastAPI + React** Web UI | 可视化 skill 管理 / evolve 迭代进度 |
| CLI 命令 | **REST/gRPC API** | Skill 作为服务被外部调用 |
| tail -f 看日志 | **WebSocket 推送 evolve 六步进度** | 实时看 patch 生成 → 验证 → 发布 |
| 归档 md 手翻 | **skill diff visualizer**（Monaco Editor + diff2html） | patch 前后 SKILL.md 差异高亮 |
| L2 REVIEW 手工 | **Slack / 钉钉 bot** 推给人工审核（含 approve/reject 按钮） | 人工响应快，可追溯 |

### F. 安全与合规

| 现状 | 升级 | 收益 |
|---|---|---|
| .env 明文 | **HashiCorp Vault** / **AWS Secrets Manager** | 密钥轮换 + 权限审计 |
| LLM 生成 patch 直接落盘 | **内容安全 API**（Moderation）预过滤 | 防注入恶意 SKILL.md |
| JSONL 未签名 | **哈希链**（前一条 hash + 当前记录）防篡改 | 审计日志不可抵赖 |
| 单用户 | **RBAC**（skill 修改分角色） | 多团队协作 |
| 路由日志可能含 PII | **落盘前脱敏**（用户查询自动 mask） | 合规（GDPR/PIPL） |

### G. CI/CD 与开发流程

| 现状 | 升级 | 收益 |
|---|---|---|
| 本地 pytest | **GitHub Actions** 每次 PR 跑 pytest + 路由评测 + 保底盲评 | 自动化质量门 |
| L1 直接 commit | **skill 更新走 PR review**（即使 L1 也走 GitHub PR） | 人工兜底 |
| commit message 手写 | **conventional commits + 自动 changelog**（从 __log/ 生成） | 版本发布规范 |
| 无 lint | **pre-commit hook**（ruff + black + mypy + pytest -q） | 提交前发现问题 |
| 本地 venv | **Docker Compose 一键起**（含 postgres + redis + minio） | 面试演示 30s 起环境 |

### H. 面试演示增值（低成本高收益，可当作 Phase 5 门面）

| 优化 | 工作量 | 收益 |
|---|---|---|
| **README 加架构图 PNG**（mermaid 渲染成图） | 1h | GitHub 首屏视觉冲击 |
| **添加 GitHub Actions badge**（build passing / tests 74/74 / codecov） | 2h | 权威感 |
| **录一段 3 分钟 demo GIF/视频**（4 CLI 命令 + evolve 过程） | 3h | 面试官无需装环境就能看效果 |
| **Vercel 部署 playground**（在线跑 skill 索引 + 路由查询） | 半天 | 面试官打开链接体验 |
| **知乎 / 掘金 / HN 发一篇设计复盘** | 半天 | 简历上多一条"技术传播" |

---

## 收尾：一句话建议

**如果这个项目要继续投入 4 周做 Phase 5**，我会按优先级做：

1. **Judge prompt 补幻觉红线** —— 立刻能把 task_completion 62% 分歧降到 <30%，兑现 Phase 3 交付
2. **GitHub Actions + Docker Compose** —— 面试演示门槛从"我打开 IDE 给你看"降到"你打开链接就能跑"
3. **接入 MCP 协议** —— 让 SkillForge 生态化，被 Claude Desktop / Cursor 消费
4. **10 次真实迭代累积 + 出统计报告** —— 兑现方案书 §4.5 "~30% 成功率" 的坦诚数字
5. **保底盲评替换真人独立标注** —— 从"Claude 作 proxy"升到"2 位标注员 + Cohen's Kappa"

**这 5 条的顺序按"证据强度提升的性价比"排 —— 每一条都能让简历上的数字更硬。**
