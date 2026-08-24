"""
Phase 2, final step: derive the public seed file for usage-metrics.

Reads deposit_status.csv + data/published_catalog.csv (post-Layer-1) and rewrites
../usage-metrics/inventory.csv (same path, same 9-column schema:
study_id, archive, deposit_via, status, title, version, version_date, doi, url).

Status vocabulary mapping (the two files track different axes):
  nanda-inventory `status` (dataset identity)     -> usage-metrics `status` (publication)
  ------------------------------------------------------------------------------------
  current            -> published   (live, downloadable — track usage)
  alternate-deposit  -> published   (live, downloadable — track usage)
  blank/needs-review -> published   (a Done! deposit with a resolving DOI IS live;
                                     the pending review is about dataset IDENTITY —
                                     current vs alternate vs superseded — not about
                                     whether it is published)
  superseded         -> DROPPED from inventory.csv entirely

Everything already in inventory.csv is preserved verbatim (including all `unpublished`
rows — deposit_status.csv only tracks published deposits, so those rows have no
counterpart here and must pass through untouched). This script only:
  1. drops rows whose study_id is `superseded` in deposit_status.csv, and
  2. appends rows tracked in deposit_status.csv but missing from inventory.csv
     (i.e. new DPM-sourced deposits), built to the same conventions as
     usage-metrics/add_to_inventory.py: full NaNDA-prefixed title, V-normalized
     version, M/D/YYYY version_date from DataCite `created`, sites-form landing URL,
     and the same strict validation gates. A row that fails to fetch or validate is
     skipped and logged — it stays pending in deposit_status.csv and is retried on
     the next run.

Metadata for new rows comes from one DataCite GET per row (using the DOI Layer 1
already confirmed resolves, recorded in published_catalog.csv `resolve_doi_used`).

Run AFTER layer1_catalog.py. Writes a markdown summary to $GITHUB_STEP_SUMMARY when set.
"""

from __future__ import annotations

import csv
import os
import re
import sys
from pathlib import Path

import requests

REPO = Path(__file__).resolve().parent
DEPOSIT_CSV = REPO / "deposit_status.csv"
CATALOG_CSV = REPO / "data" / "published_catalog.csv"
INVENTORY_CSV = REPO.parent / "usage-metrics" / "inventory.csv"

INVENTORY_COLUMNS = ["study_id", "archive", "deposit_via", "status", "title",
                     "version", "version_date", "doi", "url"]

DATACITE_URL = "https://api.datacite.org/dois/{doi}"
LANDING_URL = "https://www.icpsr.umich.edu/sites/icpsr/view/studies/{id}"
HTTP_TIMEOUT = 30

# Validation gates — mirror usage-metrics/add_to_inventory.py exactly. Kept as a copy
# (not a cross-repo import) so this script's deps stay requests-only; if those gates
# change over there, update these to match.
TITLE_PREFIX = "National Neighborhood Data Archive (NaNDA):"
VERSION_RE = re.compile(r"^V\d+(\.\d+)?$")
DATE_RE = re.compile(r"^\d{1,2}/\d{1,2}/\d{4}$")
URL_PREFIXES = ("https://www.icpsr.umich.edu/", "https://www.openicpsr.org/")


def iso_to_slash_date(iso_date: str) -> str:
    """'2024-10-14T14:03:22.000Z' -> '10/14/2024' (same as add_to_inventory.py)."""
    m = re.match(r"^(\d{4})-(\d{1,2})-(\d{1,2})", iso_date or "")
    if not m:
        return iso_date or ""
    return f"{int(m.group(2))}/{int(m.group(3))}/{m.group(1)}"


def normalize_version(version: str) -> str:
    """'v1' -> 'V1', '1.0' -> 'V1.0' (same as add_to_inventory.py)."""
    v = (version or "").strip()
    if not v:
        return ""
    return "V" + (v[1:] if v[0] in "vV" else v)


def fetch_datacite(doi: str) -> dict | None:
    r = requests.get(DATACITE_URL.format(doi=doi), timeout=HTTP_TIMEOUT,
                     headers={"Accept": "application/vnd.api+json"})
    if r.status_code == 404:
        return None
    r.raise_for_status()
    return r.json().get("data", {}).get("attributes", {})


def validate_row(row: dict) -> list[str]:
    errs = []
    if not (row["title"] or "").startswith(TITLE_PREFIX):
        errs.append(f"title lacks NaNDA prefix: {row['title'][:70]!r}")
    if row["archive"] not in {"ICPSR", "openICPSR"}:
        errs.append(f"bad archive {row['archive']!r}")
    if row["deposit_via"] not in {"legacy", "RDE"}:
        errs.append(f"bad deposit_via {row['deposit_via']!r}")
    if not VERSION_RE.match(row["version"] or ""):
        errs.append(f"bad version {row['version']!r}")
    if not DATE_RE.match(row["version_date"] or ""):
        errs.append(f"bad version_date {row['version_date']!r}")
    if not (row["doi"] or "").startswith("10.3886/"):
        errs.append(f"bad doi {row['doi']!r}")
    if not any((row["url"] or "").startswith(p) for p in URL_PREFIXES):
        errs.append(f"bad url {row['url']!r}")
    return errs


def build_new_row(dep: dict, cat: dict) -> tuple[dict | None, str]:
    """Build a 9-column inventory row for a deposit not yet in inventory.csv.
    Returns (row, "") on success or (None, reason) on skip."""
    sid = dep["study_id"].strip()
    fetch_doi = (cat.get("resolve_doi_used") or dep["resolve_doi_for_datacite"]).strip()
    try:
        attrs = fetch_datacite(fetch_doi)
    except requests.RequestException as exc:
        return None, f"{sid}: DataCite fetch failed ({exc})"
    if not attrs:
        return None, f"{sid}: DOI {fetch_doi} did not resolve at DataCite"

    titles = attrs.get("titles") or []
    title = titles[0].get("title", "") if titles else ""
    row = {
        "study_id": sid,
        "archive": dep.get("archive", "").strip(),
        "deposit_via": dep.get("deposit_via", "").strip() or "RDE",
        "status": "published",
        "title": title,
        "version": normalize_version(attrs.get("version") or ""),
        "version_date": iso_to_slash_date(attrs.get("created") or ""),
        "doi": dep.get("seed_doi", "").strip(),
        "url": LANDING_URL.format(id=sid),
    }
    errs = validate_row(row)
    if errs:
        return None, f"{sid}: validation failed ({'; '.join(errs)})"
    return row, ""


def summary_out(lines: list[str]) -> None:
    text = "\n".join(lines)
    print(text)
    path = os.environ.get("GITHUB_STEP_SUMMARY")
    if path:
        with open(path, "a", encoding="utf-8") as fh:
            fh.write("## Public catalog export -> usage-metrics/inventory.csv\n\n"
                     + text + "\n")


def main() -> int:
    for p in (DEPOSIT_CSV, CATALOG_CSV, INVENTORY_CSV):
        if not p.exists():
            print(f"FATAL: {p} missing. Run the pipeline steps in order.", file=sys.stderr)
            return 2

    with DEPOSIT_CSV.open("r", encoding="utf-8-sig", newline="") as fh:
        deposits = list(csv.DictReader(fh))
    with CATALOG_CSV.open("r", encoding="utf-8-sig", newline="") as fh:
        catalog = {r["study_id"].strip(): r for r in csv.DictReader(fh)}
    with INVENTORY_CSV.open("r", encoding="utf-8-sig", newline="") as fh:
        inventory = list(csv.DictReader(fh))
    inv_ids = {r["study_id"].strip() for r in inventory}

    superseded = {d["study_id"].strip() for d in deposits
                  if d.get("status", "").strip() == "superseded"}

    kept, dropped = [], []
    for r in inventory:
        if r["study_id"].strip() in superseded:
            dropped.append(r["study_id"].strip())
        else:
            kept.append(r)

    added, skipped = [], []
    for dep in deposits:
        sid = dep["study_id"].strip()
        if sid in inv_ids or sid in superseded:
            continue
        row, reason = build_new_row(dep, catalog.get(sid, {}))
        if row is None:
            skipped.append(reason)
        else:
            added.append(row)

    changed = bool(dropped or added)
    if changed:
        out = kept + added
        with INVENTORY_CSV.open("w", encoding="utf-8", newline="") as fh:
            w = csv.DictWriter(fh, fieldnames=INVENTORY_COLUMNS, lineterminator="\n")
            w.writeheader()
            w.writerows(out)

    lines = [f"Existing inventory rows kept verbatim: {len(kept)}"]
    if dropped:
        lines.append(f"**Dropped (superseded in deposit_status.csv): "
                     f"{', '.join(dropped)}**")
    if added:
        lines.append(f"**Appended new rows: {len(added)}**")
        for r in added:
            lines.append(f"  - {r['study_id']}  {r['doi']}  {r['title'][:60]}")
    if skipped:
        lines.append(f"**Rows pending, not exported (need attention): {len(skipped)}**")
        for s in skipped:
            lines.append(f"  - {s}")
    if not changed:
        lines.append("No changes — inventory.csv left untouched.")
    summary_out(lines)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
