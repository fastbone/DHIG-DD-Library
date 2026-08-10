# DD Library

A due-diligence assistant over a local document library: upload or point it at a
data room of PDFs, spreadsheets and decks, watch it get indexed, then ask
questions and generate deliverables — with a citation on every claim.

Your documents stay on your machine. Only the excerpts the model actually needs
are sent to the Claude API.

```bash
cp .env.example .env      # set DD_SECRET_KEY
docker compose up -d      # → http://127.0.0.1:8000
```

or without Docker:

```bash
pip install -r requirements.txt
./run.sh                  # → http://127.0.0.1:8000
```

The first visit asks you to create an administrator. Then: **Admin → API keys**,
**Corpus → upload a .zip**, **Indexing → Run sweep**, **Ask**.

---

## The idea in one paragraph

1.3 GB of Office files is roughly 20–40 million tokens of text: 20–40× a 1M
context window, and far too expensive to re-read per question. So the assistant
does not hold the library. Instead it holds a **map** of it — one compact
catalogue card per document, always in context, behind a 1-hour prompt-cache
breakpoint — and **reads on demand** through search, page-level reads, and
Python over the original workbooks. The map is what makes it aware of every
file; the tools are what make it accurate about their contents.

The economics are the point. A 5,000-document map is ~250K tokens; at cache-read
rates that is about **$0.12 per turn** instead of $1.25, and a question that
reads 50K tokens of actual pages lands around **$0.40–1.00**. Indexing the whole
library once, on Claude Haiku 4.5, costs roughly **$10–25**.

---

## What it does

**Corpus tab** — drop a `.zip` on the upload panel, or point it at a folder. It
walks the tree, extracts text into a greppable mirror, and records a citation
anchor for every page, slide and sheet. Re-running only processes what changed.

Uploads stream to disk and extraction is deliberately paranoid — path traversal,
absolute paths, symlinks, device nodes, member-count explosions and compression
bombs are refused rather than trusted, and each refusal is logged. `.zip`,
`.tar`, `.tar.gz`, `.tar.bz2` and `.tar.xz` are accepted; an archive can extract
and ingest in one step.

| Source | Extracted as | Anchors |
|---|---|---|
| PDF | text per page, optional OCR for scans | `p14` |
| PPTX | markdown per slide **including speaker notes** | `slide7` |
| XLSX / XLSM | one block per sheet: computed values **and** formulas | `Summary!A1:H240` |
| DOCX | sectioned text plus tables | `sec3` |
| CSV / TSV / TXT / MD / JSON | chunked text | `rows501-1000` |

It also handles the mess a real data room is made of. Byte-identical files
collapse into one content-addressed document with every path it was filed at
recorded. Near-duplicate version families (`_v3`, `_v3_FINAL`, `_v3_FINAL_JD`)
are detected and grouped, so the assistant can tell you two documents disagree
instead of silently picking one.

**Indexing tab** — runs the sweep: one structured catalogue card per document
(title, type, workstream, parties, period, key figures, two-sentence summary,
flags like `draft` / `unsigned` / `scanned`). Live progress, a running cost
meter, the activity log, and a preview of the corpus map the analyst will
actually see — including what it costs per turn cached vs uncached.

**Ask tab** — streaming answers with the reasoning summary, the tool trace
(every search, read and computation), and a citations panel. Clicking any
citation opens the source document at that exact page. With verification on,
each cited claim is re-checked against its cited span by a second model and
badged `supported` / `partial` / `unsupported`.

**Deliverables tab** — Word, Excel, PowerPoint and Markdown documents the
analyst generated, plus the question log: every question, its citations, its
verdicts and its cost.

**Admin tab** (administrators only) — four things:

- **API keys.** Paste an Anthropic key and it is encrypted with the instance key
  and stored; the browser only ever sees a label and the last four characters.
  One key is active at a time, each can be liveness-tested (a model lookup, so
  it costs nothing), and with no key stored the app falls back to
  `ANTHROPIC_API_KEY`.
- **Accounts.** Create analysts and administrators, promote, disable, reset
  passwords, delete. Disabling or changing a password ends that user's sessions
  immediately. The last administrator cannot be demoted, disabled or deleted.
- **Data folder.** What is on disk, broken down by area, with the corpus folders
  the app knows about and how many indexed documents each accounts for. Reclaim
  operations: delete generated documents, clear catalogue cards (keeps the text,
  requires a new sweep), reset the index (drops the text mirror too), purge
  documents whose files are gone, vacuum the database, delete an extracted
  corpus folder.
- **Audit log.** Every sign-in, failed sign-in, account change, key change,
  upload, extraction, question and storage operation, with actor and timestamp.

## Accounts and access

Two roles. `analyst` can ingest, upload, ask and generate; `admin` additionally
manages accounts, keys and storage. Sessions are server-side (revocable, sliding
expiry, stored by token digest) in an HttpOnly `SameSite=Lax` cookie, and every
state-changing request must echo a per-session CSRF token in an `X-CSRF-Token`
header. Passwords are scrypt-hashed. Failed sign-ins are throttled per
username+IP, then locked out.

There is no password reset by mail. An administrator resets passwords from the
Admin tab; a locked-out administrator is recovered by setting `DD_ADMIN_USER`,
`DD_ADMIN_PASSWORD` and `DD_ADMIN_RESET_PASSWORD=1` and restarting.

## The analyst's tools

| Tool | Why it exists |
|---|---|
| `search_corpus` | BM25 (SQLite FTS5) over every page/slide/sheet, returning `doc_id:anchor` citations |
| `read_document` | Read a specific unit or page through a document |
| `document_card` | The card plus the full anchor list, to see a document's shape before reading it |
| `list_documents` | Page the catalogue when the map is a rollup rather than a full listing |
| `run_python` | **Load the original workbook and compute.** Never eyeball financials out of extracted text |
| `create_deliverable` | Emit docx/xlsx/pptx/md from a declarative spec, citations intact |

`run_python` matters more than it looks. Extracted text loses number formatting,
merged cells and formulas — so for anything quantitative the system prompt
requires the model to open the real file with `pandas`/`openpyxl` and show its
work. A helper module is injected: `dd.path(doc_id)`, `dd.text(doc_id)`,
`dd.find("revenue model")`.

---

## Install and run

### Docker (recommended)

```bash
cp .env.example .env
python3 -c "import secrets; print(secrets.token_urlsafe(48))"   # → DD_SECRET_KEY
docker compose up -d
```

Two volumes: `dd-data` holds the index, text mirror, uploads and generated
documents (back this up), and `./corpus` is bind-mounted read-only at `/corpus`
if you want to index a data room straight off the host rather than uploading it.
For OCR, build with `INSTALL_OCR=true` (adds ~250 MB of Tesseract) and set
`DD_OCR=1`.

The compose file publishes on `127.0.0.1` only, and hardens the container
because `run_python` executes model-authored code: read-only root filesystem,
tmpfs `/tmp`, all capabilities dropped, `no-new-privileges`, a PID limit and a
memory limit. To reach it from another machine, put a TLS-terminating reverse
proxy in front, set `DD_COOKIE_SECURE=1`, and leave the published port bound to
localhost.

**Keep `DD_SECRET_KEY`.** It encrypts stored API keys; if it changes, they cannot
be decrypted and have to be re-added (the app tells you rather than failing
mysteriously).

### Without Docker

Python 3.11+.

```bash
pip install -r requirements.txt
export DD_SECRET_KEY=...                 # optional; otherwise data/secret.key
export ANTHROPIC_API_KEY=sk-ant-...      # optional; or add a key in Admin
./run.sh
```

No data room handy? Generate a synthetic one:

```bash
python3 tools/make_sample_corpus.py ./sample-dataroom
```

It contains audited accounts, a model with formulas, a deck with revealing
speaker notes, a contract with a change-of-control clause, an exact duplicate
filed twice, and a near-duplicate version pair — enough to exercise every path.
All figures are invented.

### Configuration

Everything is env-overridable; defaults in `app/config.py`.

| Variable | Default | Notes |
|---|---|---|
| `DD_DATA_DIR` | `./data` | Index, text mirror, generated files. Disposable — rebuildable from the corpus |
| `DD_CORPUS_ROOT` | *(set in the UI)* | Persisted to `data/settings.json` once set |
| `DD_ANALYST_MODEL` | `claude-opus-5` | The reasoning loop |
| `DD_CARDER_MODEL` | `claude-haiku-4-5` | The bulk indexing sweep |
| `DD_VERIFIER_MODEL` | `claude-haiku-4-5` | Citation checking |
| `DD_ANALYST_EFFORT` | `high` | Also selectable per question in the UI |
| `DD_EXTRACT_WORKERS` | cores − 1 | Extraction threads |
| `DD_CARD_CONCURRENCY` | `8` | Concurrent indexing calls |
| `DD_MANIFEST_CHAR_BUDGET` | `1_000_000` | Above this the map degrades to compact, then to a rollup |
| `DD_OCR` | `0` | OCR pages with no text layer. Slow; worth it for scanned data rooms |
| `DD_ENABLE_PYTHON` | `1` | See the security note below |
| `DD_HOST` / `DD_PORT` | `127.0.0.1:8000` | |
| `DD_SECRET_KEY` | *(generated into `data/secret.key`)* | Encrypts stored API keys. Set explicitly in Docker |
| `DD_ADMIN_USER` / `DD_ADMIN_PASSWORD` | — | Creates the first administrator on start; otherwise created in the browser |
| `DD_ADMIN_RESET_PASSWORD` | `0` | Set to `1` for one restart to recover a locked-out administrator |
| `DD_SESSION_TTL_HOURS` | `12` | Sliding session expiry |
| `DD_COOKIE_SECURE` | `0` | Set to `1` when serving over HTTPS |
| `DD_LOGIN_MAX_ATTEMPTS` / `DD_LOGIN_LOCKOUT_SECONDS` | `8` / `300` | Sign-in throttle |
| `DD_BROWSE_ROOTS` | extraction root + `/corpus` + cwd | Directories the folder picker may descend into |
| `DD_MAX_UPLOAD_MB` | `4096` | Per-archive upload ceiling |
| `DD_MAX_EXTRACT_GB` | `20` | Total uncompressed size ceiling |
| `DD_MAX_ARCHIVE_MEMBERS` | `100000` | Member-count ceiling |
| `DD_MAX_COMPRESSION_RATIO` | `200` | Above this an archive is treated as a decompression bomb |

Effort is worth a sweep of its own on your questions: `low` and `medium` are
strong on Claude Opus 5 and much cheaper, while `xhigh` suits the hardest
multi-document reconciliations.

---

## Security and confidentiality

Read this before pointing it at a live deal.

- **`run_python` executes model-generated code.** It runs in a subprocess with a
  scrubbed environment, a temp working directory and a timeout — but it is *not*
  a sandbox: it can read any file the server user can read, and reach the
  network. The container hardening in `docker-compose.yml` is what makes this
  acceptable; run it there, or on a dedicated VM, or set `DD_ENABLE_PYTHON=0`
  and accept that the assistant can no longer compute over your spreadsheets.
- **Anyone who can sign in can read the whole library.** Roles separate
  administration from use, not documents from documents — the corpus map is a
  single blob in the system prompt, so every user learns that every document
  exists. If your data room has folders only some people may see, run one
  instance per access tier with separate data volumes.
- **`/api/documents/{id}/original` serves any indexed file** to any signed-in
  user, and the folder picker can descend into `DD_BROWSE_ROOTS` (administrators
  only). Both are intended; keep the roots narrow.
- **Bind to localhost.** The default is `127.0.0.1`. Serving it publicly means a
  TLS-terminating reverse proxy in front, `DD_COOKIE_SECURE=1`, and
  `DD_FORWARDED_ALLOW_IPS` set to that proxy — not `DD_HOST=0.0.0.0` on its own.
- **Uploads land on the server's filesystem.** Extraction refuses traversal,
  absolute paths, links, special files, oversized and over-ratio archives, and
  writes only under `data/uploads/extracted`. The limits are configurable; the
  defaults are deliberately not generous.
- **Back up `DD_SECRET_KEY` with the data volume.** Without it the stored API
  keys in that volume are unrecoverable.
- **Confirm data-retention terms with your Anthropic account team** before
  uploading privileged material, and note that Claude Fable 5 requires 30-day
  retention — stay on Claude Opus 5 if you are contractually zero-retention.
- Excerpts, not whole files, leave the machine: the indexing sweep sends up to
  ~24K characters per document, and answering sends the pages the model chose to
  read. Everything sent is recorded in the question log.

---

## Verifying it works

Three suites, none of which spend a token or touch your real index — each uses
its own temporary data directory.

```bash
python3 tools/api_smoke.py            # 50 checks: auth, CSRF, keys, uploads, storage, audit
python3 tools/ui_smoke.py             # 25 checks: the browser front end, end to end
tools/container_check.sh              # 6 checks: the container's runtime constraints
```

`api_smoke.py` drives the real HTTP surface with a cookie-aware client:
unauthenticated access is refused, first-run bootstrap works once and then
closes, a missing or wrong CSRF token is rejected, failed sign-ins throttle,
analysts cannot reach admin routes, disabling a user kills their session
immediately, a stored API key round-trips through encryption and is never echoed
back, an archive with a `../` member extracts its safe files and refuses the
rest, browsing outside the permitted roots is refused, and every privileged
action lands in the audit log.

`ui_smoke.py` ingests the sample corpus, stamps synthetic cards, replaces the
agent with a scripted event stream, and drives a real browser: sign-in,
streaming answer, reasoning panel, tool trace, citation chips that open the
source at the cited page, verdict badges, artifact download, the admin surfaces,
and a drag-and-drop archive upload through to extraction. Screenshots land in
`/tmp`. Requires `pip install playwright` and a Chromium binary (set
`DD_CHROMIUM` if it is not at `/opt/pw-browsers/chromium`).

`container_check.sh` recreates the image's constraints without Docker — a
read-only application directory, a separate writable data directory, the
environment-bootstrapped administrator — and confirms the app starts, writes
nothing into its own source tree, puts everything in the data volume, and can
still run its Python tool. Pass `--user` (as root) to also drop to an
unprivileged account.

---

## Layout

```
app/
  config.py       settings, supported formats, workstream taxonomy, browse roots
  db.py           SQLite schema (documents, units + FTS5, occurrences, jobs,
                  qa_log, users, sessions, api_keys, archives, audit)
  security.py     scrypt password hashing, session tokens, AES-GCM at rest
  auth.py         users, sessions, roles, login throttle, route guards
  credentials.py  API key storage and Anthropic client construction
  extract.py      per-format extraction into the anchored text mirror
  ingest.py       walk → hash → extract → index → duplicate detection
  uploads.py      archive upload and hardened extraction
  storage.py      disk usage and the reclaim operations
  manifest.py     the sweep (one card per document) and the corpus map
  search.py       BM25 search, catalogue browsing, corpus statistics
  tools.py        the analyst's tool definitions and handlers
  agent.py        the streaming tool loop, system prompt, citation parsing
  verify.py       re-read each cited span and judge the claim
  docgen.py       docx / xlsx / pptx / md from a block spec
  pricing.py      token accounting and the cost meter
  events.py       in-process pub/sub behind the SSE progress stream
  server.py       FastAPI routes and the access-control middleware
web/              single-page UI plus the login page, no build step
tools/            sample corpus, API smoke test, UI smoke test, container check
Dockerfile        non-root, read-only /app, /data volume
docker-compose.yml  hardened runtime, localhost-published
```

### Design notes worth knowing before you change things

**The prompt prefix is load-bearing.** Order is `tools` → instructions → corpus
map, with the cache breakpoint on the map. Tool definitions are kept in a fixed
sorted order and the map is rebuilt only after a sweep, because any byte change
in that prefix throws away the cache that makes the whole approach affordable.

**Documents are content-addressed** (`sha256[:16]`). This is what makes ingest
resumable and idempotent, and it is why exact duplicates cannot exist as
separate rows — extra paths live in the `occurrences` table.

**Near-duplicate detection is blocked, not exhaustive.** Candidate pairs come
from version-stripped filenames and identical first units; only those pairs get
a Jaccard comparison. A renamed near-duplicate with a different first page will
be missed. That is the documented trade-off for staying linear across thousands
of documents.

**Writes are serialised.** SQLite allows one writer; ingest is multi-threaded.
All writes go through a single lock in `db.py` — without it, an explicit
transaction in one thread races autocommit writes in another and rows are
silently lost. `db.unit_count_mismatches()` exists to catch exactly that, and
ingest reports it.

**Access control lives in one middleware**, not in per-route dependencies, so a
new route is protected by default rather than by remembering. Admin-only routes
add `Depends(auth.require_admin)` on top. Public paths are an explicit
allow-list in `server.py`.

**`hashlib.scrypt` needs an explicit `maxmem`.** OpenSSL applies a 32 MiB
default ceiling and raises "memory limit exceeded" when the parameters approach
it — which surfaces as a 500 on the login route, on some hosts only. The cost
parameters are stored inside each hash, so they can be raised later without
invalidating existing passwords.

## Where to take it next

- **Semantic search** — everything here is keyword + map. If you find questions
  that BM25 misses, add embeddings over the same units (Anthropic has no
  embeddings endpoint; use a provider or a local model) and merge the rankings.
- **Sub-agents per workstream** — fan a broad question out across financial /
  legal / commercial in parallel and synthesise, rather than one serial loop.
- **Batch the sweep** — the Batch API halves indexing cost; live progress is
  what it trades away.
- **Multi-user** — real auth, per-user access tiers, and a map filtered per tier.
