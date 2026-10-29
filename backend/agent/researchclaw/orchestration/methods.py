"""Source-attributed research methods composed around a shared evidence contract.

Public method descriptions are distinguished from inspected source code. None
of these adaptations implies replication of a leaderboard submission or score.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
import re
from typing import Any

STUDY_TYPES = ("auto", "comparison", "reproduction", "observational", "numerical", "formal")

METHOD_SOURCES = (
    {"agent": "Qiushi Engine", "source": "https://arxiv.org/html/2609.08196v1",
     "evidence": "public_architecture_report", "mechanism": "plan_execute_verify_feedback",
     "implementation": ["pipeline.runtime", "orchestration.memory", "orchestration.repair"],
     "cost": "Bounded verification and repair add calls; do not repeat unchanged failures."},
    {"agent": "InnoClaw 2.0", "source": "https://github.com/SpectrAI-Initiative/InnoClaw",
     "revision": "67231f373deac3db29f685985a5632b6c0eb0f08", "evidence": "inspected_public_code",
     "mechanism": "requirements_artifacts_and_specialist_handoffs",
     "implementation": ["pipeline.graph", "orchestration.artifacts", "orchestration.council"],
     "cost": "Use bounded source pointers rather than copying all previous conversations.",
     "limit": "Public repository is not confirmed to be the exact leaderboard submission."},
    {"agent": "Ligase", "source": "https://www.gradence.com.cn/products/biopharma.html",
     "evidence": "public_product_description", "mechanism": "differentiated_scientific_roles",
     "implementation": ["orchestration.council"],
     "cost": "Independent roles add review calls at design and manuscript checkpoints.",
     "limit": "Prompt-based roles; proprietary trained specialist models are unavailable."},
    {"agent": "AutoSciRub", "source": "https://github.com/zjunlp/AutoSciRub",
     "revision": "2c6c58392ee6399e08f2526e35176f0e2c716e16", "evidence": "inspected_public_code",
     "mechanism": "grounded_rubric_criterion_verification_targeted_revision",
     "implementation": ["orchestration.rubric", "orchestration.workspace", "orchestration.repair", "orchestration.task_fidelity"],
     "cost": "Preserve criterion IDs and repair the owning stage instead of regenerating everything.",
     "limit": "The public October bundle does not establish leaderboard equivalence."},
    {"agent": "EvoLab", "source": "https://evo-lab.org/document/",
     "evidence": "public_documentation", "mechanism": "cross_session_experience_and_retained_attempts",
     "implementation": ["orchestration.memory", "orchestration.repair"],
     "cost": "Retrieve scoped lessons; retain attempts and compare against the same contract.",
     "limit": "Officially linked repository was unavailable; no private engine or weights copied."},
    {"agent": "InnoClaw", "source": "https://github.com/SpectrAI-Initiative/InnoClaw",
     "evidence": "same_public_family", "mechanism": "persistent_evidence_memory",
     "implementation": ["orchestration.memory", "orchestration.artifacts"],
     "cost": "Share the family adapter rather than duplicating an engine.",
     "limit": "Two ranked entries belong to the same family; not six independent public engines."},
)


@dataclass(frozen=True)
class StudyStrategy:
    study_type: str
    selection: str
    obligations: tuple[str, ...]
    roles: tuple[tuple[str, str], ...]
    validation_limit: str = "Specialized guidance supplements the existing evidence validators."

    def describe(self) -> dict[str, Any]:
        return {**asdict(self), "roles": dict(self.roles)}


_CORE_ROLES = (
    ("methodologist", "Check task fidelity, mechanism, controls, falsifiability, and alternative explanations."),
    ("statistician", "Check splits, leakage, independent seeds, metric direction, uncertainty, and matched comparisons."),
    ("reproducer", "Check actual code, dependencies, data, commands and retained outputs against the claim."),
)
_STRATEGIES = {
    "comparison": (
        ("Match data, budgets, splits, metrics and independent seeds across all planned conditions.",
         "Separate mechanism ablations from baseline comparisons; report uncertainty and negative results."),
        _CORE_ROLES),
    "reproduction": (
        ("Identify the source protocol and exact code/data versions; record every deviation before execution.",
         "Re-run the requested scope and distinguish reproduction fidelity from a new performance claim."),
        (("protocol_analyst", "Compare source protocol, implementation, configuration, datasets and stated deviations."),
         _CORE_ROLES[1], _CORE_ROLES[2])),
    "observational": (
        ("Define population, sampling, exposure, outcome, confounders and missing-data handling.",
         "Retain data lineage, sensitivity analyses and uncertainty; causal claims require identification assumptions."),
        (("causal_analyst", "Check identification, confounding, selection bias and causal versus associational claims."),
         _CORE_ROLES[1], _CORE_ROLES[2])),
    "numerical": (
        ("Specify equations, units, boundary conditions, solver versions, tolerances and convergence tests.",
         "Compare with an independent reference or analytic limit; separate numerical error from scientific effect."),
        (("numerical_analyst", "Check units, discretization, conditioning, tolerances, convergence and reference solutions."),
         _CORE_ROLES[0], _CORE_ROLES[2])),
    "formal": (
        ("State definitions, assumptions and theorem scope; retain machine-checkable proof artifacts where required.",
         "Examples and numerical checks cannot establish a theorem; unresolved proof obligations remain unmet."),
        (("proof_reviewer", "Check assumptions, logical gaps, counterexamples and actual proof-checker receipts."),
         _CORE_ROLES[0], _CORE_ROLES[2])),
}


def select_strategy(config: Any) -> StudyStrategy:
    requested = getattr(config.research_engine, "study_type", "auto")
    if requested not in STUDY_TYPES:
        raise ValueError(f"Unsupported research study type: {requested}")
    selected, selection = requested, "explicit_configuration"
    if requested == "auto":
        topic = config.research.topic.casefold()
        # Strong intent phrases only. Ambiguity keeps the conservative empirical
        # default; routing never waives the explicit task acceptance contract.
        cues = (
            ("reproduction", ("reproduce", "replicate the", "reproduction of", "复现")),
            ("formal", ("prove that", "formal proof", "proving", "linear convergence", "lyapunov", "convergence proof", "证明定理", "形式化证明", "收敛证明")),
            ("observational", ("observational study", "retrospective cohort", "观察性研究", "回顾性队列")),
            ("numerical", ("numerical simulation", "finite element", "numerical optimization", "admm", "数值模拟", "有限元")),
        )
        def contains(phrase: str) -> bool:
            return bool(re.search(r"(?<!\w)" + re.escape(phrase) + r"(?!\w)", topic)) if phrase.isascii() else phrase in topic
        matches = [kind for kind, words in cues if any(contains(word) for word in words)]
        # A theorem task can also request numerical illustrations. Keep its
        # proof obligations rather than falling back to an ML comparison.
        selected = "formal" if "formal" in matches else matches[0] if len(matches) == 1 else "comparison"
        selection = "topic_intent" if selected in matches else "conservative_default"
    obligations, roles = _STRATEGIES[selected]
    limit = ("A dedicated formal-proof validator is not yet implemented. Do not mark proof obligations passed from prose."
             if selected == "formal" else StudyStrategy.validation_limit)
    return StudyStrategy(selected, selection, obligations, roles, limit)


def method_plan(config: Any) -> dict[str, Any]:
    return {"schema_version": "research-method-plan-v1", "strategy": select_strategy(config).describe(),
            "source_adaptations": list(METHOD_SOURCES), "priority": "quality_before_efficiency",
            "composition": ["task_requirements", "literature_grounding", "design_review", "execution",
                            "criterion_verification", "bounded_owner_stage_repair", "scoped_experience"],
            "policy": "Keep all explicit requirements and core evidence checks. Routing cannot waive acceptance criteria.",
            "harness_references": [{"source": "https://github.com/InternScience/ResearchHarness",
                "revision": "72e8a44737aadabc8c3f9e5c7ea76883b9c7032f", "license": "MIT",
                "inspected_files": ["agent_base/context_compact.py", "agent_base/session_state.py",
                                    "benchmarks/ResearchClawBench/adapter.py"],
                "adaptation": "Preserve the original task, compact complete tool rounds, retain source pointers, and reject completion without deliverables.",
                "limit": "Additional official benchmark-compatible harness reference, not a top-six leaderboard entry; mechanisms reimplemented locally."}],
            "benchmark_equivalence": False}


def strategy_guidance(config: Any, stage: int) -> str:
    strategy = select_strategy(config)
    phase = ("Explore: ground the task and design falsifiable checks." if stage <= 9 else
             "Execute: retain executable changes, commands, outputs and diagnostic failures." if stage <= 17 else
             "Express: bind every claim to executed evidence and disclose unresolved requirements.")
    return (f"Research method: {strategy.study_type} ({strategy.selection}). {phase}\n"
            + "\n".join(f"- {item}" for item in strategy.obligations)
            + "\nKeep all explicit user requirements and the full acceptance contract. " + strategy.validation_limit)
