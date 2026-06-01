"""
Layer 1 generator: published catalog (bibliographic spine).

Source of truth for the spine is the DataCite DOI registry, seeded from the authoritative
NaNDA list in ../usage-metrics/inventory.csv (status == published). Each known DOI is
enriched from DataCite; no discovery by prefix.

Field sourcing (decided at the recon gate):
  - Title, Authors, DataCite URL, publicationYear  <- DataCite
  - Version, Version Date                           <- seed inventory.csv (precise)
  - DOI, Study ID, Archive, URL                     <- seed
A `version_check` column compares seed vs DataCite version for ICPSR-style DOIs (the
seed-freshness backstop); openICPSR E-style DOIs report V0 at the base and are not
comparable, so the seed stays authoritative there.

The Topic Folder / Geographies / Date Range / Unique ID(s) / smallest-geo columns are
LEFT EMPTY here — they are Layer 2's job, joined in reconcile.py.

Output: data/published_catalog.csv  (+ stdout summary). Read-only HTTP GET against DataCite.
"""

from __future__ import annotations

import csv
import re
import sys
import time
from pathlib import Path

import requests

REPO = Path(__file__).resolve().parent
SEED_CSV = REPO.parent / "usage-metrics" / "inventory.csv"
OUT_CSV = REPO / "data" / "published_catalog.csv"
API = "https://api.datacite.org/dois/"

TITLE_PREFIXES = (
    "National Neighborhood Data Archive (NaNDA): ",
    "National Neighborhood Data Archive: ",
)

OUT_COLUMNS = [
    "study_id", "archive", "deposit_via", "title", "version", "version_date",
    "version_date_flag", "doi", "url", "authors", "publication_year",
    "datacite_version", "version_check", "datacite_status", "datacite_url",
    # Layer 2 fills these in reconcile.py:
    "topic_folder", "geographies", "date_range", "unique_ids", "smallest_geo",
]


def strip_title_prefix(title: str) -> str:
    t = (title or "").strip()
    for pre in TITLE_PREFIXES:
        if t.startswith(pre):
            return t[len(pre):].strip()
    return t


def strip_doi_version(doi: str) -> str:
    """Drop a trailing .vN / VN version segment (DataCite mints at major-version)."""
    return re.sub(r"[.]?[vV]\d+$", "", doi)


def norm_version(v: str) -> int | None:
    """Normalize a version label to its integer major component. 'V5.0'->5, 'v2'->2."""
    if not v:
        return None
    m = re.search(r"(\d+)", str(v))
    return int(m.group(1)) if m else None


def normalize_author(creator: dict) -> str:
    given = (creator.get("givenName") or "").strip()
    family = (creator.get("familyName") or "").strip()
    if given and family:
        return f"{given} {family}"
    name = (creator.get("name") or "").strip()
    if "," in name:  # "Last, First" -> "First Last"
        last, first = (p.strip() for p in name.split(",", 1))
        return f"{first} {last}".strip()
    return name


def fetch_datacite(doi: str) -> dict:
    """Return {status, attributes|None}. status: resolved | resolved-via-base | UNRESOLVED."""
    def _get(d: str):
        return requests.get(API + requests.utils.quote(d, safe=""),
                            headers={"Accept": "application/vnd.api+json"}, timeout=30)
    try:
        resp = _get(doi)
        if resp.status_code == 200:
            return {"status": "resolved", "attributes": resp.json()["data"]["attributes"]}
        base = strip_doi_version(doi)
        if base != doi:
            resp2 = _get(base)
            if resp2.status_code == 200:
                return {"status": "resolved-via-base",
                        "attributes": resp2.json()["data"]["attributes"]}
        return {"status": "UNRESOLVED", "attributes": None,
                "http": resp.status_code}
    except requests.RequestException as exc:
        return {"status": "UNRESOLVED", "attributes": None, "error": str(exc)}


def build_row(seed: dict) -> dict:
    doi = seed["doi"].strip()
    dc = fetch_datacite(doi)
    attr = dc.get("attributes") or {}

    titles = attr.get("titles") or []
    dc_title = strip_title_prefix(titles[0]["title"]) if titles else ""
    creators = attr.get("creators") or []
    authors = "; ".join(a for a in (normalize_author(c) for c in creators) if a)
    dc_version = attr.get("version") or ""

    seed_version = seed.get("version", "").strip()
    seed_vd = seed.get("version_date", "").strip()

    # version_check (seed-freshness backstop).
    is_icpsr_style = "/ICPSR" in doi.upper().replace("/E", "/E")  # ICPSR vs E-style
    is_icpsr_style = bool(re.search(r"/ICPSR", doi, re.IGNORECASE))
    if not dc.get("attributes"):
        version_check = "datacite-unresolved"
    elif is_icpsr_style:
        sv, dv = norm_version(seed_version), norm_version(dc_version)
        version_check = "match" if sv is not None and sv == dv else f"MISMATCH seed={seed_version} datacite={dc_version}"
    else:
        version_check = f"E-style: datacite={dc_version or 'V0'} (seed authoritative)"

    return {
        "study_id": seed["study_id"].strip(),
        "archive": seed["archive"].strip(),
        "deposit_via": seed.get("deposit_via", "").strip(),
        "title": dc_title or strip_title_prefix(seed.get("title", "")),
        "version": seed_version,
        "version_date": seed_vd,
        "version_date_flag": "BLANK_IN_SEED" if not seed_vd else "",
        "doi": doi,
        "url": seed.get("url", "").strip(),
        "authors": authors,
        "publication_year": attr.get("publicationYear", "") or "",
        "datacite_version": dc_version,
        "version_check": version_check,
        "datacite_status": dc["status"],
        "datacite_url": attr.get("url", "") or "",
        "topic_folder": "", "geographies": "", "date_range": "",
        "unique_ids": "", "smallest_geo": "",
    }


def main() -> int:
    if not SEED_CSV.exists():
        print(f"FATAL: seed not found at {SEED_CSV}", file=sys.stderr)
        return 2
    OUT_CSV.parent.mkdir(parents=True, exist_ok=True)

    with SEED_CSV.open("r", encoding="utf-8-sig", newline="") as fh:
        seeds = [r for r in csv.DictReader(fh) if r.get("status", "").strip() == "published"]

    print(f"Published rows in seed: {len(seeds)}. Querying DataCite...\n")
    rows = []
    for i, seed in enumerate(seeds, 1):
        row = build_row(seed)
        rows.append(row)
        print(f"  [{i:>2}/{len(seeds)}] {row['study_id']:>7}  {row['datacite_status']:<17} "
              f"v={row['version'] or '?':<6} {row['version_check']}")
        time.sleep(1)  # polite

    with OUT_CSV.open("w", encoding="utf-8", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=OUT_COLUMNS)
        w.writeheader()
        w.writerows(rows)

    # Summary
    unresolved = [r for r in rows if r["datacite_status"] == "UNRESOLVED"]
    via_base = [r for r in rows if r["datacite_status"] == "resolved-via-base"]
    mismatches = [r for r in rows if r["version_check"].startswith("MISMATCH")]
    blank_dates = [r for r in rows if r["version_date_flag"]]
    no_authors = [r for r in rows if not r["authors"]]

    print("\n" + "=" * 70)
    print("LAYER 1 — PUBLISHED CATALOG")
    print("=" * 70)
    print(f"Rows written            : {len(rows)}  ->  {OUT_CSV}")
    print(f"Resolved directly       : {sum(1 for r in rows if r['datacite_status']=='resolved')}")
    print(f"Resolved via base DOI   : {len(via_base)}  (openICPSR E-style)")
    print(f"UNRESOLVED              : {len(unresolved)}")
    for r in unresolved:
        print(f"      {r['study_id']}  {r['doi']}")
    print(f"Version seed-vs-DataCite MISMATCH (ICPSR-style): {len(mismatches)}")
    for r in mismatches:
        print(f"      {r['study_id']}  {r['version_check']}")
    print(f"Blank version_date in seed (flagged): {len(blank_dates)}")
    for r in blank_dates:
        print(f"      {r['study_id']}  {r['doi']}")
    print(f"Rows with no authors from DataCite  : {len(no_authors)}")
    for r in no_authors:
        print(f"      {r['study_id']}  ({r['datacite_status']})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
