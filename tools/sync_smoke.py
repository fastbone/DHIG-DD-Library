#!/usr/bin/env python3
"""Sync smoke test: connections, credentials at rest, and the sync job.

Runs against a throwaway data directory, a stub Microsoft Graph, and the fake
rclone in `tools/fake_rclone.py` — so it needs no tenant, no network and no
tokens, and it exercises the real subprocess and the real ingest.

    python3 tools/sync_smoke.py
"""

from __future__ import annotations

import asyncio
import json
import os
import secrets
import sys
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

_TMP = tempfile.mkdtemp(prefix="dd-syncsmoke-")
# Named "data", like the container's volume — the shape ingest has to cope with.
_DATA = str(Path(_TMP) / "data")
os.environ["DD_DATA_DIR"] = _DATA
os.environ["DD_SECRET_KEY"] = secrets.token_urlsafe(32)
os.environ.pop("ANTHROPIC_API_KEY", None)

# The sync job hands rclone a deliberately scrubbed environment — only PATH, HOME
# and the RCLONE_* it means to set — so there is no way to pass a test scenario
# through to the fake. Nor should there be: a test hook in that env would be a
# hole in production code. Instead the scenario lives in a file, and DD_RCLONE_BIN
# points at a shim that knows the path to it.
_SCENARIO = Path(_TMP) / "scenario.json"
_SHIM = Path(_TMP) / "rclone_shim.py"
_SHIM.write_text(
    "#!/usr/bin/env python3\n"
    "import json, os, runpy, sys\n"
    f"scenario = json.loads(open({str(_SCENARIO)!r}).read())\n"
    "os.environ.update({k: str(v) for k, v in scenario.items()})\n"
    f"sys.argv[0] = {str(ROOT / 'tools' / 'fake_rclone.py')!r}\n"
    f"runpy.run_path({str(ROOT / 'tools' / 'fake_rclone.py')!r}, run_name='__main__')\n"
)
_SHIM.chmod(0o755)
os.environ["DD_RCLONE_BIN"] = str(_SHIM)


def scenario(**kwargs) -> None:
    """Set what the next fake-rclone invocation should do."""
    current = json.loads(_SCENARIO.read_text()) if _SCENARIO.exists() else {}
    for key, value in kwargs.items():
        if value is None:
            current.pop(key, None)
        else:
            current[key] = value
    _SCENARIO.write_text(json.dumps(current))

PASSES: list[str] = []
FAILURES: list[str] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    (PASSES if ok else FAILURES).append(name)
    print(f"  {'PASS' if ok else 'FAIL'}  {name}{'' if ok else '  ← ' + str(detail)[:200]}")


# --- a stub Graph --------------------------------------------------------


class GraphStub(BaseHTTPRequestHandler):
    def log_message(self, *a):  # noqa: ANN002
        pass

    def _send(self, code: int, obj: dict) -> None:
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):  # noqa: N802
        n = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(n).decode()
        if "client_secret=right-secret" in body:
            self._send(200, {"access_token": "tok", "expires_in": 3599})
        else:
            self._send(401, {"error": "invalid_client",
                             "error_description": "AADSTS7000215: Invalid client secret provided."})

    def do_GET(self):  # noqa: N802
        p = self.path
        if p.endswith("/drives"):
            # Two libraries, so selecting one by name is a real choice.
            self._send(200, {"value": [
                {"id": "drv-1", "name": "Documents", "driveType": "documentLibrary"},
                {"id": "drv-dd", "name": "DD Room", "driveType": "documentLibrary"},
            ]})
        elif "/root" in p:
            self._send(200, {"id": "root", "name": "root", "size": 1024,
                             "folder": {"childCount": 4}})
        elif p.startswith("/sites/"):
            self._send(200, {"id": "site-1", "displayName": "Project X"})
        else:
            self._send(404, {"error": {"code": "itemNotFound", "message": p}})


def start_graph() -> str:
    srv = HTTPServer(("127.0.0.1", 0), GraphStub)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return f"http://127.0.0.1:{srv.server_port}"


SITE = "https://contoso.sharepoint.com/sites/ProjectX"


async def main() -> int:
    from app import db, graph, search, security, sync
    from app.events import broker

    base = start_graph()
    graph.LOGIN_HOST = base
    graph.GRAPH = base

    db.init()
    broker.bind_loop(asyncio.get_running_loop())

    print("— connections —")
    conn = sync.create(label="DD Room", site_url=SITE, tenant="tid", client_id="cid",
                       secret="right-secret", actor="alice")
    cid = conn["id"]
    check("a connection is created with a mirror directory under the sync root",
          Path(conn["mirror_dir"]).parent == Path(_DATA) / "sync", conn["mirror_dir"])
    check("the client secret is not in the public view",
          not any(k.startswith("secret_") and k not in {"secret_last4", "secret_expires_at"}
                  for k in conn), sorted(conn))
    check("only the last four characters are exposed", conn["secret_last4"] == "cret",
          conn["secret_last4"])
    check("the secret decrypts server-side", sync.client_secret(cid) == "right-secret")

    n, ct = security.encrypt("sk-ant-not-a-client-secret", security.AAD_API_KEY)
    try:
        security.decrypt(n, ct, security.AAD_CONNECTION)
        check("an API-key ciphertext cannot be reused as a client secret", False, "it decrypted")
    except Exception:  # noqa: BLE001
        check("an API-key ciphertext cannot be reused as a client secret", True)

    print("\n— testing a connection —")
    result = await sync.test(cid, actor="alice")
    check("a good connection tests ok", result["ok"], result["note"])
    check("the verdict is stored", sync.get(cid)["last_test_ok"] is True)
    check("the drive id is resolved and remembered", sync.get(cid)["drive_id"] == "drv-1")

    # An empty library means "the site's default one". It has to be clearable, or
    # a connection pointed at a named library can never be pointed back.
    sync.update(cid, library="DD Room")
    check("a named library is stored", sync.get(cid)["library"] == "DD Room")
    # The drive id is what actually gets mirrored, so pointing the connection at a
    # different library has to discard it — otherwise the next sync keeps mirroring
    # the old drive while the UI shows the new library.
    check("naming a different library discards the resolved drive",
          sync.get(cid)["drive_id"] is None, sync.get(cid)["drive_id"])
    await sync.test(cid)  # re-resolve, and against the named library this time
    check("re-testing resolves the named library's own drive",
          sync.get(cid)["drive_id"] == "drv-dd", sync.get(cid)["drive_id"])
    sync.update(cid, library="")
    check("an empty library clears back to the default", sync.get(cid)["library"] is None,
          sync.get(cid)["library"])
    check("clearing the library also discards the resolved drive",
          sync.get(cid)["drive_id"] is None, sync.get(cid)["drive_id"])
    await sync.test(cid)
    sync.update(cid, site_url="https://contoso.sharepoint.com/sites/Other")
    check("a new site URL discards the resolved drive too",
          sync.get(cid)["drive_id"] is None, sync.get(cid)["drive_id"])
    sync.update(cid, site_url=SITE)
    await sync.test(cid)
    # Only a *changed* site or library invalidates it. The edit form submits every
    # field on every save, so testing for presence would discard the drive on each
    # Save and make an unrelated rename cost a Graph round trip.
    sync.update(cid, label="DD Room", interval_minutes=0, mirror_deletions=True)
    check("an edit that does not touch site or library keeps the resolved drive",
          sync.get(cid)["drive_id"] == "drv-1", sync.get(cid)["drive_id"])
    sync.update(cid, label="DD Room 2026", site_url=SITE, library="",
                tenant="tid", client_id="cid")
    check("resubmitting the same site and library keeps the resolved drive",
          sync.get(cid)["drive_id"] == "drv-1", sync.get(cid)["drive_id"])
    for field in ("label", "site_url", "tenant", "client_id"):
        try:
            sync.update(cid, **{field: "   "})
            check(f"an empty {field} is refused", False, "accepted")
        except sync.SyncError:
            check(f"an empty {field} is refused", True)

    sync.update(cid, secret="wrong-secret")
    result = await sync.test(cid)
    check("a bad secret tests not-ok without raising", result["ok"] is False, result["note"])
    check("the failure explains itself", "invalid_client" in result["note"], result["note"])
    sync.update(cid, secret="right-secret")

    print("\n— the sync job —")
    env_dump = Path(_TMP) / "rclone-env.json"
    scenario(FAKE_RCLONE_ENV_DUMP=str(env_dump), FAKE_RCLONE_MODE="ok", FAKE_RCLONE_FILES=4,
             FAKE_RCLONE_COUNT=4, FAKE_RCLONE_DELETES=0,
             FAKE_RCLONE_WRITE_DIR=conn["mirror_dir"])

    events: list[dict] = []
    queue = broker.subscribe()

    async def collect() -> None:
        while True:
            events.append(await queue.get())

    collector = asyncio.create_task(collect())
    job = sync.SyncJob(cid, actor="alice")
    await job.run(then_ingest=True)
    await asyncio.sleep(0.2)
    collector.cancel()

    check("the job reports the files it transferred", job.done == 4, f"done={job.done}")
    check("unchanged files are counted as skipped, not transferred", job.skipped >= 1,
          f"skipped={job.skipped}")
    check("the connection is marked ok", sync.get(cid)["status"] == "ok", sync.get(cid)["error"])
    check("the last sync time is recorded", bool(sync.get(cid)["last_sync_at"]))

    jobs = [e for e in events if e.get("kind") == "job"]
    check("progress is published as sync jobs",
          any(e.get("job_kind") == "sync" for e in jobs), [e.get("job_kind") for e in jobs][:5])
    check("a terminal job event is published",
          any(e.get("job_kind") == "sync" and e.get("status") == "done" for e in jobs))
    check("the ingest that follows publishes its own progress",
          any(e.get("job_kind") == "ingest" for e in jobs),
          sorted({e.get("job_kind") for e in jobs}))

    stats = search.stats()
    check("the mirrored files are indexed", stats["documents"] == 4, stats)
    hits = search.search("revenue")
    check("mirrored content is searchable", len(hits) >= 1, str(hits)[:160])
    check("the corpus root points at the mirror",
          str(sync.settings.corpus_root) == conn["mirror_dir"])

    print("\n— the secret never reaches argv —")
    dumped = json.loads(env_dump.read_text())
    check("rclone gets the secret from the environment",
          dumped.get("RCLONE_ONEDRIVE_CLIENT_SECRET") == "right-secret")
    check("the secret is absent from the command line",
          "right-secret" not in dumped.get("_argv", ""), dumped.get("_argv"))
    check("client credentials mode is on",
          dumped.get("RCLONE_ONEDRIVE_CLIENT_CREDENTIALS") == "true")
    check("the drive type is documentLibrary, not onedrive",
          dumped.get("RCLONE_ONEDRIVE_DRIVE_TYPE") == "documentLibrary")
    check("no config file is consulted", dumped.get("RCLONE_CONFIG") == "/dev/null")
    check("only supported extensions are requested",
          "--include *.xlsx" in dumped.get("_argv", ""), dumped.get("_argv"))
    check("a deletion ceiling is passed when mirroring deletions",
          "--max-delete" in dumped.get("_argv", ""), dumped.get("_argv"))

    check("the row records the library's size, not this run's transfers",
          sync.get(cid)["n_files"] == 4 and sync.get(cid)["bytes_total"] > 0,
          f"n_files={sync.get(cid)['n_files']} bytes={sync.get(cid)['bytes_total']}")

    # The case that made this wrong: a second sync with nothing to move. The row
    # must still describe the library rather than reporting "0 files".
    scenario(FAKE_RCLONE_FILES=0, FAKE_RCLONE_WRITE_DIR=None)
    job = sync.SyncJob(cid)
    await job.run(then_ingest=False)
    row = sync.get(cid)
    check("an incremental sync that moves nothing keeps the file count",
          row["n_files"] == 4, f"n_files={row['n_files']}")
    check("and keeps the byte total", row["bytes_total"] > 0, f"bytes={row['bytes_total']}")

    print("\n— deletions drop documents from the index —")
    for name in ("finance/model.xlsx", "legal/spa.md"):
        (Path(conn["mirror_dir"]) / name).unlink()
    scenario(FAKE_RCLONE_DELETES=2, FAKE_RCLONE_FILES=0, FAKE_RCLONE_WRITE_DIR=None)
    job = sync.SyncJob(cid, actor="alice")
    await job.run(then_ingest=True)
    check("the job notices the remote deletions", job.deleted == 2, f"deleted={job.deleted}")
    check("documents whose files are gone are purged", search.stats()["documents"] == 2,
          search.stats())

    print("\n— failure and cancellation —")
    scenario(FAKE_RCLONE_MODE="fail", FAKE_RCLONE_FILES=1)
    job = sync.SyncJob(cid)
    await job.run(then_ingest=False)
    row = sync.get(cid)
    check("a failed sync marks the connection failed", row["status"] == "failed", row["status"])
    check("the failure reason is stored", bool(row["error"]) and "403" in row["error"],
          row["error"])

    scenario(FAKE_RCLONE_MODE="hang")
    job = sync.SyncJob(cid)
    runner = asyncio.create_task(job.run(then_ingest=False))
    await asyncio.sleep(0.8)
    job.cancel.set()
    try:
        await asyncio.wait_for(runner, timeout=20)
        check("a cancelled sync stops promptly", True)
    except asyncio.TimeoutError:
        check("a cancelled sync stops promptly", False, "still running after 20s")
    check("cancelling does not mark the connection failed",
          sync.get(cid)["status"] == "ok", sync.get(cid)["status"])

    # A JSON-only critical line must still reach the operator. rclone reports
    # unusable credentials that way and nowhere else, so swallowing it would turn
    # "invalid_client" into "exit code 1".
    scenario(FAKE_RCLONE_MODE="critical")
    job = sync.SyncJob(cid)
    await job.run(then_ingest=False)
    err = sync.get(cid)["error"] or ""
    check("a JSON-only critical line becomes the reported reason",
          "invalid_client" in err, err)
    check("the reason is not reduced to an exit code", "exit code" not in err, err)

    # Cancelling must work during the preflight too, not only the transfer. The
    # preflight runs before any progress is published, so a hang there is the
    # least visible way for a job to wedge — and it holds the one-sync-at-a-time
    # lock while it does.
    scenario(FAKE_RCLONE_MODE="hangsize")
    job = sync.SyncJob(cid)
    runner = asyncio.create_task(job.run(then_ingest=False))
    await asyncio.sleep(0.8)
    job.cancel.set()
    try:
        await asyncio.wait_for(runner, timeout=20)
        check("a sync cancelled during the preflight stops promptly", True)
    except asyncio.TimeoutError:
        check("a sync cancelled during the preflight stops promptly", False,
              "still running after 20s")

    scenario(FAKE_RCLONE_MODE="badauth")
    job = sync.SyncJob(cid)
    await job.run(then_ingest=False)
    check("a credential failure during preflight is reported, not crashed through",
          sync.get(cid)["status"] == "failed" and "AADSTS" in (sync.get(cid)["error"] or ""),
          sync.get(cid)["error"])

    print("\n— a stale 'syncing' status does not stop the schedule —")
    sync.update(cid, interval_minutes=60)
    db.execute("UPDATE sync_connections SET status='syncing', last_sync_at=NULL WHERE id=?",
               (cid,))
    check("a connection stuck mid-sync is not due", sync.due_connections() == [])
    cleared = sync.reset_interrupted()
    check("startup clears the leftover status", cleared == 1 and
          sync.get(cid)["status"] == "failed", sync.get(cid)["status"])
    check("and the schedule picks it up again", len(sync.due_connections()) == 1)
    check("the reason is recorded rather than silently reset",
          "restart" in (sync.get(cid)["error"] or ""), sync.get(cid)["error"])
    check("a second call finds nothing to clear", sync.reset_interrupted() == 0)

    # And that it is actually wired into startup, not merely available: run the
    # real lifespan and check the leftover status is gone when it comes up.
    from app import server as _server

    db.execute("UPDATE sync_connections SET status='syncing' WHERE id=?", (cid,))
    async with _server.lifespan(_server.app):
        pass
    check("the app clears it on startup, not just on request",
          sync.get(cid)["status"] != "syncing", sync.get(cid)["status"])
    sync.update(cid, interval_minutes=0)

    print("\n— size limits —")
    scenario(FAKE_RCLONE_MODE="ok", FAKE_RCLONE_SIZE=999 * 10**9)
    job = sync.SyncJob(cid)
    await job.run(then_ingest=False)
    err = sync.get(cid)["error"] or ""
    check("a library over DD_MAX_SYNC_GB is refused before any transfer",
          "over the" in err and "GB limit" in err, err)

    # The free-space check must credit what the mirror already holds. A library
    # that only just fitted the first time would otherwise be refused on every
    # resync — including a no-op one — because its own mirror is what filled the
    # disk. Simulated by claiming a library slightly larger than the free space
    # while the mirror already holds more than the difference.
    # The arithmetic matters. With a mirror holding H and F free afterwards, the
    # old check refused when remote > F; the new one refuses when remote - H > F.
    # So the two differ exactly for remote in (F, F + H] — hence a 32 MiB mirror
    # and a library 16 MiB larger than the space left, which is comfortably
    # inside that window in both directions.
    import shutil as _shutil

    mirror = Path(sync.get(cid)["mirror_dir"])
    mirror.mkdir(parents=True, exist_ok=True)
    (mirror / "already-here.bin").write_bytes(b"\0" * (32 << 20))
    free_after = _shutil.disk_usage(_DATA).free
    scenario(FAKE_RCLONE_SIZE=free_after + (16 << 20), FAKE_RCLONE_FILES=0)
    job = sync.SyncJob(cid)
    await job.run(then_ingest=False)
    row = sync.get(cid)
    check("a resync is not refused for space the mirror itself occupies",
          row["status"] == "ok", f"status={row['status']} error={row['error']}")

    # But a library genuinely larger than free space plus the mirror is refused.
    scenario(FAKE_RCLONE_SIZE=free_after + (256 << 20))
    job = sync.SyncJob(cid)
    await job.run(then_ingest=False)
    err = sync.get(cid)["error"] or ""
    check("a library that genuinely will not fit is still refused",
          "needs another" in err and "free" in err, err)
    (mirror / "already-here.bin").unlink()
    scenario(FAKE_RCLONE_SIZE=1 << 20, FAKE_RCLONE_FILES=1)

    print("\n— deleting a connection —")
    mirror = Path(sync.get(cid)["mirror_dir"])
    out = sync.delete(cid, drop_mirror=True, actor="alice")
    check("the mirror is removed on request", out["mirror_removed"] and not mirror.exists())
    check("the connection is gone", sync.get(cid) is None)
    check("no connections remain", sync.listing() == [])

    actions = {r["action"] for r in db.recent_audit(50)}
    check("every action is audited",
          {"sync.connect", "sync.test", "sync.run", "sync.delete"} <= actions, sorted(actions))

    print()
    print(f"{len(PASSES)} passed, {len(FAILURES)} failed")
    if FAILURES:
        print("failed:", ", ".join(FAILURES))
    print(f"(data dir {_DATA})")
    return 1 if FAILURES else 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
