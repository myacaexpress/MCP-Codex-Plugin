"""Regression tests for the independent release review of 1.0.0 (findings 1-9) and follow-ups."""
import importlib
import json
import os
import subprocess
import sys

import pytest

from conftest import PLUGIN, ROOT
from medcare import catalog, cms, lookups, research
from medcare.mcp_stdio import Server, schema

SB_2027 = ["2027 Summary of Benefits\nExample Plan (HMO) H1234-001\nJanuary 1, 2027 - December 31, 2027\nPrimary care $0"]


def fake_landscape(segments_by_pbp, plans=None):
    def _f(year, plan_id):
        c, p, s = cms.parse_plan_id(plan_id)
        segs = segments_by_pbp.get((year, f"{c}-{p}"), [])
        return {"segments_in_landscape": segs, "found": bool(segs),
                "plans": plans or [{"plan_id": x} for x in segs]}
    return _f


# ---- Finding 1: wrong benefit year returned as verified
def test_f1_bundled_medcare_docs_not_verified_for_other_year():
    r = research.plan_document_search("H5767-001", 2026, "monthly premium")
    assert r["status"] == "no_verified_document"
    assert all(d["status"] != "verified_document" for d in r["documents_seen"])
    ok = research.plan_document_search("H5767-001", 2027, "monthly premium")
    assert ok["results"] and all(d["status"] == "verified_document" for d in ok["documents_searched"])


def test_f1_compare_reports_unknown_for_wrong_year():
    c = research.compare_plans(["H5767-001"], 2026, topics=["monthly premium"])
    assert c["plans"][0]["benefits_by_topic"]["monthly premium"]["status"] == "unknown_no_verified_document"


# ---- Finding 2: wrong competitor segment returned from cache
def test_f2_cached_document_segment_must_match_exactly(monkeypatch, tmp_path):
    monkeypatch.setattr(research, "_sb_dir", lambda: tmp_path)
    monkeypatch.setattr(cms, "plan_landscape", fake_landscape({(2027, "H1234-001"): ["H1234-001-001", "H1234-001-002"]}))
    doc = {"url": "https://x/sb.pdf", "document_type": "summary_of_benefits", "sha256": "a" * 64,
           "status": "verified_document", "pages": ["2027 Summary of Benefits H1234-001-001 dental $0"],
           "retrieved_at": "t", "verification": {}, "verified_plan_id": "H1234-001-001", "verified_year": 2027,
           "plan_id": "H1234-001-001"}
    (tmp_path / "H1234-001-001_2027_summary_of_benefits_aaaa.json").write_text(json.dumps(doc))
    assert research.plan_document_search("H1234-001-002", 2027, "dental")["status"] == "no_verified_document"
    assert research.plan_document_search("H1234-001", 2027, "dental")["status"] == "no_verified_document"
    assert research.plan_document_search("H1234-001-001", 2027, "dental")["results"]


def test_f2_multi_segment_pdf_is_not_auto_verified(monkeypatch):
    monkeypatch.setattr(cms, "plan_landscape", fake_landscape({(2027, "H1234-001"): ["H1234-001-001", "H1234-001-002"]}))
    pages = ["2027 Summary of Benefits\nPlan A H1234-001-001 and Plan B H1234-001-002\nJanuary 1, 2027"]
    v = research._verify_identity(pages, "H1234-001-002", 2027)
    assert not v["identity_verified"] and v["segment_check"] == "multiple_segments_in_document_column_review_required"
    v = research._verify_identity(pages, "H1234-001", 2027)
    assert not v["identity_verified"] and v["segment_check"] == "segment_required_multiple_segments_in_cms"


# ---- Finding 3: conflicting document years still pass
def test_f3_conflicting_title_years_are_unverified(monkeypatch):
    monkeypatch.setattr(cms, "plan_landscape", fake_landscape({(2027, "H1234-001"): ["H1234-001-000"],
                                                               (2026, "H1234-001"): ["H1234-001-000"]}))
    pages = ["2026 Summary of Benefits\nExample Plan H1234-001\nSee also the 2027 Summary of Benefits\nJanuary 1, 2026"]
    for y in (2026, 2027):
        v = research._verify_identity(pages, "H1234-001", y)
        assert not v["identity_verified"] and v["year_resolution"]["status"] == "ambiguous_conflicting_years"
    assert research._verify_identity(SB_2027, "H1234-001", 2027)["identity_verified"]
    assert not research._verify_identity(SB_2027, "H1234-001", 2026)["identity_verified"]


def test_f3_effective_dates_only_conflicting_is_ambiguous():
    yr = research.resolve_year(["Plan H1234-001 benefits January 1, 2026 ... January 1, 2027"])
    assert yr["status"] == "ambiguous_conflicting_years" and yr["year"] is None


def test_f3_real_medcare_documents_resolve_to_2027():
    s = catalog._load()
    for d in s["docs"].values():
        if d["category"] in ("summary_of_benefits", "evidence_of_coverage"):
            yr = research.resolve_year(d["pages"])
            assert yr["year"] == "2027", (d["url"], yr)


# ---- Finding 4: provider acceptance filter mixes plans
PROVIDER = {"npi": "1111111111", "name": "Synthetic Provider", "plans": [
    {"plan_id": "H5767-001-000", "accepting_new_patients": "Not Accepting", "specialties": [{"name": "Cardiology"}]},
    {"plan_id": "H5767-002-000", "accepting_new_patients": "Accepting", "specialties": [{"name": "Cardiology"}]}]}
FACILITY = {"npi": "2222222222", "name": "Synthetic Facility", "provider_type": "Radiology"}


def test_f4_plan_and_acceptance_apply_to_same_entry():
    out, ex = lookups.filter_providers([PROVIDER, FACILITY], plan_id="H5767-001", accepting_only=True)
    assert out == [] and ex["unknown_plan_membership"] == 1 and ex["no_matching_plan_entry"] == 1
    out, _ = lookups.filter_providers([PROVIDER], plan_id="H5767-002", accepting_only=True)
    assert [e["plan_id"] for e in out[0]["plans"]] == ["H5767-002-000"]
    out, _ = lookups.filter_providers([PROVIDER, FACILITY], plan_id="H5767-001")
    assert [p["npi"] for p in out] == ["1111111111"] and len(out[0]["plans"]) == 1
    out, _ = lookups.filter_providers([PROVIDER, FACILITY], accepting_only=True)
    assert [p["npi"] for p in out] == ["1111111111"]
    out, _ = lookups.filter_providers([PROVIDER, FACILITY])
    assert len(out) == 2


# ---- Finding 5: PDP Part A OR Part B
def _criteria(monkeypatch, category, **facts):
    plan = {"plan_id": "S1234-001-000", "plan_name": "Synthetic PDP", "category": category, "snp": "No",
            "service_area": {"TX": ["All Counties"]}, "snp_type": "Not Applicable"}
    monkeypatch.setattr(cms, "plan_landscape", lambda y, p: {"found": True, "plans": [plan],
                                                             "segments_in_landscape": [plan["plan_id"]]})
    monkeypatch.setattr(cms, "landscape_source", lambda y: {"data_last_updated": "synthetic"})
    r = research.plan_eligibility_criteria("S1234-001", 2027, **facts)
    return r["criteria"][0]["assessment"]


def test_f5_pdp_or_semantics_and_ma_and_semantics(monkeypatch):
    assert _criteria(monkeypatch, "PDP", has_part_a=False, has_part_b=True) == "appears_met"
    assert _criteria(monkeypatch, "PDP", has_part_a=True, has_part_b=None) == "appears_met"
    assert _criteria(monkeypatch, "PDP", has_part_a=False, has_part_b=False) == "appears_not_met"
    assert _criteria(monkeypatch, "PDP", has_part_a=False, has_part_b=None) == "unknown"
    assert _criteria(monkeypatch, "MA-PD", has_part_a=False, has_part_b=True) == "appears_not_met"
    assert _criteria(monkeypatch, "MA-PD", has_part_a=True, has_part_b=None) == "unknown"
    assert _criteria(monkeypatch, "MA-PD", has_part_a=True, has_part_b=True) == "appears_met"


# ---- Finding 6: ambiguous ZIP treated as confirmed service area
def test_f6_zip_spanning_covered_and_uncovered_counties_is_unknown(monkeypatch):
    monkeypatch.setattr(cms, "zip_counties", lambda z: {"counties": [
        {"county": "Harris", "state": "TX", "share_of_zcta_land_area": 0.6},
        {"county": "Fort Bend", "state": "TX", "share_of_zcta_land_area": 0.4}]})
    area = {"TX": ["Harris"]}
    r = research.service_area_assessment(area, zip_code="77000")
    assert r["assessment"] == "unknown_zip_spans_covered_and_uncovered_counties"
    assert research.service_area_assessment(area, zip_code="77000", county="Harris", state="TX")["assessment"] == "appears_met"
    assert research.service_area_assessment(area, zip_code="77000", county="Fort Bend", state="TX")["assessment"] == "appears_not_met"
    assert research.service_area_assessment({"TX": ["Harris", "Fort Bend"]}, zip_code="77000")["assessment"] == "appears_met"
    assert research.service_area_assessment({"TX": ["Dallas"]}, zip_code="77000")["assessment"] == "appears_not_met"


def test_f6_find_plans_requires_county_for_multi_county_zip(monkeypatch):
    sys.path.insert(0, str(PLUGIN))
    server = importlib.import_module("server")
    monkeypatch.setattr(cms, "zip_counties", lambda z: {"counties": [
        {"county": "Harris", "state": "TX"}, {"county": "Fort Bend", "state": "TX"}]})
    r = server.cms_find_plans(2027, zip_code="77000")
    assert r["status"] == "county_required" and len(r["candidate_counties"]) == 2


# ---- Finding 7: CMS cache refresh policy
def test_f7_stale_cache_is_rechecked_and_rebuilt_when_cms_changes(monkeypatch, tmp_path):
    monkeypatch.setattr(cms, "_cms_dir", lambda: tmp_path)
    builds = []

    def fake_build(year, url, target):
        import sqlite3
        builds.append(url)
        target.unlink(missing_ok=True)
        c = sqlite3.connect(target)
        c.execute("create table meta(key text primary key, value text)")
        c.executemany("insert into meta values (?,?)", [("http_last_modified", fake_build.lm), ("source_url", url),
                       ("retrieved_at", "2020-01-01T00:00:00+00:00"), ("last_checked_at", "2020-01-01T00:00:00+00:00")])
        c.commit(); c.close()
    fake_build.lm = "Mon, 01 Jan 2026 00:00:00 GMT"

    class Head:
        headers = {}
    monkeypatch.setattr(cms, "_build_landscape", fake_build)
    monkeypatch.setattr(cms, "_discover_landscape", lambda y: "https://www.cms.gov/files/zip/cy2027-landscape-202610.zip")
    monkeypatch.setattr(cms.net, "fetch", lambda *a, **k: type("R", (), {"headers": {"last-modified": fake_build.lm}})())
    cms.landscape_db(2027)                      # initial build
    assert len(builds) == 1
    cms.landscape_db(2027)                      # stale (2020) -> remote check, unchanged -> no rebuild
    assert len(builds) == 1
    meta = cms._meta(cms.landscape_db(2027))
    assert meta["last_checked_at"] > "2026"     # check recorded; now fresh, no further remote call
    fake_build.lm = "Tue, 20 Oct 2026 20:12:53 GMT"  # CMS revised the same file name
    cms.landscape_db(2027, refresh=True)
    assert len(builds) == 2


def test_f7_offline_refresh_keeps_cache_and_discloses(monkeypatch, tmp_path):
    import sqlite3
    monkeypatch.setattr(cms, "_cms_dir", lambda: tmp_path)
    p = tmp_path / "landscape_v2_2027_202610.sqlite"
    c = sqlite3.connect(p)
    c.execute("create table meta(key text primary key, value text)")
    c.execute("insert into meta values ('retrieved_at','2020-01-01T00:00:00+00:00')")
    c.commit(); c.close()

    def boom(y):
        raise cms.net.FetchError("offline")
    monkeypatch.setattr(cms, "_discover_landscape", boom)
    cms.landscape_db(2027)
    assert "offline" in cms._REFRESH_ERRORS[("landscape", 2027)]
    assert cms._freshness(cms._meta(cms._connect(p)), "landscape", 2027)["last_refresh_error"]


def test_f7_refresh_tool_listed():
    names = [json.loads(l)["result"] for l in _rpc([{"jsonrpc": "2.0", "id": 1, "method": "tools/list"}]).stdout.splitlines()]
    assert "cms_refresh_data" in {t["name"] for t in names[0]["tools"]}


# ---- Finding 8: malformed MCP requests
def _rpc(msgs, raw=None, timeout=60):
    data = raw if raw is not None else "\n".join(json.dumps(m) for m in msgs) + "\n"
    return subprocess.run([str(PLUGIN / "bin" / "medcare-mcp")], input=data, capture_output=True, text=True,
                          timeout=timeout, cwd=str(PLUGIN))


def test_f8_malformed_requests_always_get_responses_and_server_survives():
    msgs = [
        {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": []},
        {"jsonrpc": "2.0", "id": 2, "method": "tools/call", "params": {"name": ["x"], "arguments": {}}},
        {"jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": {"name": "medcare_contacts", "arguments": []}},
        {"jsonrpc": "2.0", "id": 4, "method": "tools/call", "params": "nope"},
        {"jsonrpc": "2.0", "id": 5, "method": 42},
        {"jsonrpc": "2.0", "id": {"bad": 1}, "method": "ping"},
        {"jsonrpc": "1.0", "id": 7, "method": "ping"},
        {"jsonrpc": "2.0", "id": 8, "method": "tools/call", "params": {"name": "medcare_search_resources",
                                                                      "arguments": {"query": {"x": 1}}}},
        {"jsonrpc": "2.0", "id": 9, "method": "ping"},
    ]
    proc = _rpc(msgs, raw="\n".join(json.dumps(m) for m in msgs) + "\n{not json\n[]\n")
    out = [json.loads(l) for l in proc.stdout.splitlines() if l.strip()]
    by_id = {}
    for m in out:
        by_id.setdefault(json.dumps(m.get("id")), m)
    for i in (1, 2, 3, 4, 5):
        assert by_id[str(i)]["error"]["code"] in (-32602, -32600), by_id[str(i)]
    assert by_id["7"]["error"]["code"] == -32600
    assert by_id["8"]["result"]["isError"] is True
    assert by_id["9"]["result"] == {}
    assert sum(1 for m in out if m.get("id") is None and m.get("error")) >= 3  # bad id, parse error, empty batch
    assert proc.returncode == 0


def test_f8_validator_rejects_wrong_container_types():
    srv = Server("t", "0")
    sch = schema({"q": {"type": "string"}}, ["q"])
    from medcare.mcp_stdio import ToolError
    with pytest.raises(ToolError):
        srv._validate(sch, ["q"])


# ---- Finding 9: transactional installer
def test_f9_installer_rolls_back_code_and_marketplace(monkeypatch, tmp_path):
    sys.path.insert(0, str(ROOT))
    inst = importlib.import_module("install")
    home = tmp_path / "home"
    first = inst.install(home, replace=False)
    target = home / ".codex/plugins/medcare-resources"
    market = home / ".agents/plugins/marketplace.json"
    (target / "MARKER_OLD").write_text("old")
    before_market = market.read_bytes()

    def fail(path, data):
        raise OSError("simulated marketplace rename failure")
    monkeypatch.setattr(inst, "_atomic_write", fail)
    with pytest.raises(OSError):
        inst.install(home, replace=True)
    assert (target / "MARKER_OLD").exists(), "old plugin code restored"
    assert market.read_bytes() == before_market, "marketplace restored"
    assert not list(target.parent.glob(".medcare-resources*")), "no staging/rollback leftovers"


def test_f9_first_install_failure_leaves_nothing(monkeypatch, tmp_path):
    sys.path.insert(0, str(ROOT))
    inst = importlib.import_module("install")
    home = tmp_path / "home"
    monkeypatch.setattr(inst, "_atomic_write", lambda p, d: (_ for _ in ()).throw(OSError("fail")))
    with pytest.raises(OSError):
        inst.install(home, replace=False)
    assert not (home / ".codex/plugins/medcare-resources").exists()
    assert not (home / ".agents/plugins/marketplace.json").exists()


# ---- Follow-ups: Windows constraint, handoff labelling, SDK pin
def test_windows_is_explicitly_unsupported(monkeypatch, tmp_path):
    sys.path.insert(0, str(ROOT))
    inst = importlib.import_module("install")
    monkeypatch.setattr(inst.os, "name", "nt")
    monkeypatch.setattr(sys, "argv", ["install.py", "--home", str(tmp_path)])
    with pytest.raises(SystemExit) as e:
        inst.main()
    assert "Windows is not supported" in str(e.value)
    assert "Windows" in (ROOT / "README.md").read_text() and "not supported" in (ROOT / "README.md").read_text()


def test_browser_handoffs_are_labelled():
    link = research.medicare_gov_link("H5767-001", 2027, "77072")
    assert link["access_mode"] == "browser_handoff" and link["retrieved_by_tool"] is False and link["evidence"] is False
    vendors = catalog.list_resources(category="dental")["resources"]
    ext = [r for r in vendors if (r.get("url") or "").startswith("http") and "mcpartnerstx" not in r["url"]]
    assert ext and all(r["access_mode"] == "browser_handoff" for r in ext)
    nav = catalog.search_resources("Search Drug List")["resources"]
    assert any(r["access_mode"] == "automated_live:medcare_drug_lookup" for r in nav)
    placeholders = catalog.list_resources(status="unavailable")["resources"]
    assert placeholders and all(r["access_mode"] == "unavailable" for r in placeholders)


def test_test_sdk_is_pinned():
    req = (ROOT / "tests" / "requirements-test.txt").read_text()
    assert "mcp>=1.20,<2" in req
