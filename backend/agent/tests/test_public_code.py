"""Bounded public-code acquisition, with no live HTTP or model calls."""
import base64
import copy
from dataclasses import replace
import hashlib
import io
import json
import os
from pathlib import Path
import tarfile
from types import SimpleNamespace
import urllib.error

import pytest

from researchclaw.orchestration import public_code


COMMIT, TREE = 'a' * 40, 'b' * 40
REPO = 'https://github.com/public-science/example-method'


def blob_sha(raw):
    return hashlib.sha1(b'blob ' + str(len(raw)).encode() + b'\0' + raw).hexdigest()


def source(url=REPO, **kwargs):
    return {'source': 'https://example.org/paper', 'origin': 'public_primary_source',
            'public_code_urls': [url], **kwargs}


class Response(io.BytesIO):
    def __init__(self, raw, url):
        super().__init__(raw)
        self.url = url

    def geturl(self):
        return self.url


@pytest.fixture
def server(monkeypatch):
    files = {'README.md': b'Public scientific method. See configuration and source.\n',
             'configuration/region.yaml': b'selection:\n  excluded_observations: [low_confidence]\n',
             'src/combine.py': b'def propagated_uncertainty(x, covariance):\n    vector = [x, 1]\n    return vector, covariance\n',
             'tests/test_outputs.py': b'assert result == expected_value\n'}
    entries = [{'path': path, 'type': 'blob', 'mode': '100644', 'size': len(raw), 'sha': blob_sha(raw)}
               for path, raw in files.items()]
    entries.extend([{'path': 'unsafe.py', 'type': 'blob', 'mode': '120000', 'size': 10, 'sha': 'c' * 40},
                    {'path': '../escape.py', 'type': 'blob', 'mode': '100644', 'size': 10, 'sha': 'd' * 40}])
    state = {'requests': [], 'files': files, 'entries': entries, 'truncated': False, 'bad_blob': False,
             'redirect': False, 'http_error': None, 'bad_commit': False, 'api_rate_limit': False}

    class Opener:
        def open(self, request, timeout):
            assert 0 < timeout <= 8
            headers = {key.casefold(): value for key, value in request.header_items()}
            assert not {'authorization', 'cookie', 'proxy-authorization'} & headers.keys()
            url = request.full_url
            assert url.startswith(('https://api.github.com/repos/', 'https://github.com/', 'https://codeload.github.com/'))
            state['requests'].append(url)
            if state['http_error'] or (state['api_rate_limit'] and url.startswith('https://api.github.com/')):
                raise urllib.error.HTTPError(url, state['http_error'] or 403, 'public error', {}, None)
            if '.git/info/refs?' in url:
                line = COMMIT.encode() + b' HEAD\0multi_ack\n'
                return Response(f'{len(line) + 4:04x}'.encode() + line + b'0000', url)
            if url.startswith('https://codeload.github.com/'):
                assert url.endswith('/tar.gz/' + COMMIT)
                buffer = io.BytesIO()
                with tarfile.open(fileobj=buffer, mode='w:gz') as archive:
                    for name, raw in state['files'].items():
                        member = tarfile.TarInfo('repo-' + COMMIT + '/' + name)
                        member.size = len(raw)
                        archive.addfile(member, io.BytesIO(raw))
                    link = tarfile.TarInfo('repo-' + COMMIT + '/unsafe.py')
                    link.type, link.linkname = tarfile.SYMTYPE, '/etc/passwd'
                    archive.addfile(link)
                return Response(buffer.getvalue(), url)
            if url.endswith('/commits?per_page=1'):
                value = [{'sha': COMMIT, 'commit': {'tree': {'sha': TREE}}}] if not state['bad_commit'] else [{'commit': []}]
            elif '/git/trees/' in url:
                assert url.endswith(TREE + '?recursive=1')
                value = {'sha': TREE, 'tree': state['entries'], 'truncated': state['truncated']}
            else:
                sha = url.rsplit('/', 1)[-1]
                raw = next(raw for raw in state['files'].values() if blob_sha(raw) == sha)
                value = {'sha': sha, 'encoding': 'base64', 'content': base64.b64encode(raw).decode()}
                if state['bad_blob']:
                    value['content'] = base64.b64encode(b'wrong bytes').decode()
            return Response(json.dumps(value).encode(), 'https://not-github.example/content' if state['redirect'] else url)

    def opener(*handlers):
        assert any(isinstance(handler, public_code._NoRedirect) for handler in handlers)
        return Opener()
    monkeypatch.setattr(public_code.urllib.request, 'build_opener', opener)
    return state


@pytest.mark.parametrize('value', [
    'http://github.com/owner/repo', 'https://github.com.evil/owner/repo',
    'https://user:secret@github.com/owner/repo', 'https://github.com:444/owner/repo',
    'https://api.github.com/repos/owner/repo', 'https://github.com/owner/repo?token=secret',
    'https://github.com/owner/repo/tree/main', 'file:///root/private',
])
def test_repository_url_rejects_nonpublic_or_nonroot_urls(value):
    assert public_code.repository_url(value) == ''


def test_acquisition_pins_commit_blobs_and_retains_config_and_source(tmp_path, server):
    rows = [source()]
    audit = public_code.acquire_public_code(tmp_path, rows, 'selection covariance uncertainty')
    evidence = rows[0]['public_code']
    assert evidence['status'] == audit['status'] == 'available'
    assert evidence['commit_sha'] == COMMIT and evidence['tree_sha'] == TREE
    assert {row['path'] for row in evidence['files']} == {'README.md', 'configuration/region.yaml', 'src/combine.py'}
    packet = Path(evidence['packet_path']).read_text()
    assert 'excluded_observations' in packet and 'vector = [x, 1]' in packet
    assert 'not imported, installed or executed' in packet
    assert 'not certified as the paper version' in packet
    assert len(packet) <= public_code.CodeLimits().context_chars
    assert len(server['requests']) == 5
    assert hashlib.sha256(packet.encode()).hexdigest() == evidence['packet_sha256']
    for record in evidence['files']:
        raw = Path(record['local_path']).read_bytes()
        assert record['blob_sha'] == blob_sha(raw)
        assert record['raw_sha256'] == hashlib.sha256(raw).hexdigest()
        assert record['complete_file_in_context']
        assert '/blob/' + COMMIT + '/' in record['source_url']
    for request in audit['requests']:
        assert hashlib.sha256(Path(request['raw_path']).read_bytes()).hexdigest() == request['raw_sha256']
    assert audit['llm_calls'] == 0


def test_only_one_explicit_primary_repository_and_identity_priority(tmp_path, server):
    rows = [source('https://github.com/other/library'), source(task_identity_matches=['NamedMethod']),
            source('https://github.com/untrusted/repo', origin='local_notes')]
    public_code.acquire_public_code(tmp_path, rows, 'NamedMethod')
    assert rows[1]['public_code']['commit_sha'] == COMMIT
    assert rows[0]['public_code']['status'] == rows[2]['public_code']['status'] == 'unavailable'
    assert all('/public-science/example-method/' in url for url in server['requests'])


def test_missing_link_is_explicit_and_makes_no_http_calls(tmp_path, server):
    rows = [source('https://evil.example/repo')]
    audit = public_code.acquire_public_code(tmp_path, rows, 'method')
    assert audit['status'] == rows[0]['public_code']['status'] == 'unavailable'
    assert not server['requests']


@pytest.mark.parametrize('limit,expected_requests', [('requests', 2), ('total_bytes', 1)])
def test_finite_http_and_byte_budgets(tmp_path, server, limit, expected_requests):
    limits = replace(public_code.CodeLimits(), **{limit: 2 if limit == 'requests' else 20})
    rows = [source()]
    audit = public_code.acquire_public_code(tmp_path, rows, 'method', limits=limits)
    assert audit['status'] == rows[0]['public_code']['status'] == 'unavailable'
    assert len(server['requests']) == expected_requests
    assert audit['downloaded_bytes'] <= limits.total_bytes + 1


def test_file_and_context_budgets_preserve_partial_scope(tmp_path, server):
    server['truncated'] = True
    server['files']['src/combine.py'] = (b'def unrelated():\n    pass\n' * 100 +
                                        b'def covariance_propagation(x):\n    return x\n')
    raw = server['files']['src/combine.py']
    server['entries'][2].update(size=len(raw), sha=blob_sha(raw))
    limits = replace(public_code.CodeLimits(), files=3, context_chars=2200)
    rows = [source()]
    audit = public_code.acquire_public_code(tmp_path, rows, 'covariance propagation', limits=limits)
    assert audit['status'] == 'partial'
    assert len(audit['files']) <= 3
    assert len(Path(rows[0]['public_code']['packet_path']).read_text()) <= limits.context_chars
    code = next(row for row in audit['files'] if row['kind'] == 'algorithm')
    assert not code['complete_file_in_context'] and code['selected_line_ranges']
    assert 'def covariance_propagation' in Path(audit['packet_path']).read_text()


@pytest.mark.parametrize('fault', ['redirect', 'bad_blob', 'bad_commit'])
def test_unavailable_on_redirect_or_identity_mismatch(tmp_path, server, fault):
    server[fault] = True
    rows = [source()]
    audit = public_code.acquire_public_code(tmp_path, rows, 'method')
    assert audit['status'] == rows[0]['public_code']['status'] == 'unavailable'
    assert not rows[0]['public_code'].get('packet_path')


def test_rate_limit_is_explicit_without_auth_or_repeating_api_request(tmp_path, server):
    server['http_error'] = 403
    rows = [source()]
    audit = public_code.acquire_public_code(tmp_path, rows, 'method')
    assert len(server['requests']) == 2 and audit['reason'] == 'public_http_403'
    assert audit['fallback_reason'] == 'public_http_403'
    assert audit['requests'][0]['http_status'] == 403


def test_time_budget_stops_before_http(tmp_path, server):
    rows = [source()]
    audit = public_code.acquire_public_code(tmp_path, rows, 'method', limits=replace(public_code.CodeLimits(), seconds=0))
    assert not server['requests'] and audit['status'] == 'unavailable'


def test_rest_quota_fallback_pins_archive_and_extracts_only_bounded_regular_text(tmp_path, server):
    server['api_rate_limit'] = True
    server['files']['../escape.py'] = b'unsafe path\n'
    rows = [source()]
    audit = public_code.acquire_public_code(tmp_path, rows, 'uncertainty selection')
    evidence = rows[0]['public_code']
    assert audit['status'] == 'available' and len(server['requests']) == 3
    assert evidence['commit_sha'] == COMMIT and 'tree_sha' not in evidence
    assert evidence['identity_verification'].startswith('official_commit_pinned_archive')
    assert {record['path'] for record in evidence['files']} == {'README.md', 'configuration/region.yaml', 'src/combine.py'}
    assert not (tmp_path / 'escape.py').exists()
    assert not list(tmp_path.rglob('unsafe.py'))
    assert all(Path(record['local_path']).suffix == '.txt' for record in evidence['files'])
    assert audit['fallback_reason'] == 'public_http_403'


def test_archive_expansion_limit_is_explicit(tmp_path, server):
    server['api_rate_limit'] = True
    rows = [source()]
    audit = public_code.acquire_public_code(tmp_path, rows, 'method', limits=replace(public_code.CodeLimits(), expanded_bytes=512))
    assert audit['status'] == 'unavailable'
    assert audit['reason'] == 'public_code_expansion_or_time_budget_exhausted'
    assert not list(tmp_path.rglob('*.txt'))


def test_discover_sources_attaches_code_metadata_before_return(tmp_path, monkeypatch):
    from researchclaw.orchestration import grounding
    monkeypatch.setattr(grounding, 'source_queries', lambda *args: ['scientific method'])
    monkeypatch.setattr(grounding, 'named_frameworks', lambda topic: [])
    content = 'Scientific source methods. ' * 100 + REPO
    payload = {'results': [{'url': 'https://arxiv.org/abs/2501.12345', 'title': 'A method', 'content': content}]}
    monkeypatch.setattr(grounding.urllib.request, 'urlopen', lambda req, timeout: Response(json.dumps(payload).encode(), req.full_url))
    def acquire(root, rows, query):
        assert rows[0]['public_code_urls'] == [REPO]
        rows[0]['public_code'] = {'status': 'partial', 'commit_sha': COMMIT, 'packet_path': str(root / 'packet.md')}
        return {'status': 'partial', 'llm_calls': 0}
    monkeypatch.setattr(public_code, 'acquire_public_code', acquire)
    config = SimpleNamespace(research=SimpleNamespace(topic='Scientific method'),
        research_engine=SimpleNamespace(public_source_search=True, max_source_queries=1, max_public_sources=1))
    rows = grounding.discover_sources(tmp_path, config, SimpleNamespace(base_url='https://api.deepseek.com', api_key='fixture'), {})
    assert rows[0]['public_code']['commit_sha'] == COMMIT
    audit = json.loads((tmp_path / 'public_source_retrieval.json').read_text())
    assert audit['sources'][0]['public_code']['status'] == 'partial'
    assert audit['public_code']['llm_calls'] == 0


def assert_original_lines(text, excerpt, ranges, cap):
    assert len(excerpt) <= cap
    lines = text.splitlines()
    expected = [f'L{i:05d}: {lines[i - 1]}' for start, end in ranges for i in range(start, end + 1)]
    assert excerpt.splitlines() == expected


def test_long_docstring_cannot_displace_complete_math_body():
    text = ('def combine_measurements(values, errors):\n    """\n' +
            '    Selection uncertainty weighting documentation and background.\n' * 150 +
            '    """\n'
            '    count = len(values)\n'
            '    mean = sum(values) / count\n'
            '    variance = sum(error ** 2 for error in errors) / count ** 2\n'
            '    return mean, variance\n')
    excerpt, ranges = public_code._excerpt(text, 'combine selection uncertainty weighting', 800)
    assert 'count = len(values)' in excerpt
    assert 'mean = sum(values) / count' in excerpt
    assert 'variance = sum(error ** 2 for error in errors) / count ** 2' in excerpt
    assert 'return mean, variance' in excerpt
    assert 'def combine_measurements' in excerpt
    assert 'documentation and background' not in excerpt
    assert_original_lines(text, excerpt, ranges, 800)


def test_large_function_keeps_calculation_dependencies_and_multiline_statements():
    text = ('def estimate_uncertainty(values, errors):\n    """\n' +
            '    Ensemble uncertainty and weighting details.\n' * 120 + '    """\n' +
            ''.join(f'    unused_{i} = "' + 'x' * 100 + '"\n' for i in range(50)) +
            '    count = len(values)\n'
            '    mean = sum(values) / count\n'
            '    squared = sum(error ** 2 for error in errors)\n'
            '    variance = (\n'
            '        squared / count ** 2\n'
            '    )\n'
            '    return mean, variance\n')
    excerpt, ranges = public_code._excerpt(text, 'estimate uncertainty variance mean', 950)
    assert 'count = len(values)' in excerpt
    assert 'mean = sum(values) / count' in excerpt
    assert 'squared = sum(error ** 2 for error in errors)' in excerpt
    assert 'variance = (' in excerpt and 'squared / count ** 2' in excerpt
    assert 'return mean, variance' in excerpt
    assert 'unused_' not in excerpt
    assert_original_lines(text, excerpt, ranges, 950)


def test_method_and_control_context_are_retained_without_class_docstring():
    text = ('class Reconciler:\n    """\n' + '    Scientific calibration context.\n' * 100 +
            '    """\n'
            '    def uncertainty(self, values, enabled):\n'
            '        """Measurement uncertainty."""\n'
            '        if enabled:\n'
            '            count = len(values)\n'
            '            variance = sum(x ** 2 for x in values) / count\n'
            '            return variance\n'
            '        return None\n')
    excerpt, ranges = public_code._excerpt(text, 'uncertainty variance', 850)
    assert 'class Reconciler:' in excerpt and 'def uncertainty(' in excerpt
    assert 'if enabled:' in excerpt
    assert 'count = len(values)' in excerpt and 'sum(x ** 2 for x in values) / count' in excerpt
    assert 'Scientific calibration context.' not in excerpt
    assert_original_lines(text, excerpt, ranges, 850)


def test_docstring_keywords_cannot_outrank_executable_method():
    text = ('def describe():\n    """\n' + '    covariance uncertainty weighting variance.\n' * 100 +
            '    """\n    return "background"\n\n'
            'def covariance(values):\n'
            '    count = len(values)\n'
            '    mean = sum(values) / count\n'
            '    return sum((x - mean) ** 2 for x in values) / count\n')
    excerpt, ranges = public_code._excerpt(text, 'covariance uncertainty weighting variance', 500)
    assert 'def covariance(values):' in excerpt and 'mean = sum(values) / count' in excerpt
    assert 'return sum((x - mean) ** 2 for x in values) / count' in excerpt
    assert 'weighting variance.' not in excerpt
    assert_original_lines(text, excerpt, ranges, 500)


def test_acquisition_marks_docstring_omission_and_preserves_full_raw_source(tmp_path, server):
    raw = ('def combine(values):\n    """\n' + '    Uncertainty metadata.\n' * 100 +
           '    """\n    count = len(values)\n    return sum(values) / count\n').encode()
    server['files']['src/combine.py'] = raw
    server['entries'][2].update(size=len(raw), sha=blob_sha(raw))
    rows = [source()]
    audit = public_code.acquire_public_code(tmp_path, rows, 'combine uncertainty',
                                           limits=replace(public_code.CodeLimits(), context_chars=2200))
    record = next(row for row in audit['files'] if row['kind'] == 'algorithm')
    assert Path(record['local_path']).read_bytes() == raw
    assert not record['complete_file_in_context']
    assert record['excerpt_scope'].startswith('partial original statement/line ranges')
    assert 'return sum(values) / count' in Path(audit['packet_path']).read_text()
    assert audit['status'] == 'partial'


@pytest.mark.parametrize('control', ['if_else', 'try_except', 'match'])
def test_partial_calculation_retains_original_branch_guards(control):
    head = ('def variance(values, enabled):\n    """\n' + '    Numerical uncertainty.\n' * 80 + '    """\n' +
            ''.join(f'    unused_{i} = "' + 'x' * 100 + '"\n' for i in range(25)))
    bodies = {
        'if_else': ('    if enabled:\n        scale = len(values)\n    else:\n        scale = 1\n'
                    '    return sum(x ** 2 for x in values) / scale\n'),
        'try_except': ('    try:\n        scale = len(values)\n    except ValueError:\n        scale = 1\n'
                       '    return sum(x ** 2 for x in values) / scale\n'),
        'match': ('    match enabled:\n        case True:\n            scale = len(values)\n'
                  '        case _:\n            scale = 1\n    return sum(x ** 2 for x in values) / scale\n'),
    }
    text = head + bodies[control]
    excerpt, ranges = public_code._excerpt(text, 'variance uncertainty', 600)
    assert 'return sum(x ** 2 for x in values) / scale' in excerpt
    if 'scale = 1' in excerpt:
        assert {'if_else': 'else:', 'try_except': 'except ValueError:', 'match': 'case _:'}[control] in excerpt
    assert {'if_else': 'if enabled:', 'try_except': 'try:', 'match': 'match enabled:'}[control] in excerpt
    assert_original_lines(text, excerpt, ranges, 600)


def test_augmented_calculation_retains_prior_value_dependency_and_loop_context():
    text = ('def accumulated_variance(errors):\n    """\n' + '    Uncertainty documentation.\n' * 80 + '    """\n' +
            ''.join(f'    unused_{i} = "' + 'x' * 100 + '"\n' for i in range(25)) +
            '    total = 0.0\n'
            '    for error in errors:\n'
            '        total += error ** 2\n'
            '    return total / len(errors)\n')
    excerpt, ranges = public_code._excerpt(text, 'variance uncertainty', 600)
    assert 'total = 0.0' in excerpt and 'total += error ** 2' in excerpt
    assert 'for error in errors:' in excerpt and 'return total / len(errors)' in excerpt
    assert_original_lines(text, excerpt, ranges, 600)


def test_dataframe_squaring_before_sum_is_an_atomic_prerequisite():
    text = ('def global_uncertainty(frames, factor):\n    """\n' + '    Regional uncertainty combination.\n' * 100 + '    """\n'
            '    for frame in frames:\n'
            '        frame["errors"] *= factor\n'
            '        frame["errors"] = frame["errors"] ** 2\n'
            '    merged = concat([item for item in frames])\n'
            '    uncertainty = sqrt(merged["errors"].sum())\n'
            '    return uncertainty\n')
    excerpt, ranges = public_code._excerpt(text, 'global uncertainty', 1100)
    assert 'sqrt(merged["errors"].sum())' in excerpt
    assert 'frame["errors"] *= factor' in excerpt
    assert 'frame["errors"] = frame["errors"] ** 2' in excerpt
    assert 'for frame in frames:' in excerpt
    assert 'merged = concat([item for item in frames])' in excerpt
    assert_original_lines(text, excerpt, ranges, 1100)


@pytest.mark.parametrize('target', ['frame["errors"]', 'frame.errors'])
def test_prior_unit_conversion_alias_and_inplace_updates_are_retained(target):
    text = ('def uncertainty(frame, conversion):\n    """\n' + '    Conversion uncertainty.\n' * 80 + '    """\n'
            '    working = frame\n'
            f'    {target} *= conversion\n'
            '    working.normalize(inplace=True)\n'
            f'    {target} **= 2\n'
            f'    return sqrt({target}.sum())\n')
    excerpt, ranges = public_code._excerpt(text, 'unit conversion uncertainty', 900)
    assert f'{target} *= conversion' in excerpt
    assert f'{target} **= 2' in excerpt and 'working.normalize(inplace=True)' in excerpt
    assert 'working = frame' in excerpt and f'return sqrt({target}.sum())' in excerpt
    assert_original_lines(text, excerpt, ranges, 900)


def test_container_insertion_keeps_later_element_mutation_before_aggregation():
    text = ('def uncertainty(frame):\n    """\n' + '    Aggregation uncertainty context.\n' * 80 + '    """\n'
            '    frames = []\n'
            '    frames.append(frame)\n'
            '    frame["errors"] **= 2\n'
            '    merged = concat(frames)\n'
            '    return sqrt(merged["errors"].sum())\n')
    excerpt, ranges = public_code._excerpt(text, 'aggregation uncertainty', 900)
    assert 'frame["errors"] **= 2' in excerpt
    assert 'frames.append(frame)' in excerpt and 'return sqrt(merged["errors"].sum())' in excerpt
    assert_original_lines(text, excerpt, ranges, 900)


def test_unfitting_state_transform_omits_consumer_instead_of_changing_its_meaning():
    text = ('def uncertainty(frame):\n'
            '    frame["errors"] = (\n' + ''.join(f'        convert_{i}(frame["errors"]) +\n' for i in range(30)) +
            '        frame["errors"] ** 2\n    )\n'
            '    return sqrt(frame["errors"].sum())\n')
    omissions = []
    excerpt, ranges = public_code._excerpt(text, 'uncertainty', 350, omissions=omissions)
    assert 'sqrt(frame["errors"].sum())' not in excerpt
    assert [len(text.splitlines()), len(text.splitlines())] in omissions
    assert_original_lines(text, excerpt, ranges, 350)


def test_phase_renderer_is_bounded_hash_verified_and_read_only(tmp_path, server, monkeypatch):
    rows = [source()]
    public_code.acquire_public_code(tmp_path, rows, 'uncertainty selection')
    evidence = rows[0]['public_code']
    original = copy.deepcopy(evidence)
    before = {path: (hashlib.sha256(path.read_bytes()).hexdigest(), path.stat().st_mtime_ns)
              for path in tmp_path.rglob('*') if path.is_file()}
    monkeypatch.setattr(public_code.urllib.request, 'build_opener', lambda *args: pytest.fail('renderer attempted HTTP'))
    monkeypatch.setattr(public_code, 'write_json', lambda *args: pytest.fail('renderer attempted write'))
    for limit in [0, 80, 800, 2200, 12000, 90000]:
        packet = public_code.render_code_context(evidence, 'uncertainty selection', limit)
        assert len(packet) <= min(limit, 36000)
        if limit >= 2200:
            assert COMMIT in packet and REPO in packet and evidence['source_scope'] in packet
            assert 'not imported, installed or executed' in packet
            assert 'vector = [x, 1]' in packet
    after = {path: (hashlib.sha256(path.read_bytes()).hexdigest(), path.stat().st_mtime_ns)
             for path in tmp_path.rglob('*') if path.is_file()}
    assert evidence == original and before == after


@pytest.mark.parametrize('fault', ['changed', 'missing', 'symlink', 'hardlink', 'traversal', 'outside', 'bad_hash', 'bad_commit'])
def test_phase_renderer_rejects_changed_missing_or_unsafe_raw_files(tmp_path, server, fault):
    rows = [source()]
    public_code.acquire_public_code(tmp_path, rows, 'uncertainty selection')
    evidence = rows[0]['public_code']
    record = evidence['files'][0]
    path = Path(record['local_path'])
    if fault == 'changed':
        path.write_text('changed source')
    elif fault == 'missing':
        path.unlink()
    elif fault in {'symlink', 'hardlink'}:
        other = tmp_path / 'elsewhere.txt'
        other.write_bytes(path.read_bytes())
        path.unlink()
        if fault == 'symlink':
            path.symlink_to(other)
        else:
            path.hardlink_to(other)
    elif fault == 'traversal':
        record['local_path'] = str(path.parent / '..' / 'files' / path.name)
    elif fault == 'outside':
        record['local_path'] = str(tmp_path / 'elsewhere.txt')
    elif fault == 'bad_hash':
        record['raw_sha256'] = 'not-a-hash'
    else:
        evidence['commit_sha'] = 'branch-name'
    packet = public_code.render_code_context(evidence, 'uncertainty', 3000)
    assert 'PUBLIC CODE UNAVAILABLE:' in packet
    assert 'L00001:' not in packet


def test_phase_renderer_explicitly_omits_unclosed_mathematical_statement(tmp_path, server):
    raw = ('def uncertainty(frame):\n'
           '    frame["errors"] = (\n' + ''.join(f'        convert_{i}(frame["errors"]) +\n' for i in range(50)) +
           '        frame["errors"] ** 2\n    )\n'
           '    return sqrt(frame["errors"].sum())\n').encode()
    server['files']['src/combine.py'] = raw
    server['entries'][2].update(size=len(raw), sha=blob_sha(raw))
    rows = [source()]
    public_code.acquire_public_code(tmp_path, rows, 'uncertainty')
    packet = public_code.render_code_context(rows[0]['public_code'], 'uncertainty', 2200)
    assert 'sqrt(frame["errors"].sum())' not in packet
    assert 'OMITTED:' in packet and 'prerequisites' in packet


def test_visible_object_preconditions_are_kept_with_mutation_and_formula():
    text = ('def uncertainty(frame):\n    """\n' + '    Source uncertainty context.\n' * 80 + '    """\n'
            '    assert frame.error_kind == "standard_deviation"\n'
            '    frame.errors **= 2\n'
            '    return sqrt(frame.errors.sum())\n')
    excerpt, ranges = public_code._excerpt(text, 'uncertainty', 800)
    assert 'assert frame.error_kind == "standard_deviation"' in excerpt
    assert 'frame.errors **= 2' in excerpt and 'sqrt(frame.errors.sum())' in excerpt
    assert_original_lines(text, excerpt, ranges, 800)


def test_phase_renderer_rejects_fifo_without_blocking(tmp_path, server):
    rows = [source()]
    public_code.acquire_public_code(tmp_path, rows, 'uncertainty')
    evidence = rows[0]['public_code']
    path = Path(evidence['files'][0]['local_path'])
    path.unlink()
    os.mkfifo(path)
    packet = public_code.render_code_context(evidence, 'uncertainty', 3000)
    assert 'PUBLIC CODE UNAVAILABLE: unsafe_raw_source_type_or_size' in packet


@pytest.mark.parametrize('evidence,query,limit', [
    (None, 'uncertainty', 3000), ([], 'uncertainty', 3000),
    ({}, None, 3000), ({'repository_url': 42}, 'uncertainty', 3000),
    ({}, 'uncertainty', None), ({}, 'uncertainty', '3000'),
])
def test_phase_renderer_malformed_arguments_are_explicitly_unavailable(evidence, query, limit):
    assert 'PUBLIC CODE UNAVAILABLE:' in public_code.render_code_context(evidence, query, limit)


@pytest.mark.parametrize('component', ['files', 'packet_directory'])
def test_phase_renderer_rejects_symlinked_ancestor_directories(tmp_path, server, component):
    rows = [source()]
    public_code.acquire_public_code(tmp_path, rows, 'uncertainty')
    evidence = rows[0]['public_code']
    directory = Path(evidence['packet_path']).parent
    original = directory / 'files' if component == 'files' else directory
    moved = tmp_path / 'moved-source'
    original.rename(moved)
    original.symlink_to(moved, target_is_directory=True)
    assert 'PUBLIC CODE UNAVAILABLE:' in public_code.render_code_context(evidence, 'uncertainty', 3000)
