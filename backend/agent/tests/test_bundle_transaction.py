"""Binary publication, recovery, and conflict protection for retained bundles."""

import json
from pathlib import Path

import pytest

from researchclaw.orchestration import bundle_transaction as transaction


@pytest.fixture
def bundle(tmp_path):
    root = tmp_path / "run"
    root.mkdir()
    snapshots = root / "research_retention" / "reviewed"
    snapshots.mkdir(parents=True)
    (snapshots / "replace.bin").write_bytes(b"\xff\x00new\x80")
    (snapshots / "add.bin").write_bytes(b"\x00\xf0added")
    (snapshots / "manifest.json").write_bytes(b'{"source":"reviewed"}\n')
    (root / "replace.bin").write_bytes(b"\x00\xfeold")
    (root / "delete.bin").write_bytes(b"\x80removed")
    (root / ".source_manifest.json").write_bytes(b'{"source":"before"}\n')
    untouched = {
        "llm_budget.json": b'{"spent":17}',
        "inputs/data.bin": b"\x00private-input",
        "archives/old.bin": b"archived",
        "unrelated.txt": b"unchanged",
    }
    for relative, data in untouched.items():
        path = root / relative
        path.parent.mkdir(exist_ok=True)
        path.write_bytes(data)
    files = {
        "replace.bin": snapshots / "replace.bin",
        "delete.bin": None,
        "nested/add.bin": snapshots / "add.bin",
        ".source_manifest.json": snapshots / "manifest.json",
    }
    before = {relative: (root / relative).read_bytes() if (root / relative).exists() else None
              for relative in files}
    return root, root / "research_retention" / "transactions", files, before, untouched


def contents(root, paths):
    return {relative: (root / relative).read_bytes() if (root / relative).exists() else None
            for relative in paths}


def manifest(journal):
    return json.loads((journal / "manifest.json").read_text())


def interrupt_after(monkeypatch, files, count):
    install = transaction._install
    calls = 0

    def interrupted(root, relative, data, expected):
        nonlocal calls
        install(root, relative, data, expected)
        if relative in files:
            calls += 1
            if calls == count:
                raise KeyboardInterrupt("simulated process interruption")

    monkeypatch.setattr(transaction, "_install", interrupted)
    return install


def pending(bundle, monkeypatch, count=2):
    root, parent, files, _, _ = bundle
    install = interrupt_after(monkeypatch, files, count)
    with pytest.raises(KeyboardInterrupt):
        transaction.publish_bundle(root, files, parent)
    monkeypatch.setattr(transaction, "_install", install)
    return next(parent.glob("bundle-*"))


def test_binary_replace_delete_add_and_source_manifest_commit(bundle):
    root, parent, files, before, untouched = bundle
    sources = {source: source.read_bytes() for source in files.values() if source is not None}
    journal = transaction.publish_bundle(root, files, parent, {"assessment": {"score": 0.87}})

    expected = {relative: source.read_bytes() if source is not None else None
                for relative, source in files.items()}
    assert contents(root, files) == expected
    saved = manifest(journal)
    assert saved["status"] == "committed"
    assert saved["root"]["path"] == str(root)
    assert saved["metadata"] == {"assessment": {"score": 0.87}}
    for index, relative in enumerate(files):
        for side, data in (("before", before[relative]), ("after", expected[relative])):
            snapshot = journal / side / f"{index:06d}.bin"
            assert snapshot.read_bytes() == data if data is not None else not snapshot.exists()
    assert {source: source.read_bytes() for source in sources} == sources
    assert contents(root, untouched) == untouched

    # A terminal journal must never undo a later, legitimate edit.
    (root / "replace.bin").write_bytes(b"later edit")
    transaction.recover_bundles(root, parent)
    assert (root / "replace.bin").read_bytes() == b"later edit"
    assert manifest(journal)["status"] == "committed"


def test_ordinary_application_error_rolls_back_all_files(bundle, monkeypatch):
    root, parent, files, before, untouched = bundle
    install = transaction._install

    def fail_on_add(checked_root, relative, data, expected):
        if relative == "nested/add.bin":
            raise OSError("injected replace failure")
        install(checked_root, relative, data, expected)

    monkeypatch.setattr(transaction, "_install", fail_on_add)
    with pytest.raises(OSError, match="injected replace failure"):
        transaction.publish_bundle(root, files, parent)
    assert contents(root, files) == before
    assert contents(root, untouched) == untouched
    assert manifest(next(parent.glob("bundle-*")))["status"] == "rolled_back"


@pytest.mark.parametrize("count", [1, 2, 3, 4])
def test_interrupted_publication_recovers_idempotently(bundle, monkeypatch, count):
    root, parent, files, before, untouched = bundle
    journal = pending(bundle, monkeypatch, count)
    assert manifest(journal)["status"] == "applying"
    assert contents(root, files) != before
    transaction.recover_bundles(root, parent)
    assert contents(root, files) == before
    assert contents(root, untouched) == untouched
    assert manifest(journal)["status"] == "rolled_back"
    transaction.recover_bundles(root, parent)
    assert contents(root, files) == before


def test_prepared_journal_recovers_without_applying_new_bytes(bundle, monkeypatch):
    root, parent, files, before, _ = bundle
    save = transaction._save

    def stop_when_prepared(checked_root, journal, value):
        save(checked_root, journal, value)
        if value["status"] == "prepared":
            raise KeyboardInterrupt("stop before applying")

    monkeypatch.setattr(transaction, "_save", stop_when_prepared)
    with pytest.raises(KeyboardInterrupt):
        transaction.publish_bundle(root, files, parent)
    journal = next(parent.glob("bundle-*"))
    assert manifest(journal)["status"] == "prepared"
    monkeypatch.setattr(transaction, "_save", save)
    transaction.recover_bundles(root, parent)
    assert contents(root, files) == before
    assert manifest(journal)["status"] == "rolled_back"


@pytest.mark.parametrize("side", ["before", "after"])
def test_tampered_snapshot_blocks_recovery_before_any_restore(bundle, monkeypatch, side):
    root, parent, files, _, _ = bundle
    journal = pending(bundle, monkeypatch)
    interrupted = contents(root, files)
    (journal / side / "000000.bin").write_bytes(b"tampered")
    with pytest.raises(ValueError, match="tampered"):
        transaction.recover_bundles(root, parent)
    assert contents(root, files) == interrupted
    assert manifest(journal)["status"] == "applying"


@pytest.mark.parametrize("identity_field", ["path", "inode"])
def test_wrong_root_identity_blocks_recovery(bundle, monkeypatch, identity_field):
    root, parent, files, _, _ = bundle
    journal = pending(bundle, monkeypatch)
    saved = manifest(journal)
    saved["root"][identity_field] = "/other/root" if identity_field == "path" else -1
    (journal / "manifest.json").write_text(json.dumps(saved))
    interrupted = contents(root, files)
    with pytest.raises(ValueError, match="identity"):
        transaction.recover_bundles(root, parent)
    assert contents(root, files) == interrupted


def test_external_edit_blocks_entire_rollback(bundle, monkeypatch):
    root, parent, files, before, _ = bundle
    journal = pending(bundle, monkeypatch)
    (root / "delete.bin").write_bytes(b"external edit")
    edited = contents(root, files)
    with pytest.raises(ValueError, match="External edit"):
        transaction.recover_bundles(root, parent)
    assert contents(root, files) == edited
    assert manifest(journal)["status"] == "applying"
    # Once the conflicting file is reconciled, the same journal can recover.
    (root / "delete.bin").write_bytes(before["delete.bin"])
    transaction.recover_bundles(root, parent)
    assert contents(root, files) == before


@pytest.mark.parametrize("relative", ["../escape", "/absolute", "a/../b", "a//b", ".", "", "a\\b"])
def test_invalid_relative_paths_fail_before_any_mutation(bundle, relative):
    root, parent, files, before, _ = bundle
    with pytest.raises(ValueError):
        transaction.publish_bundle(root, {**files, relative: None}, parent)
    assert contents(root, files) == before
    assert not parent.exists()


def test_external_source_is_rejected_before_any_mutation(bundle):
    root, parent, files, before, _ = bundle
    outside = root.parent / "outside.bin"
    outside.write_bytes(b"outside")
    with pytest.raises(ValueError, match="outside bundle root"):
        transaction.publish_bundle(root, {**files, "extra.bin": outside}, parent)
    assert contents(root, files) == before
    assert not parent.exists()
    assert outside.read_bytes() == b"outside"


@pytest.mark.parametrize("kind", ["destination", "destination_parent", "source", "source_parent", "journal"])
def test_symlink_paths_cannot_escape_root(bundle, kind):
    root, parent, files, _, untouched = bundle
    outside = root.parent / "outside"
    outside.mkdir()
    (outside / "target.bin").write_bytes(b"outside")
    source = files["replace.bin"]
    if kind == "destination":
        (root / "link.bin").symlink_to(outside / "target.bin")
        files = {"link.bin": source}
    elif kind == "destination_parent":
        (root / "link").symlink_to(outside, target_is_directory=True)
        files = {"link/target.bin": source}
    elif kind == "source":
        (root / "source.bin").symlink_to(outside / "target.bin")
        files = {"replace.bin": root / "source.bin"}
    elif kind == "source_parent":
        (root / "source").symlink_to(outside, target_is_directory=True)
        files = {"replace.bin": root / "source" / "target.bin"}
    else:
        parent.symlink_to(outside, target_is_directory=True)
    with pytest.raises(ValueError):
        transaction.publish_bundle(root, files, parent)
    assert (outside / "target.bin").read_bytes() == b"outside"
    assert sorted(path.name for path in outside.iterdir()) == ["target.bin"]
    assert contents(root, untouched) == untouched


def test_destination_changed_during_preparation_is_not_overwritten(bundle, monkeypatch):
    root, parent, files, before, _ = bundle
    install = transaction._install

    def edit_during_snapshot(checked_root, relative, data, expected):
        install(checked_root, relative, data, expected)
        if relative.endswith("before/000000.bin"):
            (root / "replace.bin").write_bytes(b"external edit")

    monkeypatch.setattr(transaction, "_install", edit_during_snapshot)
    with pytest.raises(ValueError, match="changed during preparation"):
        transaction.publish_bundle(root, files, parent)
    before["replace.bin"] = b"external edit"
    assert contents(root, files) == before
    transaction.recover_bundles(root, parent)
    assert contents(root, files) == before


def test_source_changed_during_preparation_is_not_published(bundle, monkeypatch):
    root, parent, files, before, _ = bundle
    install = transaction._install

    def edit_during_snapshot(checked_root, relative, data, expected):
        install(checked_root, relative, data, expected)
        if relative.endswith("after/000000.bin"):
            files["replace.bin"].write_bytes(b"changed reviewed snapshot")

    monkeypatch.setattr(transaction, "_install", edit_during_snapshot)
    with pytest.raises(ValueError, match="snapshot changed during preparation"):
        transaction.publish_bundle(root, files, parent)
    assert contents(root, files) == before


def test_destination_changed_before_publication_is_not_overwritten(bundle, monkeypatch):
    root, parent, files, before, _ = bundle
    save = transaction._save

    def edit_when_prepared(checked_root, journal, value):
        save(checked_root, journal, value)
        if value["status"] == "prepared":
            (root / "replace.bin").write_bytes(b"external edit")

    monkeypatch.setattr(transaction, "_save", edit_when_prepared)
    with pytest.raises(RuntimeError, match="requires recovery"):
        transaction.publish_bundle(root, files, parent)
    before["replace.bin"] = b"external edit"
    assert contents(root, files) == before


@pytest.mark.parametrize("extra", [{"replace.bin/child": None}, {"research_retention/transactions/state": None}])
def test_overlapping_destinations_and_journals_are_rejected(bundle, extra):
    root, parent, files, before, _ = bundle
    with pytest.raises(ValueError, match="overlap"):
        transaction.publish_bundle(root, {**files, **extra}, parent)
    assert contents(root, files) == before
    assert not parent.exists()


def test_recovery_rejects_traversal_in_manifest_before_restoring(bundle, monkeypatch):
    root, parent, files, _, _ = bundle
    journal = pending(bundle, monkeypatch)
    saved = manifest(journal)
    saved["files"][0]["path"] = "../outside.bin"
    (journal / "manifest.json").write_text(json.dumps(saved))
    interrupted = contents(root, files)
    with pytest.raises(ValueError, match="relative path"):
        transaction.recover_bundles(root, parent)
    assert contents(root, files) == interrupted


def test_recovery_without_journal_directory_is_a_noop(bundle):
    root, parent, files, before, _ = bundle
    transaction.recover_bundles(root, parent)
    assert contents(root, files) == before
    assert not parent.exists()
