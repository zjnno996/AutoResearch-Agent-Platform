---
created: '2026-07-18T13:29:50+00:00'
evidence:
- stage-19/outline.md
id: paper_outline-rc-20260718-132752-c78bcc
run_id: rc-20260718-132752-c78bcc
stage: 19-paper_outline
tags:
- paper_outline
- stage-19
- run-rc-20260
title: 'Stage 19: Paper Outline'
---

# Stage 19: Paper Outline

# Paper Outline: Evidence-Limited IEEE Technical Report

## **Method Name Proposal**
**Name**: **L-HAR** (Lightweight Human Activity Recognition Audit)
*Rationale*: Acronym is short (4 chars), directly references the domain (HAR), and the "L" signifies both "Lightweight" and "Link/Audit" (verification). It avoids claiming a "new algorithm" while branding the *reproduction protocol*.

## **Candidate Titles**
*(Format: MethodName: Subtitle | Max 14 words)*

| # | Title | Memorability (1-5) | Specificity (1-5) | Novelty Signal (1-5) | Rationale |
|---|---|---|---|---|---|
| 1 | **L-HAR: A Strict Reproduction Audit of Linear SGD and Random Forests on UCI-HAR** | 4 | 5 | 2 | Highly specific about methods and dataset; signals "audit" rather than "breakthrough." |
| 2 | **L-HAR: Verifying Baseline Performance in Human Activity Recognition with Subject-Specific Splits** | 4 | 4 | 3 | Focuses on the "verification" aspect and the critical subject-split constraint. |
| 3 | **L-HAR: Engineering Smoke Testing of Standard Classifiers on the UCI-HAR Benchmark** | 3 | 5 | 1 | Uses "Smoke Testing" to manage expectations; extremely honest about the engineering nature. |

**Selected Title**: **L-HAR: A Strict Reproduction Audit of Linear SGD and Random Forests on UCI-HAR**
*Reason*: It balances academic rigor with the "audit" nature of the work, clearly defining the scope without overclaiming novelty.

---

## **Section-by-Section Outline**

### **1. Abstract**
*   **Word Count Target**: 180–220 words.
*   **Goal**: Clearly state that this is a *reproduction audit* (not a novel method proposal) and report the observed performance bounds with statistical caveats.
*   **Structure**:
    *   **S1-S2 (Problem)**: Despite the ubiquity of UCI-HAR in literature, rigorous, seed-stable reproduction of standard baselines (Linear SGD, Random Forest) under strict subject-split protocols is often inconsistent. Many papers lack transparent statistical validation of these "solved" benchmarks.
    *   **S3-S4 (Method)**: We present **L-HAR**, an evidence-limited engineering audit that re-executes Linear SGD (LogLoss) and Random Forest (200 trees) on the official UCI-HAR dataset. We enforce the official subject-based train/test split, utilize three independent random seeds, and report Accuracy and Macro-F1 with 95% Confidence Intervals.
    *   **S5-S6 (Results)**: Our audit reveals that Linear SGD achieves a mean Macro-F1 of **0.9368** (95% CI: [0.9264, 0.9472]), slightly outperforming Random Forest (0.9279). However, with only three seeds, statistical tests (t-test vs. Wilcoxon) yield conflicting p-values (0.007 vs. 0.25), indicating that current evidence supports a *feasibility* claim but not a definitive superiority claim.
    *   **Constraint**: Explicitly state that results are descriptive engineering evidence requiring further validation.

### **2. Introduction**
*   **Word Count Target**: 800–1000 words.
*   **Goal**: Frame the "Lightweight Real Link Acceptance" as a necessary engineering checkpoint in the HAR pipeline, emphasizing reproducibility over novelty.
*   **Paragraph 1 (Motivation)**: Human Activity Recognition (HAR) relies heavily on the UCI-HAR dataset. While deep learning dominates recent literature, the performance of classical baselines (Linear Models, Ensembles) remains a critical reference point for "real link" acceptance in resource-constrained or safety-critical deployments.
*   **Paragraph 2 (The Gap)**: Existing literature often reports single-run results or aggregates without strict subject-level separation, leading to inflated performance estimates. There is a lack of standardized, seed-stable reproduction of these baselines that adheres strictly to the original UCI protocol (subject split, 561 precomputed features).
*   **Paragraph 3 (Our Approach)**: We introduce **L-HAR**, a strict reproduction protocol. We do not propose a new architecture. Instead, we rigorously re-implement `Strict_Linear_SGD_LogLoss` and `Robust_RandomForest_200` using the official dataset manifest, enforcing a fixed subject split and reporting metrics across three independent seeds.
*   **Paragraph 4 (Contributions)**:
    1.  **Strict Protocol Enforcement**: A reproducible pipeline adhering exactly to UCI-HAR subject IDs (Train: 21 subjects, Test: 9 subjects).
    2.  **Statistical Transparency**: Reporting 95% Confidence Intervals and paired test results, highlighting the instability of conclusions drawn from low seed counts ($N=3$).
    3.  **Baseline Audit**: Providing a verified performance ceiling for linear and tree-based methods on the standard 561-feature representation, serving as a "smoke test" for future lightweight HAR claims.

### **3. Related Work**
*   **Word Count Target**: 600–800 words.
*   **Goal**: Contextualize the baselines without claiming to beat them. Organize by "Classical Baselines" and "Reproducibility Challenges."
*   **Subsection 3.1: Classical Baselines in HAR**:
    *   Review early HAR works utilizing SV

... (truncated, see full artifact)
