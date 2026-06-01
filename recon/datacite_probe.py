"""
Recon spike, Part B: DataCite field-coverage probe.

The brief flags a known-unknown: DataCite field coverage for NaNDA DOIs is unverified.
Confirm whether DataCite returns granular *version* and *version date* for a sample of
NaNDA DOIs. If version date isn't present, Layer 1 will derive version from the
version-DOI suffix (e.g. ...V19) and flag the date cell rather than guess.

Probes a spread of DOI shapes from the seed inventory:
  - ICPSR-style DOI    (10.3886/ICPSR38586.v2)
  - openICPSR E-style  (10.3886/E209163V30)
  - a brand-new RDE    (10.3886/ICPSR302937.v1)

Reads the seed list, picks samples, fetches each from the DataCite REST API, and reports
exactly which fields resolve. Writes recon/out/datacite_probe.json and prints a summary.
Read-only HTTP GET against api.datacite.org.
"""

from __future__ import annotations

import json
import re
import sys
import time
from pathlib import Path

import requests

SEED_CSV = Path(__file__).resolve().parents[2] / "usage-metrics" / "inventory.csv"
OUT_DIR = Path(__file__).resolve().parent / "out"
API = "https://api.datacite.org/dois/"

# DOIs to probe (chosen to span the format/route variety). Overridable via argv.
SAMPLE_DOIS = [
    "10.3886/ICPSR38586.v2",   # parks, ICPSR legacy, versioned suffix .v2
    "10.3886/E209163V30",      # arts/entertainment, openICPSR E-style, V30
    "10.3886/ICPSR200038.V5",  # libraries, RDE route, .V5
    "10.3886/ICPSR302937.v1",  # broadband 2025, newest RDE, .v1 (version_date blank in seed)
    "10.3886/E141121V36",      # historic redlining, non-"NaNDA"-titled
]


def strip_doi_version(doi: str) -> str:
    """Drop a trailing .vN / VN version segment (DataCite mints at major-version)."""
    return re.sub(r"[.]?[vV]\d+$", "", doi)


def probe(doi: str) -> dict:
    url = API + requests.utils.quote(doi, safe="")
    out = {"doi": doi, "http_status": None, "resolved": False}
    try:
        resp = requests.get(url, headers={"Accept": "application/vnd.api+json"}, timeout=30)
        out["http_status"] = resp.status_code
        if resp.status_code != 200:
            # Retry against the version-stripped DOI (DataCite may only hold the base).
            base = strip_doi_version(doi)
            if base != doi:
                out["retry_base_doi"] = base
                resp2 = requests.get(API + requests.utils.quote(base, safe=""),
                                     headers={"Accept": "application/vnd.api+json"}, timeout=30)
                out["retry_http_status"] = resp2.status_code
                if resp2.status_code == 200:
                    resp = resp2
                    out["resolved_via"] = "version-stripped base DOI"
                else:
                    return out
            else:
                return out
        data = resp.json().get("data", {})
        attr = data.get("attributes", {})
        out["resolved"] = True
        titles = attr.get("titles") or []
        creators = attr.get("creators") or []
        out["fields"] = {
            "title": (titles[0].get("title") if titles else None),
            "version": attr.get("version"),
            "publicationYear": attr.get("publicationYear"),
            "dates": attr.get("dates"),  # list of {date, dateType}
            "registered": attr.get("registered"),
            "updated": attr.get("updated"),
            "creators_count": len(creators),
            "creators_sample": [c.get("name") for c in creators[:3]],
            "url": attr.get("url"),
            "state": attr.get("state"),
        }
        # Surface which dateTypes are present (Issued / Updated / Available / ...).
        out["date_types_present"] = sorted(
            {d.get("dateType") for d in (attr.get("dates") or []) if d.get("dateType")}
        )
        out["has_version"] = attr.get("version") not in (None, "")
        out["has_issued_date"] = any(
            (d.get("dateType") == "Issued") for d in (attr.get("dates") or [])
        )
    except requests.RequestException as exc:
        out["error"] = f"{type(exc).__name__}: {exc}"
    return out


def main(argv: list[str]) -> int:
    dois = argv[1:] or SAMPLE_DOIS
    OUT_DIR.mkdir(parents=True, exist_ok=True)

    results = []
    for doi in dois:
        print(f"Probing {doi} ...")
        results.append(probe(doi))
        time.sleep(1)  # polite

    (OUT_DIR / "datacite_probe.json").write_text(
        json.dumps({"seed_csv": str(SEED_CSV), "results": results}, indent=2),
        encoding="utf-8",
    )

    print("\n" + "=" * 70)
    print("DATACITE FIELD-COVERAGE PROBE")
    print("=" * 70)
    for r in results:
        print(f"\nDOI: {r['doi']}  (HTTP {r.get('http_status')}, resolved={r['resolved']})")
        if r.get("resolved_via"):
            print(f"  resolved via: {r['resolved_via']}")
        if not r["resolved"]:
            print(f"  !! did not resolve  {r.get('error', '')}")
            continue
        f = r["fields"]
        print(f"  title          : {f['title']}")
        print(f"  version        : {f['version']!r}   (has_version={r['has_version']})")
        print(f"  publicationYear: {f['publicationYear']}")
        print(f"  date_types     : {r['date_types_present']}   (has Issued={r['has_issued_date']})")
        print(f"  dates          : {f['dates']}")
        print(f"  creators       : {f['creators_count']}  e.g. {f['creators_sample']}")
        print(f"  url            : {f['url']}")

    resolved = [r for r in results if r["resolved"]]
    print("\n" + "-" * 70)
    print(f"Resolved {len(resolved)}/{len(results)}.")
    print(f"  with granular version field : {sum(1 for r in resolved if r.get('has_version'))}")
    print(f"  with an Issued date         : {sum(1 for r in resolved if r.get('has_issued_date'))}")
    print(f"\nArtifact written to {OUT_DIR / 'datacite_probe.json'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
