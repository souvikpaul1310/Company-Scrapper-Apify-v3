"""IT Company Finder - standalone edition.

Uses no paid Store actors, so it runs on the Apify Creator plan. Everything
comes from resources included in every plan:

  Stage 1  discovery      Google local finder via the GOOGLE_SERP proxy group
  Stage 2  website crawl   direct HTTP (no proxy needed) - founder, type, size
  Stage 3  SERP enrichment GOOGLE_SERP again - founder, employee count
  Stage 4  merge + emit    one dataset row per company, with confidences

SERP requests are the scarce resource (the Creator plan includes 10,000/month),
so the actor budgets them explicitly and reports consumption at the end.
"""

from __future__ import annotations

import asyncio
import logging
import re
from urllib.parse import urlparse

from apify import Actor

from .classify import classify_company_type
from .people import extract_employees, extract_founder
from .serp import SerpClient
from .taxonomy import (
    CATEGORY_LABELS,
    DEFAULT_CATEGORIES,
    KOLKATA_AREAS,
    categorise,
    resolve_category,
    search_terms_for,
)
from .website import WebsiteData, enrich_from_website

logger = logging.getLogger(__name__)

def _domain(url: str) -> str:
    try:
        host = (urlparse(url).netloc or "").lower()
        return host[4:] if host.startswith("www.") else host
    except Exception:
        return ""


def _dedupe_keys(name: str, website: str) -> list[str]:
    """All identity keys a listing could be filed under.

    Google's local finder exposes the website link inconsistently, so the same
    company arrives sometimes with a domain and sometimes without. Returning
    both a domain key and a folded-name key -- and registering every key as an
    alias of one canonical record -- stops one company becoming two rows.
    """
    keys: list[str] = []
    dom = _domain(website)
    if dom:
        keys.append(f"d:{dom}")

    folded = re.sub(r"[^a-z0-9]", "", (name or "").lower())
    # Strip repeatedly: "X Solutions Pvt Ltd" must fold to the same key as
    # "X Solutions" and "X SOLUTIONS PRIVATE LIMITED".
    suffixes = re.compile(
        r"(?:privatelimited|pvtltd|private|limited|ltd|llp|opc|inc|corp|"
        r"technologies|technology|solutions|solution|services|service|"
        r"infotech|softwares|software)$"
    )
    while True:
        stripped = suffixes.sub("", folded)
        if stripped == folded or len(stripped) < 4:
            break
        folded = stripped
    if folded:
        keys.append(f"n:{folded}")
    return keys


def _merge_place(prev, row) -> None:
    """Fold a duplicate sighting into the record we already hold."""
    if not prev.website and row.website:
        prev.website = row.website
    if prev.rating is None and row.rating is not None:
        prev.rating, prev.reviews = row.rating, row.reviews
    if len(row.address or "") > len(prev.address or ""):
        prev.address = row.address
    if not prev.phone and row.phone:
        prev.phone = row.phone
    if not prev.category and row.category:
        prev.category = row.category
    # Prefer the longest name variant: it usually carries the legal suffix and
    # reads better in a lead list.
    if len(row.name or "") > len(prev.name or ""):
        prev.name = row.name


async def main() -> None:
    async with Actor:
        cfg = await Actor.get_input() or {}

        location = (cfg.get("location") or "").strip()
        if not location:
            await Actor.fail(status_message="`location` is required (e.g. 'Kolkata, India').")
            return

        categories = cfg.get("categories") or list(DEFAULT_CATEGORIES)
        terms = [t.strip() for t in (cfg.get("searchTerms") or []) if t and t.strip()]
        if not terms:
            terms = search_terms_for(categories)

        # Area sweep. Google's local finder caps out near 100 results per
        # query, so running the same terms against each sub-area is the only
        # way to get past that ceiling towards full coverage.
        areas = [a.strip() for a in (cfg.get("areas") or []) if a and a.strip()]
        if not areas:
            if cfg.get("useBuiltInAreas", False):
                areas = list(KOLKATA_AREAS)
            else:
                areas = [location]

        country = (cfg.get("countryCode") or "IN").upper()
        max_pages = max(1, int(cfg.get("maxLocalPagesPerTerm") or 3))
        target = int(cfg.get("maxCompanies") or 0)  # 0 = no limit
        serp_budget = int(cfg.get("serpBudget") or 500)

        apply_taxonomy = cfg.get("itFilter", True)
        require_website = cfg.get("requireWebsite", False)
        min_rating = cfg.get("minRating")
        min_reviews = int(cfg.get("minReviews") or 0)

        do_website = cfg.get("enrichFromWebsite", True)
        do_serp_founder = cfg.get("enrichFounderFromSerp", True)
        do_serp_employees = cfg.get("enrichEmployeesFromSerp", True)
        debug_dump = cfg.get("debugDumpHtml", False)

        pages_per_site = max(1, int(cfg.get("maxPagesPerSite") or 5))
        concurrency = max(1, int(cfg.get("concurrency") or 8))
        req_timeout = max(5, int(cfg.get("requestTimeoutSecs") or 20))

        # The GOOGLE_SERP group needs the proxy password, which the platform
        # injects into every run.
        proxy_cfg = await Actor.create_proxy_configuration(groups=["GOOGLE_SERP"])
        password = None
        if proxy_cfg is not None:
            password = getattr(proxy_cfg, "_password", None)
        password = password or Actor.config.proxy_password
        if not password:
            await Actor.fail(
                status_message=(
                    "No Apify proxy password available. The GOOGLE_SERP proxy group is "
                    "required for discovery; run this actor on the Apify platform."
                )
            )
            return

        store = await Actor.open_key_value_store() if debug_dump else None

        # ---------------------------------------------------- Stage 1: discover
        discovered: dict[str, dict] = {}
        alias: dict[str, str] = {}   # every identity key -> canonical record key
        reject_tally: dict[str, int] = {}
        plan = [(area, term) for area in areas for term in terms]
        logger.info(
            "Discovery plan: %s areas x %s terms = %s queries, up to %s pages each "
            "(worst case %s SERP requests, budget %s)",
            len(areas), len(terms), len(plan), max_pages,
            len(plan) * max_pages, serp_budget or "unlimited",
        )

        async with SerpClient(
            password,
            country=country,
            timeout=req_timeout,
            budget=serp_budget,
            store=store,
        ) as serp:
            for area, term in plan:
                if serp.stats.exhausted or (target and len(discovered) >= target):
                    break
                query = f"{term} in {area}"

                for page in range(max_pages):
                    if serp.stats.exhausted or (target and len(discovered) >= target):
                        break

                    rows, html = await serp.local_search(query, start=page * 20)
                    if debug_dump and html and page == 0 and area == areas[0]:
                        await serp.dump_html(
                            f"lcl-{re.sub(r'[^a-z0-9]+', '-', term.lower())}.html", html
                        )

                    if not rows:
                        break

                    kept = 0
                    for row in rows:
                        category = ""
                        if apply_taxonomy:
                            matched, reason = categorise(row.name, row.category)
                            if matched:
                                category, reason = resolve_category(matched, categories)
                            if not category:
                                bucket = reason.split(" ")[0] if reason else "unknown"
                                reject_tally[bucket] = reject_tally.get(bucket, 0) + 1
                                continue

                        keys = _dedupe_keys(row.name, row.website)
                        existing = next((alias[k] for k in keys if k in alias), None)
                        if existing is not None:
                            _merge_place(discovered[existing]["place"], row)
                            # Register any newly-learned key against the same
                            # canonical record.
                            for k in _dedupe_keys(
                                discovered[existing]["place"].name,
                                discovered[existing]["place"].website,
                            ) + keys:
                                alias[k] = existing
                            continue

                        canonical = keys[0]
                        discovered[canonical] = {
                            "place": row,
                            "term": term,
                            "area": area,
                            "category": category,
                        }
                        for k in keys:
                            alias[k] = canonical
                        kept += 1

                    logger.info(
                        "[%s/%s] %r p%s: %s rows, %s new (total %s)",
                        serp.stats.requests, serp_budget or "-", query, page,
                        len(rows), kept, len(discovered),
                    )
                    if len(rows) < 5:
                        break  # thin page: pagination has run out

            logger.info("Discovery finished: %s unique companies", len(discovered))
            if reject_tally:
                logger.info(
                    "Filtered out by taxonomy: %s",
                    ", ".join(f"{k}={v}" for k, v in sorted(reject_tally.items(), key=lambda x: -x[1])),
                )

            # ------------------------------------------------- filters on 4/5
            def passes(place) -> bool:
                if require_website and not place.website:
                    return False
                if min_rating is not None:
                    if place.rating is None or place.rating < float(min_rating):
                        return False
                if min_reviews and (place.reviews or 0) < min_reviews:
                    return False
                return True

            selected = [v for v in discovered.values() if passes(v["place"])]
            if target:
                selected = selected[:target]
            logger.info("%s companies passed filters", len(selected))

            # ------------------------------------- Stage 2: crawl own websites
            website_data: dict[str, WebsiteData] = {}
            if do_website:
                sem = asyncio.Semaphore(concurrency)

                async def crawl(entry) -> None:
                    place = entry["place"]
                    if not place.website:
                        return
                    async with sem:
                        try:
                            website_data[place.name] = await enrich_from_website(
                                place.website,
                                max_pages=pages_per_site,
                                timeout_secs=req_timeout,
                            )
                        except Exception as exc:
                            logger.warning("Website crawl failed for %s: %s", place.name, exc)

                await asyncio.gather(*(crawl(e) for e in selected))
                logger.info("Crawled %s websites", len(website_data))

            # ------------------------------------ Stage 3: SERP enrichment
            for entry in selected:
                place = entry["place"]
                wd = website_data.get(place.name)

                founder = ("", "", "not found", "")
                employees = ("", "not found", "")

                # Prefer what the company says about itself.
                if wd and wd.founder_name:
                    founder = (wd.founder_name, wd.founder_role or "", "high", "company website")

                if do_serp_founder and not wd or (do_serp_founder and not founder[0]):
                    if not serp.stats.exhausted:
                        q = f'"{place.name}" founder OR CEO {location}'
                        results, _ = await serp.organic_search(q)
                        if results:
                            founder = extract_founder(results, place.name)

                if do_serp_employees and not serp.stats.exhausted:
                    q = f'site:linkedin.com/company "{place.name}"'
                    results, _ = await serp.organic_search(q)
                    hint = wd.employee_hint if wd else None
                    employees = extract_employees(results, hint)
                elif wd and wd.employee_hint:
                    employees = (f"{wd.employee_hint} (company site)", "medium", "")

                entry["founder"] = founder
                entry["employees"] = employees

            stats = serp.stats

        # ------------------------------------------------ Stage 4: emit rows
        rows_out = []
        for entry in selected:
            place = entry["place"]
            wd = website_data.get(place.name)
            f_name, f_role, f_conf, f_note = entry.get("founder", ("", "", "not found", ""))
            e_val, e_conf, e_note = entry.get("employees", ("", "not found", ""))

            if wd and wd.company_type:
                c_type, c_conf, c_evidence = wd.company_type, wd.type_confidence, wd.type_evidence
            else:
                c_type, c_conf, c_evidence = "not found", "not found", ""

            notes = [n for n in (f_note, e_note) if n]
            if wd and not wd.reachable:
                notes.append("website did not respond")

            rows_out.append(
                {
                    "company_name": place.name,
                    "category": entry.get("category", ""),
                    "category_label": CATEGORY_LABELS.get(entry.get("category", ""), ""),
                    "address": place.address or "not found",
                    "website_url": place.website or "not found",
                    "rating": place.rating,
                    "reviews": place.reviews,
                    "owner_founder": f_name or "not found",
                    "owner_founder_role": f_role or "",
                    "owner_founder_confidence": f_conf,
                    "employees": e_val or "not found",
                    "employees_confidence": e_conf,
                    "company_type": c_type,
                    "company_type_confidence": c_conf,
                    "company_type_evidence": c_evidence,
                    "phone": place.phone or "",
                    "google_category": place.category or "",
                    "found_via_term": entry.get("term", ""),
                    "found_via_area": entry.get("area", ""),
                    "notes": "; ".join(notes),
                }
            )

        if rows_out:
            await Actor.push_data(rows_out)

        filled = lambda k: sum(1 for r in rows_out if r[k] not in ("", None, "not found"))
        logger.info(
            "Done. %s companies | founder %s | employees %s | type %s",
            len(rows_out), filled("owner_founder"), filled("employees"), filled("company_type"),
        )
        logger.info(
            "SERP usage: %s requests (budget %s), %s blocked, %s failed",
            stats.requests, stats.budget or "unlimited", stats.blocked, stats.failed,
        )
        by_cat: dict[str, int] = {}
        for r in rows_out:
            key = r.get("category_label") or "uncategorised"
            by_cat[key] = by_cat.get(key, 0) + 1
        if by_cat:
            logger.info(
                "By sector: %s",
                ", ".join(f"{k}={v}" for k, v in sorted(by_cat.items(), key=lambda x: -x[1])),
            )
        if stats.dumps:
            logger.info("Debug HTML saved to key-value store: %s", ", ".join(stats.dumps))
        if stats.blocked > stats.requests * 0.3 and stats.requests > 5:
            logger.warning(
                "High block rate. Lower concurrency, raise the delay, or re-check "
                "the local-result selectors against a saved HTML dump."
            )
