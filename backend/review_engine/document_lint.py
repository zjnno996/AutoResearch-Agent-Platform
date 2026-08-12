"""Deterministic thesis lint that emits evidence-verifiable review candidates.

The scanner deliberately produces candidates, not final findings.  Every item is
still passed through the normal evidence verifier and confidence gate.
"""

from __future__ import annotations

import re
from collections import Counter, defaultdict
from typing import Any


_SENTENCE_RE = re.compile(r"[^。！？!?\n]{35,260}[。！？!?]")
_ABBREVIATION_RE = re.compile(r"\b[A-Z]{3,9}\b")
_COMMON_ABBREVIATIONS = {
    "AI", "ML", "DL", "LLM", "NLP", "CV", "GPU", "CPU", "RAM", "ROM",
    "API", "SDK", "OS", "IoT", "HTTP", "HTTPS", "TCP", "UDP", "PDF",
    "IEEE", "ACM", "CCF", "GB", "MB", "KB", "FPS", "FLOPS", "SOTA",
}


def _line_locator(text: str, offset: int) -> tuple[int, str]:
    line_no = text.count("\n", 0, offset) + 1
    line_start = text.rfind("\n", 0, offset) + 1
    line_end = text.find("\n", offset)
    if line_end < 0:
        line_end = len(text)
    excerpt = re.sub(r"\s+", " ", text[line_start:line_end]).strip()
    if not excerpt:
        excerpt = re.sub(r"\s+", " ", text[offset:offset + 100]).strip()
    return line_no, excerpt[:180]


def _candidate(
    *,
    rule_id: str,
    text: str,
    dimension: str,
    suggestion: str,
    evidence: str,
    severity: str = "minor",
) -> dict[str, Any]:
    return {
        "rule_id": rule_id,
        "text": text,
        "dimension": dimension,
        "suggestion": suggestion,
        "evidence": evidence,
        "severity": severity,
        "generation_source": "deterministic_document_lint",
    }


def _placeholder_findings(paper_text: str) -> list[dict[str, Any]]:
    pattern = re.compile(
        r"\[(?:导师|作者|姓名|学校|学院|实验室|项目|待补充)[^\]\n]{0,40}\]"
        r"|\b(?:TODO|TBD|FIXME)\b|(?<![A-Za-z])XXX(?![A-Za-z])",
        re.I,
    )
    matches = list(pattern.finditer(paper_text))
    if not matches:
        return []
    line_no, excerpt = _line_locator(paper_text, matches[0].start())
    samples = list(dict.fromkeys(match.group(0) for match in matches))[:4]
    return [_candidate(
        rule_id="unresolved_placeholder",
        text=f"论文仍保留未替换的占位符（如{'、'.join(samples)}），影响最终稿完整性。",
        dimension="writing_format",
        suggestion="建议在提交前全文搜索并替换所有占位符，同时复核致谢、作者信息和模板字段。",
        evidence=f"第{line_no}行原文：{excerpt}",
        severity="major",
    )]


def _thesis_naming_findings(paper_text: str) -> list[dict[str, Any]]:
    thesis_marker = re.search(r"博士学位论文|硕士学位论文|doctoral dissertation|master(?:'s)? thesis", paper_text, re.I)
    if not thesis_marker:
        return []
    english_abstract = re.search(
        r"(?is)(?:^|\n)\s*ABSTRACT\s*\n(.{0,12000}?)(?=\n\s*(?:KEY\s*WORDS|CONTENTS|CHAPTER\s+1|第\s*1\s*章)\b|\Z)",
        paper_text,
    )
    search_text = english_abstract.group(1) if english_abstract else paper_text
    base_offset = english_abstract.start(1) if english_abstract else 0
    match = re.search(r"\bthis paper\b", search_text, re.I)
    if not match:
        return []
    offset = base_offset + match.start()
    line_no, excerpt = _line_locator(paper_text, offset)
    return [_candidate(
        rule_id="thesis_called_paper",
        text="英文摘要使用“this paper”指代学位论文，学位论文体裁表述不够准确。",
        dimension="writing_format",
        suggestion="建议在英文摘要中根据学校规范改为“this dissertation”或“this thesis”，并全文统一相关指代。",
        evidence=f"英文摘要第{line_no}行原文：{excerpt}",
    )]


def _distributed_future_work_findings(paper_text: str) -> list[dict[str, Any]]:
    matches = list(re.finditer(
        r"(?im)^\s*(?:第?\s*\d+(?:\.\d+)*\s*[章节]?[、.\s]*)?"
        r"(?:讨论和未来工作|讨论与未来工作|讨论及未来工作|未来工作与讨论)\s*$",
        paper_text,
    ))
    if len(matches) < 2:
        return []
    locators = [_line_locator(paper_text, match.start())[0] for match in matches[:6]]
    return [_candidate(
        rule_id="distributed_future_work",
        text=f"“讨论/未来工作”分散出现在至少{len(matches)}处，容易造成章节结构重复和总结主线分散。",
        dimension="structure_logic",
        suggestion="建议保留各章必要的局限性小结，将跨章节的未来工作统一归纳到结论与展望章节。",
        evidence=f"第{'、'.join(str(item) for item in locators)}行分别出现讨论/未来工作标题。",
    )]


def _exact_repetition_findings(paper_text: str) -> list[dict[str, Any]]:
    occurrences: dict[str, list[tuple[int, str]]] = defaultdict(list)
    for match in _SENTENCE_RE.finditer(paper_text):
        sentence = re.sub(r"\s+", "", match.group(0))
        if len(sentence) < 35:
            continue
        if re.search(r"版权所有|学校代码|分类号|参考文献|http|doi", sentence, re.I):
            continue
        occurrences[sentence].append((match.start(), match.group(0).strip()))
    repeated = [items for items in occurrences.values() if len(items) >= 2]
    if not repeated:
        return []
    repeated.sort(key=lambda items: (len(items), len(items[0][1])), reverse=True)
    items = repeated[0]
    lines = [_line_locator(paper_text, offset)[0] for offset, _ in items[:4]]
    excerpt = re.sub(r"\s+", " ", items[0][1])[:120]
    return [_candidate(
        rule_id="exact_long_sentence_repetition",
        text=f"正文存在跨位置完全重复的较长表述，至少在{len(items)}处重复，可能造成摘要、贡献或结论内容冗余。",
        dimension="structure_logic",
        suggestion="建议比较重复段落的功能，只保留必要信息，并让结论侧重综合发现、局限性和新增归纳。",
        evidence=f"第{'、'.join(str(item) for item in lines)}行出现相同表述：“{excerpt}”。",
    )]


def _abbreviation_findings(paper_text: str) -> list[dict[str, Any]]:
    # This rule targets thesis-format requirements. Conference papers commonly
    # contain venue, benchmark, and bibliography tokens that should not be
    # expanded (ICLR, CIFAR10, PMLR, etc.).
    if not re.search(
        r"博士学位论文|硕士学位论文|doctoral dissertation|master(?:'s)? thesis",
        paper_text,
        re.I,
    ):
        return []
    references = re.search(r"(?im)^\s*(?:参考文献|REFERENCES)\s*$", paper_text)
    references_offset = references.start() if references else len(paper_text)
    counts = Counter(_ABBREVIATION_RE.findall(paper_text))
    candidates: list[tuple[int, str, re.Match[str]]] = []
    for abbreviation, count in counts.items():
        if count < 5 or abbreviation in _COMMON_ABBREVIATIONS:
            continue
        definition_patterns = (
            rf"[A-Za-z][A-Za-z\s/-]{{4,80}}\(\s*{re.escape(abbreviation)}\s*\)",
            rf"[\u4e00-\u9fff][^（）\n]{{2,40}}（\s*{re.escape(abbreviation)}\s*）",
            rf"\b{re.escape(abbreviation)}\b\s*[（(][^）)\n]{{3,80}}[）)]",
        )
        if any(re.search(pattern, paper_text) for pattern in definition_patterns):
            continue
        first = re.search(rf"\b{re.escape(abbreviation)}\b", paper_text)
        if first and first.start() < references_offset:
            candidates.append((count, abbreviation, first))
    candidates.sort(reverse=True, key=lambda item: item[0])
    findings: list[dict[str, Any]] = []
    for count, abbreviation, first in candidates[:2]:
        line_no, excerpt = _line_locator(paper_text, first.start())
        findings.append(_candidate(
            rule_id="undefined_abbreviation",
            text=f"缩写“{abbreviation}”在全文出现{count}次，但首次出现附近未检出中英文全称或解释。",
            dimension="writing_format",
            suggestion=f"建议在“{abbreviation}”首次出现处补充中英文全称，并统一目录、图表和正文中的写法。",
            evidence=f"第{line_no}行首次出现：{excerpt}",
        ))
    return findings


def scan_document_lint(paper_text: str) -> list[dict[str, Any]]:
    """Return high-precision candidates with line-level evidence locators."""
    if not paper_text.strip():
        return []
    findings: list[dict[str, Any]] = []
    for scanner in (
        _placeholder_findings,
        _thesis_naming_findings,
        _distributed_future_work_findings,
        _exact_repetition_findings,
        _abbreviation_findings,
    ):
        findings.extend(scanner(paper_text))
    return findings
