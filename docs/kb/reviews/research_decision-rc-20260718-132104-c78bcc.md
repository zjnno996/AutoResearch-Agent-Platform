---
created: '2026-07-18T13:21:28+00:00'
evidence:
- stage-17/decision.md
- stage-17/decision_structured.json
- stage-17/research_readiness.json
id: research_decision-rc-20260718-132104-c78bcc
run_id: rc-20260718-132104-c78bcc
stage: 17-research_decision
tags:
- research_decision
- stage-17
- run-rc-20260
title: 'Stage 17: Research Decision'
---

# Stage 17: Research Decision

## Decision
**REFINE**

## Justification
The decision is **REFINE** because the analysis explicitly states that the experiment status is "engineering smoke only" and requires "repeated runs and scientific validation" before any scientific claims can be made. While the numerical results exist, the **Analysis Quality Rating** is effectively **4/10** (as indicated by the `readiness_score` of 49.0 and the explicit `claim_status: requires_repeated_runs_and_scientific_validation`).

According to the **MINIMUM QUALITY CRITERIA for PROCEED**, Criterion 5 requires the analysis quality rating to be **≥4/10**. While the score is borderline, the qualitative assessment in the "Conclusions" and "Non-Negotiable Research Readiness" sections explicitly forbids presenting these results as scientific evidence. The statistical analysis section further highlights that with only three seeds, the parametric and non-parametric tests disagree (p=0.007 vs p=0.25), indicating that the current experimental tuning is insufficient to support a robust conclusion. Therefore, the experiments need re-tuning (specifically, increasing seed count or resolving the statistical disagreement) before proceeding to paper writing.

## Evidence
1.  **Status Warning**: "实验代码已执行；结论仍需重复实验与科学验证" (Code executed; conclusions require repeated experiments and scientific validation).
2.  **Readiness Score**: `readiness_score`: 49.0, which maps to an analysis quality well below a "scientific" threshold, despite the raw score calculation.
3.  **Claim Boundary**: `writing_policy`: "engineering_report_only" and `scientific_claims_allowed`: false.
4.  **Statistical Instability**: The `paired_t_test` (p=0.007) and `wilcoxon_signed_rank` (p=0.25) yield contradictory results, explicitly noted as "descriptive and do not support a broad significance claim" due to the low seed count (n=3).
5.  **Criterion Check**: While 2 baselines and 3 seeds are present, the **Analysis Quality** is deemed insufficient for scientific publication due to the instability of the results and the explicit "engineering smoke" label.

## Next Actions
1.  **Re-run Experiments**: Increase the number of independent random seeds from 3 to at least 5-10 to stabilize the confidence intervals and resolve the discrepancy between parametric and non-parametric tests.
2.  **Resolve Statistical Conflict**: Investigate why the t-test and Wilcoxon tests disagree; if the sample size is the cause, the new runs must be prioritized.
3.  **Update Readiness**: Once new runs are complete and statistical significance is consistent across tests, re-evaluate the `readiness_score`.
4.  **Refine Hypothesis**: Ensure the "Lightweight Real Link Acceptance" hypothesis is framed as a feasibility study or engineering benchmark rather than a novel scientific breakthrough until the data supports it.
5.  **Fix Data Pathing**: Address the "Lessons from Prior Runs" error regarding missing `README.txt` files to ensure future runs do not fail at the resource planning stage.

## Operational Boundary
The scientific recommendation above remains advisory. The user locked this run to the requested baselines, and the real experiment completed with saved metrics. The workflow therefore proceeds to an evidence-limited engineering report without adding seeds, models, datasets, ablations, or scientific performance claims.


{
  "decision": "refine",
  "raw_text_excerpt": "## Decision\n**REFINE**\n\n## Justification\nThe decision is **REFINE** because the analysis explicitly states that the experiment status is \"engineering smoke only\" and requires \"repeated runs and scientific validation\" before any scientific claims can be made. While the numerical results exist, the **Analysis Quality Rating** is effectively **4/10** (as indicated by the `readiness_score` of 49.0 and the explicit `claim_status: requires_repeated_runs_and_scientific_validation`).\n\nAccording to the *",
  "quality_warnings": [],
  "generated": "2026-07-18T13:21:28+00:00",
  "execution_control_decision": "proceed",
  "operational_override": true,
  "operational_override_reason": "scope_locked_reproduction_and_real_execution_complete",
  "claim_scope": "evidence_limited",
  "readiness_level": "engineering_smoke_only",
  "readiness_score": 49.0,
  "writing_policy": "engineering_report_only",
  "scientific_claims_allowed": false,
  "limited_claims_allowed": false
}

{
  "schema_version": "research-readiness-v1",
  "generated": "2026-07-18T13:21:28+00:00",
  "decision": "refine",
  "readiness_level": "engineering_smoke_only",
  "readiness_score": 49.0,
  "writing_policy": "engineering_report_only",
  "user_facing_status_zh": "代码已执行但仅属于工程 Smoke；可报告流程可运行，禁止形成科研性能结论。",
  "should_proceed_to_writing": true,
  "scientific_claims_allowed": false,
  "limited_claims_allowed": false,
  "evidence": {
    "executed": true,
    "real_code_execution": true,
    "experiment_scope": "candidate_domain_experiment",
    "claim_status": "requires_repeated_runs_and_scientific_validation",
    "plan_degraded": true,
    "plan_parse_strategy": "qwen_yaml",
    "has_metrics": true,
    "total_runs": 6,
    "baseline_count": 2,
    "dataset_count": 1,
    "condition_count": 2,
    "min_seeds_per_condition": 3,
    "paired_comparison_count": 2
  },
  "scores": {
    "plan_quality": 45,
    "execution": 100,
    "evidence_strength": 20,
    "experiment_rigor": 86,
    "reproducibility": 75,
    "raw_readiness_score_before_claim_cap": 61.0
  },
  "recommended_actions": [
    "修复实验计划解析或 BenchmarkAgent 校验问题"
  ],
  "execution_control_decision": "proceed",
  "operational_override": true,
  "operational_override_reason": "scope_locked_reproduction_and_real_execution_complete",
  "claim_scope": "evidence_limited"
}