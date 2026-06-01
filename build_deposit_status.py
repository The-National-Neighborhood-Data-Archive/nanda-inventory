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
    """Preserve hand-edited related_to_doi / note across re-runs, keyed by study_id."""
    if not OUT_CSV.exists():
        return {}
    with OUT_CSV.open("r", encoding="utf-8-sig", newline="") as fh:
        return {
            r["study_id"]: {"related_to_doi": r.get("related_to_doi", ""),
                            "note": r.get("note", "")}
            for r in csv.DictReader(fh)
        }


def main() -> int:
    curated = load_existing_curation()

    with SEED_CSV.open("r", encoding="utf-8-sig", newline="") as fh:
        seeds = [r for r in csv.DictReader(fh) if r.get("status", "").strip() == "published"]

    rows = []
    for s in seeds:
        sid = s["study_id"].strip()
        seed_doi = s["doi"].strip()
        resolve = resolve_doi_for(sid, seed_doi, s.get("version", ""))

        # Default curatorial values: seed the two verified twins; otherwise blank.
        if sid in TWIN_OVERRIDE:
            default_related = resolve  # the resolving ICPSR-form twin
            default_note = "seed E-form DOI 404s at DataCite; published twin is ICPSR-form"
        else:
            default_related, default_note = "", ""

        prev = curated.get(sid, {})
        rows.append({
            "study_id": sid,
            "archive": s["archive"].strip(),
            "deposit_via": s.get("deposit_via", "").strip(),
            "status": s.get("status", "").strip(),
            "seed_doi": seed_doi,
            "resolve_doi_for_datacite": resolve,
            # Preserve hand edits if present, else fall back to the computed default.
            "related_to_doi": prev.get("related_to_doi") or default_related,
            "note": prev.get("note") or default_note,
        })

    with OUT_CSV.open("w", encoding="utf-8", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=COLUMNS)
        w.writeheader()
        w.writerows(rows)

    icpsr = sum(1 for r in rows if is_icpsr_form(r["seed_doi"]) and r["study_id"] not in TWIN_OVERRIDE)
    eform = sum(1 for r in rows if not is_icpsr_form(r["seed_doi"]) and r["study_id"] not in TWIN_OVERRIDE)
    twins = [r for r in rows if r["study_id"] in TWIN_OVERRIDE]
    related = [r for r in rows if r["related_to_doi"]]

    print(f"Wrote {len(rows)} deposit rows -> {OUT_CSV}")
    print(f"  resolve as-is (ICPSR-form)     : {icpsr}")
    print(f"  resolve via base (E-form)      : {eform}")
    print(f"  twin override (ICPSR-form twin): {len(twins)}")
    for r in twins:
        print(f"      {r['study_id']}  seed={r['seed_doi']}  ->  resolve={r['resolve_doi_for_datacite']}")
    print(f"  rows with related_to_doi set   : {len(related)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
