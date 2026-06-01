"""
Generate deposit_status.csv — the canonical row set + status source for Layer 1.

The catalog model changed: we no longer dedup. All published deposits stay as their own
row; relationships between them are expressed via `related_to_doi` rather than collapsed.
deposit_status.csv is the control file Layer 1 reads instead of raw inventory.csv.

This generator bootstraps deposit_status.csv from the seed inventory.csv (status==published)
and computes the DOI to hand DataCite. It is RE-RUNNABLE: the two curatorial columns
(`related_to_doi`, `note`) are PRESERVED from any existing deposit_status.csv so hand
edits are never clobbered — only the deterministic columns are recomputed.

resolve_doi_for_datacite — the value Layer 1 starts the fallback chain from:
  - ICPSR-form DOI (.../ICPSR<id>...)      -> used as-is (already versioned, resolves directly)
  - openICPSR E-form DOI (.../E<id>V<n>)   -> version-stripped base (DataCite mints at base)
  - verified twin exceptions               -> the ICPSR-form twin that actually resolves
Layer 1 keeps a runtime fallback (base -> ICPSR{study_id} twin) for any future E-form whose
base 404s, so the chain is robust even if a row isn't pre-flagged here.

Columns: study_id, archive, deposit_via, status, seed_doi, resolve_doi_for_datacite,
         related_to_doi, note

Output: deposit_status.csv (repo root). Offline — no network.
"""

from __future__ import annotations

import csv
import re
from pathlib import Path

REPO = Path(__file__).resolve().parent
SEED_CSV = REPO.parent / "usage-metrics" / "inventory.csv"
OUT_CSV = REPO / "deposit_status.csv"

# Verified 2026-06-01: these two RDE deposits carry a seed E-form DOI that 404s at DataCite
# (versioned AND base), but resolve cleanly under the ICPSR-form twin. Their published DOI
# is the ICPSR form; the seed inventory.csv still lists the openICPSR draft E-form.
TWIN_OVERRIDE = {"301419", "302178"}

COLUMNS = ["study_id", "archive", "deposit_via", "status", "seed_doi",
           "resolve_doi_for_datacite", "related_to_doi", "note"]

# --- Curatorial baseline (the human-judgment layer; can't be derived from ICPSR or O:) ----
# `status` vocabulary (the three-value model): current | alternate-deposit | superseded.
# A blank status means "needs review" — applied to deposits that surface from the seed after
# bootstrap, so dataset identity stays a human call rather than a silent "current".
#
# These five non-current classifications fell out of the dedup worklist (not title-matching):
#   same date range as the ICPSR twin  -> alternate-deposit
#   older vintage replaced by the twin -> superseded
# related_to_doi points at the current canonical deposit's DOI (matchable in the catalog).
BASELINE = {
    # openICPSR draft E-DOI for an RDE study whose published DOI is the ICPSR-form twin.
    "301419": ("current", "10.3886/ICPSR301419.v1",
               "seed E-form DOI 404s at DataCite; published twin is ICPSR-form"),
    "302178": ("current", "10.3886/ICPSR302178.v1",
               "seed E-form DOI 404s at DataCite; published twin is ICPSR-form"),
    # alternate openICPSR deposits of a current ICPSR study (same coverage / date range).
    "222901": ("alternate-deposit", "10.3886/ICPSR39378.v1",
               "alternate openICPSR deposit of ICPSR39378 (Hospitals, 2023)"),
    "220701": ("alternate-deposit", "10.3886/ICPSR38598.v2",
               "alternate openICPSR deposit of ICPSR38598 (Land Cover, 1985-2023)"),
    # older vintage superseded by a current ICPSR study.
    "110663": ("superseded", "10.3886/ICPSR38598.v2",
               "superseded by ICPSR38598 (Land Cover); older 2001-2016 vintage"),
}
DEFAULT_STATUS = "current"   # known bootstrap deposits not in BASELINE
NEEDS_REVIEW = ""            # blank -> needs review, for deposits surfacing after bootstrap
# Legacy placeholder values that predate the curatorial model; treat as uncurated.
UNCURATED = {"", "published"}


def strip_doi_version(doi: str) -> str:
    return re.sub(r"[.]?[vV]\d+$", "", doi)


def major_version(v: str) -> int:
    m = re.search(r"(\d+)", v or "")
    return int(m.group(1)) if m else 1


def is_icpsr_form(doi: str) -> bool:
    return bool(re.search(r"/ICPSR\d+", doi, re.IGNORECASE))


def resolve_doi_for(study_id: str, doi: str, version: str) -> str:
    if study_id in TWIN_OVERRIDE:
        return f"10.3886/ICPSR{study_id}.v{major_version(version)}"
    if is_icpsr_form(doi):
        return doi
    return strip_doi_version(doi)  # E-form -> base


def load_existing_curation() -> dict[str, dict]:
    """Preserve hand-edited curatorial columns across re-runs, keyed by study_id."""
    if not OUT_CSV.exists():
        return {}
    with OUT_CSV.open("r", encoding="utf-8-sig", newline="") as fh:
        return {
            r["study_id"]: {"status": r.get("status", ""),
                            "related_to_doi": r.get("related_to_doi", ""),
                            "note": r.get("note", "")}
            for r in csv.DictReader(fh)
        }


def curate(study_id: str, existing: dict[str, dict]) -> tuple[str, str, str]:
    """Resolve the curatorial (status, related_to_doi, note) for a deposit.

    Precedence: a prior human-curated row wins (never clobbered); else the bootstrap
    BASELINE; else `current` for deposits known at bootstrap; else blank (needs review)
    for deposits surfacing from the seed after bootstrap.
    """
    ex = existing.get(study_id)
    if ex is not None and (ex.get("status") or "").strip() not in UNCURATED:
        return ex["status"].strip(), ex.get("related_to_doi", "").strip(), ex.get("note", "").strip()
    if study_id in BASELINE:
        return BASELINE[study_id]
    if ex is not None:  # known at bootstrap, no special classification
        return DEFAULT_STATUS, ex.get("related_to_doi", "").strip(), ex.get("note", "").strip()
    return NEEDS_REVIEW, "", "NEW DEPOSIT - needs review"


def main() -> int:
    curated = load_existing_curation()

    with SEED_CSV.open("r", encoding="utf-8-sig", newline="") as fh:
        seeds = [r for r in csv.DictReader(fh) if r.get("status", "").strip() == "published"]

    rows = []
    for s in seeds:
        sid = s["study_id"].strip()
        seed_doi = s["doi"].strip()
        resolve = resolve_doi_for(sid, seed_doi, s.get("version", ""))
        status, related, note = curate(sid, curated)
        rows.append({
            "study_id": sid,
            "archive": s["archive"].strip(),
            "deposit_via": s.get("deposit_via", "").strip(),
            "status": status,                       # curatorial role (not seed published flag)
            "seed_doi": seed_doi,
            "resolve_doi_for_datacite": resolve,
            "related_to_doi": related,
            "note": note,
        })

    with OUT_CSV.open("w", encoding="utf-8", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=COLUMNS)
        w.writeheader()
        w.writerows(rows)

    from collections import Counter
    icpsr = sum(1 for r in rows if is_icpsr_form(r["seed_doi"]) and r["study_id"] not in TWIN_OVERRIDE)
    eform = sum(1 for r in rows if not is_icpsr_form(r["seed_doi"]) and r["study_id"] not in TWIN_OVERRIDE)
    twins = [r for r in rows if r["study_id"] in TWIN_OVERRIDE]
    related = [r for r in rows if r["related_to_doi"]]
    by_status = Counter((r["status"] or "(needs review)") for r in rows)

    print(f"Wrote {len(rows)} deposit rows -> {OUT_CSV}")
    print(f"  resolve as-is (ICPSR-form)     : {icpsr}")
    print(f"  resolve via base (E-form)      : {eform}")
    print(f"  twin override (ICPSR-form twin): {len(twins)}  "
          f"({', '.join(r['study_id'] for r in twins)})")
    print(f"  status breakdown               : {dict(by_status)}")
    print(f"  rows with related_to_doi ({len(related)}):")
    for r in related:
        print(f"      {r['study_id']:>7}  [{r['status']}]  -> {r['related_to_doi']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
