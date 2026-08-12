"""Evidence-gated candidate consolidation and targeted multi-agent debate."""

from __future__ import annotations

import json
import os
import re
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, dataclass, field
from typing import Any

from review_data.issue_patterns import IssuePatternIndex, get_issue_pattern_index, semantic_similarity, semantic_tokens

from .document_lint import scan_document_lint
from .evidence_map import EvidenceMap
from .claim_audit import build_claim_evidence_matrix, generate_claim_audit_findings


@dataclass
class CandidateIssue:
    candidate_id: str
    text: str
    dimension: str
    suggestion: str = ""
    source_dimensions: list[str] = field(default_factory=list)
    source_count: int = 1
    severity: str = "major"
    evidence: str = ""
    evidence_confidence: float = 0.45
    verdict: str = "uncertain"
    dataset_prior: float = 0.0
    debate: dict[str, Any] = field(default_factory=dict)
    priority_score: float = 0.0
    issue_category: str = "other"
    evidence_quality: float = 0.0
    suggestion_actionability: float = 0.0
    generation_source: str = "review_agent"
    rule_id: str = ""
    display_threshold: float = 0.0
    confidence_basis: str = ""
    claim_impact: float = 0.0
    fixability: float = 0.0
    counterfactual: str = ""
    counterfactual_impact: str = "unknown"
    exists_confirmed: bool | None = None
    changes_main_conclusion: bool | None = None
    presentation_only: bool | None = None
    needs_new_experiment: bool | None = None
    alternative_explanation: str = ""
    evidence_locators: list[str] = field(default_factory=list)
    evidence_excerpt: str = ""
    audit_type: str = ""
    debate_route: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


_SEVERITY_TERMS = {
    "critical": ("错误", "无效", "矛盾", "泄漏", "安全漏洞", "无法证明", "不成立", "fatal", "invalid", "incorrect"),
    "major": ("缺乏", "不足", "遗漏", "未考虑", "不清晰", "不公平", "missing", "insufficient", "unclear", "unfair"),
}
_DIMENSION_IMPACT = {
    "methodology": 1.0,
    "experiment": 1.0,
    "novelty": 0.9,
    "theory_depth": 0.9,
    "skeptic": 0.85,
    "reproducibility": 0.75,
    "related_work": 0.7,
    "structure_logic": 0.65,
    "writing_format": 0.55,
    "writing": 0.55,
    "ethics": 0.75,
    "deep_dive": 0.85,
    "patch": 0.75,
}
_VERIFIER_BATCH_SIZE = 8
_PATTERN_RECALL_DIMENSIONS = (
    "methodology", "novelty", "experiment", "writing", "related_work",
    "reproducibility", "ethics", "writing_format", "structure_logic",
    "theory_depth",
)


def _contains_chinese(text: str) -> bool:
    return bool(re.search(r"[\u4e00-\u9fff]", text))


def _infer_severity(text: str, dimension: str) -> str:
    lower = text.lower()
    if any(term in lower for term in _SEVERITY_TERMS["critical"]):
        return "critical"
    if any(term in lower for term in _SEVERITY_TERMS["major"]):
        return "major"
    if dimension in {"methodology", "experiment", "novelty", "theory_depth", "skeptic"}:
        return "major"
    return "minor"


def _infer_issue_category(text: str, dimension: str) -> str:
    """Stable user-facing taxonomy independent of free-form Qwen wording."""
    rules = (
        ("scope_coherence", r"题目|标题|摘要|贡献|范围|外推|结论|一致|重复|章节|逻辑"),
        ("theory_assumption", r"理论|收敛|复杂度|证明|最优|假设|边界|推导|目标函数"),
        ("experiment_coverage", r"实验|基线|消融|数据集|硬件|设备|场景|鲁棒|统计|显著|误差|置信区间"),
        ("system_cost", r"能耗|功耗|内存|时延|通信|同步|开销|成本|部署|端.{0,2}边.{0,2}云"),
        ("reproducibility", r"复现|代码|随机种子|超参数|配置|版本|实现细节"),
        ("safety_ethics", r"安全|隐私|伦理|偏见|受试者|知情同意|风险|幻觉"),
        ("visual_format", r"图|表|公式|算法|三线表|排版|编号|分辨率|字体|图例"),
        ("language_reference", r"术语|缩写|英文摘要|翻译|参考文献|著录|期刊|会议|页码"),
        ("related_work", r"相关工作|文献|引用|已有工作|最新工作|SOTA"),
    )
    for category, pattern in rules:
        if re.search(pattern, text, re.I):
            return category
    dimension_fallback = {
        "experiment": "experiment_coverage",
        "theory_depth": "theory_assumption",
        "reproducibility": "reproducibility",
        "ethics": "safety_ethics",
        "writing_format": "visual_format",
        "related_work": "related_work",
        "structure_logic": "scope_coherence",
    }
    return dimension_fallback.get(dimension, "other")


def _paired_suggestion_for_weakness(
    weakness: str,
    weakness_index: int,
    suggestions: list[str],
) -> str:
    """Choose the action item that should travel with a weakness.

    Dimension prompts usually ask reviewers to emit weaknesses and suggestions as
    parallel lists.  Expert-style suggestions often do not share many tokens with
    the weakness they fix (e.g. "理论深度不足" vs. "补充收敛性和复杂度证明"),
    so a pure semantic-similarity gate silently drops useful suggestions and
    hurts strict same-type alignment.  Prefer a semantic match when it is clear;
    otherwise preserve the same-order reviewer pairing.
    """
    if not suggestions:
        return ""

    best_similarity = 0.0
    best_suggestion = ""
    for suggestion in suggestions:
        similarity = semantic_similarity(weakness, suggestion)
        if similarity > best_similarity:
            best_similarity, best_suggestion = similarity, suggestion
    if best_similarity >= 0.12:
        return best_suggestion

    if 0 <= weakness_index < len(suggestions):
        return suggestions[weakness_index]
    return suggestions[0]


def _fallback_suggestion_from_weakness(weakness: str, dimension: str) -> str:
    """Create a conservative thesis-review action when a verified issue lacks one.

    The fallback is intentionally category-based and only used as an action item
    attached to an issue that still must pass evidence verification.  This keeps
    suggestions available for strict same-type alignment without inventing new
    criticisms.
    """
    text = weakness.strip()
    if not text:
        return ""
    if re.search(r"题目|标题", text):
        return "建议修正论文题目或章节标题，使其准确覆盖正文的核心研究对象、应用场景和技术范围。"
    if re.search(r"英文摘要|摘要|翻译|dissertation|paper", text, re.I):
        return "建议对中英文摘要进行逐句校对，统一贡献表述、术语翻译和学位论文相关英文表述。"
    if re.search(r"参考文献|文献格式|著录|会议名称|期刊|页码", text):
        return "建议按照学校或 GB/T 7714 等规范全面检查参考文献著录项、会议/期刊名称、页码和格式一致性。"
    if re.search(r"缩写|术语|全称|中英文|算法名|系统名", text):
        return "建议统一全文术语和缩写写法，并在首次出现时补充中英文全称或必要解释。"
    if re.search(r"理论|收敛|复杂度|证明|最优|目标函数|数学|推导|边界", text):
        return "建议补充相应算法或系统机制的理论分析，包括复杂度、收敛性、最优性边界或统一优化目标的推导。"
    if re.search(r"图|表|公式|算法\s*\d|算法编号|编号|三线表|标点|排版|格式", text):
        return "建议逐项检查相关图、表、公式和算法的编号、标题、正文引用与排版格式，并按学位论文规范统一修改。"
    if re.search(r"章节|结构|逻辑|小结|未来工作|展望|结论|绪论|重复|关系", text):
        return "建议调整章节组织和衔接说明，减少摘要、绪论与结论的重复，并将讨论和未来工作集中到结论展望部分。"
    if re.search(r"实验|测试|场景|硬件|设备|鲁棒|压力|基准|对比|消融|数据集|工业|极端|动态", text):
        return "建议补充更全面的实验验证，包括关键基线、消融实验、复杂动态场景、真实设备或极端条件下的鲁棒性测试。"
    if re.search(r"协同|联动|端到端|跨模块|统一|端.{0,2}边.{0,2}云|部署|工具链", text):
        return "建议增加跨模块联动或端到端部署验证，明确各子系统之间的协同机制和整体收益。"
    if re.search(r"能耗|功耗|内存|开销|延迟|通信|同步|成本", text):
        return "建议补充运行开销、内存峰值、通信同步、能耗或部署成本等指标，并分析其对实际应用的影响。"
    if dimension in {"writing_format", "writing"}:
        return "建议按照学位论文写作规范对相关表述、格式和引用进行专项校对。"
    if dimension == "structure_logic":
        return "建议进一步梳理章节之间的问题递进、方法关系和结论对应关系。"
    if dimension == "theory_depth":
        return "建议补充关键方法的理论依据、复杂度分析或适用边界说明。"
    return "建议围绕该问题补充具体证据、修改相关表述，并说明其对论文结论的影响。"


def build_candidate_pool(results: list[dict[str, Any]]) -> list[CandidateIssue]:
    raw: list[CandidateIssue] = []
    for result in results:
        dimension = str(result.get("dimensionId", "methodology"))
        structured_findings = result.get("findings", []) or []
        if structured_findings:
            for finding in structured_findings:
                if not isinstance(finding, dict):
                    continue
                text = str(finding.get("weakness", "") or finding.get("text", "")).strip()
                if not text:
                    continue
                suggestion = str(finding.get("suggestion", "")).strip()
                raw.append(CandidateIssue(
                    candidate_id=f"C{len(raw) + 1:03d}",
                    text=text,
                    dimension=dimension,
                    suggestion=suggestion or _fallback_suggestion_from_weakness(text, dimension),
                    source_dimensions=list(dict.fromkeys(
                        [dimension] + [
                            str(source) for source in (finding.get("source_dimensions", []) or [])
                            if str(source).strip()
                        ]
                    )),
                    source_count=max(1, int(finding.get("source_count", 1) or 1)),
                    severity=str(finding.get("severity", "")) or _infer_severity(text, dimension),
                    evidence=str(finding.get("evidence", ""))[:600],
                    issue_category=_infer_issue_category(text, dimension),
                ))
            continue
        suggestions = [str(item).strip() for item in result.get("suggestions", []) if str(item).strip()]
        for weakness_index, weakness in enumerate(result.get("weaknesses", []) or []):
            text = str(weakness).strip()
            if not text or "Unable to complete" in text or "Review process" in text:
                continue
            paired_suggestion = _paired_suggestion_for_weakness(
                text, weakness_index, suggestions,
            )
            if not paired_suggestion:
                paired_suggestion = _fallback_suggestion_from_weakness(text, dimension)
            raw.append(CandidateIssue(
                candidate_id=f"C{len(raw) + 1:03d}",
                text=text,
                dimension=dimension,
                suggestion=paired_suggestion,
                source_dimensions=[dimension],
                severity=_infer_severity(text, dimension),
                issue_category=_infer_issue_category(text, dimension),
            ))

    # Semantic clustering works for Chinese and English; retain the more specific
    # representative while recording independent-agent support.
    clusters: list[CandidateIssue] = []
    for candidate in raw:
        match = next(
            (kept for kept in clusters if semantic_similarity(candidate.text, kept.text) >= 0.58),
            None,
        )
        if match is None:
            clusters.append(candidate)
            continue
        match.source_count += 1
        for source in candidate.source_dimensions or [candidate.dimension]:
            if source not in match.source_dimensions:
                match.source_dimensions.append(source)
        if len(candidate.text) > len(match.text):
            match.text = candidate.text
            match.dimension = candidate.dimension
            if not match.suggestion:
                match.suggestion = _fallback_suggestion_from_weakness(
                    match.text, match.dimension,
                )
        if candidate.suggestion and len(candidate.suggestion) > len(match.suggestion):
            match.suggestion = candidate.suggestion
        if candidate.severity == "critical":
            match.severity = "critical"

    for index, candidate in enumerate(clusters, 1):
        candidate.candidate_id = f"C{index:03d}"
        if not candidate.suggestion:
            candidate.suggestion = _fallback_suggestion_from_weakness(
                candidate.text, candidate.dimension,
            )
    return clusters


def _merge_candidate_pool(
    candidates: list[CandidateIssue],
    additions: list[CandidateIssue],
) -> list[CandidateIssue]:
    """Merge recall candidates without losing their bound suggestion/evidence."""
    for candidate in additions:
        match = next(
            (
                kept for kept in candidates
                if semantic_similarity(candidate.text, kept.text) >= 0.58
            ),
            None,
        )
        if match is None:
            candidates.append(candidate)
            continue
        match.source_count += 1
        if candidate.dimension not in match.source_dimensions:
            match.source_dimensions.append(candidate.dimension)
        if len(candidate.text) > len(match.text):
            match.text = candidate.text
            match.dimension = candidate.dimension
        if len(candidate.suggestion) > len(match.suggestion):
            match.suggestion = candidate.suggestion
        if candidate.evidence and not match.evidence:
            match.evidence = candidate.evidence
        if candidate.generation_source not in match.generation_source:
            match.generation_source += "+" + candidate.generation_source
        if candidate.rule_id and not match.rule_id:
            match.rule_id = candidate.rule_id
        if candidate.audit_type and not match.audit_type:
            match.audit_type = candidate.audit_type
        if candidate.severity == "critical":
            match.severity = "critical"
    for index, candidate in enumerate(candidates, 1):
        candidate.candidate_id = f"C{index:03d}"
    return candidates


def generate_pattern_recall_candidates(
    issue_index: IssuePatternIndex,
    evidence_map: EvidenceMap,
    client: Any | None,
    model: str | None,
    target_paper_text: str,
    max_candidates: int = 12,
) -> list[CandidateIssue]:
    """Use expert-dataset patterns as questions, then ground answers in this paper."""
    if client is None or not issue_index.cards:
        return []

    pattern_cards: list[dict[str, Any]] = []
    seen: set[str] = set()
    query = target_paper_text[:16000]
    for dimension in _PATTERN_RECALL_DIMENSIONS:
        for score, card in issue_index.retrieve(
            query=query,
            dimension=dimension,
            target_paper_text=target_paper_text,
            k=3,
        ):
            key = re.sub(r"\s+", "", card.text)
            if key in seen:
                continue
            seen.add(key)
            pattern_cards.append({
                "dimension": card.dimension,
                "pattern": card.text,
                "prior": round(score, 3),
            })
    if not pattern_cards:
        return []

    pattern_cards = pattern_cards[:24]
    pattern_text = "\n".join(
        f"- [{item['dimension']}] {item['pattern']}"
        for item in pattern_cards
    )
    evidence_prompt = evidence_map.to_prompt(
        max_chars=18000,
        query=" ".join(item["pattern"] for item in pattern_cards),
    )
    system = (
        "你是学位论文专家意见召回员。给出的模式来自其他论文，只能作为检查问题，"
        "绝不是当前论文的事实。你必须在当前论文证据中重新核查；没有明确证据就不要输出。"
        "只输出有效JSON，所有文字使用简体中文。"
    )
    prompt = (
        f"## 当前论文证据\n{evidence_prompt}\n\n"
        f"## 其他论文专家问题模式\n{pattern_text}\n\n"
        "请找出主评审可能漏掉、且能由当前论文明确证据支持的问题。"
        "每项必须绑定不足、建议和证据；建议必须直接修复同一问题，并以“建议”开头。"
        f"最多输出{max_candidates}项，同一维度最多2项，不要凑数。\n"
        '{"findings":[{"dimension":"experiment","weakness":"具体不足",'
        '"suggestion":"建议……","evidence":"页码/章节/图表及事实",'
        '"confidence":0.0,"severity":"critical|major|minor"}]}'
    )
    try:
        response = client.chat(
            messages=[{"role": "user", "content": prompt}],
            system=system,
            model=model if model else None,
            max_tokens=4096,
            temperature=0.1,
            json_mode=True,
        )
        data = _extract_json(response.content)
    except Exception:
        return []

    findings = data.get("findings", []) if isinstance(data, dict) else []
    output: list[CandidateIssue] = []
    for item in findings:
        if not isinstance(item, dict):
            continue
        dimension = str(item.get("dimension", ""))
        weakness = str(item.get("weakness", "")).strip()
        suggestion = str(item.get("suggestion", "")).strip()
        evidence = str(item.get("evidence", "")).strip()
        if dimension not in _PATTERN_RECALL_DIMENSIONS or not weakness or not evidence:
            continue
        severity = str(item.get("severity", "major"))
        if severity not in {"critical", "major", "minor"}:
            severity = _infer_severity(weakness, dimension)
        output.append(CandidateIssue(
            candidate_id=f"P{len(output) + 1:03d}",
            text=weakness[:500],
            dimension=dimension,
            suggestion=(suggestion or _fallback_suggestion_from_weakness(
                weakness, dimension,
            ))[:500],
            source_dimensions=[dimension, "dataset_pattern_recall"],
            severity=severity,
            evidence=evidence[:600],
            issue_category=_infer_issue_category(weakness, dimension),
        ))
        if len(output) >= max_candidates:
            break
    return output


def _extract_json(content: str) -> Any:
    raw = content.strip()
    if raw.startswith("```"):
        raw = raw.split("\n", 1)[-1]
        raw = raw.rsplit("```", 1)[0]
    return json.loads(raw)


def _heuristic_verify(candidate: CandidateIssue, evidence_text: str) -> None:
    evidence_tokens = semantic_tokens(evidence_text)
    candidate_tokens = semantic_tokens(candidate.text)
    overlap = len(candidate_tokens & evidence_tokens) / max(len(candidate_tokens), 1)
    has_locator = bool(re.search(
        r"§|第\s*\d|图\s*\d|表\s*\d|Fig\.?\s*\d|Table\s*\d|Page\s*\d",
        candidate.text,
        re.IGNORECASE,
    ))
    candidate.evidence_confidence = min(0.82, 0.32 + overlap * 0.45 + (0.12 if has_locator else 0.0))
    candidate.verdict = "supported" if candidate.evidence_confidence >= 0.62 else "uncertain"


def verify_candidates(
    candidates: list[CandidateIssue],
    evidence_map: EvidenceMap,
    client: Any | None,
    model: str | None,
) -> list[CandidateIssue]:
    if not candidates:
        return candidates
    for candidate in candidates:
        candidate_evidence = evidence_map.to_prompt(max_chars=6000, query=candidate.text)
        _heuristic_verify(candidate, candidate_evidence)

    if client is None:
        return candidates

    system = (
        "You are an evidence verifier, not a reviewer. Check each candidate issue "
        "against the supplied current-paper evidence. A cited page/section alone is not enough: "
        "the evidence must entail the same target, scope, and polarity. Claims of absence require "
        "evidence that the relevant paper scope was inspected. Return JSON only. "
        "All textual fields, especially evidence, corrected_text and corrected_suggestion, "
        "must be written in Simplified Chinese; "
        "retain English only for necessary model names, dataset names, abbreviations, and symbols."
    )
    batch_size = _VERIFIER_BATCH_SIZE
    for batch_start in range(0, len(candidates), batch_size):
        batch = candidates[batch_start:batch_start + batch_size]
        evidence_prompt = evidence_map.to_prompt(
            max_chars=10000,
            query=" \n".join(candidate.text for candidate in batch),
        )
        payload = [
            {
                "id": candidate.candidate_id,
                "issue": candidate.text,
                "dimension": candidate.dimension,
                "suggestion": candidate.suggestion,
                "candidate_evidence": candidate.evidence,
            }
            for candidate in batch
        ]
        prompt = (
            f"{evidence_prompt}\n\nCandidate issues:\n{json.dumps(payload, ensure_ascii=False)}\n\n"
            "Return {\"verifications\":[{\"id\":\"C001\","
            "\"verdict\":\"supported|uncertain|contradicted\","
            "\"confidence\":0.0,\"severity\":\"critical|major|minor\","
            "\"evidence\":\"用简体中文给出精确页码/章节及支持或反驳事实\","
            "\"corrected_text\":\"用简体中文给出精确问题，或留空\","
            "\"corrected_suggestion\":\"用‘建议’开头，给出与该问题同一对象的可操作建议，或留空\"}]}"
        )
        try:
            response = client.chat(
                messages=[{"role": "user", "content": prompt}],
                system=system,
                model=model if model else None,
                max_tokens=4096,
                temperature=0.1,
                json_mode=True,
            )
            data = _extract_json(response.content)
            by_id = {
                item.get("id"): item
                for item in data.get("verifications", [])
                if isinstance(item, dict)
            }
        except Exception:
            continue

        for candidate in batch:
            item = by_id.get(candidate.candidate_id)
            if not item:
                continue
            verdict = str(item.get("verdict", "uncertain"))
            if verdict not in {"supported", "uncertain", "contradicted"}:
                verdict = "uncertain"
            candidate.verdict = verdict
            candidate.evidence_confidence = max(0.0, min(1.0, float(item.get("confidence", 0.5))))
            severity = str(item.get("severity", candidate.severity))
            if severity in {"critical", "major", "minor"}:
                candidate.severity = severity
            candidate.evidence = str(item.get("evidence", ""))[:600]
            corrected = str(item.get("corrected_text", "")).strip()
            if corrected and (_contains_chinese(corrected) or not _contains_chinese(candidate.text)):
                candidate.text = corrected[:500]
            corrected_suggestion = str(item.get("corrected_suggestion", "")).strip()
            if corrected_suggestion and _contains_chinese(corrected_suggestion):
                candidate.suggestion = corrected_suggestion[:500]
            elif not candidate.suggestion:
                candidate.suggestion = _fallback_suggestion_from_weakness(
                    candidate.text, candidate.dimension,
                )
    return candidates


def reverify_borderline_candidates(
    candidates: list[CandidateIssue],
    evidence_map: EvidenceMap,
    client: Any | None,
    model: str | None,
    min_confidence: float,
    limit: int = 6,
) -> tuple[int, int]:
    """Give valuable near-threshold findings one focused evidence lookup."""
    if client is None:
        return 0, 0
    borderline = [
        candidate for candidate in candidates
        if candidate.verdict != "contradicted"
        and 0.35 <= candidate.evidence_confidence < min_confidence
    ]
    borderline.sort(
        key=lambda item: (
            item.dataset_prior,
            item.severity == "critical",
            item.source_count,
            item.evidence_confidence,
        ),
        reverse=True,
    )
    borderline = borderline[:limit]
    if not borderline:
        return 0, 0

    cases = []
    for candidate in borderline:
        cases.append({
            "id": candidate.candidate_id,
            "issue": candidate.text,
            "suggestion": candidate.suggestion,
            "focused_evidence": evidence_map.to_prompt(
                max_chars=3500,
                query=candidate.text + " " + candidate.suggestion,
            ),
        })
    system = (
        "你是学位论文证据复核员。以下候选在首轮证据检索中接近阈值。"
        "逐项判断当前论文证据是否真正支持同一对象、范围和极性；"
        "不能因有页码就判支持，声称缺失时必须确认检查范围。只输出有效JSON和简体中文。"
    )
    prompt = (
        f"{json.dumps(cases, ensure_ascii=False)}\n\n"
        '返回{"verifications":[{"id":"C001","verdict":"supported|uncertain|contradicted",'
        '"confidence":0.0,"evidence":"精确证据或不确定原因",'
        '"corrected_text":"必要时收窄后的不足","corrected_suggestion":"以建议开头的对应动作"}]}'
    )
    try:
        response = client.chat(
            messages=[{"role": "user", "content": prompt}],
            system=system,
            model=model if model else None,
            max_tokens=4096,
            temperature=0.0,
            json_mode=True,
        )
        data = _extract_json(response.content)
    except Exception:
        return 0, 0

    before = {
        candidate.candidate_id: candidate.evidence_confidence
        for candidate in borderline
    }
    by_id = {candidate.candidate_id: candidate for candidate in borderline}
    for item in data.get("verifications", []) if isinstance(data, dict) else []:
        if not isinstance(item, dict) or item.get("id") not in by_id:
            continue
        candidate = by_id[str(item["id"])]
        verdict = str(item.get("verdict", "uncertain"))
        if verdict in {"supported", "uncertain", "contradicted"}:
            candidate.verdict = verdict
        try:
            candidate.evidence_confidence = max(
                0.0, min(1.0, float(item.get("confidence", candidate.evidence_confidence)))
            )
        except (TypeError, ValueError):
            pass
        evidence = str(item.get("evidence", "")).strip()
        if evidence:
            candidate.evidence = evidence[:600]
        corrected = str(item.get("corrected_text", "")).strip()
        if corrected:
            candidate.text = corrected[:500]
        suggestion = str(item.get("corrected_suggestion", "")).strip()
        if suggestion:
            candidate.suggestion = suggestion[:500]
    recovered = sum(
        before[candidate.candidate_id] < min_confidence
        and candidate.verdict != "contradicted"
        and candidate.evidence_confidence >= min_confidence
        for candidate in borderline
    )
    return len(borderline), recovered


def _agent_position(
    client: Any,
    model: str | None,
    role: str,
    candidate: CandidateIssue,
    evidence_prompt: str,
) -> dict[str, Any]:
    if role == "defender":
        system = (
            "You are the paper-side defender in a review debate. Defend the paper only with supplied "
            "evidence; concede the issue when evidence is missing. Return JSON only. "
            "All textual fields must be written in Simplified Chinese."
        )
        task = "Find evidence that refutes, narrows, or already addresses this issue."
    elif role == "skeptic":
        system = (
            "You are a skeptical domain reviewer in a review debate. Test whether the issue is real, "
            "material, and not a generic complaint. Return JSON only. "
            "All textual fields must be written in Simplified Chinese."
        )
        task = "Find evidence supporting the issue and explain its impact; identify overstatement."
    else:
        system = (
            "You are the impact and remediation assessor in a heterogeneous review committee. "
            "Judge whether the issue materially affects the paper's claims, validity, reproducibility, "
            "or readability, and whether the proposed suggestion directly fixes the same issue. "
            "Use only supplied evidence. Return JSON only. All textual fields must be in Simplified Chinese."
        )
        task = (
            "Assess materiality and produce one concrete, location-aware revision action. "
            "Reject cosmetic or generic advice when it does not affect the review judgment."
        )
    prompt = (
        f"{evidence_prompt}\n\nIssue: {candidate.text}\nCurrent evidence: {candidate.evidence}\n"
        f"Current suggestion: {candidate.suggestion}\n"
        f"Task: {task}\n"
        "Return {\"position\":\"support|oppose|narrow|uncertain\","
        "\"evidence\":\"page/section facts\",\"confidence\":0.0,\"reason\":\"...\","
        "\"impact\":\"claim_validity|reproducibility|system_value|clarity|cosmetic\","
        "\"final_suggestion\":\"以建议开头、直接修复同一问题的动作\",\"actionability\":0.0}."
    )
    response = client.chat(
        messages=[{"role": "user", "content": prompt}],
        system=system,
        model=model if model else None,
        max_tokens=1536,
        temperature=0.2,
        json_mode=True,
    )
    data = _extract_json(response.content)
    return data if isinstance(data, dict) else {}


def _chair_decision(
    client: Any,
    model: str | None,
    candidate: CandidateIssue,
    defender: dict[str, Any],
    skeptic: dict[str, Any],
    assessor: dict[str, Any],
    evidence_prompt: str,
) -> dict[str, Any]:
    system = (
        "You are the senior review chair of a heterogeneous four-agent committee. Resolve the paper "
        "defender, domain skeptic, and impact/remediation assessor using only paper evidence. Reject "
        "generic or ungrounded issues and cosmetic issues incorrectly labeled as major. Return JSON only. "
        "All textual fields, including final_text, final_suggestion, evidence, and rationale, must be in Simplified Chinese."
    )
    prompt = (
        f"Evidence map:\n{evidence_prompt}\n\nIssue: {candidate.text}\n"
        f"Current suggestion: {candidate.suggestion}\n"
        f"Defender: {json.dumps(defender, ensure_ascii=False)}\n"
        f"Skeptic: {json.dumps(skeptic, ensure_ascii=False)}\n"
        f"Impact/remediation assessor: {json.dumps(assessor, ensure_ascii=False)}\n"
        "The final_text is a factual criticism: it must not introduce numeric values, datasets, "
        "methods, or experimental settings absent from the issue and evidence. Proposed new settings "
        "may appear only in final_suggestion and must be phrased as examples.\n"
        "Return {\"decision\":\"accept|narrow|reject\",\"confidence\":0.0,"
        "\"severity\":\"critical|major|minor\",\"final_text\":\"...\","
        "\"final_suggestion\":\"以建议开头的对应修改动作\","
        "\"evidence\":\"...\",\"rationale\":\"...\","
        "\"counterfactual\":\"若不修复该问题，对主要结论的影响\","
        "\"counterfactual_impact\":\"high|medium|low\","
        "\"exists\":true,\"changes_main_conclusion\":true,"
        "\"is_only_presentation_issue\":false,\"needs_new_experiment\":true,"
        "\"alternative_explanation\":\"...\"}."
    )
    response = client.chat(
        messages=[{"role": "user", "content": prompt}],
        system=system,
        model=model if model else None,
        max_tokens=2048,
        temperature=0.1,
        json_mode=True,
    )
    data = _extract_json(response.content)
    return data if isinstance(data, dict) else {}


def _debate_agreement(*positions: dict[str, Any]) -> tuple[float, str]:
    votes = [
        str(position.get("position", "uncertain"))
        for position in positions
        if isinstance(position, dict)
    ]
    if not votes:
        return 0.0, "low"
    counts = {vote: votes.count(vote) for vote in set(votes)}
    score = round(max(counts.values()) / len(votes), 3)
    label = "high" if score >= 0.99 else "medium" if score >= 0.66 else "low"
    return score, label


_NUMERIC_FACT_RE = re.compile(
    r"\d+(?:\.\d+)?\s*(?:%|倍|Mbps|Gbps|MB|GB|KB|ms|s|秒|分钟|Hz|次|个)?",
    re.I,
)


def _unsupported_numeric_facts(final_text: str, source_text: str) -> list[str]:
    """Return numeric facts introduced by the chair but absent from evidence."""
    source_numbers = {
        re.sub(r"\s+", "", item).lower()
        for item in _NUMERIC_FACT_RE.findall(source_text)
    }
    final_numbers = {
        re.sub(r"\s+", "", item).lower()
        for item in _NUMERIC_FACT_RE.findall(final_text)
    }
    # Chapter/table locators are structural references rather than result facts.
    def substantive(item: str) -> bool:
        return bool(re.search(r"%|倍|ms|秒|分钟|mb|gb|kb|mbps|gbps|hz|次|个", item, re.I))
    return sorted(item for item in final_numbers - source_numbers if substantive(item))


def run_targeted_debates(
    candidates: list[CandidateIssue],
    evidence_map: EvidenceMap,
    client: Any | None,
    model: str | None,
    max_debates: int = 5,
) -> int:
    if client is None or max_debates <= 0:
        return 0
    eligible = [
        candidate for candidate in candidates
        if candidate.verdict != "contradicted"
        and candidate.severity in {"critical", "major"}
        and (
            candidate.verdict == "uncertain"
            or 0.35 <= candidate.evidence_confidence <= 0.78
            or candidate.source_count > 1
            # High-confidence findings can still be the most consequential
            # review issues.  Send claim-validity candidates through the
            # committee as well, rather than treating Debate only as a rescue
            # path for borderline confidence.
            or _claim_impact(candidate) >= 0.75
        )
    ]
    eligible.sort(
        key=lambda item: (
            item.severity == "critical",
            _claim_impact(item),
            item.source_count,
            item.dataset_prior,
            1.0 - item.evidence_confidence,
        ),
        reverse=True,
    )
    debated = 0
    for candidate in eligible[:max_debates]:
        preliminary_impact = _claim_impact(candidate)
        candidate.debate_route = (
            "critical" if candidate.severity == "critical"
            else "claim_impact" if preliminary_impact >= 0.75
            else "multi_source" if candidate.source_count > 1
            else "borderline_confidence"
        )
        evidence_prompt = evidence_map.to_prompt(max_chars=14000, query=candidate.text)
        try:
            with ThreadPoolExecutor(max_workers=3) as pool:
                defender_future = pool.submit(
                    _agent_position, client, model, "defender", candidate, evidence_prompt
                )
                skeptic_future = pool.submit(
                    _agent_position, client, model, "skeptic", candidate, evidence_prompt
                )
                assessor_future = pool.submit(
                    _agent_position, client, model, "impact_editor", candidate, evidence_prompt
                )
                defender = defender_future.result()
                skeptic = skeptic_future.result()
                assessor = assessor_future.result()
            chair = _chair_decision(
                client, model, candidate, defender, skeptic, assessor, evidence_prompt,
            )
        except Exception:
            continue
        decision = str(chair.get("decision", "narrow"))
        agreement_score, agreement_label = _debate_agreement(
            defender, skeptic, assessor,
        )
        candidate.debate = {
            "agent_count": 4,
            "roles": ["paper_defender", "domain_skeptic", "impact_editor", "senior_chair"],
            "defender": defender,
            "skeptic": skeptic,
            "impact_editor": assessor,
            "chair": chair,
            "agreement_score": agreement_score,
            "agreement_label": agreement_label,
        }
        if decision == "reject":
            candidate.verdict = "contradicted"
        else:
            candidate.verdict = "supported" if decision == "accept" else "uncertain"
            final_text = str(chair.get("final_text", "")).strip()
            unsupported_numbers = _unsupported_numeric_facts(
                final_text,
                " ".join((candidate.text, candidate.evidence, str(chair.get("evidence", "")))),
            )
            if unsupported_numbers:
                candidate.debate["chair_final_text_rejected"] = {
                    "reason": "主席结论引入了证据中不存在的数值事实",
                    "unsupported_numeric_facts": unsupported_numbers,
                }
            elif final_text and (_contains_chinese(final_text) or not _contains_chinese(candidate.text)):
                candidate.text = final_text[:500]
            evidence = str(chair.get("evidence", "")).strip()
            if evidence and (_contains_chinese(evidence) or not _contains_chinese(candidate.text)):
                candidate.evidence = evidence[:600]
            final_suggestion = str(chair.get("final_suggestion", "")).strip()
            if final_suggestion and _contains_chinese(final_suggestion):
                candidate.suggestion = final_suggestion[:500]
            if str(chair.get("counterfactual", "")).strip():
                candidate.counterfactual = str(chair.get("counterfactual"))[:300]
            if str(chair.get("counterfactual_impact", "")) in {"high", "medium", "low"}:
                candidate.counterfactual_impact = str(chair.get("counterfactual_impact"))
            for field_name, chair_name in (
                ("exists_confirmed", "exists"),
                ("changes_main_conclusion", "changes_main_conclusion"),
                ("presentation_only", "is_only_presentation_issue"),
                ("needs_new_experiment", "needs_new_experiment"),
            ):
                if isinstance(chair.get(chair_name), bool):
                    setattr(candidate, field_name, chair[chair_name])
            if str(chair.get("alternative_explanation", "")).strip():
                candidate.alternative_explanation = str(chair.get("alternative_explanation"))[:300]
            try:
                candidate.evidence_confidence = max(
                    0.0, min(1.0, float(chair.get("confidence", candidate.evidence_confidence)))
                )
            except (TypeError, ValueError):
                pass
            severity = str(chair.get("severity", candidate.severity))
            if severity in {"critical", "major", "minor"}:
                candidate.severity = severity
            # The chair is not allowed to erase genuine committee
            # disagreement.  A low-agreement acceptance remains a candidate,
            # but is downgraded for focused re-checking instead of entering the
            # formal findings as if consensus had been reached.
            if candidate.exists_confirmed is False:
                candidate.verdict = "contradicted"
            elif decision == "accept" and agreement_score < 0.66:
                candidate.verdict = "uncertain"
                candidate.evidence_confidence = min(
                    candidate.evidence_confidence, 0.74,
                )
                candidate.debate["consensus_calibration"] = (
                    "委员会角色分歧较大，主席接受结论降级为待复核候选"
                )
            if candidate.presentation_only is True and candidate.severity in {"critical", "major"}:
                candidate.severity = "minor"
        debated += 1
    return debated


_ABSENCE_CLAIM_RE = re.compile(
    r"缺少|缺乏|未报告|未说明|未给出|未提供|未讨论|未考虑|没有|不足|不完整|遗漏"
)
_EVIDENCE_LOCATOR_RE = re.compile(
    r"§\s*\d|第\s*\d+(?:\.\d+)*\s*[章节]?|图\s*\d|表\s*\d|公式\s*\d|算法\s*\d|"
    r"Page\s*\d|Line\s*\d|第\s*\d+\s*行|Fig(?:ure)?\.?\s*\d|Table\s*\d|Section\s*\d|Eq(?:uation)?\.?\s*\d|"
    r"E\d{3,}|V\d+(?:-\d+)?|摘要|目录|参考文献|致谢",
    re.I,
)
_EVIDENCE_UNCERTAINTY_RE = re.compile(
    r"无法确认|无法根据|不能确认|未包含|未提供完整|需查阅|需要查阅|需核实|"
    r"请提供|证据不足以|无法直接|未能找到|可能.*但无法",
    re.I,
)
_GENERIC_SUGGESTION_RE = re.compile(
    r"请提供.*(?:以便|从而).*核实|进一步(?:加强|完善|改进)(?:相关)?(?:内容|问题|工作)?[。.!！]?$|"
    r"对应证据位置修正|以建议开头的对应动作|建议改善论文|建议进一步研究",
    re.I,
)


_EVIDENCE_LOCATOR_EXTRACT_RE = re.compile(
    r"E\d{3,}|V\d+(?:-\d+)?|§\s*\d+(?:\.\d+)*|"
    r"第\s*\d+(?:\.\d+)*\s*[章节页行]?|"
    r"(?:图|表|公式|算法)\s*\d+(?:[.\-]\d+)*|"
    r"(?:Page|Line|Section|Fig(?:ure)?\.?|Table|Eq(?:uation)?\.?)\s*\d+(?:[.\-]\d+)*|"
    r"摘要|目录|参考文献|致谢",
    re.I,
)


def _structure_candidate_evidence(candidate: CandidateIssue) -> None:
    """Expose evidence location and excerpt as stable machine-readable fields."""
    evidence = re.sub(r"\s+", " ", candidate.evidence).strip()
    candidate.evidence_excerpt = evidence[:420]
    candidate.evidence_locators = list(dict.fromkeys(
        match.group(0).strip()
        for match in _EVIDENCE_LOCATOR_EXTRACT_RE.finditer(evidence)
    ))[:8]


def calibrate_absence_claims(
    candidates: list[CandidateIssue],
    min_confidence: float,
) -> int:
    """Down-calibrate unsupported 'the paper lacks X' claims before display.

    Absence claims are valuable for expert recall but unusually easy to
    hallucinate. A verifier sentence such as “正文未见” is not enough unless it
    names the inspected section, figure, table, formula, or page.
    """
    calibrated = 0
    cap = max(0.0, min(0.54, min_confidence - 0.01))
    for candidate in candidates:
        if candidate.verdict == "contradicted" or not _ABSENCE_CLAIM_RE.search(candidate.text):
            continue
        evidence = candidate.evidence.strip()
        if len(evidence) >= 12 and _EVIDENCE_LOCATOR_RE.search(evidence):
            continue
        if candidate.evidence_confidence > cap:
            candidate.evidence_confidence = cap
            candidate.verdict = "uncertain"
            calibrated += 1
    return calibrated


def normalize_candidate_suggestions(candidates: list[CandidateIssue]) -> int:
    """Replace non-actions and evidence requests with a concrete repair action."""
    replaced = 0
    for candidate in candidates:
        suggestion = candidate.suggestion.strip()
        if len(suggestion) < 10 or _GENERIC_SUGGESTION_RE.search(suggestion):
            candidate.suggestion = _fallback_suggestion_from_weakness(
                candidate.text, candidate.dimension,
            )
            replaced += 1
            continue
        if not suggestion.startswith("建议"):
            candidate.suggestion = "建议" + suggestion.lstrip("：:，,。 ")
    return replaced


def calibrate_candidate_severity(candidates: list[CandidateIssue]) -> int:
    """Keep presentation nits below claim-validity and system-level issues."""
    changed = 0
    high_impact = re.compile(
        r"结论|有效性|公平性|不可复现|安全|隐私|错误|不成立|失效|外推|核心贡献|主要贡献"
    )
    critical = re.compile(r"数据泄漏|伦理违规|安全漏洞|结论不成立|实验无效|学术不端")
    for candidate in candidates:
        original = candidate.severity
        if candidate.severity == "critical" and not critical.search(candidate.text):
            candidate.severity = "major"
        if (
            candidate.issue_category in {"visual_format", "language_reference"}
            and not high_impact.search(candidate.text)
        ):
            candidate.severity = "minor"
        elif (
            candidate.issue_category == "scope_coherence"
            and not high_impact.search(candidate.text)
            and candidate.dimension in {"writing", "structure_logic", "writing_format"}
        ):
            candidate.severity = "minor"
        if candidate.severity != original:
            changed += 1
    return changed


def _evidence_quality(candidate: CandidateIssue) -> float:
    evidence = candidate.evidence.strip()
    if not evidence:
        return 0.0
    score = 0.45 if _EVIDENCE_LOCATOR_RE.search(evidence) else 0.1
    if re.search(r"\d+(?:\.\d+)?\s*(?:%|倍|ms|s|MB|GB|次|个)", evidence, re.I):
        score += 0.2
    if len(evidence) >= 30:
        score += 0.15
    if candidate.source_count >= 2:
        score += 0.1
    if candidate.debate:
        score += 0.1
    if _EVIDENCE_UNCERTAINTY_RE.search(evidence):
        score -= 0.45
    return round(max(0.0, min(1.0, score)), 3)


def _suggestion_actionability(candidate: CandidateIssue) -> float:
    suggestion = candidate.suggestion.strip()
    if not suggestion:
        return 0.0
    score = 0.25 if suggestion.startswith("建议") else 0.1
    if re.search(r"第\s*\d|§|图\s*\d|表\s*\d|公式\s*\d|算法\s*\d|摘要|附录|参考文献", suggestion):
        score += 0.25
    if re.search(r"补充|增加|删除|合并|改写|统一|报告|绘制|对比|推导|公开|说明|量化|校对", suggestion):
        score += 0.25
    if re.search(r"均值|标准差|置信区间|复杂度|消融|基线|参数|配置|边界|定义|编号|页码|数据", suggestion):
        score += 0.15
    if len(suggestion) >= 25:
        score += 0.1
    if _GENERIC_SUGGESTION_RE.search(suggestion):
        score -= 0.5
    return round(max(0.0, min(1.0, score)), 3)


def candidate_display_decision(
    candidate: CandidateIssue,
    min_confidence: float,
) -> tuple[bool, str]:
    """Apply a precision-first gate to a fully verified candidate."""
    # Dimension-specific calibration reflects observed error modes: novelty,
    # related-work absence, and theoretical sufficiency are harder to verify
    # from local excerpts than deterministic formatting checks.
    dimension_adjustment = {
        "novelty": 0.05,
        "related_work": 0.04,
        "theory_depth": 0.03,
        "ethics": 0.03,
        "writing_format": -0.02,
    }.get(candidate.dimension, 0.0)
    if candidate.generation_source == "deterministic_document_lint":
        dimension_adjustment = min(dimension_adjustment, -0.06)
    if candidate.source_count >= 2:
        dimension_adjustment -= 0.02
    required_confidence = max(0.45, min(0.95, min_confidence + dimension_adjustment))
    confidence_reasons = [f"全局阈值{min_confidence:.2f}"]
    if dimension_adjustment:
        confidence_reasons.append(f"维度/来源校准{dimension_adjustment:+.2f}")
    if _ABSENCE_CLAIM_RE.search(candidate.text) and not candidate.debate:
        required_confidence = min(0.95, required_confidence + 0.08)
        confidence_reasons.append("缺失类断言加严0.08")
    if candidate.issue_category == "related_work" and _ABSENCE_CLAIM_RE.search(candidate.text):
        required_confidence = min(0.95, required_confidence + 0.04)
        confidence_reasons.append("相关工作缺失判断再加严0.04")
    candidate.display_threshold = round(required_confidence, 3)
    candidate.confidence_basis = "；".join(confidence_reasons)
    if candidate.verdict == "contradicted":
        return False, "证据与候选问题矛盾"
    if candidate.evidence_confidence < required_confidence:
        return False, f"证据置信度低于风险校准阈值{required_confidence:.2f}"
    evidence = candidate.evidence.strip()
    if not evidence or not _EVIDENCE_LOCATOR_RE.search(evidence):
        return False, "证据缺少可核查的章节、页面、图表或证据单元定位"
    if _EVIDENCE_UNCERTAINTY_RE.search(evidence):
        return False, "证据文本仍明确表示无法确认该问题"
    if candidate.suggestion and candidate.suggestion_actionability < 0.35:
        return False, "对应建议缺少明确修改动作或可验收目标"
    if candidate.verdict == "supported":
        return True, "证据支持"
    # An uncertain candidate is only displayable when a full defender/skeptic/
    # chair debate narrowed it and still assigned strong evidence confidence.
    chair = candidate.debate.get("chair", {}) if isinstance(candidate.debate, dict) else {}
    if (
        candidate.verdict == "uncertain"
        and str(chair.get("decision", "")) == "narrow"
        and candidate.evidence_confidence >= max(0.78, required_confidence + 0.15)
    ):
        return True, "多智能体 Debate 收窄后保留"
    return False, "结论仍为 uncertain，未达到重点问题展示标准"


_HIGH_IMPACT_TERMS = re.compile(
    r"结论|主要贡献|主张|baseline|基线|消融|数据泄漏|泄漏|统计显著|显著性|不公平|泛化|有效性|可靠性|复现|validity|claim|leakage",
    re.IGNORECASE,
)


def _claim_impact(candidate: CandidateIssue) -> float:
    text = f"{candidate.text} {candidate.evidence}".lower()
    score = 0.45
    if _HIGH_IMPACT_TERMS.search(text):
        score += 0.30
    if candidate.dimension in {"methodology", "experiment", "skeptic", "reproducibility"}:
        score += 0.12
    if candidate.severity == "critical":
        score += 0.10
    if candidate.changes_main_conclusion is True:
        score += 0.15
    if candidate.presentation_only is True:
        score -= 0.20
    return min(1.0, round(score, 3))


def _fixability(candidate: CandidateIssue) -> float:
    text = f"{candidate.text} {candidate.suggestion}".lower()
    if any(term in text for term in ("补充", "增加", "报告", "明确", "add", "report", "clarify")):
        return 0.85
    if any(term in text for term in ("重做", "重新设计", "replace", "redesign")):
        return 0.55
    return 0.40


def _counterfactual(candidate: CandidateIssue) -> tuple[str, str]:
    if candidate.claim_impact >= 0.75:
        return "若不修复，该问题可能改变主要结论的可信度或适用边界。", "high"
    if candidate.claim_impact >= 0.55:
        return "若不修复，论文说服力和结论稳健性会下降，但未必推翻主要结论。", "medium"
    return "若不修复，主要影响表达完整性或复现便利性，不直接改变主要结论。", "low"


def rank_candidates(candidates: list[CandidateIssue]) -> list[CandidateIssue]:
    severity_weight = {"critical": 1.0, "major": 0.75, "minor": 0.4}
    verdict_weight = {"supported": 1.0, "uncertain": 0.58, "contradicted": 0.05}
    for candidate in candidates:
        _structure_candidate_evidence(candidate)
        severity = severity_weight.get(candidate.severity, 0.5)
        verdict = verdict_weight.get(candidate.verdict, 0.5)
        impact = _DIMENSION_IMPACT.get(candidate.dimension, 0.65)
        consensus = min(1.0, 0.5 + 0.15 * candidate.source_count)
        candidate.evidence_quality = _evidence_quality(candidate)
        candidate.suggestion_actionability = _suggestion_actionability(candidate)
        candidate.claim_impact = _claim_impact(candidate)
        candidate.fixability = _fixability(candidate)
        candidate.counterfactual, candidate.counterfactual_impact = _counterfactual(candidate)
        debate_bonus = 0.03 if candidate.debate and candidate.verdict == "supported" else 0.0
        weighted_sum = (
            0.22 * severity
            + 0.20 * candidate.evidence_confidence
            + 0.16 * verdict
            + 0.06 * impact
            + 0.08 * consensus
            + 0.05 * candidate.dataset_prior
            + 0.08 * candidate.suggestion_actionability
            + 0.09 * candidate.evidence_quality
            + 0.10 * candidate.claim_impact
            + 0.05 * candidate.fixability
        )
        # Component weights sum to 1.09.  The previous implementation capped
        # the unnormalised value at 1.0, causing many strong candidates to tie
        # at 100 and destroying the ordering needed for "fix these first".
        score = weighted_sum / 1.09 + debate_bonus
        candidate.priority_score = round(min(1.0, score) * 100, 1)
    return sorted(candidates, key=lambda item: item.priority_score, reverse=True)


def run_consensus_pipeline(
    results: list[dict[str, Any]],
    evidence_map: EvidenceMap,
    client: Any | None,
    model: str | None,
    target_paper_text: str,
    issue_index: IssuePatternIndex | None = None,
    enable_debate: bool = True,
    max_debates: int = 2,
    min_confidence: float = 0.65,
) -> dict[str, Any]:
    fast_consensus = os.environ.get("AUTO_REVIEW_FAST_CONSENSUS", "0") == "1"
    if fast_consensus:
        client = None
        enable_debate = False
    issue_index = issue_index or get_issue_pattern_index()
    raw_count = sum(
        len(result.get("findings", []) or result.get("weaknesses", []) or [])
        for result in results
    )
    candidates = build_candidate_pool(results)
    lint_findings = scan_document_lint(target_paper_text)
    lint_candidates = [
        CandidateIssue(
            candidate_id=f"L{index:03d}",
            text=str(item.get("text", "")),
            dimension=str(item.get("dimension", "writing_format")),
            suggestion=str(item.get("suggestion", "")),
            source_dimensions=["document_lint"],
            severity=str(item.get("severity", "minor")),
            evidence=str(item.get("evidence", ""))[:600],
            issue_category=_infer_issue_category(
                str(item.get("text", "")), str(item.get("dimension", "writing_format")),
            ),
            generation_source="deterministic_document_lint",
            rule_id=str(item.get("rule_id", "")),
        )
        for index, item in enumerate(lint_findings, 1)
        if str(item.get("text", "")).strip()
    ]
    candidates = _merge_candidate_pool(candidates, lint_candidates)
    pattern_candidates = generate_pattern_recall_candidates(
        issue_index=issue_index,
        evidence_map=evidence_map,
        client=client,
        model=model,
        target_paper_text=target_paper_text,
    )
    candidates = _merge_candidate_pool(candidates, pattern_candidates)
    claim_matrix = build_claim_evidence_matrix(evidence_map)
    claim_audit_findings = generate_claim_audit_findings(
        matrix=claim_matrix,
        evidence_map=evidence_map,
        client=client,
        model=model,
    )
    claim_audit_candidates = [
        CandidateIssue(
            candidate_id=f"A{index:03d}",
            text=str(item.get("weakness", "")),
            dimension=str(item.get("dimension", "experiment")),
            suggestion=str(item.get("suggestion", "")),
            source_dimensions=[
                str(item.get("dimension", "experiment")),
                "claim_evidence_audit",
            ],
            severity=str(item.get("severity", "major")),
            evidence=str(item.get("evidence", ""))[:700],
            evidence_confidence=float(item.get("confidence", 0.45) or 0.45),
            issue_category=_infer_issue_category(
                str(item.get("weakness", "")),
                str(item.get("dimension", "experiment")),
            ),
            generation_source="claim_evidence_audit",
            audit_type=str(item.get("issue_type", "")),
        )
        for index, item in enumerate(claim_audit_findings, 1)
        if str(item.get("weakness", "")).strip()
    ]
    candidates = _merge_candidate_pool(candidates, claim_audit_candidates)
    for candidate in candidates:
        candidate.dataset_prior = issue_index.prior_for_text(
            candidate.text,
            target_paper_text=target_paper_text,
        )
    verify_candidates(candidates, evidence_map, client, model)
    reverified, recovered = reverify_borderline_candidates(
        candidates,
        evidence_map,
        client,
        model,
        min_confidence=min_confidence,
    )
    for candidate in candidates:
        candidate.issue_category = _infer_issue_category(
            candidate.text, candidate.dimension,
        )
    suggestions_replaced = normalize_candidate_suggestions(candidates)
    severity_calibrated = calibrate_candidate_severity(candidates)
    debated = run_targeted_debates(
        candidates,
        evidence_map,
        client if enable_debate else None,
        model,
        max_debates=max_debates,
    )
    absence_calibrated = calibrate_absence_claims(candidates, min_confidence)
    suggestions_replaced += normalize_candidate_suggestions(candidates)
    for candidate in candidates:
        candidate.issue_category = _infer_issue_category(
            candidate.text, candidate.dimension,
        )
    severity_calibrated += calibrate_candidate_severity(candidates)
    ranked = rank_candidates(candidates)
    accepted = [candidate for candidate in ranked if candidate.verdict != "contradicted"]
    decisions = {
        candidate.candidate_id: candidate_display_decision(candidate, min_confidence)
        for candidate in ranked
    }
    retained = [candidate for candidate in ranked if decisions[candidate.candidate_id][0]]
    filtered = [candidate for candidate in ranked if not decisions[candidate.candidate_id][0]]
    filtered_reason_counts: dict[str, int] = {}
    for candidate in filtered:
        reason = decisions[candidate.candidate_id][1]
        filtered_reason_counts[reason] = filtered_reason_counts.get(reason, 0) + 1
    return {
        "verifiedFindings": [candidate.to_dict() for candidate in retained],
        "claimEvidenceMatrix": claim_matrix,
        "filteredFindings": [
            {
                "candidate_id": candidate.candidate_id,
                "dimension": candidate.dimension,
                "text": candidate.text,
                "suggestion": candidate.suggestion,
                "evidence": candidate.evidence,
                "evidence_locators": candidate.evidence_locators,
                "evidence_excerpt": candidate.evidence_excerpt,
                "severity": candidate.severity,
                "evidence_confidence": candidate.evidence_confidence,
                "claim_impact": candidate.claim_impact,
                "fixability": candidate.fixability,
                "counterfactual": candidate.counterfactual,
                "counterfactual_impact": candidate.counterfactual_impact,
                "reason": decisions[candidate.candidate_id][1],
            }
            for candidate in filtered
        ],
        "metrics": {
            "raw_candidates": raw_count,
            "semantic_clusters": len(candidates),
            "pattern_recall_candidates": len(pattern_candidates),
            "claim_audit_candidates": len(claim_audit_candidates),
            "claim_evidence_coverage": float(claim_matrix.get("coverage", 0.0)),
            "claim_evidence_needs_verification": int(
                claim_matrix.get("needs_verification_count", 0)
            ),
            "document_lint_candidates": len(lint_candidates),
            "document_lint_rule_counts": {
                rule_id: sum(candidate.rule_id == rule_id for candidate in lint_candidates)
                for rule_id in sorted({candidate.rule_id for candidate in lint_candidates if candidate.rule_id})
            },
            "document_lint_retained": sum(
                "deterministic_document_lint" in candidate.generation_source
                for candidate in retained
            ),
            "borderline_reverified": reverified,
            "borderline_recovered": recovered,
            "accepted_candidates": len(accepted),
            "retained_findings": len(retained),
            "filtered_low_confidence": sum(
                candidate.evidence_confidence < candidate.display_threshold
                for candidate in filtered
            ),
            "filtered_precision_gate": len(filtered),
            "filtered_reason_counts": filtered_reason_counts,
            "min_confidence": min_confidence,
            "risk_adjusted_thresholds": {
                f"{candidate.display_threshold:.2f}": sum(
                    other.display_threshold == candidate.display_threshold for other in ranked
                )
                for candidate in ranked
            },
            "supported_candidates": sum(candidate.verdict == "supported" for candidate in candidates),
            "uncertain_candidates": sum(candidate.verdict == "uncertain" for candidate in candidates),
            "contradicted_candidates": sum(candidate.verdict == "contradicted" for candidate in candidates),
            "debates_triggered": debated,
            "high_impact_debates": sum(
                bool(candidate.debate) and candidate.claim_impact >= 0.75
                for candidate in candidates
            ),
            "debate_disagreements": sum(
                bool(candidate.debate)
                and float(candidate.debate.get("agreement_score", 1.0)) < 0.66
                for candidate in candidates
            ),
            "debate_route_counts": {
                route: sum(candidate.debate_route == route for candidate in candidates)
                for route in sorted({candidate.debate_route for candidate in candidates if candidate.debate_route})
            },
            "hybrid_debate_agents": debated * 4,
            "hybrid_debate_roles": [
                "paper_defender", "domain_skeptic", "impact_editor", "senior_chair",
            ],
            "absence_claims_calibrated": absence_calibrated,
            "suggestions_replaced": suggestions_replaced,
            "severity_calibrated": severity_calibrated,
            "issue_category_counts": {
                category: sum(
                    candidate.issue_category == category for candidate in retained
                )
                for category in sorted({
                    candidate.issue_category for candidate in retained
                })
            },
            "evidence_locator_coverage": round(
                sum(bool(candidate.evidence_locators) for candidate in retained)
                / max(len(retained), 1),
                3,
            ),
            "verification_batches": (
                (len(candidates) + _VERIFIER_BATCH_SIZE - 1) // _VERIFIER_BATCH_SIZE
                if client is not None and candidates else 0
            ),
            "estimated_consensus_llm_calls": (
                (
                    (len(candidates) + _VERIFIER_BATCH_SIZE - 1) // _VERIFIER_BATCH_SIZE
                    if client is not None and candidates else 0
                )
                + (1 if client is not None and pattern_candidates else 0)
                + (1 if client is not None and claim_matrix.get("claims") else 0)
                + (1 if client is not None and reverified else 0)
                + debated * 4
            ),
            "fast_consensus": fast_consensus,
            "redundancy_reduction_pct": round(
                (
                    1
                    - len(candidates)
                    / max(raw_count + len(lint_candidates) + len(pattern_candidates), 1)
                ) * 100,
                1,
            ),
        },
    }
