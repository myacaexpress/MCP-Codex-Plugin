#!/usr/bin/env python3
"""Regenerate the bundled public-data snapshot from live official sources.

    python3 scripts/build_data.py            # crawl site, re-index documents, audit against CMS
    python3 scripts/build_data.py --no-cms   # skip the CMS cross-check

Writes data/catalog.json, data/documents.json.gz and data/nucc_taxonomy.json.
Only public information is fetched. No credentials are used.
"""
from __future__ import annotations

import argparse
import csv
import gzip
import hashlib
import io
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent / "plugins" / "medcare-resources"
sys.path.insert(0, str(ROOT))

from medcare import DATA, net, sitecrawl  # noqa: E402
from medcare import catalog as catalog_mod  # noqa: E402

NUCC_PAGE = "https://www.nucc.org/index.php/code-sets-mainmenu-41/provider-taxonomy-mainmenu-40/csv-mainmenu-57"


def load_previous_texts() -> dict:
    path = DATA / "documents.json.gz"
    if not path.exists():
        return {}
    data = json.loads(gzip.decompress(path.read_bytes()))
    return {sha: d["pages"] for sha, d in data.get("documents", {}).items()}


INTERACTIVE = sitecrawl.INTERACTIVE


def nucc() -> dict:
    page = net.fetch(NUCC_PAGE, hosts=frozenset({"www.nucc.org"})).text()
    files = sorted(set(re.findall(r'href="(/images/stories/CSV/nucc_taxonomy_(\d+)\.csv)"', page)), key=lambda x: int(x[1]))
    url = "https://www.nucc.org" + files[-1][0]
    body = net.fetch(url, hosts=frozenset({"www.nucc.org"}), accept="text/csv,*/*").body.decode("utf-8-sig", "replace")
    codes = {}
    for row in csv.DictReader(io.StringIO(body)):
        code = row.get("Code", "").strip()
        if code:
            codes[code] = {"display": (row.get("Display Name") or "").strip(),
                           "classification": (row.get("Classification") or "").strip(),
                           "specialization": (row.get("Specialization") or "").strip(),
                           "grouping": (row.get("Grouping") or "").strip()}
    return {"source_url": url, "version": files[-1][1], "retrieved_at": net.now_iso(),
            "attribution": "Health Care Provider Taxonomy code set, National Uniform Claim Committee (NUCC), nucc.org",
            "codes": codes}


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--no-cms", action="store_true")
    ap.add_argument("--no-nucc", action="store_true")
    args = ap.parse_args()
    previous = load_previous_texts()
    catalog, texts = sitecrawl.crawl(progress=print, previous_texts=previous)
    catalog["interactive_tools"] = INTERACTIVE
    if not args.no_cms:
        catalog["cms_audit"] = catalog_mod.cms_audit(catalog)
    # Formulary snapshot provenance check
    snap = json.loads(gzip.decompress((DATA / "mcp-formulary-2027.json.gz").read_bytes()))
    live = next((r for r in catalog["resources"] if r.get("url") == snap["source_url"]), None)
    catalog["formulary_snapshot"] = {
        "file": "data/mcp-formulary-2027.json.gz", "source_url": snap["source_url"], "sha256": snap["sha256"],
        "entries": len(snap["entries"]), "originally_checked": snap["checked"],
        "matches_live_at_build": bool(live and live.get("document", {}).get("sha256") == snap["sha256"]),
        "build_checked_at": catalog["generated_at"]}
    docs = {}
    for r in catalog["resources"]:
        d = r.get("document")
        if not d or r["status"] != "available" or r.get("unlinked_media") or not (r.get("url") or "").lower().endswith(".pdf"):
            continue
        if d["sha256"] in texts:
            docs[d["sha256"]] = {"resource_id": r["id"], "url": r["url"], "title": r["title"],
                                 "category": r["category"], "plan_ids": r["plan_ids"], "language": r["language"],
                                 "pages": texts[d["sha256"]]}
    (DATA / "documents.json.gz").write_bytes(gzip.compress(json.dumps(
        {"generated_at": catalog["generated_at"], "documents": docs}).encode(), 9, mtime=0))
    catalog["counts"]["indexed_documents"] = len(docs)
    catalog["counts"]["indexed_pages"] = sum(len(d["pages"]) for d in docs.values())
    (DATA / "catalog.json").write_text(json.dumps(catalog, indent=1, ensure_ascii=False) + "\n")
    if not args.no_nucc:
        (DATA / "nucc_taxonomy.json").write_text(json.dumps(nucc(), separators=(",", ":")) + "\n")
    print(json.dumps(catalog["counts"], indent=1))
    by_status = {}
    for r in catalog["resources"]:
        by_status[r["status"]] = by_status.get(r["status"], 0) + 1
    print(json.dumps(by_status, indent=1))


if __name__ == "__main__":
    main()
