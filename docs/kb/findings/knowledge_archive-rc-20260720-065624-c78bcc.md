---
created: '2026-07-20T06:57:51+00:00'
evidence:
- stage-24/archive.md
- stage-24/bundle_index.json
id: knowledge_archive-rc-20260720-065624-c78bcc
run_id: rc-20260720-065624-c78bcc
stage: 24-knowledge_archive
tags:
- knowledge_archive
- stage-24
- run-rc-20260
title: 'Stage 24: Knowledge Archive'
---

# Stage 24: Knowledge Archive

# Research Retrospective: UCI-HAR Baseline Audit (Engineering Smoke Test)

**Date:** 2024-07-18  
**Status:** `REFINE` (Engineering Report Only)  
**Readiness Score:** 49.0/100  
**Verdict:** **Do not submit as scientific evidence.** The current results serve as a reproducible engineering baseline but lack the statistical power for inferential claims.

---

## 1. Executive Summary

This retrospective documents a strict reproducibility audit of two classical baselines—Linear Stochastic Gradient Descent (SGD) and Random Forest (RF)—on the UCI-HAR dataset. The experiment was designed to enforce the official subject-independent split, utilize three independent random seeds, and report 95% confidence intervals alongside paired statistical tests.

While the pipeline executed successfully and generated parseable metrics, the **statistical analysis reveals a critical instability**: the parametric t-test ($p=0.007$) and non-parametric Wilcoxon test ($p=0.25$) yield contradictory conclusions. This discrepancy, driven by the low sample size ($N=3$ seeds), prevents a definitive claim of superiority for either model. Consequently, the output is classified as an **Engineering Smoke Test** rather than a scientific publication. The primary contribution of this run is the validated pipeline and the explicit demonstration of why $N=3$ is insufficient for rigorous HAR benchmarking.

---

## 2. Lessons Learned

### 2.1 Statistical Power & Sample Size
*   **Lesson:** In high-variance domains like HAR, $N=3$ seeds are insufficient to resolve the distributional assumptions of parametric vs. non-parametric tests.
*   **Evidence:** The t-test suggested significant superiority of Linear SGD ($p=0.007$), while the Wilcoxon test failed to reject the null hypothesis ($p=0.25$).
*   **Action:** Future iterations must increase the seed count to $N \ge 10$ to stabilize the confidence intervals and align statistical tests.

### 2.2 Data Pathing & Resource Planning
*   **Lesson:** Strict adherence to dataset directory structures is non-negotiable. A missing `README.txt` or misaligned path in the resource planning stage caused a pipeline failure in prior runs.
*   **Evidence:** `Stage resource_planning failed: [Errno 2] No such file or directory: .../UCI HAR Dataset/README.txt`.
*   **Action:** Implement a pre-flight check script that validates the existence of all critical manifest files (`README.txt`, `features.txt`, `activity_labels.txt`) before the experiment loop begins.

### 2.3 The "Smoke Test" Mindset
*   **Lesson:** It is scientifically honest to label a run as an "engineering smoke test" when the data does not support a hypothesis. Attempting to force a conclusion from conflicting p-values degrades the credibility of the entire study.
*   **Evidence:** The `readiness_score` of 49.0 correctly flagged the results as "requires_repeated_runs_and_scientific_validation."
*   **Action:** Adopt a "fail-fast" reporting policy. If statistical tests disagree, the paper section must explicitly state the ambiguity rather than cherry-picking the favorable p-value.

---

## 3. Reproducibility Notes

### 3.1 Experimental Protocol
*   **Dataset:** UCI-HAR (Official Release).
*   **Split:** Strict Subject Independence.
    *   *Training:* Subjects {1, 3, 5, 6, 7, 8, 11, 14, 15, 16, 17, 19, 21, 22, 23, 25, 26, 27, 28, 29, 30} (21 subjects).
    *   *Testing:* Subjects {2, 4, 9, 10, 12, 13, 18, 20, 24} (9 subjects).
*   **Preprocessing:** Z-score normalization parameters ($\mu, \sigma$) computed **only** on the training set. No global normalization.
*   **Models:**
    1.  `Strict_Linear_SGD_LogLoss`: Logistic loss, L2 regularization.
    2.  `Robust_RandomForest_200`: 200 estimators, Gini impurity.
*   **Seeds:** 11, 29, 47 (Fixed for this run).

### 3.2 Artifact Integrity
*   **Source Files:** All raw metrics, per-seed logs, and configuration YAMLs are preserved in `experiment_final/outputs/`.
*   **Determinism:** The pipeline is deterministic given the seed. The variance observed is purely due to the model's stochasticity (SGD initialization/RF bootstrap) and data split order.
*   **Verification:** The `dataset_manifest.json` and `features_info.txt` confirm the 561-feature input space was used, avoiding raw signal processing discrepancies.

### 3.3 Known Limitations
*   **Feature Engineering:** This audit uses the *pre-engineered* features provided by Anguita et al. It does not evaluate the impact of raw signal windowing or FFT parameters, which are potential sources of variance in other studies.
*   **Metric Scope:** Only Accuracy and Macro-F1 were reported. No latency, energy, or model size metrics were collected, limiting the "Edge AI" claim to theoretical inference.

---

## 4. Statistical Analysis Review

The core finding of this retrospective is the **statistical instability** observed with $N=3$.

| Metric | Linear SGD (Mean) | Random Forest (Mean) | Diff | T-Test $p$ | Wilcoxon $p$ | Conclusion |
| :--- | :--- | :--- | :--- | :--- | :--- | 

... (truncated, see full artifact)


{
  "run_id": "qwen-real-imu-chain-smoke-20260718",
  "generated": "2026-07-20T06:57:51+00:00",
  "artifact_count": 526,
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
