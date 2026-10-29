"""Persisted provider-call budget with an allocation for final deliverables."""
from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from pathlib import Path
from typing import Iterator

from researchclaw.orchestration.artifacts import read_json, write_json


class ModelCallBudgetExceeded(RuntimeError):
    pass


class ModelCallBudget:
    def __init__(self, path: Path, limit: int, final_reserve: int) -> None:
        self.path, self.limit, self.reserve = path, limit, final_reserve if limit else 0
        self.state = read_json(path) or {"attempts": 0, "by_stage": {}}
        self.state.update(limit=limit, final_reserve=self.reserve)

    def claim(self, stage: int | None) -> None:
        used = self.state["attempts"]
        ceiling = self.limit if stage is not None and stage >= 19 else self.limit - self.reserve
        if self.limit and used >= ceiling:
            raise ModelCallBudgetExceeded(f"Model-call budget reached ({used}/{self.limit}); "
                                         f"{self.reserve} calls are allocated to reporting and acceptance")
        self.state["attempts"] += 1
        key = str(stage or "unknown")
        self.state["by_stage"][key] = self.state["by_stage"].get(key, 0) + 1
        write_json(self.path, self.state)

    def available(self, stage: int | None) -> bool:
        if self.limit == 0:
            return True
        ceiling = self.limit if stage is not None and stage >= 19 else self.limit - self.reserve
        return self.state['attempts'] < ceiling


_BUDGET: ContextVar[ModelCallBudget | None] = ContextVar("model_call_budget", default=None)


def claim_model_call() -> None:
    budget = _BUDGET.get()
    if budget:
        from researchclaw.observability.tracing import current_stage_number
        budget.claim(current_stage_number())


def budget_status() -> str:
    budget = _BUDGET.get()
    if budget and not budget.limit:
        return f"Provider calls used: {budget.state['attempts']}; no fixed model-call quota. Existing repair and runtime policies still apply."
    return (f"Provider calls used: {budget.state['attempts']}/{budget.limit}; reserve {budget.reserve} for final reporting/acceptance."
            if budget else "")


def can_claim_model_call(stage: int) -> bool:
    budget = _BUDGET.get()
    return budget.available(stage) if budget else True


@contextmanager
def model_call_budget(path: Path, limit: int, final_reserve: int = 8) -> Iterator[ModelCallBudget]:
    budget = ModelCallBudget(path, limit, final_reserve)
    token = _BUDGET.set(budget)
    try:
        yield budget
    finally:
        _BUDGET.reset(token)
