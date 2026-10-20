"""Main CLI entry point."""

import json
import os
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable, List, Optional

import typer
from dotenv import load_dotenv
from typer.core import TyperGroup

# Load environment variables from .env file
load_dotenv()

from .analyze import is_automated_sync_pr, load_prompt  # noqa: E402
from .batch import (  # noqa: E402
    generate_pr_list_from_date_range,
    load_pr_urls_from_file,
    run_batch_analysis_with_labels,
)
from . import ado, sources  # noqa: E402
from .sources import resolve_pr_ref  # noqa: E402
from .config import (  # noqa: E402
    get_ado_collection,
    get_ado_pat,
    get_ado_server_url,
    get_anthropic_api_key,
    get_gemini_api_key,
    get_github_token,
    get_github_tokens,
    get_kimi_api_key,
    get_openai_api_key,
    get_openai_base_url,
)
from .constants import (  # noqa: E402
    DEFAULT_HUNKS_PER_FILE,
    DEFAULT_MAX_TOKENS,
    DEFAULT_MODEL,
    DEFAULT_SLEEP_SECONDS,
    DEFAULT_TIMEOUT,
)
from .errors import ErrorHandler  # noqa: E402
from .github import (  # noqa: E402
    GitHubAPIError,
    TokenRotator,
    check_rate_limit,
    fetch_pr,
    fetch_pr_with_rotation,
)
from .io_safety import normalize_path, write_json_atomic  # noqa: E402
from .llm import (  # noqa: E402
    LLMError,
    get_provider,
    resolve_provider,
    resolve_provider_credentials,
)
from .logging_config import get_logger, setup_logging  # noqa: E402
from .preprocess import make_prompt_input, process_diff  # noqa: E402
from .scoring import InvalidResponseError  # noqa: E402
from .utils import is_ado_pr_url  # noqa: E402

# Keep regex for direct URL validation in main callback
_OWNER_REPO_RE = re.compile(r"https?://github\.com/([^/\s]+)/([^/\s]+)/pull/(\d+)")


def _is_supported_pr_url(url: str) -> bool:
    """Return True for GitHub or Azure DevOps pull request URLs."""
    return bool(_OWNER_REPO_RE.match(url.strip())) or is_ado_pr_url(url)


class _DirectUrlGroup(TyperGroup):
    """Click group that routes `complexity-cli <PR_URL>` to the analyze-pr command.

    Click resolves the first positional argument as a command name, so a bare
    PR URL would otherwise fail with "No such command". Rewriting the argument
    list here keeps the original direct-invocation interface working.
    """

    def resolve_command(self, ctx, args):
        if args and _is_supported_pr_url(args[0]):
            args = ["analyze-pr", *args]
        return super().resolve_command(ctx, args)


app = typer.Typer(
    help="Analyze pull request complexity using LLMs (GitHub and Azure DevOps)",
    cls=_DirectUrlGroup,
)

# Initialize logger
logger = get_logger()


def analyze_pr_to_dict(
    pr_url: str,
    prompt_text: str,
    github_token: Optional[str],
    openai_key: Optional[str] = None,
    model: str = DEFAULT_MODEL,
    timeout: float = DEFAULT_TIMEOUT,
    max_tokens: int = DEFAULT_MAX_TOKENS,
    hunks_per_file: int = DEFAULT_HUNKS_PER_FILE,
    sleep_seconds: float = DEFAULT_SLEEP_SECONDS,
    progress_callback: Optional[Callable[[str], None]] = None,
    token_rotator: Optional[TokenRotator] = None,
    base_url: Optional[str] = None,
    provider: str = "auto",
    gemini_key: Optional[str] = None,
    anthropic_key: Optional[str] = None,
    kimi_key: Optional[str] = None,
    ado_pat: Optional[str] = None,
    source: str = "auto",
) -> dict:
    """
    Analyze a pull request (GitHub or Azure DevOps) and return result as dictionary.

    This is the core analysis function that can be reused by both single PR
    and batch analysis workflows.

    Args:
        pr_url: GitHub or Azure DevOps PR URL
        prompt_text: Prompt text for LLM
        github_token: GitHub API token (optional, ignored if token_rotator is provided)
        openai_key: OpenAI API key (optional)
        model: Model name
        timeout: Request timeout in seconds
        max_tokens: Maximum tokens for diff excerpt
        hunks_per_file: Maximum hunks per file
        sleep_seconds: Sleep between forge API calls
        progress_callback: Optional callback for progress messages (e.g., rate limit warnings)
        token_rotator: Optional TokenRotator for automatic token rotation on rate limits
        base_url: Base URL for OpenAI-compatible API
        provider: LLM provider name ("auto", "gemini", "openai", "anthropic", "kimi")
        gemini_key: Gemini API key (optional)
        anthropic_key: Anthropic API key (optional)
        kimi_key: Kimi / Moonshot API key (optional)
        ado_pat: Azure DevOps personal access token (optional)
        source: Force PR source ("auto", "github", "azure-devops")

    Returns:
        Dict with keys: score, explanation, provider, model, tokens, timestamp,
        repo, pr, url, title

    Raises:
        ValueError: If PR URL is invalid
        GitHubAPIError: If forge API call fails
        LLMError: If LLM call fails
        InvalidResponseError: If LLM response is invalid
    """
    effective_prov, effective_key, effective_model = resolve_provider(
        provider=provider,
        openai_api_key=openai_key,
        gemini_api_key=gemini_key,
        anthropic_api_key=anthropic_key,
        kimi_api_key=kimi_key,
        model=model,
        env_openai_api_key=get_openai_api_key(),
        env_gemini_api_key=get_gemini_api_key(),
        env_anthropic_api_key=get_anthropic_api_key(),
        env_kimi_api_key=get_kimi_api_key(),
    )

    provider_inst = get_provider(
        provider=effective_prov,
        api_key=effective_key,
        model=effective_model,
        timeout=timeout,
        base_url=base_url,
    )

    # Resolve PR reference (GitHub or Azure DevOps) from the URL
    ref = resolve_pr_ref(pr_url, source=source, ado_pat=ado_pat)

    # Fetch PR - use token rotator for GitHub when available
    if ref.source == "github":
        if token_rotator:
            diff_text, meta = fetch_pr_with_rotation(
                ref.owner,
                ref.repo,
                ref.pr,
                token_rotator,
                sleep_s=sleep_seconds,
                progress_callback=progress_callback,
                timeout=timeout,
            )
        else:
            diff_text, meta = fetch_pr(
                ref.owner,
                ref.repo,
                ref.pr,
                github_token,
                sleep_s=sleep_seconds,
                progress_callback=progress_callback,
            )
    else:
        diff_text, meta = sources.fetch_pr(
            ref,
            github_token=github_token,
            sleep_s=sleep_seconds,
            progress_callback=progress_callback,
            timeout=timeout,
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
        diff_text, meta, max_tokens=max_tokens, hunks_per_file=hunks_per_file
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
    output = {
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

    return output


def _analyze_pr_impl(
    pr_url: str,
    prompt_file: Optional[Path] = None,
    model: str = DEFAULT_MODEL,
    format: str = "json",
    output_file: Optional[Path] = None,
    timeout: float = DEFAULT_TIMEOUT,
    max_tokens: int = DEFAULT_MAX_TOKENS,
    hunks_per_file: int = DEFAULT_HUNKS_PER_FILE,
    sleep_seconds: float = DEFAULT_SLEEP_SECONDS,
    dry_run: bool = False,
    post_comment: bool = False,
    openai_api_key: Optional[str] = None,
    github_token: Optional[str] = None,
    verbose: bool = False,
    base_url: Optional[str] = None,
    provider: str = "auto",
    gemini_api_key: Optional[str] = None,
    anthropic_api_key: Optional[str] = None,
    kimi_api_key: Optional[str] = None,
    ado_pat: Optional[str] = None,
    source: str = "auto",
):
    """
    Analyze a pull request (GitHub or Azure DevOps) and compute complexity score.

    Environment variables:
    - GH_TOKEN or GITHUB_TOKEN: GitHub API token (optional for public repos)
    - AZURE_DEVOPS_PAT: Azure DevOps personal access token
    - OPENAI_API_KEY: OpenAI API key
    - GEMINI_API_KEY or GOOGLE_API_KEY: Gemini API key
    - ANTHROPIC_API_KEY: Anthropic API key
    - KIMI_API_KEY or MOONSHOT_API_KEY: Kimi / Moonshot AI API key
    """
    # Set up logging if verbose
    if verbose:
        setup_logging(verbose=True)

    try:
        # Resolve PR reference and validate the URL
        ref = resolve_pr_ref(pr_url, source=source, ado_pat=ado_pat)
        final_github_token = github_token or get_github_token()
        final_ado_pat = ref.ado_pat

        if ref.source == "azure-devops" and not final_ado_pat:
            typer.echo("Error: Azure DevOps PAT is required for Azure DevOps PRs", err=True)
            typer.echo(
                "Set it with: export AZURE_DEVOPS_PAT='your-pat' or pass --ado-pat", err=True
            )
            raise typer.Exit(1)

        # Pre-flight resolution and key validation
        resolve_provider_credentials(
            provider=provider,
            openai_api_key=openai_api_key,
            gemini_api_key=gemini_api_key,
            anthropic_api_key=anthropic_api_key,
            kimi_api_key=kimi_api_key,
            env_openai_api_key=get_openai_api_key(),
            env_gemini_api_key=get_gemini_api_key(),
            env_anthropic_api_key=get_anthropic_api_key(),
            env_kimi_api_key=get_kimi_api_key(),
        )

        # Load prompt
        try:
            prompt_text = load_prompt(prompt_file)
        except FileNotFoundError as e:
            typer.echo(f"Error: {e}", err=True)
            raise typer.Exit(1)

        # Handle dry run
        if dry_run:
            typer.echo(f"Fetching PR {ref.repo_slug}#{ref.pr}...", err=True)
            try:
                if ref.source == "github":
                    diff_text, meta = fetch_pr(
                        ref.owner, ref.repo, ref.pr, final_github_token, sleep_s=sleep_seconds
                    )
                else:
                    diff_text, meta = sources.fetch_pr(
                        ref,
                        github_token=final_github_token,
                        sleep_s=sleep_seconds,
                        timeout=timeout,
                    )
                title = (meta.get("title") or "").strip()
                typer.echo(f"PR: {title}", err=True)
                typer.echo("Processing diff...", err=True)
                truncated_diff, stats, selected_files = process_diff(
                    diff_text, meta, max_tokens=max_tokens, hunks_per_file=hunks_per_file
                )
                typer.echo("Dry run: Skipping LLM call", err=True)
                typer.echo(f"Diff excerpt length: {len(truncated_diff)} chars", err=True)
                typer.echo(f"Selected files: {len(selected_files)}", err=True)
                raise typer.Exit(0)
            except GitHubAPIError as e:
                if e.status_code == 404:
                    ErrorHandler.handle_github_404(
                        ref.owner or ref.project or "?",
                        ref.repo or "?",
                        ref.pr or 0,
                        bool(final_github_token or final_ado_pat),
                    )
                else:
                    ErrorHandler.handle_github_error(e)
                raise typer.Exit(1)
            except typer.Exit:
                # Re-raise typer.Exit (success case) without catching it
                raise
            except (ValueError, RuntimeError) as e:
                typer.echo(f"Failed to fetch PR: {e}", err=True)
                logger.debug("Fetch error", exc_info=True)
                raise typer.Exit(1)

        # Analyze PR
        typer.echo(f"Fetching PR {ref.repo_slug}#{ref.pr}...", err=True)
        try:
            output = analyze_pr_to_dict(
                pr_url=pr_url,
                prompt_text=prompt_text,
                github_token=final_github_token,
                openai_key=openai_api_key,
                gemini_key=gemini_api_key,
                anthropic_key=anthropic_api_key,
                kimi_key=kimi_api_key,
                ado_pat=final_ado_pat,
                source=source,
                provider=provider,
                model=model,
                timeout=timeout,
                max_tokens=max_tokens,
                hunks_per_file=hunks_per_file,
                sleep_seconds=sleep_seconds,
                base_url=base_url,
            )

            typer.echo(f"PR: {output['title']}", err=True)
            typer.echo("Processing diff...", err=True)
            typer.echo("Analyzing complexity with LLM...", err=True)
        except GitHubAPIError as e:
            if e.status_code == 404:
                ErrorHandler.handle_github_404(
                    ref.owner or ref.project or "?",
                    ref.repo or "?",
                    ref.pr or 0,
                    bool(final_github_token or final_ado_pat),
                )
            else:
                ErrorHandler.handle_github_error(e)
            raise typer.Exit(1)
        except LLMError as e:
            ErrorHandler.handle_llm_error(e)
            raise typer.Exit(1)
        except InvalidResponseError as e:
            typer.echo(f"Invalid LLM response: {e}", err=True)
            raise typer.Exit(1)
        except (ValueError, RuntimeError) as e:
            typer.echo(f"Error: {e}", err=True)
            logger.debug("Analysis error", exc_info=True)
            raise typer.Exit(1)

        # Output
        if format == "markdown":
            md = f"""# PR Complexity Analysis

**Score:** {output['score']}/10

**Explanation:** {output['explanation']}

**Details:**
- Repository: {output['repo']}
- PR: #{output['pr']}
- Model: {output['model']}
- Tokens used: {output.get('tokens', 'N/A')}
"""
            typer.echo(md)
        else:
            # JSON output
            json_output = json.dumps(
                {
                    "score": output["score"],
                    "explanation": output["explanation"],
                    "provider": output["provider"],
                    "model": output["model"],
                    "tokens": output.get("tokens"),
                    "timestamp": output["timestamp"],
                },
                ensure_ascii=False,
                indent=2,
            )
            typer.echo(json_output)

        # Set GitHub Action outputs
        if "GITHUB_OUTPUT" in os.environ:
            with open(os.environ["GITHUB_OUTPUT"], "a") as f:
                # Score
                f.write(f"score={output['score']}\n")

                # Explanation (handle multiline)
                explanation = output["explanation"]
                if "\n" in explanation:
                    delimiter = "EOF"
                    # Generate random delimiter to avoid conflicts? Simple EOF is usually fine for this content.
                    f.write(f"explanation<<{delimiter}\n{explanation}\n{delimiter}\n")
                else:
                    f.write(f"explanation={explanation}\n")

                # Full JSON output
                full_output_json = json.dumps(output, ensure_ascii=False)
                f.write(f"output={full_output_json}\n")

                # Model used
                f.write(f"model={output.get('model', 'unknown')}\n")

        # Write to file if requested
        if output_file:
            try:
                # Normalize path for safety
                if output_file.is_absolute():
                    # Allow absolute paths but warn
                    output_path = output_file
                else:
                    # Relative to current directory
                    output_path = normalize_path(Path.cwd(), str(output_file))

                write_json_atomic(output_path, output)
                typer.echo(f"Output written to: {output_path}", err=True)
            except Exception as e:
                typer.echo(f"Warning: Failed to write output file: {e}", err=True)

    except KeyboardInterrupt:
        typer.echo("\nInterrupted by user", err=True)
        raise typer.Exit(130)
    except typer.Exit:
        raise
    except (ValueError, LLMError, RuntimeError) as e:
        typer.echo(f"Error: {e}", err=True)
        raise typer.Exit(1)
    except Exception as e:
        ErrorHandler.handle_unexpected_error(e, debug=bool(os.getenv("DEBUG")))
        raise typer.Exit(1)


def get_pr_url_from_context() -> Optional[str]:
    """Get PR URL from GitHub Actions context."""
    event_path = os.getenv("GITHUB_EVENT_PATH")
    if not event_path or not os.path.exists(event_path):
        return None

    try:
        with open(event_path, "r") as f:
            event_data = json.load(f)
        return event_data.get("pull_request", {}).get("html_url")
    except (json.JSONDecodeError, IOError):
        return None


@app.command(name="analyze-pr")
def analyze_pr(
    pr_url: Optional[str] = typer.Argument(
        None,
        help="GitHub or Azure DevOps PR URL. If not provided, will try to infer from GitHub Actions context.",
    ),
    prompt_file: Optional[Path] = typer.Option(
        None, "--prompt-file", "-p", help="Path to custom prompt file (default: embedded prompt)"
    ),
    provider: str = typer.Option(
        "auto",
        "--provider",
        help="LLM provider: gemini, openai, anthropic, kimi, or auto",
    ),
    model: str = typer.Option(DEFAULT_MODEL, "--model", "-m", help="Model name"),
    format: str = typer.Option("json", "--format", "-f", help="Output format: json or markdown"),
    output_file: Optional[Path] = typer.Option(
        None, "--output-file", "-o", help="Write output to file"
    ),
    timeout: float = typer.Option(
        DEFAULT_TIMEOUT, "--timeout", "-t", help="Request timeout in seconds"
    ),
    max_tokens: int = typer.Option(
        DEFAULT_MAX_TOKENS, "--max-tokens", help="Maximum tokens for diff excerpt"
    ),
    hunks_per_file: int = typer.Option(
        DEFAULT_HUNKS_PER_FILE, "--hunks-per-file", help="Maximum hunks per file"
    ),
    sleep_seconds: float = typer.Option(
        DEFAULT_SLEEP_SECONDS, "--sleep-seconds", help="Sleep between forge API calls"
    ),
    dry_run: bool = typer.Option(False, "--dry-run", help="Fetch PR but don't call LLM"),
    gemini_api_key: Optional[str] = typer.Option(
        None, "--gemini-api-key", help="Gemini / Google AI Studio API key"
    ),
    openai_api_key: Optional[str] = typer.Option(None, "--openai-api-key", help="OpenAI API key"),
    anthropic_api_key: Optional[str] = typer.Option(
        None, "--anthropic-api-key", help="Anthropic API key"
    ),
    kimi_api_key: Optional[str] = typer.Option(
        None, "--kimi-api-key", help="Kimi / Moonshot AI API key"
    ),
    github_token: Optional[str] = typer.Option(None, "--github-token", help="GitHub token"),
    ado_pat: Optional[str] = typer.Option(
        None, "--ado-pat", help="Azure DevOps personal access token (PAT)"
    ),
    source: str = typer.Option(
        "auto",
        "--source",
        help="PR source: github, azure-devops, or auto (auto-detected from URL)",
    ),
    api_base_url: Optional[str] = typer.Option(
        None, "--api-base-url", help="Base URL for OpenAI-compatible API endpoint"
    ),
    verbose: bool = typer.Option(False, "--verbose", "-v", help="Enable verbose logging"),
):
    """Analyze a GitHub or Azure DevOps PR and compute complexity score."""
    final_pr_url = pr_url
    if not final_pr_url:
        typer.echo(
            "PR URL not provided, attempting to infer from GitHub Actions context...", err=True
        )
        final_pr_url = get_pr_url_from_context()
        if not final_pr_url:
            typer.echo("Error: Could not infer PR URL from context.", err=True)
            typer.echo("Please provide the PR URL as an argument.", err=True)
            raise typer.Exit(1)
        typer.echo(f"Inferred PR URL: {final_pr_url}", err=True)

    _analyze_pr_impl(
        pr_url=final_pr_url,
        prompt_file=prompt_file,
        model=model,
        format=format,
        output_file=output_file,
        timeout=timeout,
        max_tokens=max_tokens,
        hunks_per_file=hunks_per_file,
        sleep_seconds=sleep_seconds,
        dry_run=dry_run,
        openai_api_key=openai_api_key,
        gemini_api_key=gemini_api_key,
        anthropic_api_key=anthropic_api_key,
        kimi_api_key=kimi_api_key,
        ado_pat=ado_pat,
        source=source,
        provider=provider,
        github_token=github_token,
        verbose=verbose,
        base_url=api_base_url or get_openai_base_url(),
    )


@app.command(name="batch-analyze")
def batch_analyze(
    input_file: Optional[Path] = typer.Option(
        None, "--input-file", "-i", help="File containing PR URLs (one per line)"
    ),
    org: Optional[str] = typer.Option(
        None, "--org", help="Organization name (for date range search)"
    ),
    since: Optional[str] = typer.Option(
        None, "--since", help="Start date (YYYY-MM-DD) for date range search"
    ),
    until: Optional[str] = typer.Option(
        None, "--until", help="End date (YYYY-MM-DD) for date range search"
    ),
    output_file: Optional[Path] = typer.Option(
        None, "--output", "-o", help="Output CSV file path (required unless --label is used)"
    ),
    cache_file: Optional[Path] = typer.Option(
        None, "--cache", help="Cache file for PR list (used with date range)"
    ),
    prompt_file: Optional[Path] = typer.Option(
        None, "--prompt-file", "-p", help="Path to custom prompt file (default: embedded prompt)"
    ),
    provider: str = typer.Option(
        "auto", "--provider", help="LLM provider: gemini, openai, anthropic, or auto"
    ),
    model: str = typer.Option(DEFAULT_MODEL, "--model", "-m", help="Model name"),
    timeout: float = typer.Option(
        DEFAULT_TIMEOUT, "--timeout", "-t", help="Request timeout in seconds"
    ),
    max_tokens: int = typer.Option(
        DEFAULT_MAX_TOKENS, "--max-tokens", help="Maximum tokens for diff excerpt"
    ),
    hunks_per_file: int = typer.Option(
        DEFAULT_HUNKS_PER_FILE, "--hunks-per-file", help="Maximum hunks per file"
    ),
    sleep_seconds: float = typer.Option(
        DEFAULT_SLEEP_SECONDS, "--sleep-seconds", help="Sleep between GitHub API calls"
    ),
    resume: bool = typer.Option(
        True, "--resume/--no-resume", help="Resume from existing output file"
    ),
    workers: int = typer.Option(
        1, "--workers", "-w", help="Number of parallel workers (default: 1 = sequential)"
    ),
    label: bool = typer.Option(
        False, "--label", "-l", help="Label PRs with complexity instead of CSV output"
    ),
    label_prefix: str = typer.Option(
        "complexity:", "--label-prefix", help="Prefix for complexity labels (used with --label)"
    ),
    force: bool = typer.Option(
        False, "--force", "-f", help="Re-analyze PRs even if they already have a complexity label"
    ),
    state: str = typer.Option(
        "closed", "--state", help="PR state to search for: 'closed' or 'open' (used with --org)"
    ),
    since_minutes: Optional[int] = typer.Option(
        None,
        "--since-minutes",
        help="Look back N minutes from now (mutually exclusive with --since/--until)",
    ),
    api_base_url: Optional[str] = typer.Option(
        None, "--api-base-url", help="Base URL for OpenAI-compatible API endpoint"
    ),
    gemini_api_key: Optional[str] = typer.Option(
        None, "--gemini-api-key", help="Gemini / Google AI Studio API key"
    ),
    openai_api_key: Optional[str] = typer.Option(None, "--openai-api-key", help="OpenAI API key"),
    anthropic_api_key: Optional[str] = typer.Option(
        None, "--anthropic-api-key", help="Anthropic API key"
    ),
    github_tokens: Optional[str] = typer.Option(
        None,
        "--github-tokens",
        help="Comma-separated list of GitHub tokens for rotation on rate limits. "
        "Can also be set via GH_TOKENS or GITHUB_TOKENS environment variables.",
    ),
    ado_pat: Optional[str] = typer.Option(
        None, "--ado-pat", help="Azure DevOps personal access token (PAT)"
    ),
    project: Optional[str] = typer.Option(
        None, "--project", help="Azure DevOps project name (for date range enumeration)"
    ),
    source: str = typer.Option(
        "auto",
        "--source",
        help="PR source: github, azure-devops, or auto (auto-detected from URLs)",
    ),
    kimi_api_key: Optional[str] = typer.Option(
        None, "--kimi-api-key", help="Kimi / Moonshot AI API key"
    ),
):
    """
    Batch analyze multiple PRs from a file or date range.

    Either --input-file OR (--org, --since, --until) must be provided.

    By default, output is written to CSV with columns: pr_url, complexity, explanation.
    Use --label to apply complexity labels to PRs instead of CSV output.

    When using --label:
    - PRs that already have a complexity label are skipped
    - Labels are applied in the format "complexity:N" (customizable with --label-prefix)
    - No CSV output is generated unless --output is also specified

    If interrupted, run the same command again to resume from where it stopped.

    Use --workers to enable parallel processing (e.g., --workers 5 for 5 parallel workers).
    Note: Parallel processing may hit rate limits faster; adjust --sleep-seconds if needed.

    Token Rotation Mode:
    When multiple GitHub tokens are provided (via --github-tokens or GH_TOKENS env var),
    the tool automatically rotates between tokens when rate limits are hit. This allows
    for higher throughput when processing large batches of PRs.
    """
    try:
        # Validate inputs
        if input_file and (org or since or until or since_minutes):
            typer.echo("Error: Cannot specify both --input-file and date range options", err=True)
            raise typer.Exit(1)

        if since_minutes is not None and (since or until):
            typer.echo(
                "Error: --since-minutes is mutually exclusive with --since/--until", err=True
            )
            raise typer.Exit(1)

        if not input_file and not org and not project:
            typer.echo(
                "Error: Must specify either --input-file OR --org/--project (with date range options)",
                err=True,
            )
            raise typer.Exit(1)

        if not input_file and (org or project) and since_minutes is None and not (since and until):
            typer.echo(
                "Error: Must specify either --since-minutes OR (--since, --until) with --org/--project",
                err=True,
            )
            raise typer.Exit(1)

        # Require output_file unless --label is used
        if not label and not output_file:
            typer.echo("Error: --output is required unless --label is used", err=True)
            raise typer.Exit(1)

        # Resolve base URL (CLI option takes precedence over env)
        final_base_url = api_base_url or get_openai_base_url()

        # Pre-flight resolution and key validation
        resolve_provider_credentials(
            provider=provider,
            openai_api_key=openai_api_key,
            gemini_api_key=gemini_api_key,
            anthropic_api_key=anthropic_api_key,
            kimi_api_key=kimi_api_key,
            env_openai_api_key=get_openai_api_key(),
            env_gemini_api_key=get_gemini_api_key(),
            env_anthropic_api_key=get_anthropic_api_key(),
            env_kimi_api_key=get_kimi_api_key(),
        )

        # Get GitHub tokens - CLI option takes precedence over environment
        token_list: List[str] = []
        if github_tokens:
            # Parse comma-separated tokens from CLI
            token_list = [t.strip() for t in github_tokens.split(",") if t.strip()]
        else:
            # Fall back to environment variables
            token_list = get_github_tokens()

        # Azure DevOps PAT (CLI option takes precedence over environment)
        final_ado_pat = ado_pat or get_ado_pat()

        # Create TokenRotator if we have multiple tokens
        token_rotator: Optional[TokenRotator] = None
        github_token: Optional[str] = None

        if len(token_list) > 1:
            token_rotator = TokenRotator(token_list)
            github_token = token_list[0]  # Use first token for non-rotatable operations
            typer.echo(f"Token rotation enabled with {len(token_list)} tokens", err=True)
        elif len(token_list) == 1:
            github_token = token_list[0]
        else:
            github_token = None

        # A forge token is required for labeling
        if label and not github_token and not final_ado_pat:
            typer.echo("Error: A forge token is required for labeling PRs", err=True)
            typer.echo(
                "For GitHub: export GH_TOKEN='your-token' or export GITHUB_TOKEN='your-token'",
                err=True,
            )
            typer.echo(
                "For Azure DevOps: export AZURE_DEVOPS_PAT='your-pat' or pass --ado-pat", err=True
            )
            raise typer.Exit(1)

        # Warn if GitHub token is missing (needed for private repos)
        if not github_token and not project:
            typer.echo(
                "Warning: GH_TOKEN or GITHUB_TOKEN not set. Private repos may fail with 404 errors.",
                err=True,
            )
            typer.echo(
                "Set it with: export GH_TOKEN='your-token' or export GITHUB_TOKEN='your-token'",
                err=True,
            )

        # Load prompt
        try:
            prompt_text = load_prompt(prompt_file)
        except FileNotFoundError as e:
            typer.echo(f"Error: {e}", err=True)
            raise typer.Exit(1)

        # Get PR URLs
        if input_file:
            typer.echo(f"Loading PR URLs from file: {input_file}", err=True)
            pr_urls = load_pr_urls_from_file(input_file)
        else:
            # Compute dates from --since-minutes or --since/--until
            if since_minutes is not None:
                now = datetime.now(timezone.utc)
                until_dt = now
                since_dt = now - timedelta(minutes=since_minutes)
            else:
                # Parse dates
                try:
                    since_dt = datetime.strptime(since, "%Y-%m-%d")
                    until_dt = datetime.strptime(until, "%Y-%m-%d")
                except ValueError as e:
                    typer.echo(f"Error: Invalid date format. Use YYYY-MM-DD: {e}", err=True)
                    raise typer.Exit(1)

                if since_dt > until_dt:
                    typer.echo("Error: --since date must be before --until date", err=True)
                    raise typer.Exit(1)

            if project:
                # Azure DevOps date range enumeration (needs server/collection from env)
                ado_server = get_ado_server_url()
                ado_collection = get_ado_collection()
                if not ado_server:
                    typer.echo(
                        "Error: AZURE_DEVOPS_SERVER_URL is required for Azure DevOps enumeration",
                        err=True,
                    )
                    typer.echo(
                        "Example: export AZURE_DEVOPS_SERVER_URL='https://tfs.company.local:8080/tfs'",
                        err=True,
                    )
                    raise typer.Exit(1)
                if not ado_collection:
                    typer.echo(
                        "Error: AZURE_DEVOPS_COLLECTION is required for Azure DevOps enumeration",
                        err=True,
                    )
                    typer.echo(
                        "Example: export AZURE_DEVOPS_COLLECTION='DefaultCollection'", err=True
                    )
                    raise typer.Exit(1)
                if not final_ado_pat:
                    typer.echo(
                        "Error: Azure DevOps PAT is required for date range enumeration", err=True
                    )
                    typer.echo(
                        "Set it with: export AZURE_DEVOPS_PAT='your-pat' or pass --ado-pat",
                        err=True,
                    )
                    raise typer.Exit(1)
                typer.echo(
                    f"Fetching {state} PRs for project '{project}' from "
                    f"{since_dt.date()} to {until_dt.date()}...",
                    err=True,
                )
                ado_state = "closed" if state == "closed" else "all"
                pr_urls = ado.search_prs(
                    server=ado_server.rstrip("/"),
                    collection=ado_collection,
                    project=project,
                    since=since_dt,
                    until=until_dt,
                    pat=final_ado_pat,
                    sleep_s=sleep_seconds,
                    state=ado_state,
                )
                typer.echo(f"Found {len(pr_urls)} PRs", err=True)
            else:
                pr_urls = generate_pr_list_from_date_range(
                    org=org,
                    since=since_dt,
                    until=until_dt,
                    cache_file=cache_file,
                    github_token=github_token,
                    sleep_seconds=sleep_seconds,
                    state=state,
                )

        # Create analyzer function with progress callback
        def progress_msg(msg: str) -> None:
            """Display progress messages."""
            typer.echo(msg, err=True)

        def analyze_fn(pr_url: str) -> dict:
            """Wrapper for analyze_pr_to_dict that handles errors."""
            return analyze_pr_to_dict(
                pr_url=pr_url,
                prompt_text=prompt_text,
                github_token=github_token,
                openai_key=openai_api_key,
                gemini_key=gemini_api_key,
                anthropic_key=anthropic_api_key,
                kimi_key=kimi_api_key,
                ado_pat=final_ado_pat,
                source=source,
                provider=provider,
                model=model,
                timeout=timeout,
                max_tokens=max_tokens,
                hunks_per_file=hunks_per_file,
                sleep_seconds=sleep_seconds,
                progress_callback=progress_msg,
                token_rotator=token_rotator,
                base_url=final_base_url,
            )

        # Validate workers
        if workers < 1:
            typer.echo("Error: --workers must be >= 1", err=True)
            raise typer.Exit(1)

        # Run batch analysis with labeling support
        run_batch_analysis_with_labels(
            pr_urls=pr_urls,
            output_file=output_file,
            analyze_fn=analyze_fn,
            resume=resume,
            workers=workers,
            label_prs=label,
            label_prefix=label_prefix,
            github_token=github_token,
            ado_pat=final_ado_pat,
            timeout=timeout,
            force=force,
        )

    except KeyboardInterrupt:
        typer.echo("\nInterrupted by user", err=True)
        raise typer.Exit(130)
    except typer.Exit:
        raise
    except (ValueError, LLMError, RuntimeError) as e:
        typer.echo(f"Error: {e}", err=True)
        raise typer.Exit(1)
    except Exception as e:
        ErrorHandler.handle_unexpected_error(e, debug=bool(os.getenv("DEBUG")))
        raise typer.Exit(1)


@app.command(name="rate-limit")
def rate_limit(
    format: str = typer.Option("json", "--format", "-f", help="Output format: json or human"),
):
    """
    Check GitHub API rate limit status.

    Shows the current rate limit status for both core API and search API endpoints.
    """
    try:
        github_token = get_github_token()

        rate_limit_info = check_rate_limit(token=github_token)

        if format == "human":
            core = rate_limit_info["core"]
            search = rate_limit_info["search"]

            # Format reset time
            from datetime import datetime

            core_reset = datetime.fromtimestamp(core["reset"]) if core["reset"] else None
            search_reset = datetime.fromtimestamp(search["reset"]) if search["reset"] else None

            typer.echo("GitHub API Rate Limits:", err=False)
            typer.echo("", err=False)
            typer.echo("Core API:", err=False)
            typer.echo(f"  Limit: {core['limit']}", err=False)
            typer.echo(f"  Remaining: {core['remaining']}", err=False)
            typer.echo(f"  Used: {core['used']}", err=False)
            if core_reset:
                typer.echo(
                    f"  Resets at: {core_reset.strftime('%Y-%m-%d %H:%M:%S UTC')}", err=False
                )

            typer.echo("", err=False)
            typer.echo("Search API:", err=False)
            typer.echo(f"  Limit: {search['limit']}", err=False)
            typer.echo(f"  Remaining: {search['remaining']}", err=False)
            typer.echo(f"  Used: {search['used']}", err=False)
            if search_reset:
                typer.echo(
                    f"  Resets at: {search_reset.strftime('%Y-%m-%d %H:%M:%S UTC')}", err=False
                )
        else:
            # JSON output
            json_output = json.dumps(rate_limit_info, indent=2)
            typer.echo(json_output)

    except GitHubAPIError as e:
        ErrorHandler.handle_github_error(e)
        raise typer.Exit(1)
    except Exception as e:
        ErrorHandler.handle_unexpected_error(e, debug=bool(os.getenv("DEBUG")))
        raise typer.Exit(1)


@app.command(name="label-pr")
def label_pr(
    pr_url: Optional[str] = typer.Argument(
        None,
        help="GitHub or Azure DevOps PR URL. If not provided, will try to infer from GitHub Actions context.",
    ),
    prompt_file: Optional[Path] = typer.Option(
        None, "--prompt-file", "-p", help="Path to custom prompt file (default: embedded prompt)"
    ),
    provider: str = typer.Option(
        "auto",
        "--provider",
        help="LLM provider: gemini, openai, anthropic, kimi, or auto",
    ),
    model: str = typer.Option(DEFAULT_MODEL, "--model", "-m", help="Model name"),
    timeout: float = typer.Option(
        DEFAULT_TIMEOUT, "--timeout", "-t", help="Request timeout in seconds"
    ),
    max_tokens: int = typer.Option(
        DEFAULT_MAX_TOKENS, "--max-tokens", help="Maximum tokens for diff excerpt"
    ),
    hunks_per_file: int = typer.Option(
        DEFAULT_HUNKS_PER_FILE, "--hunks-per-file", help="Maximum hunks per file"
    ),
    sleep_seconds: float = typer.Option(
        DEFAULT_SLEEP_SECONDS, "--sleep-seconds", help="Sleep between forge API calls"
    ),
    label_prefix: str = typer.Option(
        "complexity:", "--label-prefix", help="Prefix for the complexity label"
    ),
    dry_run: bool = typer.Option(False, "--dry-run", help="Analyze but don't update label"),
    gemini_api_key: Optional[str] = typer.Option(
        None, "--gemini-api-key", help="Gemini / Google AI Studio API key"
    ),
    openai_api_key: Optional[str] = typer.Option(None, "--openai-api-key", help="OpenAI API key"),
    anthropic_api_key: Optional[str] = typer.Option(
        None, "--anthropic-api-key", help="Anthropic API key"
    ),
    kimi_api_key: Optional[str] = typer.Option(
        None, "--kimi-api-key", help="Kimi / Moonshot AI API key"
    ),
    github_token: Optional[str] = typer.Option(None, "--github-token", help="GitHub token"),
    ado_pat: Optional[str] = typer.Option(
        None, "--ado-pat", help="Azure DevOps personal access token (PAT)"
    ),
    source: str = typer.Option(
        "auto",
        "--source",
        help="PR source: github, azure-devops, or auto (auto-detected from URL)",
    ),
    api_base_url: Optional[str] = typer.Option(
        None, "--api-base-url", help="Base URL for OpenAI-compatible API endpoint"
    ),
):
    """
    Analyze a pull request and update its complexity label.

    Computes the complexity score and sets a label like "complexity:7" on the PR.
    Removes any existing complexity labels before adding the new one.
    Works with both GitHub and Azure DevOps pull request URLs.

    Environment variables:
    - GH_TOKEN or GITHUB_TOKEN: GitHub API token (required for GitHub label updates)
    - AZURE_DEVOPS_PAT: Azure DevOps PAT (required for Azure DevOps label updates)
    - OPENAI_API_KEY: OpenAI API key
    - GEMINI_API_KEY or GOOGLE_API_KEY: Gemini API key
    - ANTHROPIC_API_KEY: Anthropic API key
    - KIMI_API_KEY or MOONSHOT_API_KEY: Kimi / Moonshot AI API key
    """
    try:
        # Get PR URL
        final_pr_url = pr_url
        if not final_pr_url:
            typer.echo(
                "PR URL not provided, attempting to infer from GitHub Actions context...", err=True
            )
            final_pr_url = get_pr_url_from_context()
            if not final_pr_url:
                typer.echo("Error: Could not infer PR URL from context.", err=True)
                typer.echo("Please provide the PR URL as an argument.", err=True)
                raise typer.Exit(1)
            typer.echo(f"Inferred PR URL: {final_pr_url}", err=True)

        # Resolve PR reference (GitHub or Azure DevOps) and credentials
        ref = resolve_pr_ref(final_pr_url, source=source, ado_pat=ado_pat)
        final_github_token = github_token or get_github_token()

        if ref.source == "azure-devops" and not ref.ado_pat:
            typer.echo("Error: Azure DevOps PAT is required to update labels", err=True)
            typer.echo(
                "Set it with: export AZURE_DEVOPS_PAT='your-pat' or pass --ado-pat", err=True
            )
            raise typer.Exit(1)

        # Pre-flight resolution and key validation
        resolve_provider_credentials(
            provider=provider,
            openai_api_key=openai_api_key,
            gemini_api_key=gemini_api_key,
            anthropic_api_key=anthropic_api_key,
            kimi_api_key=kimi_api_key,
            env_openai_api_key=get_openai_api_key(),
            env_gemini_api_key=get_gemini_api_key(),
            env_anthropic_api_key=get_anthropic_api_key(),
            env_kimi_api_key=get_kimi_api_key(),
        )

        if ref.source == "github" and not final_github_token:
            typer.echo("Error: GitHub token is required to update labels", err=True)
            typer.echo("Set it with: export GH_TOKEN='your-token' or pass --github-token", err=True)
            raise typer.Exit(1)

        # Load prompt
        try:
            prompt_text = load_prompt(prompt_file)
        except FileNotFoundError as e:
            typer.echo(f"Error: {e}", err=True)
            raise typer.Exit(1)

        # Analyze PR
        typer.echo(f"Analyzing PR {ref.repo_slug}#{ref.pr}...", err=True)
        try:
            output = analyze_pr_to_dict(
                pr_url=final_pr_url,
                prompt_text=prompt_text,
                github_token=final_github_token,
                openai_key=openai_api_key,
                gemini_key=gemini_api_key,
                anthropic_key=anthropic_api_key,
                kimi_key=kimi_api_key,
                ado_pat=ref.ado_pat,
                source=source,
                provider=provider,
                model=model,
                timeout=timeout,
                max_tokens=max_tokens,
                hunks_per_file=hunks_per_file,
                sleep_seconds=sleep_seconds,
                base_url=api_base_url or get_openai_base_url(),
            )

        except GitHubAPIError as e:
            if e.status_code == 404:
                ErrorHandler.handle_github_404(
                    ref.owner or ref.project or "?",
                    ref.repo or "?",
                    ref.pr or 0,
                    bool(final_github_token or ref.ado_pat),
                )
            else:
                ErrorHandler.handle_github_error(e)
            raise typer.Exit(1)
        except LLMError as e:
            ErrorHandler.handle_llm_error(e)
            raise typer.Exit(1)
        except InvalidResponseError as e:
            typer.echo(f"Invalid LLM response: {e}", err=True)
            raise typer.Exit(1)
        except (ValueError, RuntimeError) as e:
            typer.echo(f"Error: {e}", err=True)
            logger.debug("Analysis error", exc_info=True)
            raise typer.Exit(1)

        complexity = output["score"]
        typer.echo(f"Complexity score: {complexity}/10", err=True)
        typer.echo(f"Explanation: {output['explanation']}", err=True)

        # Update label
        if dry_run:
            label_name = f"{label_prefix}{complexity}"
            typer.echo(f"Dry run: Would set label '{label_name}'", err=True)
        else:
            typer.echo("Updating PR label...", err=True)
            try:
                label_name = sources.update_complexity_label(
                    ref,
                    complexity=complexity,
                    github_token=final_github_token,
                    label_prefix=label_prefix,
                    timeout=timeout,
                )
                typer.echo(f"Label set: {label_name}", err=True)
            except GitHubAPIError as e:
                typer.echo(f"Failed to update label: {e}", err=True)
                raise typer.Exit(1)

        # Output result as JSON
        result = {
            "score": complexity,
            "explanation": output["explanation"],
            "label": f"{label_prefix}{complexity}",
            "pr": ref.pr,
            "repo": ref.repo_slug,
            "url": final_pr_url,
            "dry_run": dry_run,
        }
        typer.echo(json.dumps(result, ensure_ascii=False, indent=2))

        # Set GitHub Action outputs if available
        if "GITHUB_OUTPUT" in os.environ:
            with open(os.environ["GITHUB_OUTPUT"], "a") as f:
                f.write(f"score={complexity}\n")
                f.write(f"label={label_prefix}{complexity}\n")

                # Explanation (handle multiline)
                explanation = output["explanation"]
                if "\n" in explanation:
                    delimiter = "EOF"
                    f.write(f"explanation<<{delimiter}\n{explanation}\n{delimiter}\n")
                else:
                    f.write(f"explanation={explanation}\n")

    except KeyboardInterrupt:
        typer.echo("\nInterrupted by user", err=True)
        raise typer.Exit(130)
    except typer.Exit:
        raise
    except (ValueError, LLMError, RuntimeError) as e:
        typer.echo(f"Error: {e}", err=True)
        raise typer.Exit(1)
    except Exception as e:
        ErrorHandler.handle_unexpected_error(e, debug=bool(os.getenv("DEBUG")))
        raise typer.Exit(1)


@app.callback(invoke_without_command=True, no_args_is_help=False)
def main(ctx: typer.Context):
    """
    Analyze a GitHub PR and compute complexity score.

    Environment variables:
    - GH_TOKEN or GITHUB_TOKEN: GitHub API token (optional for public repos)
    - OPENAI_API_KEY: OpenAI API key (required)
    """
    # If a subcommand was invoked, let it handle things
    if ctx.invoked_subcommand is not None:
        return

    # Otherwise, handle direct URL invocation
    # Get the first argument from ctx.args
    if not ctx.args or len(ctx.args) == 0:
        typer.echo("Error: PR URL is required", err=True)
        typer.echo("Usage: complexity-cli analyze-pr <PR_URL>", err=True)
        typer.echo("   or: complexity-cli <PR_URL>", err=True)
        raise typer.Exit(1)

    # Check if first arg looks like a supported PR URL
    first_arg = ctx.args[0]
    if not _is_supported_pr_url(first_arg):
        typer.echo(f"Error: Invalid PR URL: {first_arg}", err=True)
        typer.echo("Usage: complexity-cli analyze-pr <PR_URL>", err=True)
        typer.echo("   or: complexity-cli <PR_URL>", err=True)
        raise typer.Exit(1)

    # For direct invocation, use defaults (options require analyze-pr subcommand)
    typer.echo(
        "Note: For options like --model, --format, etc., use 'complexity-cli analyze-pr <URL> [OPTIONS]'",
        err=True,
    )
    _analyze_pr_impl(
        pr_url=first_arg,
        prompt_file=None,
        model=DEFAULT_MODEL,
        format="json",
        output_file=None,
        timeout=DEFAULT_TIMEOUT,
        max_tokens=DEFAULT_MAX_TOKENS,
        hunks_per_file=DEFAULT_HUNKS_PER_FILE,
        sleep_seconds=DEFAULT_SLEEP_SECONDS,
        dry_run=False,
        base_url=get_openai_base_url(),
    )


if __name__ == "__main__":
    app()
