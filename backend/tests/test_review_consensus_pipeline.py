from __future__ import annotations

import json

from review_data.issue_patterns import IssuePatternIndex, load_issue_cards, semantic_similarity
from review_engine.consensus import (
    CandidateIssue,
    build_candidate_pool,
    calibrate_candidate_severity,
    candidate_display_decision,
    normalize_candidate_suggestions,
    rank_candidates,
    reverify_borderline_candidates,
    run_consensus_pipeline,
    run_targeted_debates,
    _unsupported_numeric_facts,
    verify_candidates,
)
from review_engine.evidence_map import build_evidence_map
from review_engine.document_lint import scan_document_lint
from review_engine.claim_audit import (
    build_claim_evidence_matrix,
    generate_claim_audit_findings,
)
from review_engine.pdf_utils import _parse_sections


def test_chinese_sections_accumulate_and_build_evidence_map() -> None:
    paper = """摘要
本文研究动态推理。
第3章 方法设计
本文提出动态微批次方法，降低推理时延20%。
3.1 实验设置
表3.1显示吞吐量提升15%。
第4章 扩展方法
本文提出异构部署算法。
4.1 实验设置
图4.2表明GPU利用率提升10%。
第7章 总结与展望
本文总结全文。
"""
    sections = _parse_sections(paper)
    assert "实验设置" in sections["experiment"]
    assert "表3.1" in sections["experiment"]
    assert "图4.2" in sections["experiment"]
    evidence = build_evidence_map(paper)
    assert evidence.stats["units"] >= 3
    assert any("吞吐量" in unit.text for unit in evidence.units)


def test_claim_evidence_matrix_separates_supported_and_unverified_claims() -> None:
    evidence = build_evidence_map("""第3章 实验
表3.1显示准确率提升15%。
第4章 讨论
结果表明该方法显著优于所有基线。
""")

    matrix = build_claim_evidence_matrix(evidence)

    assert matrix["claim_count"] >= 2
    by_text = {row["claim"]: row for row in matrix["claims"]}
    supported = next(row for text, row in by_text.items() if "提升15%" in text)
    unverified = next(row for text, row in by_text.items() if "所有基线" in text)
    assert supported["status"] == "supported"
    assert unverified["status"] == "needs_verification"


def test_claim_evidence_matrix_excludes_preamble_and_related_work_claims() -> None:
    evidence = build_evidence_map("""培养要求
完成授权国家发明专利2项。
第2章 相关工作
已有方法显著提升了推理速度。
第3章 本文方法
本文方法降低了部署时延。
""")

    matrix = build_claim_evidence_matrix(evidence)
    claims = " ".join(row["claim"] for row in matrix["claims"])

    assert "发明专利" not in claims
    assert "已有方法" not in claims
    assert "部署时延" in claims


class FakeClaimAuditClient:
    def chat(self, messages, system="", **kwargs):
        class Response:
            content = json.dumps({"findings": [{
                "dimension": "structure_logic",
                "weakness": "摘要E0001与结论E0007报告的提升幅度不一致。",
                "suggestion": "建议核对原始结果并统一摘要与结论中的提升幅度。",
                "evidence": "E0001（摘要）报告15%，E0007（第6章）报告20%。",
                "issue_type": "cross_section_conflict",
                "confidence": 0.88,
                "severity": "major",
            }]}, ensure_ascii=False)
        return Response()


def test_claim_audit_recalls_only_localized_structured_findings() -> None:
    evidence = build_evidence_map("第3章 实验\n表3.1显示准确率提升15%。")
    matrix = build_claim_evidence_matrix(evidence)

    findings = generate_claim_audit_findings(
        matrix, evidence, FakeClaimAuditClient(), "qwen-test",
    )

    assert len(findings) == 1
    assert findings[0]["issue_type"] == "cross_section_conflict"
    assert findings[0]["suggestion"].startswith("建议")


def test_document_lint_detects_high_precision_thesis_format_issues() -> None:
    repeated = "本文围绕动态边缘环境构建了完整技术体系，并从计算、通信和知识三个维度展开研究。"
    paper = f"""博士学位论文
摘要
本文研究动态边缘计算。
ABSTRACT
This paper studies elastic edge intelligence.
KEY WORDS: edge intelligence
第1章 绪论
{repeated}
3.7 讨论和未来工作
后续考虑复杂场景。
第4章 方法
系统使用 XYZ 完成调度，XYZ 是核心模块，XYZ 支持部署，XYZ 降低开销，XYZ 完成优化。
4.7 讨论和未来工作
后续考虑真实设备。
第7章 总结
{repeated}
致谢
感谢[导师姓名]的指导。
"""

    findings = scan_document_lint(paper)
    rule_ids = {item["rule_id"] for item in findings}

    assert "unresolved_placeholder" in rule_ids
    assert "thesis_called_paper" in rule_ids
    assert "distributed_future_work" in rule_ids
    assert "exact_long_sentence_repetition" in rule_ids
    assert "undefined_abbreviation" in rule_ids
    assert all("第" in item["evidence"] for item in findings)


def test_document_lint_does_not_flag_defined_abbreviation_or_dissertation() -> None:
    paper = """博士学位论文
ABSTRACT
This dissertation presents a Dynamic Scheduling Network (DSN).
第1章 绪论
动态调度网络（DSN）用于边缘推理。DSN 支持部署，DSN 处理请求，DSN 优化时延，DSN 改善吞吐。
"""

    rule_ids = {item["rule_id"] for item in scan_document_lint(paper)}

    assert "thesis_called_paper" not in rule_ids
    assert "undefined_abbreviation" not in rule_ids


def test_issue_cards_load_and_target_paper_is_excluded() -> None:
    cards = load_issue_cards()
    assert len(cards) >= 100
    assert any(card.dimension == "experiment" for card in cards)
    index = IssuePatternIndex(cards)
    target_title = "物联网应用的智能生成与优化关键技术研究"
    retrieved = index.retrieve(
        "WasmRL 实验缺少能耗和复杂度分析",
        dimension="experiment",
        target_paper_text=target_title + " 摘要",
        k=20,
    )
    assert retrieved
    assert all(card.paper_title != target_title for _, card in retrieved)


def test_chinese_semantic_similarity_detects_paraphrases() -> None:
    score = semantic_similarity(
        "实验未覆盖高带宽网络环境，评估场景不充分",
        "缺少对高带宽网络场景的实验测试，评估覆盖不足",
    )
    assert score >= 0.35


class FakeConsensusClient:
    def chat(self, messages, system="", **kwargs):
        class Response:
            content = ""
        response = Response()
        if "evidence verifier" in system:
            response.content = json.dumps({
                "verifications": [
                    {
                        "id": "C001",
                        "verdict": "uncertain",
                        "confidence": 0.65,
                        "severity": "major",
                        "evidence": "Table 3.1 reports only one network setting",
                        "corrected_text": "实验仅覆盖单一网络设置，无法验证跨带宽鲁棒性",
                    },
                    {
                        "id": "C002",
                        "verdict": "supported",
                        "confidence": 0.9,
                        "severity": "major",
                        "evidence": "Section 4 has no complexity analysis",
                        "corrected_text": "第4章缺少算法复杂度分析",
                    },
                ]
            }, ensure_ascii=False)
        elif "paper-side defender" in system:
            response.content = json.dumps({
                "position": "narrow", "evidence": "Table 3.1", "confidence": 0.6,
                "reason": "Only one setting is shown"
            })
        elif "skeptical domain reviewer" in system:
            response.content = json.dumps({
                "position": "support", "evidence": "Table 3.1", "confidence": 0.85,
                "reason": "No bandwidth sweep"
            })
        elif "impact and remediation assessor" in system:
            response.content = json.dumps({
                "position": "support", "evidence": "Table 3.1", "confidence": 0.86,
                "reason": "The missing sweep weakens robustness evidence",
                "impact": "claim_validity",
                "final_suggestion": "建议在表3.1补充多带宽扫描并报告性能变化。",
                "actionability": 0.92,
            }, ensure_ascii=False)
        elif "senior review chair" in system:
            response.content = json.dumps({
                "decision": "accept", "confidence": 0.88, "severity": "major",
                "final_text": "表3.1仅覆盖单一网络设置，跨带宽鲁棒性证据不足",
                "final_suggestion": "建议在表3.1补充多带宽扫描并报告均值与标准差。",
                "evidence": "Table 3.1", "rationale": "The committee confirms the scope gap"
            }, ensure_ascii=False)
        else:
            raise AssertionError(f"unexpected system prompt: {system[:80]}")
        return response


class FakeSuggestionVerifier:
    def chat(self, messages, system="", **kwargs):
        class Response:
            content = json.dumps({
                "verifications": [
                    {
                        "id": "C001",
                        "verdict": "supported",
                        "confidence": 0.88,
                        "severity": "major",
                        "evidence": "第5章小结未给出复杂度分析",
                        "corrected_text": "第5章缺少 WasmRL 收敛性和复杂度分析",
                        "corrected_suggestion": "建议在 WasmRL 章节小结中补充收敛性与时间/空间复杂度分析。",
                    }
                ]
            }, ensure_ascii=False)
        return Response()


def test_consensus_pipeline_clusters_verifies_debates_and_ranks() -> None:
    results = [
        {
            "dimensionId": "experiment",
            "weaknesses": ["实验未覆盖多种网络带宽，鲁棒性评估不足"],
            "suggestions": ["增加不同网络带宽下的实验"],
        },
        {
            "dimensionId": "skeptic",
            "weaknesses": ["缺少多带宽网络场景测试，鲁棒性证据不充分"],
            "suggestions": ["补充带宽扫描实验"],
        },
        {
            "dimensionId": "theory_depth",
            "weaknesses": ["第4章缺少算法复杂度分析"],
            "suggestions": ["补充时间和空间复杂度推导"],
        },
    ]
    evidence = build_evidence_map(
        """摘要\n本文提出网络推理系统。\n第3章 实验设置\n表3.1仅报告1Gbps网络结果。\n第4章 方法\n本文提出调度算法。"""
    )
    output = run_consensus_pipeline(
        results=results,
        evidence_map=evidence,
        client=FakeConsensusClient(),
        model="fake",
        target_paper_text="unseen target paper",
        issue_index=IssuePatternIndex([]),
        enable_debate=True,
        max_debates=1,
        min_confidence=0.55,
    )
    metrics = output["metrics"]
    assert metrics["raw_candidates"] == 3
    assert metrics["semantic_clusters"] <= 3
    assert metrics["debates_triggered"] == 1
    assert metrics["hybrid_debate_agents"] == 4
    assert metrics["hybrid_debate_roles"] == [
        "paper_defender", "domain_skeptic", "impact_editor", "senior_chair",
    ]
    assert len(output["verifiedFindings"]) == metrics["retained_findings"]
    assert output["verifiedFindings"][0]["priority_score"] > 0
    assert all(item["evidence_confidence"] >= 0.55 for item in output["verifiedFindings"])


def test_hybrid_debate_uses_three_parallel_advisors_and_one_chair() -> None:
    candidate = CandidateIssue(
        candidate_id="C001",
        text="表3.1仅覆盖单一网络设置，跨带宽鲁棒性证据不足。",
        dimension="experiment",
        suggestion="建议补充带宽扫描实验。",
        severity="major",
        evidence="表3.1仅报告1Gbps网络结果。",
        evidence_confidence=0.65,
        verdict="uncertain",
    )
    evidence = build_evidence_map("第3章实验\n表3.1仅报告1Gbps网络结果。")

    debated = run_targeted_debates(
        [candidate], evidence, FakeConsensusClient(), "fake", max_debates=1,
    )

    assert debated == 1
    assert candidate.debate["agent_count"] == 4
    assert candidate.debate["roles"] == [
        "paper_defender", "domain_skeptic", "impact_editor", "senior_chair",
    ]
    assert candidate.debate["impact_editor"]["impact"] == "claim_validity"
    assert candidate.debate["agreement_score"] >= 0.66
    assert candidate.verdict == "supported"
    assert candidate.suggestion.startswith("建议在表3.1")


def test_high_confidence_claim_validity_issue_still_enters_debate() -> None:
    candidate = CandidateIssue(
        candidate_id="C001",
        text="表3.1的 baseline 使用不同数据划分，公平性不足并影响主要结论。",
        dimension="experiment",
        suggestion="建议在表3.1使用同一数据划分重新比较所有基线。",
        severity="major",
        evidence="表3.1及第3章实验设置显示基线采用不同数据划分。",
        evidence_confidence=0.91,
        verdict="supported",
    )
    evidence = build_evidence_map(
        "第3章实验设置\n表3.1中 baseline 使用不同数据划分。"
    )

    debated = run_targeted_debates(
        [candidate], evidence, FakeConsensusClient(), "fake", max_debates=1,
    )

    assert debated == 1
    assert candidate.debate["agent_count"] == 4


class FakeDisagreementClient:
    def chat(self, messages, system="", **kwargs):
        class Response:
            content = ""
        response = Response()
        if "paper-side defender" in system:
            payload = {"position": "oppose", "confidence": 0.8, "evidence": "表3.1"}
        elif "skeptical domain reviewer" in system:
            payload = {"position": "support", "confidence": 0.8, "evidence": "表3.1"}
        elif "impact and remediation assessor" in system:
            payload = {"position": "uncertain", "confidence": 0.6, "evidence": "表3.1"}
        elif "senior review chair" in system:
            payload = {
                "decision": "accept", "confidence": 0.92, "severity": "major",
                "final_text": "表3.1的比较设置可能影响结论。",
                "final_suggestion": "建议统一表3.1的比较设置。",
                "evidence": "表3.1", "exists": True,
                "is_only_presentation_issue": False,
            }
        else:
            raise AssertionError(system)
        response.content = json.dumps(payload, ensure_ascii=False)
        return response


def test_low_committee_agreement_downgrades_chair_acceptance() -> None:
    candidate = CandidateIssue(
        candidate_id="C001",
        text="表3.1的基线比较设置不一致。",
        dimension="experiment",
        suggestion="建议统一表3.1的比较设置。",
        severity="major",
        evidence="表3.1列出不同设置。",
        evidence_confidence=0.70,
        verdict="supported",
    )

    debated = run_targeted_debates(
        [candidate], build_evidence_map("第3章实验\n表3.1列出不同设置。"),
        FakeDisagreementClient(), "fake", max_debates=1,
    )

    assert debated == 1
    assert candidate.debate["agreement_score"] < 0.66
    assert candidate.verdict == "uncertain"
    assert candidate.evidence_confidence <= 0.74
    assert "consensus_calibration" in candidate.debate


def test_chair_cannot_add_numeric_facts_to_factual_weakness() -> None:
    unsupported = _unsupported_numeric_facts(
        "表3.1仅测试1Gbps，未覆盖100Mbps和10Gbps。",
        "表3.1仅测试1Gbps。",
    )
    assert "100mbps" in unsupported
    assert "10gbps" in unsupported
    assert "1gbps" not in unsupported


def test_candidate_pool_preserves_parallel_expert_style_suggestions() -> None:
    results = [
        {
            "dimensionId": "theory_depth",
            "weaknesses": [
                "第5章对强化学习优化过程的理论解释不足。",
                "第6章跨域对齐方法的适用边界讨论不充分。",
            ],
            "suggestions": [
                "建议在 WasmRL 章节小结中补充收敛性与复杂度分析。",
                "建议增加极低标签比例场景下的性能分析以明确 FDAS 的适用边界。",
            ],
        }
    ]

    candidates = build_candidate_pool(results)

    assert candidates[0].suggestion == "建议在 WasmRL 章节小结中补充收敛性与复杂度分析。"
    assert candidates[1].suggestion == "建议增加极低标签比例场景下的性能分析以明确 FDAS 的适用边界。"


def test_candidate_pool_prefers_bound_findings_over_parallel_arrays() -> None:
    candidates = build_candidate_pool([{
        "dimensionId": "experiment",
        "weaknesses": ["不应使用的旧问题"],
        "suggestions": ["不应使用的旧建议"],
        "findings": [{
            "weakness": "表3.1只报告单次实验结果，缺少方差。",
            "suggestion": "建议补充多次运行的均值、标准差和显著性检验。",
            "evidence": "表3.1",
            "severity": "major",
        }],
    }])

    assert len(candidates) == 1
    assert candidates[0].text.startswith("表3.1")
    assert candidates[0].suggestion.startswith("建议补充")
    assert candidates[0].evidence == "表3.1"


class FakeBorderlineVerifier:
    def chat(self, messages, system="", **kwargs):
        class Response:
            content = json.dumps({
                "verifications": [{
                    "id": "C001",
                    "verdict": "supported",
                    "confidence": 0.81,
                    "evidence": "表3.1仅给出一次运行的准确率。",
                    "corrected_text": "表3.1只报告单次运行，结果稳定性证据不足。",
                    "corrected_suggestion": "建议至少重复运行五次并报告均值和标准差。",
                }]
            }, ensure_ascii=False)
        return Response()


def test_borderline_candidate_gets_focused_reverification() -> None:
    candidates = build_candidate_pool([{
        "dimensionId": "experiment",
        "weaknesses": ["表3.1缺少多次运行结果。"],
        "suggestions": ["建议补充重复实验。"],
    }])
    candidates[0].evidence_confidence = 0.49
    candidates[0].verdict = "uncertain"
    evidence = build_evidence_map("第3章 实验\n表3.1仅给出一次运行的准确率。")

    checked, recovered = reverify_borderline_candidates(
        candidates, evidence, FakeBorderlineVerifier(), "fake", min_confidence=0.55,
    )

    assert checked == 1
    assert recovered == 1
    assert candidates[0].evidence_confidence == 0.81
    assert candidates[0].suggestion.startswith("建议至少")


def test_candidate_pool_adds_thesis_style_fallback_suggestions() -> None:
    results = [
        {
            "dimensionId": "writing_format",
            "weaknesses": ["参考文献列表中部分会议名称和页码格式不统一。"],
            "suggestions": [],
        },
        {
            "dimensionId": "structure_logic",
            "weaknesses": ["第3章和第4章之间的逻辑关系说明不足。"],
            "suggestions": [],
        },
    ]

    candidates = build_candidate_pool(results)

    assert candidates[0].suggestion.startswith("建议")
    assert "参考文献" in candidates[0].suggestion
    assert candidates[1].suggestion.startswith("建议")
    assert "章节" in candidates[1].suggestion


def test_algorithm_complexity_routes_to_theory_action_not_formatting() -> None:
    candidate = build_candidate_pool([{
        "dimensionId": "theory_depth",
        "weaknesses": ["第4章缺少算法复杂度分析。"],
        "suggestions": [],
    }])[0]
    assert "复杂度" in candidate.suggestion
    assert "排版格式" not in candidate.suggestion


def test_verifier_can_correct_candidate_suggestion() -> None:
    candidates = build_candidate_pool([
        {
            "dimensionId": "theory_depth",
            "weaknesses": ["第5章理论分析不足。"],
            "suggestions": [],
        }
    ])
    evidence = build_evidence_map("第5章 WasmRL 方法\n本章介绍强化学习优化，但小结没有复杂度分析。")

    verify_candidates(candidates, evidence, FakeSuggestionVerifier(), "fake")

    assert candidates[0].text == "第5章缺少 WasmRL 收敛性和复杂度分析"
    assert candidates[0].suggestion == "建议在 WasmRL 章节小结中补充收敛性与时间/空间复杂度分析。"


def test_precision_gate_rejects_high_score_when_evidence_still_says_uncertain() -> None:
    candidate = CandidateIssue(
        candidate_id="C001",
        text="第5章缺少复杂度分析。",
        dimension="theory_depth",
        verdict="uncertain",
        evidence_confidence=0.91,
        evidence="第5章证据片段未包含完整方法，无法确认是否给出复杂度分析。",
    )
    accepted, reason = candidate_display_decision(candidate, 0.55)
    assert not accepted
    assert "无法确认" in reason


def test_precision_gate_keeps_supported_localized_issue() -> None:
    candidate = CandidateIssue(
        candidate_id="C001",
        text="表3.1只给出单次运行结果。",
        dimension="experiment",
        verdict="supported",
        evidence_confidence=0.82,
        evidence="表3.1仅列出一次运行的准确率，未报告均值与标准差。",
    )
    assert candidate_display_decision(candidate, 0.55) == (True, "证据支持")


def test_ranked_finding_exposes_structured_evidence_location_and_excerpt() -> None:
    candidate = CandidateIssue(
        candidate_id="C001",
        text="表3.1只给出单次运行结果。",
        dimension="experiment",
        suggestion="建议在第3章补充多次运行统计。",
        verdict="supported",
        evidence_confidence=0.86,
        evidence="E0042（第3.2节，表3.1）：仅列出一次准确率，未报告方差。",
    )

    rank_candidates([candidate])

    assert candidate.evidence_locators == ["E0042", "第3.2节", "表3.1"]
    assert "未报告方差" in candidate.evidence_excerpt


def test_precision_gate_uses_higher_threshold_for_absence_claims() -> None:
    candidate = CandidateIssue(
        candidate_id="C001",
        text="第5章未报告算法复杂度分析。",
        dimension="theory_depth",
        verdict="supported",
        evidence_confidence=0.60,
        evidence="第5章5.4节仅给出算法流程和实验结果。",
    )

    accepted, reason = candidate_display_decision(candidate, 0.55)

    assert not accepted
    assert candidate.display_threshold == 0.66
    assert "风险校准" in reason


def test_precision_gate_calibrates_threshold_by_dimension_and_source() -> None:
    novelty = CandidateIssue(
        candidate_id="C001", text="第2章未充分区分最近工作。",
        dimension="novelty", suggestion="建议在第2章增加逐项对比。",
        verdict="supported", evidence_confidence=0.58,
        evidence="第2章2.3节只给出方法列表。",
    )
    lint = CandidateIssue(
        candidate_id="C002", text="表3.1编号重复。",
        dimension="writing_format", suggestion="建议修正表3.1编号。",
        verdict="supported", evidence_confidence=0.52,
        evidence="表3.1与后续表格使用相同编号。",
        generation_source="deterministic_document_lint",
    )

    assert candidate_display_decision(novelty, 0.55)[0] is False
    assert novelty.display_threshold == 0.60
    rank_candidates([lint])
    assert candidate_display_decision(lint, 0.55)[0] is True
    assert lint.display_threshold == 0.49


def test_precision_gate_rejects_non_actionable_bound_suggestion() -> None:
    candidate = CandidateIssue(
        candidate_id="C001",
        text="表3.1只报告单次运行结果。",
        dimension="experiment",
        suggestion="建议进一步研究。",
        verdict="supported",
        evidence_confidence=0.90,
        evidence="表3.1只列出一次准确率。",
    )
    rank_candidates([candidate])

    accepted, reason = candidate_display_decision(candidate, 0.55)

    assert not accepted
    assert "修改动作" in reason


def test_precision_gate_only_keeps_uncertain_after_strong_narrowing_debate() -> None:
    candidate = CandidateIssue(
        candidate_id="C001",
        text="图4.2对低带宽条件的覆盖有限。",
        dimension="experiment",
        verdict="uncertain",
        evidence_confidence=0.84,
        evidence="图4.2仅展示100Mbps和1Gbps两个带宽点。",
        debate={"chair": {"decision": "narrow"}},
    )
    accepted, reason = candidate_display_decision(candidate, 0.55)
    assert accepted
    assert "Debate" in reason


def test_generic_verification_request_becomes_actionable_suggestion() -> None:
    candidate = CandidateIssue(
        candidate_id="C001",
        text="第5章缺少复杂度分析。",
        dimension="theory_depth",
        suggestion="请提供第5章完整内容以便核实。",
    )
    assert normalize_candidate_suggestions([candidate]) == 1
    assert candidate.suggestion.startswith("建议")
    assert "复杂度" in candidate.suggestion
    assert "请提供" not in candidate.suggestion


def test_severity_and_priority_favor_claim_validity_over_format_nits() -> None:
    theory = CandidateIssue(
        candidate_id="C001",
        text="第5章核心算法缺少复杂度分析，无法判断其在大规模场景下的可扩展性。",
        dimension="theory_depth",
        suggestion="建议在第5章补充时间与空间复杂度推导。",
        severity="major",
        evidence="第5章5.4节仅给出算法流程，未给出复杂度推导。",
        evidence_confidence=0.86,
        verdict="supported",
        issue_category="theory_assumption",
    )
    formatting = CandidateIssue(
        candidate_id="C002",
        text="表5.1边框样式不是三线表。",
        dimension="writing_format",
        suggestion="建议将表5.1统一修改为三线表。",
        severity="major",
        evidence="表5.1使用了封闭边框。",
        evidence_confidence=0.92,
        verdict="supported",
        issue_category="visual_format",
    )
    assert calibrate_candidate_severity([theory, formatting]) == 1
    ranked = rank_candidates([formatting, theory])
    assert formatting.severity == "minor"
    assert ranked[0].candidate_id == "C001"
    assert theory.evidence_quality > 0
    assert theory.suggestion_actionability > 0
    assert theory.priority_score < 100
