"""Adversarial regressions for the independent review of 1.2.0 (commit 19843bf). Synthetic fixtures only.

Each test names the finding it covers. RxNorm, the CMS Part D file and the MedCare site are all synthetic.
"""
import re
import sqlite3
import zipfile

import pytest

from conftest import ROOT
from medcare import batch, cms
import test_batch as tb
from test_batch import P, synthetic  # noqa: F401  (fixture)

H = "H1111-001"


def add(monkeypatch, name, products, formulary=None, excluded=None):
    monkeypatch.setitem(tb.PRODUCTS, name, products)
    db = tb.synthetic_db.db
    for rx, (t, pa, st, ql, amt, days) in (formulary or {}).items():
        db.execute("insert into formulary values ('F1',?,?,?,?,?,?,?)", (rx, str(t), pa, st, ql, amt, days))
    for rx, (tier, pa) in (excluded or {}).items():
        db.execute("insert into excluded values ('H1111','001',?,?,'0','','',?,'N','N')", (rx, str(tier), pa))


def one(text=None, meds=None, plans=(H,)):
    r = batch.check_list(text or "", meds, plan_ids=list(plans), include_medcare_site=False)
    assert len(r["results"]) == 1, r["results"]
    return r["results"][0]


def rx(it):
    return [m["rxcui"] for m in it.get("matched_products", [])]


T1 = (1, "N", "N", "N", "", "")


# ------------------------------------------------------------------ 1. units, denominators, association
def test_f1_percent_is_not_mg(synthetic, monkeypatch):
    add(monkeypatch, "hydrocortisone", [P("h1", "SCD", "hydrocortisone 1 MG/G Topical Cream"),
                                        P("h10", "SCD", "hydrocortisone 10 MG/G Topical Cream"),
                                        P("h25", "SCD", "hydrocortisone 25 MG/G Topical Cream")],
        {"h1": T1, "h10": T1, "h25": T1})
    for text in ("hydrocortisone 1% cream", "hydrocortisone1% cream"):
        it = one(text)
        assert it["status"] == "matched" and rx(it) == ["h10"], it   # 1% == 10 mg/g, never 1 mg/g
    it = one("hydrocortisone 2.5% cream")
    assert rx(it) == ["h25"]


def test_f1_units_vs_mg_and_denominator(synthetic, monkeypatch):
    add(monkeypatch, "insulin glargine", [P("g1", "SCD", "insulin glargine 100 UNT/ML Injectable Solution"),
                                          P("g2", "SCD", "3 ML insulin glargine 100 UNT/ML Pen Injector")],
        {"g1": T1, "g2": T1})
    for text in ("insulin glargine 100 mg/ml vial", "insulin glargine100mg/ml vial"):
        it = one(text)
        assert it["status"] == "needs_clarification" and it["clarification"]["reason"] == "strength_not_found", it
        assert "matched_products" not in it
    it = one("insulin glargine 100 mg vial")             # no denominator never matches a per-mL product
    assert it["status"] == "needs_clarification" and not rx(it)
    it = one("insulin glargine 100 units/mL vial")
    assert it["status"] == "matched" and rx(it) == ["g1"]


def test_f1_ingredient_dose_association_enforced(synthetic, monkeypatch):
    # synthetic: only HCTZ 12.5 / losartan 50 and HCTZ 25 / losartan 100 exist
    add(monkeypatch, "losartan", tb.PRODUCTS["losartan"] + [
        P("33", "SCD", "hydrochlorothiazide 25 MG / losartan potassium 100 MG Oral Tablet")], {"33": T1})
    monkeypatch.setitem(tb.PRODUCTS, "hydrochlorothiazide", tb.PRODUCTS["losartan"])
    it = one("losartan/HCTZ 12.5/50 mg")                  # losartan 12.5 + HCTZ 50: reversed, does not exist
    assert it["status"] == "needs_clarification" and it["clarification"]["reason"] == "strength_not_found", it
    for text in ("losartan/HCTZ 50/12.5 mg", "HCTZ/losartan 12.5/50 mg", "Losartan-HCTZ 50-12.5 mg daily",
                 "losartan 50 mg / HCTZ 12.5 mg"):
        it = one(text)
        assert it["status"] == "matched" and rx(it) == ["31"], (text, it)
        assert it["match"]["strength_association"] == "written_order"


def test_f1_brand_combo_values_without_names_are_disclosed(synthetic, monkeypatch):
    monkeypatch.setitem(tb.PRODUCTS, "hyzaar", [tb.PRODUCTS["losartan"][2]])
    it = one("Hyzaar 50-12.5 mg")
    assert rx(it) == ["32"] and it["match"]["strength_association"] == "not_stated" and it.get("strength_note")


# ------------------------------------------------------------------ 2. unitless numbers are kept and clarified
def test_f2_unitless_combo_strength_never_matches_any_strength(synthetic, monkeypatch):
    add(monkeypatch, "losartan", tb.PRODUCTS["losartan"] + [
        P("33", "SCD", "hydrochlorothiazide 25 MG / losartan potassium 100 MG Oral Tablet")], {"33": T1})
    for text in ("losartan/HCTZ 50-12.5", "losartan/HCTZ50-12.5", "Losartan-HCTZ 50-12.5 daily"):
        it = one(text)
        assert it["status"] == "needs_clarification", (text, it)
        c = it["clarification"]
        assert c["reason"] == "strength_unit_not_stated" and c["unresolved_numbers"] == ["50-12.5"]
        assert not any("25 MG" in o for o in c["options_in_rxnorm"]), c   # 25/100 never offered for 50-12.5
        assert any("12.5 MG / losartan potassium 50 MG" in o for o in c["options_in_rxnorm"])
        assert "plans" not in it
    it = one("aspirin 81")
    assert it["status"] in ("needs_clarification", "unrecognized")


# ------------------------------------------------------------------ 3. explicit delivery forms
def test_f3_vial_pen_and_nebulizer_mdi_are_distinct(synthetic, monkeypatch):
    add(monkeypatch, "insulin glargine", [P("g1", "SCD", "insulin glargine 100 UNT/ML Injectable Solution"),
                                          P("g2", "SCD", "3 ML insulin glargine 100 UNT/ML Pen Injector"),
                                          P("g3", "SCD", "3 ML insulin glargine 100 UNT/ML Cartridge")],
        {"g1": (3, "N", "N", "N", "", ""), "g2": (2, "N", "N", "N", "", ""), "g3": T1})
    assert rx(one("insulin glargine 100 units/mL vial")) == ["g1"]
    assert rx(one("insulin glargine 100 units/mL pen")) == ["g2"]
    assert rx(one("insulin glargine 100 units/mL cartridge")) == ["g3"]
    it = one("insulin glargine 100 units/mL injection")      # generic word: all injectables, coverage differs
    assert it["status"] == "needs_clarification" and set(rx(it)) == {"g1", "g2", "g3"}
    add(monkeypatch, "albuterol", [P("a1", "SCD", "albuterol 0.833 MG/ML Inhalation Solution"),
                                   P("a2", "SCD", "albuterol 0.09 MG/ACTUAT Metered Dose Inhaler")],
        {"a1": (1, "N", "N", "N", "", ""), "a2": (2, "N", "N", "Y", "2", "30")})
    assert rx(one("albuterol nebulizer solution")) == ["a1"]
    assert rx(one("albuterol 2.5 mg/3 mL nebulizer")) == ["a1"]
    assert rx(one("albuterol HFA 90 mcg/act inhaler")) == ["a2"]
    it = one("albuterol 0.09 mg/act nebulizer")             # MDI strength written with 'nebulizer': no match
    assert it["status"] == "needs_clarification" and not rx(it)
    assert rx(one("albuterol inhaler")) == ["a2"]            # 'inhaler' alone never means nebulizer solution


# ------------------------------------------------------------------ 4. numeric commas
def test_f4_numeric_commas_are_numbers_not_separators(synthetic, monkeypatch):
    add(monkeypatch, "cholecalciferol", [P("d1", "SCD", "cholecalciferol 1000 UNT Oral Tablet"),
                                         P("d0", "SCD", "cholecalciferol 400 UNT Oral Tablet")], {"d1": T1, "d0": T1})
    entries, _ = batch.split_entries("cholecalciferol 1,000 units tablet\nlevothyroxine 0,025 mg tablet\n"
                                     "calcium 1,250 mg, cholecalciferol 1,000 units\nmetformin 500 mg, 1 tab daily")
    assert [e["original"] for e in entries] == ["cholecalciferol 1,000 units tablet", "levothyroxine 0,025 mg tablet",
                                                "calcium 1,250 mg", "cholecalciferol 1,000 units",
                                                "metformin 500 mg, 1 tab daily"]
    vals = [[(s["value"], s["unit"]) for s in batch.parse_entry(e["text"])["strengths"]] for e in entries]
    assert vals[:4] == [[(1000.0, "unt")], [(0.025, "mg")], [(1250.0, "mg")], [(1000.0, "unt")]]
    it = one("cholecalciferol 1,000 units tablet")
    assert rx(it) == ["d1"]
    it = one("levothyroxine 0,025 mg tablet")
    assert rx(it) == ["40"]                                    # 0.025 mg, never 25 mg
    s = batch.check_list(medications=[{"name": "levothyroxine", "strength": "0,025 mg", "form": "tablet"}], plan_ids=[H],
                         include_medcare_site=False)
    assert rx(s["results"][0]) == ["40"]


# ------------------------------------------------------------------ 5. duplicates keep brand intent
def test_f5_duplicate_key_includes_parenthetical_brand(synthetic):
    r = batch.check_list("lisinopril 10 mg tablet\nlisinopril (Zestril) 10 mg tablet\nlisinopril 10 mg tablet\n"
                         "lisinopril10 mg tablet", plan_ids=[H], include_medcare_site=False)
    a, b, c, d = r["results"]
    assert "duplicate_of" not in b and b["match"]["brand_intent"] is True and rx(b) == ["3"]
    assert rx(a) == ["1"] and c["duplicate_of"] == 1 and d["duplicate_of"] == 1


# ------------------------------------------------------------------ 6. identifiers on every input path
def test_f6_structured_identifiers_never_reach_upstream(synthetic):
    calls = synthetic
    r = batch.check_list(medications=[{"name": "Patient Synthetic Example"},
                               {"name": "lisinopril", "strength": "10 mg", "original": "Patient: Synthetic Example"},
                               {"name": "lisinopril", "strength": "DOB 01/02/1950"},
                               {"name": "lisinopril (MBI 1EG4TE5MK73)", "strength": "10 mg"},
                               "Name: Synthetic Example",
                               {"name": "lisinopril", "strength": "10 mg"}],
                         plan_ids=[H], include_medcare_site=False)
    res = r["results"]
    assert [x["status"] for x in res[:5]] == ["invalid_entry"] * 5
    assert all("redacted" in x["original"] for x in res[:5])
    assert res[5]["status"] == "matched"
    joined = " ".join(calls).lower()
    for leak in ("patient", "synthetic", "example", "dob", "1950", "mbi", "1eg4te5mk73", "name"):
        assert leak not in joined, (leak, calls)
    assert len(r["ignored_lines"]) == 5 and "Synthetic Example" not in str(r) and "1950" not in str(r)
    for q in ("Patient Synthetic Example", "DOB 01/02/1950", "123-45-6789"):
        with pytest.raises(ValueError):
            batch._guard_upstream(q)


def test_f6_text_aliases_and_navigator_guarded(synthetic, monkeypatch):
    sent = []
    monkeypatch.setattr(batch.lookups, "navigator_search", lambda q, max_details=0: sent.append(q) or {"matches": []})
    monkeypatch.setattr(batch.lookups, "snapshot_search", lambda q, limit=200: {"matches": [], "source_pdf": "x",
                                                                                  "effective": "x"})
    r = batch.check_list("lisinopril (patient Synthetic Example) 10 mg\nlisinopril 10 mg", plan_ids=["H5767-001"],
                         include_medcare_site=True)
    assert len(r["results"]) == 1 and r["ignored_lines"][0]["reason"] == "possible_personal_identifier_redacted"
    assert all("synthetic" not in q.lower() for q in sent + synthetic)


# ------------------------------------------------------------------ 7. CMS refresh never leaves stale data
def _puf(file_date: str, formulary: dict, copay: float) -> sqlite3.Connection:
    db = sqlite3.connect(":memory:", check_same_thread=False)
    db.row_factory = sqlite3.Row
    db.execute("create table meta(key text primary key, value text)")
    db.executemany("insert into meta values (?,?)", [("dataset", f"Synthetic file {file_date}"), ("file_date", file_date),
                                                     ("source_url", f"https://example.invalid/{file_date}.zip"),
                                                     ("retrieved_at", "2026-10-21T00:00:00+00:00"),
                                                     ("last_checked_at", "2026-10-21T00:00:00+00:00")])
    db.execute("create table plan_info(CONTRACT_ID, PLAN_ID, SEGMENT_ID, PLAN_NAME, FORMULARY_ID, DEDUCTIBLE)")
    db.execute("insert into plan_info values ('H1111','001','000','Synthetic Plan','F9','0')")
    db.execute("create table formulary(FORMULARY_ID, RXCUI, TIER_LEVEL_VALUE, PRIOR_AUTHORIZATION_YN, "
               "STEP_THERAPY_YN, QUANTITY_LIMIT_YN, QUANTITY_LIMIT_AMOUNT, QUANTITY_LIMIT_DAYS)")
    for r, t in formulary.items():
        db.execute("insert into formulary values ('F9',?,?,'N','N','N','','')", (r, str(t)))
    db.execute("create table excluded(CONTRACT_ID, PLAN_ID, RXCUI, TIER, QUANTITY_LIMIT_YN, QUANTITY_LIMIT_AMOUNT, "
               "QUANTITY_LIMIT_DAYS, PRIOR_AUTH_YN, STEP_THERAPY_YN, CAPPED_BENEFIT_YN)")
    cols = ["CONTRACT_ID", "PLAN_ID", "SEGMENT_ID", "COVERAGE_LEVEL", "TIER", "DAYS_SUPPLY"] + [
        f"COST_{a}_{b}" for b in ("PREF", "NONPREF", "MAIL_PREF", "MAIL_NONPREF") for a in ("TYPE", "AMT", "MIN_AMT", "MAX_AMT")
    ] + ["TIER_SPECIALTY_YN", "DED_APPLIES_YN"]
    db.execute(f"create table cost({', '.join(cols)})")
    for tier in (1, 2):
        row = {c: "" for c in cols}
        row.update(CONTRACT_ID="H1111", PLAN_ID="001", SEGMENT_ID="000", COVERAGE_LEVEL="1", TIER=str(tier),
                   DAYS_SUPPLY="1", COST_TYPE_NONPREF="1", COST_AMT_NONPREF=str(copay * tier), COST_TYPE_PREF="0",
                   COST_TYPE_MAIL_PREF="0", COST_TYPE_MAIL_NONPREF="0", TIER_SPECIALTY_YN="N", DED_APPLIES_YN="N")
        db.execute(f"insert into cost values ({','.join('?' * len(cols))})", [row[c] for c in cols])
    db.execute("create table insulin(CONTRACT_ID, PLAN_ID, SEGMENT_ID, TIER, DAYS_SUPPLY)")
    return db


def test_f7_cms_refresh_is_never_masked_by_a_cache(synthetic, monkeypatch):
    monkeypatch.setattr(batch, "_plan_context", tb_real_plan_context)
    monkeypatch.setattr(batch, "_sources", batch_real_sources)
    old = _puf("OLD", {"1": 1}, 5.0)                       # lisinopril 10 listed; atorvastatin 40 not listed
    new = _puf("NEW", {"1": 1, "50": 2}, 7.0)              # NEW: atorvastatin 40 added, copays changed
    current = {"db": old}
    monkeypatch.setattr(batch.cms, "puf_db", lambda year, *a, **k: current["db"])
    text = "lisinopril 10 mg tablet\natorvastatin 40 mg tablet"
    r1 = batch.check_list(text, plan_ids=[H], include_medcare_site=False)
    assert r1["results"][1]["plans"][0]["coverage"] == "not_on_formulary"
    assert r1["results"][0]["plans"][0]["cost_sharing"]["retail_30_day"] == "$5.00 copay"
    assert r1["sources"]["cms_part_d_file"]["file_date"] == "OLD"
    current["db"] = new                                    # CMS refresh between calls (same process)
    r2 = batch.check_list(text, plan_ids=[H], include_medcare_site=False)
    lis, ator = r2["results"][0]["plans"][0], r2["results"][1]["plans"][0]
    assert ator["coverage"] == "covered" and ator["tier"] == 2 and ator["cost_sharing"]["retail_30_day"] == "$14.00 copay"
    assert lis["cost_sharing"]["retail_30_day"] == "$7.00 copay"
    assert r2["sources"]["cms_part_d_file"]["file_date"] == "NEW"
    assert lis["cms_file"]["file_date"] == ator["cms_file"]["file_date"] == "NEW"
    assert r2["plans"][0]["cms_file"]["file_date"] == "NEW"


tb_real_plan_context = batch._plan_context
batch_real_sources = batch._sources


# ------------------------------------------------------------------ 8. supplemental coverage stays per product
def test_f8_supplemental_excluded_drug_is_product_specific(synthetic, monkeypatch):
    add(monkeypatch, "zolpidem", [P("z1", "SCD", "zolpidem tartrate 10 MG Oral Tablet"),
                                  P("z2", "SCD", "zolpidem tartrate 10 MG Oral Capsule")], {}, {"z1": (2, "N")})
    it = one("zolpidem 10 mg")
    p = it["plans"][0]
    assert p["coverage"] == "varies_by_product_needs_clarification" and it["status"] == "needs_clarification"
    by = {x["rxcui"]: x["basis"] for x in p["products"]}
    assert by == {"z1": "supplemental_excluded_drug", "z2": "not_listed"}
    it = one("zolpidem 10 mg tablet")
    assert it["plans"][0]["coverage"] == "covered_as_supplemental_excluded_drug" and it["plans"][0]["tier"] == 2
    it = one("zolpidem 10 mg capsule")
    assert it["plans"][0]["coverage"] == "not_on_formulary"
    tb.synthetic_db.db.execute("insert into excluded values ('H1111','001','z2','3','0','','','Y','N','N')")
    it = one("zolpidem 10 mg")                                  # both supplemental but different terms
    assert it["plans"][0]["coverage"] == "varies_by_product_needs_clarification"


# ------------------------------------------------------------------ docs: ZIP README never points at missing files
def test_zip_readme_links_resolve_inside_the_zip(tmp_path):
    import subprocess
    import sys
    out = subprocess.run([sys.executable, str(ROOT / "package.py")], capture_output=True, text=True, cwd=ROOT)
    assert out.returncode == 0, out.stderr
    z = zipfile.ZipFile(out.stdout.strip())
    names = {n.split("/", 1)[1] for n in z.namelist()}
    prefix = z.namelist()[0].split("/", 1)[0]
    readme = z.read(f"{prefix}/README.md").decode()
    for target in re.findall(r"\]\(([^)#]+)(?:#[^)]*)?\)", readme):
        if target.startswith(("http://", "https://", "mailto:")):
            continue
        assert target in names or any(n.startswith(target.rstrip("/") + "/") for n in names), target
    assert "remote/" not in names and "CHATGPT_SETUP.md" not in names
    assert "github.com/myacaexpress/MCP-Codex-Plugin" in readme and "not included in this ZIP" in readme


# ------------------------------------------------------------------ review of 1.2.1: explicit parenthetical brand
GEN = {"rxcui": "1", "name": "lisinopril 10 MG Oral Tablet", "tty": "SCD"}
ZES = {"rxcui": "3", "name": "lisinopril 10 MG Oral Tablet [Zestril]", "tty": "SBD"}
PRI = {"rxcui": "5", "name": "lisinopril 10 MG Oral Tablet [Prinivil]", "tty": "SBD"}


def _rx(table):
    calls = []

    def fake(q):
        calls.append(q)
        v = table.get(q.lower(), [])
        if isinstance(v, Exception):
            raise v
        return v
    return fake, calls


def test_reviewer_repro_brand_omitted_by_upstream_never_selects_generic(monkeypatch):
    monkeypatch.setattr(batch, "_rxnav", lambda q: [dict(GEN)])          # every query: generic only
    res = batch.candidate_products(batch.parse_entry("lisinopril (Zestril) 10 mg tablet"))
    assert "Zestril" in res["queries"]                                    # the alias is always queried
    assert res["products"] == [] and res["alias_unresolved"]["reason"] == "parenthetical_name_not_found_in_rxnorm_results"


def test_brand_alias_missing_partial_and_present(synthetic, monkeypatch):
    # (a) alias query returns nothing, generic present -> clarification, generic NOT selected
    fake, calls = _rx({"lisinopril": [GEN], "zestril": []})
    monkeypatch.setattr(batch, "_rxnav", fake)
    it = one("lisinopril (Zestril) 10 mg tablet")
    assert it["status"] == "needs_clarification" and "matched_products" not in it and "plans" not in it
    assert it["clarification"]["parenthetical_name"] == "Zestril" and "Zestril" in calls
    # (b) partial upstream failure on the alias -> lookup_failed (unknown), never the generic
    fake, calls = _rx({"lisinopril": [GEN], "zestril": batch.net.FetchError("synthetic outage")})
    monkeypatch.setattr(batch, "_rxnav", fake)
    it = one("lisinopril (Zestril) 10 mg tablet")
    assert it["status"] == "lookup_failed" and "matched_products" not in it
    # (c) brand present only through the alias query -> brand product only
    fake, calls = _rx({"lisinopril": [GEN], "zestril": [ZES]})
    monkeypatch.setattr(batch, "_rxnav", fake)
    it = one("lisinopril (Zestril) 10 mg tablet")
    assert it["match"]["brand_intent"] is True and rx(it) == ["3"]
    # (d) brand present in the first response too -> same answer
    fake, _ = _rx({"lisinopril": [GEN, ZES], "zestril": [ZES]})
    monkeypatch.setattr(batch, "_rxnav", fake)
    assert rx(one("lisinopril (Zestril) 10 mg tablet")) == ["3"]
    # (e) brand written first with the generic in parentheses
    fake, _ = _rx({"zestril": [ZES], "lisinopril": [GEN, ZES]})
    monkeypatch.setattr(batch, "_rxnav", fake)
    assert rx(one("Zestril (lisinopril) 10 mg tablet")) == ["3"]
    # (f) explicit '(generic)' annotation keeps generic intent
    fake, _ = _rx({"lisinopril": [GEN, ZES]})
    monkeypatch.setattr(batch, "_rxnav", fake)
    assert rx(one("lisinopril (generic) 10 mg tablet")) == ["1"]
    # (g) two different brands written -> clarification, nothing selected
    fake, _ = _rx({"zestril": [ZES], "prinivil": [PRI], "lisinopril": [GEN, ZES, PRI]})
    monkeypatch.setattr(batch, "_rxnav", fake)
    it = one("Zestril (Prinivil) 10 mg tablet")
    assert it["status"] == "needs_clarification" and it["clarification"]["reason"] == \
        "parenthetical_brand_conflicts_with_written_brand" and "matched_products" not in it
    # (h) unknown parenthetical text -> clarification, never the generic
    fake, _ = _rx({"lisinopril": [GEN, ZES]})
    monkeypatch.setattr(batch, "_rxnav", fake)
    it = one("lisinopril (Qwertyzol) 10 mg tablet")
    assert it["status"] == "needs_clarification" and "matched_products" not in it
