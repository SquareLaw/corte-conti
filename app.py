"""
app.py - DIAGNOSTIC ONLY. Consolidated: runs every check we've built so
far, plus backup hypotheses, in ONE pass - so we don't need another
redeploy loop for each new idea.

Visit /diagnose_all to run everything at once.
"""

import os
from playwright.async_api import async_playwright
from fastapi import FastAPI
from fastapi.responses import JSONResponse, FileResponse

app = FastAPI()

TARGET_URL = "https://banchedati.corteconti.it/"
SCREENSHOT_1 = "/tmp/screenshot_1_initial.png"
SCREENSHOT_2 = "/tmp/screenshot_2_after_search.png"
SCREENSHOT_3 = "/tmp/screenshot_3_after_click.png"


async def safe(coro, default=None):
    """Run a diagnostic step without letting its failure kill the others."""
    try:
        return await coro
    except Exception as e:
        return {"error": str(e)} if default is None else default


@app.get("/diagnose_all")
async def diagnose_all(q: str = "accesso agli atti appalti"):
    result = {"target_url": TARGET_URL, "query": q}

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        page = await browser.new_page(
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                       "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
        )

        # --- 1. Load the page ---
        try:
            response = await page.goto(TARGET_URL, timeout=30000, wait_until="networkidle")
            result["http_status"] = response.status if response else None
            result["page_title"] = await page.title()
            await page.wait_for_timeout(2000)
            await page.screenshot(path=SCREENSHOT_1, full_page=True)
            result["screenshot_1_available"] = True
        except Exception as e:
            result["load_error"] = str(e)
            result["screenshot_1_available"] = False
            await browser.close()
            return JSONResponse(result)  # nothing else will work if this failed

        # --- 2. Form structure ---
        result["inputs_found"] = await safe(page.eval_on_selector_all(
            "input",
            "els => els.map(e => ({type: e.type, name: e.name, id: e.id, placeholder: e.placeholder}))"
        ), [])
        result["buttons_found"] = await safe(page.eval_on_selector_all(
            "button, input[type=submit]",
            "els => els.map(e => ({id: e.id, text: e.innerText || e.value}))"
        ), [])

        # --- 3. Run the search ---
        try:
            await page.fill("#inputRicerca", q)
            await page.click("#buttonSearch")
            await page.wait_for_timeout(4000)
            result["search_submitted"] = True
            await page.screenshot(path=SCREENSHOT_2, full_page=True)
            result["screenshot_2_available"] = True
        except Exception as e:
            result["search_error"] = str(e)
            result["search_submitted"] = False
            result["screenshot_2_available"] = False
            await browser.close()
            return JSONResponse(result)

        # --- 4. Multiple hypotheses about where results live, all captured together ---
        all_links = await safe(page.eval_on_selector_all(
            "a[href]",
            "els => els.map(e => ({href: e.getAttribute('href'), text: e.innerText.trim()}))"
            ".filter(x => x.text)"
        ), [])
        result["all_links_with_text"] = all_links[:40] if isinstance(all_links, list) else all_links

        result["custom_tag_counts"] = await safe(page.evaluate("""
            () => {
                const counts = {};
                document.querySelectorAll('*').forEach(el => {
                    const tag = el.tagName.toLowerCase();
                    if (tag.includes('-')) counts[tag] = (counts[tag] || 0) + 1;
                });
                return counts;
            }
        """), {})

        result["global_tr_count"] = await safe(page.evaluate(
            "() => document.querySelectorAll('tr').length"
        ), None)

        result["table_cdc_inspection"] = await safe(page.evaluate("""
            () => {
                const container = document.querySelector('app-cmp-pag-table-cdc');
                if (!container) return {found: false};
                const rows = container.querySelectorAll('tr');
                const sample = [];
                for (let i = 0; i < Math.min(rows.length, 3); i++) {
                    sample.push(rows[i].outerHTML.slice(0, 1500));
                }
                return {found: true, row_count: rows.length, sample_rows_html: sample};
            }
        """), {"found": False})

        result["mat_row_count"] = await safe(page.evaluate(
            "() => document.querySelectorAll('[mat-row], mat-row').length"
        ), None)

        result["cdk_virtual_scroll_items"] = await safe(page.evaluate(
            "() => document.querySelectorAll('.cdk-virtual-scroll-content-wrapper > *').length"
        ), None)

        result["clickable_class_candidates"] = await safe(page.evaluate("""
            () => {
                const els = document.querySelectorAll(
                    '[class*="result"], [class*="item"], [class*="card"], [class*="row"]'
                );
                const counts = {};
                els.forEach(el => {
                    const cls = el.className.toString();
                    counts[cls] = (counts[cls] || 0) + 1;
                });
                return counts;
            }
        """), {})

        # --- 5. Attempt to actually click a plausible row and see what happens ---
        # Try the most likely candidates in order; report which (if any) worked.
        click_attempt = {"tried_selectors": [], "success": False}
        candidate_selectors = [
            "app-cmp-pag-table-cdc tr:nth-child(2)",
            "app-cmp-pag-table-cdc tbody tr:first-child",
            "[mat-row]:first-child",
            "app-cnt-results tr:nth-child(2)",
        ]
        for sel in candidate_selectors:
            click_attempt["tried_selectors"].append(sel)
            try:
                el = await page.query_selector(sel)
                if not el:
                    continue
                url_before = page.url
                await el.click(timeout=5000)
                await page.wait_for_timeout(3000)
                url_after = page.url
                click_attempt["success"] = True
                click_attempt["selector_used"] = sel
                click_attempt["url_before"] = url_before
                click_attempt["url_after"] = url_after
                click_attempt["url_changed"] = url_before != url_after
                click_attempt["page_text_after_click"] = (await page.inner_text("body"))[:2000]
                await page.screenshot(path=SCREENSHOT_3, full_page=True)
                result["screenshot_3_available"] = True
                break
            except Exception as e:
                click_attempt[f"error_for_{sel}"] = str(e)
        result["click_attempt"] = click_attempt
        result.setdefault("screenshot_3_available", False)

        result["page_text_preview"] = await safe(page.inner_text("body"), "")
        if isinstance(result["page_text_preview"], str):
            result["page_text_preview"] = result["page_text_preview"][:3000]

        await browser.close()

    return JSONResponse(result)


@app.get("/screenshot/{n}")
def screenshot(n: int):
    paths = {1: SCREENSHOT_1, 2: SCREENSHOT_2, 3: SCREENSHOT_3}
    path = paths.get(n)
    if path and os.path.exists(path):
        return FileResponse(path, media_type="image/png")
    return JSONResponse({"error": f"No screenshot {n} yet - call /diagnose_all first"}, status_code=404)


@app.get("/")
def home():
    return {"message": "Diagnostic app. Visit /diagnose_all to run every check in one pass, then /screenshot/1, /screenshot/2, /screenshot/3 to see each stage."}
