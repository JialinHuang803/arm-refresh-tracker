"""Builds scripts/lib/package-index.json mapping @azure/arm-* package names
to their tspconfig.yaml directory in azure-rest-api-specs and to their
SDK package directory in azure-sdk-for-js.

Strategy:
- Code search for tspconfig.yaml files in azure-rest-api-specs that
  reference @azure/arm- packages, then fetch each YAML to extract the
  exact `package-details.name`.
- Code search for package.json files in azure-sdk-for-js that contain
  `"@azure/arm-` to map package names to SDK directories. The result
  set is filtered to ARM management packages only.

Run from the repo root:
    python scripts/build_index.py
"""

from __future__ import annotations

import json
import os
import re
import sys
from pathlib import Path

# Allow `from lib.github_api import ...` when running this script directly.
sys.path.insert(0, str(Path(__file__).resolve().parent))

import yaml  # type: ignore  # noqa: E402
from lib.github_api import (  # noqa: E402
    get_contents_file,
    make_session,
    search_code,
)

REPO_ROOT = Path(__file__).resolve().parents[1]
OUTPUT = REPO_ROOT / "scripts" / "lib" / "package-index.json"

SPECS_OWNER = "Azure"
SPECS_REPO = "azure-rest-api-specs"
SDK_OWNER = "Azure"
SDK_REPO = "azure-sdk-for-js"


def _ts_package_name(yaml_text: str) -> str | None:
    """Extract options.@azure-tools/typespec-ts.package-details.name from tspconfig.yaml."""
    try:
        data = yaml.safe_load(yaml_text) or {}
    except yaml.YAMLError:
        return None
    options = (data.get("options") or {}).get("@azure-tools/typespec-ts") or {}
    name = (options.get("package-details") or {}).get("name")
    if isinstance(name, str) and name.startswith("@azure/arm-"):
        return name
    return None


def _pkg_name_from_pkgjson(text: str) -> str | None:
    """Extract the `name` field from a package.json without full JSON parse robustness."""
    try:
        obj = json.loads(text)
    except json.JSONDecodeError:
        return None
    name = obj.get("name")
    if isinstance(name, str) and name.startswith("@azure/arm-"):
        return name
    return None


def build_spec_map(session) -> dict[str, str]:
    """Return packageName -> tspconfigDirectory (relative to repo root)."""
    print(f"[specs] searching tspconfig.yaml files in {SPECS_OWNER}/{SPECS_REPO}…")
    items = search_code(
        session,
        query=f'filename:tspconfig.yaml "@azure/arm-" repo:{SPECS_OWNER}/{SPECS_REPO}',
    )
    print(f"[specs]   {len(items)} candidate file(s); fetching contents to confirm names…")
    out: dict[str, str] = {}
    for item in items:
        path = item.get("path", "")
        if not (path.endswith("/tspconfig.yaml") or path == "tspconfig.yaml"):
            continue
        text = get_contents_file(session, SPECS_OWNER, SPECS_REPO, path)
        if not text:
            continue
        name = _ts_package_name(text)
        if not name:
            continue
        out[name] = path.rsplit("/tspconfig.yaml", 1)[0]
    print(f"[specs]   resolved {len(out)} package(s).")
    return out


def build_sdk_map(session) -> dict[str, str]:
    """Return packageName -> sdk directory (relative to repo root)."""
    print(f"[sdk] searching package.json files in {SDK_OWNER}/{SDK_REPO}…")
    items = search_code(
        session,
        query=f'filename:package.json "@azure/arm-" repo:{SDK_OWNER}/{SDK_REPO}',
    )
    sdk_pat = re.compile(r"^sdk/[^/]+/arm-[^/]+/package\.json$")
    candidates = [it for it in items if sdk_pat.match(it.get("path", ""))]
    print(f"[sdk]   {len(items)} hits, {len(candidates)} look like ARM package roots; verifying…")
    out: dict[str, str] = {}
    for item in candidates:
        path = item["path"]
        text = get_contents_file(session, SDK_OWNER, SDK_REPO, path)
        if not text:
            continue
        name = _pkg_name_from_pkgjson(text)
        if not name:
            continue
        out[name] = path.rsplit("/package.json", 1)[0]
    print(f"[sdk]   resolved {len(out)} package(s).")
    return out


def main() -> None:
    if not os.environ.get("GITHUB_TOKEN"):
        print("warning: GITHUB_TOKEN not set; code search will be heavily rate-limited.", file=sys.stderr)

    session = make_session()
    spec_map = build_spec_map(session)
    sdk_map = build_sdk_map(session)

    all_names = sorted(set(spec_map) | set(sdk_map))
    index = {}
    for name in all_names:
        entry = {}
        if name in spec_map:
            entry["specPath"] = spec_map[name]
        if name in sdk_map:
            entry["sdkPath"] = sdk_map[name]
        index[name] = entry

    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT.write_text(json.dumps(index, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    both = sum(1 for v in index.values() if "specPath" in v and "sdkPath" in v)
    spec_only = sum(1 for v in index.values() if "specPath" in v and "sdkPath" not in v)
    sdk_only = sum(1 for v in index.values() if "sdkPath" in v and "specPath" not in v)
    print(
        f"\nWrote {OUTPUT.relative_to(REPO_ROOT)}: total={len(index)} both={both} "
        f"spec-only={spec_only} sdk-only={sdk_only}"
    )


if __name__ == "__main__":
    main()
