#!/usr/bin/env python3
"""UI smoke test: drives the Ask tab against a canned agent stream.

Replaces ``agent.ask`` with a scripted event sequence, so the whole streaming
front end — thinking panel, tool trace, citation chips, verdict badges,
artifact rows — is exercised without spending a token.

    python3 tools/ui_smoke.py            # assert + screenshot to /tmp
    python3 tools/ui_smoke.py --headed   # watch it happen

Requires: playwright (`pip install playwright`) and a Chromium binary.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import tempfile
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

# Self-contained: never touch the operator's real index.
_TMP = tempfile.mkdtemp(prefix="dd-smoke-")
os.environ.setdefault("DD_DATA_DIR", _TMP)
# agent.ask is replaced below, so no request is ever made — but the /api/ask
# route refuses to run without credentials configured.
os.environ.setdefault("ANTHROPIC_API_KEY", "sk-ant-ui-smoke-test-no-request-is-made")

from app import agent, db, docgen, ingest, manifest, search  # noqa: E402

PORT = int(os.environ.get("DD_SMOKE_PORT", "8099"))

SYNTHETIC_CARDS = {
    "pdf": ("Audited financial statements", "audited_accounts", "financial",
            ["Kestrel Holding GmbH"], "FY2024", ["FY2024 revenue EUR 412.6m"], []),
    "xlsx": ("Financial model", "financial_model", "financial",
             ["Kestrel Holding GmbH"], "FY2022-FY2024", ["FY2024 EBITDA EUR 79.2m"], []),
    "pptx": ("Management presentation", "board_deck", "commercial",
             ["Kestrel Holding GmbH"], "March 2025", ["Revenue target EUR 560m by FY2027"],
             ["appears_superseded"]),
    "docx": ("Vendor due diligence report", "vdd_report", "financial",
             ["Vendor adviser"], "FY2024", [], ["draft"]),
    "text": ("Monthly bookings extract", "management_accounts", "commercial", [], "2024", [], []),
}


async def prepare_index() -> None:
    """Ingest the sample data room and stamp cards, without calling the API."""
    corpus = ROOT / "sample-dataroom"
    if not corpus.exists():
        sys.path.insert(0, str(ROOT / "tools"))
        from make_sample_corpus import make

        make(corpus)
    db.init()
    await ingest.IngestJob(corpus.resolve(), ocr=False).run()

    for row in db.rows("SELECT id, family, filename FROM documents WHERE status='extracted'"):
        title, doc_type, ws, parties, period, figures, flags = SYNTHETIC_CARDS.get(
            row["family"], ("Document", "other", "other", [], "", [], [])
        )
        db.execute(
            "UPDATE documents SET status='carded', title=?, doc_type=?, workstream=?, parties=?,"
            " period_covered=?, key_figures=?, summary=?, languages=?, card_flags=?, carded_at=?"
            " WHERE id=?",
            (f"{title} — {row['filename']}", doc_type, ws, json.dumps(parties), period,
             json.dumps(figures), f"Synthetic card for the UI smoke test ({row['family']}).",
             json.dumps(["en"]), json.dumps(flags), time.time(), row["id"]),
        )
    manifest.invalidate_manifest()
    print(f"index ready: {db.scalar('SELECT COUNT(*) FROM documents')} documents "
          f"({db.scalar('SELECT COUNT(*) FROM units')} units) in {os.environ['DD_DATA_DIR']}")


def pick_citations() -> list[str]:
    """Two real citations from the local index, so the reader panel resolves."""
    hits = search.search("revenue", limit=6) + search.search("change of control", limit=6)
    seen: list[str] = []
    for h in hits:
        if h.get("citation") and h["citation"] not in seen:
            seen.append(h["citation"])
    return seen[:2] or ["deadbeefdeadbeef:p1", "deadbeefdeadbeef:p2"]


def canned(question: str, cits: list[str]):
    a, b = (cits + cits)[:2]
    answer = (
        f"Revenue reached EUR 412.6 million in FY2024, up 10.9% year on year [[{a}]].\n\n"
        f"## Evidence\n"
        f"- The audited profit-and-loss statement reports revenue of EUR 412,600 thousand [[{a}]].\n"
        f"- The largest supply agreement can be terminated on a change of control [[{b}]].\n\n"
        f"| Metric | FY2023 | FY2024 |\n| --- | --- | --- |\n"
        f"| Revenue (EUR m) | 372.1 | 412.6 |\n| EBITDA (EUR m) | 66.2 | 79.2 |\n\n"
        f"**Caveat:** the management deck adds back restructuring costs, so its EBITDA "
        f"is not comparable [[{b}]]."
    )

    async def gen():
        yield {"type": "status", "message": "corpus map: 8 documents, ~224 tokens (full mode)",
               "qa_id": "smoke", "manifest": {"mode": "full", "chars": 896, "approx_tokens": 224,
                                              "n_indexed": 8, "n_unindexed": 0}}
        yield {"type": "phase", "phase": "thinking"}
        for chunk in ["Checking the corpus map for audited accounts. ",
                      "The figure should come from the workbook, not the extracted text — ",
                      "I will compute it."]:
            yield {"type": "thinking_delta", "text": chunk}
            await asyncio.sleep(0.05)
        yield {"type": "tool_use", "id": "t1", "name": "search_corpus",
               "input": {"query": "revenue FY2024"}, "label": "revenue FY2024"}
        await asyncio.sleep(0.1)
        yield {"type": "tool_result", "id": "t1", "name": "search_corpus", "ok": True,
               "summary": "6 hits", "payload": {"n_hits": 6}}
        yield {"type": "tool_use", "id": "t2", "name": "run_python",
               "input": {"purpose": "Recompute revenue and EBITDA from the workbook"},
               "label": "Recompute revenue and EBITDA from the workbook"}
        await asyncio.sleep(0.1)
        yield {"type": "tool_result", "id": "t2", "name": "run_python", "ok": True,
               "summary": "exit 0 · revenue FY2024 = 412600", "payload": {"exit_code": 0}}
        yield {"type": "tool_use", "id": "t3", "name": "read_document",
               "input": {"doc_id": a.split(":")[0], "anchor": a.split(":")[1]}, "label": a}
        yield {"type": "tool_result", "id": "t3", "name": "read_document", "ok": False,
               "summary": "error: unknown anchor 'p99'", "payload": {"error": "unknown anchor"}}
        yield {"type": "usage", "turn": 0, "cost_usd": 0.0412,
               "cumulative": {"cost_usd": 0.0412}, "cache_read": 224, "cache_write": 0,
               "input": 1840, "output": 620}
        yield {"type": "phase", "phase": "writing"}
        for i in range(0, len(answer), 24):
            yield {"type": "text_delta", "text": answer[i : i + 24]}
            await asyncio.sleep(0.01)

        art = docgen.create(
            {"kind": "md", "filename": "smoke_memo.md", "title": "Smoke memo",
             "blocks": [{"type": "paragraph", "text": f"Revenue EUR 412.6m [[{a}]]."}]},
            "smoke",
        )
        yield {"type": "artifact", **art}

        citations = agent.parse_citations(answer)
        yield {"type": "citations", "citations": citations}
        for cit, verdict, note in [(a, "supported", ""),
                                   (b, "partial", "The source supports the clause but not the 30-day notice.")]:
            await asyncio.sleep(0.1)
            yield {"type": "verdict", "claim": "…", "citations": [cit],
                   "verdict": verdict, "note": note}
        yield {"type": "done", "qa_id": "smoke", "answer": answer, "citations": citations,
               "verdicts": [{"citations": [a], "verdict": "supported", "note": ""},
                            {"citations": [b], "verdict": "partial", "note": "Notice period differs."}],
               "artifacts": [art], "usage": {"cost_usd": 0.0412}, "duration_s": 6.2,
               "assistant_message": answer}

    return gen()


async def main() -> int:
    headed = "--headed" in sys.argv
    await prepare_index()
    cits = pick_citations()
    print("using citations:", cits)

    agent.ask = lambda question, **kw: canned(question, cits)  # type: ignore[assignment]

    import uvicorn

    from app.server import app

    config = uvicorn.Config(app, host="127.0.0.1", port=PORT, log_level="warning")
    server = uvicorn.Server(config)
    threading.Thread(target=server.run, daemon=True).start()
    for _ in range(100):
        await asyncio.sleep(0.1)
        if server.started:
            break

    from playwright.async_api import async_playwright

    problems: list[str] = []
    async with async_playwright() as p:
        browser = await p.chromium.launch(
            headless=not headed,
            executable_path=os.environ.get("DD_CHROMIUM", "/opt/pw-browsers/chromium"),
        )
        page = await browser.new_page(viewport={"width": 1500, "height": 1000})
        page.on("pageerror", lambda e: problems.append(f"pageerror: {e}"))
        page.on("console", lambda m: problems.append(f"console.error: {m.text}")
                if m.type == "error" else None)

        await page.goto(f"http://127.0.0.1:{PORT}/", wait_until="networkidle")
        await page.click('button[data-tab="ask"]')
        await page.fill("#question", "What was FY2024 revenue, and is the largest contract at risk?")
        await page.click("#askBtn")

        try:
            await page.wait_for_function(
                "() => document.getElementById('runMeta').textContent.includes('done in')",
                timeout=30_000,
            )
        except Exception as exc:  # noqa: BLE001 — diagnose rather than just fail
            await page.screenshot(path="/tmp/ask-timeout.png", full_page=True)
            print("TIMED OUT waiting for the run to finish:", exc)
            print("  toast   :", await page.inner_text("#toast"))
            print("  runMeta :", await page.inner_text("#runMeta"))
            print("  thread  :", (await page.inner_text("#thread"))[:400])
            print("  console :", problems or "none")
            await browser.close()
            return 1
        await page.wait_for_timeout(600)

        checks = {
            "answer rendered": await page.locator(".msg.assistant .body table").count() == 1,
            "citation chips": await page.locator(".msg.assistant .cite").count() >= 3,
            "thinking shown": "Checking the corpus map" in await page.inner_text(".think"),
            "trace ok step": await page.locator(".tstep.ok").count() >= 2,
            "trace err step": await page.locator(".tstep.err").count() == 1,
            "citation panel": await page.locator("#citations .citation").count() >= 2,
            "verdict supported": await page.locator("#citations .v.supported").count() >= 1,
            "verdict partial": await page.locator("#citations .v.partial").count() >= 1,
            "artifact row": await page.locator(".msg.assistant .artifact").count() >= 1,
            "cost in runMeta": "$" in await page.inner_text("#runMeta"),
        }
        await page.screenshot(path="/tmp/ask-answer.png", full_page=True)

        # click a citation chip -> the reader drawer should open on that anchor
        await page.locator(".msg.assistant .cite").first.click()
        await page.wait_for_timeout(900)
        checks["drawer opens from citation"] = await page.locator("#drawer.open").count() == 1
        checks["drawer has text"] = len(await page.inner_text("#drawerText")) > 50
        await page.screenshot(path="/tmp/ask-drawer.png", full_page=True)

        for name, ok in checks.items():
            print(f"  {'PASS' if ok else 'FAIL'}  {name}")
            if not ok:
                problems.append(f"check failed: {name}")
        await browser.close()

    print("\nJS problems:", problems or "none")
    print("screenshots: /tmp/ask-answer.png /tmp/ask-drawer.png")
    return 1 if problems else 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
