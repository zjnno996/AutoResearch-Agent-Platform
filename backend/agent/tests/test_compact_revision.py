"""Observed regressions: partial context rewrites and interrupted multi-file repair."""
from pathlib import Path

import pytest

from researchclaw.orchestration.artifacts import read_json, write_json
from researchclaw.orchestration.compact_contracts import ContractError
from researchclaw.orchestration import compact_revision as revision
from researchclaw.orchestration.compact_generation import bind_report_images, record_sources, source_files, source_texts, source_packet, reference_packet
from researchclaw.orchestration.artifacts import recover_object_members


def edit(name, original, old, new):
    return {"path": name, "sha256": revision.digest(original), "old": old, "new": new}


@pytest.mark.parametrize('text', ['{"a":1} {"b":2}', '{"a":1},"dimensions":{',
                                   '{"a":1} prose', '[1,2],"a":3}', '{"a":1},"b":2'])
def test_response_recovery_never_discards_trailers_or_accepts_incomplete_json(text):
    assert recover_object_members(text) == {}


def test_reference_retrieval_uses_original_cached_text_for_a_specific_scientific_gap(tmp_path):
    text = tmp_path / 'source.md'
    original = ('Abstract and source identity\n' + 'Unrelated background. ' * 600
                + 'Fractional interval duration must conserve integrated changes. Density conversion is explicitly defined.\n'
                + 'Unrelated discussion. ' * 600)
    text.write_text(original)
    packet = reference_packet({'references': [{'source': 'primary-paper', 'text_path': str(text),
                                               'context': 'Old abstract-only context'}]}, 3500,
                              query='fractional interval duration density conversion')
    assert 'Fractional interval duration must conserve' in packet
    assert 'source characters' in packet and len(packet) <= 3500


def test_reference_retrieval_cannot_read_evaluation_text(tmp_path):
    text = tmp_path / 'target_study/paper.md'
    text.parent.mkdir()
    text.write_text('FORBIDDEN target content')
    packet = reference_packet({'references': [{'source': 'visible citation', 'text_path': str(text),
                                               'context': 'Original visible context'}]}, query='density')
    assert 'FORBIDDEN' not in packet and 'Original visible context' in packet


def test_targeted_edit_preserves_unseen_source_and_independent_files(tmp_path):
    source = "VALUE = -1\n" + "# independently supported details\n" * 2500 + "TAIL = 7\n"
    originals = {"main.py": source, "solver.py": "PRECISION = 1e-10\n"}
    work = tmp_path / "work"
    work.mkdir()
    for name, text in originals.items():
        (work / name).write_text(text)
    changed = revision.apply_edits(work, originals, {"edits": [edit("main.py", source, "VALUE = -1", "VALUE = 1")]})
    journal = revision.publish(work, originals, changed, journal_root=tmp_path / "research_revisions", kind="source", context="one")
    assert (work / "main.py").read_text() == source.replace("VALUE = -1", "VALUE = 1")
    assert (work / "solver.py").read_text() == originals["solver.py"]
    assert read_json(journal / "manifest.json")["status"] == "committed"
    assert revision.committed(tmp_path / "research_revisions", "source", "one") == ["main.py"]
    (work / "solver.py").write_text("external change\n")
    assert revision.committed(tmp_path / "research_revisions", "source", "one") is None


@pytest.mark.parametrize("failure", ["stale", "ambiguous", "syntax", "scope", "traversal", "empty"])
def test_invalid_patch_never_overwrites_source(tmp_path, failure):
    source = "VALUE = 1\n# repeated\n# repeated\n"
    (tmp_path / "main.py").write_text(source)
    item = edit("main.py", source, "VALUE = 1", "VALUE = 2")
    if failure == "stale": item["sha256"] = "0" * 64
    if failure == "ambiguous": item["old"] = "# repeated"
    if failure == "syntax": item["new"] = "def broken("
    if failure == "scope": item["path"] = "other.py"
    if failure == "traversal": item["path"] = "../main.py"
    payload = {"edits": [] if failure == "empty" else [item]}
    with pytest.raises(ContractError):
        revision.apply_edits(tmp_path, {"main.py": source}, payload)
    assert (tmp_path / "main.py").read_text() == source


def test_source_patch_cannot_follow_input_symlinks(tmp_path):
    data = tmp_path / "inputs"
    data.mkdir()
    (data / "secret.py").write_text("VALUE = 1\n")
    work = tmp_path / "work"
    work.mkdir()
    (work / "linked").symlink_to(data)
    with pytest.raises(ContractError):
        revision.apply_edits(work, {}, {"add_files": [{"path": "linked/new.py", "content": "VALUE = 2\n"}]}, allow_new_source=True)


def test_interrupted_publication_restores_all_prior_files(tmp_path, monkeypatch):
    work = tmp_path / "work"
    work.mkdir()
    originals = {"main.py": "VALUE = 1\n", "helper.py": "VALUE = 2\n"}
    changed = {"main.py": "VALUE = 3\n", "helper.py": "VALUE = 4\n"}
    for name, text in originals.items():
        (work / name).write_text(text)
    atomic = revision._atomic_text
    def fail_on_second_file(path, text):
        if path == work / "helper.py" and text == changed["helper.py"]:
            raise OSError("simulated interrupted publication")
        return atomic(path, text)
    monkeypatch.setattr(revision, "_atomic_text", fail_on_second_file)
    with pytest.raises(OSError):
        revision.publish(work, originals, changed, journal_root=tmp_path / "research_revisions", kind="source", context="one")
    assert {name: (work / name).read_text() for name in originals} == originals
    assert read_json(next((tmp_path / "research_revisions").glob("*/manifest.json")))["status"] == "rolled_back"


def test_process_restart_recovers_a_partially_published_transaction(tmp_path):
    work = tmp_path / "work"
    work.mkdir()
    original = "VALUE = 1\n"
    (work / "main.py").write_text(original)
    journal = revision.publish(work, {"main.py": original}, {"main.py": "VALUE = 2\n"},
                               journal_root=tmp_path / "research_revisions", kind="source", context="one")
    row = read_json(journal / "manifest.json")
    row["status"] = "publishing"  # Simulate process death after the file write, before the commit marker.
    write_json(journal / "manifest.json", row)
    revision.recover(tmp_path)
    assert (work / "main.py").read_text() == original
    assert read_json(journal / "manifest.json")["status"] == "rolled_back"


def test_recovery_preserves_an_external_edit_instead_of_silently_rolling_it_back(tmp_path):
    work = tmp_path / "work"
    work.mkdir()
    original = "VALUE = 1\n"
    (work / "main.py").write_text(original)
    journal = revision.publish(work, {"main.py": original}, {"main.py": "VALUE = 2\n"},
                               journal_root=tmp_path / "research_revisions", kind="source", context="one")
    row = read_json(journal / "manifest.json")
    row["status"] = "publishing"
    write_json(journal / "manifest.json", row)
    (work / "main.py").write_text("VALUE = 99\n")
    with pytest.raises(ContractError, match="external edit"):
        revision.recover(tmp_path)
    assert (work / "main.py").read_text() == "VALUE = 99\n"


@pytest.mark.parametrize("alias", ["images/curve.png", "report/images/curve.png", "outputs/curve.png", "curve.png"])
def test_report_aliases_bind_only_to_a_real_current_figure(tmp_path, alias):
    figure = tmp_path / "stage-11/experiment/outputs/curve.png"
    figure.parent.mkdir(parents=True)
    figure.write_bytes(b"fixture path binding only")
    report = tmp_path / "stage-25/paper_final.md"
    text = f'# Evidence\n![Measured error]({alias} "Observed curve")\nUnchanged scientific text.\n'
    result = bind_report_images(text, report, [figure])
    assert result == '# Evidence\n![Measured error](../stage-11/experiment/outputs/curve.png "Observed curve")\nUnchanged scientific text.\n'
    assert bind_report_images(result, report, [figure]) == result


@pytest.mark.parametrize("target", ["images/missing.png", "https://example.test/curve.png", "../unrelated/curve.png"])
def test_report_binding_does_not_fabricate_or_fetch_missing_figures(tmp_path, target):
    with pytest.raises(ContractError):
        bind_report_images(f'![Result]({target})', tmp_path / 'stage-25/paper_final.md', [])


def test_duplicate_figure_names_require_an_unambiguous_path(tmp_path):
    figures = [tmp_path / 'outputs' / name / 'curve.png' for name in ('first', 'second')]
    for path in figures:
        path.parent.mkdir(parents=True)
        path.write_bytes(b'path fixture')
    report = tmp_path / 'stage-25/paper_final.md'
    with pytest.raises(ContractError, match='2 matches'):
        bind_report_images('![Result](images/curve.png)', report, figures)
    assert '../outputs/first/curve.png' in bind_report_images('![Result](images/first/curve.png)', report, figures)


@pytest.mark.parametrize('name', ['figure_data_overview.png', 'fig_01_data_overview.png', 'plot_2_data_overview.png', '01_data_overview.png'])
def test_report_structural_naming_prefix_is_not_a_new_scientific_figure(tmp_path, name):
    figure = tmp_path / 'outputs' / name
    figure.parent.mkdir()
    figure.write_bytes(b'path fixture')
    result = bind_report_images('![Measured inputs](images/data_overview.png)', tmp_path / 'stage-25/paper_final.md', [figure])
    assert result == f'![Measured inputs](../outputs/{name})'


def test_structural_figure_names_still_reject_ambiguity(tmp_path):
    figures = [tmp_path / 'outputs' / name for name in ('figure_data_overview.png', 'plot_data_overview.png')]
    figures[0].parent.mkdir()
    for figure in figures:figure.write_bytes(b'path fixture')
    with pytest.raises(ContractError, match='2 matches'):
        bind_report_images('![Inputs](images/data_overview.png)', tmp_path / 'stage-25/paper_final.md', figures)


def test_source_manifest_preserves_authored_packages_but_excludes_generated_outputs(tmp_path):
    (tmp_path / 'main.py').write_text('import report.renderer\n')
    (tmp_path / 'report').mkdir()
    (tmp_path / 'report/renderer.py').write_text('VALUE = 1\n')
    record_sources(tmp_path, ['main.py', 'report/renderer.py'])
    (tmp_path / 'report/images').mkdir()
    (tmp_path / 'report/images/result.png').write_bytes(b'\x89PNG\r\n\x1a\n')
    (tmp_path / 'generated_table.json').write_text('{"result":2}')
    assert {p.relative_to(tmp_path).as_posix() for p in source_files(tmp_path)} == {'main.py', 'report/renderer.py', '.source_manifest.json'}
    assert set(source_texts(tmp_path)) == {'main.py', 'report/renderer.py'}
    (tmp_path / 'helper.py').write_text('VALUE = 2\n')
    record_sources(tmp_path, ['helper.py'])
    assert 'helper.py' in source_texts(tmp_path) and 'generated_table.json' not in source_texts(tmp_path)


def test_binary_assets_are_hashed_but_not_decoded_as_editable_text(tmp_path):
    (tmp_path / 'main.py').write_text('VALUE = 1\n')
    (tmp_path / 'weights.bin').write_bytes(b'\x89\xff\x00')
    record_sources(tmp_path, ['main.py', 'weights.bin'])
    assert tmp_path / 'weights.bin' in source_files(tmp_path)
    assert source_texts(tmp_path) == {'main.py': 'VALUE = 1\n'}


def test_missing_declared_source_does_not_silently_disappear_from_provenance(tmp_path):
    (tmp_path / 'main.py').write_text('VALUE = 1\n')
    record_sources(tmp_path, ['main.py'])
    (tmp_path / 'main.py').unlink()
    with pytest.raises(ContractError, match='missing'):
        source_files(tmp_path)


def test_full_source_budget_includes_middle_scientific_transform(tmp_path):
    source = '# background\n' * 1800 + '\ndef normalize_units(value):\n    return value * 0.001\n' + '# appendix\n' * 1800
    (tmp_path / 'main.py').write_text(source)
    packet = source_packet(tmp_path, 60000)
    assert 'def normalize_units(value)' in packet and '[... middle omitted ...]' not in packet
