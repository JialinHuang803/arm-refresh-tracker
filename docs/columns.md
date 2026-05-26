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

Evaluation order (first match wins):

| # | Condition | Release Status |
|---|---|---|
| 1 | row was **Released** in the previous run | **Released** (sticky — see below) |
| 2 | `pr.kind == "self-service"` && `sdkIsTypeSpec == true` | **Released** (open PR is for the next version) |
| 3 | `pr.kind == "self-service"` && `sdkIsTypeSpec == false` | **In Progress** |
| 4 | refresh `pr.state == "open"` | **In Progress** |
| 5 | refresh `pr.merged == true` && exact `package.json` version is published on npm | **Released** |
| 6 | refresh `pr.merged == true` && version is **not** on npm yet | **To Release** |
| 7 | `pr == null` && `sdkIsTypeSpec == true` | **Released** (self-served) |
| 8 | `pr == null` && `sdkIsTypeSpec == false` | **Not Started** |

### Sticky Released

Once a row is **Released**, future runs reuse the entire previous snapshot for that row (specsApiVersion, sdkPr link, releaseBy) and skip all per-row API calls. The "previous snapshot" is loaded from `site/data.json` if present locally, otherwise fetched from the deployed Pages URL.

This means new spec API versions or new self-service PRs are **not** reflected for already-Released rows. If you need to force a re-evaluation, delete the package from `data/brownfield.json` and add it back, or run with the local `site/data.json` deleted and patch the deployed JSON.

## Release By (column 8)

| Condition | Release By |
|---|---|
| sticky Released (carried over from previous run) | (whatever was previously recorded) |
| open Self-Service Release PR | `self-serve` |
| refresh PR (open or merged) and no self-service PR | `refresh` |
| no PR && `sdkIsTypeSpec == true` | `self-serve` |
| no PR && `sdkIsTypeSpec == false` | blank |

## Last Updated banner

Captured once at the start of `scripts/refresh.py` and stored as `generatedAt` at the top of `site/data.json`. Rendered in the dashboard header.

## API budget

Per workflow run, when the index cache is warm and there are *N* non-sticky rows:

- 1 GET on `https://jialinhuang803.github.io/arm-refresh-tracker/data.json` (the previous snapshot).
- 1–3 search calls (the refresh-PR search, paginated) **only if at least one row is non-sticky**.
- 1 search call (the open Self-Service Release PR search) **only if at least one row is non-sticky**.
- ~1 raw fetch per non-sticky row (Specs API Version).
- ~1 contents call per non-sticky row (tsp-location.yaml existence).
- ~2 calls per merged refresh PR in those *N* (package.json version + npm registry).

In steady state most rows are sticky-Released, so *N* shrinks over time and runs get cheaper.
