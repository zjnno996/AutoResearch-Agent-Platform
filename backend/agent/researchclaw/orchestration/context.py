"""Request context is local to an invocation, including concurrent projects."""

from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from typing import Iterator

_CONTEXT: ContextVar[str] = ContextVar("research_stage_context", default="")
_READ_DIRS: ContextVar[tuple[str, ...]] = ContextVar("research_skill_directories", default=())
MARKER = "<research-stage-guidance>"


@contextmanager
def stage_guidance(text: str, *, read_dirs: tuple[str, ...] = ()) -> Iterator[None]:
    token = _CONTEXT.set(text)
    directory_token = _READ_DIRS.set(read_dirs)
    try:
        yield
    finally:
        _CONTEXT.reset(token)
        _READ_DIRS.reset(directory_token)


def skill_read_directories() -> tuple[str, ...]:
    return _READ_DIRS.get()


def augment_system(system: str | None) -> str | None:
    context = _CONTEXT.get()
    if not context or MARKER in (system or ""):
        return system
    return (system or "") + f"\n\n{MARKER}\n{context}\n</research-stage-guidance>"
