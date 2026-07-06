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
from datetime import date, datetime, timedelta, timezone
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
TSP_PR_LABELS = {"first-typespec-migration", "refresh"}
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


_PKG_NAME_RE = re.compile(r"^@azure/arm-([a-z0-9-]+)$")


def resolve_sdk_path(session, package_name: str, indexed: str | None) -> str | None:
    """Return a usable sdkPath for this package, even if the index lacks one.

    Strategy:
      1. If `indexed` (from package-index.json) is set, trust it.
      2. Otherwise try the natural location: sdk/{suffix}/arm-{suffix}/package.json.
         If `package.json` exists there with the matching name, use that path.
      3. Otherwise return None — the package likely doesn't exist in the repo yet.

    Result is cached in-process to avoid re-probing on subsequent rows.
    """
    if indexed:
        return indexed
    if package_name in _SDK_PATH_CACHE:
        return _SDK_PATH_CACHE[package_name]
    m = _PKG_NAME_RE.match(package_name or "")
    if not m:
        _SDK_PATH_CACHE[package_name] = None
        return None
    suffix = m.group(1)
    guess = f"sdk/{suffix}/arm-{suffix}"
    if file_exists(session, SDK_OWNER, SDK_REPO, f"{guess}/package.json"):
        _SDK_PATH_CACHE[package_name] = guess
        return guess
    _SDK_PATH_CACHE[package_name] = None
    return None


_SDK_PATH_CACHE: dict[str, str | None] = {}


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


def _api_versions_at_commit(session, sdk_path: str, sha: str) -> dict:
    """Read apiVersions from metadata.json at the given commit."""
    text = get_raw_file(session, SDK_OWNER, SDK_REPO, f"{sdk_path}/metadata.json", ref=sha)
    if not text:
        return {}
    try:
        meta = json.loads(text)
        return meta.get("apiVersions") or meta.get("apiVersion") or {}
    except json.JSONDecodeError:
        return {}


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
        "apiVersionsAtMerge": _api_versions_at_commit(session, sdk_path, sha),
    }


def find_stable_pr(session, sdk_path: str, beta_pr_number: int | None) -> dict | None:
    """Find a merged PR with the 'refresh' label for the same package, different from the beta PR.

    Searches for merged PRs that modified the package.json in the sdk_path
    and have the 'refresh' label. Returns the one that is NOT the beta PR.
    """
    short_name = sdk_path.split("/")[-1]  # e.g. arm-automation
    query = (
        f'repo:{SDK_OWNER}/{SDK_REPO} is:pr is:merged label:refresh '
        f'in:title "[AutoPR @azure-{short_name}]"'
    )
    items = search_issues(session, query=query)
    if not items:
        return None

    # Filter out the beta PR and find the stable one
    for it in items:
        if it.get("number") == beta_pr_number:
            continue
        # Verify the title matches exactly this package (GitHub search can return partial matches)
        title = it.get("title", "") or ""
        m = TITLE_PKG_RE.search(title)
        if not m or f"arm-{m.group(1)}" != short_name:
            continue
        # This is a different PR with the 'refresh' label -> stable candidate
        pr_number = it["number"]
        # Fetch full PR details to get merge commit
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


# ---------------------------------------------------------------------------
# open AutoPR PRs (one batched search, then per-package verification)
# ---------------------------------------------------------------------------

def fetch_open_autopr_prs(session) -> dict[str, dict]:
    """Map packageName -> most-recently-updated open, non-draft AutoPR PR.

    A single search call covers every package; we filter and group locally.
    Revert PRs are excluded — the title still mentions the package but the
    PR does the opposite of a migration. PRs that lack a TypeSpec-related
    label (`first-typespec-migration` or `refresh`) are also dropped to
    avoid counting unrelated regeneration PRs as in-progress migrations.
    """
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


def find_open_tsp_pr(
    session, package_name: str, sdk_path: str, open_pr_map: dict[str, dict]
) -> dict | None:
    """Return the open AutoPR (if any) introducing TypeSpec for this package.

    The caller has already verified main lacks tsp-location.yaml for this
    package, so any open AutoPR matching the package's title is treated as
    the in-flight migration PR. We deliberately avoid inspecting the PR's
    file list — AutoPR PRs frequently exceed the 3000-file cap on
    /pulls/{n}/files, which made tsp-location.yaml unreachable and caused
    in-progress packages to fall back to "Not Started".
    """
    candidate = open_pr_map.get(package_name)
    if not candidate:
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


def released_npm_publish_time(
    session,
    package_name: str,
    version: str,
    npm_cache: dict,
    *,
    merged_at: str | None = None,
) -> str | None:
    """Return the ISO publish time of the npm version that satisfies the
    Released check, or None if the TypeSpec migration hasn't shipped.

    Released if either:
      - the exact `version` (from package.json at the introducing commit) is
        on npm, OR
      - some non-alpha version >= `version` exists on npm and was published
        at-or-after `merged_at`.

    The merged_at gate keeps stale pre-migration releases (e.g.
    arm-appservice 30.0.0-beta.x from 2021) from being counted.
    """
    if not version:
        return None
    info = npm_cache.get(package_name)
    if info is None:
        info = npm_package_versions(session, package_name) or {}
        npm_cache[package_name] = info
    versions = (info.get("versions") or {})
    times = info.get("time") or {}
    if not versions:
        return None
    if version in versions:
        return times.get(version) or ""
    try:
        target = _parse_version(version)
    except Exception:
        return None
    for v in versions.keys():
        if "alpha" in v.lower():
            continue
        try:
            cand = _parse_version(v)
        except Exception:
            continue
        if cand < target:
            continue
        t = times.get(v) or ""
        if merged_at and t < merged_at:
            continue
        return t
    return None


def is_published_on_npm(*args, **kwargs) -> bool:
    """Back-compat shim: True if the migration has shipped."""
    return released_npm_publish_time(*args, **kwargs) is not None


def _parse_version(v: str):
    """Lazy import so test runs without packaging installed still load this module."""
    from packaging.version import Version  # type: ignore
    return Version(v)


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
        sdk_path = resolve_sdk_path(session, pkg, entry.get("sdkPath")) if pkg else None

        specs_ver = fetch_specs_api_version(session, spec_path)
        sdk_pr: dict | None = None
        release_status = "Not Started"
        release_by = ""

        released_at_npm: str | None = None
        sdk_version: str = ""
        sdk_api_versions: dict = {}
        stable_pr: dict | None = None
        stable_version: str = ""
        stable_release_status: str = ""
        if sdk_path and pkg and sdk_is_typespec(session, sdk_path):
            tsp_pr = find_first_tsp_pr(session, sdk_path)
            sdk_pr = tsp_pr
            version = (tsp_pr or {}).get("versionAtMerge", "")
            sdk_version = version
            sdk_api_versions = (tsp_pr or {}).get("apiVersionsAtMerge", {})
            merged_at = (tsp_pr or {}).get("mergedAt")
            if version:
                released_at_npm = released_npm_publish_time(
                    session, pkg, version, npm_cache, merged_at=merged_at
                )
            if released_at_npm is not None:
                release_status = "Released"
            else:
                release_status = "To Release"
            release_by = derive_release_by((tsp_pr or {}).get("labels", []))

            # Look for a stable release PR (different from the beta PR)
            if release_by == "refresh" and "beta" in sdk_version:
                beta_pr_number = (tsp_pr or {}).get("number")
                stable_pr = find_stable_pr(session, sdk_path, beta_pr_number)
                if stable_pr:
                    stable_version = stable_pr.get("versionAtMerge", "")
                    # Check if stable version is published on npm
                    if stable_version:
                        stable_released_at = released_npm_publish_time(
                            session, pkg, stable_version, npm_cache,
                            merged_at=stable_pr.get("mergedAt"),
                        )
                        if stable_released_at is not None:
                            stable_release_status = "Released"
                        else:
                            stable_release_status = "To Release"
                    else:
                        stable_release_status = "To Release"
                else:
                    # Check if there's an open PR with refresh label for this package
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
                sdkVersion=sdk_version,
                sdkApiVersions=sdk_api_versions,
                sdkPr=sdk_pr,
                stableVersion=stable_version,
                stablePr=stable_pr,
                stableReleaseStatus=stable_release_status,
                releaseStatus=release_status,
                releaseBy=release_by,
                releasedAt=released_at_npm,
            )
        )

        if i % 10 == 0 or i == total:
            print(f"[rows]   processed {i}/{total}")

    return results


# ---------------------------------------------------------------------------
# entry point
# ---------------------------------------------------------------------------
# Planner — fixed-quota burndown vs the 2026-06-30 deadline.
# ---------------------------------------------------------------------------

PLANNER_START = date(2026, 6, 1)
PLANNER_DEADLINE = date(2026, 6, 30)
PLANNER_HOLIDAYS = {date(2026, 6, 19)}
PLANNER_DAILY_QUOTA = 4
# Snapshot of Released count taken at planner start. Workflow runs are
# stateless (nothing is committed back) so we can't auto-snapshot; bump
# this manually if you reset the baseline.
PLANNER_RELEASED_AT_START = 70


def _is_working_day(d: date) -> bool:
    return d.weekday() < 5 and d not in PLANNER_HOLIDAYS


def _working_days_between(start: date, end_inclusive: date) -> int:
    """Number of working days in [start, end_inclusive]. 0 if end < start."""
    if end_inclusive < start:
        return 0
    n = 0
    d = start
    while d <= end_inclusive:
        if _is_working_day(d):
            n += 1
        d += timedelta(days=1)
    return n


def build_planner(rows: list[dict]) -> dict | None:
    """Burndown snapshot for the dashboard.

    Hard-coded constants for now (start, deadline, holidays, daily quota).
    "Done" = Released only. We have no historical Released-count series,
    so `releasedAtStart` is snapshotted from today's count: every Release
    that ships from now on counts toward the daily quota.
    """
    today = datetime.now(timezone(timedelta(hours=8))).date()
    total = len(rows)
    released_total = sum(1 for r in rows if r.get("releaseStatus") == "Released")
    remaining = max(total - released_total, 0)

    elapsed = _working_days_between(PLANNER_START, min(today, PLANNER_DEADLINE))
    days_remaining = _working_days_between(max(today + timedelta(days=1), PLANNER_START), PLANNER_DEADLINE)

    # This week (Mon 00:00 UTC+8 → next Mon 00:00 UTC+8). week_left counts
    # working days from today through Sunday so the "to ship by Fri" label
    # still tracks how much working time is left.
    monday = today - timedelta(days=today.weekday())
    next_monday = monday + timedelta(days=7)
    week_end = today + timedelta(days=(6 - today.weekday()))
    week_left = _working_days_between(today, min(week_end, PLANNER_DEADLINE))
    weekly_quota = PLANNER_DAILY_QUOTA * _working_days_between(monday, min(week_end, PLANNER_DEADLINE))

    # Released this week = rows whose npm publish time falls in [Mon, next Mon)
    monday_iso = f"{monday.isoformat()}T00:00:00+08:00"
    next_monday_iso = f"{next_monday.isoformat()}T00:00:00+08:00"
    released_this_week = sum(
        1
        for r in rows
        if r.get("releaseStatus") == "Released"
        and r.get("releasedAt")
        and monday_iso <= _as_utc8(r["releasedAt"]) < next_monday_iso
    )
    target_this_week_remaining = max(0, weekly_quota - released_this_week)

    # Baseline snapshot — see PLANNER_RELEASED_AT_START above.
    released_at_start = PLANNER_RELEASED_AT_START
    target_by_today = PLANNER_DAILY_QUOTA * elapsed
    actual_by_today = released_total - released_at_start
    delta = actual_by_today - target_by_today

    required_pace = 0
    if remaining > 0 and days_remaining > 0:
        required_pace = -(-remaining // days_remaining)  # ceil div
    elif remaining > 0:
        required_pace = remaining  # past deadline — show what's left

    if today > PLANNER_DEADLINE:
        phase = "past-deadline"
    elif today < PLANNER_START:
        phase = "pre-start"
    elif remaining == 0:
        phase = "complete"
    else:
        phase = "active"

    return {
        "startDate": PLANNER_START.isoformat(),
        "deadline": PLANNER_DEADLINE.isoformat(),
        "holidays": sorted(d.isoformat() for d in PLANNER_HOLIDAYS),
        "dailyQuota": PLANNER_DAILY_QUOTA,
        "today": today.isoformat(),
        "phase": phase,
        "totalPackages": total,
        "releasedTotal": released_total,
        "releasedAtStart": released_at_start,
        "remaining": remaining,
        "workingDaysElapsed": elapsed,
        "workingDaysRemaining": days_remaining,
        "workingDaysLeftThisWeek": week_left,
        "targetByToday": target_by_today,
        "actualByToday": actual_by_today,
        "delta": delta,
        "weeklyQuota": weekly_quota,
        "releasedThisWeek": released_this_week,
        "targetThisWeekRemaining": target_this_week_remaining,
        "requiredPaceToFinish": required_pace,
    }


def _as_utc8(iso: str) -> str:
    """Normalize an ISO timestamp to a string that sorts correctly against a
    UTC+8 boundary. Convert any Z/UTC time to UTC+8 wall-clock so lexical
    comparison against `YYYY-MM-DDT00:00:00+08:00` works.
    """
    try:
        dt = datetime.fromisoformat(iso.replace("Z", "+00:00"))
    except Exception:
        return iso
    dt8 = dt.astimezone(timezone(timedelta(hours=8)))
    return dt8.isoformat()


# ---------------------------------------------------------------------------

def main() -> None:
    if not os.environ.get("GITHUB_TOKEN"):
        print("warning: GITHUB_TOKEN not set; rate limits may bite.", file=sys.stderr)
    session = make_session()
    rows = build_rows(session)
    payload = {
        "generatedAt": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "planner": build_planner(rows),
        "rows": rows,
    }
    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    print(f"\nWrote {OUTPUT.relative_to(REPO_ROOT)} with {len(rows)} row(s).")


if __name__ == "__main__":
    main()
