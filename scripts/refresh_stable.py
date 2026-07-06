"""Refresh only the stable release status columns.

Reads the frozen main tracker data (site/data-main.json), updates the three
dynamic stable columns (stableVersion, stablePr, stableReleaseStatus) for
each stable candidate, and writes site/data.json for the stable page.
"""
from __future__ import annotations

import json
import re
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from packaging.version import Version

sys.path.insert(0, str(Path(__file__).resolve().parent))

from lib.github_api import (  # noqa: E402
    gh_get,
    make_session,
    npm_package_versions,
    search_issues,
)

REPO_ROOT = Path(__file__).resolve().parents[1]
INDEX = REPO_ROOT / "scripts" / "lib" / "package-index.json"
BASE_DATA = REPO_ROOT / "site" / "data-main.json"
OUTPUT = REPO_ROOT / "site" / "data.json"

SDK_OWNER = "Azure"
SDK_REPO = "azure-sdk-for-js"

TITLE_PKG_RE = re.compile(r"\[AutoPR @azure-arm-([a-z0-9-]+)\]", re.IGNORECASE)
TSP_PR_LABELS = {"first-typespec-migration", "refresh"}


def _version_at_commit(session, sdk_path: str, sha: str) -> str:
    from lib.github_api import get_raw_file
    text = get_raw_file(session, SDK_OWNER, SDK_REPO, f"{sdk_path}/package.json", ref=sha)
    if not text:
        return ""
    try:
        return (json.loads(text).get("version") or "").strip()
    except json.JSONDecodeError:
        return ""


def find_stable_pr(session, sdk_path: str, beta_pr_number: int | None) -> dict | None:
    """Find a merged PR with the 'refresh' label for the same package, different from the beta PR."""
    short_name = sdk_path.split("/")[-1]
    query = (
        f'repo:{SDK_OWNER}/{SDK_REPO} is:pr is:merged label:refresh '
        f'in:title "[AutoPR @azure-{short_name}]"'
    )
    items = search_issues(session, query=query)
    if not items:
        return None

    for it in items:
        if it.get("number") == beta_pr_number:
            continue
        title = it.get("title", "") or ""
        m = TITLE_PKG_RE.search(title)
        if not m or f"arm-{m.group(1)}" != short_name:
            continue
        pr_number = it["number"]
        resp = gh_get(
            session,
            f"/repos/{SDK_OWNER}/{SDK_REPO}/pulls/{pr_number}",
            allow_404=True,
        )
        if resp is None:
            continue
        pr = resp.json()
        if not pr.get("merged_at"):
            continue
        merge_sha = pr.get("merge_commit_sha", "")
        version = _version_at_commit(session, sdk_path, merge_sha) if merge_sha else ""
        return {
            "number": pr_number,
            "url": pr.get("html_url", ""),
            "title": pr.get("title", ""),
            "mergedAt": pr.get("merged_at"),
            "versionAtMerge": version,
        }
    return None


def fetch_batch_refresh_prs(session, index: dict) -> dict[str, list[dict]]:
    """Find refresh-labeled PRs (open or merged) without [AutoPR] title pattern."""
    path_to_pkg: dict[str, str] = {}
    for pkg_name, info in index.items():
        sdk_path = info.get("sdkPath", "")
        if sdk_path:
            path_to_pkg[sdk_path] = pkg_name

    print("[batch-prs] searching for batch refresh PRs…")
    open_items = search_issues(
        session,
        query=f'repo:{SDK_OWNER}/{SDK_REPO} is:pr is:open label:refresh',
    )
    open_items = [
        it for it in open_items
        if "[AutoPR" not in (it.get("title") or "")
    ]

    cutoff_date = (date.today() - timedelta(days=90)).isoformat()
    merged_items = search_issues(
        session,
        query=f'repo:{SDK_OWNER}/{SDK_REPO} is:pr is:merged label:refresh merged:>{cutoff_date}',
    )
    merged_items = [
        it for it in merged_items
        if "[AutoPR" not in (it.get("title") or "")
    ]

    all_items = open_items + merged_items
    print(f"[batch-prs]   {len(open_items)} open, {len(merged_items)} recently merged.")

    out: dict[str, list[dict]] = {}
    for it in all_items:
        title = it.get("title", "") or ""
        if title.lstrip().lower().startswith("revert"):
            continue
        pr_number = it["number"]
        is_open = it.get("state", "").lower() == "open"

        resp = gh_get(
            session,
            f"/repos/{SDK_OWNER}/{SDK_REPO}/pulls/{pr_number}/files",
            params={"per_page": "100"},
            allow_404=True,
        )
        if resp is None:
            continue
        files = resp.json()
        for page in [2, 3]:
            if len(files) % 100 != 0:
                break
            resp2 = gh_get(
                session,
                f"/repos/{SDK_OWNER}/{SDK_REPO}/pulls/{pr_number}/files",
                params={"per_page": "100", "page": str(page)},
                allow_404=True,
            )
            if resp2 is None or not resp2.json():
                break
            files.extend(resp2.json())

        packages_in_pr: set[str] = set()
        for f in files:
            fn = f.get("filename", "")
            parts = fn.split("/")
            if len(parts) == 4 and parts[0] == "sdk" and parts[3] == "package.json":
                sdk_path = "/".join(parts[:3])
                pkg_name = path_to_pkg.get(sdk_path)
                if pkg_name:
                    packages_in_pr.add(pkg_name)

        if not packages_in_pr:
            continue

        pr_info = {
            "number": pr_number,
            "url": it.get("html_url", ""),
            "title": title,
            "state": "open" if is_open else "merged",
            "merged": not is_open,
            "mergedAt": (it.get("pull_request", {}).get("merged_at")
                         or it.get("closed_at")) if not is_open else None,
            "updatedAt": it.get("updated_at"),
        }

        print(f"[batch-prs]   PR #{pr_number} ({pr_info['state']}): {len(packages_in_pr)} packages")
        for pkg_name in packages_in_pr:
            out.setdefault(pkg_name, []).append(pr_info)

    print(f"[batch-prs]   mapped to {len(out)} package(s) total.")
    return out


def fetch_open_autopr_prs(session) -> dict[str, dict]:
    """Map packageName -> most-recently-updated open, non-draft AutoPR PR."""
    print("[prs] searching open non-draft AutoPR PRs…")
    items = search_issues(
        session,
        query=f'repo:{SDK_OWNER}/{SDK_REPO} is:pr is:open draft:false in:title "[AutoPR @azure-arm-"',
    )
    print(f"[prs]   {len(items)} candidate PR(s).")
    out: dict[str, dict] = {}
    for it in items:
        title = it.get("title", "") or ""
        if title.lstrip().lower().startswith("revert"):
            continue
        labels = {(lbl.get("name") or "") for lbl in it.get("labels", [])}
        if not (labels & TSP_PR_LABELS):
            continue
        m = TITLE_PKG_RE.search(title)
        if not m:
            continue
        pkg = f"@azure/arm-{m.group(1)}"
        prev = out.get(pkg)
        if prev is None or (it.get("updated_at") or "") > (prev.get("updated_at") or ""):
            out[pkg] = it
    print(f"[prs]   grouped into {len(out)} package(s).")
    return out


def released_on_npm(session, package_name: str, version: str, npm_cache: dict) -> bool:
    """Check if a specific version is published on npm."""
    info = npm_cache.get(package_name)
    if info is None:
        info = npm_package_versions(session, package_name) or {}
        npm_cache[package_name] = info
    return version in (info.get("versions") or {})


def update_stable_status(session, rows: list[dict], index: dict) -> list[dict]:
    """Update the stable columns for each candidate row."""
    open_pr_map = fetch_open_autopr_prs(session)
    batch_pr_map = fetch_batch_refresh_prs(session, index)
    npm_cache: dict[str, dict] = {}

    total = len(rows)
    for i, row in enumerate(rows, 1):
        pkg = row.get("sdkPackageName", "")
        sdk_version = row.get("sdkVersion", "")
        release_by = row.get("releaseBy", "")
        sdk_path = (index.get(pkg) or {}).get("sdkPath", "")
        beta_pr_number = (row.get("sdkPr") or {}).get("number")

        # Only process refresh candidates with beta versions
        if release_by != "refresh" or "beta" not in sdk_version or not sdk_path:
            continue

        print(f"[{i}/{total}] {pkg}…")

        stable_pr: dict | None = None
        stable_version: str = ""
        stable_release_status: str = ""

        # 1. Look for a single-package stable PR
        stable_pr = find_stable_pr(session, sdk_path, beta_pr_number)
        if stable_pr:
            stable_version = stable_pr.get("versionAtMerge", "")
            if stable_version and released_on_npm(session, pkg, stable_version, npm_cache):
                stable_release_status = "Released"
            else:
                stable_release_status = "To Release"
        else:
            # 2. Check if stable version already exists on npm
            info = npm_cache.get(pkg)
            if info is None:
                info = npm_package_versions(session, pkg) or {}
                npm_cache[pkg] = info
            try:
                beta_ver = Version(sdk_version)
            except Exception:
                beta_ver = None
            if beta_ver and info:
                stable_versions = [
                    v for v in (info.get("versions") or {}).keys()
                    if "beta" not in v and "alpha" not in v
                ]
                newer_stable = []
                for v in stable_versions:
                    try:
                        if Version(v) > beta_ver:
                            newer_stable.append(v)
                    except Exception:
                        pass
                if newer_stable:
                    stable_version = sorted(newer_stable, key=Version)[-1]
                    stable_release_status = "Released"

            # 3. Check batch PRs
            if not stable_release_status:
                batch_prs = batch_pr_map.get(pkg, [])
                merged_batch = [
                    p for p in batch_prs
                    if p.get("merged") and p.get("number") != beta_pr_number
                ]
                open_batch = [
                    p for p in batch_prs
                    if not p.get("merged") and p.get("number") != beta_pr_number
                ]

                if merged_batch:
                    bp = merged_batch[0]
                    # Fetch full PR details to get merge commit SHA
                    pr_resp = gh_get(
                        session,
                        f"/repos/{SDK_OWNER}/{SDK_REPO}/pulls/{bp['number']}",
                        allow_404=True,
                    )
                    merge_sha = ""
                    if pr_resp:
                        merge_sha = pr_resp.json().get("merge_commit_sha", "")
                    version = _version_at_commit(session, sdk_path, merge_sha) if merge_sha else ""
                    stable_pr = {
                        "number": bp["number"],
                        "url": bp.get("url", ""),
                        "title": bp.get("title", ""),
                        "mergedAt": bp.get("mergedAt"),
                        "versionAtMerge": version,
                    }
                    stable_version = version
                    if stable_version and released_on_npm(session, pkg, stable_version, npm_cache):
                        stable_release_status = "Released"
                    else:
                        stable_release_status = "To Release"
                elif open_batch:
                    bp = open_batch[0]
                    # Read version from PR head branch
                    pr_resp = gh_get(
                        session,
                        f"/repos/{SDK_OWNER}/{SDK_REPO}/pulls/{bp['number']}",
                        allow_404=True,
                    )
                    head_sha = ""
                    if pr_resp:
                        head_sha = pr_resp.json().get("head", {}).get("sha", "")
                    version = _version_at_commit(session, sdk_path, head_sha) if head_sha else ""
                    stable_release_status = "In Progress"
                    stable_version = version
                    stable_pr = {
                        "number": bp["number"],
                        "url": bp.get("url", ""),
                        "title": bp.get("title", ""),
                        "mergedAt": None,
                        "versionAtMerge": version,
                    }
                else:
                    # 4. Check single-package open PRs
                    open_stable = open_pr_map.get(pkg)
                    if open_stable and open_stable.get("number") != beta_pr_number:
                        stable_release_status = "In Progress"
                        stable_pr = {
                            "number": open_stable["number"],
                            "url": open_stable.get("html_url", ""),
                            "title": open_stable.get("title", ""),
                            "mergedAt": None,
                            "versionAtMerge": "",
                        }
                    else:
                        stable_release_status = "Not Started"

        # Update the row
        row["stableVersion"] = stable_version
        row["stablePr"] = stable_pr
        row["stableReleaseStatus"] = stable_release_status

    return rows


def main():
    if not BASE_DATA.exists():
        raise SystemExit(f"missing {BASE_DATA}; freeze main tracker data first.")
    if not INDEX.exists():
        raise SystemExit(f"missing {INDEX}; run scripts/build_index.py first.")

    base = json.loads(BASE_DATA.read_text(encoding="utf-8"))
    index = json.loads(INDEX.read_text(encoding="utf-8"))
    rows = base.get("rows", [])

    session = make_session()
    rows = update_stable_status(session, rows, index)

    output = {
        "generatedAt": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "planner": base.get("planner", {}),
        "rows": rows,
    }
    OUTPUT.write_text(json.dumps(output, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"\n✅ Wrote {OUTPUT} ({len(rows)} rows)")


if __name__ == "__main__":
    main()
