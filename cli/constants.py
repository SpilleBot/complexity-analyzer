"""Centralized constants for the CLI."""

# Timeouts
DEFAULT_TIMEOUT = 120.0

# API sleep/delay
DEFAULT_SLEEP_SECONDS = 0.7

# Token display
TOKEN_VISIBLE_CHARS = 4

# LLM defaults
DEFAULT_MODEL = "gpt-5.2"
DEFAULT_GEMINI_MODEL = "gemini-flash-latest"
DEFAULT_ANTHROPIC_MODEL = "claude-sonnet-4-5"
DEFAULT_KIMI_MODEL = "kimi-k2.6"
DEFAULT_KIMI_MAX_COMPLETION_TOKENS = 1000
DEFAULT_MAX_TOKENS = 50000
DEFAULT_HUNKS_PER_FILE = 2

# Kimi (Moonshot AI) OpenAI-compatible API
KIMI_BASE_URL = "https://api.moonshot.ai/v1"

# Azure DevOps Server API
ADO_API_VERSION = "7.0"

# Environment Variables
ANTHROPIC_API_KEY = "ANTHROPIC_API_KEY"
ANTHROPIC_BASE_URL = "ANTHROPIC_BASE_URL"


# Rate limits
MAX_RATE_LIMIT_WAIT = 3600  # 1 hour
RATE_LIMIT_CACHE_SECONDS = 30

# CSV
CSV_BATCH_SIZE = 1  # Flush every row immediately so --resume survives interrupts

# Retry settings
DEFAULT_MAX_RETRIES = 3
DEFAULT_RETRY_DELAY = 1.0

# GitHub API
GITHUB_API_VERSION = "2022-11-28"
GITHUB_API_BASE_URL = "https://api.github.com"
GITHUB_PER_PAGE = 100  # Max items per page for GitHub API
