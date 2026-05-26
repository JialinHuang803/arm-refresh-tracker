# Column definitions

The dashboard publishes one row per tracked SDK package. Source data:

- [`data/brownfield.json`](../data/brownfield.json) — the curated list (manually maintained).
- [`scripts/lib/package-index.json`](../scripts/lib/package-index.json) — built by `scripts/build_index.py`, maps each `@azure/arm-*` package to its spec folder in `Azure/azure-rest-api-specs` and its SDK folder in `Azure/azure-sdk-for-js`.

| # | Column | Source | Notes |
|---|---|---|---|
| 1 | **Service** | `brownfield.json` | Pass-through. |
| 2 | **ARM Namespace** | `brownfield.json` | Pass-through. |
| 3 | **Spec Folder** | `brownfield.json` | Top-level subfolder under `specification/` in azure-rest-api-specs. |
| 4 | **SDK Package Name** | `brownfield.json` | One row per package; multi-package services are pre-expanded. |
| 5 | **Specs API Version** | Raw fetch of `{specPath}/main.tsp` on `azure-rest-api-specs@main`. Reads the last value of `enum Versions {…}`. | Blank if no TypeSpec spec is authored. |
| 6 | **SDK PR** | Two GitHub searches per run: `repo:Azure/azure-sdk-for-js is:pr label:refresh` and `repo:Azure/azure-sdk-for-js is:pr is:open label:"Self-Service Release"`. PR titles (both kinds) match the pattern `[AutoPR @azure-arm-<name>]…`, which is parsed back to `@azure/arm-<name>`. **Closed-but-not-merged refresh PRs are dropped**, and only **open** Self-Service Release PRs are tracked (merged self-service PRs become TypeSpec on main, already captured by `sdkIsTypeSpec`). | If both kinds exist for a package, the **open Self-Service Release PR wins**; otherwise the most recently updated refresh PR wins. |
| 7 | **Release Status** | Derived (see below). | Color-coded badge. |
| 8 | **Release By** | Derived (see below). | |

## Release Status (column 7)

`sdkIsTypeSpec` is determined by checking whether `{sdkPath}/tsp-location.yaml` exists on `azure-sdk-for-js@main`. `pr` is the chosen PR for this package (open self-service > refresh; closed-unmerged refresh PRs are filtered out upstream), so the table below assumes `pr` is `null` / open / merged.

| Condition | Release Status |
|---|---|
| `pr.state == "open"` (refresh **or** Self-Service Release) | **In Progress** |
| `pr.merged == true` && exact `package.json` version is published on npm | **Released** |
| `pr.merged == true` && version is **not** on npm yet | **To Release** |
| `pr == null` && `sdkIsTypeSpec == true` | **Released** (self-served) |
| `pr == null` && `sdkIsTypeSpec == false` | **Not Started** |

## Release By (column 8)

| Condition | Release By |
|---|---|
| has an open Self-Service Release PR | `self-serve` |
| has a refresh PR (open or merged) and no self-service PR | `refresh` |
| no PR && `sdkIsTypeSpec == true` | `self-serve` |
| no PR && `sdkIsTypeSpec == false` | blank |

## Last Updated banner

Captured once at the start of `scripts/refresh.py` and stored as `generatedAt` at the top of `site/data.json`. Rendered in the dashboard header.

## API budget

Per workflow run, when the index cache is warm:

- 1–3 search calls (the refresh-PR search, paginated).
- 1 search call (the open Self-Service Release PR search; rarely paginates).
- ~1 raw fetch per row (Specs API Version).
- ~1 contents call per row (tsp-location.yaml existence).
- ~2 calls per merged refresh PR (package.json version + npm registry).

Comfortably under the 5,000/hr `GITHUB_TOKEN` core quota and the 30/min search quota.
