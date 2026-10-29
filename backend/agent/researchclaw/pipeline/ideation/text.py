"""Ideation text components."""

from __future__ import annotations

import logging
import re
from typing import Sequence

from researchclaw.config import RCConfig
from researchclaw.pipeline.support.artifacts import _utcnow_iso

logger = logging.getLogger(__name__)


def _clamp_idea_count(value: object) -> int:
    try:
        count = int(value)
    except (TypeError, ValueError):
        count = 5
    return max(1, min(8, count))


def _configured_idea_count(config: RCConfig | None) -> int:
    return _clamp_idea_count(getattr(getattr(config, "research", None), "idea_count", 5))


_IDEA_LIKE_HEADING_RE = re.compile(
    r"^##\s+(Idea\s*\d+|H\s*\d+|想法\s*\d+|假设\s*\d+|Idea\s*[一二三四五六七八九十]+|[0-9]+[.、])[:：\s-]*(.+)?$",
    re.M,
)


def _idea_like_blocks(text: str) -> list[tuple[str, str]]:
    matches = list(_IDEA_LIKE_HEADING_RE.finditer(text or ""))
    blocks: list[tuple[str, str]] = []
    for idx, match in enumerate(matches):
        start = match.start()
        end = matches[idx + 1].start() if idx + 1 < len(matches) else len(text)
        title = (match.group(2) or match.group(1) or f"Idea {idx + 1}").strip()
        block = text[start:end].strip()
        non_idea_heading = re.search(
            r"\n##\s+(?!(?:Idea|H|想法|假设)\s*(?:\d+|[一二三四五六七八九十])|[0-9]+[.、])",
            block,
            flags=re.I,
        )
        if non_idea_heading:
            block = block[:non_idea_heading.start()].strip()
        blocks.append((title, block))
    return blocks


def _idea_like_count(text: str) -> int:
    return len(_idea_like_blocks(text))


def _idea_text_tokens(text: str) -> set[str]:
    raw = re.findall(r"[A-Za-z0-9][A-Za-z0-9_.+-]{1,}|[\u4e00-\u9fff]{2,}", (text or "").lower())
    stop = {"idea", "hypothesis", "method", "model", "dataset", "baseline", "metric", "risk", "核心", "假设", "实验", "方法", "模型", "数据集"}
    tokens = {tok for tok in raw if tok not in stop and len(tok) >= 2}
    chinese = "".join(re.findall(r"[\u4e00-\u9fff]", text or ""))
    tokens.update(chinese[i:i + 2] for i in range(max(0, len(chinese) - 1)))
    return tokens


def _idea_text_similarity(left: str, right: str) -> float:
    lt, rt = _idea_text_tokens(left), _idea_text_tokens(right)
    if not lt or not rt:
        return 0.0
    overlap = len(lt & rt)
    jaccard = overlap / max(len(lt | rt), 1)
    containment = overlap / max(min(len(lt), len(rt)), 1)
    return max(jaccard, containment)


def _renumber_idea_blocks(blocks: list[tuple[str, str]]) -> str:
    out: list[str] = ["# Core Ideas"]
    for idx, (title, block) in enumerate(blocks, start=1):
        body = block.splitlines()
        if body and body[0].lstrip().startswith("##"):
            body = body[1:]
        out.append(f"## Idea {idx}：{title}")
        out.append("\n".join(body).strip())
    return "\n\n".join(part for part in out if part).strip() + "\n"


def _complete_idea_set(
    candidate_md: str,
    *,
    topic: str,
    synthesis: str,
    idea_count: int,
    fallback_sources: Sequence[str] = (),
) -> str:
    """Prevent LLM refinement/review steps from collapsing a multi-idea set."""
    target = _clamp_idea_count(idea_count)
    blocks = _idea_like_blocks(candidate_md)
    original_count = len(blocks)
    if original_count >= target:
        return candidate_md

    for source in (*fallback_sources, _fallback_hypotheses_from_synthesis(topic, synthesis, target)):
        for title, block in _idea_like_blocks(source):
            if len(blocks) >= target:
                break
            if any(
                _idea_text_similarity(block, existing) >= 0.42
                or _idea_text_similarity(title, existing_title) >= 0.34
                for existing_title, existing in blocks
            ):
                continue
            blocks.append((title, block))
        if len(blocks) >= target:
            break

    if len(blocks) >= target:
        logger.warning(
            "S8: Candidate contained only %d idea(s); completed final set to %d using prior/fallback ideas",
            original_count,
            len(blocks),
        )
        return _renumber_idea_blocks(blocks[:target])
    return candidate_md


def _idea_count_rule(idea_count: int) -> str:
    return (
        f"目标输出 exactly {idea_count} 个最终 Idea。每个 Idea 必须是不同技术机制、不同验证路径或不同问题切入点；"
        "如果两个 Idea 只是同一机制换标题/换应用场景，必须合并或替换，不能重复凑数。"
    )


def _default_hypotheses(topic: str, idea_count: int = 5) -> str:
    ideas = [
        (
            "协议控制提升稳定性",
            f"针对 {topic}，更严格的实验协议控制能够降低随机种子、数据划分和负载波动带来的指标方差，使结论更可复现。",
        ),
        (
            "鲁棒性目标提升泛化",
            f"针对 {topic}，在主任务目标之外加入鲁棒性约束，可以提升分布外场景表现，同时不显著损害分布内性能。",
        ),
        (
            "组合方案优于单一组件",
            "在固定计算预算下，将协议控制与鲁棒性目标结合，应当比任一单独组件获得更稳定的综合收益。",
        ),
        (
            "数据/负载分层揭示隐藏失败模式",
            f"针对 {topic}，按样本难度、负载区间或场景类型做分层评估，可能发现平均指标掩盖的系统性失败模式，并指导更有针对性的改进。",
        ),
        (
            "轻量自适应机制优于全量复杂重构",
            f"针对 {topic}，在既有 pipeline 上加入轻量自适应组件，可能以更低工程成本获得接近复杂端到端重构的收益。",
        ),
        (
            "失败簇驱动的定向修复优于平均优化",
            f"针对 {topic}，先定位高频失败簇再定向修复，可能比直接优化整体平均指标更快提升最差场景表现。",
        ),
        (
            "跨数据源一致性检查提升可信度",
            f"针对 {topic}，把多来源证据、日志或 benchmark 结果做一致性校验，可以减少偶然数据偏差导致的错误结论。",
        ),
        (
            "人类反馈约束减少不可执行想法",
            f"针对 {topic}，在自动化研究流程中加入轻量人工反馈约束，可能显著减少不可验证或工程成本过高的研究想法。",
        ),
    ]
    blocks = ["# 研究假设", ""]
    for idx, (title, body) in enumerate(ideas[:_clamp_idea_count(idea_count)], start=1):
        blocks.append(f"## H{idx}：{title}\n{body}")
    blocks.append(f"## 生成时间\n{_utcnow_iso()}")
    return "\n\n".join(blocks) + "\n"


def _fallback_hypotheses_from_synthesis(topic: str, synthesis: str, idea_count: int = 5) -> str:
    """Create usable Stage 8 output when all LLM calls are unavailable."""
    synthesis_excerpt = synthesis.strip()
    if len(synthesis_excerpt) > 3000:
        synthesis_excerpt = synthesis_excerpt[:3000].rsplit("\n", 1)[0].strip()
    evidence_note = (
        synthesis_excerpt
        if synthesis_excerpt
        else "前序阶段已完成，但没有可用的详细综述文本。"
    )
    ideas = [
        ("面向主题的关键瓶颈建模", "现有研究通常分别优化单个组件，但对不同瓶颈之间的相互作用刻画不足。", "如果显式建模主要瓶颈之间的耦合关系，就能在不显著增加系统复杂度的前提下提升整体表现。", "选取一个可控子任务，对比标准基线、单组件优化方案和耦合建模方案。"),
        ("动态资源分配提升效率", "静态配置难以适应不同样本、请求或实验条件的变化。", "基于输入特征或运行时信号的动态分配策略，可以提升资源利用率并降低尾部延迟或失败概率。", "构造混合难度/混合长度的测试集，对比静态策略、常规动态策略和预测引导策略。"),
        ("标准化评估协议揭示真实收益", "相关工作常使用不同数据、硬件、负载和指标，导致方法优劣难以直接比较。", "建立覆盖关键变量的标准化评估协议，可以区分真正可迁移的改进和只在特定设置下有效的优化。", "搭建小型 benchmark harness，扫描至少两个关键变量，并评估两个基线加一个新策略。"),
        ("失败案例驱动的鲁棒性改进", "平均性能优化往往忽略最容易失败的边界场景，导致方法上线或跨域迁移时不稳定。", "先挖掘失败簇，再针对失败簇设计轻量修正机制，可以比盲目扩大模型或数据更高效地提升鲁棒性。", "抽取失败样本，聚类成 2-3 类错误模式，并对比基线与失败感知修正方案。"),
        ("轻量代理/控制器增强主系统", "完整重构主方法成本高，且难以定位收益来源。", "在主系统外加入轻量代理、调度器或校验器，可以在不改变主体模型的情况下提升效率、稳定性或可解释性。", "实现一个规则或小模型控制器，对比无控制器、静态控制器和自适应控制器三组。"),
        ("跨源证据一致性过滤", "单一文献源或单一 benchmark 可能放大偶然结论。", "把论文证据、公开实现和小规模复现实验做一致性过滤，可以更早排除伪 novelty。", "对 shortlist 论文构建 evidence matrix，并用小规模 sanity run 验证排名最高的两个机制。"),
        ("约束生成减少重复 Idea", "自动生成的研究想法容易出现同一机制换名、换场景的重复。", "在生成阶段加入机制指纹和相似度惩罚，可以提升候选集合的多样性和可选择性。", "对比无约束生成、prompt 约束生成和相似度过滤生成的重复率与人工可用率。"),
        ("人工反馈闭环提升可执行性", "完全自动的 idea 选择常忽视本地资源、代码基础和用户偏好。", "在关键决策点加入轻量人工偏好反馈，可以提高首选 idea 的可执行性并减少后续重跑。", "让用户对候选 idea 做一次快速排序，对比反馈前后的 S9 方案可执行度。"),
    ]
    blocks = [
        "# 研究假设",
        "",
        "> 本文件由本地 fallback 生成，因为 S8 阶段配置的 LLM 调用不可用。",
        "> 这些内容是可继续细化的种子 Idea，建议在模型端点稳定后再做一次 LLM 精炼。",
        "",
    ]
    for idx, (title, gap, claim, experiment) in enumerate(ideas[:_clamp_idea_count(idea_count)], start=1):
        blocks.append(
            f"## H{idx}：{title}\n"
            f"- 问题缺口：针对 {topic}，{gap}\n"
            f"- 假设：{claim}\n"
            f"- 最小实验：{experiment}\n"
            "- 指标：主任务性能、方差、资源消耗、失败率，以及关键场景下的稳定性。\n"
            "- 计算预算：优先设计为单卡或小规模子集 2 周内可验证。\n"
            "- 主要风险：需要用独立场景或严格消融确认收益不是来自数据偏差或额外计算。"
        )
    blocks.extend([
        "## 推荐优先尝试",
        "优先选择计算成本低、机制最清晰、与其他候选重复度最低的 Idea；若两个候选机制相同，应合并而不是重复推进。",
        "",
        "## 使用的证据",
        evidence_note,
        "",
        "## 生成时间",
        _utcnow_iso(),
    ])
    return "\n\n".join(blocks) + "\n"


def _idea_title_slug(title: str) -> str:
    cleaned = re.sub(r"\s+", " ", (title or "").strip())
    cleaned = re.sub(r"^[#\s0-9IdeaH想法假设一二三四五六七八九十：:.-]+", "", cleaned).strip()
    return cleaned[:80] or "未命名 Idea"


def _add_yaml_frontmatter_to_core_ideas(core_ideas_md: str, *, topic: str, hardware_tier: str = "",
                                         experiment_memory_lessons: int = 0, overall_score: float = 0.0) -> str:
    """Prepend structured YAML frontmatter to core_ideas.md for downstream automation."""
    safe_topic = topic.replace('"', "'").replace("\n", " ")[:120]
    frontmatter = "---\n"
    frontmatter += f'topic: "{safe_topic}"\n'
    frontmatter += f"generated_at: {_utcnow_iso()}\n"
    frontmatter += f"total_ideas: {len(_idea_like_blocks(core_ideas_md))}\n"
    frontmatter += f"overall_avg_score: {overall_score}\n"
    if hardware_tier:
        frontmatter += f"hardware_tier: \"{hardware_tier}\"\n"
    if experiment_memory_lessons > 0:
        frontmatter += f"experiment_memory_lessons: {experiment_memory_lessons}\n"
    # Extract unmet signals and skill suggestions from content if present
    unmet_signals = _extract_frontmatter_list(core_ideas_md, "unmet_success_signals")
    if unmet_signals:
        for s in unmet_signals:
            cleaned = s.replace('"', "'")
            frontmatter += '  - "' + cleaned[:80] + '"\n'
    skill_suggestions = _extract_frontmatter_list(core_ideas_md, "skill_suggestions")
    if skill_suggestions:
        frontmatter += "skill_suggestions:\n"
        for s in skill_suggestions:
            cleaned = s.replace('"', "'")
            frontmatter += '  - "' + cleaned[:80] + '"\n'
    frontmatter += "---\n\n"
    # Only prepend if not already present
    if core_ideas_md.strip().startswith("---"):
        return core_ideas_md
    return frontmatter + core_ideas_md


def _extract_frontmatter_list(text: str, key: str) -> list[str]:
    """Extract a list of items from a key in the markdown text (JSON-schema-like pattern)."""
    pattern = rf'{key}:\s*\[(.*?)\]'
    match = re.search(pattern, text, re.DOTALL)
    if not match:
        return []
    raw = match.group(1)
    items = re.findall(r'"([^"]*)"', raw)
    return [item.strip() for item in items if item.strip()]
