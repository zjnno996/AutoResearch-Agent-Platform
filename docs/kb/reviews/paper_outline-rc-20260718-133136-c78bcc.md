---
created: '2026-07-18T13:33:41+00:00'
evidence:
- stage-19/outline.md
id: paper_outline-rc-20260718-133136-c78bcc
run_id: rc-20260718-133136-c78bcc
stage: 19-paper_outline
tags:
- paper_outline
- stage-19
- run-rc-20260
title: 'Stage 19: Paper Outline'
---

# Stage 19: Paper Outline

# Paper Outline: Evidence-Limited IEEE Engineering Report

## Executive Summary & Strategy
**Core Constraint**: The experiment is classified as **"Engineering Smoke Only"** with a `readiness_score` of 49.0. The statistical tests (t-test vs. Wilcoxon) disagree due to low sample size (n=3).
**Strategy**: The paper will **not** claim a novel method or a definitive scientific breakthrough. Instead, it will be framed as a **Reproducibility Audit and Baseline Verification Report**. The narrative focuses on the *rigor of the evaluation protocol* (official splits, seed independence, statistical caution) rather than the superiority of a specific model. We will explicitly highlight the statistical ambiguity as a limitation and a call for future larger-scale validation, adhering strictly to the "evidence-limited" boundary.

**No New Method Name**: Since no new method was invented, titles will follow the `Context: Action` format, avoiding the `MethodName: Subtitle` convention reserved for novel contributions.

---

## Candidate Titles

### Option 1
**Title**: UCI-HAR Baseline Audit: Reproducibility of Linear SGD and Random Forest under Strict Subject Splits
- **Memorability**: 3/5 (Descriptive, functional)
- **Specificity**: 5/5 (Explicitly names dataset, methods, and constraint)
- **Novelty Signal**: 2/5 (Clearly signals a verification/audit task, not a breakthrough)
- **Rationale**: Best fits the "Engineering Report" constraint. It sets accurate expectations that this is an audit, not a new discovery.

### Option 2
**Title**: Verifying Human Activity Recognition: A Statistical Reproduction of Linear and Ensemble Baselines
- **Memorability**: 3/5 (Standard academic phrasing)
- **Specificity**: 4/5 (Mentions "Statistical Reproduction")
- **Novelty Signal**: 2/5 (Focuses on the process of verification)
- **Rationale**: Emphasizes the statistical methodology (paired tests, CI) which is the strongest part of the current evidence, while downplaying the model performance.

### Option 3
**Title**: Lightweight Link Acceptance: A Feasibility Study on UCI-HAR with Official Splits and Rigorous Validation
- **Memorability**: 4/5 (Uses the user's specific "Lightweight Link Acceptance" terminology)
- **Specificity**: 4/5 (Mentions "Feasibility Study" and "Official Splits")
- **Novelty Signal**: 3/5 (Frames the work as a "Link Acceptance" protocol check)
- **Rationale**: Directly adopts the user's research context ("轻量真实链路验收") as the core theme, positioning the paper as a protocol validation rather than a model paper.

**Recommended Choice**: **Option 1**. It is the most precise regarding the "Audit" nature required by the `REFINE` decision and avoids overclaiming novelty.

---

## Detailed Section Outline

### 1. Title Page
- **Title**: UCI-HAR Baseline Audit: Reproducibility of Linear SGD and Random Forest under Strict Subject Splits
- **Authors**: [Author Names]
- **Affiliation**: [Institution]
- **Abstract**: (See Section 2)

### 2. Abstract (Target: 190 words)
- **S1 (Problem)**: Establishing reliable baselines in Human Activity Recognition (HAR) requires strict adherence to dataset protocols, yet many studies obscure subject-level leakage or insufficient statistical validation.
- **S2 (Gap)**: Existing literature often lacks rigorous reproducibility checks on the official UCI-HAR split, leading to ambiguous performance claims for lightweight models.
- **S3 (Method)**: We present a reproducibility audit of two canonical baselines: Linear Stochastic Gradient Descent (LogLoss) and Random Forest (200 estimators).
- **S4 (Method)**: The study enforces the official subject-based train/test split, utilizes three independent random seeds, and reports Accuracy and Macro-F1 with 95% confidence intervals.
- **S5 (Results)**: Linear SGD achieved a mean Macro-F1 of 0.936, while Random Forest scored 0.928.
- **S6 (Caveat)**: However, paired statistical tests (t-test vs. Wilcoxon) yielded conflicting significance levels (p=0.007 vs. p=0.25) due to limited seed count.
- **S7 (Conclusion)**: We conclude that while Linear SGD shows competitive performance, definitive superiority claims require expanded sampling to resolve statistical instability.

### 3. Introduction (Target: 900 words)
- **Paragraph 1: Motivation**:
    - Discuss the critical role of HAR in healthcare and IoT.
    - Emphasize the need for "lightweight" models that can run on edge devices.
    - State the problem: "Lightweight real link acceptance" (验证轻量真实链路) requires not just high accuracy, but *proven* reliability under strict constraints.
- **Paragraph 2: The Reproducibility Gap**:
    - Cite 3-5 papers (e.g., Anguita et al. [1], recent deep learning baselines [2-4]) that claim high accuracy.
    - Critique: Many fail to report subject-level leakage prevention or use arbitrary random splits.
    - Highlight the lack of consensus on whether simple linear models can match ensemble performance when the split is strictly controlled.
- **Paragraph 3: Our Approach (The Audit)**:
 

... (truncated, see full artifact)
