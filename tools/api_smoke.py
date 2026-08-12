#!/usr/bin/env python3
"""API smoke test: authentication, CSRF, uploads, admin surfaces, storage ops.

Runs against a throwaway data directory and spends no tokens. Every assertion
is a behaviour someone could break by accident.

    python3 tools/api_smoke.py
"""

from __future__ import annotations

import io
import json
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
# The data directory is named "data", matching the container's /data volume.
# Not cosmetic: ingest once skipped every path with a component called "data",
# so an upload extracted into the volume indexed nothing in Docker while this
# suite — run against a bare temp dir — passed. Keep the shapes the same.
_DATA = str(Path(_TMP) / "data")
os.environ["DD_DATA_DIR"] = _DATA
os.environ["DD_SECRET_KEY"] = secrets.token_urlsafe(32)
os.environ.pop("DD_ADMIN_USER", None)
os.environ.pop("DD_ADMIN_PASSWORD", None)
os.environ.pop("ANTHROPIC_API_KEY", None)
os.environ["DD_BROWSE_ROOTS"] = f"{_DATA}/uploads/extracted{os.pathsep}{ROOT}"

# A sync engine that never finishes, so a sync can be held in flight while other
# requests are made against it. The job scrubs its child's environment, so the
# scenario travels in the binary path rather than an environment variable.
_HANGING_RCLONE = Path(_TMP) / "hanging_rclone.py"
_HANGING_RCLONE.write_text(
    "#!/usr/bin/env python3\n"
    "import os, runpy, sys\n"
    "os.environ['FAKE_RCLONE_MODE'] = 'hang'\n"
    f"sys.argv[0] = {str(ROOT / 'tools' / 'fake_rclone.py')!r}\n"
    f"runpy.run_path({str(ROOT / 'tools' / 'fake_rclone.py')!r}, run_name='__main__')\n"
)
_HANGING_RCLONE.chmod(0o755)
os.environ["DD_RCLONE_BIN"] = str(_HANGING_RCLONE)

PORT = int(os.environ.get("DD_SMOKE_PORT", "8097"))
BASE = f"http://127.0.0.1:{PORT}"

PASSES: list[str] = []
FAILURES: list[str] = []


def _probe_default_browse_roots():
    """True if /inbox is a default browse root, None if there is no /inbox here.

    Runs in a subprocess: the browse-root default is only consulted when
    DD_BROWSE_ROOTS is unset, and this suite sets it process-wide.
    """
    import subprocess

    if not Path("/inbox").is_dir():
        return None
    env = {k: v for k, v in os.environ.items() if k != "DD_BROWSE_ROOTS"}
    env["DD_DATA_DIR"] = tempfile.mkdtemp(prefix="dd-inbox-probe-")
    out = subprocess.run(
        [sys.executable, "-c",
         "import sys; sys.path.insert(0, %r)\n" % str(ROOT)
         + "from app.config import settings\n"
         + "print('/inbox' in [str(r) for r in settings.browse_roots])"],
        capture_output=True, text=True, env=env, timeout=60,
    )
    return out.stdout.strip() == "True"


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


def _spreadsheet_fixtures() -> dict[str, bytes]:
    """The four spreadsheet shapes a real data room contains.

    Extensions in a data room lie in both directions, and the app dispatches on
    the container rather than the name. Each of these went wrong at some point:
    a legacy workbook reached openpyxl and raised InvalidFileException; a modern
    workbook named .xls was refused by openpyxl's *extension* check even though
    its bytes were fine; and a reporting system's HTML table named .xls is not a
    workbook at all.
    """
    import base64
    import gzip

    from openpyxl import Workbook

    sys.path.insert(0, str(ROOT / "tools"))
    from make_sample_corpus import _LEGACY_XLS_GZ_B64

    legacy = gzip.decompress(base64.b64decode(_LEGACY_XLS_GZ_B64))

    buf = io.BytesIO()
    wb = Workbook()
    ws = wb.active
    ws.title = "Model"
    ws.append(["Revenue", 412600])
    ws.append(["EBITDA", 79192])
    ws.append(["Margin", "=B2/B1"])
    wb.save(buf)
    modern = buf.getvalue()

    return {
        # A genuine Excel 97-2003 workbook, correctly named.
        "deal/legacy_working_file.xls": legacy,
        # Modern workbook, wrongly named .xls.
        "deal/mislabelled_modern.xls": modern,
        # Legacy workbook, wrongly named .xlsx.
        "deal/mislabelled_legacy.xlsx": legacy,
        # An HTML export a reporting system called a spreadsheet.
        "deal/reporting_export.xls": (
            "<html><body><table>"
            "<tr><td>Backlog</td><td>214,000</td></tr>"
            "</table></body></html>"
        ).encode(),
    }


def build_zip() -> bytes:
    """A benign archive plus two members that must be refused."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("deal/notes.txt", "Revenue EUR 412.6m in FY2024.\n" * 40)
        zf.writestr("deal/sub/more.md", "# Findings\n\nCustomer concentration 31%.\n" * 20)
        # A supported extension over bytes that are not a PDF. Ingest must record
        # it as a failed document and log the failure with enough structure to
        # report — which is what the activity-log checks below read.
        zf.writestr("deal/corrupt_scan.pdf", b"%PDF-1.4 truncated before anything useful")
        for name, content in _spreadsheet_fixtures().items():
            zf.writestr(name, content)
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

    from app import graph
    from app.server import app

    # Point Microsoft Graph at a closed local port. The connection checks below
    # want the *failure* path, and without this they would make a real call to
    # login.microsoftonline.com — slow, and a hang on an offline machine. The
    # sync suite covers the success path against a stub.
    graph.LOGIN_HOST = "http://127.0.0.1:9"
    graph.GRAPH = "http://127.0.0.1:9"

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
    code, body, headers = admin.post("/api/bootstrap",
                                     {"username": "alice", "password": "correct-horse-1"})
    check("bootstrap creates the first admin and signs in",
          code == 200 and body.get("user", {}).get("role") == "admin", str(body))
    # This whole suite talks plain HTTP. A Secure cookie would be dropped by a
    # real browser, so sign-in would look fine and every later request would 401
    # — the shipped default must not do that.
    cookie_attrs = (headers.get("Set-Cookie") or "").lower()
    check("the session cookie is not Secure over plain HTTP",
          "secure" not in cookie_attrs, cookie_attrs)
    check("the session cookie is HttpOnly and SameSite=Lax",
          "httponly" in cookie_attrs and "samesite=lax" in cookie_attrs, cookie_attrs)
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
    # Connecting a library decides who can read a data room, so it sits with
    # accounts and keys rather than with everyday ingest.
    code, _, _ = bob.get("/api/sync/connections")
    check("the analyst cannot list connected libraries", code == 403, f"got {code}")
    code, _, _ = bob.post("/api/sync/connections",
                          {"label": "x", "site_url": "https://c.sharepoint.com/sites/X",
                           "tenant": "t", "client_id": "c", "secret": "s" * 12})
    check("the analyst cannot connect a library", code == 403, f"got {code}")
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
    # Also the guard that ingest can index a corpus living inside the data
    # directory at all — the extraction root is under it, as is any synced
    # library. A scan() that skips the data volume shows up here as zero
    # documents rather than as an error.
    check("auto-extract then auto-ingest indexes the contents", extracted)

    code, body, _ = admin.get("/api/archives")
    arc = next((a for a in body["archives"] if a["id"] == archive_id), None)
    check("the archive is recorded as extracted", arc and arc["status"] == "extracted", str(arc))
    check("unsafe members were skipped, not written", arc and arc["n_skipped"] == 2,
          f"skipped={arc and arc['n_skipped']}")
    extract_dir = Path(arc["extract_dir"]) if arc else None
    root = Path(_DATA) / "uploads" / "extracted"
    escapes = list(root.parent.glob("escape.txt")) + list(Path(_TMP).glob("escape.txt")) \
        + list(Path("/").glob("abs.txt"))
    check("no member escaped the extraction root", not escapes, str(escapes))
    check("the safe members landed", extract_dir and (extract_dir / "deal" / "notes.txt").exists())

    code, body, _ = admin.get("/api/search?q=revenue")
    check("extracted content is searchable", code == 200 and len(body["hits"]) >= 1, str(body)[:160])

    print("\n— spreadsheets are routed by content, not by extension —")
    # The end-to-end half: a genuine Excel 97-2003 workbook indexed through the
    # normal ingest path, with a figure only that workbook contains.
    # Searched by content unique to the legacy workbook. Deliberately not by
    # filename: the .xls and the .xlsx fixture are byte-identical, so which of the
    # two names ends up canonical is a race between concurrent extract workers —
    # the document is the content, and either name reaches it.
    code, body, _ = admin.get("/api/search?q=%22previous+ledger+system%22")
    hit = next(iter(body.get("hits", [])), None) if code == 200 else None
    check("a legacy .xls is indexed and searchable", hit is not None, str(body)[:200])
    if hit:
        code, card, _ = admin.get(f"/api/documents/{hit['doc_id']}")
        anchors = [a["anchor"] for a in card.get("anchors", [])]
        # Same anchor shape as a modern workbook, so a citation into a legacy file
        # resolves identically.
        check("legacy sheets get sheet-range anchors",
              anchors and all("!" in a and ":" in a for a in anchors), str(anchors))
        # Identical bytes filed under two names collapse to one content-addressed
        # document that knows both paths.
        filed_at = {card.get("rel_path"), *card.get("identical_copies_at", [])}
        check("the same workbook under both names collapses onto one document",
              any("legacy_working_file.xls" in p for p in filed_at)
              and any("mislabelled_legacy.xlsx" in p for p in filed_at),
              str(sorted(filed_at)))
    else:
        check("legacy sheets get sheet-range anchors", False, "legacy content not indexed")
        check("the same workbook under both names collapses onto one document", False, "missing")

    # The routing half, in process: every name/content combination, including the
    # ones dedupe hides above.
    from app import extract as _extract

    probe_dir = Path(_TMP) / "spreadsheets"
    probe_dir.mkdir(exist_ok=True)
    expectations = {
        # name                            container  must contain
        "deal/legacy_working_file.xls": ("ole2", "Consolidated P&L"),
        "deal/mislabelled_modern.xls": ("ooxml", "## formulas"),
        "deal/mislabelled_legacy.xlsx": ("ole2", "Consolidated P&L"),
        "deal/reporting_export.xls": ("other", "214,000"),
    }
    for name, content in _spreadsheet_fixtures().items():
        target = probe_dir / Path(name).name
        target.write_bytes(content)
        want_container, want_text = expectations[name]
        got_container = _extract._container(target)
        try:
            text, units = _extract.extract_spreadsheet(target)
        except Exception as exc:  # noqa: BLE001 — the failure is the finding
            check(f"{target.name} extracts", False, repr(exc))
            continue
        check(
            f"{target.name} is read as {want_container}",
            got_container == want_container and want_text in text and bool(units),
            f"container={got_container} units={len(units)} text_ok={want_text in text}",
        )
    # A legacy workbook exposes values but never formulas; the mirror has to say
    # so, or the model reads the absence as "this workbook has no formulas".
    text, _ = _extract.extract_spreadsheet(probe_dir / "legacy_working_file.xls")
    check("the legacy mirror discloses that formulas are unrecoverable",
          "formulas are not recoverable" in text, text[:160])
    # BIFF gives a time-only cell the same type as a date, and the serial lands on
    # the epoch day, so a 15:00 cut-off renders as "1899-12-31 15:00:00" unless the
    # date part is checked. An invented timestamp in a mirror gets quoted as fact.
    check("a time-only cell renders as a time, not an 1899 timestamp",
          "15:00" in text and "1899" not in text,
          next((ln for ln in text.splitlines() if "Cut-off" in ln), "row missing"))
    check("dates in a legacy workbook render as ISO dates, not serial numbers",
          "2023-03-14" in text and "44999" not in text,
          next((ln for ln in text.splitlines() if "Ledger closed" in ln), "row missing"))

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

    print("\n— corpus access —")
    from app import ingest as _ingest

    # Regression: the walk used to test every component of the *absolute* path
    # against SKIP_DIRS, which contained "data". Any root under a directory of
    # that name — including the container's own /data volume, where every
    # uploaded archive lands — therefore scanned as zero files and reported
    # success. This is the shape of that bug, in a permitted root.
    room = root / "deal-data" / "data" / "sub"
    room.mkdir(parents=True, exist_ok=True)
    (room / "memo.txt").write_text("quarterly revenue memo")
    found = _ingest.scan(root)
    check("a folder named 'data' is still scanned",
          any(p.name == "memo.txt" for p in found.files),
          str(sorted(p.name for p in found.files))[:200])

    # …but the app's own output must never be ingested as source material. Note
    # the paths: the data directory is _DATA (_TMP/data), so the real text mirror
    # is _DATA/derived. Writing to _TMP/derived would create an unrelated folder
    # that scan() is right to pick up, and prove nothing.
    Path(_DATA, "derived").mkdir(parents=True, exist_ok=True)
    Path(_DATA, "derived", "deadbeef.md").write_text("mirror text, not a source document")
    whole = _ingest.scan(Path(_DATA))
    check("the text mirror is never re-ingested",
          not any("derived" in p.parts for p in whole.files),
          str([str(p) for p in whole.files if "derived" in p.parts])[:200])

    # Unreadable paths must be reported, not silently dropped.
    blind = root / "unreadable"
    blind.mkdir(exist_ok=True)
    (blind / "hidden.txt").write_text("invisible")
    os.chmod(blind, 0o000)
    try:
        walked = _ingest.scan(root)
        denied = walked.blocked
    finally:
        os.chmod(blind, 0o755)
    check("an unreadable folder is reported rather than skipped in silence",
          denied >= 1 or os.geteuid() == 0,
          f"blocked={denied} (euid={os.geteuid()})")

    # The feed inbox has to be reachable without configuration: compose names it
    # in DD_BROWSE_ROOTS, but a non-Docker install relies on the default list.
    inbox_default = _probe_default_browse_roots()
    if inbox_default is None:
        print("  SKIP  /inbox is a browsable root by default  ← no /inbox on this host")
    else:
        check("an existing /inbox is a browsable root by default", inbox_default,
              "default roots did not include /inbox")

    code, body, _ = admin.get("/api/access-check")
    check("access check reports the runtime identity",
          code == 200 and "uid" in body.get("identity", {}), str(body)[:160])
    check("access check inspects every configured root",
          code == 200 and body.get("roots"), str(body)[:160])
    code, body, _ = admin.get("/api/access-check?path=/etc")
    check("access check honours the browse-root fence", code == 403, f"got {code}")
    code, body, _ = anon.get("/api/access-check")
    check("access check needs a session", code == 401, f"got {code}")
    code, body, _ = admin.post("/api/access-repair", {"path": str(root)})
    check("access repair runs and re-reports",
          code == 200 and "repaired" in body and "host_commands" in body, str(body)[:160])

    # Repair must reach a fixed point. Opening a directory reveals its contents,
    # which can themselves be locked, and one audit only lists MAX_ISSUES of
    # them — so a single pass leaves nested paths broken while claiming they
    # need a fix on the host. Only meaningful unprivileged; root reads anything.
    from app import access as _access

    nest = root / "nested-lock" / "inner" / "deeper"
    nest.mkdir(parents=True, exist_ok=True)
    (nest / "buried.txt").write_text("buried but ours")
    for level in (nest, nest.parent, nest.parent.parent):
        os.chmod(level, 0o000)
    try:
        healed = _access.repair([str(root)])
        reachable = any(p.name == "buried.txt" for p in _ingest.scan(root).files)
    finally:
        for level in (nest.parent.parent, nest.parent, nest):
            os.chmod(level, 0o755)
    check("repair opens nested locked folders in one pass, not just the top one",
          reachable and not healed["fixable"],
          f"reachable={reachable} fixable_left={healed['fixable']} "
          f"repaired={len(healed['repaired'])}")

    print("\n— connected libraries —")
    # No tenant here, so the probe that follows creation will fail — which is
    # itself worth asserting: a connection that cannot be reached is still stored
    # and reported honestly rather than rejected or silently marked good.
    client_secret = "sharepoint-client-secret-" + secrets.token_hex(6)
    code, body, _ = admin.post("/api/sync/connections", {
        "label": "DD Room", "site_url": "https://contoso.sharepoint.com/sites/ProjectX",
        "tenant": "tenant-id", "client_id": "client-id", "secret": client_secret,
    })
    check("a library can be connected", code == 200 and body["connection"]["id"], str(body)[:200])
    conn_id = body.get("connection", {}).get("id")
    check("the connection is probed on creation", "test" in body, str(body)[:160])

    code, body, _ = admin.get("/api/sync/connections")
    serialised = str(body)
    check("the client secret is never returned", client_secret not in serialised)
    check("the secret is masked to its last four",
          body["connections"][0]["secret_last4"] == client_secret[-4:], str(body)[:200])
    check("the mirror lives under the sync root",
          body["connections"][0]["mirror_dir"].startswith(body["sync_root"]), str(body)[:200])
    check("whether the sync engine is installed is reported",
          "available" in body["rclone"], str(body.get("rclone")))

    code, body, _ = admin.post("/api/sync/connections", {
        "label": "bad", "site_url": "not-a-url", "tenant": "t", "client_id": "c",
        "secret": "x" * 12,
    })
    check("a malformed site URL is refused", code == 400, f"got {code}: {str(body)[:120]}")

    code, body, _ = admin.post(f"/api/sync/connections/{conn_id}",
                               {"label": "DD Room 2026", "interval_minutes": 120})
    check("a connection can be edited",
          code == 200 and body["connection"]["interval_minutes"] == 120, str(body)[:160])
    rotated = "rotated-secret-" + secrets.token_hex(4)
    code, body, _ = admin.post(f"/api/sync/connections/{conn_id}", {"secret": rotated})
    check("rotating the secret updates the mask",
          code == 200 and body["connection"]["secret_last4"] == rotated[-4:], str(body)[:160])
    code, body, _ = admin.get("/api/sync/connections")
    check("the rotated secret is not returned either", rotated not in str(body))

    # The patch route drops nulls so untouched fields keep their value, which is
    # why the form sends "" to mean "back to the site's default library".
    code, body, _ = admin.post(f"/api/sync/connections/{conn_id}", {"library": "DD Room"})
    check("a library can be named", code == 200 and body["connection"]["library"] == "DD Room",
          str(body)[:160])
    code, body, _ = admin.post(f"/api/sync/connections/{conn_id}", {"library": ""})
    check("an empty library clears it rather than being ignored",
          code == 200 and body["connection"]["library"] is None, str(body)[:160])
    code, body, _ = admin.post(f"/api/sync/connections/{conn_id}", {"label": ""})
    check("an empty label is refused rather than nulling a required column",
          code == 400, f"got {code}: {str(body)[:120]}")

    code, body, _ = admin.post(f"/api/sync/connections/{conn_id}/test")
    check("testing an unreachable library reports a reason, not a crash",
          code == 200 and body["ok"] is False and body["note"], str(body)[:200])

    # A sync holds the corpus root and indexes its own mirror, so nothing else
    # may index at the same time. Held in flight with a sync engine that never
    # returns; the drive id is set directly because resolving it would need Graph.
    from app import db as _db

    _db.execute("UPDATE sync_connections SET drive_id='drv-1' WHERE id=?", (conn_id,))
    code, body, _ = admin.post(f"/api/sync/connections/{conn_id}/sync")
    sync_job = body.get("job_id") if code == 200 else None
    check("a sync starts", code == 200 and sync_job, str(body)[:160])
    running = wait_for_job(
        admin, lambda s: any(k.startswith("sync-") for k in s["jobs_running"]), timeout=30
    )
    check("the sync shows up as a running job", running)
    code, body, _ = admin.post("/api/ingest", {"path": str(root)})
    check("a manual ingest is refused while a sync is running", code == 409,
          f"got {code}: {str(body)[:120]}")
    code, body, _ = admin.post(f"/api/sync/connections/{conn_id}/sync")
    check("a second sync is refused", code == 409, f"got {code}: {str(body)[:120]}")
    if sync_job:
        admin.post(f"/api/jobs/{sync_job}/cancel")
    cleared = wait_for_job(admin, lambda s: not s["jobs_running"], timeout=30)
    check("cancelling the sync clears the running jobs", cleared)
    # Deliberately the archive's own folder rather than the whole extraction root:
    # the corpus-access checks above scatter files across the root, and indexing
    # those would leave documents behind whose files survive the extracted-folder
    # deletion later — which is exactly what the purge check measures.
    code, body, _ = admin.post("/api/ingest", {"path": str(extract_dir)})
    check("ingest works again once the sync is gone", code == 200, f"got {code}: {str(body)[:120]}")
    wait_for_job(admin, lambda s: not s["jobs_running"], timeout=60)

    code, body, _ = admin.delete("/api/sync/connections/does-not-exist")
    check("deleting an unknown connection is a 404", code == 404, f"got {code}")
    code, body, _ = admin.delete(f"/api/sync/connections/{conn_id}?drop_mirror=true")
    check("a connection can be deleted with its mirror", code == 200 and body["deleted"] == conn_id,
          str(body)[:160])
    code, body, _ = admin.get("/api/sync/connections")
    check("no connections remain", code == 200 and body["connections"] == [], str(body)[:160])

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

    print("\n— weekly spending budgets —")
    from app import budget as _budget
    from app import db as _bdb

    # Its own analyst, because this section runs before the accounts section and
    # must not depend on an account another check later disables.
    dana = Client()
    admin.post("/api/users",
               {"username": "dana", "password": "budget-pass-3", "role": "analyst"})
    _, dana_login, _ = dana.post("/api/login",
                                 {"username": "dana", "password": "budget-pass-3"})
    dana.csrf = (dana_login.get("user") or {}).get("csrf")

    code, body, _ = admin.get("/api/users")
    check("accounts report their budgets and the week's spend",
          code == 200 and "budget" in body["users"][0] and "budget_defaults" in body,
          str(body.get("budget_defaults")))
    check("the week starts on a Monday at midnight",
          time.localtime(body["week_start"]).tm_wday == 0
          and time.localtime(body["week_start"]).tm_hour == 0,
          time.strftime("%a %H:%M", time.localtime(body.get("week_start", 0))))
    # The contract is "next Monday at midnight", not "168 hours later": a week
    # containing a daylight-saving change is 167 or 169 hours long, and a fixed
    # offset would promise a reset at 23:00 or 01:00.
    reset = time.localtime(body["resets_at"])
    span_h = (body["resets_at"] - body["week_start"]) / 3600
    check("the reset is the next Monday at midnight",
          reset.tm_wday == 0 and reset.tm_hour == 0 and reset.tm_min == 0
          and 167 <= round(span_h) <= 169,
          f"{time.strftime('%a %H:%M', reset)} after {span_h:.0f}h")

    code, body, _ = admin.post("/api/users/dana", {"budget_ask": 5, "budget_index": "unlimited"})
    b = (body.get("user") or {}).get("budget") or {}
    check("a cap and an unlimited can be set together",
          code == 200 and b.get("ask", {}).get("limit_usd") == 5.0
          and b.get("index", {}).get("unlimited") is True, str(b)[:200])
    code, body, _ = admin.post("/api/users/dana", {"budget_ask": "default"})
    b = (body.get("user") or {}).get("budget") or {}
    check("'default' returns an account to the instance setting",
          code == 200 and b.get("ask", {}).get("inherited") is True, str(b.get("ask")))
    check("setting one budget leaves the other alone",
          b.get("index", {}).get("unlimited") is True, str(b.get("index")))
    admin.post("/api/users/dana", {"budget_ask": "default", "budget_index": "default"})
    code, body, _ = admin.get("/api/users")
    dana_b = next(u for u in body["users"] if u["username"] == "dana")["budget"]
    check("an account inheriting an unlimited default reports as inherited",
          dana_b["ask"]["inherited"] is True and dana_b["ask"]["unlimited"] is True,
          str(dana_b["ask"]))

    code, body, _ = admin.post("/api/users/dana", {"budget_ask": "twenty quid"})
    check("an unparseable budget is refused rather than stored as zero",
          code == 400, f"got {code}: {str(body)[:120]}")
    code, body, _ = dana.post("/api/users/dana", {"budget_ask": 999})
    check("an analyst cannot raise their own budget", code == 403, f"got {code}")

    # Spend is attributed and counted against the right budget.
    _bdb.spend_record("dana", "ask", "analyst", "claude-opus-5", 4.0, ref="probe")
    _bdb.spend_record("dana", "index", "carder", "claude-haiku-4-5", 99.0, ref="probe")
    code, body, _ = admin.get("/api/users")
    dana_row = next(u for u in body["users"] if u["username"] == "dana")
    check("spend lands against the budget it belongs to",
          abs(dana_row["spend_this_week"]["ask"] - 4.0) < 1e-6
          and abs(dana_row["spend_this_week"]["index"] - 99.0) < 1e-6,
          str(dana_row["spend_this_week"]))
    check("an unlimited budget never reads as exhausted",
          dana_row["budget"]["index"]["exhausted"] is False,
          str(dana_row["budget"]["index"]))

    # Both refusals need a stored key to reach: "no API key configured" is the more
    # fundamental precondition and is reported ahead of the budget, which is the
    # right order for whoever has to fix it. The key is never used — a refusal
    # happens before any request goes out, which is the point of a pre-flight.
    _, keyed, _ = admin.post("/api/keys",
                             {"label": "budget-probe", "key": "sk-ant-" + "b" * 40})
    probe_key = (keyed.get("key") or {}).get("id")

    admin.post("/api/users/dana", {"budget_ask": 1})
    code, body, _ = dana.post("/api/ask", {"question": "What is the revenue?", "verify": False})
    stream = str(body)
    check("a question is refused when the week's budget is gone",
          code == 200 and '"reason": "budget"' in stream, f"got {code}: {stream[:200]}")
    check("the refusal names the reset and how to raise it",
          "resets" in stream and "Accounts" in stream, stream[:240])
    check("the refusal costs nothing — no model call is made",
          "text_delta" not in stream and "usage" not in stream, stream[:200])

    # The sweep is a separate budget, refused separately.
    admin.post("/api/users/alice", {"budget_index": 0})
    code, body, _ = admin.post("/api/sweep", {"redo": False})
    check("a sweep is refused when the indexing budget is gone",
          code == 402 and "indexing" in str(body), f"got {code}: {str(body)[:160]}")
    admin.post("/api/users/alice", {"budget_index": "unlimited"})
    if probe_key:
        admin.delete(f"/api/keys/{probe_key}")

    # The grace is once a week, and pre-flight never grants it.
    _budget.set_budgets("grace-probe", ask=1.0, actor="smoke")
    _bdb.spend_record("grace-probe", "ask", "analyst", "claude-opus-5", 1.02)
    first, note = _budget.turn_decision("grace-probe", holding_grace=False)
    second, _ = _budget.turn_decision("grace-probe", holding_grace=False)
    check("the first overrun of the week is granted",
          first == _budget.GRACE and "one-time" in note, f"{first}: {note[:120]}")
    check("the second overrun of the week is refused",
          second == _budget.STOP, second)
    check("an answer already holding the grace keeps going inside it",
          _budget.turn_decision("grace-probe", holding_grace=True)[0] == _budget.CONTINUE)
    _bdb.spend_record("grace-probe", "ask", "analyst", "claude-opus-5", 0.20)   # past 1.10
    check("even a graced answer stops at the overrun ceiling",
          _budget.turn_decision("grace-probe", holding_grace=True)[0] == _budget.STOP)
    try:
        _budget.require("grace-probe", "ask")
        check("grace never lets a new question start on an empty budget", False, "allowed")
    except _budget.BudgetExceeded:
        check("grace never lets a new question start on an empty budget", True)

    code, body, _ = admin.get("/api/status")
    check("a user sees their own budget on status",
          code == 200 and "budget" in body and "ask" in body["budget"],
          str(body.get("budget"))[:160])

    print("\n— question refinement —")
    from app import agent as _agent
    from app import scope as _scope
    from app import tools as _apptools

    # The whole cost argument for scoping is that it reuses the analyst's cached
    # prefix instead of forking it. That is a property of the request bytes, not
    # of any behaviour, so it is asserted directly — a well-meant "let's give the
    # scoper its own tools" would otherwise pass every other test in this file
    # while quietly making each round cost more than the answer it prefaces.
    check("the scoper reuses the analyst's cached system prefix",
          _scope.system_for_scope() == _agent.system_blocks()[0])
    check("the scoper passes the tool array through unchanged",
          _scope.tools_for_scope() is _apptools.TOOLS)
    _tool_names = {t["name"] for t in _apptools.TOOLS}
    check("the scoper's tools are a subset of the analyst's",
          _scope.SCOPE_TOOLS <= _tool_names, str(_scope.SCOPE_TOOLS - _tool_names))
    check("the scoper cannot read documents, run code or write files",
          not (_scope.SCOPE_TOOLS
               & {"read_document", "run_python", "create_deliverable"}))

    # Preconditions match /api/ask exactly, and the route is fenced by the
    # middleware's default rather than by remembering to fence it.
    code, body, _ = anon.post("/api/scope", {"question": "What are the risks?"})
    check("scoping needs a session", code == 401, f"got {code}")
    no_csrf = Client()
    no_csrf.cookie = admin.cookie          # a real session, no CSRF header
    code, body, _ = no_csrf.post("/api/scope", {"question": "What are the risks?"})
    check("scoping needs the CSRF header", code == 403, f"got {code}")

    # Coverage: the model proposes, the probe caps. These are the guards that
    # keep the percentage honest, so they are checked without a server at all.
    empty_probe = {"score": 0, "hits": 0, "docs": 0, "workstreams": [], "top": [], "basis": ""}
    raw_round = {
        "ready": False, "assessment": "x",
        "coverage": {"score": 95, "reasons": [], "missing": [], "answer_shape": ""},
        "questions": [{"id": "q1", "question": "Which period?", "why": "w", "kind": "single",
                       "default": "", "options": [
                           {"label": f"o{i}", "detail": "d", "evidence": ["deadbeefdeadbeef"]}
                           for i in range(12)]}] * 9,
        "brief": {"question": "refined", "scope": [], "out_of_scope": [],
                  "evidence_plan": [{"doc_id": "deadbeefdeadbeef", "rel_path": "x", "why": "y"}],
                  "deliverable": "prose", "assumptions": ["a"] * 20},
        "complexity": {"level": "nonsense", "drivers": [], "docs_to_read": 999,
                       "needs_computation": False, "recommended_effort": "turbo"},
        "gaps": [],
    }
    zero = _scope._coerce(raw_round, empty_probe)
    check("a confident score is capped when nothing retrieved",
          zero["coverage"]["score"] <= 15, str(zero["coverage"]["score"]))
    thin_probe = {"score": 24, "hits": 4, "docs": 2, "workstreams": ["financial"], "top": [],
                  "basis": ""}
    thin = _scope._coerce(raw_round, thin_probe)
    check("a two-document evidence base caps coverage well short of confident",
          thin["coverage"]["score"] <= 55, str(thin["coverage"]["score"]))
    check("the model may not run far ahead of what actually retrieved",
          thin["coverage"]["score"] <= thin_probe["score"] + 25)
    check("coverage is displayed, never a gate — a thin round still asks",
          zero["ready"] is False and len(zero["questions"]) == 4)
    check("questions and options are clamped to what a reader can hold",
          len(zero["questions"]) == 4
          and all(len(q["options"]) <= 5 for q in zero["questions"]))
    check("evidence that does not resolve to a document is dropped",
          zero["brief"]["evidence_plan"] == []
          and all(not o["evidence"] for q in zero["questions"] for o in q["options"]))
    check("a skipped question always has a default to fall back on",
          all(q["default"] for q in zero["questions"]))
    check("an unknown enum falls back rather than reaching the model",
          zero["complexity"]["level"] == "moderate"
          and zero["complexity"]["recommended_effort"] == "high")
    for bad in (900, -3):
        r = json.loads(json.dumps(raw_round))
        r["coverage"]["score"] = bad
        got = _scope._coerce(r, {**empty_probe, "score": 80, "docs": 20})["coverage"]["score"]
        check(f"a score of {bad} is clamped into 0-100", 0 <= got <= 100, str(got))

    # The brief is instructions, not a claim: it must not turn up in the
    # citation panel of the answer it produced.
    brief_text = _scope.render_brief(zero["brief"])
    check("the brief is tagged as scope for the analyst",
          brief_text.startswith("<research_brief>") and brief_text.endswith("</research_brief>"))
    check("the brief does not pollute the citation panel",
          _agent.parse_citations(brief_text) == [])

    # A thin corpus should not be answered with the most expensive model.
    deep_covered = _scope.propose_model({"score": 88}, {"level": "deep",
                                                       "recommended_effort": "max"})
    deep_thin = _scope.propose_model({"score": 22}, {"level": "deep",
                                                    "recommended_effort": "max"})
    check("a thin question is proposed a cheaper run than a well-covered one",
          deep_thin["model"] != deep_covered["model"],
          f"{deep_thin['model']} vs {deep_covered['model']}")

    # Model settings: admin-only, and never a model the ledger has no price for.
    code, body, _ = admin.get("/api/status")
    check("status lists the models that may be selected",
          code == 200 and len(body["models"]["available"]) >= 4
          and "scope_max_rounds" in body["models"], str(body.get("models"))[:160])
    code, body, _ = dana.request("PATCH", "/api/settings/models",
                                 {"models": {"analyst": "claude-sonnet-5"}})
    check("an analyst cannot change the models", code == 403, f"got {code}")
    code, body, _ = admin.request("PATCH", "/api/settings/models",
                                  {"models": {"analyst": "gpt-nonexistent"}})
    check("an unpriced model is refused rather than silently mis-billed",
          code == 400 and "unknown" in str(body).lower(), f"got {code}: {str(body)[:160]}")
    code, body, _ = admin.request("PATCH", "/api/settings/models", {"scope_max_rounds": 9})
    check("the number of question rounds is bounded", code == 400, f"got {code}")
    code, body, _ = admin.request(
        "PATCH", "/api/settings/models",
        {"models": {"analyst": "claude-sonnet-5", "scoper": ""}, "scoper_effort": "medium",
         "scope_max_rounds": 3, "complexity_models": {"deep": "claude-opus-5"}})
    check("an admin can change the models", code == 200, f"got {code}: {str(body)[:160]}")
    check("an unset scoper inherits the analyst, which is the cheap default",
          body["scoper"] == "claude-sonnet-5" and body["scoper_configured"] == "",
          str(body)[:200])
    check("the choice is visible to everyone who has to live with it",
          admin.get("/api/status")[1]["models"]["scope_max_rounds"] == 3)
    admin.request("PATCH", "/api/settings/models",
                  {"models": {"analyst": "claude-opus-5"}, "scope_max_rounds": 2})

    # Round two resumes from the stored transcript, and a round can end three
    # ways: the model stopped, it used its last tool turn, or the budget stopped
    # it holding a tool_use nobody answered. All three have to come back as a
    # conversation the API will accept — a failure here only shows up on the
    # second round of a real question, which is the worst place to find it.
    _endings = {
        "the model stopped on its own": [
            {"role": "user", "content": "q"},
            {"role": "assistant", "content": [{"type": "text", "text": "found enough"}]},
        ],
        "the last tool turn was used up": [
            {"role": "user", "content": "q"},
            {"role": "assistant",
             "content": [{"type": "tool_use", "id": "t1", "name": "search_corpus", "input": {}}]},
            {"role": "user",
             "content": [{"type": "tool_result", "tool_use_id": "t1", "content": "{}"}]},
        ],
        "the budget stopped it mid-turn": [
            {"role": "user", "content": "q"},
            {"role": "assistant",
             "content": [{"type": "tool_use", "id": "t1", "name": "search_corpus", "input": {}}]},
        ],
    }
    for _ending, _messages in _endings.items():
        _resumed = _scope._sealed(list(_messages))
        _scope._resume(_resumed, "the user answered: FY2024")
        _roles = [m["role"] for m in _resumed]
        check(f"a round resumes cleanly when {_ending}",
              _roles[-1] == "user"
              and all(_roles[i] != _roles[i + 1] for i in range(len(_roles) - 1))
              and "FY2024" in str(_resumed[-1]["content"]),
              str(_roles))

    # A missing session is a 404, not someone else's brief.
    code, _, _ = admin.get("/api/scope/deadbeefdead")
    check("an unknown scoping session is not found", code == 404, f"got {code}")

    print("\n— setup guide —")
    # Served by the app rather than linked out: it is read while setting up a
    # server that may have no general internet egress.
    code, body, headers = admin.get("/help/sharepoint")
    check("the SharePoint setup guide is served", code == 200, f"got {code}")
    check("the guide is the walkthrough, not a stub",
          isinstance(body, str) and "Application (client) ID" in body
          and "Sites.Selected" in body and "Grant admin consent" in body,
          str(body)[:160])
    check("the guide styles itself from the app stylesheet, so it cannot drift",
          isinstance(body, str) and '/styles.css' in body, "no stylesheet link")
    # Page routes redirect to the login form rather than returning 401 — that is
    # the middleware's contract for anything outside /api/. urllib follows the
    # redirect, so the guide is fenced when what comes back is the sign-in page.
    anon = Client()
    code, landed, _ = anon.get("/help/sharepoint")
    check("the guide needs a session like every other page",
          code == 200 and isinstance(landed, str)
          and "Sign in" in landed and "Sites.Selected" not in landed,
          f"got {code}: {str(landed)[:120]}")

    print("\n— activity log —")
    # The live SSE feed is a ring buffer, so the point of this table is that a
    # failure is still there — and still exportable — after it scrolled past.
    code, body, _ = admin.get("/api/logs?limit=500")
    entries = body.get("entries", []) if code == 200 else []
    check("the activity log is persisted and readable", code == 200 and len(entries) > 5,
          f"got {code} with {len(entries)} entries")
    check("counts are reported per level",
          isinstance(body.get("counts"), dict) and "error" in body["counts"], str(body.get("counts")))

    failure = next(
        (e for e in entries if "corrupt_scan.pdf" in (e.get("message") or "")), None
    )
    check("an extraction failure is logged at error level",
          failure is not None and failure["level"] == "error",
          str(failure)[:200] if failure else "no line mentions the corrupt file")
    ctx = (failure or {}).get("context") or {}
    # The whole reason for the context column: a message says what broke, this says
    # which file, how big, and where in the code — the difference between a report
    # someone can act on and a retyped fragment of one line.
    check("the failure carries the path, extension and traceback",
          ctx.get("rel_path", "").endswith("corrupt_scan.pdf") and ctx.get("ext") == ".pdf"
          and "Traceback" in (ctx.get("traceback") or ""),
          str({k: str(v)[:60] for k, v in ctx.items()}))
    check("the failure is attributed to a source and a job",
          failure and failure.get("source") == "ingest" and failure.get("job_id"),
          str(failure)[:160] if failure else "")
    check("streamed and stored lines share an id, so the browser can dedupe them",
          all(e.get("id") for e in entries[:20]), str(entries[:1])[:160])

    code, body, _ = admin.get("/api/logs?levels=error")
    only_errors = body.get("entries", [])
    check("the error filter returns errors only",
          code == 200 and only_errors and all(e["level"] == "error" for e in only_errors),
          str({e["level"] for e in only_errors}))
    code, body, _ = admin.get("/api/logs?levels=error,warn")
    check("the problems filter returns warnings as well as errors",
          code == 200 and {e["level"] for e in body["entries"]} <= {"error", "warn"},
          str({e["level"] for e in body.get("entries", [])}))
    code, body, _ = admin.get("/api/logs?levels=nonsense")
    check("an unrecognised level is ignored rather than returning nothing",
          code == 200 and len(body["entries"]) > 5, f"{len(body.get('entries', []))} entries")
    code, body, _ = admin.get("/api/logs?q=corrupt_scan")
    check("the text filter narrows to matching lines",
          code == 200 and body["entries"] and all(
              "corrupt_scan" in (e["message"] or "") + json.dumps(e["context"] or {})
              for e in body["entries"]),
          f"{len(body.get('entries', []))} entries")
    code, body, _ = admin.get("/api/logs?source=ingest")
    check("the source filter narrows to one subsystem",
          code == 200 and body["entries"] and all(e["source"] == "ingest" for e in body["entries"]),
          str({e["source"] for e in body.get("entries", [])}))

    # Paging cursors. after_id is what a browser uses to fill the gap after its
    # event stream dropped: lines written while it was down are never streamed, so
    # without it a failure stays invisible until a reload.
    newest_id = entries[0]["id"]
    code, body, _ = admin.get(f"/api/logs?after_id={newest_id - 3}")
    check("after_id returns only newer lines",
          code == 200 and body["entries"] and all(e["id"] > newest_id - 3 for e in body["entries"]),
          str([e["id"] for e in body.get("entries", [])])[:120])
    code, body, _ = admin.get(f"/api/logs?before_id={newest_id}")
    check("before_id pages backwards",
          code == 200 and all(e["id"] < newest_id for e in body["entries"]),
          str([e["id"] for e in body.get("entries", [])])[:120])
    code, body, _ = admin.get(f"/api/logs?after_id={newest_id}")
    check("after_id at the newest line returns nothing rather than everything",
          code == 200 and body["entries"] == [], f"{len(body.get('entries', []))} entries")

    # An oversize context must shrink as an *object*. Slicing the serialised JSON
    # would cut mid-string, and an unparseable context degrades to an opaque blob —
    # losing the traceback exactly when a bug report needs it.
    from app import db as _db

    # Long paths deliberately: a list capped by item count rather than by
    # characters passes a short-path test and still blows the column ten times
    # over, and the fallback for "still too big" is a stub with no traceback in it.
    _db.log_record(
        "error", "oversize context probe", source="ingest",
        context={
            "rel_path": "deep/folder/enormous.pdf",
            "traceback": "Traceback (most recent call last):\n" + "  File 'x.py', line 1\n" * 3000,
            "paths": [
                f"/corpus/{'deeply_nested_folder_'*8}{i}/Consolidated statements {i}.pdf"
                for i in range(500)
            ],
        },
    )
    code, body, _ = admin.get("/api/logs?q=oversize%20context%20probe")
    probe = (body.get("entries") or [{}])[0]
    ctx = probe.get("context")
    check("an oversize context is still stored as parseable structure",
          isinstance(ctx, dict) and "raw" not in ctx
          and ctx.get("rel_path") == "deep/folder/enormous.pdf",
          str(ctx)[:200])
    check("an oversize context is shortened rather than abandoned",
          isinstance(ctx, dict) and not ctx.get("truncated")
          and str(ctx.get("traceback", "")).startswith("Traceback (most recent call last)")
          and 0 < len(ctx.get("paths") or []) < 500,
          str({k: str(v)[:40] for k, v in (ctx or {}).items()}))
    check("an oversize context fits the column it is stored in",
          isinstance(ctx, dict) and len(json.dumps(ctx)) <= _db.LOG_CONTEXT_MAX,
          f"{len(json.dumps(ctx)) if isinstance(ctx, dict) else 0} chars")
    # And the shapes the app really produces are not shortened at all.
    real_tb = "Traceback (most recent call last):\n" + "  File 'extract.py', line 5\n" * 12
    _db.log_record("error", "realistic failure probe", source="ingest",
                   context={"rel_path": "1.1.6/dhig_FinancialStatements.xls", "ext": ".xls",
                            "size_bytes": 2411520, "exc_type": "InvalidFileException",
                            "traceback": real_tb})
    code, body, _ = admin.get("/api/logs?q=realistic%20failure%20probe")
    real_ctx = (body.get("entries") or [{}])[0].get("context") or {}
    check("a real extraction context is stored whole, traceback and all",
          real_ctx.get("traceback") == real_tb and real_ctx.get("size_bytes") == 2411520,
          str({k: str(v)[:40] for k, v in real_ctx.items()}))

    code, report, _ = admin.get("/api/logs/export?levels=error")
    check("the log exports as plain text", code == 200 and isinstance(report, str), f"got {code}")
    if isinstance(report, str):
        check("the export names its filter and its counts",
              "DD Library activity log" in report and "levels=error" in report, report[:200])
        check("the export inlines the traceback, not a JSON blob of one",
              "corrupt_scan.pdf" in report and "traceback:" in report
              and "Traceback (most recent call last)" in report,
              report[:400])
        check("the export reads oldest first",
              report.index("Oldest first") < report.index("corrupt_scan.pdf"), "order")

    carol = Client()
    code, _, _ = admin.post("/api/users",
                            {"username": "carol", "password": "analyst-pass-7", "role": "analyst"})
    _, carol_login, _ = carol.post("/api/login",
                                   {"username": "carol", "password": "analyst-pass-7"})
    carol.csrf = (carol_login.get("user") or {}).get("csrf")
    code, body, _ = carol.get("/api/logs?levels=error")
    check("an analyst can read the log, so they can report a failure",
          code == 200 and "entries" in body, f"got {code}")
    code, body, _ = carol.post("/api/logs/clear", {"levels": []})
    check("an analyst cannot clear the log", code == 403, f"got {code}")

    before = admin.get("/api/logs")[1]["counts"]
    code, body, _ = admin.post("/api/logs/clear", {"levels": ["error"]})
    check("an admin can clear one level", code == 200 and body["removed"] == before["error"],
          f"removed {body.get('removed')} of {before['error']}")
    check("clearing errors leaves the rest of the log alone",
          body["counts"]["error"] == 0 and body["counts"]["info"] == before["info"],
          str(body.get("counts")))
    code, body, _ = admin.post("/api/logs/clear", {"levels": []})
    check("an admin can clear the whole log", code == 200 and body["counts"]["total"] == 0,
          str(body.get("counts")))

    print("\n— audit trail —")
    code, body, _ = admin.get("/api/audit")
    actions = {e["action"] for e in body["entries"]}
    expected = {"login.success", "login.failure", "user.create", "apikey.add", "apikey.delete",
                "archive.upload", "archive.extract", "storage.reset_index", "log.clear"}
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
    print(f"(data dir {_DATA})")
    return 1 if FAILURES else 0


if __name__ == "__main__":
    raise SystemExit(main())
