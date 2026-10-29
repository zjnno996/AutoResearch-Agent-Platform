from pathlib import Path
import shutil
from types import SimpleNamespace

import pytest
from PIL import Image

from researchclaw.benchmarks.partial_recovery import retained_review_audit
from researchclaw.orchestration.artifacts import read_json, write_json
from researchclaw.orchestration.compact_generation import record_sources, source_files
from researchclaw.orchestration.compact_state import file_hash


@pytest.fixture
def reviewed(tmp_path):
    tmp_path = tmp_path / 'reviewed-run'
    tmp_path.mkdir()
    work = tmp_path/'stage-11/experiment'
    work.mkdir(parents=True)
    (work/'main.py').write_text("print('actual execution fixture')\n")
    record_sources(work,['main.py'])
    output=work/'outputs';output.mkdir()
    (output/'metrics.json').write_text('{"residual":0}')
    (output/'analysis.md').write_text('Measured residual; scientific proof gap remains.')
    Image.new('RGB',(30,30),'blue').save(output/'result.png')
    write_json(tmp_path/'stage-16/experiment_provenance.json', {
        'success':True,'returncode':0,
        'source_hashes':{p.relative_to(work).as_posix():file_hash(p) for p in source_files(work)},
        'output_hashes':{p.relative_to(work).as_posix():file_hash(p) for p in output.iterdir()}})
    report=tmp_path/'stage-25/paper_final.md';report.parent.mkdir()
    report.write_text('# Scientific result\n'+'Measured result, with unresolved proof. '*15+'\n![Actual](../stage-11/experiment/outputs/result.png)')
    plan=tmp_path/'stage-09/exp_plan.yaml';plan.parent.mkdir();plan.write_text('protocol:\n  command: python main.py\n')
    write_json(tmp_path/'compact_contract.json',{'requirements':[{'id':'R1','requirement':'Prove the result','acceptance':'Valid argument'}]})
    review={'criteria':[{'id':'R1','status':'unmet','reason':'Proof incomplete'}],
            'missing_requirements':[],'quality_score':6,'summary':'Partial research, not accepted'}
    write_json(tmp_path/'compact_review.json',review)
    write_json(tmp_path/'research_verification.json',{'all_satisfied':False})
    tracked=[report,plan,tmp_path/'compact_contract.json',tmp_path/'stage-16/experiment_provenance.json',
             *source_files(work),*output.iterdir()]
    write_json(tmp_path/'research_reviews/review-one/review.json',
               {'review':review,'artifact_hashes':{p.relative_to(tmp_path).as_posix():file_hash(p) for p in tracked}})
    return tmp_path


def test_rejected_revision_can_retain_only_the_same_reviewed_partial_evidence(reviewed):
    before=(reviewed/'stage-25/paper_final.md').read_bytes()
    assert retained_review_audit(reviewed)['eligible']
    assert (reviewed/'stage-25/paper_final.md').read_bytes()==before
    assert read_json(reviewed/'research_verification.json')['all_satisfied'] is False


@pytest.mark.parametrize('change',['report','source','output','review','archive','science'])
def test_missing_or_changed_reviewed_evidence_cannot_be_recovered_as_scientific_partial(reviewed,change):
    if change=='report':(reviewed/'stage-25/paper_final.md').write_text('changed')
    if change=='source':(reviewed/'stage-11/experiment/main.py').write_text("print('new source')")
    if change=='output':(reviewed/'stage-11/experiment/outputs/metrics.json').write_text('{"residual":1}')
    if change=='review':write_json(reviewed/'compact_review.json',{'criteria':[]})
    if change=='archive':(reviewed/'research_reviews/review-one/review.json').unlink()
    if change=='science':(reviewed/'stage-25/scientific_analysis.md').write_text('Unreviewed new theorem')
    assert not retained_review_audit(reviewed)['eligible']


@pytest.mark.parametrize('mode',['retained','changed_input','missing_archive'])
def test_native_adapter_exports_retained_partial_without_marking_failed_revision_passed(reviewed,tmp_path,monkeypatch,mode):
    from researchclaw.benchmarks import rcbench
    from researchclaw.pipeline.models import StageResult
    from researchclaw.pipeline.stages import Stage, StageStatus
    repo=tmp_path/'official';task=repo/'tasks/Math_001'
    (task/'data').mkdir(parents=True);(task/'data/input.csv').write_text('x\n1\n')
    write_json(task/'task_info.json', {'task':'Prove the stated result','data':[]})
    (task/'related_work').mkdir();(repo/'evaluation').mkdir()
    (repo/'evaluation/run_task.py').write_text('# official runner fixture')
    config=tmp_path/'agent.yaml'
    config.write_text('project:\n  name: recovery\nresearch:\n  topic: Prove result\nruntime:\n  timezone: UTC\nnotifications:\n  channel: local\nknowledge_base:\n  root: kb\nllm:\n  api_key_env: UNUSED_TEST_KEY\nresearch_engine:\n  enabled: true\n')
    class Runner:
        def __init__(self,task_id,agent_cmd,agent_name):self.task_id=task_id;self.agent_name=agent_name
        def setup_workspace(self):
            self.workspace.mkdir(parents=True)
            for name in ['data','related_work']:shutil.copytree(task/name,self.workspace/name)
            self.instructions_path.write_text('Prove the stated result and generate an actual numerical illustration.')
        def _write_meta(self,status,extra):write_json(self.meta_path,{'status':status,'task_id':self.task_id,**extra})
    monkeypatch.setattr(rcbench,'_official',lambda *args:SimpleNamespace(TaskRunner=Runner))
    monkeypatch.setattr(rcbench,'revision',lambda *args:'fixed-upstream')
    monkeypatch.setattr('researchclaw.llm.create_llm_client',lambda config:SimpleNamespace(preflight=lambda:(True,'OK')))
    workspace=rcbench.prepare(repo,'Math_001',config)
    def pipeline(**kwargs):
        run=kwargs['run_dir'];shutil.copytree(reviewed,run,dirs_exist_ok=True)
        write_json(run/'compact_state.json',{'phases':{'report':{'status':'failed','error':'ContractError: Rejected local candidate','failure_kind':'contract_or_execution'}}})
        if mode=='changed_input':(workspace/'data/input.csv').write_text('changed original input')
        if mode=='missing_archive':shutil.rmtree(run/'research_reviews')
        return [StageResult(Stage(25),StageStatus.FAILED,(),error='Rejected local candidate')]
    monkeypatch.setattr('researchclaw.pipeline.runner.execute_pipeline',pipeline)
    code=rcbench.run_agent(workspace,config)
    meta=read_json(workspace/'_meta.json')
    assert code==(0 if mode=='retained' else 1)
    assert meta['status']==('completed_with_gaps' if mode=='retained' else 'failed')
    assert meta['failed_stage']==25 and meta.get('internal_verification') is not True
    if mode=='retained':
        assert meta['pipeline_status']=='failed' and meta['scientific_status']=='gaps'
        assert meta['revision_status']=='failed_candidate_retained_original'
        assert (workspace/'report/report.md').is_file()
        assert read_json(workspace/'.claw/run/compact_state.json')['phases']['report']['status']=='failed'
