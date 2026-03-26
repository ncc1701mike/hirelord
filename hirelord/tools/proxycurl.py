"""
Hire Lord — Job Discovery Tools
=================================
Primary source: JSearch API (RapidAPI)
  - Searches Google for Jobs in real-time
  - Covers LinkedIn, Indeed, Glassdoor, ZipRecruiter, Dice + more
  - Free tier: 200 requests/month
  - Sign up: https://rapidapi.com/letscrape-6bRBa3QguO5/api/jsearch

Fallback: Direct company career page RSS (future)
"""

import asyncio
import hashlib
import re
from dataclasses import dataclass
from typing import Optional

import httpx

# ── Normalized job listing ────────────────────────────────────────────────────

@dataclass
class JobListing:
    title: str
    company: str
    location: str
    description: str
    url: str
    source: str
    employment_type: str = "full_time"
    remote_type: str = ""
    salary_range_text: str = ""
    posted_at: str = ""
    company_linkedin: str = ""
    job_id: str = ""

    def __post_init__(self):
        if not self.remote_type:
            combined = (self.title + " " + self.location + " " + self.description).lower()
            if "remote" in combined:
                self.remote_type = "remote"
            elif "hybrid" in combined:
                self.remote_type = "hybrid"
            elif self.location and self.location.lower() not in ("", "anywhere", "united states"):
                self.remote_type = "onsite"

    @property
    def dedup_key(self) -> str:
        normalized = re.sub(r"[^a-z0-9]", "", (self.company + self.title).lower())
        return hashlib.md5(normalized.encode()).hexdigest()[:16]


# ── JSearch API (primary source) ──────────────────────────────────────────────

JSEARCH_ENDPOINT = "https://jsearch.p.rapidapi.com/search"

async def search_jsearch(
    keywords: list[str],
    api_key: str,
    remote_only: bool = True,
    days_posted: int = 7,
    limit_per_keyword: int = 10,
) -> list[JobListing]:
    """
    Search jobs via JSearch API — pulls from Google for Jobs in real-time.
    Covers LinkedIn, Indeed, Glassdoor, ZipRecruiter, Dice and more.
    """
    if not api_key:
        print("  ⚠  RAPIDAPI_KEY not set — skipping JSearch")
        return []

    headers = {
        "x-rapidapi-key":  api_key,
        "x-rapidapi-host": "jsearch.p.rapidapi.com",
        "Content-Type":    "application/json",
    }

    results = []
    seen_keys = set()

    async with httpx.AsyncClient(timeout=30) as client:
        for keyword in keywords:
            query = keyword + (" remote" if remote_only else "")
            params = {
                "query":        query,
                "page":         "1",
                "num_pages":    "1",
                "date_posted":  "week",
                "remote_jobs_only": "true" if remote_only else "false",
                "employment_types": "FULLTIME",
                "country":      "us",
            }

            try:
                print(f"  🔎 JSearch: '{keyword}'...")
                resp = await client.get(
                    JSEARCH_ENDPOINT,
                    headers=headers,
                    params=params,
                )
                resp.raise_for_status()
                data = resp.json()

                jobs = data.get("data", [])[:limit_per_keyword]
                for job in jobs:
                    # Extract salary text
                    sal_min = job.get("job_min_salary")
                    sal_max = job.get("job_max_salary")
                    sal_period = job.get("job_salary_period", "")
                    if sal_min and sal_max:
                        sal_text = f"${sal_min:,.0f} - ${sal_max:,.0f} {sal_period}"
                    elif sal_min:
                        sal_text = f"${sal_min:,.0f}+ {sal_period}"
                    else:
                        sal_text = ""

                    # Build apply URL
                    apply_url = (
                        job.get("job_apply_link") or
                        job.get("job_google_link") or
                        ""
                    )

                    listing = JobListing(
                        title=job.get("job_title", ""),
                        company=job.get("employer_name", ""),
                        location=f"{job.get('job_city', '')}, {job.get('job_state', '')}".strip(", "),
                        description=job.get("job_description", ""),
                        url=apply_url,
                        source=f"jsearch:{job.get('job_publisher', 'google')}",
                        employment_type=job.get("job_employment_type", "FULLTIME").lower(),
                        remote_type="remote" if job.get("job_is_remote") else "",
                        salary_range_text=sal_text,
                        posted_at=job.get("job_posted_at_datetime_utc", ""),
                        company_linkedin=job.get("employer_linkedin", ""),
                        job_id=job.get("job_id", ""),
                    )

                    if listing.dedup_key not in seen_keys and listing.title:
                        seen_keys.add(listing.dedup_key)
                        results.append(listing)

                count = len(jobs)
                print(f"     → {count} jobs returned")
                await asyncio.sleep(0.5)

            except httpx.HTTPStatusError as e:
                print(f"  ❌ JSearch error for '{keyword}': HTTP {e.response.status_code}")
                if e.response.status_code == 429:
                    print("     Rate limited — pausing 5s...")
                    await asyncio.sleep(5)
            except Exception as e:
                print(f"  ❌ JSearch error for '{keyword}': {e}")

    print(f"  ✅ JSearch: {len(results)} unique jobs found")
    return results


# ── Combined discovery ────────────────────────────────────────────────────────

async def discover_jobs(
    linkedin_api_key: str = "",
    use_indeed: bool = True,      # kept for API compat, ignored (Indeed blocked)
    use_linkedin: bool = True,    # kept for API compat, ignored
) -> list[JobListing]:
    """
    Run job discovery via JSearch and return deduplicated results.
    JSearch covers LinkedIn, Indeed, Glassdoor, ZipRecruiter + more in one call.
    """
    api_key = linkedin_api_key  # reuse same RapidAPI key

    # Mike's target search keywords — ordered by priority
    KEYWORDS = [
        "Unity XR Developer",
        "Unity Developer",
        "Unity Game Developer",
        "Unity C#Developer",
        "Unity VR Developer",
        "Unity AR MR Developer",
        "XR Developer Unity C#",
        "XR Developer Unity",
        "XR Developer Unity C#",
        "XR Developer",
        "VR Developer Unity",
        "Unity 3D XR Engineer",
        "AI Engineer Unity",
        "AI Training Engineer",
        "Machine Learning Engineer game",
    ]

    all_results = await search_jsearch(
        keywords=KEYWORDS,
        api_key=api_key,
        remote_only=True,
        days_posted=7,
        limit_per_keyword=10,
    )

    # Final dedup
    seen = set()
    deduped = []
    for job in all_results:
        if job.dedup_key not in seen:
            seen.add(job.dedup_key)
            deduped.append(job)

    print(f"\n  📋 Total unique jobs discovered: {len(deduped)}")
    return deduped