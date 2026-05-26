"""Generates site/data.json — the dashboard's data file.

Reads:
  - data/brownfield.json (curated list, one row per package)
  - scripts/lib/package-index.json (built by build_index.py)

For each row, computes:
  - specsApiVersion (from main.tsp in azure-rest-api-specs)
  - sdkPr (from the refresh-PR map)
  - sdkIsTypeSpec (existence of tsp-location.yaml on azure-sdk-for-js@main)
  - releaseStatus, releaseBy (derived per plan.md)

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

# URL of the previously-deployed dashboard data. Used by load_previous_rows
# so that Released rows can be carried over without re-fetching their state.
PUBLISHED_DATA_URL = "https://jialinhuang803.github.io/arm-refresh-tracker/data.json"

VERSIONS_ENUM_RE = re.compile(r"enum\s+Versions\s*\{([^}]+)\}", re.DOTALL)
TITLE_PKG_RE = re.compile(r"\[AutoPR @azure-arm-([a-z0-9-]+)\]", re.IGNORECASE)


def _latest_version_from_tsp(text: str) -> str:
    m = VERSIONS_ENUM_RE.search(text)
    if not m:
        return ""
    versions = re.findall(r'"([^"]+)"', m.group(1))
    return versions[-1] if versions else ""


def fetch_refresh_prs(session) -> dict[str, dict]:
    """Search refresh-labeled PRs and map packageName -> PR object.

    Only merged or still-open PRs are kept: closed-unmerged PRs were
    abandoned/superseded and would just clutter the dashboard with broken
    links, so we treat them the same as 'no refresh PR'.
    """
    print("[prs] searching refresh-labeled PRs…")
    items = search_issues(
        session,
        query=f"repo:{SDK_OWNER}/{SDK_REPO} is:pr label:refresh",
    )
    print(f"[prs]   {len(items)} PR(s) found.")
    out: dict[str, dict] = {}
    skipped_closed_unmerged = 0
    for it in items:
        title = it.get("title", "")
        m = TITLE_PKG_RE.search(title)
        if not m:
            continue
        state = it.get("state", "")
        merged = bool(it.get("pull_request", {}).get("merged_at"))
        if state == "closed" and not merged:
            skipped_closed_unmerged += 1
            continue
        pkg_name = f"@azure/arm-{m.group(1)}"
        pr_obj = {
            "number": it["number"],
            "url": it["html_url"],
            "title": title,
            "state": state,
            "merged": merged,
            "mergedAt": it.get("pull_request", {}).get("merged_at"),
            "updatedAt": it.get("updated_at"),
            "labels": [lbl.get("name", "") for lbl in it.get("labels", [])],
            "kind": "refresh",
        }
        # Keep most recently updated if duplicate
        prev = out.get(pkg_name)
        if prev is None or (pr_obj["updatedAt"] or "") > (prev["updatedAt"] or ""):
            out[pkg_name] = pr_obj
    print(f"[prs]   mapped to {len(out)} package(s) (skipped {skipped_closed_unmerged} closed-unmerged).")
    return out


def fetch_self_service_prs(session) -> dict[str, dict]:
    """Search OPEN 'Self-Service Release'-labeled PRs and map packageName -> PR.

    Self-service release PRs are also created by the AutoPR bot with the same
    title pattern as refresh PRs, so the same regex back-maps to the package
    name. We only look at open ones — merged self-service releases land on
    main as TypeSpec code, which is already covered by the sdkIsTypeSpec
    branch in derive_release_status. Closed-unmerged ones are abandoned and
    not actionable.
    """
    print("[prs] searching open Self-Service Release PRs…")
    items = search_issues(
        session,
        query=f'repo:{SDK_OWNER}/{SDK_REPO} is:pr is:open label:"Self-Service Release"',
    )
    print(f"[prs]   {len(items)} PR(s) found.")
    out: dict[str, dict] = {}
    for it in items:
        title = it.get("title", "")
        m = TITLE_PKG_RE.search(title)
        if not m:
            continue
        pkg_name = f"@azure/arm-{m.group(1)}"
        pr_obj = {
            "number": it["number"],
            "url": it["html_url"],
            "title": title,
            "state": it.get("state", ""),
            "merged": False,
            "mergedAt": None,
            "updatedAt": it.get("updated_at"),
            "labels": [lbl.get("name", "") for lbl in it.get("labels", [])],
            "kind": "self-service",
        }
        prev = out.get(pkg_name)
        if prev is None or (pr_obj["updatedAt"] or "") > (prev["updatedAt"] or ""):
            out[pkg_name] = pr_obj
    print(f"[prs]   mapped to {len(out)} package(s).")
    return out


def fetch_specs_api_version(session, spec_path: str | None) -> str:
    if not spec_path:
        return ""
    text = get_raw_file(session, SPECS_OWNER, SPECS_REPO, f"{spec_path}/main.tsp")
    if not text:
        return ""
    return _latest_version_from_tsp(text)


def sdk_is_typespec(session, sdk_path: str | None) -> bool:
    if not sdk_path:
        return False
    return file_exists(session, SDK_OWNER, SDK_REPO, f"{sdk_path}/tsp-location.yaml")


def sdk_main_version(session, sdk_path: str | None) -> str | None:
    if not sdk_path:
        return None
    text = get_raw_file(session, SDK_OWNER, SDK_REPO, f"{sdk_path}/package.json")
    if not text:
        return None
    try:
        return json.loads(text).get("version") or None
    except json.JSONDecodeError:
        return None


def is_published_on_npm(session, package_name: str, version: str) -> bool:
    info = npm_package_versions(session, package_name)
    if not info:
        return False
    return version in (info.get("versions") or {})


def derive_release_by(pr: dict | None, sdk_is_ts: bool) -> str:
    if pr is not None:
        return "self-serve" if pr.get("kind") == "self-service" else "refresh"
    if sdk_is_ts:
        return "self-serve"
    return ""


def derive_release_status(
    session,
    pr: dict | None,
    sdk_is_ts: bool,
    package_name: str,
    sdk_path: str | None,
) -> str:
    # An open Self-Service Release PR only overrides Not Started.
    # If the package already shipped as TypeSpec (sdkIsTypeSpec=True),
    # the open self-service PR is for the next version — keep it Released.
    if pr is not None and pr.get("kind") == "self-service":
        if sdk_is_ts:
            return "Released"
        return "In Progress"
    # Refresh PR state drives the status (closed-unmerged are filtered upstream).
    if pr is not None:
        if pr.get("state") == "open":
            return "In Progress"
        if pr.get("merged"):
            version = sdk_main_version(session, sdk_path)
            if version and is_published_on_npm(session, package_name, version):
                return "Released"
            return "To Release"
    return "Released" if sdk_is_ts else "Not Started"


def load_previous_rows(session) -> dict[str, dict]:
    """Return previous { sdkPackageName: row } so already-Released rows stick.

    Prefers a local site/data.json (handy for dev), otherwise falls back to
    the deployed dashboard JSON. On the very first run, returns an empty map.
    """
    raw_data = None
    source = ""
    if OUTPUT.exists():
        try:
            raw_data = json.loads(OUTPUT.read_text(encoding="utf-8"))
            source = "local site/data.json"
        except json.JSONDecodeError:
            pass
    if raw_data is None:
        try:
            resp = session.get(PUBLISHED_DATA_URL, timeout=30)
            if resp.status_code == 200:
                raw_data = resp.json()
                source = "deployed Pages"
        except Exception:
            pass
    if raw_data is None:
        print("[prev] no previous data.json available; all rows will be re-evaluated.")
        return {}
    rows = raw_data.get("rows", [])
    by_pkg: dict[str, dict] = {}
    for r in rows:
        pkg = r.get("sdkPackageName")
        if pkg:
            by_pkg[pkg] = r
    print(f"[prev] loaded {len(by_pkg)} previous row(s) from {source}.")
    return by_pkg


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
    previous = load_previous_rows(session)

    # Partition rows: those whose previous status was 'Released' stick, the
    # rest need a fresh evaluation.
    sticky: list[tuple[int, dict]] = []
    pending: list[tuple[int, dict]] = []
    for i, row in enumerate(brownfield):
        pkg = row["sdkPackageName"]
        prev = previous.get(pkg) if pkg else None
        if prev and prev.get("releaseStatus") == "Released":
            sticky.append((i, prev))
        else:
            pending.append((i, row))

    print(f"[rows] {len(sticky)} sticky Released, {len(pending)} to re-evaluate.")

    results: dict[int, dict] = {}
    for i, prev in sticky:
        row = brownfield[i]
        results[i] = _row_from_brownfield(
            row,
            specsApiVersion=prev.get("specsApiVersion", ""),
            sdkPr=prev.get("sdkPr"),
            releaseStatus="Released",
            releaseBy=prev.get("releaseBy", ""),
        )

    if pending:
        refresh_prs = fetch_refresh_prs(session)
        self_service_prs = fetch_self_service_prs(session)
        total = len(pending)
        for processed, (i, row) in enumerate(pending, 1):
            pkg = row["sdkPackageName"]
            entry = index.get(pkg, {}) if pkg else {}
            spec_path = entry.get("specPath")
            sdk_path = entry.get("sdkPath")

            sdk_is_ts = sdk_is_typespec(session, sdk_path) if pkg else False
            specs_ver = fetch_specs_api_version(session, spec_path)
            pr = None
            if pkg:
                pr = self_service_prs.get(pkg) or refresh_prs.get(pkg)
            release_by = derive_release_by(pr, sdk_is_ts)
            release_status = derive_release_status(session, pr, sdk_is_ts, pkg or "", sdk_path)

            results[i] = _row_from_brownfield(
                row,
                specsApiVersion=specs_ver,
                sdkPr=pr,
                releaseStatus=release_status,
                releaseBy=release_by,
            )
            if processed % 25 == 0 or processed == total:
                print(f"[rows]   processed {processed}/{total}")
    else:
        print("[rows] no rows needed re-evaluation — skipping all API calls.")

    return [results[i] for i in range(len(brownfield))]


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
