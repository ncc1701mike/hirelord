"""
Hire Lord — LinkedIn Playwright Scraper
=========================================
Uses your saved LinkedIn session to:
  1. Fetch full job descriptions from LinkedIn job pages
  2. Detect application type (Easy Apply vs external URL)
  3. Extract the external application URL when present

This module is the bridge between Gmail discovery (job IDs)
and the Application Router (form filling + submission).

Requires:
  - linkedin_session.json (run linkedin_auth.py first)
  - playwright + chromium installed
"""

import asyncio
import json
import re
from pathlib import Path
from typing import Optional
from dataclasses import dataclass

ROOT         = Path(__file__).parent.parent.parent
SESSION_FILE = ROOT / "linkedin_session.json"

USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/120.0.0.0 Safari/537.36"
)

# ── Data structures ───────────────────────────────────────────────────────────

@dataclass
class LinkedInJob:
    job_id:          str
    title:           str
    company:         str
    location:        str
    description:     str
    requirements:    str
    salary_text:     str
    remote_type:     str
    employment_type: str
    posted_at:       str
    apply_type:      str   # "easy_apply" | "external" | "unknown"
    apply_url:       str   # external URL if apply_type == "external"
    company_url:     str
    raw_url:         str


# ── Browser context factory ───────────────────────────────────────────────────

async def get_browser_context(playwright, headless: bool = True):
    """Create an authenticated browser context using saved LinkedIn session."""
    if not SESSION_FILE.exists():
        raise FileNotFoundError(
            f"LinkedIn session not found at {SESSION_FILE}\n"
            "Run: uv run python -m hirelord.tools.linkedin_auth"
        )

    browser = await playwright.chromium.launch(
        headless=headless,
        args=["--no-sandbox", "--disable-blink-features=AutomationControlled"],
    )
    context = await browser.new_context(
        storage_state=str(SESSION_FILE),
        user_agent=USER_AGENT,
        viewport={"width": 1280, "height": 800},
        locale="en-US",
    )

    # Mask automation signals
    await context.add_init_script("""
        Object.defineProperty(navigator, 'webdriver', { get: () => undefined });
    """)

    return browser, context


# ── Job details fetcher ───────────────────────────────────────────────────────

async def fetch_linkedin_job(
    job_id: str,
    headless: bool = True,
    timeout: int = 20000,
) -> Optional[LinkedInJob]:
    """
    Fetch full job details from a LinkedIn job page using saved session.
    Returns None if the job can't be fetched.
    """
    try:
        from playwright.async_api import async_playwright
    except ImportError:
        print("  ❌ Playwright not installed")
        return None

    url = f"https://www.linkedin.com/jobs/view/{job_id}/"

    async with async_playwright() as p:
        browser, context = await get_browser_context(p, headless=headless)
        page = await context.new_page()

        try:
            await page.goto(url, timeout=30000, wait_until="domcontentloaded")
            await asyncio.sleep(3) # Small human-like delay

            # Check if redirected to login (session expired)
            if "login" in page.url or "authwall" in page.url:
                print(f"  ⚠  LinkedIn session expired — re-run linkedin_auth.py")
                return None

            # ── Extract job details ───────────────────────────────────────────

            title = ""
            company = ""
            location = ""
            description = ""
            salary_text = ""
            posted_at = ""
            apply_type = "unknown"
            apply_url = ""
            company_url = ""

            # Title
            title = await page.evaluate("""() => {
                const selectors = [
                    'h1.job-details-jobs-unified-top-card__job-title',
                    'h1.t-24.t-bold',
                    '.job-details-jobs-unified-top-card__job-title',
                    '.jobs-unified-top-card__job-title',
                    'h1',
                ];
                for (const sel of selectors) {
                    const el = document.querySelector(sel);
                    if (el && el.textContent.trim()) {
                        return el.textContent.trim();
                    }
                }
                return '';
            }""")

            # Fallback to page title
            if not title:
                page_title = await page.title()
                if " at " in page_title:
                    title = page_title.split(" at ")[0].strip()
                elif " | " in page_title:
                    title = page_title.split(" | ")[0].strip()

            # Company
            company = await page.evaluate("""() => {
                const selectors = [
                    '.job-details-jobs-unified-top-card__company-name a',
                    '.jobs-unified-top-card__company-name a',
                    '.job-details-jobs-unified-top-card__primary-description-container a',
                    'a[data-tracking-control-name*="company"]',
                    '.topcard__org-name-link',
                    '[class*="company-name"] a',
                    '[class*="company-name"]',
                ];
                for (const sel of selectors) {
                    const el = document.querySelector(sel);
                    if (el && el.textContent.trim()) return el.textContent.trim();
                }
                return '';
            }""")

            # Fallback: parse from page title "Job Title | Company | LinkedIn"
            if not company:
                page_title = await page.title()
                parts = page_title.split(" | ")
                if len(parts) >= 2:
                    company = parts[1].strip()

            # Location
            location = await page.evaluate("""() => {
                const selectors = [
                    '.job-details-jobs-unified-top-card__bullet',
                    '.job-details-jobs-unified-top-card__primary-description-container span',
                    '.jobs-unified-top-card__bullet',
                    '.topcard__flavor--bullet',
                    'span.tvm__text',
                ];
                for (const sel of selectors) {
                    const els = document.querySelectorAll(sel);
                    for (const el of els) {
                        const text = el.textContent.trim();
                        if (text && (text.includes(',') || text.includes('Remote') 
                            || text.includes('United States') || text.includes('Anywhere'))) {
                            return text;
                        }
                    }
                }
                return '';
            }""")

            # Salary (may not always be present)
            for selector in [
                ".job-details-jobs-unified-top-card__job-insight span",
                ".compensation__salary",
                "[class*='salary']",
            ]:
                el = page.locator(selector).first
                if await el.count() > 0:
                    text = (await el.text_content() or "").strip()
                    if "$" in text or "/hr" in text or "year" in text.lower():
                        salary_text = text
                        break

            # Job description — the main content
            # Wait for it to render then extract
            try:
                # Wait for description container to appear
                await page.wait_for_selector(
                    "#job-details, .jobs-description, [class*='description']",
                    timeout=10000,
                )
            except Exception:
                pass  # Continue anyway and try to extract

            description = await page.evaluate("""() => {
                const selectors = [
                    '#job-details',
                    '.jobs-description__content',
                    '.jobs-description',
                    '[class*="job-details__main-content"]',
                    '[class*="description__text"]',
                    '.jobs-box__html-content',
                    '[class*="jobs-description"]',
                ];
                for (const sel of selectors) {
                    const el = document.querySelector(sel);
                    if (el) {
                        const text = el.innerText || el.textContent;
                        if (text && text.trim().length > 100) {
                            return text.trim();
                        }
                    }
                }
                // Last resort — grab all text from main content area
                const main = document.querySelector('main') || document.querySelector('.scaffold-layout__main');
                if (main) {
                    return main.innerText.trim();
                }
                return '';
            }""")

            # If still empty, try scrolling to trigger lazy load
            if not description or len(description) < 100:
                await page.evaluate("window.scrollTo(0, 500)")
                await asyncio.sleep(2)
                description = await page.evaluate("""() => {
                    const els = document.querySelectorAll('[class*="description"], [class*="job-details"]');
                    for (const el of els) {
                        const text = el.innerText || el.textContent || '';
                        if (text.trim().length > 200) return text.trim();
                    }
                    return '';
                }""")

             # ← NEW: Clean up description — extract just the JD content
            if description and "About the job" in description:
                description = description.split("About the job", 1)[1].strip()
            elif description and "About the Job" in description:
                description = description.split("About the Job", 1)[1].strip()

            noise_phrases = [
                "Use AI to assess how you fit",
                "Show match details",
                "Tailor my resume",
                "Help me stand out",
                "Retry Premium",
                "Get AI-powered advice",
            ]
            if description:
                lines = description.split("\n")
                clean_lines = [l for l in lines 
                                if not any(n in l for n in noise_phrases)]
                description = "\n".join(clean_lines).strip()

            # Detect apply type
            apply_info = await page.evaluate("""() => {
                // Easy Apply button
                const easyBtns = document.querySelectorAll('button');
                for (const btn of easyBtns) {
                    if (btn.textContent.includes('Easy Apply')) {
                        return { type: 'easy_apply', url: '' };
                    }
                }
                // External apply link
                const applyLinks = document.querySelectorAll(
                    'a[href*="apply"], a[data-tracking-control-name*="apply"]'
                );
                for (const a of applyLinks) {
                    const href = a.href;
                    if (href && !href.includes('linkedin.com/jobs/view')) {
                        return { type: 'external', url: href };
                    }
                }
                // Apply button that opens popup
                const applyBtns = document.querySelectorAll('button');
                for (const btn of applyBtns) {
                    if (btn.textContent.trim() === 'Apply') {
                        return { type: 'external', url: '' };
                    }
                }
                return { type: 'unknown', url: '' };
            }""")

            apply_type = apply_info.get("type", "unknown")
            apply_url  = apply_info.get("url", "")
            # Decode LinkedIn redirect URLs
            if "linkedin.com/redir/redirect" in apply_url:
                from urllib.parse import unquote, urlparse, parse_qs
                parsed = urlparse(apply_url)
                qs = parse_qs(parsed.query)
                if "url" in qs:
                    apply_url = unquote(qs["url"][0])

            # If external with no URL yet, click the button to get the redirect
            if apply_type == "external" and not apply_url:
                try:
                    apply_btn = page.locator("button:has-text('Apply'):not(:has-text('Easy'))").first
                    if await apply_btn.count() > 0:
                        async with page.expect_popup(timeout=8000) as popup_info:
                            await apply_btn.click()
                        popup = await popup_info.value
                        await popup.wait_for_load_state("domcontentloaded", timeout=8000)
                        apply_url = popup.url
                        await popup.close()
                except Exception:
                    pass
            # Posted date
            for selector in [
                ".job-details-jobs-unified-top-card__posted-date",
                ".jobs-unified-top-card__posted-date",
                "[class*='posted-date']",
            ]:
                el = page.locator(selector).first
                if await el.count() > 0:
                    posted_at = (await el.text_content() or "").strip()
                    break

            # Infer remote type
            remote_type = ""
            combined = (title + " " + location + " " + description).lower()
            if "remote" in combined:
                remote_type = "remote"
            elif "hybrid" in combined:
                remote_type = "hybrid"
            elif location and location.lower() not in ("", "united states", "anywhere"):
                remote_type = "onsite"

            job = LinkedInJob(
                job_id=job_id,
                title=title,
                company=company,
                location=location,
                description=description,
                requirements="",   # extracted by JD parser downstream
                salary_text=salary_text,
                remote_type=remote_type,
                employment_type="full_time",
                posted_at=posted_at,
                apply_type=apply_type,
                apply_url=apply_url,
                company_url=company_url,
                raw_url=url,
            )

            if not title:
                print(f"  DEBUG: could not extract title")
                content = await page.content()
                print(content[2000:3000])
                return None
            return job

        except Exception as e:
            import traceback
            print(f"  ❌ Error fetching job {job_id}: {e}")
            print(traceback.format_exc())
            return None

        finally:
            await context.close()
            await browser.close()


# ── Batch fetcher ─────────────────────────────────────────────────────────────

async def fetch_linkedin_jobs_batch(
    job_ids: list[str],
    headless: bool = True,
    delay_between: float = 2.0,
    max_concurrent: int = 2,
) -> list[LinkedInJob]:
    """
    Fetch multiple LinkedIn jobs with rate limiting.
    Uses a semaphore to limit concurrent browser instances.
    """
    semaphore = asyncio.Semaphore(max_concurrent)
    results = []

    async def fetch_one(job_id: str, index: int, total: int):
        async with semaphore:
            print(f"  [{index}/{total}] Fetching LinkedIn job {job_id}...")
            job = await fetch_linkedin_job(job_id, headless=headless)
            if job:
                print(f"    ✅ {job.title} @ {job.company} ({job.apply_type})")
                results.append(job)
            else:
                print(f"    ⚠  Could not fetch job {job_id}")
            # Polite delay between requests
            await asyncio.sleep(delay_between)

    tasks = [
        fetch_one(job_id, i+1, len(job_ids))
        for i, job_id in enumerate(job_ids)
    ]
    await asyncio.gather(*tasks)
    return results


# ── Apply URL classifier ──────────────────────────────────────────────────────

def classify_ats(url: str) -> str:
    """
    Classify an external application URL by ATS type.
    Returns: greenhouse | lever | workday | mercor | ashby |
             smartrecruiters | jobvite | icims | taleo | generic
    """
    url_lower = url.lower()
    if "greenhouse.io" in url_lower or "boards.greenhouse" in url_lower:
        return "greenhouse"
    elif "lever.co" in url_lower:
        return "lever"
    elif "myworkdayjobs.com" in url_lower or "workday.com" in url_lower:
        return "workday"
    elif "app.mercor.com" in url_lower or "mercor.com" in url_lower:
        return "mercor"
    elif "ashbyhq.com" in url_lower:
        return "ashby"
    elif "smartrecruiters.com" in url_lower:
        return "smartrecruiters"
    elif "jobvite.com" in url_lower:
        return "jobvite"
    elif "icims.com" in url_lower:
        return "icims"
    elif "taleo.net" in url_lower:
        return "taleo"
    elif "careers." in url_lower or "jobs." in url_lower:
        return "company_careers"
    else:
        return "generic"


# ── CLI for testing ───────────────────────────────────────────────────────────

if __name__ == "__main__":
    import sys

    if "--test" in sys.argv:
        # Test with the Limit Break job from earlier
        test_id = "4389850777"
        if len(sys.argv) > 2 and sys.argv[2] != "--test":
            test_id = sys.argv[2]

        print(f"Testing LinkedIn scraper with job ID: {test_id}")
        job = asyncio.run(fetch_linkedin_job(test_id, headless=False))

        if job:
            print(f"\n✅ Job fetched successfully!")
            print(f"   Title:       {job.title}")
            print(f"   Company:     {job.company}")
            print(f"   Location:    {job.location}")
            print(f"   Apply type:  {job.apply_type}")
            print(f"   Apply URL:   {job.apply_url[:80] if job.apply_url else '—'}")
            print(f"   Remote:      {job.remote_type}")
            print(f"   Salary:      {job.salary_text or '—'}")
            print(f"\n   Description preview:")
            print(f"   {job.description[:800]}...")
        else:
            print("❌ Could not fetch job")
    else:
        print("Usage:")
        print("  uv run python -m hirelord.tools.linkedin_scraper --test")
        print("  uv run python -m hirelord.tools.linkedin_scraper --test 4389850777")
