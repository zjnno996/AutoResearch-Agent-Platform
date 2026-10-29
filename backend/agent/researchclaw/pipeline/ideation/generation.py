"""Literature-grounded ideation, reflection and final idea selection."""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path
import re
from typing import Any, Sequence

from researchclaw.adapters import AdapterBundle
from researchclaw.config import RCConfig
from researchclaw.llm.client import LLMClient
from researchclaw.pipeline.ideation.branches import (
    _generate_idea_mutation_branches,
    _synthesize_idea_mutation_branches,
)
from researchclaw.pipeline.ideation.memory import _write_ideation_memory_update
from researchclaw.pipeline.ideation.ranking import _run_idea_tournament, _write_idea_decision_table
from researchclaw.pipeline.ideation.roles import (
    _multi_perspective_generate,
    _synthesize_perspectives,
)
from researchclaw.pipeline.ideation.schema import _idea_quality_rubric, _idea_schema_instruction
from researchclaw.pipeline.ideation.text import (
    _add_yaml_frontmatter_to_core_ideas,
    _clamp_idea_count,
    _complete_idea_set,
    _configured_idea_count,
    _fallback_hypotheses_from_synthesis,
    _idea_count_rule,
    _idea_like_blocks,
    _idea_like_count,
    _idea_text_similarity,
    _idea_title_slug,
    _renumber_idea_blocks,
)
from researchclaw.pipeline.models import StageResult
from researchclaw.pipeline.stages import Stage, StageStatus
from researchclaw.pipeline.support.artifacts import (
    _load_hardware_profile,
    _parse_jsonl_rows,
    _read_prior_artifact,
    _utcnow_iso,
)
from researchclaw.pipeline.support.context import _get_evolution_overlay
from researchclaw.pipeline.support.research_memory import (
    _load_experiment_memory,
    _load_ideation_memory,
)
from researchclaw.pipeline.support.settings import _create_idea_judge_llm
from researchclaw.prompts import PromptManager

logger = logging.getLogger(__name__)


def _paper_authors_short(authors: Any, *, limit: int = 3) -> str:
    if isinstance(authors, list):
        names: list[str] = []
        for item in authors[:limit]:
            if isinstance(item, dict):
                name = str(item.get("name", "") or "").strip()
            else:
                name = str(item or "").strip()
            if name:
                names.append(name)
        if len(authors) > limit:
            names.append("等")
        return ", ".join(names)
    if isinstance(authors, str):
        return authors[:160]
    return ""


def _format_paper_for_idea_evidence(row: dict[str, Any], idx: int) -> str:
    title = str(row.get("title", "") or f"Paper {idx}").strip()
    authors = _paper_authors_short(row.get("authors", []))
    year = str(row.get("year", "") or "").strip()
    venue = str(row.get("venue", "") or row.get("journal", "") or row.get("source", "") or "").strip()
    abstract = str(row.get("abstract", "") or row.get("summary", "") or "").strip()
    reason = str(row.get("keep_reason", "") or row.get("relevance_reason", "") or "").strip()
    finding = str(row.get("findings", "") or row.get("key_finding", "") or "").strip()
    parts = [f"{idx}. {title}"]
    meta = "，".join(part for part in (authors, year, venue) if part)
    if meta:
        parts.append(f"   - 元信息：{meta}")
    if abstract:
        parts.append(f"   - 摘要线索：{abstract[:520]}")
    if reason:
        parts.append(f"   - 入选原因：{reason[:260]}")
    if finding:
        parts.append(f"   - 关键发现：{finding[:260]}")
    return "\n".join(parts)


def _collect_idea_evidence_pack(run_dir: Path, *, max_papers: int = 12, max_chars: int = 16_000) -> str:
    """Collect compact paper/card evidence for Stage 8 idea grounding."""
    sections: list[str] = []
    seen_titles: set[str] = set()
    paper_rows: list[dict[str, Any]] = []
    for artifact_name in ("shortlist.jsonl", "candidates.jsonl"):
        text = _read_prior_artifact(run_dir, artifact_name) or ""
        if not text.strip():
            continue
        for row in _parse_jsonl_rows(text):
            title = str(row.get("title", "") or "").strip().lower()
            if not title or title in seen_titles:
                continue
            seen_titles.add(title)
            paper_rows.append(row)
            if len(paper_rows) >= max_papers:
                break
        if len(paper_rows) >= max_papers:
            break
    if paper_rows:
        sections.append(
            "## 相近文献候选（用于每个 Idea 的 novelty 对照）\n"
            + "\n\n".join(
                _format_paper_for_idea_evidence(row, idx)
                for idx, row in enumerate(paper_rows, start=1)
            )
        )

    cards_path_text = _read_prior_artifact(run_dir, "cards/") or ""
    if cards_path_text:
        card_snippets: list[str] = []
        cards_dir = Path(cards_path_text)
        if cards_dir.is_dir():
            for card_path in sorted(cards_dir.glob("*.md"))[:8]:
                try:
                    snippet = card_path.read_text(encoding="utf-8", errors="ignore").strip()
                except OSError:
                    continue
                if snippet:
                    card_snippets.append(f"### {card_path.stem}\n{snippet[:1000]}")
        if card_snippets:
            sections.append("## 知识卡片摘要（用于 gap 和实验设计）\n" + "\n\n".join(card_snippets))

    if not sections:
        return ""
    pack = "\n\n".join(sections)
    if len(pack) > max_chars:
        pack = pack[:max_chars].rsplit("\n", 1)[0].strip() + "\n...（证据包已截断）"
    return pack


def _safe_stage_json_from_text(text: str) -> dict[str, Any]:
    """Best-effort JSON extraction from an LLM response."""
    raw = (text or "").strip()
    if not raw:
        return {}
    fenced = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", raw, re.S)
    candidates = [fenced.group(1)] if fenced else []
    candidates.append(raw)
    brace_start = raw.find("{")
    brace_end = raw.rfind("}")
    if brace_start >= 0 and brace_end > brace_start:
        candidates.append(raw[brace_start:brace_end + 1])
    for candidate in candidates:
        try:
            parsed = json.loads(candidate)
        except (json.JSONDecodeError, TypeError):
            continue
        if isinstance(parsed, dict):
            return parsed
    return {}


def _structured_idea_reflection(
    checkpoint_name: str,
    *,
    topic: str,
    context: str = "",
    dimensions: Sequence[str] = ("progress", "evidence", "strategy", "handoff"),
) -> str:
    """Structured reflection at key Stage 8 checkpoints, inspired by EvoScientist's 7-dimension think_tool.

    Dimensions available (pass subset relevant to the checkpoint):
    1. progress — What has been accomplished? What concrete steps remain?
    2. evidence — Is evidence sufficient for the goal? Would a critical reviewer accept it?
    3. skills — Is there an installed MetaClaw skill or learned lesson to leverage?
    4. prior_knowledge — Have ideation-memory and experiment-memory been checked?
    5. strategy — Continue, adjust, or try something different?
    6. handoff — What artifacts does the next sub-stage need?
    7. resource — Before heavy operations, estimate runtime and feasibility.
    """
    dim_descriptions = {
        "progress": "Progress — What has been accomplished? What concrete steps remain?",
        "evidence": "Evidence quality — Is the current evidence sufficient for the goal? Would a critical reviewer accept it?",
        "skills": "Skills leverage — Is there an installed MetaClaw skill or prior-run lesson that provides guidance?",
        "prior_knowledge": "Prior knowledge — Has ideation-memory and experiment-memory been reviewed for relevant past findings?",
        "strategy": "Strategy — Should the current approach continue, adjust, or change direction? What evidence supports this?",
        "handoff": "Handoff — Which artifacts and results does the next sub-stage need? Are outputs well-organized?",
        "resource": "Resource & compute — Estimate runtime and feasibility before proceeding. Does the hardware profile support the planned experiments?",
    }
    selected = [dim_descriptions.get(d, d) for d in dimensions]
    md = [
        f"## Structured Reflection: {checkpoint_name}",
        "",
        "### Relevant Dimensions",
        *[f"- {d}" for d in selected],
        "",
        "### Reflection Notes",
        "*[This checkpoint records the reasoning at this decision point for traceability.]*",
        "",
    ]
    if context:
        md.append(f"\nContext: {context[:2000]}\n")
    return "\n".join(md)


def _fallback_challenge_insight_tree(topic: str, synthesis: str, evidence_pack: str) -> dict[str, Any]:
    text = f"{synthesis}\n{evidence_pack}"
    challenge_keywords = [
        "robust", "scal", "latency", "cost", "privacy", "security", "alignment",
        "generalization", "evaluation", "dataset", "鲁棒", "泛化", "隐私", "安全", "延迟", "成本", "评估", "数据",
    ]
    sentences = re.split(r"(?<=[。！？.!?])\s+|\n+", text)
    challenges: list[dict[str, Any]] = []
    for sent in sentences:
        compact = sent.strip()
        if len(compact) < 24:
            continue
        field_name = compact.lstrip("- ").split("：", 1)[0].split(":", 1)[0]
        if compact.startswith(("匹配词", "来源", "### Evidence", "score=")) or field_name in {
            "核心假设", "技术机制", "文献缺口", "最小实验", "风险", "评分",
        }:
            continue
        lower = compact.lower()
        if any(k in lower for k in challenge_keywords) or any(k in compact for k in challenge_keywords):
            challenges.append({
                "challenge": compact[:180],
                "existing_insights": [],
                "missing_insights": ["需要从文献中进一步定位未被解决的机制缺口"],
                "transfer_opportunities": ["尝试把相邻方法迁移到该 challenge 的最小可验证版本"],
                "why_open": "由综述中的高频问题线索自动抽取，需在候选 Idea 中进一步验证。",
            })
        if len(challenges) >= 6:
            break
    if not challenges:
        challenges = [{
            "challenge": f"{topic} 中尚未被现有方法充分验证的关键机制缺口",
            "existing_insights": [],
            "missing_insights": ["缺少清晰的可证伪假设和最小实验"],
            "transfer_opportunities": ["从相近文献提取可复用 insight，形成两周 MVP"],
            "why_open": "文献证据不足时的保守兜底。",
        }]
    return {"topic": topic, "challenges": challenges, "bridge_opportunities": []}


def _challenge_tree_to_markdown(tree: dict[str, Any]) -> str:
    topic = str(tree.get("topic", "") or "").strip()
    lines = ["# Challenge-Insight Tree", "", f"Topic: {topic or '未记录'}", ""]
    challenges = tree.get("challenges", [])
    if not isinstance(challenges, list):
        challenges = []
    for idx, item in enumerate(challenges, start=1):
        if not isinstance(item, dict):
            continue
        lines.extend([f"## Challenge {idx}: {item.get('challenge', '未命名挑战')}", ""])
        for label, key in (
            ("Existing Insights", "existing_insights"),
            ("Missing Insights", "missing_insights"),
            ("Transfer Opportunities", "transfer_opportunities"),
        ):
            values = item.get(key, [])
            if isinstance(values, str):
                values = [values]
            lines.append(f"### {label}")
            if values:
                lines.extend(f"- {str(v).strip()}" for v in values[:8] if str(v).strip())
            else:
                lines.append("- 暂无明确条目")
            lines.append("")
        if item.get("why_open"):
            lines.extend(["### Why Still Open", f"- {item.get('why_open')}", ""])
    bridges = tree.get("bridge_opportunities", [])
    if isinstance(bridges, list) and bridges:
        lines.extend(["## Cross-Challenge Bridge Opportunities", ""])
        for bridge in bridges[:8]:
            lines.append(f"- {bridge}")
    return "\n".join(lines).strip() + "\n"


def _generate_challenge_insight_tree(
    llm: LLMClient | None,
    *,
    topic: str,
    synthesis: str,
    evidence_pack: str,
    ideation_memory: str,
) -> tuple[dict[str, Any], str]:
    if llm is None:
        tree = _fallback_challenge_insight_tree(topic, synthesis, evidence_pack)
        return tree, _challenge_tree_to_markdown(tree)
    system = (
        "你是科研选题分析专家。请把文献综述拆成 Challenge-Insight Tree，"
        "目标是帮助后续生成互不重复、文献扎实、可实验的研究 idea。只输出 JSON。"
    )
    user = (
        f"研究主题：\n{topic}\n\n"
        "请输出 JSON，不要 Markdown，schema 如下：\n"
        "{\n"
        '  "topic": "...",\n'
        '  "challenges": [\n'
        "    {\n"
        '      "challenge": "具体开放挑战",\n'
        '      "existing_insights": ["已有方法/发现"],\n'
        '      "missing_insights": ["尚缺机制/证据/实验"],\n'
        '      "transfer_opportunities": ["可从其他 challenge/方法迁移的 insight"],\n'
        '      "why_open": "为什么还值得做"\n'
        "    }\n"
        "  ],\n"
        '  "bridge_opportunities": ["跨 challenge 组合机会"]\n'
        "}\n\n"
        "要求：\n"
        "- challenges 4-7 个，避免泛泛而谈。\n"
        "- existing_insights 必须来自综述/证据包，不要编造论文。\n"
        "- missing_insights 要能转化成可证伪 idea。\n"
        "- 标出哪些 challenge 已经成熟，哪些 challenge 缺 insight 更适合创新。\n\n"
        f"长期 ideation memory：\n{ideation_memory[:5000] if ideation_memory else '无'}\n\n"
        f"文献综述：\n{synthesis[:12000]}\n\n"
        f"证据包：\n{evidence_pack[:12000] if evidence_pack else '无'}\n"
    )
    resp = llm.chat([{"role": "user", "content": user}], system=system, max_tokens=4096, temperature=0.2, json_mode=True)
    tree = _safe_stage_json_from_text(resp.content)
    if not tree:
        tree = _fallback_challenge_insight_tree(topic, synthesis, evidence_pack)
    return tree, _challenge_tree_to_markdown(tree)


def _generate_candidate_ideas(
    llm: LLMClient | None,
    *,
    topic: str,
    synthesis: str,
    raw_hypotheses_md: str,
    evidence_pack: str,
    challenge_tree_md: str,
    ideation_memory: str,
    idea_count: int,
) -> str:
    candidate_count = max(12, min(21, _clamp_idea_count(idea_count) * 3))
    if llm is None:
        seed_blocks = _idea_like_blocks(raw_hypotheses_md)
        seed_blocks.extend(_idea_like_blocks(_fallback_hypotheses_from_synthesis(topic, synthesis, max(idea_count, 8))))
        challenges = re.findall(r"^## Challenge\s+\d+\s*[：:]\s*(.+)$", challenge_tree_md, flags=re.M)
        evidence_lines = [
            line.strip("- ").strip()
            for line in (evidence_pack or synthesis).splitlines()
            if len(line.strip()) > 40
        ][:8]
        mechanisms = [
            "面向失败模式的检索增强校验",
            "轻量级因果探针与消融矩阵",
            "跨数据源一致性约束",
            "预算感知的主动采样",
            "不确定性驱动的人机复核",
            "结构化证据图上的反事实对照",
            "小模型先验与强模型裁判协同",
            "运行时漂移检测与早停策略",
        ]
        for idx in range(candidate_count * 2):
            challenge = challenges[idx % len(challenges)] if challenges else topic
            evidence = evidence_lines[idx % len(evidence_lines)] if evidence_lines else "已有综述指出该方向仍缺少可复现实证闭环。"
            mechanism = mechanisms[idx % len(mechanisms)]
            challenge_title = _idea_title_slug(re.split(r"[。！？.!?；;]", challenge)[0])[:34]
            if challenge_title.startswith(("匹配词", "来源")) or not challenge_title:
                challenge_title = f"{topic}关键瓶颈"
            title = f"{challenge_title} / {mechanism}"
            block = "\n".join([
                f"## Idea {idx + 1}: {title}",
                f"- 核心假设：围绕“{challenge_title}”引入{mechanism}，可以把泛化、可验证性或成本瓶颈转化为可测实验问题。",
                f"- 文献缺口：{evidence[:220]}",
                f"- 技术机制：构建 evidence map，定位一个主要失败模式，并用{mechanism}形成与现有方法不同的干预变量。",
                "- 最小实验：选择一个公开基准和一个压力测试子集，对比 baseline、无机制版本和完整版本。",
                "- 风险：若提升只来自额外算力或数据清洗，则降级为诊断工具而不是主方法。",
                "- 评分：Novelty 3 / Feasibility 4 / Impact 3 / Testability 4 / Compute 3 / Diversity 4",
            ])
            seed_blocks.append((title, block))
        unique_blocks: list[tuple[str, str]] = []
        for title, block in seed_blocks:
            if len(unique_blocks) >= candidate_count:
                break
            if any(
                _idea_text_similarity(block, existing_block) >= 0.86
                or _idea_text_similarity(title, existing_title) >= 0.70
                for existing_title, existing_block in unique_blocks
            ):
                continue
            unique_blocks.append((title, block))
        if len(unique_blocks) < candidate_count:
            for title, block in seed_blocks:
                if len(unique_blocks) >= candidate_count:
                    break
                if any(_idea_text_similarity(title, existing_title) >= 0.86 for existing_title, _ in unique_blocks):
                    continue
                unique_blocks.append((title, block))
        return _renumber_idea_blocks(unique_blocks[:candidate_count])
    system = (
        "你是科研 idea 扩展器。你的任务不是最终筛选，而是基于 Challenge-Insight Tree "
        "生成足够多、机制差异明显、可进入 tournament 的候选 idea。必须中文输出。"
    )
    user = (
        f"研究主题：\n{topic}\n\n"
        f"请生成 {candidate_count} 个候选 Idea，用 Markdown 输出。\n"
        "规则：\n"
        "- 候选必须覆盖不同 challenge / insight bridge / 技术机制。\n"
        "- 允许有高风险候选，但必须写清最小证据。\n"
        "- 不要把同一机制换标题、换数据集、换指标凑数。\n"
        "- 每个候选保持紧凑，但必须包含：核心假设、文献缺口、关键区别、最小实验、风险、粗略评分。\n"
        "- 参考 ideation memory：继承成功方向，避开 fundamental failure 方向。\n\n"
        "输出格式：\n"
        "## Idea 1：标题\n"
        "- 核心假设：...\n- 文献缺口：...\n- 技术机制：...\n- 最小实验：...\n- 风险：...\n- 评分：Novelty/Feasibility/Impact/Testability/Compute/Diversity\n\n"
        f"Challenge-Insight Tree：\n{challenge_tree_md[:10000]}\n\n"
        f"长期 ideation memory：\n{ideation_memory[:5000] if ideation_memory else '无'}\n\n"
        f"文献综述：\n{synthesis[:9000]}\n\n"
        f"证据包：\n{evidence_pack[:9000] if evidence_pack else '无'}\n\n"
        f"已有 raw hypotheses seed：\n{raw_hypotheses_md[:9000]}\n"
    )
    resp = llm.chat([{"role": "user", "content": user}], system=system, max_tokens=12000, temperature=0.8)
    text = resp.content.strip()
    if _idea_like_count(text) < idea_count:
        text = _complete_idea_set(
            text,
            topic=topic,
            synthesis=synthesis,
            idea_count=candidate_count,
            fallback_sources=(raw_hypotheses_md, _fallback_hypotheses_from_synthesis(topic, synthesis, candidate_count)),
        )
    return text


def _generate_role_review(
    llm: LLMClient | None,
    *,
    topic: str,
    synthesis: str,
    core_ideas_md: str,
    evidence_pack: str,
    tournament_md: str,
) -> str:
    if llm is None:
        return "# Idea Role Review\n\n- LLM unavailable; role review skipped.\n"
    system = (
        "你是由三位科研角色组成的 idea panel：Innovator、Pragmatist、Critic。"
        "请分别从突破性、可执行性、科学严谨性审查候选 idea，最后给出合议修改建议。中文输出。"
    )
    user = (
        f"研究主题：\n{topic}\n\n"
        "请输出：\n"
        "# Idea Role Review\n"
        "## Innovator 视角\n逐条指出哪些 idea 有突破潜力，如何增强 novelty。\n"
        "## Pragmatist 视角\n逐条指出两周 MVP、数据、baseline、compute 是否清楚。\n"
        "## Critic 视角\n逐条指出 reviewer 会质疑的新颖性、证据和实验漏洞。\n"
        "## 合议修改清单\n给出必须修改/合并/替换的条目。\n\n"
        f"Tournament 摘要：\n{tournament_md[:6000]}\n\n"
        f"文献综述：\n{synthesis[:7000]}\n\n"
        f"证据包：\n{evidence_pack[:8000] if evidence_pack else '无'}\n\n"
        f"待审查 ideas：\n{core_ideas_md[:16000]}\n"
    )
    resp = llm.chat([{"role": "user", "content": user}], system=system, max_tokens=8192)
    return resp.content.strip()


def _maybe_literature_grounded_pivot(
    llm: LLMClient | None,
    *,
    topic: str,
    synthesis: str,
    current_ideas_md: str,
    evidence_pack: str,
    tournament_report: dict[str, Any],
    challenge_tree_md: str,
    idea_count: int,
) -> tuple[str, str]:
    ranking = tournament_report.get("ranking", []) if isinstance(tournament_report, dict) else []
    needs_pivot = False
    if isinstance(ranking, list) and ranking:
        weak = [r for r in ranking[:_clamp_idea_count(idea_count)] if isinstance(r, dict) and (float(r.get("diversity", 5) or 5) <= 2 or float(r.get("literature_grounding", 5) or 5) <= 2)]
        needs_pivot = bool(weak)
    if llm is None or not needs_pivot:
        reason = "No pivot triggered: selected ideas passed local diversity/grounding thresholds."
        return current_ideas_md, f"# Literature-Grounded Pivot\n\n{reason}\n"
    system = (
        "你是科研 idea pivot 专家。只在 idea 重复、文献依据弱或机制过虚时进行替换。"
        "你的输出必须保留强 idea，替换弱 idea，最终中文输出 exactly 指定数量。"
    )
    user = (
        f"研究主题：\n{topic}\n\n"
        f"请基于文献证据对当前 ideas 做 literature-grounded pivot，最终输出 exactly {idea_count} 个非重复 Idea。\n"
        "规则：\n"
        "- 保留 tournament 中强且可实验的 idea。\n"
        "- 替换 diversity 低、literature grounding 弱或机制重复的 idea。\n"
        "- 替代 idea 必须来自 Challenge-Insight Tree 中未充分覆盖的 challenge 或 transfer opportunity。\n"
        "- 每个替代都写 Pivot Reason。\n\n"
        f"{_idea_schema_instruction()}\n\n"
        f"Challenge-Insight Tree：\n{challenge_tree_md[:8000]}\n\n"
        f"Tournament JSON：\n{json.dumps(tournament_report, ensure_ascii=False)[:9000]}\n\n"
        f"文献综述：\n{synthesis[:7000]}\n\n"
        f"证据包：\n{evidence_pack[:9000] if evidence_pack else '无'}\n\n"
        f"当前 ideas：\n{current_ideas_md[:14000]}\n"
    )
    resp = llm.chat([{"role": "user", "content": user}], system=system, max_tokens=12000)
    pivoted = resp.content.strip()
    if not pivoted:
        return current_ideas_md, "# Literature-Grounded Pivot\n\nPivot attempted but returned empty output; kept current ideas.\n"
    pivoted = _complete_idea_set(pivoted, topic=topic, synthesis=synthesis, idea_count=idea_count, fallback_sources=(current_ideas_md,))
    return pivoted, "# Literature-Grounded Pivot\n\nPivot triggered because local tournament found weak diversity or grounding.\n\n" + pivoted


def _add_reflection_checkpoint(
    stage_dir: Path,
    checkpoint_name: str,
    *,
    topic: str,
    context: str = "",
    dimensions: Sequence[str] = ("progress", "evidence", "strategy", "handoff"),
) -> str:
    """Write a structured reflection checkpoint to disk during Stage 8."""
    reflection = _structured_idea_reflection(
        checkpoint_name, topic=topic, context=context, dimensions=dimensions,
    )
    try:
        ref_dir = stage_dir / "reflections"
        ref_dir.mkdir(parents=True, exist_ok=True)
        safe_name = re.sub(r"[^a-z0-9]+", "_", checkpoint_name.lower()).strip("_")
        (ref_dir / f"{safe_name}.md").write_text(reflection, encoding="utf-8")
        logger.info("S8: Reflection checkpoint '%s' written", checkpoint_name)
    except OSError:
        logger.warning("S8: Could not write reflection checkpoint '%s'", checkpoint_name)
    return reflection


def _generate_idea_review(
    llm: LLMClient,
    topic: str,
    synthesis: str,
    core_ideas_md: str,
    evidence_pack: str,
    idea_count: int = 5,
) -> str:
    """Generate a reviewer-style quality gate for Stage 8 ideas."""
    system = (
        "你是顶会 senior area chair 和严苛科研导师。你的任务不是生成新 idea，"
        "而是审查已有 idea 是否真的新、是否可实验、是否会被 reviewer 质疑。"
        "必须使用中文输出，英文仅保留论文名、方法名、数据集和指标。"
    )
    user = (
        f"研究主题：\n{topic}\n\n"
        "请审查下面的 core ideas。你必须严格、具体、可执行。不要泛泛而谈。\n\n"
        f"{_idea_quality_rubric()}\n\n"
        "输出格式：\n"
        "# Idea Review\n\n"
        "## 总体结论\n"
        "说明这批 idea 是否已经适合进入实验设计阶段。\n\n"
        "## 逐项评分表\n"
        "对每个 Idea 给 Novelty、Feasibility、Impact、Testability、Literature Grounding、Risk、Compute Cost、Diversity、总分。\n\n"
        "## 最相近文献与 novelty 风险\n"
        "每个 Idea 列出 3-5 篇最相近文献，指出 reviewer 会认为它不新的具体理由。\n\n"
        "## 必须修复的问题\n"
        "列出阻碍进入 S9 实验设计的具体问题，每条给修复建议。\n\n"
        "## 推荐排序\n"
        f"只推荐一个首选 Idea，并说明其他 {max(idea_count - 1, 0)} 个 Idea 的优先级、暂缓条件或合并原因。\n\n"
        "## Go / No-Go 检查\n"
        "给出进入实验设计前必须满足的检查项。\n\n"
        f"文献综述摘要：\n{synthesis[:9000]}\n\n"
        f"证据包：\n{evidence_pack[:12000] if evidence_pack else '没有额外证据包。'}\n\n"
        f"待审查 ideas：\n{core_ideas_md[:18000]}\n"
    )
    resp = llm.chat(
        [{"role": "user", "content": user}],
        system=system,
        max_tokens=8192,
    )
    return resp.content


def _apply_idea_review(
    llm: LLMClient,
    topic: str,
    synthesis: str,
    core_ideas_md: str,
    idea_review_md: str,
    evidence_pack: str,
    idea_count: int = 5,
) -> str:
    """Revise core ideas using the reviewer report and fixed idea schema."""
    system = (
        "你是负责把科研想法改到可以开题/进实验的 research lead。"
        "你必须吸收 reviewer 批评，产出最终可执行中文版 core_ideas.md。"
        "不要输出内部推理过程，不要输出英文草稿。"
    )
    user = (
        f"研究主题：\n{topic}\n\n"
        "请根据 reviewer report 重写/修订 core ideas。要求：\n"
        f"- 输出 exactly {idea_count} 个最终 Idea；如果发现机制重复，必须合并重复项并补充新的非重复候选。\n"
        "- 每个 Idea 必须绑定相近文献、差异点、实验、失败条件和评分。\n"
        "- 最终全文使用中文。英文只保留专有名词。\n"
        "- 最终结果应能直接交给 S9 实验设计使用。\n\n"
        f"{_idea_schema_instruction()}\n\n"
        f"{_idea_quality_rubric()}\n\n"
        f"文献综述摘要：\n{synthesis[:8000]}\n\n"
        f"证据包：\n{evidence_pack[:10000] if evidence_pack else '没有额外证据包。'}\n\n"
        f"原始 core ideas：\n{core_ideas_md[:16000]}\n\n"
        f"Reviewer report：\n{idea_review_md[:10000]}\n"
    )
    resp = llm.chat(
        [{"role": "user", "content": user}],
        system=system,
        max_tokens=12000,
    )
    return resp.content


def _distill_core_ideas(
    llm: LLMClient,
    topic: str,
    synthesis: str,
    hypotheses_md: str,
    evidence_pack: str = "",
    idea_count: int = 5,
) -> str:
    """Refine raw hypotheses into highly detailed, execution-ready core ideas."""
    synthesis_snippet = synthesis[:12000].strip()
    hypotheses_snippet = hypotheses_md[:16000].strip()
    evidence_snippet = evidence_pack[:12000].strip()
    system = (
        "You are a senior research director refining research hypotheses into detailed, "
        "execution-ready core ideas. Your output must be MORE detailed than the input — "
        "you add concrete implementation specifics, precise experimental designs, "
        "and clear evaluation plans. Do NOT shorten or condense; instead, elaborate and specify. "
        "You must write the final document in Chinese, while preserving necessary English technical terms."
    )
    user = (
        f"Research topic:\n{topic}\n\n"
        "OUTPUT LANGUAGE: Write the entire final core-ideas document in Chinese. Preserve English only for paper names, method names, datasets, metrics, libraries, and model checkpoints.\n\n"
        "You will receive a literature synthesis and a set of draft hypotheses.\n"
        f"Your job is to REFINE exactly {idea_count} strongest, non-overlapping ideas into highly detailed, "
        "execution-ready research plans.\n"
        "IMPORTANT: You must ADD detail, NOT remove it. Make each idea so concrete "
        "that a graduate student could start coding within an hour.\n\n"
        "The final document must behave like a mini research proposal, not brainstorming notes.\n"
        "Every idea must be grounded in nearby papers, include a novelty defense, and contain a falsifiable experiment plan.\n\n"
        f"{_idea_schema_instruction()}\n\n"
        f"{_idea_quality_rubric()}\n\n"
        "For each idea, also make sure these details are concrete and thorough:\n\n"
        "## Idea N: <short, descriptive title>\n\n"
        "### Problem Gap\n"
        "<What specific gap in prior work this targets. Cite specific papers from the "
        "synthesis and explain what they missed.>\n\n"
        "### Core Idea\n"
        "<4-6 sentences explaining the actual technical mechanism with specific details: "
        "model architecture, loss function, training procedure, data preprocessing. "
        "Include layer dimensions, hyperparameter choices, and design rationale.>\n\n"
        "### Why It Is New\n"
        "<Concrete comparison with existing methods: what is different from obvious baselines, "
        "and why that difference matters. Do NOT say 'no one has tried this' — instead explain "
        "what conventional wisdom this challenges or what technical barrier it overcomes.>\n\n"
        "### Feasibility Analysis\n"
        "<Why this can be done now: available pre-trained models, existing codebases to build on, "
        "compute requirements (estimated GPU hours), data availability.>\n\n"
        "### Detailed Experimental Design\n"
        "- Dataset(s): <name specific datasets, splits, preprocessing, and why they were chosen>\n"
        "- Model architecture: <exact layer configuration, parameter counts, initialization>\n"
        "- Training procedure: <optimizer, learning rate schedule, batch size, precision, epochs, regularization>\n"
        "- Baseline 1: <paper + method + expected performance>\n"
        "- Baseline 2: <paper + method + expected performance>\n"
        "- Baseline 3 (if needed): <paper + method + expected performance>\n"
        "- Evaluation metrics: <exact metrics, calculation details, and statistical significance tests>\n"
        "- Expected outcome: <specific numbers: 'Our method should achieve X% vs baseline Y% on metric Z'>\n"
        "- Ablation studies: <list 2-3 ablations that isolate each component of your method>\n\n"
        "### 2-Week MVP\n"
        "<The smallest concrete experiment that can validate the core idea. Include: "
        "which subset of data to use, which model size, what is the earliest checkpoint "
        "that would show signal, and what would constitute a 'Go/No-Go' decision.>\n\n"
        "### Main Risks\n"
        "<2-3 specific things that could cause failure, ranked by likelihood, with early detection signals and fallback plans>\n\n"
        "---\n\n"
        "After listing all ideas, add:\n"
        "## Recommended First Try\n"
        "<Exactly one idea recommended for first try, with 3-4 sentence justification based on "
        "expected impact, feasibility, and novelty trade-off.>\n\n"
        "## Quick Comparison\n"
        "<A short table comparing the ideas side by side on: novelty, feasibility, expected impact, "
        "risk handling, compute cost, and mechanism diversity.>\n\n"
        "Rules:\n"
        "- Do NOT make the output shorter than the input. Longer is better if it adds specifics.\n"
        f"- Target exactly {idea_count} final ideas. Merge overlapping ideas when they are the same mechanism, then add a different non-overlapping candidate so the final set remains at the target count.\n"
        "- Do a duplicate check before finalizing: same mechanism with a different title, dataset, or metric is still duplicate.\n"
        "- Remove only vague or untestable ideas.\n"
        "- Prefer depth over breadth — one fully specified idea is better than three vague ones.\n"
        "- Name exact datasets, model checkpoints, and framework libraries when possible.\n"
        "- No brainstorming chatter, no reviewer-style debate, no meta-commentary.\n\n"
        f"Literature synthesis:\n{synthesis_snippet}\n\n"
        f"Nearby-paper evidence pack:\n{evidence_snippet if evidence_snippet else 'No additional evidence pack available.'}\n\n"
        f"Draft hypotheses:\n{hypotheses_snippet}\n"
    )
    resp = llm.chat(
        [{"role": "user", "content": user}],
        system=system,
    )
    return resp.content


def _refine_core_ideas(
    llm: LLMClient,
    topic: str,
    synthesis: str,
    core_ideas_md: str,
    evidence_pack: str = "",
) -> str:
    """Self-critique and refine core ideas, filling in missing details."""
    synthesis_snippet = synthesis[:8000].strip()
    core_snippet = core_ideas_md[:16000].strip()
    evidence_snippet = evidence_pack[:10000].strip()
    system = (
        "You are a meticulous research reviewer. Your job is to make research ideas "
        "MORE specific and MORE actionable by identifying and filling in missing details.\n\n"
        "For each gap you find (missing dataset name, vague architecture description, "
        "unspecified training hyperparameters, unclear evaluation protocol), "
        "you add the missing detail. You NEVER remove content — only add. "
        "You must write the improved document in Chinese, preserving necessary English technical terms."
    )
    user = (
        f"Research topic:\n{topic}\n\n"
        "OUTPUT LANGUAGE: Use Chinese for the full improved core-ideas document. Keep English only for proper nouns such as paper names, datasets, metrics, methods, libraries, and model checkpoints.\n\n"
        "Review the core ideas below against the literature synthesis and produce "
        "an IMPROVED version. Your goal is to add specificity wherever the text is vague.\n\n"
        "Checklist of common vagueness to fix:\n"
        "- [ ] 'use a transformer' → specify which one (BERT-base? ViT-B/16? GPT-2?) and size\n"
        "- [ ] 'train on a standard dataset' → name the exact dataset, version, and split\n"
        "- [ ] 'standard hyperparameters' → specify optimizer, LR, batch size, epochs, schedule\n"
        "- [ ] 'compare with baselines' → name exact baselines with paper citations\n"
        "- [ ] 'evaluate on metrics' → name exact metrics and how they are computed\n"
        "- [ ] 'reasonable compute budget' → specify approximate GPU hours\n"
        "- [ ] 'ablation study' → specify what exactly gets ablated\n"
        "- [ ] missing nearest-paper comparison → list 3-5 similar works and exact differences\n"
        "- [ ] missing falsification threshold → state what result would reject the idea\n"
        "- [ ] missing score table → add Novelty/Feasibility/Impact/Testability/Literature Grounding/Risk/Compute Cost/Diversity scores\n\n"
        "OUTPUT FORMAT:\n"
        "Output the COMPLETE improved version of the core ideas document. "
        "Keep the original structure and all original content. Add details inline "
        "wherever the checklist items above identify vagueness.\n\n"
        "If the document is already fully specific, output it unchanged.\n\n"
        f"{_idea_schema_instruction()}\n\n"
        f"{_idea_quality_rubric()}\n\n"
        f"Literature synthesis (for reference):\n{synthesis_snippet}\n\n"
        f"Nearby-paper evidence pack:\n{evidence_snippet if evidence_snippet else 'No additional evidence pack available.'}\n\n"
        f"Core ideas to refine:\n{core_snippet}\n"
    )
    resp = llm.chat(
        [{"role": "user", "content": user}],
        system=system,
    )
    refined = resp.content
    return refined


def _execute_hypothesis_gen(
    stage_dir: Path,
    run_dir: Path,
    config: RCConfig,
    adapters: AdapterBundle,
    *,
    llm: LLMClient | None = None,
    prompts: PromptManager | None = None,
) -> StageResult:
    synthesis = _read_prior_artifact(run_dir, "synthesis.md") or ""
    idea_count = _configured_idea_count(config)

    # --- Load hardware profile for feasibility-aware idea generation ---
    _hardware_profile = _load_hardware_profile(run_dir)
    _hardware_context = ""
    if _hardware_profile:
        hw_tier = _hardware_profile.get("tier", "unknown")
        gpu_name = _hardware_profile.get("gpu_name", "unknown")
        vram = _hardware_profile.get("vram_mb", "unknown")
        gpu_type = _hardware_profile.get("gpu_type", "cpu")
        _hardware_context = (
            f"\n\n## HARDWARE CONSTRAINT AWARENESS\n"
            f"Available hardware: tier={hw_tier}, gpu={gpu_name}, vram={vram}MB, type={gpu_type}\n"
            f"Implications for idea feasibility:\n"
        )
        if hw_tier == "cpu_only":
            _hardware_context += (
                "- No GPU available. Prefer ideas using NumPy/sklearn/lightweight methods.\n"
                "- Ideas requiring large-model training or massive parallel compute are infeasible — downgrade their compute_cost and feasibility scores.\n"
                "- Recommend CPU-linear-time algorithms, theoretical work, or small-scale proofs-of-concept.\n"
            )
        elif hw_tier == "limited":
            _hardware_context += (
                f"- Limited GPU ({vram}MB). Prefer ideas that fit in single-GPU with batch-size tuning.\n"
                "- Ideas requiring multi-GPU training, 7B+ parameter models, or large-scale pretraining carry high compute risk.\n"
                "- Recommend parameter-efficient methods (LoRA, adapters), smaller backbone models, or data-subset experiments.\n"
            )
        else:
            _hardware_context += (
                "- High-end GPU available. Most compute-heavy ideas are feasible.\n"
                "- Multi-GPU training and large-scale experiments are possible.\n"
                "- Still flag compute_cost in idea scoring for resource awareness.\n"
            )

    # --- Inject prior knowledge from shared knowledge base ---
    _shared_dir = getattr(config.experiment, "shared_results_dir", "") or ""
    _prior_knowledge = ""
    if _shared_dir:
        _kb_index = Path(_shared_dir) / "knowledge_base" / "knowledge_index.jsonl"
        if _kb_index.exists():
            try:
                _kb_lines = _kb_index.read_text(encoding="utf-8").strip().split("\n")
                _kb_entries = []
                for _line in _kb_lines[-20:]:
                    _e = json.loads(_line)
                    _kb_entries.append(
                        f"- **{_e.get('topic', '?')}**: "
                        f"Conclusions: {'; '.join(_e.get('conclusions', [])[:3])}. "
                        f"Insights: {'; '.join(_e.get('insights', [])[:2])}. "
                        f"Directions: {'; '.join(_e.get('suggested_directions', [])[:2])}"
                    )
                if _kb_entries:
                    _prior_knowledge = (
                        "\n\n## PRIOR RESEARCH KNOWLEDGE (from completed projects)\n"
                        "Consider these findings when generating hypotheses. "
                        "Build upon successful approaches and avoid repeating failed ones.\n\n"
                        + "\n".join(_kb_entries) + "\n"
                    )
                    logger.info("S8: Injected %d prior knowledge entries", len(_kb_entries))
            except Exception:
                pass

    raw_hypotheses_md = ""
    _ideation_memory = _load_ideation_memory(config, run_dir)
    _overlay = _get_evolution_overlay(run_dir, "hypothesis_gen")
    _hypothesis_context = (
        synthesis
        + "\n\n## LANGUAGE REQUIREMENT FOR STAGE 8\n"
        + "最终 hypotheses.md、hypotheses_raw.md 和 core_ideas.md 必须使用中文撰写。英文只保留论文名、方法名、数据集、指标、库名、模型 checkpoint 等专有名词。\n"
        + f"\n\n## IDEA COUNT AND DEDUP REQUIREMENT\n{_idea_count_rule(idea_count)}\n"
        + _prior_knowledge
    )
    if _ideation_memory:
        _hypothesis_context += (
            "\n\n## IDEATION MEMORY (avoid repeated failed directions; build on promising ones)\n"
            + _ideation_memory[:8000]
            + "\n"
        )
    if _overlay:
        _hypothesis_context += (
            "\n\n## HYPOTHESIS GENERATION SKILL GUIDANCE\n"
            "Apply this guidance when formulating and selecting hypotheses.\n\n"
            f"{_overlay}\n"
        )

    if _hardware_context:
        _hypothesis_context += _hardware_context

    # --- Load experiment memory for cross-cycle learning ---
    _experiment_memory = _load_experiment_memory(config, run_dir)
    if _experiment_memory:
        _hypothesis_context += (
            "\n\n## EXPERIMENT MEMORY (lessons from past experimental runs — avoid repeated failures, build on proven strategies)\n"
            + _experiment_memory[:8000]
            + "\n"
        )

    use_local_stage8 = os.environ.get("RESEARCHCLAW_STAGE8_LOCAL_FALLBACK", "").lower() in {"1", "true", "yes"}
    if use_local_stage8:
        logger.info("S8: Using local fallback because RESEARCHCLAW_STAGE8_LOCAL_FALLBACK is set")
        raw_hypotheses_md = _fallback_hypotheses_from_synthesis(
            config.research.topic,
            synthesis or _hypothesis_context,
            idea_count,
        )
    elif llm is not None:
        _pm = prompts or PromptManager()
        from researchclaw.prompts import DEBATE_ROLES_HYPOTHESIS  # noqa: PLC0415

        try:
            # --- Multi-perspective debate ---
            perspectives_dir = stage_dir / "perspectives"
            variables = {"topic": config.research.topic, "synthesis": _hypothesis_context, "idea_count": str(idea_count)}
            perspectives = _multi_perspective_generate(
                llm, DEBATE_ROLES_HYPOTHESIS, variables, perspectives_dir
            )
            if len(perspectives) < 2:
                raise RuntimeError(
                    f"Only {len(perspectives)} debate perspective(s) succeeded; using local fallback"
                )
            # --- Synthesize into raw hypotheses ---
            raw_hypotheses_md = _synthesize_perspectives(
                llm, perspectives, "hypothesis_synthesize", _pm, idea_count
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "S8: LLM hypothesis generation failed; using local synthesis fallback: %s",
                exc,
            )
            raw_hypotheses_md = _fallback_hypotheses_from_synthesis(
                config.research.topic,
                _hypothesis_context,
                idea_count,
            )
    else:
        raw_hypotheses_md = _fallback_hypotheses_from_synthesis(config.research.topic, _hypothesis_context, idea_count)

    (stage_dir / "hypotheses_raw.md").write_text(raw_hypotheses_md, encoding="utf-8")

    idea_evidence_pack = _collect_idea_evidence_pack(run_dir)
    retrieval_artifacts: tuple[str, ...] = ()
    if use_local_stage8:
        logger.info("S8: Skipping Research RAG bundle in local fallback mode")
    else:
        try:
            from researchclaw.rag.hybrid import build_research_rag_bundle

            projects_dir = run_dir.parent.parent if run_dir.parent.name.startswith("run-") else run_dir.parent
            rag_context, retrieval_artifacts, rag_report = build_research_rag_bundle(
                run_dir,
                config.research.topic,
                stage_dir,
                projects_dir=projects_dir if projects_dir.is_dir() else None,
                intent="idea",
                top_k=14,
            )
            if rag_context:
                idea_evidence_pack = (
                    idea_evidence_pack
                    + "\n\n## HYBRID RETRIEVAL EVIDENCE\n"
                    + rag_context
                ).strip()
            logger.info(
                "S8: Research RAG bundle built with %d hit(s), project_chunks=%s, global_chunks=%s",
                int(rag_report.get("count", 0) or 0),
                rag_report.get("index", {}).get("project_chunks", 0),
                rag_report.get("index", {}).get("global_chunks", 0),
            )
        except Exception:  # noqa: BLE001
            logger.warning("S8: Research RAG bundle failed — using artifact evidence only", exc_info=True)

    if idea_evidence_pack:
        (stage_dir / "idea_evidence_pack.md").write_text(idea_evidence_pack, encoding="utf-8")
        logger.info("S8: Collected idea evidence pack (%d chars)", len(idea_evidence_pack))

    hypotheses_path = stage_dir / "hypotheses.md"
    if not hypotheses_path.exists():
        hypotheses_path.write_text(raw_hypotheses_md, encoding="utf-8")

    # --- Pre-ideation structured reflection (strategy checkpoint) ---
    try:
        reflection_context = (
            f"Topic: {config.research.topic}\n"
            f"Evidence pack: {len(idea_evidence_pack)} chars\n"
            f"Ideation memory: {'loaded' if _ideation_memory else 'none'}\n"
            f"Hardware: {_hardware_profile.get('tier', 'unknown') if _hardware_profile else 'unknown'}\n"
            f"Experiment memory: {'loaded' if _experiment_memory else 'none'}"
        )
        _add_reflection_checkpoint(
            stage_dir, "pre_ideation_strategy",
            topic=config.research.topic,
            context=reflection_context,
            dimensions=("progress", "strategy", "prior_knowledge", "resource"),
        )
    except Exception:  # noqa: BLE001
        pass

    # --- EvoScientist-inspired idea factory front-end: challenge tree → candidates → tournament ---
    challenge_artifacts: tuple[str, ...] = ()
    candidate_artifacts: tuple[str, ...] = ()
    tournament_artifacts: tuple[str, ...] = ()
    challenge_tree_md = ""
    candidate_ideas_md = ""
    tournament_report: dict[str, Any] = {}
    tournament_md = ""
    tournament_selected_md = ""
    try:
        challenge_tree, challenge_tree_md = _generate_challenge_insight_tree(
            None if use_local_stage8 else llm,
            topic=config.research.topic,
            synthesis=(synthesis if use_local_stage8 else _hypothesis_context),
            evidence_pack=idea_evidence_pack,
            ideation_memory=_ideation_memory,
        )
        (stage_dir / "challenge_insight_tree.json").write_text(
            json.dumps(challenge_tree, indent=2, ensure_ascii=False),
            encoding="utf-8",
        )
        (stage_dir / "challenge_insight_tree.md").write_text(challenge_tree_md, encoding="utf-8")
        challenge_artifacts = ("challenge_insight_tree.json", "challenge_insight_tree.md")
        logger.info("S8: Challenge-Insight Tree generated")
    except Exception:  # noqa: BLE001
        logger.warning("S8: Challenge-Insight Tree generation failed", exc_info=True)
        challenge_tree_md = ""

    try:
        candidate_ideas_md = _generate_candidate_ideas(
            None if use_local_stage8 else llm,
            topic=config.research.topic,
            synthesis=(synthesis if use_local_stage8 else _hypothesis_context),
            raw_hypotheses_md=raw_hypotheses_md,
            evidence_pack=idea_evidence_pack,
            challenge_tree_md=challenge_tree_md,
            ideation_memory=_ideation_memory,
            idea_count=idea_count,
        )
        if candidate_ideas_md.strip():
            (stage_dir / "candidate_ideas.md").write_text(candidate_ideas_md, encoding="utf-8")
            candidate_artifacts = ("candidate_ideas.md",)
            tournament_report, tournament_md, tournament_selected_md = _run_idea_tournament(
                candidate_ideas_md,
                idea_count=idea_count,
            )
            (stage_dir / "idea_tournament.json").write_text(
                json.dumps(tournament_report, indent=2, ensure_ascii=False),
                encoding="utf-8",
            )
            (stage_dir / "idea_tournament.md").write_text(tournament_md, encoding="utf-8")
            tournament_artifacts = ("idea_tournament.json", "idea_tournament.md")
            logger.info(
                "S8: Idea tournament ranked %d candidate(s)",
                int(tournament_report.get("candidate_count", 0) or 0),
            )
    except Exception:  # noqa: BLE001
        logger.warning("S8: Candidate idea expansion/tournament failed", exc_info=True)
        candidate_ideas_md = ""
        tournament_selected_md = ""

    # --- Post-tournament evidence reflection ---
    try:
        _add_reflection_checkpoint(
            stage_dir, "post_tournament_evidence",
            topic=config.research.topic,
            context=f"Tournament candidates: {tournament_report.get('candidate_count', 0)}, "
                    f"selected: {len(_idea_like_blocks(tournament_selected_md)) if tournament_selected_md else 0}",
            dimensions=("progress", "evidence", "strategy"),
        )
    except Exception:  # noqa: BLE001
        pass

    core_ideas_md = tournament_selected_md.strip() or raw_hypotheses_md
    core_ideas_md = _complete_idea_set(
        core_ideas_md,
        topic=config.research.topic,
        synthesis=synthesis or _hypothesis_context,
        idea_count=idea_count,
        fallback_sources=(candidate_ideas_md, raw_hypotheses_md),
    )
    if llm is not None and not use_local_stage8:
        try:
            core_ideas_md = _distill_core_ideas(
                llm,
                config.research.topic,
                _hypothesis_context,
                raw_hypotheses_md,
                idea_evidence_pack,
                idea_count,
            )
            core_ideas_md = _complete_idea_set(
                core_ideas_md,
                topic=config.research.topic,
                synthesis=_hypothesis_context,
                idea_count=idea_count,
                fallback_sources=(raw_hypotheses_md,),
            )
            logger.info("S8: Distilled hypotheses into detailed core ideas (%d chars)", len(core_ideas_md))
        except Exception:  # noqa: BLE001
            logger.warning("S8: Idea distillation failed — falling back to raw hypotheses", exc_info=True)
            core_ideas_md = raw_hypotheses_md

    (stage_dir / "core_ideas.md").write_text(core_ideas_md, encoding="utf-8")

    # --- Self-critique and refinement (only for LLM-generated ideas) ---
    refined_ideas_md = core_ideas_md
    if llm is not None and not use_local_stage8:
        try:
            refined_ideas_md = _refine_core_ideas(
                llm,
                config.research.topic,
                _hypothesis_context,
                core_ideas_md,
                idea_evidence_pack,
            )
            refined_ideas_md = _complete_idea_set(
                refined_ideas_md,
                topic=config.research.topic,
                synthesis=_hypothesis_context,
                idea_count=idea_count,
                fallback_sources=(core_ideas_md, raw_hypotheses_md),
            )
            logger.info(
                "S8: Self-critique refined core ideas (%d chars → %d chars)",
                len(core_ideas_md), len(refined_ideas_md),
            )
            (stage_dir / "core_ideas.md").write_text(refined_ideas_md, encoding="utf-8")
        except Exception:  # noqa: BLE001
            logger.warning("S8: Self-critique refinement failed — keeping original", exc_info=True)

    # --- Multi-branch idea mutation and synthesis (non-blocking) ---
    branch_artifacts: tuple[str, ...] = ()
    if llm is not None and not use_local_stage8:
        try:
            branches_dir = stage_dir / "idea_branches"
            idea_branches = _generate_idea_mutation_branches(
                llm,
                config.research.topic,
                _hypothesis_context,
                refined_ideas_md,
                idea_evidence_pack,
                branches_dir,
                idea_count,
            )
            if idea_branches:
                branch_artifacts = ("idea_branches/",)
                branch_synthesis_md = _synthesize_idea_mutation_branches(
                    llm,
                    config.research.topic,
                    _hypothesis_context,
                    idea_branches,
                    idea_evidence_pack,
                    idea_count,
                )
                if branch_synthesis_md.strip():
                    (stage_dir / "idea_branch_synthesis.md").write_text(branch_synthesis_md, encoding="utf-8")
                    branch_artifacts = ("idea_branches/", "idea_branch_synthesis.md")
                    refined_ideas_md = _complete_idea_set(
                        branch_synthesis_md,
                        topic=config.research.topic,
                        synthesis=_hypothesis_context,
                        idea_count=idea_count,
                        fallback_sources=(refined_ideas_md, core_ideas_md, raw_hypotheses_md),
                    )
                    (stage_dir / "core_ideas.md").write_text(refined_ideas_md, encoding="utf-8")
                    logger.info("S8: Synthesized idea mutation branches into final core ideas (%d chars)", len(refined_ideas_md))
        except Exception:  # noqa: BLE001
            logger.warning("S8: Idea mutation branching failed — keeping self-refined ideas", exc_info=True)

    # --- Multi-role idea panel: Innovator / Pragmatist / Critic (non-blocking) ---
    role_review_artifacts: tuple[str, ...] = ()
    role_review_md = ""
    if llm is not None and not use_local_stage8:
        try:
            role_review_md = _generate_role_review(
                llm,
                topic=config.research.topic,
                synthesis=_hypothesis_context,
                core_ideas_md=refined_ideas_md,
                evidence_pack=idea_evidence_pack,
                tournament_md=tournament_md,
            )
            if role_review_md.strip():
                (stage_dir / "idea_role_review.md").write_text(role_review_md, encoding="utf-8")
                role_review_artifacts = ("idea_role_review.md",)
                logger.info("S8: Generated multi-role idea review (%d chars)", len(role_review_md))
        except Exception:  # noqa: BLE001
            logger.warning("S8: Multi-role idea review failed — continuing", exc_info=True)

    # --- Idea reviewer quality gate and final revision (non-blocking) ---
    idea_review_md = ""
    if llm is not None and not use_local_stage8:
        try:
            idea_review_md = _generate_idea_review(
                llm,
                config.research.topic,
                _hypothesis_context,
                refined_ideas_md,
                idea_evidence_pack,
                idea_count,
            )
            if role_review_md.strip():
                idea_review_md = role_review_md.rstrip() + "\n\n---\n\n" + idea_review_md.lstrip()
            (stage_dir / "idea_review.md").write_text(idea_review_md, encoding="utf-8")
            logger.info("S8: Generated idea reviewer report (%d chars)", len(idea_review_md))
        except Exception:  # noqa: BLE001
            logger.warning("S8: Idea reviewer report failed — continuing without it", exc_info=True)

        if idea_review_md:
            try:
                final_ideas_md = _apply_idea_review(
                    llm,
                    config.research.topic,
                    _hypothesis_context,
                    refined_ideas_md,
                    idea_review_md,
                    idea_evidence_pack,
                    idea_count,
                )
                if final_ideas_md.strip():
                    refined_ideas_md = _complete_idea_set(
                        final_ideas_md,
                        topic=config.research.topic,
                        synthesis=_hypothesis_context,
                        idea_count=idea_count,
                        fallback_sources=(refined_ideas_md, core_ideas_md, raw_hypotheses_md),
                    )
                    (stage_dir / "core_ideas.md").write_text(refined_ideas_md, encoding="utf-8")
                    logger.info("S8: Applied idea review into final core ideas (%d chars)", len(refined_ideas_md))
            except Exception:  # noqa: BLE001
                logger.warning("S8: Applying idea review failed — keeping refined ideas", exc_info=True)

    # --- Literature-grounded pivot for weak/repetitive ideas (non-blocking) ---
    pivot_artifacts: tuple[str, ...] = ()
    try:
        pivoted_md, pivot_report_md = _maybe_literature_grounded_pivot(
            None if use_local_stage8 else llm,
            topic=config.research.topic,
            synthesis=_hypothesis_context,
            current_ideas_md=refined_ideas_md,
            evidence_pack=idea_evidence_pack,
            tournament_report=tournament_report,
            challenge_tree_md=challenge_tree_md,
            idea_count=idea_count,
        )
        if pivot_report_md.strip():
            (stage_dir / "idea_pivot.md").write_text(pivot_report_md, encoding="utf-8")
            pivot_artifacts = ("idea_pivot.md",)
        if pivoted_md.strip() and pivoted_md.strip() != refined_ideas_md.strip():
            refined_ideas_md = _complete_idea_set(
                pivoted_md,
                topic=config.research.topic,
                synthesis=synthesis or _hypothesis_context,
                idea_count=idea_count,
                fallback_sources=(refined_ideas_md, candidate_ideas_md, raw_hypotheses_md),
            )
            (stage_dir / "core_ideas.md").write_text(refined_ideas_md, encoding="utf-8")
            logger.info("S8: Literature-grounded pivot updated core ideas (%d chars)", len(refined_ideas_md))
    except Exception:  # noqa: BLE001
        logger.warning("S8: Literature-grounded pivot failed — keeping current ideas", exc_info=True)

    refined_ideas_md = _complete_idea_set(
        refined_ideas_md,
        topic=config.research.topic,
        synthesis=synthesis or _hypothesis_context,
        idea_count=idea_count,
        fallback_sources=(candidate_ideas_md, raw_hypotheses_md, core_ideas_md),
    )
    (stage_dir / "core_ideas.md").write_text(refined_ideas_md, encoding="utf-8")

    # --- Structured idea quality evaluation (non-blocking) ---
    idea_quality_artifacts: tuple[str, ...] = ()
    try:
        from researchclaw.evaluation.idea_quality import write_idea_quality_report

        if use_local_stage8:
            judge_llm, judge_model_name = None, "local-rules"
        else:
            judge_llm, judge_model_name = _create_idea_judge_llm(config)
        quality_report = write_idea_quality_report(
            refined_ideas_md,
            stage_dir / "idea_quality_scores.json",
            stage_dir / "idea_quality_summary.md",
            llm_judge=judge_llm,
            judge_model_name=judge_model_name,
        )
        judge = quality_report.get("llm_judge", {}) if isinstance(quality_report, dict) else {}
        if isinstance(judge, dict) and judge:
            judge_summary = judge.get("summary", {}) if isinstance(judge.get("summary"), dict) else {}
            judge_lines = [
                "",
                "---",
                "",
                "## LLM-as-Judge 二级评分",
                "",
                f"- Judge 模型：{judge.get('judge_model', judge_model_name)}",
                f"- 状态：{judge.get('status', 'failed')}",
            ]
            if judge.get("status") == "ok":
                judge_lines.extend([
                    f"- 强模型总体平均分：{judge_summary.get('overall_avg', 'N/A')}/5",
                    f"- 结论：{judge_summary.get('verdict', 'N/A')}",
                    f"- 主要理由：{judge_summary.get('main_reason', '')}",
                    "",
                    "| Idea | Overall | Novelty | Feasibility | Impact | Testability | Grounding | Risk | Compute | Diversity |",
                    "|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|",
                ])
                for item in judge.get("ideas", []):
                    if not isinstance(item, dict):
                        continue
                    judge_lines.append(
                        f"| {item.get('title', item.get('idea_id', ''))} | {item.get('overall', '')} | {item.get('novelty', '')} | {item.get('feasibility', '')} | {item.get('impact', '')} | {item.get('testability', '')} | {item.get('literature_grounding', '')} | {item.get('risk', '')} | {item.get('compute_cost', '')} | {item.get('diversity', '')} |"
                    )
                for item in judge.get("ideas", []):
                    if not isinstance(item, dict):
                        continue
                    fixes = item.get("required_fixes", [])
                    weaknesses = item.get("weaknesses", [])
                    if fixes or weaknesses:
                        judge_lines.extend(["", f"### {item.get('title', item.get('idea_id', ''))}"])
                        judge_lines.extend(f"- Weakness: {x}" for x in weaknesses[:4])
                        judge_lines.extend(f"- Required fix: {x}" for x in fixes[:4])
            else:
                judge_lines.append(f"- 错误：{judge.get('error', '')}")
            refined_ideas_md = refined_ideas_md.rstrip() + '\n' + '\n'.join(judge_lines).rstrip() + '\n'
            (stage_dir / "core_ideas.md").write_text(refined_ideas_md, encoding="utf-8")
        idea_quality_artifacts = ("idea_quality_scores.json", "idea_quality_summary.md")
        logger.info("S8: Wrote structured idea quality scores")
    except Exception:  # noqa: BLE001
        logger.warning("S8: Structured idea quality evaluation failed", exc_info=True)

    # --- Novelty check (non-blocking) ---
    novelty_artifacts: tuple[str, ...] = ()
    try:
        skip_external_novelty = use_local_stage8 or os.environ.get(
            "RESEARCHCLAW_SKIP_EXTERNAL_NOVELTY", ""
        ).lower() in {"1", "true", "yes"}
        if skip_external_novelty:
            novelty_report = {
                "novelty_score": None,
                "assessment": "skipped",
                "similar_papers": [],
                "recommendation": "External novelty APIs skipped for local validation or by RESEARCHCLAW_SKIP_EXTERNAL_NOVELTY.",
                "generated": _utcnow_iso(),
            }
        else:
            from researchclaw.literature.novelty import check_novelty  # noqa: PLC0415

            candidates_text = _read_prior_artifact(run_dir, "candidates.jsonl") or ""
            papers_seen = _parse_jsonl_rows(candidates_text) if candidates_text else []
            novelty_report = check_novelty(
                topic=config.research.topic,
                hypotheses_text=refined_ideas_md,
                papers_already_seen=papers_seen,
                s2_api_key=getattr(config.llm, "s2_api_key", ""),
            )
        (stage_dir / "novelty_report.json").write_text(
            json.dumps(novelty_report, indent=2, ensure_ascii=False),
            encoding="utf-8",
        )
        novelty_artifacts = ("novelty_report.json",)
        score = novelty_report.get("novelty_score")
        logger.info(
            "Novelty check: score=%s  assessment=%s  recommendation=%s",
            "N/A" if score is None else f"{score:.3f}",
            novelty_report.get("assessment", "unknown"),
            novelty_report.get("recommendation", ""),
        )
    except Exception:  # noqa: BLE001
        logger.warning("Novelty check failed (non-blocking)", exc_info=True)

    # --- Final handoff reflection checkpoint ---
    try:
        hw_tier = _hardware_profile.get("tier", "") if _hardware_profile else ""
        _add_reflection_checkpoint(
            stage_dir, "idea_handoff",
            topic=config.research.topic,
            context=f"Final ideas: {len(_idea_like_blocks(refined_ideas_md))}, "
                    f"Hardware tier: {hw_tier}, "
                    f"Total artifacts generated before handoff",
            dimensions=("handoff", "resource", "strategy"),
        )
    except Exception:  # noqa: BLE001
        pass

    # --- Final decision table, ideation memory update and YAML frontmatter ---
    decision_artifacts: tuple[str, ...] = ()
    memory_artifacts: tuple[str, ...] = ()
    try:
        decision_table_md = _write_idea_decision_table(stage_dir, refined_ideas_md, tournament_report)
        decision_artifacts = ("idea_decision_table.md",)
        memory_artifacts = _write_ideation_memory_update(
            config,
            run_dir,
            stage_dir,
            topic=config.research.topic,
            core_ideas_md=refined_ideas_md,
            decision_table_md=decision_table_md,
            tournament_report=tournament_report,
        )
        # --- YAML frontmatter for structured downstream consumption ---
        try:
            hw_tier = _hardware_profile.get("tier", "") if _hardware_profile else ""
            exp_mem_lessons = len(_load_experiment_memory(config, run_dir, max_chars=99999).split("## ")) - 1 if _load_experiment_memory(config, run_dir) else 0
            overall_score = 0.0
            quality_path = stage_dir / "idea_quality_scores.json"
            if quality_path.exists():
                qdata = json.loads(quality_path.read_text(encoding="utf-8"))
                overall_score = float(qdata.get("summary", {}).get("overall_avg", 0.0) or 0.0)
            frontmatter_ideas = _add_yaml_frontmatter_to_core_ideas(
                refined_ideas_md,
                topic=config.research.topic,
                hardware_tier=hw_tier,
                experiment_memory_lessons=exp_mem_lessons,
                overall_score=overall_score,
            )
            (stage_dir / "core_ideas.md").write_text(frontmatter_ideas, encoding="utf-8")
            logger.info("S8: Added YAML frontmatter to core_ideas.md")
        except Exception:  # noqa: BLE001
            logger.warning("S8: YAML frontmatter addition failed — keeping existing core_ideas.md", exc_info=True)
        logger.info("S8: Wrote idea decision table and ideation memory update")
    except Exception:  # noqa: BLE001
        logger.warning("S8: Decision table / ideation memory update failed", exc_info=True)

    return StageResult(
        stage=Stage.HYPOTHESIS_GEN,
        status=StageStatus.DONE,
        artifacts=(
            "hypotheses.md", "core_ideas.md", "hypotheses_raw.md", "idea_review.md",
            "idea_evidence_pack.md",
        ) + challenge_artifacts + candidate_artifacts + tournament_artifacts + retrieval_artifacts + branch_artifacts + role_review_artifacts + pivot_artifacts + idea_quality_artifacts + novelty_artifacts + decision_artifacts + memory_artifacts,
        evidence_refs=(
            "stage-08/hypotheses.md", "stage-08/core_ideas.md", "stage-08/challenge_insight_tree.md",
            "stage-08/idea_tournament.md", "stage-08/idea_decision_table.md",
            "stage-08/idea_review.md", "stage-08/idea_branch_synthesis.md", "stage-08/idea_quality_scores.json",
        ),
    )
