# MedCare Resources — Codex plugin

A standalone Codex plugin for teams who work with **MedCare Partners Health Plan of Texas** (https://mcpartnerstx.com, contract **H5767**) and who research Medicare plans. It bundles an MCP server with 28 tools and two skills. The same server can also be hosted for **ChatGPT** (see [ChatGPT](#chatgpt-remote-server) below).

**Version 1.2.4** (1.2.0 plus fixes from its independent reviews) has `medication_list_check`, which checks a whole pasted medication list in one call, and a remote Streamable HTTP + OAuth transport for ChatGPT. See `CHANGELOG.md`.

> Independent team tool. It is not affiliated with MedCare Partners, CMS or Medicare.gov. It uses **public information only**: no credentials, no member records, no enrollment or eligibility systems (no MARx), no Wayfinder dependency.

## What it does

| Area | Tools | Source (live or snapshot) |
|---|---|---|
| Every public resource and link on mcpartnerstx.com, including placeholders and site defects | `medcare_search_resources`, `medcare_list_resources`, `medcare_get_resource`, `medcare_site_audit`, `medcare_contacts` | Snapshot from a crawl of the WordPress REST API and every page (24 pages, 143 resources) |
| Full-text search of official PDFs (SB, EOC, formulary, PA/ST criteria, directories, forms, notices), with page citations | `medcare_search_documents`, `medcare_read_document_page` | Snapshot of 29 PDFs (1,713 pages) |
| Checking freshness | `medcare_verify_resource` (SHA-256 compared live), `medcare_refresh_catalog` (full re-crawl into the local cache) | Live |
| **Whole medication lists** | `medication_list_check`: paste the full list once; every entry comes back in order with the exact product matched, per-plan tier, PA/ST/QL, cost sharing, provenance, summary counts and a full table | **Live** NLM RxNav and the CMS Part D formulary file, plus the MedCare Formulary Navigator |
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

## Medication list check
Paste the whole list once:

> Check this whole medication list against MedCare Classic 2027: *(paste list)*

The tool is `medication_list_check`. Its arguments are `text` (pasted list) and/or `medications` (`[{name, strength, form, release, original}]`), plus optional `plan_ids` (default `H5767-001`, `-002`, `-003`; any Part D plan works, up to 6), `year` (default 2027), `start_at`, `include_medcare_site` and `time_budget_seconds`.

- **Formats:** numbered, bulleted and check-box lists, and `;`, `|`, tab or comma separators. Headers, sig text and start dates are kept apart from the drug. Duplicates are flagged. Combination products (`Losartan-HCTZ 50-12.5`, `hydrocodone/acetaminophen 5/325`) stay as one entry.
- **No silent substitution:** the matched RxNorm product must have the same ingredient set, strengths, form, release type (ER/DR/XL …) and brand or generic.
  - Strengths match on value **and** unit **and** denominator. `100 mg/mL` never matches `100 UNT/ML`, and `1%` matches `10 MG/G`.
  - Combination values bind to the ingredients in the order written.
  - Numbers written without a unit (`losartan/HCTZ 50-12.5`) are kept and returned as `needs_clarification`.
  - Explicit delivery forms (vial, pen, cartridge, nebulizer, metered-dose or dry-powder inhaler) are never interchanged.
  - Numeric commas (`1,000 units`, `0,025 mg`) are read as numbers.
  - A name in parentheses (`lisinopril (Zestril) 10 mg`) is a constraint. It must be confirmed as the brand or the ingredient; otherwise the entry is returned for clarification, never matched to the generic.
  - If several products remain and their coverage differs, the entry is `needs_clarification` and lists the options.
  - If only the brand or generic counterpart is on the formulary, the tool says so and asks.
- **Statuses** (one per entry, in input order):
  - `matched`;
  - `matched_multiple_equivalent`;
  - `needs_clarification`;
  - `unrecognized`;
  - `lookup_failed` (upstream or timeout: coverage is **unknown**, not "not covered");
  - `invalid_entry`;
  - `deferred_batch_limit`.
- **Per plan:** tier, PA/ST/QL (with amounts), initial-coverage cost sharing with its limitations, the excluded-drug supplement, and the exact plan ID/segment, year, formulary ID and CMS file vintage.
- **Output:** summary counts, a full Markdown table and sources. Lists over 100 entries return every entry, with the rest marked `deferred_batch_limit` and a `continuation.start_at`.
- **Privacy:** lines that look like identifiers (DOB, MBI, SSN, member ID, name or address labels) are redacted and never sent upstream. Lists are not stored or logged.

## ChatGPT (remote server)
The same tools can be hosted for ChatGPT over MCP Streamable HTTP, with OAuth sign-in for individual team accounts and a per-person allowlist. **The remote server is not included in this ZIP**, which contains only the Codex plugin. To set up the ChatGPT version, check out the repository; setup steps, account requirements and teammate instructions are in `CHATGPT_SETUP.md` there:

- Repository: https://github.com/myacaexpress/MCP-Codex-Plugin (`remote/` folder)
- Setup guide: https://github.com/myacaexpress/MCP-Codex-Plugin/blob/main/CHATGPT_SETUP.md

```bash
git clone https://github.com/myacaexpress/MCP-Codex-Plugin.git && cd MCP-Codex-Plugin
pip install -r remote/requirements.txt
docker build -f remote/Dockerfile -t medcare-resources-remote .
```
The Codex plugin does not use or need any of this.

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
1. Unzip `medcare-resources-<version>.zip`.
2. `cd medcare-resources-<version> && python3 install.py`. This copies the plugin to `~/.codex/plugins/medcare-resources`, adds an entry to `~/.agents/plugins/marketplace.json` and checks that the server lists its tools.
3. `codex plugin add medcare-resources@personal-plugins`, or restart the desktop app and install it from the **Personal plugins** source.

Optional: `python3 install.py --prefetch` downloads the CMS 2027 data (about 25 MB) ahead of time so the first query is fast. To remove the plugin: `python3 install.py --uninstall`, or `codex plugin remove medcare-resources@<marketplace>`.

Re-running `python3 install.py --replace` is transactional. The new copy is checked first. If registering it fails, both the old plugin folder and the original `marketplace.json` are restored. One backup is kept at `~/.codex/plugins/medcare-resources.previous`.

**Pin a release:** `main` moves. To pin the reviewed commit, pass its SHA, which is listed in the release's FIX_REPORT.md: `codex plugin marketplace add myacaexpress/MCP-Codex-Plugin --ref <commit-sha>`. This was verified with Codex 0.163.

### First-use notes
- CMS caches are re-checked against CMS automatically when older than 7 days (set `MEDCARE_CMS_MAX_AGE_DAYS`) or on demand with `cms_refresh_data`. They are re-downloaded only when CMS publishes a new or revised file. If CMS can't be reached, the cached copy is used and `freshness.last_refresh_error` says so.
- CMS data is downloaded on first use and cached in `~/.cache/medcare-resources` (Landscape about 13 MB, Part D members about 8 MB, Census about 7 MB). Set `MEDCARE_CACHE_DIR` to change the location.
- The tools are read-only, apart from writing to that cache.
- Several chats or processes can share the cache safely. Each dataset is built by one process at a time (a file lock) into its own temp file and published atomically; the others wait and reuse it. Published files are never modified in place.

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
pip install -r tests/requirements-test.txt   # pytest<9, mcp>=1.20,<2, remote deps (test-only; the Codex plugin has no deps)
python3 -m pytest -q tests                   # offline, regression, batch and HTTP/OAuth tests
python3 tests/http_e2e.py                    # remote server as a real process + MCP SDK HTTP client (live lookups)
MEDCARE_LIVE_TESTS=1 python3 -m pytest -q tests/test_live.py   # live public sources, synthetic queries
python3 package.py                # dist/medcare-resources-<version>.zip
```
Repository layout: `remote/` is the ChatGPT HTTP transport; `plugins/medcare-resources/` is the plugin (`.codex-plugin/plugin.json`, `.mcp.json`, `bin/medcare-mcp`, `server.py`, `medcare/`, `vendor/`, `data/`, `skills/`), and `.agents/plugins/marketplace.json` is the Git marketplace.

Tested on Linux with Codex CLI/app-server 0.163.0 (marketplace add, plugin add, skill loading, MCP server start, `mcpServerStatus/list`, `mcpServer/tool/call`), Python 3.9 to 3.13, and the MCP Python SDK client. A root-level portable `plugin.json` is deliberately left out: with one present, Codex 0.163 ignored the plugin's MCP servers.
