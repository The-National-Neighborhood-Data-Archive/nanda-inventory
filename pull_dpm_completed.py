"""
Phase 2, step 0: pull newly-completed datasets from the DPM Workflows sheet.

Reads the `Completed` tab of the DPM curation pipeline sheet (Google Sheets API,
service-account auth, read-only), filters to rows a curator marked `Done!`, and appends
any deposit not already tracked in deposit_status.csv as a NEW row with a blank `status`
("needs review" — never auto-set to `current`), matching the curatorial convention
documented in build_deposit_status.py.

Only two sheet columns are consumed: `ICPSR Study #` and `doi` (plus `Dataset Name` for
log readability). Everything else is derived:
  - archive        <- DOI form: 10.3886/ICPSR... -> ICPSR ; 10.3886/E... -> openICPSR
  - deposit_via    <- always RDE (every new deposit goes through RDE; `legacy` is
                      backfill-only, per usage-metrics add_to_inventory.py)
  - status / related_to_doi / topic_folder  <- blank (curatorial review)

Messy cells are skipped and logged, never guessed at:
  - Study # like "38528/237313" (two IDs in one cell) -> skipped, listed for Lindsay
  - rows with neither a Study # nor a DOI -> counted, not an error (nothing to look up)
  - rows with one but not the other -> skipped, listed for Lindsay

Credentials (read-only scope): either
  - env GOOGLE_SHEETS_CREDENTIALS holding the service-account JSON itself
    (this is how GitHub Actions supplies it, from a repo secret), or
  - credentials/service_account.json (gitignored; local runs).
Setup steps for Lindsay are in README.md.

The live sheet's header row is read and mapped by NAME at runtime — if a required header
disappears (sheet restructure), this exits with an error listing what it found instead
of guessing at positions.

Output: appends to deposit_status.csv (never modifies existing rows). Also writes a
markdown run summary to $GITHUB_STEP_SUMMARY when set (the Actions run-summary page is
the main way Lindsay notices something needs her review).
"""

from __future__ import annotations

import csv
import json
import os
import re
import sys
from pathlib import Path

import requests

REPO = Path(__file__).resolve().parent
DEPOSIT_CSV = REPO / "deposit_status.csv"
CREDENTIALS_FILE = REPO / "credentials" / "service_account.json"

SPREADSHEET_ID = "1cl6cJBBy4wbfTA0JYF9VF1K-nRuqi4HQZWg3k8Q6Yek"
TAB_NAME = "Completed"
SHEETS_SCOPE = "https://www.googleapis.com/auth/spreadsheets.readonly"
VALUES_URL = ("https://sheets.googleapis.com/v4/spreadsheets/"
              f"{SPREADSHEET_ID}/values/{TAB_NAME}")

DONE_VALUE = "done!"          # matched case-insensitively, trimmed
REQUIRED_HEADERS = ["icpsr study #", "status", "doi"]   # matched case-insensitively
NAME_HEADER = "dataset name"  # optional, for logging only

DEPOSIT_COLUMNS = ["study_id", "archive", "deposit_via", "status", "seed_doi",
                   "resolve_doi_for_datacite", "related_to_doi", "topic_folder",
                   "topic_review", "note"]

NEW_ROW_NOTE = "NEW DEPOSIT from DPM Completed tab - needs review"


# --- Sheets access ------------------------------------------------------------------

def load_credentials():
    """Service-account credentials from env (CI) or the gitignored local file."""
    from google.oauth2 import service_account  # deferred: clearer error below if absent

    raw = os.environ.get("GOOGLE_SHEETS_CREDENTIALS", "").strip()
    if raw:
        info = json.loads(raw)
        return service_account.Credentials.from_service_account_info(
            info, scopes=[SHEETS_SCOPE])
    if CREDENTIALS_FILE.exists():
        return service_account.Credentials.from_service_account_file(
            str(CREDENTIALS_FILE), scopes=[SHEETS_SCOPE])
    raise FileNotFoundError(
        "No Google Sheets credential found. Set GOOGLE_SHEETS_CREDENTIALS (service-"
        f"account JSON) or place the key at {CREDENTIALS_FILE}. Setup: see README.md.")


def fetch_completed_tab() -> list[list[str]]:
    """Return the Completed tab as a list of rows (list of cell strings)."""
    creds = load_credentials()
    from google.auth.transport.requests import Request as GoogleRequest
    creds.refresh(GoogleRequest())
    r = requests.get(VALUES_URL,
                     headers={"Authorization": f"Bearer {creds.token}"},
                     timeout=30)
    r.raise_for_status()
    return r.json().get("values", [])


# --- Normalization ------------------------------------------------------------------

def normalize_study_id(raw: str) -> str | None:
    """'ICPSR 38580' -> '38580'; '38528/237313' -> None (can't resolve to one ID)."""
    s = (raw or "").strip()
    s = re.sub(r"^\s*icpsr\s*", "", s, flags=re.IGNORECASE)
    s = s.strip()
    if re.fullmatch(r"\d{4,7}", s):
        return s
    return None


def normalize_doi(raw: str) -> str | None:
    """Strip URL/'doi:' wrappers; must be a 10.3886/... DOI to be usable."""
    s = (raw or "").strip()
    s = re.sub(r"^https?://(dx\.)?doi\.org/", "", s, flags=re.IGNORECASE)
    s = re.sub(r"^doi:\s*", "", s, flags=re.IGNORECASE)
    s = s.strip().rstrip("/")
    if s.startswith("10.3886/"):
        return s
    return None


def is_icpsr_form(doi: str) -> bool:
    return bool(re.search(r"/ICPSR\d+", doi, re.IGNORECASE))


def is_eform(doi: str) -> bool:
    return bool(re.search(r"/E\d+", doi, re.IGNORECASE))


def strip_doi_version(doi: str) -> str:
    return re.sub(r"[.]?[vV]\d+$", "", doi)


def derive_archive(doi: str) -> str | None:
    """DOI form fully determines the archive (same rule Layer 1 relies on)."""
    if is_icpsr_form(doi):
        return "ICPSR"
    if is_eform(doi):
        return "openICPSR"
    return None


def resolve_doi_for(doi: str) -> str:
    """Match build_deposit_status.resolve_doi_for: ICPSR-form as-is, E-form -> base.
    (No TWIN_OVERRIDE here — Layer 1's runtime twin fallback covers any new E-form
    whose base 404s.)"""
    if is_icpsr_form(doi):
        return doi
    return strip_doi_version(doi)


# --- Main ---------------------------------------------------------------------------

def summary_out(lines: list[str]) -> None:
    text = "\n".join(lines)
    print(text)
    path = os.environ.get("GITHUB_STEP_SUMMARY")
    if path:
        with open(path, "a", encoding="utf-8") as fh:
            fh.write("## DPM Completed-tab pull\n\n" + text + "\n")


def main() -> int:
    if not DEPOSIT_CSV.exists():
        print(f"FATAL: {DEPOSIT_CSV} missing — refusing to bootstrap it from the sheet. "
              "It is the hand-curated control file.", file=sys.stderr)
        return 2

    try:
        values = fetch_completed_tab()
    except Exception as exc:
        print(f"FATAL: could not read the DPM sheet: {exc}", file=sys.stderr)
        return 2
    if not values:
        print("FATAL: Completed tab returned no rows at all.", file=sys.stderr)
        return 2

    # Map columns by header NAME, from the live sheet — never by position.
    header = [h.strip().lower() for h in values[0]]
    col = {}
    missing = []
    for want in REQUIRED_HEADERS + [NAME_HEADER]:
        if want in header:
            col[want] = header.index(want)
        elif want in REQUIRED_HEADERS:
            missing.append(want)
    if missing:
        print("FATAL: Completed tab header row has drifted from what this script "
              f"expects. Missing: {missing}. Found: {values[0]}. "
              "Ask Lindsay before changing the mapping.", file=sys.stderr)
        return 2

    def cell(row: list[str], key: str) -> str:
        i = col.get(key)
        return row[i].strip() if i is not None and i < len(row) else ""

    with DEPOSIT_CSV.open("r", encoding="utf-8-sig", newline="") as fh:
        existing_rows = list(csv.DictReader(fh))
    known_ids = {r["study_id"].strip() for r in existing_rows}

    added, skipped, empty_count, already = [], [], 0, []
    for row in values[1:]:
        status = cell(row, "status").lower()
        if status != DONE_VALUE:
            continue
        raw_id = cell(row, "icpsr study #")
        raw_doi = cell(row, "doi")
        name = cell(row, NAME_HEADER) or "(unnamed)"

        if not raw_id and not raw_doi:
            empty_count += 1          # nothing to look up yet — expected, not an error
            continue

        sid = normalize_study_id(raw_id)
        doi = normalize_doi(raw_doi)
        if sid is None:
            skipped.append(f"{name}: unresolvable Study # {raw_id!r}")
            continue
        if sid in known_ids:
            already.append(sid)   # checked BEFORE the DOI gate: a tracked row with a
            continue              # messy sheet DOI needs no attention — it's done
        if doi is None:
            skipped.append(f"{name} ({sid}): missing/unusable DOI {raw_doi!r}")
            continue
        archive = derive_archive(doi)
        if archive is None:
            skipped.append(f"{name} ({sid}): DOI {doi!r} is neither ICPSR- nor E-form")
            continue

        added.append({
            "study_id": sid,
            "archive": archive,
            "deposit_via": "RDE",
            "status": "",                       # blank = needs curatorial review
            "seed_doi": doi,
            "resolve_doi_for_datacite": resolve_doi_for(doi),
            "related_to_doi": "",
            "topic_folder": "",
            "topic_review": "needs topic_folder review",
            "note": NEW_ROW_NOTE,
        })
        known_ids.add(sid)

    if added:
        with DEPOSIT_CSV.open("a", encoding="utf-8", newline="") as fh:
            w = csv.DictWriter(fh, fieldnames=DEPOSIT_COLUMNS, lineterminator="\n")
            w.writerows(added)

    lines = [f"Done! rows already tracked : {len(already)}",
             f"Done! rows with no ID/DOI  : {empty_count} (nothing to look up yet)"]
    if added:
        lines.append(f"**NEW deposits appended to deposit_status.csv "
                     f"(blank status — need Lindsay's review): {len(added)}**")
        for r in added:
            lines.append(f"  - {r['study_id']}  {r['archive']:<9} {r['seed_doi']}")
    else:
        lines.append("No new deposits found.")
    if skipped:
        lines.append(f"**Skipped rows needing manual attention: {len(skipped)}**")
        for s in skipped:
            lines.append(f"  - {s}")
    summary_out(lines)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
