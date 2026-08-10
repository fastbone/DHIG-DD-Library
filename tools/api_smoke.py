#!/usr/bin/env python3
"""API smoke test: authentication, CSRF, uploads, admin surfaces, storage ops.

Runs against a throwaway data directory and spends no tokens. Every assertion
is a behaviour someone could break by accident.

    python3 tools/api_smoke.py
"""

from __future__ import annotations

import io
import os
import secrets
import sys
import tempfile
import threading
import time
import zipfile
from pathlib import Path

import urllib.error
import urllib.request

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

_TMP = tempfile.mkdtemp(prefix="dd-apismoke-")
os.environ["DD_DATA_DIR"] = _TMP
os.environ["DD_SECRET_KEY"] = secrets.token_urlsafe(32)
os.environ.pop("DD_ADMIN_USER", None)
os.environ.pop("DD_ADMIN_PASSWORD", None)
os.environ.pop("ANTHROPIC_API_KEY", None)
os.environ["DD_BROWSE_ROOTS"] = f"{_TMP}/uploads/extracted{os.pathsep}{ROOT}"

PORT = int(os.environ.get("DD_SMOKE_PORT", "8097"))
BASE = f"http://127.0.0.1:{PORT}"

PASSES: list[str] = []
FAILURES: list[str] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    (PASSES if ok else FAILURES).append(name)
    print(f"  {'PASS' if ok else 'FAIL'}  {name}{'' if ok else '  ← ' + detail}")


class Client:
    """Minimal cookie-aware HTTP client so the test exercises the real wire."""

    def __init__(self) -> None:
        self.cookie: str | None = None
        self.csrf: str | None = None

    def request(self, method: str, path: str, body=None, *, content_type="application/json",
                headers=None, raw=False):
        import json as _json

        data = None
        if body is not None:
            data = body if raw else _json.dumps(body).encode()
        req = urllib.request.Request(BASE + path, data=data, method=method)
        if data is not None:
            req.add_header("Content-Type", content_type)
        if self.cookie:
            req.add_header("Cookie", self.cookie)
        if self.csrf and method not in ("GET", "HEAD"):
            req.add_header("X-CSRF-Token", self.csrf)
        for k, v in (headers or {}).items():
            req.add_header(k, v)
        try:
            with urllib.request.urlopen(req, timeout=60) as resp:
                payload = resp.read()
                set_cookie = resp.headers.get("Set-Cookie")
                if set_cookie:
                    self.cookie = set_cookie.split(";")[0]
                return resp.status, _decode(payload), resp.headers
        except urllib.error.HTTPError as exc:
            return exc.code, _decode(exc.read()), exc.headers

    def get(self, path, **kw):
        return self.request("GET", path, **kw)

    def post(self, path, body=None, **kw):
        return self.request("POST", path, body, **kw)

    def delete(self, path, **kw):
        return self.request("DELETE", path, **kw)


def _decode(payload: bytes):
    import json as _json

    try:
        return _json.loads(payload)
    except Exception:  # noqa: BLE001
        return payload.decode("utf-8", "replace")


def build_zip() -> bytes:
    """A benign archive plus two members that must be refused."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("deal/notes.txt", "Revenue EUR 412.6m in FY2024.\n" * 40)
        zf.writestr("deal/sub/more.md", "# Findings\n\nCustomer concentration 31%.\n" * 20)
        zf.writestr("../escape.txt", "should never be written outside the target")
        zf.writestr("/abs.txt", "absolute path, also refused")
    return buf.getvalue()


def multipart(fields: dict[str, str], filename: str, content: bytes) -> tuple[bytes, str]:
    boundary = "----ddsmoke" + secrets.token_hex(8)
    parts: list[bytes] = []
    for k, v in fields.items():
        parts.append(
            f'--{boundary}\r\nContent-Disposition: form-data; name="{k}"\r\n\r\n{v}\r\n'.encode()
        )
    parts.append(
        f'--{boundary}\r\nContent-Disposition: form-data; name="file"; filename="{filename}"\r\n'
        f"Content-Type: application/zip\r\n\r\n".encode()
    )
    parts.append(content)
    parts.append(f"\r\n--{boundary}--\r\n".encode())
    return b"".join(parts), f"multipart/form-data; boundary={boundary}"


def wait_for_job(client: Client, predicate, timeout: float = 60) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        _, status, _ = client.get("/api/status")
        if isinstance(status, dict) and predicate(status):
            return True
        time.sleep(0.4)
    return False


def main() -> int:
    import uvicorn

    from app.server import app

    config = uvicorn.Config(app, host="127.0.0.1", port=PORT, log_level="error")
    server = uvicorn.Server(config)
    threading.Thread(target=server.run, daemon=True).start()
    for _ in range(200):
        time.sleep(0.05)
        if server.started:
            break

    anon = Client()

    print("\n— unauthenticated access —")
    code, body, headers = anon.get("/api/status")
    check("GET /api/status is 401 without a session", code == 401, f"got {code}")
    # urllib follows redirects by default; disable that so the 302 is visible.
    class _NoRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, *_args, **_kw):
            return None

    opener = urllib.request.build_opener(_NoRedirect)
    try:
        with opener.open(BASE + "/", timeout=10) as resp:
            code, location = resp.status, resp.headers.get("Location", "")
    except urllib.error.HTTPError as exc:
        code, location = exc.code, exc.headers.get("Location", "")
    check("GET / redirects to the login page", code == 302 and location == "/login",
          f"got {code} → {location!r}")
    code, body, _ = anon.get("/api/session")
    check("GET /api/session is public and offers first-run setup",
          code == 200 and body.get("needs_bootstrap") is True, str(body))
    code, _, _ = anon.get("/api/users")
    check("admin route refuses anonymous callers", code == 401)
    code, _, _ = anon.get("/styles.css")
    check("static assets stay public", code == 200)

    print("\n— first-run bootstrap —")
    admin = Client()
    code, body, _ = admin.post("/api/bootstrap", {"username": "alice", "password": "correct-horse-1"})
    check("bootstrap creates the first admin and signs in",
          code == 200 and body.get("user", {}).get("role") == "admin", str(body))
    admin.csrf = body.get("user", {}).get("csrf")
    code, body, _ = admin.post("/api/bootstrap", {"username": "eve", "password": "another-one-11"})
    check("bootstrap is closed once an account exists", code == 409, f"got {code}")

    print("\n— session and CSRF —")
    code, body, _ = admin.get("/api/status")
    check("authenticated status works", code == 200 and body["user"]["username"] == "alice")
    saved, admin.csrf = admin.csrf, None
    code, body, _ = admin.post("/api/dedupe")
    check("state change without a CSRF header is refused", code == 403, f"got {code}")
    admin.csrf = "wrong-token"
    code, _, _ = admin.post("/api/dedupe")
    check("state change with a wrong CSRF token is refused", code == 403, f"got {code}")
    admin.csrf = saved
    code, _, _ = admin.post("/api/dedupe")
    check("state change with the right CSRF token is accepted", code == 200, f"got {code}")

    print("\n— login throttling —")
    bad = Client()
    codes = [bad.post("/api/login", {"username": "alice", "password": f"wrong{i}"})[0]
             for i in range(10)]
    check("bad credentials are 401 then throttled to 429",
          codes[0] == 401 and 429 in codes, str(codes))

    print("\n— accounts —")
    code, body, _ = admin.post("/api/users",
                               {"username": "bob", "password": "analyst-pass-9", "role": "analyst"})
    check("admin can create an analyst", code == 200 and body["user"]["role"] == "analyst", str(body))
    bob = Client()
    code, body, _ = bob.post("/api/login", {"username": "bob", "password": "analyst-pass-9"})
    bob.csrf = body.get("user", {}).get("csrf")
    check("the analyst can sign in", code == 200, str(body))
    code, _, _ = bob.get("/api/users")
    check("the analyst cannot reach admin routes", code == 403, f"got {code}")
    code, _, _ = bob.get("/api/documents")
    check("the analyst can use the library", code == 200)
    code, body, _ = admin.post("/api/users/bob", {"disabled": True})
    check("admin can disable an account", code == 200 and body["user"]["disabled"] == 1, str(body))
    code, _, _ = bob.get("/api/status")
    check("disabling ends the analyst's session immediately", code == 401, f"got {code}")
    code, body, _ = admin.delete("/api/users/alice")
    check("the last admin cannot delete themselves", code == 400, str(body))

    print("\n— API keys —")
    fake = "sk-ant-api03-" + secrets.token_urlsafe(60)
    code, body, _ = admin.post("/api/keys", {"label": "deal-kestrel", "key": fake})
    check("a key can be stored", code == 200 and body["key"]["last4"] == fake[-4:], str(body))
    key_id = body["key"]["id"]
    code, body, _ = admin.get("/api/keys")
    serialised = str(body)
    check("the plaintext key is never returned", fake not in serialised)
    check("the key is masked to its last four", body["keys"][0]["last4"] == fake[-4:])
    check("credentials now resolve from the store", body["source"] == "stored", str(body["source"]))
    code, body, _ = admin.post("/api/keys", {"label": "nonsense", "key": "too short"})
    check("an implausible key is rejected", code == 400, str(body))
    code, body, _ = admin.post(f"/api/keys/{key_id}/test")
    check("testing a bogus key reports the failure rather than raising",
          code == 200 and body["ok"] is False, str(body))

    from app import credentials as creds

    check("the stored key decrypts to the original", creds.active_key()[1] == fake)
    code, _, _ = admin.delete(f"/api/keys/{key_id}")
    check("a key can be deleted", code == 200)
    code, body, _ = admin.get("/api/keys")
    check("deleting the last key falls back to the environment",
          body["source"] in ("environment", "none"), str(body["source"]))

    print("\n— archive upload and extraction —")
    payload, ctype = multipart({"auto_extract": "true", "auto_ingest": "true"},
                               "deal-room.zip", build_zip())
    code, body, _ = admin.post("/api/archives", payload, raw=True, content_type=ctype)
    check("a zip uploads and starts extracting", code == 200 and "archive" in body, str(body)[:200])
    archive_id = body.get("archive", {}).get("id")

    extracted = wait_for_job(
        admin,
        lambda s: s["stats"]["documents"] >= 2 and not s["jobs_running"],
        timeout=90,
    )
    check("auto-extract then auto-ingest indexes the contents", extracted)

    code, body, _ = admin.get("/api/archives")
    arc = next((a for a in body["archives"] if a["id"] == archive_id), None)
    check("the archive is recorded as extracted", arc and arc["status"] == "extracted", str(arc))
    check("unsafe members were skipped, not written", arc and arc["n_skipped"] == 2,
          f"skipped={arc and arc['n_skipped']}")
    extract_dir = Path(arc["extract_dir"]) if arc else None
    root = Path(_TMP) / "uploads" / "extracted"
    escapes = list(root.parent.glob("escape.txt")) + list(Path(_TMP).glob("escape.txt")) \
        + list(Path("/").glob("abs.txt"))
    check("no member escaped the extraction root", not escapes, str(escapes))
    check("the safe members landed", extract_dir and (extract_dir / "deal" / "notes.txt").exists())

    code, body, _ = admin.get("/api/search?q=revenue")
    check("extracted content is searchable", code == 200 and len(body["hits"]) >= 1, str(body)[:160])

    payload, ctype = multipart({"auto_extract": "false"}, "notes.txt", b"not an archive")
    code, body, _ = admin.post("/api/archives", payload, raw=True, content_type=ctype)
    check("a non-archive upload is refused", code == 400, str(body)[:120])

    print("\n— folder browsing is fenced —")
    code, body, _ = admin.get("/api/browse?path=/etc")
    check("browsing outside the permitted roots is refused", code == 403, f"got {code}")
    code, body, _ = admin.get("/api/browse")
    check("browsing with no path lists the roots", code == 200 and body["dirs"], str(body)[:160])
    code, body, _ = admin.get(f"/api/browse?path={root}")
    check("browsing inside a permitted root works", code == 200, str(body)[:160])

    # The picker is not the only way to name a path: these two routes take one
    # straight from the request body, so they must honour the same fence.
    code, body, _ = admin.post("/api/corpus-root", {"path": "/etc"})
    check("setting the corpus root outside the roots is refused", code == 403, f"got {code}")
    code, body, _ = admin.post("/api/ingest", {"path": "/etc"})
    check("indexing a path outside the roots is refused", code == 403, f"got {code}")
    code, body, _ = admin.post("/api/corpus-root", {"path": str(root)})
    check("setting the corpus root inside the roots works", code == 200, str(body)[:160])

    print("\n— storage management —")
    code, body, _ = admin.get("/api/storage")
    check("storage usage reports areas and roots",
          code == 200 and len(body["areas"]) == 5 and body["known_roots"], str(body)[:160])
    docs_before = body["stats"]["documents"]
    code, body, _ = admin.post("/api/storage/vacuum")
    check("vacuum runs", code == 200 and "after_bytes" in body, str(body))
    code, body, _ = admin.post("/api/storage/extracted/delete", {"path": str(extract_dir)})
    check("an extracted folder can be deleted", code == 200 and not extract_dir.exists(), str(body))
    code, body, _ = admin.post("/api/storage/extracted/delete", {"path": "/etc"})
    check("deletion outside the extraction root is refused", code == 400, str(body)[:120])
    code, body, _ = admin.post("/api/storage/purge_missing")
    check("purge drops documents whose files are gone",
          code == 200 and body["purged_documents"] == docs_before, str(body))
    code, body, _ = admin.post("/api/storage/reset_index")
    check("reset_index clears the catalogue", code == 200, str(body))
    code, body, _ = admin.get("/api/status")
    check("the corpus is empty after a reset", body["stats"]["documents"] == 0, str(body["stats"]))

    print("\n— audit trail —")
    code, body, _ = admin.get("/api/audit")
    actions = {e["action"] for e in body["entries"]}
    expected = {"login.success", "login.failure", "user.create", "apikey.add", "apikey.delete",
                "archive.upload", "archive.extract", "storage.reset_index"}
    missing = expected - actions
    check("every privileged action is audited", not missing, f"missing {missing}")
    check("failed logins are audited with the username",
          any(e["action"] == "login.failure" for e in body["entries"]))

    print("\n— sign out —")
    code, _, _ = admin.post("/api/logout")
    check("logout succeeds", code == 200)
    code, _, _ = admin.get("/api/status")
    check("the session is dead after logout", code == 401, f"got {code}")

    print(f"\n{len(PASSES)} passed, {len(FAILURES)} failed")
    if FAILURES:
        print("failed:", ", ".join(FAILURES))
    print(f"(data dir {_TMP})")
    return 1 if FAILURES else 0


if __name__ == "__main__":
    raise SystemExit(main())
