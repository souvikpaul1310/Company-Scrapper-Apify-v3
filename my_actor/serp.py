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

CONSENT_MARKERS = ("consent.google", "Before you continue", 'id="CXQnmb"')

# Block detection must be precise. An earlier version tested for the bare
# substring "captcha", which matches "recaptcha" in the gstatic script tag
# present on ordinary Google result pages -- so every successful fetch was
# misreported as blocked. Match only markers that cannot appear on a good page.
BLOCK_MARKERS = (
    "Our systems have detected unusual traffic",
    "detected unusual traffic from your computer network",
    "/sorry/index",
    'id="captcha-form"',
    "why did this happen",
    "unusual traffic from your computer",
)
# A "sorry" interstitial also identifies itself in the title.
BLOCK_TITLE = re.compile(r"<title[^>]*>\s*(?:Error\s*)?(?:302\s*)?(?:Moved|Sorry)", re.I)


def _is_blocked(body: str, status: int) -> str:
    """Return a reason string if this response is a block page, else ''."""
    if status == 429:
        return "http 429"
    if status == 503:
        return "http 503"
    for marker in BLOCK_MARKERS:
        if marker in body:
            return f"marker: {marker[:40]}"
    if BLOCK_TITLE.search(body[:2000]):
        return "sorry/redirect title"
    return ""


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
        self._dumped_block = False
        self._dumped_error = False
        self._lcl_failures = 0
        self._lcl_disabled = False

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

                    reason = _is_blocked(body, resp.status)
                    if reason:
                        self.stats.blocked += 1
                        logger.warning(
                            "SERP blocked (attempt %s, %s, %s bytes) for %s",
                            attempt, reason, len(body), label or url,
                        )
                        # Snapshot the first block so the cause is inspectable
                        # rather than guessed at. Without this, a block page and
                        # a markup change look identical from the log.
                        if not self._dumped_block:
                            self._dumped_block = True
                            await self.dump_html("BLOCKED-response.html", body)
                            logger.warning(
                                "Saved the blocked response to the key-value store as "
                                "BLOCKED-response.html - open it to see what Google returned."
                            )
                        await asyncio.sleep(3 * attempt)
                        continue

                    if resp.status >= 400:
                        logger.warning(
                            "SERP HTTP %s (%s bytes) for %s", resp.status, len(body), label or url
                        )
                        if not self._dumped_error:
                            self._dumped_error = True
                            await self.dump_html(f"HTTP{resp.status}-response.html", body)
                        await asyncio.sleep(2 * attempt)
                        continue

                    if any(m in body for m in CONSENT_MARKERS):
                        # Consent interstitial: retry usually lands on a
                        # different IP that doesn't require it.
                        logger.info("Consent page returned for %s; retrying", label or url)
                        await asyncio.sleep(2)
                        continue

                    logger.debug("SERP ok (%s bytes) for %s", len(body), label or url)
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

    async def local_search(
        self, query: str, *, start: int = 0, allow_lcl: bool = True
    ) -> tuple[list[LocalResult], str]:
        """Businesses for a query, with automatic fallback.

        Preferred path is `tbm=lcl` (Google's local-finder tab): ~20 businesses
        per page with rating and review count inline, paginated by 20.

        Caveat: Apify's GOOGLE_SERP proxy documents support for Google Search
        and Google Shopping only. `tbm=lcl` is still a `/search` URL so it may
        pass, but it is not guaranteed. When it comes back blocked or empty we
        fall back to a plain `/search`, which is explicitly supported, and
        parse the local pack out of it. The pack holds ~3 businesses instead of
        20, so coverage per request is lower -- but it works.
        """
        if allow_lcl and not self._lcl_disabled:
            url = (
                f"http://{self.domain}/search?q={quote_plus(query)}"
                f"&tbm=lcl&hl=en&gl={self.country.lower()}&start={start}"
            )
            html = await self._get(url, label=f"lcl:{query}@{start}")
            if html:
                rows = parse_local_results(html)
                if rows:
                    return rows, html
                logger.info("tbm=lcl returned no parseable rows for %r", query)
            self._lcl_failures += 1
            if self._lcl_failures >= 3:
                self._lcl_disabled = True
                logger.warning(
                    "tbm=lcl failed %s times; falling back to plain search for the rest "
                    "of this run. Apify's GOOGLE_SERP proxy only documents support for "
                    "Google Search and Shopping, so the local-finder tab may be refused.",
                    self._lcl_failures,
                )

        # Fallback: plain search. Pagination by 10 here, not 20.
        url = (
            f"http://{self.domain}/search?q={quote_plus(query)}"
            f"&hl=en&gl={self.country.lower()}&start={start // 2}"
        )
        html = await self._get(url, label=f"plain:{query}@{start}")
        if not html:
            return [], ""
        return parse_local_pack(html), html

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


def parse_local_pack(html: str) -> list[LocalResult]:
    """Extract the local pack from a *plain* Google search page.

    A normal results page shows a small map block with roughly three
    businesses. Far less dense than tbm=lcl, but plain `/search` is the
    endpoint Apify's GOOGLE_SERP proxy officially supports, so this is the
    reliable fallback.
    """
    soup = BeautifulSoup(html, "lxml")
    results: list[LocalResult] = []
    seen: set[str] = set()

    # The pack lives in a handful of containers depending on layout.
    blocks = soup.select(
        "div.VkpGBb, div.rllt__details, div.cXedhc, div.uMdZh, "
        "div[data-record-index], div.C8TUKc"
    )
    if not blocks:
        # Some layouts only expose headings; climb to a sensible ancestor.
        for t in soup.select("div.dbg0pd, span.OSrXXb"):
            anc = t.find_parent("div")
            if anc is not None and anc.find_parent("div") is not None:
                blocks.append(anc.find_parent("div"))

    for block in blocks:
        text = block.get_text(" ", strip=True)
        if not text or len(text) < 6:
            continue

        name_el = block.select_one("div.dbg0pd, span.OSrXXb, div.qBF1Pd, span.DkEaL, h3")
        name = name_el.get_text(" ", strip=True) if name_el else text.split("·")[0].strip()
        name = re.sub(r"^\d+\.\s*", "", name).strip()[:120]
        if not name or name.lower() in seen:
            continue
        seen.add(name.lower())

        item = LocalResult(name=name)

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
        if item.rating is not None and not (0.0 < item.rating <= 5.0):
            item.rating = None

        parts = [p.strip() for p in re.split(r"\u00b7|\|", text) if p.strip()]
        name_low = name.lower()
        for part in parts:
            if part.lower().startswith(name_low):
                part = part[len(name):].strip(" ,-–—·|")
                if not part:
                    continue
            low = part.lower()
            if low == name_low:
                continue
            if re.search(r"\d", part) and any(
                tok in low for tok in ("road", "rd", "street", "block", "sector", "floor",
                                       "tower", "lane", "nagar", "park", "kolkata", "howrah", "7000")
            ):
                cand = _trim_address(part)
                if len(cand) > len(item.address):
                    item.address = cand
            elif not item.category and re.fullmatch(r"[A-Za-z /&'-]{3,40}", part):
                if not RATING_ONLY.search(part) and "review" not in low:
                    item.category = part

        for a in block.select("a[href]"):
            u = _clean_url(a.get("href", ""))
            if u:
                item.website = u
                break

        results.append(item)

    return results
