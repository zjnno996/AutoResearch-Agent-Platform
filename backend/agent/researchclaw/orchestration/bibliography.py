"""Bibliographic identity and provenance without inspecting scientific body text.

An article identity groups publisher aliases. Document scope remains separate:
supplements and peer-review files can carry their parent article's DOI, but their
contents must never be relabelled as the main article.
"""
from __future__ import annotations

import re
import urllib.parse
from copy import deepcopy
from typing import Any


_PUBLISHERS = {"nature.com", "link.springer.com", "science.org", "pnas.org", "cell.com",
    "journals.aps.org", "pubs.acs.org", "academic.oup.com", "sciencedirect.com",
    "epubs.siam.org", "biorxiv.org", "medrxiv.org", "iopscience.iop.org", "wiley.com",
    "tandfonline.com", "journals.plos.org", "doi.org"}
_NATURE_ID = r"(?:s\d{4,6}-\d{2,4}-[a-z0-9]+(?:-[a-z0-9]+)*|nature\d+[a-z0-9]*|n[a-z]+\.?\d+(?:[.-]?[a-z0-9]+)*)"
_ARXIV_ID = r"(?:\d{4}\.\d{4,5}|[a-z][a-z.-]*(?:\.[A-Z]{2})?/\d{7})(?:v\d+)?"
_ATTACHMENT = re.compile(r"/(?:bin|suppl_file|suppl|supplement(?:ary)?(?:[-_][^/]*)?|peer[-_]review)(?:/|$)", re.I)


def _host(host: str, names: set[str]) -> bool:
    return any(host == name or host.endswith("." + name) for name in names)


def _unwrap(value: str) -> str:
    # Preserve balanced parentheses inside old-style DOI suffixes.
    value = value.rstrip('"\'')
    while value and any(value.endswith(end) and value.count(end) > value.count(start)
                        for start, end in (("(", ")"), ("[", "]"), ("{", "}"), ("<", ">"))):
        value = value[:-1]
    return value


def _url_tail(value: str) -> str:
    previous = None
    while value != previous:
        previous = value
        value = _unwrap(value.rstrip(".,"))
    return value


def _parts(url: str) -> urllib.parse.SplitResult | None:
    try:
        p = urllib.parse.urlsplit(str(url).strip().lstrip("<"))
        if (p.scheme not in {"http", "https"} or not p.hostname or p.username or p.password
                or p.port not in (None, 443 if p.scheme == "https" else 80)):
            return None
        return p
    except (TypeError, ValueError):
        return None


def _structured_metadata(metadata: dict | None) -> dict:
    if not isinstance(metadata, dict):
        return {}
    nested = metadata.get("document_metadata")
    return nested if isinstance(nested, dict) else metadata


def _doi(value: Any) -> str:
    if not isinstance(value, str):
        return ""
    value = value.strip()
    if value.casefold().startswith("doi:"):
        value = value[4:].strip()
    if value.casefold().startswith(("https://doi.org/", "http://doi.org/", "https://dx.doi.org/", "http://dx.doi.org/")):
        value = urllib.parse.unquote(urllib.parse.urlsplit(value).path.lstrip("/"))
    return value.casefold() if re.fullmatch(r"10\.\d{4,9}/[^\s?#]+", value) else ""


def _url_doi(p: urllib.parse.SplitResult) -> str:
    host, path = (p.hostname or "").casefold(), urllib.parse.unquote(p.path)
    if not _host(host, _PUBLISHERS):
        return ""
    nature = re.fullmatch(r"/articles/(" + _NATURE_ID + r")(?:[.,:;!?]+)?/?", _url_tail(path), re.I)
    if _host(host, {"nature.com"}) and nature:
        return "10.1038/" + nature[1].casefold()
    if host in {"doi.org", "dx.doi.org", "www.doi.org"}:
        suffix = path.lstrip("/")
    else:
        match = re.fullmatch(r"/(?:doi/(?:abs/|full/|pdf/|epdf/|suppl/)?|articles?/|chapter/|"
                             r"content/(?:pdf/|html/)?|[^/]+/(?:abstract|fulltext|pdf)/)(10\.\d{4,9}/.+)", path, re.I)
        if not match:
            return ""
        suffix = match[1]
        attachment = _ATTACHMENT.search(suffix)
        if attachment:
            suffix = suffix[:attachment.start()]
        if path.startswith("/content/pdf/"):
            suffix = suffix.removesuffix(".pdf")
        if _host(host, {"biorxiv.org", "medrxiv.org"}):
            suffix = re.sub(r"(?:v\d+)?(?:\.full(?:\.pdf)?|\.abstract)?$", "", suffix)
    # Nature article IDs have a constrained suffix, unlike arbitrary DOI strings.
    # This safely removes prose ':' without truncating legitimate DOI punctuation.
    nature_doi = re.fullmatch(r"10\.1038/(" + _NATURE_ID + r")[.,:;!?]*", suffix, re.I)
    if nature_doi:
        suffix = "10.1038/" + nature_doi[1]
    return _doi(_url_tail(suffix))


def canonical_article_url(url: str) -> str:
    """Normalize recognized article links while preserving meaningful DOI suffixes."""
    p = _parts(url)
    if p is None:
        return ""
    host, path = (p.hostname or "").casefold(), urllib.parse.unquote(p.path)
    # Fixed article-ID grammars make trailing prose unambiguous.
    pmc = re.match(r"^/articles/(?:instance/)?(?:PMC)?(\d{1,12})(?=/|[.,:;!?)\]}]|$)", path, re.I)
    if host == "pmc.ncbi.nlm.nih.gov" and pmc:
        if not _ATTACHMENT.search(path):
            return "https://pmc.ncbi.nlm.nih.gov/articles/PMC" + pmc[1] + "/"
    arxiv = re.fullmatch(r"/(?:abs|pdf|html)/(" + _ARXIV_ID + r")(?:\.pdf)?[.,:;!?)\]}]*", path)
    if host in {"arxiv.org", "www.arxiv.org", "export.arxiv.org"} and arxiv:
        return "https://arxiv.org/abs/" + arxiv[1]
    nature = re.fullmatch(r"/articles/(" + _NATURE_ID + r")[.,:;!?]*", _url_tail(path), re.I)
    if _host(host, {"nature.com"}) and nature:
        return "https://" + host + "/articles/" + nature[1]
    doi = _url_doi(p)
    if doi:
        cleaned = _url_tail(path)
        # Preserve balanced DOI suffix punctuation. Nature's constrained IDs also
        # permit removing a trailing prose colon without guessing DOI boundaries.
        cleaned = re.sub(r"(10\.1038/" + _NATURE_ID + r")[.,:;!?]+$", r"\1", cleaned, flags=re.I)
        path = cleaned
    encoded = urllib.parse.quote(path, safe="/:@-._~!$&'()*+,;=") if doi else p.path
    return urllib.parse.urlunsplit((p.scheme, host, encoded, p.query, ""))


def article_identity(url: str, metadata: dict | None = None) -> str:
    """Return an article key from exact DOI metadata or a trusted article URL.

    ``metadata`` may be a reference row or its ``document_metadata``. Body text,
    titles, reference lists, query parameters and fragments never supply a DOI.
    """
    exact = _doi(_structured_metadata(metadata).get("doi"))
    if exact:
        return "doi:" + exact
    canonical = canonical_article_url(url)
    p = _parts(canonical)
    if p is None:
        return "source:" + str(url).strip() if str(url).strip() else ""
    doi = _url_doi(p)
    if doi:
        return "doi:" + doi
    host, path = (p.hostname or "").casefold(), urllib.parse.unquote(p.path)
    if host == "arxiv.org":
        match = re.fullmatch(r"/abs/(" + _ARXIV_ID + r")", path)
        if match:
            return "arxiv:" + re.sub(r"v\d+$", "", match[1]).casefold()
    if host == "pmc.ncbi.nlm.nih.gov":
        match = re.match(r"^/articles/(?:instance/)?(?:PMC)?(\d{1,12})(?:/|$)", path, re.I)
        if match:
            return "pmc:PMC" + match[1]
    return "url:" + canonical


def article_scope(url: str, metadata: dict | None = None) -> str:
    """Describe the actual document separately from its parent article identity."""
    row = metadata if isinstance(metadata, dict) else {}
    document = _structured_metadata(metadata)
    labels = " ".join(str(value) for value in (
        row.get("source_scope", ""), row.get("retrieval_status", ""), row.get("title", ""),
        document.get("document_type", ""), document.get("article_type", "")))
    p = _parts(url)
    path = urllib.parse.unquote(p.path).casefold() if p else ""
    if row.get("source_scope") == "review" or re.search(r"peer[ _-]?review|review(?:er)?[ _-](?:letter|report)|editorial correspondence|author response", labels + " " + path, re.I):
        return "review"
    if _ATTACHMENT.search(path) or re.search(r"supplement|supporting information", labels, re.I):
        return "supplement"
    if (row.get("source_scope") == "main" or document.get("included_passages")
            or re.search(r"main[ -]article|article[- ]body", labels, re.I)):
        return "main"
    if p and (_url_doi(p) or (p.hostname or "") in {"arxiv.org", "pmc.ncbi.nlm.nih.gov"}):
        return "main"
    return "unknown"


def article_preference(row: dict[str, Any]) -> tuple[int, int, int, int]:
    """Prefer main-article content over a parent-DOI supplement or review file."""
    scope = article_scope(str(row.get("source", "")), row)
    return ({"main": 3, "unknown": 2, "supplement": 1, "review": 0}[scope],
            {'pmc_jats': 2, 'pmc_bioc': 1}.get(row.get("content_origin"), 0),
            int(row.get("retrieval_status") == "source_content") + int("fetch" in str(row.get("content_origin", ""))),
            len(str(row.get("context", ""))))


def deduplicate_references(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Choose one actual document per article while retaining alias/search audit.

    Main article wins over parent-DOI supplementary documents. Distinct DOI
    supplements remain distinct articles. No document text is spliced or renamed.
    The input rows and their cached files are unchanged.
    """
    groups: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        identity = article_identity(str(row.get("source", "")), row)
        groups.setdefault(identity or "unidentified:" + str(len(groups)), []).append(row)
    result = []

    def values(row: dict, field: str) -> list:
        value = row.get(field)
        return value if isinstance(value, list) else []

    for identity, group in groups.items():
        chosen = max(group, key=article_preference)
        kept = deepcopy(chosen)
        kept["article_identity"] = identity
        kept["canonical_source"] = canonical_article_url(str(chosen.get("source", ""))) or chosen.get("source", "")
        aliases, hits, duplicates = [], [], []
        for row in group:
            for alias in [row.get("source", ""), *values(row, "alias_sources"), *values(row, "source_aliases")]:
                if alias and alias != kept.get("source") and alias not in aliases:
                    aliases.append(alias)
            row_hits = list(values(row, "search_hits"))
            if row.get("search_hit_url") and not any(isinstance(hit, dict) and hit.get("url") == row["search_hit_url"] for hit in row_hits):
                row_hits.append({"url": row["search_hit_url"]})
            for hit in row_hits:
                if hit not in hits:
                    hits.append(hit)
            for duplicate in values(row, "duplicate_sources"):
                if duplicate not in duplicates:
                    duplicates.append(duplicate)
            if row is not chosen:
                duplicate = {"source": row.get("source", ""), "source_scope": article_scope(str(row.get("source", "")), row),
                             "search_hit_url": row.get("search_hit_url", ""), "reason": "same_article_identity"}
                if duplicate not in duplicates:
                    duplicates.append(duplicate)
        kept["alias_sources"], kept["search_hits"], kept["duplicate_sources"] = aliases, deepcopy(hits), deepcopy(duplicates)
        kept["search_hit_urls"] = list(dict.fromkeys(
            [str(hit["url"]) for hit in hits if isinstance(hit, dict) and hit.get("url")]
            + [str(url) for row in group for url in values(row, "search_hit_urls")]))
        for field in ("public_code_urls", "named_framework_matches", "task_identity_matches"):
            kept[field] = list(dict.fromkeys(value for row in group for value in values(row, field) if isinstance(value, str)))
        code = [value for row in group for value in
                ([row["public_code"]] if isinstance(row.get("public_code"), dict) else []) + values(row, "public_code_aliases")
                if isinstance(value, dict)]
        if code:
            kept["public_code"] = deepcopy(max(code, key=lambda value: (
                bool(value.get("packet_path")), value.get("status") in {"available", "partial"},
                len(values(value, "files")))))
            kept["public_code_aliases"] = deepcopy([value for value in code if value != kept["public_code"]])
        result.append(kept)
    return result
