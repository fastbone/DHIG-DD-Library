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

Which is also why no question goes straight to the analyst. A vague question
costs a full run to discover it was the wrong one, so it is **scoped first**: the
corpus is read, a few grounded clarifying questions come back, and you approve an
editable brief — along with an honest figure for how much of what the question
needs the data room actually holds. A scoping round costs cents. See *Scoping a
question*.

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

Or connect a **SharePoint Online document library** and let it mirror itself. An
administrator enters the site URL and an Entra app registration once, and the
library is copied into the data folder and indexed like any other folder —
manually, or on an interval. See *Connecting a SharePoint library* below.

| Source | Extracted as | Anchors |
|---|---|---|
| PDF | text per page, optional OCR for scans | `p14` |
| PPTX | markdown per slide **including speaker notes** | `slide7` |
| XLSX / XLSM | one block per sheet: computed values **and** formulas | `Summary!A1:H240` |
| XLS (Excel 97-2003) | one block per sheet, computed values | `Summary!A1:H240` |
| DOCX | sectioned text plus tables | `sec3` |
| CSV / TSV / TXT / MD / JSON | chunked text | `rows501-1000` |

Spreadsheets are routed by what the file **is**, not what it is called. Data-room
extensions are unreliable in both directions — modern workbooks saved as `.xls`,
Excel 97-2003 workbooks renamed `.xlsx`, and reporting-system HTML tables handed
out as `.xls` — so the container is identified from its bytes and each of those
three is indexed rather than failed. Legacy `.xls` files carry one inherent
limitation: the format stores computed values only, so there is no formula pass
for them and the extracted text says so explicitly, rather than leaving the model
to read the absence as "this workbook has no formulas". `.xlsb` (binary) is not
supported and is skipped rather than failed.

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

**The activity log is kept, not just streamed.** Every line an ingest, sweep,
upload or sync produces is written to the index, so a failure is still findable
after it scrolled past and after a restart. Filter to **problems** (warnings and
errors) or **errors only** in one click, narrow by source or by text, and expand
any line with a `▸ detail` marker to see the structured facts behind it — the
failing path relative to the data room, its extension and size, the exception
type, and the traceback.

*copy for a bug report* / *download* produce a plain-text report of whatever the
filter currently shows: oldest first, tracebacks inlined, with a header naming
the filter and the counts. That output is meant to be pasted straight into an
issue — the one-line message says what broke, and the context says which file and
where in the code, which is the difference between a report someone can act on
and a retyped fragment.

Reading and exporting the log needs only a sign-in, so an analyst who hits a
failure can report it. Clearing it is destructive and therefore admin-only; a
clear honours the level filter, so *errors only* → *clear* drops the failures you
have just reported and leaves the rest of the history intact.

**Search tab** — the corpus's own full-text index, for people. BM25 over every
extracted page, slide and sheet, returning **passages** rather than documents: one
hit per page/slide/sheet with the matched terms highlighted, and a click opens the
same reader a citation does, at that same anchor. Filter by workstream or file type.

This index is not new — it is what the analyst's `search_corpus` tool has always
run. What was missing is that only the model could reach it, so "which documents
mention indemnity" cost a question: thirty seconds and ~$0.40 for a lookup that is
instant and free. The same box is mounted in three places, because that is where
the question gets asked: its own tab, above the Documents table on the Corpus tab,
and beside the question box on the Ask tab as the cheap alternative to asking.

Search complements the catalogue filter rather than replacing it: the Documents
filter matches names, titles, parties and summaries — the *card* — while this
matches the text inside the files.

**Ask tab** — ask in whatever words you have; the question is scoped before it
is answered. See *Scoping a question* below. Then streaming answers with the
reasoning summary, the tool trace (every search, read and computation), and a
citations panel. Clicking any citation opens the source document at that exact
page. With verification on, each cited claim is re-checked against its cited
span by a second model and badged `supported` / `partial` / `unsupported`.

**Deliverables tab** — Word, Excel, PowerPoint and Markdown documents the
analyst generated, plus the question log: every question, its citations, its
verdicts and its cost.

**Admin tab** (administrators only) — six things:

- **Models.** Which model fills each of the four roles — analyst, scoper, carder,
  verifier — the default effort, how many rounds of clarifying questions a
  question gets, and which model the scoper proposes for a simple, moderate or
  deep question. Only models the cost ledger has a price for can be selected; a
  role pinned by an environment variable is shown but not editable.
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
- **Weekly spending budgets.** Two caps per account — one for questions, one
  for indexing — each set to a number, `unlimited`, or left to inherit the
  instance default. Every account row shows what it has spent this week against
  each cap. Scoping is charged to the question budget. See *Weekly spending
  budgets* below.
- **Audit log.** Every sign-in, failed sign-in, account change, key change,
  upload, extraction, sync, question, budget change and storage operation, with
  actor and timestamp.

Connected libraries are managed from the Corpus tab but are administrator-only
for the same reason keys are: connecting one makes its contents readable to
everyone who can sign in.

## Scoping a question

The expensive part of due diligence is not answering a question, it is answering
the wrong one. So nothing goes straight to the analyst. A question you type is
first read against the corpus by the **scoper**, which comes back with a few
clarifying questions and then a research brief you can edit.

```
you type            →  "How risky is this deal?"
probe (free)        →  BM25 over the raw question, a provisional coverage figure
scope round         →  the corpus is searched, then: coverage, questions, a draft brief
you answer          →  clickable options, "something else", or skip any of them
scope round 2       →  only if it is still unsure; hard cap, then the brief is forced
the brief           →  editable, with the model and effort it proposes for the run
you approve         →  the analyst runs, on the brief, at the model you chose
```

**The questions are grounded, not generic.** The scoper searches before it asks,
and every option has to name something it actually found — a workstream, a
period, a document — and carry its `doc_id`. Options whose documents do not
resolve are dropped server-side before they reach the browser, because a
plausible-looking option for a document that does not exist is worse than no
option at all. The citations inside a question are clickable: you can open the
source before deciding.

**Every question has a default,** so *skip* is always a well-defined action
rather than a hole, and *Skip all — use your judgement* still produces a runnable
brief. Whatever was assumed instead of asked is recorded in the brief's
assumptions, and the analyst states them as caveats.

**Coverage — can this be answered at all?** Every round reports a percentage:
*the share of what this question needs that the data room appears to contain.*
It is deliberately not called a success rate: it is a claim about evidence, which
is what the app can actually measure, and what a DD reader needs to know.

It is computed in two layers, and the second one caps the first:

- a **free probe** — BM25 over the raw question, no API call — counts how many
  distinct documents and workstreams the question's own words reach;
- the **scoper's reading**, which rides on the round it was going to emit anyway,
  so it costs nothing extra, and can tell "twelve documents mention the customer"
  from "twelve documents contain the contract terms this needs".

A model scoring itself drifts upward, so the ceiling is mechanical and not
promptable: nothing retrieved caps the score at 15, a two-document evidence base
at 55, and the model may never run more than 25 points ahead of the probe. Below
70 the brief lists what is missing; below 40 it says so plainly and the run
button reads **Run anyway**.

Coverage never blocks a run and never ends the loop — a gate would only teach the
model to score whatever gets it through. And on a thin question the missing list
is the point: *"audited FY2025 accounts — would settle whether the FY25 figures
are management's or audited — searched: `audit report 2025`, `FY25 auditor`"* is
a document request you can send to the seller.

**Which model runs it.** The scoper also judges how hard the brief is — how many
documents it implies, whether figures have to be computed — and proposes a model
and an effort accordingly, preselected on the brief with an estimated cost. Thin
coverage drops the proposal a tier, because spending the strongest model on a
question the corpus cannot answer is exactly the waste this feature exists to
prevent. Change either dropdown before approving; the defaults and the
complexity→model mapping are set in Admin → Models.

**What it costs.** Roughly $0.15–0.25 per round, against a thirty-turn analyst
run at high effort. That number depends on one implementation detail: the scoper
sends the analyst's *exact* cached prefix — same tool array, same system blocks —
so it reads the corpus map at 0.1× instead of writing its own copy at 2×, and
leaves the cache warm for the run that follows. Its read-only tool subset is
enforced at dispatch rather than by handing it a different tool array, and its
directive rides in the first user turn, after the cache breakpoint. Changing
either would fork the cache and make a scoping round cost more than the answer it
prefaces; `tools/api_smoke.py` asserts the prefix directly for that reason.

**What is kept.** Each round is stored in `scope_rounds` — the questions, the
answers, the brief, the coverage score, and the transcript the next round
rehydrates from. Once the brief is run, every row of the session is stamped with
the answer's `qa_id`, so the audit trail runs both ways: from an answer back to
how its question was framed, and from a scoping session forward to what it
produced. The transcript stays on the server and never crosses the wire — it
contains document text and is replayed into a model whose output becomes the
analyst's instructions, so a client-supplied one would be an injection path
straight into the brief. The browser holds only the session id, in
`sessionStorage`, which is enough to restore a half-answered round after a
reload.

## Connecting a SharePoint library

The app mirrors the library to disk and then treats it as an ordinary corpus
folder. Files are cached in full rather than as text alone, because two things
need the original bytes: opening a citation at its source, and `run_python`
computing over the real workbook instead of eyeballing extracted text.

The click-by-click version of what follows — including the two routes for the
per-site grant and what each failure message means — is served by the app itself
at **`/help/sharepoint`**, linked from the *Connect a library* form. It is a page
rather than a link out because it gets read while setting up a server that may
have no general internet egress.

**In Entra ID**, once per tenant:

1. Register an application (any name, no redirect URI needed — this is app-only
   auth, so no user ever signs in to it).
2. Add an **application** permission, not a delegated one, and grant admin
   consent. **`Sites.Selected` is the right choice**: it grants nothing until an
   administrator assigns the app to a specific site, so the blast radius is one
   library. `Sites.Read.All` would hand the app every site in the tenant.
3. Grant the app read access to the one site (`Grant-PnPAzureADAppSitePermission`
   in PnP PowerShell, or `POST /sites/{id}/permissions` in Graph).
4. Create a client secret and copy it — Entra shows it once.

**In DD Library**, Corpus → SharePoint libraries → *Connect a library*: paste the
site URL (as copied from the browser — a URL pointing at a folder inside the
library is fine), the directory and application IDs, and the secret. The
connection is tested immediately, so a wrong secret or a missing site permission
is reported there and then.

**Every run is kept.** Each connection has a *detail* button (*watch* while a sync
is running) opening the run history: what transferred, what was left unchanged,
what was deleted, how long it took, how much moved — and **the list of files that
actually changed**, which is the question someone has on a Monday morning and
which the summary line cannot answer. Indexing is reported separately from
transferring, because "3 files copied" and "2 new documents indexed" are different
facts and both get asked about.

A run that is still going shows the same view live, driven by the event stream
rather than polling: the files in flight with their progress, the transfer rate and
an ETA. A failed run keeps its reason and the paths that failed, so a bad night is
diagnosable the next morning rather than being a red tag with no detail. The last
`DD_SYNC_RUNS_KEEP` runs per connection are retained.

Then *sync now*, or set an interval. What happens on each sync:

- the library's size is checked first: one over `DD_MAX_SYNC_GB` is refused
  outright, and so is one whose *not yet mirrored* bytes would not fit the free
  space — what the mirror already holds is credited, or a library that only just
  fitted the first time could never be synced again;
- only the eleven indexable extensions are fetched by default — a data room is
  full of `.msg`, images and video that ingest would ignore anyway;
- deletions are mirrored, bounded by `DD_MAX_SYNC_DELETE` so a connection pointed
  at the wrong library cannot empty the mirror in one run;
- the mirror is then indexed, and documents whose files disappeared are dropped
  from the catalogue.

The client secret is encrypted at rest with `DD_SECRET_KEY` and is never returned
to the browser — editing a connection leaves the field blank, meaning "keep it".
It reaches the sync engine through its environment, never a config file and never
the command line, which `ps` would expose to anything else on the host.

Two things worth knowing: the app authenticates as itself, not as the person
asking, so **SharePoint's own per-user permissions do not carry into DD Library** —
anyone who can sign in can read anything that was synced. And Entra client secrets
expire; when one does, syncs fail with `invalid_client` until it is replaced under
*edit*.

## Weekly spending budgets

Every paid API call is attributed to whoever caused it and written to a ledger,
so "what did this cost and who spent it" is answerable from disk rather than from
a counter that dies with the process. On top of that ledger sit two weekly caps
per account:

| Budget | Covers | Typical size |
|---|---|---|
| **questions** | Asking in the Ask tab, and the verifier that re-checks each citation | $0.40–1.00 per question |
| **indexing** | The sweep that writes one catalogue card per document | $10–25 for a whole library, once |

They are separate because the two are nothing alike. A single pooled figure would
have to be either too small to sweep with or too large to be a limit on
questions. An analyst who never sweeps can be left at 0 for indexing; the
administrator who does the sweeping needs a cap that a full library fits inside,
or `unlimited`.

Each cap is one of three things:

- **a number** — dollars per week. `0` means this account cannot spend at all.
- **`unlimited`** — no cap. This is what you give the administrator who runs
  sweeps, or a lead who should never be interrupted.
- **`default`** — inherit `DD_WEEKLY_BUDGET_ASK_USD` / `DD_WEEKLY_BUDGET_INDEX_USD`.
  Both default to unlimited, so **adding this feature changes nothing until
  someone sets a number.**

**The week runs Monday 00:00 to Monday 00:00**, server time. A rolling window
would be fairer but has no reset anyone can name, and "when do I get my budget
back" is the first question after being stopped. The Admin tab states the next
reset in your own timezone.

**What happens at the limit.** A question is refused *before* it starts if the
week's allowance is already gone — nothing is spent to find that out. A question
already running is checked between turns, which is the only point where stopping
does not throw away work already paid for; the partial answer and its citations
are kept and marked as stopped early, and verification is skipped rather than
spending further. A sweep is refused before it starts and stops between
documents, which is safe: the cards already written are kept and a later run
skips them, so it resumes rather than restarts.

**One overrun a week.** An answer abandoned halfway has spent money and produced
nothing, so the first time in a week that a question would be cut off, the
account may exceed its question budget by `DD_BUDGET_GRACE_PCT` (10% by default)
to finish that one answer. It is claimed at the moment it is needed, logged and
audited, and stays claimed until Monday — so the next answer that runs out stops
where it stands. Sweeps get no overrun and need none.

Everyone sees their own position: when a cap applies, the header carries a
`this week $3.40 / $4.00` pill that turns amber near the limit, so the number is
not something you first learn by being refused.

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
work (`pandas.read_excel` covers legacy `.xls` too, via the same `xlrd` the
extractor uses). A helper module is injected: `dd.path(doc_id)`,
`dd.text(doc_id)`, `dd.find("revenue model")`.

---

## Install and run

### Docker (recommended)

```bash
cp .env.example .env
python3 -c "import secrets; print(secrets.token_urlsafe(48))"   # → DD_SECRET_KEY
docker compose up -d
```

Three volumes: `dd-data` holds the index, text mirror, uploads and generated
documents (back this up); `./corpus` is bind-mounted read-only at `/corpus` if
you want to index a data room straight off the host rather than uploading it;
and `dd-inbox` is a writable drop point at `/inbox` for external feeds — see
*Feeding the corpus from outside the app*. All three are browsable from the
folder picker. For OCR, build with `INSTALL_OCR=true` (adds ~250 MB of
Tesseract) and set `DD_OCR=1`.

The compose file publishes on `127.0.0.1:8412` only, and hardens the container
because `run_python` executes model-authored code: read-only root filesystem,
tmpfs `/tmp`, all capabilities dropped, `no-new-privileges`, a PID limit and a
memory limit. To reach it from another machine, put a TLS-terminating reverse
proxy in front, set `DD_COOKIE_SECURE=1`, and leave the published port bound to
localhost — see *Deploying to a host* below.

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
| `DD_SCOPER_MODEL` | *(the analyst's)* | Question refinement. Unset is the cheap default — see below |
| `DD_ANALYST_EFFORT` | `high` | Proposed per question on the brief, and changeable there |
| `DD_SCOPER_EFFORT` | `low` | Scoping is narrow and the user is waiting on it |
| `DD_SCOPE_MAX_ROUNDS` | `2` | Rounds of clarifying questions before the brief is forced (1–4) |

Any model variable that is set **pins** that role: it cannot then be changed from
Admin → Models. Leave it unset to make the role settable in the UI, where the
choice is persisted to `data/settings.json` and takes effect without a restart.
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
| `DD_COOKIE_SECURE` | `auto` | Marks the session cookie Secure when the request arrived over HTTPS. `1`/`0` force it |
| `DD_LOGIN_MAX_ATTEMPTS` / `DD_LOGIN_LOCKOUT_SECONDS` | `8` / `300` | Sign-in throttle |
| `DD_BROWSE_ROOTS` | `/corpus` + `/inbox` + cwd | External directories the folder picker may descend into. The extraction root and the sync root are always included |
| `DD_MAX_UPLOAD_MB` | `4096` | Per-archive upload ceiling |
| `DD_MAX_EXTRACT_GB` | `20` | Total uncompressed size ceiling |
| `DD_MAX_ARCHIVE_MEMBERS` | `100000` | Member-count ceiling |
| `DD_MAX_COMPRESSION_RATIO` | `200` | Above this an archive is treated as a decompression bomb |
| `DD_MAX_SYNC_GB` | `50` | Refuse to mirror a library larger than this |
| `DD_MAX_SYNC_DELETE` | `500` | Abort a sync that would delete more than this many mirrored files |
| `DD_SYNC_TIMEOUT` | `21600` | Give up on one sync after this many seconds |
| `DD_SYNC_RUNS_KEEP` | `50` | Sync runs kept per connection for the detail view |
| `DD_RCLONE_BIN` | `rclone` | The sync engine. Point at another binary for a newer version |
| `DD_LOG_RETENTION` | `20000` | Activity-log lines kept before the oldest are trimmed. `0` keeps everything |
| `DD_WEEKLY_BUDGET_ASK_USD` | `-1` | Default weekly question budget per account. `-1` unlimited, `0` no spending |
| `DD_WEEKLY_BUDGET_INDEX_USD` | `-1` | Default weekly indexing (sweep) budget per account |
| `DD_BUDGET_GRACE_PCT` | `10` | How far over the question budget one answer a week may run to finish |

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
- **A connected SharePoint library loses SharePoint's permissions.** The app
  authenticates as itself, not as the person asking, so anything synced is
  readable by anyone who can sign in — regardless of who could open it in
  SharePoint. Scope the Entra app with `Sites.Selected` on the one library, and
  connect only libraries whose whole contents everyone here may see.
- **Back up `DD_SECRET_KEY` with the data volume** — it also encrypts the stored
  SharePoint client secrets, not just API keys.
- **`/api/documents/{id}/original` serves any indexed file** to any signed-in
  user, and the folder picker can descend into `DD_BROWSE_ROOTS` (administrators
  only). Both are intended; keep the roots narrow.
- **Bind to localhost.** The default is `127.0.0.1`. Serving it publicly means a
  TLS-terminating reverse proxy in front and `DD_FORWARDED_ALLOW_IPS` set to that
  proxy — not `DD_HOST=0.0.0.0` on its own. That variable also decides whether
  the session cookie gets marked Secure, since `DD_COOKIE_SECURE=auto` follows
  the forwarded scheme; if your proxy terminates TLS without forwarding it, set
  `DD_COOKIE_SECURE=1`.
- **Uploads land on the server's filesystem.** Extraction refuses traversal,
  absolute paths, links, special files, oversized and over-ratio archives, and
  writes only under `data/uploads/extracted`. The limits are configurable; the
  defaults are deliberately not generous.
- Without `DD_SECRET_KEY` the encrypted secrets in that volume — API keys and
  SharePoint client secrets alike — are unrecoverable and must be re-entered.
- **Confirm data-retention terms with your Anthropic account team** before
  uploading privileged material, and note that Claude Fable 5 requires 30-day
  retention — stay on Claude Opus 5 if you are contractually zero-retention.
- Excerpts, not whole files, leave the machine: the indexing sweep sends up to
  ~24K characters per document, and answering sends the pages the model chose to
  read. Everything sent is recorded in the question log.

---

## Deploying to a host

The production instance is `https://ddlib.dhig.net`: the host's existing nginx
terminates TLS and forwards to the container on `127.0.0.1:8412`.

### Port

The container listens on 8000 internally and is published on `8412`, chosen
because the deployment host already has 80/81/443 (nginx), 3001, 3003, 8000,
8080, 8081, 8082, 9432 and 9443 (other containers) and 8763 in use. Change it in
one place — `DD_HOST_PORT` in `.env` — and update the proxy to match. Check
before you pick: `ss -tulpn | grep -w 8412`.

`DD_BIND_ADDR` stays `127.0.0.1`. Publishing on `0.0.0.0` would serve the app
over plain HTTP beside the TLS vhost, cookies and all.

### Reverse proxy

`deploy/nginx/ddlib.dhig.net.conf` is a complete vhost:

```bash
sudo cp deploy/nginx/ddlib.dhig.net.conf /etc/nginx/sites-available/ddlib.dhig.net
sudo ln -s ../sites-available/ddlib.dhig.net /etc/nginx/sites-enabled/
sudo certbot --nginx -d ddlib.dhig.net
sudo nginx -t && sudo systemctl reload nginx
```

Four of its settings are load-bearing, and each fails in a way that looks like
an application bug:

| Setting | Without it |
|---|---|
| `client_max_body_size 4096m` | uploading a data-room zip fails with 413 — nginx's default is 1 MB |
| `proxy_request_buffering off` | nginx spools the whole multi-gigabyte upload to disk first |
| `proxy_buffering off` | indexing progress and streamed answers appear only once finished |
| `proxy_read_timeout 3600s` | long analyst turns die at 60s |

If the host runs Nginx Proxy Manager instead (port 81 suggests it does), create
the proxy host in its UI and paste the block at the end of that file into the
**Advanced** tab; tick Websockets Support, Force SSL and HTTP/2.

`DD_FORWARDED_ALLOW_IPS` decides whose `X-Forwarded-*` is believed, and so
whether the audit log and the login lockout see real client addresses — and
whether the session cookie is marked Secure, since `DD_COOKIE_SECURE=auto`
follows the forwarded scheme. It is **not** `127.0.0.1`: the proxy connects to
the published loopback port and Docker's NAT rewrites the source to the bridge
gateway (typically `172.17.0.1`), so the compose default trusts the private
ranges Docker allocates from. Narrow it to the exact gateway if you want.

If your proxy terminates TLS but does not forward `X-Forwarded-Proto`, set
`DD_COOKIE_SECURE=1` explicitly rather than leaving it on `auto`.

### Outbound access

The container needs `api.anthropic.com`. Connecting a SharePoint library adds
`login.microsoftonline.com`, `graph.microsoft.com` and `*.sharepoint.com` (Graph
redirects downloads there). If the host restricts egress, that allowlist has to
grow before the first sync — otherwise the connection test fails with a "cannot
reach" message that looks like a credential problem.

### Updating

`deploy/update.sh` runs on the docker host, from the checkout:

```bash
cd /opt/dd-library
./deploy/update.sh              # fetch, build, swap, verify — rolls back if unhealthy
./deploy/update.sh --status     # what is running now
./deploy/update.sh --dry-run    # print the plan, change nothing
./deploy/update.sh --rollback   # back to the previous commit
```

What it does, in order: takes a `flock` so two runs can't overlap; refuses to
start without `.env` and a non-empty `DD_SECRET_KEY`, with a dirty checkout
(unless `--force`), or if the port belongs to someone else; fast-forwards the
branch (never merges) and prints the changelog; warns if `.env.example` gained a
variable; snapshots the index and secrets; builds the new image **while the old
container still serves**; swaps; then polls `/api/health` for up to three
minutes. If the new version doesn't come up healthy it restores the previous
commit, brings the old one back and exits non-zero — a failed update leaves the
service running.

Nightly, unattended:

```
17 3 * * *  cd /opt/dd-library && ./deploy/update.sh >> /var/log/ddlib-update.log 2>&1
```

An up-to-date, healthy instance prints one line and exits 0.

### Backups

`update.sh` snapshots into `.deploy/backups/<timestamp>/` before each update
(keeping the newest 5, `--keep N` to change): the SQLite catalogue via the
backup API — a file copy of a live database would be torn — plus `secret.key`
and `settings.json`, because the catalogue's stored API keys are useless without
the key that decrypts them. **Those snapshots contain secrets; `.deploy/` is
gitignored, keep it that way.**

That is the catalogue, not the corpus. For a full restore also back up the
`dd-data` volume (text mirror, uploads, generated deliverables) — a host-level
backup of `/var/lib/docker/volumes/*_dd-data` while the container is stopped, or
`docker run --rm -v dd-library_dd-data:/data -v $PWD:/out alpine tar czf
/out/dd-data.tgz /data`. Everything except uploads and deliverables can be
rebuilt from the corpus by reindexing, at the cost of another indexing sweep.

Mirrors of connected libraries live in that volume too, under `data/sync`. They
are worth *excluding* from a volume backup if size matters — they are a byte-for-
byte copy of something SharePoint still holds, and a fresh sync rebuilds them.
The connection rows themselves, including the encrypted client secrets, are in
`index.sqlite3` and so are already covered by the snapshot above.

`dd-inbox` is staging, not a system of record: back it up only if the feed that
fills it cannot simply re-send.

### Feeding the corpus from outside the app

The container runs as uid/gid **10001** with every capability dropped, so a file
it cannot read is a file that does not exist as far as indexing is concerned.
Anything that writes into a mounted corpus from outside — a host shell, an rsync
or SFTP feed, another container, a scheduled export — has to leave its output
readable by that uid. A feed running as root with a `0077` umask produces a data
room that indexes as zero documents.

**The inbox.** `dd-inbox` is mounted writable at `/inbox` for exactly this. It is
a named volume rather than a bind mount because Docker seeds a *new* named volume
from the image — ownership included — and the image ships `/inbox` as
`dd:dd 2775`. So the volume arrives owned by the runtime user instead of by
root, which is the failure a bind-mounted host directory walks straight into. The
setgid bit means files a feed creates inside inherit group 10001 even when the
feed runs as some other uid.

Seeding only happens the first time the volume is created. If you already had a
`dd-inbox` volume, or you replace it with a bind mount, set the ownership once:

```bash
docker run --rm -v dd-library_dd-inbox:/inbox alpine chown -R 10001:10001 /inbox
```

The most robust way to fill it is to write as the runtime user in the first
place, which needs no fixups afterwards:

```bash
docker run --rm -u 10001:10001 \
  -v dd-library_dd-inbox:/inbox -v /srv/exports:/src:ro \
  alpine cp -a /src/. /inbox/
```

Then point **Corpus → Ingest** at `/inbox` (it is in the folder picker) or set it
as the corpus root. Nothing deletes from the inbox automatically — it is a
staging area, so prune it on whatever schedule suits the feed.

**Any writable path.** Whether you use the inbox or your own mount, make the drop
readable at the source — one line in whatever does the copying:

```bash
umask 022                     # in the feed's environment, before it writes
chmod -R a+rX /srv/data-room  # or afterwards, on the host
```

`a+rX` is the right hammer: the capital `X` sets the execute bit on directories
only, so the tree becomes traversable without marking every PDF executable. Use
`chown -R 10001:10001` instead when the app must also write to that path.

A feed that still writes as root with a restrictive umask defeats all of this —
the setgid bit fixes group *ownership*, not the mode. That is what the access
check below is for.

To check the current state, sign in as an administrator and use **Admin → Corpus
access**. It walks every configured root as the runtime user and lists exactly
what it cannot read, with each path's owner and mode. *Fix what I can* repairs
the one case the container is permitted to repair — a path the app itself owns,
on a writable mount, whose mode locks it out — re-auditing after each pass,
because opening a directory can reveal more locked paths inside it.

For the rest it prints host-side commands. The path in them is derived from
`/proc/self/mountinfo` and is a *hint*: it is where the mount sits inside its
source filesystem, which equals the host path only when that filesystem is
mounted at `/` on the host. The panel says which device it came from — check it
before running a recursive `chmod`. It cannot do more than that by design: `chown` needs `CAP_CHOWN`,
`chmod` on someone else's file needs `CAP_FOWNER`, both are dropped, and
`/corpus` is mounted read-only. A button that silently needed those privileges
would be a worse security posture than a button that tells you what to run.

Ingest reports the same thing in its log: unreadable folders are listed at error
level and counted in the completion message, rather than quietly reducing the
document count.

---

## Verifying it works

Four suites, none of which spend a token, touch your real index, or need a
network — each uses its own temporary data directory.

```bash
python3 tools/api_smoke.py            # 199 checks: auth, CSRF, keys, uploads, access, sync, storage, scoping
python3 tools/sync_smoke.py           # 79 checks: connected libraries, end to end
python3 tools/ui_smoke.py             # 118 checks: the browser front end, end to end
tools/container_check.sh              # 6 checks plus a sync-engine note
```

`api_smoke.py` drives the real HTTP surface with a cookie-aware client:
unauthenticated access is refused, first-run bootstrap works once and then
closes, a missing or wrong CSRF token is rejected, failed sign-ins throttle,
analysts cannot reach admin routes, disabling a user kills their session
immediately, a stored API key round-trips through encryption and is never echoed
back, an archive with a `../` member extracts its safe files and refuses the
rest, browsing outside the permitted roots is refused — as is naming such a path
directly to the corpus-root and ingest routes — and every privileged action
lands in the audit log. It also pins the ingest walk: a folder named `data` is
scanned like any other, the app's own text mirror never re-enters the corpus as
source material, and an unreadable folder is reported rather than skipped in
silence.

Its scoping section pins the two properties nothing else would catch: that the
scoper's system prefix and tool array are byte-identical to the analyst's — the
whole cost argument — and that coverage is capped by the probe rather than by
whatever the model claims, clamped into 0–100, and never allowed to end the loop.
It also checks that unresolvable doc_ids are dropped, that a rendered brief
contributes no citations, and that an unpriced model cannot be selected.

`sync_smoke.py` covers connected libraries against a stub Graph and a fake
rclone (`tools/fake_rclone.py`), so it needs no tenant and no network while still
spawning the real subprocess and running the real ingest: a connection round-trips
through encryption and is never echoed back, an API-key ciphertext cannot be
reused as a client secret, progress parsing tracks transfers and unchanged files,
remote deletions flow through to purging the index, a failure and a cancellation
both leave the connection in a sane state, an oversized library is refused before
anything is fetched, and the client secret reaches rclone through its environment
rather than its command line.

`ui_smoke.py` ingests the sample corpus, stamps synthetic cards, replaces the
agent and the scoper with scripted event streams, and drives a real browser:
sign-in, a round of clarifying questions answered by number key, multi-select and
free text, the coverage meter, a brief edited before approval — asserting that
what actually ran was the edited text and that the scoping dialogue never entered
the analyst's history — the thin-coverage warning and its *Run anyway* path,
streaming answer, reasoning panel, tool trace, citation chips that open the
source at the cited page, verdict badges, artifact download, the admin surfaces,
a drag-and-drop archive upload through to extraction, and the connected-library
pane — including that a sync event drives the sync progress bar and not the
indexing one. Screenshots land in `/tmp`. Requires `pip install playwright` and a
Chromium binary (set `DD_CHROMIUM` if it is not at `/opt/pw-browsers/chromium`).

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
                  qa_log, scope_rounds, users, sessions, api_keys, archives,
                  sync_connections, sync_runs, audit, logs, spend, user_budgets)
  security.py     scrypt password hashing, session tokens, AES-GCM at rest
  auth.py         users, sessions, roles, login throttle, route guards
  credentials.py  API key storage and Anthropic client construction
  extract.py      per-format extraction into the anchored text mirror
  access.py       corpus readability diagnosis and the repairs we are allowed
  ingest.py       walk → hash → extract → index → duplicate detection
  uploads.py      archive upload and hardened extraction
  graph.py        the app-only Microsoft Graph calls rclone cannot make
  sync.py         connected libraries: credentials, the mirror job, per-run
                  history, scheduling
  storage.py      disk usage and the reclaim operations
  manifest.py     the sweep (one card per document) and the corpus map
  search.py       BM25 search, catalogue browsing, corpus statistics
  tools.py        the analyst's tool definitions and handlers
  agent.py        the streaming tool loop, system prompt, citation parsing
  scope.py        question refinement: the free coverage probe, the grounded
                  clarifying rounds, the research brief and the model proposal
  verify.py       re-read each cited span and judge the claim
  docgen.py       docx / xlsx / pptx / md from a block spec
  pricing.py      token accounting, the cost meter, and spend attribution
  budget.py       weekly per-account caps: the week, inheritance, the overrun
  events.py       in-process pub/sub behind the SSE progress stream, and the
                  sink that persists each log line to the `logs` table
  server.py       FastAPI routes and the access-control middleware
web/              single-page UI, the login page, and the SharePoint setup
                  guide served at /help/sharepoint — no build step
tools/            sample corpus, API / sync / UI smoke tests, container check,
                  fake rclone
Dockerfile        non-root, read-only /app, /data and /inbox volumes
docker-compose.yml  hardened runtime, localhost-published on 8412
deploy/
  update.sh       host-side update: pull, build, swap, health-check, roll back
  nginx/ddlib.dhig.net.conf   the production vhost
```

### Design notes worth knowing before you change things

**The prompt prefix is load-bearing.** Order is `tools` → instructions → corpus
map, with the cache breakpoint on the map. Tool definitions are kept in a fixed
sorted order and the map is rebuilt only after a sweep, because any byte change
in that prefix throws away the cache that makes the whole approach affordable.

**The scoper rides that same prefix, byte for byte.** It sends `tools.TOOLS` and
`agent.system_blocks()` unchanged, restricts itself to read-only tools *at
dispatch*, and carries its directive in the first user turn — after the
breakpoint. That is what makes a scoping round a cache read at 0.1× rather than a
fresh write at 2×, and it is why `scoper_model` defaults to the analyst's model:
caches are per-model, so a cheaper scoper has its own copy of a 250k-token map to
pay for. `tools/api_smoke.py` asserts the prefix identity directly, because
nothing else in the suite would notice it breaking.

**A model must never be the only thing bounding a number it reports.** The
coverage percentage is capped by a mechanical BM25 probe before it is shown, and
doc_ids in questions and briefs are dropped unless they resolve in `documents`.
The schema cannot express either rule, so both live in `scope._coerce`. Keep new
model-authored fields on that side of the line.

**Documents are content-addressed** (`sha256[:16]`). This is what makes ingest
resumable and idempotent, and it is why exact duplicates cannot exist as
separate rows — extra paths live in the `occurrences` table.

**Near-duplicate detection is blocked, not exhaustive.** Candidate pairs come
from version-stripped filenames and identical first units; only those pairs get
a Jaccard comparison. A renamed near-duplicate with a different first page will
be missed. That is the documented trade-off for staying linear across thousands
of documents.

**The ingest walk must never fail silently.** It is recursive, and the two ways
it can return nothing while reporting success have both bitten: a name-based
skip list matched against the *absolute* path (so `data` in the list excluded
the container's own `/data` volume and every host path like `/srv/data/room`),
and `Path.rglob` swallowing the `PermissionError` from a directory the runtime
user cannot read. Hence `os.walk` with an `onerror` hook, directory names
matched only below the root, the app's own output excluded by resolved path, and
a `ScanResult` that carries what the walk could not see so the job can say so.
Keep that property: a zero-document ingest must always explain itself.

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
- **Delta sync** — each sync re-lists the library and ingest re-hashes the mirror.
  Fine at this size; if it stops being fine, Graph's `/delta` endpoint gives
  changed items only, and `occurrences.mtime`/`size_bytes` are already recorded
  and unused, so ingest could skip unchanged files without reading them.
- **More sources** — the connection abstraction is "fill a directory, then
  ingest". Google Drive, Dropbox and S3 are all rclone backends already; each
  would need its own credential fields and its own discovery step.
