"""Remote corpus sources: connected SharePoint libraries, mirrored to disk.

The division of labour is deliberate. rclone moves the bytes — it already knows
about Graph throttling, retries, resumable transfers and chunked downloads, and
none of that is worth reimplementing. This module owns the parts that are ours:
where a mirror lives, what a connection is, how its secret is stored, what the
guard rails are, and how progress reaches the browser.

A mirror is an ordinary directory, so once it is filled the existing pipeline
takes over unchanged: `IngestJob` walks it, documents are content-addressed, and
nothing downstream knows the files came from SharePoint.

Credentials are app-only (client credentials). rclone reads them from its
*environment*, never from a config file and never from argv — argv is readable by
any process on the host via `ps`.
"""

from __future__ import annotations

import asyncio
import json
import re
import shutil
import subprocess
import time
import uuid
from pathlib import Path

from . import db, graph, security
from .config import SUPPORTED_EXTS, settings
from .events import broker

# rclone reports "Config file not found - using defaults" on stderr when there is
# none. There is none by design, and it is not worth alarming anyone with it.
_BENIGN_LOG = re.compile(r"config file|Using \S+ config", re.IGNORECASE)
_UNSAFE_LABEL = re.compile(r"[^A-Za-z0-9._-]+")


class SyncError(RuntimeError):
    """A sync that cannot proceed, with a message worth showing a person."""


def safe_label(label: str) -> str:
    """A directory-safe form of a connection label."""
    cleaned = _UNSAFE_LABEL.sub("-", (label or "").strip()).strip("-.")
    return cleaned[:48] or "library"


def _dir_size(path: Path) -> int:
    """Bytes already held on disk. Missing or unreadable counts as nothing."""
    total = 0
    for p in path.rglob("*"):
        try:
            if p.is_file():
                total += p.stat().st_size
        except OSError:
            continue
    return total


def reset_interrupted() -> int:
    """Clear connections left mid-sync by a crash or a redeploy.

    `status='syncing'` is set when a job starts and cleared when it ends, so a
    process that dies in between leaves it set forever — and `due_connections`
    skips that status, which quietly stops the schedule for that connection until
    someone syncs it by hand. Called once at startup, where a `syncing` row can
    only be a leftover: nothing is running yet.
    """
    stale = db.rows("SELECT id, label FROM sync_connections WHERE status='syncing'")
    if not stale:
        return 0
    db.execute(
        "UPDATE sync_connections SET status='failed',"
        " error='interrupted — the app restarted mid-sync' WHERE status='syncing'"
    )
    for row in stale:
        broker.log(
            f"{row['label']} was left mid-sync by a restart; it will sync again on schedule.",
            level="warn",
        )
    return len(stale)


def engine_version() -> dict:
    """Whether the sync engine is actually installed, and which version.

    Worth surfacing: a missing rclone is a deployment problem whose only other
    symptom is that every sync fails at the first subprocess call.
    """
    path = shutil.which(settings.rclone_bin) or (
        settings.rclone_bin if Path(settings.rclone_bin).is_file() else None
    )
    if not path:
        return {"available": False, "version": None, "bin": settings.rclone_bin}
    try:
        out = subprocess.run(  # noqa: S603 — a fixed binary, no shell, no user input
            [path, "version"], capture_output=True, text=True, timeout=10, check=False
        )
        first = (out.stdout or out.stderr or "").strip().splitlines()
        version = first[0].strip() if first else None
    except (OSError, subprocess.SubprocessError) as exc:
        return {"available": False, "version": None, "bin": path, "error": str(exc)[:200]}
    return {"available": True, "version": version, "bin": path}


# --- connections ---------------------------------------------------------


def _public(row) -> dict | None:
    """Strip the secret. The browser sees enough to recognise a connection."""
    if row is None:
        return None
    d = dict(row)
    d.pop("secret_nonce", None)
    d.pop("secret_ciphertext", None)
    for flag in ("only_supported_types", "mirror_deletions"):
        d[flag] = bool(d[flag])
    d["last_test_ok"] = None if d["last_test_ok"] is None else bool(d["last_test_ok"])
    return d


def get(conn_id: str) -> dict | None:
    return _public(db.one("SELECT * FROM sync_connections WHERE id=?", (conn_id,)))


def listing() -> list[dict]:
    return [
        _public(r)
        for r in db.rows("SELECT * FROM sync_connections ORDER BY label COLLATE NOCASE")
    ]


def client_secret(conn_id: str) -> str:
    """Decrypt one connection's client secret. Never leaves the server."""
    row = db.one(
        "SELECT secret_nonce, secret_ciphertext FROM sync_connections WHERE id=?", (conn_id,)
    )
    if row is None:
        raise SyncError("no such connection")
    try:
        return security.decrypt(
            row["secret_nonce"], row["secret_ciphertext"], security.AAD_CONNECTION
        )
    except Exception as exc:  # noqa: BLE001 — cryptography raises InvalidTag
        raise SyncError(
            "the stored client secret cannot be decrypted — DD_SECRET_KEY has "
            "probably changed. Edit the connection and enter the secret again."
        ) from exc


def create(
    *,
    label: str,
    site_url: str,
    tenant: str,
    client_id: str,
    secret: str,
    library: str | None = None,
    only_supported_types: bool = True,
    mirror_deletions: bool = True,
    interval_minutes: int = 0,
    drive_id: str | None = None,
    drive_name: str | None = None,
    actor: str | None = None,
) -> dict:
    label = (label or "").strip()
    if not label:
        raise SyncError("a label is required")
    for name, value in (("site URL", site_url), ("tenant", tenant),
                        ("client id", client_id), ("client secret", secret)):
        if not (value or "").strip():
            raise SyncError(f"{name} is required")
    graph.split_site_url(site_url)  # reject a malformed URL before storing anything

    conn_id = uuid.uuid4().hex[:12]
    mirror = settings.sync_root / f"{conn_id}_{safe_label(label)}"
    nonce, ciphertext = security.encrypt(secret.strip(), security.AAD_CONNECTION)
    db.execute(
        "INSERT INTO sync_connections(id, kind, label, site_url, library, tenant, client_id,"
        " secret_nonce, secret_ciphertext, secret_last4, drive_id, drive_name, mirror_dir,"
        " only_supported_types, mirror_deletions, interval_minutes, status, created_at,"
        " created_by) VALUES(?,'sharepoint',?,?,?,?,?,?,?,?,?,?,?,?,?,?,'new',?,?)",
        (conn_id, label, site_url.strip(), (library or "").strip() or None, tenant.strip(),
         client_id.strip(), nonce, ciphertext, secret.strip()[-4:], drive_id, drive_name,
         str(mirror), int(only_supported_types), int(mirror_deletions),
         max(0, int(interval_minutes)), time.time(), actor),
    )
    db.audit("sync.connect", actor=actor, detail=f"{label} ({site_url})")
    broker.log(f"Connected SharePoint library {label!r}.", level="success")
    broker.publish("sync_dirty")
    return get(conn_id)


def update(conn_id: str, *, actor: str | None = None, **fields) -> dict:
    before = get(conn_id)
    if before is None:
        raise SyncError("no such connection")
    # `library` is the one text column that may be cleared — an empty value means
    # "use the site's default library". For the rest an empty value is a mistake,
    # and nulling them would either break the row (label is NOT NULL) or leave a
    # connection that cannot authenticate.
    required = {"label", "site_url", "tenant", "client_id"}
    sets: list[str] = []
    params: list = []
    applied: dict[str, object] = {}
    for column in ("label", "site_url", "library", "tenant", "client_id",
                   "only_supported_types", "mirror_deletions", "interval_minutes"):
        if column not in fields or fields[column] is None:
            continue
        value = fields[column]
        if column in {"only_supported_types", "mirror_deletions"}:
            value = int(bool(value))
        elif column == "interval_minutes":
            value = max(0, int(value))
        elif isinstance(value, str):
            value = value.strip()
            if not value:
                if column in required:
                    raise SyncError(f"{column.replace('_', ' ')} cannot be empty")
                value = None
        applied[column] = value
        sets.append(f"{column}=?")
        params.append(value)
    if fields.get("secret"):
        nonce, ciphertext = security.encrypt(
            fields["secret"].strip(), security.AAD_CONNECTION
        )
        sets += ["secret_nonce=?", "secret_ciphertext=?", "secret_last4=?"]
        params += [nonce, ciphertext, fields["secret"].strip()[-4:]]
    if fields.get("site_url"):
        graph.split_site_url(fields["site_url"])
    # The drive id is what rclone actually mirrors, and site URL + library are the
    # two things that identify it. When either genuinely changes the stored id is
    # stale, so it is discarded and the next sync re-resolves — leaving it would go
    # on mirroring the previous library while the UI showed the new one, which is
    # wrong data rather than an error.
    #
    # Compared against the stored value, not merely present in the patch: the edit
    # form submits every field on every save, so testing for presence discarded the
    # id on each Save and made an unrelated rename cost a Graph round trip.
    if any(f in applied and applied[f] != before[f] for f in ("site_url", "library")):
        sets += ["drive_id=NULL", "drive_name=NULL"]
    if not sets:
        return get(conn_id)
    params.append(conn_id)
    db.execute(f"UPDATE sync_connections SET {', '.join(sets)} WHERE id=?", params)
    db.audit("sync.update", actor=actor, detail=f"{conn_id} {sorted(fields)}")
    broker.publish("sync_dirty")
    return get(conn_id)


def delete(conn_id: str, *, drop_mirror: bool = False, actor: str | None = None) -> dict:
    row = get(conn_id)
    if row is None:
        raise SyncError("no such connection")
    removed = False
    if drop_mirror:
        target = Path(row["mirror_dir"])
        # Same guard as deleting an extracted archive: only ever inside the
        # directory the app itself created.
        if security.is_within(target, settings.sync_root) and target.is_dir():
            shutil.rmtree(target, ignore_errors=True)
            removed = True
            if settings.corpus_root == target:
                settings.clear_corpus_root()
    db.execute("DELETE FROM sync_connections WHERE id=?", (conn_id,))
    db.audit("sync.delete", actor=actor,
             detail=f"{row['label']}" + (" (mirror removed)" if removed else ""))
    broker.log(
        f"Removed connection {row['label']!r}"
        + (". Run 'purge missing' to drop its documents from the index."
           if removed else "."),
        level="warn",
    )
    broker.publish("sync_dirty")
    return {"deleted": conn_id, "mirror_removed": removed}


async def test(conn_id: str, *, actor: str | None = None) -> dict:
    """Probe a connection and remember the verdict, like testing an API key."""
    row = db.one("SELECT * FROM sync_connections WHERE id=?", (conn_id,))
    if row is None:
        raise SyncError("no such connection")
    try:
        secret = client_secret(conn_id)
    except SyncError as exc:
        result = {"ok": False, "note": str(exc)}
    else:
        result = await graph.probe(
            row["tenant"], row["client_id"], secret, row["site_url"], row["library"],
        )
    db.execute(
        "UPDATE sync_connections SET last_test_at=?, last_test_ok=?, last_test_note=? WHERE id=?",
        (time.time(), int(bool(result["ok"])), result["note"][:400], conn_id),
    )
    if result.get("drive_id"):
        db.execute(
            "UPDATE sync_connections SET drive_id=?, drive_name=COALESCE(?, drive_name) WHERE id=?",
            (result["drive_id"], result.get("drive_name"), conn_id),
        )
    db.audit("sync.test", actor=actor, detail=f"{row['label']}: {result['note'][:120]}")
    broker.publish("sync_dirty")
    return {**result, "connection": get(conn_id)}


# --- the sync job --------------------------------------------------------


class SyncJob:
    """Mirrors one connected library into its directory, then indexes it."""

    def __init__(self, conn_id: str, *, actor: str | None = None) -> None:
        # Single hyphen: the job kind is derived by splitting the id on "-".
        self.id = f"sync-{uuid.uuid4().hex[:8]}"
        self.conn_id = conn_id
        self.actor = actor
        self.cancel = asyncio.Event()
        self.total = 0
        self.done = 0
        self.failed = 0
        self.skipped = 0
        self.deleted = 0
        self.bytes_done = 0
        # The library's own size, filled by the preflight. Separate from
        # done/bytes_done, which count only what this run transferred.
        self.library_files = 0
        self.library_bytes = 0
        # True once the preflight has actually measured the library. Until then
        # library_files/bytes are placeholders that must not reach the row.
        self.library_measured = False
        self.mirror: Path | None = None
        self._proc: asyncio.subprocess.Process | None = None
        self._error_tail: list[str] = []
        # What this run moved, in order. "12 files transferred" answers whether the
        # sync worked; the list answers what changed in the data room, which is the
        # question someone actually has on a Monday morning.
        self._changes: list[dict] = []
        # Live detail from rclone's stats, for the running view. Replaced wholesale
        # each tick — it describes this instant, not the run.
        self.transferring: list[dict] = []
        self.speed_bps = 0.0
        self.eta_s: float | None = None
        self.elapsed_s = 0.0

    def live_snapshot(self) -> dict:
        """The figures a running sync has that its row does not yet.

        The row is only written at the end, so without this a running sync reads as
        stalled. Only meaningful while the run is actually running — see the caller.
        """
        out = {
            "transferred": self.done, "unchanged": self.skipped, "deleted": self.deleted,
            "errors": self.failed, "bytes": self.bytes_done,
            "transferring": self.transferring[:6],
            "speed_bps": round(self.speed_bps, 1),
            "eta_s": self.eta_s,
            "elapsed_s": round(self.elapsed_s, 1),
            # The tail rather than the head: while a sync is running, the files it
            # touched most recently are the interesting ones.
            "changes": list(self._changes)[-200:],
            "error_lines": list(self._error_tail),
            "live": True,
        }
        if self.library_measured:
            out["library_files"] = self.library_files
            out["library_bytes"] = self.library_bytes
        return out

    def _publish(self, status: str = "running", message: str = "") -> None:
        broker.publish(
            "job", job_id=self.id, job_kind="sync", status=status, total=self.total,
            done=self.done, failed=self.failed, skipped=self.skipped,
            deleted=self.deleted, bytes_done=self.bytes_done, message=message,
            conn_id=self.conn_id,
            # Enough for the detail view to render a running sync without polling.
            # Bounded: rclone runs four transfers at a time, and a list that grew
            # would be a growing event on a 2-second timer.
            transferring=self.transferring[:6],
            speed_bps=round(self.speed_bps, 1),
            eta_s=self.eta_s,
            elapsed_s=round(self.elapsed_s, 1),
            library_bytes=self.library_bytes if self.library_measured else None,
        )

    # --- rclone plumbing -------------------------------------------------

    def _env(self, row: dict, secret: str) -> dict:
        """rclone's configuration, entirely in the environment.

        No config file to write (the rootfs is read-only and a file would be one
        more copy of the secret at rest), and nothing sensitive in argv, which
        `ps` would expose to anything else running on the host.
        """
        return {
            "PATH": "/usr/local/bin:/usr/bin:/bin",
            "HOME": "/tmp",
            "RCLONE_CONFIG": "/dev/null",
            "RCLONE_ONEDRIVE_CLIENT_ID": row["client_id"],
            "RCLONE_ONEDRIVE_CLIENT_SECRET": secret,
            "RCLONE_ONEDRIVE_TENANT": row["tenant"],
            "RCLONE_ONEDRIVE_CLIENT_CREDENTIALS": "true",
            "RCLONE_ONEDRIVE_DRIVE_ID": row["drive_id"] or "",
            # The docs are explicit that "onedrive" does not work with the client
            # credentials flow; a SharePoint library is a documentLibrary.
            "RCLONE_ONEDRIVE_DRIVE_TYPE": "documentLibrary",
        }

    def _filters(self, row: dict) -> list[str]:
        """Only fetch what ingest could index, unless told otherwise.

        A real data room carries plenty ingest ignores — .msg archives, images,
        video — and every one of those is bandwidth and disk for nothing.
        """
        if not row["only_supported_types"]:
            return []
        args: list[str] = []
        for ext in sorted(SUPPORTED_EXTS):
            args += ["--include", f"*{ext}"]
        return args

    async def _run_rclone(self, args: list[str], env: dict, *, parse: bool) -> int:
        """Run rclone, streaming its JSON log. Returns the exit code."""
        # stdout is discarded rather than piped: progress and errors come on
        # stderr, and an unread pipe would deadlock rclone once its buffer filled.
        self._proc = await asyncio.create_subprocess_exec(
            settings.rclone_bin, *args,
            stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.PIPE, env=env,
        )
        proc = self._proc
        # Shared with _consume_log, which puts JSON error lines back into it.
        self._error_tail = []
        tail = self._error_tail

        async def pump_stderr() -> None:
            assert proc.stderr is not None
            async for raw in proc.stderr:
                line = raw.decode("utf-8", "replace").strip()
                if not line:
                    continue
                if parse and self._consume_log(line):
                    continue
                if not _BENIGN_LOG.search(line):
                    tail.append(line)
                    del tail[:-20]

        async def watch_cancel() -> None:
            await self.cancel.wait()
            if proc.returncode is None:
                proc.terminate()
                try:
                    await asyncio.wait_for(proc.wait(), timeout=10)
                except asyncio.TimeoutError:
                    proc.kill()

        canceller = asyncio.create_task(watch_cancel())
        try:
            await asyncio.wait_for(
                asyncio.gather(pump_stderr(), proc.wait()),
                timeout=settings.sync_timeout_s,
            )
        except asyncio.TimeoutError:
            proc.kill()
            raise SyncError(
                f"rclone exceeded DD_SYNC_TIMEOUT ({settings.sync_timeout_s}s)"
            ) from None
        finally:
            canceller.cancel()
            self._proc = None

        if proc.returncode != 0 and not self.cancel.is_set():
            detail = " | ".join(tail[-3:]) or f"exit code {proc.returncode}"
            raise SyncError(f"rclone failed: {detail}")
        return proc.returncode or 0

    def _consume_log(self, line: str) -> bool:
        """Map one rclone JSON log line onto job progress. True if consumed."""
        if not line.startswith("{"):
            return False
        try:
            event = json.loads(line)
        except ValueError:
            return False
        stats = event.get("stats")
        if not isinstance(stats, dict):
            # Structured logs are swallowed so routine notices ("config file not
            # found") stay out of the error tail — but an error or critical line
            # is usually the *only* statement of why the run failed, so it is put
            # back into the tail rather than merely counted. rclone reports bad
            # credentials exactly this way, and without this the failure would
            # reach the operator as "exit code 1".
            level = event.get("level")
            if level in {"error", "critical"}:
                self.failed += 1
                msg = str(event.get("msg", "")).strip()
                if msg:
                    broker.log(
                        f"  {msg[:200]}",
                        level="warn",
                        source="sync",
                        job_id=self.id,
                        context={"rclone_level": level, "rclone_message": msg,
                                 "object": event.get("object")},
                    )
                    obj = event.get("object")
                    self._error_tail.append(f"{obj}: {msg}" if obj else msg)
                    del self._error_tail[:-db.SYNC_RUN_ERRORS_MAX]
                return True
            self._note_change(event)
            return True
        self.done = int(stats.get("transfers") or 0)
        checks = int(stats.get("checks") or 0)
        # rclone's "totalTransfers" only covers what it decided to move; files it
        # skipped as unchanged surface as checks.
        self.total = max(int(stats.get("totalTransfers") or 0), self.done)
        self.skipped = max(0, checks - self.done)
        self.bytes_done = int(stats.get("bytes") or 0)
        self.deleted = int(stats.get("deletes") or 0)
        self.failed = int(stats.get("errors") or 0)
        self.speed_bps = float(stats.get("speed") or 0.0)
        self.elapsed_s = float(stats.get("elapsedTime") or 0.0)
        eta = stats.get("eta")
        self.eta_s = float(eta) if isinstance(eta, (int, float)) else None
        # What is in flight right now, for the running view. rclone reports each
        # in-progress object with its own byte count and percentage.
        live = []
        for t in (stats.get("transferring") or [])[:6]:
            if not isinstance(t, dict):
                continue
            live.append({
                "name": str(t.get("name") or "")[:200],
                "size": int(t.get("size") or 0),
                "bytes": int(t.get("bytes") or 0),
                "percentage": int(t.get("percentage") or 0),
                "speed_bps": float(t.get("speedAvg") or t.get("speed") or 0.0),
            })
        self.transferring = live
        self._publish(message=f"{self.done} transferred")
        return True

    # rclone's own wording for what it did to an object. Matched by prefix so a
    # variant like "Copied (replaced existing)" lands in the same bucket, and an
    # unrecognised message is simply not a change rather than a crash.
    _CHANGE_VERBS = (
        ("Copied", "copied"),
        ("Updated", "updated"),
        ("Moved", "moved"),
        ("Renamed", "moved"),
        ("Deleted", "deleted"),
    )

    def _note_change(self, event: dict) -> None:
        """Record one per-object line as a change, if that is what it is."""
        obj = event.get("object")
        if not obj or len(self._changes) >= db.SYNC_RUN_CHANGES_MAX:
            return
        msg = str(event.get("msg") or "")
        for prefix, op in self._CHANGE_VERBS:
            if msg.startswith(prefix):
                self._changes.append({"op": op, "path": str(obj)[:400]})
                return

    # --- lifecycle -------------------------------------------------------

    async def _preflight(self, row: dict, env: dict, mirror: Path) -> None:
        """Refuse a library that will not fit before fetching any of it."""
        args = ["size", ":onedrive:", "--json", *self._filters(row)]
        # Registered and bounded like the transfer: a hung listing must be
        # cancellable from the UI and must honour DD_SYNC_TIMEOUT, or the job sits
        # there holding the one-sync-at-a-time lock with no way out.
        self._proc = await asyncio.create_subprocess_exec(
            settings.rclone_bin, *args,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE, env=env,
        )
        proc = self._proc

        async def watch_cancel() -> None:
            await self.cancel.wait()
            if proc.returncode is None:
                proc.terminate()

        canceller = asyncio.create_task(watch_cancel())
        try:
            out, err = await asyncio.wait_for(
                proc.communicate(), timeout=settings.sync_timeout_s
            )
        except asyncio.TimeoutError:
            proc.kill()
            raise SyncError(
                f"listing the library exceeded DD_SYNC_TIMEOUT ({settings.sync_timeout_s}s)"
            ) from None
        finally:
            canceller.cancel()
            self._proc = None

        if self.cancel.is_set():
            return
        if proc.returncode != 0:
            detail = err.decode("utf-8", "replace").strip().splitlines()
            raise SyncError(f"could not size the library: {detail[-1] if detail else 'unknown'}")
        try:
            data = json.loads(out.decode("utf-8", "replace") or "{}")
            count = int(data.get("count") or 0)
            remote_bytes = int(data.get("bytes") or 0)
        except (ValueError, TypeError) as exc:
            raise SyncError(f"could not read the library size: {exc}") from None

        if remote_bytes > settings.max_sync_gb * 1e9:
            raise SyncError(
                f"the library is {remote_bytes / 1e9:.1f} GB, over the "
                f"{settings.max_sync_gb} GB limit (raise DD_MAX_SYNC_GB if that is intended)"
            )
        # Only the bytes not already mirrored have to fit. Comparing the whole
        # library against free space refuses every resync of a library that only
        # just fitted the first time — including a no-op one with nothing to fetch,
        # since its own mirror is what consumed the space.
        free = shutil.disk_usage(settings.data_dir).free
        held = _dir_size(mirror)
        needed = max(0, remote_bytes - held)
        if needed > free:
            raise SyncError(
                f"the library is {remote_bytes / 1e9:.1f} GB and {held / 1e9:.1f} GB is "
                f"already mirrored, so it needs another {needed / 1e9:.1f} GB — only "
                f"{free / 1e9:.1f} GB is free on the data volume"
            )
        self.total = count
        # What the library *holds*, as distinct from what this run moves. The row
        # stores these, so an incremental sync that transfers nothing still shows
        # the library's real size rather than "0 files".
        self.library_files = count
        self.library_bytes = remote_bytes
        self.library_measured = True
        broker.log(
            f"{count} file(s), {remote_bytes / 1e9:.2f} GB in the library · "
            f"{held / 1e9:.2f} GB already mirrored · {free / 1e9:.1f} GB free."
        )
        self._publish(message="starting transfer")

    async def run(self, *, then_ingest: bool = True) -> None:
        row = db.one("SELECT * FROM sync_connections WHERE id=?", (self.conn_id,))
        if row is None:
            broker.log("Sync aborted: connection not found.", level="error",
                       source="sync", job_id=self.id,
                       context={"connection_id": self.conn_id})
            return
        row = dict(row)
        label = row["label"]
        started = time.time()
        db.execute("UPDATE sync_connections SET status='syncing', error=NULL WHERE id=?",
                   (self.conn_id,))
        db.job_upsert(self.id, kind="sync", status="running", message=f"syncing {label}")
        db.sync_run_start(self.id, self.conn_id, label, self.actor)
        broker.publish("sync_dirty")

        mirror = Path(row["mirror_dir"])
        self.mirror = mirror
        try:
            secret = client_secret(self.conn_id)

            if not row["drive_id"]:
                broker.log(f"Resolving the library behind {row['site_url']} …")
                access = await graph.token(row["tenant"], row["client_id"], secret)
                resolved = await graph.resolve_library(access, row["site_url"], row["library"])
                db.execute(
                    "UPDATE sync_connections SET drive_id=?, drive_name=? WHERE id=?",
                    (resolved["drive_id"], resolved["drive_name"], self.conn_id),
                )
                row["drive_id"] = resolved["drive_id"]
                row["drive_name"] = resolved["drive_name"]

            mirror.mkdir(parents=True, exist_ok=True)
            env = self._env(row, secret)
            await self._preflight(row, env, mirror)
            if self.cancel.is_set():
                # Cancelled while listing. Stop here rather than falling through
                # and starting a transfer nobody asked to continue.
                await self._finish(row, "cancelled", started, then_ingest=False)
                return

            verb = "sync" if row["mirror_deletions"] else "copy"
            args = [
                verb, ":onedrive:", str(mirror),
                "--use-json-log", "--stats", "2s", "--stats-log-level", "NOTICE",
                # INFO, so each object rclone moves is named. Unchanged files are
                # not logged at this level, so the volume tracks what changed
                # rather than the size of the library.
                "-v",
                "--max-size", f"{settings.max_file_mb}M",
                "--transfers", "4", "--checkers", "8",
                "--low-level-retries", "5", "--retries", "3",
                *self._filters(row),
            ]
            if row["mirror_deletions"]:
                # A connection pointed at the wrong library must not be able to
                # empty the mirror in one run.
                args += ["--max-delete", str(settings.max_sync_delete)]
            await self._run_rclone(args, env, parse=True)
        except (SyncError, graph.GraphError) as exc:
            await self._fail(label, str(exc))
            return
        except Exception as exc:  # noqa: BLE001 — a sync must not take the app down
            await self._fail(label, f"{type(exc).__name__}: {exc}")
            return

        if self.cancel.is_set():
            await self._finish(row, "cancelled", started, then_ingest=False)
            return
        await self._finish(row, "ok", started, then_ingest=then_ingest)

    async def _fail(self, label: str, msg: str) -> None:
        db.execute("UPDATE sync_connections SET status='failed', error=? WHERE id=?",
                   (msg[:400], self.conn_id))
        db.job_upsert(self.id, status="failed", message=msg, finished_at=time.time())
        db.sync_run_update(
            self.id, finished_at=time.time(), status="failed", message=msg, error=msg,
            transferred=self.done, unchanged=self.skipped, deleted=self.deleted,
            errors=max(self.failed, 1), bytes=self.bytes_done,
            changes=self._changes, error_lines=self._error_tail,
        )
        broker.log(f"Sync of {label!r} failed: {msg}", level="error",
                   source="sync", job_id=self.id,
                   context={"connection_id": self.conn_id, "label": label, "error": msg})
        self._publish("failed", msg)
        broker.publish("sync_dirty")

    async def _finish(self, row: dict, status: str, started: float, *, then_ingest: bool) -> None:
        elapsed = time.time() - started
        # n_files / bytes_total describe the library, not this run: an incremental
        # sync that transfers nothing would otherwise store zeros and the UI would
        # report a healthy connection as "0 files".
        #
        # And only when this run actually measured them. A run cancelled during
        # the listing has no figures, and writing its zeros would throw away what
        # the last good sync knew — the same "0 files" bug by another route.
        new_status = "ok" if status == "ok" else "failed" if status == "failed" else "ok"
        if self.library_measured:
            db.execute(
                "UPDATE sync_connections SET status=?, error=NULL, last_sync_at=?,"
                " last_sync_seconds=?, n_files=?, n_deleted=?, bytes_total=? WHERE id=?",
                (new_status, time.time(), elapsed, self.library_files, self.deleted,
                 self.library_bytes, self.conn_id),
            )
        else:
            db.execute(
                "UPDATE sync_connections SET status=?, error=NULL, last_sync_seconds=?"
                " WHERE id=?",
                (new_status, elapsed, self.conn_id),
            )
        size = (f"{self.bytes_done / 1e9:.2f} GB" if self.bytes_done >= 1e9
                else f"{self.bytes_done / 1e6:.1f} MB")
        msg = (
            f"{'Synced' if status == 'ok' else 'Sync cancelled for'} {row['label']}: "
            f"{self.done} transferred ({size}) in {elapsed:.0f}s"
            + (f" · {self.skipped} unchanged" if self.skipped else "")
            + (f" · {self.deleted} deleted" if self.deleted else "")
            + (f" · {self.failed} errors" if self.failed else "")
        )
        db.job_upsert(self.id, status="done" if status == "ok" else status, total=self.total,
                      done=self.done, failed=self.failed, message=msg, finished_at=time.time())
        db.sync_run_update(
            self.id, finished_at=time.time(), status=status, message=msg,
            transferred=self.done, unchanged=self.skipped, deleted=self.deleted,
            errors=self.failed, bytes=self.bytes_done,
            library_files=self.library_files if self.library_measured else None,
            library_bytes=self.library_bytes if self.library_measured else None,
            changes=self._changes, error_lines=self._error_tail,
        )
        broker.log(msg, level="success" if status == "ok" else "warn")
        self._publish("done" if status == "ok" else status, msg)
        broker.publish("sync_dirty")
        db.audit("sync.run", actor=self.actor,
                 detail=f"{row['label']}: {self.done} files, {self.deleted} deleted")

        if then_ingest:
            await self.ingest_mirror(row)

    async def ingest_mirror(self, row: dict) -> None:
        """Index the mirror, then drop documents whose files the sync removed."""
        from . import ingest, manifest, storage

        mirror = Path(row["mirror_dir"])
        broker.log(f"Indexing {mirror.name} …")
        settings.set_corpus_root(str(mirror))
        job = ingest.IngestJob(mirror, ocr=settings.ocr_enabled)
        # Registered so it is cancellable and visible in the running-jobs list,
        # rather than silently owned by this job.
        from .server import JOBS

        JOBS[job.id] = job
        try:
            await job.run()
        finally:
            JOBS.pop(job.id, None)
            manifest.invalidate_manifest()
        db.sync_run_update(
            self.id,
            indexed_new=max(job.done - job.skipped - job.failed, 0),
            indexed_failed=job.failed,
        )

        if self.deleted:
            # Ingest never removes anything, so a file the sync deleted would
            # otherwise linger in the catalogue pointing at a path that is gone.
            removed = await asyncio.to_thread(storage.purge_missing)
            db.sync_run_update(self.id, purged=removed["purged_documents"])
            broker.log(
                f"{self.deleted} file(s) removed remotely · "
                f"{removed['purged_documents']} document(s) dropped from the index."
            )


# --- scheduling ----------------------------------------------------------


def due_connections(now: float | None = None) -> list[dict]:
    """Connections whose interval has elapsed. Manual-only ones never are."""
    now = time.time() if now is None else now
    due = []
    for row in db.rows(
        "SELECT * FROM sync_connections WHERE interval_minutes > 0 AND status != 'syncing'"
    ):
        last = row["last_sync_at"] or 0
        if now - last >= row["interval_minutes"] * 60:
            due.append(_public(row))
    return due
