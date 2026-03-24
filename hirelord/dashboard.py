"""
Hire Lord — Terminal Dashboard v2
====================================
Rich terminal UI for the full job application pipeline.

Usage:
    uv run python -m hirelord.dashboard              # pipeline overview
    uv run python -m hirelord.dashboard --job <id>   # full job detail + JD + requirements
    uv run python -m hirelord.dashboard --prep <id>  # interview prep for a job
    uv run python -m hirelord.dashboard --watch      # auto-refresh every 30s
"""

import asyncio
import argparse
import json as _json
from datetime import datetime
from typing import Optional

from rich.console import Console
from rich.table import Table
from rich.panel import Panel
from rich.columns import Columns
from rich.text import Text
from rich.markdown import Markdown
from rich.rule import Rule
from rich import box

from .db.store import (
    get_pipeline_summary, get_full_pipeline,
    get_job_history, get_job, get_interview_prep,
)
from .db.models import JOB_STATUSES, APPLICATION_STATUSES

console = Console()


# ── Formatters ────────────────────────────────────────────────────────────────

def fmt_date(s: Optional[str]) -> str:
    if not s: return "—"
    try: return datetime.fromisoformat(s).strftime("%b %d, %Y %I:%M %p")
    except: return s[:16]

def fmt_date_short(s: Optional[str]) -> str:
    if not s: return "—"
    try: return datetime.fromisoformat(s).strftime("%b %d")
    except: return s[:10]

def is_overdue(s: Optional[str]) -> bool:
    if not s: return False
    try: return datetime.fromisoformat(s) < datetime.now()
    except: return False

def parse_json_field(val, default=None):
    if default is None: default = []
    if not val: return default
    try: return _json.loads(val)
    except: return default


# ── Main pipeline dashboard ───────────────────────────────────────────────────

async def render_dashboard() -> None:
    summary  = await get_pipeline_summary()
    pipeline = await get_full_pipeline()

    # Header
    console.print(Panel(
        "[bold gold1]👑  HIRE LORD  —  Job Application Command Center[/bold gold1]\n"
        f"[dim]Updated: {datetime.now().strftime('%B %d, %Y  %I:%M %p')} MT[/dim]",
        border_style="gold1", expand=True,
    ))

    # Summary cards
    jc = summary.get("jobs", {})
    ac = summary.get("applications", {})
    cards = [
        Panel(f"[bold white]{sum(jc.values())}[/bold white]\n[dim]Jobs Tracked[/dim]",
              border_style="white", expand=True),
        Panel(f"[bold blue]{sum(ac.values())}[/bold blue]\n[dim]Applications[/dim]",
              border_style="blue", expand=True),
        Panel(f"[bold cyan]{jc.get('screened',0)}[/bold cyan]\n[dim]Ready to Tailor[/dim]",
              border_style="cyan", expand=True),
        Panel(f"[bold yellow]{summary.get('followups_due',0)}[/bold yellow]\n[dim]Follow-ups Due[/dim]",
              border_style="yellow", expand=True),
        Panel(f"[bold magenta]{summary.get('interviews_upcoming',0)}[/bold magenta]\n[dim]Interviews[/dim]",
              border_style="magenta", expand=True),
        Panel(f"[bold gold1]{ac.get('offer',0)}[/bold gold1]\n[dim]Offers[/dim]",
              border_style="gold1", expand=True),
        Panel(f"[bold green]{summary.get('avg_match_score',0)}[/bold green]\n[dim]Avg Match Score[/dim]",
              border_style="green", expand=True),
    ]
    console.print(Columns(cards, equal=True, expand=True))

    # Pipeline status breakdown
    breakdown = Table(
        title="Pipeline Breakdown", box=box.SIMPLE_HEAD,
        border_style="dim", header_style="bold dim", expand=False,
    )
    breakdown.add_column("Stage",        min_width=22, style="bold")
    breakdown.add_column("Jobs",         justify="center", min_width=6)
    breakdown.add_column("Apps",         justify="center", min_width=6)
    breakdown.add_column("Description",  style="dim")

    for job_key, (label, color, desc) in JOB_STATUSES.items():
        j = jc.get(job_key, 0)
        app_key_map = {
            "applied": "submitted", "interviewing": "interviewing",
            "offered": "offer", "rejected": "rejected", "withdrawn": "withdrawn",
        }
        a = ac.get(app_key_map.get(job_key, ""), 0)
        if j == 0 and a == 0: continue
        breakdown.add_row(
            Text(label, style=color),
            Text(str(j) if j else "—", style=color if j else "dim"),
            Text(str(a) if a else "—", style=color if a else "dim"),
            desc,
        )
    console.print(breakdown)
    console.print()

    if not pipeline:
        console.print(Panel(
            "[dim]No active jobs yet.\n"
            "Run [bold]uv run python run_tailor.py[/bold] to add your first job.[/dim]",
            border_style="dim",
        ))
        return

    # Main pipeline table
    tbl = Table(
        title="Active Pipeline",
        box=box.ROUNDED, border_style="dim",
        header_style="bold gold1", expand=True,
    )
    tbl.add_column("#",           justify="right", min_width=3,  style="dim")
    tbl.add_column("Company",     min_width=16, style="bold")
    tbl.add_column("Role",        min_width=24)
    tbl.add_column("Score",       justify="center", min_width=6)
    tbl.add_column("Tier",        justify="center", min_width=8)
    tbl.add_column("Job Status",  min_width=13)
    tbl.add_column("App Status",  min_width=18)
    tbl.add_column("Remote",      min_width=8)
    tbl.add_column("Applied",     min_width=8, style="dim")
    tbl.add_column("Follow-up",   min_width=9)
    tbl.add_column("Interviews",  justify="center", min_width=4)
    tbl.add_column("Location",    min_width=12, style="dim")

    for i, row in enumerate(pipeline, 1):
        js    = row.get("job_status", "new")
        _, jc_, _ = JOB_STATUSES.get(js, ("", "white", ""))
        as_   = row.get("app_status") or ""
        _, ac_, _ = APPLICATION_STATUSES.get(as_, ("", "dim", ""))

        score = row.get("match_score") or 0
        sc    = "green" if score >= 80 else "yellow" if score >= 60 else "red"
        tier  = row.get("match_tier") or "—"
        tc    = "green" if tier=="strong" else "yellow" if tier=="good" else "red"

        fu    = row.get("next_followup_at")
        fu_t  = Text(f"⚠ {fmt_date_short(fu)}", style="bold red") if fu and is_overdue(fu) \
                else Text(fmt_date_short(fu), style="yellow") if fu \
                else Text("—", style="dim")

        remote = row.get("remote_type") or ""
        remote_t = Text("REMOTE", style="green") if remote == "remote" \
                   else Text("HYBRID", style="yellow") if remote == "hybrid" \
                   else Text("ONSITE", style="red") if remote == "onsite" \
                   else Text("—", style="dim")

        iv_count = row.get("upcoming_interviews") or 0
        iv_t  = Text(f"🎙 {iv_count}", style="bold magenta") if iv_count > 0 \
                else Text("—", style="dim")

        tbl.add_row(
            str(i),
            row["company"][:18],
            (row["title"][:30] + "…") if len(row["title"]) > 30 else row["title"],
            Text(str(score), style=f"bold {sc}"),
            Text(tier.upper(), style=tc),
            Text(js.upper(), style=jc_),
            Text(as_.upper() if as_ else "—", style=ac_ if as_ else "dim"),
            remote_t,
            fmt_date_short(row.get("applied_at")),
            fu_t,
            iv_t,
            (row.get("location") or "")[:14],
        )

    console.print(tbl)

    # Overdue follow-ups alert
    overdue = [r for r in pipeline
               if r.get("next_followup_at") and is_overdue(r["next_followup_at"])
               and r.get("app_status") not in ("offer","rejected","withdrawn")]
    if overdue:
        lines = "\n".join(
            f"  [bold red]⚠[/bold red]  [bold]{r['company']}[/bold] — "
            f"{r['title']}   [dim](due {fmt_date_short(r['next_followup_at'])})[/dim]"
            for r in overdue
        )
        console.print(Panel(lines, title="[bold red]Follow-ups Overdue[/bold red]",
                            border_style="red"))

    console.print(
        f"\n[dim]  [bold]--job <id>[/bold] for full JD + requirements  "
        f"|  [bold]--prep <id>[/bold] for interview prep  "
        f"|  [bold]--watch[/bold] to auto-refresh[/dim]\n"
    )


# ── Job detail view (includes full JD + requirements) ─────────────────────────

async def render_job_detail(job_id: str) -> None:
    job = await get_job(job_id)
    if not job:
        console.print(f"[red]Job {job_id} not found.[/red]")
        return

    _, jcolor, _ = JOB_STATUSES.get(job["status"], ("", "white", ""))

    console.print(Rule(style="gold1"))
    console.print(Panel(
        f"[bold]{job['title']}[/bold]  @  [bold cyan]{job['company']}[/bold cyan]\n"
        f"[dim]{job.get('location','')}[/dim]  "
        f"{'| ' + job['remote_type'].upper() if job.get('remote_type') else ''}  "
        f"{'| ' + job['employment_type'].upper() if job.get('employment_type') else ''}\n\n"
        f"[dim]URL:[/dim]  {job.get('url','—')}\n"
        f"[dim]Source:[/dim]  {job.get('source','—')}  "
        f"  [dim]Posted:[/dim]  {fmt_date_short(job.get('posted_at'))}  "
        f"  [dim]Discovered:[/dim]  {fmt_date(job.get('discovered_at'))}\n\n"
        f"[dim]Match Score:[/dim]  [bold]{job['match_score']}/100[/bold]  "
        f"  [dim]Tier:[/dim]  [bold]{job.get('match_tier','—').upper()}[/bold]  "
        f"  [dim]Priority:[/dim]  {job.get('priority','—')}  "
        f"  [dim]Status:[/dim]  [{jcolor}]{job['status'].upper()}[/{jcolor}]",
        title="[bold gold1]👑 Job Detail[/bold gold1]",
        border_style="gold1",
    ))

    # Salary
    sal = job.get("salary_range_text") or ""
    if not sal and job.get("salary_low"):
        sal = f"${job['salary_low']:,} – ${job['salary_high']:,}" if job.get("salary_high") \
              else f"${job['salary_low']:,}+"
    if sal:
        console.print(f"[bold]💰 Salary:[/bold]  {sal}")

    # AI summary
    if job.get("description_summary"):
        console.print(Panel(job["description_summary"],
                            title="AI Summary", border_style="cyan"))

    # Recommendation
    if job.get("recommendation"):
        console.print(Panel(job["recommendation"],
                            title="AI Recommendation", border_style="cyan"))

    # Skills match
    matching = parse_json_field(job.get("matching_skills"))
    missing  = parse_json_field(job.get("missing_skills"))
    keywords = parse_json_field(job.get("keyword_matches"))
    tech     = parse_json_field(job.get("tech_stack"))

    if matching or missing:
        skills_tbl = Table(box=box.SIMPLE, show_header=False, padding=(0,1))
        skills_tbl.add_column("Label", style="bold", min_width=20)
        skills_tbl.add_column("Skills")
        if matching:
            skills_tbl.add_row(
                Text("✅ Matching Skills", style="green"),
                Text(", ".join(matching), style="green"),
            )
        if missing:
            skills_tbl.add_row(
                Text("⚠  Missing Skills", style="yellow"),
                Text(", ".join(missing), style="yellow"),
            )
        if keywords:
            skills_tbl.add_row(
                Text("🔑 JD Keywords", style="cyan"),
                Text(", ".join(keywords), style="cyan"),
            )
        if tech:
            skills_tbl.add_row(
                Text("🛠  Tech Stack", style="dim"),
                Text(", ".join(tech), style="dim"),
            )
        console.print(Panel(skills_tbl, title="Skills Analysis", border_style="dim"))

    # Requirements (verbatim — critical for interviews)
    req_parsed = parse_json_field(job.get("requirements_parsed"))
    nice       = parse_json_field(job.get("nice_to_have"))
    resp       = parse_json_field(job.get("responsibilities"))

    if req_parsed or job.get("requirements_raw"):
        req_table = Table(
            title="📋 Requirements (Verbatim from JD)",
            box=box.SIMPLE_HEAD, border_style="yellow",
            header_style="bold yellow", show_header=True,
        )
        req_table.add_column("#",            justify="right", min_width=3, style="dim")
        req_table.add_column("Requirement",  min_width=60)
        req_table.add_column("Match",        justify="center", min_width=6)

        matching_lower = [s.lower() for s in matching]
        for idx, req in enumerate(req_parsed, 1):
            matched = any(m in req.lower() for m in matching_lower)
            match_t = Text("✅", style="green") if matched else Text("—", style="dim")
            req_table.add_row(str(idx), req, match_t)

        console.print(req_table)

        if not req_parsed and job.get("requirements_raw"):
            console.print(Panel(
                job["requirements_raw"],
                title="📋 Requirements (Raw)", border_style="yellow",
            ))

    if nice:
        nice_t = Table(title="Nice to Have", box=box.SIMPLE, border_style="dim",
                       show_header=False)
        nice_t.add_column("#", style="dim", justify="right", min_width=3)
        nice_t.add_column("Item")
        for idx, n in enumerate(nice, 1):
            nice_t.add_row(str(idx), n)
        console.print(nice_t)

    if resp:
        resp_t = Table(title="Responsibilities", box=box.SIMPLE, border_style="dim",
                       show_header=False)
        resp_t.add_column("#", style="dim", justify="right", min_width=3)
        resp_t.add_column("Responsibility")
        for idx, r in enumerate(resp, 1):
            resp_t.add_row(str(idx), r)
        console.print(resp_t)

    # Full JD
    if job.get("description_full"):
        console.print(Rule("Full Job Description", style="dim"))
        console.print(Panel(
            job["description_full"],
            title="📄 Complete JD (verbatim)", border_style="dim",
        ))

    # Company context
    if job.get("company_context"):
        console.print(Panel(
            job["company_context"],
            title="🏢 Company Research", border_style="cyan",
        ))

    # Audit trail
    history = await get_job_history(job_id)
    if history:
        hist_t = Table(
            title="📜 Full Audit Trail",
            box=box.SIMPLE_HEAD, border_style="dim", header_style="bold dim",
        )
        hist_t.add_column("When",     min_width=18, style="dim")
        hist_t.add_column("Type",     min_width=12)
        hist_t.add_column("From",     min_width=12, style="dim")
        hist_t.add_column("→ To",     min_width=18)
        hist_t.add_column("By",       min_width=8, style="dim")
        hist_t.add_column("Note",     style="dim")
        for h in history:
            ec = "cyan" if h["entity_type"] == "application" else "white"
            hist_t.add_row(
                fmt_date(h["changed_at"]),
                Text(h["entity_type"].upper(), style=ec),
                h.get("from_status") or "—",
                Text(h["to_status"].upper(), style="bold"),
                h.get("changed_by") or "system",
                (h.get("note") or "")[:64],
            )
        console.print(hist_t)


# ── Interview prep view ───────────────────────────────────────────────────────

async def render_interview_prep(job_id: str) -> None:
    job  = await get_job(job_id)
    prep = await get_interview_prep(job_id)

    if not job:
        console.print(f"[red]Job {job_id} not found.[/red]")
        return

    console.print(Panel(
        f"[bold]Interview Prep[/bold]: {job['title']} @ {job['company']}\n"
        f"[dim]Score: {job['match_score']}/100 | Status: {job['status'].upper()}[/dim]",
        border_style="magenta", title="[bold magenta]🎙 Interview Prep[/bold magenta]",
    ))

    if not prep:
        console.print("[yellow]No interview prep generated yet for this job.[/yellow]\n"
                      "[dim]Run the prep agent once the application status is 'interviewing'.[/dim]")
        return

    if prep.get("company_research"):
        console.print(Panel(prep["company_research"],
                            title="🏢 Company Research", border_style="cyan"))

    if prep.get("role_analysis"):
        console.print(Panel(prep["role_analysis"],
                            title="🔍 Role Analysis", border_style="cyan"))

    questions = parse_json_field(prep.get("likely_questions"))
    answers   = parse_json_field(prep.get("suggested_answers"))
    if questions:
        q_tbl = Table(title="❓ Likely Interview Questions + Suggested Answers",
                      box=box.ROUNDED, border_style="magenta",
                      header_style="bold magenta", show_header=True)
        q_tbl.add_column("#",        justify="right", min_width=3, style="dim")
        q_tbl.add_column("Question", min_width=40)
        q_tbl.add_column("Suggested Answer (STAR)", min_width=50)
        for idx, q in enumerate(questions):
            ans = answers[idx] if idx < len(answers) else "—"
            q_tbl.add_row(str(idx+1), q, ans)
        console.print(q_tbl)

    ask = parse_json_field(prep.get("questions_to_ask"))
    if ask:
        ask_t = Table(title="💬 Questions for the Interviewer",
                      box=box.SIMPLE, border_style="dim", show_header=False)
        ask_t.add_column("#", style="dim", justify="right", min_width=3)
        ask_t.add_column("Question")
        for idx, q in enumerate(ask, 1):
            ask_t.add_row(str(idx), q)
        console.print(ask_t)

    if prep.get("technical_prep"):
        console.print(Panel(prep["technical_prep"],
                            title="🛠 Technical Prep", border_style="yellow"))

    if prep.get("red_flags"):
        console.print(Panel(prep["red_flags"],
                            title="⚠ Red Flags / Gaps to Address", border_style="red"))

    talking = parse_json_field(prep.get("talking_points"))
    if talking:
        tp_t = Table(title="💡 Key Talking Points",
                     box=box.SIMPLE, border_style="green", show_header=False)
        tp_t.add_column("#", style="dim", justify="right", min_width=3)
        tp_t.add_column("Point")
        for idx, t in enumerate(talking, 1):
            tp_t.add_row(str(idx), t)
        console.print(tp_t)

    if prep.get("salary_strategy"):
        console.print(Panel(prep["salary_strategy"],
                            title="💰 Salary Negotiation Strategy", border_style="green"))


# ── Entry point ───────────────────────────────────────────────────────────────

async def main(
    job_id: Optional[str] = None,
    prep_id: Optional[str] = None,
    watch: bool = False,
) -> None:
    if job_id:
        await render_job_detail(job_id)
    elif prep_id:
        await render_interview_prep(prep_id)
    elif watch:
        while True:
            console.clear()
            await render_dashboard()
            await asyncio.sleep(30)
    else:
        await render_dashboard()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Hire Lord Dashboard")
    parser.add_argument("--job",   help="Full detail + JD + requirements for a job ID")
    parser.add_argument("--prep",  help="Interview prep for a job ID")
    parser.add_argument("--watch", action="store_true", help="Auto-refresh every 30s")
    args = parser.parse_args()
    asyncio.run(main(job_id=args.job, prep_id=args.prep, watch=args.watch))
