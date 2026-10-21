"""Official public CMS datasets, downloaded on demand and cached in SQLite.

* Medicare Advantage / Part D Landscape (cms.gov): every approved plan by state/county
  with premiums, deductible, MOOP, SNP type, C-SNP conditions and star ratings.
* Monthly Prescription Drug Plan Formulary and Pharmacy Network Information
  (data.cms.gov PUF): plan information, formulary (RXCUI/NDC, tier, PA/ST/QL),
  beneficiary cost sharing and insulin cost sharing for every Part D plan.
  Only the small members are range-read out of the ~2.3 GB archive.
* Census 2020 ZCTA-to-county relationship file for ZIP -> county lookups.
* NLM RxNav for drug name -> RXCUI.

These describe published plan designs. They never establish a person's eligibility,
enrollment or what they will pay at a specific pharmacy.
"""
from __future__ import annotations

import csv
import io
import json
import re
import sqlite3
import threading
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import quote

from . import net

LANDSCAPE_PAGE = "https://www.cms.gov/medicare/coverage/prescription-drug-coverage"
CMS_CATALOG = "https://data.cms.gov/data.json"
PUF_TITLE = "Monthly Prescription Drug Plan Formulary and Pharmacy Network Information"
PUF_LAYOUT = ("https://data.cms.gov/sites/default/files/2025-10/0564eb37-402d-4110-bd98-2d5399dc30e7/"
              "PUFRecordLayout-2026.pdf")
ZCTA_URL = ("https://www2.census.gov/geo/docs/maps-data/data/rel2020/zcta520/"
            "tab20_zcta520_county20_natl.txt")
RXNAV = "https://rxnav.nlm.nih.gov/REST"
CMS_HOSTS = frozenset({"www.cms.gov", "cms.gov", "data.cms.gov"})
CENSUS_HOSTS = frozenset({"www2.census.gov"})
RXNAV_HOSTS = frozenset({"rxnav.nlm.nih.gov"})

_LOCK = threading.RLock()
PLAN_RE = re.compile(r"^\s*([HRSE]\d{4})[-_ ]?(\d{3})(?:[-_ ](\d{1,3}))?\s*$", re.I)

DAYS = {"1": "30-day", "2": "90-day", "3": "other", "4": "60-day"}
LEVEL = {"0": "deductible (pre-deductible)", "1": "initial coverage", "3": "catastrophic"}
COST_TYPE = {"0": "not offered", "1": "copay", "2": "coinsurance"}
SNP = {"0": "Not a SNP", "1": "Chronic or Disabling Condition SNP (C-SNP)",
       "2": "Dual-Eligible SNP (D-SNP)", "3": "Institutional SNP (I-SNP)"}


class CMSDataError(Exception):
    pass


def parse_plan_id(value: str) -> tuple[str, str, str | None]:
    """'H5767-001', 'H5767-001-000', 'H5767_001_0' -> ('H5767','001','000'|None)."""
    m = PLAN_RE.match(str(value or ""))
    if not m:
        raise ValueError(f"Plan ID '{value}' is not a contract-PBP such as H5767-001 or H5767-001-000.")
    contract, pbp, seg = m.groups()
    return contract.upper(), pbp, (str(int(seg)).zfill(3) if seg is not None else None)


def fmt_plan(contract: str, pbp: str, seg: str | None) -> str:
    return f"{contract}-{pbp}" + (f"-{seg}" if seg is not None else "")


def _money(value: str):
    v = (value or "").strip()
    if not v or v.lower() in ("not applicable", "n/a"):
        return None
    try:
        return float(v.replace("$", "").replace(",", "").strip())
    except ValueError:
        return v


def _cms_dir() -> Path:
    d = net.cache_dir() / "cms"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _meta(conn) -> dict:
    return dict(conn.execute("select key, value from meta").fetchall())


# ---------------------------------------------------------------- Landscape
def _discover_landscape(year: int) -> str:
    page = net.fetch(LANDSCAPE_PAGE, hosts=CMS_HOSTS, max_bytes=5_000_000).text()
    links = sorted(set(re.findall(rf'href="(/files/zip/cy{year}-landscape-(\d{{6}})\.zip)"', page)),
                   key=lambda x: x[1])
    if not links:
        raise CMSDataError(f"CMS has not published a CY{year} Landscape file on {LANDSCAPE_PAGE}.")
    return "https://www.cms.gov" + links[-1][0]


def landscape_db(year: int, refresh: bool = False) -> sqlite3.Connection:
    with _LOCK:
        existing = sorted(_cms_dir().glob(f"landscape_v2_{year}_*.sqlite"))
        if existing and not refresh:
            conn = sqlite3.connect(existing[-1], check_same_thread=False)
            conn.row_factory = sqlite3.Row
            return conn
        url = _discover_landscape(year)
        stamp = re.search(r"-(\d{6})\.zip$", url).group(1)
        target = _cms_dir() / f"landscape_v2_{year}_{stamp}.sqlite"
        if target.exists() and refresh:
            conn = sqlite3.connect(target, check_same_thread=False)
            conn.row_factory = sqlite3.Row
            return conn
        resp = net.fetch(url, hosts=CMS_HOSTS, max_bytes=120_000_000, timeout=180)
        z = zipfile.ZipFile(io.BytesIO(resp.body))
        csv_name = next((n for n in z.namelist() if n.lower().endswith(".csv")), None)
        readme_name = next((n for n in z.namelist() if "readme" in n.lower()), None)
        if not csv_name:
            raise CMSDataError("CMS Landscape archive has no CSV member; format changed.")
        updated = None
        if readme_name:
            m = re.search(r"Data Last Updated:\s*([0-9/]+)", z.read(readme_name).decode("utf-8", "replace"))
            updated = m.group(1) if m else None
        tmp = target.with_suffix(".tmp")
        tmp.unlink(missing_ok=True)
        conn = sqlite3.connect(tmp)
        conn.execute("create table meta(key text primary key, value text)")
        # Plan attributes are identical across counties within a plan/state (checked at build),
        # so store them once and keep the service area separately.
        conn.execute("create table plan(contract text, pbp text, seg text, state text, row text)")
        conn.execute("create table area(contract text, pbp text, seg text, state text, county text)")
        reader = csv.DictReader(io.TextIOWrapper(z.open(csv_name), encoding="utf-8-sig"))
        need = {"Contract ID", "Plan ID", "Segment ID", "County Name", "State Territory Abbreviation",
                "Plan Name", "Plan Type", "Contract Year"}
        if not need.issubset(reader.fieldnames or []):
            raise CMSDataError("CMS Landscape columns changed; refusing to guess.")
        seen, areas = {}, []
        keep = set(_LANDSCAPE_FIELDS.values()) | need
        for row in reader:
            key = (row["Contract ID"].strip().upper(), row["Plan ID"].strip().zfill(3),
                   row["Segment ID"].strip().zfill(3), row["State Territory Abbreviation"].strip())
            slim = {k: v for k, v in row.items() if k in keep}
            slim.pop("County Name", None)
            blob = json.dumps(slim, sort_keys=True)
            if key in seen and seen[key] != blob:
                raise CMSDataError(f"Landscape attributes vary by county for {key}; cache format needs revision.")
            seen[key] = blob
            areas.append(key + (row["County Name"].strip(),))
        conn.executemany("insert into plan values (?,?,?,?,?)", [k + (v,) for k, v in seen.items()])
        conn.executemany("insert into area values (?,?,?,?,?)", areas)
        conn.execute("create index ix_plan on plan(contract, pbp, seg)")
        conn.execute("create index ix_area on area(state, county)")
        conn.execute("create index ix_area_plan on area(contract, pbp, seg)")
        meta = {"source_url": url, "source_page": LANDSCAPE_PAGE, "year": str(year),
                "data_last_updated": updated or "", "http_last_modified": resp.headers.get("last-modified", ""),
                "retrieved_at": resp.fetched_at, "sha256": resp.sha256, "file": csv_name}
        conn.executemany("insert into meta values (?,?)", list(meta.items()))
        conn.commit()
        conn.close()
        tmp.replace(target)
        conn = sqlite3.connect(target, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        return conn


def landscape_source(year: int) -> dict:
    m = _meta(landscape_db(year))
    return {"dataset": f"CMS CY{year} Medicare Advantage & Part D Landscape", **m,
            "notice": "CMS: 'The data within are subject to change as contracts are finalized.'"}


_LANDSCAPE_FIELDS = {
    "plan_type": "Plan Type", "category": "Contract Category Type", "organization": "Organization Marketing Name",
    "parent_organization": "Parent Organization Name", "contract_name": "Contract Name",
    "organization_type": "Organization Type", "snp": "Special Needs Plan (SNP) Indicator",
    "snp_type": "SNP Type", "csnp_conditions": "Chronic or Disabling Condition SNP (C-SNP) Condition Type",
    "dsnp_integration": "Dual Eligible SNP (D-SNP) Integration Status",
    "dsnp_zero_cost_sharing": "Medicare Zero-Dollar Cost Sharing D-SNP",
    "part_d_coverage": "Part D Coverage Indicator", "drug_benefit_type": "Drug Benefit Type",
    "part_c_premium": "Part C Premium", "part_d_total_premium": "Part D Total Premium",
    "part_d_basic_premium": "Part D Basic Premium", "part_d_supplemental_premium": "Part D Supplemental Premium",
    "monthly_consolidated_premium": "Monthly Consolidated Premium (Part C + D)",
    "part_d_deductible": "Annual Part D Deductible Amount", "moop_in_network": "In-Network Maximum Out-of-Pocket (MOOP) Amount",
    "part_d_oop_threshold": "Part D Out-of-Pocket (OOP) Threshold",
    "low_income_premium_subsidy": "Low Income Premium Subsidy (LIPS) Amount",
    "part_d_low_income_beneficiary_premium": "Part D Low Income Beneficiary Premium Amount",
    "overall_star_rating": "Overall Star Rating", "part_c_star_rating": "Part C Summary Star Rating",
    "part_d_star_rating": "Part D Summary Star Rating", "sanctioned": "Sanctioned Plan",
    "enrollment_capacity_limit": "Enrollment Capacity Limit Amount",
}
_MONEY = {"part_c_premium", "part_d_total_premium", "part_d_basic_premium", "part_d_supplemental_premium",
          "monthly_consolidated_premium", "part_d_deductible", "moop_in_network", "part_d_oop_threshold",
          "low_income_premium_subsidy", "part_d_low_income_beneficiary_premium"}


def _shape(row: dict) -> dict:
    out = {"plan_id": f"{row['Contract ID']}-{row['Plan ID'].zfill(3)}-{row['Segment ID'].zfill(3)}",
           "plan_name": row["Plan Name"], "year": int(row["Contract Year"]),
           "state": row["State Territory Abbreviation"], "county": row["County Name"]}
    for key, col in _LANDSCAPE_FIELDS.items():
        val = row.get(col, "")
        out[key] = _money(val) if key in _MONEY else (val.strip() if isinstance(val, str) else val)
    return out


def counties(year: int, state: str) -> list[str]:
    conn = landscape_db(year)
    rows = conn.execute("select distinct county from area where state=? order by county", (state.upper(),))
    return [r[0] for r in rows]


def find_plans(year: int, state: str, county: str, plan_type: str = "", organization: str = "",
               snp: str = "", include_pdp: bool = True, part_d_only: bool = False) -> dict:
    conn = landscape_db(year)
    st = state.strip().upper()
    if not re.fullmatch(r"[A-Z]{2}", st):
        raise ValueError("state must be a two-letter abbreviation such as TX.")
    cty = re.sub(r"\s+county$", "", county.strip(), flags=re.I)
    rows = conn.execute("""select p.row, a.county from area a join plan p on p.contract=a.contract and p.pbp=a.pbp
                           and p.seg=a.seg and p.state=a.state
                           where a.state=? and (lower(a.county)=lower(?) or a.county='All Counties')""",
                        (st, cty)).fetchall()
    known = {r[0].lower() for r in conn.execute("select distinct county from area where state=?", (st,))}
    if cty.lower() not in known:
        close = [c for c in counties(year, st) if c.lower().startswith(cty.lower()[:3])][:10]
        raise ValueError(f"County '{county}' not found for {st} in the CY{year} Landscape. Similar: {close}")
    plans = []
    for raw, area_county in rows:
        p = _shape({**json.loads(raw), "County Name": area_county})
        if p["category"] == "PDP" and not include_pdp:
            continue
        if plan_type and plan_type.lower() not in p["plan_type"].lower():
            continue
        if organization and organization.lower() not in (p["organization"] + " " + p["parent_organization"] + " " + p["contract_name"]).lower():
            continue
        if snp:
            s = snp.lower()
            if s in ("none", "no", "non-snp") and p["snp"] != "No":
                continue
            if s in ("c-snp", "csnp", "chronic") and "Chronic" not in p["snp_type"]:
                continue
            if s in ("d-snp", "dsnp", "dual") and "Dual" not in p["snp_type"]:
                continue
            if s in ("i-snp", "isnp", "institutional") and "Institutional" not in p["snp_type"]:
                continue
        if part_d_only and p["part_d_coverage"] != "Yes":
            continue
        plans.append(p)
    plans.sort(key=lambda p: (p["category"] == "PDP", p["organization"], p["plan_id"]))
    return {"year": year, "state": st, "county": cty, "count": len(plans), "plans": plans,
            "source": landscape_source(year)}


def plan_landscape(year: int, plan_id: str) -> dict:
    contract, pbp, seg = parse_plan_id(plan_id)
    conn = landscape_db(year)
    q = """select p.row, a.county from area a join plan p on p.contract=a.contract and p.pbp=a.pbp
           and p.seg=a.seg and p.state=a.state where a.contract=? and a.pbp=?"""
    rows = conn.execute(q + (" and a.seg=?" if seg else ""), (contract, pbp, seg) if seg else (contract, pbp)).fetchall()
    shaped = [_shape({**json.loads(r[0]), "County Name": r[1]}) for r in rows]
    segments: dict[str, dict] = {}
    for p in shaped:
        s = segments.setdefault(p["plan_id"], {**{k: v for k, v in p.items() if k not in ("state", "county")},
                                               "service_area": {}})
        s["service_area"].setdefault(p["state"], []).append(p["county"])
    all_segments = sorted({r[0] for r in conn.execute(
        "select seg from plan where contract=? and pbp=?", (contract, pbp))})
    return {"query": plan_id, "contract": contract, "pbp": pbp, "segment_requested": seg,
            "segments_in_landscape": [f"{contract}-{pbp}-{s}" for s in all_segments],
            "found": bool(segments), "plans": list(segments.values()), "source": landscape_source(year)}


def contract_plans(year: int, contract: str) -> list[dict]:
    conn = landscape_db(year)
    out = {}
    for raw, state in conn.execute("select row, state from plan where contract=?", (contract.upper(),)):
        p = _shape({**json.loads(raw), "County Name": ""})
        p.pop("county", None)
        out.setdefault(p["plan_id"], p)
    return list(out.values())


# ---------------------------------------------------------------- ZIP -> county
def zip_counties(zip_code: str) -> dict:
    if not re.fullmatch(r"\d{5}", str(zip_code or "")):
        raise ValueError("ZIP code must be exactly five digits.")
    path = _cms_dir() / "zcta_county_2020.sqlite"
    with _LOCK:
        if not path.exists():
            resp = net.fetch(ZCTA_URL, hosts=CENSUS_HOSTS, max_bytes=30_000_000, timeout=120,
                             accept="text/plain,*/*")
            tmp = path.with_suffix(".tmp")
            tmp.unlink(missing_ok=True)
            conn = sqlite3.connect(tmp)
            conn.execute("create table z(zcta text, county_fips text, county text, land_part integer)")
            conn.execute("create table meta(key text primary key, value text)")
            reader = csv.DictReader(io.StringIO(resp.body.decode("utf-8-sig")), delimiter="|")
            rows = [(r["GEOID_ZCTA5_20"], r["GEOID_COUNTY_20"], r["NAMELSAD_COUNTY_20"],
                     int(r["AREALAND_PART"] or 0)) for r in reader if r.get("GEOID_ZCTA5_20")]
            conn.executemany("insert into z values (?,?,?,?)", rows)
            conn.execute("create index ix on z(zcta)")
            conn.executemany("insert into meta values (?,?)", [("source_url", ZCTA_URL),
                             ("retrieved_at", resp.fetched_at), ("vintage", "2020 Census ZCTA-to-county relationship")])
            conn.commit()
            conn.close()
            tmp.replace(path)
    conn = sqlite3.connect(path)
    rows = conn.execute("select county_fips, county, land_part from z where zcta=? order by land_part desc",
                        (zip_code,)).fetchall()
    meta = dict(conn.execute("select key, value from meta").fetchall())
    total = sum(r[2] for r in rows) or 1
    fips_state = {"48": "TX"}
    out = [{"county_fips": r[0], "county": re.sub(r"\s+(County|Parish|Borough|Municipio)$", "", r[1]),
            "state_fips": r[0][:2], "state": _STATE_BY_FIPS.get(r[0][:2], fips_state.get(r[0][:2], "")),
            "share_of_zcta_land_area": round(r[2] / total, 4)} for r in rows]
    return {"zip": zip_code, "counties": out, "source": meta,
            "caution": ("Census ZCTAs approximate USPS ZIP codes. When a ZIP spans counties, confirm the "
                        "county of the residence address; plan availability is by county.")}


_STATE_BY_FIPS = {"01": "AL", "02": "AK", "04": "AZ", "05": "AR", "06": "CA", "08": "CO", "09": "CT",
                  "10": "DE", "11": "DC", "12": "FL", "13": "GA", "15": "HI", "16": "ID", "17": "IL",
                  "18": "IN", "19": "IA", "20": "KS", "21": "KY", "22": "LA", "23": "ME", "24": "MD",
                  "25": "MA", "26": "MI", "27": "MN", "28": "MS", "29": "MO", "30": "MT", "31": "NE",
                  "32": "NV", "33": "NH", "34": "NJ", "35": "NM", "36": "NY", "37": "NC", "38": "ND",
                  "39": "OH", "40": "OK", "41": "OR", "42": "PA", "44": "RI", "45": "SC", "46": "SD",
                  "47": "TN", "48": "TX", "49": "UT", "50": "VT", "51": "VA", "53": "WA", "54": "WV",
                  "55": "WI", "56": "WY", "72": "PR"}


# ---------------------------------------------------------------- Part D PUF
class _RemoteZip(io.RawIOBase):
    """Seekable reader over HTTP byte ranges so only needed ZIP members are downloaded."""

    def __init__(self, url: str, budget: int = 80_000_000):
        self.url, self.pos, self.read_total, self.budget = url, 0, 0, budget
        r = net.fetch(url, hosts=CMS_HOSTS, max_bytes=1, headers={"Range": "bytes=0-0"}, accept="*/*")
        m = re.fullmatch(r"bytes 0-0/(\d+)", r.headers.get("content-range", ""))
        if r.status != 206 or not m:
            raise CMSDataError("data.cms.gov did not honour a byte-range request.")
        self.size = int(m.group(1))
        self.last_modified = r.headers.get("last-modified", "")

    def readable(self):
        return True

    def seekable(self):
        return True

    def tell(self):
        return self.pos

    def seek(self, offset, whence=0):
        self.pos = offset if whence == 0 else self.pos + offset if whence == 1 else self.size + offset
        return self.pos

    def read(self, size=-1):
        if size is None or size < 0:
            size = self.size - self.pos
        size = min(size, self.size - self.pos)
        if size <= 0:
            return b""
        self.read_total += size
        if self.read_total > self.budget:
            raise CMSDataError("CMS archive read budget exceeded.")
        end = self.pos + size - 1
        r = net.fetch(self.url, hosts=CMS_HOSTS, max_bytes=size, accept="*/*", timeout=180,
                      headers={"Range": f"bytes={self.pos}-{end}"})
        if r.status != 206 or len(r.body) != size:
            raise CMSDataError("CMS byte-range response mismatch.")
        self.pos += size
        return r.body

    def readinto(self, b):
        data = self.read(len(b))
        b[: len(data)] = data
        return len(data)


def _discover_puf(year: int) -> tuple[str, str]:
    cat = net.fetch(CMS_CATALOG, hosts=CMS_HOSTS, max_bytes=60_000_000, accept="application/json", timeout=120).json()
    best = None
    for ds in cat.get("dataset", []):
        if not str(ds.get("title", "")).startswith(PUF_TITLE):
            continue
        for dist in ds.get("distribution", []):
            url = dist.get("downloadURL") or ""
            m = re.search(rf"/{year}_(\d{{8}})\.zip$", url)
            if m and (best is None or m.group(1) > best[1]):
                best = (url, m.group(1), ds.get("title"))
    if not best:
        raise CMSDataError(f"data.cms.gov has no CY{year} Part D formulary PUF yet (published from October of the prior year).")
    net.validate_url(best[0], CMS_HOSTS)
    return best[0], best[1]


_PUF_MEMBERS = {
    "plan information": "plan_info", "basic drugs formulary": "formulary", "beneficiary cost file": "cost",
    "insulin beneficiary cost": "insulin", "excluded drugs formulary": "excluded",
    "geographic locator": "geo", "indication based coverage": "indication",
}


def puf_db(year: int, refresh: bool = False) -> sqlite3.Connection:
    with _LOCK:
        existing = sorted(_cms_dir().glob(f"partd_puf_{year}_*.sqlite"))
        if existing and not refresh:
            conn = sqlite3.connect(existing[-1], check_same_thread=False)
            conn.row_factory = sqlite3.Row
            return conn
        url, stamp = _discover_puf(year)
        target = _cms_dir() / f"partd_puf_{year}_{stamp}.sqlite"
        if target.exists():
            conn = sqlite3.connect(target, check_same_thread=False)
            conn.row_factory = sqlite3.Row
            return conn
        rz = _RemoteZip(url)
        outer = zipfile.ZipFile(rz)
        tmp = target.with_suffix(".tmp")
        tmp.unlink(missing_ok=True)
        conn = sqlite3.connect(tmp)
        conn.execute("create table meta(key text primary key, value text)")
        loaded = []
        for info in outer.infolist():
            low = info.filename.lower()
            table = next((t for k, t in _PUF_MEMBERS.items() if low.startswith(k)), None)
            if not table:
                continue
            if info.file_size > 40_000_000:
                raise CMSDataError(f"Unexpectedly large CMS member {info.filename}.")
            inner = zipfile.ZipFile(io.BytesIO(outer.read(info)))
            txt = next(n for n in inner.namelist() if n.lower().endswith(".txt"))
            stream = io.TextIOWrapper(inner.open(txt), encoding="utf-8-sig", errors="replace", newline="")
            reader = csv.reader(stream, delimiter="|")
            header = [h.strip().upper() for h in next(reader)]
            cols = ", ".join(f'"{h}" text' for h in header)
            conn.execute(f"create table {table} ({cols})")
            ph = ",".join("?" * len(header))
            batch = []
            for row in reader:
                if len(row) != len(header):
                    row = (row + [""] * len(header))[: len(header)]
                batch.append([c.strip() for c in row])
                if len(batch) >= 20000:
                    conn.executemany(f"insert into {table} values ({ph})", batch)
                    batch = []
            conn.executemany(f"insert into {table} values ({ph})", batch)
            loaded.append(f"{table}:{txt}")
        for stmt in ("create index ix_pi on plan_info(CONTRACT_ID, PLAN_ID, SEGMENT_ID)",
                     "create index ix_f on formulary(FORMULARY_ID, RXCUI)",
                     "create index ix_c on cost(CONTRACT_ID, PLAN_ID, SEGMENT_ID)",
                     "create index ix_i on insulin(CONTRACT_ID, PLAN_ID, SEGMENT_ID)",
                     "create index ix_e on excluded(CONTRACT_ID, PLAN_ID, RXCUI)",
                     "create index ix_g on geo(COUNTY_CODE)"):
            conn.execute(stmt)
        meta = {"source_url": url, "dataset": f"{PUF_TITLE} (CY{year} file {stamp})", "file_date": stamp,
                "http_last_modified": rz.last_modified, "retrieved_at": net.now_iso(),
                "bytes_downloaded": str(rz.read_total), "members": "; ".join(loaded),
                "record_layout": PUF_LAYOUT}
        conn.executemany("insert into meta values (?,?)", list(meta.items()))
        conn.commit()
        conn.close()
        tmp.replace(target)
        conn = sqlite3.connect(target, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        return conn


def puf_source(year: int) -> dict:
    m = _meta(puf_db(year))
    m["notice"] = ("CMS formulary/cost PUF reflects plan submissions; plans can change formularies during "
                   "the year. Not a personal price quote.")
    return m


def partd_plan(year: int, plan_id: str) -> dict:
    contract, pbp, seg = parse_plan_id(plan_id)
    conn = puf_db(year)
    q = "select * from plan_info where CONTRACT_ID=? and PLAN_ID=?" + (" and SEGMENT_ID=?" if seg else "")
    rows = [dict(r) for r in conn.execute(q, (contract, pbp, seg) if seg else (contract, pbp))]
    if not rows:
        return {"found": False, "plan_id": plan_id, "source": puf_source(year)}
    geo = {}
    for r in rows:
        g = conn.execute("select * from geo where COUNTY_CODE=?", (r["COUNTY_CODE"],)).fetchone()
        if g:
            geo[r["COUNTY_CODE"]] = f"{g['COUNTY']}, {g['STATENAME']}"
    segs = {}
    for r in rows:
        key = fmt_plan(r["CONTRACT_ID"], r["PLAN_ID"], r["SEGMENT_ID"])
        s = segs.setdefault(key, {"plan_id": key, "plan_name": r["PLAN_NAME"], "contract_name": r["CONTRACT_NAME"],
                                  "formulary_id": r["FORMULARY_ID"], "part_d_premium": _money(r["PREMIUM"]),
                                  "part_d_deductible": _money(r["DEDUCTIBLE"]), "snp": SNP.get(r["SNP"], r["SNP"]),
                                  "suppressed": r["PLAN_SUPPRESSED_YN"], "counties": []})
        if r["COUNTY_CODE"]:
            s["counties"].append(geo.get(r["COUNTY_CODE"], r["COUNTY_CODE"]))
    return {"found": True, "segments": list(segs.values()), "source": puf_source(year)}


def _cost_value(ctype: str, amt: str, mn: str = "", mx: str = ""):
    t = COST_TYPE.get(ctype, ctype)
    if t == "not offered":
        return None
    try:
        a = float(amt)
    except (TypeError, ValueError):
        return {"type": t, "raw": amt}
    out = {"type": t}
    if t == "copay":
        out["amount_usd"] = a
        out["text"] = f"${a:,.2f} copay"
    else:
        out["percent"] = round(a * 100, 2)
        out["text"] = f"{a * 100:g}% coinsurance"
    for label, v in (("min_usd", mn), ("max_usd", mx)):
        try:
            if v not in ("", None) and float(v):
                out[label] = float(v)
        except ValueError:
            pass
    return out


def tier_cost_sharing(year: int, plan_id: str) -> dict:
    contract, pbp, seg = parse_plan_id(plan_id)
    conn = puf_db(year)
    seg = seg or "000"
    rows = [dict(r) for r in conn.execute("select * from cost where CONTRACT_ID=? and PLAN_ID=? and SEGMENT_ID=?",
                                          (contract, pbp, seg))]
    if not rows:
        return {"found": False, "plan_id": fmt_plan(contract, pbp, seg), "source": puf_source(year)}
    table = []
    for r in rows:
        table.append({
            "coverage_stage": LEVEL.get(r["COVERAGE_LEVEL"], r["COVERAGE_LEVEL"]), "tier": int(r["TIER"]),
            "days_supply": DAYS.get(r["DAYS_SUPPLY"], r["DAYS_SUPPLY"]),
            "preferred_retail": _cost_value(r["COST_TYPE_PREF"], r["COST_AMT_PREF"], r["COST_MIN_AMT_PREF"], r["COST_MAX_AMT_PREF"]),
            "standard_retail": _cost_value(r["COST_TYPE_NONPREF"], r["COST_AMT_NONPREF"], r["COST_MIN_AMT_NONPREF"], r["COST_MAX_AMT_NONPREF"]),
            "preferred_mail": _cost_value(r["COST_TYPE_MAIL_PREF"], r["COST_AMT_MAIL_PREF"], r["COST_MIN_AMT_MAIL_PREF"], r["COST_MAX_AMT_MAIL_PREF"]),
            "standard_mail": _cost_value(r["COST_TYPE_MAIL_NONPREF"], r["COST_AMT_MAIL_NONPREF"], r["COST_MIN_AMT_MAIL_NONPREF"], r["COST_MAX_AMT_MAIL_NONPREF"]),
            "specialty_tier": r["TIER_SPECIALTY_YN"] == "Y", "deductible_applies": r["DED_APPLIES_YN"] == "Y"})
    table.sort(key=lambda x: (x["coverage_stage"] != "initial coverage", x["tier"], x["days_supply"]))
    insulin = []
    for r in conn.execute("select * from insulin where CONTRACT_ID=? and PLAN_ID=? and SEGMENT_ID=?", (contract, pbp, seg)):
        r = dict(r)
        insulin.append({"tier": int(r["TIER"]), "days_supply": DAYS.get(r["DAYS_SUPPLY"], r["DAYS_SUPPLY"]),
                        **{k.lower(): (float(v) if v not in ("", None) else None) for k, v in r.items()
                           if k.lower().startswith(("copay_", "coin_")) and v.strip() != ""}})
    return {"found": True, "plan_id": fmt_plan(contract, pbp, seg), "tiers": table,
            "insulin_cost_sharing": insulin, "source": puf_source(year),
            "interpretation": ("Plan-level published cost sharing by tier, coverage stage, days supply and pharmacy type "
                               "(record layout: 0=not offered). Insulin rows override tier cost sharing for covered "
                               "insulin products. Not a personal price quote.")}


# ---------------------------------------------------------------- RxNav
def rxnav_candidates(name: str, limit: int = 40) -> dict:
    name = (name or "").strip()
    if not re.fullmatch(r"[A-Za-z0-9 .,%/()+\-]{2,80}", name):
        raise ValueError("Give a drug name (2-80 letters/numbers), without patient details.")
    data = net.fetch(f"{RXNAV}/drugs.json?name={quote(name)}", hosts=RXNAV_HOSTS,
                     accept="application/json", max_bytes=10_000_000).json()
    out = []
    for group in (data.get("drugGroup") or {}).get("conceptGroup") or []:
        for c in group.get("conceptProperties") or []:
            out.append({"rxcui": c["rxcui"], "name": c["name"], "tty": c["tty"],
                        "synonym": c.get("synonym") or ""})
    suggestion = None
    if not out:
        sp = net.fetch(f"{RXNAV}/approximateTerm.json?term={quote(name)}&maxEntries=8", hosts=RXNAV_HOSTS,
                       accept="application/json").json()
        cands = (sp.get("approximateGroup") or {}).get("candidate") or []
        seen = set()
        for c in cands:
            rx = c.get("rxcui")
            if not rx or rx in seen:
                continue
            seen.add(rx)
            rel = net.fetch(f"{RXNAV}/rxcui/{rx}/related.json?tty=SCD+SBD+GPCK+BPCK", hosts=RXNAV_HOSTS,
                            accept="application/json").json()
            for group in (rel.get("relatedGroup") or {}).get("conceptGroup") or []:
                for cp in group.get("conceptProperties") or []:
                    out.append({"rxcui": cp["rxcui"], "name": cp["name"], "tty": cp["tty"],
                                "synonym": cp.get("synonym") or ""})
            if out:
                suggestion = c.get("name") or rx
                break
    uniq = {c["rxcui"]: c for c in out}
    return {"query": name, "approximate_match": suggestion, "candidates": list(uniq.values())[:limit],
            "total_candidates": len(uniq), "source": {"api": "NLM RxNav", "url": f"{RXNAV}/drugs.json",
                                                      "retrieved_at": net.now_iso()}}


def drug_coverage(year: int, plan_ids: list[str], name: str, strength_filter: str = "") -> dict:
    rx = rxnav_candidates(name, limit=200)
    cands = rx["candidates"]
    if strength_filter:
        f = strength_filter.lower().replace(" ", "")
        cands = [c for c in cands if f in (c["name"] + c["synonym"]).lower().replace(" ", "")]
    conn = puf_db(year)
    results = []
    for pid in plan_ids:
        contract, pbp, seg = parse_plan_id(pid)
        seg = seg or "000"
        pi = conn.execute("select * from plan_info where CONTRACT_ID=? and PLAN_ID=? and SEGMENT_ID=? limit 1",
                          (contract, pbp, seg)).fetchone()
        if not pi:
            results.append({"plan_id": fmt_plan(contract, pbp, seg), "found": False,
                            "note": "Plan not in the CMS Part D PUF (no Part D, employer plan, or wrong ID/segment/year)."})
            continue
        fid = pi["FORMULARY_ID"]
        drugs = []
        for c in cands:
            rows = conn.execute("select * from formulary where FORMULARY_ID=? and RXCUI=?", (fid, c["rxcui"])).fetchall()
            excl = conn.execute("select * from excluded where CONTRACT_ID=? and PLAN_ID=? and RXCUI=?",
                                (contract, pbp, c["rxcui"])).fetchall()
            ind = conn.execute("select DISEASE from indication where CONTRACT_ID=? and PLAN_ID=? and RXCUI=?",
                               (contract, pbp, c["rxcui"])).fetchall()
            if rows:
                r = rows[0]
                drugs.append({"rxcui": c["rxcui"], "drug": c["name"], "on_formulary": True, "tier": int(r["TIER_LEVEL_VALUE"]),
                              "prior_authorization": r["PRIOR_AUTHORIZATION_YN"] == "Y",
                              "step_therapy": r["STEP_THERAPY_YN"] == "Y",
                              "quantity_limit": (f"{r['QUANTITY_LIMIT_AMOUNT']} per {r['QUANTITY_LIMIT_DAYS']} days"
                                                 if r["QUANTITY_LIMIT_YN"] == "Y" else None),
                              "selected_drug_negotiated_price": r["SELECTED_DRUG_YN"] == "Y" if "SELECTED_DRUG_YN" in r.keys() else None,
                              "ndc_rows": len(rows),
                              "indication_limited_to": [i[0] for i in ind] or None})
            elif excl:
                e = excl[0]
                drugs.append({"rxcui": c["rxcui"], "drug": c["name"], "on_formulary": True,
                              "excluded_drug_supplemental_coverage": True, "tier": e["TIER"],
                              "prior_authorization": e["PRIOR_AUTH_YN"] == "Y", "step_therapy": e["STEP_THERAPY_YN"] == "Y"})
        qlow = name.strip().lower()
        drugs.sort(key=lambda d: (not d["drug"].lower().startswith(qlow), "/" in d["drug"], d["drug"].lower()))
        covered_rx = {d["rxcui"] for d in drugs}
        results.append({"plan_id": fmt_plan(contract, pbp, seg), "plan_name": pi["PLAN_NAME"], "formulary_id": fid,
                        "found": True, "covered_products": drugs,
                        "not_listed_products": [{"rxcui": c["rxcui"], "drug": c["name"]} for c in cands
                                                if c["rxcui"] not in covered_rx][:60]})
    return {"query": name, "year": year, "rxnav": {k: v for k, v in rx.items() if k != "candidates"},
            "products_considered": len(cands), "plans": results, "source": puf_source(year),
            "interpretation": ("Coverage is by exact RxNorm product (RXCUI). 'Not listed' means that exact product is "
                               "absent from the CMS file for that formulary; another strength/form may be covered, and "
                               "plans may cover drugs via exceptions/transition fills. Confirm with the plan's current "
                               "formulary before advising.")}
