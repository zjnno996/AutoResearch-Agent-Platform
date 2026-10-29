"""One controller per run, with reentrant ownership during bounded repairs."""

from __future__ import annotations

import fcntl
import json
import os
import threading
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator

_OWNED: ContextVar[dict[str, tuple[int, int]]] = ContextVar("research_execution_leases", default={})


class RunAlreadyActive(RuntimeError):
    pass


@contextmanager
def run_lease(run_dir: Path, run_id: str) -> Iterator[None]:
    root = run_dir.resolve()
    key = str(root)
    identity = (os.getpid(), threading.get_ident())
    if _OWNED.get().get(key) == identity:
        yield
        return
    root.mkdir(parents=True, exist_ok=True)
    path = root / ".execution.lock"
    with path.open("a+", encoding="utf-8") as handle:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise RunAlreadyActive(f"Run already has an active controller: {root}") from exc
        token = _OWNED.set({**_OWNED.get(), key: identity})
        owner = {"pid": identity[0], "run_id": run_id, "status": "running",
                 "acquired_at": datetime.now(timezone.utc).isoformat()}
        try:
            handle.seek(0)
            handle.truncate()
            handle.write(json.dumps(owner))
            handle.flush()
            yield
        finally:
            _OWNED.reset(token)
            owner.update(status="released", released_at=datetime.now(timezone.utc).isoformat())
            handle.seek(0)
            handle.truncate()
            handle.write(json.dumps(owner))
            handle.flush()
            fcntl.flock(handle, fcntl.LOCK_UN)
