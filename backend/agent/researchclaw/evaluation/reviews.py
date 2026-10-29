"""Validate manuscript review contracts before they can affect quality gates."""

from __future__ import annotations

from datetime import datetime, timezone
from math import isfinite
from typing import Any


def unavailable_review(reason: str) -> dict[str, Any]:
    return {"score_1_to_10": 0.0, "verdict": "revise", "review_status": "unavailable",
            "review_error": reason, "criteria": {}, "strengths": [],
            "weaknesses": [reason],
            "required_actions": ["Complete an independent manuscript review against the retained evidence."],
            "generated": datetime.now(timezone.utc).isoformat(timespec="seconds")}


def validate_review(report: Any) -> dict[str, Any]:
    if not isinstance(report, dict):
        return unavailable_review("Manuscript reviewer did not return a JSON object")
    score = report.get("score_1_to_10")
    if type(score) not in (int, float) or not isfinite(score) or not 1 <= score <= 10:
        return unavailable_review("Manuscript reviewer returned no finite score in [1, 10]")
    verdict = str(report.get("verdict", "")).strip().lower()
    aliases = {"pass": "proceed", "passed": "proceed", "accept": "proceed", "accepted": "proceed",
               "fail": "reject", "failed": "reject", "revision": "revise"}
    verdict = aliases.get(verdict, verdict)
    if verdict not in ("proceed", "revise", "reject"):
        return unavailable_review("Manuscript reviewer returned no valid verdict")
    return {**report, "score_1_to_10": float(score), "verdict": verdict, "review_status": "completed"}
