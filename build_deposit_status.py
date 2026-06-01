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
           "resolve_doi_for_datacite", "related_to_doi", "topic_folder",
           "topic_review", "note"]

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

# --- Curated topic_folder map (study_id -> O: topic folder under O:\NaNDA\Data) -----------
# The deterministic join key. Filled ONLY where the O: shortname is unambiguous. Many-to-one
# is expected (sibling deposits share a folder). Anything not here is left blank and listed in
# TOPIC_REVIEW for a human call — a wrong topic_folder would silently join to the wrong files.
TOPIC = {
    "200038": "libraries", "207966": "religious_civic_social_orgs",
    "208207": "social_services", "208366": "post_offices_banks",
    "208682": "retail_establishments", "208684": "law_enforcement",
    "208751": "eating_drinking", "208906": "personal_care_laundromats",
    "208907": "liquor_tobacco_convenience", "209050": "health_care_nets",
    "209163": "arts_entertainment_recreation", "209164": "recreation",
    "209313": "grocery_stores", "209324": "dollar_stores",
    "210581": "ADI_standardized", "222263": "opthamologists", "230941": "PRISM",
    "237305": "air_conditioning", "301419": "essential_businesses",
    "302178": "essential_workers", "302343": "training_vocation",
    "302937": "broadband", "305511": "parks", "38506": "voting",
    "38528": "ses_demographics", "38559": "broadband", "38567": "broadband",
    "38569": "schools", "38579": "schools", "38580": "street_connectivity",
    "38584": "traffic", "38585": "traffic", "38586": "parks", "38597": "pollution",
    "38605": "public_transit", "38606": "urbanicity", "38649": "crime",
    "38858": "weather", "38974": "essential_workers", "39093": "HMDA",
}
# Deposits deliberately left blank, with the reason a human needs to resolve.
TOPIC_REVIEW = {
    "38598":  "land_cover cluster (canonical) - confirm folder; shared w/ 220701, 110663",
    "220701": "alternate-deposit; land_cover cluster - confirm folder",
    "110663": "superseded; land_cover cluster - confirm folder + relationship",
    "39378":  "hospitals cluster (canonical) - confirm folder; shared w/ 222901",
    "222901": "alternate-deposit; hospitals cluster - confirm folder",
    "127681": "education_training co-canonical sibling (Tract) - confirm folder",
    "127682": "education_training co-canonical sibling (ZCTA) - confirm folder",
    "120088": "ZCTA merge CODE deposit, not a dataset - folder unclear (crosswalks? ZIPtoZCTA?)",
    "141121": "historic_redlining folder exists but Layer 2 found no dataset units - verify",
    "190141": "Alzheimer's special data release - O: folder not identified",
}


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
                            "topic_folder": r.get("topic_folder", ""),
                            "topic_review": r.get("topic_review", ""),
                            "note": r.get("note", "")}
            for r in csv.DictReader(fh)
        }


def curate(study_id: str, existing: dict[str, dict]) -> dict:
    """Resolve all curatorial fields for a deposit.

    Precedence: a prior human-curated value wins (never clobbered); else the bootstrap
    baseline; else a sensible default. topic_folder is preserved if hand-filled; otherwise
    drafted from TOPIC, and left blank with a TOPIC_REVIEW reason where ambiguous.
    """
    ex = existing.get(study_id) or {}
    # status / related_to_doi / note
    if (ex.get("status") or "").strip() not in UNCURATED:
        status = ex["status"].strip()
        related = ex.get("related_to_doi", "").strip()
        note = ex.get("note", "").strip()
    elif study_id in BASELINE:
        status, related, note = BASELINE[study_id]
    elif existing.get(study_id) is not None:
        status, related = DEFAULT_STATUS, ex.get("related_to_doi", "").strip()
        note = ex.get("note", "").strip()
    else:
        status, related, note = NEEDS_REVIEW, "", "NEW DEPOSIT - needs review"

    # topic_folder (preserve hand edit, else draft); topic_review reason only while blank
    topic = (ex.get("topic_folder") or "").strip() or TOPIC.get(study_id, "")
    if topic:
        topic_review = ""
    else:
        topic_review = (ex.get("topic_review") or "").strip() or \
            TOPIC_REVIEW.get(study_id, "needs topic_folder review")

    return {"status": status, "related_to_doi": related, "note": note,
            "topic_folder": topic, "topic_review": topic_review}


def main() -> int:
    curated = load_existing_curation()

    with SEED_CSV.open("r", encoding="utf-8-sig", newline="") as fh:
        seeds = [r for r in csv.DictReader(fh) if r.get("status", "").strip() == "published"]

    rows = []
    for s in seeds:
        sid = s["study_id"].strip()
        seed_doi = s["doi"].strip()
        resolve = resolve_doi_for(sid, seed_doi, s.get("version", ""))
        cur = curate(sid, curated)
        rows.append({
            "study_id": sid,
            "archive": s["archive"].strip(),
            "deposit_via": s.get("deposit_via", "").strip(),
            "status": cur["status"],                # curatorial role (not seed published flag)
            "seed_doi": seed_doi,
            "resolve_doi_for_datacite": resolve,
            "related_to_doi": cur["related_to_doi"],
            "topic_folder": cur["topic_folder"],
            "topic_review": cur["topic_review"],
            "note": cur["note"],
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

    topic_filled = [r for r in rows if r["topic_folder"]]
    topic_blank = [r for r in rows if not r["topic_folder"]]
    print(f"  topic_folder filled            : {len(topic_filled)} / {len(rows)}")
    print(f"  topic_folder BLANK (review)    : {len(topic_blank)}")
    for r in topic_blank:
        print(f"      {r['study_id']:>7}  {r['topic_review']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
