#!/usr/bin/env python3
"""
Agent Bridge v2 — project isolation, inter-layer task queues, idle-pull scheduling.

Architecture:
  runs_base/
  ├── projects/
  │   ├── proj-xxx/          # Each project has its own run_dir
  │   │   ├── stage-01/ ... stage-15/
  │   │   ├── checkpoint.json
  │   │   └── heartbeat.json
  │   └── proj-yyy/ ...
  └── queues/
      ├── idea_to_experiment.json
      ├── experiment_to_coding.json
      ├── coding_to_execution.json
      └── execution_feedback.json

Usage:
    python agent_bridge.py [--port 8766] [--agent-dir /path/to/agent]
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import hashlib
import json
import os
import re
import signal
import shutil
import subprocess
import sys
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path

import websockets

# 用户认证模块
_SERVICE_DIR = Path(__file__).resolve().parent
if str(_SERVICE_DIR) not in sys.path:
    sys.path.insert(0, str(_SERVICE_DIR))
from user_auth import register_user, login_user, verify_token, load_users

# ── Constants ───────────────────────────────────────────────────────────────

STAGE_TO_LAYER: dict[int, str] = {
    1: "idea", 2: "idea", 3: "idea", 4: "idea",
    5: "idea", 6: "idea", 7: "idea", 8: "idea",
    9: "experiment",
    10: "coding", 11: "coding", 12: "coding", 13: "coding",
    14: "execution", 15: "execution", 16: "execution", 17: "execution", 18: "execution",
    19: "writing", 20: "writing", 21: "writing", 22: "writing",
    23: "writing", 24: "writing", 25: "writing", 26: "writing",
}

LAYER_STAGES: dict[str, list[int]] = {
    "idea": [1, 2, 3, 4, 5, 6, 7, 8],
    "experiment": [9],
    "coding": [10, 11, 12, 13],
    "execution": [14, 15, 16, 17, 18],
    "writing": [19, 20, 21, 22, 23, 24, 25, 26],
}

LAYER_RANGE: dict[str, tuple[int, int]] = {
    "idea": (1, 8),
    "experiment": (9, 9),
    "coding": (10, 13),
    "execution": (14, 18),
    "writing": (19, 26),
}

LAYER_RANGE_PHASE1: dict[str, tuple[int, int]] = {"idea": (1, 7)}
LAYER_RANGE_PHASE2: dict[str, tuple[int, int]] = {"idea": (8, 8)}

DISCUSSION_STAGE = 100
PROJECT_TERMINAL_STAGE = 26
DEFAULT_QWEN3_MODEL = "Qwen3.5-122B-A10B-FP8"
IEEE_FULL_CHAIN_REQUIREMENT = (
    "写作与交付要求：最终论文必须是完整 IEEE 风格英文论文，采用 IEEE 双栏 LaTeX；"
    "正文目标为 8 页（参考文献计入总页数），不得通过重复、空泛背景或放大图表凑页；"
    "包含 Title、Abstract、4–6 个 Index Terms、Introduction、Related Work、Methodology、"
    "Experimental Setup、Results、Discussion、Limitations、Conclusion 和 References。"
    "正文使用 IEEE 数字引用格式，图表标题、公式编号、单位和符号遵循 IEEE 规范。"
    "必须交付可运行实验代码、真实运行记录、结果图表、IEEE LaTeX 源码、参考文献和编译 PDF。"
    "任何未真实执行或证据不足的结果必须明确标记，不得编造实验数据。"
)

PASSTHROUGH_LAYERS: set[str] = set()

STAGE_NAMES: dict[int, str] = {
    1: "TOPIC_INIT", 2: "PROBLEM_DECOMPOSE", 3: "SEARCH_STRATEGY",
    4: "LITERATURE_COLLECT", 5: "LITERATURE_SCREEN", 6: "KNOWLEDGE_EXTRACT",
    7: "SYNTHESIS", 8: "HYPOTHESIS_GEN", 9: "EXPERIMENT_DESIGN",
    10: "CODEBASE_SEARCH", 11: "CODE_GENERATION", 12: "SANITY_CHECK",
    13: "RESOURCE_PLANNING", 14: "EXPERIMENT_RUN", 15: "ITERATIVE_REFINE",
    16: "RESULT_ANALYSIS", 17: "RESEARCH_DECISION", 18: "KNOWLEDGE_SUMMARY",
    19: "PAPER_OUTLINE", 20: "PAPER_DRAFT", 21: "PEER_REVIEW", 22: "PAPER_REVISION",
    23: "QUALITY_GATE", 24: "KNOWLEDGE_ARCHIVE", 25: "EXPORT_PUBLISH", 26: "CITATION_VERIFY",
}

STAGE_OUTPUTS: dict[int, list[str]] = {
    1: ["goal.md", "hardware_profile.json"], 2: ["problem_tree.md"],
    3: ["search_plan.yaml", "sources.json", "queries.json"],
    4: ["candidates.jsonl", "references.bib", "reference_paper_text.md", "web_context.md", "search_meta.json"],
    5: ["shortlist.jsonl"], 6: ["cards/"], 7: ["synthesis.md", "literature_watch_report.md"], 8: ["hypotheses.md", "core_ideas.md", "hypotheses_raw.md", "challenge_insight_tree.md", "challenge_insight_tree.json", "candidate_ideas.md", "idea_tournament.md", "idea_tournament.json", "idea_role_review.md", "idea_review.md", "idea_pivot.md", "idea_decision_table.md", "ideation_memory_update.md", "idea_evidence_pack.md", "idea_branch_synthesis.md", "idea_branches/", "rag_index.jsonl", "global_rag_index.jsonl", "rag_retrieval_report.json", "citation_graph.json", "idea_quality_scores.json", "idea_quality_summary.md", "novelty_report.json", "idea_selection.json", "discussion_consensus.md"],
    9: ["exp_plan.yaml", "exp_plan_diagnostics.json"], 10: ["codebase_candidates.json"],
    11: ["experiment/", "experiment_spec.md"], 12: ["sanity_report.json"],
    13: ["schedule.json"], 14: ["runs/"],
    15: ["refinement_log.json", "experiment_final/"],
    16: ["analysis.md", "experiment_summary.json", "experiment_provenance.json", "evaluation_protocol_audit.json", "charts/"], 17: ["decision.md", "decision_structured.json", "research_readiness.json"], 18: ["knowledge_entry.json"],
    19: ["outline.md"], 20: ["paper_draft.md"], 21: ["reviews.md"], 22: ["paper_revised.md", "latex_package.zip"],
    23: ["quality_report.json", "fabrication_flags.json", "claim_integrity_report.json"],
    24: ["archive.md", "bundle_index.json"],
    25: ["paper_final.md", "paper.tex", "references.bib", "code/", "charts/", "final_claim_integrity_report.json", "reproducibility_manifest.json"],
    26: ["verification_report.json", "references_verified.bib", "paper_final_verified.md"],
}

# Curated artifacts to display in the frontend DataShelf (subset of STAGE_OUTPUTS)
DISPLAY_ARTIFACTS: set[str] = {
    "search_plan.yaml", "queries.json", "candidates.jsonl", "shortlist.jsonl",
    "cards/", "synthesis.md", "literature_watch_report.md", "references.bib", "reference_paper_text.md",
    "web_context.md", "search_meta.json",
    # Idea 仓库 — S8 distilled ideas + hypotheses
    "hypotheses.md", "core_ideas.md", "challenge_insight_tree.md", "candidate_ideas.md", "idea_tournament.md", "idea_role_review.md", "idea_review.md", "idea_pivot.md", "idea_decision_table.md", "ideation_memory_update.md", "idea_evidence_pack.md", "idea_branch_synthesis.md", "idea_branches/", "rag_retrieval_report.json", "citation_graph.json", "idea_quality_scores.json", "idea_quality_summary.md",
    # 知识库
    "knowledge_entry.json",
    # 论文仓库 — final paper + LaTeX package
    "paper_revised.md", "latex_package.zip", "quality_report.json", "claim_integrity_report.json", "final_claim_integrity_report.json", "reproducibility_manifest.json",
    "archive.md", "paper_final.md", "paper.tex", "verification_report.json",
    # 结果
    "analysis.md", "experiment_summary.json", "experiment_provenance.json", "charts/", "decision.md", "decision_structured.json", "research_readiness.json",
    # 实验设计
    "exp_plan.yaml", "exp_plan_diagnostics.json",
    # 代码
    "experiment/", "experiment_spec.md", "experiment_final/",
}

# 这些文件在发送到前端时需要完整内容（不截断），用于展示和导出
_FULL_CONTENT_ARTIFACTS: set[str] = {
    "synthesis.md", "literature_watch_report.md", "hypotheses.md", "core_ideas.md",
    "challenge_insight_tree.md", "candidate_ideas.md", "idea_tournament.md",
    "idea_role_review.md", "idea_review.md", "idea_pivot.md", "idea_decision_table.md",
    "ideation_memory_update.md", "idea_evidence_pack.md", "idea_branch_synthesis.md",
    "idea_quality_summary.md", "rag_retrieval_report.json", "citation_graph.json",
    "consensus_synthesis.md", "discussion_transcript.md",
    "paper_final.md", "paper_final_verified.md", "quality_report.json", "claim_integrity_report.json", "final_claim_integrity_report.json", "reproducibility_manifest.json", "verification_report.json",
    "experiment_summary.json", "experiment_provenance.json", "exp_plan_diagnostics.json", "decision_structured.json", "research_readiness.json",
}

REPO_FOR_STAGE: dict[int, str] = {
    1: "knowledge", 2: "knowledge", 3: "knowledge", 4: "knowledge",
    5: "knowledge", 6: "knowledge", 7: "knowledge", 8: "knowledge",
    9: "exp_design",
    10: "codebase", 11: "codebase", 12: "codebase", 13: "codebase",
    14: "results", 15: "results", 16: "results", 17: "results", 18: "insights",
    19: "papers", 20: "papers", 21: "papers", 22: "papers",
    23: "papers", 24: "knowledge", 25: "papers", 26: "papers",
}

# Queue names between layers
QUEUE_NAMES: dict[str, tuple[str, str]] = {
    "idea_to_experiment":     ("idea",       "experiment"),
    "experiment_to_coding":   ("experiment", "coding"),
    "coding_to_execution":    ("coding",     "execution"),
    "execution_to_writing":   ("execution",  "writing"),
    "execution_feedback":     ("execution",  "idea"),
}

# Which queue a completing layer feeds into
LAYER_OUTPUT_QUEUE: dict[str, str] = {
    "idea":       "idea_to_experiment",
    "experiment": "experiment_to_coding",
    "coding":     "coding_to_execution",
    "execution":  "execution_to_writing",
    "writing":    "execution_feedback",
}

# Which queue a layer pulls tasks from
LAYER_INPUT_QUEUE: dict[str, str] = {
    "experiment": "idea_to_experiment",
    "coding":     "experiment_to_coding",
    "execution":  "coding_to_execution",
    "writing":    "execution_to_writing",
    "idea":       "execution_feedback",
}

# ── Human Feedback Persistence ───────────────────────────────────────────────

def _save_feedback(state: "BridgeState", content: str, target_layer: str, message_id: str, user_id: str = "") -> None:
    """Persist human feedback to disk so running agents can pick it up.

    Writes to:
    1. Global ``runs_base/feedback/feedback_log.jsonl`` — full audit trail
    2. Each matching project's ``run_dir/human_feedback.jsonl`` — consumed by
       the executor's ``_load_human_feedback()`` before each pipeline stage
    """
    feedback_dir = Path(state.runs_base_dir) / "feedback"
    feedback_dir.mkdir(parents=True, exist_ok=True)
    entry = {
        "id": message_id,
        "content": content,
        "targetLayer": target_layer,
        "timestamp": _now_ms(),
    }
    log_path = feedback_dir / "feedback_log.jsonl"
    with open(log_path, "a", encoding="utf-8") as f:
        f.write(json.dumps(entry, ensure_ascii=False) + "\n")

    latest_path = feedback_dir / "latest_feedback.json"
    _write_json(latest_path, entry)

    injected_to: list[str] = []
    for agent in state.agents.values():
        if not agent.run_dir or agent.status not in ("working", "idle"):
            continue
        if user_id and (not agent.project_id or _project_owner_id(state, agent.project_id) != user_id):
            continue
        if target_layer != "all" and agent.layer != target_layer:
            continue
        run_dir = Path(agent.run_dir)
        if not run_dir.exists():
            continue
        fb_path = run_dir / "human_feedback.jsonl"
        try:
            with open(fb_path, "a", encoding="utf-8") as f:
                f.write(json.dumps(entry, ensure_ascii=False) + "\n")
            injected_to.append(f"{agent.name}({agent.project_id})")
        except OSError:
            pass

    if injected_to:
        print(f"[feedback] Injected to {len(injected_to)} project(s): {', '.join(injected_to)}")


# ── Utilities ───────────────────────────────────────────────────────────────

def _uid() -> str:
    return str(uuid.uuid4())[:8]

def _now_ms() -> int:
    return int(time.time() * 1000)

def _read_json(path: Path) -> dict | None:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return None

def _pid_is_alive(pid: object) -> bool:
    try:
        pid_int = int(pid)
    except (TypeError, ValueError):
        return False
    if pid_int <= 0:
        return False
    try:
        os.kill(pid_int, 0)
        return True
    except PermissionError:
        return True
    except OSError:
        return False

def _pid_cmdline_contains_run_dir(pid: object, run_dir: Path) -> bool:
    try:
        pid_int = int(pid)
    except (TypeError, ValueError):
        return False
    try:
        raw = Path(f"/proc/{pid_int}/cmdline").read_bytes()
    except (FileNotFoundError, PermissionError, OSError):
        return True
    cmdline = raw.replace(bytes([0]), b" " ).decode("utf-8", errors="ignore")
    return str(run_dir) in cmdline

def _run_dir_has_live_heartbeat(run_dir: Path) -> bool:
    heartbeat = _read_json(run_dir / "heartbeat.json")
    if not heartbeat:
        return False
    pid = heartbeat.get("pid")
    return _pid_is_alive(pid) and _pid_cmdline_contains_run_dir(pid, run_dir)

def _project_has_live_heartbeat(proj_dir: Path) -> bool:
    if _run_dir_has_live_heartbeat(proj_dir):
        return True
    try:
        children = list(proj_dir.iterdir())
    except OSError:
        return False
    for sub in children:
        if (
            sub.is_dir()
            and sub.name.startswith("run-")
            and _run_dir_has_live_heartbeat(sub)
        ):
            return True
    return False

def _write_json(path: Path, data: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")


_intent_llm_client: "object | None" = None
_intent_llm_init_done: bool = False

_INTENT_SYSTEM_PROMPT = (
    "你是一个意图分类器。用户在一个 AI 研究 pipeline 的控制面板中输入了一条消息。"
    "判断这条消息是【查询】(想了解当前运行状态/进度/阶段) 还是【反馈】(想给 pipeline 提供指导/建议/修改指令)。"
    "只回复一个词: query 或 feedback"
)


def _init_intent_llm(state: "BridgeState") -> None:
    """Lazily create a lightweight LLM client for intent classification."""
    global _intent_llm_client, _intent_llm_init_done
    if _intent_llm_init_done:
        return
    _intent_llm_init_done = True
    try:
        agent_dir = state.agent_package_dir
        if agent_dir not in sys.path:
            sys.path.insert(0, agent_dir)
        from researchclaw.llm.client import LLMClient, LLMConfig

        import yaml as _yaml
        root_config_path = Path(__file__).resolve().parents[2] / "config.arc.yaml"
        root_raw = {}
        if root_config_path.exists():
            with open(root_config_path, encoding="utf-8") as f:
                root_raw = _yaml.safe_load(f) or {}

        section = root_raw.get("web_chat_llm", {}) if isinstance(root_raw, dict) else {}
        if not isinstance(section, dict):
            section = {}
        api_key = str(
            section.get("api_key", "")
            or os.environ.get(str(section.get("api_key_env", "RESEARCHCLAW_API_KEY")), "")
            or os.environ.get("RESEARCHCLAW_API_KEY", "")
        )

        if not api_key:
            print("[intent-llm] No API key found, using keyword fallback")
            return

        from researchclaw.llm import resolve_provider_base_url
        provider = str(section.get("provider", "openai-compatible") or "openai-compatible")
        configured_base_url = str(section.get("base_url", "") or "")
        base_url = resolve_provider_base_url(provider, configured_base_url)
        fallback_models = section.get("fallback_models", [])
        if not isinstance(fallback_models, list):
            fallback_models = []
        _intent_llm_client = LLMClient(LLMConfig(
            base_url=base_url,
            api_key=api_key,
            primary_model=str(section.get("primary_model", DEFAULT_QWEN3_MODEL) or DEFAULT_QWEN3_MODEL),
            fallback_models=[str(m) for m in fallback_models if str(m).strip()],
            max_retries=1,
            timeout_sec=min(int(section.get("timeout_sec", 30) or 30), 30),
            max_tokens=256,
            strip_thinking=bool(section.get("strip_thinking", True)),
        ))
        print(f"[intent-llm] Initialized ({base_url})")
    except Exception as exc:
        print(f"[intent-llm] Init failed, will use keyword fallback: {exc}")


def _classify_chat_intent_keywords(text: str) -> str:
    """Fast keyword-based fallback for intent classification."""
    t = text.lower()
    q, f = 0, 0
    for kw in ("状态", "进度", "进展", "阶段", "跑到", "做到", "到哪", "到第几",
               "什么阶段", "什么状态", "查看", "查询", "怎么样了", "情况",
               "status", "progress", "stage", "how far"):
        if kw in t:
            q += 1
    for kw in ("请", "应该", "建议", "不要", "换成", "改成", "使用",
               "注意", "确保", "调整", "修改", "尝试", "模型", "参数",
               "checkpoint", "路径", "下载"):
        if kw in t:
            f += 1
    if t.rstrip()[-1:] in ("?", "？"):
        q += 2
    if any(p in t for p in ("吗", "呢")):
        q += 1
    if len(t) > 80:
        f += 1
    return "query" if q > f else "feedback"


async def _classify_chat_intent(text: str, state: "BridgeState") -> str:
    """Classify user chat intent: LLM first, keyword fallback on failure."""
    _init_intent_llm(state)
    if _intent_llm_client is not None:
        try:
            resp = await asyncio.get_event_loop().run_in_executor(
                None,
                lambda: _intent_llm_client.chat(  # type: ignore[union-attr]
                    [{"role": "user", "content": text}],
                    system=_INTENT_SYSTEM_PROMPT,
                    max_tokens=10,
                    temperature=0,
                ),
            )
            answer = resp.content.strip().lower()
            if "query" in answer:
                return "query"
            if "feedback" in answer:
                return "feedback"
        except Exception as exc:
            print(f"[intent-llm] Call failed, falling back to keywords: {exc}")
    return _classify_chat_intent_keywords(text)


def _pause_project(state: "BridgeState", project_id: str) -> list[dict]:
    """Pause a running project: stop agents and remove queued tasks, but keep all files."""
    messages: list[dict] = []
    sys_agent = LobsterAgent(
        id="system", name="系统", layer="idea", run_id="", run_dir="", config_path="",
    )

    if not project_id:
        return messages

    stopped = 0
    for agent in list(state.agents.values()):
        if agent.project_id == project_id:
            if agent.process is not None and agent.process.poll() is None:
                agent.process.terminate()
                try:
                    agent.process.wait(timeout=5)
                except Exception:
                    agent.process.kill()
                stopped += 1
            _reset_agent_idle(agent)
            messages.append(msg_agent_update(agent))

    removed = 0
    for q in state.queues.values():
        before = len(q.tasks)
        q.tasks = [t for t in q.tasks if t.project_id != project_id]
        removed += before - len(q.tasks)

    released = state.gpu_allocator.release(project_id)
    if released:
        messages.append(msg_log(sys_agent, f"GPU {released} 已释放 (项目暂停)", "info"))

    messages.append(msg_log(
        sys_agent,
        f"项目 [{project_id}] 已暂停 (停止 {stopped} 个 Agent, 移除 {removed} 个队列任务)",
        "warning",
    ))
    return messages


RESTART_FROM_OPTIONS: dict[str, tuple[int, str]] = {
    "topic": (1, "研究主题与目标"),
    "questions": (2, "研究问题拆解"),
    "search": (3, "检索策略"),
    "literature": (4, "文献收集与筛选"),
    "evidence": (6, "证据卡片抽取"),
    "synthesis": (7, "知识综述"),
    "ideas": (8, "研究想法生成"),
    "experiment": (9, "实验方案设计"),
    "code": (10, "代码检索与生成"),
    "run": (14, "实验执行"),
    "analysis": (16, "结果分析与决策"),
    "writing": (19, "论文写作"),
    "finalization": (23, "质量检查与最终导出"),
    "export": (25, "最终导出与归档"),
}


def _stage_dir_name(stage: int) -> str:
    return f"stage-{stage:02d}"


def _clear_run_from_stage(run_dir: Path, start_stage: int) -> None:
    """Remove artifacts from start_stage onward and set resume checkpoint."""
    for stage in range(start_stage, PROJECT_TERMINAL_STAGE + 1):
        stage_name = _stage_dir_name(stage)
        # A resumed pipeline may preserve a previous attempt as ``stage-XX_vN``.
        # Those directories are outputs of the same logical stage and must not
        # survive an explicit restart, otherwise later stages can ingest stale
        # metrics from an earlier experiment.
        for stage_dir in run_dir.glob(f"{stage_name}*"):
            if stage_dir.is_dir() and (
                stage_dir.name == stage_name
                or re.fullmatch(rf"{re.escape(stage_name)}_v\d+", stage_dir.name)
            ):
                shutil.rmtree(stage_dir, ignore_errors=True)

    # Discussion output depends on synthesis; keep it only when rerunning ideas.
    if start_stage <= 7:
        shutil.rmtree(run_dir / "discussion", ignore_errors=True)

    for stale_name in ("heartbeat.json", "intervention.json"):
        (run_dir / stale_name).unlink(missing_ok=True)

    checkpoint = run_dir / "checkpoint.json"
    if start_stage <= 1:
        checkpoint.unlink(missing_ok=True)
        return

    _write_json(
        checkpoint,
        {
            "last_completed_stage": start_stage - 1,
            "last_completed_name": STAGE_NAMES.get(start_stage - 1, ""),
            "timestamp": _now_ms(),
        },
    )


def _restart_project(state: "BridgeState", project_id: str, restart_from: str = "topic") -> list[dict]:
    """Restart a project from a content-level rollback point."""
    messages: list[dict] = []
    sys_agent = LobsterAgent(
        id="system", name="系统", layer="idea", run_id="", run_dir="", config_path="",
    )

    if not project_id:
        return messages

    proj_dir = state.projects_dir() / project_id
    if not proj_dir.exists():
        messages.append(msg_log(sys_agent, f"项目 [{project_id}] 不存在", "error"))
        return messages

    messages.extend(_pause_project(state, project_id))

    meta = _read_json(proj_dir / "project_meta.json")
    config_path = meta.get("config_path", "") if meta else ""
    topic = meta.get("topic", "") if meta else ""
    mode = meta.get("mode", "lab") if meta else "lab"

    if not config_path:
        messages.append(msg_log(sys_agent, f"项目 [{project_id}] 缺少配置文件路径, 无法重启", "error"))
        return messages

    start_stage, restart_label = RESTART_FROM_OPTIONS.get(
        str(restart_from or "topic"),
        RESTART_FROM_OPTIONS["topic"],
    )

    angle_dirs = sorted(proj_dir.glob("run-*"))
    if mode == "lab" and angle_dirs:
        for angle_dir in angle_dirs:
            if angle_dir.is_dir():
                _clear_run_from_stage(angle_dir, start_stage)

        if start_stage <= 7:
            shutil.rmtree(proj_dir / "discussion", ignore_errors=True)
        if start_stage <= 1:
            (proj_dir / "checkpoint.json").unlink(missing_ok=True)
        elif (proj_dir / "checkpoint.json").exists():
            _write_json(
                proj_dir / "checkpoint.json",
                {
                    "last_completed_stage": start_stage - 1,
                    "last_completed_name": STAGE_NAMES.get(start_stage - 1, ""),
                    "timestamp": _now_ms(),
                },
            )

        messages.append(msg_log(sys_agent, f"项目 [{project_id}] 已回退到「{restart_label}」，正在重新启动…", "info"))
        messages.extend(resume_project(state, project_id))
    else:
        _clear_run_from_stage(proj_dir, start_stage)
        messages.append(msg_log(sys_agent, f"项目 [{project_id}] 已回退到「{restart_label}」，正在重新启动…", "info"))
        if start_stage <= 1:
            messages.extend(submit_new_project(
                state,
                project_id,
                config_path,
                topic,
                mode=mode,
                user_id=str((meta or {}).get("user_id", "") or ""),
            ))
        else:
            messages.extend(resume_project(state, project_id))
    return messages

def _delete_project(state: "BridgeState", project_id: str) -> list[dict]:
    """Delete a project: stop any running processes, clean up state, remove files."""
    import shutil

    messages: list[dict] = []
    sys_agent = LobsterAgent(
        id="system", name="系统", layer="idea", run_id="", run_dir="", config_path="",
    )

    if not project_id:
        messages.append(msg_feedback_ack(f"del-{_uid()}", "请指定要删除的项目 ID。"))
        return messages

    for agent in list(state.agents.values()):
        if agent.project_id == project_id:
            if agent.process is not None and agent.process.poll() is None:
                agent.process.terminate()
                try:
                    agent.process.wait(timeout=5)
                except Exception:
                    agent.process.kill()
            _reset_agent_idle(agent)
            messages.append(msg_agent_update(agent))

    released = state.gpu_allocator.release(project_id)
    if released:
        messages.append(msg_log(sys_agent, f"GPU {released} 已释放 (项目删除)", "info"))

    for q in state.queues.values():
        original_len = len(q.tasks)
        q.tasks = [t for t in q.tasks if t.project_id != project_id]
        if len(q.tasks) != original_len:
            q.save()

    state._fail_counts.pop(project_id, None)

    # Clean up discussion state
    for aid in list(state.discussion_waiting):
        a = state.discussion_waiting[aid]
        if a.project_id == "" or a.project_id == project_id:
            state.discussion_waiting.pop(aid, None)
    state.discussion_groups.pop(project_id, None)

    proj_dir = state.projects_dir() / project_id

    def _stop_run_from_heartbeat(run_dir: Path) -> None:
        heartbeat = _read_json(run_dir / "heartbeat.json")
        if not isinstance(heartbeat, dict):
            return
        pid = heartbeat.get("pid")
        if not _pid_is_alive(pid) or not _pid_cmdline_contains_run_dir(pid, run_dir):
            return
        try:
            pid_int = int(pid)
        except (TypeError, ValueError):
            return
        try:
            os.kill(pid_int, 15)
            for _ in range(10):
                if not _pid_is_alive(pid_int):
                    return
                time.sleep(0.2)
            os.kill(pid_int, 9)
        except OSError:
            return

    if proj_dir.exists() and proj_dir.is_dir():
        _stop_run_from_heartbeat(proj_dir)
        try:
            for sub in proj_dir.iterdir():
                if sub.is_dir() and sub.name.startswith("run-"):
                    _stop_run_from_heartbeat(sub)
        except OSError:
            pass

    removed_configs: list[str] = []
    config_dir = Path(state.runs_base_dir) / "project_configs"
    meta = _read_json(proj_dir / "project_meta.json") if proj_dir.exists() else {}
    candidate_config_paths: set[Path] = set()
    if isinstance(meta, dict):
        config_path = str(meta.get("config_path", "") or "").strip()
        if config_path:
            candidate_config_paths.add(Path(config_path))
    if config_dir.exists():
        for cfg in config_dir.glob("*.yaml"):
            stem = cfg.stem
            if stem == project_id or stem.startswith(f"{project_id}--"):
                candidate_config_paths.add(cfg)
    for cfg in sorted(candidate_config_paths):
        try:
            if cfg.exists() and cfg.is_file():
                cfg.unlink()
                removed_configs.append(str(cfg.name))
        except OSError as exc:
            messages.append(msg_log(sys_agent, f"删除项目配置失败: {cfg}: {exc}", "warning"))

    if proj_dir.exists() and proj_dir.is_dir():
        try:
            shutil.rmtree(proj_dir)
            detail = f"，同时删除 {len(removed_configs)} 个配置文件" if removed_configs else ""
            messages.append(msg_log(sys_agent, f"项目 [{project_id}] 及阶段产物已删除{detail}", "success"))
        except OSError as exc:
            messages.append(msg_log(sys_agent, f"删除项目目录失败: {exc}", "error"))
    else:
        detail = f"，已删除 {len(removed_configs)} 个配置文件" if removed_configs else ""
        messages.append(msg_log(sys_agent, f"项目 [{project_id}] 目录不存在{detail}", "warning"))

    return messages


def _build_status_summary(state: "BridgeState", target_layer: str = "all", for_user_id: str = "") -> str:
    """Build a human-readable status summary for all running/recent projects."""
    lines: list[str] = []
    projects_dir = state.projects_dir()

    active_projects: dict[str, LobsterAgent] = {}
    for agent in state.agents.values():
        if not agent.project_id or agent.status not in ("working", "idle"):
            continue
        if for_user_id and _project_owner_id(state, agent.project_id) != for_user_id:
            continue
        active_projects[agent.project_id] = agent

    project_dirs = sorted(
        (d for d in projects_dir.iterdir() if d.is_dir() and not d.name.startswith("_")),
        key=lambda p: p.stat().st_mtime, reverse=True,
    ) if projects_dir.is_dir() else []

    if not project_dirs:
        return "当前没有任何项目。"

    visible_project_dirs: list[Path] = []
    for proj_dir in project_dirs:
        if for_user_id and _project_owner_id(state, proj_dir.name) != for_user_id:
            continue
        visible_project_dirs.append(proj_dir)

    if not visible_project_dirs:
        return "当前没有你创建的项目。"

    for proj_dir in visible_project_dirs[:5]:
        pid = proj_dir.name
        if target_layer != "all" and pid not in active_projects:
            continue

        lines.append(f"📋 项目: {pid}")

        agent = active_projects.get(pid)
        if agent:
            layer_cn = {"idea": "调研", "experiment": "设计", "coding": "编码",
                        "execution": "执行", "writing": "写作"}.get(agent.layer, agent.layer)
            status_cn = {"working": "运行中", "idle": "空闲", "error": "错误",
                         "done": "完成"}.get(agent.status, agent.status)
            lines.append(f"  状态: {status_cn} | 层: {layer_cn}")
            if agent.current_stage:
                sname = STAGE_NAMES.get(agent.current_stage, "?")
                lines.append(f"  当前阶段: S{agent.current_stage} {sname}")
        else:
            lines.append("  状态: 未活跃")

        stage_statuses = []
        for s in range(1, PROJECT_TERMINAL_STAGE + 1):
            health = _read_json(proj_dir / f"stage-{s:02d}" / "stage_health.json")
            if health:
                st = health.get("status", "?")
                dur = health.get("duration_sec")
                err = health.get("error")
                icon = "✅" if st == "done" else "❌" if st == "failed" else "🔄"
                sname = STAGE_NAMES.get(s, "?")
                entry = f"  {icon} S{s} {sname}"
                if dur is not None:
                    if dur < 60:
                        entry += f" ({dur:.0f}s)"
                    else:
                        entry += f" ({dur / 60:.1f}min)"
                if err:
                    entry += f" — {err[:60]}"
                stage_statuses.append(entry)

        if stage_statuses:
            last_done = [s for s in stage_statuses if "✅" in s]
            failed = [s for s in stage_statuses if "❌" in s]
            lines.append(
                f"  已完成: {len(last_done)}/{PROJECT_TERMINAL_STAGE} 阶段"
                + (f", {len(failed)} 失败" if failed else "")
            )
            for s in stage_statuses:
                lines.append(s)
        else:
            lines.append("  暂无阶段数据")

        heartbeat = _read_json(proj_dir / "heartbeat.json")
        if heartbeat:
            ts = heartbeat.get("timestamp", "")
            lines.append(f"  最后心跳: {ts}")

        lines.append("")

    gpu_info = state.gpu_allocator.summary()
    lines.append(f"🖥️ GPU: {gpu_info['free']}/{gpu_info['total']} 空闲")
    if gpu_info["assignments"]:
        for proj, gpus in gpu_info["assignments"].items():
            lines.append(f"  {proj} → GPU {gpus}")

    return "\n".join(lines)


# ── Data structures ─────────────────────────────────────────────────────────

@dataclass
class Task:
    id: str
    project_id: str
    run_dir: str
    config_path: str
    source_layer: str
    target_layer: str
    topic: str = ""
    status: str = "pending"          # pending | assigned | completed | failed
    assigned_to: str | None = None
    created_at: int = 0
    assigned_at: int = 0
    completed_at: int = 0
    waiting_reason: str = ""
    waiting_since: int = 0

    def to_dict(self) -> dict:
        return {k: v for k, v in self.__dict__.items()}

    @staticmethod
    def from_dict(d: dict) -> "Task":
        t = Task(
            id=d["id"], project_id=d["project_id"], run_dir=d["run_dir"],
            config_path=d.get("config_path", ""),
            source_layer=d["source_layer"], target_layer=d["target_layer"],
            topic=d.get("topic", ""),
        )
        t.status = d.get("status", "pending")
        t.assigned_to = d.get("assigned_to")
        t.created_at = d.get("created_at", 0)
        t.assigned_at = d.get("assigned_at", 0)
        t.completed_at = d.get("completed_at", 0)
        t.waiting_reason = d.get("waiting_reason", "")
        t.waiting_since = d.get("waiting_since", 0)
        return t


@dataclass
class TaskQueue:
    """File-backed FIFO task queue."""
    name: str
    path: Path
    tasks: list[Task] = field(default_factory=list)

    def load(self):
        data = _read_json(self.path)
        if data and isinstance(data, list):
            self.tasks = [Task.from_dict(d) for d in data]

    def save(self):
        _write_json(self.path, [t.to_dict() for t in self.tasks])

    def push(self, task: Task):
        self.tasks.append(task)
        self.save()

    def peek_pending(self) -> Task | None:
        for t in self.tasks:
            if t.status == "pending":
                return t
        return None

    def assign(self, task_id: str, agent_id: str) -> Task | None:
        for t in self.tasks:
            if t.id == task_id and t.status == "pending":
                t.status = "assigned"
                t.assigned_to = agent_id
                t.assigned_at = _now_ms()
                t.waiting_reason = ""
                t.waiting_since = 0
                self.save()
                return t
        return None

    def complete(self, task_id: str):
        for t in self.tasks:
            if t.id == task_id:
                t.status = "completed"
                t.completed_at = _now_ms()
                self.save()
                return

    def fail(self, task_id: str):
        for t in self.tasks:
            if t.id == task_id:
                t.status = "failed"
                self.save()
                return

    def pending_count(self) -> int:
        return sum(1 for t in self.tasks if t.status == "pending")

    def summary(self) -> dict:
        return {
            "name": self.name,
            "total": len(self.tasks),
            "pending": sum(1 for t in self.tasks if t.status == "pending"),
            "assigned": sum(1 for t in self.tasks if t.status == "assigned"),
            "completed": sum(1 for t in self.tasks if t.status == "completed"),
            "waitingReasons": {
                reason: sum(
                    t.status == "pending" and t.waiting_reason == reason
                    for t in self.tasks
                )
                for reason in sorted({
                    t.waiting_reason for t in self.tasks
                    if t.status == "pending" and t.waiting_reason
                })
            },
        }


class AttachedPidProcess:
    """Small Popen-compatible handle for a child surviving bridge restart."""

    def __init__(self, pid: int, run_dir: Path) -> None:
        self.pid = int(pid)
        self.run_dir = run_dir

    def poll(self) -> int | None:
        return None if (
            _pid_is_alive(self.pid) and _pid_cmdline_contains_run_dir(self.pid, self.run_dir)
        ) else 0

    def terminate(self) -> None:
        try:
            os.kill(self.pid, signal.SIGTERM)
        except OSError:
            pass

    def kill(self) -> None:
        try:
            os.kill(self.pid, signal.SIGKILL)
        except OSError:
            pass

    def wait(self, timeout: float | None = None) -> int:
        deadline = time.monotonic() + (timeout or 0)
        while self.poll() is None:
            if timeout is not None and time.monotonic() >= deadline:
                raise subprocess.TimeoutExpired(str(self.pid), timeout)
            time.sleep(0.05)
        return 0


@dataclass
class DiscussionGroup:
    """Tracks a group of L1 agents discussing the same topic."""
    project_id: str
    topic: str
    config_path: str
    agent_ids: list[str] = field(default_factory=list)
    run_dirs: dict[str, str] = field(default_factory=dict)   # agent_id -> run_dir
    completed_s7: set[str] = field(default_factory=set)       # agent_ids done with S7
    completed_s8: set[str] = field(default_factory=set)       # agent_ids done with S8
    best_agent_id: str = ""
    status: str = "gathering"    # gathering | waiting | discussing | done
    discussion_process: subprocess.Popen | None = field(default=None, repr=False)
    discussion_output_dir: str = ""

    def all_ready(self) -> bool:
        return len(self.completed_s7) >= len(self.agent_ids) and len(self.agent_ids) >= 2

    def all_s8_done(self) -> bool:
        eligible = [
            aid for aid in self.agent_ids
            if self.run_dirs.get(aid) and (Path(self.run_dirs[aid]) / "stage-07").exists()
        ]
        return bool(eligible) and len(self.completed_s8) >= len(eligible)

    def synthesis_dirs(self) -> list[str]:
        dirs = []
        for aid in self.agent_ids:
            rd = self.run_dirs.get(aid, "")
            if rd:
                dirs.append(str(Path(rd) / "stage-07"))
        return dirs


@dataclass
class LobsterAgent:
    id: str
    name: str
    layer: str
    run_id: str
    run_dir: str
    config_path: str
    project_id: str = ""
    status: str = "idle"
    current_stage: int | None = None
    current_task: str = ""
    assigned_task_id: str | None = None
    stage_progress: dict[int, str] = field(default_factory=dict)
    role_tag: str = ""
    process: subprocess.Popen | None = field(default=None, repr=False)
    _prev_heartbeat: dict = field(default_factory=dict, repr=False)
    _prev_checkpoint: dict = field(default_factory=dict, repr=False)
    _known_artifacts: set[str] = field(default_factory=set, repr=False)

    def to_frontend(self) -> dict:
        return {
            "id": self.id, "name": self.name, "layer": self.layer,
            "runId": self.run_id, "status": self.status,
            "currentStage": self.current_stage,
            "currentTask": self.current_task,
            "stageProgress": self.stage_progress,
            "projectId": self.project_id,
            "roleTag": self.role_tag,
        }


class GpuAllocator:
    """Manages GPU assignment across concurrent projects."""

    def __init__(self, total_gpus: int = 8, gpus_per_project: int = 2):
        self.total_gpus = total_gpus
        self.gpus_per_project = gpus_per_project
        self.assignments: dict[str, list[int]] = {}  # project_id -> [gpu_ids]
        self._occupied: set[int] = set()

    def available_count(self) -> int:
        return self.total_gpus - len(self._occupied)

    def can_allocate(self) -> bool:
        return self.available_count() >= self.gpus_per_project

    def allocate(self, project_id: str) -> list[int] | None:
        if project_id in self.assignments:
            return self.assignments[project_id]
        if not self.can_allocate():
            return None
        free = sorted(set(range(self.total_gpus)) - self._occupied)
        assigned = free[:self.gpus_per_project]
        self.assignments[project_id] = assigned
        self._occupied.update(assigned)
        return assigned

    def release(self, project_id: str) -> list[int]:
        gpus = self.assignments.pop(project_id, [])
        self._occupied -= set(gpus)
        return gpus

    def get(self, project_id: str) -> list[int] | None:
        return self.assignments.get(project_id)

    def summary(self) -> dict:
        return {
            "total": self.total_gpus,
            "per_project": self.gpus_per_project,
            "free": self.available_count(),
            "assignments": {k: v for k, v in self.assignments.items()},
        }


@dataclass
class BridgeState:
    agents: dict[str, LobsterAgent] = field(default_factory=dict)
    queues: dict[str, TaskQueue] = field(default_factory=dict)
    clients: set = field(default_factory=set)
    python_path: str = ""
    agent_package_dir: str = ""
    runs_base_dir: str = ""
    gpu_allocator: GpuAllocator = field(default_factory=GpuAllocator)
    result_registry: "ResultRegistry | None" = None
    auto_loop: bool = False
    # Discussion mode: L1 agents discuss after S7, before S8
    discussion_mode: bool = True
    discussion_groups: dict[str, DiscussionGroup] = field(default_factory=dict)
    discussion_rounds: int = 3
    discussion_models: list[str] = field(default_factory=lambda: [DEFAULT_QWEN3_MODEL])
    # Cross-project discussion: agents waiting for a peer to discuss with
    discussion_waiting: dict[str, "LobsterAgent"] = field(default_factory=dict)
    # Idea factory: L1 idle → produce ideas via S7+S8
    idea_factory_topic: str = ""
    idea_factory_config: str = ""
    idea_factory_remaining: int = 0  # 0=disabled, -1=infinite, N=count
    idea_factory_produced: int = 0
    _fail_counts: dict[str, int] = field(default_factory=dict)  # project_id → consecutive fail count
    # Lab mode: track which sub-projects belong to the same batch
    lab_batches: dict[str, list[str]] = field(default_factory=dict)  # base_id → [sub_project_ids]
    # 多用户支持：websocket → user_id 映射
    user_clients: dict[websockets.ServerConnection, str] = field(default_factory=dict)
    literature_schedules: list[dict] = field(default_factory=list)
    _schedule_poll_at: float = 0.0

    def projects_dir(self) -> Path:
        return Path(self.runs_base_dir) / "projects"

    def queues_dir(self) -> Path:
        return Path(self.runs_base_dir) / "queues"

    def schedules_path(self) -> Path:
        return Path(self.runs_base_dir) / "schedules" / "literature_tasks.json"


# ── Message builders ────────────────────────────────────────────────────────

def msg_agent_update(agent: LobsterAgent) -> dict:
    return {"type": "agent_update", "payload": agent.to_frontend()}

def msg_stage_update(agent_id: str, stage: int, status: str) -> dict:
    return {"type": "stage_update", "payload": {"agentId": agent_id, "stage": stage, "status": status}}

def msg_artifact(repo_id: str, filename: str, agent_name: str, size: str,
                  project_id: str = "", content: str = "", stage: int = 0) -> dict:
    payload: dict = {
        "id": _uid(), "repoId": repo_id, "projectId": project_id, "filename": filename,
        "producedBy": agent_name, "timestamp": _now_ms(), "size": size, "status": "fresh",
    }
    if content:
        payload["content"] = content
    if stage:
        payload["stage"] = stage
    return {"type": "artifact_produced", "payload": payload}


_NO_CONTENT_ARTIFACTS: set[str] = {"paper_revised.md", "paper_draft.md", "outline.md", "reviews.md"}

def _extract_artifact_summary(path: Path, filename: str, max_chars: int = 500) -> str:
    """Extract a human-readable summary from an artifact file."""
    if filename in _NO_CONTENT_ARTIFACTS:
        return ""
    try:
        # 对需要完整内容的文件，直接返回全文（用于前端展示和导出）
        if filename in _FULL_CONTENT_ARTIFACTS:
            text = path.read_text(encoding="utf-8", errors="ignore")
            return text.strip()[:50000]

        if path.is_dir():
            children = list(path.iterdir())
            file_count = sum(1 for c in children if c.is_file())
            md_titles = []
            for c in sorted(children)[:8]:
                if c.suffix == ".md" and c.is_file():
                    first_line = c.read_text(encoding="utf-8", errors="ignore").strip().split("\n")[0]
                    title = first_line.lstrip("#").strip()
                    if title:
                        md_titles.append(title)
            if md_titles:
                return f"{file_count} files: " + "; ".join(md_titles[:6])
            return f"{file_count} files"

        text = path.read_text(encoding="utf-8", errors="ignore")
        if not text.strip():
            return ""

        if filename == "hypotheses.md":
            lines = text.strip().split("\n")
            hyp_titles = []
            for line in lines:
                stripped = line.strip()
                if stripped.startswith("##") and "hypothesis" in stripped.lower():
                    title = stripped.lstrip("#").strip().lstrip("—").lstrip("-").strip()
                    title = title.removeprefix("Final Hypothesis").strip()
                    title = title.removeprefix("Hypothesis").strip()
                    title = title.lstrip("0123456789").strip().lstrip("—").lstrip("-").strip()
                    title = title.replace("**", "").replace("*", "")
                    if title:
                        hyp_titles.append(f"• {title}")
            if hyp_titles:
                return "\n".join(hyp_titles)[:max_chars]

        if filename.endswith(".md"):
            lines = text.strip().split("\n")
            summary_parts = []
            for line in lines:
                stripped = line.strip()
                if not stripped:
                    if summary_parts:
                        break
                    continue
                clean = stripped.lstrip("#").strip().rstrip("---").strip()
                if clean:
                    summary_parts.append(clean)
                if len(" ".join(summary_parts)) > max_chars:
                    break
            return " ".join(summary_parts)[:max_chars]

        if filename.endswith((".yaml", ".yml")):
            lines = text.strip().split("\n")
            top_keys = []
            for line in lines[:15]:
                if line and not line.startswith(" ") and not line.startswith("#") and ":" in line:
                    top_keys.append(line.split(":")[0].strip())
            return f"keys: {', '.join(top_keys[:8])}" if top_keys else ""

        if filename.endswith(".json"):
            data = json.loads(text)
            if isinstance(data, dict):
                # knowledge_entry.json: extract topic + hypothesis names
                if "topic" in data and "hypotheses" in data and isinstance(data["hypotheses"], list):
                    topic = str(data["topic"])[:120]
                    hyp_names = []
                    for h in data["hypotheses"][:5]:
                        if isinstance(h, dict):
                            name = h.get("name") or h.get("id", "")
                            status = h.get("status", "")
                            entry = str(name)[:60]
                            if status:
                                entry += f" ({status[:20]})"
                            hyp_names.append(entry)
                    summary = topic
                    if hyp_names:
                        summary += "\n" + "\n".join(f"• {n}" for n in hyp_names)
                    return summary[:max_chars]

                keys = list(data.keys())[:8]
                preview_parts = []
                for k in keys[:4]:
                    v = data[k]
                    if isinstance(v, str) and len(v) < 80:
                        preview_parts.append(f"{k}: {v}")
                    elif isinstance(v, (int, float, bool)):
                        preview_parts.append(f"{k}: {v}")
                    elif isinstance(v, list):
                        preview_parts.append(f"{k}: [{len(v)} items]")
                return "; ".join(preview_parts) if preview_parts else f"keys: {', '.join(keys)}"
            if isinstance(data, list):
                return f"{len(data)} entries"

        if filename.endswith(".jsonl"):
            line_count = text.count("\n")
            first_line = text.strip().split("\n")[0] if text.strip() else ""
            if first_line:
                try:
                    entry = json.loads(first_line)
                    title = entry.get("title") or entry.get("name") or entry.get("id", "")
                    if title:
                        return f"{line_count} entries — first: {str(title)[:80]}"
                except Exception:
                    pass
            return f"{line_count} entries"

    except Exception:
        pass
    return ""

# ── 用户认证辅助 ────────────────────────────────────────────────────────────

def get_ws_user_id(state: BridgeState, websocket: websockets.ServerConnection) -> str:
    """从 WebSocket 连接获取当前用户 ID，未认证返回空字符串。"""
    return state.user_clients.get(websocket, "")


def enforce_owner(state: BridgeState, websocket: websockets.ServerConnection, project_id: str) -> bool:
    """检查当前用户是否有权限操作该项目。"""
    user_id = get_ws_user_id(state, websocket)
    if not user_id:
        return True  # 未认证用户向后兼容，允许操作
    proj_dir = state.projects_dir() / project_id
    if not proj_dir.exists():
        return True
    meta = _read_json(proj_dir / "project_meta.json") or {}
    owner = meta.get("user_id", "")
    return not owner or owner == user_id


def user_project_filter(state: BridgeState, user_id: str, projects: list[dict]) -> list[dict]:
    """按用户过滤项目列表。"""
    if not user_id:
        return projects  # 未认证用户看到所有项目
    return [p for p in projects if p.get("user_id", "") == user_id]


def _project_owner_id(state: BridgeState, project_id: str) -> str:
    """Return the persisted owner id for a project, or empty when unknown."""
    if not project_id:
        return ""
    meta = _read_json(state.projects_dir() / project_id / "project_meta.json") or {}
    return str(meta.get("user_id", "") or "")


def _ws_can_see_project(state: BridgeState, websocket: object, project_id: str) -> bool:
    """Authenticated users can only see projects they own."""
    if not project_id:
        return True
    user_id = state.user_clients.get(websocket, "")
    if not user_id:
        return True
    return _project_owner_id(state, project_id) == user_id


def _message_project_id(state: BridgeState, msg: dict) -> str:
    payload = msg.get("payload") if isinstance(msg, dict) else None
    if isinstance(payload, dict):
        project_id = str(payload.get("projectId", "") or payload.get("project_id", "") or "")
        if project_id:
            return project_id
        if msg.get("type") == "stage_update":
            agent_id = str(payload.get("agentId", "") or "")
            agent = state.agents.get(agent_id)
            return agent.project_id if agent else ""
    return ""


def _message_for_ws(state: BridgeState, websocket: object, msg: dict) -> dict | None:
    """Return a per-user safe message, or None if the user must not see it."""
    target_user_id = str(msg.get("_targetUserId", "") or "")
    if target_user_id and state.user_clients.get(websocket, "") != target_user_id:
        return None

    if msg.get("type") == "project_list":
        user_id = state.user_clients.get(websocket, "")
        payload = msg.get("payload", [])
        projects = payload if isinstance(payload, list) else []
        return {"type": "project_list", "payload": user_project_filter(state, user_id, projects)}

    project_id = _message_project_id(state, msg)
    if project_id and not _ws_can_see_project(state, websocket, project_id):
        return None

    if target_user_id:
        safe = dict(msg)
        safe.pop("_targetUserId", None)
        return safe
    return msg


def _mark_messages_private(messages: list[dict], user_id: str) -> list[dict]:
    if user_id:
        for msg in messages:
            msg.setdefault("_targetUserId", user_id)
    return messages


def msg_log(agent: LobsterAgent, message: str, level: str = "info", stage: int | None = None) -> dict:
    payload = {
        "id": _uid(), "agentId": agent.id, "agentName": agent.name,
        "layer": agent.layer, "stage": stage or agent.current_stage,
        "message": message, "level": level, "timestamp": _now_ms(),
    }
    if agent.project_id:
        payload["projectId"] = agent.project_id
    return {"type": "log", "payload": payload}

def msg_queue_update(queues: dict[str, TaskQueue]) -> dict:
    return {"type": "queue_update", "payload": {name: q.summary() for name, q in queues.items()}}

def msg_project_list(projects: list[dict]) -> dict:
    return {"type": "project_list", "payload": projects}


def msg_chat_message(
    role: str,
    content: str,
    *,
    target_layer: str = "all",
    project_id: str = "",
    message_id: str = "",
    timestamp: int | None = None,
) -> dict:
    payload = {
        "id": message_id or f"{role}-{_uid()}",
        "role": role,
        "content": content,
        "timestamp": timestamp or _now_ms(),
        "targetLayer": target_layer,
    }
    if project_id:
        payload["projectId"] = project_id
    return {"type": "chat_message", "payload": payload}


def msg_feedback_ack(
    message_id: str,
    content: str,
    target_layer: str = "all",
    plan_update: str = "",
    project_id: str = "",
) -> dict:
    return msg_chat_message(
        "system",
        content,
        target_layer=target_layer,
        project_id=project_id,
        message_id=f"sys-{_uid()}",
        timestamp=_now_ms(),
    )


_PROJECT_CHAT_ARTIFACTS: tuple[tuple[str, str, int], ...] = (
    ("stage-08/core_ideas.md", "Core Ideas", 150),
    ("stage-08/idea_decision_table.md", "Idea Decision Table", 145),
    ("stage-08/idea_tournament.md", "Idea Tournament", 140),
    ("stage-08/challenge_insight_tree.md", "Challenge Insight Tree", 135),
    ("stage-08/idea_branch_synthesis.md", "Idea Branch Synthesis", 125),
    ("stage-08/idea_role_review.md", "Idea Role Review", 120),
    ("stage-08/idea_review.md", "Idea Review", 115),
    ("stage-08/candidate_ideas.md", "Candidate Ideas", 105),
    ("stage-08/hypotheses.md", "Hypotheses", 100),
    ("stage-07/synthesis.md", "Synthesis", 95),
    ("stage-06/cards", "Knowledge Cards", 70),
    ("stage-05/shortlist.jsonl", "Shortlist", 55),
    ("stage-04/reference_paper_text.md", "Uploaded Reference Text", 90),
    ("stage-04/web_context.md", "Web Search Context", 45),
    ("stage-04/search_plan.yaml", "Search Plan", 35),
)


def _project_run_dirs(project_dir: Path) -> list[Path]:
    run_dirs = sorted(p for p in project_dir.glob("run-*") if p.is_dir())
    return run_dirs or [project_dir]


def _read_jsonl_titles(path: Path, limit: int = 10) -> str:
    titles: list[str] = []
    try:
        for line in path.read_text(encoding="utf-8", errors="ignore").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(row, dict):
                title = str(row.get("title", "")).strip()
                if title:
                    titles.append(f"- {title}")
            if len(titles) >= limit:
                break
    except OSError:
        return ""
    return "\n".join(titles)


def _read_cards_summary(cards_dir: Path, limit: int = 3, max_chars: int = 3000) -> str:
    snippets: list[str] = []
    for card_path in sorted(cards_dir.glob("*.md"))[:limit]:
        try:
            snippets.append(card_path.read_text(encoding="utf-8", errors="ignore")[:900])
        except OSError:
            continue
    return "\n\n".join(snippets)[:max_chars]


def _tokenize_for_context(text: str) -> set[str]:
    tokens = set(re.findall(r"[A-Za-z0-9][A-Za-z0-9_.+-]{1,}|[\u4e00-\u9fff]{2,}", text.lower()))
    stopwords = {
        "这个", "当前", "项目", "论文", "文献", "总结", "一下", "什么", "怎么",
        "哪些", "我们", "需要", "请用", "中文", "the", "and", "for", "with",
        "that", "this", "from", "into", "about",
    }
    return {token for token in tokens if token not in stopwords and len(token) >= 2}


def _split_context_text(text: str, chunk_chars: int = 900) -> list[str]:
    text = text.strip()
    if not text:
        return []
    blocks = [block.strip() for block in re.split(r"\n\s*\n", text) if block.strip()]
    chunks: list[str] = []
    current = ""
    for block in blocks:
        if len(current) + len(block) + 2 <= chunk_chars:
            current = f"{current}\n\n{block}".strip()
        else:
            if current:
                chunks.append(current)
            if len(block) <= chunk_chars:
                current = block
            else:
                for start in range(0, len(block), chunk_chars):
                    chunks.append(block[start:start + chunk_chars])
                current = ""
    if current:
        chunks.append(current)
    return chunks


def _context_query_bonus(label: str, question: str) -> int:
    q = question.lower()
    label_lower = label.lower()
    bonus = 0
    if any(word in q for word in ("idea", "想法", "方案", "创新", "选题")):
        if label_lower in {"core ideas", "hypotheses"}:
            bonus += 80
    if any(word in q for word in ("总结", "综述", "结论", "gap", "空缺")):
        if label_lower == "synthesis":
            bonus += 70
    if any(word in q for word in ("相似", "相关工作", "已有工作", "区分", "文献")):
        if label_lower in {"shortlist", "knowledge cards", "uploaded reference text", "web search context"}:
            bonus += 55
    if any(word in q for word in ("上传", "这篇", "pdf", "paper")):
        if label_lower == "uploaded reference text":
            bonus += 80
    return bonus


def _collect_project_chat_context(project_dir: Path, question: str = "", max_chars: int = 8_000) -> str:
    candidates: list[tuple[int, str, str]] = []
    query_tokens = _tokenize_for_context(question)
    for run_dir in _project_run_dirs(project_dir):
        run_label = run_dir.name
        for rel_path, label, base_score in _PROJECT_CHAT_ARTIFACTS:
            target = run_dir / rel_path
            if not target.exists():
                continue
            if target.is_dir():
                card_paths = sorted(target.glob("*.md"))
                for card_path in card_paths[:12]:
                    try:
                        card_text = card_path.read_text(encoding="utf-8", errors="ignore")
                    except OSError:
                        continue
                    for chunk in _split_context_text(card_text, 800)[:2]:
                        overlap = len(query_tokens & _tokenize_for_context(chunk))
                        score = base_score + _context_query_bonus(label, question) + overlap * 20
                        title = f"## {run_label} · {label} · {card_path.name}"
                        candidates.append((score, title, chunk))
                continue
            elif target.suffix == ".jsonl":
                content = _read_jsonl_titles(target)
            else:
                try:
                    content = target.read_text(encoding="utf-8", errors="ignore")
                except OSError:
                    content = ""
            if not content.strip():
                continue
            for chunk in _split_context_text(content, 900)[:8]:
                overlap = len(query_tokens & _tokenize_for_context(chunk))
                score = base_score + _context_query_bonus(label, question) + overlap * 20
                candidates.append((score, f"## {run_label} · {label}", chunk))

    upload_dir = project_dir / "reference_uploads"
    if upload_dir.is_dir():
        uploaded = sorted(p.name for p in upload_dir.glob("*.pdf"))
        if uploaded:
            candidates.append((30, "## Uploaded References", "\n".join(f"- {name}" for name in uploaded[:20])))

    if not candidates:
        return ""

    candidates.sort(key=lambda item: item[0], reverse=True)
    chunks: list[str] = []
    used = 0
    seen: set[str] = set()
    for score, title, content in candidates:
        normalized = hashlib.md5(content[:400].encode("utf-8", errors="ignore")).hexdigest()
        if normalized in seen:
            continue
        seen.add(normalized)
        block = f"{title}\n{content.strip()}"
        if used + len(block) + 2 > max_chars:
            continue
        chunks.append(block)
        used += len(block) + 2
        if len(chunks) >= 8:
            break

    return "\n\n".join(chunks)


def _project_chat_history_path(project_dir: Path) -> Path:
    return project_dir / "chat_history.jsonl"


def _load_project_chat_history(project_dir: Path, limit: int = 20) -> list[dict[str, object]]:
    path = _project_chat_history_path(project_dir)
    rows: list[dict[str, object]] = []
    if not path.exists():
        return rows
    try:
        for line in path.read_text(encoding="utf-8", errors="ignore").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                item = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(item, dict):
                rows.append(item)
    except OSError:
        return []
    return rows[-limit:]


def _append_project_chat_history(
    project_dir: Path,
    role: str,
    content: str,
    project_id: str,
    *,
    message_id: str = "",
) -> dict[str, object]:
    entry: dict[str, object] = {
        "id": message_id or f"{role}-{_uid()}",
        "role": role,
        "content": content,
        "projectId": project_id,
        "targetLayer": "project",
        "timestamp": _now_ms(),
    }
    path = _project_chat_history_path(project_dir)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")
    except OSError:
        pass
    return entry


def _history_entry_to_message(entry: dict[str, object]) -> dict:
    return msg_chat_message(
        str(entry.get("role", "system")),
        str(entry.get("content", "")),
        target_layer=str(entry.get("targetLayer", "project")),
        project_id=str(entry.get("projectId", "")),
        message_id=str(entry.get("id", "")),
        timestamp=int(entry.get("timestamp", _now_ms()) or _now_ms()),
    )


def _clean_project_chat_answer(text: str) -> str:
    cleaned = text.strip()
    if not cleaned:
        return cleaned
    cleaned = re.sub(r"<think>.*?</think>", "", cleaned, flags=re.IGNORECASE | re.DOTALL).strip()
    if not cleaned:
        return cleaned
    lowered = cleaned.lower()
    for marker in ("最终回答：", "最终答案：", "答：", "final answer:", "final response:", "final:", "答案：", "回答："):
        idx = lowered.rfind(marker.lower())
        if idx != -1:
            return cleaned[idx + len(marker):].strip()

    starts_like_reasoning = (
        cleaned.startswith("Thinking Process:")
        or cleaned.startswith("思考过程")
        or lowered.startswith("1.  **analyze the request")
        or lowered.startswith("1. **analyze the request")
        or lowered.startswith("1. analyze the request")
        or "user question:" in lowered[:400]
        or "review the provided context" in lowered[:800]
        or "determine the response" in lowered[:800]
        or "system instruction" in lowered[:800]
        or "previous conversation:" in lowered[:800]
    )

    if starts_like_reasoning:
        lines = cleaned.splitlines()
        for idx, line in enumerate(lines):
            stripped = line.strip()
            normalized = stripped.lower()
            if normalized in ("answer:", "final answer:", "最终答案：", "答案：", "回答：", "response:", "final response:"):
                return _trim_project_chat_answer("\n".join(lines[idx + 1:]).strip())
            if re.match(r"^#+\s*(最终答案|最终回答|回答|答案)\s*[:：]?$", stripped, re.I):
                return _trim_project_chat_answer("\n".join(lines[idx + 1:]).strip())
            if stripped.startswith(("##", "###", "- ", "* ", "1. ")) and idx > 3:
                tail = "\n".join(lines[idx:]).strip()
                if not any(marker in tail.lower() for marker in (
                    "analyze the request",
                    "review the provided context",
                    "determine the response",
                    "project id:",
                )):
                    return _trim_project_chat_answer(tail)
        kept = [
            line for line in lines
            if not re.match(r"^\s*\d+\.\s+\*\*?(analyze|review|determine|draft|evaluate)", line, re.I)
            and not line.strip().lower().lstrip("* ").startswith((
                "thinking process",
                "analyze the request",
                "review the provided context",
                "determine the response",
                "drafting the response",
                "review the provided context",
                "evaluate",
                "draft",
                "refine",
                "self-correction",
                "user question",
                "context:",
                "project id:",
                "research topic:",
                "core ideas:",
                "shortlist:",
                "uploaded reference text:",
                "previous conversation:",
                "constraint:",
                "language:",
                "format:",
            ))
        ]
        cleaned_kept = "\n".join(kept).strip()
        if cleaned_kept and not any(marker in cleaned_kept.lower()[:700] for marker in (
            "analyze the request",
            "review context",
            "review the provided context",
            "system instructions",
            "determine the response",
        )):
            return _trim_project_chat_answer(cleaned_kept)
        return "这次模型返回了内部分析过程，没有生成有效答案。请重新提问，或换一个更稳定的对话模型。"
    return _trim_project_chat_answer(cleaned)


def _trim_project_chat_answer(text: str, max_chars: int = 24000) -> str:
    if len(text) <= max_chars:
        lines = [
            line for line in text.splitlines()
            if not re.match(r"^\s*\d+\.\s+\*\*?(analyze|review|determine|draft|evaluate)", line, re.I)
            and not line.strip().lower().lstrip("* ").startswith((
                "project id:",
                "research topic:",
                "core ideas:",
                "shortlist:",
                "uploaded reference text:",
                "previous conversation:",
                "determine the response",
                "drafting the response",
            ))
        ]
        return "\n".join(lines).strip()
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    useful: list[str] = []
    for line in lines:
        if line.lower().lstrip("* ").startswith((
            "thinking process",
            "analyze",
            "review",
            "evaluate",
            "draft",
            "project id:",
            "research topic:",
            "core ideas:",
            "shortlist:",
            "uploaded reference text:",
            "previous conversation:",
            "determine the response",
        )):
            continue
        useful.append(line)
    joined = "\n".join(useful).strip() or text
    return joined[:max_chars].rstrip()


def _list_project_artifact_messages(project_id: str, state: "BridgeState") -> list[dict]:
    project_dir = state.projects_dir() / project_id
    if not project_dir.exists():
        return []

    messages: list[dict] = []
    for run_dir in _project_run_dirs(project_dir):
        producer = run_dir.name if run_dir != project_dir else project_id
        for stage, outputs in STAGE_OUTPUTS.items():
            stage_dir = run_dir / f"stage-{stage:02d}"
            if not stage_dir.is_dir():
                continue
            for expected in outputs:
                artifact_path = stage_dir / expected.rstrip("/")
                if not artifact_path.exists():
                    continue
                size = "dir" if artifact_path.is_dir() else f"{artifact_path.stat().st_size / 1024:.1f} KB"
                content = _extract_artifact_summary(artifact_path, expected)
                messages.append(
                    msg_artifact(
                        REPO_FOR_STAGE.get(stage, "knowledge"),
                        expected,
                        producer,
                        size,
                        project_id,
                        content,
                        stage=stage,
                    )
                )
        deliverables_dir = run_dir / "deliverables"
        if deliverables_dir.is_dir():
            for expected in (
                "manifest.json",
                "paper_final.md",
                "paper.tex",
                "paper.pdf",
                "references.bib",
                "verification_report.json",
            ):
                artifact_path = deliverables_dir / expected
                if not artifact_path.exists():
                    continue
                size = f"{artifact_path.stat().st_size / 1024:.1f} KB"
                content = _extract_artifact_summary(artifact_path, expected)
                messages.append(
                    msg_artifact(
                        "deliverables",
                        expected,
                        producer,
                        size,
                        project_id,
                        content,
                        stage=26,
                    )
                )
            for expected in ("code", "charts"):
                artifact_path = deliverables_dir / expected
                if not artifact_path.is_dir():
                    continue
                content = _extract_artifact_summary(artifact_path, expected)
                messages.append(
                    msg_artifact(
                        "deliverables",
                        f"{expected}/",
                        producer,
                        "dir",
                        project_id,
                        content,
                        stage=26,
                    )
                )
    return messages


def _load_chat_llm_client_from_section(section: dict, state: "BridgeState") -> "object | None":
    try:
        if state.agent_package_dir not in sys.path:
            sys.path.insert(0, state.agent_package_dir)
        from researchclaw.llm import resolve_provider_base_url
        from researchclaw.llm.client import LLMClient, LLMConfig

        provider = str(section.get("provider", "openai-compatible") or "openai-compatible")
        configured_base_url = str(section.get("base_url", "") or "")
        api_key = str(
            section.get("api_key", "")
            or os.environ.get(str(section.get("api_key_env", "OPENAI_API_KEY")), "")
            or os.environ.get("RESEARCHCLAW_API_KEY", "")
        )
        if not api_key:
            return None
        base_url = resolve_provider_base_url(provider, configured_base_url)
        fallback_models = section.get("fallback_models", [])
        if not isinstance(fallback_models, list):
            fallback_models = []
        extra_body = section.get("extra_body", {})
        if not isinstance(extra_body, dict):
            extra_body = {}
        return LLMClient(LLMConfig(
            base_url=base_url,
            api_key=api_key,
            primary_model=str(section.get("primary_model", DEFAULT_QWEN3_MODEL) or DEFAULT_QWEN3_MODEL),
            fallback_models=[str(m) for m in fallback_models if str(m).strip()],
            max_tokens=int(section.get("max_tokens", 4096) or 4096),
            timeout_sec=int(section.get("timeout_sec", 90) or 90),
            max_retries=int(section.get("max_retries", 2) or 2),
            extra_body=extra_body,
            strip_thinking=bool(section.get("strip_thinking", False)),
        ))
    except Exception as exc:
        print(f"[project-chat] LLM init failed: {exc}")
        return None


def _load_root_arc_config() -> dict:
    try:
        import yaml as _yaml

        root_config_path = Path(__file__).resolve().parents[2] / "config.arc.yaml"
        if root_config_path.exists():
            with open(root_config_path, encoding="utf-8") as f:
                return _yaml.safe_load(f) or {}
    except Exception:
        return {}
    return {}


def _llm_section_for_model(config_raw: dict, model_name: str) -> dict:
    selected_model = _coerce_qwen3_model(model_name)
    if not selected_model:
        return {}

    sections: list[dict] = []
    for key in ("llm", "web_chat_llm"):
        section = config_raw.get(key, {}) if isinstance(config_raw, dict) else {}
        if isinstance(section, dict) and section:
            sections.append(section)

    fallbacks = config_raw.get("web_chat_llm_fallbacks", []) if isinstance(config_raw, dict) else []
    if isinstance(fallbacks, list):
        sections.extend(item for item in fallbacks if isinstance(item, dict) and item)

    for section in sections:
        fallback_models = section.get("fallback_models", [])
        if not isinstance(fallback_models, list):
            fallback_models = []
        known_models = [str(section.get("primary_model", "") or ""), *[str(m) for m in fallback_models]]
        if selected_model in known_models:
            matched = dict(section)
            matched["primary_model"] = selected_model
            matched["fallback_models"] = []
            return matched
    return {}


def _load_project_chat_llm_candidates(
    config_path: str,
    state: "BridgeState",
    preferred_model: str = "",
) -> list[tuple[str, object]]:
    try:
        import yaml as _yaml

        project_raw: dict = {}
        with open(config_path, encoding="utf-8") as f:
            project_raw = _yaml.safe_load(f) or {}

        root_raw = _load_root_arc_config()

        sections: list[tuple[str, dict]] = []
        project_llm = project_raw.get("llm", {}) or {}
        root_llm = root_raw.get("llm", {}) or {}
        selected_model = _coerce_qwen3_model(preferred_model) if preferred_model else ""
        if selected_model:
            matched_section = _llm_section_for_model(root_raw, selected_model)
            if isinstance(matched_section, dict) and matched_section:
                sections.append((f"selected_model:{selected_model}", matched_section))
            else:
                print(f"[project-chat] selected model has no configured endpoint, using defaults: {selected_model}")

        if isinstance(project_llm, dict) and project_llm:
            sections.append(("project.llm", project_llm))

        primary = root_raw.get("web_chat_llm", {}) or {}
        if isinstance(primary, dict) and primary:
            sections.append(("web_chat_llm", primary))

        fallbacks = root_raw.get("web_chat_llm_fallbacks", [])
        if isinstance(fallbacks, list):
            for idx, item in enumerate(fallbacks, start=1):
                if isinstance(item, dict) and item:
                    sections.append((f"web_chat_llm_fallbacks[{idx}]", item))

        clients: list[tuple[str, object]] = []
        seen: set[tuple[str, str]] = set()
        for label, section in sections:
            signature = (
                str(section.get("base_url", "") or ""),
                str(section.get("primary_model", "") or ""),
            )
            if signature in seen:
                continue
            seen.add(signature)
            client = _load_chat_llm_client_from_section(section, state)
            if client is not None:
                clients.append((label, client))
        return clients
    except Exception as exc:
        print(f"[project-chat] Candidate config load failed for {config_path}: {exc}")
        return []


async def _answer_project_chat(
    state: "BridgeState",
    project_id: str,
    question: str,
    chat_model_name: str = "",
) -> str:
    project_dir = state.projects_dir() / project_id
    if not project_dir.exists():
        return f"项目 [{project_id}] 不存在，暂时无法回答。"

    meta = _read_json(project_dir / "project_meta.json") or {}
    topic = str(meta.get("topic", "")).strip()
    config_path = str(meta.get("config_path", "")).strip()
    context = _collect_project_chat_context(project_dir, question, max_chars=20_000)
    history = _load_project_chat_history(project_dir, limit=8)
    print(f"[project-chat] context selected for {project_id}: {len(context)} chars")

    if not context:
        return (
            f"项目 [{project_id}] 目前还没有足够的检索或总结结果。"
            "建议先运行文献检索和 idea 生成，再来提问。"
        )

    llm_candidates = _load_project_chat_llm_candidates(config_path, state, chat_model_name) if config_path else []
    if not llm_candidates:
        return (
            f"这是项目 [{project_id}] 的当前上下文摘要入口，但我现在没有可用的项目模型配置来生成回答。\n\n"
            f"研究主题：{topic or '未记录'}\n"
            "已经检测到该项目存在文献/综合/idea 产物，你可以先在工作台里查看 synthesis 和 core ideas。"
        )

    dialogue_messages: list[dict[str, str]] = []
    for item in history:
        role = str(item.get("role", ""))
        if role not in ("user", "system", "assistant"):
            continue
        model_role = "assistant" if role == "system" else role
        content_text = str(item.get("content", "")).strip()
        if not content_text:
            continue
        if model_role == "assistant":
            content_text = _clean_project_chat_answer(content_text)
            if not content_text or any(marker in content_text.lower()[:500] for marker in (
                "analyze the request",
                "review the provided context",
                "determine the response",
                "project id:",
            )):
                continue
        dialogue_messages.append({"role": model_role, "content": content_text[:2000]})

    dialogue_messages.append({
        "role": "user",
        "content": f"用户问题：{question}\n请给出详细、充实的回答，包含具体的文献引用和分析。",
    })

    system = (
        "你是一个博学、有洞见的中文研究助手，回答风格接近于一位资深研究员在指导博士生。"
        "你的回答必须详尽、有深度、结构清晰。\n\n"
        "回答原则：\n"
        "1. 详细充实：每个回答至少写 5-8 段或更长的详细分析，除非用户明确要求简短。"
        "不要只给结论，要把推理过程、依据和背景都讲清楚。\n"
        "2. 引用具体文献：提到论文时要说出论文标题、作者和关键结论，"
        "不要笼统地说【有论文表明】，要具体。\n"
        "3. 结构清晰：用标题、小标题、列表组织长回答，让用户容易跟随。\n"
        "4. 给出 actionable 建议：不只是分析现状，还要给出具体的下一步做什么。\n"
        "5. 诚实面对不确定性：如果资料不足，说明缺什么、为什么缺、怎么补。\n\n"
        "禁止事项：\n"
        "- 不要输出推理过程、内部分析、草稿或英文过程标题\n"
        "- 不要复述上下文清单，但可以直接引用项目中的综述、idea 和文献\n"
        "- 不要编造不存在的论文、实验结果或引用\n"
        "- 不要过于简短——详细是首要要求"
    )
    context_user = (
        "以下是只供参考的项目资料。不要复述资料清单，不要解释资料来源。\n"
        f"<project>\nID: {project_id}\nTopic: {topic or '未记录'}\n</project>\n\n"
        f"<context>\n{context}\n</context>\n\n"
        "后续用户问题必须基于这些资料直接回答。"
    )
    errors: list[str] = []
    for label, llm in llm_candidates:
        try:
            response_max_tokens = int(getattr(getattr(llm, "config", None), "max_tokens", 8192) or 8192)
            resp = await asyncio.get_event_loop().run_in_executor(
                None,
                lambda: llm.chat(
                    [{"role": "user", "content": context_user}, *dialogue_messages],
                    system=system,
                    max_tokens=response_max_tokens,
                    temperature=0.7,
                    strip_thinking=True,
                ),
            )
            content = _clean_project_chat_answer(resp.content)
            if content:
                return content
            errors.append(f"{label}: 空回复")
        except Exception as exc:
            errors.append(f"{label}: {exc}")

    return "项目问答这次调用失败了：" + " | ".join(errors[:3])


# ── File monitoring ─────────────────────────────────────────────────────────

def _sync_completed_stages(
    agent: LobsterAgent, run_dir: Path, layer_range: tuple[int, int], done_up_to: int,
) -> list[dict]:
    """Mark all stages from layer_range[0] to done_up_to as completed, emit events for new ones."""
    messages: list[dict] = []
    for s in range(layer_range[0], min(done_up_to, layer_range[1]) + 1):
        if agent.stage_progress.get(s) == "completed":
            continue
        if s not in STAGE_TO_LAYER:
            continue
        agent.stage_progress[s] = "completed"
        messages.append(msg_stage_update(agent.id, s, "completed"))
        messages.append(msg_log(agent, f"{STAGE_NAMES.get(s, f'S{s}')} 完成", "success", s))
        stage_dir = run_dir / f"stage-{s:02d}"
        if stage_dir.is_dir():
            for expected in STAGE_OUTPUTS.get(s, []):
                artifact_path = stage_dir / expected.rstrip("/")
                key = f"{s}:{expected}"
                if key not in agent._known_artifacts and artifact_path.exists():
                    agent._known_artifacts.add(key)
                    if expected not in DISPLAY_ARTIFACTS:
                        continue
                    size = "dir" if artifact_path.is_dir() else f"{artifact_path.stat().st_size / 1024:.1f} KB"
                    content = _extract_artifact_summary(artifact_path, expected)
                    messages.append(msg_artifact(
                        REPO_FOR_STAGE.get(s, "knowledge"), expected, agent.name, size, agent.project_id, content, stage=s,
                    ))
    return messages


def poll_agent(agent: LobsterAgent) -> list[dict]:
    messages: list[dict] = []
    run_dir = Path(agent.run_dir)
    if not run_dir.exists():
        return messages

    # Only read heartbeat/checkpoint if THIS agent's process is running,
    # to avoid cross-contamination when multiple agents share a run_dir.
    if agent.process is not None and agent.process.poll() is None:
        _s7_only = getattr(agent, '_is_idea_factory_s7_only', False)
        layer_range = (7, 7) if _s7_only else LAYER_RANGE.get(agent.layer, (1, 15))

        hb = _read_json(run_dir / "heartbeat.json")
        if hb and hb != agent._prev_heartbeat:
            new_stage = hb.get("last_stage")
            old_stage = agent.current_stage
            if (
                new_stage and new_stage != old_stage
                and new_stage in STAGE_TO_LAYER
                and layer_range[0] <= new_stage <= layer_range[1]
            ):
                agent.current_stage = new_stage
                agent.current_task = f"Stage {new_stage}: {STAGE_NAMES.get(new_stage, '?')}"
                agent.status = "working"
                if new_stage not in agent.stage_progress or agent.stage_progress[new_stage] != "completed":
                    agent.stage_progress[new_stage] = "running"
                messages.append(msg_agent_update(agent))
                messages.append(msg_stage_update(agent.id, new_stage, "running"))
                messages.append(msg_log(agent, f"开始 {STAGE_NAMES.get(new_stage, f'S{new_stage}')}", "info", new_stage))
            agent._prev_heartbeat = hb

        cp = _read_json(run_dir / "checkpoint.json")
        if cp and cp != agent._prev_checkpoint:
            done_up_to = cp.get("last_completed_stage", 0)
            messages.extend(_sync_completed_stages(agent, run_dir, layer_range, done_up_to))
            agent._prev_checkpoint = cp

            if agent.current_stage and done_up_to >= agent.current_stage and done_up_to < layer_range[1]:
                next_stage = done_up_to + 1
                if next_stage in STAGE_TO_LAYER and layer_range[0] <= next_stage <= layer_range[1]:
                    agent.current_stage = next_stage
                    agent.current_task = f"Stage {next_stage}: {STAGE_NAMES.get(next_stage, '?')}"
                    agent.stage_progress[next_stage] = "running"
                    messages.append(msg_agent_update(agent))
                    messages.append(msg_stage_update(agent.id, next_stage, "running"))
                    messages.append(msg_log(agent, f"开始 {STAGE_NAMES.get(next_stage, f'S{next_stage}')}", "info", next_stage))

    if agent.process is not None:
        retcode = agent.process.poll()
        if retcode is not None:
            # Final read: catch any checkpoint/artifact updates written before exit
            _s7_only_final = getattr(agent, '_is_idea_factory_s7_only', False)
            layer_range = (7, 7) if _s7_only_final else LAYER_RANGE.get(agent.layer, (1, 15))
            cp = _read_json(run_dir / "checkpoint.json")
            if cp:
                done_up_to = cp.get("last_completed_stage", 0)
                messages.extend(_sync_completed_stages(agent, run_dir, layer_range, done_up_to))
                agent._prev_checkpoint = cp

            expected_to = int(
                getattr(agent, "_expected_to_stage", layer_range[1]) or layer_range[1]
            )
            checkpoint_done = int((cp or {}).get("last_completed_stage", 0) or 0)
            if retcode == 0 and checkpoint_done < expected_to:
                # A subprocess can exit cleanly after a swallowed LLM/network
                # failure.  Exit code 0 is not completion unless the durable
                # checkpoint reached the requested target stage.
                retcode = 70
                agent._last_exit_reason = (  # type: ignore[attr-defined]
                    f"进程退出但断点仅到 S{checkpoint_done}，目标为 S{expected_to}"
                )

            if retcode == 0:
                agent.status = "done"
                agent.current_task = ""
                agent.current_stage = None
                messages.append(msg_agent_update(agent))
                messages.append(msg_log(agent, f"层任务完成 (project={agent.project_id})", "success"))
            else:
                agent.status = "error"
                reason = getattr(agent, "_last_exit_reason", f"exit code={retcode}")
                agent.current_task = reason
                messages.append(msg_agent_update(agent))
                messages.append(msg_log(agent, f"进程异常: {reason}", "error"))
            agent.process = None

    return messages


# ── Agent lifecycle ─────────────────────────────────────────────────────────

def create_agent(state: BridgeState, name: str, layer: str) -> LobsterAgent:
    agent = LobsterAgent(
        id=f"L-{_uid()}", name=name, layer=layer,
        run_id="", run_dir="", config_path="",
        stage_progress={s: "pending" for s in LAYER_STAGES.get(layer, [])},
    )
    state.agents[agent.id] = agent
    return agent


def _assign_task_to_agent(agent: LobsterAgent, task: Task) -> None:
    """Common setup when assigning a task to an agent."""
    if not hasattr(agent, '_base_name'):
        agent._base_name = agent.name  # type: ignore[attr-defined]

    agent.project_id = task.project_id
    agent.run_dir = task.run_dir
    agent.run_id = task.project_id
    agent.config_path = task.config_path
    agent.assigned_task_id = task.id
    agent._topic = task.topic
    agent.status = "working"
    layer_stages = LAYER_STAGES.get(agent.layer, [])
    agent.stage_progress = {s: "pending" for s in layer_stages}
    agent.current_stage = layer_stages[0] if layer_stages else 0
    agent.current_task = f"准备执行 [{task.project_id}]"
    agent._prev_heartbeat = {}
    agent._prev_checkpoint = {}
    agent._known_artifacts = set()

    # Lab mode: extract role tag from topic pattern "[RoleName] topic"
    import re as _re
    _role_match = _re.match(r"^\[(.+?)\]\s", task.topic or "")
    agent.role_tag = _role_match.group(1) if _role_match else ""


def _passthrough_agent(agent: LobsterAgent) -> list[dict]:
    """For passthrough layers (e.g. coding): read existing artifacts, mark done immediately."""
    messages: list[dict] = []
    run_dir = Path(agent.run_dir)
    layer_range = LAYER_RANGE.get(agent.layer, (1, 15))

    for s in range(layer_range[0], layer_range[1] + 1):
        stage_dir = run_dir / f"stage-{s:02d}"
        if stage_dir.is_dir():
            agent.stage_progress[s] = "completed"
            messages.append(msg_stage_update(agent.id, s, "completed"))
            messages.append(msg_log(agent, f"{STAGE_NAMES.get(s, f'S{s}')} 结果已就绪 (由上层产出)", "success", s))
            for expected in STAGE_OUTPUTS.get(s, []):
                artifact_path = stage_dir / expected.rstrip("/")
                key = f"{s}:{expected}"
                if key not in agent._known_artifacts and artifact_path.exists():
                    agent._known_artifacts.add(key)
                    if expected not in DISPLAY_ARTIFACTS:
                        continue
                    size = "dir" if artifact_path.is_dir() else f"{artifact_path.stat().st_size / 1024:.1f} KB"
                    content = _extract_artifact_summary(artifact_path, expected)
                    messages.append(msg_artifact(
                        REPO_FOR_STAGE.get(s, "codebase"), expected, agent.name, size, agent.project_id, content, stage=s,
                    ))
        else:
            agent.stage_progress[s] = "failed"
            messages.append(msg_log(agent, f"S{s} 产物未找到 (stage-{s:02d}/ 不存在)", "warning", s))

    agent.status = "done"
    agent.current_task = ""
    agent.current_stage = None
    messages.append(msg_agent_update(agent))
    messages.append(msg_log(agent, f"验收完成 (project={agent.project_id})", "success"))
    return messages


def _ensure_full_chain_ieee_config(config_path: str) -> bool:
    """Migrate new or resumed full-chain projects to the IEEE export contract."""
    path = Path(config_path)
    if not path.is_file():
        return False
    try:
        import yaml

        raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        export = raw.setdefault("export", {})
        research = raw.setdefault("research", {})
        topic = str(research.get("topic", "") or "")
        changed = export.get("target_conference") != "ieee"
        export["target_conference"] = "ieee"
        for key, value in (("target_pages", 8), ("min_pages", 7), ("max_pages", 8)):
            if export.get(key) != value:
                export[key] = value
                changed = True
        if IEEE_FULL_CHAIN_REQUIREMENT not in topic:
            research["topic"] = f"{topic}\n\n{IEEE_FULL_CHAIN_REQUIREMENT}".strip()
            changed = True
        if changed:
            path.write_text(yaml.safe_dump(raw, sort_keys=False, allow_unicode=True), encoding="utf-8")
        return changed
    except Exception:
        return False


def launch_agent_for_task(state: BridgeState, agent: LobsterAgent, task: Task) -> list[dict]:
    """Assign a task to an agent. Passthrough layers skip process launch."""
    messages: list[dict] = []
    _assign_task_to_agent(agent, task)

    # Passthrough layers: just verify artifacts and mark done
    if agent.layer in PASSTHROUGH_LAYERS:
        messages.append(msg_agent_update(agent))
        messages.append(msg_log(agent, f"领取任务 [{task.project_id}] 验收 S10 代码产物", "info"))
        messages.extend(_passthrough_agent(agent))
        return messages

    # Normal layers: launch ResearchClaw process
    # Discussion mode: L1 runs S1-S7 only (S8 runs after discussion)
    # Reproduce mode skips discussion → runs full S1-S8
    _task_meta = _read_project_meta(task.run_dir) if task.run_dir else None
    if not _task_meta and task.project_id:
        _task_meta = _read_project_meta(str(state.projects_dir() / task.project_id))
    _is_reproduce = _task_meta.get("mode") == "reproduce" if _task_meta else False
    _is_literature_watch = _task_meta.get("run_mode") == "literature_watch" if _task_meta else False
    if isinstance(_task_meta, dict) and _task_meta.get("run_mode", "full_chain") == "full_chain":
        _ensure_full_chain_ieee_config(task.config_path)
    if _is_literature_watch and agent.layer == "idea":
        layer_range = LAYER_RANGE_PHASE1["idea"]
    elif state.discussion_mode and agent.layer == "idea" and not _is_reproduce:
        layer_range = LAYER_RANGE_PHASE1["idea"]
    else:
        layer_range = LAYER_RANGE.get(agent.layer, (1, 15))
    fs, ts = layer_range

    # Checkpoint-aware resume: skip already-completed stages within this layer
    cp = _read_json(Path(task.run_dir) / "checkpoint.json")
    if cp:
        last_done = cp.get("last_completed_stage", 0)
        resume_stage = last_done + 1

        # Discussion mode: if S1-S7 already done, skip straight to discussion/S8
        if state.discussion_mode and agent.layer == "idea" and not _is_reproduce and not _is_literature_watch and last_done >= ts:
            for s in range(fs, ts + 1):
                agent.stage_progress[s] = "completed"
            messages.append(msg_log(
                agent,
                f"S1-S7 已完成 (checkpoint={last_done}), 跳过重跑 → 直接进入讨论/S8",
                "info",
            ))
            messages.extend(_skip_discussion_proceed_s8(state, agent))
            return messages

        if fs <= resume_stage <= ts:
            for s in range(fs, resume_stage):
                if s in STAGE_TO_LAYER:
                    agent.stage_progress[s] = "completed"
            fs = resume_stage
            agent.current_stage = fs
            agent.current_task = f"断点恢复 → {STAGE_NAMES.get(fs, f'S{fs}')}"
            messages.append(msg_agent_update(agent))
            messages.append(msg_log(
                agent,
                f"断点恢复: 跳过已完成阶段, 从 {STAGE_NAMES.get(fs, f'S{fs}')} 开始",
                "info",
            ))

    assigned_gpus: list[int] | None = None
    if agent.layer == "execution":
        assigned_gpus = state.gpu_allocator.allocate(task.project_id)
        if assigned_gpus is None:
            agent.status = "error"
            agent.current_task = "GPU 资源不足，任务保留等待"
            task.status = "pending"
            task.assigned_to = None
            task.waiting_reason = "waiting_for_gpu"
            task.waiting_since = task.waiting_since or _now_ms()
            messages.append(msg_agent_update(agent))
            messages.append(msg_log(agent, "GPU 资源不足，执行任务等待可用配额", "warning"))
            return messages

    cmd = [
        state.python_path, "-m", "researchclaw", "run",
        "--config", task.config_path,
        "--output", task.run_dir,
        "--from-stage", STAGE_NAMES.get(fs, str(fs)),
        "--to-stage", STAGE_NAMES.get(ts, str(ts)),
        "--auto-approve",
    ]
    if task.topic:
        cmd.extend(["--topic", task.topic])

    try:
        proc_env = {**os.environ, "PYTHONUNBUFFERED": "1"}
        if assigned_gpus:
            proc_env["CUDA_VISIBLE_DEVICES"] = ",".join(str(gpu) for gpu in assigned_gpus)
            messages.append(msg_log(agent, f"已分配 GPU {assigned_gpus}", "info"))
        else:
            proc_env.pop("CUDA_VISIBLE_DEVICES", None)

        log_path = Path(task.run_dir) / f"agent_{agent.id}.log"
        log_file = open(log_path, "w", encoding="utf-8")
        proc = subprocess.Popen(
            cmd, cwd=state.agent_package_dir,
            stdout=log_file, stderr=subprocess.STDOUT,
            env=proc_env,
        )
        agent.process = proc
        agent._expected_from_stage = fs  # type: ignore[attr-defined]
        agent._expected_to_stage = ts  # type: ignore[attr-defined]
        agent.current_task = f"项目 {task.project_id} · PID={proc.pid}"
        messages.append(msg_agent_update(agent))
        messages.append(msg_log(agent, f"领取任务 [{task.project_id}] 启动 S{fs}→S{ts} (PID={proc.pid})", "info"))
    except Exception as e:
        if assigned_gpus:
            state.gpu_allocator.release(task.project_id)
        agent.status = "error"
        agent.current_task = f"启动失败: {e}"
        messages.append(msg_agent_update(agent))
        messages.append(msg_log(agent, f"启动失败: {e}", "error"))

    return messages


def stop_agent(agent: LobsterAgent) -> list[dict]:
    messages: list[dict] = []
    if agent.process and agent.process.poll() is None:
        agent.process.terminate()
        try:
            agent.process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            agent.process.kill()
    agent.process = None
    agent.status = "idle"
    agent.current_task = ""
    agent.current_stage = None
    agent.assigned_task_id = None
    messages.append(msg_agent_update(agent))
    messages.append(msg_log(agent, "Agent 已停止", "warning"))
    return messages


# ── Task queue operations ───────────────────────────────────────────────────

def _save_project_meta(
    run_dir: str,
    project_id: str,
    config_path: str,
    topic: str,
    mode: str = "lab",
    user_id: str = "",
    run_mode: str = "full_chain",
) -> None:
    """Persist project metadata so it can be recovered on restart."""
    meta = {
        "project_id": project_id,
        "config_path": config_path,
        "topic": topic,
        "mode": mode,
        "run_mode": run_mode,
        "user_id": user_id,
        "created_at": _now_ms(),
    }
    meta_path = Path(run_dir) / "project_meta.json"
    if not meta_path.exists():
        _write_json(meta_path, meta)


def _read_project_meta(run_dir: str) -> dict | None:
    return _read_json(Path(run_dir) / "project_meta.json")


def _completed_terminal_stage(run_dir: str) -> bool:
    cp = _read_json(Path(run_dir) / "checkpoint.json")
    return bool(cp and cp.get("last_completed_stage", 0) >= PROJECT_TERMINAL_STAGE)


def _determine_resume_target(run_dir: str) -> tuple[str, int] | None:
    """Read checkpoint and return (target_layer, next_stage) or None if no checkpoint."""
    cp = _read_json(Path(run_dir) / "checkpoint.json")
    if not cp:
        return None
    last_done = cp.get("last_completed_stage", 0)
    if last_done <= 0:
        return None
    if last_done >= PROJECT_TERMINAL_STAGE:
        return None
    next_stage = last_done + 1
    if next_stage > PROJECT_TERMINAL_STAGE:
        return None
    target_layer = STAGE_TO_LAYER.get(next_stage)
    if not target_layer:
        return None
    return (target_layer, next_stage)


def _queue_for_layer(target_layer: str) -> str:
    """Return the input queue name that feeds into target_layer."""
    if target_layer == "idea":
        return "init_to_idea"
    return LAYER_INPUT_QUEUE.get(target_layer, "init_to_idea")


def _enqueue_checkpoint_retry(
    state: BridgeState, agent: LobsterAgent, attempt: int,
) -> list[dict]:
    """Durably requeue an interrupted project from its last checkpoint."""
    if not agent.project_id or not agent.run_dir or not agent.config_path:
        return []
    if _completed_terminal_stage(agent.run_dir):
        return []

    # Avoid duplicate recovery tasks when two observers notice the same exit.
    for queue in state.queues.values():
        if any(
            task.project_id == agent.project_id
            and task.status in ("pending", "assigned")
            for task in queue.tasks
        ):
            return []

    resume = _determine_resume_target(agent.run_dir)
    target_layer = resume[0] if resume else agent.layer
    queue_name = _queue_for_layer(target_layer)
    source_layer = {
        "idea": "init", "experiment": "idea", "coding": "experiment",
        "execution": "coding", "writing": "execution",
    }.get(target_layer, "init")
    retry = Task(
        id=f"task-{_uid()}",
        project_id=agent.project_id,
        run_dir=agent.run_dir,
        config_path=agent.config_path,
        topic=getattr(agent, "_topic", ""),
        source_layer=source_layer,
        target_layer=target_layer,
        created_at=_now_ms(),
    )
    state.queues[queue_name].push(retry)
    cp = _read_json(Path(agent.run_dir) / "checkpoint.json") or {}
    last_done = int(cp.get("last_completed_stage", 0) or 0)
    next_stage = last_done + 1 if last_done else LAYER_RANGE.get(target_layer, (1, 1))[0]
    return [msg_log(
        agent,
        f"检测到中断，已自动重试 {attempt}/3：从 S{next_stage} 断点恢复",
        "warning",
        next_stage,
    )]


def _recover_orphaned_inflight_projects(state: BridgeState) -> int:
    """Requeue runs whose heartbeat advanced past their durable checkpoint.

    This covers bridge restarts and externally killed subprocesses, including
    the S7-discussion-S8 handoff where the original queue task is already
    marked completed.
    """
    active_projects = {
        task.project_id
        for queue in state.queues.values()
        for task in queue.tasks
        if task.status in ("pending", "assigned")
    }
    recovered = 0
    for project_dir in state.projects_dir().iterdir():
        if not project_dir.is_dir() or project_dir.name.startswith("_"):
            continue
        candidates = [project_dir]
        candidates.extend(
            child for child in project_dir.iterdir()
            if child.is_dir() and child.name.startswith("run-")
        )
        project_meta = _read_project_meta(str(project_dir)) or {}
        for run_dir in candidates:
            heartbeat = _read_json(run_dir / "heartbeat.json") or {}
            checkpoint = _read_json(run_dir / "checkpoint.json") or {}
            last_stage = int(heartbeat.get("last_stage", 0) or 0)
            last_done = int(checkpoint.get("last_completed_stage", 0) or 0)
            try:
                heartbeat_age = time.time() - (run_dir / "heartbeat.json").stat().st_mtime
            except OSError:
                heartbeat_age = float("inf")
            recent_s7_handoff = (
                last_stage == 7 and last_done == 7 and heartbeat_age <= 86400
            )
            if (
                not heartbeat
                or (last_stage <= last_done and not recent_s7_handoff)
                or last_done >= PROJECT_TERMINAL_STAGE
            ):
                continue
            if _run_dir_has_live_heartbeat(run_dir):
                continue
            meta = _read_project_meta(str(run_dir)) or project_meta
            if not isinstance(meta, dict) or meta.get("run_mode", "full_chain") != "full_chain":
                continue
            if meta.get("intervention"):
                continue
            project_id = str(meta.get("project_id") or project_dir.name)
            if project_id in active_projects:
                continue
            config_path = str(meta.get("config_path", "") or "")
            if not config_path:
                continue
            resume = _determine_resume_target(str(run_dir))
            if not resume:
                continue
            target_layer, _ = resume
            queue_name = _queue_for_layer(target_layer)
            source_layer = {
                "idea": "init", "experiment": "idea", "coding": "experiment",
                "execution": "coding", "writing": "execution",
            }.get(target_layer, "init")
            state.queues[queue_name].push(Task(
                id=f"task-{_uid()}",
                project_id=project_id,
                run_dir=str(run_dir),
                config_path=config_path,
                topic=str(meta.get("topic", "") or ""),
                source_layer=source_layer,
                target_layer=target_layer,
                created_at=_now_ms(),
            ))
            active_projects.add(project_id)
            (run_dir / "heartbeat.json").unlink(missing_ok=True)
            recovered += 1
    return recovered


def _adopt_live_subprocesses(state: BridgeState) -> int:
    """Attach surviving ResearchClaw PIDs to fresh in-memory agents."""
    adopted = 0
    seen_pids: set[int] = set()
    for project_dir in state.projects_dir().iterdir():
        if not project_dir.is_dir() or project_dir.name.startswith("_"):
            continue
        candidates = [project_dir]
        candidates.extend(
            child for child in project_dir.iterdir()
            if child.is_dir() and child.name.startswith("run-")
        )
        project_meta = _read_project_meta(str(project_dir)) or {}
        for run_dir in candidates:
            heartbeat = _read_json(run_dir / "heartbeat.json") or {}
            pid = int(heartbeat.get("pid", 0) or 0)
            stage = int(heartbeat.get("last_stage", 0) or 0)
            if not pid or pid in seen_pids or not _run_dir_has_live_heartbeat(run_dir):
                continue
            layer = STAGE_TO_LAYER.get(stage)
            if not layer:
                continue
            agent = next(
                (item for item in state.agents.values()
                 if item.layer == layer and item.status == "idle"),
                None,
            )
            if agent is None:
                continue
            meta = _read_project_meta(str(run_dir)) or project_meta
            project_id = str((meta or {}).get("project_id") or project_dir.name)
            config_path = str((meta or {}).get("config_path", "") or "")
            if not config_path:
                continue

            agent.project_id = project_id
            agent.run_dir = str(run_dir)
            agent.run_id = str(heartbeat.get("run_id", "") or project_id)
            agent.config_path = config_path
            agent.status = "working"
            agent.current_stage = stage
            agent.current_task = f"重启后接管 S{stage} · PID={pid}"
            agent.stage_progress = {
                number: ("completed" if number < stage else "running" if number == stage else "pending")
                for number in LAYER_STAGES.get(layer, [])
            }
            agent.process = AttachedPidProcess(pid, run_dir)
            agent._topic = str((meta or {}).get("topic", "") or "")  # type: ignore[attr-defined]
            agent._expected_from_stage = stage  # type: ignore[attr-defined]
            agent._expected_to_stage = (
                8 if layer == "idea" and stage == 8 else LAYER_RANGE.get(layer, (stage, stage))[1]
            )  # type: ignore[attr-defined]
            if layer == "idea" and stage == 8:
                agent._is_discussion_s8 = True  # type: ignore[attr-defined]

            for queue in state.queues.values():
                for task in queue.tasks:
                    if task.project_id == project_id and task.status == "assigned":
                        agent.assigned_task_id = task.id
                        task.assigned_to = agent.id
                        queue.save()
                        break
            seen_pids.add(pid)
            adopted += 1
    return adopted


def submit_new_project(
    state: BridgeState,
    project_id: str,
    config_path: str,
    topic: str = "",
    mode: str = "lab",
    user_id: str = "",
    run_mode: str = "full_chain",
) -> list[dict]:
    """Submit a project — auto-detects checkpoint and resumes from where it left off.

    In cross-project discussion mode, each project gets ONE agent.
    After S7, agents from different projects discuss with each other.
    """
    messages: list[dict] = []
    sys_agent = LobsterAgent(id="system", name="系统", layer="idea", run_id="", run_dir="", config_path="")

    run_dir = str(state.projects_dir() / project_id)
    os.makedirs(run_dir, exist_ok=True)
    _save_project_meta(run_dir, project_id, config_path, topic, mode=mode, user_id=user_id, run_mode=run_mode)

    if state.discussion_mode:
        messages.append(msg_log(
            sys_agent,
            f"新项目 [{project_id}] 跨 project 讨论模式: 分配 1 个 agent, S7 后与其他 project agent 讨论",
            "info", DISCUSSION_STAGE,
        ))

    if _completed_terminal_stage(run_dir):
        messages.append(msg_log(
            sys_agent,
            f"项目 [{project_id}] 已完成到 S{PROJECT_TERMINAL_STAGE}，按当前设置不再继续后续阶段",
            "success",
        ))
        messages.append(msg_queue_update(state.queues))
        return messages

    # Check for checkpoint to enable resume
    resume_info = _determine_resume_target(run_dir)
    if resume_info:
        target_layer, next_stage = resume_info
        queue_name = _queue_for_layer(target_layer)
        source_layer = {
            "idea": "init", "experiment": "idea", "coding": "experiment",
            "execution": "coding", "writing": "execution",
        }.get(target_layer, "init")

        task = Task(
            id=f"task-{_uid()}", project_id=project_id, run_dir=run_dir,
            config_path=config_path, topic=topic,
            source_layer=source_layer, target_layer=target_layer,
            created_at=_now_ms(),
        )
        state.queues[queue_name].push(task)
        stage_name = STAGE_NAMES.get(next_stage, f"S{next_stage}")
        messages.append(msg_log(
            sys_agent,
            f"项目 [{project_id}] 检测到断点 → 从 {stage_name} (Stage {next_stage}) 恢复",
            "success",
        ))
    else:
        task = Task(
            id=f"task-{_uid()}", project_id=project_id, run_dir=run_dir,
            config_path=config_path, topic=topic,
            source_layer="init", target_layer="idea",
            created_at=_now_ms(),
        )
        state.queues["init_to_idea"].push(task)
        messages.append(msg_log(sys_agent, f"新项目 [{project_id}] 已加入调研队列", "info"))

    messages.append(msg_queue_update(state.queues))
    return messages


def _check_s12_sanity_failure(state: "BridgeState", agent: "LobsterAgent") -> list[dict]:
    """If S12 sanity_report.json shows fail, pause the project and return notification messages."""
    messages: list[dict] = []
    _s12_dir = Path(agent.run_dir) / "stage-12"
    _sanity_path = _s12_dir / "sanity_report.json"
    if not _sanity_path.exists():
        return messages
    try:
        _sanity = json.loads(_sanity_path.read_text(encoding="utf-8"))
    except Exception:
        return messages
    if _sanity.get("status") != "fail":
        return messages

    _fix_log_path = _s12_dir / "fix_log.json"
    _exp_dir = None
    for _sd in sorted(Path(agent.run_dir).glob("stage-11*"), reverse=True):
        _ed = _sd / "experiment"
        if _ed.exists():
            _exp_dir = str(_ed)
            break

    _last_error = ""
    _iters = _sanity.get("iterations", [])
    if _iters:
        _last_iter = _iters[-1]
        _failed_checks = [c for c in _last_iter.get("checks", []) if not c.get("passed")]
        if _failed_checks:
            _fc = _failed_checks[-1]
            _last_error = (_fc.get("stderr_tail") or _fc.get("stderr") or "")[-800:]

    _detail = (
        f"⚠️ S12 SANITY_CHECK 循环修复失败，需要手动介入\n"
        f"项目: {agent.project_id}\n"
        f"修复轮次: {_sanity.get('total_iterations', '?')}/{_sanity.get('max_fix_iterations', '?')}\n"
        f"实验代码: {_exp_dir or 'N/A'}\n"
        f"修复日志: {_fix_log_path}\n"
        f"检查报告: {_sanity_path}"
    )
    if _last_error:
        _detail += f"\n最后报错:\n{_last_error}"

    # Persist intervention reason so the frontend can display it
    _meta_path = Path(agent.run_dir) / "project_meta.json"
    if _meta_path.exists():
        try:
            _meta = json.loads(_meta_path.read_text(encoding="utf-8"))
            _meta["intervention"] = _detail
            _meta_path.write_text(json.dumps(_meta, indent=2, ensure_ascii=False), encoding="utf-8")
        except Exception:
            pass

    messages.append(msg_log(agent, _detail, "error", 12))
    messages.extend(_pause_project(state, agent.project_id))
    messages.append(msg_project_list(list_all_projects(state)))
    return messages


def on_agent_done(state: BridgeState, agent: LobsterAgent) -> list[dict]:
    """When an agent finishes, complete its task and create a follow-up task for the next layer."""
    messages: list[dict] = []

    state._fail_counts.pop(agent.project_id, None)  # reset fail counter on success

    # Complete assigned task
    if agent.assigned_task_id:
        for q in state.queues.values():
            q.complete(agent.assigned_task_id)

    # S26 is the actual terminal stage.  Stop here so a completed writing
    # task cannot enqueue the project back into the idea feedback queue.
    if agent.run_dir and _completed_terminal_stage(agent.run_dir):
        messages.append(msg_log(
            agent,
            f"S{PROJECT_TERMINAL_STAGE} 已完成，最终论文、代码、图表和引用校验均已交付",
            "success",
            PROJECT_TERMINAL_STAGE,
        ))
        _reset_agent_idle(agent)
        messages.append(msg_agent_update(agent))
        messages.append(msg_queue_update(state.queues))
        messages.append(msg_project_list(list_all_projects(state)))
        return messages

    # Scheduled literature watchers intentionally terminate at S7. Their
    # deliverable is the screened evidence set plus Chinese synthesis, not an
    # idea/experiment/writing project.
    _watch_meta = _read_project_meta(agent.run_dir) if agent.run_dir else None
    _watch_cp = _read_json(Path(agent.run_dir) / "checkpoint.json") if agent.run_dir else None
    if (
        agent.layer == "idea"
        and isinstance(_watch_meta, dict)
        and _watch_meta.get("run_mode") == "literature_watch"
        and int((_watch_cp or {}).get("last_completed_stage", 0) or 0) >= 7
    ):
        messages.append(msg_log(agent, "定期文献抓取与中文综述已完成", "success", 7))
        _reset_agent_idle(agent)
        messages.append(msg_agent_update(agent))
        messages.append(msg_queue_update(state.queues))
        messages.append(msg_project_list(list_all_projects(state)))
        return messages

    # Discussion mode: L1 agent completed S7 → enter discussion
    # Reproduce mode skips discussion entirely
    _agent_proj_dir = state.projects_dir() / agent.project_id if agent.project_id else None
    _agent_meta = _read_project_meta(str(_agent_proj_dir)) if _agent_proj_dir and _agent_proj_dir.exists() else None
    _agent_is_reproduce = _agent_meta.get("mode") == "reproduce" if _agent_meta else False
    if state.discussion_mode and agent.layer == "idea" and not _agent_is_reproduce:
        state.discussion_waiting[agent.id] = agent
        agent.current_stage = DISCUSSION_STAGE
        agent.stage_progress[DISCUSSION_STAGE] = "running"
        agent.status = "waiting_discussion"
        agent.current_task = "S7 完成，等待讨论伙伴..."
        messages.append(msg_agent_update(agent))

        pid = agent.project_id
        expected_count = state.lab_batches.get(pid, 0)

        if expected_count >= 2:
            # Lab mode: same project_id, wait for all N agents to finish S7
            waiting_same_proj = [
                a for a in state.discussion_waiting.values()
                if a.project_id == pid
            ]
            if len(waiting_same_proj) >= expected_count:
                messages.append(msg_log(
                    agent,
                    f"项目 [{pid}] 全部 {len(waiting_same_proj)} 个方向 S7 完成 → 启动跨领域讨论",
                    "info", DISCUSSION_STAGE,
                ))
                group = DiscussionGroup(
                    project_id=pid,
                    topic=getattr(agent, '_topic', '') or agent.current_task,
                    config_path=agent.config_path,
                    agent_ids=[a.id for a in waiting_same_proj],
                    run_dirs={a.id: a.run_dir for a in waiting_same_proj},
                )
                for a in waiting_same_proj:
                    state.discussion_waiting.pop(a.id, None)
                    group.completed_s7.add(a.id)
                state.discussion_groups[pid] = group
                messages.extend(_trigger_discussion(state, group))
            else:
                messages.append(msg_log(
                    agent,
                    f"S7 完成，等待同项目其他方向 ({len(waiting_same_proj)}/{expected_count})",
                    "info", DISCUSSION_STAGE,
                ))
            return messages

        # Non-Lab: cross-project pair discussion (original logic)
        # 1) Find another agent that also completed S7 (different project)
        peers = [a for a in state.discussion_waiting.values()
                 if a.id != agent.id and a.project_id != agent.project_id]

        if peers:
            peer = peers[0]
            messages.append(msg_log(agent, f"S7 完成，与 [{peer.project_id}] 的 agent 开始跨 project 讨论", "info", DISCUSSION_STAGE))
            messages.extend(_trigger_cross_project_discussion(state, agent, peer))
            return messages

        # 2) Find an idle agent (no project, can act as reviewer/critic)
        idle_agents = [
            a for a in state.agents.values()
            if a.layer == "idea" and a.status == "idle"
            and a.id != agent.id and not a.assigned_task_id
        ]
        if idle_agents:
            reviewer = idle_agents[0]
            reviewer.status = "discussing"
            reviewer.current_stage = DISCUSSION_STAGE
            reviewer.current_task = f"讨论评审: [{agent.project_id}]"
            reviewer.stage_progress[DISCUSSION_STAGE] = "running"
            messages.append(msg_agent_update(reviewer))
            messages.append(msg_log(agent, f"S7 完成，与空闲 agent [{reviewer.name}] 开始讨论评审", "info", DISCUSSION_STAGE))
            messages.extend(_trigger_cross_project_discussion(state, agent, reviewer))
            return messages

        # 3) No peer available at all — skip discussion
        messages.append(msg_log(agent, "S7 完成，无可用讨论伙伴，跳过讨论直接进入 S8", "info", DISCUSSION_STAGE))
        messages.extend(_skip_discussion_proceed_s8(state, agent))
        return messages

    # Idea-gated mode: after S8, stop for the frontend to review ideas
    # before dispatching L2 experiment design.
    if agent.layer == "idea" and agent.project_id and agent.run_dir:
        cp = _read_json(Path(agent.run_dir) / "checkpoint.json")
        if cp and int(cp.get("last_completed_stage", 0) or 0) >= 8:
            proj_dir = state.projects_dir() / agent.project_id
            meta = _read_project_meta(str(proj_dir)) if proj_dir.exists() else {}
            if isinstance(meta, dict) and meta.get("run_mode") == "idea_gate" and not meta.get("ideas_confirmed"):
                meta["intervention"] = "idea_review:S8 核心想法已生成，请在右侧查看并确认后进入实验设计"
                meta["selected_idea_run_dir"] = agent.run_dir
                meta["selected_idea_config_path"] = agent.config_path
                meta["selected_idea_agent"] = agent.name
                _write_json(proj_dir / "project_meta.json", meta)
                messages.append(msg_log(
                    agent,
                    "S8 Idea 已生成，已暂停等待确认进入 L2 实验设计",
                    "info",
                    8,
                ))
                _reset_agent_idle(agent)
                messages.append(msg_agent_update(agent))
                messages.append(msg_queue_update(state.queues))
                messages.append(msg_project_list(list_all_projects(state)))
                return messages

    # L3 (coding) → check S12 sanity_report: if failed, pause and notify user
    if agent.layer == "coding" and agent.project_id:
        _s12_msgs = _check_s12_sanity_failure(state, agent)
        if _s12_msgs:
            messages.extend(_s12_msgs)
            return messages

    # Create follow-up task in the next queue
    output_queue_name = LAYER_OUTPUT_QUEUE.get(agent.layer)
    if output_queue_name and output_queue_name in state.queues and agent.project_id:
        # L4→L5: only push if S17 PROCEED
        if agent.layer == "execution" and output_queue_name == "execution_to_writing":
            _decision_file = Path(agent.run_dir) / "stage-17" / "decision.md"
            _warning_file = Path(agent.run_dir) / "quality_warning.txt"
            _summary_file = Path(agent.run_dir) / "pipeline_summary.json"
            _is_proceed = False
            if _decision_file.exists():
                _dec_text = _decision_file.read_text(encoding="utf-8").upper()
                _is_proceed = "PROCEED" in _dec_text and "REFINE" not in _dec_text.split("PROCEED")[0][-50:]
            if not _is_proceed and _warning_file.exists():
                _warn_text = _warning_file.read_text(encoding="utf-8")
                if "max pivots" in _warn_text.lower():
                    _is_proceed = True
                    messages.append(msg_log(agent, "S17 决策为 REFINE 但已达最大迭代次数，强制进入论文写作", "warning"))
            if not _is_proceed and _summary_file.exists():
                try:
                    import json as _json
                    _summary = _json.loads(_summary_file.read_text(encoding="utf-8"))
                    if _summary.get("final_status") == "done" and _summary.get("stages_failed", 1) == 0:
                        _is_proceed = True
                        messages.append(msg_log(agent, "Pipeline 全部完成，进入论文写作", "info"))
                except Exception:
                    pass
            if not _is_proceed:
                messages.append(msg_log(agent, f"S17 决策非 PROCEED，跳过论文写作", "info"))
                output_queue_name = None

        if output_queue_name and output_queue_name in state.queues:
            _, target_layer = QUEUE_NAMES[output_queue_name]
            follow_task = Task(
                id=f"task-{_uid()}",
                project_id=agent.project_id,
                run_dir=agent.run_dir,
                config_path=agent.config_path,
                topic=getattr(agent, '_topic', ''),
                source_layer=agent.layer,
                target_layer=target_layer,
                created_at=_now_ms(),
            )
            state.queues[output_queue_name].push(follow_task)
            messages.append(msg_log(
                agent,
                f"任务完成 → 项目 [{agent.project_id}] 已加入 {output_queue_name} 队列",
                "success",
            ))

    # Release GPU allocation for execution layer
    if agent.layer == "execution" and agent.project_id:
        released = state.gpu_allocator.release(agent.project_id)
        if released:
            messages.append(msg_log(agent, f"GPU {released} 已释放", "info"))

    # Reset agent for next task
    _reset_agent_idle(agent)
    messages.append(msg_agent_update(agent))
    messages.append(msg_queue_update(state.queues))

    return messages


def _reset_agent_idle(agent: LobsterAgent) -> None:
    """Reset agent to idle state, clearing all project-related fields."""
    agent.assigned_task_id = None
    agent.project_id = ""
    agent.status = "idle"
    agent.current_task = "等待任务..."
    agent.run_id = ""
    agent.run_dir = ""
    agent.config_path = ""
    agent.current_stage = 0
    agent.stage_progress = {}
    agent.role_tag = ""
    agent.process = None
    for attr in ("_expected_from_stage", "_expected_to_stage", "_last_exit_reason"):
        if hasattr(agent, attr):
            delattr(agent, attr)
    base = getattr(agent, '_base_name', None)
    if base:
        agent.name = base


def list_all_projects(state: BridgeState, for_user_id: str = "") -> list[dict]:
    """Scan runs/projects/ and return status info for all projects.
    如果 for_user_id 非空，只返回该用户的项目。
    """
    projects_dir = state.projects_dir()
    result: list[dict] = []
    if not projects_dir.exists():
        return result

    running_project_ids: set[str] = set()
    live_agent_ids: set[str] = set()
    for a in state.agents.values():
        if a.process is not None and a.process.poll() is None:
            live_agent_ids.add(a.id)
            if a.project_id:
                running_project_ids.add(a.project_id)

    queued_project_ids: set[str] = set()
    failed_project_ids: set[str] = set()
    for q in state.queues.values():
        for t in q.tasks:
            if not t.project_id:
                continue
            if t.status == "pending":
                queued_project_ids.add(t.project_id)
            elif t.status == "assigned" and t.assigned_to in live_agent_ids:
                queued_project_ids.add(t.project_id)
            elif t.status == "failed":
                failed_project_ids.add(t.project_id)

    # Queue files are the durable source after a bridge restart.  Reading them
    # keeps projects that failed before their first checkpoint from looking new.
    queues_dir = Path(state.runs_base_dir) / "queues"
    if queues_dir.exists():
        for queue_file in queues_dir.glob("*.json"):
            try:
                raw_tasks = json.loads(queue_file.read_text(encoding="utf-8"))
            except Exception:
                continue
            if isinstance(raw_tasks, dict):
                raw_tasks = raw_tasks.get("tasks", [])
            if not isinstance(raw_tasks, list):
                continue
            for item in raw_tasks:
                if not isinstance(item, dict):
                    continue
                project = str(item.get("project_id", "") or "")
                if not project:
                    continue
                status_value = str(item.get("status", "") or "")
                if status_value == "pending":
                    queued_project_ids.add(project)
                elif status_value == "failed":
                    failed_project_ids.add(project)

    for proj_dir in sorted(projects_dir.iterdir()):
        if not proj_dir.is_dir() or proj_dir.name.startswith("_"):
            continue
        project_id = proj_dir.name
        meta = _read_json(proj_dir / "project_meta.json")
        project_mode = meta.get("mode", "lab") if meta else "lab"

        # 按用户过滤：已登录用户只能看到自己的项目
        if for_user_id:
            owner = (meta or {}).get("user_id", "")
            if not owner or owner != for_user_id:
                continue

        # Read checkpoint: top-level first, then aggregate from sub-runs (Lab mode)
        cp = _read_json(proj_dir / "checkpoint.json")
        has_started_artifacts = any(
            child.is_dir() and child.name.startswith("stage-")
            for child in proj_dir.iterdir()
        )
        if not cp:
            best_cp = None
            best_stage = 0
            for sub in proj_dir.iterdir():
                if sub.is_dir() and sub.name.startswith("run-"):
                    if (sub / "heartbeat.json").exists():
                        has_started_artifacts = True
                    if any(child.is_dir() and child.name.startswith("stage-") for child in sub.iterdir()):
                        has_started_artifacts = True
                    sub_cp = _read_json(sub / "checkpoint.json")
                    if sub_cp and sub_cp.get("last_completed_stage", 0) > best_stage:
                        best_stage = sub_cp.get("last_completed_stage", 0)
                        best_cp = sub_cp
            cp = best_cp

        last_stage = cp.get("last_completed_stage", 0) if cp else 0
        last_name = cp.get("last_completed_name", "") if cp else ""
        timestamp = cp.get("timestamp", "") if cp else ""

        first_stage = 1
        total_stages = 26
        completed_threshold = 7 if (meta or {}).get("run_mode") == "literature_watch" else total_stages

        project_is_running = (
            project_id in running_project_ids
            or _project_has_live_heartbeat(proj_dir)
        )

        if last_stage >= completed_threshold:
            status = "completed"
        elif project_is_running:
            status = "running"
        elif project_id in queued_project_ids:
            status = "queued"
        elif last_stage > 0:
            status = "interrupted"
        elif project_id in failed_project_ids:
            status = "failed"
        elif has_started_artifacts:
            status = "interrupted"
        else:
            status = "new"

        topic = ""
        config_path = ""
        if meta:
            topic = meta.get("topic", "")
            config_path = meta.get("config_path", "")
        if not topic:
            goal_path = proj_dir / "stage-01" / "goal.md"
            if not goal_path.exists():
                for sub in proj_dir.iterdir():
                    if sub.is_dir() and sub.name.startswith("run-"):
                        goal_path = sub / "stage-01" / "goal.md"
                        if goal_path.exists():
                            break
            if goal_path.exists():
                try:
                    topic = goal_path.read_text(encoding="utf-8")[:300]
                except OSError:
                    pass

        intervention = meta.get("intervention", "") if meta else ""

        result.append({
            "projectId": project_id,
            "status": status,
            "lastCompletedStage": last_stage,
            "lastCompletedName": last_name,
            "firstStage": first_stage,
            "totalStages": total_stages,
            "timestamp": timestamp,
            "topic": topic,
            "configPath": config_path,
            "intervention": intervention,
            "runMode": (meta or {}).get("run_mode", "full_chain"),
            "user_id": (meta or {}).get("user_id", ""),
        })

    return result


def resume_project(state: BridgeState, project_id: str) -> list[dict]:
    """Resume a project from its last checkpoint."""
    messages: list[dict] = []
    sys_agent = LobsterAgent(id="system", name="系统", layer="idea", run_id="", run_dir="", config_path="")

    state._fail_counts.pop(project_id, None)  # reset fail counter on manual resume

    proj_dir = state.projects_dir() / project_id
    if not proj_dir.exists():
        messages.append(msg_log(sys_agent, f"项目 [{project_id}] 不存在", "error"))
        return messages

    for a in state.agents.values():
        if a.project_id == project_id and a.process is not None and a.process.poll() is None:
            messages.append(msg_log(sys_agent, f"项目 [{project_id}] 已在运行中", "warning"))
            return messages

    meta = _read_json(proj_dir / "project_meta.json")
    config_path = meta.get("config_path", "") if meta else ""
    topic = meta.get("topic", "") if meta else ""
    mode = meta.get("mode", "lab") if meta else "lab"

    # Clear intervention flag on resume
    if meta and meta.get("intervention"):
        meta.pop("intervention", None)
        try:
            (proj_dir / "project_meta.json").write_text(
                json.dumps(meta, indent=2, ensure_ascii=False), encoding="utf-8"
            )
        except Exception:
            pass

    if not config_path:
        messages.append(msg_log(sys_agent, f"项目 [{project_id}] 缺少配置文件路径, 无法恢复", "error"))
        return messages

    # Lab mode with run-* sub-directories: resume each angle separately
    angle_dirs = sorted(proj_dir.glob("run-*"))
    if mode == "lab" and angle_dirs:
        task_count = 0
        for angle_dir in angle_dirs:
            if not angle_dir.is_dir():
                continue
            slug = angle_dir.name.removeprefix("run-")
            angle_config_key = f"{project_id}--{slug}"
            angle_config = str(Path(state.runs_base_dir) / "project_configs" / f"{angle_config_key}.yaml")
            if not Path(angle_config).exists():
                angle_config = config_path

            run_dir = str(angle_dir)
            if _completed_terminal_stage(run_dir):
                messages.append(msg_log(
                    sys_agent,
                    f"  方向 [{slug}] 已完成到 S{PROJECT_TERMINAL_STAGE}，不再继续",
                    "success",
                ))
                continue

            resume_info = _determine_resume_target(run_dir)
            if resume_info:
                target_layer, next_stage = resume_info
                queue_name = _queue_for_layer(target_layer)
                source_layer = {
                    "idea": "init", "experiment": "idea", "coding": "experiment",
                    "execution": "coding", "writing": "execution",
                }.get(target_layer, "init")
                stage_name = STAGE_NAMES.get(next_stage, f"S{next_stage}")
                messages.append(msg_log(
                    sys_agent,
                    f"  方向 [{slug}] 断点恢复 → {stage_name} (Stage {next_stage})",
                    "success",
                ))
            else:
                queue_name = "init_to_idea"
                source_layer = "init"
                target_layer = "idea"
                messages.append(msg_log(sys_agent, f"  方向 [{slug}] 从头开始", "info"))

            task = Task(
                id=f"task-{_uid()}",
                project_id=project_id,
                run_dir=run_dir,
                config_path=angle_config,
                topic=f"[{slug}] {topic}",
                source_layer=source_layer,
                target_layer=target_layer,
                created_at=_now_ms(),
            )
            state.queues[queue_name].push(task)
            task_count += 1

        if task_count >= 2:
            state.lab_batches[project_id] = task_count

        messages.append(msg_queue_update(state.queues))
        messages.append(msg_project_list(list_all_projects(state)))
        messages.append(msg_log(
            sys_agent,
            f"Lab 模式: 项目 [{project_id}] — {task_count} 个方向已恢复",
            "success",
        ))
        return messages

    messages.extend(submit_new_project(
        state,
        project_id,
        config_path,
        topic,
        mode=mode,
        user_id=str((meta or {}).get("user_id", "") or ""),
    ))
    return messages


def _slugify(text: str, max_len: int = 40) -> str:
    """Turn arbitrary text into a filesystem-safe slug."""
    text = text.lower().strip()
    text = re.sub(r'[^\w\s-]', '', text)
    text = re.sub(r'[\s_-]+', '-', text)
    text = text.strip('-')[:max_len].rstrip('-')
    if not text:
        text = hashlib.md5(str(time.time()).encode()).hexdigest()[:8]
    return text


def _infer_experiment_metric(topic: str) -> tuple[str, str] | None:
    """Infer an explicitly requested evaluation metric and its direction.

    The template default is deliberately generic (``primary_metric`` /
    ``minimize``), but that is unsafe when the user names a higher-is-better
    metric such as accuracy.  Only infer from explicit metric terms; otherwise
    preserve the template defaults for domain-specific pipelines.
    """
    normalized = str(topic or "").lower().replace("_", "-")
    maximize_metrics = (
        (("macro-f1", "macro f1", "f1-score", "f1 score"), "f1_macro"),
        (("accuracy", "准确率", "精度"), "accuracy"),
        (("auc", "auroc"), "auroc"),
        (("precision", "查准率"), "precision"),
        (("recall", "查全率"), "recall"),
        (("bleu",), "bleu"),
    )
    for aliases, metric_key in maximize_metrics:
        if any(alias in normalized for alias in aliases):
            return metric_key, "maximize"

    minimize_metrics = (
        (("rmse",), "rmse"),
        (("mae",), "mae"),
        (("latency", "延迟"), "latency"),
        (("error rate", "错误率"), "error_rate"),
        (("loss", "损失"), "loss"),
    )
    for aliases, metric_key in minimize_metrics:
        if any(alias in normalized for alias in aliases):
            return metric_key, "minimize"
    return None


def _generate_config_from_template(
    state: BridgeState, project_id: str, topic: str, role_prompt: str = "",
    reference_papers: list[str] | None = None,
    paper_source_mode: str = "hybrid",
    codebases_dir: str = "", datasets_dir: str = "", checkpoints_dir: str = "",
    model_name: str = "",
    idea_count: int = 5,
    target_conference: str = "",
) -> str:
    """Generate a project-specific YAML config from the default template.

    If role_prompt is provided (Lab mode), it's prepended to the topic so the
    pipeline agent operates from that specialist perspective.
    """
    repo_root = Path(__file__).resolve().parent.parent.parent
    template_path = repo_root / "examples" / "config_template.yaml"
    if not template_path.exists():
        template_path = Path(state.agent_package_dir).parent / "config_template.yaml"
    if not template_path.exists():
        template_path = Path(__file__).resolve().parent.parent / "config_template.yaml"
    if not template_path.exists():
        raise FileNotFoundError(f"Config template not found at {template_path}")

    full_topic = f"{role_prompt}\n\n研究主题: {topic}" if role_prompt else topic
    if target_conference == "ieee":
        full_topic += f"\n\n{IEEE_FULL_CHAIN_REQUIREMENT}"

    content = template_path.read_text(encoding="utf-8")
    content = content.replace("__PROJECT_ID__", project_id)
    content = content.replace("__TOPIC__", full_topic.replace('"', '\\"'))

    if reference_papers:
        yaml_list = "\n".join(f'    - "{p}"' for p in reference_papers)
        content = content.replace("  reference_papers: __REFERENCE_PAPERS__",
                                  f"  reference_papers:\n{yaml_list}")
    else:
        content = content.replace("  reference_papers: __REFERENCE_PAPERS__",
                                  "  reference_papers: []")
    content = content.replace(
        "  reference_papers: []",
        f"  reference_papers: []\n  paper_source_mode: \"{paper_source_mode}\"",
        1,
    ) if "paper_source_mode:" not in content else content
    content = content.replace(
        f"  reference_papers:\n{yaml_list}" if reference_papers else "",
        (f"  reference_papers:\n{yaml_list}\n  paper_source_mode: \"{paper_source_mode}\""
         if reference_papers and f"  reference_papers:\n{yaml_list}" in content else ""),
        1,
    ) if reference_papers and "paper_source_mode:" not in content else content

    import re as _re
    if codebases_dir:
        content = _re.sub(r'(codebases_dir:\s*)"[^"]*"', f'\\1"{codebases_dir}"', content)
    if datasets_dir:
        content = _re.sub(r'(datasets_dir:\s*)"[^"]*"', f'\\1"{datasets_dir}"', content)
    if checkpoints_dir:
        content = _re.sub(r'(checkpoints_dir:\s*)"[^"]*"', f'\\1"{checkpoints_dir}"', content)
    selected_model = _coerce_qwen3_model(model_name) if model_name else DEFAULT_QWEN3_MODEL
    if selected_model:
        root_section = _llm_section_for_model(_load_root_arc_config(), selected_model)
        if root_section:
            import yaml as _yaml

            raw = _yaml.safe_load(content) or {}
            llm_raw = raw.setdefault("llm", {})
            if isinstance(llm_raw, dict):
                for key in (
                    "provider", "base_url", "api_key", "api_key_env", "timeout_sec",
                    "max_retries", "max_tokens", "strip_thinking", "extra_body",
                ):
                    if key in root_section:
                        llm_raw[key] = root_section[key]
                llm_raw["primary_model"] = selected_model
                llm_raw["coding_model"] = selected_model
                llm_raw["fallback_models"] = []
            content = _yaml.safe_dump(raw, sort_keys=False, allow_unicode=True)
        else:
            print(f"[config] selected model has no configured endpoint, keeping template defaults: {selected_model}")

    import yaml as _yaml
    try:
        raw_cfg = _yaml.safe_load(content) or {}
        research_cfg = raw_cfg.setdefault("research", {})
        research_cfg["idea_count"] = max(1, min(8, int(idea_count or 5)))
        inferred_metric = _infer_experiment_metric(topic)
        if inferred_metric:
            experiment_cfg = raw_cfg.setdefault("experiment", {})
            experiment_cfg["metric_key"], experiment_cfg["metric_direction"] = inferred_metric
        if target_conference:
            export_cfg = raw_cfg.setdefault("export", {})
            export_cfg["target_conference"] = target_conference
            if target_conference == "ieee":
                export_cfg["target_pages"] = 8
                export_cfg["min_pages"] = 7
                export_cfg["max_pages"] = 8
        content = _yaml.safe_dump(raw_cfg, sort_keys=False, allow_unicode=True)
    except Exception:
        pass

    configs_dir = Path(state.runs_base_dir) / "project_configs"
    configs_dir.mkdir(parents=True, exist_ok=True)
    config_path = configs_dir / f"{project_id}.yaml"
    config_path.write_text(content, encoding="utf-8")
    return str(config_path)


def _safe_reference_upload_name(filename: str) -> str:
    base = Path(filename or "reference.pdf").name
    cleaned = re.sub(r"[^\w.\-]+", "_", base).strip("._")
    if not cleaned.lower().endswith(".pdf"):
        cleaned = f"{cleaned or 'reference'}.pdf"
    return cleaned


def _sanitize_model_name(model_name: str) -> str:
    model = str(model_name or "").strip()
    if not model or len(model) > 128:
        return ""
    if not re.fullmatch(r"[\w.\-:/]+", model):
        return ""
    return model


def _is_qwen3_model(model_name: str) -> bool:
    model = str(model_name or "").lower()
    return "qwen3" in model or "qwen-3" in model


def _coerce_qwen3_model(model_name: str) -> str:
    model = _sanitize_model_name(model_name)
    return model if _is_qwen3_model(model) else DEFAULT_QWEN3_MODEL


def _persist_reference_uploads(
    project_dir: Path,
    reference_uploads: list[dict[str, str]] | None,
) -> list[str]:
    if not reference_uploads:
        return []

    upload_dir = project_dir / "reference_uploads"
    upload_dir.mkdir(parents=True, exist_ok=True)
    saved_paths: list[str] = []

    for item in reference_uploads:
        if not isinstance(item, dict):
            continue
        name = _safe_reference_upload_name(str(item.get("name", "reference.pdf")))
        content_b64 = str(item.get("contentBase64", "")).strip()
        if not content_b64:
            continue
        try:
            raw = base64.b64decode(content_b64, validate=True)
        except Exception:
            continue
        stem = Path(name).stem
        suffix = Path(name).suffix or ".pdf"
        target = upload_dir / f"{stem}-{uuid.uuid4().hex[:6]}{suffix}"
        target.write_bytes(raw)
        saved_paths.append(str(target.resolve()))

    return saved_paths


def _parse_reference_papers(value: object) -> list[str]:
    if isinstance(value, str):
        return [p.strip() for p in re.split(r"[\n,，;；]", value) if p.strip()]
    if isinstance(value, list):
        return [str(p).strip() for p in value if str(p).strip()]
    return []


def _project_config_paths(state: BridgeState, project_id: str, project_dir: Path) -> list[Path]:
    paths: list[Path] = []
    meta = _read_project_meta(str(project_dir)) or {}
    meta_config = str(meta.get("config_path", "")).strip()
    if meta_config:
        paths.append(Path(meta_config))

    configs_dir = Path(state.runs_base_dir) / "project_configs"
    if configs_dir.is_dir():
        prefix = f"{project_id}"
        for path in sorted(configs_dir.glob("*.yaml")):
            if path.name == f"{project_id}.yaml" or path.name.startswith(f"{prefix}--"):
                paths.append(path)

    unique: list[Path] = []
    seen: set[str] = set()
    for path in paths:
        key = str(path.resolve()) if path.exists() else str(path)
        if key not in seen:
            seen.add(key)
            unique.append(path)
    return unique


def _update_config_reference_papers(
    config_path: Path,
    references: list[str],
    paper_source_mode: str,
) -> tuple[int, int]:
    import yaml

    if not config_path.exists():
        return (0, 0)
    try:
        raw = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}
    except Exception:
        return (0, 0)

    research = raw.setdefault("research", {})
    existing = research.get("reference_papers", [])
    if not isinstance(existing, list):
        existing = []

    normalized_existing = [str(item).strip() for item in existing if str(item).strip()]
    seen = set(normalized_existing)
    added = 0
    for ref in references:
        if ref not in seen:
            normalized_existing.append(ref)
            seen.add(ref)
            added += 1

    research["reference_papers"] = normalized_existing
    if paper_source_mode:
        research["paper_source_mode"] = paper_source_mode

    config_path.write_text(
        yaml.safe_dump(raw, sort_keys=False, allow_unicode=True),
        encoding="utf-8",
    )
    return (added, len(normalized_existing))


def _import_literature_batch(
    state: BridgeState,
    project_id: str,
    reference_papers: object = None,
    reference_uploads: list[dict[str, str]] | None = None,
    paper_source_mode: str = "hybrid",
) -> list[dict]:
    messages: list[dict] = []
    sys_agent = LobsterAgent(id="system", name="系统", layer="idea", run_id="", run_dir="", config_path="")
    project_id = project_id.strip()
    project_dir = state.projects_dir() / project_id
    if not project_id or not project_dir.exists():
        messages.append(msg_log(sys_agent, f"项目不存在: {project_id or 'N/A'}", "error"))
        return messages

    mode = (paper_source_mode or "hybrid").strip().lower()
    if mode not in {"upload", "hybrid", "auto"}:
        mode = "hybrid"
    if mode == "auto":
        mode = "hybrid"

    text_refs = _parse_reference_papers(reference_papers)
    saved_paths = _persist_reference_uploads(project_dir, reference_uploads)
    references = [*text_refs, *saved_paths]
    if not references:
        messages.append(msg_log(sys_agent, "没有可导入的文献。请提供标题/DOI/arXiv 或 PDF 文件。", "warning"))
        return messages

    config_paths = _project_config_paths(state, project_id, project_dir)
    total_added = 0
    updated_configs = 0
    for config_path in config_paths:
        added, _total = _update_config_reference_papers(config_path, references, mode)
        if added > 0 or config_path.exists():
            updated_configs += 1
            total_added += added

    import_entry = {
        "id": f"lit-{_uid()}",
        "project_id": project_id,
        "timestamp": _now_ms(),
        "paper_source_mode": mode,
        "text_references": text_refs,
        "uploaded_files": saved_paths,
        "updated_configs": [str(path) for path in config_paths if path.exists()],
    }
    try:
        import_log = project_dir / "literature_imports.jsonl"
        with open(import_log, "a", encoding="utf-8") as f:
            f.write(json.dumps(import_entry, ensure_ascii=False) + "\n")
    except OSError:
        pass

    meta_path = project_dir / "project_meta.json"
    meta = _read_json(meta_path) or {}
    meta["last_literature_import"] = {
        "timestamp": import_entry["timestamp"],
        "paper_source_mode": mode,
        "reference_count": len(references),
        "uploaded_file_count": len(saved_paths),
    }
    _write_json(meta_path, meta)

    messages.append(msg_log(
        sys_agent,
        f"项目 [{project_id}] 已导入 {len(references)} 篇/条文献，更新 {updated_configs} 个配置",
        "success",
    ))
    if saved_paths:
        messages.append(msg_log(sys_agent, f"已保存 {len(saved_paths)} 个 PDF 到 reference_uploads", "info"))
    if total_added == 0:
        messages.append(msg_log(sys_agent, "这些文献已存在于配置中，本次没有新增条目", "info"))
    messages.append(msg_project_list(list_all_projects(state)))
    return messages


KNOWN_LAB_ANGLES: dict[str, str] = {
    "CV": (
        "你是实验室的「计算机视觉 (CV)」方向研究员。"
        "你的专长是图像识别、目标检测、语义分割、图像生成、视频理解。"
        "请从 CV 的视角进行深入调研，"
        "重点关注: 视觉骨干网络（ViT/CNN/Mamba）、"
        "自监督/对比学习、生成模型（Diffusion/GAN/Flow Matching）、"
        "3D 视觉、视频时序建模、以及 CV 在多模态与具身场景中的应用。"
    ),
    "VLM": (
        "你是实验室的「视觉语言模型 (VLM)」方向研究员。"
        "你的专长是多模态理解、视觉-语言对齐、图文推理、视觉 Grounding。"
        "请从 VLM 的视角进行深入调研，"
        "重点关注: 视觉编码器选型、跨模态融合架构、指令微调策略、"
        "视觉推理能力评估、以及 VLM 在具身场景中的感知与决策应用。"
    ),
    "World Model": (
        "你是实验室的「世界模型 (World Model)」方向研究员。"
        "你的专长是环境建模、视频预测、物理仿真、因果推理。"
        "请从 World Model 的视角进行深入调研，"
        "重点关注: 世界模型的架构设计（自回归/扩散/状态空间）、"
        "时空表征学习、动力学建模、长时序预测、"
        "以及世界模型在具身智能中的规划与想象能力。"
    ),
    "VLA": (
        "你是实验室的「视觉-语言-动作模型 (VLA)」方向研究员。"
        "你的专长是端到端策略学习、动作生成、机器人操作、模仿学习。"
        "请从 VLA 的视角进行深入调研，"
        "重点关注: VLA 模型架构（RT-2、OpenVLA、π₀ 等）、"
        "动作 tokenization 与解码策略、多任务泛化、"
        "sim-to-real 迁移、以及 VLA 在真实机器人上的部署与评估。"
    ),
}

DEFAULT_LAB_ANGLES: list[dict[str, str]] = [
    {"name": "CV", "prompt": KNOWN_LAB_ANGLES["CV"]},
]


def _build_role_prompt(angle_name: str, main_topic: str) -> str:
    """Build a role prompt for a Lab mode agent. Uses predefined prompts for known
    angles, otherwise generates a reasonable prompt from the angle name."""
    if angle_name in KNOWN_LAB_ANGLES:
        return KNOWN_LAB_ANGLES[angle_name]
    return (
        f"你是实验室的「{angle_name}」方向研究员。"
        f"请从 {angle_name} 的专业视角对研究主题进行深入调研，"
        f"重点关注该方向最相关的理论、方法、数据集和最新进展。"
    )


def quick_submit_project(
    state: BridgeState, topic: str, project_id: str = "",
    mode: str = "lab",
    submission_mode: str = "hybrid",
    research_angles: list[str] | None = None,
    reference_papers: list[str] | None = None,
    reference_uploads: list[dict[str, str]] | None = None,
    path_overrides: dict[str, str] | None = None,
    model_name: str = "",
    user_id: str = "",
    idea_count: int = 5,
    run_mode: str = "full_chain",
) -> list[dict]:
    """Create a project from a topic string.

    Modes:
      - "lab": Multi-angle parallel research (default). If no angles provided,
        uses 3 default perspectives. Each agent gets a specialized role prompt.
      - "reproduce": Single-agent focused pipeline for paper reproduction.
    """
    messages: list[dict] = []
    sys_agent = LobsterAgent(id="system", name="系统", layer="idea", run_id="", run_dir="", config_path="")

    if not topic.strip():
        messages.append(msg_log(sys_agent, "请输入研究主题", "error"))
        return messages

    submission_mode = (submission_mode or "hybrid").strip().lower()
    if submission_mode not in {"auto", "upload", "hybrid"}:
        submission_mode = "hybrid"
    run_mode = (run_mode or "full_chain").strip().lower()
    if run_mode not in {"full_chain", "idea_gate"}:
        run_mode = "full_chain"
    selected_model = _coerce_qwen3_model(model_name)

    base_id = project_id or _slugify(topic)

    # ── Reproduce mode: single-agent standard pipeline ──
    if mode == "reproduce":
        if not project_id:
            existing = state.projects_dir() / base_id
            if existing.exists():
                base_id = f"{base_id}-{_uid()[:4]}"
        project_dir = state.projects_dir() / base_id
        project_dir.mkdir(parents=True, exist_ok=True)
        saved_reference_paths = _persist_reference_uploads(project_dir, reference_uploads)
        all_reference_papers = [*(reference_papers or []), *saved_reference_paths]
        if submission_mode == "auto":
            all_reference_papers = []
        if submission_mode == "upload" and not all_reference_papers:
            messages.append(msg_log(sys_agent, "上传优先模式至少需要提供一篇参考文献或上传一个 PDF", "error"))
            return messages
        _po = path_overrides or {}
        try:
            config_path = _generate_config_from_template(
                state, base_id, topic.strip(),
                reference_papers=all_reference_papers,
                paper_source_mode=submission_mode,
                codebases_dir=_po.get("codebases_dir", ""),
                datasets_dir=_po.get("datasets_dir", ""),
                checkpoints_dir=_po.get("checkpoints_dir", ""),
                model_name=selected_model,
                idea_count=idea_count,
                target_conference="ieee" if run_mode == "full_chain" else "",
            )
        except Exception as e:
            messages.append(msg_log(sys_agent, f"配置生成失败: {e}", "error"))
            return messages
        if saved_reference_paths:
            messages.append(msg_log(sys_agent, f"已接收 {len(saved_reference_paths)} 个本地 PDF 参考文件", "info"))
        messages.append(msg_log(sys_agent, f"复现模式: 项目 [{base_id}] 单 Agent 全流程启动", "success"))
        messages.extend(submit_new_project(state, base_id, config_path, topic.strip(), mode="reproduce", user_id=user_id, run_mode=run_mode))
        return messages

    # ── Lab mode: parallel research (ONE project, N agents — or 1 agent if single direction) ──
    angles: list[dict[str, str]]
    if research_angles and len(research_angles) >= 1:
        angles = [
            {"name": a.strip(), "prompt": _build_role_prompt(a.strip(), topic.strip())}
            for a in research_angles if a.strip()
        ]
    if not research_angles or not angles:
        angles = DEFAULT_LAB_ANGLES

    # Deduplicate project id
    existing = state.projects_dir() / base_id
    if existing.exists():
        base_id = f"{base_id}-{_uid()[:4]}"

    project_dir = state.projects_dir() / base_id
    project_dir.mkdir(parents=True, exist_ok=True)
    saved_reference_paths = _persist_reference_uploads(project_dir, reference_uploads)
    all_reference_papers = [*(reference_papers or []), *saved_reference_paths]
    if submission_mode == "auto":
        all_reference_papers = []
    if submission_mode == "upload" and not all_reference_papers:
        messages.append(msg_log(sys_agent, "上传优先模式至少需要提供一篇参考文献或上传一个 PDF", "error"))
        return messages

    _po = path_overrides or {}
    try:
        project_config_path = _generate_config_from_template(
            state, base_id, topic.strip(),
            reference_papers=all_reference_papers,
            paper_source_mode=submission_mode,
            codebases_dir=_po.get("codebases_dir", ""),
            datasets_dir=_po.get("datasets_dir", ""),
            checkpoints_dir=_po.get("checkpoints_dir", ""),
            model_name=selected_model,
            idea_count=idea_count,
            target_conference="ieee" if run_mode == "full_chain" else "",
        )
    except Exception as e:
        messages.append(msg_log(sys_agent, f"配置生成失败: {e}", "error"))
        return messages

    _save_project_meta(str(project_dir), base_id, project_config_path, topic.strip(), mode="lab", user_id=user_id, run_mode=run_mode)

    messages.append(msg_log(
        sys_agent,
        f"Lab 模式: 项目 [{base_id}] — {len(angles)} 个方向并行调研，模型 {selected_model}",
        "info",
    ))
    if saved_reference_paths:
        messages.append(msg_log(sys_agent, f"已接收 {len(saved_reference_paths)} 个本地 PDF 参考文件", "info"))

    task_count = 0
    for i, angle in enumerate(angles):
        name = angle["name"]
        role_prompt = angle["prompt"]
        slug = _slugify(name, 20)

        # Each direction gets its own run sub-directory within the project
        run_dir = str(project_dir / f"run-{slug}")
        os.makedirs(run_dir, exist_ok=True)

        _po = path_overrides or {}
        try:
            config_path = _generate_config_from_template(
                state, f"{base_id}--{slug}", topic.strip(), role_prompt,
                reference_papers=all_reference_papers,
                paper_source_mode=submission_mode,
                codebases_dir=_po.get("codebases_dir", ""),
                datasets_dir=_po.get("datasets_dir", ""),
                checkpoints_dir=_po.get("checkpoints_dir", ""),
                model_name=selected_model,
                idea_count=idea_count,
                target_conference="ieee" if run_mode == "full_chain" else "",
            )
        except Exception as e:
            messages.append(msg_log(sys_agent, f"配置生成失败 [{name}]: {e}", "error"))
            continue

        task = Task(
            id=f"task-{_uid()}",
            project_id=base_id,
            run_dir=run_dir,
            config_path=config_path,
            topic=f"[{name}] {topic.strip()}",
            source_layer="init",
            target_layer="idea",
            created_at=_now_ms(),
        )
        state.queues["init_to_idea"].push(task)
        task_count += 1

        messages.append(msg_log(
            sys_agent,
            f"  方向 {i+1}/{len(angles)}: {name}",
            "info",
        ))

    # Register Lab batch: same project_id, expect N agents to finish S7
    if task_count >= 2:
        state.lab_batches[base_id] = task_count

    messages.append(msg_queue_update(state.queues))
    messages.append(msg_project_list(list_all_projects(state)))
    messages.append(msg_log(
        sys_agent,
        (
            f"{task_count} 个方向 agent S7 完成后将自动讨论 → 合并为统一假设 → 暂停等待确认"
            if run_mode == "idea_gate"
            else f"{task_count} 个方向 agent S7 完成后将自动讨论 → 合并为统一假设 → 进入 L2"
        ),
        "success",
    ))
    return messages


def _create_model_config(base_config_path: str, model_name: str, output_dir: str) -> str:
    """Create a per-agent config file with a different primary_model."""
    import yaml
    model_name = _coerce_qwen3_model(model_name)
    with open(base_config_path, "r", encoding="utf-8") as f:
        cfg = yaml.safe_load(f)
    if "llm" in cfg:
        cfg["llm"]["primary_model"] = model_name
        cfg["llm"]["coding_model"] = model_name
        cfg["llm"]["image_model"] = model_name
        cfg["llm"]["fallback_models"] = []
    agent_config_path = str(Path(output_dir) / f"config_{model_name.replace('/', '_')}.yaml")
    with open(agent_config_path, "w", encoding="utf-8") as f:
        yaml.dump(cfg, f, default_flow_style=False, allow_unicode=True)
    return agent_config_path


def _launch_idea_factory_run(state: BridgeState, agent: LobsterAgent, s7_only: bool = False, model_override: str = "") -> list[dict]:
    """Launch L1 agent to produce ideas. s7_only=True runs only S7 (for discussion mode)."""
    messages: list[dict] = []

    idea_id = f"idea-{_uid()}"
    run_dir = str(Path(state.runs_base_dir).parent / "shared_results" / "idea_runs" / idea_id)
    os.makedirs(run_dir, exist_ok=True)

    _s6_seed = Path(run_dir) / "stage-06"
    _s6_seed.mkdir(parents=True, exist_ok=True)
    (_s6_seed / "cards").mkdir(exist_ok=True)

    _s7_seed = Path(run_dir) / "stage-07"
    _s7_seed.mkdir(parents=True, exist_ok=True)

    config_path = state.idea_factory_config
    if model_override:
        try:
            config_path = _create_model_config(state.idea_factory_config, model_override, run_dir)
        except Exception as e:
            messages.append(msg_log(agent, f"模型配置创建失败 ({model_override}): {e}，使用默认配置", "warning"))
            config_path = state.idea_factory_config

    task = Task(
        id=f"task-{_uid()}",
        project_id=idea_id,
        run_dir=run_dir,
        config_path=config_path,
        topic=state.idea_factory_topic,
        source_layer="idea_factory",
        target_layer="idea",
        created_at=_now_ms(),
    )

    _assign_task_to_agent(agent, task)
    agent._is_idea_factory = True  # type: ignore[attr-defined]
    agent._is_idea_factory_s7_only = s7_only  # type: ignore[attr-defined]

    if s7_only:
        layer_range = (7, 7)
    else:
        layer_range = (7, 8)
    fs, ts = layer_range

    cmd = [
        state.python_path, "-m", "researchclaw", "run",
        "--config", config_path,
        "--output", task.run_dir,
        "--from-stage", STAGE_NAMES.get(fs, str(fs)),
        "--to-stage", STAGE_NAMES.get(ts, str(ts)),
        "--auto-approve",
        "--topic", state.idea_factory_topic,
    ]

    try:
        log_path = Path(run_dir) / f"agent_{agent.id}.log"
        log_file = open(log_path, "w", encoding="utf-8")
        proc = subprocess.Popen(
            cmd, cwd=state.agent_package_dir,
            stdout=log_file, stderr=subprocess.STDOUT,
            env={**os.environ, "PYTHONUNBUFFERED": "1"},
        )
        agent.process = proc
        n = state.idea_factory_produced + 1
        model_tag = f" [{model_override}]" if model_override else ""
        agent.current_task = f"Idea 工厂 #{n}{model_tag}" + (" (S7 综合)" if s7_only else " (S7→S8)")
        messages.append(msg_agent_update(agent))
        label = f"Idea 工厂 #{n}: 知识综合中 (S7){model_tag}" if s7_only else f"Idea 工厂 #{n}: 生成假设中 (S7→S8){model_tag}"
        messages.append(msg_log(agent, label, "info"))
    except Exception as e:
        agent.status = "error"
        agent.current_task = f"Idea 工厂启动失败: {e}"
        messages.append(msg_agent_update(agent))

    return messages


def _on_idea_factory_done(state: BridgeState, agent: LobsterAgent) -> list[dict]:
    """Handle idea factory run completion: extract hypotheses, push to idea pool + L2 queue."""
    messages: list[dict] = []
    run_dir = Path(agent.run_dir)

    # Read hypotheses
    hyp_file = None
    for sd in sorted(run_dir.glob("stage-08*"), reverse=True):
        f = sd / "hypotheses.md"
        if f.exists():
            hyp_file = f
            break

    if hyp_file:
        hyp_text = hyp_file.read_text(encoding="utf-8")

        # Write to idea pool
        pool_dir = Path(state.runs_base_dir).parent / "shared_results" / "idea_pool"
        pool_dir.mkdir(parents=True, exist_ok=True)
        pool_file = pool_dir / "ideas.jsonl"

        entry = {
            "id": agent.project_id,
            "topic": state.idea_factory_topic,
            "hypotheses": hyp_text[:2000],
            "timestamp": _now_ms(),
            "status": "pending",
        }
        with open(pool_file, "a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")

        # Create L2 task from this idea
        idea_run_dir = str(Path(state.runs_base_dir) / "projects" / agent.project_id)
        os.makedirs(idea_run_dir, exist_ok=True)

        # Copy hypotheses to the project run dir so L2 can find it
        s8_dir = Path(idea_run_dir) / "stage-08"
        s8_dir.mkdir(parents=True, exist_ok=True)
        (s8_dir / "hypotheses.md").write_text(hyp_text, encoding="utf-8")

        # Also copy synthesis if available
        for sd in sorted(run_dir.glob("stage-07*"), reverse=True):
            sf = sd / "synthesis.md"
            if sf.exists():
                s7_dir = Path(idea_run_dir) / "stage-07"
                s7_dir.mkdir(parents=True, exist_ok=True)
                (s7_dir / "synthesis.md").write_text(sf.read_text(encoding="utf-8"), encoding="utf-8")
                break

        # Push to idea_to_experiment queue
        queue = state.queues.get("idea_to_experiment")
        if queue:
            follow_task = Task(
                id=f"task-{_uid()}",
                project_id=agent.project_id,
                run_dir=idea_run_dir,
                config_path=state.idea_factory_config,
                topic=state.idea_factory_topic,
                source_layer="idea",
                target_layer="experiment",
                created_at=_now_ms(),
            )
            queue.push(follow_task)
            messages.append(msg_log(agent, f"Idea #{state.idea_factory_produced + 1} → 实验设计队列", "success"))

        state.idea_factory_produced += 1
        if state.idea_factory_remaining > 0:
            state.idea_factory_remaining -= 1

        messages.append(msg_log(
            agent,
            f"Idea 工厂: 已产出 {state.idea_factory_produced} 个, 剩余 {'无限' if state.idea_factory_remaining == -1 else state.idea_factory_remaining}",
            "info",
        ))
    else:
        messages.append(msg_log(agent, "Idea 工厂: 未生成假设", "warning"))

    # Reset agent
    _reset_agent_idle(agent)
    agent._is_idea_factory = False  # type: ignore[attr-defined]
    agent._is_idea_factory_s7_only = False  # type: ignore[attr-defined]
    agent._is_discussion_s8 = False  # type: ignore[attr-defined]
    agent._idea_factory_batch_id = None  # type: ignore[attr-defined]
    messages.append(msg_agent_update(agent))

    return messages


def _on_idea_factory_s7_done(state: BridgeState, agent: LobsterAgent) -> list[dict]:
    """Handle idea factory S7-only completion: enter discussion flow."""
    messages: list[dict] = []
    agent._is_idea_factory_s7_only = False  # type: ignore[attr-defined]

    batch_id = getattr(agent, '_idea_factory_batch_id', None)
    if not batch_id or batch_id not in state.discussion_groups:
        messages.append(msg_log(agent, "S7 完成但无沟通讨论组，回退到非讨论模式", "warning"))
        agent._is_idea_factory = False  # type: ignore[attr-defined]
        _reset_agent_idle(agent)
        messages.append(msg_agent_update(agent))
        return messages

    group = state.discussion_groups[batch_id]
    group.completed_s7.add(agent.id)
    agent.status = "waiting_discussion"
    agent.current_stage = DISCUSSION_STAGE
    agent.current_task = f"等待沟通讨论 ({len(group.completed_s7)}/{len(group.agent_ids)})"
    agent.stage_progress[DISCUSSION_STAGE] = "running"
    messages.append(msg_agent_update(agent))
    messages.append(msg_stage_update(agent.id, DISCUSSION_STAGE, "running"))
    messages.append(msg_log(agent, f"S7 完成，等待沟通讨论 ({len(group.completed_s7)}/{len(group.agent_ids)})", "info", DISCUSSION_STAGE))

    if group.all_ready():
        messages.extend(_trigger_discussion(state, group))

    return messages


def _trigger_discussion(state: BridgeState, group: DiscussionGroup) -> list[dict]:
    """Launch the discussion runner when all agents in a group have completed S7."""
    messages: list[dict] = []
    group.status = "discussing"

    disc_dir = str(state.projects_dir() / group.project_id / "discussion")
    os.makedirs(disc_dir, exist_ok=True)
    group.discussion_output_dir = disc_dir

    for aid in group.agent_ids:
        agent = state.agents.get(aid)
        if agent:
            agent.status = "discussing"
            agent.current_stage = DISCUSSION_STAGE
            agent.current_task = "多 Agent 沟通讨论中..."
            messages.append(msg_agent_update(agent))

    synthesis_dirs = group.synthesis_dirs()
    runner_path = str(Path(__file__).resolve().parent / "discussion_runner.py")
    cmd = [
        state.python_path, runner_path,
        "--config", group.config_path,
        "--synthesis-dirs", *synthesis_dirs,
        "--output", disc_dir,
        "--rounds", str(state.discussion_rounds),
    ]
    if group.topic:
        cmd.extend(["--topic", group.topic])

    try:
        log_path = Path(disc_dir) / "discussion.log"
        log_file = open(log_path, "w", encoding="utf-8")
        proc = subprocess.Popen(
            cmd, cwd=state.agent_package_dir,
            stdout=log_file, stderr=subprocess.STDOUT,
            env={**os.environ, "PYTHONUNBUFFERED": "1"},
        )
        group.discussion_process = proc
        sys_agent = LobsterAgent(id="system", name="系统", layer="idea", run_id="", run_dir="", config_path="")
        messages.append(msg_log(
            sys_agent,
            f"项目 [{group.project_id}] 沟通讨论开始: {len(group.agent_ids)} 个 agent, {state.discussion_rounds} 轮 (PID={proc.pid})",
            "info", DISCUSSION_STAGE,
        ))
    except Exception as e:
        group.status = "done"
        sys_agent = LobsterAgent(id="system", name="系统", layer="idea", run_id="", run_dir="", config_path="")
        messages.append(msg_log(sys_agent, f"沟通讨论启动失败: {e}", "error", DISCUSSION_STAGE))
        for aid in group.agent_ids:
            agent = state.agents.get(aid)
            if agent:
                agent.status = "error"
                agent.current_task = f"沟通讨论启动失败: {e}"
                messages.append(msg_agent_update(agent))

    return messages


def _trigger_cross_project_discussion(
    state: BridgeState, agent1: LobsterAgent, agent2: LobsterAgent,
) -> list[dict]:
    """Launch a discussion between two agents from different projects."""
    messages: list[dict] = []

    for a in (agent1, agent2):
        a.status = "discussing"
        a.current_task = f"跨 project 讨论: {agent1.project_id} × {agent2.project_id}"
        messages.append(msg_agent_update(a))

    p1_id = agent1.project_id or agent1.name
    p2_id = agent2.project_id or agent2.name
    disc_name = f"{p1_id}_x_{p2_id}"
    disc_dir = str(state.projects_dir() / "_cross_discussions" / disc_name)
    os.makedirs(disc_dir, exist_ok=True)

    synthesis_dirs = []
    for a in (agent1, agent2):
        s7 = Path(a.run_dir) / "stage-07" if a.run_dir else None
        if s7 and s7.exists():
            synthesis_dirs.append(str(s7))

    group = DiscussionGroup(
        project_id=disc_name,
        topic=f"{p1_id} | {p2_id}",
        config_path=agent1.config_path,
    )
    group.agent_ids = [agent1.id, agent2.id]
    group.run_dirs = {agent1.id: agent1.run_dir, agent2.id: agent2.run_dir}
    group.status = "discussing"
    group.discussion_output_dir = disc_dir
    group._cross_project = True  # type: ignore[attr-defined]

    runner_path = str(Path(__file__).resolve().parent / "discussion_runner.py")
    cmd = [
        state.python_path, runner_path,
        "--config", agent1.config_path,
        "--synthesis-dirs", *synthesis_dirs,
        "--output", disc_dir,
        "--rounds", str(state.discussion_rounds),
        "--topic", group.topic,
    ]

    try:
        log_path = Path(disc_dir) / "discussion.log"
        log_file = open(log_path, "w", encoding="utf-8")
        proc = subprocess.Popen(
            cmd, cwd=state.agent_package_dir,
            stdout=log_file, stderr=subprocess.STDOUT,
            env={**os.environ, "PYTHONUNBUFFERED": "1"},
        )
        group.discussion_process = proc
        state.discussion_groups[disc_name] = group

        sys_agent = LobsterAgent(id="system", name="系统", layer="idea", run_id="", run_dir="", config_path="")
        messages.append(msg_log(
            sys_agent,
            f"跨 project 讨论开始: [{agent1.project_id}] × [{agent2.project_id}], {state.discussion_rounds} 轮 (PID={proc.pid})",
            "info", DISCUSSION_STAGE,
        ))
    except Exception as e:
        sys_agent = LobsterAgent(id="system", name="系统", layer="idea", run_id="", run_dir="", config_path="")
        messages.append(msg_log(sys_agent, f"跨 project 讨论启动失败: {e}", "error", DISCUSSION_STAGE))
        for a in (agent1, agent2):
            state.discussion_waiting.pop(a.id, None)
        messages.extend(_skip_discussion_proceed_s8(state, agent1))
        messages.extend(_skip_discussion_proceed_s8(state, agent2))

    return messages


def _skip_discussion_proceed_s8(state: BridgeState, agent: LobsterAgent) -> list[dict]:
    """Skip discussion and proceed directly to S8 for a single agent."""
    messages: list[dict] = []
    state.discussion_waiting.pop(agent.id, None)

    agent.stage_progress[DISCUSSION_STAGE] = "completed"
    messages.append(msg_stage_update(agent.id, DISCUSSION_STAGE, "completed"))

    fs, ts = LAYER_RANGE_PHASE2["idea"]
    agent.status = "working"
    agent.current_task = f"项目 {agent.project_id} · S8 假设生成 (跳过讨论)"
    agent.stage_progress[8] = "running"
    messages.append(msg_agent_update(agent))
    messages.append(msg_stage_update(agent.id, 8, "running"))
    messages.append(msg_log(agent, "跳过讨论 → 直接启动 S8 假设生成", "info", 8))

    cmd = [
        state.python_path, "-m", "researchclaw", "run",
        "--config", agent.config_path,
        "--output", agent.run_dir,
        "--from-stage", STAGE_NAMES.get(fs, str(fs)),
        "--to-stage", STAGE_NAMES.get(ts, str(ts)),
        "--auto-approve",
    ]
    if agent.project_id:
        cmd.extend(["--topic", agent.project_id])

    try:
        log_path = Path(agent.run_dir) / f"agent_{agent.id}_s8.log"
        log_file = open(log_path, "w", encoding="utf-8")
        proc = subprocess.Popen(
            cmd, cwd=state.agent_package_dir,
            stdout=log_file, stderr=subprocess.STDOUT,
            env={**os.environ, "PYTHONUNBUFFERED": "1"},
        )
        agent.process = proc
        agent._expected_from_stage = fs  # type: ignore[attr-defined]
        agent._expected_to_stage = ts  # type: ignore[attr-defined]
        agent._is_discussion_s8 = True  # type: ignore[attr-defined]
        messages.append(msg_log(agent, f"S8 启动 (PID={proc.pid})", "info", 8))
    except Exception as e:
        agent.status = "error"
        agent.current_task = f"S8 启动失败: {e}"
        messages.append(msg_agent_update(agent))
        messages.append(msg_log(agent, f"S8 启动失败: {e}", "error"))

    return messages


def _poll_discussion(state: BridgeState, group: DiscussionGroup) -> list[dict]:
    """Check if a discussion subprocess has finished and handle completion."""
    messages: list[dict] = []
    if group.status != "discussing" or group.discussion_process is None:
        return messages

    retcode = group.discussion_process.poll()
    if retcode is None:
        return messages

    group.discussion_process = None
    sys_agent = LobsterAgent(id="system", name="系统", layer="idea", run_id="", run_dir="", config_path="")

    is_cross = getattr(group, "_cross_project", False)

    if retcode != 0:
        group.status = "done"
        messages.append(msg_log(sys_agent, f"讨论 [{group.project_id}] 失败 (exit={retcode})", "error", DISCUSSION_STAGE))
        for aid in group.agent_ids:
            agent = state.agents.get(aid)
            if agent:
                state.discussion_waiting.pop(aid, None)
                if is_cross:
                    messages.append(msg_log(agent, "讨论失败，跳过讨论直接进入 S8", "warning", DISCUSSION_STAGE))
                    messages.extend(_skip_discussion_proceed_s8(state, agent))
                else:
                    agent.status = "error"
                    agent.current_task = f"沟通讨论失败 (exit={retcode})"
                    agent.stage_progress[DISCUSSION_STAGE] = "failed"
                    messages.append(msg_agent_update(agent))
                    messages.append(msg_stage_update(agent.id, DISCUSSION_STAGE, "failed"))
        return messages

    consensus_file = Path(group.discussion_output_dir) / "consensus_synthesis.md"
    if not consensus_file.exists():
        messages.append(msg_log(sys_agent, f"讨论 [{group.project_id}] 完成但未产生共识", "warning", DISCUSSION_STAGE))
        group.status = "done"
        for aid in group.agent_ids:
            agent = state.agents.get(aid)
            if agent:
                state.discussion_waiting.pop(aid, None)
                has_project = bool(agent.run_dir and (Path(agent.run_dir) / "stage-07").exists())
                if has_project:
                    messages.extend(_skip_discussion_proceed_s8(state, agent))
                else:
                    _reset_agent_idle(agent)
                    agent.current_stage = 0
                    messages.append(msg_agent_update(agent))
        return messages

    consensus_text = consensus_file.read_text(encoding="utf-8")
    messages.append(msg_log(sys_agent, f"讨论 [{group.project_id}] 完成，共识已生成，启动假设生成", "success", DISCUSSION_STAGE))
    for aid in group.agent_ids:
        agent = state.agents.get(aid)
        if agent:
            agent.stage_progress[DISCUSSION_STAGE] = "completed"
            messages.append(msg_stage_update(agent.id, DISCUSSION_STAGE, "completed"))

    transcript_file = Path(group.discussion_output_dir) / "discussion_transcript.md"
    if transcript_file.exists():
        messages.append(msg_artifact(
            "knowledge", "discussion_transcript.md",
            "沟通讨论", f"{transcript_file.stat().st_size / 1024:.1f} KB",
            group.project_id,
        ))

    group.status = "done"

    # Collect pre-discussion syntheses from all agents for ablation data
    pre_discussion_parts: list[str] = []
    for i, _aid in enumerate(group.agent_ids):
        _ag = state.agents.get(_aid)
        if not _ag:
            continue
        _s7_synth = Path(_ag.run_dir) / "stage-07" / "synthesis.md"
        if _s7_synth.exists():
            _text = _s7_synth.read_text(encoding="utf-8")
            pre_discussion_parts.append(f"## Agent {i+1} ({_aid[:8]})\n\n{_text}")

    for aid in group.agent_ids:
        agent = state.agents.get(aid)
        if not agent:
            continue
        state.discussion_waiting.pop(aid, None)

        has_project = bool(agent.run_dir and (Path(agent.run_dir) / "stage-07").exists())

        if has_project:
            s7_dir = Path(agent.run_dir) / "stage-07"
            s7_dir.mkdir(parents=True, exist_ok=True)
            existing_synthesis = s7_dir / "synthesis.md"
            if existing_synthesis.exists():
                original = existing_synthesis.read_text(encoding="utf-8")
                enriched = (
                    f"{original}\n\n"
                    f"---\n\n"
                    f"# {'Cross-Project' if is_cross else 'Multi-Agent'} Discussion Consensus\n\n"
                    f"{consensus_text}"
                )
                existing_synthesis.write_text(enriched, encoding="utf-8")
            else:
                (s7_dir / "synthesis.md").write_text(consensus_text, encoding="utf-8")

            # Save discussion artifacts for L5 paper ablation study
            disc_artifact_dir = Path(agent.run_dir) / "discussion"
            disc_artifact_dir.mkdir(parents=True, exist_ok=True)
            if pre_discussion_parts:
                (disc_artifact_dir / "pre_discussion_syntheses.md").write_text(
                    "\n\n---\n\n".join(pre_discussion_parts), encoding="utf-8"
                )
            (disc_artifact_dir / "consensus_synthesis.md").write_text(
                consensus_text, encoding="utf-8"
            )
            if transcript_file.exists():
                shutil.copy2(str(transcript_file), str(disc_artifact_dir / "discussion_transcript.md"))

            messages.extend(_launch_s8_for_agent(state, agent, group))
        else:
            _reset_agent_idle(agent)
            agent.current_stage = 0
            agent.stage_progress[DISCUSSION_STAGE] = "completed"
            messages.append(msg_agent_update(agent))
            messages.append(msg_log(agent, "讨论评审完成，恢复空闲", "info", DISCUSSION_STAGE))

    return messages


def _launch_s8_for_agent(state: BridgeState, agent: LobsterAgent, group: DiscussionGroup) -> list[dict]:
    """Launch S8 (HYPOTHESIS_GEN) for a single agent after discussion."""
    messages: list[dict] = []
    fs, ts = LAYER_RANGE_PHASE2["idea"]

    agent.status = "working"
    agent.current_task = f"项目 {group.project_id} · S8 假设生成"
    agent.stage_progress[8] = "running"
    messages.append(msg_agent_update(agent))
    messages.append(msg_stage_update(agent.id, 8, "running"))
    messages.append(msg_log(agent, "沟通讨论完成 → 开始假设生成", "info", 8))

    cmd = [
        state.python_path, "-m", "researchclaw", "run",
        "--config", group.config_path,
        "--output", agent.run_dir,
        "--from-stage", STAGE_NAMES.get(fs, str(fs)),
        "--to-stage", STAGE_NAMES.get(ts, str(ts)),
        "--auto-approve",
    ]
    if group.topic:
        cmd.extend(["--topic", group.topic])

    try:
        log_path = Path(agent.run_dir) / f"agent_{agent.id}_s8.log"
        log_file = open(log_path, "w", encoding="utf-8")
        proc = subprocess.Popen(
            cmd, cwd=state.agent_package_dir,
            stdout=log_file, stderr=subprocess.STDOUT,
            env={**os.environ, "PYTHONUNBUFFERED": "1"},
        )
        agent.process = proc
        agent._expected_from_stage = fs  # type: ignore[attr-defined]
        agent._expected_to_stage = ts  # type: ignore[attr-defined]
        agent._is_discussion_s8 = True  # type: ignore[attr-defined]
        messages.append(msg_log(agent, f"S8 启动 (PID={proc.pid})", "info", 8))
    except Exception as e:
        agent.status = "error"
        agent.current_task = f"S8 启动失败: {e}"
        messages.append(msg_agent_update(agent))
        messages.append(msg_log(agent, f"S8 启动失败: {e}", "error"))

    return messages


def _select_best_hypothesis(state: BridgeState, group: DiscussionGroup) -> str:
    """Select a branch using explicit idea quality and evidence coverage.

    Text length is intentionally excluded: verbosity is not research quality.
    """
    best_id = group.agent_ids[0]
    best_score = -1.0
    scorecards: list[dict[str, object]] = []
    for aid in group.agent_ids:
        rd = group.run_dirs.get(aid, "")
        if not rd:
            continue
        stage8 = Path(rd) / "stage-08"
        quality = _read_json(stage8 / "idea_quality_scores.json") or {}
        novelty = _read_json(stage8 / "novelty_report.json") or {}
        ideas = quality.get("ideas", []) if isinstance(quality, dict) else []
        judge = quality.get("llm_judge", {}) if isinstance(quality, dict) else {}
        judged_ideas = judge.get("ideas", []) if isinstance(judge, dict) else []

        def max_overall(items: object) -> float:
            values = []
            for item in items if isinstance(items, list) else []:
                if isinstance(item, dict):
                    try:
                        values.append(float(item.get("overall", 0) or 0))
                    except (TypeError, ValueError):
                        pass
            return max(values, default=0.0)

        deterministic_quality = max_overall(ideas)
        judge_quality = max_overall(judged_ideas)
        evidence_count = max(
            (int(item.get("evidence_count", 0) or 0) for item in ideas if isinstance(item, dict)),
            default=0,
        )
        coverage = str(novelty.get("search_coverage", "unknown")) if isinstance(novelty, dict) else "unknown"
        retrieved = int(novelty.get("total_papers_retrieved", 0) or 0) if isinstance(novelty, dict) else 0
        coverage_score = 1.0 if coverage == "complete" and retrieved >= 5 else 0.5 if retrieved >= 3 else 0.0
        has_hypothesis = (stage8 / "hypotheses.md").exists()
        score = (
            0.40 * judge_quality
            + 0.30 * deterministic_quality
            + 0.15 * min(5.0, evidence_count / 2.0)
            + 0.15 * (coverage_score * 5.0)
        )
        if not has_hypothesis:
            score = 0.0
        scorecards.append({
            "agent_id": aid,
            "score": round(score, 3),
            "judge_quality": judge_quality,
            "deterministic_quality": deterministic_quality,
            "evidence_count": evidence_count,
            "novelty_search_coverage": coverage,
            "novelty_papers_retrieved": retrieved,
        })
        if score > best_score:
            best_score = score
            best_id = aid
    selected_dir = Path(group.run_dirs.get(best_id, "")) / "stage-08"
    if selected_dir.is_dir():
        _write_json(selected_dir / "idea_selection.json", {
            "selected_agent_id": best_id,
            "selection_score": round(best_score, 3),
            "policy": "quality_evidence_novelty_coverage_v1",
            "scorecards": scorecards,
            "consensus_used_by_all_branches": bool(group.discussion_output_dir),
            "generated": _now_ms(),
        })
        consensus_path = Path(group.discussion_output_dir) / "consensus_synthesis.md"
        if consensus_path.exists():
            shutil.copy2(consensus_path, selected_dir / "discussion_consensus.md")
    return best_id


def _on_discussion_s8_done(state: BridgeState, agent: LobsterAgent) -> list[dict]:
    """Handle S8 completion — wait for all agents, then pick the best hypothesis
    and create only ONE downstream task to avoid duplicate experiments."""
    messages: list[dict] = []
    agent._is_discussion_s8 = False  # type: ignore[attr-defined]

    project_id = agent.project_id
    group = state.discussion_groups.get(project_id)
    if not group:
        for g in state.discussion_groups.values():
            if agent.id in g.agent_ids:
                group = g
                break

    if group:
        group.completed_s8.add(agent.id)
        messages.append(msg_log(
            agent,
            f"S8 完成 ({len(group.completed_s8)}/{len(group.agent_ids)})，等待其他 agent...",
            "info",
        ))

    # Not all agents done yet — park this agent, wait for peers
    if group and not group.all_s8_done():
        _reset_agent_idle(agent)
        messages.append(msg_agent_update(agent))
        return messages

    # All S8 done — pick one hypothesis branch and continue to L2.
    sys_agent = LobsterAgent(id="system", name="系统", layer="idea", run_id="", run_dir="", config_path="")

    selected_agent = agent
    if group:
        best_id = _select_best_hypothesis(state, group)
        maybe_best = state.agents.get(best_id)
        if maybe_best and maybe_best.run_dir and (Path(maybe_best.run_dir) / "stage-08").exists():
            selected_agent = maybe_best
        group.best_agent_id = selected_agent.id

    downstream_project_id = selected_agent.project_id or project_id
    if downstream_project_id:
        proj_dir = state.projects_dir() / downstream_project_id
        meta = _read_project_meta(str(proj_dir)) if proj_dir.exists() else {}
        if isinstance(meta, dict) and meta.get("run_mode") == "idea_gate" and not meta.get("ideas_confirmed"):
            meta["intervention"] = "idea_review:S8 核心想法已生成，请在右侧查看并确认后进入实验设计"
            meta["selected_idea_run_dir"] = selected_agent.run_dir
            meta["selected_idea_config_path"] = selected_agent.config_path
            meta["selected_idea_agent"] = selected_agent.name
            _write_json(proj_dir / "project_meta.json", meta)
            messages.append(msg_log(
                sys_agent,
                f"项目 [{downstream_project_id}] S8 Idea 已生成，已暂停等待确认进入 L2",
                "info",
                8,
            ))
            if group:
                state.discussion_groups.pop(group.project_id, None)
            _reset_agent_idle(agent)
            messages.append(msg_agent_update(agent))
            messages.append(msg_queue_update(state.queues))
            messages.append(msg_project_list(list_all_projects(state)))
            return messages

        follow_task = Task(
            id=f"task-{_uid()}",
            project_id=downstream_project_id,
            run_dir=selected_agent.run_dir,
            config_path=selected_agent.config_path,
            topic=getattr(selected_agent, "_topic", "") or agent.project_id or "",
            source_layer="idea",
            target_layer="experiment",
            created_at=_now_ms(),
        )
        state.queues["idea_to_experiment"].push(follow_task)
        messages.append(msg_log(
            sys_agent,
            f"项目 [{downstream_project_id}] S8 完成，已选择 [{selected_agent.name}] 的假设分支 → 进入 L2 实验设计队列",
            "success",
            8,
        ))
    else:
        messages.append(msg_log(
            sys_agent,
            "S8 完成但未找到有效 project_id，无法进入 L2",
            "error",
            8,
        ))

    if group:
        state.discussion_groups.pop(group.project_id, None)

    # Reset this agent (peers were already reset when they finished earlier)
    _reset_agent_idle(agent)
    messages.append(msg_agent_update(agent))
    messages.append(msg_queue_update(state.queues))
    messages.append(msg_project_list(list_all_projects(state)))

    return messages


def schedule_idle_agents(state: BridgeState) -> list[dict]:
    """Assign pending tasks to idle agents (FIFO pull)."""
    messages: list[dict] = []

    for agent in state.agents.values():
        if agent.status not in ("idle",) or agent.process is not None:
            continue
        if agent.status in ("waiting_discussion", "discussing"):
            continue
        if agent.assigned_task_id:
            continue

        # L4 execution layer: skip if no GPU available
        if agent.layer == "execution" and not state.gpu_allocator.can_allocate():
            execution_queue = state.queues.get(LAYER_INPUT_QUEUE.get("execution", ""))
            waiting_task = execution_queue.peek_pending() if execution_queue else None
            if waiting_task and not waiting_task.waiting_reason:
                waiting_task.waiting_reason = "waiting_for_gpu"
                waiting_task.waiting_since = _now_ms()
                execution_queue.save()
            continue

        # Idea agents pull from init_to_idea, and optionally execution_feedback (auto-loop)
        if agent.layer == "idea":
            candidate_queues = ["init_to_idea"]
            if state.auto_loop:
                candidate_queues.append("execution_feedback")
        else:
            q_name = LAYER_INPUT_QUEUE.get(agent.layer, "")
            candidate_queues = [q_name] if q_name else []

        assigned = False
        for queue_name in candidate_queues:
            queue = state.queues.get(queue_name)
            if not queue:
                continue
            task = queue.peek_pending()
            if not task or task.target_layer != agent.layer:
                continue
            if state._fail_counts.get(task.project_id, 0) >= 3:
                continue

            state._fail_counts.pop(task.project_id, None)  # reset on successful assignment
            queue.assign(task.id, agent.id)
            messages.extend(launch_agent_for_task(state, agent, task))
            messages.append(msg_queue_update(state.queues))
            assigned = True
            break

        # Idea factory: L1 idle with no queued tasks → produce ideas
        if not assigned and agent.layer == "idea" and state.idea_factory_remaining != 0:
            if state.idea_factory_topic and state.idea_factory_config:
                if not state.discussion_mode:
                    messages.extend(_launch_idea_factory_run(state, agent))
                continue

    # Discussion-mode idea factory: batch-launch when 2+ L1 agents are idle
    if state.discussion_mode and state.idea_factory_remaining != 0 and state.idea_factory_topic and state.idea_factory_config:
        idle_idea_agents = [
            a for a in state.agents.values()
            if a.layer == "idea" and a.status == "idle" and a.process is None
            and not a.assigned_task_id
            and a.status not in ("waiting_discussion", "discussing")
        ]
        if len(idle_idea_agents) >= 2:
            batch_id = f"idea-batch-{_uid()}"
            group = DiscussionGroup(
                project_id=batch_id,
                topic=state.idea_factory_topic,
                config_path=state.idea_factory_config,
            )
            models = state.discussion_models
            for i, agent in enumerate(idle_idea_agents[:2]):
                model = models[i % len(models)] if models else ""
                messages.extend(_launch_idea_factory_run(state, agent, s7_only=True, model_override=model))
                group.agent_ids.append(agent.id)
                group.run_dirs[agent.id] = agent.run_dir
                agent._idea_factory_batch_id = batch_id  # type: ignore[attr-defined]
            state.discussion_groups[batch_id] = group
            model_list = ", ".join(models[:2]) if models else "默认"
            sys_agent = LobsterAgent(id="system", name="系统", layer="idea", run_id="", run_dir="", config_path="")
            messages.append(msg_log(
                sys_agent,
                f"Idea 工厂沟通讨论模式: {len(group.agent_ids)} 个 agent 开始独立综合 (S7) — 模型: {model_list}",
                "info", DISCUSSION_STAGE,
            ))

    return messages


# ── WebSocket server ────────────────────────────────────────────────────────

async def broadcast(state: BridgeState, messages: list[dict]):
    if not messages or not state.clients:
        return
    dead = set()
    for msg in messages:
        for ws in list(state.clients):
            safe_msg = _message_for_ws(state, ws, msg)
            if safe_msg is None:
                continue
            data = json.dumps(safe_msg, ensure_ascii=False)
            try:
                await ws.send(data)
            except websockets.ConnectionClosed:
                dead.add(ws)
    state.clients -= dead


def _save_literature_schedules(state: BridgeState) -> None:
    state.schedules_path().parent.mkdir(parents=True, exist_ok=True)
    _write_json(state.schedules_path(), state.literature_schedules)


def _schedule_list_message(state: BridgeState, user_id: str = "") -> dict:
    schedules = [
        item for item in state.literature_schedules
        if not user_id or not item.get("userId") or item.get("userId") == user_id
    ]
    return {"type": "literature_schedule_list", "payload": schedules}


def _scheduled_literature_entries(run_dir: Path) -> list[dict]:
    for filename in ("stage-05/shortlist.jsonl", "stage-04/candidates.jsonl"):
        path = run_dir / filename
        if not path.exists():
            continue
        entries: list[dict] = []
        for line in path.read_text(encoding="utf-8", errors="ignore").splitlines():
            try:
                item = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(item, dict) and item.get("title"):
                entries.append(item)
        if entries:
            return entries
    return []


def _paper_identity(item: dict) -> str:
    return str(
        item.get("paper_id") or item.get("doi") or item.get("arxiv_id")
        or re.sub(r"\W+", "", str(item.get("title", "")).lower())
    ).strip()


def _finalize_literature_schedule(state: BridgeState, schedule: dict) -> bool:
    if schedule.get("status") != "running" or not schedule.get("lastProjectId"):
        return False
    project_id = str(schedule["lastProjectId"])
    run_dir = state.projects_dir() / project_id
    checkpoint = _read_json(run_dir / "checkpoint.json") or {}
    if int(checkpoint.get("last_completed_stage", 0) or 0) < 7:
        project_tasks = [
            task
            for queue in state.queues.values()
            for task in queue.tasks
            if task.project_id == project_id
        ]
        has_failed = any(task.status == "failed" for task in project_tasks)
        has_active = any(task.status in {"pending", "assigned"} for task in project_tasks)
        if has_failed and not has_active:
            completed_at = _now_ms()
            interval_ms = max(1, int(schedule.get("intervalHours", 24) or 24)) * 3600 * 1000
            schedule["status"] = "idle" if schedule.get("enabled", True) else "paused"
            schedule["lastCompletedAt"] = completed_at
            schedule["lastNewPaperCount"] = 0
            schedule["lastError"] = "本周期抓取连续重试后仍失败，已保留任务并安排下个周期自动重试。"
            schedule["nextRunAt"] = max(int(schedule.get("nextRunAt", 0) or 0), completed_at + interval_ms)
            history = schedule.setdefault("history", [])
            history.append({
                "projectId": project_id,
                "completedAt": completed_at,
                "status": "failed",
                "newPaperCount": 0,
                "error": schedule["lastError"],
            })
            schedule["history"] = history[-30:]
            _save_literature_schedules(state)
            return True
        return False

    entries = _scheduled_literature_entries(run_dir)
    seen = set(str(value) for value in schedule.get("seenPaperIds", []) if value)
    new_entries = [item for item in entries if _paper_identity(item) not in seen]
    for item in entries:
        identity = _paper_identity(item)
        if identity:
            seen.add(identity)
    schedule["seenPaperIds"] = list(seen)[-2000:]
    schedule["status"] = "idle" if schedule.get("enabled", True) else "paused"
    schedule["lastCompletedAt"] = _now_ms()
    schedule["lastNewPaperCount"] = len(new_entries)
    schedule.pop("lastError", None)
    interval_ms = max(1, int(schedule.get("intervalHours", 24) or 24)) * 3600 * 1000
    if int(schedule.get("nextRunAt", 0) or 0) <= schedule["lastCompletedAt"]:
        schedule["nextRunAt"] = schedule["lastCompletedAt"] + interval_ms

    synthesis_path = run_dir / "stage-07" / "synthesis.md"
    synthesis = synthesis_path.read_text(encoding="utf-8", errors="ignore") if synthesis_path.exists() else ""
    report_dir = run_dir / "deliverables"
    report_dir.mkdir(parents=True, exist_ok=True)
    report_path = run_dir / "stage-07" / "literature_watch_report.md"
    lines = [
        f"# {schedule.get('name') or schedule.get('topic')}：增量文献综述",
        "",
        f"- 追踪主题：{schedule.get('topic', '')}",
        f"- 本次筛选论文：{len(entries)} 篇",
        f"- 相对历史新增：{len(new_entries)} 篇",
        f"- 执行时间：{time.strftime('%Y-%m-%d %H:%M:%S', time.gmtime())} UTC",
        "",
        "## 本次新增论文",
        "",
    ]
    if new_entries:
        for item in new_entries:
            year = item.get("year", "")
            venue = item.get("venue", "") or item.get("journal", "")
            url = item.get("url", "") or (f"https://doi.org/{item.get('doi')}" if item.get("doi") else "")
            suffix = " · ".join(str(v) for v in (year, venue) if v)
            lines.append(f"- [{item.get('title')}]({url})" + (f" — {suffix}" if suffix else ""))
    else:
        lines.append("- 本周期没有发现未收录的新论文。")
    lines.extend(["", "## Qwen3 文献综述", "", synthesis or "本次未生成有效综述。", ""])
    report_path.write_text("\n".join(lines), encoding="utf-8")
    shutil.copy2(report_path, report_dir / report_path.name)
    schedule["lastReportPath"] = str(report_path)
    history = schedule.setdefault("history", [])
    history.append({
        "projectId": project_id,
        "completedAt": schedule["lastCompletedAt"],
        "paperCount": len(entries),
        "newPaperCount": len(new_entries),
        "reportPath": str(report_path),
    })
    schedule["history"] = history[-30:]
    _save_literature_schedules(state)
    return True


def _dispatch_literature_schedule(state: BridgeState, schedule: dict) -> list[dict]:
    now_ms = _now_ms()
    topic = str(schedule.get("topic", "")).strip()
    keywords = [str(item).strip() for item in schedule.get("keywords", []) if str(item).strip()]
    sources = [str(item).strip() for item in schedule.get("sources", []) if str(item).strip()]
    lookback_days = int(schedule.get("lookbackDays", 30) or 30)
    known = [str(item) for item in schedule.get("seenPaperIds", [])[-40:]]
    prompt_topic = (
        f"定期文献追踪主题：{topic}。重点关键词：{', '.join(keywords) or topic}。"
        f"优先检索最近 {lookback_days} 天公开的相关论文，来源优先级：{', '.join(sources) or 'arXiv, OpenAlex, Semantic Scholar'}。"
        "完成严格去重、相关性筛选、证据卡片抽取，并输出中文增量文献综述；综述需包含研究脉络、方法分类、关键结果、局限、争议和未来研究机会。"
    )
    if known:
        prompt_topic += " 历史已收录标识（尽量避免重复）：" + ", ".join(known)
    stamp = time.strftime("%Y%m%d-%H%M%S", time.gmtime())
    project_id = f"literature-watch-{schedule['id']}-{stamp}-{_uid()}"
    config_path = _generate_config_from_template(
        state, project_id, prompt_topic,
        paper_source_mode="auto", model_name=DEFAULT_QWEN3_MODEL, idea_count=1,
    )
    messages = submit_new_project(
        state, project_id, config_path, prompt_topic,
        mode="scheduled_literature", user_id=str(schedule.get("userId", "")),
        run_mode="literature_watch",
    )
    interval_ms = max(1, int(schedule.get("intervalHours", 24) or 24)) * 3600 * 1000
    schedule.update({
        "lastRunAt": now_ms,
        "nextRunAt": now_ms + interval_ms,
        "lastProjectId": project_id,
        "status": "running",
        "runCount": int(schedule.get("runCount", 0) or 0) + 1,
    })
    _save_literature_schedules(state)
    messages.append(_schedule_list_message(state, str(schedule.get("userId", ""))))
    return messages


def poll_literature_schedules(state: BridgeState) -> list[dict]:
    now = time.monotonic()
    if now < state._schedule_poll_at:
        return []
    state._schedule_poll_at = now + 15.0
    messages: list[dict] = []
    changed_users: set[str] = set()
    for schedule in state.literature_schedules:
        if _finalize_literature_schedule(state, schedule):
            changed_users.add(str(schedule.get("userId", "")))
        if (
            schedule.get("enabled", True)
            and schedule.get("status") != "running"
            and int(schedule.get("nextRunAt", 0) or 0) <= _now_ms()
        ):
            messages.extend(_dispatch_literature_schedule(state, schedule))
    for user in changed_users:
        messages.append(_schedule_list_message(state, user))
    return messages


async def handle_command(state: BridgeState, data: dict, websocket: object | None = None) -> list[dict]:
    cmd = data.get("command")
    messages: list[dict] = []
    user_id = state.user_clients.get(websocket, "") if websocket else ""
    sys_agent = LobsterAgent(id="system", name="系统", layer="idea", run_id="", run_dir="", config_path="")

    # ── 所有权检查：对需要 projectId 的命令验证用户权限 ──
    project_commands = {
        "list_project_artifacts", "list_project_chat", "list_project_literature",
        "delete_project", "pause_project", "resume_project", "restart_project",
        "project_chat", "confirm_project_ideas", "get_download_url",
        "import_literature_batch", "batch_import_literature", "human_feedback",
    }
    if cmd in project_commands and user_id:
        pid = str(data.get("projectId", "")).strip()
        if pid:
            proj_dir = state.projects_dir() / pid
            if proj_dir.exists():
                meta = _read_json(proj_dir / "project_meta.json") or {}
                owner = meta.get("user_id", "")
                if not owner or owner != user_id:
                    messages.append(msg_log(sys_agent, f"无权限操作项目 [{pid}]：该项目不属于当前用户", "error"))
                    return _mark_messages_private(messages, user_id)

    # ── 认证命令 ──
    if cmd == "login":
        username = str(data.get("username", "")).strip()
        password = str(data.get("password", "")).strip()
        if username and password:
            from user_auth import login_user as auth_login
            result = auth_login(username, password, str(state.runs_base_dir))
            if result:
                # 关联 WebSocket 与用户
                if websocket:
                    state.user_clients[websocket] = result["user"]["id"]
                messages.append({"type": "auth_result", "payload": {"ok": True, **result}})
                print(f"  → User [{username}] logged in")
            else:
                messages.append({"type": "auth_result", "payload": {"ok": False, "error": "用户名或密码错误"}})
        else:
            messages.append({"type": "auth_result", "payload": {"ok": False, "error": "请输入用户名和密码"}})
        return messages

    elif cmd == "register":
        username = str(data.get("username", "")).strip()
        password = str(data.get("password", "")).strip()
        if username and password:
            from user_auth import register_user as auth_register
            try:
                user = auth_register(username, password, str(state.runs_base_dir))
                # 注册后自动登录
                from user_auth import login_user as auth_login
                result = auth_login(username, password, str(state.runs_base_dir))
                if result and websocket:
                    state.user_clients[websocket] = result["user"]["id"]
                messages.append({"type": "auth_result", "payload": {"ok": True, **(result or {}), "registered": user}})
                print(f"  → User [{username}] registered and logged in")
            except ValueError as e:
                messages.append({"type": "auth_result", "payload": {"ok": False, "error": str(e)}})
        else:
            messages.append({"type": "auth_result", "payload": {"ok": False, "error": "请输入用户名和密码"}})
        return messages

    elif cmd == "check_auth":
        is_auth = bool(user_id)
        messages.append({"type": "auth_result", "payload": {"ok": True, "authenticated": is_auth, "user_id": user_id or None}})
        return messages

    elif cmd == "logout":
        if websocket:
            state.user_clients.pop(websocket, None)
        messages.append({"type": "auth_result", "payload": {"ok": True, "authenticated": False}})
        return messages

    # ── 定期文献追踪 ──
    if cmd == "list_literature_schedules":
        messages.append(_schedule_list_message(state, user_id))

    elif cmd == "create_literature_schedule":
        topic = str(data.get("topic", "")).strip()
        name = str(data.get("name", "")).strip() or topic
        if not topic:
            return [{"type": "system", "payload": {"message": "定期文献任务必须填写追踪主题"}}]
        raw_keywords = data.get("keywords", [])
        if isinstance(raw_keywords, str):
            raw_keywords = [item.strip() for item in re.split(r"[,，、;；\n]", raw_keywords) if item.strip()]
        if not isinstance(raw_keywords, list):
            raw_keywords = []
        raw_sources = data.get("sources", ["arxiv", "openalex", "semantic_scholar"])
        if not isinstance(raw_sources, list):
            raw_sources = ["arxiv", "openalex", "semantic_scholar"]
        try:
            interval_hours = max(1, min(24 * 30, int(data.get("intervalHours", 24) or 24)))
            lookback_days = max(1, min(365, int(data.get("lookbackDays", 30) or 30)))
        except (TypeError, ValueError):
            interval_hours, lookback_days = 24, 30
        now_ms = _now_ms()
        schedule = {
            "id": f"lit-{_uid()}", "userId": user_id,
            "name": name, "topic": topic,
            "keywords": [str(item).strip() for item in raw_keywords if str(item).strip()][:20],
            "sources": [str(item).strip() for item in raw_sources if str(item).strip()][:8],
            "intervalHours": interval_hours, "lookbackDays": lookback_days,
            "enabled": True, "status": "idle", "createdAt": now_ms,
            "lastRunAt": 0, "nextRunAt": now_ms if data.get("runImmediately", True) else now_ms + interval_hours * 3600 * 1000,
            "runCount": 0, "seenPaperIds": [], "history": [],
        }
        state.literature_schedules.append(schedule)
        _save_literature_schedules(state)
        if schedule["nextRunAt"] <= now_ms:
            messages.extend(_dispatch_literature_schedule(state, schedule))
            messages.extend(schedule_idle_agents(state))
        messages.append(_schedule_list_message(state, user_id))

    elif cmd in {"toggle_literature_schedule", "run_literature_schedule", "delete_literature_schedule"}:
        schedule_id = str(data.get("scheduleId", ""))
        schedule = next((item for item in state.literature_schedules if item.get("id") == schedule_id), None)
        if schedule and user_id and schedule.get("userId") not in ("", user_id):
            schedule = None
        if schedule is None:
            messages.append({"type": "system", "payload": {"message": "未找到该定期文献任务或无权操作"}})
        elif cmd == "delete_literature_schedule":
            state.literature_schedules.remove(schedule)
            _save_literature_schedules(state)
            messages.append(_schedule_list_message(state, user_id))
        elif cmd == "toggle_literature_schedule":
            schedule["enabled"] = bool(data.get("enabled", not schedule.get("enabled", True)))
            if schedule.get("status") != "running":
                schedule["status"] = "idle" if schedule["enabled"] else "paused"
            if schedule["enabled"] and not schedule.get("nextRunAt"):
                schedule["nextRunAt"] = _now_ms()
            _save_literature_schedules(state)
            messages.append(_schedule_list_message(state, user_id))
        elif schedule.get("status") == "running":
            messages.append({"type": "system", "payload": {"message": "该文献任务正在执行，无需重复启动"}})
        else:
            messages.extend(_dispatch_literature_schedule(state, schedule))
            messages.extend(schedule_idle_agents(state))
            messages.append(_schedule_list_message(state, user_id))

    # ── 现有命令 ──
    elif cmd == "list_agents":
        for a in state.agents.values():
            if a.project_id and user_id and _project_owner_id(state, a.project_id) != user_id:
                continue
            messages.append(msg_agent_update(a))
        messages.append(msg_queue_update(state.queues))
        messages.append(msg_project_list(list_all_projects(state, for_user_id=user_id)))

    elif cmd == "add_lobster":
        name = data.get("name", f"龙虾-{_uid()}")
        layer = data.get("layer", "idea")
        agent = create_agent(state, name, layer)
        messages.append(msg_agent_update(agent))
        messages.append(msg_log(agent, f"龙虾已加入 {layer} 层", "info"))

    elif cmd == "remove_lobster":
        agent_id = data.get("agentId")
        agent = state.agents.pop(agent_id, None)
        if agent:
            messages.extend(stop_agent(agent))

    elif cmd == "submit_project":
        project_id = data.get("projectId") or f"proj-{_uid()}"
        config_path = data.get("configPath", "")
        topic = data.get("topic", "")
        messages.extend(submit_new_project(state, project_id, config_path, topic, user_id=user_id))
        messages.extend(schedule_idle_agents(state))
        messages.append(msg_project_list(list_all_projects(state, for_user_id=user_id)))

    elif cmd == "list_projects":
        messages.append(msg_project_list(list_all_projects(state, for_user_id=user_id)))

    elif cmd == "list_project_artifacts":
        project_id = str(data.get("projectId", "")).strip()
        if project_id:
            messages.extend(_list_project_artifact_messages(project_id, state))

    elif cmd == "list_project_chat":
        project_id = str(data.get("projectId", "")).strip()
        if project_id:
            project_dir = state.projects_dir() / project_id
            if project_dir.exists():
                for entry in _load_project_chat_history(project_dir, limit=80):
                    messages.append(_history_entry_to_message(entry))

    elif cmd == "list_project_literature":
        project_id = str(data.get("projectId", "")).strip()
        if project_id:
            project_dir = state.projects_dir() / project_id
            papers: list[dict] = []
            seen: set[str] = set()
            # 引入 CCF 检测
            try:
                from researchclaw.literature.ccf_venues import lookup_ccf_tier
            except ImportError:
                lookup_ccf_tier = lambda v, j="": None  # noqa: E731
            for run_dir in _project_run_dirs(project_dir):
                for stage_dir_name in ("stage-04", "stage-05"):
                    stage_dir = run_dir / stage_dir_name
                    jsonl_file = stage_dir / ("candidates.jsonl" if stage_dir_name == "stage-04" else "shortlist.jsonl")
                    if not jsonl_file.exists():
                        continue
                    try:
                        for line in jsonl_file.read_text(encoding="utf-8", errors="ignore").splitlines():
                            line = line.strip()
                            if not line:
                                continue
                            try:
                                entry = json.loads(line)
                            except json.JSONDecodeError:
                                continue
                            dedup_key = entry.get("paper_id", "") or entry.get("doi", "") or entry.get("title", "")
                            if dedup_key in seen:
                                continue
                            seen.add(dedup_key)
                            authors = entry.get("authors", [])
                            author_names = [a.get("name", "") for a in authors] if isinstance(authors, list) else []
                            venue = str(entry.get("venue", "") or "")
                            journal = str(entry.get("journal", "") or "")
                            ccf_tier = lookup_ccf_tier(venue, journal)  # type: ignore[arg-type]
                            papers.append({
                                "paper_id": str(entry.get("paper_id", "") or entry.get("id", "") or dedup_key),
                                "title": entry.get("title", ""),
                                "authors": author_names,
                                "year": entry.get("year", 0),
                                "venue": venue,
                                "abstract": (entry.get("abstract", "") or "")[:500],
                                "citation_count": entry.get("citation_count", 0),
                                "doi": entry.get("doi", ""),
                                "url": entry.get("url", ""),
                                "source": stage_dir_name,
                                "ccf_tier": ccf_tier or "",
                            })
                    except OSError:
                        continue
            papers.sort(key=lambda p: (
                -{"CCF-A": 3, "CCF-B": 2, "CCF-C": 1}.get(p.get("ccf_tier", ""), 0),
                -p.get("citation_count", 0),
            ))
            messages.append({"type": "literature_list", "payload": {"projectId": project_id, "papers": papers}})

    elif cmd in {"import_literature_batch", "batch_import_literature"}:
        project_id = str(data.get("projectId", "")).strip()
        reference_files = data.get("referenceFiles")
        if not isinstance(reference_files, list):
            reference_files = None
        messages.extend(_import_literature_batch(
            state,
            project_id,
            reference_papers=data.get("referencePapers"),
            reference_uploads=reference_files,
            paper_source_mode=str(data.get("paperSourceMode", data.get("submissionMode", "hybrid"))),
        ))

    elif cmd == "resume_project":
        project_id = data.get("projectId", "")
        if project_id:
            messages.extend(resume_project(state, project_id))
            messages.extend(schedule_idle_agents(state))
            messages.append(msg_project_list(list_all_projects(state, for_user_id=user_id)))

    elif cmd == "quick_submit":
        topic = data.get("topic", "")
        project_id = data.get("projectId", "")
        mode = data.get("mode", "lab")
        submission_mode = data.get("submissionMode", "hybrid")
        angles = data.get("researchAngles")
        if isinstance(angles, str) and angles.strip():
            angles = [a.strip() for a in re.split(r"[,，、;；]", angles) if a.strip()]
        elif not isinstance(angles, list):
            angles = None
        ref_papers = data.get("referencePapers")
        if isinstance(ref_papers, str) and ref_papers.strip():
            ref_papers = [p.strip() for p in re.split(r"[\n,，;；]", ref_papers) if p.strip()]
        elif not isinstance(ref_papers, list):
            ref_papers = None
        reference_files = data.get("referenceFiles")
        if not isinstance(reference_files, list):
            reference_files = None
        model_name = data.get("modelName", "")
        try:
            idea_count = max(1, min(8, int(data.get("ideaCount", 5) or 5)))
        except (TypeError, ValueError):
            idea_count = 5
        run_mode = str(data.get("runMode", "full_chain") or "full_chain").strip().lower()
        if run_mode not in {"full_chain", "idea_gate"}:
            run_mode = "full_chain"
        path_overrides = {
            "codebases_dir": data.get("codebasesDir", ""),
            "datasets_dir": data.get("datasetsDir", ""),
            "checkpoints_dir": data.get("checkpointsDir", ""),
        }
        messages.extend(quick_submit_project(state, topic, project_id, mode, submission_mode, angles, ref_papers, reference_files, path_overrides, model_name, user_id=user_id, idea_count=idea_count, run_mode=run_mode))
        messages.extend(schedule_idle_agents(state))
        messages.append(msg_project_list(list_all_projects(state, for_user_id=user_id)))

    elif cmd == "stop_agent":
        agent_id = data.get("agentId")
        agent = state.agents.get(agent_id)
        if agent:
            messages.extend(stop_agent(agent))

    elif cmd == "get_queues":
        messages.append(msg_queue_update(state.queues))

    elif cmd == "get_shared_results":
        if state.result_registry:
            messages.append({
                "type": "system",
                "payload": {"message": json.dumps(state.result_registry.summary(), ensure_ascii=False)},
            })

    elif cmd == "start_idea_factory":
        state.idea_factory_topic = data.get("topic", "")
        state.idea_factory_config = data.get("configPath", "")
        state.idea_factory_remaining = int(data.get("ideaCount", 0))
        state.idea_factory_produced = 0
        _sys_a = LobsterAgent(id="system", name="系统", layer="idea", run_id="", run_dir="", config_path="")
        label = "无限" if state.idea_factory_remaining == -1 else str(state.idea_factory_remaining)
        messages.append(msg_log(_sys_a, f"Idea 工厂已启动: topic={state.idea_factory_topic[:50]}... count={label}", "info"))

    elif cmd == "stop_idea_factory":
        state.idea_factory_remaining = 0
        _sys_a = LobsterAgent(id="system", name="系统", layer="idea", run_id="", run_dir="", config_path="")
        messages.append(msg_log(_sys_a, f"Idea 工厂已停止 (已产出 {state.idea_factory_produced} 个)", "info"))

    elif cmd == "set_discussion_mode":
        enabled = bool(data.get("enabled", False))
        state.discussion_mode = enabled
        rounds = data.get("rounds")
        if rounds is not None:
            state.discussion_rounds = int(rounds)

    elif cmd == "chat_input":
        content = data.get("content", "").strip()
        target_layer = data.get("targetLayer", "all")
        intent = await _classify_chat_intent(content, state)
        if intent == "query":
            reply = _build_status_summary(state, target_layer, for_user_id=user_id)
            messages.append(msg_feedback_ack(f"qs-{_uid()}", reply, target_layer))
        else:
            data["command"] = "human_feedback"
            messages.extend(await handle_command(state, data))

    elif cmd == "query_status":
        target_layer = data.get("targetLayer", "all")
        reply = _build_status_summary(state, target_layer, for_user_id=user_id)
        messages.append(msg_feedback_ack(f"qs-{_uid()}", reply, target_layer))

    elif cmd == "project_chat":
        project_id = str(data.get("projectId", "")).strip()
        content = str(data.get("content", "")).strip()
        client_message_id = str(data.get("clientMessageId", "")).strip()
        chat_model_name = str(data.get("chatModelName", "")).strip()
        if project_id and content:
            project_dir = state.projects_dir() / project_id
            if project_dir.exists():
                user_entry = _append_project_chat_history(
                    project_dir,
                    "user",
                    content,
                    project_id,
                    message_id=client_message_id,
                )
                messages.append(_history_entry_to_message(user_entry))
            reply = await _answer_project_chat(state, project_id, content, chat_model_name)
            if project_dir.exists():
                assistant_entry = _append_project_chat_history(project_dir, "system", reply, project_id)
                messages.append(_history_entry_to_message(assistant_entry))
            else:
                messages.append(
                    msg_feedback_ack(
                        f"pc-{_uid()}",
                        reply,
                        "project",
                        project_id=project_id,
                    )
                )

    elif cmd == "pause_project":
        project_id = data.get("projectId", "")
        if project_id:
            messages.extend(_pause_project(state, project_id))
            messages.extend(schedule_idle_agents(state))
            messages.append(msg_project_list(list_all_projects(state, for_user_id=user_id)))

    elif cmd == "restart_project":
        project_id = data.get("projectId", "")
        restart_from = str(data.get("restartFrom", "topic") or "topic")
        if project_id:
            messages.extend(_restart_project(state, project_id, restart_from))
            messages.extend(schedule_idle_agents(state))
            messages.append(msg_project_list(list_all_projects(state, for_user_id=user_id)))

    elif cmd == "delete_project":
        project_id = data.get("projectId", "")
        messages.extend(_delete_project(state, project_id))
        state.lab_batches.pop(project_id, None)
        messages.append(msg_project_list(list_all_projects(state, for_user_id=user_id)))

    elif cmd == "confirm_project_ideas":
        project_id = str(data.get("projectId", "")).strip()
        if project_id:
            proj_dir = state.projects_dir() / project_id
            sys_agent = LobsterAgent(id="system", name="系统", layer="idea", run_id="", run_dir="", config_path="")
            if not proj_dir.exists():
                messages.append(msg_log(sys_agent, f"项目 [{project_id}] 不存在", "error"))
            else:
                meta = _read_project_meta(str(proj_dir)) or {}
                intervention = str(meta.get("intervention", ""))
                if not intervention.startswith("idea_review"):
                    messages.append(msg_log(sys_agent, f"项目 [{project_id}] 没有待确认的 Idea", "warning"))
                else:
                    # Mark ideas as confirmed
                    meta.pop("intervention", None)
                    meta["ideas_confirmed"] = True
                    _write_json(proj_dir / "project_meta.json", meta)

                    # Create experiment design task from the selected best idea
                    # branch when available; otherwise fall back to all run-* dirs.
                    selected_run_dir = str(meta.get("selected_idea_run_dir", "") or "")
                    selected_config_path = str(meta.get("selected_idea_config_path", "") or "")
                    angle_dirs = sorted(proj_dir.glob("run-*"))
                    task_count = 0
                    if selected_run_dir and Path(selected_run_dir).is_dir():
                        task = Task(
                            id=f"task-{_uid()}",
                            project_id=project_id,
                            run_dir=selected_run_dir,
                            config_path=selected_config_path or meta.get("config_path", ""),
                            topic=meta.get("topic", ""),
                            source_layer="idea",
                            target_layer="experiment",
                            created_at=_now_ms(),
                        )
                        state.queues["idea_to_experiment"].push(task)
                        task_count += 1
                    elif angle_dirs:
                        for angle_dir in angle_dirs:
                            if not angle_dir.is_dir():
                                continue
                            slug = angle_dir.name.removeprefix("run-")
                            angle_config = str(Path(state.runs_base_dir) / "project_configs" / f"{project_id}--{slug}.yaml")
                            if not Path(angle_config).exists():
                                angle_config = meta.get("config_path", "")
                            task = Task(
                                id=f"task-{_uid()}",
                                project_id=project_id,
                                run_dir=str(angle_dir),
                                config_path=angle_config,
                                topic=f"[{slug}] {meta.get('topic', '')}",
                                source_layer="idea",
                                target_layer="experiment",
                                created_at=_now_ms(),
                            )
                            state.queues["idea_to_experiment"].push(task)
                            task_count += 1
                    else:
                        task = Task(
                            id=f"task-{_uid()}",
                            project_id=project_id,
                            run_dir=str(proj_dir),
                            config_path=meta.get("config_path", ""),
                            topic=meta.get("topic", ""),
                            source_layer="idea",
                            target_layer="experiment",
                            created_at=_now_ms(),
                        )
                        state.queues["idea_to_experiment"].push(task)
                        task_count += 1

                    messages.append(msg_log(
                        sys_agent,
                        f"✓ Idea 已确认！{task_count} 个方向已加入实验设计队列，即将启动 S9",
                        "success",
                    ))
                    messages.append(msg_queue_update(state.queues))
                    messages.extend(schedule_idle_agents(state))
                    messages.append(msg_project_list(list_all_projects(state, for_user_id=user_id)))

    elif cmd == "human_feedback":
        content = data.get("content", "")
        target_layer = data.get("targetLayer", "all")
        message_id = data.get("messageId", f"fb-{_uid()}")

        sys_agent = LobsterAgent(
            id="system", name="系统", layer=target_layer if target_layer != "all" else "idea",
            run_id="", run_dir="", config_path="",
        )
        messages.append(msg_log(sys_agent, f"收到人工反馈: {content[:80]}{'...' if len(content) > 80 else ''}", "info"))

        _save_feedback(state, content, target_layer, message_id, user_id=user_id)

        injected_projects = []
        for agent in state.agents.values():
            if not agent.run_dir or agent.status not in ("working", "idle"):
                continue
            if user_id and (not agent.project_id or _project_owner_id(state, agent.project_id) != user_id):
                continue
            if target_layer != "all" and agent.layer != target_layer:
                continue
            if agent.project_id:
                injected_projects.append(agent.project_id)

        if injected_projects:
            unique = sorted(set(injected_projects))
            plan_hint = (
                f"已将反馈注入 {len(unique)} 个项目的 prompt 上下文中 "
                f"({', '.join(unique)})。"
                f"当前阶段完成后，下一个阶段的 LLM 将读取并参考你的反馈来调整执行计划。"
            )
        else:
            plan_hint = (
                f"已记录反馈。当前无匹配的运行中项目，反馈将在新任务启动时生效。"
            )
        messages.append(msg_feedback_ack(message_id, plan_hint, target_layer))

    elif cmd == "get_download_url":
        project_id = data.get("projectId", "")
        filename = data.get("filename", "latex_package.zip")
        if project_id:
            messages.append({
                "type": "download_url",
                "payload": {
                    "projectId": project_id,
                    "filename": filename,
                    "url": f"/download/{project_id}/{filename}",
                },
            })

    return _mark_messages_private(messages, user_id)


def _scan_existing_artifacts(state: BridgeState, for_user_id: str = "") -> list[dict]:
    """Scan all project directories for completed stage artifacts to send on connect."""
    messages: list[dict] = []
    projects_dir = state.projects_dir()
    if not projects_dir.exists():
        return messages

    for proj_dir in sorted(projects_dir.iterdir()):
        if not proj_dir.is_dir() or proj_dir.name.startswith("_"):
            continue
        # 按用户过滤：已登录用户只能看到自己的项目
        if for_user_id:
            meta = _read_json(proj_dir / "project_meta.json") or {}
            owner = meta.get("user_id", "")
            if not owner or owner != for_user_id:
                continue
        project_id = proj_dir.name

        # Collect all run dirs: project root + any run-* sub-dirs (Lab mode)
        run_dirs: list[Path] = []
        angle_dirs = list(proj_dir.glob("run-*"))
        if angle_dirs:
            run_dirs.extend(d for d in angle_dirs if d.is_dir())
        else:
            run_dirs.append(proj_dir)

        seen: set[str] = set()
        for run_dir in run_dirs:
            for s, outputs in STAGE_OUTPUTS.items():
                stage_dir = run_dir / f"stage-{s:02d}"
                if not stage_dir.is_dir():
                    continue
                for expected in outputs:
                    if expected not in DISPLAY_ARTIFACTS:
                        continue
                    artifact_path = stage_dir / expected.rstrip("/")
                    dedup_key = f"{project_id}:{s}:{expected}"
                    if dedup_key in seen or not artifact_path.exists():
                        continue
                    seen.add(dedup_key)
                    size = "dir" if artifact_path.is_dir() else f"{artifact_path.stat().st_size / 1024:.1f} KB"
                    content = _extract_artifact_summary(artifact_path, expected)
                    messages.append(msg_artifact(
                        REPO_FOR_STAGE.get(s, "knowledge"), expected, project_id, size, project_id, content, stage=s,
                    ))
            discussion_path = run_dir / "discussion" / "discussion_transcript.md"
            dedup_key = f"{project_id}:discussion:discussion_transcript.md"
            if dedup_key not in seen and discussion_path.is_file():
                seen.add(dedup_key)
                size = f"{discussion_path.stat().st_size / 1024:.1f} KB"
                content = _extract_artifact_summary(discussion_path, "discussion_transcript.md")
                messages.append(msg_artifact(
                    "knowledge", "discussion_transcript.md", "沟通讨论", size, project_id, content
                ))
    return messages


async def ws_handler(state: BridgeState, websocket: websockets.ServerConnection):
    print(f"[+] New connection (not yet in clients set)")

    # 读取第一条消息（可以是 auth/login/register，也可以是普通命令）
    first_msg_data: dict | None = None
    try:
        raw = await asyncio.wait_for(websocket.recv(), timeout=10)
        try:
            msg = json.loads(raw)
            if msg.get("command") == "auth":
                token = msg.get("token", "")
                if token:
                    from user_auth import verify_token as verify_auth_token
                    payload = verify_auth_token(token, str(state.runs_base_dir))
                    if payload:
                        user_id = payload.get("user_id", "")
                        username = payload.get("username", "")
                        state.user_clients[websocket] = user_id
                        print(f"  → User [{username}]({user_id}) authenticated")
                        await websocket.send(json.dumps({
                            "type": "auth_result",
                            "payload": {"ok": True, "user": {"id": user_id, "username": username}},
                        }))
                    else:
                        await websocket.send(json.dumps({
                            "type": "auth_result",
                            "payload": {"ok": False, "error": "token 无效或已过期"},
                        }))
                else:
                    await websocket.send(json.dumps({
                        "type": "auth_result",
                        "payload": {"ok": True, "user": None, "message": "未提供 token"},
                    }))
            else:
                # 非 auth 消息，保留给主循环处理
                first_msg_data = msg
        except json.JSONDecodeError:
            pass
    except asyncio.TimeoutError:
        await websocket.send(json.dumps({
            "type": "auth_result",
            "payload": {"ok": True, "user": None, "message": "未认证模式"},
        }))
    except websockets.ConnectionClosed:
        print(f"[-] Connection closed during auth")
        return

    # 未认证的 WebSocket 默认标记为空用户
    if websocket not in state.user_clients:
        state.user_clients[websocket] = ""

    # 现在加入广播列表，让新连接接收后续广播
    state.clients.add(websocket)
    print(f"[+] Client added to broadcast (total: {len(state.clients)})")

    # 发送初始状态
    for agent in state.agents.values():
        if agent.project_id and not _ws_can_see_project(state, websocket, agent.project_id):
            continue
        try:
            await websocket.send(json.dumps(msg_agent_update(agent), ensure_ascii=False))
        except websockets.ConnectionClosed:
            break
    try:
        await websocket.send(json.dumps(msg_queue_update(state.queues), ensure_ascii=False))
    except websockets.ConnectionClosed:
        pass
    try:
        scan_user_id = state.user_clients.get(websocket, "")
        for msg in _scan_existing_artifacts(state, for_user_id=scan_user_id):
            await websocket.send(json.dumps(msg, ensure_ascii=False))
    except websockets.ConnectionClosed:
        pass

    # 先处理第一条消息（如果没被 auth 消费）
    try:
        if first_msg_data is not None:
            responses = await handle_command(state, first_msg_data, websocket)
            has_auth = any(r.get("type") == "auth_result" for r in responses)
            if has_auth:
                for r in responses:
                    await websocket.send(json.dumps(r, ensure_ascii=False))
            else:
                await broadcast(state, responses)
    except websockets.ConnectionClosed:
        pass

    # 再处理后续消息
    try:
        async for raw in websocket:
            try:
                msg_data = json.loads(raw)
            except json.JSONDecodeError:
                continue
            responses = await handle_command(state, msg_data, websocket)
            has_auth = any(r.get("type") == "auth_result" for r in responses)
            if has_auth:
                for r in responses:
                    try:
                        await websocket.send(json.dumps(r, ensure_ascii=False))
                    except websockets.ConnectionClosed:
                        break
            else:
                await broadcast(state, responses)
    except websockets.ConnectionClosed:
        pass
    finally:
        state.clients.discard(websocket)
        state.user_clients.pop(websocket, None)
        print(f"[-] Client disconnected (total: {len(state.clients)})")


async def poll_loop(state: BridgeState, interval: float):
    while True:
        await asyncio.sleep(interval)
        all_messages: list[dict] = []

        for agent in list(state.agents.values()):
            prev_status = agent.status
            msgs = poll_agent(agent)
            all_messages.extend(msgs)

            # Detect layer completion → feed task queue
            if prev_status == "working" and agent.status == "done":
                if getattr(agent, '_is_idea_factory_s7_only', False):
                    all_messages.extend(_on_idea_factory_s7_done(state, agent))
                elif getattr(agent, '_is_idea_factory', False):
                    all_messages.extend(_on_idea_factory_done(state, agent))
                elif getattr(agent, '_is_discussion_s8', False):
                    all_messages.extend(_on_discussion_s8_done(state, agent))
                else:
                    all_messages.extend(on_agent_done(state, agent))

            # Detect failure → mark task failed, release GPU, track retry count
            if prev_status == "working" and agent.status == "error":
                _fail_pid = agent.project_id or "unknown"
                state._fail_counts[_fail_pid] = state._fail_counts.get(_fail_pid, 0) + 1
                _n_fails = state._fail_counts[_fail_pid]
                _MAX_RETRIES = 3

                if agent.assigned_task_id:
                    for q in state.queues.values():
                        q.fail(agent.assigned_task_id)

                # S12 sanity check failure → pause project and notify user
                if agent.layer == "coding" and agent.project_id:
                    _s12_err_msgs = _check_s12_sanity_failure(state, agent)
                    if _s12_err_msgs:
                        all_messages.extend(_s12_err_msgs)
                        continue

                if agent.layer == "execution" and agent.project_id:
                    released = state.gpu_allocator.release(agent.project_id)
                    if released:
                        all_messages.append(msg_log(agent, f"GPU {released} 已释放 (错误后)", "warning"))

                if _n_fails >= _MAX_RETRIES:
                    all_messages.append(msg_log(
                        agent,
                        f"项目 [{_fail_pid}] 连续失败 {_n_fails} 次，已停止自动重试。请检查日志后手动恢复。",
                        "error",
                    ))

                # Clean up discussion group if agent failed
                _batch_id = getattr(agent, '_idea_factory_batch_id', None)
                _disc_key = _batch_id or (agent.project_id if agent.project_id in state.discussion_groups else None)
                if _disc_key and _disc_key in state.discussion_groups:
                    _grp = state.discussion_groups[_disc_key]
                    if agent.id in _grp.agent_ids:
                        _grp.agent_ids.remove(agent.id)
                        _grp.run_dirs.pop(agent.id, None)
                        _grp.completed_s7.discard(agent.id)
                    remaining = [state.agents.get(a) for a in _grp.agent_ids if state.agents.get(a)]
                    waiting = [a for a in remaining if a.status == "waiting_discussion"]
                    if waiting and len(_grp.agent_ids) < 2:
                        sole = waiting[0]
                        all_messages.append(msg_log(
                            sole,
                            f"伙伴 agent 失败，跳过讨论 → 直接进入 S8 假设生成",
                            "warning", DISCUSSION_STAGE,
                        ))
                        sole.stage_progress[DISCUSSION_STAGE] = "skipped"
                        all_messages.append(msg_stage_update(sole.id, DISCUSSION_STAGE, "skipped"))
                        all_messages.extend(_launch_s8_for_agent(state, sole, _grp))
                        _grp.status = "done"
                    elif not remaining:
                        del state.discussion_groups[_disc_key]

                if _n_fails < _MAX_RETRIES:
                    all_messages.extend(_enqueue_checkpoint_retry(state, agent, _n_fails))
                _reset_agent_idle(agent)
                all_messages.append(msg_agent_update(agent))
                all_messages.append(msg_queue_update(state.queues))

        # Poll active discussions
        for group in list(state.discussion_groups.values()):
            all_messages.extend(_poll_discussion(state, group))

        # Persistent periodic literature tracking jobs.
        all_messages.extend(poll_literature_schedules(state))

        # Schedule idle agents
        sched_msgs = schedule_idle_agents(state)
        all_messages.extend(sched_msgs)

        # Periodically broadcast project list (every ~10 poll cycles)
        if not hasattr(state, '_project_list_counter'):
            state._project_list_counter = 0  # type: ignore[attr-defined]
        state._project_list_counter += 1  # type: ignore[attr-defined]
        if state._project_list_counter >= 10:  # type: ignore[attr-defined]
            state._project_list_counter = 0  # type: ignore[attr-defined]
            all_messages.append(msg_project_list(list_all_projects(state)))

        await broadcast(state, all_messages)


# ── Startup ─────────────────────────────────────────────────────────────────

async def main(args: argparse.Namespace):
    state = BridgeState(
        python_path=args.python,
        agent_package_dir=args.agent_dir,
        runs_base_dir=args.runs_dir,
        gpu_allocator=GpuAllocator(args.total_gpus, args.gpus_per_project),
        auto_loop=args.auto_loop,
        discussion_mode=args.discussion_mode,
        discussion_rounds=args.discussion_rounds,
        discussion_models=[
            _coerce_qwen3_model(m)
            for m in args.discussion_models.split(",")
            if m.strip()
        ] or [DEFAULT_QWEN3_MODEL],
        idea_factory_topic=args.idea_topic,
        idea_factory_config=args.idea_config,
        idea_factory_remaining=args.idea_count,
    )

    # Initialize shared results registry
    _shared_results_path = Path(state.runs_base_dir).parent / "shared_results"
    try:
        from result_registry import ResultRegistry
        state.result_registry = ResultRegistry(str(_shared_results_path))
    except Exception:
        pass

    state.projects_dir().mkdir(parents=True, exist_ok=True)
    state.queues_dir().mkdir(parents=True, exist_ok=True)
    raw_schedules = _read_json(state.schedules_path())
    if isinstance(raw_schedules, list):
        state.literature_schedules = [item for item in raw_schedules if isinstance(item, dict)]

    # Initialize queues (load from disk, clean stale tasks from prior run)
    _completed_projects: set[str] = set()
    for _pd in state.projects_dir().iterdir():
        if _pd.is_dir() and not _pd.name.startswith("_"):
            _cp = _read_json(_pd / "checkpoint.json")
            if _cp and _cp.get("last_completed_stage", 0) >= 26:
                _completed_projects.add(_pd.name)
    for queue_name in list(QUEUE_NAMES.keys()) + ["init_to_idea"]:
        q = TaskQueue(name=queue_name, path=state.queues_dir() / f"{queue_name}.json")
        q.load()
        _stale = 0
        _cleaned = 0
        for t in q.tasks:
            if t.status == "assigned":
                if t.run_dir and _run_dir_has_live_heartbeat(Path(t.run_dir)):
                    continue
                # The bridge may have restarted while the subprocess was
                # healthy. Keep the durable task schedulable so it resumes
                # from checkpoint instead of becoming a permanent failure.
                t.status = "pending"
                t.assigned_to = None
                t.assigned_at = 0
                _stale += 1
        orig_len = len(q.tasks)
        q.tasks = [t for t in q.tasks if not (
            t.project_id in _completed_projects and t.status in ("pending", "assigned", "failed")
        )]
        _cleaned = orig_len - len(q.tasks)
        if _stale or _cleaned:
            q.save()
        if _stale:
            print(f"   [queue] {queue_name}: requeued {_stale} stale assigned task(s)")
        if _cleaned:
            print(f"   [queue] {queue_name}: removed {_cleaned} task(s) for completed projects")
        state.queues[queue_name] = q

    _recovered_orphans = _recover_orphaned_inflight_projects(state)
    if _recovered_orphans:
        print(f"   [recovery] requeued {_recovered_orphans} orphaned in-flight project(s)")

    # Create default lobster pool (configurable via --pool)
    pool_sizes = {"idea": args.pool_idea, "experiment": args.pool_exp,
                  "coding": args.pool_code, "execution": args.pool_exec,
                  "writing": args.pool_write}
    pool_names = {"idea": "L1", "experiment": "L2", "coding": "L3", "execution": "L4", "writing": "L5"}
    default_pool = []
    for layer, count in pool_sizes.items():
        for i in range(count):
            tag = chr(ord('A') + i) if count > 1 else ""
            default_pool.append((f"{pool_names[layer]}·{tag}".rstrip("·"), layer))
    for name, layer in default_pool:
        create_agent(state, name, layer)

    _adopted_processes = _adopt_live_subprocesses(state)
    if _adopted_processes:
        print(f"   [recovery] adopted {_adopted_processes} live subprocess(es)")

    queued_tasks = sum(q.pending_count() for q in state.queues.values())

    print(f"🦞 Agent Bridge v2 starting on ws://0.0.0.0:{args.port}")
    print(f"   Agent package: {args.agent_dir}")
    print(f"   Runs base:     {args.runs_dir}")
    print(f"   Python:        {args.python}")
    print(f"   Lobsters:      {len(state.agents)}")
    print(f"   GPUs:          {args.total_gpus}x ({args.gpus_per_project}/project, max {args.total_gpus // max(args.gpus_per_project, 1)} parallel)")
    print(f"   Auto-loop:     {'ON' if args.auto_loop else 'OFF'}")
    _disc_info = f"ON ({args.discussion_rounds} rounds, models: {args.discussion_models})" if args.discussion_mode else "OFF"
    print(f"   Discussion:    {_disc_info}")
    print(f"   Queued tasks:  {queued_tasks}")
    print()

    def _make_process_request(st: BridgeState):
        """Create HTTP request handler for file downloads."""
        from http import HTTPStatus
        from websockets.http11 import Response as WSResponse

        def _http_response(status_code: int, body: bytes, content_type: str = "text/plain",
                           extra_headers: dict | None = None) -> WSResponse:
            reason = HTTPStatus(status_code).phrase
            headers = {"Content-Type": content_type, "Content-Length": str(len(body)),
                        "Access-Control-Allow-Origin": "*"}
            if extra_headers:
                headers.update(extra_headers)
            return WSResponse(status_code, reason, websockets.Headers(headers), body)

        async def process_request(connection, request):
            if request.path.startswith("/download/"):
                from urllib.parse import unquote
                parts = unquote(request.path[len("/download/"):]).split("/", 1)
                if len(parts) < 2:
                    return _http_response(404, b"Not found\n")
                project_id, filename = parts[0], parts[1]
                proj_dir = Path(st.runs_base_dir) / "projects" / project_id
                file_path = None
                search_roots: list[Path] = []
                if proj_dir.is_dir():
                    search_roots.append(proj_dir)
                cross_proj_dir = Path(st.runs_base_dir) / "projects" / "_cross_discussions" / project_id
                if cross_proj_dir.is_dir():
                    search_roots.append(cross_proj_dir)
                if not search_roots:
                    return _http_response(404, f"Project {project_id} not found\n".encode())

                for root_dir in search_roots:
                    candidate = root_dir / "deliverables" / filename
                    if candidate.is_file():
                        file_path = candidate
                        break
                    for deliverables_dir in sorted(root_dir.glob("run-*/deliverables"), reverse=True):
                        candidate = deliverables_dir / filename
                        if candidate.is_file():
                            file_path = candidate
                            break
                    if file_path:
                        break
                    for stage_dir in sorted(root_dir.glob("run-*/stage-*"), reverse=True):
                        candidate = stage_dir / filename
                        if candidate.is_file():
                            file_path = candidate
                            break
                    if file_path:
                        break
                    for discussion_dir in sorted(root_dir.glob("run-*/discussion"), reverse=True):
                        candidate = discussion_dir / filename
                        if candidate.is_file():
                            file_path = candidate
                            break
                    if file_path:
                        break
                    candidate = root_dir / "discussion" / filename
                    if candidate.is_file():
                        file_path = candidate
                        break
                if not file_path:
                    return _http_response(404, f"File {filename} not found\n".encode())
                try:
                    data = file_path.read_bytes()
                    return _http_response(200, data, "application/octet-stream",
                                          {"Content-Disposition": f'attachment; filename="{file_path.name}"'})
                except Exception as e:
                    return _http_response(500, f"Error: {e}\n".encode())
            return None
        return process_request

    handler = lambda ws: ws_handler(state, ws)
    async with websockets.serve(
        handler, "0.0.0.0", args.port,
        process_request=_make_process_request(state),
        max_size=64 * 1024 * 1024,
    ):
        await poll_loop(state, args.interval)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Agent Bridge v2")
    parser.add_argument("--port", type=int, default=8766)
    parser.add_argument("--interval", type=float, default=2.0)
    parser.add_argument("--python", default=sys.executable)
    parser.add_argument("--agent-dir",
                        default=str(Path(__file__).resolve().parent.parent / "agent"))
    parser.add_argument("--runs-dir",
                        default=str(Path(__file__).resolve().parent.parent / "runs"))
    parser.add_argument("--pool-idea", type=int, default=2)
    parser.add_argument("--pool-exp", type=int, default=2)
    parser.add_argument("--pool-code", type=int, default=3)
    parser.add_argument("--pool-exec", type=int, default=4)
    parser.add_argument("--pool-write", type=int, default=2)
    parser.add_argument("--total-gpus", type=int, default=8,
                        help="Total number of GPUs available")
    parser.add_argument("--gpus-per-project", type=int, default=2,
                        help="GPUs allocated per project in execution layer")
    parser.add_argument("--auto-loop", action="store_true", default=False,
                        help="Enable auto-loop: L4 completion feeds back to L1 for new research cycle")
    parser.add_argument("--discussion-mode", action="store_true", default=True,
                        help="Enable L1 discussion: agents discuss after S7 before generating hypotheses")
    parser.add_argument("--no-discussion-mode", action="store_false", dest="discussion_mode",
                        help="Disable L1 discussion mode")
    parser.add_argument("--discussion-rounds", type=int, default=3,
                        help="Number of LLM discussion rounds (default: 3)")
    parser.add_argument("--discussion-models", default=DEFAULT_QWEN3_MODEL,
                        help="Comma-separated Qwen3 models for discussion agents")
    parser.add_argument("--idea-count", type=int, default=0,
                        help="Idea factory: number of ideas to produce (0=disabled, -1=infinite)")
    parser.add_argument("--idea-topic", default="",
                        help="Idea factory: research topic for idea generation")
    parser.add_argument("--idea-config", default="",
                        help="Idea factory: config file path")
    args = parser.parse_args()
    asyncio.run(main(args))
