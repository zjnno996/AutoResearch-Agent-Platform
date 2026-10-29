"""Support model_roles components."""

from __future__ import annotations

import logging
from pathlib import Path

from researchclaw.llm.client import LLMClient
from researchclaw.pipeline.ideation.text import _idea_count_rule
from researchclaw.prompts import PromptManager

logger = logging.getLogger(__name__)


def _multi_perspective_generate(
    llm: LLMClient,
    roles: dict[str, dict[str, str]],
    variables: dict[str, str],
    perspectives_dir: Path,
) -> dict[str, str]:
    """Generate outputs from multiple debate perspectives.

    Each role has its own system/user prompt. Outputs are saved to
    *perspectives_dir* and returned as ``{role_name: response_text}``.
    """
    from researchclaw.prompts import _render  # noqa: PLC0415

    perspectives_dir.mkdir(parents=True, exist_ok=True)
    results: dict[str, str] = {}
    for role_name, role_prompts in roles.items():
        try:
            system = _render(role_prompts["system"], variables) + "\n必须用中文输出。保留必要英文术语，但论证、分析和建议用中文。"
            user = (
                _render(role_prompts["user"], variables)
                + "\n\n## 输出语言要求\n"
                + "请用中文生成所有假设、论证、实验设计和风险分析。英文只用于论文名、方法名、数据集名、指标名等专有名词。"
                + (
                    f"\n\n## 数量与去重要求\n{_idea_count_rule(int(variables.get('idea_count', '5')))}"
                    if str(variables.get('idea_count', '')).isdigit()
                    else ""
                )
            )
            resp = llm.chat(
                [{"role": "user", "content": user}],
                system=system,
            )
            results[role_name] = resp.content
            (perspectives_dir / f"{role_name}.md").write_text(
                resp.content, encoding="utf-8"
            )
            logger.info("Debate perspective '%s' generated (%d chars)", role_name, len(resp.content))
        except Exception as exc:  # noqa: BLE001
            logger.warning("Debate perspective '%s' failed: %s", role_name, exc)
    if len(results) < 2:
        logger.error("Multi-perspective debate: only %d/%d roles succeeded", len(results), len(roles))
    return results


def _synthesize_perspectives(
    llm: LLMClient,
    perspectives: dict[str, str],
    sub_prompt_name: str,
    prompts: PromptManager,
    idea_count: int = 5,
) -> str:
    """Synthesize multiple perspective outputs into a unified result."""
    parts = []
    for role_name, text in perspectives.items():
        parts.append(f"### Perspective: {role_name}\n{text}")
    combined = "\n\n---\n\n".join(parts)
    sp = prompts.sub_prompt(sub_prompt_name, perspectives=combined)
    user = (
        sp.user
        + "\n\n## 输出语言要求\n"
        + "请用中文综合为最终假设和推荐方案。保留必要英文专有名词，但章节说明、判断和实验计划必须是中文。"
        + f"\n\n## 数量与去重要求\n{_idea_count_rule(idea_count)}"
    )
    system = sp.system + "\n必须用中文输出最终假设综合结果。"
    resp = llm.chat(
        [{"role": "user", "content": user}],
        system=system,
    )
    return resp.content
