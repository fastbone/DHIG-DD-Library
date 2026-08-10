"""FastAPI application: REST + SSE, serving the single-page web UI."""

from __future__ import annotations

import asyncio
import json
import sqlite3
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from . import agent, db, extract, ingest, manifest, pricing, search
from .config import SUPPORTED_EXTS, WORKSTREAMS, settings
from .events import broker, sse

WEB_DIR = Path(__file__).resolve().parent.parent / "web"

JOBS: dict[str, object] = {}


@asynccontextmanager
async def lifespan(app: FastAPI):
    db.init()
    broker.bind_loop(asyncio.get_running_loop())
    broker.log("Server ready.")
    yield


app = FastAPI(title="DD Library", lifespan=lifespan)


@app.exception_handler(Exception)
async def unhandled(request: Request, exc: Exception):
    broker.log(f"{request.url.path}: {type(exc).__name__}: {exc}", level="error")
    return JSONResponse({"error": f"{type(exc).__name__}: {exc}"}, status_code=500)


# --- status / config -----------------------------------------------------


@app.get("/api/status")
async def status():
    running = {
        k: {"id": k, "kind": getattr(v, "id", k).split("-")[0],
            "done": getattr(v, "done", 0), "total": getattr(v, "total", 0),
            "failed": getattr(v, "failed", 0)}
        for k, v in JOBS.items()
    }
    m = manifest.build()
    return {
        "corpus_root": str(settings.corpus_root) if settings.corpus_root else None,
        "has_api_key": settings.has_api_key(),
        "models": {
            "analyst": settings.analyst_model,
            "carder": settings.carder_model,
            "verifier": settings.verifier_model,
            "effort": settings.analyst_effort,
        },
        "python_tool": settings.enable_python_tool,
        "ocr": settings.ocr_enabled,
        "supported_extensions": sorted(SUPPORTED_EXTS),
        "workstreams": WORKSTREAMS,
        "stats": search.stats(),
        "manifest": {k: m[k] for k in ("mode", "chars", "approx_tokens", "n_indexed", "n_unindexed")},
        "manifest_cost_per_turn_usd": round(
            m["approx_tokens"] * pricing.PRICES.get(settings.analyst_model, (5, 25))[0]
            * pricing.CACHE_READ_MULTIPLIER / 1_000_000,
            4,
        ),
        "jobs_running": running,
        "recent_jobs": db.recent_jobs(8),
        "lifetime_usage": pricing.lifetime.snapshot(),
    }


class RootBody(BaseModel):
    path: str


@app.post("/api/corpus-root")
async def set_root(body: RootBody):
    try:
        resolved = settings.set_corpus_root(body.path)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    broker.log(f"Corpus root set to {resolved}")
    return {"corpus_root": str(resolved)}


@app.get("/api/browse")
async def browse(path: str = Query("/")):
    """Minimal directory picker for the ingest form."""
    p = Path(path).expanduser()
    if not p.is_dir():
        p = p.parent if p.parent.is_dir() else Path.home()
    try:
        entries = sorted(
            (c for c in p.iterdir() if c.is_dir() and not c.name.startswith(".")),
            key=lambda c: c.name.lower(),
        )[:400]
    except PermissionError:
        entries = []
    counts: dict[str, int] = {}
    try:
        for c in p.iterdir():
            if c.is_file() and c.suffix.lower() in SUPPORTED_EXTS:
                counts[c.suffix.lower()] = counts.get(c.suffix.lower(), 0) + 1
    except PermissionError:
        pass
    return {
        "path": str(p.resolve()),
        "parent": str(p.resolve().parent),
        "dirs": [{"name": c.name, "path": str(c.resolve())} for c in entries],
        "supported_files_here": counts,
    }


# --- ingest / sweep ------------------------------------------------------


class IngestBody(BaseModel):
    path: str | None = None
    ocr: bool = False


@app.post("/api/ingest")
async def start_ingest(body: IngestBody):
    root = Path(body.path).expanduser().resolve() if body.path else settings.corpus_root
    if root is None:
        raise HTTPException(400, "no corpus root set")
    if not root.is_dir():
        raise HTTPException(400, f"not a directory: {root}")
    if any(k.startswith("ingest-") for k in JOBS):
        raise HTTPException(409, "an ingest job is already running")
    settings.set_corpus_root(str(root))
    job = ingest.IngestJob(root, ocr=body.ocr or settings.ocr_enabled)
    JOBS[job.id] = job

    async def runner():
        try:
            await job.run()
        finally:
            JOBS.pop(job.id, None)
            manifest.invalidate_manifest()

    asyncio.create_task(runner())
    return {"job_id": job.id, "root": str(root)}


class SweepBody(BaseModel):
    redo: bool = False


@app.post("/api/sweep")
async def start_sweep(body: SweepBody):
    if not settings.has_api_key():
        raise HTTPException(400, "no Anthropic credentials found (set ANTHROPIC_API_KEY)")
    if any(k.startswith("sweep-") for k in JOBS):
        raise HTTPException(409, "a sweep is already running")
    job = manifest.SweepJob(redo=body.redo)
    JOBS[job.id] = job

    async def runner():
        try:
            await job.run()
        finally:
            JOBS.pop(job.id, None)

    asyncio.create_task(runner())
    return {"job_id": job.id, "pending": job.total}


@app.post("/api/jobs/{job_id}/cancel")
async def cancel_job(job_id: str):
    job = JOBS.get(job_id)
    if job is None:
        raise HTTPException(404, "job not running")
    job.cancel.set()  # type: ignore[attr-defined]
    broker.log(f"Cancelling {job_id} …", level="warn")
    return {"cancelling": job_id}


@app.post("/api/dedupe")
async def rerun_dedupe():
    result = await asyncio.to_thread(ingest.mark_duplicates)
    manifest.invalidate_manifest()
    broker.publish("stats_dirty")
    return result


# --- events (SSE) --------------------------------------------------------


@app.get("/api/events")
async def events(request: Request):
    q = broker.subscribe()

    async def gen():
        try:
            for past in broker.replay():
                yield sse(past)
            yield sse({"kind": "hello"})
            while True:
                if await request.is_disconnected():
                    break
                try:
                    event = await asyncio.wait_for(q.get(), timeout=15)
                except asyncio.TimeoutError:
                    yield ": keepalive\n\n"
                    continue
                yield sse(event)
        finally:
            broker.unsubscribe(q)

    return StreamingResponse(
        gen(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no", "Connection": "keep-alive"},
    )


# --- corpus browsing ----------------------------------------------------


@app.get("/api/documents")
async def documents(
    query: str | None = None,
    workstream: str | None = None,
    doc_type: str | None = None,
    status: str | None = None,
    flagged: bool = False,
    duplicates: str = "all",
    limit: int = 60,
    offset: int = 0,
):
    return search.list_documents(
        query=query, workstream=workstream, doc_type=doc_type, status=status,
        flagged=flagged, duplicates=duplicates,
        limit=max(1, min(limit, 300)), offset=max(0, offset),
    )


@app.get("/api/documents/{doc_id}")
async def document(doc_id: str):
    from . import tools

    card = tools.document_card(doc_id)
    if card.get("error"):
        raise HTTPException(404, card["error"])
    return card


@app.get("/api/documents/{doc_id}/text")
async def document_text(doc_id: str, start: int = 0, chars: int = 20_000, anchor: str | None = None):
    row = db.one("SELECT n_chars FROM documents WHERE id=?", (doc_id,))
    if row is None:
        raise HTTPException(404, "unknown document")
    if anchor:
        unit = db.one(
            "SELECT char_start FROM units WHERE doc_id=? AND anchor=?", (doc_id, anchor)
        )
        if unit:
            start = max(0, unit["char_start"] - 200)
    chars = max(1000, min(chars, 200_000))
    text = extract.read_mirror(doc_id, start, start + chars)
    return {
        "doc_id": doc_id, "start": start, "end": start + len(text),
        "total": row["n_chars"] or 0, "text": text,
    }


@app.get("/api/documents/{doc_id}/original")
async def document_original(doc_id: str):
    row = db.one("SELECT abs_path, filename FROM documents WHERE id=?", (doc_id,))
    if row is None or not Path(row["abs_path"]).exists():
        raise HTTPException(404, "original not available")
    return FileResponse(row["abs_path"], filename=row["filename"])


@app.get("/api/search")
async def api_search(q: str, limit: int = 25, workstream: str | None = None):
    return {"query": q, "hits": search.search(q, limit=min(limit, 100), workstream=workstream)}


@app.get("/api/manifest")
async def api_manifest(full: bool = False):
    m = manifest.build()
    out = {k: m[k] for k in ("mode", "chars", "approx_tokens", "n_indexed", "n_unindexed", "rollup")}
    if full:
        out["text"] = m["text"]
    return out


# --- ask ----------------------------------------------------------------


class AskBody(BaseModel):
    question: str = Field(min_length=2)
    history: list[dict] = []
    verify: bool = True
    effort: str | None = None


@app.post("/api/ask")
async def ask(body: AskBody):
    if not settings.has_api_key():
        raise HTTPException(400, "no Anthropic credentials found (set ANTHROPIC_API_KEY)")

    async def gen():
        async for event in agent.ask(
            body.question, history=body.history, do_verify=body.verify, effort=body.effort
        ):
            yield sse(event)

    return StreamingResponse(
        gen(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@app.get("/api/qa-log")
async def qa_log(limit: int = 50):
    out = []
    for r in db.rows(
        "SELECT id, question, answer, citations, verdicts, usage, duration_s, created_at"
        " FROM qa_log ORDER BY created_at DESC LIMIT ?",
        (min(limit, 200),),
    ):
        d = dict(r)
        for k in ("citations", "verdicts", "usage"):
            try:
                d[k] = json.loads(d[k] or "[]")
            except (json.JSONDecodeError, TypeError):
                d[k] = []
        out.append(d)
    return {"entries": out}


# --- artifacts ----------------------------------------------------------


@app.get("/api/artifacts")
async def artifacts(limit: int = 50):
    return {
        "artifacts": [
            {**dict(r), "download_url": f"/api/artifacts/{r['id']}/download"}
            for r in db.rows(
                "SELECT * FROM artifacts ORDER BY created_at DESC LIMIT ?", (min(limit, 200),)
            )
        ]
    }


@app.get("/api/artifacts/{artifact_id}/download")
async def download(artifact_id: str):
    row = db.one("SELECT * FROM artifacts WHERE id=?", (artifact_id,))
    if row is None or not Path(row["path"]).exists():
        raise HTTPException(404, "artifact not found")
    return FileResponse(row["path"], filename=row["filename"])


# --- static -------------------------------------------------------------

if WEB_DIR.exists():
    app.mount("/", StaticFiles(directory=str(WEB_DIR), html=True), name="web")


def main() -> None:
    import uvicorn

    host = __import__("os").environ.get("DD_HOST", "127.0.0.1")
    port = int(__import__("os").environ.get("DD_PORT", "8000"))
    uvicorn.run("app.server:app", host=host, port=port, reload=False, log_level="info")


if __name__ == "__main__":
    main()
