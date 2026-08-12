from __future__ import annotations

import base64
import importlib.util
import sys
from pathlib import Path
from types import SimpleNamespace

import yaml


def _load_agent_bridge_module():
    module_path = Path(__file__).resolve().parents[2] / "services" / "agent_bridge.py"
    spec = importlib.util.spec_from_file_location("agent_bridge_for_tests", module_path)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_generate_config_from_template_persists_reference_papers(tmp_path: Path) -> None:
    agent_bridge = _load_agent_bridge_module()
    state = SimpleNamespace(
        runs_base_dir=str(tmp_path / "runs"),
        agent_package_dir=str(tmp_path / "agent"),
    )

    config_path = agent_bridge._generate_config_from_template(
        state,
        project_id="proj-ref-test",
        topic="test topic",
        reference_papers=[
            "1234.56789",
            "/data/papers/local_reference.pdf",
        ],
    )

    content = Path(config_path).read_text(encoding="utf-8")
    parsed = yaml.safe_load(content)
    assert parsed["research"]["reference_papers"] == [
        "1234.56789",
        "/data/papers/local_reference.pdf",
    ]
    assert "__REFERENCE_PAPERS__" not in content


def test_persist_reference_uploads_saves_pdf_files(tmp_path: Path) -> None:
    agent_bridge = _load_agent_bridge_module()
    project_dir = tmp_path / "project"

    saved_paths = agent_bridge._persist_reference_uploads(
        project_dir,
        [
            {
                "name": "paper one.pdf",
                "contentBase64": base64.b64encode(b"%PDF-1.4 sample").decode("ascii"),
            }
        ],
    )

    assert len(saved_paths) == 1
    saved_path = Path(saved_paths[0])
    assert saved_path.exists()
    assert saved_path.suffix == ".pdf"
    assert saved_path.read_bytes() == b"%PDF-1.4 sample"


def test_list_all_projects_treats_live_subrun_heartbeat_as_running(
    tmp_path: Path, monkeypatch
) -> None:
    agent_bridge = _load_agent_bridge_module()
    runs = tmp_path / "runs"
    run_dir = runs / "projects" / "proj-live" / "run-cv"
    run_dir.mkdir(parents=True)
    (run_dir / "checkpoint.json").write_text(
        '{"last_completed_stage": 3, "last_completed_name": "SEARCH_STRATEGY"}',
        encoding="utf-8",
    )
    (run_dir / "heartbeat.json").write_text(
        '{"pid": 12345, "last_stage": 3, "last_stage_name": "SEARCH_STRATEGY"}',
        encoding="utf-8",
    )
    state = agent_bridge.BridgeState(runs_base_dir=str(runs))

    monkeypatch.setattr(agent_bridge, "_pid_is_alive", lambda pid: True)
    monkeypatch.setattr(
        agent_bridge, "_pid_cmdline_contains_run_dir", lambda pid, path: True
    )

    projects = agent_bridge.list_all_projects(state)

    assert projects[0]["projectId"] == "proj-live"
    assert projects[0]["status"] == "running"


def test_list_all_projects_marks_stale_heartbeat_interrupted(
    tmp_path: Path, monkeypatch
) -> None:
    agent_bridge = _load_agent_bridge_module()
    runs = tmp_path / "runs"
    run_dir = runs / "projects" / "proj-stale" / "run-cv"
    run_dir.mkdir(parents=True)
    (run_dir / "checkpoint.json").write_text(
        '{"last_completed_stage": 3, "last_completed_name": "SEARCH_STRATEGY"}',
        encoding="utf-8",
    )
    (run_dir / "heartbeat.json").write_text(
        '{"pid": 12345, "last_stage": 3, "last_stage_name": "SEARCH_STRATEGY"}',
        encoding="utf-8",
    )
    state = agent_bridge.BridgeState(runs_base_dir=str(runs))

    monkeypatch.setattr(agent_bridge, "_pid_is_alive", lambda pid: False)

    projects = agent_bridge.list_all_projects(state)

    assert projects[0]["projectId"] == "proj-stale"
    assert projects[0]["status"] == "interrupted"


def test_full_chain_uses_s26_as_terminal_and_resumes_finalization(
    tmp_path: Path,
) -> None:
    agent_bridge = _load_agent_bridge_module()
    run_dir = tmp_path / "run"
    run_dir.mkdir()

    assert agent_bridge.PROJECT_TERMINAL_STAGE == 26
    assert agent_bridge.LAYER_RANGE["writing"] == (19, 26)
    assert agent_bridge.STAGE_TO_LAYER[23] == "writing"
    assert agent_bridge.STAGE_TO_LAYER[26] == "writing"

    (run_dir / "checkpoint.json").write_text(
        '{"last_completed_stage": 22, "last_completed_name": "PAPER_REVISION"}',
        encoding="utf-8",
    )
    assert agent_bridge._determine_resume_target(str(run_dir)) == ("writing", 23)

    (run_dir / "checkpoint.json").write_text(
        '{"last_completed_stage": 25, "last_completed_name": "EXPORT_PUBLISH"}',
        encoding="utf-8",
    )
    assert agent_bridge._determine_resume_target(str(run_dir)) == ("writing", 26)

    (run_dir / "checkpoint.json").write_text(
        '{"last_completed_stage": 26, "last_completed_name": "CITATION_VERIFY"}',
        encoding="utf-8",
    )
    assert agent_bridge._determine_resume_target(str(run_dir)) is None


def test_restart_from_writing_clears_all_finalization_stages(tmp_path: Path) -> None:
    agent_bridge = _load_agent_bridge_module()
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    for stage in range(19, 27):
        stage_dir = run_dir / f"stage-{stage:02d}"
        stage_dir.mkdir()
        (stage_dir / "artifact.txt").write_text("stale", encoding="utf-8")

    agent_bridge._clear_run_from_stage(run_dir, 19)

    assert all(not (run_dir / f"stage-{stage:02d}").exists() for stage in range(19, 27))
    checkpoint = (run_dir / "checkpoint.json").read_text(encoding="utf-8")
    assert '"last_completed_stage": 18' in checkpoint
