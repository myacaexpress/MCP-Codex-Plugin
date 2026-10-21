# MedCare Resources — Codex plugin

A standalone Codex plugin for teams who work with **MedCare Partners Health Plan of Texas** (https://mcpartnerstx.com, contract **H5767**) and who research Medicare plans. It bundles an MCP server with 27 tools and two skills.

**Version 1.1.1** fixes all release blockers from the independent review of 1.0.0 and the retest of 1.1.0 (see `CHANGELOG.md`).

> Independent team tool. It is not affiliated with MedCare Partners, CMS or Medicare.gov. It uses **public information only**: no credentials, no member records, no enrollment or eligibility systems (no MARx), no Wayfinder dependency.

## What it does

| Area | Tools | Source (live or snapshot) |
|---|---|---|
| Every public resource and link on mcpartnerstx.com, including placeholders and site defects | `medcare_search_resources`, `medcare_list_resources`, `medcare_get_resource`, `medcare_site_audit`, `medcare_contacts` | Snapshot from a crawl of the WordPress REST API and every page (24 pages, 143 resources) |
| Full-text search of official PDFs (SB, EOC, formulary, PA/ST criteria, directories, forms, notices), with page citations | `medcare_search_documents`, `medcare_read_document_page` | Snapshot of 29 PDFs (1,713 pages) |
| Checking freshness | `medcare_verify_resource` (SHA-256 compared live), `medcare_refresh_catalog` (full re-crawl into the local cache) | Live |
| Medications | `medcare_drug_lookup`: tier, PA/ST/QL and restriction flags, quantity-limit amounts from the formulary PDF, tier conflicts, cost sharing per plan by tier | **Live** Formulary Navigator, plus the PDF snapshot and the CMS Part D file |
| Providers | `medcare_provider_search`: NPI, per-plan participation, accepting-new-patients status, NUCC specialty names, locations | **Live** MedCare provider-search API |
| Pharmacies | `medcare_pharmacy_search` | Pharmacy Directory PDF, parsed (637 entries) |
| Dental, vision, hearing, chiropractic, acupuncture, fitness, OTC, transportation, food | `medcare_benefit_directory`: vendor links with status, plus SB lines per plan | Snapshot, with link checks |
| MedCare plans compared with CMS | `medcare_plan_overview` | CMS Landscape |
| Plans in any county (competitors) | `cms_find_plans`, `zip_to_county` | **Live** CMS CY Landscape and Census ZCTA file (cached) |
| Exact plan identity and availability | `cms_plan_details`: segments, service area, Part D formulary, county check, Medicare.gov hand-off link, how to find the SB | CMS |
| Drug coverage on any plan | `cms_drug_coverage`, `cms_tier_cost_sharing` | **Live** CMS Part D formulary PUF and NLM RxNav |
| Official carrier Summary of Benefits / EOC | `plan_document_discover`, `plan_document_retrieve` (checks the year, contract-plan and segment printed in the PDF, then cross-checks MOOP, premium and deductible against CMS), `plan_document_search` | Live carrier PDFs |
| Comparison | `compare_plans`: CMS figures, cited benefit lines and drug coverage | CMS and verified documents |
| Public eligibility criteria | `plan_eligibility_criteria`: Part A/B, service area, SNP conditions; returns *appears met / not met / unknown* for facts the user stated, **never verified** | CMS and the EOC |
| Provider identity | `npi_lookup` | Live NPPES |
| Data freshness | `source_status`, `cms_refresh_data` (re-check CMS now; caches also auto-re-check after 7 days) | — |

Every result includes provenance: source URL, retrieval or snapshot time, file version and SHA-256 hash.

### Access modes (what is automated and what is not)
Every catalog resource and hand-off carries an `access_mode`:

| access_mode | Meaning |
|---|---|
| `automated_live[:tool]` | The tool fetched the source during the call (provider API, Formulary Navigator, CMS, NPPES, RxNav, verified carrier PDFs). |
| `snapshot_document` / `site_page` | Bundled MedCare snapshot; check with `medcare_verify_resource`. |
| `browser_handoff` | **Not retrieved or read by the tool.** A link for a person (or Codex's own browser) to open: Medicare.gov Plan Compare, Delta Dental, VSP, ASH, Silver&Fit, MedImpact pricing, other external pages. Hand-offs carry `retrieved_by_tool: false` and `evidence: false`. |
| `unavailable` | `#` placeholders, sample PDFs, generic links. Not evidence. |

### Year, plan and segment rules (1.1.0)
- Plan documents are evidence only for the **exact coverage year and contract-plan-segment** proven by the PDF text. A 2027 document is never returned for a 2026 request.
- If a plan has several segments, the segment must be given. A PDF covering several segments is not auto-verified; its columns need manual review.
- If the title area names more than one coverage year (in any word order, English or Spanish, including document codes and coverage periods), the document is `unverified` (ambiguous).
- Eligibility criteria and comparisons never use an arbitrary segment. The segment comes from the request, from a single-segment plan, or from the one segment whose service area covers the supplied location (this choice is disclosed). Otherwise the result is `segment_required`.
- ZIPs that span counties inside and outside a service area return `unknown_zip_spans_covered_and_uncovered_counties`. `cms_find_plans` asks for the county.
- Provider plan, specialty and accepting-new-patients filters apply to the **same** plan entry. Records with unknown plan membership are excluded (and counted) when a plan or accepting filter is used.
- PDP criteria use Part A **or** Part B; MA/SNP criteria need Part A **and** Part B.

### Known problems on the live MedCare site (snapshot of 2026-10-21; run `medcare_site_audit` for current data)
- **Identity mismatch.** The "MedCare Choice — Summary of Benefits — Español" link (`H5767_SB0032027SP_M.pdf`) contains the **MedCare Classic (H5767-001)** document.
- `/plan-results/` lists plans that are not in the CMS CY2027 Landscape: "MedCare Value/Balance/Core (HMO)" and H5767-004. It also contains draft copy, and those plans' "View Plan Details" buttons open a lorem-ipsum sample PDF.
- `/plan-resources/` and `/privacy-policy-2/` contain Champion Health Plans template text.
- 39 links have no destination (`#` or empty). These include Spanish EOCs, the OTC catalog, vision, hearing and healthy-food documents, Grievance/Appeal, Prior Authorization and DMR forms, the SSBCI and PHI-consent forms, online enrollment, and the provider portal.
- The Spanish Enrollment Booklet and the "Find a Pharmacy" button both link to a sample PDF.
- Every page of the 2027 Provider Directory PDF has an invisible copy of Pharmacy Directory text in its text layer. The tool removes it before indexing.
- The site publishes several different phone numbers. `medcare_contacts` shows where each one appears.

## Install (teammates)

### Platform support
**macOS and Linux only.** The Codex MCP launcher (`bin/medcare-mcp`) is a POSIX shell script. **Windows is not supported** in this release: `install.py` refuses to run on Windows, and the plugin has not been tested there.

Requirements: the Codex desktop app or the Codex CLI, plus **Python 3.9 or newer** (python.org, `brew install python`, or the Xcode Command Line Tools). If you don't have Python but do have [uv](https://docs.astral.sh/uv/), the launcher uses uv instead. There are no pip installs.

### Option A — from GitHub (recommended; easy to update)
```bash
codex plugin marketplace add myacaexpress/MCP-Codex-Plugin            # or add: --ref <reviewed commit sha>
codex plugin add medcare-resources@medcare-team-tools
```
To install from the desktop app instead, run the first command, restart the app, open **Plugins**, choose the **MedCare team tools** source and install **MedCare Resources**. Then open a **new** chat and ask: *"Use MedCare Resources source_status."*

To update later: `codex plugin marketplace upgrade medcare-team-tools`, then reinstall or restart.

### Option B — offline ZIP
1. Unzip `medcare-resources-1.0.0.zip`.
2. `cd medcare-resources-1.0.0 && python3 install.py`. This copies the plugin to `~/.codex/plugins/medcare-resources`, adds an entry to `~/.agents/plugins/marketplace.json` and checks that the server lists its tools.
3. `codex plugin add medcare-resources@personal-plugins`, or restart the desktop app and install it from the **Personal plugins** source.

Optional: `python3 install.py --prefetch` downloads the CMS 2027 data (about 25 MB) ahead of time so the first query is fast. To remove the plugin: `python3 install.py --uninstall`, or `codex plugin remove medcare-resources@<marketplace>`.

Re-running `python3 install.py --replace` is transactional. The new copy is checked first. If registering it fails, both the old plugin folder and the original `marketplace.json` are restored. One backup is kept at `~/.codex/plugins/medcare-resources.previous`.

**Pin a release:** `main` moves. To pin the reviewed commit, pass its SHA, which is listed in the release's FIX_REPORT.md: `codex plugin marketplace add myacaexpress/MCP-Codex-Plugin --ref <commit-sha>`. This was verified with Codex 0.163.

### First-use notes
- CMS caches are re-checked against CMS automatically when older than 7 days (set `MEDCARE_CMS_MAX_AGE_DAYS`) or on demand with `cms_refresh_data`. They are re-downloaded only when CMS publishes a new or revised file. If CMS can't be reached, the cached copy is used and `freshness.last_refresh_error` says so.
- CMS data is downloaded on first use and cached in `~/.cache/medcare-resources` (Landscape about 13 MB, Part D members about 8 MB, Census about 7 MB). Set `MEDCARE_CACHE_DIR` to change the location.
- The tools are read-only, apart from writing to that cache.

## Privacy and safety
- Tool inputs are plan IDs, county or ZIP, drug names and provider names. Never enter names, DOB, MBI, SSN, member IDs or clinical notes. The skills tell Codex the same.
- Network access is limited to HTTPS on allowlisted public hosts. Every redirect is re-checked, response sizes are capped, nothing is sent except the query, and no cookies or auth are used.
- PDF and web text is treated as untrusted evidence, never as instructions.
- Public plan criteria are **not** beneficiary eligibility.

## Limitations
- **Medicare.gov Plan Compare is not scraped** (it is interactive and bot-protected). Tools return a deep link for a person to open. The deep-link format has not been confirmed to open plan details; if it doesn't, start at Plan Compare.
- **Competitor SB/EOC URLs** have to be found with web search or a browser. `plan_document_discover` can scan an official carrier page (verified on Devoted's county page), but carrier sites differ.
- **Vendor directories** (Delta Dental, VSP, ASH, Silver&Fit) and the **MedImpact price tool** are listed and link-checked but not searched.
- **CMS PBP medical-benefit files** are not decoded. Medical cost sharing comes from the text of verified SBs and EOCs.
- The MedCare document index is a snapshot. Use `medcare_verify_resource` or `medcare_refresh_catalog` to check it against the live site.
- Search uses keywords. Tables in PDFs can lose column alignment, so open the cited page to confirm figures.

## Maintainers
```bash
python3 scripts/build_data.py     # re-crawl site, re-index PDFs, CMS cross-check, NUCC table
pip install -r tests/requirements-test.txt   # pytest<9, mcp>=1.20,<2 (test-only; the plugin itself has no deps)
python3 -m pytest -q tests                   # offline + regression tests
MEDCARE_LIVE_TESTS=1 python3 -m pytest -q tests/test_live.py   # live public sources, synthetic queries
python3 package.py                # dist/medcare-resources-<version>.zip
```
Repository layout: `plugins/medcare-resources/` is the plugin (`.codex-plugin/plugin.json`, `.mcp.json`, `bin/medcare-mcp`, `server.py`, `medcare/`, `vendor/`, `data/`, `skills/`), and `.agents/plugins/marketplace.json` is the Git marketplace.

Tested on Linux with Codex CLI/app-server 0.163.0 (marketplace add, plugin add, skill loading, MCP server start, `mcpServerStatus/list`, `mcpServer/tool/call`), Python 3.9 to 3.13, and the MCP Python SDK client. A root-level portable `plugin.json` is deliberately left out: with one present, Codex 0.163 ignored the plugin's MCP servers.
