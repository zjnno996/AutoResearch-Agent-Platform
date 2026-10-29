"""Scientific readiness and evidence boundaries for manuscript claims."""

from __future__ import annotations

import json
import logging
import math
from pathlib import Path
import re
from typing import Any

import yaml

from researchclaw.pipeline.support.artifacts import _read_prior_artifact, _utcnow_iso
from researchclaw.pipeline.support.experiment_evidence import _build_experiment_fact_contract
from researchclaw.pipeline.support.parsing import _safe_json_loads

logger = logging.getLogger(__name__)


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
    smoke_improvement_claim = False
    for improvement in re.finditer(r"(?i)\b(?:improv\w*|outperform\w*|superior\w*)\b|提升|优于|提高", empirical_text):
        prefix = empirical_text[max(0, improvement.start() - 100):improvement.start()]
        if not re.search(r"(?i)(?:does\s+not|do\s+not|did\s+not|cannot|no\s+claim|never|unsupported|not\s+supported)[^.\n]{0,90}$", prefix):
            smoke_improvement_claim = True
            break
    prohibited_empirical_claims = bool(
        has_empirical_performance_claims
        and (
            writing_policy == "no_empirical_claims"
            or (writing_policy == "engineering_report_only" and (not real_executed_metrics or smoke_improvement_claim))
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
