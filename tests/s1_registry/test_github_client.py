from __future__ import annotations

import logging
import sqlite3
from collections.abc import Callable
from dataclasses import replace

import httpx
import pytest

from skillsig.config import Config
from skillsig.paths import resolve
from skillsig.s1_registry.github_client import (
    CallBudgetExceeded,
    GitHubClient,
    GitHubRequestError,
)

TOKEN = "ghp_skillsig_test_token_9f3c"
REPO_JSON = {
    "stargazers_count": 12,
    "forks_count": 3,
    "license": {"key": "mit", "name": "MIT License", "spdx_id": "MIT"},
    "created_at": "2025-10-01T00:00:00Z",
    "pushed_at": "2026-07-01T00:00:00Z",
    "default_branch": "main",
    "archived": False,
    "fork": False,
    "description": "skills",
}
EXPECTED_REPO = {
    "stars": 12,
    "forks": 3,
    "license": "MIT",
    "created_at": "2025-10-01T00:00:00Z",
    "pushed_at": "2026-07-01T00:00:00Z",
    "default_branch": "main",
    "archived": False,
    "fork": False,
    "description": "skills",
}


def _commit(sha: str, message: str = "subject line\n\nbody") -> dict[str, object]:
    return {
        "sha": sha,
        "commit": {
            "message": message,
            "author": {"date": "2026-01-02T03:04:05Z"},
            "committer": {"date": "2026-01-02T03:04:06Z"},
        },
        "author": {"login": "octocat", "type": "User"},
    }


def _ok(payload: object, **headers: str) -> httpx.Response:
    return httpx.Response(200, json=payload, headers=headers)


@pytest.fixture
def github(tmp_cfg: Config, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    opened: list[GitHubClient] = []

    def open_client(
        handler: Callable[[httpx.Request], httpx.Response],
        *,
        token: str | None = None,
        cfg: Config | None = None,
    ) -> GitHubClient:
        client = GitHubClient(
            cfg or tmp_cfg,
            token=token,
            transport=httpx.MockTransport(handler),
        )
        opened.append(client)
        return client

    yield open_client
    for client in opened:
        client.close()


def test_304_is_served_from_cache_and_does_not_increment_calls(github, tmp_cfg: Config) -> None:
    seen: list[str | None] = []

    def handler(request: httpx.Request) -> httpx.Response:
        match = request.headers.get("if-none-match")
        seen.append(match)
        if match == '"v1"':
            return httpx.Response(304, headers={"ETag": '"v1"'})
        return _ok(REPO_JSON, ETag='"v1"')

    client = github(handler)
    assert client.repo("acme", "skills") == EXPECTED_REPO
    assert client.calls_made == 1
    assert client.repo("acme", "skills") == EXPECTED_REPO
    assert client.calls_made == 1
    assert seen == [None, '"v1"']

    db_path = resolve(tmp_cfg, tmp_cfg.paths.cache) / "github.sqlite"
    with sqlite3.connect(db_path) as conn:
        columns = [row[1] for row in conn.execute("PRAGMA table_info(etag_cache)")]
        stored = conn.execute("SELECT etag, body FROM etag_cache").fetchone()
    assert columns == ["url", "etag", "body", "fetched_at"]
    assert stored is not None
    assert stored[0] == '"v1"'
    assert "stargazers_count" in stored[1]


def test_remaining_below_50_sleeps_until_reset(github, monkeypatch: pytest.MonkeyPatch) -> None:
    now = 1_700_000_000
    reset = now + 90
    slept: list[float] = []
    monkeypatch.setattr("skillsig.s1_registry.github_client._now", lambda: float(now))
    monkeypatch.setattr("skillsig.s1_registry.github_client._sleep", slept.append)

    def handler(_request: httpx.Request) -> httpx.Response:
        return _ok(
            REPO_JSON,
            **{
                "X-RateLimit-Remaining": "49",
                "X-RateLimit-Reset": str(reset),
            },
        )

    client = github(handler)
    assert client.repo("acme", "skills")["stars"] == 12
    assert slept == [90.0]


def test_404_returns_none(github) -> None:
    calls = {"n": 0}

    def handler(_request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(404, json={"message": "Not Found"})

    client = github(handler)
    assert client.repo("acme", "missing") is None
    assert client.blob_sha_at("acme", "missing", "skills/SKILL.md", "main") is None
    assert calls["n"] == 2
    assert client.calls_made == 2


def test_5xx_retried_five_times_then_raises(github, monkeypatch: pytest.MonkeyPatch) -> None:
    attempts = {"n": 0}
    slept: list[float] = []
    monkeypatch.setattr("skillsig.s1_registry.github_client._sleep", slept.append)

    def handler(_request: httpx.Request) -> httpx.Response:
        attempts["n"] += 1
        return httpx.Response(503, json={"message": "unavailable"})

    client = github(handler)
    with pytest.raises(GitHubRequestError) as exc:
        client.repo("acme", "skills")
    assert exc.value.status == 503
    assert attempts["n"] == 5
    assert slept == [1.0, 2.0, 4.0, 8.0]
    assert client.calls_made == 5


def test_token_never_appears_in_logs(github, caplog: pytest.LogCaptureFixture) -> None:
    seen: dict[str, str | None] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["authorization"] = request.headers.get("authorization")
        seen["accept"] = request.headers.get("accept")
        seen["user_agent"] = request.headers.get("user-agent")
        return _ok(REPO_JSON)

    caplog.set_level(logging.DEBUG)
    client = github(handler, token=TOKEN)
    assert client.repo("acme", "skills")["license"] == "MIT"
    assert seen["authorization"] == f"Bearer {TOKEN}"
    assert seen["accept"] == "application/vnd.github+json"
    assert seen["user_agent"] == "skillsig-research/0.1"
    assert caplog.records
    for record in caplog.records:
        message = record.getMessage()
        assert TOKEN not in message
        assert "authorization" not in message.lower()


def test_commits_follow_link_header(github) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if "page=2" in str(request.url):
            return _ok([_commit("bbbb", "second")])
        assert request.url.params["path"] == "skills/SKILL.md"
        assert request.url.params["per_page"] == "100"
        assert request.url.params["since"] == "2026-01-01T00:00:00Z"
        link = (
            '<https://api.github.com/repos/acme/skills/commits?page=2>; rel="next", '
            '<https://api.github.com/repos/acme/skills/commits?page=2>; rel="last"'
        )
        return _ok([_commit("aaaa")], Link=link)

    client = github(handler)
    rows = client.commits_for_path(
        "acme",
        "skills",
        "skills/SKILL.md",
        since="2026-01-01T00:00:00Z",
    )
    assert rows == [
        {
            "sha": "aaaa",
            "date": "2026-01-02T03:04:05Z",
            "message": "subject line\n\nbody",
            "author_login": "octocat",
            "author_type": "User",
        },
        {
            "sha": "bbbb",
            "date": "2026-01-02T03:04:05Z",
            "message": "second",
            "author_login": "octocat",
            "author_type": "User",
        },
    ]


def test_call_budget_stops_before_the_next_page(github, tmp_cfg: Config) -> None:
    calls = {"n": 0}
    limited = replace(tmp_cfg, github=replace(tmp_cfg.github, max_calls=1))

    def handler(_request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        link = '<https://api.github.com/repos/acme/skills/commits?page=2>; rel="next"'
        return _ok([_commit("aaaa")], Link=link)

    client = github(handler, cfg=limited)
    with pytest.raises(CallBudgetExceeded):
        client.commits_for_path("acme", "skills", "SKILL.md")
    assert calls["n"] == 1
    assert client.calls_made == 1


def test_rate_limit_returns_core_window(github) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/rate_limit"
        return _ok(
            {
                "rate": {
                    "limit": 5000,
                    "remaining": 4999,
                    "reset": 123,
                    "used": 1,
                }
            }
        )

    client = github(handler)
    assert client.rate_limit() == {
        "limit": 5000,
        "remaining": 4999,
        "reset": 123,
        "used": 1,
    }


def test_blob_sha_at_reads_contents_sha(github, tmp_cfg: Config) -> None:
    skill_text = "c2VjcmV0IHNraWxsIHRleHQ="

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.params["ref"] == "abc123"
        return _ok(
            {
                "type": "file",
                "sha": "blobsha",
                "encoding": "base64",
                "content": skill_text,
            },
            ETag='"blob1"',
        )

    client = github(handler)
    assert client.blob_sha_at("acme", "skills", "skills/SKILL.md", "abc123") == "blobsha"
    db_path = resolve(tmp_cfg, tmp_cfg.paths.cache) / "github.sqlite"
    with sqlite3.connect(db_path) as conn:
        stored = conn.execute("SELECT body FROM etag_cache").fetchone()
    assert stored is not None
    assert skill_text not in stored[0]
    assert "blobsha" in stored[0]
