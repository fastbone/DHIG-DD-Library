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
os.environ.setdefault("DD_SECRET_KEY", "ui-smoke-secret-not-for-production")
# Deterministic first-run account so the browser can sign in.
os.environ["DD_ADMIN_USER"] = SMOKE_USER = "smoke-admin"
os.environ["DD_ADMIN_PASSWORD"] = SMOKE_PASSWORD = "ui-smoke-password"

from app import agent, auth, db, docgen, ingest, manifest, search  # noqa: E402

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
    auth.bootstrap()
    from app.config import settings

    settings.set_corpus_root(str(corpus.resolve()))
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


def _smoke_zip() -> bytes:
    """A small archive with one traversal attempt, for the upload path."""
    import io
    import zipfile

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("uploaded/board_minutes.md",
                    "# Board minutes\n\nApproved the FY2025 budget of EUR 61.4m.\n" * 30)
        zf.writestr("../escape.md", "must be skipped")
    return buf.getvalue()


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

        # Sign in through the real login form.
        await page.goto(f"http://127.0.0.1:{PORT}/", wait_until="networkidle")
        checks_login = page.url.endswith("/login")
        await page.fill("#username", SMOKE_USER)
        await page.fill("#password", SMOKE_PASSWORD)
        await page.click("#submitBtn")
        await page.wait_for_url(f"http://127.0.0.1:{PORT}/", timeout=15_000)
        await page.wait_for_timeout(800)

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
            "unauthenticated visit lands on /login": checks_login,
            "sign-in returns to the app": page.url.rstrip("/").endswith(str(PORT)),
            "user chip shows the account": SMOKE_USER in await page.inner_text("#userChip"),
            "admin tab visible to an admin":
                await page.locator("#adminTabBtn:not(.hidden)").count() == 1,
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
        await page.keyboard.press("Escape")

        # Admin tab: keys, accounts, storage, audit all render from live data.
        await page.click('button[data-tab="admin"]')
        await page.wait_for_timeout(1500)
        checks["admin: storage areas listed"] = await page.locator("#usageLegend span").count() >= 5
        checks["admin: corpus folder listed"] = await page.locator("#rootList .rowitem").count() >= 1
        checks["admin: own account listed"] = SMOKE_USER in await page.inner_text("#userList")
        checks["admin: audit rows present"] = await page.locator("#auditTable tbody tr").count() >= 1
        checks["admin: key notice when none stored"] = (
            await page.locator("#keyList .notice").count() == 1
        )
        await page.screenshot(path="/tmp/admin.png", full_page=True)

        # Corpus access: the check must run against the live filesystem and say
        # who the process is, since that uid is what a host-side chown needs.
        await page.click("#accessCheckBtn")
        await page.wait_for_timeout(1500)
        identity = await page.inner_text("#accessIdentity")
        checks["admin: access check reports the runtime uid"] = "uid" in identity
        checks["admin: access check reports each root"] = (
            await page.locator("#accessReport .rowitem").count() >= 1
        )
        await page.screenshot(path="/tmp/admin-access.png", full_page=True)

        # Store a (fake) API key through the UI and confirm it is masked.
        await page.fill("#keyLabel", "smoke-key")
        await page.fill("#keyValue", "sk-ant-api03-uismoke-" + "x" * 40)
        await page.click("#addKeyBtn")
        await page.wait_for_timeout(1200)
        key_text = await page.inner_text("#keyList")
        checks["admin: key stored and masked"] = "smoke-key" in key_text and "xxxx" in key_text
        checks["admin: plaintext key not shown"] = "sk-ant-api03-uismoke" not in key_text
        await page.screenshot(path="/tmp/admin-key.png", full_page=True)

        # Upload an archive through the drop zone and watch it extract + ingest.
        await page.click('button[data-tab="corpus"]')
        await page.wait_for_timeout(500)
        await page.set_input_files("#fileInput", {
            "name": "smoke-upload.zip",
            "mimeType": "application/zip",
            "buffer": _smoke_zip(),
        })
        try:
            await page.wait_for_function(
                "() => document.getElementById('archiveList').textContent.includes('smoke-upload')",
                timeout=45_000,
            )
            checks["upload: archive appears in the list"] = True
        except Exception:  # noqa: BLE001
            checks["upload: archive appears in the list"] = False
        await page.wait_for_timeout(3000)
        archive_text = await page.inner_text("#archiveList")
        checks["upload: archive reports extracted files"] = "files" in archive_text
        await page.screenshot(path="/tmp/corpus-upload.png", full_page=True)

        # Connected libraries: the pane renders, the connect form opens, and a
        # sync job event drives the *sync* bar. That last one is the cheap guard
        # against a missing JOB_UI entry, which would silently move the sweep bar
        # instead and look like the sync had gone haywire.
        checks["sync: the libraries pane is visible to an admin"] = (
            await page.locator("#syncSection:not(.hidden)").count() == 1
        )
        checks["sync: an empty list says so"] = "no libraries connected" in (
            await page.inner_text("#syncList")
        )
        await page.click("#addConnectionBtn")
        await page.wait_for_timeout(300)
        checks["sync: the connect form opens"] = (
            await page.locator("#connectModal.open").count() == 1
        )
        # Saving without a secret must be refused in the browser, before a request.
        await page.fill("#cxLabel", "Project X")
        await page.fill("#cxSite", "https://contoso.sharepoint.com/sites/ProjectX")
        await page.fill("#cxTenant", "tenant-id")
        await page.fill("#cxClientId", "client-id")
        await page.click("#connectSave")
        await page.wait_for_timeout(400)
        checks["sync: a missing client secret is caught before submitting"] = (
            "secret is required" in await page.inner_text("#connectStatus")
        )
        checks["sync: the secret field is a password field"] = (
            await page.get_attribute("#cxSecret", "type") == "password"
        )
        # The help link is only worth having if it resolves. A dead link in a
        # credentials form is worse than none — it is read as "there are no docs".
        help_href = await page.get_attribute("#cxHelpLink", "href")
        checks["sync: the form links to the setup guide"] = (
            help_href is not None and help_href.endswith("/help/sharepoint")
        )
        await page.screenshot(path="/tmp/corpus-connect.png", full_page=True)
        await page.click("#connectClose")
        await page.wait_for_timeout(200)
        checks["sync: the form closes"] = await page.locator("#connectModal.open").count() == 0

        # onJob is a top-level function in a classic script, so it is a global.
        await page.evaluate(
            """() => onJob({kind: "job", job_id: "sync-abc12345", job_kind: "sync",
                                     status: "running", total: 10, done: 4, failed: 0,
                                     skipped: 2, deleted: 1, bytes_done: 2048,
                                     message: "4 transferred"})"""
        )
        await page.wait_for_timeout(200)
        checks["sync: a sync job drives the sync bar"] = (
            await page.locator("#syncProgress:not(.hidden)").count() == 1
            and await page.get_attribute("#syncBar", "style") == "width: 40%;"
        )
        sync_meta = await page.inner_text("#syncMeta")
        checks["sync: progress reports transfers, unchanged and deletions"] = (
            "4 / 10" in sync_meta and "2 unchanged" in sync_meta and "1 deleted" in sync_meta
        )
        checks["sync: the sweep bar was not touched"] = (
            await page.locator("#sweepProgress.hidden").count() == 1
        )
        await page.screenshot(path="/tmp/corpus-sync.png", full_page=True)

        # ── the detailed activity log ─────────────────────────────────────
        # Lines injected through the broker, which is the same funnel the real
        # jobs use, so this exercises persistence → query → filter → export
        # rather than a mock of it.
        from app.events import broker as _broker

        _broker.log("routine progress line, nothing wrong", source="ingest")
        _broker.log("a caution worth noticing", level="warn", source="upload")
        _broker.log(
            "quarterly_pack.pdf: FileDataError: cannot open broken document",
            level="error",
            source="ingest",
            job_id="ingest-uismoke1",
            context={
                "rel_path": "01_financial/quarterly_pack.pdf",
                "ext": ".pdf",
                "size_bytes": 91234,
                "exc_type": "FileDataError",
                "traceback": "Traceback (most recent call last):\n"
                             "  File \"app/extract.py\", line 1, in extract_pdf\n"
                             "    pymupdf.open(path)\n"
                             "pymupdf.FileDataError: cannot open broken document",
            },
        )
        await page.click('button[data-tab="sweep"]')
        await page.evaluate("() => loadLog()")
        await page.wait_for_timeout(500)

        rows = await page.locator("#log > div, #log > details").count()
        checks["log: stored lines are listed"] = rows >= 3
        checks["log: counts are summarised in the header"] = (
            "error" in (await page.inner_text("#logCounts"))
        )
        checks["log: a line with context is expandable"] = (
            await page.locator("#log > details.error").count() >= 1
        )
        # Closed by default, so the traceback is one click away rather than in the
        # way of the next line. Open it — a disclosure nobody can open is not one.
        await page.click("#log details.error summary")
        await page.wait_for_timeout(150)
        detail = await page.inner_text("#log details.error[open] .ctx")
        checks["log: the expanded detail carries the path and traceback"] = (
            "01_financial/quarterly_pack.pdf" in detail
            and "Traceback (most recent call last)" in detail
        )
        checks["log: the source is shown on the line"] = (
            "ingest" in await page.inner_text("#log details.error summary")
        )

        # The error filter is the point of the feature: one click to the failures.
        await page.click('#logLevelFilter button[data-levels="error"]')
        await page.wait_for_timeout(500)
        body_text = await page.inner_text("#log")
        checks["log: the error filter hides everything else"] = (
            "quarterly_pack.pdf" in body_text
            and "routine progress line" not in body_text
            and "a caution worth noticing" not in body_text
        )
        await page.click('#logLevelFilter button[data-levels="error,warn"]')
        await page.wait_for_timeout(500)
        body_text = await page.inner_text("#log")
        checks["log: the problems filter keeps warnings too"] = (
            "a caution worth noticing" in body_text and "routine progress line" not in body_text
        )

        await page.click('#logLevelFilter button[data-levels=""]')
        await page.fill("#logSearch", "quarterly_pack")
        await page.wait_for_timeout(700)
        body_text = await page.inner_text("#log")
        checks["log: the text filter narrows to matching lines"] = (
            "quarterly_pack.pdf" in body_text and "routine progress line" not in body_text
        )

        # The export is what gets pasted into a bug report, so assert its content
        # rather than that a button exists.
        report = await page.evaluate("() => logExportText()")
        checks["log: the export carries the traceback for a bug report"] = (
            "DD Library activity log" in report
            and "01_financial/quarterly_pack.pdf" in report
            and "Traceback (most recent call last)" in report
        )
        checks["log: clearing is offered to an admin"] = (
            await page.locator("#logClear:not(.hidden)").count() == 1
        )

        # The reconnect gap. Written straight to the table, deliberately bypassing
        # the broker, so nothing is streamed — exactly the state of a line written
        # while the event stream was down. Log lines are not replayed on reconnect,
        # so catchUpLog is what has to find it.
        await page.fill("#logSearch", "")
        await page.wait_for_timeout(500)
        db.log_record("error", "written while the stream was down", source="sync")
        before_gap = await page.inner_text("#log")
        await page.evaluate("() => catchUpLog()")
        await page.wait_for_timeout(500)
        after_gap = await page.inner_text("#log")
        checks["log: a reconnect picks up lines written while the stream was down"] = (
            "written while the stream was down" not in before_gap
            and "written while the stream was down" in after_gap
        )
        checks["log: catching up does not duplicate lines already shown"] = (
            after_gap.count("quarterly_pack.pdf: FileDataError") == 1
        )
        # The paging cursor must describe what is on screen. It once advanced to a
        # page that trimming had discarded, so every further click paged over
        # history nobody had seen.
        cursor_ok = await page.evaluate(
            """() => {
                 const ids = logState.entries.map(e => e.id).filter(n => n != null);
                 return !ids.length || logState.oldestId === Math.min(...ids);
               }"""
        )
        checks["log: the paging cursor matches the oldest line displayed"] = cursor_ok
        await page.fill("#logSearch", "")
        await page.wait_for_timeout(500)
        await page.screenshot(path="/tmp/sweep-log.png", full_page=True)

        # Last, because it navigates away from the app. Same page deliberately: the
        # guide sits behind the session cookie, and a fresh context would not have it.
        resp = await page.goto(f"http://127.0.0.1:{PORT}/help/sharepoint")
        checks["help: the setup guide loads for a signed-in admin"] = (
            resp is not None and resp.status == 200
            and await page.locator("h1").count() == 1
        )
        guide_text = await page.inner_text("body")
        checks["help: the guide covers the steps people miss"] = (
            "Sites.Selected" in guide_text
            and "Grant admin consent" in guide_text
            and "Secret ID" in guide_text
        )
        # Reusing the app's stylesheet is meant to inherit its *tokens*. It also
        # inherits its component rules, and the app's h3 is a 12px uppercase muted
        # micro-label because in the app an h3 always labels a card. Unnoticed, that
        # flattens every step title on this page into a label and the hierarchy with
        # it — so assert a step heading still reads as a heading.
        heading = await page.evaluate(
            """() => {
                 const h = document.querySelector('.doc-main .step h3');
                 if (!h) return null;
                 const s = getComputedStyle(h);
                 return {transform: s.textTransform, size: parseFloat(s.fontSize),
                         color: s.color, muted: getComputedStyle(
                           document.querySelector('.pre-label')).color};
               }"""
        )
        checks["help: step headings read as headings, not as the app's card labels"] = (
            heading is not None and heading["transform"] == "none"
            and heading["size"] >= 15 and heading["color"] != heading["muted"]
        )
        checks["help: the guide picked up the app stylesheet"] = (
            # Unstyled, the body would be on the UA default with no padding column.
            await page.evaluate(
                "() => getComputedStyle(document.body).backgroundColor"
            ) not in ("rgba(0, 0, 0, 0)", "")
            and await page.locator(".doc-main .step").count() >= 7
        )
        await page.screenshot(path="/tmp/help-sharepoint.png", full_page=True)

        for name, ok in checks.items():
            print(f"  {'PASS' if ok else 'FAIL'}  {name}")
            if not ok:
                problems.append(f"check failed: {name}")
        await browser.close()

    print("\nJS problems:", problems or "none")
    print("screenshots: /tmp/ask-answer.png /tmp/ask-drawer.png /tmp/admin.png "
          "/tmp/admin-key.png /tmp/admin-access.png /tmp/corpus-upload.png "
          "/tmp/corpus-connect.png /tmp/corpus-sync.png /tmp/sweep-log.png "
          "/tmp/help-sharepoint.png")
    return 1 if problems else 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
