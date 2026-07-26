"""Deterministic ranking metrics for controlled retrieval regression gates."""

from __future__ import annotations

from typing import Any, Mapping, Sequence


def evaluate_rankings(cases: Sequence[Mapping[str, Any]], rankings: Mapping[str, Sequence[str]]) -> dict[str, float | int]:
    if not cases:
        raise ValueError("evaluation set must not be empty")
    recall = 0.0
    reciprocal_rank = 0.0
    forbidden_hits = 0
    returned = 0
    for case in cases:
        case_id = str(case.get("case_id") or "").strip()
        expected = {str(item) for item in case.get("expected_source_refs") or []}
        forbidden = {str(item) for item in case.get("forbidden_source_refs") or []}
        if not case_id or not expected:
            raise ValueError("each evaluation case requires id and expected sources")
        values = [str(item) for item in rankings.get(case_id, [])]
        positions = [index for index, item in enumerate(values, 1) if item in expected]
        recall += len(set(values) & expected) / len(expected)
        reciprocal_rank += 1.0 / positions[0] if positions else 0.0
        forbidden_hits += sum(item in forbidden for item in values)
        returned += len(values)
    count = len(cases)
    return {
        "cases": count,
        "recall_at_k": round(recall / count, 6),
        "mrr": round(reciprocal_rank / count, 6),
        "forbidden_hits": forbidden_hits,
        "returned": returned,
        "forbidden_hit_rate": round(forbidden_hits / returned, 6) if returned else 0.0,
    }


__all__ = ["evaluate_rankings"]
