"""Core PR analysis logic extracted from main.py."""

import json
import re
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Dict, Optional

from .config_types import AnalysisConfig
from .github import fetch_pr, fetch_pr_with_rotation
from .io_safety import read_text_file
from .llm import get_provider, resolve_provider
from .preprocess import make_prompt_input, process_diff
from . import sources
from .sources import resolve_pr_ref

_SYNC_TITLE_RE = re.compile(
    r"synced (?:local )?file\(s\)",
    re.IGNORECASE,
)
_SYNC_BODY_RE = re.compile(
    r"(?:repo-file-sync-action|created automatically by|This PR was created automatically)",
    re.IGNORECASE,
)
_SYNC_BOT_LOGINS = frozenset(
    {
        "github-actions[bot]",
        "github-actions",
        "repo-file-sync-action",
        "repo-file-sync-action[bot]",
        "dependabot[bot]",
        "renovate[bot]",
    }
)


def is_automated_sync_pr(
    title: str,
    author_login: Optional[str],
    body: Optional[str] = None,
) -> bool:
    """
    Detect automated cross-repo file sync PRs.

    Requires the title to look like a sync/mirror PR (e.g. "synced file(s)")
    AND a corroborating bot signal — either a known bot login OR a body
    signature from a sync action (since sync workflows often commit under a
    human PAT, making author_login alone unreliable).
    """
    if not title or not _SYNC_TITLE_RE.search(title):
        return False

    if author_login and author_login.lower() in {login.lower() for login in _SYNC_BOT_LOGINS}:
        return True

    if body and _SYNC_BODY_RE.search(body):
        return True

    return False


def load_prompt(prompt_file: Optional[Path] = None) -> str:
    """
    Load prompt from file or use default embedded prompt.

    Args:
        prompt_file: Optional path to custom prompt file

    Returns:
        Prompt text

    Raises:
        FileNotFoundError: If prompt file not found
    """
    if prompt_file:
        if not prompt_file.exists():
            raise FileNotFoundError(f"Prompt file not found: {prompt_file}")
        return read_text_file(prompt_file)

    # Load default embedded prompt
    default_prompt_path = Path(__file__).parent / "prompt" / "default.txt"
    if not default_prompt_path.exists():
        raise FileNotFoundError(f"Default prompt not found: {default_prompt_path}")
    return read_text_file(default_prompt_path)


def analyze_single_pr(
    pr_url: str,
    config: AnalysisConfig,
    progress_callback: Optional[Callable[[str], None]] = None,
) -> Dict[str, Any]:
    """
    Analyze a single PR and return the result.

    This is the core analysis function that can be reused by both single PR
    and batch analysis workflows.

    Args:
        pr_url: GitHub PR URL
        config: Analysis configuration
        progress_callback: Optional callback for progress messages

    Returns:
        Dict with keys: score, explanation, provider, model, tokens, timestamp,
        repo, pr, url, title

    Raises:
        ValueError: If PR URL is invalid or config is missing required values
        GitHubAPIError: If GitHub API call fails
        LLMError: If LLM call fails
        InvalidResponseError: If LLM response is invalid
    """
    effective_prov, effective_key, effective_model = resolve_provider(
        provider=config.provider,
        openai_api_key=config.openai_key,
        gemini_api_key=config.gemini_key,
        anthropic_api_key=config.anthropic_key,
        kimi_api_key=config.kimi_key,
        model=config.model,
    )

    # Delegate provider/model/key resolution directly to llm.get_provider factory
    provider_inst = get_provider(
        provider=effective_prov,
        api_key=effective_key,
        model=effective_model,
        timeout=config.timeout,
    )

    # Resolve PR reference (GitHub or Azure DevOps) from the URL
    ref = resolve_pr_ref(pr_url, source=config.source, ado_pat=config.ado_pat)

    # Load prompt if not provided
    prompt_text = config.prompt_text
    if not prompt_text:
        prompt_text = load_prompt()

    # Fetch PR - use token rotator for GitHub when available, otherwise
    # dispatch to the forge-specific client (GitHub or Azure DevOps).
    if ref.source == "github":
        if config.token_rotator:
            diff_text, meta = fetch_pr_with_rotation(
                ref.owner,
                ref.repo,
                ref.pr,
                config.token_rotator,
                sleep_s=config.sleep_seconds,
                progress_callback=progress_callback,
                timeout=config.timeout,
            )
        else:
            diff_text, meta = fetch_pr(
                ref.owner,
                ref.repo,
                ref.pr,
                config.github_token,
                sleep_s=config.sleep_seconds,
                progress_callback=progress_callback,
            )
    else:
        diff_text, meta = sources.fetch_pr(
            ref,
            github_token=config.github_token,
            sleep_s=config.sleep_seconds,
            progress_callback=progress_callback,
            timeout=config.timeout,
        )

    title = (meta.get("title") or "").strip()
    author_login = (meta.get("user") or {}).get("login")
    body = meta.get("body") or ""

    # Short-circuit automated cross-repo file sync PRs: they are mechanical
    # mirrors of upstream files, so there is no implementation effort to score.
    if is_automated_sync_pr(title, author_login, body):
        return {
            "score": 1,
            "explanation": "Automated cross-repo file sync; no implementation work.",
            "provider": "heuristic",
            "model": "sync-shortcircuit",
            "tokens": None,
            "timestamp": datetime.utcnow().isoformat() + "Z",
            "repo": ref.repo_slug,
            "pr": ref.pr,
            "url": pr_url,
            "title": title,
        }

    # Process diff
    truncated_diff, stats, selected_files = process_diff(
        diff_text, meta, max_tokens=config.max_tokens, hunks_per_file=config.hunks_per_file
    )

    # Format prompt input
    diff_for_prompt = make_prompt_input(pr_url, title, stats, selected_files, truncated_diff)

    # Call LLM
    result = provider_inst.analyze_complexity(
        prompt=prompt_text,
        diff_excerpt=diff_for_prompt,
        stats_json=json.dumps(stats),
        title=title,
    )

    # Prepare output
    return {
        "score": result["complexity"],
        "explanation": result["explanation"],
        "provider": result.get("provider", provider_inst.provider_name),
        "model": result.get("model", provider_inst.model_name),
        "tokens": result.get("tokens"),
        "timestamp": datetime.utcnow().isoformat() + "Z",
        "repo": ref.repo_slug,
        "pr": ref.pr,
        "url": pr_url,
        "title": title,
    }


def handle_dry_run(
    pr_url: str,
    config: AnalysisConfig,
    progress_callback: Optional[Callable[[str], None]] = None,
) -> Dict[str, Any]:
    """
    Handle dry-run mode - fetch and process PR but don't call LLM.

    Args:
        pr_url: GitHub PR URL
        config: Analysis configuration
        progress_callback: Optional callback for progress messages

    Returns:
        Dict with dry run information including diff stats

    Raises:
        ValueError: If PR URL is invalid
        GitHubAPIError: If GitHub API call fails
    """
    # Resolve PR reference (GitHub or Azure DevOps) from the URL
    ref = resolve_pr_ref(pr_url, source=config.source, ado_pat=config.ado_pat)

    # Fetch PR
    if ref.source == "github":
        if config.token_rotator:
            diff_text, meta = fetch_pr_with_rotation(
                ref.owner,
                ref.repo,
                ref.pr,
                config.token_rotator,
                sleep_s=config.sleep_seconds,
                progress_callback=progress_callback,
                timeout=config.timeout,
            )
        else:
            diff_text, meta = fetch_pr(
                ref.owner,
                ref.repo,
                ref.pr,
                config.github_token,
                sleep_s=config.sleep_seconds,
                progress_callback=progress_callback,
            )
    else:
        diff_text, meta = sources.fetch_pr(
            ref,
            github_token=config.github_token,
            sleep_s=config.sleep_seconds,
            progress_callback=progress_callback,
            timeout=config.timeout,
        )

    title = (meta.get("title") or "").strip()

    # Process diff
    truncated_diff, stats, selected_files = process_diff(
        diff_text, meta, max_tokens=config.max_tokens, hunks_per_file=config.hunks_per_file
    )

    return {
        "dry_run": True,
        "title": title,
        "diff_excerpt_length": len(truncated_diff),
        "selected_files_count": len(selected_files),
        "selected_files": selected_files,
        "stats": stats,
        "repo": ref.repo_slug,
        "pr": ref.pr,
        "url": pr_url,
    }


def format_output(result: Dict[str, Any], output_format: str) -> str:
    """
    Format analysis result for output.

    Args:
        result: Analysis result dict
        output_format: Output format ("json" or "markdown")

    Returns:
        Formatted output string
    """
    if output_format == "markdown":
        return f"""# PR Complexity Analysis

**Score:** {result['score']}/10

**Explanation:** {result['explanation']}

**Details:**
- Repository: {result['repo']}
- PR: #{result['pr']}
- Model: {result['model']}
- Tokens used: {result.get('tokens', 'N/A')}
"""
    else:
        # JSON output
        return json.dumps(
            {
                "score": result["score"],
                "explanation": result["explanation"],
                "provider": result["provider"],
                "model": result["model"],
                "tokens": result.get("tokens"),
                "timestamp": result["timestamp"],
            },
            ensure_ascii=False,
            indent=2,
        )
