"""Data preview must not call nested observations absent or flatten unlike schemas."""
from dataclasses import replace
import json
from types import SimpleNamespace

from researchclaw.orchestration.tabular_profile import table_catalog
from researchclaw.orchestration.workspace import profile_workspace


def test_nested_tables_keep_schema_samples_and_path_metadata(tmp_path):
    raw = tmp_path / 'data/input/3_region/method/observations.csv'
    raw.parent.mkdir(parents=True)
    raw.write_text('start_dates,end_dates,changes,unit\n2000.75,2001.75,-2.1,mwe\n2001.75,2002.75,-1.2,mwe\n')
    result = tmp_path / 'data/output/annual.csv'
    result.parent.mkdir(parents=True)
    result.write_text('year,region,value\n2001,3,-2.1\n')
    catalog = table_catalog(tmp_path / 'data')
    assert catalog['table_files_seen'] == 2 and catalog['schema_groups_seen'] == 2
    group = next(g for g in catalog['schema_groups'] if 'start_dates' in g['columns'])
    assert group['examples'][0]['path'] == 'input/3_region/method/observations.csv'
    assert group['examples'][0]['rows'][0] == ['2000.75', '2001.75', '-2.1', 'mwe']
    assert 'missing inline metadata' in catalog['guidance']


def test_table_catalog_covers_families_beyond_the_display_inventory_limit(tmp_path):
    data = tmp_path / 'data'
    data.mkdir()
    for n in range(125):
        (data / f'{n:03}.csv').write_text('value,unit\n1,m\n')
    nested = data / 'deep/input/region/special.csv'
    nested.parent.mkdir(parents=True)
    nested.write_text('start,end,estimate\n2000.5,2001.5,2\n')
    config = SimpleNamespace(experiment=SimpleNamespace(datasets_dir=str(data), codebases_dir='', checkpoints_dir=''))
    profile = profile_workspace(tmp_path / 'run', config)
    dataset = profile['inputs'][0]
    assert len(dataset['files']) == 120 and dataset['inventory_truncated']
    assert dataset['table_catalog']['table_files_seen'] == 126
    assert dataset['table_catalog']['schema_groups_seen'] == 2


def test_profile_reaches_primary_files_beyond_two_directory_levels(tmp_path):
    data = tmp_path / 'data'
    path = data / 'catalog/input/region/primary.csv'
    path.parent.mkdir(parents=True)
    path.write_text('changes,unit\n-1.0,Gt\n')
    config = SimpleNamespace(experiment=SimpleNamespace(datasets_dir=str(data), codebases_dir='', checkpoints_dir=''))
    dataset = profile_workspace(tmp_path / 'run', config)['inputs'][0]
    assert dataset['files'][0]['path'] == 'catalog/input/region/primary.csv'
    assert dataset['files'][0]['columns'] == ['changes', 'unit']


def test_catalog_excludes_hidden_evaluation_inputs_and_symlinked_directories(tmp_path):
    (tmp_path / 'visible.csv').write_text('value\n1\n')
    for folder in ('target_study', '.private'):
        (tmp_path / folder).mkdir()
        (tmp_path / folder / 'not_visible.csv').write_text('forbidden\nsecret\n')
    (tmp_path / 'linked').symlink_to(tmp_path / 'target_study')
    result = table_catalog(tmp_path)
    assert result['table_files_seen'] == 1
    assert 'forbidden' not in json.dumps(result)


def test_catalog_marks_depth_and_scan_limits_without_claiming_absence(tmp_path):
    path = tmp_path / 'one/two/three/four/data.csv'
    path.parent.mkdir(parents=True)
    path.write_text('value\n1\n')
    assert table_catalog(tmp_path, max_depth=1)['scan_truncated']
    assert table_catalog(tmp_path, max_entries=1)['scan_truncated']


def test_adjacent_data_definitions_keep_units_and_lineage_within_a_budget(tmp_path):
    (tmp_path / 'primary.csv').write_text('start,end,changes\n2000.75,2001.75,-1\n')
    (tmp_path / 'DATASETS_INFO.md').write_text('Dates are fractional years; changes are interval totals in m or Gt.')
    (tmp_path / 'README.md').write_text('A' * 10000)
    (tmp_path / 'private').mkdir()
    (tmp_path / 'private/metadata.md').write_text('SECRET')
    catalog = table_catalog(tmp_path)
    docs = {d['path']: d for d in catalog['documentation']}
    assert 'fractional years' in docs['DATASETS_INFO.md']['text']
    assert docs['README.md']['truncated'] and len(docs['README.md']['text']) <= 3000
    assert sum(len(d['text']) for d in docs.values()) <= 8000
    assert 'SECRET' not in json.dumps(catalog)
