from __future__ import annotations

from review_engine.pdf_utils import _page_importance_score, _select_diverse_pages


def test_visual_page_scoring_prefers_experiment_figures_over_front_matter() -> None:
    front = _page_importance_score(
        "浙江大学研究生学位论文独创性声明 目录 致谢",
        image_count=0,
        page_num=2,
        total_pages=100,
    )
    experiment = _page_importance_score(
        "第六章 实验结果与分析 图6.3 吞吐量对比 表6.2 消融实验结果",
        image_count=0,
        page_num=70,
        total_pages=100,
    )
    assert experiment > front + 30


def test_visual_page_selection_spans_the_document() -> None:
    candidates = [
        (5, 10.0),
        (20, 30.0),
        (30, 25.0),
        (55, 20.0),
        (75, 22.0),
        (92, 18.0),
    ]
    selected = _select_diverse_pages(candidates, total_pages=100, max_pages=4)
    assert any(page < 25 for page in selected)
    assert any(25 <= page < 50 for page in selected)
    assert any(50 <= page < 75 for page in selected)
    assert any(page >= 75 for page in selected)


def test_vision_page_is_attached_as_png_data_url() -> None:
    import base64

    import fitz

    from review_engine.pdf_utils import _vision_extract_page

    doc = fitz.open()
    page = doc.new_page()
    page.insert_text((72, 72), "Figure 1: visual evidence")
    raw = doc.tobytes()
    doc.close()

    captured: dict = {}

    class Response:
        content = "visual evidence extracted"

    class FakeClient:
        def chat(self, **kwargs):
            captured.update(kwargs)
            return Response()

    result = _vision_extract_page(raw, 0, FakeClient(), "fake-vision-model")
    assert result == "visual evidence extracted"
    content = captured["messages"][0]["content"]
    image_url = content[1]["image_url"]["url"]
    assert image_url.startswith("data:image/png;base64,")
    png = base64.b64decode(image_url.split(",", 1)[1])
    assert png.startswith(b"\x89PNG\r\n\x1a\n")


def test_visual_evidence_survives_section_routing() -> None:
    from review_engine.reviewer import VISUAL_EVIDENCE_MARKER, _split_visual_evidence

    body, visual = _split_visual_evidence(
        "paper body" + VISUAL_EVIDENCE_MARKER + "--- Page 62 ---\nTable 6.2 values"
    )
    assert body == "paper body"
    assert "Page 62" in visual
    assert "Table 6.2 values" in visual


def test_all_visual_pages_and_multiple_regions_are_included() -> None:
    import fitz

    from review_engine.pdf_utils import _find_visual_clips, _get_all_visual_pages

    doc = fitz.open()
    front = doc.new_page()
    front.insert_text((72, 72), "图目录")
    front.insert_text((72, 100), "图1.1 ........ 10")
    visual = doc.new_page()
    visual.insert_text((72, 300), "Figure 1: System architecture")
    visual.insert_text((72, 650), "Table 1: Experimental results")
    plain = doc.new_page()
    plain.insert_text((72, 72), "This page has only ordinary paragraph text.")
    raw = doc.tobytes()
    doc.close()

    assert _get_all_visual_pages(raw, "paper.pdf") == [1]
    check = fitz.open(stream=raw, filetype="pdf")
    clips = _find_visual_clips(check[1])
    check.close()
    assert len(clips) == 2


def test_vision_call_contains_every_visual_region_on_page() -> None:
    import fitz

    from review_engine.pdf_utils import _vision_extract_page

    doc = fitz.open()
    page = doc.new_page()
    page.insert_text((72, 300), "Figure 1: Architecture")
    page.insert_text((72, 650), "Table 1: Results")
    raw = doc.tobytes()
    doc.close()
    captured: dict = {}

    class Response:
        content = "### Visual 1\nA\n### Visual 2\nB"

    class FakeClient:
        def chat(self, **kwargs):
            captured.update(kwargs)
            return Response()

    result = _vision_extract_page(raw, 0, FakeClient(), "fake-vision-model")
    assert result and "Visual 2" in result
    content = captured["messages"][0]["content"]
    assert len([part for part in content if part.get("type") == "image_url"]) == 2


def test_visual_evidence_compaction_keeps_late_pages() -> None:
    from review_engine.reviewer import VISUAL_EVIDENCE_MARKER, _split_visual_evidence

    visual = "\n\n".join(
        f"--- Page {page} ---\n### Visual 1\npage-{page}-evidence " + "x" * 500
        for page in range(1, 31)
    )
    _, compact = _split_visual_evidence("body" + VISUAL_EVIDENCE_MARKER + visual, max_chars=12000)
    assert "Page 1" in compact
    assert "Page 30" in compact
    assert "page-30-evidence" in compact


def test_evidence_map_retrieves_each_visual_on_multi_figure_pages() -> None:
    from review_engine.evidence_map import build_evidence_map

    visual = """--- Page 10 ---
### Visual 1
Figure 3.1 throughput rises by 20 percent.
### Visual 2
Figure 3.2 rare-tail-latency drops by 35 percent.
--- Page 90 ---
### Visual 1
Table 8.4 energy consumption increases by 4 percent.
"""
    evidence = build_evidence_map("摘要\n本文提出一个系统。", visual)
    visual_units = [unit for unit in evidence.units if unit.kind == "visual"]
    assert len(visual_units) == 3
    prompt = evidence.to_prompt(max_chars=6000, query="rare-tail-latency")
    assert "rare-tail-latency" in prompt
    assert "Page 90" in prompt  # all-page index is preserved


def test_full_visual_pipeline_reports_complete_coverage_with_fake_qwen() -> None:
    import re
    from unittest.mock import patch

    import fitz
    import review_engine.pdf_utils as pdf_utils

    doc = fitz.open()
    front = doc.new_page()
    front.insert_text((72, 72), "图目录")
    front.insert_text((72, 100), "图1.1 ........ 2")
    page2 = doc.new_page()
    page2.insert_text((72, 300), "Figure 1: Architecture")
    page2.insert_text((72, 650), "Table 1: Results")
    page3 = doc.new_page()
    page3.insert_text((72, 400), "Figure 2: Ablation study")
    raw = doc.tobytes()
    doc.close()
    calls: list[int] = []

    class Response:
        def __init__(self, content: str):
            self.content = content

    class FakeClient:
        def chat(self, **kwargs):
            prompt = kwargs["messages"][0]["content"][0]["text"]
            page = int(re.search(r"page (\d+)", prompt).group(1))
            image_count = sum(part.get("type") == "image_url" for part in kwargs["messages"][0]["content"])
            calls.append(page)
            visuals = "\n".join(f"### Visual {index + 1}\npage-{page}-visual-{index + 1}" for index in range(image_count))
            return Response(visuals)

    pdf_utils._vision_extraction_cache.clear()
    with patch.object(pdf_utils, "_find_vision_model", return_value="fake-qwen-vl"), patch(
        "review_engine.llm_client.get_client_for_model", return_value=FakeClient()
    ), patch.object(pdf_utils, "_cache_get", return_value=None), patch.object(
        pdf_utils, "_cache_set", return_value=None
    ):
        extracted, meta = pdf_utils._vision_extract_paper(raw, "coverage-test.pdf")

    assert sorted(calls) == [2, 3]
    assert meta["vision_coverage_mode"] == "all_figures_tables"
    assert meta["vision_coverage_complete"] is True
    assert meta["vision_failed_pages"] == []
    assert meta["vision_selected_pages"] == [2, 3]
    assert meta["vision_detected_regions"] == 3
    assert "--- Page 2 ---" in extracted and "--- Page 3 ---" in extracted
