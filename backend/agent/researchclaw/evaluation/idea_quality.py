"""Structured quality evaluation for Stage 8 research ideas."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, asdict
from pathlib import Path
from typing import Any

_DIMENSIONS = ('novelty', 'feasibility', 'impact', 'testability', 'literature_grounding', 'risk', 'compute_cost', 'diversity')

@dataclass
class IdeaScore:
    idea_id: str
    title: str
    novelty: float
    feasibility: float
    impact: float
    testability: float
    literature_grounding: float
    risk: float
    compute_cost: float
    diversity: float
    duplicate_with: list[str]
    overall: float
    evidence_count: int
    missing_sections: list[str]
    notes: list[str]


def _sections(text: str) -> list[tuple[str, str]]:
    matches = list(re.finditer(r"^##\s+(Idea\s*\d+|H\s*\d+|想法\s*\d+|假设\s*\d+|Idea\s*[一二三四五六七八九十]+|[0-9]+[.、])[:：\s-]*(.+)$", text, re.M))
    if not matches:
        return [('idea-1', text[:80].strip() or '整体方案', text)]  # type: ignore[return-value]
    out: list[tuple[str, str, str]] = []
    for idx, m in enumerate(matches):
        start = m.start()
        end = matches[idx + 1].start() if idx + 1 < len(matches) else len(text)
        out.append((f'idea-{idx + 1}', m.group(2).strip() or m.group(1).strip(), text[start:end]))
    return out  # type: ignore[return-value]


def _has_any(body: str, patterns: list[str]) -> bool:
    lower = body.lower()
    return any(p.lower() in lower for p in patterns)


def _tokens(text: str) -> set[str]:
    raw = re.findall(r"[A-Za-z0-9][A-Za-z0-9_.+-]{1,}|[\u4e00-\u9fff]{2,}", text.lower())
    stop = {'idea', 'hypothesis', 'method', 'model', 'dataset', 'baseline', 'metric', 'risk', '核心', '假设', '实验', '方法', '模型', '数据集'}
    tokens = {tok for tok in raw if tok not in stop and len(tok) >= 2}
    # Add Chinese character bigrams so near-duplicate Chinese ideas are easier to catch.
    chinese = ''.join(re.findall(r"[\u4e00-\u9fff]", text))
    tokens.update(chinese[i:i + 2] for i in range(max(0, len(chinese) - 1)))
    return tokens


def _similarity(a: str, b: str) -> float:
    ta, tb = _tokens(a), _tokens(b)
    if not ta or not tb:
        return 0.0
    return round(len(ta & tb) / max(len(ta | tb), 1), 3)


def _score_idea(idea_id: str, title: str, body: str) -> IdeaScore:
    required = {
        '核心假设': ['核心假设', 'claim', 'hypothesis'],
        '文献依据': ['文献依据', '相近文献', 'prior work', 'literature'],
        '技术路线': ['技术路线', 'technical', 'method', '机制'],
        '可验证实验': ['可验证实验', '实验', 'baseline', 'metric', '指标'],
        '两周 MVP': ['两周', 'mvp', 'go/no-go', 'go / no-go'],
        '风险失败条件': ['风险', '失败', 'failure', '反例'],
        '评分': ['评分', 'novelty', 'feasibility', 'testability'],
        '计算成本': ['计算预算', 'compute', 'gpu', '成本', 'hours', '小时'],
    }
    missing = [name for name, pats in required.items() if not _has_any(body, pats)]
    evidence_count = len(re.findall(r"(?:\b20\d{2}\b|arXiv|NeurIPS|ICLR|ICML|ACL|EMNLP|CVPR|Semantic Scholar|OpenReview|等\))", body))
    table_like = body.count('|') >= 8
    has_threshold = _has_any(body, ['失败阈值', 'failure condition', 'reject', 'go/no-go', 'go / no-go'])
    has_baseline = _has_any(body, ['baseline', '基线', '对照'])
    has_metric = _has_any(body, ['metric', '指标', 'accuracy', 'f1', 'auc', 'latency'])
    has_dataset = _has_any(body, ['dataset', '数据集', 'split', '划分'])
    has_novelty = _has_any(body, ['新颖', 'novelty', '不同点', '差异', 'reviewer'])
    has_risk = _has_any(body, ['风险', '失败', '反例', 'fallback'])
    has_compute = _has_any(body, ['计算预算', 'compute', 'gpu', 'gpu hours', '小时', '成本'])
    has_low_compute = _has_any(body, ['两周', 'mvp', '单卡', 'single gpu', '<1 day', '小规模', '子集', '轻量'])
    has_high_compute_risk = _has_any(body, ['多机', '集群', '数周', '大规模预训练', 'hundreds of gpu', 'thousands of gpu'])

    novelty = 2.0 + (1.2 if has_novelty else 0) + min(evidence_count, 5) * 0.25 + (0.4 if table_like else 0)
    feasibility = 2.0 + (0.8 if has_dataset else 0) + (0.8 if has_baseline else 0) + (0.7 if _has_any(body, ['两周', 'mvp', '计算预算', 'gpu']) else 0)
    impact = 2.4 + (0.8 if _has_any(body, ['贡献', 'impact', '价值', '论文']) else 0) + (0.5 if has_novelty else 0)
    testability = 1.8 + (0.8 if has_metric else 0) + (0.8 if has_baseline else 0) + (0.8 if has_threshold else 0) + (0.4 if _has_any(body, ['消融', 'ablation']) else 0)
    grounding = 1.6 + min(evidence_count, 6) * 0.45 + (0.6 if has_baseline else 0)
    risk = 2.0 + (1.1 if has_risk else 0) + (0.7 if has_threshold else 0) + (0.4 if _has_any(body, ['fallback', '备选']) else 0)
    compute_cost = 1.7 + (1.0 if has_compute else 0) + (1.2 if has_low_compute else 0) - (1.0 if has_high_compute_risk else 0)
    vals = [min(5.0, round(v, 2)) for v in (novelty, feasibility, impact, testability, grounding, risk, compute_cost)]
    diversity = 5.0
    overall = round((sum(vals) + diversity) / (len(vals) + 1), 2)
    notes: list[str] = []
    if evidence_count < 3:
        notes.append('相近文献/年份/会议线索偏少，novelty defense 可能不足。')
    if not has_threshold:
        notes.append('缺少明确失败阈值或 Go/No-Go 标准。')
    if not has_compute:
        notes.append('缺少明确计算预算或 GPU 小时估计。')
    if missing:
        notes.append('缺失结构字段：' + '、'.join(missing))
    return IdeaScore(
        idea_id=idea_id,
        title=title[:120],
        novelty=vals[0],
        feasibility=vals[1],
        impact=vals[2],
        testability=vals[3],
        literature_grounding=vals[4],
        risk=vals[5],
        compute_cost=vals[6],
        diversity=diversity,
        duplicate_with=[],
        overall=overall,
        evidence_count=evidence_count,
        missing_sections=missing,
        notes=notes,
    )


def evaluate_ideas(core_ideas_md: str) -> dict[str, Any]:
    sections = _sections(core_ideas_md)
    ideas = [_score_idea(idea_id, title, body) for idea_id, title, body in sections]
    duplicate_pairs: list[dict[str, Any]] = []
    max_similarities = [0.0 for _ in ideas]
    for i, left in enumerate(sections):
        for j in range(i + 1, len(sections)):
            sim = _similarity(left[2], sections[j][2])
            max_similarities[i] = max(max_similarities[i], sim)
            max_similarities[j] = max(max_similarities[j], sim)
            if sim >= 0.42:
                ideas[i].duplicate_with.append(ideas[j].idea_id)
                ideas[j].duplicate_with.append(ideas[i].idea_id)
                duplicate_pairs.append({
                    'idea_a': ideas[i].idea_id,
                    'title_a': ideas[i].title,
                    'idea_b': ideas[j].idea_id,
                    'title_b': ideas[j].title,
                    'similarity': sim,
                })
    for idea, max_sim in zip(ideas, max_similarities):
        idea.diversity = round(max(1.0, min(5.0, 5.0 - max_sim * 6.0)), 2)
        if idea.duplicate_with:
            idea.notes.append('疑似与其他 Idea 机制重复：' + '、'.join(idea.duplicate_with))
        vals = [getattr(idea, dim) for dim in _DIMENSIONS]
        idea.overall = round(sum(vals) / len(vals), 2)
    summary = {
        'idea_count': len(ideas),
        'overall_avg': round(sum(i.overall for i in ideas) / max(len(ideas), 1), 2),
        'dimension_avg': {
            dim: round(sum(getattr(i, dim) for i in ideas) / max(len(ideas), 1), 2)
            for dim in _DIMENSIONS
        },
        'diversity_avg': round(sum(i.diversity for i in ideas) / max(len(ideas), 1), 2),
        'duplicate_pair_count': len(duplicate_pairs),
        'duplicate_pairs': duplicate_pairs,
        'best_idea': max(ideas, key=lambda i: i.overall).title if ideas else '',
    }
    return {'summary': summary, 'ideas': [asdict(i) for i in ideas]}


def _safe_json_object(text: str) -> dict[str, Any]:
    text = text.strip()
    if not text:
        return {}
    try:
        data = json.loads(text)
        return data if isinstance(data, dict) else {}
    except json.JSONDecodeError:
        pass
    match = re.search(r"\{.*\}", text, re.S)
    if not match:
        return {}
    try:
        data = json.loads(match.group(0))
    except json.JSONDecodeError:
        return {}
    return data if isinstance(data, dict) else {}


def _normalize_score(value: Any) -> float:
    try:
        score = float(value)
    except (TypeError, ValueError):
        return 0.0
    if score > 5 and score <= 10:
        score = score / 2.0
    return round(max(1.0, min(5.0, score)), 2)


def evaluate_ideas_with_llm_judge(core_ideas_md: str, llm: Any, *, model_name: str = "Qwen3.5-122B-A10B-FP8") -> dict[str, Any]:
    """Use a strong LLM as a second-stage judge for idea quality.

    The judge is asked to return strict JSON. The function is deliberately
    independent from a concrete client type: it only requires ``llm.chat``.
    """
    rule_report = evaluate_ideas(core_ideas_md)
    rubric = """
你是顶级会议 Area Chair，负责评估自动化科研 Agent 生成的 research ideas。
请严格按 1-5 分评分，5 分代表顶会级强信号，1 分代表不可用。
评分维度：
- novelty：是否有真实新意，是否能抵抗“已有工作已做过”的质疑。
- feasibility：两周 MVP 和当前资源下是否能启动验证。
- impact：如果成立，是否有论文贡献或明确系统价值。
- testability：是否有清晰 baseline、dataset、metric、失败阈值和可证伪实验。
- literature_grounding：是否有相近文献、差异点和 evidence support。
- risk：是否识别关键失败模式、早停信号和 fallback。
- compute_cost：计算成本是否低且估计明确；5 分代表低成本/单卡可验证，1 分代表昂贵或未说明。
- diversity：该 idea 与其他 idea 是否是不同技术机制；重复换皮必须低分。

只输出 JSON，格式如下：
{
  "judge_model": "Qwen3.5-122B-A10B-FP8",
  "summary": {
    "overall_avg": 3.8,
    "best_idea": "...",
    "verdict": "accept/revise/reject",
    "main_reason": "..."
  },
  "ideas": [
    {
      "idea_id": "idea-1",
      "title": "...",
      "novelty": 4,
      "feasibility": 3,
      "impact": 4,
      "testability": 4,
      "literature_grounding": 3,
      "risk": 3,
      "compute_cost": 4,
      "diversity": 5,
      "overall": 3.5,
      "strengths": ["..."],
      "weaknesses": ["..."],
      "required_fixes": ["..."]
    }
  ]
}
""".strip()
    user = (
        "请评估下面的最终 core ideas。必须返回可解析 JSON，不要输出 Markdown。\n\n"
        f"规则评分初稿供参考：\n{json.dumps(rule_report, ensure_ascii=False)[:6000]}\n\n"
        f"待评估 core ideas：\n{core_ideas_md[:24000]}"
    )
    resp = llm.chat(
        [{"role": "user", "content": user}],
        system=rubric,
        max_tokens=8192,
        temperature=0,
        json_mode=False,
        strip_thinking=True,
    )
    parsed = _safe_json_object(getattr(resp, "content", ""))
    if not parsed:
        return {
            "judge_model": model_name,
            "status": "failed",
            "error": "LLM judge returned non-JSON response",
            "raw_preview": getattr(resp, "content", "")[:1000],
        }
    ideas = []
    for idx, item in enumerate(parsed.get("ideas", []), start=1):
        if not isinstance(item, dict):
            continue
        normalized = dict(item)
        normalized.setdefault("idea_id", f"idea-{idx}")
        for dim in (*_DIMENSIONS, "overall"):
            normalized[dim] = _normalize_score(normalized.get(dim, 0))
        ideas.append(normalized)
    summary = parsed.get("summary", {}) if isinstance(parsed.get("summary"), dict) else {}
    if ideas:
        summary["overall_avg"] = round(sum(i.get("overall", 0) for i in ideas) / len(ideas), 2)
        summary.setdefault("best_idea", max(ideas, key=lambda i: i.get("overall", 0)).get("title", ""))
    return {
        "judge_model": parsed.get("judge_model", model_name),
        "status": "ok",
        "summary": summary,
        "ideas": ideas,
    }


def write_idea_quality_report(
    core_ideas_md: str,
    output_json: Path,
    output_md: Path | None = None,
    *,
    llm_judge: Any | None = None,
    judge_model_name: str = "Qwen3.5-122B-A10B-FP8",
) -> dict[str, Any]:
    report = evaluate_ideas(core_ideas_md)
    if llm_judge is not None:
        try:
            report['llm_judge'] = evaluate_ideas_with_llm_judge(
                core_ideas_md,
                llm_judge,
                model_name=judge_model_name,
            )
        except Exception as exc:  # noqa: BLE001
            report['llm_judge'] = {
                'judge_model': judge_model_name,
                'status': 'failed',
                'error': str(exc),
            }
    output_json.parent.mkdir(parents=True, exist_ok=True)
    output_json.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
    if output_md:
        lines = ['# Idea Quality Scores', '', f"规则评分总体平均分：{report['summary']['overall_avg']}/5", '']
        judge = report.get('llm_judge', {})
        if isinstance(judge, dict):
            if judge.get('status') == 'ok':
                js = judge.get('summary', {}) if isinstance(judge.get('summary'), dict) else {}
                lines.extend([
                    '## LLM-as-Judge 强模型评分',
                    '',
                    f"Judge 模型：{judge.get('judge_model', judge_model_name)}",
                    f"强模型总体平均分：{js.get('overall_avg', 'N/A')}/5",
                    f"结论：{js.get('verdict', 'N/A')}",
                    f"主要理由：{js.get('main_reason', '')}",
                    '',
                ])
            elif judge:
                lines.extend([
                    '## LLM-as-Judge 强模型评分',
                    '',
                    f"Judge 模型：{judge.get('judge_model', judge_model_name)}",
                    f"状态：{judge.get('status', 'failed')}",
                    f"错误：{judge.get('error', '')}",
                    '',
                ])
        lines.append('## 规则评分明细')
        lines.append('')
        lines.append('| Idea | Overall | Novelty | Feasibility | Impact | Testability | Grounding | Risk | Compute | Diversity |')
        lines.append('|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|')
        for item in report['ideas']:
            lines.append(
                f"| {item['title']} | {item['overall']} | {item['novelty']} | {item['feasibility']} | {item['impact']} | {item['testability']} | {item['literature_grounding']} | {item['risk']} | {item['compute_cost']} | {item['diversity']} |"
            )
        if isinstance(judge, dict) and judge.get('status') == 'ok' and judge.get('ideas'):
            lines.extend(['', '## 强模型评分明细', ''])
            lines.append('| Idea | Overall | Novelty | Feasibility | Impact | Testability | Grounding | Risk | Compute | Diversity |')
            lines.append('|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|')
            for item in judge.get('ideas', []):
                lines.append(
                    f"| {item.get('title', item.get('idea_id', ''))} | {item.get('overall', '')} | {item.get('novelty', '')} | {item.get('feasibility', '')} | {item.get('impact', '')} | {item.get('testability', '')} | {item.get('literature_grounding', '')} | {item.get('risk', '')} | {item.get('compute_cost', '')} | {item.get('diversity', '')} |"
                )
            lines.append('')
            for item in judge.get('ideas', []):
                fixes = item.get('required_fixes', []) if isinstance(item, dict) else []
                weaknesses = item.get('weaknesses', []) if isinstance(item, dict) else []
                if fixes or weaknesses:
                    lines.append(f"### {item.get('title', item.get('idea_id', ''))}")
                    for weakness in weaknesses:
                        lines.append(f"- Weakness: {weakness}")
                    for fix in fixes:
                        lines.append(f"- Required fix: {fix}")
                    lines.append('')
        duplicate_pairs = report.get('summary', {}).get('duplicate_pairs', [])
        if duplicate_pairs:
            lines.extend(['', '## 疑似重复 / 多样性风险', ''])
            for pair in duplicate_pairs:
                lines.append(
                    f"- {pair.get('idea_a')} ↔ {pair.get('idea_b')} similarity={pair.get('similarity')}: "
                    f"{pair.get('title_a')} / {pair.get('title_b')}"
                )
        lines.append('')
        for item in report['ideas']:
            if item['notes']:
                lines.append(f"## {item['title']}")
                lines.extend(f"- {note}" for note in item['notes'])
                lines.append('')
        output_md.write_text('\n'.join(lines), encoding='utf-8')
    return report
