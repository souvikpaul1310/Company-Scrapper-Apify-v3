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
from .website import WebsiteData, enrich_from_website

logger = logging.getLogger(__name__)

DEFAULT_TERMS = [
    "software company",
    "IT company",
    "software development company",
    "web development company",
    "IT consulting",
]

# Names that signal the listing is not an IT company at all. Google's local
# finder mixes training institutes, repair shops and staffing agencies into
# these queries constantly.
EXCLUDE_NAME = re.compile(
    r"\b(?:computer (?:training|centre|center|institute|repair|academy)|"
    r"training institute|coaching|tuition|placement|recruitment|staffing|"
    r"manpower|consultancy services|job|career|laptop repair|mobile repair|"
    r"cyber cafe|xerox|stationery|hardware store|electronics store)\b",
    re.I,
)

IT_SIGNAL = re.compile(
    r"\b(?:software|it|tech|technolog|infotech|digital|web|app|cloud|data|"
    r"cyber|system|solution|comput|develop|consult|analytic|ai|saas|erp)\w*",
    re.I,
)


def _domain(url: str) -> str:
    try:
        host = (urlparse(url).netloc or "").lower()
        return host[4:] if host.startswith("www.") else host
    except Exception:
        return ""


def _looks_like_it(name: str, category: str) -> bool:
    blob = f"{name} {category}"
    if EXCLUDE_NAME.search(blob):
        return False
    return bool(IT_SIGNAL.search(blob))


def _dedupe_key(name: str, website: str) -> str:
    """Domain is the most reliable identity key; fall back to a folded name."""
    dom = _domain(website)
    if dom:
        return f"d:{dom}"
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
    return f"n:{folded}"


async def main() -> None:
    async with Actor:
        cfg = await Actor.get_input() or {}

        location = (cfg.get("location") or "").strip()
        if not location:
            await Actor.fail(status_message="`location` is required (e.g. 'Kolkata, India').")
            return

        terms = [t.strip() for t in (cfg.get("searchTerms") or DEFAULT_TERMS) if t and t.strip()]
        country = (cfg.get("countryCode") or "IN").upper()
        max_pages = max(1, int(cfg.get("maxLocalPagesPerTerm") or 3))
        target = int(cfg.get("maxCompanies") or 100)
        serp_budget = int(cfg.get("serpBudget") or 500)

        it_filter = cfg.get("itFilter", True)
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

        async with SerpClient(
            password,
            country=country,
            timeout=req_timeout,
            budget=serp_budget,
            store=store,
        ) as serp:
            for term in terms:
                query = f"{term} in {location}"
                for page in range(max_pages):
                    if len(discovered) >= target or serp.stats.exhausted:
                        break

                    rows, html = await serp.local_search(query, start=page * 20)
                    if debug_dump and html and page == 0:
                        await serp.dump_html(
                            f"lcl-{re.sub(r'[^a-z0-9]+', '-', term.lower())}.html", html
                        )

                    if not rows:
                        logger.info("No local results for %r page %s; stopping this term", query, page)
                        break

                    kept = 0
                    for row in rows:
                        if it_filter and not _looks_like_it(row.name, row.category):
                            continue
                        key = _dedupe_key(row.name, row.website)
                        if key in discovered:
                            # Keep whichever copy has more information.
                            prev = discovered[key]["place"]
                            if not prev.website and row.website:
                                prev.website = row.website
                            if prev.rating is None and row.rating is not None:
                                prev.rating, prev.reviews = row.rating, row.reviews
                            continue
                        discovered[key] = {"place": row, "term": term}
                        kept += 1

                    logger.info(
                        "%r page %s: %s results, %s new (total %s)",
                        query, page, len(rows), kept, len(discovered),
                    )
                    if len(rows) < 5:
                        break  # thin page: pagination has run out

            logger.info("Discovery finished: %s unique companies", len(discovered))

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

            selected = [v for v in discovered.values() if passes(v["place"])][:target]
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
                    "category": place.category or "",
                    "search_term": entry.get("term", ""),
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
        if stats.dumps:
            logger.info("Debug HTML saved to key-value store: %s", ", ".join(stats.dumps))
        if stats.blocked > stats.requests * 0.3 and stats.requests > 5:
            logger.warning(
                "High block rate. Lower concurrency, raise the delay, or re-check "
                "the local-result selectors against a saved HTML dump."
            )
