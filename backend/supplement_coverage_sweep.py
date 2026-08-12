"""Run only the expert-gap sweep, verify it, and merge into a v6 review cache."""

from __future__ import annotations

import json
import sys
from pathlib import Path

from review_data.issue_patterns import semantic_similarity
from review_engine.consensus import run_consensus_pipeline
from review_engine.evidence_map import build_evidence_map
from review_engine.llm_client import (
    _llm_clients,
    _llm_configs,
    get_client_for_model,
    register_config_section,
)
from review_engine.pdf_utils import _extract_pdf_text, _vision_extract_paper
from review_engine.reviewer import (
    _build_categorized_findings,
    _build_confidence_summary,
    _compact_issue_text,
    _normalize_outward_chinese,
    _retain_verified_findings,
    _run_expert_coverage_sweep,
)


ROOT = Path(__file__).resolve().parents[1]
PIPELINE = "qwen-vision-v7.2-hybrid-agents"
DATASETS = {
    "dataset1": {
        "key": "dataset1-IoT-Generation",
        "pdf": Path("/root/auto_review_dataset/dataset1/物联网应用的智能生成与优化关键技术研究.pdf"),
    },
    "dataset3": {
        "key": "dataset3-Edge-Computing",
        "pdf": Path("/root/auto_review_dataset/dataset3/hjm_毕业论文_2026_v3.1.pdf"),
    },
}


def _key_findings(findings: list[dict]) -> dict:
    return {
        "weaknesses": [
            {
                "text": item.get("text", ""),
                "severity": item.get("severity", "major"),
                "dimensionId": item.get("dimension", ""),
                "evidence": item.get("evidence", ""),
                "confidence": item.get("evidence_confidence", 0.0),
                "priorityScore": item.get("priority_score", 0.0),
            }
            for item in findings[:5]
        ],
        "suggestions": [
            {
                "text": item.get("suggestion", ""),
                "dimensionId": item.get("dimension", ""),
                "priorityScore": item.get("priority_score", 0.0),
            }
            for item in findings
            if item.get("suggestion")
        ][:5],
    }


def _clean_supplemental_findings(findings: list[dict]) -> list[dict]:
    """Keep the most specific/high-confidence item in main-vs-supplement overlaps."""
    for item in findings:
        item["text"] = _compact_issue_text(str(item.get("text", "")))
    original = [
        item for item in findings
        if not str(item.get("candidate_id", "")).startswith("S")
    ]
    supplements = [
        item for item in findings
        if str(item.get("candidate_id", "")).startswith("S")
    ]
    retained: list[dict] = []

    def quality(item: dict) -> float:
        text = str(item.get("text", ""))
        confidence = float(item.get("evidence_confidence", 0.0))
        locator_bonus = 0.05 if any(
            marker in text for marker in ("§", "第", "图", "表", "Page")
        ) else 0.0
        return confidence + min(len(text), 220) / 1000 + locator_bonus

    for item in supplements:
        overlaps = [
            old for old in original + retained
            if semantic_similarity(
                str(item.get("text", "")), str(old.get("text", "")),
            ) >= 0.30
        ]
        if overlaps and quality(item) <= max(quality(old) for old in overlaps) + 0.02:
            continue
        for old in overlaps:
            if old in original:
                original.remove(old)
            elif old in retained:
                retained.remove(old)
        retained.append(item)
    for index, item in enumerate(retained, 1):
        item["candidate_id"] = f"S{index:03d}"
    return sorted(
        original + retained,
        key=lambda item: float(item.get("priority_score", 0.0)),
        reverse=True,
    )


def supplement(dataset: str, model: str) -> None:
    spec = DATASETS[dataset]
    cache = (
        ROOT / "backend" / "eval_cache" / "thesis" / "auto_reviews"
        / f"{spec['key']}-{PIPELINE}.json"
    )
    review = json.loads(cache.read_text(encoding="utf-8"))
    raw = spec["pdf"].read_bytes()
    paper_text = _extract_pdf_text(raw)
    visual_text, _ = _vision_extract_paper(raw, spec["pdf"].name)
    evidence_map = build_evidence_map(paper_text, visual_text or "")
    facts = {"evidence_map": evidence_map.to_prompt(max_chars=16000)}

    issues = _run_expert_coverage_sweep(
        paper_text + (visual_text or ""),
        review.get("dim_results", []),
        facts=facts,
        model=model,
    )
    print(f"[{dataset}] sweep candidates: {len(issues)}", flush=True)
    review["meta"]["coverage_sweep_candidates"] = len(issues)
    review["meta"].setdefault("coverage_sweep_runs", []).append({
        "candidates": issues,
    })
    if not issues:
        cache.write_text(json.dumps(review, ensure_ascii=False, indent=1), encoding="utf-8")
        return

    pseudo_results: list[dict] = []
    for dimension in sorted({str(issue["dimension"]) for issue in issues}):
        selected = [issue for issue in issues if issue["dimension"] == dimension]
        pseudo_results.append({
            "dimensionId": dimension,
            "score": 50,
            "strengths": [],
            "weaknesses": [issue["weakness"] for issue in selected],
            "suggestions": [issue["suggestion"] for issue in selected],
        })

    client = get_client_for_model(model)
    supplement_result = run_consensus_pipeline(
        results=pseudo_results,
        evidence_map=evidence_map,
        client=client,
        model=model,
        target_paper_text=paper_text,
        enable_debate=True,
        max_debates=2,
        min_confidence=0.55,
    )
    new_findings = _normalize_outward_chinese(
        supplement_result.get("verifiedFindings", []), client, model,
    )
    existing = list(review.get("verifiedFindings", []))
    retained: list[dict] = []
    for item in new_findings:
        if any(
            semantic_similarity(str(item.get("text", "")), str(old.get("text", ""))) >= 0.30
            for old in existing + retained
        ):
            continue
        item["candidate_id"] = f"S{len(retained) + 1:03d}"
        retained.append(item)

    combined = _clean_supplemental_findings(existing + retained)
    review["verifiedFindings"] = combined
    review["dim_results"] = _retain_verified_findings(
        review.get("dim_results", []), combined,
    )
    meta = review["meta"]
    meta["verifiedFindings"] = combined
    meta["coverage_sweep_retained"] = int(meta.get("coverage_sweep_retained", 0)) + len(retained)
    meta["coverage_sweep_metrics"] = supplement_result.get("metrics", {})
    meta["keyFindings"] = _key_findings(combined)
    meta["categorizedFindings"] = _build_categorized_findings(
        review["dim_results"], combined, meta,
    )
    meta["confidenceSummary"] = _build_confidence_summary(
        combined, meta.get("consensusMetrics", {}), meta,
    )
    review["keyFindings"] = meta["keyFindings"]
    review["categorizedFindings"] = meta["categorizedFindings"]
    review["confidenceSummary"] = meta["confidenceSummary"]
    cache.write_text(json.dumps(review, ensure_ascii=False, indent=1), encoding="utf-8")
    print(
        f"[{dataset}] retained {len(retained)} new findings; total={len(combined)}",
        flush=True,
    )


def clean_cache(dataset: str) -> None:
    """Apply deterministic supplemental deduplication without another LLM call."""
    spec = DATASETS[dataset]
    cache = (
        ROOT / "backend" / "eval_cache" / "thesis" / "auto_reviews"
        / f"{spec['key']}-{PIPELINE}.json"
    )
    review = json.loads(cache.read_text(encoding="utf-8"))
    before = list(review.get("verifiedFindings", []))
    combined = _clean_supplemental_findings(before)
    review["verifiedFindings"] = combined
    review["dim_results"] = _retain_verified_findings(
        review.get("dim_results", []), combined,
    )
    meta = review["meta"]
    meta["verifiedFindings"] = combined
    meta["coverage_sweep_retained"] = sum(
        str(item.get("candidate_id", "")).startswith("S") for item in combined
    )
    meta["keyFindings"] = _key_findings(combined)
    meta["categorizedFindings"] = _build_categorized_findings(
        review["dim_results"], combined, meta,
    )
    meta["confidenceSummary"] = _build_confidence_summary(
        combined, meta.get("consensusMetrics", {}), meta,
    )
    review["keyFindings"] = meta["keyFindings"]
    review["categorizedFindings"] = meta["categorizedFindings"]
    review["confidenceSummary"] = meta["confidenceSummary"]
    cache.write_text(json.dumps(review, ensure_ascii=False, indent=1), encoding="utf-8")
    coverage_path = (
        ROOT / "backend" / "eval_cache" / "thesis" / "coverage"
        / f"{spec['key']}-{PIPELINE}.json"
    )
    if coverage_path.exists():
        coverage = json.loads(coverage_path.read_text(encoding="utf-8"))
        by_id = {
            str(item.get("candidate_id", "")): item
            for item in combined
            if item.get("candidate_id")
        }
        for group_name in (
            "auto_points", "matched_auto_points", "useful_unmatched_auto_points",
            "unhelpful_unmatched_auto_points",
        ):
            for point in coverage.get(group_name, []) or []:
                finding = by_id.get(str(point.get("candidate_id", "")))
                if not finding:
                    continue
                if point.get("type") == "suggestion":
                    point["text"] = str(finding.get("suggestion", point.get("text", "")))
                else:
                    point["text"] = str(finding.get("text", point.get("text", "")))
                point["evidence"] = str(finding.get("evidence", point.get("evidence", "")))
        coverage_path.write_text(
            json.dumps(coverage, ensure_ascii=False, indent=1),
            encoding="utf-8",
        )
    print(f"[{dataset}] cleaned {len(before) - len(combined)} duplicates; total={len(combined)}")


def main() -> None:
    clean_only = "--clean-only" in sys.argv
    targets = [arg for arg in sys.argv[1:] if arg != "--clean-only"] or ["dataset1", "dataset3"]
    for name in targets:
        if name not in DATASETS:
            raise SystemExit(f"Unknown dataset: {name}")
    if clean_only:
        for name in targets:
            clean_cache(name)
        return
    qwen = "Qwen3.5-122B-A10B-FP8"
    if qwen in _llm_configs:
        del _llm_configs[qwen]
    if qwen in _llm_clients:
        del _llm_clients[qwen]
    model = register_config_section("llm")
    for name in targets:
        supplement(name, model)


if __name__ == "__main__":
    main()
