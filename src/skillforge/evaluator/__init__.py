"""八维评估器装配：结构分 40 + 效果分 60 + 客观指标 + P0 门槛

flow（evaluate_skill）：
    1. 结构分：SkillMeta 静态检查（不阻断，只出警告）
    2. 效果分：对每个 case 跑「无 Skill vs 有 Skill」两版本 → Judge 配对
       → 任务完成度 / 鲁棒性 / 可读性 3 维配对；效率维用客观 token 比
    3. 客观指标：turns / tokens / latency 平均
    4. P0：P0 case 中 task_completion 维度 B_better（对照更好）视为 P0 fail

参见 ARCHITECTURE §4-E、§7
"""
from __future__ import annotations
import time
from pathlib import Path
from statistics import mean
from typing import Optional

from hello_agents.tools import Tool, ToolParameter

from ..models import EvalResult, RatchetVerdict
from .structure import score_structure, structure_total
from .judge import PairwiseJudge
from .metrics import collect_objective_metrics
from .ratchet import check_ratchet as _check_ratchet


DEFAULT_SYSTEM_PROMPT_HEADER = (
    "你是一个 Agent。以下是你要遵守的 Skill 说明书；请严格按 Instructions "
    "执行、按 Constraints 拒绝越界请求：\n\n"
)


class SkillEvaluator(Tool):
    def __init__(self, registry, llm, judge_llm=None):
        """
        Args:
            registry: SkillRegistry 实例（提供 get_meta / get body）
            llm:      运行 Agent 的 LLM（模拟"用户调用 Skill"）
            judge_llm: Judge 用的 LLM（默认与 llm 相同；生产可换独立更强模型降自评偏见）
        """
        super().__init__(
            name="skill_evaluator",
            description="八维评估器：结构分 40 + 效果分 60 + 客观指标 + 棘轮门槛",
        )
        self.registry = registry
        self.llm = llm
        self.judge = PairwiseJudge(judge_llm or llm)

    def get_parameters(self) -> list[ToolParameter]:
        return [
            ToolParameter(name="skill_name", type="string",
                          description="要评估的 skill_name", required=True),
            ToolParameter(name="eval_set", type="string",
                          description="评估集名（对应 evaluation_sets/<name>.json）",
                          required=False, default="baseline_dev"),
        ]

    def run(self, parameters: dict) -> str:
        result = self.evaluate_skill(
            parameters["skill_name"],
            parameters.get("eval_set", "baseline_dev"),
        )
        return f"total={structure_total(result.structure_score) + sum(result.effect_score.values()):.2f}, p0={result.p0_pass}"

    # ARCHITECTURE §7 签名
    def evaluate(self, release_id: str, eval_set: str = "baseline_dev") -> EvalResult:
        """按 release_id 走：从 SQLite 反查 skill_name 再走 evaluate_skill"""
        rel = self.registry.get_current_release(release_id) if hasattr(self.registry, "get_current_release_by_id") else None
        # Phase 3 简化：直接从 release 表读 skill_name
        sm = self.registry._get_sm()
        row = sm.get_release(release_id)
        if not row:
            raise KeyError(f"release_id 不存在：{release_id}")
        return self.evaluate_skill(row["skill_name"], eval_set, release_id=release_id)

    def check_ratchet(self, old: Optional[EvalResult], new: EvalResult) -> RatchetVerdict:
        return _check_ratchet(old, new)

    # ----------------- 核心 -----------------

    def evaluate_skill(
        self,
        skill_name: str,
        eval_set: str = "baseline_dev",
        release_id: str = "",
        cases: Optional[list[dict]] = None,
        p0_ids: Optional[list[str]] = None,
        verbose: bool = False,
    ) -> EvalResult:
        """
        跑评估的核心方法（不强依赖 release_id）。

        Args:
            skill_name: 要评估的 skill
            eval_set:   评估集文件名（不含后缀），默认 baseline_dev
            release_id: 可选，写进 EvalResult；单跑评估时可空
            cases:      可选，直接传 case 列表（供测试注入）
            p0_ids:     可选，P0 case ID 列表（不传则从 p0_cases.json 读）
            verbose:    True 时逐 case 打印进度
        """
        meta = self.registry.get_meta(skill_name)
        # 从注册表拿磁盘 body（Phase 3 不强绑 git commit 版本）
        body = self.registry._bodies.get(skill_name, "")

        if cases is None:
            cases = self._load_cases(eval_set, skill_name)
        if p0_ids is None:
            p0_ids = self._load_p0_ids()

        # 1. 结构分
        struct = score_structure(meta, body)

        # 2. 效果分（3 维 Judge 配对 + 1 维客观效率）
        verdicts = {"task_completion": [], "robustness": [], "readability": []}
        base_metrics_list = []
        skill_metrics_list = []
        p0_pass = True

        case_verdicts: list[dict] = []  # Phase 4 元 Agent 输入
        case_outputs: list[dict] = []

        for i, case in enumerate(cases):
            query = case["query"]
            ref = case.get("reference")
            case_id = case["id"]

            if verbose:
                print(f"  [{i + 1}/{len(cases)}] {case_id}: {query[:40]}...")

            base_out, base_m = self._run_bare(query)
            skill_out, skill_m = self._run_with_skill(query, body)
            base_metrics_list.append(base_m)
            skill_metrics_list.append(skill_m)

            per_case = {"case_id": case_id, "query": query}
            for dim in verdicts:
                v = self.judge.compare(query, skill_out, base_out, dim, reference=ref)
                verdicts[dim].append((case_id, v))
                per_case[dim] = v
                # P0 语义：task 维度上 skill 版被判 B_better（不如 baseline） → P0 fail
                if case_id in p0_ids and dim == "task_completion" and v == "B_better":
                    p0_pass = False
            case_verdicts.append(per_case)
            case_outputs.append({
                "case_id": case_id,
                "query": query,
                "reference": ref,
                "output_skill": skill_out,
                "output_baseline": base_out,
            })

        # 效果分：胜=1 平=0.5 负=0 加权
        effect = {
            "task": self._dim_score(verdicts["task_completion"], max_score=25.0),
            "robust": self._dim_score(verdicts["robustness"], max_score=15.0),
            "readability": self._dim_score(verdicts["readability"], max_score=10.0),
            "efficiency": self._efficiency_score(base_metrics_list, skill_metrics_list),
        }

        # 3. 客观指标平均
        obj = {
            "avg_turns_skill": round(mean(m["turns"] for m in skill_metrics_list), 2),
            "avg_tokens_skill": round(mean(m["tokens"] for m in skill_metrics_list), 2),
            "avg_tokens_baseline": round(mean(m["tokens"] for m in base_metrics_list), 2),
            "avg_latency_ms_skill": round(mean(m["latency_ms"] for m in skill_metrics_list), 2),
        }

        return EvalResult(
            release_id=release_id,
            structure_score=struct,
            effect_score=effect,
            objective_metrics=obj,
            p0_pass=p0_pass,
            case_verdicts=case_verdicts,
            case_outputs=case_outputs,
        )

    # ----------------- 辅助 -----------------

    def _load_cases(self, eval_set: str, skill_name: str) -> list[dict]:
        """从 evaluation_sets/<name>.json 加载 skill 相关的 case"""
        repo_root = self.registry.repo_root
        path = repo_root / "evaluation_sets" / f"{eval_set}.json"
        if not path.exists():
            raise FileNotFoundError(f"评估集不存在：{path}")
        import json
        data = json.loads(path.read_text(encoding="utf-8"))
        cases = [c for c in data.get("cases", []) if c.get("skill") == skill_name]
        return cases

    def _load_p0_ids(self) -> list[str]:
        """加载 p0_cases.json 的 case ID 列表"""
        path = self.registry.repo_root / "evaluation_sets" / "p0_cases.json"
        if not path.exists():
            return []
        import json
        data = json.loads(path.read_text(encoding="utf-8"))
        return data.get("p0_ids", [])

    def _run_bare(self, query: str) -> tuple[str, dict]:
        """无 Skill 的 Agent 跑 baseline"""
        started = time.perf_counter()
        messages = [{"role": "user", "content": query}]
        resp = self.llm.invoke(messages)
        latency_ms = (time.perf_counter() - started) * 1000

        content = str(getattr(resp, "content", resp) or "")
        usage = self._extract_usage(resp)
        run_log = {
            "messages": messages + [{"role": "assistant", "content": content}],
            "usage": usage,
            "latency_ms": latency_ms,
        }
        return content, collect_objective_metrics(run_log)

    def _run_with_skill(self, query: str, body: str) -> tuple[str, dict]:
        """有 Skill 的 Agent 跑：Skill Body 作为 system prompt"""
        started = time.perf_counter()
        messages = [
            {"role": "system", "content": DEFAULT_SYSTEM_PROMPT_HEADER + body},
            {"role": "user", "content": query},
        ]
        resp = self.llm.invoke(messages)
        latency_ms = (time.perf_counter() - started) * 1000

        content = str(getattr(resp, "content", resp) or "")
        usage = self._extract_usage(resp)
        run_log = {
            "messages": messages + [{"role": "assistant", "content": content}],
            "usage": usage,
            "latency_ms": latency_ms,
        }
        return content, collect_objective_metrics(run_log)

    @staticmethod
    def _extract_usage(resp) -> dict:
        u = getattr(resp, "usage", None)
        if u is None:
            return {}
        if isinstance(u, dict):
            return u
        # LLMResponse.usage 可能是自定义对象；转 dict 取常见字段
        return {
            "prompt_tokens": getattr(u, "prompt_tokens", 0),
            "completion_tokens": getattr(u, "completion_tokens", 0),
            "total_tokens": getattr(u, "total_tokens", 0),
        }

    @staticmethod
    def _dim_score(case_verdicts: list[tuple[str, str]], max_score: float) -> float:
        """配对判定 → 维度分：胜 1 / 平 0.5 / 负 0，加权到 max_score"""
        if not case_verdicts:
            return 0.0
        n = len(case_verdicts)
        weighted = sum(
            1.0 if v == "A_better" else 0.5 if v == "tied" else 0.0
            for _, v in case_verdicts
        )
        return round(weighted / n * max_score, 2)

    @staticmethod
    def _efficiency_score(base_metrics: list[dict], skill_metrics: list[dict]) -> float:
        """效率维度：skill token 平均 / baseline token 平均
           ratio=1 → 10；ratio=2 → 5；ratio=3 → 0（clamp [0, 10]）
           无有效数据 → 5（居中）
        """
        base_tokens = [m["tokens"] for m in base_metrics if m["tokens"] > 0]
        skill_tokens = [m["tokens"] for m in skill_metrics if m["tokens"] > 0]
        if not base_tokens or not skill_tokens:
            return 5.0
        ratio = mean(skill_tokens) / mean(base_tokens)
        score = 10.0 - 5.0 * (ratio - 1.0)
        return round(max(0.0, min(10.0, score)), 2)


__all__ = ["SkillEvaluator"]
