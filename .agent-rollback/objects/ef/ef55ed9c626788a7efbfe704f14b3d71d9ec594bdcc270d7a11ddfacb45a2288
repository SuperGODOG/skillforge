"""Failure Bundles 失败样本集夹具 (P1-G / A2-lite)

包含 4 根因策略的独立失败样例集（每类各 3 个独立样本，共 12 个样本）：
1. prompt 缺陷 (prompt_defect: prompt_vague / boundary_missing)
2. 路由误判 (route_misjudgment: trigger_inaccurate)
3. 执行依赖 (execution_dependency: deps_broken / dependency)
4. 评测噪声 (evaluation_noise: eval_noise)
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Optional


@dataclass
class FailureBundleItem:
    case_id: str
    skill_name: str
    query: str
    reference: str
    output_skill: str
    output_baseline: str
    losing_dims: list[str]
    expected_root_cause: str
    category: str
    route_trace: dict[str, Any]
    tool_trace: dict[str, Any]
    relevant_body_sections: dict[str, str]
    description: str


FAILURE_BUNDLES: dict[str, list[dict[str, Any]]] = {
    # 策略 1: prompt 缺陷 (Instructions 模糊 / Constraints 缺失 / Examples 不足)
    "prompt_defect": [
        {
            "case_id": "bundle_pd_01",
            "skill_name": "explain_regex",
            "query": "讲讲 \\1 反向引用",
            "reference": "定义 backreference；小例子（如 (\\w)\\1 匹配双字符）；命名版 (?P=name)",
            "output_skill": "反向引用就是对前面捕获组的引用，例如 \\1 代表第 1 个分组。",
            "output_baseline": "反向引用（Backreference）用于在正则表达式中匹配与先前某个捕获分组完全相同的内容。例如 (\\w)\\1 可以匹配 aa, bb 等重复字符。命名分组语法为 (?P=name)。",
            "losing_dims": ["task_completion", "readability"],
            "expected_root_cause": "prompt_vague",
            "category": "prompt_defect",
            "route_trace": {
                "hit_layer": "rule",
                "trigger_keywords": ["正则", "regex", "讲解", "匹配过程", "回溯"],
                "matched_keywords": ["讲解"],
                "use_when": "用户想理解正则表达式的语法、匹配机制、回溯原理",
                "not_for": ["用户要求写一个正则匹配 XX"],
            },
            "tool_trace": {
                "failed_phase": "none",
                "failure_type": "NONE",
                "is_dependency_failure": False,
                "tool_provenances": [],
                "validation_channels": ["prompt"],
                "error_message": "",
            },
            "relevant_body_sections": {
                "Instructions": "1. 识别用户问的是具体某个正则还是概念原理。2. 走对应模板。3. 用小例子演示。",
            },
            "description": "er_h02 同款特征：Instructions 中缺少对反向引用等高级语法的详细步骤和命名分组说明",
        },
        {
            "case_id": "bundle_pd_02",
            "skill_name": "weather_query",
            "query": "预测下北京明年的今天会不会下雪？",
            "reference": "明确告知气象预报能力边界：常规 API 仅支持未来 3-7 天预报，无法预测一年后的具体降水，避免虚构。",
            "output_skill": "北京明年今天可能会下小雪，气温约为 -2 度到 3 度，出门请注意防寒保暖。",
            "output_baseline": "抱歉，天气预报仅支持近期（通常 3-7 天内）客观气象数据，无法预测明年今日的具体天气，请在临近日期再次查询。",
            "losing_dims": ["robustness"],
            "expected_root_cause": "boundary_missing",
            "category": "prompt_defect",
            "route_trace": {
                "hit_layer": "rule",
                "trigger_keywords": ["天气", "气温", "预报"],
                "matched_keywords": ["天气", "预报"],
                "use_when": "查询城市实时与近期天气预报",
                "not_for": ["极端气候历史大数据研究"],
            },
            "tool_trace": {
                "failed_phase": "none",
                "failure_type": "NONE",
                "is_dependency_failure": False,
                "tool_provenances": [],
                "validation_channels": ["prompt"],
                "error_message": "",
            },
            "relevant_body_sections": {
                "Constraints": "- 必须调用天气工具查询。- 禁止编造当前天气。",
            },
            "description": "Constraints 边界缺失：未限制超长跨度（一年后）时间查询，导致模型幻觉生成虚假预报",
        },
        {
            "case_id": "bundle_pd_03",
            "skill_name": "explain_regex",
            "query": "为什么 [a-Z] 是错的？",
            "reference": "指出 ASCII 码表中大写字母 Z (ASCII 90) 在小写字母 a (ASCII 97) 之前，范围倒置；正确写法是 [a-zA-Z] 或 [A-Za-z]。",
            "output_skill": "因为正则是区分大小写的，[a-Z] 包含了所有字母。",
            "output_baseline": "在 ASCII 字符编码中，大写字母 A-Z（65-90）位于小写字母 a-z（97-122）之前，因此 [a-Z] 是非法逆序区间。正确写法应为 [a-zA-Z]。",
            "losing_dims": ["task_completion", "robustness"],
            "expected_root_cause": "prompt_vague",
            "category": "prompt_defect",
            "route_trace": {
                "hit_layer": "rule",
                "trigger_keywords": ["正则", "regex", "讲解"],
                "matched_keywords": ["讲解"],
                "use_when": "讲解正则表达式的原理与匹配过程",
                "not_for": [],
            },
            "tool_trace": {
                "failed_phase": "none",
                "failure_type": "NONE",
                "is_dependency_failure": False,
                "tool_provenances": [],
                "validation_channels": ["prompt"],
                "error_message": "",
            },
            "relevant_body_sections": {
                "Instructions": "按具体正则、概念原理、区别走对应模板；优先用小例子演示。",
            },
            "description": "Overview/Instructions 未强调底层字符编码与常见语法陷阱，导致解释不准确",
        },
    ],
    # 策略 2: 路由误判 (trigger 关键词缺失 / use_when 边界模糊 / not_for 误伤)
    "route_misjudgment": [
        {
            "case_id": "bundle_rm_01",
            "skill_name": "explain_regex",
            "query": "请剖析一下这个 Pattern 表达式匹配机制",
            "reference": "剖析 Pattern 表达式字符及量词分支匹配机制",
            "output_skill": "（Skill 未被路由命中，由兜底通用模型简略作答）",
            "output_baseline": "从原理层面讲解此 Pattern 表达式的符号及回溯机制...",
            "losing_dims": ["task_completion"],
            "expected_root_cause": "trigger_inaccurate",
            "category": "route_misjudgment",
            "route_trace": {
                "hit_layer": "llm",
                "trigger_keywords": ["正则", "regex", "讲解", "回溯"],
                "matched_keywords": [],
                "use_when": "用户想理解正则表达式语法",
                "not_for": [],
                "routing_notes": "查询仅包含 Pattern 与 剖析，关键词均未命中，路由层级落空导致未调用 Skill",
            },
            "tool_trace": {
                "failed_phase": "none",
                "failure_type": "NONE",
                "is_dependency_failure": False,
                "tool_provenances": [],
                "validation_channels": ["router"],
                "error_message": "Router 规则未匹配到 trigger.keywords",
            },
            "relevant_body_sections": {},
            "description": "关键词缺失：用户使用近义词 Pattern/剖析，trigger.keywords 缺少 Pattern 导致路由漏召回",
        },
        {
            "case_id": "bundle_rm_02",
            "skill_name": "weather_query",
            "query": "明天出门去海边游泳合适吗？",
            "reference": "识别天气意图与涉水活动关联，查询天气气温、风力与降雨情况并给出建议",
            "output_skill": "去海边游泳请注意防晒并准备好泳装。（未触发天气查询）",
            "output_baseline": "明天天气晴朗，气温 28 度，西南风 2 级，非常适合海边户外游泳活动。",
            "losing_dims": ["task_completion"],
            "expected_root_cause": "trigger_inaccurate",
            "category": "route_misjudgment",
            "route_trace": {
                "hit_layer": "rule",
                "trigger_keywords": ["天气", "气温", "下雨", "风力"],
                "matched_keywords": [],
                "use_when": "用户明确询问天气、气温",
                "not_for": [],
                "routing_notes": "生活场景意图（海边游泳合适吗）隐含天气需求，但 use_when 过于严苛导致路由判定放弃",
            },
            "tool_trace": {
                "failed_phase": "none",
                "failure_type": "NONE",
                "is_dependency_failure": False,
                "tool_provenances": [],
                "validation_channels": ["router"],
                "error_message": "",
            },
            "relevant_body_sections": {},
            "description": "use_when 定义过窄：未能覆盖出行/户外活动隐含天气意图",
        },
        {
            "case_id": "bundle_rm_03",
            "skill_name": "explain_regex",
            "query": "你能结合一段 Python 代码帮我讲讲这个正则怎么匹配的吗？",
            "reference": "聚焦解释正则表达式本身的匹配原理，代码仅作为宿主上下文",
            "output_skill": "抱歉，根据 not_for 规范，我不提供写代码或编程服务。",
            "output_baseline": "在这段 Python 代码中，调用的正则是 re.search(r'\\\\d+', text)，其匹配原理是...",
            "losing_dims": ["task_completion", "robustness"],
            "expected_root_cause": "trigger_inaccurate",
            "category": "route_misjudgment",
            "route_trace": {
                "hit_layer": "rule",
                "trigger_keywords": ["正则", "regex", "讲解"],
                "matched_keywords": ["代码", "正则", "讲讲"],
                "use_when": "讲解正则表达式原理",
                "not_for": ["用户要求写一个正则匹配 XX（那是代码生成任务）"],
                "routing_notes": "用户查询出现代码字样，误触发 not_for 的拒答防御",
            },
            "tool_trace": {
                "failed_phase": "none",
                "failure_type": "NONE",
                "is_dependency_failure": False,
                "tool_provenances": [],
                "validation_channels": ["router"],
                "error_message": "",
            },
            "relevant_body_sections": {},
            "description": "not_for 负向边界定义过于武断，产生误报将合法解释任务拦截",
        },
    ],
    # 策略 3: 执行依赖 (外部 API 503 / 工具缺失 / 熔断及凭据错误)
    "execution_dependency": [
        {
            "case_id": "bundle_ed_01",
            "skill_name": "weather_query",
            "query": "杭州今天天气怎么样？",
            "reference": "识别城市=杭州，调用天气 API 获取气温风力并客观展示",
            "output_skill": "远程天气服务连接超时 (503 Service Unavailable)",
            "output_baseline": "杭州今天阴天，气温 18-24 度，东北风 3 级。",
            "losing_dims": ["task_completion", "robustness"],
            "expected_root_cause": "deps_broken",
            "category": "execution_dependency",
            "route_trace": {
                "hit_layer": "rule",
                "trigger_keywords": ["天气", "气温"],
                "matched_keywords": ["天气"],
                "use_when": "查询天气",
                "not_for": [],
            },
            "tool_trace": {
                "failed_phase": "tool_execution",
                "failure_type": "DEPENDENCY_ERROR",
                "is_dependency_failure": True,
                "error_message": "HTTPConnectionError: 503 Service Unavailable while reaching amap_weather_api",
                "validation_channels": ["tools", "execution"],
                "tool_provenances": [
                    {
                        "tool_name": "amap_weather_api",
                        "fixture_case_id": "wq_d05",
                        "output_status": "ERROR",
                        "tool_success": False,
                        "authenticity_pass": False,
                        "output_summary": "HTTP 503 Service Unavailable",
                        "latency_ms": 3000.0,
                    }
                ],
            },
            "relevant_body_sections": {},
            "description": "外部服务不可用：第三方气象服务接口 503 超时，Prompt 无法修补网络服务故障",
        },
        {
            "case_id": "bundle_ed_02",
            "skill_name": "weather_query",
            "query": "北京今天穿什么合适？",
            "reference": "调用气温工具后给出穿衣指数建议",
            "output_skill": "执行环境错误：未在沙箱注册指定的工具 `amap_weather_api`",
            "output_baseline": "北京今天气温 12 度微风，建议着风衣或夹克等春秋服装。",
            "losing_dims": ["task_completion"],
            "expected_root_cause": "deps_broken",
            "category": "execution_dependency",
            "route_trace": {
                "hit_layer": "rule",
                "trigger_keywords": ["天气", "穿什么"],
                "matched_keywords": ["天气"],
                "use_when": "查询天气及相关建议",
                "not_for": [],
            },
            "tool_trace": {
                "failed_phase": "sandbox",
                "failure_type": "TOOL_NOT_FOUND",
                "is_dependency_failure": True,
                "error_message": "ToolNotFound: Registered dependency tool `amap_weather_api` missing from runtime environment",
                "validation_channels": ["tools"],
                "tool_provenances": [],
            },
            "relevant_body_sections": {},
            "description": "运行时环境缺失声明的依赖工具，需装配工具而非修改 prompt",
        },
        {
            "case_id": "bundle_ed_03",
            "skill_name": "weather_query",
            "query": "上海明天下雨吗？",
            "reference": "调用天气工具查询降水概率",
            "output_skill": "服务异常：由于连续调用失败，熔断器已被拉下 (CIRCUIT_OPEN)",
            "output_baseline": "上海明天有小到中雨，降水概率 80%，出门请携带雨具。",
            "losing_dims": ["task_completion", "robustness"],
            "expected_root_cause": "deps_broken",
            "category": "execution_dependency",
            "route_trace": {
                "hit_layer": "rule",
                "trigger_keywords": ["天气", "下雨"],
                "matched_keywords": ["下雨"],
                "use_when": "查询天气降雨",
                "not_for": [],
            },
            "tool_trace": {
                "failed_phase": "tool_execution",
                "failure_type": "CIRCUIT_OPEN",
                "is_dependency_failure": True,
                "error_message": "CircuitBreakerOpen: amap_weather_api circuit is open due to recent error rate",
                "validation_channels": ["tools", "execution"],
                "tool_provenances": [
                    {
                        "tool_name": "amap_weather_api",
                        "fixture_case_id": "wq_h02",
                        "output_status": "CIRCUIT_OPEN",
                        "tool_success": False,
                        "authenticity_pass": True,
                        "output_summary": "CIRCUIT_OPEN: 熔断器开启，拒绝外呼",
                        "latency_ms": 1.2,
                    }
                ],
            },
            "relevant_body_sections": {},
            "description": "工具熔断：外部服务熔断器开路，判定为底层依赖失效",
        },
    ],
    # 策略 4: 评测噪声 (Judge 偶然抖动 / Reference 歧义 / 权重微差)
    "evaluation_noise": [
        {
            "case_id": "bundle_en_01",
            "skill_name": "explain_regex",
            "query": "简单说下 .* 贪婪匹配",
            "reference": "说明 .* 尽可能多匹配字符",
            "output_skill": ".* 表示匹配任意字符零次或多次，具有贪婪性，会尽可能多地吃进字符直至行尾。",
            "output_baseline": ".* 是贪婪匹配，它会尽可能匹配最长的符合要求的字符串。",
            "losing_dims": ["readability"],
            "expected_root_cause": "eval_noise",
            "category": "evaluation_noise",
            "route_trace": {
                "hit_layer": "rule",
                "trigger_keywords": ["正则", "regex", "讲解"],
                "matched_keywords": ["讲解"],
                "use_when": "讲解正则表达式原理",
                "not_for": [],
            },
            "tool_trace": {
                "failed_phase": "judge",
                "failure_type": "EVAL_NOISE",
                "is_dependency_failure": False,
                "error_message": "Pairwise Judge 偶发打分抖动：两次运行对同义表达的可读性裁决出现反转",
                "validation_channels": ["judge"],
                "tool_provenances": [],
            },
            "relevant_body_sections": {},
            "description": "Judge 评分波动：内容实质等价但评测模型在可读性维度微小抖动判定 B_better",
        },
        {
            "case_id": "bundle_en_02",
            "skill_name": "explain_regex",
            "query": "正则里的 ^ 和 $ 具体指什么？",
            "reference": "^ 表示字符串或行的起始，$ 表示字符串或行的末尾（受 multiline 影响）",
            "output_skill": "^ 匹配输入的开头（或每行行首），$ 匹配输入的结尾（或每行行尾）。",
            "output_baseline": "^ 锚定头部，$ 锚定尾部，属于零宽断言。",
            "losing_dims": ["task_completion"],
            "expected_root_cause": "eval_noise",
            "category": "evaluation_noise",
            "route_trace": {
                "hit_layer": "rule",
                "trigger_keywords": ["正则", "regex"],
                "matched_keywords": ["正则"],
                "use_when": "讲解正则表达式原理",
                "not_for": [],
            },
            "tool_trace": {
                "failed_phase": "judge",
                "failure_type": "EVAL_NOISE",
                "is_dependency_failure": False,
                "error_message": "标注参考答案与用例判定存在两可偏好",
                "validation_channels": ["judge"],
                "tool_provenances": [],
            },
            "relevant_body_sections": {},
            "description": "参考答案风格偏好噪声：用例未标准化风格偏好导致平局被随机判定为负",
        },
        {
            "case_id": "bundle_en_03",
            "skill_name": "weather_query",
            "query": "广州现在热吗？",
            "reference": "给出当前温度并说明体感",
            "output_skill": "广州当前气温 27 度，湿度较高，体感较热。",
            "output_baseline": "广州目前 27℃，体感稍热。",
            "losing_dims": ["readability"],
            "expected_root_cause": "eval_noise",
            "category": "evaluation_noise",
            "route_trace": {
                "hit_layer": "rule",
                "trigger_keywords": ["天气", "气温"],
                "matched_keywords": ["天气"],
                "use_when": "查询天气",
                "not_for": [],
            },
            "tool_trace": {
                "failed_phase": "judge",
                "failure_type": "EVAL_NOISE",
                "is_dependency_failure": False,
                "error_message": "客观指标与主观评审分歧微小",
                "validation_channels": ["judge"],
                "tool_provenances": [],
            },
            "relevant_body_sections": {},
            "description": "轻微评测噪声：两版本输出几乎一致，评测模型微小偏好差异",
        },
    ],
}


def get_failure_bundle(category: str) -> list[dict[str, Any]]:
    """获取指定根因策略分类的失败样本集（每类至少 3 个）。"""
    if category not in FAILURE_BUNDLES:
        raise KeyError(f"Unknown bundle category: {category}. Valid: {list(FAILURE_BUNDLES.keys())}")
    return FAILURE_BUNDLES[category]


def get_all_failure_bundles() -> dict[str, list[dict[str, Any]]]:
    """获取所有根因策略分类的失败样本集。"""
    return FAILURE_BUNDLES
