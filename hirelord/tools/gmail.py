"""
Hire Lord — Gmail Tool
========================
Handles all Gmail interactions:
  1. Job alert email parsing (LinkedIn/Indeed/Glassdoor alerts → job URLs)
  2. Employer reply monitoring (classify + draft responses)
  3. Email sending (follow-ups, cover letters)

First run: opens browser for OAuth consent.
Subsequent runs: uses cached token.json automatically.

Setup:
  1. Place credentials.json in project root
  2. Run: uv run python -m hirelord.tools.gmail --auth
     (opens browser, saves token.json)
"""

import base64
import email as email_lib
import json
import os
import re
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional

from dotenv import load_dotenv
load_dotenv()

# Google API imports
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build

ROOT = Path(__file__).parent.parent.parent

CREDENTIALS_FILE = ROOT / "credentials.json"
TOKEN_FILE       = ROOT / "token.json"

SCOPES = [
    "https://www.googleapis.com/auth/gmail.readonly",
    "https://www.googleapis.com/auth/gmail.send",
    "https://www.googleapis.com/auth/gmail.modify",
]

# ── Auth ──────────────────────────────────────────────────────────────────────

def get_gmail_service():
    """
    Authenticate and return a Gmail API service object.
    Opens browser on first run to get consent.
    Uses cached token.json on subsequent runs.
    """
    creds = None

    if TOKEN_FILE.exists():
        creds = Credentials.from_authorized_user_file(str(TOKEN_FILE), SCOPES)

    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            if not CREDENTIALS_FILE.exists():
                raise FileNotFoundError(
                    f"credentials.json not found at {CREDENTIALS_FILE}\n"
                    "Download it from Google Cloud Console → APIs & Services → Credentials"
                )
            flow = InstalledAppFlow.from_client_secrets_file(
                str(CREDENTIALS_FILE), SCOPES
            )
            creds = flow.run_local_server(port=0)

        TOKEN_FILE.write_text(creds.to_json())

    return build("gmail", "v1", credentials=creds)


# ── Email parsing helpers ─────────────────────────────────────────────────────

def decode_body(part: dict) -> str:
    """Decode base64-encoded email body."""
    data = part.get("body", {}).get("data", "")
    if not data:
        return ""
    return base64.urlsafe_b64decode(data + "==").decode("utf-8", errors="ignore")


def get_email_body(msg: dict) -> tuple[str, str]:
    """
    Extract plain text and HTML body from a Gmail message.
    Returns (plain_text, html).
    """
    plain = ""
    html  = ""
    payload = msg.get("payload", {})
    parts   = payload.get("parts", [])

    if not parts:
        # Single-part message
        mime = payload.get("mimeType", "")
        body = decode_body(payload)
        if "plain" in mime:
            plain = body
        elif "html" in mime:
            html = body
        return plain, html

    def extract_parts(parts_list):
        nonlocal plain, html
        for part in parts_list:
            mime = part.get("mimeType", "")
            if mime == "text/plain":
                plain += decode_body(part)
            elif mime == "text/html":
                html += decode_body(part)
            elif mime.startswith("multipart/"):
                extract_parts(part.get("parts", []))

    extract_parts(parts)
    return plain, html


def get_header(msg: dict, name: str) -> str:
    """Get a specific header value from a Gmail message."""
    headers = msg.get("payload", {}).get("headers", [])
    for h in headers:
        if h.get("name", "").lower() == name.lower():
            return h.get("value", "")
    return ""


# ── Job alert URL extraction ──────────────────────────────────────────────────

# LinkedIn job alert URL patterns
LINKEDIN_JOB_PATTERNS = [
    r"https://www\.linkedin\.com/comm/jobs/view/(\d+)",
    r"https://www\.linkedin\.com/jobs/view/(\d+)",
]

# Indeed job alert URL patterns
INDEED_JOB_PATTERNS = [
    r"https://(?:www\.)?indeed\.com/(?:rc/clk|viewjob)\?[^\s\"'<>]+jk=([a-z0-9]+)",
    r"https://(?:www\.)?indeed\.com/m/basecamp/viewjob\?[^\s\"'<>]+viewJobId=([a-z0-9]+)",
]

# Glassdoor job alert URL patterns
GLASSDOOR_JOB_PATTERNS = [
    r"https://(?:www\.)?glassdoor\.com/job-listing/[^\s\"'<>]+jobListingId=(\d+)",
    r"https://(?:www\.)?glassdoor\.com/partner/jobListing[^\s\"'<>]+jobListingId=(\d+)",
]

# Senders that indicate job alert emails
JOB_ALERT_SENDERS = [
    "jobalerts-noreply@linkedin.com",
    "jobs-listings@linkedin.com",
    "noreply@indeed.com",
    "alert@indeed.com",
    "noreply@glassdoor.com",
    "alerts@glassdoor.com",
    "noreply@ziprecruiter.com",
]


def extract_job_urls_from_email(plain_text: str, html: str) -> list[dict]:
    """
    Extract job URLs and IDs from a job alert email.
    Returns list of dicts with source and url/job_id.
    """
    results = []
    combined = plain_text + " " + html

    # LinkedIn
    for pattern in LINKEDIN_JOB_PATTERNS:
        for match in re.finditer(pattern, combined):
            job_id = match.group(1)
            results.append({
                "source": "linkedin",
                "job_id": job_id,
                "url": f"https://www.linkedin.com/jobs/view/{job_id}",
            })

    # Indeed
    for pattern in INDEED_JOB_PATTERNS:
        for match in re.finditer(pattern, combined):
            results.append({
                "source": "indeed",
                "job_id": match.group(1),
                "url": match.group(0),
            })

    # Glassdoor
    for pattern in GLASSDOOR_JOB_PATTERNS:
        for match in re.finditer(pattern, combined):
            results.append({
                "source": "glassdoor",
                "job_id": match.group(1),
                "url": match.group(0),
            })

    # Deduplicate by job_id
    seen = set()
    deduped = []
    for r in results:
        key = f"{r['source']}:{r['job_id']}"
        if key not in seen:
            seen.add(key)
            deduped.append(r)

    return deduped


def is_job_alert_email(sender: str, subject: str) -> bool:
    """Determine if an email is a job alert."""
    sender_lower = sender.lower()
    subject_lower = subject.lower()

    if any(s in sender_lower for s in JOB_ALERT_SENDERS):
        return True

    job_alert_keywords = [
        "job alert", "jobs for you", "new jobs matching",
        "job recommendations", "jobs you might like",
        "new job matches", "job digest",
    ]
    if any(kw in subject_lower for kw in job_alert_keywords):
        return True

    return False


# ── Employer reply classification ─────────────────────────────────────────────

EMPLOYER_REPLY_KEYWORDS = {
    "interview_request": [
        "interview", "schedule", "availability", "call", "meet",
        "zoom", "teams", "google meet", "phone screen", "video call",
        "chat", "speak", "connect",
    ],
    "rejection": [
        "not moving forward", "decided to move", "other candidates",
        "not a fit", "regret", "unfortunately", "position has been filled",
        "not selected", "won't be moving", "other direction",
    ],
    "offer": [
        "offer", "compensation", "salary", "start date", "onboarding",
        "congratulations", "pleased to offer", "welcome to the team",
    ],
    "info_request": [
        "question", "clarify", "additional information", "portfolio",
        "references", "sample", "tell me more", "could you share",
    ],
}


def classify_employer_email(subject: str, body: str) -> str:
    """
    Classify an employer email into a category.
    Returns: interview_request | rejection | offer | info_request | unknown
    """
    combined = (subject + " " + body).lower()
    scores = {}
    for category, keywords in EMPLOYER_REPLY_KEYWORDS.items():
        scores[category] = sum(1 for kw in keywords if kw in combined)

    if not any(scores.values()):
        return "unknown"

    return max(scores, key=scores.get)


# ── Gmail API operations ──────────────────────────────────────────────────────

def fetch_recent_job_alerts(days_back: int = 2) -> list[dict]:
    """
    Fetch recent job alert emails from Gmail.
    Returns list of dicts with email metadata + extracted job URLs.
    """
    service = get_gmail_service()
    after   = (datetime.now() - timedelta(days=days_back)).strftime("%Y/%m/%d")

    # Search for job alert emails
    query = (
        f"after:{after} ("
        f"from:jobalerts-noreply@linkedin.com OR "
        f"from:jobs-listings@linkedin.com OR "
        f"from:noreply@indeed.com OR "
        f"from:alert@indeed.com OR "
        f"from:noreply@glassdoor.com OR "
        f"subject:\"job alert\" OR "
        f"subject:\"jobs for you\" OR "
        f"subject:\"new jobs matching\""
        f")"
    )

    results = service.users().messages().list(
        userId="me", q=query, maxResults=20
    ).execute()

    messages = results.get("messages", [])
    emails   = []

    for msg_ref in messages:
        msg = service.users().messages().get(
            userId="me", id=msg_ref["id"], format="full"
        ).execute()

        sender  = get_header(msg, "from")
        subject = get_header(msg, "subject")
        date    = get_header(msg, "date")

        if not is_job_alert_email(sender, subject):
            continue

        plain, html = get_email_body(msg)
        job_urls    = extract_job_urls_from_email(plain, html)

        if job_urls:
            emails.append({
                "message_id": msg_ref["id"],
                "sender":     sender,
                "subject":    subject,
                "date":       date,
                "job_urls":   job_urls,
                "job_count":  len(job_urls),
            })

        # Mark as read
        service.users().messages().modify(
            userId="me",
            id=msg_ref["id"],
            body={"removeLabelIds": ["UNREAD"]},
        ).execute()

    return emails


def fetch_employer_replies(days_back: int = 7) -> list[dict]:
    """
    Fetch potential employer reply emails from Gmail.
    Returns classified emails with draft responses.
    """
    from ..db.store import get_full_pipeline

    service = get_gmail_service()
    after   = (datetime.now() - timedelta(days=days_back)).strftime("%Y/%m/%d")

    # Get our active applications to match against
    pipeline = []
    try:
        import asyncio
        pipeline = asyncio.get_event_loop().run_until_complete(get_full_pipeline())
    except Exception:
        pass

    # Build set of companies we've applied to
    applied_companies = {
        r["company"].lower()
        for r in pipeline
        if r.get("app_status") == "submitted"
    }

    query = f"after:{after} in:inbox -from:linkedin.com -from:indeed.com -from:glassdoor.com"

    results = service.users().messages().list(
        userId="me", q=query, maxResults=50
    ).execute()

    messages = results.get("messages", [])
    replies  = []

    for msg_ref in messages:
        msg = service.users().messages().get(
            userId="me", id=msg_ref["id"], format="full"
        ).execute()

        sender  = get_header(msg, "from")
        subject = get_header(msg, "subject")
        date    = get_header(msg, "date")
        plain, _ = get_email_body(msg)

        # Check if sender domain matches a company we applied to
        sender_domain = sender.split("@")[-1].split(">")[0].lower() if "@" in sender else ""
        company_match = None

        for company in applied_companies:
            company_words = company.split()
            if any(word in sender_domain for word in company_words if len(word) > 3):
                company_match = company
                break

        if not company_match and not any(
            kw in (subject + plain).lower()
            for kw in ["application", "interview", "position", "role", "opportunity"]
        ):
            continue

        category = classify_employer_email(subject, plain)
        if category == "unknown" and not company_match:
            continue

        replies.append({
            "message_id":    msg_ref["id"],
            "sender":        sender,
            "sender_domain": sender_domain,
            "subject":       subject,
            "date":          date,
            "body":          plain[:2000],
            "category":      category,
            "company_match": company_match,
        })

    return replies


def send_email(to: str, subject: str, body: str, reply_to_id: str = "") -> bool:
    """Send an email via Gmail API."""
    service = get_gmail_service()

    msg = email_lib.mime.text.MIMEText(body)
    msg["to"]      = to
    msg["subject"] = subject
    if reply_to_id:
        msg["In-Reply-To"] = reply_to_id
        msg["References"]  = reply_to_id

    raw = base64.urlsafe_b64encode(msg.as_bytes()).decode()

    try:
        service.users().messages().send(
            userId="me",
            body={"raw": raw},
        ).execute()
        return True
    except Exception as e:
        print(f"  ❌ Email send failed: {e}")
        return False


# ── Auth CLI ──────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import sys
    if "--auth" in sys.argv:
        print("Opening browser for Gmail authentication...")
        service = get_gmail_service()
        profile = service.users().getProfile(userId="me").execute()
        print(f"✅ Authenticated as: {profile['emailAddress']}")
        print(f"✅ token.json saved to {TOKEN_FILE}")
        print("\nHire Lord can now access your Gmail.")
    elif "--test-alerts" in sys.argv:
        print("Fetching recent job alert emails...")
        alerts = fetch_recent_job_alerts(days_back=7)
        print(f"Found {len(alerts)} job alert emails")
        for a in alerts:
            print(f"\n  From:    {a['sender']}")
            print(f"  Subject: {a['subject']}")
            print(f"  Jobs:    {a['job_count']}")
            for j in a["job_urls"]:
                print(f"    {j['source']}: {j['url']}")
    elif "--test-replies" in sys.argv:
        print("Checking for employer replies...")
        replies = fetch_employer_replies(days_back=7)
        print(f"Found {len(replies)} potential employer emails")
        for r in replies:
            print(f"\n  From:     {r['sender']}")
            print(f"  Subject:  {r['subject']}")
            print(f"  Category: {r['category']}")
    else:
        print("Usage:")
        print("  uv run python -m hirelord.tools.gmail --auth")
        print("  uv run python -m hirelord.tools.gmail --test-alerts")
        print("  uv run python -m hirelord.tools.gmail --test-replies")
