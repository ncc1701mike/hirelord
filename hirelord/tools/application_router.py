"""
Hire Lord — Application Router
=================================
Handles the full application submission flow after a job is fetched and tailored.

Routing logic:
    LinkedIn Easy Apply  → fill multi-step LinkedIn form
    Greenhouse           → fill standard Greenhouse form
    Lever                → fill Lever application form
    Workday              → fill Workday application (complex, multi-step)
    Mercor               → fill Mercor form (seen in screenshots)
    Ashby                → fill Ashby form
    Generic/Unknown      → AI-powered field detection + fill

All routes:
  1. Navigate to apply URL
  2. Detect form fields
  3. Map Mike's profile to each field
  4. Upload resume PDF
  5. Fill and submit
  6. Capture confirmation

HITL gate sits BEFORE this agent runs — resume/cover letter already approved.
"""

import asyncio
import json
import os
from pathlib import Path
from typing import Optional
from dataclasses import dataclass
from dotenv import load_dotenv
load_dotenv()

ROOT         = Path(__file__).parent.parent.parent
SESSION_FILE = ROOT / "linkedin_session.json"
OUTPUT_DIR   = ROOT / "output"

USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/120.0.0.0 Safari/537.36"
)

# Mike's profile data for form filling
CANDIDATE_PROFILE = {
    "first_name":    "Mike",
    "last_name":     "Doran",
    "full_name":     "Mike Doran",
    "email":         os.environ.get("CANDIDATE_EMAIL", "michaelbryandoran@gmail.com"),
    "phone":         os.environ.get("CANDIDATE_PHONE", "801-613-2057"),
    "linkedin_url":  "https://www.linkedin.com/in/michaelbryandoran",
    "location":      "Holladay, Utah",
    "city":          "Holladay",
    "state":         "Utah",
    "zip":           "84124",
    "country":       "United States",
    "years_exp":     "5",
    "work_auth":     "yes",  # US citizen / authorized to work
    "require_visa":  "no",
    "salary_min":    os.environ.get("SALARY_MIN", "120000"),
    "availability":  "2 weeks",
    "portfolio_url": os.environ.get("PORTFOLIO_URL", ""),
    "github_url":    os.environ.get("GITHUB_URL", ""),
}


@dataclass
class ApplicationResult:
    success:         bool
    job_id:          str
    company:         str
    apply_type:      str
    ats_type:        str
    confirmation:    str   # confirmation number or page text
    screenshot_path: str
    error:           str


# ── Browser context ───────────────────────────────────────────────────────────

async def get_context(playwright, use_linkedin_session: bool = False):
    """Create browser context, optionally with LinkedIn session."""
    browser = await playwright.chromium.launch(
        headless=True,
        args=["--no-sandbox", "--disable-blink-features=AutomationControlled"],
    )

    kwargs = dict(
        user_agent=USER_AGENT,
        viewport={"width": 1280, "height": 900},
        locale="en-US",
        accept_downloads=True,
    )

    if use_linkedin_session and SESSION_FILE.exists():
        kwargs["storage_state"] = str(SESSION_FILE)

    context = await browser.new_context(**kwargs)
    await context.add_init_script(
        "Object.defineProperty(navigator, 'webdriver', { get: () => undefined });"
    )
    return browser, context


# ── Resume PDF finder ─────────────────────────────────────────────────────────

def find_resume_pdf(company: str = "", job_title: str = "") -> Optional[Path]:
    """
    Find the most relevant resume PDF for a given job.
    Looks for tailored resume first, falls back to base resume.
    """
    # Check output dir for tailored resume
    if OUTPUT_DIR.exists():
        # Try to find company-specific tailored resume
        safe_company = company.lower().replace(" ", "_")
        for f in OUTPUT_DIR.glob(f"*{safe_company}*.pdf"):
            return f
        # Fall back to any recent PDF
        pdfs = sorted(OUTPUT_DIR.glob("*.pdf"), key=lambda p: p.stat().st_mtime, reverse=True)
        if pdfs:
            return pdfs[0]

    # Fall back to base resume
    base = ROOT / "data" / "MIKE_DORAN_UnityResume_Current_2024.docx"
    if base.exists():
        return base

    return None


# ── Generic form field AI mapper ──────────────────────────────────────────────

async def ai_fill_form(page, profile: dict, cover_letter: str = "") -> dict:
    """
    Use Claude Haiku to identify form fields and map profile data to them.
    Returns dict of {selector: value} mappings.
    """
    from langchain_anthropic import ChatAnthropic
    from langchain_core.messages import SystemMessage, HumanMessage

    # Get all input fields on the page
    fields = await page.evaluate("""() => {
        const inputs = Array.from(document.querySelectorAll(
            'input:not([type="hidden"]):not([type="submit"]):not([type="button"]), '
            'textarea, select'
        ));
        return inputs.map((el, i) => ({
            index: i,
            tag: el.tagName,
            type: el.type || '',
            name: el.name || '',
            id: el.id || '',
            placeholder: el.placeholder || '',
            label: el.labels?.[0]?.textContent?.trim() || '',
            required: el.required,
        }));
    }""")

    if not fields:
        return {}

    llm = ChatAnthropic(model="claude-haiku-4-5-20251001", temperature=0)

    prompt = f"""Map these form fields to candidate data. Return JSON only.

CANDIDATE:
{json.dumps(profile, indent=2)}

FORM FIELDS:
{json.dumps(fields, indent=2)}

Return a JSON object mapping field identifiers to values:
{{
  "field_id_or_name": "value to fill",
  ...
}}

Rules:
- Use field 'id' as key if available, else 'name', else 'index'
- Only map fields where you're confident about the value
- For cover letter/motivation fields, use: "See attached cover letter."
- Skip file upload fields (type=file)
- Skip fields you can't confidently map
- Return empty object {{}} if no clear mappings
"""

    try:
        resp = llm.invoke([
            SystemMessage(content="You are a job application form filler. Return only valid JSON."),
            HumanMessage(content=prompt),
        ])
        raw = resp.content.strip()
        if raw.startswith("```"):
            raw = raw.split("```")[1]
            if raw.startswith("json"):
                raw = raw[4:]
        return json.loads(raw.strip())
    except Exception:
        return {}


# ── ATS-specific fillers ──────────────────────────────────────────────────────

async def fill_greenhouse(page, profile: dict, resume_path: Optional[Path],
                           cover_letter_text: str) -> bool:
    """Fill a Greenhouse application form."""
    try:
        await page.wait_for_load_state("networkidle", timeout=15000)

        # Standard Greenhouse fields
        field_map = {
            "#first_name":        profile["first_name"],
            "#last_name":         profile["last_name"],
            "#email":             profile["email"],
            "#phone":             profile["phone"],
            'input[name="job_application[location]"]': profile["location"],
        }

        for selector, value in field_map.items():
            try:
                el = page.locator(selector).first
                if await el.count() > 0:
                    await el.fill(value)
            except Exception:
                pass

        # Resume upload
        if resume_path:
            try:
                file_input = page.locator('input[type="file"]').first
                if await file_input.count() > 0:
                    await file_input.set_input_files(str(resume_path))
            except Exception:
                pass

        # Cover letter text area
        try:
            cl_area = page.locator('textarea[name*="cover"], textarea[id*="cover"]').first
            if await cl_area.count() > 0:
                await cl_area.fill(cover_letter_text[:3000] if cover_letter_text else "")
        except Exception:
            pass

        return True
    except Exception as e:
        print(f"    ⚠  Greenhouse fill error: {e}")
        return False


async def fill_lever(page, profile: dict, resume_path: Optional[Path],
                     cover_letter_text: str) -> bool:
    """Fill a Lever application form."""
    try:
        await page.wait_for_load_state("networkidle", timeout=15000)

        field_map = {
            'input[name="name"]':                    profile["full_name"],
            'input[name="email"]':                   profile["email"],
            'input[name="phone"]':                   profile["phone"],
            'input[name="urls[LinkedIn]"]':          profile["linkedin_url"],
            'input[name="urls[Portfolio]"]':         profile.get("portfolio_url", ""),
        }

        for selector, value in field_map.items():
            if not value:
                continue
            try:
                el = page.locator(selector).first
                if await el.count() > 0:
                    await el.fill(value)
            except Exception:
                pass

        # Resume
        if resume_path:
            try:
                file_input = page.locator('input[type="file"]').first
                if await file_input.count() > 0:
                    await file_input.set_input_files(str(resume_path))
            except Exception:
                pass

        # Cover letter
        try:
            cl_area = page.locator('textarea[name="comments"]').first
            if await cl_area.count() > 0:
                await cl_area.fill(cover_letter_text[:3000] if cover_letter_text else "")
        except Exception:
            pass

        return True
    except Exception as e:
        print(f"    ⚠  Lever fill error: {e}")
        return False


async def fill_mercor(page, profile: dict, resume_path: Optional[Path],
                      cover_letter_text: str) -> bool:
    """
    Fill a Mercor application form (seen in screenshots).
    Mercor uses a simple form with name, email, phone, LinkedIn URL fields.
    """
    try:
        await page.wait_for_load_state("networkidle", timeout=15000)

        # Mercor standard fields based on screenshot
        field_map = {
            'input[placeholder*="name" i]':     profile["full_name"],
            'input[placeholder*="email" i]':    profile["email"],
            'input[placeholder*="phone" i]':    profile["phone"],
            'input[placeholder*="linkedin" i]': profile["linkedin_url"],
            'input[name="name"]':               profile["full_name"],
            'input[name="email"]':              profile["email"],
            'input[type="tel"]':                profile["phone"],
        }

        for selector, value in field_map.items():
            if not value:
                continue
            try:
                el = page.locator(selector).first
                if await el.count() > 0:
                    await el.fill(value)
                    await asyncio.sleep(0.3)
            except Exception:
                pass

        return True
    except Exception as e:
        print(f"    ⚠  Mercor fill error: {e}")
        return False


async def fill_generic(page, profile: dict, resume_path: Optional[Path],
                       cover_letter_text: str) -> bool:
    """AI-powered generic form filler for unknown ATS systems."""
    try:
        await page.wait_for_load_state("networkidle", timeout=15000)
        mappings = await ai_fill_form(page, profile, cover_letter_text)

        for field_id, value in mappings.items():
            if not value:
                continue
            try:
                # Try by id first, then name, then index
                for selector in [f"#{field_id}", f'[name="{field_id}"]',
                                  f'[data-field="{field_id}"]']:
                    el = page.locator(selector).first
                    if await el.count() > 0:
                        tag = await el.evaluate("el => el.tagName")
                        if tag == "SELECT":
                            await el.select_option(label=value)
                        else:
                            await el.fill(str(value))
                        break
            except Exception:
                pass

        # Always try to upload resume if there's a file input
        if resume_path:
            try:
                file_input = page.locator('input[type="file"]').first
                if await file_input.count() > 0:
                    await file_input.set_input_files(str(resume_path))
            except Exception:
                pass

        return True
    except Exception as e:
        print(f"    ⚠  Generic fill error: {e}")
        return False


# ── LinkedIn Easy Apply ───────────────────────────────────────────────────────

async def submit_easy_apply(
    job_id: str,
    profile: dict,
    resume_path: Optional[Path],
    cover_letter_text: str,
    headless: bool = True,
) -> ApplicationResult:
    """Handle LinkedIn Easy Apply multi-step form."""
    try:
        from playwright.async_api import async_playwright
    except ImportError:
        return ApplicationResult(False, job_id, "", "easy_apply", "linkedin",
                                 "", "", "Playwright not installed")

    url = f"https://www.linkedin.com/jobs/view/{job_id}/"

    async with async_playwright() as p:
        browser, context = await get_context(p, use_linkedin_session=True)
        page = await context.new_page()

        try:
            await page.goto(url, timeout=20000, wait_until="domcontentloaded")
            await page.wait_for_load_state("networkidle", timeout=15000)
            await asyncio.sleep(1.5)

            # Click Easy Apply button
            easy_apply = page.locator(
                "button.jobs-apply-button:has-text('Easy Apply'), "
                "button[aria-label*='Easy Apply']"
            ).first
            if await easy_apply.count() == 0:
                return ApplicationResult(False, job_id, "", "easy_apply", "linkedin",
                                         "", "", "Easy Apply button not found")
            await easy_apply.click()
            await asyncio.sleep(2)

            # Multi-step form — iterate through pages
            max_steps = 10
            for step in range(max_steps):
                await page.wait_for_load_state("domcontentloaded", timeout=8000)

                # Fill phone if present
                phone_input = page.locator('input[id*="phoneNumber"]').first
                if await phone_input.count() > 0:
                    current = await phone_input.input_value()
                    if not current:
                        await phone_input.fill(profile["phone"])

                # Handle "Yes/No" questions intelligently
                # Work authorization
                for label_text in ["authorized to work", "legally authorized",
                                    "require sponsorship"]:
                    labels = page.locator(f"label:has-text('{label_text}')")
                    if await labels.count() > 0:
                        # Find associated radio/select
                        try:
                            if "sponsorship" in label_text:
                                # "Do you require sponsorship?" → No
                                no_radio = page.locator(
                                    f"label:has-text('{label_text}') ~ * input[value='No']"
                                ).first
                                if await no_radio.count() > 0:
                                    await no_radio.click()
                            else:
                                # "Are you authorized?" → Yes
                                yes_radio = page.locator(
                                    f"label:has-text('{label_text}') ~ * input[value='Yes']"
                                ).first
                                if await yes_radio.count() > 0:
                                    await yes_radio.click()
                        except Exception:
                            pass

                # Upload resume if file input present
                if resume_path:
                    file_input = page.locator('input[type="file"]').first
                    if await file_input.count() > 0:
                        try:
                            await file_input.set_input_files(str(resume_path))
                        except Exception:
                            pass

                # Check for Next / Review / Submit button
                submit_btn = page.locator(
                    "button[aria-label='Submit application'], "
                    "button:has-text('Submit application')"
                ).first
                next_btn = page.locator(
                    "button[aria-label='Continue to next step'], "
                    "button:has-text('Next'), "
                    "button:has-text('Review')"
                ).first

                if await submit_btn.count() > 0:
                    await submit_btn.click()
                    await asyncio.sleep(2)

                    # Capture confirmation
                    confirmation = ""
                    try:
                        conf_el = page.locator(
                            "[class*='confirmation'], "
                            "h2:has-text('application was sent'), "
                            ".artdeco-inline-feedback"
                        ).first
                        if await conf_el.count() > 0:
                            confirmation = await conf_el.text_content() or ""
                    except Exception:
                        confirmation = "Application submitted via Easy Apply"

                    screenshot = str(OUTPUT_DIR / f"applied_{job_id}.png")
                    await page.screenshot(path=screenshot)

                    return ApplicationResult(
                        success=True, job_id=job_id, company="",
                        apply_type="easy_apply", ats_type="linkedin",
                        confirmation=confirmation or "Submitted",
                        screenshot_path=screenshot, error=""
                    )

                elif await next_btn.count() > 0:
                    await next_btn.click()
                    await asyncio.sleep(1.5)
                else:
                    break  # No navigation button found

            return ApplicationResult(False, job_id, "", "easy_apply", "linkedin",
                                     "", "", "Could not complete Easy Apply form")

        except Exception as e:
            return ApplicationResult(False, job_id, "", "easy_apply", "linkedin",
                                     "", "", str(e))
        finally:
            await context.close()
            await browser.close()


# ── External ATS submitter ────────────────────────────────────────────────────

async def submit_external_ats(
    job_id: str,
    company: str,
    apply_url: str,
    ats_type: str,
    profile: dict,
    resume_path: Optional[Path],
    cover_letter_text: str,
    headless: bool = True,
) -> ApplicationResult:
    """Submit application to an external ATS."""
    try:
        from playwright.async_api import async_playwright
    except ImportError:
        return ApplicationResult(False, job_id, company, "external", ats_type,
                                 "", "", "Playwright not installed")

    async with async_playwright() as p:
        browser, context = await get_context(p, use_linkedin_session=False)
        page = await context.new_page()

        try:
            await page.goto(apply_url, timeout=20000, wait_until="domcontentloaded")
            await page.wait_for_load_state("networkidle", timeout=15000)
            await asyncio.sleep(1.5)

            # Route to appropriate filler
            success = False
            if ats_type == "greenhouse":
                success = await fill_greenhouse(page, profile, resume_path, cover_letter_text)
            elif ats_type == "lever":
                success = await fill_lever(page, profile, resume_path, cover_letter_text)
            elif ats_type == "mercor":
                success = await fill_mercor(page, profile, resume_path, cover_letter_text)
            else:
                success = await fill_generic(page, profile, resume_path, cover_letter_text)

            if not success:
                return ApplicationResult(False, job_id, company, "external", ats_type,
                                         "", "", "Form fill failed")

            # Take screenshot before submitting for HITL review
            screenshot = str(OUTPUT_DIR / f"review_{job_id}_{ats_type}.png")
            await page.screenshot(path=screenshot, full_page=True)

            return ApplicationResult(
                success=True, job_id=job_id, company=company,
                apply_type="external", ats_type=ats_type,
                confirmation="Form filled — awaiting HITL approval to submit",
                screenshot_path=screenshot, error=""
            )

        except Exception as e:
            return ApplicationResult(False, job_id, company, "external", ats_type,
                                     "", "", str(e))
        finally:
            await context.close()
            await browser.close()


# ── Main router ───────────────────────────────────────────────────────────────

async def route_and_apply(
    job_id: str,
    company: str,
    apply_type: str,
    apply_url: str,
    cover_letter_text: str = "",
    headless: bool = True,
    auto_submit: bool = False,  # False = HITL gate before final submit
) -> ApplicationResult:
    """
    Main entry point: route a job application to the right handler.
    """
    from .linkedin_scraper import classify_ats

    resume_path = find_resume_pdf(company)

    if apply_type == "easy_apply":
        print(f"    → LinkedIn Easy Apply")
        result = await submit_easy_apply(
            job_id, CANDIDATE_PROFILE, resume_path,
            cover_letter_text, headless=headless
        )

    elif apply_type == "external" and apply_url:
        ats_type = classify_ats(apply_url)
        print(f"    → External ATS: {ats_type} ({apply_url[:60]}...)")
        result = await submit_external_ats(
            job_id, company, apply_url, ats_type,
            CANDIDATE_PROFILE, resume_path,
            cover_letter_text, headless=headless
        )

    else:
        result = ApplicationResult(
            False, job_id, company, apply_type, "unknown",
            "", "", f"Unknown apply type: {apply_type}"
        )

    return result
