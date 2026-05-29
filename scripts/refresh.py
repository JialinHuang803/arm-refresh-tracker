"""Generates site/data.json — the dashboard's data file.

Reads:
  - data/brownfield.json (curated list, one row per package)
  - scripts/lib/package-index.json (built by build_index.py)

Per-row workflow (see docs/columns.md for full rationale):

  1. If sdk/<path>/tsp-location.yaml exists on main:
       - Find the FIRST PR that introduced that file on main.
       - Read package.json at that PR's introducing commit -> versionAtMerge.
       - If versionAtMerge is published on npm  -> Released
         else                                   -> To Release
       sdkPr = that introducing PR.

  2. Otherwise (no tsp-location.yaml on main):
       - Look for an open, non-draft AutoPR PR for this package that ADDS
         sdk/<path>/tsp-location.yaml.
       - If such PR exists                       -> In Progress
         else                                    -> Not Started
       sdkPr = that open PR (if any).

Release By, in both branches, comes from the labels on sdkPr:
  - 'refresh' label present                              -> "refresh"
  - 'first-typespec-migration' + 'Self-Service Release'  -> "self-serve"
  - otherwise                                            -> ""

Writes site/data.json containing { "generatedAt": "...", "rows": [...] }.
"""

from __future__ import annotations

import json
import os
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from lib.github_api import (  # noqa: E402
    file_exists,
    get_raw_file,
    gh_get,
    make_session,
    npm_package_versions,
    search_issues,
)

REPO_ROOT = Path(__file__).resolve().parents[1]
BROWNFIELD = REPO_ROOT / "data" / "brownfield.json"
INDEX = REPO_ROOT / "scripts" / "lib" / "package-index.json"
OUTPUT = REPO_ROOT / "site" / "data.json"

SDK_OWNER = "Azure"
SDK_REPO = "azure-sdk-for-js"
SPECS_OWNER = "Azure"
SPECS_REPO = "azure-rest-api-specs"

VERSIONS_ENUM_RE = re.compile(r"enum\s+Versions\s*\{([^}]+)\}", re.DOTALL)
TITLE_PKG_RE = re.compile(r"\[AutoPR @azure-arm-([a-z0-9-]+)\]", re.IGNORECASE)
LINK_LAST_RE = re.compile(r'<[^>]*[?&]page=(\d+)[^>]*>;\s*rel="last"')


# ---------------------------------------------------------------------------
# specs api version
# ---------------------------------------------------------------------------

def _latest_version_from_tsp(text: str) -> str:
    m = VERSIONS_ENUM_RE.search(text)
    if not m:
        return ""
    versions = re.findall(r'"([^"]+)"', m.group(1))
    return versions[-1] if versions else ""


def fetch_specs_api_version(session, spec_path: str | None) -> str:
    if not spec_path:
        return ""
    text = get_raw_file(session, SPECS_OWNER, SPECS_REPO, f"{spec_path}/main.tsp")
    if not text:
        return ""
    return _latest_version_from_tsp(text)


# ---------------------------------------------------------------------------
# tsp-location.yaml presence + first-introducing PR
# ---------------------------------------------------------------------------

def sdk_is_typespec(session, sdk_path: str | None) -> bool:
    if not sdk_path:
        return False
    return file_exists(session, SDK_OWNER, SDK_REPO, f"{sdk_path}/tsp-location.yaml")


def _find_oldest_commit_sha(session, file_path: str) -> str | None:
    """Return the SHA of the very first commit on main that touched file_path.

    Uses the commits list API with per_page=1 + Link rel="last" header to
    jump straight to the oldest entry instead of paginating through history.
    """
    resp = gh_get(
        session,
        f"/repos/{SDK_OWNER}/{SDK_REPO}/commits",
        params={"path": file_path, "per_page": "1"},
        allow_404=True,
    )
    if resp is None:
        return None
    items = resp.json()
    if not items:
        return None
    link = resp.headers.get("Link", "")
    m = LINK_LAST_RE.search(link)
    if not m:
        # only one page total -> the single item is also the oldest
        return items[0].get("sha")
    last_page = int(m.group(1))
    resp = gh_get(
        session,
        f"/repos/{SDK_OWNER}/{SDK_REPO}/commits",
        params={"path": file_path, "per_page": "1", "page": str(last_page)},
        allow_404=True,
    )
    if resp is None:
        return None
    items = resp.json()
    if not items:
        return None
    return items[0].get("sha")


def _pr_for_commit(session, sha: str) -> dict | None:
    resp = gh_get(
        session,
        f"/repos/{SDK_OWNER}/{SDK_REPO}/commits/{sha}/pulls",
        allow_404=True,
    )
    if resp is None:
        return None
    prs = resp.json()
    if not prs:
        return None
    # If multiple PRs include this commit (rare), prefer the earliest merged one.
    prs.sort(key=lambda p: (p.get("merged_at") or "9999", p.get("number") or 0))
    return prs[0]


def _version_at_commit(session, sdk_path: str, sha: str) -> str:
    text = get_raw_file(session, SDK_OWNER, SDK_REPO, f"{sdk_path}/package.json", ref=sha)
    if not text:
        return ""
    try:
        return (json.loads(text).get("version") or "").strip()
    except json.JSONDecodeError:
        return ""


def find_first_tsp_pr(session, sdk_path: str) -> dict | None:
    """Find the PR that first added sdk/<path>/tsp-location.yaml on main."""
    file_path = f"{sdk_path}/tsp-location.yaml"
    sha = _find_oldest_commit_sha(session, file_path)
    if not sha:
        return None
    pr = _pr_for_commit(session, sha)
    if not pr:
        return None
    return {
        "number": pr["number"],
        "url": pr["html_url"],
        "title": pr.get("title", ""),
        "state": pr.get("state", "closed"),
        "merged": pr.get("merged_at") is not None,
        "mergedAt": pr.get("merged_at"),
        "updatedAt": pr.get("updated_at"),
        "labels": [lbl.get("name", "") for lbl in pr.get("labels", [])],
        "introducingSha": sha,
        "versionAtMerge": _version_at_commit(session, sdk_path, sha),
    }


# ---------------------------------------------------------------------------
# open AutoPR PRs (one batched search, then per-package verification)
# ---------------------------------------------------------------------------

def fetch_open_autopr_prs(session) -> dict[str, dict]:
    """Map packageName -> most-recently-updated open, non-draft AutoPR PR.

    A single search call covers every package; we filter and group locally.
    """
    print("[prs] searching open non-draft AutoPR PRs…")
    items = search_issues(
        session,
        query=f'repo:{SDK_OWNER}/{SDK_REPO} is:pr is:open draft:false in:title "[AutoPR @azure-arm-"',
    )
    print(f"[prs]   {len(items)} candidate PR(s).")
    out: dict[str, dict] = {}
    for it in items:
        m = TITLE_PKG_RE.search(it.get("title", ""))
        if not m:
            continue
        pkg = f"@azure/arm-{m.group(1)}"
        prev = out.get(pkg)
        if prev is None or (it.get("updated_at") or "") > (prev.get("updated_at") or ""):
            out[pkg] = it
    print(f"[prs]   grouped into {len(out)} package(s).")
    return out


def _pr_adds_file(session, pr_number: int, file_path: str) -> bool:
    """Return True if `file_path` is added/renamed (i.e. newly present) in this PR."""
    page = 1
    while page <= 10:  # PRs >1000 changed files are vanishingly rare here
        resp = gh_get(
            session,
            f"/repos/{SDK_OWNER}/{SDK_REPO}/pulls/{pr_number}/files",
            params={"per_page": "100", "page": page},
            allow_404=True,
        )
        if resp is None:
            return False
        files = resp.json()
        if not files:
            return False
        for f in files:
            if f.get("filename") == file_path and f.get("status") in ("added", "renamed", "modified"):
                # First-typespec-migration adds the file. If a PR modifies it
                # in a TS package, we'd never reach here (tsp-location.yaml
                # already on main goes down the other branch).
                return f.get("status") in ("added", "renamed")
        if len(files) < 100:
            return False
        page += 1
    return False


def find_open_tsp_pr(
    session, package_name: str, sdk_path: str, open_pr_map: dict[str, dict]
) -> dict | None:
    candidate = open_pr_map.get(package_name)
    if not candidate:
        return None
    file_path = f"{sdk_path}/tsp-location.yaml"
    if not _pr_adds_file(session, candidate["number"], file_path):
        return None
    return {
        "number": candidate["number"],
        "url": candidate["html_url"],
        "title": candidate.get("title", ""),
        "state": "open",
        "merged": False,
        "mergedAt": None,
        "updatedAt": candidate.get("updated_at"),
        "labels": [lbl.get("name", "") for lbl in candidate.get("labels", [])],
    }


# ---------------------------------------------------------------------------
# release status / release-by derivation
# ---------------------------------------------------------------------------

def derive_release_by(labels: list[str]) -> str:
    s = {lbl for lbl in labels}
    if "refresh" in s:
        return "refresh"
    if "first-typespec-migration" in s and "Self-Service Release" in s:
        return "self-serve"
    return ""


def is_published_on_npm(session, package_name: str, version: str, npm_cache: dict) -> bool:
    if not version:
        return False
    info = npm_cache.get(package_name)
    if info is None:
        info = npm_package_versions(session, package_name) or {}
        npm_cache[package_name] = info
    return version in (info.get("versions") or {})


# ---------------------------------------------------------------------------
# row builder
# ---------------------------------------------------------------------------

def _row_from_brownfield(row: dict, **dynamic) -> dict:
    return {
        "service": row["service"],
        "armNamespace": row["armNamespace"],
        "specFolder": row["specFolder"],
        "sdkPackageName": row["sdkPackageName"],
        **dynamic,
    }


def build_rows(session) -> list[dict]:
    brownfield = json.loads(BROWNFIELD.read_text(encoding="utf-8"))
    if not INDEX.exists():
        raise SystemExit(f"missing {INDEX}; run scripts/build_index.py first.")
    index = json.loads(INDEX.read_text(encoding="utf-8"))

    open_pr_map = fetch_open_autopr_prs(session)

    npm_cache: dict[str, dict] = {}
    results: list[dict] = []
    total = len(brownfield)
    for i, row in enumerate(brownfield, 1):
        pkg = row.get("sdkPackageName")
        entry = index.get(pkg, {}) if pkg else {}
        spec_path = entry.get("specPath")
        sdk_path = entry.get("sdkPath")

        specs_ver = fetch_specs_api_version(session, spec_path)
        sdk_pr: dict | None = None
        release_status = "Not Started"
        release_by = ""

        if sdk_path and pkg and sdk_is_typespec(session, sdk_path):
            tsp_pr = find_first_tsp_pr(session, sdk_path)
            sdk_pr = tsp_pr
            version = (tsp_pr or {}).get("versionAtMerge", "")
            if version and is_published_on_npm(session, pkg, version, npm_cache):
                release_status = "Released"
            else:
                release_status = "To Release"
            release_by = derive_release_by((tsp_pr or {}).get("labels", []))
        elif sdk_path and pkg:
            open_pr = find_open_tsp_pr(session, pkg, sdk_path, open_pr_map)
            if open_pr is not None:
                sdk_pr = open_pr
                release_status = "In Progress"
                release_by = derive_release_by(open_pr.get("labels", []))

        results.append(
            _row_from_brownfield(
                row,
                specsApiVersion=specs_ver,
                sdkPr=sdk_pr,
                releaseStatus=release_status,
                releaseBy=release_by,
            )
        )

        if i % 10 == 0 or i == total:
            print(f"[rows]   processed {i}/{total}")

    return results


# ---------------------------------------------------------------------------
# entry point
# ---------------------------------------------------------------------------

def main() -> None:
    if not os.environ.get("GITHUB_TOKEN"):
        print("warning: GITHUB_TOKEN not set; rate limits may bite.", file=sys.stderr)
    session = make_session()
    rows = build_rows(session)
    payload = {
        "generatedAt": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "rows": rows,
    }
    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    print(f"\nWrote {OUTPUT.relative_to(REPO_ROOT)} with {len(rows)} row(s).")


if __name__ == "__main__":
    main()
