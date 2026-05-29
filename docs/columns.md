# Column definitions

The dashboard publishes one row per tracked SDK package. Source data:

- [`data/brownfield.json`](../data/brownfield.json) — the curated list (manually maintained).
- [`scripts/lib/package-index.json`](../scripts/lib/package-index.json) — built by `scripts/build_index.py`, maps each `@azure/arm-*` package to its spec folder in `Azure/azure-rest-api-specs` and its SDK folder in `Azure/azure-sdk-for-js`.

| # | Column | Source |
|---|---|---|
| 1 | **Service** | `brownfield.json` (pass-through) |
| 2 | **ARM Namespace** | `brownfield.json` (pass-through) |
| 3 | **Spec Folder** | `brownfield.json` (pass-through) |
| 4 | **SDK Package Name** | `brownfield.json`; multi-package services are pre-expanded into one row per package |
| 5 | **Specs API Version** | Raw fetch of `{specPath}/main.tsp` on `azure-rest-api-specs@main`; reads the last value of `enum Versions {…}`. Blank if no TypeSpec spec is authored. |
| 6 | **SDK PR** | See below. |
| 7 | **Release Status** | See below. |
| 8 | **Release By** | Labels on the chosen SDK PR. |

## SDK PR (column 6) and Release Status (column 7)

For each row, the workflow checks the SDK repo (`Azure/azure-sdk-for-js@main`)
to decide whether the package has already been migrated to TypeSpec:

`sdkIsTypeSpec = exists("{sdkPath}/tsp-location.yaml" on main)`

### If `sdkIsTypeSpec == true`

The migration PR is found by walking the commit history of
`{sdkPath}/tsp-location.yaml`:

1. `GET /repos/.../commits?path={sdkPath}/tsp-location.yaml&per_page=1` and
   follow the `Link: rel="last"` header to the oldest page → the **first**
   commit that touched the file.
2. `GET /repos/.../commits/{sha}/pulls` → the PR that introduced it (the
   squash-merge PR if applicable).
3. `GET {sdkPath}/package.json` at that commit SHA → `versionAtMerge`.

That PR is reported as **SDK PR**.

Release Status is then:

| Condition | Release Status |
|---|---|
| `versionAtMerge` is published on npm | **Released** |
| `versionAtMerge` is not on npm yet | **To Release** |

### If `sdkIsTypeSpec == false`

The workflow looks for an open, non-draft AutoPR PR that adds
`{sdkPath}/tsp-location.yaml`:

1. One batched search: `repo:Azure/azure-sdk-for-js is:pr is:open draft:false "[AutoPR @azure-arm-" in:title` (paginated).
2. Titles are parsed with `\[AutoPR @azure-arm-([a-z0-9-]+)\]` → grouped by package name.
3. For the candidate PR, `GET /pulls/{n}/files` verifies that
   `{sdkPath}/tsp-location.yaml` is added/renamed in this PR (filters out
   accidental matches).

That PR (if any) is reported as **SDK PR**.

Release Status is then:

| Condition | Release Status |
|---|---|
| matching open non-draft PR found | **In Progress** |
| no such PR | **Not Started** |

## Release By (column 8)

Derived from labels on the chosen SDK PR (see column 6):

| Labels | Release By |
|---|---|
| includes `refresh` | `refresh` |
| includes both `first-typespec-migration` and `Self-Service Release` | `self-serve` |
| no SDK PR, or labels don't match | empty |

## Last Updated banner

Captured once at the start of `scripts/refresh.py` and stored as
`generatedAt` at the top of `site/data.json`. Rendered in the dashboard header.

## API budget

Per workflow run, for *N* rows (~144 today):

- 1 paginated search call (open AutoPR PRs).
- ~1 raw fetch per row (Specs API Version, skipped if no specPath).
- ~1 contents call per row (`tsp-location.yaml` existence check).
- ~3 calls per TypeSpec-migrated row (oldest-commit lookup + PR-from-commit + raw `package.json`) plus 1 npm registry call.
- ~1 `/pulls/{n}/files` call per row that matched the open-PR map.

In practice this is well under the 5,000 req/hr authenticated budget.
There is **no** sticky / carried-over logic — every run is recomputed
from scratch so newly merged or newly opened PRs are always reflected.
