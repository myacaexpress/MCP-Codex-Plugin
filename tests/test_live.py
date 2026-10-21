"""Live end-to-end tests through a real MCP client session (official `mcp` SDK) against
live public sources, with synthetic queries only. Run with MEDCARE_LIVE_TESTS=1.
Writes tests/live-report.json summarising every call."""
import asyncio
import json
import os
import time

import pytest

from conftest import PLUGIN, ROOT

pytestmark = pytest.mark.skipif(os.environ.get("MEDCARE_LIVE_TESTS") != "1", reason="set MEDCARE_LIVE_TESTS=1")


def _sdk_ok():
    try:
        from importlib.metadata import version
        return int(version("mcp").split(".")[0]) < 2
    except Exception:
        return False


if os.environ.get("MEDCARE_LIVE_TESTS") == "1" and not _sdk_ok():
    raise RuntimeError("Live tests need the pinned MCP SDK v1: pip install -r tests/requirements-test.txt")

SYNTHETIC_LIST = """Medications (synthetic test list):
1. Lisinopril 10 mg tablet daily
2) Metformin ER 500mg - take 1 tab twice daily
- Eliquis 5 mg BID
• atorvastatin 40 mg, furosemide 40 mg
* Losartan-HCTZ 50-12.5 mg daily
Levothyroxine 25 mcg tablet; omeprazole 20 mg DR capsule
Lantus SoloStar 100 units/mL pen
amlodipine 5 mg tablet | Toprol XL 50 mg
hydrocodone/acetaminophen 5/325 mg q6h prn
gabapentin 300 mg capsule
tamsulosin 0.4 mg capsule
sertraline 50 mg tablet
pantoprazole 40 mg DR tablet
Ozempic 1 mg weekly
Lisinopril 10 mg tablet daily
Xyzzaprel 10 mg
lisinopril 15 mg
"""


def _batch_ok(r):
    res = r["results"]
    by = {x["original"]: x for x in res}
    assert r["entries_total"] == len(res) == 20, r["entries_total"]
    assert [x["index"] for x in res] == list(range(1, 21))
    eli = by["- Eliquis 5 mg BID"]
    assert eli["status"] == "matched" and all(p["coverage"] == "covered" and p["tier"] == 3 for p in eli["plans"]
                                              if p["requested_plan_id"].startswith("H5767"))
    assert all(p["year"] == 2027 and p["plan_id"].count("-") == 2 for p in eli["plans"])
    assert "hydrochlorothiazide 12.5 MG / losartan potassium 50 MG Oral Tablet" in json.dumps(
        by["* Losartan-HCTZ 50-12.5 mg daily"]["matched_products"])
    assert by["Xyzzaprel 10 mg"]["status"] == "unrecognized"
    assert by["lisinopril 15 mg"]["status"] == "needs_clarification"
    assert by["Lisinopril 10 mg tablet daily"]["duplicate_of"] == 1
    assert r["sources"]["cms_part_d_file"]["file_date"]
    return True


CALLS = [
    ("medication_list_check", {"text": SYNTHETIC_LIST, "plan_ids": ["H5767-001", "H5767-002", "H5767-003", "H7993-030"],
                               "year": 2027}, _batch_ok),
    ("source_status", {"check_cms": True}, lambda r: r["cms_landscape_2027"]["data_last_updated"]),
    ("medcare_site_audit", {}, lambda r: r["summary"]["identity_mismatches"] >= 1),
    ("medcare_search_resources", {"query": "appointment of representative"}, lambda r: r["resources"]),
    ("medcare_list_resources", {"category": "summary_of_benefits"}, lambda r: r["count"] >= 6),
    ("medcare_search_documents", {"query": "maximum out-of-pocket", "plan_id": "H5767-001", "category": "summary_of_benefits"},
     lambda r: "1,500" in json.dumps(r["results"])),
    ("medcare_contacts", {"query": "pharmacy"}, lambda r: r["contacts"]),
    ("medcare_drug_lookup", {"drug_name": "Eliquis"}, lambda r: r["live_formulary_navigator"]["match_count"] > 0
     and r["cost_sharing_by_tier"]["H5767-001"]["initial_coverage_by_tier"]),
    ("medcare_drug_lookup", {"drug_name": "Ozempic", "include_costs": False},
     lambda r: any("Prior Authorization" in (m.get("restrictions") or []) for m in r["live_formulary_navigator"]["matches"])),
    ("medcare_provider_search", {"provider_type": "primary_care", "zip_code": "77072", "limit": 5},
     lambda r: r["matches"] > 0),
    ("medcare_provider_search", {"provider_type": "specialist", "specialty": "cardio", "limit": 5}, lambda r: r["matches"] > 0),
    ("medcare_provider_search", {"provider_type": "Skilled Nursing Facility", "limit": 3}, lambda r: r["matches"] > 0),
    ("medcare_pharmacy_search", {"zip_code": "77072"}, lambda r: r["matches"] > 0),
    ("medcare_benefit_directory", {"benefit": "dental"}, lambda r: r["vendor_and_site_links"] and r["summary_of_benefits_by_plan"]["H5767-001"]),
    ("medcare_plan_overview", {"year": 2027}, lambda r: len(r["plans"]) == 3),
    ("zip_to_county", {"zip_code": "77072"}, lambda r: r["counties"][0]["county"] == "Harris"),
    ("cms_find_plans", {"year": 2027, "zip_code": "77072", "snp": "c-snp"}, lambda r: r["count"] >= 5),
    ("cms_plan_details", {"plan_id": "H5767-002", "year": 2027, "zip_code": "77072"},
     lambda r: r["service_area_check"][0]["plan_offered_in_county_per_cms"]),
    ("cms_drug_coverage", {"drug_name": "metformin", "plan_ids": ["H5767-001", "H7993-030"], "year": 2027},
     lambda r: all(p["found"] for p in r["plans"])),
    ("cms_tier_cost_sharing", {"plan_id": "H5767-003", "year": 2027}, lambda r: r["found"]),
    ("npi_lookup", {"npi": "1972121416"}, lambda r: r["count"] == 1),
    ("plan_document_discover", {"page_url": "https://www.devoted.com/find-a-plan/harris-tx-45610/", "plan_id": "H7993-030",
                                "year": 2027}, lambda r: r["candidates"]),
    ("plan_document_retrieve", {"url": "https://assets.devoted.com/plan-documents/2027/2027-DEVOTED-C-SNP-ENHANCED-030-TX-%28HMO-C-SNP%29-SB-H7993-030-ENG.pdf",
                                "plan_id": "H7993-030", "year": 2027}, lambda r: r["status"] == "verified_document"),
    ("plan_document_retrieve", {"url": "https://mcpartnerstx.com/wp-content/uploads/2026/10/H5767_SB0032027SP_M.pdf",
                                "plan_id": "H5767-003", "year": 2027}, lambda r: r["status"] == "unverified_document"),
    ("plan_document_search", {"plan_id": "H7993-030", "year": 2027, "query": "dental"}, lambda r: r["results"]),
    ("plan_document_search", {"plan_id": "H7993-030", "year": 2026, "query": "dental"},
     lambda r: r["status"] == "no_verified_document"),
    ("plan_document_search", {"plan_id": "H5767-001", "year": 2026, "query": "monthly premium"},
     lambda r: r["status"] == "no_verified_document"),
    ("plan_document_search", {"plan_id": "H5767-001", "year": 2027, "query": "monthly premium"},
     lambda r: r["results"] and all(d["status"] == "verified_document" for d in r["documents_searched"])),
    ("cms_find_plans", {"year": 2027, "zip_code": "77477"}, lambda r: r.get("status") == "county_required"
     or r.get("count", 0) > 0),
    ("medcare_provider_search", {"provider_type": "specialist", "plan_id": "H5767-001", "accepting_only": True, "limit": 5},
     lambda r: all(all(e["plan_id"].startswith("H5767-001") and e["accepting_new_patients"].lower().startswith("accepting")
                       for e in p["plans"]) for p in r["results"])),
    ("cms_refresh_data", {"year": 2027, "dataset": "landscape"}, lambda r: r["landscape"]["after"]["last_checked_at"]),
    ("compare_plans", {"plan_ids": ["H5767-002", "H7993-030"], "year": 2027, "topics": ["primary care", "dental"],
                       "drugs": ["Eliquis"]}, lambda r: len(r["plans"]) == 2),
    ("plan_eligibility_criteria", {"plan_id": "H5767-002", "year": 2027, "zip_code": "77072", "has_part_a": True,
                                   "has_part_b": True, "qualifying_conditions": ["diabetes"]},
     lambda r: r["personal_eligibility_verified"] is False and r["criteria"][1]["assessment"] == "appears_met"),
    ("plan_eligibility_criteria", {"plan_id": "H4513-061", "year": 2027}, lambda r: r["status"] == "segment_required"),
    ("plan_eligibility_criteria", {"plan_id": "H4513-061", "year": 2027, "county": "Dallas", "state": "TX"},
     lambda r: r["plan"]["plan_id"] == "H4513-061-005" and r["segment_resolution"]["method"] == "resolved_by_location"),
    ("compare_plans", {"plan_ids": ["H4513-061"], "year": 2027, "topics": ["dental"]},
     lambda r: r["plans"][0]["cms"]["status"] == "segment_required"),
    ("plan_eligibility_criteria", {"plan_id": "H5767-002", "year": 2027, "county": "Fort Bend", "state": "TX"},
     lambda r: r["criteria"][1]["assessment"] == "appears_not_met"),
]


async def _run():
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client
    params = StdioServerParameters(command=str(PLUGIN / "bin" / "medcare-mcp"), args=[], cwd=str(PLUGIN),
                                   env={**os.environ})
    report = []
    async with stdio_client(params) as (r, w):
        async with ClientSession(r, w) as s:
            init = await s.initialize()
            tools = (await s.list_tools()).tools
            report.append({"step": "initialize", "server": init.serverInfo.name, "protocol": init.protocolVersion,
                           "tools": len(tools)})
            # verify one live resource and read a page using ids from the catalog
            res = await s.call_tool("medcare_search_resources", {"query": "Summary of Benefits MedCare Classic English"})
            rid = next(x["id"] for x in res.structuredContent["resources"]
                       if (x.get("url") or "").endswith("H5767_SB0012027_M.pdf"))
            extra = [("medcare_verify_resource", {"resource_id": rid}, lambda r: r["live_status"] == "unchanged"),
                     ("medcare_read_document_page", {"resource_id": rid, "page": 9}, lambda r: "Part D" in r["text"]),
                     ("medcare_get_resource", {"resource_id": rid}, lambda r: r["resource"]["found_on"])]
            for name, args, check in CALLS + extra:
                t = time.time()
                out = await s.call_tool(name, args)
                data = out.structuredContent or {}
                ok = (not out.isError) and bool(check(data))
                report.append({"tool": name, "args": args, "ok": ok, "is_error": out.isError,
                               "seconds": round(time.time() - t, 2),
                               "error_text": out.content[0].text[:300] if out.isError else None})
    return report


def test_live_all_tools():
    report = asyncio.run(_run())
    (ROOT / "tests" / "live-report.json").write_text(json.dumps(report, indent=1))
    failed = [r for r in report[1:] if not r["ok"]]
    assert report[0]["tools"] >= 27
    assert not failed, json.dumps(failed, indent=1)
