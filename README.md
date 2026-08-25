# IT Company Finder — standalone edition

Finds IT/software companies in a location and returns eight fields per company.
**Calls no paid Store actors**, so it runs on the Apify Creator plan.

## Why this version exists

The earlier version orchestrated two rented actors (`compass/crawler-google-places`
and `harvestapi/linkedin-company`). The Creator plan restricts access to Apify
Store actors, so that architecture cannot run on it. This version replaces both
with resources included in every Apify plan.

| Field | v2 (paid actors) | v3 (this version) |
|---|---|---|
| Name, address, website | compass Maps actor | Google local finder via `GOOGLE_SERP` proxy |
| Rating, reviews | compass Maps actor | same local finder page |
| Owner / founder | website crawl | website crawl → Google SERP fallback |
| Employees | harvestapi LinkedIn actor ($3/1k) | LinkedIn's Google snippet |
| Type (service/product/both) | website crawl | unchanged |

## How it works

```
Stage 1  Discovery      GOOGLE_SERP proxy → google.co.in/search?tbm=lcl
                        ~20 businesses per page, paginated by 20
                        Swept across areas × terms, deduped on 2 keys
                        Yields fields 1-5 directly
Stage 2  Website crawl  Direct HTTP, no proxy, costs no SERP quota
                        Yields founder, team-size hint, service/product signals
Stage 3  SERP enrich    1 request/company for founder (only if the site was silent)
                        1 request/company for employees (LinkedIn snippet)
Stage 4  Merge + emit   One dataset row per company, every inferred field
                        carrying a confidence level
```

`tbm=lcl` is the key detail. A normal Google search shows a 3-result local pack;
the local-finder tab returns roughly twenty businesses per page **with rating and
review count inline**, and paginates. That single endpoint covers five of the
eight fields.

## Getting *all* companies (the area sweep)

Google's local finder caps out near 100 results per query. Running 24 search
terms city-wide therefore plateaus around 200 companies no matter how deep you
paginate. The way past that ceiling is to multiply queries **geographically**:
run every term separately against each locality.

Set `useBuiltInAreas: true` to sweep 46 built-in Kolkata/Howrah localities, or
pass your own list in `areas`.

| Configuration | Discovery requests | Realistic yield |
|---|---|---|
| City-wide, 3 pages (default) | 72 | ~200 |
| Core 12 areas, 2 pages | 576 | ~400 |
| **All 46 areas, 2 pages** | **2,208** | **~550-650** |
| All 46 areas, 3 pages | 3,312 | ~650 (diminishing) |

Cross-area duplication is heavy and expected — the same firm surfaces from
several localities and several terms. Dedupe runs on two keys at once (website
domain *and* folded company name) with an alias index, because Google exposes
the website link inconsistently; without that, one company becomes two rows.
Duplicate sightings are merged, keeping the fullest address, the rating, the
phone and the longest name variant.

## Sectors

Scope is wider than "IT". Each sector contributes its own search phrases, and
each output row is tagged with the sector it matched.

**On by default** (20 search phrases): `software` · `web_mobile` ·
`digital_marketing` · `erp_crm` · `it_services`

**Available but off**: `data_ai` · `cybersecurity` · `game_dev`

The off-by-default sectors are still *detected*, which matters more than it
sounds. Deleting their patterns would not exclude those companies — an AI or
security firm would simply fall through to the generic `it_services` catch-all
(which matches ordinary words like "solutions" and "systems") and get included
under the wrong label. Keeping the patterns and dropping on resolution is what
actually excludes them.

Two rules make that precise:

- **A specific wanted sector always wins.** A software firm that mentions AI is
  kept as `software`, not dropped as `data_ai`. Only *pure plays* in an
  unselected sector are excluded.
- **The generic catch-all cannot rescue.** `it_services` can classify a listing
  that has no more specific match, but it cannot pull back a listing whose
  specific match is an excluded sector — otherwise "Indian Cyber Security
  Solutions" slips through purely on the word "Solutions".

Separately rejected are businesses that aren't vendors at all — training
institutes, recruitment agencies, repair shops, retailers — which Google mixes
into every one of these queries. A firm whose name carries a strong vendor
signal is rescued even if its Google category reads like a training institute.

All rejections are tallied by reason in the run log
(`excluded sector:data_ai=37, rejected:recruitment=22, ...`), so filtering is
auditable rather than mysterious.

## SERP budget

SERP requests are the scarce resource — the Creator plan includes **10,000 per
month**. The actor caps itself via `serpBudget` (default 500) so a misconfigured
run cannot drain the allowance.

Enrichment costs about 1.6 requests per company on top of discovery. A full
Kolkata sweep therefore looks like:

| Stage | Requests |
|---|---|
| Discovery (24 terms × 46 areas × 2 pages) | 2,208 |
| Enrichment (~600 companies × 1.6) | ~960 |
| **Total** | **~3,170** |

That leaves roughly 6,800 of the monthly 10,000 spare. Turn off
`enrichEmployeesFromSerp` to cut enrichment roughly in half.

`serpBudget` (default 2,000) is a hard stop. Raise it to ~4,000 before running a
full area sweep, or discovery will halt partway through.

## Running locally

```bash
pip install -r requirements.txt
export APIFY_PROXY_PASSWORD=<from Apify Console → Proxy>
export APIFY_TOKEN=<your token>
python -m my_actor
```

Note: on the Apify **free** plan the proxy is only reachable from inside the
platform. On a paid plan (Creator included) you can use it from your own machine.

## When it breaks — and it will

The actor parses Google's own HTML, and Google rotates CSS class names without
notice. When discovery suddenly returns zero results, that is almost always the
cause rather than a logic bug.

The fix path is built in:

1. Re-run with **`debugDumpHtml: true`**.
2. Open the run's key-value store and download `lcl-<term>.html`.
3. Compare the real markup against the selector lists in `my_actor/serp.py`
   (`parse_local_results`, `parse_organic_results`) and add the new class name.

The parsers already try several strategies and fall back to text-level regex for
ratings and review counts, so partial breakage usually degrades rather than fails
outright. High `blocked` counts in the log mean rate-limiting instead — raise the
delay in `SerpClient`.

## On LinkedIn

This actor never fetches or logs into linkedin.com. It reads only what Google
already publishes in its result snippets, which for company pages routinely
includes the headcount. Scraping LinkedIn directly is both technically blocked
and legally contested; that is a deliberate omission, not a gap.

Consequence: employee coverage is lower than a dedicated LinkedIn actor would
give, and the value is often a band ("201-500") rather than an exact number. The
output labels which it is.

## Confidence levels

Fields 6–8 carry a confidence, because a lead list you can't calibrate is a
liability:

- **high** — stated on the company's own site, or a LinkedIn figure corroborated elsewhere
- **medium** — two independent third parties agree
- **low** — single aggregator, or sources disagreeing by more than 3×
- **not found** — searched, came up empty

### The director trap, enforced in code

Indian company registries (MCA, Zauba Corp, Tofler) list **directors** — a legal
role that is frequently *not* a founder: a spouse added to meet the two-director
minimum, or a later professional hire. A name sourced only from a registry is
reported as `Director (per company registry)` at low confidence and is never
promoted to "founder".

Similarly, LinkedIn follower counts get misread as headcount constantly. If the
same number appears as both, it is discarded rather than reported.
