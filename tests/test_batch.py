"""Offline, deterministic tests for medication_list_check (synthetic RxNorm + synthetic CMS formulary)."""
import json
import sqlite3
import threading
import time

import pytest

from medcare import batch, cms, net

P = lambda rx, tty, name: {"rxcui": rx, "tty": tty, "name": name}
PRODUCTS = {
    "lisinopril": [P("1", "SCD", "lisinopril 10 MG Oral Tablet"), P("2", "SCD", "lisinopril 20 MG Oral Tablet"),
                   P("3", "SBD", "lisinopril 10 MG Oral Tablet [Zestril]"),
                   P("4", "SCD", "hydrochlorothiazide 12.5 MG / lisinopril 10 MG Oral Tablet")],
    "metformin": [P("10", "SCD", "metformin hydrochloride 500 MG Oral Tablet"),
                  P("11", "SCD", "24 HR metformin hydrochloride 500 MG Extended Release Oral Tablet"),
                  P("12", "SCD", "metformin hydrochloride 1000 MG / sitagliptin 50 MG Oral Tablet")],
    "eliquis": [P("20", "SBD", "apixaban 5 MG Oral Tablet [Eliquis]"), P("21", "SBD", "apixaban 2.5 MG Oral Tablet [Eliquis]")],
    "apixaban": [P("22", "SCD", "apixaban 5 MG Oral Tablet"), P("20", "SBD", "apixaban 5 MG Oral Tablet [Eliquis]")],
    "losartan": [P("30", "SCD", "losartan potassium 50 MG Oral Tablet"),
                 P("31", "SCD", "hydrochlorothiazide 12.5 MG / losartan potassium 50 MG Oral Tablet"),
                 P("32", "SBD", "hydrochlorothiazide 12.5 MG / losartan potassium 50 MG Oral Tablet [Hyzaar]")],
    "levothyroxine": [P("40", "SCD", "levothyroxine sodium 0.025 MG Oral Tablet"),
                      P("41", "SCD", "levothyroxine sodium 0.025 MG Oral Capsule")],
    "atorvastatin": [P("50", "SCD", "atorvastatin 40 MG Oral Tablet")],
    "omeprazole": [P("60", "SCD", "omeprazole 20 MG Delayed Release Oral Capsule")],
    "lantus": [P("70", "SBD", "3 ML insulin glargine 100 UNT/ML Pen Injector [Lantus]"),
               P("71", "SBD", "insulin glargine 100 UNT/ML Injectable Solution [Lantus]")],
    "hydrocodone": [P("80", "SCD", "acetaminophen 325 MG / hydrocodone bitartrate 5 MG Oral Tablet"),
                    P("81", "SCD", "hydrocodone bitartrate 10 MG 12 HR Extended Release Oral Capsule")],
    "gabapentin": [P("90", "SCD", "gabapentin 300 MG Oral Capsule"), P("91", "SCD", "gabapentin 300 MG Oral Tablet")],
    "furosemide": [P("95", "SCD", "furosemide 40 MG Oral Tablet")],
    "amlodipine": [P("96", "SCD", "amlodipine 5 MG Oral Tablet")],
    "simvastatin": [P("97", "SCD", "simvastatin 20 MG Oral Tablet")],
    "glipizide": [P("98", "SCD", "glipizide 5 MG Oral Tablet")],
    "sertraline": [P("99", "SCD", "sertraline 50 MG Oral Tablet")],
    "warfarin": [P("100", "SCD", "warfarin sodium 5 MG Oral Tablet")],
    "tamsulosin": [P("101", "SCD", "tamsulosin hydrochloride 0.4 MG Oral Capsule")],
    "carvedilol": [P("102", "SCD", "carvedilol 12.5 MG Oral Tablet")],
    "donepezil": [P("103", "SCD", "donepezil hydrochloride 10 MG Oral Tablet")],
    "pantoprazole": [P("104", "SCD", "pantoprazole 40 MG Delayed Release Oral Tablet")],
}
# formulary rows: rxcui -> (tier, PA, ST, QLyn, QLamt, QLdays)
FORMULARY = {"1": (1, "N", "N", "N", "", ""), "2": (1, "N", "N", "N", "", ""), "4": (1, "N", "N", "N", "", ""),
             "10": (1, "N", "N", "Y", "60", "30"), "11": (2, "N", "N", "Y", "120", "30"),
             "20": (3, "N", "N", "Y", "74", "30"), "31": (1, "N", "N", "N", "", ""), "30": (1, "N", "N", "N", "", ""),
             "40": (1, "N", "N", "N", "", ""), "41": (4, "Y", "N", "N", "", ""), "50": (1, "N", "N", "N", "", ""),
             "60": (1, "N", "N", "N", "", ""), "70": (3, "N", "N", "Y", "30", "28"), "80": (2, "N", "N", "Y", "240", "30"),
             "90": (1, "N", "N", "N", "", ""), "91": (1, "N", "N", "N", "", ""), "95": (1, "N", "N", "N", "", "")}
for rx in ("96", "97", "98", "99", "100", "101", "102", "103", "104"):
    FORMULARY[rx] = (1, "N", "N", "N", "", "")


@pytest.fixture
def synthetic(monkeypatch):
    calls = []
    lock = threading.Lock()

    def fake_rxnav(name):
        with lock:
            calls.append(name)
        if name.lower().startswith("slowdrug"):
            time.sleep(3)
        if name.lower().startswith("faildrug"):
            raise net.FetchError("synthetic upstream outage")
        key = name.lower().split()[0].split("-")[0].split("/")[0]
        return PRODUCTS.get(name.lower(), PRODUCTS.get(key, []))

    db = sqlite3.connect(":memory:", check_same_thread=False)
    db.row_factory = sqlite3.Row
    db.execute("create table formulary(FORMULARY_ID, RXCUI, TIER_LEVEL_VALUE, PRIOR_AUTHORIZATION_YN, "
               "STEP_THERAPY_YN, QUANTITY_LIMIT_YN, QUANTITY_LIMIT_AMOUNT, QUANTITY_LIMIT_DAYS)")
    db.execute("create table excluded(CONTRACT_ID, PLAN_ID, RXCUI)")
    for rx, (t, pa, st, ql, amt, days) in FORMULARY.items():
        db.execute("insert into formulary values ('F1',?,?,?,?,?,?,?)", (rx, str(t), pa, st, ql, amt, days))
    tiers = [{"coverage_stage": "initial coverage", "tier": t, "days_supply": d,
              "standard_retail": {"text": f"${t * 5}.00 copay"}, "standard_mail": {"text": f"${t * 10}.00 copay"},
              "preferred_retail": None, "deductible_applies": t >= 3, "specialty_tier": False}
             for t in range(1, 7) for d in ("30-day", "90-day")]

    def fake_ctx(year, pid):
        c, p, s = cms.parse_plan_id(pid)
        if c == "H9999":
            raise ValueError(f"{pid}: not in the CMS Part D file for {year}")
        return {"plan_id": f"{c}-{p}-{s or '000'}", "plan_name": f"Synthetic {p}", "year": year, "formulary_id": "F1",
                "part_d_deductible": 250.0, "tiers": tiers, "insulin": [], "contract": c, "pbp": p}

    monkeypatch.setattr(batch, "_rxnav", fake_rxnav)
    monkeypatch.setattr(batch, "_suggest", lambda n: ["synthetic-suggestion"])
    monkeypatch.setattr(batch, "_plan_context", fake_ctx)
    monkeypatch.setattr(batch.cms, "puf_db", lambda year, *a, **k: db)
    monkeypatch.setattr(batch, "_sources", lambda y, c: {"synthetic": True})
    return calls


def run(text="", meds=None, **kw):
    kw.setdefault("include_medcare_site", False)
    kw.setdefault("plan_ids", ["H1111-001", "H1111-002"])
    return batch.check_list(text, meds, **kw)


MIXED = """Home medications:
1. Lisinopril 10 mg tablet daily
2) Metformin ER 500mg - take 1 tab twice daily with meals
- Eliquis 5 mg BID
• atorvastatin 40 mg, furosemide 40 mg
* Losartan-HCTZ 50-12.5 mg daily
Levothyroxine 25 mcg tablet; omeprazole 20 mg DR capsule
Lantus SoloStar 100 units/mL pen
amlodipine 5mg | glipizide 5 mg\tsertraline 50 mg
hydrocodone/acetaminophen 5/325 mg q6h prn
#12 warfarin 5 mg
[ ] tamsulosin 0.4 mg capsule
a) carvedilol 12.5 mg
donepezil 10 mg, pantoprazole 40 mg DR tablet
metformin 500 mg, 1 tab twice daily
Lisinopril 10 mg tablet daily
gabapentin 300 mg capsule
simvastatin 20 mg
Xyzzaprel 10 mg
lisinopril 15 mg
"""


def test_mixed_formatting_order_and_one_result_per_entry(synthetic):
    r = run(MIXED)
    originals = [x["original"] for x in r["results"]]
    assert len(originals) == r["entries_total"] == 24
    assert originals[0].startswith("1. Lisinopril") and originals[4] == "furosemide 40 mg"
    assert originals[11] == "sertraline 50 mg" and originals[-1] == "lisinopril 15 mg"
    assert [x["index"] for x in r["results"]] == list(range(1, 25))
    assert sum(r["summary"]["by_status"].values()) == 24
    assert r["ignored_lines"][0]["reason"] == "header"
    table_rows = [l for l in r["table_markdown"].splitlines()[2:]]
    assert len(table_rows) == 24
    assert r["continuation"] is None


def test_exact_matching_without_substitution(synthetic):
    r = run(MIXED)
    by = {x["original"]: x for x in r["results"]}
    # release type respected
    met_er = by["2) Metformin ER 500mg - take 1 tab twice daily with meals"]
    assert [m["rxcui"] for m in met_er["matched_products"]] == ["11"]
    # unspecified release with different coverage -> clarification (not a guess)
    met = by["metformin 500 mg, 1 tab twice daily"]
    assert met["status"] == "needs_clarification" and {m["rxcui"] for m in met["matched_products"]} == {"10", "11"}
    # strength never substituted
    assert by["lisinopril 15 mg"]["status"] == "needs_clarification"
    assert by["lisinopril 15 mg"]["clarification"]["reason"] == "strength_not_found"
    # form respected; tablet-only for levothyroxine
    levo = by["Levothyroxine 25 mcg tablet"]
    assert [m["rxcui"] for m in levo["matched_products"]] == ["40"] and levo["status"] == "matched"
    # gabapentin capsule vs tablet
    assert [m["rxcui"] for m in by["gabapentin 300 mg capsule"]["matched_products"]] == ["90"]
    # brand intent stays brand
    eli = by["- Eliquis 5 mg BID"]
    assert [m["rxcui"] for m in eli["matched_products"]] == ["20"] and eli["plans"][0]["coverage"] == "covered"
    # pen form for Lantus
    assert [m["rxcui"] for m in by["Lantus SoloStar 100 units/mL pen"]["matched_products"]] == ["70"]


def test_combination_products_not_split_and_single_never_matches_combo(synthetic):
    r = run(MIXED)
    by = {x["original"]: x for x in r["results"]}
    combo = by["* Losartan-HCTZ 50-12.5 mg daily"]
    assert [m["rxcui"] for m in combo["matched_products"]] == ["31"]
    hc = by["hydrocodone/acetaminophen 5/325 mg q6h prn"]
    assert [m["rxcui"] for m in hc["matched_products"]] == ["80"]
    lis = by["1. Lisinopril 10 mg tablet daily"]
    assert [m["rxcui"] for m in lis["matched_products"]] == ["1"]          # not the HCTZ combo, not Zestril
    assert sum(1 for x in r["results"] if "Losartan" in x["original"]) == 1  # one entry, not two


def test_duplicates_share_lookup_but_each_entry_reported(synthetic):
    r = run(MIXED)
    dups = [x for x in r["results"] if x.get("duplicate_of")]
    assert dups and dups[0]["original"] == "Lisinopril 10 mg tablet daily" and dups[0]["duplicate_of"] == 1
    assert dups[0]["plans"][0]["coverage"] == "covered"
    assert sum(1 for c in synthetic if c == "Lisinopril") == 1


def test_generic_counterpart_and_unrecognized(synthetic):
    r = run("apixaban 5 mg tablet\nXyzzaprel 10 mg")
    a, x = r["results"]
    assert a["status"] == "needs_clarification"
    assert a["plans"][0]["coverage"] == "not_listed_but_counterpart_listed_needs_clarification"
    assert a["plans"][0]["counterpart_listed"][0]["rxcui"] == "20"
    assert x["status"] == "unrecognized" and x["clarification"]["spelling_suggestions"]


def test_cost_sharing_and_plan_year_provenance(synthetic):
    r = run("Eliquis 5 mg", year=2027, plan_ids=["H1111-001", "H1111-002-000"])
    it = r["results"][0]
    for p in it["plans"]:
        assert p["year"] == 2027 and p["plan_id"].count("-") == 2 and p["formulary_id"] == "F1"
        assert p["tier"] == 3 and p["quantity_limit"] == "74 per 30 days"
        assert p["cost_sharing"]["retail_30_day"] == "$15.00 copay" and p["cost_sharing"]["deductible_applies"]
    assert [p["requested_plan_id"] for p in it["plans"]] == ["H1111-001", "H1111-002-000"]


def test_invalid_plan_does_not_discard_others(synthetic):
    r = run("Eliquis 5 mg", plan_ids=["H1111-001", "H9999-001"])
    plans = {p["requested_plan_id"]: p for p in r["results"][0]["plans"]}
    assert plans["H1111-001"]["coverage"] == "covered"
    assert plans["H9999-001"]["coverage"] == "lookup_failed"
    assert r["plan_errors"]["H9999-001"]


def test_upstream_failure_and_timeout_are_isolated(synthetic, monkeypatch):
    monkeypatch.setattr(batch, "MIN_BUDGET", 1)
    r = run("faildrug 10 mg\nslowdrug 5 mg\nEliquis 5 mg\nfurosemide 40 mg", time_budget_seconds=1)
    st = {x["original"]: x for x in r["results"]}
    assert st["faildrug 10 mg"]["status"] == "lookup_failed" and "outage" in st["faildrug 10 mg"]["detail"]
    assert st["slowdrug 5 mg"]["status"] == "lookup_failed" and "Timed out" in st["slowdrug 5 mg"]["detail"]
    assert "not a 'not covered' result" in st["slowdrug 5 mg"]["note"]
    assert st["Eliquis 5 mg"]["status"] == "matched" and st["furosemide 40 mg"]["status"] == "matched"


def test_structured_items_and_invalid_items(synthetic):
    r = run("", [{"name": "Metformin", "strength": "500 mg", "release": "ER"},
                 {"name": "losartan/hydrochlorothiazide", "strength": "50/12.5 mg", "form": "tablet"},
                 {"strength": "10 mg"}, "Eliquis 2.5 mg"])
    s = [x["status"] for x in r["results"]]
    assert s[0] == "matched" and r["results"][0]["matched_products"][0]["rxcui"] == "11"
    assert r["results"][1]["matched_products"][0]["rxcui"] == "31"
    assert s[2] == "invalid_entry"
    assert r["results"][3]["matched_products"][0]["rxcui"] == "21"


def test_batch_limit_never_truncates_silently(synthetic):
    text = "\n".join(f"furosemide 40 mg  # item {i}" for i in range(105))
    r = run(text)
    assert r["entries_total"] == 105 and len(r["results"]) == 105
    deferred = [x for x in r["results"] if x["status"] == "deferred_batch_limit"]
    assert len(deferred) == 5 and deferred[0]["index"] == 101
    assert r["continuation"]["start_at"] == 101
    r2 = run(text, start_at=101)
    assert sum(1 for x in r2["results"] if x["status"] != "deferred_batch_limit") == 5
    assert r2["continuation"] is None


def test_identifier_lines_redacted_and_never_sent_upstream(synthetic):
    r = run("Patient: Jane Example\nDOB 01/02/1950\nMBI 1EG4TE5MK73\n123-45-6789\nEliquis 5 mg")
    assert len(r["results"]) == 1 and r["results"][0]["original"] == "Eliquis 5 mg"
    assert all(x["reason"] == "possible_personal_identifier_redacted" for x in r["ignored_lines"])
    assert all("Jane" not in json.dumps(x) for x in r["ignored_lines"])
    assert not any("jane" in c.lower() or "1950" in c for c in synthetic)


def test_headers_with_extra_words_are_not_entries(synthetic):
    r = run("Medications (current, per synthetic list):\nfurosemide 40 mg\nAllergies: none")
    assert [x["original"] for x in r["results"]] == ["furosemide 40 mg"]
    assert {x["reason"] for x in r["ignored_lines"]} == {"header"}


def test_dates_in_medication_lines_are_kept(synthetic):
    r = run("furosemide 40 mg started 3/4/2023")
    assert len(r["results"]) == 1 and r["results"][0]["status"] == "matched"


def test_text_rendering_is_complete(synthetic):
    r = run(MIXED)
    txt = batch.render_text(r)
    assert txt.count("\"index\":") == 24 and "| 24 |" in txt
    assert len(txt) < 400_000


def test_large_list_uses_bounded_concurrency(synthetic, monkeypatch):
    active, peak = [0], [0]
    lock = threading.Lock()
    orig = batch.candidate_products

    def tracked(parsed):
        with lock:
            active[0] += 1
            peak[0] = max(peak[0], active[0])
        try:
            time.sleep(0.05)
            return orig(parsed)
        finally:
            with lock:
                active[0] -= 1
    monkeypatch.setattr(batch, "candidate_products", tracked)
    names = list(PRODUCTS)[:20]
    run("\n".join(f"{n} 1 mg" for n in names))
    assert 1 < peak[0] <= batch.WORKERS
