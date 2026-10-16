"""Explainable, local email classification with no external services.

These rules are a useful starting point for a personal inbox, not a security
verdict. A provider can feed the same parsed messages to a different classifier
later without changing the inbox's storage or HTTP interfaces.
"""

from __future__ import annotations

import re
from datetime import datetime, timezone

from .parser import ParsedEmail


CATEGORIES = [
    {"id": "work", "name": "Work", "description": "Projects, teammates, and decisions."},
    {"id": "finance", "name": "Finance", "description": "Bills, payments, and account statements."},
    {"id": "personal", "name": "Personal", "description": "Friends, family, and everyday plans."},
    {"id": "travel", "name": "Travel", "description": "Trips, tickets, and reservations."},
    {"id": "shopping", "name": "Shopping", "description": "Orders, deliveries, and offers."},
    {"id": "newsletters", "name": "Newsletters", "description": "Reading lists and regular updates."},
    {"id": "security", "name": "Security", "description": "Sign-ins and account protection."},
    {"id": "other", "name": "Other", "description": "Messages that do not fit another category."},
]
PRIORITIES = ["critical", "high", "normal", "low"]

# Expressions are bounded to avoid substring matches such as "bill" in "billion".
_CATEGORY_RULES = {
    "work": (r"project|team|teammate|meeting|standup|roadmap|sprint|proposal|client|"
             r"stakeholder|review|deployment|production|incident|release|approval|"
             r"design|launch|deliverable|quarterly planning"),
    "finance": (r"invoice|payment|statement|billing|bill|bank|transaction|tax|"
                r"salary|payroll|budget|mortgage|rent|financial|debit|credit card|"
                r"account balance|subscription renewal"),
    "personal": (r"family|dinner|birthday|weekend|picnic|coffee|mom|dad|mum|"
                 r"grandma|grandpa|book club|see you|catch up|lunch|wedding|"
                 r"doctor|appointment|school|parent|potluck|anniversary"),
    "travel": (r"flight|boarding|itinerary|airline|airport|hotel|reservation|"
               r"check.in|travel|train|trip|passport|booking|departure|gate change"),
    "shopping": (r"order|delivery|delivered|shipment|shipped|package|tracking|"
                 r"purchase|store|sale|discount|coupon|shopping|promotion|"
                 r"collection|refund|return label"),
    "newsletters": (r"newsletter|digest|weekly reads|reading list|edition|"
                    r"subscribe|unsubscribe|roundup|round.up|issue #?\d+"),
    "security": (r"security|sign.in|login|password|verification code|"
                 r"two.factor|2fa|authentication|unauthorized|unrecognized|"
                 r"suspicious|compromised|account locked|new device|recovery code"),
}


def _has(pattern: str, text: str) -> bool:
    return re.search(r"\b(?:" + pattern + r")\b", text, re.I) is not None


def _deadline_soon(text: str, now: datetime) -> bool:
    if _has(r"today|tomorrow|tonight|this afternoon|this evening|within 24 hours|"
            r"end of (?:the )?day|eod|in (?:one|1|two|2) hours", text):
        return True
    # Explicit ISO dates are common in automated billing / task notifications.
    for value in re.findall(r"\b\d{4}-\d{2}-\d{2}\b", text):
        try:
            days = (datetime.fromisoformat(value).date() - now.date()).days
        except ValueError:
            continue
        if 0 <= days <= 2:
            return True
    return False


def classify_email(parsed: ParsedEmail, now: datetime | None = None) -> dict:
    """Return deterministic category, priority, score, labels, and explanations.

    Subject matches carry more weight than body matches. Explicit newsletters
    and sales are suppressed before urgency scoring, so a marketing subject
    such as "URGENT: sale ends tonight" does not displace an actual bill.
    """
    now = now or datetime.now(timezone.utc)
    subject = parsed.subject.lower()
    sender = " ".join(f"{item.name} {item.address}" for item in parsed.from_).lower()
    body = (parsed.text or re.sub(r"<[^>]+>", " ", parsed.html)).lower()
    content = f"{subject}\n{body}"
    headers = {item["name"].lower(): item["value"] for item in parsed.headers}

    promotion_pattern = (r"sale|discount|coupon|promo(?:tion|tional)?|shop now|"
                         r"limited.time offer|\d+% off|black friday|exclusive offer|"
                         r"clearance|flash deal")
    promotion = _has(promotion_pattern, content)
    promotion_subject = _has(promotion_pattern, subject)
    newsletter_subject = _has(
        r"newsletter|digest|weekly reads|weekly roundup|reading list|issue #?\d+", subject
    )
    # List-ID identifies a distribution list, which may deliver operational
    # alerts rather than reading material. Likewise, discount wording can be
    # part of a client decision or a transactional message's marketing footer.
    transactional = _has(r"invoice|payment (?:failed|due|received)|statement|"
                         r"order (?:confirmation|confirmed|shipped)|receipt|"
                         r"security alert|suspicious|unrecognized|unauthorized|"
                         r"password (?:was |has been )?changed|new (?:sign.in|login)|"
                         r"account (?:locked|at risk)|verification code", subject)
    operational = _has(
        r"production (?:incident|outage)|service outage|critical incident|sev.?1|p0 incident",
        subject,
    ) or (
        _has(r"client|project|deployment|release|launch", subject)
        and _has(r"approval|decision|blocked|blocking|incident|outage|action required", subject)
    )
    newsletter = bool(headers.get("list-id")) or newsletter_subject
    # Clear subscription/sale subjects keep their suppression even if their
    # body contains alarmist operational or security vocabulary. Strong direct
    # subjects take precedence over incidental body and distribution signals.
    if newsletter_subject:
        promotion = False
    elif promotion_subject:
        newsletter = False
    elif transactional or operational:
        promotion = False
        newsletter = False

    scores = {}
    for category, expression in _CATEGORY_RULES.items():
        scores[category] = (
            4 * len(re.findall(r"\b(?:" + expression + r")\b", subject))
            + 2 * len(re.findall(r"\b(?:" + expression + r")\b", sender))
            + min(5, len(re.findall(r"\b(?:" + expression + r")\b", body)))
        )
    if newsletter:
        category = "newsletters"
    elif promotion:
        category = "shopping"
    else:
        category = max(scores, key=scores.get)
        if scores[category] == 0:
            category = "other"

    name = next(item["name"] for item in CATEGORIES if item["id"] == category)
    labels = [name]
    reasons = [
        f"Classified as {name.lower()} from the subject, sender, and message content."
        if category != "other" else "No strong category signals; placed in Other."
    ]
    base = {"work": 43, "finance": 46, "personal": 42, "travel": 43,
            "shopping": 36, "newsletters": 18, "security": 62, "other": 38}
    score = base[category]
    # Automated receipts often say "no action required". Remove only those
    # explicit negated phrases, preserving genuine requests elsewhere.
    action_content = re.sub(
        r"\b(?:no|not|without) (?:immediate )?(?:action|response|approval|payment) "
        r"(?:is )?(?:required|needed|due)\b", "", content,
    )
    urgency_content = re.sub(r"\b(?:not|isn't|is not) urgent\b", "", content)
    action = _has(r"action required|requires? (?:your )?(?:action|approval|attention)|"
                  r"please (?:review|approve|confirm|reply|respond|send|sign|pay)|"
                  r"need (?:you|your)|response (?:required|needed)|approval needed|"
                  r"decision needed|payment due|balance due|rsvp", action_content)
    urgent = _has(r"urgent|asap|immediately|immediate action|time.sensitive|"
                  r"overdue|past due|blocking|blocked|deadline", urgency_content)
    soon = _deadline_soon(content, now)
    security_risk = category == "security" and _has(
        r"suspicious|unrecognized|unauthorized|compromised|security alert|"
        r"unusual (?:activity|sign.in)|account (?:locked|at risk)|"
        r"password (?:was |has been )?changed|new (?:sign.in|login)", content
    )
    incident = category == "work" and _has(
        r"production (?:incident|outage)|service outage|customer.impacting|"
        r"critical incident|sev.?1|p0 incident", content
    )
    failed_payment = category == "finance" and _has(
        r"payment (?:failed|declined|overdue)|past due|overdue|service (?:suspension|interruption)",
        content,
    )

    if promotion or newsletter:
        score = 14 if promotion else 20
        labels.append("Promotional" if promotion else "Reading")
        reasons.append("Promotional or subscription content is kept at low priority, even when it uses urgent wording.")
    else:
        if action:
            score += 16
            labels.append("Action required")
            reasons.append("The message asks for a reply, approval, confirmation, or payment.")
        if soon:
            score += 14
            labels.append("Due soon")
            reasons.append("The message mentions today, tomorrow, or a date within the next two days.")
        if urgent:
            score += 15
            labels.append("Time sensitive")
            reasons.append("The message contains urgency, a deadline, or a blocking issue.")
        if failed_payment:
            score = max(score, 80)
            labels.append("Payment attention")
            reasons.append("A failed or overdue payment may need attention to avoid interruption.")
        if security_risk:
            score = max(score, 91)
            labels.append("Account alert")
            reasons.append("The message reports unusual account activity or an account security change.")
        if incident:
            score = max(score, 94)
            labels.append("Incident")
            reasons.append("A production or customer-facing incident needs prompt attention.")
        if category == "shopping" and not action and not urgent and not soon:
            score = 30
            reasons.append("Routine order and delivery updates do not request action.")
        if len(reasons) == 1:
            reasons.append("No urgent deadline or explicit action request was detected.")
    if parsed.attachments:
        labels.append("Attachment")
    score = max(0, min(100, score))
    priority = "critical" if score >= 85 else "high" if score >= 65 else "normal" if score >= 35 else "low"
    return {"category": category, "priority": priority, "score": score,
            "labels": labels, "reasons": reasons}
