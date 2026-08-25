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

## SERP budget

SERP requests are the scarce resource — the Creator plan includes **10,000 per
month**. The actor caps itself via `serpBudget` (default 500) so a misconfigured
run cannot drain the allowance.

Rough cost for 100 companies:

| Stage | Requests |
|---|---|
| Discovery (5 terms × 3 pages) | 15 |
| Founder lookups (only where the website was silent) | ~60 |
| Employee lookups | 100 |
| **Total** | **~175** |

That is about 1.75 SERP requests per company, so the monthly allowance is worth
roughly 5,700 companies. Turn off `enrichEmployeesFromSerp` to halve it.

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
