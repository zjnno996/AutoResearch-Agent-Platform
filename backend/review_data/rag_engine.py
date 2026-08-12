"""Lightweight RAG engine for retrieving relevant few-shot review examples.

Uses TF-IDF vectorization + cosine similarity to find the most relevant
review examples given a paper's abstract, keywords, or query text.

Pure Python (no NumPy/scikit-learn required). Uses the standard library's
math and collections.Counter for vector operations.

Architecture:
  1. Build TF-IDF index from all example reviews
  2. Given a query (paper abstract + keywords), compute TF-IDF vector
  3. Return top-k most similar examples by cosine similarity

Usage:
    engine = RAGEngine()
    engine.build_index(fewshot_dataset)
    engine.build_index_from_library()  # Uses curated few-shot libary
    examples = engine.retrieve("transformer architecture for protein folding", dim_id="novelty", k=2)
"""

from __future__ import annotations

import math
import re
from collections import Counter
from typing import Any

from .schema import Review
from .fewshot_library import ALL_DIMENSION_EXAMPLES, get_all_examples

# Try to load review-history-derived examples
try:
    from .review_history_examples import REVIEW_HISTORY_EXAMPLES, get_history_examples
except Exception:
    REVIEW_HISTORY_EXAMPLES: dict[str, list[Review]] = {}
    get_history_examples = lambda: []  # type: ignore[assignment]


def _tokenize(text: str) -> list[str]:
    """Tokenize text into lowercase alphanumeric tokens."""
    text = text.lower()
    # Split on non-alphanumeric
    tokens = re.findall(r"[a-z0-9]+(?:'[a-z]+)?", text)
    # Filter short tokens and common stopwords
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


def _compute_tf(tokens: list[str]) -> dict[str, float]:
    """Compute term frequency (log normalization)."""
    if not tokens:
        return {}
    counter = Counter(tokens)
    total = len(tokens)
    return {term: 1.0 + math.log(count) for term, count in counter.items()}


def _compute_idf(documents: list[list[str]]) -> dict[str, float]:
    """Compute inverse document frequency."""
    n_docs = len(documents)
    if n_docs == 0:
        return {}

    doc_freq: Counter[str] = Counter()
    for doc_tokens in documents:
        unique = set(doc_tokens)
        for term in unique:
            doc_freq[term] += 1

    idf: dict[str, float] = {}
    for term, freq in doc_freq.items():
        idf[term] = math.log((n_docs + 1) / (freq + 1)) + 1.0

    return idf


def _normalize_vector(v: dict[str, float]) -> dict[str, float]:
    """L2-normalize a vector."""
    norm = math.sqrt(sum(val * val for val in v.values()))
    if norm == 0:
        return v
    return {k: val / norm for k, val in v.items()}


def _cosine_similarity(
    v1: dict[str, float],
    v2: dict[str, float],
) -> float:
    """Compute cosine similarity between two sparse vectors."""
    dot_product = 0.0
    # Iterate over the smaller vector's keys
    if len(v1) > len(v2):
        v1, v2 = v2, v1
    for term, val in v1.items():
        if term in v2:
            dot_product += val * v2[term]
    return dot_product


# =============================================================================
# Document representation
# =============================================================================

class IndexedReview:
    """A review example with its precomputed TF vector."""

    def __init__(self, review: Review):
        self.review = review
        self.text = self._build_text()
        self.tokens = _tokenize(self.text)
        self.tf_vector: dict[str, float] = {}
        self.dim_id = ""
        self.score = review.overall_score or 0

        # Extract dimension from DimensionReview if available
        if review.dimensions:
            self.dim_id = review.dimensions[0].dimension_id

    def _build_text(self) -> str:
        """Build searchable text from the review."""
        parts = [
            self.review.paper_title,
            " ".join(self.review.paper_keywords),
            self.review.comment_to_author,
            " ".join(self.review.strengths),
            " ".join(self.review.weaknesses),
            " ".join(self.review.suggestions),
        ]
        return " ".join(p for p in parts if p)


# =============================================================================
# RAG Engine
# =============================================================================

class RAGEngine:
    """Lightweight TF-IDF retrieval engine for few-shot review examples."""

    def __init__(self) -> None:
        self.documents: list[IndexedReview] = []
        self.idf: dict[str, float] = {}
        self._built = False
        self._dim_index: dict[str, list[int]] = {}  # dim_id -> doc indices

    def build_index(self, reviews: list[Review]) -> None:
        """Build the TF-IDF index from a list of Review objects."""
        self.documents = [IndexedReview(r) for r in reviews]
        self._build()

    def build_index_from_library(self) -> None:
        """Build index from the curated few-shot library + review history."""
        all_reviews = []
        for dim_id, examples in ALL_DIMENSION_EXAMPLES.items():
            for ex in examples:
                if not ex.dimensions:
                    from .schema import DimensionReview
                    ex.dimensions.append(DimensionReview(dimension_id=dim_id))
                all_reviews.append(ex)

        # Also include review-history-derived examples
        for dim_id, examples in REVIEW_HISTORY_EXAMPLES.items():
            for ex in examples:
                if not ex.dimensions:
                    from .schema import DimensionReview
                    ex.dimensions.append(DimensionReview(dimension_id=dim_id))
                all_reviews.append(ex)

        reviews = get_all_examples() + get_history_examples()
        self.build_index(reviews)

    def _build(self) -> None:
        """Build the TF-IDF index."""
        all_tokens = [doc.tokens for doc in self.documents]
        self.idf = _compute_idf(all_tokens)

        for doc in self.documents:
            doc.tf_vector = _compute_tf(doc.tokens)

        # Build dimension index
        self._dim_index = {}
        for i, doc in enumerate(self.documents):
            dim = doc.dim_id
            if dim:
                self._dim_index.setdefault(dim, []).append(i)

        self._built = True

    def retrieve(
        self,
        query: str,
        dim_id: str | None = None,
        k: int = 2,
        min_score: float | None = None,
    ) -> list[Review]:
        """Retrieve the top-k most relevant reviews for a query.

        Args:
            query: Search query (e.g., paper abstract + problem statement)
            dim_id: Optional dimension filter (only retrieve from this dimension)
            k: Number of results to return
            min_score: Optional minimum score filter

        Returns:
            List of Review objects, sorted by relevance descending
        """
        if not self._built:
            return []

        # Filter by dimension
        if dim_id and dim_id in self._dim_index:
            candidates = [self.documents[i] for i in self._dim_index[dim_id]]
        else:
            candidates = self.documents

        if not candidates:
            return []

        # Filter by min_score
        if min_score is not None:
            candidates = [d for d in candidates if d.score >= min_score]

        if not candidates:
            return []

        # Compute query TF-IDF vector
        query_tokens = _tokenize(query)
        if not query_tokens:
            candidates_sorted = candidates[:k]
            return [d.review for d in candidates_sorted]

        query_tf = _compute_tf(query_tokens)
        query_vec = _normalize_vector({term: val * self.idf.get(term, 1.0)
                                        for term, val in query_tf.items()})

        # Compute similarities
        scored: list[tuple[float, IndexedReview]] = []
        for doc in candidates:
            doc_vec = _normalize_vector({term: val * self.idf.get(term, 1.0)
                                          for term, val in doc.tf_vector.items()})
            sim = _cosine_similarity(query_vec, doc_vec)
            scored.append((sim, doc))

        # Sort by similarity descending
        scored.sort(key=lambda x: x[0], reverse=True)

        return [doc.review for _, doc in scored[:k]]

    def retrieve_for_paper(
        self,
        abstract: str,
        keywords: list[str] | None = None,
        dim_id: str | None = None,
        k: int = 2,
    ) -> list[Review]:
        """Convenience method to retrieve examples for a paper.

        Args:
            abstract: Paper abstract text
            keywords: Optional list of keywords
            dim_id: Optional dimension filter
            k: Number of examples to return

        Returns:
            Relevant Review objects
        """
        query_parts = [abstract]
        if keywords:
            query_parts.extend(keywords)
        query = " ".join(query_parts)
        return self.retrieve(query, dim_id=dim_id, k=k)


# =============================================================================
# Singleton
# =============================================================================

_engine: RAGEngine | None = None


def get_rag_engine(rebuild: bool = False) -> RAGEngine:
    """Get or create the singleton RAG engine."""
    global _engine
    if _engine is None or rebuild:
        _engine = RAGEngine()
        _engine.build_index_from_library()
    return _engine


def retrieve_examples(
    query: str,
    dim_id: str | None = None,
    k: int = 2,
) -> list[Review]:
    """Quick access: retrieve few-shot examples with default engine."""
    engine = get_rag_engine()
    return engine.retrieve(query, dim_id=dim_id, k=k)
