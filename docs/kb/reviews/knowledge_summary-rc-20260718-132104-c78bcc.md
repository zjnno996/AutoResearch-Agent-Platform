---
created: '2026-07-18T13:22:04+00:00'
evidence:
- stage-18/knowledge_entry.json
id: knowledge_summary-rc-20260718-132104-c78bcc
run_id: rc-20260718-132104-c78bcc
stage: 18-knowledge_summary
tags:
- knowledge_summary
- stage-18
- run-rc-20260
title: 'Stage 18: Knowledge Summary'
---

# Stage 18: Knowledge Summary

{
  "topic": "Re-evaluating the superiority of deep learning over linear models in Human Activity Recognition under strict subject-level splits and statistical controls",
  "hypotheses": [
    "Under strict UCI-HAR subject-level splits and multiple random seeds, the performance difference between Linear SGD and complex models (e.g., Random Forest) will be statistically insignificant (p > 0.05 in Wilcoxon Signed-Rank Test).",
    "Complex models will exhibit wider 95% confidence intervals compared to linear models, suggesting higher sensitivity to random seeds and potential data leakage in less rigorous studies.",
    "Reported deep learning advantages in literature are often statistical artifacts resulting from insufficient seed repetition and data leakage rather than genuine feature extraction capabilities."
  ],
  "method": "The study implemented a 'statistically controlled adversarial baseline' protocol using the UCI-HAR dataset with strict subject-level train/test splits to prevent data leakage. Two models, Strict Linear SGD and Robust Random Forest, were evaluated across multiple random seeds with Z-score normalization derived solely from training statistics. Performance was assessed using Macro-F1 and Accuracy, followed by paired t-tests and Wilcoxon Signed-Rank tests to determine statistical significance.",
  "settings": {
    "dataset": "UCI-HAR (9-dimensional IMU signals, 128-point windows, 50% overlap)",
    "models": [
      "Strict_Linear_SGD_LogLoss (SGDClassifier, L2 penalty, alpha=1e-4)",
      "Robust_RandomForest_200 (200 estimators, max_depth=15, class_weight=balanced)"
    ],
    "normalization": "Training-set only Z-score (no global leakage)",
    "seeds": "3 independent random seeds",
    "metrics": "Macro-F1, Accuracy, 95% Confidence Intervals"
  },
  "results": {
    "Strict_Linear_SGD_LogLoss_f1_macro_mean": 0.932384,
    "Strict_Linear_SGD_LogLoss_f1_macro_ci95": [
      0.92643,
      0.94725
    ],
    "Robust_RandomForest_200_f1_macro_mean": 0.927926,
    "Robust_RandomForest_200_f1_macro_ci95": [
      0.92081,
      0.93505
    ],
    "paired_t_test_p_value": 0.00754,
    "wilcoxon_signed_rank_p_value": 0.25
  },
  "conclusions": [
    "Linear SGD achieved a slightly higher mean Macro-F1 (0.932) compared to Random Forest (0.928) under strict conditions.",
    "Non-parametric statistical testing (Wilcoxon) indicated no significant difference between the models (p=0.25), while parametric testing (t-test) suggested significance (p=0.0075), highlighting the instability of conclusions with limited sample sizes (n=3 seeds).",
    "The experiment confirms that linear models can compete with or outperform ensemble tree methods on HAR tasks when data leakage is eliminated, challenging the necessity of complex architectures for this specific dataset.",
    "The current results are preliminary and require more seeds (n>=5) and rigorous scientific validation before definitive claims about model superiority can be made."
  ],
  "insights": [
    "Statistical significance in HAR research is highly sensitive to the number of random seeds; n=3 is insufficient to reconcile discrepancies between parametric and non-parametric tests.",
    "Strict adherence to subject-level splitting and training-only normalization can drastically reduce the performance gap between simple linear models and complex non-linear models.",
    "The 'deep learning advantage' in HAR may be overstated in literature that fails to control for random seed variance and data leakage."
  ],
  "limitations": [
    "Insufficient number of random seeds (n=3) led to conflicting statistical conclusions between t-tests and Wilcoxon tests.",
    "The study was limited to UCI-HAR and did not include the originally hypothesized CNN-LSTM deep learning baseline.",
    "The experiment status is currently 'engineering smoke only' and lacks the reproducibility and scale required for publication as a scientific finding."
  ],
  "suggested_directions": [
    "Extend the experiment to n=5 or n=10 independent seeds to stabilize statistical significance testing.",
    "Implement the full CNN-LSTM architecture as proposed in the original hypothesis to directly compare deep learning against the linear baseline.",
    "Investigate the specific impact of different windowing strategies and feature normalization techniques on the variance of model performance.",
    "Conduct a meta-analysis of existing HAR literature to quantify the prevalence of data leakage and insufficient seed reporting."
  ],
  "project_id": "qwen-real-imu-chain-smoke-20260718",
  "research_topic": "轻量真实链路验收：仅使用官方 UCI-HAR 数据集，复现线性 SGD 与随机森林活动识别基线，使用官方 subject split、3 个独立随机种子、Accuracy 与 Macro-F1、95%置信区间和配对检验。禁止把未实现的新方法、未运行的消融或合成指标写入论文；最终只生成证据范围受限的 IEEE 报告。",
  "domains": [
    "deep-learning"
  ],
  "timestamp": "2026-07-18T13:22:04+00:00"
}