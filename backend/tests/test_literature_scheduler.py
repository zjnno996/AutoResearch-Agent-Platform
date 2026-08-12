from __future__ import annotations

import json
from pathlib import Path

import services.agent_bridge as agent_bridge
from services.agent_bridge import (
    BridgeState,
    Task,
    TaskQueue,
    _dispatch_literature_schedule,
    _finalize_literature_schedule,
    poll_literature_schedules,
)


def _write_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")


def test_finalize_creates_incremental_chinese_review_and_deduplicates(tmp_path: Path) -> None:
    runs = tmp_path / "runs"
    project_id = "literature-watch-test"
    project_dir = runs / "projects" / project_id
    _write_json(project_dir / "checkpoint.json", {"last_completed_stage": 7})
    shortlist = [
        {"paper_id": "known", "title": "已经收录的论文", "year": 2025},
        {"paper_id": "new", "title": "新发现的论文", "year": 2026, "url": "https://example.org/new"},
    ]
    shortlist_path = project_dir / "stage-05" / "shortlist.jsonl"
    shortlist_path.parent.mkdir(parents=True, exist_ok=True)
    shortlist_path.write_text("\n".join(json.dumps(item, ensure_ascii=False) for item in shortlist), encoding="utf-8")
    synthesis_path = project_dir / "stage-07" / "synthesis.md"
    synthesis_path.parent.mkdir(parents=True, exist_ok=True)
    synthesis_path.write_text("这是基于真实检索结果生成的中文综述。", encoding="utf-8")

    schedule = {
        "id": "lit-test", "name": "测试周报", "topic": "多模态评审",
        "enabled": True, "status": "running", "lastProjectId": project_id,
        "seenPaperIds": ["known"], "history": [],
    }
    state = BridgeState(runs_base_dir=str(runs), literature_schedules=[schedule])

    assert _finalize_literature_schedule(state, schedule) is True
    assert schedule["status"] == "idle"
    assert schedule["lastNewPaperCount"] == 1
    assert set(schedule["seenPaperIds"]) == {"known", "new"}
    report = (project_dir / "stage-07" / "literature_watch_report.md").read_text(encoding="utf-8")
    assert "增量文献综述" in report
    assert "相对历史新增：1 篇" in report
    assert "新发现的论文" in report
    assert "已经收录的论文" not in report
    assert "这是基于真实检索结果生成的中文综述" in report
    assert (project_dir / "deliverables" / "literature_watch_report.md").exists()
    assert (runs / "schedules" / "literature_tasks.json").exists()


def test_dispatch_uses_qwen3_literature_watch_pipeline(tmp_path: Path, monkeypatch) -> None:
    runs = tmp_path / "runs"
    state = BridgeState(runs_base_dir=str(runs))
    captured: dict[str, object] = {}

    def fake_config(*args, **kwargs):
        captured["config_kwargs"] = kwargs
        return str(tmp_path / "generated.yaml")

    def fake_submit(_state, project_id, config_path, topic, **kwargs):
        captured.update({"project_id": project_id, "config_path": config_path, "topic": topic, **kwargs})
        return [{"type": "system", "payload": {"message": "queued"}}]

    monkeypatch.setattr(agent_bridge, "_generate_config_from_template", fake_config)
    monkeypatch.setattr(agent_bridge, "submit_new_project", fake_submit)
    schedule = {
        "id": "lit-abc", "userId": "u1", "name": "周报", "topic": "具身智能",
        "keywords": ["embodied AI"], "sources": ["arxiv"], "lookbackDays": 7,
        "intervalHours": 24, "enabled": True, "status": "idle", "runCount": 0,
        "seenPaperIds": [],
    }
    state.literature_schedules = [schedule]

    messages = _dispatch_literature_schedule(state, schedule)

    assert schedule["status"] == "running"
    assert schedule["runCount"] == 1
    assert captured["run_mode"] == "literature_watch"
    assert captured["mode"] == "scheduled_literature"
    assert captured["user_id"] == "u1"
    assert "最近 7 天" in str(captured["topic"])
    assert captured["config_kwargs"]["model_name"] == agent_bridge.DEFAULT_QWEN3_MODEL
    assert messages[-1]["type"] == "literature_schedule_list"


def test_due_schedule_is_dispatched_by_poll_loop(tmp_path: Path, monkeypatch) -> None:
    state = BridgeState(runs_base_dir=str(tmp_path / "runs"))
    schedule = {
        "id": "lit-due", "userId": "u1", "enabled": True, "status": "idle",
        "nextRunAt": 0,
    }
    state.literature_schedules = [schedule]

    def fake_dispatch(_state, item):
        item["status"] = "running"
        return [{"type": "system", "payload": {"message": "dispatched"}}]

    monkeypatch.setattr(agent_bridge, "_dispatch_literature_schedule", fake_dispatch)

    messages = poll_literature_schedules(state)

    assert schedule["status"] == "running"
    assert any(message.get("payload", {}).get("message") == "dispatched" for message in messages)


def test_failed_run_releases_schedule_for_next_period(tmp_path: Path) -> None:
    runs = tmp_path / "runs"
    project_id = "literature-watch-failed"
    project_dir = runs / "projects" / project_id
    project_dir.mkdir(parents=True)
    queue = TaskQueue("init_to_idea", runs / "queues" / "init_to_idea.json")
    failed_task = Task(
        id="task-failed", project_id=project_id, run_dir=str(project_dir),
        config_path="config.yaml", source_layer="init", target_layer="idea",
        status="failed",
    )
    queue.tasks = [failed_task]
    schedule = {
        "id": "lit-failed", "topic": "失败恢复", "enabled": True,
        "status": "running", "lastProjectId": project_id,
        "intervalHours": 24, "nextRunAt": 0, "history": [],
    }
    state = BridgeState(
        runs_base_dir=str(runs), queues={"init_to_idea": queue},
        literature_schedules=[schedule],
    )

    assert _finalize_literature_schedule(state, schedule) is True
    assert schedule["status"] == "idle"
    assert schedule["nextRunAt"] > schedule["lastCompletedAt"]
    assert schedule["history"][-1]["status"] == "failed"
    assert "下个周期自动重试" in schedule["lastError"]
