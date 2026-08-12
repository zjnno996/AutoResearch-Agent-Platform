from __future__ import annotations

import base64
import json
import os
import re
from contextlib import ExitStack
from unittest.mock import patch

import fitz

import review_engine.pdf_utils as pdf_utils
import review_engine.reviewer as reviewer


class _Response:
    def __init__(self, content: str):
        self.content = content
        self.prompt_tokens = 100
        self.completion_tokens = 50
        self.total_tokens = 150


class FakeQwen:
    def __init__(self) -> None:
        self.stages: list[str] = []

    def chat(self, **kwargs):
        system = str(kwargs.get("system", ""))
        content = kwargs["messages"][0]["content"]
        prompt = content[0]["text"] if isinstance(content, list) else str(content)

        if isinstance(content, list) and "Extract ALL visual evidence" in prompt:
            self.stages.append("vision")
            page = int(re.search(r"page (\d+)", prompt).group(1))
            image_count = sum(part.get("type") == "image_url" for part in content)
            return _Response("\n".join(
                f"### Visual {index + 1}\nFigure on Page {page}: throughput improves by {10 + index}%."
                for index in range(image_count)
            ))

        if "expert academic paper analyst" in system:
            self.stages.append("facts")
            return _Response(json.dumps({
                "research_question": "How to improve dynamic edge inference?",
                "claim": "The proposed scheduler reduces latency.",
                "method_summary": "A dynamic scheduling method is evaluated in §3.",
                "datasets": ["SyntheticEdge-1K"],
                "baselines": ["Baseline-A"],
                "key_results": [{
                    "claim": "Latency drops by 20%",
                    "evidence": "20%",
                    "section": "Table 3.1",
                }],
                "limitations": ["Only one bandwidth setting is evaluated."],
            }))

        if "generate 5-8 specific questions" in prompt:
            self.stages.append("skeptic_questions")
            return _Response(json.dumps([
                "Table 3.1 only uses one bandwidth setting; does the result generalize?"
            ]))

        if "## Evaluation Dimensions" in prompt:
            self.stages.append("dimension_batch")
            dimension_ids = re.findall(r"\(id:\s*([a-z_]+)\)", prompt)
            weaknesses = {
                "methodology": "§3.1未报告随机种子控制。",
                "novelty": "§2.1缺少与既有调度器的组件级比较。",
                "experiment": "表3.1未报告方差和置信区间。",
                "writing": "§1.2使用了未定义的调度缩写。",
                "related_work": "§2.2遗漏最接近的动态推理基线。",
                "reproducibility": "§3.3未给出调度器超参数。",
                "ethics": "§6未讨论设备遥测数据隐私。",
                "writing_format": "图1的图例无法独立理解。",
                "structure_logic": "§4在定义评价指标前先给出了结果。",
                "theory_depth": "§3.2缺少时间复杂度分析。",
            }
            return _Response(json.dumps({
                dimension_id: {
                    "analysis": f"§3.1和表3.1提供了{dimension_id}相关证据。",
                    "self_critique": "现有证据仍然有限。",
                    "score": 72,
                    "summary": f"{dimension_id}维度契约测试摘要。",
                    "strengths": [f"§3.1给出了具体的{dimension_id}描述。"],
                    "weaknesses": [weaknesses[dimension_id]],
                    "suggestions": [f"在对应证据位置修正{dimension_id}问题。"],
                }
                for dimension_id in dimension_ids
            }))

        if "critical second-pass reviewer" in system:
            self.stages.append("deep_dive")
            return _Response(json.dumps({
                "summary": "仍有一个深层问题。",
                "weaknesses": ["§4.2缺少跨设备失效分析。"],
                "suggestions": ["在§4.2补充跨设备失效分析。"],
            }))

        if "critical patch reviewer" in system:
            self.stages.append("patch")
            return _Response(json.dumps({
                "summary": "仍有一个格式问题。",
                "weaknesses": ["表3.1未定义全部缩写。"],
                "suggestions": ["在表3.1下方定义全部缩写。"],
            }))

        if any(name in system for name in (
            "研究范围与结构专家", "理论与实验专家", "图表与学术规范专家",
        )):
            self.stages.append("coverage_sweep")
            if "研究范围与结构专家" in system:
                issue = {
                    "dimension": "structure_logic",
                    "weakness": "第1章研究范围与第6章结论的对应关系不清。",
                    "suggestion": "建议在第6章逐项对应第1章研究问题。",
                    "evidence": "第1章1.2节与第6章6.1节未建立逐项映射。",
                    "confidence": 0.83,
                }
            elif "图表与学术规范专家" in system:
                issue = {
                    "dimension": "writing_format",
                    "weakness": "图1的图例未解释调度缩写。",
                    "suggestion": "建议在图1图例中补充全部缩写定义。",
                    "evidence": "图1仅展示调度缩写，正文第1章未给出图例定义。",
                    "confidence": 0.84,
                }
            else:
                issue = {
                    "dimension": "experiment",
                    "weakness": "表3.1仅覆盖单一带宽，未检验混合动态场景。",
                    "suggestion": "建议在表3.1后补充多带宽混合动态场景实验。",
                    "evidence": "表3.1仅列出一个带宽设置。",
                    "confidence": 0.86,
                }
            return _Response(json.dumps({
                "issues": [issue]
            }, ensure_ascii=False))

        if "学术评审文本翻译器" in system:
            self.stages.append("zh_normalizer")
            items = json.loads(prompt.splitlines()[-1])
            return _Response(json.dumps({
                "translations": [{
                    "id": item["id"],
                    "text": f"第3章相关评审内容已转换为中文（定位信息保持不变）。",
                } for item in items]
            }, ensure_ascii=False))

        if "devil's advocate reviewer" in system:
            self.stages.append("skeptic_review")
            return _Response(json.dumps({
                "analysis": "表3.1只覆盖一个设置。",
                "self_critique": "其他位置可能还有不同设置。",
                "score": 68,
                "summary": "主要假设需要更广泛的测试。",
                "strengths": ["§3.1说明了已评估设置。"],
                "weaknesses": ["表3.1未测试带宽变化。"],
                "suggestions": ["围绕表3.1补充带宽扫描实验。"],
            }))

        if "evidence verifier" in system:
            self.stages.append("verifier")
            ids = list(dict.fromkeys(re.findall(r'"id":\s*"(C\d+)"', prompt)))
            verifications = []
            for index, candidate_id in enumerate(ids):
                if index == len(ids) - 1:
                    verdict, confidence = "uncertain", 0.42
                elif index == len(ids) - 2:
                    verdict, confidence = "contradicted", 0.9
                else:
                    verdict, confidence = "supported", 0.82
                verifications.append({
                    "id": candidate_id,
                    "verdict": verdict,
                    "confidence": confidence,
                    "severity": "major",
                    "evidence": "§3.1 / Table 3.1",
                    "corrected_text": "",
                })
            return _Response(json.dumps({"verifications": verifications}))

        if "博士学位论文评审专家委员会负责人" in system:
            self.stages.append("summary")
            return _Response(json.dumps({
                "reviewers": [
                    {
                        "expertise": f"专家{index}",
                        "overallEvaluation": "流程契约测试评价。",
                        "keyIssues": ["Table 3.1 evidence gap."],
                        "improvementAdvice": ["Add a broader evaluation."],
                        "overallVerdict": "需修改",
                        "recommendation": "同意修改后答辩",
                    }
                    for index in range(1, 4)
                ]
            }, ensure_ascii=False))

        raise AssertionError(f"Unexpected fake-Qwen stage: {system[:80]} / {prompt[:80]}")


def _synthetic_thesis_pdf() -> bytes:
    doc = fitz.open()
    page = doc.new_page()
    page.insert_text((72, 72), "Abstract")
    page.insert_text((72, 100), "This thesis studies dynamic edge inference and scheduling.")
    page.insert_text((72, 130), "The proposed scheduler reduces latency by 20 percent.")
    page.insert_text((72, 300), "Figure 1: Dynamic scheduling architecture")
    page = doc.new_page()
    page.insert_text((72, 72), "3 Method and Experiments")
    page.insert_text((72, 100), "Baseline-A is evaluated on SyntheticEdge-1K at one bandwidth.")
    page.insert_text((72, 300), "Table 3.1: Latency and throughput results")
    raw = doc.tobytes()
    doc.close()
    return raw


def test_full_pipeline_generates_all_confident_findings_without_top10() -> None:
    raw = _synthetic_thesis_pdf()
    client = FakeQwen()
    dimensions = [
        "methodology", "novelty", "experiment", "writing", "related_work",
        "reproducibility", "ethics", "skeptic", "writing_format",
        "structure_logic", "theory_depth",
    ]

    pdf_utils._vision_extraction_cache.clear()
    with ExitStack() as stack:
        stack.enter_context(patch.dict(os.environ, {"AUTO_REVIEW_DISABLE_CHECKPOINT": "1"}))
        stack.enter_context(patch.object(pdf_utils, "_find_vision_model", return_value="fake-qwen-vl"))
        stack.enter_context(patch("review_engine.llm_client.get_client_for_model", return_value=client))
        stack.enter_context(patch.object(pdf_utils, "_cache_get", return_value=None))
        stack.enter_context(patch.object(pdf_utils, "_cache_set", return_value=None))
        stack.enter_context(patch.object(reviewer, "get_client_for_model", return_value=client))
        stack.enter_context(patch.object(reviewer, "_fetch_reference_context", return_value=""))
        results, meta, summary = reviewer.run_review(
            base64.b64encode(raw).decode(),
            "synthetic-thesis.pdf",
            dimensions,
            model="Qwen3.5-122B-A10B-FP8",
            vision_reader=True,
            hybrid=True,
            venue="THESIS",
            enable_debate=False,
            min_finding_confidence=0.55,
        )
    pdf_utils._vision_extraction_cache.clear()

    assert len(results) >= 11
    assert meta["vision_coverage_complete"] is True
    assert meta["consensusMetrics"]["raw_candidates"] > 10
    assert meta["consensusMetrics"]["filtered_low_confidence"] == 1
    assert meta["consensusMetrics"]["contradicted_candidates"] >= 1
    assert len(meta["verifiedFindings"]) > 10  # explicitly not Top-10 truncated
    assert all(item["evidence_confidence"] >= 0.55 for item in meta["verifiedFindings"])
    assert all(
        detail["evidence_confidence"] >= 0.55
        for result in results
        for detail in result.get("verifiedFindingDetails", [])
    )
    assert len(meta["categorizedFindings"]) == 7
    assert summary and len(summary["reviewers"]) == 3
    assert all(
        re.search(r"[\u4e00-\u9fff]", str(item.get(field, "")))
        for item in meta["verifiedFindings"]
        for field in ("text", "evidence")
        if item.get(field)
    )
    assert {
        "vision", "facts", "skeptic_questions", "dimension_batch",
        "skeptic_review", "deep_dive", "patch", "coverage_sweep",
        "zh_normalizer", "verifier", "summary",
    }.issubset(set(client.stages))
