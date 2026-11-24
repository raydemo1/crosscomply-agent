import os

import pytest

from law_agent.config import (
    require_agent_llm_config,
    require_llm_config,
    require_semantic_llm_config,
)


@pytest.mark.parametrize("override,expected", [("deepseek-v4-pro", "deepseek-v4-pro"), ("", "task-model")])
def test_semantic_model_override_is_independent_and_inherits_task_when_unset(monkeypatch, tmp_path, override, expected):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("OPENAI_COMPATIBLE_API_KEY", "fake")
    monkeypatch.setenv("OPENAI_COMPATIBLE_MODEL", "deepseek-flash")
    monkeypatch.setenv("LAWAGENT_SEMANTIC_MODEL", override)
    monkeypatch.setenv("LAWAGENT_SEMANTIC_REASONING_EFFORT", "low")
    assert require_semantic_llm_config(model_id="task-model").model == expected
    assert require_semantic_llm_config(model_id="task-model").reasoning_effort == "low"
    assert require_agent_llm_config().model == "deepseek-flash"


def test_production_verifier_uses_semantic_model_even_when_task_names_flash(monkeypatch, tmp_path):
    from law_agent.review import semantic_grounding
    from tests.test_review_llm import FakeClient

    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("OPENAI_COMPATIBLE_API_KEY", "fake")
    monkeypatch.setenv("LAWAGENT_SEMANTIC_MODEL", "deepseek-v4-pro")
    client = FakeClient()
    configs = []
    def build(config):
        configs.append(config)
        return client
    monkeypatch.setattr(semantic_grounding, "OpenAICompatibleClient", build)
    verifier = semantic_grounding.SemanticGroundingVerifier(model_id="deepseek-flash")
    assert verifier.node.model == configs[0].model == "deepseek-v4-pro"


def test_require_llm_config_fails_without_api_key(monkeypatch, tmp_path) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("OPENAI_COMPATIBLE_API_KEY", raising=False)
    monkeypatch.setenv("OPENAI_COMPATIBLE_BASE_URL", "https://api.deepseek.com")
    monkeypatch.setenv("OPENAI_COMPATIBLE_MODEL", "deepseek-flash")

    with pytest.raises(RuntimeError, match="OPENAI_COMPATIBLE_API_KEY is required"):
        require_llm_config()


def test_stream_timeouts_are_independent(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("OPENAI_COMPATIBLE_API_KEY", "fake")
    monkeypatch.setenv("OPENAI_COMPATIBLE_TIMEOUT_SECONDS", "20")
    monkeypatch.setenv("OPENAI_COMPATIBLE_READ_TIMEOUT_SECONDS", "35")
    monkeypatch.setenv("OPENAI_COMPATIBLE_TOTAL_TIMEOUT_SECONDS", "100")
    cfg = require_semantic_llm_config()
    assert (cfg.timeout_seconds, cfg.read_timeout_seconds, cfg.total_timeout_seconds) == (20, 35, 100)


@pytest.mark.parametrize("value", ["0", "-1", "nan", "inf"])
def test_stream_deadline_cannot_be_disabled_or_unbounded(monkeypatch, tmp_path, value):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("OPENAI_COMPATIBLE_API_KEY", "fake")
    monkeypatch.setenv("OPENAI_COMPATIBLE_TOTAL_TIMEOUT_SECONDS", value)
    with pytest.raises(ValueError, match="total_timeout_seconds"):
        require_semantic_llm_config()


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


def test_named_profile_uses_its_own_endpoint_and_preserves_service_settings(monkeypatch, tmp_path):
    from law_agent.config import load_service_config

    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(os, "environ", os.environ.copy())
    monkeypatch.delenv("CROSSCOMPLY_LLM_PROFILE", raising=False)
    monkeypatch.delenv("ES_URL", raising=False)
    (tmp_path / ".env").write_text(
        "CROSSCOMPLY_LLM_PROFILE=lab\n"
        "OPENAI_COMPATIBLE_API_KEY=base-key\n"
        "OPENAI_COMPATIBLE_BASE_URL=https://base.invalid\n"
        "OPENAI_COMPATIBLE_MODEL=base-model\n"
        "ES_URL=http://test-es:9200\n",
        encoding="utf-8",
    )
    profile_dir = tmp_path / "model-profiles"
    profile_dir.mkdir()
    (profile_dir / ".env.lab").write_text(
        "OPENAI_COMPATIBLE_API_KEY=lab-key\n"
        "OPENAI_COMPATIBLE_BASE_URL=https://lab.invalid/v1\n"
        "OPENAI_COMPATIBLE_MODEL=lab-model\n"
        "LAWAGENT_AGENT_REASONING_EFFORT=none\n",
        encoding="utf-8",
    )
    # Compose injects the base endpoint and model overrides into the process.
    with monkeypatch.context() as injected:
        for name, value in {
            "OPENAI_COMPATIBLE_API_KEY": "base-key",
            "OPENAI_COMPATIBLE_BASE_URL": "https://base.invalid",
            "OPENAI_COMPATIBLE_BETA_BASE_URL": "https://base.invalid/beta",
            "OPENAI_COMPATIBLE_MODEL": "base-model",
            "OPENAI_COMPATIBLE_TIMEOUT_SECONDS": "25",
            "OPENAI_COMPATIBLE_STRUCTURED_OUTPUT": "strict_tool",
            "LAWAGENT_AGENT_REASONING_EFFORT": "max",
            "LAWAGENT_SEMANTIC_MODEL": "base-verifier",
        }.items():
            injected.setenv(name, value)
        config = require_agent_llm_config()
        assert (config.env_profile, config.base_url, config.api_key, config.model) == (
            "lab", "https://lab.invalid/v1", "lab-key", "lab-model",
        )
        assert config.beta_base_url == "https://lab.invalid/v1/beta"
        assert config.structured_output_mode == "json_object"
        assert config.timeout_seconds == 60
        assert config.reasoning_effort == "none"
        assert require_semantic_llm_config().model == "lab-model"
        assert load_service_config().elasticsearch.url == "http://test-es:9200"

        injected.setenv("CROSSCOMPLY_LLM_PROFILE", "default")
        base = require_llm_config()
        assert (base.env_profile, base.model, base.structured_output_mode) == (
            "default", "base-model", "strict_tool",
        )


def test_named_profile_requires_its_own_credentials(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("OPENAI_COMPATIBLE_API_KEY", "base-key")
    monkeypatch.setenv("CROSSCOMPLY_LLM_PROFILE", "lab")
    with pytest.raises(FileNotFoundError, match=r"\.env\.lab"):
        require_llm_config()

    profile_dir = tmp_path / "model-profiles"
    profile_dir.mkdir()
    (profile_dir / ".env.lab").write_text(
        "OPENAI_COMPATIBLE_BASE_URL=https://lab.invalid\nOPENAI_COMPATIBLE_MODEL=lab-model\n",
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="OPENAI_COMPATIBLE_API_KEY"):
        require_llm_config()

    monkeypatch.setenv("CROSSCOMPLY_LLM_PROFILE", "../lab")
    with pytest.raises(ValueError, match="CROSSCOMPLY_LLM_PROFILE"):
        require_llm_config()
