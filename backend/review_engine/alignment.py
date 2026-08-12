"""Classify auto-review findings against human review points."""

from __future__ import annotations

import re
from typing import Any


_CITATION_RE = re.compile(
    r"§\s*\d|第\s*\d+\s*[章节]|图\s*\d|表\s*\d|"
    r"Fig(?:ure)?\.?\s*\d|Table\s*\d|Section\s*\d|Page\s*\d|Eq(?:uation)?\.?\s*\d",
    re.IGNORECASE,
)


def point_types_compatible(human_point: dict[str, Any], auto_point: dict[str, Any]) -> bool:
    """Require exact point type so strengths cannot match criticism or advice."""
    return str(human_point.get("type", "")) == str(auto_point.get("type", ""))


def point_confidence(point: dict[str, Any]) -> float:
    explicit = point.get("confidence", point.get("evidence_confidence"))
    if explicit is not None:
        try:
            return max(0.0, min(1.0, float(explicit)))
        except (TypeError, ValueError):
            pass
    text = str(point.get("text", ""))
    if _CITATION_RE.search(text):
        return 0.68
    if len(text.strip()) >= 40:
        return 0.52
    return 0.35


def flatten_confident_auto_points(
    auto_result: dict[str, Any],
    min_confidence: float = 0.55,
) -> list[dict[str, Any]]:
    """Return cited strengths plus evidence-verified weaknesses/suggestions."""
    points: list[dict[str, Any]] = []
    meta = auto_result.get("meta") or {}
    findings = auto_result.get("verifiedFindings") or meta.get("verifiedFindings") or []

    for finding in findings:
        confidence = point_confidence(finding)
        if confidence < min_confidence:
            continue
        base = {
            "dimension": str(finding.get("dimension", "?")),
            "confidence": confidence,
            "verdict": str(finding.get("verdict", "uncertain")),
            "evidence": str(finding.get("evidence", "")),
            "candidate_id": str(finding.get("candidate_id", "")),
        }
        text = str(finding.get("text", "")).strip()
        if text:
            points.append({**base, "type": "weakness", "text": text})
        suggestion = str(finding.get("suggestion", "")).strip()
        if suggestion:
            points.append({**base, "type": "suggestion", "text": suggestion})

    # Strengths are not part of weakness consensus. Retain only evidence-anchored
    # strengths, with a conservative confidence estimate.
    for result in auto_result.get("dim_results", []) or []:
        dimension = str(result.get("dimensionId", "?"))
        for strength in result.get("strengths", []) or []:
            point = {"type": "strength", "text": str(strength), "dimension": dimension}
            confidence = point_confidence(point)
            if confidence >= min_confidence:
                points.append({**point, "confidence": confidence, "verdict": "supported"})

    return points


def build_usefulness_prompt(
    human_points: list[dict[str, Any]],
    auto_points: list[dict[str, Any]],
    unmatched_indices: list[int],
) -> str:
    human_text = "\n".join(
        f"[H{index}] ({point.get('type', '?')}) {str(point.get('text', ''))[:300]}"
        for index, point in enumerate(human_points)
    )
    auto_text = "\n".join(
        f"[A{index}] ({auto_points[index].get('type', '?')}; "
        f"confidence={point_confidence(auto_points[index]):.2f}) "
        f"{str(auto_points[index].get('text', ''))[:350]}"
        for index in unmatched_indices
    )
    return f"""你正在判断未命中人工专家意见的自动评审观点是否仍然有用。

人工专家意见：
{human_text}

未命中的自动评审观点：
{auto_text}

对每个 A 编号逐一判断。仅当观点满足以下全部条件时，useful=true：
1. 针对当前论文的具体内容，并有证据定位；
2. 相比人工意见包含实质性新增信息，而非换一种说法重复；
3. 技术相关、不与论文事实矛盾；
4. 可操作，或足以影响评审判断。

泛泛而谈、无证据的缺失断言、无实际影响的格式琐事、重复项和推测性意见均判为无用。
reason 必须使用简体中文；category 保留以下内部枚举值。

只返回 JSON：
{{"judgments":[{{"auto_idx":0,"useful":true,"confidence":0.82,
"reason":"该问题有明确表格证据且补充了人工意见未涉及的场景","category":"novel_issue|actionable_suggestion|extra_strength|generic|unsupported|duplicate"}}]}}"""


def select_auto_candidate_indices(
    human_points: list[dict[str, Any]],
    auto_points: list[dict[str, Any]],
    limit: int = 36,
) -> list[int]:
    """Retrieve same-type semantic candidates while retaining global indices."""
    if limit <= 0 or not auto_points:
        return []
    try:
        from review_data.issue_patterns import semantic_similarity
    except Exception:
        semantic_similarity = lambda left, right: 0.0  # type: ignore[assignment]

    selected: set[int] = set()
    aggregate_scores: dict[int, float] = {}
    for human in human_points:
        ranked: list[tuple[float, int]] = []
        for index, auto in enumerate(auto_points):
            if not point_types_compatible(human, auto):
                continue
            score = float(semantic_similarity(
                str(human.get("text", "")),
                str(auto.get("text", "")),
            ))
            score += 0.08 * point_confidence(auto)
            if auto.get("evidence"):
                score += 0.04
            aggregate_scores[index] = max(aggregate_scores.get(index, 0.0), score)
            ranked.append((score, index))
        ranked.sort(reverse=True)
        selected.update(index for _, index in ranked[:6])

    if len(selected) < limit:
        remaining = sorted(
            (
                (aggregate_scores.get(index, 0.0), index)
                for index in range(len(auto_points))
                if index not in selected
            ),
            reverse=True,
        )
        selected.update(index for _, index in remaining[:limit - len(selected)])

    return sorted(
        selected,
        key=lambda index: aggregate_scores.get(index, 0.0),
        reverse=True,
    )[:limit]


def build_recall_audit_prompt(
    human_points: list[dict[str, Any]],
    auto_points: list[dict[str, Any]],
    candidate_indices: list[int],
) -> str:
    """Build a narrow second-pass prompt for points missed by the broad judge."""
    human_text = "\n".join(
        f"[{index}] ({point.get('type', '?')}) {str(point.get('text', ''))[:350]}"
        for index, point in enumerate(human_points)
    )
    auto_text = "\n".join(
        f"[{index}] ({auto_points[index].get('type', '?')}; "
        f"confidence={point_confidence(auto_points[index]):.2f}) "
        f"[{auto_points[index].get('dimension', '?')}] "
        f"{str(auto_points[index].get('text', ''))[:320]}"
        for index in candidate_indices
    )
    return f"""你是专家意见对齐的严格复核员。以下人工意见在第一轮被判定为未覆盖。
本轮只用于发现第一轮的明显漏判，不能因为主题相近就判为覆盖。

待复核人工意见：
{human_text}

同类型语义检索候选（方括号编号是全局 auto_idx）：
{auto_text}

covered=true 必须同时满足：
1. 人工意见与自动观点类型完全一致；
2. 指向同一方法、章节、实验、图表或写作对象；
3. 核心事实、范围和极性一致，自动观点能够实质蕴含人工意见；
4. confidence >= 0.78；拿不准必须判 false。

只返回JSON：
{{"matches":[{{"human_idx":0,"auto_idx":3,"covered":true,
"confidence":0.88,"reason":"两者均明确指出同一表格缺少误差棒"}}]}}"""


def validate_recall_audit_match(
    human_point: dict[str, Any],
    auto_point: dict[str, Any],
    match: dict[str, Any],
    min_confidence: float = 0.78,
    min_similarity: float = 0.10,
) -> tuple[bool, str]:
    """Apply deterministic gates after the second Qwen judgment."""
    if not bool(match.get("covered")):
        return False, "judge_negative"
    if not point_types_compatible(human_point, auto_point):
        return False, "type_mismatch"
    try:
        confidence = float(match.get("confidence", 0.0))
    except (TypeError, ValueError):
        confidence = 0.0
    if confidence < min_confidence:
        return False, "low_judge_confidence"
    try:
        from review_data.issue_patterns import semantic_similarity
        similarity = semantic_similarity(
            str(human_point.get("text", "")),
            str(auto_point.get("text", "")),
        )
    except Exception:
        similarity = 0.0
    if similarity < min_similarity:
        return False, "insufficient_semantic_overlap"
    return True, "accepted"


def classify_alignment(
    human_points: list[dict[str, Any]],
    auto_points: list[dict[str, Any]],
    matches: list[dict[str, Any]],
    usefulness_judgments: list[dict[str, Any]] | None = None,
    min_judge_confidence: float = 0.55,
) -> dict[str, Any]:
    covered_human = {
        int(match["human_index"])
        for match in matches
        if match.get("covered") and isinstance(match.get("human_index"), int)
    }
    matched_auto = {
        int(match["auto_index"])
        for match in matches
        if match.get("covered") and isinstance(match.get("auto_index"), int)
    }
    human_by_auto: dict[int, list[int]] = {}
    for match in matches:
        if not match.get("covered"):
            continue
        ai, hi = match.get("auto_index"), match.get("human_index")
        if isinstance(ai, int) and isinstance(hi, int):
            human_by_auto.setdefault(ai, []).append(hi)

    judgments = {
        int(item["auto_idx"]): item
        for item in (usefulness_judgments or [])
        if isinstance(item, dict) and isinstance(item.get("auto_idx"), int)
    }

    matched = [
        {**auto_points[index], "auto_index": index, "matched_human_indices": human_by_auto.get(index, [])}
        for index in sorted(matched_auto)
        if 0 <= index < len(auto_points)
    ]
    missed = [
        {**point, "human_index": index}
        for index, point in enumerate(human_points)
        if index not in covered_human
    ]

    useful_unmatched: list[dict[str, Any]] = []
    unhelpful_unmatched: list[dict[str, Any]] = []
    for index, point in enumerate(auto_points):
        if index in matched_auto:
            continue
        judgment = judgments.get(index)
        if judgment is not None:
            judge_confidence = max(0.0, min(1.0, float(judgment.get("confidence", 0.0))))
            useful = bool(judgment.get("useful")) and judge_confidence >= min_judge_confidence
            detail = {
                **point,
                "auto_index": index,
                "usefulness_confidence": judge_confidence,
                "usefulness_reason": str(judgment.get("reason", "")),
                "usefulness_category": str(judgment.get("category", "")),
            }
        else:
            confidence = point_confidence(point)
            useful = (
                confidence >= 0.65
                and bool(_CITATION_RE.search(str(point.get("text", ""))) or point.get("evidence"))
                and len(str(point.get("text", "")).strip()) >= 25
            )
            detail = {
                **point,
                "auto_index": index,
                "usefulness_confidence": confidence,
                "usefulness_reason": "基于证据定位与可操作性的确定性兜底判断",
                "usefulness_category": "novel_issue" if useful else "unsupported",
            }
        (useful_unmatched if useful else unhelpful_unmatched).append(detail)

    return {
        "matched_auto_points": matched,
        "useful_unmatched_auto_points": useful_unmatched,
        "unhelpful_unmatched_auto_points": unhelpful_unmatched,
        "missed_human_points": missed,
        "counts": {
            "matched": len(matched),
            "useful_unmatched": len(useful_unmatched),
            "unhelpful_unmatched": len(unhelpful_unmatched),
            "missed_human": len(missed),
        },
    }
