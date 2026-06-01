# Recon spike — findings note

**Phase 1 gate deliverable.** Run before building any generator. Two probes:
(A) dictionary-file format census across `O:\NaNDA\Data`, and (B) a DataCite
field-coverage probe for sample NaNDA DOIs. Raw artifacts in `recon/out/`
(`dict_files.csv`, `dict_census.json`, `datacite_probe.json`).

Generated: 2026-06-01. Read-only against O: and the DataCite API.

---

## Bottom line (what the gate needs to decide)

1. **Layer 2 must parse `.xlsx`, not just `.csv`.** The brief's premise — read the
   dictionary *CSV* exports, no binary parsing — holds for only **4 of 90** published
   dataset subfolders. **83 published subfolders have *only* an `.xlsx` dictionary.**
   The CSV-only export is a recent (RDE-era) convention; the entire back-catalog is xlsx.
   This is a real change to the Layer 2 plan, not a detail. openpyxl already reads them
   fine (the census did exactly this), so the cost is low — but the parser spec changes.

2. **The xlsx dictionaries come in ~37 header layouts.** The parser must normalize
   variable-name detection across `Variable` / `variable` / `Variable Name` /
   `ZCTA10 Variable`, tolerate merged-cell artifacts (duplicate `Variable`/`Label`
   columns, trailing blank columns), and not assume a fixed column order.

3. **DataCite version/date coverage depends on DOI style** (details in Part B). ICPSR-style
   DOIs return a granular version; openICPSR `E…` DOIs only resolve at the *base* DOI and
   report `version: "V0"`. Issued dates are **year-only and missing for 2 of 5** sampled.
   → Take **version + version_date from the seed `inventory.csv`** (precise), use DataCite
   for **title, creators, url**. Flag rows where the seed `version_date` is blank.

4. **176 dataset subfolders → 90 published → ~50 catalog DOIs.** Each topic folder holds
   several superseded internal versions. Layer 2 must select the *current* subfolder per
   topic; the reconciliation join can't naively pair all 90 against 50 DOIs.

None of the above is built yet — these are decisions for you before I write the generators.

---

## Part A — Dictionary-file format census

Source: `recon/dict_census.py` → `recon/out/dict_census.json`, `dict_files.csv`.

| Metric | Value |
|---|---|
| Topic folders scanned (excl. `crosswalks`, `z_archive`, `_templates`, `ZIPtoZCTA`) | 50 |
| Dataset subfolders | 176 |
| Dictionary files found | 543 |
| By extension | **`.xlsx` 466**, `.csv` 77 |
| By filename pattern | `*_data_dictionary.xlsx` 401 · `*_dictionary.csv` 71 · `*_dictionary.xlsx` 65 · `*_data_dictionary.csv` 6 |
| Variable-name column header | `Variable` 414 · `variable` 121 · `Variable Name` 5 · *(undetected)* 3 |
| Distinct publish-stage header layouts | **37** |

### The CSV-only assumption breaks at scale

Of the **90 published subfolders** (those containing a `_P.dta`):

- **4** have a CSV dictionary.
- **83** have **only** an `.xlsx` dictionary.
- **3** have **no** publish-stage dictionary at all:
  `arts_entertainment_recreation\artsentrec_tract_2006-2015`,
  `pollution\polluting_sites_tract_2000-2018`,
  `religious_civic_social_orgs\relcivsoc_1990-2021`.

The newest RDE datasets (parks 2024, training_vocation, arts/entertainment 1990-2022)
do export `*_dictionary.csv` into `documentation\`. Everything older is `*_data_dictionary.xlsx`.

### Header-shape variety (parser must normalize)

The common shape is `Variable | Type | Obs | Unique | Mean | Min | Max | Label`
(134 + 118 files across the two case variants). But the long tail is messy. Examples the
parser must survive:

- `Variable | Type | Label` (110 files — no stats columns; fine, we only need names).
- `ZCTA10 Variable | Type | Obs | …` (variable column not literally named "Variable").
- `Variable | name | Obs | …` (urbanicity) and `Drop | Variable | Type | …` (turnover dicts).
- Merged-cell artifacts: `Variable | Type | Label | Variable | Obs | … | Label` (hospitals),
  trailing empty columns (`… | Label |  |  |  | `).
- Stat-column synonyms: `Obs` / `Observations` / `Obs.` / `Not-Null Count` / `Non-Zero Count`;
  `Label` / `Variable Label` / `Variable Description` / `variable label`.

**Implication for derivation:** Layer 2 needs only the **variable-name list** per dataset to
derive geographies (`tract_fips10/20/22`, `zcta…`, `county_fips`, `blckgrp`…), unique ID
(the geo identifier var, incl. Connecticut's `tract_fips22`), and date range (`year` min/max,
else the folder's `_[daterange]` token). So a robust variable-column detector + a geo-variable
lookup is enough; we don't need to interpret the stat columns. That keeps the 37-layout
problem tractable.

### Subfolder multiplicity

`parks/` alone has 5 dataset subfolders (`parks_tract_2018`, `parks_zcta_2018`,
`parks_tract20_2022`, `parks_zcta20_2022`, `parks_tractzcta_2024`). Across all topics that's
176 subfolders, 90 with a published `_P.dta`. Only the latest maps to the current catalog DOI.
Note also that the `_P` convention isn't universal: `parks_tract20_2022` / `parks_zcta20_2022`
ship `…_01.dta` with no `P`, so a "has published .dta" rule can't rely on `_P` alone.

### Parse failures (listed, not skipped — per brief verification)

After excluding `~$…` Office lock files, **1 genuinely unreadable file**:

- `street_connectivity\street_connectivity_tract_2020\documentation\nanda_stconnect_tract_2020_01_data_dictionary.xlsx`
  → `BadZipFile: File is not a zip file` (corrupt or mis-saved). This is its *only* dictionary,
  so that subfolder effectively has no usable dict. **A data-quality issue on O: worth a fix.**

---

## Part B — DataCite field-coverage probe

Source: `recon/datacite_probe.py` → `recon/out/datacite_probe.json`. Five DOIs spanning the
format/route variety. **All 5 resolved** (one behavior caveat below).

### Resolution behavior differs by DOI style

| DOI (seed) | Direct GET | Resolved how | DataCite `version` | Issued date |
|---|---|---|---|---|
| `10.3886/ICPSR38586.v2` (parks) | 200 | versioned DOI | `v2` ✓ | `2022` (year only) |
| `10.3886/ICPSR200038.V5` (libraries, RDE) | 200 | versioned DOI | `V5` ✓ | **none** |
| `10.3886/ICPSR302937.v1` (broadband 2025, RDE) | 200 | versioned DOI | `V1` ✓ | **none** |
| `10.3886/E209163V30` (arts/ent) | **404** | version-stripped base | `V0` (base) | `2024` (year only) |
| `10.3886/E141121V36` (redlining) | **404** | version-stripped base | `V0` (base) | `2023` (year only) |

- **ICPSR-style DOIs** (`…/ICPSR…`) resolve at the versioned DOI and return the real version.
- **openICPSR `E…` DOIs** 404 at the versioned form; they resolve only at the base
  (`10.3886/E209163`), where `version` is `"V0"` — i.e. DataCite does **not** carry the
  granular `V30`/`V36`. For these, derive version from the seed DOI suffix (the
  `strip_doi_version` logic), exactly as the brief's fallback anticipated.

### Field coverage

- **title** — present, clean, matches the seed for all 5.
- **creators** — present for all (3–9 each); these populate the Authors column. **Name format
  is inconsistent** across records (`Melendez, Robert` vs `Robert Melendez`) — normalize on write.
- **version** — present but only reliable for ICPSR-style DOIs; `V0` for E-style base records.
- **version date** — DataCite `Issued` is **year-only** and **absent for 2 of 5**. It is *not*
  a substitute for the seed's precise `version_date` (e.g. `11/29/2023`).
- **publicationYear** — present, but reflects latest registration (often 2026), not original
  release. Not a version-date source.

### Recommended field sourcing for Layer 1

| Field | Source | Notes |
|---|---|---|
| Title | DataCite | strip the `"National Neighborhood Data Archive (NaNDA): "` prefix to match downstream, as usage-metrics does |
| Authors | DataCite `creators` | normalize "Last, First" → "First Last" |
| Version | **seed DOI suffix** / seed `version` | DataCite unreliable for E-style |
| Version date | **seed `version_date`** | precise; flag blank (e.g. 302343, 302937) rather than guess |
| DOI / URL | seed + DataCite `url` | DataCite `url` points at the live ICPSR/NaNDA study page |
| publicationYear | DataCite | informational only |

**Resolver note for the full Layer 1 run:** query the versioned DOI first; on 404, retry the
version-stripped base. With that two-step, all 5 samples resolved — I expect the ~50-row seed
to resolve fully, but I'll report any that don't when I build Layer 1.

---

## Brief verification checklist — status at the gate

- ✅ **Recon reported before any generator built.** (This note. No Layer 1/2/reconcile code exists yet.)
- ✅ **Seed column structure confirmed.** `inventory.csv` holds `study_id` and `doi`; 50 rows
  `status=published`, 55 `unpublished`. The brief's "45 after dedupe" is lower than 50 published —
  the gap is duplicate tract/zcta and hospital rows that collapse by topic, plus the v03 oracle's
  45 hand-filled IDs. **To reconcile during the join, not now.**
- ✅ **Dict-CSV format census reported** (Part A) — counts of filename patterns, header variants,
  variable-name column variants.
- ✅ **DataCite field probe reported** (Part B) — which fields resolve, and the version/date caveat.
- ⏳ **Unique-ID diff vs v03's 45 values** — deferred to the Layer 2 build (oracle located at
  `O:\NaNDA\Data\nanda_inventory_v03_2026-04-07_1641.xlsx`).
- ⏳ **Drift-flag spot-checks** (v03 `Unmapped Topics`, e.g. `LGBTQ`, `NHPD`) — deferred to reconcile.
  Note: `LGBTQ` and `NHPD` *do* appear as O: topic folders, so they will surface as
  "O: files but no catalog entry."
- ✅ **Parser ran clean across every variant** — 0 crashes; the 1 unparseable file is listed by path.

---

## Open questions before building the generators

1. **xlsx parsing is now in-scope for Layer 2** — confirmed approach? (openpyxl, read-only,
   variable-column auto-detect + geo-variable lookup table.) This is the one substantive
   departure from the brief.
2. **Current-subfolder selection per topic** — pick the latest dataset subfolder by
   folder-name daterange / mtime, or drive it from the catalog DOI's date range? I lean toward
   matching on the catalog's geography+daterange so the join stays DOI-keyed.
3. **The 3 published subfolders with no publish-stage dict** and **the 1 corrupt xlsx** — derive
   geo/date from the `.dta` (the one place we'd read binary) or from the folder name + a
   validate-stage dict in `code\`, or just flag them as "no dictionary on disk"?
4. **Field-sourcing split** (version/date from seed, title/authors from DataCite) — OK as above?
