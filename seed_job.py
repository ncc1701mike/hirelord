"""
Hire Lord — Job Seed Script
=============================
Manually add a job to the pipeline by URL or by pasting a job description.
Bypasses discovery — goes straight to parse → screen → save → optional tailor.

Usage:
    # Add by LinkedIn/Indeed/Glassdoor URL
    uv run python seed_job.py --url "https://www.linkedin.com/jobs/view/1234567890"

    # Add by URL with auto-tailor if strong match
    uv run python seed_job.py --url "https://www.linkedin.com/jobs/view/1234567890" --tailor

    # Add multiple URLs at once
    uv run python seed_job.py --url "https://..." --url "https://..." --tailor

    # Interactive mode — paste job description manually
    uv run python seed_job.py --interactive
"""

import argparse
import asyncio
import json
import os
import sys
import uuid
from dotenv import load_dotenv
load_dotenv()

import httpx
from rich.console import Console
from rich.panel import Panel
from rich.prompt import Prompt, Confirm

console = Console()

# ── JD Fetcher ────────────────────────────────────────────────────────────────

JSEARCH_ENDPOINT = "https://jsearch.p.rapidapi.com/search"
JSEARCH_DETAILS_ENDPOINT = "https://jsearch.p.rapidapi.com/job-details"

HEADERS = {
    "x-rapidapi-key":  os.environ.get("RAPIDAPI_KEY", ""),
    "x-rapidapi-host": "jsearch.p.rapidapi.com",
    "Content-Type":    "application/json",
}

BROWSER_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.5",
}


def detect_source(url: str) -> str:
    """Detect job board from URL."""
    url_lower = url.lower()
    if "linkedin.com" in url_lower:
        return "linkedin"
    elif "indeed.com" in url_lower:
        return "indeed"
    elif "glassdoor.com" in url_lower:
        return "glassdoor"
    elif "greenhouse.io" in url_lower:
        return "greenhouse"
    elif "lever.co" in url_lower:
        return "lever"
    elif "workday.com" in url_lower:
        return "workday"
    else:
        return "direct"


def extract_linkedin_job_id(url: str) -> str:
    """Extract LinkedIn job ID from URL."""
    import re
    # Handles formats like:
    # /jobs/view/1234567890
    # /jobs/view/title-at-company-1234567890
    match = re.search(r"/jobs/view/(?:[^/]*-)?(\d+)", url)
    if match:
        return match.group(1)
    # Also try query param
    match = re.search(r"currentJobId=(\d+)", url)
    if match:
        return match.group(1)
    return ""


async def fetch_via_jsearch(url: str) -> dict | None:
    """
    Try to fetch job details via JSearch job-details endpoint.
    Works best for LinkedIn job URLs.
    """
    api_key = os.environ.get("RAPIDAPI_KEY", "")
    if not api_key:
        return None

    try:
        async with httpx.AsyncClient(timeout=20) as client:
            resp = await client.get(
                JSEARCH_DETAILS_ENDPOINT,
                headers=HEADERS,
                params={"job_id": f"https://www.linkedin.com/jobs/view/{url}", 
        "extended_publisher_details": "false"},
            )
            if resp.status_code == 200:
                data = resp.json()
                jobs = data.get("data", [])
                if jobs:
                    return jobs[0]
    except Exception as e:
        console.print(f"  [dim]JSearch details lookup failed: {e}[/dim]")
    return None


async def fetch_job_page(url: str) -> str:
    """
    Fetch raw HTML from a job posting URL.
    Used as fallback when JSearch doesn't have the job.
    """
    try:
        async with httpx.AsyncClient(
            timeout=20,
            headers=BROWSER_HEADERS,
            follow_redirects=True,
        ) as client:
            resp = await client.get(url)
            resp.raise_for_status()
            return resp.text
    except Exception as e:
        console.print(f"  [yellow]⚠ Could not fetch page: {e}[/yellow]")
        return ""


async def extract_jd_from_html(html: str, url: str) -> dict:
    """Use Claude Haiku to extract job details from raw HTML."""
    from langchain_anthropic import ChatAnthropic
    from langchain_core.messages import SystemMessage, HumanMessage

    llm = ChatAnthropic(model="claude-haiku-4-5-20251001", temperature=0)

    # Truncate HTML — we don't need the whole page
    truncated = html[:12000] if len(html) > 12000 else html

    # Strip obvious boilerplate tags
    import re
    clean = re.sub(r"<script[^>]*>.*?</script>", "", truncated, flags=re.DOTALL)
    clean = re.sub(r"<style[^>]*>.*?</style>", "", clean, flags=re.DOTALL)
    clean = re.sub(r"<[^>]+>", " ", clean)
    clean = re.sub(r"\s+", " ", clean).strip()[:8000]

    prompt = f"""Extract job posting information from this webpage text and return JSON only.

URL: {url}

PAGE TEXT:
{clean}

Return this exact JSON structure (no markdown, no preamble):
{{
  "title": "job title",
  "company": "company name",
  "location": "city, state or Remote",
  "description": "full job description text",
  "requirements": "requirements section text",
  "salary_range_text": "salary if mentioned or empty string",
  "employment_type": "full_time or contract or part_time",
  "remote_type": "remote or hybrid or onsite"
}}

If you cannot extract a field, use an empty string. Never return null."""

    try:
        response = llm.invoke([
            SystemMessage(content="You are a job posting parser. Return only valid JSON."),
            HumanMessage(content=prompt),
        ])
        raw = response.content.strip()
        if raw.startswith("```"):
            raw = raw.split("```")[1]
            if raw.startswith("json"):
                raw = raw[4:]
        return json.loads(raw.strip())
    except Exception as e:
        console.print(f"  [yellow]⚠ HTML extraction failed: {e}[/yellow]")
        return {}


# ── Main seed flow ────────────────────────────────────────────────────────────

async def seed_from_url(url: str, auto_tailor: bool = False) -> dict | None:
    """Full flow: fetch → parse → screen → save → optional tailor."""
    from hirelord.tools.resume import parse_job_description
    from hirelord.db.store import upsert_job, update_job_screening, get_job
    from hirelord.prompts.tailor import JOB_MATCH_SYSTEM, JOB_MATCH_HUMAN
    from langchain_anthropic import ChatAnthropic
    from langchain_core.messages import SystemMessage, HumanMessage

    source = detect_source(url)
    console.print(f"\n[bold]Source:[/bold] {source} | [bold]URL:[/bold] {url[:80]}...")

    # ── Step 1: Fetch job data ────────────────────────────────────────────────
    console.print("\n[cyan]Step 1: Fetching job data...[/cyan]")

    job_data = {}

    # Try JSearch first (best structured data)
    if source == "linkedin":
        job_id = extract_linkedin_job_id(url)
        if job_id:
            console.print(f"  LinkedIn job ID: {job_id} — trying JSearch...")
            jsearch_result = await fetch_via_jsearch(job_id)
            if jsearch_result:
                job_data = {
                    "title":       jsearch_result.get("job_title", ""),
                    "company":     jsearch_result.get("employer_name", ""),
                    "location":    f"{jsearch_result.get('job_city', '')}, {jsearch_result.get('job_state', '')}".strip(", "),
                    "description": jsearch_result.get("job_description", ""),
                    "requirements": "",
                    "salary_range_text": "",
                    "employment_type": jsearch_result.get("job_employment_type", "FULLTIME").lower(),
                    "remote_type": "remote" if jsearch_result.get("job_is_remote") else "",
                }
                console.print("  [green]✅ Got data from JSearch[/green]")

    # Fallback: scrape the page directly
    if not job_data.get("title"):
        console.print("  Falling back to direct page scrape...")
        html = await fetch_job_page(url)
        if html:
            job_data = await extract_jd_from_html(html, url)
            if job_data.get("title"):
                console.print("  [green]✅ Extracted from page HTML[/green]")
            else:
                console.print("  [yellow]⚠ Could not extract job data automatically[/yellow]")

    # Last resort: ask user to fill in manually
    if not job_data.get("title"):
        console.print("\n  [yellow]Automatic extraction failed. Please provide details manually:[/yellow]")
        job_data["title"]       = Prompt.ask("  Job title")
        job_data["company"]     = Prompt.ask("  Company name")
        job_data["location"]    = Prompt.ask("  Location", default="Remote")
        job_data["description"] = Prompt.ask("  Paste job description (or press Enter to skip)")

    if not job_data.get("title"):
        console.print("[red]❌ Could not get job data. Skipping.[/red]")
        return None

    console.print(f"\n  [bold]{job_data['title']}[/bold] @ [bold cyan]{job_data['company']}[/bold cyan]")
    console.print(f"  {job_data.get('location', '')} | {job_data.get('remote_type', '')}")

    # ── Step 2: Parse JD structure ────────────────────────────────────────────
    console.print("\n[cyan]Step 2: Parsing job description...[/cyan]")
    parsed = await parse_job_description(
        description=job_data.get("description", ""),
        title=job_data.get("title", ""),
        company=job_data.get("company", ""),
    )
    console.print(f"  Tech stack: {', '.join(parsed.get('tech_stack', [])[:8])}")
    console.print(f"  Seniority:  {parsed.get('seniority_level', 'unknown')}")

    # ── Step 3: Screen against Mike's profile ─────────────────────────────────
    console.print("\n[cyan]Step 3: Screening against your profile...[/cyan]")
    haiku = ChatAnthropic(model="claude-haiku-4-5-20251001", temperature=0)

    screen_prompt = JOB_MATCH_HUMAN.format(
        job_title=job_data.get("title", ""),
        company_name=job_data.get("company", ""),
        location=job_data.get("location", ""),
        job_description=(job_data.get("description") or "")[:4000],
    )

    try:
        resp = haiku.invoke([
            SystemMessage(content=JOB_MATCH_SYSTEM),
            HumanMessage(content=screen_prompt),
        ])
        raw = resp.content.strip()
        if raw.startswith("```"):
            raw = raw.split("```")[1]
            if raw.startswith("json"):
                raw = raw[4:]
        screening = json.loads(raw.strip())
    except Exception:
        screening = {"match_score": 70, "match_tier": "good",
                     "matching_skills": [], "missing_skills": [],
                     "recommendation": "Manual review needed.", "priority": 3}

    score = screening.get("match_score", 0)
    tier  = screening.get("match_tier", "good")
    color = "green" if score >= 80 else "yellow" if score >= 60 else "red"
    console.print(f"  [{color}]{score}/100 ({tier})[/{color}]")
    console.print(f"  {screening.get('recommendation', '')}")

    # ── Step 4: Save to DB ────────────────────────────────────────────────────
    console.print("\n[cyan]Step 4: Saving to database...[/cyan]")

    try:
        job_id = await upsert_job(
            title=job_data.get("title", ""),
            company=job_data.get("company", ""),
            description_full=job_data.get("description", ""),
            url=url,
            location=job_data.get("location", ""),
            source=f"manual:{source}",
            requirements_raw=job_data.get("requirements", ""),
            requirements_parsed=parsed.get("requirements_parsed", []),
            nice_to_have=parsed.get("nice_to_have", []),
            responsibilities=parsed.get("responsibilities", []),
            tech_stack=parsed.get("tech_stack", []),
            seniority_level=parsed.get("seniority_level", ""),
            employment_type=parsed.get("employment_type", "full_time"),
            salary_range_text=job_data.get("salary_range_text", "")
                              or parsed.get("salary_range_text", ""),
            remote_type=job_data.get("remote_type", "")
                        or parsed.get("remote_type", ""),
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
        console.print(f"  [green]✅ Saved — job ID: {job_id}[/green]")

    except Exception as e:
        console.print(f"  [red]❌ DB save failed: {e}[/red]")
        return None

    # ── Step 5: Optional tailor ───────────────────────────────────────────────
    if auto_tailor and tier in ("strong", "good"):
        console.print("\n[cyan]Step 5: Tailoring resume + cover letter...[/cyan]")
        from hirelord.agents.tailor import tailor_for_job
        result = await tailor_for_job(
            job_title=job_data.get("title", ""),
            company_name=job_data.get("company", ""),
            job_description=job_data.get("description", ""),
            job_url=url,
            location=job_data.get("location", ""),
        )
        if result.get("__interrupt__"):
            console.print("\n[gold1]👑 Review required — run:[/gold1]")
            console.print(f"[dim]uv run python run_tailor.py[/dim]")
    elif auto_tailor and tier not in ("strong", "good"):
        console.print(f"\n[yellow]⏭  Skipping tailor — tier is '{tier}' (below threshold)[/yellow]")

    return {"job_id": job_id, "score": score, "tier": tier,
            "title": job_data.get("title"), "company": job_data.get("company")}


async def interactive_mode():
    """Paste a job description directly — no URL needed."""
    console.print("\n[bold gold1]👑 Hire Lord — Manual Job Entry[/bold gold1]")
    console.print("[dim]Enter job details manually[/dim]\n")

    title   = Prompt.ask("Job title")
    company = Prompt.ask("Company name")
    location = Prompt.ask("Location", default="Remote")
    url     = Prompt.ask("Job URL (optional)", default="")

    console.print("\nPaste the full job description below.")
    console.print("[dim](Paste all text, then press Enter twice + Ctrl+D when done)[/dim]\n")

    lines = []
    try:
        while True:
            line = input()
            lines.append(line)
    except EOFError:
        pass

    description = "\n".join(lines).strip()
    if not description:
        console.print("[red]No description provided. Exiting.[/red]")
        return

    # Build a fake URL if none provided
    if not url:
        url = f"manual:{company.lower().replace(' ','-')}-{title.lower().replace(' ','-')}-{uuid.uuid4().hex[:8]}"

    # Inject the data and run through the pipeline
    import types
    # Monkey-patch fetch functions to return our manual data
    job_data_override = {
        "title": title, "company": company,
        "location": location, "description": description,
        "requirements": "", "salary_range_text": "",
        "employment_type": "full_time", "remote_type": "",
    }

    # Run directly using the parsed data
    from hirelord.tools.resume import parse_job_description
    from hirelord.db.store import upsert_job, update_job_screening
    from hirelord.prompts.tailor import JOB_MATCH_SYSTEM, JOB_MATCH_HUMAN
    from langchain_anthropic import ChatAnthropic
    from langchain_core.messages import SystemMessage, HumanMessage
    import json

    console.print("\n[cyan]Parsing...[/cyan]")
    parsed = await parse_job_description(description, title, company)

    console.print("[cyan]Screening...[/cyan]")
    haiku = ChatAnthropic(model="claude-haiku-4-5-20251001", temperature=0)
    resp = haiku.invoke([
        SystemMessage(content=JOB_MATCH_SYSTEM),
        HumanMessage(content=JOB_MATCH_HUMAN.format(
            job_title=title, company_name=company,
            location=location, job_description=description[:4000],
        )),
    ])
    raw = resp.content.strip()
    if raw.startswith("```"):
        raw = raw.split("```")[1]
        if raw.startswith("json"):
            raw = raw[4:]
    screening = json.loads(raw.strip())

    score = screening.get("match_score", 0)
    tier  = screening.get("match_tier", "good")
    color = "green" if score >= 80 else "yellow" if score >= 60 else "red"
    console.print(f"[{color}]{score}/100 ({tier})[/{color}] — {screening.get('recommendation','')}")

    console.print("[cyan]Saving...[/cyan]")
    job_id = await upsert_job(
        title=title, company=company,
        description_full=description, url=url,
        location=location, source="manual:paste",
        requirements_parsed=parsed.get("requirements_parsed", []),
        nice_to_have=parsed.get("nice_to_have", []),
        responsibilities=parsed.get("responsibilities", []),
        tech_stack=parsed.get("tech_stack", []),
        seniority_level=parsed.get("seniority_level", ""),
        employment_type=parsed.get("employment_type", "full_time"),
        remote_type=parsed.get("remote_type", ""),
    )
    await update_job_screening(
        job_id=job_id, match_score=score, match_tier=tier,
        matching_skills=screening.get("matching_skills", []),
        missing_skills=screening.get("missing_skills", []),
        recommendation=screening.get("recommendation", ""),
        priority=screening.get("priority", 3),
        description_summary=parsed.get("description_summary", ""),
    )

    console.print(Panel(
        f"[bold green]✅ Saved![/bold green]\n"
        f"Job ID: {job_id}\n"
        f"Score:  {score}/100 ({tier})\n\n"
        f"Run [bold]uv run python run_tailor.py[/bold] to tailor your resume.",
        border_style="green",
    ))

    if tier in ("strong", "good"):
        if Confirm.ask("\nTailor resume + cover letter now?"):
            from hirelord.agents.tailor import tailor_for_job
            await tailor_for_job(
                job_title=title, company_name=company,
                job_description=description, job_url=url, location=location,
            )


# ── Entry point ───────────────────────────────────────────────────────────────

async def main():
    parser = argparse.ArgumentParser(
        description="Hire Lord — Seed a job manually",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  uv run python seed_job.py --url "https://www.linkedin.com/jobs/view/1234567890"
  uv run python seed_job.py --url "https://..." --tailor
  uv run python seed_job.py --interactive
        """
    )
    parser.add_argument(
        "--url", "-u",
        action="append",
        dest="urls",
        metavar="URL",
        help="Job posting URL (can be used multiple times)",
    )
    parser.add_argument(
        "--tailor", "-t",
        action="store_true",
        help="Auto-tailor resume if match is strong/good",
    )
    parser.add_argument(
        "--interactive", "-i",
        action="store_true",
        help="Interactive mode — paste job description manually",
    )
    args = parser.parse_args()

    console.print(Panel(
        "[bold gold1]👑 HIRE LORD — Job Seeder[/bold gold1]\n"
        "[dim]Manually add jobs to your pipeline[/dim]",
        border_style="gold1",
        expand=False,
    ))

    if args.interactive:
        await interactive_mode()
        return

    if not args.urls:
        console.print("[yellow]No URLs provided. Use --url or --interactive.[/yellow]")
        console.print("[dim]Example: uv run python seed_job.py --url 'https://linkedin.com/jobs/view/...'[/dim]")
        parser.print_help()
        return

    results = []
    for url in args.urls:
        result = await seed_from_url(url.strip(), auto_tailor=args.tailor)
        if result:
            results.append(result)

    if results:
        console.print(f"\n[bold gold1]═══ Seeding Complete ═══[/bold gold1]")
        for r in results:
            color = "green" if r["score"] >= 80 else "yellow" if r["score"] >= 60 else "red"
            console.print(
                f"  [{color}]{r['score']}/100[/{color}]  "
                f"[bold]{r['company']}[/bold] — {r['title']}"
            )
        console.print(f"\n[dim]View pipeline: [bold]uv run python -m hirelord.dashboard[/bold][/dim]")
        console.print(f"[dim]Tailor a job:  [bold]uv run python run_tailor.py[/bold][/dim]\n")


if __name__ == "__main__":
    asyncio.run(main())