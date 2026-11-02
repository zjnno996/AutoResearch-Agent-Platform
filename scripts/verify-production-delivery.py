#!/usr/bin/env python3
"""Independently replay the fixed linear public-deployment acceptance task."""
import csv
import hashlib
import json
import math
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import time
from zipfile import ZipFile

ROOT = Path(__file__).resolve().parents[1]
ARTIFACTS = ROOT/'.local/production/verification'


def read(path):
    return json.loads(path.read_text())


def close(actual, expected):
    assert type(actual) in (int,float) and math.isfinite(actual)
    assert math.isclose(actual, expected, rel_tol=1e-10, abs_tol=1e-10), (actual,expected)


def check_outputs(directory):
    expected = {'a':3.0,'b':-2.0,'train_mse':0.0,'test_mse':0.0,'test_r2':1.0,'baseline_test_mse':150.0}
    metrics = read(directory/'metrics.json')
    for key,value in expected.items():
        close(metrics[key],value)
    with (directory/'predictions.csv').open() as handle:
        rows = list(csv.DictReader(handle))
    assert len(rows)==8
    assert sorted(float(row['x']) for row in rows)==list(range(-3,5))
    aliases={'train':'train','training':'train','训练':'train','训练集':'train',
             'test':'test','testing':'test','测试':'test','测试集':'test'}
    splits=[aliases[str(row['split']).strip().lower()] for row in rows]
    assert splits.count('train')==5 and splits.count('test')==3
    for row,split in zip(rows,splits):
        x=float(row['x']);y=float(row['y'])
        assert split==('train' if x<=1 else 'test')
        close(y,3*x-2);close(float(row['prediction']),y);close(float(row['baseline_prediction']),-5.0)
    from PIL import Image
    images=[]
    for path in sorted(directory.rglob('*.png')):
        with Image.open(path) as image:
            image.load()
            assert image.format=='PNG' and image.width>=200 and image.height>=150
            assert image.convert('RGB').getextrema()!=((255,255),(255,255),(255,255))
        images.append(path.name)
    assert len(images)>=2
    return metrics,images


def main():
    run=read(ARTIFACTS/'research-run.json')
    assert run['topic'].startswith('[PROD-E2E-20261102')
    project=ROOT/'backend/runs/production/projects'/run['id']
    state=read(project/'compact_state.json')
    assert all(state['phases'][name]['status']=='done' for name in ('ground','plan','implement','execute','report','verify'))
    assert read(project/'checkpoint.json')['last_completed_stage']==26
    scientific=read(project/'research_verification.json')
    assert scientific['all_satisfied'] is True and scientific['summary']['failed']==0
    receipt=read(project/'stage-16/experiment_provenance.json')
    assert receipt['success'] is True and receipt['returncode']==0
    work=project/'stage-11/experiment'
    for field in ('source_hashes','output_hashes'):
        assert receipt[field]
        for name,digest in receipt[field].items():
            path=(work/name).resolve()
            assert path.is_relative_to(work.resolve()) and not path.is_symlink()
            assert hashlib.sha256(path.read_bytes()).hexdigest()==digest,name
    original,images=check_outputs(work/'outputs')
    report=ARTIFACTS/'research-report.md'
    assert report.read_bytes()==(project/'stage-25/paper_final.md').read_bytes()
    with ZipFile(ARTIFACTS/'outputs.zip') as archive:
        assert json.loads(archive.read('metrics.json'))==original
    with tempfile.TemporaryDirectory(prefix='emnets-production-replay-') as temporary:
        directory=Path(temporary)
        with ZipFile(ARTIFACTS/'experiment.zip') as archive:
            for entry in archive.infolist():
                assert (directory/entry.filename).resolve().is_relative_to(directory.resolve())
                assert entry.external_attr >> 16 & 0o170000 != 0o120000
            archive.extractall(directory)
        for name,digest in receipt['source_hashes'].items():
            assert hashlib.sha256((directory/name).read_bytes()).hexdigest()==digest
        shutil.rmtree(directory/'outputs')
        import yaml
        from researchclaw.orchestration.compact_contracts import execution_arguments
        arguments,_=execution_arguments(yaml.safe_load((project/'stage-09/exp_plan.yaml').read_text()))
        environment={k:os.environ[k] for k in ('PATH','HOME','LANG','LC_ALL') if k in os.environ}
        environment.update(OPENBLAS_NUM_THREADS='1',OMP_NUM_THREADS='1',MKL_NUM_THREADS='1',PYTHONNOUSERSITE='1')
        started=time.monotonic()
        replay=subprocess.run([str(ROOT/'.venv-web/bin/python'),'main.py',*arguments],cwd=directory,
                              env=environment,capture_output=True,text=True,timeout=180)
        assert replay.returncode==0,replay.stderr[-3000:]
        reproduced,_=check_outputs(directory/'outputs')
        for key in ('a','b','train_mse','test_mse','test_r2','baseline_test_mse'):
            close(reproduced[key],original[key])
        seconds=round(time.monotonic()-started,3)
    import fitz
    with fitz.open(ARTIFACTS/'research-report.pdf') as pdf:
        assert len(pdf)>0
        pages=len(pdf)
    result={'id':run['id'],'six_phases_complete':True,'scientifically_accepted':True,
            'requirements_passed':scientific['summary']['passed'],'receipt_returncode':0,
            'source_hashes_verified':len(receipt['source_hashes']),'output_hashes_verified':len(receipt['output_hashes']),
            'metrics':original,'csv_rows':8,'images':images,'independent_archive_replay':True,
            'independent_replay_seconds':seconds,'pdf_pages':pages,'official_benchmark_score':None}
    (ARTIFACTS/'delivery-verification.json').write_text(json.dumps(result,ensure_ascii=False,indent=2)+'\n')
    print(json.dumps(result,ensure_ascii=False))


if __name__=='__main__':
    main()
