"""
Reconciliation join (Phase 1, step 5) + output assembly.

Joins Layer 1 (published catalog) and Layer 2 (O-drive reality) on the curated topic_folder
(from deposit_status.csv), selects the matching O: subfolder per catalog deposit by geography
+ date range (with the _P tiebreaker), fills the Published spine, and flags drift. Also diffs
the derived values against the v03 oracle's hand-filled Studies tab.

Selection rule (decided with Lindsay):
  - restrict candidates to units in the deposit's topic_folder
  - score by date-range overlap, then geography overlap
  - prefer the _P version; a single match is used regardless of _P;
    multiple equally-good matches with no _P -> aggregate + flag MULTI_SUBFOLDER

Outputs (data/ + master_inventory.xlsx):
  published_catalog_joined.csv   Published tab: catalog + topic_folder + derived O: fields
  drift.csv                      reconciliation / drift findings
  oracle_diff.csv                derived vs v03 oracle (topic_folder, geos, date, unique IDs)
  master_inventory.xlsx          Published | O-Drive Reality | Drift | Oracle Diff
"""

from __future__ import annotations

import csv
import re
from collections import defaultdict
from pathlib import Path

import openpyxl
import pandas as pd

REPO = Path(__file__).resolve().parent
DATA = REPO / "data"
DEPOSIT_CSV = REPO / "deposit_status.csv"
CATALOG_CSV = DATA / "published_catalog.csv"
ODRIVE_CSV = DATA / "odrive_reality.csv"
ORACLE_XLSX = Path(r"O:\NaNDA\Data\nanda_inventory_v03_2026-04-07_1641.xlsx")

YEAR_RE = re.compile(r"(?:19|20)\d{2}")
GEO_RANK = {"Block Group": 1, "Census Tract": 2, "ZCTA": 3, "ZIP": 3,
            "School District": 4, "County": 5, "State": 6}


def year_span(text: str):
    yrs = [int(y) for y in YEAR_RE.findall(text or "")]
    return (min(yrs), max(yrs)) if yrs else None


def date_score(cat_span, unit_span) -> int:
    if not cat_span or not unit_span:
        return 0
    if cat_span == unit_span:
        return 3
    if cat_span[0] == unit_span[0] or cat_span[1] == unit_span[1]:
        return 2
    lo, hi = max(cat_span[0], unit_span[0]), min(cat_span[1], unit_span[1])
    return 1 if lo <= hi else 0


def geos_from_title(title: str) -> set:
    s = (title or "").lower()
    g = set()
    if "block group" in s:
        g.add("Block Group")
    if "tract" in s:
        g.add("Census Tract")
    if "zip code tabulation" in s or "zcta" in s:
        g.add("ZCTA")
    if "school district" in s:
        g.add("School District")
    if re.search(r"\bcounty\b", s):
        g.add("County")
    return g


def geos_in_unit(unit_geos: str) -> set:
    return {lbl for lbl in GEO_RANK if lbl in (unit_geos or "")}


def tokenize_uid(s: str) -> set:
    return {t for t in re.split(r"[;,\s]+", (s or "").lower()) if t}


# --------------------------------------------------------------------------------------------
def load_csv(path: Path) -> list[dict]:
    with path.open("r", encoding="utf-8-sig", newline="") as fh:
        return list(csv.DictReader(fh))


def load_oracle() -> dict[str, dict]:
    wb = openpyxl.load_workbook(ORACLE_XLSX, read_only=True, data_only=True)
    ws = wb["Studies"]
    rows = list(ws.iter_rows(values_only=True))
    wb.close()
    header = [str(c).strip() if c else "" for c in rows[0]]
    idx = {h: i for i, h in enumerate(header)}
    out = {}
    for r in rows[1:]:
        sid = str(r[idx["Study ID"]]).strip() if r[idx["Study ID"]] else ""
        if not sid:
            continue
        out[sid] = {
            "topic_folder": str(r[idx["Topic Folder(s)"]] or "").strip(),
            "latest_subfolder": str(r[idx["Latest Subfolder"]] or "").strip(),
            "geographies": str(r[idx["Geographies"]] or "").strip(),
            "date_range": str(r[idx["Date Range"]] or "").strip(),
            "unique_ids": str(r[idx["Unique ID(s)"]] or "").strip(),
        }
    return out


def select_unit(deposit: dict, units: list[dict]):
    """Return (selected_units, flag). selected_units may be >1 (aggregate)."""
    if not units:
        return [], "NO_O_UNIT_IN_FOLDER"
    cat_span = year_span(deposit["title"])
    cat_geos = geos_from_title(deposit["title"])
    scored = []
    for u in units:
        ds = date_score(cat_span, year_span(u["date_range"]))
        gs = len(cat_geos & geos_in_unit(u["geographies"]))
        scored.append((ds, gs, u))
    best = max((ds * 10 + gs) for ds, gs, _ in scored)
    if best == 0:
        # No overlap at all — fall back to all units in folder, flagged.
        return units, "WEAK_MATCH"
    winners = [u for ds, gs, u in scored if ds * 10 + gs == best]
    p_winners = [u for u in winners if u["has_P_dta"]]
    if p_winners:
        winners = p_winners
    if len(winners) == 1:
        return winners, ""
    return winners, "MULTI_SUBFOLDER"


def agg(units: list[dict], field: str) -> str:
    seen, out = set(), []
    for u in units:
        for tok in re.split(r"; ", u.get(field, "")):
            tok = tok.strip()
            if tok and tok not in seen:
                seen.add(tok)
                out.append(tok)
    return "; ".join(out)


def smallest_of(units: list[dict]) -> str:
    labels = [u["smallest_geo"] for u in units if u["smallest_geo"]]
    return min(labels, key=lambda g: GEO_RANK.get(g, 9)) if labels else ""


def main() -> int:
    catalog = {r["study_id"]: r for r in load_csv(CATALOG_CSV)}
    deposits = {r["study_id"]: r for r in load_csv(DEPOSIT_CSV)}
    odrive = load_csv(ODRIVE_CSV)
    oracle = load_oracle()

    units_by_topic = defaultdict(list)
    for u in odrive:
        units_by_topic[u["topic_folder"]].append(u)

    selected_subfolders = defaultdict(set)  # topic -> set of selected subfolders
    published, drift = [], []

    for sid, dep in deposits.items():
        cat = catalog.get(sid, {})
        topic = dep.get("topic_folder", "").strip()
        row = {
            "study_id": sid, "status": dep.get("status", ""),
            "title": cat.get("title", ""), "version": cat.get("version", ""),
            "version_date": cat.get("version_date", ""), "seed_doi": dep.get("seed_doi", ""),
            "resolve_doi_used": cat.get("resolve_doi_used", ""),
            "related_to_doi": dep.get("related_to_doi", ""), "url": cat.get("url", ""),
            "authors": cat.get("authors", ""), "topic_folder": topic,
            "subfolder": "", "geographies": "", "date_range": "", "unique_ids": "",
            "smallest_geo": "", "match_flag": "",
        }
        if not topic:
            row["match_flag"] = "NO_O_FILES"
            drift.append({"type": "catalog_no_o_files", "study_id": sid,
                          "topic_folder": "", "detail": dep.get("topic_review", "")})
            published.append(row)
            continue

        units = units_by_topic.get(topic, [])
        chosen, flag = select_unit({"title": cat.get("title", "")}, units)
        for u in chosen:
            selected_subfolders[topic].add(u["subfolder"])
        row["subfolder"] = "; ".join(u["subfolder"] for u in chosen)
        row["geographies"] = agg(chosen, "geographies")
        row["date_range"] = agg(chosen, "date_range")
        row["unique_ids"] = agg(chosen, "unique_ids")
        row["smallest_geo"] = smallest_of(chosen)
        row["match_flag"] = flag
        if flag:
            drift.append({"type": flag.lower(), "study_id": sid, "topic_folder": topic,
                          "detail": f"chosen: {row['subfolder']}"})
        # Matched a unit but it yielded no geography/uid -> its dict is missing/malformed.
        if not row["geographies"]:
            row["match_flag"] = (row["match_flag"] + "+NO_DICT").lstrip("+")
            drift.append({"type": "matched_unit_no_dict", "study_id": sid,
                          "topic_folder": topic,
                          "detail": f"{row['subfolder']} has no parseable dict on disk"})
        published.append(row)

    # Drift: O: units in a mapped topic that no deposit selected (superseded / extra vintages),
    # and O: topics with no catalog deposit at all (unmapped topics).
    mapped_topics = {d["topic_folder"] for d in deposits.values() if d.get("topic_folder")}
    for topic, units in sorted(units_by_topic.items()):
        if topic in mapped_topics:
            for u in units:
                if u["subfolder"] not in selected_subfolders[topic]:
                    drift.append({"type": "o_unit_unselected", "study_id": "",
                                  "topic_folder": topic,
                                  "detail": f"{u['subfolder']} ({u['date_range']}) not matched to a deposit"})
        else:
            drift.append({"type": "o_topic_no_catalog", "study_id": "", "topic_folder": topic,
                          "detail": f"{len(units)} O: unit(s); no catalog deposit maps here"})

    # Oracle diff -------------------------------------------------------------
    oracle_rows = []
    for row in published:
        sid = row["study_id"]
        o = oracle.get(sid)
        if not o:
            oracle_rows.append({"study_id": sid, "in_oracle": "N", "topic_check": "",
                                "uid_check": "", "derived_unique_ids": row["unique_ids"],
                                "oracle_unique_ids": "", "detail": "not in v03 oracle"})
            continue
        topic_check = "match" if row["topic_folder"].lower() == o["topic_folder"].lower() \
            else f"DIFF mine={row['topic_folder']} oracle={o['topic_folder']}"
        mine, theirs = tokenize_uid(row["unique_ids"]), tokenize_uid(o["unique_ids"])
        oracle_stale_sub = (row["subfolder"] and o["latest_subfolder"]
                            and o["latest_subfolder"] not in row["subfolder"])
        if mine == theirs:
            uid_check = "match"
        elif not theirs:
            uid_check = "oracle_unfilled"        # oracle cell was left blank
        elif not mine:
            uid_check = "MINE_EMPTY"             # real: matched unit had no dict
        elif theirs <= mine:
            uid_check = "mine_superset"          # mine aggregates more geos
        elif oracle_stale_sub:
            uid_check = "oracle_stale"           # oracle pinned an older subfolder
        else:
            uid_check = "GENUINE_DIFF"           # both filled, same-ish subfolder, differ
        oracle_rows.append({
            "study_id": sid, "in_oracle": "Y", "topic_check": topic_check,
            "uid_check": uid_check, "derived_unique_ids": row["unique_ids"],
            "oracle_unique_ids": o["unique_ids"],
            "detail": f"oracle subfolder={o['latest_subfolder']}",
        })

    # Write CSVs + workbook ---------------------------------------------------
    pub_df = pd.DataFrame(published)
    od_df = pd.DataFrame(odrive)
    drift_df = pd.DataFrame(drift)
    orc_df = pd.DataFrame(oracle_rows)
    pub_df.to_csv(DATA / "published_catalog_joined.csv", index=False)
    drift_df.to_csv(DATA / "drift.csv", index=False)
    orc_df.to_csv(DATA / "oracle_diff.csv", index=False)
    with pd.ExcelWriter(REPO / "master_inventory.xlsx", engine="openpyxl") as xw:
        pub_df.to_excel(xw, sheet_name="Published", index=False)
        od_df.to_excel(xw, sheet_name="O-Drive Reality", index=False)
        drift_df.to_excel(xw, sheet_name="Reconciliation Drift", index=False)
        orc_df.to_excel(xw, sheet_name="Oracle Diff", index=False)

    # Summary -----------------------------------------------------------------
    flags = defaultdict(int)
    for r in published:
        flags[r["match_flag"] or "clean"] += 1
    dtypes = defaultdict(int)
    for d in drift:
        dtypes[d["type"]] += 1
    topic_diffs = [o for o in oracle_rows if o["topic_check"].startswith("DIFF")]
    uid_cat = defaultdict(int)
    for o in oracle_rows:
        if o["in_oracle"] == "Y":
            uid_cat[o["uid_check"]] += 1
    real_uid = [o for o in oracle_rows if o["uid_check"] in ("MINE_EMPTY", "GENUINE_DIFF")]

    print("=" * 70)
    print("RECONCILIATION JOIN")
    print("=" * 70)
    print(f"Published rows           : {len(published)}")
    print(f"  match flags            : {dict(flags)}")
    print(f"Drift findings           : {len(drift)}")
    print(f"  by type                : {dict(dtypes)}")
    print()
    print("Oracle diff (vs v03 Studies):")
    print(f"  topic_folder matches   : {sum(1 for o in oracle_rows if o['topic_check']=='match')}"
          f" / {sum(1 for o in oracle_rows if o['in_oracle']=='Y')}   DIFFs: {len(topic_diffs)}")
    for o in topic_diffs:
        print(f"      {o['study_id']}  {o['topic_check']}")
    print(f"  unique_id categories   : {dict(uid_cat)}")
    print(f"  -> needs a human look ({len(real_uid)}): MINE_EMPTY + GENUINE_DIFF")
    for o in real_uid:
        print(f"      {o['study_id']:>7} [{o['uid_check']}] mine=[{o['derived_unique_ids']}] oracle=[{o['oracle_unique_ids']}]  {o['detail']}")
    print()
    print("O: topics with no catalog deposit (unmapped):")
    for d in drift:
        if d["type"] == "o_topic_no_catalog":
            print(f"      {d['topic_folder']:30s} {d['detail']}")
    print(f"\nWrote master_inventory.xlsx + data/published_catalog_joined.csv, drift.csv, oracle_diff.csv")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
