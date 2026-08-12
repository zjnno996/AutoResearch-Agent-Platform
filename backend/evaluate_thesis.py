"""Evaluate auto-review coverage against Chinese thesis human reviews.

Pipeline:
  1. Extract full PDF text from local files
  2. Run multimodal auto-review with Qwen on each original PDF
  3. Parse XLS reviewer comments into structured points (with Qwen)
  4. Qwen-as-judge: compare auto vs human coverage
  5. Generate readable comparison report

Usage:
    python backend/evaluate_thesis.py [--dataset1] [--dataset2] [--dataset3] [--skip-review] [--skip-parse] [--force]

Examples:
    python backend/evaluate_thesis.py --dataset1                  # only dataset1
    python backend/evaluate_thesis.py --dataset1 --dataset2       # both
    python backend/evaluate_thesis.py --dataset1 --skip-review    # only parse+compare
    python backend/evaluate_thesis.py --dataset1 --force          # re-run everything
"""

from __future__ import annotations

import base64
import json
import os
import re
import sys
import textwrap
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------

BACKEND_DIR = Path(__file__).resolve().parent
ROOT_DIR = BACKEND_DIR.parent
DATASET_DIR = Path("/root/auto_review_dataset")

# Cache directories (separate from the ICLR eval)
CACHE_DIR = BACKEND_DIR / "eval_cache" / "thesis"
REVIEW_CACHE_DIR = CACHE_DIR / "auto_reviews"
PARSE_CACHE_DIR = CACHE_DIR / "parsed_human"
COVERAGE_CACHE_DIR = CACHE_DIR / "coverage"

for d in [CACHE_DIR, REVIEW_CACHE_DIR, PARSE_CACHE_DIR, COVERAGE_CACHE_DIR]:
    d.mkdir(parents=True, exist_ok=True)

# Ensure Python path
sys.path.insert(0, str(BACKEND_DIR))

# ---------------------------------------------------------------------------
# Dataset definition
# ---------------------------------------------------------------------------

DATASETS: dict[str, dict[str, Any]] = {
    "dataset1": {
        "name": "物联网应用的智能生成与优化关键技术研究",
        "pdf": DATASET_DIR / "dataset1" / "物联网应用的智能生成与优化关键技术研究.pdf",
        "human_reviews_txt": DATASET_DIR / "dataset1" / "human_reviews.txt",
        "xls_dir": DATASET_DIR / "dataset1",
        "xls_pattern": r"12221189_龚凯杰_(\d+)\.xls",
        "type": "phd",
        "short": "dataset1-IoT-Generation",
        "human_review_rows": {
            "evaluation": 135,    # Row for overall evaluation text
            "weaknesses": 168,    # Row for weaknesses & suggestions
        },
    },
    "dataset2": {
        "name": "面向消费级算力网络的大模型流水线并行推理优化技术研究",
        "pdf": DATASET_DIR / "dataset2" / "盲审版本.pdf",
        "xls_dir": DATASET_DIR / "dataset2",
        "xls_pattern": r"22321165_陈永麒_(\d+)\.xls",
        "type": "master",
        "short": "dataset2-Pipeline-Parallel",
        "human_review_rows": {
            "evaluation": 65,     # Row for overall evaluation text
            "weaknesses": 84,     # Row for weaknesses & suggestions
        },
    },
    "dataset3": {
        "name": "面向动态边缘环境的模型弹性计算与协同演进关键技术研究",
        "pdf": DATASET_DIR / "dataset3" / "hjm_毕业论文_2026_v3.1.pdf",
        "human_reviews_txt": DATASET_DIR / "dataset3" / "human_reviews.txt",
        "type": "phd",
        "short": "dataset3-Edge-Computing",
    },
}


# ===========================================================================
# Stage 1: Extract full PDF text
# ===========================================================================

def extract_pdf_text(pdf_path: Path) -> str:
    """Extract full text from a local PDF file."""
    with open(pdf_path, "rb") as f:
        raw = f.read()

    # Use the project's PDF extraction utilities
    from review_engine.pdf_utils import _extract_pdf_text
    text = _extract_pdf_text(raw)

    if not text.strip():
        print(f"  [WARNING] PDF text extraction returned empty for {pdf_path.name}")
    else:
        print(f"  Extracted {len(text)} chars, {len(text.split())} words")

    return text


# ===========================================================================
# Stage 2: Read XLS human reviews
# ===========================================================================

def _read_structured_text_reviews(dataset_key: str, path: Path) -> dict[str, Any]:
    """Read REVIEWER / [TYPE] lines already structured in a dataset directory."""
    ds = DATASETS[dataset_key]
    reviews: list[dict[str, Any]] = []
    current: dict[str, Any] | None = None
    for raw_line in path.read_text(encoding="utf-8", errors="ignore").splitlines():
        line = raw_line.strip()
        reviewer_match = re.match(r"REVIEWER\s+(\d+)", line, re.IGNORECASE)
        if reviewer_match:
            if current:
                reviews.append(current)
            current = {
                "reviewer_num": int(reviewer_match.group(1)),
                "structured_points": [],
            }
            continue
        point_match = re.match(
            r"\[(STRENGTH|WEAKNESS|SUGGESTION)\]\s*(.+)",
            line,
            re.IGNORECASE,
        )
        if current is not None and point_match:
            current["structured_points"].append({
                "type": point_match.group(1).lower(),
                "text": point_match.group(2).strip(),
            })
    if current:
        reviews.append(current)

    for review in reviews:
        points = review["structured_points"]
        review["evaluation"] = "；".join(
            point["text"] for point in points if point["type"] == "strength"
        )
        review["weaknesses"] = "；".join(
            point["text"] for point in points if point["type"] != "strength"
        )
        review["combined_text"] = "\n".join(
            f"[{point['type'].upper()}] {point['text']}" for point in points
        )
    reviews.sort(key=lambda review: review["reviewer_num"])
    return {"name": ds["name"], "dataset_key": dataset_key, "reviews": reviews}


def read_xls_reviews(dataset_key: str) -> dict[str, Any]:
    """Read XLS reviews or a structured human_reviews.txt file."""
    ds = DATASETS[dataset_key]
    text_path = ds.get("human_reviews_txt")
    if text_path:
        return _read_structured_text_reviews(dataset_key, Path(text_path))
    xls_dir = ds["xls_dir"]
    pattern = ds["xls_pattern"]
    rows = ds["human_review_rows"]

    try:
        import pandas as pd
    except ImportError:
        print("pandas required for XLS reading. Install with: pip install pandas xlrd openpyxl")
        return {"name": ds["name"], "reviews": []}

    reviews = []
    for fname in sorted(os.listdir(xls_dir)):
        m = re.match(pattern, fname)
        if not m:
            continue
        reviewer_num = int(m.group(1))

        filepath = xls_dir / fname
        try:
            df = pd.read_excel(filepath, header=None)
        except Exception as e:
            print(f"  [ERROR] Cannot read {fname}: {e}")
            continue

        # Extract evaluation text
        eval_text = ""
        eval_row = rows["evaluation"]
        if df.shape[0] > eval_row and pd.notna(df.iloc[eval_row, 0]):
            eval_text = str(df.iloc[eval_row, 0]).strip()
            # Remove the header prefix
            prefix = "评阅人对学位论文的评价意见"
            if prefix in eval_text:
                eval_text = eval_text.split(prefix, 1)[-1].strip()
            if eval_text.startswith("(请根据"):
                eval_text = ""
            # Also check next rows for actual content
            for offset in range(1, 5):
                if df.shape[0] > eval_row + offset and pd.notna(df.iloc[eval_row + offset, 0]):
                    next_text = str(df.iloc[eval_row + offset, 0]).strip()
                    if next_text and "提供" not in next_text[:20]:
                        eval_text = next_text
                        break

        # Extract weaknesses & suggestions
        weak_text = ""
        weak_row = rows["weaknesses"]
        if df.shape[0] > weak_row and pd.notna(df.iloc[weak_row, 0]):
            weak_text = str(df.iloc[weak_row, 0]).strip()
        elif df.shape[0] > weak_row - 1 and pd.notna(df.iloc[weak_row - 1, 0]):
            weak_text = str(df.iloc[weak_row - 1, 0]).strip()

        reviews.append({
            "reviewer_num": reviewer_num,
            "evaluation": eval_text,
            "weaknesses": weak_text,
            "combined_text": f"【评价】\n{eval_text}\n\n【不足之处与修改建议】\n{weak_text}" if eval_text or weak_text else "",
        })

    # Sort by reviewer number
    reviews.sort(key=lambda r: r["reviewer_num"])

    return {
        "name": ds["name"],
        "dataset_key": dataset_key,
        "reviews": reviews,
    }


# ===========================================================================
# Stage 3: Run auto-review
# ===========================================================================

PIPELINE_VERSION = "qwen-vision-v8-claim-evidence"


def run_auto_review(
    dataset_key: str,
    paper_text: str,
    force: bool = False,
    model: str | None = None,
) -> dict[str, Any] | None:
    """Run Qwen-only multimodal auto-review on the original thesis PDF."""
    ds = DATASETS[dataset_key]
    short = ds["short"]
    cache_file = REVIEW_CACHE_DIR / f"{short}-{PIPELINE_VERSION}.json"

    if cache_file.exists() and not force:
        print(f"  [review] {short} (cached)")
        with open(cache_file, encoding="utf-8") as f:
            return json.load(f)

    print(f"  [review] {short}...", end=" ", flush=True)

    # Use the same Qwen-only policy as the web AutoReview service.
    from review_engine.llm_client import get_preferred_review_model_name, register_config_section
    register_config_section("llm")
    model = get_preferred_review_model_name()

    # Run review directly from the original PDF so figures/tables are available.
    from review_engine.reviewer import run_review
    pdf_bytes = ds["pdf"].read_bytes()
    file_base64 = base64.b64encode(pdf_bytes).decode("ascii")
    file_name = ds["pdf"].name

    dim_ids = [
        "methodology", "novelty", "experiment", "writing",
        "related_work", "reproducibility", "ethics",
        "skeptic",
        "writing_format", "structure_logic", "theory_depth",
    ]

    try:
        dim_results, meta, overall_summary = run_review(
            file_base64=file_base64,
            file_name=file_name,
            dimension_ids=dim_ids,
            model=model,
            vision_reader=True,
            batch=False,
            hybrid=True,
            venue="THESIS",
            enable_debate=True,
            max_debates=2,
        )

        result = {
            "forum": short,
            "title": ds["name"],
            "dim_results": dim_results,
            "meta": meta,
            "overall_summary": overall_summary,
            "verifiedFindings": meta.get("verifiedFindings", []),
            "consensusMetrics": meta.get("consensusMetrics", {}),
            "categorizedFindings": meta.get("categorizedFindings", []),
            "confidenceSummary": meta.get("confidenceSummary", {}),
            "keyFindings": meta.get("keyFindings", {}),
            "pipeline_version": PIPELINE_VERSION,
            "overall_score": (
                sum(r["score"] for r in dim_results) // len(dim_results)
                if dim_results else 0
            ),
        }

        with open(cache_file, "w", encoding="utf-8") as f:
            json.dump(result, f, ensure_ascii=False, indent=1)

        print(f"done (score={result['overall_score']})")
        return result

    except Exception as e:
        print(f"FAILED: {e}")
        import traceback
        traceback.print_exc()
        error_result = {
            "forum": short,
            "title": ds["name"],
            "error": str(e),
        }
        with open(cache_file, "w", encoding="utf-8") as f:
            json.dump(error_result, f, ensure_ascii=False, indent=1)
        return None


# ===========================================================================
# Stage 4: Parse human reviews into structured points
# ===========================================================================

HUMAN_PARSE_PROMPT = """你正在分析一篇中文博士/硕士学位论文的专家评阅意见。请从评阅意见中提取出具体的评审要点。

每个要点应是一个具体的观察或评价。

将每个要点分类为：
- "strength": 正面评价，论文做得好的地方
- "weakness": 批评或指出的不足
- "suggestion": 具体的改进建议

规则：
- 每个要点要具体，把不同观点分开提取
- 保持原文含义，不要改写丢失细节
- 如果提到具体章节、图表、公式，保留引用
- 跳过泛泛的客套话
- 输出 JSON 对象，包含 "points" 数组

输出格式：
{"points": [
  {"type": "strength", "text": "..."},
  {"type": "weakness", "text": "..."},
  {"type": "suggestion", "text": "..."}
]}"""


def parse_human_reviews(
    human_data: dict[str, Any],
    force: bool = False,
    model: str | None = None,
) -> dict[str, Any]:
    """Parse human review text into structured points using LLM."""
    dataset_key = human_data["dataset_key"]
    ds = DATASETS[dataset_key]
    short = ds["short"]
    cache_file = PARSE_CACHE_DIR / f"{short}.json"

    if cache_file.exists() and not force:
        print(f"  [parse] {short} (cached)")
        with open(cache_file, encoding="utf-8") as f:
            return json.load(f)

    print(f"  [parse] {short}...", flush=True)

    from review_engine.llm_client import register_config_section
    if model is None:
        model = register_config_section("llm")

    def _llm_json(prompt: str, system: str = "", max_tokens: int = 4096) -> dict | list:
        from review_engine.llm_client import get_client_for_model, register_config_section as reg
        if model:
            reg("llm")
        client = get_client_for_model(model)
        resp = client.chat(
            messages=[{"role": "user", "content": prompt}],
            system=system,
            max_tokens=max_tokens,
            temperature=0.1,
            json_mode=True,
        )
        raw = resp.content.strip()
        if raw.startswith("```"):
            raw = raw.split("\n", 1)[-1]
            raw = raw.rsplit("```", 1)[0]
        return json.loads(raw)

    parsed_reviews = []

    for rv in human_data.get("reviews", []):
        if rv.get("structured_points") is not None:
            clean_points = [
                {
                    "type": str(point.get("type", "weakness")),
                    "text": str(point.get("text", ""))[:500],
                }
                for point in rv["structured_points"]
                if str(point.get("text", "")).strip()
            ]
            parsed_reviews.append({
                "reviewer_num": rv["reviewer_num"],
                "points": clean_points,
                "point_count": len(clean_points),
            })
            print(f"    Reviewer {rv['reviewer_num']}: {len(clean_points)} structured points loaded")
            continue
        review_text = rv.get("combined_text", "").strip()
        if not review_text:
            continue

        try:
            points = _llm_json(
                prompt=f"从以下博士/硕士学位论文评阅意见中提取评审要点：\n\n{review_text[:6000]}",
                system=HUMAN_PARSE_PROMPT,
            )
            # Normalize
            if isinstance(points, dict) and not points.get("type"):
                for val in points.values():
                    if isinstance(val, list):
                        points = val
                        break
            if isinstance(points, dict):
                points = [points]
            if not isinstance(points, list):
                points = []
        except Exception as e:
            print(f"    Reviewer {rv['reviewer_num']}: parse error: {e}")
            points = []

        clean_points = []
        for p in points:
            if isinstance(p, dict) and "text" in p:
                clean_points.append({
                    "type": p.get("type", "weakness"),
                    "text": p["text"][:500],
                })
        parsed_reviews.append({
            "reviewer_num": rv["reviewer_num"],
            "points": clean_points,
            "point_count": len(clean_points),
        })
        print(f"    Reviewer {rv['reviewer_num']}: {len(clean_points)} points extracted")

    parsed_data = {
        "forum": short,
        "title": ds["name"],
        "reviews": parsed_reviews,
        "total_points": sum(r["point_count"] for r in parsed_reviews),
    }

    with open(cache_file, "w", encoding="utf-8") as f:
        json.dump(parsed_data, f, ensure_ascii=False, indent=1)

    print(f"    Total: {parsed_data['total_points']} points across {len(parsed_reviews)} reviews")
    return parsed_data


# ===========================================================================
# Stage 5: Coverage comparison (LLM-as-judge)
# ===========================================================================

COVERAGE_PROMPT = """You are evaluating how well an automated review system covers specific points raised by human expert reviewers for a Chinese thesis.

HUMAN POINTS (from expert reviewers):
{human_points}

AUTO-REVIEW POINTS (generated by automated system):
{auto_points}

For each HUMAN point, determine if ANY auto-review point makes the same evidence-bearing claim.
Set covered=true ONLY when all conditions hold:
1. The point type/polarity is compatible (strength cannot match criticism; absence cannot match presence).
2. The target method, experiment, section, figure, or writing issue is the same.
3. The substantive claim and scope are entailed, not merely topically related.
4. The auto point does not contradict the human point.
Use covered=false for generic overlap, partial topic overlap, opposite conclusions, or uncertain matches.

Output ONLY valid JSON:
{{"matches": [{{"human_idx": 0, "auto_idx": 3, "covered": true}}, {{"human_idx": 1, "auto_idx": null, "covered": false}}]}}"""


def _get_llm_client(model: str | None = None):
    """Get the LLM client for coverage comparison."""
    from review_engine.llm_client import (
        get_client_for_model, get_preferred_review_model_name, register_config_section,
    )
    register_config_section("llm")
    return get_client_for_model(get_preferred_review_model_name())


def _llm_json(prompt: str, system: str = "", max_tokens: int = 4096,
              model: str | None = None) -> dict | list:
    """Call LLM and parse JSON response."""
    client = _get_llm_client(model)
    resp = client.chat(
        messages=[{"role": "user", "content": prompt}],
        system=system,
        max_tokens=max_tokens,
        temperature=0.1,
        json_mode=True,
    )
    raw = resp.content.strip()
    if raw.startswith("```"):
        raw = raw.split("\n", 1)[-1]
        raw = raw.rsplit("```", 1)[0]
    return json.loads(raw)


def _flatten_auto_points(auto_result: dict[str, Any]) -> list[dict[str, Any]]:
    """Return only evidence-anchored strengths and confidence-filtered findings."""
    from review_engine.alignment import flatten_confident_auto_points
    return flatten_confident_auto_points(auto_result, min_confidence=0.55)


def _run_alignment_recall_audit(
    human_points: list[dict[str, Any]],
    auto_points: list[dict[str, Any]],
    matches: list[dict[str, Any]],
    batch_size: int = 8,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Strictly recheck first-pass misses using a smaller same-type candidate set."""
    from review_engine.alignment import (
        build_recall_audit_prompt,
        select_auto_candidate_indices,
        validate_recall_audit_match,
    )

    initially_covered = {
        int(match["human_index"])
        for match in matches
        if match.get("covered") and isinstance(match.get("human_index"), int)
    }
    target_indices = [
        index for index, point in enumerate(human_points)
        if index not in initially_covered
        and point.get("type") in {"weakness", "suggestion"}
    ]
    metrics = {
        "enabled": True,
        "targets": len(target_indices),
        "batches": 0,
        "recovered": 0,
        "rejected": 0,
    }
    recovered_human: set[int] = set()
    for batch_start in range(0, len(target_indices), batch_size):
        global_indices = target_indices[batch_start:batch_start + batch_size]
        chunk = [human_points[index] for index in global_indices]
        candidate_indices = select_auto_candidate_indices(chunk, auto_points, limit=32)
        if not candidate_indices:
            continue
        metrics["batches"] += 1
        try:
            result = _llm_json(
                build_recall_audit_prompt(chunk, auto_points, candidate_indices),
                max_tokens=4096,
            )
        except Exception as exc:
            metrics.setdefault("errors", []).append(str(exc))
            continue
        proposed = result if isinstance(result, list) else result.get("matches", [])
        for item in proposed:
            if not isinstance(item, dict):
                continue
            try:
                local_index = int(item.get("human_idx", -1))
            except (TypeError, ValueError):
                metrics["rejected"] += 1
                continue
            auto_index = item.get("auto_idx")
            if (
                not 0 <= local_index < len(chunk)
                or not isinstance(auto_index, int)
                or not 0 <= auto_index < len(auto_points)
            ):
                metrics["rejected"] += 1
                continue
            global_index = global_indices[local_index]
            if global_index in recovered_human:
                continue
            accepted, gate_reason = validate_recall_audit_match(
                chunk[local_index], auto_points[auto_index], item,
            )
            if not accepted:
                metrics["rejected"] += 1
                continue
            recovered_human.add(global_index)
            matches.append({
                "human_index": global_index,
                "auto_index": auto_index,
                "covered": True,
                "notes": (
                    "recall_audit: "
                    + str(item.get("reason", gate_reason))
                ),
                "judge_confidence": float(item.get("confidence", 0.0)),
                "match_stage": "recall_audit",
            })
    metrics["recovered"] = len(recovered_human)
    return matches, metrics


def compute_coverage(
    parsed_human: dict[str, Any],
    auto_result: dict[str, Any],
    force: bool = False,
    chunk_size: int = 5,
) -> dict[str, Any]:
    """Compare human vs auto points."""
    short = parsed_human.get("forum", "unknown")
    pipeline_version = auto_result.get("pipeline_version", "legacy")
    cache_file = COVERAGE_CACHE_DIR / f"{short}-{pipeline_version}.json"

    if cache_file.exists() and not force:
        print(f"  [coverage] {short} (cached)")
        with open(cache_file, encoding="utf-8") as f:
            return json.load(f)

    # Gather human points
    all_human: list[dict[str, Any]] = []
    for rv in parsed_human.get("reviews", []):
        for p in rv.get("points", []):
            all_human.append({
                "reviewer_num": rv["reviewer_num"],
                "type": p["type"],
                "text": p["text"],
            })

    if not all_human:
        print(f"  [coverage] {short}: no human points to compare")
        return {"forum": short, "total_human": 0, "coverage_pct": 0, "matches": []}

    auto_points = _flatten_auto_points(auto_result)
    if not auto_points:
        print(f"  [coverage] {short}: no auto points to compare")
        return {"forum": short, "total_human": len(all_human), "coverage_pct": 0, "matches": []}

    # Process in chunks
    all_matches: list[dict[str, Any]] = []
    judge_batches_total = (len(all_human) + chunk_size - 1) // chunk_size
    judge_batches_succeeded = 0
    judge_errors: list[str] = []

    for chunk_start in range(0, len(all_human), chunk_size):
        chunk = all_human[chunk_start:chunk_start + chunk_size]
        human_fmt = "\n".join(
            f"[{j}] ({p['type']}) {p['text'][:300]}"
            for j, p in enumerate(chunk)
        )
        from review_engine.alignment import select_auto_candidate_indices
        candidate_indices = select_auto_candidate_indices(chunk, auto_points, limit=36)
        auto_fmt = "\n".join(
            f"[{index}] ({auto_points[index]['type']}) "
            f"[{auto_points[index]['dimension']}] {auto_points[index]['text'][:260]}"
            for index in candidate_indices
        )

        prompt = COVERAGE_PROMPT.format(human_points=human_fmt, auto_points=auto_fmt)

        # Truncate if too long
        if len(prompt) > 15000:
            ratio = 15000 / len(prompt)
            max_human = int(len(human_fmt) * ratio * 1.5)
            max_auto = int(len(auto_fmt) * ratio)
            human_fmt = human_fmt[:max_human] + "\n[...]"
            auto_fmt = auto_fmt[:max_auto] + "\n[...]"
            prompt = COVERAGE_PROMPT.format(human_points=human_fmt, auto_points=auto_fmt)

        try:
            result = _llm_json(prompt, max_tokens=4096)
        except Exception as e:
            print(f"    chunk {chunk_start}: LLM error: {e}")
            judge_errors.append(f"chunk {chunk_start}: {e}")
            continue
        judge_batches_succeeded += 1

        matches = result if isinstance(result, list) else result.get("matches", [])
        for m in matches:
            if isinstance(m, dict):
                local_human_index = int(m.get("human_idx", 0))
                auto_index = m.get("auto_idx")
                type_compatible = (
                    0 <= local_human_index < len(chunk)
                    and isinstance(auto_index, int)
                    and 0 <= auto_index < len(auto_points)
                    and chunk[local_human_index].get("type") == auto_points[auto_index].get("type")
                )
                covered = bool(m.get("covered", False)) and type_compatible
                all_matches.append({
                    "human_index": chunk_start + local_human_index,
                    "auto_index": auto_index,
                    "covered": covered,
                    "notes": (m.get("notes", "") if type_compatible else "rejected: point type/polarity mismatch"),
                })

        # Small delay between chunks
        if chunk_start + chunk_size < len(all_human):
            time.sleep(0.5)

    # Infrastructure failure is not 0% semantic coverage. Partial judging also
    # biases the denominator, so do not write a score or overwrite a valid cache
    # unless every human-point batch was evaluated successfully.
    if judge_batches_succeeded != judge_batches_total:
        raise RuntimeError(
            "Coverage judge incomplete: "
            f"{judge_batches_succeeded}/{judge_batches_total} batches succeeded. "
            + (judge_errors[0] if judge_errors else "unknown judge error")
        )

    recall_audit = {"enabled": False, "targets": 0, "batches": 0, "recovered": 0}
    if os.environ.get("AUTO_REVIEW_ALIGNMENT_RECALL_AUDIT", "1") != "0":
        all_matches, recall_audit = _run_alignment_recall_audit(
            all_human, auto_points, all_matches,
        )

    covered_human_indices = {
        m["human_index"] for m in all_matches
        if m.get("covered") and isinstance(m.get("human_index"), int)
    }
    covered_count = len(covered_human_indices)

    from review_engine.alignment import build_usefulness_prompt, classify_alignment
    matched_auto_indices = {
        int(m["auto_index"]) for m in all_matches
        if m.get("covered") and isinstance(m.get("auto_index"), int)
    }
    unmatched_indices = [
        index for index in range(len(auto_points)) if index not in matched_auto_indices
    ]
    usefulness_judgments: list[dict[str, Any]] = []
    if unmatched_indices:
        try:
            usefulness_result = _llm_json(
                build_usefulness_prompt(all_human, auto_points, unmatched_indices),
                max_tokens=4096,
            )
            usefulness_judgments = (
                usefulness_result if isinstance(usefulness_result, list)
                else usefulness_result.get("judgments", [])
            )
        except Exception as exc:
            print(f"  [usefulness] judge failed, using deterministic fallback: {exc}")

    alignment = classify_alignment(
        all_human, auto_points, all_matches,
        usefulness_judgments=usefulness_judgments,
    )
    coverage_data = {
        "forum": short,
        "title": parsed_human.get("title", ""),
        "total_human_points": len(all_human),
        "total_auto_points": len(auto_points),
        "covered_count": covered_count,
        "coverage_pct": round(covered_count / max(len(all_human), 1) * 100, 1),
        "matches": [{
            "human_index": m["human_index"],
            "auto_index": m["auto_index"],
            "covered": m["covered"],
            **({"match_stage": m["match_stage"]} if m.get("match_stage") else {}),
            **({"judge_confidence": m["judge_confidence"]} if m.get("judge_confidence") is not None else {}),
        } for m in all_matches],
        "human_points": all_human,
        "auto_points": auto_points,
        "recall_audit": recall_audit,
        "judge_batches_total": judge_batches_total,
        "judge_batches_succeeded": judge_batches_succeeded,
        **alignment,
    }

    print(f"  [coverage] {short}: {covered_count}/{len(all_human)} covered ({coverage_data['coverage_pct']}%)")
    print(f"    Auto points: {len(auto_points)}, Human points: {len(all_human)}")

    with open(cache_file, "w", encoding="utf-8") as f:
        json.dump(coverage_data, f, ensure_ascii=False, indent=1)

    return coverage_data


# ===========================================================================
# Stage 6: Generate readable output
# ===========================================================================

DIMENSION_LABELS: dict[str, str] = {
    "methodology": "研究方法",
    "novelty": "创新性与贡献",
    "experiment": "实验与结果",
    "writing": "写作表达",
    "related_work": "相关工作",
    "reproducibility": "可复现性",
    "ethics": "伦理与风险",
    "skeptic": "质疑与交叉核验",
    "deep_dive": "技术深度审查",
    "patch": "补充审查",
    "writing_format": "写作格式",
    "structure_logic": "结构与逻辑",
    "theory_depth": "理论深度",
}
POINT_TYPE_LABELS = {"strength": "优点", "weakness": "不足", "suggestion": "建议"}


def _point_type_label(value: object) -> str:
    return POINT_TYPE_LABELS.get(str(value), str(value))


def format_comparison(coverage_data: dict[str, Any], human_data: dict[str, Any],
                       auto_result: dict[str, Any]) -> str:
    """Format a readable comparison report."""
    lines: list[str] = []
    title = coverage_data.get("title", human_data.get("name", ""))
    ds_key = human_data.get("dataset_key", "")
    ds_type = DATASETS.get(ds_key, {}).get("type", "")

    lines.append("=" * 80)
    lines.append(f"  {title}")
    lines.append(f"  数据集：{ds_key}（{ds_type.upper()} 学位论文）")
    lines.append(f"  专家意见覆盖：{coverage_data['covered_count']}/{coverage_data['total_human_points']} "
                 f"({coverage_data['coverage_pct']}%)")
    lines.append(f"  置信度过滤后的自动观点：{coverage_data['total_auto_points']}")
    counts = coverage_data.get("counts", {})
    lines.append(
        f"  已对上：{counts.get('matched', 0)} | "
        f"未对上但有用：{counts.get('useful_unmatched', 0)} | "
        f"未对上且无效：{counts.get('unhelpful_unmatched', 0)} | "
        f"专家意见漏检：{counts.get('missed_human', 0)}"
    )
    recall_audit = coverage_data.get("recall_audit", {})
    if recall_audit.get("enabled"):
        lines.append(
            f"  严格二次复核：检查 {recall_audit.get('targets', 0)} 条首轮漏项，"
            f"恢复 {recall_audit.get('recovered', 0)} 条，"
            f"拒绝 {recall_audit.get('rejected', 0)} 条候选"
        )
    lines.append("=" * 80)

    auto_points_list = coverage_data.get("auto_points", [])
    human_points_list = coverage_data.get("human_points", [])
    matches = coverage_data.get("matches", [])
    matched_set = set()
    for m in matches:
        if m.get("covered") and m.get("auto_index") is not None:
            matched_set.add(m["auto_index"])

    # Group human points by reviewer
    reviewer_points: dict[int, list[dict]] = {}
    for hp in human_points_list:
        rn = hp.get("reviewer_num", 1)
        reviewer_points.setdefault(rn, []).append(hp)

    for reviewer_num in sorted(reviewer_points.keys()):
        # Find human review recommendation
        rev_info = ""
        for rv in human_data.get("reviews", []):
            if rv["reviewer_num"] == reviewer_num:
                rev_info = rv.get("evaluation", "")[:80]
                break

        lines.append("")
        lines.append("─" * 72)
        lines.append(f"  专家 {reviewer_num}" + ("  |  " + rev_info if rev_info else ""))
        lines.append("─" * 72)

        points = reviewer_points[reviewer_num]
        for hp in points:
            # Find the human index
            h_idx = human_points_list.index(hp)
            match = next((m for m in matches if m["human_index"] == h_idx), None)
            is_covered = match.get("covered", False) if match else False
            icon = "✓" if is_covered else "✗"
            tag = _point_type_label(hp.get("type", "?"))

            lines.append(f"\n  [{icon}] {tag}:  {hp['text']}")

            if is_covered and match and match.get("auto_index") is not None:
                auto_idx = match["auto_index"]
                if auto_idx < len(auto_points_list):
                    ap = auto_points_list[auto_idx]
                    dim_label = DIMENSION_LABELS.get(ap.get("dimension", ""), ap.get("dimension", ""))
                    lines.append(
                        f"       └ auto [{dim_label}/{_point_type_label(ap.get('type', '?'))}]: "
                        f"{ap.get('text', '')}"
                    )
            elif not is_covered:
                lines.append("       └ 未找到自动匹配")

    # Gap analysis: Human有 / Auto无
    human_missed = []
    for hp in human_points_list:
        h_idx = human_points_list.index(hp)
        match = next((m for m in matches if m["human_index"] == h_idx), None)
        if not match or not match.get("covered"):
            human_missed.append(hp)

    if human_missed:
        lines.append("")
        lines.append("─" * 72)
        lines.append(f"  【覆盖缺口】Human提到但Auto遗漏的要点 ({len(human_missed)}个)")
        lines.append("─" * 72)
        for hp in human_missed[:8]:
            lines.append(
                f"  ✗ [{_point_type_label(hp.get('type', '?'))}] {hp.get('text','')[:100]}"
            )
        if len(human_missed) > 8:
            lines.append(f"  ... 还有{len(human_missed) - 8}个遗漏要点")

    useful_unmatched = coverage_data.get("useful_unmatched_auto_points", [])
    if useful_unmatched:
        lines.append("")
        lines.append("─" * 72)
        lines.append(f"  【未对上但有用】系统新增的有效发现 ({len(useful_unmatched)}个)")
        lines.append("─" * 72)
        for point in useful_unmatched:
            dim_label = DIMENSION_LABELS.get(point.get("dimension", ""), point.get("dimension", ""))
            confidence = float(point.get("confidence", 0.0))
            lines.append(
                f"  + [{dim_label}/{_point_type_label(point.get('type', '?'))} | {confidence:.0%}] "
                f"{point.get('text', '')}"
            )
            if point.get("usefulness_reason"):
                lines.append(f"       └ 有用原因: {point['usefulness_reason']}")

    unhelpful_unmatched = coverage_data.get("unhelpful_unmatched_auto_points", [])
    if unhelpful_unmatched:
        lines.append("")
        lines.append("─" * 72)
        lines.append(f"  【未对上且无效】应排除或继续改进 ({len(unhelpful_unmatched)}个)")
        lines.append("─" * 72)
        for point in unhelpful_unmatched:
            lines.append(
                f"  - [{DIMENSION_LABELS.get(point.get('dimension', '?'), point.get('dimension', '?'))}/"
                f"{_point_type_label(point.get('type', '?'))}] "
                f"{point.get('text', '')}"
            )
            if point.get("usefulness_reason"):
                lines.append(f"       └ 排除原因: {point['usefulness_reason']}")

    lines.append("")
    return "\n".join(lines)


def format_summary(coverage_list: list[dict[str, Any]], human_data_list: list[dict[str, Any]]) -> str:
    """Aggregate summary across datasets."""
    lines: list[str] = []
    lines.append("=" * 80)
    lines.append("  学位论文自动评审覆盖评测")
    lines.append(f"  论文数：{len(coverage_list)}")
    lines.append("=" * 80)

    total_human = 0
    total_covered = 0
    total_auto = 0

    for i, cov in enumerate(coverage_list):
        total_human += cov.get("total_human_points", 0)
        total_covered += cov.get("covered_count", 0)
        total_auto += cov.get("total_auto_points", 0)

        title = cov.get("title", f"Paper {i+1}")
        ds_key = human_data_list[i].get("dataset_key", "") if i < len(human_data_list) else ""

        # Count by type
        h_str = sum(1 for hp in cov.get("human_points", []) if hp.get("type") == "strength")
        h_wk = sum(1 for hp in cov.get("human_points", []) if hp.get("type") == "weakness")
        h_sg = sum(1 for hp in cov.get("human_points", []) if hp.get("type") == "suggestion")
        a_str = sum(1 for ap in cov.get("auto_points", []) if ap.get("type") == "strength")
        a_wk = sum(1 for ap in cov.get("auto_points", []) if ap.get("type") == "weakness")
        a_sg = sum(1 for ap in cov.get("auto_points", []) if ap.get("type") == "suggestion")

        counts = cov.get("counts", {})
        missed_count = int(counts.get("missed_human", cov.get("not_covered_count", 0)))
        useful_count = int(counts.get("useful_unmatched", 0))
        unhelpful_count = int(counts.get("unhelpful_unmatched", 0))
        matched_count = int(counts.get("matched", 0))

        lines.append(f"\n  [{ds_key}] {title[:56]}")
        lines.append(f"    Coverage: {cov['covered_count']}/{cov['total_human_points']} ({cov['coverage_pct']}%)  "
                     f"| Human漏: {missed_count}")
        lines.append(
            f"    Auto对上: {matched_count} | 未对上但有用: {useful_count} | "
            f"未对上且无效: {unhelpful_count}"
        )
        lines.append(f"    Human: {cov['total_human_points']} (S:{h_str} W:{h_wk} Sg:{h_sg})  "
                     f"Auto: {cov['total_auto_points']} (S:{a_str} W:{a_wk} Sg:{a_sg})")

    if total_human > 0:
        overall_pct = round(total_covered / total_human * 100, 1)
        lines.append(f"\n  {'─' * 60}")
        lines.append(f"  OVERALL: {total_covered}/{total_human} ({overall_pct}%)")
        lines.append(f"  Total auto points: {total_auto}")

    lines.append("")
    return "\n".join(lines)


# ===========================================================================
# Main pipeline
# ===========================================================================

def format_expert_reviews(reviewers: list[dict], title: str) -> str:
    """Format multi-expert review output like real thesis blind review templates."""
    lines = []
    lines.append("=" * 80)
    lines.append("  博士学位论文盲审评阅意见")
    lines.append(f"  论文题目：{title}")
    lines.append("=" * 80)

    for i, rev in enumerate(reviewers):
        lines.append("")
        lines.append("─" * 72)
        lines.append(f"  评阅专家 {i+1}：{rev.get('expertise', '')}")
        lines.append("─" * 72)

        overall = rev.get("overallEvaluation", "")
        if overall:
            wrapped = textwrap.fill(overall.strip(), width=72, initial_indent="", subsequent_indent="")
            lines.append(f"\n{wrapped}")

        issues = rev.get("keyIssues", [])
        advice = rev.get("improvementAdvice", [])
        if issues:
            for j, iss in enumerate(issues, 1):
                adv = advice[j-1] if j-1 < len(advice) else ""
                if adv:
                    lines.append(f"\n  ● {iss}")
                    wrapped_adv = textwrap.fill(adv.strip(), width=70, initial_indent="    建议：", subsequent_indent="          ")
                    lines.append(wrapped_adv)
                else:
                    lines.append(f"\n  ● {iss}")

        verdict = rev.get("overallVerdict", "")
        rec = rev.get("recommendation", "")
        if verdict or rec:
            parts = []
            if verdict:
                parts.append(verdict)
            if rec:
                parts.append(f"【答辩意见】{rec}")
            lines.append(f"\n  {'  |  '.join(parts)}")

    lines.append("")
    return "\n".join(lines)


def process_dataset(
    dataset_key: str,
    skip_review: bool = False,
    skip_parse: bool = False,
    force: bool = False,
    review_model: str | None = None,
    coverage_model: str | None = None,
) -> dict[str, Any]:
    """Run the full pipeline for one dataset."""
    ds = DATASETS[dataset_key]
    print(f"\n{'=' * 60}")
    print(f"  Processing: {ds['name']} ({dataset_key})")
    print(f"{'=' * 60}")

    # Step 1: Read XLS reviews
    print("\n[1/5] Reading human reviews from XLS...")
    human_data = read_xls_reviews(dataset_key)
    print(f"  Found {len(human_data['reviews'])} reviewer(s)")

    if not human_data["reviews"]:
        print("  [ERROR] No reviews found, aborting.")
        return {}

    # Step 2: Extract PDF text
    print("\n[2/5] Extracting PDF text...")
    pdf_path = ds["pdf"]
    if not pdf_path.exists():
        print(f"  [ERROR] PDF not found: {pdf_path}")
        return {}
    paper_text = extract_pdf_text(pdf_path)

    # Step 3: Run auto-review
    print("\n[3/5] Running auto-review...")
    if skip_review:
        auto_result = _load_cached_review(dataset_key)
    else:
        auto_result = run_auto_review(dataset_key, paper_text, force=force, model=review_model)

    if not auto_result or "error" in auto_result:
        print(f"  [ERROR] Auto-review failed: {auto_result.get('error', 'unknown') if auto_result else 'no result'}")
        return {}

    # Step 4: Parse human reviews into points
    print("\n[4/5] Parsing human reviews...")
    if skip_parse:
        short = ds["short"]
        cache_file = PARSE_CACHE_DIR / f"{short}.json"
        if cache_file.exists():
            with open(cache_file, encoding="utf-8") as f:
                parsed_human = json.load(f)
        else:
            print("  No cached parse found, running parse...")
            parsed_human = parse_human_reviews(human_data, force=force, model=review_model)
    else:
        parsed_human = parse_human_reviews(human_data, force=force, model=review_model)

    # Step 5: Coverage comparison
    print("\n[5/5] Computing coverage...")
    coverage_data = compute_coverage(parsed_human, auto_result, force=force)

    # Generate readable report
    readable = format_comparison(coverage_data, human_data, auto_result)
    output_path = CACHE_DIR / f"{ds['short']}_comparison.txt"
    with open(output_path, "w", encoding="utf-8") as f:
        f.write(readable)
    print(f"\n  Comparison saved to: {output_path}")

    # Save multi-expert review output (if available)
    overall_summary = auto_result.get("overall_summary", {})
    if isinstance(overall_summary, dict) and "reviewers" in overall_summary:
        expert_path = CACHE_DIR / f"{ds['short']}_expert_reviews.txt"
        with open(expert_path, "w", encoding="utf-8") as f:
            f.write(format_expert_reviews(overall_summary["reviewers"], ds["name"]))
        print(f"  Expert reviews saved to: {expert_path}")

    return {
        "human_data": human_data,
        "auto_result": auto_result,
        "parsed_human": parsed_human,
        "coverage_data": coverage_data,
        "readable": readable,
    }


def _load_cached_review(dataset_key: str) -> dict | None:
    short = DATASETS[dataset_key]["short"]
    cache_file = REVIEW_CACHE_DIR / f"{short}-{PIPELINE_VERSION}.json"
    if cache_file.exists():
        with open(cache_file, encoding="utf-8") as f:
            return json.load(f)
    return None


def main():
    import argparse
    parser = argparse.ArgumentParser(description="Evaluate thesis auto-review coverage")
    parser.add_argument("--dataset1", action="store_true", help="Process dataset1")
    parser.add_argument("--dataset2", action="store_true", help="Process dataset2")
    parser.add_argument("--dataset3", action="store_true", help="Process dataset3")
    parser.add_argument("--skip-review", action="store_true", help="Skip auto-review (use cached)")
    parser.add_argument("--skip-parse", action="store_true", help="Skip human review parsing (use cached)")
    parser.add_argument("--force", action="store_true", help="Re-run all steps")
    parser.add_argument("--report-only", action="store_true", help="Only regenerate report from cache")
    args = parser.parse_args()

    # Default: process both
    datasets_to_run = []
    if args.dataset1:
        datasets_to_run.append("dataset1")
    if args.dataset2:
        datasets_to_run.append("dataset2")
    if args.dataset3:
        datasets_to_run.append("dataset3")
    if not datasets_to_run:
        datasets_to_run = ["dataset1", "dataset2"]

    all_results: list[dict] = []
    human_data_list: list[dict] = []
    coverage_list: list[dict] = []

    for dsk in datasets_to_run:
        result = process_dataset(
            dsk,
            skip_review=args.skip_review or args.report_only,
            skip_parse=args.skip_parse or args.report_only,
            force=args.force,
        )
        if result:
            all_results.append(result)
            human_data_list.append(result["human_data"])
            coverage_list.append(result["coverage_data"])

    # Print summary
    if coverage_list:
        print("\n\n" + format_summary(coverage_list, human_data_list))

    # Also save summary
    if coverage_list:
        summary_path = CACHE_DIR / "summary.txt"
        with open(summary_path, "w", encoding="utf-8") as f:
            f.write(format_summary(coverage_list, human_data_list))
        print(f"\nSummary saved to: {summary_path}")


if __name__ == "__main__":
    main()
