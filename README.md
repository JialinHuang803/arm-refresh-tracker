# ARM SDK Refresh Tracker

Automated dashboard tracking the Swagger → TypeSpec refresh status of the
Azure JS management-plane SDKs (`@azure/arm-*`).

📊 **Dashboard**: https://JialinHuang803.github.io/arm-refresh-tracker/

## How it works

A GitHub Actions workflow runs daily (06:00 UTC) and on demand. It:

1. Reads the curated list of services from [`data/brownfield.json`](data/brownfield.json).
2. Rebuilds [`scripts/lib/package-index.json`](scripts/lib/package-index.json) (cached weekly) — a map from each `@azure/arm-*` package to its spec/SDK directories, built via the GitHub code-search API.
3. For each row, queries `Azure/azure-rest-api-specs`, `Azure/azure-sdk-for-js`, and the npm registry to compute the `Specs API Version`, the active refresh PR, the `Release Status`, and the `Release By`.
4. Writes [`site/data.json`](site/data.json) and publishes the static [`site/`](site/) directory to GitHub Pages.

See [`docs/columns.md`](docs/columns.md) for the exact derivation rules for every column.

## Repository layout

```
arm-refresh-tracker/
├── data/
│   └── brownfield.json                 # source-of-truth list (manual)
├── scripts/
│   ├── convert_brownfield.py           # one-off xlsx → brownfield.json
│   ├── build_index.py                  # rebuilds the package index
│   ├── refresh.py                      # produces site/data.json
│   ├── lib/
│   │   ├── github_api.py
│   │   └── package-index.json          # (generated)
│   └── requirements.txt
├── site/                               # GitHub Pages content
│   ├── index.html · app.js · styles.css
│   └── data.json                       # (generated)
└── .github/workflows/refresh.yml       # daily cron + manual trigger
```

## Maintaining the tracked-services list

`data/brownfield.json` is committed source data. To add, remove, or fix a row:

1. Open `data/brownfield.json` in a PR.
2. Each entry has four fields: `service`, `armNamespace`, `specFolder`, `sdkPackageName`. For a service that has multiple `@azure/arm-*` packages (e.g. `Microsoft.KubernetesConfiguration`), add **one row per package**.
3. Merge — the next scheduled run picks it up automatically.

You never need to edit the generated outputs.

## Triggering a refresh manually

- GitHub UI → **Actions** → **Refresh ARM SDK Tracker** → **Run workflow**.
- Tick `rebuild_index` if the package index might be stale (e.g. a new package landed in either upstream repo this week).

## Running locally

```bash
# install python deps
pip install -r scripts/requirements.txt

# need a token to avoid rate limits (gh CLI works)
export GITHUB_TOKEN=$(gh auth token)

# rebuild the package index (slowish, ~1 minute)
python scripts/build_index.py

# refresh the dashboard data (~30 seconds)
python scripts/refresh.py

# preview the dashboard
python -m http.server -d site 8000
open http://localhost:8000
```

## Initial setup

To stand the repo up on GitHub:

1. Create `JialinHuang803/arm-refresh-tracker` (public).
2. `git remote add origin https://github.com/JialinHuang803/arm-refresh-tracker.git`
3. `git push -u origin main`
4. In the repo's **Settings → Pages**, set the source to **GitHub Actions**.
5. **Actions → Refresh ARM SDK Tracker → Run workflow** to seed the first deployment.

## Column definitions

See [`docs/columns.md`](docs/columns.md).
