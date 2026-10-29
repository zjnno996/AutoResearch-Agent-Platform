import json

from services import agent_bridge as bridge


def setup_project(tmp_path):
    project = tmp_path / "projects/paused"
    project.mkdir(parents=True)
    config = tmp_path / "config.yaml"
    config.write_text("project: {}")
    meta = {"project_id": "paused", "config_path": str(config), "topic": "Finite task", "mode": "focused", "run_mode": "full_chain"}
    (project / "project_meta.json").write_text(json.dumps(meta))
    (project / "checkpoint.json").write_text('{"last_completed_stage":8}')
    (project / "heartbeat.json").write_text('{"pid":99999999,"last_stage":9}')
    queue = bridge.TaskQueue("idea_to_experiment", tmp_path / "queues/idea_to_experiment.json")
    state = bridge.BridgeState(runs_base_dir=str(tmp_path), queues={"idea_to_experiment": queue})
    task = bridge.Task(id="pending", project_id="paused", run_dir=str(project), config_path=str(config), topic="Finite task", source_layer="idea", target_layer="experiment", created_at=0)
    queue.push(task)
    return state, project, queue


def test_pause_survives_restart_and_manual_resume_clears_it(tmp_path):
    state, project, queue = setup_project(tmp_path)
    bridge._pause_project(state, "paused")
    assert json.loads((project / "project_meta.json").read_text())["paused"] is True
    restarted = bridge.TaskQueue(queue.name, queue.path)
    restarted.load()
    assert not restarted.tasks
    state.queues[queue.name] = restarted
    assert bridge._recover_orphaned_inflight_projects(state) == 0
    assert bridge.list_all_projects(state)[0]["status"] == "paused"
    bridge.resume_project(state, "paused")
    assert not json.loads((project / "project_meta.json").read_text()).get("paused")
    assert len(restarted.tasks) == 1 and restarted.tasks[0].status == "pending"


def test_assignment_keeps_failures_until_a_stage_actually_completes(tmp_path, monkeypatch):
    state, project, queue = setup_project(tmp_path)
    agent = bridge.LobsterAgent(id="worker", name="L2", layer="experiment", run_id="", run_dir="", config_path="")
    state.agents[agent.id] = agent
    state._fail_counts["paused"] = 2
    launched = []
    monkeypatch.setattr(bridge, "launch_agent_for_task", lambda *args: launched.append(args[-1].id) or [])
    bridge.schedule_idle_agents(state)
    assert launched == ["pending"] and state._fail_counts["paused"] == 2
    queue.tasks[0].status = "pending"
    state._fail_counts["paused"] = 3
    bridge.schedule_idle_agents(state)
    assert launched == ["pending"] and queue.tasks[0].status == "cancelled"


def test_paused_project_cannot_be_requeued_by_a_late_failure(tmp_path):
    state, project, queue = setup_project(tmp_path)
    bridge._pause_project(state, "paused")
    agent = bridge.LobsterAgent(id="worker", name="L2", layer="experiment", run_id="", run_dir=str(project), config_path=str(tmp_path / "config.yaml"), project_id="paused")
    assert bridge._enqueue_checkpoint_retry(state, agent, 1) == []
    assert not queue.tasks
