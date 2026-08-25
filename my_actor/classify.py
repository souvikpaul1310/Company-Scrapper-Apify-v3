"""
classify.py
-----------
Heuristic classifier that decides whether an IT company sells
services, products, or both, based on the language on its website.

The logic is deliberately transparent (weighted keyword hits, not a
black box) so you can read `type_evidence` on any row and immediately
see WHY it was classified that way -- and tune the weights below if
your market uses different vocabulary.
"""

from __future__ import annotations

import re

# (regex, weight) -- higher weight = stronger signal.
PRODUCT_SIGNALS: list[tuple[str, int]] = [
    (r"\bstart(?:ing)?\s+(?:your\s+)?free\s+trial\b", 4),
    (r"\bfree\s+trial\b", 3),
    (r"\bbook\s+a\s+demo\b|\brequest\s+a\s+demo\b|\bget\s+a\s+demo\b", 3),
    (r"\bpricing\s+plans?\b|\bchoose\s+(?:your\s+)?plan\b", 4),
    (r"\bper\s+(?:month|user|seat)\b|\b/mo\b|\bbilled\s+annually\b", 4),
    (r"\bsign\s+up\s+(?:free|now|today)\b", 3),
    (r"\bour\s+(?:product|platform|software|app)\b", 3),
    (r"\bsaas\b|\bsoftware\s+as\s+a\s+service\b", 3),
    (r"\bapi\s+(?:docs|documentation|reference)\b|\bdeveloper\s+docs\b", 3),
    (r"\bchangelog\b|\brelease\s+notes\b|\bwhat'?s\s+new\b", 2),
    (r"\bapp\s*store\b|\bgoogle\s*play\b|\bplay\s*store\b", 3),
    (r"\bfeatures\b", 1),
    (r"\bintegrations?\b", 1),
    (r"\bdownload\s+(?:the\s+)?app\b", 3),
]

SERVICE_SIGNALS: list[tuple[str, int]] = [
    (r"\bhire\s+(?:dedicated\s+)?(?:developers?|engineers?|designers?)\b", 4),
    (r"\bstaff\s+augmentation\b|\bteam\s+augmentation\b", 4),
    (r"\boutsourc\w+\b|\boffshore\s+develop\w+\b", 4),
    (r"\bour\s+services\b|\bservices\s+we\s+offer\b|\bwhat\s+we\s+do\b", 3),
    (r"\bcustom\s+(?:software|web|app|application)\s+develop\w+\b", 3),
    (r"\bget\s+a\s+quote\b|\brequest\s+a\s+quote\b|\bfree\s+consultation\b", 4),
    (r"\bconsult(?:ing|ancy)\b", 2),
    (r"\bcase\s+stud(?:y|ies)\b", 2),
    (r"\bour\s+(?:clients?|portfolio|work)\b", 2),
    (r"\bwe\s+build\b|\bwe\s+develop\b|\bwe\s+design\b", 2),
    (r"\btailor(?:ed|-made)\b|\bbespoke\b|\bend[-\s]to[-\s]end\s+solutions?\b", 2),
    (r"\bhourly\s+rate\b|\bengagement\s+model\b|\bdedicated\s+team\s+model\b", 4),
    (r"\bmaintenance\s+(?:and|&)\s+support\b", 2),
    (r"\btechnolog(?:y|ies)\s+we\s+(?:use|work\s+with)\b", 2),
]

# Compiled once at import.
_PRODUCT = [(re.compile(p, re.I), w) for p, w in PRODUCT_SIGNALS]
_SERVICE = [(re.compile(p, re.I), w) for p, w in SERVICE_SIGNALS]

# A company must clear this to be called anything other than "unknown".
MIN_SCORE = 3
# If the weaker side is at least this fraction of the stronger, call it "both".
BOTH_RATIO = 0.45


def classify_company_type(
    text: str,
    urls_seen: list[str] | None = None,
) -> tuple[str, str, str]:
    """
    Returns (company_type, confidence, evidence).

    company_type: "service" | "product" | "both" | "unknown"
    confidence:   "high" | "medium" | "low"
    evidence:     human-readable summary of what drove the decision
    """
    text = text or ""
    urls_seen = urls_seen or []

    product_score = 0
    product_hits: list[str] = []
    for pattern, weight in _PRODUCT:
        m = pattern.search(text)
        if m:
            product_score += weight
            product_hits.append(m.group(0).strip().lower())

    service_score = 0
    service_hits: list[str] = []
    for pattern, weight in _SERVICE:
        m = pattern.search(text)
        if m:
            service_score += weight
            service_hits.append(m.group(0).strip().lower())

    # A real /pricing or /plans page is one of the strongest product tells.
    for u in urls_seen:
        low = u.lower()
        if re.search(r"/(pricing|plans|subscribe)(/|$|\?)", low):
            product_score += 4
            product_hits.append("has a pricing page")
            break

    for u in urls_seen:
        low = u.lower()
        if re.search(r"/(services|our-services|solutions)(/|$|\?)", low):
            service_score += 3
            service_hits.append("has a services page")
            break

    hi, lo = max(product_score, service_score), min(product_score, service_score)

    if hi < MIN_SCORE:
        return "unknown", "low", "Not enough signal on the site to classify."

    if lo >= MIN_SCORE and lo >= hi * BOTH_RATIO:
        ctype = "both"
    elif product_score > service_score:
        ctype = "product"
    else:
        ctype = "service"

    # Confidence: driven by how strong AND how separated the scores are.
    if hi >= 10 and (ctype == "both" or lo <= hi * 0.3):
        confidence = "high"
    elif hi >= 6:
        confidence = "medium"
    else:
        confidence = "low"

    evidence = (
        f"service={service_score} [{', '.join(sorted(set(service_hits))[:5]) or 'none'}] | "
        f"product={product_score} [{', '.join(sorted(set(product_hits))[:5]) or 'none'}]"
    )
    return ctype, confidence, evidence
