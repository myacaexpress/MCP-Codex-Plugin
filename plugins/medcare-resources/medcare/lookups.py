"""Substantive MedCare lookups: medications, providers, pharmacies, NPI identity."""
from __future__ import annotations

import gzip
import html
import json
import re
from concurrent.futures import ThreadPoolExecutor
from functools import lru_cache
from urllib.parse import quote, quote_plus

from . import DATA, catalog, cms, net
from .textsearch import fold, tokens

NAV_BASE = "https://client.formularynavigator.com/Search.aspx?siteCode=4117394336"
NAV_HOSTS = frozenset({"client.formularynavigator.com"})
PROVIDER_API = "https://mcpartnerstx.com/wp-json/provider-search/v1/search"
NPPES_API = "https://npiregistry.cms.hhs.gov/api/"
MEDCARE_PLANS = ("H5767-001", "H5767-002", "H5767-003")
YEAR = 2027

# ---------------------------------------------------------------- medications
_ITEM = re.compile(r"drugNameClicked\(&#39;([^&]*)&#39;,(true|false),&#39;(\d*)&#39;\);\" id=\"lbDrug_\d+\".*?"
                   r"alt=\"([^\"]*)\".*?<span class=\"(\w+)\">([^<]*)</span>", re.S)


def _clean_name(value: str) -> str:
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9 .,%/()+\-]{3,50}", value.strip()):
        raise ValueError("Medication name must be 3-50 letters/numbers (no patient details).")
    return value.strip()


def navigator_search(name: str, max_details: int = 12) -> dict:
    """Live search of MedCare's public Formulary Navigator site."""
    name = _clean_name(name)
    url = f"{NAV_BASE}&targetScreen=1&drugSearchText={quote(name)}"
    resp = net.fetch(url, hosts=NAV_HOSTS, max_bytes=5_000_000, timeout=40)
    page = resp.text()
    items = [{"name": html.unescape(m[5]).strip(), "tier": m[3], "kind": "brand" if m[4] == "brandLink" else "generic",
              "drug_id": m[2], "_key": m[0], "_multi": m[1] == "true"} for m in _ITEM.findall(page)]
    eff = re.search(r"Effective Date:\s*([0-9/]+)\s*Updated:\s*([0-9/]+)", re.sub(r"<[^>]+>", " ", page))

    def detail(item):
        u = f"{NAV_BASE}&targetScreen=3&drugBrandListBaseKey={item['_key']}" + (
            f"&drugId={quote(item['drug_id'])}" if item["drug_id"] else "") + ("&multiClass=yes" if item["_multi"] else "")
        try:
            d = net.fetch(u, hosts=NAV_HOSTS, max_bytes=5_000_000, timeout=40).text()
        except net.FetchError as err:
            return {"detail_error": str(err)}
        m = re.search(r'<table[^>]*id="tblResults".*?</table>', d, re.S)
        if not m:
            return {"detail_error": "results table not found"}
        rows = re.findall(r"<tr[^>]*>(.*?)</tr>", m.group(0), re.S)
        for row in rows:
            cells = [re.sub(r"\s+", " ", html.unescape(re.sub(r"<[^>]+>", " ", c))).strip()
                     for c in re.findall(r"<td[^>]*>(.*?)</td>", row, re.S)]
            icons = [html.unescape(t) for t in re.findall(r"<img[^>]*title=[\"']([^\"']*)[\"']", row)]
            details = []
            for popup, title in re.findall(r"onclick=\"showPopup\('[^']*','[^']*','([^']*)'.*?<img[^>]*title=[\"']([^\"']*)[\"']", row, re.S):
                parts = [x.strip() for x in html.unescape(popup).split("|")]
                pairs = {parts[i].rstrip(":"): parts[i + 1] for i in range(0, len(parts) - 1, 2) if parts[i + 1]}
                details.append({"restriction": html.unescape(title), "details": pairs})
            if cells:
                restrictions = [i for i in icons if not i.startswith("Tier") and i != "Over The Counter"]
                return {"therapeutic_class": cells[1] if len(cells) > 1 else None,
                        "dose_form_strength": cells[2] if len(cells) > 2 else None,
                        "tier_icon": next((i for i in icons if i.startswith("Tier") or i == "Over The Counter"), None),
                        "restrictions": restrictions, "restriction_details": details or None, "detail_url": u}
        return {"detail_error": "no result row"}

    with ThreadPoolExecutor(max_workers=6) as pool:
        details = list(pool.map(detail, items[:max_details]))
    for item, det in zip(items, details):
        item.update(det)
    for item in items:
        item.pop("_key", None)
        item.pop("_multi", None)
    return {"query": name, "search_url": url, "retrieved_at": resp.fetched_at, "match_count": len(items),
            "details_fetched_for": min(len(items), max_details), "matches": items,
            "formulary_effective": eff.group(1) if eff else None, "formulary_updated": eff.group(2) if eff else None,
            "source": "MedCare Partners Formulary Navigator (public, linked from mcpartnerstx.com)"}


@lru_cache(maxsize=1)
def _snapshot():
    data = json.loads(gzip.decompress((DATA / "mcp-formulary-2027.json.gz").read_bytes()))
    return data


def snapshot_search(name: str, limit: int = 40) -> dict:
    snap = _snapshot()
    q = tokens(name)
    rows = []
    for e in snap["entries"]:
        hay = set(tokens(e["name"] + " " + e.get("reference", "")))
        if all(any(h == t or h.startswith(t) for h in hay) for t in q):
            rows.append(e)
    rows.sort(key=lambda e: (not fold(e["name"]).startswith(fold(name)), e["name"]))
    fs = catalog._load()["catalog"].get("formulary_snapshot", {})
    return {"source_pdf": snap["source_url"], "source_page": snap["source_page"], "effective": snap["effective"],
            "sha256": snap["sha256"], "matches_live_at_last_build": fs.get("matches_live_at_build"),
            "last_build_check": fs.get("build_checked_at"),
            "total_matches": len(rows), "matches": rows[:limit],
            "legend": "Restrictions: PA prior authorization, ST step therapy, QL quantity limit, NM not available by "
                      "mail order, LA limited access, NDS non-extended day supply (see formulary PDF key)."}


def drug_lookup(name: str, include_costs: bool = True, include_live: bool = True) -> dict:
    """MedCare medication lookup combining live navigator, PDF snapshot and CMS cost sharing."""
    name = _clean_name(name)
    out = {"query": name, "plan_year": YEAR, "plans": list(MEDCARE_PLANS)}
    if include_live:
        try:
            out["live_formulary_navigator"] = navigator_search(name)
        except net.FetchError as err:
            out["live_formulary_navigator"] = {"status": "unavailable", "error": str(err)}
    out["formulary_pdf_snapshot"] = snapshot_search(name)
    if include_costs:
        costs = {}
        try:
            for pid in MEDCARE_PLANS:
                t = cms.tier_cost_sharing(YEAR, pid)
                costs[pid] = {"plan_id": t.get("plan_id"),
                              "initial_coverage_by_tier": [
                                  {"tier": r["tier"], "days_supply": r["days_supply"],
                                   "standard_retail": (r["standard_retail"] or {}).get("text"),
                                   "standard_mail": (r["standard_mail"] or {}).get("text"),
                                   "deductible_applies": r["deductible_applies"]}
                                  for r in t.get("tiers", []) if r["coverage_stage"] == "initial coverage"],
                              "deductible_stage_note": "Rows for the deductible stage exist where a deductible applies; see cms_tier_cost_sharing."}
            pd = {pid: cms.partd_plan(YEAR, pid) for pid in MEDCARE_PLANS}
            for pid, info in pd.items():
                if info.get("found"):
                    costs[pid]["part_d_deductible"] = info["segments"][0]["part_d_deductible"]
                    costs[pid]["plan_name"] = info["segments"][0]["plan_name"]
            out["cost_sharing_by_tier"] = costs
            out["cost_source"] = cms.puf_source(YEAR)
        except (net.FetchError, cms.CMSDataError) as err:
            out["cost_sharing_by_tier"] = {"status": "unavailable", "error": str(err)}
    # cross-check tiers between live navigator and snapshot
    discrepancies = []
    live = out.get("live_formulary_navigator", {}).get("matches") or []
    snap = {fold(m["name"]): m for m in out["formulary_pdf_snapshot"]["matches"]}
    for m in live:
        s = snap.get(fold(m["name"]))
        if s and m.get("tier", "").replace("Tier ", "") != str(s["tier"]):
            discrepancies.append({"drug": m["name"], "live_tier": m["tier"], "pdf_snapshot_tier": s["tier"]})
    out["tier_discrepancies_live_vs_pdf"] = discrepancies
    out["how_to_read"] = ("A drug's plan cost = the cost sharing for its tier in the plan's current coverage stage. "
                          "Confirm exact strength/form, restrictions (PA/ST/QL), pharmacy type and days supply. "
                          "Insulin and vaccine rules override tier cost sharing (see plan SB). This is published "
                          "plan design, not a quote and not a coverage determination. No match does not prove a drug "
                          "is excluded: check spelling, brand vs generic, and the exceptions process.")
    return out


# ---------------------------------------------------------------- providers
PROVIDER_TYPES = {
    "specialist": "Individual", "individual": "Individual", "primary care": "primary_care_provider",
    "pcp": "primary_care_provider", "primary care provider": "primary_care_provider",
    "primary care physician": "primary_care_provider", "specialists": "Individual",
}
FACILITY_TYPES = ["Inpatient Rehabilitation", "Center Radiology", "Clinical Medical Laboratory", "Home Health",
                  "Long Term Care Hospital", "Home Infusion Therapy Pharmacy", "Radiology", "Rehabilitation Hospital",
                  "Rehabilitation Unit", "Skilled Nursing Facility", "Durable Medical Equipment",
                  "General Acute Care Hospital"]


def _ptype(value: str) -> str:
    v = re.sub(r"[_\-]+", " ", (value or "").strip())
    if (value or "").strip() == "primary_care_provider":
        return "primary_care_provider"
    if v.lower() in PROVIDER_TYPES:
        return PROVIDER_TYPES[v.lower()]
    for f in FACILITY_TYPES:
        if v.lower() == f.lower():
            return f
    raise ValueError("provider_type must be one of: specialist, primary_care, " + ", ".join(FACILITY_TYPES))


def _taxonomy(code: str) -> str:
    c = catalog.nucc().get("codes", {}).get(code)
    return c["display"] if c else code


def _phone(p):
    d = re.sub(r"\D", "", str(p or ""))
    return f"({d[:3]}) {d[3:6]}-{d[6:10]}" if len(d) == 10 else (p or None)


def _normalize_provider(raw: dict) -> dict:
    name = raw.get("name")
    if isinstance(name, dict):
        name = " ".join(x for x in (name.get("prefix"), name.get("first"), name.get("middle"), name.get("last"),
                                    name.get("suffix")) if x)
    elif not name:
        name = " ".join(x for x in (raw.get("firstName"), raw.get("middleName"), raw.get("lastName")) if x) \
            or raw.get("facilityName")
    out = {"npi": raw.get("npi"), "name": (name or "").strip() or None,
           "degree": raw.get("degree"), "group": raw.get("providerEntityName"),
           "provider_type": raw.get("type") or raw.get("facilityType") or None,
           "languages": raw.get("languages") or None,
           "record_updated": raw.get("lastUpdatedOn") or (raw.get("updatedAt") or "")[:10] or None}
    if raw.get("startDate"):
        out["network_start"] = raw["startDate"][:10]
        out["network_end"] = (raw.get("endDate") or "")[:10] or None
    if raw.get("primarySpecialty"):
        out["specialty"] = raw.get("primarySpecialty")
    plans = []
    for p in raw.get("plans") or []:
        plans.append({"plan_id": p.get("maPlanId"), "contract_year": p.get("yearContractYear"),
                      "accepting_new_patients": p.get("accepting"),
                      "specialties": [{"code": c, "name": _taxonomy(c)} for c in (p.get("specialty") or [])],
                      "locations": [{"address": ", ".join(x for x in (a.get("address"), a.get("address2")) if x),
                                     "city": a.get("city"), "state": a.get("state"), "zip": a.get("zip"),
                                     "phone": _phone(a.get("phone"))} for a in p.get("addresses") or []]})
    if plans:
        out["plans"] = plans
    if raw.get("addressLine1") or raw.get("address"):
        out["locations"] = [{"address": ", ".join(x for x in (raw.get("addressLine1") or raw.get("address"),
                                                              raw.get("addressLine2")) if x),
                             "city": raw.get("city"), "state": raw.get("state"), "zip": raw.get("zip"),
                             "phone": _phone(raw.get("phone") or raw.get("phoneNumber"))}]
    if raw.get("contractYear"):
        out["contract_year"] = raw.get("contractYear")
    return {k: v for k, v in out.items() if v not in (None, [], "")}


def _plan_matches(entry_plan: str, wanted: str) -> bool:
    try:
        wc, wp, ws = cms.parse_plan_id(wanted)
        ec, ep, es = cms.parse_plan_id(entry_plan or "")
    except ValueError:
        return False
    return wc == ec and wp == ep and (ws is None or ws == (es or "000"))


def filter_providers(results: list[dict], specialty: str = "", plan_id: str = "",
                     accepting_only: bool = False) -> tuple[list[dict], dict]:
    """Apply plan, specialty and accepting-new-patients filters to the SAME plan entry.

    Records without per-plan data (some facility records) have unknown plan membership and
    unknown acceptance; they are excluded whenever plan_id or accepting_only is requested.
    """
    excluded = {"unknown_plan_membership": 0, "no_matching_plan_entry": 0}
    spec = fold(specialty) if specialty else ""
    out = []
    for p in results:
        entries = p.get("plans")
        if not entries:
            if plan_id or accepting_only:
                excluded["unknown_plan_membership"] += 1
                continue
            if spec and spec not in fold(str(p.get("specialty", "")) + " " + str(p.get("provider_type", ""))):
                continue
            out.append(p)
            continue
        keep = []
        for e in entries:
            if plan_id and not _plan_matches(e.get("plan_id"), plan_id):
                continue
            if spec and spec not in fold(json.dumps(e.get("specialties", []))):
                continue
            if accepting_only and not (e.get("accepting_new_patients") or "").strip().lower().startswith("accepting"):
                continue
            keep.append(e)
        if not keep:
            excluded["no_matching_plan_entry"] += 1
            continue
        q = dict(p)
        if plan_id or accepting_only or spec:
            q["plans"] = keep
            q["filtered_to_matching_plan_entries"] = True
        out.append(q)
    return out, excluded


def provider_search(provider_type: str, name: str = "", zip_code: str = "", address: str = "",
                    specialty: str = "", plan_id: str = "", accepting_only: bool = False, limit: int = 25) -> dict:
    ptype = _ptype(provider_type)
    params = {"type": ptype}
    if name:
        if not re.fullmatch(r"[A-Za-z .,'&\-]{2,60}", name):
            raise ValueError("name: 2-60 letters (provider or facility name only).")
        params["name"] = name.strip()
    if zip_code:
        if not re.fullmatch(r"\d{5}", zip_code):
            raise ValueError("zip_code must be 5 digits.")
        params["zipCode"] = zip_code
    if address:
        if not re.fullmatch(r"[A-Za-z0-9 .,#'\-]{2,80}", address):
            raise ValueError("address: street/city text only.")
        params["address"] = address.strip()
    qs = "&".join(f"{k}={quote_plus(v)}" for k, v in params.items())
    url = f"{PROVIDER_API}?{qs}"
    resp = net.fetch(url, hosts=net.MEDCARE_HOSTS, accept="application/json", max_bytes=40_000_000, timeout=60)
    data = resp.json()
    if not data.get("success"):
        return {"status": "no_results_or_error", "api_message": data.get("message"), "request": params,
                "retrieved_at": resp.fetched_at}
    merged: dict[str, dict] = {}
    for raw in data.get("data") or []:
        p = _normalize_provider(raw)
        key = (p.get("npi") or "") + "|" + (p.get("name") or "") + "|" + json.dumps(p.get("locations"), sort_keys=True)
        if key in merged:
            continue
        merged[key] = p
    results = list(merged.values())
    results, excluded = filter_providers(results, specialty=specialty, plan_id=plan_id, accepting_only=accepting_only)
    return {"request": params, "filters": {k: v for k, v in dict(specialty=specialty, plan_id=plan_id,
                                                                 accepting_only=accepting_only).items() if v},
            "api_records": len(data.get("data") or []), "matches": len(results), "results": results[:limit],
            "excluded": excluded,
            "excluded_note": ("Records without per-plan data (most facility records) have unknown plan participation "
                              "and are excluded when plan_id or accepting_only is used; search without those filters "
                              "and confirm with the plan.") if excluded.get("unknown_plan_membership") else None,
            "truncated": len(results) > limit, "source_url": url, "retrieved_at": resp.fetched_at,
            "source": "MedCare Partners public provider search (the API behind mcpartnerstx.com/provider-directory-search/)",
            "caution": ("The site states: 'Provider information may change. Please confirm with the provider that they "
                        "participate in your MedCare Partners Health Plan of Texas plan before receiving care.' "
                        "Network status here is the plan's published directory data, not a guarantee of availability.")}


def npi_lookup(npi: str = "", first_name: str = "", last_name: str = "", organization: str = "",
               state: str = "TX", limit: int = 10) -> dict:
    params = {"version": "2.1", "limit": str(max(1, min(int(limit), 50)))}
    if npi:
        if not re.fullmatch(r"\d{10}", npi):
            raise ValueError("NPI must be 10 digits.")
        params["number"] = npi
    else:
        for k, v in (("first_name", first_name), ("last_name", last_name), ("organization_name", organization)):
            if v:
                if not re.fullmatch(r"[A-Za-z .,'&\-*]{2,60}", v):
                    raise ValueError(f"{k}: letters only.")
                params[k] = v
        if len(params) == 2:
            raise ValueError("Give an NPI, or a provider last name / organization name.")
        if state:
            params["state"] = state.upper()[:2]
    url = NPPES_API + "?" + "&".join(f"{k}={quote_plus(v)}" for k, v in params.items())
    resp = net.fetch(url, hosts=frozenset({"npiregistry.cms.hhs.gov"}), accept="application/json")
    data = resp.json()
    results = []
    for r in data.get("results") or []:
        b = r.get("basic", {})
        results.append({
            "npi": r.get("number"), "type": r.get("enumeration_type"),
            "name": b.get("organization_name") or " ".join(x for x in (b.get("first_name"), b.get("middle_name"),
                                                                       b.get("last_name")) if x),
            "credential": b.get("credential"), "status": b.get("status"), "last_updated": b.get("last_updated"),
            "taxonomies": [{"code": t.get("code"), "desc": t.get("desc"), "primary": t.get("primary"),
                            "state": t.get("state")} for t in r.get("taxonomies") or []],
            "practice_locations": [{"address": a.get("address_1"), "city": a.get("city"), "state": a.get("state"),
                                    "zip": (a.get("postal_code") or "")[:5], "phone": a.get("telephone_number")}
                                   for a in r.get("addresses") or [] if a.get("address_purpose") == "LOCATION"]})
    return {"query": {k: v for k, v in params.items() if k != "version"}, "count": len(results), "results": results,
            "source_url": url, "retrieved_at": resp.fetched_at,
            "caution": "NPPES confirms identity and self-reported taxonomy only. It does not show plan network participation."}


# ---------------------------------------------------------------- pharmacies
_PHARM_ROW = re.compile(
    r"(?P<lead>.+)\s{2,}(?P<city>[A-Z][A-Z .'\-]+?)\s{2,}(?P<county>[A-Z][A-Z .'\-]+?)\s{2,}(?P<state>[A-Z]{2})\s{2,}"
    r"(?P<zip>\d{5})(?:-\d{4})?\s{2,}(?P<phone>\(\d{3}\)-\d{3}-\d{4})")


@lru_cache(maxsize=1)
def pharmacy_entries() -> dict:
    s = catalog._load()
    res = next((r for r in s["catalog"]["resources"] if r["category"] == "pharmacy_directory"
                and r["status"] == "available" and not r.get("unlinked_media") and (r.get("url") or "").endswith(".pdf")), None)
    if not res:
        return {"entries": [], "resource": None}
    doc = s["docs"].get(res["document"]["sha256"])
    entries, section = [], "Retail"
    for pno, text in enumerate(doc["pages"], 1):
        buf = []
        for ln in text.splitlines():
            raw = ln.rstrip()
            st = raw.strip()
            if re.fullmatch(r"[A-Za-z /()&]+ - Pharmacies", st):
                section = st
                buf = []
                continue
            if st.startswith("Pharmacy Name") or not st:
                continue
            buf.append(raw)
            joined = "  ".join(x.strip() for x in buf)
            m = _PHARM_ROW.search(joined)
            if m:
                lead = m.group("lead").strip()
                entries.append({"name_and_street": re.sub(r"\s{2,}", " | ", lead), "city": m.group("city").title(),
                                "county": m.group("county").title(), "state": m.group("state"), "zip": m.group("zip"),
                                "phone": m.group("phone").replace(")-", ") "), "section": section, "page": pno})
                buf = []
            elif len(buf) > 6:
                buf = buf[-3:]
    return {"entries": entries, "resource": res}


def pharmacy_search(query: str = "", zip_code: str = "", city: str = "", limit: int = 25) -> dict:
    data = pharmacy_entries()
    res = data["resource"]
    if not res:
        return {"status": "unavailable", "detail": "No indexed pharmacy directory PDF in the current catalog."}
    rows = data["entries"]
    if zip_code:
        if not re.fullmatch(r"\d{3,5}", zip_code):
            raise ValueError("zip_code: 3-5 digits (prefix allowed).")
        rows = [r for r in rows if r["zip"].startswith(zip_code)]
    if city:
        rows = [r for r in rows if fold(city) in fold(r["city"])]
    if query:
        q = tokens(query)
        rows = [r for r in rows if all(t in fold(r["name_and_street"] + " " + r["section"]) for t in q)]
    return {"filters": {k: v for k, v in dict(query=query, zip_code=zip_code, city=city).items() if v},
            "matches": len(rows), "results": rows[:limit], "truncated": len(rows) > limit,
            "parsed_entries_total": len(data["entries"]),
            "source": {"document": res["title"], "url": res["url"], "sha256": res["document"]["sha256"],
                       "fetched_at": res["document"]["fetched_at"]},
            "caution": ("Parsed from the plan's published Pharmacy Directory PDF (snapshot); name and street are kept "
                        "together as printed. Confirm network status with the plan or pharmacy before filling. The "
                        "site's 'Find a Pharmacy' search button currently links to a sample PDF, so no live pharmacy "
                        "search exists on the site.")}
