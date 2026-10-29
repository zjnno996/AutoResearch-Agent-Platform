"""Support visuals components."""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from researchclaw.config import RCConfig


logger = logging.getLogger(__name__)


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
