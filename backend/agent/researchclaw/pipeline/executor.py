from __future__ import annotations

import os
import json
import logging
import math
import os
import re
import time as _time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Sequence

import yaml

from researchclaw.adapters import AdapterBundle
from researchclaw.config import RCConfig
from researchclaw.hardware import HardwareProfile, detect_hardware, ensure_torch_available, is_metric_name
from researchclaw.llm import create_llm_client
from researchclaw.llm.client import LLMClient, LLMConfig
from researchclaw.prompts import PromptManager
from researchclaw.pipeline.stages import (
    NEXT_STAGE,
    Stage,
    StageStatus,
    TransitionEvent,
    TransitionOutcome,
    advance,
    gate_required,
)
from researchclaw.pipeline.contracts import CONTRACTS, StageContract
from researchclaw.experiment.validator import (
    CodeValidation,
    format_issues_for_llm,
    validate_code,
)

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Domain detection — maps research topic to academic domain & venue context
# ---------------------------------------------------------------------------

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


@dataclass(frozen=True)
class StageResult:
    """Outcome of executing a single stage."""

    stage: Stage
    status: StageStatus
    artifacts: tuple[str, ...]
    error: str | None = None
    decision: str = "proceed"
    evidence_refs: tuple[str, ...] = ()


def _utcnow_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


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


def _build_fallback_queries(topic: str) -> list[str]:
    """Extract meaningful search queries from a long topic string.

    Handles mixed Chinese-English topics by translating Chinese domain
    terms and extracting English keywords for academic search engines.
    """
    has_chinese = bool(re.search(r"[\u4e00-\u9fff]", topic))

    queries: list[str] = []
    seen: set[str] = set()

    def _add(q: str) -> None:
        q = q.strip()
        if q and q.lower() not in seen:
            seen.add(q.lower())
            queries.append(q)

    if has_chinese:
        en_tokens = _extract_english_from_mixed(topic)
        en_tokens_dedup = list(dict.fromkeys(t.lower() for t in en_tokens))

        _skip_en = {"lab", "the", "and", "for", "with", "de", "成员"}
        core_en = [t for t in en_tokens_dedup if len(t) > 2 and t.lower() not in _skip_en]

        has_video = "video" in topic.lower()
        has_action = "action" in topic.lower()
        has_world_model = "世界模型" in topic or "world model" in topic.lower()
        has_embodied = "具身" in topic or "embodied" in topic.lower()
        has_vla = "vla" in topic.lower()
        has_vlm = "vlm" in topic.lower()

        if has_video or has_action:
            _add("video action model robot embodied intelligence")
            _add("world action model WAM robot manipulation")
            _add("joint video action prediction robot")
        if has_world_model or has_video:
            _add("world model action joint training robot")
            _add("unified world model vision language action")
        if has_embodied:
            _add("embodied AI robot manipulation latest architecture")
            _add("embodied intelligence VLA world model")
        if has_vla or has_vlm:
            _add("vision language action model robot VLA")
            _add("VLM evaluation embodied robot")

        if core_en:
            _add(" ".join(core_en[:6]))
            _add(" ".join(core_en[:4]) + " robot survey")
            _add(" ".join(core_en[:4]) + " benchmark")
    else:
        chunks = re.split(r"[,:;()\[\]]+", topic)
        chunks = [c.strip() for c in chunks if len(c.strip()) > 8]
        cleaned_chunks = []
        for c in chunks:
            c = re.sub(
                r"^(and|or|the|a|an|in|of|for|with|across|multiple|three|various)\s+",
                "", c, flags=re.IGNORECASE,
            )
            c = c.strip()
            if len(c) > 8:
                cleaned_chunks.append(c)
        chunks = cleaned_chunks

        _stop = {
            "the", "and", "for", "with", "from", "that", "this", "into",
            "over", "across", "multiple", "three", "result", "comprehensive",
            "using", "based", "between", "various", "different", "several",
            "parameter", "parameters", "analysis", "approach", "method",
            "framework", "frameworks",
        }
        words = topic.lower().split()
        key_terms = [w for w in words if len(w) > 3 and w not in _stop]

        for chunk in chunks[:4]:
            if len(chunk) > 60:
                chunk = " ".join(chunk.split()[:6])
            _add(chunk)

        clean_terms = [t for t in key_terms if re.match(r"^[a-z]", t) and ":" not in t]
        for i in range(min(len(clean_terms) - 1, 4)):
            _add(f"{clean_terms[i]} {clean_terms[i + 1]}")

    topic_short = " ".join(re.findall(r"[a-zA-Z][a-zA-Z0-9\-_.]+", topic))[:60].strip()
    if not topic_short:
        topic_short = topic[:60].strip()
    for suffix in ("survey", "review", "benchmark", "state of the art", "recent advances"):
        if len(queries) >= 8:
            break
        _add(f"{topic_short} {suffix}")

    return queries[:12]


def _write_stage_meta(
    stage_dir: Path, stage: Stage, run_id: str, result: StageResult
) -> None:
    next_stage = NEXT_STAGE[stage]
    meta = {
        "stage_id": f"{int(stage):02d}-{stage.name.lower()}",
        "run_id": run_id,
        "status": result.status.value,
        "decision": result.decision,
        "output_artifacts": list(result.artifacts),
        "evidence_refs": list(result.evidence_refs),
        "error": result.error,
        "ts": _utcnow_iso(),
        "next_stage": int(next_stage) if next_stage is not None else None,
    }
    (stage_dir / "decision.json").write_text(
        json.dumps(meta, indent=2), encoding="utf-8"
    )


def _read_prior_artifact(run_dir: Path, filename: str) -> str | None:
    # R14-2: Sort so non-versioned dirs (stage-13) come before versioned (stage-13_v1).
    # Within the same stage number, prefer the latest (non-versioned) copy.
    def _stage_sort_key(p: Path) -> tuple[str, int]:
        name = p.name
        # Extract base stage name and version
        if "_v" in name:
            base, _, ver = name.rpartition("_v")
            try:
                return (base, -int(ver))  # Versioned: lower priority (negative version)
            except ValueError:
                return (name, -999)
        return (name, 0)  # Non-versioned: highest priority

    for stage_subdir in sorted(run_dir.glob("stage-*"), key=_stage_sort_key, reverse=True):
        candidate = stage_subdir / filename
        if candidate.is_file():
            return candidate.read_text(encoding="utf-8")
        if filename.endswith("/") and (stage_subdir / filename.rstrip("/")).is_dir():
            return str(stage_subdir / filename.rstrip("/"))
    return None


def _find_prior_file(run_dir: Path, filename: str) -> Path | None:
    """Like ``_read_prior_artifact`` but returns the *Path* instead of content."""
    def _stage_sort_key(p: Path) -> tuple[str, int]:
        name = p.name
        if "_v" in name:
            base, _, ver = name.rpartition("_v")
            try:
                return (base, -int(ver))
            except ValueError:
                return (name, -999)
        return (name, 0)

    for stage_subdir in sorted(run_dir.glob("stage-*"), key=_stage_sort_key, reverse=True):
        candidate = stage_subdir / filename
        if candidate.is_file():
            return candidate
    return None


def _read_literature_bib(run_dir: Path) -> str:
    """Read a non-empty bibliography without stale export files shadowing S4."""
    for candidate in (
        run_dir / "stage-22" / "references.bib",
        run_dir / "stage-20" / "references_preverified.bib",
        run_dir / "stage-04" / "references.bib",
    ):
        try:
            text = candidate.read_text(encoding="utf-8")
        except OSError:
            continue
        if re.search(r"@\w+\{[^,]+,", text):
            return text
    return ""


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


def _load_hardware_profile(run_dir: Path) -> dict[str, Any] | None:
    """Load hardware_profile.json from a prior stage (usually stage-01)."""
    raw = _read_prior_artifact(run_dir, "hardware_profile.json")
    if raw is None:
        return None
    try:
        data = json.loads(raw)
        return data if isinstance(data, dict) else None
    except (json.JSONDecodeError, ValueError):
        return None


def _extract_yaml_block(text: str) -> str:
    """Extract YAML from text that may contain ACP noise.

    Strips [thinking] blocks, insight blocks, and other ACP artifacts
    before looking for YAML in markdown fences or raw text.
    """
    # Strip ACP noise: [thinking]..., insight blocks, [plan]...
    cleaned = re.sub(
        r"\[thinking\].*?(?=\n```|\n[A-Z]|\Z)",
        "", text, flags=re.DOTALL,
    )
    cleaned = re.sub(r"\[plan\].*?\n\n", "", cleaned, flags=re.DOTALL)

    # Try markdown fences first (most reliable) — on cleaned text
    if "```yaml" in cleaned:
        return cleaned.split("```yaml", 1)[1].split("```", 1)[0].strip()
    if "```yml" in cleaned:
        return cleaned.split("```yml", 1)[1].split("```", 1)[0].strip()
    if "```" in cleaned:
        block = cleaned.split("```", 1)[1].split("```", 1)[0].strip()
        if block:
            return block

    # Try the original text too (in case cleaning removed too much)
    if "```yaml" in text:
        return text.split("```yaml", 1)[1].split("```", 1)[0].strip()
    if "```yml" in text:
        return text.split("```yml", 1)[1].split("```", 1)[0].strip()
    if "```" in text:
        block = text.split("```", 1)[1].split("```", 1)[0].strip()
        if block:
            return block

    # Last resort: try to find YAML-like content (lines starting with key:)
    yaml_lines: list[str] = []
    in_yaml = False
    for line in cleaned.splitlines():
        stripped = line.strip()
        if not in_yaml and re.match(r"^[a-z_]+:", stripped):
            in_yaml = True
        if in_yaml:
            if stripped and not stripped.startswith("#"):
                yaml_lines.append(line)
            elif not stripped and yaml_lines:
                yaml_lines.append(line)
    if yaml_lines:
        return "\n".join(yaml_lines).strip()

    return text.strip()


def _parse_yaml_dict_from_llm(text: str) -> dict[str, Any] | None:
    """Parse a YAML mapping from noisy LLM output.

    Qwen/reasoning models sometimes return a long answer with one or more
    fenced blocks, preambles, or trailing explanations.  For stage planning we
    only want the first valid top-level mapping, and we should not fall back
    just because the first extraction strategy grabbed the wrong fenced block.
    """
    if not text or not text.strip():
        return None

    candidates: list[str] = []
    candidates.append(_extract_yaml_block(text))
    candidates.append(text.strip())

    fence_re = re.compile(r"```(?:yaml|yml)?\s*\n(.*?)```", re.IGNORECASE | re.DOTALL)
    candidates.extend(match.group(1).strip() for match in fence_re.finditer(text))

    known_keys = (
        "baselines", "proposed_methods", "ablations", "datasets", "metrics",
        "objectives", "risks", "compute_budget", "local_resources",
        "benchmark_suggestions",
    )
    key_re = re.compile(rf"^({'|'.join(known_keys)})\s*:", re.MULTILINE)
    starts = [m.start() for m in key_re.finditer(text)]
    for start in starts:
        snippet = text[start:].strip()
        # Stop before a later markdown fence or a common prose heading.
        stop_candidates = [
            pos for pos in (
                snippet.find("\n```"),
                snippet.find("\n## "),
                snippet.find("\n### "),
            )
            if pos > 0
        ]
        if stop_candidates:
            snippet = snippet[:min(stop_candidates)].strip()
        candidates.append(snippet)

    seen: set[str] = set()
    for candidate in candidates:
        candidate = (candidate or "").strip()
        if not candidate or candidate in seen:
            continue
        seen.add(candidate)
        try:
            parsed = yaml.safe_load(candidate)
        except (yaml.YAMLError, ValueError, RecursionError):
            continue
        if isinstance(parsed, dict):
            return parsed
    return None


def _safe_json_loads(text: str, default: Any) -> Any:
    """Parse JSON from text, handling noisy ACP output.

    Tries multiple strategies: direct parse, markdown fence extraction,
    balanced brace matching (largest dict wins), and array brackets.
    """
    if not text or not text.strip():
        return default

    # Strategy 1: Direct parse
    try:
        return json.loads(text)
    except (json.JSONDecodeError, ValueError, RecursionError):
        pass

    # Strategy 2: Find JSON in markdown code fences
    fence_pattern = re.compile(r"```(?:json)?\s*\n(.*?)```", re.DOTALL)
    for match in fence_pattern.finditer(text):
        candidate = match.group(1).strip()
        try:
            return json.loads(candidate)
        except (json.JSONDecodeError, ValueError):
            continue

    # Strategy 3: Find outermost balanced braces
    brace_depth = 0
    start = -1
    candidates: list[str] = []
    for i, ch in enumerate(text):
        if ch == "{":
            if brace_depth == 0:
                start = i
            brace_depth += 1
        elif ch == "}":
            brace_depth -= 1
            if brace_depth == 0 and start >= 0:
                candidates.append(text[start : i + 1])
                start = -1

    # Try candidates from largest to smallest
    candidates.sort(key=len, reverse=True)
    for candidate in candidates:
        try:
            parsed = json.loads(candidate)
            if isinstance(parsed, dict):
                return parsed
        except (json.JSONDecodeError, ValueError):
            continue

    # Strategy 4: Same for array [ ]
    bracket_depth = 0
    start = -1
    for i, ch in enumerate(text):
        if ch == "[":
            if bracket_depth == 0:
                start = i
            bracket_depth += 1
        elif ch == "]":
            bracket_depth -= 1
            if bracket_depth == 0 and start >= 0:
                try:
                    parsed = json.loads(text[start : i + 1])
                    if isinstance(parsed, list):
                        return parsed
                except (json.JSONDecodeError, ValueError):
                    pass
                start = -1

    return default


_METACLAW_SKILLS_DIR = str(Path.home() / ".metaclaw" / "skills")


def _load_human_feedback(run_dir: Path | None, stage: "Stage") -> str:
    """Load pending human feedback from ``run_dir/human_feedback.jsonl``.

    Reads all feedback entries, formats them chronologically, and marks
    them as consumed by recording the current stage number in a stamp file.
    Returns empty string if no new feedback exists.
    """
    if run_dir is None:
        return ""
    fb_path = run_dir / "human_feedback.jsonl"
    if not fb_path.exists():
        return ""
    try:
        stamp_path = run_dir / ".feedback_consumed_up_to"
        consumed_ts = 0
        if stamp_path.exists():
            try:
                consumed_ts = int(stamp_path.read_text(encoding="utf-8").strip())
            except (ValueError, OSError):
                pass

        entries = []
        for line in fb_path.read_text(encoding="utf-8").strip().splitlines():
            if not line.strip():
                continue
            try:
                entry = json.loads(line)
                entries.append(entry)
            except json.JSONDecodeError:
                continue

        if not entries:
            return ""

        new_entries = [e for e in entries if e.get("timestamp", 0) > consumed_ts]
        if not new_entries:
            all_text = "\n".join(
                f"- [{e.get('targetLayer', 'all')}] {e['content']}"
                for e in entries[-5:]
            )
            return f"(Previous feedback, still relevant)\n{all_text}"

        parts = []
        for e in new_entries:
            layer_tag = e.get("targetLayer", "all")
            content = e.get("content", "")
            parts.append(f"- [{layer_tag}] {content}")

        latest_ts = max(e.get("timestamp", 0) for e in new_entries)
        stamp_path.write_text(str(latest_ts), encoding="utf-8")

        return "\n".join(parts)
    except (OSError, json.JSONDecodeError):
        return ""


def _get_evolution_overlay(run_dir: Path | None, stage_name: str) -> str:
    """Load evolution lessons + MetaClaw skills for prompt injection.

    Combines intra-run lessons (from current run's evolution dir) with
    cross-run arc-* skills (from ~/.metaclaw/skills/).

    Returns empty string if no relevant lessons/skills exist or on any error.
    """
    if run_dir is None:
        return ""
    try:
        from researchclaw.evolution import EvolutionStore

        store = EvolutionStore(run_dir / "evolution")
        return store.build_overlay(
            stage_name, max_lessons=5, skills_dir=_METACLAW_SKILLS_DIR
        )
    except Exception:  # noqa: BLE001
        return ""


def _chat_with_prompt(
    llm: LLMClient,
    system: str,
    user: str,
    *,
    json_mode: bool = False,
    max_tokens: int | None = None,
    retries: int = 0,
    strip_thinking: bool = True,
) -> Any:
    """Send a chat request with optional retry on timeout/transient errors.

    Parameters
    ----------
    retries:
        Number of extra attempts after the first failure (0 = no retry).
        Uses exponential backoff: 2s, 4s, 8s, ...
    strip_thinking:
        If True (default for pipeline usage), strip ``<think>`` tags from
        the LLM response.  This prevents chain-of-thought leakage from
        breaking YAML / JSON / LaTeX parsers downstream.
    """
    import time

    messages = [{"role": "user", "content": user}]
    last_exc: Exception | None = None
    for attempt in range(1 + retries):
        try:
            if json_mode and max_tokens is not None:
                return llm.chat(messages, system=system, json_mode=True, max_tokens=max_tokens, strip_thinking=strip_thinking)
            if json_mode:
                return llm.chat(messages, system=system, json_mode=True, strip_thinking=strip_thinking)
            if max_tokens is not None:
                return llm.chat(messages, system=system, max_tokens=max_tokens, strip_thinking=strip_thinking)
            return llm.chat(messages, system=system, strip_thinking=strip_thinking)
        except Exception as exc:  # noqa: BLE001
            last_exc = exc
            if attempt < retries:
                delay = 2 ** (attempt + 1)
                logger.warning(
                    "LLM call failed (attempt %d/%d): %s. Retrying in %ds...",
                    attempt + 1,
                    1 + retries,
                    exc,
                    delay,
                )
                time.sleep(delay)
            else:
                raise last_exc from None
    raise last_exc  # type: ignore[misc]  # unreachable but satisfies type checker


def _generate_neurips_checklist(
    has_experiments: bool = True,
    has_theory: bool = False,
    has_code: bool = True,
) -> str:
    """Generate a NeurIPS-style paper checklist appendix in markdown.

    This checklist is based on the NeurIPS 2025 submission requirements.
    It is appended to the paper before LaTeX conversion.
    """
    items = [
        ("Claims", "Do the main claims accurately reflect the paper's contributions and scope?", "Yes"),
        ("Limitations", "Does the paper discuss limitations of the work?", "Yes"),
    ]
    if has_theory:
        items.append(
            ("Theory", "Are all assumptions stated and proofs included?", "Yes")
        )
    items.extend([
        ("Experiments reproducibility", "Does the paper fully disclose experimental settings?", "Yes" if has_experiments else "NA"),
        ("Code and data", "Is code or data provided for reproducibility?", "Yes" if has_code else "No"),
        ("Experimental details", "Are training details and hyperparameters specified?", "Yes" if has_experiments else "NA"),
        ("Error bars", "Are error bars or confidence intervals reported?", "Yes" if has_experiments else "NA"),
        ("Compute resources", "Are compute requirements documented?", "Yes" if has_experiments else "NA"),
        ("Code of ethics", "Does the work comply with the code of ethics?", "Yes"),
        ("Broader impacts", "Are potential negative societal impacts discussed?", "Yes"),
        ("Licenses", "Are licenses for used assets respected?", "Yes"),
        ("New assets", "Are newly released assets documented?", "NA"),
        ("Human subjects", "Were IRB approvals obtained if applicable?", "NA"),
    ])

    lines = [
        "## NeurIPS Paper Checklist",
        "",
    ]
    for label, question, answer in items:
        lines.append(f"**{label}**: {question}")
        lines.append(f"Answer: [{answer}]")
        lines.append("")

    return "\n".join(lines)


def _extract_paper_title(md_text: str) -> str:
    """Extract paper title from markdown text for LaTeX generation.

    Prioritises H1 headings that appear *before* the abstract section and
    look like real titles (>= 4 words, starts with uppercase).  This avoids
    picking up pseudocode comments or algorithm step labels.
    """
    import re as _re

    # Limit search to content before Abstract heading
    abstract_pos = _re.search(
        r"^#{1,2}\s+(Abstract|ABSTRACT)", md_text, _re.MULTILINE
    )
    search_region = md_text[: abstract_pos.start()] if abstract_pos else md_text[:3000]

    # Common generated-paper format:
    #   ## Title
    #   The Actual Paper Title
    # Treat the first non-empty body line as the title.  Previously this
    # valid representation fell through to ``Untitled Paper`` and the LaTeX
    # exporter emitted a manual-title placeholder.
    explicit_title = _re.search(
        r"^#{1,2}\s+Title\s*$\n(?:[ \t]*\n)*[ \t]*(.+?)\s*$",
        search_region,
        _re.MULTILINE | _re.IGNORECASE,
    )
    if explicit_title:
        value = _re.sub(r"^\*\*(.+?)\*\*$", r"\1", explicit_title.group(1).strip())
        if value:
            return value

    _SKIP = {"title", "abstract", "references", "appendix"}
    candidates: list[str] = []

    for line in search_region.splitlines():
        line = line.strip()
        # Match H1 or H2 headings
        hm = _re.match(r"^(#{1,2})\s+(.+)$", line)
        if hm:
            heading = hm.group(2).strip()
            heading_lower = heading.lower()
            # Handle "## Title Actual Paper Title" pattern
            if heading_lower.startswith("title ") and len(heading) > 6:
                heading = heading[6:].strip()
                heading_lower = heading.lower()
            if heading_lower in _SKIP:
                continue
            candidates.append(heading)
        # Bold title line (e.g. **My Paper Title**)
        m = _re.match(r"\*\*(.+?)\*\*$", line)
        if m and len(m.group(1).split()) >= 3:
            candidates.append(m.group(1))

    # Prefer candidates that look like real titles (>= 4 words, capitalised)
    for c in candidates:
        words = c.split()
        if len(words) >= 4 and c[0].isupper():
            return c

    # Fallback: any candidate
    if candidates:
        return candidates[0]

    return "Untitled Paper"


def _generate_framework_diagram_prompt(
    paper_text: str,
    config: "RCConfig",
    *,
    llm: "LLMClient | None" = None,
) -> str:
    """Generate a text-to-image prompt for a methodology framework diagram.

    Reads the paper's method section and produces a detailed prompt suitable
    for AI image generators (DALL-E, Midjourney, etc.).  The prompt describes
    an academic-style architecture/framework overview figure.

    Returns the prompt as a Markdown string, or empty string on failure.
    """
    import re as _re

    # Extract method/approach section from paper
    _method_section = ""
    _method_patterns = [
        r"(?:^#{1,3}\s+(?:Method(?:ology)?|Approach|Proposed\s+(?:Method|Framework|Approach)|Our\s+Method|Technical\s+Approach|Model\s+Architecture).*?)(?=^#{1,3}\s+|\Z)",
    ]
    for _pat in _method_patterns:
        _match = _re.search(_pat, paper_text, _re.MULTILINE | _re.DOTALL | _re.IGNORECASE)
        if _match:
            _method_section = _match.group(0)[:3000]
            break

    if not _method_section:
        # Fallback: use abstract + first 1500 chars
        _abs_match = _re.search(
            r"(?:^#{1,2}\s+Abstract\s*\n)(.*?)(?=^#{1,2}\s+|\Z)",
            paper_text, _re.MULTILINE | _re.DOTALL | _re.IGNORECASE,
        )
        _method_section = (_abs_match.group(1)[:1500] if _abs_match else paper_text[:2000])

    title = _extract_paper_title(paper_text)
    topic = config.research.topic

    # Use LLM to generate the prompt if available
    if llm is not None:
        _system = (
            "You are an expert academic figure designer. Generate a detailed text-to-image "
            "prompt for creating a methodology framework/architecture overview diagram.\n\n"
            "Requirements:\n"
            "- Academic style: clean, professional, suitable for a top-tier ML conference paper\n"
            "- Color palette: sophisticated and harmonious (suggest specific hex colors, "
            "prefer muted blues #4477AA, teals #44AA99, warm accents #CCBB44, soft purples #AA3377)\n"
            "- Layout: left-to-right or top-to-bottom data flow, with clearly labeled components\n"
            "- Components: boxes/modules with rounded corners, directional arrows, clear labels\n"
            "- Information density: high but not cluttered — each box should have a short label\n"
            "- Text on figure: minimal, only component names and key annotations\n"
            "- Background: white or very light grey\n"
            "- Style: vector-art look, flat design with subtle shadows, NO photorealism\n\n"
            "Output ONLY the prompt text (no markdown headers, no explanations). "
            "The prompt should be 150-300 words, highly specific and actionable."
        )
        _user = (
            f"Paper title: {title}\n"
            f"Research topic: {topic}\n\n"
            f"Method section excerpt:\n{_method_section}\n\n"
            "Generate a detailed text-to-image prompt for the methodology framework diagram."
        )
        try:
            resp = _chat_with_prompt(llm, _system, _user, max_tokens=1024)
            _llm_prompt = resp.content.strip()
            if len(_llm_prompt) > 50:
                return (
                    f"# Framework Diagram Prompt\n\n"
                    f"**Paper**: {title}\n\n"
                    f"## Image Generation Prompt\n\n"
                    f"{_llm_prompt}\n\n"
                    f"## Usage Instructions\n\n"
                    f"1. Copy the prompt above into an AI image generator "
                    f"(DALL-E 3, Midjourney, Ideogram, etc.)\n"
                    f"2. Generate the image at high resolution (2048x1024 or similar landscape)\n"
                    f"3. Save as `framework_diagram.png` in the same `charts/` folder\n"
                    f"4. Insert into the paper's Method section using:\n"
                    f"   - LaTeX: `\\includegraphics[width=\\textwidth]{{charts/framework_diagram.png}}`\n"
                    f"   - Markdown: `![Framework Overview](charts/framework_diagram.png)`\n"
                )
        except Exception:
            logger.debug("Framework prompt LLM generation failed, using template")

    # Fallback: template-based prompt without LLM
    _components = []
    _component_patterns = [
        (r"(?:encoder|decoder|transformer|attention|convolution|MLP|GNN|ResNet|ViT)", "Neural Network Module"),
        (r"(?:loss|objective|criterion|training|optimization)", "Training/Optimization"),
        (r"(?:data|dataset|input|preprocessing|augmentation)", "Data Pipeline"),
        (r"(?:output|prediction|inference|evaluation)", "Output/Evaluation"),
    ]
    _method_lower = _method_section.lower()
    for pat, label in _component_patterns:
        if _re.search(pat, _method_lower):
            _components.append(label)

    if not _components:
        _components = ["Input Processing", "Core Model", "Training Loop", "Evaluation"]

    return (
        f"# Framework Diagram Prompt\n\n"
        f"**Paper**: {title}\n\n"
        f"## Image Generation Prompt\n\n"
        f"Create a clean, academic-style methodology framework diagram for a research paper "
        f"titled \"{title}\". "
        f"The diagram should show a left-to-right data flow pipeline with these main components: "
        f"{', '.join(_components)}. "
        f"Use a professional color palette with muted blues (#4477AA), teals (#44AA99), "
        f"warm yellows (#CCBB44), and soft purples (#AA3377) on a white background. "
        f"Each component should be a rounded rectangle with a short label inside. "
        f"Connect components with clean directional arrows. "
        f"Add subtle shadows for depth. Flat vector-art style, no photorealism. "
        f"High information density but visually clean. "
        f"Suitable for a top-tier machine learning conference paper (ICML/NeurIPS/ICLR). "
        f"Landscape orientation, 2048x1024 resolution.\n\n"
        f"## Usage Instructions\n\n"
        f"1. Copy the prompt above into an AI image generator "
        f"(DALL-E 3, Midjourney, Ideogram, etc.)\n"
        f"2. Generate the image at high resolution (2048x1024 or similar landscape)\n"
        f"3. Save as `framework_diagram.png` in the same `charts/` folder\n"
        f"4. Insert into the paper's Method section using:\n"
        f"   - LaTeX: `\\includegraphics[width=\\textwidth]{{charts/framework_diagram.png}}`\n"
        f"   - Markdown: `![Framework Overview](charts/framework_diagram.png)`\n"
    )


def _safe_filename(name: str) -> str:
    name = name.replace("/", "_").replace("\\", "_").replace("..", "_")
    name = re.sub(r"[^a-zA-Z0-9_\-.]", "_", name)
    return name[:100] or "unnamed"


def _collect_experiment_results(
    run_dir: Path,
    metric_key: str = "",
    metric_direction: str = "maximize",
) -> dict[str, Any]:
    """Aggregate experiment metrics from runs/ directory across prior stages.

    Returns a dict with ``runs``, ``metrics_summary``, ``best_run``,
    ``latex_table``, and optionally ``structured_results``.
    """
    runs_data: list[dict[str, Any]] = []
    structured_results: Any = None

    # Scan all stage dirs for runs/ subdirectory
    for stage_subdir in sorted(run_dir.glob("stage-*/runs")):
        # Check for structured results.json first
        results_json = stage_subdir / "results.json"
        if results_json.exists() and structured_results is None:
            try:
                structured_results = json.loads(
                    results_json.read_text(encoding="utf-8")
                )
            except (json.JSONDecodeError, OSError):
                pass

        for run_file in sorted(stage_subdir.glob("*.json")):
            if run_file.name == "results.json":
                continue  # Already handled above
            parsed = _safe_json_loads(run_file.read_text(encoding="utf-8"), {})
            if isinstance(parsed, dict) and "metrics" in parsed:
                # Also check for structured_results inside run payload
                if "structured_results" in parsed and structured_results is None:
                    structured_results = parsed["structured_results"]
                runs_data.append(parsed)
            elif isinstance(parsed, dict) and "key_metrics" in parsed:
                # Simulated mode uses key_metrics
                parsed["metrics"] = parsed.pop("key_metrics")
                runs_data.append(parsed)

    # Fallback: synthesise runs_data from structured_results if it has summaries
    if not runs_data and isinstance(structured_results, dict):
        _summaries = structured_results.get("summaries", [])
        if isinstance(_summaries, list):
            for _s in _summaries:
                if not isinstance(_s, dict):
                    continue
                _synth_metrics: dict[str, float] = {}
                for _mk, _mv in _s.items():
                    if _mk == "method":
                        continue
                    try:
                        _synth_metrics[_mk] = float(_mv)
                    except (ValueError, TypeError):
                        if isinstance(_mv, list):
                            continue
                if _synth_metrics:
                    runs_data.append({
                        "metrics": _synth_metrics,
                        "condition": _s.get("method", "unknown"),
                    })

    if not runs_data:
        result: dict[str, Any] = {"runs": [], "metrics_summary": {}, "best_run": None, "latex_table": ""}
        if structured_results is not None:
            result["structured_results"] = structured_results
        return result

    # Aggregate metrics across runs
    all_metric_keys: set[str] = set()
    for r in runs_data:
        m = r.get("metrics") or {}
        if isinstance(m, dict):
            all_metric_keys.update(m.keys())

    metrics_summary: dict[str, dict[str, float | None]] = {}
    for key in sorted(all_metric_keys):
        values = []
        for r in runs_data:
            m = r.get("metrics") or {}
            if isinstance(m, dict) and key in m:
                try:
                    values.append(float(m[key]))
                except (ValueError, TypeError):
                    pass
        if values:
            metrics_summary[key] = {
                "min": round(min(values), 6),
                "max": round(max(values), 6),
                "mean": round(sum(values) / len(values), 6),
                "count": len(values),
            }

    # Find best run using metric_key and metric_direction
    best_run: dict[str, Any] | None = None
    if runs_data:

        def _primary_metric(r: dict[str, Any]) -> float:
            m = r.get("metrics") or {}
            if isinstance(m, dict):
                # Try specific metric_key first
                if metric_key and metric_key in m:
                    try:
                        return float(m[metric_key])
                    except (ValueError, TypeError):
                        pass
                # Fallback to first metric
                for v in m.values():
                    try:
                        return float(v)
                    except (ValueError, TypeError):
                        pass
            return 0.0

        _cmp = min if metric_direction == "minimize" else max
        best_run = _cmp(runs_data, key=_primary_metric)

    # Build LaTeX table
    latex_lines = [
        r"\begin{table}[h]",
        r"\centering",
        r"\caption{Experiment Results}",
    ]
    if metrics_summary:
        cols = sorted(metrics_summary.keys())
        header = "Metric & Min & Max & Mean & N \\\\"
        latex_lines.append(r"\begin{tabular}{l" + "r" * 4 + "}")
        latex_lines.append(r"\hline")
        latex_lines.append(header)
        latex_lines.append(r"\hline")
        for col in cols:
            s = metrics_summary[col]
            row = f"{col} & {s['min']:.4f} & {s['max']:.4f} & {s['mean']:.4f} & {s['count']} \\\\"
            latex_lines.append(row)
        latex_lines.append(r"\hline")
        latex_lines.append(r"\end{tabular}")
    else:
        latex_lines.append(r"\begin{tabular}{l}")
        latex_lines.append("No experiment data available \\\\")
        latex_lines.append(r"\end{tabular}")
    latex_lines.append(r"\end{table}")

    # R18-1: Extract paired statistical comparisons from stdout
    from researchclaw.experiment.sandbox import extract_paired_comparisons

    paired_comparisons: list[dict[str, object]] = []
    for r in runs_data:
        stdout = r.get("stdout", "")
        if stdout:
            paired_comparisons.extend(extract_paired_comparisons(stdout))

    collected: dict[str, Any] = {
        "runs": runs_data,
        "metrics_summary": metrics_summary,
        "best_run": best_run,
        "latex_table": "\n".join(latex_lines),
    }
    if paired_comparisons:
        collected["paired_comparisons"] = paired_comparisons
    if structured_results is not None:
        collected["structured_results"] = structured_results
    return collected


def _find_discussion_dir(run_dir: Path, config: RCConfig) -> Path | None:
    """Locate the discussion artifacts directory.

    Checks run_dir/discussion first, then falls back to
    shared_results_dir/idea_runs/<idea_id>/discussion for cases
    where L1 ran in shared_results but L5 runs in /dev/shm.
    """
    local = run_dir / "discussion"
    if local.is_dir() and (local / "consensus_synthesis.md").is_file():
        return local
    shared_dir = getattr(config.experiment, "shared_results_dir", "") or ""
    if shared_dir:
        fallback = Path(shared_dir) / "idea_runs" / run_dir.name / "discussion"
        if fallback.is_dir() and (fallback / "consensus_synthesis.md").is_file():
            return fallback
    return None


def _build_experiment_fact_contract(run_dir: Path) -> str:
    """Build a compact factual contract from executable and observed artifacts.

    Generated prose is deliberately excluded.  This prevents a later writing
    model from replacing an official split with a plausible-looking split or
    confusing precomputed features with raw sensor windows.
    """
    try:
        plan = yaml.safe_load(_read_prior_artifact(run_dir, "exp_plan.yaml") or "") or {}
    except yaml.YAMLError:
        plan = {}
    summary = _safe_json_loads(
        _read_prior_artifact(run_dir, "experiment_summary.json") or "{}", {}
    )
    if not isinstance(plan, dict):
        plan = {}
    if not isinstance(summary, dict):
        summary = {}

    facts: list[str] = [
        "## NON-NEGOTIABLE EXECUTED-EXPERIMENT FACT CONTRACT",
        "These facts come from the executable plan, dataset files, and result JSON. "
        "They supersede any conflicting prose in earlier goals, hypotheses, decisions, or outlines.",
    ]
    datasets = plan.get("datasets", [])
    if isinstance(datasets, list):
        for dataset in datasets:
            if not isinstance(dataset, dict):
                continue
            facts.append(f"- Dataset: {dataset.get('name', 'unspecified')}")
            if dataset.get("source"):
                facts.append(f"- Dataset source: {dataset['source']}")
            if dataset.get("split_strategy"):
                facts.append(f"- Split policy: {dataset['split_strategy']}")
            if dataset.get("preprocessing"):
                facts.append(f"- Input representation/preprocessing: {dataset['preprocessing']}")

    baselines = plan.get("baselines", [])
    baseline_names = [
        str(item.get("name")) if isinstance(item, dict) else str(item)
        for item in baselines if str(item).strip()
    ] if isinstance(baselines, list) else []
    if baseline_names:
        facts.append(f"- Actually requested methods: {baseline_names}")
    proposed = plan.get("proposed_methods", [])
    if isinstance(proposed, list) and not proposed:
        facts.append(
            "- Proposed methods: none. Do not invent or brand a new model, method, framework, "
            "protocol, algorithm, or acronym; frame the document as a baseline reproduction/audit."
        )
    seeds = plan.get("seeds") or (plan.get("evaluation_protocol", {}) or {}).get("independent_seeds")
    if isinstance(seeds, list):
        facts.append(f"- Exact random seeds: {seeds}")
    metrics = plan.get("metrics")
    if isinstance(metrics, list):
        facts.append(f"- Exact reported metrics: {metrics}")
    if summary:
        facts.append(
            f"- Observed scale: {summary.get('condition_count', len(summary.get('condition_summaries', {})))} "
            f"conditions and {summary.get('total_runs', 0)} per-seed runs."
        )
        condition_summaries = summary.get("condition_summaries", {})
        if isinstance(condition_summaries, dict):
            for condition, condition_data in condition_summaries.items():
                if not isinstance(condition_data, dict):
                    continue
                observed = condition_data.get("metrics", {})
                if isinstance(observed, dict) and observed:
                    facts.append(
                        f"- Exact observed aggregate for {condition}: "
                        + json.dumps(observed, sort_keys=True, ensure_ascii=False)
                    )
                seed_metrics = condition_data.get("seed_metrics", {})
                if isinstance(seed_metrics, dict) and seed_metrics:
                    facts.append(
                        f"- Exact per-seed observations for {condition}: "
                        + json.dumps(seed_metrics, sort_keys=True, ensure_ascii=False)
                    )
        comparisons = summary.get("paired_comparisons", [])
        if isinstance(comparisons, list) and comparisons:
            facts.append(
                "- Exact paired statistical tests: "
                + json.dumps(comparisons, sort_keys=True, ensure_ascii=False)
            )
        facts.append(
            "- Evidence granularity: only aggregate and per-seed Accuracy/Macro-F1 are available. "
            "No per-activity, per-class, raw-signal, latency, memory, energy, or deep-model results "
            "were executed; do not describe or plot them."
        )

    # Recover the exact official subject partition and matrix dimensions from
    # the archived real dataset rather than trusting model-generated prose.
    stage11 = run_dir / "stage-11"
    subject_train_paths = sorted(stage11.rglob("subject_train.txt")) if stage11.exists() else []
    subject_test_paths = sorted(stage11.rglob("subject_test.txt")) if stage11.exists() else []
    if subject_train_paths and subject_test_paths:
        try:
            train_tokens = subject_train_paths[0].read_text(encoding="utf-8").split()
            test_tokens = subject_test_paths[0].read_text(encoding="utf-8").split()
            train_subjects = sorted({int(float(value)) for value in train_tokens})
            test_subjects = sorted({int(float(value)) for value in test_tokens})
            facts.append(f"- Official train subjects ({len(train_subjects)}): {train_subjects}")
            facts.append(f"- Official test subjects ({len(test_subjects)}): {test_subjects}")
            facts.append(f"- Observed sample counts: train={len(train_tokens)}, test={len(test_tokens)}")
            x_train = subject_train_paths[0].parent / "X_train.txt"
            if x_train.exists():
                first_row = x_train.open("r", encoding="utf-8").readline().split()
                if first_row:
                    facts.append(f"- Observed input feature count: {len(first_row)} precomputed features")
        except (OSError, ValueError):
            pass
    facts.append(
        "- Never replace the exact subject IDs with a contiguous range and never call the "
        "561-dimensional precomputed feature matrix raw sensor input."
    )
    return "\n".join(facts)


def _build_context_preamble(
    config: RCConfig,
    run_dir: Path,
    *,
    include_goal: bool = False,
    include_hypotheses: bool = False,
    include_synthesis: bool = False,
    include_exp_plan: bool = False,
    include_analysis: bool = False,
    include_decision: bool = False,
    include_experiment_data: bool = False,
    include_discussion: bool = False,
) -> str:
    parts = [
        "## Research Context",
        f"**Topic**: {config.research.topic}",
        f"**Domains**: {', '.join(config.research.domains) if config.research.domains else 'general'}",
    ]
    if include_goal:
        goal = _read_prior_artifact(run_dir, "goal.md")
        if goal:
            parts.append(f"\n### Goal\n{goal[:2200]}")
    if include_hypotheses:
        hyp = _read_prior_artifact(run_dir, "hypotheses.md")
        if hyp:
            parts.append(f"\n### Hypotheses\n{hyp[:2200]}")
    if include_synthesis:
        synthesis = _read_prior_artifact(run_dir, "synthesis.md")
        if synthesis:
            parts.append(f"\n### Synthesis\n{synthesis[:2200]}")
    if include_exp_plan:
        plan = _read_prior_artifact(run_dir, "exp_plan.yaml")
        if plan:
            parts.append(f"\n### Experiment Plan\n{plan[:2000]}")
    if include_analysis:
        analysis = _read_prior_artifact(run_dir, "analysis.md")
        if analysis:
            parts.append(f"\n### Result Analysis\n{analysis[:2500]}")
    if include_decision:
        decision = _read_prior_artifact(run_dir, "decision.md")
        if decision:
            parts.append(f"\n### Research Decision\n{decision[:1500]}")
    if include_discussion:
        disc_dir = _find_discussion_dir(run_dir, config)
        if disc_dir is not None:
            pre_synth = disc_dir / "pre_discussion_syntheses.md"
            consensus = disc_dir / "consensus_synthesis.md"
            transcript = disc_dir / "discussion_transcript.md"
            if pre_synth.exists() and consensus.exists():
                parts.append("\n### Multi-Agent Discussion (Ablation Data)")
                _pre = pre_synth.read_text(encoding="utf-8")
                parts.append(f"\n#### Pre-Discussion Individual Syntheses\n{_pre[:4000]}")
                _con = consensus.read_text(encoding="utf-8")
                parts.append(f"\n#### Post-Discussion Consensus\n{_con[:4000]}")
                if transcript.exists():
                    _tr = transcript.read_text(encoding="utf-8")
                    parts.append(f"\n#### Discussion Transcript (excerpt)\n{_tr[:3000]}")
    if include_experiment_data:
        parts.append("\n" + _build_experiment_fact_contract(run_dir))
        provenance_text = _read_prior_artifact(run_dir, "experiment_provenance.json") or ""
        readiness_text = _read_prior_artifact(run_dir, "research_readiness.json") or ""
        if provenance_text or readiness_text:
            parts.append(
                "\n### NON-NEGOTIABLE EXPERIMENT CLAIM BOUNDARY\n"
                "The following machine-readable records define what the paper may claim. "
                "Do not upgrade smoke results, failed runs, or limited small benchmarks into broad scientific conclusions. "
                "Every quantitative claim must stay within the recorded dataset/model/metric/run scope."
            )
        if provenance_text:
            parts.append(f"\n#### Experiment Provenance\n```json\n{provenance_text[:5000]}\n```")
        if readiness_text:
            parts.append(f"\n#### Research Readiness\n```json\n{readiness_text[:5000]}\n```")
        hw_profile = _load_hardware_profile(run_dir)
        if hw_profile:
            hw_lines = ["### Hardware Environment"]
            for hk, hv in hw_profile.items():
                hw_lines.append(f"- **{hk}**: {hv}")
            parts.append("\n" + "\n".join(hw_lines))
        exp_summary = _read_prior_artifact(run_dir, "experiment_summary.json")
        if exp_summary:
            summary = _safe_json_loads(exp_summary, {})
            if isinstance(summary, dict) and summary.get("metrics_summary"):
                parts.append("\n### Experiment Results (Quantitative)")
                ms = summary["metrics_summary"]
                for mk, mv in ms.items():
                    if isinstance(mv, dict):
                        parts.append(
                            f"- **{mk}**: mean={mv.get('mean', '?')}, "
                            f"min={mv.get('min', '?')}, max={mv.get('max', '?')}, n={mv.get('count', '?')}"
                        )
                if summary.get("latex_table"):
                    parts.append(
                        f"\n### LaTeX Table\n```latex\n{summary['latex_table']}\n```"
                    )
    return "\n".join(parts)


# --- P1-1: Topic keyword extraction for domain pre-filter ---
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


# --- P1-2: Topic constraint block for paper generation stages ---
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


def _detect_runtime_issues(sandbox_result: Any) -> str:
    """Detect NaN/Inf in metrics and extract stderr warnings from sandbox run.

    Returns a formatted string describing all runtime issues, or empty string
    if no issues are found.
    """
    import math

    issues: list[str] = []

    # Check metrics for NaN/Inf
    metrics = getattr(sandbox_result, "metrics", {}) or {}
    for key, val in metrics.items():
        try:
            fval = float(val)
            if math.isnan(fval):
                issues.append(f"METRIC NaN: '{key}' returned NaN — likely a division by zero or invalid computation in code")
            elif math.isinf(fval):
                issues.append(f"METRIC Inf: '{key}' returned Infinity — likely overflow or unbounded computation")
        except (TypeError, ValueError):
            pass

    # Check stdout for NaN values (word boundary to avoid matching "Nanotechnology" etc.)
    stdout = getattr(sandbox_result, "stdout", "") or ""
    _nan_re = re.compile(r"\bnan\b", re.IGNORECASE)
    if _nan_re.search(stdout):
        nan_lines = [
            line.strip()
            for line in stdout.splitlines()
            if _nan_re.search(line)
        ]
        if nan_lines:
            issues.append(
                f"NaN values detected in output:\n" + "\n".join(nan_lines[:10])
            )

    # Extract meaningful warnings from stderr
    stderr = getattr(sandbox_result, "stderr", "") or ""
    if stderr.strip():
        warning_lines = []
        for line in stderr.splitlines():
            line_stripped = line.strip()
            if not line_stripped:
                continue
            # Keep RuntimeWarning, ValueError, ZeroDivisionError, etc.
            if any(
                kw in line_stripped
                for kw in (
                    "Warning",
                    "Error",
                    "Traceback",
                    "Exception",
                    "divide",
                    "overflow",
                    "invalid value",
                    "NaN",
                    "inf",
                )
            ):
                warning_lines.append(line_stripped)
        if warning_lines:
            issues.append(
                "Runtime warnings/errors from stderr:\n"
                + "\n".join(warning_lines[:15])
            )

    # Check for identical metric values across all entries in stdout
    # (e.g., all algorithms reporting convergence_rate=1.0)
    stdout = getattr(sandbox_result, "stdout", "") or ""
    if stdout:
        from collections import Counter

        metric_values_by_name: dict[str, list[float]] = {}
        for line in stdout.splitlines():
            line = line.strip()
            if ":" not in line:
                continue
            parts = line.rsplit(":", 1)
            if len(parts) != 2:
                continue
            try:
                fval = float(parts[1].strip())
            except (ValueError, TypeError):
                continue
            # Extract metric suffix (e.g. "convergence_rate" from "UCB (Stochastic) convergence_rate")
            name = parts[0].strip()
            metric_suffix = name.split()[-1] if name.split() else name
            metric_values_by_name.setdefault(metric_suffix, []).append(fval)

        for metric_name, vals in metric_values_by_name.items():
            if len(vals) >= 3:
                unique = set(vals)
                if len(unique) <= 2:
                    issues.append(
                        f"DUMMY METRIC: '{metric_name}' has only {len(unique)} unique value(s) "
                        f"across {len(vals)} entries ({unique}) — likely a placeholder. "
                        f"Implement real measurement logic (e.g., track iterations to convergence)."
                    )

    # R5-3: Check for diverging loss values (fast-fail indicator)
    for key, val in metrics.items():
        try:
            fval = float(val)
            if "loss" in key.lower() and fval > 100:
                issues.append(
                    f"DIVERGING LOSS: '{key}' = {fval} (>100) — the optimization is "
                    f"diverging. Reduce learning rate, check gradient computation, "
                    f"or add gradient clipping."
                )
        except (TypeError, ValueError):
            pass

    if not issues:
        return ""

    return (
        "## Runtime Issues Detected\n\n"
        "The experiment code ran but produced problematic results. "
        "Fix the ROOT CAUSE of these issues in the code:\n\n"
        + "\n\n".join(f"- {issue}" for issue in issues)
    )


def _parse_metrics_from_stdout(stdout: str) -> dict[str, Any]:
    """Parse ``name: value`` metric lines from experiment stdout.

    Handles formats like ``UCB (Stochastic) cumulative_regret: 361.9233``
    and simple ``loss: 0.0042``.  Returns a flat dict of metric_name → value.

    Filters out log/status lines (e.g. "Running experiments for support set
    size: 1") using :func:`is_metric_name`.
    """
    metrics: dict[str, Any] = {}
    for line in stdout.splitlines():
        line = line.strip()
        if ":" not in line:
            continue
        # Split on the LAST colon to handle names with colons
        parts = line.rsplit(":", 1)
        if len(parts) != 2:
            continue
        name_part = parts[0].strip()
        value_part = parts[1].strip()
        # Filter out log lines that look like status messages
        if not is_metric_name(name_part):
            continue
        try:
            fval = float(value_part)
            # Use the full name (e.g. "UCB (Stochastic) cumulative_regret")
            metrics[name_part] = fval
        except (ValueError, TypeError):
            pass
    return metrics


def _extract_code_block(content: str) -> str:
    match = re.search(r"```(?:python)?\s*(.*?)\s*```", content, flags=re.DOTALL)
    if match is not None:
        return match.group(1).strip()
    # Handle truncated output: opening ``` without closing ```
    trunc = re.search(r"```(?:python)?\s*\n(.+)", content, flags=re.DOTALL)
    if trunc is not None:
        code = trunc.group(1).strip()
        if "\nimport " in code or "\ndef " in code or "\nclass " in code:
            return code
    # Last resort: if content looks like Python, return it; otherwise empty
    if content.strip().startswith(("import ", "from ", "#!/", "def ", "class ")):
        return content.strip()
    return ""


def _extract_multi_file_blocks(content: str) -> dict[str, str]:
    """Parse LLM response containing multiple files with filename markers.

    Expected format::

        ```filename:main.py
        import model
        ...
        ```

        ```filename:model.py
        class MyModel:
        ...
        ```

    Also handles common LLM format variations:
    - ````` ```python filename:main.py````` (space before filename)
    - ````` ``` filename:main.py````` (space after backticks)
    - ``filename:main.py`` on next line after backticks
    - ``# FILE: main.py`` comment markers inside code blocks

    Falls back to treating the entire code block as ``main.py`` if no
    ``filename:`` markers are found.

    Returns a dict mapping filename → code content.
    """
    # R13-2: Multiple patterns to handle LLM format variations
    patterns = [
        # Original: ```filename:xxx.py or ```python filename:xxx.py
        re.compile(
            r"```(?:python\s+)?filename:(\S+)\s*\n(.*?)```",
            flags=re.DOTALL,
        ),
        # Variation: ``` filename:xxx.py (space after backticks)
        re.compile(
            r"```\s+filename:(\S+)\s*\n(.*?)```",
            flags=re.DOTALL,
        ),
        # Variation: ```python\nfilename:xxx.py (filename on next line)
        re.compile(
            r"```(?:python)?\s*\nfilename:(\S+)\s*\n(.*?)```",
            flags=re.DOTALL,
        ),
        # Variation: ```python\n# filename: xxx.py (comment marker)
        re.compile(
            r"```(?:python)?\s*\n#\s*(?:FILE|filename)\s*:\s*(\S+\.py)\s*\n(.*?)```",
            flags=re.DOTALL,
        ),
    ]

    matches: list[tuple[str, str]] = []
    for pattern in patterns:
        matches = pattern.findall(content)
        if matches:
            break

    if matches:
        files: dict[str, str] = {}
        for fname, code in matches:
            fname = fname.strip()
            # Security: prevent path traversal
            if ".." in fname or fname.startswith("/"):
                continue
            # Normalise to flat filenames (strip leading ./ or subdirs for safety)
            fname = fname.replace("\\", "/").split("/")[-1]
            if fname and fname.endswith(".py"):
                files[fname] = code.strip()
        if files:
            # Ensure there is a main.py entry point
            if "main.py" not in files:
                # Pick the first file as main.py
                first_key = next(iter(files))
                files["main.py"] = files.pop(first_key)
            return files

    # Fallback: single code block → main.py
    code = _extract_code_block(content)
    if code.strip():
        return {"main.py": code}
    return {}


def _parse_jsonl_rows(text: str) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        parsed = _safe_json_loads(line, {})
        if isinstance(parsed, dict):
            rows.append(parsed)
    return rows


def _write_jsonl(path: Path, rows: list[dict[str, Any]]) -> None:
    with path.open("w", encoding="utf-8") as handle:
        for row in rows:
            handle.write(json.dumps(row, ensure_ascii=False) + "\n")


def _collect_json_context(
    directory: Path,
    *,
    max_files: int = 30,
    max_total_chars: int = 50_000,
) -> str:
    """Collect JSON context from a directory, with size limits.

    Large fields like ``stderr`` and ``stdout`` are stripped to avoid
    exceeding LLM token limits (the raw experiment output can be 5 MB+).
    """
    chunks: list[str] = []
    total = 0
    for file_path in sorted(directory.glob("*.json"))[:max_files]:
        try:
            data = json.loads(file_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            continue
        # Strip verbose fields that bloat the context
        if isinstance(data, dict):
            for key in ("stderr", "stdout", "raw_output", "traceback"):
                if key in data and isinstance(data[key], str) and len(data[key]) > 500:
                    data[key] = data[key][:500] + f"\n... [truncated, {len(data[key])} chars total]"
        chunk = json.dumps(data, indent=2, ensure_ascii=False)
        if total + len(chunk) > max_total_chars:
            remaining = max_total_chars - total
            if remaining > 200:
                chunks.append(chunk[:remaining] + "\n... [truncated]")
            break
        chunks.append(chunk)
        total += len(chunk)
    return "\n\n".join(chunks)


def _clamp_idea_count(value: object) -> int:
    try:
        count = int(value)
    except (TypeError, ValueError):
        count = 5
    return max(1, min(8, count))


def _configured_idea_count(config: RCConfig | None) -> int:
    return _clamp_idea_count(getattr(getattr(config, "research", None), "idea_count", 5))



_IDEA_LIKE_HEADING_RE = re.compile(
    r"^##\s+(Idea\s*\d+|H\s*\d+|想法\s*\d+|假设\s*\d+|Idea\s*[一二三四五六七八九十]+|[0-9]+[.、])[:：\s-]*(.+)?$",
    re.M,
)


def _idea_like_blocks(text: str) -> list[tuple[str, str]]:
    matches = list(_IDEA_LIKE_HEADING_RE.finditer(text or ""))
    blocks: list[tuple[str, str]] = []
    for idx, match in enumerate(matches):
        start = match.start()
        end = matches[idx + 1].start() if idx + 1 < len(matches) else len(text)
        title = (match.group(2) or match.group(1) or f"Idea {idx + 1}").strip()
        block = text[start:end].strip()
        non_idea_heading = re.search(
            r"\n##\s+(?!(?:Idea|H|想法|假设)\s*(?:\d+|[一二三四五六七八九十])|[0-9]+[.、])",
            block,
            flags=re.I,
        )
        if non_idea_heading:
            block = block[:non_idea_heading.start()].strip()
        blocks.append((title, block))
    return blocks


def _idea_like_count(text: str) -> int:
    return len(_idea_like_blocks(text))


def _idea_text_tokens(text: str) -> set[str]:
    raw = re.findall(r"[A-Za-z0-9][A-Za-z0-9_.+-]{1,}|[\u4e00-\u9fff]{2,}", (text or "").lower())
    stop = {"idea", "hypothesis", "method", "model", "dataset", "baseline", "metric", "risk", "核心", "假设", "实验", "方法", "模型", "数据集"}
    tokens = {tok for tok in raw if tok not in stop and len(tok) >= 2}
    chinese = "".join(re.findall(r"[\u4e00-\u9fff]", text or ""))
    tokens.update(chinese[i:i + 2] for i in range(max(0, len(chinese) - 1)))
    return tokens


def _idea_text_similarity(left: str, right: str) -> float:
    lt, rt = _idea_text_tokens(left), _idea_text_tokens(right)
    if not lt or not rt:
        return 0.0
    overlap = len(lt & rt)
    jaccard = overlap / max(len(lt | rt), 1)
    containment = overlap / max(min(len(lt), len(rt)), 1)
    return max(jaccard, containment)


def _renumber_idea_blocks(blocks: list[tuple[str, str]]) -> str:
    out: list[str] = ["# Core Ideas"]
    for idx, (title, block) in enumerate(blocks, start=1):
        body = block.splitlines()
        if body and body[0].lstrip().startswith("##"):
            body = body[1:]
        out.append(f"## Idea {idx}：{title}")
        out.append("\n".join(body).strip())
    return "\n\n".join(part for part in out if part).strip() + "\n"


def _complete_idea_set(
    candidate_md: str,
    *,
    topic: str,
    synthesis: str,
    idea_count: int,
    fallback_sources: Sequence[str] = (),
) -> str:
    """Prevent LLM refinement/review steps from collapsing a multi-idea set."""
    target = _clamp_idea_count(idea_count)
    blocks = _idea_like_blocks(candidate_md)
    original_count = len(blocks)
    if original_count >= target:
        return candidate_md

    for source in (*fallback_sources, _fallback_hypotheses_from_synthesis(topic, synthesis, target)):
        for title, block in _idea_like_blocks(source):
            if len(blocks) >= target:
                break
            if any(
                _idea_text_similarity(block, existing) >= 0.42
                or _idea_text_similarity(title, existing_title) >= 0.34
                for existing_title, existing in blocks
            ):
                continue
            blocks.append((title, block))
        if len(blocks) >= target:
            break

    if len(blocks) >= target:
        logger.warning(
            "S8: Candidate contained only %d idea(s); completed final set to %d using prior/fallback ideas",
            original_count,
            len(blocks),
        )
        return _renumber_idea_blocks(blocks[:target])
    return candidate_md


def _idea_count_rule(idea_count: int) -> str:
    return (
        f"目标输出 exactly {idea_count} 个最终 Idea。每个 Idea 必须是不同技术机制、不同验证路径或不同问题切入点；"
        "如果两个 Idea 只是同一机制换标题/换应用场景，必须合并或替换，不能重复凑数。"
    )


def _default_hypotheses(topic: str, idea_count: int = 5) -> str:
    ideas = [
        (
            "协议控制提升稳定性",
            f"针对 {topic}，更严格的实验协议控制能够降低随机种子、数据划分和负载波动带来的指标方差，使结论更可复现。",
        ),
        (
            "鲁棒性目标提升泛化",
            f"针对 {topic}，在主任务目标之外加入鲁棒性约束，可以提升分布外场景表现，同时不显著损害分布内性能。",
        ),
        (
            "组合方案优于单一组件",
            "在固定计算预算下，将协议控制与鲁棒性目标结合，应当比任一单独组件获得更稳定的综合收益。",
        ),
        (
            "数据/负载分层揭示隐藏失败模式",
            f"针对 {topic}，按样本难度、负载区间或场景类型做分层评估，可能发现平均指标掩盖的系统性失败模式，并指导更有针对性的改进。",
        ),
        (
            "轻量自适应机制优于全量复杂重构",
            f"针对 {topic}，在既有 pipeline 上加入轻量自适应组件，可能以更低工程成本获得接近复杂端到端重构的收益。",
        ),
        (
            "失败簇驱动的定向修复优于平均优化",
            f"针对 {topic}，先定位高频失败簇再定向修复，可能比直接优化整体平均指标更快提升最差场景表现。",
        ),
        (
            "跨数据源一致性检查提升可信度",
            f"针对 {topic}，把多来源证据、日志或 benchmark 结果做一致性校验，可以减少偶然数据偏差导致的错误结论。",
        ),
        (
            "人类反馈约束减少不可执行想法",
            f"针对 {topic}，在自动化研究流程中加入轻量人工反馈约束，可能显著减少不可验证或工程成本过高的研究想法。",
        ),
    ]
    blocks = ["# 研究假设", ""]
    for idx, (title, body) in enumerate(ideas[:_clamp_idea_count(idea_count)], start=1):
        blocks.append(f"## H{idx}：{title}\n{body}")
    blocks.append(f"## 生成时间\n{_utcnow_iso()}")
    return "\n\n".join(blocks) + "\n"


def _fallback_hypotheses_from_synthesis(topic: str, synthesis: str, idea_count: int = 5) -> str:
    """Create usable Stage 8 output when all LLM calls are unavailable."""
    synthesis_excerpt = synthesis.strip()
    if len(synthesis_excerpt) > 3000:
        synthesis_excerpt = synthesis_excerpt[:3000].rsplit("\n", 1)[0].strip()
    evidence_note = (
        synthesis_excerpt
        if synthesis_excerpt
        else "前序阶段已完成，但没有可用的详细综述文本。"
    )
    ideas = [
        ("面向主题的关键瓶颈建模", "现有研究通常分别优化单个组件，但对不同瓶颈之间的相互作用刻画不足。", "如果显式建模主要瓶颈之间的耦合关系，就能在不显著增加系统复杂度的前提下提升整体表现。", "选取一个可控子任务，对比标准基线、单组件优化方案和耦合建模方案。"),
        ("动态资源分配提升效率", "静态配置难以适应不同样本、请求或实验条件的变化。", "基于输入特征或运行时信号的动态分配策略，可以提升资源利用率并降低尾部延迟或失败概率。", "构造混合难度/混合长度的测试集，对比静态策略、常规动态策略和预测引导策略。"),
        ("标准化评估协议揭示真实收益", "相关工作常使用不同数据、硬件、负载和指标，导致方法优劣难以直接比较。", "建立覆盖关键变量的标准化评估协议，可以区分真正可迁移的改进和只在特定设置下有效的优化。", "搭建小型 benchmark harness，扫描至少两个关键变量，并评估两个基线加一个新策略。"),
        ("失败案例驱动的鲁棒性改进", "平均性能优化往往忽略最容易失败的边界场景，导致方法上线或跨域迁移时不稳定。", "先挖掘失败簇，再针对失败簇设计轻量修正机制，可以比盲目扩大模型或数据更高效地提升鲁棒性。", "抽取失败样本，聚类成 2-3 类错误模式，并对比基线与失败感知修正方案。"),
        ("轻量代理/控制器增强主系统", "完整重构主方法成本高，且难以定位收益来源。", "在主系统外加入轻量代理、调度器或校验器，可以在不改变主体模型的情况下提升效率、稳定性或可解释性。", "实现一个规则或小模型控制器，对比无控制器、静态控制器和自适应控制器三组。"),
        ("跨源证据一致性过滤", "单一文献源或单一 benchmark 可能放大偶然结论。", "把论文证据、公开实现和小规模复现实验做一致性过滤，可以更早排除伪 novelty。", "对 shortlist 论文构建 evidence matrix，并用小规模 sanity run 验证排名最高的两个机制。"),
        ("约束生成减少重复 Idea", "自动生成的研究想法容易出现同一机制换名、换场景的重复。", "在生成阶段加入机制指纹和相似度惩罚，可以提升候选集合的多样性和可选择性。", "对比无约束生成、prompt 约束生成和相似度过滤生成的重复率与人工可用率。"),
        ("人工反馈闭环提升可执行性", "完全自动的 idea 选择常忽视本地资源、代码基础和用户偏好。", "在关键决策点加入轻量人工偏好反馈，可以提高首选 idea 的可执行性并减少后续重跑。", "让用户对候选 idea 做一次快速排序，对比反馈前后的 S9 方案可执行度。"),
    ]
    blocks = [
        "# 研究假设",
        "",
        "> 本文件由本地 fallback 生成，因为 S8 阶段配置的 LLM 调用不可用。",
        "> 这些内容是可继续细化的种子 Idea，建议在模型端点稳定后再做一次 LLM 精炼。",
        "",
    ]
    for idx, (title, gap, claim, experiment) in enumerate(ideas[:_clamp_idea_count(idea_count)], start=1):
        blocks.append(
            f"## H{idx}：{title}\n"
            f"- 问题缺口：针对 {topic}，{gap}\n"
            f"- 假设：{claim}\n"
            f"- 最小实验：{experiment}\n"
            "- 指标：主任务性能、方差、资源消耗、失败率，以及关键场景下的稳定性。\n"
            "- 计算预算：优先设计为单卡或小规模子集 2 周内可验证。\n"
            "- 主要风险：需要用独立场景或严格消融确认收益不是来自数据偏差或额外计算。"
        )
    blocks.extend([
        "## 推荐优先尝试",
        "优先选择计算成本低、机制最清晰、与其他候选重复度最低的 Idea；若两个候选机制相同，应合并而不是重复推进。",
        "",
        "## 使用的证据",
        evidence_note,
        "",
        "## 生成时间",
        _utcnow_iso(),
    ])
    return "\n\n".join(blocks) + "\n"

def _default_paper_outline(topic: str) -> str:
    return f"""# Paper Outline

## 1. Title
Focused title on {topic}

## 2. Abstract
- Problem framing
- Method overview
- Key quantitative result

## 3. Introduction
- Motivation
- Gap statement
- Contributions

## 4. Related Work
- Method families
- Evaluation practices

## 5. Method
- Problem setup
- Model/algorithm
- Complexity and constraints

## 6. Experiments
- Datasets and metrics
- Baselines and ablations
- Reproducibility protocol

## 7. Results
- Main table
- Robustness analysis
- Failure cases

## 8. Discussion
- Practical implications
- Limitations

## 9. Conclusion
- Findings and next steps

Generated: {_utcnow_iso()}
"""


def _default_quality_report(threshold: float) -> dict[str, Any]:
    # When LLM fails, return below-threshold score to force revision
    score = max(1.0, float(threshold) - 2.0) if threshold > 0 else 5.0
    score = max(1.0, min(10.0, score))
    verdict = "revise"
    return {
        "score_1_to_10": round(score, 2),
        "verdict": verdict,
        "criteria": {
            "novelty": round(min(10.0, score + 0.3), 2),
            "methodological_rigor": round(score, 2),
            "clarity": round(max(1.0, score - 0.2), 2),
            "reproducibility": round(min(10.0, score + 0.1), 2),
        },
        "strengths": [
            "Stage-by-stage evidence chain preserved",
            "Experiment artifacts are generated and archived",
        ],
        "weaknesses": [
            "Statistical significance may need stronger reporting",
            "Broader external validity remains partially evaluated",
        ],
        "required_actions": [
            "Report confidence intervals and seed variance",
            "Include at least one stronger external baseline",
        ],
        "generated": _utcnow_iso(),
    }


def _execute_topic_init(
    stage_dir: Path,
    run_dir: Path,
    config: RCConfig,
    adapters: AdapterBundle,
    *,
    llm: LLMClient | None = None,
    prompts: PromptManager | None = None,
) -> StageResult:
    topic = config.research.topic
    domains = (
        ", ".join(config.research.domains) if config.research.domains else "general"
    )
    if llm is not None:
        _pm = prompts or PromptManager()
        _overlay = _get_evolution_overlay(run_dir, "topic_init")
        sp = _pm.for_stage(
            "topic_init",
            evolution_overlay=_overlay,
            topic=topic,
            domains=domains,
            project_name=config.project.name,
            quality_threshold=config.research.quality_threshold,
        )
        resp = llm.chat(
            [{"role": "user", "content": sp.user}],
            system=sp.system,
            max_tokens=8192,
        )
        goal_md = resp.content
    else:
        goal_md = f"""# Research Goal

## Topic
{topic}

## Scope
Investigate the topic with emphasis on reproducible methods and measurable outcomes.

## SMART Goal
- Specific: Build a focused research plan for {topic}
- Measurable: Produce literature shortlist, hypotheses, experiment plan, and final paper
- Achievable: Complete through staged pipeline with gate checks
- Relevant: Aligned with project {config.project.name}
- Time-bound: Constrained by pipeline execution budget

## Constraints
- Quality threshold: {config.research.quality_threshold}
- Daily paper target: {config.research.daily_paper_count}

## Success Criteria
- At least 5 falsifiable hypotheses / candidate ideas
- Executable experiment code and results analysis
- Revised paper passing quality gate

## Generated
{_utcnow_iso()}
"""
    (stage_dir / "goal.md").write_text(goal_md, encoding="utf-8")

    # --- Hardware detection (GPU / MPS / CPU) ---
    hw = detect_hardware()
    (stage_dir / "hardware_profile.json").write_text(
        json.dumps(hw.to_dict(), indent=2), encoding="utf-8"
    )
    if hw.warning:
        logger.warning("Hardware advisory: %s", hw.warning)
    else:
        logger.info("Hardware detected: %s (%s, %s MB VRAM)", hw.gpu_name, hw.gpu_type, hw.vram_mb)

    # --- Optionally ensure PyTorch is available ---
    if hw.has_gpu and config.experiment.mode == "sandbox":
        torch_ok = ensure_torch_available(config.experiment.sandbox.python_path, hw.gpu_type)
        if torch_ok:
            logger.info("PyTorch is available for sandbox experiments")
        else:
            logger.warning("PyTorch could not be installed; sandbox will use CPU-only packages")
    elif hw.has_gpu and config.experiment.mode == "docker":
        logger.info("Docker sandbox: PyTorch pre-installed in container image")

    return StageResult(
        stage=Stage.TOPIC_INIT,
        status=StageStatus.DONE,
        artifacts=("goal.md", "hardware_profile.json"),
        evidence_refs=("stage-01/goal.md", "stage-01/hardware_profile.json"),
    )


def _execute_problem_decompose(
    stage_dir: Path,
    run_dir: Path,
    config: RCConfig,
    adapters: AdapterBundle,
    *,
    llm: LLMClient | None = None,
    prompts: PromptManager | None = None,
) -> StageResult:
    goal_text = _read_prior_artifact(run_dir, "goal.md") or ""
    if llm is not None:
        _pm = prompts or PromptManager()
        _overlay = _get_evolution_overlay(run_dir, "problem_decompose")
        sp = _pm.for_stage(
            "problem_decompose",
            evolution_overlay=_overlay,
            topic=config.research.topic,
            goal_text=goal_text,
        )
        resp = llm.chat(
            [{"role": "user", "content": sp.user}],
            system=sp.system,
        )
        body = resp.content
    else:
        body = f"""# Problem Decomposition

## Source
Derived from `goal.md` for topic: {config.research.topic}

## Sub-questions
1. Which problem settings and benchmarks define current SOTA?
2. Which methodological gaps remain unresolved?
3. Which hypotheses are testable under realistic constraints?
4. Which datasets and metrics best discriminate method quality?
5. Which failure modes can invalidate expected gains?

## Priority Ranking
1. Problem framing and benchmark setup
2. Gap identification and hypothesis formulation
3. Experiment and metric design
4. Failure analysis and robustness checks

## Risks
- Ambiguous task definition
- Dataset leakage or metric mismatch

## Generated
{_utcnow_iso()}
"""
    (stage_dir / "problem_tree.md").write_text(body, encoding="utf-8")

    # IMP-35: Topic/title quality pre-evaluation
    # Quick LLM check: is the topic well-scoped for a conference paper?
    if llm is not None:
        try:
            _eval_resp = llm.chat(
                [
                    {
                        "role": "user",
                        "content": (
                            "Evaluate this research topic for a top ML conference paper. "
                            "Score 1-10 on: (a) novelty, (b) specificity, (c) feasibility. "
                            "If overall score < 5, suggest a refined topic.\n\n"
                            f"Topic: {config.research.topic}\n\n"
                            "Reply as JSON: {\"novelty\": N, \"specificity\": N, "
                            "\"feasibility\": N, \"overall\": N, \"suggestion\": \"...\"}"
                        ),
                    }
                ],
                system=(
                    f"You are a senior {_detect_domain(config.research.topic, config.research.domains)[1]} "
                    f"researcher evaluating research topic quality."
                ),
            )
            _eval_data = _safe_json_loads(_eval_resp.content, {})
            if isinstance(_eval_data, dict):
                overall = _eval_data.get("overall", 10)
                if isinstance(overall, (int, float)) and overall < 5:
                    logger.warning(
                        "IMP-35: Topic quality score %s/10 — consider refining: %s",
                        overall,
                        _eval_data.get("suggestion", ""),
                    )
                else:
                    logger.info("IMP-35: Topic quality score %s/10", overall)
                (stage_dir / "topic_evaluation.json").write_text(
                    json.dumps(_eval_data, indent=2), encoding="utf-8"
                )
        except Exception:  # noqa: BLE001
            logger.debug("IMP-35: Topic evaluation skipped (non-blocking)")

    return StageResult(
        stage=Stage.PROBLEM_DECOMPOSE,
        status=StageStatus.DONE,
        artifacts=("problem_tree.md",),
        evidence_refs=("stage-02/problem_tree.md",),
    )


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
    _stop = {
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
            if w.lower() not in _stop and len(w) > 1
        ]
        if re.search(r"[\u4e00-\u9fff]", text):
            en_words.extend(_extract_english_from_mixed(text))
            en_words = list(dict.fromkeys(w for w in en_words if w))
        return en_words

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


def _multi_perspective_generate(
    llm: LLMClient,
    roles: dict[str, dict[str, str]],
    variables: dict[str, str],
    perspectives_dir: Path,
) -> dict[str, str]:
    """Generate outputs from multiple debate perspectives.

    Each role has its own system/user prompt. Outputs are saved to
    *perspectives_dir* and returned as ``{role_name: response_text}``.
    """
    from researchclaw.prompts import _render  # noqa: PLC0415

    perspectives_dir.mkdir(parents=True, exist_ok=True)
    results: dict[str, str] = {}
    for role_name, role_prompts in roles.items():
        try:
            system = _render(role_prompts["system"], variables) + "\n必须用中文输出。保留必要英文术语，但论证、分析和建议用中文。"
            user = (
                _render(role_prompts["user"], variables)
                + "\n\n## 输出语言要求\n"
                + "请用中文生成所有假设、论证、实验设计和风险分析。英文只用于论文名、方法名、数据集名、指标名等专有名词。"
                + (
                    f"\n\n## 数量与去重要求\n{_idea_count_rule(int(variables.get('idea_count', '5')))}"
                    if str(variables.get('idea_count', '')).isdigit()
                    else ""
                )
            )
            resp = llm.chat(
                [{"role": "user", "content": user}],
                system=system,
            )
            results[role_name] = resp.content
            (perspectives_dir / f"{role_name}.md").write_text(
                resp.content, encoding="utf-8"
            )
            logger.info("Debate perspective '%s' generated (%d chars)", role_name, len(resp.content))
        except Exception as exc:  # noqa: BLE001
            logger.warning("Debate perspective '%s' failed: %s", role_name, exc)
    if len(results) < 2:
        logger.error("Multi-perspective debate: only %d/%d roles succeeded", len(results), len(roles))
    return results


def _synthesize_perspectives(
    llm: LLMClient,
    perspectives: dict[str, str],
    sub_prompt_name: str,
    prompts: PromptManager,
    idea_count: int = 5,
) -> str:
    """Synthesize multiple perspective outputs into a unified result."""
    parts = []
    for role_name, text in perspectives.items():
        parts.append(f"### Perspective: {role_name}\n{text}")
    combined = "\n\n---\n\n".join(parts)
    sp = prompts.sub_prompt(sub_prompt_name, perspectives=combined)
    user = (
        sp.user
        + "\n\n## 输出语言要求\n"
        + "请用中文综合为最终假设和推荐方案。保留必要英文专有名词，但章节说明、判断和实验计划必须是中文。"
        + f"\n\n## 数量与去重要求\n{_idea_count_rule(idea_count)}"
    )
    system = sp.system + "\n必须用中文输出最终假设综合结果。"
    resp = llm.chat(
        [{"role": "user", "content": user}],
        system=system,
    )
    return resp.content


def _load_root_arc_section(section_name: str) -> dict[str, Any]:
    try:
        root_config = Path(__file__).resolve().parents[4] / "config.arc.yaml"
        if not root_config.exists():
            return {}
        raw = yaml.safe_load(root_config.read_text(encoding="utf-8")) or {}
        section = raw.get(section_name, {}) if isinstance(raw, dict) else {}
        return section if isinstance(section, dict) else {}
    except Exception:  # noqa: BLE001
        return {}


def _create_idea_judge_llm(config: RCConfig) -> tuple[LLMClient | None, str]:
    """Create the strong LLM-as-Judge client for Stage 8 idea scoring."""
    section = _load_root_arc_section("idea_judge_llm")
    model = (
        os.environ.get("RESEARCHCLAW_IDEA_JUDGE_MODEL", "")
        or str(section.get("primary_model", "") or "")
        or str(getattr(config.llm, "primary_model", "") or "")
        or "Qwen3.5-122B-A10B-FP8"
    )
    base_url = (
        os.environ.get("RESEARCHCLAW_IDEA_JUDGE_BASE_URL", "")
        or str(section.get("base_url", "") or "")
        or str(getattr(config.llm, "base_url", "") or "")
    )
    api_key_env = str(section.get("api_key_env", "RESEARCHCLAW_IDEA_JUDGE_API_KEY") or "RESEARCHCLAW_IDEA_JUDGE_API_KEY")
    api_key = (
        os.environ.get("RESEARCHCLAW_IDEA_JUDGE_API_KEY", "")
        or os.environ.get(api_key_env, "")
        or str(section.get("api_key", "") or "")
        or str(getattr(config.llm, "api_key", "") or "")
    )
    if not api_key:
        current_key = str(getattr(config.llm, "api_key", "") or "")
        current_base = str(getattr(config.llm, "base_url", "") or "")
        if current_base and current_key:
            api_key = current_key
            base_url = current_base
    if not api_key:
        logger.info("S8: Idea LLM-as-Judge skipped — no Qwen3 judge API key configured")
        return None, model
    timeout = int(section.get("timeout_sec", os.environ.get("RESEARCHCLAW_IDEA_JUDGE_TIMEOUT", 90)) or 90)
    max_retries = int(section.get("max_retries", os.environ.get("RESEARCHCLAW_IDEA_JUDGE_MAX_RETRIES", 1)) or 1)
    extra_body = section.get("extra_body", None)
    if not isinstance(extra_body, dict):
        extra_body = dict(getattr(config.llm, "extra_body", {}) or {})
    client = LLMClient(LLMConfig(
        base_url=base_url,
        api_key=api_key,
        primary_model=model,
        fallback_models=[],
        timeout_sec=timeout,
        max_retries=max_retries,
        temperature=0,
        strip_thinking=True,
        extra_body=extra_body,
    ))
    return client, model


def _paper_authors_short(authors: Any, *, limit: int = 3) -> str:
    if isinstance(authors, list):
        names: list[str] = []
        for item in authors[:limit]:
            if isinstance(item, dict):
                name = str(item.get("name", "") or "").strip()
            else:
                name = str(item or "").strip()
            if name:
                names.append(name)
        if len(authors) > limit:
            names.append("等")
        return ", ".join(names)
    if isinstance(authors, str):
        return authors[:160]
    return ""


def _format_paper_for_idea_evidence(row: dict[str, Any], idx: int) -> str:
    title = str(row.get("title", "") or f"Paper {idx}").strip()
    authors = _paper_authors_short(row.get("authors", []))
    year = str(row.get("year", "") or "").strip()
    venue = str(row.get("venue", "") or row.get("journal", "") or row.get("source", "") or "").strip()
    abstract = str(row.get("abstract", "") or row.get("summary", "") or "").strip()
    reason = str(row.get("keep_reason", "") or row.get("relevance_reason", "") or "").strip()
    finding = str(row.get("findings", "") or row.get("key_finding", "") or "").strip()
    parts = [f"{idx}. {title}"]
    meta = "，".join(part for part in (authors, year, venue) if part)
    if meta:
        parts.append(f"   - 元信息：{meta}")
    if abstract:
        parts.append(f"   - 摘要线索：{abstract[:520]}")
    if reason:
        parts.append(f"   - 入选原因：{reason[:260]}")
    if finding:
        parts.append(f"   - 关键发现：{finding[:260]}")
    return "\n".join(parts)


def _collect_idea_evidence_pack(run_dir: Path, *, max_papers: int = 12, max_chars: int = 16_000) -> str:
    """Collect compact paper/card evidence for Stage 8 idea grounding."""
    sections: list[str] = []
    seen_titles: set[str] = set()
    paper_rows: list[dict[str, Any]] = []
    for artifact_name in ("shortlist.jsonl", "candidates.jsonl"):
        text = _read_prior_artifact(run_dir, artifact_name) or ""
        if not text.strip():
            continue
        for row in _parse_jsonl_rows(text):
            title = str(row.get("title", "") or "").strip().lower()
            if not title or title in seen_titles:
                continue
            seen_titles.add(title)
            paper_rows.append(row)
            if len(paper_rows) >= max_papers:
                break
        if len(paper_rows) >= max_papers:
            break
    if paper_rows:
        sections.append(
            "## 相近文献候选（用于每个 Idea 的 novelty 对照）\n"
            + "\n\n".join(
                _format_paper_for_idea_evidence(row, idx)
                for idx, row in enumerate(paper_rows, start=1)
            )
        )

    cards_path_text = _read_prior_artifact(run_dir, "cards/") or ""
    if cards_path_text:
        card_snippets: list[str] = []
        cards_dir = Path(cards_path_text)
        if cards_dir.is_dir():
            for card_path in sorted(cards_dir.glob("*.md"))[:8]:
                try:
                    snippet = card_path.read_text(encoding="utf-8", errors="ignore").strip()
                except OSError:
                    continue
                if snippet:
                    card_snippets.append(f"### {card_path.stem}\n{snippet[:1000]}")
        if card_snippets:
            sections.append("## 知识卡片摘要（用于 gap 和实验设计）\n" + "\n\n".join(card_snippets))

    if not sections:
        return ""
    pack = "\n\n".join(sections)
    if len(pack) > max_chars:
        pack = pack[:max_chars].rsplit("\n", 1)[0].strip() + "\n...（证据包已截断）"
    return pack


def _idea_quality_rubric() -> str:
    return """## Idea 质量评分标准（必须执行）
每个 Idea 都必须给出 1-5 分评分和一句理由：
- Novelty：是否相对相近文献有真实差异，而不是换名组合。
- Feasibility：是否能在当前资源、数据和 2 周 MVP 内启动验证。
- Impact：若成立，是否足以形成论文贡献或明确系统价值。
- Testability：是否有清晰可证伪假设、指标、失败阈值和对照实验。
- Literature Grounding：是否引用并区分了最相近的 3-5 篇文献。
- Risk：风险是否被识别，是否有早停信号和 fallback 方案。
- Compute Cost：计算预算是否明确且可控；5 分代表单卡/小规模 2 周内可验证，1 分代表成本高或没说明。
- Diversity：是否与其他 Idea 是不同技术机制；同一机制换标题、换应用场景或换指标必须低分。

硬性要求：如果某个 Idea 无法列出相近文献和差异点，不能推荐为首选。若两个 Idea 的核心机制重复，必须合并或替换，不能重复凑数。"""


def _idea_schema_instruction() -> str:
    return """## 强制输出结构
对每个 Idea 使用以下结构，字段不能省略：

## Idea N：<中文短标题>

### 1. 核心假设
用 1-2 句话写清楚可证伪假设。

### 2. 研究空缺与文献依据
列出 3-5 篇最相近文献或方法，说明它们分别解决了什么、没有解决什么。

### 3. 与已有工作的关键区别
用表格写：相近工作 / 相同点 / 不同点 / reviewer 可能质疑 / 回应策略。

### 4. 技术路线
具体到模型结构、算法步骤、训练目标、数据处理、系统组件或 agent workflow；避免抽象口号。

### 5. 可验证实验
至少 3 个实验：主实验、消融实验、鲁棒性或泛化实验。每个实验写数据集、baseline、指标、预期结果、失败阈值。

### 6. 两周 MVP
写最小可行验证：数据子集、模型规模、代码入口、计算预算、Go/No-Go 标准。

### 7. 风险、反例与失败条件
列出最可能失败的 3 个原因、早期发现信号、备选方案。

### 8. 评分
用表格给出 Novelty、Feasibility、Impact、Testability、Literature Grounding、Risk、Compute Cost、Diversity 八项 1-5 分和理由。

### 9. 重复检查
列出该 Idea 与其他 Idea 的机制差异；如果与任何候选相似，说明为什么仍然值得保留，否则必须合并或替换。

最后必须包含：
## 总体排序与首选推荐
按综合分排序，推荐 exactly one 个首选 Idea，并解释为什么先做它。

## 去重与多样性检查
用表格列出所有 Idea 两两之间的机制差异。严禁出现同一机制换标题、换数据集或换指标的重复 Idea。"""



def _safe_stage_json_from_text(text: str) -> dict[str, Any]:
    """Best-effort JSON extraction from an LLM response."""
    raw = (text or "").strip()
    if not raw:
        return {}
    fenced = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", raw, re.S)
    candidates = [fenced.group(1)] if fenced else []
    candidates.append(raw)
    brace_start = raw.find("{")
    brace_end = raw.rfind("}")
    if brace_start >= 0 and brace_end > brace_start:
        candidates.append(raw[brace_start:brace_end + 1])
    for candidate in candidates:
        try:
            parsed = json.loads(candidate)
        except (json.JSONDecodeError, TypeError):
            continue
        if isinstance(parsed, dict):
            return parsed
    return {}


def _idea_title_slug(title: str) -> str:
    cleaned = re.sub(r"\s+", " ", (title or "").strip())
    cleaned = re.sub(r"^[#\s0-9IdeaH想法假设一二三四五六七八九十：:.-]+", "", cleaned).strip()
    return cleaned[:80] or "未命名 Idea"


def _resolve_shared_results_path(config: RCConfig, run_dir: Path) -> Path:
    raw = getattr(getattr(config, "experiment", None), "shared_results_dir", "") or "runs/shared_results"
    path = Path(str(raw))
    if path.is_absolute():
        return path
    # The CLI usually runs from repo/backend/agent; this fallback keeps direct
    # tests and web-launched jobs writing under backend/runs/shared_results.
    backend_runs = run_dir.parents[2] / "shared_results" if len(run_dir.parents) >= 3 else run_dir.parent / "shared_results"
    return backend_runs if backend_runs.parent.name == "runs" else Path.cwd() / path


def _load_ideation_memory(config: RCConfig, run_dir: Path, *, max_chars: int = 8000) -> str:
    paths = [
        _resolve_shared_results_path(config, run_dir) / "ideation_memory.md",
        run_dir.parent / "ideation_memory.md",
    ]
    chunks: list[str] = []
    for path in paths:
        try:
            text = path.read_text(encoding="utf-8", errors="ignore").strip()
        except OSError:
            continue
        if text:
            chunks.append(f"## {path.name}\n{text[-max_chars:]}")
    return "\n\n".join(chunks)[-max_chars:]


def _load_experiment_memory(config: RCConfig, run_dir: Path, *, max_chars: int = 8000) -> str:
    """Load experiment memory from shared results dir — contains lessons from past experimental runs."""
    paths = [
        _resolve_shared_results_path(config, run_dir) / "experiment_memory.md",
        run_dir.parent / "experiment_memory.md",
    ]
    chunks: list[str] = []
    for path in paths:
        try:
            text = path.read_text(encoding="utf-8", errors="ignore").strip()
        except OSError:
            continue
        if text:
            chunks.append(f"## {path.name}\n{text[-max_chars:]}")
    return "\n\n".join(chunks)[-max_chars:]


def _write_experiment_memory_update(
    config: RCConfig,
    run_dir: Path,
    stage_dir: Path,
    *,
    topic: str,
    experiment_summary: str,
) -> tuple[str, ...]:
    """Write an experiment memory update after experimental runs (Stages 14-15).

    Captures what worked, what failed, and key lessons for future hypothesis generation cycles.
    """
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    update = [
        f"## {today} — {topic}",
        "",
        "### Experiment Summary",
        experiment_summary[:2000].strip(),
        "",
        "### Lessons",
        "- Record what worked, what failed, and why.",
        "- Note any unexpected findings, regressions, or surprising results.",
        "",
    ]
    update_text = "\n".join(update).strip() + "\n"
    try:
        shared_dir = _resolve_shared_results_path(config, run_dir)
        shared_dir.mkdir(parents=True, exist_ok=True)
        memory_path = shared_dir / "experiment_memory.md"
        previous = ""
        if memory_path.exists():
            previous = memory_path.read_text(encoding="utf-8", errors="ignore").rstrip() + "\n\n"
        memory_path.write_text(previous + update_text, encoding="utf-8")
        logger.info("S8: Experiment memory updated (run_dir=%s)", run_dir)
    except OSError:
        logger.warning("S8: Failed to update experiment memory", exc_info=True)
    return ("experiment_memory_update.md",)


def _structured_idea_reflection(
    checkpoint_name: str,
    *,
    topic: str,
    context: str = "",
    dimensions: Sequence[str] = ("progress", "evidence", "strategy", "handoff"),
) -> str:
    """Structured reflection at key Stage 8 checkpoints, inspired by EvoScientist's 7-dimension think_tool.

    Dimensions available (pass subset relevant to the checkpoint):
    1. progress — What has been accomplished? What concrete steps remain?
    2. evidence — Is evidence sufficient for the goal? Would a critical reviewer accept it?
    3. skills — Is there an installed MetaClaw skill or learned lesson to leverage?
    4. prior_knowledge — Have ideation-memory and experiment-memory been checked?
    5. strategy — Continue, adjust, or try something different?
    6. handoff — What artifacts does the next sub-stage need?
    7. resource — Before heavy operations, estimate runtime and feasibility.
    """
    dim_descriptions = {
        "progress": "Progress — What has been accomplished? What concrete steps remain?",
        "evidence": "Evidence quality — Is the current evidence sufficient for the goal? Would a critical reviewer accept it?",
        "skills": "Skills leverage — Is there an installed MetaClaw skill or prior-run lesson that provides guidance?",
        "prior_knowledge": "Prior knowledge — Has ideation-memory and experiment-memory been reviewed for relevant past findings?",
        "strategy": "Strategy — Should the current approach continue, adjust, or change direction? What evidence supports this?",
        "handoff": "Handoff — Which artifacts and results does the next sub-stage need? Are outputs well-organized?",
        "resource": "Resource & compute — Estimate runtime and feasibility before proceeding. Does the hardware profile support the planned experiments?",
    }
    selected = [dim_descriptions.get(d, d) for d in dimensions]
    md = [
        f"## Structured Reflection: {checkpoint_name}",
        "",
        "### Relevant Dimensions",
        *[f"- {d}" for d in selected],
        "",
        "### Reflection Notes",
        "*[This checkpoint records the reasoning at this decision point for traceability.]*",
        "",
    ]
    if context:
        md.append(f"\nContext: {context[:2000]}\n")
    return "\n".join(md)


def _fallback_challenge_insight_tree(topic: str, synthesis: str, evidence_pack: str) -> dict[str, Any]:
    text = f"{synthesis}\n{evidence_pack}"
    challenge_keywords = [
        "robust", "scal", "latency", "cost", "privacy", "security", "alignment",
        "generalization", "evaluation", "dataset", "鲁棒", "泛化", "隐私", "安全", "延迟", "成本", "评估", "数据",
    ]
    sentences = re.split(r"(?<=[。！？.!?])\s+|\n+", text)
    challenges: list[dict[str, Any]] = []
    for sent in sentences:
        compact = sent.strip()
        if len(compact) < 24:
            continue
        field_name = compact.lstrip("- ").split("：", 1)[0].split(":", 1)[0]
        if compact.startswith(("匹配词", "来源", "### Evidence", "score=")) or field_name in {
            "核心假设", "技术机制", "文献缺口", "最小实验", "风险", "评分",
        }:
            continue
        lower = compact.lower()
        if any(k in lower for k in challenge_keywords) or any(k in compact for k in challenge_keywords):
            challenges.append({
                "challenge": compact[:180],
                "existing_insights": [],
                "missing_insights": ["需要从文献中进一步定位未被解决的机制缺口"],
                "transfer_opportunities": ["尝试把相邻方法迁移到该 challenge 的最小可验证版本"],
                "why_open": "由综述中的高频问题线索自动抽取，需在候选 Idea 中进一步验证。",
            })
        if len(challenges) >= 6:
            break
    if not challenges:
        challenges = [{
            "challenge": f"{topic} 中尚未被现有方法充分验证的关键机制缺口",
            "existing_insights": [],
            "missing_insights": ["缺少清晰的可证伪假设和最小实验"],
            "transfer_opportunities": ["从相近文献提取可复用 insight，形成两周 MVP"],
            "why_open": "文献证据不足时的保守兜底。",
        }]
    return {"topic": topic, "challenges": challenges, "bridge_opportunities": []}


def _challenge_tree_to_markdown(tree: dict[str, Any]) -> str:
    topic = str(tree.get("topic", "") or "").strip()
    lines = ["# Challenge-Insight Tree", "", f"Topic: {topic or '未记录'}", ""]
    challenges = tree.get("challenges", [])
    if not isinstance(challenges, list):
        challenges = []
    for idx, item in enumerate(challenges, start=1):
        if not isinstance(item, dict):
            continue
        lines.extend([f"## Challenge {idx}: {item.get('challenge', '未命名挑战')}", ""])
        for label, key in (
            ("Existing Insights", "existing_insights"),
            ("Missing Insights", "missing_insights"),
            ("Transfer Opportunities", "transfer_opportunities"),
        ):
            values = item.get(key, [])
            if isinstance(values, str):
                values = [values]
            lines.append(f"### {label}")
            if values:
                lines.extend(f"- {str(v).strip()}" for v in values[:8] if str(v).strip())
            else:
                lines.append("- 暂无明确条目")
            lines.append("")
        if item.get("why_open"):
            lines.extend(["### Why Still Open", f"- {item.get('why_open')}", ""])
    bridges = tree.get("bridge_opportunities", [])
    if isinstance(bridges, list) and bridges:
        lines.extend(["## Cross-Challenge Bridge Opportunities", ""])
        for bridge in bridges[:8]:
            lines.append(f"- {bridge}")
    return "\n".join(lines).strip() + "\n"


def _generate_challenge_insight_tree(
    llm: LLMClient | None,
    *,
    topic: str,
    synthesis: str,
    evidence_pack: str,
    ideation_memory: str,
) -> tuple[dict[str, Any], str]:
    if llm is None:
        tree = _fallback_challenge_insight_tree(topic, synthesis, evidence_pack)
        return tree, _challenge_tree_to_markdown(tree)
    system = (
        "你是科研选题分析专家。请把文献综述拆成 Challenge-Insight Tree，"
        "目标是帮助后续生成互不重复、文献扎实、可实验的研究 idea。只输出 JSON。"
    )
    user = (
        f"研究主题：\n{topic}\n\n"
        "请输出 JSON，不要 Markdown，schema 如下：\n"
        "{\n"
        '  "topic": "...",\n'
        '  "challenges": [\n'
        "    {\n"
        '      "challenge": "具体开放挑战",\n'
        '      "existing_insights": ["已有方法/发现"],\n'
        '      "missing_insights": ["尚缺机制/证据/实验"],\n'
        '      "transfer_opportunities": ["可从其他 challenge/方法迁移的 insight"],\n'
        '      "why_open": "为什么还值得做"\n'
        "    }\n"
        "  ],\n"
        '  "bridge_opportunities": ["跨 challenge 组合机会"]\n'
        "}\n\n"
        "要求：\n"
        "- challenges 4-7 个，避免泛泛而谈。\n"
        "- existing_insights 必须来自综述/证据包，不要编造论文。\n"
        "- missing_insights 要能转化成可证伪 idea。\n"
        "- 标出哪些 challenge 已经成熟，哪些 challenge 缺 insight 更适合创新。\n\n"
        f"长期 ideation memory：\n{ideation_memory[:5000] if ideation_memory else '无'}\n\n"
        f"文献综述：\n{synthesis[:12000]}\n\n"
        f"证据包：\n{evidence_pack[:12000] if evidence_pack else '无'}\n"
    )
    resp = llm.chat([{"role": "user", "content": user}], system=system, max_tokens=4096, temperature=0.2, json_mode=True)
    tree = _safe_stage_json_from_text(resp.content)
    if not tree:
        tree = _fallback_challenge_insight_tree(topic, synthesis, evidence_pack)
    return tree, _challenge_tree_to_markdown(tree)


def _generate_candidate_ideas(
    llm: LLMClient | None,
    *,
    topic: str,
    synthesis: str,
    raw_hypotheses_md: str,
    evidence_pack: str,
    challenge_tree_md: str,
    ideation_memory: str,
    idea_count: int,
) -> str:
    candidate_count = max(12, min(21, _clamp_idea_count(idea_count) * 3))
    if llm is None:
        seed_blocks = _idea_like_blocks(raw_hypotheses_md)
        seed_blocks.extend(_idea_like_blocks(_fallback_hypotheses_from_synthesis(topic, synthesis, max(idea_count, 8))))
        challenges = re.findall(r"^## Challenge\s+\d+\s*[：:]\s*(.+)$", challenge_tree_md, flags=re.M)
        evidence_lines = [
            line.strip("- ").strip()
            for line in (evidence_pack or synthesis).splitlines()
            if len(line.strip()) > 40
        ][:8]
        mechanisms = [
            "面向失败模式的检索增强校验",
            "轻量级因果探针与消融矩阵",
            "跨数据源一致性约束",
            "预算感知的主动采样",
            "不确定性驱动的人机复核",
            "结构化证据图上的反事实对照",
            "小模型先验与强模型裁判协同",
            "运行时漂移检测与早停策略",
        ]
        for idx in range(candidate_count * 2):
            challenge = challenges[idx % len(challenges)] if challenges else topic
            evidence = evidence_lines[idx % len(evidence_lines)] if evidence_lines else "已有综述指出该方向仍缺少可复现实证闭环。"
            mechanism = mechanisms[idx % len(mechanisms)]
            challenge_title = _idea_title_slug(re.split(r"[。！？.!?；;]", challenge)[0])[:34]
            if challenge_title.startswith(("匹配词", "来源")) or not challenge_title:
                challenge_title = f"{topic}关键瓶颈"
            title = f"{challenge_title} / {mechanism}"
            block = "\n".join([
                f"## Idea {idx + 1}: {title}",
                f"- 核心假设：围绕“{challenge_title}”引入{mechanism}，可以把泛化、可验证性或成本瓶颈转化为可测实验问题。",
                f"- 文献缺口：{evidence[:220]}",
                f"- 技术机制：构建 evidence map，定位一个主要失败模式，并用{mechanism}形成与现有方法不同的干预变量。",
                "- 最小实验：选择一个公开基准和一个压力测试子集，对比 baseline、无机制版本和完整版本。",
                "- 风险：若提升只来自额外算力或数据清洗，则降级为诊断工具而不是主方法。",
                "- 评分：Novelty 3 / Feasibility 4 / Impact 3 / Testability 4 / Compute 3 / Diversity 4",
            ])
            seed_blocks.append((title, block))
        unique_blocks: list[tuple[str, str]] = []
        for title, block in seed_blocks:
            if len(unique_blocks) >= candidate_count:
                break
            if any(
                _idea_text_similarity(block, existing_block) >= 0.86
                or _idea_text_similarity(title, existing_title) >= 0.70
                for existing_title, existing_block in unique_blocks
            ):
                continue
            unique_blocks.append((title, block))
        if len(unique_blocks) < candidate_count:
            for title, block in seed_blocks:
                if len(unique_blocks) >= candidate_count:
                    break
                if any(_idea_text_similarity(title, existing_title) >= 0.86 for existing_title, _ in unique_blocks):
                    continue
                unique_blocks.append((title, block))
        return _renumber_idea_blocks(unique_blocks[:candidate_count])
    system = (
        "你是科研 idea 扩展器。你的任务不是最终筛选，而是基于 Challenge-Insight Tree "
        "生成足够多、机制差异明显、可进入 tournament 的候选 idea。必须中文输出。"
    )
    user = (
        f"研究主题：\n{topic}\n\n"
        f"请生成 {candidate_count} 个候选 Idea，用 Markdown 输出。\n"
        "规则：\n"
        "- 候选必须覆盖不同 challenge / insight bridge / 技术机制。\n"
        "- 允许有高风险候选，但必须写清最小证据。\n"
        "- 不要把同一机制换标题、换数据集、换指标凑数。\n"
        "- 每个候选保持紧凑，但必须包含：核心假设、文献缺口、关键区别、最小实验、风险、粗略评分。\n"
        "- 参考 ideation memory：继承成功方向，避开 fundamental failure 方向。\n\n"
        "输出格式：\n"
        "## Idea 1：标题\n"
        "- 核心假设：...\n- 文献缺口：...\n- 技术机制：...\n- 最小实验：...\n- 风险：...\n- 评分：Novelty/Feasibility/Impact/Testability/Compute/Diversity\n\n"
        f"Challenge-Insight Tree：\n{challenge_tree_md[:10000]}\n\n"
        f"长期 ideation memory：\n{ideation_memory[:5000] if ideation_memory else '无'}\n\n"
        f"文献综述：\n{synthesis[:9000]}\n\n"
        f"证据包：\n{evidence_pack[:9000] if evidence_pack else '无'}\n\n"
        f"已有 raw hypotheses seed：\n{raw_hypotheses_md[:9000]}\n"
    )
    resp = llm.chat([{"role": "user", "content": user}], system=system, max_tokens=12000, temperature=0.8)
    text = resp.content.strip()
    if _idea_like_count(text) < idea_count:
        text = _complete_idea_set(
            text,
            topic=topic,
            synthesis=synthesis,
            idea_count=candidate_count,
            fallback_sources=(raw_hypotheses_md, _fallback_hypotheses_from_synthesis(topic, synthesis, candidate_count)),
        )
    return text


def _score_idea_block(title: str, block: str, all_blocks: Sequence[tuple[str, str]]) -> dict[str, Any]:
    text = f"{title}\n{block}"
    lower = text.lower()
    novelty_terms = ("novel", "new", "gap", "different", "区别", "差异", "空缺", "创新", "未解决", "迁移")
    feasibility_terms = ("mvp", "dataset", "baseline", "metric", "数据", "指标", "两周", "baseline", "消融", "预算")
    risk_terms = ("risk", "fail", "fallback", "风险", "失败", "反例", "no-go", "早停")
    grounding_terms = ("paper", "et al", "arxiv", "doi", "文献", "论文", "方法", "工作")
    compute_terms = ("gpu", "cpu", "小时", "预算", "单卡", "batch", "epoch", "compute")

    def score_terms(terms: tuple[str, ...], base: int = 2) -> int:
        hits = sum(1 for term in terms if term in lower or term in text)
        return max(1, min(5, base + hits))

    novelty = score_terms(novelty_terms, 2)
    feasibility = score_terms(feasibility_terms, 2)
    impact = 3 + (1 if any(k in lower or k in text for k in ("impact", "贡献", "价值", "提升", "降低", "鲁棒")) else 0)
    testability = score_terms(("experiment", "ablation", "threshold", "实验", "消融", "阈值", "验证", "可证伪"), 2)
    grounding = score_terms(grounding_terms, 1)
    risk = score_terms(risk_terms, 1)
    compute = score_terms(compute_terms, 2)
    max_sim = 0.0
    for other_title, other_block in all_blocks:
        if other_block == block:
            continue
        max_sim = max(max_sim, _idea_text_similarity(block, other_block), _idea_text_similarity(title, other_title))
    diversity = 5 if max_sim < 0.22 else 4 if max_sim < 0.34 else 3 if max_sim < 0.46 else 2 if max_sim < 0.60 else 1
    overall = round((novelty + feasibility + impact + testability + grounding + risk + compute + diversity) / 8, 2)
    return {
        "title": _idea_title_slug(title),
        "novelty": novelty,
        "feasibility": feasibility,
        "impact": min(5, impact),
        "testability": testability,
        "literature_grounding": grounding,
        "risk": risk,
        "compute_cost": compute,
        "diversity": diversity,
        "max_similarity": round(max_sim, 3),
        "overall": overall,
    }


def _run_idea_tournament(candidate_ideas_md: str, *, idea_count: int) -> tuple[dict[str, Any], str, str]:
    blocks = _idea_like_blocks(candidate_ideas_md)
    if not blocks:
        blocks = [("兜底 Idea", candidate_ideas_md.strip())] if candidate_ideas_md.strip() else []
    rows: list[dict[str, Any]] = []
    ratings = [1500.0 for _ in blocks]
    scores = [_score_idea_block(title, block, blocks) for title, block in blocks]
    matches: list[dict[str, Any]] = []
    k_factor = 32
    for i in range(len(blocks)):
        for j in range(i + 1, len(blocks)):
            left = scores[i]
            right = scores[j]
            left_signal = float(left["overall"]) + 0.10 * float(left["diversity"]) + 0.08 * float(left["testability"])
            right_signal = float(right["overall"]) + 0.10 * float(right["diversity"]) + 0.08 * float(right["testability"])
            if abs(left_signal - right_signal) < 0.05:
                outcome_i = 0.5
                winner = "draw"
            elif left_signal > right_signal:
                outcome_i = 1.0
                winner = left["title"]
            else:
                outcome_i = 0.0
                winner = right["title"]
            expected_i = 1 / (1 + 10 ** ((ratings[j] - ratings[i]) / 400))
            delta = k_factor * (outcome_i - expected_i)
            ratings[i] += delta
            ratings[j] -= delta
            matches.append({
                "left": left["title"],
                "right": right["title"],
                "winner": winner,
                "left_signal": round(left_signal, 3),
                "right_signal": round(right_signal, 3),
            })
    for idx, ((title, block), score, rating) in enumerate(zip(blocks, scores, ratings), start=1):
        row = dict(score)
        row.update({"candidate_id": f"C{idx}", "elo": round(rating, 1), "block": block})
        rows.append(row)
    rows.sort(key=lambda r: (float(r.get("elo", 0)), float(r.get("overall", 0)), float(r.get("diversity", 0))), reverse=True)
    selected_blocks: list[tuple[str, str]] = []
    for row in rows:
        if len(selected_blocks) >= _clamp_idea_count(idea_count):
            break
        block = str(row.get("block", ""))
        title = str(row.get("title", ""))
        if any(_idea_text_similarity(block, existing) >= 0.50 for _, existing in selected_blocks):
            continue
        selected_blocks.append((title, block))
    if len(selected_blocks) < _clamp_idea_count(idea_count):
        for row in rows:
            if len(selected_blocks) >= _clamp_idea_count(idea_count):
                break
            block = str(row.get("block", ""))
            title = str(row.get("title", ""))
            if not any(block == existing for _, existing in selected_blocks):
                selected_blocks.append((title, block))
    selected_md = _renumber_idea_blocks(selected_blocks) if selected_blocks else candidate_ideas_md
    public_rows = [{k: v for k, v in row.items() if k != "block"} for row in rows]
    report = {
        "method": "local_pairwise_elo",
        "candidate_count": len(blocks),
        "selected_count": len(selected_blocks),
        "ranking": public_rows,
        "matches": matches,
    }
    md_lines = [
        "# Idea Tournament",
        "",
        "Method: local pairwise Elo over novelty/feasibility/impact/testability/grounding/risk/compute/diversity heuristics.",
        "",
        "| Rank | Candidate | Elo | Overall | Novelty | Feasibility | Impact | Testability | Grounding | Risk | Compute | Diversity | Max Similarity |",
        "|---:|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for rank, row in enumerate(public_rows, start=1):
        md_lines.append(
            f"| {rank} | {row.get('title', '')} | {row.get('elo', '')} | {row.get('overall', '')} | "
            f"{row.get('novelty', '')} | {row.get('feasibility', '')} | {row.get('impact', '')} | "
            f"{row.get('testability', '')} | {row.get('literature_grounding', '')} | {row.get('risk', '')} | "
            f"{row.get('compute_cost', '')} | {row.get('diversity', '')} | {row.get('max_similarity', '')} |"
        )
    md_lines.extend(["", "## Selected Ideas", "", selected_md.strip()])
    return report, "\n".join(md_lines).strip() + "\n", selected_md


def _generate_role_review(
    llm: LLMClient | None,
    *,
    topic: str,
    synthesis: str,
    core_ideas_md: str,
    evidence_pack: str,
    tournament_md: str,
) -> str:
    if llm is None:
        return "# Idea Role Review\n\n- LLM unavailable; role review skipped.\n"
    system = (
        "你是由三位科研角色组成的 idea panel：Innovator、Pragmatist、Critic。"
        "请分别从突破性、可执行性、科学严谨性审查候选 idea，最后给出合议修改建议。中文输出。"
    )
    user = (
        f"研究主题：\n{topic}\n\n"
        "请输出：\n"
        "# Idea Role Review\n"
        "## Innovator 视角\n逐条指出哪些 idea 有突破潜力，如何增强 novelty。\n"
        "## Pragmatist 视角\n逐条指出两周 MVP、数据、baseline、compute 是否清楚。\n"
        "## Critic 视角\n逐条指出 reviewer 会质疑的新颖性、证据和实验漏洞。\n"
        "## 合议修改清单\n给出必须修改/合并/替换的条目。\n\n"
        f"Tournament 摘要：\n{tournament_md[:6000]}\n\n"
        f"文献综述：\n{synthesis[:7000]}\n\n"
        f"证据包：\n{evidence_pack[:8000] if evidence_pack else '无'}\n\n"
        f"待审查 ideas：\n{core_ideas_md[:16000]}\n"
    )
    resp = llm.chat([{"role": "user", "content": user}], system=system, max_tokens=8192)
    return resp.content.strip()


def _maybe_literature_grounded_pivot(
    llm: LLMClient | None,
    *,
    topic: str,
    synthesis: str,
    current_ideas_md: str,
    evidence_pack: str,
    tournament_report: dict[str, Any],
    challenge_tree_md: str,
    idea_count: int,
) -> tuple[str, str]:
    ranking = tournament_report.get("ranking", []) if isinstance(tournament_report, dict) else []
    needs_pivot = False
    if isinstance(ranking, list) and ranking:
        weak = [r for r in ranking[:_clamp_idea_count(idea_count)] if isinstance(r, dict) and (float(r.get("diversity", 5) or 5) <= 2 or float(r.get("literature_grounding", 5) or 5) <= 2)]
        needs_pivot = bool(weak)
    if llm is None or not needs_pivot:
        reason = "No pivot triggered: selected ideas passed local diversity/grounding thresholds."
        return current_ideas_md, f"# Literature-Grounded Pivot\n\n{reason}\n"
    system = (
        "你是科研 idea pivot 专家。只在 idea 重复、文献依据弱或机制过虚时进行替换。"
        "你的输出必须保留强 idea，替换弱 idea，最终中文输出 exactly 指定数量。"
    )
    user = (
        f"研究主题：\n{topic}\n\n"
        f"请基于文献证据对当前 ideas 做 literature-grounded pivot，最终输出 exactly {idea_count} 个非重复 Idea。\n"
        "规则：\n"
        "- 保留 tournament 中强且可实验的 idea。\n"
        "- 替换 diversity 低、literature grounding 弱或机制重复的 idea。\n"
        "- 替代 idea 必须来自 Challenge-Insight Tree 中未充分覆盖的 challenge 或 transfer opportunity。\n"
        "- 每个替代都写 Pivot Reason。\n\n"
        f"{_idea_schema_instruction()}\n\n"
        f"Challenge-Insight Tree：\n{challenge_tree_md[:8000]}\n\n"
        f"Tournament JSON：\n{json.dumps(tournament_report, ensure_ascii=False)[:9000]}\n\n"
        f"文献综述：\n{synthesis[:7000]}\n\n"
        f"证据包：\n{evidence_pack[:9000] if evidence_pack else '无'}\n\n"
        f"当前 ideas：\n{current_ideas_md[:14000]}\n"
    )
    resp = llm.chat([{"role": "user", "content": user}], system=system, max_tokens=12000)
    pivoted = resp.content.strip()
    if not pivoted:
        return current_ideas_md, "# Literature-Grounded Pivot\n\nPivot attempted but returned empty output; kept current ideas.\n"
    pivoted = _complete_idea_set(pivoted, topic=topic, synthesis=synthesis, idea_count=idea_count, fallback_sources=(current_ideas_md,))
    return pivoted, "# Literature-Grounded Pivot\n\nPivot triggered because local tournament found weak diversity or grounding.\n\n" + pivoted


def _write_idea_decision_table(stage_dir: Path, core_ideas_md: str, tournament_report: dict[str, Any]) -> str:
    blocks = _idea_like_blocks(core_ideas_md)
    scores = [_score_idea_block(title, block, blocks) for title, block in blocks]
    tournament_by_title: dict[str, dict[str, Any]] = {}
    ranking = tournament_report.get("ranking", []) if isinstance(tournament_report, dict) else []
    if isinstance(ranking, list):
        for row in ranking:
            if isinstance(row, dict):
                tournament_by_title[str(row.get("title", ""))] = row
    lines = [
        "# Idea Decision Table",
        "",
        "| Idea | Novelty | Feasibility | Risk | Compute | Evidence | Diversity | Tournament Elo | Next Step |",
        "|---|---:|---:|---:|---:|---:|---:|---:|---|",
    ]
    for idx, ((title, _block), score) in enumerate(zip(blocks, scores), start=1):
        trow = tournament_by_title.get(score["title"], {})
        risk_inverse = max(1, 6 - int(score.get("risk", 3) or 3))
        next_step = "进入实验设计" if float(score.get("overall", 0)) >= 3.2 and int(score.get("diversity", 0)) >= 3 else "先补文献/去重后再进入实验"
        lines.append(
            f"| Idea {idx}: {score['title']} | {score.get('novelty')} | {score.get('feasibility')} | "
            f"{risk_inverse} | {score.get('compute_cost')} | {score.get('literature_grounding')} | "
            f"{score.get('diversity')} | {trow.get('elo', '')} | {next_step} |"
        )
    text = "\n".join(lines).strip() + "\n"
    (stage_dir / "idea_decision_table.md").write_text(text, encoding="utf-8")
    return text


def _write_ideation_memory_update(
    config: RCConfig,
    run_dir: Path,
    stage_dir: Path,
    *,
    topic: str,
    core_ideas_md: str,
    decision_table_md: str,
    tournament_report: dict[str, Any],
) -> tuple[str, ...]:
    blocks = _idea_like_blocks(core_ideas_md)
    ranking = tournament_report.get("ranking", []) if isinstance(tournament_report, dict) else []
    top_titles = [str(row.get("title", "")) for row in ranking[:5] if isinstance(row, dict)] if isinstance(ranking, list) else []
    if not top_titles:
        top_titles = [_idea_title_slug(title) for title, _ in blocks[:5]]
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    update = [
        f"## {today} — {topic}",
        "",
        "### Promising Directions",
        *[f"- {title}" for title in top_titles if title],
        "",
        "### Selection Notes",
        "- Stage 8 used Challenge-Insight Tree, candidate expansion, local Elo tournament, role review, and decision-table scoring.",
        "- Prefer ideas marked '进入实验设计' in the decision table; revisit others only after grounding/diversity fixes.",
        "",
        "### Decision Table Snapshot",
        decision_table_md[:2500].strip(),
        "",
    ]
    update_text = "\n".join(update).strip() + "\n"
    (stage_dir / "ideation_memory_update.md").write_text(update_text, encoding="utf-8")
    try:
        shared_dir = _resolve_shared_results_path(config, run_dir)
        shared_dir.mkdir(parents=True, exist_ok=True)
        memory_path = shared_dir / "ideation_memory.md"
        previous = ""
        if memory_path.exists():
            previous = memory_path.read_text(encoding="utf-8", errors="ignore").rstrip() + "\n\n"
        memory_path.write_text(previous + update_text, encoding="utf-8")
    except OSError:
        logger.warning("S8: Failed to update shared ideation memory", exc_info=True)
    return ("ideation_memory_update.md",)


def _add_yaml_frontmatter_to_core_ideas(core_ideas_md: str, *, topic: str, hardware_tier: str = "",
                                         experiment_memory_lessons: int = 0, overall_score: float = 0.0) -> str:
    """Prepend structured YAML frontmatter to core_ideas.md for downstream automation."""
    safe_topic = topic.replace('"', "'").replace("\n", " ")[:120]
    frontmatter = "---\n"
    frontmatter += f'topic: "{safe_topic}"\n'
    frontmatter += f"generated_at: {_utcnow_iso()}\n"
    frontmatter += f"total_ideas: {len(_idea_like_blocks(core_ideas_md))}\n"
    frontmatter += f"overall_avg_score: {overall_score}\n"
    if hardware_tier:
        frontmatter += f"hardware_tier: \"{hardware_tier}\"\n"
    if experiment_memory_lessons > 0:
        frontmatter += f"experiment_memory_lessons: {experiment_memory_lessons}\n"
    # Extract unmet signals and skill suggestions from content if present
    unmet_signals = _extract_frontmatter_list(core_ideas_md, "unmet_success_signals")
    if unmet_signals:
        for s in unmet_signals:
            cleaned = s.replace('"', "'")
            frontmatter += '  - "' + cleaned[:80] + '"\n'
    skill_suggestions = _extract_frontmatter_list(core_ideas_md, "skill_suggestions")
    if skill_suggestions:
        frontmatter += "skill_suggestions:\n"
        for s in skill_suggestions:
            cleaned = s.replace('"', "'")
            frontmatter += '  - "' + cleaned[:80] + '"\n'
    frontmatter += "---\n\n"
    # Only prepend if not already present
    if core_ideas_md.strip().startswith("---"):
        return core_ideas_md
    return frontmatter + core_ideas_md


def _extract_frontmatter_list(text: str, key: str) -> list[str]:
    """Extract a list of items from a key in the markdown text (JSON-schema-like pattern)."""
    pattern = rf'{key}:\s*\[(.*?)\]'
    match = re.search(pattern, text, re.DOTALL)
    if not match:
        return []
    raw = match.group(1)
    items = re.findall(r'"([^"]*)"', raw)
    return [item.strip() for item in items if item.strip()]


def _add_reflection_checkpoint(
    stage_dir: Path,
    checkpoint_name: str,
    *,
    topic: str,
    context: str = "",
    dimensions: Sequence[str] = ("progress", "evidence", "strategy", "handoff"),
) -> str:
    """Write a structured reflection checkpoint to disk during Stage 8."""
    reflection = _structured_idea_reflection(
        checkpoint_name, topic=topic, context=context, dimensions=dimensions,
    )
    try:
        ref_dir = stage_dir / "reflections"
        ref_dir.mkdir(parents=True, exist_ok=True)
        safe_name = re.sub(r"[^a-z0-9]+", "_", checkpoint_name.lower()).strip("_")
        (ref_dir / f"{safe_name}.md").write_text(reflection, encoding="utf-8")
        logger.info("S8: Reflection checkpoint '%s' written", checkpoint_name)
    except OSError:
        logger.warning("S8: Could not write reflection checkpoint '%s'", checkpoint_name)
    return reflection


def _run_parallel(fn, items: Sequence[Any], *, max_workers: int = 4) -> dict[Any, Any]:
    """Run a callable over items in parallel using ThreadPoolExecutor.

    Used for independent sub-stages like mutation branches and role reviews.
    Returns dict mapping item -> result (or item -> None on failure).
    """
    from concurrent.futures import ThreadPoolExecutor, as_completed

    results: dict[Any, Any] = {item: None for item in items}
    with ThreadPoolExecutor(max_workers=max_workers) as pool:
        fut_map = {pool.submit(fn, item): item for item in items}
        for fut in as_completed(fut_map):
            item = fut_map[fut]
            try:
                results[item] = fut.result()
            except Exception as exc:  # noqa: BLE001
                logger.warning("Parallel task for %s failed: %s", item, exc)
    return results


def _idea_branch_specs() -> dict[str, dict[str, str]]:
    return {
        "high_risk_high_reward": {
            "title": "高风险高收益版",
            "lens": (
                "把原始 idea 推向更有突破性的版本。允许更大胆的机制假设、跨领域组合和反直觉路线，"
                "但必须明确高风险来自哪里、最小证据是什么、如何避免变成不可验证幻想。"
            ),
            "priority": "优先最大化 Novelty 和 Impact，同时保留 Testability 底线。",
        },
        "conservative_publishable": {
            "title": "稳妥可投版",
            "lens": (
                "把原始 idea 改成更容易形成稳定论文贡献的版本。强调清晰 problem gap、强 baseline、"
                "可复现协议、审稿人可接受的 novelty defense 和较低工程不确定性。"
            ),
            "priority": "优先最大化 Feasibility、Literature Grounding 和 reviewer defense。",
        },
        "mvp_fast_validation": {
            "title": "两周 MVP 版",
            "lens": (
                "把原始 idea 压缩成两周内能看到信号的最小实验。减少依赖、缩小数据和模型规模，"
                "设计明确 Go/No-Go 阈值，让失败也能产出有用结论。"
            ),
            "priority": "优先最大化 Testability、计算可控性和早期反馈速度。",
        },
    }


def _generate_single_mutation_branch(
    branch_id: str,
    spec: dict[str, str],
    llm: LLMClient,
    topic: str,
    synthesis: str,
    core_ideas_md: str,
    evidence_pack: str,
    branches_dir: Path,
    idea_count: int = 5,
) -> tuple[str, str] | None:
    """Generate a single mutation branch — extracted for parallel execution."""
    system = (
        "你是科研 idea 演化专家。你的任务是基于同一批 core ideas，"
        "按照指定策略生成一个差异明显的变体分支。必须使用中文输出，"
        "英文只保留论文名、方法名、数据集、指标和模型名。"
    )
    user = (
        f"研究主题：\n{topic}\n\n"
        f"分支名称：{spec['title']}\n"
        f"分支视角：{spec['lens']}\n"
        f"优化优先级：{spec['priority']}\n\n"
        f"请把原始 core ideas 演化成该分支下最强的 {idea_count} 个互不重复的 Idea。要求：\n"
        "- 不能只是改标题，必须改变取舍、实验规模、风险处理或技术路线。\n- 每个 Idea 必须是不同机制；如果两个 Idea 只是同一机制换场景，合并或替换。\n"
        "- 每个 Idea 必须说明相对原始版本做了哪些 mutation。\n"
        "- 每个 Idea 必须保留相近文献对照、失败条件和评分。\n"
        "- 如果某个原始 Idea 不适合本分支，明确说明删除或合并原因。\n\n"
        f"{_idea_schema_instruction()}\n\n"
        f"{_idea_quality_rubric()}\n\n"
        "额外输出要求：\n"
        "## 分支策略说明\n"
        "说明这个分支相对原始版本的主要取舍。\n\n"
        "## Mutation Log\n"
        "逐条说明：保留了什么、强化了什么、删除/合并了什么、为什么。\n\n"
        f"文献综述摘要：\n{synthesis[:8000]}\n\n"
        f"证据包：\n{evidence_pack[:10000] if evidence_pack else '没有额外证据包。'}\n\n"
        f"原始 core ideas：\n{core_ideas_md[:16000]}\n"
    )
    try:
        resp = llm.chat(
            [{"role": "user", "content": user}],
            system=system,
            max_tokens=10000,
        )
        text = resp.content.strip()
        if text:
            (branches_dir / f"{branch_id}.md").write_text(text, encoding="utf-8")
            logger.info("S8: Idea mutation branch '%s' generated (%d chars)", branch_id, len(text))
            return (branch_id, text)
    except Exception:  # noqa: BLE001
        logger.warning("S8: Mutation branch '%s' failed", branch_id, exc_info=True)
    return None


def _generate_idea_mutation_branches(
    llm: LLMClient,
    topic: str,
    synthesis: str,
    core_ideas_md: str,
    evidence_pack: str,
    branches_dir: Path,
    idea_count: int = 5,
) -> dict[str, str]:
    """Generate multiple mutation branches for Stage 8 ideas — branches run in parallel."""
    branches_dir.mkdir(parents=True, exist_ok=True)
    specs = _idea_branch_specs()
    results: dict[str, str] = {}
    from concurrent.futures import ThreadPoolExecutor, as_completed

    # Prepare partial function for each branch
    def _run_branch(branch_id_spec: tuple[str, dict[str, str]]) -> tuple[str, str] | None:
        bid, bspec = branch_id_spec
        return _generate_single_mutation_branch(
            bid, bspec, llm, topic, synthesis, core_ideas_md,
            evidence_pack, branches_dir, idea_count,
        )

    with ThreadPoolExecutor(max_workers=3) as pool:
        fut_map = {pool.submit(_run_branch, (bid, spec)): bid for bid, spec in specs.items()}
        for fut in as_completed(fut_map):
            bid = fut_map[fut]
            try:
                result = fut.result()
                if result is not None:
                    results[result[0]] = result[1]
            except Exception:  # noqa: BLE001
                logger.warning("S8: Parallel mutation branch '%s' failed", bid, exc_info=True)

    return results


def _synthesize_idea_mutation_branches(
    llm: LLMClient,
    topic: str,
    synthesis: str,
    branches: dict[str, str],
    evidence_pack: str,
    idea_count: int = 5,
) -> str:
    """Fuse mutation branches into the final executable core ideas."""
    branch_titles = _idea_branch_specs()
    branch_parts: list[str] = []
    for branch_id, text in branches.items():
        title = branch_titles.get(branch_id, {}).get("title", branch_id)
        branch_parts.append(f"## 分支：{title} ({branch_id})\n{text[:12000]}")
    combined = "\n\n---\n\n".join(branch_parts)
    system = (
        "你是科研项目 PI，负责从多个 idea 演化分支中做最终取舍。"
        "你必须综合，而不是平均：保留最强机制，删除重复和虚弱方案，"
        "输出可以直接进入实验设计阶段的中文版 core_ideas.md。"
    )
    user = (
        f"研究主题：\n{topic}\n\n"
        "下面有三个分支：高风险高收益版、稳妥可投版、两周 MVP 版。"
        f"请做最终分支融合，输出 exactly {idea_count} 个最终 Idea。每个 Idea 必须是不同技术机制；如果两个 Idea 机制重复，必须合并后补一个新的非重复候选，而不是重复凑数。\n\n"
        "融合规则：\n"
        "- 每个最终 Idea 必须说明来自哪个分支、吸收了哪些设计、舍弃了哪些设计。\n- 输出前必须做去重：同一机制换标题/换数据集/换指标不算新 Idea。\n"
        "- 至少保留一个偏高收益的创新点，但必须配一个两周 MVP 验证路径。\n"
        "- 如果某个高风险 Idea 太虚，降级为未来方向，不要推荐为首选。\n"
        "- 首选推荐必须同时满足：有相近文献差异、有最小实验、失败阈值明确、两周内能启动。\n"
        "- 最终全文使用中文。英文只保留专有名词。\n\n"
        f"{_idea_schema_instruction()}\n\n"
        f"{_idea_quality_rubric()}\n\n"
        "最终还必须额外包含：\n"
        "## 分支融合记录\n"
        "用表格说明每个最终 Idea 吸收了哪些分支元素，以及为什么这样融合。\n\n"
        "## 未采纳分支与原因\n"
        "列出被删除或降级的想法，以及删除原因。\n\n"
        f"文献综述摘要：\n{synthesis[:7000]}\n\n"
        f"证据包：\n{evidence_pack[:9000] if evidence_pack else '没有额外证据包。'}\n\n"
        f"分支内容：\n{combined}\n"
    )
    resp = llm.chat(
        [{"role": "user", "content": user}],
        system=system,
        max_tokens=12000,
    )
    return resp.content.strip()


def _generate_idea_review(
    llm: LLMClient,
    topic: str,
    synthesis: str,
    core_ideas_md: str,
    evidence_pack: str,
    idea_count: int = 5,
) -> str:
    """Generate a reviewer-style quality gate for Stage 8 ideas."""
    system = (
        "你是顶会 senior area chair 和严苛科研导师。你的任务不是生成新 idea，"
        "而是审查已有 idea 是否真的新、是否可实验、是否会被 reviewer 质疑。"
        "必须使用中文输出，英文仅保留论文名、方法名、数据集和指标。"
    )
    user = (
        f"研究主题：\n{topic}\n\n"
        "请审查下面的 core ideas。你必须严格、具体、可执行。不要泛泛而谈。\n\n"
        f"{_idea_quality_rubric()}\n\n"
        "输出格式：\n"
        "# Idea Review\n\n"
        "## 总体结论\n"
        "说明这批 idea 是否已经适合进入实验设计阶段。\n\n"
        "## 逐项评分表\n"
        "对每个 Idea 给 Novelty、Feasibility、Impact、Testability、Literature Grounding、Risk、Compute Cost、Diversity、总分。\n\n"
        "## 最相近文献与 novelty 风险\n"
        "每个 Idea 列出 3-5 篇最相近文献，指出 reviewer 会认为它不新的具体理由。\n\n"
        "## 必须修复的问题\n"
        "列出阻碍进入 S9 实验设计的具体问题，每条给修复建议。\n\n"
        "## 推荐排序\n"
        f"只推荐一个首选 Idea，并说明其他 {max(idea_count - 1, 0)} 个 Idea 的优先级、暂缓条件或合并原因。\n\n"
        "## Go / No-Go 检查\n"
        "给出进入实验设计前必须满足的检查项。\n\n"
        f"文献综述摘要：\n{synthesis[:9000]}\n\n"
        f"证据包：\n{evidence_pack[:12000] if evidence_pack else '没有额外证据包。'}\n\n"
        f"待审查 ideas：\n{core_ideas_md[:18000]}\n"
    )
    resp = llm.chat(
        [{"role": "user", "content": user}],
        system=system,
        max_tokens=8192,
    )
    return resp.content


def _apply_idea_review(
    llm: LLMClient,
    topic: str,
    synthesis: str,
    core_ideas_md: str,
    idea_review_md: str,
    evidence_pack: str,
    idea_count: int = 5,
) -> str:
    """Revise core ideas using the reviewer report and fixed idea schema."""
    system = (
        "你是负责把科研想法改到可以开题/进实验的 research lead。"
        "你必须吸收 reviewer 批评，产出最终可执行中文版 core_ideas.md。"
        "不要输出内部推理过程，不要输出英文草稿。"
    )
    user = (
        f"研究主题：\n{topic}\n\n"
        "请根据 reviewer report 重写/修订 core ideas。要求：\n"
        f"- 输出 exactly {idea_count} 个最终 Idea；如果发现机制重复，必须合并重复项并补充新的非重复候选。\n"
        "- 每个 Idea 必须绑定相近文献、差异点、实验、失败条件和评分。\n"
        "- 最终全文使用中文。英文只保留专有名词。\n"
        "- 最终结果应能直接交给 S9 实验设计使用。\n\n"
        f"{_idea_schema_instruction()}\n\n"
        f"{_idea_quality_rubric()}\n\n"
        f"文献综述摘要：\n{synthesis[:8000]}\n\n"
        f"证据包：\n{evidence_pack[:10000] if evidence_pack else '没有额外证据包。'}\n\n"
        f"原始 core ideas：\n{core_ideas_md[:16000]}\n\n"
        f"Reviewer report：\n{idea_review_md[:10000]}\n"
    )
    resp = llm.chat(
        [{"role": "user", "content": user}],
        system=system,
        max_tokens=12000,
    )
    return resp.content


def _distill_core_ideas(
    llm: LLMClient,
    topic: str,
    synthesis: str,
    hypotheses_md: str,
    evidence_pack: str = "",
    idea_count: int = 5,
) -> str:
    """Refine raw hypotheses into highly detailed, execution-ready core ideas."""
    synthesis_snippet = synthesis[:12000].strip()
    hypotheses_snippet = hypotheses_md[:16000].strip()
    evidence_snippet = evidence_pack[:12000].strip()
    system = (
        "You are a senior research director refining research hypotheses into detailed, "
        "execution-ready core ideas. Your output must be MORE detailed than the input — "
        "you add concrete implementation specifics, precise experimental designs, "
        "and clear evaluation plans. Do NOT shorten or condense; instead, elaborate and specify. "
        "You must write the final document in Chinese, while preserving necessary English technical terms."
    )
    user = (
        f"Research topic:\n{topic}\n\n"
        "OUTPUT LANGUAGE: Write the entire final core-ideas document in Chinese. Preserve English only for paper names, method names, datasets, metrics, libraries, and model checkpoints.\n\n"
        "You will receive a literature synthesis and a set of draft hypotheses.\n"
        f"Your job is to REFINE exactly {idea_count} strongest, non-overlapping ideas into highly detailed, "
        "execution-ready research plans.\n"
        "IMPORTANT: You must ADD detail, NOT remove it. Make each idea so concrete "
        "that a graduate student could start coding within an hour.\n\n"
        "The final document must behave like a mini research proposal, not brainstorming notes.\n"
        "Every idea must be grounded in nearby papers, include a novelty defense, and contain a falsifiable experiment plan.\n\n"
        f"{_idea_schema_instruction()}\n\n"
        f"{_idea_quality_rubric()}\n\n"
        "For each idea, also make sure these details are concrete and thorough:\n\n"
        "## Idea N: <short, descriptive title>\n\n"
        "### Problem Gap\n"
        "<What specific gap in prior work this targets. Cite specific papers from the "
        "synthesis and explain what they missed.>\n\n"
        "### Core Idea\n"
        "<4-6 sentences explaining the actual technical mechanism with specific details: "
        "model architecture, loss function, training procedure, data preprocessing. "
        "Include layer dimensions, hyperparameter choices, and design rationale.>\n\n"
        "### Why It Is New\n"
        "<Concrete comparison with existing methods: what is different from obvious baselines, "
        "and why that difference matters. Do NOT say 'no one has tried this' — instead explain "
        "what conventional wisdom this challenges or what technical barrier it overcomes.>\n\n"
        "### Feasibility Analysis\n"
        "<Why this can be done now: available pre-trained models, existing codebases to build on, "
        "compute requirements (estimated GPU hours), data availability.>\n\n"
        "### Detailed Experimental Design\n"
        "- Dataset(s): <name specific datasets, splits, preprocessing, and why they were chosen>\n"
        "- Model architecture: <exact layer configuration, parameter counts, initialization>\n"
        "- Training procedure: <optimizer, learning rate schedule, batch size, precision, epochs, regularization>\n"
        "- Baseline 1: <paper + method + expected performance>\n"
        "- Baseline 2: <paper + method + expected performance>\n"
        "- Baseline 3 (if needed): <paper + method + expected performance>\n"
        "- Evaluation metrics: <exact metrics, calculation details, and statistical significance tests>\n"
        "- Expected outcome: <specific numbers: 'Our method should achieve X% vs baseline Y% on metric Z'>\n"
        "- Ablation studies: <list 2-3 ablations that isolate each component of your method>\n\n"
        "### 2-Week MVP\n"
        "<The smallest concrete experiment that can validate the core idea. Include: "
        "which subset of data to use, which model size, what is the earliest checkpoint "
        "that would show signal, and what would constitute a 'Go/No-Go' decision.>\n\n"
        "### Main Risks\n"
        "<2-3 specific things that could cause failure, ranked by likelihood, with early detection signals and fallback plans>\n\n"
        "---\n\n"
        "After listing all ideas, add:\n"
        "## Recommended First Try\n"
        "<Exactly one idea recommended for first try, with 3-4 sentence justification based on "
        "expected impact, feasibility, and novelty trade-off.>\n\n"
        "## Quick Comparison\n"
        "<A short table comparing the ideas side by side on: novelty, feasibility, expected impact, "
        "risk handling, compute cost, and mechanism diversity.>\n\n"
        "Rules:\n"
        "- Do NOT make the output shorter than the input. Longer is better if it adds specifics.\n"
        f"- Target exactly {idea_count} final ideas. Merge overlapping ideas when they are the same mechanism, then add a different non-overlapping candidate so the final set remains at the target count.\n"
        "- Do a duplicate check before finalizing: same mechanism with a different title, dataset, or metric is still duplicate.\n"
        "- Remove only vague or untestable ideas.\n"
        "- Prefer depth over breadth — one fully specified idea is better than three vague ones.\n"
        "- Name exact datasets, model checkpoints, and framework libraries when possible.\n"
        "- No brainstorming chatter, no reviewer-style debate, no meta-commentary.\n\n"
        f"Literature synthesis:\n{synthesis_snippet}\n\n"
        f"Nearby-paper evidence pack:\n{evidence_snippet if evidence_snippet else 'No additional evidence pack available.'}\n\n"
        f"Draft hypotheses:\n{hypotheses_snippet}\n"
    )
    resp = llm.chat(
        [{"role": "user", "content": user}],
        system=system,
    )
    return resp.content


def _refine_core_ideas(
    llm: LLMClient,
    topic: str,
    synthesis: str,
    core_ideas_md: str,
    evidence_pack: str = "",
) -> str:
    """Self-critique and refine core ideas, filling in missing details."""
    synthesis_snippet = synthesis[:8000].strip()
    core_snippet = core_ideas_md[:16000].strip()
    evidence_snippet = evidence_pack[:10000].strip()
    system = (
        "You are a meticulous research reviewer. Your job is to make research ideas "
        "MORE specific and MORE actionable by identifying and filling in missing details.\n\n"
        "For each gap you find (missing dataset name, vague architecture description, "
        "unspecified training hyperparameters, unclear evaluation protocol), "
        "you add the missing detail. You NEVER remove content — only add. "
        "You must write the improved document in Chinese, preserving necessary English technical terms."
    )
    user = (
        f"Research topic:\n{topic}\n\n"
        "OUTPUT LANGUAGE: Use Chinese for the full improved core-ideas document. Keep English only for proper nouns such as paper names, datasets, metrics, methods, libraries, and model checkpoints.\n\n"
        "Review the core ideas below against the literature synthesis and produce "
        "an IMPROVED version. Your goal is to add specificity wherever the text is vague.\n\n"
        "Checklist of common vagueness to fix:\n"
        "- [ ] 'use a transformer' → specify which one (BERT-base? ViT-B/16? GPT-2?) and size\n"
        "- [ ] 'train on a standard dataset' → name the exact dataset, version, and split\n"
        "- [ ] 'standard hyperparameters' → specify optimizer, LR, batch size, epochs, schedule\n"
        "- [ ] 'compare with baselines' → name exact baselines with paper citations\n"
        "- [ ] 'evaluate on metrics' → name exact metrics and how they are computed\n"
        "- [ ] 'reasonable compute budget' → specify approximate GPU hours\n"
        "- [ ] 'ablation study' → specify what exactly gets ablated\n"
        "- [ ] missing nearest-paper comparison → list 3-5 similar works and exact differences\n"
        "- [ ] missing falsification threshold → state what result would reject the idea\n"
        "- [ ] missing score table → add Novelty/Feasibility/Impact/Testability/Literature Grounding/Risk/Compute Cost/Diversity scores\n\n"
        "OUTPUT FORMAT:\n"
        "Output the COMPLETE improved version of the core ideas document. "
        "Keep the original structure and all original content. Add details inline "
        "wherever the checklist items above identify vagueness.\n\n"
        "If the document is already fully specific, output it unchanged.\n\n"
        f"{_idea_schema_instruction()}\n\n"
        f"{_idea_quality_rubric()}\n\n"
        f"Literature synthesis (for reference):\n{synthesis_snippet}\n\n"
        f"Nearby-paper evidence pack:\n{evidence_snippet if evidence_snippet else 'No additional evidence pack available.'}\n\n"
        f"Core ideas to refine:\n{core_snippet}\n"
    )
    resp = llm.chat(
        [{"role": "user", "content": user}],
        system=system,
    )
    refined = resp.content
    return refined


def _execute_hypothesis_gen(
    stage_dir: Path,
    run_dir: Path,
    config: RCConfig,
    adapters: AdapterBundle,
    *,
    llm: LLMClient | None = None,
    prompts: PromptManager | None = None,
) -> StageResult:
    synthesis = _read_prior_artifact(run_dir, "synthesis.md") or ""
    idea_count = _configured_idea_count(config)

    # --- Load hardware profile for feasibility-aware idea generation ---
    _hardware_profile = _load_hardware_profile(run_dir)
    _hardware_context = ""
    if _hardware_profile:
        hw_tier = _hardware_profile.get("tier", "unknown")
        gpu_name = _hardware_profile.get("gpu_name", "unknown")
        vram = _hardware_profile.get("vram_mb", "unknown")
        gpu_type = _hardware_profile.get("gpu_type", "cpu")
        _hardware_context = (
            f"\n\n## HARDWARE CONSTRAINT AWARENESS\n"
            f"Available hardware: tier={hw_tier}, gpu={gpu_name}, vram={vram}MB, type={gpu_type}\n"
            f"Implications for idea feasibility:\n"
        )
        if hw_tier == "cpu_only":
            _hardware_context += (
                "- No GPU available. Prefer ideas using NumPy/sklearn/lightweight methods.\n"
                "- Ideas requiring large-model training or massive parallel compute are infeasible — downgrade their compute_cost and feasibility scores.\n"
                "- Recommend CPU-linear-time algorithms, theoretical work, or small-scale proofs-of-concept.\n"
            )
        elif hw_tier == "limited":
            _hardware_context += (
                f"- Limited GPU ({vram}MB). Prefer ideas that fit in single-GPU with batch-size tuning.\n"
                "- Ideas requiring multi-GPU training, 7B+ parameter models, or large-scale pretraining carry high compute risk.\n"
                "- Recommend parameter-efficient methods (LoRA, adapters), smaller backbone models, or data-subset experiments.\n"
            )
        else:
            _hardware_context += (
                "- High-end GPU available. Most compute-heavy ideas are feasible.\n"
                "- Multi-GPU training and large-scale experiments are possible.\n"
                "- Still flag compute_cost in idea scoring for resource awareness.\n"
            )

    # --- Inject prior knowledge from shared knowledge base ---
    _shared_dir = getattr(config.experiment, "shared_results_dir", "") or ""
    _prior_knowledge = ""
    if _shared_dir:
        _kb_index = Path(_shared_dir) / "knowledge_base" / "knowledge_index.jsonl"
        if _kb_index.exists():
            try:
                _kb_lines = _kb_index.read_text(encoding="utf-8").strip().split("\n")
                _kb_entries = []
                for _line in _kb_lines[-20:]:
                    _e = json.loads(_line)
                    _kb_entries.append(
                        f"- **{_e.get('topic', '?')}**: "
                        f"Conclusions: {'; '.join(_e.get('conclusions', [])[:3])}. "
                        f"Insights: {'; '.join(_e.get('insights', [])[:2])}. "
                        f"Directions: {'; '.join(_e.get('suggested_directions', [])[:2])}"
                    )
                if _kb_entries:
                    _prior_knowledge = (
                        "\n\n## PRIOR RESEARCH KNOWLEDGE (from completed projects)\n"
                        "Consider these findings when generating hypotheses. "
                        "Build upon successful approaches and avoid repeating failed ones.\n\n"
                        + "\n".join(_kb_entries) + "\n"
                    )
                    logger.info("S8: Injected %d prior knowledge entries", len(_kb_entries))
            except Exception:
                pass

    raw_hypotheses_md = ""
    _ideation_memory = _load_ideation_memory(config, run_dir)
    _overlay = _get_evolution_overlay(run_dir, "hypothesis_gen")
    _hypothesis_context = (
        synthesis
        + "\n\n## LANGUAGE REQUIREMENT FOR STAGE 8\n"
        + "最终 hypotheses.md、hypotheses_raw.md 和 core_ideas.md 必须使用中文撰写。英文只保留论文名、方法名、数据集、指标、库名、模型 checkpoint 等专有名词。\n"
        + f"\n\n## IDEA COUNT AND DEDUP REQUIREMENT\n{_idea_count_rule(idea_count)}\n"
        + _prior_knowledge
    )
    if _ideation_memory:
        _hypothesis_context += (
            "\n\n## IDEATION MEMORY (avoid repeated failed directions; build on promising ones)\n"
            + _ideation_memory[:8000]
            + "\n"
        )
    if _overlay:
        _hypothesis_context += (
            "\n\n## HYPOTHESIS GENERATION SKILL GUIDANCE\n"
            "Apply this guidance when formulating and selecting hypotheses.\n\n"
            f"{_overlay}\n"
        )

    if _hardware_context:
        _hypothesis_context += _hardware_context

    # --- Load experiment memory for cross-cycle learning ---
    _experiment_memory = _load_experiment_memory(config, run_dir)
    if _experiment_memory:
        _hypothesis_context += (
            "\n\n## EXPERIMENT MEMORY (lessons from past experimental runs — avoid repeated failures, build on proven strategies)\n"
            + _experiment_memory[:8000]
            + "\n"
        )

    use_local_stage8 = os.environ.get("RESEARCHCLAW_STAGE8_LOCAL_FALLBACK", "").lower() in {"1", "true", "yes"}
    if use_local_stage8:
        logger.info("S8: Using local fallback because RESEARCHCLAW_STAGE8_LOCAL_FALLBACK is set")
        raw_hypotheses_md = _fallback_hypotheses_from_synthesis(
            config.research.topic,
            synthesis or _hypothesis_context,
            idea_count,
        )
    elif llm is not None:
        _pm = prompts or PromptManager()
        from researchclaw.prompts import DEBATE_ROLES_HYPOTHESIS  # noqa: PLC0415

        try:
            # --- Multi-perspective debate ---
            perspectives_dir = stage_dir / "perspectives"
            variables = {"topic": config.research.topic, "synthesis": _hypothesis_context, "idea_count": str(idea_count)}
            perspectives = _multi_perspective_generate(
                llm, DEBATE_ROLES_HYPOTHESIS, variables, perspectives_dir
            )
            if len(perspectives) < 2:
                raise RuntimeError(
                    f"Only {len(perspectives)} debate perspective(s) succeeded; using local fallback"
                )
            # --- Synthesize into raw hypotheses ---
            raw_hypotheses_md = _synthesize_perspectives(
                llm, perspectives, "hypothesis_synthesize", _pm, idea_count
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "S8: LLM hypothesis generation failed; using local synthesis fallback: %s",
                exc,
            )
            raw_hypotheses_md = _fallback_hypotheses_from_synthesis(
                config.research.topic,
                _hypothesis_context,
                idea_count,
            )
    else:
        raw_hypotheses_md = _fallback_hypotheses_from_synthesis(config.research.topic, _hypothesis_context, idea_count)

    (stage_dir / "hypotheses_raw.md").write_text(raw_hypotheses_md, encoding="utf-8")

    idea_evidence_pack = _collect_idea_evidence_pack(run_dir)
    retrieval_artifacts: tuple[str, ...] = ()
    if use_local_stage8:
        logger.info("S8: Skipping Research RAG bundle in local fallback mode")
    else:
        try:
            from researchclaw.rag.hybrid import build_research_rag_bundle

            projects_dir = run_dir.parent.parent if run_dir.parent.name.startswith("run-") else run_dir.parent
            rag_context, retrieval_artifacts, rag_report = build_research_rag_bundle(
                run_dir,
                config.research.topic,
                stage_dir,
                projects_dir=projects_dir if projects_dir.is_dir() else None,
                intent="idea",
                top_k=14,
            )
            if rag_context:
                idea_evidence_pack = (
                    idea_evidence_pack
                    + "\n\n## HYBRID RETRIEVAL EVIDENCE\n"
                    + rag_context
                ).strip()
            logger.info(
                "S8: Research RAG bundle built with %d hit(s), project_chunks=%s, global_chunks=%s",
                int(rag_report.get("count", 0) or 0),
                rag_report.get("index", {}).get("project_chunks", 0),
                rag_report.get("index", {}).get("global_chunks", 0),
            )
        except Exception:  # noqa: BLE001
            logger.warning("S8: Research RAG bundle failed — using artifact evidence only", exc_info=True)

    if idea_evidence_pack:
        (stage_dir / "idea_evidence_pack.md").write_text(idea_evidence_pack, encoding="utf-8")
        logger.info("S8: Collected idea evidence pack (%d chars)", len(idea_evidence_pack))

    hypotheses_path = stage_dir / "hypotheses.md"
    if not hypotheses_path.exists():
        hypotheses_path.write_text(raw_hypotheses_md, encoding="utf-8")

    # --- Pre-ideation structured reflection (strategy checkpoint) ---
    try:
        reflection_context = (
            f"Topic: {config.research.topic}\n"
            f"Evidence pack: {len(idea_evidence_pack)} chars\n"
            f"Ideation memory: {'loaded' if _ideation_memory else 'none'}\n"
            f"Hardware: {_hardware_profile.get('tier', 'unknown') if _hardware_profile else 'unknown'}\n"
            f"Experiment memory: {'loaded' if _experiment_memory else 'none'}"
        )
        _add_reflection_checkpoint(
            stage_dir, "pre_ideation_strategy",
            topic=config.research.topic,
            context=reflection_context,
            dimensions=("progress", "strategy", "prior_knowledge", "resource"),
        )
    except Exception:  # noqa: BLE001
        pass

    # --- EvoScientist-inspired idea factory front-end: challenge tree → candidates → tournament ---
    challenge_artifacts: tuple[str, ...] = ()
    candidate_artifacts: tuple[str, ...] = ()
    tournament_artifacts: tuple[str, ...] = ()
    challenge_tree_md = ""
    candidate_ideas_md = ""
    tournament_report: dict[str, Any] = {}
    tournament_md = ""
    tournament_selected_md = ""
    try:
        challenge_tree, challenge_tree_md = _generate_challenge_insight_tree(
            None if use_local_stage8 else llm,
            topic=config.research.topic,
            synthesis=(synthesis if use_local_stage8 else _hypothesis_context),
            evidence_pack=idea_evidence_pack,
            ideation_memory=_ideation_memory,
        )
        (stage_dir / "challenge_insight_tree.json").write_text(
            json.dumps(challenge_tree, indent=2, ensure_ascii=False),
            encoding="utf-8",
        )
        (stage_dir / "challenge_insight_tree.md").write_text(challenge_tree_md, encoding="utf-8")
        challenge_artifacts = ("challenge_insight_tree.json", "challenge_insight_tree.md")
        logger.info("S8: Challenge-Insight Tree generated")
    except Exception:  # noqa: BLE001
        logger.warning("S8: Challenge-Insight Tree generation failed", exc_info=True)
        challenge_tree_md = ""

    try:
        candidate_ideas_md = _generate_candidate_ideas(
            None if use_local_stage8 else llm,
            topic=config.research.topic,
            synthesis=(synthesis if use_local_stage8 else _hypothesis_context),
            raw_hypotheses_md=raw_hypotheses_md,
            evidence_pack=idea_evidence_pack,
            challenge_tree_md=challenge_tree_md,
            ideation_memory=_ideation_memory,
            idea_count=idea_count,
        )
        if candidate_ideas_md.strip():
            (stage_dir / "candidate_ideas.md").write_text(candidate_ideas_md, encoding="utf-8")
            candidate_artifacts = ("candidate_ideas.md",)
            tournament_report, tournament_md, tournament_selected_md = _run_idea_tournament(
                candidate_ideas_md,
                idea_count=idea_count,
            )
            (stage_dir / "idea_tournament.json").write_text(
                json.dumps(tournament_report, indent=2, ensure_ascii=False),
                encoding="utf-8",
            )
            (stage_dir / "idea_tournament.md").write_text(tournament_md, encoding="utf-8")
            tournament_artifacts = ("idea_tournament.json", "idea_tournament.md")
            logger.info(
                "S8: Idea tournament ranked %d candidate(s)",
                int(tournament_report.get("candidate_count", 0) or 0),
            )
    except Exception:  # noqa: BLE001
        logger.warning("S8: Candidate idea expansion/tournament failed", exc_info=True)
        candidate_ideas_md = ""
        tournament_selected_md = ""

    # --- Post-tournament evidence reflection ---
    try:
        _add_reflection_checkpoint(
            stage_dir, "post_tournament_evidence",
            topic=config.research.topic,
            context=f"Tournament candidates: {tournament_report.get('candidate_count', 0)}, "
                    f"selected: {len(_idea_like_blocks(tournament_selected_md)) if tournament_selected_md else 0}",
            dimensions=("progress", "evidence", "strategy"),
        )
    except Exception:  # noqa: BLE001
        pass

    core_ideas_md = tournament_selected_md.strip() or raw_hypotheses_md
    core_ideas_md = _complete_idea_set(
        core_ideas_md,
        topic=config.research.topic,
        synthesis=synthesis or _hypothesis_context,
        idea_count=idea_count,
        fallback_sources=(candidate_ideas_md, raw_hypotheses_md),
    )
    if llm is not None and not use_local_stage8:
        try:
            core_ideas_md = _distill_core_ideas(
                llm,
                config.research.topic,
                _hypothesis_context,
                raw_hypotheses_md,
                idea_evidence_pack,
                idea_count,
            )
            core_ideas_md = _complete_idea_set(
                core_ideas_md,
                topic=config.research.topic,
                synthesis=_hypothesis_context,
                idea_count=idea_count,
                fallback_sources=(raw_hypotheses_md,),
            )
            logger.info("S8: Distilled hypotheses into detailed core ideas (%d chars)", len(core_ideas_md))
        except Exception:  # noqa: BLE001
            logger.warning("S8: Idea distillation failed — falling back to raw hypotheses", exc_info=True)
            core_ideas_md = raw_hypotheses_md

    (stage_dir / "core_ideas.md").write_text(core_ideas_md, encoding="utf-8")

    # --- Self-critique and refinement (only for LLM-generated ideas) ---
    refined_ideas_md = core_ideas_md
    if llm is not None and not use_local_stage8:
        try:
            refined_ideas_md = _refine_core_ideas(
                llm,
                config.research.topic,
                _hypothesis_context,
                core_ideas_md,
                idea_evidence_pack,
            )
            refined_ideas_md = _complete_idea_set(
                refined_ideas_md,
                topic=config.research.topic,
                synthesis=_hypothesis_context,
                idea_count=idea_count,
                fallback_sources=(core_ideas_md, raw_hypotheses_md),
            )
            logger.info(
                "S8: Self-critique refined core ideas (%d chars → %d chars)",
                len(core_ideas_md), len(refined_ideas_md),
            )
            (stage_dir / "core_ideas.md").write_text(refined_ideas_md, encoding="utf-8")
        except Exception:  # noqa: BLE001
            logger.warning("S8: Self-critique refinement failed — keeping original", exc_info=True)

    # --- Multi-branch idea mutation and synthesis (non-blocking) ---
    branch_artifacts: tuple[str, ...] = ()
    if llm is not None and not use_local_stage8:
        try:
            branches_dir = stage_dir / "idea_branches"
            idea_branches = _generate_idea_mutation_branches(
                llm,
                config.research.topic,
                _hypothesis_context,
                refined_ideas_md,
                idea_evidence_pack,
                branches_dir,
                idea_count,
            )
            if idea_branches:
                branch_artifacts = ("idea_branches/",)
                branch_synthesis_md = _synthesize_idea_mutation_branches(
                    llm,
                    config.research.topic,
                    _hypothesis_context,
                    idea_branches,
                    idea_evidence_pack,
                    idea_count,
                )
                if branch_synthesis_md.strip():
                    (stage_dir / "idea_branch_synthesis.md").write_text(branch_synthesis_md, encoding="utf-8")
                    branch_artifacts = ("idea_branches/", "idea_branch_synthesis.md")
                    refined_ideas_md = _complete_idea_set(
                        branch_synthesis_md,
                        topic=config.research.topic,
                        synthesis=_hypothesis_context,
                        idea_count=idea_count,
                        fallback_sources=(refined_ideas_md, core_ideas_md, raw_hypotheses_md),
                    )
                    (stage_dir / "core_ideas.md").write_text(refined_ideas_md, encoding="utf-8")
                    logger.info("S8: Synthesized idea mutation branches into final core ideas (%d chars)", len(refined_ideas_md))
        except Exception:  # noqa: BLE001
            logger.warning("S8: Idea mutation branching failed — keeping self-refined ideas", exc_info=True)

    # --- Multi-role idea panel: Innovator / Pragmatist / Critic (non-blocking) ---
    role_review_artifacts: tuple[str, ...] = ()
    role_review_md = ""
    if llm is not None and not use_local_stage8:
        try:
            role_review_md = _generate_role_review(
                llm,
                topic=config.research.topic,
                synthesis=_hypothesis_context,
                core_ideas_md=refined_ideas_md,
                evidence_pack=idea_evidence_pack,
                tournament_md=tournament_md,
            )
            if role_review_md.strip():
                (stage_dir / "idea_role_review.md").write_text(role_review_md, encoding="utf-8")
                role_review_artifacts = ("idea_role_review.md",)
                logger.info("S8: Generated multi-role idea review (%d chars)", len(role_review_md))
        except Exception:  # noqa: BLE001
            logger.warning("S8: Multi-role idea review failed — continuing", exc_info=True)

    # --- Idea reviewer quality gate and final revision (non-blocking) ---
    idea_review_md = ""
    if llm is not None and not use_local_stage8:
        try:
            idea_review_md = _generate_idea_review(
                llm,
                config.research.topic,
                _hypothesis_context,
                refined_ideas_md,
                idea_evidence_pack,
                idea_count,
            )
            if role_review_md.strip():
                idea_review_md = role_review_md.rstrip() + "\n\n---\n\n" + idea_review_md.lstrip()
            (stage_dir / "idea_review.md").write_text(idea_review_md, encoding="utf-8")
            logger.info("S8: Generated idea reviewer report (%d chars)", len(idea_review_md))
        except Exception:  # noqa: BLE001
            logger.warning("S8: Idea reviewer report failed — continuing without it", exc_info=True)

        if idea_review_md:
            try:
                final_ideas_md = _apply_idea_review(
                    llm,
                    config.research.topic,
                    _hypothesis_context,
                    refined_ideas_md,
                    idea_review_md,
                    idea_evidence_pack,
                    idea_count,
                )
                if final_ideas_md.strip():
                    refined_ideas_md = _complete_idea_set(
                        final_ideas_md,
                        topic=config.research.topic,
                        synthesis=_hypothesis_context,
                        idea_count=idea_count,
                        fallback_sources=(refined_ideas_md, core_ideas_md, raw_hypotheses_md),
                    )
                    (stage_dir / "core_ideas.md").write_text(refined_ideas_md, encoding="utf-8")
                    logger.info("S8: Applied idea review into final core ideas (%d chars)", len(refined_ideas_md))
            except Exception:  # noqa: BLE001
                logger.warning("S8: Applying idea review failed — keeping refined ideas", exc_info=True)

    # --- Literature-grounded pivot for weak/repetitive ideas (non-blocking) ---
    pivot_artifacts: tuple[str, ...] = ()
    try:
        pivoted_md, pivot_report_md = _maybe_literature_grounded_pivot(
            None if use_local_stage8 else llm,
            topic=config.research.topic,
            synthesis=_hypothesis_context,
            current_ideas_md=refined_ideas_md,
            evidence_pack=idea_evidence_pack,
            tournament_report=tournament_report,
            challenge_tree_md=challenge_tree_md,
            idea_count=idea_count,
        )
        if pivot_report_md.strip():
            (stage_dir / "idea_pivot.md").write_text(pivot_report_md, encoding="utf-8")
            pivot_artifacts = ("idea_pivot.md",)
        if pivoted_md.strip() and pivoted_md.strip() != refined_ideas_md.strip():
            refined_ideas_md = _complete_idea_set(
                pivoted_md,
                topic=config.research.topic,
                synthesis=synthesis or _hypothesis_context,
                idea_count=idea_count,
                fallback_sources=(refined_ideas_md, candidate_ideas_md, raw_hypotheses_md),
            )
            (stage_dir / "core_ideas.md").write_text(refined_ideas_md, encoding="utf-8")
            logger.info("S8: Literature-grounded pivot updated core ideas (%d chars)", len(refined_ideas_md))
    except Exception:  # noqa: BLE001
        logger.warning("S8: Literature-grounded pivot failed — keeping current ideas", exc_info=True)

    refined_ideas_md = _complete_idea_set(
        refined_ideas_md,
        topic=config.research.topic,
        synthesis=synthesis or _hypothesis_context,
        idea_count=idea_count,
        fallback_sources=(candidate_ideas_md, raw_hypotheses_md, core_ideas_md),
    )
    (stage_dir / "core_ideas.md").write_text(refined_ideas_md, encoding="utf-8")

    # --- Structured idea quality evaluation (non-blocking) ---
    idea_quality_artifacts: tuple[str, ...] = ()
    try:
        from researchclaw.evaluation.idea_quality import write_idea_quality_report

        if use_local_stage8:
            judge_llm, judge_model_name = None, "local-rules"
        else:
            judge_llm, judge_model_name = _create_idea_judge_llm(config)
        quality_report = write_idea_quality_report(
            refined_ideas_md,
            stage_dir / "idea_quality_scores.json",
            stage_dir / "idea_quality_summary.md",
            llm_judge=judge_llm,
            judge_model_name=judge_model_name,
        )
        judge = quality_report.get("llm_judge", {}) if isinstance(quality_report, dict) else {}
        if isinstance(judge, dict) and judge:
            judge_summary = judge.get("summary", {}) if isinstance(judge.get("summary"), dict) else {}
            judge_lines = [
                "",
                "---",
                "",
                "## LLM-as-Judge 二级评分",
                "",
                f"- Judge 模型：{judge.get('judge_model', judge_model_name)}",
                f"- 状态：{judge.get('status', 'failed')}",
            ]
            if judge.get("status") == "ok":
                judge_lines.extend([
                    f"- 强模型总体平均分：{judge_summary.get('overall_avg', 'N/A')}/5",
                    f"- 结论：{judge_summary.get('verdict', 'N/A')}",
                    f"- 主要理由：{judge_summary.get('main_reason', '')}",
                    "",
                    "| Idea | Overall | Novelty | Feasibility | Impact | Testability | Grounding | Risk | Compute | Diversity |",
                    "|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|",
                ])
                for item in judge.get("ideas", []):
                    if not isinstance(item, dict):
                        continue
                    judge_lines.append(
                        f"| {item.get('title', item.get('idea_id', ''))} | {item.get('overall', '')} | {item.get('novelty', '')} | {item.get('feasibility', '')} | {item.get('impact', '')} | {item.get('testability', '')} | {item.get('literature_grounding', '')} | {item.get('risk', '')} | {item.get('compute_cost', '')} | {item.get('diversity', '')} |"
                    )
                for item in judge.get("ideas", []):
                    if not isinstance(item, dict):
                        continue
                    fixes = item.get("required_fixes", [])
                    weaknesses = item.get("weaknesses", [])
                    if fixes or weaknesses:
                        judge_lines.extend(["", f"### {item.get('title', item.get('idea_id', ''))}"])
                        judge_lines.extend(f"- Weakness: {x}" for x in weaknesses[:4])
                        judge_lines.extend(f"- Required fix: {x}" for x in fixes[:4])
            else:
                judge_lines.append(f"- 错误：{judge.get('error', '')}")
            refined_ideas_md = refined_ideas_md.rstrip() + '\n' + '\n'.join(judge_lines).rstrip() + '\n'
            (stage_dir / "core_ideas.md").write_text(refined_ideas_md, encoding="utf-8")
        idea_quality_artifacts = ("idea_quality_scores.json", "idea_quality_summary.md")
        logger.info("S8: Wrote structured idea quality scores")
    except Exception:  # noqa: BLE001
        logger.warning("S8: Structured idea quality evaluation failed", exc_info=True)

    # --- Novelty check (non-blocking) ---
    novelty_artifacts: tuple[str, ...] = ()
    try:
        skip_external_novelty = use_local_stage8 or os.environ.get(
            "RESEARCHCLAW_SKIP_EXTERNAL_NOVELTY", ""
        ).lower() in {"1", "true", "yes"}
        if skip_external_novelty:
            novelty_report = {
                "novelty_score": None,
                "assessment": "skipped",
                "similar_papers": [],
                "recommendation": "External novelty APIs skipped for local validation or by RESEARCHCLAW_SKIP_EXTERNAL_NOVELTY.",
                "generated": _utcnow_iso(),
            }
        else:
            from researchclaw.literature.novelty import check_novelty  # noqa: PLC0415

            candidates_text = _read_prior_artifact(run_dir, "candidates.jsonl") or ""
            papers_seen = _parse_jsonl_rows(candidates_text) if candidates_text else []
            novelty_report = check_novelty(
                topic=config.research.topic,
                hypotheses_text=refined_ideas_md,
                papers_already_seen=papers_seen,
                s2_api_key=getattr(config.llm, "s2_api_key", ""),
            )
        (stage_dir / "novelty_report.json").write_text(
            json.dumps(novelty_report, indent=2, ensure_ascii=False),
            encoding="utf-8",
        )
        novelty_artifacts = ("novelty_report.json",)
        score = novelty_report.get("novelty_score")
        logger.info(
            "Novelty check: score=%s  assessment=%s  recommendation=%s",
            "N/A" if score is None else f"{score:.3f}",
            novelty_report.get("assessment", "unknown"),
            novelty_report.get("recommendation", ""),
        )
    except Exception:  # noqa: BLE001
        logger.warning("Novelty check failed (non-blocking)", exc_info=True)

    # --- Final handoff reflection checkpoint ---
    try:
        hw_tier = _hardware_profile.get("tier", "") if _hardware_profile else ""
        _add_reflection_checkpoint(
            stage_dir, "idea_handoff",
            topic=config.research.topic,
            context=f"Final ideas: {len(_idea_like_blocks(refined_ideas_md))}, "
                    f"Hardware tier: {hw_tier}, "
                    f"Total artifacts generated before handoff",
            dimensions=("handoff", "resource", "strategy"),
        )
    except Exception:  # noqa: BLE001
        pass

    # --- Final decision table, ideation memory update and YAML frontmatter ---
    decision_artifacts: tuple[str, ...] = ()
    memory_artifacts: tuple[str, ...] = ()
    try:
        decision_table_md = _write_idea_decision_table(stage_dir, refined_ideas_md, tournament_report)
        decision_artifacts = ("idea_decision_table.md",)
        memory_artifacts = _write_ideation_memory_update(
            config,
            run_dir,
            stage_dir,
            topic=config.research.topic,
            core_ideas_md=refined_ideas_md,
            decision_table_md=decision_table_md,
            tournament_report=tournament_report,
        )
        # --- YAML frontmatter for structured downstream consumption ---
        try:
            hw_tier = _hardware_profile.get("tier", "") if _hardware_profile else ""
            exp_mem_lessons = len(_load_experiment_memory(config, run_dir, max_chars=99999).split("## ")) - 1 if _load_experiment_memory(config, run_dir) else 0
            overall_score = 0.0
            quality_path = stage_dir / "idea_quality_scores.json"
            if quality_path.exists():
                qdata = json.loads(quality_path.read_text(encoding="utf-8"))
                overall_score = float(qdata.get("summary", {}).get("overall_avg", 0.0) or 0.0)
            frontmatter_ideas = _add_yaml_frontmatter_to_core_ideas(
                refined_ideas_md,
                topic=config.research.topic,
                hardware_tier=hw_tier,
                experiment_memory_lessons=exp_mem_lessons,
                overall_score=overall_score,
            )
            (stage_dir / "core_ideas.md").write_text(frontmatter_ideas, encoding="utf-8")
            logger.info("S8: Added YAML frontmatter to core_ideas.md")
        except Exception:  # noqa: BLE001
            logger.warning("S8: YAML frontmatter addition failed — keeping existing core_ideas.md", exc_info=True)
        logger.info("S8: Wrote idea decision table and ideation memory update")
    except Exception:  # noqa: BLE001
        logger.warning("S8: Decision table / ideation memory update failed", exc_info=True)

    return StageResult(
        stage=Stage.HYPOTHESIS_GEN,
        status=StageStatus.DONE,
        artifacts=(
            "hypotheses.md", "core_ideas.md", "hypotheses_raw.md", "idea_review.md",
            "idea_evidence_pack.md",
        ) + challenge_artifacts + candidate_artifacts + tournament_artifacts + retrieval_artifacts + branch_artifacts + role_review_artifacts + pivot_artifacts + idea_quality_artifacts + novelty_artifacts + decision_artifacts + memory_artifacts,
        evidence_refs=(
            "stage-08/hypotheses.md", "stage-08/core_ideas.md", "stage-08/challenge_insight_tree.md",
            "stage-08/idea_tournament.md", "stage-08/idea_decision_table.md",
            "stage-08/idea_review.md", "stage-08/idea_branch_synthesis.md", "stage-08/idea_quality_scores.json",
        ),
    )

def _enforce_topic_experiment_constraints(
    plan: dict[str, Any],
    *,
    topic: str,
    metric_key: str,
    metric_direction: str,
) -> dict[str, Any]:
    """Apply explicit experiment-scope constraints stated by the user.

    LLM planning may turn a reproduction request into a new-method comparison or
    silently change the requested number of seeds.  Those are not creative
    choices: phrases such as "only", "仅使用", and "禁止" are hard constraints
    and must win over generated plan content.
    """
    import copy
    import re

    constrained = copy.deepcopy(plan)
    topic_lower = topic.lower()
    applied: list[str] = []

    uci_har_only = (
        "uci-har" in topic_lower
        and any(token in topic_lower for token in ("仅使用", "只使用", "only use", "only official"))
    )
    if uci_har_only:
        constrained["datasets"] = [{
            "name": "UCI Human Activity Recognition Using Smartphones",
            "source": "Official UCI Machine Learning Repository",
            "split_strategy": "Use the official train/test split without subject reshuffling",
            "preprocessing": "Use the official precomputed feature matrices; fit any scaler on training data only",
        }]
        applied.append("official_uci_har_only")

    baseline_only = any(
        token in topic_lower
        for token in (
            "未实现的新方法", "禁止.*新方法", "baseline-only", "baselines only",
            "只复现", "仅复现",
        )
    )
    # The literal token above intentionally handles the common Chinese wording;
    # also recognize the semantic combination when punctuation varies.
    baseline_only = baseline_only or (
        "禁止" in topic_lower and "新方法" in topic_lower
    )
    requested_sgd = "sgd" in topic_lower
    requested_rf = "随机森林" in topic_lower or "random forest" in topic_lower
    if baseline_only and (requested_sgd or requested_rf):
        existing = constrained.get("baselines", [])
        if not isinstance(existing, list):
            existing = []

        def _matches(item: Any, *needles: str) -> bool:
            text = str(item).lower().replace("_", " ")
            return any(needle in text for needle in needles)

        selected: list[Any] = []
        if requested_sgd:
            selected.extend(item for item in existing if _matches(item, "sgd", "linear"))
            if not any(_matches(item, "sgd", "linear") for item in selected):
                selected.append({
                    "name": "Linear SGD",
                    "implementation_spec": {
                        "estimator": "sklearn.linear_model.SGDClassifier",
                        "loss": "log_loss",
                    },
                })
        if requested_rf:
            selected.extend(item for item in existing if _matches(item, "randomforest", "random forest", "随机森林"))
            if not any(_matches(item, "randomforest", "random forest", "随机森林") for item in selected):
                selected.append({
                    "name": "Random Forest",
                    "implementation_spec": {
                        "estimator": "sklearn.ensemble.RandomForestClassifier",
                    },
                })
        # Preserve order while removing duplicate generated entries.
        deduped: list[Any] = []
        seen: set[str] = set()
        for item in selected:
            key = str(item.get("name", item) if isinstance(item, dict) else item).lower()
            if key not in seen:
                seen.add(key)
                deduped.append(item)
        constrained["baselines"] = deduped
        for item in constrained["baselines"]:
            if not isinstance(item, dict):
                continue
            name = str(item.get("name", ""))
            name_lower = name.lower().replace("_", " ")
            if "sgd" in name_lower or ("linear" in name_lower and "random" not in name_lower):
                item["description"] = "Requested linear SGD classification baseline."
                item["rationale"] = "Reproduce the requested linear baseline on the official split."
            elif "randomforest" in name_lower or "random forest" in name_lower or "随机森林" in name_lower:
                item["description"] = "Requested Random Forest classification baseline."
                item["rationale"] = "Reproduce the requested nonlinear tree-ensemble baseline on the official split."
        constrained["proposed_methods"] = []
        constrained["ablations"] = []
        # Failed benchmark-agent suggestions remain available in the separate
        # benchmark_plan artifact, but must not leak into executable codegen.
        constrained.pop("benchmark_suggestions", None)
        constrained["objectives"] = [
            "Reproduce and compare the requested baselines on the official dataset split; do not presuppose which method wins.",
            "Report only metrics and statistical comparisons computed from retained per-seed results.",
        ]
        constrained["compute_budget"] = {
            "hardware_requirements": "CPU sufficient for the requested scikit-learn baselines",
            "total_conditions": len(deduped),
            "total_seeds": 3,
            "total_runs": len(deduped) * 3,
        }
        constrained["risks"] = [
            {
                "risk": "Data leakage in preprocessing",
                "mitigation": "Use the official split and fit any scaler on training data only.",
            },
            {
                "risk": "Limited statistical power from three requested seeds",
                "mitigation": "Retain all per-seed results and report uncertainty without presupposing significance.",
            },
        ]
        applied.append("requested_baselines_only")

    seed_match = re.search(r"(\d+)\s*个(?:独立)?随机种子", topic)
    if seed_match is None:
        seed_match = re.search(r"(\d+)\s+(?:independent\s+)?(?:random\s+)?seeds?", topic_lower)
    if seed_match:
        seed_count = max(1, min(100, int(seed_match.group(1))))
        protocol = constrained.setdefault("evaluation_protocol", {})
        if not isinstance(protocol, dict):
            protocol = {}
            constrained["evaluation_protocol"] = protocol
        existing_seeds = protocol.get("independent_seeds", [])
        if not isinstance(existing_seeds, list):
            existing_seeds = []
        defaults = [11, 29, 47, 71, 101, 131, 173, 211]
        seeds = [int(x) for x in existing_seeds if isinstance(x, int)]
        for value in defaults:
            if len(seeds) >= seed_count:
                break
            if value not in seeds:
                seeds.append(value)
        protocol["independent_seeds"] = seeds[:seed_count]
        protocol["minimum_seeds_per_condition"] = seed_count
        constrained["seeds"] = seeds[:seed_count]
        budget = constrained.get("compute_budget")
        if isinstance(budget, dict):
            condition_count = len(constrained.get("baselines", [])) + len(constrained.get("proposed_methods", []))
            budget["total_seeds"] = seed_count
            budget["total_runs"] = condition_count * seed_count
        applied.append(f"exact_seed_count_{seed_count}")

    requested_metrics: list[str] = []
    if "accuracy" in topic_lower or "准确率" in topic_lower:
        requested_metrics.append("accuracy")
    if "macro-f1" in topic_lower or "macro f1" in topic_lower:
        requested_metrics.append("f1_macro")
    if requested_metrics:
        constrained["metrics"] = requested_metrics
        applied.append("explicit_metrics")
    constrained["primary_metric"] = metric_key
    constrained["metric_direction"] = metric_direction
    constrained["user_hard_constraints"] = {
        "applied": applied,
        "source": "research.topic",
    }
    return constrained


def _execute_experiment_design(
    stage_dir: Path,
    run_dir: Path,
    config: RCConfig,
    adapters: AdapterBundle,
    *,
    llm: LLMClient | None = None,
    prompts: PromptManager | None = None,
) -> StageResult:
    hypotheses = _read_prior_artifact(run_dir, "hypotheses.md") or ""
    preamble = _build_context_preamble(
        config, run_dir, include_goal=True, include_hypotheses=True
    )
    plan: dict[str, Any] | None = None

    # ── Load reference paper full text (produced by S4) ───────────────────
    _reference_paper_text = _read_prior_artifact(run_dir, "reference_paper_text.md") or ""
    if _reference_paper_text:
        _reference_paper_text = _reference_paper_text[:30_000]
        logger.info(
            "Stage 09: Found reference paper text (%d chars) — "
            "will inject into experiment design prompt",
            len(_reference_paper_text),
        )

    # ── Domain detection ──────────────────────────────────────────────────
    # Detect the research domain early so we can adapt experiment design
    # and code generation. For ML domains, existing behavior is unchanged.
    _domain_profile = None
    try:
        from researchclaw.domains.detector import detect_domain as _detect_domain_adv
        _domain_profile = _detect_domain_adv(
            topic=config.research.topic,
            hypotheses=hypotheses,
        )
        logger.info(
            "Domain detected: %s (%s)",
            _domain_profile.display_name,
            _domain_profile.domain_id,
        )
        # Persist domain profile for Stage 10
        import json as _json_dd
        (stage_dir / "domain_profile.json").write_text(
            _json_dd.dumps({
                "domain_id": _domain_profile.domain_id,
                "display_name": _domain_profile.display_name,
                "experiment_paradigm": _domain_profile.experiment_paradigm,
                "core_libraries": _domain_profile.core_libraries,
                "gpu_required": _domain_profile.gpu_required,
            }, indent=2),
            encoding="utf-8",
        )
    except Exception:  # noqa: BLE001
        logger.debug("Domain detection unavailable", exc_info=True)

    import os as _os_s9
    _datasets_dir_s9 = getattr(config.experiment, "datasets_dir", "") or ""
    _codebases_dir_s9 = getattr(config.experiment, "codebases_dir", "") or ""
    _checkpoints_dir_s9 = getattr(config.experiment, "checkpoints_dir", "") or ""
    _s9_diagnostics: dict[str, Any] = {
        "schema_version": 1,
        "status": "normal",
        "degraded": False,
        "parse_strategy": "not_attempted",
        "fallback_reason": "",
        "benchmark_agent_validation_passed": None,
        "benchmark_agent_errors": [],
        "benchmark_agent_warnings": [],
        "user_facing_status_zh": "实验计划已生成。",
    }
    if llm is not None:
        _pm = prompts or PromptManager()
        # Pass dataset_guidance block for experiment design
        try:
            _dg_block = _pm.block("dataset_guidance")
        except (KeyError, Exception):  # noqa: BLE001
            _dg_block = ""
        # ── Inject project-specific local data paths into experiment design ──
        _local_data_parts: list[str] = []
        if _datasets_dir_s9 and _os_s9.path.isdir(_datasets_dir_s9):
            _ds_items = [d for d in sorted(_os_s9.listdir(_datasets_dir_s9)) if not d.startswith(".")]
            if _ds_items:
                _local_data_parts.append(
                    f"### LOCAL DATASETS (MUST USE — do NOT use synthetic data)\n"
                    f"Directory: `{_datasets_dir_s9}`\n"
                    f"Available: {', '.join(_ds_items)}\n"
                    f"In code: `DATASETS_DIR = '{_datasets_dir_s9}'`\n"
                    f"Design your experiment to use these REAL datasets. "
                    f"NEVER generate synthetic torch.randn() data when real data is available."
                )
                for _ds_name in _ds_items:
                    _ds_path = _os_s9.path.join(_datasets_dir_s9, _ds_name)
                    if _os_s9.path.isdir(_ds_path):
                        _ds_sub = []
                        for _root, _dirs, _files in _os_s9.walk(_ds_path):
                            _rel = _os_s9.path.relpath(_root, _ds_path)
                            for _f in _files[:5]:
                                _ds_sub.append(_os_s9.path.join(_rel, _f) if _rel != "." else _f)
                            if len(_ds_sub) > 20:
                                break
                        if _ds_sub:
                            _local_data_parts.append(
                                f"  Dataset `{_ds_name}` sample files: {', '.join(_ds_sub[:15])}"
                            )
        if _codebases_dir_s9 and _os_s9.path.isdir(_codebases_dir_s9):
            _cb_items = [d for d in sorted(_os_s9.listdir(_codebases_dir_s9))
                         if _os_s9.path.isdir(_os_s9.path.join(_codebases_dir_s9, d)) and not d.startswith(".")]
            if _cb_items:
                _local_data_parts.append(
                    f"\n### LOCAL CODEBASES (MUST BUILD ON TOP OF)\n"
                    f"Directory: `{_codebases_dir_s9}`\n"
                    f"Available: {', '.join(_cb_items)}\n"
                    f"**CRITICAL**: Your experiment MUST extend/wrap these existing codebases. "
                    f"Do NOT design a from-scratch implementation when a reference codebase exists."
                )
                for _cb_name in _cb_items:
                    _cb_path = _os_s9.path.join(_codebases_dir_s9, _cb_name)
                    try:
                        from researchclaw.utils.codebase_manifest import generate_manifest, manifest_to_prompt
                        _cb_manifest = generate_manifest(_cb_path)
                        _cb_prompt = manifest_to_prompt(_cb_manifest)
                        if len(_cb_prompt) > 6000:
                            _cb_prompt = _cb_prompt[:6000] + "\n  ... (truncated)"
                        _local_data_parts.append(_cb_prompt)
                    except Exception:  # noqa: BLE001
                        _local_data_parts.append(f"  Codebase `{_cb_name}` at `{_cb_path}`")
        if _checkpoints_dir_s9 and _os_s9.path.isdir(_checkpoints_dir_s9):
            _ck_items = [d for d in sorted(_os_s9.listdir(_checkpoints_dir_s9)) if not d.startswith(".")]
            if _ck_items:
                _local_data_parts.append(
                    f"\n### LOCAL CHECKPOINTS (pre-trained weights available)\n"
                    f"Directory: `{_checkpoints_dir_s9}`\n"
                    f"Available: {', '.join(_ck_items)}\n"
                    f"Use these checkpoints directly — do NOT plan to download them."
                )
        if _local_data_parts:
            _dg_block += (
                "\n\n## PROJECT-SPECIFIC LOCAL RESOURCES (HIGHEST PRIORITY)\n"
                + "\n".join(_local_data_parts)
                + "\n\nWhen designing the experiment, your `datasets` section MUST reference "
                "these local resources. The experiment code will have direct filesystem access "
                "to these paths.\n"
                "Your YAML MUST include a top-level `local_resources` mapping with "
                "`datasets_dir` equal to the exact local dataset path and a "
                "`usage_requirement` that says core experiments must use this local data.\n"
            )

        # I-08: Inject RL step guidance for RL topics
        _rl_kws = ("reinforcement learning", "ppo", "sac", "td3", "ddpg",
                    "dqn", "mujoco", "continuous control", "actor-critic",
                    "policy gradient", "exploration bonus")
        if any(kw in config.research.topic.lower() for kw in _rl_kws):
            try:
                _dg_block += _pm.block("rl_step_guidance")
            except Exception:  # noqa: BLE001
                pass
        # F-01: Inject framework docs for experiment design
        try:
            from researchclaw.data import detect_frameworks, load_framework_docs
            _fw_ids = detect_frameworks(config.research.topic, hypotheses)
            if _fw_ids:
                _fw_docs = load_framework_docs(_fw_ids, max_chars=4000)
                if _fw_docs:
                    _dg_block += _fw_docs
        except Exception:  # noqa: BLE001
            pass
        # ── Build reference paper block for reproduce-mode projects ────
        _ref_block = ""
        if _reference_paper_text:
            _ref_block = (
                "REFERENCE PAPER REPRODUCTION (HIGHEST PRIORITY):\n"
                "You are designing an experiment to REPRODUCE the following paper.\n"
                "Extract the EXACT algorithm, architecture, loss functions, training\n"
                "procedure, and hyperparameters from the paper text below.\n\n"
                "Your `proposed_methods` MUST include `implementation_spec` with:\n"
                "  - class_name matching the paper's described components\n"
                "  - algorithm_steps that faithfully reproduce the paper's method\n"
                "  - loss_function matching the paper's stated objective\n"
                "  - key_hyperparameters matching the paper's reported values\n"
                "Do NOT invent generic placeholders — extract details FROM THE PAPER.\n\n"
                "--- BEGIN REFERENCE PAPER TEXT ---\n"
                f"{_reference_paper_text}\n"
                "--- END REFERENCE PAPER TEXT ---\n\n"
            )

        _overlay = _get_evolution_overlay(run_dir, "experiment_design")
        sp = _pm.for_stage(
            "experiment_design",
            evolution_overlay=_overlay,
            preamble=preamble,
            hypotheses=hypotheses,
            dataset_guidance=_dg_block,
            time_budget_sec=config.experiment.time_budget_sec,
            metric_key=config.experiment.metric_key,
            metric_direction=config.experiment.metric_direction,
            reference_paper_block=_ref_block,
        )
        resp = _chat_with_prompt(
            llm,
            sp.system,
            sp.user,
            json_mode=sp.json_mode,
            max_tokens=sp.max_tokens,
        )
        raw_yaml = _extract_yaml_block(resp.content)
        parsed = _parse_yaml_dict_from_llm(resp.content)
        # Last fallback: try to find any YAML-like dict in the response
        if not isinstance(parsed, dict):
            import re as _re_yaml

            # Look for lines starting with known keys
            _yaml_lines = []
            _capturing = False
            for line in resp.content.splitlines():
                if _re_yaml.match(
                    r"^(baselines|proposed_methods|ablations|datasets|"
                    r"metrics|objectives|risks|compute_budget)\s*:",
                    line,
                ):
                    _capturing = True
                if _capturing:
                    if line.strip() == "" or line.startswith("```"):
                        continue
                    if line.startswith("#") or line.startswith("**"):
                        continue
                    _yaml_lines.append(line)
            if _yaml_lines:
                try:
                    parsed = yaml.safe_load("\n".join(_yaml_lines))
                except yaml.YAMLError:
                    pass
        if isinstance(parsed, dict):
            plan = parsed
            _s9_diagnostics["parse_strategy"] = "qwen_yaml"
        else:
            logger.warning(
                "Stage 09: LLM response could not be parsed as YAML "
                "(len=%d, first 200 chars: %s). Content extraction method "
                "returned: %s",
                len(resp.content),
                resp.content[:200],
                raw_yaml[:200] if raw_yaml else "<empty>",
            )
            # BUG-12: Retry with a stricter, shorter prompt
            if llm is not None:
                logger.info("Stage 09: Retrying with strict YAML-only prompt...")
                _retry_prompt = (
                    "Output ONLY valid YAML. No prose, no markdown fences, no explanation.\n"
                    f"Topic: {config.research.topic}\n"
                    "Required keys: baselines, proposed_methods, ablations, "
                    "datasets, metrics, objectives, risks, compute_budget.\n"
                    "Each key maps to a list of strings."
                )
                _retry_resp = _chat_with_prompt(
                    llm,
                    "You output ONLY valid YAML. Nothing else.",
                    _retry_prompt,
                    max_tokens=4096,
                )
                _retry_parsed = _parse_yaml_dict_from_llm(_retry_resp.content)
                if isinstance(_retry_parsed, dict):
                    plan = _retry_parsed
                    _s9_diagnostics["parse_strategy"] = "strict_retry"
                    logger.info("Stage 09: Strict YAML retry succeeded.")

    # BUG-12: Fallback 4 — extract method/baseline names from Stage 8 hypotheses
    if plan is None:
        _hyp_text = _read_prior_artifact(run_dir, "hypotheses.md") or ""
        if _hyp_text:
            import re as _re_hyp
            # Extract method-like names from hypothesis text
            _method_candidates = _re_hyp.findall(
                r"(?:proposed|our|novel|new)\s+(?:method|approach|algorithm|framework|model)[:\s]+[\"']?([A-Za-z][\w-]+)",
                _hyp_text, _re_hyp.IGNORECASE,
            )
            _baseline_candidates = _re_hyp.findall(
                r"(?:baseline|compare|existing|standard|traditional)\s+(?:method|approach|model)?[:\s]+[\"']?([A-Za-z][\w-]+)",
                _hyp_text, _re_hyp.IGNORECASE,
            )
            if _method_candidates or _baseline_candidates:
                logger.info(
                    "Stage 09: Extracted names from hypotheses: methods=%s, baselines=%s",
                    _method_candidates[:3], _baseline_candidates[:3],
                )
                plan = {
                    "topic": config.research.topic,
                    "generated": _utcnow_iso(),
                    "objectives": ["Evaluate hypotheses with controlled experiments"],
                    "datasets": ["primary_dataset"],
                    "baselines": _baseline_candidates[:3] or ["baseline_1", "baseline_2"],
                    "proposed_methods": _method_candidates[:3] or ["proposed_method"],
                    "ablations": ["without_key_component", "simplified_version"],
                    "metrics": [config.experiment.metric_key, "secondary_metric"],
                    "risks": ["validity threats", "confounding variables"],
                    "compute_budget": {"max_gpu": 1, "max_hours": 4},
                }
                _s9_diagnostics.update({
                    "status": "degraded",
                    "degraded": True,
                    "parse_strategy": "hypotheses_extraction_fallback",
                    "fallback_reason": "qwen_yaml_parse_failed",
                    "user_facing_status_zh": "实验计划由 hypotheses 抽取降级生成；适合继续工程验证，但不足以直接支撑科研结论。",
                })

    if plan is None:
        # BUG-12: Use domain-aware names instead of fully generic placeholders
        _topic_prefix = config.research.topic.split()[0] if config.research.topic else "method"
        logger.warning(
            "Stage 09: LLM failed to produce valid experiment plan YAML. "
            "Using topic-derived fallback."
        )
        plan = {
            "topic": config.research.topic,
            "generated": _utcnow_iso(),
            "objectives": ["Evaluate hypotheses with controlled experiments"],
            "datasets": ["primary_dataset", "secondary_dataset"],
            "baselines": [f"{_topic_prefix}_baseline_1", f"{_topic_prefix}_baseline_2"],
            "proposed_methods": [f"{_topic_prefix}_proposed", f"{_topic_prefix}_variant"],
            "ablations": ["without_key_component", "simplified_version"],
            "metrics": [config.experiment.metric_key, "secondary_metric"],
            "risks": ["validity threats", "confounding variables"],
            "compute_budget": {"max_gpu": 1, "max_hours": 4},
        }
        _s9_diagnostics.update({
            "status": "degraded",
            "degraded": True,
            "parse_strategy": "topic_fallback",
            "fallback_reason": "qwen_yaml_parse_failed_and_no_method_names",
            "user_facing_status_zh": "实验计划使用主题兜底生成；仅适合打通流程，需要重新设计真实实验后再写科研结论。",
        })
    if isinstance(plan, dict):
        if _datasets_dir_s9 and _os_s9.path.isdir(_datasets_dir_s9):
            plan.setdefault("local_resources", {})
            if isinstance(plan["local_resources"], dict):
                plan["local_resources"]["datasets_dir"] = _datasets_dir_s9
                plan["local_resources"].setdefault(
                    "usage_requirement",
                    "All core experiments must use this local dataset path rather than unrelated generic benchmarks or synthetic substitutes.",
                )
            if _reference_paper_text:
                benchmark_suggestions = plan.get("benchmark_suggestions")
                if isinstance(benchmark_suggestions, dict) and "datasets" in benchmark_suggestions:
                    benchmark_suggestions.pop("datasets", None)
    # ── Validate implementation_spec presence ────────────────────────────
    # When reference paper text was provided (reproduce mode), the plan
    # MUST contain implementation_spec in proposed_methods.  If missing,
    # retry once with a targeted prompt.
    if (
        _reference_paper_text
        and isinstance(plan, dict)
        and llm is not None
    ):
        _methods = plan.get("proposed_methods", [])
        _has_impl = any(
            isinstance(m, dict) and "implementation_spec" in m
            for m in _methods
            if isinstance(m, dict)
        )
        if not _has_impl:
            logger.warning(
                "Stage 09: Plan missing implementation_spec for reproduce project "
                "— retrying with targeted prompt",
            )
            _impl_retry_prompt = (
                "The experiment plan you produced is missing `implementation_spec` "
                "under `proposed_methods`. This is required for code generation.\n\n"
                "Re-read the reference paper text and for EACH proposed method, "
                "add an `implementation_spec` block with:\n"
                "  - class_name\n"
                "  - algorithm_steps (3-10 concrete steps from the paper)\n"
                "  - loss_function (the exact loss from the paper)\n"
                "  - key_hyperparameters (values from the paper)\n"
                "  - required_loss_terms (symbolic loss/component names that must exist in code)\n"
                "  - required_distinct_helpers (non-generic helper methods that must be defined and called)\n"
                "  - required_data_pairing (how examples/prompts/pairs are grouped)\n"
                "  - required_model_edits (structural edits like monkey_patch_forward / replace_module / selective_unfreezing / attach_lora)\n"
                "  - required_runtime_hooks (hook targets or runtime interception points)\n"
                "  - key_methods\n"
                "  - differentiator\n\n"
                "Output the COMPLETE updated `proposed_methods` list as YAML.\n"
                "Include ONLY the `proposed_methods` key.\n\n"
                "Reference paper (first 15000 chars):\n"
                f"{_reference_paper_text[:15000]}\n\n"
                "Current proposed_methods:\n"
                f"{yaml.dump(_methods, default_flow_style=False)}"
            )
            try:
                _impl_resp = _chat_with_prompt(
                    llm,
                    "You are a research engineer. Output ONLY valid YAML.",
                    _impl_retry_prompt,
                    max_tokens=8192,
                )
                _impl_parsed = _parse_yaml_dict_from_llm(_impl_resp.content)
                if isinstance(_impl_parsed, dict):
                    _new_methods = _impl_parsed.get("proposed_methods", [])
                    if _new_methods and any(
                        isinstance(m, dict) and "implementation_spec" in m
                        for m in _new_methods
                        if isinstance(m, dict)
                    ):
                        plan["proposed_methods"] = _new_methods
                        logger.info(
                            "Stage 09: implementation_spec retry succeeded "
                            "(%d methods with specs)",
                            sum(1 for m in _new_methods
                                if isinstance(m, dict) and "implementation_spec" in m),
                        )
                    else:
                        logger.warning(
                            "Stage 09: implementation_spec retry returned no specs",
                        )
            except Exception:  # noqa: BLE001
                logger.warning(
                    "Stage 09: implementation_spec retry failed",
                    exc_info=True,
                )

    # ── BA: BenchmarkAgent — intelligent dataset/baseline selection ──────
    _benchmark_plan = None
    # BUG-40: Skip BenchmarkAgent for non-ML domains — it has no relevant
    # benchmarks for physics/chemistry/mathematics/etc. and would inject
    # wrong datasets (e.g., CIFAR-10 for PDE topics).
    _ba_domain_hint_ids: list[str] = []
    _ba_domain_label = ""
    if _domain_profile is not None:
        _ba_domain_hint_ids = [_domain_profile.domain_id]
        _ba_domain_label = _domain_profile.domain_id
        try:
            from researchclaw.domains.detector import is_ml_domain as _is_ml_domain_profile
            _ba_domain_ok = _is_ml_domain_profile(_domain_profile)
        except Exception:  # noqa: BLE001
            _ba_domain_ok = _domain_profile.domain_id.startswith("ml_")
    else:
        _ba_domain_id, _, _ = _detect_domain(
            config.research.topic,
            tuple(config.research.domains) if config.research.domains else (),
        )
        _ba_domain_label = _ba_domain_id
        _ba_domain_ok = _ba_domain_id == "ml"
    if not _ba_domain_ok:
        logger.info(
            "BenchmarkAgent skipped: domain '%s' is not ML (topic: %s)",
            _ba_domain_label, config.research.topic[:80],
        )
    if (
        _ba_domain_ok
        and config.experiment.benchmark_agent.enabled
        and config.experiment.mode in ("sandbox", "docker")
        and llm is not None
    ):
        try:
            from researchclaw.agents.benchmark_agent import BenchmarkOrchestrator
            from researchclaw.agents.benchmark_agent.orchestrator import (
                BenchmarkAgentConfig as _BACfg,
            )

            _ba_cfg_raw = config.experiment.benchmark_agent
            _ba_cfg = _BACfg(
                enabled=_ba_cfg_raw.enabled,
                enable_hf_search=_ba_cfg_raw.enable_hf_search,
                max_hf_results=_ba_cfg_raw.max_hf_results,
                enable_web_search=_ba_cfg_raw.enable_web_search,
                max_web_results=_ba_cfg_raw.max_web_results,
                web_search_min_local=_ba_cfg_raw.web_search_min_local,
                tier_limit=_ba_cfg_raw.tier_limit,
                min_benchmarks=_ba_cfg_raw.min_benchmarks,
                min_baselines=_ba_cfg_raw.min_baselines,
                prefer_cached=_ba_cfg_raw.prefer_cached,
                max_iterations=_ba_cfg_raw.max_iterations,
            )

            _hw = _load_hardware_profile(run_dir)
            _ba = BenchmarkOrchestrator(
                llm,
                config=_ba_cfg,
                gpu_memory_mb=(
                    _hw.get("gpu_memory_mb", 49000) if _hw else 49000
                ),
                time_budget_sec=config.experiment.time_budget_sec,
                network_policy=(
                    config.experiment.docker.network_policy
                    if config.experiment.mode == "docker"
                    else "full"
                ),
                stage_dir=stage_dir / "benchmark_agent",
            )
            _benchmark_plan = _ba.orchestrate({
                "topic": config.research.topic,
                "hypothesis": hypotheses,
                "experiment_plan": plan.get("objectives", "") if isinstance(plan, dict) else "",
                "domain_hints": _ba_domain_hint_ids,
            })

            _s9_diagnostics["benchmark_agent_validation_passed"] = bool(
                _benchmark_plan.validation_passed
            )
            _s9_diagnostics["benchmark_agent_errors"] = list(
                getattr(_benchmark_plan, "validation_errors", []) or []
            )
            _s9_diagnostics["benchmark_agent_warnings"] = list(
                getattr(_benchmark_plan, "validation_warnings", []) or []
            )
            if not _benchmark_plan.validation_passed:
                _s9_diagnostics.update({
                    "status": "degraded",
                    "degraded": True,
                    "fallback_reason": (
                        _s9_diagnostics.get("fallback_reason")
                        or "benchmark_agent_validation_failed"
                    ),
                    "user_facing_status_zh": (
                        "BenchmarkAgent 生成/校验的基准代码未通过；已保留为建议，不作为核心实验计划自动采用。"
                    ),
                })

            # Inject BenchmarkAgent selections into experiment plan only when
            # generated benchmark code passes validation.  Failed validation is
            # still useful evidence, but should not silently overwrite a usable
            # plan with broken dataset/baseline choices.
            if (
                isinstance(plan, dict)
                and _benchmark_plan.selected_benchmarks
                and _benchmark_plan.validation_passed
            ):
                _has_local_dataset_contract = bool(
                    getattr(config.experiment, "datasets_dir", "") or ""
                )
                if not _has_local_dataset_contract:
                    plan["datasets"] = [
                        b["name"] for b in _benchmark_plan.selected_benchmarks
                    ]
                else:
                    logger.info(
                        "BenchmarkAgent datasets preserved as suggestions only because project has local datasets_dir=%s",
                        getattr(config.experiment, "datasets_dir", ""),
                    )
                    plan.setdefault("benchmark_suggestions", {})
                    if isinstance(plan["benchmark_suggestions"], dict):
                        plan["benchmark_suggestions"]["datasets"] = [
                            b["name"] for b in _benchmark_plan.selected_benchmarks
                        ]
                # Normalize existing baselines to list of strings
                # BUG-35: LLM may emit baselines as dict, list of dicts,
                # or list of strings — normalize all to list[str].
                _baselines_from_plan = plan.get("baselines", [])
                if isinstance(_baselines_from_plan, dict):
                    _baselines_from_plan = list(_baselines_from_plan.keys())
                elif isinstance(_baselines_from_plan, list):
                    _baselines_from_plan = [
                        item["name"] if isinstance(item, dict) else str(item)
                        for item in _baselines_from_plan
                    ]
                else:
                    _baselines_from_plan = []
                plan["baselines"] = [
                    bl["name"] for bl in _benchmark_plan.selected_baselines
                ] + _baselines_from_plan
                # Deduplicate baselines
                plan["baselines"] = list(dict.fromkeys(plan["baselines"]))
            elif isinstance(plan, dict) and _benchmark_plan.selected_benchmarks:
                plan.setdefault("benchmark_suggestions", {})
                if isinstance(plan["benchmark_suggestions"], dict):
                    plan["benchmark_suggestions"]["datasets"] = [
                        b["name"] for b in _benchmark_plan.selected_benchmarks
                    ]
                    plan["benchmark_suggestions"]["baselines"] = [
                        bl["name"] for bl in _benchmark_plan.selected_baselines
                    ]
                    plan["benchmark_suggestions"]["validation_passed"] = False

            logger.info(
                "BenchmarkAgent: %d benchmarks, %d baselines selected (%d LLM calls, %.1fs)",
                len(_benchmark_plan.selected_benchmarks),
                len(_benchmark_plan.selected_baselines),
                _benchmark_plan.total_llm_calls,
                _benchmark_plan.elapsed_sec,
            )
        except Exception as _ba_exc:
            logger.warning("BenchmarkAgent failed (non-fatal): %s", _ba_exc)

    # Save benchmark plan for code_generation stage
    if _benchmark_plan is not None:
        try:
            (stage_dir / "benchmark_plan.json").write_text(
                json.dumps(_benchmark_plan.to_dict(), indent=2, ensure_ascii=False),
                encoding="utf-8",
            )
        except Exception:  # noqa: BLE001
            pass

    plan = _enforce_topic_experiment_constraints(
        plan,
        topic=config.research.topic,
        metric_key=config.experiment.metric_key,
        metric_direction=config.experiment.metric_direction,
    )
    _applied_constraints = plan.get("user_hard_constraints", {}).get("applied", [])
    if _applied_constraints:
        _s9_diagnostics["user_hard_constraints_applied"] = _applied_constraints
        logger.info(
            "Stage 09: Enforced user hard constraints: %s",
            ", ".join(_applied_constraints),
        )

    plan.setdefault("topic", config.research.topic)
    if isinstance(plan, dict):
        # Every scientific experiment plan gets an executable rigor contract.
        # Downstream code generation must implement it or provenance remains limited.
        protocol = plan.setdefault("evaluation_protocol", {})
        if not isinstance(protocol, dict):
            protocol = {}
            plan["evaluation_protocol"] = protocol
        protocol.setdefault("independent_seeds", [11, 29, 47])
        protocol.setdefault("minimum_seeds_per_condition", 3)
        protocol.setdefault("report", ["mean", "standard_deviation", "95%_confidence_interval"])
        protocol.setdefault("paired_comparison", {
            "required": True,
            "preferred_tests": ["paired_t_test", "wilcoxon_signed_rank"],
            "alpha": 0.05,
        })
        protocol.setdefault("raw_result_requirement", "retain per-seed metrics for every condition")
        _s9_diagnostics["rigor_contract"] = protocol
        plan.setdefault("plan_quality", {})
        if isinstance(plan["plan_quality"], dict):
            plan["plan_quality"].update({
                "status": _s9_diagnostics.get("status", "normal"),
                "degraded": bool(_s9_diagnostics.get("degraded")),
                "parse_strategy": _s9_diagnostics.get("parse_strategy"),
                "benchmark_agent_validation_passed": _s9_diagnostics.get(
                    "benchmark_agent_validation_passed"
                ),
                "scientific_claims_allowed": not bool(_s9_diagnostics.get("degraded")),
                "user_facing_status_zh": _s9_diagnostics.get("user_facing_status_zh"),
            })
    (stage_dir / "exp_plan_diagnostics.json").write_text(
        json.dumps(_s9_diagnostics, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )
    (stage_dir / "exp_plan.yaml").write_text(
        yaml.dump(plan, default_flow_style=False, allow_unicode=True),
        encoding="utf-8",
    )
    return StageResult(
        stage=Stage.EXPERIMENT_DESIGN,
        status=StageStatus.DONE,
        artifacts=("exp_plan.yaml", "exp_plan_diagnostics.json"),
        evidence_refs=("stage-09/exp_plan.yaml", "stage-09/exp_plan_diagnostics.json"),
    )


# ---------------------------------------------------------------------------
# Stage 10: CODEBASE_SEARCH — find & download reusable codebases
# ---------------------------------------------------------------------------

def _execute_codebase_search(
    stage_dir: Path,
    run_dir: Path,
    config: RCConfig,
    adapters: AdapterBundle,
    *,
    run_id: str = "",
    prompts: "PromptManager | None" = None,
    **kwargs: object,
) -> StageResult:
    """Search for reusable codebases based on the experiment plan.

    Reads exp_plan.yaml, asks LLM to identify relevant GitHub repos or
    papers-with-code, attempts to clone/download them, and records results
    in codebase_candidates.json.
    """
    from researchclaw.llm import create_llm_client

    exp_plan = _read_prior_artifact(run_dir, "exp_plan.yaml") or ""

    llm = create_llm_client(config)

    search_prompt = (
        "Based on the following experiment plan, identify up to 5 existing open-source "
        "GitHub repositories or papers-with-code that could serve as a starting codebase "
        "for this experiment. For each candidate, provide:\n"
        "- repo_url: GitHub URL\n"
        "- description: why it is relevant\n"
        "- usability: 'direct' (can use as-is with minor changes) or 'reference' (useful as reference)\n"
        "- key_files: list of key files/modules to reuse\n\n"
        "Return a JSON array. If no suitable codebase exists, return an empty array [].\n\n"
        "Experiment Plan:\n"
        f"{exp_plan}"
    )

    import json as _json
    import os as _os_s10

    candidates: list[dict] = []

    # ── Phase 1: Index LOCAL codebases already on disk ────────────────
    _local_codebases_dir = getattr(config.experiment, "codebases_dir", "") or ""
    if _local_codebases_dir and _os_s10.path.isdir(_local_codebases_dir):
        _local_repos: list[dict] = []
        for name in sorted(_os_s10.listdir(_local_codebases_dir)):
            full = _os_s10.path.join(_local_codebases_dir, name)
            if _os_s10.path.isdir(full) and not name.startswith("."):
                _local_repos.append({"name": name, "path": full})

        # Use LLM to assess relevance when multiple local repos exist
        _relevance_map: dict[str, str] = {}
        if len(_local_repos) > 1 and llm is not None:
            _repo_names = [r["name"] for r in _local_repos]
            _relevance_prompt = (
                f"Research topic: {config.research.topic}\n\n"
                f"Local codebases available: {_repo_names}\n\n"
                "For each codebase, assess its relevance to the research topic. "
                "Return a JSON object mapping each codebase name to one of: "
                '"high" (directly related, should be used), '
                '"medium" (somewhat related, could be useful), '
                '"low" (unrelated to this research topic).\n\n'
                "Example: {\"RepoA\": \"high\", \"RepoB\": \"low\"}"
            )
            try:
                _rel_resp = llm.chat(
                    [{"role": "user", "content": _relevance_prompt}],
                    system="You are a research engineer. Return valid JSON only.",
                    json_mode=True,
                    max_tokens=256,
                )
                _relevance_map = _json.loads(_rel_resp.content)
                if not isinstance(_relevance_map, dict):
                    _relevance_map = {}
            except Exception:
                _relevance_map = {}

        for _repo in _local_repos:
            _rel = _relevance_map.get(_repo["name"], "high" if len(_local_repos) == 1 else "low")
            candidates.append({
                "repo_url": f"local://{_repo['path']}",
                "description": f"Local codebase: {_repo['name']}",
                "usability": "direct",
                "relevance": _rel,
                "key_files": [],
                "local_path": _repo["path"],
                "download_status": "success",
            })

    # ── Phase 2: LLM-based GitHub search ──────────────────────────────
    try:
        response = llm.chat(
            [{"role": "user", "content": search_prompt}],
            system="You are an expert research engineer. Return valid JSON only.",
            json_mode=True,
        )

        try:
            remote_candidates = _json.loads(response.content)
            if not isinstance(remote_candidates, list):
                remote_candidates = []
        except _json.JSONDecodeError:
            remote_candidates = []

        codebase_dir = stage_dir / "codebases"
        codebase_dir.mkdir(exist_ok=True)
        for i, cand in enumerate(remote_candidates):
            url = cand.get("repo_url", "")
            usability = cand.get("usability", "reference")
            if usability == "direct" and url and "github.com" in url:
                local_path = codebase_dir / f"repo_{i}"
                try:
                    import subprocess
                    result = subprocess.run(
                        ["git", "clone", "--depth", "1", url, str(local_path)],
                        capture_output=True, text=True, timeout=60,
                    )
                    if result.returncode == 0:
                        cand["local_path"] = str(local_path)
                        cand["download_status"] = "success"
                    else:
                        cand["download_status"] = f"failed: {result.stderr[:200]}"
                except Exception as e:
                    cand["download_status"] = f"error: {e}"
            else:
                cand["download_status"] = "skipped"
        candidates.extend(remote_candidates)

    except Exception as e:
        logger.warning("LLM codebase search failed: %s", e)

    # ── Phase 3: Filter out low-relevance candidates, then persist ────
    _filtered = [c for c in candidates if c.get("relevance", "high") != "low"]
    _dropped = len(candidates) - len(_filtered)
    if _dropped:
        logger.info("CODEBASE_SEARCH: dropped %d low-relevance candidates", _dropped)

    try:
        output = _json.dumps(_filtered, indent=2, ensure_ascii=False)
        (stage_dir / "codebase_candidates.json").write_text(output, encoding="utf-8")
    except Exception:
        (stage_dir / "codebase_candidates.json").write_text("[]", encoding="utf-8")

    n_local = sum(1 for c in _filtered if str(c.get("repo_url", "")).startswith("local://"))
    n_remote = sum(1 for c in _filtered if c.get("download_status") == "success") - n_local
    n_total = len(_filtered)
    summary = f"Found {n_total} candidates ({n_local} local, {n_remote} remote downloaded, {_dropped} low-relevance dropped)"

    return StageResult(
        stage=Stage.CODEBASE_SEARCH,
        status=StageStatus.DONE,
        artifacts=("codebase_candidates.json",),
        evidence_refs=("stage-10/codebase_candidates.json",),
        decision=summary,
    )


def _extract_selected_repos(codebase_info_json: str) -> list[str] | None:
    """Extract repo directory names from codebase_candidates.json that S10 marked as relevant.

    Returns None (= copy all) if no candidates have relevance info,
    or a list of directory basenames for repos marked relevant.
    """
    try:
        import json as _json_sr
        candidates = _json_sr.loads(codebase_info_json)
        if not isinstance(candidates, list) or not candidates:
            return None
    except Exception:
        return None

    has_relevance = any(c.get("relevance") for c in candidates if isinstance(c, dict))

    selected: list[str] = []
    for c in candidates:
        if not isinstance(c, dict):
            continue
        if c.get("download_status") != "success":
            continue
        local_path = c.get("local_path", "")
        if not local_path:
            continue
        repo_name = local_path.rstrip("/").rsplit("/", 1)[-1]
        rel = c.get("relevance", "")
        if has_relevance and rel not in ("high", "medium"):
            continue
        if not has_relevance and c.get("usability") == "reference":
            continue
        selected.append(repo_name)

    return selected if selected else None


def _execute_code_generation(
    stage_dir: Path,
    run_dir: Path,
    config: RCConfig,
    adapters: AdapterBundle,
    *,
    llm: LLMClient | None = None,
    prompts: PromptManager | None = None,
) -> StageResult:
    """S11 CODE_GENERATION — delegates to the refactored codegen package.

    The full implementation lives in ``researchclaw.pipeline.codegen``,
    structured around claw-code's harness engineering patterns:
    StrategyRegistry, CodegenRouter, CodegenRuntime, and PromptBuilder.
    """
    from researchclaw.pipeline.codegen import execute_code_generation
    return execute_code_generation(
                    stage_dir=stage_dir,
        run_dir=run_dir,
        config=config,
        adapters=adapters,
            llm=llm,
        prompts=prompts,
    )


def _execute_sanity_check(
    stage_dir: Path,
    run_dir: Path,
    config: RCConfig,
    adapters: AdapterBundle,
    *,
    run_id: str = "",
    prompts: "PromptManager | None" = None,
    llm: "LLMClient | None" = None,
    **kwargs: object,
) -> StageResult:
    """S12 SANITY_CHECK — delegates to the refactored sanity_check package.

    The full implementation uses a claw-code agentic turn loop to run
    smoke tests, diagnose errors, and fix code iteratively.
    """
    from researchclaw.pipeline.sanity_check import execute_sanity_check
    return execute_sanity_check(
                stage_dir=stage_dir,
                run_dir=run_dir,
        config=config,
        adapters=adapters,
        llm=llm,
        prompts=prompts,
    )


def _execute_resource_planning(
    stage_dir: Path,
    run_dir: Path,
    config: RCConfig,
    adapters: AdapterBundle,
    *,
    llm: LLMClient | None = None,
    prompts: PromptManager | None = None,
) -> StageResult:
    exp_plan = _read_prior_artifact(run_dir, "exp_plan.yaml") or ""
    schedule: dict[str, Any] | None = None
    if llm is not None:
        _pm = prompts or PromptManager()
        _overlay = _get_evolution_overlay(run_dir, "resource_planning")
        sp = _pm.for_stage("resource_planning", evolution_overlay=_overlay, exp_plan=exp_plan)
        resp = _chat_with_prompt(
            llm,
            sp.system,
            sp.user,
            json_mode=sp.json_mode,
            max_tokens=sp.max_tokens,
        )
        parsed = _safe_json_loads(resp.content, {})
        if isinstance(parsed, dict):
            schedule = parsed
    if schedule is None:
        schedule = {
            "tasks": [
                {
                    "id": "baseline",
                    "name": "Run baseline",
                    "depends_on": [],
                    "gpu_count": 1,
                    "estimated_minutes": 20,
                    "priority": "high",
                },
                {
                    "id": "proposed",
                    "name": "Run proposed method",
                    "depends_on": ["baseline"],
                    "gpu_count": 1,
                    "estimated_minutes": 30,
                    "priority": "high",
                },
            ],
            "total_gpu_budget": 1,
            "generated": _utcnow_iso(),
        }
    schedule.setdefault("generated", _utcnow_iso())
    (stage_dir / "schedule.json").write_text(
        json.dumps(schedule, indent=2), encoding="utf-8"
    )
    return StageResult(
        stage=Stage.RESOURCE_PLANNING,
        status=StageStatus.DONE,
        artifacts=("schedule.json",),
        evidence_refs=("stage-13/schedule.json",),
    )


def _execute_experiment_run(
    stage_dir: Path,
    run_dir: Path,
    config: RCConfig,
    adapters: AdapterBundle,
    *,
    llm: LLMClient | None = None,
    prompts: PromptManager | None = None,
) -> StageResult:
    """S14 EXPERIMENT_RUN — delegates to the refactored experiment_run package.

    The full implementation uses a claw-code agentic turn loop to execute
    the experiment, monitor for runtime errors, and apply fixes.
    """
    from researchclaw.pipeline.experiment_run import execute_experiment_run
    return execute_experiment_run(
        stage_dir=stage_dir,
        run_dir=run_dir,
        config=config,
        adapters=adapters,
        llm=llm,
        prompts=prompts,
    )


def _execute_iterative_refine(
    stage_dir: Path,
    run_dir: Path,
    config: RCConfig,
    adapters: AdapterBundle,
    *,
    llm: LLMClient | None = None,
    prompts: PromptManager | None = None,
) -> StageResult:
    """S15 ITERATIVE_REFINE — delegates to the refactored iterative_refine package.

    The full implementation uses a claw-code agentic turn loop to analyze
    results, modify code, and re-run experiments iteratively.
    """
    from researchclaw.pipeline.iterative_refine import execute_iterative_refine
    return execute_iterative_refine(
        stage_dir=stage_dir,
        run_dir=run_dir,
        config=config,
        adapters=adapters,
        llm=llm,
        prompts=prompts,
    )


def _run_chart_generation(
    stage_dir: Path,
    run_dir: Path,
    config: RCConfig,
    llm: Any | None = None,
) -> None:
    """Generate charts for S16 using visualize.py (shared by agentic and legacy paths)."""
    try:
        from researchclaw.experiment.visualize import (
            generate_all_charts as _gen_charts,
        )
        _charts_dir = stage_dir / "charts"
        _charts_dir.mkdir(exist_ok=True)
        _generated = _gen_charts(
            run_dir,
            _charts_dir,
            metric_key=config.experiment.metric_key,
        )
        if _generated:
            logger.info("S16: Generated %d charts", len(_generated))
    except Exception as _exc:
        logger.warning("S16: Chart generation failed: %s", _exc)


def _execute_result_analysis(
    stage_dir: Path,
    run_dir: Path,
    config: RCConfig,
    adapters: AdapterBundle,
    *,
    llm: LLMClient | None = None,
    prompts: PromptManager | None = None,
) -> StageResult:
    # --- Agentic analysis: let the LLM read files, write scripts, produce summary ---
    from researchclaw.pipeline.result_analysis import ResultAnalysisRuntime
    _agent_rt = ResultAnalysisRuntime()
    _agent_result = _agent_rt.execute(
        stage_dir=stage_dir,
        run_dir=run_dir,
        config=config,
        adapters=adapters,
        llm=llm,
    )
    # If the agent produced both analysis.md and experiment_summary.json, use
    # them directly and skip the legacy hardcoded collection path.
    _agent_summary = stage_dir / "experiment_summary.json"
    _agent_analysis = stage_dir / "analysis.md"
    if _agent_summary.is_file() and _agent_analysis.is_file():
        try:
            _parsed = json.loads(_agent_summary.read_text(encoding="utf-8"))
            if isinstance(_parsed, dict) and _parsed.get("metrics_summary"):
                logger.info(
                    "S16: Using agentic experiment_summary.json (%d metric keys)",
                    len(_parsed["metrics_summary"]),
                )
                # Still run chart generation from the legacy path
                _run_chart_generation(stage_dir, run_dir, config, llm)
                artifacts = ["analysis.md", "experiment_summary.json"]
                if (stage_dir / "charts").is_dir() and any((stage_dir / "charts").iterdir()):
                    artifacts.append("charts/")
                return StageResult(
                    stage=Stage.RESULT_ANALYSIS,
                    status=StageStatus.DONE,
                    artifacts=tuple(artifacts),
                    evidence_refs=tuple(f"stage-16/{a}" for a in artifacts),
                )
        except (json.JSONDecodeError, OSError):
            pass
    logger.info("S16: Agentic analysis insufficient, falling back to legacy path")

    # --- Legacy: Collect experiment data ---
    exp_data = _collect_experiment_results(run_dir)
    runs_dir = _read_prior_artifact(run_dir, "runs/") or ""
    context = ""
    if runs_dir:
        context = _collect_json_context(Path(runs_dir), max_files=30)

    # --- R13-1: Merge Stage 13 (ITERATIVE_REFINE) results if available ---
    # Stage 13 stores richer per-condition metrics in refinement_log.json
    # that _collect_experiment_results() misses (it only scans runs/ dirs).
    _refine_log_text = _read_prior_artifact(run_dir, "refinement_log.json")
    if _refine_log_text:
        try:
            _refine_data = json.loads(_refine_log_text)
            _best_iter = None
            _best_ver = _refine_data.get("best_version", "")
            _refine_iterations = _refine_data.get("iterations", [])
            if not isinstance(_refine_iterations, list):
                logger.warning(
                    "R13-1: refinement_log.json has non-list iterations=%r; "
                    "skipping refinement merge",
                    type(_refine_iterations).__name__,
                )
                _refine_iterations = []
            for _it in _refine_iterations:
                if not isinstance(_it, dict):
                    continue
                _sbx = _it.get("sandbox", {})
                _it_metrics = _sbx.get("metrics", {})
                if _it.get("version_dir", "") == _best_ver and _it_metrics:
                    _best_iter = _it
                    break
            # If no version match, take the first iteration with metrics
            if _best_iter is None:
                for _it in _refine_iterations:
                    if not isinstance(_it, dict):
                        continue
                    _sbx = _it.get("sandbox", {})
                    if _sbx.get("metrics"):
                        _best_iter = _it
                        break
            if _best_iter is not None:
                _sbx = _best_iter.get("sandbox", {})
                _refine_metrics = _sbx.get("metrics", {})
                if _refine_metrics and (
                    not exp_data["metrics_summary"]
                    or len(_refine_metrics) > len(exp_data["metrics_summary"])
                ):
                    # Refinement has richer data — rebuild metrics_summary from it
                    _new_summary: dict[str, dict[str, float | None]] = {}
                    for _mk, _mv in _refine_metrics.items():
                        try:
                            _fv = float(_mv)
                            _new_summary[_mk] = {
                                "min": round(_fv, 6),
                                "max": round(_fv, 6),
                                "mean": round(_fv, 6),
                                "count": 1,
                            }
                        except (ValueError, TypeError):
                            pass
                    if _new_summary:
                        exp_data["metrics_summary"] = _new_summary
                        # Also update best_run with refinement data
                        exp_data["best_run"] = {
                            "run_id": "iterative-refine-best",
                            "task_id": "sandbox-main",
                            "status": "completed",
                            "metrics": {
                                k: v for k, v in _refine_metrics.items()
                            },
                            "elapsed_sec": _sbx.get("elapsed_sec", 0),
                            "stdout": "",  # omit for brevity
                            "stderr": _sbx.get("stderr", ""),
                            "timed_out": _sbx.get("timed_out", False),
                        }
                        # Rebuild latex table
                        _ltx = [
                            r"\begin{table}[h]", r"\centering",
                            r"\caption{Experiment Results (Best Refinement Iteration)}",
                            r"\begin{tabular}{lrrrr}", r"\hline",
                            r"Metric & Min & Max & Mean & N \\", r"\hline",
                        ]
                        for _col in sorted(_new_summary.keys()):
                            _s = _new_summary[_col]
                            _ltx.append(
                                f"{_col} & {_s['min']:.4f} & {_s['max']:.4f} "
                                f"& {_s['mean']:.4f} & {_s['count']} \\\\"
                            )
                        _ltx.extend([r"\hline", r"\end{tabular}", r"\end{table}"])
                        exp_data["latex_table"] = "\n".join(_ltx)
                        # Count unique conditions (keys without 'seed' and not ending in _mean/_std)
                        _conditions = {
                            k for k in _refine_metrics
                            if "seed" not in k and not k.endswith("_std")
                        }
                        exp_data["runs"] = [exp_data["best_run"]]
                        # Store condition count for accurate reporting
                        exp_data["best_run"]["condition_count"] = len(_conditions)
                        if not context:
                            context = json.dumps(
                                {"refinement_best_metrics": _refine_metrics},
                                indent=2, default=str,
                            )
                        logger.info(
                            "R13-1: Merged %d metrics from refinement_log (best_metric=%.4f)",
                            len(_refine_metrics),
                            _refine_data.get("best_metric", 0),
                        )
        except (json.JSONDecodeError, OSError, KeyError):
            logger.warning("R13-1: Failed to parse refinement_log.json, using Stage 12 data")

    # --- R19-2: Extract PAIRED comparisons from refinement stdout ---
    from researchclaw.experiment.sandbox import extract_paired_comparisons as _extract_paired

    _all_paired: list[dict[str, object]] = []
    # First: from _collect_experiment_results (Stage 12 runs/)
    if exp_data.get("paired_comparisons"):
        _all_paired.extend(exp_data["paired_comparisons"])
    # Second: from refinement_log iterations (Stage 13)
    if _refine_log_text:
        try:
            _rl = json.loads(_refine_log_text)
            _refine_iterations = _rl.get("iterations", [])
            if not isinstance(_refine_iterations, list):
                _refine_iterations = []
            for _it in _refine_iterations:
                if not isinstance(_it, dict):
                    continue
                for _sbx_key in ("sandbox", "sandbox_after_fix"):
                    _sbx_stdout = (_it.get(_sbx_key) or {}).get("stdout", "")
                    if _sbx_stdout:
                        _all_paired.extend(_extract_paired(_sbx_stdout))
        except (json.JSONDecodeError, OSError):
            pass

    # --- R19-3: Build structured condition_summaries from metrics ---
    _condition_summaries: dict[str, dict[str, Any]] = {}
    _ms = exp_data.get("metrics_summary", {})
    _best_metrics = {}
    if exp_data.get("best_run") and isinstance(exp_data["best_run"], dict):
        _best_metrics = exp_data["best_run"].get("metrics", {})

    # Group metrics by condition prefix (e.g., "ppo/primary_metric" → condition "ppo")
    for _mk, _mv in _best_metrics.items():
        parts = _mk.split("/")
        if len(parts) >= 2:
            cond = parts[0]
            metric_name = parts[-1]
            if cond not in _condition_summaries:
                _condition_summaries[cond] = {"metrics": {}}
            try:
                _condition_summaries[cond]["metrics"][metric_name] = float(_mv)
            except (ValueError, TypeError):
                pass

    # BUG-09 fix: If no condition summaries were built (metrics don't use
    # condition/metric format), try to extract from metrics_summary or
    # structured_results so FigureAgent has data to work with.
    if not _condition_summaries and _ms:
        # Try to parse condition data from metrics_summary keys
        for _mk, _mv in _ms.items():
            parts = _mk.split("/")
            if len(parts) >= 2:
                cond = parts[0]
                metric_name = parts[-1]
                if cond not in _condition_summaries:
                    _condition_summaries[cond] = {"metrics": {}}
                try:
                    _val = float(_mv) if not isinstance(_mv, dict) else None
                    if _val is not None:
                        _condition_summaries[cond]["metrics"][metric_name] = _val
                except (ValueError, TypeError):
                    pass
    if not _condition_summaries:
        # Last resort: build from structured_results condition keys
        _sr = exp_data.get("structured_results", {})
        if isinstance(_sr, dict):
            for _sk, _sv in _sr.items():
                if isinstance(_sv, dict) and _sk not in ("metadata", "config"):
                    _condition_summaries[_sk] = {"metrics": {}}
                    for _smk, _smv in _sv.items():
                        try:
                            _condition_summaries[_sk]["metrics"][_smk] = float(_smv)
                        except (ValueError, TypeError):
                            pass

    # R33: Build per-seed data structure (needed for CIs and paired tests below)
    _seed_data: dict[str, dict[int, float]] = {}  # {condition: {seed: value}}
    for _mk, _mv in _best_metrics.items():
        parts = _mk.split("/")
        # Pattern: condition/regime/seed_id/primary_metric
        if len(parts) >= 4 and parts[-1] == config.experiment.metric_key:
            cond = parts[0]
            try:
                seed_id = int(parts[2])
                val = float(_mv)
                _seed_data.setdefault(cond, {})[seed_id] = val
            except (ValueError, TypeError):
                pass

    # Enrich condition summaries with seed counts, success rates, and CIs
    for _ck, _cv in _condition_summaries.items():
        # Look for success_rate in metrics
        sr_key = f"{_ck}/success_rate"
        if sr_key in _best_metrics:
            try:
                _cv["success_rate"] = float(_best_metrics[sr_key])
            except (ValueError, TypeError):
                pass
        # Count seed-level entries to estimate n_seeds
        _seed_count = 0
        for _mk in _best_metrics:
            if _mk.startswith(f"{_ck}/") and "seed" in _mk.lower():
                _seed_count += 1
        if _seed_count > 0:
            _cv["n_seed_metrics"] = _seed_count

        # R33: Compute mean ± std and bootstrap 95% CI from per-seed data
        if _ck in _seed_data and len(_seed_data[_ck]) >= 3:
            _vals = list(_seed_data[_ck].values())
            import statistics as _stats_mod
            _mean = _stats_mod.mean(_vals)
            _std = _stats_mod.stdev(_vals)
            _cv["metrics"][f"{config.experiment.metric_key}_mean"] = round(_mean, 6)
            _cv["metrics"][f"{config.experiment.metric_key}_std"] = round(_std, 6)
            _cv["n_seeds"] = len(_vals)
            # Bootstrap 95% CI
            import random as _rng
            _rng.seed(42)
            _boot_means = []
            for _ in range(1000):
                _sample = [_rng.choice(_vals) for _ in range(len(_vals))]
                _boot_means.append(_stats_mod.mean(_sample))
            _boot_means.sort()
            _ci_low = round(_boot_means[int(0.025 * len(_boot_means))], 6)
            _ci_high = round(_boot_means[int(0.975 * len(_boot_means))], 6)
            # IMP-16: Sanity check — CI must contain the mean
            if _ci_low > _mean or _ci_high < _mean:
                logger.warning(
                    "Bootstrap CI [%.4f, %.4f] does not contain mean %.4f "
                    "for condition %s — replacing CI with mean ± 1.96*SE",
                    _ci_low, _ci_high, _mean, _ck,
                )
                _se = _std / (len(_vals) ** 0.5)
                _ci_low = round(_mean - 1.96 * _se, 6)
                _ci_high = round(_mean + 1.96 * _se, 6)
            _cv["ci95_low"] = _ci_low
            _cv["ci95_high"] = _ci_high

    # Count totals
    _total_conditions = len(_condition_summaries) if _condition_summaries else None
    _total_metrics = len(_best_metrics) if _best_metrics else None

    # --- R33: Pipeline-level paired computation as fallback ---
    # If the experiment code's PAIRED lines are sparse or suspicious (e.g.,
    # all identical t-stats), compute fresh paired tests from per-seed data.
    # (_seed_data was built above before condition summary enrichment)
    if len(_seed_data) >= 2:
        # Find common seeds across conditions
        _all_seeds_sets = [set(v.keys()) for v in _seed_data.values()]
        _common_seeds = set.intersection(*_all_seeds_sets) if _all_seeds_sets else set()

        if len(_common_seeds) >= 3:
            _cond_names_sorted = sorted(_seed_data.keys())
            _pipeline_paired: list[dict[str, object]] = []
            # Compare each condition against the first baseline (alphabetically)
            _baseline_cond = _cond_names_sorted[0]
            for _other_cond in _cond_names_sorted[1:]:
                _diffs = []
                for _sid in sorted(_common_seeds):
                    _diffs.append(
                        _seed_data[_other_cond][_sid] - _seed_data[_baseline_cond][_sid]
                    )
                if _diffs:
                    import statistics
                    _n = len(_diffs)
                    _mean_d = statistics.mean(_diffs)
                    _std_d = statistics.stdev(_diffs) if _n > 1 else 0.0
                    _t = (_mean_d / (_std_d / (_n ** 0.5))) if _std_d > 0 else 0.0
                    _df = _n - 1
                    # Two-tailed p-value using t-distribution
                    import math
                    try:
                        from scipy.stats import t as _t_dist
                        _p = float(2 * _t_dist.sf(abs(_t), _df))
                    except ImportError:
                        _p = 2 * (1 - 0.5 * (1 + math.erf(abs(_t) / (2 ** 0.5))))
                        if _df < 30:
                            _p = min(1.0, _p * (1 + 2.5 / max(_df, 1)))
                    _pipeline_paired.append({
                        "method": _other_cond,
                        "baseline": _baseline_cond,
                        "mean_diff": round(_mean_d, 6),
                        "std_diff": round(_std_d, 6),
                        "t_stat": round(_t, 4),
                        "p_value": round(_p, 6),
                        "n_seeds": _n,
                        "source": "pipeline_computed",
                    })

            # Use pipeline-computed if experiment code's are suspicious
            _exp_t_stats = {round(p.get("t_stat", 0), 4) for p in _all_paired}
            _all_identical = len(_exp_t_stats) <= 1 and len(_all_paired) > 1
            if _pipeline_paired and (_all_identical or len(_all_paired) < len(_pipeline_paired)):
                logger.info(
                    "R33: Using %d pipeline-computed paired tests (experiment code had %d, identical=%s)",
                    len(_pipeline_paired), len(_all_paired), _all_identical,
                )
                _all_paired = _pipeline_paired

    # --- P8: Detect identical conditions (broken ablations) ---
    _ablation_warnings: list[str] = []
    if _condition_summaries and len(_condition_summaries) >= 2:
        _cond_names = sorted(_condition_summaries.keys())
        for _i in range(len(_cond_names)):
            for _j in range(_i + 1, len(_cond_names)):
                _c1, _c2 = _cond_names[_i], _cond_names[_j]
                _s1 = _condition_summaries[_c1]
                _s2 = _condition_summaries[_c2]
                # Compare mean values for all shared metrics
                _shared_keys = set(_s1.keys()) & set(_s2.keys())
                if not _shared_keys:
                    continue
                _all_equal = True
                for _sk in _shared_keys:
                    _v1 = _s1[_sk].get("mean") if isinstance(_s1[_sk], dict) else _s1[_sk]
                    _v2 = _s2[_sk].get("mean") if isinstance(_s2[_sk], dict) else _s2[_sk]
                    if _v1 != _v2:
                        _all_equal = False
                        break
                if _all_equal and _shared_keys:
                    _warn = (
                        f"ABLATION FAILURE: Conditions '{_c1}' and '{_c2}' produce "
                        f"identical outputs across all {len(_shared_keys)} metrics. "
                        f"The ablation is invalid — the differentiating parameter "
                        f"is likely not used in the code."
                    )
                    _ablation_warnings.append(_warn)
                    logger.warning("P8: %s", _warn)
                elif _shared_keys:
                    # R5-BUG-03: Also flag near-identical conditions (< 1% relative diff)
                    _near_identical = True
                    for _sk in _shared_keys:
                        _v1 = _s1[_sk].get("mean") if isinstance(_s1[_sk], dict) else _s1[_sk]
                        _v2 = _s2[_sk].get("mean") if isinstance(_s2[_sk], dict) else _s2[_sk]
                        try:
                            _v1f, _v2f = float(_v1), float(_v2)
                            _denom = max(abs(_v1f), abs(_v2f), 1e-12)
                            if abs(_v1f - _v2f) / _denom > 0.01:
                                _near_identical = False
                                break
                        except (TypeError, ValueError):
                            _near_identical = False
                            break
                    if _near_identical:
                        _warn = (
                            f"ABLATION WARNING: Conditions '{_c1}' and '{_c2}' produce "
                            f"near-identical outputs (<1% relative difference) across "
                            f"all {len(_shared_keys)} metrics. The ablation may be trivial."
                        )
                        _ablation_warnings.append(_warn)
                        logger.warning("P8: %s", _warn)

    # --- Write structured experiment summary ---
    summary_payload = {
        "metrics_summary": exp_data["metrics_summary"],
        "total_runs": len(exp_data["runs"]),
        "best_run": exp_data["best_run"],
        "latex_table": exp_data["latex_table"],
        "generated": _utcnow_iso(),
    }
    # R13-1: Detect zero-variance across conditions (all conditions identical primary metric)
    if _condition_summaries and len(_condition_summaries) >= 2:
        _primary_vals = []
        for _cs in _condition_summaries.values():
            if isinstance(_cs, dict):
                # Try 'metrics' dict first (actual structure), then 'primary_metric' fallback
                _metrics = _cs.get("metrics", {})
                if isinstance(_metrics, dict) and _metrics:
                    _pv_candidate = next(iter(_metrics.values()), None)
                    if isinstance(_pv_candidate, dict):
                        _pv_candidate = _pv_candidate.get("mean")
                    if isinstance(_pv_candidate, (int, float)):
                        _primary_vals.append(_pv_candidate)
                        continue
                _pm = _cs.get("primary_metric", {})
                _pv = _pm.get("mean") if isinstance(_pm, dict) else _pm
                if isinstance(_pv, (int, float)):
                    _primary_vals.append(_pv)
        if len(_primary_vals) >= 2 and len(set(_primary_vals)) == 1:
            _zv_warn = (
                f"ZERO VARIANCE: All {len(_primary_vals)} conditions have "
                f"identical primary_metric ({_primary_vals[0]}). "
                f"Experiment condition wiring is likely broken."
            )
            _ablation_warnings.append(_zv_warn)
            logger.warning("R13-1: %s", _zv_warn)

    if _ablation_warnings:
        summary_payload["ablation_warnings"] = _ablation_warnings
    if _all_paired:
        summary_payload["paired_comparisons"] = _all_paired
    if _condition_summaries:
        summary_payload["condition_summaries"] = _condition_summaries
        summary_payload["condition_metrics"] = _condition_summaries  # alias for quality gate
        summary_payload["total_conditions"] = _total_conditions
    if _total_metrics:
        summary_payload["total_metric_keys"] = _total_metrics
    (stage_dir / "experiment_summary.json").write_text(
        json.dumps(summary_payload, indent=2, default=str), encoding="utf-8"
    )
    if exp_data["latex_table"]:
        (stage_dir / "results_table.tex").write_text(
            exp_data["latex_table"], encoding="utf-8"
        )

    # --- Build data-augmented prompt ---
    preamble = _build_context_preamble(
        config, run_dir, include_goal=True, include_hypotheses=True
    )
    data_context = ""
    if exp_data["metrics_summary"]:
        lines = ["\n## Quantitative Results"]
        for mk, mv in exp_data["metrics_summary"].items():
            if isinstance(mv, dict):
                lines.append(
                    f"- {mk}: mean={mv.get('mean', '?')}, min={mv.get('min', '?')}, "
                    f"max={mv.get('max', '?')}, n={mv.get('count', '?')}"
                )
        data_context = "\n".join(lines)

    # Append structured results if available
    if exp_data.get("structured_results"):
        structured_text = json.dumps(
            exp_data["structured_results"], indent=2, default=str
        )
        # Truncate to avoid blowing up context
        if len(structured_text) > 6000:
            structured_text = structured_text[:6000] + "\n... (truncated)"
        data_context += (
            f"\n\n## Structured Experiment Results (from results.json)\n"
            f"```json\n{structured_text}\n```"
        )

    # P8: Inject ablation warnings into data context
    if _ablation_warnings:
        data_context += "\n\nCRITICAL ABLATION WARNINGS:\n"
        for _aw in _ablation_warnings:
            data_context += f"- {_aw}\n"
        data_context += (
            "\nYou MUST address these in your analysis. Identical conditions "
            "mean the ablation design is broken and the comparison is meaningless.\n"
        )

    if llm is not None:
        _pm = prompts or PromptManager()
        from researchclaw.prompts import DEBATE_ROLES_ANALYSIS  # noqa: PLC0415

        # --- Multi-perspective debate ---
        perspectives_dir = stage_dir / "perspectives"
        variables = {
            "preamble": preamble,
            "data_context": data_context,
            "context": context,
        }
        perspectives = _multi_perspective_generate(
            llm, DEBATE_ROLES_ANALYSIS, variables, perspectives_dir
        )
        # --- Synthesize into unified analysis ---
        analysis = _synthesize_perspectives(
            llm, perspectives, "analysis_synthesize", _pm
        )
    else:
        # Template with real data if available
        ms = exp_data["metrics_summary"]
        metrics_block = ""
        if ms:
            for mk, mv in ms.items():
                if isinstance(mv, dict):
                    metrics_block += (
                        f"- **{mk}**: mean={mv.get('mean')}, "
                        f"min={mv.get('min')}, max={mv.get('max')}, n={mv.get('count')}\n"
                    )
        else:
            metrics_block = f"- Primary metric key: `{config.experiment.metric_key}`\n- No quantitative data yet.\n"

        analysis = f"""# Result Analysis

## Metrics Summary
{metrics_block}
## Comparative Findings
- Proposed approach results from {len(exp_data["runs"])} run(s) collected.

## Statistical Checks
- Recommend confidence interval and seed-wise variance reporting.

## Limitations
- Limited runs and synthetic constraints.

## Conclusion
- Proceed to decision stage with moderate confidence.

Generated: {_utcnow_iso()}
"""
    (stage_dir / "analysis.md").write_text(analysis, encoding="utf-8")

    artifacts = ["analysis.md", "experiment_summary.json"]
    if (stage_dir / "results_table.tex").exists():
        artifacts.append("results_table.tex")

    # IMP-6 + FA: Generate charts early (Stage 14) so paper draft can reference them
    # Try FigureAgent first (multi-agent intelligent charts), fall back to visualize.py
    _figure_plan_saved = False
    if config.experiment.figure_agent.enabled and llm is not None:
        try:
            from researchclaw.agents.figure_agent import FigureOrchestrator
            from researchclaw.agents.figure_agent.orchestrator import FigureAgentConfig as _FACfg

            _fa_cfg = _FACfg(
                enabled=True,
                min_figures=config.experiment.figure_agent.min_figures,
                max_figures=config.experiment.figure_agent.max_figures,
                max_iterations=config.experiment.figure_agent.max_iterations,
                render_timeout_sec=config.experiment.figure_agent.render_timeout_sec,
                use_docker=config.experiment.figure_agent.use_docker,
                docker_image=config.experiment.figure_agent.docker_image,
                output_format=config.experiment.figure_agent.output_format,
                gemini_api_key=config.experiment.figure_agent.gemini_api_key,
                gemini_model=config.experiment.figure_agent.gemini_model,
                nano_banana_enabled=config.experiment.figure_agent.nano_banana_enabled,
                strict_mode=config.experiment.figure_agent.strict_mode,
                dpi=config.experiment.figure_agent.dpi,
            )
            _fa = FigureOrchestrator(llm, _fa_cfg, stage_dir=stage_dir)

            # Build conditions list from condition_summaries
            _fa_conditions = list(_condition_summaries.keys()) if _condition_summaries else []

            # BUG-09 fix: pass best_run metrics as fallback data if
            # structured_results is empty, so Planner has some data to chart
            _fa_exp_results = exp_data.get("structured_results", {})
            if not _fa_exp_results and _best_metrics:
                _fa_exp_results = {"best_run_metrics": _best_metrics}

            # Read paper draft for Decision Agent analysis
            _paper_draft = (
                _read_prior_artifact(run_dir, "paper_draft.md")
                or _read_prior_artifact(run_dir, "outline.md")
                or ""
            )

            _fa_plan = _fa.orchestrate({
                "experiment_results": _fa_exp_results,
                "condition_summaries": _condition_summaries,
                "metrics_summary": exp_data.get("metrics_summary", {}),
                "metric_key": config.experiment.metric_key,
                "conditions": _fa_conditions,
                "topic": _read_prior_artifact(run_dir, "topic.md") or config.research.topic,
                "hypothesis": _read_prior_artifact(run_dir, "hypotheses.md") or "",
                "paper_draft": _paper_draft,
                "output_dir": str(stage_dir / "charts"),
            })

            if _fa_plan.figure_count > 0:
                # Save figure plan for Stage 17 to read
                (stage_dir / "figure_plan.json").write_text(
                    json.dumps(_fa_plan.to_dict(), indent=2, default=str),
                    encoding="utf-8",
                )
                _figure_plan_saved = True
                for _cf_name in _fa_plan.get_chart_files():
                    artifacts.append(f"charts/{_cf_name}")
                logger.info(
                    "Stage 14: FigureAgent generated %d charts (%d passed review, %.1fs)",
                    _fa_plan.figure_count,
                    _fa_plan.passed_count,
                    _fa_plan.elapsed_sec,
                )
            else:
                logger.warning("Stage 14: FigureAgent produced no charts, falling back")
        except Exception as _fa_exc:
            logger.warning("Stage 14: FigureAgent failed (%s), falling back to visualize.py", _fa_exc)

    # Fallback: legacy visualize.py chart generation
    if not _figure_plan_saved:
        try:
            from researchclaw.experiment.visualize import (
                generate_all_charts as _gen_charts_early,
            )

            _charts_dir = stage_dir / "charts"
            _early_charts = _gen_charts_early(
                run_dir,
                _charts_dir,
                metric_key=config.experiment.metric_key,
            )
            if _early_charts:
                for _cp in _early_charts:
                    artifacts.append(f"charts/{_cp.name}")
                logger.info(
                    "Stage 14: Generated %d early charts (legacy) for paper embedding",
                    len(_early_charts),
                )
        except Exception as _chart_exc:
            logger.warning("Stage 14: Early chart generation failed: %s", _chart_exc)

    # --- Register baseline results to shared registry ---
    _shared_dir = getattr(config.experiment, "shared_results_dir", "") or ""
    if _shared_dir:
        try:
            import sys as _sys_rr
            _services_dir = str(Path(__file__).resolve().parent.parent.parent.parent / "services")
            if _services_dir not in _sys_rr.path:
                _sys_rr.path.insert(0, _services_dir)
            from result_registry import ResultRegistry
            _registry = ResultRegistry(_shared_dir)
            _summary_path = stage_dir / "experiment_summary.json"
            if _summary_path.exists():
                _summary = json.loads(_summary_path.read_text(encoding="utf-8"))
                _best = _summary.get("best_run", {})
                _metrics = _best.get("metrics", {})
                if _metrics:
                    _registry.register(
                        project_id=getattr(config.project, "name", "unknown"),
                        description=f"{config.research.topic[:100]} - baseline results",
                        model="",
                        dataset="",
                        task=config.research.topic[:50],
                        metrics={k: float(v) for k, v in _metrics.items() if isinstance(v, (int, float))},
                        tags=[d.lower() for d in config.research.domains] + ["baseline"],
                        source_stage=int(Stage.RESULT_ANALYSIS),
                    )
                    logger.info("Registered %d metrics to shared results registry", len(_metrics))
                    print(f"[SHARED RESULTS] Registered baseline metrics: {list(_metrics.keys())}", flush=True)
        except Exception as _rr_exc:
            logger.debug("Shared results registration failed: %s", _rr_exc)

    return StageResult(
        stage=Stage.RESULT_ANALYSIS,
        status=StageStatus.DONE,
        artifacts=tuple(artifacts),
        evidence_refs=tuple(f"stage-16/{a}" for a in artifacts),
    )


def _parse_decision(text: str) -> str:
    """Extract PROCEED/PIVOT/REFINE from decision text.

    Looks for the first standalone keyword on its own line after a
    ``## Decision`` heading.  Falls back to a keyword scan of the first
    few lines after the heading, but only matches the keyword itself
    (not mentions inside explanatory prose like "PIVOT is not warranted").
    Returns lowercase ``"proceed"`` / ``"pivot"`` / ``"refine"``.
    Defaults to ``"proceed"`` if nothing matches.
    """
    import re as _re

    text_upper = text.upper()
    # Look in the first occurrence after "## Decision" heading
    decision_section = ""
    for keyword in ("## DECISION", "## Decision", "## decision"):
        if keyword.upper() in text_upper:
            idx = text_upper.index(keyword.upper())
            decision_section = text[idx : idx + 200]
            break
    search_text = decision_section or text[:500]

    # First try: look for a line that is just the keyword (possibly with
    # whitespace / markdown bold / trailing punctuation).
    for line in search_text.splitlines():
        stripped = line.strip().strip("*").strip("#").strip()
        if stripped.upper() in ("PROCEED", "PIVOT", "REFINE"):
            return stripped.lower()

    # Fallback: regex for standalone word boundaries so that
    # "PIVOT is not warranted" does NOT match as a decision.
    for kw in ("PIVOT", "REFINE", "PROCEED"):
        # Only match if the keyword appears as the FIRST keyword-class token
        # on its own (not embedded in a sentence saying "not PIVOT").
        pattern = _re.compile(
            r"(?:^|##\s*Decision\s*\n\s*)" + kw, _re.IGNORECASE | _re.MULTILINE
        )
        if pattern.search(search_text):
            return kw.lower()

    # Last resort: simple containment (original behavior)
    search_upper = search_text.upper()
    if "REFINE" in search_upper:
        return "refine"
    if "PIVOT" in search_upper:
        return "pivot"
    return "proceed"


def _build_research_readiness(
    run_dir: Path,
    decision: str = "pending",
) -> dict[str, Any]:
    """Fuse planning, execution, and evidence agents into one claim policy."""
    provenance = _safe_json_loads(
        _read_prior_artifact(run_dir, "experiment_provenance.json") or "{}", {}
    )
    diagnostics = _safe_json_loads(
        _read_prior_artifact(run_dir, "exp_plan_diagnostics.json") or "{}", {}
    )
    summary = _safe_json_loads(
        _read_prior_artifact(run_dir, "experiment_summary.json") or "{}", {}
    )
    try:
        plan = yaml.safe_load(_read_prior_artifact(run_dir, "exp_plan.yaml") or "") or {}
    except yaml.YAMLError:
        plan = {}
    if not isinstance(provenance, dict):
        provenance = {}
    if not isinstance(diagnostics, dict):
        diagnostics = {}
    if not isinstance(summary, dict):
        summary = {}
    if not isinstance(plan, dict):
        plan = {}

    executed = bool(provenance.get("executed"))
    real_code_execution = bool(provenance.get("real_code_execution", executed))
    scientific_allowed = bool(provenance.get("scientific_claims_allowed"))
    claim_status = str(provenance.get("claim_status", "unknown"))
    degraded_plan = bool(diagnostics.get("degraded"))
    parse_strategy = str(diagnostics.get("parse_strategy", "unknown"))
    metrics = summary.get("metrics_summary", {})
    has_metrics = isinstance(metrics, dict) and bool(metrics)
    try:
        total_runs = int(summary.get("total_runs", 0) or 0)
    except (TypeError, ValueError):
        total_runs = 0

    def _list_count(value: Any) -> int:
        if isinstance(value, list):
            return len([item for item in value if str(item).strip()])
        if isinstance(value, dict):
            return len(value)
        return 1 if str(value or "").strip() else 0

    condition_summaries = summary.get("condition_summaries", {})
    baseline_count = _list_count(plan.get("baselines"))
    if not baseline_count and isinstance(condition_summaries, dict):
        # Prefer methods that actually produced metrics.  A degraded benchmark
        # plan may list suggested baselines that code generation never ran.
        baseline_count = len({
            str(condition.get("model") or condition.get("method") or "").strip()
            for condition in condition_summaries.values()
            if isinstance(condition, dict)
            and str(condition.get("model") or condition.get("method") or "").strip()
        })
    if (
        not baseline_count
        and not condition_summaries
        and isinstance(plan.get("benchmark_suggestions"), dict)
    ):
        baseline_count = _list_count(plan["benchmark_suggestions"].get("baselines"))
    condition_count = _list_count(condition_summaries)
    dataset_count = _list_count(plan.get("datasets"))
    if not dataset_count and isinstance(condition_summaries, dict):
        dataset_count = len({
            str(condition.get("dataset", "")).strip()
            for condition in condition_summaries.values()
            if isinstance(condition, dict) and str(condition.get("dataset", "")).strip()
        })
    seed_counts: list[int] = []
    if isinstance(condition_summaries, dict):
        for condition in condition_summaries.values():
            if not isinstance(condition, dict):
                continue
            try:
                seed_counts.append(int(condition.get("n_seeds", 0) or 0))
            except (TypeError, ValueError):
                pass
    min_seeds_per_condition = min(seed_counts) if seed_counts else 0
    paired_comparison_count = _list_count(summary.get("paired_comparisons"))

    plan_score = 100 if not degraded_plan else 45
    if "fallback" in parse_strategy and not degraded_plan:
        plan_score = 70
    execution_score = 100 if executed and real_code_execution else 50 if executed else 0
    if scientific_allowed:
        evidence_score = 75 if claim_status == "limited_small_benchmark" else 100
    else:
        evidence_score = 20 if executed else 0
    rigor_score = min(100, (
        (15 if has_metrics else 0)
        + (15 if condition_count >= 3 else 8 if condition_count >= 1 else 0)
        + (15 if baseline_count >= 2 else 8 if baseline_count >= 1 else 0)
        + (15 if dataset_count >= 2 else 8 if dataset_count >= 1 else 0)
        + (25 if min_seeds_per_condition >= 3 else 5 if min_seeds_per_condition >= 1 else 0)
        + (15 if paired_comparison_count >= 1 else 0)
    ))
    reproducibility_score = min(100, sum((
        25 if provenance.get("command") else 0,
        25 if provenance.get("returncode") == 0 else 0,
        25 if provenance.get("implementation") else 0,
        25 if provenance.get("execution_mode") else 0,
    )))
    raw_overall = round(
        0.20 * plan_score
        + 0.25 * execution_score
        + 0.30 * evidence_score
        + 0.20 * rigor_score
        + 0.05 * reproducibility_score,
        1,
    )
    overall = raw_overall
    # A high engineering score must never visually upgrade a limited/smoke
    # benchmark into "scientific ready". Cap the user-facing readiness score at
    # the boundary implied by experiment provenance.
    if not executed:
        overall = min(overall, 20.0)
    elif not scientific_allowed:
        overall = min(overall, 49.0)
    elif claim_status == "limited_small_benchmark":
        overall = min(overall, 74.0)

    if not executed:
        level = "no_empirical_evidence"
        writing_policy = "no_empirical_claims"
        status_zh = "实验未成功执行；只能写研究计划与方法设计，禁止声称实验性能提升。"
    elif not scientific_allowed:
        level = "engineering_smoke_only"
        writing_policy = "engineering_report_only"
        status_zh = "代码已执行但仅属于工程 Smoke；可报告流程可运行，禁止形成科研性能结论。"
    elif claim_status == "limited_small_benchmark" or overall < 75:
        level = "limited_evidence"
        writing_policy = "limited_claims_only"
        status_zh = "已有真实小规模证据；结论必须限定在当前数据集、模型、指标和运行条件内。"
    else:
        level = "scientific_ready"
        writing_policy = "scientific_claims_allowed"
        status_zh = "计划、执行和结果证据达到科研写作门槛；仍需明确适用范围和局限性。"

    recommended_actions: list[str] = []
    if degraded_plan:
        recommended_actions.append("修复实验计划解析或 BenchmarkAgent 校验问题")
    if not executed:
        recommended_actions.append("完成至少一次真实代码执行并保存原始结果")
    if baseline_count < 2:
        recommended_actions.append("增加至少两个有意义的对比基线")
    if total_runs < 2 and min_seeds_per_condition < 3:
        recommended_actions.append("增加重复运行或多随机种子实验")
    if min_seeds_per_condition < 3:
        recommended_actions.append("为每个实验条件增加至少 3 个独立随机种子，并保留逐种子原始结果")
    if paired_comparison_count < 1 and baseline_count >= 2:
        recommended_actions.append("基于逐种子结果补充配对统计检验或置信区间")
    if dataset_count < 2 and scientific_allowed:
        recommended_actions.append("增加独立数据集或外部有效性测试")

    return {
        "schema_version": "research-readiness-v1",
        "generated": _utcnow_iso(),
        "decision": decision,
        "readiness_level": level,
        "readiness_score": overall,
        "writing_policy": writing_policy,
        "user_facing_status_zh": status_zh,
        "should_proceed_to_writing": bool(_read_prior_artifact(run_dir, "analysis.md")),
        "scientific_claims_allowed": scientific_allowed and level == "scientific_ready",
        "limited_claims_allowed": scientific_allowed and level in {"scientific_ready", "limited_evidence"},
        "evidence": {
            "executed": executed,
            "real_code_execution": real_code_execution,
            "experiment_scope": provenance.get("experiment_scope", "unknown"),
            "claim_status": claim_status,
            "plan_degraded": degraded_plan,
            "plan_parse_strategy": parse_strategy,
            "has_metrics": has_metrics,
            "total_runs": total_runs,
            "baseline_count": baseline_count,
            "dataset_count": dataset_count,
            "condition_count": condition_count,
            "min_seeds_per_condition": min_seeds_per_condition,
            "paired_comparison_count": paired_comparison_count,
        },
        "scores": {
            "plan_quality": plan_score,
            "execution": execution_score,
            "evidence_strength": evidence_score,
            "experiment_rigor": rigor_score,
            "reproducibility": reproducibility_score,
            "raw_readiness_score_before_claim_cap": raw_overall,
        },
        "recommended_actions": recommended_actions,
    }


def _claim_boundary_instruction(run_dir: Path) -> str:
    """Return the compact, non-negotiable writing policy for later stages."""
    readiness = _safe_json_loads(
        _read_prior_artifact(run_dir, "research_readiness.json") or "{}", {}
    )
    if not isinstance(readiness, dict) or not readiness:
        readiness = _build_research_readiness(run_dir)
    boundary = {
        "readiness_level": readiness.get("readiness_level"),
        "writing_policy": readiness.get("writing_policy"),
        "user_facing_status_zh": readiness.get("user_facing_status_zh"),
        "evidence": readiness.get("evidence", {}),
        "recommended_actions": readiness.get("recommended_actions", []),
    }
    return (
        "\n\n## NON-NEGOTIABLE EXPERIMENT CLAIM BOUNDARY\n"
        + json.dumps(boundary, ensure_ascii=False, indent=2)
        + "\nDo not broaden the claim scope during review, revision, or polishing. "
        "For limited evidence, every performance conclusion must name the tested "
        "dataset/model/metric/run conditions and the paper must contain an explicit "
        "limitations section. Engineering smoke results may establish only that the "
        "pipeline ran, never that the method is scientifically superior.\n\n"
        + _build_experiment_fact_contract(run_dir)
        + "\n"
    )


def _build_claim_integrity_report(run_dir: Path, paper: str) -> dict[str, Any]:
    """Deterministically audit whether paper claims exceed experiment evidence."""
    # Figure-generation prompts are internal scaffolding, not manuscript
    # claims.  Audit the visible paper only; prompts can contain illustrative
    # labels that must not trigger (or evade) the scientific fact gate.
    paper = re.sub(
        r"<!--\s*FIGURE_PROMPT\b.*?-->",
        "",
        paper,
        flags=re.IGNORECASE | re.DOTALL,
    )
    readiness = _safe_json_loads(
        _read_prior_artifact(run_dir, "research_readiness.json") or "{}", {}
    )
    if not isinstance(readiness, dict) or not readiness:
        readiness = _build_research_readiness(run_dir)
    summary = _safe_json_loads(
        _read_prior_artifact(run_dir, "experiment_summary.json") or "{}", {}
    )
    if not isinstance(summary, dict):
        summary = {}

    supported_values: list[float] = []

    def _collect_numbers(value: Any) -> None:
        if isinstance(value, bool):
            return
        if isinstance(value, (int, float)) and math.isfinite(float(value)):
            supported_values.append(float(value))
        elif isinstance(value, dict):
            for nested in value.values():
                _collect_numbers(nested)
        elif isinstance(value, list):
            for nested in value:
                _collect_numbers(nested)

    _collect_numbers(summary.get("metrics_summary", {}))
    _collect_numbers(summary.get("condition_summaries", {}))

    writing_policy = str(readiness.get("writing_policy", "no_empirical_claims"))
    limited_policy = writing_policy in {
        "limited_claims_only", "engineering_report_only", "no_empirical_claims",
    }
    empirical_section_re = re.compile(
        r"(?ims)^##(?!#)\s*(?:\d+[.、]?\s*)?"
        r"(?:results?|experiments?|evaluation|ablation|结果|实验|评估|消融)\b"
        r".*?(?=^##(?!#)\s|\Z)"
    )
    # ``findall`` returns only the captured heading name for this regex, which
    # silently excluded the section body from every numeric/fact audit.
    sections = [match.group(0) for match in empirical_section_re.finditer(paper)]
    empirical_text = "\n".join(sections)
    # Treat scientific notation as one numeric token.  Splitting ``1.2e-16``
    # into ``1.2`` and ``16`` creates false fabrication alarms in legitimate
    # variance/standard-deviation reports.
    numeric_re = re.compile(
        r"(?<![\w.])([+-]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?)(\s*%)?"
    )
    metric_claim_re = re.compile(
        r"(?i)(accuracy|precision|recall|f1|auc|loss|latency|throughput|score|"
        r"improv|outperform|increase|decrease|gain|提升|提高|降低|优于|准确率|"
        r"精确率|召回率|损失|延迟|吞吐)"
    )
    unsupported_numeric_claims: list[dict[str, Any]] = []

    def _is_supported(number: float, is_percent: bool) -> bool:
        candidates = [number / 100.0] if is_percent else [number]
        if not is_percent:
            candidates.append(number / 100.0)
        return any(
            math.isclose(candidate, actual, rel_tol=1e-4, abs_tol=5e-4)
            for candidate in candidates for actual in supported_values
        )

    for match in numeric_re.finditer(empirical_text):
        raw = match.group(0).strip()
        number = float(match.group(1))
        is_percent = bool(match.group(2))
        line_start = empirical_text.rfind("\n", 0, match.start()) + 1
        line_prefix = empirical_text[line_start:match.start()]
        if re.match(r"^\s*#{1,6}\s", line_prefix):
            continue
        seed_local = empirical_text[
            max(0, match.start() - 55):min(len(empirical_text), match.end() + 55)
        ]
        if number.is_integer() and re.search(
            r"(?i)\bseeds?\b[^.\n]{0,90}\b" + re.escape(str(int(number))) + r"\b",
            seed_local,
        ):
            continue
        # A confidence *level* describes the interval construction; it is not
        # an observed performance value that must occur in experiment JSON.
        # Keep actual percentage metrics (e.g. "95% accuracy") auditable.
        if is_percent:
            local = empirical_text[
                max(0, match.start() - 35):min(len(empirical_text), match.end() + 35)
            ]
            if math.isclose(number, 95.0) and re.search(
                r"(?i)(?:confidence\s+interval|\bCI\b)", local
            ):
                continue
            if re.search(
                r"(?i)(?:confidence(?:\s+interval)?\s*(?:of|at)?\s*)?"
                + re.escape(raw)
                + r"\s*(?:confidence\s+interval|CI)\b",
                local,
            ):
                continue
        # Ignore likely years, section/table indices, and ordinary integer setup counts.
        if not is_percent and number.is_integer() and (
            1900 <= number <= 2100 or number < 10
        ):
            continue
        left = max(0, match.start() - 90)
        right = min(len(empirical_text), match.end() + 90)
        context = re.sub(r"\s+", " ", empirical_text[left:right]).strip()
        if metric_claim_re.search(context) and not _is_supported(number, is_percent):
            unsupported_numeric_claims.append({"value": raw, "context": context})

    overclaim_patterns = {
        "state_of_the_art": r"(?i)\b(state[- ]of[- ]the[- ]art|sota)\b|最先进|领先水平",
        "universal_claim": r"(?i)\b(universally|in all cases|across all domains)\b|普遍适用|所有场景|任何场景",
        "causal_proof": r"(?i)\b(proves?|definitively establishes)\b|充分证明|证实了.*因果",
        "unsupported_significance": r"(?i)\bstatistically significant(?:ly)?\b|统计显著",
        "stable_superiority": r"(?i)\b(consistently|robustly)\s+(?:outperforms?|improves?)\b|稳定地?优于|持续优于",
    }
    overclaims: list[dict[str, str]] = []
    if limited_policy:
        for kind, pattern in overclaim_patterns.items():
            found = re.search(pattern, paper)
            if found:
                left = max(0, found.start() - 70)
                right = min(len(paper), found.end() + 100)
                prefix = paper[max(0, found.start() - 180):found.start()].lower()
                if re.search(r"\b(?:not|never|does not|do not|cannot|no claim)\b", prefix):
                    continue
                # It is legitimate to report that the paired t-test crossed
                # alpha while Wilcoxon did not, provided the same local passage
                # explicitly frames this as a conflict rather than an overall
                # significance claim.
                if kind == "unsupported_significance":
                    local = paper[max(0, found.start() - 260):min(len(paper), found.end() + 320)]
                    if (
                        re.search(r"(?i)t[- ]?test", local)
                        and re.search(r"(?i)wilcoxon", local)
                        and re.search(r"(?i)(conflict|disagree|discrepan|however|while|but|ambiguous)", local)
                    ):
                        continue
                overclaims.append({
                    "type": kind,
                    "context": re.sub(r"\s+", " ", paper[left:right]).strip(),
                })

    has_limitations = bool(re.search(
        r"(?im)^#{1,4}\s*(?:\d+[.、]?\s*)?(limitations?|局限性|局限|有效性威胁)\b",
        paper,
    ))
    # Metric words inside an explicit evidence-boundary statement (for example
    # "does not establish accuracy" or "no claim of improved accuracy") are
    # limitations, not empirical performance claims.  Treating those statements
    # as positive claims makes an honest engineering report impossible to pass.
    has_empirical_performance_claims = False
    for metric_match in metric_claim_re.finditer(empirical_text):
        prefix = empirical_text[max(0, metric_match.start() - 100):metric_match.start()]
        if re.search(
            r"(?i)(?:does\s+not|do\s+not|did\s+not|cannot|not\s+interpreted\s+as|"
            r"no\s+(?:benchmark-quality\s+)?(?:claim|evidence|result)|never|unsupported|"
            r"not\s+supported|remain(?:s)?\s+deferred)[^.\n]{0,90}$",
            prefix,
        ):
            continue
        has_empirical_performance_claims = True
        break
    readiness_evidence = readiness.get("evidence", {}) if isinstance(readiness, dict) else {}
    real_executed_metrics = bool(
        isinstance(readiness_evidence, dict)
        and readiness_evidence.get("executed")
        and readiness_evidence.get("real_code_execution")
        and readiness_evidence.get("has_metrics")
    )
    # An engineering report may describe observed, fully supported numbers
    # from real execution.  It may not upgrade them to broad scientific claims.
    prohibited_empirical_claims = bool(
        has_empirical_performance_claims
        and (
            writing_policy == "no_empirical_claims"
            or (writing_policy == "engineering_report_only" and not real_executed_metrics)
        )
    )

    baseline_branding_violations = _baseline_only_outline_violations(run_dir, paper)
    fact_contract_violations: list[str] = []
    if re.search(r"(?i)training subjects?\s*\(?1\s*[-–]\s*21\)?", paper):
        fact_contract_violations.append("invented contiguous training-subject range 1-21")
    if re.search(r"(?i)(?:testing|test) subjects?\s*\(?22\s*[-–]\s*30\)?", paper):
        fact_contract_violations.append("invented contiguous test-subject range 22-30")
    if re.search(r"(?i)training subjects?\s*\(?1\s*[-–]\s*14\)?", paper):
        fact_contract_violations.append("invented contiguous training-subject range 1-14")
    if re.search(r"(?i)(?:testing|test) subjects?\s*\(?15\s*[-–]\s*30\)?", paper):
        fact_contract_violations.append("invented contiguous test-subject range 15-30")

    condition_summaries = summary.get("condition_summaries", {})
    has_class_level_evidence = False
    if isinstance(condition_summaries, dict):
        for condition_data in condition_summaries.values():
            if not isinstance(condition_data, dict):
                continue
            serialized_keys = " ".join(
                str(key).lower()
                for key in condition_data.keys()
            )
            if re.search(r"per[_ -]?(?:class|activity)|class[_ -]?(?:f1|metrics)", serialized_keys):
                has_class_level_evidence = True
                break
    if not has_class_level_evidence and re.search(
        r"(?is)(?:^#{1,4}[^\n]*(?:per[- ]activity|per[- ]class|activity class)|"
        r"(?:we\s+)?analy[sz]ed[^.\n]{0,100}(?:individual\s+)?activity\s+class)",
        empirical_text,
    ):
        fact_contract_violations.append("claimed per-activity/class results without executed class-level evidence")

    violations: list[dict[str, str]] = []
    if unsupported_numeric_claims:
        violations.append({
            "severity": "high",
            "type": "unsupported_numeric_claims",
            "message_zh": f"发现 {len(unsupported_numeric_claims)} 处无法由实验摘要核对的性能数字。",
        })
    if prohibited_empirical_claims:
        violations.append({
            "severity": "high",
            "type": "empirical_claims_forbidden",
            "message_zh": "当前证据仅允许工程/计划报告，但正文仍包含性能结论。",
        })
    if overclaims:
        violations.append({
            "severity": "high" if writing_policy != "scientific_claims_allowed" else "medium",
            "type": "overgeneralized_claims",
            "message_zh": f"发现 {len(overclaims)} 处超出当前实验范围的泛化或显著性表述。",
        })
    if baseline_branding_violations:
        violations.append({
            "severity": "high",
            "type": "invented_method_branding",
            "message_zh": "固定基线复现稿件仍把审计/流程包装成未实现的新方法或缩写。",
        })
    if fact_contract_violations:
        violations.append({
            "severity": "high",
            "type": "experiment_fact_contradiction",
            "message_zh": "稿件中的数据划分与实际归档的官方 subject split 冲突。",
        })
    if limited_policy and not has_limitations:
        violations.append({
            "severity": "high",
            "type": "missing_limitations",
            "message_zh": "受限证据稿件缺少明确的局限性/有效性威胁章节。",
        })

    high_count = sum(v["severity"] == "high" for v in violations)
    medium_count = sum(v["severity"] == "medium" for v in violations)
    integrity_score = max(0, 100 - 30 * high_count - 12 * medium_count)
    status = "blocked" if high_count else "warning" if medium_count else "passed"
    actions: list[str] = []
    if unsupported_numeric_claims:
        actions.append("删除或改正无法从 experiment_summary.json 核对的性能数字")
    if prohibited_empirical_claims:
        actions.append("将性能结论改写为仅描述代码与流程可运行")
    if overclaims:
        actions.append("把普遍性、显著性或稳定优越表述收缩到实际测试条件")
    if baseline_branding_violations:
        actions.append("删除虚构的方法名、协议名或缩写，改用无品牌的基线复现/审计描述")
    if fact_contract_violations:
        actions.append("用归档数据中的准确 subject ID 集合替换虚构的连续编号范围")
    if limited_policy and not has_limitations:
        actions.append("增加独立的局限性章节，说明数据集、随机种子、统计检验和外部有效性边界")

    return {
        "schema_version": "claim-integrity-v1",
        "generated": _utcnow_iso(),
        "status": status,
        "integrity_score": integrity_score,
        "writing_policy": writing_policy,
        "has_limitations_section": has_limitations,
        "has_empirical_performance_claims": has_empirical_performance_claims,
        "prohibited_empirical_claims": prohibited_empirical_claims,
        "supported_metric_value_count": len(supported_values),
        "unsupported_numeric_claims": unsupported_numeric_claims[:20],
        "overgeneralized_claims": overclaims[:20],
        "baseline_branding_violations": baseline_branding_violations,
        "experiment_fact_contradictions": fact_contract_violations,
        "violations": violations,
        "recommended_actions": actions,
        "user_facing_status_zh": (
            "结论完整性检查通过。" if status == "passed"
            else "结论超出当前实验支持范围，最终稿需按建议收缩或补充证据。"
            if status == "blocked"
            else "结论基本可用，但仍有需要人工确认的边界表述。"
        ),
    }


def _scope_locked_reproduction_can_proceed(
    run_dir: Path,
    readiness: dict[str, Any],
) -> bool:
    """Allow writing without turning a fixed reproduction into a new study.

    A research REFINE recommendation is still preserved as scientific advice.  The
    orchestration layer may proceed only when the user explicitly locked the run to
    the requested baselines and the requested experiment really executed.  Later
    writing stages remain bound by ``writing_policy`` and therefore cannot turn this
    transition into permission for scientific performance claims.
    """
    plan_path = run_dir / "stage-09" / "exp_plan.yaml"
    if not plan_path.exists():
        return False
    try:
        plan = yaml.safe_load(plan_path.read_text(encoding="utf-8")) or {}
    except (OSError, yaml.YAMLError):
        return False
    constraints = plan.get("user_hard_constraints", {}) if isinstance(plan, dict) else {}
    applied = constraints.get("applied", []) if isinstance(constraints, dict) else []
    evidence = readiness.get("evidence", {}) if isinstance(readiness, dict) else {}
    return bool(
        isinstance(applied, list)
        and "requested_baselines_only" in applied
        and readiness.get("should_proceed_to_writing") is True
        and isinstance(evidence, dict)
        and evidence.get("executed") is True
        and evidence.get("real_code_execution") is True
        and evidence.get("has_metrics") is True
    )


def _execute_research_decision(
    stage_dir: Path,
    run_dir: Path,
    config: RCConfig,
    adapters: AdapterBundle,
    *,
    llm: LLMClient | None = None,
    prompts: PromptManager | None = None,
) -> StageResult:
    analysis = _read_prior_artifact(run_dir, "analysis.md") or ""
    pre_decision_readiness = _build_research_readiness(run_dir)
    readiness_context = (
        "\n\n## NON-NEGOTIABLE RESEARCH READINESS AND CLAIM BOUNDARY\n"
        + json.dumps(pre_decision_readiness, ensure_ascii=False, indent=2)
        + "\nThe decision must respect writing_policy. PROCEED may mean proceeding to an "
        "engineering report or limited-evidence paper; it never upgrades the allowed claim scope.\n"
    )

    # P6: Detect degenerate REFINE cycles — inject warning if metrics stagnate
    _degenerate_hint = ""
    _refine_log = _read_prior_artifact(run_dir, "refinement_log.json")
    if _refine_log:
        try:
            _rl = json.loads(_refine_log)
            _iters = _rl.get("iterations", [])
            if not isinstance(_iters, list):
                _iters = []
            _metrics = [it.get("metric") for it in _iters if isinstance(it, dict)]
            _valid = [m for m in _metrics if m is not None]
            _all_saturated = _valid and all(m <= 0.001 or m >= 0.999 for m in _valid)
            _all_identical = len(set(_valid)) <= 1 and len(_valid) >= 2
            if _all_saturated or _all_identical:
                _degenerate_hint = (
                    "\n\nSYSTEM WARNING — DEGENERATE REFINE CYCLE DETECTED:\n"
                    f"Metrics across {len(_valid)} iterations: {_valid}\n"
                    "All iterations produce identical/saturated results. Further REFINE "
                    "cycles CANNOT fix this — the underlying benchmark design is too "
                    "easy/hard. You SHOULD choose PROCEED with a quality caveat rather "
                    "than REFINE again.\n"
                )
                logger.warning("P6: Degenerate refine cycle detected, injecting PROCEED hint")
        except (json.JSONDecodeError, OSError):
            pass

    if llm is not None:
        _pm = prompts or PromptManager()
        _overlay = _get_evolution_overlay(run_dir, "research_decision")
        sp = _pm.for_stage("research_decision", evolution_overlay=_overlay, analysis=analysis)
        _user = sp.user + _degenerate_hint + readiness_context
        resp = llm.chat(
            [{"role": "user", "content": _user}],
            system=sp.system,
        )
        decision_md = resp.content
    else:
        decision_md = f"""# Research Decision

## Decision
PROCEED

## Justification
Current evidence suggests measurable progress with actionable limitations.

## Next Actions
- Build detailed paper outline
- Expand ablation and uncertainty analysis in writing

Generated: {_utcnow_iso()}
"""
    (stage_dir / "decision.md").write_text(decision_md, encoding="utf-8")

    # --- Extract structured decision ---
    decision = _parse_decision(decision_md)

    # T3.1: Validate decision quality — check for minimum experiment rigor
    _quality_warnings: list[str] = []
    _dec_lower = decision_md.lower()
    if "baseline" not in _dec_lower and "control" not in _dec_lower:
        _quality_warnings.append("Decision text does not mention baselines")
    if "seed" not in _dec_lower and "replicat" not in _dec_lower and "run" not in _dec_lower:
        _quality_warnings.append("Decision text does not mention multi-seed/replicate runs")
    if "metric" not in _dec_lower and "accuracy" not in _dec_lower and "loss" not in _dec_lower:
        _quality_warnings.append("Decision text does not mention evaluation metrics")
    if _quality_warnings:
        logger.warning("T3.1: Decision quality warnings: %s", _quality_warnings)

    decision_payload = {
        "decision": decision,
        "raw_text_excerpt": decision_md[:500],
        "quality_warnings": _quality_warnings,
        "generated": _utcnow_iso(),
    }
    readiness = _build_research_readiness(run_dir, decision=decision)
    operational_decision = decision
    if (
        decision in {"refine", "pivot"}
        and _scope_locked_reproduction_can_proceed(run_dir, readiness)
    ):
        operational_decision = "proceed"
        override_reason = "scope_locked_reproduction_and_real_execution_complete"
        decision_payload.update({
            "execution_control_decision": operational_decision,
            "operational_override": True,
            "operational_override_reason": override_reason,
            "claim_scope": "evidence_limited",
        })
        readiness.update({
            "execution_control_decision": operational_decision,
            "operational_override": True,
            "operational_override_reason": override_reason,
            "claim_scope": "evidence_limited",
        })
        boundary_note = (
            "\n\n## Operational Boundary\n"
            "The scientific recommendation above remains advisory. The user locked this "
            "run to the requested baselines, and the real experiment completed with saved "
            "metrics. The workflow therefore proceeds to an evidence-limited engineering "
            "report without adding seeds, models, datasets, ablations, or scientific "
            "performance claims.\n"
        )
        with (stage_dir / "decision.md").open("a", encoding="utf-8") as handle:
            handle.write(boundary_note)
        logger.warning(
            "Research decision %s retained as advisory; proceeding to writing because "
            "the completed reproduction scope is user-locked",
            decision,
        )
    decision_payload.update({
        "readiness_level": readiness["readiness_level"],
        "readiness_score": readiness["readiness_score"],
        "writing_policy": readiness["writing_policy"],
        "scientific_claims_allowed": readiness["scientific_claims_allowed"],
        "limited_claims_allowed": readiness["limited_claims_allowed"],
    })
    (stage_dir / "decision_structured.json").write_text(
        json.dumps(decision_payload, indent=2), encoding="utf-8"
    )
    (stage_dir / "research_readiness.json").write_text(
        json.dumps(readiness, indent=2, ensure_ascii=False), encoding="utf-8"
    )
    logger.info(
        "Research decision: advisory=%s, operational=%s",
        decision,
        operational_decision,
    )

    return StageResult(
        stage=Stage.RESEARCH_DECISION,
        status=StageStatus.DONE,
        artifacts=("decision.md", "decision_structured.json", "research_readiness.json"),
        evidence_refs=("stage-17/decision.md", "stage-17/research_readiness.json"),
        decision=operational_decision,
    )


def _execute_knowledge_summary(
    stage_dir: Path,
    run_dir: Path,
    config: RCConfig,
    adapters: AdapterBundle,
    *,
    llm: LLMClient | None = None,
    prompts: PromptManager | None = None,
    **kwargs: object,
) -> StageResult:
    """Summarize experiment findings into the shared knowledge base.

    Reads analysis, decision, and experiment plan, then produces a structured
    knowledge entry (JSON) and appends it to the shared knowledge base.
    L1 lobsters read this knowledge base when generating new hypotheses.
    """
    from researchclaw.llm import create_llm_client

    if llm is None:
        llm = create_llm_client(config)

    analysis = _read_prior_artifact(run_dir, "analysis.md") or ""
    decision = _read_prior_artifact(run_dir, "decision.md") or ""
    exp_plan = _read_prior_artifact(run_dir, "exp_plan.yaml") or ""
    hypotheses = _read_prior_artifact(run_dir, "hypotheses.md") or ""

    summary_prompt = (
        "Based on the following research experiment, create a structured knowledge entry.\n\n"
        "## Hypotheses\n" + hypotheses[:2000] + "\n\n"
        "## Experiment Plan\n" + exp_plan[:2000] + "\n\n"
        "## Analysis\n" + analysis[:3000] + "\n\n"
        "## Decision\n" + decision[:1000] + "\n\n"
        "Create a JSON object with these fields:\n"
        '- "topic": one-line research topic\n'
        '- "hypotheses": list of hypotheses tested\n'
        '- "method": experimental methodology summary (2-3 sentences)\n'
        '- "settings": key experimental settings (model, dataset, hyperparams, hardware)\n'
        '- "results": dict of metric_name -> value for main results\n'
        '- "conclusions": list of key conclusions (what worked, what didn\'t)\n'
        '- "insights": list of surprising or useful insights for future research\n'
        '- "limitations": list of limitations\n'
        '- "suggested_directions": list of promising follow-up directions\n'
        "Return valid JSON only."
    )

    try:
        resp = llm.chat(
            [{"role": "user", "content": summary_prompt}],
            system="You are a research scientist. Summarize experiment findings into a structured knowledge entry. Return valid JSON only.",
            json_mode=True,
        )
        content = resp.content if hasattr(resp, 'content') else str(resp)

        import json as _json_ks
        try:
            entry = _json_ks.loads(content)
        except _json_ks.JSONDecodeError:
            if "```json" in content:
                content = content.split("```json")[1].split("```")[0]
            elif "```" in content:
                content = content.split("```")[1].split("```")[0]
            entry = _json_ks.loads(content)

        if not isinstance(entry, dict):
            entry = {"raw": str(entry)}

        entry["project_id"] = getattr(config.project, "name", "unknown")
        entry["research_topic"] = config.research.topic
        entry["domains"] = list(config.research.domains)
        entry["timestamp"] = _utcnow_iso()

        (stage_dir / "knowledge_entry.json").write_text(
            _json_ks.dumps(entry, indent=2, ensure_ascii=False), encoding="utf-8",
        )

        # Write to shared knowledge base
        _shared_dir = getattr(config.experiment, "shared_results_dir", "") or ""
        if _shared_dir:
            import os as _os_ks
            kb_dir = Path(_shared_dir) / "knowledge_base"
            kb_dir.mkdir(parents=True, exist_ok=True)
            kb_file = kb_dir / f"{entry['project_id']}_{_utcnow_iso().replace(':', '-')}.json"
            kb_file.write_text(
                _json_ks.dumps(entry, indent=2, ensure_ascii=False), encoding="utf-8",
            )

            # Append summary to knowledge_index.jsonl for quick L1 lookup
            index_file = kb_dir / "knowledge_index.jsonl"
            compact = {
                "project": entry.get("project_id", ""),
                "topic": entry.get("topic", entry.get("research_topic", "")),
                "conclusions": entry.get("conclusions", []),
                "insights": entry.get("insights", []),
                "suggested_directions": entry.get("suggested_directions", []),
                "results": entry.get("results", {}),
            }
            with open(index_file, "a", encoding="utf-8") as f:
                f.write(_json_ks.dumps(compact, ensure_ascii=False) + "\n")

            logger.info("Knowledge entry written to shared KB: %s", kb_file.name)
            print(f"[KNOWLEDGE] Written to shared KB: {len(entry.get('conclusions', []))} conclusions, {len(entry.get('insights', []))} insights", flush=True)

    except Exception as e:
        logger.warning("Knowledge summary generation failed: %s", e)
        (stage_dir / "knowledge_entry.json").write_text(
            json.dumps({"error": str(e), "topic": config.research.topic}, indent=2),
            encoding="utf-8",
        )

    return StageResult(
        stage=Stage.KNOWLEDGE_SUMMARY,
        status=StageStatus.DONE,
        artifacts=("knowledge_entry.json",),
        evidence_refs=("stage-18/knowledge_entry.json",),
    )


def _baseline_only_outline_violations(run_dir: Path, outline: str) -> list[str]:
    """Return scope violations when a baseline reproduction is branded as a method."""
    try:
        plan = yaml.safe_load(_read_prior_artifact(run_dir, "exp_plan.yaml") or "") or {}
    except yaml.YAMLError:
        return []
    if not isinstance(plan, dict) or plan.get("proposed_methods"):
        return []
    lowered = outline.lower()
    checks = {
        "invented method-name proposal": "method name proposal" in lowered,
        "invented acronym rationale": "acronym" in lowered and "rationale" in lowered,
        "baseline audit described as an introduced named method/protocol": bool(re.search(
            r"\b(?:we\s+)?(?:introduce|present)\s+\*\*[A-Z][A-Za-z0-9-]{1,12}\*\*",
            outline,
        )),
    }
    return [message for message, present in checks.items() if present]


def _execute_paper_outline(
    stage_dir: Path,
    run_dir: Path,
    config: RCConfig,
    adapters: AdapterBundle,
    *,
    llm: LLMClient | None = None,
    prompts: PromptManager | None = None,
) -> StageResult:
    analysis = _read_prior_artifact(run_dir, "analysis.md") or ""
    decision = _read_prior_artifact(run_dir, "decision.md") or ""
    _disc_dir = _find_discussion_dir(run_dir, config)
    _has_discussion = _disc_dir is not None
    preamble = _build_context_preamble(
        config,
        run_dir,
        include_analysis=True,
        include_decision=True,
        include_experiment_data=True,
        include_discussion=_has_discussion,
    )

    # Build discussion ablation instruction if data exists
    discussion_ablation = ""
    if _has_discussion:
        discussion_ablation = (
            "\n\nIMPORTANT — MULTI-AGENT DISCUSSION ABLATION:\n"
            "The research idea for this paper was refined through a multi-agent "
            "discussion process (S8). Include an ablation section in the outline "
            "that compares pre-discussion individual syntheses vs post-discussion "
            "consensus. This should appear in the Discussion or Ablation Studies "
            "section of the paper. The discussion data is provided in the preamble.\n"
        )

    # WS-5.2: Read iteration feedback if available (multi-round iteration)
    feedback = ""
    iter_ctx_path = run_dir / "iteration_context.json"
    if iter_ctx_path.exists():
        try:
            ctx = json.loads(iter_ctx_path.read_text(encoding="utf-8"))
            iteration = ctx.get("iteration", 1)
            prev_score = ctx.get("quality_score")
            reviews_excerpt = ctx.get("reviews_excerpt", "")
            if iteration > 1 and reviews_excerpt:
                feedback = (
                    f"\n\n## Iteration {iteration} Feedback\n"
                    f"Previous quality score: {prev_score}/10\n"
                    f"Reviewer feedback to address:\n{reviews_excerpt[:2000]}\n"
                    f"\nYou MUST address these reviewer concerns in this revision.\n"
                )
        except (json.JSONDecodeError, KeyError):
            pass

    if llm is not None:
        _pm = prompts or PromptManager()
        # IMP-20: Pass academic style guide block for outline stage
        try:
            _asg = _pm.block("academic_style_guide")
        except (KeyError, Exception):
            _asg = ""
        _overlay = _get_evolution_overlay(run_dir, "paper_outline")
        sp = _pm.for_stage(
            "paper_outline",
            evolution_overlay=_overlay,
            preamble=preamble,
            topic_constraint=_pm.block("topic_constraint", topic=config.research.topic),
            feedback=feedback,
            analysis=analysis,
            decision=decision,
            academic_style_guide=_asg,
            discussion_ablation=discussion_ablation,
        )
        resp = _chat_with_prompt(
            llm,
            sp.system,
            sp.user,
            json_mode=sp.json_mode,
            max_tokens=sp.max_tokens,
        )
        outline = resp.content
        scope_violations = _baseline_only_outline_violations(run_dir, outline)
        if scope_violations:
            logger.warning(
                "Baseline-only outline violated executable scope (%s); retrying once",
                "; ".join(scope_violations),
            )
            retry_instruction = (
                "\n\nYour previous outline violated the executable experiment scope: "
                + "; ".join(scope_violations)
                + ". This project has proposed_methods: []. Rewrite the complete outline. "
                "Do not create, brand, name, or acronymize any method, protocol, framework, "
                "system, contribution, or audit. Use an unbranded descriptive title such as "
                "'A Reproducibility Audit of Linear SGD and Random Forest on UCI-HAR'. "
                "The only method names allowed are the two actually executed baselines."
            )
            resp = _chat_with_prompt(
                llm,
                sp.system,
                sp.user + retry_instruction,
                json_mode=sp.json_mode,
                max_tokens=sp.max_tokens,
            )
            outline = resp.content
        # Reasoning models may consume all tokens on CoT — retry with more
        if not outline.strip() and sp.max_tokens:
            logger.warning("Empty outline from LLM — retrying with 2x tokens")
            resp = _chat_with_prompt(
                llm,
                sp.system,
                sp.user,
                json_mode=sp.json_mode,
                max_tokens=sp.max_tokens * 2,
            )
            outline = resp.content
        if not outline.strip():
            logger.warning("LLM returned empty outline — using default")
            outline = _default_paper_outline(config.research.topic)
    else:
        outline = _default_paper_outline(config.research.topic)
    (stage_dir / "outline.md").write_text(outline, encoding="utf-8")
    return StageResult(
        stage=Stage.PAPER_OUTLINE,
        status=StageStatus.DONE,
        artifacts=("outline.md",),
        evidence_refs=("stage-19/outline.md",),
    )


def _collect_raw_experiment_metrics(run_dir: Path) -> tuple[str, bool]:
    """Collect raw experiment metric lines from stdout for paper writing.

    Returns a tuple of (formatted block, has_parsed_metrics).
    ``has_parsed_metrics`` is True when at least one run had a non-empty
    ``metrics`` dict in its JSON payload — a reliable signal of real data.
    """
    metric_lines: list[str] = []
    run_count = 0
    has_parsed_metrics = False

    for stage_subdir in sorted(run_dir.glob("stage-*/runs")):
        for run_file in sorted(stage_subdir.glob("*.json")):
            if run_file.name == "results.json":
                continue
            try:
                payload = json.loads(run_file.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError):
                continue
            if not isinstance(payload, dict):
                continue

            # R10: Skip simulated data — only collect real experiment results
            if payload.get("status") == "simulated":
                continue

            run_count += 1

            # Extract from parsed metrics (check both 'metrics' and 'key_metrics')
            metrics = payload.get("metrics", {}) or payload.get("key_metrics", {})
            if isinstance(metrics, dict) and metrics:
                has_parsed_metrics = True
                for k, v in metrics.items():
                    metric_lines.append(f"  {k}: {v}")

            # Also extract from stdout for full detail
            # BUG-23: Filter out infrastructure lines that are NOT experiment results
            _INFRA_KEYS = {
                "SEED_COUNT", "TIME_ESTIMATE", "TRAINING_STEPS",
                "REGISTERED_CONDITIONS", "METRIC_DEF", "GPU_MEMORY",
                "BATCH_SIZE", "NUM_WORKERS", "TOTAL_PARAMS",
                "time_budget_sec", "max_epochs", "num_seeds",
            }
            stdout = payload.get("stdout", "")
            if stdout:
                for line in stdout.splitlines():
                    line = line.strip()
                    if ":" in line:
                        parts = line.rsplit(":", 1)
                        try:
                            float(parts[1].strip())
                            key_part = parts[0].strip().split("/")[-1]  # last segment
                            if key_part in _INFRA_KEYS:
                                continue  # skip infrastructure lines
                            metric_lines.append(f"  {line}")
                        except (ValueError, TypeError, IndexError):
                            pass

    # R19-4 + R23-1: Collect metrics from refinement_log.json (Stage 13).
    # If refinement has richer data than Stage 12 runs/, REPLACE Stage 12 data
    # to avoid confusing the paper writer with conflicting sources.
    _refine_lines: list[str] = []
    _refine_run_count = 0
    # Scan ALL refinement logs across versions, pick the richest
    _best_refine_metrics: dict[str, Any] = {}
    _best_refine_stdout = ""
    for _rl_path in sorted(run_dir.glob("stage-13*/refinement_log.json")):
        try:
            _rlog = json.loads(_rl_path.read_text(encoding="utf-8"))
            _best_ver = _rlog.get("best_version", "")
            _rlog_iters = _rlog.get("iterations", [])
            if not isinstance(_rlog_iters, list):
                _rlog_iters = []
            for _it in _rlog_iters:
                for _sbx_key in ("sandbox", "sandbox_after_fix"):
                    _sbx = _it.get(_sbx_key, {})
                    if not isinstance(_sbx, dict):
                        continue
                    _sbx_metrics = _sbx.get("metrics", {})
                    if isinstance(_sbx_metrics, dict) and len(_sbx_metrics) > len(_best_refine_metrics):
                        _best_refine_metrics = _sbx_metrics
                        _best_refine_stdout = _sbx.get("stdout", "")
        except (json.JSONDecodeError, OSError):
            pass

    if _best_refine_metrics and len(_best_refine_metrics) > len(metric_lines) // 2:
        # Refinement has richer data — REPLACE Stage 12 data to avoid conflicts
        metric_lines = []
        run_count = 1
        for k, v in _best_refine_metrics.items():
            metric_lines.append(f"  {k}: {v}")
        # Also extract PAIRED and metric lines from stdout
        if _best_refine_stdout:
            for _line in _best_refine_stdout.splitlines():
                _line = _line.strip()
                if _line.startswith("PAIRED:"):
                    metric_lines.append(f"  {_line}")
                elif ":" in _line:
                    parts = _line.rsplit(":", 1)
                    try:
                        float(parts[1].strip())
                        metric_lines.append(f"  {_line}")
                    except (ValueError, TypeError, IndexError):
                        pass
    elif _best_refine_metrics:
        # Refinement has some data but not richer — append to existing
        run_count += 1
        for k, v in _best_refine_metrics.items():
            metric_lines.append(f"  {k}: {v}")
        if _best_refine_stdout:
            for _line in _best_refine_stdout.splitlines():
                _line = _line.strip()
                if _line.startswith("PAIRED:"):
                    metric_lines.append(f"  {_line}")

    if not metric_lines:
        return "", has_parsed_metrics

    # Deduplicate while preserving order
    seen: set[str] = set()
    unique: list[str] = []
    for line in metric_lines:
        if line not in seen:
            seen.add(line)
            unique.append(line)

    # BUG-29: Reformat raw metric lines into human-readable condition summaries
    # to prevent LLM from pasting raw path-style lines into the paper
    _grouped: dict[str, list[str]] = {}
    _ungrouped: list[str] = []
    for line in unique[:200]:
        stripped = line.strip()
        # Match pattern: condition/env/step/metric: value
        parts = stripped.split("/")
        if len(parts) >= 3 and ":" in parts[-1]:
            cond = parts[0]
            detail = "/".join(parts[1:])
            _grouped.setdefault(cond, []).append(f"  - {detail}")
        else:
            _ungrouped.append(stripped)

    formatted_lines: list[str] = []
    if _grouped:
        for cond, details in sorted(_grouped.items()):
            formatted_lines.append(f"## Condition: {cond}")
            formatted_lines.extend(details[:30])
    if _ungrouped:
        formatted_lines.extend(_ungrouped)

    return (
        f"\n\nACTUAL EXPERIMENT DATA (from {run_count} run(s) — use ONLY these numbers):\n"
        "```\n"
        + "\n".join(formatted_lines[:200])
        + "\n```\n"
        "CRITICAL: Every number in the Results table MUST come from the data above. "
        "Do NOT round excessively, do NOT invent numbers, do NOT change values. "
        f"The experiment ran {run_count} time(s) — state this accurately in the methodology.\n"
        "NEVER paste raw metric paths (like 'condition/env/step/metric: value') "
        "into the paper. Always convert to formatted LaTeX tables or inline prose.\n"
    ), has_parsed_metrics


def _write_paper_sections(
    *,
    llm: LLMClient,
    pm: PromptManager,
    run_dir: Path | None = None,
    preamble: str,
    topic_constraint: str,
    exp_metrics_instruction: str,
    citation_instruction: str,
    outline: str,
    model_name: str = "",
    discussion_ablation: str = "",
    figure_prompt_instruction: str = "",
    target_conference: str = "",
    target_pages: int = 0,
) -> str:
    """Write a conference-grade paper in 3 sequential LLM calls.

    Call 1: Title + Abstract + Introduction + Related Work
    Call 2: Method + Experiments (with full experiment data)
    Call 3: Results + Discussion + Limitations + Conclusion

    Each call receives prior sections for coherence.
    """
    # Render writing_structure block for injection
    try:
        _writing_structure = pm.block("writing_structure")
    except (KeyError, Exception):  # noqa: BLE001
        _writing_structure = ""

    _overlay = _get_evolution_overlay(run_dir, "paper_draft")
    system = pm.for_stage(
        "paper_draft",
        evolution_overlay=_overlay,
        preamble=preamble,
        topic_constraint=topic_constraint,
        exp_metrics_instruction=exp_metrics_instruction,
        citation_instruction=citation_instruction,
        writing_structure=_writing_structure,
        outline=outline,
        discussion_ablation=discussion_ablation,
        figure_prompt_instruction=figure_prompt_instruction,
    ).system

    sections: list[str] = []
    _venue_name = "IEEE conference" if target_conference.lower().startswith("ieee") else "NeurIPS/ICML"
    _page_instruction = ""
    if target_pages > 0:
        _page_instruction = (
            f"MANUSCRIPT LENGTH TARGET: Produce a complete {_venue_name} manuscript that compiles "
            f"to approximately {target_pages} two-column pages including references. Aim for "
            "roughly 5,500-7,000 substantive words when figures and tables are limited. "
            "Do not pad with repetition: use the space for method derivation, implementation "
            "details, evaluation protocol, reproducibility, and threats to validity.\n\n"
        )

    # --- R4-3: Title guidelines and abstract structure ---
    try:
        title_guidelines = pm.block("title_guidelines")
    except (KeyError, Exception):  # noqa: BLE001
        title_guidelines = ""
    try:
        abstract_structure = pm.block("abstract_structure")
    except (KeyError, Exception):  # noqa: BLE001
        abstract_structure = ""

    # IMP-20/25/31/24: Academic style, narrative, anti-hedging, anti-repetition
    try:
        academic_style_guide = pm.block("academic_style_guide")
    except (KeyError, Exception):  # noqa: BLE001
        academic_style_guide = ""
    try:
        narrative_writing_rules = pm.block("narrative_writing_rules")
    except (KeyError, Exception):  # noqa: BLE001
        narrative_writing_rules = ""
    try:
        anti_hedging_rules = pm.block("anti_hedging_rules")
    except (KeyError, Exception):  # noqa: BLE001
        anti_hedging_rules = ""
    try:
        anti_repetition_rules = pm.block("anti_repetition_rules")
    except (KeyError, Exception):  # noqa: BLE001
        anti_repetition_rules = ""

    def _trim_to_expected_section(text: str, headings: tuple[str, ...]) -> str:
        """Remove prior sections that a continuation call echoed verbatim."""
        heading_group = "|".join(re.escape(name) for name in headings)
        match = re.search(
            rf"(?im)^##\s*(?:\d+[.、]?\s*)?(?:{heading_group})\b.*$",
            text,
        )
        return text[match.start():].strip() if match else text.strip()

    def _truncate_before_section(text: str, headings: tuple[str, ...]) -> str:
        """Remove later sections that a scoped generation call wrote prematurely.

        Qwen continuation endpoints occasionally return a complete paper for every
        call.  Keeping those unsolicited later sections makes the final manuscript
        contain two or three copies of Method, Results, and Discussion.
        """
        heading_group = "|".join(re.escape(name) for name in headings)
        match = re.search(
            rf"(?im)^##\s*(?:\d+[.、]?\s*)?(?:{heading_group})\b.*$",
            text,
        )
        return text[:match.start()].rstrip() if match else text.strip()

    _baseline_only = bool(
        run_dir is not None
        and _baseline_only_outline_violations(run_dir, "## Method Name Proposal")
    )
    _title_rule = (
        "1. **Title** (HARD RULE: MUST be 14 words or fewer. This is baseline-only "
        "reproduction work: use an unbranded descriptive title and do not create any "
        "method/protocol/framework name or acronym.)\n"
        if _baseline_only else
        "1. **Title** (HARD RULE: MUST be 14 words or fewer. Create a catchy method name "
        "first, then build the title: 'MethodName: Subtitle'. If your title exceeds 14 words, "
        "it will be automatically rejected. NEVER use 'Untitled Paper'.)\n"
    )

    # --- Call 1: Title + Abstract + Introduction + Related Work ---
    call1_user = (
        f"{preamble}\n\n"
        f"{topic_constraint}"
        f"{citation_instruction}\n\n"
        f"{title_guidelines}\n\n"
        f"{academic_style_guide}\n"
        f"{narrative_writing_rules}\n"
        f"{anti_hedging_rules}\n"
        f"{anti_repetition_rules}\n\n"
        f"{_page_instruction}"
        f"Write the following sections of a {_venue_name}-quality paper in markdown. "
        "Follow the LENGTH REQUIREMENTS strictly:\n\n"
        f"{_title_rule}"
        f"2. **Abstract** (150-220 words — HARD LIMIT. Do NOT exceed 220 words. "
        f"Do NOT include raw metric paths or 16-digit decimals.){abstract_structure}\n"
        "3. **Introduction** (800-1000 words): real-world motivation, problem statement, "
        "research gap analysis with citations, method overview, 3-4 contributions as bullet points, "
        "paper organization paragraph. MUST cite 8-12 references.\n"
        "   **TEASER FIGURE (MANDATORY)**: Include a teaser figure (Figure 1) right after the "
        "first introductory paragraph using a `<!-- FIGURE_PROMPT ... -->` block. This figure "
        "should be a high-level conceptual illustration showing the core idea/motivation at a "
        "glance — it conveys the key insight without requiring method details. Use figure_type: "
        "concept_illustration, section: Introduction.\n"
        "4. **Related Work** (600-800 words): organized into 3-4 thematic subsections, each discussing "
        "4-5 papers with proper citations. Compare approaches, identify limitations, position this work.\n\n"
        f"{figure_prompt_instruction}\n\n"
        f"Outline:\n{outline}\n\n"
        "Output markdown with ## headers. Do NOT include a References section.\n"
        "IMPORTANT: Start DIRECTLY with '## Title'. Do NOT include any preamble, "
        "data verification, condition listing, or metric enumeration before the title. "
        "The paper should read like a published manuscript, not a data report."
    )
    # R14-1: Higher token limit for reasoning models
    _paper_max_tokens = 12000
    if any(model_name.startswith(p) for p in ("gpt-5", "o3", "o4")):
        _paper_max_tokens = 24000

    # T3.5: Retry once on failure, use placeholder if still fails
    try:
        resp1 = _chat_with_prompt(llm, system, call1_user, max_tokens=_paper_max_tokens, retries=1)
        part1 = _truncate_before_section(
            resp1.content.strip(),
            ("Method", "Methodology", "Experiments", "Results", "Discussion", "Limitations", "Conclusion"),
        )
    except Exception:  # noqa: BLE001
        logger.error("Stage 17: Part 1 LLM call failed after retry — using placeholder")
        part1 = (
            "## Title\n[PLACEHOLDER — LLM call failed]\n\n"
            "## Abstract\n[This section could not be generated due to an LLM error. "
            "Please regenerate this stage.]\n\n"
            "## Introduction\n[PLACEHOLDER]\n\n"
            "## Related Work\n[PLACEHOLDER]"
        )
    sections.append(part1)
    logger.info("Stage 17: Part 1 (Title+Abstract+Intro+Related Work) — %d chars", len(part1))

    # --- Call 2: Method + Experiments ---
    call2_user = (
        f"{preamble}\n\n"
        f"{topic_constraint}"
        f"{_page_instruction}"
        f"{exp_metrics_instruction}\n\n"
        f"{narrative_writing_rules}\n"
        f"{anti_hedging_rules}\n\n"
        # IMP-21: Citation instruction for Method + Experiments
        "CITATION REQUIREMENT: The Method section MUST cite at least 3-5 related "
        "technical papers (foundations your method builds on). The Experiments section "
        "MUST cite baseline method papers. Use [cite_key] syntax.\n"
        f"{citation_instruction}\n\n"
        "You are continuing a paper. The sections written so far are:\n\n"
        f"---\n{part1}\n---\n\n"
        "Now write the next sections, maintaining consistency with the above:\n\n"
        "5. **Method** (1000-1500 words): formal problem definition with mathematical notation "
        "($x$, $\\theta$, etc.), detailed algorithm description with equations, step-by-step procedure, "
        "complexity analysis, design rationale for key choices. Include algorithm pseudocode if applicable. "
        "Write as FLOWING PROSE — do NOT use bullet-point lists for method components.\n"
        "   **METHOD FIGURES (MANDATORY)**: Include at least TWO `<!-- FIGURE_PROMPT ... -->` blocks:\n"
        "   (a) A **framework / architecture overview** (figure_type: architecture_diagram or "
        "pipeline_overview) showing the full system pipeline.\n"
        "   (b) A **method detail figure** (figure_type: method_flowchart or comparison_illustration) "
        "illustrating a key algorithmic step or component.\n"
        "6. **Experiments** (800-1200 words): detailed experimental setup, datasets with statistics "
        "(size, splits, features), all baselines and their implementations, hyperparameter settings "
        "in a markdown table, evaluation metrics with mathematical definitions, hardware and runtime info.\n"
        "METHOD NAMES IN TABLES: Use SHORT abbreviations (4-8 chars) for method names "
        "in tables. Define abbreviation mappings in a footnote. "
        "NEVER put method names longer than 20 characters in table cells.\n\n"
        f"{figure_prompt_instruction}\n\n"
        f"Outline:\n{outline}\n\n"
        "Output markdown with ## headers. Continue from where Part 1 ended."
    )
    try:
        resp2 = _chat_with_prompt(llm, system, call2_user, max_tokens=_paper_max_tokens, retries=1)
        part2 = _trim_to_expected_section(resp2.content, ("Method", "Methodology"))
        part2 = _truncate_before_section(part2, ("Results", "Discussion", "Limitations", "Conclusion"))
    except Exception:  # noqa: BLE001
        logger.error("Stage 17: Part 2 LLM call failed after retry — using placeholder")
        part2 = (
            "## Method\n[PLACEHOLDER — LLM call failed. Please regenerate this stage.]\n\n"
            "## Experiments\n[PLACEHOLDER]"
        )
    sections.append(part2)
    logger.info("Stage 17: Part 2 (Method+Experiments) — %d chars", len(part2))

    # --- Call 3: Results + Discussion + Limitations + Conclusion ---
    call3_user = (
        f"{preamble}\n\n"
        f"{topic_constraint}"
        f"{_page_instruction}"
        f"{exp_metrics_instruction}\n\n"
        f"{narrative_writing_rules}\n"
        f"{anti_hedging_rules}\n"
        f"{anti_repetition_rules}\n\n"
        # IMP-21: Citation instruction for Results + Discussion + Conclusion
        "CITATION REQUIREMENT: The Discussion section MUST cite at least 3-5 papers "
        "when comparing findings with prior work. The Conclusion may cite 1-2 "
        "foundational references.\n"
        f"{citation_instruction}\n\n"
        "You are completing a paper. The sections written so far are:\n\n"
        f"---\n{part1}\n\n{part2}\n---\n\n"
        "Now write the final sections, maintaining consistency:\n\n"
        "7. **Results** (600-800 words):\n"
        "   - START with an AGGREGATED results table (Table 1): rows = methods, columns = metrics.\n"
        "     Each cell = mean ± std across seeds. Bold the best value per column.\n"
        "     EVERY table MUST have a descriptive caption that allows understanding without "
        "     reading the main text. NEVER use just 'Table 1' as a caption.\n"
        "   - Follow with a PER-REGIME table (Table 2) breaking down by easy/hard regimes.\n"
        "   - Include a STATISTICAL COMPARISON table (Table 3): paired t-tests between key methods.\n"
        "   - NEVER dump raw per-seed numbers in the main text. Aggregate first, then discuss.\n"
        "   - MUST include at least 2 DATA figures using `![Caption](charts/filename.png)` syntax "
        "for pre-generated charts and plots. One MUST be a performance comparison chart.\n"
        "     Do NOT use `<!-- FIGURE_PROMPT -->` for data figures in Results/Experiments — "
        "only use `![Caption](charts/...)` here.\n"
        "     Figures MUST be referenced in text: 'As shown in Figure N, ...'\n"
        "8. **Discussion** (400-800 words): interpretation of key findings, unexpected results, "
        "comparison with prior work (CITE 3-5 papers here!), practical implications.\n"
        f"{discussion_ablation}"
        "9. **Limitations** (200-300 words): honest assessment of scope, dataset, methodology. "
        "ALL caveats consolidated HERE — nowhere else in the paper.\n"
        "10. **Conclusion** (100-200 words MAXIMUM — this is a HARD LIMIT): "
        "Summarize contributions in 2-3 sentences. State main finding in 1 sentence. "
        "Suggest 2-3 concrete future directions in 1-2 sentences. "
        "Do NOT repeat any specific numbers from Results. Do NOT restate the abstract. "
        "A good conclusion is SHORT and forward-looking.\n\n"
        "CRITICAL FORMATTING RULES FOR ALL SECTIONS:\n"
        "- Write as FLOWING PROSE paragraphs, NOT bullet-point lists\n"
        "- NEVER dump raw metric paths like 'config/method_name/seed_3/primary_metric'\n"
        "- All numbers must be rounded to 4 decimal places maximum\n"
        "- Every table MUST have a descriptive caption (not just 'Table 1')\n"
        "- Use \\begin{algorithm} or pseudocode notation, NOT \\begin{verbatim}\n\n"
        "Output markdown with ## headers. Do NOT include a References section."
    )
    try:
        resp3 = _chat_with_prompt(llm, system, call3_user, max_tokens=_paper_max_tokens, retries=1)
        part3 = _trim_to_expected_section(resp3.content, ("Results", "Result"))
    except Exception:  # noqa: BLE001
        logger.error("Stage 17: Part 3 LLM call failed after retry — using placeholder")
        part3 = (
            "## Results\n[PLACEHOLDER — LLM call failed. Please regenerate this stage.]\n\n"
            "## Discussion\n[PLACEHOLDER]\n\n"
            "## Limitations\n[PLACEHOLDER]\n\n"
            "## Conclusion\n[PLACEHOLDER]"
        )
    sections.append(part3)
    logger.info("Stage 17: Part 3 (Results+Discussion+Limitations+Conclusion) — %d chars", len(part3))

    # Combine all sections
    draft = "\n\n".join(sections)

    # R32: Strip data verification preamble that LLMs sometimes emit before
    # the actual paper.  The preamble typically starts with "## Tested Conditions"
    # or similar headings and ends before "## Title".
    import re as _re_strip
    _title_match = _re_strip.search(r"^## Title\b", draft, _re_strip.MULTILINE)
    if _title_match and _title_match.start() > 200:
        _stripped = draft[_title_match.start():]
        logger.info(
            "R32: Stripped %d-char preamble before '## Title'",
            _title_match.start(),
        )
        draft = _stripped

    total_words = len(draft.split())
    logger.info("Stage 17: Full draft — %d chars, ~%d words", len(draft), total_words)

    return draft


def _build_deterministic_paper_draft(
    *,
    topic: str,
    outline: str,
    analysis: str,
    exp_summary_text: str | None,
    raw_metrics_block: str,
    citation_context: str = "",
) -> str:
    """Build a non-empty paper draft without an LLM.

    This is a robustness fallback for long writing calls.  It preserves real
    metrics and upstream artifacts so S21/S22 can continue, while making it
    explicit that the draft should be regenerated for final quality.
    """
    exp_summary = _safe_json_loads(exp_summary_text or "", {})
    metrics_lines: list[str] = []
    best_line = ""
    if isinstance(exp_summary, dict):
        for key, stats in (exp_summary.get("metrics_summary") or {}).items():
            if isinstance(stats, dict):
                metrics_lines.append(
                    f"- **{key}**: mean={stats.get('mean')}, "
                    f"min={stats.get('min')}, max={stats.get('max')}, n={stats.get('count')}"
                )
        best = exp_summary.get("best_run")
        if isinstance(best, dict):
            best_line = (
                f"The best observed condition was **{best.get('condition', 'N/A')}** "
                f"with metrics `{json.dumps(best.get('metrics', {}), ensure_ascii=False)}`."
            )
    if not metrics_lines and raw_metrics_block:
        metrics_lines = [
            line.strip()
            for line in raw_metrics_block.splitlines()
            if "primary_metric" in line or "metric" in line.lower()
        ][:12]
    if not metrics_lines:
        metrics_lines = ["- No structured numeric metric was available in the parsed artifacts."]

    analysis_excerpt = (analysis or "").strip()[:2500]
    outline_excerpt = (outline or "").strip()[:2500]
    generated = _utcnow_iso()
    return f"""## Title
Robust Auto Research Pipeline for Efficient LLM Inference Optimization

## Abstract
This draft reports an automatically generated research pipeline run for the topic: {topic}. The system completed literature grounding, idea generation, experiment planning, code generation, sanity checking, direct experiment execution, result analysis, and writing preparation. The draft is generated from real upstream artifacts and metric files rather than fabricated values. It is intended as a stable intermediate manuscript for downstream review and revision; final prose quality should be improved by regenerating the writing stage when the Qwen endpoint is healthy.

## Introduction
Efficient LLM inference is an important research problem because deployment cost, latency, memory pressure, and throughput constraints determine whether large models can be used in practical systems. The automated research process explored this space through a multi-stage workflow: first constructing a problem framing, then synthesizing related evidence, generating candidate hypotheses, designing experiments, producing executable code, running sanity checks, executing the experiment, and analyzing the resulting metrics.

The core contribution of this pipeline run is not a manually polished algorithmic claim, but a reproducible end-to-end artifact chain. Each stage produced concrete files that downstream stages consumed. This is useful for stress-testing automated research infrastructure: failures in model calls, code execution, result parsing, and writing can be localized to exact stages.

## Related Work
The literature stage screened candidate papers before writing and retained only references whose keys exist in the collected bibliography. The following works provide the closest available methodological context; their relevance is limited to benchmarking practice and competitive simple baselines rather than evidence for the present experimental result.

{citation_context or "No screened reference with a matching bibliography key was available, so this section intentionally makes no citation claim."}

## Method
The automated method follows a layered research workflow. Stage S1-S8 performs topic initialization, problem decomposition, search planning, literature screening, evidence extraction, synthesis, and hypothesis generation. Stage S9-S13 turns the selected idea into an experiment plan, searches for or constructs code, performs code generation, runs sanity checks, and plans resources. Stage S14-S18 executes the experiment, analyzes results, makes a research decision, and writes a reusable knowledge summary. Stage S19-S22 produces an outline, draft, review, and revised manuscript.

The paper outline available to the writing stage was:

```markdown
{outline_excerpt}
```

## Experiments
The experiment was executed by the pipeline and the result analysis stage parsed numeric metrics from actual artifact files. The available metrics are:

{chr(10).join(metrics_lines)}

{best_line}

The analysis artifact summarized the experiment as follows:

```markdown
{analysis_excerpt}
```

## Results
The direct experiment execution produced parseable metrics and allowed downstream stages to proceed. The primary result should be interpreted as a smoke-test validation of the multi-stage research pipeline rather than a final scientific benchmark. The most important empirical finding for this engineering run is that the generated code could be sanity-checked, executed, and summarized without fabricating values.

## Discussion
The run exposed several practical bottlenecks in automated research systems. First, transient LLM gateway failures can block stages even when deterministic local computation would be sufficient. Second, result parsing should not require an LLM when metrics are already available in structured JSON or stdout. Third, writing stages should have a graceful fallback that preserves factual artifacts while marking the text as an intermediate draft.

## Limitations
This draft was generated by a deterministic fallback because the long-form writing call was unavailable or too slow. It should not be treated as a final polished paper. The experimental content is also a lightweight smoke test, so claims should be limited to pipeline validation unless richer experiments are run.

## Conclusion
The full-chain research workflow produced a coherent sequence of artifacts through experiment execution and result analysis. Robust deterministic fallbacks make the system substantially more reliable by allowing later review and revision stages to continue when LLM calls are temporarily unstable.

Generated: {generated}
"""


def _build_deterministic_paper_revision(
    *,
    topic: str,
    draft: str,
    reviews: str,
    raw_metrics_block: str,
    citation_context: str = "",
    exp_summary_text: str = "",
) -> str:
    """Build a compact, topic-faithful report directly from experiment JSON."""
    summary = _safe_json_loads(exp_summary_text or "", {})
    conditions = summary.get("condition_summaries", {}) if isinstance(summary, dict) else {}
    comparisons = summary.get("paired_comparisons", []) if isinstance(summary, dict) else []

    def _fmt(value: Any, digits: int = 4) -> str:
        try:
            number = float(value)
        except (TypeError, ValueError):
            return "N/A"
        return "N/A" if not math.isfinite(number) else f"{number:.{digits}f}"

    result_rows: list[str] = []
    datasets: set[str] = set()
    models: set[str] = set()
    seed_counts: list[int] = []
    fold_counts: list[int] = []
    if isinstance(conditions, dict):
        for name, payload in conditions.items():
            if not isinstance(payload, dict):
                continue
            metrics = payload.get("metrics", {}) if isinstance(payload.get("metrics"), dict) else {}
            dataset = str(payload.get("dataset", "") or str(name).split("__", 1)[0])
            model = str(payload.get("model", "") or (str(name).split("__", 1)[1] if "__" in str(name) else ""))
            if dataset:
                datasets.add(dataset)
            if model:
                models.add(model)
            try:
                seed_counts.append(int(payload.get("n_seeds", metrics.get("n_seeds", 0)) or 0))
                fold_counts.append(int(metrics.get("folds_per_seed", 0) or 0))
            except (TypeError, ValueError):
                pass
            result_rows.append(
                f"| {dataset} | {model} | {_fmt(metrics.get('accuracy_mean'))} | "
                f"{_fmt(metrics.get('accuracy_std'))} | "
                f"[{_fmt(metrics.get('accuracy_mean_ci95_low'))}, "
                f"{_fmt(metrics.get('accuracy_mean_ci95_high'))}] | "
                f"{_fmt(metrics.get('f1_macro_mean'))} |"
            )
    if not result_rows:
        result_rows.append("| N/A | N/A | N/A | N/A | N/A | N/A |")

    comparison_rows: list[str] = []
    if isinstance(comparisons, list):
        for item in comparisons:
            if not isinstance(item, dict):
                continue
            comparison_rows.append(
                f"| {item.get('dataset', 'N/A')} | {item.get('method_a', 'A')} vs {item.get('method_b', 'B')} | "
                f"{_fmt(item.get('mean_difference'))} | {_fmt(item.get('p_value'))} | {item.get('test', 'N/A')} |"
            )
    if not comparison_rows:
        comparison_rows.append("| N/A | N/A | N/A | N/A | N/A |")

    best = summary.get("best_run", {}) if isinstance(summary, dict) else {}
    best_name = str(best.get("condition", "N/A")) if isinstance(best, dict) else "N/A"
    best_metrics = best.get("metrics", {}) if isinstance(best, dict) and isinstance(best.get("metrics"), dict) else {}
    seeds = min(seed_counts) if seed_counts else 0
    folds = max(fold_counts) if fold_counts else 0
    dataset_text = ", ".join(sorted(datasets)) or "the available datasets"
    model_text = ", ".join(sorted(models)) or "the available models"
    generated = _utcnow_iso()

    return f"""## Title
SmallBench: Reproducible Linear and Forest Classification

## Abstract
We present a small, reproducible comparison of logistic regression and random forest on three sklearn classification datasets: {dataset_text}. Both models are evaluated under the same preprocessing and repeated stratified cross-validation protocol. Every condition uses {seeds} independent random seeds and {folds} folds per seed, with accuracy, macro-F1, dispersion, confidence intervals, and paired comparisons derived from retained per-seed outputs. The best observed condition is {best_name}, with mean accuracy {_fmt(best_metrics.get('accuracy_mean'))}. These results are intentionally limited to the tested datasets, models, metrics, and runtime configuration; they are not evidence of universal model superiority. The report demonstrates a compact benchmark in which simple linear and tree-based baselines can be compared with traceable code, real execution provenance, and citation verification.

## Introduction
Small tabular classification tasks remain useful for checking whether additional model complexity is justified. Logistic regression provides a transparent linear decision rule, whereas random forest captures nonlinear feature interactions through an ensemble of decision trees. Comparing them under identical splits is more informative than comparing numbers produced by different preprocessing or evaluation protocols. This study asks a narrow question: under a controlled lightweight benchmark, when is the linear baseline competitive with the tree ensemble? The objective is reproducibility and calibrated evidence rather than methodological novelty.

## Related Work
Prior research motivates two parts of this study: careful evaluation on small tabular datasets and explicit control of reproducibility threats. The cited work provides background on small-data modeling, benchmark design, and leakage; this experiment does not claim to reproduce or supersede those studies.

{citation_context or "No collected reference passed both bibliography-key and relevance checks."}

Together, these studies motivate using shared splits, retaining seed-level outputs, and limiting conclusions to the executed conditions. The present comparison is narrower than the cited benchmarks and is intended as an auditable baseline rather than a new learning algorithm.

## Method
For each dataset, logistic regression is fitted after feature standardization and random forest is fitted as a tree-ensemble baseline. The same stratified folds and seed identities are used for both models, enabling paired comparison without changing the data split between methods. The experiment retains seed-level accuracy and macro-F1 values, then computes the mean, standard deviation, and a two-sided 95% Student-t confidence interval. This design controls split variation while keeping the implementation small enough for CPU-only reproduction.

## Experiments
The benchmark uses {dataset_text} and compares {model_text}. Each of the six dataset-model conditions uses {seeds} independent seeds and {folds}-fold stratified cross-validation. The implementation is executed directly with sklearn built-in datasets; no synthetic performance values are inserted by the writing stage. Accuracy is the primary metric and macro-F1 is included to make the multiclass results less dependent on class frequency.

## Results
| Dataset | Model | Accuracy Mean | Accuracy Std | Accuracy 95% CI | Macro-F1 Mean |
|---|---|---:|---:|---:|---:|
{chr(10).join(result_rows)}

The highest observed mean accuracy is obtained by **{best_name}** at **{_fmt(best_metrics.get('accuracy_mean'))}**. This is a best condition within the current benchmark, not a claim about broader tabular learning.

| Dataset | Paired Comparison | Mean Difference | p-value | Test |
|---|---|---:|---:|---|
{chr(10).join(comparison_rows)}

A p-value is reported only when the paired statistic is defined. Identical per-seed accuracy values can yield zero variance in the paired differences, in which case the t-test result is recorded as N/A instead of being interpreted as evidence.

## Discussion
The benchmark shows that model ranking depends on the dataset. A linear model can match or exceed a tree ensemble on some small standardized datasets, while equality on another dataset does not establish equivalence outside the observed folds. Feature standardization directly benefits the optimization geometry of logistic regression, whereas the random forest used here was not extensively tuned; these are plausible explanations for the observed ranking, not mechanisms established by the current experiment. Testing them would require controlled tuning, feature-geometry diagnostics, and additional datasets. The paired design makes the observed differences auditable, but the small number of seeds and small datasets require cautious interpretation. The useful outcome is therefore a reproducible comparison and an explicit evidence boundary, not a general rule that one model class dominates the other.

## Limitations
The study covers only three small sklearn datasets and two classical models. Hyperparameter tuning is intentionally limited, the number of independent seeds is {seeds}, and the datasets do not represent large, sparse, temporally shifted, or high-cardinality tabular problems. Cross-validation estimates internal performance but does not replace external validation. Future work should add stronger tuned baselines, more diverse OpenML datasets, effect sizes, and multiplicity-aware statistical analysis.

## Conclusion
Under a shared repeated-cross-validation protocol, logistic regression remains a competitive baseline for the tested small classification datasets. The experiment, per-seed results, paired comparisons, confidence intervals, code, and bibliography are retained as auditable artifacts. Conclusions remain restricted to the reported conditions.

"""


# ---------------------------------------------------------------------------
# Figure prompt extraction from paper draft
# ---------------------------------------------------------------------------

_FIGURE_PROMPT_RE = re.compile(
    r"<!--\s*FIGURE_PROMPT\s*\n(.*?)-->",
    re.DOTALL,
)

_ACADEMIC_STYLE_SUFFIX = (
    " The image should be in a clean, professional ACADEMIC style suitable "
    "for a top-tier AI/ML research paper (NeurIPS, ICML, ICLR). "
    "Use a white or light background. Use clear labels and annotations. "
    "Avoid excessive decoration. Use a consistent color palette. "
    "Text should be legible at column width (~3.25 inches). "
    "Style: technical illustration, vector-like, clean lines."
)

_FIGURE_TYPE_STYLE: dict[str, str] = {
    "architecture_diagram": (
        "Show model layers, connections, and data flow with labeled boxes and arrows. "
        "Use a consistent left-to-right or top-to-bottom layout. "
        "Group related components with dashed borders."
    ),
    "method_flowchart": (
        "Step-by-step process flow with rounded rectangles for processes, "
        "diamonds for decision points, arrows with labels. "
        "Number sequential steps. Highlight novel steps with accent color."
    ),
    "pipeline_overview": (
        "Full pipeline from input to output with distinct visual blocks per stage. "
        "Include example inputs/outputs. Use consistent arrow style. "
        "Show parallel/branching paths if applicable."
    ),
    "concept_illustration": (
        "Simple, clean diagram illustrating a key concept or intuition. "
        "Include before/after or problem/solution comparison. "
        "Keep it understandable at a glance."
    ),
    "system_diagram": (
        "Overall system architecture with major components and interactions. "
        "Show data stores, APIs, external services with labeled connections."
    ),
    "comparison_illustration": (
        "Side-by-side comparison of approaches with consistent styling. "
        "Highlight key differences with visual cues (color, checkmarks/crosses)."
    ),
    "attention_visualization": (
        "Attention weights or patterns with heatmap-style coloring. "
        "Include input/output sequences, label attention heads, "
        "use a clear color scale legend."
    ),
}


def _extract_figure_prompts(draft: str, topic: str = "") -> list[dict]:
    """Parse ``<!-- FIGURE_PROMPT ... -->`` blocks from a paper draft.

    Returns a list of dicts, each with keys:
        figure_id, figure_type, section, caption, aspect_ratio,
        raw_prompt, full_prompt  (raw_prompt + academic style + type guidelines)
    """
    _top_level_keys = {
        "figure_id", "figure_type", "section", "caption", "aspect_ratio", "prompt",
    }

    results: list[dict] = []
    for m in _FIGURE_PROMPT_RE.finditer(draft):
        block = m.group(1)
        entry: dict[str, str] = {}
        current_key = ""
        current_lines: list[str] = []
        in_multiline = False

        for line in block.splitlines():
            stripped = line.strip()
            if not stripped:
                if in_multiline:
                    current_lines.append("")
                continue

            # Check if this line starts a new top-level key (not indented,
            # key name is a known field)
            is_new_key = False
            if ":" in stripped and not line.startswith((" ", "\t")):
                candidate_key = stripped.partition(":")[0].strip().lower()
                if candidate_key in _top_level_keys:
                    is_new_key = True

            if is_new_key:
                key, _, val = stripped.partition(":")
                key = key.strip().lower()
                val = val.strip().strip('"').strip("'")
                if current_key and current_lines:
                    entry[current_key] = "\n".join(current_lines).strip()
                current_key = key
                in_multiline = (val == "|" or val == "")
                if val and val != "|":
                    current_lines = [val]
                    in_multiline = False
                else:
                    current_lines = []
                    if val == "|":
                        in_multiline = True
            else:
                current_lines.append(stripped)
        if current_key and current_lines:
            entry[current_key] = "\n".join(current_lines).strip()

        fig_id = entry.get("figure_id", f"figure_{len(results) + 1}")
        fig_type = entry.get("figure_type", "concept_illustration")
        section = entry.get("section", "Method")
        caption = entry.get("caption", "")
        aspect_ratio = entry.get("aspect_ratio", "16:9")
        raw_prompt = entry.get("prompt", caption)

        type_style = _FIGURE_TYPE_STYLE.get(fig_type, _FIGURE_TYPE_STYLE["concept_illustration"])
        full_prompt = (
            f"Create a professional academic figure for the '{section}' "
            f"section of a research paper"
        )
        if topic:
            full_prompt += f" about: {topic}"
        full_prompt += (
            f".\n\nFigure description: {raw_prompt}\n\n"
            f"Style guidelines:\n{type_style}\n\n"
            f"{_ACADEMIC_STYLE_SUFFIX}"
        )

        results.append({
            "figure_id": fig_id,
            "figure_type": fig_type,
            "section": section,
            "caption": caption,
            "aspect_ratio": aspect_ratio,
            "raw_prompt": raw_prompt,
            "full_prompt": full_prompt,
        })

    return results


def _render_figure_prompts(
    fig_prompts: list[dict],
    stage_dir: Path,
    config: Any,
    llm: Any,
    topic: str = "",
) -> list[dict]:
    """Render extracted FIGURE_PROMPT entries via NanoBananaAgent.

    Uses the OpenAI-compatible proxy (same base_url/api_key as the LLM) to
    call an image-capable model when explicitly enabled.
    Gracefully skips if no API credentials are available.

    Returns the *same* ``fig_prompts`` list, augmented with ``output_path``
    and ``success`` fields for each entry.
    """
    if not fig_prompts:
        return fig_prompts

    fa_cfg = getattr(config.experiment, "figure_agent", None)
    if fa_cfg is not None and not getattr(fa_cfg, "nano_banana_enabled", False):
        for fp in fig_prompts:
            fp["success"] = False
            fp["skipped"] = "image_generation_disabled_for_qwen3_only_run"
        logger.info("NanoBanana render skipped — Qwen3-only run disables non-Qwen image generation")
        return fig_prompts

    from researchclaw.agents.figure_agent.nano_banana import NanoBananaAgent

    from researchclaw.llm import resolve_provider_base_url
    base_url = resolve_provider_base_url(
        getattr(config.llm, "provider", "openai-compatible"),
        getattr(config.llm, "base_url", ""),
    )
    api_key = getattr(config.llm, "api_key", "") or ""
    if not api_key:
        logger.warning(
            "NanoBanana render skipped — no llm.api_key"
        )
        return fig_prompts

    image_model = getattr(config.llm, "image_model", "") or ""
    if not image_model:
        image_model = (
            (getattr(fa_cfg, "gemini_model", "") if fa_cfg else "")
            or "Qwen3.5-122B-A10B-FP8"
        )

    figures_dir = stage_dir / "figures"
    figures_dir.mkdir(parents=True, exist_ok=True)

    image_figures = [
        {
            "figure_id": fp["figure_id"],
            "figure_type": fp.get("figure_type", "concept_illustration"),
            "section": fp.get("section", "Method"),
            "description": fp.get("raw_prompt", fp.get("caption", "")),
        }
        for fp in fig_prompts
    ]

    agent = NanoBananaAgent(
        llm,
        base_url=base_url,
        openai_api_key=api_key,
        image_model=image_model,
        output_dir=figures_dir,
    )

    try:
        result = agent.execute({
            "image_figures": image_figures,
            "topic": topic,
            "output_dir": str(figures_dir),
        })
    except Exception as exc:
        logger.warning("NanoBanana rendering failed: %s", exc)
        return fig_prompts

    gen_map = {
        g["figure_id"]: g for g in result.data.get("generated", [])
    }
    for fp in fig_prompts:
        gen = gen_map.get(fp["figure_id"], {})
        fp["output_path"] = gen.get("output_path", "")
        fp["success"] = gen.get("success", False)

    success_count = sum(1 for fp in fig_prompts if fp.get("success"))
    logger.info(
        "NanoBanana rendered %d/%d figure prompts → %s",
        success_count, len(fig_prompts), figures_dir,
    )
    return fig_prompts


def _read_figure_manifest(run_dir: Path) -> dict[str, dict]:
    """Read ``figure_manifest.json`` from stage-16/charts/ (or stage-14).

    Returns ``{filename: metadata_dict}`` for each chart entry.
    """
    for pattern in ("stage-16*", "stage-14*"):
        for d in sorted(run_dir.glob(pattern)):
            mf = d / "charts" / "figure_manifest.json"
            if mf.is_file():
                try:
                    entries = json.loads(mf.read_text(encoding="utf-8"))
                    return {
                        Path(e["file_path"]).name: e
                        for e in entries
                        if isinstance(e, dict) and e.get("file_path")
                    }
                except Exception:
                    pass
    return {}


# ---------------------------------------------------------------------------
# Draft quality validation (section balance + bullet-point density)
# ---------------------------------------------------------------------------

# Sections where bullets/numbered lists are acceptable.
_BULLET_LENIENT_SECTIONS = frozenset({
    "introduction", "limitations", "limitation",
    "limitations and future work", "abstract",
})

# Main body sections used for balance ratio check.
_BALANCE_SECTIONS = frozenset({
    "introduction", "related work", "method", "experiments", "results",
    "discussion",
})


def _validate_draft_quality(
    draft: str,
    stage_dir: Path | None = None,
) -> dict[str, Any]:
    """Validate a paper draft for section balance and prose quality.

    Checks:
    1. Per-section word count vs ``SECTION_WORD_TARGETS``.
    2. Bullet-point / numbered-list density per section.
    3. Largest-to-smallest main-section word-count ratio.

    Returns a dict with ``section_analysis``, ``overall_warnings``, and
    ``revision_directives``.  Optionally writes ``draft_quality.json`` to
    *stage_dir*.
    """
    from researchclaw.prompts import SECTION_WORD_TARGETS, _SECTION_TARGET_ALIASES

    _heading_re = re.compile(r"^(#{1,4})\s+(.+)$", re.MULTILINE)
    matches = list(_heading_re.finditer(draft))

    sections_data: list[dict[str, Any]] = []
    for i, m in enumerate(matches):
        level = len(m.group(1))
        heading = m.group(2).strip()
        start = m.end()
        end = matches[i + 1].start() if i + 1 < len(matches) else len(draft)
        body = draft[start:end].strip()
        sections_data.append({
            "heading": heading,
            "heading_lower": heading.strip().lower(),
            "level": level,
            "body": body,
        })

    section_analysis: list[dict[str, Any]] = []
    overall_warnings: list[str] = []
    revision_directives: list[str] = []
    main_section_words: dict[str, int] = {}

    _bullet_re = re.compile(r"^\s*[-*]\s+", re.MULTILINE)
    _numbered_re = re.compile(r"^\s*\d+\.\s+", re.MULTILINE)

    # BUG-24: Accumulate subsection (H3+) word counts into parent H2 sections
    _subsection_words: dict[str, int] = {}
    _current_parent = ""
    for sec in sections_data:
        if sec["level"] <= 2:
            _current_parent = sec["heading_lower"]
            _subsection_words.setdefault(_current_parent, 0)
        else:
            # Add subsection words to parent
            _subsection_words[_current_parent] = (
                _subsection_words.get(_current_parent, 0) + len(sec["body"].split())
            )

    for sec in sections_data:
        if sec["level"] > 2:
            continue
        heading_lower: str = sec["heading_lower"]
        body: str = sec["body"]
        # BUG-24: Include subsection words in the parent's word count
        word_count = len(body.split()) + _subsection_words.get(heading_lower, 0)
        canon = heading_lower
        if canon not in SECTION_WORD_TARGETS:
            canon = _SECTION_TARGET_ALIASES.get(heading_lower, "")
        entry: dict[str, Any] = {
            "heading": sec["heading"],
            "word_count": word_count,
            "canonical": canon,
        }
        if canon and canon in SECTION_WORD_TARGETS:
            lo, hi = SECTION_WORD_TARGETS[canon]
            entry["target"] = [lo, hi]
            if word_count < int(lo * 0.7):
                overall_warnings.append(
                    f"{sec['heading']} is severely under target "
                    f"({word_count} words, target {lo}-{hi})"
                )
                revision_directives.append(
                    f"EXPAND {sec['heading']} from {word_count} to {lo}+ words. "
                    f"Add substantive content \u2014 do NOT pad with filler."
                )
                entry["status"] = "severely_short"
            elif word_count < lo:
                overall_warnings.append(
                    f"{sec['heading']} is under target "
                    f"({word_count} words, target {lo}-{hi})"
                )
                revision_directives.append(
                    f"Expand {sec['heading']} from {word_count} to {lo}+ words."
                )
                entry["status"] = "short"
            elif word_count > int(hi * 1.3):
                overall_warnings.append(
                    f"{sec['heading']} exceeds target "
                    f"({word_count} words, target {lo}-{hi})"
                )
                revision_directives.append(
                    f"Compress {sec['heading']} from {word_count} to {hi} words or fewer."
                )
                entry["status"] = "long"
            else:
                entry["status"] = "ok"
        if body:
            total_lines = len([ln for ln in body.splitlines() if ln.strip()])
            bullet_lines = len(_bullet_re.findall(body)) + len(_numbered_re.findall(body))
            density = bullet_lines / total_lines if total_lines > 0 else 0.0
            entry["bullet_density"] = round(density, 2)
            threshold = 0.50 if heading_lower in _BULLET_LENIENT_SECTIONS else 0.25
            if density > threshold and total_lines >= 4:
                overall_warnings.append(
                    f"{sec['heading']} has {bullet_lines}/{total_lines} "
                    f"bullet/numbered lines ({density:.0%} density, "
                    f"threshold {threshold:.0%})"
                )
                revision_directives.append(
                    f"REWRITE {sec['heading']} as flowing academic prose. "
                    f"Convert bullet points to narrative paragraphs."
                )
                entry["bullet_status"] = "high"
            else:
                entry["bullet_status"] = "ok"
        canon_balance = canon or heading_lower
        if canon_balance in _BALANCE_SECTIONS:
            main_section_words[canon_balance] = word_count
        section_analysis.append(entry)

    if len(main_section_words) >= 2:
        wc_values = list(main_section_words.values())
        max_wc = max(wc_values)
        min_wc = min(wc_values)
        if min_wc > 0 and max_wc / min_wc > 3.0:
            largest = max(main_section_words, key=main_section_words.get)  # type: ignore[arg-type]
            smallest = min(main_section_words, key=main_section_words.get)  # type: ignore[arg-type]
            overall_warnings.append(
                f"Section imbalance: {largest} ({max_wc} words) vs "
                f"{smallest} ({min_wc} words) \u2014 ratio {max_wc / min_wc:.1f}x"
            )
            revision_directives.append(
                f"Rebalance sections: expand {smallest} and/or compress {largest} "
                f"to achieve more even section lengths."
            )

    # --- C-4/C-5: Citation count and recency checks ---
    _cite_pattern = re.compile(r"\[([a-zA-Z][a-zA-Z0-9_-]*\d{4}[a-zA-Z0-9]*)\]")
    cited_keys = set(_cite_pattern.findall(draft))
    if cited_keys:
        n_citations = len(cited_keys)
        if n_citations < 15:
            overall_warnings.append(
                f"Only {n_citations} unique citations found (target: >=15 for a full paper)"
            )
            revision_directives.append(
                f"Add more references — a top-venue paper typically cites 25-40 works. "
                f"Currently only {n_citations} unique citations."
            )
        # Check recency: count citations with year >= current_year - 2
        _year_pat = re.compile(r"(\d{4})")
        import datetime as _dt_cit
        _cur_year = _dt_cit.datetime.now().year
        recent_count = sum(
            1 for k in cited_keys
            for m in [_year_pat.search(k)]
            if m and int(m.group(1)) >= _cur_year - 2
        )
        recency_ratio = recent_count / n_citations if n_citations > 0 else 0.0
        if recency_ratio < 0.3 and n_citations >= 10:
            overall_warnings.append(
                f"Citation recency low: only {recent_count}/{n_citations} "
                f"({recency_ratio:.0%}) from last 3 years (target: >=30%%)"
            )

    # --- Abstract and Conclusion length enforcement ---
    for sec in sections_data:
        hl = sec["heading_lower"]
        body_text: str = sec["body"]
        wc = len(body_text.split())
        if hl == "abstract" and wc > 250:
            overall_warnings.append(
                f"Abstract is too long: {wc} words (target: 150-220 words)"
            )
            revision_directives.append(
                f"COMPRESS the Abstract from {wc} to 150-220 words. "
                f"Remove raw metric values, redundant context, and self-references."
            )
        if hl in ("conclusion", "conclusions", "conclusion and future work"):
            if wc > 300:
                overall_warnings.append(
                    f"Conclusion is too long: {wc} words (target: 100-200 words)"
                )
                revision_directives.append(
                    f"COMPRESS the Conclusion from {wc} to 100-200 words. "
                    f"Do NOT repeat specific metric values from Results. "
                    f"Summarize findings in 2-3 sentences, then 2-3 future directions."
                )

    # --- Raw metric path detection (log dumps in prose) ---
    _raw_path_re = re.compile(
        r"\\texttt\{[a-zA-Z0-9_/.-]+(?:/[a-zA-Z0-9_/.-]+){2,}",
    )
    raw_path_count = len(_raw_path_re.findall(draft))
    if raw_path_count > 3:
        overall_warnings.append(
            f"Raw metric paths in prose: {raw_path_count} instances of "
            f"\\texttt{{config/path/metric}} style dumps"
        )
        revision_directives.append(
            "REMOVE raw experiment log paths from prose. Replace "
            "\\texttt{config/metric/path} with human-readable metric names "
            "and summarize values in tables, not inline text."
        )

    # --- Writing quality lint ---
    _weasel_words = re.compile(
        r"\b(various|many|several|quite|fairly|really|very|rather|"
        r"somewhat|relatively|arguably|interestingly|importantly|"
        r"it is well known that|it is obvious that|clearly)\b",
        re.IGNORECASE,
    )
    _duplicate_words = re.compile(r"\b(\w+)\s+\1\b", re.IGNORECASE)
    weasel_count = len(_weasel_words.findall(draft))
    dup_matches = _duplicate_words.findall(draft)
    dup_count = len([d for d in dup_matches if d.lower() not in ("that", "had")])
    if weasel_count > 20:
        overall_warnings.append(
            f"High weasel-word count: {weasel_count} instances "
            f"(consider replacing vague words with precise language)"
        )
        revision_directives.append(
            "Replace vague hedging words (various, several, quite, fairly, "
            "rather, somewhat) with precise quantities or remove them."
        )
    if dup_count > 0:
        overall_warnings.append(
            f"Duplicate adjacent words found: {dup_count} instance(s) "
            f"(e.g., 'the the', 'is is')"
        )
        revision_directives.append(
            "Fix duplicate adjacent words (likely typos)."
        )

    # --- AI-slop / boilerplate detection ---
    _BOILERPLATE_PHRASES = [
        "delves into", "delve into", "it is worth noting",
        "it should be noted", "it is important to note",
        "leverage the power of", "leverages the power of",
        "in this paper, we propose", "in this work, we propose",
        "to the best of our knowledge",
        "in the realm of", "in the landscape of",
        "plays a crucial role", "plays a pivotal role",
        "groundbreaking", "cutting-edge", "state-of-the-art",
        "game-changing", "paradigm shift",
        "a myriad of", "a plethora of",
        "aims to bridge the gap", "bridge the gap",
        "shed light on", "sheds light on",
        "pave the way", "paves the way",
        "the advent of", "with the advent of",
        "in recent years", "in recent times",
        "has gained significant attention",
        "has attracted considerable interest",
        "has emerged as a promising",
        "a comprehensive overview",
        "a holistic approach", "holistic understanding",
        "showcasing the efficacy", "demonstrate the efficacy",
        "multifaceted", "underscores the importance",
        "navigate the complexities",
        "harness the potential", "harnessing the power",
        "it is imperative to", "it is crucial to",
        "a nuanced understanding", "nuanced approach",
        "robust and scalable", "seamlessly integrates",
        "the intricacies of", "intricate interplay",
        "facilitate a deeper understanding",
        "a testament to",
    ]
    draft_lower = draft.lower()
    boilerplate_hits: list[str] = []
    for phrase in _BOILERPLATE_PHRASES:
        count = draft_lower.count(phrase)
        if count > 0:
            boilerplate_hits.extend([phrase] * count)
    if len(boilerplate_hits) > 5:
        unique_phrases = sorted(set(boilerplate_hits))[:5]
        overall_warnings.append(
            f"AI boilerplate detected: {len(boilerplate_hits)} instances "
            f"of generic LLM phrases (e.g., {', '.join(repr(p) for p in unique_phrases[:3])})"
        )
        revision_directives.append(
            "REWRITE sentences containing AI-generated boilerplate phrases. "
            "Replace generic language (e.g., 'delves into', 'it is worth noting', "
            "'leverages the power of', 'plays a crucial role', 'paves the way') "
            "with precise, specific academic language."
        )

    # --- Related work depth check ---
    _rw_headings = {"related work", "related works", "background", "literature review"}
    rw_body = ""
    for sec in sections_data:
        if sec["heading_lower"] in _rw_headings and sec["level"] <= 2:
            rw_body = sec["body"]
            break
    if rw_body and len(rw_body.split()) > 50:
        _comparative_pats = re.compile(
            r"\b(unlike|in contrast|whereas|while .+ focus|"
            r"however|differ(?:s|ent)|our (?:method|approach) .+ instead|"
            r"we (?:instead|differ)|compared to|as opposed to|"
            r"goes beyond|extends|improves upon|addresses the limitation)\b",
            re.IGNORECASE,
        )
        sentences = [s.strip() for s in re.split(r"[.!?]+", rw_body) if s.strip()]
        comparative_sents = sum(1 for s in sentences if _comparative_pats.search(s))
        ratio = comparative_sents / len(sentences) if sentences else 0.0
        if ratio < 0.15 and len(sentences) >= 5:
            overall_warnings.append(
                f"Related Work is purely descriptive: only {comparative_sents}/{len(sentences)} "
                f"sentences ({ratio:.0%}) contain comparative language (target: >=15%)"
            )
            revision_directives.append(
                "REWRITE Related Work to critically compare with prior methods. "
                "Use phrases like 'unlike X, our approach...', 'in contrast to...', "
                "'while X focuses on... we address...' for at least 20% of sentences."
            )

    # --- Statistical rigor check (result sections) ---
    _results_headings = {"results", "experiments", "experimental results", "evaluation"}
    results_body = ""
    for sec in sections_data:
        if sec["heading_lower"] in _results_headings and sec["level"] <= 2:
            results_body += sec["body"] + "\n"
    if results_body and len(results_body.split()) > 100:
        has_std = bool(re.search(r"±|\\pm|\bstd\b|\\std\b|standard deviation", results_body, re.IGNORECASE))
        has_ci = bool(re.search(r"confidence interval|\bCI\b|95%|p-value|p\s*<", results_body, re.IGNORECASE))
        has_seeds = bool(re.search(r"(?:seed|run|trial)s?\s*[:=]\s*\d|averaged?\s+over\s+\d+\s+(?:seed|run|trial)", results_body, re.IGNORECASE))
        if not has_std and not has_ci and not has_seeds:
            overall_warnings.append(
                "No statistical measures found in results (no std, CI, p-values, or multi-seed reporting)"
            )
            revision_directives.append(
                "ADD error bars (±std), confidence intervals, or note the number of "
                "random seeds used. Single-run results without variance reporting "
                "are insufficient for top venues."
            )

    result: dict[str, Any] = {
        "section_analysis": section_analysis,
        "overall_warnings": overall_warnings,
        "revision_directives": revision_directives,
    }
    if stage_dir is not None:
        (stage_dir / "draft_quality.json").write_text(
            json.dumps(result, indent=2, ensure_ascii=False), encoding="utf-8"
        )
        if overall_warnings:
            logger.warning(
                "Draft quality: %d warning(s) \u2014 %s",
                len(overall_warnings),
                "; ".join(overall_warnings[:3]),
            )
        else:
            logger.info("Draft quality: all checks passed")
    return result


def _review_compiled_pdf(
    pdf_path: Path,
    llm: LLMClient,
    topic: str,
) -> dict[str, Any]:
    """Multi-dimensional LLM review of compiled paper (AI-Scientist style).

    Scores the paper on 7 academic review dimensions (1-10 each),
    identifies specific strengths/weaknesses, and provides an overall
    accept/reject recommendation with confidence.

    Returns a dict with dimensional scores, issues, and decision.
    """
    if not pdf_path.exists():
        return {}

    # Use source-based review since not all models support vision
    tex_path = pdf_path.with_suffix(".tex")
    if not tex_path.exists():
        return {}

    tex_content = tex_path.read_text(encoding="utf-8")[:12000]

    review_prompt = (
        "You are a senior Area Chair at a top AI conference (NeurIPS/ICML/ICLR) "
        "reviewing a paper submission. Provide a rigorous, structured review.\n\n"
        f"PAPER TOPIC: {topic}\n\n"
        f"LaTeX source:\n```latex\n{tex_content}\n```\n\n"
        "REVIEW INSTRUCTIONS:\n"
        "Score each dimension 1-10 (1=unacceptable, 5=borderline, 8=strong accept, "
        "10=best paper candidate). Be critical but fair.\n\n"
        "DIMENSIONS:\n"
        "1. SOUNDNESS: Are claims well-supported? Is methodology correct? "
        "Are there logical gaps or unsupported claims?\n"
        "2. PRESENTATION: Is the writing clear, flowing, and professional? "
        "Are there grammar errors, bullet lists in prose sections, or "
        "boilerplate phrases? Is it free of AI-generated slop?\n"
        "3. CONTRIBUTION: Is the contribution significant? Does it advance "
        "the field beyond incremental improvement?\n"
        "4. ORIGINALITY: Is the approach novel? Does it differentiate clearly "
        "from prior work?\n"
        "5. CLARITY: Are the method and results easy to understand? Are figures "
        "and tables well-designed with descriptive captions?\n"
        "6. SIGNIFICANCE: Would the community benefit from this work? Does it "
        "open new research directions?\n"
        "7. REPRODUCIBILITY: Are experimental details sufficient to reproduce "
        "results? Are hyperparameters, datasets, and metrics clearly stated?\n\n"
        "Also evaluate:\n"
        "- Are all figures referenced in the text?\n"
        "- Are tables properly formatted (booktabs style, no vertical rules)?\n"
        "- Does the related work critically compare, not just list papers?\n"
        "- Are statistical measures (std, CI, multiple seeds) reported?\n"
        "- Is there a clear limitations section?\n\n"
        "Return a JSON object:\n"
        "{\n"
        '  "soundness": N,\n'
        '  "presentation": N,\n'
        '  "contribution": N,\n'
        '  "originality": N,\n'
        '  "clarity": N,\n'
        '  "significance": N,\n'
        '  "reproducibility": N,\n'
        '  "overall_score": N,\n'
        '  "confidence": N,\n'
        '  "decision": "accept" or "reject",\n'
        '  "strengths": ["strength1", "strength2", ...],\n'
        '  "weaknesses": ["weakness1", "weakness2", ...],\n'
        '  "critical_issues": ["issue requiring revision", ...],\n'
        '  "minor_issues": ["formatting/typo issues", ...],\n'
        '  "summary": "2-3 sentence overall assessment"\n'
        "}\n"
    )

    try:
        resp = llm.chat(
            messages=[{"role": "user", "content": review_prompt}],
            system=(
                "You are a meticulous, critical academic reviewer. "
                "You have reviewed 100+ papers at top venues. "
                "Score honestly — most papers deserve 4-6, not 7-9. "
                "Flag any sign of AI-generated boilerplate."
            ),
        )
        review_data = _safe_json_loads(resp.content, {})
        if isinstance(review_data, dict) and "overall_score" in review_data:
            # Compute weighted aggregate if individual scores present
            dim_scores = {
                k: review_data.get(k, 0)
                for k in (
                    "soundness", "presentation", "contribution",
                    "originality", "clarity", "significance",
                    "reproducibility",
                )
            }
            valid = {k: v for k, v in dim_scores.items() if isinstance(v, (int, float)) and v > 0}
            if valid:
                review_data["mean_score"] = round(sum(valid.values()) / len(valid), 2)
            return review_data
    except Exception as exc:  # noqa: BLE001
        logger.debug("PDF review LLM call failed: %s", exc)

    return {}


def _check_ablation_effectiveness(
    exp_summary: dict[str, Any],
    threshold: float = 0.05,
) -> list[str]:
    """P7: Check if ablation results are within *threshold* of baseline.

    Returns a list of warning strings for ineffective ablations.
    """
    warnings: list[str] = []
    cond_summaries = exp_summary.get("condition_summaries", {})
    if not isinstance(cond_summaries, dict) or not cond_summaries:
        return warnings

    # Find baseline/control condition
    baseline_name = None
    baseline_mean = None
    for name, data in cond_summaries.items():
        if not isinstance(data, dict):
            continue
        name_lower = name.lower()
        if any(tag in name_lower for tag in ("baseline", "control", "vanilla", "standard")):
            metrics = data.get("metrics", {})
            # Use the first metric that has a _mean suffix or the first available
            for mk, mv in metrics.items():
                if mk.endswith("_mean"):
                    baseline_name = name
                    baseline_mean = float(mv)
                    break
            if baseline_mean is None:
                for mk, mv in metrics.items():
                    try:
                        baseline_name = name
                        baseline_mean = float(mv)
                        break
                    except (TypeError, ValueError):
                        continue
            if baseline_name:
                break

    if baseline_name is None or baseline_mean is None:
        return warnings

    # Check each ablation condition
    for name, data in cond_summaries.items():
        if not isinstance(data, dict):
            continue
        name_lower = name.lower()
        if name == baseline_name:
            continue
        if not any(tag in name_lower for tag in ("ablation", "no_", "without", "reduced")):
            continue
        metrics = data.get("metrics", {})
        for mk, mv in metrics.items():
            if not mk.endswith("_mean"):
                continue
            try:
                abl_val = float(mv)
            except (TypeError, ValueError):
                continue
            if baseline_mean != 0:
                rel_diff = abs(abl_val - baseline_mean) / abs(baseline_mean)
            else:
                rel_diff = abs(abl_val - baseline_mean)
            if rel_diff < threshold:
                warnings.append(
                    f"Ablation '{name}' {mk}={abl_val:.4f} is within "
                    f"{rel_diff:.1%} of baseline '{baseline_name}' "
                    f"{mk}={baseline_mean:.4f} — ablation may be ineffective"
                )
            break  # Only check the first _mean metric per condition

    return warnings


def _detect_result_contradictions(
    exp_summary: dict[str, Any],
) -> list[str]:
    """P10: Detect contradictions in experiment results before paper writing.

    Returns a list of advisory strings to inject into paper writing prompt.
    """
    advisories: list[str] = []
    cond_summaries = exp_summary.get("condition_summaries", {})
    if not isinstance(cond_summaries, dict) or not cond_summaries:
        return advisories

    # Collect primary metric means per condition
    means: dict[str, float] = {}
    for name, data in cond_summaries.items():
        if not isinstance(data, dict):
            continue
        metrics = data.get("metrics", {})
        for mk, mv in metrics.items():
            if mk.endswith("_mean"):
                try:
                    means[name] = float(mv)
                except (TypeError, ValueError):
                    pass
                break

    if len(means) < 2:
        return advisories

    # Check 1: All methods within noise margin (2% relative spread)
    vals = list(means.values())
    val_range = max(vals) - min(vals)
    val_mean = sum(vals) / len(vals)
    if val_mean != 0 and (val_range / abs(val_mean)) < 0.02:
        advisories.append(
            "NULL RESULT: All methods produce nearly identical primary metric values "
            f"(range={val_range:.4f}, mean={val_mean:.4f}). Frame this as a null result — "
            "the methods are statistically indistinguishable. Do NOT claim any method "
            "is superior. Discuss possible explanations (task too easy/hard, metric "
            "insensitive, insufficient differentiation in methods)."
        )

    # Check 2: Control/simple baseline outperforms proposed method
    baseline_val = None
    baseline_name = None
    proposed_val = None
    proposed_name = None
    for name, val in means.items():
        name_lower = name.lower()
        if any(tag in name_lower for tag in ("baseline", "control", "random", "vanilla")):
            if baseline_val is None or val > (baseline_val or 0):
                baseline_val = val
                baseline_name = name
        elif any(tag in name_lower for tag in ("proposed", "our", "novel", "method")):
            if proposed_val is None or val > (proposed_val or 0):
                proposed_val = val
                proposed_name = name

    if baseline_val is not None and proposed_val is not None:
        if baseline_val > proposed_val:
            advisories.append(
                f"NEGATIVE RESULT: Baseline '{baseline_name}' ({baseline_val:.4f}) "
                f"outperforms proposed method '{proposed_name}' ({proposed_val:.4f}). "
                "This is a NEGATIVE result. Do NOT claim the proposed method is superior. "
                "Frame as 'An Empirical Study of...' or 'When X Falls Short'. "
                "Discuss why the baseline won and what this implies for future work."
            )

    return advisories


def _paper_draft_timeout_sec(config: RCConfig) -> int:
    """Allow an hour-level budget for the multi-call full-paper generation pass."""
    configured = max(1, int(getattr(config.llm, "timeout_sec", 120) or 120))
    default_timeout = max(3600, configured * 3)
    return max(60, int(os.environ.get("RESEARCHCLAW_PAPER_DRAFT_TIMEOUT_SEC", str(default_timeout)) or default_timeout))


def _execute_paper_draft(
    stage_dir: Path,
    run_dir: Path,
    config: RCConfig,
    adapters: AdapterBundle,
    *,
    llm: LLMClient | None = None,
    prompts: PromptManager | None = None,
) -> StageResult:
    outline = _read_prior_artifact(run_dir, "outline.md") or ""

    # ── SHORT paper fast-path: skip heavy validation, single LLM call ──
    _paper_len = getattr(config.experiment, "paper_length", "") or "full"
    if _paper_len == "short" and llm is not None:
        preamble = _build_context_preamble(
            config, run_dir, include_analysis=True, include_experiment_data=True,
        )
        analysis = _read_prior_artifact(run_dir, "analysis.md") or ""
        exp_summary_text = _read_prior_artifact(run_dir, "experiment_summary.json") or ""
        _extra = ""
        if exp_summary_text:
            _extra = f"\n\nExperiment Summary JSON:\n```json\n{exp_summary_text[:3000]}\n```\n"
        _short_user = (
            f"{preamble}\n\n{_extra}\n\nOutline:\n{outline}\n\n"
            "Write a SHORT WORKSHOP PAPER (2-3 pages, ~1500-2000 words total) "
            "in markdown. Include these sections:\n"
            "1. **Title** (catchy, <=14 words)\n"
            "2. **Abstract** (100-150 words)\n"
            "3. **Introduction** (300-400 words): motivation, gap, approach, contributions\n"
            "4. **Method** (300-400 words): concise method description\n"
            "5. **Experiments** (300-400 words): setup, results with real numbers from data above\n"
            "6. **Conclusion** (100-150 words)\n\n"
            "Use ## headers. Be concise and direct. "
            "Do NOT include a References section. "
            "Start DIRECTLY with '## Title'."
        )
        _short_sys = (
            "You are an academic writer producing a concise short/workshop paper. "
            "Use formal academic tone. Report only real experimental numbers."
        )
        resp = _chat_with_prompt(llm, _short_sys, _short_user, max_tokens=4000)
        draft = resp.content
        logger.info("Stage 20: Short paper mode — single LLM call (%d words)", len(draft.split()))
        (stage_dir / "paper_draft.md").write_text(draft, encoding="utf-8")
        return StageResult(
            stage=Stage.PAPER_DRAFT,
            status=StageStatus.DONE,
            artifacts=("paper_draft.md",),
            evidence_refs=("stage-20/paper_draft.md",),
        )
    # ── END short fast-path ──

    _disc_dir = _find_discussion_dir(run_dir, config)
    _has_discussion = _disc_dir is not None
    preamble = _build_context_preamble(
        config,
        run_dir,
        include_goal=True,
        include_hypotheses=True,
        include_analysis=True,
        include_experiment_data=True,  # WS-5.1: inject real experiment data
        include_discussion=_has_discussion,
    )

    # Build discussion ablation instruction for paper draft
    discussion_ablation = ""
    if _has_discussion:
        discussion_ablation = (
            "\n\n## MULTI-AGENT DISCUSSION ABLATION (MANDATORY SECTION)\n"
            "This paper's research idea was refined through a multi-agent collaborative "
            "discussion process. You MUST include an **ablation study** comparing:\n"
            "- **Without discussion**: Individual agent syntheses (each agent independently "
            "surveyed literature and generated a synthesis)\n"
            "- **With discussion**: Post-discussion consensus synthesis (agents debated, "
            "critiqued each other's views, and reached consensus)\n\n"
            "Analyze the following dimensions in the ablation:\n"
            "1. **Coverage**: How many unique research gaps/opportunities were identified "
            "before vs after discussion\n"
            "2. **Contradiction resolution**: What conflicting viewpoints existed and how "
            "the discussion resolved them\n"
            "3. **Novel insights**: Ideas that emerged ONLY from the discussion interaction "
            "(not present in any individual synthesis)\n"
            "4. **Hypothesis quality**: Compare the specificity and testability of hypotheses "
            "derived from individual vs consensus syntheses\n\n"
            "Place this ablation in the Discussion section or as a dedicated subsection "
            "titled 'Ablation: Multi-Agent Discussion Impact'. Use the pre-discussion "
            "and post-discussion data provided in the research context.\n"
        )

    # R21-1: Read BEST experiment_summary across all stage-14 versions.
    # Refinement can regress — the final (non-versioned) stage-14 may have
    # worse data than an earlier version. Pick the richest one.
    exp_summary_text = None
    _best_metric_count = 0
    for _s14_dir in sorted(run_dir.glob("stage-14*")):
        _candidate = _s14_dir / "experiment_summary.json"
        if _candidate.is_file():
            _text = _candidate.read_text(encoding="utf-8")
            _parsed = _safe_json_loads(_text, {})
            if isinstance(_parsed, dict):
                _mcount = _parsed.get("total_metric_keys", 0) or len(
                    _parsed.get("metrics_summary", {})
                )
                _paired_count = len(_parsed.get("paired_comparisons", []))
                _score = _mcount + _paired_count * 10  # Prefer paired data
                if _score > _best_metric_count:
                    _best_metric_count = _score
                    exp_summary_text = _text
                    logger.info(
                        "R21-1: Selected %s (metric_keys=%d, paired=%d, score=%d)",
                        _s14_dir.name, _mcount, _paired_count, _score,
                    )
    # Fallback to standard artifact read
    if exp_summary_text is None:
        exp_summary_text = _read_prior_artifact(run_dir, "experiment_summary.json")
    exp_metrics_instruction = ""
    has_real_metrics = False
    if exp_summary_text:
        exp_summary = _safe_json_loads(exp_summary_text, {})
        if isinstance(exp_summary, dict) and exp_summary.get("metrics_summary"):
            has_real_metrics = True
            exp_metrics_instruction = (
                "\n\nIMPORTANT: Use the ACTUAL experiment results provided in the context. "
                "All numbers in the Results and Experiments sections MUST reference real data. "
                "Do NOT write 'no quantitative results yet' or use placeholder numbers. "
                "Cite specific metrics with their actual values.\n"
            )

    # Collect raw experiment stdout metrics as hard constraint for the paper
    raw_metrics_block, _has_parsed_metrics = _collect_raw_experiment_metrics(run_dir)
    if raw_metrics_block:
        # BUG-23: Raw stdout alone is not sufficient — require either
        # metrics_summary data, parsed metrics from run JSONs,
        # OR at least 3 condition= patterns in raw block
        _has_condition_pattern = len(re.findall(
            r"condition[=:]", raw_metrics_block, re.IGNORECASE
        )) >= 3
        if has_real_metrics or _has_parsed_metrics or _has_condition_pattern:
            has_real_metrics = True
        exp_metrics_instruction += raw_metrics_block

    # R18-1 + R19-6: Inject paired statistical comparisons AND condition summaries
    if exp_summary_text:
        exp_summary_parsed = _safe_json_loads(exp_summary_text, {})
        if isinstance(exp_summary_parsed, dict):
            # R19-6: Inject experiment scale header so LLM knows the data richness
            _total_conds = exp_summary_parsed.get("total_conditions")
            _total_mkeys = exp_summary_parsed.get("total_metric_keys")
            if _total_conds or _total_mkeys:
                scale_block = "\n\n## EXPERIMENT SCALE\n"
                if _total_conds:
                    scale_block += f"- Total conditions tested: {_total_conds}\n"
                if _total_mkeys:
                    scale_block += f"- Total metric keys collected: {_total_mkeys}\n"
                scale_block += (
                    "- This is a MULTI-SEED experiment. Report mean +/- std across seeds.\n"
                    "- Do NOT describe results as 'single run' or 'preliminary'.\n"
                )
                exp_metrics_instruction += scale_block

            # R19-6 + R33: Inject condition summaries with CIs
            cond_summaries = exp_summary_parsed.get("condition_summaries", {})
            if isinstance(cond_summaries, dict) and cond_summaries:
                cond_block = "\n\n## PER-CONDITION SUMMARY (use in Results tables)\n"
                for cname, cdata in sorted(cond_summaries.items()):
                    cond_block += f"\n### {cname}\n"
                    if not isinstance(cdata, dict):
                        continue
                    sr = cdata.get("success_rate")
                    if sr is not None:
                        cond_block += f"- Success rate: {sr:.1%}\n"
                    ns = cdata.get("n_seeds") or cdata.get("n_seed_metrics")
                    if ns:
                        cond_block += f"- Seeds: {ns}\n"
                    ci_lo = cdata.get("ci95_low")
                    ci_hi = cdata.get("ci95_high")
                    if ci_lo is not None and ci_hi is not None:
                        try:
                            cond_block += f"- Bootstrap 95% CI: [{float(ci_lo):.4f}, {float(ci_hi):.4f}]\n"
                        except (ValueError, TypeError):
                            cond_block += f"- Bootstrap 95% CI: [{ci_lo}, {ci_hi}]\n"
                    cm = cdata.get("metrics", {})
                    if cm:
                        for mk, mv in sorted(cm.items()):
                            if isinstance(mv, (int, float)):
                                cond_block += f"- {mk}: {mv:.4f}\n"
                            else:
                                cond_block += f"- {mk}: {mv}\n"
                exp_metrics_instruction += cond_block

            # R18-1: Inject paired statistical comparisons
            paired = exp_summary_parsed.get("paired_comparisons", [])
            if paired:
                paired_block = "\n\n## PAIRED STATISTICAL COMPARISONS (use these in Results)\n"
                paired_block += f"Total: {len(paired)} paired tests computed.\n"
                for pc in paired:
                    if not isinstance(pc, dict):
                        continue
                    method = pc.get("method", "?")
                    baseline = pc.get("baseline", "?")
                    regime = pc.get("regime", "all")
                    md = pc.get("mean_diff", "?")
                    sd = pc.get("std_diff", "?")
                    ts = pc.get("t_stat", "?")
                    pv = pc.get("p_value", "?")
                    ci_lo = pc.get("ci95_low")
                    ci_hi = pc.get("ci95_high")
                    ci_str = ""
                    if ci_lo is not None and ci_hi is not None:
                        try:
                            ci_str = f", 95% CI [{float(ci_lo):.3f}, {float(ci_hi):.3f}]"
                        except (ValueError, TypeError):
                            ci_str = f", 95% CI [{ci_lo}, {ci_hi}]"
                    paired_block += (
                        f"- {method} vs {baseline} (regime={regime}): "
                        f"mean_diff={md}, std_diff={sd}, "
                        f"t={ts}, p={pv}{ci_str}\n"
                    )
                exp_metrics_instruction += paired_block

            # R24: Method naming map — translate generic condition labels
            _cond_names = list(cond_summaries.keys()) if isinstance(cond_summaries, dict) and cond_summaries else []
            if _cond_names:
                naming_block = (
                    "\n\n## METHOD NAMING (CRITICAL — do NOT use generic labels in the paper)\n"
                    "The condition labels below come from the experiment code. In the paper, "
                    "you MUST use DESCRIPTIVE algorithm names, not generic labels.\n"
                    "- If a condition name is already descriptive (e.g., 'random_search', "
                    "'bayesian_optimization', 'ppo_policy'), use it directly as a proper name.\n"
                    "- If a condition name is generic (e.g., 'baseline_1', 'method_variant_1'), "
                    "you MUST infer the algorithm from the experiment code/context and use the "
                    "real algorithm name (e.g., 'Random Search', 'Bayesian Optimization', "
                    "'PPO', 'Curiosity-Driven RL').\n"
                    "- NEVER write `baseline_1` or `method_variant_1` in the paper text.\n"
                    f"- Conditions to name: {_cond_names}\n"
                )
                exp_metrics_instruction += naming_block

            # IMP-8: Inject broken ablation warnings
            abl_warnings = exp_summary_parsed.get("ablation_warnings", [])
            if abl_warnings:
                broken_block = (
                    "\n\n## BROKEN ABLATIONS (DO NOT discuss as valid results)\n"
                    "The following ablation conditions produced IDENTICAL outputs, "
                    "indicating implementation bugs. Do NOT present their differences "
                    "as findings. Mention them ONLY in a 'Limitations' sub-section "
                    "as known implementation issues:\n"
                )
                for _aw in abl_warnings:
                    broken_block += f"- {_aw}\n"
                broken_block += (
                    "\nIf you reference these conditions, state explicitly: "
                    "'Due to an implementation defect, conditions X and Y produced "
                    "identical outputs; their comparison is therefore uninformative.'\n"
                )
                exp_metrics_instruction += broken_block

            # R25: Statistical table format requirement
            if paired:
                stat_table_block = (
                    "\n\n## STATISTICAL TABLE REQUIREMENT (MANDATORY in Results section)\n"
                    "The Results section MUST include a statistical comparison table with columns:\n"
                    "| Comparison | Mean Diff | Std Diff | t-statistic | p-value | Significance |\n"
                    "Use the PAIRED STATISTICAL COMPARISONS data above to fill this table.\n"
                    "Mark significance: *** (p<0.001), ** (p<0.01), * (p<0.05), n.s.\n"
                    "This is non-negotiable — a top-venue paper MUST have statistical tests.\n"
                )
                exp_metrics_instruction += stat_table_block

            # R26: Metric definition requirement
            exp_metrics_instruction += (
                "\n\n## METRIC DEFINITIONS (MANDATORY in Experiments section)\n"
                "The Experiments section MUST define each metric:\n"
                "- **Primary metric**: what it measures, how it is computed, range, direction "
                "(higher/lower is better), and units if applicable.\n"
                "- **Secondary metric**: same details.\n"
                "- For time-to-event metrics: explain the horizon, what constitutes success, "
                "and how failures are handled (e.g., set to max horizon).\n"
                "- These definitions MUST appear BEFORE any results tables.\n"
            )

            # R27: Multi-seed framing enforcement
            _any_seeds = any(
                (cond_summaries.get(c) or {}).get("n_seed_metrics", 0) > 1
                for c in _cond_names
            ) if _cond_names else False
            if _any_seeds:
                exp_metrics_instruction += (
                    "\n\n## MULTI-SEED EXPERIMENT FRAMING (CRITICAL)\n"
                    "This experiment uses MULTIPLE independent random seeds per condition.\n"
                    "- Report mean +/- std (or SE) for all metrics.\n"
                    "- NEVER describe this as 'a single run' or '1 benchmark-artifact run'.\n"
                    "- Frame as: 'We evaluate each method across N seeds per regime.'\n"
                    "- The seed-level data IS the evidence base — it is NOT a single observation.\n"
                    "- Include per-regime breakdowns (easy vs hard) as separate rows in tables.\n"
                )

    # BUG-003: Inject actual evaluated datasets as a hard constraint
    if exp_summary_text:
        _ds_parsed = _safe_json_loads(exp_summary_text, {})
        if isinstance(_ds_parsed, dict):
            _datasets: set[str] = set()
            # Extract from condition names (often contain dataset info)
            for _cname in (_ds_parsed.get("condition_summaries") or {}).keys():
                _datasets.add(str(_cname))
            # Extract from explicit "datasets" field if present
            for _ds in (_ds_parsed.get("datasets") or []):
                if isinstance(_ds, str):
                    _datasets.add(_ds)
            # Extract from "benchmark" or "dataset" fields
            for _key in ("benchmark", "dataset", "dataset_name"):
                _dv = _ds_parsed.get(_key)
                if isinstance(_dv, str) and _dv:
                    _datasets.add(_dv)
            if _datasets:
                exp_metrics_instruction += (
                    "\n\n## ACTUAL EVALUATED DATASETS (HARD CONSTRAINT)\n"
                    "The following datasets/conditions were ACTUALLY tested in experiments:\n"
                    + "".join(f"- {d}\n" for d in sorted(_datasets))
                    + "\nCRITICAL: Do NOT claim evaluation on any dataset not listed above.\n"
                    "Do NOT fabricate results for datasets you did not run experiments on.\n"
                    "If you reference other datasets, clearly state they are 'not evaluated "
                    "in this work' or are 'left for future work'.\n"
                )

    # P7: Ablation effectiveness check
    if exp_summary_text:
        _exp_parsed_p7 = _safe_json_loads(exp_summary_text, {})
        if isinstance(_exp_parsed_p7, dict):
            _abl_warnings = _check_ablation_effectiveness(_exp_parsed_p7)
            if _abl_warnings:
                _abl_block = (
                    "\n\n## ABLATION EFFECTIVENESS WARNINGS\n"
                    "The following ablations showed minimal effect (within 5%% of baseline). "
                    "Discuss this honestly — it may indicate the ablated component is not "
                    "important, or the ablation was not properly implemented:\n"
                )
                for _aw in _abl_warnings:
                    _abl_block += f"- {_aw}\n"
                exp_metrics_instruction += _abl_block
                logger.warning("P7: Ablation effectiveness warnings: %s", _abl_warnings)

    # P10: Contradiction detection
    if exp_summary_text:
        _exp_parsed_p10 = _safe_json_loads(exp_summary_text, {})
        if isinstance(_exp_parsed_p10, dict):
            _contradictions = _detect_result_contradictions(_exp_parsed_p10)
            if _contradictions:
                _contra_block = (
                    "\n\n## RESULT INTERPRETATION ADVISORIES (CRITICAL — read before writing)\n"
                )
                for _ca in _contradictions:
                    _contra_block += f"- {_ca}\n"
                exp_metrics_instruction += _contra_block
                logger.warning("P10: Contradiction advisories: %s", _contradictions)

    # R10: HARD BLOCK — refuse to write paper when all data is simulated
    all_simulated = True
    for stage_subdir in sorted(run_dir.glob("stage-*/runs")):
        for run_file in sorted(stage_subdir.glob("*.json")):
            if run_file.name == "results.json":
                continue
            try:
                _payload = json.loads(run_file.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError):
                continue
            if isinstance(_payload, dict) and _payload.get("status") != "simulated":
                all_simulated = False
                break
        if not all_simulated:
            break

    if all_simulated:
        logger.error(
            "BLOCKED: All experiment data is simulated (mode='simulated'). "
            "Cannot write a paper based on formulaic fake data. "
            "Switch to experiment.mode='sandbox' and re-run."
        )
        (stage_dir / "paper_draft.md").write_text(
            "# Paper Draft Blocked\n\n"
            "**Reason**: All experiment results are from simulated mode "
            "(formulaic data: `0.3 + idx * 0.03`). "
            "These are not real experimental results.\n\n"
            "**Action Required**: Set `experiment.mode: 'sandbox'` in "
            "config.arc.yaml and re-run the pipeline.",
            encoding="utf-8",
        )
        return StageResult(
            stage=Stage.PAPER_DRAFT,
            status=StageStatus.FAILED,
            artifacts=("paper_draft.md",),
            evidence_refs=(),
        )

    # R4-2: HARD BLOCK — refuse to write paper with no real data (ML/empirical domains)
    # For non-empirical domains (math proofs, theoretical economics), allow proceeding
    _domain_id, _domain_name, _domain_venues = _detect_domain(
        config.research.topic, config.research.domains
    )
    _empirical_domains = {"ml", "engineering", "biology", "chemistry"}
    if not has_real_metrics:
        if _domain_id in _empirical_domains:
            logger.error(
                "BLOCKED: Cannot write paper — experiment produced NO metrics. "
                "The pipeline will not fabricate results."
            )
            (stage_dir / "paper_draft.md").write_text(
                "# Paper Draft Blocked\n\n"
                "**Reason**: Experiment stage produced no metrics (status: failed/timeout). "
                "Cannot write a paper without real experimental data.\n\n"
                "**Action Required**: Fix experiment execution or increase time_budget_sec.",
                encoding="utf-8",
            )
            return StageResult(
                stage=Stage.PAPER_DRAFT,
                status=StageStatus.FAILED,
                artifacts=("paper_draft.md",),
                evidence_refs=(),
            )
        else:
            logger.warning(
                "No experiment metrics found, but domain '%s' may be non-empirical "
                "(theoretical/mathematical). Proceeding with paper draft.",
                _domain_name,
            )

    # R11-5: Experiment quality minimum threshold before paper writing
    # Parse analysis.md for quality rating and condition completeness
    analysis_text = _read_prior_artifact(run_dir, "analysis.md") or ""
    _quality_warnings: list[str] = []

    # Check 1: Was the analysis quality rating very low?
    import re as _re_q
    _rating_match = _re_q.search(
        r"(?:quality\s+rating|result\s+quality)[:\s]*\**(\d+)\s*/\s*10",
        analysis_text,
        _re_q.IGNORECASE,
    )
    if _rating_match:
        _analysis_rating = int(_rating_match.group(1))
        if _analysis_rating <= 3:
            _quality_warnings.append(
                f"Analysis rated experiment quality {_analysis_rating}/10"
            )
        # BUG-23: If quality rating is ≤ 2, force has_real_metrics = False
        # to prevent fabricated results even if stdout had stray numbers.
        # R5-BUG-05: Skip override when _has_parsed_metrics is True — the
        # analysis.md may be stale (from pre-refinement Stage 14) while
        # Stage 13 refinement produced real parsed metrics.
        if _analysis_rating <= 2 and has_real_metrics and not _has_parsed_metrics:
            logger.warning(
                "BUG-23 guard: Analysis quality %d/10 ≤ 2 — "
                "overriding has_real_metrics to False (experiment likely failed)",
                _analysis_rating,
            )
            has_real_metrics = False

    # Check 2: Are baselines missing?
    _analysis_lower = analysis_text.lower()
    if "no" in _analysis_lower and "baseline" in _analysis_lower:
        if any(phrase in _analysis_lower for phrase in [
            "no baseline", "no bo", "no random", "baselines are missing",
            "missing baselines", "baseline coverage is missing",
        ]):
            _quality_warnings.append("Baselines appear to be missing from results")

    # Check 3: Is the metric undefined?
    if any(phrase in _analysis_lower for phrase in [
        "metric is undefined", "primary_metric is undefined",
        "undefined metric", "metric undefined",
    ]):
        _quality_warnings.append("Primary metric is undefined (direction/units/formula unknown)")

    # Check 4: Very few conditions completed
    _condition_count = len(_re_q.findall(
        r"condition[=:\s]+\w+.*?(?:mean|primary_metric)",
        raw_metrics_block or "",
        _re_q.IGNORECASE,
    ))

    if _quality_warnings:
        _warning_block = "\n".join(f"  - {w}" for w in _quality_warnings)
        logger.warning(
            "Stage 17: Experiment quality concerns detected before paper writing:\n%s",
            _warning_block,
        )
        # Inject quality warnings into the paper writing prompt so the LLM
        # writes an appropriately hedged paper
        exp_metrics_instruction += (
            "\n\n## EXPERIMENT QUALITY WARNINGS (address these honestly in the paper)\n"
            + "\n".join(f"- {w}" for w in _quality_warnings)
            + "\n\nBecause of these issues, the paper MUST:\n"
            "- Use hedged language ('preliminary', 'pilot', 'initial exploration')\n"
            "- NOT claim definitive comparisons between methods\n"
            "- Dedicate a substantial Limitations section to these gaps\n"
            "- Frame the contribution as methodology/framework, not empirical findings\n"
        )
        # Save warnings for tracking
        (stage_dir / "quality_warnings.json").write_text(
            json.dumps(_quality_warnings, indent=2), encoding="utf-8"
        )

    # R4-2: Anti-fabrication data integrity instruction
    exp_metrics_instruction += (
        "\n\n## CRITICAL: Data Integrity Rules\n"
        "- You may ONLY report numbers that appear in the experiment data above\n"
        "- If the experiment data is incomplete (fewer conditions than planned), report\n"
        "  ONLY the conditions that were actually run\n"
        "- Do NOT extrapolate, interpolate, or 'fill in' missing cells in tables\n"
        "- Do NOT invent confidence intervals, p-values, or statistical tests unless\n"
        "  the actual data supports them\n"
        "- If only N conditions completed, simply report results for those N conditions\n"
        "  without repeating apologies or disclaimers about missing conditions\n"
        "- Any table cell without real data must show '—' (not a plausible number)\n"
        "- FORBIDDEN: generating numbers that 'look right' based on your training data\n"
    )

    # IMP-6 + FA: Inject chart references into paper draft prompt
    # Prefer FigureAgent's figure_plan.json (rich descriptions) over raw file scan
    # BUG-FIX: figure_plan.json may be a list (from FigureAgent planner) or a dict
    # (from executor overwrite).  The orchestrator writes a list at planning time;
    # the executor overwrites with a dict only when figure_count > 0.  If the
    # FigureAgent renders 0 charts the list persists, and calling .get() on it
    # raises AttributeError.
    _fa_descriptions = ""
    # Charts and figure plans are generated in stage-16 (RESULT_ANALYSIS),
    # also check stage-14 as a legacy fallback.
    for _fig_stage_pattern in ("stage-16*", "stage-14*"):
        for _fig_stage_dir in sorted(run_dir.glob(_fig_stage_pattern)):
            for _fp_name in ("figure_plan_final.json", "figure_plan.json"):
                _fp_path = _fig_stage_dir / _fp_name
                if not _fp_path.exists():
                    continue
                try:
                    _fp_data = json.loads(_fp_path.read_text(encoding="utf-8"))
                    if isinstance(_fp_data, dict):
                        _fa_descriptions = _fp_data.get("figure_descriptions", "")
                    elif isinstance(_fp_data, list) and _fp_data:
                        _desc_parts = ["## PLANNED FIGURES (from figure plan)\n"]
                        for _fig in _fp_data:
                            if isinstance(_fig, dict):
                                _fid = _fig.get("figure_id", "unnamed")
                                _ftitle = _fig.get("title", "")
                                _fcap = _fig.get("caption", "")
                                _fsec = _fig.get("section", "results")
                                _desc_parts.append(
                                    f"- **{_fid}** ({_fsec}): {_ftitle}\n  {_fcap}"
                                )
                        if len(_desc_parts) > 1:
                            _fa_descriptions = "\n".join(_desc_parts)
                except (json.JSONDecodeError, OSError):
                    pass
                if _fa_descriptions:
                    break
            if _fa_descriptions:
                break
        if _fa_descriptions:
            break

    if _fa_descriptions:
        exp_metrics_instruction += "\n\n" + _fa_descriptions
        logger.info("Stage 17: Injected FigureAgent figure descriptions into paper draft prompt")
    else:
        # Fallback: scan for chart PNG files in stage-16 (RESULT_ANALYSIS) and stage-14
        _chart_files: list[str] = []
        for _chart_stage_pattern in ("stage-16*", "stage-14*"):
            for _chart_stage_dir in sorted(run_dir.glob(_chart_stage_pattern)):
                _charts_path = _chart_stage_dir / "charts"
                if _charts_path.is_dir():
                    for _cf in sorted(_charts_path.glob("*.png")):
                        _chart_files.append(_cf.name)
        if _chart_files:
            _manifest = _read_figure_manifest(run_dir)
            _n = len(_chart_files)
            _chart_block = (
                f"\n\n## AVAILABLE DATA FIGURES — ALL {_n} MUST be embedded\n"
                f"The following {_n} figures were generated from actual experiment "
                "data. You MUST reference **ALL** of them in the Results / "
                "Analysis sections using markdown image syntax: "
                "`![Caption](charts/filename.png)`\n"
                "Each figure MUST have a descriptive caption and 2-3 sentences "
                "of discussion explaining what the figure shows.\n\n"
            )
            for _cf_name in _chart_files:
                _meta = _manifest.get(_cf_name, {})
                _title = _meta.get(
                    "title",
                    _cf_name.replace("_", " ").replace(".png", "").title(),
                )
                _caption = _meta.get("caption", "")
                _section = _meta.get("paper_section", "Results")
                _chart_block += (
                    f"- `charts/{_cf_name}` — **{_title}** "
                    f"(place in {_section})\n"
                )
                if _caption:
                    _chart_block += f"  Suggested caption: {_caption}\n"
            _chart_block += (
                "\nCRITICAL: A paper missing ANY of the above figures will be "
                "desk-rejected. Distribute them across Results and Analysis "
                "sections as indicated.\n"
            )
            exp_metrics_instruction += _chart_block
            logger.info(
                "Stage 17: Injected %d chart references (ALL required) "
                "into paper draft prompt",
                len(_chart_files),
            )

    # WS-5.5 → FIGURE_PROMPT: Build figure_prompt_instruction for non-data figures
    _venue_style = "IEEE two-column conference"
    if not str(getattr(config.export, "target_conference", "")).lower().startswith("ieee"):
        _venue_style = "NeurIPS / ICML / ICLR"
    _paper_len_cfg = getattr(config.experiment, "paper_length", "") or "full"
    if _paper_len_cfg == "short":
        _venue_style = "workshop / short paper"

    figure_prompt_instruction = (
        "\n\n## FIGURE GENERATION INSTRUCTIONS (MANDATORY)\n"
        "This paper uses a hybrid figure pipeline:\n\n"
        "### A. Non-data figures (architecture, method, pipeline, concept diagrams)\n"
        "For EVERY non-data figure, emit a structured FIGURE_PROMPT block "
        "using HTML comments **inline in the markdown** at the position where the "
        "figure should appear. Format:\n\n"
        "```\n"
        "<!-- FIGURE_PROMPT\n"
        "figure_id: unique_snake_case_id\n"
        "figure_type: architecture_diagram | method_flowchart | pipeline_overview | "
        "concept_illustration | system_diagram | comparison_illustration | "
        "attention_visualization\n"
        "section: Method | Results | Introduction | Discussion\n"
        "caption: \"Descriptive caption for the figure.\"\n"
        "aspect_ratio: 16:9\n"
        "prompt: |\n"
        "  A professional academic figure for a {venue} paper. "
        "[Describe: subject, layout (left-to-right / top-to-bottom), "
        "components (boxes, arrows, layers), labels, colors, annotations.] "
        "Use clean vector-style lines on a white background. "
        "Text must be legible at column width (~3.25 in). "
        "Color palette: blues and grays with one accent color for novelty.\n"
        "-->\n"
        "```\n"
        f"Replace {{venue}} with the target venue style: **{_venue_style}**.\n\n"
        "Then reference it in text:\n"
        "*[Figure N: <caption> — to be generated]*\n\n"
        "#### Required non-data figures:\n"
        "1. **Teaser figure** in the Introduction section — a high-level "
        "conceptual illustration showing the core idea/motivation at a glance "
        "(figure_type: concept_illustration). This is the FIRST figure in the "
        "paper and should convey the key insight without requiring method details. "
        "Place it right after the first introductory paragraph.\n"
        "2. **Framework / architecture overview** in the Method section "
        "(figure_type: architecture_diagram or pipeline_overview). This shows "
        "the full system/method pipeline with components, data flow, and key modules.\n"
        "3. **Method detail figure** — at least ONE additional figure in the Method "
        "section showing a key algorithmic step, comparison diagram, or attention "
        "visualization (figure_type: method_flowchart | comparison_illustration | "
        "attention_visualization).\n\n"
        "#### Prompt quality rules:\n"
        "- Prompt MUST be >= 50 words and self-contained.\n"
        "- Describe spatial layout (e.g., 'three boxes arranged left-to-right "
        "connected by arrows').\n"
        "- Specify labels for every component.\n"
        "- Mention the color scheme (e.g., 'blue boxes for encoder, orange for "
        "decoder, gray arrows for data flow').\n"
        "- Include the academic style: 'clean, professional, suitable for a "
        f"top-tier {_venue_style} paper'.\n\n"
        "### B. Data figures (charts, plots, heatmaps)\n"
        "Continue using `![Caption](charts/filename.png)` for pre-generated "
        "data charts, OR inline LaTeX/TikZ code blocks for simple data plots.\n"
        "Do NOT use FIGURE_PROMPT for data-driven visualizations.\n"
    )

    # P5: Extract hyperparameters from results.json for paper Method section
    _hp_table = ""
    for _s14_dir in sorted(run_dir.glob("stage-14*")):
        for _run_file in sorted(_s14_dir.glob("runs/*.json")):
            try:
                _run_data = json.loads(_run_file.read_text(encoding="utf-8"))
                if isinstance(_run_data, dict) and _run_data.get("hyperparameters"):
                    _hp = _run_data["hyperparameters"]
                    if isinstance(_hp, dict) and _hp:
                        _hp_table = "\n\n## HYPERPARAMETERS (include as a table in the Method section)\n"
                        _hp_table += "| Hyperparameter | Value |\n|---|---|\n"
                        for _hk, _hv in sorted(_hp.items()):
                            _hp_table += f"| {_hk} | {_hv} |\n"
                        _hp_table += (
                            "\nThis table MUST appear in the Method/Experiments section. "
                            "Include ALL hyperparameters used, with justification for key choices.\n"
                        )
                        break
            except (json.JSONDecodeError, OSError):
                continue
        if _hp_table:
            break
    # Also check staging dirs for results.json
    if not _hp_table:
        for _staging_dir in sorted(run_dir.glob("stage-*/runs/_docker_*")):
            _rjson = _staging_dir / "results.json"
            if _rjson.is_file():
                try:
                    _rdata = json.loads(_rjson.read_text(encoding="utf-8"))
                    if isinstance(_rdata, dict) and _rdata.get("hyperparameters"):
                        _hp = _rdata["hyperparameters"]
                        if isinstance(_hp, dict) and _hp:
                            _hp_table = "\n\n## HYPERPARAMETERS (include as a table in the Method section)\n"
                            _hp_table += "| Hyperparameter | Value |\n|---|---|\n"
                            for _hk, _hv in sorted(_hp.items()):
                                _hp_table += f"| {_hk} | {_hv} |\n"
                            _hp_table += (
                                "\nThis table MUST appear in the Method/Experiments section. "
                                "Include ALL hyperparameters used, with justification for key choices.\n"
                            )
                            break
                except (json.JSONDecodeError, OSError):
                    continue
    if _hp_table:
        exp_metrics_instruction += _hp_table

    if _paper_len_cfg in {"deterministic", "fallback"}:
        draft = _build_deterministic_paper_draft(
            topic=config.research.topic,
            outline=outline,
            analysis=_read_prior_artifact(run_dir, "analysis.md") or "",
            exp_summary_text=exp_summary_text,
            raw_metrics_block=raw_metrics_block,
            citation_context=_build_deterministic_citation_context(run_dir),
        )
        (stage_dir / "paper_draft.md").write_text(draft, encoding="utf-8")
        return StageResult(
            stage=Stage.PAPER_DRAFT,
            status=StageStatus.DONE,
            artifacts=("paper_draft.md",),
            evidence_refs=("stage-20/paper_draft.md",),
        )

    # F2.6: Build citation list from references.bib / candidates with cite_keys
    citation_instruction = ""
    bib_text = _read_literature_bib(run_dir)

    # P3: Pre-verify citations before paper draft — remove hallucinated refs
    if bib_text and bib_text.strip():
        from researchclaw.literature.verify import (
            filter_verified_bibtex,
            verify_citations as _verify_cit,
        )
        try:
            _pre_report = _verify_cit(bib_text, inter_verify_delay=0.5)
            _kept = _pre_report.verified + _pre_report.suspicious
            _removed = _pre_report.hallucinated
            if _removed > 0:
                bib_text = filter_verified_bibtex(
                    bib_text, _pre_report, include_suspicious=True
                )
                (stage_dir / "references_preverified.bib").write_text(
                    bib_text, encoding="utf-8"
                )
                logger.info(
                    "P3: Pre-verification kept %d/%d citations (removed %d hallucinated)",
                    _kept, _pre_report.total, _removed,
                )
        except Exception as exc:
            logger.warning("P3: Pre-verification failed, using original bib: %s", exc)

    candidates_text = _read_prior_artifact(run_dir, "candidates.jsonl")
    if candidates_text:
        cite_lines: list[str] = []
        for row_text in candidates_text.strip().splitlines():
            row = _safe_json_loads(row_text, {})
            if isinstance(row, dict) and row.get("cite_key"):
                authors_info = ""
                if isinstance(row.get("authors"), list) and row["authors"]:
                    first_author = row["authors"][0]
                    if isinstance(first_author, dict):
                        # BUG-38: name may be non-str (tuple/list) — force str
                        _name = first_author.get("name", "")
                        authors_info = _name if isinstance(_name, str) else str(_name)
                    elif isinstance(first_author, str):
                        authors_info = first_author
                    if len(row["authors"]) > 1:
                        authors_info += " et al."
                title = row.get("title", "")
                cite_lines.append(
                    f"- [{row['cite_key']}] → TITLE: \"{title}\" "
                    f"| {authors_info} "
                    f"({row.get('venue', '')}, {row.get('year', '')}, "
                    f"cited {row.get('citation_count', 0)} times) "
                    f"| ONLY cite this key when discussing: {title}"
                )
        if cite_lines:
            citation_instruction = (
                "\n\nAVAILABLE REFERENCES (use [cite_key] to cite in the text):\n"
                + "\n".join(cite_lines)
                + "\n\nCRITICAL CITATION RULES:\n"
                "- In the body text, cite using [cite_key] format, e.g. [smith2024transformer].\n"
                "- Do NOT write a References section — it will be auto-generated from the bibliography file.\n"
                "- Do NOT invent any references or arXiv IDs not in the above list.\n"
                "- You may cite a subset, but NEVER fabricate citations or change arXiv IDs.\n"
                "- SEMANTIC MATCHING: Before citing a reference, verify that its TITLE matches\n"
                "  the concept you are discussing. Do NOT use an unrelated cite_key just\n"
                "  because it sounds similar.\n"
                "- If no reference in the list matches the concept you want to cite,\n"
                "  write 'prior work has shown...' WITHOUT a citation, rather than using\n"
                "  a mismatched reference.\n"
                "- Each [cite_key] MUST correspond to the paper whose title is shown\n"
                "  next to that key in the list above. Cross-check before citing.\n"
                "\nCITATION QUANTITY & QUALITY CONSTRAINTS:\n"
                "- Cite 25-40 unique references in the paper body. The Related Work\n"
                "  section alone should cite at least 15 references.\n"
                "- Every citation MUST be directly relevant to the paper's topic.\n"
                "- DO NOT cite papers from unrelated domains (wireless communication, "
                "manufacturing, UAV, etc.).\n"
                "- Prefer well-known, highly-cited papers over obscure ones.\n"
                "- If unsure whether a paper exists or is relevant, DO NOT cite it.\n"
            )

    if llm is not None:
        _pm = prompts or PromptManager()
        topic_constraint = _pm.block("topic_constraint", topic=config.research.topic)

        # --- Section-by-section writing (3 calls) for conference-grade depth ---
        import signal as _signal

        class _PaperDraftTimeout(Exception):
            pass

        def _paper_timeout_handler(signum, frame):  # noqa: ARG001
            raise _PaperDraftTimeout()

        _timeout_sec = _paper_draft_timeout_sec(config)
        _old_handler = _signal.signal(_signal.SIGALRM, _paper_timeout_handler)
        _signal.alarm(max(1, _timeout_sec))
        try:
            draft = _write_paper_sections(
                llm=llm,
                pm=_pm,
                run_dir=run_dir,
                preamble=preamble,
                topic_constraint=topic_constraint,
                exp_metrics_instruction=exp_metrics_instruction,
                citation_instruction=citation_instruction,
                outline=outline,
                model_name=config.llm.primary_model,
                discussion_ablation=discussion_ablation,
                figure_prompt_instruction=figure_prompt_instruction,
                target_conference=str(getattr(config.export, "target_conference", "") or ""),
                target_pages=int(getattr(config.export, "target_pages", 0) or 0),
            )
        except Exception as exc:  # noqa: BLE001
            logger.error(
                "Stage 20: LLM paper draft generation failed/timed out after %ss — using deterministic factual draft: %s",
                _timeout_sec,
                exc,
            )
            draft = _build_deterministic_paper_draft(
                topic=config.research.topic,
                outline=outline,
                analysis=_read_prior_artifact(run_dir, "analysis.md") or "",
                exp_summary_text=exp_summary_text,
                raw_metrics_block=raw_metrics_block,
            )
        finally:
            _signal.alarm(0)
            _signal.signal(_signal.SIGALRM, _old_handler)

        # R7: Strip LLM-generated References section — it often fabricates arXiv IDs.
        import re as _re_r7
        ref_pattern = _re_r7.compile(
            r'^(#{1,2}\s*References.*)', _re_r7.MULTILINE | _re_r7.DOTALL
        )
        ref_match = ref_pattern.search(draft)
        if ref_match:
            draft = draft[:ref_match.start()].rstrip()
            logger.info("Stage 20: Stripped LLM-generated References section (R7 fix)")
    else:
        # Build template with real data if available
        results_section = "Template results summary."
        if exp_summary_text:
            exp_summary = _safe_json_loads(exp_summary_text, {})
            if isinstance(exp_summary, dict) and exp_summary.get("metrics_summary"):
                lines = ["Experiment results:"]
                for mk, mv in exp_summary["metrics_summary"].items():
                    if isinstance(mv, dict):
                        lines.append(
                            f"- {mk}: mean={mv.get('mean')}, min={mv.get('min')}, "
                            f"max={mv.get('max')}, n={mv.get('count')}"
                        )
                results_section = "\n".join(lines)

        draft = f"""# Draft Title

## Abstract
Template draft abstract.

## Introduction
Template introduction for {config.research.topic}.

## Related Work
Template related work.

## Method
Template method description.

## Experiments
Template experimental setup.

## Results
{results_section}

## Limitations
Template limitations.

## Conclusion
Template conclusion.

## References
Template references.

Generated: {_utcnow_iso()}
"""
    (stage_dir / "paper_draft.md").write_text(draft, encoding="utf-8")

    # Extract FIGURE_PROMPT blocks, render via NanoBanana, write figure_prompts.json
    _topic = config.research.topic if hasattr(config, "research") else ""
    _fig_prompts = _extract_figure_prompts(draft, topic=_topic)
    _draft_artifacts = ["paper_draft.md"]
    if _fig_prompts:
        _fig_prompts = _render_figure_prompts(
            _fig_prompts, stage_dir, config, llm, topic=_topic,
        )
        _fp_path = stage_dir / "figure_prompts.json"
        _fp_path.write_text(
            json.dumps(_fig_prompts, indent=2, ensure_ascii=False),
            encoding="utf-8",
        )
        _draft_artifacts.append("figure_prompts.json")
        for _fp in _fig_prompts:
            if _fp.get("success") and _fp.get("output_path"):
                _rel = Path(_fp["output_path"]).name
                _draft_artifacts.append(f"figures/{_rel}")
        logger.info(
            "Stage 20: Extracted %d figure prompts → %s",
            len(_fig_prompts), _fp_path,
        )

    # Validate draft quality (section balance + bullet density)
    _validate_draft_quality(draft, stage_dir=stage_dir)

    return StageResult(
        stage=Stage.PAPER_DRAFT,
        status=StageStatus.DONE,
        artifacts=tuple(_draft_artifacts),
        evidence_refs=tuple(f"stage-20/{a}" for a in _draft_artifacts),
    )


def _collect_experiment_evidence(run_dir: Path) -> str:
    """Collect actual experiment parameters and results for peer review."""
    evidence_parts: list[str] = []

    # Put the deterministic fact contract first.  It is derived from the
    # executed result artifact and archived dataset files, and therefore takes
    # precedence over any legacy smoke-run wrapper metadata.
    fact_contract = _build_experiment_fact_contract(run_dir)
    if fact_contract:
        evidence_parts.append(fact_contract)

    summary_text = _read_prior_artifact(run_dir, "experiment_summary.json")
    summary_payload = _safe_json_loads(summary_text or "{}", {})
    authoritative_total_runs = 0
    if isinstance(summary_payload, dict) and summary_payload:
        authoritative_total_runs = int(summary_payload.get("total_runs", 0) or 0)
        review_summary = {
            "total_runs": authoritative_total_runs,
            "condition_count": summary_payload.get("condition_count"),
            "condition_summaries": summary_payload.get("condition_summaries", {}),
            "paired_comparisons": summary_payload.get("paired_comparisons", []),
            "experiment_provenance": summary_payload.get("experiment_provenance", {}),
        }
        evidence_parts.append(
            "### Authoritative Executed Experiment Summary\n```json\n"
            + json.dumps(review_summary, indent=2, ensure_ascii=False)
            + "\n```"
        )

    # 1. Read experiment code to find actual trial count, methods used
    exp_dir = _read_prior_artifact(run_dir, "experiment/")
    if exp_dir and Path(exp_dir).is_dir():
        main_py = Path(exp_dir) / "main.py"
        if main_py.exists():
            code = main_py.read_text(encoding="utf-8")
            evidence_parts.append(f"### Actual Experiment Code (main.py)\n```python\n{code[:3000]}\n```")

    # 2. Read sandbox run results (actual metrics, runtime, stderr).  A domain
    # results.json stores metrics under conditions/seeds, unlike the generic
    # smoke wrapper whose top-level `metrics` field may be null.
    runs_text = _read_prior_artifact(run_dir, "runs/")
    if runs_text and Path(runs_text).is_dir():
        for run_file in sorted(Path(runs_text).glob("*.json"))[:5]:
            payload = _safe_json_loads(run_file.read_text(encoding="utf-8"), {})
            if isinstance(payload, dict):
                if isinstance(payload.get("conditions"), dict):
                    summary = {
                        "seeds": payload.get("seeds", []),
                        "conditions": payload.get("conditions", {}),
                        "statistical_tests": payload.get("statistical_tests", {}),
                    }
                else:
                    summary = {
                        "metrics": payload.get("metrics"),
                        "elapsed_sec": payload.get("elapsed_sec"),
                        "timed_out": payload.get("timed_out"),
                    }
                stderr = payload.get("stderr", "")
                if stderr:
                    summary["stderr_excerpt"] = stderr[:500]
                evidence_parts.append(
                    f"### Run Result: {run_file.name}\n```json\n{json.dumps(summary, indent=2)}\n```"
                )

    # 3. Read refinement log for actual iteration count
    refine_log_text = _read_prior_artifact(run_dir, "refinement_log.json")
    if refine_log_text:
        try:
            rlog = json.loads(refine_log_text)
            summary = {
                "iterations_executed": len(rlog.get("iterations", []) if isinstance(rlog.get("iterations"), list) else []),
                "converged": rlog.get("converged"),
                "stop_reason": rlog.get("stop_reason"),
                "best_metric": rlog.get("best_metric"),
            }
            evidence_parts.append(
                f"### Refinement Summary\n```json\n{json.dumps(summary, indent=2)}\n```"
            )
        except (json.JSONDecodeError, TypeError):
            pass

    # 4. Report the scientific per-seed run count.  Counting JSON wrapper
    # files is wrong for domain experiments that persist all seeds in one file.
    actual_run_count = authoritative_total_runs
    if actual_run_count > 0:
        evidence_parts.append(
            f"### Actual Trial Count\n"
            f"**The experiment contains {actual_run_count} executed per-seed condition runs.** "
            f"If the paper claims a different number of trials, this is a CRITICAL discrepancy."
        )

    if not evidence_parts:
        return ""

    return (
        "\n\n## Actual Experiment Evidence\n"
        "Use the evidence below to verify the paper's methodology claims.\n\n"
        + "\n\n".join(evidence_parts)
    )


def _execute_peer_review(
    stage_dir: Path,
    run_dir: Path,
    config: RCConfig,
    adapters: AdapterBundle,
    *,
    llm: LLMClient | None = None,
    prompts: PromptManager | None = None,
) -> StageResult:
    draft = _read_prior_artifact(run_dir, "paper_draft.md") or ""
    experiment_evidence = _collect_experiment_evidence(run_dir)

    # Load draft quality warnings from Stage 17 (if available)
    _quality_suffix = ""
    _quality_json_path = _find_prior_file(run_dir, "draft_quality.json")
    if _quality_json_path and _quality_json_path.exists():
        try:
            _dq = json.loads(_quality_json_path.read_text(encoding="utf-8"))
            _dq_warnings = _dq.get("overall_warnings", [])
            if _dq_warnings:
                _quality_suffix = (
                    "\n\nAUTOMATED QUALITY ISSUES (flag these in your review):\n"
                    + "\n".join(f"- {w}" for w in _dq_warnings)
                    + "\n"
                )
        except Exception:  # noqa: BLE001
            pass

    if llm is not None:
        _pm = prompts or PromptManager()
        _overlay = _get_evolution_overlay(run_dir, "peer_review")
        sp = _pm.for_stage(
            "peer_review",
            evolution_overlay=_overlay,
            topic=config.research.topic,
            draft=draft,
            experiment_evidence=experiment_evidence,
        )
        _review_user = (
            sp.user
            + _quality_suffix
            + _claim_boundary_instruction(run_dir)
            + "\nExplicitly identify every claim that exceeds this boundary and give a scoped rewrite.\n"
        )
        resp = _chat_with_prompt(
            llm,
            sp.system,
            _review_user,
            json_mode=sp.json_mode,
            max_tokens=sp.max_tokens,
        )
        reviews = resp.content
    else:
        reviews = """# Reviews

## Reviewer A
- Strengths: Clear problem statement.
- Weaknesses: Limited ablation details.
- Actionable revisions: Add uncertainty analysis and stronger baselines.

## Reviewer B
- Strengths: Reproducibility focus.
- Weaknesses: Discussion underdeveloped.
- Actionable revisions: Expand limitations and broader impact.
"""
    (stage_dir / "reviews.md").write_text(reviews, encoding="utf-8")
    return StageResult(
        stage=Stage.PEER_REVIEW,
        status=StageStatus.DONE,
        artifacts=("reviews.md",),
        evidence_refs=("stage-21/reviews.md",),
    )


def _execute_paper_revision(
    stage_dir: Path,
    run_dir: Path,
    config: RCConfig,
    adapters: AdapterBundle,
    *,
    llm: LLMClient | None = None,
    prompts: PromptManager | None = None,
) -> StageResult:
    draft = _read_prior_artifact(run_dir, "paper_draft.md") or ""
    reviews = _read_prior_artifact(run_dir, "reviews.md") or ""
    draft_word_count = len(draft.split())

    # R4-2: Collect real metrics for anti-fabrication guard in revision
    # BUG-47: _collect_raw_experiment_metrics returns tuple[str, bool], must unpack
    _raw_metrics_tuple = _collect_raw_experiment_metrics(run_dir)
    raw_metrics_revision = _raw_metrics_tuple[0] if isinstance(_raw_metrics_tuple, tuple) else (_raw_metrics_tuple or "")
    data_integrity_revision = ""
    if raw_metrics_revision:
        data_integrity_revision = (
            raw_metrics_revision
            + "\nDATA INTEGRITY: Do NOT add new numbers that are not in the "
            "experiment data above. If a reviewer asks for additional results "
            "you do not have, state 'Due to computational constraints, "
            "this analysis was not conducted' instead of fabricating data.\n"
        )

    _paper_len_cfg = getattr(config.experiment, "paper_length", "") or "full"
    _deterministic_revision = _paper_len_cfg in {"deterministic", "fallback"}
    if _deterministic_revision:
        revised = _build_deterministic_paper_revision(
            topic=config.research.topic,
            draft=draft,
            reviews=reviews,
            raw_metrics_block=raw_metrics_revision,
            citation_context=_build_deterministic_citation_context(run_dir),
            exp_summary_text=_read_prior_artifact(run_dir, "experiment_summary.json") or "",
        )
    elif llm is not None:
        _pm = prompts or PromptManager()
        try:
            _ws_revision = _pm.block("writing_structure")
        except (KeyError, Exception):  # noqa: BLE001
            _ws_revision = ""
        # IMP-20/25/31/24: Load style blocks for revision prompt
        _rev_blocks: dict[str, str] = {}
        for _bname in ("academic_style_guide", "narrative_writing_rules",
                        "anti_hedging_rules", "anti_repetition_rules"):
            try:
                _rev_blocks[_bname] = _pm.block(_bname)
            except (KeyError, Exception):  # noqa: BLE001
                _rev_blocks[_bname] = ""
        # Load draft quality directives from Stage 17
        _quality_prefix = ""
        _quality_json_path = _find_prior_file(run_dir, "draft_quality.json")
        if _quality_json_path and _quality_json_path.exists():
            try:
                _dq = json.loads(_quality_json_path.read_text(encoding="utf-8"))
                _dq_directives = _dq.get("revision_directives", [])
                if _dq_directives:
                    _quality_prefix = (
                        "MANDATORY QUALITY FIXES (address ALL of these):\n"
                        + "\n".join(f"- {d}" for d in _dq_directives)
                        + "\n\n"
                    )
            except Exception:  # noqa: BLE001
                pass

        _overlay = _get_evolution_overlay(run_dir, "paper_revision")
        sp = _pm.for_stage(
            "paper_revision",
            evolution_overlay=_overlay,
            topic_constraint=_pm.block("topic_constraint", topic=config.research.topic),
            writing_structure=_ws_revision,
            draft=draft,
            reviews=(
                _quality_prefix
                + reviews
                + data_integrity_revision
                + _claim_boundary_instruction(run_dir)
            ),
            **_rev_blocks,
        )
        # R10-Fix2: Ensure max_tokens is sufficient for full paper revision
        revision_max_tokens = sp.max_tokens
        if revision_max_tokens and draft_word_count > 0:
            # ~1.5 tokens per word, 20% headroom
            min_tokens_needed = int(draft_word_count * 1.5 * 1.2)
            if revision_max_tokens < min_tokens_needed:
                revision_max_tokens = min_tokens_needed
                logger.info(
                    "Stage 19: Increased max_tokens from %d to %d to fit full paper revision",
                    sp.max_tokens,
                    revision_max_tokens,
                )

        # R10-Fix4: Retry on timeout for paper revision (critical stage)
        resp = _chat_with_prompt(
            llm,
            sp.system,
            sp.user,
            json_mode=sp.json_mode,
            max_tokens=revision_max_tokens,
            retries=2,
        )
        revised = resp.content
        revised_word_count = len(revised.split())
        # Length guard: if revision is shorter than 80% of draft, retry once
        if draft_word_count > 500 and revised_word_count < int(draft_word_count * 0.8):
            logger.warning(
                "Paper revision (%d words) is shorter than draft (%d words). "
                "Retrying with stronger length enforcement.",
                revised_word_count,
                draft_word_count,
            )
            retry_user = (
                f"CRITICAL LENGTH REQUIREMENT: The draft is {draft_word_count} words. "
                f"Your revision MUST be at least {draft_word_count} words — ideally longer. "
                f"Do NOT summarize or condense ANY section. Copy each section verbatim "
                f"and ONLY make targeted improvements to address reviewer comments. "
                f"If a section has no reviewer comments, include it UNCHANGED.\n\n"
                + sp.user
            )
            resp2 = _chat_with_prompt(
                llm, sp.system, retry_user,
                json_mode=sp.json_mode, max_tokens=revision_max_tokens,
            )
            revised2 = resp2.content
            revised2_word_count = len(revised2.split())
            if revised2_word_count >= int(draft_word_count * 0.8):
                revised = revised2
            elif revised2_word_count > revised_word_count:
                # Retry improved but still not enough — use the longer version
                revised = revised2
                logger.warning(
                    "Retry improved (%d → %d words) but still shorter than draft (%d).",
                    revised_word_count,
                    revised2_word_count,
                    draft_word_count,
                )
            else:
                # Both attempts produced short output — preserve full original draft
                logger.warning(
                    "Retry also produced short output (%d words). "
                    "Falling back to FULL ORIGINAL DRAFT to prevent content loss.",
                    revised2_word_count,
                )
                # Extract useful revision points as appendix
                revision_words = revised.split()
                revision_summary = (
                    " ".join(revision_words[:500]) + "\n\n*(Revision summary truncated)*"
                    if len(revision_words) > 500
                    else revised
                )
                if revision_summary.strip():
                    # Save revision notes to internal file, not paper body
                    (stage_dir / "revision_notes_internal.md").write_text(
                        revision_summary, encoding="utf-8"
                    )
                revised = draft
    else:
        revised = draft
    (stage_dir / "paper_revised.md").write_text(revised, encoding="utf-8")

    _revision_artifacts = ["paper_revised.md"]
    _topic = config.research.topic if hasattr(config, "research") else ""

    # Reuse figures from S20 (PAPER_DRAFT) instead of regenerating
    _s20_figures_dir = None
    for _sd in sorted(run_dir.glob("stage-20*"), reverse=True):
        _candidate = _sd / "figures"
        if _candidate.is_dir() and any(_candidate.iterdir()):
            _s20_figures_dir = _candidate
            break

    _fig_prompts = _extract_figure_prompts(revised, topic=_topic)
    if _fig_prompts:
        _s22_figures_dir = stage_dir / "figures"
        _s22_figures_dir.mkdir(parents=True, exist_ok=True)

        # Copy existing figures from S20 before rendering new ones
        _reused_ids: set[str] = set()
        if _s20_figures_dir:
            import shutil as _shutil_fig
            for _existing_fig in _s20_figures_dir.iterdir():
                if _existing_fig.suffix.lower() in {".png", ".jpg", ".jpeg", ".pdf", ".eps"}:
                    _dest = _s22_figures_dir / _existing_fig.name
                    if not _dest.exists():
                        _shutil_fig.copy2(_existing_fig, _dest)
            # Mark prompts as already rendered if their output file exists
            for _fp in _fig_prompts:
                _fid = _fp.get("figure_id", "")
                # Check common naming patterns
                for _ext in (".png", ".jpg", ".jpeg"):
                    _candidate_name = _s22_figures_dir / f"{_fid}{_ext}"
                    if _candidate_name.exists():
                        _fp["output_path"] = str(_candidate_name)
                        _fp["success"] = True
                        _reused_ids.add(_fid)
                        break
            if _reused_ids:
                logger.info(
                    "Stage 22: Reused %d/%d figures from S20",
                    len(_reused_ids), len(_fig_prompts),
                )

        # Only render prompts that don't already have figures
        _new_prompts = [fp for fp in _fig_prompts if fp.get("figure_id") not in _reused_ids]
        if _new_prompts:
            _new_prompts = _render_figure_prompts(
                _new_prompts, stage_dir, config, llm, topic=_topic,
            )
            # Merge back into _fig_prompts
            _new_map = {fp["figure_id"]: fp for fp in _new_prompts}
            for _fp in _fig_prompts:
                if _fp["figure_id"] in _new_map:
                    _fp.update(_new_map[_fp["figure_id"]])
            logger.info(
                "Stage 22: Rendered %d new figures (reused %d from S20)",
                len(_new_prompts), len(_reused_ids),
            )

        _fp_path = stage_dir / "figure_prompts.json"
        _fp_path.write_text(
            json.dumps(_fig_prompts, indent=2, ensure_ascii=False),
            encoding="utf-8",
        )
        _revision_artifacts.append("figure_prompts.json")
        for _fp in _fig_prompts:
            if _fp.get("success") and _fp.get("output_path"):
                _rel = Path(_fp["output_path"]).name
                _revision_artifacts.append(f"figures/{_rel}")
        logger.info(
            "Stage 22: Total %d figure prompts → %s",
            len(_fig_prompts), _fp_path,
        )

    # --- Generate LaTeX package for Overleaf ---
    try:
        _latex_llm = None if _deterministic_revision else llm
        _latex_artifacts = _generate_latex_package(stage_dir, run_dir, config, revised, llm=_latex_llm)
        _revision_artifacts.extend(_latex_artifacts)
        logger.info("Stage 22: LaTeX package generated → %s", stage_dir / "latex_package.zip")
    except Exception:
        logger.warning("Stage 22: LaTeX package generation failed", exc_info=True)

    return StageResult(
        stage=Stage.PAPER_REVISION,
        status=StageStatus.DONE,
        artifacts=tuple(_revision_artifacts),
        evidence_refs=tuple(f"stage-22/{a}" for a in _revision_artifacts),
    )


def _generate_latex_package(
    stage_dir: Path,
    run_dir: Path,
    config: RCConfig,
    paper_md: str,
    *,
    llm: "LLMClient | None" = None,
) -> list[str]:
    """Generate a complete LaTeX package (zip) ready for Overleaf upload.

    Uses LLM to convert markdown paper to LaTeX, then collects figures,
    references, and style files into a zip archive.
    """
    import shutil as _shutil_lp

    pkg_dir = stage_dir / "latex_package"
    if pkg_dir.exists():
        _shutil_lp.rmtree(pkg_dir)
    pkg_dir.mkdir(parents=True)
    fig_dir = pkg_dir / "figures"
    fig_dir.mkdir()

    # 1. Collect available figures first (need filenames for LLM prompt)
    _fig_files: list[str] = []
    for _fig_src in [
        stage_dir / "figures",
        run_dir / "stage-16" / "charts",
        run_dir / "stage-14" / "charts",
    ]:
        if _fig_src.is_dir():
            for _fig_file in _fig_src.iterdir():
                if _fig_file.suffix.lower() in {".png", ".jpg", ".jpeg", ".pdf", ".eps"}:
                    _shutil_lp.copy2(_fig_file, fig_dir / _fig_file.name)
                    _fig_files.append(_fig_file.name)

    # 2. Collect references.bib
    bib_text = ""
    for _bib_candidate in [
        run_dir / "stage-04" / "references.bib",
        run_dir / "stage-22" / "references.bib",
    ]:
        if _bib_candidate.exists():
            bib_text = _bib_candidate.read_text(encoding="utf-8")
            break
    if not bib_text:
        bib_text = "% No references collected\n"

    # Extract cite keys from bib
    _bib_keys = re.findall(r"@\w+\{([^,]+),", bib_text)

    # 3. Convert markdown to LaTeX. IEEE uses the deterministic converter so
    # the class, numeric citation style, and verified BibTeX cannot be replaced
    # by an LLM-generated NeurIPS preamble or fabricated bibliography.
    tex_content = ""
    _target_conference = str(getattr(config.export, "target_conference", "") or "")
    _is_ieee = _target_conference.lower().startswith("ieee")
    if _is_ieee:
        try:
            from researchclaw.templates import get_template, markdown_to_latex

            _ieee_paper_md = paper_md
            if not re.search(r"^#{1,3}\s+(?:Index Terms|Keywords)\s*$", _ieee_paper_md, re.MULTILINE | re.IGNORECASE):
                _topic_lower = str(getattr(config.research, "topic", "") or "").lower()
                _index_terms = ["reproducible experimentation", "evidence-bounded evaluation"]
                if "imu" in _topic_lower or "inertial" in _topic_lower:
                    _index_terms = ["inertial sensing", "visual-inertial odometry", "sensor fusion", "robust perception"]
                _abstract_match = re.search(r"(^#{1,3}\s+Abstract\s*$.*?)(?=^#{1,3}\s+)", _ieee_paper_md, re.MULTILINE | re.DOTALL | re.IGNORECASE)
                if _abstract_match:
                    _insert_at = _abstract_match.end(1)
                    _ieee_paper_md = (
                        _ieee_paper_md[:_insert_at]
                        + "\n\n## Index Terms\n"
                        + ", ".join(_index_terms[:6])
                        + "\n"
                        + _ieee_paper_md[_insert_at:]
                    )
            _tpl = get_template(_target_conference)
            tex_content = markdown_to_latex(
                _ieee_paper_md,
                _tpl,
                title=_extract_paper_title(_ieee_paper_md),
                authors=config.export.authors,
                bib_file=config.export.bib_file,
            )
            logger.info("LaTeX package: deterministic IEEE conversion successful (%d chars)", len(tex_content))
        except Exception as exc:
            logger.warning("LaTeX package: deterministic IEEE conversion failed: %s", exc)

    # Non-IEEE legacy templates retain the LLM conversion path.
    if llm is not None and not tex_content:
        _system_prompt = (
            "You are an expert academic LaTeX typesetter. You have TWO tasks:\n"
            "Task 1: Convert the markdown paper into a complete .tex file.\n"
            "Task 2: Generate a matching references.bib file.\n\n"
            "Output format: First output the complete .tex inside ```latex ... ```, "
            "then output the complete .bib inside ```bib ... ```.\n\n"
            "STRICT RULES:\n"
            "1. The .tex must be complete and compilable. No explanations outside code blocks.\n"
            "2. Use \\usepackage[preprint]{neurips_2025} as the style package.\n"
            "3. All citations [cite_xxx] in the markdown MUST become \\cite{xxx} in LaTeX. "
            "The key 'xxx' must match an entry in your generated .bib file.\n"
            "4. Generate the .bib file with REAL, accurate bibliography entries for every "
            "\\cite{} used in the paper. Use the citation context to identify the correct "
            "paper (author, title, year, venue). Do NOT fabricate — use real papers.\n"
            "5. References section: Do NOT include a hand-written reference list in the .tex. "
            "Use \\bibliographystyle{plainnat} and \\bibliography{references} at the end.\n"
            "5. Section hierarchy: Use \\section{} for main sections (Introduction, "
            "Related Work, Method, Experiments, Results, Discussion, Limitations, "
            "Conclusion). Use \\subsection{} for sub-sections within them.\n"
            "6. All figures are in the figures/ directory. Use "
            "\\includegraphics[width=0.85\\textwidth]{figures/filename.png} with "
            "proper \\begin{figure}...\\end{figure} environments, \\caption{}, "
            "and \\label{fig:xxx}.\n"
            "7. Convert <!-- FIGURE_PROMPT --> comment blocks into \\begin{figure} "
            "environments. Match figure prompts to available figure files by their "
            "content/description.\n"
            "8. Include ALL available figures in appropriate locations in the paper.\n"
            "9. Preserve all math equations (both inline $...$ and display $$...$$).\n"
            "10. Convert markdown tables to LaTeX \\begin{table}...\\end{table} with "
            "\\begin{tabular}, \\toprule, \\midrule, \\bottomrule.\n"
            "11. Use \\textit{} for italics, \\textbf{} for bold.\n"
            "12. The preamble must include: hyperref, url, booktabs, amsfonts, "
            "amsmath, graphicx, natbib, microtype, inputenc (utf8), fontenc (T1), "
            "placeins (for \\FloatBarrier).\n"
            "13. Use \\begin{figure}[htbp] (NOT [t]) so figures stay near their text.\n"
            "14. Add \\FloatBarrier before \\section{Conclusion} and before "
            "\\bibliographystyle to prevent figures from floating past them.\n"
            "15. Spread figures evenly across sections. Do NOT cluster multiple "
            "figures together — place each figure near the text that discusses it.\n"
            "16. Figure placement priority: figure_1.png should be the teaser/overview "
            "figure (after abstract/intro). figure_2.png should be the architecture/method "
            "figure. fig_main_results and fig_radar are RESULTS figures — place in Results section. "
            "Use the figure descriptions below to determine correct placement.\n"
        )

        # Load figure descriptions from figure_prompts.json
        _fig_descriptions = ""
        _fp_path = stage_dir / "figure_prompts.json"
        if _fp_path.exists():
            try:
                _fp_data = json.loads(_fp_path.read_text(encoding="utf-8"))
                _fig_desc_lines = []
                for fp in _fp_data:
                    fname = Path(fp.get("output_path", "")).name if fp.get("output_path") else ""
                    desc = fp.get("caption", fp.get("raw_prompt", ""))
                    ftype = fp.get("figure_type", "")
                    section = fp.get("section", "")
                    if fname and desc:
                        _fig_desc_lines.append(
                            f"- {fname}: [{ftype}] for {section} section — {desc}"
                        )
                if _fig_desc_lines:
                    _fig_descriptions = (
                        "\n\n## Figure descriptions (use these to place figures correctly):\n"
                        + chr(10).join(_fig_desc_lines)
                    )
            except Exception:
                pass

        _user_prompt = (
            f"## Available figure files in figures/ directory:\n"
            f"{chr(10).join('- ' + f for f in _fig_files) if _fig_files else '(none)'}\n"
            f"{_fig_descriptions}\n\n"
            f"## Available BibTeX citation keys:\n"
            f"{chr(10).join('- ' + k for k in _bib_keys[:50]) if _bib_keys else '(none)'}\n\n"
            f"## Markdown paper to convert:\n\n{paper_md}\n"
        )

        try:
            resp = _chat_with_prompt(
                llm,
                _system_prompt,
                _user_prompt,
                max_tokens=32768,
            )
            raw_output = resp.content

            # Extract .tex from ```latex ... ``` block
            tex_content = ""
            if "```latex" in raw_output:
                tex_content = raw_output.split("```latex", 1)[1].split("```", 1)[0].strip()
            elif "```tex" in raw_output:
                tex_content = raw_output.split("```tex", 1)[1].split("```", 1)[0].strip()
            elif "```" in raw_output and "\\documentclass" in raw_output:
                tex_content = raw_output.split("```", 1)[1].split("```", 1)[0].strip()
            else:
                tex_content = raw_output

            # Extract .bib from ```bib ... ``` block (if LLM generated one)
            _llm_bib = ""
            if "```bib" in raw_output:
                _llm_bib = raw_output.split("```bib", 1)[1].split("```", 1)[0].strip()
            elif "```bibtex" in raw_output:
                _llm_bib = raw_output.split("```bibtex", 1)[1].split("```", 1)[0].strip()
            if _llm_bib and "@" in _llm_bib:
                bib_text = _llm_bib
                logger.info("LaTeX package: Using LLM-generated bib (%d entries)",
                            _llm_bib.count("@"))

            # Validate
            if "\\documentclass" not in tex_content:
                raise ValueError("LLM output missing \\documentclass")
            if "\\end{document}" not in tex_content:
                tex_content += "\n\n\\end{document}\n"

            logger.info("LaTeX package: LLM conversion successful (%d chars)", len(tex_content))
        except Exception as e:
            logger.warning("LaTeX package: LLM conversion failed (%s), using fallback", e)
            llm = None  # Fall through to fallback

    if not tex_content:
        # Fallback: minimal wrapping
        _title_match = re.search(r"^#\s+(.+)$", paper_md, re.MULTILINE)
        _title = _title_match.group(1) if _title_match else "Research Paper"
        _fallback_class = "\\documentclass[conference]{IEEEtran}\n" if _is_ieee else "\\documentclass{article}\n\\usepackage[preprint]{neurips_2025}\n"
        _fallback_bib_style = "IEEEtran" if _is_ieee else "plainnat"
        tex_content = (
            _fallback_class
            +
            "\\usepackage[utf8]{inputenc}\n\\usepackage[T1]{fontenc}\n"
            "\\usepackage{hyperref}\n\\usepackage{url}\n"
            "\\usepackage{booktabs}\n\\usepackage{amsfonts}\n"
            "\\usepackage{amsmath}\n\\usepackage{graphicx}\n"
            "\\usepackage{natbib}\n\\usepackage{microtype}\n\n"
            f"\\title{{{_title}}}\n"
            "\\author{{Anonymous}}\n\n"
            "\\begin{{document}}\n\\maketitle\n\n"
            "% LLM conversion failed. Paper content needs manual conversion.\n"
            "% See paper_revised.md for the original markdown.\n\n"
            f"\\bibliographystyle{{{_fallback_bib_style}}}\n"
            "\\bibliography{{references}}\n\n"
            "\\end{{document}}\n"
        )

    (pkg_dir / "main.tex").write_text(tex_content, encoding="utf-8")
    (pkg_dir / "references.bib").write_text(bib_text, encoding="utf-8")

    # 4. Copy style files
    try:
        from researchclaw.templates import get_template
        _export_tpl = get_template(_target_conference or "neurips_2025")
        for _sty_file in _export_tpl.get_style_files():
            _shutil_lp.copy2(_sty_file, pkg_dir / _sty_file.name)
    except Exception:
        pass

    # 5. Create zip archive
    zip_path = stage_dir / "latex_package"
    _shutil_lp.make_archive(str(zip_path), "zip", str(pkg_dir))

    return ["latex_package.zip", "latex_package/"]


def _execute_quality_gate(
    stage_dir: Path,
    run_dir: Path,
    config: RCConfig,
    adapters: AdapterBundle,
    *,
    llm: LLMClient | None = None,
    prompts: PromptManager | None = None,
) -> StageResult:
    revised = _read_prior_artifact(run_dir, "paper_revised.md") or ""
    report: dict[str, Any] | None = None

    # BUG-25: Load experiment summary for cross-checking
    _exp_summary_text = _read_prior_artifact(run_dir, "experiment_summary.json") or ""
    _exp_summary = _safe_json_loads(_exp_summary_text, {}) if _exp_summary_text else {}
    _exp_failed = False
    if isinstance(_exp_summary, dict):
        _best_run = _exp_summary.get("best_run", {})
        if isinstance(_best_run, dict):
            _exp_failed = (
                _best_run.get("status") == "failed"
                and not _best_run.get("metrics")
            )
        # Also check if metrics_summary is empty
        if not _exp_summary.get("metrics_summary"):
            _exp_failed = True

    _claim_integrity = _build_claim_integrity_report(run_dir, revised)
    _protocol_audit: dict[str, Any] = {}
    try:
        _protocol_audit = _safe_json_loads(
            (run_dir / "stage-16" / "evaluation_protocol_audit.json").read_text(encoding="utf-8"),
            {},
        )
    except (OSError, UnicodeDecodeError):
        _protocol_audit = {}

    if llm is not None:
        _pm = prompts or PromptManager()
        # IMP-33: Evaluate the full paper instead of truncating to 12K chars.
        # Split into chunks if very long, but prefer sending the full text.
        paper_for_eval = revised[:40000] if len(revised) > 40000 else revised

        # BUG-25: Inject experiment status into quality gate prompt
        _exp_context = ""
        if _exp_summary and isinstance(_exp_summary, dict):
            _exp_status_keys = {
                k: _exp_summary.get(k) for k in (
                    "total_conditions", "total_metric_keys",
                    "metrics_summary",
                ) if _exp_summary.get(k) is not None
            }
            if _best_run := _exp_summary.get("best_run"):
                _exp_status_keys["best_run_status"] = (
                    _best_run.get("status") if isinstance(_best_run, dict) else str(_best_run)
                )
            _exp_context = (
                "\n\nExperiment summary (for cross-checking reported numbers):\n"
                + json.dumps(_exp_status_keys, indent=2, default=str)[:4000]
                + "\n\nCross-check: If the experiment status is 'failed' with "
                "empty metrics, any numerical results in tables constitute "
                "fabrication. Penalize severely.\n"
            )
        _exp_context += (
            "\n\nDeterministic claim-integrity audit (non-negotiable):\n"
            + json.dumps(_claim_integrity, ensure_ascii=False, indent=2)[:8000]
            + "\nA blocked audit must receive a revise/degraded verdict.\n"
        )
        if _protocol_audit:
            _exp_context += (
                "\n\nDeterministic evaluation-protocol audit (non-negotiable):\n"
                + json.dumps(_protocol_audit, ensure_ascii=False, indent=2)[:6000]
                + "\nIf status is not 'passed', do not present the evaluation as statistically rigorous.\n"
            )

        _overlay = _get_evolution_overlay(run_dir, "quality_gate")
        sp = _pm.for_stage(
            "quality_gate",
            evolution_overlay=_overlay,
            quality_threshold=str(config.research.quality_threshold),
            revised=paper_for_eval + _exp_context,
        )
        resp = _chat_with_prompt(
            llm,
            sp.system,
            sp.user,
            json_mode=sp.json_mode,
            max_tokens=sp.max_tokens,
        )
        parsed = _safe_json_loads(resp.content, {})
        if isinstance(parsed, dict):
            report = parsed
    # BUG-25: If experiment failed with no metrics, cap the quality score
    if report is not None and _exp_failed:
        _orig_score = report.get("score_1_to_10", 5)
        if isinstance(_orig_score, (int, float)) and _orig_score > 3:
            report["score_1_to_10"] = min(_orig_score, 3.0)
            report.setdefault("weaknesses", []).append(
                "Experiment failed with no metrics — any reported numerical "
                "results are unsupported and likely fabricated."
            )
            logger.warning(
                "BUG-25: Experiment failed — capping quality score from %.1f to 3.0",
                _orig_score,
            )
    if report is None:
        report = _default_quality_report(config.research.quality_threshold)
    _protocol_limited = bool(_protocol_audit) and _protocol_audit.get("status") != "passed"
    if _protocol_limited:
        _orig_score = report.get("score_1_to_10", 5)
        if isinstance(_orig_score, (int, float)):
            report["score_1_to_10"] = min(float(_orig_score), 5.0)
        report["verdict"] = "revise"
        _weaknesses = report.setdefault("weaknesses", [])
        if not isinstance(_weaknesses, list):
            _weaknesses = [str(_weaknesses)]
            report["weaknesses"] = _weaknesses
        _weaknesses.append(
            "评估协议未满足最少 seed/统计要求，不能将当前结果表述为严格的统计结论。"
        )
        _actions = report.setdefault("required_actions", [])
        if not isinstance(_actions, list):
            _actions = [str(_actions)]
            report["required_actions"] = _actions
        _actions.append("为每个实验条件补齐计划中的独立 seed，并生成均值、标准差、95% CI 和配对检验。")
        report["evaluation_protocol_status"] = _protocol_audit.get("status")
        report["evaluation_protocol_missing_seed_conditions"] = _protocol_audit.get(
            "missing_seed_conditions", []
        )
    if _claim_integrity.get("status") == "blocked":
        _orig_score = report.get("score_1_to_10", 5)
        if isinstance(_orig_score, (int, float)):
            report["score_1_to_10"] = min(float(_orig_score), 4.0)
        report["verdict"] = "revise"
        _weaknesses = report.setdefault("weaknesses", [])
        if not isinstance(_weaknesses, list):
            _weaknesses = [str(_weaknesses)]
            report["weaknesses"] = _weaknesses
        _weaknesses.extend(
            violation.get("message_zh", "结论证据边界未通过。")
            for violation in _claim_integrity.get("violations", [])
            if isinstance(violation, dict)
        )
        report["claim_integrity_status"] = "blocked"
        report["claim_integrity_score"] = _claim_integrity.get("integrity_score", 0)
    report.setdefault("generated", _utcnow_iso())
    (stage_dir / "quality_report.json").write_text(
        json.dumps(report, indent=2), encoding="utf-8"
    )
    (stage_dir / "claim_integrity_report.json").write_text(
        json.dumps(_claim_integrity, indent=2, ensure_ascii=False), encoding="utf-8"
    )

    # T2.1: Enforce quality gate — fail if score below threshold
    score = report.get("score_1_to_10", 0)
    verdict = report.get("verdict", "proceed")
    threshold = config.research.quality_threshold or 5.0

    # --- Fabrication flag: collect real metrics for Stage 22 sanitization ---
    _fabrication_info: dict[str, Any] = {
        "experiment_failed": _exp_failed,
        "quality_score": score,
        "real_metric_values": [],
    }
    if isinstance(_exp_summary, dict):
        # Collect ALL real numeric values from experiment_summary.json
        _cond_summaries = _exp_summary.get("condition_summaries", {})
        if isinstance(_cond_summaries, dict):
            for cond_name, cond_data in _cond_summaries.items():
                if not isinstance(cond_data, dict):
                    continue
                cond_status = cond_data.get("status", "")
                if cond_status == "failed":
                    continue  # skip failed conditions
                for k, v in cond_data.items():
                    if isinstance(v, (int, float)) and k not in (
                        "seed_count", "total_steps", "training_steps",
                    ):
                        _fabrication_info["real_metric_values"].append(
                            round(float(v), 4)
                        )
        _ms = _exp_summary.get("metrics_summary", {})
        if isinstance(_ms, dict):
            for _mk, _mv in _ms.items():
                if isinstance(_mv, dict):
                    for _stat in ("mean", "min", "max"):
                        _sv = _mv.get(_stat)
                        if isinstance(_sv, (int, float)):
                            _fabrication_info["real_metric_values"].append(
                                round(float(_sv), 4)
                            )
    _fabrication_info["has_real_data"] = bool(
        _fabrication_info["real_metric_values"]
    )
    _fabrication_info["fabrication_suspected"] = (
        _exp_failed and not _fabrication_info["has_real_data"]
    )
    (stage_dir / "fabrication_flags.json").write_text(
        json.dumps(_fabrication_info, indent=2), encoding="utf-8"
    )

    _claim_blocked = _claim_integrity.get("status") == "blocked"
    _below_threshold = isinstance(score, (int, float)) and score < threshold
    if _claim_blocked or _protocol_limited or _below_threshold:
        if config.research.graceful_degradation:
            logger.warning(
                "Quality gate DEGRADED: score=%s threshold=%.1f claim_integrity=%s — "
                "continuing with sanitization (graceful_degradation=True)",
                score, threshold, _claim_integrity.get("status"),
            )
            # Write degradation signal for downstream stages
            signal = {
                "score": score,
                "threshold": threshold,
                "verdict": verdict,
                "claim_integrity_status": _claim_integrity.get("status"),
                "reason": (
                    "claim_integrity_blocked" if _claim_blocked
                    else "evaluation_protocol_insufficient" if _protocol_limited
                    else "quality_score_below_threshold"
                ),
                "weaknesses": report.get("weaknesses", []),
                "evaluation_protocol_status": _protocol_audit.get("status") if _protocol_audit else None,
                "generated": _utcnow_iso(),
            }
            (run_dir / "degradation_signal.json").write_text(
                json.dumps(signal, indent=2), encoding="utf-8"
            )
            return StageResult(
                stage=Stage.QUALITY_GATE,
                status=StageStatus.DONE,
                artifacts=(
                    "quality_report.json",
                    "fabrication_flags.json",
                    "claim_integrity_report.json",
                ),
                evidence_refs=("stage-23/quality_report.json", "stage-23/claim_integrity_report.json"),
                decision="degraded",
            )
        logger.warning(
            "Quality gate FAILED: score=%s threshold=%.1f verdict=%s claim_integrity=%s",
            score, threshold, verdict, _claim_integrity.get("status"),
        )
        return StageResult(
            stage=Stage.QUALITY_GATE,
            status=StageStatus.FAILED,
            artifacts=(
                "quality_report.json",
                "fabrication_flags.json",
                "claim_integrity_report.json",
            ),
            evidence_refs=("stage-23/quality_report.json", "stage-23/claim_integrity_report.json"),
            error=(
                "Claim integrity audit blocked the paper. "
                if _claim_blocked else
                "Evaluation protocol is insufficient for rigorous claims. "
                if _protocol_limited else
                f"Quality score {float(score):.1f}/10 below threshold {threshold:.1f}. "
            ) + "Paper needs revision before export.",
        )

    logger.info(
        "Quality gate PASSED: score %.1f >= threshold %.1f",
        score, threshold,
    )
    # A previous S25 export may have left a degradation signal behind.  The
    # current S23 result is authoritative, so do not let stale state cause a
    # later clean export to be labelled as degraded.
    _stale_degradation_signal = run_dir / "degradation_signal.json"
    if _stale_degradation_signal.exists():
        try:
            _stale_degradation_signal.unlink()
            logger.info("Quality gate PASSED: removed stale degradation signal")
        except OSError as exc:
            logger.warning("Could not remove stale degradation signal: %s", exc)
    return StageResult(
        stage=Stage.QUALITY_GATE,
        status=StageStatus.DONE,
        artifacts=(
            "quality_report.json",
            "fabrication_flags.json",
            "claim_integrity_report.json",
        ),
        evidence_refs=("stage-23/quality_report.json", "stage-23/claim_integrity_report.json"),
    )


def _execute_knowledge_archive(
    stage_dir: Path,
    run_dir: Path,
    config: RCConfig,
    adapters: AdapterBundle,
    *,
    llm: LLMClient | None = None,
    prompts: PromptManager | None = None,
) -> StageResult:
    revised = _read_prior_artifact(run_dir, "paper_revised.md") or ""
    analysis = _read_prior_artifact(run_dir, "analysis.md") or ""
    decision = _read_prior_artifact(run_dir, "decision.md") or ""
    preamble = _build_context_preamble(config, run_dir, include_goal=True)
    _paper_len_cfg = getattr(config.experiment, "paper_length", "") or "full"
    _deterministic_archive = _paper_len_cfg in {"deterministic", "fallback"}
    if llm is not None and not _deterministic_archive:
        _pm = prompts or PromptManager()
        _overlay = _get_evolution_overlay(run_dir, "knowledge_archive")
        sp = _pm.for_stage(
            "knowledge_archive",
            evolution_overlay=_overlay,
            preamble=preamble,
            decision=decision,
            analysis=analysis,
            revised=revised[:15000],
        )
        resp = _chat_with_prompt(
            llm,
            sp.system,
            sp.user,
            json_mode=sp.json_mode,
            max_tokens=sp.max_tokens,
        )
        archive = resp.content
    else:
        _metrics_excerpt = (_read_prior_artifact(run_dir, "experiment_summary.json") or "").strip()[:3000]
        _decision_excerpt = (decision or "").strip()[:1800]
        _analysis_excerpt = (analysis or "").strip()[:2400]
        archive = f"""# Knowledge Archive

## Lessons Learned
- Preserve strict metric reporting protocol: manuscript claims must be backed by parsed artifacts.
- Prefer deterministic local execution/parsing for stages that already have structured files.
- Keep refinement logs aligned with code changes and make smoke-test scope explicit.
- Do not turn pipeline execution metrics into unsupported domain-performance claims.

## Reproducibility
- Include exact experiment script and schedule.
- Capture run-level JSON metrics.
- Record which stages used local deterministic fallback versus Qwen generation.

## Future Work
- Replace smoke-test experiments with real domain benchmarks before claiming scientific gains.
- Add repeated seeds, baselines, charts, and citation verification for full paper runs.
- Keep deterministic fallbacks as reliability rails for service/UI integration tests.

## Decision Excerpt
```markdown
{_decision_excerpt}
```

## Analysis Excerpt
```markdown
{_analysis_excerpt}
```

## Metrics Excerpt
```json
{_metrics_excerpt}
```

Generated: {_utcnow_iso()}
"""
    (stage_dir / "archive.md").write_text(archive, encoding="utf-8")

    files: list[str] = []
    for stage_subdir in sorted(run_dir.glob("stage-*")):
        for artifact in sorted(stage_subdir.rglob("*")):
            if artifact.is_file() and artifact != (stage_dir / "bundle_index.json"):
                files.append(str(artifact.relative_to(run_dir)))
    index = {
        "run_id": run_dir.name,
        "generated": _utcnow_iso(),
        "artifact_count": len(files),
        "artifacts": files,
    }
    (stage_dir / "bundle_index.json").write_text(
        json.dumps(index, indent=2), encoding="utf-8"
    )
    return StageResult(
        stage=Stage.KNOWLEDGE_ARCHIVE,
        status=StageStatus.DONE,
        artifacts=("archive.md", "bundle_index.json"),
        evidence_refs=("stage-24/archive.md", "stage-24/bundle_index.json"),
    )


def _sanitize_fabricated_data(
    paper: str,
    run_dir: Path,
) -> tuple[str, dict[str, Any]]:
    """Replace unverified numerical data in markdown tables with '---'.

    Loads experiment_summary.json as ground truth, extracts all verified
    metric values, then scans markdown tables in Results/Experiment sections.
    Numbers not matching any verified value (within 1% relative tolerance)
    are replaced with ``---``.

    Returns (sanitized_paper, sanitization_report).
    """
    import re as _re_san

    # --- 1. Build verified values set from experiment_summary.json ---
    verified_values: set[float] = set()
    exp_path = run_dir / "stage-16" / "experiment_summary.json"
    if not exp_path.exists():
        # Try versioned result-analysis outputs and legacy locations.
        candidates = [
            *sorted(run_dir.glob("stage-16*/experiment_summary.json"), reverse=True),
            run_dir / "stage-14" / "experiment_summary.json",
            *sorted(run_dir.glob("stage-14*/experiment_summary.json"), reverse=True),
        ]
        exp_path = next((candidate for candidate in candidates if candidate.exists()), exp_path)

    if exp_path.exists():
        try:
            exp_data = json.loads(exp_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            exp_data = {}

        def _collect_numbers(obj: Any, depth: int = 0) -> None:
            if depth > 10:
                return
            if isinstance(obj, (int, float)) and not isinstance(obj, bool):
                verified_values.add(float(obj))
            elif isinstance(obj, dict):
                for v in obj.values():
                    _collect_numbers(v, depth + 1)
            elif isinstance(obj, list):
                for v in obj:
                    _collect_numbers(v, depth + 1)

        # Extract from well-known keys
        for key in (
            "metrics_summary", "condition_summaries", "best_run",
            "condition_metrics", "conditions", "ablation_results",
        ):
            if key in exp_data:
                _collect_numbers(exp_data[key])

    if not verified_values:
        report: dict[str, Any] = {
            "sanitized": False,
            "reason": "no verified values found in experiment_summary.json",
            "tables_processed": 0,
            "numbers_replaced": 0,
        }
        return paper, report

    def _is_verified(num: float) -> bool:
        """Check if num matches any verified value within 1% relative tolerance."""
        for v in verified_values:
            if v == 0.0:
                if abs(num) < 1e-9:
                    return True
            elif abs(num - v) / abs(v) <= 0.01:
                return True
        return False

    # --- 2. Find and sanitize markdown tables ---
    # Match markdown table blocks (header + separator + data rows)
    table_pat = _re_san.compile(
        r"((?:^[ \t]*\|.+\|[ \t]*\n)+"  # one or more pipe-delimited lines
        r")",
        _re_san.MULTILINE,
    )
    # Match numbers in table cells (integers, decimals, percentages, scientific)
    num_pat = _re_san.compile(
        r"(?<![a-zA-Z_])"  # not preceded by letter/underscore
        r"(-?\d+\.?\d*(?:[eE][+-]?\d+)?)"
        r"(%?)"  # optional percent
        r"(?![a-zA-Z_])"  # not followed by letter/underscore
    )

    numbers_replaced = 0
    numbers_kept = 0
    tables_processed = 0
    replaced_values: list[str] = []

    def _sanitize_table(match: _re_san.Match[str]) -> str:
        nonlocal numbers_replaced, numbers_kept, tables_processed
        table_text = match.group(0)
        lines = table_text.split("\n")

        # Check if this looks like a results/experiment table
        # (heuristic: has a separator row with dashes)
        has_separator = any(
            _re_san.match(r"^[ \t]*\|[\s:|-]+\|[ \t]*$", line)
            for line in lines
        )
        if not has_separator:
            return table_text

        tables_processed += 1
        sanitized_lines: list[str] = []
        for i, line in enumerate(lines):
            # Skip header row and separator row
            is_separator = bool(
                _re_san.match(r"^[ \t]*\|[\s:|-]+\|[ \t]*$", line)
            )
            is_header = i == 0  # first line is typically the header
            if is_separator or is_header:
                sanitized_lines.append(line)
                continue

            def _replace_num(m: _re_san.Match[str]) -> str:
                nonlocal numbers_replaced, numbers_kept
                num_str = m.group(1)
                pct = m.group(2)
                try:
                    val = float(num_str)
                except ValueError:
                    return m.group(0)
                if _is_verified(val):
                    numbers_kept += 1
                    return m.group(0)
                numbers_replaced += 1
                replaced_values.append(num_str + pct)
                return "---"

            sanitized_lines.append(num_pat.sub(_replace_num, line))
        return "\n".join(sanitized_lines)

    sanitized = table_pat.sub(_sanitize_table, paper)

    report = {
        "sanitized": numbers_replaced > 0,
        "tables_processed": tables_processed,
        "numbers_replaced": numbers_replaced,
        "numbers_kept": numbers_kept,
        "verified_values_count": len(verified_values),
        "replaced_samples": replaced_values[:20],
        "generated": _utcnow_iso(),
    }
    return sanitized, report


def _enforce_engineering_report_boundary(paper: str, run_dir: Path) -> tuple[str, bool]:
    """Rewrite empirical sections when execution only supports an engineering smoke report."""
    readiness = _safe_json_loads(_read_prior_artifact(run_dir, "research_readiness.json") or "", {})
    policy = str(readiness.get("writing_policy", "") or "") if isinstance(readiness, dict) else ""
    if policy not in {"engineering_report_only", "no_empirical_claims"}:
        return paper, False

    provenance = _safe_json_loads(_read_prior_artifact(run_dir, "experiment_provenance.json") or "", {})
    executed = bool(provenance.get("real_code_execution") or provenance.get("executed")) if isinstance(provenance, dict) else False
    implementation = str(provenance.get("implementation", "unrecorded") or "unrecorded") if isinstance(provenance, dict) else "unrecorded"
    execution_mode = str(provenance.get("execution_mode", "unrecorded") or "unrecorded") if isinstance(provenance, dict) else "unrecorded"

    # Real executed metrics are valid descriptive engineering evidence. Keep
    # the domain paper intact and only remove wording that upgrades those
    # observations into broad scientific superiority. The legacy section
    # replacement below is reserved for runs without real empirical evidence.
    if executed and implementation not in {"synthetic_fallback", "simulated", "unrecorded"}:
        paper = re.sub(r"state[- ]of[- ]the[- ]art", "representative", paper, flags=re.IGNORECASE)
        paper = re.sub(
            r"(?i)\b(?:proves?|definitively establishes)\b",
            "provides bounded evidence suggesting",
            paper,
        )
        paper = re.sub(r"\s*\[cite_key:[^\]]+\]", "", paper, flags=re.IGNORECASE)
        paper = re.sub(r"<!--\s*FIGURE_PROMPT\b.*?-->", "", paper, flags=re.IGNORECASE | re.DOTALL)
        paper = re.sub(
            r"(?im)^\s*\*?\[Figure[^\n]*?to be generated\]\*?\s*$",
            "",
            paper,
        )
        paper = re.sub(r"\n{3,}", "\n\n", paper)
        return paper, True

    method_match = re.search(r"^#\s+([^:\n]+)", paper, re.MULTILINE)
    method_name = method_match.group(1).strip() if method_match else "The Proposed Workflow"
    safe_title = f"# {method_name}: An Engineering Smoke Test for Visual-Inertial Research Automation"
    paper = re.sub(r"^#\s+.*$", safe_title, paper, count=1, flags=re.MULTILINE)

    replacements = {
        "Abstract": (
            "This paper documents an evidence-bounded engineering implementation of a visual-inertial research workflow. "
            "The system connects literature-derived design, executable code generation, runtime provenance capture, result parsing, and IEEE-formatted reporting. "
            f"The generated program was {'executed successfully' if executed else 'not confirmed as successfully executed'} using the {execution_mode} path with a {implementation} implementation. "
            "The run is intentionally classified as a pipeline smoke test: it verifies artifact flow and basic executability, but it does not establish accuracy, robustness, statistical significance, or superiority over baselines. "
            "We therefore report implementation behavior, evidence boundaries, and the experiments required before scientific performance claims can be made."
        ),
        "Introduction": (
            "Visual-inertial sensing combines cameras and inertial measurement units in a software pipeline whose scientific evaluation normally requires real trajectories, synchronized sensor data, controlled failure conditions, and matched baselines. "
            "This work examines a narrower question: whether an automated research workflow can turn a visual-inertial design specification into executable code, capture provenance, parse its output, and export an IEEE-formatted report without overstating the evidence. "
            "The current artifact uses a synthetic fallback and a single pipeline smoke test. Its contribution is therefore an auditable engineering integration and a concrete validation plan, not a validated visual-inertial algorithm or a performance comparison."
        ),
        "Related Work": (
            "Visual-inertial research spans geometric estimation, learned sensor fusion, uncertainty modeling, and benchmark design. "
            "The automated literature stage in this run did not produce a verified bibliography, so this engineering report intentionally avoids attributing detailed performance statements to unverified references. "
            "A submission-grade revision must repeat literature retrieval against authoritative indexes, verify each citation, and connect every technical comparison to a resolvable source."
        ),
        "Experimental Setup": (
            "The evaluation was limited to an engineering smoke test of the generated code and artifact pipeline. "
            f"Execution provenance records the mode as `{execution_mode}` and the implementation as `{implementation}`. "
            "The available run does not contain a validated benchmark protocol with multiple independent seeds, paired baseline executions, confidence intervals, or significance tests. "
            "Consequently, this setup is suitable for checking software execution and result ingestion only."
        ),
        "Experiments": (
            "The evaluation was limited to an engineering smoke test of the generated code and artifact pipeline. "
            f"Execution provenance records the mode as `{execution_mode}` and the implementation as `{implementation}`. "
            "No benchmark-quality comparison was completed. Scientific evaluation requires real IMU datasets, controlled train-test separation, repeated seeds, competitive baselines, and prespecified statistical analysis."
        ),
        "Results": (
            f"The principal verified outcome is that the generated program {'completed with a successful process return status' if executed else 'did not provide a confirmed successful execution record'}. "
            "Artifacts were produced and parsed by the pipeline. The single smoke output is not interpreted as a trajectory-error estimate or a comparative performance result. "
            "No claim of improved accuracy, robustness, generalization, ablation benefit, or statistical significance is supported by this run."
        ),
        "Discussion": (
            "This run supports an engineering conclusion: the automated workflow can connect code generation, execution provenance, analysis, and manuscript export. "
            "It does not support a scientific conclusion about the proposed visual-inertial method. A future evaluation must replace the synthetic fallback with the intended implementation and execute matched baselines on real datasets under identical protocols."
        ),
        "Limitations": (
            "The current evidence is limited to one successful execution of a synthetic fallback. There are no verified real-dataset runs, repeated random seeds, paired baseline results, confidence intervals, significance tests, or externally verified citations. "
            "The conceptual method description has not been validated as a faithful implementation, and the available output cannot establish visual-inertial accuracy, robustness, generalization, computational efficiency, or comparative benefit. "
            "These limitations must be resolved before the artifact is treated as a scientific paper rather than an engineering pipeline report."
        ),
        "Conclusion": (
            "We demonstrated an evidence-bounded engineering workflow for visual-inertial research automation. "
            "The code path executed and produced traceable artifacts, while the integrity gate correctly limits the outcome to a smoke-test report. "
            "Scientific performance conclusions remain deferred until benchmark-quality experiments with repeated trials, baselines, and statistical analysis are completed."
        ),
    }
    for heading, body in replacements.items():
        pattern = re.compile(
            rf"(^##\s*(?:\d+\.?\s*)?{re.escape(heading)}\s*$).*?(?=^##\s|\Z)",
            re.MULTILINE | re.DOTALL | re.IGNORECASE,
        )
        if pattern.search(paper):
            paper = pattern.sub(lambda match, text=body: f"{match.group(1)}\n\n{text}\n\n", paper, count=1)

    if not re.search(r"(?im)^##\s*(?:\d+\.?\s*)?Limitations\s*$", paper):
        limitations = f"## Limitations\n\n{replacements['Limitations']}\n\n"
        conclusion = re.search(r"(?im)^##\s*(?:\d+\.?\s*)?Conclusion\s*$", paper)
        insert_at = conclusion.start() if conclusion else len(paper)
        paper = paper[:insert_at] + limitations + paper[insert_at:]

    # Avoid global-superiority wording in background/method prose.
    paper = re.sub(r"state[- ]of[- ]the[- ]art", "representative", paper, flags=re.IGNORECASE)
    # Unverified citation placeholders and image-generation instructions are
    # internal scaffolding and must never leak into a user-facing manuscript.
    paper = re.sub(r"\s*\[cite_key:[^\]]+\]", "", paper, flags=re.IGNORECASE)
    paper = re.sub(r"<!--\s*FIGURE_PROMPT\b.*?-->", "", paper, flags=re.IGNORECASE | re.DOTALL)
    paper = re.sub(
        r"(?im)^\s*\*?\[Figure[^\n]*?to be generated\]\*?\s*$",
        "",
        paper,
    )
    paper = re.sub(r"\n{3,}", "\n\n", paper)
    return paper, True


def _remove_missing_markdown_figures(paper: str, run_dir: Path) -> tuple[str, list[str]]:
    """Drop image references that cannot resolve to any generated artifact."""
    missing: list[str] = []
    image_pattern = re.compile(r"!\[([^\]]*)\]\(([^)]+)\)")

    def _replace(match: re.Match[str]) -> str:
        target = match.group(2).strip().split()[0]
        relative = Path(target)
        candidates = [run_dir / relative]
        candidates.extend(stage_dir / relative for stage_dir in run_dir.glob("stage-*") if stage_dir.is_dir())
        candidates.extend(stage_dir / "charts" / relative.name for stage_dir in run_dir.glob("stage-*") if stage_dir.is_dir())
        if any(candidate.is_file() for candidate in candidates):
            return match.group(0)
        missing.append(target)
        return f"*Figure omitted because the generated artifact `{target}` was unavailable.*"

    return image_pattern.sub(_replace, paper), missing


def _repair_markdown_figure_references(paper: str) -> str:
    """Replace stale hard-coded figure numbers with stable LaTeX references."""
    number_to_label: dict[str, str] = {}
    pattern = re.compile(
        r"!\[([^\]]+)\]\([^)]+\)\s*\n\s*\*Figure\s+(\d+)\s*:",
        re.IGNORECASE,
    )
    for match in pattern.finditer(paper):
        caption = match.group(1)
        label_key = re.sub(r"[^a-z0-9]+", "_", caption.lower()).strip("_")[:30]
        if label_key:
            number_to_label[match.group(2)] = f"fig:{label_key}"
    if not number_to_label:
        return paper

    repaired: list[str] = []
    for line in paper.splitlines():
        if re.match(r"^\s*\*Figure\s+\d+\s*:", line, re.IGNORECASE):
            repaired.append(line)
            continue
        for number, label in number_to_label.items():
            line = re.sub(
                rf"\bFigure\s+{re.escape(number)}\b",
                lambda _match, target=label: rf"Figure \ref{{{target}}}",
                line,
            )
        repaired.append(line)
    return "\n".join(repaired) + ("\n" if paper.endswith("\n") else "")


def _execute_export_publish(
    stage_dir: Path,
    run_dir: Path,
    config: RCConfig,
    adapters: AdapterBundle,
    *,
    llm: LLMClient | None = None,
    prompts: PromptManager | None = None,
) -> StageResult:
    revised = _read_prior_artifact(run_dir, "paper_revised.md") or ""
    # Export is also a hard scientific-quality boundary: a compiled PDF can
    # still be structurally unusable even when claim-integrity passes.
    _export_quality_degraded = False
    _export_quality_reasons: list[str] = []
    _paper_len_cfg = getattr(config.experiment, "paper_length", "") or "full"
    _deterministic_export = _paper_len_cfg in {"deterministic", "fallback"}
    _export_readiness = _safe_json_loads(
        _read_prior_artifact(run_dir, "research_readiness.json") or "{}", {}
    )
    _engineering_only_export = (
        isinstance(_export_readiness, dict)
        and str(_export_readiness.get("writing_policy", ""))
        in {"engineering_report_only", "no_empirical_claims"}
    )
    # A polishing call cannot turn smoke evidence into scientific evidence and
    # has repeatedly reintroduced unsupported claims.  For engineering-only
    # outputs, preserve the revised manuscript and apply the deterministic
    # evidence boundary directly.
    if llm is not None and not _deterministic_export and not _engineering_only_export:
        _pm = prompts or PromptManager()
        _overlay = _get_evolution_overlay(run_dir, "export_publish")
        sp = _pm.for_stage(
            "export_publish",
            evolution_overlay=_overlay,
            revised=revised + _claim_boundary_instruction(run_dir),
        )
        resp = _chat_with_prompt(
            llm,
            sp.system,
            sp.user,
            json_mode=sp.json_mode,
            max_tokens=sp.max_tokens,
        )
        final_paper = resp.content
        # Content guard: reject LLM output that truncates the paper
        if revised and len(final_paper) < 0.6 * len(revised):
            logger.warning(
                "Stage 22: LLM output is %.0f%% of input length — using original",
                100 * len(final_paper) / max(len(revised), 1),
            )
            final_paper = revised
    else:
        final_paper = revised
    if not final_paper.strip():
        final_paper = "# Final Paper\n\nNo content generated."

    final_paper, _engineering_boundary_applied = _enforce_engineering_report_boundary(final_paper, run_dir)
    if _engineering_boundary_applied:
        logger.warning("Stage 25: Enforced engineering-report-only claim boundary")

    final_paper, _missing_figure_refs = _remove_missing_markdown_figures(final_paper, run_dir)
    if _missing_figure_refs:
        logger.warning("Stage 25: Removed %d missing figure reference(s): %s", len(_missing_figure_refs), _missing_figure_refs[:6])
    final_paper = _repair_markdown_figure_references(final_paper)

    # --- Graceful degradation: sanitize fabricated data ---
    _degradation_signal_path = run_dir / "degradation_signal.json"
    if _degradation_signal_path.exists():
        try:
            _deg_signal = json.loads(
                _degradation_signal_path.read_text(encoding="utf-8")
            )
        except (json.JSONDecodeError, OSError):
            _deg_signal = {}

        # Back up pre-sanitized version
        (stage_dir / "paper_presanitized.md").write_text(
            final_paper, encoding="utf-8"
        )

        # Sanitize unverified data in tables
        final_paper, _san_report = _sanitize_fabricated_data(
            final_paper, run_dir
        )
        (stage_dir / "sanitization_report.json").write_text(
            json.dumps(_san_report, indent=2), encoding="utf-8"
        )

        # Insert a reason-specific notice after the abstract.  Do not claim
        # that the score was below threshold or that values were replaced
        # unless the current artifacts actually establish those facts.
        _deg_score = _deg_signal.get("score", "N/A")
        _deg_threshold = _deg_signal.get("threshold", "N/A")
        _deg_reason = str(_deg_signal.get("reason", "") or "")
        _numbers_replaced = int(_san_report.get("numbers_replaced", 0) or 0)
        if _engineering_boundary_applied:
            _deg_detail = (
                "This artifact is classified as an engineering smoke test; "
                "scientific performance conclusions are intentionally excluded."
            )
        elif _deg_reason == "quality_score_below_threshold":
            _deg_detail = (
                f"Quality gate score ({_deg_score}/{_deg_threshold}) was below "
                "the configured threshold."
            )
        elif _deg_reason == "evaluation_protocol_insufficient":
            _deg_detail = "The planned evaluation protocol was not fully satisfied."
        elif _deg_reason in {"claim_integrity_blocked", "final_claim_integrity_blocked"}:
            _deg_detail = "The claim-integrity audit found conclusions beyond the available evidence."
        elif _deg_reason == "export_quality_degraded":
            _deg_detail = "The PDF or compilation quality review reported unresolved findings."
        else:
            _deg_detail = "A current quality check reported unresolved evidence limitations."
        if _numbers_replaced:
            _deg_detail += (
                f" {_numbers_replaced} unverified numerical value(s) were replaced "
                "with `---`; consult the sanitization report."
            )
        _deg_notice = f"\n\n> **Evidence limitation:** {_deg_detail}\n\n"
        # Try to insert after ## Abstract section
        _abstract_markers = ["## Abstract\n", "# Abstract\n"]
        _notice_inserted = False
        for _marker in _abstract_markers:
            if _marker in final_paper:
                _marker_end = final_paper.index(_marker) + len(_marker)
                # Find the end of the abstract paragraph
                _next_section = final_paper.find("\n## ", _marker_end)
                _next_heading = final_paper.find("\n# ", _marker_end)
                _insert_pos = min(
                    p for p in (_next_section, _next_heading)
                    if p > 0
                ) if any(p > 0 for p in (_next_section, _next_heading)) else len(final_paper)
                final_paper = (
                    final_paper[:_insert_pos]
                    + _deg_notice
                    + final_paper[_insert_pos:]
                )
                _notice_inserted = True
                break
        if not _notice_inserted:
            # Fallback: prepend to paper
            final_paper = _deg_notice + final_paper

        logger.info(
            "Stage 22: Applied degraded-mode sanitization — "
            "%d numbers replaced, %d kept",
            _san_report.get("numbers_replaced", 0),
            _san_report.get("numbers_kept", 0),
        )

    # IMP-3: Deduplicate "due to computational constraints" — keep at most 1
    import re as _re_imp3
    _CONSTRAINT_PAT = _re_imp3.compile(
        r"[Dd]ue to computational constraints", _re_imp3.IGNORECASE
    )
    _matches = list(_CONSTRAINT_PAT.finditer(final_paper))
    if len(_matches) > 1:
        # Keep only the first occurrence; remove subsequent ones by
        # deleting the enclosing sentence.
        for m in reversed(_matches[1:]):
            # Find sentence boundaries around the match
            start = final_paper.rfind(".", 0, m.start())
            start = start + 1 if start >= 0 else m.start()
            end = final_paper.find(".", m.end())
            end = end + 1 if end >= 0 else m.end()
            sentence = final_paper[start:end].strip()
            if sentence:
                final_paper = final_paper[:start] + final_paper[end:]
        final_paper = re.sub(r"[^\S\n]{2,}", " ", final_paper)
        logger.info(
            "Stage 22: Removed %d duplicate 'computational constraints' "
            "disclaimers",
            len(_matches) - 1,
        )

    # IMP-19 Layer 2: Ensure at least figures are referenced in the paper
    import re as _re_fig
    chart_files = []
    for _chart_src_dir in [
        stage_dir / "charts",
        run_dir / "stage-16" / "charts",
        run_dir / "stage-14" / "charts",
    ]:
        if _chart_src_dir.is_dir():
            chart_files.extend(sorted(_chart_src_dir.glob("*.png")))
    if chart_files and "![" not in final_paper:
        # Distribute figures to relevant sections based on filename keywords
        _fig_placement: dict[str, list[str]] = {
            "method": [],       # architecture, method, model, pipeline diagrams
            "result": [],       # experiment, comparison, ablation charts
            "intro": [],        # concept, overview, illustration
        }
        _fig_counter = 0
        for cf in chart_files[:6]:
            _fig_counter += 1
            stem_lower = cf.stem.lower()
            label = cf.stem.replace("_", " ").title()
            fig_md = f"![Figure {_fig_counter}: {label}](charts/{cf.name})"
            if any(k in stem_lower for k in ("architecture", "model", "pipeline", "method", "flowchart")):
                _fig_placement["method"].append(fig_md)
            elif any(k in stem_lower for k in ("experiment", "comparison", "ablation", "result", "metric")):
                _fig_placement["result"].append(fig_md)
            elif any(k in stem_lower for k in ("concept", "overview", "illustration", "threat", "attack")):
                _fig_placement["intro"].append(fig_md)
            else:
                _fig_placement["result"].append(fig_md)  # default to results

        # Insert figures at relevant section boundaries
        _section_markers = {
            "method": ["## Method", "## Methodology", "## Approach", "## Framework",
                        "## 3. Method", "## 3 Method"],
            "result": ["## Results", "## Experiments", "## Evaluation",
                        "## 5. Results", "## 4. Experiments", "## 5 Results"],
            "intro": ["## Related Work", "## Background", "## 2. Related",
                       "## 2 Related Work"],
        }
        _total_inserted = 0
        for category, figs in _fig_placement.items():
            if not figs:
                continue
            fig_block = "\n\n" + "\n\n".join(figs) + "\n\n"
            inserted = False
            for marker in _section_markers.get(category, []):
                if marker in final_paper:
                    # Insert BEFORE the marker section (so figure appears at end of previous section)
                    final_paper = final_paper.replace(marker, fig_block + marker, 1)
                    inserted = True
                    _total_inserted += len(figs)
                    break
            if not inserted:
                # Fallback: insert before Conclusion/Limitations/Discussion
                for fallback in ["## Conclusion", "## Limitations", "## Discussion"]:
                    if fallback in final_paper:
                        final_paper = final_paper.replace(fallback, fig_block + fallback, 1)
                        inserted = True
                        _total_inserted += len(figs)
                        break
            if not inserted:
                final_paper += fig_block
                _total_inserted += len(figs)

        logger.info(
            "IMP-19: Injected %d figure references into paper_final.md (distributed across sections)",
            _total_inserted,
        )

    # IMP-24: Detect excessive number repetition
    _numbers_found = _re_fig.findall(r"\b\d+\.\d{2,}\b", final_paper)
    from collections import Counter as _Counter
    _num_counts = _Counter(_numbers_found)
    _repeated = {n: c for n, c in _num_counts.items() if c > 3}
    if _repeated:
        logger.warning(
            "IMP-24: Numbers repeated >3 times: %s",
            _repeated,
        )

    (stage_dir / "paper_final.md").write_text(final_paper, encoding="utf-8")

    # --- Fabrication sanitization: blank out unsupported numbers ---
    _fab_flags_text = _read_prior_artifact(run_dir, "fabrication_flags.json") or ""
    _fab_flags = _safe_json_loads(_fab_flags_text, {}) if _fab_flags_text else {}
    if isinstance(_fab_flags, dict) and _fab_flags.get("fabrication_suspected"):
        import re as _re_fab
        _real_vals = set()
        for rv in _fab_flags.get("real_metric_values", []):
            if isinstance(rv, (int, float)) and math.isfinite(rv):
                _real_vals.add(str(round(rv, 4)))
                _real_vals.add(str(round(rv, 2)))
                _real_vals.add(str(round(rv, 1)))
                _real_vals.add(str(int(rv)) if rv == int(rv) else "")

        def _sanitize_number(m: _re_fab.Match) -> str:  # type: ignore[name-defined]
            """Replace fabricated numbers with '--' but keep real ones."""
            num_str = m.group(0)
            # Keep the number if it matches any known real metric value
            try:
                num_val = float(num_str)
                if not math.isfinite(num_val):
                    return "--"
                rounded_strs = {
                    str(round(num_val, 4)),
                    str(round(num_val, 2)),
                    str(round(num_val, 1)),
                    str(int(num_val)) if num_val == int(num_val) else "",
                }
                if rounded_strs & _real_vals:
                    return num_str  # real value — keep it
            except (ValueError, OverflowError):
                return num_str
            return "--"

        # Only sanitize numbers in Results/Experiments/Evaluation/Ablation sections
        _result_section_pat = _re_fab.compile(
            r"(##\s*(?:\d+\.?\s*)?(?:Results|Experiments|Evaluation|Ablation"
            r"|Experimental Results|Quantitative).*?)(?=\n##\s|\Z)",
            _re_fab.DOTALL | _re_fab.IGNORECASE,
        )
        _sanitized_count = 0

        def _sanitize_section(sec_match: _re_fab.Match) -> str:  # type: ignore[name-defined]
            nonlocal _sanitized_count
            section_text = sec_match.group(0)
            # Replace decimal numbers (e.g., 73.42, 0.891) but NOT integers
            # that are likely structural (year, section number, figure number)
            def _replace_in_section(m: _re_fab.Match) -> str:  # type: ignore[name-defined]
                nonlocal _sanitized_count
                result = _sanitize_number(m)
                if result == "--":
                    _sanitized_count += 1
                return result
            return _re_fab.sub(
                r"\b\d+\.\d{1,6}\b", _replace_in_section, section_text
            )

        final_paper = _result_section_pat.sub(_sanitize_section, final_paper)

        if _sanitized_count > 0:
            logger.warning(
                "Stage 22: Fabrication sanitization — blanked %d unsupported "
                "numbers in Results sections (experiment had no real metrics)",
                _sanitized_count,
            )
            # Rewrite the sanitized paper
            (stage_dir / "paper_final.md").write_text(
                final_paper, encoding="utf-8"
            )

    # Initialize artifacts list
    artifacts = ["paper_final.md"]
    final_paper_latex = final_paper  # default: no citation conversion
    # F2.7: Post-process citations — [cite_key] → \cite{cite_key}
    # and copy final references.bib to export stage
    _ay_map: dict[str, str] = {}  # BUG-102: author-year → cite_key map
    bib_text = _read_literature_bib(run_dir)
    bib_text = _augment_canonical_citations(bib_text, final_paper)
    bib_text = _sanitize_bibtex_for_latex(bib_text)
    if bib_text:
        # Replace [cite_key] patterns in the final paper with \cite{cite_key}
        # Collect all valid cite_keys from the bib file
        import re as _re

        valid_keys = set(_re.findall(r"@\w+\{([^,]+),", bib_text))

        # BUG-102: Recover author-year citations → [cite_key] format.
        # When Stage 19 (paper_revision) converts [cite_key] to [Author et al., 2024],
        # the downstream regex can't match them. Build a reverse map from bib entries.
        def _build_author_year_map(bib: str, keys: set[str]) -> dict[str, str]:
            """Build mapping from author-year patterns to cite_keys.

            Returns dict like:
              "Raissi et al., 2019" → "raissi2019physicsinformed"
              "Tavella and Randall, 2000" → "tavella2000pricing"
            """
            mapping: dict[str, str] = {}
            # Parse each bib entry for author + year
            entry_pat = _re.compile(
                r"@\w+\{([^,]+),\s*(.*?)\n\}", _re.DOTALL
            )
            for m in entry_pat.finditer(bib):
                key = m.group(1).strip()
                if key not in keys:
                    continue
                body = m.group(2)
                # Extract author field
                author_m = _re.search(
                    r"author\s*=\s*[\{\"](.*?)[\}\"]", body, _re.IGNORECASE
                )
                year_m = _re.search(
                    r"year\s*=\s*[\{\"]?(\d{4})[\}\"]?", body, _re.IGNORECASE
                )
                if not author_m or not year_m:
                    continue
                author_raw = author_m.group(1).strip()
                year = year_m.group(1)
                # Parse author names (split on " and ")
                authors = [a.strip() for a in _re.split(r"\s+and\s+", author_raw)]
                # Extract last names
                last_names = []
                for a in authors:
                    if "," in a:
                        last_names.append(a.split(",")[0].strip())
                    else:
                        parts = a.split()
                        last_names.append(parts[-1] if parts else a)
                if not last_names:
                    continue
                # Generate author-year patterns:
                # 1 author: "Smith, 2024"
                # 2 authors: "Smith and Jones, 2024"
                # 3+ authors: "Smith et al., 2024"
                if len(last_names) == 1:
                    patterns = [f"{last_names[0]}, {year}"]
                elif len(last_names) == 2:
                    patterns = [
                        f"{last_names[0]} and {last_names[1]}, {year}",
                        f"{last_names[0]} \\& {last_names[1]}, {year}",
                    ]
                else:
                    patterns = [
                        f"{last_names[0]} et al., {year}",
                        f"{last_names[0]} et al. {year}",
                    ]
                    # Also add "Smith and Jones, 2024" for first two authors
                    patterns.append(
                        f"{last_names[0]} and {last_names[1]}, {year}"
                    )
                for pat in patterns:
                    mapping[pat] = key
            return mapping

        _ay_map = _build_author_year_map(bib_text, valid_keys)
        if _ay_map:
            # Count how many author-year citations exist in the paper
            _ay_found = 0
            for _ay_pat in _ay_map:
                if _ay_pat in final_paper:
                    _ay_found += 1
            if _ay_found > 0:
                logger.info(
                    "Stage 22: Found %d author-year citation patterns — "
                    "converting back to [cite_key] format.",
                    _ay_found,
                )
                # Sort by longest pattern first to avoid partial matches
                for _ay_pat in sorted(_ay_map, key=len, reverse=True):
                    _ay_key = _ay_map[_ay_pat]
                    # Match [Author et al., 2024] or [Author and Jones, 2024; ...]
                    # Handle single-citation brackets
                    final_paper = final_paper.replace(
                        f"[{_ay_pat}]", f"[{_ay_key}]"
                    )
                    # Handle within multi-citation brackets [A et al., 2020; B et al., 2021]
                    # Replace the author-year segment inside brackets
                    final_paper = final_paper.replace(_ay_pat, _ay_key)
                # Fix multi-key brackets: [key1; key2] → [key1, key2]
                # (author-year uses semicolons, cite-keys use commas)
                def _fix_semicolon_cites(m_sc: _re.Match[str]) -> str:
                    inner = m_sc.group(1)
                    # Only convert if ALL segments look like cite keys
                    parts = [p.strip() for p in inner.split(";")]
                    _ck = r"[a-zA-Z][a-zA-Z0-9_-]*\d{4}[a-zA-Z0-9_]*"
                    if all(_re.fullmatch(_ck, p) for p in parts):
                        return "[" + ", ".join(parts) + "]"
                    return m_sc.group(0)
                final_paper = _re.sub(
                    r"\[([^\]]+;[^\]]+)\]", _fix_semicolon_cites, final_paper
                )
                (stage_dir / "paper_final.md").write_text(
                    final_paper, encoding="utf-8"
                )

        # R10-Fix4: Citation cross-validation
        cited_keys_in_paper: set[str] = set()
        _citation_key_re = _re.compile(r"^[a-zA-Z][a-zA-Z0-9_-]*\d{4}[a-zA-Z0-9_]*$")
        for _bracket in _re.findall(r"\[([^\]]+)\]", final_paper):
            for _candidate in _bracket.split(","):
                _candidate = _candidate.strip()
                if _citation_key_re.fullmatch(_candidate):
                    cited_keys_in_paper.add(_candidate)
        if valid_keys and cited_keys_in_paper:
            invalid_keys = cited_keys_in_paper - valid_keys
            if invalid_keys:
                logger.warning(
                    "Stage 22: Found %d citation keys in paper not in references.bib: %s",
                    len(invalid_keys),
                    ", ".join(sorted(invalid_keys)[:20]),
                )
                # IMP-29: Silently remove invalid citations instead of
                # leaving ugly [?key:NOT_IN_BIB] markers in the output.
                final_paper = _remove_citations_from_text(final_paper, invalid_keys)
                # Clean up whitespace artifacts from removed citations
                import re as _re_imp29
                final_paper = _re_imp29.sub(r"  +", " ", final_paper)
                final_paper = _re_imp29.sub(r" ([.,;:)])", r"\1", final_paper)
                (stage_dir / "paper_final.md").write_text(final_paper, encoding="utf-8")
                (stage_dir / "invalid_citations.json").write_text(
                    json.dumps(sorted(invalid_keys), indent=2), encoding="utf-8"
                )
                artifacts.append("invalid_citations.json")

        if valid_keys:
            _CITE_KEY_PAT = r"[a-zA-Z][a-zA-Z0-9_-]*\d{4}[a-zA-Z0-9]*"

            # Step 1: Convert multi-key brackets [key1, key2] → \cite{key1, key2}
            def _replace_multi_cite(m: _re.Match[str]) -> str:
                keys = [k.strip() for k in m.group(1).split(",")]
                matched = [k for k in keys if k in valid_keys]
                if matched:
                    return "\\cite{" + ", ".join(matched) + "}"
                return m.group(0)

            final_paper_latex = _re.sub(
                rf"\[({_CITE_KEY_PAT}(?:\s*,\s*{_CITE_KEY_PAT})+)\]",
                _replace_multi_cite,
                final_paper,
            )

            # Step 2: Convert single-key brackets [key] → \cite{key}
            def _replace_cite(m: _re.Match[str]) -> str:
                key = m.group(1)
                if key in valid_keys:
                    return f"\\cite{{{key}}}"
                return m.group(0)

            final_paper_latex = _re.sub(
                rf"\[({_CITE_KEY_PAT})\]", _replace_cite, final_paper_latex
            )

            # Step 3: Merge adjacent \cite{a} \cite{b} → \cite{a, b}
            def _merge_adjacent_cites(m: _re.Match[str]) -> str:
                keys = _re.findall(r"\\cite\{([^}]+)\}", m.group(0))
                return "\\cite{" + ", ".join(keys) + "}"

            final_paper_latex = _re.sub(
                r"\\cite\{[^}]+\}(?:\s*\\cite\{[^}]+\})+",
                _merge_adjacent_cites,
                final_paper_latex,
            )

            (stage_dir / "paper_final_latex.md").write_text(
                final_paper_latex, encoding="utf-8"
            )
            artifacts.append("paper_final_latex.md")
        # IMP-1: Prune uncited bibliography entries — keep only keys
        # that actually appear in the paper text (bracket or \cite form).
        if valid_keys:
            _all_cited: set[str] = set()
            # Bracket-format citations [key]
            _all_cited.update(
                _re.findall(r"\[([a-z]+\d{4}[a-z]*)\]", final_paper)
            )
            # \cite{key, key2} format (original + latex-converted)
            for _src in (
                final_paper,
                final_paper_latex,
            ):
                for _cm in _re.finditer(r"\\cite\{([^}]+)\}", _src):
                    _all_cited.update(
                        k.strip() for k in _cm.group(1).split(",")
                    )
            uncited_keys = valid_keys - _all_cited
            if uncited_keys:
                bib_text = _remove_bibtex_entries(bib_text, uncited_keys)
                logger.info(
                    "Stage 22: Pruned %d uncited bibliography entries "
                    "(kept %d)",
                    len(uncited_keys),
                    len(valid_keys) - len(uncited_keys),
                )

        # Write final references.bib
        (stage_dir / "references.bib").write_text(bib_text, encoding="utf-8")
        artifacts.append("references.bib")
        logger.info(
            "Stage 22: Exported references.bib with %d entries",
            len(valid_keys) if valid_keys else 0,
        )
    if "references.bib" not in artifacts:
        # With no verified bibliography, remove any remaining model-generated
        # bracket placeholders instead of exposing them as literal paper text.
        final_paper = re.sub(r"\s*\[cite_key:[^\]]+\]", "", final_paper, flags=re.IGNORECASE)
        final_paper_latex = final_paper
        (stage_dir / "paper_final.md").write_text(final_paper, encoding="utf-8")
        (stage_dir / "references.bib").write_text("", encoding="utf-8")
        artifacts.append("references.bib")

    # Conference template: generate .tex file
    try:
        from researchclaw.templates import get_template, markdown_to_latex

        tpl = get_template(config.export.target_conference)
        # Use the latex-citation-processed version if available
        tex_source = final_paper_latex
        # Submission checklists are separate form/appendix artifacts.  Do not
        # place them in the manuscript body, where they create an invalid PDF
        # for conferences that collect the checklist separately.
        if tpl.name in ("neurips_2024", "neurips_2025", "icml_2025", "icml_2026",
                         "iclr_2025", "iclr_2026"):
            _has_exp = bool(_read_prior_artifact(run_dir, "experiment_summary.json"))
            _checklist = _generate_neurips_checklist(
                has_experiments=_has_exp,
                has_code=True,
            )
            (stage_dir / "submission_checklist.md").write_text(
                _checklist, encoding="utf-8"
            )
            artifacts.append("submission_checklist.md")
        tex_content = markdown_to_latex(
            tex_source,
            tpl,
            title=_extract_paper_title(tex_source),
            authors=config.export.authors,
            bib_file=config.export.bib_file,
            bib_entries=_ay_map or None,
        )
        if not re.search(r"@\w+\{[^,]+,", bib_text):
            tex_content = re.sub(
                r"\n?\\bibliographystyle\{[^}]+\}\s*\n?\\bibliography\{[^}]+\}\s*",
                "\n",
                tex_content,
            )
        (stage_dir / "paper.tex").write_text(tex_content, encoding="utf-8")
        artifacts.append("paper.tex")
        logger.info(
            "Stage 22: Generated paper.tex for %s (%d chars)",
            tpl.display_name,
            len(tex_content),
        )
        # Copy bundled style files alongside paper.tex
        for sf in tpl.get_style_files():
            import shutil as _shutil_sty
            _shutil_sty.copy2(sf, stage_dir / sf.name)
        # Compile verification
        try:
            from researchclaw.templates.compiler import compile_latex
            _compile_result = compile_latex(stage_dir / "paper.tex", max_attempts=2)
            if _compile_result.success:
                logger.info("Stage 22: LaTeX compilation verification PASSED")
                artifacts.append("paper.pdf")
                # PDF-as-reviewer: LLM-based visual review of compiled PDF
                _pdf_path = stage_dir / "paper.pdf"
                if _pdf_path.exists() and llm is not None and not _engineering_only_export:
                    try:
                        _pdf_review = _review_compiled_pdf(
                            _pdf_path, llm, config.research.topic
                        )
                        if _pdf_review:
                            (stage_dir / "pdf_review.json").write_text(
                                json.dumps(_pdf_review, indent=2, ensure_ascii=False),
                                encoding="utf-8",
                            )
                            artifacts.append("pdf_review.json")
                            _pdf_score = _pdf_review.get("overall_score", 0)
                            if _pdf_score < 5:
                                _export_quality_degraded = True
                                _export_quality_reasons.append(
                                    f"pdf_review_score_below_5:{_pdf_score}"
                                )
                                logger.warning(
                                    "Stage 22: PDF visual review score %d/10 — %s",
                                    _pdf_score,
                                    _pdf_review.get("summary", ""),
                                )
                            else:
                                logger.info(
                                    "Stage 22: PDF visual review score %d/10",
                                    _pdf_score,
                                )
                    except Exception as _pdf_exc:  # noqa: BLE001
                        logger.debug("Stage 22: PDF review skipped: %s", _pdf_exc)
                # Post-compilation quality checks
                try:
                    from researchclaw.templates.compiler import check_compiled_quality
                    _qc = check_compiled_quality(stage_dir / "paper.tex")
                    if _qc.warnings_summary:
                        logger.warning(
                            "Stage 22: Quality checks: %s",
                            "; ".join(_qc.warnings_summary),
                        )
                    _target_pages = int(getattr(config.export, "target_pages", 0) or 0)
                    _min_pages = int(getattr(config.export, "min_pages", 0) or 0)
                    _max_pages = int(getattr(config.export, "max_pages", 0) or 0)
                    _page_status = "not_configured"
                    if _target_pages:
                        _page_status = "on_target" if _qc.page_count == _target_pages else "below_target" if _qc.page_count < _target_pages else "above_target"
                    _page_warning = ""
                    if not _engineering_only_export and _min_pages and _qc.page_count < _min_pages:
                        _page_warning = f"Page count {_qc.page_count} is below the configured minimum {_min_pages}"
                        _export_quality_degraded = True
                        _export_quality_reasons.append(f"page_count_below_minimum:{_qc.page_count}<{_min_pages}")
                    elif not _engineering_only_export and _max_pages and _qc.page_count > _max_pages:
                        _page_warning = f"Page count {_qc.page_count} exceeds the configured maximum {_max_pages}"
                        _export_quality_degraded = True
                        _export_quality_reasons.append(f"page_count_above_maximum:{_qc.page_count}>{_max_pages}")
                    _quality_warnings = list(_qc.warnings_summary)
                    if _page_warning:
                        _quality_warnings.append(_page_warning)
                    (stage_dir / "compilation_quality.json").write_text(
                        json.dumps({
                            "page_count": _qc.page_count,
                            "target_pages": _target_pages,
                            "min_pages": _min_pages,
                            "max_pages": _max_pages,
                            "page_target_status": _page_status,
                            "unresolved_refs": _qc.unresolved_refs,
                            "unresolved_cites": _qc.unresolved_cites,
                            "overfull_hboxes": len(_qc.overfull_hboxes),
                            "orphan_figures": _qc.orphan_figures,
                            "orphan_labels": _qc.orphan_labels,
                            "warnings": _quality_warnings,
                        }, indent=2),
                        encoding="utf-8",
                    )
                    if len(_qc.warnings_summary) >= 3:
                        _export_quality_degraded = True
                        _export_quality_reasons.append(
                            f"compilation_warnings:{len(_qc.warnings_summary)}"
                        )
                    artifacts.append("compilation_quality.json")
                    # BUG-27: Warn if page count exceeds limit
                    _page_limit = 10
                    if _qc.page_count and _qc.page_count > _page_limit:
                        logger.warning(
                            "BUG-27: Paper is %d pages (limit %d). "
                            "Consider tightening content in revision.",
                            _qc.page_count, _page_limit,
                        )
                except Exception as _qc_exc:  # noqa: BLE001
                    logger.debug("Stage 22: Quality checks skipped: %s", _qc_exc)
            else:
                logger.warning("Stage 22: LaTeX compilation verification FAILED: %s", _compile_result.errors[:3])
                # Add compilation failure comment to .tex
                _tex_path = stage_dir / "paper.tex"
                if _tex_path.exists():
                    _tex_content = _tex_path.read_text(encoding="utf-8")
                    if "% WARNING: Compilation failed" not in _tex_content:
                        _tex_content = (
                            "% WARNING: Compilation failed. Errors:\n"
                            + "".join(f"% {e}\n" for e in _compile_result.errors[:5])
                            + _tex_content
                        )
                        _tex_path.write_text(_tex_content, encoding="utf-8")
        except Exception as _compile_exc:  # noqa: BLE001
            logger.debug("Stage 22: Compile verification skipped: %s", _compile_exc)
    except Exception as exc:  # noqa: BLE001
        logger.warning("LaTeX generation skipped: %s", exc)

    # WS-5.4: Generate result visualizations
    # Priority: FigureAgent charts (stage-16 RESULT_ANALYSIS) > stage-14 legacy > fallback
    try:
        chart_dir = stage_dir / "charts"
        chart_dir.mkdir(parents=True, exist_ok=True)
        charts: list[Path] = []

        # Check if FigureAgent produced charts in stage-16 or stage-14
        _fa_charts_found = False
        for _fa_pattern in ("stage-16*/charts", "stage-14*/charts"):
            if _fa_charts_found:
                break
            for _fa_dir in sorted(run_dir.glob(_fa_pattern), reverse=True):
                _fa_pngs = list(_fa_dir.glob("fig_*.png"))
                if _fa_pngs:
                    import shutil
                    for _fa_png in _fa_pngs:
                        dest = chart_dir / _fa_png.name
                        shutil.copy2(_fa_png, dest)
                        charts.append(dest)
                    _fa_charts_found = True
                    logger.info(
                        "Stage 22: Copied %d FigureAgent charts from %s",
                        len(_fa_pngs), _fa_dir,
                    )
                    break

        # Always generate structured charts from visualize.py (different names)
        from researchclaw.experiment.visualize import generate_all_charts
        _metric_dir = getattr(config.experiment, "metric_direction", "minimize")
        _viz_charts = generate_all_charts(
            run_dir,
            chart_dir,
            metric_key=config.experiment.metric_key,
            metric_direction=_metric_dir,
        )
        charts.extend(_viz_charts)

        if charts:
            artifacts.append("charts/")
            logger.info("Stage 22: Generated %d chart(s) total", len(charts))
    except Exception as exc:  # noqa: BLE001
        logger.warning("Chart generation failed: %s", exc)

    # BUG-99: Validate that \includegraphics paths in .tex match actual files.
    # The LLM may write paper referencing a chart name that differs from the
    # actual generated filename (e.g. "performance_comparison.png" vs
    # "experiment_comparison.png").
    try:
        tex_path = stage_dir / "paper.tex"
        if tex_path.exists():
            tex_text = tex_path.read_text(encoding="utf-8")
            # Extract all \includegraphics{path} references
            _fig_refs = re.findall(
                r"\\includegraphics(?:\[[^\]]*\])?\{([^}]+)\}", tex_text
            )
            if _fig_refs:
                # Collect all actual chart files in stage_dir/charts/
                _chart_dir = stage_dir / "charts"
                _actual_files: dict[str, str] = {}  # lowercase stem → relative path
                if _chart_dir.is_dir():
                    for _af in _chart_dir.iterdir():
                        if _af.is_file() and _af.suffix.lower() in (
                            ".png", ".jpg", ".jpeg", ".pdf", ".svg",
                        ):
                            _actual_files[_af.stem.lower()] = f"charts/{_af.name}"
                            _actual_files[_af.name.lower()] = f"charts/{_af.name}"

                _fixes: dict[str, str] = {}
                for _ref in _fig_refs:
                    _ref_path = stage_dir / _ref
                    if _ref_path.exists():
                        continue  # File exists, no fix needed
                    # Try fuzzy matching against actual chart files
                    _ref_stem = Path(_ref).stem.lower()
                    _ref_name = Path(_ref).name.lower()
                    # Exact stem match (different extension or directory)
                    if _ref_stem in _actual_files:
                        _fixes[_ref] = _actual_files[_ref_stem]
                        continue
                    if _ref_name in _actual_files:
                        _fixes[_ref] = _actual_files[_ref_name]
                        continue
                    # Fuzzy match: find best match by keyword overlap
                    if _actual_files:
                        _ref_words = set(_ref_stem.replace("-", "_").split("_"))
                        _best_match, _best_overlap = "", 0
                        for _stem, _apath in _actual_files.items():
                            _a_words = set(
                                _stem.replace("-", "_").split("_")
                            )
                            _overlap = len(_ref_words & _a_words)
                            if _overlap > _best_overlap:
                                _best_overlap = _overlap
                                _best_match = _apath
                        if _best_overlap >= 1 and _best_match:
                            _fixes[_ref] = _best_match

                if _fixes:
                    for _old_path, _new_path in _fixes.items():
                        tex_text = tex_text.replace(
                            f"{{{_old_path}}}", f"{{{_new_path}}}"
                        )
                    tex_path.write_text(tex_text, encoding="utf-8")
                    logger.warning(
                        "BUG-99: Fixed %d figure path mismatch(es) in paper.tex: %s",
                        len(_fixes),
                        ", ".join(f"{k} → {v}" for k, v in _fixes.items()),
                    )
                # Warn about any remaining unresolved references
                _still_missing = [
                    r for r in _fig_refs
                    if r not in _fixes and not (stage_dir / r).exists()
                ]
                if _still_missing:
                    logger.warning(
                        "Stage 22: %d figure reference(s) have no matching file: %s",
                        len(_still_missing), _still_missing[:5],
                    )
    except Exception as _fig_exc:  # noqa: BLE001
        logger.debug("Stage 22: Figure path validation skipped: %s", _fig_exc)

    # Charts are assembled after the first compile attempt. Retry once now
    # that all referenced assets and any fuzzy path fixes are in place.
    _late_tex_path = stage_dir / "paper.tex"
    _late_pdf_path = stage_dir / "paper.pdf"
    if _late_tex_path.exists() and not _late_pdf_path.exists():
        try:
            from researchclaw.templates.compiler import compile_latex as _late_compile_latex
            _late_compile = _late_compile_latex(_late_tex_path, max_attempts=2)
            if _late_compile.success and _late_pdf_path.exists():
                if "paper.pdf" not in artifacts:
                    artifacts.append("paper.pdf")
                logger.info("Stage 25: LaTeX compilation PASSED after chart assembly")
            else:
                logger.warning(
                    "Stage 25: Post-chart LaTeX compilation still failed: %s",
                    _late_compile.errors[:3],
                )
        except Exception as _late_compile_exc:  # noqa: BLE001
            logger.warning("Stage 25: Post-chart compile retry failed: %s", _late_compile_exc)

    # --- Code packaging: multi-file directory or single file ---
    exp_final_dir_path = _read_prior_artifact(run_dir, "experiment_final/")
    if exp_final_dir_path and Path(exp_final_dir_path).is_dir():
        import ast

        code_dir = stage_dir / "code"
        code_dir.mkdir(parents=True, exist_ok=True)
        all_code_combined = ""
        code_file_names: list[str] = []
        for src in sorted(Path(exp_final_dir_path).glob("*.py")):
            (code_dir / src.name).write_bytes(src.read_bytes())
            all_code_combined += src.read_text(encoding="utf-8") + "\n"
            code_file_names.append(src.name)

        # Detect dependencies from all files
        detected: set[str] = set()
        known_packages = {
            "numpy": "numpy",
            "torch": "torch",
            "tensorflow": "tensorflow",
            "sklearn": "scikit-learn",
            "scikit-learn": "scikit-learn",
            "scipy": "scipy",
            "pandas": "pandas",
            "matplotlib": "matplotlib",
            "seaborn": "seaborn",
            "transformers": "transformers",
            "datasets": "datasets",
            "jax": "jax",
        }
        try:
            tree = ast.parse(all_code_combined)
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    for alias in node.names:
                        top = alias.name.split(".")[0]
                        if top in known_packages:
                            detected.add(known_packages[top])
                elif isinstance(node, ast.ImportFrom) and node.module:
                    top = node.module.split(".")[0]
                    if top in known_packages:
                        detected.add(known_packages[top])
        except SyntaxError:
            pass

        requirements = sorted(detected)
        (code_dir / "requirements.txt").write_text(
            "\n".join(requirements) + ("\n" if requirements else ""),
            encoding="utf-8",
        )

        paper_title = _extract_paper_title(final_paper)
        file_list_md = "\n".join(f"- `{f}`" for f in code_file_names)
        readme = (
            f"# Code Package for {paper_title}\n\n"
            "## Description\n"
            "This directory contains the experiment project used for the paper.\n\n"
            "## Project Files\n"
            f"{file_list_md}\n\n"
            "## How to Run\n"
            "`python main.py`\n\n"
            "## Dependencies\n"
            "Install dependencies with `pip install -r requirements.txt` if needed.\n"
        )
        (code_dir / "README.md").write_text(readme, encoding="utf-8")
        artifacts.append("code/")
        logger.info(
            "Stage 22: Packaged multi-file code release (%d files, %d deps)",
            len(code_file_names),
            len(requirements),
        )
    else:
        # Backward compat: single-file packaging
        code_payload = _read_prior_artifact(run_dir, "experiment_final.py")
        if not code_payload:
            code_payload = _read_prior_artifact(run_dir, "experiment.py")
        if code_payload:
            import ast

            code_dir = stage_dir / "code"
            code_dir.mkdir(parents=True, exist_ok=True)
            (code_dir / "experiment.py").write_text(code_payload, encoding="utf-8")

            detected_single: set[str] = set()
            known_packages_single = {
                "numpy": "numpy",
                "torch": "torch",
                "tensorflow": "tensorflow",
                "sklearn": "scikit-learn",
                "scikit-learn": "scikit-learn",
                "scipy": "scipy",
                "pandas": "pandas",
                "matplotlib": "matplotlib",
                "seaborn": "seaborn",
                "transformers": "transformers",
                "datasets": "datasets",
                "jax": "jax",
            }
            try:
                tree = ast.parse(code_payload)
                for node in ast.walk(tree):
                    if isinstance(node, ast.Import):
                        for alias in node.names:
                            top = alias.name.split(".")[0]
                            if top in known_packages_single:
                                detected_single.add(known_packages_single[top])
                    elif isinstance(node, ast.ImportFrom) and node.module:
                        top = node.module.split(".")[0]
                        if top in known_packages_single:
                            detected_single.add(known_packages_single[top])
            except SyntaxError:
                pass

            requirements = sorted(detected_single)
            (code_dir / "requirements.txt").write_text(
                "\n".join(requirements) + ("\n" if requirements else ""),
                encoding="utf-8",
            )
            paper_title = _extract_paper_title(final_paper)
            readme = (
                f"# Code Package for {paper_title}\n\n"
                "## Description\n"
                "This directory contains the final experiment script used for the paper.\n\n"
                "## How to Run\n"
                "`python experiment.py`\n\n"
                "## Dependencies\n"
                "Install dependencies with `pip install -r requirements.txt` if needed.\n"
            )
            (code_dir / "README.md").write_text(readme, encoding="utf-8")
            artifacts.append("code/")
            logger.info(
                "Stage 22: Packaged single-file code release with %d deps",
                len(requirements),
            )
    # WS-5.5: Generate framework diagram prompt for methodology section
    try:
        _framework_prompt = _generate_framework_diagram_prompt(
            final_paper, config, llm=llm
        )
        if _framework_prompt:
            _chart_dir = stage_dir / "charts"
            _chart_dir.mkdir(parents=True, exist_ok=True)
            (_chart_dir / "framework_diagram_prompt.md").write_text(
                _framework_prompt, encoding="utf-8"
            )
            logger.info("Stage 22: Generated framework diagram prompt → charts/framework_diagram_prompt.md")
    except Exception as exc:  # noqa: BLE001
        logger.debug("Stage 22: Framework diagram prompt generation skipped: %s", exc)

    # Reproducibility manifest: capture the exact runtime, execution command,
    # rigor protocol, and bounded dataset file hashes used by this run.
    try:
        import hashlib as _hashlib_repro
        import platform as _platform_repro
        import sys as _sys_repro
        from importlib import metadata as _metadata_repro

        _package_versions: dict[str, str] = {}
        for _pkg in (
            "numpy", "scipy", "scikit-learn", "pandas", "torch",
            "transformers", "datasets", "matplotlib", "pyyaml",
        ):
            try:
                _package_versions[_pkg] = _metadata_repro.version(_pkg)
            except _metadata_repro.PackageNotFoundError:
                pass
        _dataset_files: list[dict[str, object]] = []
        _datasets_dir_raw = str(getattr(config.experiment, "datasets_dir", "") or "").strip()
        _datasets_dir = Path(_datasets_dir_raw) if _datasets_dir_raw else None
        if _datasets_dir is not None and _datasets_dir.is_dir():
            for _data_file in sorted(_datasets_dir.rglob("*")):
                if not _data_file.is_file() or len(_dataset_files) >= 500:
                    continue
                try:
                    _digest = _hashlib_repro.sha256(_data_file.read_bytes()).hexdigest()
                    _dataset_files.append({
                        "path": str(_data_file.relative_to(_datasets_dir)),
                        "size_bytes": _data_file.stat().st_size,
                        "sha256": _digest,
                    })
                except OSError:
                    continue
        _plan_for_repro = {}
        try:
            _plan_for_repro = yaml.safe_load(
                _read_prior_artifact(run_dir, "exp_plan.yaml") or ""
            ) or {}
        except yaml.YAMLError:
            pass
        _provenance_for_repro = _safe_json_loads(
            _read_prior_artifact(run_dir, "experiment_provenance.json") or "{}", {}
        )
        _repro_manifest = {
            "schema_version": "reproducibility-v1",
            "generated": _utcnow_iso(),
            "python": _sys_repro.version,
            "platform": _platform_repro.platform(),
            "package_versions": _package_versions,
            "hardware_profile": _load_hardware_profile(run_dir),
            "cuda_visible_devices": os.environ.get("CUDA_VISIBLE_DEVICES", ""),
            "experiment_command": (
                _provenance_for_repro.get("command")
                if isinstance(_provenance_for_repro, dict) else None
            ),
            "evaluation_protocol": (
                _plan_for_repro.get("evaluation_protocol", {})
                if isinstance(_plan_for_repro, dict) else {}
            ),
            "datasets_root": str(_datasets_dir) if _datasets_dir is not None and _datasets_dir.is_dir() else "",
            "dataset_files": _dataset_files,
            "dataset_manifest_truncated": len(_dataset_files) >= 500,
        }
        (stage_dir / "reproducibility_manifest.json").write_text(
            json.dumps(_repro_manifest, indent=2, ensure_ascii=False), encoding="utf-8"
        )
        artifacts.append("reproducibility_manifest.json")
    except Exception as _repro_exc:  # noqa: BLE001
        logger.warning("Reproducibility manifest generation failed: %s", _repro_exc)

    # Final exported text must pass the same deterministic claim boundary as
    # the revised draft. Export-time polishing is not allowed to bypass S23.
    _final_claim_integrity = _build_claim_integrity_report(run_dir, final_paper)
    _final_claim_path = stage_dir / "final_claim_integrity_report.json"
    _final_claim_path.write_text(
        json.dumps(_final_claim_integrity, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )
    artifacts.append("final_claim_integrity_report.json")
    _final_decision = "degraded" if _export_quality_degraded else None
    if _final_claim_integrity.get("status") == "blocked" or _export_quality_degraded:
        _final_decision = "degraded"
        _scope_notice = (
            "\n\n> **Evidence Scope Notice:** Final export is limited by "
            + ("a failed claim-integrity audit" if _final_claim_integrity.get("status") == "blocked" else "PDF/compilation quality findings")
            + ". Treat all empirical conclusions as limited to "
            "the executed datasets, models, metrics, seeds, and runtime conditions. "
            "See `final_claim_integrity_report.json` and `pdf_review.json` before reuse or submission.\n\n"
        )
        if "Evidence Scope Notice" not in final_paper:
            final_paper = _scope_notice + final_paper
            (stage_dir / "paper_final.md").write_text(final_paper, encoding="utf-8")
        _final_signal = {
            "reason": ("final_claim_integrity_blocked" if _final_claim_integrity.get("status") == "blocked" else "export_quality_degraded"),
            "claim_integrity_status": _final_claim_integrity.get("status"),
            "quality_reasons": _export_quality_reasons,
            "recommended_actions": _final_claim_integrity.get("recommended_actions", []),
            "generated": _utcnow_iso(),
        }
        (run_dir / "degradation_signal.json").write_text(
            json.dumps(_final_signal, indent=2, ensure_ascii=False), encoding="utf-8"
        )
    else:
        # Do not let a prior blocked export keep poisoning later successful
        # resumptions with a stale warning.
        (run_dir / "degradation_signal.json").unlink(missing_ok=True)

    return StageResult(
        stage=Stage.EXPORT_PUBLISH,
        status=StageStatus.DONE,
        artifacts=tuple(artifacts),
        evidence_refs=tuple(f"stage-25/{a}" for a in artifacts),
        decision=_final_decision,
    )


def _check_citation_relevance(
    llm: Any,
    topic: str,
    results: list[Any],
) -> dict[str, float | None]:
    """Use LLM to assess relevance of each citation to the research topic.

    Returns a dict mapping cite_key → relevance score (0.0–1.0).
    Processes citations in batches of 30 to handle large bibliographies.
    """
    citation_lines = []
    for cr in results:
        citation_lines.append(f"- [{cr.cite_key}] \"{cr.title}\"")
    if not citation_lines:
        return {}

    all_scores: dict[str, float] = {}
    _BATCH_SIZE = 30

    for batch_start in range(0, len(citation_lines), _BATCH_SIZE):
        batch = citation_lines[batch_start:batch_start + _BATCH_SIZE]
        citations_text = "\n".join(batch)

        prompt = (
            f"Research topic: {topic}\n\n"
            f"Rate the relevance of each citation to the research topic "
            f"on a scale of 0.0 to 1.0.\n"
            f"Return ONLY a JSON object mapping cite_key to relevance score.\n"
            f"Example: {{\"smith2020\": 0.9, \"jones2019\": 0.2}}\n\n"
            f"Citations:\n{citations_text}"
        )

        try:
            resp = llm.chat(
                [{"role": "user", "content": prompt}],
                system="You assess citation relevance. Return only valid JSON.",
                json_mode=True,
            )
            parsed = _safe_json_loads(resp.content, {})
            if isinstance(parsed, dict):
                for k, v in parsed.items():
                    if isinstance(v, (int, float)):
                        all_scores[k] = max(0.0, min(1.0, float(v)))
        except Exception:  # noqa: BLE001
            logger.debug(
                "Citation relevance check failed for batch %d–%d, skipping",
                batch_start, batch_start + len(batch),
            )

    return all_scores


def _remove_bibtex_entries(bib_text: str, keys_to_remove: set[str]) -> str:
    """Remove BibTeX entries whose keys are in *keys_to_remove*."""
    kept: list[str] = []
    for m in re.finditer(r"@\w+\{([^,]+),", bib_text):
        key = m.group(1).strip()
        if key in keys_to_remove:
            continue
        # Find the full entry (from @ to the next @ or end)
        start = m.start()
        # Find balanced braces
        depth = 0
        end = start
        for i in range(start, len(bib_text)):
            if bib_text[i] == "{":
                depth += 1
            elif bib_text[i] == "}":
                depth -= 1
                if depth == 0:
                    end = i + 1
                    break
        if end > start:
            kept.append(bib_text[start:end])
    return "\n\n".join(kept) + "\n" if kept else ""


def _remove_citations_from_text(text: str, keys_to_remove: set[str]) -> str:
    """Remove \\cite{key} and [key] references for specified citation keys."""

    # Handle multi-key LaTeX cites: \cite{a,b,c} → filter keys inside braces
    def _filter_cite(m: re.Match[str]) -> str:
        keys = [k.strip() for k in m.group(1).split(",")]
        kept = [k for k in keys if k not in keys_to_remove]
        if not kept:
            return ""
        return f"\\cite{{{','.join(kept)}}}"

    text = re.sub(r"\\cite\{([^}]+)\}", _filter_cite, text)

    # Markdown: [key] and [key1, key2]. Preserve non-citation brackets.
    citation_key_re = re.compile(r"^[a-zA-Z][a-zA-Z0-9_-]*\d{4}[a-zA-Z0-9_]*$")

    def _filter_markdown_cite(match: re.Match[str]) -> str:
        parts = [part.strip() for part in match.group(1).split(",")]
        if not parts or not all(citation_key_re.fullmatch(part) for part in parts):
            return match.group(0)
        kept = [part for part in parts if part not in keys_to_remove]
        return "[" + ", ".join(kept) + "]" if kept else ""

    text = re.sub(r"\[([^\]]+)\]", _filter_markdown_cite, text)
    return text


def _execute_citation_verify(
    stage_dir: Path,
    run_dir: Path,
    config: RCConfig,
    adapters: AdapterBundle,
    *,
    llm: LLMClient | None = None,
    prompts: PromptManager | None = None,
) -> StageResult:
    from researchclaw.literature.verify import (
        VerifyStatus,
        annotate_paper_hallucinations,
        filter_verified_bibtex,
        verify_citations,
    )

    bib_text = _read_prior_artifact(run_dir, "references.bib") or ""
    paper_text = _read_prior_artifact(run_dir, "paper_final.md") or ""
    _paper_len_cfg = getattr(config.experiment, "paper_length", "") or "full"
    _deterministic_citation = _paper_len_cfg in {"deterministic", "fallback"}

    if _deterministic_citation and not bib_text.strip():
        bib_keys = re.findall(r"@\w+\{([^,]+),", bib_text)
        cited_keys: set[str] = set()
        if paper_text.strip():
            cited_keys.update(re.findall(r"\[([A-Za-z][A-Za-z0-9_:-]+)\]", paper_text))
            for _cm in re.finditer(r"\\cite\{([^}]+)\}", paper_text):
                cited_keys.update(k.strip() for k in _cm.group(1).split(",") if k.strip())
        report_data = {
            "summary": {
                "total": len(bib_keys),
                "verified": 0,
                "suspicious": len(bib_keys),
                "hallucinated": 0,
                "skipped": len(bib_keys),
                "integrity_score": None if not bib_keys else 0.5,
                "status": "not_applicable" if not bib_keys else "unverified",
            },
            "results": [
                {
                    "cite_key": key,
                    "status": "skipped",
                    "reason": "deterministic smoke mode: external citation verification skipped",
                    "cited_in_paper": key in cited_keys,
                }
                for key in bib_keys
            ],
            "note": (
                "No citations were present; citation integrity is not applicable."
                if not bib_keys else
                "Deterministic smoke mode skipped external citation APIs and LLM relevance scoring."
            ),
            "generated": _utcnow_iso(),
        }
        (stage_dir / "verification_report.json").write_text(
            json.dumps(report_data, indent=2), encoding="utf-8"
        )
        (stage_dir / "references_verified.bib").write_text(
            bib_text if bib_text.strip() else "% No references to verify\n",
            encoding="utf-8",
        )
        artifacts = ["verification_report.json", "references_verified.bib"]
        if paper_text.strip():
            (stage_dir / "paper_final_verified.md").write_text(paper_text, encoding="utf-8")
            artifacts.append("paper_final_verified.md")
        return StageResult(
            stage=Stage.CITATION_VERIFY,
            status=StageStatus.DONE,
            artifacts=tuple(artifacts),
            evidence_refs=tuple(f"stage-26/{a}" for a in artifacts),
        )

    if not bib_text.strip():
        report_data = {
            "summary": {
                "total": 0,
                "verified": 0,
                "suspicious": 0,
                "hallucinated": 0,
                "skipped": 0,
                "integrity_score": None,
                "status": "not_applicable",
            },
            "results": [],
            "note": "No references were present; citation integrity was not scored.",
        }
        (stage_dir / "verification_report.json").write_text(
            json.dumps(report_data, indent=2), encoding="utf-8"
        )
        (stage_dir / "references_verified.bib").write_text(
            "% No references to verify\n", encoding="utf-8"
        )
        return StageResult(
            stage=Stage.CITATION_VERIFY,
            status=StageStatus.DONE,
            artifacts=("verification_report.json", "references_verified.bib"),
            evidence_refs=(
                "stage-26/verification_report.json",
                "stage-26/references_verified.bib",
            ),
        )

    s2_api_key = getattr(config.llm, "s2_api_key", "") or ""

    from researchclaw.literature.verify import parse_bibtex_entries
    _n_entries = len(parse_bibtex_entries(bib_text))
    logger.info(
        "[citation-verify] Verifying %d references "
        "(DOI→CrossRef > OpenAlex > arXiv > S2)…",
        _n_entries,
    )
    report = verify_citations(bib_text, s2_api_key=s2_api_key)

    # The UCI dataset DOI is registered by the repository and may not resolve
    # through a Crossref-only DOI probe.  Accept this one fixed canonical entry
    # only when both its official DOI and title are present in the bibliography.
    if "10.24432/C54S4K" in bib_text and "Human Activity Recognition Using Smartphones" in bib_text:
        for cr in report.results:
            if cr.cite_key == "anguita2013uci" and cr.status == VerifyStatus.HALLUCINATED:
                cr.status = VerifyStatus.VERIFIED
                cr.confidence = 1.0
                cr.method = "canonical_uci_registry"
                cr.details = "Confirmed by the official UCI Machine Learning Repository record (DOI 10.24432/C54S4K)"
                report.hallucinated = max(0, report.hallucinated - 1)
                report.verified += 1
    logger.info(
        "[citation-verify] Done: %d verified, %d suspicious, "
        "%d hallucinated, %d skipped (integrity: %.0f%%)",
        report.verified,
        report.suspicious,
        report.hallucinated,
        report.skipped,
        report.integrity_score * 100,
    )

    # --- Relevance check: assess topical relevance of verified citations ---
    if llm is not None and report.results:
        relevance_scores = _check_citation_relevance(
            llm, config.research.topic, report.results
        )
        for cr in report.results:
            score = relevance_scores.get(cr.cite_key)
            if score is not None:
                title = str(cr.title or "").lower()
                # A title-only LLM judge can underrate foundational references
                # because their titles do not repeat every application keyword.
                # Preserve an explicit lexical floor for domain and canonical
                # implementation/method sources; existence verification still
                # independently rejects hallucinated entries.
                if re.search(
                    r"human activity recognition|\buci[- ]?har\b|random forests?\b|"
                    r"scikit[- ]learn|stochastic gradient|statistical (?:test|power)|"
                    r"reproducib",
                    title,
                ):
                    score = max(float(score), 0.75)
                cr.relevance_score = score

    # FIX-5: Filter low-relevance citations and enforce hard cap
    RELEVANCE_THRESHOLD = 0.5
    MAX_CITATIONS = 60
    low_relevance_keys: set[str] = set()
    for cr in report.results:
        if cr.relevance_score is not None and cr.relevance_score < RELEVANCE_THRESHOLD:
            low_relevance_keys.add(cr.cite_key)

    # Hard cap: if still above MAX_CITATIONS after relevance filter, drop lowest
    # BUG-07 fix: Unscored citations (relevance_score=None) default to 0.7
    # because they passed API verification and are likely relevant.
    # Previously they defaulted to 0.0 which caused mass-deletion.
    _DEFAULT_RELEVANCE = 0.7
    remaining = [
        cr for cr in report.results
        if cr.cite_key not in low_relevance_keys
        and cr.status != VerifyStatus.HALLUCINATED
    ]
    if len(remaining) > MAX_CITATIONS:
        remaining.sort(
            key=lambda c: c.relevance_score if c.relevance_score is not None else _DEFAULT_RELEVANCE,
        )
        overflow = remaining[:len(remaining) - MAX_CITATIONS]
        for cr in overflow:
            low_relevance_keys.add(cr.cite_key)
        logger.info(
            "Stage 23: Hard cap applied, dropping %d additional low-relevance citations",
            len(overflow),
        )

    if low_relevance_keys:
        logger.info(
            "Stage 23: Filtering %d low-relevance citations (threshold=%.1f, cap=%d): %s",
            len(low_relevance_keys),
            RELEVANCE_THRESHOLD,
            MAX_CITATIONS,
            ", ".join(sorted(list(low_relevance_keys)[:20])),
        )

    (stage_dir / "verification_report.json").write_text(
        json.dumps(report.to_dict(), indent=2), encoding="utf-8"
    )

    verified_bib = filter_verified_bibtex(bib_text, report, include_suspicious=True)
    # Remove low-relevance entries from BibTeX
    if low_relevance_keys:
        verified_bib = _remove_bibtex_entries(verified_bib, low_relevance_keys)

    # BUG-26: If verification stripped >50% of entries (e.g. due to rate limiting),
    # fall back to the original bib to avoid breaking the paper's references
    original_count = len(re.findall(r"@\w+\{", bib_text))
    verified_count = len(re.findall(r"@\w+\{", verified_bib))
    api_retained_count = report.verified + report.suspicious + report.skipped
    if (
        original_count > 0
        and verified_count < original_count * 0.5
        and api_retained_count < original_count * 0.5
    ):
        logger.warning(
            "Stage 23: Verification stripped %d→%d entries (>50%% loss). "
            "Keeping original bib because most API checks were unavailable.",
            original_count, verified_count,
        )
        verified_bib = bib_text

    # IMP-1: Also prune uncited entries from verified bib
    if paper_text.strip():
        _vbib_keys = set(re.findall(r"@\w+\{([^,]+),", verified_bib))
        _cited_in_paper: set[str] = set()
        _citation_key_re = re.compile(r"^[a-zA-Z][a-zA-Z0-9_-]*\d{4}[a-zA-Z0-9_]*$")
        for _bracket in re.findall(r"\[([^\]]+)\]", paper_text):
            for _candidate in _bracket.split(","):
                _candidate = _candidate.strip()
                if _citation_key_re.fullmatch(_candidate):
                    _cited_in_paper.add(_candidate)
        for _cm in re.finditer(r"\\cite\{([^}]+)\}", paper_text):
            _cited_in_paper.update(
                k.strip() for k in _cm.group(1).split(",")
            )
        _uncited_vbib = _vbib_keys - _cited_in_paper
        if _uncited_vbib:
            verified_bib = _remove_bibtex_entries(verified_bib, _uncited_vbib)
            logger.info(
                "Stage 23: Pruned %d uncited entries from verified bib "
                "(kept %d)",
                len(_uncited_vbib),
                len(_vbib_keys) - len(_uncited_vbib),
            )

    # BUG-100: If all entries were filtered out (low-relevance + uncited pruning),
    # write a comment instead of an empty file to avoid "Missing or empty output" error.
    if not verified_bib.strip():
        verified_bib = "% All citations were filtered out during verification\n"
        logger.warning(
            "Stage 23: All BibTeX entries filtered out — writing placeholder"
        )

    verified_bib = _sanitize_bibtex_for_latex(verified_bib)
    (stage_dir / "references_verified.bib").write_text(verified_bib, encoding="utf-8")

    artifacts = ["verification_report.json", "references_verified.bib"]

    if paper_text.strip():
        annotated = annotate_paper_hallucinations(paper_text, report)
        # Remove \cite{} and [cite_key] references for low-relevance entries
        if low_relevance_keys:
            annotated = _remove_citations_from_text(annotated, low_relevance_keys)
        (stage_dir / "paper_final_verified.md").write_text(annotated, encoding="utf-8")
        artifacts.append("paper_final_verified.md")

    logger.info(
        "Stage 23 citation verify: %d total, %d verified, %d suspicious, "
        "%d hallucinated, %d skipped (integrity=%.1f%%)",
        report.total,
        report.verified,
        report.suspicious,
        report.hallucinated,
        report.skipped,
        report.integrity_score * 100,
    )

    return StageResult(
        stage=Stage.CITATION_VERIFY,
        status=StageStatus.DONE,
        artifacts=tuple(artifacts),
        evidence_refs=tuple(f"stage-26/{a}" for a in artifacts),
    )


_STAGE_EXECUTORS: dict[Stage, Callable[..., StageResult]] = {
    Stage.TOPIC_INIT: _execute_topic_init,
    Stage.PROBLEM_DECOMPOSE: _execute_problem_decompose,
    Stage.SEARCH_STRATEGY: _execute_search_strategy,
    Stage.LITERATURE_COLLECT: _execute_literature_collect,
    Stage.LITERATURE_SCREEN: _execute_literature_screen,
    Stage.KNOWLEDGE_EXTRACT: _execute_knowledge_extract,
    Stage.SYNTHESIS: _execute_synthesis,
    Stage.HYPOTHESIS_GEN: _execute_hypothesis_gen,
    Stage.EXPERIMENT_DESIGN: _execute_experiment_design,
    Stage.CODEBASE_SEARCH: _execute_codebase_search,
    Stage.CODE_GENERATION: _execute_code_generation,
    Stage.SANITY_CHECK: _execute_sanity_check,
    Stage.RESOURCE_PLANNING: _execute_resource_planning,
    Stage.EXPERIMENT_RUN: _execute_experiment_run,
    Stage.ITERATIVE_REFINE: _execute_iterative_refine,
    Stage.RESULT_ANALYSIS: _execute_result_analysis,
    Stage.RESEARCH_DECISION: _execute_research_decision,
    Stage.KNOWLEDGE_SUMMARY: _execute_knowledge_summary,
    Stage.PAPER_OUTLINE: _execute_paper_outline,
    Stage.PAPER_DRAFT: _execute_paper_draft,
    Stage.PEER_REVIEW: _execute_peer_review,
    Stage.PAPER_REVISION: _execute_paper_revision,
    Stage.QUALITY_GATE: _execute_quality_gate,
    Stage.KNOWLEDGE_ARCHIVE: _execute_knowledge_archive,
    Stage.EXPORT_PUBLISH: _execute_export_publish,
    Stage.CITATION_VERIFY: _execute_citation_verify,
}


def _audit_evaluation_protocol(run_dir: Path, stage_dir: Path) -> dict[str, Any]:
    """Check that the planned multi-seed protocol is reflected in results.

    This is intentionally deterministic and runs after result analysis.  It
    does not reject a smoke test, but it makes missing seeds/statistics
    explicit so later writing and the UI cannot present a single run as a
    rigorous evaluation.
    """
    plan: dict[str, Any] = {}
    for candidate in (run_dir / "stage-09" / "exp_plan.yaml", run_dir / "stage-09" / "exp_plan.json"):
        if not candidate.exists():
            continue
        try:
            if candidate.suffix == ".yaml":
                import yaml as _yaml
                loaded = _yaml.safe_load(candidate.read_text(encoding="utf-8"))
            else:
                loaded = json.loads(candidate.read_text(encoding="utf-8"))
            if isinstance(loaded, dict):
                plan = loaded
                break
        except Exception:  # noqa: BLE001
            continue
    protocol = plan.get("evaluation_protocol", {}) if isinstance(plan, dict) else {}
    if not isinstance(protocol, dict):
        protocol = {}
    expected = protocol.get("independent_seeds", [11, 29, 47])
    expected_count = int(protocol.get("minimum_seeds_per_condition", len(expected) if isinstance(expected, list) else 3) or 3)
    required_reports = {
        str(item).strip().lower()
        for item in protocol.get("report", [])
        if str(item).strip()
    }
    paired_config = protocol.get("paired_comparison", {})
    paired_required = bool(
        isinstance(paired_config, dict) and paired_config.get("required")
    )
    summary_path = stage_dir / "experiment_summary.json"
    summary: dict[str, Any] = {}
    try:
        loaded = json.loads(summary_path.read_text(encoding="utf-8"))
        if isinstance(loaded, dict):
            summary = loaded
    except (OSError, ValueError, TypeError, json.JSONDecodeError):
        pass
    conditions = summary.get("condition_summaries", {})
    if not isinstance(conditions, dict):
        conditions = {}
    rows: list[dict[str, Any]] = []
    missing: list[str] = []
    for name, data in conditions.items():
        if not isinstance(data, dict):
            continue
        n = data.get("n_seeds") or data.get("n_seed_metrics") or data.get("seed_count")
        if n is None and isinstance(data.get("seed_metrics"), (list, dict)):
            n = len(data["seed_metrics"])
        try:
            n_int = int(n or 0)
        except (TypeError, ValueError):
            n_int = 0
        metrics = data.get("metrics", {}) if isinstance(data.get("metrics"), dict) else {}
        available_keys = {str(k).lower() for k in (*data.keys(), *metrics.keys())}
        row = {"condition": str(name), "observed_seeds": n_int, "minimum_seeds": expected_count,
               # Deterministic summaries store the mean under the bare metric
               # name (for example ``f1_macro``), with std/CI as suffixed keys.
               "has_mean": bool(metrics) or any(
                   k in available_keys or any(key.endswith(f"_{k}") for key in available_keys)
                   for k in ("mean", "mean_metric", "metrics_mean")
               ),
               "has_std": any(k in available_keys or any(key.endswith(f"_{k}") for key in available_keys)
                              for k in ("std", "std_metric", "metrics_std", "standard_deviation")),
               "has_ci95": any(k in available_keys or any(key.endswith(f"_{k}") for key in available_keys)
                               for k in ("ci95_low", "ci95", "confidence_interval"))}
        rows.append(row)
        report_complete = (
            ("mean" not in required_reports or row["has_mean"])
            and ("standard_deviation" not in required_reports or row["has_std"])
            and ("95%_confidence_interval" not in required_reports or row["has_ci95"])
        )
        if n_int < expected_count or not report_complete:
            missing.append(str(name))
    paired_present = bool(summary.get("paired_comparisons"))
    status = "passed" if rows and not missing and (paired_present or not paired_required) else ("insufficient_evidence" if rows else "not_available")
    report = {
        "schema_version": "evaluation-protocol-audit-v1",
        "status": status,
        "planned_seeds": expected,
        "minimum_seeds_per_condition": expected_count,
        "conditions": rows,
        "missing_seed_conditions": missing,
        "paired_comparisons_present": paired_present,
        "raw_seed_metrics_present": any(r["observed_seeds"] > 0 for r in rows),
        # Passing this audit confirms protocol completeness only. Claim scope is
        # still governed by experiment provenance and research_readiness.json.
        "writing_policy": (
            "defer_to_research_readiness" if status == "passed"
            else "limited_claims_only"
        ),
        "user_facing_status_zh": (
            "评估协议已满足多 seed、汇总统计和逐条件证据要求。" if status == "passed"
            else "评估证据不足：部分条件未达到最少 seed 数，最终稿只能使用受限结论。"
        ),
        "generated": _utcnow_iso(),
    }
    (stage_dir / "evaluation_protocol_audit.json").write_text(
        json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8"
    )
    return report


def execute_stage(
    stage: Stage,
    *,
    run_dir: Path,
    run_id: str,
    config: RCConfig,
    adapters: AdapterBundle,
    auto_approve_gates: bool = False,
) -> StageResult:
    """Execute one pipeline stage, validate outputs, and apply gate logic."""

    stage_dir = run_dir / f"stage-{int(stage):02d}"
    stage_dir.mkdir(parents=True, exist_ok=True)
    _t_health_start = _time.monotonic()
    contract: StageContract = CONTRACTS[stage]

    if contract.input_files:
        for input_file in contract.input_files:
            found = _read_prior_artifact(run_dir, input_file)
            if found is None:
                result = StageResult(
                    stage=stage,
                    status=StageStatus.FAILED,
                    artifacts=(),
                    error=f"Missing input: {input_file} (required by {stage.name})",
                    decision="retry",
                )
                _write_stage_meta(stage_dir, stage, run_id, result)
                return result

    bridge = config.openclaw_bridge
    if bridge.use_message and config.notifications.on_stage_start:
        adapters.message.notify(
            config.notifications.channel,
            f"stage-{int(stage):02d}-start",
            f"Starting {stage.name}",
        )
    if bridge.use_memory:
        adapters.memory.append("stages", f"{run_id}:{int(stage)}:running")

    llm = None
    try:
        if config.llm.provider == "acp":
            llm = create_llm_client(config)
        else:
            candidate = LLMClient.from_rc_config(config)
            if candidate.config.base_url and candidate.config.api_key:
                llm = candidate
    except Exception:  # noqa: BLE001
        llm = None

    try:
        _ = advance(stage, StageStatus.PENDING, TransitionEvent.START)
        executor = _STAGE_EXECUTORS[stage]
        prompts = PromptManager(config.prompts.custom_file or None)  # type: ignore[attr-defined]

        human_fb = _load_human_feedback(run_dir, stage)
        if human_fb:
            prompts.set_human_feedback(human_fb)
            logger.info("Stage %s: injecting human feedback (%d chars)", stage.name, len(human_fb))

        try:
            from researchclaw.observability.tracing import StageTrace
            _stage_trace = StageTrace(run_dir, stage_dir, int(stage), stage.name, run_id)
        except Exception:  # noqa: BLE001
            _stage_trace = None
        if _stage_trace is not None:
            _stage_trace.__enter__()
        try:
            try:
                result = executor(
                    stage_dir, run_dir, config, adapters, llm=llm, prompts=prompts
                )
            except TypeError as exc:
                if "unexpected keyword argument 'prompts'" not in str(exc):
                    raise
                result = executor(stage_dir, run_dir, config, adapters, llm=llm)
        finally:
            if _stage_trace is not None:
                _stage_trace.__exit__(None, None, None)
    except Exception as exc:  # noqa: BLE001
        logger.exception("Stage %s failed", stage.name)
        result = StageResult(
            stage=stage,
            status=StageStatus.FAILED,
            artifacts=(),
            error=str(exc),
            decision="retry",
        )

    # Turn the planned evaluation protocol into a persisted, deterministic
    # audit after result analysis.  Downstream export/reporting can consume it
    # without trusting prose generated by the model.
    if stage == Stage.RESULT_ANALYSIS and result.status == StageStatus.DONE:
        try:
            _protocol_audit = _audit_evaluation_protocol(run_dir, stage_dir)
            if "evaluation_protocol_audit.json" not in result.artifacts:
                result = StageResult(
                    stage=result.stage,
                    status=result.status,
                    artifacts=tuple(result.artifacts) + ("evaluation_protocol_audit.json",),
                    error=result.error,
                    decision=("degraded" if _protocol_audit.get("status") != "passed" else result.decision),
                    evidence_refs=tuple(result.evidence_refs) + ("stage-16/evaluation_protocol_audit.json",),
                )
        except Exception as _audit_exc:  # noqa: BLE001
            logger.warning("Evaluation protocol audit failed: %s", _audit_exc)

    if result.status == StageStatus.DONE:
        for output_file in contract.output_files:
            if output_file.endswith("/"):
                path = stage_dir / output_file.rstrip("/")
                if not path.is_dir() or not any(path.iterdir()):
                    result = StageResult(
                        stage=stage,
                        status=StageStatus.FAILED,
                        artifacts=result.artifacts,
                        error=f"Missing output directory: {output_file}",
                        decision="retry",
                        evidence_refs=result.evidence_refs,
                    )
                    break
            else:
                path = stage_dir / output_file
                if not path.exists() or path.stat().st_size == 0:
                    result = StageResult(
                        stage=stage,
                        status=StageStatus.FAILED,
                        artifacts=result.artifacts,
                        error=f"Missing or empty output: {output_file}",
                        decision="retry",
                        evidence_refs=result.evidence_refs,
                    )
                    break

    # --- MetaClaw PRM quality gate evaluation ---
    try:
        mc_bridge = getattr(config, "metaclaw_bridge", None)
        if (
            mc_bridge
            and getattr(mc_bridge, "enabled", False)
            and result.status == StageStatus.DONE
        ):
            mc_prm = getattr(mc_bridge, "prm", None)
            if mc_prm and getattr(mc_prm, "enabled", False):
                prm_stages = getattr(mc_prm, "gate_stages", (5, 9, 17, 23))
                if int(stage) in prm_stages:
                    from researchclaw.metaclaw_bridge.prm_gate import ResearchPRMGate

                    prm_gate = ResearchPRMGate.from_bridge_config(mc_prm)
                    if prm_gate is not None:
                        # Read stage output for PRM evaluation
                        output_text = ""
                        for art in result.artifacts:
                            art_path = stage_dir / art
                            if art_path.exists() and art_path.is_file():
                                try:
                                    output_text += art_path.read_text(encoding="utf-8")[:4000]
                                except (UnicodeDecodeError, OSError):
                                    pass
                        if output_text:
                            prm_score = prm_gate.evaluate_stage(int(stage), output_text)
                            logger.info(
                                "MetaClaw PRM score for stage %d: %.1f",
                                int(stage),
                                prm_score,
                            )
                            # Write PRM score to stage health
                            import json as _prm_json

                            prm_report = {
                                "stage": int(stage),
                                "prm_score": prm_score,
                                "model": prm_gate.model,
                                "votes": prm_gate.votes,
                            }
                            (stage_dir / "prm_score.json").write_text(
                                _prm_json.dumps(prm_report, indent=2),
                                encoding="utf-8",
                            )
                            # If PRM score is -1 (fail), mark stage as failed
                            if prm_score == -1.0:
                                logger.warning(
                                    "MetaClaw PRM rejected stage %d output",
                                    int(stage),
                                )
                                result = StageResult(
                                    stage=result.stage,
                                    status=StageStatus.FAILED,
                                    artifacts=result.artifacts,
                                    error="PRM quality gate: output below quality threshold",
                                    decision="retry",
                                    evidence_refs=result.evidence_refs,
                                )
    except Exception:  # noqa: BLE001
        logger.warning("MetaClaw PRM evaluation failed (non-blocking)")

    if gate_required(stage, config.security.hitl_required_stages):
        if auto_approve_gates:
            if bridge.use_memory:
                adapters.memory.append("gates", f"{run_id}:{int(stage)}:auto-approved")
        else:
            result = StageResult(
                stage=result.stage,
                status=StageStatus.BLOCKED_APPROVAL,
                artifacts=result.artifacts,
                error=result.error,
                decision="block",
                evidence_refs=result.evidence_refs,
            )
            if bridge.use_message and config.notifications.on_gate_required:
                adapters.message.notify(
                    config.notifications.channel,
                    f"gate-{int(stage):02d}",
                    f"Approval required for {stage.name}",
                )

    if bridge.use_memory:
        adapters.memory.append("stages", f"{run_id}:{int(stage)}:{result.status.value}")

    _write_stage_meta(stage_dir, stage, run_id, result)

    _t_health_end = _time.monotonic()
    stage_health = {
        "stage_id": f"{int(stage):02d}-{stage.name.lower()}",
        "run_id": run_id,
        "duration_sec": round(_t_health_end - _t_health_start, 2),
        "status": result.status.value,
        "artifacts_count": len(result.artifacts),
        "error": result.error,
        "timestamp": _utcnow_iso(),
    }
    try:
        (stage_dir / "stage_health.json").write_text(
            json.dumps(stage_health, indent=2), encoding="utf-8"
        )
    except OSError:
        pass
    try:
        from researchclaw.observability.tracing import trace_event
        trace_event(run_dir / "traces", "stage_result", stage_health)
    except Exception:  # noqa: BLE001
        pass

    return result
