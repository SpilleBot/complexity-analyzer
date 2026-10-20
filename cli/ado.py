"""Azure DevOps Server API client for fetching PR diffs and metadata.

Uses the Azure DevOps Server REST API with a personal access token (PAT)
authenticated via HTTP Basic auth (base64 of ":<PAT>").

Unlike GitHub, Azure DevOps has no single endpoint that returns a ready-made
unified diff for a whole pull request. ``fetch_pr`` therefore assembles the
diff itself: it lists the changed files of the latest PR iteration, downloads
the old and new content of each file via the Git Items API, and builds a
unified diff locally with :mod:`difflib`. The result is shaped exactly like
the GitHub metadata/diff consumed by ``cli.preprocess`` and ``cli.analyze``.
"""

import base64
import difflib
import time
from datetime import datetime
from typing import Any, Callable, Dict, List, Optional, Tuple

import httpx

from .constants import ADO_API_VERSION, DEFAULT_SLEEP_SECONDS, DEFAULT_TIMEOUT
from .github import GitHubAPIError
from .utils import parse_ado_pr_url

# Maximum file size (bytes) to include in a locally assembled diff.
MAX_FILE_BYTES = 1024 * 1024


class AzureDevOpsAPIError(GitHubAPIError):
    """Azure DevOps API error.

    Subclasses :class:`GitHubAPIError` so that all existing CLI error handling
    (404 hints, batch skip logic, etc.) works unchanged for both forges.
    """

    def __init__(self, status_code: int, message: str, url: str):
        super().__init__(status_code, message, url)
        self.status_code = status_code
        self.message = message
        self.url = url


def build_ado_headers(pat: str) -> Dict[str, str]:
    """
    Build headers for Azure DevOps API requests.

    Azure DevOps authenticates PATs with HTTP Basic auth: the username is
    empty and the password is the PAT, i.e. base64(":<PAT>").
    """
    if not pat:
        raise ValueError("Azure DevOps PAT is required")
    token = base64.b64encode(f":{pat}".encode("utf-8")).decode("ascii")
    return {
        "Authorization": f"Basic {token}",
        "Accept": "application/json",
    }


def _api_url(server: str, collection: str, project: str, path: str) -> str:
    """Build an Azure DevOps REST API URL."""
    return f"{server}/{collection}/{project}/_apis/{path}" f"?api-version={ADO_API_VERSION}"


def parse_ado_pr(url: str) -> Tuple[str, str, str, str, int]:
    """Parse an Azure DevOps PR URL (delegates to :func:`cli.utils.parse_ado_pr_url`)."""
    return parse_ado_pr_url(url)


def fetch_pr_metadata(
    server: str,
    collection: str,
    project: str,
    repo: str,
    pr: int,
    pat: str,
    timeout: float = DEFAULT_TIMEOUT,
) -> Dict[str, Any]:
    """
    Fetch pull request metadata from Azure DevOps.

    Endpoint: ``GET .../git/repositories/{repo}/pullRequests/{pr}``
    """
    url = _api_url(server, collection, project, f"git/repositories/{repo}/pullRequests/{pr}")
    try:
        with httpx.Client(timeout=timeout) as client:
            response = client.get(url, headers=build_ado_headers(pat))
            response.raise_for_status()
            return response.json()
    except httpx.HTTPStatusError as e:
        raise AzureDevOpsAPIError(e.response.status_code, e.response.text[:500], url)
    except httpx.RequestError as e:
        raise RuntimeError(f"Failed to fetch PR metadata: {e}")


def _fetch_json(
    client: httpx.Client,
    url: str,
    pat: str,
    params: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """GET a JSON document from the Azure DevOps API."""
    response = client.get(url, headers=build_ado_headers(pat), params=params)
    response.raise_for_status()
    return response.json()


def fetch_item_content(
    client: httpx.Client,
    server: str,
    collection: str,
    project: str,
    repo: str,
    path: str,
    version: str,
    pat: str,
    timeout: float = DEFAULT_TIMEOUT,
) -> Optional[str]:
    """
    Fetch a file's content at a specific commit version.

    Returns the decoded text, or ``None`` when the file is binary, too large,
    or unavailable (missing at that version, etc.).
    """
    url = _api_url(server, collection, project, f"git/repositories/{repo}/items")
    params = {
        "path": path,
        "versionDescriptor.version": version,
        "versionDescriptor.versionType": "commit",
        "includeContent": "true",
    }
    try:
        response = client.get(url, headers=build_ado_headers(pat), params=params)
        if response.status_code == 404:
            return None
        response.raise_for_status()
    except httpx.RequestError:
        return None

    content = response.content
    if not content:
        return ""
    if len(content) > MAX_FILE_BYTES:
        return None
    if b"\x00" in content[:8192]:
        return None
    try:
        return content.decode("utf-8")
    except UnicodeDecodeError:
        return None


def build_file_diff(
    path: str,
    old_content: Optional[str],
    new_content: Optional[str],
) -> Optional[str]:
    """
    Build a GitHub-style unified diff for a single file.

    Args:
        path: Repository-relative file path
        old_content: Content before the change (None/"" for added files)
        new_content: Content after the change (None/"" for deleted files)

    Returns:
        Diff text starting with a ``diff --git`` header, or None if unchanged
        or if both sides are unavailable (e.g. binary on both sides).
    """
    old = old_content if old_content is not None else ""
    new = new_content if new_content is not None else ""

    if old == new:
        return None
    if (old_content is None and new_content is None) or (not old and not new):
        return None

    rel = path.lstrip("/")
    diff_lines = difflib.unified_diff(
        old.splitlines(),
        new.splitlines(),
        fromfile=f"a/{rel}",
        tofile=f"b/{rel}",
        lineterm="",
    )
    body = list(diff_lines)
    if not body:
        return None

    # difflib emits "--- a/path" / "+++ b/path" plus @@ hunks; prepend the
    # "diff --git" header expected by preprocess.parse_diff_sections().
    return "\n".join([f"diff --git a/{rel} b/{rel}", *body])


def _count_diff_stats(diff_text: str) -> Tuple[int, int]:
    """Count additions/deletions in a single-file unified diff body."""
    additions = 0
    deletions = 0
    for line in diff_text.splitlines():
        if line.startswith("+++") or line.startswith("---"):
            continue
        if line.startswith("+"):
            additions += 1
        elif line.startswith("-"):
            deletions += 1
    return additions, deletions


def fetch_pr(
    server: str,
    collection: str,
    project: str,
    repo: str,
    pr: int,
    pat: str,
    sleep_s: float = DEFAULT_SLEEP_SECONDS,
    timeout: float = DEFAULT_TIMEOUT,
    progress_callback: Optional[Callable[[str], None]] = None,
) -> Tuple[str, Dict[str, Any]]:
    """
    Fetch a pull request's diff and metadata from Azure DevOps.

    Assembles a unified diff from the latest iteration's change list and the
    Git Items API, then normalizes everything into the metadata shape that
    ``cli.preprocess``/``cli.analyze`` expect from GitHub.

    Args:
        server: Server base URL, e.g. "https://tfs.company.local:8080/tfs"
        collection: Collection name
        project: Project name
        repo: Repository name
        pr: Pull request id
        pat: Personal access token
        sleep_s: Sleep between API calls
        timeout: Request timeout in seconds
        progress_callback: Optional callback for progress messages

    Returns:
        Tuple of (diff_text, metadata_dict)
    """
    if not pat:
        raise ValueError("Azure DevOps PAT is required. Set AZURE_DEVOPS_PAT or pass --ado-pat.")

    pr_data = fetch_pr_metadata(server, collection, project, repo, pr, pat, timeout)

    created_by = pr_data.get("createdBy") or {}
    author_login = created_by.get("uniqueName") or created_by.get("displayName") or "unknown"

    source_commit = (pr_data.get("lastMergeSourceCommit") or {}).get("commitId")
    target_commit = (pr_data.get("lastMergeTargetCommit") or {}).get("commitId")

    diff_parts: List[str] = []
    files_meta: List[Dict[str, Any]] = []
    total_additions = 0
    total_deletions = 0

    try:
        with httpx.Client(timeout=timeout) as client:
            # Latest iteration -> list of changed files
            iterations_url = _api_url(
                server,
                collection,
                project,
                f"git/repositories/{repo}/pullRequests/{pr}/iterations",
            )
            iterations = _fetch_json(client, iterations_url, pat)
            iteration_list = iterations.get("value") or []
            if not iteration_list:
                raise AzureDevOpsAPIError(
                    404,
                    f"Pull request {pr} has no iterations",
                    iterations_url,
                )
            iteration_id = max(it.get("id", 0) for it in iteration_list)

            changes_url = _api_url(
                server,
                collection,
                project,
                f"git/repositories/{repo}/pullRequests/{pr}/iterations/{iteration_id}/changes",
            )
            changes = _fetch_json(client, changes_url, pat)
            change_entries = changes.get("changeEntries") or []

            for entry in change_entries:
                item = entry.get("item") or {}
                path = item.get("path") or ""
                if not path:
                    continue
                change_type = (entry.get("changeType") or "edit").lower()

                old_content: Optional[str] = None
                new_content: Optional[str] = None

                if change_type == "add":
                    if source_commit:
                        new_content = fetch_item_content(
                            client,
                            server,
                            collection,
                            project,
                            repo,
                            path,
                            source_commit,
                            pat,
                            timeout,
                        )
                elif change_type == "delete":
                    if target_commit:
                        old_content = fetch_item_content(
                            client,
                            server,
                            collection,
                            project,
                            repo,
                            path,
                            target_commit,
                            pat,
                            timeout,
                        )
                else:  # edit, rename, etc.
                    if source_commit:
                        new_content = fetch_item_content(
                            client,
                            server,
                            collection,
                            project,
                            repo,
                            path,
                            source_commit,
                            pat,
                            timeout,
                        )
                    if target_commit:
                        old_content = fetch_item_content(
                            client,
                            server,
                            collection,
                            project,
                            repo,
                            path,
                            target_commit,
                            pat,
                            timeout,
                        )

                if change_type != "delete" and new_content is None and change_type != "add":
                    # Binary or unavailable on the new side — nothing reviewable.
                    continue
                if change_type == "delete" and old_content is None:
                    continue

                file_diff = build_file_diff(path, old_content, new_content)
                if not file_diff:
                    continue

                additions, deletions = _count_diff_stats(file_diff)
                total_additions += additions
                total_deletions += deletions
                files_meta.append(
                    {
                        "filename": path.lstrip("/"),
                        "status": change_type,
                        "patch": file_diff,
                        "additions": additions,
                        "deletions": deletions,
                    }
                )
                diff_parts.append(file_diff)
                time.sleep(max(0.0, sleep_s / 4))
    except httpx.HTTPStatusError as e:
        raise AzureDevOpsAPIError(e.response.status_code, e.response.text[:500], str(e.request.url))
    except httpx.RequestError as e:
        raise RuntimeError(f"Failed to fetch PR diff: {e}")

    diff_text = "\n".join(diff_parts)

    # Normalize to the GitHub-like metadata shape consumed downstream.
    meta: Dict[str, Any] = {
        "title": pr_data.get("title") or "",
        "body": pr_data.get("description") or "",
        "user": {"login": author_login},
        "additions": total_additions,
        "deletions": total_deletions,
        "changed_files": len(files_meta),
        "files": files_meta,
        "_ado": {
            "server": server,
            "collection": collection,
            "project": project,
            "repository": repo,
            "pullRequestId": pr,
            "status": pr_data.get("status"),
        },
    }
    return diff_text, meta


def get_pr_labels(
    server: str,
    collection: str,
    project: str,
    repo: str,
    pr: int,
    pat: str,
    timeout: float = DEFAULT_TIMEOUT,
) -> List[str]:
    """Get all labels on a pull request."""
    url = _api_url(server, collection, project, f"git/repositories/{repo}/pullRequests/{pr}/labels")
    try:
        with httpx.Client(timeout=timeout) as client:
            response = client.get(url, headers=build_ado_headers(pat))
            response.raise_for_status()
            labels = response.json().get("value") or []
            return [label.get("name", "") for label in labels if label.get("name")]
    except httpx.HTTPStatusError as e:
        raise AzureDevOpsAPIError(e.response.status_code, e.response.text[:500], url)
    except httpx.RequestError as e:
        raise RuntimeError(f"Failed to get PR labels: {e}")


def add_pr_label(
    server: str,
    collection: str,
    project: str,
    repo: str,
    pr: int,
    label: str,
    pat: str,
    timeout: float = DEFAULT_TIMEOUT,
) -> None:
    """Add a label to a pull request."""
    url = _api_url(server, collection, project, f"git/repositories/{repo}/pullRequests/{pr}/labels")
    try:
        with httpx.Client(timeout=timeout) as client:
            response = client.post(url, headers=build_ado_headers(pat), json={"name": label})
            if response.status_code == 400:
                # Label already assigned — treat as success.
                return
            response.raise_for_status()
    except httpx.HTTPStatusError as e:
        raise AzureDevOpsAPIError(e.response.status_code, e.response.text[:500], url)
    except httpx.RequestError as e:
        raise RuntimeError(f"Failed to add PR label: {e}")


def remove_pr_label(
    server: str,
    collection: str,
    project: str,
    repo: str,
    pr: int,
    label: str,
    pat: str,
    timeout: float = DEFAULT_TIMEOUT,
) -> None:
    """Remove a label from a pull request."""
    import urllib.parse

    encoded_label = urllib.parse.quote(label, safe="")
    url = _api_url(
        server,
        collection,
        project,
        f"git/repositories/{repo}/pullRequests/{pr}/labels/{encoded_label}",
    )
    try:
        with httpx.Client(timeout=timeout) as client:
            response = client.delete(url, headers=build_ado_headers(pat))
            if response.status_code == 404:
                return  # Label doesn't exist — fine
            response.raise_for_status()
    except httpx.HTTPStatusError as e:
        if e.response.status_code == 404:
            return
        raise AzureDevOpsAPIError(e.response.status_code, e.response.text[:500], url)
    except httpx.RequestError as e:
        raise RuntimeError(f"Failed to remove PR label: {e}")


def update_complexity_label(
    server: str,
    collection: str,
    project: str,
    repo: str,
    pr: int,
    complexity: int,
    pat: str,
    label_prefix: str = "complexity:",
    timeout: float = DEFAULT_TIMEOUT,
) -> str:
    """
    Replace any existing complexity labels on a PR with the new score.

    Returns the label name that was applied.
    """
    if not 1 <= complexity <= 10:
        raise ValueError(f"Complexity must be between 1 and 10, got: {complexity}")

    for label in get_pr_labels(server, collection, project, repo, pr, pat, timeout):
        if label.startswith(label_prefix):
            remove_pr_label(server, collection, project, repo, pr, label, pat, timeout)

    new_label = f"{label_prefix}{complexity}"
    add_pr_label(server, collection, project, repo, pr, new_label, pat, timeout)
    return new_label


def has_complexity_label(
    server: str,
    collection: str,
    project: str,
    repo: str,
    pr: int,
    pat: str,
    label_prefix: str = "complexity:",
    timeout: float = DEFAULT_TIMEOUT,
) -> Optional[str]:
    """Return the existing complexity label name if present, else None."""
    for label in get_pr_labels(server, collection, project, repo, pr, pat, timeout):
        if label.startswith(label_prefix):
            return label
    return None


def search_prs(
    server: str,
    collection: str,
    project: str,
    since: datetime,
    until: datetime,
    pat: str,
    sleep_s: float = DEFAULT_SLEEP_SECONDS,
    timeout: float = DEFAULT_TIMEOUT,
    on_pr_found: Optional[Callable[[str], None]] = None,
    state: str = "closed",
    progress_callback: Optional[Callable[[str], None]] = None,
) -> List[str]:
    """
    Enumerate pull requests of a project within a date range.

    Lists the project's repositories and then the PRs of each repository
    (Azure DevOps has no org-wide search like GitHub's). Dates map onto
    ``closedDate`` for closed PRs and ``creationDate`` for open ones.

    Returns:
        List of PR URLs in the same form ``parse_ado_pr_url`` accepts.
    """
    if not pat:
        raise ValueError("Azure DevOps PAT is required. Set AZURE_DEVOPS_PAT or pass --ado-pat.")
    if state not in ("closed", "open", "all"):
        raise ValueError(f"Invalid state: {state}. Must be 'closed', 'open', or 'all'.")

    status_map = {"closed": "completed", "open": "active", "all": "all"}
    status = status_map[state]

    pr_urls: List[str] = []

    def _in_range(pr_data: Dict[str, Any]) -> bool:
        if state == "open":
            date_str = pr_data.get("creationDate")
        else:
            date_str = pr_data.get("closedDate") or pr_data.get("creationDate")
        if not date_str:
            return state != "closed"
        try:
            dt = datetime.fromisoformat(date_str.replace("Z", "+00:00"))
        except ValueError:
            return False
        return since <= dt.replace(tzinfo=None) <= until

    try:
        with httpx.Client(timeout=timeout) as client:
            repos_url = _api_url(server, collection, project, "git/repositories")
            repos = _fetch_json(client, repos_url, pat).get("value") or []

            for repo_obj in repos:
                repo_name = repo_obj.get("name")
                if not repo_name:
                    continue
                prs_url = _api_url(
                    server,
                    collection,
                    project,
                    f"git/repositories/{repo_name}/pullRequests",
                )
                params = {"searchCriteria.status": status}
                try:
                    prs = _fetch_json(client, prs_url, pat, params=params).get("value") or []
                except httpx.HTTPStatusError as e:
                    # A single inaccessible/broken repository (e.g. TF401019 for a
                    # repo the PAT cannot read) must not abort the whole project.
                    if e.response.status_code not in (404, 403):
                        raise
                    if progress_callback:
                        try:
                            progress_callback(
                                f"Skipping repository '{repo_name}'"
                                f" (HTTP {e.response.status_code})"
                            )
                        except Exception:
                            pass
                    continue

                for pr_data in prs:
                    if not _in_range(pr_data):
                        continue
                    pr_id = pr_data.get("pullRequestId")
                    if pr_id is None:
                        continue
                    pr_url = (
                        f"{server}/{collection}/{project}/_git/{repo_name}" f"/pullrequest/{pr_id}"
                    )
                    pr_urls.append(pr_url)
                    if on_pr_found:
                        try:
                            on_pr_found(pr_url)
                        except Exception:
                            pass  # Callback failure must not abort enumeration
                time.sleep(max(0.0, sleep_s))
    except httpx.HTTPStatusError as e:
        raise AzureDevOpsAPIError(e.response.status_code, e.response.text[:500], str(e.request.url))
    except httpx.RequestError as e:
        raise RuntimeError(f"Failed to enumerate PRs: {e}")

    return pr_urls
