"""Thin HTTP helpers for the GitHub REST API + raw content + npm registry.

All functions accept a `requests.Session` and reuse connection pooling.
Rate-limit-aware retry: a 403 / secondary-rate-limit response triggers a
single sleep based on `Retry-After` / `X-RateLimit-Reset` then one retry.
"""

from __future__ import annotations

import base64
import os
import time
from typing import Any

import requests

GITHUB_API = "https://api.github.com"
RAW_BASE = "https://raw.githubusercontent.com"
NPM_REGISTRY = "https://registry.npmjs.org"

USER_AGENT = "arm-refresh-tracker (+https://github.com/JialinHuang803/arm-refresh-tracker)"


def make_session(token: str | None = None) -> requests.Session:
    s = requests.Session()
    token = token or os.environ.get("GITHUB_TOKEN")
    headers = {
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
        "User-Agent": USER_AGENT,
    }
    if token:
        headers["Authorization"] = f"Bearer {token}"
    s.headers.update(headers)
    return s


def _sleep_for_rate_limit(resp: requests.Response) -> bool:
    """If the response is a rate-limit failure, sleep and return True."""
    if resp.status_code not in (403, 429):
        return False
    retry_after = resp.headers.get("Retry-After")
    if retry_after:
        time.sleep(min(int(retry_after) + 1, 120))
        return True
    reset = resp.headers.get("X-RateLimit-Reset")
    remaining = resp.headers.get("X-RateLimit-Remaining")
    if remaining == "0" and reset:
        delay = max(int(reset) - int(time.time()), 0) + 2
        if delay <= 180:
            time.sleep(delay)
            return True
    return False


def gh_get(session: requests.Session, path_or_url: str, *, params: dict | None = None, allow_404: bool = False) -> requests.Response | None:
    """GET with one retry on rate-limit. Returns None on 404 when allow_404."""
    url = path_or_url if path_or_url.startswith("http") else f"{GITHUB_API}{path_or_url}"
    for attempt in range(2):
        resp = session.get(url, params=params, timeout=30)
        if resp.status_code == 200:
            return resp
        if allow_404 and resp.status_code == 404:
            return None
        if attempt == 0 and _sleep_for_rate_limit(resp):
            continue
        resp.raise_for_status()
    return None


def search_code(session: requests.Session, query: str, *, per_page: int = 100) -> list[dict]:
    """Paginated code search. Returns the list of items across all pages."""
    out: list[dict] = []
    page = 1
    while True:
        resp = gh_get(
            session,
            "/search/code",
            params={"q": query, "per_page": per_page, "page": page},
        )
        if resp is None:
            break
        data = resp.json()
        items = data.get("items", [])
        out.extend(items)
        total = data.get("total_count", 0)
        if page * per_page >= total or not items:
            break
        page += 1
        # search has a strict 30/min limit; throttle gently between pages
        time.sleep(2)
    return out


def search_issues(session: requests.Session, query: str, *, per_page: int = 100) -> list[dict]:
    """Paginated search/issues (PRs + issues). Returns items across all pages."""
    out: list[dict] = []
    page = 1
    while True:
        resp = gh_get(
            session,
            "/search/issues",
            params={"q": query, "per_page": per_page, "page": page, "sort": "updated", "order": "desc"},
        )
        if resp is None:
            break
        data = resp.json()
        items = data.get("items", [])
        out.extend(items)
        total = data.get("total_count", 0)
        if page * per_page >= total or not items:
            break
        page += 1
        time.sleep(2)
    return out


def get_contents_file(
    session: requests.Session,
    owner: str,
    repo: str,
    path: str,
    ref: str = "main",
) -> str | None:
    """Fetch a file via the contents API and return decoded text (or None on 404)."""
    resp = gh_get(
        session,
        f"/repos/{owner}/{repo}/contents/{path}",
        params={"ref": ref},
        allow_404=True,
    )
    if resp is None:
        return None
    payload = resp.json()
    if isinstance(payload, list):
        # path resolved to a directory; let caller handle separately
        return None
    encoding = payload.get("encoding")
    content = payload.get("content", "")
    if encoding == "base64":
        try:
            return base64.b64decode(content).decode("utf-8", errors="replace")
        except Exception:
            return None
    return content


def get_raw_file(session: requests.Session, owner: str, repo: str, path: str, ref: str = "main") -> str | None:
    url = f"{RAW_BASE}/{owner}/{repo}/{ref}/{path}"
    resp = session.get(url, timeout=30)
    if resp.status_code == 404:
        return None
    resp.raise_for_status()
    return resp.text


def file_exists(session: requests.Session, owner: str, repo: str, path: str, ref: str = "main") -> bool:
    resp = gh_get(
        session,
        f"/repos/{owner}/{repo}/contents/{path}",
        params={"ref": ref},
        allow_404=True,
    )
    return resp is not None


def npm_package_versions(session: requests.Session, package_name: str) -> dict[str, Any] | None:
    """Fetch /{packageName} from the npm registry. Returns the JSON dict or None."""
    url = f"{NPM_REGISTRY}/{package_name}"
    resp = session.get(url, timeout=30, headers={"Accept": "application/json", "User-Agent": USER_AGENT})
    if resp.status_code == 404:
        return None
    resp.raise_for_status()
    return resp.json()
