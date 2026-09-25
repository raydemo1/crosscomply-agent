import pytest

from law_agent.config import require_llm_config


def test_require_llm_config_fails_without_api_key(monkeypatch, tmp_path) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("OPENAI_COMPATIBLE_API_KEY", raising=False)
    monkeypatch.setenv("OPENAI_COMPATIBLE_BASE_URL", "https://api.deepseek.com")
    monkeypatch.setenv("OPENAI_COMPATIBLE_MODEL", "deepseek-flash")

    with pytest.raises(RuntimeError, match="OPENAI_COMPATIBLE_API_KEY is required"):
        require_llm_config()
