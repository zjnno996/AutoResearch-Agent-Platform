"""
CCF 推荐学术会议与期刊数据库（计算机学科）。

提供会议/期刊名称到 CCF 等级（A/B/C）的映射，用于文献检索时
优先展示 CCF 推荐出版物中的论文。

数据来源：中国计算机学会(CCF)推荐国际学术会议和期刊目录(2022版)
"""

from __future__ import annotations

import re
from typing import Optional

# ---------------------------------------------------------------------------
# CCF-A 类会议
# ---------------------------------------------------------------------------
CCF_A_CONFERENCES: set[str] = {
    # 人工智能 / 机器学习 / 模式识别
    "aaai", "neurips", "nips", "icml", "iclr", "ijcai",
    "cvpr", "iccv", "eccv", "acl", "emnlp", "naacl",
    "coling",  # AACL / COLING
    "sigir", "www", "kdd", "wine",  # 数据挖掘 / 信息检索
    # 计算机系统 / 网络
    "osdi", "sosp", "eurosys", "usenix atc", "sigcomm",
    "mobicom", "sensys", "fast", "sc",  # supercomputing
    # 安全
    "sp", "ieee s&p", "ccs", "usenix security", "ndss",
    # 数据库 / 数据工程
    "sigmod", "vldb", "icde", "podsi",
    # 软件工程 / 编程语言
    "pldi", "popl", "icse", "fse", "ase", "oopsla",
    # 计算机图形学 / 可视化
    "sigraph", "siggraph", "vis",
    # 计算机体系结构
    "isca", "micro", "hpca", "asplos",
    # 人机交互
    "chi", "uist", "ubicomp",
    # 机器人
    "icra", "iros", "rss",
    # 计算理论
    "stoc", "focs", "lics", "soda",
}

# ---------------------------------------------------------------------------
# CCF-B 类会议
# ---------------------------------------------------------------------------
CCF_B_CONFERENCES: set[str] = {
    # 人工智能
    "aistats", "aamas", "icaps", "uai", "coot",
    "iccbr", "alt", "ecml", "pkdd", "ecai",
    "iconip",  # 但其变体只算 B
    "icann", "icpr", "accv", "bmvc", "icassp",
    "inter speech", "interspeech", "icme",
    "cinlp", "eacl", "ijcnlp", "conll",
    "recsys", "wsdm", "ecir", "pkdd/ecml",
    "cikm", "ismb", "recomb",
    # 系统 / 网络
    "middleware", "icnp", "icdcs", "ipccc",
    "icpp", "cluster", "hpec", "ccgrid",
    "socc", "hotos",
    # 安全
    "acsac", "esorics", "raids", "dac",
    "date", "cis",
    # 数据库
    "er", "dasfaa", "pods", "ssdbm", "waim", "mdm",
    # 软件工程
    "icsm", "icsme", "saner", "programming", "icpc",
    "issta", "issre", "req",
    # 图形学 / 可视化
    "cgf", "eg", "pg", "vr", "ieee vr",
    # 人机交互
    "group", "iui", "mobilehci", "dis", "ecscw",
    # 机器人
    "icra",  # 同一个
    "anch",
    # 计算机体系结构 / 芯片
    "fpga", "dac", "date", "ispd", "glsvlsi",
    # 网络
    "imc", "conext", "networks", "infocom",
}

# ---------------------------------------------------------------------------
# CCF-C 类会议（部分代表性）
# ---------------------------------------------------------------------------
CCF_C_CONFERENCES: set[str] = {
    "aiai", "icb", "iciar", "aipr", "caip", "iciip",
    "ijcnn", "isnn", "nc", "prcv", "sibgrapi",
    "icme",  # 有些来源定为 B, 有些为 C
    "colt", "acml", "pricai", "icnc",
    "kes", "isnn",
    "nca", "padl", "pkd",
    "sara", "sas",
    "ai", "aicom", "exa",
    "icas", "icassp",  # 已有
    "aic", "civr", "ariis",
    "bigdata", "wcnc", "percom",
    "mascots", "iscc", "lcn",
    "sec", "iwqos", "msn",
}

# ---------------------------------------------------------------------------
# CCF-A 类期刊
# ---------------------------------------------------------------------------
CCF_A_JOURNALS: set[str] = {
    # AI / ML / 视觉
    "ieee transactions on pattern analysis and machine intelligence",
    "tpami", "international journal of computer vision", "ijcv",
    "journal of machine learning research", "jmlr",
    "artificial intelligence", "aij",
    "ieee transactions on neural networks and learning systems",
    "tnnls", "ieee transactions on image processing", "tip",
    # 数据 / 系统
    "ieee transactions on knowledge and data engineering", "tkde",
    "vldb journal", "vldbj",
    "acm transactions on computer systems", "tocs",
    "ieee/acm transactions on networking", "ton",
    "ieee transactions on parallel and distributed systems", "tpds",
    "ieee transactions on information theory", "tit",
    # 软件
    "acm transactions on software engineering and methodology", "tosem",
    "ieee transactions on software engineering", "tse",
    # 安全
    "ieee transactions on dependable and secure computing", "tdsc",
    "journal of cryptology",
    # 计算机图形学
    "acm transactions on graphics", "tog",
    "ieee transactions on visualization and computer graphics", "tvcg",
}

# ---------------------------------------------------------------------------
# CCF-B 类期刊
# ---------------------------------------------------------------------------
CCF_B_JOURNALS: set[str] = {
    "machine learning", "mlj",  # 是期刊名也是会议 ...
    "neural networks",
    "ieee transactions on cybernetics",
    "ieee transactions on affective computing",
    "ieee transactions on multimedia",
    "acm transactions on intelligent systems and technology",
    "acm transactions on knowledge discovery from data",
    "data mining and knowledge discovery",
    "ieee transactions on services computing",
    "ieee transactions on cloud computing",
    "computer vision and image understanding", "cviu",
    "pattern recognition",
    "neurocomputing",
    "knowledge-based systems",
    "expert systems with applications",
    "engineering applications of artificial intelligence",
    "applied soft computing",
    "soft computing",
    "journal of artificial intelligence research", "jair",
    "cognitive science",
    "computational linguistics",
    "ieee robotics and automation letters",
    "autonomous robots",
    "international journal of robotics research",
    "journal of field robotics",
    "ieee transactions on robotics",
    "ieee transactions on circuits and systems for video technology",
    "acm computing surveys",
    "ieee software",
    "empirical software engineering",
    "acm transactions on programming languages and systems", "toplas",
    "science of computer programming",
    "journal of parallel and distributed computing",
    "future generation computer systems",
}

# ---------------------------------------------------------------------------
# 综合映射表（用于模糊匹配）
# ---------------------------------------------------------------------------

CCF_TIER_MAP: list[tuple[re.Pattern, str]] = []


def _compile_patterns() -> list[tuple[re.Pattern, str]]:
    """编译所有 CCF 会议/期刊名称到正则模式，用于模糊匹配。"""
    patterns: list[tuple[re.Pattern, str]] = []

    # 会议名称匹配（大小写不敏感，忽略标点）
    for name in CCF_A_CONFERENCES:
        patterns.append((re.compile(re.escape(name), re.IGNORECASE), "CCF-A"))
    for name in CCF_B_CONFERENCES:
        patterns.append((re.compile(re.escape(name), re.IGNORECASE), "CCF-B"))
    for name in CCF_C_CONFERENCES:
        patterns.append((re.compile(re.escape(name), re.IGNORECASE), "CCF-C"))

    # 期刊名称匹配——全名匹配
    for name in CCF_A_JOURNALS:
        patterns.append((re.compile(re.escape(name), re.IGNORECASE), "CCF-A"))
    for name in CCF_B_JOURNALS:
        patterns.append((re.compile(re.escape(name), re.IGNORECASE), "CCF-B"))

    return patterns


# 延迟初始化模式匹配
_patterns: list[tuple[re.Pattern, str]] | None = None


def _get_patterns() -> list[tuple[re.Pattern, str]]:
    global _patterns
    if _patterns is None:
        _patterns = _compile_patterns()
    return _patterns


# ---------------------------------------------------------------------------
# 额外缩写/变体映射
# ---------------------------------------------------------------------------

_VENUE_ALIASES: dict[str, str] = {
    # 常见会议缩写变体
    "nips": "neurips",
    "ieee symposium on security and privacy": "sp",
    "ieee s&p": "sp",
    "ieee security and privacy": "sp",
    "usenix security symposium": "usenix security",
    "usenix annual technical conference": "usenix atc",
    "sigar": "sigir",
    "world wide web conference": "www",
    "international conference on machine learning": "icml",
    "international conference on learning representations": "iclr",
    "international joint conference on artificial intelligence": "ijcai",
    "conference on computer vision and pattern recognition": "cvpr",
    "international conference on computer vision": "iccv",
    "european conference on computer vision": "eccv",
    "annual meeting of the association for computational linguistics": "acl",
    "empirical methods in natural language processing": "emnlp",
    "north american chapter of the association for computational linguistics": "naacl",
    "knowledge discovery and data mining": "kdd",
    "international conference on software engineering": "icse",
    "foundations of software engineering": "fse",
    "symposium on operating systems principles": "sosp",
    "operating systems design and implementation": "osdi",
    "architectural support for programming languages and operating systems": "asplos",
    "international symposium on computer architecture": "isca",
    "ieee international symposium on high-performance computer architecture": "hpca",
    "international symposium on microarchitecture": "micro",
    "conference on applications and technologies": "usenix atc",
    "acm sigcomm": "sigcomm",
    "acm special interest group on data communication": "sigcomm",
    "conference on computer and communications security": "ccs",
    "network and distributed system security symposium": "ndss",
    "international conference on machine learning and applications": "icmla",
    # 期刊常见变体
    "ieee trans. pattern anal. mach. intell": "tpami",
    "ieee trans. neural networks learn. syst": "tnnls",
    "int. J. comput. vis": "ijcv",
    "j. mach. learn. res": "jmlr",
    "journal of machine learning research (jmlr)": "jmlr",
    "ieee trans. knowl. data eng": "tkde",
    "vldb": "vldb journal",
    "acm trans. graph": "tog",
    "ieee trans. vis. comput. graph": "tvcg",
    "ieee trans. softw. eng": "tse",
    "ieee trans. parallel distrib. syst": "tpds",
    "ieee trans. inf. theory": "tit",
    "ieee trans. image process": "tip",
    "ieee transactions on neural networks": "tnnls",
    "advances in neural information processing systems": "neurips",
    "proceedings of machine learning research": "pmlr",
}


def normalize_venue(venue: str) -> str:
    """标准化会议/期刊名称，统一缩写变体。"""
    v = venue.strip().lower()
    # 缩写映射
    normalized = _VENUE_ALIASES.get(v, v)
    if normalized == v:
        # 去掉常见的冗余前缀/后缀
        for prefix in [
            "proceedings of the ", "proceedings of ",
            "international conference on ", "ieee conference on ",
            "ieee ", "acm conference on ", "acm ",
            "the ", "annual ", "symposium on ",
        ]:
            if normalized.startswith(prefix):
                normalized = normalized[len(prefix):]
                break
        for suffix in [
            " conference", " symposium", " international conference",
        ]:
            if normalized.endswith(suffix):
                normalized = normalized[:-len(suffix)]
                break
    return normalized.strip()


def lookup_ccf_tier(venue: str, journal: str = "") -> Optional[str]:
    """查找会议/期刊的 CCF 等级。

    Args:
        venue: 会议或期刊名称（如 "NeurIPS", "CVPR", "TPAMI"）
        journal: 可选期刊名称（如 "IEEE Transactions on Pattern Analysis..."）

    Returns:
        "CCF-A", "CCF-B", "CCF-C" 或在无匹配时返回 None
    """
    # 优先检查 journal 字段
    names_to_check = []
    if journal:
        names_to_check.append(normalize_venue(journal))
    if venue:
        names_to_check.append(normalize_venue(venue))

    if not names_to_check:
        return None

    patterns = _get_patterns()
    for name in names_to_check:
        if not name:
            continue
        for pattern, tier in patterns:
            if pattern.search(name):
                return tier
    return None


def get_ccf_weight(tier: Optional[str]) -> float:
    """获取 CCF 等级对应的权重（用于排序）。"""
    if tier == "CCF-A":
        return 3.0
    elif tier == "CCF-B":
        return 2.0
    elif tier == "CCF-C":
        return 1.0
    return 0.0


def annotate_ccf_tier(papers: list) -> list:
    """返回带 CCF 等级的论文列表，同时兼容不可变 Paper 数据类。"""
    from dataclasses import is_dataclass, replace

    annotated = []
    for paper in papers:
        venue = getattr(paper, "venue", "") or ""
        tier = lookup_ccf_tier(venue)
        tier_value = tier or ""
        weight = get_ccf_weight(tier)
        if is_dataclass(paper) and hasattr(paper, "_ccf_tier"):
            annotated.append(
                replace(paper, _ccf_tier=tier_value, _ccf_weight=weight)
            )
        else:
            setattr(paper, "_ccf_tier", tier_value)
            setattr(paper, "_ccf_weight", weight)
            annotated.append(paper)
    return annotated
