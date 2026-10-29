"""Scientific counterexamples exercise the real controller/subprocess boundary."""
from dataclasses import replace
import hashlib
import os
import py_compile
import json
from pathlib import Path
import shutil
from types import SimpleNamespace
import sys

import pytest
import yaml

from researchclaw.config import RCConfig, ResearchEngineConfig
from researchclaw.orchestration.artifacts import read_json, write_json
from researchclaw.orchestration.compact import CompactResearch
from researchclaw.orchestration.compact_contracts import ContractError
from researchclaw.orchestration.method_probes import prepare_method_probes, validate_method_sources
from researchclaw.orchestration.method_execution import audit_method_execution


TASK = 'Validate the skew coupling of two primal variables and their multiplier.'
CORRECT = 'def coupling(x, z, u):\n    return u, -u, z-x\n'
WRONG = CORRECT.replace('z-x', 'x-z')


def fixture(tmp_path, source=CORRECT, used=True, replies=(), runtime_seconds=15):
    config = RCConfig.from_dict({'project': {'name': 'witness-test'}, 'research': {'topic': TASK},
        'runtime': {'timezone': 'UTC'}, 'notifications': {'channel': 'console'},
        'knowledge_base': {'root': str(tmp_path/'kb')},
        'llm': {'api_key_env': 'TEST_KEY', 'primary_model': 'fixture'}})
    config = replace(config, research_engine=ResearchEngineConfig(enabled=True, workflow='compact',
        skills_enabled=False, council_enabled=False, max_repair_rounds=1 if replies else 0),
        experiment=replace(config.experiment, mode='sandbox', time_budget_sec=runtime_seconds,
                           sandbox=replace(config.experiment.sandbox, python_path=sys.executable)))
    class Replies:
        def __init__(self):
            self.config, self.replies, self.calls = SimpleNamespace(), list(replies), 0
        def chat(self, *args, **kwargs):
            self.calls += 1
            return SimpleNamespace(content=json.dumps(self.replies.pop(0)), truncated=False, finish_reason='stop')
    llm = Replies()
    engine = CompactResearch(tmp_path, 'method-test', config, llm)
    work = engine.work
    work.mkdir(parents=True, exist_ok=True)
    (work/'scientific_methods.py').write_text(source)
    computation = ('from scientific_methods import coupling\na,b,c=coupling(1.,0.,-1.)\nvalue=a-c\n'
                   if used else 'value=0.\n')
    (work/'main.py').write_text('from pathlib import Path\nimport json\nfrom PIL import Image\n'
        + computation + "p=Path('outputs');p.mkdir(exist_ok=True)\n"
        + "(p/'metrics.json').write_text(json.dumps({'inner_product':value}))\n"
        + "(p/'analysis.md').write_text('One finite illustrative identity check, not a proof.')\n"
        + "Image.new('RGB',(32,32),'blue').save(p/'identity.png')\n")
    write_json(work/'.source_manifest.json', {'files': ['main.py', 'scientific_methods.py']})
    contract = {'requirements': [{'id': 'R1', 'requirement': TASK, 'acceptance': 'Skew coupling identity'}]}
    write_json(engine.contract_path, contract)
    write_json(tmp_path/'stage-02/task_data_profile.json', {'inputs': []})
    plan = {'methods': ['skew coupling'], 'datasets': [], 'dataset_policy': 'Self-contained analytic fixture',
        'metrics': ['inner_product'], 'protocol': {'command': 'python main.py'},
        'requirement_map': {'R1': {'approach': 'Evaluate the specified coupling', 'artifact': 'outputs/metrics.json',
                                  'evidence_route': 'direct', 'must_preserve': 'Skew coupling and its state ordering'}},
        'method_steps': [{'id': 'M1', 'requirement_ids': ['R1'], 'operation': '(u,-u,z-x)',
            'inputs': 'Dimensionless real x,z,u', 'outputs': 'Dimensionless vector', 'assumptions': [],
            'source': {'reference': 'task', 'quote': TASK},
            'binding': {'module': 'scientific_methods.py', 'callable': 'coupling'}}],
        'method_probes': [{'id': 'P1', 'method_step_id': 'M1', 'kind': 'analytic',
            'basis': 'The inner product of a skew linear map with its argument vanishes.',
            'wrong_alternative': 'The final coordinate x-z reverses the skew sign.',
            'code': 'def check(invoke):\n    a,b,c=invoke(1.,0.,-1.)\n    assert abs(a-c)<1e-12\n'}]}
    engine.plan_path.parent.mkdir(parents=True, exist_ok=True)
    engine.plan_path.write_text(yaml.safe_dump(plan))
    prepare_method_probes(tmp_path, plan, TASK, [])
    return engine, plan, llm


def test_correct_method_has_both_real_pilot_and_full_run_evidence(tmp_path):
    engine, plan, llm = fixture(tmp_path)
    paths = engine.execute()
    receipt = read_json(tmp_path/'stage-16/experiment_provenance.json')
    assert receipt['success'] and receipt['python'] == sys.executable
    assert receipt['method_probes']['pilot']['target_call_counts']['M1'] > 0
    assert receipt['method_probes']['observe']['target_call_counts']['M1'] > 0
    assert len(receipt['method_probes']['receipt_hashes']) == 2
    assert all(path in paths for path in receipt['method_probes']['receipt_hashes'])
    assert audit_method_execution(tmp_path, engine.work, plan, receipt)['passed']
    assert llm.calls == 0
    plan['method_steps'][0]['operation'] = '(u,-u,x-z)'
    assert not audit_method_execution(tmp_path, engine.work, plan, receipt)['passed']


def test_incomplete_pilot_timeout_retries_unchanged_without_a_model_repair_or_budget_reset(tmp_path, monkeypatch):
    from researchclaw.pipeline.claw_engine.tools.executor import ToolExecutor
    import researchclaw.orchestration.method_execution as execution
    engine, plan, llm = fixture(tmp_path, runtime_seconds=2400)
    original = ToolExecutor.execute_structured
    limits, clock = [], [0.]
    monkeypatch.setattr(execution, 'time', SimpleNamespace(monotonic=lambda: clock[0]))
    before = (engine.work/'scientific_methods.py').read_bytes()
    held = (tmp_path/'stage-09/method_probes/manifest.json').read_bytes()
    def run(executor, tool, args):
        limits.append(executor.bash_timeout)
        if len(limits) == 1:
            clock[0] += 120
            return SimpleNamespace(is_error=True, timed_out=True, exit_code=-15)
        result = original(executor, tool, args)
        clock[0] += 1
        return result
    monkeypatch.setattr(ToolExecutor, 'execute_structured', run)
    engine.execute()
    receipt = read_json(tmp_path/'stage-16/experiment_provenance.json')
    assert receipt['success'] and llm.calls == 0
    assert limits == [120., 120., 2279.]
    assert [row['timed_out'] for row in receipt['method_probes']['pilot_attempts']] == [True, False]
    assert len(receipt['method_probes']['receipt_hashes']) == 2
    assert (engine.work/'scientific_methods.py').read_bytes() == before
    assert (tmp_path/'stage-09/method_probes/manifest.json').read_bytes() == held
    assert audit_method_execution(tmp_path, engine.work, plan, receipt)['passed']


def test_repeated_pilot_timeouts_do_not_force_uninformed_scientific_source_edits(tmp_path, monkeypatch):
    from researchclaw.pipeline.claw_engine.tools.executor import ToolExecutor
    engine, _, llm = fixture(tmp_path, replies=[{'should_not_be_requested': True}], runtime_seconds=2400)
    calls = []
    before = (engine.work/'scientific_methods.py').read_bytes()
    def timeout(executor, tool, args):
        calls.append(args['command'])
        return SimpleNamespace(is_error=True, timed_out=True, exit_code=-15)
    monkeypatch.setattr(ToolExecutor, 'execute_structured', timeout)
    with pytest.raises(ContractError, match='source left unchanged'):
        engine.execute()
    assert len(calls) == 2 and llm.calls == 0
    assert (engine.work/'scientific_methods.py').read_bytes() == before
    assert not (tmp_path/'stage-16/experiment_provenance.json').exists()
    receipt = read_json(next((tmp_path/'research_attempts').glob('*/receipt.json')))
    assert receipt['success'] is False and receipt['timed_out'] is True


def test_copied_historical_receipts_verify_only_as_an_explicit_read_only_snapshot(tmp_path):
    origin = tmp_path/'original'
    engine, plan, llm = fixture(origin)
    engine.execute()
    copied = tmp_path/'copied'
    shutil.copytree(origin, copied)
    receipt = read_json(copied/'stage-16/experiment_provenance.json')
    work = copied/'stage-11/experiment'
    manifest = copied/'stage-09/method_probes/manifest.json'
    original_bytes = manifest.read_bytes()
    assert not audit_method_execution(copied, work, plan, receipt)['passed']
    # The offline check must read the snapshot, never recover current source
    # from the old path or rerun a historical scientific computation.
    shutil.rmtree(origin/'stage-11/experiment')
    assert audit_method_execution(copied, work, plan, receipt, evidence_origin_root=origin)['passed']
    assert manifest.read_bytes() == original_bytes and llm.calls == 0
    assert not audit_method_execution(copied, work, plan, receipt, evidence_origin_root=tmp_path/'other')['passed']
    (work/'scientific_methods.py').write_text(WRONG)
    assert not audit_method_execution(copied, work, plan, receipt, evidence_origin_root=origin)['passed']


def test_copied_snapshot_cannot_change_held_oracles_or_execution_receipts(tmp_path):
    origin = tmp_path/'original'
    engine, plan, _ = fixture(origin)
    engine.execute()
    copied = tmp_path/'copied'
    shutil.copytree(origin, copied)
    receipt = read_json(copied/'stage-16/experiment_provenance.json')
    work = copied/'stage-11/experiment'
    probe = next((copied/'stage-09/method_probes').glob('probe-*.py'))
    before = probe.read_bytes()
    probe.write_text('def check(invoke):\n    assert invoke(1,0,-1) == (0,0,0)\n')
    assert not audit_method_execution(copied, work, plan, receipt, evidence_origin_root=origin)['passed']
    probe.write_bytes(before)
    path = copied/next(iter(receipt['method_probes']['receipt_hashes']))
    path.write_text('{"passed":true}')
    assert not audit_method_execution(copied, work, plan, receipt, evidence_origin_root=origin)['passed']


def test_wrong_sign_fails_before_running_the_full_experiment(tmp_path):
    engine, _, _ = fixture(tmp_path, WRONG)
    with pytest.raises(ContractError, match='Held method pilot failed'):
        engine.execute()
    assert not (engine.work/'outputs/metrics.json').exists()
    receipts = list((tmp_path/'research_attempts').glob('*/receipt.json'))
    assert len(receipts) == 1 and read_json(receipts[0])['success'] is False


def test_a_disconnected_correct_helper_cannot_validate_the_full_run(tmp_path):
    engine, _, _ = fixture(tmp_path, used=False)
    with pytest.raises(ContractError, match='Full-run method linkage failed'):
        engine.execute()
    receipt = read_json(next((tmp_path/'research_attempts').glob('*/receipt.json')))
    assert receipt['method_probes']['pilot']['passed']
    assert not receipt['success']


def test_local_source_repair_keeps_the_held_scientific_oracle(tmp_path):
    patch = {'edits': [{'path': 'scientific_methods.py', 'sha256': hashlib.sha256(WRONG.encode()).hexdigest(),
                       'old': 'return u, -u, x-z', 'new': 'return u, -u, z-x'}]}
    engine, _, llm = fixture(tmp_path, WRONG, replies=[patch])
    manifest = tmp_path/'stage-09/method_probes/manifest.json'
    before = manifest.read_bytes()
    engine.execute()
    assert manifest.read_bytes() == before and llm.calls == 1
    receipts = [read_json(path) for path in (tmp_path/'research_attempts').glob('*/receipt.json')]
    assert len(receipts) == 2 and sum(row['success'] for row in receipts) == 1


def test_extracted_pdf_text_has_a_separate_source_identity(tmp_path):
    original, text = tmp_path/'paper.pdf', tmp_path/'paper.md'
    original.write_bytes(b'%PDF original source bytes')
    text.write_text(TASK)
    _, plan, _ = fixture(tmp_path/'run')
    plan['method_steps'][0]['source']['reference'] = str(original)
    references = [{'source': str(original), 'text_path': str(text),
        'sha256': hashlib.sha256(original.read_bytes()).hexdigest(),
        'text_sha256': hashlib.sha256(text.read_bytes()).hexdigest()}]
    bound = validate_method_sources(plan, TASK, references)
    assert bound[0]['source_sha256'] == references[0]['text_sha256']


def test_same_timestamp_same_size_bytecode_cannot_replace_the_verified_source(tmp_path):
    engine, _, _ = fixture(tmp_path, WRONG)
    source = engine.work/'scientific_methods.py'
    previous = source.stat()
    py_compile.compile(str(source), doraise=True)
    source.write_text(CORRECT)
    os.utime(source, ns=(previous.st_atime_ns, previous.st_mtime_ns))
    engine.execute()
    assert read_json(tmp_path/'stage-16/experiment_provenance.json')['success']
