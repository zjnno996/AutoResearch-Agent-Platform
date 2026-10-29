from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "services"))
from engine_snapshot import freeze_engine
from agent_bridge import BridgeState, _project_agent_dir


def test_snapshot_preserves_runtime_after_developer_edits(tmp_path):
    source = tmp_path / "agent/researchclaw"
    source.mkdir(parents=True)
    (source / "__init__.py").write_text("VERSION = 1")
    script = source / "skill-helper.sh"
    script.write_text("#!/bin/sh\nprintf 'skill helper'\n")
    script.chmod(0o755)
    (source / "__pycache__").mkdir()
    (source / "__pycache__/ignored.pyc").write_bytes(b"cache")
    before, old = freeze_engine(source.parent, tmp_path / "snapshots")
    (source / "__init__.py").write_text("VERSION = 2")
    after, new = freeze_engine(source.parent, tmp_path / "snapshots")
    assert old != new and before != after
    assert (before / "researchclaw/__init__.py").read_text() == "VERSION = 1"
    assert (after / "researchclaw/__init__.py").read_text() == "VERSION = 2"
    assert not (before / "researchclaw/__pycache__").exists()
    assert (before / "researchclaw/skill-helper.sh").stat().st_mode & 0o111 == 0o111
    assert freeze_engine(source.parent, tmp_path / "snapshots") == (after, new)


def test_project_keeps_its_engine_across_worker_handoffs_and_bridge_restart(tmp_path):
    source = tmp_path / "agent/researchclaw"
    source.mkdir(parents=True)
    (source / "__init__.py").write_text("VERSION = 1")
    runs = tmp_path / "runs"
    (runs / "projects/focused").mkdir(parents=True)
    (runs / "projects/focused/project_meta.json").write_text('{}')
    state = BridgeState(runs_base_dir=str(runs), agent_package_dir=str(source.parent))
    first = _project_agent_dir(state, "focused")
    (source / "__init__.py").write_text("VERSION = 2")
    state = BridgeState(runs_base_dir=str(runs), agent_package_dir=str(source.parent))
    assert _project_agent_dir(state, "focused") == first
    assert (Path(first) / "researchclaw/__init__.py").read_text() == "VERSION = 1"


def test_legacy_checkpoint_is_not_silently_rebound_to_new_source(tmp_path):
    project = tmp_path / "runs/projects/old"
    project.mkdir(parents=True)
    (project / "checkpoint.json").write_text('{"workflow":"compact","last_completed_stage":8}')
    state = BridgeState(runs_base_dir=str(tmp_path / "runs"), agent_package_dir="/original/engine")
    assert _project_agent_dir(state, "old") == "/original/engine"
