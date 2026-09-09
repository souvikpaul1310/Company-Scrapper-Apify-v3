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


def build_overpass_query(city: str, radius_km: int = 25) -> str:
    """Overpass QL listing place nodes in and around a named city.

    Uses `area[name=...]` when the city is a mapped admin boundary, and falls
    back to a radius around the city node, because plenty of places are mapped
    as nodes without a boundary relation.
    """
    # Use only the first comma-segment: OSM boundaries are named "Kolkata",
    # not "Kolkata, India", so passing the full input string matches nothing.
    safe = city.split(",")[0].strip().replace('"', '\\"')
    kinds = "|".join(PLACE_RANKS)
    return f"""
[out:json][timeout:60];
(
  area[name="{safe}"][boundary=admin]->.a;
  node(area.a)[place~"^({kinds})$"];
);
out tags 400;
""".strip()


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
    session, city: str, *, limit: int = 40, timeout: int = 60
) -> list[str]:
    """Fetch sub-areas for `city`. Returns [] on any failure."""
    query = build_overpass_query(city)
    for endpoint in OVERPASS_ENDPOINTS:
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
                if resp.status != 200:
                    logger.warning("Overpass %s returned HTTP %s", endpoint, resp.status)
                    continue
                body = await resp.text()
        except Exception as exc:
            logger.warning("Overpass %s failed: %s", endpoint, exc)
            continue

        areas = parse_overpass(body, city, limit=limit)
        if areas:
            logger.info(
                "Discovered %s sub-areas of %r from OpenStreetMap: %s%s",
                len(areas), city, ", ".join(areas[:6]),
                " ..." if len(areas) > 6 else "",
            )
            return areas
        logger.warning("Overpass returned no usable places for %r", city)
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
