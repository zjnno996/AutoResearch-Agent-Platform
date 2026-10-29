"""Repository-owned paths independent of individual stage module locations."""

from pathlib import Path


def repository_root() -> Path:
    return Path(__file__).resolve().parents[5]


def backend_services_directory() -> Path:
    return Path(__file__).resolve().parents[4] / "services"
