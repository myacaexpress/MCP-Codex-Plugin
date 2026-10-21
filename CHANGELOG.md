# Changelog

## 1.2.0 — whole medication lists, and a remote (ChatGPT) transport
**Medication lists (Codex and ChatGPT)**
- New tool `medication_list_check` (28 tools in total). It takes a pasted list (`text`) and/or a structured `medications` array, with optional `plan_ids` (default: the three MedCare 2027 plans) and `year`. It returns one result per entry, in input order, with the original text kept.
- Parsing handles numbered and bulleted lists, check boxes, `;` `|` tab and comma separators, headers, sig text, start dates, abbreviations (HCTZ, APAP …) and duplicates (`duplicate_of`). Combination products such as `Losartan-HCTZ 50-12.5` or `hydrocodone/acetaminophen 5/325` stay as one entry.
- Matching uses exact RxNorm products: the same ingredient set, strengths, dose form, release type and brand vs generic. Nothing is substituted. Each entry gets one status:
  - `matched`;
  - `matched_multiple_equivalent` (every candidate has identical coverage);
  - `needs_clarification` (with the options);
  - `unrecognized` (with spelling suggestions);
  - `lookup_failed` (an upstream or timeout problem; **not** "not covered");
  - `invalid_entry`;
  - `deferred_batch_limit`.
- For each plan an entry reports tier, PA/ST/QL (with amounts), initial-coverage cost sharing with its limitations, the excluded-drug supplement, and brand/generic counterparts when only the counterpart is listed. Every result carries the exact contract-plan-segment, year, formulary ID, CMS file vintage and source.
- Each result includes summary counts, a full Markdown table and continuation (`start_at`) for lists over 100 entries. Nothing is truncated silently.
- Limits: 4 lookup workers, a time budget, and per-item isolation (an invalid plan or a failed lookup affects only its own cells).
- Lines that look like identifiers (DOB, MBI, SSN, member ID, name or address labels) are redacted, reported as ignored, and never sent upstream. Lists are not written to disk or logged. The RxNorm lookup cache is in memory only, bounded (2,000 names, 12 h).
- Both skills now use the batch tool for lists. The single-drug tools are unchanged.

**Remote transport for ChatGPT (`remote/`)**
- MCP Streamable HTTP server (`POST /mcp`, JSON responses, stateless). It imports the plugin's own `server.mcp` and dispatches through the same `Server.handle_sync()`, so schemas, validation and results are identical to the Codex stdio server (tested).
- OAuth 2.1 resource-server role:
  - RFC 9728 metadata at `/.well-known/oauth-protected-resource`, and a 401 `WWW-Authenticate` challenge with `resource_metadata`.
  - PyJWT JWKS verification of signature (asymmetric algorithms only), `iss`, `aud`/resource, `exp`/`nbf`, scope.
  - A per-person allowlist (email from a token claim or a verified userinfo email, and/or subject).
  - Per-tool `securitySchemes`.
  - The server never issues tokens; a maintained IdP does.
- `/healthz`, a body-size limit, a per-user rate limit, a global tool-call concurrency limit with a bounded wait, a request timeout, a default batch budget of 90 s, an Origin check and an MCP-Protocol-Version check. No body, argument or email logging.
- Deployment files: `remote/Dockerfile`, pinned `remote/requirements.txt`, `.env.example` (no secrets), `fly.toml.example`, `render.yaml.example`.
- Tests: `tests/test_http.py` (offline: initialize/list/call, auth failures, allowlist, limits, HTTP-vs-stdio equality), and `tests/http_e2e.py` (real uvicorn process, a throwaway local HTTPS IdP, the official MCP SDK Streamable HTTP client, live public lookups).

**Logging**
- Unexpected errors print a traceback by default (stdio). Set `MEDCARE_LOG_TRACEBACKS=0`, the default in the remote server, to log the exception type only, so argument values never reach logs.

## 1.1.1 — fixes for the independent retest of 2041bd1
- **P1 title-year detection.** A title year with no separator ("Summary of Benefits 2026"), reversed order, a colon or dash separator, Spanish titles ("Resumen de Beneficios", "Evidencia de Cobertura de …"), document codes and coverage-period statements are all collected. Any conflict means `ambiguous_conflicting_years`, so the document is not verified. Bare date mentions such as "changes on January 1, 2028" are not treated as coverage periods.
- **P2 eligibility segment.** `plan_eligibility_criteria` no longer uses an arbitrary first segment. The segment is taken from the request, from a single-segment plan, or from the one segment whose CMS service area covers every supplied county/ZIP county; this choice is disclosed. Otherwise it returns `segment_required`. `compare_plans`, `cms_plan_details` (Medicare.gov link) and the CMS cross-check in `plan_document_retrieve` also no longer use the first segment.
- **MCP.** A message with no `method` returns -32600 Invalid Request unless it is a genuine response (an `id` plus exactly one of `result`/`error`).

## 1.1.0 — fixes for the independent review of 1.0.0 (commit bdee23f)
1. **Wrong benefit year.** Bundled MedCare documents are re-verified for the requested year and exact contract-plan-segment before use. Year is resolved from the title area and document code (e.g. `H5767_SB0012027_M`). Requests for 2026 get `no_verified_document`.
2. **Wrong competitor segment.** Cached documents match only when their verified `contract-plan-segment` and year equal the request. Plans with several segments need an explicit segment. PDFs listing several segments return `multiple_segments_in_document_column_review_required` (not verified).
3. **Ambiguous years.** Conflicting coverage years in the title area, or in effective dates when there is no title year, make the document `unverified`.
4. **Provider filters.** Plan, specialty and accepting-new-patients are applied to the same plan entry. Unknown plan membership is excluded and counted.
5. **PDP criteria.** PDP uses Part A or Part B; MA/SNP use Part A and Part B. Unknowns are handled explicitly.
6. **Ambiguous ZIP.** Mixed coverage returns `unknown_zip_spans_covered_and_uncovered_counties`. An explicit county overrides the ZIP. `cms_find_plans` returns `county_required` for multi-county ZIPs. `cms_plan_details` adds `service_area_determination`.
7. **CMS refresh.** Seven-day auto re-check (`MEDCARE_CMS_MAX_AGE_DAYS`), and a new `cms_refresh_data` tool. Data is re-downloaded only on a new or revised CMS file. Source vintage, age and refresh errors are disclosed.
8. **Malformed MCP.** Every request is type-validated (params, name, arguments, method, id). Every request gets a JSON-RPC error or result. A bad message can no longer crash or hang the server.
9. **Installer.** Transactional replace: code and marketplace registration roll back together on any failure.
- Windows is explicitly unsupported (the installer refuses to run; the README says so).
- Test SDK pinned (`tests/requirements-test.txt`: `mcp>=1.20,<2`).
- Browser hand-offs are labelled (`access_mode`, `retrieved_by_tool: false`, `evidence: false`).
