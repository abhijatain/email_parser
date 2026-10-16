"""A deterministic simulated provider that emits real RFC 5322 / MIME emails.

The provider knows nothing about classification or SQLite. Future Gmail and
IMAP adapters can supply raw messages to the same ingestion path instead.
All identities use reserved example.com addresses.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from email.message import EmailMessage
from email.policy import SMTP
from email.utils import format_datetime


@dataclass(frozen=True)
class DemoEmail:
    key: str
    data: bytes
    unread: bool = True
    starred: bool = False


# key, sender name, mailbox, subject, body, age in hours, unread, starred,
# optional attachment (filename, content).
_INITIAL = [
    ("production", "Maya Chen", "maya.chen", "Production incident: checkout errors need attention", "Alex,\n\nThe checkout deployment is returning errors for some customers. This is a customer-impacting production incident. Please review the rollback plan immediately and reply in the incident channel.\n\nCurrent status\n• About 8% of requests are affected.\n• The previous release is available for rollback.\n• Support has a holding response ready.\n\nI am coordinating with infrastructure. We need your approval before the rollback.\n\nMaya", 0.25, True, True, None),
    ("security", "Account Protection", "security", "Security alert: unrecognized sign-in", "Hi Alex,\n\nWe detected an unrecognized sign-in to your account from a new device. If this was not you, review your recent activity and secure your account. Do not share passwords or verification codes with anyone.\n\nThis is a simulated account notification for the demo inbox.", 0.5, True, False, None),
    ("invoice", "Priya Shah", "priya.shah", "Invoice 1042 — approval needed today", "Hi Alex,\n\nThe invoice for the research project is ready. Please approve the payment today so we can close this month's books. The total is $1,280.\n\nI attached the itemized invoice. Let me know if you need a different cost center.\n\nThanks,\nPriya", 1, True, True, ("invoice-1042.txt", "Invoice 1042\nResearch services: $1,280\nDemo document; no payment is due.")),
    ("launch", "Jordan Lee", "jordan.lee", "Launch decision needed: final project review", "Hi Alex,\n\nPlease review the launch proposal and send your decision by end of day. The team is blocked until we confirm the rollout schedule.\n\nWe have completed accessibility testing, updated the help center, and assigned an owner for each launch task. The remaining decision is whether to enable the feature for everyone or begin with a small pilot.\n\nMy recommendation is a staged release. Please reply with your approval or concerns.\n\nJordan", 1.5, True, False, None),
    ("payment", "Billing Team", "billing", "Payment failed — update needed", "Hello Alex,\n\nYour latest subscription payment failed. Please review your billing details to avoid service interruption tomorrow. This is a demo notice; no real account or card is connected.\n\nBilling Team", 2, True, False, None),
    ("flight", "Travel Desk", "travel", "Flight update: please confirm tomorrow's departure", "Hi Alex,\n\nYour flight now departs at 09:40 tomorrow. Please confirm that the revised departure works for you. Check-in opens tonight. Your booking reference is DEMO42.\n\nThe itinerary attachment includes terminal and baggage details.\n\nTravel Desk", 3, True, True, ("itinerary.txt", "Demo itinerary\nFlight AT204\nDeparture 09:40\nTerminal 2\nBooking DEMO42")),
    ("school", "Elena Rivera", "elena.rivera", "School trip permission — please reply today", "Hi Alex,\n\nWe need your permission for Sam's school trip tomorrow. Please reply today so we can finalize the group. The children will visit the science museum and return before regular pickup.\n\nThank you!\nElena", 4, True, False, None),
    ("roadmap", "Maya Chen", "maya.chen", "Project roadmap: notes from our planning meeting", "Hi team,\n\nHere are the notes from our quarterly planning meeting. We agreed to start with the reporting improvements, then revisit onboarding after the research interviews.\n\nMilestones\n1. Share research findings next week.\n2. Draft the revised reporting flows.\n3. Review implementation estimates together.\n\nNothing you need to do right now. I will circulate a meeting invite when the drafts are ready.\n\nMaya", 5, False, True, None),
    ("statement", "Everyday Banking", "bank", "Your monthly account statement is ready", "Hi Alex,\n\nYour monthly bank statement is ready to review. It includes transactions, deposits, and your closing account balance. No payment or action is required.\n\nFor this demo, the attachment contains sample figures only.\n\nEveryday Banking", 6, False, False, ("statement.txt", "Demo statement\nOpening balance: $2,400\nClosing balance: $2,640\nNo real financial data.")),
    ("dinner", "Sam Morgan", "sam.morgan", "Dinner on Saturday?", "Hey Alex,\n\nWant to come over for dinner this weekend? I'm trying the pasta recipe you sent. Bring your favorite board game if you can.\n\nNo rush — we can figure out the details later.\n\nSam", 7, True, True, None),
    ("hotel", "Cedar House Hotel", "cedar.hotel", "Hotel reservation confirmed — Cedar House", "Dear Alex,\n\nYour hotel reservation at Cedar House is confirmed for next month's trip. Check-in begins at 15:00 and breakfast is included. You can change your reservation up to two days before arrival.\n\nWe look forward to welcoming you.\n\nCedar House", 8, False, False, None),
    ("delivery", "Parcel Updates", "parcel", "Your order has shipped", "Hi Alex,\n\nYour order has shipped and the package is on its way. Delivery is expected in three business days. Tracking reference: DEMO-PARCEL-18.\n\nNo action is needed.\n\nParcel Updates", 9, True, False, None),
    ("design", "Noah Williams", "noah.williams", "Design review: a calmer project dashboard", "Hi Alex,\n\nI have shared the latest dashboard design with the team. The new layout groups related project tasks and gives status updates more breathing room.\n\nPlease review the proposal when you have a chance and send any feedback before next week's meeting.\n\nNoah", 10, True, False, None),
    ("verification", "Account Protection", "security", "Your verification code", "Hi Alex,\n\nYour verification code is DEMO-000. This code is a fictional example and cannot be used to access any account. Never share real verification codes.\n\nIf you did not request this code, check your account security settings.\n\nAccount Protection", 11, False, False, None),
    ("newsletter", "The Thoughtful Internet", "thoughtful", "The Thoughtful Internet — weekly reading list", "Hello readers,\n\nThis week's newsletter explores small teams doing careful work.\n\n1. Why good documentation saves a thousand meetings.\n2. A field guide to urban gardens.\n3. Building tools that respect attention.\n\nSet aside a few quiet minutes and enjoy the reading list. You can unsubscribe whenever you like.\n\nThe editors", 12, True, False, None),
    ("sale", "Field Supply", "field.supply", "URGENT: our biggest sale ends tonight", "Last chance! Shop now for 40% off bags and outdoor essentials. This limited-time offer ends tonight — act immediately to save.\n\nThis is a promotional email. Unsubscribe from offers in your preferences.\n\nField Supply", 13, True, False, None),
    ("coffee", "Aisha Patel", "aisha.patel", "Coffee and a catch up next week", "Hey Alex,\n\nIt has been too long! Would you like to grab coffee next week? I'd love to hear about your new place and tell you about the pottery class.\n\nAisha", 15, False, False, None),
    ("tax", "Rowan Ellis", "rowan.ellis", "Tax documents for your records", "Hi Alex,\n\nThe annual tax summary is attached for your records. Everything is reconciled and there is no outstanding balance. Keep a copy with your other financial documents.\n\nRowan", 17, False, False, ("tax-summary.txt", "Annual tax summary\nFictional demo document. No tax filing information.")),
    ("train", "Rail Reservations", "rail", "Train booking confirmation", "Hello Alex,\n\nYour train booking is confirmed. The itinerary is saved under reservation DEMO-TRAIN-7. You will receive platform information closer to your departure.\n\nRail Reservations", 18, True, False, None),
    ("club", "Neighborhood Desk", "neighborhood", "Book club notes and next month's pick", "Hi neighbors,\n\nThanks for a lovely book club evening. Next month's pick is The Long Way Home. We will meet at the library, and everyone is welcome to bring a friend.\n\nHappy reading!", 20, False, False, None),
    ("digest", "Studio Notes", "studio.notes", "Studio Notes newsletter: thoughtful spaces", "This edition of our newsletter visits a tiny workshop, shares interviews with independent makers, and collects five beautiful objects.\n\nRead it whenever you have time.\n\nStudio Notes\nUnsubscribe from this newsletter at any time.", 22, False, False, None),
    ("return", "Oak & Thread", "oak.thread", "Return label for your order", "Hi Alex,\n\nYour return label is attached. Pack the item in its original box and hand it to the carrier when convenient. Your refund will be processed after the package arrives.\n\nOak & Thread", 24, False, False, ("return-label.txt", "DEMO RETURN LABEL\nOrder 8214\nNot valid for postage.")),
    ("community", "Community Center", "community", "New opening hours at the community center", "Hello Alex,\n\nThe community center will open from 08:00 to 20:00 starting next month. The garden entrance will also be available during these hours.\n\nThank you for being part of the neighborhood.", 26, True, False, None),
    ("archive", "Archive Desk", "archive", "Your requested reference material", "Hello Alex,\n\nWe found the reference material you requested. It is available in the reading room under reference DEMO-119. You can stop by at a convenient time.\n\nArchive Desk", 28, False, False, None),
    ("release", "Jordan Lee", "jordan.lee", "Release notes: reporting improvements", "Hi team,\n\nThe latest release includes faster project reports, clearer empty states, and a new export flow. The deployment completed successfully.\n\nThank you to everyone who contributed. No follow-up work is needed.\n\nJordan", 30, False, False, None),
    ("garden", "Garden Journal", "garden.journal", "Garden Journal digest: a little more green", "In this week's digest: shade-loving plants, a guide to composting in small spaces, and a tour of a community orchard.\n\nA quiet weekend read from Garden Journal. Unsubscribe whenever you like.", 32, True, False, None),
    ("receipt", "Workshop Store", "workshop.store", "Receipt for your recent purchase", "Hi Alex,\n\nThank you for your purchase. Your order of notebooks and pencils has been delivered. This receipt is for your records and no action is needed.\n\nWorkshop Store", 35, False, False, None),
    ("appointment", "Harbor Clinic", "harbor.clinic", "Appointment reminder — please confirm tomorrow", "Hi Alex,\n\nThis is a reminder of your routine appointment tomorrow at 10:30. Please confirm your attendance by replying to this message.\n\nIf you need to reschedule, let us know.\n\nHarbor Clinic", 37, True, False, None),
]

_BATCHES = [
    [
        ("sync1-review", "Maya Chen", "maya.chen", "Client proposal: approval needed today", "Hi Alex,\n\nPlease approve the client proposal today. The deadline is this evening and the project team needs your decision to proceed.\n\nMaya", 0.02, True, False, None),
        ("sync1-family", "Sam Morgan", "sam.morgan", "Family picnic photos", "Hey Alex,\n\nThe family picnic photos are ready. There is a great one of everyone by the lake. What a lovely weekend!\n\nSam", 0.05, True, False, None),
        ("sync1-delivery", "Parcel Updates", "parcel", "Your package has been delivered", "Hi Alex,\n\nYour order has been delivered to the reception desk. No action is required.\n\nParcel Updates", 0.1, True, False, None),
        ("sync1-reading", "The Thoughtful Internet", "thoughtful", "Weekly reading list: making room for focus", "This week's newsletter includes a reading list about better tools, calmer work, and the value of a long walk. Unsubscribe at any time.", 0.15, True, False, None),
    ],
    [
        ("sync2-security", "Account Protection", "security", "Security alert: password changed", "Hello Alex,\n\nYour password was changed from a new device. If you did not make this change, secure your account immediately. This is a simulated notice.\n\nAccount Protection", 0.02, True, False, None),
        ("sync2-travel", "Travel Desk", "travel", "Gate change for today's flight", "Hi Alex,\n\nYour flight today will now leave from gate B14. Please confirm the updated boarding information before heading to the airport.\n\nTravel Desk", 0.05, True, False, None),
        ("sync2-bank", "Everyday Banking", "bank", "Payment received — thank you", "Hi Alex,\n\nWe received your account payment. Your outstanding balance is now zero. No action is needed.\n\nEveryday Banking", 0.1, True, False, None),
        ("sync2-offer", "Field Supply", "field.supply", "Last chance: urgent weekend sale", "Our weekend sale ends tonight! Shop now to get 30% off. This is a promotional offer; unsubscribe anytime.", 0.15, True, False, None),
    ],
    [
        ("sync3-invoice", "Priya Shah", "priya.shah", "Invoice overdue — action required", "Hi Alex,\n\nThe invoice payment is overdue. Please review and pay today to prevent service interruption tomorrow. This demo invoice does not request a real payment.\n\nPriya", 0.02, True, False, None),
        ("sync3-work", "Noah Williams", "noah.williams", "Project research: interview summary", "Hi team,\n\nI have compiled the interview notes for the project. The key themes are clearer navigation, better search, and a simpler first-run experience. We can discuss these at next week's meeting.\n\nNoah", 0.05, True, False, ("research-notes.txt", "Demo research summary\nNavigation, search, onboarding.")),
        ("sync3-personal", "Aisha Patel", "aisha.patel", "Birthday dinner — RSVP today?", "Hey Alex,\n\nI'm booking a table for my birthday dinner tomorrow. Please reply today if you can make it so I can confirm the numbers.\n\nAisha", 0.1, True, False, None),
        ("sync3-other", "Archive Desk", "archive", "Reference material ready for collection", "Hello Alex,\n\nThe reference material is ready to collect at the front desk. Stop by when convenient.\n\nArchive Desk", 0.15, True, False, None),
    ],
]

BATCH_COUNT = len(_BATCHES)


def messages_for_batch(batch: int, received_at: datetime) -> list[DemoEmail]:
    """Return the seed (0) or one of three finite simulated sync batches."""
    if not isinstance(batch, int) or not 0 <= batch <= BATCH_COUNT:
        raise ValueError("Unknown demo batch")
    rows = _INITIAL if batch == 0 else _BATCHES[batch - 1]
    messages = []
    for key, name, mailbox, subject, body, age, unread, starred, attachment in rows:
        message = EmailMessage(policy=SMTP)
        message["From"] = f"{name} <{mailbox}@example.com>"
        message["To"] = "Alex Morgan <alex.morgan@example.com>"
        message["Subject"] = subject
        message["Date"] = format_datetime(received_at - timedelta(hours=age))
        message["Message-ID"] = f"<{key}@example.com>"
        if "newsletter" in body.lower() or "digest" in subject.lower():
            message["List-ID"] = f"<{mailbox}.example.com>"
        message.set_content(body)
        if attachment:
            filename, content = attachment
            message.add_attachment(content, subtype="plain", filename=filename)
        messages.append(DemoEmail(key, message.as_bytes(), unread, starred))
    return messages
