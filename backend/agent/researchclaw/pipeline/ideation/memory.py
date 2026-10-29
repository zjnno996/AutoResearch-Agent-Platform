"""Ideation-specific experience records built on shared memory storage."""
from __future__ import annotations

from datetime import datetime, timezone
import logging
from pathlib import Path
from typing import Any

from researchclaw.config import RCConfig
from researchclaw.pipeline.ideation.text import _idea_like_blocks, _idea_title_slug
from researchclaw.pipeline.support.research_memory import (
    _resolve_shared_results_path,
    append_experience,
)
logger = logging.getLogger(__name__)


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
        append_experience(memory_path, update_text)
    except OSError:
        logger.warning("S8: Failed to update shared ideation memory", exc_info=True)
    return ("ideation_memory_update.md",)
