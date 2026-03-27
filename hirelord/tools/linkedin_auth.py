"""
Hire Lord — LinkedIn Session Auth
===================================
One-time setup: opens a real Chrome window so you can log in to LinkedIn manually.
Saves your session cookies to linkedin_session.json for all future automated runs.

Run once:
    uv run python -m hirelord.tools.linkedin_auth

After that, Playwright uses your saved session automatically.
Never need to log in again unless cookies expire (~1 year).
"""

import asyncio
import json
from pathlib import Path

ROOT         = Path(__file__).parent.parent.parent
SESSION_FILE = ROOT / "linkedin_session.json"


async def save_linkedin_session():
    try:
        from playwright.async_api import async_playwright
    except ImportError:
        print("❌ Playwright not installed. Run: uv add playwright && uv run playwright install chromium")
        return

    print("\n👑 Hire Lord — LinkedIn Session Setup")
    print("=" * 50)
    print("A Chrome window will open.")
    print("1. Log in to LinkedIn normally")
    print("2. Once you see your LinkedIn feed, come back here")
    print("3. Press Enter to save your session")
    print("=" * 50)

    async with async_playwright() as p:
        # Launch visible (non-headless) browser so you can log in
        browser = await p.chromium.launch(headless=False)
        context = await browser.new_context(
            viewport={"width": 1280, "height": 800},
            user_agent=(
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/120.0.0.0 Safari/537.36"
            ),
        )
        page = await context.new_page()

        print("\nOpening LinkedIn...")
        await page.goto("https://www.linkedin.com/login")

        print("\n✋ Log in to LinkedIn in the browser window.")
        print("   When you see your feed/home page, press Enter here.")
        input("\n   Press Enter when logged in → ")

        # Check we're actually logged in
        current_url = page.url
        if "feed" in current_url or "mynetwork" in current_url or "jobs" in current_url:
            print("  ✅ Detected LinkedIn session!")
        else:
            print(f"  ⚠  Current URL: {current_url}")
            print("  Make sure you're fully logged in before continuing.")
            input("  Press Enter again when ready → ")

        # Save cookies + storage state
        state = await context.storage_state()
        SESSION_FILE.write_text(json.dumps(state, indent=2))

        await browser.close()

    print(f"\n✅ Session saved to {SESSION_FILE}")
    print("   Hire Lord will use this session for all LinkedIn automation.")
    print("   Re-run this script if you ever get logged out.\n")


async def verify_session() -> bool:
    """Quick check that the saved session is still valid."""
    try:
        from playwright.async_api import async_playwright
    except ImportError:
        return False

    if not SESSION_FILE.exists():
        return False

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        context = await browser.new_context(
            storage_state=str(SESSION_FILE),
            user_agent=(
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/120.0.0.0 Safari/537.36"
            ),
        )
        page = await context.new_page()

        try:
            await page.goto("https://www.linkedin.com/feed/", timeout=15000)
            await page.wait_for_load_state("domcontentloaded", timeout=10000)
            url = page.url
            valid = "feed" in url or "mynetwork" in url
        except Exception:
            valid = False
        finally:
            await browser.close()

    return valid


if __name__ == "__main__":
    import sys

    if "--verify" in sys.argv:
        print("Verifying LinkedIn session...")
        valid = asyncio.run(verify_session())
        if valid:
            print("✅ Session is valid — Hire Lord is authenticated.")
        else:
            print("❌ Session expired or not found.")
            print("   Run: uv run python -m hirelord.tools.linkedin_auth")
    else:
        asyncio.run(save_linkedin_session())
