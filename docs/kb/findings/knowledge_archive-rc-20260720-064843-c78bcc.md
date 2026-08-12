---
created: '2026-07-20T06:50:16+00:00'
evidence:
- stage-24/archive.md
- stage-24/bundle_index.json
id: knowledge_archive-rc-20260720-064843-c78bcc
run_id: rc-20260720-064843-c78bcc
stage: 24-knowledge_archive
tags:
- knowledge_archive
- stage-24
- run-rc-20260
title: 'Stage 24: Knowledge Archive'
---

# Stage 24: Knowledge Archive

# Retrospective Archive: UCI-HAR Baseline Audit (Lightweight Real-World Linkage Acceptance)

**Date**: 2024-10-XX  
**Status**: **REFINED** (Engineering Report Only)  
**Topic**: Reproducibility and Statistical Rigor in HAR Baselines under Strict Protocol Constraints  
**Decision**: The experiment is classified as an **engineering smoke test**. While numerical results were generated, the statistical power is insufficient for scientific publication claims. The final output is an evidence-limited IEEE-style report that explicitly documents the limitations of the $n=3$ seed protocol.

---

## 1. Executive Summary

This retrospective documents the execution of a strict reproducibility audit on the UCI-HAR dataset. The goal was to establish a statistically validated floor for Linear SGD and Random Forest baselines under the official subject-independent split.

**Key Outcome**: The experiment successfully executed the pipeline, generating metrics for 2 models across 3 seeds. However, the analysis revealed a **critical statistical instability**: the parametric t-test ($p=0.007$) and non-parametric Wilcoxon test ($p=0.25$) yielded contradictory results. This confirms that $n=3$ is insufficient for robust hypothesis testing in this context. Consequently, the decision was made to **REFINE** the output scope: no scientific claims of superiority are made. The final deliverable is a bounded engineering report demonstrating the *process* of rigorous auditing rather than a definitive model comparison.

---

## 2. Lessons Learned

### 2.1 Statistical Power and Sample Size
*   **Observation**: With only 3 independent random seeds, the statistical tests are highly sensitive to the specific variance of those runs. The discrepancy between the t-test (assuming normality) and Wilcoxon (distribution-free) indicates that the sample size is too small to satisfy the assumptions of either test reliably.
*   **Lesson**: For HAR baseline comparisons where effect sizes are often marginal (<1%), **$n=3$ is scientifically inadequate**. Future work must target $n \ge 10$ to stabilize confidence intervals and ensure test consistency.
*   **Actionable Insight**: Never report "statistical significance" based on 3 seeds. If $n < 5$, report results as "descriptive statistics with wide confidence intervals" only.

### 2.2 Protocol Rigor vs. Result Magnitude
*   **Observation**: The strict subject split (Training: Subjects 1-10, 11-12, etc., or the full 21/9 split) successfully eliminated data leakage, but the resulting performance gap between Linear SGD and Random Forest was small and statistically ambiguous.
*   **Lesson**: Many "SOTA" claims in recent literature may be artifacts of loose protocols (e.g., global normalization) rather than genuine model superiority. A strict protocol often reveals that simple linear models are far more competitive than advertised.
*   **Actionable Insight**: The primary contribution of this work is the **audit methodology**, not the model performance. The "smoke test" nature of the experiment is a feature, not a bug, as it exposes the fragility of current benchmarks.

### 2.3 Data Pathing and Resource Planning
*   **Observation**: A prior run failed with `FileNotFoundError: README.txt`. This was caused by a mismatch between the expected dataset directory structure and the actual mounted volume.
*   **Lesson**: Hardcoding paths relative to the root or assuming a specific directory structure without verification leads to pipeline failures.
*   **Actionable Insight**: Implement a **manifest check** at the start of the `resource_planning` stage. Verify the existence of critical files (`README.txt`, `features.txt`, `train/`, `test/`) before initiating model training.

### 2.4 Claim Boundaries
*   **Observation**: The initial draft of the analysis tempted a claim of "Linear SGD superiority" due to the lower p-value of the t-test.
*   **Lesson**: In reproducibility studies, **negative or ambiguous results are valid findings**. Overclaiming based on weak statistics undermines the entire audit.
*   **Actionable Insight**: The `scientific_claims_allowed` flag must be set to `false` when statistical tests disagree or $n$ is low. The report must explicitly state: "No significant difference can be claimed."

---

## 3. Reproducibility Notes

### 3.1 Dataset and Protocol
*   **Dataset**: UCI-HAR (Human Activity Recognition using Smartphones).
*   **Split Strategy**: Strict subject-independent split.
    *   **Training**: Subjects 1-21 (IDs: 1, 3, 5, 6, 7, 8, 11, 14, 15, 16, 17, 19, 21, 22, 23, 25, 26, 27, 28, 29, 30).
    *   **Testing**: Subjects 22-30 (IDs: 2, 4, 9, 10, 12, 13, 18, 20, 24).
    *   *Note*: Ensure no overlap in subject IDs between train and test sets.
*   **Preprocessing**:
    *   Z-score normalization parameters ($\mu, \sigma$) **must** be computed *only* on the training set.
    *   Apply these parameters to the test set.
    *   **Do not** fit scalers on the full dataset (prevents leakage).

### 3.2 Expe

... (truncated, see full artifact)


{
  "run_id": "qwen-real-imu-chain-smoke-20260718",
  "generated": "2026-07-20T06:50:16+00:00",
  "artifact_count": 521,
  "artifacts": [
    "stage-01/decision.json",
    "stage-01/goal.md",
    "stage-01/hardware_profile.json",
    "stage-01/stage_health.json",
    "stage-02/decision.json",
    "stage-02/problem_tree.md",
    "stage-02/stage_health.json",
    "stage-02/topic_evaluation.json",
    "stage-03/decision.json",
    "stage-03/queries.json",
    "stage-03/search_plan.yaml",
    "stage-03/sources.json",
    "stage-03/stage_health.json",
    "stage-04/candidates.jsonl",
    "stage-04/decision.json",
    "stage-04/references.bib",
    "stage-04/search_meta.json",
    "stage-04/stage_health.json",
    "stage-04/web_search_result.json",
    "stage-05/decision.json",
    "stage-05/shortlist.jsonl",
    "stage-05/stage_health.json",
    "stage-06/cards/card_001.md",
    "stage-06/cards/card_002.md",
    "stage-06/cards/card_003.md",
    "stage-06/cards/card_004.md",
    "stage-06/cards/card_005.md",
    "stage-06/cards/card_006.md",
    "stage-06/cards/card_007.md",
    "stage-06/cards/card_008.md",
    "stage-06/cards/card_009.md",
    "stage-06/cards/card_010.md",
    "stage-06/cards/card_011.md",
    "stage-06/cards/card_012.md",
    "stage-06/cards/card_013.md",
    "stage-06/cards/card_014.md",
    "stage-06/cards/card_015.md",
    "stage-06/decision.json",
    "stage-06/stage_health.json",
    "stage-07/decision.json",
    "stage-07/stage_health.json",
    "stage-07/synthesis.md",
    "stage-08/candidate_ideas.md",
    "stage-08/challenge_insight_tree.json",
    "stage-08/challenge_insight_tree.md",
    "stage-08/citation_graph.json",
    "stage-08/core_ideas.md",
    "stage-08/decision.json",
    "stage-08/global_rag_index.jsonl",
    "stage-08/hypotheses.md",
    "stage-08/hypotheses_raw.md",
    "stage-08/idea_branch_synthesis.md",
    "stage-08/idea_branches/conservative_publishable.md",
    "stage-08/idea_branches/high_risk_high_reward.md",
    "stage-08/idea_branches/mvp_fast_validation.md",
    "stage-08/idea_decision_table.md",
    "stage-08/idea_evidence_pack.md",
    "stage-08/idea_pivot.md",
    "stage-08/idea_quality_scores.json",
    "stage-08/idea_quality_summary.md",
    "stage-08/idea_review.md",
    "stage-08/idea_role_review.md",
    "stage-08/idea_tournament.json",
    "stage-08/idea_tournament.md",
    "stage-08/ideation_memory_update.md",
    "stage-08/novelty_report.json",
    "stage-08/perspectives/contrarian.md",
    "stage-08/perspectives/innovator.md",
    "stage-08/perspectives/pragmatist.md",
    "stage-08/rag_index.jsonl",
    "stage-08/rag_retrieval_report.json",
    "stage-08/reflections/idea_handoff.md",
    "stage-08/reflections/post_tournament_evidence.md",
    "stage-08/reflections/pre_ideation_strategy.md",
    "stage-08/stage_health.json",
    "stage-09/benchmark_agent/acquisition_0.json",
    "stage-09/benchmark_agent/acquisition_1.json",
    "stage-09/benchmark_agent/benchmark_plan.json",
    "stage-09/benchmark_agent/selection_results.json",
    "stage-09/benchmark_agent/survey_results.json",
    "stage-09/benchmark_agent/validation_0.json",
    "stage-09/benchmark_agent/validation_1.json",
    "stage-09/benchmark_plan.json",
    "stage-09/decision.json",
    "stage-09/domain_profile.json",
    "stage-09/exp_plan.yaml",
    "stage-09/exp_plan_diagnostics.json",
    "stage-09/stage_health.json",
    "stage-10/codebase_candidates.json",
    "stage-10/decision.json",
    "stage-10/stage_health.json",
    "stage-11/claw_agent_log.json",
    "stage-11/claw_system_prompt.md",
    "stage-11/claw_workspace_1784378153_18228/.snapshots/main_v001.py",
    "stage-11/claw_workspace_1784378153_18228/.snapshots/main_v002.py",
    "stage-11/claw_workspace_1784378153_18228/.snapshots/main_v003.py",
    "stage-11/claw_workspace_1784378153_18228/.snapshots/main_v004.py",
    "stage-11/claw_workspace_1784378153_18228/.snapshots/main_v005.py",
    "stage-11/claw_workspace_1784378153_18228/.snapshots/main_v006.py",
    "stage-11/claw_workspace_1784378153_18228/.snapshots/main_v007.py",
    "stage-11/claw_workspace_1784378153_18228/CODEGEN.md",
    "stage-11/claw_workspace_1784378153_18228/EXPERIMENT_PLAN.yaml",
    "stage-11/claw_workspace_1784378153_18228/__pycache__/main.cpython-311.pyc",
    "stage-11/claw_workspace_1784378153_18228/data/UCI HAR Dataset/.DS_Store",
    "stage-11/claw_workspace_1784378153_18228/data/UCI HAR Dataset/README.txt",
    "stage-11/claw_workspace_1784378153_18228/data/UCI HAR Dataset/activity_labels.txt",
    "stage-11/claw_workspace_1784378153_18228/data/UCI HAR Dataset/dataset_manifest.json",
    "stage-11/claw_workspace_1784378153_18228/data/UCI HAR Dataset/features.txt",
    "stage-11/claw_workspace_1784378153_18228/data/UCI HAR Dataset/features_info.txt",
    "stage-11/claw_workspace_1784378153_18228/data/UCI HAR Dataset/test/Inertial Signals/body_acc_x_test.txt",
    "stage-11/claw_workspace_1784378153_18228/data/UCI HAR Dataset/test/Inertial Si

... (truncated, see full artifact)
