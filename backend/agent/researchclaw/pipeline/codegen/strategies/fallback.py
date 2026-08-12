"""Fallback code generators.

Produces a minimal runnable experiment when all other strategies fail.  Prefer
small real built-in sklearn datasets for matching lightweight classification
topics; otherwise fall back to a synthetic objective for pipeline plumbing.
"""

from __future__ import annotations

from typing import Any

from researchclaw.config import RCConfig
from researchclaw.pipeline.codegen.session import CodegenSession
from researchclaw.pipeline.codegen.types import (
    CodegenContext,
    CodegenPhase,
    CodegenResult,
    GeneratedFiles,
)


class FallbackStrategy:
    """Last-resort numpy-based experiment generator."""

    @property
    def name(self) -> str:
        return "fallback"

    def can_handle(self, ctx: CodegenContext, config: RCConfig) -> bool:
        return True

    def generate(
        self,
        ctx: CodegenContext,
        config: RCConfig,
        llm: Any,
        session: CodegenSession,
        prompts: Any | None = None,
    ) -> CodegenResult:
        if self._should_use_uci_har(ctx, config):
            session.log(CodegenPhase.FALLBACK, "Using official UCI-HAR real-dataset fallback generator")
            return self._generate_uci_har(ctx)
        if self._should_use_sklearn_builtin(ctx, config):
            session.log(CodegenPhase.FALLBACK, "Using sklearn built-in real-dataset fallback generator")
            return self._generate_sklearn_builtin(ctx)

        session.log(CodegenPhase.FALLBACK, "Using numpy synthetic fallback generator")
        metric = ctx.metric
        files: GeneratedFiles = {
            "main.py": (
                "import numpy as np\n"
                "\n"
                "np.random.seed(42)\n"
                "\n"
                "# Fallback experiment: parameter sweep on a synthetic objective\n"
                "# This runs when LLM code generation fails to produce valid code.\n"
                "dim = 10\n"
                "n_conditions = 3\n"
                "results = {}\n"
                "\n"
                "for cond_idx in range(n_conditions):\n"
                "    cond_name = f'condition_{cond_idx}'\n"
                "    scores = []\n"
                "    for seed in range(3):\n"
                "        rng = np.random.RandomState(seed + cond_idx * 100)\n"
                "        x = rng.randn(dim)\n"
                "        score = float(1.0 / (1.0 + np.sum(x ** 2)))\n"
                "        scores.append(score)\n"
                "    mean_score = float(np.mean(scores))\n"
                "    results[cond_name] = mean_score\n"
                f"    print(f'condition={{cond_name}} {metric}: {{mean_score:.6f}}')\n"
                "\n"
                "best = max(results, key=results.get)\n"
                f"print(f'{metric}: {{results[best]:.6f}}')\n"
            ),
            "experiment_metadata.json": (
                "{\n"
                '  "implementation": "synthetic_fallback",\n'
                '  "experiment_scope": "pipeline_smoke_test",\n'
                '  "scientific_claims_allowed": false,\n'
                '  "note": "Synthetic objective used only to verify that the pipeline can execute code and collect metrics."\n'
                "}\n"
            ),
        }
        return CodegenResult(
            files=files,
            strategy_name=self.name,
            skip_review=True,
        )

    @staticmethod
    def _should_use_sklearn_builtin(ctx: CodegenContext, config: RCConfig) -> bool:
        text = " ".join([
            getattr(getattr(config, "research", None), "topic", "") or "",
            ctx.exp_plan or "",
        ]).lower()
        positive = (
            "sklearn", "scikit-learn", "iris", "wine", "breast cancer",
            "breast_cancer", "tabular", "classification", "logistic regression",
            "random forest",
        )
        negative = (
            "diffusion", "gan", "llm inference", "kv cache", "transformer serving",
            "vision-language", "video", "3d", "object detection", "segmentation",
        )
        return any(p in text for p in positive) and not any(n in text for n in negative)

    @staticmethod
    def _should_use_uci_har(ctx: CodegenContext, config: RCConfig) -> bool:
        text = " ".join([
            getattr(getattr(config, "research", None), "topic", "") or "",
            ctx.exp_plan or "",
        ]).lower()
        return any(term in text for term in (
            "imu", "inertial", "accelerometer", "gyroscope", "uci-har",
            "activity recognition", "惯性", "加速度计", "陀螺仪",
        ))

    @staticmethod
    def _generate_uci_har(ctx: CodegenContext) -> CodegenResult:
        metric = ctx.metric or "primary_metric"
        main_code = f'''"""Real IMU baseline benchmark on the official UCI-HAR dataset.

The dataset is downloaded from UCI, SHA-256 verified, extracted, and evaluated
with the official subject-disjoint train/test split.  No synthetic data or
hard-coded performance values are used.
"""
import hashlib
import json
import urllib.request
import zipfile
from pathlib import Path

import numpy as np
from scipy.stats import ttest_rel
from sklearn.base import clone
from sklearn.ensemble import RandomForestClassifier
from sklearn.linear_model import SGDClassifier
from sklearn.metrics import accuracy_score, f1_score
from sklearn.pipeline import make_pipeline
from sklearn.preprocessing import StandardScaler
from sklearn.model_selection import StratifiedShuffleSplit

URL = "https://archive.ics.uci.edu/static/public/240/human+activity+recognition+using+smartphones.zip"
EXPECTED_SHA256 = "c00b803081a5c797cd5e4b83700a9810b38d53d9d84e01917e090e1fdbc81031"
DATA_ROOT = Path(__file__).resolve().parent / "data"
ZIP_PATH = DATA_ROOT / "uci_har.zip"
EXTRACT_ROOT = DATA_ROOT / "extracted"
OUTPUTS = Path(__file__).resolve().parent / "outputs"


def sha256(path):
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def acquire_dataset():
    DATA_ROOT.mkdir(parents=True, exist_ok=True)
    if not ZIP_PATH.exists() or sha256(ZIP_PATH) != EXPECTED_SHA256:
        temporary = ZIP_PATH.with_suffix(".part")
        temporary.unlink(missing_ok=True)
        print(f"Downloading official UCI-HAR dataset from {{URL}}")
        urllib.request.urlretrieve(URL, temporary)
        actual = sha256(temporary)
        if actual != EXPECTED_SHA256:
            temporary.unlink(missing_ok=True)
            raise RuntimeError(f"UCI-HAR SHA-256 mismatch: {{actual}}")
        temporary.replace(ZIP_PATH)
    marker = EXTRACT_ROOT / ".complete"
    if not marker.exists():
        EXTRACT_ROOT.mkdir(parents=True, exist_ok=True)
        with zipfile.ZipFile(ZIP_PATH) as archive:
            archive.extractall(EXTRACT_ROOT)
        # UCI's current endpoint wraps the historical dataset ZIP inside a
        # repository ZIP.  Extract any nested archives before locating files.
        for nested_zip in sorted(EXTRACT_ROOT.glob("*.zip")):
            with zipfile.ZipFile(nested_zip) as nested_archive:
                nested_archive.extractall(EXTRACT_ROOT)
        marker.write_text(EXPECTED_SHA256, encoding="utf-8")
    candidates = [path for path in EXTRACT_ROOT.rglob("UCI HAR Dataset") if path.is_dir()]
    if not candidates:
        raise RuntimeError("Extracted UCI HAR Dataset directory not found")
    return candidates[0]


def load_split(root, split):
    split_dir = root / split
    X = np.loadtxt(split_dir / f"X_{{split}}.txt", dtype=np.float32)
    y = np.loadtxt(split_dir / f"y_{{split}}.txt", dtype=np.int64) - 1
    subjects = np.loadtxt(split_dir / f"subject_{{split}}.txt", dtype=np.int64)
    return X, y, subjects


root = acquire_dataset()
X_train, y_train, subjects_train = load_split(root, "train")
X_test, y_test, subjects_test = load_split(root, "test")
if set(subjects_train.tolist()) & set(subjects_test.tolist()):
    raise RuntimeError("Official split unexpectedly contains subject leakage")

models = {{
    "linear_logistic_sgd": make_pipeline(
        StandardScaler(), SGDClassifier(loss="log_loss", alpha=0.0001, max_iter=800, tol=1e-3)
    ),
    "random_forest": RandomForestClassifier(
        n_estimators=40, max_depth=24, max_features="sqrt", n_jobs=1
    ),
}}
seeds = [11, 29, 47]
results = {{
    "dataset": {{
        "name": "UCI-HAR", "source_url": URL, "sha256": EXPECTED_SHA256,
        "license": "CC BY 4.0", "train_samples": int(len(y_train)),
        "test_samples": int(len(y_test)), "features": int(X_train.shape[1]),
        "classes": int(len(np.unique(y_train))),
        "train_subjects": int(len(np.unique(subjects_train))),
        "test_subjects": int(len(np.unique(subjects_test))),
        "subject_overlap": 0,
    }},
    "conditions": {{}}, "paired_comparisons": [], "metric_direction": "maximize",
}}

for model_name, template in models.items():
    seed_metrics = {{}}
    for seed in seeds:
        splitter = StratifiedShuffleSplit(n_splits=1, test_size=0.10, random_state=seed)
        fit_indices, _ = next(splitter.split(X_train, y_train))
        model = clone(template)
        random_params = {{key: seed for key in model.get_params() if key.endswith("random_state")}}
        if random_params:
            model.set_params(**random_params)
        model.fit(X_train[fit_indices], y_train[fit_indices])
        prediction = model.predict(X_test)
        seed_metrics[str(seed)] = {{
            "accuracy": float(accuracy_score(y_test, prediction)),
            "f1_macro": float(f1_score(y_test, prediction, average="macro")),
        }}
    accuracy = np.asarray([seed_metrics[str(seed)]["accuracy"] for seed in seeds])
    f1_macro = np.asarray([seed_metrics[str(seed)]["f1_macro"] for seed in seeds])
    results["conditions"][model_name] = {{
        "dataset": "UCI-HAR", "model": model_name, "n_seeds": len(seeds),
        "accuracy_mean": float(accuracy.mean()), "accuracy_std": float(accuracy.std(ddof=1)),
        "f1_macro_mean": float(f1_macro.mean()), "f1_macro_std": float(f1_macro.std(ddof=1)),
        "seed_metrics": seed_metrics,
    }}

left = np.asarray([results["conditions"]["linear_logistic_sgd"]["seed_metrics"][str(seed)]["accuracy"] for seed in seeds])
right = np.asarray([results["conditions"]["random_forest"]["seed_metrics"][str(seed)]["accuracy"] for seed in seeds])
paired = ttest_rel(right, left)
results["paired_comparisons"].append({{
    "metric": "accuracy", "method_a": "random_forest", "method_b": "linear_logistic_sgd",
    "n_pairs": len(seeds), "mean_difference": float((right - left).mean()),
    "p_value": float(paired.pvalue), "test": "paired_t_test",
}})
best_name = max(results["conditions"], key=lambda name: results["conditions"][name]["accuracy_mean"])
results["best_condition"] = best_name
results["primary_metric"] = float(results["conditions"][best_name]["accuracy_mean"])
results["{metric}"] = results["primary_metric"]
results["claim_scope"] = "limited_uci_har_baseline_benchmark"

Path("results.json").write_text(json.dumps(results, indent=2), encoding="utf-8")
OUTPUTS.mkdir(parents=True, exist_ok=True)
(OUTPUTS / "dataset_manifest.json").write_text(json.dumps(results["dataset"], indent=2), encoding="utf-8")
print(json.dumps({{"dataset": "UCI-HAR", "best_condition": best_name, "primary_metric": results["primary_metric"]}}))
print(f"{metric}: {{results['primary_metric']:.6f}}")
'''
        files: GeneratedFiles = {
            "main.py": main_code,
            "requirements.txt": "numpy\nscipy\nscikit-learn\n",
            "experiment_metadata.json": (
                "{\n"
                '  "implementation": "uci_har_real_dataset",\n'
                '  "experiment_scope": "lightweight_real_benchmark",\n'
                '  "scientific_claims_allowed": true,\n'
                '  "claim_status": "limited_small_benchmark",\n'
                '  "datasets": ["UCI-HAR"],\n'
                '  "models": ["linear_logistic_sgd", "random_forest"],\n'
                '  "note": "Official real IMU data with subject-disjoint split, three seeds, two baselines, variance reporting, and a paired test; claims remain limited to this benchmark."\n'
                "}\n"
            ),
        }
        return CodegenResult(
            files=files,
            strategy_name="fallback_uci_har_real_dataset",
            skip_review=True,
        )

    @staticmethod
    def _generate_sklearn_builtin(ctx: CodegenContext) -> CodegenResult:
        metric = ctx.metric or "primary_metric"
        files: GeneratedFiles = {
            "main.py": (
                "import json\n"
                "import numpy as np\n"
                "from sklearn.datasets import load_iris, load_wine, load_breast_cancer\n"
                "from sklearn.ensemble import RandomForestClassifier\n"
                "from sklearn.linear_model import LogisticRegression\n"
                "from sklearn.metrics import accuracy_score, f1_score\n"
                "from sklearn.base import clone\n"
                "from sklearn.model_selection import StratifiedKFold, cross_validate\n"
                "from sklearn.pipeline import make_pipeline\n"
                "from sklearn.preprocessing import StandardScaler\n"
                "from scipy.stats import ttest_rel\n"
                "\n"
                "# Lightweight real experiment: sklearn built-in real datasets.\n"
                "# No synthetic objective and no fabricated metrics are used.\n"
                "datasets = {\n"
                "    'iris': load_iris(),\n"
                "    'wine': load_wine(),\n"
                "    'breast_cancer': load_breast_cancer(),\n"
                "}\n"
                "models = {\n"
                "    'logistic_regression': make_pipeline(\n"
                "        StandardScaler(),\n"
                "        LogisticRegression(max_iter=1000, solver='lbfgs', random_state=42),\n"
                "    ),\n"
                "    'random_forest': RandomForestClassifier(\n"
                "        n_estimators=80, max_depth=None, random_state=42, n_jobs=1,\n"
                "    ),\n"
                "}\n"
                "seeds = [11, 29, 47]\n"
                "scoring = {'accuracy': 'accuracy', 'f1_macro': 'f1_macro'}\n"
                "results = {'datasets': {}, 'conditions': {}, 'paired_comparisons': [], 'metric_direction': 'maximize'}\n"
                "\n"
                "for dataset_name, dataset in datasets.items():\n"
                "    X, y = dataset.data, dataset.target\n"
                "    results['datasets'][dataset_name] = {\n"
                "        'n_samples': int(X.shape[0]),\n"
                "        'n_features': int(X.shape[1]),\n"
                "        'n_classes': int(len(np.unique(y))),\n"
                "    }\n"
                "    for model_name, model in models.items():\n"
                "        condition = f'{dataset_name}__{model_name}'\n"
                "        seed_metrics = {}\n"
                "        seed_acc = []\n"
                "        seed_f1 = []\n"
                "        for seed in seeds:\n"
                "            seeded_model = clone(model)\n"
                "            params = seeded_model.get_params()\n"
                "            random_state_params = {key: seed for key in params if key.endswith('random_state')}\n"
                "            if random_state_params:\n"
                "                seeded_model.set_params(**random_state_params)\n"
                "            cv = StratifiedKFold(n_splits=5, shuffle=True, random_state=seed)\n"
                "            scores = cross_validate(seeded_model, X, y, cv=cv, scoring=scoring, n_jobs=1)\n"
                "            acc_value = float(np.mean(scores['test_accuracy']))\n"
                "            f1_value = float(np.mean(scores['test_f1_macro']))\n"
                "            seed_acc.append(acc_value)\n"
                "            seed_f1.append(f1_value)\n"
                "            seed_metrics[str(seed)] = {'accuracy_mean': acc_value, 'f1_macro_mean': f1_value}\n"
                "        acc = np.asarray(seed_acc, dtype=float)\n"
                "        f1 = np.asarray(seed_f1, dtype=float)\n"
                "        results['conditions'][condition] = {\n"
                "            'dataset': dataset_name,\n"
                "            'model': model_name,\n"
                "            'accuracy_mean': float(np.mean(acc)),\n"
                "            'accuracy_std': float(np.std(acc, ddof=1)),\n"
                "            'f1_macro_mean': float(np.mean(f1)),\n"
                "            'f1_macro_std': float(np.std(f1, ddof=1)),\n"
                "            'n_seeds': int(len(seeds)),\n"
                "            'folds_per_seed': 5,\n"
                "            'seed_metrics': seed_metrics,\n"
                "        }\n"
                "    left = np.asarray([results['conditions'][f'{dataset_name}__logistic_regression']['seed_metrics'][str(seed)]['accuracy_mean'] for seed in seeds])\n"
                "    right = np.asarray([results['conditions'][f'{dataset_name}__random_forest']['seed_metrics'][str(seed)]['accuracy_mean'] for seed in seeds])\n"
                "    test = ttest_rel(right, left)\n"
                "    results['paired_comparisons'].append({\n"
                "        'dataset': dataset_name, 'metric': 'accuracy_mean',\n"
                "        'method_a': 'random_forest', 'method_b': 'logistic_regression',\n"
                "        'n_pairs': int(len(seeds)), 'mean_difference': float(np.mean(right - left)),\n"
                "        'p_value': float(test.pvalue), 'test': 'paired_t_test',\n"
                "    })\n"
                "\n"
                "best_condition = max(\n"
                "    results['conditions'],\n"
                "    key=lambda name: results['conditions'][name]['accuracy_mean'],\n"
                ")\n"
                "best = results['conditions'][best_condition]\n"
                f"results['{metric}'] = float(best['accuracy_mean'])\n"
                "results['primary_metric'] = float(best['accuracy_mean'])\n"
                "results['best_condition'] = best_condition\n"
                "results['best_model'] = best['model']\n"
                "results['best_dataset'] = best['dataset']\n"
                "results['claim_scope'] = 'limited_small_benchmark'\n"
                "\n"
                "with open('results.json', 'w', encoding='utf-8') as f:\n"
                "    json.dump(results, f, indent=2, ensure_ascii=False)\n"
                "\n"
                "print(json.dumps({\n"
                "    'best_condition': best_condition,\n"
                f"    '{metric}': results['{metric}'],\n"
                "    'primary_metric': results['primary_metric'],\n"
                "    'claim_scope': results['claim_scope'],\n"
                "}, ensure_ascii=False))\n"
                f"print(f'{metric}: {{results[\"{metric}\"]:.6f}}')\n"
            ),
            "experiment_metadata.json": (
                "{\n"
                '  "implementation": "sklearn_builtin_real_dataset",\n'
                '  "experiment_scope": "lightweight_real_benchmark",\n'
                '  "scientific_claims_allowed": true,\n'
                '  "claim_status": "limited_small_benchmark",\n'
                '  "datasets": ["iris", "wine", "breast_cancer"],\n'
                '  "models": ["logistic_regression", "random_forest"],\n'
                '  "note": "Real sklearn built-in datasets with three independent seeds, stratified cross-validation, variance reporting, and paired tests; supports only limited small-benchmark claims."\n'
                "}\n"
            ),
        }
        return CodegenResult(
            files=files,
            strategy_name="fallback_sklearn_builtin",
            skip_review=True,
        )
