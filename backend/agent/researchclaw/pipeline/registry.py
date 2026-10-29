"""Stage implementations are registered separately from their lifecycle."""

from __future__ import annotations

import inspect
from collections.abc import Callable, Mapping
from typing import Any

from researchclaw.pipeline.contracts import CONTRACTS
from researchclaw.pipeline.models import StageResult
from researchclaw.pipeline.stages import PHASE_MAP, Stage


class StageRegistry:
    def __init__(self, handlers: Mapping[Stage, Callable[..., StageResult]] | None = None) -> None:
        # Keep the legacy mapping live while implementation modules migrate.
        self.handlers = handlers if isinstance(handlers, dict) else dict(handlers or {})

    def register(self, stage: Stage, handler: Callable[..., StageResult], *, replace: bool = False) -> None:
        if stage not in CONTRACTS or not callable(handler):
            raise ValueError(f"Invalid implementation for {stage}")
        if stage in self.handlers and not replace:
            raise ValueError(f"Stage {stage.name} already registered")
        self.handlers[stage] = handler

    def resolve(self, stage: Stage) -> Callable[..., StageResult]:
        try:
            return self.handlers[stage]
        except KeyError as exc:
            raise LookupError(f"No implementation registered for {stage.name}") from exc

    def invoke(self, stage: Stage, *args: Any, **kwargs: Any) -> StageResult:
        handler = self.resolve(stage)
        try:
            parameters = inspect.signature(handler).parameters
            accepts_prompts = "prompts" in parameters or any(
                parameter.kind == inspect.Parameter.VAR_KEYWORD for parameter in parameters.values())
        except (TypeError, ValueError):
            accepts_prompts = True
        if not accepts_prompts:
            kwargs.pop("prompts", None)
        # A TypeError raised inside a handler must not execute it a second time.
        result = handler(*args, **kwargs)
        if not isinstance(result, StageResult) or result.stage != stage:
            raise TypeError(f"Invalid stage result returned by {stage.name}")
        return result

    def describe(self) -> list[dict[str, Any]]:
        phase_for = {stage: phase for phase, stages in PHASE_MAP.items() for stage in stages}
        return [{"stage": int(stage), "name": stage.name, "phase": phase_for[stage],
                 "implementation": f"{handler.__module__}.{getattr(handler, '__name__', type(handler).__name__)}",
                 "inputs": list(CONTRACTS[stage].input_files), "outputs": list(CONTRACTS[stage].output_files)}
                for stage, handler in sorted(self.handlers.items())]
