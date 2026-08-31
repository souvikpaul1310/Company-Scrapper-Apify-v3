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
from dataclasses import asdict
from urllib.parse import urlparse

from apify import Actor, Event

from .classify import classify_company_type
from .people import extract_employees, extract_founder
from .serp import LocalResult, SerpClient
from .taxonomy import (
    CATEGORY_LABELS,
    DEFAULT_CATEGORIES,
    KOLKATA_AREAS,
    categorise,
    resolve_category,
    search_terms_for,
)
from .website import WebsiteData, enrich_many

logger = logging.getLogger(__name__)

# Apify migrates long-running actors between hosts without warning; when that
# happens the container restarts and main() runs again from the top. Discovery
# is the expensive stage (hours of SERP requests), so it is checkpointed to the
# key-value store and resumed rather than repeated.
STATE_KEY = "STATE-discovery.json"
CHECKPOINT_EVERY = 10  # queries
PUSH_BATCH = 25        # rows per push_data call


def _serialise_state(discovered: dict, alias: dict, done: set) -> dict:
    return {
        "discovered": {
            key: {
                "place": asdict(val["place"]),
                "term": val.get("term", ""),
                "area": val.get("area", ""),
                "category": val.get("category", ""),
            }
            for key, val in discovered.items()
        },
        "alias": alias,
        "done": sorted(f"{a}\t{t}" for a, t in done),
    }


def _deserialise_state(payload: dict) -> tuple[dict, dict, set]:
    discovered: dict = {}
    for key, val in (payload.get("discovered") or {}).items():
        discovered[key] = {
            "place": LocalResult(**val["place"]),
            "term": val.get("term", ""),
            "area": val.get("area", ""),
            "category": val.get("category", ""),
        }
    alias = payload.get("alias") or {}
    done = set()
    for row in payload.get("done") or []:
        area, _, term = row.partition("\t")
        if term:
            done.add((area, term))
    return discovered, alias, done


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


def _build_row(entry: dict, wd) -> dict:
    """Assemble one output row from a discovery entry plus website data."""
    place = entry["place"]
    f_name, f_role, f_conf, f_note = entry.get("founder", ("", "", "not found", ""))
    e_val, e_conf, e_note = entry.get("employees", ("", "not found", ""))

    if wd and wd.company_type:
        c_type, c_conf, c_evidence = wd.company_type, wd.type_confidence, wd.type_evidence
    else:
        c_type, c_conf, c_evidence = "not found", "not found", ""

    notes = [n for n in (f_note, e_note) if n]
    if wd and not wd.reachable:
        notes.append("website did not respond")
    if not entry.get("enriched"):
        notes.append("not SERP-enriched (cap reached or run ended)")

    return {
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


async def main() -> None:
    async with Actor:
        # Route this package's loggers through Apify's handler at INFO level.
        # Without this, `logging.getLogger(__name__)` inherits the root logger's
        # default WARNING level and every progress line is silently dropped --
        # the run looks frozen even while it is working normally.
        pkg_logger = logging.getLogger("my_actor")
        pkg_logger.setLevel(logging.INFO)
        if not pkg_logger.handlers:
            for handler in Actor.log.handlers:
                pkg_logger.addHandler(handler)
            if not pkg_logger.handlers:  # fallback if the SDK exposed none
                logging.basicConfig(level=logging.INFO)
        pkg_logger.propagate = not pkg_logger.handlers

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
        # Two very different timeouts, deliberately separate inputs:
        #   req_timeout  - crawling company websites. Ordinary HTTP; 20s is ample.
        #   serp_timeout - fetching via Apify's GOOGLE_SERP proxy, which scrapes
        #                  Google server-side and routinely needs 30-90s. Capping
        #                  this at 20s fails every single request with an empty
        #                  asyncio.TimeoutError.
        req_timeout = max(5, int(cfg.get("requestTimeoutSecs") or 20))
        serp_timeout = max(30, int(cfg.get("serpRequestTimeoutSecs") or 120))

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

        store = await Actor.open_key_value_store()

        # Apify sends MIGRATING shortly before moving the container. Persist
        # whatever discovery has found so the replacement container resumes
        # instead of starting over. NOTE: a migration does NOT reset the run
        # timeout -- the clock keeps running from the original start.
        migration_state: dict = {"discovered": None, "alias": None, "done": None}

        async def _on_migrating(_event=None) -> None:
            try:
                if migration_state["discovered"] is not None:
                    await store.set_value(
                        STATE_KEY,
                        _serialise_state(
                            migration_state["discovered"],
                            migration_state["alias"],
                            migration_state["done"],
                        ),
                    )
                    logger.warning(
                        "Migration signalled: checkpointed %s companies before handover.",
                        len(migration_state["discovered"]),
                    )
            except Exception as exc:
                logger.warning("Could not checkpoint on migration: %s", exc)

        try:
            Actor.on(Event.MIGRATING, _on_migrating)
        except Exception as exc:  # pragma: no cover - event API is best-effort
            logger.debug("Could not register migration handler: %s", exc)

        logger.info(
            "Config: location=%r | %s areas | %s terms | %s pages/term | "
            "serpBudget=%s | website=%s founderSerp=%s employeeSerp=%s",
            location, len(areas), len(terms), max_pages, serp_budget or "unlimited",
            do_website, do_serp_founder, do_serp_employees,
        )
        est = len(areas) * len(terms) * max_pages
        logger.info(
            "Worst-case discovery requests: %s. At ~2s politeness delay plus fetch "
            "time that is roughly %s-%s minutes for discovery alone.",
            est, est * 3 // 60, est * 6 // 60,
        )
        if est * 4 > 280:
            logger.warning(
                "This configuration cannot finish inside the Apify default 300s "
                "timeout. Set Input > Run options > Timeout to at least %s seconds, "
                "or reduce maxLocalPagesPerTerm / areas / searchTerms.",
                max(3600, est * 10),
            )
        if req_timeout > 120:
            logger.warning(
                "requestTimeoutSecs=%s is the PER-REQUEST timeout, not the run "
                "timeout. A value this high lets one hung server stall the run; 20-30 "
                "is normal. The run timeout lives in Input > Run options.",
                req_timeout,
            )
        if serp_timeout < 60:
            logger.warning(
                "serpRequestTimeoutSecs=%s is likely too low. Apify's GOOGLE_SERP "
                "proxy scrapes server-side and a fetch often needs 30-90s; short "
                "timeouts fail every request with an empty TimeoutError.",
                serp_timeout,
            )
        if min_rating is not None:
            logger.warning(
                "minRating=%s drops every company with NO Google rating at all, which "
                "is most small and new firms. Leave it empty to keep unrated companies.",
                min_rating,
            )

        # ---------------------------------------------------- Stage 1: discover
        discovered: dict[str, dict] = {}
        alias: dict[str, str] = {}   # every identity key -> canonical record key
        done_queries: set[tuple[str, str]] = set()
        reject_tally: dict[str, int] = {}

        # Resume from a checkpoint if a previous attempt was migrated mid-run.
        if cfg.get("resumeFromCheckpoint", True):
            try:
                saved = await store.get_value(STATE_KEY)
            except Exception:
                saved = None
            if saved:
                try:
                    discovered, alias, done_queries = _deserialise_state(saved)
                    logger.info(
                        "Resumed from checkpoint: %s companies already found, "
                        "%s of the query plan already done.",
                        len(discovered), len(done_queries),
                    )
                except Exception as exc:
                    logger.warning("Checkpoint unreadable (%s); starting fresh.", exc)
                    discovered, alias, done_queries = {}, {}, set()

        migration_state["discovered"] = discovered
        migration_state["alias"] = alias
        migration_state["done"] = done_queries

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
            timeout=serp_timeout,
            budget=serp_budget,
            store=store,
        ) as serp:
            completed_this_run = 0
            for area, term in plan:
                if serp.stats.exhausted or (target and len(discovered) >= target):
                    break
                if (area, term) in done_queries:
                    continue
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
                                # Keep the whole reason up to the parenthetical
                                # detail: splitting on the first space turned
                                # "no sector signal" into a useless "no".
                                bucket = (reason or "unknown").split(" (")[0]
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

                done_queries.add((area, term))
                completed_this_run += 1
                if completed_this_run % CHECKPOINT_EVERY == 0:
                    try:
                        await store.set_value(
                            STATE_KEY, _serialise_state(discovered, alias, done_queries)
                        )
                        logger.info(
                            "Checkpoint saved (%s companies, %s/%s queries done)",
                            len(discovered), len(done_queries), len(plan),
                        )
                    except Exception as exc:
                        logger.warning("Could not save checkpoint: %s", exc)

            try:
                await store.set_value(
                    STATE_KEY, _serialise_state(discovered, alias, done_queries)
                )
            except Exception:
                pass

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
                # Use enrich_many, not enrich_from_website: it owns the
                # aiohttp session, bounds concurrency, and isolates per-site
                # failures. Calling enrich_from_website directly means passing
                # a session yourself, and its timeout kwarg is `timeout` --
                # getting either wrong fails every single site.
                sites = [e["place"].website or "" for e in selected]
                results = await enrich_many(
                    sites,
                    concurrency=concurrency,
                    timeout=req_timeout,
                    max_pages=pages_per_site,
                    log=None,
                )
                reachable = 0
                for entry, wd in zip(selected, results):
                    website_data[entry["place"].name] = wd
                    if wd.reachable:
                        reachable += 1
                logger.info(
                    "Crawled %s sites, %s reachable, %s founders named on-site",
                    len(sites), reachable,
                    sum(1 for w in results if w.founder_name),
                )

            # ------------------------------------ Stage 3: SERP enrichment
            # Enrichment costs ~2 SERP requests per company, each up to
            # serpRequestTimeoutSecs. For 600+ companies that is 10+ hours, so
            # it is capped and prioritised: the companies with the most Google
            # reviews are the ones a lead list actually cares about.
            enrich_cap = int(cfg.get("maxEnrichCompanies") or 0)
            order = sorted(
                selected,
                key=lambda e: (e["place"].reviews or 0, e["place"].rating or 0),
                reverse=True,
            )
            to_enrich = order[:enrich_cap] if enrich_cap else order
            if enrich_cap and len(order) > enrich_cap:
                logger.info(
                    "Enriching the top %s of %s companies by review count "
                    "(maxEnrichCompanies). The rest keep fields 1-5 plus website data.",
                    len(to_enrich), len(order),
                )

            # Rows are pushed to the dataset in batches AS THEY COMPLETE, not
            # at the end. A previous run spent four hours on discovery and
            # enrichment, hit the run timeout mid-enrichment, and saved nothing
            # at all -- push_data only ran after every company was finished.
            # Partial output beats an empty dataset.
            pending: list[dict] = []
            pushed = 0

            async def flush(force: bool = False) -> None:
                nonlocal pending, pushed
                if pending and (force or len(pending) >= PUSH_BATCH):
                    batch, pending = pending, []
                    try:
                        await Actor.push_data(batch)
                        pushed += len(batch)
                        logger.info("Pushed %s rows (total %s)", len(batch), pushed)
                    except Exception as exc:
                        logger.warning("push_data failed (%s); re-queueing", exc)
                        pending = batch + pending

            for i, entry in enumerate(to_enrich, 1):
                place = entry["place"]
                wd = website_data.get(place.name)

                founder = ("", "", "not found", "")
                employees = ("", "not found", "")

                # Prefer what the company says about itself.
                if wd and wd.founder_name:
                    founder = (wd.founder_name, wd.founder_role or "", "high", "company website")

                # Fall back to a Google lookup only when the company's own site
                # did not name a founder.
                if do_serp_founder and not founder[0] and not serp.stats.exhausted:
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
                entry["enriched"] = True

                pending.append(_build_row(entry, wd))
                await flush()

                if i % 25 == 0:
                    logger.info(
                        "Enriched %s/%s (SERP used %s of %s)",
                        i, len(to_enrich), serp.stats.requests, serp_budget or "unlimited",
                    )

            await flush(force=True)
            stats = serp.stats

        # ------------------------------------------------ Stage 4: emit the rest
        # Everything not SERP-enriched still carries fields 1-5 plus whatever
        # the website crawl found, which is the bulk of the value.
        remainder = [e for e in selected if not e.get("enriched")]
        rows_out = []
        for entry in remainder:
            rows_out.append(_build_row(entry, website_data.get(entry["place"].name)))
        if rows_out:
            for i in range(0, len(rows_out), PUSH_BATCH):
                await Actor.push_data(rows_out[i:i + PUSH_BATCH])
            logger.info("Pushed %s un-enriched rows", len(rows_out))

        total_rows = pushed + len(rows_out)
        rows_out = [_build_row(e, website_data.get(e["place"].name)) for e in selected]

        filled = lambda k: sum(1 for r in rows_out if r[k] not in ("", None, "not found"))
        logger.info(
            "Done. %s rows saved | founder %s | employees %s | type %s",
            total_rows, filled("owner_founder"), filled("employees"), filled("company_type"),
        )
        logger.info(
            "SERP usage: %s requests (budget %s), %s blocked, %s timed out, %s failed",
            stats.requests, stats.budget or "unlimited", stats.blocked,
            stats.timeouts, stats.failed,
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
