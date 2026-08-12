"""Run auto-review on thesis datasets."""
import base64, json, sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "backend"))

from review_engine.llm_client import register_config_section, _llm_configs, _llm_clients
from review_engine.pdf_utils import _extract_pdf_text
from review_engine.reviewer import run_review

dataset_key = sys.argv[1]  # "dataset1" or "dataset2"
force = "--force" in sys.argv
resume_stages = "--resume" in sys.argv
# --force keeps its historical meaning (fresh model run). For an interrupted
# run whose final cache was never written, use --resume or simply rerun without
# --force to reuse completed stage checkpoints.
if force and not resume_stages:
    os.environ["AUTO_REVIEW_DISABLE_CHECKPOINT"] = "1"
# Multimodal Qwen v3 all-figures mode is the default; use --text-only only for legacy baselines.
vision = "--text-only" not in sys.argv

datasets = {
    "dataset1": {
        "pdf": "/root/auto_review_dataset/dataset1/物联网应用的智能生成与优化关键技术研究.pdf",
        "short": "dataset1-IoT-Generation",
        "title": "物联网应用的智能生成与优化关键技术研究",
    },
    "dataset2": {
        "pdf": "/root/auto_review_dataset/dataset2/盲审版本.pdf",
        "short": "dataset2-Pipeline-Parallel",
        "title": "面向消费级算力网络的大模型流水线并行推理优化技术研究",
    },
    "dataset3": {
        "pdf": "/root/auto_review_dataset/dataset3/hjm_毕业论文_2026_v3.1.pdf",
        "short": "dataset3-Edge-Computing",
        "title": "面向动态边缘环境的模型弹性计算与协同演进关键技术研究",
    },
}

ds = datasets[dataset_key]
cache_dir = os.path.join(os.path.dirname(__file__), "backend", "eval_cache", "thesis", "auto_reviews")
os.makedirs(cache_dir, exist_ok=True)
debug_fast = any(
    os.environ.get(name, "0") == "1"
    for name in (
        "AUTO_REVIEW_FAST_CONSENSUS",
        "AUTO_REVIEW_SKIP_PATCH",
        "AUTO_REVIEW_SKIP_DEEP_DIVE",
        "AUTO_REVIEW_SKIP_COVERAGE_SWEEP",
    )
)
pipeline_version = "qwen-vision-v7.2-hybrid-agents"
if debug_fast:
    pipeline_version += "-debug-fast"
run_suffix = f"-{pipeline_version}" if vision else ""
cache_file = os.path.join(cache_dir, f"{ds['short']}{run_suffix}.json")

if os.path.exists(cache_file) and not force:
    print(f"Cached result found at {cache_file}, loading...")
    with open(cache_file) as f:
        result = json.load(f)
    print(f"Loaded cached result, score={result['overall_score']}")
    print(json.dumps(result, ensure_ascii=False))
    sys.exit(0)

# Clear cached config & register
for mod in ["Qwen3.5-122B-A10B-FP8"]:
    if mod in _llm_configs: del _llm_configs[mod]
    if mod in _llm_clients: del _llm_clients[mod]

model = register_config_section("review_llm")
print(f"Model: {model}")

# Read the source once. Visual mode lets the review pipeline extract text and
# selected figures together; the legacy text-only path extracts text here.
with open(ds["pdf"], "rb") as f:
    raw = f.read()
text = _extract_pdf_text(raw) if not vision else ""
if vision:
    print(f"Loaded visual PDF ({len(raw)} bytes) from {ds['pdf']}")
else:
    print(f"Extracted {len(text)} chars from {ds['pdf']}")

# Run review
dim_ids = ["methodology", "novelty", "experiment", "writing",
           "related_work", "reproducibility", "ethics", "skeptic",
           "writing_format", "structure_logic", "theory_depth"]

if vision:
    file_base64 = base64.b64encode(raw).decode("ascii")
    file_name = os.path.basename(ds["pdf"])
else:
    file_base64 = base64.b64encode(text.encode("utf-8")).decode("ascii")
    file_name = f"{dataset_key}.txt"
print(f"Run mode: {'Qwen visual PDF' if vision else 'text only'} -> {cache_file}")
print(
    "Stage checkpoint: "
    + ("resume enabled" if resume_stages or not force else "disabled by --force")
)

dim_results, meta, overall_summary = run_review(
    file_base64=file_base64, file_name=file_name,
    dimension_ids=dim_ids, model=model,
    vision_reader=vision, batch=False, hybrid=True, venue="THESIS",
    enable_debate=True, max_debates=2,
)

result = {
    "forum": ds["short"],
    "title": ds["title"],
    "dim_results": dim_results,
    "meta": meta,
    "overall_summary": overall_summary,
    "pipeline_version": pipeline_version,
    "verifiedFindings": meta.get("verifiedFindings", []),
    "consensusMetrics": meta.get("consensusMetrics", {}),
    "categorizedFindings": meta.get("categorizedFindings", []),
    "confidenceSummary": meta.get("confidenceSummary", {}),
    "keyFindings": meta.get("keyFindings", {}),
    "overall_score": sum(r["score"] for r in dim_results) // len(dim_results) if dim_results else 0,
}

with open(cache_file, "w", encoding="utf-8") as f:
    json.dump(result, f, ensure_ascii=False, indent=1)

scores = {r.get("dimensionId", "?"): r.get("score", 0) for r in dim_results}
print(f"\nDone! Overall score: {result['overall_score']}")
print(f"Scores: {scores}")
