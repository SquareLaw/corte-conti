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
from playwright.async_api import async_playwright
from fastapi import FastAPI
from fastapi.responses import JSONResponse, FileResponse
import anthropic

app = FastAPI()
claude_client = anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])

TARGET_URL = "https://banchedati.corteconti.it/"
SCREENSHOT_1 = "/tmp/screenshot_1_initial.png"
SCREENSHOT_2 = "/tmp/screenshot_2_after_search.png"
SCREENSHOT_3 = "/tmp/screenshot_3_after_click.png"
MAX_CHARS_PER_DOC = 6000  # cap sent to Claude - keeps cost bounded per search


async def safe(coro, default=None):
    """Run a diagnostic step without letting its failure kill the others."""
    try:
        return await coro
    except Exception as e:
        return {"error": str(e)} if default is None else default


async def extract_results(q: str, n: int) -> dict:
    """Core extraction loop: for each of the first n results, load the
    search fresh, click into that specific result, and grab the full text.
    Re-runs the search per result rather than navigating 'back' from the
    document viewer, since no reliable back-navigation method is known yet."""
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

                await page.wait_for_selector(
                    'app-cmp-pag-table-cdc tr.parent button[title="Vai al dettaglio"]',
                    timeout=20000
                )
                await page.wait_for_timeout(800)

                detail_buttons = page.locator(
                    'app-cmp-pag-table-cdc tr.parent button[title="Vai al dettaglio"]'
                )
                count = await detail_buttons.count()
                if count == 0:
                    await page.wait_for_timeout(2000)
                    count = await detail_buttons.count()
                if i >= count:
                    errors.append(f"Result {i}: only {count} results available on this page")
                    break

                await detail_buttons.nth(i).click()
                try:
                    await page.wait_for_selector("text=Identificativo locale", timeout=15000)
                except Exception:
                    pass
                await page.wait_for_timeout(1000)

                full_text = await page.inner_text("body")

                # Best-effort structured fields when present (delibere have
                # them; sentenze often don't - that's fine, Claude reads the
                # raw text either way).
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
                })

            except Exception as e:
                errors.append(f"Result {i}: {str(e)}")
            finally:
                await page.close()

        await browser.close()

    return {"extracted": extracted, "errors": errors}


@app.get("/search")
async def search(q: str, n: int = 5):
    """The real endpoint: search, extract, filter+summarize via Claude."""
    extraction = await extract_results(q, n)
    docs = extraction["extracted"]

    if not docs:
        return JSONResponse({
            "query": q,
            "summary": "Nessun documento estratto - il motore di ricerca del portale non ha restituito risultati, o l'estrazione e' fallita.",
            "extraction_errors": extraction["errors"],
        })

    context_blocks = []
    for d in docs:
        riferimento = d["identificativo_locale"] or f"documento {d['result_index']} (riferimento non estratto automaticamente - vedi testo)"
        testo = d["testo_completo"][:MAX_CHARS_PER_DOC]
        context_blocks.append(f"[Risultato {d['result_index']} - {riferimento}]\n{testo}")

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
        "sintesi di 2-3 frasi, e spiega perche' e' pertinente\n"
        "3. Se NON pertinente: scartalo, indicando in una riga il motivo\n\n"
        "Rispondi ESCLUSIVAMENTE sulla base del testo fornito - non inventare "
        "riferimenti o contenuti non presenti nei documenti."
    )

    response = claude_client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=1500,
        system=system_prompt,
        messages=[{"role": "user", "content": f"{context}\n\nDomanda: {q}"}],
    )
    summary = "".join(block.text for block in response.content if block.type == "text")

    return JSONResponse({
        "query": q,
        "documents_extracted": len(docs),
        "summary": summary,
        "extraction_errors": extraction["errors"],
    })


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
    return {
        "message": "Corte dei Conti search filter/summarizer",
        "version": "v4-search-with-claude",
        "endpoints": ["/search?q=...&n=5", "/diagnose_all", "/screenshot/1", "/screenshot/2"],
    }
