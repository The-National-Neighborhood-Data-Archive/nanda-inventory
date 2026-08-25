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
    # Cluster folders confirmed (the alternate/superseded/co-canonical relationships are
    # already captured in status + related_to_doi; the folder itself is unambiguous).
    "38598": "land_cover", "220701": "land_cover", "110663": "land_cover",
    "39378": "hospitals", "222901": "hospitals",
    "127681": "education_training", "127682": "education_training",
    "120088": "crosswalks",
}
# Deposits with no topic_folder. "NO O: FILES" rows are RESOLVED (expected catalog-only
# entries).
TOPIC_REVIEW = {
    "141121": "NO O: FILES - published to NaNDA as an external dataset (no local curation files)",
    "190141": "NO O: FILES - special release built on Dropbox from merged NaNDA data (one-off)",
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


def load_existing_rows() -> list[dict]:
    """Full existing deposit_status.csv rows (for curation preservation AND for
    carrying forward rows not yet present in the seed — see main())."""
    if not OUT_CSV.exists():
        return []
    with OUT_CSV.open("r", encoding="utf-8-sig", newline="") as fh:
        return list(csv.DictReader(fh))


def load_existing_curation() -> dict[str, dict]:
    """Preserve hand-edited curatorial columns across re-runs, keyed by study_id."""
    return {
        r["study_id"]: {"status": r.get("status", ""),
                        "related_to_doi": r.get("related_to_doi", ""),
                        "topic_folder": r.get("topic_folder", ""),
                        "topic_review": r.get("topic_review", ""),
                        "note": r.get("note", "")}
        for r in load_existing_rows()
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
        prev = (ex.get("status") or "").strip()
        if prev == "":
            # Pending review from an earlier run (e.g. a curation-pipeline-sourced row). Blank stays
            # blank across reruns — NEVER auto-promoted to `current`; only a human sets
            # a status. (Before this guard, a blank row silently became `current` on
            # the next rerun via DEFAULT_STATUS.)
            status, related = NEEDS_REVIEW, ex.get("related_to_doi", "").strip()
            note = ex.get("note", "").strip() or "NEW DEPOSIT - needs review"
        else:
            # Legacy "published" placeholder from before the curatorial model.
            status, related = DEFAULT_STATUS, ex.get("related_to_doi", "").strip()
            note = ex.get("note", "").strip()
    else:
        status, related, note = NEEDS_REVIEW, "", "NEW DEPOSIT - needs review"

    # topic_folder is curatorial (preserve hand edit, else draft from TOPIC).
    # topic_review is a DERIVED hint that refreshes each run (not hand-curated) — it only
    # shows while topic_folder is blank.
    topic = (ex.get("topic_folder") or "").strip() or TOPIC.get(study_id, "")
    topic_review = "" if topic else TOPIC_REVIEW.get(study_id, "needs topic_folder review")

    return {"status": status, "related_to_doi": related, "note": note,
            "topic_folder": topic, "topic_review": topic_review}


def main() -> int:
    existing_rows = load_existing_rows()
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

    # Carry forward rows that exist in deposit_status.csv but not (yet) in the seed —
    # e.g. rows appended by pull_curation_pipeline_completed.py before they reach usage-metrics'
    # inventory.csv, or rows dropped from the public export (superseded). The row set
    # is seed UNION existing; nothing tracked here is ever silently dropped.
    seed_ids = {r["study_id"] for r in rows}
    carried = [dict(r) for r in existing_rows if r["study_id"].strip() not in seed_ids]
    rows.extend(carried)

    with OUT_CSV.open("w", encoding="utf-8", newline="") as fh:
        # "\n" explicitly: this now also runs on Linux CI, where csv's default "\r\n"
        # would fight the repo's LF storage (locally autocrlf hides the difference).
        w = csv.DictWriter(fh, fieldnames=COLUMNS, lineterminator="\n")
        w.writeheader()
        w.writerows(rows)

    from collections import Counter
    icpsr = sum(1 for r in rows if is_icpsr_form(r["seed_doi"]) and r["study_id"] not in TWIN_OVERRIDE)
    eform = sum(1 for r in rows if not is_icpsr_form(r["seed_doi"]) and r["study_id"] not in TWIN_OVERRIDE)
    twins = [r for r in rows if r["study_id"] in TWIN_OVERRIDE]
    related = [r for r in rows if r["related_to_doi"]]
    by_status = Counter((r["status"] or "(needs review)") for r in rows)

    print(f"Wrote {len(rows)} deposit rows -> {OUT_CSV}")
    if carried:
        print(f"  carried forward (not in seed)  : {len(carried)}  "
              f"({', '.join(r['study_id'] for r in carried)})")
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
