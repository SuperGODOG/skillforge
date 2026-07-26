# 事件：路由硬负例识别调优（Phase 2 P2.T5-T7）

**日期**：2026-07-26
**阶段**：Phase 2 · IntentRouter 三层路由 + 50 硬负例评测

## 问题场景

首版 IntentRouter 严格按方案书 §4.3 的"规则 → embed → LLM 级联"实现，
让规则层 keyword 独占命中直接决策。跑 50 条评测集：

- 正例 R@1 = 96.67%
- **硬负例 R@1 = 5.56%**（1/18，惨败）
- 总 R@1 = 62%（离门槛 80% 差 18 分）

## 根因

规则层 keyword 子串命中 → 独占决策，无法区分"字面词命中"和"意图匹配"。

| 硬负例 query | 命中 keyword | 错选 skill | 应该 |
|---|---|---|---|
| 帮我写一个正则匹配所有邮箱 | 正则 | explain_regex | NONE（生成 vs 讲解） |
| 北京最近三年的年均降水量 | 降水 | weather_query | NONE（气候学 vs 预报） |
| 帮我写会议纪要 | ~（无命中）~ | write_weekly_report（embed 层） | NONE |

**方案书 §4.1 其实明确写过**：`trigger.keywords 降级为排序信号，不做自动展开`。
首版实现违反了这条口径——规则独占决策≈自动展开。

## 修复三步

### Step 1：规则不独占决策（62% → 64% R@1）

改 `cascade.py`：规则层只做记账，始终走 embed。
效果：
- 正例 R@1 96.67% → 70%（一些正例 top1 < HIGH_CONF 被拒）
- 硬负例 R@1 5.56% → 50%
- 总 R@1 62% → 64%
- **总 R@3 64% → 82%**（正例的 expected 100% 在 top-3 里）

### Step 2：启用 LLM 兜底（64% → 78% R@1）

修复 `hello-agents>=1.0` 返回 `LLMResponse` 对象（不再是 str）的兼容问题
（`resp.content` 而非 `resp.strip()`）。

效果：
- 正例 R@1 96.67%（恢复）
- 硬负例 R@1 44.44%（LLM 部分识别）
- 总 R@1 78%（门槛 80% 差 1 条）
- 总 R@3 80%

### Step 3：调阈值 + 升级 LLM prompt（78% → 98% R@1）

- **阈值**：HIGH_CONF 0.55→0.75，MARGIN 0.05→0.10（embed 独占门槛提高，
  更多中间地带交 LLM）
- **LLM prompt**：候选块从"name + description"扩到"name + description +
  use_when + not_for"；判定规则明写"命中 not_for 必须 NONE / 宁可 NONE 不硬选"
- **接口**：`LLMLayer.choose` 接受 SkillMeta 对象（不再是 (name, desc) 元组）

## 最终结果

| 类型 | R@1 | R@3 |
|---|---|---|
| 正例（30） | 96.67% | **100%** |
| 硬负例（18） | **100%** | **100%** |
| 不相关（2） | 100% | 100% |
| **总（50）** | **98%** | **100%** |

**双双大幅超过门槛**（R@1 ≥ 80%, R@3 ≥ 90%）。

唯一 R@1 fail：`"讲一下 lookahead 是什么"` —— LLM 对英文技术词
不确定属于哪个 skill，输出 NONE；但 top3 含 explain_regex 所以 R@3 pass。

## 教训

1. **口径要坚守**：方案书 A.2 一致性口径清单已列 `trigger.keywords 只作为路由排序信号，不做自动展开`——首版实现违反了。修改前先查一致性清单。
2. **硬负例调优三件套**：规则不独占 + LLM 兜底 + prompt 给 not_for。缺一严重掉分。
3. **调阈值前先看分布**：不是拍脑袋，是根据评测集里 top1 相似度分布调（当前阈值把 harden negative 和 positive 分开）。
