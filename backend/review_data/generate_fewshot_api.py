"""Generate expanded few-shot library using the DeepSeek API.

Strategy:
  - Read the existing 16 curated examples as seed/format reference
  - For each of 7 dimensions, call DeepSeek API to generate 8-10 new examples
  - Stratify across quality levels (high/medium/low)
  - Combine existing + new examples into the expanded library

Usage:
    python3 -m review_data.generate_fewshot_api
"""

from __future__ import annotations

import json
import os
import re
import sys
import time
import urllib.request
import urllib.error
from pathlib import Path
from typing import Any

SCRIPT_DIR = Path(__file__).resolve().parent
BACKEND_DIR = SCRIPT_DIR.parent
ROOT_DIR = BACKEND_DIR.parent

# Load config for API credentials
sys.path.insert(0, str(BACKEND_DIR / "agent"))

# =============================================================================
# Config — from environment or config.arc.yaml
# =============================================================================

API_URL = os.environ.get(
    "FEWSHOT_API_URL",
    "https://api.deepseek.com/chat/completions",
)
API_KEY = os.environ.get("FEWSHOT_API_KEY", "") or os.environ.get(
    "RESEARCHCLAW_API_KEY", ""
)
MODEL = os.environ.get("FEWSHOT_MODEL", "deepseek-v4-pro")

if not API_KEY:
    # Try reading from config.arc.yaml
    try:
        import yaml as _yaml
        config_path = ROOT_DIR / "config.arc.yaml"
        if config_path.exists():
            with open(config_path) as _f:
                _cfg = _yaml.safe_load(_f) or {}
            _llm = _cfg.get("web_chat_llm", {}) or {}
            API_KEY = (
                str(_llm.get("api_key", ""))
                or os.environ.get(
                    str(_llm.get("api_key_env", "RESEARCHCLAW_API_KEY")), ""
                )
                or ""
            )
    except Exception:
        pass

if not API_KEY:
    print("WARNING: No API key found. Set FEWSHOT_API_KEY or RESEARCHCLAW_API_KEY.")
    print("Usage: FEWSHOT_API_KEY=sk-... python3 -m review_data.generate_fewshot_api")

# =============================================================================
# OpenReview API simulation - in case we can get real review data
# =============================================================================

def openreview_fetch_notes(invitation: str, limit: int = 50) -> list[dict]:
    """Fetch notes from OpenReview API for a given invitation.

    Returns empty list if API is unavailable.
    """
    url = f"https://api.openreview.net/notes?invitation={urllib.parse.quote(invitation)}&limit={limit}"
    req = urllib.request.Request(url, headers={"User-Agent": "ClawAI-Review/1.0"})
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            data = json.loads(resp.read())
            return data.get("notes", [])
    except Exception:
        return []


# =============================================================================
# LLM client
# =============================================================================

def llm_chat(system: str, prompt: str, temperature: float = 0.7) -> str:
    """Call DeepSeek API and return response text."""
    body = json.dumps({
        "model": MODEL,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": prompt},
        ],
        "temperature": temperature,
        "max_tokens": 8192,
    }).encode()

    req = urllib.request.Request(
        API_URL,
        data=body,
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {API_KEY}",
        },
    )
    for attempt in range(3):
        try:
            with urllib.request.urlopen(req, timeout=120) as resp:
                result = json.loads(resp.read())
                choice = result["choices"][0]
                content = choice["message"]["content"]
                return content
        except urllib.error.HTTPError as e:
            body_text = e.read().decode()[:500]
            if attempt < 2:
                wait = 2 ** attempt
                print(f"  [retry {attempt + 1}] API error: {body_text[:100]}... waiting {wait}s")
                time.sleep(wait)
            else:
                raise RuntimeError(f"API failed: {body_text}")
        except (urllib.error.URLError, OSError) as e:
            if attempt < 2:
                wait = 2 ** attempt
                print(f"  [retry {attempt + 1}] Connection error: {e}... waiting {wait}s")
                time.sleep(wait)
            else:
                raise
    return ""


# =============================================================================
# Generation prompt
# =============================================================================

SYSTEM_PROMPT = """You are an expert machine learning peer reviewer with experience reviewing for top conferences (NeurIPS, ICML, ICLR, CVPR, ACL).

You write thorough, evidence-anchored reviews that:
1. Reference specific sections, figures, tables, and equations in the paper
2. Are critical but constructive
3. Include concrete suggestions for improvement

You will generate structured few-shot review examples that represent reviews at different quality levels:

- HIGH quality (score 7-9/10): Reviews that are thorough, evidence-anchored, specific, and balanced — strengths AND weaknesses
- MEDIUM quality (score 4-6/10): Reviews with some good points but noticeable gaps — less specific, missing some aspects
- LOW quality (score 1-3/10): Superficial reviews — vague, missing evidence, overly brief, or missing key critiques

Each example must have exactly this structure:
  - score: 1-10 integer
  - summary: 1-2 sentence overview of the review
  - strengths: 3 specific, evidence-anchored items (cite §, Fig, Table, Eq)
  - weaknesses: 3 specific, evidence-anchored items
  - suggestions: 3 actionable, constructive suggestions

Make the examples realistic — they should read like real peer reviews, not textbook examples."""


def _build_generation_prompt(
    dim_id: str,
    dim_label: str,
    dim_description: str,
    seed_examples: list[dict],
    count: int = 8,
) -> str:
    """Build prompt to generate examples for one dimension."""
    seed_text = ""
    if seed_examples:
        seed_text = "Here are reference examples showing the expected format:\n\n"
        for i, ex in enumerate(seed_examples):
            seed_text += f"Example {i+1}:\n{json.dumps(ex, indent=2, ensure_ascii=False)}\n\n"

    return f"""Generate {count} diverse few-shot review examples for the review dimension "{dim_label}" ({dim_id}).

Dimension description: {dim_description}

{seed_text}
IMPORTANT REQUIREMENTS:

1. Each example must be a complete, realistic peer review focused SPECIFICALLY on {dim_label}.
   Do NOT write reviews that discuss all dimensions — stay focused on {dim_id}.

2. Cover a RANGE of quality levels:
   - 2-3 high quality (score 7-9): thorough, specific, evidence-anchored
   - 3-4 medium quality (score 4-6): decent but with gaps
   - 2-3 low quality (score 1-3): superficial, missing details

3. Each review must have:
   - score: integer 1-10
   - summary: concise 1-2 sentence assessment of {dim_label}
   - strengths: 3 items, each referencing specific paper content (§, Fig, Table, Eq)
   - weaknesses: 3 items, each referencing specific paper content
   - suggestions: 3 actionable recommendations

4. Make them realistic:
   - High-quality reviews should cite specific sections and be nuanced
   - Low-quality reviews should be genuinely weak (vague, short, missing evidence)
   - Vary the paper topics (NLP, CV, RL, theory, systems, etc.)

Output format: Return ONLY a JSON array of objects. Each object must have keys:
score, summary, strengths, weaknesses, suggestions.

Example format:
[
  {{
    "score": 8,
    "summary": "The paper presents a thorough evaluation...",
    "strengths": ["Clear hypothesis stated in §1", "Comprehensive baselines in Table 2", "Well-designed ablation in §4.3"],
    "weaknesses": ["Missing statistical significance tests in §4", "Only one dataset domain used", "No error bars reported in Fig 3"],
    "suggestions": ["Add significance testing", "Test on at least one additional domain", "Report confidence intervals"]
  }}
]

Generate exactly {count} examples, with a diversity of scores spanning the 1-10 range."""


# =============================================================================
# Dimension definitions
# =============================================================================

DIMENSIONS = [
    {
        "id": "methodology",
        "label": "Methodology",
        "description": "Soundness of the proposed method/design, technical correctness, theoretical grounding, and appropriateness of the approach for the problem.",
    },
    {
        "id": "novelty",
        "label": "Novelty",
        "description": "Originality and significance of the contribution relative to existing work. Whether the paper introduces genuinely new ideas or is incremental.",
    },
    {
        "id": "experiment",
        "label": "Experiment",
        "description": "Thoroughness of experimental evaluation, quality and diversity of baselines, ablation studies, statistical rigor, and reproducibility of results.",
    },
    {
        "id": "writing",
        "label": "Writing",
        "description": "Clarity of exposition, paper organization, quality of figures and tables, correctness of notation, and overall readability.",
    },
    {
        "id": "related_work",
        "label": "Related Work",
        "description": "Coverage of relevant literature, accurate positioning of contributions, and appropriate comparison with prior approaches.",
    },
    {
        "id": "reproducibility",
        "label": "Reproducibility",
        "description": "Availability of code and data, completeness of implementation details, documentation of hyperparameters and training configurations.",
    },
    {
        "id": "ethics",
        "label": "Ethics",
        "description": "Consideration of ethical implications, bias analysis, fairness evaluation, privacy concerns, and broader societal impact.",
    },
]


# =============================================================================
# Parse LLM output to structured examples
# =============================================================================

def parse_examples(text: str) -> list[dict]:
    """Parse JSON array from LLM response."""
    # Try to extract JSON array
    text = text.strip()
    # Remove markdown code fences if present
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*", "", text)
        text = re.sub(r"\s*```$", "", text)

    # Try direct parse
    try:
        data = json.loads(text)
        if isinstance(data, list):
            return data
        return []
    except json.JSONDecodeError:
        pass

    # Try to find JSON array in text
    match = re.search(r"\[\s*\{.*\}\s*\]", text, re.DOTALL)
    if match:
        try:
            data = json.loads(match.group())
            if isinstance(data, list):
                return data
        except json.JSONDecodeError:
            pass

    print(f"  WARNING: Could not parse JSON from response (first 200 chars): {text[:200]}")
    return []


def validate_example(ex: dict) -> bool:
    """Validate a single example has all required fields."""
    required = {"score", "summary", "strengths", "weaknesses", "suggestions"}
    if not all(k in ex for k in required):
        return False
    if not isinstance(ex["score"], (int, float)):
        return False
    if not isinstance(ex["strengths"], list) or len(ex["strengths"]) < 2:
        return False
    if not isinstance(ex["weaknesses"], list) or len(ex["weaknesses"]) < 2:
        return False
    if not isinstance(ex["suggestions"], list) or len(ex["suggestions"]) < 1:
        return False
    if not (1 <= ex["score"] <= 10):
        return False
    return True


# =============================================================================
# Format as Python source
# =============================================================================

def _escape(s: str) -> str:
    """Escape string for Python source inclusion."""
    return s.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n").replace("\r", "")


def format_as_make_call(ex: dict, dim_id: str, score: float) -> str:
    """Format one example as a _make() call."""
    strengths = [str(s)[:200] for s in ex.get("strengths", [])[:3]]
    while len(strengths) < 3:
        strengths.append("(See paper for details)")
    weaknesses = [str(w)[:200] for w in ex.get("weaknesses", [])[:3]]
    while len(weaknesses) < 3:
        weaknesses.append("(See paper for details)")
    suggestions = [str(s)[:200] for s in ex.get("suggestions", [])[:3]]
    while len(suggestions) < 3:
        suggestions.append("(See paper for details)")

    summary = str(ex.get("summary", ""))[:300]

    lines = ["    _make("]
    lines.append(f'        dim_id="{dim_id}",')
    lines.append(f"        score={score},")
    lines.append(f'        summary="{_escape(summary)}",')
    lines.append("        strengths=[")
    for s in strengths:
        lines.append(f'            "{_escape(s)}",')
    lines.append("        ],")
    lines.append("        weaknesses=[")
    for w in weaknesses:
        lines.append(f'            "{_escape(w)}",')
    lines.append("        ],")
    lines.append("        suggestions=[")
    for s in suggestions:
        lines.append(f'            "{_escape(s)}",')
    lines.append("        ],")
    lines.append("    ),")

    return "\n".join(lines)


# =============================================================================
# Generate all
# =============================================================================

def generate_all(target_per_dim: int = 10, temperature: float = 0.7) -> dict[str, list[dict]]:
    """Generate examples for all dimensions."""
    results: dict[str, list[dict]] = {}

    for dim in DIMENSIONS:
        dim_id = dim["id"]
        dim_label = dim["label"]
        dim_desc = dim["description"]

        print(f"\n{'=' * 60}")
        print(f"Generating: {dim_label} ({dim_id})")
        print(f"{'=' * 60}")

        # Each LLM call generates 8 examples
        # Call twice to get 16, then select best
        all_examples: list[dict] = []

        for batch in range(2):
            prompt = _build_generation_prompt(
                dim_id=dim_id,
                dim_label=dim_label,
                dim_description=dim_desc,
                seed_examples=results.get(dim_id, [])[-2:] if results.get(dim_id) else [],
                count=6,
            )

            print(f"  Batch {batch + 1}/2: calling API...")
            try:
                text = llm_chat(SYSTEM_PROMPT, prompt, temperature=temperature)
                examples = parse_examples(text)
                valid = [ex for ex in examples if validate_example(ex)]
                print(f"  Got {len(examples)} examples ({len(valid)} valid)")
                all_examples.extend(valid)
            except Exception as e:
                print(f"  ERROR: {e}")
                continue

            time.sleep(1)  # Polite delay

        # Deduplicate by summary
        seen_summaries: set[str] = set()
        unique_examples: list[dict] = []
        for ex in all_examples:
            key = str(ex.get("summary", ""))[:50]
            if key not in seen_summaries:
                unique_examples.append(ex)
                seen_summaries.add(key)

        print(f"  Total unique valid: {len(unique_examples)}")

        if not unique_examples:
            print(f"  WARNING: No examples generated for {dim_id}!")
            continue

        # Score-stratified selection
        by_score: dict[str, list[dict]] = {"high": [], "medium": [], "low": []}
        for ex in unique_examples:
            s = ex["score"]
            if s >= 7:
                by_score["high"].append(ex)
            elif s >= 4:
                by_score["medium"].append(ex)
            else:
                by_score["low"].append(ex)

        selected = []
        selected.extend(by_score["high"][:4])
        selected.extend(by_score["medium"][:4])
        selected.extend(by_score["low"][:3])

        # Fill remaining from best overall
        if len(selected) < target_per_dim:
            remaining = [ex for ex in unique_examples if ex not in selected]
            selected.extend(remaining[: target_per_dim - len(selected)])

        results[dim_id] = selected[:target_per_dim]
        h = min(len(by_score.get("high", [])), 4)
        m = min(len(by_score.get("medium", [])), 4)
        l = min(len(by_score.get("low", [])), 3)
        print(f"  Selected: {len(results[dim_id])} "
              f"(high={h}, medium={m}, low={l})")

    return results


# =============================================================================
# Write library
# =============================================================================

LIBRARY_HEADER = '''"""Expanded few-shot review examples — auto-generated via LLM.

Generated using {model} from curated seed reviews.
Contains {total} diverse examples across all 7 review dimensions.

Score reference:
  9-10/10 = exceptional (strong accept)
  7-8/10  = good (accept)
  5-6/10  = marginal (borderline)
  3-4/10  = weak (reject)
  1-2/10  = poor (strong reject)
"""

from __future__ import annotations

from .schema import Review, ReviewDataset, DimensionReview


def _make(
    dim_id: str,
    score: float,
    summary: str,
    strengths: list[str],
    weaknesses: list[str],
    suggestions: list[str],
    paper_title: str = "",
    paper_venue: str = "",
) -> Review:
    """Build a Review object quickly."""
    strengths_fixed = [s[:200] for s in strengths[:3]]
    while len(strengths_fixed) < 3:
        strengths_fixed.append("(See paper for details)")
    weaknesses_fixed = [w[:200] for w in weaknesses[:3]]
    while len(weaknesses_fixed) < 3:
        weaknesses_fixed.append("(See paper for details)")
    suggestions_fixed = [s[:200] for s in suggestions[:3]]
    while len(suggestions_fixed) < 3:
        suggestions_fixed.append("(See paper for details)")

    return Review(
        source="curated",
        paper_title=paper_title,
        paper_venue=paper_venue,
        overall_score=score,
        comment_to_author=summary,
        strengths=strengths_fixed,
        weaknesses=weaknesses_fixed,
        suggestions=suggestions_fixed,
        dimensions=[DimensionReview(dimension_id=dim_id, score=score, summary=summary)],
    )
'''


DIM_SECTION_TMPL = '''\
# =============================================================================
# {label}
# ({count} examples)
# =============================================================================

{var_name} = [
{examples}
]

'''


FOOTER_TMPL = '''\
# =============================================================================
# Combined dataset
# =============================================================================

ALL_DIMENSION_EXAMPLES: dict[str, list[Review]] = {{
{entries}
}}


def get_examples_for_dimension(
    dim_id: str,
    min_score: float | None = None,
    max_score: float | None = None,
) -> list[Review]:
    """Get few-shot examples for a specific dimension, optionally filtered by score."""
    examples = ALL_DIMENSION_EXAMPLES.get(dim_id, [])
    if min_score is not None:
        examples = [e for e in examples if e.overall_score is not None and e.overall_score >= min_score]
    if max_score is not None:
        examples = [e for e in examples if e.overall_score is not None and e.overall_score <= max_score]
    return examples


def get_all_examples() -> list[Review]:
    """Get all few-shot examples across all dimensions."""
    all_examples: list[Review] = []
    for examples in ALL_DIMENSION_EXAMPLES.values():
        all_examples.extend(examples)
    return all_examples


def format_fewshot_block(examples: list[Review], max_chars: int = 1500) -> str:
    """Format a list of review examples as a few-shot prompt block."""
    if not examples:
        return ""

    parts: list[str] = []
    for i, ex in enumerate(examples[:3]):
        score_str = f"Score: {ex.overall_score:.0f}/10" if ex.overall_score is not None else ""
        block = f"Reference Example {i+1}: {score_str}\\n"
        if ex.strengths:
            block += "Strengths:\n" + "\n".join(f"- {{s}}" for s in ex.strengths[:2]) + "\n"
        if ex.weaknesses:
            block += "Weaknesses:\n" + "\n".join(f"- {{w}}" for w in ex.weaknesses[:2]) + "\n"
        if ex.suggestions:
            block += "Suggestions:\n" + "\n".join(f"- {{s}}" for s in ex.suggestions[:1]) + "\n"
        if len(block) > 600:
            block = block[:600] + "...\n"
        parts.append(block.strip())

    result = "\n\n".join(parts)
    return result[:max_chars]


def get_fewshot_dataset() -> ReviewDataset:
    """Get all curated examples as a ReviewDataset."""
    return ReviewDataset(
        name="curated_fewshot",
        source="curated",
        reviews=get_all_examples(),
    )
'''


def write_library(
    results: dict[str, list[dict]],
    output_path: str,
    existing_examples: dict[str, list[dict]] | None = None,
) -> None:
    """Write the expanded fewshot_library.py file."""
    total = sum(len(v) for v in results.values())
    lines = LIBRARY_HEADER.format(model=MODEL, total=total)
    lines += "\n"

    all_entries = []

    for dim in DIMENSIONS:
        dim_id = dim["id"]
        var_name = f"{dim_id.upper()}_EXAMPLES"
        examples = results.get(dim_id, [])

        if not examples:
            lines += f"# WARNING: No examples for {dim_id}\n{var_name} = []\n\n"
            all_entries.append(f'    "{dim_id}": {var_name},')
            continue

        # Combine with any existing manually-written examples
        if existing_examples and dim_id in existing_examples:
            existing = existing_examples[dim_id]
            # Deduplicate by checking summary overlap
            existing_summaries = {str(e.get("summary", ""))[:80] for e in examples}
            for ex in existing:
                if str(ex.get("summary", ""))[:80] not in existing_summaries:
                    examples.append(ex)

        lines += f"# =============================================================================\n"
        lines += f"# {dim['label']}\n"
        lines += f"# ({len(examples)} examples)\n"
        lines += f"# =============================================================================\n\n"
        lines += f"{var_name} = [\n"

        for ex in examples:
            score = ex["score"]
            lines += format_as_make_call(ex, dim_id, score) + "\n"

        lines += "]\n\n"
        all_entries.append(f'    "{dim_id}": {var_name},')

    lines += "# =============================================================================\n"
    lines += "# Combined dataset\n"
    lines += "# =============================================================================\n\n"
    lines += "ALL_DIMENSION_EXAMPLES: dict[str, list[Review]] = {\n"
    for entry in all_entries:
        lines += entry + "\n"
    lines += "}\n\n"
    lines += FOOTER_TMPL

    output_file = Path(SCRIPT_DIR / output_path)
    output_file.write_text(lines, encoding="utf-8")
    print(f"\n{'=' * 60}")
    print(f"Written: {output_file}")
    print(f"Total: {total} examples across {len(results)} dimensions")
    for dim in DIMENSIONS:
        count = len(results.get(dim["id"], []))
        if count:
            scores = [ex["score"] for ex in results[dim["id"]]]
            avg = sum(scores) / len(scores)
            print(f"  {dim['id']}: {count} examples, avg score {avg:.1f}, range {min(scores)}-{max(scores)}")
    print(f"{'=' * 60}")


# =============================================================================
# Main
# =============================================================================

if __name__ == "__main__":
    target = int(sys.argv[1]) if len(sys.argv) > 1 else 10
    temp = float(sys.argv[2]) if len(sys.argv) > 2 else 0.7

    print(f"Target: {target} examples per dimension, temperature={temp}")
    results = generate_all(target_per_dim=target, temperature=temp)
    write_library(results, "fewshot_library.py")
