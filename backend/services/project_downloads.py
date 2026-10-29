"""Resolve project deliverables for both focused and multi-run workspaces."""
from __future__ import annotations

from io import BytesIO
from pathlib import Path, PurePosixPath
from urllib.parse import quote
from zipfile import ZipFile, ZIP_DEFLATED


def project_download(projects: Path, project_id: str, filename: str) -> tuple[bytes, str, str] | None:
    """Return bytes, content type and disposition, confined to one project.

    Experiment archives include the actual source and outputs. They are bounded
    before assembly, so an accidental large dataset cannot exhaust the service.
    """
    if not project_id or project_id in {".", ".."} or "/" in project_id or "\\" in project_id:
        return None
    relative = PurePosixPath(filename)
    if not filename or relative.is_absolute() or ".." in relative.parts or "\\" in filename:
        return None
    base = projects.resolve()
    project = (base / project_id).resolve()
    if not project.is_dir():
        project = (base / "_cross_discussions" / project_id).resolve()
    if not project.is_relative_to(base) or not project.is_dir():
        return None
    runs = [project, *sorted(project.glob("run-*"), reverse=True)]
    for run in runs:
        if not run.is_dir() or not run.resolve().is_relative_to(project):
            continue
        roots = [run / "deliverables", *sorted(run.glob("stage-*"), reverse=True), run / "discussion"]
        if filename in {"experiment.zip", "outputs.zip"}:
            directory = run / "stage-11" / "experiment"
            if filename == "outputs.zip":
                directory /= "outputs"
            if directory.is_dir() and directory.resolve().is_relative_to(project):
                files = sorted(p for p in directory.rglob("*") if p.is_file()
                    and not p.is_symlink() and p.resolve().is_relative_to(directory.resolve())
                    and "__pycache__" not in p.parts and ".venv" not in p.parts)
                if len(files) > 5000 or sum(p.stat().st_size for p in files) > 64 * 1024 * 1024:
                    raise ValueError("Experiment archive exceeds the 64 MiB / 5000 file download limit")
                output = BytesIO()
                with ZipFile(output, "w", ZIP_DEFLATED) as archive:
                    for path in files:
                        archive.write(path, path.relative_to(directory).as_posix())
                return output.getvalue(), "application/zip", _disposition(filename)
        for root in roots:
            candidate = (root / filename).resolve()
            if candidate.is_relative_to(project) and candidate.is_file():
                return candidate.read_bytes(), "application/octet-stream", _disposition(relative.name)
    return None


def _disposition(filename: str) -> str:
    # ASCII headers remain valid for project artifacts with Chinese filenames.
    return f"attachment; filename*=UTF-8''{quote(filename, safe='')}"
