# Interview Design Map — SuperGODOG__skillforge

> 由 repo_fuse 自动生成（面试题库 × 本仓库设计映射）。用于把本项目的设计决策与面试高频设计主题对齐。

## 项目技术画像

- 框架/技术栈：Pydantic、HuggingFace Transformers
- 文件规模：43 个源文件
- 与面试题库命中 21 个设计主题

## 设计主题 ↔ 仓库实现映射

| 面试设计主题 | 仓库中的实现/证据 | 对应题库位置 |
|---|---|---|
| ReAct 与 Agent 工作流范式 | <内容命中> | 见题库「ReAct 与 Agent 工作流范式」概念笔记 |
| MCP | <内容命中> | 见题库「MCP」概念笔记 |
| 容错限流与高可用 | <内容命中> | 见题库「容错限流与高可用」概念笔记 |
| LangGraph 与图编排 | tests/test_state_machine.py, src/skillforge/state_machine.py | 见题库「LangGraph 与图编排」概念笔记 |
| Prompt Engineering | <内容命中> | 见题库「Prompt Engineering」概念笔记 |
| RAG 与知识库 | <内容命中> | 见题库「RAG 与知识库」概念笔记 |
| 技能体系 | SkillForge-项目方案书-v3.docx, src/skillforge/state_machine.py, src/skillforge/models.py | 见题库「技能体系」概念笔记 |
| Agent 记忆机制 | <内容命中> | 见题库「Agent 记忆机制」概念笔记 |
| AI Coding Agent | <内容命中> | 见题库「AI Coding Agent」概念笔记 |
| 上下文管理与压缩 | <内容命中> | 见题库「上下文管理与压缩」概念笔记 |
| 意图识别与分发 | <内容命中> | 见题库「意图识别与分发」概念笔记 |
| 向量数据库与 HNSW | <内容命中> | 见题库「向量数据库与 HNSW」概念笔记 |
| Badcase 闭环与数据飞轮 | <内容命中> | 见题库「Badcase 闭环与数据飞轮」概念笔记 |
| 评测体系与 Benchmark | evaluation_sets/router_negatives.json, evaluation_sets/baseline_dev.json, evaluation_sets/baseline_hidden.json | 见题库「评测体系与 Benchmark」概念笔记 |
| 存储选型与状态持久化 | <内容命中> | 见题库「存储选型与状态持久化」概念笔记 |

## 建议的面试切入点

- 本仓库最能体现设计深度的模块（按命中概念与证据文件定位，建议对照《项目内作答》准备）
- 每个主题准备"框架给的 vs 我设计的"对照（如用 LangGraph 则强调图结构与状态设计的自有决策）

---
生成时间：由 repo_fuse 生成，随题库/仓库变化可重跑更新。
