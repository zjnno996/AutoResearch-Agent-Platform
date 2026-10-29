"""Deterministic checks of declared experiment evaluation protocols."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


def _utcnow_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def audit_evaluation_protocol(run_dir: Path, stage_dir: Path) -> dict[str, Any]:
    """Check that the planned multi-seed protocol is reflected in results.

    This is intentionally deterministic and runs after result analysis.  It
    does not reject a smoke test, but it makes missing seeds/statistics
    explicit so later writing and the UI cannot present a single run as a
    rigorous evaluation.
    """
    plan: dict[str, Any] = {}
    for candidate in (run_dir / "stage-09" / "exp_plan.yaml", run_dir / "stage-09" / "exp_plan.json"):
        if not candidate.exists():
            continue
        try:
            if candidate.suffix == ".yaml":
                import yaml as _yaml
                loaded = _yaml.safe_load(candidate.read_text(encoding="utf-8"))
            else:
                loaded = json.loads(candidate.read_text(encoding="utf-8"))
            if isinstance(loaded, dict):
                plan = loaded
                break
        except Exception:  # noqa: BLE001
            continue
    protocol = plan.get("evaluation_protocol", {}) if isinstance(plan, dict) else {}
    if not isinstance(protocol, dict):
        protocol = {}
    expected = protocol.get("independent_seeds", [11, 29, 47])
    expected_count = protocol.get("minimum_seeds_per_condition", len(expected) if isinstance(expected, list) else 3)
    protocol_errors = []
    if type(expected_count) is not int or expected_count < 1:
        protocol_errors.append("minimum_seeds_per_condition must be a positive integer")
        expected_count = 3
    if not isinstance(expected, list) or len({str(seed) for seed in expected}) != len(expected):
        protocol_errors.append("independent_seeds must be a list of distinct seeds")
    required_reports = {
        str(item).strip().lower()
        for item in protocol.get("report", [])
        if str(item).strip()
    }
    paired_config = protocol.get("paired_comparison", {})
    paired_required = bool(
        isinstance(paired_config, dict) and paired_config.get("required")
    )
    summary_path = stage_dir / "experiment_summary.json"
    summary: dict[str, Any] = {}
    try:
        loaded = json.loads(summary_path.read_text(encoding="utf-8"))
        if isinstance(loaded, dict):
            summary = loaded
    except (OSError, ValueError, TypeError, json.JSONDecodeError):
        pass
    conditions = summary.get("condition_summaries", {})
    if not isinstance(conditions, dict):
        conditions = {}
    rows: list[dict[str, Any]] = []
    missing: list[str] = []
    from researchclaw.orchestration.artifacts import finite_numbers, has_nonfinite
    for name, data in conditions.items():
        if not isinstance(data, dict):
            continue
        claimed = data.get("n_seeds") or data.get("n_seed_metrics") or data.get("seed_count")
        raw_seeds = data.get("seed_metrics")
        values = list(raw_seeds.values()) if isinstance(raw_seeds, dict) else raw_seeds if isinstance(raw_seeds, list) else []
        valid_values = [item for item in values if finite_numbers(item) and not has_nonfinite(item)]
        n_int = len(valid_values)
        metrics = data.get("metrics", {}) if isinstance(data.get("metrics"), dict) else {}
        available_keys = {str(k).lower() for k in (*data.keys(), *metrics.keys())}
        row = {"condition": str(name), "observed_seeds": n_int, "claimed_seeds": claimed,
               "raw_seed_metrics_present": bool(valid_values), "minimum_seeds": expected_count,
               # Deterministic summaries store the mean under the bare metric
               # name (for example ``f1_macro``), with std/CI as suffixed keys.
               "has_mean": bool(metrics) or any(
                   k in available_keys or any(key.endswith(f"_{k}") for key in available_keys)
                   for k in ("mean", "mean_metric", "metrics_mean")
               ),
               "has_std": any(k in available_keys or any(key.endswith(f"_{k}") for key in available_keys)
                              for k in ("std", "std_metric", "metrics_std", "standard_deviation")),
               "has_ci95": any(k in available_keys or any(key.endswith(f"_{k}") for key in available_keys)
                               for k in ("ci95_low", "ci95", "confidence_interval"))}
        rows.append(row)
        report_complete = (
            ("mean" not in required_reports or row["has_mean"])
            and ("standard_deviation" not in required_reports or row["has_std"])
            and ("95%_confidence_interval" not in required_reports or row["has_ci95"])
        )
        if n_int < expected_count or not report_complete:
            missing.append(str(name))
    paired_present = bool(summary.get("paired_comparisons"))
    status = "passed" if rows and not missing and not protocol_errors and (paired_present or not paired_required) else ("insufficient_evidence" if rows else "not_available")
    report = {
        "schema_version": "evaluation-protocol-audit-v1",
        "status": status,
        "planned_seeds": expected,
        "minimum_seeds_per_condition": expected_count,
        "protocol_errors": protocol_errors,
        "conditions": rows,
        "missing_seed_conditions": missing,
        "paired_comparisons_present": paired_present,
        "raw_seed_metrics_present": any(r["observed_seeds"] > 0 for r in rows),
        # Passing this audit confirms protocol completeness only. Claim scope is
        # still governed by experiment provenance and research_readiness.json.
        "writing_policy": (
            "defer_to_research_readiness" if status == "passed"
            else "limited_claims_only"
        ),
        "user_facing_status_zh": (
            "评估协议已满足多 seed、汇总统计和逐条件证据要求。" if status == "passed"
            else "评估证据不足：部分条件未达到最少 seed 数，最终稿只能使用受限结论。"
        ),
        "generated": _utcnow_iso(),
    }
    from researchclaw.orchestration.artifacts import write_json
    write_json(stage_dir / "evaluation_protocol_audit.json", report)
    return report
