"""Task-named primary methods and scanned references must reach research context."""
import hashlib
import io
import json
from pathlib import Path
import shutil
import subprocess
from types import SimpleNamespace

import pytest
from PIL import Image, ImageDraw, ImageFont

from researchclaw.orchestration.artifacts import recover_missing_root_brace, recover_plan_mapping_boundary
from researchclaw.orchestration.compact_generation import reference_packet, scientific_excerpt
from researchclaw.orchestration.grounding import discover_sources
from researchclaw.orchestration.literature import extract_pdf, named_frameworks, source_queries


TOPIC = 'Develop Variable and Operator Splitting (VOS), with Nesterov and ADMM comparisons.'


def test_exact_named_method_precedes_generic_search_without_duplicating_specific_queries():
    assert named_frameworks(TOPIC) == [('Variable and Operator Splitting', 'VOS')]
    queries = source_queries(TOPIC, ['convex ODE', 'ADMM convergence'], 2)
    assert queries == ['"Variable and Operator Splitting" VOS', 'convex ODE']
    specific = ['Variable operator splitting and Nesterov', 'ADMM convergence']
    assert source_queries(TOPIC, specific, 2) == specific
    assert source_queries(TOPIC, [], 2) == []


def test_retrieval_gives_both_queries_a_slot_and_fetches_canonical_method(tmp_path, monkeypatch):
    sent = []
    class Response:
        def __init__(self, data): self.data = data
        def __enter__(self): return self
        def __exit__(self, *args): pass
        def read(self, *args): return json.dumps(self.data).encode()
    def request(req, **kwargs):
        body = json.loads(req.data)
        sent.append((req.full_url, body))
        if req.full_url.endswith('/fetch'):
            return Response({'content': 'Variable and Operator Splitting (VOS) canonical defining equations. ' * 90})
        if 'Variable' in body['query']:
            return Response({'results': [
                {'url': 'https://arxiv.org/html/2401.00001v2#9', 'content': 'A named-method index excerpt. ' * 80,
                 'title': 'Variable and Operator Splitting', 'rerank_score': 1},
                {'url': 'https://arxiv.org/abs/2401.00002', 'content': 'Other method. ' * 180}]})
        return Response({'results': [
            {'url': 'https://arxiv.org/abs/2401.00001', 'content': 'Duplicate version. ' * 130},
            {'url': 'https://arxiv.org/abs/2401.00003', 'content': 'Complementary theorem. ' * 130}]})
    monkeypatch.setattr('urllib.request.urlopen', request)
    cfg = SimpleNamespace(research=SimpleNamespace(topic=TOPIC), research_engine=SimpleNamespace(
        public_source_search=True, max_source_queries=2, max_public_sources=2))
    llm = SimpleNamespace(base_url='https://api.deepseek.com/v1', api_key='private-fixture')
    refs = discover_sources(tmp_path, cfg, llm, {'source_queries': ['generic convergence', 'ODE']})
    assert [row['source'] for row in refs] == ['https://arxiv.org/abs/2401.00001v2', 'https://arxiv.org/abs/2401.00003']
    assert refs[0]['named_framework_matches'] == ['Variable and Operator Splitting']
    assert refs[0]['content_origin'] == 'fetch_and_search'
    assert 'index excerpt' in Path(refs[0]['text_path']).read_text()
    assert [body['url'] for endpoint, body in sent if endpoint.endswith('/fetch')] == ['https://arxiv.org/html/2401.00001v2']
    assert len(sent) == 3 and 'private-fixture' not in (tmp_path / 'public_source_retrieval.json').read_text()


def test_short_named_acronym_survives_generic_repetition_and_all_sources_remain_visible(tmp_path):
    text = 'Source identity.\n' + 'Generic algorithm convergence theorem.\n' * 800
    text += '\nVOS_DEFINING_UPDATE: VOS splits the variable and operator; VOS identity z=x+y.\n'
    text += 'Other background discussion.\n' * 600
    excerpt = scientific_excerpt(text, 'VOS unified algorithm', 2200)
    assert 'VOS_DEFINING_UPDATE' in excerpt and 'source characters' in excerpt
    path = tmp_path / 'method.md'
    path.write_text(text)
    packet = reference_packet({'references': [
        {'source': 'method-primary', 'text_path': str(path), 'context': 'summary without the method',
         'named_framework_matches': ['Variable and Operator Splitting']},
        *[{'source': f'background-{index}', 'context': 'Other context. ' * 400} for index in range(4)]
    ]}, 6500, query='VOS unified algorithm')
    assert 'VOS_DEFINING_UPDATE' in packet and len(packet) <= 6500
    assert all(f'background-{index}' in packet for index in range(4))


def test_one_missing_plan_boundary_preserves_all_ids_and_scientific_content():
    plan = {'methods': ['VOS'], 'datasets': ['visible inputs'], 'metrics': ['residual'],
            'protocol': {'command': 'python main.py'},
            'requirement_map': {f'R{i}': {'approach': f'Solve condition {i}', 'artifact': 'outputs/result.npz'} for i in range(1, 14)},
            'scientific_analysis_markdown': 'Detailed argument with assumptions. ' * 250,
            'risks': ['The convergence proof remains incomplete.']}
    raw = json.dumps(plan)
    marker = '}, "scientific_analysis_markdown"'
    broken = raw.replace(marker, ', "scientific_analysis_markdown"', 1)
    assert broken != raw and recover_plan_mapping_boundary(broken) == plan
    assert recover_plan_mapping_boundary(raw) == {}
    assert recover_missing_root_brace('{"claim":"incomplete') == {}
    assert recover_missing_root_brace('{"negative_result":true') == {'negative_result': True}
    assert recover_missing_root_brace('{"a":1,') == {}


def scan_pdf(path, pages=1):
    fitz = pytest.importorskip('fitz')
    image = Image.new('RGB', (1200, 500), 'white')
    font = ImageFont.truetype('DejaVuSans.ttf', 27)
    draw = ImageDraw.Draw(image)
    for index, line in enumerate(['A Method of Convex Optimization', 'Original scanned source and assumptions.',
                                  'The gradient is Lipschitz continuous.', 'Numerical curves are not a mathematical proof.',
                                  'Convergence rates require stated hypotheses.']):
        draw.text((25, 25 + index * 70), line, font=font, fill='black')
    data = io.BytesIO()
    image.save(data, format='PNG')
    document = fitz.open()
    for _ in range(pages):
        page = document.new_page(width=720, height=300)
        page.insert_image(page.rect, stream=data.getvalue())
    document.save(path)
    document.close()


def test_selectable_pdf_needs_no_ocr_and_source_hash_is_unchanged(tmp_path, monkeypatch):
    fitz = pytest.importorskip('fitz')
    path = tmp_path / 'vector.pdf'
    doc = fitz.open()
    page = doc.new_page()
    page.insert_textbox(page.rect, 'A theorem requires assumptions and a complete proof.\n' * 10)
    doc.save(path)
    doc.close()
    before = hashlib.sha256(path.read_bytes()).hexdigest()
    monkeypatch.setattr(subprocess, 'run', lambda *args, **kw: pytest.fail('Vector PDF must not invoke OCR'))
    text, metadata = extract_pdf(path, tmp_path / 'cache')
    assert 'theorem requires' in text and metadata['usable_text'] and metadata['complete_page_coverage']
    assert metadata['ocr_pages'] == [] and hashlib.sha256(path.read_bytes()).hexdigest() == before


def test_scanned_pdf_uses_real_local_ocr_and_marks_precision_limits(tmp_path):
    if not shutil.which('tesseract'):
        pytest.skip('Optional local Tesseract is not installed')
    source = tmp_path / 'scan.pdf'
    scan_pdf(source)
    before = hashlib.sha256(source.read_bytes()).hexdigest()
    text, metadata = extract_pdf(source, tmp_path / 'cache')
    assert 'Convex Optimization' in text and 'Lipschitz' in text
    assert metadata['usable_text'] and metadata['ocr_pages'] == [1]
    assert 'may misread mathematical symbols' in metadata['precision_note']
    assert hashlib.sha256(source.read_bytes()).hexdigest() == before
    assert not list((tmp_path / 'cache').iterdir())


def test_ocr_timeout_and_page_budget_are_explicit_not_fake_usable_text(tmp_path, monkeypatch):
    path = tmp_path / 'scan.pdf'
    scan_pdf(path, pages=3)
    calls = []
    monkeypatch.setattr(shutil, 'which', lambda name: '/local/tesseract')
    monkeypatch.setenv('DEEPSEEK_API_KEY', 'private-fixture')
    def timeout(args, **kwargs):
        calls.append(args)
        assert 'DEEPSEEK_API_KEY' not in kwargs['env'] and kwargs['timeout'] <= 30
        raise subprocess.TimeoutExpired(args, kwargs['timeout'])
    monkeypatch.setattr(subprocess, 'run', timeout)
    text, metadata = extract_pdf(path, tmp_path / 'cache', max_ocr_pages=1)
    assert len(calls) == 1 and not metadata['usable_text'] and not metadata['complete_page_coverage']
    assert metadata['unreadable_pages'] == [1, 2, 3]
    assert metadata['pages'][0]['ocr_status'] == 'TimeoutExpired'


def test_missing_ocr_tool_is_reported_as_unreadable_source(tmp_path, monkeypatch):
    path = tmp_path / 'scan.pdf'
    scan_pdf(path)
    monkeypatch.setattr(shutil, 'which', lambda name: None)
    _, metadata = extract_pdf(path, tmp_path / 'cache')
    assert not metadata['usable_text'] and metadata['unreadable_pages'] == [1]
