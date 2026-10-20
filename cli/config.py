"""Configuration parsing and validation."""

import os
import re
from typing import List, Optional

from .constants import TOKEN_VISIBLE_CHARS


def get_github_token() -> Optional[str]:
    """Get GitHub token from environment.

    Checks GH_TOKEN first (GitHub CLI convention), then GITHUB_TOKEN.
    """
    return os.getenv("GH_TOKEN") or os.getenv("GITHUB_TOKEN")


def get_github_tokens() -> List[str]:
    """Get multiple GitHub tokens from environment.

    Checks GH_TOKENS first (comma-separated), then falls back to single token.
    Tokens can be comma-separated or newline-separated.

    Environment variables (in order of priority):
    - GH_TOKENS: Comma or newline-separated list of tokens
    - GITHUB_TOKENS: Comma or newline-separated list of tokens
    - GH_TOKEN: Single token (fallback)
    - GITHUB_TOKEN: Single token (fallback)

    Returns:
        List of GitHub tokens (empty list if none found)
    """
    # Check for multiple tokens first
    tokens_str = os.getenv("GH_TOKENS") or os.getenv("GITHUB_TOKENS")
    if tokens_str:
        # Support both comma and newline separators
        tokens = []
        for line in tokens_str.replace("\n", ",").split(","):
            token = line.strip()
            if token:
                tokens.append(token)
        if tokens:
            return tokens

    # Fall back to single token
    single_token = get_github_token()
    if single_token:
        return [single_token]

    return []


def get_openai_api_key() -> Optional[str]:
    """Get OpenAI API key from environment."""
    return os.getenv("OPENAI_API_KEY")


def get_gemini_api_key() -> Optional[str]:
    """Get Gemini API key from environment.

    Checks GEMINI_API_KEY first, then falls back to GOOGLE_API_KEY.
    """
    return os.getenv("GEMINI_API_KEY") or os.getenv("GOOGLE_API_KEY")


def get_openai_base_url() -> Optional[str]:
    """Get base URL for OpenAI-compatible API from environment."""
    return os.getenv("OPENAI_BASE_URL") or None


def get_anthropic_api_key() -> Optional[str]:
    """Get Anthropic API key from environment."""
    return os.getenv("ANTHROPIC_API_KEY")


def get_anthropic_base_url() -> Optional[str]:
    """Get Anthropic base URL from environment."""
    return os.getenv("ANTHROPIC_BASE_URL") or None


def get_kimi_api_key() -> Optional[str]:
    """Get Kimi (Moonshot AI) API key from environment.

    Checks KIMI_API_KEY first, then falls back to MOONSHOT_API_KEY.
    """
    return os.getenv("KIMI_API_KEY") or os.getenv("MOONSHOT_API_KEY")


def get_kimi_base_url() -> Optional[str]:
    """Get Kimi base URL override from environment."""
    return os.getenv("KIMI_BASE_URL") or None


def get_ado_pat() -> Optional[str]:
    """Get Azure DevOps personal access token from environment.

    Checks AZURE_DEVOPS_PAT first, then falls back to AZURE_DEVOPS_TOKEN.
    """
    return os.getenv("AZURE_DEVOPS_PAT") or os.getenv("AZURE_DEVOPS_TOKEN")


def get_ado_server_url() -> Optional[str]:
    """Get Azure DevOps Server base URL from environment."""
    return os.getenv("AZURE_DEVOPS_SERVER_URL") or None


def get_ado_collection() -> Optional[str]:
    """Get default Azure DevOps collection from environment."""
    return os.getenv("AZURE_DEVOPS_COLLECTION") or None


def get_ado_project() -> Optional[str]:
    """Get default Azure DevOps project from environment."""
    return os.getenv("AZURE_DEVOPS_PROJECT") or None


def validate_owner_repo(owner: str, repo: str) -> None:
    """Validate owner and repo names."""
    pattern = re.compile(r"^[A-Za-z0-9_.-]+$")
    if not pattern.match(owner):
        raise ValueError(f"Invalid owner name: {owner}")
    if not pattern.match(repo):
        raise ValueError(f"Invalid repo name: {repo}")


def validate_pr_number(pr: int) -> None:
    """Validate PR number."""
    if pr <= 0:
        raise ValueError(f"PR number must be positive, got: {pr}")


def redact_secret(value: str, visible_chars: int = TOKEN_VISIBLE_CHARS) -> str:
    """Redact a secret value for logging."""
    if len(value) <= visible_chars:
        return "*" * len(value)
    return value[:visible_chars] + "*" * (len(value) - visible_chars)
