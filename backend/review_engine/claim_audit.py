"""Claim-to-evidence coverage and contradiction recall for Auto Review.

The matrix is deterministic and auditable.  Qwen may use it to recall review
candidates, but every recalled issue still passes the shared verifier, Debate,
and confidence gate in :mod:`review_engine.consensus`.
"""

from __future__ import annotations

import json
import re
from typing import Any

from review_data.issue_patterns import semantic_similarity

from .evidence_map import EvidenceMap, EvidenceUnit


_STRONG_CLAIM_RE = re.compile(
    r"优于|提升|降低|减少|证明|表明|达到|显著|有效|"
    r"outperform|improv|reduc|significant|demonstrat|prove",
    re.I,
)
_NON_AUTHOR_CLAIM_SECTIONS = {
    "preamble", "related_work", "references", "acknowledgments",
    "目录", "参考文献", "致谢",
}
_PRIOR_WORK_ATTRIBUTION_RE = re.compile(
    r"^\s*(?:已有|现有|传统方法|相关研究|先前研究|文献\s*\[|"
    r"Prior\s+work|Existing\s+(?:work|methods?)|Previous\s+(?:work|studies))",
    re.I,
)


def _support_score(claim: EvidenceUnit, evidence: EvidenceUnit) -> float:
    score = semantic_similarity(claim.text, evidence.text)
    if evidence.kind == "visual":
        score += 0.08
    if evidence.numbers:
        score += 0.06
    if claim.section != evidence.section:
        score += 0.03
    return round(min(1.0, score), 3)


def build_claim_evidence_matrix(
    evidence_map: EvidenceMap,
    max_claims: int = 48,
) -> dict[str, Any]:
    """Map strong paper claims to independently retrievable evidence units."""
    claims = [
        unit for unit in evidence_map.units
        if unit.kind == "claim"
        and unit.section not in _NON_AUTHOR_CLAIM_SECTIONS
        and not _PRIOR_WORK_ATTRIBUTION_RE.search(unit.text)
        and _STRONG_CLAIM_RE.search(unit.text)
    ][:max_claims]
    rows: list[dict[str, Any]] = []
    for claim in claims:
        ranked = sorted(
            (
                (_support_score(claim, unit), unit)
                for unit in evidence_map.units
                if unit.unit_id != claim.unit_id
            ),
            key=lambda item: item[0],
            reverse=True,
        )
        supports = [
            {
                "unit_id": unit.unit_id,
                "section": unit.section,
                "score": score,
                "locators": unit.locators[:4],
                "excerpt": unit.text[:260],
            }
            for score, unit in ranked[:4]
            if score >= 0.20
        ]
        direct_quantitative = bool(claim.numbers and claim.locators)
        independently_supported = any(
            item["score"] >= 0.30 for item in supports
        )
        status = (
            "supported" if direct_quantitative or independently_supported
            else "needs_verification"
        )
        rows.append({
            "claim_id": claim.unit_id,
            "section": claim.section,
            "claim": claim.text[:420],
            "locators": claim.locators[:6],
            "numbers": claim.numbers[:8],
            "strong_claim": True,
            "status": status,
            "supporting_evidence": supports,
        })
    supported = sum(row["status"] == "supported" for row in rows)
    return {
        "claims": rows,
        "claim_count": len(rows),
        "supported_count": supported,
        "needs_verification_count": len(rows) - supported,
        "coverage": round(supported / max(len(rows), 1), 3),
    }


def generate_claim_audit_findings(
    matrix: dict[str, Any],
    evidence_map: EvidenceMap,
    client: Any | None,
    model: str | None,
    max_findings: int = 8,
) -> list[dict[str, Any]]:
    """Use Qwen to recall unsupported claims and cross-section/visual conflicts."""
    if client is None or not matrix.get("claims"):
        return []
    compact_rows = matrix["claims"][:32]
    visual_query = " ".join(str(row.get("claim", "")) for row in compact_rows)
    evidence = evidence_map.to_prompt(max_chars=18000, query=visual_query)
    system = (
        "你是论文 Claim–Evidence 审计员。只能依据当前论文证据判断，不得把检索不到"
        "直接等同于论文不存在。重点检查主要结论是否有实验支撑、摘要/正文/结论数字是否"
        "矛盾、图表与正文是否冲突、结论是否超出实验范围。只输出有效JSON和简体中文。"
    )
    prompt = (
        f"## Claim–Evidence矩阵\n{json.dumps(compact_rows, ensure_ascii=False)}\n\n"
        f"## 当前论文证据\n{evidence}\n\n"
        "只输出可以绑定精确Evidence Unit、章节、页码或图表的问题。"
        "缺失类问题必须说明检查了哪些相关章节/表格；数字矛盾必须列出冲突双方。"
        "每条建议必须直接修复对应不足并包含可验收产物。不要输出纯语言润色。\n"
        '{"findings":[{"dimension":"experiment|methodology|structure_logic|writing_format|reproducibility",'
        '"weakness":"具体不足","suggestion":"建议……","evidence":"E编号/章节/图表和原文事实",'
        '"issue_type":"unsupported_claim|cross_section_conflict|visual_text_conflict|scope_overreach",'
        '"confidence":0.0,"severity":"critical|major|minor"}]}\n'
        f"最多{max_findings}项，没有可靠问题返回空数组。"
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
        raw = response.content.strip()
        if raw.startswith("```"):
            raw = raw.split("\n", 1)[-1].rsplit("```", 1)[0]
        data = json.loads(raw)
    except Exception:
        return []
    allowed_dimensions = {
        "experiment", "methodology", "structure_logic",
        "writing_format", "reproducibility",
    }
    allowed_types = {
        "unsupported_claim", "cross_section_conflict",
        "visual_text_conflict", "scope_overreach",
    }
    output: list[dict[str, Any]] = []
    for item in data.get("findings", []) if isinstance(data, dict) else []:
        if not isinstance(item, dict):
            continue
        dimension = str(item.get("dimension", ""))
        weakness = str(item.get("weakness", "")).strip()
        suggestion = str(item.get("suggestion", "")).strip()
        evidence_text = str(item.get("evidence", "")).strip()
        issue_type = str(item.get("issue_type", ""))
        try:
            confidence = float(item.get("confidence", 0.0))
        except (TypeError, ValueError):
            confidence = 0.0
        if (
            dimension not in allowed_dimensions
            or issue_type not in allowed_types
            or not weakness or not suggestion or not evidence_text
            or confidence < 0.45
            or not re.search(
                r"E\d{3,}|V\d+(?:-\d+)?|第\s*\d|图\s*\d|表\s*\d|Page\s*\d|Section\s*\d",
                evidence_text,
                re.I,
            )
        ):
            continue
        if not suggestion.startswith("建议"):
            suggestion = "建议" + suggestion.lstrip("：:，,。 ")
        severity = str(item.get("severity", "major"))
        if severity not in {"critical", "major", "minor"}:
            severity = "major"
        output.append({
            "dimension": dimension,
            "weakness": weakness[:500],
            "suggestion": suggestion[:500],
            "evidence": evidence_text[:700],
            "confidence": max(0.0, min(1.0, confidence)),
            "severity": severity,
            "issue_type": issue_type,
        })
        if len(output) >= max_findings:
            break
    return output
