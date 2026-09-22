"""
app.py - DIAGNOSTIC ONLY. This does not search anything yet.

Purpose: answer two questions before writing any real scraping logic:
  1. Does banchedati.corteconti.it block Render's server (like it blocked
     this environment's own fetch attempts), or does a real Playwright
     browser get through where a plain HTTP request didn't?
  2. What does the actual search form look like (field names, button
     selectors) - so the next version can use real selectors instead
     of guesses.

Visit /diagnose on the deployed app to run the check. It returns:
  - whether the page loaded at all (or got blocked/errored)
  - the page title (a quick sanity check)
  - a list of every <input> and <button> found on the page, with their
    name/id/placeholder attributes - this is what tells us the real
    field names to use for an actual search
  - a screenshot, saved and served back, so you can SEE what loaded
    (useful if it's a CAPTCHA/block page rather than the real search form)
"""

import os
from playwright.async_api import async_playwright
from fastapi import FastAPI
from fastapi.responses import JSONResponse, FileResponse

app = FastAPI()

TARGET_URL = "https://banchedati.corteconti.it/"
SCREENSHOT_PATH = "/tmp/diagnostic_screenshot.png"


@app.get("/diagnose")
async def diagnose():
    result = {"target_url": TARGET_URL}

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        page = await browser.new_page(
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                       "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
        )

        try:
            response = await page.goto(TARGET_URL, timeout=30000, wait_until="networkidle")
            result["http_status"] = response.status if response else None
            result["page_title"] = await page.title()

            # Give any client-side rendering a moment to finish
            await page.wait_for_timeout(2000)

            # Collect every input and button on the page - this is the
            # real form structure, not a guess
            inputs = await page.eval_on_selector_all(
                "input",
                "els => els.map(e => ({tag: 'input', type: e.type, name: e.name, "
                "id: e.id, placeholder: e.placeholder}))"
            )
            buttons = await page.eval_on_selector_all(
                "button, input[type=submit]",
                "els => els.map(e => ({tag: e.tagName, id: e.id, "
                "text: e.innerText || e.value}))"
            )
            result["inputs_found"] = inputs
            result["buttons_found"] = buttons

            # A quick heuristic flag for whether this looks like a block/
            # CAPTCHA page rather than the real site
            body_text = (await page.inner_text("body"))[:500]
            result["body_text_preview"] = body_text
            result["looks_blocked"] = any(
                kw in body_text.lower() for kw in ["captcha", "access denied", "blocked", "forbidden"]
            )

            await page.screenshot(path=SCREENSHOT_PATH, full_page=True)
            result["screenshot_available"] = True

        except Exception as e:
            result["error"] = str(e)
            result["screenshot_available"] = False
        finally:
            await browser.close()

    return JSONResponse(result)


@app.get("/screenshot")
def screenshot():
    if os.path.exists(SCREENSHOT_PATH):
        return FileResponse(SCREENSHOT_PATH, media_type="image/png")
    return JSONResponse({"error": "No screenshot yet - call /diagnose first"}, status_code=404)


@app.get("/")
def home():
    return {"message": "Diagnostic app. Visit /diagnose to test the Corte dei Conti site, then /screenshot to see what loaded."}
