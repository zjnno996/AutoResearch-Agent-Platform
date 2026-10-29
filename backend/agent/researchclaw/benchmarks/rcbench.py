"""EMNETS-RA adapter for the unmodified official ResearchClawBench scorer."""

from __future__ import annotations

import hashlib
import importlib
import importlib.util
import json
import math
import os
import re
import shlex
import shutil
import signal
import subprocess
import sys
import time
import uuid
from dataclasses import asdict, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from researchclaw.config import RCConfig
from researchclaw.orchestration.artifacts import read_json, write_json

UPSTREAM = "https://github.com/InternScience/ResearchClawBench"
INSPECTED_REVISION = "7627954d0ff3fe5496cbc0675f6afbe9b6cd40f9"
AGENT_ID = "claw_ai_lab"


def source_identity() -> str:
    root = Path(__file__).resolve().parents[1]
    digest = hashlib.sha256()
    for path in sorted(root.rglob("*")):
        relative = path.relative_to(root)
        if (not path.is_file() or "__pycache__" in relative.parts
                or relative.parts[:2] == ("skills", "library")
                or path.suffix not in (".py", ".yaml", ".yml", ".json", ".md")):
            continue
        digest.update(relative.as_posix().encode() + b"\0" + path.read_bytes())
    return digest.hexdigest()


def _label(config: RCConfig) -> str:
    return f"EMNETS-RA ({source_identity()[:10]}-{settings_identity(config)[:6]}, {config.llm.primary_model})"


def settings_identity(config: RCConfig) -> str:
    from researchclaw.llm.provider_policy import deployment_policy_identity
    settings = {"engine": asdict(config.research_engine), "experiment": asdict(config.experiment),
                'deployment_policy': deployment_policy_identity(),
                "model": {key: value for key, value in asdict(config.llm).items()
                          if key not in ("api_key", "api_key_env")},
                "research_policy": {key: value for key, value in asdict(config.research).items()
                                    if key not in ("topic", "reference_papers", "domains")}}
    return hashlib.sha256(json.dumps(settings, sort_keys=True, default=str).encode()).hexdigest()


def revision(repo: Path) -> str:
    result = subprocess.run(["git", "-C", str(repo), "rev-parse", "HEAD"],
                            capture_output=True, text=True, timeout=10)
    return result.stdout.strip() if result.returncode == 0 else "unknown"


def _official(repo: Path, module: str) -> Any:
    repo = repo.resolve()
    if not (repo / "evaluation/run_task.py").is_file():
        raise ValueError("Expected an official ResearchClawBench checkout")
    loaded = sys.modules.get("evaluation")
    if loaded and not Path(loaded.__file__).resolve().is_relative_to(repo):
        raise ValueError("Another evaluation package is already imported")
    if str(repo) not in sys.path:
        sys.path.insert(0, str(repo))
    return importlib.import_module(f"evaluation.{module}")


def _task_path(repo: Path, task_id: str) -> Path:
    if not re.fullmatch(r"[A-Za-z]+_\d{3}", task_id):
        raise ValueError("Expected a task ID such as Math_001")
    path = repo.resolve() / "tasks" / task_id
    if path.is_symlink() or not (path / "task_info.json").is_file():
        raise ValueError(f"Task is not downloaded: {task_id}")
    return path


def _manifest(root: Path) -> dict[str, str]:
    if not root.is_dir():
        return {}
    files = {}
    for path in sorted(root.rglob("*")):
        if path.is_file():
            if path.is_symlink() and not path.resolve().is_relative_to(root.resolve()):
                raise ValueError("Input symlink leaves its declared directory")
            files[path.relative_to(root).as_posix()] = hashlib.sha256(path.read_bytes()).hexdigest()
    return files


def doctor(repo: Path, config_path: Path, task_id: str | None = None) -> dict[str, Any]:
    config = RCConfig.load(config_path, check_paths=False)
    from researchclaw.llm.client import LLMClient
    client = LLMClient.from_rc_config(config)
    repo = repo.resolve()
    tasks = sorted(path.parent.name for path in (repo / "tasks").glob("*/task_info.json") if path.is_file())
    selected = _task_path(repo, task_id) if task_id else None
    judge_vars = {key: bool(os.environ.get(key)) for key in ("JUDGE_API_KEY", "JUDGE_API_BASE", "JUDGE_MODEL_NAME")}
    scorer = importlib.util.find_spec("structai") is not None
    has_agent = bool(client.config.api_key and client.config.base_url)
    has_inputs = bool(selected and (selected / "data").is_dir()) if task_id else bool(tasks)
    return {"schema_version": "claw-rcbench-doctor-v1", "upstream": UPSTREAM,
            "benchmark_revision": revision(repo), "inspected_revision": INSPECTED_REVISION,
            "downloaded_tasks": tasks, "selected_task": task_id, "task_inputs_available": has_inputs,
            "agent_model": client.config.primary_model, "agent_credentials_configured": has_agent,
            "agent_connectivity": "not_probed", "judge_environment": judge_vars,
            "scorer_dependency_available": scorer,
            "ready_for_preparation": (repo / "evaluation/run_task.py").is_file() and has_inputs,
            "configuration_ready_for_scoring": scorer and all(judge_vars.values()),
            "note": "Configuration presence is not a successful model probe or a benchmark score."}


def register(repo: Path, config_path: Path) -> dict[str, Any]:
    config = RCConfig.load(config_path, check_paths=False)
    path = repo / "evaluation/agents.json"
    presets = read_json(path)
    package_root = Path(__file__).resolve().parents[2]
    command = (f"env PYTHONPATH={shlex.quote(str(package_root.parent))} {shlex.quote(sys.executable)} "
               f"-m researchclaw rcbench agent --config {shlex.quote(str(config_path.resolve()))} "
               "--workspace <WORKSPACE> --prompt <PROMPT>")
    presets[AGENT_ID] = {"label": _label(config), "icon": "EM", "cmd": command}
    write_json(path, presets)
    return {"agent_id": AGENT_ID, "preset_file": str(path), "benchmark_revision": revision(repo),
            "status": "registered_locally", "official_submission": False}


def prepare(repo: Path, task_id: str, config_path: Path) -> Path:
    task = _task_path(repo, task_id)
    if not (task / "data").is_dir():
        raise ValueError("Task data directory is missing")
    config = RCConfig.load(config_path, check_paths=False)
    runner = _official(repo, "run_task").TaskRunner(task_id, agent_cmd="EMNETS-RA native adapter", agent_name=_label(config))
    run_id = f"{task_id}_claw_{datetime.now(timezone.utc):%Y%m%d_%H%M%S}_{uuid.uuid4().hex[:8]}"
    workspace = repo.resolve() / "workspaces" / run_id
    runner.run_id, runner.workspace = run_id, workspace
    runner.meta_path, runner.output_path = workspace / "_meta.json", workspace / "_agent_output.jsonl"
    runner.instructions_path = workspace / "INSTRUCTIONS.md"
    runner.setup_workspace()
    runner._write_meta("prepared", {"model": config.llm.primary_model})
    # The official runner copies data and related_work only. Evaluation-only
    # files stay in the separate benchmark task checkout.
    if (workspace / "target_study").exists():
        raise ValueError("Evaluation-only inputs must not enter the agent workspace")
    contract = task_contract((workspace / "INSTRUCTIONS.md").read_text(encoding="utf-8"))
    write_json(workspace / "_claw_task_contract.json", contract)
    write_json(workspace / "_claw_benchmark.json", {
        "schema_version": "claw-rcbench-run-v1", "upstream": UPSTREAM,
        "benchmark_revision": revision(repo), "task_id": task_id, "run_id": run_id,
        "model": config.llm.primary_model, "profile": config.research_engine.profile,
        "agent_source_sha256": source_identity(), "agent_name": _label(config),
        "agent_settings_sha256": settings_identity(config),
        "task_contract_sha256": hashlib.sha256(json.dumps(contract, sort_keys=True).encode()).hexdigest(),
        "instructions_sha256": hashlib.sha256((workspace / "INSTRUCTIONS.md").read_bytes()).hexdigest(),
        "task_instructions_sha256": hashlib.sha256((workspace / "INSTRUCTIONS.md").read_text(encoding="utf-8").replace(
            str(workspace.resolve()), "{WORKSPACE}").encode()).hexdigest(),
        "data_manifest": _manifest(workspace / "data"),
        "related_work_manifest": _manifest(workspace / "related_work"),
        "official_submission": False,
    })
    return workspace


def task_contract(instructions: str) -> dict[str, str]:
    """Keep the scientific task separate from the outer ReAct session policy.

    The native controller owns completion and export. Individual planning and
    review calls have no tools, and coding calls have a stage-local workspace;
    forwarding the session's "always call a tool" policy corrupts both.
    The complete official instructions remain unchanged in INSTRUCTIONS.md.
    """
    text = instructions.replace("\r\n", "\n")
    task = re.search(r"(?ms)^## Research Task[ \t]*\n(.*?)^## Execution Protocol[ \t]*$", text)
    if task:
        workspace = re.search(r"(?m)^## Workspace[ \t]*$", text)
        if workspace is None or workspace.start() < task.end():
            raise ValueError("Unsupported official instruction layout: missing workspace contract")
        delivery = re.search(r"(?ms)^### Deliverables[ \t]*\n(.*?)^### Technical Notes[ \t]*$", text[workspace.end():])
        if delivery is None:
            raise ValueError("Unsupported official instruction layout: missing deliverables")
        scientific = task.group(1).strip().removesuffix("---").rstrip()
        if not scientific:
            raise ValueError("Official scientific task is empty")
        # Preserve delivery requirements verbatim, with their scope made clear.
        # The top-level workspace path and ReAct policy never become a topic.
        topic = (scientific + "\n\n## Final deliverables assembled by the pipeline controller\n"
                 "The following paths are final export destinations. Follow the current "
                 "stage's output format and working directory; the controller assembles "
                 "these deliverables after all stages complete.\n\n" + delivery.group(1).strip())
        return {"schema_version": "claw-rcbench-task-contract-v1", "research_topic": topic,
                "scientific_request": scientific, "workspace_contract": text[workspace.start():],
                "execution_protocol_scope": "native_controller_only"}
    if re.search(r"(?m)^## (?:Role|Execution Protocol)[ \t]*$", text):
        raise ValueError("Unsupported official instruction layout: cannot isolate scientific task")
    # Plain task prompts remain supported by file-based integrations and tests.
    return {"schema_version": "claw-rcbench-task-contract-v1", "research_topic": instructions,
            "scientific_request": instructions, "workspace_contract": "",
            "execution_protocol_scope": "plain_task"}


def task_config(config: RCConfig, workspace: Path) -> RCConfig:
    instructions = (workspace / "INSTRUCTIONS.md").read_text(encoding="utf-8")
    contract = task_contract(instructions)
    references = tuple(str(path) for path in sorted((workspace / "related_work").rglob("*.pdf")))
    supplied_data = any(path.is_file() for path in (workspace / "data").rglob("*"))
    return replace(config,
        project=replace(config.project, name=workspace.name, mode="full-auto"),
        research=replace(config.research, topic=contract["research_topic"], domains=(), reference_papers=references,
                         reference_purpose="background"),
        experiment=replace(config.experiment, datasets_dir=str(workspace / "data"),
                           codebases_dir="", checkpoints_dir="", shared_results_dir=str(workspace / ".claw/shared"),
                           benchmark_agent=replace(config.experiment.benchmark_agent,
                               enabled=config.experiment.benchmark_agent.enabled and not supplied_data)),
        knowledge_base=replace(config.knowledge_base, root=str(workspace / ".claw/kb")),
        research_engine=replace(config.research_engine, shared_memory=False),
        security=replace(config.security, hitl_required_stages=()),
    )


_IMAGE = re.compile(r"!\[([^\]]*)\]\(([^)]+)\)")


def export_report(run_dir: Path, workspace: Path) -> dict[str, Any]:
    candidates = (run_dir / "stage-26/paper_final_verified.md", run_dir / "stage-25/paper_final.md")
    compact = read_json(run_dir / "compact_state.json").get("workflow") == "compact"
    if compact and read_json(run_dir / "compact_state.json").get("phases", {}).get("verify", {}).get("status") != "done":
        candidates = (run_dir / "stage-25/paper_final.md",)
    source = next((path for path in candidates if path.is_file() and path.stat().st_size), None)
    if source is None:
        raise ValueError("Pipeline did not produce a final research report")
    report_dir = workspace / "report"
    image_dir = report_dir / "images"
    image_dir.mkdir(parents=True, exist_ok=True)
    missing, copied = [], []
    roots = (source.parent, run_dir / "deliverables", run_dir / "stage-16", run_dir / "stage-14")
    def image_reference(match):
        original = match.group(2).strip().strip("<>").split(' "', 1)[0]
        relative = Path(original)
        if re.match(r"^https?://", original):
            missing.append(original)
            return match.group(0)
        choices = [base / relative for base in roots] + [base / "charts" / relative.name for base in roots]
        path = next((item for item in choices if item.is_file() and item.resolve().is_relative_to(run_dir.resolve())), None)
        if path is None:
            missing.append(original)
            return match.group(0)
        digest = hashlib.sha256(path.read_bytes()).hexdigest()[:16]
        destination = image_dir / f"{path.stem}_{digest}.png"
        if path.suffix.lower() == ".png":
            shutil.copy2(path, destination)
        else:
            from PIL import Image
            with Image.open(path) as img:
                img.convert("RGB").save(destination, format="PNG")
        copied.append(destination.name)
        return f"![{match.group(1)}](images/{destination.name})"
    text = _IMAGE.sub(image_reference, source.read_text(encoding="utf-8"))
    (report_dir / "report.md").write_text(text, encoding="utf-8")
    scientific_analysis = run_dir / "stage-25/scientific_analysis.md"
    if scientific_analysis.is_file():
        shutil.copy2(scientific_analysis, report_dir / "scientific_analysis.md")
    code_roots = ((run_dir / "stage-11/experiment",) if compact else
                  (run_dir / "stage-15/experiment_final", run_dir / "stage-11/experiment"))
    code = next((path for path in code_roots if path.is_dir()), None)
    if code:
        if (code / '.source_manifest.json').is_file():
            from researchclaw.orchestration.compact_generation import source_files
            for path in source_files(code):
                target = workspace / 'code' / path.relative_to(code)
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(path, target)
        else:
            shutil.copytree(code, workspace / "code", dirs_exist_ok=True,
                            ignore=shutil.ignore_patterns("__pycache__", ".snapshots", "data", "outputs"))
        (workspace / "outputs").mkdir(exist_ok=True)
        if (code / "outputs").is_dir():
            shutil.copytree(code / "outputs", workspace / "outputs", dirs_exist_ok=True)
        for name in ("data", "outputs"):
            alias = workspace / "code" / name
            if (workspace / name).is_dir() and not alias.exists() and not alias.is_symlink():
                alias.symlink_to(Path("..") / name, target_is_directory=True)
    for name in ("experiment_summary.json", "experiment_provenance.json", "evaluation_protocol_audit.json"):
        source_json = run_dir / "stage-16" / name
        if source_json.is_file():
            (workspace / "outputs").mkdir(exist_ok=True)
            shutil.copy2(source_json, workspace / "outputs" / name)
    result = {"report": "report/report.md", "images": sorted(set(copied)), "missing_images": missing,
              "deliverables_complete": bool(copied) and not missing and code is not None}
    write_json(workspace / "_claw_export.json", result)
    return result


def run_agent(workspace: Path, config_path: Path, prompt: str | None = None, *, resume: bool = False) -> int:
    workspace = workspace.resolve()
    if not (workspace / "INSTRUCTIONS.md").is_file() or (workspace / "target_study").exists():
        raise ValueError("Expected a prepared agent workspace without target_study")
    instructions = (workspace / "INSTRUCTIONS.md").read_text(encoding="utf-8")
    if prompt is not None and prompt != instructions:
        raise ValueError("Prompt must match the benchmark's INSTRUCTIONS.md")
    base = RCConfig.load(config_path, check_paths=False)
    config = task_config(base, workspace)
    manifest = read_json(workspace / "_claw_benchmark.json")
    if manifest.get("instructions_sha256") != hashlib.sha256((workspace / "INSTRUCTIONS.md").read_bytes()).hexdigest():
        raise ValueError("Official instructions changed since preparation")
    for directory, field in (("data", "data_manifest"), ("related_work", "related_work_manifest")):
        if manifest.get(field) != _manifest(workspace / directory):
            raise ValueError(f"Benchmark {directory} changed since preparation")
    contract = task_contract(instructions)
    if manifest.get("task_contract_sha256") != hashlib.sha256(json.dumps(contract, sort_keys=True).encode()).hexdigest():
        raise ValueError("Task projection changed since preparation; prepare a fresh benchmark attempt")
    if manifest.get("agent_source_sha256") != source_identity():
        raise ValueError("Agent source changed since preparation; prepare a fresh benchmark attempt")
    if manifest.get("model") != config.llm.primary_model:
        raise ValueError("Agent model changed since preparation; prepare a fresh benchmark attempt")
    if manifest.get("agent_settings_sha256") != settings_identity(base):
        raise ValueError("Agent settings changed since preparation; prepare a fresh benchmark attempt")
    from researchclaw.benchmarks.resume import validate_resume
    validate_resume(workspace, resume, config.research_engine.max_model_calls)
    from researchclaw.llm import create_llm_client
    ok, _ = create_llm_client(config).preflight()
    if not ok:
        raise ValueError("Agent model preflight failed; no benchmark run was started")
    from researchclaw.adapters import AdapterBundle
    from researchclaw.pipeline.runner import execute_pipeline
    from researchclaw.pipeline.stages import StageStatus
    from researchclaw.orchestration.lease import run_lease
    with run_lease(workspace / ".claw", workspace.name):
        continuation = validate_resume(workspace, resume, config.research_engine.max_model_calls)
        history_path = workspace / '.claw/resume_history.json'
        history = read_json(history_path)
        if resume:
            continuation.update(started_at=datetime.now(timezone.utc).isoformat(), status='running',
                                agent_source_sha256=manifest['agent_source_sha256'],
                                agent_settings_sha256=manifest['agent_settings_sha256'])
            history.setdefault('events', []).append(continuation)
            write_json(history_path, history)
        invocation_started = time.monotonic()
        before = {name: _manifest(workspace / name) for name in ("data", "related_work")}
        print(f"model: {config.llm.primary_model}", flush=True)
        meta = read_json(workspace / "_meta.json")
        meta.update(status="running", model=config.llm.primary_model, pid=os.getpid())
        if resume:
            meta.update(resume_count=len(history['events']), resume_history='.claw/resume_history.json', latency_comparable=False)
        write_json(workspace / "_meta.json", meta)
        try:
            results = execute_pipeline(run_dir=workspace / ".claw/run", run_id=workspace.name,
                                       config=config, adapters=AdapterBundle(), auto_approve_gates=True)
            if source_identity() != manifest.get("agent_source_sha256"):
                raise ValueError("Agent source changed during research; retain this attempt as invalid and prepare a fresh one")
            partial = False
            if results and results[-1].status == StageStatus.FAILED:
                state = read_json(workspace / ".claw/run/compact_state.json")
                failed = next((name for name, row in state.get("phases", {}).items() if row.get("status") == "failed"), None)
                meta.update(status="failed", failed_stage=int(results[-1].stage), failed_phase=failed,
                            error=results[-1].error,
                            failure_kind=state.get("phases", {}).get(failed, {}).get("failure_kind", "stage_failure"))
                write_json(workspace / "_meta.json", meta)
                if (int(results[-1].stage) == 26 and results[-1].error ==
                        "Scientific review has unresolved requirements; see compact_review.json"):
                    from researchclaw.benchmarks.partial_recovery import retained_review_audit
                    audit = retained_review_audit(workspace / ".claw/run")
                    write_json(workspace / ".claw/run/retained_partial_delivery.json", audit)
                    partial = audit["eligible"]
                    if partial and read_json(workspace / ".claw/run/compact_retention.json").get("status") == "retained":
                        meta.update(partial_delivery_reason="scientific_regression_retained_prior_reviewed_bundle",
                                    revision_status="regressed_candidate_retained_original")
                        write_json(workspace / "_meta.json", meta)
                elif (int(results[-1].stage) in {13, 25}
                      and state.get('phases', {}).get(failed, {}).get('error', '').startswith('ContractError:')):
                    from researchclaw.benchmarks.partial_recovery import retained_review_audit
                    retained = retained_review_audit(workspace / '.claw/run')
                    write_json(workspace / '.claw/run/retained_partial_delivery.json', retained)
                    partial = retained['eligible']
                    if partial:
                        meta.update(partial_delivery_reason='unchanged_reviewed_report_after_rejected_revision',
                                    revision_status='failed_candidate_retained_original')
                        write_json(workspace / '_meta.json', meta)
                if not partial:
                    return 1
            exported = export_report(workspace / ".claw/run", workspace)
            unchanged = all(_manifest(workspace / name) == digest for name, digest in before.items())
            passed = bool(results and int(results[-1].stage) == 26 and results[-1].status == StageStatus.DONE)
            status = ("completed" if passed else "completed_with_gaps") if (
                (passed or partial) and unchanged and exported["deliverables_complete"]) else "failed"
            meta = read_json(workspace / "_meta.json")
            meta.update(status=status, model=config.llm.primary_model, inputs_unchanged=unchanged,
                        pipeline_status="failed" if partial else "done",
                        scientific_status="gaps" if partial else ("internally_accepted" if passed else "unverified"),
                        internal_verification=read_json(workspace / ".claw/run/research_verification.json").get("all_satisfied", False))
            if passed:
                for key in ('error', 'error_type', 'failed_phase', 'failed_stage', 'failure_kind'):
                    meta.pop(key, None)
            write_json(workspace / "_meta.json", meta)
            return 0 if status in {"completed", "completed_with_gaps"} else 1
        except BaseException as exc:
            meta.update(status="failed", error_type=type(exc).__name__)
            write_json(workspace / "_meta.json", meta)
            raise
        finally:
            if resume:
                continuation.update(finished_at=datetime.now(timezone.utc).isoformat(),
                                    active_seconds=round(time.monotonic()-invocation_started, 3),
                                    status=read_json(workspace / '_meta.json').get('status', 'failed'),
                                    calls_after=read_json(workspace / '.claw/run/model_call_budget.json').get('attempts', 0))
                write_json(history_path, history)


def score(repo: Path, workspace: Path) -> dict[str, Any]:
    if not all(os.environ.get(key) for key in ("JUDGE_API_KEY", "JUDGE_API_BASE", "JUDGE_MODEL_NAME")):
        raise ValueError("Set JUDGE_API_KEY, JUDGE_API_BASE and JUDGE_MODEL_NAME for an independent judge")
    if read_json(workspace / "_meta.json").get("status") not in {"completed", "completed_with_gaps"}:
        raise ValueError("Only completed deliverables can be scored; scientific gaps remain recorded")
    if importlib.util.find_spec("structai") is None:
        raise ValueError("Install the official scorer dependency: structai>=0.1.23")
    scorer = _official(repo, "score")
    scorer.JUDGE_MODEL_NAME = os.environ["JUDGE_MODEL_NAME"]
    from researchclaw.benchmarks.judge import configured_official_judge, judge_options
    options = judge_options()
    with configured_official_judge(scorer, options):
        result = scorer.score_workspace(workspace)
    if result.get("error"):
        raise ValueError(result["error"])
    items = result.get("items", [])
    if not items or any(
        type(item.get("score")) not in (int, float) or not math.isfinite(item["score"])
        or not 0 <= item["score"] <= 100
        or str(item.get("reasoning", "")).startswith(("Failed to parse scoring response", "Scoring failed"))
        for item in items
    ):
        raise ValueError("Official judge returned incomplete scoring; retry scoring this retained report")
    metadata = read_json(workspace / "_claw_benchmark.json")
    external = {"benchmark": "ResearchClawBench", "task_id": result["task_id"],
                "evaluator_version": revision(repo), "judge_model": os.environ["JUDGE_MODEL_NAME"],
                "data_revision": hashlib.sha256(json.dumps({key: metadata.get(key) for key in (
                    "data_manifest", "related_work_manifest", "task_instructions_sha256")}, sort_keys=True).encode()).hexdigest(),
                "score": result["total_score"], "scorer_source_sha256": hashlib.sha256((repo / "evaluation/score.py").read_bytes()).hexdigest(),
                "official_submission": False}
    external["agent_source_sha256"] = metadata.get("agent_source_sha256")
    external["agent_settings_sha256"] = metadata.get("agent_settings_sha256")
    external["scientific_status"] = read_json(workspace / "_meta.json").get("scientific_status", "unverified")
    external['resume_count'] = read_json(workspace / '_meta.json').get('resume_count', 0)
    external['latency_comparable'] = read_json(workspace / '_meta.json').get('latency_comparable', True)
    external["judge_options"] = options
    protocol = {"scorer_source_sha256": external["scorer_source_sha256"],
                "judge_model": external["judge_model"], "extra_body": options,
                "endpoint_sha256": hashlib.sha256(os.environ["JUDGE_API_BASE"].encode()).hexdigest()}
    external["judge_protocol_sha256"] = hashlib.sha256(json.dumps(protocol, sort_keys=True).encode()).hexdigest()
    write_json(workspace / "external_evaluation.json", external)
    write_json(workspace / ".claw/run/external_evaluation.json", external)
    return external


def summary(repo: Path) -> dict[str, Any]:
    attempts = []
    for path in sorted((repo / "workspaces").glob("*_claw_*")):
        meta = read_json(path / "_meta.json")
        evaluated = read_json(path / "external_evaluation.json")
        attempts.append({"run_id": path.name, "task_id": meta.get("task_id"), "status": meta.get("status"),
                         "score": evaluated.get("score"), "model": meta.get("model"),
                         "judge_model": evaluated.get("judge_model"), "benchmark_revision": evaluated.get("evaluator_version")})
    scored = [row for row in attempts if type(row["score"]) in (int, float)]
    return {"attempts": attempts, "prepared": sum(row["status"] == "prepared" for row in attempts),
            "scored_attempts": len(scored), "scored_tasks": len({row["task_id"] for row in scored}),
            "official_task_count": 40, "official_submission": False,
            "note": "Per-run local evaluations; mixed models, judges and revisions must not be pooled as one ranking."}


def batch(repo: Path, config_path: Path, tasks: list[str], *, repeats: int = 1, timeout: int = 2400) -> dict[str, Any]:
    """Run a finite, sequential campaign and retain every failed attempt."""
    if not 1 <= repeats <= 20 or not 60 <= timeout <= 86400:
        raise ValueError("repeats must be in [1, 20] and timeout in [60, 86400] seconds")
    if not tasks or len(tasks) != len(set(tasks)):
        raise ValueError("Provide a nonempty list of distinct task IDs")
    for task_id in tasks:
        _task_path(repo, task_id)
    readiness = doctor(repo, config_path)
    if not readiness["configuration_ready_for_scoring"]:
        raise ValueError("A batch needs the official scorer dependency and independent judge configuration")
    from researchclaw.llm import create_llm_client
    ok, _ = create_llm_client(RCConfig.load(config_path, check_paths=False)).preflight()
    if not ok:
        raise ValueError("Agent model preflight failed; no batch workspaces were created")
    batch_id = f"claw_batch_{datetime.now(timezone.utc):%Y%m%d_%H%M%S}_{uuid.uuid4().hex[:8]}"
    ledger_path = repo / "workspaces" / "claw_batches" / f"{batch_id}.json"
    ledger = {"batch_id": batch_id, "tasks": tasks, "repeats": repeats, "timeout_seconds": timeout,
              "planned_attempts": len(tasks) * repeats, "attempts": [], "status": "running",
              "official_submission": False, "judge_model": os.environ["JUDGE_MODEL_NAME"]}
    write_json(ledger_path, ledger)
    try:
        for repeat in range(1, repeats + 1):
            for task_id in tasks:
                workspace = prepare(repo, task_id, config_path)
                row = {"task_id": task_id, "repeat": repeat, "workspace": str(workspace), "status": "running", "score": None}
                ledger["attempts"].append(row)
                write_json(ledger_path, ledger)
                environment = os.environ.copy()
                environment["PYTHONPATH"] = str(Path(__file__).resolve().parents[2])
                command = [sys.executable, "-m", "researchclaw", "rcbench", "agent",
                           "--config", str(config_path.resolve()), "--workspace", str(workspace)]
                started = time.monotonic()
                with (workspace / "_agent_output.jsonl").open("w", encoding="utf-8") as output:
                    with subprocess.Popen(command, cwd=workspace, env=environment, stdout=output,
                                          stderr=subprocess.STDOUT, start_new_session=True) as process:
                        try:
                            code = process.wait(timeout=timeout)
                        except subprocess.TimeoutExpired:
                            _terminate_run(process)
                            code = 124
                            row["error"] = "research_timeout"
                        except BaseException:
                            _terminate_run(process)
                            row.update(status="interrupted", error="controller_interrupted")
                            meta = read_json(workspace / "_meta.json")
                            meta.update(status="failed", interruption="controller_interrupted")
                            write_json(workspace / "_meta.json", meta)
                            raise
                meta = read_json(workspace / "_meta.json")
                if code != 0:
                    meta.update(status="failed", exit_code=code)
                    write_json(workspace / "_meta.json", meta)
                row.update(status=meta.get("status", "failed"), duration_seconds=round(time.monotonic() - started, 2), exit_code=code)
                if code == 0 and row["status"] in {"completed", "completed_with_gaps"}:
                    row["scientific_status"] = meta.get("scientific_status", "unverified")
                    row.update(status="scoring", scoring_started_at=datetime.now(timezone.utc).isoformat())
                    write_json(ledger_path, ledger)
                    try:
                        row["score"] = score(repo, workspace)["score"]
                        row["status"] = "scored"
                    except Exception as exc:
                        row.update(status="scoring_failed", error=type(exc).__name__)
                write_json(ledger_path, ledger)
        ledger["status"] = "finished"
    except BaseException as exc:
        ledger.update(status="interrupted", interruption=type(exc).__name__)
        write_json(ledger_path, ledger)
        raise
    finally:
        write_json(ledger_path, ledger)
    return {"ledger": str(ledger_path), **ledger}


def _terminate_run(process: subprocess.Popen) -> None:
    """Clean up the owned agent process and nested tool process groups."""
    parents = {}
    for path in Path("/proc").iterdir():
        if not path.name.isdigit():
            continue
        try:
            status = (path / "status").read_text()
            parents[int(path.name)] = int(next(line.split()[1] for line in status.splitlines() if line.startswith("PPid:")))
        except (OSError, StopIteration, ValueError):
            continue
    descendants = {process.pid}
    while True:
        expanded = descendants | {pid for pid, parent in parents.items() if parent in descendants}
        if expanded == descendants:
            break
        descendants = expanded
    groups = {process.pid}
    for pid in descendants:
        try:
            if os.getpgid(pid) == pid:
                groups.add(pid)
        except ProcessLookupError:
            pass
    for group in groups:
        try:
            os.killpg(group, signal.SIGKILL)
        except ProcessLookupError:
            pass
    process.wait(timeout=5)
    deadline = time.monotonic() + 2
    while True:
        live = []
        for pid in descendants:
            try:
                stat = Path(f"/proc/{pid}/stat").read_text()
                state = stat[stat.rfind(")") + 2:].split()[0]
                if state not in {"Z", "X"}:
                    live.append(pid)
            except (OSError, IndexError):
                continue
        if not live:
            return
        if time.monotonic() >= deadline:
            raise RuntimeError(f"Owned research processes have not stopped after termination: {live}")
        time.sleep(0.01)
