# 本地证据看板：实现与验收记录

首版日期：2026-11-22；D5 更新日期：2026-11-23。状态：**D1–D4 与 D6 离线演示导出已完成本地验收；D5 真实 V1 生成和执行已验证，真实 V2 修补尚未验证。原实验含 Oracle 误报，已封存原成绩并单独离线复核。**

## 本轮范围

提供运行证据、技能演进、评测对照三页本地只读看板。读取已有产物，不调用模型、不修改运行状态、不执行恢复、不批准发布。首版未改动 Pi 引擎、业务 Oracle、RepairJob 或正式技能库；D5 对业务 Oracle 的定点修复见文末。

| 阶段 | 实现 | 状态 |
| --- | --- | --- |
| D1 数据适配 | 有界文件扫描、SQLite 只读投影、Pi 报告、P6 分区、候选版本摘要与数据完整度 | 完成 |
| D2 本地入口 | 回环 HTTP 服务、快照 API、CLI 子命令、包内页面资源 | 完成 |
| D3 页面与验收 | 三页交互、来源关联、父子版本预览、逐任务对照、移动端与 HTML 注入检查 | 完成 |
| D4a 新演进记录 | 当前 Pi 链产生两个失败 Episode、正常回归、RepairJob、共同验证、临时库晋升及新任务复用 | 完成 |
| D4b 看板关联 | 来源运行跳转、基线快照对比、配置与数据集绑定、检索复用强关联、浏览器验收 | 完成 |
| D4a 演进证据补齐 | 基于当前 Pi 真实执行链、双失败 Episode 归因、修复验证、显式门禁晋升与未来复用的全链路证据生成 | 完成 |
| D5a Pi 文本适配 | 官方 SDK 单轮文本、共享预算、实际 usage、失败闭锁与脱敏；本地自定义模型多轮恢复受既有绑定约束 | 定点通过 |
| D5b 真实生成与演进 | 真实 V1 生成、DEV/锁定执行与独立判据复核；诊断引用无效被拒绝，没有合法 V2 | 部分验证，不能整阶段宣告通过 |
| D6 演示与分享 | 项目内单 HTML 原子导出、离线浏览三页、原分数与复核保留、三分钟导览 | 完成 |

最初委派给 AGY 的 D1 任务经历网络中断及等待连接超时，约 46 分钟后仍无数据文件落盘。经结果与运行状态核对后，已使用 `cancel_task` 终止 `task_skillforge_0a6869_1795349315_8db198`，返回明确 CANCELLED。随后由 Codex 按既定契约补齐数据读取层、定点测试与浏览器验收；没有重复并行派工，没有把 AGY 的在途状态当作交付证据。

## 变更文件

- `src/skillforge/dashboard_data.py`：只读 DTO；扫描最多 150 个来源、深度 5、每表或任务列表最多 500 条、单 JSON 最多 10 MiB；截断与损坏提示。
- `src/skillforge/dashboard.py`：固定绑定 `127.0.0.1`；只提供页面和快照；Host/Origin 校验及响应安全头；正常退出释放端口。
- `src/skillforge/dashboard.html`：自包含页面，无外部字体或脚本，无前端构建依赖；数据通过 `textContent` 渲染。
- `src/skillforge/cli.py`、`pyproject.toml`：增加 `dashboard` 子命令与 HTML 包资源声明，保留旧命令行为。
- `tests/test_dashboard_data.py`、`tests/test_dashboard_server.py`：数据与真实本地 HTTP 验证。
- `README.md`、`docs/DASHBOARD.md`：看板入口及使用说明。

SQLite 不通过 Store / Runtime 初始化：使用 `mode=ro` 与 `query_only`，不迁移数据库。检查点只读公开 ID、epoch、意图版本与时间，不读取或导出其中的消息及模型配置列。不同来源范围内的同名运行 ID 不直接合并；同范围 JSON 与数据库保留来源引用。页面状态来自持久化记录，不作为进程存活证明。

## 实际数据核对

本地当前读取 **30 个来源、11 条运行记录、11 条 Episode 记录、3 条实验版本摘要和 5 份实验档案**。这是载入记录数；不同复核来源可以包含同一次运行，不当作独立业务样本数。

三个版本来自 P6 实验摘要，明确标记 `RECORDED / experiment_summary`，正文仅有预览时直接注明。报告未保存的来源 Episode ID、当前权威验证记录及正式发布凭据保持未记录；不从开发失败数量或“成功”标签补造血缘。

真实模型 P6 分区按现存逐任务记录重新统计：

| 分区 | A | B | C | B→C |
| --- | --- | --- | --- | --- |
| DEV | 6 项逐任务判定缺失；不以汇总补明细 | 4/6 | 6/6 | 改善 2、退化 0、不变 4 |
| LOCKED_EVAL | 5/6 | 5/6 | 6/6 | 改善 1、退化 0、不变 5 |

这是历史实验原记录的展示，不是本轮新模型实验。scripted 前瞻性对照、事后重分区探索、同构探索与事后挑战各自保留独立档案。没有重新证明历史冻结或任务族独立性，没有重新评分或纠正旧 Oracle 判定；原回答、失败原因与判定不一致时保留原貌，供进一步核查。

原报告汇总与逐项可核验数据分开；缺失用量为 null，SDK 零价格占位不作为真实账单。最终快照仅有一项数据提示：真实模型 DEV / A 缺少 6 项逐任务判定。浏览器验收临时目录不纳入业务证据扫描。

## 验证命令与结果

Codex 执行：

```bash
env -u PI_SOURCE_ROOT .venv/bin/pytest \
  tests/test_dashboard_data.py \
  tests/test_dashboard_server.py \
  tests/test_pi_cli.py -q
```

**28 passed in 9.26s，exit 0**，由 10 项数据测试、17 项服务测试及 1 项既有 Pi CLI 测试组成。未跑全仓库测试，不与前轮测试数量累加。

覆盖现实 SQLite schema 与父子来源关联、旧库缺字段、损坏来源继续、无库不创建库、DB SHA256/mtime 不变、符号链接及目录越界拒绝、缺组与缺 usage 保持未知、配对退化与 infra 保留、真实/受控/探索档案分离、镜像不重复作为新实验、被替代故障样本不隐藏、敏感信息不导出，以及真实 HTTP 的写请求拒绝、任意路径拒绝、Host/Origin、响应头和 CLI 分发。

真实本地 Chrome（1440px 桌面与 390px 移动端）验收：三页导航、父子正文预览、DEV/LOCKED 切换、A/B/C 回答展开、筛选与刷新；HTML 注入作为普通文字显示，没有创建图片节点或执行脚本；没有浏览器异常或页面横向溢出。

读取及页面刷新前后，对 **30 个原始来源文件**比较 SHA256 和 mtime：**全部未变**。`git diff --check` 通过。本轮没有真实模型调用、安装依赖、commit 或 push。

本地验收产物位于 `scratch/dashboard-acceptance/`（未纳入版本控制）：

- `test_receipt.json`：最终测试命令与结果。
- `browser_receipt.json`：浏览器交互与注入检查。
- `source_integrity_receipt.json`：原始来源文件只读性核对。
- `snapshot.json`：本轮导入快照。
- `runs.png`、`evolution.png`、`benchmarks.png`、`locked.png`、`mobile.png`：页面截图。

## D4a 演进证据补齐实测记录

日期：2026-11-23。状态：**已完成端到端定点测试与只读快照导入验收**。

### 范围与实现

为本地看板提供真实官方 Pi 执行链从失败归因到修复晋升、再到未来复用的完整逐节点追溯证据：

- `src/skillforge/scenarios/dashboard_evolution_demo.py`：
  - 唯一子目录 `dashboard_evolution_<uuid>` 隔离生成全部 SQLite（`runtime.db`、`episodes.db`、`candidates.db`、`skills.db`）、技能源码、临时 Git 夹具与原子 `report.json`。
  - 明确证据标记：`model="scripted / faux"`、`data="synthetic"`、`execution="official_pi_runner_with_agent_runtime"`、`os_sandbox="unverified"`、`cost=null`。不联网、不调用外部付费模型、不篡改全局 Mock 数据。
  - 建立隔离基线技能 `1.0.0`，通过真实 Pi 运行完成两个不同任务 ID 的 learning 失败（`ORD_2026_0901` 遗漏 `PKG_102`、`ORD_2026_0902` 遗漏 `PKG_202` 并冒称全部签收），由 `verify_logistics_fulfillment` 独立拦截并回收失败 Episode 与工具 provenance；另完成一个 normal 成功任务（`ORD_DEV_0101`）。
  - 执行真实 `repair_skill_failure(max_attempts=1)`：归因至 `skill/prompt`，由 ScriptedPatcher 生成 `1.0.1` 候选，经由 `DynamicPiEvaluationBackend`（Pi 运行器）与 `DeterministicJudgeLLM` 在正常案例与目标案例回归评测中通过棘轮门禁，生成 READY 状态候选与关联 2 个真实失败 Episode ID 的验证记录，不虚构 `parent_candidate_id`。
  - 执行 `promote_repaired_skill`：验证 `caller_confirmed=False` 严格被拒且保持 `1.0.0`；隔离演示批准 `caller_confirmed=True` 后经由 `ReleaseStateMachine` 晋升至 `1.0.1`。
  - 重建运行时执行 `start_run(enable_reuse=True, require_reuse=True)`：零配置自主检索复用 `1.0.1` 技能且 `frozen_body` 一致，真实 Pi 运行产出新 success Episode，旧运行严格冻结在 `1.0.0`。
- `tests/test_dashboard_evolution_demo.py`：测试实际启动官方 Pi runner 执行演进链，并通过 `dashboard_data.build_snapshot` 验证导入规范性、跨实体 Canonical ID 关联完整度、数据库只读性（读取前后 SHA256 与 mtime 不变）及重复调用目录隔离。

### 验证命令与结果

```bash
env -u PI_SOURCE_ROOT .venv/bin/pytest tests/test_dashboard_evolution_demo.py -q
```

AGY 首次报告 **2 passed，exit 0**。Codex 核对后补齐最终回答持久化、第二个失败案例回归、设置阶段异常与逐阶段报告、连接关闭、严格 hash/config/dataset 绑定断言及未知合成数据拒绝；补强后的单文件验证为 **3 passed in 30.94s，exit 0**。

### 现场数据与血缘关系核对

- 运行记录：载入 4 条核心业务运行（`run_learning_fail_1`、`run_learning_fail_2`、`run_learning_normal_1`、`run_future_reuse_1`）及评测沙箱子运行；
- 经历记录：载入 2 个真实失败 Episode、1 个正常 Episode、1 个复用成功 Episode（版本 `1.0.1`）；
- 候选版本：载入 1 条权威 Candidate，来源 `source_episode_ids` 恰包含 2 个失败 Episode ID，无虚构 `parent_candidate_id`；
- 验证与发布：载入 1 条 `PASS` 验证记录及关联的正式 Release `1.0.1`；
- 关联修复作业：`RepairJob` 处于 `PROMOTED` 状态，关联 candidate 与 release ID 完全一致；
- 只读性：`runtime.db`、`episodes.db`、`candidates.db`、`skills.db` 在 `build_snapshot` 读取前后 SHA256 与 mtime **100% 保持未变**。

### D4b Codex 独立核对与最终验收

读取层只新增 Episode 中四个公开检索绑定字段：`enable_reuse`、`retrieval_hit`、`selected_skill`、`selected_version`。完整 environment、检查点消息与模型凭据仍不导出。页面从真实 ID 查找来源运行；基线执行快照必须来自来源 Episode 的运行、且版本与验证基线相同。后续复用要求同范围、验证记录已晋升、发布名称与候选版本一致、运行完整正文相同，以及 Collector 保存的严格布尔检索命中记录；缺项保留未关联。正文预览、跨目录同名 ID、仅同名 Skill 和字符串 `"true"` 均不作为复用依据。

新页面可从两个来源 Episode 跳到各自实际工具轨迹与回答，显示基线 `1.0.0` 与候选 `1.0.1` 正文对比、RepairJob、验证配置和数据集绑定、临时库发布以及新任务 `run_future_reuse_1` 的新 Episode。

Codex 实际运行：

```bash
env -u PI_SOURCE_ROOT .venv/bin/python -m skillforge.scenarios.dashboard_evolution_demo --output-dir runs
env -u PI_SOURCE_ROOT .venv/bin/pytest \
  tests/test_dashboard_data.py tests/test_dashboard_server.py \
  tests/test_dashboard_evolution_demo.py tests/test_pi_cli.py -q
```

演示命令 exit 0；最终联合验证 **33 passed in 39.24s，exit 0**，不是与历史测试相加。包含真实 Pi 子进程执行、规范 Store 来源、验证和发布关系、旧运行冻结、重建 Runtime 检索、设置失败留诊断、重复调用目录隔离、读取只读性、字段隐私、浏览器端关联函数的反例验证及既有 HTTP/CLI 回归。

本轮保留的现场目录：`runs/dashboard_evolution_40916fa624a3/`（已加入 `.gitignore`，可按命令重建，不提交生成数据库或嵌套 Git 仓库）。其中：

- 4 个 learning 运行与 Episode：两个失败、一个正常成功、一个晋升后复用成功。
- 12 个实际评测子运行与 Episode：purpose=evaluation，outcome=unknown，不混入两个学习来源。
- 1 个修订候选 `cand_repair_015f2b6b1caf`，1 条 PASS 验证，发布 `cb1133ae-b576-47af-87ee-a0e003a7b12b`，版本 `1.0.1`。
- 1 个逻辑 RepairJob `job_ebdfbf832425`；episodes.db 与 skills.db 各保存一份 PROMOTED 记录。看板保留两个来源副本，不当作两个独立修补作业。
- 两条来源 ID 为 `ep_run_learning_fail_1` / `ep_run_learning_fail_2`；新复用 Episode 为 `ep_run_future_reuse_1`。不同演示目录中的同名 ID 仍按范围隔离。

加入该目录后，默认全量快照载入 **35 个来源、27 条运行记录、27 条 Episode、4 条候选/摘要记录、1 条验证、1 条发布、2 份 RepairJob 来源记录、5 份实验档案**。固定历史报告仍读取，故仅指定新目录时也会载入三个旧版本摘要；这些计数不代表独立业务样本规模。

实际 Chrome 验收通过：1440px 桌面和 390px 移动端无横向溢出；两个来源展开及跳转、基线与候选双列、RepairJob、配置/数据集绑定、检索复用跳转、历史 LOCKED_EVAL 的 5/6、5/6、6/6 未改、搜索刷新及 HTML 注入文本化，无浏览器异常。Codex 已查看桌面与移动端最终截图。

创建演示前的 **30 个历史来源** hash/mtime 未变；看板读取与浏览器交互前后的 **35 个当前来源** hash/mtime 全部未变。`git diff --check` 通过。

新增本地验收凭据位于 `scratch/dashboard-acceptance/`：`d4_test_receipt.json`、`d4_browser_receipt.json`、`d4_source_integrity_receipt.json`、`d4_snapshot.json`、`d4-evolution.png` 与 `d4-mobile.png`。

### 证据边界与下一阶段

D4 验证的是实际 Pi / Runtime / Broker / Collector / RepairJob / Store / 晋升路径和看板关联机制。初始正式版本是隔离夹具，响应与修补文本是固定脚本；评测脚本不会因 Skill 正文改变而自动形成行为改善，因此不据此报告模型收益或泛化能力。正常与失败案例均属开发回归，未将其冒充独立 held-out 实验。临时库显式演示批准不等于用户正式技能发布；未执行新真实模型调用或本轮 OS 隔离测试。

## D5a Pi SDK 文本适配器实测记录

日期：2026-11-23。状态：**已完成端到端定点测试与闭环验证；未进行真实外部模型调用**。

### 范围与实现

为连接 Pi 官方 SDK 到既有 `llm.invoke` 接口（`SimpleNamespace(content, usage)`）提供纯文本薄适配，不执行真实付费模型实验：

- `src/skillforge/pi_text_client.py`：
  - 同步接口：对外暴露 `invoke(messages: str | list[dict], **kwargs) -> SimpleNamespace(content, usage)`。内部在无运行事件循环的同步边界由 `asyncio.run` 启动独立的 `PiRunnerClient` 官方 SDK 子进程（`tools=[]`，`is_test=False`），`finally` 严格清理回收子进程。若检测到活跃事件循环直接抛出 `ValueError` fail-closed，不堆叠线程桥接。
  - 角色保真与严格校验：仅允许 `system`、`user`、`assistant` 文本消息；多段 `system` 拆入 `init_run(system_prompt=...)`，历史对话保真注入 `initial_messages`，末尾 `user` 消息送入 `prompt()`。对非文本、工具调用字段（`tool_calls`/`tool_call_id`）及缺失用户消息严格拒绝。
  - 公开配置白名单与凭据隔离：仅接受公开字段（`provider`、`model_id`、`max_tokens <= 4096`、`temperature`、`max_retries == 0`、`timeout_ms <= 120000`、`reasoning='off'`）。严格禁止 `faux`、`responses`、`is_test`，严禁在 Python 层透传 `apiKey`、`baseUrl`、`headers` 等 secret 配置（Node.js 端通过 `PI_AGENT_DIR` 本地读取）。
  - 顶层预算与账本防双算：复用 `LLMLedger`。在 provider 真实 turn 实际入场前由 `turn_handler` 执行 `reserve_turn(role, call_id)`；在官方 `turn_receipt` 帧到达时由 `receipt_handler` 执行 `settle_turn` 结算实际用量，不手动调用 `record_call` 避免双算。
  - 未知用量与单次失败 Fail-Closed：SDK 零值占位（`input: 0, output: 0, totalTokens: 0`）与缺失用量不伪装为真实 0 用量，映射为 `None` 并结算至 `usage_unknown`，触发 `TOKEN_USAGE_UNKNOWN` 阻断后续 token 上限请求；单次请求异常时通过 `cancel_pending` 结算为 unknown。
  - 可选审计工件脱敏落盘：`artifact_dir` 仅按原子方式保存 `call_<uuid>.json`，导出脱敏文本、公开模型配置、标准化用量、延迟、角色与账本摘要，严格排除 `thinking`、`reasoning_content` 与原始 secrets。
- `tests/test_pi_text_client.py`：
  - 基于本地 `127.0.0.1` 极轻量 SSE mock 服务，不联网、不调用外部 API、不访问真实凭据。
  - 覆盖：1. 文本返回、真实 usage/cache 与 system 角色保留；2. 两 client 共享账本超限第 2 次未发 HTTP；3. 未知用量 fail-closed 阻断后续请求；4. 禁止配置（faux/apiKey/baseUrl/responses/is_test/超限参数）拒绝；5. 500 异常与 length 截断标记；6. 工件原子落盘、敏感信息脱敏与无 thinking 验证；7. 消息校验与非法工具消息拦截；8. 活跃事件循环同步边界守卫。

### 验证命令与结果

```bash
env -u PI_SOURCE_ROOT .venv/bin/pytest tests/test_pi_text_client.py -q
```

**8 passed in 6.54s，exit 0**。未运行全仓无关测试，未调用外部网络。

### 边界说明

本轮仅完成 Pi SDK 文本调用的接口适配与账本门禁，未执行任何外部实际模型生成、修补或重分区实验（D5b）。

## 使用

```bash
.venv/bin/python -m skillforge.dashboard --root . --port 8765
# 或
skillforge --root . dashboard --port 8765
```

手动刷新读取新数据，无后台轮询。服务仅面向本机；没有新增执行、审批或发布按钮。


## D5 最终复核：真实 V1 与 Oracle 误报（2026-11-23）

### 实际执行与证据

使用本地 Pi 已配置的 `cc-switch-deep-seek / deepseek-v4-flash`，真实生成 V1 Markdown，并通过当前 AgentRuntime / ToolBroker / Collector 消费冻结正文、调用合成物流工具和产生 Episode。没有使用固定响应冒充真实模型。实验目录为 `scratch/pi_d5_live_2d7ef8ffc6/`，主记录为 `report.json`，共享实际请求账本为 `ledger.json`。

修订前冻结的 `protocol.json` SHA256 为 `226dbf4d1dca2d79abd247886ceaed356773f20afb5eb05d59db8ff57977752a`。DEV 与 LOCKED_EVAL 各三个合成业务家族、来源/派生族不交叉，输入与独立判据先锁定；规模为 pilot，且来自已有 synthetic 家族，不据此声称新领域泛化。上限 200 次请求、400000 tokens、3600 秒、修补两次；本轮实际 **38 次 provider 请求、46159 tokens**，其中生成 1 次 / 3116 tokens、执行 36 次 / 42552 tokens、诊断 1 次 / 491 tokens。该次“repair”角色请求实际只执行了归因，未生成补丁。费用无账单为 null；无待结算请求、无基础设施失败。

| 分区 | 原 A 无 Skill | 原 B 真实 V1 | C/V2 |
| --- | --- | --- | --- |
| DEV | 3/3 | 2/3 | 未生成，3 项未知 |
| LOCKED_EVAL | 3/3 | 3/3 | 未生成，3 项未知 |

V1 候选 `cand_4c5312e50663`、正文 hash `6d3c8fb19f1b7093f8ea9273dbb83d4a75f3274921aec3f7beff4764af8a756a`。比较基线库只是隔离实验载体，不代表经权威验证和用户确认的正式发布。

### 为什么没有 V2

DEV B 唯一失败的实际回答列全三个包裹，均为 DELIVERED，并明确写出“3 个包裹全部为已签收状态，无异常标记”。旧 Oracle 未识别“全部为已签收”，又将“无异常”中的“异常”当成异常声明，误判为在途。此失败不构成 Skill 缺陷。

随后实际诊断模型把工具事实描述写成不存在的 evidence_refs，规范证据门禁拒绝，RepairJob `job_680cd384355e` 为 DECLINED，attempts 为空、没有合法补丁或 PASS。该记录证明无效证据被拦截，不能用来证明成功修补。

### 定点修复与历史保留

- 两个物流判据共用字面声明与否定处理：补识别“全部为已签收”，排除“无异常”等否定；对部分签收却宣称全部签收的回答，即使夹有在途文字也拒绝；保留“不满足‘全部签收’条件”及多次出现相反声明的反例。
- 修复实验 JSON 工具记录导出读取错误字段的问题：使用 Broker 的 `input_params`。原报告的空参数不回填；离线复核改从原始 `runtime.db` 只读读取完整参数。
- Pi 适配器和场景账本统一计入缓存输入用量；缺用量不写假零。截断/空文本不进入生成器，异常仍落盘；真实执行逐组阶段保存，预算全局共享。
- 本地自定义 Pi 模型历史消息恢复仍受既有冻结配置绑定约束，明确 fail-closed；本次单轮文本生成不受影响。没有为通过测试绕过恢复绑定。
- 看板仅把同目录 `acceptance_review.json` 关联到准确的 `report.json`，显示复核告知，保留原分数和 C 未知，不更改 Store/Episode 的历史结论。

原判据源码封存 `oracle_original.py`，原 hash `2cb09383b5f13ffd170901ffb0aa2378e95d94ecbb9937631c2a07b3d2af9d64`；原报告 hash `60d4b62cdf0fc0f4f9a51426ef30196019892fcadaa79fa9622a25f27a829162`。原 protocol/report 字节未改，旧冻结协议不会自动接受新判据 hash。

离线脚本 `replay_oracle.py` 对原回答与数据库工具轨迹重新判定，结果另存 `oracle_replay.json`：DEV 与 LOCKED_EVAL 的 A/B 均为 3/3，唯一改判为 `run_dev_b_D5_DEV_NORMAL_005621d6`。**这是事后判据诊断，零新模型调用，不是新的前瞻性成绩或 V2 收益。**

### 结论与剩余工作

真实 V1 生成和执行证据成立；本轮不能证明 Skill 改善了成功率，也未验证真实 V2 修补。没有可靠业务失败时不制造错误以产出 V2。后续真实修补应来自新的、经独立业务核实的开发失败，再预先冻结新评测协议；本轮已看过的锁定结果不能用于调候选刷分。

本轮未验证真实电商平台、生产行为、OS 级隔离或通用自定义模型多轮恢复；未 commit/push。测试与浏览器凭据补记于下。

### 最终验证凭据

```bash
env -u PI_SOURCE_ROOT .venv/bin/pytest \
  tests/test_logistics_oracle_claim_polarity.py \
  tests/test_pi_text_client.py tests/test_pi_live_evolution.py \
  tests/test_dashboard_data.py tests/test_dashboard_server.py \
  tests/test_dashboard_evolution_demo.py tests/test_pi_evaluation.py \
  tests/test_pi_evolution.py tests/test_p6_business_experiment_and_handoff.py -q
```

**83 passed in 65.25s，exit 0**。包含 11 个物流声明/否定反例、Pi 文本与预算、阶段产物保留、导出工具参数、看板同范围复核且不改原成绩，以及受影响 Pi/P6 旧专项。此前适配器 8 项通过只是 AGY 初稿记录；最终以本次联合结果为准。没有运行全仓无关测试或新增真实模型请求。

Chrome 实际验收：1440px / 390px 下 D5 实验可选择，Oracle 复核告知可见，原 B 2/3 和 C 3 项未知保留，无横向溢出或浏览器异常；Codex 已查看最终两张截图。凭据为 `scratch/dashboard-acceptance/d5_browser_receipt.json`、`d5-desktop.png`、`d5-mobile.png` 与 `d5_snapshot.json`。原 report/protocol 的 hash 复查未变，`git diff --check` 通过。看板本地入口 `http://127.0.0.1:8765`，只读手动刷新，不自动执行实验。

## D6 演示与分享收口（2026-11-23，已完成）

目标：让已有执行与演进证据在无需启动 Python 服务时也能阅读，降低面试演示与项目分享的体验门槛。保持 D5 真实 V2 尚未验证的结论，不通过新模型实验或修改历史成绩补造闭环。

- D6a：现有看板复用同一页面与只读数据投影，增加项目内单 HTML 导出及模块/顶层 CLI 参数；仅三个核心文件与一个 HTTP/CLI 定点测试文件。
- D6b：Codex 独立核对导出安全、源文件只读性，并以实际 Chrome 离线打开检查三页、来源关联、分区、复核说明与未知项。
- D6c：更新导出命令、三分钟演示顺序和当前验收边界；本轮不增加演进模块、不调用真实模型、不发布或推送。

本阶段以实际导出文件、测试退出码和离线浏览器结果验收完成；D5 的真实 V2 证据缺口保持原状态。


### D6a 实现与独立复核

AGY 在 `src/skillforge/dashboard.py`、`src/skillforge/dashboard.html`、`src/skillforge/cli.py` 和唯一靶标 `tests/test_dashboard_server.py` 内实现单文件导出，报告定点 29 passed / exit 0。模块入口和顶层 CLI 支持 `--export-html`，复用原有只读脱敏快照与页面；通过内嵌 application/json 和字面转义阻断 script breakout，离线元 CSP 禁止网络连接。快照时间、只读标识、禁用刷新与分享前审阅提示共同展示；在线 API 和手动刷新路径保留。

输出限定项目内 `.html`，不覆盖已有目标；同目录临时文件完成后用 `os.link` 独占发布，失败清理临时文件。文件系统不支持硬链接时失败退出，不降低为可能覆盖的非原子写法。HTML 不纳入证据源扫描。

Codex 独立复现了 AGY 原测试未覆盖的祖先符号链接问题：`alias/nested/out.html` 的 nested 目录已存在时，早期父目录检查会跳过 alias。已改为在解析前遍历词法路径的每一层，并新增反例；不修改数据读取层、业务判据或任何模型实验产物。

### D6b 测试与浏览器证据

```bash
env -u PI_SOURCE_ROOT .venv/bin/pytest \
  tests/test_dashboard_server.py tests/test_dashboard_data.py \
  tests/test_dashboard_evolution_demo.py tests/test_pi_cli.py -q
```

最终 **47 passed in 41.91s，exit 0**。包含导出不启动 HTTP、输入数据库 hash/mtime 不变、快照一致性、分数与未知保留、注入转义、已知敏感字段脱敏、路径冲突/符号链接/越界、失败清理，以及旧在线接口、演进关联和 CLI 回归。此次联合结果单独计数，不与 D5 的 83 项相加。

实际交付：`scratch/dashboard-acceptance/skillforge-evidence-final.html`，429232 bytes，SHA256 `35f5a5c562ec5e78ed0b3aa2bfa283da187885d5b50e5b9c78796538b2415007`。

Chrome 在断网模式下通过 file:// 直接打开：三页切换、运行搜索、两条 Episode 来源跳转、基线/候选双列、RepairJob、配置绑定、1.0.1 检索复用跳转均通过。D5 原 DEV B 2/3、C 未知和 Oracle 复核说明仍存在。1440px 与 390px 无横向溢出、无浏览器异常；Codex 已查看最终截图。页面请求日志只有当前 HTML 文件，没有 HTTP/HTTPS 请求。

另一个实际浏览器反例导出包含 `</script><script>`、带外链的 img/onerror 与 Bearer 字符串：脚本未执行、未生成图片节点、原文按文本显示、已知密钥串脱敏，且没有网络请求。

开始前记录的 **40 个证据源** 在最终导出与浏览器阅读后 hash/mtime 全部未变。凭据保存在 `scratch/dashboard-acceptance/`：`d6_sources_before.json`、`d6_source_integrity_receipt.json`、`d6_browser_receipt.json`、`d6_attack_browser_receipt.json`、`d6-desktop.png` 和 `d6-mobile.png`。`git diff --check` 通过。

### D6c 使用与边界

README 和 `docs/DASHBOARD.md` 已补导出命令与三分钟讲解顺序：运行证据 → 技能演进 → 评测对照。文件是固定时刻的快照，需要 JavaScript；它不查询运行状态，不自动同步，不包含审批或执行入口。真实模型记录与 scripted 机制演示分开展示，D5 未生成合法 V2 的事实保持。

本轮没有新模型实验，没有正式库发布、commit/push 或公网部署。字段脱敏覆盖已知模式，分享前仍需审阅业务内容。后续真实修补应以独立确认的业务失败和新冻结协议开展，不通过修改历史分数补齐演示。

## D7 Skill 跨任务复用对照（2026-11-25）

D6 看板数据测试复跑 **13 passed / exit 0**，已有离线 HTML 的 SHA-256 与 D6 记录相符。D7 复用现有运行、Episode 和分区视图，评测页按实验提供的分组展示四组对照。

### D7a 冻结试验输入

| 组 | 可用的历史经验 |
| --- | --- |
| A | 无 |
| B | 按词法相关性检索预算内的完整 Episode，保留任务输入、工具轨迹、模型输出和判定 |
| C | 现有隔离演示中的 `logistics_query@1.0.1` 冻结正文 |
| D | C 的同一正文，以及通过 Broker 按需回查 Skill 引用的 Episode |

历史库复用 D4 的两条修补来源及一条正常对照，按 Episode ID 用只读方式核对 SQLite 中的任务、用途及结果，并核对已晋升正文与发布报告一致。D4 来源为 scripted 模型和合成物流数据，D7 在相同素材下测量使用方式的差异。三条历史记录按同一个全单核查家族计。

| 新业务家族 | 用例 | 业务判定 |
| --- | --- | --- |
| 商家页滞后 | STALE | 上游标记完成时仍核对全部包裹的当次状态 |
| 作废面单替换 | REISSUE | 只核实当前生效的面单 |
| 跨承运商交接 | HANDOFF | 交接批次结案和实际签收分别核实 |
| 受限查询范围 | ONE、CARRIER | 仅查询单件或指定承运商，整单签收保持未知 |
| 单包裹接口不可用 | OUTAGE | 失败的包裹保持未知，其他包裹按当次证据报告 |

试验包含 6 道题、5 个题目家族。家族与历史全单核查经过人工核对，允许业务步骤相似，防止把单纯换编号或改写旧任务算成新家族。协议校验拦截显式来源和派生冲突、精确文本重复及快照缺项。D7 Oracle 独立核对当次订单和包裹工具调用，再核对结构化结论，旧 Episode 返回不计作新订单证据。

冻结目录 `scratch/pi_d7_pilot_20261125/`，协议 SHA-256 `780b74ea6b971cc9e239010da1a51b22bb6df8e589587954c31794b99c5777ad`。共同预算为 168 次模型请求、220,000 tokens、3600 秒，每组每题 18 步、120 秒，历史素材限制 20,000 UTF-8 bytes。源码为 `d7_protocol.py`、`d7_pilot.py`、`d7_oracle.py`，协议和判据定点验证如下。

```bash
.venv/bin/pytest tests/test_d7_protocol.py tests/test_d7_oracle.py -q
```

**33 passed / exit 0**。包含协议被改动、来源污染、并发冻结保留旧记录，以及范围越界、旧经历冒充当次证据和基础设施未知结果的反例。

### D7b 执行与账本

`d7_reuse_experiment.py` 轮转每道题的起始组，复用 Pi Runtime、Broker、Collector 和持久化模型账本。四组的业务工具和模型预算相同。D 增加专用只读工具，模型可见 Skill 引用的 Episode ID，并可在额度内回查。B 的三条原始轨迹都能完整放入预算，在真实试跑前固定这一检索口径。输出对每次执行单独保存判据、证据、用量和延迟，所有新 Episode 的用途都是 `evaluation`。

```bash
.venv/bin/pytest tests/test_d7_reuse_experiment.py -q
env -u PI_SOURCE_ROOT .venv/bin/python -m skillforge.scenarios.d7_reuse_experiment scratch/pi_d7_pilot_20261125
```

定点 **5 passed / exit 0**。测试模拟模型响应，实际经过 Runtime、Broker 和 Collector。真实运行在新目录留下只读可查看的协议、逐任务结果、数据库和账本。目录一经开始执行即锁定，以免同一协议意外重跑。

### D7c 真实试跑结果与复核

以本地 Pi 的 `cc-switch-deep-seek / deepseek-v4-flash` 完成 6 题 × 4 组。真实请求 **68 次 / 120,198 tokens**，0 次模型请求失败，进程 exit 0。SQLite 实查 24 条新增 Episode 均为 `evaluation`，C/D 运行正文 hash 一致。合成业务夹具中共 5 个新任务家族，费用字段为 `null`。

| 组 | 原始通过 | 迁移题 | 边界题 | Token 合计 | 工具调用 |
| --- | --- | --- | --- | ---: | ---: |
| A 无历史 | 4/6 | 3/3 | 1/3 | 16,329 | 25 |
| B 原始 Episode | 4/6 | 3/3 | 1/3 | 66,426 | 25 |
| C 固定 Skill | 4/6 | 3/3 | 1/3 | 17,631 | 25 |
| D Skill＋来源回查 | 4/6 | 3/3 | 1/3 | 19,812 | 25 |

B→C、C→D 均为 0 项改善、0 项退化、6 项不变、0 项未知。C 的总 Token 比 B 少 **73.5%**，A 的总 Token 最低。D 六次运行实际回查 0 次。试点只支持在这些任务上比较两种经验表示的开销，回查有效性和准确率收益仍缺实验依据。

- `ONE`：四组都直接查询了用户明确点名的包裹，返回正确状态。原判据额外要求查整单。离线复核按单件口径四组均通过。当前协议仅允许显式点名的单件任务跳过订单查询，仍要求当次跟踪证据。
- `CARRIER`：四组最终都返回正确的承运商子集，同时都查询了范围外的包裹。复核确认 Pi 的 OpenAI-compatible 传输只发送工具返回的文本内容。原映射在 `details`，模型收到的文本只有包裹 ID。当前 D7 订单查询的文本返回包含结构化承运商映射，回归测试验证它可通过 Broker 的文本通道传出。

原 `protocol.json`、`report.json` 和 `ledger.json` 保留。试跑使用的判据、运行器和物流源码按原 SHA-256 复制到 `frozen_sources/`。`acceptance_review.json` 单独关联原报告并保存离线复核，零新模型调用。修正后的试验条件需要在新目录重新冻结。历史成绩代表当时的输入、判据和执行记录。

### D7 看板验收

已有评测页兼容历史三组报告，D7 报告显示四组、B→C 与 C→D 配对变化、逐题来源回查和同目录运行证据跳转。原成绩与离线复核同时显示。

```bash
.venv/bin/pytest tests/test_d7_protocol.py tests/test_d7_oracle.py \
  tests/test_d7_reuse_experiment.py tests/test_dashboard_data.py tests/test_dashboard_server.py -q
```

**83 passed / exit 0**。最终离线文件为 `scratch/dashboard-acceptance/skillforge-evidence-d7-reviewed.html`。用断网 Chrome 验证真实报告的两类分区、四组结果、运行跳转、旧报告三组及桌面和窄屏显示，未观察到网络请求或页面错误。浏览器记录在 `scratch/dashboard-acceptance/d7_browser_receipt.json`。D6 记录的 40 个源文件仍与验收时的哈希及修改时间一致。

### D7 来源回查对照（2026-11-25）

6 道新合成订单题，每题 C/D 各运行一次并交替先后顺序。两组使用同一 Pi 模型 `cc-switch-deep-seek / deepseek-v4-flash`、冻结 Skill 正文、任务、业务工具和预算。D 多一个 Broker 只读来源回查工具。4 道题的当前订单只有协议版本和包裹标签，店铺付费标签只能从匹配版本的历史对账协议确认。另 2 道题由当前订单直接提供已批准范围。历史来源为人工编写的合成 Episode 夹具。

运行前冻结输入、预算、判据和执行／分析源码哈希。独立 Oracle 检查当次订单查询、是否只查询预先固定范围内的包裹、逐件状态是否准确，以及整单签收是否填 `null`。另行记录安全弃权、其他失败和是否在查询包裹前取得匹配版本的已批准历史证据。协议 SHA-256 为 `9576e7a3cd864219d0746270054791519d897139e197a072773cb3e87022c026`。

| 组 | 需要历史 | 当前范围已知 | 范围违规任务 | 回查次数 | 模型请求 | 总 Token | 平均每题耗时 |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| C 仅 Skill | 0/4 | 2/2 | 4 | 0 | 18 | 21,534 | 3.15s |
| D Skill＋回查 | 4/4 | 2/2 | 0 | 5 | 22 | 30,511 | 3.67s |

4 项改善、0 项退化、2 项不变、0 项未知。C 在需要历史的 4 题中均发生范围违规，共查询 7 次范围外包裹，安全弃权 0 次。D 在 4 题中均先取匹配证据再查包裹。`D7L-831` 返回旧版与当前版两条来源，D 匹配当前版本后只查第 2 件。两道当前范围已知的题均未回查。全组业务及回查工具调用 C 20 次、D 19 次。合计增加 8,977 Token（41.7%）、4 次模型请求，平均每题增加 0.52s。需要历史的 4 题增加 8,203 Token。无需回查的 2 题增加 774 Token，这包含提供回查工具的上下文开销及模型输出差异。

本轮合计 40 次真实模型请求、52,045 Token、0 次模型请求失败，费用 `null`。SQLite 实核 12 条归档 Episode 全部标为 `evaluation`，运行正文哈希一致。Oracle 能在轨迹中检测范围违规。合成只读工具在本次测试中容许错误选择发生，因此成绩反映模型选参行为，不能据此推出 Broker 有逐包裹的授权强制能力。结果适用于该小样本和模型配置。原 D7 四组试跑报告及账本哈希复核一致。

```bash
.venv/bin/python -m skillforge.scenarios.d7_lookup_pilot freeze scratch/pi_d7_lookup_20261125
env -u PI_SOURCE_ROOT .venv/bin/python -m skillforge.scenarios.d7_lookup_pilot run scratch/pi_d7_lookup_20261125
.venv/bin/pytest tests/test_d7_lookup_pilot.py tests/test_d7_protocol.py tests/test_d7_oracle.py tests/test_d7_reuse_experiment.py -q
.venv/bin/pytest tests/test_dashboard_data.py tests/test_dashboard_server.py -q
```

实跑 exit 0。专项 40 项、看板 44 项通过。目录内 `protocol.json` 为冻结内容，`report.json` 为逐题及工具调用，`ledger.json` 为模型用量，`lookup_review.json` 记录匹配来源及弃权分类。复现时需使用新目录，已有报告禁止覆盖。

[逐题公开记录](fixtures/d7_source_lookup_results.json) 保留合成协议、预期范围、两组实际查询与回查、逐题判定和模型用量。协议及原报告哈希用于和完整本地记录核对。
