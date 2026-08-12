"""Local evidence-map construction for long academic papers.

This module deliberately uses deterministic parsing. It gives downstream LLM
agents a shared, inspectable evidence substrate before any critique is made.
"""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass, field
from typing import Any

from .pdf_utils import _parse_sections


_LOCATOR_RE = re.compile(
    r"(?:§\s*)?\d+(?:\.\d+){1,3}"
    r"|(?:图|表|公式|算法)\s*\d+(?:[.\-]\d+)*"
    r"|(?:Fig(?:ure)?\.?|Table|Eq(?:uation)?\.?|Algorithm)\s*\d+(?:[.\-]\d+)*",
    re.IGNORECASE,
)
_CLAIM_RE = re.compile(
    r"提出|设计|构建|实现|证明|表明|结果显示|结果表明|优于|提升|降低|减少|增加|达到"
    r"|we propose|we present|we develop|we show|results? (?:show|demonstrate)|outperform|improv|reduc",
    re.IGNORECASE,
)
_NUMBER_RE = re.compile(r"(?<!\w)[+-]?\d+(?:\.\d+)?\s*(?:%|倍|ms|s|GB|MB|KB|fps|req/s|tokens?/s)?", re.IGNORECASE)


@dataclass
class EvidenceUnit:
    unit_id: str
    section: str
    text: str
    locators: list[str] = field(default_factory=list)
    numbers: list[str] = field(default_factory=list)
    kind: str = "statement"

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class EvidenceMap:
    sections: dict[str, str]
    units: list[EvidenceUnit]
    visual_evidence: str = ""

    @property
    def stats(self) -> dict[str, int]:
        return {
            "sections": len(self.sections),
            "units": len(self.units),
            "claim_units": sum(unit.kind == "claim" for unit in self.units),
            "visual_chars": len(self.visual_evidence),
        }

    def to_prompt(self, max_chars: int = 16000, query: str = "") -> str:
        parts = ["## Deterministic Evidence Map"]
        for name, content in self.sections.items():
            preview = re.sub(r"\s+", " ", content).strip()[:500]
            if preview:
                parts.append(f"[{name}] {preview}")

        visual_units = [unit for unit in self.units if unit.kind == "visual"]
        text_units = [unit for unit in self.units if unit.kind != "visual"]
        if visual_units:
            parts.append(
                "Visual evidence page index (all detected pages): "
                + ", ".join(unit.locators[0] for unit in visual_units if unit.locators)
            )

        def tokens(value: str) -> set[str]:
            lower = value.lower()
            output = set(re.findall(r"[a-z0-9]+", lower))
            for chunk in re.findall(r"[\u4e00-\u9fff]{2,}", lower):
                output.update(chunk[index:index + 2] for index in range(len(chunk) - 1))
            return output

        def ranked(units: list[EvidenceUnit], limit: int) -> list[EvidenceUnit]:
            if not query:
                return units[:limit]
            query_tokens = tokens(query)
            return sorted(
                units,
                key=lambda unit: len(tokens(unit.text) & query_tokens) / max(len(query_tokens), 1),
                reverse=True,
            )[:limit]

        chosen_text = ranked(text_units, 48)
        chosen_visual = ranked(visual_units, 48)
        selected: list[EvidenceUnit] = []
        for index in range(max(len(chosen_text), len(chosen_visual))):
            if index < len(chosen_text):
                selected.append(chosen_text[index])
            if index < len(chosen_visual):
                selected.append(chosen_visual[index])

        if selected:
            parts.append("\nEvidence units (balanced text + visual retrieval):")
            for unit in selected:
                locator = ", ".join(unit.locators[:3]) or unit.section
                line = f"- {unit.unit_id} ({unit.kind}; {locator}): {unit.text[:420]}"
                if len("\n".join(parts)) + len(line) + 1 > max_chars:
                    break
                parts.append(line)
        return "\n".join(parts)[:max_chars]

    def search_text(self, max_chars: int = 30000) -> str:
        return " ".join(unit.text for unit in self.units)[:max_chars]


def _sentence_candidates(text: str) -> list[str]:
    lines = [re.sub(r"\s+", " ", line).strip() for line in text.splitlines()]
    result: list[str] = []
    for line in lines:
        if len(line) < 10 or len(line) > 800:
            continue
        for sentence in re.split(r"(?<=[。！？!?;；])\s*", line):
            sentence = sentence.strip()
            if 10 <= len(sentence) <= 500:
                result.append(sentence)
    return result


def build_evidence_map(
    paper_text: str,
    visual_evidence: str = "",
    max_units: int = 240,
) -> EvidenceMap:
    sections = _parse_sections(paper_text)
    units: list[EvidenceUnit] = []
    seen: set[str] = set()

    section_quota = max(12, max_units // max(len(sections), 1))
    for section_name, content in sections.items():
        section_added = 0
        for sentence in _sentence_candidates(content):
            normalized = re.sub(r"\W+", "", sentence.lower())
            if not normalized or normalized in seen:
                continue
            locators = list(dict.fromkeys(_LOCATOR_RE.findall(sentence)))
            numbers = list(dict.fromkeys(match.group(0).strip() for match in _NUMBER_RE.finditer(sentence)))
            is_claim = bool(_CLAIM_RE.search(sentence))
            if not (is_claim or locators or len(numbers) >= 2):
                continue
            seen.add(normalized)
            units.append(EvidenceUnit(
                unit_id=f"E{len(units) + 1:04d}",
                section=section_name,
                text=sentence,
                locators=locators[:6],
                numbers=numbers[:8],
                kind="claim" if is_claim else "evidence",
            ))
            section_added += 1
            if section_added >= section_quota or len(units) >= max_units:
                break
        if len(units) >= max_units:
            break

    # Visual page summaries are first-class evidence units rather than an
    # appendix that section truncation can silently remove.
    for page, content in re.findall(
        r"--- Page (\d+) ---\s*(.*?)(?=--- Page \d+ ---|\Z)",
        visual_evidence,
        re.DOTALL,
    ):
        # Preserve every visual on multi-figure pages as its own retrievable
        # evidence unit instead of truncating the page after the first figure.
        visual_parts = re.split(r"(?=###\s+Visual(?:\s+\d+)?)", content, flags=re.IGNORECASE)
        visual_parts = [part for part in visual_parts if part.strip()]
        for visual_index, part in enumerate(visual_parts, 1):
            compact = re.sub(r"\s+", " ", part).strip()
            if not compact:
                continue
            suffix = f"-{visual_index}" if len(visual_parts) > 1 else ""
            units.append(EvidenceUnit(
                unit_id=f"V{page}{suffix}",
                section="visual",
                text=compact[:1600],
                locators=[f"Page {page}", f"Visual {visual_index}"],
                numbers=list(dict.fromkeys(_NUMBER_RE.findall(compact)))[:8],
                kind="visual",
            ))

    return EvidenceMap(sections=sections, units=units, visual_evidence=visual_evidence)
