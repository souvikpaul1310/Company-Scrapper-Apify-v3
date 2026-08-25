"""
website.py
----------
Fetches a company's own website (homepage + a few high-value inner pages)
and extracts:

  * the company's LinkedIn URL   -> feeds the LinkedIn enrichment step
  * a founder / owner name       -> field 6
  * an employee-count hint       -> field 7 fallback
  * service/product/both signals -> field 8

Design notes:
- Only follows same-domain links, and only ones whose href or anchor text
  looks like About / Team / Leadership / Services / Pricing. That keeps
  it to ~5 requests per company instead of a full crawl.
- Every request is bounded by a semaphore and a timeout; a dead site
  degrades to empty fields rather than failing the run.
- Scraping the LinkedIn URL off the company's own footer is far more
  accurate than searching LinkedIn by name (no wrong-company matches)
  and costs nothing.
"""

from __future__ import annotations

import asyncio
import re
from dataclasses import dataclass, field
from urllib.parse import urljoin, urlparse

import aiohttp
from bs4 import BeautifulSoup

from .classify import classify_company_type

DEFAULT_TIMEOUT_SECS = 20
MAX_PAGES_PER_SITE = 5

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)

# Inner pages worth opening, best-first.
INTERESTING_PAGE = re.compile(
    r"(about|about-us|our-team|team|leadership|management|company|"
    r"who-we-are|founders?|services|our-services|solutions|pricing|plans)",
    re.I,
)

LINKEDIN_COMPANY = re.compile(
    r"https?://(?:[a-z]{2,3}\.)?linkedin\.com/company/([A-Za-z0-9_\-.%]+)", re.I
)

# --- Founder / owner extraction ------------------------------------------

ROLE_RANK = {
    "founder": 100,
    "co-founder": 95,
    "cofounder": 95,
    "founder & ceo": 100,
    "owner": 85,
    "proprietor": 80,
    "managing director": 75,
    "ceo": 70,
    "chief executive officer": 70,
    "chairman": 60,
    "director": 40,
}

_ROLE_ALT = (
    r"co[-\s]?founder|founder|owner|proprietor|managing\s+director|"
    r"chief\s+executive\s+officer|ceo|chairman|director"
)
# Case-insensitivity is scoped to the ROLE only. Applying re.I to the whole
# pattern would make [A-Z] match lowercase too, so "Anita Desai works" would
# be captured as a three-word name.
_ROLE_CI = rf"(?i:{_ROLE_ALT})"
_NAME = r"[A-Z][a-z]{1,20}(?:\s+[A-Z][a-z.]{1,20}){1,2}"
# Separator between name and role. A dash MUST be preceded by whitespace,
# otherwise "Priya Sharma Co-Founder" parses as name="Priya Sharma Co".
_SEP = r"(?:\s*[,|:·]\s*|\s+[-–—]\s*)"

# "Rahul Sharma, Founder"  /  "Rahul Sharma - Co-Founder & CEO"
NAME_THEN_ROLE = re.compile(
    rf"\b({_NAME}){_SEP}(?:[Tt]he\s+)?({_ROLE_CI})\b",
    re.M,
)
# "Founder: Rahul Sharma"  /  "CEO - Rahul Sharma"
ROLE_THEN_NAME = re.compile(
    rf"\b({_ROLE_CI}){_SEP}({_NAME})\b",
    re.M,
)
# A role keyword at the START of a text run -- used by the DOM pass to test
# whether the element following a name is that person's job title.
ROLE_ONLY = re.compile(rf"^\s*(?:the\s+)?({_ROLE_ALT})\b", re.I)

# "founded by Rahul Sharma" / "Founded in 2009 by Meera Iyer"
FOUNDED_BY = re.compile(
    rf"\b(?i:founded)\s+(?:(?i:in)\s+\d{{4}}\s+)?(?i:by)\s+({_NAME})\b"
)

# .title() turns "ceo" into "Ceo" and "founder & ceo" into "Founder & Ceo".
# Keep known acronyms upper-case instead.
_ROLE_ACRONYMS = {"ceo", "cto", "coo", "cfo"}


def _format_role(role: str) -> str:
    return " ".join(
        w.upper() if w.lower() in _ROLE_ACRONYMS else w.capitalize()
        for w in role.split(" ")
    )


# Single tokens that never appear in a person's name. Any candidate
# containing one of these is rejected outright -- this is what stops
# "Meet Our" and "Our Founder" being read as names on team pages.
NON_NAME_WORDS = {
    "meet", "our", "the", "us", "we", "your", "my", "all", "more", "view",
    "read", "learn", "get", "see", "why", "how", "what", "who", "join",
    "contact", "about", "home", "team", "careers", "blog", "news", "privacy",
    "terms", "policy", "follow", "copyright", "rights", "reserved", "menu",
    "founder", "cofounder", "owner", "ceo", "cto", "coo", "cfo", "chief",
    "executive", "officer", "director", "managing", "head", "lead", "senior",
    "junior", "manager", "co", "president", "chairman", "partner", "associate",
    "software", "development", "developer", "technologies", "technology",
    "solutions", "services", "digital", "marketing", "consulting", "systems",
    "private", "limited", "ltd", "llp", "inc", "pvt", "company", "group",
    "india", "kolkata", "mumbai", "delhi", "bangalore", "chennai", "pune",
    "email", "phone", "address", "call", "now", "here", "click", "sign",
    "portfolio", "clients", "work", "products", "product", "pricing",
}

# Capitalised phrases that look like names but aren't.
NAME_STOPLIST = {
    "about us", "our team", "contact us", "read more", "learn more",
    "privacy policy", "terms of service", "our services", "the company",
    "get in touch", "our story", "our mission", "our vision", "case study",
    "case studies", "our clients", "the team", "meet the", "view all",
    "quick links", "follow us", "all rights", "site map", "home about",
    "software development", "web development", "digital marketing",
    "mobile app", "information technology", "united states", "new delhi",
}

# --- Employee count extraction -------------------------------------------

EMPLOYEE_PATTERNS = [
    re.compile(r"\bteam\s+of\s+(?:over\s+|more\s+than\s+)?(\d{1,5})\s*\+?\b", re.I),
    re.compile(
        r"\b(\d{1,5})\s*\+?\s*(?:employees|professionals|experts|engineers|"
        r"developers|specialists|team\s+members|strong)\b",
        re.I,
    ),
    re.compile(
        r"\b(?:over|more\s+than)\s+(\d{1,5})\s+(?:employees|professionals|people)\b",
        re.I,
    ),
]


@dataclass
class WebsiteData:
    reachable: bool = False
    checked_urls: list[str] = field(default_factory=list)
    linkedin_url: str = ""
    founder_name: str = ""
    founder_role: str = ""
    employee_hint: int | None = None
    company_type: str = "unknown"
    type_confidence: str = "low"
    type_evidence: str = ""
    error: str = ""


def normalize_url(raw: str) -> str:
    url = (raw or "").strip()
    if not url:
        return ""
    if not url.startswith(("http://", "https://")):
        url = "https://" + url
    return url


def _looks_like_person(name: str) -> bool:
    low = name.strip().lower()
    if low in NAME_STOPLIST:
        return False
    if any(stop in low for stop in NAME_STOPLIST):
        return False
    parts = name.split()
    if not 2 <= len(parts) <= 3:
        return False
    if any(p.strip(".,").lower() in NON_NAME_WORDS for p in parts):
        return False
    # Reject ALL-CAPS acronyms and single-letter fragments.
    return all(len(p.strip(".")) >= 2 for p in parts)


def extract_founder_from_dom(soup: BeautifulSoup) -> list[tuple[int, str, str]]:
    """
    Handle the standard team-card layout:

        <h3>Arjun Banerjee</h3><p>Founder & CEO</p>

    There's no punctuation between name and role here, so the flat-text
    regexes can't see it. Walking the DOM is both more accurate and safer
    than loosening those regexes to allow a bare space separator.
    """
    candidates: list[tuple[int, str, str]] = []
    name_tags = ["h1", "h2", "h3", "h4", "h5", "h6", "strong", "b", "span", "a", "td"]

    for tag in soup.find_all(name_tags):
        label = tag.get_text(" ", strip=True)
        # A person's name is short; anything long is a paragraph, not a name.
        if not label or len(label) > 40 or not _looks_like_person(label):
            continue

        # Peek at a small window of text that follows this element.
        # find_all_next() also returns the tag's OWN descendant text, which
        # would put the name itself at the front and break the anchored
        # role match -- so skip anything still inside this tag.
        parts: list[str] = []
        for s in tag.find_all_next(string=True):
            if tag in s.parents:
                continue
            t = s.strip()
            if t:
                parts.append(t)
            if len(parts) >= 4:
                break
        following = " ".join(parts)[:80]

        m = ROLE_ONLY.match(following)
        if m:
            role = m.group(1).strip().lower()
            candidates.append((ROLE_RANK.get(role, 30), label, role))

    return candidates


def extract_founder(text: str, soup: BeautifulSoup | None = None) -> tuple[str, str]:
    """Returns (name, role) for the highest-ranking person found, else ('', '')."""
    candidates: list[tuple[int, str, str]] = []

    if soup is not None:
        candidates.extend(extract_founder_from_dom(soup))

    for m in NAME_THEN_ROLE.finditer(text):
        name, role = m.group(1).strip(), m.group(2).strip().lower()
        if _looks_like_person(name):
            candidates.append((ROLE_RANK.get(role, 30), name, role))

    for m in ROLE_THEN_NAME.finditer(text):
        role, name = m.group(1).strip().lower(), m.group(2).strip()
        if _looks_like_person(name):
            candidates.append((ROLE_RANK.get(role, 30), name, role))

    for m in FOUNDED_BY.finditer(text):
        name = m.group(1).strip()
        if _looks_like_person(name):
            candidates.append((100, name, "founder"))

    if not candidates:
        return "", ""

    candidates.sort(key=lambda c: -c[0])
    _, name, role = candidates[0]
    return name, _format_role(role)


def extract_employee_hint(text: str) -> int | None:
    best: int | None = None
    for pattern in EMPLOYEE_PATTERNS:
        for m in pattern.finditer(text):
            try:
                n = int(m.group(1))
            except (ValueError, IndexError):
                continue
            # Filter out implausible numbers (years, phone fragments, revenue).
            if 2 <= n <= 500_000 and not (1900 <= n <= 2100):
                best = n if best is None else max(best, n)
    return best


async def _fetch(
    session: aiohttp.ClientSession, url: str, timeout: int, proxy: str | None
) -> str | None:
    """GET a page, returning HTML or None. Retries once without cert checks."""
    for verify_ssl in (True, False):
        try:
            async with session.get(
                url,
                timeout=aiohttp.ClientTimeout(total=timeout),
                allow_redirects=True,
                ssl=verify_ssl,
                proxy=proxy,
            ) as resp:
                if resp.status >= 400:
                    return None
                ctype = resp.headers.get("Content-Type", "")
                if "html" not in ctype.lower() and ctype:
                    return None
                return await resp.text(errors="ignore")
        except (aiohttp.ClientSSLError, aiohttp.ClientConnectorSSLError):
            continue  # retry with verify_ssl=False
        except Exception:
            return None
    return None


def _pick_inner_links(soup: BeautifulSoup, base_url: str, limit: int) -> list[str]:
    base_host = urlparse(base_url).netloc.lower().removeprefix("www.")
    scored: list[tuple[int, str]] = []
    seen: set[str] = set()

    for a in soup.find_all("a", href=True):
        href = a["href"].strip()
        if href.startswith(("mailto:", "tel:", "javascript:", "#")):
            continue
        full = urljoin(base_url, href).split("#")[0].rstrip("/")
        host = urlparse(full).netloc.lower().removeprefix("www.")
        if host != base_host or full in seen:
            continue

        blob = f"{href} {a.get_text(' ', strip=True)}"
        if not INTERESTING_PAGE.search(blob):
            continue

        seen.add(full)
        # Prefer About/Team pages -- that's where founders live.
        low = blob.lower()
        priority = 0
        if re.search(r"team|leadership|founder|management", low):
            priority = 3
        elif re.search(r"about|who-we-are|company", low):
            priority = 2
        elif re.search(r"pricing|plans", low):
            priority = 2
        else:
            priority = 1
        scored.append((priority, full))

    scored.sort(key=lambda s: -s[0])
    return [u for _, u in scored[:limit]]


async def enrich_from_website(
    session: aiohttp.ClientSession,
    raw_url: str,
    timeout: int = DEFAULT_TIMEOUT_SECS,
    proxy: str | None = None,
    max_pages: int = MAX_PAGES_PER_SITE,
) -> WebsiteData:
    data = WebsiteData()
    url = normalize_url(raw_url)
    if not url:
        data.error = "no website listed"
        return data

    html = await _fetch(session, url, timeout, proxy)
    if html is None and url.startswith("https://"):
        url = "http://" + url[len("https://"):]
        html = await _fetch(session, url, timeout, proxy)

    if html is None:
        data.error = "website unreachable"
        return data

    data.reachable = True
    data.checked_urls.append(url)

    soup = BeautifulSoup(html, "lxml")
    pages_html = [html]

    inner = _pick_inner_links(soup, url, max_pages - 1)
    if inner:
        results = await asyncio.gather(
            *[_fetch(session, u, timeout, proxy) for u in inner],
            return_exceptions=True,
        )
        for u, r in zip(inner, results):
            if isinstance(r, str) and r:
                pages_html.append(r)
                data.checked_urls.append(u)

    combined_html = "\n".join(pages_html)

    # LinkedIn company URL, straight from the site's own markup.
    m = LINKEDIN_COMPANY.search(combined_html)
    if m:
        slug = m.group(1).rstrip("/")
        data.linkedin_url = f"https://www.linkedin.com/company/{slug}"

    # Visible text only -- keeps regexes off scripts and CSS. We keep the
    # parsed soups too, since the DOM pass finds founders that flat text
    # can't (name and role in adjacent elements, no punctuation between).
    text_parts: list[str] = []
    soups: list[BeautifulSoup] = []
    for page in pages_html:
        s = BeautifulSoup(page, "lxml")
        for tag in s(["script", "style", "noscript"]):
            tag.decompose()
        soups.append(s)
        text_parts.append(s.get_text(" ", strip=True))
    combined_text = "\n".join(text_parts)
    combined_text = re.sub(r"\s{2,}", " ", combined_text)

    # Run the DOM pass over every fetched page, best candidate wins.
    dom_candidates: list[tuple[int, str, str]] = []
    for s in soups:
        dom_candidates.extend(extract_founder_from_dom(s))

    name, role = extract_founder(combined_text)
    if dom_candidates:
        # The DOM pass is structurally grounded (a name element followed by
        # a role element), so it beats flat-text regex whenever it fires.
        dom_candidates.sort(key=lambda c: -c[0])
        _, best_name, best_role = dom_candidates[0]
        name, role = best_name, _format_role(best_role)

    data.founder_name, data.founder_role = name, role
    data.employee_hint = extract_employee_hint(combined_text)
    data.company_type, data.type_confidence, data.type_evidence = (
        classify_company_type(combined_text, data.checked_urls)
    )
    return data


async def enrich_many(
    websites: list[str],
    concurrency: int = 5,
    timeout: int = DEFAULT_TIMEOUT_SECS,
    proxy: str | None = None,
    max_pages: int = MAX_PAGES_PER_SITE,
    log=None,
) -> list[WebsiteData]:
    """Enrich a list of website URLs concurrently. Order is preserved."""
    semaphore = asyncio.Semaphore(concurrency)
    connector = aiohttp.TCPConnector(limit=concurrency * 2, ssl=False)
    headers = {"User-Agent": USER_AGENT, "Accept-Language": "en-US,en;q=0.9"}

    async with aiohttp.ClientSession(
        connector=connector, headers=headers, trust_env=True
    ) as session:

        async def _bounded(idx: int, site: str) -> WebsiteData:
            async with semaphore:
                if log:
                    log.info(
                        f"[{idx}/{len(websites)}] Reading {site or '(no website)'} ..."
                    )
                try:
                    return await enrich_from_website(
                        session, site, timeout=timeout, proxy=proxy, max_pages=max_pages
                    )
                except Exception as e:  # never let one bad site kill the run
                    if log:
                        log.warning(f"  failed on {site}: {e}")
                    return WebsiteData(error=str(e))

        tasks = [_bounded(i, s) for i, s in enumerate(websites, 1)]
        return await asyncio.gather(*tasks)
