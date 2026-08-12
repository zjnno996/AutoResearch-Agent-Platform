from researchclaw.templates.compiler import fix_common_latex_errors


def test_unicode_latex_error_sanitizes_all_unsupported_runs_at_once():
    tex = (
        "\\begin{document}\n"
        "Research topic: 你是实验室的研究员。 Efficient inference – café.\n"
        "\\end{document}\n"
    )

    fixed, fixes = fix_common_latex_errors(
        tex,
        ["! LaTeX Error: Unicode character 你 (U+4F60)"],
    )

    assert "你" not in fixed
    assert "[non-English text omitted]" in fixed
    assert "cafe" in fixed
    assert all(ord(char) < 128 for char in fixed)
    assert fixes == [
        "Replaced unsupported Unicode runs with pdflatex-safe text",
    ]
