"""Stage prompt context, feedback and scoped evolution guidance."""

from __future__ import annotations

import json
import logging
from pathlib import Path
import re
from typing import Any

from researchclaw.config import RCConfig
from researchclaw.llm.client import LLMClient
from researchclaw.pipeline.support.artifacts import (
    _find_discussion_dir,
    _load_hardware_profile,
    _read_prior_artifact,
)
from researchclaw.pipeline.support.domain import _extract_english_from_mixed
from researchclaw.pipeline.support.experiment_evidence import _build_experiment_fact_contract
from researchclaw.pipeline.support.parsing import _safe_json_loads

logger = logging.getLogger(__name__)


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
