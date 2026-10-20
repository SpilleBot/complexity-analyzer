"""Tests for the Kimi (Moonshot AI) LLM provider."""

from unittest.mock import Mock

import pytest

from cli.constants import (
    DEFAULT_KIMI_MAX_COMPLETION_TOKENS,
    DEFAULT_KIMI_MODEL,
    DEFAULT_MODEL,
)
from cli.llm import (
    PROVIDER_SPECS,
    KimiProvider,
    LLMError,
    get_provider,
    get_provider_spec,
    resolve_provider,
    resolve_provider_credentials,
)


class TestKimiProviderSpec:
    def test_spec_registered(self):
        spec = PROVIDER_SPECS.get("kimi")
        assert spec is not None
        assert spec.provider_cls is KimiProvider
        assert spec.default_model == DEFAULT_KIMI_MODEL
        assert "KIMI_API_KEY" in spec.env_var_keys
        assert "MOONSHOT_API_KEY" in spec.env_var_keys

    def test_moonshot_alias(self):
        spec = get_provider_spec("moonshot")
        assert spec is not None
        assert spec.name == "kimi"

    def test_alias_case_insensitive(self):
        spec = get_provider_spec(" MoonShot ")
        assert spec is not None
        assert spec.name == "kimi"


class TestKimiCredentialResolution:
    def test_auto_selects_kimi_from_env(self, monkeypatch):
        monkeypatch.setenv("KIMI_API_KEY", "sk-kimi-env")
        assert resolve_provider_credentials("auto") == ("kimi", "sk-kimi-env")

    def test_auto_selects_kimi_from_moonshot_env(self, monkeypatch):
        monkeypatch.setenv("MOONSHOT_API_KEY", "sk-moonshot-env")
        assert resolve_provider_credentials("auto") == ("kimi", "sk-moonshot-env")

    def test_explicit_kimi_argument(self):
        assert resolve_provider_credentials("kimi", kimi_api_key="sk-arg") == ("kimi", "sk-arg")

    def test_explicit_kimi_without_key_raises(self, monkeypatch):
        monkeypatch.delenv("KIMI_API_KEY", raising=False)
        monkeypatch.delenv("MOONSHOT_API_KEY", raising=False)
        monkeypatch.setenv("OPENAI_API_KEY", "sk-openai")
        with pytest.raises(ValueError, match="KIMI_API_KEY or MOONSHOT_API_KEY"):
            resolve_provider_credentials("kimi")

    def test_openai_still_wins_over_kimi(self, monkeypatch):
        """Original precedence is preserved: OpenAI beats Kimi in auto mode."""
        monkeypatch.setenv("OPENAI_API_KEY", "sk-openai")
        monkeypatch.setenv("KIMI_API_KEY", "sk-kimi")
        assert resolve_provider_credentials("auto") == ("openai", "sk-openai")

    def test_resolve_provider_kimi_default_model(self):
        prov, key, model = resolve_provider("kimi", kimi_api_key="sk-kimi")
        assert prov == "kimi"
        assert key == "sk-kimi"
        assert model == DEFAULT_KIMI_MODEL

    def test_resolve_provider_kimi_cli_default_model_resolves(self):
        """A caller passing the generic CLI default model gets the Kimi default."""
        prov, key, model = resolve_provider("kimi", kimi_api_key="sk-kimi", model=DEFAULT_MODEL)
        assert prov == "kimi"
        assert model == DEFAULT_KIMI_MODEL

    def test_resolve_provider_kimi_explicit_model(self):
        prov, key, model = resolve_provider(
            "kimi", kimi_api_key="sk-kimi", model="kimi-k2-0905-preview"
        )
        assert model == "kimi-k2-0905-preview"


class TestKimiProvider:
    def test_missing_key_raises(self, monkeypatch):
        monkeypatch.delenv("KIMI_API_KEY", raising=False)
        monkeypatch.delenv("MOONSHOT_API_KEY", raising=False)
        with pytest.raises(LLMError, match="KIMI_API_KEY"):
            KimiProvider(api_key=None)

    def test_init_sets_model_and_openai_backend(self):
        provider = KimiProvider(api_key="sk-kimi", model="kimi-k2.6")
        assert provider.model_name == "kimi-k2.6"
        # Underlying backend is OpenAI-compatible
        assert provider.provider_name == "openai"

    def test_default_model(self):
        provider = KimiProvider(api_key="sk-kimi")
        assert provider.model_name == DEFAULT_KIMI_MODEL

    def test_provider_prefix_stripped(self):
        provider = KimiProvider(api_key="sk-kimi", model="kimi:kimi-k2-0905-preview")
        assert provider.model_name == "kimi-k2-0905-preview"

    def test_luna_names_not_used_by_default(self):
        """Kimi must go through chat completions, not the Responses API."""
        provider = KimiProvider(api_key="sk-kimi")
        assert "-luna" not in provider.model_name.lower()

    def test_kimi_code_gateway_env_ignored(self, monkeypatch):
        """KIMI_BASE_URL from the Kimi Code CLI must not hijack the endpoint."""
        monkeypatch.setenv("KIMI_BASE_URL", "https://agent-gw.kimi.com/coding/")
        provider = KimiProvider(api_key="sk-kimi")
        model = provider._model_instance
        client = model.client
        base_url = str(client.base_url)
        assert "agent-gw" not in base_url
        assert "moonshot" in base_url

    def test_explicit_base_url_honored(self, monkeypatch):
        monkeypatch.setenv("KIMI_BASE_URL", "https://agent-gw.kimi.com/coding/")
        provider = KimiProvider(api_key="sk-kimi", base_url="https://api.moonshot.cn/v1")
        base_url = str(provider._model_instance.client.base_url)
        assert base_url.startswith("https://api.moonshot.cn/")

    def test_thinking_disabled_in_model_settings(self):
        provider = KimiProvider(api_key="sk-kimi")
        extra_body = provider._agent.model_settings.get("extra_body")
        assert extra_body == {
            "thinking": {"type": "disabled"},
            "max_completion_tokens": DEFAULT_KIMI_MAX_COMPLETION_TOKENS,
        }

    def test_max_completion_tokens_applied_to_both_agents(self):
        provider = KimiProvider(api_key="sk-kimi")
        for agent in (provider._agent, provider._fallback_agent):
            extra_body = agent.model_settings.get("extra_body")
            assert extra_body["max_completion_tokens"] == 1000
            assert extra_body["thinking"] == {"type": "disabled"}

    def test_k26_default_model_keeps_thinking_disabled(self):
        provider = KimiProvider(api_key="sk-kimi")
        assert provider.model_name == DEFAULT_KIMI_MODEL
        extra_body = provider._agent.model_settings.get("extra_body")
        assert extra_body["thinking"] == {"type": "disabled"}
        assert extra_body["max_completion_tokens"] == DEFAULT_KIMI_MAX_COMPLETION_TOKENS

    def test_k27_models_keep_thinking_enabled(self):
        provider = KimiProvider(api_key="sk-kimi", model="kimi-k2.7-code-highspeed")
        extra_body = provider._agent.model_settings.get("extra_body")
        assert extra_body["thinking"] == {"type": "enabled"}
        assert extra_body["max_completion_tokens"] == 1000

    def test_get_provider_factory(self):
        provider = get_provider(provider="kimi", api_key="sk-kimi", model=DEFAULT_KIMI_MODEL)
        assert isinstance(provider, KimiProvider)
        assert provider.model_name == DEFAULT_KIMI_MODEL


class _FakeUsage:
    total_tokens = 42
    input_tokens = 30
    output_tokens = 12
    details = {}


class _FakeResult:
    def __init__(self, output_text):
        self.output = output_text

    def usage(self):
        return _FakeUsage()


class TestKimiFallback:
    def test_structured_path_used_first(self, monkeypatch):
        provider = KimiProvider(api_key="sk-kimi")

        def fake_super(
            self, prompt, diff_excerpt, stats_json, title, max_retries=3, retry_delay=1.0
        ):
            return {
                "complexity": 6,
                "explanation": "structured",
                "provider": "openai",
                "model": provider.model_name,
                "tokens": 10,
            }

        monkeypatch.setattr("cli.llm.PydanticAIProvider.analyze_complexity", fake_super)
        result = provider.analyze_complexity("p", "d", "s", "t")
        assert result["complexity"] == 6
        assert result["explanation"] == "structured"

    def test_text_fallback_on_structured_failure(self, monkeypatch):
        provider = KimiProvider(api_key="sk-kimi")

        def fake_super(*args, **kwargs):
            raise LLMError("kimi rejected function tools")

        monkeypatch.setattr("cli.llm.PydanticAIProvider.analyze_complexity", fake_super)
        provider._fallback_agent = Mock()
        provider._fallback_agent.run_sync.return_value = _FakeResult(
            'Sure! {"complexity": 5, "explanation": "moderate changes"}'
        )

        result = provider.analyze_complexity("p", "d", "s", "t")
        assert result["complexity"] == 5
        assert result["explanation"] == "moderate changes"
        assert result["model"] == provider.model_name
        assert result["tokens"] == 42

    def test_fallback_failure_raises_llm_error(self, monkeypatch):
        provider = KimiProvider(api_key="sk-kimi")

        def fake_super(*args, **kwargs):
            raise LLMError("structured failed")

        monkeypatch.setattr("cli.llm.PydanticAIProvider.analyze_complexity", fake_super)
        provider._fallback_agent = Mock()
        provider._fallback_agent.run_sync.return_value = _FakeResult("no json here")

        with pytest.raises(LLMError, match="text fallback failed"):
            provider.analyze_complexity("p", "d", "s", "t")
