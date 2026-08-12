"""Generate a complete Markdown report from cached expert-alignment results."""

from __future__ import annotations

import json
from pathlib import Path


ROOT = Path(__file__).resolve().parent / "eval_cache" / "thesis"
PIPELINE = "qwen-vision-v7.2-hybrid-agents"
DATASETS = [
    ("dataset1", "dataset1-IoT-Generation"),
    ("dataset3", "dataset3-Edge-Computing"),
]
TYPE_LABELS = {"strength": "优点", "weakness": "不足", "suggestion": "建议"}
DIMENSION_LABELS = {
    "methodology": "研究方法", "novelty": "创新性", "experiment": "实验与结果",
    "writing": "写作表达", "related_work": "相关工作", "reproducibility": "可复现性",
    "ethics": "伦理与风险", "skeptic": "质疑审查", "writing_format": "格式规范",
    "structure_logic": "结构逻辑", "theory_depth": "理论深度",
    "deep_dive": "深度审查", "patch": "补充审查",
}


def _type_label(value: object) -> str:
    return TYPE_LABELS.get(str(value), str(value))


def _dimension_label(value: object) -> str:
    return DIMENSION_LABELS.get(str(value), str(value))


def main() -> None:
    lines = [
        "# AutoReview v6 与真实专家对齐报告",
        "",
        "> 模型：Qwen3.5-122B-A10B-FP8；仅保留置信度 ≥ 0.55 的意见；"
        "匹配要求类型、对象、范围和极性一致。",
        "",
    ]

    for dataset, key in DATASETS:
        coverage_path = ROOT / "coverage" / f"{key}-{PIPELINE}.json"
        review_path = ROOT / "auto_reviews" / f"{key}-{PIPELINE}.json"
        coverage = json.loads(coverage_path.read_text(encoding="utf-8"))
        review = json.loads(review_path.read_text(encoding="utf-8"))
        meta = review["meta"]
        counts = coverage["counts"]
        recall_audit = coverage.get("recall_audit", {})

        lines.extend([
            f"## {dataset}: {coverage.get('title', key)}",
            "",
            f"- 综合评分：{review['overall_score']}/100",
            f"- 图表覆盖：{meta.get('vision_pages')}/{meta.get('vision_expected_pages')} 页，"
            f"失败页：{meta.get('vision_failed_pages')}",
            f"- 专家覆盖：{coverage['covered_count']}/{coverage['total_human_points']}"
            f"（{coverage['coverage_pct']}%）",
            f"- Auto对上：{counts['matched']}；未对上但有用：{counts['useful_unmatched']}；"
            f"未对上且无效：{counts['unhelpful_unmatched']}；专家漏检：{counts['missed_human']}",
            f"- 类型不一致硬拒绝：{coverage.get('type_mismatch_rejections', 0)}",
            f"- 严格二次复核：检查 {recall_audit.get('targets', 0)} 条首轮漏项，"
            f"恢复 {recall_audit.get('recovered', 0)} 条",
            "",
            "### 对上的专家意见",
            "",
        ])

        for match in coverage["matches"]:
            if not match.get("covered"):
                continue
            human = coverage["human_points"][match["human_index"]]
            auto = coverage["auto_points"][match["auto_index"]]
            lines.extend([
                f"- 专家（{_type_label(human['type'])}）：{human['text']}",
                f"  - Auto（{_dimension_label(auto['dimension'])}，置信度 "
                f"{float(auto.get('confidence', 0)):.0%}）：{auto['text']}",
            ])

        lines.extend(["", "### 未对上但有用的新增意见", ""])
        for point in coverage.get("useful_unmatched_auto_points", []):
            lines.extend([
                f"- [{_dimension_label(point.get('dimension'))}/{_type_label(point.get('type'))}，置信度 "
                f"{float(point.get('confidence', 0)):.0%}] {point.get('text')}",
                f"  - 判定理由：{point.get('usefulness_reason', '')}",
            ])

        lines.extend(["", "### 未对上且无效的意见", ""])
        unhelpful = coverage.get("unhelpful_unmatched_auto_points", [])
        if not unhelpful:
            lines.append("- 无")
        for point in unhelpful:
            lines.extend([
                f"- [{_dimension_label(point.get('dimension'))}/{_type_label(point.get('type'))}] {point.get('text')}",
                f"  - 排除理由：{point.get('usefulness_reason', '')}",
            ])

        lines.extend(["", "### 专家提出但系统漏掉的意见", ""])
        for point in coverage.get("missed_human_points", []):
            lines.append(f"- [{_type_label(point.get('type'))}] {point.get('text')}")
        lines.append("")

    output = ROOT / "final_expert_alignment_v6.md"
    output.write_text("\n".join(lines), encoding="utf-8")
    print(output)


if __name__ == "__main__":
    main()
