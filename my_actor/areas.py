"""Sub-area discovery for any location, via OpenStreetMap.

Why this module exists
----------------------
Google's local finder caps out near 100 results per query, so a single
city-wide sweep plateaus regardless of how deep you paginate. Breaking that
ceiling means running the same search terms once per locality.

The first version shipped a hardcoded KOLKATA_AREAS list, which obviously does
not generalise. The obvious alternative -- learning locality names from the
addresses Google returns -- does not work either: local results truncate the
address to a building fragment ("6th Floor, PTI Building", "CN-8/2",
"Street Number 4") with no locality and no postcode. There is nothing to
harvest.

So areas are fetched from OpenStreetMap's Overpass API, which can enumerate
`place=suburb|neighbourhood|town` nodes inside a named administrative area for
anywhere on earth. One HTTP request per run, no API key.

Overpass is a free, volunteer-run service: it rate-limits, occasionally returns
504, and is not guaranteed. Every failure path here degrades to something
usable rather than aborting the run.
"""

from __future__ import annotations

import asyncio
import json
import logging
from urllib.parse import quote

logger = logging.getLogger(__name__)

OVERPASS_ENDPOINTS = (
    "https://overpass-api.de/api/interpreter",
    "https://overpass.kumi.systems/api/interpreter",
)

# Ordered by how likely each is to be a useful search term. Suburbs first:
# "software company in Behala, Kolkata" works; "software company in
# <tiny hamlet>" mostly returns the city-wide set again.
PLACE_RANKS = {
    "city_district": 0,
    "borough": 1,
    "suburb": 2,
    "quarter": 3,
    "neighbourhood": 4,
    "town": 5,
    "village": 6,
}


# Overpass charges by work done, and two things make a query expensive enough
# to hit a 504 gateway timeout:
#   * a REGEX on `place` -- Overpass cannot use its tag index for regex, so it
#     scans every node in the area instead of looking them up
#   * an over-broad area lookup
# So each place kind gets its own exact-match statement, which is indexed.
#
# Also note `boundary=administrative`: the first version of this used
# `boundary=admin`, which is not a real OSM tag value and matched nothing.
QUERY_TIERS = (
    # Tier 1: the useful place kinds inside the admin boundary.
    ("suburb", "neighbourhood", "city_district", "quarter", "borough", "town"),
    # Tier 2: cheapest possible -- just suburbs and neighbourhoods.
    ("suburb", "neighbourhood"),
)


def build_overpass_query(city: str, kinds: tuple[str, ...] | None = None,
                         timeout: int = 90, limit: int = 300) -> str:
    """Overpass QL listing place nodes inside a named city's admin boundary.

    Uses one indexed exact-match statement per place kind rather than a single
    regex, which is the difference between a fast lookup and a 504.
    """
    # Use only the first comma-segment: OSM boundaries are named "Kolkata",
    # not "Kolkata, India", so passing the full input string matches nothing.
    safe = city.split(",")[0].strip().replace('"', '\\"')
    kinds = kinds or QUERY_TIERS[0]
    stmts = "\n".join(
        f'  node["place"="{k}"](area.searchArea);' for k in kinds
    )
    return (
        f"[out:json][timeout:{timeout}];\n"
        f'area["name"="{safe}"]["boundary"="administrative"]->.searchArea;\n'
        f"(\n{stmts}\n);\n"
        f"out tags {limit};"
    )


def parse_overpass(payload: str | dict, city: str, limit: int = 40) -> list[str]:
    """Turn an Overpass response into search-ready area strings.

    Returns e.g. ["Behala, Kolkata", "New Town, Kolkata", ...] ordered by
    place rank so the densest localities are swept first -- if the run is cut
    short by a timeout, the most valuable areas are already done.
    """
    if isinstance(payload, str):
        try:
            payload = json.loads(payload)
        except (ValueError, TypeError):
            return []
    elements = (payload or {}).get("elements") or []

    seen: set[str] = set()
    scored: list[tuple[int, str]] = []
    city_low = city.split(",")[0].strip().lower()

    for el in elements:
        tags = el.get("tags") or {}
        # Prefer the English name: search queries are issued with hl=en, and a
        # Bengali/Devanagari name returns a different (usually empty) result set.
        name = (tags.get("name:en") or tags.get("name") or "").strip()
        if not name or len(name) < 3 or len(name) > 40:
            continue
        low = name.lower()
        if low == city_low or low in seen:
            continue
        # Skip names that are just the city plus a qualifier; they duplicate the
        # city-wide query.
        if low.replace(city_low, "").strip(" -,") == "":
            continue
        seen.add(low)
        rank = PLACE_RANKS.get(tags.get("place", ""), 9)
        scored.append((rank, name))

    scored.sort(key=lambda x: (x[0], x[1]))
    city_suffix = city.split(",")[0].strip()
    out = []
    for _, name in scored[:limit]:
        # "Behala" -> "Behala, Kolkata" so the query stays unambiguous.
        out.append(name if city_suffix.lower() in name.lower() else f"{name}, {city_suffix}")
    return out


async def discover_areas(
    session, city: str, *, limit: int = 40, timeout: int = 100
) -> list[str]:
    """Fetch sub-areas for `city`. Returns [] on any failure.

    Overpass is free and volunteer-run: 504 gateway timeouts are common and
    usually mean "too expensive, try smaller" rather than "broken". So this
    walks down QUERY_TIERS (progressively cheaper queries) across both
    mirrors, with a short backoff between attempts.
    """
    attempts: list[tuple[str, tuple[str, ...], int, int]] = []
    for endpoint in OVERPASS_ENDPOINTS:
        for tier_no, kinds in enumerate(QUERY_TIERS):
            attempts.append((endpoint, kinds, 90 if tier_no == 0 else 45,
                             300 if tier_no == 0 else 150))

    last = ""
    for i, (endpoint, kinds, qtimeout, qlimit) in enumerate(attempts, 1):
        query = build_overpass_query(city, kinds, timeout=qtimeout, limit=qlimit)
        try:
            async with session.post(
                endpoint,
                data=f"data={quote(query)}",
                headers={
                    "Content-Type": "application/x-www-form-urlencoded",
                    # Overpass asks for a contactable UA; be a good citizen.
                    "User-Agent": "apify-it-company-finder/3.0",
                },
                timeout=timeout,
            ) as resp:
                if resp.status in (429, 504, 503):
                    last = f"HTTP {resp.status}"
                    logger.warning(
                        "Overpass attempt %s/%s: %s from %s with %s place kinds "
                        "(504 usually means the query was too heavy; retrying smaller)",
                        i, len(attempts), last, endpoint.split("/")[2], len(kinds),
                    )
                    await asyncio.sleep(3 * i)
                    continue
                if resp.status != 200:
                    last = f"HTTP {resp.status}"
                    logger.warning("Overpass attempt %s/%s: %s", i, len(attempts), last)
                    continue
                body = await resp.text()
        except Exception as exc:
            last = f"{type(exc).__name__}: {exc or '(no message)'}"
            logger.warning("Overpass attempt %s/%s failed -- %s", i, len(attempts), last)
            await asyncio.sleep(2 * i)
            continue

        areas = parse_overpass(body, city, limit=limit)
        if areas:
            logger.info(
                "Discovered %s sub-areas of %r from OpenStreetMap: %s%s",
                len(areas), city, ", ".join(areas[:6]),
                " ..." if len(areas) > 6 else "",
            )
            return areas
        last = "no usable places in response"
        logger.warning("Overpass attempt %s/%s: %s", i, len(attempts), last)

    logger.warning(
        "Area auto-discovery failed after %s attempts (last: %s). Supply an "
        "`areas` list in the input to sweep localities explicitly.",
        len(attempts), last,
    )
    return []


# Retained as a fallback and for offline runs. No longer the primary mechanism:
# pass `areas` explicitly, or let discover_areas() handle any city.
KOLKATA_AREAS = [
    "Salt Lake Sector V, Kolkata", "Salt Lake Sector 1, Kolkata",
    "Salt Lake Sector 2, Kolkata", "Salt Lake Sector 3, Kolkata",
    "New Town, Kolkata", "Rajarhat, Kolkata", "Baguiati, Kolkata",
    "Kestopur, Kolkata", "Lake Town, Kolkata", "Dum Dum, Kolkata",
    "Nagerbazar, Kolkata", "Sodepur, Kolkata", "Barrackpore, Kolkata",
    "Shyambazar, Kolkata", "Bagbazar, Kolkata", "Ultadanga, Kolkata",
    "Manicktala, Kolkata", "Beleghata, Kolkata", "Sealdah, Kolkata",
    "Park Street, Kolkata", "Esplanade, Kolkata", "BBD Bagh, Kolkata",
    "Burrabazar, Kolkata", "Entally, Kolkata", "Topsia, Kolkata",
    "Tangra, Kolkata", "Kasba, Kolkata", "Ballygunge, Kolkata",
    "Gariahat, Kolkata", "Bhowanipore, Kolkata", "Alipore, Kolkata",
    "New Alipore, Kolkata", "Behala, Kolkata", "Thakurpukur, Kolkata",
    "Tollygunge, Kolkata", "Jadavpur, Kolkata", "Santoshpur, Kolkata",
    "Garia, Kolkata", "Narendrapur, Kolkata", "Sonarpur, Kolkata",
    "Bansdroni, Kolkata", "Taratala, Kolkata", "Howrah",
    "Shibpur, Howrah", "Bally, Howrah", "Liluah, Howrah",
]
