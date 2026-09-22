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
                for (let i = 0; i < Math.min(rows.length, 4); i++) {
                    sample.push(rows[i].outerHTML);
                }
                // Also specifically hunt for anything clickable inside the
                // first real data row (row index 1, since 0 is the header)
                let clickable_in_row = [];
                if (rows.length > 1) {
                    const dataRow = rows[1];
                    const candidates = dataRow.querySelectorAll(
                        'a, button, [routerlink], [role="button"], .cursor-pointer, i, svg, fa-icon'
                    );
                    clickable_in_row = Array.from(candidates).map(el => ({
                        tag: el.tagName,
                        class: el.className ? el.className.toString() : '',
                        href: el.getAttribute ? el.getAttribute('href') : null,
                        routerlink: el.getAttribute ? el.getAttribute('routerlink') : null,
                        text: el.innerText ? el.innerText.trim() : '',
                        title: el.getAttribute ? el.getAttribute('title') : null
                    }));
                }
                return {
                    found: true,
                    row_count: rows.length,
                    sample_rows_html: sample,
                    clickable_elements_in_first_data_row: clickable_in_row
                };
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

        # --- 5. Click the real "Vai al dettaglio" button (not the row itself) ---
        click_attempt = {"success": False}
        try:
            detail_button = await page.query_selector(
                'app-cmp-pag-table-cdc button[title="Vai al dettaglio"]'
            )
            if detail_button:
                url_before = page.url
                # Watch for either: (a) a new tab opening, or (b) a file download
                # starting - either is plausible for a "view document" action
                popup_promise = page.context.wait_for_event("page", timeout=8000)
                download_promise = page.wait_for_event("download", timeout=8000)

                await detail_button.click()
                await page.wait_for_timeout(2000)

                click_attempt["success"] = True
                click_attempt["url_before"] = url_before
                click_attempt["url_after_same_page"] = page.url

                # Check if a new tab/popup opened
                try:
                    popup = await popup_promise
                    await popup.wait_for_load_state(timeout=8000)
                    click_attempt["opened_new_tab"] = True
                    click_attempt["new_tab_url"] = popup.url
                    click_attempt["new_tab_title"] = await popup.title()
                    click_attempt["new_tab_text_preview"] = (await popup.inner_text("body"))[:2000]
                except Exception:
                    click_attempt["opened_new_tab"] = False

                # Check if a download started instead
                try:
                    download = await download_promise
                    click_attempt["triggered_download"] = True
                    click_attempt["download_filename"] = download.suggested_filename
                    click_attempt["download_url"] = download.url
                except Exception:
                    click_attempt["triggered_download"] = False
            else:
                click_attempt["error"] = "Could not find the 'Vai al dettaglio' button"
        except Exception as e:
            click_attempt["error"] = str(e)

        try:
            await page.screenshot(path=SCREENSHOT_3, full_page=True)
            result["screenshot_3_available"] = True
        except Exception:
            result["screenshot_3_available"] = False
        result["click_attempt"] = click_attempt

        result["page_text_preview"] = await safe(page.inner_text("body"), "")
        if isinstance(result["page_text_preview"], str):
            result["page_text_preview"] = result["page_text_preview"][:3000]

        await browser.close()

    return JSONResponse(result)


@app.get("/extract_results")
async def extract_results(q: str = "accesso agli atti appalti", n: int = 5):
    """The real extraction loop: for each of the first n results, load the
    search fresh, click into that specific result, and parse out the
    structured fields we identified (Identificativo locale, Organo
    emittente, Tipo deliberazione, Descrizione, Testo provvedimento).

    Re-runs the search per result rather than trying to navigate 'back'
    from the document viewer, since we don't yet know a reliable way to
    do that in this Angular app - slower, but much less likely to break.
    """
    import re

    extracted = []
    errors = []

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)

        for i in range(n):
            page = await browser.new_page(
                user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                           "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
            )
            try:
                await page.goto(TARGET_URL, timeout=30000, wait_until="networkidle")
                await page.fill("#inputRicerca", q)
                await page.click("#buttonSearch")

                # Wait for actual result rows to appear, rather than a fixed
                # delay - a fresh browser context (no cache) can be slower
                # to render than the warmed-up session used in earlier tests.
                await page.wait_for_selector(
                    'app-cmp-pag-table-cdc tr.parent button[title="Vai al dettaglio"]',
                    timeout=20000
                )

                detail_buttons = page.locator(
                    'app-cmp-pag-table-cdc tr.parent button[title="Vai al dettaglio"]'
                )
                count = await detail_buttons.count()
                if i >= count:
                    errors.append(f"Result {i}: only {count} results available on this page")
                    break

                await detail_buttons.nth(i).click()

                # Wait for the actual document viewer content to appear,
                # not just a fixed delay
                try:
                    await page.wait_for_selector("text=Identificativo locale", timeout=15000)
                except Exception:
                    pass  # fall through - extraction below will report if text truly never appeared
                await page.wait_for_timeout(1000)  # brief settle time after content appears

                full_text = await page.inner_text("body")

                def extract_field(label, next_label, text):
                    pattern = re.escape(label) + r"\s*\n+(.*?)\n+\s*" + re.escape(next_label)
                    match = re.search(pattern, text, re.DOTALL)
                    return match.group(1).strip() if match else None

                identificativo = extract_field("Identificativo locale", "Organo emittente", full_text)
                organo = extract_field("Organo emittente", "Attiva riferimenti", full_text)
                tipo = extract_field("TIPO DELIBERAZIONE", "DESCRIZIONE", full_text)
                descrizione = extract_field("DESCRIZIONE", "TESTO PROVVEDIMENTO", full_text)

                testo_match = re.search(r"TESTO PROVVEDIMENTO\s*\n+(.*)", full_text, re.DOTALL)
                testo = testo_match.group(1).strip() if testo_match else None

                extracted.append({
                    "result_index": i,
                    "identificativo_locale": identificativo,
                    "organo_emittente": organo,
                    "tipo_deliberazione": tipo,
                    "descrizione": descrizione,
                    "testo_provvedimento_length": len(testo) if testo else 0,
                    "testo_provvedimento_preview": testo[:500] if testo else None,
                    "parse_succeeded": bool(identificativo or organo or testo),
                })

            except Exception as e:
                errors.append(f"Result {i}: {str(e)}")
            finally:
                await page.close()

        await browser.close()

    return JSONResponse({
        "query": q,
        "requested": n,
        "extracted_count": len(extracted),
        "results": extracted,
        "errors": errors,
    })


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
