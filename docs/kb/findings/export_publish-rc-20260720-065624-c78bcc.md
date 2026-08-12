---
created: '2026-07-20T06:58:06+00:00'
evidence:
- stage-25/paper_final.md
- stage-25/invalid_citations.json
- stage-25/paper_final_latex.md
- stage-25/references.bib
- stage-25/paper.tex
- stage-25/charts//
- stage-25/code//
- stage-25/reproducibility_manifest.json
- stage-25/final_claim_integrity_report.json
id: export_publish-rc-20260720-065624-c78bcc
run_id: rc-20260720-065624-c78bcc
stage: 25-export_publish
tags:
- export_publish
- stage-25
- run-rc-20260
title: 'Stage 25: Export Publish'
---

# Stage 25: Export Publish

# UCI-HAR Baseline Audit: Reproducibility of Linear SGD and Random Forest under Strict Subject Splits

## Abstract
Human Activity Recognition (HAR) benchmarks can be distorted by subject-level data leakage and incomplete uncertainty reporting. This study presents an engineering reproducibility audit of Linear Stochastic Gradient Descent (LogLoss) and Random Forest (200 estimators) on the official UCI-HAR dataset. We enforce the original subject-based train/test split, use independent seeds 11, 29, and 47, and report Accuracy and Macro-F1 with 95% confidence intervals. In this configuration, Linear SGD produced mean Macro-F1 0.9368 and Random Forest produced 0.9279. The paired t-test returned $p=0.0075$, whereas the Wilcoxon signed-rank test returned $p=0.2500$. With only three pairs, these conflicting diagnostics and their distributional assumptions do not support an inferential superiority claim. The contribution is therefore an auditable baseline execution and a bounded example of statistical reporting, not a new method or a general comparison of model architectures. The released artifacts retain per-seed outputs, exact subject identifiers, preprocessing constraints, model settings, and provenance so that every aggregate can be independently recomputed.

## 1. Introduction

Human Activity Recognition (HAR) has become a cornerstone of ubiquitous computing, enabling applications ranging from elderly care monitoring to personalized fitness tracking [rahmani2021machine]. As these systems transition from controlled laboratory settings to real-world deployment, the demand for lightweight models capable of operating on resource-constrained edge devices has intensified [novac2021quantization, lim2023efficient]. Simple, interpretable models should therefore be evaluated as explicit baselines before additional architectural complexity is credited with an improvement. However, baseline performance is often reported without strict adherence to dataset protocols or adequate uncertainty analysis [demrozi2021bhar]. A particularly important risk is data leakage when normalization statistics are computed over the entire dataset rather than strictly on the training split [chen2024private].

A critical gap exists in the evaluation of classical machine learning models on the UCI-HAR dataset, often regarded as the "MNIST" of human activity recognition. While early studies established the utility of Support Vector Machines (SVM) and Random Forests [tahir2022stochastic], recent trends have shifted almost exclusively toward complex Convolutional Neural Networks (CNN) and Recurrent Neural Networks (RNN) [mekruksavanich2023hybrid, khatun2022deep]. This shift has inadvertently obscured the true performance ceiling of linear and ensemble methods. Many contemporary studies report marginal accuracy gains over baselines without accounting for subject-level data leakage, where information from test subjects inadvertently leaks into the training phase during feature normalization or windowing [chen2024private]. Furthermore, the reliance on single-seed evaluations or insufficient statistical testing (e.g., reporting only mean accuracy without confidence intervals) makes it difficult to distinguish genuine architectural improvements from random variance [yuan2024bstcahar]. The statistical power of such studies is often negligible, rendering claims of "significant improvement" scientifically unsound.

To address these issues, we conduct a strict reproducibility audit of two canonical baselines: Linear Stochastic Gradient Descent (SGD) and Random Forest. Unlike studies proposing novel architectures, our contribution lies in the rigorous enforcement of the official UCI-HAR subject split and the application of robust statistical validation. We utilize three independent random seeds to estimate variance, compute 95% confidence intervals for both Accuracy and Macro-F1, and perform paired statistical tests to assess significance. This approach allows us to isolate the effect of the evaluation protocol from model complexity. Our results demonstrate that while Linear SGD yields a higher mean Macro-F1 than Random Forest, the statistical significance of this difference is ambiguous when assessed with $n=3$, underscoring the need for larger sample sizes in future benchmarks.

The contribution has four connected parts. First, the executable pipeline enforces the official subject-based train/test split and train-only preprocessing. Second, it provides a side-by-side descriptive audit of Linear SGD and Random Forest with Accuracy, Macro-F1, confidence intervals, and paired diagnostics. Third, it preserves the disagreement between the parametric and non-parametric tests instead of selecting the favorable result. Fourth, the artifact retains configurations, seeds, subject identifiers, per-seed metrics, and provenance so that the reported aggregates can be recomputed.

The remainder of this paper is organized as follows: Section 2 reviews HAR baselines and 

... (truncated, see full artifact)


[
  "bishop2006pattern",
  "ivc2025randomness",
  "kohavi1995study",
  "scott2023statistical"
]

# UCI-HAR Baseline Audit: Reproducibility of Linear SGD and Random Forest under Strict Subject Splits

## Abstract
Human Activity Recognition (HAR) benchmarks can be distorted by subject-level data leakage and incomplete uncertainty reporting. This study presents an engineering reproducibility audit of Linear Stochastic Gradient Descent (LogLoss) and Random Forest (200 estimators) on the official UCI-HAR dataset. We enforce the original subject-based train/test split, use independent seeds 11, 29, and 47, and report Accuracy and Macro-F1 with 95% confidence intervals. In this configuration, Linear SGD produced mean Macro-F1 0.9368 and Random Forest produced 0.9279. The paired t-test returned $p=0.0075$, whereas the Wilcoxon signed-rank test returned $p=0.2500$. With only three pairs, these conflicting diagnostics and their distributional assumptions do not support an inferential superiority claim. The contribution is therefore an auditable baseline execution and a bounded example of statistical reporting, not a new method or a general comparison of model architectures. The released artifacts retain per-seed outputs, exact subject identifiers, preprocessing constraints, model settings, and provenance so that every aggregate can be independently recomputed.

## 1. Introduction

Human Activity Recognition (HAR) has become a cornerstone of ubiquitous computing, enabling applications ranging from elderly care monitoring to personalized fitness tracking \cite{rahmani2021machine}. As these systems transition from controlled laboratory settings to real-world deployment, the demand for lightweight models capable of operating on resource-constrained edge devices has intensified \cite{novac2021quantization, lim2023efficient}. Simple, interpretable models should therefore be evaluated as explicit baselines before additional architectural complexity is credited with an improvement. However, baseline performance is often reported without strict adherence to dataset protocols or adequate uncertainty analysis \cite{demrozi2021bhar}. A particularly important risk is data leakage when normalization statistics are computed over the entire dataset rather than strictly on the training split \cite{chen2024private}.

A critical gap exists in the evaluation of classical machine learning models on the UCI-HAR dataset, often regarded as the "MNIST" of human activity recognition. While early studies established the utility of Support Vector Machines (SVM) and Random Forests \cite{tahir2022stochastic}, recent trends have shifted almost exclusively toward complex Convolutional Neural Networks (CNN) and Recurrent Neural Networks (RNN) \cite{mekruksavanich2023hybrid, khatun2022deep}. This shift has inadvertently obscured the true performance ceiling of linear and ensemble methods. Many contemporary studies report marginal accuracy gains over baselines without accounting for subject-level data leakage, where information from test subjects inadvertently leaks into the training phase during feature normalization or windowing \cite{chen2024private}. Furthermore, the reliance on single-seed evaluations or insufficient statistical testing (e.g., reporting only mean accuracy without confidence intervals) makes it difficult to distinguish genuine architectural improvements from random variance \cite{yuan2024bstcahar}. The statistical power of such studies is often negligible, rendering claims of "significant improvement" scientifically unsound.

To address these issues, we conduct a strict reproducibility audit of two canonical baselines: Linear Stochastic Gradient Descent (SGD) and Random Forest. Unlike studies proposing novel architectures, our contribution lies in the rigorous enforcement of the official UCI-HAR subject split and the application of robust statistical validation. We utilize three independent random seeds to estimate variance, compute 95% confidence intervals for both Accuracy and Macro-F1, and perform paired statistical tests to assess significance. This approach allows us to isolate the effect of the evaluation protocol from model complexity. Our results demonstrate that while Linear SGD yields a higher mean Macro-F1 than Random Forest, the statistical significance of this difference is ambiguous when assessed with $n=3$, underscoring the need for larger sample sizes in future benchmarks.

The contribution has four connected parts. First, the executable pipeline enforces the official subject-based train/test split and train-only preprocessing. Second, it provides a side-by-side descriptive audit of Linear SGD and Random Forest with Accuracy, Macro-F1, confidence intervals, and paired diagnostics. Third, it preserves the disagreement between the parametric and non-parametric tests instead of selecting the favorable result. Fourth, the artifact retains configurations, seeds, subject identifiers, per-seed metrics, and provenance so that the reported aggregates can be recomputed.

The remainder of this paper is organized as follo

... (truncated, see full artifact)


@article{mekruksavanich2023hybrid,
  title = {Hybrid convolution neural network with channel attention mechanism for sensor-based human activity recognition},
  author = {Sakorn Mekruksavanich and Anuchit Jitpattanakul},
  year = {2023},
  journal = {Scientific Reports},
  doi = {10.1038/s41598-023-39080-y},
  url = {https://doi.org/10.1038/s41598-023-39080-y},
}

@article{novac2022ucaehar,
  title = {UCA-EHAR: A Dataset for Human Activity Recognition with Embedded AI on Smart Glasses},
  author = {Pierre-Emmanuel Novac and Alain Pégatoquet and Benoît Miramond and Christophe Caquineau},
  year = {2022},
  journal = {Applied Sciences},
  doi = {10.3390/app12083849},
  url = {https://doi.org/10.3390/app12083849},
}

@article{yuan2024bstcahar,
  title = {BSTCA-HAR: Human Activity Recognition Model Based on Wearable Mobile Sensors},
  author = {Yan Yuan and Lidong Huang and Xuewen Tan and Fanchang Yang and Shiwei Yang},
  year = {2024},
  journal = {Applied Sciences},
  doi = {10.3390/app14166981},
  url = {https://doi.org/10.3390/app14166981},
}

@article{salman2024efficient,
  title = {Efficient Human Activity Recognition using PCA Dimensionality Reduction and GWO-Enhanced LSTM},
  author = {Israa Ramadhan Salman and Ali Abdulridha Rasheed and Saif Hassan and Rasha S. Hussein and Mennatallah Abdelzaher},
  year = {2024},
  journal = {Journal of Advanced Research in Applied Sciences and Engineering Technology},
  doi = {10.37934/araset.54.2.317343},
  url = {https://doi.org/10.37934/araset.54.2.317343},
}

@article{demrozi2021bhar,
  title = {B-HAR: an open-source baseline framework for in depth study of human activity recognition datasets and workflows},
  author = {Florenc Demrozi and Cristian Turetta and Graziano Pravadelli},
  year = {2021},
  journal = {arXiv preprint arXiv:2101.10870},
  doi = {10.1109/ACCESS.2024.3496497},
  eprint = {2101.10870},
  archiveprefix = {arXiv},
  url = {https://arxiv.org/abs/2101.10870},
}

@article{khatun2022deep,
  title = {Deep CNN-LSTM With Self-Attention Model for Human Activity Recognition Using Wearable Sensor},
  author = {Mst. Alema Khatun and Mohammad Abu Yousuf and Sabbir Ahmed and Md. Zia Uddin and Salem A. Alyami and Samer Al-Ashhab and Hanan Akhdar and Asaduzzaman Khan and AKM Azad and Mohammad Ali Moni},
  year = {2022},
  journal = {IEEE Journal of Translational Engineering in Health and Medicine},
  doi = {10.1109/jtehm.2022.3177710},
  url = {https://doi.org/10.1109/jtehm.2022.3177710},
}

@article{chen2024private,
  title = {Private Data Leakage in Federated Human Activity Recognition for Wearable Healthcare Devices},
  author = {Kongyang Chen and Dongping Zhang and Guan, Sijia and Mi, Bing and Shen, Jiaxing and Wang, Guoqing},
  year = {2024},
  journal = {arXiv (Cornell University)},
  doi = {10.48550/arxiv.2405.10979},
  url = {https://doi.org/10.48550/arxiv.2405.10979},
}

@article{rahmani2021machine,
  title = {Machine Learning (ML) in Medicine: Review, Applications, and Challenges},
  author = {Amir Masoud Rahmani and Efat Yousefpoor and Mohammad Sadegh Yousefpoor and Zahid Mehmood and Amir Haider and Mehdi Hosseinzadeh and Rizwan Ali Naqvi},
  year = {2021},
  journal = {Mathematics},
  doi = {10.3390/math9222970},
  url = {https://doi.org/10.3390/math9222970},
}

@article{novac2021quantization,
  title = {Quantization and Deployment of Deep Neural Networks on Microcontrollers},
  author = {Pierre-Emmanuel Novac and Ghouthi Boukli Hacene and Alain Pégatoquet and Benoît Miramond and Vincent Gripon},
  year = {2021},
  journal = {Sensors},
  doi = {10.3390/s21092984},
  url = {https://doi.org/10.3390/s21092984},
}

@article{che2023multimodal,
  title = {Multimodal Federated Learning: A Survey},
  author = {Liwei Che and Jiaqi Wang and Yao Zhou and Fenglong Ma},
  year = {2023},
  journal = {Sensors},
  doi = {10.3390/s23156986},
  url = {https://doi.org/10.3390/s23156986},
}

@article{qin2022domain,
  title = {Domain Generalization for Activity Recognition via Adaptive Feature Fusion},
  author = {Xin Qin and Jindong Wang and Yiqiang Chen and Lu Wang and Xinlong Jiang},
  year = {2022},
  journal = {ACM Transactions on Intelligent Systems and Technology},
  doi = {10.1145/3552434},
  url = {https://doi.org/10.1145/3552434},
}

@article{tahir2022stochastic,
  title = {Stochastic Recognition of Human Physical Activities via Augmented Feature Descriptors and Random Forest Model},
  author = {Sheikh Badar ud din Tahir and Abdul Basit Dogar and Rubia Fatima and Affan Yasin and Muhammad Shafiq and Javed Ali Khan and Muhammad Assam and Abdullah Mohamed and El-Awady Attia},
  year = {2022},
  journal = {Sensors},
  doi = {10.3390/s22176632},
  url = {https://doi.org/10.3390/s22176632},
}

@article{stojchevska2023real,
  title = {From Lab to Real World: Assessing the Effectiveness of Human Activity Recognition and Optimization through Personalization},
  author = {Marija Stojchevska and Mathias De Brouwer and Martijn Courteaux and Femke Ongenae and Sofie Van 

... (truncated, see full artifact)


% WARNING: Compilation failed. Errors:
% ! LaTeX Error: Unicode character l (U+2113)
% !  ==> Fatal error occurred, no output PDF file produced!
% Style file: https://www.ieee.org/conferences/publishing/templates.html
\documentclass[conference]{IEEEtran}
\usepackage{cite}
\usepackage{amsmath}
\usepackage{amssymb}
\usepackage{graphicx}
\usepackage{booktabs}
\usepackage{array}
\usepackage{url}
\usepackage{hyperref}
\usepackage{algorithmic}
\usepackage{adjustbox}
\IEEEoverridecommandlockouts

\title{UCI-HAR Baseline Audit: Reproducibility of Linear SGD and Random Forest under Strict Subject Splits}

\author{Anonymous}

\begin{document}
\maketitle

\begin{abstract}
Human Activity Recognition (HAR) benchmarks can be distorted by subject-level data leakage and incomplete uncertainty reporting. This study presents an engineering reproducibility audit of Linear Stochastic Gradient Descent (LogLoss) and Random Forest (200 estimators) on the official UCI-HAR dataset. We enforce the original subject-based train/test split, use independent seeds 11, 29, and 47, and report Accuracy and Macro-F1 with 95\% confidence intervals. In this configuration, Linear SGD produced mean Macro-F1 0.9368 and Random Forest produced 0.9279. The paired t-test returned $p=0.0075$, whereas the Wilcoxon signed-rank test returned $p=0.2500$. With only three pairs, these conflicting diagnostics and their distributional assumptions do not support an inferential superiority claim. The contribution is therefore an auditable baseline execution and a bounded example of statistical reporting, not a new method or a general comparison of model architectures. The released artifacts retain per-seed outputs, exact subject identifiers, preprocessing constraints, model settings, and provenance so that every aggregate can be independently recomputed.
\end{abstract}

\section{Introduction}

\label{sec:introduction}

Human Activity Recognition (HAR) has become a cornerstone of ubiquitous computing, enabling applications ranging from elderly care monitoring to personalized fitness tracking \cite{rahmani2021machine}. As these systems transition from controlled laboratory settings to real-world deployment, the demand for lightweight models capable of operating on resource-constrained edge devices has intensified \cite{novac2021quantization, lim2023efficient}. Simple, interpretable models should therefore be evaluated as explicit baselines before additional architectural complexity is credited with an improvement. However, baseline performance is often reported without strict adherence to dataset protocols or adequate uncertainty analysis \cite{demrozi2021bhar}. A particularly important risk is data leakage when normalization statistics are computed over the entire dataset rather than strictly on the training split \cite{chen2024private}.

A critical gap exists in the evaluation of classical machine learning models on the UCI-HAR dataset, often regarded as the "MNIST" of human activity recognition. While early studies established the utility of Support Vector Machines (SVM) and Random Forests \cite{tahir2022stochastic}, recent trends have shifted almost exclusively toward complex Convolutional Neural Networks (CNN) and Recurrent Neural Networks (RNN) \cite{mekruksavanich2023hybrid, khatun2022deep}. This shift has inadvertently obscured the true performance ceiling of linear and ensemble methods. Many contemporary studies report marginal accuracy gains over baselines without accounting for subject-level data leakage, where information from test subjects inadvertently leaks into the training phase during feature normalization or windowing \cite{chen2024private}. Furthermore, the reliance on single-seed evaluations or insufficient statistical testing (e.g., reporting only mean accuracy without confidence intervals) makes it difficult to distinguish genuine architectural improvements from random variance \cite{yuan2024bstcahar}. The statistical power of such studies is often negligible, rendering claims of "significant improvement" scientifically unsound.

To address these issues, we conduct a strict reproducibility audit of two canonical baselines: Linear Stochastic Gradient Descent (SGD) and Random Forest. Unlike studies proposing novel architectures, our contribution lies in the rigorous enforcement of the official UCI-HAR subject split and the application of robust statistical validation. We utilize three independent random seeds to estimate variance, compute 95\% confidence intervals for both Accuracy and Macro-F1, and perform paired statistical tests to assess significance. This approach allows us to isolate the effect of the evaluation protocol from model complexity. Our results demonstrate that while Linear SGD yields a higher mean Macro-F1 than Random Forest, the statistical significance of this difference is ambiguous when assessed with $n=3$, underscoring the need for larger sample sizes in future benchmarks.

The contribution has four connected parts. Fir

... (truncated, see full artifact)


Directory with 4 files: fig_main_comparison.png, fig_metric_breakdown.png, fig_paired_comparison.png, framework_diagram_prompt.md

Directory with 3 files: README.md, main.py, requirements.txt

{
  "schema_version": "reproducibility-v1",
  "generated": "2026-07-20T06:58:06+00:00",
  "python": "3.11.15 (main, Mar 11 2026, 17:20:07) [GCC 14.3.0]",
  "platform": "Linux-6.8.0-124-generic-x86_64-with-glibc2.35",
  "package_versions": {
    "numpy": "1.26.4",
    "scipy": "1.13.1",
    "scikit-learn": "1.9.0",
    "pandas": "3.0.3",
    "torch": "2.5.1+cu124",
    "transformers": "5.14.0",
    "datasets": "5.0.0",
    "matplotlib": "3.10.9",
    "pyyaml": "6.0.3"
  },
  "hardware_profile": {
    "has_gpu": true,
    "gpu_type": "cuda",
    "gpu_name": "NVIDIA GeForce RTX 4090",
    "vram_mb": 24564,
    "tier": "high",
    "warning": ""
  },
  "cuda_visible_devices": "",
  "experiment_command": "",
  "evaluation_protocol": {
    "independent_seeds": [
      11,
      29,
      47
    ],
    "minimum_seeds_per_condition": 3,
    "paired_comparison": {
      "alpha": 0.05,
      "preferred_tests": [
        "paired_t_test",
        "wilcoxon_signed_rank"
      ],
      "required": true
    },
    "raw_result_requirement": "retain per-seed metrics for every condition",
    "report": [
      "mean",
      "standard_deviation",
      "95%_confidence_interval"
    ]
  },
  "datasets_root": "",
  "dataset_files": [],
  "dataset_manifest_truncated": false
}

{
  "schema_version": "claim-integrity-v1",
  "generated": "2026-07-20T06:58:06+00:00",
  "status": "passed",
  "integrity_score": 100,
  "writing_policy": "engineering_report_only",
  "has_limitations_section": true,
  "has_empirical_performance_claims": true,
  "prohibited_empirical_claims": false,
  "supported_metric_value_count": 62,
  "unsupported_numeric_claims": [],
  "overgeneralized_claims": [],
  "baseline_branding_violations": [],
  "experiment_fact_contradictions": [],
  "violations": [],
  "recommended_actions": [],
  "user_facing_status_zh": "结论完整性检查通过。"
}