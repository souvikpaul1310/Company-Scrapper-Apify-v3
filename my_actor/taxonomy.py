"""Business-category taxonomy for discovery filtering.

Two jobs, kept deliberately separate:

  * `categorise` decides whether a listing belongs in the output at all, and
    tags it with a broad sector. This is about *what kind of business* it is.
  * `classify.py` decides service vs product vs both. That is about *how the
    business earns revenue*, which is a different question.

The scope here is intentionally wide: IT, software, web/app development,
digital marketing, SEO, design agencies, data/AI, and IT services. What gets
rejected is businesses that are not agencies or vendors at all -- training
institutes, recruitment agencies, device repair shops, retailers -- because
Google's local finder mixes those into every one of these queries heavily.
"""

from __future__ import annotations

import re

# Ordered: the first matching category wins, so the more specific patterns
# must come before the generic "it_services" catch-all.
CATEGORY_PATTERNS: list[tuple[str, re.Pattern]] = [
    (
        "data_ai",
        re.compile(
            r"\b(?:artificial intelligence|machine learning|\bml\b|\bai\b|data science|"
            r"data analytics|data annotation|data label|computer vision|\bnlp\b|"
            r"big data|business intelligence|\bbi\b)\b",
            re.I,
        ),
    ),
    (
        "cybersecurity",
        re.compile(r"\b(?:cyber ?security|infosec|vapt|penetration test|ethical hack|soc\b)\b", re.I),
    ),
    (
        "erp_crm",
        re.compile(r"\b(?:erp|crm|tally|odoo|sap|marg|netsuite|dynamics|accounting software)\b", re.I),
    ),
    (
        "game_dev",
        re.compile(
            r"\b(?:game ?dev\w*|gaming|game studio|games? (?:compan|studio)|"
            r"unity|unreal|vfx|animation studio)",
            re.I,
        ),
    ),
    (
        "digital_marketing",
        re.compile(
            r"\b(?:digital marketing|seo|search engine optimi|sem\b|ppc|social media|"
            r"smo\b|content marketing|performance marketing|advertis|branding|"
            r"media agency|growth agency|lead generation)\b",
            re.I,
        ),
    ),
    (
        "web_mobile",
        re.compile(
            r"\b(?:web ?(?:site|design|develop)|webdesign|mobile app|app ?develop|"
            r"android|ios\b|flutter|react|shopify|wordpress|magento|woocommerce|"
            r"ecommerce|e-commerce|ui ?/? ?ux|graphic design)\b",
            re.I,
        ),
    ),
    (
        "software",
        re.compile(
            r"\b(?:software|saas|product engineering|custom software|application develop|"
            r"programming|coding|devops|cloud|blockchain|iot)\b",
            re.I,
        ),
    ),
    (
        "it_services",
        re.compile(
            r"\b(?:\bit\b|information technology|infotech|technolog|tech\b|systems?|"
            r"solutions?|consult|computing|digital|network|managed services|bpo|"
            r"outsourc|kpo|call ?cent)\b",
            re.I,
        ),
    ),
]

# Hard rejects. These are checked first and override any category match,
# because a name like "Brainware Computer Academy" matches "it_services" on
# the word "computer" while being a training institute.
REJECT_PATTERNS: list[tuple[str, re.Pattern]] = [
    (
        "training_institute",
        re.compile(
            r"\b(?:academy|institute|coaching|tuition|classes|training cent|"
            r"computer cent|computer training|learning cent|educational|college|"
            r"university|school|tutorial|skill ?devel|certification cent)\b",
            re.I,
        ),
    ),
    (
        "recruitment",
        re.compile(
            r"\b(?:placement|recruit|manpower|staffing|hr consult|employment|"
            r"job ?(?:portal|consult|agency)|career ?(?:consult|zone|point)|"
            r"talent acquisition|head ?hunt)\b",
            re.I,
        ),
    ),
    (
        "repair_retail",
        re.compile(
            r"\b(?:repair|servicing cent|laptop ?(?:service|repair)|mobile ?repair|"
            r"chip ?level|hardware store|electronics store|computer (?:store|shop|sales)|"
            r"xerox|photocopy|stationery|cyber ?cafe|internet ?cafe|mobile shop|"
            r"accessories|spare ?parts|toner|cartridge)\b",
            re.I,
        ),
    ),
    (
        "not_a_business",
        re.compile(
            r"\b(?:housing|apartment|residency|estate|complex|hotel|restaurant|cafe|"
            r"hospital|clinic|diagnostic|pharmacy|temple|church|mosque|park|"
            r"metro station|railway station|bus stand|petrol|elevator|lift\b|"
            r"news|magazine|gym|salon|boutique|travel agency|tour)\b",
            re.I,
        ),
    ),
]

CATEGORY_LABELS = {
    "software": "Software / product engineering",
    "web_mobile": "Web & mobile development",
    "digital_marketing": "Digital marketing / SEO",
    "data_ai": "Data / AI / analytics",
    "cybersecurity": "Cybersecurity",
    "erp_crm": "ERP / CRM implementation",
    "game_dev": "Game development / animation",
    "it_services": "IT services / consulting",
}


DEFAULT_CATEGORIES = [
    "software",
    "web_mobile",
    "digital_marketing",
    "erp_crm",
    "it_services",
]


def categorise(
    name: str, google_category: str = "", snippet: str = ""
) -> tuple[list[str], str]:
    """Return (matched_categories, reject_reason).

    Returns *every* sector the listing matches, most specific first, rather
    than just the first hit. The caller resolves that against the sectors it
    actually wants -- so a software firm that happens to mention AI is kept
    under `software` instead of being dropped as `data_ai`.

    `matched_categories` is empty when the listing should be excluded outright;
    `reject_reason` then names the rule that fired, which keeps filtering
    decisions auditable rather than mysterious.
    """
    blob = " ".join(p for p in (name, google_category, snippet) if p)
    if not blob.strip():
        return [], "empty"

    for label, pattern in REJECT_PATTERNS:
        m = pattern.search(blob)
        if m:
            # A strong vendor signal in the *name* can rescue a listing whose
            # Google category is noisy -- e.g. a software firm that also runs a
            # training arm. Require the signal in the name specifically.
            if label == "training_institute" and re.search(
                r"\b(?:software|technolog|infotech|solutions?|systems?|labs?|studio)\b",
                name,
                re.I,
            ):
                continue
            return [], f"rejected:{label} ({m.group(0).strip().lower()})"

    matched = [key for key, pattern in CATEGORY_PATTERNS if pattern.search(blob)]
    if not matched:
        return [], "no sector signal"
    return matched, ""


# `it_services` matches on generic words like "solutions", "systems" and
# "technologies", which appear in almost every company name. It is therefore
# treated as a weak signal: it can classify a listing that has no more specific
# match, but it must NOT rescue a listing whose specific match is a sector the
# user excluded. Without this, "Indian Cyber Security Solutions" would slip
# through as it_services purely on the word "Solutions".
GENERIC_CATEGORIES = {"it_services"}


def resolve_category(
    matched: list[str], allowed: list[str] | None
) -> tuple[str, str]:
    """Pick the best sector for a listing given the sectors the user wants.

    Returns (chosen, reject_reason). A listing that only matches sectors the
    user excluded is dropped -- deleting those patterns instead would let such
    firms fall through to the generic `it_services` catch-all and get included
    under the wrong label.
    """
    if not matched:
        return "", "no sector signal"
    allow = set(allowed) if allowed else set(DEFAULT_CATEGORIES)

    specific = [k for k in matched if k not in GENERIC_CATEGORIES]

    # A specific wanted sector always wins, even if an excluded sector also
    # matched: a software firm that mentions AI is still a software firm.
    for key in specific:
        if key in allow:
            return key, ""

    # No specific wanted match. If a specific *excluded* sector matched, this
    # is a pure play in a sector the user does not want -- drop it rather than
    # letting the generic catch-all pull it back in.
    if specific:
        return "", f"excluded sector:{specific[0]}"

    # Only generic signals matched; classify under whichever is allowed.
    for key in matched:
        if key in allow:
            return key, ""
    return "", f"excluded sector:{matched[0]}"


def search_terms_for(categories: list[str] | None = None) -> list[str]:
    """Search phrases per sector. Each is run separately per area."""
    by_cat = {
        "software": [
            "software company",
            "software development company",
            "product engineering company",
            "custom software development",
        ],
        "web_mobile": [
            "web development company",
            "website design company",
            "mobile app development company",
            "ecommerce development company",
            "UI UX design agency",
        ],
        "digital_marketing": [
            "digital marketing agency",
            "SEO company",
            "social media marketing agency",
            "advertising agency",
            "branding agency",
        ],
        "data_ai": [
            "artificial intelligence company",
            "data analytics company",
        ],
        "cybersecurity": ["cyber security company"],
        "erp_crm": ["ERP software company", "CRM software company"],
        "game_dev": ["game development company"],
        "it_services": [
            "IT company",
            "IT services company",
            "IT consulting company",
            "IT solutions provider",
        ],
    }
    if not categories:
        categories = list(DEFAULT_CATEGORIES)
    out: list[str] = []
    for cat in categories:
        out.extend(by_cat.get(cat, []))
    # Preserve order, drop duplicates.
    return list(dict.fromkeys(out))


# Kolkata and its surrounding municipalities, at roughly the granularity
# Google's local finder treats as distinct. Sweeping per-area is what breaks
# past the ~100-results-per-query ceiling.
KOLKATA_AREAS = [
    "Salt Lake Sector V, Kolkata",
    "Salt Lake Sector 1, Kolkata",
    "Salt Lake Sector 2, Kolkata",
    "Salt Lake Sector 3, Kolkata",
    "New Town, Kolkata",
    "Rajarhat, Kolkata",
    "Baguiati, Kolkata",
    "Kestopur, Kolkata",
    "Lake Town, Kolkata",
    "Dum Dum, Kolkata",
    "Nagerbazar, Kolkata",
    "Sodepur, Kolkata",
    "Barrackpore, Kolkata",
    "Shyambazar, Kolkata",
    "Bagbazar, Kolkata",
    "Ultadanga, Kolkata",
    "Manicktala, Kolkata",
    "Beleghata, Kolkata",
    "Sealdah, Kolkata",
    "Park Street, Kolkata",
    "Esplanade, Kolkata",
    "BBD Bagh, Kolkata",
    "Burrabazar, Kolkata",
    "Entally, Kolkata",
    "Topsia, Kolkata",
    "Tangra, Kolkata",
    "Kasba, Kolkata",
    "Ballygunge, Kolkata",
    "Gariahat, Kolkata",
    "Bhowanipore, Kolkata",
    "Alipore, Kolkata",
    "New Alipore, Kolkata",
    "Behala, Kolkata",
    "Thakurpukur, Kolkata",
    "Tollygunge, Kolkata",
    "Jadavpur, Kolkata",
    "Santoshpur, Kolkata",
    "Garia, Kolkata",
    "Narendrapur, Kolkata",
    "Sonarpur, Kolkata",
    "Bansdroni, Kolkata",
    "Taratala, Kolkata",
    "Howrah",
    "Shibpur, Howrah",
    "Bally, Howrah",
    "Liluah, Howrah",
]
