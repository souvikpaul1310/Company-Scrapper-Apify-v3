"""Google SERP access through Apify's GOOGLE_SERP proxy group.

This module is the reason the actor needs no paid Store actors. The
GOOGLE_SERP proxy group is included on every Apify plan (Creator included),
and returns the raw HTML of a Google results page.

Two constraints imposed by the proxy, both enforced here:
  1. Only plain HTTP is allowed (not HTTPS).
  2. The hostname must start with "www.".

Because we parse Google's own HTML, selectors are inherently fragile: Google
rotates class names regularly. Every parser here therefore uses several
independent strategies and falls back to text-level regex, and `dump_html`
lets you snapshot a page to the key-value store when something breaks.
"""

from __future__ import annotations

import asyncio
import logging
import random
import re
from dataclasses import dataclass, field
from urllib.parse import quote_plus, urlparse, parse_qs, unquote

import aiohttp
from bs4 import BeautifulSoup

logger = logging.getLogger(__name__)

PROXY_HOST = "proxy.apify.com"
PROXY_PORT = 8000

# Country code -> Google domain. The proxy only applies the country if the
# hostname (minus www.) starts with "google", and the domain must match the
# country you asked for or results come back for the wrong locale.
GOOGLE_DOMAINS = {
    "IN": "www.google.co.in",
    "US": "www.google.com",
    "GB": "www.google.co.uk",
    "AU": "www.google.com.au",
    "CA": "www.google.ca",
    "DE": "www.google.de",
    "SG": "www.google.com.sg",
    "AE": "www.google.ae",
}

USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/121.0 Safari/537.36",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0 Safari/537.36",
]

CONSENT_MARKERS = ("consent.google", "Before you continue", "id=\"CXQnmb\"")
BLOCK_MARKERS = ("Our systems have detected unusual traffic", "/sorry/index", "captcha")


@dataclass
class LocalResult:
    """One business from Google's local finder (tbm=lcl)."""

    name: str = ""
    address: str = ""
    rating: float | None = None
    reviews: int | None = None
    category: str = ""
    website: str = ""
    phone: str = ""


@dataclass
class OrganicResult:
    title: str = ""
    url: str = ""
    snippet: str = ""


@dataclass
class SerpStats:
    """Tracks SERP consumption so runs stay inside the plan's monthly cap."""

    requests: int = 0
    blocked: int = 0
    failed: int = 0
    budget: int = 0
    dumps: list[str] = field(default_factory=list)

    @property
    def exhausted(self) -> bool:
        return self.budget > 0 and self.requests >= self.budget


class SerpClient:
    """Fetches and parses Google result pages via the GOOGLE_SERP proxy."""

    def __init__(
        self,
        proxy_password: str,
        *,
        country: str = "IN",
        timeout: int = 30,
        budget: int = 0,
        min_delay: float = 1.0,
        max_delay: float = 2.5,
        store=None,
    ) -> None:
        self.country = (country or "IN").upper()
        self.domain = GOOGLE_DOMAINS.get(self.country, "www.google.com")
        self.timeout = aiohttp.ClientTimeout(total=timeout)
        self.min_delay = min_delay
        self.max_delay = max_delay
        self.stats = SerpStats(budget=budget)
        self._store = store
        self._session: aiohttp.ClientSession | None = None

        # Google SERP proxy username carries the params; there is no session
        # parameter for this group.
        username = f"groups-GOOGLE_SERP,country-{self.country}"
        self._proxy_url = f"http://{username}:{proxy_password}@{PROXY_HOST}:{PROXY_PORT}"

    async def __aenter__(self) -> "SerpClient":
        self._session = aiohttp.ClientSession(timeout=self.timeout)
        return self

    async def __aexit__(self, *exc) -> None:
        if self._session:
            await self._session.close()

    # ---------------------------------------------------------------- fetching

    async def _get(self, url: str, *, label: str = "") -> str | None:
        if self.stats.exhausted:
            logger.warning("SERP budget of %s reached; skipping %s", self.stats.budget, label or url)
            return None
        if not self._session:
            raise RuntimeError("SerpClient must be used as an async context manager")

        # Politeness delay. Google SERP proxy rotates IPs per request, but
        # hammering it still raises the block rate.
        await asyncio.sleep(random.uniform(self.min_delay, self.max_delay))

        headers = {
            "User-Agent": random.choice(USER_AGENTS),
            "Accept": "text/html,application/xhtml+xml",
            "Accept-Language": "en-US,en;q=0.9",
        }

        for attempt in (1, 2, 3):
            try:
                self.stats.requests += 1
                async with self._session.get(
                    url, proxy=self._proxy_url, headers=headers, allow_redirects=True
                ) as resp:
                    body = await resp.text(errors="ignore")

                    if resp.status == 429 or any(m in body for m in BLOCK_MARKERS):
                        self.stats.blocked += 1
                        logger.warning("SERP blocked (attempt %s) for %s", attempt, label or url)
                        await asyncio.sleep(3 * attempt)
                        continue
                    if resp.status >= 400:
                        logger.warning("SERP HTTP %s for %s", resp.status, label or url)
                        await asyncio.sleep(2 * attempt)
                        continue
                    if any(m in body for m in CONSENT_MARKERS):
                        # Consent interstitial: retry usually lands on a
                        # different IP that doesn't require it.
                        logger.info("Consent page returned for %s; retrying", label or url)
                        await asyncio.sleep(2)
                        continue

                    return body

            except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
                logger.warning("SERP fetch error (attempt %s) for %s: %s", attempt, label or url, exc)
                await asyncio.sleep(2 * attempt)

        self.stats.failed += 1
        return None

    async def dump_html(self, key: str, html: str) -> None:
        """Snapshot a page to the key-value store for selector debugging."""
        if not self._store or not html:
            return
        try:
            await self._store.set_value(key, html, content_type="text/html")
            self.stats.dumps.append(key)
        except Exception as exc:  # pragma: no cover - storage is best-effort
            logger.debug("Could not dump %s: %s", key, exc)

    # ------------------------------------------------------------ local finder

    async def local_search(self, query: str, *, start: int = 0) -> tuple[list[LocalResult], str]:
        """Google local finder page. Returns (results, raw_html).

        `tbm=lcl` is the "Places"/local tab. It returns roughly 20 businesses
        per page with rating and review count inline, and paginates by 20 via
        the `start` parameter -- far denser than the 3-result local pack on a
        normal search page.
        """
        url = (
            f"http://{self.domain}/search?q={quote_plus(query)}"
            f"&tbm=lcl&hl=en&gl={self.country.lower()}&start={start}"
        )
        html = await self._get(url, label=f"lcl:{query}@{start}")
        if not html:
            return [], ""
        return parse_local_results(html), html

    async def organic_search(self, query: str, *, num: int = 10) -> tuple[list[OrganicResult], str]:
        url = (
            f"http://{self.domain}/search?q={quote_plus(query)}"
            f"&hl=en&gl={self.country.lower()}&num={num}"
        )
        html = await self._get(url, label=f"web:{query}")
        if not html:
            return [], ""
        return parse_organic_results(html), html


# ------------------------------------------------------------------ parsers

# "4.5(128)" / "4.5 (128)" / "4,5 · 128 reviews"
RATING_REVIEWS = re.compile(
    r"(\d[.,]\d)\s*(?:\u00b7|\||\s)?\s*\(?\s*([\d,\.]+)\s*\)?\s*(?:reviews?|ratings?)?",
    re.I,
)
REVIEWS_ONLY = re.compile(r"([\d,\.]+)\s*(?:reviews?|ratings?)", re.I)
RATING_ONLY = re.compile(r"\b([1-5][.,]\d)\b")
PHONE_RE = re.compile(r"(?:\+91[\s-]?)?(?:\d[\s-]?){9,13}")


def _to_int(raw: str) -> int | None:
    digits = re.sub(r"[^\d]", "", raw or "")
    return int(digits) if digits else None


def _to_float(raw: str) -> float | None:
    try:
        return float((raw or "").replace(",", "."))
    except ValueError:
        return None


# Link/CTA text that Google appends after the address in the block's text.
TRAILING_JUNK = re.compile(
    r"\s*\b(?:website|site|directions|call|menu|order online|book online|"
    r"reserve a table|open now|closed|opens|closes|share|save)\b.*$",
    re.I,
)


def _trim_address(part: str) -> str:
    """Strip trailing phone numbers and link labels off an address segment."""
    cleaned = TRAILING_JUNK.sub("", part or "").strip()
    # Cut at a phone number if one got concatenated on.
    pm = PHONE_RE.search(cleaned)
    if pm and len(re.sub(r"\D", "", pm.group(0))) >= 10 and pm.start() > 10:
        cleaned = cleaned[: pm.start()].strip()
    return cleaned.strip(" ,-–—·|")


def _clean_url(href: str) -> str:
    """Unwrap Google's /url?q= redirector and drop tracking params."""
    if not href:
        return ""
    if href.startswith("/url?") or href.startswith("/aclk?"):
        qs = parse_qs(urlparse(href).query)
        for key in ("q", "url"):
            if key in qs and qs[key]:
                href = unquote(qs[key][0])
                break
    if not href.startswith("http"):
        return ""
    parsed = urlparse(href)
    host = (parsed.netloc or "").lower()
    # Google's own properties are never the company's website.
    if any(bad in host for bad in ("google.", "gstatic.", "googleusercontent.")):
        return ""
    return f"{parsed.scheme}://{parsed.netloc}{parsed.path}".rstrip("/")


def parse_local_results(html: str) -> list[LocalResult]:
    """Extract businesses from a tbm=lcl page.

    Google gives each business a container with a data-cid attribute in most
    layouts. We try that first, then fall back to broader heuristics, because
    the markup varies by locale and rolls over without notice.
    """
    soup = BeautifulSoup(html, "lxml")
    results: list[LocalResult] = []
    seen: set[str] = set()

    # Strategy 1: containers carrying a business id.
    blocks = soup.select("[data-cid], div.VkpGBb, div.uMdZh, div.rllt__details")

    # Strategy 2: if nothing matched, treat each local-title heading's
    # grandparent as a block.
    if not blocks:
        titles = soup.select("div.dbg0pd, div.rllt__details div:first-child, span.OSrXXb")
        blocks = [t.find_parent("div").find_parent("div") for t in titles if t.find_parent("div")]
        blocks = [b for b in blocks if b is not None]

    for block in blocks:
        text = block.get_text(" ", strip=True)
        if not text or len(text) < 4:
            continue

        item = LocalResult()

        # --- name
        name_el = block.select_one("div.dbg0pd, span.OSrXXb, div.qBF1Pd, a.vwVdIc, h3")
        if name_el:
            item.name = name_el.get_text(" ", strip=True)
        else:
            # First reasonably short line of the block is usually the name.
            first = text.split("·")[0].strip()
            item.name = first[:120]
        item.name = re.sub(r"^\d+\.\s*", "", item.name).strip()
        if not item.name:
            continue

        key = item.name.lower()
        if key in seen:
            continue
        seen.add(key)

        # --- rating + reviews. Prefer aria-labels, which are more stable
        # than class names, then fall back to text.
        aria = " ".join(
            el.get("aria-label", "")
            for el in block.select("[aria-label]")
            if "star" in el.get("aria-label", "").lower()
            or "review" in el.get("aria-label", "").lower()
        )
        probe = aria if aria else text

        m = RATING_REVIEWS.search(probe)
        if m:
            item.rating = _to_float(m.group(1))
            item.reviews = _to_int(m.group(2))
        else:
            rm = RATING_ONLY.search(probe)
            if rm:
                item.rating = _to_float(rm.group(1))
            vm = REVIEWS_ONLY.search(probe)
            if vm:
                item.reviews = _to_int(vm.group(1))

        # Sanity: ratings are 1-5; anything else is a misparse.
        if item.rating is not None and not (0.0 < item.rating <= 5.0):
            item.rating = None

        # --- category and address live in the "·"-separated detail lines.
        # Consider every segment (not just those after the first), because the
        # name may have come from an element rather than the leading segment.
        parts = [p.strip() for p in re.split(r"\u00b7|\|", text) if p.strip()]
        name_low = item.name.lower()
        for part in parts:
            # The leading segment often reads "<Name> <Category>" because the
            # name element sits inside the same text node.
            if part.lower().startswith(name_low):
                part = part[len(item.name):].strip(" ,-–—·|")
                if not part:
                    continue
            low = part.lower()
            if low == name_low or low in name_low:
                continue
            if re.search(r"\d", part) and any(
                token in low for token in ("road", "rd", "street", "st", "block", "sector",
                                           "floor", "tower", "lane", "nagar", "park", "kolkata",
                                           "howrah", "pin", "7000")
            ):
                candidate = _trim_address(part)
                if len(candidate) > len(item.address):
                    item.address = candidate
            elif not item.category and re.fullmatch(r"[A-Za-z /&'-]{3,40}", part):
                if not RATING_ONLY.search(part) and "review" not in low:
                    item.category = part

        # --- website / phone
        for a in block.select("a[href]"):
            url = _clean_url(a.get("href", ""))
            if url:
                item.website = url
                break
        pm = PHONE_RE.search(text)
        if pm and len(re.sub(r"\D", "", pm.group(0))) >= 10:
            item.phone = pm.group(0).strip()

        results.append(item)

    return results


def parse_organic_results(html: str) -> list[OrganicResult]:
    """Extract organic web results (title, url, snippet)."""
    soup = BeautifulSoup(html, "lxml")
    out: list[OrganicResult] = []
    seen: set[str] = set()

    for block in soup.select("div.g, div.tF2Cxc, div.MjjYud, div.Gx5Zad"):
        link = block.select_one("a[href]")
        if not link:
            continue
        url = _clean_url(link.get("href", ""))
        if not url or url in seen:
            continue

        title_el = block.select_one("h3, div[role='heading']")
        title = title_el.get_text(" ", strip=True) if title_el else ""

        snippet = ""
        for sel in ("div.VwiC3b", "div.IsZvec", "span.aCOpRe", "div.s3v9rd", "div.lEBKkf"):
            el = block.select_one(sel)
            if el:
                snippet = el.get_text(" ", strip=True)
                break
        if not snippet:
            # Fall back to block text minus the title.
            body = block.get_text(" ", strip=True)
            snippet = body.replace(title, "", 1).strip()[:400]

        if not title and not snippet:
            continue
        seen.add(url)
        out.append(OrganicResult(title=title, url=url, snippet=snippet))

    return out
