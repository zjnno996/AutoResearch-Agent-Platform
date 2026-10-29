"""Ideation schema components."""

from __future__ import annotations

import logging


logger = logging.getLogger(__name__)


def _idea_quality_rubric() -> str:
    return """## Idea 质量评分标准（必须执行）
每个 Idea 都必须给出 1-5 分评分和一句理由：
- Novelty：是否相对相近文献有真实差异，而不是换名组合。
- Feasibility：是否能在当前资源、数据和 2 周 MVP 内启动验证。
- Impact：若成立，是否足以形成论文贡献或明确系统价值。
- Testability：是否有清晰可证伪假设、指标、失败阈值和对照实验。
- Literature Grounding：是否引用并区分了最相近的 3-5 篇文献。
- Risk：风险是否被识别，是否有早停信号和 fallback 方案。
- Compute Cost：计算预算是否明确且可控；5 分代表单卡/小规模 2 周内可验证，1 分代表成本高或没说明。
- Diversity：是否与其他 Idea 是不同技术机制；同一机制换标题、换应用场景或换指标必须低分。

硬性要求：如果某个 Idea 无法列出相近文献和差异点，不能推荐为首选。若两个 Idea 的核心机制重复，必须合并或替换，不能重复凑数。"""


def _idea_schema_instruction() -> str:
    return """## 强制输出结构
对每个 Idea 使用以下结构，字段不能省略：

## Idea N：<中文短标题>

### 1. 核心假设
用 1-2 句话写清楚可证伪假设。

### 2. 研究空缺与文献依据
列出 3-5 篇最相近文献或方法，说明它们分别解决了什么、没有解决什么。

### 3. 与已有工作的关键区别
用表格写：相近工作 / 相同点 / 不同点 / reviewer 可能质疑 / 回应策略。

### 4. 技术路线
具体到模型结构、算法步骤、训练目标、数据处理、系统组件或 agent workflow；避免抽象口号。

### 5. 可验证实验
至少 3 个实验：主实验、消融实验、鲁棒性或泛化实验。每个实验写数据集、baseline、指标、预期结果、失败阈值。

### 6. 两周 MVP
写最小可行验证：数据子集、模型规模、代码入口、计算预算、Go/No-Go 标准。

### 7. 风险、反例与失败条件
列出最可能失败的 3 个原因、早期发现信号、备选方案。

### 8. 评分
用表格给出 Novelty、Feasibility、Impact、Testability、Literature Grounding、Risk、Compute Cost、Diversity 八项 1-5 分和理由。

### 9. 重复检查
列出该 Idea 与其他 Idea 的机制差异；如果与任何候选相似，说明为什么仍然值得保留，否则必须合并或替换。

最后必须包含：
## 总体排序与首选推荐
按综合分排序，推荐 exactly one 个首选 Idea，并解释为什么先做它。

## 去重与多样性检查
用表格列出所有 Idea 两两之间的机制差异。严禁出现同一机制换标题、换数据集或换指标的重复 Idea。"""
