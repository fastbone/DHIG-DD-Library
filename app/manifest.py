"""The manifest sweep: one structured card per document, then the corpus map.

The sweep is the expensive-once / cheap-forever half of the design. Each
document gets a compact card from a small model; the cards are assembled into a
single manifest that rides in the analyst's system prompt behind a 1-hour cache
breakpoint. That is what makes the assistant aware of every file in the library
without ever loading the library.
"""

from __future__ import annotations

import asyncio
import json
import time
import uuid

from anthropic import AsyncAnthropic

from . import credentials, db, extract, pricing
from .config import WORKSTREAMS, settings
from .events import broker

CARD_SCHEMA = {
    "type": "object",
    "properties": {
        "title": {"type": "string", "description": "Real document title, not the filename"},
        "doc_type": {
            "type": "string",
            "description": "e.g. contract, financial_model, board_deck, audited_accounts, "
            "cap_table, employment_agreement, tax_ruling, policy, invoice, org_chart",
        },
        "workstream": {"type": "string", "enum": WORKSTREAMS},
        "parties": {"type": "array", "items": {"type": "string"}},
        "period_covered": {
            "type": "string",
            "description": "Reporting period or effective dates; empty string if none",
        },
        "key_figures": {
            "type": "array",
            "items": {"type": "string"},
            "description": "Up to 5 headline numbers with units, e.g. 'FY23 revenue EUR 412.6m'",
        },
        "summary": {"type": "string", "description": "Two sentences, maximum"},
        "languages": {"type": "array", "items": {"type": "string"}},
        "flags": {
            "type": "array",
            "items": {"type": "string"},
            "description": "Any of: draft, unsigned, scanned, redacted, contains_pii, "
            "password_protected, appears_superseded, low_text_quality",
        },
    },
    "required": [
        "title", "doc_type", "workstream", "parties", "period_covered",
        "key_figures", "summary", "languages", "flags",
    ],
    "additionalProperties": False,
}

CARD_SYSTEM = """You index documents for a corporate due diligence data room.
For each document you produce one factual catalogue card. Rules:
- Use only what the excerpt supports. Never guess numbers, parties, or dates.
- period_covered and any unknown string field: return "" rather than inventing.
- key_figures must be verbatim-grounded in the excerpt, with units and currency.
- summary: what the document IS and what it CONTAINS, in two sentences. No praise, no hedging.
- flags: only when clearly evidenced by the excerpt."""


def excerpt(doc_id: str, budget: int) -> str:
    """Head + middle + tail sample, so long documents still get characterised."""
    text = extract.read_mirror(doc_id)
    if len(text) <= budget:
        return text
    third = budget // 3
    mid = len(text) // 2
    return (
        text[:third]
        + "\n\n[… excerpt truncated …]\n\n"
        + text[mid - third // 2 : mid + third // 2]
        + "\n\n[… excerpt truncated …]\n\n"
        + text[-third:]
    )


async def card_one(client: AsyncAnthropic, doc: dict, meter: pricing.Meter) -> dict:
    body = excerpt(doc["id"], settings.card_excerpt_chars)
    if not body.strip():
        db.execute(
            "UPDATE documents SET status='card_failed', error=? WHERE id=?",
            ("no extractable text", doc["id"]),
        )
        return {"id": doc["id"], "error": "no extractable text"}

    prompt = (
        f"File: {doc['rel_path']}\n"
        f"Type: {doc['family']} · {doc['size_bytes'] / 1024:.0f} KB · {doc['n_units']} unit(s)\n"
        f"--- excerpt ---\n{body}\n--- end excerpt ---\n\n"
        "Produce the catalogue card."
    )
    resp = await client.messages.create(
        model=settings.carder_model,
        max_tokens=1500,
        system=CARD_SYSTEM,
        output_config={"format": {"type": "json_schema", "schema": CARD_SCHEMA}},
        messages=[{"role": "user", "content": prompt}],
    )
    pricing.record(settings.carder_model, resp.usage, meter=meter)

    text = next((b.text for b in resp.content if b.type == "text"), "")
    if resp.stop_reason == "refusal":
        raise RuntimeError("carder refused this document")
    card = json.loads(text)
    db.execute(
        "UPDATE documents SET status='carded', error=NULL, title=?, doc_type=?, workstream=?,"
        " parties=?, period_covered=?, key_figures=?, summary=?, languages=?, card_flags=?,"
        " carded_at=? WHERE id=?",
        (
            card["title"][:500],
            card["doc_type"][:100],
            card["workstream"],
            json.dumps(card["parties"][:12]),
            card["period_covered"][:120],
            json.dumps(card["key_figures"][:5]),
            card["summary"][:1200],
            json.dumps(card["languages"][:5]),
            json.dumps(card["flags"][:8]),
            time.time(),
            doc["id"],
        ),
    )
    return {"id": doc["id"], "card": card}


class SweepJob:
    """Cards every extracted-but-unindexed document."""

    def __init__(self, redo: bool = False) -> None:
        self.id = f"sweep-{uuid.uuid4().hex[:8]}"
        self.redo = redo
        self.cancel = asyncio.Event()
        self.total = 0
        self.done = 0
        self.failed = 0
        self.meter = pricing.Meter()

    def _publish(self, message: str = "") -> None:
        broker.publish(
            "job", job_id=self.id, job_kind="sweep", status="running", total=self.total,
            done=self.done, failed=self.failed, message=message,
            usage=self.meter.snapshot(),
        )

    def pending(self) -> list[dict]:
        where = "status='extracted'" if not self.redo else "status IN ('extracted','carded','card_failed')"
        return [
            dict(r)
            for r in db.rows(
                f"SELECT id, rel_path, family, size_bytes, n_units FROM documents"
                f" WHERE {where} ORDER BY size_bytes DESC"
            )
        ]

    async def run(self) -> None:
        started = time.time()
        docs = self.pending()
        self.total = len(docs)
        db.job_upsert(self.id, kind="sweep", status="running", total=self.total,
                      message=f"{self.total} documents to index")
        broker.log(f"Sweep starting: {self.total} documents → {settings.carder_model}")
        self._publish("starting")

        if not self.total:
            db.job_upsert(self.id, status="done", message="nothing to index",
                          finished_at=time.time())
            broker.log("Nothing to index — every document already has a card.", level="success")
            broker.publish("job", job_id=self.id, job_kind="sweep", status="done", total=0, done=0,
                           failed=0, message="nothing to index")
            return

        client = credentials.get_client()
        sem = asyncio.Semaphore(settings.card_concurrency)

        async def worker(doc: dict) -> None:
            if self.cancel.is_set():
                return
            async with sem:
                if self.cancel.is_set():
                    return
                for attempt in range(3):
                    try:
                        result = await card_one(client, doc, self.meter)
                        if result.get("error"):
                            self.failed += 1
                        break
                    except Exception as exc:  # noqa: BLE001
                        if attempt == 2:
                            self.failed += 1
                            db.execute(
                                "UPDATE documents SET status='card_failed', error=? WHERE id=?",
                                (f"{type(exc).__name__}: {exc}"[:400], doc["id"]),
                            )
                            broker.log(
                                f"card failed {doc['rel_path']}: {type(exc).__name__}: {exc}",
                                level="warn",
                            )
                        else:
                            await asyncio.sleep(2**attempt)
                self.done += 1
                if self.done % 3 == 0 or self.done == self.total:
                    db.job_upsert(self.id, done=self.done, failed=self.failed)
                    self._publish()

        try:
            await asyncio.gather(*(worker(d) for d in docs))
        finally:
            await client.close()

        status = "cancelled" if self.cancel.is_set() else "done"
        usage = self.meter.snapshot()
        msg = (
            f"Indexed {self.done - self.failed}/{self.total} documents in "
            f"{time.time() - started:.0f}s · ${usage['cost_usd']:.2f} · {self.failed} failed"
        )
        db.job_upsert(self.id, status=status, done=self.done, failed=self.failed, message=msg,
                      detail=json.dumps(usage), finished_at=time.time())
        broker.log(msg, level="success" if status == "done" else "warn")
        broker.publish("job", job_id=self.id, job_kind="sweep", status=status, total=self.total,
                       done=self.done, failed=self.failed, message=msg, usage=usage)
        invalidate_manifest()
        broker.publish("stats_dirty")


# --- manifest assembly ---------------------------------------------------

_cache: dict | None = None


def invalidate_manifest() -> None:
    global _cache
    _cache = None


def _card_line(d: dict, with_summary: bool) -> str:
    bits = [f"[{d['id']}]", f"{d['workstream']}/{d['doc_type']}"]
    if d.get("period_covered"):
        bits.append(d["period_covered"])
    bits.append(f"“{d['title']}”")
    parties = json.loads(d["parties"] or "[]")
    if parties:
        bits.append("parties: " + ", ".join(parties[:4]))
    figs = json.loads(d["key_figures"] or "[]")
    if figs:
        bits.append("figures: " + "; ".join(figs[:3]))
    flags = json.loads(d["card_flags"] or "[]")
    if flags:
        bits.append("flags: " + ",".join(flags))
    bits.append(f"{d['n_units']}u · {d['rel_path']}")
    line = " · ".join(bits)
    if with_summary and d.get("summary"):
        line += f"\n    {d['summary']}"
    return line


def build() -> dict:
    """Assemble the corpus map. Cached until the next sweep completes."""
    global _cache
    if _cache is not None:
        return _cache

    carded = [
        dict(r)
        for r in db.rows(
            "SELECT id, rel_path, workstream, doc_type, title, parties, period_covered,"
            " key_figures, summary, card_flags, n_units FROM documents"
            " WHERE status='carded'"
            " ORDER BY workstream, doc_type, rel_path"
        )
    ]
    unindexed = [
        dict(r)
        for r in db.rows(
            "SELECT id, rel_path, family, n_units, status FROM documents"
            " WHERE status IN ('extracted','card_failed','extract_failed')"
            " ORDER BY rel_path LIMIT 400"
        )
    ]

    rollup: dict[str, dict[str, int]] = {}
    for d in carded:
        rollup.setdefault(d["workstream"], {}).setdefault(d["doc_type"], 0)
        rollup[d["workstream"]][d["doc_type"]] += 1

    def render(mode: str) -> str:
        out: list[str] = []
        out.append("# CORPUS MAP")
        out.append(
            f"{len(carded)} indexed documents"
            + (f", {len(unindexed)} extracted but not yet indexed" if unindexed else "")
            + ". Every id below is addressable with read_document / search_corpus."
        )
        out.append("")
        out.append("## Coverage by workstream")
        for ws in sorted(rollup):
            types = ", ".join(f"{t}×{n}" for t, n in sorted(rollup[ws].items(), key=lambda kv: -kv[1]))
            out.append(f"- {ws} ({sum(rollup[ws].values())}): {types}")
        out.append("")
        if mode != "rollup":
            out.append("## Documents")
            current = None
            for d in carded:
                if d["workstream"] != current:
                    current = d["workstream"]
                    out.append(f"\n### {current}")
                out.append(_card_line(d, with_summary=(mode == "full")))
        else:
            out.append("## Documents")
            out.append(
                "Too many documents to list inline. Use list_documents(workstream=…, query=…)"
                " to page through the catalogue before searching."
            )
        if unindexed:
            out.append("")
            out.append("## Not yet indexed (text extracted, no card)")
            for d in unindexed[:200]:
                out.append(f"[{d['id']}] {d['status']} · {d['family']} · {d['rel_path']}")
        return "\n".join(out)

    for mode in ("full", "compact", "rollup"):
        text = render(mode)
        if len(text) <= settings.manifest_char_budget or mode == "rollup":
            _cache = {
                "text": text,
                "mode": mode,
                "chars": len(text),
                "approx_tokens": len(text) // 4,
                "n_indexed": len(carded),
                "n_unindexed": len(unindexed),
                "rollup": rollup,
            }
            return _cache
    raise AssertionError("unreachable")
