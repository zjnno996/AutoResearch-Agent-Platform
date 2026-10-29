"""Steps archive components."""

from __future__ import annotations

import json
import logging
from pathlib import Path

from researchclaw.adapters import AdapterBundle
from researchclaw.config import RCConfig
from researchclaw.llm.client import LLMClient
from researchclaw.pipeline.models import StageResult
from researchclaw.pipeline.stages import Stage, StageStatus
from researchclaw.pipeline.support.artifacts import (
    _read_prior_artifact,
    _utcnow_iso,
    active_stage_glob,
)
from researchclaw.pipeline.support.context import (
    _build_context_preamble,
    _chat_with_prompt,
    _get_evolution_overlay,
)
from researchclaw.prompts import PromptManager

logger = logging.getLogger(__name__)


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
    for stage_subdir in sorted(active_stage_glob(run_dir, "stage-*")):
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
