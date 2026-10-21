"""Crawl https://mcpartnerstx.com/ and normalise every public resource/link.

Discovery uses the site's public WordPress REST API (all published pages, posts and
the media library), then the rendered HTML of every page for links, headings and
text. Every link occurrence keeps its source page, nearest heading, plan context and
label. Documents are downloaded, hashed and identity-checked (plan IDs, year,
document code, language, sample/lorem-ipsum detection). Placeholders ('#', empty
hrefs, sample files, template text) are recorded explicitly as unavailable.
"""
from __future__ import annotations

import hashlib
import io
import json
import re
import zipfile
from collections import defaultdict
from urllib.parse import urljoin, urlsplit, urlunsplit

from . import net
from .htmlparse import extract

SITE = "https://mcpartnerstx.com/"
WP = SITE + "wp-json/wp/v2/"
PROVIDER_API = SITE + "wp-json/provider-search/v1/search"
PLAN_NAMES = {"classic": "H5767-001", "focus": "H5767-002", "choice": "H5767-003"}
DOC_TYPES = (".pdf", ".docx", ".doc", ".zip", ".xlsx", ".xls")
INTERACTIVE = [
    {"name": "Provider Directory Search", "page": "https://mcpartnerstx.com/provider-directory-search/",
     "endpoint": PROVIDER_API, "automation": "automated",
     "tool": "medcare_provider_search",
     "notes": "Public WordPress endpoint used by the site's own search form. Requires a provider type."},
    {"name": "Formulary (Drug List) Search", "page": "https://client.formularynavigator.com/Search.aspx?siteCode=4117394336",
     "automation": "automated", "tool": "medcare_drug_lookup",
     "notes": "Formulary Navigator public search pages (drug name -> tier and restriction icons)."},
    {"name": "Real-Time Pharmacy Tool (drug price estimate)", "page": "https://openenrollment.medimpact.com/#/web/rps/drug-price",
     "automation": "browser_required",
     "notes": "Single-page app hosted by MedImpact; not automated. Prices depend on pharmacy, quantity and coverage stage."},
    {"name": "Pharmacy Directory Search page", "page": "https://mcpartnerstx.com/pharmacy-directory-search/",
     "automation": "document_search", "tool": "medcare_pharmacy_search",
     "notes": "The page's 'Find a Pharmacy' button links to a sample PDF; the downloadable Pharmacy Directory PDF is indexed instead."},
    {"name": "Plan Finder questionnaire", "page": "https://mcpartnerstx.com/plan-finder/",
     "automation": "criteria_tool", "tool": "plan_eligibility_criteria",
     "notes": "Client-side questionnaire (Part A & B, qualifying chronic condition, Harris County, Medicaid). Its "
              "public criteria are reproduced from CMS and plan documents; answers are never submitted."},
    {"name": "Online Health Risk Assessment form", "page": "https://mcpartnerstx.com/health-risk-assessment-online-form/",
     "automation": "not_automated_by_design",
     "notes": "Collects member health information. This plugin never fills in or submits it."},
    {"name": "Contact Us message form", "page": "https://mcpartnerstx.com/contact-us/",
     "automation": "not_automated_by_design", "notes": "Sends a message to the plan; never submitted by this plugin."},
    {"name": "Provider Portal", "page": "https://mcpartnerstx.com/provider-resources/",
     "automation": "unavailable", "notes": "'Log In to Provider Portal' has no destination (href '#') on the live site."},
    {"name": "Online enrollment", "page": "https://mcpartnerstx.com/enroll/",
     "automation": "unavailable", "notes": "'Enroll Online' has no destination (href '#'); phone, mail and fax options are listed."},
]

SAMPLE_MARKERS = ("lorem ipsum", "file-sample", "sample_150kb")


def _get_json(url):
    return net.fetch(url, hosts=net.MEDCARE_HOSTS, accept="application/json", max_bytes=20_000_000)


def wp_collection(kind: str) -> list[dict]:
    items, page = [], 1
    while True:
        resp = _get_json(f"{WP}{kind}?per_page=100&page={page}")
        items.extend(resp.json())
        total = int(resp.headers.get("x-wp-totalpages", "1") or 1)
        if page >= total:
            return items
        page += 1


def norm_url(href: str, base: str) -> str:
    url = urljoin(base, href.strip())
    parts = urlsplit(url)
    if parts.scheme in ("http", "https"):
        host = (parts.hostname or "").lower()
        path = parts.path or "/"
        return urlunsplit((parts.scheme, host, path, parts.query, parts.fragment))
    return url


def plan_ids_from(text: str) -> list[str]:
    ids = set()
    for p in re.findall(r"\bH5767-(\d{3})\b", text or "", re.I):
        ids.add(f"H5767-{p}")
    for p in re.findall(r"\bH5767_(?:SB|EOC|ANOC)(\d{3})20\d\d", text or "", re.I):
        ids.add(f"H5767-{p}")
    for name, pid in PLAN_NAMES.items():
        if re.search(rf"\bMedCare\s+{name}\b", text or "", re.I):
            ids.add(pid)
    return sorted(ids)


SITE_PAGE_CATEGORIES = {
    "": "home", "who-we-are": "about", "forms": "forms", "plan-resources": "member_resources",
    "prescription-drug-coverage": "formulary", "provider-directories": "provider_directory",
    "benefits": "benefits", "agent-resources": "agent_resources", "provider-resources": "provider_resources",
    "plan-documents": "plan_documents", "enroll": "enrollment", "plan-finder": "plan_comparison",
    "plan-results": "plan_comparison", "contact-us": "contact", "clinical-criteria": "clinical_criteria",
    "provider-directory-search": "provider_directory", "pharmacy-directory-search": "pharmacy_directory",
    "health-risk-assessment-online-form": "health_risk_assessment", "terms-of-use": "legal_notice",
    "privacy-policy-2": "privacy_phi", "notice-of-availability": "legal_notice",
    "non-discrimination-notice": "legal_notice", "accessibility-statement": "legal_notice",
    "hello-world": "not_a_resource",
}


def classify(url: str, label: str, section: str, page: str) -> tuple[str, list[str]]:
    hay = " ".join([url, label, section]).lower()
    page = page.lower()
    parts = urlsplit(url or "")
    if (parts.hostname or "") in net.MEDCARE_HOSTS and not parts.path.lower().endswith(DOC_TYPES):
        slug = parts.path.strip("/").split("/")[-1] if parts.path.strip("/") else ""
        if slug in SITE_PAGE_CATEGORIES:
            cat = SITE_PAGE_CATEGORIES[slug]
            aud = ["agent"] if slug == "agent-resources" else ["provider"] if slug in (
                "provider-resources", "clinical-criteria") else ["member"]
            return cat, aud
    rules = [
        ("summary_of_benefits", r"summary of benefits|_sb0\d\d|resumen de beneficios"),
        ("evidence_of_coverage", r"evidence of coverage|_eoc0\d\d"),
        ("formulary", r"formulary|drug list|formularynavigator|search drug|covered drug"),
        ("drug_pricing_tool", r"medimpact|real-time pharmacy"),
        ("drug_utilization_criteria", r"prior[- ]authorization[- ]criteria|step[- ]therapy|transition[- ]policy"),
        ("medication_therapy_management", r"mtm|medication therapy|medication[- ]list|medicamentos"),
        ("prescription_payment_plan", r"prescription payment plan|m3p|mp3"),
        ("pharmacy_directory", r"pharmacy[- ]directory|find a pharmacy|pharmacy-directory|search for pharmacies"),
        ("provider_directory", r"provider[- ]directory|find a provider|search for providers|provider-directory"),
        ("dental", r"dental|deltadental"),
        ("vision", r"vision|vsp\.com|eye"),
        ("hearing", r"hearing"),
        ("chiropractic_acupuncture", r"ashlink|chiropract|acupunct"),
        ("fitness", r"fitness|silverandfit"),
        ("transportation", r"transportation"),
        ("otc_healthy_food", r"\botc\b|over-the-counter|healthy food|enhanced care"),
        ("enrollment", r"enroll|scope of appointment|soa"),
        ("health_risk_assessment", r"health risk assessment|\bhra\b"),
        ("appeals_grievances", r"grievance|appeal|redetermination|complaint|appointment of representative|aor"),
        ("coverage_determination", r"coverage determination|exception request|drug-claim|drug claim|reimbursement|dmr"),
        ("clinical_criteria", r"clinical criteria|coverage determinations \(|ncd|lcd|milliman|mcg\.com|cms014961"),
        ("snp_eligibility", r"ssbci|chronically ill|otcssbcielig|provider verification"),
        ("privacy_phi", r"privacy|phi\b"),
        ("legal_notice", r"non-discrimination|notice of availability|accessibility|terms of use|_noa_"),
        ("plan_comparison", r"plan finder|comparison|plan-finder|plan summaries|benefits at-a-glance|benefit_highlights"),
        ("agent_marketing", r"brochure|flyer|digital ads|email templates|marketing"),
        ("provider_portal", r"provider portal"),
        ("contact", r"contact|tel:|mailto:|member services"),
        ("plan_documents", r"plan documents|enrollment book|plan information"),
    ]
    category = "other"
    for cat, rx in rules:
        if re.search(rx, hay):
            category = cat
            break
    audience = set()
    if "agent-resources" in page or category in ("agent_marketing",) or re.search(r"scope of appointment|soa", hay):
        audience.add("agent")
    if "provider-resources" in page or "clinical-criteria" in page or category in ("provider_portal",):
        audience.add("provider")
    if not audience or category in ("summary_of_benefits", "evidence_of_coverage", "formulary"):
        audience.add("member")
    return category, sorted(audience)


def language_of(label: str, url: str, text: str = "") -> str | None:
    s = f"{label} {url}".lower()
    if re.search(r"español|espanol|spanish|sp_m\.pdf|sp\.pdf|lista de medicamentos", s):
        return "es"
    if re.search(r"\benglish\b|_eng\b|-eng\.", s):
        return "en"
    if text:
        t = text[:4000].lower()
        es = len(re.findall(r"\b(beneficios|usted|cobertura|medicamentos|para)\b", t))
        en = len(re.findall(r"\b(benefits|you|coverage|drugs|the)\b", t))
        if es + en > 10:
            return "es" if es > en else "en"
    return None


def pdf_pages(body: bytes) -> list[str]:
    from pypdf import PdfReader  # vendored
    reader = PdfReader(io.BytesIO(body))
    if reader.is_encrypted:
        try:
            reader.decrypt("")
        except Exception:
            raise ValueError("encrypted PDF")
    pages = []
    for page in reader.pages:
        try:
            pages.append(page.extract_text() or "")
        except Exception as err:  # keep page numbering stable
            pages.append(f"[text extraction failed on this page: {type(err).__name__}]")
    return pages


_HIDDEN_PHARMACY = "Pharmacy Directory - Harris County"
_PROVIDER_HEADER = "Provider Directory - Harris County"


def clean_page_text(text: str, url: str) -> tuple[str, bool]:
    """The 2027 Provider Directory PDF carries an invisible copy of a Pharmacy Directory page in
    the text layer of every page (verified by rendering). Remove it so searches only see the
    visible provider listings. Returns (text, cleaned?)."""
    if "provider_directory" not in url.lower() or _HIDDEN_PHARMACY not in text or _PROVIDER_HEADER not in text:
        return text, False
    lines = text.splitlines()
    k = next(i for i, ln in enumerate(lines) if _HIDDEN_PHARMACY in ln)
    j = next(i for i, ln in enumerate(lines) if _PROVIDER_HEADER in ln)
    if k >= j:
        return text, False
    start = j - 1 if j > 0 and not re.search(r"\(\d{3}\)|\d{5}", lines[j - 1]) else j
    return "\n".join(lines[:k] + lines[start:]), True


def docx_text(body: bytes) -> str:
    with zipfile.ZipFile(io.BytesIO(body)) as z:
        xml = z.read("word/document.xml").decode("utf-8", "replace")
    xml = re.sub(r"</w:p>", "\n", xml)
    return re.sub(r"<[^>]+>", "", xml)


def identify(text: str, url: str) -> dict:
    head = re.sub(r"[ \t]+", " ", text[:6000])
    years = sorted(set(re.findall(r"\b(20[2-3]\d)\b", head)))
    doc_code = re.findall(r"\b(H5767_[A-Z0-9]+_[A-Z]{1,2})\b", head)
    title = ""
    for ln in head.splitlines():
        ln = ln.strip()
        if len(ln) > 8 and not re.fullmatch(r"[\W\d_]+", ln):
            title = ln[:160]
            break
    lower = head.lower()
    return {"title_line": title, "plan_ids": plan_ids_from(head), "years_in_first_pages": years,
            "document_codes": sorted(set(doc_code)),
            "is_sample_or_placeholder": any(m in lower for m in SAMPLE_MARKERS) or "file-sample" in url.lower(),
            "language_detected": language_of("", "", text)}


def crawl(progress=print, fetch_documents: bool = True, previous_texts: dict | None = None) -> tuple[dict, dict]:
    """Return (catalog, texts_by_sha256). `previous_texts` (sha256 -> page texts) lets
    unchanged documents reuse already-extracted text."""
    started = net.now_iso()
    pages = wp_collection("pages")
    posts = wp_collection("posts")
    media = wp_collection("media")
    progress(f"WordPress REST: {len(pages)} pages, {len(posts)} posts, {len(media)} media items")
    media_by_url = {m["source_url"]: m for m in media}

    page_records, occurrences, parsed_pages = [], [], []
    for item in pages + posts:
        resp = net.fetch(item["link"], hosts=net.MEDCARE_HOSTS, max_bytes=8_000_000)
        parsed_pages.append((item, resp, extract(resp.text())))
    # Lines repeated on most pages are site template (menus, footer, banners), not page content.
    line_freq: dict[str, int] = defaultdict(int)
    for _, _, parsed in parsed_pages:
        for ln in set(parsed["text_lines"]):
            line_freq[ln] += 1
    template = {ln for ln, n in line_freq.items() if n >= max(3, len(parsed_pages) // 2)}
    url_pages: dict[str, set] = defaultdict(set)
    for item, _, parsed in parsed_pages:
        for link in parsed["links"]:
            url_pages[norm_url(link["href"], item["link"]) if link["href"] else ""].add(item["link"])
    for item, resp, parsed in parsed_pages:
        url = item["link"]
        lines = [ln for ln in parsed["text_lines"] if ln not in template]
        text = "\n".join(lines)
        issues = []
        if item.get("type") == "post" and item.get("slug") == "hello-world":
            issues.append("WordPress default sample post ('Hello world!'); not a plan resource.")
        if re.search(r"\bChampion\b", text):
            issues.append("Contains another organisation's template text (mentions 'Champion' Health Plans "
                          "terms of use); do not treat as MedCare policy.")
        if re.search(r"description that is there now", text, re.I):
            issues.append("Contains unfinished draft copy ('The description that is there now is the description "
                          "for MedCare Focus').")
        if re.search(r"coming soon", text, re.I):
            issues.append("Says 'Coming soon' for at least one feature.")
        if re.search(r"Lorem ipsum", text, re.I):
            issues.append("Contains lorem-ipsum placeholder text.")
        page_records.append({
            "id": "page-" + (item.get("slug") or str(item["id"])), "type": "page", "url": url,
            "title": re.sub(r"<[^>]+>", "", item["title"]["rendered"]).replace("&#8211;", "-").strip(),
            "wp_id": item["id"], "wp_modified": item.get("modified"), "http_sha256": resp.sha256,
            "fetched_at": resp.fetched_at, "text": text[:40000], "issues": issues,
            "forms": [f for f in parsed["forms"] if f["fields"]],
            "select_options": parsed["selects"],
        })
        for link in parsed["links"]:
            target = norm_url(link["href"], url) if link["href"] else ""
            sitewide = len(url_pages.get(target, ())) >= max(3, len(parsed_pages) // 2)
            occurrences.append({**link, "chrome": link["chrome"] or sitewide,
                                "section": "" if (link["chrome"] or sitewide) else link["section"],
                                "plan_context": "" if (link["chrome"] or sitewide) else link["plan_context"],
                                "page": url, "page_title": page_records[-1]["title"]})
    progress(f"Parsed {len(page_records)} pages ({len(template)} site-template lines excluded), "
             f"{len(occurrences)} link occurrences")
    site_template_text = sorted(template)

    resources: dict[str, dict] = {}
    for occ in occurrences:
        href = occ["href"]
        label = occ["text"] or ""
        section = occ["section"] or ""
        ctx = {"page": occ["page"], "page_title": occ["page_title"], "section": section, "label": label,
               "plan_heading": occ["plan_context"], "site_chrome": occ["chrome"]}
        placeholder = None
        if not occ["has_href"] or href == "":
            placeholder = "missing_href"
        elif href == "#" or (href.startswith("#") and href not in ("#content", "#respond")):
            placeholder = "hash_placeholder"
        elif href.lower().startswith("javascript"):
            placeholder = "javascript_placeholder"
        elif "your-link.com" in href:
            placeholder = "template_placeholder_domain"
        if href in ("#content",) or "replytocom" in href or href.endswith("#respond") or "/author/" in href \
                or "#comment-" in href:
            continue  # skip-links, comment widgets and author archives are not resources
        if placeholder:
            if label.lower() == "members" and not section:
                key = "placeholder::nav-members"
            else:
                key = f"placeholder::{occ['page']}::{section}::{label}::{occ['plan_context']}"
            rec = resources.setdefault(key, {"url": None, "href": href, "status": "unavailable_placeholder",
                                             "placeholder_kind": placeholder, "found_on": []})
        else:
            url = norm_url(href, occ["page"])
            if url.startswith("tel:") or url.startswith("mailto:"):
                kind = "phone" if url.startswith("tel:") else "email"
                rec = resources.setdefault(url, {"url": url, "href": href, "status": kind, "found_on": []})
            else:
                key = url.split("#")[0] if urlsplit(url).hostname in net.MEDCARE_HOSTS else url
                rec = resources.setdefault(key, {"url": key, "href": href, "status": "pending", "found_on": []})
        if ctx not in rec["found_on"]:
            rec["found_on"].append(ctx)
        # best (most specific) context first: page content beats site chrome
        rec["found_on"].sort(key=lambda c: (c["site_chrome"], not c["section"]))

    # Media library documents never linked from any page
    linked = {r["url"] for r in resources.values() if r.get("url")}
    for m in media:
        src = m["source_url"]
        if src.lower().endswith(DOC_TYPES) and src not in linked:
            resources[src] = {"url": src, "href": None, "status": "pending", "found_on": [],
                              "unlinked_media": True}

    previous_texts = previous_texts or {}
    texts_by_sha: dict[str, list[str]] = {}
    out = []
    for key, rec in resources.items():
        url = rec.get("url")
        first = rec["found_on"][0] if rec["found_on"] else {}
        label = first.get("label", "")
        section = first.get("section", "")
        page = first.get("page", "")
        category, audience = classify(url or "", label, section, page)
        context_text = " ".join(f"{c['section']} {c['label']} {c['plan_heading'] if c['section'] in ('Summary of Benefits', 'Evidence of Coverage') or c['label'] == 'View Plan Documents' else ''}" for c in rec["found_on"])
        plan_ids = plan_ids_from((url or "") + " " + context_text)
        rid = "r-" + hashlib.sha1(key.encode()).hexdigest()[:10]
        title_bits = [section or first.get("page_title", "")]
        if label and label.lower() not in (section or "").lower():
            title_bits.append(label)
        if first.get("plan_heading") and first.get("plan_heading") not in " ".join(title_bits) and \
                (section in ("Summary of Benefits", "Evidence of Coverage") or label == "View Plan Documents"):
            title_bits.insert(0, first["plan_heading"])
        page_titles = {pr["url"]: pr["title"] for pr in page_records}
        if url in page_titles and (first.get("site_chrome") or not section):
            title_bits = [page_titles[url]]
        elif first.get("site_chrome"):
            title_bits = [label or (url or key)]
        elif not rec["found_on"]:
            title_bits = [(url or key).rsplit("/", 1)[-1]]
        record = {"id": rid, "title": " — ".join(b for b in title_bits if b) or (url or key),
                  "url": url, "category": category, "audience": audience, "plan_ids": plan_ids,
                  "language": language_of(label, url or ""), "status": rec["status"], "issues": [],
                  "found_on": rec["found_on"], "unlinked_media": rec.get("unlinked_media", False)}
        if rec.get("placeholder_kind"):
            record["placeholder_kind"] = rec["placeholder_kind"]
            record["issues"].append("Link has no destination on the live site (" + rec["placeholder_kind"].replace("_", " ")
                                    + "); treat as unavailable.")
        if record["unlinked_media"]:
            record["issues"].append("In the site's media library but not linked from any published page; may be "
                                    "superseded or draft. Do not use as current evidence without confirmation.")
        if url and url.startswith("http"):
            host = urlsplit(url).hostname or ""
            path = urlsplit(url).path.lower()
            is_doc = path.endswith(DOC_TYPES)
            if host in net.MEDCARE_HOSTS and is_doc:
                record["link_type"] = path.rsplit(".", 1)[-1]
                m = media_by_url.get(url)
                if m:
                    record["wp_media"] = {"id": m["id"], "modified": m.get("modified"), "date": m.get("date"),
                                          "mime_type": m.get("mime_type")}
                if fetch_documents and not path.endswith(".zip"):
                    try:
                        resp = net.fetch(url, hosts=net.MEDCARE_HOSTS, max_bytes=60_000_000, accept="*/*", timeout=120)
                        sha = resp.sha256
                        record["document"] = {"sha256": sha, "bytes": len(resp.body), "content_type": resp.content_type,
                                              "http_last_modified": resp.headers.get("last-modified"),
                                              "fetched_at": resp.fetched_at}
                        cached = previous_texts.get(sha)
                        if path.endswith(".pdf"):
                            texts = cached if cached else pdf_pages(resp.body)
                        elif path.endswith(".docx"):
                            texts = cached if cached else [docx_text(resp.body)]
                        else:
                            texts = []
                        cleaned = 0
                        for i, t in enumerate(texts):
                            texts[i], did = clean_page_text(t, url)
                            cleaned += did
                        if cleaned:
                            record["issues"].append(
                                f"{cleaned} pages carry an invisible duplicate of Pharmacy Directory text in the PDF "
                                "text layer (not visible when rendered); removed from this tool's search index.")
                        texts_by_sha[sha] = texts
                        record["document"]["page_count"] = len(texts) if path.endswith(".pdf") else None
                        ident = identify("\n".join(texts[:3]), url)
                        record["document"]["identity"] = ident
                        if not rec["found_on"] and ident["title_line"]:
                            record["title"] = f"{record['title']} — {ident['title_line'][:90]}"
                        if not record["language"]:
                            record["language"] = ident["language_detected"]
                        if ident["is_sample_or_placeholder"]:
                            record["status"] = "unavailable_sample_document"
                            record["issues"].append("Linked file is a generic sample/lorem-ipsum PDF, not a plan document.")
                        else:
                            record["status"] = "available"
                        if ident["plan_ids"] and plan_ids and not set(plan_ids) & set(ident["plan_ids"]):
                            record["status"] = "available_identity_mismatch"
                            record["link_context_plan_ids"] = plan_ids
                            record["issues"].append(
                                f"IDENTITY MISMATCH: the link's label/filename indicates {plan_ids}, but the document "
                                f"itself is for {ident['plan_ids']}. Use it only as a {ident['plan_ids']} document.")
                        if ident["plan_ids"]:
                            record["plan_ids"] = sorted(ident["plan_ids"])
                        if path.endswith(".docx"):
                            record["issues"].append("Editable Word source file (.docx) is linked; prefer the published PDF.")
                    except (net.FetchError, ValueError, KeyError, zipfile.BadZipFile) as err:
                        record["status"] = "unavailable_fetch_failed"
                        record["issues"].append(f"Could not retrieve document: {err}")
                elif path.endswith(".zip"):
                    record["status"] = "available_not_indexed"
                    record["issues"].append("ZIP archive; not downloaded or indexed by this tool.")
                else:
                    record["status"] = "not_checked"
            elif host in net.MEDCARE_HOSTS:
                record["link_type"] = "page"
                record["status"] = "available_site_page"
            else:
                record["link_type"] = "external"
                if url.startswith("http://"):
                    secure = "https://" + url[len("http://"):]
                    chk = net.check_link(secure)
                    chk["checked_https_variant_of"] = url
                    record["issues"].append("The site links with insecure http://; this tool checked the https:// "
                                            "equivalent" + (" (reachable)." if chk.get("ok") else " (not reachable)."))
                else:
                    chk = net.check_link(url)
                record["link_check"] = chk
                record["status"] = "external_reachable" if chk.get("ok") else "external_unreachable"
                if host in ("www.facebook.com", "www.instagram.com", "www.linkedin.com", "x.com") and \
                        urlsplit(url).path in ("", "/"):
                    record["status"] = "unavailable_generic_social_link"
                    record["issues"].append("Social icon links to the network's home page, not a MedCare profile.")
                if host == "wordpress.org" or host == "gravatar.com":
                    record["status"] = "not_a_resource"
        out.append(record)
        progress(f"  {record['status']:<32} {record['title'][:70]}")

    # Phone numbers / contacts with provenance
    contacts = []
    for pr in page_records:
        for ln in pr["text"].splitlines():
            for m in re.finditer(r"(?:1[-.\s]?)?\(?\d{3}\)?[-.\s]?(?:[A-Z]{3,7}[-.\s]?[A-Z0-9]{0,4}|\d{3}[-.\s]?\d{4})", ln):
                num = m.group(0).strip()
                if sum(ch.isdigit() for ch in num) >= 10 or re.search(r"[A-Z]{3}", num):
                    contacts.append({"number": num, "line": ln[:200], "page": pr["url"]})
    uniq = []
    for c in contacts:
        if c not in uniq:
            uniq.append(c)

    catalog = {"site": SITE, "generated_at": net.now_iso(), "crawl_started_at": started,
               "method": "WordPress REST API (pages, posts, media) + rendered HTML of every published page",
               "counts": {"pages": len(page_records), "resources": len(out),
                          "unavailable": sum(1 for r in out if r["status"].startswith("unavailable"))},
               "pages": page_records, "resources": out, "contacts": uniq,
               "site_template_text": site_template_text}
    return catalog, texts_by_sha
