from io import BytesIO
from pathlib import Path
import sys
from zipfile import ZipFile

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "services"))
from project_downloads import project_download


def test_focused_stage_report_and_executed_source_are_downloadable(tmp_path):
    project = tmp_path / "focused"
    (project / "stage-25").mkdir(parents=True)
    (project / "stage-25/paper_final.md").write_text("Actual report")
    work = project / "stage-11/experiment"
    (work / "outputs").mkdir(parents=True)
    (work / "main.py").write_text("print('executed source')")
    (work / "outputs/metrics.json").write_text('{"mse": 0}')
    assert project_download(tmp_path, "focused", "paper_final.md")[0] == b"Actual report"
    data, content_type, _ = project_download(tmp_path, "focused", "experiment.zip")
    with ZipFile(BytesIO(data)) as archive:
        assert set(archive.namelist()) == {"main.py", "outputs/metrics.json"}
    assert content_type == "application/zip"


def test_lab_deliverables_take_precedence_over_stage_drafts(tmp_path):
    run = tmp_path / "lab/run-1"
    for directory, text in [("deliverables", "Final"), ("stage-25", "Draft")]:
        (run / directory).mkdir(parents=True)
        (run / directory / "paper_final.md").write_text(text)
    assert project_download(tmp_path, "lab", "paper_final.md")[0] == b"Final"


@pytest.mark.parametrize("project,filename", [("..", "secret"), ("focused", "../secret"), ("focused", "/etc/passwd"), ("../focused", "report.md")])
def test_paths_cannot_escape_the_project(tmp_path, project, filename):
    assert project_download(tmp_path, project, filename) is None


def test_external_symlinks_are_not_downloaded_or_archived(tmp_path):
    outside = tmp_path / "private.txt"
    outside.write_text("private")
    work = tmp_path / "focused/stage-11/experiment"
    work.mkdir(parents=True)
    (work / "leak.txt").symlink_to(outside)
    assert project_download(tmp_path, "focused", "experiment/leak.txt") is None
    data, _, _ = project_download(tmp_path, "focused", "experiment.zip")
    with ZipFile(BytesIO(data)) as archive:
        assert not archive.namelist()


def test_large_experiment_is_rejected_before_archive_assembly(tmp_path):
    work = tmp_path / "focused/stage-11/experiment"
    work.mkdir(parents=True)
    with (work / "data.bin").open("wb") as stream:
        stream.truncate(65 * 1024 * 1024)
    with pytest.raises(ValueError, match="archive exceeds"):
        project_download(tmp_path, "focused", "experiment.zip")
