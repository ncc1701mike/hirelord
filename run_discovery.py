"""
Hire Lord — Discovery Runner
==============================
Manually trigger the job discovery pipeline.

Usage:
    uv run python run_discovery.py              # discover + screen + save to DB
    uv run python run_discovery.py --no-indeed  # LinkedIn only
    uv run python run_discovery.py --no-linkedin # Indeed only
    uv run python run_discovery.py --auto-tailor # discover then tailor all strong matches
"""

import argparse
import asyncio
from dotenv import load_dotenv
load_dotenv()

from hirelord.agents.discovery import run_discovery
from hirelord.db.store import get_strong_matches


async def main(
    use_indeed: bool = True,
    use_linkedin: bool = True,
    auto_tailor: bool = False,
):
    result = await run_discovery(
        use_indeed=use_indeed,
        use_linkedin=use_linkedin,
    )

    if auto_tailor:
        queued = result.get("queued_for_tailoring", [])
        if not queued:
            print("\nNo jobs queued for tailoring.")
            return

        print(f"\n👑 Auto-tailoring {len(queued)} jobs...")
        from hirelord.agents.tailor import tailor_for_job
        from hirelord.db.store import get_job

        for job_id in queued:
            job = await get_job(job_id)
            if not job:
                continue
            print(f"\n  Tailoring: {job['title']} @ {job['company']}")
            await tailor_for_job(
                job_title=job["title"],
                company_name=job["company"],
                job_description=job["description_full"],
                job_url=job.get("url", ""),
                location=job.get("location", ""),
                company_context=job.get("company_context", ""),
            )


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Hire Lord — Job Discovery")
    parser.add_argument("--no-indeed",   action="store_true", help="Skip Indeed RSS")
    parser.add_argument("--no-linkedin", action="store_true", help="Skip LinkedIn RapidAPI")
    parser.add_argument("--auto-tailor", action="store_true", help="Tailor all strong matches after discovery")
    args = parser.parse_args()

    asyncio.run(main(
        use_indeed=not args.no_indeed,
        use_linkedin=not args.no_linkedin,
        auto_tailor=args.auto_tailor,
    ))
