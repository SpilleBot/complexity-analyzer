"""Shared pytest fixtures."""

import pytest

# Provider credentials that leak in from the developer shell (or from the .env
# file cli.main loads at import time) and would otherwise flip auto-detection.
_PROVIDER_ENV_VARS = (
    "OPENAI_API_KEY",
    "OPENAI_BASE_URL",
    "GEMINI_API_KEY",
    "GOOGLE_API_KEY",
    "ANTHROPIC_API_KEY",
    "ANTHROPIC_BASE_URL",
    "KIMI_API_KEY",
    "MOONSHOT_API_KEY",
    "KIMI_BASE_URL",
    "AZURE_DEVOPS_PAT",
    "AZURE_DEVOPS_TOKEN",
    "AZURE_DEVOPS_SERVER_URL",
    "AZURE_DEVOPS_COLLECTION",
    "AZURE_DEVOPS_PROJECT",
)


@pytest.fixture(autouse=True)
def clean_provider_env(monkeypatch):
    """Remove ambient LLM provider credentials so tests are hermetic."""
    for var in _PROVIDER_ENV_VARS:
        monkeypatch.delenv(var, raising=False)
