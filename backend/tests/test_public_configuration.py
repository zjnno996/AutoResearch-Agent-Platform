import sys
from pathlib import Path

import yaml

from researchclaw.config import RCConfig
from researchclaw.llm import resolve_provider_base_url


def test_public_endpoint_uses_environment_and_preserves_explicit_configuration(monkeypatch):
    monkeypatch.setenv("EMNETS_API_BASE", "https://model.example.test/v1/")
    monkeypatch.setenv("OPENAI_BASE_URL", "https://secondary.example.test/v1")
    assert resolve_provider_base_url("openai-compatible", "") == "https://model.example.test/v1"
    assert resolve_provider_base_url("openai-compatible", "https://explicit.test/v1/") == "https://explicit.test/v1"
    monkeypatch.delenv("EMNETS_API_BASE")
    assert resolve_provider_base_url("openai-compatible", "") == "https://secondary.example.test/v1"
    monkeypatch.delenv("OPENAI_BASE_URL")
    assert resolve_provider_base_url("openai-compatible", "") == "http://localhost:8000/v1"


def test_public_config_contains_no_credentials_or_host_specific_interpreter(monkeypatch):
    monkeypatch.delenv("EMNETS_PROVIDER_LOCK", raising=False)
    monkeypatch.delenv("EMNETS_SANDBOX_PYTHON", raising=False)
    root = Path(__file__).resolve().parents[2]
    raw = yaml.safe_load((root / "config.arc.yaml").read_text())
    for name in ["llm", "review_llm", "web_chat_llm", "idea_judge_llm"]:
        assert raw[name]["api_key"] == "" and raw[name]["base_url"] == ""
        assert raw[name]["primary_model"] == "Qwen3.8-27B-FP8"
    config = RCConfig.from_dict(raw, project_root=root, check_paths=False)
    assert config.experiment.sandbox.python_path == sys.executable
    monkeypatch.setenv("EMNETS_SANDBOX_PYTHON", "/configured/experiment/python")
    config = RCConfig.from_dict(raw, project_root=root, check_paths=False)
    assert config.experiment.sandbox.python_path == "/configured/experiment/python"


def test_review_health_checks_the_locked_endpoint_without_exposing_credentials(monkeypatch):
    from services.review_service import _configured_review_endpoints
    monkeypatch.setenv("EMNETS_PROVIDER_LOCK", "1")
    monkeypatch.setenv("EMNETS_API_BASE", "https://private-model.example.test/v1")
    monkeypatch.setenv("EMNETS_MODEL", "Qwen3.8-27B-FP8")
    monkeypatch.setenv("EMNETS_API_KEY_ENV", "EMNETS_TEST_CREDENTIAL")
    monkeypatch.setenv("EMNETS_TEST_CREDENTIAL", "test-only-placeholder")
    assert _configured_review_endpoints() == ["https://private-model.example.test/v1"]


def test_browser_model_catalog_omits_private_provider_fields(monkeypatch):
    from services import review_service
    monkeypatch.setattr(review_service, '_build_model_configs', lambda: None)
    monkeypatch.setattr(review_service, 'get_preferred_review_model_name', lambda: 'Qwen3.8-27B-FP8')
    monkeypatch.setattr(review_service, 'get_model_options', lambda: [{
        'value': 'Qwen3.8-27B-FP8', 'label': 'Qwen3.8-27B-FP8',
        'baseUrl': 'https://private-model.example.test/v1', 'api_key': 'private-fixture-only'}])
    handler = object.__new__(review_service.ReviewHandler)
    handler.path = '/api/models'
    replies = []
    handler._send_json = lambda status, value: replies.append((status, value))
    handler.do_GET()
    assert replies == [(200, {'models': [{
        'value': 'Qwen3.8-27B-FP8', 'label': 'Qwen3.8-27B-FP8', 'baseUrl': ''}]})]


def test_browser_health_omits_internal_endpoints_and_transport_errors(monkeypatch):
    from services import review_service
    monkeypatch.setattr(review_service, '_review_model_health', lambda: {
        'available': False, 'endpoints': [{'endpoint': 'https://private-model.example.test/v1',
                                         'error': 'private transport details', 'available': False}]})
    handler = object.__new__(review_service.ReviewHandler)
    handler.path = '/api/health'
    replies = []
    handler._send_json = lambda status, value: replies.append((status, value))
    handler.do_GET()
    assert replies[0][1]['reviewModel'] == {'available': False}
    assert 'private-model' not in str(replies)
    assert 'private-model' not in review_service._review_unavailable_message({
        'endpoints': [{'endpoint': 'https://private-model.example.test/v1'}]})
