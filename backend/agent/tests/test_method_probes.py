"""Real subprocess checks of source-held witnesses, not stubbed model verdicts."""
import copy
import json
from pathlib import Path
import subprocess
import sys

import pytest

from researchclaw.orchestration.artifacts import read_json
from researchclaw.orchestration.compact_state import file_hash
from researchclaw.orchestration.method_probes import (
    HELD_MANIFEST, prepare_method_probes, read_method_receipt, runner_arguments,
    schema_hint, validate_method_plan, validate_method_sources,
)


TASK = "Use the skew coupling F(x,z)=(z,-x), so u dot F(u)=0 in dimensionless units."
GOOD = "def coupling(state):\n    return [state[1], -state[0]]\n"
BAD = "def coupling(state):\n    return [state[1], state[0]]\n"
WITNESS = ("def check(invoke):\n"
           "    state = [2.0, 3.0]\n"
           "    actual = invoke(state)\n"
           "    assert len(actual) == 2\n"
           "    assert sum(a*b for a,b in zip(state, actual)) == 0.0\n")


def plan(code=WITNESS):
    return {"method_steps": [{"id": "M1", "requirement_ids": ["R1"],
            "operation": "Apply the skew linear coupling, not its symmetric sign-flipped alternative.",
            "inputs": "Two dimensionless real coordinates x,z", "outputs": "Two dimensionless coupling components",
            "assumptions": ["Real two-dimensional input"],
            "source": {"reference": "task", "quote": "F(x,z)=(z,-x)"},
            "binding": {"module": "scientific_methods.py", "callable": "coupling"}}],
            "method_probes": [{"id": "P1", "method_step_id": "M1", "kind": "metamorphic",
            "basis": "The declared skew map has zero inner product with its input.",
            "wrong_alternative": "Replacing the second component -x with +x breaks skewness.", "code": code}]}


def test_reflowed_pdf_quote_resolves_one_original_span_without_rewriting_words(tmp_path):
    cached = tmp_path / 'source.md'
    original = TASK.replace(', so', ',\n\tso')
    cached.write_text('Source preface\n' + original + '\n')
    value = plan()
    value['method_steps'][0]['source'] = {'reference': 'public-paper', 'quote': TASK}
    refs = [{'source': 'public-paper', 'text_path': str(cached), 'text_sha256': file_hash(cached)}]
    bound = validate_method_sources(value, TASK, refs)[0]
    assert bound['quote'] == original and bound['provided_quote'] == TASK
    assert bound['quote_resolution'] == 'unique_whitespace_reflow'
    assert cached.read_text()[bound['start_character']:bound['end_character']] == bound['quote']
    assert bound['source_sha256'] == file_hash(cached)
    assert value['method_steps'][0]['source']['quote'] == TASK
    cached.write_text(cached.read_text() + 'changed')
    with pytest.raises(ValueError, match='hash'):
        validate_method_sources(value, TASK, refs)


@pytest.mark.parametrize('case', ['ambiguous', 'changed_equation', 'omitted_words'])
def test_quote_reflow_rejects_ambiguous_or_scientifically_changed_passages(tmp_path, case):
    cached = tmp_path / 'source.md'
    original = TASK.replace(', so', ',\nso')
    cached.write_text((original + '\n') * (2 if case == 'ambiguous' else 1))
    quote = TASK
    if case == 'changed_equation':
        quote = quote.replace('(z,-x)', '(z,x)')
    elif case == 'omitted_words':
        quote = quote.replace('dimensionless ', '')
    value = plan()
    value['method_steps'][0]['source'] = {'reference': 'public-paper', 'quote': quote}
    refs = [{'source': 'public-paper', 'text_path': str(cached), 'text_sha256': file_hash(cached)}]
    with pytest.raises(ValueError, match='exact original text'):
        validate_method_sources(value, TASK, refs)


def test_invalid_quote_gets_exact_original_correction_context_but_is_not_accepted(tmp_path):
    cached = tmp_path/'source.md'
    text = 'Using the scaled dual variable, we can express ADMM as\nx = argmin f(x) + rho/2 * norm(A*x+B*z-c+u)**2.\n'
    cached.write_text(text)
    value = plan()
    value['method_steps'][0]['source'] = {'reference': 'original-paper',
        'quote': 'Using the scaled dual variable, we can express ADMM as x = argmin f(x) - rho/2 * norm(A*x+B*z-c+u)**2.'}
    refs = [{'source': 'original-paper', 'text_path': str(cached), 'text_sha256': file_hash(cached)}]
    with pytest.raises(ValueError, match='exact original text') as error:
        validate_method_sources(value, TASK, refs)
    feedback = str(error.value)
    assert 'not an accepted quotation or proof of relevance' in feedback
    original = json.loads(feedback.split('Original excerpt (JSON-escaped for lossless copying): ', 1)[1])
    assert original in text and '+ rho/2' in original and '- rho/2' not in original
    assert cached.read_text() == text
    value['method_steps'][0]['source']['quote'] = original
    bound = validate_method_sources(value, TASK, refs)[0]
    assert bound['quote'] == original and bound['source_sha256'] == file_hash(cached)


def setup(tmp_path, source=GOOD, method_plan=None):
    root = tmp_path / "run"
    work = root / "stage-11/experiment"
    work.mkdir(parents=True)
    (work / "scientific_methods.py").write_text(source)
    (work / "main.py").write_text("from scientific_methods import coupling\nprint(coupling([2.,3.]))\n")
    manifest, paths = prepare_method_probes(root, method_plan or plan(), TASK, {"references": []})
    assert paths[-1] == HELD_MANIFEST
    return root, work, manifest


def run(root, work, manifest, mode="pilot"):
    receipt = root / (mode + "-receipt.json")
    result = subprocess.run([sys.executable, *runner_arguments(Path(manifest["manifest_path"]), work, receipt, mode)],
                            cwd=work, capture_output=True, text=True, timeout=12)
    return result, receipt, read_json(receipt)


def test_schema_and_legacy_and_explicit_nonapplicability(tmp_path):
    assert "wrong_alternative" in schema_hint() and "operation" in schema_hint()
    validate_method_plan({}, {"R1"})
    with pytest.raises(ValueError):
        validate_method_plan({}, {"R1"}, optionallegacy=False)
    disabled = {"method_steps": [], "method_probes": [],
                "method_probe_note": "Pure prose proof has no applicable numerical callable; proof gaps remain independently reviewed."}
    validate_method_plan(disabled, {"R1"}, optionallegacy=False)
    manifest, _ = prepare_method_probes(tmp_path, disabled, "Prove the statement.", [])
    assert manifest["status"] == "not_applicable" and not manifest["method_steps"]
    assert "passed" not in manifest
    with pytest.raises(ValueError):
        validate_method_plan({"method_steps": [], "method_probes": []}, {"R1"})


@pytest.mark.parametrize("change", ["missing_operation", "bad_requirement", "main", "traversal", "nested_callable", "too_many_steps", "too_many_probes", "oversize"])
def test_plan_rejects_incomplete_or_unbounded_witnesses(change):
    value = plan()
    step = value["method_steps"][0]
    if change == "missing_operation":
        del step["operation"]
    elif change == "bad_requirement":
        step["requirement_ids"] = ["UNKNOWN"]
    elif change == "main":
        step["binding"]["module"] = "main.py"
    elif change == "traversal":
        step["binding"]["module"] = "../foreign.py"
    elif change == "nested_callable":
        step["binding"]["callable"] = "Solver.coupling"
    elif change == "too_many_steps":
        value["method_steps"] *= 4
    elif change == "too_many_probes":
        value["method_probes"] *= 7
    elif change == "oversize":
        value["method_probes"][0]["code"] += "#" * 6001
    with pytest.raises(ValueError):
        validate_method_plan(value, {"R1"}, optionallegacy=False)


@pytest.mark.parametrize("code", [
    "def check(invoke):\n    invoke([2,3])\n    assert True\n",
    "def check(invoke):\n    invoke([2,3])\n    assert 1 == 1\n",
    "def check(invoke):\n    actual = invoke([2,3])\n    assert actual == actual\n",
    "def check(invoke):\n    assert [3,-2] == [3,-2]\n",
    "import os\ndef check(invoke):\n    assert invoke([2,3]) == [3,-2]\n",
])
def test_disconnected_or_vacuous_or_external_oracles_are_rejected(code):
    with pytest.raises(ValueError):
        validate_method_plan(plan(code), {"R1"})


def test_exact_source_quotes_are_purely_validated_and_cache_hashes_are_required(tmp_path):
    value = plan()
    assert validate_method_sources(value, TASK, [])[0]["quote_sha256"]
    cached = tmp_path / "public-source.md"
    cached.write_text("A public method states F(x,z)=(z,-x) under the given assumptions.\n")
    refs = {"references": [{"source": "https://example.org/article", "alias_sources": ["https://doi.org/10.0000/example"],
                            "text_path": str(cached), "sha256": file_hash(cached)}]}
    value["method_steps"][0]["source"]["reference"] = "https://doi.org/10.0000/example"
    before = set(tmp_path.rglob("*"))
    bound = validate_method_sources(value, TASK, refs)
    assert set(tmp_path.rglob("*")) == before
    assert bound[0]["source_path"] == str(cached)
    cached.write_text(cached.read_text() + "changed")
    with pytest.raises(ValueError, match="hash"):
        validate_method_sources(value, TASK, refs)
    value["method_steps"][0]["source"] = {"reference": "task", "quote": "an invented equation"}
    with pytest.raises(ValueError, match="exact"):
        validate_method_sources(value, TASK, [])


def test_held_design_is_idempotent_but_cannot_be_silently_rewritten(tmp_path):
    root, work, manifest = setup(tmp_path)
    path = Path(manifest["manifest_path"])
    before = path.read_bytes()
    again, _ = prepare_method_probes(root, plan(), TASK, [])
    assert again["manifest_sha256"] == manifest["manifest_sha256"] and path.read_bytes() == before
    changed = plan(WITNESS.replace("2.0, 3.0", "3.0, 4.0"))
    with pytest.raises(ValueError, match="approval"):
        prepare_method_probes(root, changed, TASK, [])
    assert path.read_bytes() == before


def test_pilot_uses_real_callable_and_detects_the_sign_regression(tmp_path):
    root, work, manifest = setup(tmp_path)
    process, receipt, record = run(root, work, manifest)
    assert process.returncode == 0, (process.stdout, process.stderr, record)
    verified = read_method_receipt(Path(manifest["manifest_path"]), work, receipt, "pilot")
    assert verified["target_call_counts"]["M1"] > 0
    assert verified["probes"][0]["assertions"] == 2
    (work / "scientific_methods.py").write_text(BAD)
    process, receipt, record = run(root, work, manifest)
    assert process.returncode != 0 and record["passed"] is False
    assert record["probes"][0]["failed_assertions"] == 1
    assert record["integrity_before"] == record["integrity_after"]
    with pytest.raises(ValueError):
        read_method_receipt(Path(manifest["manifest_path"]), work, receipt, "pilot")


@pytest.mark.parametrize("code", [
    "def check(invoke):\n    if False:\n        assert invoke([2,3]) == [3,-2]\n",
    "def check(invoke):\n    actual = invoke([2,3])\n    try:\n        assert actual == [0,0]\n    except AssertionError:\n        pass\n",
])
def test_dead_or_swallowed_assertions_do_not_pass(tmp_path, code):
    root, work, manifest = setup(tmp_path, method_plan=plan(code))
    process, receipt, record = run(root, work, manifest)
    assert process.returncode != 0 and not record["passed"]


def test_observation_executes_main_once_and_requires_the_same_binding(tmp_path):
    root, work, manifest = setup(tmp_path)
    (work / "main.py").write_text("from scientific_methods import coupling\nfrom pathlib import Path\n"
                                 "p=Path('outputs/count.txt')\np.parent.mkdir(exist_ok=True)\n"
                                 "p.write_text(str(int(p.read_text())+1) if p.exists() else '1')\n"
                                 "print(coupling([2.,3.]))\n")
    process, receipt, record = run(root, work, manifest, "observe")
    assert process.returncode == 0, (process.stderr, record)
    assert (work / "outputs/count.txt").read_text() == "1"
    assert read_method_receipt(Path(manifest["manifest_path"]), work, receipt, "observe")["target_call_counts"]["M1"] == 1
    (work / "main.py").write_text("def unrelated(state):\n    return [state[1],-state[0]]\nprint(unrelated([2,3]))\n")
    process, receipt, record = run(root, work, manifest, "observe")
    assert process.returncode != 0 and record["target_call_counts"]["M1"] == 0


def test_same_callable_name_in_different_modules_is_not_conflated(tmp_path):
    value = plan()
    other = copy.deepcopy(value["method_steps"][0])
    other["id"] = "M2"
    other["binding"]["module"] = "other_methods.py"
    value["method_steps"].append(other)
    probe = copy.deepcopy(value["method_probes"][0])
    probe.update(id="P2", method_step_id="M2")
    value["method_probes"].append(probe)
    root, work, manifest = setup(tmp_path, method_plan=value)
    (work / "other_methods.py").write_text(GOOD)
    process, receipt, record = run(root, work, manifest)
    assert process.returncode == 0 and all(record["target_call_counts"][x] > 0 for x in ("M1", "M2"))
    process, receipt, record = run(root, work, manifest, "observe")
    assert process.returncode != 0
    assert record["target_call_counts"]["M1"] > 0 and record["target_call_counts"]["M2"] == 0


def test_import_time_full_program_is_rejected_before_it_executes(tmp_path):
    source = "from pathlib import Path\nPath('imported.txt').write_text('bad')\n" + GOOD
    root, work, manifest = setup(tmp_path, source=source)
    process, receipt, record = run(root, work, manifest)
    assert process.returncode != 0 and not (work / "imported.txt").exists()


def test_source_changes_during_pilot_and_after_receipt_are_detected(tmp_path):
    root, work, manifest = setup(tmp_path)
    process, receipt, record = run(root, work, manifest)
    assert process.returncode == 0
    (work / "scientific_methods.py").write_text(GOOD + "# changed after receipt\n")
    with pytest.raises(ValueError):
        read_method_receipt(Path(manifest["manifest_path"]), work, receipt, "pilot")
    mutating = ("def coupling(state):\n    from pathlib import Path\n    p=Path(__file__)\n"
                "    p.write_text(p.read_text()+'\\n# changed during invocation\\n')\n"
                "    return [state[1],-state[0]]\n")
    (work / "scientific_methods.py").write_text(mutating)
    process, receipt, record = run(root, work, manifest)
    assert process.returncode != 0 and record["passed"] is False
    assert record["integrity_before"] != record["integrity_after"]


def test_modified_held_code_or_manifest_cannot_reuse_a_pass(tmp_path):
    root, work, manifest = setup(tmp_path)
    process, receipt, record = run(root, work, manifest)
    assert process.returncode == 0
    path = Path(manifest["manifest_path"])
    held = path.parent / read_json(path)["method_probes"][0]["path"]
    held.write_text(held.read_text() + "# changed held oracle\n")
    with pytest.raises(ValueError):
        read_method_receipt(path, work, receipt, "pilot")
    path.write_text(path.read_text() + "\n")
    with pytest.raises(ValueError, match="approved"):
        runner_arguments(path, work, receipt, expected_manifest_sha256=manifest["manifest_sha256"])


def test_configured_environment_imports_numerical_dependencies(tmp_path):
    code = ("import numpy as np\ndef check(invoke):\n"
            "    actual=np.asarray(invoke([2.0,3.0]))\n    assert np.allclose(actual, [3.0,-2.0])\n")
    root, work, manifest = setup(tmp_path, method_plan=plan(code))
    process, receipt, record = run(root, work, manifest)
    assert process.returncode == 0, (process.stderr, record)
