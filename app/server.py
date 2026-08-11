"""FastAPI application: REST + SSE, serving the single-page web UI.

Access control is enforced in one place — the middleware below — so a new route
is protected by default rather than by remembering to add a dependency. Admin
routes additionally declare `Depends(auth.require_admin)`.
"""

from __future__ import annotations

import asyncio
import json
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import Depends, FastAPI, File, Form, HTTPException, Query, Request, UploadFile
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from . import (
    access,
    agent,
    auth,
    credentials,
    db,
    extract,
    ingest,
    manifest,
    pricing,
    search,
    security,
    storage,
    uploads,
)
from .config import SUPPORTED_EXTS, WORKSTREAMS, settings
from .events import broker, sse

WEB_DIR = Path(__file__).resolve().parent.parent / "web"

JOBS: dict[str, object] = {}

# Reachable without a session. Everything else needs one.
PUBLIC_PATHS = {
    "/login", "/login.js", "/styles.css", "/app.js", "/favicon.ico",
    "/api/session", "/api/login", "/api/bootstrap", "/api/health",
}
SAFE_METHODS = {"GET", "HEAD", "OPTIONS"}


@asynccontextmanager
async def lifespan(app: FastAPI):
    db.init()
    broker.bind_loop(asyncio.get_running_loop())
    auth.bootstrap()
    storage.housekeeping()
    if security.secret_key_source() == "file":
        broker.log(
            "DD_SECRET_KEY is not set — using data/secret.key. Set it explicitly in production; "
            "losing it makes stored API keys undecryptable.",
            level="warn",
        )
    broker.log(
        f"Server ready · {auth.user_count()} account(s) · credentials: {credentials.source()}"
    )
    yield


app = FastAPI(title="DD Library", lifespan=lifespan, docs_url=None, redoc_url=None)


@app.middleware("http")
async def access_control(request: Request, call_next):
    path = request.url.path
    request.state.user = None

    if path not in PUBLIC_PATHS and not path.startswith("/api/bootstrap"):
        user = await asyncio.to_thread(auth.session_user, request)
        request.state.user = user
        if user is None:
            if path.startswith("/api/"):
                return JSONResponse({"error": "authentication required"}, status_code=401)
            return RedirectResponse("/login", status_code=302)
        # CSRF: a cookie alone must not be enough to change state.
        if request.method not in SAFE_METHODS:
            sent = request.headers.get("x-csrf-token", "")
            if not security.constant_time_equals(sent, user.get("csrf", "")):
                return JSONResponse({"error": "CSRF token missing or invalid"}, status_code=403)

    response = await call_next(request)
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    response.headers.setdefault("Referrer-Policy", "same-origin")
    response.headers.setdefault("X-Frame-Options", "DENY")
    return response


@app.exception_handler(Exception)
async def unhandled(request: Request, exc: Exception):
    broker.log(f"{request.url.path}: {type(exc).__name__}: {exc}", level="error")
    return JSONResponse({"error": f"{type(exc).__name__}: {exc}"}, status_code=500)


def actor(request: Request) -> str | None:
    user = getattr(request.state, "user", None)
    return user.get("username") if user else None


# --- session / accounts --------------------------------------------------


class LoginBody(BaseModel):
    username: str
    password: str


@app.get("/api/health")
async def health():
    return {"ok": True, "accounts": auth.user_count()}


@app.get("/api/session")
async def session_info(request: Request):
    """Public: tells the login page whether to offer first-run setup."""
    user = await asyncio.to_thread(auth.session_user, request)
    return {
        "authenticated": user is not None,
        "needs_bootstrap": auth.user_count() == 0,
        "user": {k: user[k] for k in ("username", "role", "csrf", "must_change_password")}
        if user else None,
    }


@app.post("/api/login")
async def login(body: LoginBody, request: Request):
    token, user = await asyncio.to_thread(auth.login, body.username, body.password, request)
    response = JSONResponse({"user": {k: user[k] for k in ("username", "role", "csrf")}})
    response.set_cookie(auth.COOKIE_NAME, token, **auth.cookie_kwargs(request))
    return response


@app.post("/api/logout")
async def logout(request: Request):
    auth.logout(request.cookies.get(auth.COOKIE_NAME), actor=actor(request))
    response = JSONResponse({"ok": True})
    response.delete_cookie(auth.COOKIE_NAME, path="/")
    return response


@app.post("/api/bootstrap")
async def bootstrap(body: LoginBody, request: Request):
    """Create the first administrator. Only available while no accounts exist."""
    if auth.user_count() > 0:
        raise HTTPException(409, "accounts already exist — sign in instead")
    try:
        user = await asyncio.to_thread(
            auth.create_user, body.username, body.password, "admin", created_by="first-run"
        )
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    broker.log(f"First administrator {user['username']!r} created.", level="success")
    token, logged_in = await asyncio.to_thread(auth.login, body.username, body.password, request)
    response = JSONResponse({"user": {k: logged_in[k] for k in ("username", "role", "csrf")}})
    response.set_cookie(auth.COOKIE_NAME, token, **auth.cookie_kwargs(request))
    return response


class PasswordBody(BaseModel):
    current_password: str | None = None
    new_password: str = Field(min_length=8)


@app.post("/api/me/password")
async def change_own_password(body: PasswordBody, request: Request):
    user = auth.require_user(request)
    stored = db.one("SELECT password_hash FROM users WHERE id=?", (user["id"],))
    if not security.verify_password(body.current_password or "", stored["password_hash"]):
        raise HTTPException(403, "current password is incorrect")
    await asyncio.to_thread(
        auth.set_password, user["username"], body.new_password, actor=user["username"]
    )
    response = JSONResponse({"ok": True, "note": "signed out of all sessions"})
    response.delete_cookie(auth.COOKIE_NAME, path="/")
    return response


class UserBody(BaseModel):
    username: str
    password: str = Field(min_length=8)
    role: str = "analyst"


@app.get("/api/users")
async def list_users(_: dict = Depends(auth.require_admin)):
    return {"users": auth.list_users(), "roles": list(auth.ROLES)}


@app.post("/api/users")
async def create_user(body: UserBody, request: Request, admin: dict = Depends(auth.require_admin)):
    try:
        user = await asyncio.to_thread(
            auth.create_user, body.username, body.password, body.role,
            created_by=admin["username"], must_change_password=True,
        )
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    return {"user": user}


class UserPatch(BaseModel):
    role: str | None = None
    disabled: bool | None = None
    password: str | None = None


@app.post("/api/users/{username}")
async def patch_user(username: str, body: UserPatch, admin: dict = Depends(auth.require_admin)):
    try:
        if body.password:
            await asyncio.to_thread(
                auth.set_password, username, body.password, actor=admin["username"]
            )
        user = await asyncio.to_thread(
            auth.update_user, username, role=body.role, disabled=body.disabled,
            actor=admin["username"],
        )
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    return {"user": user}


@app.delete("/api/users/{username}")
async def remove_user(username: str, admin: dict = Depends(auth.require_admin)):
    if username.strip().lower() == admin["username"]:
        raise HTTPException(400, "you cannot delete your own account")
    try:
        await asyncio.to_thread(auth.delete_user, username, actor=admin["username"])
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    return {"deleted": username}


# --- API keys ------------------------------------------------------------


class KeyBody(BaseModel):
    label: str = "default"
    key: str
    activate: bool = True


@app.get("/api/keys")
async def list_keys(_: dict = Depends(auth.require_admin)):
    return {
        "keys": credentials.list_keys(),
        "source": credentials.source(),
        "env_key_present": settings.has_api_key(),
        "secret_key_source": security.secret_key_source(),
    }


@app.post("/api/keys")
async def add_key(body: KeyBody, admin: dict = Depends(auth.require_admin)):
    try:
        key = credentials.add_key(
            body.label, body.key, actor=admin["username"], activate=body.activate
        )
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    return {"key": key, "source": credentials.source()}


@app.post("/api/keys/{key_id}/activate")
async def activate_key(key_id: str, admin: dict = Depends(auth.require_admin)):
    try:
        return {"key": credentials.set_active(key_id, actor=admin["username"])}
    except ValueError as exc:
        raise HTTPException(404, str(exc)) from exc


@app.post("/api/keys/{key_id}/test")
async def test_key(key_id: str, admin: dict = Depends(auth.require_admin)):
    try:
        return await credentials.test_key(key_id, actor=admin["username"])
    except ValueError as exc:
        raise HTTPException(404, str(exc)) from exc


@app.post("/api/keys/test-active")
async def test_active_key(admin: dict = Depends(auth.require_admin)):
    if not credentials.available():
        raise HTTPException(400, "no credentials configured")
    return await credentials.test_key(None, actor=admin["username"])


@app.delete("/api/keys/{key_id}")
async def delete_key(key_id: str, admin: dict = Depends(auth.require_admin)):
    try:
        credentials.delete_key(key_id, actor=admin["username"])
    except ValueError as exc:
        raise HTTPException(404, str(exc)) from exc
    return {"deleted": key_id, "source": credentials.source()}


# --- status / config -----------------------------------------------------


@app.get("/api/status")
async def status(request: Request):
    user = auth.require_user(request)
    m = manifest.build()
    return {
        "user": {k: user[k] for k in ("username", "role", "csrf", "must_change_password")},
        "corpus_root": str(settings.corpus_root) if settings.corpus_root else None,
        "credentials": credentials.source(),
        "has_api_key": credentials.available(),
        "models": {
            "analyst": settings.analyst_model,
            "carder": settings.carder_model,
            "verifier": settings.verifier_model,
            "effort": settings.analyst_effort,
        },
        "python_tool": settings.enable_python_tool,
        "ocr": settings.ocr_enabled,
        "supported_extensions": sorted(SUPPORTED_EXTS),
        "archive_extensions": list(uploads.ARCHIVE_SUFFIXES),
        "max_upload_mb": settings.max_upload_mb,
        "workstreams": WORKSTREAMS,
        "stats": search.stats(),
        "manifest": {k: m[k] for k in ("mode", "chars", "approx_tokens", "n_indexed", "n_unindexed")},
        "manifest_cost_per_turn_usd": round(
            m["approx_tokens"] * pricing.PRICES.get(settings.analyst_model, (5, 25))[0]
            * pricing.CACHE_READ_MULTIPLIER / 1_000_000,
            4,
        ),
        "jobs_running": {
            k: {"id": k, "kind": k.split("-")[0], "done": getattr(v, "done", 0),
                "total": getattr(v, "total", 0), "failed": getattr(v, "failed", 0)}
            for k, v in JOBS.items()
        },
        "recent_jobs": db.recent_jobs(8),
        "lifetime_usage": pricing.lifetime.snapshot(),
    }


class RootBody(BaseModel):
    path: str


def permitted_dir(raw: str) -> Path:
    """Resolve a caller-supplied directory, refusing anything outside the roots.

    Every route that takes a filesystem path from the browser goes through here.
    Without it, a signed-in user could point the indexer at any directory the
    service account can read — /etc, another tenant's data room, the host's home
    — and then read the extracted text and download the originals through the
    ordinary document routes. The roots are operator-configured
    (``DD_BROWSE_ROOTS``), which is the same fence the folder picker honours.
    """
    roots = settings.browse_roots
    if not roots:
        raise HTTPException(400, "no browsable roots configured (set DD_BROWSE_ROOTS)")
    target = Path(raw).expanduser()
    try:
        resolved = target.resolve()
    except OSError as exc:
        raise HTTPException(400, f"cannot resolve path: {raw}") from exc
    if not any(resolved == r or security.is_within(resolved, r) for r in roots):
        raise HTTPException(
            403,
            "path is outside the permitted roots: " + ", ".join(str(r) for r in roots),
        )
    if not resolved.is_dir():
        raise HTTPException(400, f"not a directory: {resolved}")
    return resolved


@app.post("/api/corpus-root")
async def set_root(body: RootBody, request: Request):
    try:
        resolved = settings.set_corpus_root(str(permitted_dir(body.path)))
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    db.audit("corpus.set_root", actor=actor(request), detail=str(resolved))
    broker.log(f"Corpus root set to {resolved}")
    return {"corpus_root": str(resolved)}


@app.get("/api/browse")
async def browse(path: str = Query(""), _: dict = Depends(auth.require_admin)):
    """Folder picker, restricted to the configured browse roots."""
    roots = settings.browse_roots
    if not roots:
        raise HTTPException(400, "no browsable roots configured (set DD_BROWSE_ROOTS)")
    if not path:
        return {
            "path": "", "parent": "", "roots": [str(r) for r in roots],
            "dirs": [{"name": str(r), "path": str(r)} for r in roots],
            "supported_files_here": {},
        }
    target = Path(path).expanduser()
    if not any(security.is_within(target, r) or target.resolve() == r for r in roots):
        raise HTTPException(403, "path is outside the permitted roots")
    if not target.is_dir():
        raise HTTPException(400, "not a directory")

    try:
        entries = sorted(
            (c for c in target.iterdir() if c.is_dir() and not c.name.startswith(".")),
            key=lambda c: c.name.lower(),
        )[:400]
    except PermissionError:
        entries = []
    counts: dict[str, int] = {}
    try:
        for c in target.iterdir():
            if c.is_file() and c.suffix.lower() in SUPPORTED_EXTS:
                counts[c.suffix.lower()] = counts.get(c.suffix.lower(), 0) + 1
    except PermissionError:
        pass
    resolved = target.resolve()
    parent = str(resolved.parent) if any(
        security.is_within(resolved.parent, r) or resolved.parent == r for r in roots
    ) else ""
    return {
        "path": str(resolved),
        "parent": parent,
        "roots": [str(r) for r in roots],
        "dirs": [{"name": c.name, "path": str(c.resolve())} for c in entries],
        "supported_files_here": counts,
    }


# --- corpus access -------------------------------------------------------


class AccessBody(BaseModel):
    path: str | None = None


def _access_targets(raw: str | None) -> list[str] | None:
    """Resolve the path to inspect, or None for "every configured root".

    A path outside the browse roots is not walked — that fence holds here as
    everywhere else — but it is not a 403 either: pointing the ingester at an
    unfenced directory is one of the two ways a corpus silently comes up empty,
    and refusing to name the problem would defeat the purpose of the check.
    """
    if not raw or not raw.strip():
        return None
    target = Path(raw).expanduser()
    try:
        resolved = target.resolve()
    except OSError as exc:
        raise HTTPException(400, f"cannot resolve path: {raw}") from exc
    roots = settings.browse_roots
    if not any(resolved == r or security.is_within(resolved, r) for r in roots):
        raise HTTPException(
            403,
            f"{resolved} is outside the permitted roots ("
            + ", ".join(str(r) for r in roots)
            + "). Add it to DD_BROWSE_ROOTS, or ingest from a folder inside one of them.",
        )
    return [str(resolved)]


@app.get("/api/access-check")
async def access_check(path: str = Query(""), _: dict = Depends(auth.require_admin)):
    """Report every corpus path this process cannot read."""
    return await asyncio.to_thread(access.check, _access_targets(path))


@app.post("/api/access-repair")
async def access_repair(body: AccessBody, admin: dict = Depends(auth.require_admin)):
    """Chmod what the app owns into readability; report the rest as host commands."""
    result = await asyncio.to_thread(access.repair, _access_targets(body.path))
    db.audit(
        "corpus.access_repair",
        actor=admin["username"],
        detail=f"repaired={len(result['repaired'])} remaining={result['blocked']}",
    )
    if result["repaired"]:
        broker.log(f"Repaired permissions on {len(result['repaired'])} path(s).", level="success")
    if result["blocked"]:
        broker.log(
            f"{result['blocked']} path(s) still unreadable — they need a fix on the Docker host.",
            level="warn",
        )
    return result


# --- ingest / sweep ------------------------------------------------------


class IngestBody(BaseModel):
    path: str | None = None
    ocr: bool = False


@app.post("/api/ingest")
async def start_ingest(body: IngestBody, request: Request):
    if body.path:
        root = permitted_dir(body.path)
    else:
        root = settings.corpus_root
        if root is None:
            raise HTTPException(400, "no corpus root set")
        if not root.is_dir():
            raise HTTPException(400, f"not a directory: {root}")
    if any(k.startswith("ingest-") for k in JOBS):
        raise HTTPException(409, "an ingest job is already running")
    settings.set_corpus_root(str(root))
    db.audit("ingest.start", actor=actor(request), detail=str(root))
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
async def start_sweep(body: SweepBody, request: Request):
    if not credentials.available():
        raise HTTPException(400, "no API key configured — add one under Admin → API keys")
    if any(k.startswith("sweep-") for k in JOBS):
        raise HTTPException(409, "a sweep is already running")
    job = manifest.SweepJob(redo=body.redo)
    JOBS[job.id] = job
    db.audit("sweep.start", actor=actor(request), detail=f"redo={body.redo}")

    async def runner():
        try:
            await job.run()
        finally:
            JOBS.pop(job.id, None)

    asyncio.create_task(runner())
    return {"job_id": job.id, "pending": job.total}


@app.post("/api/jobs/{job_id}/cancel")
async def cancel_job(job_id: str, request: Request):
    job = JOBS.get(job_id)
    if job is None:
        raise HTTPException(404, "job not running")
    job.cancel.set()  # type: ignore[attr-defined]
    db.audit("job.cancel", actor=actor(request), detail=job_id)
    broker.log(f"Cancelling {job_id} …", level="warn")
    return {"cancelling": job_id}


@app.post("/api/dedupe")
async def rerun_dedupe(request: Request):
    result = await asyncio.to_thread(ingest.mark_duplicates)
    manifest.invalidate_manifest()
    db.audit("corpus.dedupe", actor=actor(request), detail=json.dumps(result))
    broker.publish("stats_dirty")
    return result


# --- uploads -------------------------------------------------------------


@app.get("/api/archives")
async def list_archives():
    return {
        "archives": uploads.list_archives(),
        "accepted": list(uploads.ARCHIVE_SUFFIXES),
        "max_upload_mb": settings.max_upload_mb,
        "extract_root": str(settings.extract_root),
    }


@app.post("/api/archives")
async def upload_archive(
    request: Request,
    file: UploadFile = File(...),
    auto_extract: bool = Form(True),
    auto_ingest: bool = Form(False),
):
    try:
        arc = await uploads.save_upload(file, actor=actor(request))
    except uploads.UnsafeArchive as exc:
        raise HTTPException(400, str(exc)) from exc

    job_id = None
    if auto_extract:
        job_id = _start_extract(arc["id"], then_ingest=auto_ingest, actor=actor(request))
    return {"archive": arc, "job_id": job_id}


def _start_extract(archive_id: str, *, then_ingest: bool, actor: str | None) -> str:
    if any(k.startswith("extract-") for k in JOBS):
        raise HTTPException(409, "another extraction is already running")
    job = uploads.ExtractJob(archive_id, actor=actor)
    JOBS[job.id] = job

    async def runner():
        try:
            await job.run(then_ingest=then_ingest)
        finally:
            JOBS.pop(job.id, None)

    asyncio.create_task(runner())
    return job.id


class ExtractBody(BaseModel):
    auto_ingest: bool = False


@app.post("/api/archives/{archive_id}/extract")
async def extract_archive(archive_id: str, body: ExtractBody, request: Request):
    if uploads.get_archive(archive_id) is None:
        raise HTTPException(404, "no such archive")
    return {"job_id": _start_extract(archive_id, then_ingest=body.auto_ingest,
                                     actor=actor(request))}


@app.delete("/api/archives/{archive_id}")
async def delete_archive(archive_id: str, request: Request, drop_extracted: bool = False):
    try:
        return uploads.delete_archive(
            archive_id, drop_extracted=drop_extracted, actor=actor(request)
        )
    except ValueError as exc:
        raise HTTPException(404, str(exc)) from exc


# --- storage -------------------------------------------------------------


@app.get("/api/storage")
async def storage_usage(_: dict = Depends(auth.require_admin)):
    return await asyncio.to_thread(storage.usage)


@app.post("/api/storage/{operation}")
async def storage_operation(operation: str, admin: dict = Depends(auth.require_admin)):
    op = storage.OPERATIONS.get(operation)
    if op is None:
        raise HTTPException(404, f"unknown operation (have: {', '.join(storage.OPERATIONS)})")
    return await asyncio.to_thread(op, actor=admin["username"])


class PathBody(BaseModel):
    path: str


@app.post("/api/storage/extracted/delete")
async def delete_extracted(body: PathBody, admin: dict = Depends(auth.require_admin)):
    try:
        return await asyncio.to_thread(
            storage.delete_extracted, body.path, actor=admin["username"]
        )
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc


@app.get("/api/audit")
async def audit_log(limit: int = 200, _: dict = Depends(auth.require_admin)):
    return {"entries": db.recent_audit(min(limit, 1000))}


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
async def document_original(doc_id: str, request: Request):
    row = db.one("SELECT abs_path, filename FROM documents WHERE id=?", (doc_id,))
    if row is None or not Path(row["abs_path"]).exists():
        raise HTTPException(404, "original not available")
    db.audit("document.download", actor=actor(request), detail=row["filename"])
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
async def ask(body: AskBody, request: Request):
    if not credentials.available():
        raise HTTPException(400, "no API key configured — add one under Admin → API keys")
    db.audit("ask", actor=actor(request), detail=body.question[:300])

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


# --- pages --------------------------------------------------------------


@app.get("/login")
async def login_page():
    return FileResponse(WEB_DIR / "login.html")


@app.get("/")
async def index():
    return FileResponse(WEB_DIR / "index.html")


if WEB_DIR.exists():
    app.mount("/", StaticFiles(directory=str(WEB_DIR)), name="web")


def main() -> None:
    import os

    import uvicorn

    uvicorn.run(
        "app.server:app",
        host=os.environ.get("DD_HOST", "127.0.0.1"),
        port=int(os.environ.get("DD_PORT", "8000")),
        reload=False,
        log_level=os.environ.get("DD_LOG_LEVEL", "info"),
        proxy_headers=True,
        forwarded_allow_ips=os.environ.get("DD_FORWARDED_ALLOW_IPS", "127.0.0.1"),
    )


if __name__ == "__main__":
    main()
