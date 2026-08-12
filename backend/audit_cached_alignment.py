"""Run strict second-pass recall audit on existing expert-alignment caches."""

from __future__ import annotations

import json
import sys
from pathlib import Path

from evaluate_thesis import _run_alignment_recall_audit
from review_engine.alignment import classify_alignment
from review_engine.llm_client import (
    _llm_clients,
    _llm_configs,
    register_config_section,
)


ROOT = Path(__file__).resolve().parent / "eval_cache" / "thesis"
PIPELINE = "qwen-vision-v7.2-hybrid-agents"
DATASETS = {
    "dataset1": "dataset1-IoT-Generation",
    "dataset3": "dataset3-Edge-Computing",
}


def _existing_usefulness_judgments(coverage: dict) -> list[dict]:
    judgments: list[dict] = []
    for useful, group_name in (
        (True, "useful_unmatched_auto_points"),
        (False, "unhelpful_unmatched_auto_points"),
    ):
        for point in coverage.get(group_name, []) or []:
            index = point.get("auto_index")
            if not isinstance(index, int):
                continue
            judgments.append({
                "auto_idx": index,
                "useful": useful,
                "confidence": float(point.get("usefulness_confidence", 1.0)),
                "reason": str(point.get("usefulness_reason", "")),
                "category": str(point.get("usefulness_category", "")),
            })
    return judgments


def audit(dataset: str, force: bool = False) -> None:
    key = DATASETS[dataset]
    path = ROOT / "coverage" / f"{key}-{PIPELINE}.json"
    coverage = json.loads(path.read_text(encoding="utf-8"))
    prior_audit = coverage.get("recall_audit") or {}
    if prior_audit.get("enabled") and not force:
        print(
            f"[{dataset}] cached audit: recovered={prior_audit.get('recovered', 0)}",
            flush=True,
        )
        return

    human = coverage.get("human_points", [])
    auto = coverage.get("auto_points", [])
    matches = list(coverage.get("matches", []))
    initial_covered = len({
        item.get("human_index") for item in matches if item.get("covered")
    })
    matches, metrics = _run_alignment_recall_audit(human, auto, matches)
    alignment = classify_alignment(
        human,
        auto,
        matches,
        usefulness_judgments=_existing_usefulness_judgments(coverage),
    )
    covered = len({
        item.get("human_index") for item in matches if item.get("covered")
    })
    coverage.update(alignment)
    coverage["matches"] = matches
    coverage["covered_count"] = covered
    coverage["not_covered_count"] = len(human) - covered
    coverage["coverage_pct"] = round(covered / max(len(human), 1) * 100, 1)
    coverage["recall_audit"] = {
        **metrics,
        "initial_covered": initial_covered,
        "final_covered": covered,
    }
    path.write_text(json.dumps(coverage, ensure_ascii=False, indent=1), encoding="utf-8")
    print(
        f"[{dataset}] strict audit: {initial_covered} -> {covered}; "
        f"recovered={metrics.get('recovered', 0)}, rejected={metrics.get('rejected', 0)}",
        flush=True,
    )


def main() -> None:
    force = "--force" in sys.argv
    targets = [arg for arg in sys.argv[1:] if not arg.startswith("--")] or list(DATASETS)
    for target in targets:
        if target not in DATASETS:
            raise SystemExit(f"Unknown dataset: {target}")
    qwen = "Qwen3.5-122B-A10B-FP8"
    if qwen in _llm_configs:
        del _llm_configs[qwen]
    if qwen in _llm_clients:
        del _llm_clients[qwen]
    register_config_section("llm")
    for target in targets:
        audit(target, force=force)


if __name__ == "__main__":
    main()
