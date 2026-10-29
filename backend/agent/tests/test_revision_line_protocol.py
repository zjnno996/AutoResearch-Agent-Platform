from pathlib import Path

import pytest

from researchclaw.orchestration.artifacts import write_json
from researchclaw.orchestration.compact_contracts import ContractError
from researchclaw.orchestration.compact_revision import apply_edits, digest, numbered_packet, publish
from researchclaw.orchestration.executed_evidence import executed_packet


def line_edit(text, start, end, new, name='main.py'):
    return {'path': name, 'sha256': digest(text), 'start_line': start, 'end_line': end, 'new': new}


def test_original_addresses_survive_other_edits_and_keep_unedited_bytes(tmp_path):
    original = '# 汉字\r\nVALUE = 1\r\nMIDDLE = 2\r\nTAIL = 3\r\n'
    (tmp_path / 'main.py').write_bytes(original.encode())
    packet, visible = numbered_packet({'main.py': original})
    assert 'L4: TAIL = 3' in packet
    payload = {'line_edits': [line_edit(original, 2, 2, 'VALUE = 4\r\nADDED = 5'),
                              line_edit(original, 4, 4, 'TAIL = 6')]}
    changed = apply_edits(tmp_path, {'main.py': original}, payload, visible_lines=visible)
    expected = '# 汉字\r\nVALUE = 4\r\nADDED = 5\r\nMIDDLE = 2\r\nTAIL = 6\r\n'
    assert changed == {'main.py': expected}
    publish(tmp_path, {'main.py': original}, changed, journal_root=tmp_path/'journals', kind='source', context='one')
    assert (tmp_path/'main.py').read_bytes() == expected.encode()


def test_line_address_edits_do_not_need_a_hallucinated_old_sentence(tmp_path):
    original = '# Findings\n- The discrete formalization is marked partial.\n# Retained result\n'
    packet, visible = numbered_packet({'paper_final.md': original})
    changed = apply_edits(tmp_path, {'paper_final.md': original},
        {'line_edits': [line_edit(original, 2, 2, '- A rigorous written argument is provided; no machine certificate is claimed.', 'paper_final.md')]},
        visible_lines=visible)
    assert changed['paper_final.md'].endswith('# Retained result\n')
    assert 'L2:' in packet and 'no machine certificate' in changed['paper_final.md']


def test_many_local_fixes_do_not_fail_an_arbitrary_24_edit_boundary(tmp_path):
    original=''.join(f'VALUE_{i} = 0\n' for i in range(40))
    _, visible=numbered_packet({'main.py':original})
    edits=[line_edit(original,i+1,i+1,f'VALUE_{i} = 1') for i in range(27)]
    changed=apply_edits(tmp_path,{'main.py':original},{'line_edits':edits},visible_lines=visible)
    assert changed['main.py'].count(' = 1') == 27 and changed['main.py'].endswith('VALUE_39 = 0\n')


@pytest.mark.parametrize('fault', ['stale', 'overlap', 'invisible', 'bounds', 'syntax', 'mixed', 'traversal', 'boolean'])
def test_invalid_line_edits_never_modify_a_file(tmp_path, fault):
    text = 'A = 1\nB = 2\nC = 3\n'
    (tmp_path/'main.py').write_text(text)
    _, visible = numbered_packet({'main.py': text})
    item = line_edit(text, 2, 2, 'B = 4')
    payload = {'line_edits': [item]}
    if fault == 'stale': item['sha256'] = '0'*64
    if fault == 'overlap': payload['line_edits'].append(line_edit(text, 1, 2, 'A = 2'))
    if fault == 'invisible': visible = {'main.py': [(1, 1), (3, 3)]}
    if fault == 'bounds': item['end_line'] = 5
    if fault == 'syntax': item['new'] = 'def broken('
    if fault == 'traversal': item['path'] = '../main.py'
    if fault == 'boolean': item['start_line'] = True
    if fault == 'mixed': payload['edits'] = [{'path':'main.py', 'sha256':digest(text), 'old':'A = 1', 'new':'A = 7'}]
    with pytest.raises(ContractError):
        apply_edits(tmp_path, {'main.py': text}, payload, visible_lines=visible)
    assert (tmp_path/'main.py').read_text() == text


def test_context_budget_cannot_authorize_hidden_line_ranges(tmp_path):
    original = ''.join(f'VALUE_{i} = {i}\n' for i in range(2000))
    packet, visible = numbered_packet({'main.py': original}, 2000)
    assert len(packet) <= 2000 and len(visible['main.py']) == 2
    hidden = visible['main.py'][0][1] + 1
    with pytest.raises(ContractError, match='omitted'):
        apply_edits(tmp_path, {'main.py': original}, {'line_edits':[line_edit(original, hidden, hidden, 'VALUE = 1')]}, visible_lines=visible)


def test_evidence_packet_reads_actual_proof_and_comparison_artifacts(tmp_path):
    work = tmp_path/'stage-11/experiment'
    files = {'outputs/proofs/check.py':'ASSERT_IDENTITY = True\n',
             'outputs/proofs/certificate.json':'{"scope":"algebra only","passed":true}',
             'outputs/comparison.csv':'method,bias\nA,1\n', 'outputs/metrics.json':'{"residual":0}'}
    for relative, content in files.items():
        path=work/relative;path.parent.mkdir(parents=True,exist_ok=True);path.write_text(content)
    write_json(tmp_path/'stage-16/experiment_provenance.json', {'output_hashes':{n:digest(t) for n,t in files.items()}})
    packet, included = executed_packet(tmp_path)
    assert 'ASSERT_IDENTITY = True' in packet and 'algebra only' in packet and 'method,bias' in packet
    assert len(packet) <= 18000 and len(included) == 3
    (work/'outputs/proofs/check.py').write_text('TAMPERED')
    packet, included = executed_packet(tmp_path)
    assert 'TAMPERED' not in packet and work/'outputs/proofs/check.py' not in included


def test_planned_proof_file_cannot_be_confused_with_an_executed_artifact(tmp_path):
    path=tmp_path/'stage-09/exp_plan.yaml';path.parent.mkdir(parents=True)
    path.write_text('requirement_map:\n  R1:\n    artifact: outputs/proofs/\n')
    write_json(tmp_path/'stage-16/experiment_provenance.json', {'output_hashes':{}})
    packet, included = executed_packet(tmp_path)
    assert '"declared_but_not_recorded": ["outputs/proofs/"]' in packet and not included


def test_output_receipt_cannot_make_evidence_read_private_inputs(tmp_path):
    work=tmp_path/'stage-11/experiment';work.mkdir(parents=True)
    private=tmp_path/'target_study';private.mkdir();(private/'private.txt').write_text('FORBIDDEN')
    (work/'outputs').symlink_to(private,target_is_directory=True)
    write_json(tmp_path/'stage-16/experiment_provenance.json', {'output_hashes':{'outputs/private.txt':digest('FORBIDDEN')}})
    packet, paths = executed_packet(tmp_path)
    assert 'FORBIDDEN' not in packet and not paths
