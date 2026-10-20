"""Dispatch PR operations to the correct forge (GitHub or Azure DevOps).

This module is the single integration point between the forge-agnostic
analysis pipeline (``cli.preprocess``/``cli.analyze``) and the forge-specific
clients (``cli.github``, ``cli.ado``). Callers resolve a PR URL into a
:class:`PrRef` and then use the dispatch functions below, which keep the
existing GitHub behaviour unchanged while adding Azure DevOps Server support.
"""

from dataclasses import dataclass
from typing import Any, Callable, Dict, List, Optional, Tuple

from . import ado, github
from .config import get_ado_pat
from .utils import detect_pr_source, parse_ado_pr_url, parse_pr_url


@dataclass(frozen=True)
class PrRef:
    """Resolved reference to a pull request on either supported forge."""

    source: str  # "github" | "azure-devops"
    url: str
    # GitHub fields
    owner: Optional[str] = None
    # Shared fields
    repo: Optional[str] = None
    pr: Optional[int] = None
    # Azure DevOps fields
    server: Optional[str] = None
    collection: Optional[str] = None
    project: Optional[str] = None
    ado_pat: Optional[str] = None

    @property
    def repo_slug(self) -> str:
        """Human-readable repository identifier used in output."""
        if self.source == "azure-devops":
            return f"{self.project}/{self.repo}"
        return f"{self.owner}/{self.repo}"


def resolve_pr_ref(
    pr_url: str,
    source: str = "auto",
    ado_pat: Optional[str] = None,
) -> PrRef:
    """
    Parse a PR URL and resolve credentials for its forge.

    Args:
        pr_url: GitHub or Azure DevOps PR URL
        source: Force a forge ("auto", "github", "azure-devops")
        ado_pat: Azure DevOps PAT (falls back to AZURE_DEVOPS_PAT env var)

    Returns:
        Populated :class:`PrRef`
    """
    detected = detect_pr_source(pr_url, source=source)
    if detected == "azure-devops":
        server, collection, project, repo, pr = parse_ado_pr_url(pr_url)
        return PrRef(
            source="azure-devops",
            url=pr_url,
            server=server,
            collection=collection,
            project=project,
            repo=repo,
            pr=pr,
            ado_pat=ado_pat or get_ado_pat(),
        )
    owner, repo, pr = parse_pr_url(pr_url)
    return PrRef(source="github", url=pr_url, owner=owner, repo=repo, pr=pr)


def fetch_pr(
    ref: PrRef,
    github_token: Optional[str] = None,
    sleep_s: float = 0.7,
    progress_callback: Optional[Callable[[str], None]] = None,
    timeout: float = 120.0,
) -> Tuple[str, Dict[str, Any]]:
    """
    Fetch diff text and metadata for a PR on either forge.

    Returns the same ``(diff_text, metadata)`` tuple for both forges; the
    Azure DevOps metadata is normalized to the GitHub-like shape.
    """
    if ref.source == "azure-devops":
        if not ref.ado_pat:
            raise ValueError(
                "Azure DevOps PAT is required for Azure DevOps PRs. "
                "Set AZURE_DEVOPS_PAT or pass --ado-pat."
            )
        return ado.fetch_pr(
            ref.server,
            ref.collection,
            ref.project,
            ref.repo,
            ref.pr,
            ref.ado_pat,
            sleep_s=sleep_s,
            timeout=timeout,
            progress_callback=progress_callback,
        )
    return github.fetch_pr(
        ref.owner,
        ref.repo,
        ref.pr,
        github_token,
        sleep_s=sleep_s,
        progress_callback=progress_callback,
    )


def get_pr_labels(ref: PrRef, github_token: Optional[str] = None) -> List[str]:
    """Return the labels currently attached to a PR."""
    if ref.source == "azure-devops":
        return ado.get_pr_labels(
            ref.server, ref.collection, ref.project, ref.repo, ref.pr, ref.ado_pat
        )
    return github.get_pr_labels(ref.owner, ref.repo, ref.pr, github_token)


def update_complexity_label(
    ref: PrRef,
    complexity: int,
    github_token: Optional[str] = None,
    label_prefix: str = "complexity:",
    timeout: float = 120.0,
) -> str:
    """Replace existing complexity labels on a PR with the new score."""
    if ref.source == "azure-devops":
        return ado.update_complexity_label(
            ref.server,
            ref.collection,
            ref.project,
            ref.repo,
            ref.pr,
            complexity,
            ref.ado_pat,
            label_prefix=label_prefix,
            timeout=timeout,
        )
    return github.update_complexity_label(
        ref.owner,
        ref.repo,
        ref.pr,
        complexity,
        token=github_token,
        label_prefix=label_prefix,
        timeout=timeout,
    )


def has_complexity_label(
    ref: PrRef,
    github_token: Optional[str] = None,
    label_prefix: str = "complexity:",
    timeout: float = 120.0,
) -> Optional[str]:
    """Return the existing complexity label name if present, else None."""
    if ref.source == "azure-devops":
        return ado.has_complexity_label(
            ref.server,
            ref.collection,
            ref.project,
            ref.repo,
            ref.pr,
            ref.ado_pat,
            label_prefix=label_prefix,
            timeout=timeout,
        )
    return github.has_complexity_label(
        ref.owner,
        ref.repo,
        ref.pr,
        github_token,
        label_prefix=label_prefix,
        timeout=timeout,
    )
