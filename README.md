# DD Library

A due-diligence assistant over a local document library: ingest a data room of
PDFs, spreadsheets and decks, watch it get indexed, then ask questions and
generate deliverables — with a citation on every claim.

Your documents stay on your machine. Only the excerpts the model actually needs
are sent to the Claude API.

```
./run.sh   →   http://127.0.0.1:8000
```

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

**Corpus tab** — point it at a folder. It walks the tree, extracts text into a
greppable mirror, and records a citation anchor for every page, slide and sheet.
Re-running only processes what changed.

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
verdicts and its cost. That log is the audit trail.

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

Python 3.11+.

```bash
pip install -r requirements.txt
export ANTHROPIC_API_KEY=sk-ant-...     # or: ant auth login
./run.sh
```

Then in the browser: **Corpus → point at your folder → Ingest**, then
**Indexing → Run sweep**, then **Ask**.

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

Effort is worth a sweep of its own on your questions: `low` and `medium` are
strong on Claude Opus 5 and much cheaper, while `xhigh` suits the hardest
multi-document reconciliations.

---

## Security and confidentiality

Read this before pointing it at a live deal.

- **`run_python` executes model-generated code on the host.** It runs in a
  subprocess with a scrubbed environment, a temp working directory and a
  timeout — but it is *not* a sandbox: it can read any file the server user can
  read, and reach the network. Run the whole app in a container or a dedicated
  VM, or set `DD_ENABLE_PYTHON=0` and accept that the assistant can no longer
  compute over your spreadsheets.
- **It binds to localhost and has no authentication.** Do not expose it with
  `DD_HOST=0.0.0.0` except behind a proxy that authenticates, and never on a
  shared host — every endpoint, including the folder browser and file download,
  is unauthenticated.
- **The folder browser and `/api/documents/{id}/original` can serve any file the
  process can read.** That is deliberate for a single-user local tool and
  unacceptable if step 2 is ignored.
- **Access control is all-or-nothing.** The corpus map is a single blob in the
  system prompt: anyone who can use the app learns that every document exists.
  If your data room has restricted folders, run one instance per access tier.
- **Confirm data-retention terms with your Anthropic account team** before
  uploading privileged material, and note that Claude Fable 5 requires 30-day
  retention — stay on Claude Opus 5 if you are contractually zero-retention.
- Excerpts, not whole files, leave the machine: the indexing sweep sends up to
  ~24K characters per document, and answering sends the pages the model chose to
  read. Everything sent is recorded in the question log.

---

## Verifying it works

```bash
python3 tools/make_sample_corpus.py ./sample-dataroom   # synthetic data room
python3 tools/ui_smoke.py                               # end-to-end UI test, no tokens spent
```

`ui_smoke.py` ingests the sample corpus into a temp directory, stamps synthetic
cards, replaces the agent with a scripted event stream, and drives a real
browser: streaming answer, reasoning panel, tool trace, citation chips, verdict
badges, artifact download, and click-a-citation-to-open-the-source. It asserts
twelve behaviours and spends nothing. Screenshots land in `/tmp`.

Requires `pip install playwright` and a Chromium binary (set `DD_CHROMIUM` if it
is not at `/opt/pw-browsers/chromium`).

---

## Layout

```
app/
  config.py     settings, supported formats, workstream taxonomy
  db.py         SQLite schema (documents, units + FTS5, occurrences, jobs, qa_log)
  extract.py    per-format extraction into the anchored text mirror
  ingest.py     walk → hash → extract → index → duplicate detection
  manifest.py   the sweep (one card per document) and the corpus map
  search.py     BM25 search, catalogue browsing, corpus statistics
  tools.py      the analyst's tool definitions and handlers
  agent.py      the streaming tool loop, system prompt, citation parsing
  verify.py     re-read each cited span and judge the claim
  docgen.py     docx / xlsx / pptx / md from a block spec
  pricing.py    token accounting and the cost meter
  events.py     in-process pub/sub behind the SSE progress stream
  server.py     FastAPI routes
web/            single-page UI, no build step
tools/          sample-corpus generator, UI smoke test
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

## Where to take it next

- **Semantic search** — everything here is keyword + map. If you find questions
  that BM25 misses, add embeddings over the same units (Anthropic has no
  embeddings endpoint; use a provider or a local model) and merge the rankings.
- **Sub-agents per workstream** — fan a broad question out across financial /
  legal / commercial in parallel and synthesise, rather than one serial loop.
- **Batch the sweep** — the Batch API halves indexing cost; live progress is
  what it trades away.
- **Multi-user** — real auth, per-user access tiers, and a map filtered per tier.
