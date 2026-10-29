"""Writing latex components."""

from __future__ import annotations

import json
import logging
from pathlib import Path
import re

from researchclaw.config import RCConfig
from researchclaw.pipeline.support.context import _chat_with_prompt
from researchclaw.pipeline.support.templates import _extract_paper_title

logger = logging.getLogger(__name__)


def _generate_latex_package(
    stage_dir: Path,
    run_dir: Path,
    config: RCConfig,
    paper_md: str,
    *,
    llm: "LLMClient | None" = None,
) -> list[str]:
    """Generate a complete LaTeX package (zip) ready for Overleaf upload.

    Uses LLM to convert markdown paper to LaTeX, then collects figures,
    references, and style files into a zip archive.
    """
    import shutil as _shutil_lp

    pkg_dir = stage_dir / "latex_package"
    if pkg_dir.exists():
        _shutil_lp.rmtree(pkg_dir)
    pkg_dir.mkdir(parents=True)
    fig_dir = pkg_dir / "figures"
    fig_dir.mkdir()

    # 1. Collect available figures first (need filenames for LLM prompt)
    _fig_files: list[str] = []
    for _fig_src in [
        stage_dir / "figures",
        run_dir / "stage-16" / "charts",
        run_dir / "stage-14" / "charts",
    ]:
        if _fig_src.is_dir():
            for _fig_file in _fig_src.iterdir():
                if _fig_file.suffix.lower() in {".png", ".jpg", ".jpeg", ".pdf", ".eps"}:
                    _shutil_lp.copy2(_fig_file, fig_dir / _fig_file.name)
                    _fig_files.append(_fig_file.name)

    # 2. Collect references.bib
    bib_text = ""
    for _bib_candidate in [
        run_dir / "stage-04" / "references.bib",
        run_dir / "stage-22" / "references.bib",
    ]:
        if _bib_candidate.exists():
            bib_text = _bib_candidate.read_text(encoding="utf-8")
            break
    if not bib_text:
        bib_text = "% No references collected\n"

    # Extract cite keys from bib
    _bib_keys = re.findall(r"@\w+\{([^,]+),", bib_text)

    # 3. Convert markdown to LaTeX. IEEE uses the deterministic converter so
    # the class, numeric citation style, and verified BibTeX cannot be replaced
    # by an LLM-generated NeurIPS preamble or fabricated bibliography.
    tex_content = ""
    _target_conference = str(getattr(config.export, "target_conference", "") or "")
    _is_ieee = _target_conference.lower().startswith("ieee")
    if _is_ieee:
        try:
            from researchclaw.templates import get_template, markdown_to_latex

            _ieee_paper_md = paper_md
            if not re.search(r"^#{1,3}\s+(?:Index Terms|Keywords)\s*$", _ieee_paper_md, re.MULTILINE | re.IGNORECASE):
                _topic_lower = str(getattr(config.research, "topic", "") or "").lower()
                _index_terms = ["reproducible experimentation", "evidence-bounded evaluation"]
                if "imu" in _topic_lower or "inertial" in _topic_lower:
                    _index_terms = ["inertial sensing", "visual-inertial odometry", "sensor fusion", "robust perception"]
                _abstract_match = re.search(r"(^#{1,3}\s+Abstract\s*$.*?)(?=^#{1,3}\s+)", _ieee_paper_md, re.MULTILINE | re.DOTALL | re.IGNORECASE)
                if _abstract_match:
                    _insert_at = _abstract_match.end(1)
                    _ieee_paper_md = (
                        _ieee_paper_md[:_insert_at]
                        + "\n\n## Index Terms\n"
                        + ", ".join(_index_terms[:6])
                        + "\n"
                        + _ieee_paper_md[_insert_at:]
                    )
            _tpl = get_template(_target_conference)
            tex_content = markdown_to_latex(
                _ieee_paper_md,
                _tpl,
                title=_extract_paper_title(_ieee_paper_md),
                authors=config.export.authors,
                bib_file=config.export.bib_file,
            )
            logger.info("LaTeX package: deterministic IEEE conversion successful (%d chars)", len(tex_content))
        except Exception as exc:
            logger.warning("LaTeX package: deterministic IEEE conversion failed: %s", exc)

    # Non-IEEE legacy templates retain the LLM conversion path.
    if llm is not None and not tex_content:
        _system_prompt = (
            "You are an expert academic LaTeX typesetter. You have TWO tasks:\n"
            "Task 1: Convert the markdown paper into a complete .tex file.\n"
            "Task 2: Generate a matching references.bib file.\n\n"
            "Output format: First output the complete .tex inside ```latex ... ```, "
            "then output the complete .bib inside ```bib ... ```.\n\n"
            "STRICT RULES:\n"
            "1. The .tex must be complete and compilable. No explanations outside code blocks.\n"
            "2. Use \\usepackage[preprint]{neurips_2025} as the style package.\n"
            "3. All citations [cite_xxx] in the markdown MUST become \\cite{xxx} in LaTeX. "
            "The key 'xxx' must match an entry in your generated .bib file.\n"
            "4. Generate the .bib file with REAL, accurate bibliography entries for every "
            "\\cite{} used in the paper. Use the citation context to identify the correct "
            "paper (author, title, year, venue). Do NOT fabricate — use real papers.\n"
            "5. References section: Do NOT include a hand-written reference list in the .tex. "
            "Use \\bibliographystyle{plainnat} and \\bibliography{references} at the end.\n"
            "5. Section hierarchy: Use \\section{} for main sections (Introduction, "
            "Related Work, Method, Experiments, Results, Discussion, Limitations, "
            "Conclusion). Use \\subsection{} for sub-sections within them.\n"
            "6. All figures are in the figures/ directory. Use "
            "\\includegraphics[width=0.85\\textwidth]{figures/filename.png} with "
            "proper \\begin{figure}...\\end{figure} environments, \\caption{}, "
            "and \\label{fig:xxx}.\n"
            "7. Convert <!-- FIGURE_PROMPT --> comment blocks into \\begin{figure} "
            "environments. Match figure prompts to available figure files by their "
            "content/description.\n"
            "8. Include ALL available figures in appropriate locations in the paper.\n"
            "9. Preserve all math equations (both inline $...$ and display $$...$$).\n"
            "10. Convert markdown tables to LaTeX \\begin{table}...\\end{table} with "
            "\\begin{tabular}, \\toprule, \\midrule, \\bottomrule.\n"
            "11. Use \\textit{} for italics, \\textbf{} for bold.\n"
            "12. The preamble must include: hyperref, url, booktabs, amsfonts, "
            "amsmath, graphicx, natbib, microtype, inputenc (utf8), fontenc (T1), "
            "placeins (for \\FloatBarrier).\n"
            "13. Use \\begin{figure}[htbp] (NOT [t]) so figures stay near their text.\n"
            "14. Add \\FloatBarrier before \\section{Conclusion} and before "
            "\\bibliographystyle to prevent figures from floating past them.\n"
            "15. Spread figures evenly across sections. Do NOT cluster multiple "
            "figures together — place each figure near the text that discusses it.\n"
            "16. Figure placement priority: figure_1.png should be the teaser/overview "
            "figure (after abstract/intro). figure_2.png should be the architecture/method "
            "figure. fig_main_results and fig_radar are RESULTS figures — place in Results section. "
            "Use the figure descriptions below to determine correct placement.\n"
        )

        # Load figure descriptions from figure_prompts.json
        _fig_descriptions = ""
        _fp_path = stage_dir / "figure_prompts.json"
        if _fp_path.exists():
            try:
                _fp_data = json.loads(_fp_path.read_text(encoding="utf-8"))
                _fig_desc_lines = []
                for fp in _fp_data:
                    fname = Path(fp.get("output_path", "")).name if fp.get("output_path") else ""
                    desc = fp.get("caption", fp.get("raw_prompt", ""))
                    ftype = fp.get("figure_type", "")
                    section = fp.get("section", "")
                    if fname and desc:
                        _fig_desc_lines.append(
                            f"- {fname}: [{ftype}] for {section} section — {desc}"
                        )
                if _fig_desc_lines:
                    _fig_descriptions = (
                        "\n\n## Figure descriptions (use these to place figures correctly):\n"
                        + chr(10).join(_fig_desc_lines)
                    )
            except Exception:
                pass

        _user_prompt = (
            f"## Available figure files in figures/ directory:\n"
            f"{chr(10).join('- ' + f for f in _fig_files) if _fig_files else '(none)'}\n"
            f"{_fig_descriptions}\n\n"
            f"## Available BibTeX citation keys:\n"
            f"{chr(10).join('- ' + k for k in _bib_keys[:50]) if _bib_keys else '(none)'}\n\n"
            f"## Markdown paper to convert:\n\n{paper_md}\n"
        )

        try:
            resp = _chat_with_prompt(
                llm,
                _system_prompt,
                _user_prompt,
                max_tokens=32768,
            )
            raw_output = resp.content

            # Extract .tex from ```latex ... ``` block
            tex_content = ""
            if "```latex" in raw_output:
                tex_content = raw_output.split("```latex", 1)[1].split("```", 1)[0].strip()
            elif "```tex" in raw_output:
                tex_content = raw_output.split("```tex", 1)[1].split("```", 1)[0].strip()
            elif "```" in raw_output and "\\documentclass" in raw_output:
                tex_content = raw_output.split("```", 1)[1].split("```", 1)[0].strip()
            else:
                tex_content = raw_output

            # Extract .bib from ```bib ... ``` block (if LLM generated one)
            _llm_bib = ""
            if "```bib" in raw_output:
                _llm_bib = raw_output.split("```bib", 1)[1].split("```", 1)[0].strip()
            elif "```bibtex" in raw_output:
                _llm_bib = raw_output.split("```bibtex", 1)[1].split("```", 1)[0].strip()
            if _llm_bib and "@" in _llm_bib:
                bib_text = _llm_bib
                logger.info("LaTeX package: Using LLM-generated bib (%d entries)",
                            _llm_bib.count("@"))

            # Validate
            if "\\documentclass" not in tex_content:
                raise ValueError("LLM output missing \\documentclass")
            if "\\end{document}" not in tex_content:
                tex_content += "\n\n\\end{document}\n"

            logger.info("LaTeX package: LLM conversion successful (%d chars)", len(tex_content))
        except Exception as e:
            logger.warning("LaTeX package: LLM conversion failed (%s), using fallback", e)
            llm = None  # Fall through to fallback

    if not tex_content:
        # Fallback: minimal wrapping
        _title_match = re.search(r"^#\s+(.+)$", paper_md, re.MULTILINE)
        _title = _title_match.group(1) if _title_match else "Research Paper"
        _fallback_class = "\\documentclass[conference]{IEEEtran}\n" if _is_ieee else "\\documentclass{article}\n\\usepackage[preprint]{neurips_2025}\n"
        _fallback_bib_style = "IEEEtran" if _is_ieee else "plainnat"
        tex_content = (
            _fallback_class
            +
            "\\usepackage[utf8]{inputenc}\n\\usepackage[T1]{fontenc}\n"
            "\\usepackage{hyperref}\n\\usepackage{url}\n"
            "\\usepackage{booktabs}\n\\usepackage{amsfonts}\n"
            "\\usepackage{amsmath}\n\\usepackage{graphicx}\n"
            "\\usepackage{natbib}\n\\usepackage{microtype}\n\n"
            f"\\title{{{_title}}}\n"
            "\\author{{Anonymous}}\n\n"
            "\\begin{{document}}\n\\maketitle\n\n"
            "% LLM conversion failed. Paper content needs manual conversion.\n"
            "% See paper_revised.md for the original markdown.\n\n"
            f"\\bibliographystyle{{{_fallback_bib_style}}}\n"
            "\\bibliography{{references}}\n\n"
            "\\end{{document}}\n"
        )

    (pkg_dir / "main.tex").write_text(tex_content, encoding="utf-8")
    (pkg_dir / "references.bib").write_text(bib_text, encoding="utf-8")

    # 4. Copy style files
    try:
        from researchclaw.templates import get_template
        _export_tpl = get_template(_target_conference or "neurips_2025")
        for _sty_file in _export_tpl.get_style_files():
            _shutil_lp.copy2(_sty_file, pkg_dir / _sty_file.name)
    except Exception:
        pass

    # 5. Create zip archive
    zip_path = stage_dir / "latex_package"
    _shutil_lp.make_archive(str(zip_path), "zip", str(pkg_dir))

    return ["latex_package.zip", "latex_package/"]
