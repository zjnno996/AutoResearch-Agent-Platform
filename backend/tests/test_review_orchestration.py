from __future__ import annotations

import base64
import os
from contextlib import ExitStack
from unittest.mock import Mock, patch

import review_engine.reviewer as reviewer
import review_engine.llm_client as review_llm_client
from review_engine.consensus import CandidateIssue, calibrate_absence_claims
from review_engine.export_utils import export_latex
from services.review_service import (
    ReviewHandler,
    _qwen_health,
    _qwen_unavailable_message,
    _validate_uploaded_base64,
)


def test_review_service_rejects_invalid_or_empty_base64() -> None:
    assert _validate_uploaded_base64("not-base64!!!") == "Invalid base64 file payload"
    assert _validate_uploaded_base64("") == "Uploaded file is empty"
    assert _validate_uploaded_base64(base64.b64encode(b"%PDF-test").decode()) is None


def test_multipart_parser_handles_binary_pdf_and_text_fields() -> None:
    boundary = "----auto-review-test"
    raw = (
        f"--{boundary}\r\n"
        'Content-Disposition: form-data; name="file"; filename="paper.pdf"\r\n'
        "Content-Type: application/pdf\r\n\r\n"
    ).encode() + b"%PDF-test" + (
        f"\r\n--{boundary}\r\n"
        'Content-Disposition: form-data; name="dimensions"\r\n\r\n'
        '["methodology"]\r\n'
        f"--{boundary}--\r\n"
    ).encode()

    parts = ReviewHandler._parse_multipart(None, raw, boundary)

    assert parts[0] == (
        "file", base64.b64encode(b"%PDF-test").decode("ascii"), "paper.pdf",
    )
    assert parts[1] == ("dimensions", '["methodology"]', "")


def test_qwen_health_reports_endpoint_failure_without_marking_business_down(
    monkeypatch,
) -> None:
    monkeypatch.setattr(
        "services.review_service._configured_qwen_endpoints",
        lambda: ["http://127.0.0.1:1/v1"],
    )
    health = _qwen_health(timeout_sec=0.01)
    assert health["available"] is False
    assert health["endpoints"][0]["available"] is False
    message = _qwen_unavailable_message(health)
    assert "未生成、未保存" in message
    assert "127.0.0.1:1" in message


def test_same_qwen_model_keeps_independent_endpoint_failover(monkeypatch) -> None:
    config = {
        "web_chat_llm": {
            "provider": "openai-compatible", "base_url": "http://primary/v1",
            "api_key": "primary-key", "primary_model": "Qwen3-Test",
        },
        "web_chat_llm_fallbacks": [{
            "provider": "openai-compatible", "base_url": "http://fallback/v1",
            "api_key": "fallback-key", "primary_model": "Qwen3-Test",
        }],
        "review_llm": {
            "provider": "openai-compatible", "base_url": "http://primary/v1",
            "api_key": "primary-key", "primary_model": "Qwen3-Test",
        },
    }
    old_configs = review_llm_client._llm_configs
    old_options = review_llm_client._llm_model_options
    old_clients = review_llm_client._llm_clients
    try:
        monkeypatch.setattr(review_llm_client, "_load_config", lambda: config)
        review_llm_client._llm_configs = []
        review_llm_client._llm_model_options = []
        review_llm_client._llm_clients = {}
        review_llm_client._build_model_configs()
        review_llm_client.register_config_section("review_llm")

        assert len(review_llm_client._llm_configs) == 1
        endpoint_config = review_llm_client._llm_configs[0]
        assert endpoint_config["base_url"] == "http://primary/v1"
        assert endpoint_config["fallback_url"] == "http://fallback/v1"
        assert endpoint_config["fallback_api_key"] == "fallback-key"
    finally:
        review_llm_client._llm_configs = old_configs
        review_llm_client._llm_model_options = old_options
        review_llm_client._llm_clients = old_clients


def test_run_review_is_qwen_only_and_returns_confidence_filtered_findings() -> None:
    captured = {}
    dimension_result = {
        "dimensionId": "methodology", "score": 65, "summary": "ok",
        "strengths": [], "weaknesses": ["第3章缺少复杂度分析"],
        "suggestions": ["补充复杂度推导"], "_token_usage": {},
    }

    def fake_consensus(**kwargs):
        captured["model"] = kwargs["model"]
        return {
            "verifiedFindings": [{
                "candidate_id": "C001", "text": "第3章缺少复杂度分析",
                "dimension": "methodology", "suggestion": "补充复杂度推导",
                "severity": "major", "evidence": "Section 3",
                "evidence_locators": ["Section 3", "E0042"],
                "evidence_excerpt": "Section 3（E0042）只给出算法流程。",
                "evidence_confidence": 0.9, "priority_score": 88.0,
            }],
            "metrics": {"debates_triggered": 1},
        }

    with ExitStack() as stack:
        stack.enter_context(patch.dict(os.environ, {"AUTO_REVIEW_DISABLE_CHECKPOINT": "1"}))
        stack.enter_context(patch.object(reviewer, "_get_paper_text", return_value="""摘要
本文提出方法。
第3章 方法
缺少复杂度分析。"""))
        stack.enter_context(patch.object(reviewer, "_fetch_reference_context", return_value=""))
        stack.enter_context(patch.object(reviewer, "_extract_references", return_value=[]))
        stack.enter_context(patch.object(reviewer, "_extract_paper_facts", return_value={"claim": "test"}))
        stack.enter_context(patch.object(reviewer, "_generate_skeptic_questions", return_value=[]))
        stack.enter_context(patch.object(reviewer, "_run_review_hybrid", return_value=[dimension_result]))
        stack.enter_context(patch.object(reviewer, "_run_review_deep_dive", return_value={"weaknesses": [], "suggestions": []}))
        stack.enter_context(patch.object(reviewer, "_run_review_patch", return_value={"weaknesses": [], "suggestions": []}))
        stack.enter_context(patch.object(reviewer, "_generate_overall_summary", return_value={"overallAssessment": "ok"}))
        stack.enter_context(patch.object(reviewer, "get_client_for_model", return_value=object()))
        stack.enter_context(patch.object(reviewer, "run_consensus_pipeline", side_effect=fake_consensus))
        results, meta, _ = reviewer.run_review(
            base64.b64encode(b"fake-pdf").decode(), "paper.pdf",
            ["methodology"], model="deepseek-v4-flash", vision_reader=False,
        )

    assert results
    assert meta["review_model"] == "Qwen3.5-122B-A10B-FP8"
    assert meta["model_policy"] == "qwen_only"
    assert captured["model"] == "Qwen3.5-122B-A10B-FP8"
    assert len(meta["verifiedFindings"]) == 1
    assert results[0]["weaknesses"] == ["第3章缺少复杂度分析"]
    assert results[0]["filteredLowConfidenceCount"] == 0
    assert meta["keyFindings"]["weaknesses"][0]["priorityScore"] == 88.0
    assert meta["categorizedFindings"][0]["id"] == "method_theory"
    assert meta["categorizedFindings"][0]["confidenceLevel"] == "high"
    assert meta["confidenceSummary"]["overall"] == 0.9
    assert meta["confidenceSummary"]["debatesTriggered"] == 1
    action = meta["reportSummary"]["priorityActions"][0]
    assert action["evidenceLocators"] == ["Section 3", "E0042"]
    assert "算法流程" in action["evidenceExcerpt"]
    task = meta["reportSummary"]["modificationTasks"][0]
    assert task["location"] == "Section 3、E0042"
    assert task["expectedDeliverable"]
    assert len(task["verificationChecks"]) >= 3
    latex = export_latex({
        "fileName": "paper.pdf", "overallScore": 65,
        "dimensionCount": 1, "meta": meta,
    })
    assert "Modification Acceptance Plan" in latex
    assert "Expected deliverable" in latex


def test_review_stage_checkpoint_reuses_completed_qwen_stages(tmp_path) -> None:
    dimension_result = {
        "dimensionId": "methodology", "score": 65, "summary": "方法摘要",
        "strengths": ["第3章给出方法流程"],
        "weaknesses": ["第3章缺少复杂度分析"],
        "suggestions": ["补充复杂度推导"], "_token_usage": {},
    }
    facts = Mock(return_value={"claim": "测试"})
    hybrid = Mock(return_value=[dimension_result])
    consensus = Mock(return_value={
        "verifiedFindings": [{
            "candidate_id": "C001", "text": "第3章缺少复杂度分析",
            "dimension": "methodology", "suggestion": "补充复杂度推导",
            "severity": "major", "evidence": "第3章",
            "evidence_confidence": 0.9, "priority_score": 88.0,
        }],
        "metrics": {"debates_triggered": 0},
    })
    summary = Mock(return_value={"overallAssessment": "论文需要补充理论分析"})

    with ExitStack() as stack:
        stack.enter_context(patch.object(reviewer, "CHECKPOINT_DIR", tmp_path))
        stack.enter_context(patch.object(reviewer, "_get_paper_text", return_value="第3章 方法\n缺少复杂度分析。"))
        stack.enter_context(patch.object(reviewer, "_fetch_reference_context", return_value=""))
        stack.enter_context(patch.object(reviewer, "_extract_references", return_value=[]))
        stack.enter_context(patch.object(reviewer, "_extract_paper_facts", facts))
        stack.enter_context(patch.object(reviewer, "_generate_skeptic_questions", return_value=[]))
        stack.enter_context(patch.object(reviewer, "_run_review_hybrid", hybrid))
        stack.enter_context(patch.object(reviewer, "_run_review_deep_dive", return_value={"weaknesses": [], "suggestions": []}))
        stack.enter_context(patch.object(reviewer, "_run_review_patch", return_value={"weaknesses": [], "suggestions": []}))
        stack.enter_context(patch.object(reviewer, "_run_expert_coverage_sweep", return_value=[]))
        stack.enter_context(patch.object(reviewer, "run_consensus_pipeline", consensus))
        stack.enter_context(patch.object(reviewer, "_generate_overall_summary", summary))
        stack.enter_context(patch.object(reviewer, "get_client_for_model", return_value=object()))
        first = reviewer.run_review(
            base64.b64encode(b"checkpoint-pdf").decode(), "checkpoint.pdf",
            ["methodology"], model="Qwen3.5-122B-A10B-FP8",
            vision_reader=False,
        )
        second = reviewer.run_review(
            base64.b64encode(b"checkpoint-pdf").decode(), "checkpoint.pdf",
            ["methodology"], model="Qwen3.5-122B-A10B-FP8",
            vision_reader=False,
        )

    assert facts.call_count == 1
    assert hybrid.call_count == 1
    assert consensus.call_count == 1
    assert summary.call_count == 1
    assert first[1]["checkpoint"]["resumed"] is False
    assert set(second[1]["checkpoint"]["hits"]) == {
        "analysis_context", "prepared_results", "consensus_output", "overall_summary",
    }


def test_low_confidence_findings_are_removed_from_normal_results() -> None:
    results = [{
        "dimensionId": "experiment", "score": 70, "summary": "ok",
        "strengths": ["表3.1报告吞吐量"],
        "weaknesses": ["表3.1缺少误差棒", "实验可能不够丰富"],
        "suggestions": ["补充误差棒", "增加实验"],
    }]
    findings = [{
        "candidate_id": "C001", "dimension": "experiment",
        "text": "表3.1缺少误差棒", "suggestion": "补充误差棒",
        "evidence_confidence": 0.82, "verdict": "supported",
    }]
    filtered = reviewer._retain_verified_findings(results, findings)
    assert filtered[0]["weaknesses"] == ["表3.1缺少误差棒"]
    assert filtered[0]["suggestions"] == ["补充误差棒"]
    assert filtered[0]["filteredLowConfidenceCount"] == 1


def test_multilens_coverage_parser_is_evidence_gated_and_pairs_suggestion() -> None:
    lens = reviewer._COVERAGE_SWEEP_LENSES[0]
    parsed = reviewer._parse_coverage_sweep_issues({
        "issues": [
            {
                "dimension": "structure_logic",
                "weakness": "第1章贡献与第7章结论存在重复。",
                "suggestion": "合并重复表述并突出结论新增信息。",
                "evidence": "第1章1.3节与第7章7.2节包含相同贡献列表。",
                "confidence": 0.72,
            },
            {
                "dimension": "experiment",
                "weakness": "实验不足。",
                "suggestion": "建议补实验。",
                "evidence": "第5章。",
                "confidence": 0.91,
            },
            {
                "dimension": "writing",
                "weakness": "表述可能不够清晰。",
                "suggestion": "建议修改。",
                "evidence": "没有定位",
                "confidence": 0.80,
            },
        ],
    }, lens)

    assert len(parsed) == 1
    assert parsed[0]["suggestion"].startswith("建议")
    assert parsed[0]["recall_lens"] == "scope_structure"


def test_multilens_merge_retains_independent_agent_support() -> None:
    issues = [
        {
            "dimension": "structure_logic",
            "weakness": "第1章贡献与第7章结论存在大量重复。",
            "suggestion": "建议压缩第7章重复内容。",
            "evidence": "第1章1.3节和第7章7.2节。",
            "confidence": 0.76,
            "recall_lens": "scope_structure",
            "source_count": 1,
        },
        {
            "dimension": "structure_logic",
            "weakness": "第7章结论与第1章贡献陈述重复。",
            "suggestion": "建议保留结论中的新增归纳并删除重复贡献列表。",
            "evidence": "第7章7.2节与第1章1.3节内容重复。",
            "confidence": 0.82,
            "recall_lens": "visual_format",
            "source_count": 1,
        },
    ]

    merged = reviewer._merge_coverage_sweep_issues(issues)
    assert len(merged) == 1
    assert merged[0]["source_count"] == 2
    assert set(merged[0]["recall_lenses"]) == {"scope_structure", "visual_format"}


def test_structured_findings_do_not_drop_later_coverage_sweep_items() -> None:
    result = {
        "dimensionId": "structure_logic",
        "findings": [{
            "weakness": "第3章与第4章衔接说明不足。",
            "suggestion": "建议增加章节关系说明。",
            "evidence": "第3章3.7节。",
        }],
        "_coverage_sweep": [{
            "weakness": "第1章贡献与第7章结论存在重复。",
            "suggestion": "建议压缩重复内容。",
            "evidence": "第1章1.3节与第7章7.2节。",
            "source_count": 2,
            "recall_lenses": ["scope_structure", "visual_format"],
        }],
    }

    bound = reviewer._bind_result_findings(result)
    assert len(bound["findings"]) == 2
    assert bound["findings"][1]["source_count"] == 2


def test_absence_claim_without_exact_locator_is_hidden_by_confidence_gate() -> None:
    candidate = CandidateIssue(
        candidate_id="C001",
        text="论文缺少复杂度分析。",
        dimension="theory_depth",
        verdict="supported",
        evidence="正文中未发现相关分析。",
        evidence_confidence=0.88,
    )
    calibrated = calibrate_absence_claims([candidate], min_confidence=0.55)
    assert calibrated == 1
    assert candidate.evidence_confidence == 0.54
    assert candidate.verdict == "uncertain"

    grounded = CandidateIssue(
        candidate_id="C002",
        text="第5章缺少复杂度分析。",
        dimension="theory_depth",
        verdict="supported",
        evidence="第5章5.4节仅给出训练流程与实验结果，未给出时间复杂度推导。",
        evidence_confidence=0.84,
    )
    assert calibrate_absence_claims([grounded], min_confidence=0.55) == 0
    assert grounded.evidence_confidence == 0.84


def test_report_weaknesses_and_suggestions_only_use_verified_findings() -> None:
    results = [{
        "dimensionId": "experiment",
        "score": 72,
        "summary": "实验总体完整。",
        "strengths": ["表3.1给出主要结果。"],
        "weaknesses": ["表3.1缺少误差棒。"],
        "suggestions": ["建议补充误差棒。"],
    }]
    verified = [{
        "candidate_id": "C001",
        "dimension": "experiment",
        "text": "表3.1缺少误差棒。",
        "suggestion": "建议在表3.1补充均值、标准差和置信区间。",
        "severity": "major",
        "evidence": "表3.1仅报告均值。",
        "evidence_confidence": 0.86,
        "priority_score": 88.0,
    }]
    overall = {
        "reviewers": [{
            "highlights": ["第3章实验设计完整。"],
            "keyIssues": ["不存在的未经验证问题。"],
            "improvementAdvice": ["建议执行未经验证的修改。"],
        }]
    }
    report = reviewer._build_report_summary(
        results,
        {
            "verifiedFindings": verified,
            "filteredFindings": [],
            "consensusMetrics": {"min_confidence": 0.55},
        },
        overall,
        72,
    )

    assert report["weaknesses"][0]["text"] == "表3.1缺少误差棒。"
    assert report["suggestions"][0]["text"].startswith("建议在表3.1")
    assert "未经验证" not in str(report["weaknesses"] + report["suggestions"])
    assert "表3.1缺少误差棒" in report["overallComment"]
