# CLAUDE.md — SteinJobs (demo fork)

This is the public demo of SteinJobs. The rubric below is what the scorer
reads; the candidate profile is data/master_cv.md.

## What to find

High-agency generalist roles at pre-seed / seed / Series A startups (Series B only if the role is explicitly building something 0→1). Titles that fit: founding team / early employee, growth, marketing, BD/partnerships, chief of staff, PM, co-founder-adjacent. Judge the SHAPE of the work, not the title: owning outcomes end-to-end, wearing many hats, outward-facing, evangelizing the product. Deals, experiments, and relationships > months of solo work.

## Hard excludes

- Pure IC data science / analytics roles — but NOT hybrid technical-GTM roles.
  "Growth Engineer", "GTM Engineer", "Forward Deployed Engineer" and "Deployment
  Strategist" are actively wanted: they're where the data background meets the
  outward-facing work. The exclude is a pure IC seat with no ownership or customer
  contact, not any title containing "engineer".
- Big-company narrow-scope roles
- Growth-stage companies (100+ people, playbook already written)
- Consulting
- Heads-down solo-output roles
- Chief-of-staff roles that are executive assistants in disguise (calendar, travel,
  expenses, inbox). A CoS who *builds* — owns projects and real scope — is ideal;
  only the description distinguishes them.
- Senior/Sr-titled roles (unless "Founding …" or chief of staff) — prefiltered.
- Internships, co-ops, Werkstudent seats — not career seats; prefiltered.
- Sales/service execution seats — Account Executive, Account Manager, Customer
  Success, designer titles (Eric, 2026-08-17: measured as the pile's biggest
  noise). Prefiltered unless "Founding …". Solutions Engineer deliberately
  survives — it's the technical-GTM family above.

## Industries

Sweet spot: health & human performance broadly — health/digital/sports tech, fitness, supplements & health-food CPG, longevity, recovery, wearables (products Eric would authentically use himself).

Interest-based exceptions — real personal history here, so a great early-stage role in any of these clears the bar even outside health:
- Music / creative industry (co-founded and ran growth for a band)
- Psychedelics & mental health (UCSF Neuroscape research, published preprint)
- Neuroscience / clinical research tools (Emory Epilepsy Center)
- Sustainable agriculture & food systems (WWOOF farming, food science). The target
  is TECH whose product serves agriculture/food/farming — farm fintech, ag-SaaS,
  food-supply-chain software — not farms or food producers that employ a technologist.
  Covered by `boards/climatebase.py`, sector-filtered to "Food, Agriculture, & Land Use".
- Ed-tech (US go-to-market for an AI language-learning startup)
- Civic tech / election integrity (Carter Center disinformation ML work)
- Fitness & strength (competitive powerlifting, personal training)
- Outdoors / backpacking (thru-hiked the PCT through California)

The real filter is "interesting early-stage startup where Eric would own outcomes." ESCAPE HATCH: for a truly exceptional role, throw all rules out and flag it as such.

## Scoring rubric

**The API is parked (Eric, 2026-08-18): no Anthropic API spend unless absolutely
needed.** `.env` holds the key as `ANTHROPIC_API_KEY_PARKED`; with no live key,
every pass degrades to its in-session twin by design — scout queues
`data/candidates.json` for Claude Code to score on the Max plan, research/
extraction queues drain in-session ("run the scoring", "run the substack
extraction"). Fetching (boards, EDGAR, link sweep) never needed tokens and is
unaffected. To re-enable temporarily, rename the key back.

When the API path IS used: **Haiku 4.5 by default** (`SCORER_MODEL` overrides) —
rubric-checklist scoring is Haiku work. Runs of 10+ postings go through the
**Batch API at half price**; measured 2026-08-05: ~500 postings ≈ $0.56. The
scorer marks 2–4 decision-relevant phrases in **bold**; the UI renders exactly
those markers. No blind scoring (see Working in this repo).

Score each match 0–100:
- Role shape / agency fit: 35%
- Stage fit: 25%
- Industry fit: 20%
- Location: 10%
- Comp / equity signal: 10%
- Bonus +5 if it's a product Eric would authentically use
- Escape-hatch roles: score on gut, mark them as escape-hatch in "why"

When a posting is ambiguous from the title, open it and read the full description before scoring.
