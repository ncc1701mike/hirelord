"""
Hire Lord — Job Discovery Tools
=================================
Two discovery sources:
  1. RapidAPI LinkedIn Job Search API  (paid, ~$0.01/req, free tier available)
  2. Indeed RSS feeds                   (free, no API key needed)

Both return normalized JobListing objects ready to feed into the screening pipeline.
"""

import asyncio
import hashlib
import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from typing import Optional
from urllib.parse import quote_plus

import httpx

# ── Normalized job listing ────────────────────────────────────────────────────

@dataclass
class JobListing:
    title: str
    company: str
    location: str
    description: str
    url: str
    source: str                        # "linkedin_rapidapi" | "indeed_rss"
    employment_type: str = "full_time"
    remote_type: str = ""              # "remote" | "hybrid" | "onsite" | ""
    salary_range_text: str = ""
    posted_at: str = ""
    company_linkedin: str = ""
    job_id: str = ""                   # source-specific ID for deduplication

    def __post_init__(self):
        # Auto-detect remote type from title/description if not set
        if not self.remote_type:
            combined = (self.title + " " + self.location + " " + self.description).lower()
            if "remote" in combined:
                self.remote_type = "remote"
            elif "hybrid" in combined:
                self.remote_type = "hybrid"
            elif self.location and self.location.lower() not in ("", "anywhere"):
                self.remote_type = "onsite"

    @property
    def dedup_key(self) -> str:
        """Stable hash for deduplication — company + normalized title."""
        normalized = re.sub(r"[^a-z0-9]", "", (self.company + self.title).lower())
        return hashlib.md5(normalized.encode()).hexdigest()[:16]


# ── RapidAPI LinkedIn Job Search ──────────────────────────────────────────────

RAPIDAPI_ENDPOINT = "https://linkedin-job-search-api.p.rapidapi.com/active-jb-7d"

async def search_linkedin_rapidapi(
    keywords: list[str],
    location: str = "United States",
    remote_only: bool = True,
    limit_per_keyword: int = 10,
    api_key: str = "",
) -> list[JobListing]:
    """
    Search LinkedIn jobs via RapidAPI.
    API: https://rapidapi.com/fantastic-jobs-fantastic-jobs-default/api/linkedin-job-search-api
    Cost: ~$0.01/request on paid tier. Free tier: 100 req/month.
    """
    if not api_key:
        print("  ⚠  RAPIDAPI_KEY not set — skipping LinkedIn RapidAPI search")
        return []

    headers = {
        "x-rapidapi-key":  api_key,
        "x-rapidapi-host": "linkedin-job-search-api.p.rapidapi.com",
    }

    results = []
    seen_keys = set()

    async with httpx.AsyncClient(timeout=30) as client:
        for keyword in keywords:
            params = {
                "keywords":    keyword,
                "location":    location,
                "dateSincePosted": "past Week",
                "jobType":     "full time",
                "remoteFilter": "remote" if remote_only else "",
                "salary":      "",
                "experienceLevel": "",
                "limit":       str(limit_per_keyword),
                "page":        "0",
            }

            try:
                print(f"  🔎 LinkedIn RapidAPI: '{keyword}'...")
                resp = await client.get(RAPIDAPI_ENDPOINT, headers=headers, params=params)
                resp.raise_for_status()
                data = resp.json()

                jobs = data if isinstance(data, list) else data.get("jobs", [])
                for job in jobs:
                    listing = JobListing(
                        title=job.get("title", ""),
                        company=job.get("company", {}).get("name", "") if isinstance(job.get("company"), dict) else job.get("company", ""),
                        location=job.get("location", ""),
                        description=job.get("description", ""),
                        url=job.get("jobUrl", job.get("url", "")),
                        source="linkedin_rapidapi",
                        employment_type=job.get("employmentType", "full_time").lower().replace(" ", "_"),
                        salary_range_text=job.get("salaryRange", ""),
                        posted_at=job.get("postedAt", ""),
                        company_linkedin=job.get("company", {}).get("url", "") if isinstance(job.get("company"), dict) else "",
                        job_id=job.get("id", ""),
                    )
                    if listing.dedup_key not in seen_keys and listing.title:
                        seen_keys.add(listing.dedup_key)
                        results.append(listing)

                await asyncio.sleep(0.5)  # Rate limit courtesy pause

            except httpx.HTTPStatusError as e:
                print(f"  ❌ LinkedIn RapidAPI error for '{keyword}': {e.response.status_code}")
            except Exception as e:
                print(f"  ❌ LinkedIn RapidAPI error for '{keyword}': {e}")

    print(f"  ✅ LinkedIn RapidAPI: {len(results)} unique jobs found")
    return results


# ── Indeed RSS Feed ───────────────────────────────────────────────────────────

INDEED_RSS_BASE = "https://www.indeed.com/rss"

def _build_indeed_rss_url(query: str, location: str = "", remote: bool = True) -> str:
    """Build an Indeed RSS URL for a keyword + location search."""
    q = query
    if remote:
        q += " remote"
    params = f"q={quote_plus(q)}"
    if location:
        params += f"&l={quote_plus(location)}"
    params += "&sort=date&fromage=7"  # last 7 days, sorted by date
    return f"{INDEED_RSS_BASE}?{params}"


def _parse_indeed_description(raw_html: str) -> str:
    """Strip HTML tags from Indeed description."""
    clean = re.sub(r"<[^>]+>", " ", raw_html)
    clean = re.sub(r"\s+", " ", clean).strip()
    return clean[:8000]  # Cap at 8k chars


async def search_indeed_rss(
    keywords: list[str],
    location: str = "",
    remote_only: bool = True,
    limit_per_keyword: int = 15,
) -> list[JobListing]:
    """
    Search Indeed jobs via RSS feed — completely free, no API key.
    Returns up to limit_per_keyword results per keyword.
    """
    results = []
    seen_keys = set()

    async with httpx.AsyncClient(
        timeout=30,
        headers={"User-Agent": "Mozilla/5.0 (compatible; HireLord/1.0)"},
        follow_redirects=True,
    ) as client:
        for keyword in keywords:
            url = _build_indeed_rss_url(keyword, location, remote_only)
            try:
                print(f"  🔎 Indeed RSS: '{keyword}'...")
                resp = await client.get(url)
                resp.raise_for_status()

                root = ET.fromstring(resp.text)
                channel = root.find("channel")
                if channel is None:
                    continue

                items = channel.findall("item")[:limit_per_keyword]
                for item in items:
                    title_el    = item.find("title")
                    company_el  = item.find("source")
                    link_el     = item.find("link")
                    desc_el     = item.find("description")
                    date_el     = item.find("pubDate")
                    guid_el     = item.find("guid")

                    title    = title_el.text    if title_el    is not None else ""
                    link     = link_el.text     if link_el     is not None else ""
                    desc_raw = desc_el.text     if desc_el     is not None else ""
                    posted   = date_el.text     if date_el     is not None else ""
                    job_id   = guid_el.text     if guid_el     is not None else link

                    # Indeed title format: "Job Title - Company Name - Location"
                    company = ""
                    loc_str = ""
                    if " - " in (title or ""):
                        parts = title.split(" - ")
                        if len(parts) >= 3:
                            title   = parts[0].strip()
                            company = parts[1].strip()
                            loc_str = parts[2].strip()
                        elif len(parts) == 2:
                            title   = parts[0].strip()
                            company = parts[1].strip()

                    if company_el is not None and company_el.text:
                        company = company_el.text

                    description = _parse_indeed_description(desc_raw or "")

                    listing = JobListing(
                        title=title,
                        company=company or "Unknown",
                        location=loc_str or location or "Remote",
                        description=description,
                        url=link,
                        source="indeed_rss",
                        posted_at=posted,
                        job_id=job_id or link,
                    )

                    if listing.dedup_key not in seen_keys and listing.title:
                        seen_keys.add(listing.dedup_key)
                        results.append(listing)

                await asyncio.sleep(1.0)  # Be a good citizen

            except ET.ParseError:
                print(f"  ⚠  Indeed RSS parse error for '{keyword}' — skipping")
            except httpx.HTTPStatusError as e:
                print(f"  ❌ Indeed RSS error for '{keyword}': {e.response.status_code}")
            except Exception as e:
                print(f"  ❌ Indeed RSS error for '{keyword}': {e}")

    print(f"  ✅ Indeed RSS: {len(results)} unique jobs found")
    return results


# ── Combined discovery ────────────────────────────────────────────────────────

async def discover_jobs(
    linkedin_api_key: str = "",
    use_indeed: bool = True,
    use_linkedin: bool = True,
) -> list[JobListing]:
    """
    Run both discovery sources in parallel and return deduplicated results.
    """
    # Mike's target search keywords
    KEYWORDS = [
        "Unity XR Developer",
        "Unity VR Developer",
        "Unity AR Developer",
        "Unity MR Developer",
        "XR Developer Unity",
        "VR Developer Unity",
        "Unity 3D Developer XR",
        "AI Engineer Unity",
        "AI Training Engineer",
        "Machine Learning Engineer Unity",
    ]

    tasks = []
    if use_linkedin and linkedin_api_key:
        tasks.append(search_linkedin_rapidapi(
            keywords=KEYWORDS[:5],   # Top 5 for LinkedIn (credit conservation)
            remote_only=True,
            api_key=linkedin_api_key,
        ))
    if use_indeed:
        tasks.append(search_indeed_rss(
            keywords=KEYWORDS,
            remote_only=True,
        ))

    all_results = []
    if tasks:
        results_list = await asyncio.gather(*tasks, return_exceptions=True)
        for res in results_list:
            if isinstance(res, Exception):
                print(f"  ❌ Discovery source error: {res}")
            else:
                all_results.extend(res)

    # Final dedup across sources
    seen = set()
    deduped = []
    for job in all_results:
        if job.dedup_key not in seen:
            seen.add(job.dedup_key)
            deduped.append(job)

    print(f"\n  📋 Total unique jobs discovered: {len(deduped)}")
    return deduped
