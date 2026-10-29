"""Writing figures components."""

from __future__ import annotations

import json
import logging
from pathlib import Path
import re
from typing import Any

from researchclaw.pipeline.support.artifacts import active_stage_glob
from researchclaw.pipeline.support.context import _chat_with_prompt
from researchclaw.pipeline.support.templates import _extract_paper_title

logger = logging.getLogger(__name__)


def _generate_framework_diagram_prompt(
    paper_text: str,
    config: "RCConfig",
    *,
    llm: "LLMClient | None" = None,
) -> str:
    """Generate a text-to-image prompt for a methodology framework diagram.

    Reads the paper's method section and produces a detailed prompt suitable
    for AI image generators (DALL-E, Midjourney, etc.).  The prompt describes
    an academic-style architecture/framework overview figure.

    Returns the prompt as a Markdown string, or empty string on failure.
    """
    import re as _re

    # Extract method/approach section from paper
    _method_section = ""
    _method_patterns = [
        r"(?:^#{1,3}\s+(?:Method(?:ology)?|Approach|Proposed\s+(?:Method|Framework|Approach)|Our\s+Method|Technical\s+Approach|Model\s+Architecture).*?)(?=^#{1,3}\s+|\Z)",
    ]
    for _pat in _method_patterns:
        _match = _re.search(_pat, paper_text, _re.MULTILINE | _re.DOTALL | _re.IGNORECASE)
        if _match:
            _method_section = _match.group(0)[:3000]
            break

    if not _method_section:
        # Fallback: use abstract + first 1500 chars
        _abs_match = _re.search(
            r"(?:^#{1,2}\s+Abstract\s*\n)(.*?)(?=^#{1,2}\s+|\Z)",
            paper_text, _re.MULTILINE | _re.DOTALL | _re.IGNORECASE,
        )
        _method_section = (_abs_match.group(1)[:1500] if _abs_match else paper_text[:2000])

    title = _extract_paper_title(paper_text)
    topic = config.research.topic

    # Use LLM to generate the prompt if available
    if llm is not None:
        _system = (
            "You are an expert academic figure designer. Generate a detailed text-to-image "
            "prompt for creating a methodology framework/architecture overview diagram.\n\n"
            "Requirements:\n"
            "- Academic style: clean, professional, suitable for a top-tier ML conference paper\n"
            "- Color palette: sophisticated and harmonious (suggest specific hex colors, "
            "prefer muted blues #4477AA, teals #44AA99, warm accents #CCBB44, soft purples #AA3377)\n"
            "- Layout: left-to-right or top-to-bottom data flow, with clearly labeled components\n"
            "- Components: boxes/modules with rounded corners, directional arrows, clear labels\n"
            "- Information density: high but not cluttered — each box should have a short label\n"
            "- Text on figure: minimal, only component names and key annotations\n"
            "- Background: white or very light grey\n"
            "- Style: vector-art look, flat design with subtle shadows, NO photorealism\n\n"
            "Output ONLY the prompt text (no markdown headers, no explanations). "
            "The prompt should be 150-300 words, highly specific and actionable."
        )
        _user = (
            f"Paper title: {title}\n"
            f"Research topic: {topic}\n\n"
            f"Method section excerpt:\n{_method_section}\n\n"
            "Generate a detailed text-to-image prompt for the methodology framework diagram."
        )
        try:
            resp = _chat_with_prompt(llm, _system, _user, max_tokens=1024)
            _llm_prompt = resp.content.strip()
            if len(_llm_prompt) > 50:
                return (
                    f"# Framework Diagram Prompt\n\n"
                    f"**Paper**: {title}\n\n"
                    f"## Image Generation Prompt\n\n"
                    f"{_llm_prompt}\n\n"
                    f"## Usage Instructions\n\n"
                    f"1. Copy the prompt above into an AI image generator "
                    f"(DALL-E 3, Midjourney, Ideogram, etc.)\n"
                    f"2. Generate the image at high resolution (2048x1024 or similar landscape)\n"
                    f"3. Save as `framework_diagram.png` in the same `charts/` folder\n"
                    f"4. Insert into the paper's Method section using:\n"
                    f"   - LaTeX: `\\includegraphics[width=\\textwidth]{{charts/framework_diagram.png}}`\n"
                    f"   - Markdown: `![Framework Overview](charts/framework_diagram.png)`\n"
                )
        except Exception:
            logger.debug("Framework prompt LLM generation failed, using template")

    # Fallback: template-based prompt without LLM
    _components = []
    _component_patterns = [
        (r"(?:encoder|decoder|transformer|attention|convolution|MLP|GNN|ResNet|ViT)", "Neural Network Module"),
        (r"(?:loss|objective|criterion|training|optimization)", "Training/Optimization"),
        (r"(?:data|dataset|input|preprocessing|augmentation)", "Data Pipeline"),
        (r"(?:output|prediction|inference|evaluation)", "Output/Evaluation"),
    ]
    _method_lower = _method_section.lower()
    for pat, label in _component_patterns:
        if _re.search(pat, _method_lower):
            _components.append(label)

    if not _components:
        _components = ["Input Processing", "Core Model", "Training Loop", "Evaluation"]

    return (
        f"# Framework Diagram Prompt\n\n"
        f"**Paper**: {title}\n\n"
        f"## Image Generation Prompt\n\n"
        f"Create a clean, academic-style methodology framework diagram for a research paper "
        f"titled \"{title}\". "
        f"The diagram should show a left-to-right data flow pipeline with these main components: "
        f"{', '.join(_components)}. "
        f"Use a professional color palette with muted blues (#4477AA), teals (#44AA99), "
        f"warm yellows (#CCBB44), and soft purples (#AA3377) on a white background. "
        f"Each component should be a rounded rectangle with a short label inside. "
        f"Connect components with clean directional arrows. "
        f"Add subtle shadows for depth. Flat vector-art style, no photorealism. "
        f"High information density but visually clean. "
        f"Suitable for a top-tier machine learning conference paper (ICML/NeurIPS/ICLR). "
        f"Landscape orientation, 2048x1024 resolution.\n\n"
        f"## Usage Instructions\n\n"
        f"1. Copy the prompt above into an AI image generator "
        f"(DALL-E 3, Midjourney, Ideogram, etc.)\n"
        f"2. Generate the image at high resolution (2048x1024 or similar landscape)\n"
        f"3. Save as `framework_diagram.png` in the same `charts/` folder\n"
        f"4. Insert into the paper's Method section using:\n"
        f"   - LaTeX: `\\includegraphics[width=\\textwidth]{{charts/framework_diagram.png}}`\n"
        f"   - Markdown: `![Framework Overview](charts/framework_diagram.png)`\n"
    )


_FIGURE_PROMPT_RE = re.compile(
    r"<!--\s*FIGURE_PROMPT\s*\n(.*?)-->",
    re.DOTALL,
)


_ACADEMIC_STYLE_SUFFIX = (
    " The image should be in a clean, professional ACADEMIC style suitable "
    "for a top-tier AI/ML research paper (NeurIPS, ICML, ICLR). "
    "Use a white or light background. Use clear labels and annotations. "
    "Avoid excessive decoration. Use a consistent color palette. "
    "Text should be legible at column width (~3.25 inches). "
    "Style: technical illustration, vector-like, clean lines."
)


_FIGURE_TYPE_STYLE: dict[str, str] = {
    "architecture_diagram": (
        "Show model layers, connections, and data flow with labeled boxes and arrows. "
        "Use a consistent left-to-right or top-to-bottom layout. "
        "Group related components with dashed borders."
    ),
    "method_flowchart": (
        "Step-by-step process flow with rounded rectangles for processes, "
        "diamonds for decision points, arrows with labels. "
        "Number sequential steps. Highlight novel steps with accent color."
    ),
    "pipeline_overview": (
        "Full pipeline from input to output with distinct visual blocks per stage. "
        "Include example inputs/outputs. Use consistent arrow style. "
        "Show parallel/branching paths if applicable."
    ),
    "concept_illustration": (
        "Simple, clean diagram illustrating a key concept or intuition. "
        "Include before/after or problem/solution comparison. "
        "Keep it understandable at a glance."
    ),
    "system_diagram": (
        "Overall system architecture with major components and interactions. "
        "Show data stores, APIs, external services with labeled connections."
    ),
    "comparison_illustration": (
        "Side-by-side comparison of approaches with consistent styling. "
        "Highlight key differences with visual cues (color, checkmarks/crosses)."
    ),
    "attention_visualization": (
        "Attention weights or patterns with heatmap-style coloring. "
        "Include input/output sequences, label attention heads, "
        "use a clear color scale legend."
    ),
}


def _extract_figure_prompts(draft: str, topic: str = "") -> list[dict]:
    """Parse ``<!-- FIGURE_PROMPT ... -->`` blocks from a paper draft.

    Returns a list of dicts, each with keys:
        figure_id, figure_type, section, caption, aspect_ratio,
        raw_prompt, full_prompt  (raw_prompt + academic style + type guidelines)
    """
    _top_level_keys = {
        "figure_id", "figure_type", "section", "caption", "aspect_ratio", "prompt",
    }

    results: list[dict] = []
    for m in _FIGURE_PROMPT_RE.finditer(draft):
        block = m.group(1)
        entry: dict[str, str] = {}
        current_key = ""
        current_lines: list[str] = []
        in_multiline = False

        for line in block.splitlines():
            stripped = line.strip()
            if not stripped:
                if in_multiline:
                    current_lines.append("")
                continue

            # Check if this line starts a new top-level key (not indented,
            # key name is a known field)
            is_new_key = False
            if ":" in stripped and not line.startswith((" ", "\t")):
                candidate_key = stripped.partition(":")[0].strip().lower()
                if candidate_key in _top_level_keys:
                    is_new_key = True

            if is_new_key:
                key, _, val = stripped.partition(":")
                key = key.strip().lower()
                val = val.strip().strip('"').strip("'")
                if current_key and current_lines:
                    entry[current_key] = "\n".join(current_lines).strip()
                current_key = key
                in_multiline = (val == "|" or val == "")
                if val and val != "|":
                    current_lines = [val]
                    in_multiline = False
                else:
                    current_lines = []
                    if val == "|":
                        in_multiline = True
            else:
                current_lines.append(stripped)
        if current_key and current_lines:
            entry[current_key] = "\n".join(current_lines).strip()

        fig_id = entry.get("figure_id", f"figure_{len(results) + 1}")
        fig_type = entry.get("figure_type", "concept_illustration")
        section = entry.get("section", "Method")
        caption = entry.get("caption", "")
        aspect_ratio = entry.get("aspect_ratio", "16:9")
        raw_prompt = entry.get("prompt", caption)

        type_style = _FIGURE_TYPE_STYLE.get(fig_type, _FIGURE_TYPE_STYLE["concept_illustration"])
        full_prompt = (
            f"Create a professional academic figure for the '{section}' "
            f"section of a research paper"
        )
        if topic:
            full_prompt += f" about: {topic}"
        full_prompt += (
            f".\n\nFigure description: {raw_prompt}\n\n"
            f"Style guidelines:\n{type_style}\n\n"
            f"{_ACADEMIC_STYLE_SUFFIX}"
        )

        results.append({
            "figure_id": fig_id,
            "figure_type": fig_type,
            "section": section,
            "caption": caption,
            "aspect_ratio": aspect_ratio,
            "raw_prompt": raw_prompt,
            "full_prompt": full_prompt,
        })

    return results


def _render_figure_prompts(
    fig_prompts: list[dict],
    stage_dir: Path,
    config: Any,
    llm: Any,
    topic: str = "",
) -> list[dict]:
    """Render extracted FIGURE_PROMPT entries via NanoBananaAgent.

    Uses the OpenAI-compatible proxy (same base_url/api_key as the LLM) to
    call an image-capable model when explicitly enabled.
    Gracefully skips if no API credentials are available.

    Returns the *same* ``fig_prompts`` list, augmented with ``output_path``
    and ``success`` fields for each entry.
    """
    if not fig_prompts:
        return fig_prompts

    fa_cfg = getattr(config.experiment, "figure_agent", None)
    if fa_cfg is not None and not getattr(fa_cfg, "nano_banana_enabled", False):
        for fp in fig_prompts:
            fp["success"] = False
            fp["skipped"] = "image_generation_disabled_for_qwen3_only_run"
        logger.info("NanoBanana render skipped — Qwen3-only run disables non-Qwen image generation")
        return fig_prompts

    from researchclaw.agents.figure_agent.nano_banana import NanoBananaAgent

    from researchclaw.llm import resolve_provider_base_url
    base_url = resolve_provider_base_url(
        getattr(config.llm, "provider", "openai-compatible"),
        getattr(config.llm, "base_url", ""),
    )
    api_key = getattr(config.llm, "api_key", "") or ""
    if not api_key:
        logger.warning(
            "NanoBanana render skipped — no llm.api_key"
        )
        return fig_prompts

    image_model = getattr(config.llm, "image_model", "") or ""
    if not image_model:
        image_model = (
            (getattr(fa_cfg, "gemini_model", "") if fa_cfg else "")
            or "Qwen3.5-122B-A10B-FP8"
        )

    figures_dir = stage_dir / "figures"
    figures_dir.mkdir(parents=True, exist_ok=True)

    image_figures = [
        {
            "figure_id": fp["figure_id"],
            "figure_type": fp.get("figure_type", "concept_illustration"),
            "section": fp.get("section", "Method"),
            "description": fp.get("raw_prompt", fp.get("caption", "")),
        }
        for fp in fig_prompts
    ]

    agent = NanoBananaAgent(
        llm,
        base_url=base_url,
        openai_api_key=api_key,
        image_model=image_model,
        output_dir=figures_dir,
    )

    try:
        result = agent.execute({
            "image_figures": image_figures,
            "topic": topic,
            "output_dir": str(figures_dir),
        })
    except Exception as exc:
        logger.warning("NanoBanana rendering failed: %s", exc)
        return fig_prompts

    gen_map = {
        g["figure_id"]: g for g in result.data.get("generated", [])
    }
    for fp in fig_prompts:
        gen = gen_map.get(fp["figure_id"], {})
        fp["output_path"] = gen.get("output_path", "")
        fp["success"] = gen.get("success", False)

    success_count = sum(1 for fp in fig_prompts if fp.get("success"))
    logger.info(
        "NanoBanana rendered %d/%d figure prompts → %s",
        success_count, len(fig_prompts), figures_dir,
    )
    return fig_prompts


def _read_figure_manifest(run_dir: Path) -> dict[str, dict]:
    """Read ``figure_manifest.json`` from stage-16/charts/ (or stage-14).

    Returns ``{filename: metadata_dict}`` for each chart entry.
    """
    for pattern in ("stage-16*", "stage-14*"):
        for d in sorted(active_stage_glob(run_dir, pattern)):
            mf = d / "charts" / "figure_manifest.json"
            if mf.is_file():
                try:
                    entries = json.loads(mf.read_text(encoding="utf-8"))
                    return {
                        Path(e["file_path"]).name: e
                        for e in entries
                        if isinstance(e, dict) and e.get("file_path")
                    }
                except Exception:
                    pass
    return {}


_BULLET_LENIENT_SECTIONS = frozenset({
    "introduction", "limitations", "limitation",
    "limitations and future work", "abstract",
})


_BALANCE_SECTIONS = frozenset({
    "introduction", "related work", "method", "experiments", "results",
    "discussion",
})
