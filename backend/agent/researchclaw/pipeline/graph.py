"""Artifact and control dependencies for inspectable research execution plans."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any, Mapping

from researchclaw.pipeline.contracts import CONTRACTS, StageContract
from researchclaw.pipeline.stages import Stage


@dataclass(frozen=True)
class Dependency:
    producer: Stage
    consumer: Stage
    kind: str
    artifact: str = ""
    reason: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {**asdict(self), "producer": int(self.producer), "consumer": int(self.consumer)}


CONTROL_DEPENDENCIES = (
    Dependency(Stage.SANITY_CHECK, Stage.RESOURCE_PLANNING, "control", reason="Plan resources after code sanity checks"),
    Dependency(Stage.SANITY_CHECK, Stage.EXPERIMENT_RUN, "control", reason="Execute validated code"),
    Dependency(Stage.ITERATIVE_REFINE, Stage.RESULT_ANALYSIS, "control", reason="Analyze the completed refinement attempt"),
    Dependency(Stage.RESEARCH_DECISION, Stage.PAPER_OUTLINE, "control", reason="Observe the scientific claim boundary"),
    Dependency(Stage.QUALITY_GATE, Stage.KNOWLEDGE_ARCHIVE, "control", reason="Archive the reviewed research state"),
    Dependency(Stage.QUALITY_GATE, Stage.EXPORT_PUBLISH, "control", reason="Export after quality evaluation"),
)


class StageGraph:
    def __init__(self, contracts: Mapping[Stage, StageContract] = CONTRACTS,
                 controls: tuple[Dependency, ...] = CONTROL_DEPENDENCIES) -> None:
        self.contracts = dict(contracts)
        edges: list[Dependency] = []
        producers: dict[str, list[Stage]] = {}
        for stage, contract in self.contracts.items():
            for output in contract.output_files:
                producers.setdefault(output, []).append(stage)
        for stage, contract in self.contracts.items():
            for required in contract.input_files:
                candidates = [source for source in producers.get(required, []) if source < stage]
                if not candidates:
                    raise ValueError(f"No producer for {stage.name} input {required}")
                edges.append(Dependency(max(candidates), stage, "artifact", required))
        for edge in controls:
            if edge.producer in self.contracts and edge.consumer in self.contracts:
                edges.append(edge)
        self.edges = tuple(dict.fromkeys(edges))
        self.order = self._topological(set(self.contracts))

    def _topological(self, selected: set[Stage]) -> tuple[Stage, ...]:
        remaining = set(selected)
        result = []
        while remaining:
            ready = sorted(node for node in remaining if not any(
                edge.consumer == node and edge.producer in remaining for edge in self.edges))
            if not ready:
                raise ValueError("Research stage dependency graph contains a cycle")
            node = ready[0]
            result.append(node)
            remaining.remove(node)
        return tuple(result)

    def execution_order(self, start: Stage, end: Stage | None = None) -> tuple[Stage, ...]:
        if end is not None and end < start:
            raise ValueError("to_stage must be at or after from_stage")
        selected = {stage for stage in self.contracts if stage >= start and (end is None or stage <= end)}
        return self._topological(selected)

    def ready(self, completed: set[Stage], *, blocked: set[Stage] | None = None) -> tuple[Stage, ...]:
        excluded = completed | (blocked or set())
        return tuple(stage for stage in self.order if stage not in excluded and all(
            edge.producer in completed for edge in self.edges if edge.consumer == stage))

    def affected_by(self, changed: set[Stage]) -> tuple[Stage, ...]:
        affected = set(changed)
        while True:
            expanded = affected | {edge.consumer for edge in self.edges if edge.producer in affected}
            if expanded == affected:
                return tuple(stage for stage in self.order if stage in affected)
            affected = expanded

    def describe(self, start: Stage = Stage.TOPIC_INIT, end: Stage | None = None) -> dict[str, Any]:
        order = self.execution_order(start, end)
        selected = set(order)
        return {"schema_version": "research-execution-plan-v1", "scheduler": "dependency_ordered_sequential",
                "from_stage": int(start), "to_stage": int(end) if end else 26,
                "order": [int(stage) for stage in order],
                "nodes": [{"stage": int(stage), "name": stage.name,
                           "inputs": list(self.contracts[stage].input_files),
                           "outputs": list(self.contracts[stage].output_files)} for stage in order],
                "dependencies": [edge.to_dict() for edge in self.edges if edge.consumer in selected],
                "external_dependencies": [edge.to_dict() for edge in self.edges
                                          if edge.consumer in selected and edge.producer not in selected],
                "policy": "Artifact dependencies are explicit; legacy implicit reads require conservative suffix repair."}
