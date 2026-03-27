"""
Hire Lord — Gmail Agent v2 (Phase 3)
======================================
Updated to use Playwright LinkedIn scraper for full JD extraction.

Flow:
  Gmail job alert emails
      │ extract job IDs
      ↓
  LinkedIn Playwright scraper (authenticated session)
      │ fetch full JD + detect apply type
      ↓
  JD Parser (Haiku) → structured fields
      ↓
  Screener (Haiku) → match score
      ↓
  [if strong/good] → save to DB → queue for tailoring
"""

import asyncio
import json
import os
from datetime import datetime
from dotenv import load_dotenv
load_dotenv()

from langchain_anthropic import ChatAnthropic
from langchain_core.messages import SystemMessage, HumanMessage
from rich.console import Console
from rich.panel import Panel
from rich.table import Table
from rich import box
from rich.prompt import Prompt

from ..tools.gmail import (
    fetch_recent_job_alerts,
    fetch_employer_replies,
    send_email,
)
from ..tools.resume import parse_job_description
from ..tools.linkedin_scraper import fetch_linkedin_jobs_batch, LinkedInJob
from ..db.store import (
    upsert_job, update_job_screening,
    get_full_pipeline, update_application_status,
)
from ..prompts.tailor import JOB_MATCH_SYSTEM, JOB_MATCH_HUMAN

console = Console()

_haiku  = None
_sonnet = None

def get_haiku():
    global _haiku
    if _haiku is None:
        _haiku = ChatAnthropic(model="claude-haiku-4-5-20251001", temperature=0)
    return _haiku

def get_sonnet():
    global _sonnet
    if _sonnet is None:
        _sonnet = ChatAnthropic(model="claude-sonnet-4-6", temperature=0.3)
    return _sonnet


# ── Gmail Discovery ───────────────────────────────────────────────────────────

async def run_gmail_discovery(days_back: int = 2) -> dict:
    """
    Full pipeline: Gmail alerts → Playwright scrape → parse → screen → save.
    """
    console.print("\n[bold gold1]👑 Hire Lord — Gmail Job Discovery[/bold gold1]")
    console.print(f"[dim]{datetime.now().strftime('%B %d, %Y at %I:%M %p')} MT[/dim]\n")

    # Step 1: Fetch job alert emails
    console.print("[cyan]Step 1: Scanning Gmail for job alert emails...[/cyan]")
    alerts = fetch_recent_job_alerts(days_back=days_back)

    if not alerts:
        console.print("[dim]No job alert emails found in the last "
                      f"{days_back} days.[/dim]")
        return {"emails_processed": 0, "jobs_found": 0, "jobs_saved": 0}

    console.print(f"  Found [bold]{len(alerts)}[/bold] job alert emails")

    # Collect all job refs
    all_refs = []
    for alert in alerts:
        console.print(f"\n  📧 [dim]{alert['subject'][:65]}[/dim]")
        console.print(f"     {alert['job_count']} jobs")
        all_refs.extend(alert["job_urls"])

    # Deduplicate
    seen = set()
    unique_refs = []
    for ref in all_refs:
        key = f"{ref['source']}:{ref['job_id']}"
        if key not in seen:
            seen.add(key)
            unique_refs.append(ref)

    linkedin_ids = [r["job_id"] for r in unique_refs if r["source"] == "linkedin"]
    other_refs   = [r for r in unique_refs if r["source"] != "linkedin"]

    console.print(
        f"\n  [bold]{len(unique_refs)}[/bold] unique jobs "
        f"({len(linkedin_ids)} LinkedIn, {len(other_refs)} other)"
    )

    # Step 2: Fetch full JDs via Playwright
    console.print(f"\n[cyan]Step 2: Fetching full job descriptions via LinkedIn...[/cyan]")
    console.print("  [dim](Using your saved LinkedIn session)[/dim]\n")

    linkedin_jobs: list[LinkedInJob] = []
    if linkedin_ids:
        linkedin_jobs = await fetch_linkedin_jobs_batch(
            linkedin_ids,
            headless=True,
            delay_between=2.5,
            max_concurrent=2,  # Conservative — be a good citizen
        )

    console.print(f"\n  Fetched [bold]{len(linkedin_jobs)}[/bold] / "
                  f"{len(linkedin_ids)} LinkedIn jobs")

    if not linkedin_jobs:
        console.print("[yellow]  ⚠  No jobs fetched — check LinkedIn session[/yellow]")
        console.print("  [dim]Run: uv run python -m hirelord.tools.linkedin_auth[/dim]")

    # Step 3: Parse + Screen + Save
    console.print(f"\n[cyan]Step 3: Parsing, screening, and saving...[/cyan]")

    jobs_saved  = 0
    jobs_queued = 0

    for i, job in enumerate(linkedin_jobs):
        console.print(f"\n  [{i+1}/{len(linkedin_jobs)}] "
                      f"[bold]{job.title}[/bold] @ [cyan]{job.company}[/cyan]")
        console.print(f"    Apply: [dim]{job.apply_type}[/dim]  "
                      f"Remote: [dim]{job.remote_type or '?'}[/dim]")

        if not job.description or len(job.description) < 100:
            print(f"    [yellow]⚠ Description too short "
          f"({len(job.description or '')} chars) — skipping[/yellow]")
            continue

        # Parse JD structure
        parsed = await parse_job_description(
            description=job.description,
            title=job.title,
            company=job.company,
        )

        # Screen against Mike's profile
        try:
            resp = get_haiku().invoke([
                SystemMessage(content=JOB_MATCH_SYSTEM),
                HumanMessage(content=JOB_MATCH_HUMAN.format(
                    job_title=job.title,
                    company_name=job.company,
                    location=job.location,
                    job_description=job.description[:4000],
                )),
            ])
            raw = resp.content.strip()
            if raw.startswith("```"):
                raw = raw.split("```")[1]
                if raw.startswith("json"):
                    raw = raw[4:]
            screening = json.loads(raw.strip())
        except Exception:
            screening = {"match_score": 65, "match_tier": "good",
                        "matching_skills": [], "missing_skills": [],
                        "recommendation": "", "priority": 3}

        score = screening.get("match_score", 0)
        tier  = screening.get("match_tier", "good")
        color = "green" if score >= 80 else "yellow" if score >= 60 else "red"
        console.print(f"    [{color}]{score}/100 ({tier})[/{color}]  "
                      f"[dim]{screening.get('recommendation','')[:70]}[/dim]")

        if tier in ("weak", "skip") and score < 50:
            console.print("    [dim]→ Below threshold, skipping[/dim]")
            continue

        # Save to DB
        try:
            job_id = await upsert_job(
                title=job.title,
                company=job.company,
                description_full=job.description,
                url=job.raw_url,
                location=job.location,
                source="gmail:linkedin",
                requirements_parsed=parsed.get("requirements_parsed", []),
                nice_to_have=parsed.get("nice_to_have", []),
                responsibilities=parsed.get("responsibilities", []),
                tech_stack=parsed.get("tech_stack", []),
                seniority_level=parsed.get("seniority_level", ""),
                employment_type=parsed.get("employment_type", "full_time"),
                salary_range_text=job.salary_text or parsed.get("salary_range_text", ""),
                remote_type=job.remote_type or parsed.get("remote_type", ""),
                company_linkedin=job.company_url,
            )

            await update_job_screening(
                job_id=job_id,
                match_score=score,
                match_tier=tier,
                matching_skills=screening.get("matching_skills", []),
                missing_skills=screening.get("missing_skills", []),
                recommendation=screening.get("recommendation", ""),
                priority=screening.get("priority", 3),
                description_summary=parsed.get("description_summary", ""),
            )

            # Store apply type info in notes
            notes = f"apply_type:{job.apply_type}"
            if job.apply_url:
                notes += f" | apply_url:{job.apply_url[:200]}"

            jobs_saved += 1
            if tier in ("strong", "good"):
                jobs_queued += 1
            console.print(f"    [green]✅ Saved — job_id: {job_id[:8]}...[/green]")

        except Exception as e:
            console.print(f"    [red]❌ DB error: {e}[/red]")

        await asyncio.sleep(0.2)

    # Summary
    console.print(f"\n[bold gold1]═══ Gmail Discovery Complete ═══[/bold gold1]")

    tbl = Table(box=box.SIMPLE, show_header=False, padding=(0, 2))
    tbl.add_column("Label", style="dim")
    tbl.add_column("Value", style="bold")
    tbl.add_row("Emails processed", str(len(alerts)))
    tbl.add_row("Jobs in emails",   str(len(unique_refs)))
    tbl.add_row("LinkedIn jobs fetched", str(len(linkedin_jobs)))
    tbl.add_row("Jobs saved to DB", str(jobs_saved))
    tbl.add_row("Queued for tailoring", str(jobs_queued))
    console.print(tbl)

    if jobs_queued > 0:
        console.print(
            f"\n[dim]Run [bold]uv run python run_tailor.py[/bold] "
            f"to tailor resumes for queued jobs.[/dim]\n"
        )

    return {
        "emails_processed": len(alerts),
        "jobs_found":       len(unique_refs),
        "jobs_fetched":     len(linkedin_jobs),
        "jobs_saved":       jobs_saved,
        "jobs_queued":      jobs_queued,
    }


# ── Reply Monitor (unchanged from v1) ────────────────────────────────────────

REPLY_DRAFT_SYSTEM = """You are Hire Lord's email response specialist.
Draft professional, warm, and concise email replies on behalf of Mike Doran.
Mike is a Unity XR Developer in Holladay, Utah. Email: michaelbryandoran@gmail.com.
- Sound human and genuine, not templated
- Be enthusiastic but not desperate
- Keep responses under 150 words unless it's an offer negotiation
- For interview requests: confirm interest, offer availability
- Never grovel or over-thank
"""

REPLY_DRAFT_HUMAN = """
INCOMING EMAIL:
From: {sender}
Subject: {subject}
Body: {body}
Category: {category}
Company: {company}

Draft a reply from Mike Doran. Return only the email body, no subject line.
"""


async def run_gmail_monitor(days_back: int = 7) -> dict:
    """Check Gmail for employer replies and draft responses with HITL."""
    console.print("\n[bold gold1]👑 Hire Lord — Gmail Reply Monitor[/bold gold1]")
    console.print(f"[dim]{datetime.now().strftime('%B %d, %Y at %I:%M %p')} MT[/dim]\n")

    console.print("[cyan]Scanning for employer replies...[/cyan]")
    replies = fetch_employer_replies(days_back=days_back)

    if not replies:
        console.print("[dim]No employer replies found.[/dim]")
        return {"replies_found": 0, "responses_sent": 0}

    console.print(f"  Found [bold]{len(replies)}[/bold] potential employer emails\n")
    responses_sent = 0

    for reply in replies:
        category  = reply["category"]
        cat_color = {
            "interview_request": "bold green",
            "offer":             "bold gold1",
            "rejection":         "red",
            "info_request":      "yellow",
            "unknown":           "dim",
        }.get(category, "white")

        console.print(Panel(
            f"[bold]From:[/bold]    {reply['sender']}\n"
            f"[bold]Subject:[/bold] {reply['subject']}\n\n"
            f"{reply['body'][:400]}{'...' if len(reply['body']) > 400 else ''}",
            title=f"[{cat_color}]{category.replace('_',' ').upper()}[/{cat_color}]",
            border_style=cat_color,
        ))

        if category == "rejection":
            console.print("[dim]  → Logging rejection, no reply needed.\n[/dim]")
            continue

        if category == "unknown":
            console.print("[dim]  → Unknown category, skipping.\n[/dim]")
            continue

        # Draft response
        console.print("[cyan]  Drafting response...[/cyan]")
        draft = get_sonnet().invoke([
            SystemMessage(content=REPLY_DRAFT_SYSTEM),
            HumanMessage(content=REPLY_DRAFT_HUMAN.format(
                sender=reply["sender"],
                subject=reply["subject"],
                body=reply["body"],
                category=category,
                company=reply.get("company_match", "the company"),
            )),
        ]).content.strip()

        console.print(Panel(draft, title="[cyan]Draft Reply[/cyan]",
                            border_style="cyan"))

        action = Prompt.ask(
            "Decision",
            choices=["send", "edit", "skip"],
            default="send",
        )

        if action == "skip":
            console.print("[dim]Skipped.\n[/dim]")
            continue

        if action == "edit":
            console.print("[dim]Paste edited reply (Ctrl+D when done):[/dim]")
            lines = []
            try:
                while True:
                    lines.append(input())
            except EOFError:
                pass
            draft = "\n".join(lines).strip() or draft

        to_address = reply["sender"]
        if "<" in to_address:
            to_address = to_address.split("<")[1].rstrip(">")

        subject = reply["subject"]
        if not subject.lower().startswith("re:"):
            subject = f"Re: {subject}"

        if send_email(to_address, subject, draft, reply["message_id"]):
            console.print("[green]  ✅ Sent!\n[/green]")
            responses_sent += 1
        else:
            console.print("[red]  ❌ Send failed.\n[/red]")

    return {"replies_found": len(replies), "responses_sent": responses_sent}


# ── Entry point ───────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import sys
    if "--discover" in sys.argv:
        asyncio.run(run_gmail_discovery(days_back=2))
    elif "--monitor" in sys.argv:
        asyncio.run(run_gmail_monitor(days_back=7))
    else:
        print("Usage:")
        print("  uv run python -m hirelord.agents.gmail_agent --discover")
        print("  uv run python -m hirelord.agents.gmail_agent --monitor")
