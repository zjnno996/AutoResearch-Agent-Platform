"""Bounded independent idea branches and synthesis."""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, Sequence

from researchclaw.llm.client import LLMClient
from researchclaw.pipeline.ideation.schema import _idea_quality_rubric, _idea_schema_instruction

logger = logging.getLogger(__name__)


def _run_parallel(fn, items: Sequence[Any], *, max_workers: int = 4) -> dict[Any, Any]:
    """Run a callable over items in parallel using ThreadPoolExecutor.

    Used for independent sub-stages like mutation branches and role reviews.
    Returns dict mapping item -> result (or item -> None on failure).
    """
    from concurrent.futures import ThreadPoolExecutor, as_completed
    from contextvars import copy_context

    results: dict[Any, Any] = {item: None for item in items}
    with ThreadPoolExecutor(max_workers=max_workers) as pool:
        fut_map = {pool.submit(copy_context().run, fn, item): item for item in items}
        for fut in as_completed(fut_map):
            item = fut_map[fut]
            try:
                results[item] = fut.result()
            except Exception as exc:  # noqa: BLE001
                logger.warning("Parallel task for %s failed: %s", item, exc)
    return results


def _idea_branch_specs() -> dict[str, dict[str, str]]:
    return {
        "high_risk_high_reward": {
            "title": "高风险高收益版",
            "lens": (
                "把原始 idea 推向更有突破性的版本。允许更大胆的机制假设、跨领域组合和反直觉路线，"
                "但必须明确高风险来自哪里、最小证据是什么、如何避免变成不可验证幻想。"
            ),
            "priority": "优先最大化 Novelty 和 Impact，同时保留 Testability 底线。",
        },
        "conservative_publishable": {
            "title": "稳妥可投版",
            "lens": (
                "把原始 idea 改成更容易形成稳定论文贡献的版本。强调清晰 problem gap、强 baseline、"
                "可复现协议、审稿人可接受的 novelty defense 和较低工程不确定性。"
            ),
            "priority": "优先最大化 Feasibility、Literature Grounding 和 reviewer defense。",
        },
        "mvp_fast_validation": {
            "title": "两周 MVP 版",
            "lens": (
                "把原始 idea 压缩成两周内能看到信号的最小实验。减少依赖、缩小数据和模型规模，"
                "设计明确 Go/No-Go 阈值，让失败也能产出有用结论。"
            ),
            "priority": "优先最大化 Testability、计算可控性和早期反馈速度。",
        },
    }


def _generate_single_mutation_branch(
    branch_id: str,
    spec: dict[str, str],
    llm: LLMClient,
    topic: str,
    synthesis: str,
    core_ideas_md: str,
    evidence_pack: str,
    branches_dir: Path,
    idea_count: int = 5,
) -> tuple[str, str] | None:
    """Generate a single mutation branch — extracted for parallel execution."""
    system = (
        "你是科研 idea 演化专家。你的任务是基于同一批 core ideas，"
        "按照指定策略生成一个差异明显的变体分支。必须使用中文输出，"
        "英文只保留论文名、方法名、数据集、指标和模型名。"
    )
    user = (
        f"研究主题：\n{topic}\n\n"
        f"分支名称：{spec['title']}\n"
        f"分支视角：{spec['lens']}\n"
        f"优化优先级：{spec['priority']}\n\n"
        f"请把原始 core ideas 演化成该分支下最强的 {idea_count} 个互不重复的 Idea。要求：\n"
        "- 不能只是改标题，必须改变取舍、实验规模、风险处理或技术路线。\n- 每个 Idea 必须是不同机制；如果两个 Idea 只是同一机制换场景，合并或替换。\n"
        "- 每个 Idea 必须说明相对原始版本做了哪些 mutation。\n"
        "- 每个 Idea 必须保留相近文献对照、失败条件和评分。\n"
        "- 如果某个原始 Idea 不适合本分支，明确说明删除或合并原因。\n\n"
        f"{_idea_schema_instruction()}\n\n"
        f"{_idea_quality_rubric()}\n\n"
        "额外输出要求：\n"
        "## 分支策略说明\n"
        "说明这个分支相对原始版本的主要取舍。\n\n"
        "## Mutation Log\n"
        "逐条说明：保留了什么、强化了什么、删除/合并了什么、为什么。\n\n"
        f"文献综述摘要：\n{synthesis[:8000]}\n\n"
        f"证据包：\n{evidence_pack[:10000] if evidence_pack else '没有额外证据包。'}\n\n"
        f"原始 core ideas：\n{core_ideas_md[:16000]}\n"
    )
    try:
        resp = llm.chat(
            [{"role": "user", "content": user}],
            system=system,
            max_tokens=10000,
        )
        text = resp.content.strip()
        if text:
            (branches_dir / f"{branch_id}.md").write_text(text, encoding="utf-8")
            logger.info("S8: Idea mutation branch '%s' generated (%d chars)", branch_id, len(text))
            return (branch_id, text)
    except Exception:  # noqa: BLE001
        logger.warning("S8: Mutation branch '%s' failed", branch_id, exc_info=True)
    return None


def _generate_idea_mutation_branches(
    llm: LLMClient,
    topic: str,
    synthesis: str,
    core_ideas_md: str,
    evidence_pack: str,
    branches_dir: Path,
    idea_count: int = 5,
) -> dict[str, str]:
    """Generate multiple mutation branches for Stage 8 ideas — branches run in parallel."""
    branches_dir.mkdir(parents=True, exist_ok=True)
    specs = _idea_branch_specs()
    results: dict[str, str] = {}

    # Prepare partial function for each branch
    def _run_branch(bid: str) -> tuple[str, str] | None:
        return _generate_single_mutation_branch(
            bid, specs[bid], llm, topic, synthesis, core_ideas_md,
            evidence_pack, branches_dir, idea_count,
        )

    for result in _run_parallel(_run_branch, list(specs), max_workers=3).values():
        if result is not None:
            results[result[0]] = result[1]

    return results


def _synthesize_idea_mutation_branches(
    llm: LLMClient,
    topic: str,
    synthesis: str,
    branches: dict[str, str],
    evidence_pack: str,
    idea_count: int = 5,
) -> str:
    """Fuse mutation branches into the final executable core ideas."""
    branch_titles = _idea_branch_specs()
    branch_parts: list[str] = []
    for branch_id, text in branches.items():
        title = branch_titles.get(branch_id, {}).get("title", branch_id)
        branch_parts.append(f"## 分支：{title} ({branch_id})\n{text[:12000]}")
    combined = "\n\n---\n\n".join(branch_parts)
    system = (
        "你是科研项目 PI，负责从多个 idea 演化分支中做最终取舍。"
        "你必须综合，而不是平均：保留最强机制，删除重复和虚弱方案，"
        "输出可以直接进入实验设计阶段的中文版 core_ideas.md。"
    )
    user = (
        f"研究主题：\n{topic}\n\n"
        "下面有三个分支：高风险高收益版、稳妥可投版、两周 MVP 版。"
        f"请做最终分支融合，输出 exactly {idea_count} 个最终 Idea。每个 Idea 必须是不同技术机制；如果两个 Idea 机制重复，必须合并后补一个新的非重复候选，而不是重复凑数。\n\n"
        "融合规则：\n"
        "- 每个最终 Idea 必须说明来自哪个分支、吸收了哪些设计、舍弃了哪些设计。\n- 输出前必须做去重：同一机制换标题/换数据集/换指标不算新 Idea。\n"
        "- 至少保留一个偏高收益的创新点，但必须配一个两周 MVP 验证路径。\n"
        "- 如果某个高风险 Idea 太虚，降级为未来方向，不要推荐为首选。\n"
        "- 首选推荐必须同时满足：有相近文献差异、有最小实验、失败阈值明确、两周内能启动。\n"
        "- 最终全文使用中文。英文只保留专有名词。\n\n"
        f"{_idea_schema_instruction()}\n\n"
        f"{_idea_quality_rubric()}\n\n"
        "最终还必须额外包含：\n"
        "## 分支融合记录\n"
        "用表格说明每个最终 Idea 吸收了哪些分支元素，以及为什么这样融合。\n\n"
        "## 未采纳分支与原因\n"
        "列出被删除或降级的想法，以及删除原因。\n\n"
        f"文献综述摘要：\n{synthesis[:7000]}\n\n"
        f"证据包：\n{evidence_pack[:9000] if evidence_pack else '没有额外证据包。'}\n\n"
        f"分支内容：\n{combined}\n"
    )
    resp = llm.chat(
        [{"role": "user", "content": user}],
        system=system,
        max_tokens=12000,
    )
    return resp.content.strip()
