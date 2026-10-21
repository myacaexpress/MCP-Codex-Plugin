# Changelog

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
