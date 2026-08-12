"""OpenReview API data fetcher.

Fetches public review data from OpenReview and normalizes to our schema.

Endpoints (no auth required for public data):
  GET https://api.openreview.net/notes?invitation=<venue>/-/Blind_Submission
  GET https://api.openreview.net/notes?forum=<paper_forum>
"""

from __future__ import annotations

import json
import os
import re
import time
import urllib.request
import urllib.error
from pathlib import Path
from typing import Any

from ..schema import Review, ReviewItem, DimensionReview, ReviewDataset

API_BASE = "https://api.openreview.net"
DATA_DIR = Path(__file__).resolve().parent.parent / "cache" / "openreview"
REQUEST_DELAY = 1.0  # Seconds between requests (be polite)
MAX_RETRIES = 3

# Well-known venue invitations
# Format: {short_name: (invitation_id, year)}
VENUES: dict[str, tuple[str, int]] = {
    "iclr2025": ("ICLR.cc/2025/Conference", 2025),
    "iclr2024": ("ICLR.cc/2024/Conference", 2024),
    "iclr2023": ("ICLR.cc/2023/Conference", 2023),
    "neurips2024": ("NeurIPS.cc/2024/Conference", 2024),
    "neurips2023": ("NeurIPS.cc/2023/Conference", 2023),
    "neurips2022": ("NeurIPS.cc/2022/Conference", 2022),
}


def _request(url: str, retries: int = MAX_RETRIES) -> dict[str, Any] | list[Any]:
    """Make a GET request to OpenReview API with retry."""
    req = urllib.request.Request(
        url,
        headers={"User-Agent": "ClawAI-Review/1.0"},
    )
    for attempt in range(retries):
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except (urllib.error.HTTPError, urllib.error.URLError, json.JSONDecodeError) as e:
            if attempt < retries - 1:
                time.sleep(REQUEST_DELAY * (attempt + 1))
                continue
            raise
    return {}


def _ensure_cache_dir() -> Path:
    """Create cache directory if needed."""
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    return DATA_DIR


def _cache_path(key: str) -> Path:
    """Get file path for a cache key."""
    safe = re.sub(r"[^a-zA-Z0-9_]", "_", key)
    return _ensure_cache_dir() / f"{safe}.json"


def _load_cache(key: str) -> list[dict[str, Any]] | None:
    """Load cached data if fresh (within 24h)."""
    path = _cache_path(key)
    if not path.exists():
        return None
    try:
        age = time.time() - path.stat().st_mtime
        if age > 86400:  # 24h cache
            return None
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return None


def _save_cache(key: str, data: list[dict[str, Any]]) -> None:
    """Save data to cache."""
    path = _cache_path(key)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False)


# =============================================================================
# Paper listing
# =============================================================================

def list_submissions(
    venue_id: str,
    year: int = 2025,
    limit: int = 100,
    offset: int = 0,
    use_cache: bool = True,
) -> list[dict[str, Any]]:
    """List blind submissions for a venue.

    Returns raw note objects with content.title, content.abstract, content.keywords.
    """
    cache_key = f"submissions_{venue_id.replace('/', '_')}_{limit}_{offset}"
    if use_cache:
        cached = _load_cache(cache_key)
        if cached:
            return cached

    url = f"{API_BASE}/notes?invitation={venue_id}/-/Blind_Submission&limit={limit}&offset={offset}"
    data = _request(url)

    notes = data.get("notes", []) if isinstance(data, dict) else []
    if use_cache:
        _save_cache(cache_key, notes)

    return notes


def list_all_submissions(venue_id: str, year: int = 2025, max_total: int = 500) -> list[dict[str, Any]]:
    """List all submissions for a venue, paginating through results."""
    all_notes: list[dict[str, Any]] = []
    offset = 0

    while offset < max_total:
        batch = list_submissions(venue_id, year, limit=100, offset=offset, use_cache=True)
        if not batch:
            break
        all_notes.extend(batch)
        offset += len(batch)

    return all_notes


# =============================================================================
# Review fetching per paper
# =============================================================================

def get_reviews_for_paper(
    paper_forum: str,
    venue_id: str,
    use_cache: bool = True,
) -> list[dict[str, Any]]:
    """Get all review notes for a paper.

    Returns list of note objects with content.review, content.recommendation,
    content.rating, content.confidence.
    """
    cache_key = f"reviews_{venue_id.replace('/', '_')}_{paper_forum}"
    if use_cache:
        cached = _load_cache(cache_key)
        if cached:
            return cached

    url = f"{API_BASE}/notes?forum={paper_forum}"
    data = _request(url)
    notes = data.get("notes", []) if isinstance(data, dict) else []

    if use_cache:
        _save_cache(cache_key, notes)

    return notes


def _extract_venue_score(venue_id: str, rating: Any) -> float | None:
    """Normalize a venue's rating to a 0-10 scale."""
    if rating is None:
        return None
    try:
        rating = float(rating)
    except (ValueError, TypeError):
        return None

    # ICLR uses 1-10 scale
    if "ICLR" in venue_id:
        return min(10.0, max(1.0, rating))
    # NeurIPS uses 1-5 then doubled for display
    if "NeurIPS" in venue_id:
        # NeurIPS ratings are 1-5, sometimes doubled
        if rating > 5:
            return rating / 2
        return rating * 2
    return min(10.0, max(1.0, rating))


def normalize_openreview_review(
    note: dict[str, Any],
    venue_id: str,
    year: int,
    paper_title: str = "",
    paper_keywords: list[str] | None = None,
) -> Review:
    """Convert an OpenReview note object to our Review schema.

    OpenReview note content varies by venue. We extract common fields.
    """
    content = note.get("content", {})

    # Try different field names (OpenReview has inconsistent naming)
    review_text = (
        content.get("review", "") or
        content.get("review_text", "") or
        ""
    )
    comment = (
        content.get("comment", "") or
        ""
    )

    # Rating
    raw_rating = content.get("rating") or content.get("recommendation") or ""
    rating = _extract_venue_score(venue_id, raw_rating)

    # Confidence
    confidence_raw = content.get("confidence", "")
    confidence = None
    try:
        confidence = float(confidence_raw) if confidence_raw else None
    except (ValueError, TypeError):
        pass

    # Recommendation label
    recommendation = content.get("recommendation", "") or content.get("decision", "") or ""
    if isinstance(recommendation, (int, float)):
        recommendation = f"{recommendation}/10"

    # Try to parse rating into recommendation text
    rec_map = {
        1: "reject", 2: "reject", 3: "weak-reject",
        4: "weak-reject", 5: "borderline", 6: "borderline",
        7: "weak-accept", 8: "accept", 9: "strong-accept", 10: "strong-accept",
    }
    if rating and not recommendation:
        rounded = round(rating)
        recommendation = rec_map.get(rounded, f"{rating}/10")

    return Review(
        source="openreview",
        source_id=note.get("id", "") or note.get("forum", ""),
        paper_title=paper_title,
        paper_venue=venue_id.split("/")[0],
        paper_year=year,
        paper_keywords=paper_keywords or [],
        overall_score=rating,
        recommendation=recommendation,
        confidence=confidence,
        comment_to_author=review_text,
        comment_to_ac=comment,
    )


# =============================================================================
# Bulk fetch for a venue
# =============================================================================

def fetch_venue_reviews(
    venue_key: str = "iclr2025",
    max_papers: int = 50,
    min_reviews: int = 1,
    use_cache: bool = True,
) -> ReviewDataset:
    """Fetch reviews for an entire venue.

    Args:
        venue_key: Short name from VENUES dict, e.g. "iclr2025", "neurips2024"
        max_papers: Maximum number of papers to process
        min_reviews: Minimum reviews required per paper to keep
        use_cache: Use cached data if available

    Returns:
        ReviewDataset with all fetched reviews
    """
    if venue_key not in VENUES:
        available = ", ".join(VENUES.keys())
        raise ValueError(f"Unknown venue '{venue_key}'. Available: {available}")

    venue_id, year = VENUES[venue_key]

    print(f"[openreview] Fetching papers for {venue_key} ({venue_id}, {year})...")
    papers = list_all_submissions(venue_id, year, max_total=max_papers)
    print(f"[openreview] Got {len(papers)} papers")

    all_reviews: list[Review] = []

    for i, paper in enumerate(papers):
        content = paper.get("content", {})
        forum = paper.get("forum", "")
        if not forum:
            continue

        title = content.get("title", paper.get("id", "Untitled"))
        keywords = content.get("keywords", [])
        if isinstance(keywords, str):
            keywords = [kw.strip() for kw in keywords.split(",") if kw.strip()]

        print(f"[openreview]  [{i+1}/{len(papers)}] Fetching reviews for: {title[:60]}...")

        try:
            review_notes = get_reviews_for_paper(forum, venue_id, use_cache=use_cache)
        except Exception:
            continue

        # Filter to only reviews (not author rebuttals, AC decisions, etc.)
        review_invitation = f"{venue_id}/-/Paper.*/Official_Review"
        reviews = [
            n for n in review_notes
            if re.search(review_invitation, n.get("invitation", ""))
        ]

        if not reviews and min_reviews == 0:
            # Still include, just without review data
            all_reviews.append(Review(
                source="openreview",
                source_id=forum,
                paper_title=title,
                paper_venue=venue_id.split("/")[0],
                paper_year=year,
                paper_keywords=keywords,
            ))
        elif reviews:
            for rn in reviews:
                review = normalize_openreview_review(rn, venue_id, year, title, keywords)
                all_reviews.append(review)

        # Be polite to API
        if i < len(papers) - 1:
            time.sleep(REQUEST_DELAY)

    ds = ReviewDataset(
        name=f"openreview_{venue_key}",
        source="openreview",
        reviews=all_reviews,
    )

    print(f"[openreview] Collected {len(all_reviews)} reviews from {len(papers)} papers")
    return ds


# =============================================================================
# CLI entry point
# =============================================================================

def fetch_main(args: list[str] | None = None) -> None:
    """CLI entry: python -m review_data.sources.openreview <venue> <max_papers>"""
    import sys

    venue = args[0] if args and len(args) > 0 else "iclr2025"
    max_papers = int(args[1]) if args and len(args) > 1 else 50

    ds = fetch_venue_reviews(venue_key=venue, max_papers=max_papers)

    print(f"\n=== Summary ===")
    print(f"Total reviews: {len(ds)}")

    # Score distribution
    scores = [r.overall_score for r in ds.reviews if r.overall_score is not None]
    if scores:
        import statistics
        print(f"Score range: {min(scores):.1f} - {max(scores):.1f}")
        print(f"Score mean: {statistics.mean(scores):.1f}")

    # Save to disk as JSONL
    out_path = _ensure_cache_dir() / f"{venue}.jsonl"
    with open(out_path, "w", encoding="utf-8") as f:
        for review in ds.reviews:
            f.write(json.dumps(review.to_dict(), ensure_ascii=False) + "\n")
    print(f"Saved to {out_path}")


if __name__ == "__main__":
    fetch_main()
