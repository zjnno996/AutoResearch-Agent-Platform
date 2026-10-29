"""Compatibility controls do not change the official scoring protocol or research."""
from types import SimpleNamespace

import pytest

from researchclaw.benchmarks.judge import configured_official_judge, judge_options


def test_provider_defaults_are_preserved_unless_explicitly_configured(monkeypatch):
    monkeypatch.delenv("JUDGE_THINKING", raising=False)
    assert judge_options() == {}
    with configured_official_judge(SimpleNamespace(), {}):
        pass


@pytest.mark.parametrize("thinking,base,model", [
    ("invalid", "https://api.deepseek.com/v1", "deepseek-flash"),
    ("disabled", "https://other.test/v1", "deepseek-flash"),
    ("disabled", "https://api.deepseek.com/v1", "other-model"),
])
def test_unsupported_judge_options_fail_explicitly(monkeypatch, thinking, base, model):
    monkeypatch.setenv("JUDGE_THINKING", thinking)
    monkeypatch.setenv("JUDGE_API_BASE", base)
    monkeypatch.setenv("JUDGE_MODEL_NAME", model)
    with pytest.raises(ValueError):
        judge_options()


@pytest.mark.parametrize("raise_during_scoring", [False, True])
def test_judge_toggle_preserves_rubric_budget_and_restores_factory(monkeypatch, raise_during_scoring):
    monkeypatch.setenv("JUDGE_THINKING", "disabled")
    monkeypatch.setenv("JUDGE_API_BASE", "https://api.deepseek.com/v1")
    monkeypatch.setenv("JUDGE_MODEL_NAME", "deepseek-flash")
    calls, constructor = [], []
    def original_factory(**kwargs):
        constructor.append(kwargs)
        completions = SimpleNamespace(create=lambda **body: calls.append(body) or {"ok": True})
        return SimpleNamespace(client=SimpleNamespace(chat=SimpleNamespace(completions=completions)))
    scorer = SimpleNamespace(LLMAgent=original_factory)
    try:
        with configured_official_judge(scorer, judge_options()):
            agent = scorer.LLMAgent(max_tokens=500, system_prompt="Official rubric", temperature=0)
            agent.client.chat.completions.create(model="deepseek-flash", max_tokens=500,
                messages=[{"role": "user", "content": "Unmodified criterion and report"}], extra_body={"other": True})
            if raise_during_scoring:
                raise RuntimeError("scorer failed")
    except RuntimeError:
        assert raise_during_scoring
    assert scorer.LLMAgent is original_factory
    assert constructor == [{"max_tokens": 500, "system_prompt": "Official rubric", "temperature": 0}]
    assert calls[0]["max_tokens"] == 500
    assert calls[0]["messages"][0]["content"] == "Unmodified criterion and report"
    assert calls[0]["extra_body"] == {"other": True, "thinking": {"type": "disabled"}}
