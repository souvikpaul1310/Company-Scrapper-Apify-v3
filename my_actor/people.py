"""Founder names and employee counts, derived from Google result snippets.

This replaces the paid LinkedIn actor. It never logs into LinkedIn or fetches
linkedin.com directly (which is both blocked and legally fraught). Instead it
reads what Google already publishes in its result titles and snippets, which
for LinkedIn company pages routinely includes the employee count.

Every value carries a confidence level, and the director-vs-founder
distinction from the research playbook is enforced in code: a name that only
appears in a company-registry context is reported as a director, never
promoted to "founder".
"""

from __future__ import annotations

import re

from .serp import OrganicResult

# --------------------------------------------------------------- founder side

# CRITICAL: these patterns must NOT carry a global re.I flag. Under re.I the
# [A-Z] class also matches lowercase, which destroys the capitalisation
# requirement that distinguishes a person's name from ordinary prose -- it
# silently absorbs words like "and" into the captured name. Role keywords are
# therefore made case-insensitive individually with scoped (?i:...) groups.
NAME = r"[A-Z][a-z]{1,15}(?:\s+[A-Z][a-z'.-]{1,15}){1,2}"

# Bare "director" is included so registry listings are *detected*; the
# confidence logic below then demotes them rather than calling them founders.
ROLE_WORDS = (
    r"(?i:(?:co-?)?founder|chief\s+executive|ceo|cto|coo|managing\s+director|"
    r"director|proprietor|owner|chairman|partner)"
)

# "Rahul Sharma - Founder & CEO" / "Rahul Sharma, Founder"
NAME_THEN_ROLE = re.compile(
    rf"\b({NAME})\s*[-–—,|:]\s*({ROLE_WORDS}(?:[A-Za-z&\s]{{0,24}})?)"
)
# "Founder & CEO: Rahul Sharma" / "CEO Rahul Sharma"
ROLE_THEN_NAME = re.compile(
    rf"\b({ROLE_WORDS}(?:\s*(?:&|(?i:and))\s*(?i:ceo|cto|md))?)\s*[-–—,|:]?\s+({NAME})\b"
)
# "founded in 2015 by Meera Iyer" / "founded by Meera Iyer and Arjun Das"
FOUNDED_BY = re.compile(
    rf"(?i:founded)\s+(?:(?i:in)\s+\d{{4}}\s+)?(?i:by)\s+({NAME})"
    rf"(?:\s+(?:(?i:and)|&)\s+({NAME}))?"
)

ROLE_RANK = {
    "founder": 100,
    "co-founder": 95,
    "cofounder": 95,
    "owner": 80,
    "proprietor": 78,
    "managing director": 70,
    "ceo": 60,
    "chief executive": 60,
    "chairman": 55,
    "partner": 45,
    "cto": 40,
    "coo": 38,
    # Bare "director" outranks nothing -- it is a legal role, and on its own is
    # not evidence of founding.
    "director": 20,
}

# Registry mirrors. A name sourced only from these is a *director*, which is a
# legal role and frequently not a founder at all -- a spouse added to meet the
# two-director minimum for an Indian Pvt Ltd, or a later professional hire.
REGISTRY_HOSTS = (
    "zaubacorp", "tofler", "indiafilings", "mca.gov", "filesure", "quickcompany",
    "instafinancials", "thecompanycheck", "falconebiz", "registrationwala",
)

# Aggregators: weak corroboration only, never high confidence.
AGGREGATOR_HOSTS = (
    "rocketreach", "zoominfo", "signalhire", "datanyze", "lusha", "apollo.io",
    "leadiq", "easyleadz", "growjo", "kaspr",
)

NOT_A_NAME = {
    "private limited", "pvt ltd", "limited", "solutions", "technologies", "software",
    "services", "systems", "infotech", "consulting", "india", "kolkata", "salt lake",
    "sector", "team", "human resources", "talent acquisition", "customer support",
    "linkedin", "facebook", "google", "about us", "our team", "contact us",
    "chief executive", "board member", "view profile", "top companies",
}


def _plausible_name(name: str) -> bool:
    n = (name or "").strip()
    if len(n) < 5 or len(n) > 45:
        return False
    low = n.lower()
    if any(bad in low for bad in NOT_A_NAME):
        return False
    words = n.split()
    if not (2 <= len(words) <= 3):
        return False
    # Every word should look like a capitalised name token.
    return all(re.fullmatch(r"[A-Z][a-zA-Z'.-]{1,15}", w) for w in words)


def _normalise_role(role: str) -> str:
    r = re.sub(r"\s+", " ", (role or "")).strip(" -–—,:|&").lower()
    r = r.replace("cofounder", "co-founder")
    for key in sorted(ROLE_RANK, key=len, reverse=True):
        if key in r:
            return key
    return r[:32]


def _format_role(role: str) -> str:
    acronyms = {"ceo", "cto", "md", "coo", "cfo"}
    return " ".join(
        w.upper() if w.lower() in acronyms else w.capitalize() for w in (role or "").split()
    )


def extract_founder(
    results: list[OrganicResult], company_name: str
) -> tuple[str, str, str, str]:
    """Return (name, role, confidence, source_note).

    Confidence follows the playbook: an explicit role statement on the
    company's own site or a LinkedIn profile is `high`; agreement between two
    independent third parties is `medium`; a single aggregator or a
    registry-only director listing is `low`.
    """
    company_tokens = {
        t for t in re.findall(r"[a-z]{4,}", (company_name or "").lower())
    } - {"private", "limited", "solutions", "technologies", "software", "services"}

    candidates: list[tuple[int, str, str, str, bool]] = []  # rank, name, role, host, is_registry

    for res in results:
        host = (res.url or "").lower()
        blob = f"{res.title} . {res.snippet}"
        is_registry = any(h in host for h in REGISTRY_HOSTS)
        is_linkedin = "linkedin.com/in" in host
        is_own_site = bool(company_tokens) and any(t in host for t in company_tokens)
        is_aggregator = any(h in host for h in AGGREGATOR_HOSTS)

        found: list[tuple[str, str]] = []
        for m in NAME_THEN_ROLE.finditer(blob):
            found.append((m.group(1), m.group(2)))
        for m in ROLE_THEN_NAME.finditer(blob):
            found.append((m.group(2), m.group(1)))
        for m in FOUNDED_BY.finditer(blob):
            found.append((m.group(1), "founder"))
            if m.group(2):
                found.append((m.group(2), "co-founder"))

        for raw_name, raw_role in found:
            if not _plausible_name(raw_name):
                continue
            role = _normalise_role(raw_role)
            rank = ROLE_RANK.get(role, 30)
            if is_own_site or is_linkedin:
                rank += 40
            elif is_aggregator:
                rank -= 15
            if is_registry:
                rank -= 25
            candidates.append((rank, raw_name.strip(), role, host, is_registry))

    if not candidates:
        return "", "", "not found", ""

    candidates.sort(key=lambda c: -c[0])
    _, name, role, host, is_registry = candidates[0]

    # How many distinct hosts back this same person?
    same_person = {c[3] for c in candidates if c[1].lower() == name.lower()}
    non_registry = {h for h in same_person if not any(r in h for r in REGISTRY_HOSTS)}

    strong_host = any(
        ("linkedin.com/in" in h) or (company_tokens and any(t in h for t in company_tokens))
        for h in same_person
    )

    if is_registry and not non_registry:
        # Registry-only: report as director, never as founder.
        return (
            name,
            "Director (per company registry)",
            "low",
            "registry listing only - directors are a legal role, not necessarily founders",
        )

    if strong_host:
        confidence = "high"
    elif len(non_registry) >= 2:
        confidence = "medium"
    else:
        confidence = "low"

    # Collect co-founders mentioned alongside.
    others = [
        c[1] for c in candidates
        if c[1].lower() != name.lower() and c[2] in ("founder", "co-founder")
    ]
    seen: list[str] = []
    for o in others:
        if o.lower() not in {s.lower() for s in seen}:
            seen.append(o)
    if seen:
        name = f"{name} & {' & '.join(seen[:2])}"

    return name, _format_role(role), confidence, ""


# ------------------------------------------------------------- employees side

# LinkedIn SERP snippets commonly read like:
#   "... 1,234 followers on LinkedIn. ... 51-200 employees ..."
EMP_RANGE = re.compile(r"\b(\d{1,3}(?:,\d{3})?)\s*[-–—to]{1,3}\s*(\d{1,4}(?:,\d{3})?)\s*employees\b", re.I)
EMP_EXACT = re.compile(r"\b([\d,]{1,7})\s*(?:\+\s*)?employees\b", re.I)
EMP_ASSOC = re.compile(r"\b([\d,]{1,7})\s*(?:associated\s+)?members\b", re.I)
EMP_TEAM = re.compile(r"\b(?:team of|staff of|workforce of)\s*([\d,]{1,6})\s*\+?", re.I)
FOLLOWERS = re.compile(r"\b([\d,]{1,9})\s*followers\b", re.I)


def _int(raw: str) -> int | None:
    d = re.sub(r"[^\d]", "", raw or "")
    return int(d) if d else None


def extract_employees(
    results: list[OrganicResult], website_hint: int | None = None
) -> tuple[str, str, str]:
    """Return (value, confidence, note).

    Reports a range rather than averaging when sources disagree -- averaging a
    real figure with a bad scrape just produces a worse figure.
    """
    # Each entry is (label, low, high) so a band like "51-200" keeps both ends
    # instead of collapsing to a midpoint and understating the real spread.
    linkedin_vals: list[tuple[str, int, int]] = []
    other_vals: list[tuple[str, int, int]] = []

    for res in results:
        host = (res.url or "").lower()
        blob = f"{res.title} . {res.snippet}"
        is_linkedin = "linkedin.com" in host
        is_aggregator = any(h in host for h in AGGREGATOR_HOSTS)

        bucket = linkedin_vals if is_linkedin else other_vals

        m = EMP_RANGE.search(blob)
        if m:
            lo, hi = _int(m.group(1)), _int(m.group(2))
            if lo and hi and hi >= lo and hi <= 500_000:
                bucket.append((f"{lo}-{hi} (band)", lo, hi))
                continue

        for pattern, label in ((EMP_EXACT, "employees"), (EMP_ASSOC, "LinkedIn members"),
                               (EMP_TEAM, "site claim")):
            m = pattern.search(blob)
            if m:
                val = _int(m.group(1))
                # Follower counts get misread as employees constantly; if the
                # same number appears as followers, it isn't headcount.
                fm = FOLLOWERS.search(blob)
                if fm and _int(fm.group(1)) == val:
                    continue
                if val and 0 < val <= 500_000:
                    src = "LinkedIn" if is_linkedin else ("aggregator" if is_aggregator else label)
                    bucket.append((f"{val} ({src})", val, val))
                break

    if website_hint and 0 < website_hint <= 500_000:
        other_vals.append((f"{website_hint} (company site)", website_hint, website_hint))

    all_vals = linkedin_vals + other_vals
    if not all_vals:
        return "", "not found", ""

    lo = min(v[1] for v in all_vals)
    hi = max(v[2] for v in all_vals)
    spread = hi / lo if lo else 999

    # LinkedIn is the preferred anchor.
    primary = linkedin_vals[0][0] if linkedin_vals else other_vals[0][0]

    if spread > 3:
        labels = "; ".join(dict.fromkeys(v[0] for v in all_vals))
        return f"{lo}-{hi}", "low", f"sources disagree by {spread:.1f}x - {labels}"

    if linkedin_vals and other_vals:
        return primary, "high", ""
    if linkedin_vals:
        return primary, "medium", ""
    if any("aggregator" in v[0] for v in other_vals):
        return primary, "low", "aggregator sources only"
    return primary, "medium", ""
