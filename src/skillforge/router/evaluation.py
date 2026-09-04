"""Deterministic evaluation helpers shared by CLI and patch validation."""
from __future__ import annotations


def route_with_topk(router, query: str, top_k: int = 3):
    """Return ``(chosen, top_k_names, hit_layer)`` for one router query."""
    result = router.route(query)
    embed_scores = result.scores.get("embed", {}) if isinstance(result.scores, dict) else {}
    ranked = sorted(embed_scores.items(), key=lambda item: -item[1])
    top_k_names = [name for name, _ in ranked[:top_k]]
    if result.chosen and result.chosen not in top_k_names:
        top_k_names = [result.chosen] + top_k_names[: top_k - 1]
    return result.chosen, top_k_names, result.hit_layer


def evaluate_router_cases(cases: list[dict], router, top_k: int = 3) -> dict:
    """Evaluate routing outcomes without printing or mutating repository state."""
    ok_at_1 = 0
    ok_at_3 = 0
    retrieval_ok_at_1 = 0
    per_type: dict[str, dict[str, int]] = {}
    case_results: list[dict] = []

    for case in cases:
        expected = case["expected"]
        chosen, top_names, layer = route_with_topk(router, case["query"], top_k=top_k)
        if expected is None:
            r1 = chosen is None
            r3 = chosen is None
            retrieval_r1 = r1
        else:
            r1 = chosen == expected
            r3 = expected in top_names
            retrieval_r1 = bool(top_names) and top_names[0] == expected

        ok_at_1 += int(r1)
        ok_at_3 += int(r3)
        retrieval_ok_at_1 += int(retrieval_r1)
        stats = per_type.setdefault(case["type"], {"total": 0, "r1": 0, "r3": 0})
        stats["total"] += 1
        stats["r1"] += int(r1)
        stats["r3"] += int(r3)
        case_results.append(
            {
                "id": case["id"],
                "type": case["type"],
                "expected": expected,
                "chosen": chosen,
                "hit_layer": layer,
                "top3": top_names,
                "r1": r1,
                "r3": r3,
                "retrieval_r1": retrieval_r1,
            }
        )

    total = len(cases)
    return {
        "total": total,
        "recall_at_1": ok_at_1 / total if total else 0.0,
        "recall_at_3": ok_at_3 / total if total else 0.0,
        "retrieval_at_1": retrieval_ok_at_1 / total if total else 0.0,
        "per_type": per_type,
        "case_results": case_results,
        "fails": [result for result in case_results if not result["r1"]],
    }


# Backward-compatible name used by scripts/eval_router.py.
evaluate = evaluate_router_cases
