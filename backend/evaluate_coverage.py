"""Evaluate auto-review coverage against human reviews from OpenReview.

Pipeline:
  1. Fetch papers with human reviews from OpenReview (ICLR 2023)
  2. Download PDFs and run auto-review on each paper (with caching)
  3. Parse human review text into structured points (with LLM)
  4. Qwen-as-judge: compare auto-review vs human-review coverage
  5. Generate report (JSON + markdown)

Usage:
    python evaluate_coverage.py [--papers N] [--max-papers N] [--skip-review] [--skip-parse] [--force]

    --papers N       Process N papers (default: 5)
    --max-papers N   Maximum papers to fetch from OpenReview (default: 100)
    --skip-review    Skip auto-review step (use cached results)
    --skip-parse     Skip human review parsing step
    --force          Re-run all steps, ignoring cache
    --report-only    Only regenerate report from cached data
"""

from __future__ import annotations

import base64
import json
import os
import re
import sys
import time
import urllib.request
import urllib.error
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------

BACKEND_DIR = Path(__file__).resolve().parent
ROOT_DIR = BACKEND_DIR.parent
REVIEW_DATA_DIR = BACKEND_DIR / "review_data"
CACHE_DIR = BACKEND_DIR / "eval_cache"
PAPER_CACHE_DIR = CACHE_DIR / "papers"
REVIEW_CACHE_DIR = CACHE_DIR / "auto_reviews"
PARSE_CACHE_DIR = CACHE_DIR / "parsed_human"
COVERAGE_CACHE_DIR = CACHE_DIR / "coverage"

for d in [PAPER_CACHE_DIR, REVIEW_CACHE_DIR, PARSE_CACHE_DIR, COVERAGE_CACHE_DIR]:
    d.mkdir(parents=True, exist_ok=True)

# Ensure Python path includes required modules
sys.path.insert(0, str(BACKEND_DIR / "review_data"))
sys.path.insert(0, str(BACKEND_DIR))
sys.path.insert(0, str(BACKEND_DIR / "agent"))

OPENREVIEW_BASE = "https://api.openreview.net"
PIPELINE_VERSION = "qwen-vision-v7.2-hybrid-agents"

# ===========================================================================
# Stage 1: Fetch papers + reviews from OpenReview
# ===========================================================================


def _or_request(url: str) -> dict[str, Any] | list[Any]:
    req = urllib.request.Request(url, headers={"User-Agent": "ClawAI-Eval/1.0"})
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read().decode("utf-8"))


def fetch_papers_with_reviews(
    venue_id: str = "ICLR.cc/2023/Conference",
    max_papers: int = 100,
    min_reviews: int = 2,
    force: bool = False,
) -> list[dict[str, Any]]:
    """Fetch papers from OpenReview, filter to those with ≥min_reviews reviews."""
    cache_path = PAPER_CACHE_DIR / "papers_index.json"
    if cache_path.exists() and not force:
        with open(cache_path, encoding="utf-8") as f:
            return json.load(f)

    # Fetch all paper submissions
    all_papers: list[dict[str, Any]] = []
    offset = 0
    while offset < max_papers:
        url = f"{OPENREVIEW_BASE}/notes?invitation={venue_id}/-/Blind_Submission&limit=100&offset={offset}"
        data = _or_request(url)
        notes = data.get("notes", []) if isinstance(data, dict) else []
        if not notes:
            break
        all_papers.extend(notes)
        offset += len(notes)

    print(f"[fetch] Got {len(all_papers)} papers total")

    # For each paper, fetch reviews
    result_papers: list[dict[str, Any]] = []
    for i, paper in enumerate(all_papers):
        content = paper.get("content", {})
        forum = paper.get("forum", "")
        if not forum:
            continue

        title = str(content.get("title", "Untitled"))[:80]
        print(f"[fetch  {i+1}/{len(all_papers)}] {title}...", end=" ")

        # Check PDF
        pdf_path = content.get("pdf", "")
        if not pdf_path:
            print("no PDF")
            continue

        # Get all forum notes
        try:
            url = f"{OPENREVIEW_BASE}/notes?forum={forum}"
            forum_notes = _or_request(url)
            notes = forum_notes.get("notes", []) if isinstance(forum_notes, dict) else []
        except Exception as e:
            print(f"API error: {e}")
            time.sleep(2)
            continue

        # Filter to official reviews
        review_inv_pattern = re.compile(rf"{re.escape(venue_id)}/Paper\d+/-/Official_Review")
        reviews = [n for n in notes if review_inv_pattern.search(n.get("invitation", ""))]

        if len(reviews) < min_reviews:
            print(f"only {len(reviews)} reviews")
            continue

        print(f"{len(reviews)} reviews ✓")

        # Extract review data
        review_list = []
        for rn in reviews[:5]:  # max 5 reviews per paper
            rc = rn.get("content", {})
            review_list.append({
                "note_id": rn.get("id", ""),
                "strength_and_weaknesses": rc.get("strength_and_weaknesses", ""),
                "summary_of_review": rc.get("summary_of_the_review", ""),
                "summary_of_paper": rc.get("summary_of_the_paper", ""),
                "recommendation": rc.get("recommendation", ""),
                "confidence": rc.get("confidence", ""),
                "correctness": rc.get("correctness", ""),
                "technical_novelty": rc.get("technical_novelty_and_significance", ""),
                "empirical_novelty": rc.get("empirical_novelty_and_significance", ""),
                "clarity": rc.get("clarity,_quality,_novelty_and_reproducibility", ""),
            })

        result_papers.append({
            "forum": forum,
            "title": str(content.get("title", "")),
            "pdf_path": pdf_path,
            "keywords": content.get("keywords", []),
            "abstract": content.get("abstract", ""),
            "reviews": review_list,
            "venue": venue_id,
        })

        # Don't hit the API too hard
        time.sleep(0.5)

    print(f"\n[fetch] Papers with ≥{min_reviews} reviews: {len(result_papers)}")

    with open(cache_path, "w", encoding="utf-8") as f:
        json.dump(result_papers, f, ensure_ascii=False, indent=1)

    return result_papers


# ===========================================================================
# Stage 2: Download PDF & run auto-review
# ===========================================================================


def _download_pdf(paper: dict[str, Any]) -> bytes:
    """Download PDF from OpenReview, or read from local static cache if available."""
    # Check static cache first
    local_path = paper.get("_pdf_local", "")
    if local_path and Path(local_path).exists():
        return Path(local_path).read_bytes()
    # Static cache by forum id
    static_pdf = CACHE_DIR / "static" / "pdfs" / f"{paper.get('forum', '')}.pdf"
    if static_pdf.exists():
        return static_pdf.read_bytes()
    # Fallback to OpenReview
    pdf_path = paper["pdf_path"]
    url = f"{OPENREVIEW_BASE}{pdf_path}"
    req = urllib.request.Request(url, headers={"User-Agent": "ClawAI-Eval/1.0"})
    with urllib.request.urlopen(req, timeout=60) as resp:
        return resp.read()


def run_auto_reviews(
    papers: list[dict[str, Any]],
    force: bool = False,
    max_papers: int | None = None,
    model: str | None = None,
) -> list[dict[str, Any]]:
    """Run auto-review on each paper, with caching."""
    papers = papers[:max_papers] if max_papers else papers
    results = []

    # Match the web service: every review stage uses the preferred Qwen model.
    from review_engine.llm_client import get_preferred_review_model_name, register_config_section
    register_config_section("llm")
    model = get_preferred_review_model_name()

    for i, paper in enumerate(papers):
        forum = paper["forum"]
        title = paper["title"][:60]
        cache_file = REVIEW_CACHE_DIR / f"{forum}-{PIPELINE_VERSION}.json"

        if cache_file.exists() and not force:
            print(f"[review {i+1}/{len(papers)}] {title} (cached)")
            with open(cache_file, encoding="utf-8") as f:
                data = json.load(f)
            results.append(data)
            continue

        print(f"[review {i+1}/{len(papers)}] {title}...", end=" ", flush=True)

        # Download PDF
        try:
            pdf_bytes = _download_pdf(paper)
        except Exception as e:
            print(f"PDF download failed: {e}")
            continue

        file_base64 = base64.b64encode(pdf_bytes).decode("ascii")
        file_name = f"{forum[:12]}.pdf"

        # Run auto-review
        try:
            from review_engine.reviewer import run_review

            dim_ids = [
                "methodology", "novelty", "experiment", "writing",
                "related_work", "reproducibility", "ethics",
                "skeptic",
            ]

            dim_results, meta, overall_summary = run_review(
                file_base64=file_base64,
                file_name=file_name,
                dimension_ids=dim_ids,
                model=model,
                vision_reader=True,
                batch=False,
                hybrid=True,
                venue="ICLR",
                enable_debate=True,
                max_debates=2,
            )

            result = {
                "forum": forum,
                "title": paper["title"],
                "dim_results": dim_results,
                "meta": meta,
                "overall_summary": overall_summary,
                "verifiedFindings": meta.get("verifiedFindings", []),
                "consensusMetrics": meta.get("consensusMetrics", {}),
                "categorizedFindings": meta.get("categorizedFindings", []),
                "confidenceSummary": meta.get("confidenceSummary", {}),
                "keyFindings": meta.get("keyFindings", {}),
                "pipeline_version": PIPELINE_VERSION,
                "overall_score": (
                    sum(r["score"] for r in dim_results) // len(dim_results)
                    if dim_results else 0
                ),
            }

            with open(cache_file, "w", encoding="utf-8") as f:
                json.dump(result, f, ensure_ascii=False, indent=1)

            results.append(result)
            print(f"done (score={result['overall_score']})")

        except Exception as e:
            print(f"FAILED: {e}")
            # Save error so we don't retry by default
            error_result = {
                "forum": forum,
                "title": paper["title"],
                "error": str(e),
            }
            with open(cache_file, "w", encoding="utf-8") as f:
                json.dump(error_result, f, ensure_ascii=False, indent=1)

    return results


# ===========================================================================
# Stage 3: Parse human reviews into structured points
# ===========================================================================

HUMAN_PARSE_PROMPT = """You are analyzing a peer review written by a human expert for an academic paper.
Extract the reviewer's specific review POINTS from their free-text review.

Each point is a single, specific observation or critique about the paper.

Categorize each point as one of:
- "strength": A positive observation about what the paper does well
- "weakness": A criticism or identified flaw
- "suggestion": A concrete recommendation for improvement

Rules:
- Be specific: extract each distinct point separately
- Preserve the original meaning — don't paraphrase away specifics
- If a point references a specific section, figure, or table, include that reference
- Skip generic filler like "well-written paper" unless it's a specific observation
- Output a JSON object with a "points" key containing the array

Output format:
{"points": [
  {"type": "strength", "text": "...", "section": "optional §/Fig./Table ref"},
  {"type": "weakness", "text": "...", "section": "..."},
  {"type": "suggestion", "text": "...", "section": "..."}
]}"""


def _get_llm_client(model: str | None = None):
    """Get the Qwen-only LLM client used by the review and judge stages."""
    from review_engine.llm_client import (
        get_client_for_model, get_preferred_review_model_name, register_config_section,
    )
    register_config_section("llm")
    return get_client_for_model(get_preferred_review_model_name())


def _llm_json(prompt: str, system: str = "", max_tokens: int = 4096, model: str | None = None) -> dict | list:
    """Call LLM and parse JSON response."""
    client = _get_llm_client(model)
    resp = client.chat(
        messages=[{"role": "user", "content": prompt}],
        system=system,
        max_tokens=max_tokens,
        temperature=0.1,
        json_mode=True,
    )
    raw = resp.content.strip()
    if raw.startswith("```"):
        raw = raw.split("\n", 1)[-1]
        raw = raw.rsplit("```", 1)[0]
    return json.loads(raw)


def parse_human_reviews(
    papers: list[dict[str, Any]],
    force: bool = False,
    model: str | None = None,
) -> list[dict[str, Any]]:
    """Parse human review text into structured points using LLM."""
    from review_engine.llm_client import register_config_section
    if model is None:
        model = register_config_section("llm")  # use Qwen for parsing
    parsed_papers = []

    for i, paper in enumerate(papers):
        forum = paper["forum"]
        title = paper["title"][:60]
        cache_file = PARSE_CACHE_DIR / f"{forum}.json"

        if cache_file.exists() and not force:
            print(f"[parse {i+1}/{len(papers)}] {title} (cached)")
            with open(cache_file, encoding="utf-8") as f:
                parsed_papers.append(json.load(f))
            continue

        print(f"[parse {i+1}/{len(papers)}] {title}...", flush=True)

        paper_reviews = paper.get("reviews", [])
        parsed_reviews = []

        for j, rv in enumerate(paper_reviews):
            review_text = rv.get("strength_and_weaknesses", "").strip()
            if not review_text:
                continue

            # Also include summary_of_review if available
            summary = rv.get("summary_of_review", "").strip()
            if summary:
                review_text = review_text + "\n\n" + summary

            try:
                points = _llm_json(
                    prompt=f"Extract review points from this peer review:\n\n{review_text[:6000]}",
                    system=HUMAN_PARSE_PROMPT,
                    model=model,
                )
                # Normalize: unwrap dict with array key (Qwen json_object mode)
                if isinstance(points, dict) and not points.get("type"):
                    for val in points.values():
                        if isinstance(val, list):
                            points = val
                            break
                if isinstance(points, dict):
                    points = [points]
                if not isinstance(points, list):
                    points = []
            except Exception as e:
                print(f"  Review {j+1}: parse error: {e}")
                points = []

            # Normalize
            clean_points = []
            for p in points:
                if isinstance(p, dict) and "text" in p:
                    clean_points.append({
                        "type": p.get("type", "weakness"),
                        "text": p["text"][:500],
                        "section": p.get("section", ""),
                    })
            parsed_reviews.append({
                "reviewer_index": j,
                "recommendation": rv.get("recommendation", ""),
                "points": clean_points,
                "point_count": len(clean_points),
            })
            print(f"  Review {j+1}: {len(clean_points)} points extracted")

        parsed_data = {
            "forum": forum,
            "title": paper["title"],
            "reviews": parsed_reviews,
            "total_points": sum(r["point_count"] for r in parsed_reviews),
        }

        with open(cache_file, "w", encoding="utf-8") as f:
            json.dump(parsed_data, f, ensure_ascii=False, indent=1)

        parsed_papers.append(parsed_data)
        print(f"  Total: {parsed_data['total_points']} points across {len(parsed_reviews)} reviews")

    return parsed_papers


# ===========================================================================
# Stage 4: Coverage comparison (LLM-as-judge)
# ===========================================================================

COVERAGE_PROMPT = """You are evaluating how well an automated review system covers specific points raised by human reviewers.

HUMAN POINTS (specific observations/critiques):
{human_points}

AUTO-REVIEW POINTS (generated by automated system):
{auto_points}

For each HUMAN point, determine if ANY auto-review point makes the same evidence-bearing claim.
Set covered=true ONLY when all conditions hold:
1. The point type/polarity is compatible (strength cannot match criticism; absence cannot match presence).
2. The target method, experiment, section, figure, or writing issue is the same.
3. The substantive claim and scope are entailed, not merely topically related.
4. The auto point does not contradict the human point.
Use covered=false for generic overlap, partial topic overlap, opposite conclusions, or uncertain matches.

Output ONLY valid JSON (no other text):
{{"matches": [{{"human_idx": 0, "auto_idx": 3, "covered": true}}, {{"human_idx": 1, "auto_idx": null, "covered": false}}]}}"""


def _compute_coverage_chunk(
    human_chunk: list[dict[str, Any]],
    auto_points_list: list[dict[str, Any]],
    global_human_offset: int,
) -> list[dict[str, Any]]:
    """Compare a chunk of human points against auto points."""
    human_fmt = "\n".join(
        f"[{j}] ({p['type']}) {p['text'][:300]}"
        for j, p in enumerate(human_chunk)
    )
    from review_engine.alignment import select_auto_candidate_indices
    candidate_indices = select_auto_candidate_indices(
        human_chunk, auto_points_list, limit=36,
    )
    auto_fmt = "\n".join(
        f"[{index}] ({auto_points_list[index]['type']}) "
        f"[{auto_points_list[index]['dimension']}] "
        f"{auto_points_list[index]['text'][:260]}"
        for index in candidate_indices
    )

    prompt = COVERAGE_PROMPT.format(
        human_points=human_fmt,
        auto_points=auto_fmt,
    )

    # Keep prompt manageable
    if len(prompt) > 15000:
        ratio = 15000 / len(prompt)
        # Truncate both proportionally
        max_human = int(len(human_fmt) * ratio * 1.5)
        max_auto = int(len(auto_fmt) * ratio)
        human_fmt = human_fmt[:max_human] + "\n[...]"
        auto_fmt = auto_fmt[:max_auto] + "\n[...]"
        prompt = COVERAGE_PROMPT.format(human_points=human_fmt, auto_points=auto_fmt)

    try:
        result = _llm_json(prompt, max_tokens=4096)
    except Exception as e:
        # Try without json_mode
        try:
            from review_engine.llm_client import get_client_for_model
            client = get_client_for_model()
            resp = client.chat(
                messages=[{"role": "user", "content": prompt + "\n\nReturn ONLY valid JSON."}],
                max_tokens=4096,
                temperature=0.1,
                json_mode=False,
            )
            raw = resp.content.strip()
            if raw.startswith("```"):
                raw = raw.split("\n", 1)[-1]
                raw = raw.rsplit("```", 1)[0]
            result = json.loads(raw) if raw else {}
        except Exception:
            return []

    matches = result if isinstance(result, list) else result.get("matches", [])
    validated: list[dict[str, Any]] = []
    for match in matches:
        if not isinstance(match, dict):
            continue
        local_human_index = int(match.get("human_idx", 0))
        auto_index = match.get("auto_idx")
        type_compatible = (
            0 <= local_human_index < len(human_chunk)
            and isinstance(auto_index, int)
            and 0 <= auto_index < len(auto_points_list)
            and human_chunk[local_human_index].get("type") == auto_points_list[auto_index].get("type")
        )
        validated.append({
            "human_index": global_human_offset + local_human_index,
            "auto_index": auto_index,
            "covered": bool(match.get("covered", False)) and type_compatible,
            "notes": (match.get("notes", "") if type_compatible else "rejected: point type/polarity mismatch"),
        })
    return validated


def _run_alignment_recall_audit(
    human_points: list[dict[str, Any]],
    auto_points: list[dict[str, Any]],
    matches: list[dict[str, Any]],
    batch_size: int = 8,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Strict second pass for first-pass weakness/suggestion misses."""
    from review_engine.alignment import (
        build_recall_audit_prompt,
        select_auto_candidate_indices,
        validate_recall_audit_match,
    )

    covered = {
        int(item["human_index"])
        for item in matches
        if item.get("covered") and isinstance(item.get("human_index"), int)
    }
    targets = [
        index for index, point in enumerate(human_points)
        if index not in covered and point.get("type") in {"weakness", "suggestion"}
    ]
    metrics = {
        "enabled": True, "targets": len(targets), "batches": 0,
        "recovered": 0, "rejected": 0,
    }
    recovered: set[int] = set()
    for start in range(0, len(targets), batch_size):
        global_indices = targets[start:start + batch_size]
        chunk = [human_points[index] for index in global_indices]
        candidates = select_auto_candidate_indices(chunk, auto_points, limit=32)
        if not candidates:
            continue
        metrics["batches"] += 1
        try:
            result = _llm_json(
                build_recall_audit_prompt(chunk, auto_points, candidates),
                max_tokens=4096,
            )
        except Exception as exc:
            metrics.setdefault("errors", []).append(str(exc))
            continue
        proposed = result if isinstance(result, list) else result.get("matches", [])
        for item in proposed:
            if not isinstance(item, dict):
                continue
            try:
                local_index = int(item.get("human_idx", -1))
            except (TypeError, ValueError):
                metrics["rejected"] += 1
                continue
            auto_index = item.get("auto_idx")
            if (
                not 0 <= local_index < len(chunk)
                or not isinstance(auto_index, int)
                or not 0 <= auto_index < len(auto_points)
            ):
                metrics["rejected"] += 1
                continue
            global_index = global_indices[local_index]
            if global_index in recovered:
                continue
            accepted, reason = validate_recall_audit_match(
                chunk[local_index], auto_points[auto_index], item,
            )
            if not accepted:
                metrics["rejected"] += 1
                continue
            recovered.add(global_index)
            matches.append({
                "human_index": global_index,
                "auto_index": auto_index,
                "covered": True,
                "notes": "recall_audit: " + str(item.get("reason", reason)),
                "judge_confidence": float(item.get("confidence", 0.0)),
                "match_stage": "recall_audit",
            })
    metrics["recovered"] = len(recovered)
    return matches, metrics


def compute_coverage(
    parsed_papers: list[dict[str, Any]],
    auto_results: list[dict[str, Any]],
    force: bool = False,
    chunk_size: int = 5,
    min_quality: int = 1,
) -> list[dict[str, Any]]:
    """Compare human vs auto points for each paper, in chunks."""


def _flatten_auto_points(auto_result: dict[str, Any]) -> list[dict[str, Any]]:
    """Return only evidence-anchored strengths and confidence-filtered findings."""
    from review_engine.alignment import flatten_confident_auto_points
    return flatten_confident_auto_points(auto_result, min_confidence=0.55)


# Minimum quality thresholds for auto points (heuristic)
_MIN_AUTO_POINT_LENGTH = 30
_CITATION_RE = re.compile(
    r"(?:§\s*[A-Za-z0-9]|Fig\.|Figs\.|Table|Tables|Figure|Figures"
    r"|Section|Sections|Eq\.|Equation|Equations"
    r"|Algorithm|Algorithms|Theorem|Theorems"
    r"|Step|Steps)\s*\d+",
    re.IGNORECASE,
)
_GENERIC_PHRASES = [
    "good paper", "well-written", "well written", "interesting idea",
    "interesting problem", "important problem", "interesting approach",
    "sound", "solid work", "good work", "nice paper", "clear paper",
    "the paper is good", "paper is well", "work is good",
    "unable to complete", "review process", "try again",
]


def _score_auto_point_quality(point: dict[str, Any]) -> int:
    """Score an auto point's quality: 0=low (generic/no citation), 1=medium, 2=high.

    Rules:
    - 0: too short, generic filler, no citation, or error placeholder
    - 1: has some specifics but no section/figure reference
    - 2: cites a specific §/Fig./Table/Equation
    """
    text = point.get("text", "").strip()

    # Reject too-short or error placeholders
    if len(text) < _MIN_AUTO_POINT_LENGTH:
        return 0

    text_lower = text.lower()

    # Reject generic filler
    if any(phrase in text_lower for phrase in _GENERIC_PHRASES):
        return 0

    # Reject error placeholders
    if "unable to" in text_lower or "review process" in text_lower or "try again" in text_lower:
        return 0

    # Has a specific citation (evidence anchored)
    if _CITATION_RE.search(text):
        return 2

    # Has some specific content but no citation
    return 1


def _filter_auto_points(
    points: list[dict[str, Any]],
    min_quality: int = 1,
) -> list[dict[str, Any]]:
    """Filter auto points by quality score.

    Args:
        points: Flattened auto points.
        min_quality: Minimum quality score (0=lenient, 1=moderate, 2=strict).

    Returns:
        Filtered points with quality scores attached.
    """
    filtered = []
    removed = {"too_short": 0, "generic": 0, "no_citation": 0, "error": 0}
    for p in points:
        score = _score_auto_point_quality(p)
        p["_quality_score"] = score
        if score >= min_quality:
            filtered.append(p)
        elif score == 0:
            text = p.get("text", "")
            if len(text) < _MIN_AUTO_POINT_LENGTH:
                removed["too_short"] += 1
            elif any(phrase in text.lower() for phrase in _GENERIC_PHRASES):
                removed["generic"] += 1
            else:
                removed["error"] += 1
        else:
            removed["no_citation"] += 1

    if removed["too_short"] or removed["generic"] or removed["no_citation"] or removed["error"]:
        detail = "; ".join(f"{k}:{v}" for k, v in removed.items() if v)
        print(f"    [filter] removed {len(points)-len(filtered)}/{len(points)} "
              f"auto points ({detail})")

    return filtered


def compute_coverage(
    parsed_papers: list[dict[str, Any]],
    auto_results: list[dict[str, Any]],
    force: bool = False,
    chunk_size: int = 5,
    min_quality: int = 1,
) -> list[dict[str, Any]]:
    """Compare human vs auto points for each paper, in chunks."""
    # Build lookup: forum -> auto_result
    auto_by_forum: dict[str, dict[str, Any]] = {}
    for ar in auto_results:
        f = ar.get("forum", "")
        if f and "error" not in ar:
            auto_by_forum[f] = ar

    coverage_results = []

    for i, pp in enumerate(parsed_papers):
        forum = pp["forum"]
        title = pp["title"][:60]
        cache_file = COVERAGE_CACHE_DIR / f"{forum}-{PIPELINE_VERSION}.json"

        if cache_file.exists() and not force:
            with open(cache_file, encoding="utf-8") as f:
                coverage_results.append(json.load(f))
            continue

        auto_result = auto_by_forum.get(forum)
        if not auto_result:
            print(f"[coverage {i+1}/{len(parsed_papers)}] {title} — SKIP (no auto-review)")
            continue

        # Flatten human points (across all reviewers)
        human_points_list: list[dict[str, Any]] = []
        for rv in pp.get("reviews", []):
            for p in rv.get("points", []):
                human_points_list.append({
                    "reviewer_index": rv.get("reviewer_index", 0),
                    "type": p.get("type", ""),
                    "text": p["text"],
                })

        # Flatten auto points
        auto_points_list = _flatten_auto_points(auto_result)
        auto_points_list = _filter_auto_points(auto_points_list, min_quality=min_quality)

        if not human_points_list:
            print(f"[coverage {i+1}/{len(parsed_papers)}] {title} — SKIP (no human points)")
            continue
        if not auto_points_list:
            print(f"[coverage {i+1}/{len(parsed_papers)}] {title} — SKIP (no auto points)")
            continue

        print(f"[coverage {i+1}/{len(parsed_papers)}] {title}: "
              f"{len(human_points_list)} human pts, {len(auto_points_list)} auto pts, "
              f"chunk_size={chunk_size}", flush=True)

        # Process in chunks
        all_matches: list[dict[str, Any]] = []
        for chunk_start in range(0, len(human_points_list), chunk_size):
            chunk = human_points_list[chunk_start:chunk_start + chunk_size]
            print(f"  chunk {chunk_start//chunk_size + 1}: pts {chunk_start}-{chunk_start + len(chunk) - 1}",
                  end=" ", flush=True)
            try:
                chunk_matches = _compute_coverage_chunk(chunk, auto_points_list, chunk_start)
                all_matches.extend(chunk_matches)
                covered_in_chunk = sum(1 for m in chunk_matches if m.get("covered"))
                print(f"→ {covered_in_chunk}/{len(chunk)} covered", flush=True)
            except Exception as e:
                print(f"→ FAILED: {e}", flush=True)

        recall_audit = {
            "enabled": False, "targets": 0, "batches": 0, "recovered": 0,
        }
        if os.environ.get("AUTO_REVIEW_ALIGNMENT_RECALL_AUDIT", "1") != "0":
            all_matches, recall_audit = _run_alignment_recall_audit(
                human_points_list, auto_points_list, all_matches,
            )

        # Compute stats — deduplicate by human_index
        covered_human_indices = set()
        for m in all_matches:
            if m.get("covered") and m.get("human_index") is not None:
                covered_human_indices.add(m["human_index"])
        covered = len(covered_human_indices)
        not_covered = len(human_points_list) - covered

        # Determine which auto points were useful (matched at least one human point)
        matched_auto_indices = set()
        for m in all_matches:
            if m.get("covered") and m.get("auto_index") is not None:
                matched_auto_indices.add(m["auto_index"])

        from review_engine.alignment import build_usefulness_prompt, classify_alignment
        unmatched_indices = [
            index for index in range(len(auto_points_list))
            if index not in matched_auto_indices
        ]
        usefulness_judgments: list[dict[str, Any]] = []
        if unmatched_indices:
            try:
                usefulness_result = _llm_json(
                    build_usefulness_prompt(human_points_list, auto_points_list, unmatched_indices),
                    max_tokens=4096,
                )
                usefulness_judgments = (
                    usefulness_result if isinstance(usefulness_result, list)
                    else usefulness_result.get("judgments", [])
                )
            except Exception as exc:
                print(f"    [usefulness] judge failed, using deterministic fallback: {exc}")
        alignment = classify_alignment(
            human_points_list, auto_points_list, all_matches,
            usefulness_judgments=usefulness_judgments,
        )

        coverage_entry = {
            "forum": forum,
            "title": pp["title"],
            "total_human_points": len(human_points_list),
            "total_auto_points": len(auto_points_list),
            "covered_count": covered,
            "not_covered_count": not_covered,
            "coverage_pct": round(covered / len(human_points_list) * 100, 1) if human_points_list else 0,
            "matched_auto_count": len(matched_auto_indices),
            "matches": all_matches,
            "human_points": human_points_list,
            "auto_points": auto_points_list,
            "recall_audit": recall_audit,
            **alignment,
        }

        with open(cache_file, "w", encoding="utf-8") as f:
            json.dump(coverage_entry, f, ensure_ascii=False, indent=1)

        coverage_results.append(coverage_entry)
        print(f"  → Total: {covered}/{len(human_points_list)} covered ({coverage_entry['coverage_pct']:.0f}%)")

    return coverage_results


# ===========================================================================
# Stage 5: Generate report
# ===========================================================================


def _score_label(score: int) -> str:
    if score >= 85:
        return "Exceptional"
    if score >= 80:
        return "Strong Accept"
    if score >= 70:
        return "Good"
    if score >= 60:
        return "Marginal"
    return "Weak/Reject"


def generate_report(
    papers: list[dict[str, Any]],
    auto_results: list[dict[str, Any]],
    parsed_papers: list[dict[str, Any]],
    coverage_results: list[dict[str, Any]],
) -> dict[str, Any]:
    """Generate aggregate report from all stages."""
    # Build lookups
    auto_by_forum: dict[str, dict[str, Any]] = {}
    for ar in auto_results:
        f = ar.get("forum", "")
        if f and "error" not in ar:
            auto_by_forum[f] = ar

    # Auto-review score statistics
    auto_scores = []
    for ar in auto_results:
        if "error" not in ar and "overall_score" in ar:
            auto_scores.append({
                "forum": ar["forum"],
                "title": ar["title"][:60],
                "overall_score": ar["overall_score"],
            })

    avg_auto_score = (
        sum(s["overall_score"] for s in auto_scores) / len(auto_scores)
        if auto_scores else 0
    )

    # Human score statistics (from recommendations)
    human_scores = []
    for p in papers:
        for rv in p.get("reviews", []):
            rec = rv.get("recommendation", "")
            try:
                score = float(rec)
                human_scores.append({
                    "forum": p["forum"],
                    "title": p["title"][:60],
                    "score": score,
                })
            except (ValueError, TypeError):
                pass

    avg_human_score = (
        sum(s["score"] for s in human_scores) / len(human_scores)
        if human_scores else 0
    )

    # Coverage statistics
    total_covered = sum(c.get("covered_count", 0) for c in coverage_results)
    total_human = sum(c.get("total_human_points", 0) for c in coverage_results)
    total_auto = sum(c.get("total_auto_points", 0) for c in coverage_results)
    total_matched_auto = sum(c.get("matched_auto_count", 0) for c in coverage_results)

    # Coverage by point type (deduplicated by human point)
    type_coverage: dict[str, dict[str, int]] = {}
    type_seen: dict[str, set[tuple[str, int]]] = {}  # (forum, human_idx) per type
    for c in coverage_results:
        forum = c.get("forum", "")
        for m in c.get("matches", []):
            hidx = m.get("human_index")
            if hidx is not None and hidx < len(c.get("human_points", [])):
                ptype = c["human_points"][hidx].get("type", "unknown")
                if ptype not in type_coverage:
                    type_coverage[ptype] = {"covered": 0, "total": 0}
                    type_seen[ptype] = set()
                key = (forum, hidx)
                if key not in type_seen[ptype]:
                    type_seen[ptype].add(key)
                    type_coverage[ptype]["total"] += 1
                    if m.get("covered"):
                        type_coverage[ptype]["covered"] += 1

    # Coverage by dimension (which auto dimensions contributed most matches, deduplicated)
    dim_contributions: dict[str, int] = {}
    dim_seen: set[tuple[str, int]] = set()  # (forum, human_idx)
    for c in coverage_results:
        forum = c.get("forum", "")
        for m in c.get("matches", []):
            if m.get("covered"):
                hidx = m.get("human_index")
                if hidx is not None and (forum, hidx) in dim_seen:
                    continue  # Already counted this human point
                if hidx is not None:
                    dim_seen.add((forum, hidx))
                ai = m.get("auto_index")
                if ai is not None and ai < len(c.get("auto_points", [])):
                    dim = c["auto_points"][ai].get("dimension", "unknown")
                    dim_contributions[dim] = dim_contributions.get(dim, 0) + 1

    # Missed points (what human raised but auto missed)
    missed_points = []
    for c in coverage_results:
        for m in c.get("matches", []):
            if not m.get("covered"):
                hidx = m.get("human_index")
                if hidx is not None and hidx < len(c.get("human_points", [])):
                    missed_points.append({
                        "paper": c["title"][:60],
                        "text": c["human_points"][hidx]["text"],
                        "type": c["human_points"][hidx].get("type", ""),
                        "notes": m.get("notes", ""),
                    })

    # Per-paper detail
    per_paper = []
    for c in coverage_results:
        auto = auto_by_forum.get(c["forum"], {})
        score = auto.get("overall_score", 0) if auto else 0
        per_paper.append({
            "title": c["title"][:80],
            "forum": c["forum"],
            "auto_score": score,
            "auto_score_label": _score_label(score) if score else "",
            "human_points": c["total_human_points"],
            "auto_points": c["total_auto_points"],
            "covered": c.get("covered_count", 0),
            "not_covered": c.get("not_covered_count", 0),
            "coverage_pct": f"{c.get('coverage_pct', 0):.1f}%",
            "matched_auto_points": c.get("matched_auto_count", 0),
        })

    report = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "source": "ICLR 2023 (OpenReview)",
        "summary": {
            "papers_processed": len(coverage_results),
            "total_human_points": total_human,
            "total_auto_points": total_auto,
            "total_covered": total_covered,
            "overall_coverage_pct": round(total_covered / total_human * 100, 1) if total_human else 0,
            "total_matched_auto_points": total_matched_auto,
            "auto_useful_rate_pct": round(total_matched_auto / total_auto * 100, 1) if total_auto else 0,
            "avg_auto_score": round(avg_auto_score, 1),
            "avg_human_recommendation": round(avg_human_score, 1) if human_scores else 0,
        },
        "coverage_by_type": {
            ptype: {
                "covered": info["covered"],
                "total": info["total"],
                "pct": round(info["covered"] / info["total"] * 100, 1) if info["total"] else 0,
            }
            for ptype, info in sorted(type_coverage.items())
        },
        "auto_dimension_contributions": dict(
            sorted(dim_contributions.items(), key=lambda x: x[1], reverse=True)
        ),
        "missed_points": missed_points[:30],  # Top missed points
        "per_paper": per_paper,
    }

    # Save JSON report
    report_path = CACHE_DIR / "coverage_report.json"
    with open(report_path, "w", encoding="utf-8") as f:
        json.dump(report, f, ensure_ascii=False, indent=2)
    print(f"\n[report] JSON saved to {report_path}")

    return report


def print_report(report: dict[str, Any]) -> None:
    """Print a human-readable report."""
    s = report["summary"]
    print("\n" + "=" * 60)
    print("  AUTO-REVIEW COVERAGE EVALUATION REPORT")
    print("=" * 60)
    print(f"  Source: {report['source']}")
    print(f"  Papers evaluated: {s['papers_processed']}")
    print(f"\n  ▸ OVERALL COVERAGE: {s['overall_coverage_pct']}%")
    print(f"    ({s['total_covered']}/{s['total_human_points']} human-review points covered)")
    print(f"\n  ▸ AUTO POINT USEFULNESS: {s['auto_useful_rate_pct']}%")
    print(f"    ({s['total_matched_auto_points']}/{s['total_auto_points']} auto points matched human points)")
    print(f"\n  ▸ AVG AUTO SCORE: {s['avg_auto_score']}/100")
    print(f"  ▸ AVG HUMAN REC.: {s['avg_human_recommendation']}/10")
    print()

    if report.get("coverage_by_type"):
        print("  Coverage by point type:")
        for ptype, info in sorted(report["coverage_by_type"].items()):
            print(f"    {ptype:15s}  {info['pct']:5.1f}%  ({info['covered']}/{info['total']})")

    if report.get("auto_dimension_contributions"):
        print("\n  Auto-review dimension contributions to coverage:")
        for dim, count in list(report["auto_dimension_contributions"].items())[:7]:
            print(f"    {dim:20s}  matched {count} human points")

    print("\n  Per-paper breakdown:")
    print(f"  {'Paper':40s} {'Cov%':>6s} {'Human':>6s} {'Auto':>6s} {'Match':>6s} {'Score':>6s}")
    print("  " + "-" * 74)
    for pp in report.get("per_paper", []):
        print(f"  {pp['title'][:38]:38s} {pp['coverage_pct']:>6s} {pp['human_points']:>6d} "
              f"{pp['auto_points']:>6d} {pp['matched_auto_points']:>6d} {pp['auto_score']:>6d}")

    missed = report.get("missed_points", [])
    if missed:
        print(f"\n  Top missed points ({min(len(missed), 10)} shown):")
        for mp in missed[:10]:
            print(f"    [{mp['type']}] {mp['text'][:100]}")
            print(f"             ({mp['paper']})")


# ===========================================================================
# Miss pattern analysis
# ===========================================================================

# Known miss categories with keyword patterns
MISS_PATTERNS: list[dict[str, Any]] = [
    {"id": "missing_sota_baselines", "label": "Missing SOTA/baseline comparison",
     "keywords": ["sota", "baseline", "compare", "benchmark", "batch norm", "layernorm"]},
    {"id": "limited_scope", "label": "Limited scope (task/model/dataset)",
     "keywords": ["only", "limited", "not enough", "not complete", "light-weight", "heavy", "only one task",
                   "only test", "only classification", "only on"]},
    {"id": "missing_theory", "label": "Missing theoretical analysis",
     "keywords": ["theor", "proof", "mathematical", "empirical only"]},
    {"id": "unclear_setup", "label": "Unclear experimental setup",
     "keywords": ["not clear", "unclear", "how to", "how the", "not specified", "not described"]},
    {"id": "writing_notation", "label": "Writing / notation quality",
     "keywords": ["notation", "confus", "repetit", "poor", "duplicat", "undefined"]},
    {"id": "overclaiming", "label": "Overclaiming / vague claims",
     "keywords": ["overclaim", "not support", "conclusion", "vague", "carefully design"]},
    {"id": "novelty_depth", "label": "Novelty / contribution depth",
     "keywords": ["workshop", "not interest", "incremental", "insight", "trivial", "obvious"]},
    {"id": "figure_quality", "label": "Figure / visualization quality",
     "keywords": ["visual", "figure", "read", "caption", "axis", "legend"]},
    {"id": "reproducibility", "label": "Reproducibility / code release",
     "keywords": ["reproducib", "code", "open source"]},
    {"id": "limited_applicability", "label": "Limited broader applicability",
     "keywords": ["applicab", "generaliz", "broader"]},
]


def _categorize_missed_point(text: str) -> str:
    """Categorize a missed point text into a pattern id."""
    text_lower = text.lower()
    for pat in MISS_PATTERNS:
        if any(kw in text_lower for kw in pat["keywords"]):
            return pat["id"]
    return "other"


def analyze_miss_patterns(
    coverage_results: list[dict[str, Any]],
) -> dict[str, Any]:
    """Analyze coverage results and extract recurring miss patterns.

    Returns a structured report with:
    - pattern frequencies (overall and by type)
    - per-paper breakdown
    - actionable prompt improvement suggestions
    """
    from collections import Counter

    pattern_counts: Counter = Counter()
    type_patterns: dict[str, Counter] = {}
    per_paper: dict[str, dict[str, int]] = {}
    examples: dict[str, list[str]] = {}

    for c in coverage_results:
        paper_title = c.get("title", "?")[:40]
        per_paper[paper_title] = {}

        for m in c.get("matches", []):
            if m.get("covered"):
                continue
            hidx = m.get("human_index")
            if hidx is None or hidx >= len(c.get("human_points", [])):
                continue
            hp = c["human_points"][hidx]
            text = hp.get("text", "")
            ptype = hp.get("type", "unknown")

            cat = _categorize_missed_point(text)
            pattern_counts[cat] += 1

            if ptype not in type_patterns:
                type_patterns[ptype] = Counter()
            type_patterns[ptype][cat] += 1

            per_paper[paper_title][cat] = per_paper[paper_title].get(cat, 0) + 1

            if cat not in examples:
                examples[cat] = []
            if len(examples[cat]) < 3:
                examples[cat].append(text[:120])

    # Build prompt improvement suggestions
    suggestions = []
    for pat_id, count in pattern_counts.most_common():
        if count < 2:
            continue
        pat_def = next((p for p in MISS_PATTERNS if p["id"] == pat_id), None)
        if pat_def:
            suggestions.append({
                "category": pat_def["label"],
                "miss_count": count,
                "suggestion": f"Add explicit '{pat_def['label']}' checklist item in the relevant dimension prompt.",
                "examples": examples.get(pat_id, []),
            })

    return {
        "total_missed": sum(pattern_counts.values()),
        "pattern_frequencies": dict(pattern_counts.most_common()),
        "by_type": {pt: dict(c.most_common()) for pt, c in type_patterns.items()},
        "per_paper": per_paper,
        "prompt_suggestions": suggestions,
    }


def print_miss_analysis(analysis: dict[str, Any]) -> None:
    """Print miss pattern analysis report."""
    print("\n" + "=" * 60)
    print("  MISS PATTERN ANALYSIS")
    print("=" * 60)
    print(f"  Total missed points: {analysis['total_missed']}\n")

    print("  Top miss patterns:")
    for cat, count in sorted(analysis["pattern_frequencies"].items(), key=lambda x: -x[1]):
        print(f"    {count:3d}x {cat}")

    if analysis.get("by_type"):
        print("\n  By point type:")
        for ptype, patterns in analysis["by_type"].items():
            top = sorted(patterns.items(), key=lambda x: -x[1])[:3]
            print(f"    {ptype}:")
            for cat, count in top:
                print(f"      {count}x  {cat}")

    if analysis.get("prompt_suggestions"):
        print("\n  Suggested prompt improvements:")
        for s in analysis["prompt_suggestions"]:
            print(f"    [{s['miss_count']}x] {s['suggestion']}")

    if analysis.get("per_paper"):
        print("\n  Per-paper patterns:")
        for paper, patterns in analysis["per_paper"].items():
            top = sorted(patterns.items(), key=lambda x: -x[1])[:3]
            cats = "; ".join(f"{cat}({count})" for cat, count in top)
            print(f"    {paper[:30]:30s} {cats}")


# ===========================================================================
# Main
# ===========================================================================


def main() -> None:
    import argparse

    parser = argparse.ArgumentParser(description="Evaluate auto-review coverage vs human reviews")
    parser.add_argument("--papers", type=int, default=5, help="Number of papers to process")
    parser.add_argument("--max-papers", type=int, default=100, help="Max papers to fetch from OpenReview")
    parser.add_argument("--skip-review", action="store_true", help="Skip auto-review step (use cached)")
    parser.add_argument("--skip-parse", action="store_true", help="Skip human review parsing step")
    parser.add_argument("--force", action="store_true", help="Re-run all steps, ignoring cache")
    parser.add_argument("--report-only", action="store_true", help="Only regenerate report from cached data")
    parser.add_argument("--analyze-misses", action="store_true",
                        help="Analyze miss patterns from cached coverage and suggest prompt improvements")
    parser.add_argument("--min-quality", type=int, default=0, choices=[0, 1, 2],
                        help="Min auto-point quality: 0=off (default), 1=filter generic, 2=require citation")
    args = parser.parse_args()

    if args.analyze_misses:
        print("[main] Miss pattern analysis mode — loading cached coverage\n")
        coverage_results = []
        for f in sorted(COVERAGE_CACHE_DIR.glob(f"*-{PIPELINE_VERSION}.json")):
            coverage_results.append(json.load(open(f)))
        if not coverage_results:
            print("No cached coverage data found. Run evaluation first.")
            return
        analysis = analyze_miss_patterns(coverage_results)
        print_miss_analysis(analysis)
        return

    if args.report_only:
        # Load from cache and regenerate report
        print("[main] Report-only mode — loading cached results")
        papers = json.load(open(PAPER_CACHE_DIR / "papers_index.json"))
        auto_results = []
        for f in sorted(REVIEW_CACHE_DIR.glob(f"*-{PIPELINE_VERSION}.json")):
            auto_results.append(json.load(open(f)))
        parsed_papers = []
        for f in sorted(PARSE_CACHE_DIR.glob("*.json")):
            parsed_papers.append(json.load(open(f)))
        coverage_results = []
        for f in sorted(COVERAGE_CACHE_DIR.glob(f"*-{PIPELINE_VERSION}.json")):
            coverage_results.append(json.load(open(f)))
        report = generate_report(papers, auto_results, parsed_papers, coverage_results)
        print_report(report)
        return

    # Stage 1: Fetch papers
    print("\n" + "=" * 50)
    print("  STAGE 1: Fetch papers from OpenReview")
    print("=" * 50)
    papers = fetch_papers_with_reviews(max_papers=args.max_papers, force=args.force)

    # Limit
    papers = papers[:args.papers]
    print(f"\n  Processing first {len(papers)} papers\n")

    # Stage 2: Run auto-review
    print("\n" + "=" * 50)
    print("  STAGE 2: Run auto-review")
    print("=" * 50)
    if args.skip_review:
        print("  Skipping (loading from cache)")
        auto_results = []
        for f in sorted(REVIEW_CACHE_DIR.glob(f"*-{PIPELINE_VERSION}.json")):
            auto_results.append(json.load(open(f)))
    else:
        auto_results = run_auto_reviews(papers, force=args.force)
    print(f"  Auto-review results: {len(auto_results)} papers\n")

    # Stage 3: Parse human reviews
    print("\n" + "=" * 50)
    print("  STAGE 3: Parse human reviews into structured points")
    print("=" * 50)
    if args.skip_parse:
        print("  Skipping (loading from cache)")
        parsed_papers = []
        for f in sorted(PARSE_CACHE_DIR.glob("*.json")):
            parsed_papers.append(json.load(open(f)))
    else:
        parsed_papers = parse_human_reviews(papers, force=args.force)
    print(f"  Parsed papers: {len(parsed_papers)}\n")

    # Stage 4: Coverage comparison
    print("\n" + "=" * 50)
    print("  STAGE 4: Coverage comparison (LLM-as-judge)")
    print("=" * 50)
    coverage_results = compute_coverage(
        parsed_papers, auto_results, force=args.force, min_quality=args.min_quality,
    )
    print(f"  Coverage computed for: {len(coverage_results)} papers\n")

    # Stage 5: Report
    print("\n" + "=" * 50)
    print("  STAGE 5: Generate report")
    print("=" * 50)
    report = generate_report(papers, auto_results, parsed_papers, coverage_results)
    print_report(report)

    # Stage 6: Miss pattern analysis
    if coverage_results:
        analysis = analyze_miss_patterns(coverage_results)
        print_miss_analysis(analysis)


if __name__ == "__main__":
    main()
