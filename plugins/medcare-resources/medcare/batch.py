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
    r"\b(dob|d\.o\.b|date of birth|ssn|social security|mbi|medicare (?:number|id|#)|member(?: id| #| number)?|"
    r"patient(?: name)?|beneficiary|address|phone|mrn|policy (?:number|#))\b|\b(?:first |last |full )?name\s*[:=]|"
    r"\b\d{3}-\d{2}-\d{4}\b|\b\d{9}\b|"
    r"\b[1-9][A-Za-z][A-Za-z0-9]\d[A-Za-z][A-Za-z0-9]\d[A-Za-z]{2}\d{2}\b", re.I)
_SIG = re.compile(
    r"\b(take|takes|taking|inject|apply|inhale|instill|use|chew|dissolve|place|insert|daily|nightly|weekly|monthly|"
    r"once|twice|three times|four times|every|q\d+h|qd|qod|bid|tid|qid|qhs|qam|qpm|hs|prn|as needed|by mouth|"
    r"orally|po|sl|at bedtime|in the morning|with (?:food|meals)|before|after|for \d+|x\s*\d+\s*days|"
    r"#\s*\d+|qty|quantity|refills?|disp|sig|start(?:ed)?|since|stopped|discontinued|prescribed|per dr|"
    r"\d{1,2}/\d{1,2}/\d{2,4})\b", re.I)
_ABBREV = {"hctz": "hydrochlorothiazide", "apap": "acetaminophen", "asa": "aspirin", "ntg": "nitroglycerin",
           "kcl": "potassium chloride", "smx": "sulfamethoxazole", "tmp": "trimethoprim", "mtx": "methotrexate",
           "ec asa": "aspirin", "levo": "levothyroxine", "nph": "insulin isophane", "vitamin d3": "cholecalciferol",
           "vit d3": "cholecalciferol", "vitamin d2": "ergocalciferol", "vit d2": "ergocalciferol"}
_UNIT = r"(mg|mcg|µg|ug|g|gm|grams?|units?|unt|iu|ml|mls|%|meq|mmol)"
_PER = (r"(?:\s*(?:/|\bper\b)\s*(\d+(?:\.\d+)?)?\s*(ml|mls|g|gm|act(?:uat(?:ions?)?)?|puffs?|sprays?|doses?|"
        r"inhalations?|hrs?|hours?|h|days?|d)(?![a-z]))?")
_STRENGTH = re.compile(
    rf"(?<![\w.])(\d+(?:\.\d+)?(?:\s*(?:/|-)\s*\d+(?:\.\d+)?)*)\s*{_UNIT}(?![a-z]){_PER}", re.I)
# numbers that are not part of a unit-bearing strength (e.g. 'losartan/HCTZ 50-12.5', 'aspirin 81')
_BARE_NUMS = re.compile(r"(?<![\w.\-#])(\d+(?:\.\d+)?(?:\s*(?:/|-)\s*\d+(?:\.\d+)?)*)(?![\w.])"
                        r"(?!\s*(?:x\b|times\b|tabs?\b|tablets?\b|caps?\b|capsules?\b|pills?\b|puffs?\b|drops?\b|"
                        r"sprays?\b|pens?\b|patch(?:es)?\b|refills?\b|days?\b))", re.I)
_RELEASE = [
    ("extended_release", re.compile(r"\b(er|xr|xl|sr|cr|la|xr\d*|extended[- ]release|sustained[- ]release|"
                                    r"controlled[- ]release|24[- ]?(?:hr|hour|h)|12[- ]?(?:hr|hour|h))\b", re.I)),
    ("delayed_release", re.compile(r"\b(dr|ec|delayed[- ]release|enteric[- ]coated)\b", re.I)),
    ("immediate_release", re.compile(r"\b(ir|immediate[- ]release)\b", re.I)),
]
_INJECTABLES = ["Injectable", "Injection", "Pen Injector", "Auto-Injector", "Prefilled Syringe", "Cartridge"]
# (form, pattern, RxNorm dose-form words that match, words that must NOT appear). Order matters: specific first.
# An explicitly written delivery form (vial, pen, cartridge, syringe, nebulizer, metered-dose inhaler, dry-powder
# inhaler, soft-mist) only matches that delivery form; generic words ('injection', 'inhalation') stay broad and
# any coverage difference between the remaining products is then raised as a clarification.
_FORMS = [
    ("disintegrating", r"\b(odt|orally disintegrating|disintegrating)\b", ["Disintegrating"], []),
    ("chewable", r"\b(chewable|chew tab)\b", ["Chewable"], []),
    ("vial", r"\b(vials?)\b", ["Injectable Solution", "Injectable Suspension", "Injection"],
     ["Pen", "Auto-Injector", "Syringe", "Cartridge"]),
    ("cartridge", r"\b(cartridges?|penfill)\b", ["Cartridge"], []),
    ("autoinjector", r"\b(auto-?injectors?|autoinjectors?)\b", ["Auto-Injector"], []),
    ("pen", r"\b(pen|pens|flextouch|flexpen|kwikpen|solostar|tempo pen)\b", ["Pen Injector", "Auto-Injector"], []),
    ("syringe", r"\b(prefilled syringes?|syringes?)\b", ["Prefilled Syringe", "Syringe"], []),
    ("injection", r"\b(inj|injection|injectable|subq|subcutaneous|im|shot)\b", _INJECTABLES, []),
    ("nebulizer", r"\b(nebulizer|nebuliser|nebuli[sz]ed|nebs?|respules?|inhalation solution|inhalation suspension)\b",
     ["Inhalation Solution", "Inhalation Suspension"], []),
    ("metered_dose_inhaler", r"\b(mdi|hfa|metered[- ]dose(?: inhaler)?|aerosol inhaler)\b", ["Metered Dose Inhaler"], []),
    ("dry_powder_inhaler", r"\b(dpi|dry powder(?: inhaler)?|diskus|ellipta|handihaler|twisthaler|pressair|respiclick|"
                           r"inhub|flexhaler)\b", ["Dry Powder Inhaler", "Inhalation Powder"], []),
    ("soft_mist_inhaler", r"\b(respimat|soft[- ]mist)\b", ["Inhalation Spray"], []),
    ("inhaler", r"\b(inhalers?|puffers?)\b", ["Inhaler", "Inhalation Spray", "Inhalation Powder"], []),
    ("inhalation", r"\b(inhalation|inhaled)\b", ["Inhal"], []),
    ("patch", r"\b(patch|patches|transdermal)\b", ["Transdermal"], []),
    ("nasal", r"\b(nasal)\b", ["Nasal"], []),
    ("ophthalmic", r"\b(eye drops?|ophthalmic|ophth)\b", ["Ophthalmic"], []),
    ("otic", r"\b(ear drops?|otic)\b", ["Otic"], []),
    ("cream", r"\b(cream)\b", ["Cream"], []),
    ("ointment", r"\b(ointment|oint)\b", ["Ointment"], []),
    ("gel", r"\b(gel)\b", ["Gel"], []),
    ("suspension", r"\b(suspension|susp)\b", ["Suspension"], []),
    ("oral_liquid", r"\b(oral solution|oral liquid|liquid|syrup|elixir)\b", ["Oral Solution", "Syrup", "Elixir"], []),
    ("solution", r"\b(solution|soln)\b", ["Solution"], []),
    ("powder", r"\b(powder)\b", ["Powder"], []),
    ("suppository", r"\b(suppository|supp)\b", ["Suppository", "Rectal"], []),
    ("capsule", r"\b(cap|caps|capsule|capsules)\b", ["Capsule"], []),
    ("tablet", r"\b(tab|tabs|tablet|tablets|caplet)\b", ["Tablet"], []),
]

# Numeric commas are protected before any comma splitting: '1,000 units' (thousands) and '0,025 mg' / '2,5 mg'
# (decimal comma) must never be read as list separators. Markers are restored for display.
_THOU, _DEC = "\x01", "\x02"


def protect_numeric_commas(text: str) -> str:
    text = re.sub(r"(?<![\d.,])0,(?=\d)", "0" + _DEC, text)                       # 0,025 -> decimal
    text = re.sub(r"(?<![\d.,])([1-9]\d{0,2})((?:,\d{3})+)(?![\d,])",
                  lambda m: m.group(1) + m.group(2).replace(",", _THOU), text)      # 1,000 / 12,500
    text = re.sub(r"(?<=\d),(?=\d)", _DEC, text)                                   # 2,5 -> decimal
    return text


def display_text(s: str) -> str:
    return s.replace(_THOU, ",").replace(_DEC, ",")


def parse_text(s: str) -> str:
    return s.replace(_THOU, "").replace(_DEC, ".")


def _norm_unit(unit: str) -> str:
    u = unit.lower()
    return {"mcg": "mg", "µg": "mg", "ug": "mg", "g": "mg", "gm": "mg", "gram": "mg", "grams": "mg",
            "unit": "unt", "units": "unt", "unt": "unt", "iu": "unt", "mls": "ml"}.get(u, u)


def _scale(value: float, unit: str) -> float:
    u = unit.lower()
    if u in ("mcg", "µg", "ug"):
        return value / 1000.0
    if u in ("g", "gm", "gram", "grams"):
        return value * 1000.0
    return value


def _norm_per(per: str | None, qty: str | None) -> tuple[str | None, float]:
    """Denominator unit and the factor that converts 'per qty' into 'per 1'."""
    if not per:
        return None, 1.0
    p = per.lower()
    q = float(qty) if qty else 1.0
    if p in ("ml", "mls"):
        return "ml", q
    if p in ("g", "gm"):
        return "g", q
    if p.startswith(("act", "puff", "spray", "dose", "inhal")):
        return "act", q
    if p in ("day", "days", "d"):
        return "hr", 24.0 * q
    return "hr", q  # hr, hrs, hour(s), h


def _to_mg(value: float, unit: str) -> tuple[float, str]:  # kept for callers outside this module
    return _scale(value, unit), _norm_unit(unit)


def parse_strengths(text: str) -> list[dict]:
    """Unit-aware strengths, in written order. Each value keeps its unit, denominator and the written group it
    came from, e.g. '50-12.5 mg' -> two mg values (group 0), '100 units/mL' -> 100 unt per ml,
    '400 mg/5 mL' -> 80 mg per ml, '1%' -> 1 %."""
    text = parse_text(protect_numeric_commas(text))
    out = []
    for g, m in enumerate(_STRENGTH.finditer(text)):
        nums = [n for n in re.split(r"\s*(?:/|-)\s*", m.group(1)) if n]
        unit = m.group(2)
        per, factor = _norm_per(m.group(4), m.group(3))
        nu = _norm_unit(unit)
        for n in nums:
            v = _scale(float(n), unit) / factor
            out.append({"value": round(v, 6), "unit": nu + (f"/{per}" if per else ""), "base_unit": nu,
                        "per": per, "group": g, "text": m.group(0).strip(), "derived": factor != 1.0})
    return out


def looks_like_identifier(text: str) -> bool:
    return bool(text) and bool(_IDENTIFIER.search(display_text(str(text))))


_REDACTED = "[redacted: looks like a personal identifier; not sent to any service]"


def split_entries(text: str) -> tuple[list[dict], list[dict]]:
    """Split pasted text into entries. Returns (entries, ignored_lines). Never drops content silently."""
    entries, ignored = [], []
    lines = re.split(r"\r?\n", text)
    for ln_no, raw_line in enumerate(lines, 1):
        line = raw_line.strip()
        if not line:
            continue
        if looks_like_identifier(line):
            ignored.append({"line": ln_no, "reason": "possible_personal_identifier_redacted", "text": _REDACTED})
            continue
        if _HEADER.match(line) or (line.endswith(":") and not _STRENGTH.search(parse_text(protect_numeric_commas(line)))):
            ignored.append({"line": ln_no, "reason": "header", "text": line})
            continue
        line = protect_numeric_commas(line)
        # explicit delimiters that never occur inside a single drug description
        parts = [p for p in re.split(r"\s*[;|\t]\s*|\s{3,}", line) if p.strip()]
        for part in parts:
            for seg in _split_commas(part):
                seg_clean = _BULLET.sub("", seg).strip(" .,:")
                if not seg_clean:
                    continue
                entries.append({"original": display_text(seg.strip()), "line": ln_no, "text": seg_clean})
    return entries, ignored


def _split_commas(part: str) -> list[str]:
    """Split 'drug A 10 mg, drug B 20 mg' but keep 'drug 500 mg, take 1 tablet twice daily' together.
    Numeric commas were already protected, so only real separators are seen here."""
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


def detect_form(text: str) -> str | None:
    for fname, rx, _, _ in _FORMS:
        if re.search(rx, text, re.I):
            return fname
    return None


_GLUED = re.compile(r"\b([A-Za-z]{4,})(?=\d)")  # 'lisinopril10', 'HCTZ50-12.5' -> word + space + number


def parse_entry(text: str) -> dict:
    text = _GLUED.sub(r"\1 ", protect_numeric_commas(text))
    sig = ""
    if "," in text:  # anything after the first comma was merged as instructions by _split_commas
        head, tail = text.split(",", 1)
        if parse_strengths(head) or not parse_strengths(tail):
            text, sig = head, tail.strip()
    m = _SIG.search(text)
    core = text
    if m and m.start() > 0:
        core, sig = text[: m.start()], (text[m.start():] + (" " + sig if sig else "")).strip()
    core = parse_text(core)
    strengths = parse_strengths(core)
    release = None
    for name, rx in _RELEASE:
        if rx.search(core):
            release = name
            break
    form = detect_form(core)
    name = _STRENGTH.sub(" ", core)
    for _, rx in _RELEASE:
        name = rx.sub(" ", name)
    # Numbers left without a unit are kept as an unresolved strength constraint (never silently dropped):
    # 'losartan/HCTZ 50-12.5' or 'aspirin 81' -> clarification, not "any strength". Counts such as '1 tab'
    # are recognised while the form words are still present.
    name = re.sub(r"(?<![\w.])\d+(?:\.\d+)?\s*(?:x|times|tabs?|tablets?|caps?|capsules?|pills?|puffs?|drops?|sprays?|"
                  r"pens?|patch(?:es)?|refills?|days?)\b", " ", name, flags=re.I)  # counts, not strengths
    bare = [b.group(1).strip() for b in _BARE_NUMS.finditer(name)]
    name = _BARE_NUMS.sub(" ", name)
    for _, rx, _, _ in _FORMS:
        name = re.sub(rx, " ", name, flags=re.I)
    name = re.sub(r"\boral\b|\bby mouth\b", " ", name, flags=re.I)
    alias = None
    pm = re.search(r"\(([^)]*)\)", name)
    if pm:
        alias = pm.group(1).strip() or None
        name = name[: pm.start()] + name[pm.end():]
    name = re.sub(r"(?<![\w.])\d+(?:\.\d+)?(?![\w.])", " ", name)  # remaining counts like '1 tab'
    name = re.sub(r"\s+", " ", name).strip(" -,.:/+&#")
    ingredients = _ingredients(name)
    return {"name": name, "alias_in_parentheses": alias, "ingredients": ingredients,
            "strengths": strengths, "unresolved_numbers": bare or None, "form": form, "release": release,
            "sig": display_text(sig).strip() or None}


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


def _guard_upstream(q: str) -> None:
    """Last line of defence: nothing that looks like a personal identifier is ever sent upstream."""
    if looks_like_identifier(q):
        raise ValueError("query blocked: looks like a personal identifier")


def _rxnav(name: str) -> list[dict]:
    _guard_upstream(name)
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
    if looks_like_identifier(name):
        return []
    try:
        d = net.fetch(f"{cms.RXNAV}/spellingsuggestions.json?name={quote(name)}", hosts=cms.RXNAV_HOSTS,
                      accept="application/json", timeout=20).json()
        return ((d.get("suggestionGroup") or {}).get("suggestionList") or {}).get("suggestion") or []
    except (net.FetchError, ValueError):
        return []


def describe_product(p: dict) -> dict:
    """Split an RxNorm SCD/SBD name into ingredients, per-ingredient strengths (value, unit, denominator),
    package quantity, dose form, release and brand."""
    name = p["name"]
    brand = None
    bm = re.search(r"\[([^\]]+)\]\s*$", name)
    if bm:
        brand = bm.group(1)
        name = name[: bm.start()].strip()
    quantity = None
    name = re.sub(r"^(?:(?:NDA|ANDA|BLA)\d+|Sensor|Digital)\s+", "", name)   # RxNorm disambiguation prefixes
    for _ in range(3):
        qm = re.match(r"^(\d+(?:\.\d+)?)\s*(ML|HR|ACTUAT|G)\s+", name)
        if not qm:
            break
        if qm.group(2) == "ML":
            quantity = {"value": float(qm.group(1)), "unit": "ml"}
        name = name[qm.end():]
    name = re.sub(r"^[\d.]+\s*MG(?:,\s*[\d.]+\s*MG)*\s+Dose\s+", "", name)      # '0.25 MG, 0.5 MG Dose ...'
    segs = [s.strip() for s in name.split(" / ")]
    ingredients, strengths = [], []
    form = ""
    for i, s in enumerate(segs):
        m = re.match(r"^(.*?)\s+(\d[\d.]*)\s*([A-Z%]+(?:/[A-Z]+)?)\s*(.*)$", s)
        if not m:
            ingredients.append(fold(s))
            continue
        ing = fold(m.group(1))
        ingredients.append(ing)
        unit = m.group(3).lower()
        base, _, per = unit.partition("/")
        per_n, _ = _norm_per(per, None) if per else (None, 1.0)
        nu = _norm_unit(base)
        strengths.append({"ingredient": ing, "value": round(_scale(float(m.group(2)), base), 6),
                          "unit": nu + (f"/{per_n}" if per_n else ""), "base_unit": nu, "per": per_n})
        if i == len(segs) - 1:
            form = m.group(4)
    full = p["name"]
    release = ("extended_release" if re.search(r"Extended Release|\b(12|24) HR\b", full) else
               "delayed_release" if "Delayed Release" in full else "immediate_release")
    return {"ingredients": ingredients, "strengths": strengths, "quantity": quantity, "dose_form": form,
            "release": release, "brand": brand,
            "generic_name": re.sub(r"\s*\[[^\]]+\]\s*$", "", p["name"]).strip(), "is_pack": p["tty"] in ("GPCK", "BPCK")}


def _word_eq(w: str, x: str) -> bool:
    """Written word w names product word x: equal, or a >=4-letter abbreviation that is not a different
    suffixed product (e.g. 'glargine' never matches the biosimilar 'glargine-yfgn')."""
    return x == w or (len(w) >= 4 and x.startswith(w) and "-" not in x[len(w):])


def _ing_eq(e: str, p: str) -> bool:
    """Written ingredient e names product ingredient p (salt words may be omitted: 'metformin' ->
    'metformin hydrochloride'), never a different suffixed product."""
    pw = p.split()
    return p == e or p.startswith(e + " ") or all(any(_word_eq(w, x) for x in pw) for w in e.split())


def _ingredient_match(entry_ings: list[str], prod_ings: list[str]) -> bool:
    """Exact ingredient-set match (so a single drug never matches a combination product)."""
    return _ingredient_assignment(entry_ings, prod_ings) is not None


def _ingredient_assignment(entry_ings: list[str], prod_ings: list[str]) -> list[int] | None:
    """Map each written ingredient to the index of the product ingredient it names (exact set), else None."""
    if len(entry_ings) != len(prod_ings):
        return None
    remaining = list(range(len(prod_ings)))
    out = []
    for e in entry_ings:
        hit = next((i for i in remaining if _ing_eq(e, prod_ings[i])), None)
        if hit is None:
            return None
        remaining.remove(hit)
        out.append(hit)
    return out


def _close(a: float, b: float, rel: float = 1e-3) -> bool:
    return abs(a - b) <= max(1e-9, rel * max(abs(a), abs(b)))


def _value_ok(e: dict, p: dict, unitless: bool = False) -> bool:
    """One written strength value against one product ingredient strength: value AND unit AND denominator."""
    if unitless:
        return _close(e["value"], p["value"])
    if e["base_unit"] == "%" and e["per"] is None:
        if p["base_unit"] == "%" and p["per"] is None:
            return _close(e["value"], p["value"])
        # 1% = 10 mg/g (w/w) or 10 mg/mL (w/v); RxNorm normalises percentages this way
        return p["base_unit"] == "mg" and p["per"] in ("g", "ml") and _close(e["value"] * 10, p["value"])
    # a concentration computed from 'per N mL/hr' (e.g. 2.5 mg/3 mL = 0.8333 mg/mL) may differ from RxNorm's
    # rounded value (0.83 MG/ML) by under 1%; written values must match to 0.1%.
    rel = 0.01 if e.get("derived") else 1e-3
    return e["base_unit"] == p["base_unit"] and e["per"] == p["per"] and _close(e["value"], p["value"], rel)


def _assign(evals: list[dict], pstr: list[dict], order: list[int] | None, unitless: bool) -> bool:
    if order is not None:  # association stated by the writer (ingredient i <-> value i)
        return all(_value_ok(evals[i], pstr[j], unitless) for i, j in enumerate(order))
    from itertools import permutations
    return any(all(_value_ok(evals[i], pstr[j], unitless) for i, j in enumerate(perm))
               for perm in permutations(range(len(pstr))))


def strength_match(parsed: dict, d: dict, unitless: bool = False) -> tuple[bool, str]:
    """Does product `d` have exactly the strength written? Returns (ok, association).

    * Units and denominators must agree (mg vs units, per mL vs none, per actuation, per hour ...).
    * Pure volumes ('3 mL') are package sizes and must equal the product's package quantity.
    * When the entry names each ingredient and gives one value per ingredient, values are bound to
      ingredients in written order ('losartan/HCTZ 50/12.5' = losartan 50 + HCTZ 12.5); the reversed
      product never matches. Otherwise (e.g. a brand name with '50-12.5') the values must match as a set,
      and the association is reported as 'not_stated'.
    * With unitless=True the numbers alone are compared (used only to list options for clarification).
    """
    allv = parsed.get("strengths") or []
    if unitless:
        nums = []
        for b in parsed.get("unresolved_numbers") or []:
            nums += [{"value": float(x)} for x in re.split(r"\s*(?:/|-)\s*", b) if x]
        evals = nums
        volumes = []
    else:
        volumes = [e for e in allv if e["base_unit"] == "ml" and e["per"] is None]
        evals = [e for e in allv if not (e["base_unit"] == "ml" and e["per"] is None)]
    for v in volumes:
        q = d.get("quantity")
        if not q or not _close(q["value"], v["value"]):
            return False, "package_volume_mismatch"
    if not evals:
        return True, "no_strength_written"
    pstr = d["strengths"]
    if len(evals) != len(pstr):
        return False, "value_count_mismatch"
    ings = parsed.get("ingredients") or []
    order = None
    if len(pstr) > 1 and len(ings) == len(evals):
        order = _ingredient_assignment([fold(i) for i in ings], [x["ingredient"] for x in pstr])
    if len(pstr) == 1:
        return _assign(evals, pstr, [0], unitless), "single"
    if order is not None:
        return _assign(evals, pstr, order, unitless), "written_order"
    return _assign(evals, pstr, None, unitless), "not_stated"


def _strengths_match(entry: list[dict], prod: list[dict]) -> bool:  # compatibility helper
    return strength_match({"strengths": entry, "ingredients": []}, {"strengths": prod, "quantity": None})[0]


def _form_match(form: str | None, dose_form: str) -> bool:
    if not form:
        return True
    _, _, words, excl = next(f for f in _FORMS if f[0] == form)
    df = dose_form.lower()
    return any(w.lower() in df for w in words) and not any(x.lower() in df for x in excl)


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
    # A parenthetical name is an explicit constraint ('lisinopril (Zestril) 10 mg' = the Zestril brand). It is
    # captured before any lookup, always queried, and must be confirmed by RxNorm; otherwise the entry is
    # returned for clarification. It is never dropped in favour of the generic.
    alias_raw = (parsed.get("alias_in_parentheses") or "").strip()
    alias = fold(alias_raw)
    generic_marker = alias in ("generic", "gen", "generic version", "generic form")
    explicit_alias = bool(alias) and not generic_marker
    products, used = {}, []
    for q in queries:
        if not q or len(q) < 2 or (generic_marker and q == alias_raw):
            continue
        res = _rxnav(q)
        used.append(q)
        for p in res:
            products[p["rxcui"]] = p
        if res and q == parsed["name"] and len(ings) <= 1 and not explicit_alias:
            break
    described = {rx: {**p, **describe_product(p)} for rx, p in products.items()}
    typed = fold(parsed["name"])
    brands = {fold(d["brand"]) for d in described.values() if d["brand"]}
    alias_conflict = None
    if explicit_alias:
        ref_ings = [fold(i) for i in ings]
        if typed in brands:
            ref_ings += [i for d in described.values() if d["brand"] and fold(d["brand"]) == typed
                         for i in d["ingredients"]]
        alias_is_ingredient = any(_ing_eq(alias, i) or _ing_eq(i, alias) for i in ref_ings)
        if alias in brands:
            if typed in brands and typed != alias:
                alias_conflict = "parenthetical_brand_conflicts_with_written_brand"
        elif not alias_is_ingredient:
            alias_conflict = "parenthetical_name_not_found_in_rxnorm_results"
    brand_intent = typed in brands or (explicit_alias and alias in brands)
    brand_name = typed if typed in brands else alias if (explicit_alias and alias in brands) else None
    if alias_conflict:
        return {"queries": used, "brand_intent": bool(brand_intent),
                "brand": brand_name, "products": [], "counterpart_products": [], "recognized": bool(products),
                "ingredient_matches": 0, "available_after_ingredient": [], "unresolved_options": [],
                "strength_association": None, "alias_unresolved": {"reason": alias_conflict, "written": alias_raw},
                "alias_options": sorted({d["name"] for d in described.values()
                                         if _ingredient_match([fold(i) for i in ings], d["ingredients"])
                                         and not d["is_pack"]})[:25]}
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
    rel = parsed["release"]
    association = {}
    after_strength = []
    for d in pool:
        ok, mode = strength_match(parsed, d)
        if ok:
            after_strength.append(d)
            association[d["rxcui"]] = mode
    after_form = [d for d in after_strength if _form_match(parsed["form"], d["dose_form"])]
    after_release = [d for d in after_form if not rel or d["release"] == rel]
    unresolved_options = []
    if parsed.get("unresolved_numbers"):
        # numbers without a unit (or not explained): never treated as "any strength"; list what they could mean
        unresolved_options = [d for d in pool if strength_match(parsed, d, unitless=True)[0]
                              and _form_match(parsed["form"], d["dose_form"]) and (not rel or d["release"] == rel)]
        after_release = []
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
    modes = {association.get(d["rxcui"]) for d in chosen}
    return {"queries": used, "brand_intent": bool(brand_intent), "brand": brand_name, "products": chosen,
            "counterpart_products": counterpart, "recognized": bool(products), "ingredient_matches": len(by_ingredient),
            "available_after_ingredient": by_ingredient, "unresolved_options": unresolved_options,
            "strength_association": ("not_stated" if "not_stated" in modes else
                                     "written_order" if "written_order" in modes else
                                     next(iter(modes), None) if len(modes) == 1 else None)}


# ------------------------------------------------------------------ coverage per plan
def _plan_context(conn, year: int, plan_id: str) -> dict:
    """Plan facts read from ONE open CMS Part D file (`conn`). Built per call and never cached across calls,
    so a CMS refresh can never leave old formulary or cost data behind newer provenance."""
    contract, pbp, seg = cms.parse_plan_id(plan_id)
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
    tiers = cms.tier_cost_sharing(year, f"{contract}-{pbp}-{seg}", conn=conn)
    return {"plan_id": f"{contract}-{pbp}-{seg}", "plan_name": pi["PLAN_NAME"], "year": year,
            "formulary_id": pi["FORMULARY_ID"], "part_d_deductible": cms._money(pi["DEDUCTIBLE"]),
            "tiers": tiers.get("tiers", []), "insulin": tiers.get("insulin_cost_sharing", []),
            "contract": contract, "pbp": pbp, "_conn": conn}


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


def _yes(v) -> bool:
    return str(v).strip().upper() in ("Y", "1", "TRUE")


def _product_terms(conn, ctx: dict, d: dict) -> dict:
    """Coverage terms of ONE product on one plan (formulary row, else supplemental excluded-drug row)."""
    row = conn.execute("select * from formulary where FORMULARY_ID=? and RXCUI=? limit 1",
                       (ctx["formulary_id"], d["rxcui"])).fetchone()
    if row:
        return {"rxcui": d["rxcui"], "product": d["name"], "on_formulary": True, "basis": "formulary",
                "tier": int(row["TIER_LEVEL_VALUE"]), "prior_authorization": _yes(row["PRIOR_AUTHORIZATION_YN"]),
                "step_therapy": _yes(row["STEP_THERAPY_YN"]),
                "quantity_limit": (f"{row['QUANTITY_LIMIT_AMOUNT']} per {row['QUANTITY_LIMIT_DAYS']} days"
                                   if _yes(row["QUANTITY_LIMIT_YN"]) else None)}
    ex = conn.execute("select * from excluded where CONTRACT_ID=? and PLAN_ID=? and RXCUI=? limit 1",
                      (ctx["contract"], ctx["pbp"], d["rxcui"])).fetchone()
    if ex:
        keys = ex.keys()
        g = lambda k: ex[k] if k in keys else ""  # noqa: E731
        return {"rxcui": d["rxcui"], "product": d["name"], "on_formulary": False, "basis": "supplemental_excluded_drug",
                "tier": int(g("TIER")) if str(g("TIER")).isdigit() else g("TIER") or None,
                "prior_authorization": _yes(g("PRIOR_AUTH_YN")), "step_therapy": _yes(g("STEP_THERAPY_YN")),
                "quantity_limit": (f"{g('QUANTITY_LIMIT_AMOUNT')} per {g('QUANTITY_LIMIT_DAYS')} days"
                                   if _yes(g("QUANTITY_LIMIT_YN")) else None),
                "capped_benefit": _yes(g("CAPPED_BENEFIT_YN"))}
    return {"rxcui": d["rxcui"], "product": d["name"], "on_formulary": False, "basis": "not_listed"}


def coverage_for_plan(ctx: dict, products: list[dict], counterparts: list[dict]) -> dict:
    """Coverage of the matched product(s) on one plan. Terms are kept per product; a single answer is given
    only when every matched product has identical terms (including formulary vs supplemental basis)."""
    conn = ctx.get("_conn") or cms.puf_db(ctx["year"])
    per = [_product_terms(conn, ctx, d) for d in products]
    sig = {tuple(sorted((k, str(v)) for k, v in x.items() if k not in ("rxcui", "product"))) for x in per}
    out = {"plan_id": ctx["plan_id"], "plan_name": ctx["plan_name"], "year": ctx["year"],
           "formulary_id": ctx["formulary_id"], "products": per}
    if ctx.get("cms_file"):
        out["cms_file"] = ctx["cms_file"]
    if len(sig) > 1:
        out["coverage"] = "varies_by_product_needs_clarification"
        out["detail"] = "The matching products have different coverage terms on this plan; confirm the exact product."
        return out
    first = per[0]
    if first["basis"] == "formulary":
        out.update(coverage="covered", tier=first["tier"], prior_authorization=first["prior_authorization"],
                   step_therapy=first["step_therapy"], quantity_limit=first["quantity_limit"],
                   cost_sharing=_cost_for(ctx, first["tier"], "insulin" in first["product"].lower()))
        return out
    if first["basis"] == "supplemental_excluded_drug":
        out.update(coverage="covered_as_supplemental_excluded_drug", tier=first["tier"],
                   prior_authorization=first["prior_authorization"], step_therapy=first["step_therapy"],
                   quantity_limit=first["quantity_limit"], capped_benefit=first["capped_benefit"],
                   detail=("Listed in the plan's supplemental (normally excluded) drug file, not the Part D formulary. "
                           "Cost sharing for supplemental drugs is not in the CMS tier file; confirm with the plan."))
        return out
    # Not listed: say whether the brand/generic counterpart is listed instead (never substituted).
    listed_counterparts = []
    for d in counterparts:
        t = _product_terms(conn, ctx, d)
        if t["basis"] != "not_listed":
            listed_counterparts.append({"rxcui": d["rxcui"], "product": d["name"], "tier": t["tier"],
                                        "basis": t["basis"]})
    if listed_counterparts:
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
def _medcare_site(parsed: dict, products: list[dict], nav_cache: dict | None = None) -> dict:
    """Corroborate against MedCare's live Formulary Navigator and formulary PDF snapshot (best effort).
    The live-site cache lives only for one call, so results are never older than the call itself."""
    nav_cache = {} if nav_cache is None else nav_cache
    ing = (parsed["ingredients"] or [parsed["name"]])[0]
    query = parsed["name"] if len(parsed["name"]) >= 3 else ing
    key = fold(query)
    if key not in nav_cache:
        try:
            _guard_upstream(query)
            nav_cache[key] = lookups.navigator_search(query[:50], max_details=0)
        except (net.FetchError, ValueError) as err:
            nav_cache[key] = {"status": "unavailable", "error": str(err)}
    nav = nav_cache[key]
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
            entries.append({"original": "[item is not an object/string]", "line": None, "text": "",
                            "invalid": "item is not an object/string"})
            continue
        fields = {k: m.get(k) for k in ("name", "strength", "form", "release", "original")}
        if any(v is not None and not isinstance(v, (str, int, float)) for v in fields.values()):
            entries.append({"original": "[invalid item]", "line": None, "text": "",
                            "invalid": "item fields must be strings"})
            continue
        # identifier protection applies to every field of every structured item (same rule as pasted text)
        if any(looks_like_identifier(str(v)) for v in fields.values() if v not in (None, "")):
            entries.append({"original": _REDACTED, "line": None, "text": "",
                            "invalid": "possible personal identifier; this item was not sent to any service"})
            ignored.append({"item": i + 1, "reason": "possible_personal_identifier_redacted", "text": _REDACTED})
            continue
        bits = [str(fields[k] or "") for k in ("name", "strength", "form", "release")]
        txt = " ".join(b for b in bits if b).strip()
        entries.append({"original": str(fields["original"] or txt), "line": None, "text": txt, "structured": m,
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
                parsed["alias_in_parentheses"] = from_name["alias_in_parentheses"]
                parsed["ingredients"] = _ingredients(parsed["name"])
                parsed["strengths"] = from_name["strengths"]
                parsed["unresolved_numbers"] = from_name["unresolved_numbers"]
                if s.get("strength") not in (None, ""):
                    st = str(s["strength"])
                    parsed["strengths"] = parse_strengths(st)
                    rest = parse_text(_STRENGTH.sub(" ", parse_text(protect_numeric_commas(st))))
                    bare = [b.group(1).strip() for b in _BARE_NUMS.finditer(rest)]
                    parsed["unresolved_numbers"] = (from_name["unresolved_numbers"] or []) + bare or None
                    if not parsed["strengths"] and not bare:
                        raise ValueError("unreadable strength")
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
        k = (fold(p["name"]), fold(p.get("alias_in_parentheses") or ""), tuple(fold(i) for i in p["ingredients"]),
             tuple((s["value"], s["unit"], s["group"]) for s in p["strengths"]),
             tuple(p.get("unresolved_numbers") or ()), p["form"], p["release"])
        if k in keyed:
            it["duplicate_of"] = keyed[k]["index"]
        else:
            keyed[k] = it

    # 4. Plan contexts (once per plan).
    # One CMS file per call: every plan context, formulary/cost lookup and the reported provenance come from
    # the same open file, so a refresh between calls (or during one) can never mix old data and new provenance.
    plan_ctx, plan_errors = {}, {}
    cms_meta = None
    try:
        conn = cms.puf_db(year)
        cms_meta = cms.puf_source(year, conn)
    except (net.FetchError, cms.CMSDataError, ValueError) as err:
        conn = None
        for pid in plan_ids:
            plan_errors[pid] = f"CMS Part D file unavailable: {err}"
    if conn is not None:
        file_ref = {k: (cms_meta or {}).get(k) for k in ("dataset", "file_date", "source_url")}
        for pid in plan_ids:
            try:
                plan_ctx[pid] = _plan_context(conn, year, pid)
                plan_ctx[pid]["cms_file"] = file_ref
            except (ValueError, net.FetchError, cms.CMSDataError) as err:
                plan_errors[pid] = str(err)
    nav_cache: dict = {}

    # 5. Lookups with bounded concurrency and an overall time budget.
    medcare_plans = [pid for pid in plan_ids if pid.startswith("H5767")]

    def lookup(it):
        res = candidate_products(it["parsed"])
        site = (_medcare_site(it["parsed"], res["products"], nav_cache)
                if (include_medcare_site and medcare_plans) else None)
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
        "plans": [{k: v for k, v in c.items() if k in ("plan_id", "plan_name", "year", "formulary_id", "part_d_deductible",
                                                     "cms_file")}
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
        "sources": _sources(year, cms_meta),
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
    it["match"] = {"rxnav_queries": res["queries"], "brand_intent": res["brand_intent"], "brand": res["brand"],
                   "strength_association": res.get("strength_association")}
    if not res["recognized"]:
        it.update(status="unrecognized", detail="Not found in RxNorm under this name.",
                  clarification={"reason": "unrecognized_name", "spelling_suggestions": _suggest(p["name"])[:5]})
        return
    if res.get("alias_unresolved"):
        au = res["alias_unresolved"]
        it.update(status="needs_clarification",
                  clarification={"reason": au["reason"], "parenthetical_name": au["written"],
                                 "options_in_rxnorm": res.get("alias_options") or []},
                  detail=(f"The entry names '{au['written']}' in parentheses, which could not be confirmed as a brand "
                          "or ingredient of this product in RxNorm. The written name was kept; the generic was NOT "
                          "selected. Confirm the exact product."))
        return
    if p.get("unresolved_numbers") and res["available_after_ingredient"]:
        opts = sorted({d["name"] for d in res.get("unresolved_options") or [] if not d["is_pack"]})[:25]
        has_units = bool(p["strengths"])
        it.update(status="needs_clarification",
                  clarification={"reason": ("numbers_not_explained_by_written_strength" if has_units
                                            else "strength_unit_not_stated"),
                                 "unresolved_numbers": p["unresolved_numbers"],
                                 "options_in_rxnorm": opts or sorted({d["name"] for d in res["available_after_ingredient"]
                                                                      if not d["is_pack"]})[:25]},
                  detail=("The entry contains number(s) without a unit (" + ", ".join(p["unresolved_numbers"]) +
                          "). They were kept as a constraint, not ignored; confirm the exact strength and unit. "
                          "Nothing was substituted."))
        return
    if not prods:
        avail = res["available_after_ingredient"]
        options = sorted({d["name"] for d in avail if not d["is_pack"]})[:25]
        if not avail:
            reason = "no_product_with_this_exact_ingredient_combination"
        elif p["strengths"] and not any(strength_match(p, d)[0] for d in avail):
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
    if res.get("strength_association") == "not_stated":
        it["strength_note"] = ("Combination strength written without naming which value belongs to which ingredient; "
                               "matched by value set. Every product with the same values is listed.")
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


def _sources(year: int, cms_meta: dict | None) -> dict:
    out = {"rxnorm": {"api": "NLM RxNav drugs.json", "url": f"{cms.RXNAV}/drugs.json", "retrieved_at": net.now_iso()}}
    if cms_meta:
        out["cms_part_d_file"] = {k: cms_meta.get(k) for k in ("dataset", "source_url", "file_date", "retrieved_at",
                                                               "last_checked_at", "record_layout")}
    else:
        out["cms_part_d_file"] = {"status": "unavailable"}
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
