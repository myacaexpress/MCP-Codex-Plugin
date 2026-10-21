"""Batch medication-list check: paste a whole list once, get one accountable result per entry.

Design rules (see README "Medication list check"):
* Every parsed entry gets exactly one result, in input order, with its original text preserved.
* Nothing is silently substituted: strength, dose form, release type and brand-vs-generic must
  agree with what was written. If several products remain and their coverage differs, the entry
  is marked needs_clarification with the options. If they all have identical coverage terms the
  answer is reported once with every matching product listed.
* Combination products are never split: "losartan/HCTZ 50-12.5" is one entry whose ingredient
  set must match exactly. A single-ingredient entry never matches a combination product.
* lookup_failed (upstream error/timeout) is distinct from not_on_formulary.
* Per-entry isolation: one bad entry or upstream failure never discards other results.
* Bounded concurrency (4 workers), per-call caching of identical lookups, an overall time budget,
  and an explicit per-call limit (MAX_ENTRIES). Longer lists are never truncated silently: extra
  entries are listed as deferred_batch_limit with a continuation start_at value.
* No list is written to disk or logged. Lines that look like personal identifiers are redacted and
  never sent to any upstream service.
"""
from __future__ import annotations

import re
from collections import OrderedDict
import threading
import time
from concurrent.futures import ThreadPoolExecutor, wait
from urllib.parse import quote

from . import cms, lookups, net
from .textsearch import fold

MAX_ENTRIES = 100
MAX_TEXT_CHARS = 60_000
DEFAULT_PLANS = ("H5767-001", "H5767-002", "H5767-003")
DEFAULT_YEAR = 2027
WORKERS = 4
MIN_BUDGET = 20

# ------------------------------------------------------------------ text parsing
_BULLET = re.compile(r"^\s*(?:[-*•·▪◦‣–—>]+|\(?\d{1,3}[.)]|\d{1,3}\s*[-:]|#\d{1,3}[.):]?|\[[ xX]?\]|[a-zA-Z][.)](?=\s))\s*")
_HEADER = re.compile(r"^\s*(current\s+|active\s+|home\s+)?(medications?|meds|medication list|med list|rx list|"
                     r"prescriptions?|drug list|allergies?.*)\s*:?\s*$", re.I)
_IDENTIFIER = re.compile(
    r"\b(dob|d\.o\.b|date of birth|ssn|social security|mbi|medicare (?:number|id|#)|member id|patient(?: name)?|"
    r"name\s*:|address|phone|mrn)\b|\b\d{3}-\d{2}-\d{4}\b|"
    r"\b[1-9][A-Za-z][A-Za-z0-9]\d[A-Za-z][A-Za-z0-9]\d[A-Za-z]{2}\d{2}\b", re.I)
_SIG = re.compile(
    r"\b(take|takes|taking|inject|apply|inhale|instill|use|chew|dissolve|place|insert|daily|nightly|weekly|monthly|"
    r"once|twice|three times|four times|every|q\d+h|qd|qod|bid|tid|qid|qhs|qam|qpm|hs|prn|as needed|by mouth|"
    r"orally|po|sl|at bedtime|in the morning|with (?:food|meals)|before|after|for \d+|x\s*\d+\s*days|"
    r"#\s*\d+|qty|quantity|refills?|disp|sig|start(?:ed)?|since|stopped|discontinued|prescribed|per dr|"
    r"\d{1,2}/\d{1,2}/\d{2,4})\b", re.I)
_ABBREV = {"hctz": "hydrochlorothiazide", "apap": "acetaminophen", "asa": "aspirin", "ntg": "nitroglycerin",
           "kcl": "potassium chloride", "smx": "sulfamethoxazole", "tmp": "trimethoprim", "mtx": "methotrexate",
           "ec asa": "aspirin", "levo": "levothyroxine", "nph": "insulin isophane"}
_UNIT = r"(mg|mcg|µg|ug|g|grams?|units?|unt|iu|ml|%|meq|mmol)"
_STRENGTH = re.compile(
    rf"(?<![\w.])(\d+(?:[.,]\d+)?(?:\s*(?:/|-)\s*\d+(?:[.,]\d+)?)*)\s*{_UNIT}"
    r"(?:\s*/\s*(ml|dose|act|actuation|spray|hr|h|24\s*hr?|day|g))?(?![a-z])", re.I)
_RELEASE = [
    ("extended_release", re.compile(r"\b(er|xr|xl|sr|cr|la|xr\d*|extended[- ]release|sustained[- ]release|"
                                    r"controlled[- ]release|24[- ]?(?:hr|hour|h)|12[- ]?(?:hr|hour|h))\b", re.I)),
    ("delayed_release", re.compile(r"\b(dr|ec|delayed[- ]release|enteric[- ]coated)\b", re.I)),
    ("immediate_release", re.compile(r"\b(ir|immediate[- ]release)\b", re.I)),
]
_FORMS = [
    ("disintegrating", r"\b(odt|orally disintegrating|disintegrating)\b", ["Disintegrating"]),
    ("chewable", r"\b(chewable|chew tab)\b", ["Chewable"]),
    ("pen", r"\b(pen|pens|auto-?injector|autoinjector|flextouch|kwikpen|solostar)\b", ["Pen Injector", "Auto-Injector", "Cartridge"]),
    ("syringe", r"\b(prefilled syringe|syringe)\b", ["Prefilled Syringe", "Syringe"]),
    ("injection", r"\b(inj|injection|injectable|vial|subq|subcutaneous|im)\b",
     ["Injectable", "Injection", "Pen Injector", "Auto-Injector", "Prefilled Syringe", "Cartridge"]),
    ("inhaler", r"\b(inhaler|inhalation|mdi|hfa|diskus|ellipta|respimat|nebulizer|neb)\b", ["Inhal"]),
    ("patch", r"\b(patch|transdermal)\b", ["Transdermal"]),
    ("nasal", r"\b(nasal)\b", ["Nasal"]),
    ("ophthalmic", r"\b(eye drops?|ophthalmic|ophth)\b", ["Ophthalmic"]),
    ("otic", r"\b(ear drops?|otic)\b", ["Otic"]),
    ("cream", r"\b(cream)\b", ["Cream"]),
    ("ointment", r"\b(ointment|oint)\b", ["Ointment"]),
    ("gel", r"\b(gel)\b", ["Gel"]),
    ("suspension", r"\b(suspension|susp)\b", ["Suspension"]),
    ("solution", r"\b(solution|soln|liquid|syrup|elixir|oral sol)\b", ["Solution", "Syrup", "Elixir"]),
    ("powder", r"\b(powder)\b", ["Powder"]),
    ("suppository", r"\b(suppository|supp)\b", ["Suppository", "Rectal"]),
    ("capsule", r"\b(cap|caps|capsule|capsules)\b", ["Capsule"]),
    ("tablet", r"\b(tab|tabs|tablet|tablets|caplet)\b", ["Tablet"]),
]


def _num(s: str) -> float:
    return float(s.replace(",", "."))


def _to_mg(value: float, unit: str) -> tuple[float, str]:
    u = unit.lower()
    if u in ("mcg", "µg", "ug"):
        return value / 1000.0, "mg"
    if u in ("g", "gram", "grams"):
        return value * 1000.0, "mg"
    if u in ("unit", "units", "unt", "iu"):
        return value, "unt"
    return value, u


def parse_strengths(text: str) -> list[dict]:
    out = []
    for m in _STRENGTH.finditer(text):
        nums = re.split(r"\s*(?:/|-)\s*", m.group(1).replace(",", "") if re.search(r"\d,\d{3}", m.group(1)) else m.group(1))
        unit = m.group(2)
        per = (m.group(3) or "").lower()
        for n in nums:
            if not n:
                continue
            v, u = _to_mg(_num(n), unit)
            out.append({"value": round(v, 6), "unit": u + (f"/{per}" if per else ""), "text": m.group(0).strip()})
    return out


def split_entries(text: str) -> tuple[list[dict], list[dict]]:
    """Split pasted text into entries. Returns (entries, ignored_lines). Never drops content silently."""
    entries, ignored = [], []
    lines = re.split(r"\r?\n", text)
    for ln_no, raw_line in enumerate(lines, 1):
        line = raw_line.strip()
        if not line:
            continue
        if _IDENTIFIER.search(line):
            ignored.append({"line": ln_no, "reason": "possible_personal_identifier_redacted",
                            "text": "[redacted: line looks like a personal identifier; not sent to any service]"})
            continue
        if _HEADER.match(line) or (line.endswith(":") and not _STRENGTH.search(line)):
            ignored.append({"line": ln_no, "reason": "header", "text": line})
            continue
        # explicit delimiters that never occur inside a single drug description
        parts = [p for p in re.split(r"\s*[;|\t]\s*|\s{3,}", line) if p.strip()]
        for part in parts:
            for seg in _split_commas(part):
                seg_clean = _BULLET.sub("", seg).strip(" .,:")
                if not seg_clean:
                    continue
                entries.append({"original": seg.strip(), "line": ln_no, "text": seg_clean})
    return entries, ignored


def _split_commas(part: str) -> list[str]:
    """Split 'drug A 10 mg, drug B 20 mg' but keep 'drug 500 mg, take 1 tablet twice daily' together."""
    pieces = [p for p in part.split(",")]
    if len(pieces) == 1:
        return [part]
    out = []
    for p in pieces:
        s = p.strip()
        if not s:
            continue
        starts_like_sig = bool(re.match(r"^(\d|#|x\s*\d)", s)) or bool(_SIG.match(s)) or \
            bool(re.match(r"^(tab|tabs|tablet|tablets|cap|caps|capsule|capsules|po|er|xr|xl|sr|dr|ec|odt)\b", s, re.I))
        if out and starts_like_sig:
            out[-1] = out[-1] + ", " + s
        else:
            out.append(s)
    return out


def parse_entry(text: str) -> dict:
    sig = ""
    if "," in text:  # anything after the first comma was merged as instructions by _split_commas
        head, tail = text.split(",", 1)
        if parse_strengths(head) or not parse_strengths(tail):
            text, sig = head, tail.strip()
    m = _SIG.search(text)
    core = text
    if m and m.start() > 0:
        core, sig = text[: m.start()], (text[m.start():] + (" " + sig if sig else "")).strip()
    strengths = parse_strengths(core)
    release = None
    for name, rx in _RELEASE:
        if rx.search(core):
            release = name
            break
    form = None
    for fname, rx, _ in _FORMS:
        if re.search(rx, core, re.I):
            form = fname
            break
    name = _STRENGTH.sub(" ", core)
    for _, rx in _RELEASE:
        name = rx.sub(" ", name)
    for _, rx, _ in _FORMS:
        name = re.sub(rx, " ", name, flags=re.I)
    name = re.sub(r"\boral\b|\bby mouth\b", " ", name, flags=re.I)
    alias = None
    pm = re.search(r"\(([^)]*)\)", name)
    if pm:
        alias = pm.group(1).strip() or None
        name = name[: pm.start()] + name[pm.end():]
    name = re.sub(r"(?<![\w.])\d+(?:\.\d+)?(?![\w.])", " ", name)  # stray counts like '1'
    name = re.sub(r"\s+", " ", name).strip(" -,.:/+&#")
    ingredients = _ingredients(name)
    return {"name": name, "alias_in_parentheses": alias, "ingredients": ingredients,
            "strengths": strengths, "form": form, "release": release, "sig": sig.strip() or None}


def _ingredients(name: str) -> list[str]:
    n = fold(name)
    for k, v in sorted(_ABBREV.items(), key=lambda kv: -len(kv[0])):
        n = re.sub(rf"\b{re.escape(k)}\b", v, n)
    parts = [p.strip() for p in re.split(r"\s*(?:/|\+|&|\band\b|\bwith\b)\s*", n) if p.strip()]
    if len(parts) == 1 and "-" in parts[0]:
        dash = [p.strip() for p in parts[0].split("-") if p.strip()]
        if len(dash) > 1 and all(len(p) >= 4 for p in dash) and not re.search(r"-[a-z]{4}$", parts[0]):
            parts = dash  # 'losartan-hydrochlorothiazide'; keeps biosimilar suffixes like 'glargine-yfgn'
    return parts


# ------------------------------------------------------------------ RxNorm products
# In-memory only (never written to disk), keyed by a single drug name with no link to the list or user,
# bounded in size and age so a long-running HTTP server does not accumulate lookups.
_RX_CACHE: "OrderedDict[str, tuple[float, list]]" = OrderedDict()
_RX_LOCK = threading.Lock()
RX_CACHE_MAX = 2000
RX_CACHE_TTL = 12 * 3600


def _rxnav(name: str) -> list[dict]:
    key = fold(name)
    with _RX_LOCK:
        hit = _RX_CACHE.get(key)
        if hit and time.monotonic() - hit[0] < RX_CACHE_TTL:
            _RX_CACHE.move_to_end(key)
            return hit[1]
    if not re.fullmatch(r"[A-Za-z0-9 .,%/()+\-']{2,80}", name):
        raise ValueError("drug name contains unsupported characters")
    data = net.fetch(f"{cms.RXNAV}/drugs.json?name={quote(name)}", hosts=cms.RXNAV_HOSTS,
                     accept="application/json", max_bytes=10_000_000, timeout=25).json()
    out = [{"rxcui": c["rxcui"], "name": c["name"], "tty": c["tty"]}
           for g in (data.get("drugGroup") or {}).get("conceptGroup") or [] for c in g.get("conceptProperties") or []]
    with _RX_LOCK:
        _RX_CACHE[key] = (time.monotonic(), out)
        _RX_CACHE.move_to_end(key)
        while len(_RX_CACHE) > RX_CACHE_MAX:
            _RX_CACHE.popitem(last=False)
    return out


def _suggest(name: str) -> list[str]:
    try:
        d = net.fetch(f"{cms.RXNAV}/spellingsuggestions.json?name={quote(name)}", hosts=cms.RXNAV_HOSTS,
                      accept="application/json", timeout=20).json()
        return ((d.get("suggestionGroup") or {}).get("suggestionList") or {}).get("suggestion") or []
    except (net.FetchError, ValueError):
        return []


def describe_product(p: dict) -> dict:
    """Split an RxNorm SCD/SBD name into ingredients, strengths, dose form, release and brand."""
    name = p["name"]
    brand = None
    bm = re.search(r"\[([^\]]+)\]\s*$", name)
    if bm:
        brand = bm.group(1)
        name = name[: bm.start()].strip()
    name = re.sub(r"^\d+(?:\.\d+)?\s*(?:ML|HR|ACTUAT)\s+", "", name)          # '3 ML ...', '24 HR ...'
    name = re.sub(r"^[\d.]+\s*MG(?:,\s*[\d.]+\s*MG)*\s+Dose\s+", "", name)      # '0.25 MG, 0.5 MG Dose ...'
    name = re.sub(r"^\d+(?:\.\d+)?\s*(?:ML|HR|ACTUAT)\s+", "", name)
    segs = [s.strip() for s in name.split(" / ")]
    ingredients, strengths = [], []
    form = ""
    for i, s in enumerate(segs):
        m = re.match(r"^(.*?)\s+(\d[\d.]*)\s*([A-Z%]+(?:/[A-Z]+)?)\s*(.*)$", s)
        if not m:
            ingredients.append(fold(s))
            continue
        ingredients.append(fold(m.group(1)))
        unit = m.group(3).lower()
        base = unit.split("/")[0]
        v, u = _to_mg(float(m.group(2)), base)
        per = unit.split("/")[1] if "/" in unit else ""
        strengths.append({"value": round(v, 6), "unit": u + (f"/{per}" if per else "")})
        if i == len(segs) - 1:
            form = m.group(4)
    full = p["name"]
    release = ("extended_release" if re.search(r"Extended Release|\b(12|24) HR\b", full) else
               "delayed_release" if "Delayed Release" in full else "immediate_release")
    return {"ingredients": ingredients, "strengths": strengths, "dose_form": form, "release": release,
            "brand": brand, "generic_name": re.sub(r"\s*\[[^\]]+\]\s*$", "", p["name"]).strip(),
            "is_pack": p["tty"] in ("GPCK", "BPCK")}


def _ing_eq(e: str, p: str) -> bool:
    pw = p.split()
    return p.startswith(e) or all(any(x.startswith(w) for x in pw) for w in e.split())


def _ingredient_match(entry_ings: list[str], prod_ings: list[str]) -> bool:
    """Exact ingredient-set match (so a single drug never matches a combination product)."""
    if len(entry_ings) != len(prod_ings):
        return False
    remaining = list(prod_ings)
    for e in entry_ings:
        hit = next((p for p in remaining if _ing_eq(e, p)), None)
        if hit is None:
            return False
        remaining.remove(hit)
    return True


def _strengths_match(entry: list[dict], prod: list[dict]) -> bool:
    if not entry:
        return True
    pvals = [s["value"] for s in prod]
    evals = [s["value"] for s in entry]
    if len(evals) != len(pvals) and len(evals) > 1:
        return False
    if len(evals) == 1 and len(pvals) > 1:
        return False
    used = list(pvals)
    for e in evals:
        hit = next((v for v in used if abs(v - e) <= max(1e-9, 1e-6 * abs(e))), None)
        if hit is None:
            return False
        used.remove(hit)
    return True


def _form_match(form: str | None, dose_form: str) -> bool:
    if not form:
        return True
    words = next(w for f, _, w in _FORMS if f == form)
    return any(w.lower() in dose_form.lower() for w in words)


def candidate_products(parsed: dict) -> dict:
    """Resolve a parsed entry to RxNorm products without substitution. Raises on upstream failure."""
    ings = parsed["ingredients"]
    queries = []
    if parsed["name"]:
        queries.append(parsed["name"])
    if len(ings) > 1 or (ings and fold(ings[0]) != fold(parsed["name"])):
        queries.append(ings[0])
    if parsed.get("alias_in_parentheses"):
        queries.append(parsed["alias_in_parentheses"])
    products, used = {}, []
    for q in queries:
        if not q or len(q) < 2:
            continue
        res = _rxnav(q)
        used.append(q)
        for p in res:
            products[p["rxcui"]] = p
        if res and q == parsed["name"] and len(ings) <= 1:
            break
    described = {rx: {**p, **describe_product(p)} for rx, p in products.items()}
    typed = fold(parsed["name"])
    alias = fold(parsed.get("alias_in_parentheses") or "")
    brands = {fold(d["brand"]) for d in described.values() if d["brand"]}
    brand_intent = typed in brands or (alias and alias in brands)
    brand_name = typed if typed in brands else alias if alias in brands else None
    pool = []
    for d in described.values():
        if d["is_pack"] and not re.search(r"\b(pack|starter|dose pack)\b", parsed["name"], re.I):
            continue
        if brand_intent:
            if not d["brand"] or fold(d["brand"]) != brand_name:
                continue
        else:
            if not _ingredient_match([fold(i) for i in ings], d["ingredients"]):
                continue
        pool.append(d)
    by_ingredient = list(pool)
    after_strength = [d for d in pool if _strengths_match(parsed["strengths"], d["strengths"])]
    after_form = [d for d in after_strength if _form_match(parsed["form"], d["dose_form"])]
    rel = parsed["release"]
    after_release = [d for d in after_form if not rel or d["release"] == rel]
    if brand_intent:
        chosen = after_release
        counterpart = []
        generic_names = {fold(d["generic_name"]) for d in chosen}
        if generic_names:
            ing_query = chosen[0]["ingredients"][0].split()[0] if chosen[0]["ingredients"] else ""
            if ing_query:
                try:
                    for g in _rxnav(ing_query):
                        if g["tty"] == "SCD" and fold(g["name"]) in generic_names:
                            counterpart.append({**g, **describe_product(g)})
                    used.append(ing_query)
                except (net.FetchError, ValueError):
                    pass  # counterpart is informational only
    else:
        generics = [d for d in after_release if d["tty"] in ("SCD", "GPCK")]
        brands_only = [d for d in after_release if d["tty"] in ("SBD", "BPCK")]
        chosen = generics
        counterpart = brands_only
    return {"queries": used, "brand_intent": bool(brand_intent), "brand": brand_name, "products": chosen,
            "counterpart_products": counterpart, "recognized": bool(products), "ingredient_matches": len(by_ingredient),
            "available_after_ingredient": by_ingredient}


# ------------------------------------------------------------------ coverage per plan
_COST_CACHE: dict = {}


def _plan_context(year: int, plan_id: str) -> dict:
    key = (year, plan_id)
    if key in _COST_CACHE:
        return _COST_CACHE[key]
    contract, pbp, seg = cms.parse_plan_id(plan_id)
    conn = cms.puf_db(year)
    if seg is None:
        segs = [r[0] for r in conn.execute("select distinct SEGMENT_ID from plan_info where CONTRACT_ID=? and PLAN_ID=?",
                                           (contract, pbp))]
        if len(segs) != 1:
            raise ValueError(f"{plan_id}: {'segment required, CMS lists ' + str(segs) if segs else 'not in the CMS Part D file for ' + str(year)}")
        seg = segs[0]
    pi = conn.execute("select * from plan_info where CONTRACT_ID=? and PLAN_ID=? and SEGMENT_ID=? limit 1",
                      (contract, pbp, seg)).fetchone()
    if not pi:
        raise ValueError(f"{plan_id}: not in the CMS Part D file for {year}")
    tiers = cms.tier_cost_sharing(year, f"{contract}-{pbp}-{seg}")
    ctx = {"plan_id": f"{contract}-{pbp}-{seg}", "plan_name": pi["PLAN_NAME"], "year": year,
           "formulary_id": pi["FORMULARY_ID"], "part_d_deductible": cms._money(pi["DEDUCTIBLE"]),
           "tiers": tiers.get("tiers", []), "insulin": tiers.get("insulin_cost_sharing", []),
           "contract": contract, "pbp": pbp}
    _COST_CACHE[key] = ctx
    return ctx


def _cost_for(ctx: dict, tier: int, insulin: bool) -> dict:
    rows = [r for r in ctx["tiers"] if r["tier"] == tier and r["coverage_stage"] == "initial coverage"]
    out = {"stage": "initial coverage (published plan design)"}
    for r in rows:
        key = {"30-day": "retail_30_day", "90-day": "retail_90_day", "60-day": "retail_60_day"}.get(r["days_supply"],
                                                                                                   r["days_supply"])
        out[key] = (r["standard_retail"] or {}).get("text") or "not offered"
        if r["days_supply"] == "90-day":
            out["mail_90_day"] = (r["standard_mail"] or {}).get("text") or "not offered"
        if r["preferred_retail"]:
            out[key + "_preferred_pharmacy"] = r["preferred_retail"]["text"]
        out["deductible_applies"] = r["deductible_applies"]
        out["specialty_tier"] = r["specialty_tier"]
    if insulin:
        ins = [r for r in ctx["insulin"] if r["tier"] == tier]
        if ins:
            out["insulin_cost_sharing"] = ins
    if rows and out.get("deductible_applies"):
        out["deductible_note"] = f"Plan Part D deductible ${ctx['part_d_deductible']:,.0f} applies to this tier first."
    if not rows:
        out["note"] = "No CMS cost-sharing rows for this tier."
    return out


def coverage_for_plan(ctx: dict, products: list[dict], counterparts: list[dict]) -> dict:
    conn = cms.puf_db(ctx["year"])
    per = []
    for d in products:
        row = conn.execute("select * from formulary where FORMULARY_ID=? and RXCUI=? limit 1",
                           (ctx["formulary_id"], d["rxcui"])).fetchone()
        if row:
            per.append({"rxcui": d["rxcui"], "product": d["name"], "on_formulary": True,
                        "tier": int(row["TIER_LEVEL_VALUE"]), "prior_authorization": row["PRIOR_AUTHORIZATION_YN"] == "Y",
                        "step_therapy": row["STEP_THERAPY_YN"] == "Y",
                        "quantity_limit": (f"{row['QUANTITY_LIMIT_AMOUNT']} per {row['QUANTITY_LIMIT_DAYS']} days"
                                           if row["QUANTITY_LIMIT_YN"] == "Y" else None)})
        else:
            per.append({"rxcui": d["rxcui"], "product": d["name"], "on_formulary": False})
    sig = {tuple(sorted((k, str(v)) for k, v in x.items() if k not in ("rxcui", "product"))) for x in per}
    out = {"plan_id": ctx["plan_id"], "plan_name": ctx["plan_name"], "year": ctx["year"],
           "formulary_id": ctx["formulary_id"], "products": per}
    if len(sig) > 1:
        out["coverage"] = "varies_by_product_needs_clarification"
        return out
    first = per[0]
    if first["on_formulary"]:
        out.update(coverage="covered", tier=first["tier"], prior_authorization=first["prior_authorization"],
                   step_therapy=first["step_therapy"], quantity_limit=first["quantity_limit"],
                   cost_sharing=_cost_for(ctx, first["tier"], "insulin" in first["product"].lower()))
        return out
    # Not listed: say whether the brand/generic counterpart is listed instead (never substituted).
    listed_counterparts = []
    for d in counterparts:
        row = conn.execute("select TIER_LEVEL_VALUE from formulary where FORMULARY_ID=? and RXCUI=? limit 1",
                           (ctx["formulary_id"], d["rxcui"])).fetchone()
        if row:
            listed_counterparts.append({"rxcui": d["rxcui"], "product": d["name"], "tier": int(row[0])})
    excl = [conn.execute("select * from excluded where CONTRACT_ID=? and PLAN_ID=? and RXCUI=? limit 1",
                         (ctx["contract"], ctx["pbp"], d["rxcui"])).fetchone() for d in products]
    if any(excl):
        out["coverage"] = "covered_as_supplemental_excluded_drug"
        out["detail"] = "Listed in the plan's supplemental (normally excluded) drug file."
    elif listed_counterparts:
        out["coverage"] = "not_listed_but_counterpart_listed_needs_clarification"
        out["counterpart_listed"] = listed_counterparts
        out["detail"] = ("The exact product written is not on this formulary, but its brand/generic counterpart is. "
                         "Not substituted automatically: confirm which product the person uses.")
    else:
        out["coverage"] = "not_on_formulary"
        out["detail"] = ("Exact product not on this plan's CMS formulary file. It may still be obtainable through a "
                         "transition fill or a formulary exception.")
    return out


# ------------------------------------------------------------------ MedCare formulary-site corroboration
_NAV_CACHE: dict = {}


def _medcare_site(parsed: dict, products: list[dict]) -> dict:
    """Corroborate against MedCare's live Formulary Navigator and formulary PDF snapshot (best effort)."""
    ing = (parsed["ingredients"] or [parsed["name"]])[0]
    query = parsed["name"] if len(parsed["name"]) >= 3 else ing
    key = fold(query)
    if key not in _NAV_CACHE:
        try:
            _NAV_CACHE[key] = lookups.navigator_search(query[:50], max_details=0)
        except (net.FetchError, ValueError) as err:
            _NAV_CACHE[key] = {"status": "unavailable", "error": str(err)}
    nav = _NAV_CACHE[key]
    vals = {s["value"] for s in parsed["strengths"]}
    def keep(name):
        nums = {round(float(x.replace(",", "")), 6) for x in re.findall(r"\d[\d,]*\.?\d*", name)} if vals else set()
        if vals and not any(v in nums or round(v * 1000, 6) in nums for v in vals):
            return False
        if parsed["form"] and not _form_match(parsed["form"], name):
            return False
        return True
    matches = [m for m in nav.get("matches", []) if keep(m["name"])][:12] if "matches" in nav else []
    snap = lookups.snapshot_search(ing, limit=200)
    srows = [r for r in snap["matches"] if keep(r["name"])][:12]
    return {"formulary_navigator_live": ({"retrieved_at": nav.get("retrieved_at"), "search_url": nav.get("search_url"),
                                          "matching_rows": [{"name": m["name"], "tier": m["tier"]} for m in matches]}
                                         if "matches" in nav else nav),
            "formulary_pdf_snapshot": {"source_pdf": snap["source_pdf"], "effective": snap["effective"],
                                       "matching_rows": [{k: r.get(k) for k in ("name", "tier", "restrictions", "page")}
                                                         for r in srows]}}


# ------------------------------------------------------------------ batch driver
def _status(item: dict) -> str:
    return item["status"]


def check_list(text: str = "", medications: list | None = None, plan_ids: list[str] | None = None,
               year: int | None = None, start_at: int = 1, include_medcare_site: bool = True,
               time_budget_seconds: int = 200, _workers: int = WORKERS) -> dict:
    t0 = time.time()
    if text and len(text) > MAX_TEXT_CHARS:
        raise ValueError(f"Pasted text exceeds {MAX_TEXT_CHARS:,} characters; split it into parts.")
    plan_ids = [p.strip().upper() for p in (plan_ids or DEFAULT_PLANS)]
    if not 1 <= len(plan_ids) <= 6:
        raise ValueError("Give 1-6 plan IDs.")
    for p in plan_ids:
        cms.parse_plan_id(p)
    year = int(year or DEFAULT_YEAR)

    # 1. Collect entries in input order (text first, then structured items).
    entries, ignored = split_entries(text) if text else ([], [])
    for i, m in enumerate(medications or []):
        if isinstance(m, str):
            m = {"name": m}
        if not isinstance(m, dict):
            entries.append({"original": repr(m)[:200], "line": None, "text": "", "invalid": "item is not an object/string"})
            continue
        bits = [str(m.get(k) or "") for k in ("name", "strength", "form", "release")]
        txt = " ".join(b for b in bits if b).strip()
        entries.append({"original": m.get("original") or txt, "line": None, "text": txt, "structured": m,
                        "invalid": None if m.get("name") else "missing 'name'"})
    total = len(entries)
    if total == 0:
        return {"status": "no_entries", "detail": "No medication entries were found in the input.",
                "ignored_lines": ignored}
    start = max(1, int(start_at))
    window = range(start - 1, min(total, start - 1 + MAX_ENTRIES))

    # 2. Parse every entry (pure, never fails the batch).
    items = []
    for idx, e in enumerate(entries):
        item = {"index": idx + 1, "original": e["original"], "source_line": e["line"]}
        if idx not in window:
            item["status"] = "deferred_batch_limit"
            item["detail"] = f"Not processed in this call (limit {MAX_ENTRIES} per call); see continuation."
            items.append(item)
            continue
        if e.get("invalid"):
            item.update(status="invalid_entry", detail=e["invalid"])
            items.append(item)
            continue
        try:
            parsed = parse_entry(e["text"])
            if e.get("structured"):
                s = e["structured"]
                from_name = parse_entry(str(s["name"]))
                parsed["name"] = from_name["name"] or str(s["name"]).strip()
                parsed["ingredients"] = _ingredients(parsed["name"])
                if not s.get("strength") and from_name["strengths"]:
                    parsed["strengths"] = from_name["strengths"]
                if s.get("strength"):
                    parsed["strengths"] = parse_strengths(str(s["strength"]))
                if s.get("form"):
                    pf = parse_entry(str(s["form"]))
                    parsed["form"] = pf["form"] or parsed["form"]
                    parsed["release"] = pf["release"] or parsed["release"]
                if s.get("release"):
                    parsed["release"] = parse_entry(str(s["release"]))["release"] or parsed["release"]
            item["parsed"] = parsed
            if not parsed["name"] or not re.search(r"[A-Za-z]{2}", parsed["name"]):
                item.update(status="invalid_entry", detail="No medication name could be read from this entry.")
            elif not re.fullmatch(r"[A-Za-z0-9 .,%/()+\-']{2,80}", parsed["name"]):
                item.update(status="invalid_entry", detail="Medication name contains unsupported characters or is too long.")
        except Exception as err:  # parsing must never break the batch
            item.update(status="invalid_entry", detail=f"Could not parse entry: {type(err).__name__}")
        items.append(item)

    # 3. Duplicates (same normalized description) share one lookup.
    keyed = {}
    for it in items:
        if "parsed" not in it or it.get("status"):
            continue
        p = it["parsed"]
        k = (fold(p["name"]), tuple((s["value"], s["unit"]) for s in p["strengths"]), p["form"], p["release"])
        if k in keyed:
            it["duplicate_of"] = keyed[k]["index"]
        else:
            keyed[k] = it

    # 4. Plan contexts (once per plan).
    plan_ctx, plan_errors = {}, {}
    for pid in plan_ids:
        try:
            plan_ctx[pid] = _plan_context(year, pid)
        except (ValueError, net.FetchError, cms.CMSDataError) as err:
            plan_errors[pid] = str(err)

    # 5. Lookups with bounded concurrency and an overall time budget.
    medcare_plans = [pid for pid in plan_ids if pid.startswith("H5767")]

    def lookup(it):
        res = candidate_products(it["parsed"])
        site = _medcare_site(it["parsed"], res["products"]) if (include_medcare_site and medcare_plans) else None
        return res, site

    budget = max(MIN_BUDGET, min(int(time_budget_seconds), 280))
    unique = list(keyed.values())
    pool = ThreadPoolExecutor(max_workers=max(1, min(_workers, 8)))
    futures = {pool.submit(lookup, it): it for it in unique}
    done, not_done = wait(futures, timeout=budget - (time.time() - t0))
    results = {}
    for f in done:
        it = futures[f]
        try:
            results[it["index"]] = ("ok", f.result())
        except net.FetchError as err:
            results[it["index"]] = ("lookup_failed", f"Upstream source unavailable: {err}")
        except Exception as err:
            results[it["index"]] = ("lookup_failed", f"{type(err).__name__}: {err}")
    for f in not_done:
        f.cancel()
        results[futures[f]["index"]] = ("lookup_failed", f"Timed out after the {budget}s batch time budget.")
    pool.shutdown(wait=False, cancel_futures=True)

    # 6. Assemble one result per entry.
    for it in items:
        if it.get("status"):
            continue
        src = it if "duplicate_of" not in it else next(x for x in items if x["index"] == it["duplicate_of"])
        kind, payload = results.get(src["index"], ("lookup_failed", "No result recorded."))
        if kind != "ok":
            it.update(status="lookup_failed", detail=payload,
                      note="Coverage UNKNOWN because the lookup failed. This is not a 'not covered' result.")
            continue
        res, site = payload
        _assemble(it, res, site, plan_ctx, plan_errors)

    # 7. Summary and table.
    counts = {}
    for it in items:
        counts[it["status"]] = counts.get(it["status"], 0) + 1
    per_plan = {}
    for pid in plan_ids:
        c = {}
        for it in items:
            pr = next((p for p in it.get("plans", []) if p["requested_plan_id"] == pid), None)
            key = pr["coverage"] if pr else ("not_evaluated_" + it["status"])
            c[key] = c.get(key, 0) + 1
        per_plan[pid] = c
    processed = [i for i in items if i["status"] != "deferred_batch_limit"]
    next_start = (start - 1 + MAX_ENTRIES + 1) if (start - 1 + MAX_ENTRIES) < total else None
    out = {
        "year": year, "plans_requested": plan_ids,
        "plans": [{k: v for k, v in c.items() if k in ("plan_id", "plan_name", "year", "formulary_id", "part_d_deductible")}
                  for c in plan_ctx.values()],
        "plan_errors": plan_errors or None,
        "entries_total": total, "entries_processed_this_call": len(processed) - 0,
        "batch_limit_per_call": MAX_ENTRIES,
        "continuation": ({"start_at": next_start, "detail": f"{total - (next_start - 1)} more entries; call again with "
                          f"start_at={next_start} (same text)."} if next_start else None),
        "summary": {"by_status": counts, "by_plan_coverage": per_plan,
                    "needs_attention": [i["index"] for i in items if i["status"] in (
                        "needs_clarification", "unrecognized", "lookup_failed", "invalid_entry") or any(
                        p["coverage"] not in ("covered",) for p in i.get("plans", []))]},
        "results": items,
        "table_markdown": _table(items, plan_ids),
        "ignored_lines": ignored,
        "sources": _sources(year, plan_ctx),
        "elapsed_seconds": round(time.time() - t0, 2),
        "limitations": [
            "Coverage is matched by exact RxNorm product in the CMS Part D formulary file for the stated plan and year; "
            "it is published plan design, not a coverage determination or a price quote.",
            "Cost sharing shown is the initial-coverage-stage amount for standard retail/mail pharmacies; the deductible, "
            "catastrophic stage, Extra Help, pharmacy choice and the insulin/vaccine rules can change what a person pays.",
            "'not_on_formulary' means the exact product is absent from the plan file; exceptions and transition fills "
            "may still apply. 'lookup_failed' means unknown, not uncovered.",
            "Entries are never substituted to another strength, form, release type or brand/generic; such cases are "
            "marked needs_clarification.",
            "The list is processed in memory only; it is not stored or logged by this plugin."],
    }
    return out


def _assemble(it, res, site, plan_ctx, plan_errors):
    p = it["parsed"]
    prods = res["products"]
    it["match"] = {"rxnav_queries": res["queries"], "brand_intent": res["brand_intent"], "brand": res["brand"]}
    if not res["recognized"]:
        it.update(status="unrecognized", detail="Not found in RxNorm under this name.",
                  clarification={"reason": "unrecognized_name", "spelling_suggestions": _suggest(p["name"])[:5]})
        return
    if not prods:
        avail = res["available_after_ingredient"]
        options = sorted({d["name"] for d in avail if not d["is_pack"]})[:25]
        if not avail:
            reason = "no_product_with_this_exact_ingredient_combination"
        elif p["strengths"] and not any(_strengths_match(p["strengths"], d["strengths"]) for d in avail):
            reason = "strength_not_found"
        elif p["form"] and not any(_form_match(p["form"], d["dose_form"]) for d in avail):
            reason = "dose_form_not_found"
        elif not res["brand_intent"] and res["counterpart_products"]:
            reason = "only_branded_products_match"
            options = sorted({d["name"] for d in res["counterpart_products"]})[:25]
        else:
            reason = "release_type_or_form_not_found"
        it.update(status="needs_clarification",
                  clarification={"reason": reason, "options_in_rxnorm": options,
                                 "medcare_formulary_site_rows": (site or {}).get("formulary_navigator_live", {}).get(
                                     "matching_rows") if site else None},
                  detail="No product matches exactly what was written; nothing was substituted.")
        if reason == "only_branded_products_match":
            it["detail"] = ("Only branded products exist for what was written (no generic product in RxNorm). Confirm "
                            "the brand before checking coverage.")
        return
    it["matched_products"] = [{"rxcui": d["rxcui"], "product": d["name"], "tty": d["tty"]} for d in prods]
    plans = []
    for pid, ctx in plan_ctx.items():
        try:
            cov = coverage_for_plan(ctx, prods, res["counterpart_products"])
        except Exception as err:
            cov = {"plan_id": ctx["plan_id"], "coverage": "lookup_failed", "detail": f"{type(err).__name__}: {err}"}
        cov["requested_plan_id"] = pid
        plans.append(cov)
    for pid, err in plan_errors.items():
        plans.append({"requested_plan_id": pid, "coverage": "lookup_failed", "detail": err})
    it["plans"] = plans
    if site:
        it["medcare_formulary_site"] = site
        live_rows = (site.get("formulary_navigator_live") or {}).get("matching_rows")
        if isinstance(live_rows, list):
            cms_cov = [x for x in plans if x["requested_plan_id"].startswith("H5767") and x.get("coverage") == "covered"]
            if live_rows and not cms_cov:
                it["corroboration_note"] = ("MedCare's live formulary site lists matching rows although the CMS file "
                                            "match failed for this exact product; confirm with the plan.")
            elif cms_cov and not live_rows:
                it["corroboration_note"] = ("Covered in the CMS file but no matching row was found on MedCare's live "
                                            "formulary site search; confirm with the plan.")
    ambiguous = any(x.get("coverage") in ("varies_by_product_needs_clarification",
                                          "not_listed_but_counterpart_listed_needs_clarification") for x in plans)
    if ambiguous:
        it["status"] = "needs_clarification"
        it["clarification"] = {"reason": "matching_products_differ_in_coverage_or_counterpart_listed",
                               "options": it["matched_products"]}
    elif len(prods) > 1:
        it["status"] = "matched_multiple_equivalent"
        it["detail"] = (f"{len(prods)} products match what was written (e.g. different release types or package "
                        "sizes); all have identical coverage terms on every requested plan.")
    else:
        it["status"] = "matched"
    if all(x.get("coverage") == "lookup_failed" for x in plans):
        it["status"] = "lookup_failed"


def _fmt_plan(p: dict) -> str:
    c = p.get("coverage")
    if c == "covered":
        r = [f"T{p['tier']}"]
        if p.get("prior_authorization"):
            r.append("PA")
        if p.get("step_therapy"):
            r.append("ST")
        if p.get("quantity_limit"):
            r.append(f"QL {p['quantity_limit']}")
        cost = (p.get("cost_sharing") or {}).get("retail_30_day")
        if cost:
            r.append(f"{cost}/30d")
        if (p.get("cost_sharing") or {}).get("deductible_applies"):
            r.append("deductible applies")
        return "Covered " + ", ".join(r)
    return {"not_on_formulary": "NOT on formulary", "lookup_failed": "UNKNOWN (lookup failed)",
            "varies_by_product_needs_clarification": "Varies by product: clarify",
            "not_listed_but_counterpart_listed_needs_clarification": "Not listed; brand/generic counterpart listed: clarify",
            "covered_as_supplemental_excluded_drug": "Covered (supplemental excluded-drug benefit)"}.get(c, c or "")


def _cell(s: str) -> str:
    return str(s).replace("|", "\\|").replace("\n", " ")


def _table(items: list[dict], plan_ids: list[str]) -> str:
    head = "| # | Entry as written | Matched product(s) | Status | " + " | ".join(plan_ids) + " |"
    sep = "|---|---|---|---|" + "---|" * len(plan_ids)
    rows = [head, sep]
    for it in items:
        prods = "; ".join(p["product"] for p in it.get("matched_products", [])[:3])
        if len(it.get("matched_products", [])) > 3:
            prods += f"; +{len(it['matched_products']) - 3} more"
        if not prods and it.get("clarification"):
            prods = it["clarification"].get("reason", "")
        cells = []
        for pid in plan_ids:
            p = next((x for x in it.get("plans", []) if x["requested_plan_id"] == pid), None)
            cells.append(_fmt_plan(p) if p else "—")
        dup = f" (duplicate of #{it['duplicate_of']})" if it.get("duplicate_of") else ""
        rows.append(f"| {it['index']} | {_cell(it['original'])} | {_cell(prods)} | {it['status']}{dup} | "
                    + " | ".join(_cell(c) for c in cells) + " |")
    return "\n".join(rows)


def _sources(year: int, plan_ctx: dict) -> dict:
    out = {"rxnorm": {"api": "NLM RxNav drugs.json", "url": f"{cms.RXNAV}/drugs.json", "retrieved_at": net.now_iso()}}
    if plan_ctx:
        try:
            src = cms.puf_source(year)
            out["cms_part_d_file"] = {k: src.get(k) for k in ("dataset", "source_url", "file_date", "retrieved_at",
                                                               "last_checked_at", "record_layout")}
        except (net.FetchError, cms.CMSDataError) as err:
            out["cms_part_d_file"] = {"status": "unavailable", "error": str(err)}
    return out


def compact_item(it: dict) -> dict:
    """Per-medication record for the text channel (complete, without bulky corroboration rows)."""
    out = {k: it.get(k) for k in ("index", "original", "status", "duplicate_of", "detail") if it.get(k) is not None}
    if it.get("parsed"):
        p = it["parsed"]
        out["read_as"] = {"name": p["name"], "strengths": [s["text"] for s in p["strengths"]] or None,
                          "form": p["form"], "release": p["release"]}
    if it.get("matched_products"):
        out["matched_products"] = [f"{m['product']} (RXCUI {m['rxcui']})" for m in it["matched_products"]]
    if it.get("clarification"):
        c = dict(it["clarification"])
        c.pop("medcare_formulary_site_rows", None)
        out["clarification"] = c
    plans = []
    for p in it.get("plans", []):
        q = {k: p.get(k) for k in ("plan_id", "year", "formulary_id", "coverage", "tier", "prior_authorization",
                                   "step_therapy", "quantity_limit") if p.get(k) is not None}
        if p.get("cost_sharing"):
            q["cost_sharing"] = {k: v for k, v in p["cost_sharing"].items() if k != "insulin_cost_sharing"}
            if p["cost_sharing"].get("insulin_cost_sharing"):
                q["cost_sharing"]["insulin_rule"] = "Insulin cost-sharing rows apply (see structured result)."
        if p.get("counterpart_listed"):
            q["counterpart_listed"] = p["counterpart_listed"]
        if p.get("coverage") == "varies_by_product_needs_clarification":
            q["by_product"] = [{k: x.get(k) for k in ("product", "on_formulary", "tier", "prior_authorization",
                                                      "step_therapy", "quantity_limit")} for x in p["products"]]
        if p.get("detail") and p.get("coverage") != "covered":
            q["detail"] = p["detail"]
        plans.append(q)
    if plans:
        out["plans"] = plans
    if it.get("corroboration_note"):
        out["corroboration_note"] = it["corroboration_note"]
    return out


def render_text(result: dict) -> str:
    import json as _json
    if "results" not in result:
        return _json.dumps(result, indent=1)
    lines = [f"Medication list check — plan year {result['year']}; plans {', '.join(result['plans_requested'])}",
             f"Entries: {result['entries_total']} total, {result['entries_processed_this_call']} processed this call "
             f"(limit {result['batch_limit_per_call']} per call).", ""]
    if result.get("continuation"):
        lines.append(f"CONTINUATION REQUIRED: {result['continuation']['detail']}")
    if result.get("plan_errors"):
        lines.append(f"Plan errors: {result['plan_errors']}")
    lines += ["Summary: " + _json.dumps(result["summary"]), "", result["table_markdown"], "",
              "Per-medication detail (input order):"]
    lines += [_json.dumps(compact_item(it), ensure_ascii=False) for it in result["results"]]
    if result.get("ignored_lines"):
        lines += ["", "Lines not treated as medications: " + _json.dumps(result["ignored_lines"], ensure_ascii=False)]
    lines += ["", "Sources: " + _json.dumps(result["sources"]), "Limitations: " + " ".join(result["limitations"])]
    return "\n".join(lines)
