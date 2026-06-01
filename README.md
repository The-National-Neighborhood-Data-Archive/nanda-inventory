# nanda-inventory

A locally-run generator that produces an always-fresh **master inventory** of NaNDA's
published datasets. Nothing in the output is hand-maintained — every cell is scraped,
scanned, or derived, so no tab can drift from its source because no tab *is* the source.

## Three-layer model

| Layer | Name | Source of truth | Phase |
|-------|------|-----------------|-------|
| 1 | Published catalog (bibliographic spine) | DataCite DOI registry (API), seeded from the usage-metrics `inventory.csv` | **Phase 1** |
| 2 | O-drive file reality (internal mapping) | The `O:` drive itself, scanned each run (dictionary exports in each `documentation\`) | **Phase 1** |
| 3 | Pipeline / in-development | DPM Workflows tracker | Phase 2 (separate brief) |

**This repo currently covers Phase 1 only (Layers 1–2).** Phase 2 — the DPM-driven
pipeline layer, Published-trigger fan-out, folder-stamping at intake, and dissemination
tracking — is a later, separate brief. Do not build any of that here.

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

Phase 1 is **local-only** — no GitHub Actions, no remote. Commit each run so every
refresh is a diffable commit.
