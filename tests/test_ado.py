"""Tests for the Azure DevOps client and PR-source dispatch."""

import base64
from datetime import datetime
from unittest.mock import Mock, patch

import httpx
import pytest

from cli import sources
from cli.ado import (
    AzureDevOpsAPIError,
    build_ado_headers,
    build_file_diff,
    fetch_pr,
    get_pr_labels,
    has_complexity_label,
    remove_pr_label,
    search_prs,
    update_complexity_label,
)
from cli.github import GitHubAPIError
from cli.sources import resolve_pr_ref
from cli.utils import detect_pr_source, is_ado_pr_url, parse_ado_pr_url

SERVER = "https://tfs.company.local:8080/tfs"
COLLECTION = "DefaultCollection"
PROJECT = "MyProject"
REPO = "myrepo"

ADO_URL = f"{SERVER}/{COLLECTION}/{PROJECT}/_git/{REPO}/pullrequest/123"


class FakeResponse:
    """Minimal stand-in for httpx.Response."""

    def __init__(self, json_data=None, text="", content=b"", status_code=200):
        self._json = json_data if json_data is not None else {}
        self.text = text
        self.content = content if content else text.encode("utf-8")
        self.status_code = status_code
        self.headers = {}

    def json(self):
        return self._json

    def raise_for_status(self):
        if self.status_code >= 400:
            request = Mock(url="http://test")
            raise httpx.HTTPStatusError("error", request=request, response=self)


def _client_mock(handler):
    """Build a mock httpx.Client whose .get dispatches to handler(url, params)."""

    def _dispatch(url, headers=None, params=None):
        # Strip the "?api-version=..." query string for path matching
        return handler(str(url).split("?")[0], params)

    client = Mock()
    client.__enter__ = Mock(return_value=client)
    client.__exit__ = Mock(return_value=False)
    client.get.side_effect = _dispatch
    return client


# ---------------------------------------------------------------------------
# URL parsing / source detection
# ---------------------------------------------------------------------------


def test_parse_ado_pr_url_with_path_prefix():
    server, collection, project, repo, pr = parse_ado_pr_url(ADO_URL)
    assert server == SERVER
    assert collection == COLLECTION
    assert project == PROJECT
    assert repo == REPO
    assert pr == 123


def test_parse_ado_pr_url_without_prefix():
    url = "https://dev.azure.com/MyOrg/MyProject/_git/repo/pullrequest/42"
    server, collection, project, repo, pr = parse_ado_pr_url(url)
    assert server == "https://dev.azure.com"
    assert collection == "MyOrg"
    assert pr == 42


@pytest.mark.parametrize(
    "url",
    [
        "https://github.com/owner/repo/pull/123",
        "https://tfs.company.local/tfs/onlycollection",
        "not-a-url",
        "https://tfs.company.local/tfs/col/proj/_git/repo/pullrequest/abc",
    ],
)
def test_parse_ado_pr_url_invalid(url):
    with pytest.raises(ValueError, match="Invalid PR URL"):
        parse_ado_pr_url(url)


def test_detect_pr_source():
    assert detect_pr_source("https://github.com/o/r/pull/1") == "github"
    assert detect_pr_source(ADO_URL) == "azure-devops"
    with pytest.raises(ValueError):
        detect_pr_source("https://example.com/x")
    # Forced source
    assert detect_pr_source(ADO_URL, source="azure-devops") == "azure-devops"
    with pytest.raises(ValueError):
        detect_pr_source(ADO_URL, source="github")
    with pytest.raises(ValueError):
        detect_pr_source("https://github.com/o/r/pull/1", source="azure-devops")


def test_is_ado_pr_url():
    assert is_ado_pr_url(ADO_URL)
    assert not is_ado_pr_url("https://github.com/o/r/pull/1")


# ---------------------------------------------------------------------------
# Auth headers
# ---------------------------------------------------------------------------


def test_build_ado_headers_basic_auth():
    headers = build_ado_headers("my-pat")
    expected = base64.b64encode(b":my-pat").decode("ascii")
    assert headers["Authorization"] == f"Basic {expected}"
    assert headers["Accept"] == "application/json"


def test_build_ado_headers_requires_pat():
    with pytest.raises(ValueError):
        build_ado_headers("")


# ---------------------------------------------------------------------------
# Diff assembly
# ---------------------------------------------------------------------------


def test_build_file_diff_edit():
    diff = build_file_diff("/src/app.py", "line1\n", "line1\nline2\n")
    assert diff is not None
    assert diff.startswith("diff --git a/src/app.py b/src/app.py")
    assert "--- a/src/app.py" in diff
    assert "+++ b/src/app.py" in diff
    assert "+line2" in diff
    assert "@@" in diff


def test_build_file_diff_add():
    diff = build_file_diff("/new.py", "", "hello\n")
    assert diff is not None
    assert "+hello" in diff


def test_build_file_diff_delete():
    diff = build_file_diff("/gone.py", "goodbye\n", "")
    assert diff is not None
    assert "-goodbye" in diff


def test_build_file_diff_unchanged_returns_none():
    assert build_file_diff("/same.py", "same\n", "same\n") is None
    assert build_file_diff("/both-binary.py", None, None) is None


# ---------------------------------------------------------------------------
# fetch_pr
# ---------------------------------------------------------------------------

METADATA = {
    "title": "Fix the thing",
    "description": "PR body",
    "createdBy": {"uniqueName": "dev@corp.example", "displayName": "Dev"},
    "lastMergeSourceCommit": {"commitId": "src1"},
    "lastMergeTargetCommit": {"commitId": "tgt1"},
}

ITERATIONS = {"value": [{"id": 1}, {"id": 2}]}

CHANGES = {
    "changeEntries": [
        {"item": {"path": "/src/app.py"}, "changeType": "edit"},
        {"item": {"path": "/img/logo.png"}, "changeType": "add"},
    ]
}


def _fetch_pr_handler(url, params):
    if "pullRequests/123/iterations/2/changes" in url:
        return FakeResponse(json_data=CHANGES)
    if url.endswith("/pullRequests/123/iterations"):
        return FakeResponse(json_data=ITERATIONS)
    if url.endswith("/pullRequests/123") and "labels" not in url:
        return FakeResponse(json_data=METADATA)
    if "/items" in url:
        version = (params or {}).get("versionDescriptor.version")
        path = (params or {}).get("path")
        if path == "/img/logo.png":
            return FakeResponse(content=b"\x89PNG\x00\x00binary")
        if version == "src1":
            return FakeResponse(content=b"line1\nline2\n")
        return FakeResponse(content=b"line1\n")
    raise AssertionError(f"Unexpected URL: {url}")


@patch("cli.ado.httpx.Client")
def test_fetch_pr_success(mock_client_class):
    mock_client_class.return_value = _client_mock(_fetch_pr_handler)

    diff_text, meta = fetch_pr(SERVER, COLLECTION, PROJECT, REPO, 123, "pat", sleep_s=0, timeout=5)

    # Binary file skipped; text file present with correct stats
    assert "diff --git a/src/app.py b/src/app.py" in diff_text
    assert "+line2" in diff_text
    assert "logo.png" not in diff_text
    assert meta["title"] == "Fix the thing"
    assert meta["body"] == "PR body"
    assert meta["user"]["login"] == "dev@corp.example"
    assert meta["additions"] == 1
    assert meta["deletions"] == 0
    assert meta["changed_files"] == 1
    assert meta["files"][0]["filename"] == "src/app.py"
    assert meta["_ado"]["project"] == PROJECT


def test_fetch_pr_requires_pat():
    with pytest.raises(ValueError, match="AZURE_DEVOPS_PAT"):
        fetch_pr(SERVER, COLLECTION, PROJECT, REPO, 123, "", sleep_s=0)


@patch("cli.ado.httpx.Client")
def test_fetch_pr_http_error(mock_client_class):
    def handler(url, params):
        return FakeResponse(json_data={}, text="Not Found", status_code=404)

    mock_client_class.return_value = _client_mock(handler)

    with pytest.raises(AzureDevOpsAPIError) as exc_info:
        fetch_pr(SERVER, COLLECTION, PROJECT, REPO, 123, "pat", sleep_s=0)
    assert exc_info.value.status_code == 404


# ---------------------------------------------------------------------------
# Labels
# ---------------------------------------------------------------------------


@patch("cli.ado.httpx.Client")
def test_get_pr_labels(mock_client_class):
    mock_client_class.return_value = _client_mock(
        lambda url, params: FakeResponse(
            json_data={"value": [{"name": "complexity:3"}, {"name": "bug"}]}
        )
    )
    assert get_pr_labels(SERVER, COLLECTION, PROJECT, REPO, 123, "pat") == [
        "complexity:3",
        "bug",
    ]


@patch("cli.ado.httpx.Client")
def test_update_and_has_complexity_label(mock_client_class):
    state = {"labels": ["complexity:3", "bug"]}

    def handler(url, params):
        if url.endswith("/labels") and "complexity%3A3" in url:
            state["labels"].remove("complexity:3")
            return FakeResponse(status_code=204)
        if url.endswith("/labels"):
            if state["labels"] and "complexity" not in url:
                return FakeResponse(json_data={"value": [{"name": n} for n in state["labels"]]})
            return FakeResponse(status_code=200, json_data={})
        raise AssertionError(url)

    mock_client_class.return_value = _client_mock(handler)

    assert has_complexity_label(SERVER, COLLECTION, PROJECT, REPO, 123, "pat") == "complexity:3"
    # After removal the old label is gone
    state["labels"] = ["bug"]
    assert has_complexity_label(SERVER, COLLECTION, PROJECT, REPO, 123, "pat") is None


@patch("cli.ado.httpx.Client")
def test_remove_label_404_ok(mock_client_class):
    mock_client_class.return_value = _client_mock(
        lambda url, params: FakeResponse(status_code=404, text="missing")
    )
    remove_pr_label(SERVER, COLLECTION, PROJECT, REPO, 123, "complexity:3", "pat")


@patch("cli.ado.httpx.Client")
def test_update_complexity_label_validates_range(mock_client_class):
    mock_client_class.return_value = _client_mock(
        lambda url, params: FakeResponse(json_data={"value": []})
    )
    with pytest.raises(ValueError, match="between 1 and 10"):
        update_complexity_label(SERVER, COLLECTION, PROJECT, REPO, 123, 99, "pat")


# ---------------------------------------------------------------------------
# search_prs (batch enumeration)
# ---------------------------------------------------------------------------


@patch("cli.ado.httpx.Client")
def test_search_prs_enumerates_repos(mock_client_class):
    since = datetime(2024, 1, 1)
    until = datetime(2024, 1, 31, 23, 59, 59)

    def handler(url, params):
        if url.endswith("/git/repositories"):
            return FakeResponse(json_data={"value": [{"name": "repo-a"}, {"name": "repo-b"}]})
        if "pullRequests" in url:
            return FakeResponse(
                json_data={
                    "value": [
                        {
                            "pullRequestId": 7,
                            "closedDate": "2024-01-15T10:00:00Z",
                        },
                        {
                            "pullRequestId": 8,
                            "closedDate": "2024-02-15T10:00:00Z",  # out of range
                        },
                    ]
                }
            )
        raise AssertionError(url)

    mock_client_class.return_value = _client_mock(handler)

    found = search_prs(SERVER, COLLECTION, PROJECT, since, until, "pat", sleep_s=0, state="closed")
    assert found == [
        f"{SERVER}/{COLLECTION}/{PROJECT}/_git/repo-a/pullrequest/7",
        f"{SERVER}/{COLLECTION}/{PROJECT}/_git/repo-b/pullrequest/7",
    ]


@patch("cli.ado.httpx.Client")
def test_search_prs_skips_inaccessible_repo(mock_client_class):
    since = datetime(2024, 1, 1)
    until = datetime(2024, 1, 31, 23, 59, 59)
    skipped = []

    def handler(url, params):
        if url.endswith("/git/repositories"):
            return FakeResponse(json_data={"value": [{"name": "ok-repo"}, {"name": "missing"}]})
        if "missing" in url:
            return FakeResponse(status_code=404, text="TF401019")
        if "pullRequests" in url:
            return FakeResponse(
                json_data={
                    "value": [
                        {
                            "pullRequestId": 7,
                            "closedDate": "2024-01-15T10:00:00Z",
                        },
                    ]
                }
            )
        raise AssertionError(url)

    mock_client_class.return_value = _client_mock(handler)

    found = search_prs(
        SERVER,
        COLLECTION,
        PROJECT,
        since,
        until,
        "pat",
        sleep_s=0,
        state="closed",
        progress_callback=skipped.append,
    )
    assert found == [f"{SERVER}/{COLLECTION}/{PROJECT}/_git/ok-repo/pullrequest/7"]
    assert any("missing" in msg and "404" in msg for msg in skipped)


def test_search_prs_requires_pat():
    with pytest.raises(ValueError, match="AZURE_DEVOPS_PAT"):
        search_prs(SERVER, COLLECTION, PROJECT, datetime(2024, 1, 1), datetime(2024, 1, 2), "")


def test_search_prs_invalid_state():
    with pytest.raises(ValueError, match="Invalid state"):
        search_prs(
            SERVER,
            COLLECTION,
            PROJECT,
            datetime(2024, 1, 1),
            datetime(2024, 1, 2),
            "pat",
            state="merged-ish",
        )


# ---------------------------------------------------------------------------
# Error compatibility with GitHub handling
# ---------------------------------------------------------------------------


def test_ado_error_is_github_error():
    assert issubclass(AzureDevOpsAPIError, GitHubAPIError)
    err = AzureDevOpsAPIError(404, "not found", "http://x")
    assert isinstance(err, GitHubAPIError)
    assert err.status_code == 404


# ---------------------------------------------------------------------------
# Source dispatch (cli.sources)
# ---------------------------------------------------------------------------


def test_resolve_pr_ref_github():
    ref = resolve_pr_ref("https://github.com/owner/repo/pull/123")
    assert ref.source == "github"
    assert ref.owner == "owner"
    assert ref.repo == "repo"
    assert ref.pr == 123
    assert ref.repo_slug == "owner/repo"


def test_resolve_pr_ref_ado():
    ref = resolve_pr_ref(ADO_URL, ado_pat="pat")
    assert ref.source == "azure-devops"
    assert ref.server == SERVER
    assert ref.collection == COLLECTION
    assert ref.project == PROJECT
    assert ref.repo == REPO
    assert ref.pr == 123
    assert ref.ado_pat == "pat"
    assert ref.repo_slug == "MyProject/myrepo"


def test_resolve_pr_ref_ado_without_pat(monkeypatch):
    monkeypatch.delenv("AZURE_DEVOPS_PAT", raising=False)
    monkeypatch.delenv("AZURE_DEVOPS_TOKEN", raising=False)
    ref = resolve_pr_ref(ADO_URL)
    assert ref.ado_pat is None


def test_sources_fetch_pr_dispatches_to_ado(monkeypatch):
    ref = resolve_pr_ref(ADO_URL, ado_pat="pat")
    calls = {}

    def fake_ado_fetch(*args, **kwargs):
        calls["ado"] = (args, kwargs)
        return "diff", {"title": "t"}

    def fake_github_fetch(*args, **kwargs):
        calls["github"] = (args, kwargs)
        return "diff", {"title": "t"}

    monkeypatch.setattr(sources.ado, "fetch_pr", fake_ado_fetch)
    monkeypatch.setattr(sources.github, "fetch_pr", fake_github_fetch)

    diff, meta = sources.fetch_pr(ref, sleep_s=0)
    assert "ado" in calls and "github" not in calls
    assert diff == "diff" and meta["title"] == "t"


def test_sources_fetch_pr_dispatches_to_github(monkeypatch):
    ref = resolve_pr_ref("https://github.com/owner/repo/pull/123")
    calls = {}

    def fake_ado_fetch(*a, **k):
        calls["ado"] = True
        return "diff", {"title": "t"}

    def fake_github_fetch(*a, **k):
        calls["github"] = True
        return "diff", {"title": "t"}

    monkeypatch.setattr(sources.ado, "fetch_pr", fake_ado_fetch)
    monkeypatch.setattr(sources.github, "fetch_pr", fake_github_fetch)

    diff, meta = sources.fetch_pr(ref, github_token="tok", sleep_s=0)
    assert "github" in calls and "ado" not in calls
    assert diff == "diff" and meta["title"] == "t"


def test_sources_fetch_pr_ado_requires_pat(monkeypatch):
    monkeypatch.delenv("AZURE_DEVOPS_PAT", raising=False)
    monkeypatch.delenv("AZURE_DEVOPS_TOKEN", raising=False)
    ref = resolve_pr_ref(ADO_URL)
    with pytest.raises(ValueError, match="PAT"):
        sources.fetch_pr(ref)
