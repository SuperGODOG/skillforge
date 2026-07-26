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
