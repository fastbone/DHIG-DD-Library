"""FastAPI application: REST + SSE, serving the single-page web UI.

Access control is enforced in one place — the middleware below — so a new route
is protected by default rather than by remembering to add a dependency. Admin
routes additionally declare `Depends(auth.require_admin)`.
"""

from __future__ import annotations

import asyncio
import json
import time
import traceback
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import Depends, FastAPI, File, Form, HTTPException, Query, Request, UploadFile
from fastapi.responses import (
    FileResponse,
    JSONResponse,
    PlainTextResponse,
    RedirectResponse,
    StreamingResponse,
)
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from . import (
    access,
    agent,
    auth,
    budget,
    credentials,
    db,
    extract,
    graph,
    ingest,
    manifest,
    pricing,
    search,
    security,
    storage,
    sync,
    uploads,
)
from .config import CONFIG_WARNINGS, SUPPORTED_EXTS, WORKSTREAMS, settings
from .events import broker, sse

WEB_DIR = Path(__file__).resolve().parent.parent / "web"

JOBS: dict[str, object] = {}

# Reachable without a session. Everything else needs one.
PUBLIC_PATHS = {
    "/login", "/login.js", "/styles.css", "/app.js", "/favicon.ico",
    "/api/session", "/api/login", "/api/bootstrap", "/api/health",
}
SAFE_METHODS = {"GET", "HEAD", "OPTIONS"}


SCHEDULER_TICK_S = 60


async def scheduler() -> None:
    """Start syncs for connections whose interval has elapsed.

    Deliberately dumb: one tick a minute, one sync at a time, and a connection
    that is already syncing or blocked by a running ingest is simply left for the
    next tick. A due sync that cannot start is not an error — it is a sync that
    happens a minute later.
    """
    while True:
        try:
            await asyncio.sleep(SCHEDULER_TICK_S)
            for conn in sync.due_connections():
                try:
                    start_sync_job(conn["id"], actor="schedule")
                except sync.SyncError:
                    break  # something is already running; try again next tick
                else:
                    broker.log(
                        f"Scheduled sync of {conn['label']} "
                        f"(every {conn['interval_minutes']} min)."
                    )
                    break  # one at a time
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 — a bad tick must not end the loop
            broker.log(f"Sync scheduler tick failed: {exc}", level="warn")


@asynccontextmanager
async def lifespan(app: FastAPI):
    db.init()
    broker.bind_loop(asyncio.get_running_loop())
    # Persist log lines as well as streaming them. Registered here rather than
    # imported inside events.py, which stays dependency-free — and registered
    # after db.init() so the first line has a table to land in.
    broker.sink = lambda ev: db.log_record(
        ev["level"], ev["message"], source=ev.get("source"),
        context=ev.get("context"), job_id=ev.get("job_id"),
    )
    # Paid calls land in the spend ledger, which is what the weekly limits are
    # measured against. Registered here for the same reason as the log sink:
    # pricing.py stays importable without pulling in the database.
    pricing.sink = lambda at, model, cost, usage: db.spend_record(
        at.username, at.budget, at.kind, model, cost, usage=usage, ref=at.ref,
    )
    auth.bootstrap()
    storage.housekeeping()
    sync.reset_interrupted()
    # A setting that could not be parsed has silently reverted to its default, and
    # the only symptom is a limit that does not match what .env says. Say so once,
    # here, where it lands in the persisted activity log.
    for warning in CONFIG_WARNINGS:
        broker.log(warning, level="warn")
    if security.secret_key_source() == "file":
        broker.log(
            "DD_SECRET_KEY is not set — using data/secret.key. Set it explicitly in production; "
            "losing it makes stored API keys undecryptable.",
            level="warn",
        )
    broker.log(
        f"Server ready · {auth.user_count()} account(s) · credentials: {credentials.source()}"
    )
    ticker = asyncio.create_task(scheduler())
    try:
        yield
    finally:
        ticker.cancel()


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
    broker.log(
        f"{request.url.path}: {type(exc).__name__}: {exc}",
        level="error",
        source="server",
        context={
            "method": request.method,
            "path": str(request.url.path),
            "query": str(request.url.query) or None,
            "exc_type": type(exc).__name__,
            "traceback": traceback.format_exc(limit=12),
        },
    )
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
    users = auth.list_users()
    spend = db.spend_by_user(budget.week_start())
    for u in users:
        u["budget"] = budget.status(u["username"])
        u["spend_this_week"] = spend.get(u["username"], {"ask": 0.0, "index": 0.0,
                                                        "total": 0.0})
    return {
        "users": users,
        "roles": list(auth.ROLES),
        "budget_defaults": {
            "ask": settings.weekly_budget_ask_usd,
            "index": settings.weekly_budget_index_usd,
            "grace_pct": settings.budget_grace_pct,
            "unlimited": budget.UNLIMITED,
        },
        "week_start": budget.week_start(),
        "resets_at": budget.week_end(),
    }


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
    # A number, "unlimited", or "default" to inherit the instance setting.
    # Strings on the wire so nobody has to remember a sentinel number, and so
    # a typed 0 unambiguously means "no spending" rather than the opposite.
    budget_ask: float | str | None = None
    budget_index: float | str | None = None


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
        fields = body.model_fields_set
        if "budget_ask" in fields or "budget_index" in fields:
            await asyncio.to_thread(
                budget.set_budgets, username,
                ask=body.budget_ask if "budget_ask" in fields else ...,
                index=body.budget_index if "budget_index" in fields else ...,
                actor=admin["username"],
            )
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    user["budget"] = budget.status(username.strip().lower())
    return {"user": user}


@app.delete("/api/users/{username}")
async def remove_user(username: str, admin: dict = Depends(auth.require_admin)):
    if username.strip().lower() == admin["username"]:
        raise HTTPException(400, "you cannot delete your own account")
    try:
        await asyncio.to_thread(auth.delete_user, username, actor=admin["username"])
        await asyncio.to_thread(budget.forget, username)
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
        # Counts only: the connection list itself is admin-only, but the Corpus
        # tab needs to know whether to mention connected libraries at all.
        "connected_libraries": db.scalar("SELECT COUNT(*) FROM sync_connections"),
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
        # The signed-in user's own position, so an analyst can see where they
        # stand before asking rather than only when refused.
        "budget": budget.status(user["username"]),
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
    # A sync chains its own ingest and switches the corpus root to its mirror, so
    # starting one by hand mid-sync would have two ingests writing the index and
    # fighting over which root is current. During rclone's phase only the sync job
    # is registered, so checking for a running ingest is not enough.
    if any(k.startswith("sync-") for k in JOBS):
        raise HTTPException(409, "a library sync is running — it will index its mirror itself")
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
    who = actor(request)
    try:
        await asyncio.to_thread(budget.require, who, "index")
    except budget.BudgetExceeded as exc:
        raise HTTPException(402, str(exc)) from exc
    job = manifest.SweepJob(redo=body.redo, actor=who)
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


# --- connected libraries (SharePoint) ------------------------------------
#
# Admin-only, like API keys and accounts: connecting a library makes its contents
# readable to everyone who can sign in, which is a decision about who sees the
# data room rather than a day-to-day ingest action.


class ConnectionBody(BaseModel):
    label: str
    site_url: str
    tenant: str
    client_id: str
    secret: str
    library: str | None = None
    only_supported_types: bool = True
    mirror_deletions: bool = True
    interval_minutes: int = 0


class ConnectionPatch(BaseModel):
    label: str | None = None
    site_url: str | None = None
    library: str | None = None
    tenant: str | None = None
    client_id: str | None = None
    secret: str | None = None
    only_supported_types: bool | None = None
    mirror_deletions: bool | None = None
    interval_minutes: int | None = None


@app.get("/api/sync/connections")
async def list_connections(_: dict = Depends(auth.require_admin)):
    return {
        "connections": sync.listing(),
        "sync_root": str(settings.sync_root),
        "max_sync_gb": settings.max_sync_gb,
        "rclone": sync.engine_version(),
    }


@app.post("/api/sync/connections")
async def add_connection(body: ConnectionBody, admin: dict = Depends(auth.require_admin)):
    try:
        conn = sync.create(
            label=body.label, site_url=body.site_url, tenant=body.tenant,
            client_id=body.client_id, secret=body.secret, library=body.library,
            only_supported_types=body.only_supported_types,
            mirror_deletions=body.mirror_deletions,
            interval_minutes=body.interval_minutes, actor=admin["username"],
        )
    except (sync.SyncError, graph.GraphError) as exc:
        raise HTTPException(400, str(exc)) from exc
    # Probe immediately: a connection that cannot reach its library is worth
    # finding out about now, not on the first sync.
    verdict = await sync.test(conn["id"], actor=admin["username"])
    return {"connection": verdict.get("connection") or conn, "test": {
        "ok": verdict["ok"], "note": verdict["note"]}}


@app.post("/api/sync/connections/{conn_id}")
async def edit_connection(
    conn_id: str, body: ConnectionPatch, admin: dict = Depends(auth.require_admin)
):
    try:
        conn = sync.update(
            conn_id, actor=admin["username"],
            **body.model_dump(exclude_none=True),
        )
    except (sync.SyncError, graph.GraphError) as exc:
        raise HTTPException(400, str(exc)) from exc
    return {"connection": conn}


@app.get("/api/sync/connections/{conn_id}/runs")
async def sync_run_list(conn_id: str, limit: int = 12, _: dict = Depends(auth.require_admin)):
    """Recent runs for one connection, newest first, without the per-file lists."""
    if sync.get(conn_id) is None:
        raise HTTPException(404, "unknown connection")
    running = next(
        (j.id for j in JOBS.values() if getattr(j, "conn_id", None) == conn_id), None
    )
    return {"runs": db.sync_runs(conn_id, limit=limit), "running_id": running}


@app.get("/api/sync/runs/{run_id}")
async def sync_run_detail(run_id: str, _: dict = Depends(auth.require_admin)):
    """One run in full: the counts, the paths it changed, and why it failed."""
    run = db.sync_run(run_id)
    if run is None:
        raise HTTPException(404, "unknown run")
    job = JOBS.get(run_id)
    # Only while the row itself still says running. The job stays registered through
    # the ingest chained onto the sync — minutes after _finish wrote a terminal
    # status — and overlaying then would relabel a finished run as live, replace its
    # final change list with the in-memory tail, and leave stale transfers in flight.
    if job is not None and run.get("status") == "running":
        run.update(job.live_snapshot())
    return {"run": run}


@app.post("/api/sync/connections/{conn_id}/test")
async def test_connection(conn_id: str, admin: dict = Depends(auth.require_admin)):
    try:
        return await sync.test(conn_id, actor=admin["username"])
    except sync.SyncError as exc:
        raise HTTPException(404, str(exc)) from exc


@app.post("/api/sync/connections/{conn_id}/sync")
async def start_sync(conn_id: str, admin: dict = Depends(auth.require_admin)):
    if sync.get(conn_id) is None:
        raise HTTPException(404, "no such connection")
    try:
        return {"job_id": start_sync_job(conn_id, actor=admin["username"])}
    except sync.SyncError as exc:
        raise HTTPException(409, str(exc)) from exc


def start_sync_job(conn_id: str, *, actor: str | None) -> str:
    """Start a sync, registered so it shows up and can be cancelled.

    One at a time: two syncs would fight over the mirror, and the ingest each
    chains would fight over the corpus root.
    """
    if any(k.startswith("sync-") for k in JOBS):
        raise sync.SyncError("a sync is already running")
    if any(k.startswith("ingest-") for k in JOBS):
        raise sync.SyncError("an ingest is already running — it would clash with the sync")
    job = sync.SyncJob(conn_id, actor=actor)
    JOBS[job.id] = job

    async def runner():
        try:
            await job.run()
        finally:
            JOBS.pop(job.id, None)
            manifest.invalidate_manifest()

    asyncio.create_task(runner())
    return job.id


@app.delete("/api/sync/connections/{conn_id}")
async def delete_connection(
    conn_id: str, drop_mirror: bool = False, admin: dict = Depends(auth.require_admin)
):
    try:
        return sync.delete(conn_id, drop_mirror=drop_mirror, actor=admin["username"])
    except sync.SyncError as exc:
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


# --- activity log -------------------------------------------------------
#
# The live SSE feed is a ring buffer in memory: the one failure worth reporting
# scrolls past, and a restart loses it. These routes read the persisted copy, so
# a sweep's errors can still be found — and exported — an hour and a restart
# later.


def _log_levels(levels: str | None) -> list[str]:
    """Parse `levels=error,warn`. Empty or unrecognised means "everything"."""
    if not levels:
        return []
    return [lv for lv in (s.strip().lower() for s in levels.split(",")) if lv in db.LOG_LEVELS]


@app.get("/api/logs")
async def logs(
    levels: str | None = None,
    source: str | None = None,
    q: str | None = None,
    before_id: int | None = None,
    after_id: int | None = None,
    limit: int = 200,
):
    return {
        "entries": db.log_query(
            levels=_log_levels(levels), source=source, query=q,
            before_id=before_id, after_id=after_id, limit=limit,
        ),
        "counts": db.log_counts(),
        "retention": settings.log_retention,
    }


@app.get("/api/logs/export")
async def logs_export(
    levels: str | None = None,
    source: str | None = None,
    q: str | None = None,
    limit: int = 2000,
):
    """The same log as a plain-text report, oldest first.

    This is the point of persisting the log: paste the output into a bug report
    and the reader gets the failing paths, the exception types and the tracebacks
    rather than a retyped fragment of one line.
    """
    wanted = _log_levels(levels)
    entries = db.log_query(levels=wanted, source=source, query=q, limit=limit)
    text = _format_log_report(entries, levels=wanted, source=source, query=q)
    stamp = time.strftime("%Y%m%d-%H%M%S", time.localtime())
    return PlainTextResponse(
        text,
        headers={"Content-Disposition": f'attachment; filename="dd-library-log-{stamp}.txt"'},
    )


def _format_log_report(
    entries: list[dict], *, levels: list[str], source: str | None, query: str | None
) -> str:
    counts = db.log_counts()
    filters = ", ".join(
        part for part in [
            f"levels={'+'.join(levels)}" if levels else "levels=all",
            f"source={source}" if source else None,
            f"search={query!r}" if query else None,
        ] if part
    )
    lines = [
        "DD Library activity log",
        f"exported   {time.strftime('%Y-%m-%d %H:%M:%S %Z', time.localtime())}",
        f"filter     {filters}",
        f"matched    {len(entries)} of {counts['total']} kept "
        f"({counts['error']} error, {counts['warn']} warn, "
        f"{counts['success']} success, {counts['info']} info)",
        "",
        "Oldest first. Indented blocks are the structured context behind a line:",
        "the path that failed, its size and extension, and the traceback.",
        "=" * 78,
        "",
    ]
    for e in reversed(entries):  # log_query returns newest first
        stamp = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(e["ts"]))
        head = f"[{stamp}] {e['level'].upper():<7} {e.get('source') or '-'}"
        if e.get("job_id"):
            head += f"  job={e['job_id']}"
        lines.append(head)
        lines.append(f"  {e['message']}")
        ctx = e.get("context") or {}
        for key, value in ctx.items():
            if key == "traceback":
                lines.append("    traceback:")
                lines.extend(f"      {tl}" for tl in str(value).rstrip().splitlines())
            elif isinstance(value, list):
                lines.append(f"    {key}: ({len(value)})")
                lines.extend(f"      - {item}" for item in value[:200])
                if len(value) > 200:
                    lines.append(f"      … {len(value) - 200} more")
            else:
                lines.append(f"    {key}: {value}")
        lines.append("")
    if not entries:
        lines.append("(nothing matched this filter)")
    return "\n".join(lines) + "\n"


class LogClearBody(BaseModel):
    levels: list[str] = []


@app.post("/api/logs/clear")
async def logs_clear(
    body: LogClearBody, request: Request, admin: dict = Depends(auth.require_admin)
):
    wanted = [lv for lv in body.levels if lv in db.LOG_LEVELS]
    removed = db.log_clear(wanted)
    db.audit("log.clear", actor=actor(request), detail=f"{removed} line(s) {wanted or 'all'}")
    return {"removed": removed, "counts": db.log_counts()}


# --- events (SSE) --------------------------------------------------------


@app.get("/api/events")
async def events(request: Request):
    q = broker.subscribe()

    async def gen():
        try:
            for past in broker.replay():
                # Job events are replayed so a page load repaints a running
                # progress bar. Log lines are not: they are read from the `logs`
                # table now, and replaying them would double every line the
                # browser had already fetched — including on every reconnect.
                if past.get("kind") == "log":
                    continue
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
async def api_search(
    q: str,
    limit: int = 25,
    workstream: str | None = None,
    doc_type: str | None = None,
    family: str | None = None,
):
    """Passage search over every extracted page, slide and sheet.

    The filters are the ones `search.search` already applies for the agent; they
    were simply never reachable over HTTP. Document count as well as hit count,
    because "38 passages in 9 documents" is the useful shape of that answer — one
    document matching forty times is a different finding from forty documents
    matching once.
    """
    hits = search.search(
        q, limit=min(limit, 100), workstream=workstream, doc_type=doc_type, family=family
    )
    return {
        "query": q,
        "hits": hits,
        "n_hits": len(hits),
        "n_documents": len({h["doc_id"] for h in hits if h.get("doc_id")}),
    }


@app.get("/api/corpus/folders")
async def api_folders():
    """The folder tree a question can be scoped to, with per-folder counts.

    Signed in, not admin: choosing what a question may see is part of asking one.
    """
    roots = [r["path"] for r in storage.known_roots()]
    return {"folders": search.folder_tree(roots), "roots": roots}


def validated_scope(scope: list[str]) -> list[str]:
    """Every prefix must sit inside a root the app knows about.

    A scope is only ever a filter over paths already in the database, so an
    unknown prefix cannot leak anything — it would simply match nothing. It is
    rejected anyway, because a silently empty corpus reads as an empty data room,
    and that is the one thing this feature exists to keep the reader from
    concluding by accident.
    """
    roots = [Path(r["path"]) for r in storage.known_roots()]
    out: list[str] = []
    for raw in scope:
        prefix = (raw or "").strip()
        if not prefix:
            continue
        # No resolve(): these are paths recorded at ingest, and a root that has
        # since been unmounted must still be selectable in the question log's terms.
        target = Path(prefix)
        if not any(target == r or security.is_within(target, r) for r in roots):
            raise HTTPException(
                400, f"folder is outside every known corpus root: {prefix}"
            )
        out.append(str(target))
    return out


@app.get("/api/manifest")
async def api_manifest(full: bool = False, scope: str | None = None):
    # `scope` is a JSON array so the estimate shown next to the folder picker is
    # the same figure the question will actually pay for.
    prefixes: list[str] = []
    if scope:
        try:
            parsed = json.loads(scope)
        except json.JSONDecodeError as exc:
            raise HTTPException(400, "scope must be a JSON array of paths") from exc
        if not isinstance(parsed, list):
            raise HTTPException(400, "scope must be a JSON array of paths")
        prefixes = validated_scope([str(p) for p in parsed])
    m = manifest.build(prefixes)
    out = {
        k: m[k]
        for k in ("mode", "chars", "approx_tokens", "n_indexed", "n_unindexed",
                  "n_indexed_total", "scope", "rollup")
    }
    # The standing per-turn cost of carrying this map, priced the same way the
    # Indexing tab prices the full one — so the saving from a scope is visible at
    # the moment of choosing it rather than a week later on the bill.
    out["cost_per_turn_usd"] = round(
        m["approx_tokens"] * pricing.PRICES.get(settings.analyst_model, (5, 25))[0]
        * pricing.CACHE_READ_MULTIPLIER / 1_000_000,
        4,
    )
    if full:
        out["text"] = m["text"]
    return out


# --- ask ----------------------------------------------------------------


class AskBody(BaseModel):
    question: str = Field(min_length=2)
    history: list[dict] = []
    verify: bool = True
    effort: str | None = None
    scope: list[str] = []


@app.post("/api/ask")
async def ask(body: AskBody, request: Request):
    # Validated before the stream opens: an SSE body cannot carry a 400, and a
    # rejected scope inside the stream would surface as a failed answer. Before
    # the credentials gate too — a request this app would refuse whatever its
    # configuration should be told which of the two problems is its own.
    scope = validated_scope(body.scope)
    if not credentials.available():
        raise HTTPException(400, "no API key configured — add one under Admin → API keys")
    db.audit(
        "ask", actor=actor(request),
        detail=body.question[:300] + (f" · scope: {', '.join(scope)}"[:200] if scope else ""),
    )

    who = actor(request)

    async def gen():
        async for event in agent.ask(
            body.question, history=body.history, do_verify=body.verify, effort=body.effort,
            actor=who, scope=scope,
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
        "SELECT q.id, q.question, q.answer, q.citations, q.verdicts, q.usage, q.duration_s,"
        " q.created_at, s.scope FROM qa_log q"
        " LEFT JOIN qa_scopes s ON s.qa_id = q.id"
        " ORDER BY q.created_at DESC LIMIT ?",
        (min(limit, 200),),
    ):
        d = dict(r)
        # A past "not in the data room" is uninterpretable without knowing what the
        # question was allowed to see, so the scope travels with the log entry.
        for k in ("citations", "verdicts", "usage", "scope"):
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


@app.get("/help/sharepoint")
async def help_sharepoint():
    """The app-registration walkthrough, linked from the connect form.

    Served by the app rather than linked out: this is read while setting up a
    server that may have no general internet egress at all, and the one thing an
    operator should not have to do at that moment is go looking for a document.
    """
    return FileResponse(WEB_DIR / "help-sharepoint.html")


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
