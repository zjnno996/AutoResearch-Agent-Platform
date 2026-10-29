"""Article aliases consume one bounded source slot and retain honest provenance."""
from copy import deepcopy
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from researchclaw.orchestration.bibliography import (
    article_identity, article_scope, canonical_article_url, deduplicate_references,
)
from researchclaw.orchestration import grounding, public_code


DOI = "10.1038/s41586-024-08545-z"
SPRINGER = "https://link.springer.com/article/" + DOI + "?utm_source=search"
NATURE = "https://www.nature.com/articles/s41586-024-08545-z:"


@pytest.mark.parametrize("url", [SPRINGER, NATURE,
    "https://doi.org/10.1038/S41586-024-08545-Z",
    "https://www.nature.com/articles/s41586-024-08545-z)."])
def test_same_article_has_one_generic_doi_identity(url):
    assert article_identity(url) == "doi:" + DOI


@pytest.mark.parametrize("url,doi", [
    ("https://www.science.org/doi/10.1126/science.abc1234.", "10.1126/science.abc1234"),
    ("https://onlinelibrary.wiley.com/doi/full/10.1002/test(ABC)", "10.1002/test(abc)"),
    ("https://pubs.acs.org/doi/abs/10.1021/abc.12345", "10.1021/abc.12345"),
    ("https://journals.aps.org/prl/abstract/10.1103/PhysRevLett.123.456789", "10.1103/physrevlett.123.456789"),
    ("https://link.springer.com/content/pdf/10.1007/test-123.pdf", "10.1007/test-123"),
    ("https://www.biorxiv.org/content/10.1101/2024.01.02.123456v3.full.pdf", "10.1101/2024.01.02.123456"),
])
def test_explicit_publisher_doi_routes_are_not_nature_specific(url, doi):
    assert article_identity(url) == "doi:" + doi


def test_balanced_and_encoded_doi_parentheses_and_suffixes_are_preserved():
    suffix = "(SICI)1097-0258(19970815)16:15<1683::AID-SIM607>3.0.CO;2-V"
    expected = "doi:10.1002/" + suffix.casefold()
    url = "https://onlinelibrary.wiley.com/doi/10.1002/" + suffix
    assert article_identity(url) == expected
    assert article_identity(url.replace("(", "%28").replace(")", "%29")) == expected
    assert article_identity("https://doi.org/10.1234/valid(part)") == "doi:10.1234/valid(part)"
    assert canonical_article_url("https://doi.org/10.1234/valid(part)").endswith("valid(part)")
    assert article_identity("https://doi.org/10.1234/legitimate.pdf") == "doi:10.1234/legitimate.pdf"
    # Exact structured DOI metadata retains even otherwise ambiguous punctuation.
    assert article_identity("https://publisher.invalid/item", {"doi": "10.1234/exact.suffix."}) == "doi:10.1234/exact.suffix."


def test_trailing_prose_is_removed_only_for_recognized_article_links():
    assert canonical_article_url(NATURE) == NATURE[:-1]
    assert canonical_article_url("https://www.science.org/doi/10.1126/abc(1)).") == "https://www.science.org/doi/10.1126/abc(1)"
    assert grounding.primary_url(NATURE) == NATURE[:-1]
    assert canonical_article_url("https://example.org/unrecognized:").endswith("unrecognized:")


@pytest.mark.parametrize("url", [
    "https://arxiv.org/abs/2401.12345v1",
    "https://arxiv.org/pdf/2401.12345v3.pdf",
    "https://arxiv.org/html/2401.12345v2#section-4",
    "https://export.arxiv.org/pdf/2401.12345v4.pdf).",
])
def test_arxiv_versions_and_presentations_share_one_identity(url):
    assert article_identity(url) == "arxiv:2401.12345"


def test_legacy_arxiv_versions_share_one_identity():
    assert article_identity("https://arxiv.org/pdf/hep-th/9901001v3.pdf") == "arxiv:hep-th/9901001"
    assert article_identity("https://arxiv.org/abs/math.GT/0309136v2") == "arxiv:math.gt/0309136"
    assert grounding.primary_url("https://export.arxiv.org/pdf/hep-th/9901001v3.pdf") == "https://arxiv.org/abs/hep-th/9901001v3"


def test_pmc_parent_identity_and_actual_attachment_scope_are_separate():
    main = "https://pmc.ncbi.nlm.nih.gov/articles/PMC12345/"
    attachment = "https://pmc.ncbi.nlm.nih.gov/articles/instance/12345/bin/review.pdf"
    assert article_identity(main) == article_identity(attachment) == "pmc:PMC12345"
    assert canonical_article_url(main + "#methods") == main
    assert article_scope(main) == "main"
    assert article_scope(attachment, {"title": "Peer review report"}) == "review"
    assert canonical_article_url(attachment).endswith("/bin/review.pdf")


def test_no_doi_is_inferred_from_bodies_reference_lists_queries_or_host_lookalikes():
    urls = [
        "https://nature.com.example.org/articles/s41586-024-08545-z",
        "https://example.org/article/" + DOI,
        "https://www.nature.com/search?reference=" + DOI,
        "https://www.nature.com/references/" + DOI,
    ]
    for url in urls:
        assert not article_identity(url, {"content": "Cited " + DOI, "references": [{"doi": DOI}]}).startswith("doi:")
    assert not article_identity(urls[0], {"doi": "References include " + DOI}).startswith("doi:")
    assert article_identity(urls[0], {"document_metadata": {"doi": DOI}}) == "doi:" + DOI


def test_different_dois_and_unknown_paths_are_not_merged_by_title_or_text():
    rows = [{"source": "https://doi.org/10.1234/" + name, "title": "Identical title", "context": DOI}
            for name in ("one", "two")]
    rows += [{"source": name, "title": "Identical title", "context": DOI} for name in ("local/one.pdf", "local/two.pdf")]
    assert len(deduplicate_references(rows)) == 4
    assert len(deduplicate_references([rows[-1], deepcopy(rows[-1])])) == 1
    assert article_identity("https://example.org/a%2Fb") != article_identity("https://example.org/a/b")
    assert article_identity("https://example.org/article?version=1") != article_identity("https://example.org/article?version=2")


def test_main_document_preferred_with_doi_alias_provenance_and_author_code_preserved():
    rows = [
        {"source": "https://pmc.ncbi.nlm.nih.gov/articles/PMC12345/bin/review.pdf",
         "title": "Peer review report", "document_metadata": {"doi": DOI},
         "source_scope": "Peer review report only", "text_path": "review.md", "context": "REVIEW_ONLY",
         "search_hit_url": "https://search.example/review",
         "public_code_urls": ["https://github.com/authors/method"],
         "public_code": {"status": "partial", "packet_path": "code.md", "commit_sha": "a" * 40}},
        {"source": NATURE[:-1], "title": "Original article", "document_metadata": {"doi": DOI},
         "text_path": "main.md", "context": "MAIN_METHOD", "content_origin": "fetch", "retrieval_status": "source_content",
         "search_hit_url": NATURE},
    ]
    original = deepcopy(rows)
    result = deduplicate_references(rows)
    assert rows == original and len(result) == 1
    row = result[0]
    assert row["source"] == NATURE[:-1] and row["text_path"] == "main.md"
    assert row["context"] == "MAIN_METHOD" and "REVIEW_ONLY" not in row["context"]
    assert row["article_identity"] == "doi:" + DOI
    assert row["alias_sources"] == [rows[0]["source"]]
    assert set(row["search_hit_urls"]) == {NATURE, "https://search.example/review"}
    assert row["duplicate_sources"][0]["source_scope"] == "review"
    assert row["public_code"]["packet_path"] == "code.md"
    assert row["public_code_urls"] == ["https://github.com/authors/method"]
    row["public_code"]["status"] = "changed"
    assert rows == original


def test_structured_main_fetch_beats_same_article_snippet_and_review_remains_honest():
    snippet = {"source": SPRINGER, "context": "Abstract", "retrieval_status": "snippet_only"}
    main = {"source": NATURE[:-1], "context": "Body", "content_origin": "pmc_bioc", "retrieval_status": "source_content"}
    assert deduplicate_references([snippet, main])[0]["source"] == main["source"]
    main_other_host = {"source": "https://academic.oup.com/journal/article/1/2/123", "document_metadata": {"doi": DOI},
                       "source_scope": "Original article-body passages", "content_origin": "pmc_bioc", "context": "Actual body"}
    assert deduplicate_references([snippet, main_other_host])[0]["source"] == main_other_host["source"]
    supplement = {"source": "https://doi.org/10.1234/paper/suppl_file/data.pdf",
                  "source_scope": "Supplementary document only", "context": "ACTUAL_SUPPLEMENT"}
    only = deduplicate_references([supplement])[0]
    assert only["source"] == supplement["source"] and only["context"] == "ACTUAL_SUPPLEMENT"
    assert article_scope(only["source"], only) == "supplement"
    distinct = {"source": "https://doi.org/10.1234/paper-supplement", "context": "Separate DOI"}
    assert len(deduplicate_references([supplement, distinct])) == 2


def mock_discovery(monkeypatch, searches, fetches=None, *, cap=2):
    sent = []
    searches = iter(searches)
    fetches = fetches or {}

    class Response:
        def __init__(self, data): self.data = data
        def __enter__(self): return self
        def __exit__(self, *args): pass
        def read(self, *args): return json.dumps(self.data).encode()

    def request(req, **kwargs):
        assert req.full_url.startswith("https://api.deepseek.com/v1/tools/")
        endpoint = req.full_url.rsplit("/", 1)[-1]
        body = json.loads(req.data)
        sent.append((endpoint, body))
        return Response(next(searches) if endpoint == "search" else fetches.get(body["url"], {}))

    monkeypatch.setattr(grounding.urllib.request, "urlopen", request)
    monkeypatch.setattr(grounding, "source_queries", lambda *args: ["first scientific query", "second scientific query"])
    monkeypatch.setattr(grounding, "named_frameworks", lambda topic: [])
    monkeypatch.setattr(public_code, "acquire_public_code", lambda *args: {"status": "unavailable", "llm_calls": 0})
    config = SimpleNamespace(research=SimpleNamespace(topic="Scientific methods"),
        research_engine=SimpleNamespace(public_source_search=True, max_source_queries=2, max_public_sources=cap))
    llm = SimpleNamespace(base_url="https://api.deepseek.com/v1", api_key="private-fixture")
    return config, llm, sent


def hit(url, title="A scientific method", content="Short source excerpt"):
    return {"url": url, "title": title, "content": content}


def test_duplicate_publisher_hits_use_one_fetch_and_leave_next_query_a_distinct_slot(tmp_path, monkeypatch):
    distinct = "https://www.science.org/doi/10.1126/science.distinct"
    config, llm, sent = mock_discovery(monkeypatch,
        [{"results": [hit(SPRINGER)]}, {"results": [hit(NATURE), hit(distinct)]}],
        {SPRINGER: {"content": "Original main article. " * 100}, distinct: {"content": "Distinct article. " * 100}})
    rows = grounding.discover_sources(tmp_path, config, llm, {"source_queries": ["ignored"]})
    assert len(rows) == 2
    assert [body["url"] for endpoint, body in sent if endpoint == "fetch"] == [SPRINGER, distinct]
    assert len(sent) == 4
    assert rows[0]["article_identity"] == "doi:" + DOI
    assert NATURE[:-1] in rows[0]["alias_sources"]
    assert set(rows[0]["search_hit_urls"]) == {SPRINGER, NATURE}
    assert rows[1]["article_identity"] != rows[0]["article_identity"]
    audit = json.loads((tmp_path / "public_source_retrieval.json").read_text())
    assert len(audit["sources"]) == 2 and audit["deduplicated_sources"]
    assert "private-fixture" not in json.dumps(audit) and audit["llm_calls"] == 0


def test_source_cap_is_not_a_quota_when_queries_only_find_aliases(tmp_path, monkeypatch):
    config, llm, sent = mock_discovery(monkeypatch,
        [{"results": [hit(SPRINGER)]}, {"results": [hit(NATURE)]}],
        {SPRINGER: {"content": "Original main article. " * 100}})
    rows = grounding.discover_sources(tmp_path, config, llm, {})
    assert len(rows) == 1
    assert len([entry for entry in sent if entry[0] == "fetch"]) == 1


def test_main_hit_in_later_pool_is_chosen_before_parent_doi_supplement_fetch(tmp_path, monkeypatch):
    attachment = "https://www.science.org/doi/10.1126/science.parent/suppl_file/review.pdf"
    main = "https://www.science.org/doi/10.1126/science.parent"
    config, llm, sent = mock_discovery(monkeypatch,
        [{"results": [hit(attachment, "Peer review report")]}, {"results": [hit(main, "Main article")]}],
        {main: {"content": "ORIGINAL_MAIN_METHOD " * 100}})
    rows = grounding.discover_sources(tmp_path, config, llm, {})
    assert len(rows) == 1 and rows[0]["source"] == main
    assert [body["url"] for endpoint, body in sent if endpoint == "fetch"] == [main]
    assert "ORIGINAL_MAIN_METHOD" in Path(rows[0]["text_path"]).read_text()
    assert rows[0]["duplicate_sources"][0]["source_scope"] == "review"


def test_postfetch_exact_metadata_doi_dedupes_without_splicing_supplement_text(tmp_path, monkeypatch):
    supplement = "https://academic.oup.com/journal/article/1/2/123"
    main = "https://www.science.org/doi/10.1126/science.parent"
    config, llm, sent = mock_discovery(monkeypatch,
        [{"results": [hit(supplement)]}, {"results": [hit(main)]}],
        {supplement: {"content": "SUPPLEMENT_ONLY " * 100, "source_scope": "Supplementary file only",
                      "document_metadata": {"doi": "10.1126/science.parent"}},
         main: {"content": "ORIGINAL_MAIN_METHOD " * 100, "document_metadata": {"doi": "10.1126/science.parent"}}})
    rows = grounding.discover_sources(tmp_path, config, llm, {})
    assert len(rows) == 1 and rows[0]["source"] == main
    text = Path(rows[0]["text_path"]).read_text()
    assert "ORIGINAL_MAIN_METHOD" in text and "SUPPLEMENT_ONLY" not in text
    assert supplement in rows[0]["alias_sources"]
    assert len([entry for entry in sent if entry[0] == "fetch"]) == 2


def test_body_reference_doi_does_not_collapse_distinct_fetched_articles(tmp_path, monkeypatch):
    first = "https://academic.oup.com/journal/article/1/2/123"
    second = "https://academic.oup.com/journal/article/1/2/456"
    config, llm, sent = mock_discovery(monkeypatch,
        [{"results": [hit(first)]}, {"results": [hit(second)]}],
        {url: {"content": ("Reference cites " + DOI + ". ") * 100} for url in (first, second)})
    rows = grounding.discover_sources(tmp_path, config, llm, {})
    assert len(rows) == 2 and rows[0]["article_identity"] != rows[1]["article_identity"]
    assert len(sent) == 4
