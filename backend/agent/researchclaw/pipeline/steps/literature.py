"""Search, retrieve, screen, extract and synthesize source literature."""

from __future__ import annotations

from datetime import datetime, timezone
import json
import logging
from pathlib import Path
import re
from typing import Any

import yaml

from researchclaw.adapters import AdapterBundle
from researchclaw.config import RCConfig
from researchclaw.llm.client import LLMClient
from researchclaw.pipeline.models import StageResult
from researchclaw.pipeline.stages import Stage, StageStatus
from researchclaw.pipeline.support.artifacts import (
    _parse_jsonl_rows,
    _read_prior_artifact,
    _safe_filename,
    _utcnow_iso,
    _write_jsonl,
)
from researchclaw.pipeline.support.context import (
    _build_fallback_queries,
    _chat_with_prompt,
    _get_evolution_overlay,
)
from researchclaw.pipeline.support.domain import (
    _extract_english_from_mixed,
    _extract_keywords,
    _extract_topic_keywords,
)
from researchclaw.pipeline.support.parsing import _extract_yaml_block, _safe_json_loads
from researchclaw.prompts import PromptManager

logger = logging.getLogger(__name__)


def _execute_search_strategy(
    stage_dir: Path,
    run_dir: Path,
    config: RCConfig,
    adapters: AdapterBundle,
    *,
    llm: LLMClient | None = None,
    prompts: PromptManager | None = None,
) -> StageResult:
    problem_tree = _read_prior_artifact(run_dir, "problem_tree.md") or ""
    topic = config.research.topic
    plan: dict[str, Any] | None = None
    sources: list[dict[str, Any]] | None = None
    if llm is not None:
        _pm = prompts or PromptManager()
        _overlay = _get_evolution_overlay(run_dir, "search_strategy")
        sp = _pm.for_stage("search_strategy", evolution_overlay=_overlay, topic=topic, problem_tree=problem_tree)
        resp = _chat_with_prompt(
            llm,
            sp.system,
            sp.user,
            json_mode=sp.json_mode,
            max_tokens=sp.max_tokens,
        )
        payload = _safe_json_loads(resp.content, {})
        if isinstance(payload, dict):
            yaml_text = str(payload.get("search_plan_yaml", "")).strip()
            if yaml_text:
                try:
                    parsed = yaml.safe_load(_extract_yaml_block(yaml_text))
                except yaml.YAMLError:
                    parsed = None
                if isinstance(parsed, dict):
                    plan = parsed
            src = payload.get("sources", [])
            if isinstance(src, list):
                sources = [item for item in src if isinstance(item, dict)]
    if plan is None:
        # Build smart fallback queries by extracting key terms from topic
        # instead of using the raw (often very long) topic string.
        _fallback_queries = _build_fallback_queries(topic)
        plan = {
            "topic": topic,
            "generated": _utcnow_iso(),
            "search_strategies": [
                {
                    "name": "keyword_core",
                    "queries": _fallback_queries[:5],
                    "sources": ["arxiv", "semantic_scholar", "openreview"],
                    "max_results_per_query": 60,
                },
                {
                    "name": "backward_forward_citation",
                    "queries": _fallback_queries[5:10] or _fallback_queries[:3],
                    "sources": ["semantic_scholar", "google_scholar"],
                    "depth": 1,
                },
            ],
            "filters": {
                "min_year": 2020,
                "language": ["en"],
                "peer_review_preferred": True,
            },
            "deduplication": {"method": "title_doi_hash", "fuzzy_threshold": 0.9},
        }
    if not sources:
        sources = [
            {
                "id": "arxiv",
                "name": "arXiv",
                "type": "api",
                "url": "https://export.arxiv.org/api/query",
                "status": "available",
                "query": topic,
                "verified_at": _utcnow_iso(),
            },
            {
                "id": "semantic_scholar",
                "name": "Semantic Scholar",
                "type": "api",
                "url": "https://api.semanticscholar.org/graph/v1/paper/search",
                "status": "available",
                "query": topic,
                "verified_at": _utcnow_iso(),
            },
        ]
    if config.openclaw_bridge.use_web_fetch:
        for src in sources:
            try:
                response = adapters.web_fetch.fetch(str(src.get("url", "")))
                src["status"] = (
                    "verified"
                    if response.status_code in (200, 301, 302, 405)
                    else "unreachable"
                )
                src["http_status"] = response.status_code
            except Exception:  # noqa: BLE001
                src["status"] = "unknown"
    (stage_dir / "search_plan.yaml").write_text(
        yaml.dump(plan, default_flow_style=False, allow_unicode=True),
        encoding="utf-8",
    )
    (stage_dir / "sources.json").write_text(
        json.dumps(
            {"sources": sources, "count": len(sources), "generated": _utcnow_iso()},
            indent=2,
        ),
        encoding="utf-8",
    )

    # F1.5: Extract queries from plan for Stage 4 real literature search
    queries_list: list[str] = []
    year_min = 2020
    if isinstance(plan, dict):
        strategies = plan.get("search_strategies", [])
        if isinstance(strategies, list):
            for strat in strategies:
                if isinstance(strat, dict):
                    qs = strat.get("queries", [])
                    if isinstance(qs, list):
                        queries_list.extend(str(q) for q in qs if q)
        filters = plan.get("filters", {})
        if isinstance(filters, dict) and filters.get("min_year"):
            try:
                year_min = int(filters["min_year"])
            except (ValueError, TypeError):
                pass

    # --- Sanitize queries: shorten overly long queries ---
    # LLMs often produce the full topic title as a query, which is too long for
    # arXiv and Semantic Scholar (they work best with 3-8 keyword queries).

    _MAX_QUERY_LEN = 60  # characters — beyond this, shorten to keywords
    _SEARCH_SUFFIXES = ["benchmark", "survey", "seminal", "state of the art"]

    def _shorten_query(q: str, max_kw: int = 6) -> str:
        """Shorten a query to *max_kw* keywords, preserving any trailing suffix."""
        q_stripped = q.strip()
        # Check if query ends with a known search suffix
        suffix = ""
        q_core = q_stripped
        for sfx in _SEARCH_SUFFIXES:
            if q_stripped.lower().endswith(sfx):
                suffix = sfx
                q_core = q_stripped[: -len(sfx)].strip()
                break
        # Extract keywords from the core part
        kws = _extract_keywords(q_core)
        shortened = " ".join(kws[:max_kw])
        if suffix:
            shortened = f"{shortened} {suffix}"
        return shortened

    if queries_list:
        sanitized: list[str] = []
        for q in queries_list:
            if len(q) > _MAX_QUERY_LEN:
                shortened = _shorten_query(q)
                if shortened.strip():
                    sanitized.append(shortened)
            else:
                sanitized.append(q)
        queries_list = sanitized

    if not queries_list:
        # Build diverse keyword queries from the topic
        _words = _extract_keywords(topic)
        kw_primary = " ".join(_words[:6])
        kw_short = " ".join(_words[:4])
        queries_list = [
            kw_primary,
            f"{kw_short} benchmark",
            f"{kw_short} survey",
        ]

    # Ensure minimum query diversity — if dedup leaves too few, add variants
    _all_kw = _extract_keywords(topic)
    _seen_q: set[str] = set()
    unique_queries: list[str] = []
    for q in queries_list:
        q_lower = q.strip().lower()
        if q_lower and q_lower not in _seen_q:
            _seen_q.add(q_lower)
            unique_queries.append(q.strip())
    # If we have fewer than 5 unique queries, generate supplemental keyword variants
    if len(unique_queries) < 5 and len(_all_kw) >= 3:
        supplements = [
            " ".join(_all_kw[:4]) + " survey",
            " ".join(_all_kw[:4]) + " benchmark",
            " ".join(_all_kw[1:5]),  # shifted window for diversity
            " ".join(_all_kw[:3]) + " comparison",
            " ".join(_all_kw[:3]) + " deep learning",
            " ".join(_all_kw[2:6]),  # another shifted window
        ]
        for s in supplements:
            s_lower = s.strip().lower()
            if s_lower not in _seen_q:
                _seen_q.add(s_lower)
                unique_queries.append(s.strip())
            if len(unique_queries) >= 8:
                break
    queries_list = unique_queries
    (stage_dir / "queries.json").write_text(
        json.dumps({"queries": queries_list, "year_min": year_min}, indent=2),
        encoding="utf-8",
    )
    return StageResult(
        stage=Stage.SEARCH_STRATEGY,
        status=StageStatus.DONE,
        artifacts=("search_plan.yaml", "sources.json", "queries.json"),
        evidence_refs=(
            "stage-03/search_plan.yaml",
            "stage-03/sources.json",
            "stage-03/queries.json",
        ),
    )


def _expand_search_queries(queries: list[str], topic: str) -> list[str]:
    """Expand search queries for broader literature coverage.

    Generates additional queries by extracting key phrases from the topic
    and creating focused sub-queries. This ensures we find papers even when
    the original queries are too narrow or specific for arXiv.
    """
    expanded = list(queries)  # keep originals
    seen = {q.lower().strip() for q in queries}

    # Extract key phrases from topic by splitting on common delimiters
    # e.g. "Comparing A, B, and C on X with Y" → ["A", "B", "C", "X", "Y"]
    topic_words = topic.split()

    # Generate shorter, broader queries from the topic
    if len(topic_words) > 5:
        # First 5 words as a broader query
        broad = " ".join(topic_words[:5])
        if broad.lower().strip() not in seen:
            expanded.append(broad)
            seen.add(broad.lower().strip())

        # Last 5 words as another perspective
        tail = " ".join(topic_words[-5:])
        if tail.lower().strip() not in seen:
            expanded.append(tail)
            seen.add(tail.lower().strip())

    # Add "survey" and "benchmark" variants of the topic
    for suffix in ("survey", "benchmark", "comparison"):
        # Take first 4 content words + suffix
        short_topic = " ".join(topic_words[:4])
        variant = f"{short_topic} {suffix}"
        if variant.lower().strip() not in seen:
            expanded.append(variant)
            seen.add(variant.lower().strip())

    return expanded[:6]


def _literature_url_reachable(url: str, timeout: float) -> bool:
    """Probe a literature URL directly, then through environment proxies.

    Internal deployments often export a generic HTTP proxy that is much
    slower for public scholarly APIs. A short proxy-only preflight used to
    mark every source down even when direct requests completed immediately.
    HTTP 4xx still proves that the service is reachable (for example the
    unauthenticated Semantic Scholar endpoint commonly responds with 429).
    """
    import urllib.error
    import urllib.request

    request = urllib.request.Request(url, headers={"User-Agent": "ResearchClaw/0.3"})
    openers = (
        urllib.request.build_opener(urllib.request.ProxyHandler({})),
        urllib.request.build_opener(),
    )
    for opener in openers:
        try:
            with opener.open(request, timeout=timeout) as response:
                return response.status < 500
        except urllib.error.HTTPError as exc:
            return exc.code < 500
        except Exception:
            continue
    return False


def _check_literature_sites_reachable(timeout: float = 4.0) -> dict[str, bool]:
    """Quick connectivity check for literature API endpoints (parallel)."""
    import urllib.request
    from concurrent.futures import ThreadPoolExecutor, as_completed

    sites = {
        "openalex": "https://api.openalex.org/works?filter=title.search:test&per_page=1",
        "semantic_scholar": "https://api.semanticscholar.org/graph/v1/paper/search?query=test&limit=1",
        "arxiv": "https://export.arxiv.org/api/query?search_query=test&max_results=1",
    }

    def _probe(name: str, url: str) -> tuple[str, bool]:
        return name, _literature_url_reachable(url, timeout)

    results: dict[str, bool] = {}
    with ThreadPoolExecutor(max_workers=len(sites)) as pool:
        futures = {pool.submit(_probe, n, u): n for n, u in sites.items()}
        for fut in as_completed(futures, timeout=timeout * 2 + 2):
            try:
                name, ok = fut.result()
                results[name] = ok
            except Exception:
                results[futures[fut]] = False
    for name in sites:
        results.setdefault(name, False)
    return results


def _build_local_pdf_reference_entry(
    pdf_path: Path,
    *,
    title: str,
    abstract: str,
    source: str,
) -> dict[str, Any]:
    clean_title = title.strip() or pdf_path.stem
    clean_abstract = re.sub(r"\s+", " ", abstract).strip()[:1200]
    return {
        "id": f"user-ref-{_safe_filename(pdf_path.stem.lower())}",
        "title": clean_title,
        "source": source,
        "url": pdf_path.resolve().as_uri(),
        "year": datetime.now(timezone.utc).year,
        "abstract": clean_abstract,
        "authors": [],
        "collected_at": _utcnow_iso(),
    }


def _extract_full_pdf_text(pdf_path: Path, max_chars: int = 80_000) -> str:
    """Extract full text from a local PDF for downstream stages (S9, S11).

    Uses the PDFExtractor utility which reads all pages.  Falls back to
    the simpler fitz/pypdf page loop if the extractor is unavailable.
    Returns empty string on failure.
    """
    try:
        from researchclaw.web.pdf_extractor import PDFExtractor
        result = PDFExtractor(max_pages=0, extract_sections=False).extract(pdf_path)
        if result.success and result.text:
            return result.text[:max_chars]
    except Exception:  # noqa: BLE001
        pass

    try:
        import fitz
        doc = fitz.open(pdf_path)
        try:
            pages = []
            for idx in range(doc.page_count):
                text = doc.load_page(idx).get_text("text")
                if text:
                    pages.append(text)
            return "\n".join(pages)[:max_chars]
        finally:
            doc.close()
    except Exception:  # noqa: BLE001
        pass

    return ""


def _extract_local_pdf_reference(pdf_path: Path) -> dict[str, Any]:
    title = pdf_path.stem
    abstract = ""
    parser_errors: list[str] = []

    try:
        import fitz

        doc = fitz.open(pdf_path)
        try:
            metadata = getattr(doc, "metadata", {}) or {}
            title = str(metadata.get("title") or title).strip() or title
            snippets: list[str] = []
            page_count = int(getattr(doc, "page_count", 0) or 0)
            for idx in range(min(page_count, 3)):
                page = doc.load_page(idx)
                text = page.get_text("text") if hasattr(page, "get_text") else ""
                text = re.sub(r"\s+", " ", text or "").strip()
                if text:
                    snippets.append(text)
            abstract = " ".join(snippets)
        finally:
            doc.close()
        return _build_local_pdf_reference_entry(
            pdf_path,
            title=title,
            abstract=abstract,
            source="user_reference_local_pdf",
        )
    except Exception as exc:  # noqa: BLE001
        parser_errors.append(f"PyMuPDF: {exc}")

    try:
        from pypdf import PdfReader

        reader = PdfReader(str(pdf_path))
        metadata = getattr(reader, "metadata", None) or {}
        raw_title = getattr(metadata, "title", None)
        if not raw_title and isinstance(metadata, dict):
            raw_title = metadata.get("/Title")
        title = str(raw_title or title).strip() or title
        snippets = []
        for page in reader.pages[:3]:
            text = re.sub(r"\s+", " ", page.extract_text() or "").strip()
            if text:
                snippets.append(text)
        abstract = " ".join(snippets)
        return _build_local_pdf_reference_entry(
            pdf_path,
            title=title,
            abstract=abstract,
            source="user_reference_local_pdf",
        )
    except Exception as exc:  # noqa: BLE001
        parser_errors.append(f"pypdf: {exc}")

    logger.warning(
        "Failed to parse local PDF reference %s; falling back to weak entry (%s)",
        pdf_path,
        "; ".join(parser_errors) or "unknown parser error",
    )
    return _build_local_pdf_reference_entry(
        pdf_path,
        title=pdf_path.stem,
        abstract="",
        source="user_reference_local_pdf_parse_failed",
    )


def _resolve_user_reference(ref: str) -> dict | None:
    """Resolve a user-provided reference string to a candidate dict.

    Accepts arXiv IDs, arXiv URLs, local PDF paths, or plain titles.
    Attempts arXiv API lookup first, then local PDF parsing, then falls back to
    a title-only entry.
    """
    import re as _re_ref

    normalized_ref = ref.strip()
    if not normalized_ref:
        return None

    arxiv_id = ""
    m = _re_ref.search(r"(\d{4}\.\d{4,5})", normalized_ref)
    if m:
        arxiv_id = m.group(1)

    if arxiv_id:
        try:
            from researchclaw.literature.arxiv_client import get_paper_by_id
            paper = get_paper_by_id(arxiv_id)
            if paper:
                return {
                    "id": f"user-ref-{paper.paper_id}",
                    "title": paper.title,
                    "source": "user_reference",
                    "url": paper.url,
                    "year": paper.year,
                    "abstract": paper.abstract[:500] if paper.abstract else "",
                    "authors": [{"name": a.name} for a in paper.authors],
                    "arxiv_id": arxiv_id,
                    "venue": paper.venue,
                    "collected_at": _utcnow_iso(),
                }
        except Exception:  # noqa: BLE001
            pass

    pdf_path = Path(normalized_ref).expanduser()
    if pdf_path.is_file() and pdf_path.suffix.lower() == ".pdf":
        return _extract_local_pdf_reference(pdf_path)

    title = normalized_ref
    if title.startswith("http"):
        title = title.rsplit("/", 1)[-1]
    return {
        "id": f"user-ref-{title[:40].replace(' ', '-').lower()}",
        "title": title,
        "source": "user_reference",
        "url": normalized_ref if normalized_ref.startswith("http") else "",
        "year": 2025,
        "abstract": "",
        "authors": [],
        "collected_at": _utcnow_iso(),
    }


def _execute_literature_collect(
    stage_dir: Path,
    run_dir: Path,
    config: RCConfig,
    adapters: AdapterBundle,
    *,
    llm: LLMClient | None = None,
    prompts: PromptManager | None = None,
) -> StageResult:
    """Stage 4: Collect literature — prefer real APIs, fallback to LLM."""
    topic = config.research.topic
    paper_source_mode = str(getattr(config.research, "paper_source_mode", "hybrid") or "hybrid").strip().lower()
    use_external_search = paper_source_mode in {"auto", "hybrid"}
    use_reference_inputs = paper_source_mode in {"upload", "hybrid"}
    artifacts: list[str] = ["candidates.jsonl"]

    # Pre-flight: check if any literature site is reachable
    reachable: list[str] = []
    unreachable: list[str] = []
    if use_external_search:
        site_status = _check_literature_sites_reachable(timeout=4.0)
        reachable = [name for name, ok in site_status.items() if ok]
        unreachable = [name for name, ok in site_status.items() if not ok]
        if unreachable:
            logger.warning(
                "Literature sites unreachable (will be skipped): %s — reachable: %s",
                ", ".join(unreachable), ", ".join(reachable) or "none",
            )
        if not reachable:
            logger.warning(
                "ALL literature sites unreachable (%s) — continuing with references/fallbacks only",
                ", ".join(unreachable),
            )
    else:
        logger.info(
            "Stage 4: paper_source_mode=%s — skipping external literature search",
            paper_source_mode,
        )

    # Read queries.json from Stage 3 (F1.5 output)
    queries_text = _read_prior_artifact(run_dir, "queries.json")
    queries_data = _safe_json_loads(queries_text or "{}", {})
    queries: list[str] = queries_data.get("queries", [topic])
    year_min: int = queries_data.get("year_min", 2020)

    # --- Try real API search first ---
    candidates: list[dict[str, Any]] = []
    bibtex_entries: list[str] = []
    real_search_succeeded = False

    _SEARCH_TIMEOUT_SEC = 60  # hard cap on API search phase

    try:
        from concurrent.futures import ThreadPoolExecutor, TimeoutError as FuturesTimeout
        from researchclaw.literature.search import (
            search_papers_multi_query,
            papers_to_bibtex,
        )

        # Expand queries for broader coverage
        expanded_queries = _expand_search_queries(queries, config.research.topic)
        reachable_sources = tuple(reachable) if reachable else ("openalex", "semantic_scholar", "arxiv")
        if use_external_search and reachable:
            logger.info(
                "[literature] Searching %d queries (expanded from %d) "
                "across %s (skipped unreachable: %s) — timeout %ds",
                len(expanded_queries),
                len(queries),
                " → ".join(reachable_sources),
                ", ".join(unreachable) if unreachable else "none",
                _SEARCH_TIMEOUT_SEC,
            )

        papers = []
        if use_external_search and reachable:
            with ThreadPoolExecutor(max_workers=1) as _pool:
                _fut = _pool.submit(
                    search_papers_multi_query,
                    expanded_queries,
                    limit_per_query=40,
                    sources=reachable_sources,
                    year_min=year_min,
                    s2_api_key=config.llm.s2_api_key,
                )
                try:
                    papers = _fut.result(timeout=_SEARCH_TIMEOUT_SEC)
                except FuturesTimeout:
                    logger.warning(
                        "[literature] API search timed out after %ds — skipping",
                        _SEARCH_TIMEOUT_SEC,
                    )
                    _fut.cancel()
                    papers = []

        if papers:
            real_search_succeeded = True
            src_counts: dict[str, int] = {}
            for p in papers:
                src_counts[p.source] = src_counts.get(p.source, 0) + 1
                d = p.to_dict()
                d["collected_at"] = _utcnow_iso()
                candidates.append(d)
                bibtex_entries.append(p.to_bibtex())
            src_str = ", ".join(f"{s}: {n}" for s, n in src_counts.items())
            logger.info(
                "[literature] Found %d papers (%s)", len(papers), src_str
            )
    except Exception:  # noqa: BLE001
        logger.warning(
            "[rate-limit] Literature search failed — falling back to LLM",
            exc_info=True,
        )

    # --- Inject foundational/seminal papers ---
    if paper_source_mode != "upload":
        try:
            from researchclaw.data import load_seminal_papers
            seminal = load_seminal_papers(topic)
            if seminal:
                _existing_titles = {c.get("title", "").lower() for c in candidates}
                _injected = 0
                for sp in seminal:
                    if sp.get("title", "").lower() not in _existing_titles:
                        candidates.append({
                            "id": f"seminal-{sp.get('cite_key', '')}",
                            "title": sp.get("title", ""),
                            "source": "seminal_library",
                            "url": "",
                            "year": sp.get("year", 2020),
                            "abstract": f"Foundational paper on {', '.join(sp.get('keywords', [])[:3])}.",
                            "authors": [{"name": sp.get("authors", "")}],
                            "cite_key": sp.get("cite_key", ""),
                            "venue": sp.get("venue", ""),
                            "collected_at": _utcnow_iso(),
                        })
                        _injected += 1
                if _injected:
                    logger.info("Stage 4: Injected %d seminal papers from seed library", _injected)
        except Exception:  # noqa: BLE001
            logger.debug("Seminal paper injection skipped", exc_info=True)

    # --- Inject user-provided reference papers ---
    _user_refs = config.research.reference_papers if use_reference_inputs else ()
    if _user_refs:
        _existing_titles = {c.get("title", "").lower() for c in candidates}
        _ref_injected = 0
        _ref_full_texts: list[str] = []
        for ref_str in _user_refs:
            ref_str = ref_str.strip()
            if not ref_str:
                continue
            _resolved = _resolve_user_reference(ref_str)
            if _resolved and _resolved.get("title", "").lower() not in _existing_titles:
                _existing_titles.add(_resolved["title"].lower())
                candidates.append(_resolved)
                _ref_injected += 1

            # Extract full text for local PDFs (used by S9 experiment design)
            _pdf_path = Path(ref_str).expanduser()
            if _pdf_path.is_file() and _pdf_path.suffix.lower() == ".pdf":
                _full_text = _extract_full_pdf_text(_pdf_path)
                if _full_text:
                    _ref_title = (_resolved or {}).get("title", _pdf_path.stem)
                    _ref_full_texts.append(
                        f"# {_ref_title}\n\n{_full_text}"
                    )

        if _ref_full_texts:
            _combined = "\n\n---\n\n".join(_ref_full_texts)
            (stage_dir / "reference_paper_text.md").write_text(
                _combined, encoding="utf-8",
            )
            artifacts.append("reference_paper_text.md")
            logger.info(
                "Stage 4: Extracted full text from %d reference PDF(s) "
                "(%d chars total)",
                len(_ref_full_texts), len(_combined),
            )

        if _ref_injected:
            logger.info("Stage 4: Injected %d user-provided reference papers", _ref_injected)

    # --- arXiv recent papers auto-discovery (30s timeout) ---
    _ARXIV_DISC_TIMEOUT = 30
    if use_external_search:
        try:
            from concurrent.futures import ThreadPoolExecutor, TimeoutError as FuturesTimeout
            from researchclaw.literature.arxiv_client import search_arxiv as _arxiv_search
            _topic_kw = _extract_keywords(topic)
            if _topic_kw:
                _arxiv_query = " AND ".join(
                    f"all:{w}" for w in _topic_kw[:5]
                )

                def _do_arxiv_disc() -> list:
                    return _arxiv_search(
                        _arxiv_query, limit=30, sort_by="submitted_date",
                        year_min=2025,
                    )

                with ThreadPoolExecutor(max_workers=1) as _pool:
                    _fut = _pool.submit(_do_arxiv_disc)
                    try:
                        _recent = _fut.result(timeout=_ARXIV_DISC_TIMEOUT)
                    except FuturesTimeout:
                        logger.warning(
                            "arXiv recent discovery timed out after %ds — skipping",
                            _ARXIV_DISC_TIMEOUT,
                        )
                        _recent = []

                _existing_titles_lower = {c.get("title", "").lower() for c in candidates}
                _disc = 0
                for p in _recent:
                    if p.title.lower() not in _existing_titles_lower:
                        _existing_titles_lower.add(p.title.lower())
                        candidates.append({
                            "id": p.paper_id,
                            "title": p.title,
                            "source": "arxiv_recent_discovery",
                            "url": p.url,
                            "year": p.year,
                            "abstract": p.abstract[:500] if p.abstract else "",
                            "authors": [{"name": a.name} for a in p.authors],
                            "arxiv_id": p.arxiv_id,
                            "venue": p.venue,
                            "collected_at": _utcnow_iso(),
                        })
                        _disc += 1
                if _disc:
                    logger.info("Stage 4: Discovered %d recent arXiv papers (2025+)", _disc)
        except Exception:  # noqa: BLE001
            logger.debug("arXiv recent discovery skipped", exc_info=True)

    # --- Fallback: LLM-generated candidates ---
    if not candidates and llm is not None and use_external_search:
        plan_text = _read_prior_artifact(run_dir, "search_plan.yaml") or ""
        _pm = prompts or PromptManager()
        _overlay = _get_evolution_overlay(run_dir, "literature_collect")
        sp = _pm.for_stage("literature_collect", evolution_overlay=_overlay, topic=topic, plan_text=plan_text)
        resp = _chat_with_prompt(
            llm,
            sp.system,
            sp.user,
            json_mode=sp.json_mode,
            max_tokens=sp.max_tokens,
        )
        payload = _safe_json_loads(resp.content, {})
        if isinstance(payload, dict) and isinstance(payload.get("candidates"), list):
            candidates = [row for row in payload["candidates"] if isinstance(row, dict)]
            for row in candidates:
                row["source"] = "llm_unverified_candidate"
                row["is_unverified"] = True

    # --- Web search augmentation (Tavily/DDG + Google Scholar + Crawl4AI) — 60s timeout ---
    _WEB_SEARCH_TIMEOUT = 60
    web_context_parts: list[str] = []
    if config.web_search.enabled and use_external_search:
        try:
            from concurrent.futures import ThreadPoolExecutor, TimeoutError as FuturesTimeout
            from researchclaw.web.agent import WebSearchAgent
            import os

            tavily_key = config.web_search.tavily_api_key or os.environ.get(
                config.web_search.tavily_api_key_env, ""
            )
            exa_key = config.web_search.exa_api_key or os.environ.get(
                config.web_search.exa_api_key_env, ""
            )
            web_agent = WebSearchAgent(
                tavily_api_key=tavily_key,
                exa_api_key=exa_key,
                enable_scholar=config.web_search.enable_scholar,
                enable_crawling=config.web_search.enable_crawling,
                enable_pdf=config.web_search.enable_pdf_extraction,
                max_web_results=config.web_search.max_web_results,
                max_scholar_results=config.web_search.max_scholar_results,
                max_crawl_urls=config.web_search.max_crawl_urls,
            )

            with ThreadPoolExecutor(max_workers=1) as _pool:
                _fut = _pool.submit(
                    web_agent.search_and_extract,
                    topic, search_queries=queries,
                )
                try:
                    web_result = _fut.result(timeout=_WEB_SEARCH_TIMEOUT)
                except FuturesTimeout:
                    logger.warning(
                        "[web-search] Timed out after %ds — skipping",
                        _WEB_SEARCH_TIMEOUT,
                    )
                    web_result = None

            if web_result is None:
                raise RuntimeError("web search timed out")

            # Convert Google Scholar papers into candidates
            for sp in web_result.scholar_papers:
                _existing_titles = {
                    str(c.get("title", "")).lower().strip() for c in candidates
                }
                if sp.title.lower().strip() not in _existing_titles:
                    lit_paper = sp.to_literature_paper()
                    d = lit_paper.to_dict()
                    d["collected_at"] = _utcnow_iso()
                    candidates.append(d)
                    bibtex_entries.append(lit_paper.to_bibtex())

            # Save web search context for downstream stages
            web_context = web_result.to_context_string(max_length=20_000)
            if web_context.strip():
                (stage_dir / "web_context.md").write_text(
                    web_context, encoding="utf-8"
                )
                web_context_parts.append(web_context)

            # Save full web search metadata
            (stage_dir / "web_search_result.json").write_text(
                json.dumps(web_result.to_dict(), indent=2, default=str),
                encoding="utf-8",
            )

            logger.info(
                "[web-search] Added %d scholar papers, %d web results, %d crawled pages",
                len(web_result.scholar_papers),
                len(web_result.web_results),
                len(web_result.crawled_pages),
            )
        except Exception:  # noqa: BLE001
            logger.warning(
                "[web-search] Web search augmentation failed — continuing with academic APIs only",
                exc_info=True,
            )

    # Never fabricate placeholder papers. An evidence-free literature stage
    # must fail visibly instead of feeding fake citations into idea generation.
    real_candidates = [
        row for row in candidates
        if not row.get("is_placeholder") and not row.get("is_unverified")
        and str(row.get("source", "")) != "llm_unverified_candidate"
    ]
    real_search_succeeded = bool(real_candidates)
    if not candidates:
        logger.error("Stage 4: no usable literature candidates; refusing placeholder generation")

    # Write candidates
    out = stage_dir / "candidates.jsonl"
    _write_jsonl(out, candidates)

    # BUG-50 fix: Generate BibTeX from candidates when real search failed
    # (LLM/placeholder fallback paths don't populate bibtex_entries)
    if not bibtex_entries and candidates:
        for c in candidates:
            if c.get("is_placeholder") or c.get("is_unverified"):
                continue
            _ck = c.get("cite_key", "")
            if not _ck:
                # Derive cite_key from first author surname + year
                _authors = c.get("authors", [])
                _surname = "unknown"
                if isinstance(_authors, list) and _authors:
                    _a0 = _authors[0] if isinstance(_authors[0], str) else (_authors[0].get("name", "") if isinstance(_authors[0], dict) else "")
                    _surname = _a0.split()[-1].lower() if _a0.strip() else "unknown"
                _yr = c.get("year", 2024)
                _title_word = "".join(
                    w[0] for w in str(c.get("title", "study")).split()[:3]
                ).lower()
                _ck = f"{_surname}{_yr}{_title_word}"
            _title = c.get("title", "Untitled")
            _year = c.get("year", 2024)
            _author_str = ""
            _raw_authors = c.get("authors", [])
            if isinstance(_raw_authors, list):
                _names = []
                for _a in _raw_authors:
                    if isinstance(_a, str):
                        _names.append(_a)
                    elif isinstance(_a, dict):
                        _names.append(_a.get("name", ""))
                _author_str = " and ".join(n for n in _names if n)
            bibtex_entries.append(
                f"@article{{{_ck},\n"
                f"  title={{{_title}}},\n"
                f"  author={{{_author_str or 'Unknown'}}},\n"
                f"  year={{{_year}}},\n"
                f"  url={{{c.get('url', '')}}},\n"
                f"}}"
            )
        logger.info(
            "Stage 4: Generated %d BibTeX entries from candidates (fallback)",
            len(bibtex_entries),
        )

    # Write references.bib (F2.4)
    if web_context_parts:
        artifacts.append("web_context.md")
    if (stage_dir / "web_search_result.json").exists():
        artifacts.append("web_search_result.json")
    if bibtex_entries:
        bib_content = "\n\n".join(bibtex_entries) + "\n"
        (stage_dir / "references.bib").write_text(bib_content, encoding="utf-8")
        artifacts.append("references.bib")
        logger.info(
            "Stage 4: Wrote %d BibTeX entries to references.bib", len(bibtex_entries)
        )

    # Write search metadata
    _minimum_real = 1 if paper_source_mode == "upload" else 5
    _coverage_status = (
        "sufficient" if len(real_candidates) >= _minimum_real
        else "insufficient" if candidates else "empty"
    )
    (stage_dir / "search_meta.json").write_text(
        json.dumps(
            {
                "real_search": real_search_succeeded,
                "queries_used": queries,
                "year_min": year_min,
                "total_candidates": len(candidates),
                "real_candidate_count": len(real_candidates),
                "unverified_candidate_count": len(candidates) - len(real_candidates),
                "minimum_real_candidates": _minimum_real,
                "coverage_status": _coverage_status,
                "bibtex_entries": len(bibtex_entries),
                "ts": _utcnow_iso(),
            },
            indent=2,
        ),
        encoding="utf-8",
    )
    artifacts.append("search_meta.json")

    if not candidates:
        return StageResult(
            stage=Stage.LITERATURE_COLLECT,
            status=StageStatus.FAILED,
            artifacts=tuple(artifacts),
            evidence_refs=tuple(f"stage-04/{a}" for a in artifacts),
            error="No usable literature was collected; placeholder papers are forbidden.",
        )

    return StageResult(
        stage=Stage.LITERATURE_COLLECT,
        status=StageStatus.DONE,
        artifacts=tuple(artifacts),
        evidence_refs=tuple(f"stage-04/{a}" for a in artifacts),
        decision="degraded" if _coverage_status != "sufficient" else None,
    )


def _execute_literature_screen(
    stage_dir: Path,
    run_dir: Path,
    config: RCConfig,
    adapters: AdapterBundle,
    *,
    llm: LLMClient | None = None,
    prompts: PromptManager | None = None,
) -> StageResult:
    candidates_text = _read_prior_artifact(run_dir, "candidates.jsonl") or ""

    if not candidates_text.strip():
        logger.error("Stage 5: No candidates from literature collection")
        (stage_dir / "shortlist.jsonl").write_text("", encoding="utf-8")
        return StageResult(
            stage=Stage.LITERATURE_SCREEN,
            status=StageStatus.FAILED,
            artifacts=("shortlist.jsonl",),
            evidence_refs=(),
            error="No literature candidates are available for screening.",
        )

    # --- P1-1: keyword relevance pre-filter ---
    # Before LLM screening, drop papers whose title+abstract share no keywords
    # with the research topic.  This catches cross-domain noise cheaply.
    topic_keywords = _extract_topic_keywords(
        config.research.topic, config.research.domains
    )
    filtered_rows: list[dict[str, Any]] = []
    dropped_count = 0
    for raw_line in candidates_text.strip().splitlines():
        row = _safe_json_loads(raw_line, {})
        if not isinstance(row, dict):
            continue
        title = str(row.get("title", "")).lower()
        abstract = str(row.get("abstract", "")).lower()
        text_blob = f"{title} {abstract}"
        overlap = sum(1 for kw in topic_keywords if kw in text_blob)
        # T2.2: Relaxed from ≥2 to ≥1 keyword hit — previous threshold was
        # too aggressive (94% rejection rate).  Single-keyword matches are
        # still screened by the LLM in the next step.
        if overlap >= 1:
            row["keyword_overlap"] = overlap
            filtered_rows.append(row)
        else:
            dropped_count += 1
    # If pre-filter dropped everything, fall back to original (safety valve)
    if not filtered_rows:
        filtered_rows = _parse_jsonl_rows(candidates_text)
    # Rebuild candidates_text from filtered rows
    candidates_text = "\n".join(
        json.dumps(r, ensure_ascii=False) for r in filtered_rows
    )
    logger.info(
        "Domain pre-filter: kept %d, dropped %d (keywords: %s)",
        len(filtered_rows),
        dropped_count,
        topic_keywords[:8],
    )

    shortlist: list[dict[str, Any]] = []
    if llm is not None:
        _pm = prompts or PromptManager()
        _overlay = _get_evolution_overlay(run_dir, "literature_screen")
        sp = _pm.for_stage(
            "literature_screen",
            evolution_overlay=_overlay,
            topic=config.research.topic,
            domains=", ".join(config.research.domains)
            if config.research.domains
            else "general",
            quality_threshold=config.research.quality_threshold,
            candidates_text=candidates_text,
        )
        resp = _chat_with_prompt(
            llm,
            sp.system,
            sp.user,
            json_mode=sp.json_mode,
            max_tokens=sp.max_tokens,
        )
        payload = _safe_json_loads(resp.content, {})
        if isinstance(payload, dict) and isinstance(payload.get("shortlist"), list):
            shortlist = [row for row in payload["shortlist"] if isinstance(row, dict)]
    _MIN_SHORTLIST = 15
    if not shortlist:
        pool = filtered_rows if filtered_rows else _parse_jsonl_rows(candidates_text)
        pool_sorted = sorted(
            pool,
            key=lambda r: (
                r.get("keyword_overlap", 0),
                1 if r.get("source") == "seminal_library" else 0,
            ),
            reverse=True,
        )
        rows = pool_sorted[:_MIN_SHORTLIST]
        for idx, item in enumerate(rows):
            item["relevance_score"] = round(0.75 - idx * 0.02, 3)
            item["quality_score"] = round(0.72 - idx * 0.015, 3)
            item["keep_reason"] = "Template screened entry"
            shortlist.append(item)
    elif len(shortlist) < _MIN_SHORTLIST:
        # T2.2: LLM returned too few — supplement from filtered candidates
        existing_titles = {
            str(s.get("title", "")).lower().strip() for s in shortlist
        }
        for row in filtered_rows:
            if len(shortlist) >= _MIN_SHORTLIST:
                break
            title_lower = str(row.get("title", "")).lower().strip()
            if title_lower and title_lower not in existing_titles:
                row.setdefault("relevance_score", 0.5)
                row.setdefault("quality_score", 0.5)
                row.setdefault("keep_reason", "Supplemented to meet minimum shortlist")
                shortlist.append(row)
                existing_titles.add(title_lower)
        logger.info(
            "Stage 5: Supplemented shortlist to %d papers (minimum: %d)",
            len(shortlist), _MIN_SHORTLIST,
        )
    out = stage_dir / "shortlist.jsonl"
    _write_jsonl(out, shortlist)
    return StageResult(
        stage=Stage.LITERATURE_SCREEN,
        status=StageStatus.DONE,
        artifacts=("shortlist.jsonl",),
        evidence_refs=("stage-05/shortlist.jsonl",),
    )


def _execute_knowledge_extract(
    stage_dir: Path,
    run_dir: Path,
    config: RCConfig,
    adapters: AdapterBundle,
    *,
    llm: LLMClient | None = None,
    prompts: PromptManager | None = None,
) -> StageResult:
    shortlist = _read_prior_artifact(run_dir, "shortlist.jsonl") or ""

    # Inject web context from Stage 4 if available
    web_context = _read_prior_artifact(run_dir, "web_context.md") or ""
    if web_context:
        shortlist = shortlist + "\n\n--- Web Search Context ---\n" + web_context[:10_000]

    cards_dir = stage_dir / "cards"
    cards_dir.mkdir(parents=True, exist_ok=True)
    cards: list[dict[str, Any]] = []
    if llm is not None:
        _pm = prompts or PromptManager()
        _overlay = _get_evolution_overlay(run_dir, "knowledge_extract")
        sp = _pm.for_stage("knowledge_extract", evolution_overlay=_overlay, shortlist=shortlist)
        resp = _chat_with_prompt(
            llm,
            sp.system,
            sp.user,
            json_mode=sp.json_mode,
            max_tokens=sp.max_tokens,
        )
        payload = _safe_json_loads(resp.content, {})
        if isinstance(payload, dict) and isinstance(payload.get("cards"), list):
            cards = [item for item in payload["cards"] if isinstance(item, dict)]
    if not cards:
        rows = _parse_jsonl_rows(shortlist)
        for idx, paper in enumerate(rows[:6]):
            title = str(paper.get("title", f"Paper {idx + 1}"))
            cards.append(
                {
                    "card_id": f"card-{idx + 1}",
                    "title": title,
                    "problem": f"How to improve {config.research.topic}",
                    "method": "Template method summary",
                    "data": "Template dataset",
                    "metrics": "Template metric",
                    "findings": "Template key finding",
                    "limitations": "Template limitation",
                    "citation": str(paper.get("url", "")),
                    "cite_key": str(paper.get("cite_key", "")),
                }
            )
    for idx, card in enumerate(cards):
        card_id = _safe_filename(str(card.get("card_id", f"card-{idx + 1}")))
        parts = [f"# {card.get('title', card_id)}", ""]
        for key in (
            "cite_key",
            "problem",
            "method",
            "data",
            "metrics",
            "findings",
            "limitations",
            "citation",
        ):
            parts.append(f"## {key.title()}")
            parts.append(str(card.get(key, "")))
            parts.append("")
        (cards_dir / f"{card_id}.md").write_text("\n".join(parts), encoding="utf-8")
    return StageResult(
        stage=Stage.KNOWLEDGE_EXTRACT,
        status=StageStatus.DONE,
        artifacts=("cards/",),
        evidence_refs=("stage-06/cards/",),
    )


def _execute_synthesis(
    stage_dir: Path,
    run_dir: Path,
    config: RCConfig,
    adapters: AdapterBundle,
    *,
    llm: LLMClient | None = None,
    prompts: PromptManager | None = None,
) -> StageResult:
    cards_path = _read_prior_artifact(run_dir, "cards/") or ""
    cards_context = ""
    if cards_path:
        snippets: list[str] = []
        for path in sorted(Path(cards_path).glob("*.md"))[:24]:
            snippets.append(path.read_text(encoding="utf-8"))
        cards_context = "\n\n".join(snippets)
    if llm is not None:
        _pm = prompts or PromptManager()
        _overlay = _get_evolution_overlay(run_dir, "synthesis")
        sp = _pm.for_stage(
            "synthesis",
            evolution_overlay=_overlay,
            topic=config.research.topic,
            cards_context=cards_context,
        )
        synthesis_user = (
            sp.user
            + "\n\n## 输出语言要求\n"
            + "请使用中文撰写完整综述。保留必要的英文论文名、方法名、数据集名和指标名，但解释、分析、研究缺口和机会判断必须使用中文。"
        )
        synthesis_system = sp.system + "\n必须用中文输出综述正文。"
        resp = llm.chat(
            [{"role": "user", "content": synthesis_user}],
            system=synthesis_system,
            max_tokens=sp.max_tokens or 8192,
        )
        synthesis_md = resp.content
    else:
        synthesis_md = f"""# 综述

## 主题聚类概览
- 聚类 A：表示方法与模型结构
- 聚类 B：训练策略与数据构造
- 聚类 C：评估协议与鲁棒性

## 研究缺口 1
现有工作在 benchmark 协议、随机种子和数据划分上的一致性不足，导致结果可比性偏弱。

## 研究缺口 2
许多方法没有充分报告分布偏移、长尾样本或资源受限条件下的失败行为。

## 优先研究机会
1. 构建统一且可复现的实验协议。
2. 引入鲁棒性导向的评估与优化目标。

## 生成时间
{_utcnow_iso()}
"""
    (stage_dir / "synthesis.md").write_text(synthesis_md, encoding="utf-8")
    return StageResult(
        stage=Stage.SYNTHESIS,
        status=StageStatus.DONE,
        artifacts=("synthesis.md",),
        evidence_refs=("stage-07/synthesis.md",),
    )
