"""Ideation ranking components."""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, Sequence

from researchclaw.pipeline.ideation.text import (
    _clamp_idea_count,
    _idea_like_blocks,
    _idea_text_similarity,
    _idea_title_slug,
    _renumber_idea_blocks,
)

logger = logging.getLogger(__name__)


def _score_idea_block(title: str, block: str, all_blocks: Sequence[tuple[str, str]]) -> dict[str, Any]:
    text = f"{title}\n{block}"
    lower = text.lower()
    novelty_terms = ("novel", "new", "gap", "different", "区别", "差异", "空缺", "创新", "未解决", "迁移")
    feasibility_terms = ("mvp", "dataset", "baseline", "metric", "数据", "指标", "两周", "baseline", "消融", "预算")
    risk_terms = ("risk", "fail", "fallback", "风险", "失败", "反例", "no-go", "早停")
    grounding_terms = ("paper", "et al", "arxiv", "doi", "文献", "论文", "方法", "工作")
    compute_terms = ("gpu", "cpu", "小时", "预算", "单卡", "batch", "epoch", "compute")

    def score_terms(terms: tuple[str, ...], base: int = 2) -> int:
        hits = sum(1 for term in terms if term in lower or term in text)
        return max(1, min(5, base + hits))

    novelty = score_terms(novelty_terms, 2)
    feasibility = score_terms(feasibility_terms, 2)
    impact = 3 + (1 if any(k in lower or k in text for k in ("impact", "贡献", "价值", "提升", "降低", "鲁棒")) else 0)
    testability = score_terms(("experiment", "ablation", "threshold", "实验", "消融", "阈值", "验证", "可证伪"), 2)
    grounding = score_terms(grounding_terms, 1)
    risk = score_terms(risk_terms, 1)
    compute = score_terms(compute_terms, 2)
    max_sim = 0.0
    for other_title, other_block in all_blocks:
        if other_block == block:
            continue
        max_sim = max(max_sim, _idea_text_similarity(block, other_block), _idea_text_similarity(title, other_title))
    diversity = 5 if max_sim < 0.22 else 4 if max_sim < 0.34 else 3 if max_sim < 0.46 else 2 if max_sim < 0.60 else 1
    overall = round((novelty + feasibility + impact + testability + grounding + risk + compute + diversity) / 8, 2)
    return {
        "title": _idea_title_slug(title),
        "novelty": novelty,
        "feasibility": feasibility,
        "impact": min(5, impact),
        "testability": testability,
        "literature_grounding": grounding,
        "risk": risk,
        "compute_cost": compute,
        "diversity": diversity,
        "max_similarity": round(max_sim, 3),
        "overall": overall,
    }


def _run_idea_tournament(candidate_ideas_md: str, *, idea_count: int) -> tuple[dict[str, Any], str, str]:
    blocks = _idea_like_blocks(candidate_ideas_md)
    if not blocks:
        blocks = [("兜底 Idea", candidate_ideas_md.strip())] if candidate_ideas_md.strip() else []
    rows: list[dict[str, Any]] = []
    ratings = [1500.0 for _ in blocks]
    scores = [_score_idea_block(title, block, blocks) for title, block in blocks]
    matches: list[dict[str, Any]] = []
    k_factor = 32
    for i in range(len(blocks)):
        for j in range(i + 1, len(blocks)):
            left = scores[i]
            right = scores[j]
            left_signal = float(left["overall"]) + 0.10 * float(left["diversity"]) + 0.08 * float(left["testability"])
            right_signal = float(right["overall"]) + 0.10 * float(right["diversity"]) + 0.08 * float(right["testability"])
            if abs(left_signal - right_signal) < 0.05:
                outcome_i = 0.5
                winner = "draw"
            elif left_signal > right_signal:
                outcome_i = 1.0
                winner = left["title"]
            else:
                outcome_i = 0.0
                winner = right["title"]
            expected_i = 1 / (1 + 10 ** ((ratings[j] - ratings[i]) / 400))
            delta = k_factor * (outcome_i - expected_i)
            ratings[i] += delta
            ratings[j] -= delta
            matches.append({
                "left": left["title"],
                "right": right["title"],
                "winner": winner,
                "left_signal": round(left_signal, 3),
                "right_signal": round(right_signal, 3),
            })
    for idx, ((title, block), score, rating) in enumerate(zip(blocks, scores, ratings), start=1):
        row = dict(score)
        row.update({"candidate_id": f"C{idx}", "elo": round(rating, 1), "block": block})
        rows.append(row)
    rows.sort(key=lambda r: (float(r.get("elo", 0)), float(r.get("overall", 0)), float(r.get("diversity", 0))), reverse=True)
    selected_blocks: list[tuple[str, str]] = []
    for row in rows:
        if len(selected_blocks) >= _clamp_idea_count(idea_count):
            break
        block = str(row.get("block", ""))
        title = str(row.get("title", ""))
        if any(_idea_text_similarity(block, existing) >= 0.50 for _, existing in selected_blocks):
            continue
        selected_blocks.append((title, block))
    if len(selected_blocks) < _clamp_idea_count(idea_count):
        for row in rows:
            if len(selected_blocks) >= _clamp_idea_count(idea_count):
                break
            block = str(row.get("block", ""))
            title = str(row.get("title", ""))
            if not any(block == existing for _, existing in selected_blocks):
                selected_blocks.append((title, block))
    selected_md = _renumber_idea_blocks(selected_blocks) if selected_blocks else candidate_ideas_md
    public_rows = [{k: v for k, v in row.items() if k != "block"} for row in rows]
    report = {
        "method": "local_pairwise_elo",
        "candidate_count": len(blocks),
        "selected_count": len(selected_blocks),
        "ranking": public_rows,
        "matches": matches,
    }
    md_lines = [
        "# Idea Tournament",
        "",
        "Method: local pairwise Elo over novelty/feasibility/impact/testability/grounding/risk/compute/diversity heuristics.",
        "",
        "| Rank | Candidate | Elo | Overall | Novelty | Feasibility | Impact | Testability | Grounding | Risk | Compute | Diversity | Max Similarity |",
        "|---:|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for rank, row in enumerate(public_rows, start=1):
        md_lines.append(
            f"| {rank} | {row.get('title', '')} | {row.get('elo', '')} | {row.get('overall', '')} | "
            f"{row.get('novelty', '')} | {row.get('feasibility', '')} | {row.get('impact', '')} | "
            f"{row.get('testability', '')} | {row.get('literature_grounding', '')} | {row.get('risk', '')} | "
            f"{row.get('compute_cost', '')} | {row.get('diversity', '')} | {row.get('max_similarity', '')} |"
        )
    md_lines.extend(["", "## Selected Ideas", "", selected_md.strip()])
    return report, "\n".join(md_lines).strip() + "\n", selected_md


def _write_idea_decision_table(stage_dir: Path, core_ideas_md: str, tournament_report: dict[str, Any]) -> str:
    blocks = _idea_like_blocks(core_ideas_md)
    scores = [_score_idea_block(title, block, blocks) for title, block in blocks]
    tournament_by_title: dict[str, dict[str, Any]] = {}
    ranking = tournament_report.get("ranking", []) if isinstance(tournament_report, dict) else []
    if isinstance(ranking, list):
        for row in ranking:
            if isinstance(row, dict):
                tournament_by_title[str(row.get("title", ""))] = row
    lines = [
        "# Idea Decision Table",
        "",
        "| Idea | Novelty | Feasibility | Risk | Compute | Evidence | Diversity | Tournament Elo | Next Step |",
        "|---|---:|---:|---:|---:|---:|---:|---:|---|",
    ]
    for idx, ((title, _block), score) in enumerate(zip(blocks, scores), start=1):
        trow = tournament_by_title.get(score["title"], {})
        risk_inverse = max(1, 6 - int(score.get("risk", 3) or 3))
        next_step = "进入实验设计" if float(score.get("overall", 0)) >= 3.2 and int(score.get("diversity", 0)) >= 3 else "先补文献/去重后再进入实验"
        lines.append(
            f"| Idea {idx}: {score['title']} | {score.get('novelty')} | {score.get('feasibility')} | "
            f"{risk_inverse} | {score.get('compute_cost')} | {score.get('literature_grounding')} | "
            f"{score.get('diversity')} | {trow.get('elo', '')} | {next_step} |"
        )
    text = "\n".join(lines).strip() + "\n"
    (stage_dir / "idea_decision_table.md").write_text(text, encoding="utf-8")
    return text
