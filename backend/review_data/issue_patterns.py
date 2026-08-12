"""Issue-pattern retrieval from the local expert-review datasets.

The target paper's own reviews are excluded automatically to prevent evaluation
leakage. Retrieved cards are diagnostic priors, never evidence by themselves.
"""

from __future__ import annotations

import os
import re
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any


DEFAULT_DATASET_DIR = Path(os.environ.get("AUTO_REVIEW_DATASET_DIR", "/root/auto_review_dataset"))
_POINT_RE = re.compile(r"^\s*\[(STRENGTH|WEAKNESS|SUGGESTION)\]\s*(.+?)\s*$")
_REVIEWER_RE = re.compile(r"^\s*REVIEWER\s+(\d+)", re.IGNORECASE)
_LOCATOR_RE = re.compile(
    r"(?:第\s*)?\d+(?:\.\d+){0,3}\s*(?:章|节)?"
    r"|(?:图|表|公式|算法)\s*\d+(?:[.\-]\d+)*",
    re.IGNORECASE,
)

_DIMENSION_RULES: list[tuple[str, tuple[str, ...]]] = [
    ("experiment", ("实验", "数据集", "基线", "指标", "消融", "测试", "吞吐", "时延", "准确率", "评估")),
    ("theory_depth", ("理论", "收敛", "复杂度", "数学", "证明", "最优性", "目标函数", "约束条件")),
    ("writing_format", ("格式", "参考文献", "英文摘要", "缩写", "三线表", "编号", "图表", "排版")),
    ("structure_logic", ("结构", "章节", "标题", "目录", "逻辑关系", "重复", "冗余", "小结")),
    ("methodology", ("方法", "假设", "模型", "机制", "算法", "公平性", "鲁棒性", "安全性")),
    ("related_work", ("相关工作", "文献", "研究现状", "引用", "最新研究")),
    ("reproducibility", ("复现", "超参数", "实现细节", "代码", "配置", "工具链")),
    ("novelty", ("创新", "贡献", "差异", "已有工作")),
    ("writing", ("写作", "表述", "术语", "可读性", "摘要")),
    ("ethics", ("隐私", "伦理", "偏见", "滥用", "安全边界")),
]


@dataclass
class IssueCard:
    card_id: str
    source_dataset: str
    paper_title: str
    reviewer: int
    issue_type: str
    text: str
    dimension: str
    severity: str
    locators: list[str] = field(default_factory=list)
    trigger_terms: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def semantic_tokens(text: str) -> set[str]:
    lower = text.lower()
    latin = set(re.findall(r"[a-z0-9]+(?:[.\-][a-z0-9]+)*", lower))
    chinese_runs = re.findall(r"[\u4e00-\u9fff]+", lower)
    chinese: set[str] = set()
    for run in chinese_runs:
        chinese.update(run[i:i + 2] for i in range(max(0, len(run) - 1)))
        if len(run) <= 4:
            chinese.add(run)
    return {token for token in latin | chinese if token}


def semantic_similarity(a: str, b: str) -> float:
    ta, tb = semantic_tokens(a), semantic_tokens(b)
    if not ta or not tb:
        return 0.0
    intersection = len(ta & tb)
    containment = intersection / min(len(ta), len(tb))
    jaccard = intersection / len(ta | tb)
    return 0.7 * containment + 0.3 * jaccard


def infer_dimension(text: str) -> str:
    best_dim = "methodology"
    best_score = 0
    for dimension, keywords in _DIMENSION_RULES:
        score = sum(keyword.lower() in text.lower() for keyword in keywords)
        if score > best_score:
            best_dim, best_score = dimension, score
    return best_dim


def infer_severity(text: str, dimension: str, issue_type: str) -> str:
    if issue_type == "strength":
        return "positive"
    critical = ("错误", "无效", "不成立", "安全", "泄漏", "不公平", "无法证明", "严重")
    if any(term in text for term in critical):
        return "critical"
    if dimension in {"methodology", "experiment", "theory_depth", "novelty"}:
        return "major"
    return "minor"


def _extract_title(lines: list[str]) -> str:
    for line in lines:
        stripped = line.strip()
        if stripped and set(stripped) != {"="} and not _REVIEWER_RE.match(stripped):
            return stripped
    return ""


def _trigger_terms(text: str) -> list[str]:
    terms = re.findall(r"[A-Za-z][A-Za-z0-9.\-]{2,}|[\u4e00-\u9fff]{2,8}", text)
    stop = {"论文", "建议", "进一步", "相关", "进行", "方面", "部分", "研究", "需要", "作者"}
    result: list[str] = []
    for term in terms:
        if term in stop or term in result:
            continue
        result.append(term)
    return result[:12]


def load_issue_cards(dataset_dir: Path = DEFAULT_DATASET_DIR) -> list[IssueCard]:
    cards: list[IssueCard] = []
    if not dataset_dir.exists():
        return cards
    for review_path in sorted(dataset_dir.glob("dataset*/human_reviews.txt")):
        lines = review_path.read_text(encoding="utf-8", errors="ignore").splitlines()
        title = _extract_title(lines)
        reviewer = 0
        for line in lines:
            reviewer_match = _REVIEWER_RE.match(line)
            if reviewer_match:
                reviewer = int(reviewer_match.group(1))
                continue
            match = _POINT_RE.match(line)
            if not match:
                continue
            issue_type = match.group(1).lower()
            point = match.group(2).strip()
            dimension = infer_dimension(point)
            cards.append(IssueCard(
                card_id=f"{review_path.parent.name}-r{reviewer}-{len(cards) + 1}",
                source_dataset=review_path.parent.name,
                paper_title=title,
                reviewer=reviewer,
                issue_type=issue_type,
                text=point,
                dimension=dimension,
                severity=infer_severity(point, dimension, issue_type),
                locators=list(dict.fromkeys(_LOCATOR_RE.findall(point)))[:6],
                trigger_terms=_trigger_terms(point),
            ))
    return cards


class IssuePatternIndex:
    def __init__(self, cards: list[IssueCard] | None = None):
        self.cards = cards if cards is not None else load_issue_cards()

    @staticmethod
    def _is_target_leak(card: IssueCard, target_paper_text: str) -> bool:
        title_key = re.sub(r"\W+", "", card.paper_title.lower())
        target_key = re.sub(r"\W+", "", target_paper_text[:12000].lower())
        return bool(title_key and len(title_key) >= 8 and title_key in target_key)

    def retrieve(
        self,
        query: str,
        dimension: str | None = None,
        issue_types: tuple[str, ...] = ("weakness", "suggestion"),
        target_paper_text: str = "",
        k: int = 6,
    ) -> list[tuple[float, IssueCard]]:
        query_tokens = semantic_tokens(query)
        scored: list[tuple[float, IssueCard]] = []
        for card in self.cards:
            if card.issue_type not in issue_types:
                continue
            if target_paper_text and self._is_target_leak(card, target_paper_text):
                continue
            card_tokens = semantic_tokens(card.text + " " + " ".join(card.trigger_terms))
            overlap = len(query_tokens & card_tokens) / max(len(card_tokens), 1)
            dim_bonus = 0.18 if dimension and card.dimension == dimension else 0.0
            type_bonus = 0.03 if card.issue_type == "weakness" else 0.0
            score = overlap + dim_bonus + type_bonus
            scored.append((score, card))
        scored.sort(key=lambda item: item[0], reverse=True)
        return [item for item in scored[:k] if item[0] > 0.05]

    def prompt_context(
        self,
        query: str,
        dimension: str,
        target_paper_text: str,
        k: int = 6,
    ) -> str:
        retrieved = self.retrieve(
            query=query,
            dimension=dimension,
            target_paper_text=target_paper_text,
            k=k,
        )
        if not retrieved:
            return ""
        lines = [
            "## Expert Dataset Issue Patterns",
            "These are diagnostic patterns from OTHER theses, not facts about this paper. "
            "Use them only as questions to verify against current-paper evidence.",
        ]
        for score, card in retrieved:
            lines.append(
                f"- [{card.dimension}/{card.severity}; prior={score:.2f}] {card.text}"
            )
        return "\n".join(lines)

    def prior_for_text(self, text: str, target_paper_text: str = "") -> float:
        candidates = self.retrieve(
            query=text,
            target_paper_text=target_paper_text,
            k=5,
        )
        if not candidates:
            return 0.0
        return min(1.0, max(score for score, _ in candidates))


_INDEX: IssuePatternIndex | None = None


def get_issue_pattern_index(rebuild: bool = False) -> IssuePatternIndex:
    global _INDEX
    if _INDEX is None or rebuild:
        _INDEX = IssuePatternIndex()
    return _INDEX
