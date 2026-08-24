# nanda-inventory

## Status: active (Phase 1 complete; Phase 2 DPM sync built)

Phase 1 (the three-layer master inventory) is finished and working. The first
slice of Phase 2 — the automated DPM-sheet-to-usage-metrics sync — was added
2026-08-24 and runs daily via GitHub Actions (see "Phase 2 — DPM pipeline sync"
below). Layers 2+3 remain local-only.

**Phase 1 built** a reconciliation of NaNDA's dataset inventory across four
sources: the published ICPSR/openICPSR catalog, the actual contents of
`O:\NaNDA\Data\`, the drift between those two, and an oracle diff identifying
where they disagree. The inventory documents every deposit rather than
deduplicating them, because a single dataset may have multiple legitimate
deposits across versions and geographies.

**Phase 2 would have added** a pipeline layer connecting the inventory to the
data publication workflow. Deferred, not scoped out.

### Important: `deposit_status.csv`

`deposit_status.csv` is hand-curated and is the canonical record of dataset
identity. It encodes human judgments about which deposits represent the same
underlying dataset — judgments that cannot be derived automatically from the
catalog or the file system. **It must never be overwritten by a pipeline
rerun.** Any future work needs to preserve it as an input, not regenerate it
as an output.

### If you're picking this up

Start by running the Phase 1 reconciliation and comparing its output to the
last committed results, to confirm the ICPSR catalog structure hasn't changed
underneath it.

A locally-run generator that produces an always-fresh **master inventory** of NaNDA's
published datasets. Nothing in the output is hand-maintained — every cell is scraped,
scanned, or derived, so no tab can drift from its source because no tab *is* the source.

## Three-layer model

| Layer | Name | Source of truth | Phase |
|-------|------|-----------------|-------|
| 1 | Published catalog (bibliographic spine) | DataCite DOI registry (API), seeded from the usage-metrics `inventory.csv` | **Phase 1** |
| 2 | O-drive file reality (internal mapping) | The `O:` drive itself, scanned each run (dictionary exports in each `documentation\`) | **Phase 1** |
| 3 | Pipeline / in-development | DPM Workflows tracker | Phase 2 (separate brief) |

**Layers 1–2 are Phase 1 (built). The DPM-driven pipeline sync (Layer 3's first
slice) is built** — see "Phase 2 — DPM pipeline sync" below. Still future, separate
briefs: Published-trigger fan-out, folder-stamping at intake, dissemination tracking.

## Layout

```
recon/                 # Recon spike — run first, before any generator (Phase 1, step 2)
  dict_census.py       # Dictionary-file format census across O:\NaNDA\Data
  datacite_probe.py    # DataCite field-coverage probe for sample NaNDA DOIs
  out/                 # Generated census/probe artifacts (committed, diffable)
  RECON_FINDINGS.md    # Short findings note (the gate deliverable)
requirements.txt
```

## Pipeline (Phase 1 — built)

```
recon/                 ->  format census + DataCite probe  (gate: reported before building)
build_deposit_status.py->  deposit_status.csv              (canonical curated control file)
layer1_catalog.py      ->  data/published_catalog.csv      (DataCite enrichment per DOI)
layer2_odrive.py       ->  data/odrive_reality.csv         (O: scan + dict parse + derive)
reconcile.py           ->  data/published_catalog_joined.csv, drift.csv, oracle_diff.csv
                           master_inventory.xlsx           (Published | O-Drive Reality |
                                                            Reconciliation Drift | Oracle Diff)
```

### deposit_status.csv — the one curated input

Dataset identity is a human judgment call, so the row set lives in `deposit_status.csv`, the
single hand-curated control file (everything else is derived from ICPSR/DataCite or the O:
drive). `build_deposit_status.py` regenerates the mechanical columns (seed-derived fields,
`resolve_doi_for_datacite`) but **preserves** the curatorial ones — `status`
(`current` / `alternate-deposit` / `superseded`), `related_to_doi`, `topic_folder`, `note` —
across re-runs, never clobbering hand edits. New deposits surfacing from the seed arrive with
a blank status (needs review), never silently `current`.

## Inputs

- **Seed DOI list** — `../usage-metrics/inventory.csv` (authoritative NaNDA list).
  Columns confirmed: `study_id, archive, deposit_via, status, title, version,
  version_date, doi, url`. DataCite is queried *per known DOI*, never by the `10.3886`
  prefix (that prefix is all of ICPSR, not just NaNDA).
- **O: drive** — `O:\NaNDA\Data\[topic]\[shortname]_[daterange]\{code,datasets,documentation}\`.
  Published `.dta` files are the `_P` versions; dictionary exports live in `documentation\`.
- **Validation oracle** — `O:\NaNDA\Data\nanda_inventory_v03_2026-04-07_1641.xlsx`
  (`Studies` tab, 45 hand-filled Unique IDs). Used to diff derived values **only** —
  never seeded from.
- **Schema reference (Published tab)** — `../_working/stanford-afc/nanda_inventory_dates_fixed_2026-05-27.xlsx`.
  Schema shape only; the DUA-constraint and individual-linkable columns are dropped.

## Run

```powershell
pip install -r requirements.txt

# Recon (already run; re-run only to refresh the format census / DataCite probe)
python recon/dict_census.py
python recon/datacite_probe.py

# Full pipeline, in order
python build_deposit_status.py     # regenerate control file (preserves curation)
python layer1_catalog.py           # DataCite enrichment  -> data/published_catalog.csv (~1 min)
python layer2_odrive.py            # O: scan + derive      -> data/odrive_reality.csv
python reconcile.py                # join + drift + oracle -> master_inventory.xlsx
```

Layers 2+3 (`layer2_odrive.py`, `reconcile.py`, `master_inventory.xlsx`) are
**local-only** — they need the `O:` drive, which a hosted runner can't reach. Commit
each local run so every refresh is a diffable commit.

## Phase 2 — DPM pipeline sync (GitHub Actions, daily)

When a curator marks a dataset `Done!` in the DPM Workflows sheet's `Completed` tab,
`.github/workflows/dpm-sync.yml` (daily 06:17 UTC + manual `workflow_dispatch`) runs:

```
pull_dpm_completed.py    ->  appends NEW deposits to deposit_status.csv
                             (blank status = needs Lindsay's review; never auto-`current`)
build_deposit_status.py  ->  regenerates mechanical columns, preserves all curation,
                             carries forward rows not yet in the seed
layer1_catalog.py        ->  DataCite enrichment -> data/published_catalog.csv
export_public_catalog.py ->  rewrites ../usage-metrics/inventory.csv (the public seed)
```

The workflow commits `deposit_status.csv` + `data/published_catalog.csv` here, then
pushes the exported `inventory.csv` to the `usage-metrics` repo. Both pushes are
verified against the remote before the job reports success. Check the run's summary
page — new deposits and skipped rows needing manual attention are listed there.

Status mapping into `usage-metrics` (different axes — identity vs. publication):
`current`, `alternate-deposit`, and blank/needs-review all export as `published`
(a Done! deposit with a resolving DOI is live; the pending review concerns dataset
*identity*, not publication). `superseded` rows are dropped from `inventory.csv`
entirely. `unpublished` rows already in `inventory.csv` pass through untouched.
`add_to_inventory.ps1` over in usage-metrics stays available as the manual escape
hatch for edge cases.

### One-time setup (Lindsay)

Two credentials, both created manually, both stored as repository secrets in
**this** repo (Settings → Secrets and variables → Actions):

1. **`GOOGLE_SHEETS_CREDENTIALS`** — read access to the DPM sheet.
   - In Google Cloud Console, create (or reuse) a service account
     (no roles needed; it only reads a sheet shared with it).
   - Create a JSON key for it and download the file.
   - Share the DPM Workflows sheet with the service account's email
     (`...@...iam.gserviceaccount.com`) as **Viewer**.
   - Paste the entire JSON file's contents as the secret value.
   - For local runs instead: save the file as `credentials/service_account.json`
     (the `credentials/` folder is gitignored).

2. **`USAGE_METRICS_PUSH_TOKEN`** — lets the workflow push to usage-metrics.
   - GitHub → Settings → Developer settings → Fine-grained personal access tokens.
   - Repository access: **only** `usage-metrics`. Permissions: **Contents: Read
     and write**. Pick a ~1-year expiry and note the renewal date.
   - Paste the token as the secret value.

Until both secrets exist, the workflow fails cleanly at the credential step —
nothing is written anywhere.
