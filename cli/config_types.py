"""Typed configuration classes for PR analysis."""

from dataclasses import dataclass, field
from typing import Literal, Optional, TYPE_CHECKING

from .constants import (
    DEFAULT_HUNKS_PER_FILE,
    DEFAULT_MAX_TOKENS,
    DEFAULT_MODEL,
    DEFAULT_SLEEP_SECONDS,
    DEFAULT_TIMEOUT,
)

if TYPE_CHECKING:
    from .github import TokenRotator


@dataclass
class AnalysisConfig:
    """Configuration for PR analysis."""

    # LLM settings
    model: str = DEFAULT_MODEL
    timeout: float = DEFAULT_TIMEOUT
    max_tokens: int = DEFAULT_MAX_TOKENS

    # Diff processing
    hunks_per_file: int = DEFAULT_HUNKS_PER_FILE

    # API settings
    sleep_seconds: float = DEFAULT_SLEEP_SECONDS

    # LLM provider selection
    provider: str = "auto"

    # PR source (forge) selection: auto-detect from URL by default
    source: str = "auto"

    # Credentials (optional - can be provided at runtime)
    github_token: Optional[str] = None
    openai_key: Optional[str] = None
    gemini_key: Optional[str] = None
    anthropic_key: Optional[str] = None
    kimi_key: Optional[str] = None

    # Azure DevOps personal access token (optional)
    ado_pat: Optional[str] = None

    # Token rotation (optional)
    token_rotator: Optional["TokenRotator"] = None

    # Custom prompt (optional)
    prompt_text: Optional[str] = None

    def __post_init__(self) -> None:
        """Validate configuration values."""
        if self.timeout <= 0:
            raise ValueError(f"timeout must be positive, got {self.timeout}")
        if self.max_tokens <= 0:
            raise ValueError(f"max_tokens must be positive, got {self.max_tokens}")
        if self.hunks_per_file <= 0:
            raise ValueError(f"hunks_per_file must be positive, got {self.hunks_per_file}")
        if self.sleep_seconds < 0:
            raise ValueError(f"sleep_seconds cannot be negative, got {self.sleep_seconds}")
        if not self.model or not self.model.strip():
            raise ValueError("model cannot be empty")
        if self.provider not in ("auto", "gemini", "openai", "anthropic", "kimi"):
            raise ValueError(
                f"provider must be 'auto', 'gemini', 'openai', 'anthropic', or 'kimi', "
                f"got '{self.provider}'"
            )
        if self.source not in ("auto", "github", "azure-devops"):
            raise ValueError(
                f"source must be 'auto', 'github', or 'azure-devops', got '{self.source}'"
            )

    @classmethod
    def from_env(cls, **kwargs) -> "AnalysisConfig":
        """Create AnalysisConfig with defaults resolved from environment variables."""
        from .config import (
            get_anthropic_api_key,
            get_gemini_api_key,
            get_github_token,
            get_kimi_api_key,
            get_openai_api_key,
        )

        kwargs.setdefault("github_token", get_github_token())
        kwargs.setdefault("openai_key", get_openai_api_key())
        kwargs.setdefault("gemini_key", get_gemini_api_key())
        kwargs.setdefault("anthropic_key", get_anthropic_api_key())
        kwargs.setdefault("kimi_key", get_kimi_api_key())
        return cls(**kwargs)


@dataclass
class BatchConfig:
    """Configuration for batch PR analysis."""

    # Base analysis config
    analysis: AnalysisConfig = field(default_factory=AnalysisConfig)

    # Batch-specific settings
    workers: int = 1
    resume: bool = True

    # Labeling options
    label_prs: bool = False
    label_prefix: str = "complexity:"
    force: bool = False

    # Output
    output_file: Optional[str] = None

    def __post_init__(self) -> None:
        """Validate configuration values."""
        if self.workers < 1:
            raise ValueError(f"workers must be >= 1, got {self.workers}")
        if self.label_prs and not self.label_prefix:
            raise ValueError("label_prefix cannot be empty when label_prs is True")


@dataclass
class OutputConfig:
    """Configuration for output formatting."""

    format: Literal["json", "markdown"] = "json"
    output_file: Optional[str] = None
    write_github_output: bool = True

    def __post_init__(self) -> None:
        """Validate configuration values."""
        if self.format not in ("json", "markdown"):
            raise ValueError(f"format must be 'json' or 'markdown', got '{self.format}'")
