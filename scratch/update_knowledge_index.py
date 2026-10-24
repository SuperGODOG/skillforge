import re

with open('docs/skillforge-knowledge-index.html', 'r', encoding='utf-8') as f:
    html = f.read()

# 1. Update Site Header: add nav button for 验收结论与事实边界
header_needle = '<a href="skillforge-knowledge-index.html" class="nav-item active">📖 知识索引主页</a>'
header_replacement = '<a href="skillforge-knowledge-index.html" class="nav-item active">📖 知识索引主页</a>\n        <a href="#project-milestone-evidence" class="nav-item" style="color: #10b981; border-color: rgba(16, 185, 129, 0.4); background: rgba(16, 185, 129, 0.1);">⚖️ 验收结论与事实边界</a>'
if header_needle in html:
    html = html.replace(header_needle, header_replacement, 1)
    print("Site header nav item added.")
else:
    print("WARNING: header_needle not found!")

# 2. Update Sidebar TOC
toc_needle = '<li class="toc-item"><a href="#overview">🌟 全景概述与两层闭环</a></li>'
toc_replacement = '''<li class="toc-item"><a href="#overview">🌟 全景概述与两层闭环</a></li>
        <li class="toc-item"><a href="#project-milestone-evidence" style="color: var(--accent-green); font-weight: 600;">⚖️ P0~P6 验收结论与事实边界</a></li>
        <li class="toc-item nested"><a href="#milestone-completed">0.1 P0~P6 阶段完成度与 61 项回归</a></li>
        <li class="toc-item nested"><a href="#milestone-adjustments">0.2 经批准的两项核心架构调整</a></li>
        <li class="toc-item nested"><a href="#milestone-evidence-bounds">0.3 真实模型运行与证据边界</a></li>
        <li class="toc-item nested"><a href="#milestone-non-goals">0.4 本期不打算做与待定工作</a></li>'''
if toc_needle in html:
    html = html.replace(toc_needle, toc_replacement, 1)
    print("Sidebar TOC items added.")
else:
    print("WARNING: toc_needle not found!")

# 3. Insert Section: Milestone & Truthful Evidence Boundaries
milestone_section_html = '''
      <!-- Section: Milestone & Truthful Evidence Boundaries -->
      <section class="module-section" id="project-milestone-evidence" style="border-left: 3px solid var(--accent-green); background: linear-gradient(180deg, rgba(16, 185, 129, 0.04) 0%, rgba(15, 23, 42, 0) 100%);">
        <div class="section-header">
          <div>
            <div class="section-badge-group">
              <span class="section-badge green">P0~P6 ACCEPTED</span>
              <span class="section-badge">61 PASS / EXIT 0</span>
              <span class="section-badge amber">EVIDENCE BOUNDARIES</span>
            </div>
            <h2 class="section-title">⚖️ P0~P6 限定工程验收结论与事实证据边界 (Milestone Verification & Factual Bounds)</h2>
          </div>
          <div style="font-size: 12px; color: var(--text-muted); font-family: var(--font-mono);">
            依据 AGY 回归测试报告与交付记录 (docs/QUICK_GENERATION_EVOLUTION_PROGRESS.md:822)
          </div>
        </div>

        <p style="font-size: 14px; color: var(--text-secondary); margin-bottom: 20px;">
          <strong>权威结论</strong>：按原计划及两项批准调整，<strong>限定工程已全面验收；真实模型实验已经运行，但效果论证仍有未验证部分和历史证据缺失</strong>。本框架严守<em>“准确而不是装懂、证据而不是猜测、清晰责任边界”</em>的工程原则，以下为代码实现、测试记录与交付证据的真实审计清单。
        </p>

        <!-- Block 1: Completed P0-P6 & 61 Tests -->
        <div class="detail-block" id="milestone-completed">
          <h3 class="detail-heading">0.1 P0~P6 限定工程已完成阶段与验收证据</h3>
          <div class="table-container">
            <table>
              <thead>
                <tr>
                  <th style="width: 140px;">阶段编号</th>
                  <th style="width: 200px;">阶段名称</th>
                  <th>核心工程落地与验收证据</th>
                  <th style="width: 120px;">验收状态</th>
                </tr>
              </thead>
              <tbody>
                <tr>
                  <td class="highlight"><strong>P0</strong></td>
                  <td><strong>入口与兼容契约</strong></td>
                  <td>入口及旁路盘点、统一业务契约、新建正文上界约束（冷启动 3000 字符限制）、字段平滑迁移；旧库升级兼容已补全回归测试。</td>
                  <td><span class="section-badge green">PASS 已验收</span></td>
                </tr>
                <tr>
                  <td class="highlight"><strong>P1</strong></td>
                  <td><strong>快速生成与试用</strong></td>
                  <td>短需求与自然会话快速生成 Draft 并保留真实 Provenance 来源；任务内快照执行、草稿与正式库物理隔离、请求去重与权限拦截；旧注册入口受共同门禁约束。</td>
                  <td><span class="section-badge green">PASS 已验收</span></td>
                </tr>
                <tr>
                  <td class="highlight"><strong>P2</strong></td>
                  <td><strong>统一准入治理</strong></td>
                  <td>学习数据与评测数据严格用途隔离；规范 Episode 防伪与防篡改签名；各修改入口实施严格膨胀检查；验证记录绑定与工具依赖预检，分阶段控制检查成本。</td>
                  <td><span class="section-badge green">PASS 已验收</span></td>
                </tr>
                <tr>
                  <td class="highlight"><strong>P3</strong></td>
                  <td><strong>用户变向机制</strong></td>
                  <td>支持以新目标修订草稿并用于后续运行；旧结果安全隔离，同义措辞不重复建档，模糊意图变化触发确认；具备取消与副作用边界防护，快照可跨 Runtime 重建恢复。</td>
                  <td><span class="section-badge green">PASS 已验收</span></td>
                </tr>
                <tr>
                  <td class="highlight"><strong>P4</strong></td>
                  <td><strong>反馈提纯与挖掘</strong></td>
                  <td>带独立预期的测试用例提案；样本用途分流与任务族/近重复隔离；先按范围（Scope）和工具契约分组，再启动聚类挖掘，消除模式杂音。</td>
                  <td><span class="section-badge green">PASS 已验收</span></td>
                </tr>
                <tr>
                  <td class="highlight"><strong>P5</strong></td>
                  <td><strong>LangGraph 有界恢复</strong></td>
                  <td>实际演进入口接通 LangGraph → RepairJob → 共同验证流水线；全局共享预算、原绝对期限保持（start_time 跨检查点不重置）、评测集漂移阻断（CHECKPOINT_INVALIDATED）、重复执行防护与显式晋升确认；拆分仅提供建议（SplitProposal advisory）。</td>
                  <td><span class="section-badge green">PASS 已验收</span></td>
                </tr>
                <tr>
                  <td class="highlight"><strong>P6</strong></td>
                  <td><strong>业务演示与交接</strong></td>
                  <td>离线完整生命周期证据闭环；真实模型小样本 A/B/C 试运行；真实失败修补与变向复用两条独立分支验证；正式 Skill 未来检索执行及新 Episode 增量回收；架构图谱与面试文档全量同步。</td>
                  <td><span class="section-badge green">PASS 已验收</span></td>
                </tr>
              </tbody>
            </table>
          </div>

          <div class="evidence-box" style="margin-top: 14px; background: rgba(16, 185, 129, 0.08); border-color: rgba(16, 185, 129, 0.3);">
            <div class="evidence-title" style="color: var(--accent-green);">收尾批次回归测试通过证据 (Exit Code: 0)</div>
            <p style="font-size: 13px; color: var(--text-secondary); margin-bottom: 6px;">
              最新全量收尾回归批次：<strong>61 项测试全部通过，其中包含 11 项 P5 恢复专项测试，exit 0</strong>。
            </p>
            <div style="font-size: 12px; font-family: var(--font-mono); color: var(--accent-cyan); background: rgba(0,0,0,0.3); padding: 8px 12px; border-radius: 6px;">
              61 passed in 4.28s | P5 Special: 11 passed (LangGraph checkpoint restore, deadline non-reset, dataset drift abort, shadow isolation) | Exit: 0
            </div>
            <p style="font-size: 11.5px; color: var(--text-muted); margin-top: 6px;">
              * 注：此为 P0~P6 收尾验收的一组强相关回归批次，非历史全量累计测试累加。
            </p>
          </div>
        </div>

        <!-- Block 2: Approved Adjustments -->
        <div class="detail-block" id="milestone-adjustments">
          <h3 class="detail-heading">0.2 经批准的两项核心架构调整落地事实</h3>
          <div style="display: grid; grid-template-columns: repeat(auto-fit, minmax(320px, 1fr)); gap: 16px; margin-top: 12px;">
            <div style="background: var(--bg-tertiary); border: 1px solid var(--border-accent); border-radius: 8px; padding: 16px;">
              <div style="font-size: 14px; font-weight: 700; color: var(--accent-cyan); margin-bottom: 8px;">
                📏 调整一：膨胀防线规则升级 (Inflation Check Rule)
              </div>
              <p style="font-size: 13px; color: var(--text-secondary); line-height: 1.6; margin-bottom: 8px;">
                由初期的单纯比例阈值升级为「<strong>比例 AND 净增超过 1000 token</strong>」复合阻断判定；同时在冷启动阶段设立<strong>正文上界约束在 3000 字符以内</strong>。
              </p>
              <div style="font-size: 12px; color: var(--text-muted); background: rgba(0,0,0,0.25); padding: 8px; border-radius: 4px; font-family: var(--font-mono);">
                if (new_tokens / old_tokens > ratio_limit) and (new_tokens - old_tokens > 1000):<br>
                &nbsp;&nbsp;&nbsp;&nbsp;raise ContentInflationError("Prompt膨胀超标，拒绝晋升")
              </div>
              <div style="font-size: 11.5px; color: var(--accent-amber); margin-top: 8px;">
                💡 收益：彻底解决短小修改因比例过高而误报的问题，同时杜绝恶意注入或失控自愈导致的正文无节制膨胀。
              </div>
            </div>

            <div style="background: var(--bg-tertiary); border: 1px solid rgba(168, 85, 247, 0.4); border-radius: 8px; padding: 16px;">
              <div style="font-size: 14px; font-weight: 700; color: var(--accent-purple); margin-bottom: 8px;">
                ⚙️ 调整二：LangGraph 演进执行引擎架构集成
              </div>
              <p style="font-size: 13px; color: var(--text-secondary); line-height: 1.6; margin-bottom: 8px;">
                <strong>复用成熟基础设施，解耦业务状态机</strong>：复用既有的 <code>SqliteCheckpointer</code>、<code>ALLOWED_MSGPACK_MODULES</code> 与 <code>JsonPlusSerializer</code> 检查点与反序列化基础设施；独立定制适配当前演进链的专用状态机业务节点（<code>RecoveryLoopState</code>, <code>RepairJob</code>）。
              </p>
              <div style="font-size: 12px; color: var(--text-muted); background: rgba(0,0,0,0.25); padding: 8px; border-radius: 4px; font-family: var(--font-mono);">
                ShadowDirectoryContext -> Dedicated RecoveryLoopState -> SqliteCheckpointer
              </div>
              <div style="font-size: 11.5px; color: var(--accent-purple); margin-top: 8px;">
                💡 收益：有界恢复在临时 Shadow 目录执行，不与旧 Evolver 节点杂糅，杜绝预算重复计算与权限逃逸。
              </div>
            </div>
          </div>
        </div>

        <!-- Block 3: Factual Evidence Bounds -->
        <div class="detail-block" id="milestone-evidence-bounds">
          <h3 class="detail-heading">0.3 已实现，但尚未在更高证据等级验证（技术清醒度与证据边界）</h3>
          <p style="font-size: 13px; color: var(--text-secondary); margin-bottom: 12px;">
            在技术面试和架构评审中，<strong>清晰说出系统未验证的边界，比吹嘘“完美无缺”更能赢得高职级资深架构师的尊重</strong>：
          </p>
          
          <div style="display: grid; grid-template-columns: repeat(auto-fit, minmax(280px, 1fr)); gap: 14px;">
            <div style="background: rgba(244, 63, 94, 0.06); border: 1px solid rgba(244, 63, 94, 0.3); border-radius: 8px; padding: 14px;">
              <div style="font-weight: 700; color: var(--accent-rose); font-size: 13.5px; margin-bottom: 6px;">
                🔬 1. 测试套件基于 FakeLLM
              </div>
              <div style="font-size: 12.5px; color: var(--text-secondary); line-height: 1.55;">
                收尾 61 项自动化回归测试全部在本地 FakeLLM 环境下运行（0 真实外部 API 消耗）。其验证的是<strong>状态机契约、预算兜底、超时中断与序列化安全</strong>，而非商业大模型的理解与推理表现。
              </div>
            </div>

            <div style="background: rgba(245, 158, 11, 0.06); border: 1px solid rgba(245, 158, 11, 0.3); border-radius: 8px; padding: 14px;">
              <div style="font-weight: 700; color: var(--accent-amber); font-size: 13.5px; margin-bottom: 6px;">
                🌿 2. 真实模型运行于两独立分支
              </div>
              <div style="font-size: 12.5px; color: var(--text-secondary); line-height: 1.55;">
                真实模型（Gemini 2.5 Pro）验证的是<strong>两条独立分支</strong>：<code>cand_real_v2_fb30297b</code>（失败修补分支）与 <code>cand_lifecycle_v2_8e70d68c</code>（变向复用分支）。两分支各自独立，<strong>严禁拼接成同一条连续端到端轨迹</strong>。
              </div>
            </div>

            <div style="background: rgba(56, 189, 248, 0.06); border: 1px solid rgba(56, 189, 248, 0.3); border-radius: 8px; padding: 14px;">
              <div style="font-weight: 700; color: var(--accent-cyan); font-size: 13.5px; margin-bottom: 6px;">
                ⛓️ 3. 完整生命周期仅具离线链证据
              </div>
              <div style="font-size: 12.5px; color: var(--text-secondary); line-height: 1.55;">
                “变向 → 新意图失败 → 修补 → 晋升 → 未来复用”这一极长链路，目前仅具备<strong>离线环境下的完整闭环证据</strong>；尚未在真实模型下跑通一镜到底的超长大闭环。
              </div>
            </div>

            <div style="background: rgba(168, 85, 247, 0.06); border: 1px solid rgba(168, 85, 247, 0.3); border-radius: 8px; padding: 14px;">
              <div style="font-weight: 700; color: var(--accent-purple); font-size: 13.5px; margin-bottom: 6px;">
                🛡️ 4. OS 级隔离尚未与恢复链组合验收
              </div>
              <div style="font-size: 12.5px; color: var(--text-secondary); line-height: 1.55;">
                早期的 macOS Seatbelt 专项验证具备真实系统证据；但当前 P5 有界恢复运行于 Python 临时 shadow 目录隔离，尚未与 Seatbelt 内核沙箱做联合组合验收。
              </div>
            </div>

            <div style="background: rgba(100, 116, 139, 0.1); border: 1px solid var(--border-color); border-radius: 8px; padding: 14px;">
              <div style="font-weight: 700; color: var(--text-primary); font-size: 13.5px; margin-bottom: 6px;">
                📊 5. 历史记录缺失（如实标注，绝不补造）
              </div>
              <div style="font-size: 12.5px; color: var(--text-secondary); line-height: 1.55;">
                DEV A 组仅有 2/6 汇总明细缺逐任务原始日志；早期 125 次调用仅有总体统计（token 分项为 null，无法精细分账）；修订前锁定和血缘依赖缺失部分快照。不为了补账虚构数据或重跑商业模型。
              </div>
            </div>

            <div style="background: rgba(16, 185, 129, 0.06); border: 1px solid rgba(16, 185, 129, 0.3); border-radius: 8px; padding: 14px;">
              <div style="font-weight: 700; color: var(--accent-green); font-size: 13.5px; margin-bottom: 6px;">
                📈 6. 经济收益与普遍效果尚未证实
              </div>
              <div style="font-size: 12.5px; color: var(--text-secondary); line-height: 1.55;">
                真实 A/B/C 小样本试运行已经完成，但跨任务的稳定泛化、显著经济收益与成本完全打平，仍需在大样本（如 18+18 任务族、每任务重复 3 次）的后续立项中深入验证。
              </div>
            </div>
          </div>
        </div>

        <!-- Block 4: Non-goals & Future Work -->
        <div class="detail-block" id="milestone-non-goals">
          <h3 class="detail-heading">0.4 本期不打算做与待定后续工作（清晰的责任与立项边界）</h3>
          <div style="display: grid; grid-template-columns: 1fr 1fr; gap: 16px; margin-top: 10px;">
            <div style="background: var(--bg-tertiary); border: 1px solid var(--border-color); border-radius: 8px; padding: 16px;">
              <div style="font-size: 14px; font-weight: 700; color: var(--accent-rose); margin-bottom: 8px;">
                🚫 本期明确不打算做 (Explicit Non-Goals)
              </div>
              <ul style="font-size: 13px; color: var(--text-secondary); padding-left: 18px; line-height: 1.65;">
                <li><strong>自动拆分成多正式 Skill</strong>：拆分只提供建议（<code>SplitProposal advisory</code>），绝不自动分裂实体或篡改路由；</li>
                <li><strong>所有候选强制进图</strong>：拒绝为了图谱好看把噪声强塞入图，未通过准入一律封存；</li>
                <li><strong>所有 REVIEW 自动重试</strong>：严格防死循环，超出预算立即转由人工介入（HITL）；</li>
                <li><strong>为形式上的“复用更多”返工旧 Evolver 节点</strong>：保持高内聚低耦合，保护既有稳定性；</li>
                <li><strong>补造历史数据或账单</strong>：绝不为了补足早期 125 次 null 记录去伪造日志；</li>
                <li><strong>自动接真实订单与生产系统发布</strong>：本期聚焦工程内核验证，不直接对接真实生产物流环境。</li>
              </ul>
            </div>

            <div style="background: var(--bg-tertiary); border: 1px solid var(--border-color); border-radius: 8px; padding: 16px;">
              <div style="font-size: 14px; font-weight: 700; color: var(--accent-cyan); margin-bottom: 8px;">
                ⏳ 待定：尚未立项的后续工作 (Future Proposals)
              </div>
              <ul style="font-size: 13px; color: var(--text-secondary); padding-left: 18px; line-height: 1.65;">
                <li><strong>全新任务族严格商业模型对照</strong>：使用预先冻结的评测协议与标准基准集进行长效对照；</li>
                <li><strong>大样本重复运行评测</strong>：落实原建议的 18＋18 任务样本、每任务重复 3 次，统计胜率方差与置信区间；</li>
                <li><strong>接入生产物流系统与真实业务数据</strong>：在合规授权前提下，接入真实物流仓储与订单链路；</li>
                <li><strong>更多真实 API 恢复实验与成本测量</strong>：精细化度量网络抖动、Token 消耗与修复成功率之间的 ROI 曲线。</li>
              </ul>
              <div style="font-size: 11.5px; color: var(--text-muted); margin-top: 10px;">
                * 注：以上待定项需重新评估业务 ROI 并获正式立项，原 18+18 方案属建议项，非本期遗漏。
              </div>
            </div>
          </div>
        </div>
      </section>
'''

target_insert_needle = '<!-- Section: Embedded Interactive Diagram Viewer -->'
if target_insert_needle in html:
    html = html.replace(target_insert_needle, milestone_section_html + '\n      ' + target_insert_needle, 1)
    print("Milestone section inserted successfully!")
else:
    print("WARNING: target_insert_needle not found!")

# 4. Update Question Cards in Resume Matrix
# Card L63 (Card 9)
old_l63 = '多级门禁与校验契约：静态 Schema 校验 ➔ 业务规则断言 ➔ 产物指纹绑定。ValidationReceipt 记录 content_fingerprint 与 validator_config_hash，双重匹配才放行。'
new_l63 = '双重校验：receipt.py 中 is_valid_for 强制比对产物 SHA-256 与验证器配置哈希；演进恢复流水线在修补前强制校验用例集哈希（dataset_version），漂移立即抛出 CHECKPOINT_INVALIDATED 阻断，消耗 0 次大模型调用。'
if old_l63 in html:
    html = html.replace(old_l63, new_l63, 1)
    print("Card L63 updated.")
else:
    print("WARNING: old_l63 not found in html!")

# Card L80 (Card 10)
old_l80 = '自主 Agent 容错成本高且易发散；确定性工作流收敛快。SkillForge 划定边界：核心路由与沙箱执行走状态机硬编码，局部产物自愈与 Prompt 调优交由自主 Agent。'
new_l80 = '确定性工作流容错成本低且收敛；自主 Agent 容易出现级联发散。SkillForge 采用 BoundedRecovery 有界恢复状态机，共享全局预算且保持原绝对超时期限（start_time 跨检查点不重置），超时立即强杀，杜绝无限重试。'
if old_l80 in html:
    html = html.replace(old_l80, new_l80, 1)
    print("Card L80 updated.")
else:
    print("WARNING: old_l80 not found in html!")

# Card L237 (Card 11)
old_l237 = 'CorrectionPolicy 限制白名单路径与操作；finalize 强验实时产物 SHA-256 与验证器配置 Hash，内容改动或规则漂移立即作废旧 PASS。'
new_l237 = '工程化组合拳：CorrectionPolicy 限定局部修复白名单；finalize 强制双重指纹重验；膨胀防线实行“比例 AND 净增超过 1000 token”双重阻断；收尾 61 项自动化回归测试 exit 0，杜绝假 PASS 交付。'
if old_l237 in html:
    html = html.replace(old_l237, new_l237, 1)
    print("Card L237 updated.")
else:
    print("WARNING: old_l237 not found in html!")

# Card L9 (Card 21)
old_l9 = '拒绝重型黑盒框架。SkillForge 基于 LangGraph 打造轻量级单向演进图，核心节点包括 CandidateGenerator、SandboxEvaluator、PromotionGate。'
new_l9 = 'LangGraph 架构工程实践：复用既有的 SqliteCheckpointer、ALLOWED_MSGPACK_MODULES 与 JsonPlusSerializer 基础设施；业务上独立编写专用 RecoveryLoopState 与 RepairJob 状态机，在临时 Shadow 目录执行，与旧 Evolver 节点完全解耦，杜绝预算重复计算与权限逃逸。'
if old_l9 in html:
    html = html.replace(old_l9, new_l9, 1)
    print("Card L9 updated.")
else:
    print("WARNING: old_l9 not found in html!")

# Card L188 (Card 30)
old_l188 = '两阶段异步确认：Worker 产出修复 Draft 挂起为 PENDING_REVIEW 写入 SQLite；恢复时重放事件流并校验前置版本依赖，防止脑裂。'
new_l188 = '基于 SqliteCheckpointer 实现状态机断点续传。恢复时校验快照完整性与依赖哈希；演进提炼出的多技能拆分提议（SplitProposal）严格保持咨询性质（advisory），绝不自动分裂或自作主张修改路由，确保状态流转确定可控。'
if old_l188 in html:
    html = html.replace(old_l188, new_l188, 1)
    print("Card L188 updated.")
else:
    print("WARNING: old_l188 not found in html!")

# 5. Update Section 5.3 (System Limitations Table)
old_limits_table = '''              <tbody>
                <tr>
                  <td><strong>跨平台沙箱统一性</strong></td>
                  <td>当前深度利用 macOS Seatbelt 获得 &lt;5ms 极速启动，Linux 下回退为受限子进程</td>
                  <td>引入 Linux <code>bubblewrap (bwrap)</code> 与 <code>seccomp-bpf</code>，统一跨 POSIX 系统调用级沙箱抽象</td>
                </tr>
                <tr>
                  <td><strong>Python 代码技能的自动演进</strong></td>
                  <td>Prompt SOP 已实现全自动演进，但 Python 代码技能的自生成目前仍需人工确认（HITL）</td>
                  <td>引入短生命周期 Firecracker MicroVM，实现代码技能的全自动沙箱执行与动态单测生成</td>
                </tr>
                <tr>
                  <td><strong>多节点分布式协调</strong></td>
                  <td>当前基于 SQLite 单机 CAS 状态机，满足单机高并发与本地研发调试</td>
                  <td>生产级集群部署时下沉至 PostgreSQL + Redis 分布式锁，利用租约机制协调跨实例部署</td>
                </tr>
              </tbody>'''

new_limits_table = '''              <tbody>
                <tr>
                  <td><strong>真实模型端到端连续轨迹</strong></td>
                  <td>真实模型（Gemini 2.5 Pro）验证了“失败修补”与“变向复用”两条独立分支，端到端大闭环目前仅具备离线完整链证据</td>
                  <td>在大样本后续立项中，用预先冻结的协议跑通跨会话全流程一镜到底，不把两条独立分支拼接为伪证据</td>
                </tr>
                <tr>
                  <td><strong>OS 级沙箱与恢复链组合验收</strong></td>
                  <td>早期 Seatbelt 内核级沙箱专项验证已通过，但本次 P5 有界恢复运行于 Python 临时 shadow 目录隔离，尚未与 Seatbelt 组合验收</td>
                  <td>将 RepairJob 子进程执行下沉至 Seatbelt/bwrap 沙箱内，完成内核级资源权限与恢复链的端到端联调验收</td>
                </tr>
                <tr>
                  <td><strong>历史审计数据缺失（坚持不补造）</strong></td>
                  <td>DEV A 组仅保留 2/6 汇总明细缺逐任务原始日志；早期 125 次调用仅有总量统计（token 分项为 null，无法精细分账）</td>
                  <td>严守“不造假、不为了账面好看重跑模型”的工程伦理，新架构中全面实施不可变事件流与全字段强制遥测</td>
                </tr>
                <tr>
                  <td><strong>普遍经济收益与大样本泛化</strong></td>
                  <td>真实模型小样本 A/B/C 验证已证明机制可用，但经济收益打平与跨任务族稳定泛化尚未在大样本下充分证实</td>
                  <td>建议开展 18＋18 任务族、每任务重复 3 次的严格对照评测（原计划建议项），系统性评估成功率与方差</td>
                </tr>
                <tr>
                  <td><strong>跨平台沙箱统一性</strong></td>
                  <td>当前深度利用 macOS Seatbelt 获得 &lt;5ms 极速启动，Linux 下回退为受限子进程</td>
                  <td>引入 Linux <code>bubblewrap (bwrap)</code> 与 <code>seccomp-bpf</code>，统一跨 POSIX 系统调用级沙箱抽象</td>
                </tr>
              </tbody>'''

if old_limits_table in html:
    html = html.replace(old_limits_table, new_limits_table, 1)
    print("Section 5.3 Limitations Table updated.")
else:
    print("WARNING: old_limits_table not found in html!")

with open('docs/skillforge-knowledge-index.html', 'w', encoding='utf-8') as f:
    f.write(html)

print("docs/skillforge-knowledge-index.html updated successfully!")
