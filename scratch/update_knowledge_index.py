import re

html_path = 'docs/skillforge-knowledge-index.html'
with open(html_path, 'r', encoding='utf-8') as f:
    content = f.read()

# 1. Add CSS
css_to_add = """
    /* Resume & Question Bank Matrix Styles */
    .resume-matrix-card {
      background: var(--bg-card);
      border: 1px solid var(--border-accent);
      border-radius: 12px;
      padding: 24px;
      margin-top: 24px;
      margin-bottom: 32px;
      box-shadow: 0 4px 20px rgba(0, 0, 0, 0.25);
    }
    .filter-tabs-row {
      display: flex;
      gap: 8px;
      flex-wrap: wrap;
      margin: 16px 0 20px 0;
    }
    .filter-tab-btn {
      padding: 6px 14px;
      border-radius: 999px;
      border: 1px solid var(--border-color);
      background: var(--bg-tertiary);
      color: var(--text-secondary);
      font-size: 12px;
      font-weight: 600;
      cursor: pointer;
      transition: all 0.2s ease;
    }
    .filter-tab-btn:hover {
      color: var(--text-primary);
      border-color: var(--border-accent);
    }
    .filter-tab-btn.active {
      background: rgba(56, 189, 248, 0.2);
      color: var(--accent-cyan);
      border-color: var(--accent-cyan);
    }
    .bullet-block {
      background: rgba(15, 23, 42, 0.4);
      border: 1px solid var(--border-color);
      border-radius: 8px;
      padding: 18px;
      margin-bottom: 24px;
      transition: all 0.3s ease;
    }
    .bullet-header {
      display: flex;
      align-items: center;
      gap: 10px;
      margin-bottom: 12px;
    }
    .bullet-badge {
      padding: 3px 8px;
      border-radius: 4px;
      font-size: 11px;
      font-weight: 700;
      font-family: var(--font-mono);
      white-space: nowrap;
    }
    .badge-core { background: rgba(245, 158, 11, 0.2); color: #fbbf24; border: 1px solid rgba(245, 158, 11, 0.4); }
    .badge-harness { background: rgba(56, 189, 248, 0.2); color: #38bdf8; border: 1px solid rgba(56, 189, 248, 0.4); }
    .bullet-title {
      font-size: 15px;
      font-weight: 700;
      color: var(--text-primary);
    }
    .bullet-quote {
      font-size: 13px;
      color: var(--text-secondary);
      line-height: 1.55;
      background: rgba(255,255,255,0.03);
      padding: 8px 12px;
      border-radius: 6px;
      border-left: 3px solid var(--accent-cyan);
      margin-bottom: 10px;
    }
    .hook-tip {
      font-size: 12px;
      color: var(--accent-amber);
      background: rgba(245, 158, 11, 0.08);
      border: 1px solid rgba(245, 158, 11, 0.2);
      padding: 6px 12px;
      border-radius: 6px;
      margin-bottom: 16px;
      display: flex;
      align-items: center;
      gap: 6px;
    }
    .q-grid {
      display: grid;
      grid-template-columns: repeat(auto-fill, minmax(360px, 1fr));
      gap: 14px;
    }
    .q-card {
      background: var(--bg-secondary);
      border: 1px solid var(--border-color);
      border-radius: 8px;
      padding: 14px;
      display: flex;
      flex-direction: column;
      justify-content: space-between;
      transition: all 0.2s ease;
    }
    .q-card:hover {
      border-color: var(--border-accent);
      transform: translateY(-2px);
      box-shadow: 0 4px 12px rgba(0, 0, 0, 0.15);
    }
    .q-card-head {
      display: flex;
      justify-content: space-between;
      align-items: center;
      margin-bottom: 8px;
    }
    .q-id {
      font-family: var(--font-mono);
      font-weight: 700;
      font-size: 12px;
      color: var(--accent-cyan);
      background: rgba(56, 189, 248, 0.12);
      padding: 2px 6px;
      border-radius: 4px;
    }
    .q-depth {
      font-size: 11px;
      color: var(--text-muted);
      font-family: var(--font-mono);
    }
    .q-title {
      font-size: 13px;
      font-weight: 600;
      color: var(--text-primary);
      margin-bottom: 6px;
      line-height: 1.45;
    }
    .q-trigger {
      font-size: 11.5px;
      color: var(--accent-amber);
      margin-bottom: 8px;
      line-height: 1.4;
    }
    .q-answer-snip {
      font-size: 12px;
      color: var(--text-secondary);
      line-height: 1.5;
      margin-bottom: 10px;
      background: rgba(0,0,0,0.18);
      padding: 8px 10px;
      border-radius: 4px;
      border-left: 2px solid var(--accent-blue);
    }
    .q-footer {
      display: flex;
      justify-content: space-between;
      align-items: center;
      border-top: 1px dashed var(--border-color);
      padding-top: 8px;
      font-size: 11px;
    }
    .q-code-link {
      font-family: var(--font-mono);
      color: var(--accent-cyan);
      text-decoration: none;
    }
    .q-code-link:hover {
      text-decoration: underline;
    }
"""

if '/* Resume & Question Bank Matrix Styles */' not in content:
    content = content.replace('/* Footer */', css_to_add + '\n    /* Footer */')

# 2. Update Top Nav Links
old_nav = '<a href="skillforge-skill-lifecycle.html" class="nav-item">🧬 候选隔离与版本生命周期</a>'
new_nav = '<a href="skillforge-skill-lifecycle.html" class="nav-item">🧬 候选隔离与版本生命周期</a>\n      <a href="#resume-qa-matrix" class="nav-item" style="color: #fbbf24; border-color: rgba(245, 158, 11, 0.4); background: rgba(245, 158, 11, 0.1);">📋 简历与21题库</a>'
if '📋 简历与21题库' not in content:
    content = content.replace(old_nav, new_nav)

# 3. Update Sidebar TOC
old_toc = '<li class="toc-item"><a href="#overview">🌟 全景概述与两层闭环</a></li>'
new_toc = '<li class="toc-item"><a href="#overview">🌟 全景概述与两层闭环</a></li>\n        <li class="toc-item"><a href="#resume-qa-matrix" style="color: #fbbf24; font-weight: 600;">📋 简历与21题库穿透索引</a></li>'
if 'href="#resume-qa-matrix"' not in content:
    content = content.replace(old_toc, new_toc)

# 4. Add the resume-qa-matrix section HTML
section_html = """
      <!-- Section: Resume & Routed Question Bank Matrix -->
      <section class="resume-matrix-card" id="resume-qa-matrix">
        <div style="display: flex; justify-content: space-between; align-items: flex-start; flex-wrap: wrap; gap: 12px; margin-bottom: 12px;">
          <div>
            <div class="section-badge-group" style="margin-bottom: 8px;">
              <span class="section-badge" style="background: rgba(245, 158, 11, 0.2); color: #fbbf24; border: 1px solid rgba(245, 158, 11, 0.4);">INTERVIEW MATRIX</span>
              <span class="section-type">简历 4 Bullet & 口头设伏 ↔ 21 道大厂真题穿透</span>
            </div>
            <h2 class="section-title" style="margin-bottom: 4px;">📋 简历精简描述 & 口头设伏与 21 道大厂题库穿透索引</h2>
          </div>
          <a href="../面试阐述/05_简历逐字拆解与口头引导题库串联备战表.md" target="_blank" style="padding: 6px 14px; border-radius: 6px; background: rgba(56, 189, 248, 0.15); color: #38bdf8; text-decoration: none; border: 1px solid rgba(56, 189, 248, 0.35); font-size: 12px; font-weight: 600; display: inline-flex; align-items: center; gap: 6px; transition: all 0.2s;">
            <span>📖 打开 05_备战表源文档 →</span>
          </a>
        </div>
        <p style="font-size: 13.5px; color: var(--text-secondary); margin-bottom: 16px;">
          直接对接最新 4 条精简简历 Bullet 与口头介绍剧本（<em>“哪些轨迹值得留下，又凭什么从‘经历’升级成‘技能’”</em>），将刚刚路由出的 21 道大厂高频面试真题与源码落地无缝打通。点击下方标签可快速筛选切换：
        </p>

        <!-- Filter Tabs -->
        <div class="filter-tabs-row">
          <button class="filter-tab-btn active" onclick="filterBullet('all', this)">全部 21 题全景</button>
          <button class="filter-tab-btn" onclick="filterBullet('b1', this)">🌟 口头核心 & Bullet 1: 跨任务演进与记忆 (8题)</button>
          <button class="filter-tab-btn" onclick="filterBullet('b2', this)">🛡️ Bullet 2: 任务内自修复与交付重验 (3题)</button>
          <button class="filter-tab-btn" onclick="filterBullet('b3', this)">🛡️ Bullet 3: 受控执行 Harness 与沙箱 (5题)</button>
          <button class="filter-tab-btn" onclick="filterBullet('b4', this)">🧭 Bullet 4: 检索复用与版本治理 (5题)</button>
        </div>

        <!-- Bullet 1 Block -->
        <div class="bullet-block" id="bullet-block-b1" data-bullet="b1">
          <div class="bullet-header">
            <span class="bullet-badge badge-core">业务核心</span>
            <span class="bullet-title">Bullet 1 · 跨任务技能演进与经验沉淀（共 8 题）</span>
          </div>
          <div class="bullet-quote">
            <strong>简历原文：</strong>将工具调用、执行结果、失败恢复与验证证据沉淀为带来源和版本的 Episode，从重复模式中提炼 Skill Candidate；区分 Semantic 事实、Episodic 经历与 Procedural 技能，根据重复性、稳定性和复杂度筛选可复用模式。隔离学习数据与独立评测数据，通过失败归因、定向修补、回归门禁和显式晋升控制长期 Skill 更新。
          </div>
          <div class="hook-tip">
            <span>🎯 <strong>口头设伏对齐：</strong><em>“我最想展开的是中间这一步：哪些轨迹值得留下，又凭什么从‘经历’升级成‘技能’。”</em></span>
          </div>
          <div class="q-grid">
            <div class="q-card">
              <div>
                <div class="q-card-head"><span class="q-id">L19</span><span class="q-depth">深度 L3</span></div>
                <div class="q-title">长短期记忆模块实现方案与状态持久化</div>
                <div class="q-trigger">🔍 面试切入：“区分 Semantic / Episodic / Procedural，三者底层怎么存？”</div>
                <div class="q-answer-snip">三处异构存储：SQLite 存发布状态（短期指针）、Git 存内容全历史（长期记忆）、JSONL 存审计流水，release_id 幂等串联。</div>
              </div>
              <div class="q-footer"><span class="q-code-link">src/skillforge/storage/db.py:12</span><span>持久化状态机</span></div>
            </div>
            <div class="q-card">
              <div>
                <div class="q-card-head"><span class="q-id">L213</span><span class="q-depth">深度 L4</span></div>
                <div class="q-title">存储数据库选型标准与不可变事实源</div>
                <div class="q-trigger">🔍 面试切入：“为什么不用向量数据库全量存储所有经历？”</div>
                <div class="q-answer-snip">按数据形态与一致性选型：关系型管状态事务、Git 管版本 diff、日志型管只读追加，小规模向量用内存点积，避免重型 DB 运维。</div>
              </div>
              <div class="q-footer"><span class="q-code-link">src/skillforge/storage/git_ops.py:25</span><span>不可变存证</span></div>
            </div>
            <div class="q-card">
              <div>
                <div class="q-card-head"><span class="q-id">L43</span><span class="q-depth">深度 L3</span></div>
                <div class="q-title">缺乏用户反馈下的自动化抽检与评估机制</div>
                <div class="q-trigger">🔍 面试切入：“哪些轨迹才‘值得留下’并提炼为技能？”</div>
                <div class="q-answer-snip">三道硬指标：重复性 support >= 3、意图表达 >= 2 种、步骤数 >= 2 排除单步平凡工具；对比失败反例提炼边界。</div>
              </div>
              <div class="q-footer"><span class="q-code-link">src/skillforge/pattern_mining.py:50</span><span>模式挖掘</span></div>
            </div>
            <div class="q-card">
              <div>
                <div class="q-card-head"><span class="q-id">L49</span><span class="q-depth">深度 L5</span></div>
                <div class="q-title">Prompt 瓶颈后的性能提升路径（归因与调参）</div>
                <div class="q-trigger">🔍 面试切入：“遇到报错为什么不直接调大模型做 SFT 微调？”</div>
                <div class="q-answer-snip">不盲目 SFT。先走评估驱动调参（硬负例阈值 62%→98%）+ 数据飞轮沉淀 runs/failures/，构建偏好对后再考虑微调。</div>
              </div>
              <div class="q-footer"><span class="q-code-link">src/skillforge/router/cascade.py:32</span><span>评估驱动调参</span></div>
            </div>
            <div class="q-card">
              <div>
                <div class="q-card-head"><span class="q-id">L30</span><span class="q-depth">深度 L3</span></div>
                <div class="q-title">技能生成中的负向边界（`[Not For]`）提炼与改写</div>
                <div class="q-trigger">🔍 面试切入：“如何防止新提炼技能在未来任务中跨领域误触？”</div>
                <div class="q-answer-snip">失败案例对比生成结构化卡片 [Capability][Use When][Examples][Not For]，在向量空间主动推开硬负例（如周报 vs 会议纪要）。</div>
              </div>
              <div class="q-footer"><span class="q-code-link">skills/weather_query/SKILL.md:6</span><span>负向排斥</span></div>
            </div>
            <div class="q-card">
              <div>
                <div class="q-card-head"><span class="q-id">L39</span><span class="q-depth">深度 L3</span></div>
                <div class="q-title">Agent 效果评估体系（八维评估与基准集分层）</div>
                <div class="q-trigger">🔍 面试切入：“怎么证明评测集没有被‘训练集泄露’？”</div>
                <div class="q-answer-snip">八维评估（静态4维+效果4维），数据集物理分立：baseline_dev（开发集）、baseline_hidden（防过拟合）、p0_cases（核心门禁）。</div>
              </div>
              <div class="q-footer"><span class="q-code-link">src/skillforge/evaluator/__init__.py:153</span><span>基准集分层</span></div>
            </div>
            <div class="q-card">
              <div>
                <div class="q-card-head"><span class="q-id">L6</span><span class="q-depth">深度 L1</span></div>
                <div class="q-title">Agent 工作流范式对比与“单调不减”棘轮门禁</div>
                <div class="q-trigger">🔍 面试切入：“ReAct 与 Plan-and-Execute 怎么结合？凭什么升级成技能？”</div>
                <div class="q-answer-snip">执行层用 ReAct 显式 use_skill，演进层用 Plan-and-Execute 六步 pipeline；棘轮 5 重硬门禁 + P0 一票否决保证单调不减。</div>
              </div>
              <div class="q-footer"><span class="q-code-link">src/skillforge/evaluator/ratchet.py:60</span><span>棘轮硬门禁</span></div>
            </div>
            <div class="q-card">
              <div>
                <div class="q-card-head"><span class="q-id">L93</span><span class="q-depth">深度 L3</span></div>
                <div class="q-title">多步任务拆解、步骤规划与元 Agent 沙箱验证</div>
                <div class="q-trigger">🔍 面试切入：“候选技能如何进行低成本有效规划与打擂台？”</div>
                <div class="q-answer-snip">元 Agent 四根因标签驱动 L1/L2/L3 分级 patch 生成，临时 candidate 目录动态挂载打擂台，等价低成本 Beam-Search。</div>
              </div>
              <div class="q-footer"><span class="q-code-link">src/skillforge/evolver.py:398</span><span>沙箱打擂台</span></div>
            </div>
          </div>
        </div>

        <!-- Bullet 2 Block -->
        <div class="bullet-block" id="bullet-block-b2" data-bullet="b2">
          <div class="bullet-header">
            <span class="bullet-badge badge-harness">Harness 核心</span>
            <span class="bullet-title">Bullet 2 · 任务内可验证自修复与交付重验（共 3 题）</span>
          </div>
          <div class="bullet-quote">
            <strong>简历原文：</strong>将验证失败转为结构化 Receipt，根据失败责任层决定是否修补，结合有限尝试预算、回归测试和棘轮门禁验证修改。绑定产物内容与验证器配置，驱动有界 JSON 局部修复；在 finalize 阶段基于当前产物重新验证，防止旧 PASS、内容变化及配置漂移导致错误交付。
          </div>
          <div class="hook-tip">
            <span>🎯 <strong>口头设伏对齐：</strong><em>“对于结构化产物，还提供 Receipt 驱动的有限修复和交付重验。”</em></span>
          </div>
          <div class="q-grid">
            <div class="q-card">
              <div>
                <div class="q-card-head"><span class="q-id">L34</span><span class="q-depth">深度 L3</span></div>
                <div class="q-title">基于 Harness 自动评估、自修复与降低人工成本</div>
                <div class="q-trigger">🔍 面试切入：“为什么把自修复上限卡死在 2 轮？为什么不让模型多反思几次？”</div>
                <div class="q-answer-snip">结构化 Receipt 精确锁定 JSONPath 局部打补丁；min(max(1, limit), 2) 硬卡 2 轮上限，指纹环路熔断，防止发散反思与修改扩散。</div>
              </div>
              <div class="q-footer"><span class="q-code-link">src/skillforge/runtime.py:1546</span><span>2轮收敛自修复</span></div>
            </div>
            <div class="q-card">
              <div>
                <div class="q-card-head"><span class="q-id">L319</span><span class="q-depth">深度 L1</span></div>
                <div class="q-title">分层责任归属（tool/policy/infra）与降级语义</div>
                <div class="q-trigger">🔍 面试切入：“如果沙箱无权写磁盘或网络断了，修复器会去打补丁吗？”</div>
                <div class="q-answer-snip">分层责任分流（tool/policy/infra）。policy 与 infra 拒绝严禁打补丁掩盖故障，直接中断报警；LLM 失败快速拒绝不阻塞。</div>
              </div>
              <div class="q-footer"><span class="q-code-link">src/skillforge/receipt.py:21</span><span>责任层归因</span></div>
            </div>
            <div class="q-card">
              <div>
                <div class="q-card-head"><span class="q-id">L5</span><span class="q-depth">深度 L4</span></div>
                <div class="q-title">交付时（Finalize）权威重验与防 TOCTOU 篡改</div>
                <div class="q-trigger">🔍 面试切入：“前面验证都已经 PASS 了，交付时为什么还要再跑一次全量校验？”</div>
                <div class="q-answer-snip">防 TOCTOU 内存二次篡改、防验证器规则动态漂移、防 Agent 谎报；强校验最终产物 SHA-256 与 validator_config_hash。</div>
              </div>
              <div class="q-footer"><span class="q-code-link">src/skillforge/runtime.py:1218</span><span>交付时权威重验</span></div>
            </div>
          </div>
        </div>

        <!-- Bullet 3 Block -->
        <div class="bullet-block" id="bullet-block-b3" data-bullet="b3">
          <div class="bullet-header">
            <span class="bullet-badge badge-harness">Harness 核心</span>
            <span class="bullet-title">Bullet 3 · 受控执行 Harness 与沙箱底座（共 5 题）</span>
          </div>
          <div class="bullet-quote">
            <strong>简历原文：</strong>以 Runtime 管理预算、超时和取消，Tool Broker 统一工具准入与参数校验，并结合进程沙箱限制文件与网络访问；依赖在真实执行环境中验证，不可用时 Fail-Closed。
          </div>
          <div class="hook-tip">
            <span>🎯 <strong>口头设伏对齐：</strong><em>“执行侧则有 Runtime、工具权限和沙箱控制。”</em></span>
          </div>
          <div class="q-grid">
            <div class="q-card">
              <div>
                <div class="q-card-head"><span class="q-id">L80</span><span class="q-depth">深度 L1</span></div>
                <div class="q-title">确定性工作流 vs 自主智能体权衡与生命周期</div>
                <div class="q-trigger">🔍 面试切入：“外部 Agent 如果陷入死循环，Runtime 怎么掌控生命周期？”</div>
                <div class="q-answer-snip">确定性归代码、不确定性交模型。start_new_session=True 独立进程组，超时 os.killpg 强杀整棵进程树，保守拒绝兜底。</div>
              </div>
              <div class="q-footer"><span class="q-code-link">src/skillforge/runtime.py:624</span><span>Runtime 生命周期</span></div>
            </div>
            <div class="q-card">
              <div>
                <div class="q-card-head"><span class="q-id">L150</span><span class="q-depth">深度 L1</span></div>
                <div class="q-title">Prompt 注入与越狱防范，macOS 内核沙箱隔离</div>
                <div class="q-trigger">🔍 面试切入：“如果 Agent 执行了 os.system('rm -rf /')，纯代码能防住吗？”</div>
                <div class="q-answer-snip">防线必须下沉内核！ToolBroker 静态白名单 + macOS Seatbelt 原生 SBPL 规则 (deny file-write*) (deny network*)，内核级强阻断。</div>
              </div>
              <div class="q-footer"><span class="q-code-link">src/skillforge/sandbox.py:181</span><span>Seatbelt 内核隔离</span></div>
            </div>
            <div class="q-card">
              <div>
                <div class="q-card-head"><span class="q-id">L9</span><span class="q-depth">深度 L4</span></div>
                <div class="q-title">多 Agent 编排模式与框架选型考量</div>
                <div class="q-trigger">🔍 面试切入：“为什么不用 LangChain / LangGraph，非要自研 Harness？”</div>
                <div class="q-answer-snip">主 Agent + 元 Agent + 评估器分层协作；单进程零外部依赖，用 SQLite 状态机提供发布事务保障与 Watchdog 孤儿清理，可测性高。</div>
              </div>
              <div class="q-footer"><span class="q-code-link">src/skillforge/state_machine.py:49</span><span>发布状态机</span></div>
            </div>
            <div class="q-card">
              <div>
                <div class="q-card-head"><span class="q-id">L74</span><span class="q-depth">深度 L1</span></div>
                <div class="q-title">统一工具接入协议（MCP）本质与依赖声明落地</div>
                <div class="q-trigger">🔍 面试切入：“在项目中如何落地工具依赖声明与按需取数？”</div>
                <div class="q-answer-snip">SKILL.md 显式声明 dependencies 并做可用性检查；元数据索引常驻，完整 Body 仅在 use_skill 时动态披露，按需节省 Token。</div>
              </div>
              <div class="q-footer"><span class="q-code-link">skills/weather_query/SKILL.md:14</span><span>依赖声明与披露</span></div>
            </div>
            <div class="q-card">
              <div>
                <div class="q-card-head"><span class="q-id">L16</span><span class="q-depth">深度 L3</span></div>
                <div class="q-title">类似 Claude Code 的 AI Coding 底层系统架构</div>
                <div class="q-trigger">🔍 面试切入：“AI Coding 类工具底层核心的执行沙箱与技能架构是怎样的？”</div>
                <div class="q-answer-snip">SkillForge 正是其底座：ReAct 显式 tool call + reason 强归因 + 三层级联路由 + 八维评估门禁 + 发布状态机，执行全链路可追溯。</div>
              </div>
              <div class="q-footer"><span class="q-code-link">src/skillforge/registry.py:123</span><span>可解释工具加载</span></div>
            </div>
          </div>
        </div>

        <!-- Bullet 4 Block -->
        <div class="bullet-block" id="bullet-block-b4" data-bullet="b4">
          <div class="bullet-header">
            <span class="bullet-badge badge-core">工程闭环</span>
            <span class="bullet-title">Bullet 4 · 检索复用与版本治理（共 5 题）</span>
          </div>
          <div class="bullet-quote">
            <strong>简历原文：</strong>按权限、依赖、验证状态和部署状态筛选正式 Skill，并接入真实任务入口；通过运行级版本固定、不可变快照、灰度路由和受控回滚保证复用与演进过程可追溯。
          </div>
          <div class="hook-tip">
            <span>🎯 <strong>口头设伏对齐：</strong><em>“后续任务可以检索并复用，记住一次经历和接受一项长期能力是两个不同的决策。”</em></span>
          </div>
          <div class="q-grid">
            <div class="q-card">
              <div>
                <div class="q-card-head"><span class="q-id">L137</span><span class="q-depth">深度 L3</span></div>
                <div class="q-title">技能动态加载与初始化流程设计，CAS 乐观锁回滚</div>
                <div class="q-trigger">🔍 面试切入：“线上发生故障回滚时，如何避免老版本依赖缺失次生灾难？”</div>
                <div class="q-answer-snip">初始化 Pydantic 强校验；动态加载按 SQLite→Git→磁盘三级降级；回滚带 CAS 乐观锁并强制 DependencyProbe 依赖预检。</div>
              </div>
              <div class="q-footer"><span class="q-code-link">src/skillforge/deployments.py:729</span><span>CAS版本控制</span></div>
            </div>
            <div class="q-card">
              <div>
                <div class="q-card-head"><span class="q-id">L57</span><span class="q-depth">深度 L4</span></div>
                <div class="q-title">知识检索中确定性规则链路比 LLM 链路的优势</div>
                <div class="q-trigger">🔍 面试切入：“在什么情况下基于规则/意图识别比大模型生成更靠谱？”</div>
                <div class="q-answer-snip">意图可枚举、成本敏感、需硬拒绝时规则完胜。规则层 0.01ms 出结果，命中即等分，带 LOW_CONF 保守拒识，省 15 倍成本。</div>
              </div>
              <div class="q-footer"><span class="q-code-link">src/skillforge/router/rule.py:20</span><span>确定性首跳</span></div>
            </div>
            <div class="q-card">
              <div>
                <div class="q-card-head"><span class="q-id">L55</span><span class="q-depth">深度 L1</span></div>
                <div class="q-title">混合检索（Hybrid Search）必要性与状态机过滤</div>
                <div class="q-trigger">🔍 面试切入：“纯向量召回有什么缺陷？状态机和权限如何介入？”</div>
                <div class="q-answer-snip">keyword 保字面、bge 向量保语义、LLM 兜歧义；召回后强制过滤 status != 'APPROVED' 或环境依赖缺失的技能，防不可用越权。</div>
              </div>
              <div class="q-footer"><span class="q-code-link">src/skillforge/retrieval.py:280</span><span>两段式硬过滤</span></div>
            </div>
            <div class="q-card">
              <div>
                <div class="q-card-head"><span class="q-id">L21</span><span class="q-depth">深度 L4</span></div>
                <div class="q-title">记忆读写：LLM 自主决策 vs 规则路由</div>
                <div class="q-trigger">🔍 面试切入：“记忆的读取与写入是交给大模型还是基于规则？”</div>
                <div class="q-answer-snip">确定性优先、LLM 兜底。规则与高置信向量（top1 >= 0.75, margin >= 0.10）直接独占，中间地带交 LLM，硬负例调优 R@1 达 98%。</div>
              </div>
              <div class="q-footer"><span class="q-code-link">src/skillforge/router/cascade.py:75</span><span>级联置信度决策</span></div>
            </div>
            <div class="q-card">
              <div>
                <div class="q-card-head"><span class="q-id">L17</span><span class="q-depth">深度 L3</span></div>
                <div class="q-title">意图识别与前置拦截（Guards）引入上下文记忆</div>
                <div class="q-trigger">🔍 面试切入：“在 Gatekeeping 环节如何控制上下文膨胀并进行拦截？”</div>
                <div class="q-answer-snip">记忆分三层：静态索引常驻（~80 token/skill）、router.jsonl 事件归因、失败样本经验；拦截层靠 not_for 负向边界强拒识。</div>
              </div>
              <div class="q-footer"><span class="q-code-link">src/skillforge/registry.py:71</span><span>渐进式披露</span></div>
            </div>
            <div class="q-card">
              <div>
                <div class="q-card-head"><span class="q-id">L27</span><span class="q-depth">深度 L3</span></div>
                <div class="q-title">多轮对话上下文省略与指代消解的工程解法</div>
                <div class="q-trigger">🔍 面试切入：“长上下文多轮对话如何避免撑爆窗口或出现指代漂移？”</div>
                <div class="q-answer-snip">以单轮 Harness 为主，通过 Skill 结构化抽取把多轮素材压成四段结构，语义消解前置在 LLM 路由层，两段式披露控制 Token。</div>
              </div>
              <div class="q-footer"><span class="q-code-link">evaluation_sets/baseline_dev.json:27</span><span>结构化抽取压缩</span></div>
            </div>
          </div>
        </div>
      </section>
"""

# Insert section_html right before <!-- Module 1: Execution & Sandbox -->
target_marker = '<!-- Module 1: Execution & Sandbox -->'
if 'id="resume-qa-matrix"' not in content:
    content = content.replace(target_marker, section_html + '\n      ' + target_marker)

# 5. Add JavaScript for interactive filtering
js_to_add = """
    function filterBullet(bulletId, btn) {
      document.querySelectorAll('.filter-tab-btn').forEach(b => b.classList.remove('active'));
      if (btn) btn.classList.add('active');
      const blocks = document.querySelectorAll('.bullet-block');
      blocks.forEach(block => {
        if (bulletId === 'all' || block.getAttribute('data-bullet') === bulletId) {
          block.style.display = 'block';
        } else {
          block.style.display = 'none';
        }
      });
    }
"""

if 'function filterBullet' not in content:
    content = content.replace('function toggleTheme() {', js_to_add + '\n    function toggleTheme() {')

with open(html_path, 'w', encoding='utf-8') as f:
    f.write(content)

print('Successfully updated docs/skillforge-knowledge-index.html!')
