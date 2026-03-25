"""
Hire Lord — Job Discovery Agent (Phase 2)
==========================================
LangGraph pipeline that runs daily to find, parse, screen, and queue new jobs.

Flow:
  discover_jobs
      │
  parse_descriptions    ← Haiku extracts structured fields from each JD
      │
  screen_jobs           ← Haiku scores each job 0-100 against Mike's profile
      │
  save_to_db            ← Upserts strong/good matches into hirelord.db
      │
  queue_for_tailoring   ← Returns list of job_ids ready for Phase 1 pipeline
      │
      END

Run manually:    uv run python -m hirelord.agents.discovery
Schedule:        APScheduler runs this daily at 7:00 AM MT
"""

import asyncio
import json
import os
import uuid
from datetime import datetime
from typing import Optional
from typing_extensions import TypedDict
from dotenv import load_dotenv
load_dotenv()

from langchain_anthropic import ChatAnthropic
from langchain_core.messages import SystemMessage, HumanMessage
from langgraph.graph import StateGraph, START, END
from langgraph.checkpoint.memory import MemorySaver
from rich.console import Console
from rich.table import Table
from rich import box

from ..tools.proxycurl import discover_jobs, JobListing
from ..tools.resume import parse_job_description
from ..db.store import upsert_job, update_job_screening, update_job_status
from ..prompts.tailor import JOB_MATCH_SYSTEM, JOB_MATCH_HUMAN

console = Console()

# ── Models ────────────────────────────────────────────────────────────────────

_haiku = None

def get_haiku():
    global _haiku
    if _haiku is None:
        _haiku = ChatAnthropic(model="claude-haiku-4-5-20251001", temperature=0)
    return _haiku


# ── State ─────────────────────────────────────────────────────────────────────

class DiscoveryState(TypedDict):
    # Config
    linkedin_api_key: str
    use_indeed: bool
    use_linkedin: bool

    # Raw discovered listings
    raw_listings: list[dict]            # serialized JobListings

    # Parsed + screened
    parsed_jobs: list[dict]             # listings with structured fields added
    screened_jobs: list[dict]           # listings with screening results added

    # Results
    saved_job_ids: list[str]            # job_ids saved to DB
    queued_for_tailoring: list[str]     # strong/good match job_ids
    skipped_count: int
    error: Optional[str]


# ── Nodes ─────────────────────────────────────────────────────────────────────

async def discover(state: DiscoveryState) -> dict:
    """Run both discovery sources and collect raw listings."""
    console.print("\n[bold gold1]👑 Hire Lord — Job Discovery Agent[/bold gold1]")
    console.print(f"[dim]{datetime.now().strftime('%B %d, %Y at %I:%M %p')} MT[/dim]\n")
    console.print("[cyan]Phase 1: Discovering jobs...[/cyan]")

    listings = await discover_jobs(
        linkedin_api_key=state.get("linkedin_api_key", ""),
        use_indeed=state.get("use_indeed", True),
        use_linkedin=state.get("use_linkedin", True),
    )

    # Serialize for state (TypedDict can't hold dataclasses)
    raw = []
    for l in listings:
        raw.append({
            "title":           l.title,
            "company":         l.company,
            "location":        l.location,
            "description":     l.description,
            "url":             l.url,
            "source":          l.source,
            "employment_type": l.employment_type,
            "remote_type":     l.remote_type,
            "salary_range_text": l.salary_range_text,
            "posted_at":       l.posted_at,
            "company_linkedin": l.company_linkedin,
            "job_id":          l.job_id,
            "dedup_key":       l.dedup_key,
        })

    console.print(f"[green]  ✅ Discovered {len(raw)} unique jobs[/green]")
    return {"raw_listings": raw}


async def parse_descriptions(state: DiscoveryState) -> dict:
    """Use Haiku to parse structured fields from each JD."""
    console.print("\n[cyan]Phase 2: Parsing job descriptions...[/cyan]")

    listings = state["raw_listings"]
    parsed = []

    for i, job in enumerate(listings):
        console.print(f"  [{i+1}/{len(listings)}] Parsing: {job['title']} @ {job['company']}...")
        structured = await parse_job_description(
            description=job["description"],
            title=job["title"],
            company=job["company"],
        )
        parsed_job = {**job, **structured}
        parsed.append(parsed_job)
        await asyncio.sleep(0.2)  # Avoid rate limiting

    console.print(f"[green]  ✅ Parsed {len(parsed)} job descriptions[/green]")
    return {"parsed_jobs": parsed}


async def screen_jobs(state: DiscoveryState) -> dict:
    """Score each job against Mike's profile using Haiku."""
    console.print("\n[cyan]Phase 3: Screening jobs against your profile...[/cyan]")

    haiku = get_haiku()
    jobs = state["parsed_jobs"]
    screened = []

    for i, job in enumerate(jobs):
        console.print(f"  [{i+1}/{len(jobs)}] Screening: {job['title']} @ {job['company']}...")

        prompt = JOB_MATCH_HUMAN.format(
            job_title=job["title"],
            company_name=job["company"],
            location=job.get("location", ""),
            job_description=job["description"][:4000],
        )

        try:
            response = haiku.invoke([
                SystemMessage(content=JOB_MATCH_SYSTEM),
                HumanMessage(content=prompt),
            ])

            raw = response.content.strip()
            if raw.startswith("```"):
                raw = raw.split("```")[1]
                if raw.startswith("json"):
                    raw = raw[4:]

            result = json.loads(raw.strip())
            screened_job = {
                **job,
                "match_score":     result.get("match_score", 50),
                "match_tier":      result.get("match_tier", "good"),
                "matching_skills": result.get("matching_skills", []),
                "missing_skills":  result.get("missing_skills", []),
                "recommendation":  result.get("recommendation", ""),
                "priority":        result.get("priority", 3),
            }
            score = screened_job["match_score"]
            tier  = screened_job["match_tier"]
            color = "green" if score >= 80 else "yellow" if score >= 60 else "red"
            console.print(
                f"    [{color}]{score}/100 ({tier})[/{color}] — "
                f"{screened_job['recommendation'][:80]}"
            )

        except Exception as e:
            console.print(f"    [yellow]⚠ Screening failed: {e} — defaulting to 50/good[/yellow]")
            screened_job = {
                **job,
                "match_score": 50,
                "match_tier": "good",
                "matching_skills": [],
                "missing_skills": [],
                "recommendation": "Manual review needed.",
                "priority": 3,
            }

        screened.append(screened_job)
        await asyncio.sleep(0.3)

    console.print(f"[green]  ✅ Screened {len(screened)} jobs[/green]")
    return {"screened_jobs": screened}


async def save_to_db(state: DiscoveryState) -> dict:
    """Save all screened jobs to hirelord.db."""
    console.print("\n[cyan]Phase 4: Saving to database...[/cyan]")

    jobs          = state["screened_jobs"]
    saved_ids     = []
    queued_ids    = []
    skipped_count = 0

    for job in jobs:
        tier = job.get("match_tier", "good")

        if tier in ("weak", "skip"):
            skipped_count += 1
            continue

        try:
            job_id = await upsert_job(
                title=job["title"],
                company=job["company"],
                description_full=job["description"],
                url=job.get("url", ""),
                location=job.get("location", ""),
                source=job.get("source", "discovery"),
                requirements_raw="\n".join(job.get("requirements_parsed", [])),
                requirements_parsed=job.get("requirements_parsed", []),
                nice_to_have=job.get("nice_to_have", []),
                responsibilities=job.get("responsibilities", []),
                tech_stack=job.get("tech_stack", []),
                seniority_level=job.get("seniority_level", ""),
                employment_type=job.get("employment_type", "full_time"),
                salary_range_text=job.get("salary_range_text", ""),
                salary_low=job.get("salary_low"),
                salary_high=job.get("salary_high"),
                remote_type=job.get("remote_type", ""),
                company_linkedin=job.get("company_linkedin", ""),
                posted_at=job.get("posted_at", ""),
            )

            await update_job_screening(
                job_id=job_id,
                match_score=job["match_score"],
                match_tier=tier,
                matching_skills=job.get("matching_skills", []),
                missing_skills=job.get("missing_skills", []),
                recommendation=job.get("recommendation", ""),
                priority=job.get("priority", 3),
                description_summary=job.get("description_summary", ""),
            )

            saved_ids.append(job_id)

            if tier in ("strong", "good"):
                queued_ids.append(job_id)

        except Exception as e:
            console.print(f"  [red]❌ DB error for {job['title']}: {e}[/red]")

    console.print(f"[green]  ✅ Saved {len(saved_ids)} jobs | Skipped {skipped_count} weak matches[/green]")
    return {
        "saved_job_ids":        saved_ids,
        "queued_for_tailoring": queued_ids,
        "skipped_count":        skipped_count,
    }


def print_summary(state: DiscoveryState) -> dict:
    """Print a Rich summary table of the discovery run."""
    console.print("\n[bold gold1]═══ Discovery Run Complete ═══[/bold gold1]")

    jobs = state.get("screened_jobs", [])
    if not jobs:
        console.print("[dim]No jobs found this run.[/dim]")
        return {}

    tbl = Table(
        title=f"Jobs Found — {datetime.now().strftime('%B %d, %Y')}",
        box=box.ROUNDED,
        border_style="gold1",
        header_style="bold gold1",
    )
    tbl.add_column("#",          justify="right", min_width=3,  style="dim")
    tbl.add_column("Company",    min_width=18, style="bold")
    tbl.add_column("Role",       min_width=26)
    tbl.add_column("Score",      justify="center", min_width=6)
    tbl.add_column("Tier",       justify="center", min_width=8)
    tbl.add_column("Remote",     min_width=8)
    tbl.add_column("Source",     min_width=12, style="dim")

    for i, job in enumerate(
        sorted(jobs, key=lambda j: (-j.get("match_score", 0))), 1
    ):
        score = job.get("match_score", 0)
        tier  = job.get("match_tier", "")
        sc    = "green" if score >= 80 else "yellow" if score >= 60 else "red"
        tc    = "green" if tier == "strong" else "yellow" if tier == "good" else "red"
        remote = job.get("remote_type", "")
        rt    = "REMOTE" if remote == "remote" else "HYBRID" if remote == "hybrid" else "ONSITE" if remote == "onsite" else "—"
        rc    = "green" if remote == "remote" else "yellow" if remote == "hybrid" else "dim"

        tbl.add_row(
            str(i),
            job["company"][:18],
            (job["title"][:28] + "…") if len(job["title"]) > 28 else job["title"],
            f"[{sc}]{score}[/{sc}]",
            f"[{tc}]{tier.upper()}[/{tc}]",
            f"[{rc}]{rt}[/{rc}]",
            job.get("source", "")[:12],
        )

    console.print(tbl)

    queued = state.get("queued_for_tailoring", [])
    console.print(
        f"\n[bold]👑 {len(queued)} jobs queued for tailoring[/bold]  "
        f"[dim]({state.get('skipped_count', 0)} weak matches skipped)[/dim]"
    )

    if queued:
        console.print(
            "\n[dim]Run [bold]uv run python run_tailor.py --job-id <id>[/bold] "
            "to tailor a specific job, or[/dim]"
        )
        console.print(
            "[dim]Run [bold]uv run python run_discovery.py --auto-tailor[/bold] "
            "to tailor all queued jobs.[/dim]\n"
        )

    return {}


# ── Graph ─────────────────────────────────────────────────────────────────────

def build_discovery_graph():
    builder = StateGraph(DiscoveryState)

    builder.add_node("discover",           discover)
    builder.add_node("parse_descriptions", parse_descriptions)
    builder.add_node("screen_jobs",        screen_jobs)
    builder.add_node("save_to_db",         save_to_db)
    builder.add_node("print_summary",      print_summary)

    builder.add_edge(START,                "discover")
    builder.add_edge("discover",           "parse_descriptions")
    builder.add_edge("parse_descriptions", "screen_jobs")
    builder.add_edge("screen_jobs",        "save_to_db")
    builder.add_edge("save_to_db",         "print_summary")
    builder.add_edge("print_summary",      END)

    return builder.compile(checkpointer=MemorySaver())


# ── Entry point ───────────────────────────────────────────────────────────────

async def run_discovery(
    use_indeed: bool = True,
    use_linkedin: bool = True,
) -> dict:
    """Run the full discovery pipeline. Returns final state."""
    graph = build_discovery_graph()
    config = {"configurable": {"thread_id": str(uuid.uuid4())}}

    initial = {
        "linkedin_api_key": os.environ.get("RAPIDAPI_KEY", ""),
        "use_indeed":       use_indeed,
        "use_linkedin":     use_linkedin,
        "raw_listings":     [],
        "parsed_jobs":      [],
        "screened_jobs":    [],
        "saved_job_ids":    [],
        "queued_for_tailoring": [],
        "skipped_count":    0,
        "error":            None,
    }

    result = await graph.ainvoke(initial, config)
    return result


if __name__ == "__main__":
    asyncio.run(run_discovery())
