"""PDF text extraction, caching, and vision-based content extraction."""

from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from io import BytesIO
from pathlib import Path
from typing import Any

from .dimensions import (
    MAX_CACHE_ENTRIES,
    MAX_FILE_SIZE_BYTES,
    MAX_TEXT_LENGTH,
    MAX_VISION_PAGES,
    TOKEN_ESTIMATE_CHARS_PER_TOKEN,
    TOKEN_ESTIMATE_VISION_PER_IMAGE,
    VISION_DPI,
    VISION_EXTRACT_PROMPT,
    VISION_MODELS,
    DIMENSION_SECTION_MAP,
    SECTION_PATTERNS,
    needs_full_text,
)

ROOT_DIR = Path(__file__).resolve().parents[2]
CACHE_DIR = ROOT_DIR / "backend" / "review_cache"
TEXT_CACHE_DIR = CACHE_DIR / "text"
VISION_CACHE_DIR = CACHE_DIR / "vision"

# ---------------------------------------------------------------------------
# Cache
# ---------------------------------------------------------------------------

_vision_extraction_cache: dict[str, str] = {}
VISION_CACHE_VERSION = "v3-all-figures"
VISION_EXTRACTION_WORKERS = 6


def _ensure_cache_dirs():
    TEXT_CACHE_DIR.mkdir(parents=True, exist_ok=True)
    VISION_CACHE_DIR.mkdir(parents=True, exist_ok=True)


def _cache_get(cache_dir: Path, key: str) -> str | None:
    path = cache_dir / f"{key}.json"
    if not path.exists():
        return None
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f).get("value")
    except Exception:
        return None


def _cache_set(cache_dir: Path, key: str, value: str):
    _ensure_cache_dirs()
    path = cache_dir / f"{key}.json"
    try:
        with open(path, "w", encoding="utf-8") as f:
            json.dump({"key": key, "value": value}, f, ensure_ascii=False)
    except Exception:
        return
    try:
        entries = sorted(cache_dir.glob("*.json"), key=lambda p: p.stat().st_mtime)
        while len(entries) > MAX_CACHE_ENTRIES:
            entries[0].unlink()
            entries = entries[1:]
    except Exception:
        pass


def _pdf_content_hash(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


# ---------------------------------------------------------------------------
# Page selection
# ---------------------------------------------------------------------------

_VISUAL_CAPTION_RE = re.compile(
    r"(?:图|表)\s*\d+(?:[.\-]\d+)*"
    r"|(?:figure|fig\.?|table)\s*\d+(?:[.\-]\d+)*",
    re.IGNORECASE,
)
_VISUAL_CAPTION_START_RE = re.compile(
    r"^\s*(?:(?:图|表)\s*\d+(?:[.\-]\d+)*|(?:figure|fig\.?|table)\s*\d+(?:[.\-]\d+)*)\s*[:：.]?",
    re.IGNORECASE,
)
_HIGH_VALUE_PAGE_RE = re.compile(
    r"实验(?:设置|结果|评估|分析)?|结果(?:分析)?|性能(?:评估|分析)|消融"
    r"|experiment(?:al)?|evaluation|results?|ablation|benchmark",
    re.IGNORECASE,
)
_METHOD_PAGE_RE = re.compile(
    r"方法|算法|系统设计|系统实现|总体架构|框架"
    r"|method|algorithm|system design|implementation|architecture|framework",
    re.IGNORECASE,
)
_FRONT_MATTER_RE = re.compile(
    r"分类号|单位代码|学位论文独创性声明|致谢|攻读学位期间|"
    r"培养方案|申请学位|图目录|表目录|^目录$|table of contents|acknowledg",
    re.IGNORECASE,
)
_REFERENCE_RE = re.compile(r"^\s*(?:参考文献|references|bibliography)\s*$", re.IGNORECASE)


def _page_importance_score(
    text: str,
    image_count: int,
    page_num: int,
    total_pages: int,
) -> float:
    """Score a PDF page for visual-review value."""
    compact = re.sub(r"\s+", " ", text).strip()
    score = min(image_count, 4) * 12.0
    score += min(len(_VISUAL_CAPTION_RE.findall(compact)), 6) * 7.0
    if _HIGH_VALUE_PAGE_RE.search(compact):
        score += 15.0
    if _METHOD_PAGE_RE.search(compact):
        score += 8.0
    if _FRONT_MATTER_RE.search(compact[:500]):
        score -= 30.0
    # Figure/table lists often contain many captions but no actual visual evidence.
    if len(re.findall(r"\.{5,}", compact)) >= 3:
        score -= 35.0
    if _REFERENCE_RE.search(compact[:100]):
        score -= 20.0
    if len(compact) < 50:
        score += 8.0 if image_count else -20.0
    if total_pages > 1:
        score += page_num / (total_pages - 1) * 0.01
    return score


def _select_diverse_pages(
    candidates: list[tuple[int, float]],
    total_pages: int,
    max_pages: int,
) -> list[int]:
    """Select high-scoring pages while preserving coverage across the paper."""
    if not candidates or max_pages <= 0:
        return []

    max_pages = min(max_pages, len(candidates))
    selected: set[int] = set()
    bucket_count = min(max_pages, 4)
    for bucket in range(bucket_count):
        start = total_pages * bucket / bucket_count
        end = total_pages * (bucket + 1) / bucket_count
        bucket_items = [item for item in candidates if start <= item[0] < end]
        if not bucket_items:
            continue
        page_num, score = max(bucket_items, key=lambda item: item[1])
        if score > 0:
            selected.add(page_num)

    remaining = list(candidates)
    while len(selected) < max_pages and remaining:
        def adjusted(item: tuple[int, float]) -> float:
            page_num, score = item
            if page_num in selected:
                return float("-inf")
            nearest = min((abs(page_num - selected_page) for selected_page in selected), default=999)
            return score - (8.0 if nearest <= 1 else 3.0 if nearest <= 3 else 0.0)

        best = max(remaining, key=adjusted)
        remaining.remove(best)
        if best[0] not in selected:
            selected.add(best[0])

    return sorted(selected)


def _get_important_pages(raw: bytes, file_name: str = "",
                         max_pages: int = MAX_VISION_PAGES) -> list[int]:
    """Rank visual-evidence pages across the full PDF."""
    is_pdf = file_name.lower().endswith(".pdf") or raw[:4] == b"%PDF"
    if not is_pdf:
        return []
    try:
        import fitz
        doc = fitz.open(stream=raw, filetype="pdf")
        total = len(doc)
        ref_start = int(total * 0.82)
        candidates: list[tuple[int, float]] = []

        for page_num in range(total):
            page = doc[page_num]
            text = page.get_text().strip()
            image_count = len(page.get_images(full=True))
            compact = re.sub(r"\s+", " ", text).strip()

            if page_num >= ref_start:
                prefix = compact[:200].lower()
                if "references" in prefix or "bibliography" in prefix or "参考文献" in prefix:
                    continue
                if prefix and any(prefix.startswith(kw) for kw in ["[1]", "[1)", "[1 ", "[1."]):
                    continue

            if len(compact) < 50 and not image_count:
                continue
            candidates.append((
                page_num,
                _page_importance_score(text, image_count, page_num, total),
            ))

        selected = _select_diverse_pages(candidates, total, max_pages)
        doc.close()
        return selected
    except ImportError:
        return list(range(min(3, max_pages)))
    except Exception:
        return list(range(min(3, max_pages)))


def _meaningful_image_rects(page: Any) -> list[Any]:
    """Return non-trivial embedded image regions, excluding tiny logos/icons."""
    rects: list[Any] = []
    seen: set[tuple[int, int, int, int]] = set()
    try:
        page_area = max(float(page.rect.get_area()), 1.0)
        for image in page.get_images(full=True):
            xref = int(image[0])
            for rect in page.get_image_rects(xref):
                if rect.width < 60 or rect.height < 45:
                    continue
                if float(rect.get_area()) / page_area < 0.018:
                    continue
                key = tuple(round(float(value)) for value in (rect.x0, rect.y0, rect.x1, rect.y1))
                if key not in seen:
                    seen.add(key)
                    rects.append(rect)
    except Exception:
        return []
    return rects


def _visual_caption_blocks(page: Any) -> list[tuple[Any, str]]:
    blocks: list[tuple[Any, str]] = []
    try:
        for block in page.get_text("blocks"):
            text = re.sub(r"\s+", " ", str(block[4])).strip()
            if not _VISUAL_CAPTION_START_RE.search(text):
                continue
            # Exclude figure/table lists rather than treating them as evidence.
            if len(re.findall(r"\.{5,}", text)) >= 2:
                continue
            blocks.append((block, text))
    except Exception:
        return []
    return blocks


def _get_all_visual_inventory(raw: bytes, file_name: str = "") -> list[dict[str, int]]:
    """Inventory every content page containing a figure, table, or large image."""
    is_pdf = file_name.lower().endswith(".pdf") or raw[:4] == b"%PDF"
    if not is_pdf:
        return []
    try:
        import fitz
        doc = fitz.open(stream=raw, filetype="pdf")
        total = len(doc)
        inventory: list[dict[str, int]] = []
        for page_num in range(total):
            page = doc[page_num]
            compact = re.sub(r"\s+", " ", page.get_text()).strip()
            prefix = compact[:300].lower()
            if _REFERENCE_RE.search(prefix) or prefix.startswith(("references", "bibliography", "参考文献")):
                continue
            if _FRONT_MATTER_RE.search(prefix) and not _HIGH_VALUE_PAGE_RE.search(compact):
                continue
            if len(re.findall(r"\.{5,}", compact)) >= 3:
                continue
            captions = _visual_caption_blocks(page)
            image_rects = _meaningful_image_rects(page)
            if not captions and not image_rects:
                continue
            inventory.append({
                "page": page_num,
                "captions": len(captions),
                "images": len(image_rects),
                "regions": len(_find_visual_clips(page)),
            })
        doc.close()
        return inventory
    except Exception:
        return []


def _get_all_visual_pages(raw: bytes, file_name: str = "") -> list[int]:
    return [item["page"] for item in _get_all_visual_inventory(raw, file_name)]


def _render_pages_as_images(raw: bytes, page_numbers: list[int],
                            dpi: int = VISION_DPI) -> list[str]:
    """Render selected PDF pages as base64 PNG images."""
    try:
        import fitz
        doc = fitz.open(stream=raw, filetype="pdf")
        images: list[str] = []
        for pn in page_numbers:
            page = doc[pn]
            pix = page.get_pixmap(dpi=dpi)
            images.append(base64.b64encode(pix.tobytes("png")).decode("ascii"))
        doc.close()
        return images
    except Exception:
        return []


def _get_paper_images(file_base64: str, file_name: str = "",
                      max_pages: int = MAX_VISION_PAGES) -> list[str]:
    """Extract important PDF pages as base64 PNG images for multi-modal review.

    Includes every detected figure/table page while skipping front matter,
    figure lists, references, tiny logos, and blank pages.
    """
    try:
        raw = base64.b64decode(file_base64)
    except Exception:
        return []
    pages = _get_all_visual_pages(raw, file_name)
    if not pages:
        return []
    return _render_pages_as_images(raw, pages)


# ---------------------------------------------------------------------------
# Text extraction from PDF
# ---------------------------------------------------------------------------

def _extract_pdf_text(raw: bytes) -> str:
    """Extract text from PDF using multiple backends in parallel.
    Returns the best (longest) result.
    """
    results: dict[str, str] = {}

    def _try_pypdf2():
        try:
            import PyPDF2
            reader = PyPDF2.PdfReader(BytesIO(raw))
            text = "\n".join(page.extract_text() or "" for page in reader.pages)
            results["pypdf2"] = text
        except Exception:
            pass

    def _try_pdfminer():
        try:
            from pdfminer.high_level import extract_text as pdfminer_extract
            text = pdfminer_extract(BytesIO(raw))
            results["pdfminer"] = text
        except Exception:
            pass

    with ThreadPoolExecutor(max_workers=2) as pool:
        pool.submit(_try_pypdf2)
        pool.submit(_try_pdfminer)

    best_text = ""
    for _, text in results.items():
        if text.strip() and len(text.strip()) > len(best_text):
            best_text = text

    if best_text.strip():
        best_text = re.sub(r"\n\d+\n(?=\s*\n)", "\n", best_text)
        best_text = re.sub(r"\n{3,}", "\n\n", best_text)
        best_text = re.sub(r"(\w)-\n(\w)", r"\1\2", best_text)
        lines = best_text.split("\n")
        cleaned = []
        for line in lines:
            stripped = line.strip()
            if re.match(r"^\d+$", stripped) and len(stripped) <= 4:
                continue
            if re.match(r"^arXiv:\d{4}\.\d+", stripped):
                cleaned.append(stripped)
                continue
            if len(stripped) < 3 and stripped:
                continue
            cleaned.append(line)
        best_text = "\n".join(cleaned)

    return best_text


def _get_paper_text(file_base64: str, file_name: str = "") -> str:
    """Extract text from base64-encoded PDF or plain text with caching."""
    try:
        raw = base64.b64decode(file_base64)
    except Exception:
        return file_base64

    if len(raw) > MAX_FILE_SIZE_BYTES:
        return ""
    is_pdf = file_name.lower().endswith(".pdf") or raw[:4] == b"%PDF"
    if is_pdf:
        content_hash = _pdf_content_hash(raw)
        cached = _cache_get(TEXT_CACHE_DIR, content_hash)
        if cached:
            return cached[:MAX_TEXT_LENGTH]
        text = _extract_pdf_text(raw)
        if text.strip():
            _cache_set(TEXT_CACHE_DIR, content_hash, text)
        return text[:MAX_TEXT_LENGTH]

    try:
        return raw.decode("utf-8")[:MAX_TEXT_LENGTH]
    except Exception:
        return raw.decode("utf-8", errors="ignore")[:MAX_TEXT_LENGTH]


def _check_extraction_quality(text: str, raw: bytes | None = None) -> dict[str, Any]:
    """Assess quality of extracted text."""
    text = text.strip()
    word_count = len(text.split())
    has_abstract = bool(re.search(r"abstract", text[:3000], re.IGNORECASE))
    has_sections = bool(re.search(r"(?:introduction|method|experiment|conclusion)",
                                   text[:10000], re.IGNORECASE))
    avg_line_len = sum(len(l) for l in text.split("\n") if l.strip()) / max(
        len([l for l in text.split("\n") if l.strip()]), 1)

    issues = []
    if word_count < 100:
        issues.append("Very little text extracted")
    if not has_abstract:
        issues.append("Abstract not detected")
    if not has_sections:
        issues.append("No section headers detected")
    if avg_line_len < 20 and word_count > 50:
        issues.append("Text appears fragmented (short lines)")

    quality = "good"
    if word_count < 50:
        quality = "very_low"
    elif word_count < 200 or len(issues) >= 2:
        quality = "low"

    return {
        "word_count": word_count,
        "char_count": len(text),
        "has_abstract": has_abstract,
        "has_sections": has_sections,
        "avg_line_length": round(avg_line_len, 1),
        "quality": quality,
        "issues": issues,
    }


# ---------------------------------------------------------------------------
# Section parsing
# ---------------------------------------------------------------------------

_NUMBERED_HEADING_RE = re.compile(
    r"^(?:第\s*[一二三四五六七八九十百0-9]+\s*章|"
    r"\d+(?:\.\d+){0,3})[\s　、.．:：-]*(.+?)$"
)


def _classify_section_header(line: str) -> str | None:
    """Map English and numbered Chinese headings to semantic section types."""
    stripped = re.sub(r"\s+", " ", line).strip()
    if not stripped or len(stripped) > 100:
        return None
    for name, pattern in SECTION_PATTERNS:
        if re.search(pattern, stripped, re.IGNORECASE):
            return name

    match = _NUMBERED_HEADING_RE.match(stripped)
    title = match.group(1).strip() if match else stripped
    rules: list[tuple[str, tuple[str, ...]]] = [
        ("references", ("参考文献", "references", "bibliography")),
        ("related_work", ("相关工作", "研究现状", "文献综述", "related work", "prior work")),
        ("introduction", ("绪论", "引言", "研究背景", "introduction")),
        ("theory", ("理论分析", "理论基础", "收敛性", "复杂度", "最优性")),
        ("simulation", ("仿真实验", "性能评估", "性能分析", "实验分析")),
        ("experiment", ("实验", "评测", "evaluation", "benchmark")),
        ("results", ("结果分析", "实验结果", "results", "analysis")),
        ("system_design", ("系统设计", "系统实现", "总体架构", "平台设计")),
        ("method", ("方法", "算法", "模型", "框架", "机制", "method", "approach", "framework")),
        ("discussion", ("总结", "结论", "展望", "讨论", "conclusion", "discussion")),
    ]
    lower_title = title.lower()
    for name, keywords in rules:
        if any(keyword.lower() in lower_title for keyword in keywords):
            return name
    return None


def _parse_sections(text: str) -> dict[str, str]:
    """Split text into semantic sections without dropping repeated chapters."""
    lines = text.split("\n")
    section_starts: list[tuple[int, str]] = [(-1, "preamble")]

    for index, line in enumerate(lines):
        section_name = _classify_section_header(line)
        if section_name:
            section_starts.append((index, section_name))

    section_starts.append((len(lines), "eof"))
    section_parts: dict[str, list[str]] = {}
    for index in range(len(section_starts) - 1):
        start_line = section_starts[index][0]
        end_line = section_starts[index + 1][0]
        section_name = section_starts[index][1]
        section_lines = lines[start_line:end_line] if start_line >= 0 else lines[:end_line]
        section_text = "\n".join(section_lines).strip()
        if section_text:
            section_parts.setdefault(section_name, []).append(section_text)

    return {
        name: "\n\n--- repeated section ---\n\n".join(parts)
        for name, parts in section_parts.items()
    }


def _get_text_for_dimension(paper_text: str, dim_id: str,
                            max_chars: int = 50000) -> str:
    """Return paper sections relevant to a review dimension."""
    sections = _parse_sections(paper_text)
    relevant = DIMENSION_SECTION_MAP.get(dim_id, ["preamble"])
    parts: list[str] = []
    for sec_name in relevant:
        if sec_name in sections and sections[sec_name]:
            parts.append(sections[sec_name])
    if not parts:
        return paper_text[:max_chars]
    combined = "\n\n---\n\n".join(parts)
    return combined[:max_chars]


def _build_dimension_text(paper_text: str, dim_id: str,
                          max_chars: int = 200000,
                          compress_irrelevant: bool = True,
                          compression_header_len: int = 120) -> str:
    """Build paper text for a dimension with smart compression."""
    if needs_full_text(dim_id):
        return paper_text[:max_chars]

    sections = _parse_sections(paper_text)
    if not sections or "preamble" in sections and len(sections) <= 2:
        return paper_text[:max_chars]

    relevant = DIMENSION_SECTION_MAP.get(dim_id, ["preamble"])
    relevant_set = set(relevant)
    parts: list[str] = []
    for sec_name in ["preamble", "abstract", "introduction", "related_work",
                      "method", "system_design", "theory", "experiment",
                      "simulation", "results", "discussion", "references"]:
        if sec_name not in sections or not sections[sec_name]:
            continue
        if not compress_irrelevant or sec_name in relevant_set:
            parts.append(sections[sec_name])
        else:
            sec_text = sections[sec_name]
            lines = sec_text.strip().split("\n")
            header = lines[0][:80] if lines else sec_name
            body_preview = " ".join(lines[1:]) if len(lines) > 1 else ""
            body_preview = re.sub(r"\s+", " ", body_preview).strip()[:compression_header_len]
            parts.append(f"[{header}]  {body_preview}…" if body_preview else f"[{header}]")

    if not parts:
        return paper_text[:max_chars]
    combined = "\n\n".join(parts)
    return combined[:max_chars]


def _build_group_text(paper_text: str, dim_ids: list[str],
                      max_chars: int = 200000,
                      compress_irrelevant: bool = True) -> str:
    """Build paper text for a group of dimensions."""
    if not compress_irrelevant:
        return paper_text[:max_chars]
    if any(needs_full_text(did) for did in dim_ids):
        return paper_text[:max_chars]

    sections = _parse_sections(paper_text)
    if not sections or "preamble" in sections and len(sections) <= 2:
        return paper_text[:max_chars]

    relevant_set: set[str] = set()
    for dim_id in dim_ids:
        for s in DIMENSION_SECTION_MAP.get(dim_id, []):
            relevant_set.add(s)

    parts: list[str] = []
    for sec_name in ["preamble", "abstract", "introduction", "related_work",
                      "method", "system_design", "theory", "experiment",
                      "simulation", "results", "discussion", "references"]:
        if sec_name not in sections or not sections[sec_name]:
            continue
        if sec_name in relevant_set:
            parts.append(sections[sec_name])
        else:
            lines = sections[sec_name].strip().split("\n")
            header = lines[0][:80] if lines else sec_name
            body_preview = " ".join(lines[1:]) if len(lines) > 1 else ""
            body_preview = re.sub(r"\s+", " ", body_preview).strip()[:120]
            parts.append(f"[{header}]  {body_preview}…" if body_preview else f"[{header}]")

    if not parts:
        return paper_text[:max_chars]
    combined = "\n\n".join(parts)
    return combined[:max_chars]


# ---------------------------------------------------------------------------
# Vision reader pipeline
# ---------------------------------------------------------------------------

def _find_vision_model() -> str | None:
    """Find the first configured vision-capable model name."""
    from .llm_client import get_model_options
    for opt in get_model_options():
        val = opt.get("value", "").lower()
        if any(vm in val for vm in VISION_MODELS):
            return opt["value"]
    return None


def _find_visual_clips(page: Any) -> list[tuple[Any | None, str]]:
    """Find every caption/image-centered visual region on a PDF page."""
    try:
        import fitz
        page_rect = page.rect
        height, width = page_rect.height, page_rect.width
        caption_blocks = sorted(_visual_caption_blocks(page), key=lambda item: float(item[0][1]))
        clips: list[tuple[Any, str]] = []

        for index, (block, label_text) in enumerate(caption_blocks):
            _, y0, _, y1 = map(float, block[:4])
            previous_bottom = float(caption_blocks[index - 1][0][3]) if index else 0.0
            next_top = float(caption_blocks[index + 1][0][1]) if index + 1 < len(caption_blocks) else height
            visual_kind = "table" if re.search(
                r"^(?:表|table)\s*\d", label_text, re.IGNORECASE
            ) else "figure"
            if visual_kind == "table":
                # Table captions usually precede the table body.
                clip = fitz.Rect(
                    width * 0.03,
                    max(previous_bottom, y0 - height * 0.04),
                    width * 0.97,
                    min(height, next_top, y1 + height * 0.48),
                )
            else:
                # Figure captions usually follow the visual. Bound the crop by
                # the previous caption so stacked figures remain separate.
                clip = fitz.Rect(
                    width * 0.03,
                    max(previous_bottom, y0 - height * 0.48),
                    width * 0.97,
                    min(height, y1 + height * 0.06),
                )
            clip &= page_rect
            if not clip.is_empty:
                clips.append((clip, f"{visual_kind} crop near: {label_text[:160]}"))

        # Include large embedded images that have no extractable caption.
        for rect in _meaningful_image_rects(page):
            expanded = fitz.Rect(
                max(0, rect.x0 - width * 0.03),
                max(0, rect.y0 - height * 0.03),
                min(width, rect.x1 + width * 0.03),
                min(height, rect.y1 + height * 0.03),
            )
            if any(float((expanded & existing).get_area()) / max(float(expanded.get_area()), 1.0) > 0.72 for existing, _ in clips):
                continue
            clips.append((expanded, "embedded figure/image region"))

        return clips or [(None, "full page")]
    except Exception:
        return [(None, "full page")]


def _find_visual_clip(page: Any) -> tuple[Any | None, str]:
    """Compatibility wrapper returning the first detected visual region."""
    return _find_visual_clips(page)[0]


def _vision_extract_page(raw: bytes, page_num: int, client: Any,
                         vision_model: str) -> str | None:
    """Extract all figure/table regions from one page in a single model call."""
    try:
        import fitz
        doc = fitz.open(stream=raw, filetype="pdf")
        page = doc[page_num]
        regions = _find_visual_clips(page)
        rendered: list[tuple[str, str]] = []
        for clip, region_label in regions:
            render_dpi = 160 if clip is not None else VISION_DPI
            pix = page.get_pixmap(dpi=render_dpi, clip=clip)
            rendered.append((
                region_label,
                base64.b64encode(pix.tobytes("png")).decode("ascii"),
            ))
        doc.close()
    except Exception:
        return None

    labels = "; ".join(f"visual {index + 1}: {label}" for index, (label, _) in enumerate(rendered))
    content_parts: list[dict[str, Any]] = [{
        "type": "text",
        "text": (
            f"Extract ALL visual evidence from page {page_num + 1} of this research paper. "
            f"There are {len(rendered)} supplied visual regions ({labels}). For each region, "
            "identify the figure/table number and title, transcribe axes/legends/important values, "
            "and state the evidence-supported takeaway. Do not omit a supplied visual. "
            "Output markdown with one ### Visual heading per region."
        ),
    }]
    content_parts.extend({
        "type": "image_url",
        "image_url": {"url": f"data:image/png;base64,{img_b64}"},
    } for _, img_b64 in rendered)

    try:
        resp = client.chat(
            messages=[{"role": "user", "content": content_parts}],
            system=VISION_EXTRACT_PROMPT,
            model=vision_model,
            max_tokens=min(8192, 2048 + 1536 * len(rendered)),
            temperature=0.1,
            json_mode=False,
        )
        return resp.content.strip()
    except Exception:
        return None


def _vision_extract_paper(raw: bytes, file_name: str) -> tuple[str, dict[str, Any]]:
    """Extract reusable evidence from every detected figure/table page."""
    token_usage: dict[str, Any] = {
        "vision_extraction_chars": 0,
        "vision_pages_sent": 0,
        "vision_images": 0,
        "vision_selected_pages": [],
        "vision_coverage_mode": "all_figures_tables",
        "vision_detected_regions": 0,
    }

    cache_key = f"{VISION_CACHE_VERSION}_{_pdf_content_hash(raw)}"
    def cached_result(value: str) -> tuple[str, dict[str, Any]]:
        pages = [int(page) for page in re.findall(r"--- Page (\d+) ---", value)]
        inventory = _get_all_visual_inventory(raw, file_name)
        expected_pages = [item["page"] + 1 for item in inventory]
        failed_pages = [page for page in expected_pages if page not in set(pages)]
        detected_regions = sum(item["regions"] for item in inventory)
        return value, {
            **token_usage,
            "vision_cached": 1,
            "vision_extraction_chars": len(value),
            "vision_pages_sent": len(pages),
            "vision_images": detected_regions,
            "vision_selected_pages": pages,
            "vision_detected_regions": detected_regions,
            "vision_expected_pages": len(expected_pages),
            "vision_failed_pages": failed_pages,
            "vision_coverage_complete": not failed_pages,
        }

    if cache_key in _vision_extraction_cache:
        return cached_result(_vision_extraction_cache[cache_key])
    fs_cached = _cache_get(VISION_CACHE_DIR, cache_key)
    if fs_cached:
        _vision_extraction_cache[cache_key] = fs_cached
        return cached_result(fs_cached)

    vision_model = _find_vision_model()
    if not vision_model:
        return "", token_usage

    visual_inventory = _get_all_visual_inventory(raw, file_name)
    page_numbers = [item["page"] for item in visual_inventory]
    if not page_numbers:
        return "", token_usage

    from .llm_client import get_client_for_model
    try:
        client = get_client_for_model(vision_model)
    except Exception:
        return "", token_usage

    detected_regions = sum(item["regions"] for item in visual_inventory)
    token_usage["vision_pages_sent"] = len(page_numbers)
    token_usage["vision_images"] = detected_regions
    token_usage["vision_detected_regions"] = detected_regions
    token_usage["vision_selected_pages"] = [pn + 1 for pn in page_numbers]

    # Keep page failures isolated while avoiding N sequential network calls.
    page_results: dict[int, str] = {}
    worker_count = min(VISION_EXTRACTION_WORKERS, len(page_numbers))
    with ThreadPoolExecutor(max_workers=worker_count) as pool:
        futures = {
            pool.submit(_vision_extract_page, raw, pn, client, vision_model): pn
            for pn in page_numbers
        }
        for future in as_completed(futures):
            pn = futures[future]
            try:
                text = future.result()
            except Exception:
                text = None
            if text:
                page_results[pn] = text

    failed_pages = [pn + 1 for pn in page_numbers if pn not in page_results]
    token_usage["vision_expected_pages"] = len(page_numbers)
    token_usage["vision_failed_pages"] = failed_pages
    token_usage["vision_coverage_complete"] = not failed_pages
    token_usage["vision_pages_sent"] = len(page_results)
    token_usage["vision_selected_pages"] = [pn + 1 for pn in page_numbers if pn in page_results]

    page_texts = [
        f"--- Page {pn + 1} ---\n{page_results[pn]}"
        for pn in page_numbers
        if pn in page_results
    ]
    if not page_texts:
        return "", token_usage

    extracted = "\n\n".join(page_texts)
    token_usage["vision_extraction_chars"] = len(extracted)

    _vision_extraction_cache[cache_key] = extracted
    _cache_set(VISION_CACHE_DIR, cache_key, extracted)
    return extracted, token_usage


# ---------------------------------------------------------------------------
# Token estimation
# ---------------------------------------------------------------------------

def _estimate_token_usage(
    text_path_str: str | None = None,
    text_path_len: int = 0,
    vision_path_pages: int = 0,
    vision_path_extraction_len: int = 0,
    num_dims: int = 7,
    review_mode: str = "hybrid",
) -> dict[str, Any]:
    """Compare token usage between text-only and vision-reader paths."""
    text_path_chars = text_path_len if text_path_str is None else len(text_path_str or "")
    text_tokens_per_dim = text_path_chars // TOKEN_ESTIMATE_CHARS_PER_TOKEN
    text_total = text_tokens_per_dim * num_dims + num_dims * 500

    if review_mode == "hybrid":
        group_total = text_path_chars // TOKEN_ESTIMATE_CHARS_PER_TOKEN * 3
        group_total += num_dims * 500 + 500
    elif review_mode == "batch":
        group_total = text_path_chars // TOKEN_ESTIMATE_CHARS_PER_TOKEN
        group_total += num_dims * 500 + 500
    else:
        group_total = text_total

    vision_tokens = vision_path_pages * TOKEN_ESTIMATE_VISION_PER_IMAGE
    extract_tokens = vision_path_extraction_len // TOKEN_ESTIMATE_CHARS_PER_TOKEN
    review_tokens = extract_tokens * num_dims + num_dims * 500
    vision_total = vision_tokens + review_tokens
    if group_total > vision_total:
        group_total = vision_total

    savings = text_total - group_total
    pct = round((savings / max(text_total, 1)) * 100, 1)

    return {
        "text_only_estimated_tokens": text_total,
        "vision_reader_estimated_tokens": group_total,
        "estimated_savings_tokens": max(savings, 0),
        "estimated_savings_percent": max(pct, 0),
        "review_mode": review_mode,
    }
