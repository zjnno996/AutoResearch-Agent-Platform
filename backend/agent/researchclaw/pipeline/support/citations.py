"""Support citations components."""

from __future__ import annotations

import logging
import math
from pathlib import Path
import re

from researchclaw.pipeline.support.artifacts import _parse_jsonl_rows, _read_literature_bib

logger = logging.getLogger(__name__)


_CANONICAL_BASELINE_BIB: dict[str, str] = {
    "anguita2013uci": """@misc{anguita2013uci,
  author = {Jorge Reyes-Ortiz and Davide Anguita and Alessandro Ghio and Luca Oneto and Xavier Parra},
  title = {Human Activity Recognition Using Smartphones},
  year = {2013},
  publisher = {UCI Machine Learning Repository},
  doi = {10.24432/C54S4K},
  url = {https://doi.org/10.24432/C54S4K}
}""",
    "pedregosa2011scikit": """@article{pedregosa2011scikit,
  author = {Fabian Pedregosa and Gael Varoquaux and Alexandre Gramfort and Vincent Michel and Bertrand Thirion and Olivier Grisel and Mathieu Blondel and Peter Prettenhofer and Ron Weiss and Vincent Dubourg and Jake Vanderplas and Alexandre Passos and David Cournapeau and Matthieu Brucher and Matthieu Perrot and Edouard Duchesnay},
  title = {Scikit-learn: Machine Learning in Python},
  journal = {Journal of Machine Learning Research},
  volume = {12},
  number = {85},
  pages = {2825--2830},
  year = {2011},
  url = {https://jmlr.org/papers/v12/pedregosa11a.html}
}""",
    "breiman2001random": """@article{breiman2001random,
  author = {Leo Breiman},
  title = {Random Forests},
  journal = {Machine Learning},
  volume = {45},
  pages = {5--32},
  year = {2001},
  doi = {10.1023/A:1010933404324},
  url = {https://doi.org/10.1023/A:1010933404324}
}""",
}


def _augment_canonical_citations(bib_text: str, paper: str) -> str:
    """Add cited, authoritative dataset/implementation/method references.

    Literature retrieval can miss foundational works because it favors recent
    topic matches.  These entries have stable publisher records and are added
    only when the manuscript actually cites their fixed keys.
    """
    existing = set(re.findall(r"@\w+\{([^,]+),", bib_text))
    cited: set[str] = set()
    citation_key_re = re.compile(r"^[a-zA-Z][a-zA-Z0-9_-]*\d{4}[a-zA-Z0-9_]*$")
    for bracket in re.findall(r"\[([^\]]+)\]", paper):
        for candidate in bracket.split(","):
            candidate = candidate.strip()
            if citation_key_re.fullmatch(candidate):
                cited.add(candidate)
    for latex_group in re.findall(r"\\cite\{([^}]+)\}", paper):
        cited.update(candidate.strip() for candidate in latex_group.split(","))
    additions = [
        entry for key, entry in _CANONICAL_BASELINE_BIB.items()
        if key not in existing
        and key in cited
    ]
    if not additions:
        return bib_text
    return bib_text.rstrip() + "\n\n" + "\n\n".join(additions) + "\n"


def _sanitize_bibtex_for_latex(bib_text: str) -> str:
    """Escape common verified-metadata characters that break pdfLaTeX."""
    sanitized = re.sub(r"(?<!\\)&", r"\\&", bib_text)
    sanitized = sanitized.replace("ℓ", r"{$\ell$}")
    return sanitized


def _build_deterministic_citation_context(
    run_dir: Path,
    limit: int = 5,
) -> str:
    """Build citation-grounded prose from the screened shortlist only."""
    bib_text = _read_literature_bib(run_dir)
    valid_keys = set(re.findall(r"@\w+\{([^,]+),", bib_text))
    if not valid_keys:
        return ""
    candidates_path = run_dir / "stage-04" / "candidates.jsonl"
    try:
        rows = _parse_jsonl_rows(candidates_path.read_text(encoding="utf-8"))
    except OSError:
        rows = []
    keyword_weights = {
        "logistic regression": 6.0,
        "random forest": 6.0,
        "tabular": 5.0,
        "small data": 3.0,
        "reproducib": 2.5,
        "benchmark": 2.0,
        "classification": 1.0,
    }

    def _citation_score(row: dict[str, Any]) -> float:
        haystack = f"{row.get('title', '')} {row.get('abstract', '')}".lower()
        direct = any(term in haystack for term in ("logistic regression", "random forest", "tabular"))
        if not direct:
            return -1.0
        score = sum(weight for term, weight in keyword_weights.items() if term in haystack)
        score += min(3.0, math.log10(1.0 + float(row.get("citation_count", 0) or 0)))
        if row.get("doi") or row.get("arxiv_id"):
            score += 1.0
        return score

    eligible = [
        row for row in rows
        if str(row.get("cite_key", "")) in valid_keys and _citation_score(row) >= 0
    ]
    eligible.sort(key=_citation_score, reverse=True)
    sentences: list[str] = []
    for row in eligible[:limit]:
        key = str(row.get("cite_key", ""))
        title = str(row.get("title", "")).strip()
        if not key or not title:
            continue
        haystack = f"{title} {row.get('abstract', '')}".lower()
        matched = [term for term in keyword_weights if term in haystack][:4]
        scope = ", ".join(matched) or "small-data classification"
        sentences.append(
            f"Prior work relevant to {scope} includes *{title}* [{key}]."
        )
    return "\n\n".join(sentences)
