from __future__ import annotations

import pytest

from review_engine.alignment import (
    classify_alignment,
    flatten_confident_auto_points,
    select_auto_candidate_indices,
    validate_recall_audit_match,
)


def test_coverage_judge_failure_is_not_reported_as_zero_percent(
    tmp_path, monkeypatch,
) -> None:
    import evaluate_thesis

    monkeypatch.setattr(evaluate_thesis, "COVERAGE_CACHE_DIR", tmp_path)
    monkeypatch.setattr(
        evaluate_thesis,
        "_flatten_auto_points",
        lambda _: [{"type": "weakness", "dimension": "experiment", "text": "表3.1缺少误差棒"}],
    )
    monkeypatch.setattr(
        evaluate_thesis,
        "_llm_json",
        lambda *args, **kwargs: (_ for _ in ()).throw(ConnectionError("Qwen unavailable")),
    )
    human = {
        "forum": "dataset-test", "title": "test",
        "reviews": [{
            "reviewer_num": 1,
            "points": [{"type": "weakness", "text": "表3.1没有误差棒"}],
        }],
    }

    with pytest.raises(RuntimeError, match="Coverage judge incomplete"):
        evaluate_thesis.compute_coverage(
            human, {"pipeline_version": "v8"}, force=True,
        )

    assert not list(tmp_path.glob("*.json"))


def test_flatten_auto_points_keeps_only_confident_findings_and_cited_strengths() -> None:
    auto_result = {
        "verifiedFindings": [
            {
                "candidate_id": "C001",
                "dimension": "experiment",
                "text": "表3.1只报告单次运行，缺少误差棒。",
                "suggestion": "在表3.1中补充均值、方差和多随机种子结果。",
                "evidence_confidence": 0.84,
                "verdict": "supported",
                "evidence": "表3.1",
            },
            {
                "candidate_id": "C002",
                "dimension": "writing",
                "text": "写作可能不够清晰。",
                "evidence_confidence": 0.42,
                "verdict": "uncertain",
            },
        ],
        "dim_results": [
            {
                "dimensionId": "novelty",
                "strengths": [
                    "§2.3明确比较了本文方法与现有工作的差异。",
                    "论文很有趣。",
                ],
            }
        ],
    }
    points = flatten_confident_auto_points(auto_result, min_confidence=0.55)
    texts = [point["text"] for point in points]
    assert "表3.1只报告单次运行，缺少误差棒。" in texts
    assert "在表3.1中补充均值、方差和多随机种子结果。" in texts
    assert "§2.3明确比较了本文方法与现有工作的差异。" in texts
    assert "写作可能不够清晰。" not in texts
    assert "论文很有趣。" not in texts


def test_alignment_splits_matched_useful_unmatched_unhelpful_and_missed() -> None:
    human = [
        {"type": "weakness", "text": "表3.1缺少误差棒"},
        {"type": "weakness", "text": "第4章理论证明不完整"},
    ]
    auto = [
        {
            "type": "weakness",
            "text": "表3.1只报告单次运行，缺少误差棒。",
            "dimension": "experiment",
            "confidence": 0.84,
            "evidence": "表3.1",
        },
        {
            "type": "weakness",
            "text": "图5.2未覆盖低带宽场景。",
            "dimension": "experiment",
            "confidence": 0.79,
            "evidence": "图5.2",
        },
        {
            "type": "suggestion",
            "text": "建议进一步改善论文。",
            "dimension": "writing",
            "confidence": 0.6,
        },
    ]
    matches = [{"human_index": 0, "auto_index": 0, "covered": True}]
    judgments = [
        {
            "auto_idx": 1,
            "useful": True,
            "confidence": 0.9,
            "reason": "new evidence-backed robustness issue",
            "category": "novel_issue",
        },
        {
            "auto_idx": 2,
            "useful": True,
            "confidence": 0.4,
            "reason": "generic and low confidence",
            "category": "generic",
        },
    ]
    result = classify_alignment(human, auto, matches, judgments)
    assert result["counts"] == {
        "matched": 1,
        "useful_unmatched": 1,
        "unhelpful_unmatched": 1,
        "missed_human": 1,
    }
    assert result["useful_unmatched_auto_points"][0]["auto_index"] == 1
    assert result["unhelpful_unmatched_auto_points"][0]["auto_index"] == 2
    assert result["missed_human_points"][0]["human_index"] == 1


def test_readable_report_names_all_four_alignment_groups() -> None:
    from evaluate_thesis import format_comparison

    human_points = [
        {"reviewer_num": 1, "type": "weakness", "text": "表3.1缺少误差棒"},
        {"reviewer_num": 1, "type": "weakness", "text": "第4章证明不完整"},
    ]
    auto_points = [
        {"type": "weakness", "text": "表3.1缺少误差棒", "dimension": "experiment", "confidence": 0.84},
        {"type": "weakness", "text": "图5.2未覆盖低带宽场景", "dimension": "experiment", "confidence": 0.79},
        {"type": "suggestion", "text": "建议改善论文", "dimension": "writing", "confidence": 0.60},
    ]
    coverage = {
        "title": "Contract Test", "covered_count": 1,
        "total_human_points": 2, "coverage_pct": 50.0,
        "total_auto_points": 3, "human_points": human_points,
        "auto_points": auto_points,
        "matches": [{"human_index": 0, "auto_index": 0, "covered": True}],
        "counts": {"matched": 1, "useful_unmatched": 1, "unhelpful_unmatched": 1, "missed_human": 1},
        "useful_unmatched_auto_points": [{**auto_points[1], "usefulness_reason": "new evidence"}],
        "unhelpful_unmatched_auto_points": [{**auto_points[2], "usefulness_reason": "generic"}],
    }
    human_data = {"dataset_key": "dataset1", "name": "Contract Test", "reviews": [{"reviewer_num": 1, "evaluation": "test"}]}
    report = format_comparison(coverage, human_data, {"dim_results": []})
    assert "[✓]" in report
    assert "【覆盖缺口】" in report
    assert "【未对上但有用】" in report
    assert "【未对上且无效】" in report


def test_dataset3_reads_all_structured_expert_points_from_dataset_directory() -> None:
    from evaluate_thesis import read_xls_reviews

    human = read_xls_reviews("dataset3")
    points = [point for review in human["reviews"] for point in review["structured_points"]]
    assert len(human["reviews"]) == 5
    assert len(points) == 52
    assert sum(point["type"] == "strength" for point in points) == 25
    assert sum(point["type"] == "weakness" for point in points) == 15
    assert sum(point["type"] == "suggestion" for point in points) == 12


def test_point_type_compatibility_is_exact() -> None:
    from review_engine.alignment import point_types_compatible

    assert point_types_compatible({"type": "strength"}, {"type": "strength"})
    assert not point_types_compatible({"type": "strength"}, {"type": "weakness"})
    assert not point_types_compatible({"type": "suggestion"}, {"type": "weakness"})


def test_semantic_candidate_retrieval_keeps_global_indices_and_exact_types() -> None:
    human = [
        {"type": "weakness", "text": "表3.1没有报告误差棒和方差"},
        {"type": "suggestion", "text": "建议增加低带宽场景实验"},
    ]
    auto = [
        {"type": "strength", "text": "表3.1报告了较低时延", "confidence": 0.9},
        {"type": "weakness", "text": "图2结构不够清楚", "confidence": 0.7},
        {"type": "suggestion", "text": "补充不同带宽条件的鲁棒性测试", "confidence": 0.8},
        {"type": "weakness", "text": "表3.1仅给出均值，缺少方差与置信区间", "confidence": 0.85},
    ]
    indices = select_auto_candidate_indices(human, auto, limit=3)
    assert 2 in indices
    assert 3 in indices
    assert 0 not in indices


def test_recall_audit_requires_type_confidence_and_semantic_overlap() -> None:
    human = {"type": "weakness", "text": "表3.1缺少误差棒和置信区间"}
    auto = {"type": "weakness", "text": "表3.1未报告方差、误差棒与置信区间"}
    accepted, reason = validate_recall_audit_match(
        human, auto, {"covered": True, "confidence": 0.88},
    )
    assert accepted and reason == "accepted"

    accepted, reason = validate_recall_audit_match(
        human,
        {"type": "suggestion", "text": "建议给表3.1补充误差棒"},
        {"covered": True, "confidence": 0.95},
    )
    assert not accepted and reason == "type_mismatch"

    accepted, reason = validate_recall_audit_match(
        human, auto, {"covered": True, "confidence": 0.70},
    )
    assert not accepted and reason == "low_judge_confidence"


def test_recall_audit_recovers_only_strict_qwen_matches() -> None:
    from unittest.mock import patch

    from evaluate_thesis import _run_alignment_recall_audit

    human = [
        {"type": "weakness", "text": "表3.1缺少误差棒和置信区间"},
        {"type": "suggestion", "text": "建议补充多带宽场景实验"},
    ]
    auto = [
        {
            "type": "weakness",
            "text": "表3.1未报告方差、误差棒与置信区间",
            "dimension": "experiment",
            "confidence": 0.88,
        },
        {
            "type": "suggestion",
            "text": "补充不同带宽条件下的鲁棒性测试",
            "dimension": "experiment",
            "confidence": 0.82,
        },
    ]
    response = {
        "matches": [
            {"human_idx": 0, "auto_idx": 0, "covered": True, "confidence": 0.91},
            {"human_idx": 1, "auto_idx": 1, "covered": True, "confidence": 0.70},
        ]
    }
    with patch("evaluate_thesis._llm_json", return_value=response):
        matches, metrics = _run_alignment_recall_audit(human, auto, [])

    assert metrics["recovered"] == 1
    assert metrics["rejected"] == 1
    assert matches[0]["human_index"] == 0
    assert matches[0]["match_stage"] == "recall_audit"
