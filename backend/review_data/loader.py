"""Unified review data loader.

Provides a single entry point for loading review data from any supported source.
Handles caching, filtering, and export in a consistent format.

Usage:
    from review_data.loader import load_review_data
    ds = load_review_data("reviewer2", "iclr_2023")
    ds = load_review_data("peerread", "neurips_2018")
    ds = load_review_data("openreview", "iclr2025", max_papers=100)

    # Quick stats
    ds.describe()

    # Export few-shot examples
    ds.export_fewshot("output.jsonl")
"""

from __future__ import annotations

import json
import random
import sys
from pathlib import Path
from typing import Any

from .schema import Review, ReviewDataset
from .sources import peerread, reviewer2, openreview


DATA_CACHE_DIR = Path(__file__).resolve().parent / "cache"


def load_review_data(
    source: str,
    name: str = "",
    max_items: int = 500,
    min_score: float | None = None,
    max_score: float | None = None,
    use_cache: bool = True,
    **kwargs: Any,
) -> ReviewDataset:
    """Load review data from any supported source.

    Args:
        source: One of "reviewer2", "peerread", "openreview"
        name: Dataset name (e.g. "iclr_2023", "neurips_2018", "iclr2025")
        max_items: Maximum items to return after loading
        min_score: Optional minimum score filter (0-10 scale)
        max_score: Optional maximum score filter (0-10 scale)
        use_cache: Use cached data
        **kwargs: Additional args passed to the source loader

    Returns:
        ReviewDataset with normalized reviews
    """
    source = source.lower().strip()
    ds: ReviewDataset

    if source == "reviewer2":
        if not name:
            name = "iclr_2023"
        if "max_papers" in kwargs and "max_items" not in kwargs:
            kwargs.setdefault("max_papers", kwargs.pop("max_papers"))
        ds = reviewer2.load_dataset(name, use_cache=use_cache)
    elif source == "peerread":
        if not name:
            name = "iclr_2017"
        ds = peerread.load_dataset(name, use_cache=use_cache)
    elif source == "openreview":
        if not name:
            name = "iclr2025"
        max_papers = kwargs.pop("max_papers", 50)
        ds = openreview.fetch_venue_reviews(name, max_papers=max_papers, use_cache=use_cache)
    else:
        raise ValueError(f"Unknown source '{source}'. Options: reviewer2, peerread, openreview")

    # Score filter
    if min_score is not None:
        ds = ds.filter_by_quality(min_score=min_score, max_score=max_score or 10.0)

    # Limit
    if len(ds) > max_items:
        ds = ds.sample(max_items)

    return ds


def load_best_reviews(
    source: str = "reviewer2",
    name: str = "iclr_2023",
    min_score: float = 7.0,
    max_items: int = 50,
    use_cache: bool = True,
) -> ReviewDataset:
    """Load high-quality reviews (scores >= min_score) for use as few-shot examples."""
    return load_review_data(
        source=source,
        name=name,
        max_items=max_items,
        min_score=min_score,
        use_cache=use_cache,
    )


def load_high_and_low_reviews(
    source: str = "reviewer2",
    name: str = "iclr_2023",
    high_min: float = 7.0,
    low_max: float = 4.0,
    high_count: int = 30,
    low_count: int = 20,
    use_cache: bool = True,
) -> ReviewDataset:
    """Load both high-quality and low-quality reviews for contrastive few-shot."""
    from .sources import reviewer2 as _r2

    # Load full dataset
    full = _r2.load_dataset(name, use_cache=use_cache)

    high = [r for r in full.reviews if r.overall_score is not None and r.overall_score >= high_min]
    low = [r for r in full.reviews if r.overall_score is not None and r.overall_score <= low_max]

    rng = random.Random(42)
    sampled = rng.sample(high, min(high_count, len(high)))
    sampled += rng.sample(low, min(low_count, len(low)))
    rng.shuffle(sampled)

    return ReviewDataset(
        name=f"{name}_high_low",
        source=full.source,
        reviews=sampled,
    )


def export_dataset(
    ds: ReviewDataset,
    output_path: str | Path,
    format: str = "jsonl",
) -> Path:
    """Export a dataset to disk for reuse.

    Formats:
      - "jsonl": One JSON object per line (default)
      - "fewshot": Few-shot block format ready for prompt injection
    """
    out = Path(output_path)
    out.parent.mkdir(parents=True, exist_ok=True)

    if format == "jsonl":
        with open(out, "w", encoding="utf-8") as f:
            for review in ds.reviews:
                f.write(json.dumps(review.to_dict(), ensure_ascii=False) + "\n")
    elif format == "fewshot":
        with open(out, "w", encoding="utf-8") as f:
            for i, review in enumerate(ds.reviews):
                score_info = f"[Score: {review.overall_score:.1f}/10] " if review.overall_score is not None else ""
                f.write(f"## Example {i+1} {score_info} \n\n")
                f.write(review.to_fewshot_block(max_chars=1000))
                f.write("\n\n---\n\n")
    else:
        raise ValueError(f"Unknown format '{format}'")

    print(f"[export] Wrote {len(ds)} reviews to {out}")
    return out


def list_datasets() -> None:
    """Print available datasets for each source."""
    print("Available datasets:\n")

    print("reviewer2:")
    for name in reviewer2.DATASETS:
        print(f"  - {name}")

    print("\npeerread:")
    for name in peerread.DATASETS:
        print(f"  - {name}")

    print("\nopenreview:")
    for name in openreview.VENUES:
        print(f"  - {name}")


# =============================================================================
# CLI
# =============================================================================

def main() -> None:
    """CLI entry: python -m review_data <command> [args]

    Commands:
      list              List available datasets
      fetch <source> <name> [--max N] [--min-score M] [--output path]
      export <source> <name> [--format jsonl|fewshot] [--output path]
    """
    args = sys.argv[1:]
    if not args or args[0] in ("-h", "--help"):
        print(main.__doc__)
        return

    cmd = args[0]

    if cmd == "list":
        list_datasets()
        return

    if cmd == "fetch":
        if len(args) < 3:
            print("Usage: python -m review_data fetch <source> <name> [--max N] [--min-score M] [--output path]")
            return
        source = args[1]
        name = args[2]

        kwargs: dict[str, Any] = {}
        if "--max" in args:
            kwargs["max_items"] = int(args[args.index("--max") + 1])
        if "--min-score" in args:
            kwargs["min_score"] = float(args[args.index("--min-score") + 1])

        ds = load_review_data(source, name, **kwargs)
        ds.describe()

        if "--output" in args:
            out_path = args[args.index("--output") + 1]
            fmt = "fewshot" if "--format" in args and args[args.index("--format") + 1] == "fewshot" else "jsonl"
            export_dataset(ds, out_path, format=fmt)

    elif cmd == "export":
        if len(args) < 3:
            print("Usage: python -m review_data export <source> <name> [--format jsonl|fewshot] [--output path]")
            return

        source = args[1]
        name = args[2]
        fmt = "fewshot" if "--format" in args else "jsonl"
        if "--format" in args:
            fmt = args[args.index("--format") + 1]

        ds = load_review_data(source, name)
        out_path = f"data_{source}_{name}.jsonl"
        if "--output" in args:
            out_path = args[args.index("--output") + 1]
        export_dataset(ds, out_path, format=fmt)

    else:
        print(f"Unknown command '{cmd}'. Try: list, fetch, export")


if __name__ == "__main__":
    main()
