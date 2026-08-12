from __future__ import annotations

import json
from pathlib import Path

import services.agent_bridge as agent_bridge
from services.agent_bridge import (
    BridgeState,
    LobsterAgent,
    TaskQueue,
    _enqueue_checkpoint_retry,
    _adopt_live_subprocesses,
    _recover_orphaned_inflight_projects,
    poll_agent,
)


class _ExitedProcess:
    def __init__(self, code: int = 0) -> None:
        self.code = code

    def poll(self) -> int:
        return self.code


def _write_json(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value), encoding="utf-8")


def test_clean_exit_without_target_checkpoint_is_an_interruption(tmp_path: Path) -> None:
    run_dir = tmp_path / "run-cv"
    run_dir.mkdir()
    _write_json(run_dir / "checkpoint.json", {"last_completed_stage": 7})
    agent = LobsterAgent(
        id="L-test", name="L1", layer="idea", run_id="run", run_dir=str(run_dir),
        config_path="config.yaml", project_id="project", status="working",
        current_stage=8, process=_ExitedProcess(0),
    )
    agent._expected_to_stage = 8

    poll_agent(agent)

    assert agent.status == "error"
    assert "断点仅到 S7" in agent.current_task


def test_interrupted_stage_is_requeued_from_checkpoint(tmp_path: Path) -> None:
    runs = tmp_path / "runs"
    run_dir = runs / "projects" / "project" / "run-cv"
    run_dir.mkdir(parents=True)
    _write_json(run_dir / "checkpoint.json", {"last_completed_stage": 7})
    queue = TaskQueue("init_to_idea", runs / "queues" / "init_to_idea.json")
    state = BridgeState(runs_base_dir=str(runs), queues={"init_to_idea": queue})
    agent = LobsterAgent(
        id="L-test", name="L1", layer="idea", run_id="run", run_dir=str(run_dir),
        config_path="config.yaml", project_id="project", status="error",
    )
    agent._topic = "topic"

    messages = _enqueue_checkpoint_retry(state, agent, 1)

    assert len(queue.tasks) == 1
    assert queue.tasks[0].status == "pending"
    assert queue.tasks[0].target_layer == "idea"
    assert "从 S8" in messages[0]["payload"]["message"]


def test_startup_recovers_orphaned_s8_handoff(tmp_path: Path) -> None:
    runs = tmp_path / "runs"
    project_dir = runs / "projects" / "project"
    project_dir.mkdir(parents=True)
    config_path = tmp_path / "project.yaml"
    config_path.write_text("project: {}", encoding="utf-8")
    _write_json(project_dir / "project_meta.json", {
        "project_id": "project", "config_path": str(config_path),
        "topic": "topic", "run_mode": "full_chain",
    })
    _write_json(project_dir / "checkpoint.json", {"last_completed_stage": 7})
    _write_json(project_dir / "heartbeat.json", {
        "pid": 99999999, "last_stage": 8, "timestamp": "2026-07-16T00:00:00Z",
    })
    queue = TaskQueue("init_to_idea", runs / "queues" / "init_to_idea.json")
    state = BridgeState(runs_base_dir=str(runs), queues={"init_to_idea": queue})

    recovered = _recover_orphaned_inflight_projects(state)

    assert recovered == 1
    assert queue.tasks[0].project_id == "project"
    assert queue.tasks[0].status == "pending"
    assert not (project_dir / "heartbeat.json").exists()


def test_restart_adopts_live_s8_process(tmp_path: Path, monkeypatch) -> None:
    runs = tmp_path / "runs"
    project_dir = runs / "projects" / "project"
    project_dir.mkdir(parents=True)
    _write_json(project_dir / "project_meta.json", {
        "project_id": "project", "config_path": "/tmp/config.yaml",
        "topic": "topic", "run_mode": "full_chain",
    })
    _write_json(project_dir / "checkpoint.json", {"last_completed_stage": 7})
    _write_json(project_dir / "heartbeat.json", {
        "pid": 12345, "last_stage": 8, "run_id": "run-s8",
    })
    state = BridgeState(runs_base_dir=str(runs))
    agent = LobsterAgent(id="L-test", name="L1", layer="idea", run_id="", run_dir="", config_path="")
    state.agents[agent.id] = agent
    monkeypatch.setattr(agent_bridge, "_run_dir_has_live_heartbeat", lambda _: True)

    adopted = _adopt_live_subprocesses(state)

    assert adopted == 1
    assert agent.status == "working"
    assert agent.current_stage == 8
    assert agent.process.pid == 12345
    assert agent._expected_to_stage == 8
    assert agent._is_discussion_s8 is True


def test_startup_recovers_recent_s7_discussion_handoff(tmp_path: Path) -> None:
    runs = tmp_path / "runs"
    project_dir = runs / "projects" / "project"
    project_dir.mkdir(parents=True)
    config_path = tmp_path / "project.yaml"
    config_path.write_text("project: {}", encoding="utf-8")
    _write_json(project_dir / "project_meta.json", {
        "project_id": "project", "config_path": str(config_path),
        "topic": "topic", "run_mode": "full_chain",
    })
    _write_json(project_dir / "checkpoint.json", {"last_completed_stage": 7})
    _write_json(project_dir / "heartbeat.json", {
        "pid": 99999999, "last_stage": 7, "timestamp": "2026-07-16T00:00:00Z",
    })
    queue = TaskQueue("init_to_idea", runs / "queues" / "init_to_idea.json")
    state = BridgeState(runs_base_dir=str(runs), queues={"init_to_idea": queue})

    assert _recover_orphaned_inflight_projects(state) == 1
    assert queue.tasks[0].target_layer == "idea"
