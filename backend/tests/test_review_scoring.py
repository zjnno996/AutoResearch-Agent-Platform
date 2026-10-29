import json

import pytest

from review_engine import reviewer
from review_engine.scoring import review_totals


@pytest.mark.parametrize("scores,expected", [([], (0, 0)), ([80, 81], (81, 2)), ([42], (42, 1))])
def test_formal_scores_match_frontend_rounding_and_ignore_diagnostics(scores, expected):
    rows = [{"dimensionId": f"formal-{i}", "score": value} for i, value in enumerate(scores)]
    rows += [{"dimensionId": "deep_dive", "score": 50},
             {"dimensionId": "patch", "score": 50, "diagnostic_only": True}]
    assert review_totals(rows) == expected


def test_history_and_search_use_formal_totals_without_changing_model_results(tmp_path, monkeypatch):
    monkeypatch.setattr(reviewer, "HISTORY_DIR", tmp_path)
    monkeypatch.setattr(reviewer, "_SEARCH_INDEX", None)
    assert reviewer.search_review_history() == []
    rows = [{"dimensionId": "methodology", "score": 42},
            {"dimensionId": "deep_dive", "score": 50, "diagnostic_only": True},
            {"dimensionId": "patch", "score": 50, "diagnostic_only": True}]
    id = reviewer.save_review("integration.pdf", "Qwen3.8-27B-FP8", ["methodology"], rows)
    record = reviewer.load_review_by_id(id)
    assert record["overallScore"] == 42 and record["dimensionCount"] == 1
    assert record["results"] == rows
    # Old stored aggregates are corrected at read time; raw model results stay.
    path = tmp_path / f"{id}.json"
    record.update(overallScore=47, dimensionCount=3)
    path.write_text(json.dumps(record))
    assert reviewer.load_review_history()[0]["overallScore"] == 42
    assert reviewer.load_review_by_id(id)["dimensionCount"] == 1
    assert reviewer.search_review_history()[0]["overallScore"] == 42
    assert reviewer.delete_review(id)
    assert reviewer.search_review_history() == []


def test_stream_envelope_uses_formal_dimension_count(monkeypatch):
    rows = [{"dimensionId": "methodology", "score": 42},
            {"dimensionId": "deep_dive", "score": 50, "diagnostic_only": True}]
    monkeypatch.setattr(reviewer, "run_review", lambda *a, **kw: (rows, {}, None))
    queue = reviewer.run_review_streaming("test input", "input.pdf", ["methodology"])
    assert queue.get(timeout=5)["type"] == "started"
    event = queue.get(timeout=5)
    assert event["type"] == "complete" and event["overallScore"] == 42 and event["dimensionCount"] == 1
    assert event["results"] == rows
