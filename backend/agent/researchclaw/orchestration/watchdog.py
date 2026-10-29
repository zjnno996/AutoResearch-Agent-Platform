"""Observe progress without mistaking a quiet experiment for a dead process."""
from __future__ import annotations

import os
import sys
import threading
import time
import traceback
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

from researchclaw.orchestration.artifacts import read_json, write_json


def process_cpu_seconds(pid: int) -> float:
    rows = {}
    for path in Path("/proc").glob("[0-9]*/stat"):
        try:
            text = path.read_text()
            fields = text[text.rfind(")") + 2:].split()
            rows[int(path.parent.name)] = (int(fields[1]), int(fields[11]) + int(fields[12]))
        except (OSError, ValueError, IndexError):
            continue
    descendants = {pid}
    while True:
        expanded = descendants | {child for child, row in rows.items() if row[0] in descendants}
        if expanded == descendants:
            break
        descendants = expanded
    return sum(rows.get(child, (0, 0))[1] for child in descendants) / os.sysconf("SC_CLK_TCK")


def health_snapshot(root: Path, *, now: float | None = None, cpu_delta: float = 0.0) -> dict[str, Any]:
    now = time.time() if now is None else now
    provider = read_json(root / "provider_activity.json")
    heartbeat = read_json(root / "heartbeat.json")
    phase = heartbeat.get("phase", "initializing")
    state = read_json(root / "compact_state.json").get("phases", {}).get(phase, {})
    progress_at = provider.get("last_semantic_progress_at", provider.get("started_at", state.get("started", now)))
    silence = max(0, now - float(progress_at))
    active = provider.get("status") in {"started", "streaming"}
    phase_elapsed = (now - float(state.get('started', now)) if state.get('status') == 'running'
                     else float(state.get('elapsed_sec', 0)))
    request_end = now if active else float(provider.get('updated_at', provider.get('started_at', now)))
    request_elapsed = max(0, request_end - float(provider.get('started_at', request_end)))
    if active:
        status = "model_streaming" if silence < 30 and provider.get("semantic_chars", 0) else "waiting_provider"
        if silence > float(provider.get("timeout_sec", 600)) + 15:
            status = "provider_stalled_check_required"
    elif cpu_delta >= .05:
        status = "computing"
    else:
        elapsed = now - float(state.get("started", now))
        status = "quiet_check_required" if state.get("status") == "running" and elapsed > 180 else "phase_running"
    return {"pid": os.getpid(), "phase": phase, "status": status, "observed_at": now,
            'phase_elapsed_sec': round(max(0, phase_elapsed), 2),
            'request_elapsed_sec': round(request_elapsed, 2),
            'request_active': active,
            'semantic_chars': provider.get('semantic_chars', 0), 'model': provider.get('model', ''),
            "provider_silence_sec": round(silence, 2), "cpu_seconds_delta": round(max(0, cpu_delta), 3),
            "provider": provider, "policy": "Observe and diagnose. Request, experiment and batch timeouts own bounded retries; do not restart a live job just because it is quiet."}


@contextmanager
def run_watchdog(root: Path, interval: float = 10) -> Iterator[None]:
    stopped = threading.Event()
    owner = threading.get_ident()
    def monitor() -> None:
        previous = process_cpu_seconds(os.getpid())
        last_warning = ""
        while not stopped.wait(interval):
            try:
                current = process_cpu_seconds(os.getpid())
                snapshot = health_snapshot(root, cpu_delta=current - previous)
                previous = current
                write_json(root / "run_health.json", snapshot)
                if snapshot["status"].endswith("check_required") and snapshot["status"] != last_warning:
                    frame = sys._current_frames().get(owner)
                    if frame:
                        (root / "stalled_controller_stack.txt").write_text("".join(traceback.format_stack(frame)), encoding="utf-8")
                    last_warning = snapshot["status"]
                elif not snapshot["status"].endswith("check_required"):
                    last_warning = ""
            except (OSError, ValueError):
                pass
    thread = threading.Thread(target=monitor, name="research-progress-watchdog", daemon=True)
    thread.start()
    try:
        yield
    finally:
        stopped.set()
        thread.join(timeout=1)
        snapshot = health_snapshot(root)
        snapshot["status"] = "controller_finished"
        write_json(root / "run_health.json", snapshot)
