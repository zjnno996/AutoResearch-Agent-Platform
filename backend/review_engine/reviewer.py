"""Review orchestration — single-dim, batch, hybrid, multi-model, streaming."""

from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import statistics
import time
import urllib.request
import urllib.error
import uuid
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path
from queue import Queue
from threading import Thread, Lock
from typing import Any

from .dimensions import (
    BATCH_SYSTEM_PROMPT,
    BATCH_SYSTEM_PROMPT_ZH,
    DIMENSION_GROUPS,
    DIMENSION_SECTION_MAP,
    DIMENSION_TO_GROUP,
    FACT_EXTRACTION_PROMPT,
    MAX_FILE_SIZE_BYTES,
    MAX_TEXT_LENGTH,
    OVERALL_SUMMARY_SYSTEM_PROMPT,
    REVIEW_DIMENSIONS,
    REVIEW_SYSTEM_PROMPT,
    REVIEW_SYSTEM_PROMPT_ZH,
    THESIS_DIMENSIONS,
    THESIS_OVERALL_SUMMARY_SYSTEM_PROMPT_ZH,
    VISION_MODELS,
    dim_by_id,
    get_batch_system_prompt,
    get_dim_label,
    get_dim_prompt,
    get_dimensions_for_venue,
    get_overall_summary_prompt,
    get_review_system_prompt,
)
from .consensus import run_consensus_pipeline
from .evidence_map import EvidenceMap, build_evidence_map
from .llm_client import get_client_for_model, get_preferred_review_model_name, get_primary_model_name
from .pdf_utils import (
    _build_dimension_text,
    _build_group_text,
    _check_extraction_quality,
    _estimate_token_usage,
    _get_paper_images,
    _get_paper_text,
    _vision_extract_paper,
)

ROOT_DIR = Path(__file__).resolve().parents[2]
HISTORY_DIR = ROOT_DIR / "backend" / "review_history"
CHECKPOINT_DIR = ROOT_DIR / "backend" / "review_cache" / "stages"
REVIEW_CHECKPOINT_VERSION = "qwen-v8-claim-evidence-r1"


def _review_checkpoint_key(
    raw: bytes,
    file_name: str,
    dimension_ids: list[str],
    model: str,
    venue: str,
    vision_reader: bool,
    enable_debate: bool,
    max_debates: int,
    min_finding_confidence: float,
) -> str:
    payload = {
        "version": REVIEW_CHECKPOINT_VERSION,
        "content_sha256": hashlib.sha256(raw).hexdigest(),
        "file_name": Path(file_name).name,
        "dimensions": sorted(dimension_ids),
        "model": model,
        "venue": venue,
        "vision_reader": vision_reader,
        "enable_debate": enable_debate,
        "max_debates": max_debates,
        "min_finding_confidence": round(float(min_finding_confidence), 4),
        "runtime_flags": {
            name: os.environ.get(name, "0")
            for name in (
                "AUTO_REVIEW_FAST_CONSENSUS",
                "AUTO_REVIEW_SKIP_PATCH",
                "AUTO_REVIEW_SKIP_DEEP_DIVE",
                "AUTO_REVIEW_SKIP_COVERAGE_SWEEP",
            )
        },
    }
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _load_review_checkpoint(key: str) -> dict[str, Any]:
    if os.environ.get("AUTO_REVIEW_DISABLE_CHECKPOINT", "0") == "1":
        return {}
    path = CHECKPOINT_DIR / f"{key}.json"
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if data.get("version") == REVIEW_CHECKPOINT_VERSION:
            return data
    except (OSError, ValueError, TypeError):
        pass
    return {"version": REVIEW_CHECKPOINT_VERSION, "stages": {}}


def _save_review_checkpoint(key: str, data: dict[str, Any]) -> None:
    if os.environ.get("AUTO_REVIEW_DISABLE_CHECKPOINT", "0") == "1":
        return
    try:
        CHECKPOINT_DIR.mkdir(parents=True, exist_ok=True)
        path = CHECKPOINT_DIR / f"{key}.json"
        temporary = CHECKPOINT_DIR / f".{key}.{uuid.uuid4().hex}.tmp"
        temporary.write_text(
            json.dumps(data, ensure_ascii=False, separators=(",", ":")),
            encoding="utf-8",
        )
        os.replace(temporary, path)
    except (OSError, TypeError, ValueError) as exc:
        print(f"[review-checkpoint] save failed: {exc}")


def _checkpoint_stage(
    key: str,
    data: dict[str, Any],
    stage: str,
    value: Any,
) -> None:
    data.setdefault("version", REVIEW_CHECKPOINT_VERSION)
    data.setdefault("stages", {})[stage] = value
    data["updated_at"] = datetime.now(timezone.utc).isoformat()
    _save_review_checkpoint(key, data)

# =============================================================================
# Few-shot / RAG
# =============================================================================

_RAG_ENGINE = None


def _get_rag_engine():
    global _RAG_ENGINE
    if _RAG_ENGINE is None:
        try:
            from review_data.rag_engine import get_rag_engine
            _RAG_ENGINE = get_rag_engine(rebuild=False)
        except Exception:
            _RAG_ENGINE = "error"
    return _RAG_ENGINE if _RAG_ENGINE != "error" else None


def _build_fewshot_context(
    paper_text: str,
    dim_id: str | None = None,
    k: int = 2,
    score_anchoring: bool = False,
    venue: str = "",
) -> str:
    """Build few-shot example string using RAG retrieval.

    When score_anchoring=True, retrieves more examples and selects across
    score bands (high/medium/low) to calibrate the model's scoring.

    Skips retrieval for thesis reviews since few-shot examples are English papers.
    """
    if venue and "thesis" in venue.lower():
        return ""

    engine = _get_rag_engine()
    if not engine:
        return ""

    query_text = paper_text[:3000]

    if score_anchoring:
        # Retrieve enough examples to span score bands
        try:
            examples = engine.retrieve(query_text, dim_id=dim_id, k=6)
        except Exception:
            examples = []
        if not examples:
            return ""

        # Select across bands
        high = [e for e in examples
                if e.overall_score is not None and e.overall_score >= 8]
        mid = [e for e in examples
               if e.overall_score is not None and 4 <= e.overall_score < 8]
        low = [e for e in examples
               if e.overall_score is not None and e.overall_score < 4]

        selected = (high[:2] + mid[:1] + low[:1])
        if not selected:
            selected = examples[:4]

        prefix = (
            "Below are reference review examples at DIFFERENT SCORE LEVELS. "
            "Use them as calibration: notice how high-scoring reviews discuss specific "
            "evidence and nuance, while low-scoring ones highlight critical flaws. "
            "Your scores should reflect a similar standard."
        )
    else:
        try:
            examples = engine.retrieve(query_text, dim_id=dim_id, k=k)
        except Exception:
            return ""
        if not examples:
            return ""
        selected = examples[:k]
        prefix = (
            "Below are reference review examples. "
            "Use them as quality calibration for the level of specificity and "
            "evidence anchoring expected."
        )

    parts = []
    for i, ex in enumerate(selected[:4]):
        score_str = f"SCORE: {ex.overall_score}/10" if ex.overall_score is not None else ""
        block = json.dumps({
            "score": int(ex.overall_score * 10) if ex.overall_score else 70,
            "summary": (ex.comment_to_author or "")[:200],
            "strengths": ex.strengths[:3],
            "weaknesses": ex.weaknesses[:3],
            "suggestions": ex.suggestions[:3],
        }, ensure_ascii=False)
        tag = f"// Reference example {i+1} (dimension: {dim_id}) {score_str}:"
        if score_anchoring:
            band = ""
            if ex.overall_score is not None:
                if ex.overall_score >= 8:
                    band = " [HIGH SCORE ANCHOR]"
                elif ex.overall_score >= 4:
                    band = " [MEDIUM SCORE ANCHOR]"
                else:
                    band = " [LOW SCORE ANCHOR]"
            tag = f"// Reference example {i+1} (dimension: {dim_id}) {score_str}{band}:"
        parts.append(f"{tag}\n{block}")

    if not parts:
        return ""

    return "\n\n".join([prefix] + parts)


VISUAL_EVIDENCE_MARKER = "\n\n# Supplemental Visual Evidence\n"


def _split_visual_evidence(
    paper_text: str,
    max_chars: int = 40000,
) -> tuple[str, str]:
    """Keep visual evidence outside section routing so every reviewer sees it."""
    if VISUAL_EVIDENCE_MARKER not in paper_text:
        return paper_text, ""
    paper_body, visual = paper_text.split(VISUAL_EVIDENCE_MARKER, 1)
    pages = re.findall(
        r"--- Page (\d+) ---\s*(.*?)(?=--- Page \d+ ---|\Z)",
        visual,
        re.DOTALL,
    )
    if pages:
        # Preserve evidence from every visual page instead of truncating away
        # later chapters. Each page receives an equal compact budget.
        per_page = max(80, (max_chars - len(pages) * 32) // len(pages))
        compact_visual = "\n\n".join(
            f"--- Page {page} ---\n{content.strip()[:per_page]}"
            for page, content in pages
        )[:max_chars]
    else:
        compact_visual = visual[:max_chars]
    visual_section = (
        "\n\n## Visual Evidence from All Detected Figure/Table Pages\n"
        + compact_visual
    )
    return paper_body.rstrip(), visual_section


def _build_issue_pattern_context(
    paper_text: str,
    dimension_ids: list[str],
    facts: dict[str, Any] | None = None,
) -> str:
    """Retrieve leak-safe diagnostic patterns from other expert-reviewed theses."""
    try:
        from review_data.issue_patterns import get_issue_pattern_index
        index = get_issue_pattern_index()
    except Exception:
        return ""
    evidence_query = str((facts or {}).get("evidence_map", ""))
    query = (evidence_query + "\n" + paper_text[:16000])[:30000]
    blocks: list[str] = []
    seen: set[str] = set()
    for dimension_id in dimension_ids:
        block = index.prompt_context(
            query=query,
            dimension=dimension_id,
            target_paper_text=paper_text,
            k=3,
        )
        if block and block not in seen:
            seen.add(block)
            blocks.append(block)
    return "\n\n".join(blocks)[:10000]


# =============================================================================
# Citation-aware context
# =============================================================================

ARXIV_PATTERN = re.compile(r"arxiv\.org/(?:abs|pdf)/(\d{4}\.\d+(?:v\d+)?)", re.IGNORECASE)
DOI_PATTERN = re.compile(r"(?:doi\.org/|doi:\s*)(10\.\d{4,}/[^\s,;)]+)", re.IGNORECASE)


def _extract_references(paper_text: str) -> list[dict[str, str]]:
    """Extract arXiv IDs and DOIs from paper text."""
    refs: list[dict[str, str]] = []
    seen: set[str] = set()
    for match in ARXIV_PATTERN.finditer(paper_text):
        arxiv_id = match.group(1)
        if arxiv_id not in seen:
            seen.add(arxiv_id)
            refs.append({"type": "arxiv", "id": arxiv_id})
    for match in DOI_PATTERN.finditer(paper_text):
        doi = match.group(1).rstrip(".,)")
        if doi not in seen:
            seen.add(doi)
            refs.append({"type": "doi", "id": doi})
    return refs[:10]


def _fetch_reference_context(paper_text: str) -> str:
    """Fetch abstracts for cited references."""
    refs = _extract_references(paper_text)
    if not refs:
        return ""

    contexts: list[str] = []
    for ref in refs:
        try:
            if ref["type"] == "arxiv":
                url = f"https://export.arxiv.org/api/query?id_list={ref['id']}"
                req = urllib.request.Request(url, headers={"User-Agent": "ClawAI/1.0"})
                with urllib.request.urlopen(req, timeout=5) as resp:
                    xml_data = resp.read().decode("utf-8")
                    root = ET.fromstring(xml_data)
                    ns = {"atom": "http://www.w3.org/2005/Atom",
                          "arxiv": "http://arxiv.org/schemas/atom"}
                    title_el = root.find(".//atom:title", ns)
                    abstract_el = root.find(".//atom:summary", ns)
                    title = title_el.text.strip() if title_el is not None and title_el.text else ""
                    abstract = abstract_el.text.strip() if abstract_el is not None and abstract_el.text else ""
                    if title and abstract:
                        abstract_clean = re.sub(r"\s+", " ", abstract)[:500]
                        contexts.append(f"[{ref['id']}] {title}: {abstract_clean}")
            elif ref["type"] == "doi":
                url = f"https://api.crossref.org/works/{ref['id']}"
                req = urllib.request.Request(url, headers={"User-Agent": "ClawAI/1.0"})
                with urllib.request.urlopen(req, timeout=5) as resp:
                    data = json.loads(resp.read().decode("utf-8"))
                    msg = data.get("message", {})
                    title = (msg.get("title") or [""])[0]
                    abstract = (msg.get("abstract") or "")[:500]
                    if title:
                        abstract_clean = re.sub(r"\s+", " ", abstract) if abstract else "(no abstract)"
                        contexts.append(f"[DOI:{ref['id']}] {title}: {abstract_clean}")
        except Exception:
            continue

    if not contexts:
        return ""
    return "Referenced works (for verifying related work claims):\n" + "\n".join(contexts)


# =============================================================================
# Dimension review with retry
# =============================================================================

def _run_review_dimension(
    client: Any, paper_text: str, dimension: dict[str, str], model: str | None,
    page_images: list[str] | None = None,
    facts: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Run a single dimension review against the LLM with retry."""
    paper_body, visual_section = _split_visual_evidence(paper_text)
    issue_pattern_section = _build_issue_pattern_context(
        paper_body, [dimension["id"]], facts
    )
    if issue_pattern_section:
        issue_pattern_section = "\n\n" + issue_pattern_section
    has_images = page_images and any(page_images)
    role_instruction = dimension.get("role", "")
    system_msg = REVIEW_SYSTEM_PROMPT
    if role_instruction:
        system_msg = role_instruction + "\n\n" + REVIEW_SYSTEM_PROMPT

    try:
        fewshot_ctx = _build_fewshot_context(paper_text, dimension["id"], score_anchoring=True)
        if fewshot_ctx:
            system_msg += "\n\n" + fewshot_ctx
    except Exception:
        pass

    # Build facts section if available
    facts_section = ""
    if facts:
        parts = []
        if facts.get("research_question"):
            parts.append(f"Research Question: {facts['research_question']}")
        if facts.get("claim"):
            parts.append(f"Main Claim: {facts['claim']}")
        if facts.get("method_summary"):
            parts.append(f"Method: {facts['method_summary']}")
        if facts.get("datasets"):
            parts.append(f"Datasets: {'; '.join(facts['datasets'])}")
        if facts.get("baselines"):
            parts.append(f"Baselines: {'; '.join(facts['baselines'])}")
        if facts.get("key_results"):
            kr_lines = []
            for kr in facts["key_results"]:
                kr_lines.append(f"  - {kr.get('claim','')} ({kr.get('section','')}): {kr.get('evidence','')}")
            parts.append(f"Key Results:\n" + "\n".join(kr_lines))
        if facts.get("evidence_map"):
            parts.append(str(facts["evidence_map"])[:16000])
        if parts:
            facts_section = (
                "\n\n## Extracted Facts (ground truth — verify every claim against these)\n"
                + "\n".join(parts)
            )

    if has_images:
        img_prompt = (
            f"## Paper Content\n\n{_build_dimension_text(paper_body, dimension['id'], max_chars=100000)}"
            f"{visual_section}{issue_pattern_section}{facts_section}\n\n"
            f"## Review Task: {dimension['label']}\n\n{dimension['prompt']}\n\n"
            "The paper pages are attached as images. Use both the extracted text "
            "and the page images (especially figures, tables, charts) for your evaluation. "
            "Respond with valid JSON following the schema provided in the system prompt."
        )
        content_parts: list[dict[str, Any]] = [
            {"type": "text", "text": img_prompt}
        ]
        for img_b64 in page_images:
            content_parts.append({
                "type": "image_url",
                "image_url": {"url": f"data:image/png;base64,{img_b64}"}
            })
        user_content = content_parts
    else:
        dim_text = _build_dimension_text(paper_body, dimension["id"])
        user_content = (
            f"## Paper Content\n\n{dim_text}"
            f"{visual_section}{issue_pattern_section}{facts_section}\n\n"
            f"## Review Task: {dimension['label']}\n\n{dimension['prompt']}\n\n"
            "Respond with valid JSON following the schema provided in the system prompt."
        )

    last_error = ""
    token_usage: dict[str, int] = {"prompt": 0, "completion": 0, "total": 0}
    max_attempts = 2
    for attempt in range(max_attempts):
        try:
            resp = client.chat(
                messages=[{"role": "user", "content": user_content}],
                system=system_msg,
                model=model if model else None,
                max_tokens=4096,
                temperature=0.3,
                json_mode=True,
            )
            token_usage = {
                "prompt": getattr(resp, "prompt_tokens", 0),
                "completion": getattr(resp, "completion_tokens", 0),
                "total": getattr(resp, "total_tokens", 0),
            }
            result = json.loads(resp.content)
            return {
                "dimensionId": dimension["id"],
                "score": max(0, min(100, int(result.get("score", 70)))),
                "summary": str(result.get("summary", ""))[:500],
                "strengths": [str(s)[:200] for s in (result.get("strengths") or [])][:2],
                "weaknesses": [str(w)[:200] for w in (result.get("weaknesses") or [])][:5],
                "suggestions": [str(s)[:200] for s in (result.get("suggestions") or [])][:5],
                "analysis": str(result.get("analysis", ""))[:1000],
                "self_critique": str(result.get("self_critique", ""))[:500],
                "_token_usage": token_usage,
            }
        except Exception as exc:
            last_error = str(exc)
            if attempt < max_attempts - 1:
                time.sleep(1.5 * (attempt + 1))

    return {
        "dimensionId": dimension["id"],
        "score": 50,
        "summary": f"Review failed after {max_attempts} attempts: {last_error}",
        "strengths": ["Unable to complete review"],
        "weaknesses": ["Review process encountered an error"],
        "suggestions": ["Try again with a different model or paper format"],
        "analysis": "",
        "self_critique": "",
        "_token_usage": token_usage,
    }


# =============================================================================
# Batch single-call review
# =============================================================================

def _run_review_batch(
    client: Any,
    paper_text: str,
    dimensions: list[dict[str, str]],
    model: str | None = None,
    reference_context: str = "",
    facts: dict[str, Any] | None = None,
    skeptic_questions: list[str] | None = None,
    venue: str = "",
    page_images: list[str] | None = None,
) -> list[dict[str, Any]]:
    """Run ALL dimension reviews in a single LLM call.

    Args:
        client: LLM client
        paper_text: Extracted paper text
        dimensions: Dimensions to evaluate
        model: Model name override
        reference_context: Context from referenced works
        facts: Extracted paper facts for evidence grounding
        skeptic_questions: Probing questions about assumptions/gaps
        venue: Conference venue (e.g. "ICLR", "NeurIPS") for standards calibration
        page_images: Base64-encoded PNG images of key PDF pages for multi-modal input
    """
    paper_body, visual_section = _split_visual_evidence(paper_text)
    issue_pattern_section = _build_issue_pattern_context(
        paper_body, [str(d["id"]) for d in dimensions], facts
    )
    if issue_pattern_section:
        issue_pattern_section = "\n\n" + issue_pattern_section
    dim_sections = "\n\n".join(
        f"### Dimension: {get_dim_label(d, venue)} (id: {d['id']})\n{d.get('role', '')}\n{get_dim_prompt(d, venue)}"
        for d in dimensions
    )

    ref_section = ""
    if reference_context:
        ref_section = f"\n\n## Reference Context\n\n{reference_context[:5000]}"

    facts_section = ""
    if facts:
        parts = []
        if facts.get("research_question"):
            parts.append(f"Research Question: {facts['research_question']}")
        if facts.get("claim"):
            parts.append(f"Main Claim: {facts['claim']}")
        if facts.get("method_summary"):
            parts.append(f"Method: {facts['method_summary']}")
        if facts.get("datasets"):
            parts.append(f"Datasets: {'; '.join(facts['datasets'])}")
        if facts.get("baselines"):
            parts.append(f"Baselines: {'; '.join(facts['baselines'])}")
        if facts.get("key_results"):
            kr_lines = []
            for kr in facts["key_results"]:
                kr_lines.append(f"  - {kr.get('claim','')} ({kr.get('section','')}): {kr.get('evidence','')}")
            parts.append(f"Key Results:\n" + "\n".join(kr_lines))
        if facts.get("evidence_map"):
            parts.append(str(facts["evidence_map"])[:16000])
        if parts:
            facts_section = (
                "\n\n## Extracted Facts (ground truth — verify every claim against these)\n"
                + "\n".join(parts)
            )

    # Add skeptic questions section
    skeptic_section = ""
    if skeptic_questions:
        q_lines = "\n".join(f"  Q{j+1}: {q}" for j, q in enumerate(skeptic_questions))
        skeptic_section = (
            "\n\n## Critical Questions to Address\n"
            "A pre-review analysis identified these potential concerns. "
            "Your review MUST address each of these questions explicitly "
            "(say whether each concern is valid or explain why it does not apply):\n"
            f"{q_lines}"
        )

    # Add venue standards
    venue_section = ""
    if venue:
        venue_lower = venue.lower()
        if "iclr" in venue_lower:
            venue_section = (
                "\n\n## Venue Standards (ICLR)\n"
                "ICLR is a top-tier conference that emphasizes:\n"
                "- **Novelty**: Contributions must be more than incremental. "
                "Strong theoretical insights or surprisingly effective empirical results are expected.\n"
                "- **Completeness**: Papers are expected to be self-contained with thorough experiments.\n"
                "- **Reproducibility**: Code release is expected where feasible.\n"
                "Score calibration: 8+ = strong ICLR paper, 6-7 = decent but below ICLR bar, <6 = reject."
            )
        elif "neurips" in venue_lower:
            venue_section = (
                "\n\n## Venue Standards (NeurIPS)\n"
                "NeurIPS emphasizes:\n"
                "- **Technical rigor**: Sound methodology, statistical significance.\n"
                "- **Scope**: Broad interest to the ML community.\n"
                "- **Reproducibility**: Strong expectations for experimental detail.\n"
                "Score calibration: 8+ = strong NeurIPS paper, 6-7 = acceptable, <6 = reject."
            )
        elif "thesis" in venue_lower:
            venue_section = (
                "\n\n## 评审标准（中国学位论文）\n"
                "本系统评审的是中国博士/硕士学位论文，评审标准如下：\n"
                "- **格式规范**：参考文献格式、英文摘要质量、图表规范、术语统一\n"
                "- **结构逻辑**：章节安排的合理性、逻辑递进关系\n"
                "- **理论深度**：算法/模型的收敛性分析、计算复杂度、数学证明\n"
                "- **创新性**：与已有工作的实质性差异\n"
                "- **实验充分性**：数据集、对比方法、消融实验\n"
                "分数校准：90+ = 优秀论文, 80-89 = 良好, 70-79 = 合格, 60-69 = 需修改, <60 = 不通过\n"
            )

    group_text = _build_group_text(paper_body, [d["id"] for d in dimensions])
    text_content = (
        f"## Paper Content\n\n{group_text}{visual_section}{issue_pattern_section}{ref_section}{facts_section}{skeptic_section}{venue_section}\n\n"
        f"## Evaluation Dimensions\n\n{len(dimensions)} dimensions to evaluate:\n\n"
        f"{dim_sections}\n\n"
        "Output valid JSON with one key per dimension id. "
        "Each value must have: score (0-100), summary (string), "
        "strengths (array of 3), weaknesses (array of 3), suggestions (array of 3).\n"
        "Be critical and specific, referencing content from the paper."
    )

    # Multi-modal: attach key PDF pages as images alongside the text
    has_images = page_images and any(page_images)
    if has_images:
        image_note = (
            "\n\nThe paper pages are attached as images. Use both the extracted text "
            "and the page images (especially figures, tables, charts, experimental results) "
            "for your evaluation. When referencing experimental data, cite the figure/table "
            "from the page images.\n"
            "Respond with valid JSON following the schema provided in the system prompt."
        )
        content_parts: list[dict[str, Any]] = [
            {"type": "text", "text": text_content + image_note}
        ]
        for img_b64 in page_images:
            content_parts.append({
                "type": "image_url",
                "image_url": {"url": f"data:image/png;base64,{img_b64}"}
            })
        user_content = content_parts
    else:
        user_content = text_content

    roles = [d.get("role", "") for d in dimensions if d.get("role")]
    batch_system = get_batch_system_prompt(venue)
    if roles:
        role_header = "You are serving as multiple expert reviewers:\n" + "\n".join(f"- {r}" for r in roles)
        batch_system = role_header + "\n\n" + batch_system

    try:
        dim_fewshots = []
        for d in dimensions:
            ctx = _build_fewshot_context(paper_text, d["id"], score_anchoring=True, venue=venue)
            if ctx:
                dim_fewshots.append(f"[{get_dim_label(d, venue)}]\n{ctx}")
        if dim_fewshots:
            batch_system += "\n\n" + "=" * 40 + "\nReference Review Examples (Score-Anchored)\n" + "=" * 40 + "\n"
            batch_system += "\n\n".join(dim_fewshots)
    except Exception:
        pass

    token_usage: dict[str, int] = {"prompt": 0, "completion": 0, "total": 0}
    max_attempts = 2
    for attempt in range(max_attempts):
        try:
            resp = client.chat(
                messages=[{"role": "user", "content": user_content}],
                system=batch_system,
                model=model if model else None,
                max_tokens=16384,
                temperature=0.3,
                json_mode=True,
            )

            token_usage = {
                "prompt": getattr(resp, "prompt_tokens", 0),
                "completion": getattr(resp, "completion_tokens", 0),
                "total": getattr(resp, "total_tokens", 0),
            }

            raw = resp.content.strip()
            if raw.startswith("```"):
                raw = raw.split("\n", 1)[-1]
                raw = raw.rsplit("```", 1)[0]
            data = json.loads(raw)

            results: list[dict[str, Any]] = []
            for dim in dimensions:
                dim_id = dim["id"]
                entry = data.get(dim_id, {})
                results.append({
                    "dimensionId": dim_id,
                    "score": max(0, min(100, int(entry.get("score", 70)))),
                    "summary": str(entry.get("summary", ""))[:500],
                    "strengths": [str(s)[:200] for s in (entry.get("strengths") or [])][:2],
                    "weaknesses": [str(w)[:200] for w in (entry.get("weaknesses") or [])][:5],
                    "suggestions": [str(s)[:200] for s in (entry.get("suggestions") or [])][:5],
                    "analysis": str(entry.get("analysis", ""))[:1000],
                    "self_critique": str(entry.get("self_critique", ""))[:500],
                    "_token_usage": token_usage,
                })
            return results
        except Exception as exc:
            if attempt < max_attempts - 1:
                time.sleep(2.0)
                continue
            return [
                {
                    "dimensionId": dim["id"],
                    "score": 50,
                    "summary": f"Batch review failed: {exc}",
                    "strengths": ["Unable to complete review"],
                    "weaknesses": ["Review process encountered an error"],
                    "suggestions": ["Try again with a different model or paper format"],
                    "analysis": "",
                    "self_critique": "",
                    "_token_usage": token_usage,
                }
                for dim in dimensions
            ]


# =============================================================================
# Hybrid group scheduling: 3+2+2
# =============================================================================

def _run_review_hybrid(
    client: Any,
    paper_text: str,
    dimensions: list[dict[str, str]],
    model: str | None = None,
    reference_context: str = "",
    facts: dict[str, Any] | None = None,
    skeptic_questions: list[str] | None = None,
    venue: str = "",
    page_images: list[str] | None = None,
) -> list[dict[str, Any]]:
    """Run dimension reviews in parallel groups."""
    groups: dict[int, list[dict[str, str]]] = {}
    for dim in dimensions:
        gidx = DIMENSION_TO_GROUP.get(dim["id"], 0)
        groups.setdefault(gidx, []).append(dim)

    results_by_dim: dict[str, dict[str, Any]] = {}
    lock = Lock()

    def run_group(group_dims: list[dict[str, str]]) -> None:
        if not group_dims:
            return
        g_results = _run_review_batch(
            client, paper_text, group_dims, model, reference_context,
            facts=facts, skeptic_questions=skeptic_questions, venue=venue,
            page_images=page_images,
        )
        with lock:
            for r in g_results:
                results_by_dim[r["dimensionId"]] = r

    group_list = list(groups.values())
    if len(group_list) <= 1:
        for g in group_list:
            run_group(g)
    else:
        with ThreadPoolExecutor(max_workers=len(group_list)) as pool:
            pool.map(run_group, group_list)

    return [results_by_dim[dim["id"]] for dim in dimensions if dim["id"] in results_by_dim]


# =============================================================================
# Multi-model voting
# =============================================================================

def _run_review_multi_model(
    paper_text: str,
    dimensions: list[dict[str, str]],
    models: list[str],
    reference_context: str = "",
    facts: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    """Run review with multiple models and average the results."""
    all_results: dict[str, list[dict[str, Any]]] = {}

    for model_name in models:
        try:
            client = get_client_for_model(model_name)
            model_results = _run_review_batch(client, paper_text, dimensions, model_name, reference_context, facts=facts)
            for r in model_results:
                all_results.setdefault(r["dimensionId"], []).append(r)
        except Exception:
            continue

    if not all_results:
        return [
            {
                "dimensionId": dim["id"],
                "score": 50,
                "summary": "All models failed",
                "strengths": [],
                "weaknesses": ["No models could complete the review"],
                "suggestions": ["Check model availability"],
            }
            for dim in dimensions
        ]

    merged: list[dict[str, Any]] = []
    for dim in dimensions:
        dim_id = dim["id"]
        entries = all_results.get(dim_id, [])
        if not entries:
            merged.append({
                "dimensionId": dim_id,
                "score": 50,
                "summary": "No results from any model",
                "strengths": [],
                "weaknesses": ["Models failed for this dimension"],
                "suggestions": ["Retry with different models"],
            })
            continue

        scores = [e["score"] for e in entries]
        avg_score = round(statistics.mean(scores))
        stddev = round(statistics.stdev(scores), 1) if len(scores) > 1 else 0

        sorted_by_score = sorted(entries, key=lambda e: abs(e["score"] - avg_score))
        median_entry = sorted_by_score[0]

        merged.append({
            "dimensionId": dim_id,
            "score": max(0, min(100, avg_score)),
            "summary": median_entry["summary"],
            "strengths": median_entry["strengths"],
            "weaknesses": median_entry["weaknesses"],
            "suggestions": median_entry["suggestions"],
            "confidence": {
                "stddev": stddev,
                "disagreement": stddev > 10,
                "model_scores": {
                    models[i] if i < len(models) else f"model_{i}": scores[i]
                    for i in range(len(scores))
                },
                "num_models": len(scores),
            },
        })

    return merged


# =============================================================================
# Per-dimension parallel review
# =============================================================================

class ReviewProgress:
    """Tracks and emits per-dimension progress for streaming."""

    def __init__(self, dims_to_run: list[dict[str, str]]):
        self.dims_to_run = dims_to_run
        self.total = len(dims_to_run)
        self.completed: list[dict[str, Any]] = []
        self._lock = Lock()

    def add_result(self, result: dict[str, Any]) -> dict[str, Any]:
        with self._lock:
            self.completed.append(result)
        return {
            "type": "progress",
            "dimensionId": result["dimensionId"],
            "result": result,
            "completed": len(self.completed),
            "total": self.total,
        }

    def get_complete_event(self) -> dict[str, Any]:
        results = self.completed
        overall = sum(r["score"] for r in results) // len(results) if results else 0
        return {
            "type": "complete",
            "overallScore": overall,
            "dimensionCount": len(results),
            "results": results,
        }


def _run_review_parallel(
    client: Any,
    review_text: str,
    dims_to_run: list[dict[str, str]],
    model: str | None = None,
    page_images: list[str] | None = None,
    progress_queue: Queue | None = None,
    facts: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    """Run per-dimension parallel reviews."""
    results: list[dict[str, Any]] = []
    progress = ReviewProgress(dims_to_run)

    with ThreadPoolExecutor(max_workers=len(dims_to_run)) as pool:
        futures = {
            pool.submit(_run_review_dimension, client, review_text, dim, model, page_images, facts=facts): dim
            for dim in dims_to_run
        }
        for future in as_completed(futures):
            result = future.result()
            results.append(result)
            if progress_queue:
                event = progress.add_result(result)
                progress_queue.put(event)

    return results


# =============================================================================
# Review quality check
# =============================================================================

def _check_review_consistency(result: dict[str, Any]) -> dict[str, Any]:
    """Verify review internal consistency: score vs text coherence.
    Returns the review result with added quality flags."""
    score = result.get("score", 50)
    strengths = result.get("strengths", [])
    weaknesses = result.get("weaknesses", [])

    # Check if score is high but weaknesses are overly harsh > strengths
    # or score is low but all strengths > weaknesses
    flags = []

    if score >= 80 and len(weaknesses) >= 3:
        avg_w_len = sum(len(w) for w in weaknesses) / len(weaknesses)
        avg_s_len = sum(len(s) for s in strengths) / max(len(strengths), 1)
        if avg_w_len > avg_s_len * 2:
            flags.append("score-text-mismatch: high score but verbose weaknesses")

    if score <= 40 and len(strengths) >= 3:
        avg_s_len = sum(len(s) for s in strengths) / len(strengths)
        avg_w_len = sum(len(w) for w in weaknesses) / max(len(weaknesses), 1)
        if avg_s_len > avg_w_len * 2:
            flags.append("score-text-mismatch: low score but verbose strengths")

    # Check for placeholder text
    for item in strengths + weaknesses:
        if "unable to" in item.lower() or "review process" in item.lower():
            flags.append("review-failed-placeholder")
            break

    result["_quality_flags"] = flags
    return result


# =============================================================================
# Overall executive summary
# =============================================================================


def _generate_overall_summary(
    results: list[dict[str, Any]],
    model: str | None = None,
    venue: str = "",
) -> dict[str, Any] | None:
    """Generate an overall executive summary from all dimension reviews.
    Returns None if generation fails (non-blocking).
    """
    if not results:
        return None

    try:
        client = get_client_for_model(model or get_primary_model_name())
    except Exception:
        return None

    scores = [r["score"] for r in results if "score" in r]
    avg = sum(scores) // len(scores) if scores else 0
    max_d = max(scores) if scores else 0
    min_d = min(scores) if scores else 0

    dim_summaries = []
    for r in results:
        dim_summaries.append(
            f"=== {r.get('dimensionId', '?')} ===\n"
            f"Score: {r.get('score', '?')}/100\n"
            f"Summary: {r.get('summary', '')[:300]}\n"
            f"Strengths: {'; '.join(r.get('strengths', []) or [])}\n"
            f"Weaknesses: {'; '.join(r.get('weaknesses', []) or [])}\n"
            f"Suggestions: {'; '.join(r.get('suggestions', []) or [])}\n"
        )

    context = (
        f"Review scores across {len(results)} dimensions.\n"
        f"Average: {avg}/100 | Range: {min_d}–{max_d}\n\n"
        + "\n".join(dim_summaries)
    )

    try:
        summary_system_prompt = get_overall_summary_prompt(venue)
        is_thesis = venue and "thesis" in venue.lower()
        resp = client.chat(
            messages=[{"role": "user", "content": context}],
            system=summary_system_prompt,
            model=model if model else None,
            max_tokens=4096 if is_thesis else 2048,
            temperature=0.3 if is_thesis else 0.2,
            json_mode=True,
        )
        summary_tokens = {
            "prompt": getattr(resp, "prompt_tokens", 0),
            "completion": getattr(resp, "completion_tokens", 0),
        }
        summary = json.loads(resp.content)

        if is_thesis and "reviewers" in summary:
            return {
                "reviewers": summary["reviewers"],
                "comparativeAnalysis": summary.get("comparativeAnalysis", {}),
                "finalRecommendation": summary.get("finalRecommendation", {}),
                "averageScore": avg,
                "scoreRange": f"{min_d}–{max_d}",
                "_token_usage": summary_tokens,
            }
        else:
            return {
                "overallAssessment": str(summary.get("overallAssessment", ""))[:600],
                "recommendation": str(summary.get("recommendation", "borderline")),
                "detailedStrengths": [str(s)[:300] for s in (summary.get("detailedStrengths") or summary.get("topStrengths") or [])][:5],
                "detailedWeaknesses": [str(w)[:300] for w in (summary.get("detailedWeaknesses") or summary.get("topWeaknesses") or [])][:5],
                "detailedSuggestions": [str(s)[:300] for s in (summary.get("detailedSuggestions") or summary.get("keySuggestions") or [])][:5],
                "comparativeAnalysis": summary.get("comparativeAnalysis", {}),
                "executiveSummary": str(summary.get("executiveSummary", ""))[:300],
                "confidence": str(summary.get("confidence", "medium")),
                "averageScore": avg,
                "scoreRange": f"{min_d}–{max_d}",
                "_token_usage": summary_tokens,
            }
    except Exception:
        return None


# =============================================================================
# Stage 1: Structured fact extraction
# =============================================================================


def _extract_paper_facts(
    paper_text: str,
    model: str | None = None,
) -> dict[str, Any] | None:
    """Extract structured facts from paper text.

    Returns a dict with research_question, claim, method_summary, datasets,
    baselines, key_results, limitations — or None on failure.
    """
    if not paper_text or len(paper_text.strip()) < 200:
        return None

    try:
        client = get_client_for_model(model or get_primary_model_name())
    except Exception:
        return None

    try:
        resp = client.chat(
            messages=[{"role": "user", "content": paper_text[:25000]}],
            system=FACT_EXTRACTION_PROMPT,
            model=model if model else None,
            max_tokens=4096,
            temperature=0.1,
            json_mode=True,
        )
        raw = json.loads(resp.content)
        facts = {
            "research_question": str(raw.get("research_question", ""))[:500],
            "claim": str(raw.get("claim", ""))[:500],
            "method_summary": str(raw.get("method_summary", ""))[:1000],
            "datasets": [str(d)[:200] for d in (raw.get("datasets") or [])][:5],
            "baselines": [str(b)[:200] for b in (raw.get("baselines") or [])][:5],
            "key_results": [
                {
                    "claim": str(kr.get("claim", ""))[:300],
                    "evidence": str(kr.get("evidence", ""))[:300],
                    "section": str(kr.get("section", ""))[:100],
                }
                for kr in (raw.get("key_results") or [])
            ][:5],
            "limitations": [str(l)[:300] for l in (raw.get("limitations") or [])][:3],
        }
        return facts
    except Exception:
        return None


# =============================================================================
# Stage 2: Skeptic question generation (assumption probing)
# =============================================================================

SKEPTIC_PROMPT = (
    "You are a devil's advocate reviewer analyzing a research paper. "
    "Your job is to generate probing questions that identify hidden assumptions, "
    "unstated premises, alternative explanations, and methodological gaps.\n\n"
    "Based on the paper facts and text below, generate 5-8 specific questions "
    "a critical reviewer would ask about THIS specific paper — not generic questions.\n\n"
    "For each question, cite the specific section or claim it targets.\n\n"
    "Output a JSON array of strings only.\n"
    'Example: ["In §3.2, the paper assumes X without justification — what if X does not hold in practice?", '
    '"The claim that Y outperforms Z (Table 2) uses default Z hyperparameters — would proper tuning change the ranking?"]\n\n'
    "Paper facts:\n{facts_str}\n\nPaper excerpt:\n{paper_excerpt}"
)


def _generate_skeptic_questions(
    paper_text: str,
    facts: dict[str, Any] | None = None,
    model: str | None = None,
) -> list[str]:
    """Generate probing questions about the paper's assumptions and gaps."""
    if not paper_text or len(paper_text.strip()) < 200:
        return []

    facts_str = ""
    if facts:
        parts = []
        if facts.get("claim"):
            parts.append(f"Main claim: {facts['claim']}")
        if facts.get("method_summary"):
            parts.append(f"Method: {facts['method_summary']}")
        if facts.get("key_results"):
            for kr in facts["key_results"]:
                parts.append(f"Result: {kr.get('claim','')} ({kr.get('section','')}) → {kr.get('evidence','')}")
        if facts.get("limitations"):
            parts.append(f"Stated limitations: {'; '.join(facts['limitations'])}")
        if facts.get("baselines"):
            parts.append(f"Baselines: {'; '.join(facts['baselines'])}")
        facts_str = "\n".join(parts)
    else:
        facts_str = "(no facts extracted)"

    paper_excerpt = paper_text[:4000]

    prompt = SKEPTIC_PROMPT.format(facts_str=facts_str, paper_excerpt=paper_excerpt)

    try:
        client = get_client_for_model(model or get_primary_model_name())
        resp = client.chat(
            messages=[{"role": "user", "content": prompt}],
            max_tokens=2048,
            temperature=0.3,
            json_mode=True,
        )
        raw = resp.content.strip()
        if raw.startswith("```"):
            raw = raw.split("\n", 1)[-1]
            raw = raw.rsplit("```", 1)[0]
        questions = json.loads(raw)
        if isinstance(questions, dict):
            for v in questions.values():
                if isinstance(v, list):
                    questions = v
                    break
        if not isinstance(questions, list):
            return []
        return [str(q)[:300] for q in questions if isinstance(q, str)][:8]
    except Exception:
        return []


# =============================================================================
# Stage 3: Fact grounding verification
# =============================================================================


def _verify_fact_grounding(
    result: dict[str, Any],
    facts: dict[str, Any] | None,
) -> dict[str, Any]:
    """Lightweight check: flag strengths/weaknesses that may be ungrounded in facts.
    Compares key terms in review items against terms from extracted facts.
    Non-blocking — only appends to _grounding_issues.
    """
    if not facts:
        return result

    # Build a set of known significant terms from the facts
    known_terms: set[str] = set()
    for val in facts.values():
        if isinstance(val, str):
            known_terms.update(
                w.lower() for w in val.split() if len(w) > 5
            )
        elif isinstance(val, list):
            for item in val:
                if isinstance(item, str):
                    known_terms.update(
                        w.lower() for w in item.split() if len(w) > 5
                    )
                elif isinstance(item, dict):
                    for sv in item.values():
                        if isinstance(sv, str):
                            known_terms.update(
                                w.lower() for w in sv.split() if len(w) > 5
                            )

    if not known_terms:
        return result

    issues: list[str] = []
    for key in ("strengths", "weaknesses"):
        for item in (result.get(key) or []):
            item_words = set(
                w.lower() for w in item.split() if len(w) > 5
            )
            overlap = item_words & known_terms
            if len(item_words) >= 3 and len(overlap) <= 1:
                issues.append(
                    f"possibly ungrounded: \"{item[:120]}\" — "
                    f"only {len(overlap)}/{len(item_words)} key terms match extracted facts"
                )

    if issues:
        result["_grounding_issues"] = result.get("_grounding_issues", []) + issues

    return result


# =============================================================================
# Stage 4: Separate skeptic review (multi-step — runs after 7 dimensions)
# =============================================================================

SKEPTIC_REVIEW_PROMPT = (
    "You are a devil's advocate reviewer. Your job is to identify what the STANDARD review dimensions "
    "may have missed. Read the paper text below, then review it through a CRITICAL QUESTIONING lens.\n\n"
    "Focus on:\n"
    "[  ] Hidden assumptions — what does the paper take for granted? (0-10)\n"
    "[  ] Alternative explanations — are they ruled out? (0-15)\n"
    "[  ] Missing controls or edge cases in evaluation? (0-15)\n"
    "[  ] Do conclusions fully follow from evidence? (0-10)\n"
    "[  ] Overlooked baselines or related approaches? (0-10)\n"
    "Total: __/60 → 0-100 final score.\n\n"
    "IMPORTANT: Prioritize points NOT already covered by the standard dimensions listed below. "
    "If most of your concerns are already raised by the standard review, note that and score accordingly. "
    "You add the most value by identifying what everyone else missed."
)


def _run_review_skeptic(
    paper_text: str,
    skeptic_dim: dict[str, str | bool],
    main_results: list[dict[str, Any]],
    skeptic_questions: list[str] | None = None,
    facts: dict[str, Any] | None = None,
    venue: str = "",
    model: str | None = None,
) -> dict[str, Any]:
    """Run the skeptic dimension as a separate, context-aware review stage.

    Takes the results from the 7 standard dimensions and runs the skeptic
    dimension with full context (skeptic questions + other dimension findings).
    """
    try:
        client = get_client_for_model(model or get_primary_model_name())
    except Exception as e:
        return {
            "dimensionId": skeptic_dim["id"],
            "score": 50,
            "summary": f"Skeptic review failed: {e}",
            "strengths": ["Unable to complete skeptic review"],
            "weaknesses": [],
            "suggestions": [],
            "analysis": "",
            "self_critique": "",
        }

    # Build context from other dimensions' results
    other_context = ""
    if main_results:
        parts = []
        for r in main_results:
            dim_id = r.get("dimensionId", "?")
            score = r.get("score", "?")
            strengths = "; ".join(r.get("strengths", []) or [])
            weaknesses = "; ".join(r.get("weaknesses", []) or [])
            parts.append(
                f"  [{dim_id}] Score={score}/100\n"
                f"    Strengths: {strengths}\n"
                f"    Weaknesses: {weaknesses}"
            )
        other_context = (
            "\n\n## Standard Review Findings\n"
            "These points are ALREADY raised by the standard review dimensions. "
            "Do NOT repeat them. Focus on what is MISSING:\n" +
            "\n".join(parts)
        )

    # Venue section
    venue_section = ""
    if venue:
        venue_lower = venue.lower()
        if "iclr" in venue_lower:
            venue_section = (
                "\n\n## Venue Standards (ICLR)\n"
                "ICLR expects strong novelty and completeness. "
                "Frame your critical analysis at this bar."
            )
        elif "neurips" in venue_lower:
            venue_section = (
                "\n\n## Venue Standards (NeurIPS)\n"
                "NeurIPS expects technical rigor and breadth of impact. "
                "Frame your critical analysis at this bar."
            )
        elif "thesis" in venue_lower:
            venue_section = (
                "\n\n## 学位论文评审标准\n"
                "按照中国学位论文标准进行批判性分析。重点关注：\n"
                "- 研究问题是否明确且具有工程意义\n"
                "- 方法相对于已有工作的实质性创新\n"
                "- 实验设计是否充分、对比基线是否合理\n"
                "- 论文的结构组织和写作规范性\n"
            )

    # Skeptic questions section
    sq_section = ""
    if skeptic_questions:
        q_lines = "\n".join(f"  Q{j+1}: {q}" for j, q in enumerate(skeptic_questions))
        sq_section = (
            "\n\n## Pre-Generated Probing Questions\n"
            "Address each of these specifically (validate or explain why not applicable):\n"
            f"{q_lines}"
        )

    # Facts section
    facts_section = ""
    if facts:
        parts = []
        if facts.get("claim"):
            parts.append(f"Main claim: {facts['claim']}")
        if facts.get("method_summary"):
            parts.append(f"Method: {facts['method_summary']}")
        if facts.get("key_results"):
            for kr in facts["key_results"]:
                parts.append(f"Result: {kr.get('claim', '')} ({kr.get('section', '')})")
        if facts.get("evidence_map"):
            parts.append(str(facts["evidence_map"])[:16000])
        facts_section = "\n\n## Extracted Facts\n" + "\n".join(parts)

    # Build paper text — use the same group approach but relevant to skeptic
    paper_body, visual_section = _split_visual_evidence(paper_text)
    issue_pattern_section = _build_issue_pattern_context(
        paper_body, [str(skeptic_dim["id"])], facts
    )
    if issue_pattern_section:
        issue_pattern_section = "\n\n" + issue_pattern_section
    group_text = _build_group_text(paper_body, [skeptic_dim["id"]], max_chars=100000)

    # System message — venue-aware
    roles = skeptic_dim.get("role", "")
    sys_prompt = get_review_system_prompt(venue)
    system_msg = roles + "\n\n" + sys_prompt if roles else sys_prompt

    # User content — venue-aware label and prompt
    dim_label = get_dim_label(skeptic_dim, venue)
    dim_prompt = get_dim_prompt(skeptic_dim, venue)
    user_content = (
        f"## Paper Content\n\n{group_text}{visual_section}{issue_pattern_section}{facts_section}{other_context}{sq_section}{venue_section}\n\n"
        f"## Review Task: {dim_label}\n\n{dim_prompt}\n\n"
        "Respond with valid JSON following the schema provided in the system prompt."
    )

    token_usage = {"prompt": 0, "completion": 0, "total": 0}
    for attempt in range(2):
        try:
            resp = client.chat(
                messages=[{"role": "user", "content": user_content}],
                system=system_msg,
                model=model if model else None,
                max_tokens=4096,
                temperature=0.3,
                json_mode=True,
            )
            token_usage = {
                "prompt": getattr(resp, "prompt_tokens", 0),
                "completion": getattr(resp, "completion_tokens", 0),
                "total": getattr(resp, "total_tokens", 0),
            }
            result = json.loads(resp.content)
            return {
                "dimensionId": skeptic_dim["id"],
                "score": max(0, min(100, int(result.get("score", 70)))),
                "summary": str(result.get("summary", ""))[:500],
                "strengths": [str(s)[:200] for s in (result.get("strengths") or [])][:2],
                "weaknesses": [str(w)[:200] for w in (result.get("weaknesses") or [])][:5],
                "suggestions": [str(s)[:200] for s in (result.get("suggestions") or [])][:5],
                "analysis": str(result.get("analysis", ""))[:1000],
                "self_critique": str(result.get("self_critique", ""))[:500],
                "_token_usage": token_usage,
            }
        except Exception as exc:
            if attempt < 1:
                time.sleep(1.5)

    return {
        "dimensionId": skeptic_dim["id"],
        "score": 50,
        "summary": "Skeptic review failed after retries",
        "strengths": ["Unable to complete skeptic review"],
        "weaknesses": [],
        "suggestions": [],
        "analysis": "",
        "self_critique": "",
        "_token_usage": token_usage,
    }


# =============================================================================
# Stage 4: Deep Dive — missed weaknesses & suggestions
# =============================================================================

DEEP_DIVE_PROMPT = (
    "You are an expert reviewer performing a SECOND-PASS deep analysis. "
    "The standard review dimensions have already identified surface-level issues. "
    "Your job is to find DEEPER, more specific problems that standard checklists miss.\n\n"
    "Focus on:\n"
    "1. **Missing baselines** — Are there strong, well-known methods in this sub-field that the paper should compare against but doesn't?\n"
    "2. **Technical claim validity** — Does the paper make claims that aren't fully supported by the evidence shown?\n"
    "3. **Experiment design flaws** — Confounders, missing controls, unfair comparisons, dataset issues.\n"
    "4. **Domain-specific problems** — Things a researcher in THIS specific sub-field would immediately notice.\n"
    "5. **Notation / writing errors** — Inconsistencies, undefined symbols, incorrect statements.\n"
    "6. **Overlooked related work** — Prior work that directly contradicts or anticipates the paper's claims.\n\n"
    "Also check these COMMONLY MISSED categories:\n"
    "7. **Suggestion patterns** — Human reviewers often suggest:\n"
    "   - Evaluating on additional benchmark datasets or larger-scale tasks\n"
    "   - Comparing against specific SOTA methods (name concrete ones)\n"
    "   - Adding theoretical analysis or proof sketches\n"
    "   - Including ablation studies for key design choices\n"
    "   - Discussing broader applicability / limitations\n"
    "8. **Weakness patterns** — Human reviewers frequently flag:\n"
    "   - Unclear experimental setup (datasets, metrics, implementation details not specified)\n"
    "   - Writing clarity issues (poor organization, duplicated content, hard-to-read figures)\n"
    "   - Insufficient evidence for claims (overclaiming relative to results shown)\n\n"
    "ALREADY IDENTIFIED by standard review (DO NOT repeat):\n"
    "{existing_findings}\n\n"
    "Output ONLY valid JSON:\n"
    '{\n'
    '  "weaknesses": ["<deep weakness 1 — cite §/Fig./Table>", "<weakness 2>", ...],\n'
    '  "suggestions": ["<actionable suggestion 1 — cite §/Fig./Table>", "<suggestion 2>", ...],\n'
    '  "summary": "<why these matter — 2-3 sentences>"\n'
    "}\n\n"
    "Rules:\n"
    "- Each item MUST reference a specific section (§), figure, or table.\n"
    "- weaknesses and suggestions should be parallel: suggestion i should directly fix weakness i whenever possible.\n"
    "- For Chinese thesis reviews, write suggestions as concrete expert advice beginning with “建议”.\n"
    "- Max 5 weaknesses, max 5 suggestions.\n"
    '- Be specific: "weak ablation in Table 2" not "weak ablation"\n'
    "- If you can't find anything deeply new, return empty arrays rather than generic filler."
)


def _run_review_deep_dive(
    paper_text: str,
    main_results: list[dict[str, Any]],
    facts: dict[str, Any] | None = None,
    venue: str = "",
    model: str | None = None,
) -> dict[str, Any]:
    """Second-pass deep dive: find weaknesses & suggestions that standard dims missed."""
    try:
        client = get_client_for_model(model or get_primary_model_name())
    except Exception as e:
        return {"dimensionId": "deep_dive", "score": 50, "diagnostic_only": True, "summary": f"Deep dive failed: {e}",
                "strengths": [], "weaknesses": [], "suggestions": [], "analysis": "", "self_critique": ""}

    # Build existing findings summary
    existing = []
    for r in main_results:
        dim_id = r.get("dimensionId", "?")
        ws = "; ".join(r.get("weaknesses", []) or [])
        ss = "; ".join(r.get("suggestions", []) or [])
        if ws:
            existing.append(f"[{dim_id}] Weaknesses: {ws}")
        if ss:
            existing.append(f"[{dim_id}] Suggestions: {ss}")
    existing_str = "\n".join(existing) if existing else "(none found)"

    # Build paper text
    relevant_dims = ["methodology", "novelty", "experiment"]
    paper_body, visual_section = _split_visual_evidence(paper_text)
    issue_pattern_section = _build_issue_pattern_context(paper_body, relevant_dims, facts)
    if issue_pattern_section:
        issue_pattern_section = "\n\n" + issue_pattern_section
    group_text = _build_group_text(paper_body, relevant_dims, max_chars=100000)

    # Venue calibration
    venue_note = ""
    if venue:
        venue_lower = venue.lower()
        if "thesis" in venue_lower:
            venue_note = "\n按照中国学位论文标准进行深层次分析。"
        else:
            venue_note = f"\nCalibrate to {venue.upper()} standards — this is a top-tier venue."

    # Select deep dive prompt based on venue
    if venue and "thesis" in venue.lower():
        deep_dive_prompt = DEEP_DIVE_PROMPT + (
            "\n\nTHESIS-SPECIFIC CHECK:\n"
            "9. **References format** — Are references complete and correctly formatted?\n"
            "10. **English abstract** — Grammar, terminology alignment with Chinese version?\n"
            "11. **Method comparison fairness** — Are baselines from different domains compared fairly?\n"
            "12. **Missing theoretical analysis** — Would convergence/complexity analysis strengthen claims?\n"
        )
    else:
        deep_dive_prompt = DEEP_DIVE_PROMPT

    user_content = (
        f"## Paper Content\n\n{group_text}{visual_section}{issue_pattern_section}\n\n"
        f"## Already Identified Issues\n\n{existing_str}\n\n"
        f"## Task\n\n{deep_dive_prompt}"
        f"{venue_note}"
    )

    system_msg = (
        "You are a critical second-pass reviewer. "
        "Respond with valid JSON only. No other text. "
        "All summary, weakness, and suggestion text must be written in Simplified Chinese."
    )

    for attempt in range(2):
        try:
            resp = client.chat(
                messages=[{"role": "user", "content": user_content}],
                system=system_msg,
                model=model if model else None,
                max_tokens=4096,
                temperature=0.3,
                json_mode=True,
            )
            result = json.loads(resp.content)
            weaknesses = [str(w)[:300] for w in (result.get("weaknesses") or [])][:5]
            suggestions = [str(s)[:300] for s in (result.get("suggestions") or [])][:5]
            return {
                "dimensionId": "deep_dive",
                "score": 50,  # display placeholder; excluded from overall score
                "diagnostic_only": True,
                "summary": str(result.get("summary", ""))[:500],
                "strengths": [],
                "weaknesses": weaknesses,
                "suggestions": suggestions,
                "analysis": "",
                "self_critique": "",
            }
        except Exception as exc:
            if attempt < 1:
                time.sleep(1.5)

    return {
        "dimensionId": "deep_dive", "score": 50, "diagnostic_only": True, "summary": "Deep dive failed after retries",
        "strengths": [], "weaknesses": [], "suggestions": [], "analysis": "", "self_critique": "",
    }


# =============================================================================
# Stage 5: Targeted Patch — commonly missed categories
# =============================================================================

PATCH_PROMPT = (
    "You are a reviewer specializing in catching COMMONLY MISSED issues.\n\n"
    "Examine the paper through these specific lenses — categories that human reviewers frequently flag "
    "but automated reviews systematically overlook.\n\n"
    "Check each category and output any NEW weaknesses or suggestions:\n\n"
    "1. **Missing baseline comparisons** — Does the paper omit well-known baselines in its sub-field?\n"
    "2. **Experiment setup clarity** — Are experimental settings (datasets, metrics, implementation details) "
    "unclear or underspecified?\n"
    "3. **Writing & presentation** — Poor organization, duplicated content, undefined symbols, "
    "unreadable figures, grammar issues.\n"
    "4. **Insufficient evaluation** — Too few datasets/tasks/metrics. Human reviewers often ask for "
    "more benchmarks, larger-scale experiments, or ablation studies.\n"
    "5. **SOTA comparison in suggestions** — If the paper lacks key baselines, "
    "suggest specific SOTA methods it should compare against.\n"
    "6. **Theoretical depth** — Does the paper lack analysis or proof? "
    "Suggest adding theoretical grounding where appropriate.\n\n"
    "ALREADY IDENTIFIED by previous review stages (DO NOT repeat):\n"
    "{existing_findings}\n\n"
    "Output ONLY valid JSON:\n"
    '{\n'
    '  "weaknesses": ["<weakness — cite §/Fig./Table>", ...],\n'
    '  "suggestions": ["<suggestion — cite §/Fig./Table>", ...],\n'
    '  "summary": "<why these matter — 2-3 sentences>"\n'
    "}\n\n"
    "Rules:\n"
    "- Each item MUST reference a specific section, figure, or table.\n"
    "- weaknesses and suggestions should be parallel: suggestion i should directly fix weakness i whenever possible.\n"
    "- For Chinese thesis reviews, write suggestions as concrete expert advice beginning with “建议”.\n"
    "- Max 5 weaknesses, max 5 suggestions.\n"
    '- Be specific: "no ablation study in §4" not "weak evaluation"\n'
    "- Prioritize categories 1 and 4 (most commonly missed by automated review).\n"
    "- If a category has nothing to add, skip it — no generic filler."
)


def _run_review_patch(
    paper_text: str,
    main_results: list[dict[str, Any]],
    facts: dict[str, Any] | None = None,
    venue: str = "",
    model: str | None = None,
) -> dict[str, Any]:
    """Targeted patch: catch commonly missed weakness/suggestion categories."""
    try:
        client = get_client_for_model(model or get_primary_model_name())
    except Exception as e:
        return {"dimensionId": "patch", "score": 50, "diagnostic_only": True, "summary": f"Patch failed: {e}",
                "strengths": [], "weaknesses": [], "suggestions": [], "analysis": "", "self_critique": ""}

    # Build existing findings summary
    existing = []
    for r in main_results:
        dim_id = r.get("dimensionId", "?")
        ws = "; ".join(r.get("weaknesses", []) or [])
        ss = "; ".join(r.get("suggestions", []) or [])
        if ws:
            existing.append(f"[{dim_id}] Weaknesses: {ws}")
        if ss:
            existing.append(f"[{dim_id}] Suggestions: {ss}")
    existing_str = "\n".join(existing) if existing else "(none found)"

    relevant_dims = ["methodology", "novelty", "experiment", "writing"]
    paper_body, visual_section = _split_visual_evidence(paper_text)
    issue_pattern_section = _build_issue_pattern_context(paper_body, relevant_dims, facts)
    if issue_pattern_section:
        issue_pattern_section = "\n\n" + issue_pattern_section
    group_text = _build_group_text(paper_body, relevant_dims, max_chars=100000)

    venue_note = ""
    if venue:
        venue_lower = venue.lower()
        if "thesis" in venue_lower:
            venue_note = "\n按照中国学位论文评审标准。"
        else:
            venue_note = f"\nCalibrate to {venue.upper()} standards."

    # Select patch prompt based on venue
    if venue and "thesis" in venue.lower():
        patch_prompt = PATCH_PROMPT + (
            "\n\nTHESIS-SPECIFIC CHECKLIST:\n"
            "7. **Figure/table formatting** — Proper numbering, titles, and referencing in text?\n"
            "8. **Terminology consistency** — Chinese/English terms defined at first use?\n"
            "9. **Chapter numbering** — Consistent and correct structure?\n"
            "10. **Formula numbering** — Proper cross-referencing of equations?\n"
        )
    else:
        patch_prompt = PATCH_PROMPT

    user_content = (
        f"## Paper Content\n\n{group_text}{visual_section}{issue_pattern_section}\n\n"
        f"## Already Identified Issues\n\n{existing_str}\n\n"
        f"## Task\n\n{patch_prompt}"
        f"{venue_note}"
    )

    system_msg = (
        "You are a critical patch reviewer. Respond with valid JSON only. No other text. "
        "All summary, weakness, and suggestion text must be written in Simplified Chinese."
    )

    for attempt in range(2):
        try:
            resp = client.chat(
                messages=[{"role": "user", "content": user_content}],
                system=system_msg,
                model=model if model else None,
                max_tokens=4096,
                temperature=0.3,
                json_mode=True,
            )
            result = json.loads(resp.content)
            weaknesses = [str(w)[:300] for w in (result.get("weaknesses") or [])][:5]
            suggestions = [str(s)[:300] for s in (result.get("suggestions") or [])][:5]
            return {
                "dimensionId": "patch",
                "score": 50,  # display placeholder; excluded from overall score
                "diagnostic_only": True,
                "summary": str(result.get("summary", ""))[:500],
                "strengths": [],
                "weaknesses": weaknesses,
                "suggestions": suggestions,
                "analysis": "",
                "self_critique": "",
            }
        except Exception as exc:
            if attempt < 1:
                time.sleep(1.5)

    return {
        "dimensionId": "patch", "score": 50, "diagnostic_only": True, "summary": "Patch failed after retries",
        "strengths": [], "weaknesses": [], "suggestions": [], "analysis": "", "self_critique": "",
    }


# =============================================================================
# Expert coverage sweep — target recurring human-review blind spots
# =============================================================================

_COVERAGE_SWEEP_DIMENSIONS = {
    "methodology", "novelty", "experiment", "writing", "related_work",
    "reproducibility", "ethics", "skeptic", "writing_format",
    "structure_logic", "theory_depth",
}

EXPERT_COVERAGE_SWEEP_PROMPT_ZH = """你是中国博士/硕士学位论文盲审专家，执行最后一轮“专家意见覆盖扫描”。
主评审已经完成。你的任务不是重复已有意见，而是检查真实盲审专家经常指出、自动评审容易遗漏的问题。

必须逐项检查：
1. 论文题目、摘要、贡献陈述是否准确，是否与正文范围一致或重复；
2. 研究背景、应用场景和相关工作是否充分，是否缺少最接近工作的实质对比；
3. 理论证明、收敛性、复杂度、最优性和启发式规则依据是否充分；
4. 实验是否覆盖长尾硬件、极低资源、工业设备、混合动态场景、多用户竞争和极端干扰；
5. 是否缺少跨模块联动、端到端系统级验证或统一优化目标；
6. 部署成本、调度开销、内存峰值、通信同步、能耗和工具链是否评估完整；
7. 图表信息是否过载，正文是否解释关键差异、原因和结论对应关系；
8. 术语、英文缩写、公式引出语、图表/算法/公式编号是否统一规范；
9. 英文摘要、参考文献格式、章节组织、讨论与未来工作位置是否规范；
10. 结论是否外推到未经实验验证的任务、设备、数据或场景。
11. 论文题目是否过宽、过窄或未准确覆盖正文核心对象；
12. 摘要、绪论贡献陈述、结论是否存在大段重复或表述不一致；
13. 各章小结是否夹杂已发表论文介绍、未来工作是否分散在各章而非结论展望统一呈现；
14. 图、表、算法、公式编号是否符合学位论文习惯，关键图表是否有正文解释而不只是罗列结果；
15. 算法名、系统名、缩写和中英文术语是否第一次出现即解释，后文是否保持一致。

只报告能够在当前论文中找到明确章节、图、表、公式或页面证据的问题。
不要把“未在节选中看到”当成“论文不存在”。不要输出泛泛建议。
所有文字必须使用简体中文，仅保留必要的英文模型名、数据集名和缩写。
每条 weakness 和 suggestion 必须一一对应：weakness 说明当前论文的问题，suggestion 用“建议……”开头，
指向同一对象，并给出可直接修改论文的动作。不要只给“加强/完善”这种空泛建议。

输出JSON：
{"issues":[
  {"dimension":"experiment","weakness":"具体不足（含证据定位）","suggestion":"一一对应的可操作建议","evidence":"章节/图表/页面及事实","confidence":0.0}
]}

最多12项；没有可靠证据的类别不要凑数。
为避免意见被单一类别占满：
- 同一 dimension 最多2项；
- 理论证明/复杂度类合计最多2项，格式规范类合计最多2项；
- 如果论文证据支持，优先各报告至少1项“跨模块联动或统一系统目标”、
  “多种动态因素叠加的混合/极端场景”和“关键图表差异及原因解释”问题。"""


_COVERAGE_SWEEP_LENSES: tuple[dict[str, Any], ...] = (
    {
        "id": "scope_structure",
        "name": "研究范围与结构专家",
        "dimensions": {"novelty", "writing", "structure_logic", "related_work"},
        "task": (
            "重点核对题目—摘要—贡献—正文—结论是否同一范围；研究问题与创新点是否准确；"
            "章节之间是否递进、重复或关系不清；相关工作是否遗漏最接近路线的实质差异。"
        ),
    },
    {
        "id": "theory_experiment",
        "name": "理论与实验专家",
        "dimensions": {
            "methodology", "experiment", "reproducibility", "skeptic",
            "theory_depth", "ethics",
        },
        "task": (
            "重点核对假设、复杂度、收敛性和适用边界；基线、消融、统计可靠性、长尾/极端/"
            "混合动态场景；跨模块系统验证；时延、内存、通信、能耗和部署成本；复现与安全边界。"
        ),
    },
    {
        "id": "visual_format",
        "name": "图表与学术规范专家",
        "dimensions": {"writing_format", "writing", "structure_logic", "experiment"},
        "task": (
            "重点核对所有图表的可读性、正文解释和结论对应关系；公式、算法、三线表、编号和引用；"
            "英文摘要、缩写首次定义、术语一致性、参考文献著录及学位论文版式。"
        ),
    },
)


def _parse_coverage_sweep_issues(
    data: Any,
    lens: dict[str, Any],
) -> list[dict[str, Any]]:
    """Validate one recall lens without trusting its self-reported confidence."""
    issues = data if isinstance(data, list) else data.get("issues", []) if isinstance(data, dict) else []
    output: list[dict[str, Any]] = []
    allowed_dimensions = set(lens["dimensions"]) & _COVERAGE_SWEEP_DIMENSIONS
    for issue in issues:
        if not isinstance(issue, dict):
            continue
        dimension = str(issue.get("dimension", ""))
        weakness = str(issue.get("weakness", "")).strip()
        suggestion = str(issue.get("suggestion", "")).strip()
        evidence = str(issue.get("evidence", "")).strip()
        try:
            confidence = float(issue.get("confidence", 0.0))
        except (TypeError, ValueError):
            confidence = 0.0
        if (
            dimension not in allowed_dimensions
            or not weakness
            or not suggestion
            or not evidence
            # This is only a recall gate. The shared verifier/debate stage applies
            # the real display threshold, so near-threshold expert candidates are
            # allowed through for focused evidence retrieval.
            or confidence < 0.45
            or not re.search(
                r"§|第\s*\d|图\s*\d|表\s*\d|Page\s*\d|Eq\.?\s*\d|公式\s*\d|算法\s*\d",
                evidence,
                re.I,
            )
        ):
            continue
        if not suggestion.startswith("建议"):
            suggestion = "建议" + suggestion.lstrip("：:，,。 ")
        output.append({
            "dimension": dimension,
            "weakness": weakness[:500],
            "suggestion": suggestion[:500],
            "evidence": evidence[:700],
            "confidence": min(1.0, max(0.0, confidence)),
            "recall_lens": str(lens["id"]),
            "recall_lens_name": str(lens["name"]),
            "source_count": 1,
        })
    return output


def _merge_coverage_sweep_issues(issues: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Merge overlapping lens findings and retain independent-source support."""
    from review_data.issue_patterns import semantic_similarity

    merged: list[dict[str, Any]] = []
    for issue in sorted(issues, key=lambda item: float(item.get("confidence", 0.0)), reverse=True):
        match = next(
            (
                kept for kept in merged
                if kept.get("dimension") == issue.get("dimension")
                and semantic_similarity(
                    str(kept.get("weakness", "")), str(issue.get("weakness", ""))
                ) >= 0.62
            ),
            None,
        )
        if match is None:
            issue["recall_lenses"] = [str(issue.get("recall_lens", ""))]
            merged.append(issue)
            continue
        lens_id = str(issue.get("recall_lens", ""))
        lenses = match.setdefault("recall_lenses", [])
        if lens_id and lens_id not in lenses:
            lenses.append(lens_id)
            match["source_count"] = int(match.get("source_count", 1)) + 1
        if len(str(issue.get("weakness", ""))) > len(str(match.get("weakness", ""))):
            match["weakness"] = issue["weakness"]
        if len(str(issue.get("suggestion", ""))) > len(str(match.get("suggestion", ""))):
            match["suggestion"] = issue["suggestion"]
        if len(str(issue.get("evidence", ""))) > len(str(match.get("evidence", ""))):
            match["evidence"] = issue["evidence"]
        match["confidence"] = max(
            float(match.get("confidence", 0.0)), float(issue.get("confidence", 0.0))
        )
    return merged[:18]


def _run_expert_coverage_sweep(
    paper_text: str,
    existing_results: list[dict[str, Any]],
    facts: dict[str, Any] | None = None,
    model: str | None = None,
) -> list[dict[str, Any]]:
    """Find evidence-backed gaps through parallel, specialized Qwen lenses."""
    try:
        client = get_client_for_model(model or get_primary_model_name())
    except Exception:
        return []

    paper_body, visual_section = _split_visual_evidence(paper_text)
    relevant_dimensions = sorted(_COVERAGE_SWEEP_DIMENSIONS)
    issue_patterns = _build_issue_pattern_context(
        paper_body, relevant_dimensions, facts,
    )[:6000]
    existing = "\n".join(
        f"[{result.get('dimensionId', '?')}] {weakness}"
        for result in existing_results
        for weakness in (result.get("weaknesses", []) or [])
    )
    evidence_map = str((facts or {}).get("evidence_map", ""))[:8000]
    shared_context = (
        f"## 论文正文\n{_build_group_text(paper_body, relevant_dimensions, max_chars=30000)}"
        f"\n\n## 图表证据摘录\n{visual_section[:8000]}"
        f"\n\n## EvidenceMap\n{evidence_map}\n\n"
        f"## 其他论文的专家问题模式（仅作检查清单，不是当前论文事实）\n{issue_patterns}\n\n"
        f"## 已发现问题（不要重复）\n{existing[:6000]}"
    )

    def run_lens(lens: dict[str, Any]) -> list[dict[str, Any]]:
        prompt = (
            f"{shared_context}\n\n## 本轮专家身份\n{lens['name']}\n"
            f"允许输出维度：{', '.join(sorted(lens['dimensions']))}\n"
            f"专项任务：{lens['task']}\n\n"
            f"## 通用扫描规则\n{EXPERT_COVERAGE_SWEEP_PROMPT_ZH}\n\n"
            "本视角最多输出6项；没有明确证据就返回空 issues。"
        )
        try:
            response = client.chat(
                messages=[{"role": "user", "content": prompt}],
                system=(
                    f"你是{lens['name']}。只输出有效JSON。"
                    "所有意见必须使用简体中文，并以当前论文证据为依据。"
                ),
                model=model if model else None,
                max_tokens=3072,
                temperature=0.15,
                json_mode=True,
            )
            raw = response.content.strip()
            if raw.startswith("```"):
                raw = raw.split("\n", 1)[-1].rsplit("```", 1)[0]
            data = json.loads(raw)
        except Exception as exc:
            print(f"[coverage-sweep:{lens['id']}] Qwen call failed: {exc}")
            return []
        return _parse_coverage_sweep_issues(data, lens)

    collected: list[dict[str, Any]] = []
    # Independent recall views run concurrently, so recall rises without tripling
    # user-visible latency. All use the same Qwen model policy.
    with ThreadPoolExecutor(max_workers=len(_COVERAGE_SWEEP_LENSES)) as pool:
        futures = [pool.submit(run_lens, lens) for lens in _COVERAGE_SWEEP_LENSES]
        for future in as_completed(futures):
            try:
                collected.extend(future.result())
            except Exception as exc:
                print(f"[coverage-sweep] lens failed: {exc}")
    return _merge_coverage_sweep_issues(collected)


def _merge_coverage_sweep(
    results: list[dict[str, Any]],
    issues: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    by_dimension = {
        str(result.get("dimensionId", "")): result
        for result in results
        if result.get("dimensionId") in _COVERAGE_SWEEP_DIMENSIONS
    }
    for issue in issues:
        result = by_dimension.get(str(issue.get("dimension", "")))
        if not result:
            continue
        result.setdefault("weaknesses", []).append(str(issue["weakness"]))
        result.setdefault("suggestions", []).append(str(issue["suggestion"]))
        result.setdefault("_coverage_sweep", []).append(issue)
    return results


_CHINESE_NORMALIZATION_SKIP_KEYS = {
    "candidate_id", "dimension", "dimensionId", "source_dimensions",
    "severity", "verdict", "position", "category", "id", "model",
    # Evidence locators are identifiers, not prose. Translating or replacing
    # values such as ``Section 3`` and ``E0042`` destroys auditability.
    "evidence_locators", "evidenceLocators", "rule_id", "unit_id",
}


def _needs_chinese_normalization(text: str) -> bool:
    """Detect English-dominant outward prose while allowing technical names."""
    chinese_count = len(re.findall(r"[\u4e00-\u9fff]", text))
    latin_count = len(re.findall(r"[A-Za-z]", text))
    english_words = len(re.findall(r"\b[A-Za-z]{2,}\b", text))
    if latin_count < 4:
        return False
    return chinese_count == 0 or (english_words >= 5 and latin_count > chinese_count * 2)


def _normalize_outward_chinese(
    payload: Any,
    client: Any | None,
    model: str | None,
) -> Any:
    """Translate English-only outward review prose and enforce a Chinese fallback."""
    references: list[tuple[Any, Any, str]] = []

    def collect(value: Any, parent: Any = None, key: Any = None) -> None:
        if isinstance(value, dict):
            for child_key, child in value.items():
                if child_key not in _CHINESE_NORMALIZATION_SKIP_KEYS:
                    collect(child, value, child_key)
        elif isinstance(value, list):
            for index, child in enumerate(value):
                collect(child, value, index)
        elif isinstance(value, str) and _needs_chinese_normalization(value):
            references.append((parent, key, value))

    collect(payload)
    if not references:
        return payload

    translated: dict[str, str] = {}
    if client is not None:
        for batch_start in range(0, len(references), 60):
            batch = references[batch_start:batch_start + 60]
            items = [
                {"id": f"T{batch_start + offset:04d}", "text": original[:900]}
                for offset, (_, _, original) in enumerate(batch)
            ]
            prompt = (
                "将以下自动评审对外文本准确翻译为简体中文。不得增删事实、证据定位、"
                "数值、置信度或批评强度；模型名、数据集名、缩写和数学符号可保留英文。"
                "只返回JSON：{\"translations\":[{\"id\":\"T0000\",\"text\":\"中文\"}]}\n"
                + json.dumps(items, ensure_ascii=False)
            )
            try:
                response = client.chat(
                    messages=[{"role": "user", "content": prompt}],
                    system="你是学术评审文本翻译器，只输出有效JSON，所有译文必须包含简体中文。",
                    model=model if model else None,
                    max_tokens=8192,
                    temperature=0.0,
                    json_mode=True,
                )
                data = json.loads(response.content)
                for item in data.get("translations", []):
                    if not isinstance(item, dict):
                        continue
                    text = str(item.get("text", "")).strip()
                    if (
                        re.search(r"[\u4e00-\u9fff]", text)
                        and not _needs_chinese_normalization(text)
                    ):
                        translated[str(item.get("id", ""))] = text
            except Exception:
                continue

    for index, (parent, key, original) in enumerate(references):
        replacement = translated.get(f"T{index:04d}")
        if not replacement:
            replacement = "该条评审文本的中文转换失败，原英文内容已隐藏。"
        if parent is not None:
            parent[key] = replacement
    return payload


def _compact_issue_text(text: str, max_chars: int = 220) -> str:
    """Remove debate narration and keep a concise evidence-bearing issue."""
    text = re.sub(
        r"^(?:评审意见指出|该质疑部分成立，但表述过于绝对|该问题被部分接受但范围被窄化)[，。：:\s]*",
        "",
        text.strip(),
    )
    if len(text) <= max_chars:
        return text
    sentences = [
        sentence.strip()
        for sentence in re.split(r"(?<=。)", text)
        if sentence.strip()
    ]
    issue_terms = (
        "缺乏", "不足", "未提供", "未展示", "未能", "遗漏", "无法",
        "不完整", "不明确", "局限", "问题", "风险",
    )
    preferred = [
        sentence for sentence in sentences
        if any(term in sentence for term in issue_terms)
        and not sentence.startswith(("然而", "鉴于", "因此"))
    ]
    selected = preferred or sentences
    compact = ""
    for sentence in selected:
        if not compact and len(sentence) > max_chars:
            compact = sentence[:max_chars].rstrip("，；、 ") + "。"
            break
        if compact and len(compact) + len(sentence) > max_chars:
            break
        compact += sentence
        if len(compact) >= max_chars * 0.65:
            break
    return (compact or text[:max_chars]).strip()


# =============================================================================
# Cross-dimension deduplication
# =============================================================================


def _text_overlap_ratio(a: str, b: str) -> float:
    """Multilingual semantic-token similarity for Chinese and English."""
    try:
        from review_data.issue_patterns import semantic_similarity
        return semantic_similarity(a, b)
    except Exception:
        wa = set(a.lower().split())
        wb = set(b.lower().split())
        if not wa or not wb:
            return 0.0
        return len(wa & wb) / min(len(wa), len(wb))


def _dedup_cross_dimensions(results: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Remove near-duplicate strengths/weaknesses/suggestions across dimensions.

    If two dimensions produce the same idea, keep only the first occurrence.
    This maximizes unique coverage per point and improves useful rate.
    """
    for key in ("strengths",):
        seen_texts: list[str] = []
        for r in results:
            items = r.get(key, [])
            if not items:
                continue
            deduped = []
            for item in items:
                is_dup = any(
                    _text_overlap_ratio(item, seen) >= 0.85
                    for seen in seen_texts
                )
                if not is_dup:
                    deduped.append(item)
                    seen_texts.append(item)
            r[key] = deduped
    seen_weaknesses: list[str] = []
    for result in results:
        findings = result.get("findings", []) or []
        if not findings:
            continue
        retained: list[dict[str, Any]] = []
        for finding in findings:
            if not isinstance(finding, dict):
                continue
            weakness = str(finding.get("weakness", "") or finding.get("text", "")).strip()
            if not weakness or any(
                _text_overlap_ratio(weakness, seen) >= 0.85
                for seen in seen_weaknesses
            ):
                continue
            retained.append(finding)
            seen_weaknesses.append(weakness)
        result["findings"] = retained
        result["weaknesses"] = [
            str(finding.get("weakness", "") or finding.get("text", "")).strip()
            for finding in retained
        ]
        result["suggestions"] = [
            str(finding.get("suggestion", "")).strip()
            for finding in retained
            if str(finding.get("suggestion", "")).strip()
        ]
    return results


# =============================================================================
# Key findings consolidation — top ~5 weaknesses & suggestions
# =============================================================================

_CRITICAL_KEYWORDS = [
    "error", "incorrect", "wrong", "invalid", "fatal", "missing",
    "错误", "不正确", "缺失", "遗漏", "矛盾",
]


def _score_weakness_importance(
    weakness: str,
    dim_score: int,
    dim_weight: float = 1.0,
) -> float:
    """Score a weakness by importance for ranking.

    Factors:
    - Lower dimension score → higher importance (the dimension found bigger problems)
    - Longer / more specific → higher importance (references sections, figures)
    - Presence of strong critical keywords → boost
    """
    score = (100 - dim_score) * 0.5 * dim_weight  # base from dim score

    # Specificity bonus: references to sections, figures, tables
    refs = len(re.findall(r'[§§]|Fig\.|Table|算法|图\s*\d|表\s*\d|第\s*[一二三四五六七八九十]|第\s*\d\.\d', weakness))
    score += min(refs * 5, 15)

    # Length bonus (longer = more detailed)
    score += min(len(weakness) * 0.1, 10)

    # Critical keyword boost
    has_critical = any(kw in weakness.lower() for kw in _CRITICAL_KEYWORDS)
    if has_critical:
        score += 10

    return score


def _score_suggestion_importance(
    suggestion: str,
    dim_score: int,
) -> float:
    """Score a suggestion by actionability."""
    score = (100 - dim_score) * 0.3

    # Specificity: actionable suggestions reference concrete methods
    refs = len(re.findall(r'[§§]|Fig\.|Table|算法|图\s*\d|表\s*\d|第\s*[一二三四五六七八九十]|第\s*\d\.\d|should|consider|add|include|evaluate|compare',
                          suggestion))
    score += min(refs * 4, 12)

    score += min(len(suggestion) * 0.08, 8)

    return score


def _get_severity(weakness: str, dim_score: int) -> str:
    """Assign severity: critical / major / minor."""
    if dim_score < 40:
        return "critical"
    if dim_score < 60:
        # Check for critical keywords
        if any(kw in weakness.lower() for kw in _CRITICAL_KEYWORDS):
            return "critical"
        return "major"
    if dim_score < 80:
        return "major"
    return "minor"


def _consolidate_key_findings(
    results: list[dict[str, Any]],
) -> dict[str, list[dict[str, Any]]]:
    """Consolidate top ~5 weaknesses and ~5 suggestions from all dimensions.

    Returns:
        dict with keys:
        - weaknesses: top ranked weaknesses [{text, severity, dimensionId, dimScore}]
        - suggestions: top ranked suggestions [{text, dimensionId, dimScore}]
    """
    # Dimension importance weights
    dim_weights = {
        "methodology": 1.2,
        "novelty": 1.1,
        "experiment": 1.2,
        "writing": 0.8,
        "related_work": 0.7,
        "reproducibility": 0.8,
        "ethics": 0.5,
        "skeptic": 0.9,
        "writing_format": 0.8,
        "structure_logic": 0.7,
        "theory_depth": 0.9,
        "deep_dive": 1.0,
        "patch": 0.9,
    }

    # Skip meta-dimensions that don't have actionable items
    skip_dims = {"deep_dive", "patch"}

    all_weaknesses: list[dict[str, Any]] = []
    all_suggestions: list[dict[str, Any]] = []

    for r in results:
        dim_id = r.get("dimensionId", "")
        dim_score = r.get("score", 70)
        weight = dim_weights.get(dim_id, 0.8)

        weaknesses = r.get("weaknesses", []) or []
        for w in weaknesses:
            w_str = str(w).strip()
            if w_str and w_str not in ("Unable to complete review", "Review process encountered an error"):
                severity = _get_severity(w_str, dim_score)
                all_weaknesses.append({
                    "text": w_str,
                    "severity": severity,
                    "dimensionId": dim_id,
                    "dimScore": dim_score,
                    "_score": _score_weakness_importance(w_str, dim_score, weight),
                })

        suggestions = r.get("suggestions", []) or []
        for s in suggestions:
            s_str = str(s).strip()
            if s_str:
                all_suggestions.append({
                    "text": s_str,
                    "dimensionId": dim_id,
                    "dimScore": dim_score,
                    "_score": _score_suggestion_importance(s_str, dim_score),
                })

    # Deduplicate using existing overlap function
    def dedup_and_rank(items: list[dict[str, Any]], top_k: int = 5) -> list[dict[str, Any]]:
        if not items:
            return []

        # Sort by score descending
        items.sort(key=lambda x: x["_score"], reverse=True)

        # Greedy dedup: keep highest-scoring, skip similar
        kept: list[dict[str, Any]] = []
        seen_texts: list[str] = []
        for item in items:
            is_dup = any(
                _text_overlap_ratio(item["text"], seen) >= 0.75
                for seen in seen_texts
            )
            if not is_dup:
                kept.append(item)
                seen_texts.append(item["text"])
                if len(kept) >= top_k:
                    break

        return kept

    top_weaknesses = dedup_and_rank(all_weaknesses, 5)
    top_suggestions = dedup_and_rank(all_suggestions, 5)

    # Clean internal score field
    for w in top_weaknesses:
        w.pop("_score", None)
    for s in top_suggestions:
        s.pop("_score", None)

    return {
        "weaknesses": top_weaknesses,
        "suggestions": top_suggestions,
    }


_CATEGORY_SPECS: list[tuple[str, str, list[str]]] = [
    ("innovation", "创新性", ["novelty"]),
    ("experiment", "实验与结果", ["experiment", "reproducibility"]),
    ("format", "格式与排版", ["writing_format"]),
    ("structure_layout", "结构与布局", ["structure_logic", "writing"]),
    ("method_theory", "方法与理论", ["methodology", "theory_depth"]),
    ("literature", "相关工作", ["related_work"]),
    ("risk_assumptions", "风险与假设", ["ethics", "skeptic"]),
]


def _confidence_label(value: float) -> str:
    if value >= 0.75:
        return "high"
    if value >= 0.5:
        return "medium"
    return "low"


def _finding_citation_ratio(results: list[dict[str, Any]]) -> float:
    items: list[str] = []
    for result in results:
        items.extend(str(item) for item in (result.get("strengths", []) or []))
        items.extend(str(item) for item in (result.get("weaknesses", []) or []))
    if not items:
        return 0.0
    cited = sum(bool(re.search(
        r"§|第\s*\d|图\s*\d|表\s*\d|Fig\.?\s*\d|Table\s*\d|Eq\.?\s*\d|Page\s*\d",
        item,
        re.IGNORECASE,
    )) for item in items)
    return cited / len(items)


def _build_categorized_findings(
    results: list[dict[str, Any]],
    verified_findings: list[dict[str, Any]],
    meta: dict[str, Any],
) -> list[dict[str, Any]]:
    # Aggregate detailed dimension reviews into user-facing report sections.
    by_dimension = {str(result.get("dimensionId", "")): result for result in results}
    evidence_units = int((meta.get("evidence_map") or {}).get("units", 0))
    visual_used = bool(meta.get("vision_extracted"))
    output: list[dict[str, Any]] = []
    for category_id, label, dimension_ids in _CATEGORY_SPECS:
        category_results = [by_dimension[dim] for dim in dimension_ids if dim in by_dimension]
        if not category_results:
            continue
        category_issues = [
            issue for issue in verified_findings
            if str(issue.get("dimension", "")) in dimension_ids
        ]
        citation_ratio = _finding_citation_ratio(category_results)
        base_confidence = 0.35 + 0.25 * citation_ratio
        if evidence_units:
            base_confidence += 0.12
        if visual_used and category_id in {"experiment", "format", "structure_layout"}:
            base_confidence += 0.08
        if category_issues:
            verified = sum(float(issue.get("evidence_confidence", 0.0)) for issue in category_issues) / len(category_issues)
            confidence = 0.65 * verified + 0.35 * base_confidence
            basis = "verified_issues+citations+evidence_map"
        else:
            confidence = min(base_confidence, 0.72)
            basis = "citations+evidence_map"
        confidence = round(max(0.0, min(1.0, confidence)), 3)

        def collect(field: str) -> list[dict[str, str]]:
            return [
                {"text": str(item), "dimension": str(result.get("dimensionId", ""))}
                for result in category_results
                for item in (result.get(field, []) or [])
                if str(item).strip()
            ]

        output.append({
            "id": category_id,
            "label": label,
            "dimensions": [str(result.get("dimensionId", "")) for result in category_results],
            "score": round(sum(float(result.get("score", 0)) for result in category_results) / len(category_results), 1),
            "confidence": confidence,
            "confidenceLevel": _confidence_label(confidence),
            "confidenceBasis": basis,
            "summaries": [
                {"text": str(result.get("summary", "")), "dimension": str(result.get("dimensionId", ""))}
                for result in category_results if str(result.get("summary", "")).strip()
            ],
            "strengths": collect("strengths"),
            "weaknesses": collect("weaknesses"),
            "suggestions": collect("suggestions"),
            "verifiedFindings": category_issues,
        })
    return output


def _build_confidence_summary(
    verified_findings: list[dict[str, Any]],
    metrics: dict[str, Any],
    meta: dict[str, Any],
) -> dict[str, Any]:
    confidences = [float(issue.get("evidence_confidence", 0.0)) for issue in verified_findings]
    overall = round(sum(confidences) / len(confidences), 3) if confidences else 0.0
    bands = {
        "high": sum(value >= 0.75 for value in confidences),
        "medium": sum(0.5 <= value < 0.75 for value in confidences),
        "low": sum(value < 0.5 for value in confidences),
    }
    return {
        "overall": overall,
        "level": _confidence_label(overall),
        "issueBands": bands,
        "verdicts": {
            "supported": int(metrics.get("supported_candidates", 0)),
            "uncertain": int(metrics.get("uncertain_candidates", 0)),
            "contradicted": int(metrics.get("contradicted_candidates", 0)),
        },
        "evidenceUnits": int((meta.get("evidence_map") or {}).get("units", 0)),
        "visualEvidenceUsed": bool(meta.get("vision_extracted")),
        "visualPages": list(meta.get("vision_selected_pages", []) or []),
        "visualRegions": int(meta.get("vision_detected_regions", meta.get("vision_images", 0))),
        "visualCoverageMode": str(meta.get("vision_coverage_mode", "all_figures_tables")),
        "visualCoverageComplete": bool(meta.get("vision_coverage_complete", False)),
        "visualFailedPages": list(meta.get("vision_failed_pages", []) or []),
        "debatesTriggered": int(metrics.get("debates_triggered", 0)),
        "highImpactDebates": int(metrics.get("high_impact_debates", 0)),
        "debateDisagreements": int(metrics.get("debate_disagreements", 0)),
        "debateRouteCounts": dict(metrics.get("debate_route_counts", {}) or {}),
        "claimEvidenceCoverage": float(metrics.get("claim_evidence_coverage", 0.0)),
        "claimEvidenceNeedsVerification": int(
            metrics.get("claim_evidence_needs_verification", 0)
        ),
        "hybridDebateAgents": int(metrics.get("hybrid_debate_agents", 0)),
        "hybridDebateRoles": list(metrics.get("hybrid_debate_roles", []) or []),
        "note": "Confidence measures evidence support for generated findings, not the probability that the paper is correct.",
    }


def _as_text_items(items: Any, limit: int = 5) -> list[dict[str, Any]]:
    output: list[dict[str, Any]] = []
    for item in items or []:
        if isinstance(item, dict):
            text = str(item.get("text", "") or item.get("summary", "")).strip()
            if not text:
                continue
            clean = {k: v for k, v in item.items() if not str(k).startswith("_")}
            clean["text"] = text
            output.append(clean)
        else:
            text = str(item).strip()
            if text:
                output.append({"text": text})
        if len(output) >= limit:
            break
    return output


def _summary_strengths(overall_summary: dict[str, Any] | None) -> list[dict[str, Any]]:
    if not isinstance(overall_summary, dict):
        return []
    if isinstance(overall_summary.get("reviewers"), list):
        items: list[str] = []
        for reviewer in overall_summary.get("reviewers", []):
            items.extend(str(item) for item in (reviewer.get("highlights") or []) if str(item).strip())
        return _as_text_items(items, limit=5)
    for key in ("detailedStrengths", "topStrengths", "strengths"):
        if overall_summary.get(key):
            return _as_text_items(overall_summary.get(key), limit=5)
    return []


def _summary_weaknesses(overall_summary: dict[str, Any] | None) -> list[dict[str, Any]]:
    if not isinstance(overall_summary, dict):
        return []
    if isinstance(overall_summary.get("reviewers"), list):
        items: list[str] = []
        for reviewer in overall_summary.get("reviewers", []):
            items.extend(str(item) for item in (reviewer.get("keyIssues") or []) if str(item).strip())
        return _as_text_items(items, limit=5)
    for key in ("detailedWeaknesses", "topWeaknesses", "weaknesses"):
        if overall_summary.get(key):
            return _as_text_items(overall_summary.get(key), limit=5)
    return []


def _summary_suggestions(overall_summary: dict[str, Any] | None) -> list[dict[str, Any]]:
    if not isinstance(overall_summary, dict):
        return []
    if isinstance(overall_summary.get("reviewers"), list):
        items: list[str] = []
        for reviewer in overall_summary.get("reviewers", []):
            items.extend(str(item) for item in (reviewer.get("improvementAdvice") or []) if str(item).strip())
        return _as_text_items(items, limit=5)
    for key in ("detailedSuggestions", "keySuggestions", "suggestions"):
        if overall_summary.get(key):
            return _as_text_items(overall_summary.get(key), limit=5)
    return []


def _dimension_label_zh(dimension_id: str) -> str:
    dim = dim_by_id(dimension_id)
    if dim:
        return str(get_dim_label(dim, "THESIS"))
    return dimension_id


def _overall_comment_from_summary(
    overall_summary: dict[str, Any] | None,
    results: list[dict[str, Any]],
    overall_score: int,
) -> str:
    if isinstance(overall_summary, dict):
        if overall_summary.get("executiveSummary"):
            return str(overall_summary["executiveSummary"]).strip()
        if overall_summary.get("overallAssessment"):
            return str(overall_summary["overallAssessment"]).strip()
        if isinstance(overall_summary.get("reviewers"), list):
            comments = [
                str(reviewer.get("overallEvaluation", "")).strip()
                for reviewer in overall_summary.get("reviewers", [])[:2]
                if str(reviewer.get("overallEvaluation", "")).strip()
            ]
            if comments:
                return " ".join(comments)
        final_recommendation = overall_summary.get("finalRecommendation")
        if isinstance(final_recommendation, dict):
            comment = str(final_recommendation.get("summary", "") or final_recommendation.get("description", "")).strip()
            if comment:
                return comment
    best = max(results, key=lambda item: float(item.get("score", 0)), default={})
    weak = min(results, key=lambda item: float(item.get("score", 0)), default={})
    return (
        f"综合评分为 {overall_score}/100。整体来看，论文在"
        f"{_dimension_label_zh(str(best.get('dimensionId', '')))}方面表现较好，"
        f"但在{_dimension_label_zh(str(weak.get('dimensionId', '')))}方面仍有优先修改空间。"
    )


def _build_priority_actions(
    verified_findings: list[dict[str, Any]],
    key_findings: dict[str, Any],
    limit: int = 5,
) -> list[dict[str, Any]]:
    actions: list[dict[str, Any]] = []
    for issue in sorted(
        verified_findings or [],
        key=lambda item: float(item.get("priority_score", 0.0)),
        reverse=True,
    ):
        text = str(issue.get("text", "")).strip()
        suggestion = str(issue.get("suggestion", "")).strip()
        if not text and not suggestion:
            continue
        actions.append({
            "issue": text,
            "suggestion": suggestion,
            "dimensionId": str(issue.get("dimension", "")),
            "severity": str(issue.get("severity", "major")),
            "confidence": float(issue.get("evidence_confidence", 0.0) or 0.0),
            "priorityScore": float(issue.get("priority_score", 0.0) or 0.0),
            "evidence": str(issue.get("evidence", "")),
            "evidenceLocators": list(issue.get("evidence_locators", []) or []),
            "evidenceExcerpt": str(issue.get("evidence_excerpt", "") or issue.get("evidence", "")),
            "claimImpact": float(issue.get("claim_impact", 0.0) or 0.0),
            "fixability": float(issue.get("fixability", 0.0) or 0.0),
            "counterfactual": str(issue.get("counterfactual", "")),
            "counterfactualImpact": str(issue.get("counterfactual_impact", "unknown")),
            "issueCategory": str(issue.get("issue_category", "other")),
            "auditType": str(issue.get("audit_type", "")),
            "needsNewExperiment": issue.get("needs_new_experiment"),
            "debateRoute": str(issue.get("debate_route", "")),
        })
        if len(actions) >= limit:
            return actions
    weaknesses = key_findings.get("weaknesses", []) if isinstance(key_findings, dict) else []
    suggestions = key_findings.get("suggestions", []) if isinstance(key_findings, dict) else []
    for weakness in weaknesses or []:
        if len(actions) >= limit:
            break
        actions.append({
            "issue": str(weakness.get("text", "") if isinstance(weakness, dict) else weakness),
            "suggestion": "",
            "dimensionId": str(weakness.get("dimensionId", "") if isinstance(weakness, dict) else ""),
            "severity": str(weakness.get("severity", "major") if isinstance(weakness, dict) else "major"),
            "confidence": float(weakness.get("confidence", 0.0) if isinstance(weakness, dict) else 0.0),
            "priorityScore": float(weakness.get("priorityScore", 0.0) if isinstance(weakness, dict) else 0.0),
            "evidence": str(weakness.get("evidence", "") if isinstance(weakness, dict) else ""),
        })
    for suggestion in suggestions or []:
        if len(actions) >= limit:
            break
        text = str(suggestion.get("text", "") if isinstance(suggestion, dict) else suggestion).strip()
        if not text:
            continue
        actions.append({
            "issue": text,
            "suggestion": text,
            "dimensionId": str(suggestion.get("dimensionId", "") if isinstance(suggestion, dict) else ""),
            "severity": "major",
            "confidence": float(suggestion.get("confidence", 0.0) if isinstance(suggestion, dict) else 0.0),
            "priorityScore": float(suggestion.get("priorityScore", 0.0) if isinstance(suggestion, dict) else 0.0),
            "evidence": str(suggestion.get("evidence", "") if isinstance(suggestion, dict) else ""),
        })
    return actions


def _deduplicate_verified_findings(
    findings: list[dict[str, Any]],
    threshold: float = 0.72,
) -> tuple[list[dict[str, Any]], int]:
    """Keep the strongest version of repeated cross-dimension criticisms.

    Consensus operates per candidate, so two reviewer dimensions can still
    verify the same underlying problem.  Ranking first ensures that the copy
    with the strongest evidence/impact survives; its dimension coverage is
    retained for audit and exports.
    """
    ranked = sorted(
        findings or [],
        key=lambda item: float(item.get("priority_score", 0.0) or 0.0),
        reverse=True,
    )
    kept: list[dict[str, Any]] = []
    duplicate_count = 0
    for finding in ranked:
        text = str(finding.get("text", "")).strip()
        if not text:
            continue
        duplicate = next((
            item for item in kept
            if _text_overlap_ratio(text, str(item.get("text", ""))) >= threshold
        ), None)
        if duplicate is None:
            copy = dict(finding)
            copy["related_dimensions"] = [str(finding.get("dimension", ""))]
            kept.append(copy)
            continue
        duplicate_count += 1
        dimension = str(finding.get("dimension", ""))
        dimensions = duplicate.setdefault("related_dimensions", [])
        if dimension and dimension not in dimensions:
            dimensions.append(dimension)
    return kept, duplicate_count


def _build_modification_tasks(actions: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Turn evidence-gated comments into author-facing, verifiable work items."""
    tasks: list[dict[str, Any]] = []
    for index, action in enumerate(actions, 1):
        dimension = str(action.get("dimensionId", ""))
        needs_experiment = bool(action.get("needsNewExperiment")) or (
            dimension in {"experiment", "methodology", "reproducibility"} and (
            str(action.get("counterfactualImpact", "")) == "high"
            or "实验" in str(action.get("suggestion", ""))
            or "baseline" in str(action.get("issue", "")).lower()
            )
        )
        category = str(action.get("issueCategory", "other"))
        expected_deliverable = {
            "experiment_coverage": "更新后的实验设计、结果表/图及对应统计文件",
            "theory_assumption": "新增或修订后的理论推导、假设与适用边界说明",
            "visual_format": "修订后的图表及正文逐项引用和解释",
            "related_work": "新增的相关工作对比与差异定位段落或对比表",
            "reproducibility": "可执行配置、参数、随机种子及复现说明",
            "scope_coherence": "统一后的摘要、贡献、正文结论与适用范围表述",
        }.get(category, "修改后的正文段落及其证据定位")
        verification_checks = [
            "问题、建议和修改产物指向同一论文对象",
            "修改后正文可由章节、页码、图表或证据单元重新定位",
            "新增数字与实验产物一致，未扩大原有结论范围",
        ]
        if needs_experiment:
            verification_checks.append("实验可复现，并报告多 seed 统计或明确说明不适用")
        acceptance = (
            "新增实验产物可复现，正文数字与结果文件一致，并报告多 seed 统计或明确说明不适用。"
            if needs_experiment else
            "对应章节已完成修改，问题陈述、证据定位和结论边界一致，复核后不再触发该意见。"
        )
        tasks.append({
            "id": f"AR-T{index:02d}",
            "title": str(action.get("issue", ""))[:120],
            "location": (
                "、".join(str(item) for item in (action.get("evidenceLocators", []) or []))
                or str(action.get("evidence", ""))
                or "需在正文中进一步定位"
            ),
            "evidenceExcerpt": str(action.get("evidenceExcerpt", "")),
            "goal": str(action.get("counterfactual", "")) or "消除该问题对论文可信度和可复现性的影响。",
            "action": str(action.get("suggestion", "")) or "补充证据并修改对应章节。",
            "needsExperiment": needs_experiment,
            "expectedDeliverable": expected_deliverable,
            "verificationChecks": verification_checks,
            "acceptanceCriteria": acceptance,
            "priorityScore": float(action.get("priorityScore", 0.0) or 0.0),
            "confidence": float(action.get("confidence", 0.0) or 0.0),
        })
    return tasks


def _build_report_summary(
    results: list[dict[str, Any]],
    meta: dict[str, Any],
    overall_summary: dict[str, Any] | None,
    overall_score: int,
) -> dict[str, Any]:
    """Build one stable report view consumed by API, UI, and exports."""
    verified_findings = list(meta.get("verifiedFindings", []) or [])
    key_findings = meta.get("keyFindings") or _consolidate_key_findings(results)
    strengths = _summary_strengths(overall_summary)
    if not strengths:
        strengths = _as_text_items(
            [
                {"text": str(item), "dimensionId": str(result.get("dimensionId", ""))}
                for result in results
                for item in (result.get("strengths", []) or [])
            ],
            limit=5,
        )
    ranked_verified, duplicate_findings_merged = _deduplicate_verified_findings(
        verified_findings
    )
    # Product-facing weaknesses and suggestions must come from the evidence
    # gate. The free-form overall-summary model may summarize these findings,
    # but it cannot introduce a new criticism that bypasses consensus.
    weaknesses = _as_text_items([
        {
            "text": str(item.get("text", "")),
            "dimensionId": str(item.get("dimension", "")),
            "severity": str(item.get("severity", "major")),
            "confidence": float(item.get("evidence_confidence", 0.0) or 0.0),
            "priorityScore": float(item.get("priority_score", 0.0) or 0.0),
            "evidence": str(item.get("evidence", "")),
            "evidenceLocators": list(item.get("evidence_locators", []) or []),
            "evidenceExcerpt": str(item.get("evidence_excerpt", "") or item.get("evidence", "")),
        }
        for item in ranked_verified if str(item.get("text", "")).strip()
    ], limit=5)
    suggestions = _as_text_items([
        {
            "text": str(item.get("suggestion", "")),
            "dimensionId": str(item.get("dimension", "")),
            "severity": str(item.get("severity", "major")),
            "confidence": float(item.get("evidence_confidence", 0.0) or 0.0),
            "priorityScore": float(item.get("priority_score", 0.0) or 0.0),
            "evidence": str(item.get("evidence", "")),
            "evidenceLocators": list(item.get("evidence_locators", []) or []),
            "evidenceExcerpt": str(item.get("evidence_excerpt", "") or item.get("evidence", "")),
        }
        for item in ranked_verified if str(item.get("suggestion", "")).strip()
    ], limit=5)

    best = max(results, key=lambda item: float(item.get("score", 0)), default={})
    if ranked_verified:
        primary = str(ranked_verified[0].get("text", "")).strip()
        overall_comment = (
            f"综合评分为 {overall_score}/100。论文在"
            f"{_dimension_label_zh(str(best.get('dimensionId', '')))}方面表现相对较好。"
            f"经证据核验与置信度门控，本轮保留 {len(ranked_verified)} 条重点不足；"
            f"当前最优先处理的问题是：{primary}"
        )
    else:
        overall_comment = (
            f"综合评分为 {overall_score}/100。论文在"
            f"{_dimension_label_zh(str(best.get('dimensionId', '')))}方面表现相对较好。"
            "本轮未发现达到重点问题展示标准的高置信不足，低置信候选已隐藏。"
        )

    priority_actions = _build_priority_actions(ranked_verified, key_findings, limit=5)
    return {
        "overallComment": overall_comment,
        "overallScore": overall_score,
        "strengths": strengths[:5],
        "weaknesses": weaknesses[:5],
        "suggestions": suggestions[:5],
        "findings": verified_findings,
        "priorityActions": priority_actions,
        "modificationTasks": _build_modification_tasks(priority_actions),
        "filterSummary": {
            "filteredCount": len(meta.get("filteredFindings", []) or []),
            "duplicateFindingsMerged": duplicate_findings_merged,
            "minConfidence": float(
                (meta.get("consensusMetrics") or {}).get("min_confidence", 0.65)
            ),
            "borderlineReverified": int(
                (meta.get("consensusMetrics") or {}).get("borderline_reverified", 0)
            ),
            "borderlineRecovered": int(
                (meta.get("consensusMetrics") or {}).get("borderline_recovered", 0)
            ),
            "patternRecallCandidates": int(
                (meta.get("consensusMetrics") or {}).get("pattern_recall_candidates", 0)
            ),
            "documentLintCandidates": int(
                (meta.get("consensusMetrics") or {}).get("document_lint_candidates", 0)
            ),
            "documentLintRetained": int(
                (meta.get("consensusMetrics") or {}).get("document_lint_retained", 0)
            ),
            "documentLintRuleCounts": dict(
                (meta.get("consensusMetrics") or {}).get("document_lint_rule_counts", {}) or {}
            ),
            "expertLensCandidates": int(meta.get("coverage_sweep_candidates", 0) or 0),
            "expertLensCounts": dict(meta.get("coverage_sweep_lenses", {}) or {}),
            "debatesTriggered": int(
                (meta.get("consensusMetrics") or {}).get("debates_triggered", 0)
            ),
            "hybridDebateAgents": int(
                (meta.get("consensusMetrics") or {}).get("hybrid_debate_agents", 0)
            ),
            "absenceClaimsCalibrated": int(
                (meta.get("consensusMetrics") or {}).get("absence_claims_calibrated", 0)
            ),
            "riskAdjustedThresholds": dict(
                (meta.get("consensusMetrics") or {}).get("risk_adjusted_thresholds", {}) or {}
            ),
            "issueCategoryCounts": dict(
                (meta.get("consensusMetrics") or {}).get("issue_category_counts", {}) or {}
            ),
            "filteredReasonCounts": dict(
                (meta.get("consensusMetrics") or {}).get("filtered_reason_counts", {}) or {}
            ),
            "suggestionsReplaced": int(
                (meta.get("consensusMetrics") or {}).get("suggestions_replaced", 0)
            ),
        },
        "dimensions": [
            {
                "dimensionId": str(result.get("dimensionId", "")),
                "label": _dimension_label_zh(str(result.get("dimensionId", ""))),
                "score": int(result.get("score", 0) or 0),
                "diagnosticOnly": bool(result.get("diagnostic_only")),
                "summary": str(result.get("summary", "")),
                "strengths": _as_text_items(result.get("strengths", []), limit=8),
                "weaknesses": _as_text_items(result.get("weaknesses", []), limit=8),
                "suggestions": _as_text_items(result.get("suggestions", []), limit=8),
                "generatedWeaknessCount": int(result.get("generatedWeaknessCount", 0) or 0),
                "generatedSuggestionCount": int(result.get("generatedSuggestionCount", 0) or 0),
                "verifiedWeaknessCount": int(result.get("verifiedWeaknessCount", len(result.get("weaknesses", []) or [])) or 0),
                "verifiedSuggestionCount": int(result.get("verifiedSuggestionCount", len(result.get("suggestions", []) or [])) or 0),
                "filteredLowConfidenceCount": int(result.get("filteredLowConfidenceCount", 0) or 0),
                "candidateWeaknesses": _as_text_items(result.get("candidateWeaknesses", []), limit=8),
                "candidateSuggestions": _as_text_items(result.get("candidateSuggestions", []), limit=8),
                "findings": list(result.get("verifiedFindingDetails", []) or []),
                "allFindings": list(result.get("allFindingDetails", []) or []),
                "filterReasons": dict(result.get("filterReasons", {}) or {}),
            }
            for result in results
        ],
    }


def _bind_result_findings(result: dict[str, Any]) -> dict[str, Any]:
    """Normalize legacy parallel arrays into issue/action/evidence records once."""
    existing = result.get("findings", []) or []
    if existing:
        normalized: list[dict[str, Any]] = []
        for finding in existing:
            if not isinstance(finding, dict):
                continue
            weakness = str(finding.get("weakness", "") or finding.get("text", "")).strip()
            if not weakness:
                continue
            normalized.append({
                "weakness": weakness,
                "suggestion": str(finding.get("suggestion", "")).strip(),
                "evidence": str(finding.get("evidence", "")).strip(),
                "severity": str(finding.get("severity", "")).strip(),
                "source_count": int(finding.get("source_count", 1) or 1),
                "source_dimensions": list(finding.get("source_dimensions", []) or []),
                "recall_lenses": list(finding.get("recall_lenses", []) or []),
            })
        if normalized:
            # Coverage-sweep findings are appended after the main reviewer. Some
            # model responses already contain structured findings, so merge the
            # sweep records explicitly instead of silently returning early.
            for coverage in (result.get("_coverage_sweep", []) or []):
                if not isinstance(coverage, dict):
                    continue
                weakness = str(coverage.get("weakness", "")).strip()
                if not weakness or any(
                    _text_overlap_ratio(weakness, item["weakness"]) >= 0.85
                    for item in normalized
                ):
                    continue
                lenses = list(coverage.get("recall_lenses", []) or [])
                normalized.append({
                    "weakness": weakness,
                    "suggestion": str(coverage.get("suggestion", "")).strip(),
                    "evidence": str(coverage.get("evidence", "")).strip(),
                    "severity": str(coverage.get("severity", "")).strip(),
                    "source_count": int(coverage.get("source_count", 1) or 1),
                    "source_dimensions": [
                        f"coverage_sweep:{lens}" for lens in lenses if lens
                    ],
                    "recall_lenses": lenses,
                })
            result["findings"] = normalized
            result["weaknesses"] = [item["weakness"] for item in normalized]
            result["suggestions"] = [
                item["suggestion"] for item in normalized if item["suggestion"]
            ]
            return result

    weaknesses = [
        str(item).strip() for item in (result.get("weaknesses", []) or [])
        if str(item).strip()
    ]
    suggestions = [
        str(item).strip() for item in (result.get("suggestions", []) or [])
        if str(item).strip()
    ]
    coverage_by_weakness = {
        str(item.get("weakness", "")).strip(): item
        for item in (result.get("_coverage_sweep", []) or [])
        if isinstance(item, dict)
    }
    findings: list[dict[str, Any]] = []
    for index, weakness in enumerate(weaknesses):
        coverage = coverage_by_weakness.get(weakness, {})
        suggestion = str(coverage.get("suggestion", "")).strip()
        if not suggestion and index < len(suggestions):
            suggestion = suggestions[index]
        findings.append({
            "weakness": weakness,
            "suggestion": suggestion,
            "evidence": str(coverage.get("evidence", "")).strip(),
            "severity": str(coverage.get("severity", "")).strip(),
            "source_count": int(coverage.get("source_count", 1) or 1),
            "source_dimensions": [
                f"coverage_sweep:{lens}"
                for lens in (coverage.get("recall_lenses", []) or [])
                if lens
            ],
            "recall_lenses": list(coverage.get("recall_lenses", []) or []),
        })
    result["findings"] = findings
    return result


def _retain_verified_findings(
    results: list[dict[str, Any]],
    findings: list[dict[str, Any]],
    filtered_findings: list[dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    """Remove low-confidence criticism while retaining normal dimension output."""
    by_dimension: dict[str, list[dict[str, Any]]] = {}
    for finding in findings:
        dimension = str(finding.get("dimension", ""))
        if dimension:
            by_dimension.setdefault(dimension, []).append(finding)
    filtered_by_dimension: dict[str, list[dict[str, Any]]] = {}
    for finding in filtered_findings or []:
        dimension = str(finding.get("dimension", ""))
        if dimension:
            filtered_by_dimension.setdefault(dimension, []).append(finding)

    for result in results:
        dimension = str(result.get("dimensionId", ""))
        original_weaknesses = list(result.get("weaknesses", []) or [])
        original_suggestions = list(result.get("suggestions", []) or [])
        relevant = by_dimension.get(dimension, [])
        for finding in relevant:
            if not str(finding.get("suggestion", "")).strip():
                finding["suggestion"] = _default_action_for_dimension(
                    dimension, str(finding.get("text", ""))
                )
        verified_weaknesses = [
            str(finding.get("text", "")) for finding in relevant
            if str(finding.get("text", "")).strip()
        ]
        verified_suggestions = [
            str(finding.get("suggestion", "")) for finding in relevant
            if str(finding.get("suggestion", "")).strip()
        ]
        result["generatedWeaknessCount"] = len(original_weaknesses)
        result["generatedSuggestionCount"] = len(original_suggestions)
        result["verifiedWeaknessCount"] = len(verified_weaknesses)
        result["verifiedSuggestionCount"] = len(verified_suggestions)
        result["filteredLowConfidenceCount"] = max(
            len(filtered_by_dimension.get(dimension, [])),
            max(0, len(original_weaknesses) - len(verified_weaknesses)),
        )
        # Keep the original candidates for diagnostics/UI empty-state messages,
        # but expose only evidence-gated findings in the primary fields.
        result["candidateWeaknesses"] = [str(item) for item in original_weaknesses if str(item).strip()]
        result["candidateSuggestions"] = [str(item) for item in original_suggestions if str(item).strip()]
        result["weaknesses"] = verified_weaknesses
        result["suggestions"] = verified_suggestions
        filtered = filtered_by_dimension.get(dimension, [])
        result["verifiedFindingDetails"] = relevant
        # Keep rejected candidates available for the UI confidence selector;
        # they stay explicitly marked as unverified and do not affect scores.
        result["allFindingDetails"] = relevant + filtered
        reason_counts: dict[str, int] = {}
        for item in filtered:
            reason = str(item.get("reason", "未通过证据精度门控"))
            reason_counts[reason] = reason_counts.get(reason, 0) + 1
        result["filterReasons"] = reason_counts
    return results


def _default_action_for_dimension(dimension: str, issue: str) -> str:
    """Provide a concrete one-to-one action when a reviewer omitted a suggestion."""
    actions = {
        "methodology": "建议补充可复现的方法定义、假设检验和适用边界，并说明每一步如何支撑该结论。",
        "experiment": "建议补充对应的 baseline、消融实验和多 seed 统计结果，并在表格中报告均值与置信区间。",
        "reproducibility": "建议公开数据处理、随机种子、超参数和运行命令，确保他人可以按同样配置复现。",
        "writing": "建议在对应章节直接改写该段，并补充必要的定义、交叉引用或证据定位。",
        "related_work": "建议补充与该结论直接相关的代表性工作，并明确比较差异和本文定位。",
        "novelty": "建议将该主张与最接近的已有方法逐项对比，明确新增假设、机制或实证结果。",
        "theory_depth": "建议补充形式化定义、复杂度/收敛性分析或反例，说明该结论成立的条件。",
    }
    return actions.get(dimension, "建议针对该问题补充证据、修改对应段落，并在实验或附录中验证修改有效性。")


# =============================================================================
# Main review orchestration
# =============================================================================

def run_review(
    file_base64: str,
    file_name: str,
    dimension_ids: list[str],
    model: str | None = None,
    progress_queue: Queue | None = None,
    vision_reader: bool = False,
    batch: bool = False,
    hybrid: bool = True,
    models: list[str] | None = None,
    venue: str = "",
    enable_debate: bool = True,
    max_debates: int = 5,
    min_finding_confidence: float = 0.65,
) -> tuple[list[dict[str, Any]], dict[str, Any], dict[str, Any] | None]:
    """Run a full multi-dimensional paper review.

    Args:
        venue: Conference venue (e.g. "ICLR", "NeurIPS") for standards calibration
    """
    requested_model = None if model in ("default", "") else model
    model = get_preferred_review_model_name()
    # AutoReview intentionally uses one Qwen model across visual extraction,
    # review agents, evidence verification, debate, and final synthesis.
    models = None

    meta: dict[str, Any] = {
        "review_model": model,
        "requested_model": requested_model,
        "model_policy": "qwen_only",
    }
    paper_text = _get_paper_text(file_base64, file_name)
    raw = b""
    try:
        raw = base64.b64decode(file_base64)
    except Exception:
        pass
    checkpoint_key = _review_checkpoint_key(
        raw=raw,
        file_name=file_name,
        dimension_ids=dimension_ids,
        model=model,
        venue=venue,
        vision_reader=vision_reader,
        enable_debate=enable_debate,
        max_debates=max_debates,
        min_finding_confidence=min_finding_confidence,
    )
    checkpoint = _load_review_checkpoint(checkpoint_key)
    checkpoint_hits: list[str] = []
    meta["checkpoint"] = {
        "enabled": os.environ.get("AUTO_REVIEW_DISABLE_CHECKPOINT", "0") != "1",
        "version": REVIEW_CHECKPOINT_VERSION,
        "key": checkpoint_key,
        "hits": checkpoint_hits,
    }

    target_model = model or get_primary_model_name()
    is_review_vision = any(vm in target_model.lower() for vm in VISION_MODELS)

    vision_extracted: str | None = None
    vision_token_info: dict[str, Any] = {}
    should_use_vision = vision_reader and bool(raw)

    if should_use_vision:
        vision_extracted, vision_token_info = _vision_extract_paper(raw, file_name)

    if vision_extracted:
        visual_header = (
            VISUAL_EVIDENCE_MARKER
            + "The following evidence was extracted from every detected figure/table page. "
            "Use it to inspect figures, tables, formulas, and layout. Page numbers "
            "refer to the original PDF. When text and visual evidence conflict, "
            "report the conflict instead of guessing.\n\n"
        )
        review_text = paper_text + visual_header + vision_extracted if paper_text else vision_extracted
    else:
        review_text = paper_text

    quality = _check_extraction_quality(review_text, raw)
    meta["text_quality"] = quality

    reference_context = _fetch_reference_context(paper_text)
    meta["references_found"] = bool(reference_context)
    meta["references_count"] = len(_extract_references(paper_text))

    page_images = _get_paper_images(file_base64, file_name) if (is_review_vision and not vision_extracted) else []

    # Stage 0.5: Build a deterministic evidence substrate shared by all agents.
    evidence_map = build_evidence_map(paper_text, vision_extracted or "")
    evidence_map_prompt = evidence_map.to_prompt(max_chars=16000)
    meta["evidence_map"] = evidence_map.stats

    stages = checkpoint.setdefault("stages", {})
    cached_context = stages.get("analysis_context")
    if isinstance(cached_context, dict):
        extracted_facts = cached_context.get("extracted_facts") or {}
        skeptic_questions = list(cached_context.get("skeptic_questions") or [])
        checkpoint_hits.append("analysis_context")
    else:
        # Stage 1: Extract structured facts for evidence grounding.
        extracted_facts = _extract_paper_facts(review_text, model=model)
        facts_for_questions = dict(extracted_facts or {})
        facts_for_questions["evidence_map"] = evidence_map_prompt
        # Stage 1.5: Generate skeptic probing questions (assumption checking)
        skeptic_questions = _generate_skeptic_questions(
            review_text, facts_for_questions, model=model,
        )
        _checkpoint_stage(
            checkpoint_key,
            checkpoint,
            "analysis_context",
            {
                "extracted_facts": extracted_facts,
                "skeptic_questions": skeptic_questions,
            },
        )

    meta["facts_extracted"] = bool(extracted_facts)
    meta["facts_count"] = len(extracted_facts.get("key_results", [])) if extracted_facts else 0
    facts = dict(extracted_facts or {})
    facts["evidence_map"] = evidence_map_prompt

    meta["skeptic_questions_generated"] = len(skeptic_questions)
    if skeptic_questions:
        meta["skeptic_questions"] = skeptic_questions

    if not review_text.strip():
        _empty_avail = get_dimensions_for_venue(venue)
        empty_results = [
            {
                "dimensionId": d["id"],
                "score": 0,
                "summary": "Could not extract text from the uploaded file." if not vision_extracted
                           else "Vision extraction returned empty.",
                "strengths": [],
                "weaknesses": ["File appears to be empty or unreadable"],
                "suggestions": ["Upload a clear PDF with selectable text"],
            }
            for d in _empty_avail
            if not dimension_ids or d["id"] in dimension_ids
        ]
        if progress_queue:
            for r in empty_results:
                progress_queue.put({
                    "type": "progress",
                    "dimensionId": r["dimensionId"],
                    "result": r,
                    "completed": 0,
                    "total": len(empty_results),
                })
        return empty_results, meta, None

    _avail_dims = get_dimensions_for_venue(venue)
    dims_to_run = [
        d for d in _avail_dims
        if not dimension_ids or d["id"] in dimension_ids
    ]

    # Multi-step: split skeptic out of the batch/hybrid call to avoid prompt bloat.
    # Skeptic runs as a separate stage with full context of other dimensions' results.
    has_skeptic = any(d["id"] == "skeptic" for d in dims_to_run)
    skeptic_dim = next((d for d in dims_to_run if d["id"] == "skeptic"), None)
    main_dims = [d for d in dims_to_run if d["id"] != "skeptic"] if has_skeptic and (hybrid or batch) else dims_to_run

    try:
        language_client = get_client_for_model(model or get_primary_model_name())
    except Exception:
        language_client = None

    cached_prepared = stages.get("prepared_results")
    if isinstance(cached_prepared, dict) and isinstance(cached_prepared.get("results"), list):
        results = json.loads(json.dumps(cached_prepared["results"], ensure_ascii=False))
        meta["coverage_sweep_candidates"] = int(
            cached_prepared.get("coverage_sweep_candidates", 0)
        )
        meta["coverage_sweep_lenses"] = dict(
            cached_prepared.get("coverage_sweep_lenses", {}) or {}
        )
        meta["grounding_issues"] = int(cached_prepared.get("grounding_issues", 0))
        checkpoint_hits.append("prepared_results")
    else:
        cached_pre_sweep = stages.get("pre_sweep_results")
        if isinstance(cached_pre_sweep, dict) and isinstance(cached_pre_sweep.get("results"), list):
            results = json.loads(json.dumps(cached_pre_sweep["results"], ensure_ascii=False))
            checkpoint_hits.append("pre_sweep_results")
        else:
            if models and len(models) > 1:
                results = _run_review_multi_model(
                    review_text, dims_to_run, models, reference_context, facts=facts,
                )
            elif hybrid:
                client = get_client_for_model(model or get_primary_model_name())
                results = _run_review_hybrid(
                    client, review_text, main_dims, model, reference_context,
                    facts=facts, skeptic_questions=None, venue=venue,
                    page_images=page_images if page_images else None,
                )
            elif batch:
                client = get_client_for_model(model or get_primary_model_name())
                results = _run_review_batch(
                    client, review_text, main_dims, model, reference_context,
                    facts=facts, skeptic_questions=None, venue=venue,
                    page_images=page_images if page_images else None,
                )
            else:
                client = get_client_for_model(model)
                results = _run_review_parallel(
                    client, review_text, dims_to_run, model, page_images,
                    progress_queue, facts=facts,
                )

            # Stage 2: Run skeptic separately with context from other dimensions.
            if has_skeptic and skeptic_dim and len(main_dims) < len(dims_to_run):
                skeptic_result = _run_review_skeptic(
                    review_text, skeptic_dim, results, skeptic_questions, facts=facts,
                    venue=venue, model=model,
                )
                results.append(skeptic_result)

            if os.environ.get("AUTO_REVIEW_SKIP_DEEP_DIVE", "0") != "1":
                deep_dive_result = _run_review_deep_dive(
                    review_text, results, facts=facts, venue=venue, model=model,
                )
                if deep_dive_result.get("weaknesses") or deep_dive_result.get("suggestions"):
                    results.append(deep_dive_result)

            if os.environ.get("AUTO_REVIEW_SKIP_PATCH", "0") != "1":
                patch_result = _run_review_patch(
                    review_text, results, facts=facts, venue=venue, model=model,
                )
                if patch_result.get("weaknesses") or patch_result.get("suggestions"):
                    results.append(patch_result)

            _checkpoint_stage(
                checkpoint_key,
                checkpoint,
                "pre_sweep_results",
                {"results": results},
            )

        if os.environ.get("AUTO_REVIEW_SKIP_COVERAGE_SWEEP", "0") == "1":
            coverage_sweep = []
        else:
            coverage_sweep = _run_expert_coverage_sweep(
                review_text, results, facts=facts, model=model,
            )
        meta["coverage_sweep_candidates"] = len(coverage_sweep)
        meta["coverage_sweep_lenses"] = {
            str(lens["id"]): sum(
                str(lens["id"]) in (item.get("recall_lenses", []) or [])
                for item in coverage_sweep
            )
            for lens in _COVERAGE_SWEEP_LENSES
        }
        results = _merge_coverage_sweep(results, coverage_sweep)
        results = _normalize_outward_chinese(results, language_client, model)
        results = [_bind_result_findings(result) for result in results]
        results = _dedup_cross_dimensions(results)
        results = [_check_review_consistency(r) for r in results]

        if facts:
            results = [_verify_fact_grounding(r, facts) for r in results]
            meta["grounding_issues"] = sum(
                len(r.get("_grounding_issues", [])) for r in results
            )
        results = [_bind_result_findings(result) for result in results]
        _checkpoint_stage(
            checkpoint_key,
            checkpoint,
            "prepared_results",
            {
                "results": results,
                "coverage_sweep_candidates": meta.get("coverage_sweep_candidates", 0),
                "coverage_sweep_lenses": meta.get("coverage_sweep_lenses", {}),
                "grounding_issues": meta.get("grounding_issues", 0),
            },
        )

    # Normalize cached and fresh dimension outputs to the same bound finding schema.
    results = [_bind_result_findings(result) for result in results]

    # Stage 4: Evidence verifier -> targeted debate -> confidence filtering.
    try:
        consensus_client = get_client_for_model(model or get_primary_model_name())
    except Exception:
        consensus_client = None
    cached_consensus = stages.get("consensus_output")
    if isinstance(cached_consensus, dict):
        consensus_output = cached_consensus
        checkpoint_hits.append("consensus_output")
    else:
        consensus_output = run_consensus_pipeline(
            results=results,
            evidence_map=evidence_map,
            client=consensus_client,
            model=model,
            target_paper_text=paper_text,
            enable_debate=enable_debate,
            max_debates=max_debates,
            min_confidence=min_finding_confidence,
        )
        _checkpoint_stage(
            checkpoint_key,
            checkpoint,
            "consensus_output",
            consensus_output,
        )
    meta["verifiedFindings"] = _normalize_outward_chinese(
        consensus_output.get("verifiedFindings", []),
        consensus_client,
        model,
    )
    meta["filteredFindings"] = list(consensus_output.get("filteredFindings", []) or [])
    meta["claimEvidenceMatrix"] = dict(
        consensus_output.get("claimEvidenceMatrix", {}) or {}
    )
    for finding in meta["verifiedFindings"]:
        finding["text"] = _compact_issue_text(str(finding.get("text", "")))
    meta["consensusMetrics"] = consensus_output.get("metrics", {})
    results = _retain_verified_findings(
        results,
        meta["verifiedFindings"],
        meta["filteredFindings"],
    )

    # Aggregate real token usage from all calls
    total_prompt = 0
    total_completion = 0
    for r in results:
        tu = r.get("_token_usage") or {}
        total_prompt += tu.get("prompt", 0)
        total_completion += tu.get("completion", 0)
        r.pop("_token_usage", None)  # clean up internal field
    meta["token_usage"] = {
        "prompt_tokens": total_prompt,
        "completion_tokens": total_completion,
        "total_tokens": total_prompt + total_completion,
    }

    # Generate overall executive summary
    summary_model = model or get_primary_model_name()
    cached_summary = stages.get("overall_summary")
    if isinstance(cached_summary, dict):
        overall_summary = cached_summary
        checkpoint_hits.append("overall_summary")
    else:
        overall_summary = _generate_overall_summary(
            results, model=summary_model, venue=venue,
        )
        overall_summary = _normalize_outward_chinese(
            overall_summary, language_client, summary_model,
        )
        if isinstance(overall_summary, dict):
            _checkpoint_stage(
                checkpoint_key,
                checkpoint,
                "overall_summary",
                overall_summary,
            )

    # Include summary token usage if available
    if overall_summary and overall_summary.get("_token_usage"):
        st = overall_summary["_token_usage"]
        meta["token_usage"]["summary_prompt_tokens"] = st.get("prompt", 0)
        meta["token_usage"]["summary_completion_tokens"] = st.get("completion", 0)
        meta["token_usage"]["total_tokens"] += st.get("prompt", 0) + st.get("completion", 0)
        overall_summary.pop("_token_usage", None)

    is_group_or_batch = hybrid or batch
    if progress_queue and results and is_group_or_batch:
        total = len(results)
        for i, r in enumerate(results):
            progress_queue.put({
                "type": "progress",
                "dimensionId": r["dimensionId"],
                "result": r,
                "completed": i + 1,
                "total": total,
            })
        if overall_summary:
            progress_queue.put({
                "type": "summary",
                "summary": overall_summary,
            })

    text_path_len = len(paper_text) if paper_text else 0
    vision_extract_len = len(vision_extracted) if vision_extracted else 0
    vision_pages = vision_token_info.get("vision_pages_sent", 0)
    vision_images = vision_token_info.get("vision_images", vision_pages)
    review_mode = ("multi_model" if (models and len(models) > 1)
                   else "hybrid" if hybrid else "batch" if batch else "parallel")

    meta["token_estimate"] = _estimate_token_usage(
        text_path_len=text_path_len,
        vision_path_pages=vision_images,
        vision_path_extraction_len=vision_extract_len,
        num_dims=len(dims_to_run),
        review_mode=review_mode,
    )
    meta["vision_extracted"] = bool(vision_extracted)
    meta["vision_pages"] = vision_pages
    meta["vision_images"] = vision_images
    meta["vision_selected_pages"] = vision_token_info.get("vision_selected_pages", [])
    meta["vision_coverage_mode"] = vision_token_info.get("vision_coverage_mode", "all_figures_tables")
    meta["vision_detected_regions"] = vision_token_info.get("vision_detected_regions", vision_images)
    meta["vision_expected_pages"] = vision_token_info.get("vision_expected_pages", vision_pages)
    meta["vision_failed_pages"] = vision_token_info.get("vision_failed_pages", [])
    meta["vision_coverage_complete"] = vision_token_info.get("vision_coverage_complete", bool(vision_extracted))
    meta["visual_evidence_chars"] = vision_token_info.get("vision_extraction_chars", 0)
    meta["review_mode"] = review_mode
    meta["multi_model"] = bool(models and len(models) > 1)
    meta["checkpoint"]["resumed"] = bool(checkpoint_hits)
    meta["checkpoint"]["available_stages"] = sorted(
        checkpoint.get("stages", {}).keys()
    )

    # Consolidated findings come from every issue that passes the confidence gate.
    verified_findings = meta.get("verifiedFindings", [])
    if verified_findings:
        key_findings = {
            "weaknesses": [
                {
                    "text": issue.get("text", ""),
                    "severity": issue.get("severity", "major"),
                    "dimensionId": issue.get("dimension", ""),
                    "evidence": issue.get("evidence", ""),
                    "confidence": issue.get("evidence_confidence", 0.0),
                    "priorityScore": issue.get("priority_score", 0.0),
                }
                for issue in verified_findings[:5]
            ],
            "suggestions": [
                {
                    "text": issue.get("suggestion", ""),
                    "dimensionId": issue.get("dimension", ""),
                    "priorityScore": issue.get("priority_score", 0.0),
                }
                for issue in verified_findings
                if issue.get("suggestion")
            ][:5],
        }
    else:
        key_findings = _consolidate_key_findings(results)
    meta["keyFindings"] = key_findings
    meta["categorizedFindings"] = _build_categorized_findings(results, verified_findings, meta)
    meta["confidenceSummary"] = _build_confidence_summary(
        verified_findings, meta.get("consensusMetrics", {}), meta,
    )
    _scored_results = [r for r in results if not r.get("diagnostic_only")]
    overall_score = (
        sum(r["score"] for r in _scored_results) // len(_scored_results)
        if _scored_results else 0
    )
    meta["reportSummary"] = _build_report_summary(
        results, meta, overall_summary if isinstance(overall_summary, dict) else None,
        overall_score,
    )

    return results, meta, overall_summary


def run_review_streaming(
    file_base64: str,
    file_name: str,
    dimension_ids: list[str],
    model: str | None = None,
    vision_reader: bool = False,
    batch: bool = False,
    hybrid: bool = True,
    models: list[str] | None = None,
    venue: str = "",
    enable_debate: bool = True,
    max_debates: int = 5,
    min_finding_confidence: float = 0.65,
) -> Queue:
    """Run review with results streamed via a Queue."""
    q: Queue = Queue()
    avail_dims = get_dimensions_for_venue(venue)
    dims_to_run = [
        d for d in avail_dims
        if not dimension_ids or d["id"] in dimension_ids
    ]
    q.put({"type": "started", "total": len(dims_to_run)})

    def _run():
        try:
            results, meta, overall_summary = run_review(
                file_base64, file_name, dimension_ids, model,
                progress_queue=q, vision_reader=vision_reader,
                batch=batch, hybrid=hybrid, models=models, venue=venue,
                enable_debate=enable_debate, max_debates=max_debates,
                min_finding_confidence=min_finding_confidence,
            )
            overall = sum(r["score"] for r in results) // len(results) if results else 0
            ev: dict[str, Any] = {
                "type": "complete",
                "overallScore": overall,
                "dimensionCount": len(results),
                "results": results,
                "meta": meta,
                "keyFindings": meta.get("keyFindings", {"weaknesses": [], "suggestions": []}),
                "verifiedFindings": meta.get("verifiedFindings", []),
                "consensusMetrics": meta.get("consensusMetrics", {}),
                "categorizedFindings": meta.get("categorizedFindings", []),
                "confidenceSummary": meta.get("confidenceSummary", {}),
                "reportSummary": meta.get("reportSummary", {}),
            }
            if overall_summary:
                ev["overallSummary"] = overall_summary
            q.put(ev)
        except Exception as exc:
            q.put({"type": "error", "error": str(exc)})

    Thread(target=_run, daemon=True).start()
    return q


# =============================================================================
# Review history persistence
# =============================================================================

import math
import re as _re
from collections import Counter as _Counter

_SEARCH_INDEX: dict[str, Any] | None = None


def _tokenize(text: str) -> list[str]:
    tokens = _re.findall(r"[a-z0-9]+(?:'[a-z]+)?", text.lower())
    stopwords = {
        "the", "a", "an", "is", "are", "was", "were", "be", "been", "being",
        "have", "has", "had", "do", "does", "did", "will", "would", "could",
        "should", "may", "might", "shall", "can", "need", "dare", "ought",
        "used", "to", "of", "in", "for", "on", "with", "at", "by", "from",
        "as", "into", "through", "during", "before", "after", "above", "below",
        "between", "out", "off", "over", "under", "again", "further", "then",
        "once", "here", "there", "when", "where", "why", "how", "all", "each",
        "every", "both", "few", "more", "most", "other", "some", "such", "no",
        "nor", "not", "only", "own", "same", "so", "than", "too", "very",
        "just", "because", "but", "and", "or", "if", "while", "although",
        "this", "that", "these", "those", "it", "its", "also", "well", "very",
        "much", "many", "about", "which", "what", "who", "whom",
    }
    return [t for t in tokens if len(t) > 2 and t not in stopwords]


def _build_search_index() -> dict[str, Any]:
    """Build a lightweight TF-IDF search index over all review_history files."""
    global _SEARCH_INDEX
    if _SEARCH_INDEX is not None:
        return _SEARCH_INDEX

    _ensure_history_dir()
    docs: list[dict[str, Any]] = []
    doc_texts: list[str] = []

    for fpath in sorted(HISTORY_DIR.glob("*.json"), reverse=True):
        try:
            with open(fpath, encoding="utf-8") as f:
                data = json.load(f)
        except Exception:
            continue
        # Build searchable text from all fields
        parts = [data.get("fileName", "")]
        for r in (data.get("results") or []):
            parts.append(r.get("summary", ""))
            parts.extend(r.get("strengths") or [])
            parts.extend(r.get("weaknesses") or [])
            parts.extend(r.get("suggestions") or [])
        text = " ".join(p for p in parts if p)
        docs.append({"id": data.get("id", fpath.stem), "data": data, "text": text})
        doc_texts.append(text)

    if not docs:
        _SEARCH_INDEX = {"docs": [], "idf": {}, "tf_vectors": []}
        return _SEARCH_INDEX

    # Build TF-IDF vectors
    tokenized = [_tokenize(t) for t in doc_texts]
    n_docs = len(tokenized)
    doc_freq: _Counter[str] = _Counter()
    for tokens in tokenized:
        for t in set(tokens):
            doc_freq[t] += 1

    idf: dict[str, float] = {}
    for term, freq in doc_freq.items():
        idf[term] = math.log((n_docs + 1) / (freq + 1)) + 1.0

    # Compute TF vectors
    tf_vectors: list[dict[str, float]] = []
    for tokens in tokenized:
        if not tokens:
            tf_vectors.append({})
            continue
        tf: dict[str, float] = {}
        for t in tokens:
            tf[t] = 1.0 + math.log(tokens.count(t))
        tf_vectors.append(tf)

    _SEARCH_INDEX = {"docs": docs, "idf": idf, "tf_vectors": tf_vectors}
    return _SEARCH_INDEX


def _cosine_sim(v1: dict[str, float], v2: dict[str, float]) -> float:
    dot = 0.0
    for k, v in v1.items():
        if k in v2:
            dot += v * v2[k]
    return dot


def _normalize(v: dict[str, float]) -> dict[str, float]:
    norm = math.sqrt(sum(x * x for x in v.values()))
    if norm == 0:
        return v
    return {k: x / norm for k, x in v.items()}

def _ensure_history_dir() -> Path:
    HISTORY_DIR.mkdir(parents=True, exist_ok=True)
    return HISTORY_DIR


def _review_history_path(review_id: str) -> Path:
    return _ensure_history_dir() / f"{review_id}.json"


def save_review(
    file_name: str, model: str | None, dimensions: list[str],
    results: list[dict[str, Any]], meta: dict[str, Any] | None = None,
    overall_summary: dict[str, Any] | None = None,
) -> str:
    """Save review results and return the review ID."""
    review_id = (
        datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
        + "_" + uuid.uuid4().hex[:8]
    )
    overall = sum(r["score"] for r in results) // len(results) if results else 0
    record = {
        "id": review_id,
        "fileName": file_name,
        "model": model or get_primary_model_name(),
        "dimensions": dimensions,
        "overallScore": overall,
        "dimensionCount": len(results),
        "results": results,
        "meta": meta or {},
        "overallSummary": overall_summary,
        "reportSummary": (meta or {}).get("reportSummary", {}),
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }
    path = _review_history_path(review_id)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(record, f, ensure_ascii=False, indent=2)
    return review_id


def load_review_history() -> list[dict[str, Any]]:
    """Load all saved review history records (summary only)."""
    _ensure_history_dir()
    records = []
    for fpath in sorted(HISTORY_DIR.glob("*.json"), reverse=True):
        try:
            with open(fpath, encoding="utf-8") as f:
                data = json.load(f)
            records.append({
                "id": data.get("id", fpath.stem),
                "fileName": data.get("fileName", "Unknown"),
                "model": data.get("model", ""),
                "overallScore": data.get("overallScore", 0),
                "dimensionCount": data.get("dimensionCount", 0),
                "timestamp": data.get("timestamp", ""),
            })
        except Exception:
            pass
    return records


def search_review_history(
    query: str = "",
    model_filter: str = "",
    min_score: int = 0,
    max_score: int = 100,
    limit: int = 50,
) -> list[dict[str, Any]]:
    """Search review history with full-text semantic search (TF-IDF).

    Args:
        query: Free-text search (searches file names, dimension summaries,
               strengths, weaknesses, suggestions via TF-IDF cosine similarity).
        model_filter: Filter by model name (substring match).
        min_score: Minimum overall score.
        max_score: Maximum overall score.
        limit: Maximum records to return.

    Returns:
        List of matching review summaries, ranked by relevance when query is provided.
    """
    idx = _build_search_index()
    docs = idx["docs"]
    if not docs:
        return []

    # Apply model + score filters first (fast)
    filtered = []
    for entry in docs:
        d = entry["data"]
        model_val = d.get("model", "")
        score = d.get("overallScore", 0)

        if model_filter and model_filter.lower() not in model_val.lower():
            continue
        if score < min_score or score > max_score:
            continue
        filtered.append(entry)

    if not filtered:
        return []

    # If no query text, return chronologically sorted
    if not query or not query.strip():
        results = []
        for entry in filtered:
            d = entry["data"]
            results.append({
                "id": d.get("id", ""),
                "fileName": d.get("fileName", "Unknown"),
                "model": d.get("model", ""),
                "overallScore": d.get("overallScore", 0),
                "dimensionCount": d.get("dimensionCount", 0),
                "timestamp": d.get("timestamp", ""),
            })
        return results[:limit]

    # TF-IDF ranking
    query_tokens = _tokenize(query)
    if not query_tokens:
        return filtered[:limit]

    q_tf: dict[str, float] = {}
    for t in query_tokens:
        q_tf[t] = 1.0 + math.log(query_tokens.count(t))

    idf = idx["idf"]
    q_vec = _normalize({t: v * idf.get(t, 1.0) for t, v in q_tf.items()})

    # Map filtered entries back to index positions
    scored: list[tuple[float, dict[str, Any]]] = []
    for entry in filtered:
        doc_idx = docs.index(entry)  # linear scan — fine for small scale
        tfv = idx["tf_vectors"][doc_idx]
        doc_vec = _normalize({t: v * idf.get(t, 1.0) for t, v in tfv.items()})
        sim = _cosine_sim(q_vec, doc_vec)
        scored.append((sim, entry))

    scored.sort(key=lambda x: x[0], reverse=True)

    results = []
    for sim, entry in scored[:limit]:
        d = entry["data"]
        results.append({
            "id": d.get("id", ""),
            "fileName": d.get("fileName", "Unknown"),
            "model": d.get("model", ""),
            "overallScore": d.get("overallScore", 0),
            "dimensionCount": d.get("dimensionCount", 0),
            "timestamp": d.get("timestamp", ""),
            "_relevance": round(sim, 4),
        })

    return results


def load_review_by_id(review_id: str) -> dict[str, Any] | None:
    """Load a specific review record by ID."""
    path = _review_history_path(review_id)
    if not path.exists():
        return None
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return None


def delete_review(review_id: str) -> bool:
    """Delete a review record by ID."""
    path = _review_history_path(review_id)
    if path.exists():
        path.unlink()
        return True
    return False


def load_leaderboard(
    sort_by: str = "score",
    limit: int = 20,
) -> list[dict[str, Any]]:
    """Load and rank reviews by score for comparison."""
    records = load_review_history()
    if sort_by == "score":
        records.sort(key=lambda r: r.get("overallScore", 0), reverse=True)
    elif sort_by == "date":
        records.sort(key=lambda r: r.get("timestamp", ""), reverse=True)
    return records[:limit]
