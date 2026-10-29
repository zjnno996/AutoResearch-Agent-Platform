"""Use the same formal dimensions for UI, streams, exports and history."""
from __future__ import annotations

from math import floor
from typing import Any


def review_totals(results: list[dict[str, Any]]) -> tuple[int, int]:
    formal = [row for row in results if not row.get("diagnostic_only")
              and row.get("dimensionId") not in {"deep_dive", "patch"}]
    if not formal:
        return 0, 0
    # Match the UI's Math.round for the nonnegative review score scale.
    return floor(sum(row["score"] for row in formal) / len(formal) + 0.5), len(formal)


def normalize_history_totals(record: dict[str, Any]) -> dict[str, Any]:
    if isinstance(record.get("results"), list):
        score, count = review_totals(record["results"])
        return {**record, "overallScore": score, "dimensionCount": count}
    return record
