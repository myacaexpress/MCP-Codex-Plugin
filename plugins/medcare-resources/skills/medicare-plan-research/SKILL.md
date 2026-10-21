---
name: medicare-plan-research
description: Research and compare Medicare Advantage, SNP and Part D plans (MedCare and competitors) for an exact year, county and contract-plan-segment using official CMS data, Medicare.gov hand-offs and verified carrier Summary of Benefits documents, with public eligibility criteria that are never treated as verified beneficiary eligibility.
metadata:
  short-description: Medicare plan research with CMS data and verified SBs
---

# Medicare plan research

Use the `medcare-resources` MCP tools. Work from public plan data only; never request or transmit beneficiary
identifiers. Use only context the user explicitly supplied.

## Workflow
1. **Pin the scope**: coverage year, state + county (or ZIP -> `zip_to_county`; if a ZIP spans counties, ask which
   county), plan type/SNP interest.
2. **Find plans**: `cms_find_plans` (CMS Landscape: exact `contract-plan-segment`, organization, premiums, Part D
   deductible, MOOP, stars, SNP type and C-SNP conditions). Record the Landscape `data_last_updated` date.
3. **Plan identity**: `cms_plan_details` for each candidate - segments, service area, Part D formulary ID, county
   check, Medicare.gov Plan Compare link (browser hand-off; it is not scraped), and document leads.
4. **Official Summary of Benefits**:
   - MedCare (H5767): already bundled and verified.
   - Competitors: use web search with the returned `search_queries` (or open the Medicare.gov plan page in a browser
     and follow the plan's document link) to find the PDF on the carrier's own domain, then call
     `plan_document_retrieve(url, plan_id, year)`. Only `status: verified_document` (year, contract-plan and segment
     confirmed from the PDF text) may be quoted. `unverified_document` = do not use. Broker/aggregator copies are
     not official.
5. **Benefits**: `plan_document_search(plan_id, year, query)` for each topic; quote lines with page citations.
6. **Drugs**: `cms_drug_coverage(drug_name, plan_ids, year)` (CMS formulary file via RxNorm) and
   `cms_tier_cost_sharing(plan_id, year)`. For MedCare also `medcare_drug_lookup` (live plan formulary site).
7. **Compare**: `compare_plans(plan_ids, year, topics, drugs)`. Keep unknowns as unknown, flag non-comparable
   fields (different segments, plan types, years), and do not call any plan "best" on incomplete evidence.
8. **Criteria**: `plan_eligibility_criteria` returns public requirements (Part A/B, service area, SNP conditions,
   D-SNP Medicaid requirement). Pass only facts the user stated. Report results as "appears to meet / does not
   appear to meet / unknown - not verified". Enrollment period, Medicaid level, condition verification and plan
   acceptance are never verified here.

## Output checklist
- Exact plan ID with segment, plan name, year, county.
- Source + date for every figure (CMS file version, PDF URL#page, retrieved_at).
- Separate: confirmed from official source / stated in source but unverified / unknown.
- Note that Medicare.gov links are for the user to open; this tool did not read them.
