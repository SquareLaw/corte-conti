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
SEARCH_SCREENSHOT_PATH = "/tmp/search_screenshot.png"


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


@app.get("/diagnose_search")
async def diagnose_search(q: str = "accesso agli atti appalti"):
    """Runs one real search and reports what the results page looks like -
    the next unknown after /diagnose confirmed the form itself works."""
    result = {"target_url": TARGET_URL, "query": q}

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        page = await browser.new_page(
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                       "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
        )

        try:
            await page.goto(TARGET_URL, timeout=30000, wait_until="networkidle")
            await page.fill("#inputRicerca", q)
            await page.click("#buttonSearch")

            # Angular SPA - give it time to render results after the click,
            # rather than trusting networkidle alone
            await page.wait_for_timeout(4000)

            # Broaden capture: don't assume the link pattern (that guess
            # was wrong) - grab every real <a href> AND every element using
            # Angular's routerLink, since Angular apps often use one instead
            # of the other for "clickable result" elements.
            all_links = await page.eval_on_selector_all(
                "a[href]",
                "els => els.map(e => ({href: e.getAttribute('href'), text: e.innerText.trim()}))"
                ".filter(x => x.text)"
            )
            router_links = await page.eval_on_selector_all(
                "[routerlink]",
                "els => els.map(e => ({routerlink: e.getAttribute('routerlink'), "
                "tag: e.tagName, text: e.innerText.trim()}))"
            )
            result["all_links_with_text"] = all_links[:40]  # cap for readability
            result["router_links_found"] = router_links[:40]

            # Angular custom components always use hyphenated tag names
            # (web component standard). Counting these tells us which
            # element repeats ~100 times - a strong signal for "this is
            # the individual result card", since the page shows 100 results
            # loaded. Much more reliable than guessing at text boundaries.
            custom_tag_counts = await page.evaluate("""
                () => {
                    const counts = {};
                    document.querySelectorAll('*').forEach(el => {
                        const tag = el.tagName.toLowerCase();
                        if (tag.includes('-')) counts[tag] = (counts[tag] || 0) + 1;
                    });
                    return counts;
                }
            """)
            result["custom_tag_counts"] = custom_tag_counts

            result["page_text_preview"] = (await page.inner_text("body"))[:4000]

            await page.screenshot(path=SEARCH_SCREENSHOT_PATH, full_page=True)
            result["screenshot_available"] = True

        except Exception as e:
            result["error"] = str(e)
            result["screenshot_available"] = False
        finally:
            await browser.close()

    return JSONResponse(result)


@app.get("/search_screenshot")
def search_screenshot():
    if os.path.exists(SEARCH_SCREENSHOT_PATH):
        return FileResponse(SEARCH_SCREENSHOT_PATH, media_type="image/png")
    return JSONResponse({"error": "No screenshot yet - call /diagnose_search first"}, status_code=404)


@app.get("/screenshot")
def screenshot():
    if os.path.exists(SCREENSHOT_PATH):
        return FileResponse(SCREENSHOT_PATH, media_type="image/png")
    return JSONResponse({"error": "No screenshot yet - call /diagnose first"}, status_code=404)


@app.get("/")
def home():
    return {"message": "Diagnostic app. Visit /diagnose to test the Corte dei Conti site, then /screenshot to see what loaded."}
