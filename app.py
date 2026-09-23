"""
app.py - Corte dei Conti search filter/summarizer.

Pipeline: submit a real search on banchedati.corteconti.it -> extract the
top N results' full text via browser automation -> hand them to Claude,
which filters out irrelevant ones and summarizes what's left, citing the
reference it finds in each document's own text (since different document
types - delibere vs sentenze - use different metadata layouts, so we let
Claude read them the way a person would rather than maintaining multiple
brittle regex parsers).

Main endpoint: /search?q=...&n=5
Diagnostic endpoints kept for troubleshooting: /diagnose_all, /screenshot/N
"""

import os
import re
import uuid
import asyncio
from playwright.async_api import async_playwright
from fastapi import FastAPI
from fastapi.responses import JSONResponse, FileResponse, HTMLResponse
import anthropic

app = FastAPI()
claude_client = anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])

TARGET_URL = "https://banchedati.corteconti.it/"
SCREENSHOT_1 = "/tmp/screenshot_1_initial.png"
SCREENSHOT_2 = "/tmp/screenshot_2_after_search.png"
SCREENSHOT_3 = "/tmp/screenshot_3_after_click.png"
MAX_CHARS_PER_DOC = 6000  # cap sent to Claude - keeps cost bounded per search

# In-memory job store. Fine for a single-instance prototype; a real
# deployment with multiple server instances would need a shared store
# (e.g. a database row) instead, since each instance has its own memory.
JOBS = {}


async def safe(coro, default=None):
    """Run a diagnostic step without letting its failure kill the others."""
    try:
        return await coro
    except Exception as e:
        return {"error": str(e)} if default is None else default


async def extract_results(q: str, n: int, banca_dati: str = "Tutte le banche dati") -> dict:
    """Core extraction: search ONCE, then for each result, open it, extract
    its text, and return to the results list (browser back-navigation,
    falling back to re-clicking search if that doesn't work) instead of
    reloading the whole search from scratch every time - this is the main
    speed fix over the earlier version."""
    extracted = []
    errors = []

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        page = await browser.new_page(
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                       "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
        )

        try:
            await page.goto(TARGET_URL, timeout=30000, wait_until="networkidle")

            if banca_dati != "Tutte le banche dati":
                try:
                    await page.locator("mat-select").nth(0).click()
                    await page.wait_for_timeout(500)
                    await page.locator("mat-option", has_text=banca_dati).click()
                    await page.wait_for_timeout(500)
                except Exception as e:
                    errors.append(f"Could not set banca_dati filter to '{banca_dati}': {e}")

            await page.fill("#inputRicerca", q)
            await page.click("#buttonSearch")
            await page.wait_for_selector(
                'app-cmp-pag-table-cdc tr.parent button[title="Vai al dettaglio"]',
                timeout=20000
            )
            await page.wait_for_timeout(800)

            detail_buttons = page.locator(
                'app-cmp-pag-table-cdc tr.parent button[title="Vai al dettaglio"]'
            )
            download_buttons = page.locator(
                'app-cmp-pag-table-cdc tr.parent button[title^="Scarica allegato"]'
            )

            for i in range(n):
                try:
                    count = await detail_buttons.count()
                    load_more_attempts = 0
                    while i >= count and load_more_attempts < 8:
                        load_more = page.locator(
                            'button:has-text("Carica altri risultati"), '
                            'a:has-text("Carica altri risultati")'
                        )
                        if await load_more.count() == 0:
                            break
                        try:
                            await load_more.first.click()
                            await page.wait_for_timeout(2500)
                        except Exception:
                            break
                        count = await detail_buttons.count()
                        load_more_attempts += 1

                    if i >= count:
                        errors.append(f"Result {i}: only {count} results available even after loading more")
                        break

                    fonte_url = None
                    fonte_url_error = None
                    try:
                        async with page.expect_download(timeout=8000) as download_info:
                            await download_buttons.nth(i).click()
                        download = await download_info.value
                        fonte_url = download.url
                        await download.cancel()
                    except Exception as e:
                        fonte_url_error = str(e)

                    await detail_buttons.nth(i).click()
                    try:
                        await page.wait_for_selector("text=Identificativo locale", timeout=15000)
                    except Exception:
                        pass
                    await page.wait_for_timeout(1000)

                    full_text = await page.inner_text("body")

                    def extract_field(label, next_label, text):
                        pattern = re.escape(label) + r"\s*\n+(.*?)\n+\s*" + re.escape(next_label)
                        match = re.search(pattern, text, re.DOTALL)
                        return match.group(1).strip() if match else None

                    identificativo = extract_field("Identificativo locale", "Organo emittente", full_text)
                    organo = extract_field("Organo emittente", "Attiva riferimenti", full_text)

                    testo_match = re.search(r"TESTO PROVVEDIMENTO\s*\n+(.*)", full_text, re.DOTALL)
                    testo = testo_match.group(1).strip() if testo_match else full_text

                    extracted.append({
                        "result_index": i,
                        "identificativo_locale": identificativo,
                        "organo_emittente": organo,
                        "testo_completo": testo,
                        "fonte_url": fonte_url,
                        "fonte_url_error": fonte_url_error,
                    })
                    if fonte_url_error:
                        errors.append(f"Result {i}: source link capture failed - {fonte_url_error}")

                    # Return to the results list for the next result instead
                    # of reloading the whole search - the actual speed fix.
                    # Try browser back-navigation first; if the Angular app
                    # didn't push a real history entry, fall back to
                    # re-clicking the search button (still much cheaper
                    # than a full page.goto() reload).
                    if i < n - 1:
                        went_back_ok = False
                        try:
                            await page.go_back(timeout=8000)
                            await page.wait_for_selector(
                                'app-cmp-pag-table-cdc tr.parent button[title="Vai al dettaglio"]',
                                timeout=8000
                            )
                            went_back_ok = True
                        except Exception:
                            pass

                        if not went_back_ok:
                            try:
                                await page.click("#buttonSearch")
                                await page.wait_for_selector(
                                    'app-cmp-pag-table-cdc tr.parent button[title="Vai al dettaglio"]',
                                    timeout=15000
                                )
                            except Exception as e:
                                errors.append(f"Result {i}: could not return to results list - {e}")
                                break

                except Exception as e:
                    errors.append(f"Result {i}: {str(e)}")
                    break

        except Exception as e:
            errors.append(f"Setup error: {str(e)}")
        finally:
            await page.close()
            await browser.close()

    return {"extracted": extracted, "errors": errors}
async def run_search_job(job_id: str, q: str, n: int, banca_dati: str = "Tutte le banche dati"):
    """The actual work, run in the background - not tied to any single
    HTTP request's lifetime, so it can take as long as it needs."""
    try:
        JOBS[job_id]["status"] = "extracting"
        extraction = await extract_results(q, n, banca_dati)
        docs = extraction["extracted"]

        if not docs:
            JOBS[job_id]["status"] = "done"
            JOBS[job_id]["result"] = {
                "query": q,
                "summary": "Nessun documento estratto.",
                "extraction_errors": extraction["errors"],
            }
            return

        JOBS[job_id]["status"] = "summarizing"
        context_blocks = []
        for d in docs:
            riferimento = d["identificativo_locale"] or f"documento {d['result_index']} (riferimento non estratto automaticamente - vedi testo)"
            testo = d["testo_completo"][:MAX_CHARS_PER_DOC]
            fonte_line = f"\nLink al documento originale: {d['fonte_url']}" if d.get("fonte_url") else "\nLink al documento originale: non disponibile"
            context_blocks.append(f"[Risultato {d['result_index']} - {riferimento}]{fonte_line}\n{testo}")
        context = "\n\n---\n\n".join(context_blocks)

        system_prompt = (
            "Sei un assistente di ricerca giuridica specializzato nei provvedimenti "
            "della Corte dei Conti (sezioni di controllo e sezioni giurisdizionali). "
            "Ti vengono forniti alcuni documenti recuperati dal motore di ricerca "
            "ufficiale del portale banchedati.corteconti.it, in risposta alla "
            "domanda dell'utente. Il motore di ricerca del portale a volte "
            "restituisce risultati non pertinenti insieme a quelli rilevanti - "
            "il tuo compito e' filtrare.\n\n"
            "Per ciascun documento:\n"
            "1. Valuta se e' effettivamente pertinente alla domanda\n"
            "2. Se PERTINENTE: ricava dal testo stesso un riferimento identificativo "
            "(numero, sezione, tipo di atto - delibera o sentenza), scrivi una "
            "sintesi di 2-3 frasi, e spiega perche' e' pertinente. Includi anche "
            "il link al documento originale fornito (se disponibile) come link "
            "markdown, es: [Apri il documento originale](URL) - se il link non "
            "e' disponibile, dillo esplicitamente invece di ometterlo.\n"
            "3. Se NON pertinente (anche solo marginalmente o indirettamente "
            "collegato): SCARTALO SENZA RIASSUMERLO, indicando solo in una riga "
            "il motivo dello scarto. Non usare categorie intermedie come "
            "'parzialmente pertinente' - o il documento risponde davvero alla "
            "domanda, o va scartato.\n\n"
            "Rispondi ESCLUSIVAMENTE sulla base del testo fornito - non inventare "
            "riferimenti o contenuti non presenti nei documenti."
        )

        response = claude_client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=2000,
            system=system_prompt,
            messages=[{"role": "user", "content": f"{context}\n\nDomanda: {q}"}],
        )
        summary = "".join(block.text for block in response.content if block.type == "text")

        JOBS[job_id]["status"] = "done"
        JOBS[job_id]["result"] = {
            "query": q,
            "documents_extracted": len(docs),
            "summary": summary,
            "extraction_errors": extraction["errors"],
        }

    except Exception as e:
        JOBS[job_id]["status"] = "failed"
        JOBS[job_id]["error"] = str(e)


@app.get("/start_search")
async def start_search(q: str, n: int = 5, banca_dati: str = "Tutte le banche dati"):
    """Kicks off the search in the background and returns immediately with
    a job id - use this instead of waiting on one long request."""
    job_id = str(uuid.uuid4())
    JOBS[job_id] = {"status": "queued", "query": q, "n": n, "banca_dati": banca_dati}
    asyncio.create_task(run_search_job(job_id, q, n, banca_dati))
    return JSONResponse({
        "job_id": job_id,
        "status": "queued",
        "check_status_at": f"/job_status/{job_id}",
        "note": f"This will take roughly {n * 15}-{n * 25} seconds. Poll the status URL above every 15-20 seconds.",
    })


@app.get("/job_status/{job_id}")
def job_status(job_id: str):
    job = JOBS.get(job_id)
    if not job:
        return JSONResponse({"error": "Unknown job_id"}, status_code=404)
    return JSONResponse(job)


@app.get("/search")
async def search(q: str, n: int = 5, banca_dati: str = "Tutte le banche dati"):
    """Kept for small n where blocking is tolerable (n<=3 or so). For
    anything larger, use /start_search + /job_status instead."""
    job_id = str(uuid.uuid4())
    JOBS[job_id] = {"status": "queued", "query": q, "n": n, "banca_dati": banca_dati}
    await run_search_job(job_id, q, n, banca_dati)
    return JSONResponse(JOBS[job_id])


@app.get("/inspect_citation_link")
async def inspect_citation_link(q: str = "accesso agli atti appalti"):
    """One-off diagnostic: open one document and hunt for the real
    permalink pattern the user found (https://banchedati.corteconti.it/{uuid}),
    which is different from the 'Scarica allegato' download link we tried
    before. Checks two things: (1) does a UUID pattern already exist
    somewhere in the page's HTML even though the visible URL doesn't
    change, and (2) does clicking 'Citazione estremi' reveal one."""
    import re as re_module
    result = {}

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        # Grant clipboard permissions - "Citazione estremi" likely copies
        # the link to clipboard rather than displaying it, based on the
        # last test (button clicked successfully, nothing changed on page).
        context = await browser.new_context(
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                       "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
            permissions=["clipboard-read", "clipboard-write"],
        )
        page = await context.new_page()
        try:
            await page.goto(TARGET_URL, timeout=30000, wait_until="networkidle")
            await page.fill("#inputRicerca", q)
            await page.click("#buttonSearch")
            await page.wait_for_selector(
                'app-cmp-pag-table-cdc tr.parent button[title="Vai al dettaglio"]',
                timeout=20000
            )
            detail_buttons = page.locator(
                'app-cmp-pag-table-cdc tr.parent button[title="Vai al dettaglio"]'
            )
            await detail_buttons.nth(0).click()
            try:
                await page.wait_for_selector("text=Identificativo locale", timeout=15000)
            except Exception:
                pass
            await page.wait_for_timeout(1500)

            uuid_pattern = re_module.compile(
                r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", re_module.IGNORECASE
            )

            # Check 1: is a UUID already sitting in the page HTML somewhere?
            html_before = await page.content()
            uuids_found_before = list(set(uuid_pattern.findall(html_before)))
            result["uuids_in_html_before_clicking_citazione"] = uuids_found_before

            # Check 2: click "Citazione estremi" if it exists, see what changes
            citazione_el = page.locator('text="Citazione estremi"')
            result["citazione_estremi_found"] = await citazione_el.count() > 0

            if await citazione_el.count() > 0:
                try:
                    await citazione_el.first.click()
                    await page.wait_for_timeout(1500)
                    html_after = await page.content()
                    uuids_found_after = list(set(uuid_pattern.findall(html_after)))
                    result["uuids_in_html_after_clicking_citazione"] = uuids_found_after
                    result["new_uuids_revealed"] = list(set(uuids_found_after) - set(uuids_found_before))

                    # Also grab visible text near any tooltip/popover that appeared
                    body_text_after = await page.inner_text("body")
                    result["body_text_after_click"] = body_text_after[:1500]
                except Exception as e:
                    result["citazione_click_error"] = str(e)

        except Exception as e:
            result["error"] = str(e)
        finally:
            await browser.close()

    return JSONResponse(result)


@app.get("/inspect_dropdowns")
async def inspect_dropdowns():
    """One-off diagnostic: open every dropdown on the search form (Angular
    Material 'mat-select' components) and capture their option lists -
    specifically looking for the 'Tutte le banche dati' selector, which may
    let us restrict a search to only Giurisdizione or only Controllo."""
    result = {}
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        page = await browser.new_page(
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                       "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
        )
        try:
            await page.goto(TARGET_URL, timeout=30000, wait_until="networkidle")
            await page.wait_for_timeout(2000)

            selects = page.locator("mat-select")
            count = await selects.count()
            result["mat_select_count"] = count

            dropdowns = []
            for i in range(count):
                entry = {"index": i}
                try:
                    select_el = selects.nth(i)
                    entry["visible_text"] = await select_el.inner_text()
                    await select_el.click()
                    await page.wait_for_timeout(500)

                    # mat-option panels render in a CDK overlay, often
                    # appended near the end of <body>, not nested inside
                    # the select itself - so query the whole page for them.
                    options = await page.eval_on_selector_all(
                        "mat-option",
                        "els => els.map(e => e.innerText.trim())"
                    )
                    entry["options"] = options

                    await page.keyboard.press("Escape")
                    await page.wait_for_timeout(300)
                except Exception as e:
                    entry["error"] = str(e)
                dropdowns.append(entry)

            result["dropdowns"] = dropdowns

        except Exception as e:
            result["error"] = str(e)
        finally:
            await browser.close()

    return JSONResponse(result)


@app.get("/diagnose_all")
async def diagnose_all(q: str = "accesso agli atti appalti"):
    """Kept for troubleshooting the page structure if the site changes."""
    result = {"target_url": TARGET_URL, "query": q}

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
            await page.wait_for_timeout(2000)
            await page.screenshot(path=SCREENSHOT_1, full_page=True)
            result["screenshot_1_available"] = True
        except Exception as e:
            result["load_error"] = str(e)
            await browser.close()
            return JSONResponse(result)

        try:
            await page.fill("#inputRicerca", q)
            await page.click("#buttonSearch")
            await page.wait_for_selector(
                'app-cmp-pag-table-cdc tr.parent button[title="Vai al dettaglio"]',
                timeout=20000
            )
            result["search_submitted"] = True
            await page.screenshot(path=SCREENSHOT_2, full_page=True)
            result["screenshot_2_available"] = True
        except Exception as e:
            result["search_error"] = str(e)
            await browser.close()
            return JSONResponse(result)

        await browser.close()

    return JSONResponse(result)


@app.get("/screenshot/{n}")
def screenshot(n: int):
    paths = {1: SCREENSHOT_1, 2: SCREENSHOT_2, 3: SCREENSHOT_3}
    path = paths.get(n)
    if path and os.path.exists(path):
        return FileResponse(path, media_type="image/png")
    return JSONResponse({"error": f"No screenshot {n} yet"}, status_code=404)


@app.get("/")
def home():
    with open("static/index.html", encoding="utf-8") as f:
        return HTMLResponse(f.read())
