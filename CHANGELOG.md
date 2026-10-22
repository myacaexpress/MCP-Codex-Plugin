# Changelog

## 1.2.5 — readers survive a concurrent refresh (P2 from the review of 1.2.4)
- **The race.** A reader that picked the newest published Part D or Landscape file outside the lock could find it deleted before opening it. Another process could finish a refresh in that window. The reader then failed with `unable to open database file`. `medication_list_check` reported this correctly as `lookup_failed`; it is now retried instead.
- **Selection and opening are one step.** The new `_open_published` selects the newest generation, opens it, and only then checks freshness on the open connection. If the file vanished or was replaced in between, it re-selects and retries, up to 8 times with backoff, then raises an explicit `CMSDataError`. The reader therefore lands on the newly published generation, and that generation's own metadata is the reported provenance.
- **Generations are pinned.** `_connect` reads from the file immediately, so a connection keeps reading the generation it opened even if the path is replaced or deleted later.
- **Freshness stamps are bound to their generation.** Sidecar stamps carry the build identity of the file they describe. A reader holding an older generation never borrows a newer file's `last_checked_at` after a same-name rebuild.
- **Lock-timeout fallback.** The fallback path (use the existing cache when another build holds the lock too long) uses the same retrying open.
- **Equivalent caches checked:**
  - Landscape: fixed in the same way.
  - Census ZIP→county: built once and never deleted or replaced, so no window.
  - Site catalog and carrier documents: replaced atomically and never deleted; readers accept only a matching catalog pair.
- **New deterministic multiprocess regressions:**
  - a reader paused exactly between selection and opening while a real refresh in another process publishes a new Part D file or Landscape generation and deletes the old one;
  - a reader paused after opening (it keeps its generation's data and provenance);
  - the bounded retry;
  - generation-bound sidecar stamps.

  The paused-reader tests fail on 1.2.4 with `unable to open database file`.

## 1.2.4 — cross-process safe caches (review of 1.2.3)
- **Problem fixed.** Separate MCP server processes sharing the per-user cache could corrupt each other's first build or refresh: several Codex chats, a test run, or the remote server's workers. Errors included `table formulary already exists`, `disk I/O error` and `attempt to write a readonly database`. The cause was a process-local lock, one shared `*.tmp` name per dataset, and in-place writes to published files.
- **New `medcare/fslock.py`:**
  - one builder per dataset across processes and threads (`fcntl.flock` on a per-dataset lock file);
  - bounded wait (`MEDCARE_CACHE_LOCK_TIMEOUT`, default 900 s);
  - a uniquely named temp file per build (pid + random), published with an atomic `os.replace`;
  - a builder removes only its own temp; orphans of a dataset are removed only while holding that dataset's lock;
  - atomic writes for small files.
- **CMS Landscape, Part D file and Census ZIP→county caches:**
  - double-checked locking: a process that waited reuses the file another process just published instead of rebuilding;
  - published SQLite files are never written again and are opened read-only/immutable;
  - freshness stamps (`last_checked_at`) move to an atomic sidecar (`<file>.checked.json`);
  - old files are removed only after the new one is published.
- **Other cache files:** the refreshed site catalog is written atomically under a lock, and readers accept it only as a matching catalog+documents pair. Verified carrier-document records are written atomically.
- **Error handling stays explicit:**
  - a cache lock timeout or SQLite/I/O error is reported as `CMSDataError`;
  - in `medication_list_check` it becomes a per-plan `lookup_failed` with every entry still accounted for in order, never a crash or "not covered";
  - if a previous cache exists, it is used and the timeout is disclosed in `freshness.last_refresh_error`.
- **New deterministic multiprocess regressions** (`tests/test_cache_concurrency.py`, separate spawned processes, synthetic data):
  - concurrent first build of each dataset: built once, every process succeeds, no temp files left;
  - concurrent refresh to a new file while readers query;
  - failed build: cleans its temp, releases the lock, reports the error;
  - orphan cleanup scoped to the locked dataset;
  - published files are read-only;
  - cache errors keep every entry accounted.

  These fail on 1.2.3 with the reported errors.

## 1.2.3 — explicit parenthetical brand is never lost (review of 1.2.1)
- **Batch P1 fix.** Before 1.2.3, a parenthetical name such as `lisinopril (Zestril) 10 mg` was only queried when the first RxNorm response had no results. If that response did not include the brand, the generic was selected silently.
- The parenthetical name is now captured before any lookup and **always queried**. The result depends on what RxNorm confirms:
  - It must be confirmed either as a brand of the product, so only that brand's products can match, or as the entry's own ingredient (`Zestril (lisinopril)`).
  - Otherwise the entry returns `needs_clarification`: `parenthetical_name_not_found_in_rxnorm_results` or `parenthetical_brand_conflicts_with_written_brand`. The generic is never selected.
  - A failed alias lookup makes the entry `lookup_failed`.
  - `(generic)` keeps generic intent.
- Regressions:
  - the reviewer's reproduction (upstream omits the brand on every query);
  - the alias query returning nothing;
  - the alias query failing;
  - the brand present only via the alias, and present in the first response;
  - brand first with the generic in parentheses;
  - `(generic)`;
  - two conflicting brands;
  - unknown parenthetical text.
- Includes the 1.2.2 remote email-binding fix.

## 1.2.2 — remote OAuth: email verification bound to the exact address (review of 1.2.1)
- **P1 fix.** An email is now treated as verified only by the verification flag that belongs to that same address:
  - A custom claim (for example `https://medcare-resources/email`) needs its own `…_verified: true`.
  - An explicit custom flag with any other value (`false`, `"true"`, `1`, `null`) is final and is never overridden.
  - Only when the custom flag is absent may the standard `email_verified: true` be used, and only if the standard `email` is the same address.
  - Before this fix, a token carrying a verified `outside@elsewhere.example` and an explicitly unverified, allowlisted custom email was accepted.
- New mixed-claim regressions: 20 claim combinations × both claim orders, the reviewer's reproduction, and a check that subject-matched userinfo stays the only fallback when no claim is verified.
- No changes to the Codex plugin's behaviour (the version is bumped only).

## 1.2.1 — fixes for the independent review of 1.2.0 (commit 19843bf)
**Medication list matching (`medication_list_check`)**
1. **Units, denominators and association.** A strength now matches only when value, unit and denominator all agree:
   - mg never equals units; per-mL never equals a bare amount;
   - `%` converts to 10 mg/g or 10 mg/mL (hydrocortisone 1% matches 10 MG/G, never 1 MG/G);
   - "400 mg/5 mL" and "21 mg/24 hr" are normalised to per-1 values.
   - When each ingredient is named, values bind to ingredients in written order: `losartan/HCTZ 12.5/50` no longer matches HCTZ 12.5 / losartan 50.
   - When a brand is written with unnamed values (`Hyzaar 50-12.5`), matching is by value set and is disclosed (`strength_association: not_stated`).
2. **Unitless numbers are kept.** `losartan/HCTZ 50-12.5`, `aspirin 81` and `lisinopril10` keep their numbers as a constraint and return `needs_clarification` (`strength_unit_not_stated`), with options limited to products whose numbers fit. They never match any strength. Counts such as "1 tab" or "2 puffs" are still treated as counts.
3. **Delivery forms are distinct.** These forms each match only their own RxNorm dose forms: vial, pen, cartridge, auto-injector, syringe, nebulizer, metered-dose inhaler, dry-powder inhaler and soft-mist inhaler. Generic words ("injection", "inhalation") stay broad, and any coverage difference is raised as a clarification.
4. **Numeric commas.** Thousands commas (`1,000 units`) and decimal commas (`0,025 mg`, `2,5 mg`) are protected before list splitting. The original text is echoed unchanged.
5. **Duplicates.** The duplicate key includes the parenthetical brand, the ingredients and the full strength (value, unit, denominator), plus unresolved numbers, form and release. `lisinopril (Zestril) 10 mg` is no longer a duplicate of `lisinopril 10 mg`.
6. **Identifiers on every path.**
   - Structured items are checked field by field (name, strength, form, release, original) with the same identifier rule as pasted text. Matching items are redacted and never sent upstream.
   - A final guard blocks identifier-like queries before RxNav, spelling suggestions and the MedCare Navigator.
   - The identifier pattern now also catches `Name:` labels (previously missed), "member", "beneficiary", policy numbers and 9-digit numbers.
7. **No stale CMS data.** The cross-call plan/cost cache is removed. Each call opens the CMS Part D file once and uses that same file for every plan context, formulary/cost lookup and the reported provenance. Each plan result now carries `cms_file`. The MedCare Navigator cache is per call.
8. **Supplemental coverage per product.** Formulary terms and supplemental (excluded-drug) terms are kept for each product, including tier, PA, ST, QL and capped benefit. One answer is given only when every matched product has identical terms; otherwise the result is `varies_by_product_needs_clarification`.
- Also fixed:
  - RxNorm names with disambiguation prefixes (`NDA021457 200 ACTUAT …`) now parse, so metered-dose inhalers match.
  - "insulin glargine" no longer matches the biosimilar "insulin glargine-yfgn" unless it is written.
  - "vitamin D3/D2" now maps to cholecalciferol/ergocalciferol.

**Remote (ChatGPT) server**
9. An email counts only when its verified claim is the JSON literal `true`, for both token claims and userinfo. The `MEDCARE_REQUIRE_EMAIL_VERIFIED` opt-out is removed.
10. Userinfo, metadata and JWKS requests never follow redirects. They go only to plain-HTTPS URLs (no embedded credentials) on the issuer's host or an explicitly configured host. The bearer token cannot be forwarded elsewhere.
11. A userinfo response must contain a string `sub` exactly equal to the token's `sub`.
12. Logs record only allow-listed JSON-RPC method names; anything else is logged as `unknown`.

**Docs**
- The ZIP README says the remote server is not in the ZIP and links to the repository checkout. A test checks that every relative README link resolves inside the ZIP.

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
