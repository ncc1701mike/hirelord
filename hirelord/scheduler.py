"""
Hire Lord — Scheduler
======================
Runs the discovery agent daily at 7:00 AM Mountain Time.
Also exposes a manual trigger.

Usage:
    uv run python -m hirelord.scheduler          # start scheduler daemon
    uv run python -m hirelord.scheduler --now    # run immediately + schedule
"""

import argparse
import asyncio
import logging
from datetime import datetime

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from rich.console import Console

from .agents.discovery import run_discovery

console = Console()
logging.basicConfig(level=logging.WARNING)  # Suppress APScheduler noise


async def daily_discovery_job():
    """The job that APScheduler calls every morning."""
    console.print(f"\n[bold gold1]⏰ Hire Lord Scheduler — Daily Run[/bold gold1]")
    console.print(f"[dim]{datetime.now().strftime('%B %d, %Y at %I:%M %p')} MT[/dim]\n")
    try:
        await run_discovery(use_indeed=True, use_linkedin=True)
    except Exception as e:
        console.print(f"[red]❌ Discovery run failed: {e}[/red]")


async def main(run_now: bool = False):
    scheduler = AsyncIOScheduler(timezone="America/Denver")

    # Daily at 7:00 AM Mountain Time
    scheduler.add_job(
        daily_discovery_job,
        CronTrigger(hour=7, minute=0, timezone="America/Denver"),
        id="daily_discovery",
        name="Hire Lord Daily Job Discovery",
        replace_existing=True,
    )

    scheduler.start()

    next_run = scheduler.get_job("daily_discovery").next_run_time
    console.print(f"[bold gold1]👑 Hire Lord Scheduler started[/bold gold1]")
    console.print(f"[dim]Next discovery run: {next_run.strftime('%B %d, %Y at %I:%M %p MT')}[/dim]")

    if run_now:
        console.print("\n[yellow]Running discovery now (--now flag)...[/yellow]")
        await daily_discovery_job()

    console.print("\n[dim]Scheduler running. Press Ctrl+C to stop.[/dim]")

    try:
        while True:
            await asyncio.sleep(60)
    except (KeyboardInterrupt, SystemExit):
        scheduler.shutdown()
        console.print("\n[dim]Scheduler stopped.[/dim]")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Hire Lord Scheduler")
    parser.add_argument("--now", action="store_true", help="Run discovery immediately")
    args = parser.parse_args()
    asyncio.run(main(run_now=args.now))
