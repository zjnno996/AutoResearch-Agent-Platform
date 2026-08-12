"""Audit the precision-first display gate on cached expert-alignment results."""

from __future__ import annotations

import argparse
import json
from collections import Counter
from pathlib import Path
from typing import Any

from review_engine.consensus import (
    CandidateIssue,
    candidate_display_decision,
    normalize_candidate_suggestions,
)


ROOT = Path(__file__).resolve().parent / "eval_cache" / "thesis"
DEFAULT_PIPELINE = "qwen-vision-v6-structured-recall"
DATASETS = {
    "dataset1": "dataset1-IoT-Generation",
    "dataset3": "dataset3-Edge-Computing",
}


def _candidate(payload: dict[str, Any]) -> CandidateIssue:
    return CandidateIssue(
        candidate_id=str(payload.get("candidate_id", "")),
        text=str(payload.get("text", "")),
        dimension=str(payload.get("dimension", "")),
        suggestion=str(payload.get("suggestion", "")),
        source_dimensions=list(payload.get("source_dimensions", []) or []),
        source_count=int(payload.get("source_count", 1) or 1),
        severity=str(payload.get("severity", "major")),
        evidence=str(payload.get("evidence", "")),
        evidence_confidence=float(payload.get("evidence_confidence", 0.0) or 0.0),
        verdict=str(payload.get("verdict", "uncertain")),
        dataset_prior=float(payload.get("dataset_prior", 0.0) or 0.0),
        debate=dict(payload.get("debate", {}) or {}),
        priority_score=float(payload.get("priority_score", 0.0) or 0.0),
        issue_category=str(payload.get("issue_category", "other")),
    )


def audit(dataset: str, pipeline: str, min_confidence: float) -> dict[str, Any]:
    key = DATASETS[dataset]
    auto_path = ROOT / "auto_reviews" / f"{key}-{pipeline}.json"
    coverage_path = ROOT / "coverage" / f"{key}-{pipeline}.json"
    auto = json.loads(auto_path.read_text(encoding="utf-8"))
    coverage = json.loads(coverage_path.read_text(encoding="utf-8"))

    candidates = [_candidate(item) for item in (auto.get("verifiedFindings", []) or [])]
    suggestions_replaced = normalize_candidate_suggestions(candidates)
    decisions = {
        candidate.candidate_id: candidate_display_decision(candidate, min_confidence)
        for candidate in candidates
    }
    retained_ids = {
        candidate_id for candidate_id, (keep, _) in decisions.items() if keep
    }
    reasons = Counter(reason for keep, reason in decisions.values() if not keep)

    auto_points = coverage.get("auto_points", []) or []

    def keep_auto_point(index: int) -> bool:
        point = auto_points[index]
        if str(point.get("type", "")) == "strength":
            return True
        candidate_id = str(point.get("candidate_id", ""))
        return bool(candidate_id and candidate_id in retained_ids)

    covered_matches = [
        match for match in (coverage.get("matches", []) or [])
        if match.get("covered") and isinstance(match.get("auto_index"), int)
    ]
    covered_human_before = {
        int(match["human_index"]) for match in covered_matches
        if isinstance(match.get("human_index"), int)
    }
    covered_human_after = {
        int(match["human_index"]) for match in covered_matches
        if isinstance(match.get("human_index"), int)
        and keep_auto_point(int(match["auto_index"]))
    }

    def retained_group(name: str) -> int:
        return sum(
            isinstance(item.get("auto_index"), int)
            and keep_auto_point(int(item["auto_index"]))
            for item in (coverage.get(name, []) or [])
        )

    retained_auto_points = sum(keep_auto_point(index) for index in range(len(auto_points)))
    useful_after = retained_group("useful_unmatched_auto_points")
    unhelpful_after = retained_group("unhelpful_unmatched_auto_points")
    matched_auto_after = len({
        int(match["auto_index"]) for match in covered_matches
        if keep_auto_point(int(match["auto_index"]))
    })
    precision_proxy = round(
        (matched_auto_after + useful_after) /
        max(matched_auto_after + useful_after + unhelpful_after, 1) * 100,
        1,
    )

    return {
        "dataset": dataset,
        "pipeline": pipeline,
        "gate_min_confidence": min_confidence,
        "verified_findings_before": len(candidates),
        "verified_findings_after": len(retained_ids),
        "suggestions_replaced": suggestions_replaced,
        "filtered_reasons": dict(reasons),
        "expert_human_points_covered_before": len(covered_human_before),
        "expert_human_points_covered_after": len(covered_human_after),
        "expert_coverage_after_pct": round(
            len(covered_human_after) / max(int(coverage.get("total_human_points", 0)), 1) * 100,
            1,
        ),
        "auto_points_before": len(auto_points),
        "auto_points_after": retained_auto_points,
        "matched_auto_points_after": matched_auto_after,
        "useful_unmatched_after": useful_after,
        "unhelpful_unmatched_after": unhelpful_after,
        "useful_precision_proxy_pct": precision_proxy,
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("datasets", nargs="*", choices=sorted(DATASETS), default=list(DATASETS))
    parser.add_argument("--pipeline", default=DEFAULT_PIPELINE)
    parser.add_argument("--min-confidence", type=float, default=0.65)
    args = parser.parse_args()
    datasets = args.datasets or list(DATASETS)
    print(json.dumps(
        [audit(dataset, args.pipeline, args.min_confidence) for dataset in datasets],
        ensure_ascii=False,
        indent=2,
    ))


if __name__ == "__main__":
    main()
