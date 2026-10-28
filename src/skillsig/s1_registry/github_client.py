"""
STAGE: s1_registry
MODULE: github_client
PURPOSE: Cached GitHub REST client with a hard call budget and rate-limit backoff.
INPUTS: cfg.github, env GITHUB_TOKEN, src/data/cache/github.sqlite
OUTPUTS: src/data/cache/github.sqlite (etag_cache)
RQ LINK: RQ1 (repo metadata for registry context) and RQ2 (live commit history)
"""

from __future__ import annotations

import json
import logging
import os
import sqlite3
import time
from datetime import UTC, datetime
from typing import Any
from urllib.parse import quote

import httpx

from skillsig.config import Config
from skillsig.paths import resolve

logger = logging.getLogger(__name__)

_API = "https://api.github.com"
_MAX_TRIES = 5
_RATE_FLOOR = 50
_CACHE_NAME = "github.sqlite"
_REDACTED = "[redacted]"


class CallBudgetExceeded(RuntimeError):
    """Raised when non-304 GitHub calls reach cfg.github.max_calls."""

    def __init__(self, max_calls: int) -> None:
        self.max_calls = max_calls
        super().__init__(f"GitHub call budget exhausted (max_calls={max_calls})")


class GitHubRequestError(RuntimeError):
    """GitHub returned an error after retries, or a response this client cannot use."""

    def __init__(self, status: int, url: str, detail: str = "") -> None:
        self.status = status
        self.url = url
        message = f"GitHub API {status} for {url}"
        if detail:
            message = f"{message}: {detail}"
        super().__init__(message)


class _TokenRedactor(logging.Filter):
    """Remove the token from log lines emitted while this client is open."""

    def __init__(self, token: str | None) -> None:
        super().__init__()
        self._token = token or ""

    def filter(self, record: logging.LogRecord) -> bool:
        if self._token and self._token in record.getMessage():
            record.msg = record.getMessage().replace(self._token, _REDACTED)
            record.args = ()
        return True


def _now() -> float:
    return time.time()


def _sleep(seconds: float) -> None:
    time.sleep(seconds)


class GitHubClient:
    """REST client for repo metadata, path history, and blob SHAs.

    Conditional requests use ETag. A 304 is served from cache and does not
    increment `calls_made`. The token is read from the argument or `GITHUB_TOKEN`
    and is never written to logs.
    """

    def __init__(
        self,
        cfg: Config,
        token: str | None = None,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        chosen = token if token is not None else os.environ.get("GITHUB_TOKEN")
        self._cfg = cfg
        self._token = chosen or None
        self.calls_made = 0
        self._closed = False
        self._db_path = resolve(cfg, cfg.paths.cache) / _CACHE_NAME
        self._http = httpx.Client(
            base_url=_API,
            headers=_default_headers(cfg, self._token),
            transport=transport,
            timeout=30.0,
            follow_redirects=False,
        )
        self._redactor = _TokenRedactor(self._token)
        logging.getLogger(__name__).addFilter(self._redactor)

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        self._http.close()
        logging.getLogger(__name__).removeFilter(self._redactor)

    def __enter__(self) -> GitHubClient:
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()

    def repo(self, owner: str, name: str) -> dict[str, Any] | None:
        """Repo metadata, or None when GitHub returns 404."""
        payload, _headers = self._get(_repo_url(owner, name))
        if payload is None:
            return None
        if not isinstance(payload, dict):
            raise GitHubRequestError(200, _repo_url(owner, name), "expected a repository object")
        return _repo_fields(payload)

    def commits_for_path(
        self,
        owner: str,
        name: str,
        path: str,
        since: str | None = None,
    ) -> list[dict[str, Any]]:
        """Commits touching `path`, following Link rel=next. Empty when the repo is 404."""
        params: dict[str, str] | None = {
            "path": path,
            "per_page": str(self._cfg.github.per_page),
        }
        if since is not None:
            params["since"] = since
        url: str | None = _repo_url(owner, name, "commits")
        rows: list[dict[str, Any]] = []
        while url:
            payload, headers = self._get(url, params)
            if payload is None:
                if rows:
                    raise GitHubRequestError(404, url, "commit page not found")
                return []
            if not isinstance(payload, list):
                raise GitHubRequestError(200, url, "expected a commit list")
            rows.extend(_commit_row(url, item) for item in payload)
            url = _next_link(headers)
            params = None
        return rows

    def blob_sha_at(self, owner: str, name: str, path: str, ref: str) -> str | None:
        """Contents API blob SHA at `ref`, or None when the path is missing."""
        url = _repo_url(owner, name, f"contents/{quote(path, safe='/')}")
        payload, _headers = self._get(url, {"ref": ref})
        if payload is None:
            return None
        sha = payload.get("sha") if isinstance(payload, dict) else None
        if not isinstance(sha, str):
            raise GitHubRequestError(200, url, "contents response has no sha")
        return sha

    def rate_limit(self) -> dict[str, Any]:
        """Core rate-limit snapshot: limit, remaining, reset, used."""
        payload, _headers = self._get("/rate_limit")
        if not isinstance(payload, dict):
            return {}
        rate = payload.get("rate")
        if not isinstance(rate, dict):
            return payload
        return {key: rate.get(key) for key in ("limit", "remaining", "reset", "used")}

    def _get(self, url: str, params: dict[str, str] | None = None) -> tuple[Any, httpx.Headers]:
        last: httpx.Response | None = None
        for attempt in range(1, _MAX_TRIES + 1):
            response = self._attempt(url, params)
            last = response
            if response.status_code == 304:
                cached_url = str(response.request.url)
                return self._cached_json(cached_url), response.headers
            if not _retryable(response) or attempt == _MAX_TRIES:
                return _interpret(self, response), response.headers
            delay = _retry_delay(response, attempt)
            if delay > 0:
                logger.info("retrying after HTTP %s in %.1fs", response.status_code, delay)
                _sleep(delay)
        status = last.status_code if last is not None else 0
        raise GitHubRequestError(status, url, "retries exhausted")

    def _attempt(self, url: str, params: dict[str, str] | None) -> httpx.Response:
        if self.calls_made >= self._cfg.github.max_calls:
            raise CallBudgetExceeded(self._cfg.github.max_calls)
        request = self._build(url, params)
        response = self._http.send(request)
        logger.info("GET %s -> %s", request.url, response.status_code)
        if response.status_code != 304:
            self.calls_made += 1
        _wait_for_rate_limit(response)
        return response

    def _build(self, url: str, params: dict[str, str] | None) -> httpx.Request:
        request = self._http.build_request("GET", url, params=params)
        cached = self._cache_get(str(request.url))
        if cached is not None:
            request.headers["If-None-Match"] = cached[0]
        return request

    def _cached_json(self, url: str) -> Any:
        cached = self._cache_get(url)
        if cached is None:
            raise GitHubRequestError(304, url, "304 without a cached body")
        return _parse_json_text(cached[1], url)

    def _cache_get(self, url: str) -> tuple[str, str] | None:
        conn = self._connect()
        try:
            row = conn.execute("SELECT etag, body FROM etag_cache WHERE url = ?", (url,)).fetchone()
        finally:
            conn.close()
        if row is None:
            return None
        return str(row[0]), str(row[1])

    def _cache_put(self, url: str, etag: str, body: str) -> None:
        conn = self._connect()
        try:
            conn.execute(
                """
                INSERT INTO etag_cache (url, etag, body, fetched_at)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(url) DO UPDATE SET
                    etag = excluded.etag,
                    body = excluded.body,
                    fetched_at = excluded.fetched_at
                """,
                (url, etag, body, _stamp()),
            )
            conn.commit()
        finally:
            conn.close()

    def _connect(self) -> sqlite3.Connection:
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(self._db_path)
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS etag_cache (
                url TEXT PRIMARY KEY,
                etag TEXT NOT NULL,
                body TEXT NOT NULL,
                fetched_at TEXT NOT NULL
            )
            """
        )
        return conn


def _default_headers(cfg: Config, token: str | None) -> dict[str, str]:
    headers = {
        "Accept": "application/vnd.github+json",
        "User-Agent": cfg.github.user_agent,
    }
    if token:
        headers["Authorization"] = f"Bearer {token}"
    return headers


def _repo_url(owner: str, name: str, suffix: str = "") -> str:
    base = f"/repos/{quote(owner, safe='')}/{quote(name, safe='')}"
    return f"{base}/{suffix}" if suffix else base


def _interpret(client: GitHubClient, response: httpx.Response) -> Any:
    url = str(response.request.url)
    status = response.status_code
    if status == 404:
        return None
    if 200 <= status < 300:
        _store(client, response)
        return _json_body(response, url)
    raise GitHubRequestError(status, url, _error_message(response))


def _store(client: GitHubClient, response: httpx.Response) -> None:
    etag = response.headers.get("etag")
    if not etag:
        return
    client._cache_put(str(response.request.url), etag, _cache_body(response))


def _cache_body(response: httpx.Response) -> str:
    """Keep the response JSON, dropping contents-API file text."""
    text = response.text
    try:
        payload = json.loads(text)
    except json.JSONDecodeError:
        return text
    if isinstance(payload, dict) and "encoding" in payload and "content" in payload:
        kept = {key: value for key, value in payload.items() if key not in {"content", "encoding"}}
        return json.dumps(kept, sort_keys=True, separators=(",", ":"))
    return text


def _json_body(response: httpx.Response, url: str) -> Any:
    if not response.content:
        return None
    try:
        return response.json()
    except json.JSONDecodeError as exc:
        raise GitHubRequestError(response.status_code, url, "invalid JSON") from exc


def _parse_json_text(body: str, url: str) -> Any:
    if not body:
        return None
    try:
        return json.loads(body)
    except json.JSONDecodeError as exc:
        raise GitHubRequestError(304, url, "cached body is not JSON") from exc


def _wait_for_rate_limit(response: httpx.Response) -> None:
    remaining = _header_int(response.headers, "x-ratelimit-remaining")
    reset_at = _header_int(response.headers, "x-ratelimit-reset")
    if remaining is None or reset_at is None or remaining >= _RATE_FLOOR:
        return
    delay = reset_at - _now()
    if delay <= 0:
        return
    logger.info("GitHub rate limit remaining %s; sleeping %.0fs until reset", remaining, delay)
    _sleep(delay)


def _retryable(response: httpx.Response) -> bool:
    status = response.status_code
    if status >= 500 or status == 429:
        return True
    if status != 403:
        return False
    if response.headers.get("retry-after"):
        return True
    return "rate limit" in _error_message(response).lower()


def _retry_delay(response: httpx.Response, attempt: int) -> float:
    remaining = _header_int(response.headers, "x-ratelimit-remaining")
    if response.status_code == 403 and remaining is not None and remaining < _RATE_FLOOR:
        return 0.0
    retry_after = response.headers.get("retry-after")
    if retry_after:
        try:
            return max(0.0, float(retry_after))
        except ValueError:
            pass
    return float(2 ** (attempt - 1))


def _header_int(headers: httpx.Headers, name: str) -> int | None:
    raw = headers.get(name)
    if raw is None:
        return None
    try:
        return int(raw)
    except ValueError:
        return None


def _error_message(response: httpx.Response) -> str:
    try:
        payload = response.json()
    except (json.JSONDecodeError, UnicodeDecodeError):
        return ""
    if not isinstance(payload, dict):
        return ""
    message = payload.get("message")
    if not isinstance(message, str):
        return ""
    return message[:300]


def _next_link(headers: httpx.Headers) -> str | None:
    link = headers.get("link")
    if not link:
        return None
    for part in link.split(","):
        url_part, *params = part.split(";")
        rels = set()
        for param in params:
            key, _, value = param.strip().partition("=")
            if key.strip().lower() == "rel":
                rels.add(value.strip().strip('"').lower())
        if "next" in rels:
            return url_part.strip().strip("<>")
    return None


def _repo_fields(payload: dict[str, Any]) -> dict[str, Any]:
    license_obj = payload.get("license")
    spdx = license_obj.get("spdx_id") if isinstance(license_obj, dict) else None
    return {
        "stars": payload.get("stargazers_count"),
        "forks": payload.get("forks_count"),
        "license": spdx,
        "created_at": payload.get("created_at"),
        "pushed_at": payload.get("pushed_at"),
        "default_branch": payload.get("default_branch"),
        "archived": payload.get("archived"),
        "fork": payload.get("fork"),
        "description": payload.get("description"),
    }


def _commit_row(url: str, item: Any) -> dict[str, Any]:
    if not isinstance(item, dict):
        raise GitHubRequestError(200, url, "commit entry is not an object")
    commit = item.get("commit") if isinstance(item.get("commit"), dict) else {}
    dated = commit.get("author") if isinstance(commit.get("author"), dict) else {}
    if "date" not in dated and isinstance(commit.get("committer"), dict):
        dated = commit["committer"]
    actor = item.get("author") if isinstance(item.get("author"), dict) else {}
    return {
        "sha": item.get("sha"),
        "date": dated.get("date"),
        "message": commit.get("message"),
        "author_login": actor.get("login"),
        "author_type": actor.get("type"),
    }


def _stamp() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
