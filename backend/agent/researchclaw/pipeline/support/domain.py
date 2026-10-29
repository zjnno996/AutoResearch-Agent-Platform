"""Domain detection and topic constraints shared by research stages."""

from __future__ import annotations

import logging
import re


logger = logging.getLogger(__name__)


_DOMAIN_KEYWORDS: dict[str, tuple[list[str], str, str]] = {
    # domain_id: (keywords, display_name, top_venues)
    "ml": (
        ["machine learning", "deep learning", "neural network", "transformer",
         "reinforcement learning", "GAN", "diffusion model", "LLM", "language model",
         "computer vision", "NLP", "representation learning", "self-supervised",
         "federated learning", "meta-learning", "continual learning", "few-shot",
         "knowledge distillation", "attention mechanism", "fine-tuning", "RLHF",
         "vision transformer", "ViT", "BERT", "GPT", "autoencoder"],
        "machine learning",
        "NeurIPS, ICML, ICLR",
    ),
    "physics": (
        ["quantum", "thermodynamic", "electrodynamic", "particle physics",
         "condensed matter", "statistical mechanics", "cosmology", "astrophysics",
         "plasma", "optics", "photonics", "relativity", "gravitational",
         "PDE", "PINN", "physics-informed", "Burgers", "Navier-Stokes",
         "Darcy flow", "Schrödinger", "scientific computing", "operator learning",
         "neural operator", "Fourier neural", "DeepONet"],
        "physics",
        "Physical Review Letters, Nature Physics, JHEP",
    ),
    "chemistry": (
        ["molecular", "catalysis", "polymer", "organic chemistry", "inorganic",
         "electrochemistry", "spectroscopy", "crystallography", "drug discovery",
         "protein folding", "computational chemistry", "DFT", "force field"],
        "chemistry",
        "JACS, Nature Chemistry, Angewandte Chemie",
    ),
    "economics": (
        ["econometric", "macroeconomic", "microeconomic", "game theory",
         "market", "fiscal policy", "monetary", "behavioral economics",
         "causal inference", "panel data", "regression discontinuity",
         "instrumental variable", "supply chain", "auction"],
        "economics",
        "AER, Econometrica, QJE, Review of Economic Studies",
    ),
    "mathematics": (
        ["theorem", "proof", "prove", "conjecture", "topology", "algebra",
         "number theory", "combinatorics", "differential equation",
         "stochastic process", "functional analysis", "manifold",
         "Riemannian", "category theory", "graph theory",
         "neural ODE", "dynamical system", "Lorenz", "chaotic",
         "Lyapunov", "attractor", "ODE solver", "trajectory prediction",
         "mathematical formulation", "mathematical proof", "derivation",
         "Brownian motion", "branching process", "Galton-Watson",
         "Markov chain", "martingale", "ergodic", "convergence theorem",
         "marginal distribution", "extinction probability", "Feynman-Kac",
         "measure theory", "Hilbert space", "Banach space", "operator theory",
         "variational", "Euler-Lagrange", "calculus of variations"],
        "mathematics",
        "Annals of Mathematics, Inventiones Mathematicae, JAMS",
    ),
    "engineering": (
        ["robotics", "control system", "signal processing", "FPGA",
         "embedded system", "VLSI", "antenna", "fluid dynamics", "CFD",
         "finite element", "structural", "mechatronics", "autonomous"],
        "engineering",
        "IEEE Transactions, ASME journals, AIAA",
    ),
    "biology": (
        ["genomics", "proteomics", "transcriptomics", "CRISPR",
         "single-cell", "phylogenetic", "ecology", "neuroscience",
         "bioinformatics", "sequencing", "gene expression", "epigenetic"],
        "biology",
        "Nature, Science, Cell, PNAS",
    ),
}


def _detect_domain(topic: str, domains: tuple[str, ...] = ()) -> tuple[str, str, str]:
    """Detect research domain from topic string and config domains.

    Returns ``(domain_id, display_name, top_venues)``.
    Falls back to ``("ml", "machine learning", "NeurIPS, ICML, ICLR")``.
    """
    # If user explicitly specified domains, check them first
    for d in domains:
        d_lower = d.lower().strip()
        for did, (kws, dname, venues) in _DOMAIN_KEYWORDS.items():
            if d_lower in (did, dname) or any(k in d_lower for k in kws[:3]):
                return did, dname, venues

    # Auto-detect from topic text
    topic_lower = topic.lower()
    best_did, best_score = "ml", 0
    # BUG-101: Explicit theoretical intent words boost non-empirical domain scores.
    # Topics like "derive the mathematical formulation of X diffusion model"
    # should classify as math, not ML, even if "diffusion model" is an ML keyword.
    _theoretical_intent = any(
        w in topic_lower
        for w in ("derive", "prove", "mathematical formulation",
                  "mathematical proof", "formal proof", "formalism")
    )
    for did, (kws, dname, venues) in _DOMAIN_KEYWORDS.items():
        score = sum(1 for k in kws if k.lower() in topic_lower)
        # Boost non-empirical domains when theoretical intent is detected
        if _theoretical_intent and did in ("mathematics", "physics", "economics"):
            score += 1
        if score > best_score:
            best_score = score
            best_did = did

    did = best_did
    _, dname, venues = _DOMAIN_KEYWORDS[did]
    return did, dname, venues


def _is_ml_domain(domain_id: str) -> bool:
    """Check if the detected domain is ML/AI."""
    return domain_id == "ml"


_CHINESE_ENGLISH_DOMAIN_MAP: dict[str, list[str]] = {
    "具身智能": ["embodied intelligence", "embodied AI"],
    "视觉语言动作": ["vision language action", "VLA"],
    "视觉语言模型": ["vision language model", "VLM"],
    "世界模型": ["world model"],
    "动作模型": ["action model"],
    "机器人": ["robot", "robotics"],
    "操控": ["manipulation"],
    "抓取": ["grasping"],
    "导航": ["navigation"],
    "模仿学习": ["imitation learning"],
    "强化学习": ["reinforcement learning"],
    "扩散策略": ["diffusion policy"],
    "视频生成": ["video generation"],
    "动作预测": ["action prediction"],
    "架构": ["architecture"],
    "最新": ["latest", "recent", "state of the art"],
    "评估": ["evaluation", "benchmark"],
    "应用": ["application"],
    "调研": ["survey"],
    "研究": ["research"],
    "方向": ["direction"],
    "联合建模": ["joint modeling", "unified model"],
    "联合训练": ["joint training"],
    "跨具身": ["cross-embodiment"],
    "灵巧操作": ["dexterous manipulation"],
    "双臂": ["bimanual", "dual-arm"],
    "长时序": ["long-horizon"],
    "泛化": ["generalization"],
    "零样本": ["zero-shot"],
    "预训练": ["pretraining", "pre-training"],
    "微调": ["fine-tuning"],
}


def _extract_english_from_mixed(topic: str) -> list[str]:
    """Extract English tokens and translate Chinese domain terms from mixed text."""
    english_tokens = re.findall(r"[a-zA-Z][a-zA-Z0-9\-_.]{1,}", topic)
    english_tokens = [t.strip("-_.") for t in english_tokens if len(t.strip("-_.")) > 1]

    translated: list[str] = []
    for zh, en_list in _CHINESE_ENGLISH_DOMAIN_MAP.items():
        if zh in topic:
            translated.extend(en_list)

    return english_tokens + translated


_STOP_WORDS = frozenset(
    {
        "a",
        "an",
        "the",
        "and",
        "or",
        "but",
        "in",
        "on",
        "of",
        "for",
        "to",
        "with",
        "by",
        "at",
        "from",
        "as",
        "is",
        "are",
        "was",
        "were",
        "be",
        "been",
        "being",
        "have",
        "has",
        "had",
        "do",
        "does",
        "did",
        "will",
        "would",
        "could",
        "should",
        "may",
        "might",
        "can",
        "shall",
        "not",
        "no",
        "nor",
        "so",
        "yet",
        "both",
        "each",
        "every",
        "all",
        "any",
        "few",
        "more",
        "most",
        "other",
        "some",
        "such",
        "than",
        "too",
        "very",
        "just",
        "about",
        "above",
        "after",
        "again",
        "between",
        "into",
        "through",
        "during",
        "before",
        "after",
        "under",
        "over",
        "using",
        "based",
        "via",
        "toward",
        "towards",
        "new",
        "novel",
        "approach",
        "method",
        "study",
        "research",
        "paper",
        "work",
        "propose",
        "proposed",
    }
)


def _extract_topic_keywords(
    topic: str, domains: tuple[str, ...] | list[str] = ()
) -> list[str]:
    """Extract meaningful keywords from the research topic + domain list.

    Returns lowercased keyword list (2+ chars, no stop words).
    Used by the domain pre-filter to drop obviously irrelevant papers.
    Handles mixed Chinese-English topics by translating Chinese domain terms.
    """
    tokens = re.findall(r"[a-zA-Z][a-zA-Z0-9_-]+", topic.lower())
    keywords = [t for t in tokens if t not in _STOP_WORDS and len(t) >= 3]
    if re.search(r"[\u4e00-\u9fff]", topic):
        translated = _extract_english_from_mixed(topic)
        for t in translated:
            t_lower = t.lower()
            if t_lower not in _STOP_WORDS and len(t_lower) >= 3:
                keywords.append(t_lower)
    for d in domains:
        for part in re.findall(r"[a-zA-Z][a-zA-Z0-9_-]+", d.lower()):
            if part not in _STOP_WORDS and len(part) >= 2:
                keywords.append(part)
    seen: set[str] = set()
    unique: list[str] = []
    for kw in keywords:
        if kw not in seen:
            seen.add(kw)
            unique.append(kw)
    return unique


def _topic_constraint_block(topic: str) -> str:
    """Return a hard constraint instruction that anchors paper content to the topic.

    Prevents the common LLM failure mode of drifting off-topic or
    presenting environmental/infrastructure issues as research contributions.
    """
    return (
        "\n\n=== HARD TOPIC CONSTRAINT ===\n"
        f"The paper MUST be about: {topic}\n"
        "PROHIBITED content (unless user explicitly specifies case-study mode):\n"
        "- Do NOT treat environment setup, dependency installation, or infrastructure "
        "failures as a research contribution.\n"
        "- Do NOT present debugging logs, system errors, or configuration issues "
        "as experimental findings.\n"
        "- Do NOT drift to tangential topics not directly related to the stated topic.\n"
        "- Every section MUST connect back to the core research question.\n"
        "- The Abstract and Introduction MUST clearly state the research problem "
        f"derived from: {topic}\n"
        "- The Method section MUST describe a technical approach, not a workflow.\n"
        "- The Results section MUST report quantitative outcomes of experiments, "
        "not environment status.\n"
        "=== END CONSTRAINT ===\n"
    )


_SEARCH_STOP_WORDS = {
    "a", "an", "the", "of", "for", "in", "on", "and", "or", "with",
    "to", "by", "from", "its", "is", "are", "was", "be", "as", "at",
    "via", "using", "based", "study", "analysis", "empirical",
    "towards", "toward", "into", "exploring", "comparison", "tasks",
    "effectiveness", "investigation", "comprehensive", "novel",
}

def _extract_keywords(text: str) -> list[str]:
    """Extract meaningful keywords from text, removing stop words.

    Handles mixed Chinese-English text by also extracting English tokens
    and translating Chinese domain terms.
    """
    en_words = [
        w for w in re.split(r"[^a-zA-Z0-9]+", text)
        if w.lower() not in _SEARCH_STOP_WORDS and len(w) > 1
    ]
    if re.search(r"[\u4e00-\u9fff]", text):
        en_words.extend(_extract_english_from_mixed(text))
        en_words = list(dict.fromkeys(w for w in en_words if w))
    return en_words
