"""The analyst loop.

One streaming Messages call per turn, a manual tool loop around it, and an
event stream out to the browser so the user watches the same trace the model
produces: which searches it ran, which pages it read, what it computed.

Context layout (matters for cost):
    tools            → stable, sorted            ─┐
    system[0]        → instructions, stable       │ cached prefix
    system[1]        → corpus map, cache breakpoint (1h TTL)
    messages         → the question and the loop
"""

from __future__ import annotations

import asyncio
import json
import re
import time
import traceback
import uuid
from typing import AsyncIterator

from anthropic import AsyncAnthropic

from . import budget, credentials, db, docgen, manifest, pricing, tools, verify
from .config import settings
from .events import broker

MAX_TURNS = 30
CITATION_RE = re.compile(r"\[\[([0-9a-f]{6,32}):([^\]\s]{1,120})\]\]")

INSTRUCTIONS = """You are the due diligence analyst for this data room. You have a complete map of
the corpus in your context and tools to read any part of it.

## Grounding rules — these are the job, not style preferences
1. Every factual claim carries an inline citation in the form [[doc_id:anchor]], e.g.
   [[a1b2c3d4e5f6:p14]] or [[9f8e7d6c5b4a:Summary!A1:H240]]. A number without a citation is
   worthless to the reader. Cite the specific page/slide/sheet, never just the document.
2. Never do arithmetic in your head or read figures out of extracted text when the original
   spreadsheet exists. Use run_python to load the workbook and compute. Extracted text loses
   number formatting, merged cells and formulas; a DD number that is wrong is worse than absent.
3. If the corpus does not answer the question, say so plainly and say what document would answer
   it. "Not in the data room" is a finding — it is often the most valuable one. Never fill a gap
   with general knowledge about the industry.
4. Distinguish what a document asserts from what is true. Management projections are claims by
   management; audited accounts are a different class of evidence. Say which you are relying on.
5. Watch for superseded versions. The map flags duplicate groups and `appears_superseded`. If two
   documents disagree, say so explicitly with both citations rather than silently picking one.
6. Flag anything that looks like a red flag as you encounter it, even if it was not asked about:
   change-of-control clauses, unusual related-party items, missing signatures, going-concern
   language, customer concentration, unfunded liabilities, tax exposures.

## Working method
- Start from the corpus map. You already know what exists — go straight to the relevant documents
  rather than searching blindly.
- Run several narrow searches rather than one broad one; then read the pages that matter.
- For anything quantitative, compute it, and show the computation's output.
- Read enough to be right. Reading five pages you did not need is cheap; a wrong finding is not.

## Output
Lead with the answer. Then the evidence, then the caveats. Use short prose and tables; skip
preamble and skip restating the question. When the user asks for a document (memo, report,
findings table, deck), build it with create_deliverable and keep the citations inside it."""


def parse_citations(text: str) -> list[dict]:
    seen: dict[str, dict] = {}
    for doc_id, anchor in CITATION_RE.findall(text or ""):
        key = f"{doc_id}:{anchor}"
        if key in seen:
            seen[key]["count"] += 1
            continue
        row = db.one("SELECT title, rel_path, workstream FROM documents WHERE id=?", (doc_id,))
        seen[key] = {
            "citation": key,
            "doc_id": doc_id,
            "anchor": anchor,
            "title": (row["title"] if row else None) or (row["rel_path"] if row else "unknown"),
            "rel_path": row["rel_path"] if row else None,
            "workstream": row["workstream"] if row else None,
            "resolved": row is not None,
            "count": 1,
        }
    return list(seen.values())


def system_blocks() -> tuple[list[dict], dict]:
    m = manifest.build()
    return (
        [
            {"type": "text", "text": INSTRUCTIONS},
            {
                "type": "text",
                "text": m["text"],
                "cache_control": {"type": "ephemeral", "ttl": "1h"},
            },
        ],
        m,
    )


def _summarise_result(name: str, payload: dict) -> str:
    if not isinstance(payload, dict):
        return str(payload)[:200]
    if payload.get("error"):
        return f"error: {payload['error']}"
    if name == "search_corpus":
        return f"{payload.get('n_hits', 0)} hits"
    if name == "list_documents":
        return f"{len(payload.get('documents', []))} of {payload.get('total', 0)} documents"
    if name == "read_document":
        return f"{payload.get('title', '')[:60]} · {len(payload.get('text', ''))} chars"
    if name == "document_card":
        return f"{payload.get('title', '')[:60]} · {len(payload.get('anchors', []))} anchors"
    if name == "run_python":
        code = payload.get("exit_code")
        out = (payload.get("stdout") or "").strip().splitlines()
        head = out[0][:120] if out else "(no output)"
        return f"exit {code} · {head}"
    if name == "create_deliverable":
        return f"{payload.get('filename')} ({payload.get('size_bytes', 0) / 1024:.0f} KB)"
    return "ok"


async def ask(
    question: str,
    *,
    history: list[dict] | None = None,
    do_verify: bool = True,
    effort: str | None = None,
    actor: str | None = None,
) -> AsyncIterator[dict]:
    started = time.time()
    qa_id = uuid.uuid4().hex[:12]
    meter = pricing.Meter()

    # Before the corpus map is even built: a refusal should cost nothing and say
    # plainly what the position is, rather than surfacing as a failure mid-answer.
    try:
        await asyncio.to_thread(budget.require, actor, "ask")
    except budget.BudgetExceeded as exc:
        yield {"type": "error", "message": str(exc), "reason": "budget",
               "budget": await asyncio.to_thread(budget.status, actor)}
        return

    system, m = system_blocks()

    yield {
        "type": "status",
        "message": f"corpus map: {m['n_indexed']} documents, ~{m['approx_tokens']:,} tokens"
        f" ({m['mode']} mode)",
        "qa_id": qa_id,
        "manifest": {k: m[k] for k in ("mode", "chars", "approx_tokens", "n_indexed", "n_unindexed")},
    }

    if m["n_indexed"] == 0:
        yield {
            "type": "error",
            "message": "No documents are indexed yet. Point the app at a corpus, run Ingest, "
            "then run the Sweep.",
        }
        return

    messages: list[dict] = list(history or [])
    messages.append({"role": "user", "content": question})

    payer = pricing.Attribution(actor, "ask", "analyst", qa_id)
    stopped_on_budget = False
    holding_grace = False
    client = credentials.get_client()
    tool_trace: list[dict] = []
    final_text = ""
    artifacts: list[dict] = []

    try:
        for turn in range(MAX_TURNS):
            assistant_blocks: list[dict] = []
            async with client.messages.stream(
                model=settings.analyst_model,
                max_tokens=32_000,
                system=system,
                tools=tools.TOOLS,
                thinking={"type": "adaptive", "display": "summarized"},
                output_config={"effort": effort or settings.analyst_effort},
                messages=messages,
            ) as stream:
                async for event in stream:
                    if event.type == "content_block_delta":
                        d = event.delta
                        if d.type == "text_delta":
                            yield {"type": "text_delta", "text": d.text}
                        elif d.type == "thinking_delta":
                            yield {"type": "thinking_delta", "text": d.thinking}
                    elif event.type == "content_block_start":
                        if event.content_block.type == "thinking":
                            yield {"type": "phase", "phase": "thinking"}
                        elif event.content_block.type == "text":
                            yield {"type": "phase", "phase": "writing"}
                response = await stream.get_final_message()

            cost = pricing.record(
                settings.analyst_model, response.usage, meter=meter, cache_ttl_1h=True,
                attribution=payer,
            )
            yield {
                "type": "usage",
                "turn": turn,
                "cost_usd": round(cost, 4),
                "cumulative": meter.snapshot(),
                "cache_read": getattr(response.usage, "cache_read_input_tokens", 0),
                "cache_write": getattr(response.usage, "cache_creation_input_tokens", 0),
                "input": response.usage.input_tokens,
                "output": response.usage.output_tokens,
            }

            if response.stop_reason == "refusal":
                yield {"type": "error", "message": "The model declined this request."}
                return

            text_this_turn = "".join(b.text for b in response.content if b.type == "text")
            if text_this_turn:
                final_text = text_this_turn
            assistant_blocks = [b.model_dump(exclude_none=True) for b in response.content]
            messages.append({"role": "assistant", "content": assistant_blocks})

            tool_uses = [b for b in response.content if b.type == "tool_use"]
            if not tool_uses:
                break

            # Between turns is the only place a running answer can be stopped
            # without throwing away what it has already paid for. A turn boundary
            # also means the model has just written text, so the partial answer is
            # coherent rather than a half-sentence.
            decision, note = await asyncio.to_thread(
                budget.turn_decision, actor, holding_grace=holding_grace, ref=qa_id
            )
            if decision == budget.GRACE:
                holding_grace = True
                yield {"type": "status", "reason": "budget", "message": note}
            elif decision == budget.STOP:
                stopped_on_budget = True
                yield {
                    "type": "status",
                    "reason": "budget",
                    "message": f"{note} The answer below is as far as it got — its "
                               f"citations are still worth opening.",
                }
                break

            results: list[dict] = []
            for tu in tool_uses:
                payload = dict(tu.input or {})
                yield {
                    "type": "tool_use",
                    "id": tu.id,
                    "name": tu.name,
                    "input": payload,
                    "label": payload.get("purpose")
                    or payload.get("query")
                    or payload.get("filename")
                    or payload.get("doc_id", ""),
                }
                try:
                    if tu.name == "create_deliverable":
                        out = await asyncio.to_thread(_create_deliverable, payload, qa_id)
                        if not out.get("error"):
                            artifacts.append(out)
                            yield {"type": "artifact", **out}
                    else:
                        out = await asyncio.to_thread(tools.dispatch, tu.name, payload)
                except Exception as exc:  # noqa: BLE001
                    out = {"error": f"{type(exc).__name__}: {exc}"}
                is_error = bool(isinstance(out, dict) and out.get("error"))
                summary = _summarise_result(tu.name, out)
                tool_trace.append({"name": tu.name, "label": payload, "summary": summary,
                                   "error": is_error})
                yield {
                    "type": "tool_result",
                    "id": tu.id,
                    "name": tu.name,
                    "ok": not is_error,
                    "summary": summary,
                    "payload": _preview(out),
                }
                results.append(
                    {
                        "type": "tool_result",
                        "tool_use_id": tu.id,
                        "content": json.dumps(out, default=str)[:200_000],
                        "is_error": is_error,
                    }
                )
            messages.append({"role": "user", "content": results})
        else:
            yield {"type": "status", "message": f"stopped after {MAX_TURNS} turns"}

        citations = parse_citations(final_text)
        yield {"type": "citations", "citations": citations}

        verdicts: list[dict] = []
        if do_verify and citations and stopped_on_budget:
            # Verification is another round of paid calls. Spending past a limit
            # that has just stopped the answer would make the limit meaningless.
            yield {"type": "status", "reason": "budget",
                   "message": "Verification skipped — it costs another API call per claim."}
        elif do_verify and citations:
            yield {"type": "phase", "phase": "verifying"}
            async for v in verify.verify_answer(
                final_text, meter=meter, attribution=payer.as_kind("verifier")
            ):
                verdicts.append(v)
                yield {"type": "verdict", **v}

        duration = time.time() - started
        usage = meter.snapshot()
        db.execute(
            "INSERT INTO qa_log(id, question, answer, citations, verdicts, tool_calls, usage,"
            " model, duration_s, created_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
            (
                qa_id, question, final_text, json.dumps(citations), json.dumps(verdicts),
                json.dumps(tool_trace), json.dumps(usage), settings.analyst_model, duration,
                time.time(),
            ),
        )
        broker.log(
            f"Answered in {duration:.0f}s · {len(tool_trace)} tool calls · "
            f"{len(citations)} citations · ${usage['cost_usd']:.3f}",
            level="success",
        )
        yield {
            "type": "done",
            "qa_id": qa_id,
            "answer": final_text,
            "citations": citations,
            "verdicts": verdicts,
            "artifacts": artifacts,
            "usage": usage,
            "duration_s": round(duration, 1),
            "assistant_message": final_text,
            "stopped_on_budget": stopped_on_budget,
            "budget": await asyncio.to_thread(budget.status, actor),
        }
    except Exception as exc:  # noqa: BLE001
        broker.log(
            f"ask failed: {type(exc).__name__}: {exc}",
            level="error",
            source="ask",
            context={"exc_type": type(exc).__name__,
                     "traceback": traceback.format_exc(limit=12)},
        )
        yield {"type": "error", "message": f"{type(exc).__name__}: {exc}"}
    finally:
        await client.close()


def _create_deliverable(payload: dict, qa_id: str) -> dict:
    try:
        return docgen.create(payload, qa_id)
    except Exception as exc:  # noqa: BLE001
        return {"error": f"{type(exc).__name__}: {exc}"}


def _preview(out) -> dict | str:
    if isinstance(out, dict):
        slim = {}
        for k, v in out.items():
            if isinstance(v, str) and len(v) > 1200:
                slim[k] = v[:1200] + " …"
            elif isinstance(v, list) and len(v) > 12:
                slim[k] = v[:12] + [f"… {len(v) - 12} more"]
            else:
                slim[k] = v
        return slim
    return str(out)[:1200]
