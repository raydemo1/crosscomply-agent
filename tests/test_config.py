import pytest

from law_agent.config import (
    require_agent_llm_config,
    require_llm_config,
    require_semantic_llm_config,
)


def test_require_llm_config_fails_without_api_key(monkeypatch, tmp_path) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("OPENAI_COMPATIBLE_API_KEY", raising=False)
    monkeypatch.setenv("OPENAI_COMPATIBLE_BASE_URL", "https://api.deepseek.com")
    monkeypatch.setenv("OPENAI_COMPATIBLE_MODEL", "deepseek-flash")

    with pytest.raises(RuntimeError, match="OPENAI_COMPATIBLE_API_KEY is required"):
        require_llm_config()


@pytest.mark.parametrize("effort", ["none", "low", "high", "max"])
def test_reasoning_overrides_are_independent(monkeypatch, tmp_path, effort):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("OPENAI_COMPATIBLE_API_KEY", "fake")
    monkeypatch.setenv("OPENAI_COMPATIBLE_REASONING_EFFORT", "none")
    monkeypatch.setenv("LAWAGENT_AGENT_REASONING_EFFORT", "low")
    monkeypatch.setenv("LAWAGENT_SEMANTIC_REASONING_EFFORT", effort)
    assert require_llm_config().reasoning_effort == "none"
    assert require_agent_llm_config().reasoning_effort == "low"
    assert require_semantic_llm_config().reasoning_effort == effort


def test_reasoning_nodes_default_to_low_and_reject_nonexistent_medium_level(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("OPENAI_COMPATIBLE_API_KEY", "fake")
    monkeypatch.setenv("OPENAI_COMPATIBLE_REASONING_EFFORT", "none")
    monkeypatch.delenv("LAWAGENT_AGENT_REASONING_EFFORT", raising=False)
    monkeypatch.delenv("LAWAGENT_SEMANTIC_REASONING_EFFORT", raising=False)
    assert require_agent_llm_config().reasoning_effort == "low"
    assert require_semantic_llm_config().reasoning_effort == "low"
    monkeypatch.setenv("LAWAGENT_SEMANTIC_REASONING_EFFORT", "medium")
    with pytest.raises(RuntimeError, match="LAWAGENT_SEMANTIC_REASONING_EFFORT"):
        require_semantic_llm_config()
