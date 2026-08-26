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

CONSENT_MARKERS = ('id="CXQnmb"', "Before you continue to Google")

# Block detection is the single most dangerous thing in this file to get wrong,
# because a false positive makes a working scraper look permanently broken.
#
# Two markers already burned this actor:
#   * "captcha"      matches "recaptcha" in the gstatic script tag on EVERY
#                    ordinary results page.
#   * "/sorry/index" appears inside Google's inline error-handling JavaScript
#                    on ordinary results pages too.
#
# The lesson: never substring-scan a full 390 KB results page for short
# markers. Real block pages are *small* and say so in the title, so gate on
# size and search only the head of the document.
BLOCK_MARKERS = (
    "Our systems have detected unusual traffic",
    "detected unusual traffic from your computer network",
    "unusual traffic from your computer",
    'id="captcha-form"',
)
# A real interstitial announces itself in the title.
BLOCK_TITLE = re.compile(r"<title[^>]*>\s*(?:Sorry|Error|Moved|302)", re.I)

# Anything this big is a rendered results page, not an interstitial. Google's
# "sorry" pages are a few KB; a results page is 200 KB+.
BLOCK_SIZE_CEILING = 60_000


def _is_blocked(body: str, status: int) -> str:
    """Return a reason string if this response is a block page, else ''."""
    if status == 429:
        return "http 429"
    if status == 503:
        return "http 503"

    # Explicit unusual-traffic language is trustworthy at any size.
    head = body[:20_000]
    for marker in BLOCK_MARKERS:
        if marker in head:
            return f"marker: {marker[:40]}"

    # Past this size it is a real page; do not second-guess it.
    if len(body) >= BLOCK_SIZE_CEILING:
        return ""

    if BLOCK_TITLE.search(body[:2000]):
        return "sorry/redirect title"
    if "/sorry/index" in head:
        return "sorry redirect (small page)"
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
    timeouts: int = 0
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
        timeout: int = 120,
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
                # asyncio.TimeoutError stringifies to "", so log the class name
                # too or the log line reads "fetch error for X:" with no cause.
                kind = type(exc).__name__
                detail = str(exc) or "(no message)"
                logger.warning(
                    "SERP fetch error (attempt %s, %s: %s) for %s",
                    attempt, kind, detail, label or url,
                )
                if isinstance(exc, asyncio.TimeoutError):
                    self.stats.timeouts += 1
                    if self.stats.timeouts == 1:
                        logger.warning(
                            "Timed out after %ss. Apify's GOOGLE_SERP proxy scrapes "
                            "server-side and a single fetch often needs 30-90s, so a "
                            "short timeout fails every request. Raise "
                            "serpRequestTimeoutSecs (currently %ss).",
                            self.timeout.total, self.timeout.total,
                        )
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

# Google's own accessibility label is by far the most stable source for these
# two fields, and it is the only place the review count survives unrounded
# formatting. Confirmed shape against live HTML:
#   "Rated 4.2 out of 5, 2.7K user reviews"
ARIA_RATING = re.compile(
    r"Rated\s+([\d.,]+)\s+out of\s+\d+\s*,?\s*([\d.,]+\s*[KMkm]?)\s*(?:user\s+)?(?:reviews?|ratings?)",
    re.I,
)
# Fallback for visible text: "4.5(128)" / "4.5 (2.7K)"
RATING_REVIEWS = re.compile(
    r"(\d[.,]\d)\s*(?:\u00b7|\||\s)?\s*\(\s*([\d,\.]+\s*[KMkm]?)\s*\)",
    re.I,
)
REVIEWS_ONLY = re.compile(r"([\d,\.]+\s*[KMkm]?)\s*(?:user\s+)?(?:reviews?|ratings?)", re.I)
RATING_ONLY = re.compile(r"\b([1-5][.,]\d)\b")
PHONE_RE = re.compile(r"(?:\+91[\s-]?)?(?:\d[\s-]?){9,13}")

# Service attributes Google lists alongside the category; never a category.
SERVICE_ATTRS = re.compile(
    r"^(?:on-?site services?|online appointments?|onsite services?|"
    r"open 24 hours|open|closed|closes soon|identifies as[\w\s-]*|"
    r"\d+\+? years in business)$",
    re.I,
)


def _to_count(raw: str) -> int | None:
    """Parse a review count, expanding Google's K/M abbreviations.

    "2.7K" -> 2700. Google rounds these itself, so the result is approximate
    for large counts; that is Google's precision, not ours to invent.
    """
    if not raw:
        return None
    s = raw.strip().replace(",", "")
    mult = 1
    if s and s[-1] in "KkMm":
        mult = 1_000 if s[-1] in "Kk" else 1_000_000
        s = s[:-1].strip()
    try:
        return int(round(float(s) * mult))
    except ValueError:
        return None


def _rating_and_reviews(block, text: str) -> tuple[float | None, int | None]:
    """Pull (rating, reviews) from a result block, aria-label first."""
    for el in block.select("[aria-label]"):
        m = ARIA_RATING.search(el.get("aria-label", ""))
        if m:
            try:
                rating = float(m.group(1).replace(",", "."))
            except ValueError:
                rating = None
            if rating is not None and not (0.0 < rating <= 5.0):
                rating = None
            return rating, _to_count(m.group(2))

    rating = reviews = None
    m = RATING_REVIEWS.search(text)
    if m:
        try:
            rating = float(m.group(1).replace(",", "."))
        except ValueError:
            rating = None
        reviews = _to_count(m.group(2))
    else:
        rm = RATING_ONLY.search(text)
        if rm:
            try:
                rating = float(rm.group(1).replace(",", "."))
            except ValueError:
                rating = None
        vm = REVIEWS_ONLY.search(text)
        if vm:
            reviews = _to_count(vm.group(1))

    if rating is not None and not (0.0 < rating <= 5.0):
        rating = None
    return rating, reviews


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


ADDRESS_TOKENS = (
    "road", "rd", "street", "st ", "block", "sector", "floor", "tower", "lane",
    "nagar", "park", "kolkata", "howrah", "7000", "complex", "building", "unit",
    "suite", "room", "plot", "bypass", "avenue", "bagan", "pally", "colony",
    "market", "station", "cn-", "dn-", "ep ", "gp ", "bn-", "no.",
)


# Google renders the category and the address in sibling divs, so they arrive
# glued into one "·" segment: "Software company CN-8/2". Split on the noun the
# category ends with -- splitting at the first digit instead mangles addresses
# that begin with a word ("Tower 1, Godrej Waterside" -> "...company Tower").
CATEGORY_TAIL = re.compile(
    r"^(.{3,44}?(?:compan(?:y|ies)|agenc(?:y|ies)|services?|consultants?|"
    r"consulting|designer|developer|solutions?|studio|store|shop|firm|"
    r"contractor|institute|centre|center))\s+(\S.*)$",
    re.I,
)
# Fallback: category followed by a number-led address.
CATEGORY_THEN_ADDRESS = re.compile(r"^([A-Za-z][A-Za-z /&'-]{2,40}?)\s+(\d.*)$")

# Segments that are really just a phone number (optionally with opening hours
# or a review quote trailing) must never be taken as the address.
LEADING_PHONE = re.compile(r"^\s*(?:\+91[\s-]?)?0?\d[\d\s-]{7,}")


def _split_category_address(part: str) -> tuple[str, str]:
    """Return (category, remainder) if the segment carries both."""
    m = CATEGORY_TAIL.match(part) or CATEGORY_THEN_ADDRESS.match(part)
    if not m:
        return "", part
    head, tail = m.group(1).strip(), m.group(2).strip()
    # Only treat the head as a category if it reads like one.
    if 3 <= len(head) <= 40 and not SERVICE_ATTRS.match(head):
        return head, tail
    return "", part


def _looks_like_address(part: str) -> bool:
    """Heuristic: does this "·"-separated segment look like a street address?

    Google's local rows put a short address fragment here (sometimes as terse
    as "CN-8/2"), so requiring a street keyword alone misses many. Accept any
    segment carrying a digit plus an address-ish token, or a slash-and-digit
    unit reference.
    """
    low = part.lower()
    if not re.search(r"\d", part):
        return False
    if any(tok in low for tok in ADDRESS_TOKENS):
        return True
    # Bare unit references like "CN-8/2" or "J1/16".
    return bool(re.search(r"[A-Za-z]{1,3}[- ]?\d+/\d+", part))


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

        # --- rating + reviews (aria-label first; see _rating_and_reviews)
        item.rating, item.reviews = _rating_and_reviews(block, text)

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
            part = TRAILING_JUNK.sub("", part).strip(" ,-\u2013\u2014\u00b7|")
            if not part or SERVICE_ATTRS.match(part):
                continue
            if LEADING_PHONE.match(part):
                continue
            cat, rest = _split_category_address(part)
            if cat and not item.category:
                item.category = cat
            probe = rest or part
            if _looks_like_address(probe):
                if not item.address:
                    item.address = _trim_address(probe)
            elif not item.category and re.fullmatch(r"[A-Za-z /&'.,-]{3,44}", probe):
                if not RATING_ONLY.search(probe) and "review" not in probe.lower():
                    item.category = probe

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

        item.rating, item.reviews = _rating_and_reviews(block, text)

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
            part = TRAILING_JUNK.sub("", part).strip(" ,-\u2013\u2014\u00b7|")
            if not part or SERVICE_ATTRS.match(part):
                continue
            if LEADING_PHONE.match(part):
                continue
            cat, rest = _split_category_address(part)
            if cat and not item.category:
                item.category = cat
            probe = rest or part
            if _looks_like_address(probe):
                if not item.address:
                    item.address = _trim_address(probe)
            elif not item.category and re.fullmatch(r"[A-Za-z /&'.,-]{3,44}", probe):
                if not RATING_ONLY.search(probe) and "review" not in probe.lower():
                    item.category = probe

        for a in block.select("a[href]"):
            u = _clean_url(a.get("href", ""))
            if u:
                item.website = u
                break

        results.append(item)

    return results
