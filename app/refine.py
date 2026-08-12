"""The refining loop — turning a general question into a research brief.

A vague question run through the analyst costs a full expensive answer to
discover it was the wrong question. So every question is scoped first: the
refiner reads the corpus, asks a few clarifying questions whose options name real
documents, rewrites the question into a brief the user can edit, and judges how
much of what the question needs the data room actually contains.

Context layout (matters for cost — this is the whole design):
    stage A "look"     → the analyst's *exact* cached prefix, verbatim
                         tools → system[0] instructions → system[1] corpus map
                         so the map is read at 0.1x rather than written at 2x,
                         and the analyst run that follows starts on a warm cache
    stage B "propose"  → no tools, no map, a small constrained call that emits
                         the round as JSON against ROUND_SCHEMA

Nothing here may change the bytes of that prefix. The read-only tool subset is
therefore enforced at dispatch, not by handing the model a different tool array,
and the refining directive rides in the first user turn — after the breakpoint.
"""

from __future__ import annotations

import asyncio
import json
import re
import time
import traceback
import uuid
from typing import AsyncIterator

from . import agent, budget, credentials, db, manifest, pricing, search, tools
from .config import settings
from .events import broker

# One round is at most this many tool turns. Scoping that reads more than a
# handful of search results has stopped refining and started answering.
REFINE_MAX_TURNS = 4

# Read-only, and none of them writes a file or runs code. Enforced at dispatch:
# editing tools.TOOLS would fork the prompt cache this module exists to reuse.
REFINE_TOOLS = frozenset({"document_card", "list_documents", "search_corpus"})

MAX_QUESTIONS = 4
MAX_OPTIONS = 5
MAX_EVIDENCE_PLAN = 12
MAX_ASSUMPTIONS = 8
MAX_MISSING = 6

# Stage A tool results are stored so the next round can rehydrate. The analyst
# clips at 200k because it is reading documents; the refiner only ever holds
# search hits and catalogue pages.
TRANSCRIPT_CLIP = 40_000

CONTROL_CHARS = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")

BANDS = (
    (70, "well covered"),
    (40, "partial — expect gaps"),
    (0, "thin — the data room probably cannot answer this"),
)


# --- the round schema ----------------------------------------------------
#
# A frozen module constant, never assembled per request: schemas are compiled
# server-side and cached, and rebuilding this from an f-string would pay that
# cost on every call. Same discipline as the sorted TOOLS.
#
# The supported subset has no minItems/maxItems/minLength, so every bound lives
# in the directive prose and is enforced by _coerce().

_OPTION = {
    "type": "object",
    "properties": {
        "label": {
            "type": "string",
            "description": "Short and concrete. Name the actual workstream, party, period "
            "or document — never a generic bucket.",
        },
        "detail": {
            "type": "string",
            "description": "One clause on what choosing this means for the analysis, "
            "e.g. '3 documents · FY22-FY24 · financial'.",
        },
        "evidence": {
            "type": "array",
            "items": {"type": "string"},
            "description": "doc_id or doc_id:anchor values from the corpus map or a search "
            "hit that make this option real. Empty only for a genuine "
            "'none of these' choice.",
        },
    },
    "required": ["label", "detail", "evidence"],
    "additionalProperties": False,
}

_QUESTION = {
    "type": "object",
    "properties": {
        "id": {"type": "string", "description": "q1, q2, … stable within this round."},
        "question": {
            "type": "string",
            "description": "One sentence, answerable from what the user knows about the "
            "deal rather than from the corpus.",
        },
        "why": {
            "type": "string",
            "description": "One clause on what changes in the analysis depending on the "
            "answer. Cite the documents that raise it, as [[doc_id:anchor]].",
        },
        "kind": {"type": "string", "enum": ["single", "multi", "free"]},
        "options": {"type": "array", "items": _OPTION},
        "default": {
            "type": "string",
            "description": "The option label to assume if the user skips this. Never empty.",
        },
    },
    "required": ["id", "question", "why", "kind", "options", "default"],
    "additionalProperties": False,
}

_BRIEF = {
    "type": "object",
    "properties": {
        "question": {
            "type": "string",
            "description": "The rewritten question: precise, self-contained, in the user's "
            "terms rather than yours.",
        },
        "covers": {"type": "array", "items": {"type": "string"}},
        "excludes": {"type": "array", "items": {"type": "string"}},
        "evidence_plan": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "doc_id": {"type": "string"},
                    "rel_path": {"type": "string"},
                    "why": {"type": "string"},
                },
                "required": ["doc_id", "rel_path", "why"],
                "additionalProperties": False,
            },
            "description": "Documents to start from. Every doc_id must exist in the corpus.",
        },
        "deliverable": {
            "type": "string",
            "description": "prose | table | memo(docx) | workbook(xlsx) | deck(pptx)",
        },
        "assumptions": {
            "type": "array",
            "items": {"type": "string"},
            "description": "What you assumed because it was not asked or not answered. The "
            "analyst will state these as caveats.",
        },
    },
    "required": ["question", "covers", "excludes", "evidence_plan", "deliverable",
                 "assumptions"],
    "additionalProperties": False,
}

_COMPLEXITY = {
    "type": "object",
    "properties": {
        "level": {"type": "string", "enum": ["simple", "moderate", "deep"]},
        "drivers": {
            "type": "array",
            "items": {"type": "string"},
            "description": "Why this question is as hard as it is. One clause each.",
        },
        "docs_to_read": {"type": "integer"},
        "needs_computation": {
            "type": "boolean",
            "description": "True when the answer needs figures computed from a workbook "
            "rather than read out of text.",
        },
        "recommended_effort": {"type": "string", "enum": ["low", "medium", "high", "xhigh", "max"]},
    },
    "required": ["level", "drivers", "docs_to_read", "needs_computation", "recommended_effort"],
    "additionalProperties": False,
}

_COVERAGE = {
    "type": "object",
    "properties": {
        "score": {
            "type": "integer",
            "description": "0-100. The share of what this question needs that the data room "
            "appears to contain. Not a guess at whether the answer will be "
            "liked — a claim about evidence. Be harsh: a corpus that mentions "
            "a topic is not a corpus that documents it.",
        },
        "reasons": {
            "type": "array",
            "items": {"type": "string"},
            "description": "What is there, with [[doc_id:anchor]] citations.",
        },
        "missing": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "what": {"type": "string", "description": "e.g. 'audited FY2025 accounts'"},
                    "why": {"type": "string", "description": "What it would settle."},
                    "searched": {"type": "string", "description": "The queries that came back "
                                                                  "empty."},
                },
                "required": ["what", "why", "searched"],
                "additionalProperties": False,
            },
            "description": "A document request list the user could send to the seller.",
        },
        "answer_shape": {
            "type": "string",
            "description": "What an answer could honestly look like at this coverage, e.g. "
            "'a directional read, not a number'.",
        },
    },
    "required": ["score", "reasons", "missing", "answer_shape"],
    "additionalProperties": False,
}

ROUND_SCHEMA = {
    "type": "object",
    "properties": {
        "ready": {
            "type": "boolean",
            "description": "True when the brief is precise enough to run and further "
            "questions would not change the work.",
        },
        "assessment": {
            "type": "string",
            "description": "Two sentences to the user: what you found in the data room that "
            "bears on their question, and what is still open.",
        },
        "coverage": _COVERAGE,
        "questions": {
            "type": "array",
            "items": _QUESTION,
            "description": "At most 4. Empty array when ready.",
        },
        "brief": _BRIEF,
        "complexity": _COMPLEXITY,
        "gaps": {
            "type": "array",
            "items": {"type": "string"},
            "description": "Things the question needs that the data room does not appear to "
            "contain. 'Not in the data room' is a finding.",
        },
    },
    "required": ["ready", "assessment", "coverage", "questions", "brief", "complexity", "gaps"],
    "additionalProperties": False,
}


# --- prompts -------------------------------------------------------------

DIRECTIVE = """You are refining a due diligence question before an expensive analyst run.
You are NOT answering it. Do not read documents; do not compute anything.

## What makes a good clarifying question
1. Ground every question and every option in what is *in this data room*. Name the real
   workstreams, parties, periods and documents you found, and carry their doc_ids in
   `evidence`. A generic consulting question ("what is your timeline?") is a failure.
2. Search before you ask. Two or three narrow search_corpus calls, or list_documents on the
   relevant workstream, cost far less than a mis-scoped analyst run.
3. Ask only what changes the work. If you can infer it from the corpus, infer it and record
   it under `assumptions` instead of spending one of your questions on it.
4. At most 4 questions, and every question carries a `default` — what to assume if the user
   skips it.
5. Set `ready: true` as soon as more questions would not change the work. Over-refining
   wastes the user's time and is the way this feature makes the tool worse.

## Coverage
Judge honestly how much of what the question needs is actually here. A corpus that mentions
a topic is not a corpus that documents it. Where something is missing, say what document
would settle it and what you searched — that list is a deliverable in its own right.

## The brief
Rewrite the question in the user's terms, precise and self-contained. Say what is in scope
and what is out, which documents to start from, and what was assumed."""

LOOK_TASK = """Search the corpus so your clarifying questions are grounded in what is
actually here. Run two or three narrow searches, or list_documents on the workstreams that
matter, and then stop — do not read documents and do not answer. When you have enough to
ask good questions, say so in one line."""

PROPOSE_SYSTEM = """You are refining a due diligence question before an expensive analyst run.
You are given the user's question, what searching the data room turned up, and any answers
the user has already given. Return the round as JSON.

Ground everything in the search findings you were given: every doc_id you name must appear
there or in the corpus summary. Inventing a document is the one unrecoverable error — the
brief becomes the analyst's instructions.""" + "\n\n" + DIRECTIVE


# --- the free probe ------------------------------------------------------


def probe(question: str, scope=None) -> dict:
    """Mechanical answerability signal. No API call — BM25 plus the rollup.

    Deliberately blunt. Its job is not to be precise, it is to bound what the
    model may claim about coverage: a question whose words retrieve nothing
    cannot be well covered, however confident the model sounds.

    Takes the same folder `scope` as the answer will: coverage has to be a claim
    about the corpus the run may actually read, or a question narrowed to one
    folder would be scored against documents it will never see.
    """
    m = manifest.build(scope)
    if not m["n_indexed"]:
        return {"score": 0, "hits": 0, "docs": 0, "workstreams": [], "top": [],
                "basis": "nothing is indexed yet"}

    hits = [h for h in search.search(question, limit=30, scope=scope) if not h.get("error")]
    docs = {h["doc_id"] for h in hits}
    workstreams = sorted({h["workstream"] for h in hits if h.get("workstream")})

    if not docs:
        score = 0
        basis = "no keyword match anywhere in the corpus"
    else:
        score = min(60, 12 * len(docs))
        if len(workstreams) >= 2:
            score += 10
        if len(hits) >= 10:
            score += 10
        # A keyword probe can never justify more than "probably". The model's
        # reading is what distinguishes 80 from 95; this only bounds it.
        score = min(80, score)
        basis = (
            f"{len(hits)} keyword hits across {len(docs)} document"
            f"{'s' if len(docs) != 1 else ''}"
            + (f" in {', '.join(workstreams)}" if workstreams else "")
        )

    return {
        "score": score,
        "hits": len(hits),
        "docs": len(docs),
        "workstreams": workstreams,
        "top": hits[:8],
        "basis": basis,
    }


def band(score: int) -> str:
    for floor, label in BANDS:
        if score >= floor:
            return label
    return BANDS[-1][1]


# --- cache-prefix accessors ---------------------------------------------
#
# Thin on purpose: they exist so a smoke test can assert the prefix really is
# the analyst's, which is the property the whole cost model rests on.


def system_for_refine(scope=None) -> list[dict]:
    return agent.system_blocks(scope)[0]


def tools_for_refine() -> list[dict]:
    return tools.TOOLS


# --- stage A: look -------------------------------------------------------


async def _look(
    messages: list[dict], *, out: dict, meter, payer, actor, scope=None
) -> AsyncIterator[dict]:
    """Bounded tool loop on the analyst's cached prefix.

    Yields UI events. Appends the assistant/tool turns to ``out['transcript']``
    and the search results to ``out['findings']`` — a mutable out-parameter
    rather than a return value so this stays an async generator the caller can
    stream, and a seam a smoke test can replace wholesale.
    """
    client = out["client"]
    model = settings.refiner_model

    for _ in range(REFINE_MAX_TURNS):
        async with client.messages.stream(
            model=model,
            max_tokens=8_000,
            system=system_for_refine(scope),
            tools=tools_for_refine(),
            thinking={"type": "adaptive", "display": "summarized"},
            output_config={"effort": settings.refiner_effort},
            messages=messages,
        ) as stream:
            async for event in stream:
                if event.type == "content_block_delta" and event.delta.type == "thinking_delta":
                    yield {"type": "thinking_delta", "text": event.delta.thinking}
                elif event.type == "content_block_start":
                    if event.content_block.type == "thinking":
                        yield {"type": "phase", "phase": "thinking"}
            response = await stream.get_final_message()

        cost = pricing.record(model, response.usage, meter=meter, cache_ttl_1h=True,
                              attribution=payer)
        yield {
            "type": "usage",
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

        messages.append(
            {"role": "assistant",
             "content": [b.model_dump(exclude_none=True) for b in response.content]}
        )
        tool_uses = [b for b in response.content if b.type == "tool_use"]
        if not tool_uses:
            return

        decision, note = await asyncio.to_thread(budget.turn_decision, actor, ref=payer.ref)
        if decision == budget.STOP:
            yield {"type": "status", "reason": "budget",
                   "message": f"{note} Scoping stopped here; the draft brief below is what "
                              f"it had."}
            return

        results: list[dict] = []
        for tu in tool_uses:
            payload = dict(tu.input or {})
            yield {
                "type": "tool_use",
                "id": tu.id,
                "name": tu.name,
                "input": payload,
                "label": payload.get("purpose") or payload.get("query")
                or payload.get("doc_id", ""),
            }
            if tu.name not in REFINE_TOOLS:
                result = {
                    "error": f"{tu.name} is not available while refining. Use search_corpus or "
                             f"list_documents to ground your questions, then stop."
                }
            else:
                try:
                    result = await asyncio.to_thread(tools.dispatch, tu.name, payload, scope)
                except Exception as exc:  # noqa: BLE001
                    result = {"error": f"{type(exc).__name__}: {exc}"}

            is_error = bool(isinstance(result, dict) and result.get("error"))
            if not is_error:
                out["findings"].append({"tool": tu.name, "input": payload, "result": result})
            yield {
                "type": "tool_result",
                "id": tu.id,
                "name": tu.name,
                "ok": not is_error,
                "summary": agent._summarise_result(tu.name, result),
                "payload": agent._preview(result),
            }
            results.append({
                "type": "tool_result",
                "tool_use_id": tu.id,
                "content": json.dumps(result, default=str)[:TRANSCRIPT_CLIP],
                "is_error": is_error,
            })
        messages.append({"role": "user", "content": results})


# --- stage B: propose ----------------------------------------------------


def _findings_digest(findings: list[dict], probed: dict) -> str:
    """What stage A saw, flattened small enough to send without the corpus map."""
    lines: list[str] = []
    seen: set[str] = set()

    def add_hit(h: dict) -> None:
        key = f"{h.get('doc_id')}:{h.get('anchor')}"
        if key in seen:
            return
        seen.add(key)
        lines.append(
            f"[{h.get('doc_id')}:{h.get('anchor')}] {h.get('workstream')}/{h.get('doc_type')}"
            f" · “{h.get('title')}”"
            + (f" · {h['period']}" if h.get("period") else "")
            + f" · {h.get('rel_path')}\n    {(h.get('snippet') or '')[:300]}"
        )

    for h in probed.get("top", []):
        add_hit(h)
    for f in findings:
        result = f.get("result") or {}
        for h in (result.get("hits") or [])[:12]:
            add_hit(h)
        for d in (result.get("documents") or [])[:20]:
            key = str(d.get("id"))
            if key in seen:
                continue
            seen.add(key)
            lines.append(
                f"[{d.get('id')}] {d.get('workstream')}/{d.get('doc_type')}"
                f" · “{d.get('title') or d.get('rel_path')}” · {d.get('rel_path')}"
            )
        # document_card spreads the `documents` row, so its identifier arrives
        # as `id` — reading `doc_id` here printed [None] and cost the propose
        # call the one document the refiner had bothered to open.
        card_id = result.get("id") or result.get("doc_id")
        if card_id and result.get("anchors") and str(card_id) not in seen:
            seen.add(str(card_id))
            lines.append(
                f"[{card_id}] card · {result.get('workstream')}/{result.get('doc_type')}"
                f" · “{result.get('title') or result.get('rel_path')}” · "
                f"{len(result.get('anchors') or [])} anchors"
            )
    return "\n".join(lines[:120]) or "(no search hits — the corpus may not cover this at all)"


def _rollup_text(m: dict) -> str:
    rollup = m.get("rollup") or {}
    lines = [f"{m['n_indexed']} indexed documents."]
    for ws in sorted(rollup):
        types = ", ".join(f"{t}×{n}" for t, n in sorted(rollup[ws].items(), key=lambda kv: -kv[1]))
        lines.append(f"- {ws} ({sum(rollup[ws].values())}): {types}")
    return "\n".join(lines)


async def _propose(
    *,
    client,
    question: str,
    findings_text: str,
    rollup_text: str,
    answers_text: str,
    last_round: bool,
    meter,
    payer,
) -> dict:
    """One constrained call — no tools, no corpus map. The JSON seam."""
    parts = [
        f"USER QUESTION:\n{question}",
        f"CORPUS SUMMARY:\n{rollup_text}",
        f"WHAT SEARCHING THE DATA ROOM TURNED UP:\n{findings_text}",
    ]
    if answers_text:
        parts.append(f"ANSWERS THE USER HAS ALREADY GIVEN:\n{answers_text}")
    if last_round:
        parts.append(
            "THIS IS THE LAST ROUND. Return ready: true with the best brief you can, and "
            "record any remaining uncertainty under brief.assumptions."
        )

    model = settings.refiner_model
    resp = await client.messages.create(
        model=model,
        max_tokens=8_000,
        system=PROPOSE_SYSTEM,
        output_config={"format": {"type": "json_schema", "schema": ROUND_SCHEMA}},
        messages=[{"role": "user", "content": "\n\n".join(parts)}],
    )
    pricing.record(model, resp.usage, meter=meter, attribution=payer)
    text = next((b.text for b in resp.content if b.type == "text"), "{}")
    return json.loads(text)


# --- coercion ------------------------------------------------------------


def _clean(value, limit: int = 600) -> str:
    return CONTROL_CHARS.sub("", str(value or ""))[:limit].strip()


def _known_docs(ids) -> set[str]:
    wanted = {str(i).partition(":")[0] for i in ids if i}
    if not wanted:
        return set()
    marks = ",".join("?" * len(wanted))
    rows = db.rows(f"SELECT id FROM documents WHERE id IN ({marks})", tuple(wanted))
    return {r["id"] for r in rows}


def _coerce(payload: dict, probed: dict) -> dict:
    """Clamp the model's round to something the UI and the analyst can trust.

    This is where the anti-hallucination guards live, because the schema cannot
    express them: a doc_id that does not resolve is dropped, and the coverage
    score is capped by what actually retrieved.
    """
    payload = payload if isinstance(payload, dict) else {}

    # Collect every doc_id the round mentions and resolve them in one query.
    brief_in = payload.get("brief") if isinstance(payload.get("brief"), dict) else {}
    coverage_in = payload.get("coverage") if isinstance(payload.get("coverage"), dict) else {}
    questions_in = payload.get("questions") if isinstance(payload.get("questions"), list) else []
    mentioned: list[str] = []
    for q in questions_in[:MAX_QUESTIONS]:
        for o in (q.get("options") or [])[:MAX_OPTIONS] if isinstance(q, dict) else []:
            mentioned += list(o.get("evidence") or []) if isinstance(o, dict) else []
    for e in (brief_in.get("evidence_plan") or [])[:MAX_EVIDENCE_PLAN]:
        if isinstance(e, dict):
            mentioned.append(str(e.get("doc_id") or ""))
    known = _known_docs(mentioned)

    questions: list[dict] = []
    for i, q in enumerate(questions_in[:MAX_QUESTIONS]):
        if not isinstance(q, dict) or not _clean(q.get("question")):
            continue
        options: list[dict] = []
        for o in (q.get("options") or [])[:MAX_OPTIONS]:
            if not isinstance(o, dict) or not _clean(o.get("label"), 200):
                continue
            claimed = [e for e in (o.get("evidence") or [])[:4] if str(e).strip()]
            resolved = [
                _clean(e, 160) for e in claimed if str(e).partition(":")[0] in known
            ]
            # An option that named documents and had every one of them dropped is
            # ungrounded: the model asserted evidence that does not exist, and a
            # clickable choice resting on nothing is worse than one fewer choice.
            # An option that never claimed any is a different thing — "an answer
            # in chat" has no document behind it and is not supposed to — so the
            # test is "claimed and lost", not "empty".
            if claimed and not resolved:
                continue
            options.append({
                "label": _clean(o.get("label"), 200),
                "detail": _clean(o.get("detail"), 200),
                "evidence": resolved,
            })
        kind = q.get("kind") if q.get("kind") in ("single", "multi", "free") else "single"
        if not options and kind != "free":
            kind = "free"
        # The default has to be one of the choices actually on offer. Dropping an
        # ungrounded option above and leaving the default naming it would make
        # "skip" assume exactly the thing the drop removed — and the answer would
        # carry it as a recorded assumption. A question with no options keeps the
        # model's text, because there is nothing for it to match.
        default = _clean(q.get("default"), 200)
        if options and default not in {o["label"] for o in options}:
            default = options[0]["label"]
        questions.append({
            "id": _clean(q.get("id"), 12) or f"q{i + 1}",
            "question": _clean(q.get("question"), 400),
            "why": _clean(q.get("why"), 400),
            "kind": kind,
            "options": options,
            "default": default or "your best judgement",
        })

    brief = {
        "question": _clean(brief_in.get("question"), 4000),
        "covers": [_clean(s, 300) for s in (brief_in.get("covers") or [])[:8] if _clean(s)],
        "excludes": [_clean(s, 300) for s in (brief_in.get("excludes") or [])[:8]
                     if _clean(s)],
        "evidence_plan": [
            {"doc_id": _clean(e.get("doc_id"), 40),
             "rel_path": _clean(e.get("rel_path"), 300),
             "why": _clean(e.get("why"), 300)}
            for e in (brief_in.get("evidence_plan") or [])[:MAX_EVIDENCE_PLAN]
            if isinstance(e, dict) and _clean(e.get("doc_id"), 40) in known
        ],
        "deliverable": _clean(brief_in.get("deliverable"), 60) or "prose",
        "assumptions": [_clean(a, 300) for a in (brief_in.get("assumptions") or [])
                        [:MAX_ASSUMPTIONS] if _clean(a)],
    }

    cx_in = payload.get("complexity") if isinstance(payload.get("complexity"), dict) else {}
    try:
        docs_to_read = max(0, min(200, int(cx_in.get("docs_to_read") or 0)))
    except (TypeError, ValueError):
        docs_to_read = 0
    complexity = {
        "level": cx_in.get("level") if cx_in.get("level") in ("simple", "moderate", "deep")
        else "moderate",
        "drivers": [_clean(d, 200) for d in (cx_in.get("drivers") or [])[:5] if _clean(d)],
        "docs_to_read": docs_to_read,
        "needs_computation": bool(cx_in.get("needs_computation")),
        "recommended_effort": cx_in.get("recommended_effort")
        if cx_in.get("recommended_effort") in ("low", "medium", "high", "xhigh", "max")
        else "high",
    }

    # Coverage: the model proposes, the probe caps. A model asked to score
    # itself will drift upward, so the ceiling is mechanical and not promptable.
    try:
        score = int(coverage_in.get("score"))
    except (TypeError, ValueError):
        score = probed.get("score", 0)
    score = max(0, min(100, score))
    if probed.get("docs", 0) == 0:
        score = min(score, 15)
    elif probed.get("docs", 0) < 3:
        score = min(score, 55)
    score = min(score, probed.get("score", 0) + 25)

    coverage = {
        "score": score,
        "band": band(score),
        "reasons": [_clean(r, 300) for r in (coverage_in.get("reasons") or [])[:5] if _clean(r)],
        "missing": [
            {"what": _clean(mi.get("what"), 200),
             "why": _clean(mi.get("why"), 300),
             "searched": _clean(mi.get("searched"), 200)}
            for mi in (coverage_in.get("missing") or [])[:MAX_MISSING]
            if isinstance(mi, dict) and _clean(mi.get("what"), 200)
        ],
        "answer_shape": _clean(coverage_in.get("answer_shape"), 300),
        "probe": {k: probed.get(k) for k in ("score", "hits", "docs", "workstreams", "basis")},
    }

    return {
        "ready": bool(payload.get("ready")) or not questions,
        "assessment": _clean(payload.get("assessment"), 800),
        "coverage": coverage,
        "questions": questions,
        "brief": brief,
        "complexity": complexity,
        "gaps": [_clean(g, 300) for g in (payload.get("gaps") or [])[:6] if _clean(g)],
    }


def _stub_round(question: str, probed: dict) -> dict:
    """What a round becomes when the model's output cannot be parsed twice."""
    return _coerce(
        {
            "ready": True,
            "assessment": "Scoping could not be completed, so the question will run as typed.",
            "coverage": {"score": probed.get("score", 0), "reasons": [], "missing": [],
                         "answer_shape": ""},
            "questions": [],
            "brief": {"question": question, "covers": [], "excludes": [],
                      "evidence_plan": [], "deliverable": "prose",
                      "assumptions": ["Scoping failed; nothing was narrowed."]},
            "complexity": {"level": "moderate", "drivers": [], "docs_to_read": 0,
                           "needs_computation": False, "recommended_effort": "high"},
            "gaps": [],
        },
        probed,
    )


# --- the brief handed to the analyst -------------------------------------


def render_brief(brief: dict) -> str:
    """Flatten the accepted brief into the text the analyst is asked.

    Wrapped in a tag so the analyst reads it as scope rather than as a claim to
    cite: it is instructions, and it must not turn up in the citation panel.
    """
    lines = ["<research_brief>", f"QUESTION: {brief.get('question', '')}"]
    if brief.get("covers"):
        lines.append("IN SCOPE:")
        lines += [f"  - {s}" for s in brief["covers"]]
    if brief.get("excludes"):
        lines.append("OUT OF SCOPE:")
        lines += [f"  - {s}" for s in brief["excludes"]]
    if brief.get("evidence_plan"):
        lines.append("START FROM:")
        lines += [f"  - {e['doc_id']} {e['rel_path']} — {e['why']}" for e in brief["evidence_plan"]]
    if brief.get("deliverable"):
        lines.append(f"DELIVERABLE: {brief['deliverable']}")
    if brief.get("assumptions"):
        lines.append("ASSUMPTIONS (recorded during refining, unverified):")
        lines += [f"  - {a}" for a in brief["assumptions"]]
    lines.append("</research_brief>")
    return "\n".join(lines)


def propose_model(coverage: dict, complexity: dict) -> dict:
    """The model this run should use, from the complexity read.

    Coverage overrides it downward: paying for the strongest model on a question
    the corpus cannot answer is the exact waste this feature exists to prevent.
    """
    mapping = settings.complexity_models
    level = complexity.get("level", "moderate")
    model = mapping.get(level) or settings.analyst_model
    effort = complexity.get("recommended_effort", "high")
    note = f"{level} question"
    if coverage.get("score", 0) < 40:
        model = mapping.get("simple") or model
        effort = "medium" if effort in ("high", "xhigh", "max") else effort
        note = f"{level} question, but coverage is thin — proposing a cheaper run"
    return {"model": model, "effort": effort, "note": note}


def estimate_cost(model: str, complexity: dict, scope=None) -> float:
    """Rough cost of the run, for the brief. An estimate, not a quote."""
    m = manifest.build(scope)
    price_in, price_out = pricing.PRICES.get(model, (5.0, 25.0))
    turns = max(2, min(30, complexity.get("docs_to_read") or 4))
    map_cost = m["approx_tokens"] * price_in * pricing.CACHE_READ_MULTIPLIER * turns
    output_cost = 2_000 * price_out * turns
    return round((map_cost + output_cost) / 1_000_000, 3)


# --- persistence ---------------------------------------------------------


def rounds(refine_id: str) -> list[dict]:
    out = []
    for r in db.rows(
        "SELECT * FROM refine_rounds WHERE refine_id=? ORDER BY round", (refine_id,)
    ):
        d = dict(r)
        for key in ("answers", "payload", "transcript", "usage"):
            try:
                d[key] = json.loads(d[key] or "null")
            except (json.JSONDecodeError, TypeError):
                d[key] = None
        out.append(d)
    return out


def load(refine_id: str, actor: str | None = None) -> dict | None:
    """The session as the UI needs it, or None when it is not this user's."""
    found = rounds(refine_id)
    if not found:
        return None
    if actor and found[0].get("actor") and found[0]["actor"] != actor:
        return None
    last = found[-1]
    payload = last.get("payload") or {}
    try:
        scope = json.loads(found[0].get("scope") or "[]")
    except (json.JSONDecodeError, TypeError):
        scope = []
    proposal = propose_model(payload.get("coverage") or {}, payload.get("complexity") or {})
    return {
        "refine_id": refine_id,
        "question": last["question"],
        "scope": scope,
        "round": last["round"],
        "final": last["round"] >= settings.refine_max_rounds,
        "ready": bool(last["ready"]),
        "answered": [r.get("answers") for r in found],
        **payload,
        "brief_text": render_brief(payload.get("brief") or {}),
        "proposal": {
            **proposal,
            "estimated_cost_usd": estimate_cost(
                proposal["model"], payload.get("complexity") or {}, scope
            ),
        },
    }


def session_scope(refine_id: str, actor: str | None = None) -> tuple[bool, list[str]]:
    """The folders a refinement session was started with.

    Returns ``(found, scope)``. `found` is False when the id is unknown or
    belongs to another account, which is the caller's cue to ignore it rather
    than to trust whatever came with it.
    """
    row = db.one(
        "SELECT scope, actor FROM refine_rounds WHERE refine_id=? ORDER BY round LIMIT 1",
        (refine_id,),
    )
    if row is None or (actor and row["actor"] and row["actor"] != actor):
        return False, []
    try:
        stored = json.loads(row["scope"] or "[]")
    except (json.JSONDecodeError, TypeError):
        return True, []
    return True, [p for p in stored if isinstance(p, str) and p]


def save_round(
    refine_id: str,
    *,
    round_no: int,
    question: str,
    answers: list | None,
    payload: dict,
    transcript: list,
    usage: dict,
    actor: str | None,
    scope: list[str] | None = None,
) -> None:
    db.execute(
        "INSERT INTO refine_rounds(id, refine_id, round, question, scope, answers, payload,"
        " transcript, ready, coverage, usage, model, effort, actor, created_at)"
        " VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            uuid.uuid4().hex[:12], refine_id, round_no, question,
            json.dumps(scope or []),
            json.dumps(answers or [], default=str), json.dumps(payload, default=str),
            json.dumps(_sealed(transcript), default=str),
            1 if payload.get("ready") else 0,
            (payload.get("coverage") or {}).get("score"),
            json.dumps(usage), settings.refiner_model, settings.refiner_effort,
            actor, time.time(),
        ),
    )


def link_answer(refine_id: str, qa_id: str, actor: str | None = None) -> None:
    """Tie every round of a session to the answer its brief produced.

    The id arrives on the ask body, so it is whatever the client sent. Without
    the actor check one account could stamp another account's refinement rows
    with its own answer — not a data leak, but a corrupted audit trail, and the
    audit trail is most of the point of storing these at all.
    """
    db.execute(
        "UPDATE refine_rounds SET qa_id=? WHERE refine_id=?"
        " AND (actor IS ? OR actor = ?)",
        (qa_id, refine_id, actor, actor),
    )


# --- the loop ------------------------------------------------------------


def _sealed(messages: list[dict]) -> list[dict]:
    """A transcript the next round can safely resume from.

    A round can end holding an assistant turn whose tool_use blocks were never
    answered — the budget stopped it between the model's request and the
    dispatch. Replaying that turn is a request the API rejects, so the unanswered
    turn is dropped rather than carried forward.
    """
    out = list(messages)
    while out and out[-1].get("role") == "assistant" and any(
        isinstance(b, dict) and b.get("type") == "tool_use" for b in out[-1].get("content") or []
    ):
        out.pop()
    return out


def _resume(messages: list[dict], text: str) -> None:
    """Append a user turn, or fold it into the trailing one.

    A round that used its last tool turn ends on a user message of tool_results.
    Two user turns in a row is not a conversation the API accepts, so the text
    joins that turn — after the tool results, where a text block belongs.
    """
    if not messages or messages[-1].get("role") != "user":
        messages.append({"role": "user", "content": text})
        return
    content = messages[-1].get("content")
    if isinstance(content, list):
        content.append({"type": "text", "text": text})
    else:
        messages[-1]["content"] = f"{content}\n\n{text}"


def _render_answers(prior_questions: list[dict], answers: list[dict] | None) -> str:
    """The user's answers as text, with skipped questions filled from defaults."""
    by_id = {a.get("id"): a for a in (answers or []) if isinstance(a, dict)}
    lines: list[str] = []
    for q in prior_questions:
        given = by_id.get(q["id"]) or {}
        value = given.get("value")
        if isinstance(value, list):
            value = ", ".join(str(v) for v in value if str(v).strip())
        value = _clean(value, 600)
        if given.get("skipped") or not value:
            lines.append(
                f"- {q['question']}\n  SKIPPED — assume your stated default "
                f"({q['default']}) and record it under assumptions."
            )
        else:
            lines.append(f"- {q['question']}\n  {value}")
    return "\n".join(lines)


async def run(
    question: str,
    *,
    refine_id: str | None = None,
    answers: list[dict] | None = None,
    actor: str | None = None,
    scope: list[str] | None = None,
) -> AsyncIterator[dict]:
    """One refinement round. Yields the same event vocabulary as agent.ask."""
    started = time.time()
    meter = pricing.Meter()

    prior = rounds(refine_id) if refine_id else []
    if refine_id and not prior:
        yield {"type": "error", "message": "That refining session has expired. Ask again."}
        return
    if prior and prior[0].get("actor") and actor and prior[0]["actor"] != actor:
        yield {"type": "error", "message": "That refining session belongs to another account."}
        return

    refine_id = refine_id or uuid.uuid4().hex[:12]
    round_no = (prior[-1]["round"] + 1) if prior else 1
    # The original question is the one the session started with; later rounds
    # only ever carry answers.
    question = prior[0]["question"] if prior else question
    # The folders are chosen once, when the session starts. Later rounds and the
    # run that follows inherit them: a brief written against the whole library
    # and then answered inside one folder would cite documents the answer is not
    # allowed to open.
    if prior:
        try:
            scope = json.loads(prior[0].get("scope") or "[]")
        except (json.JSONDecodeError, TypeError):
            scope = []
    scope = [p for p in (scope or []) if p]
    payer = pricing.Attribution(actor, "ask", "refiner", refine_id)

    try:
        await asyncio.to_thread(budget.require, actor, "ask")
    except budget.BudgetExceeded as exc:
        yield {"type": "error", "message": str(exc), "reason": "budget",
               "budget": await asyncio.to_thread(budget.status, actor)}
        return

    m = manifest.build(scope)
    folders = ", ".join(p.rstrip("/").rsplit("/", 1)[-1] for p in scope)
    if m["n_indexed"] == 0:
        yield {
            "type": "error",
            "message": (
                f"No indexed documents in {folders}. Widen the folders, or run the Sweep "
                "if they were only just ingested."
                if scope
                else "No documents are indexed yet. Point the app at a corpus, run Ingest, "
                "then run the Sweep."
            ),
        }
        return

    probed = await asyncio.to_thread(probe, question, scope)
    yield {
        "type": "refine_probe",
        "refine_id": refine_id,
        "round": round_no,
        "score": probed["score"],
        "band": band(probed["score"]),
        "basis": probed["basis"],
        "provisional": True,
    }
    yield {
        "type": "status",
        "message": f"corpus map: {m['n_indexed']} documents, ~{m['approx_tokens']:,} tokens"
                   f" ({m['mode']} mode)" + (f" · scoped to {folders}" if scope else ""),
        "refine_id": refine_id,
        "scope": scope,
    }

    last_round = round_no >= settings.refine_max_rounds
    client = credentials.get_client()
    out = {"client": client, "findings": [], "transcript": []}

    try:
        if prior:
            messages = _sealed(list(prior[-1].get("transcript") or []))
            answers_text = _render_answers((prior[-1].get("payload") or {}).get("questions") or [],
                                           answers)
            _resume(
                messages,
                f"The user answered:\n{answers_text or '(nothing — use your defaults)'}\n\n"
                f"Search again only if these answers point somewhere you have not looked, "
                f"then stop.",
            )
        else:
            answers_text = ""
            hint = ""
            if m["mode"] == "rollup":
                hint = ("\nThe corpus map is a rollup and lists no documents, so call "
                        "list_documents on the relevant workstream before asking anything.")
            messages = [{
                "role": "user",
                "content": f"<refining_task>\n{DIRECTIVE}\n\n{LOOK_TASK}{hint}\n</refining_task>"
                           f"\n\nUSER QUESTION:\n{question}",
            }]

        async for event in _look(
            messages, out=out, meter=meter, payer=payer, actor=actor, scope=scope
        ):
            if event.get("type") == "error":
                yield event
                return
            yield event

        yield {"type": "phase", "phase": "writing"}
        findings_text = _findings_digest(out["findings"], probed)
        try:
            raw = await _propose(
                client=client, question=question, findings_text=findings_text,
                rollup_text=_rollup_text(m), answers_text=answers_text,
                last_round=last_round, meter=meter, payer=payer,
            )
            payload = _coerce(raw, probed)
        except (json.JSONDecodeError, KeyError, TypeError) as exc:
            broker.log(f"refine: unparseable round, retrying once ({exc})", level="warn",
                       source="refine")
            try:
                raw = await _propose(
                    client=client, question=question, findings_text=findings_text,
                    rollup_text=_rollup_text(m), answers_text=answers_text,
                    last_round=True, meter=meter, payer=payer,
                )
                payload = _coerce(raw, probed)
            except Exception:  # noqa: BLE001
                payload = _stub_round(question, probed)

        if last_round:
            payload["ready"] = True
            payload["questions"] = []
        if not payload["brief"]["question"]:
            payload["brief"]["question"] = question

        await asyncio.to_thread(
            save_round, refine_id, round_no=round_no, question=question, answers=answers,
            scope=scope,
            payload=payload, transcript=messages, usage=meter.snapshot(), actor=actor,
        )

        # Coverage first: a user looking at "22% — thin" should be able to walk
        # away before investing in four answers.
        yield {"type": "refine_coverage", "refine_id": refine_id, "round": round_no,
               "provisional": False, **payload["coverage"]}
        yield {
            "type": "refine_round",
            "refine_id": refine_id,
            "round": round_no,
            "final": last_round,
            "ready": payload["ready"],
            "assessment": payload["assessment"],
            "gaps": payload["gaps"],
            "n_questions": len(payload["questions"]),
        }
        for q in payload["questions"]:
            yield {"type": "refine_question", "refine_id": refine_id, **q}

        proposal = propose_model(payload["coverage"], payload["complexity"])
        yield {
            "type": "refine_brief",
            "refine_id": refine_id,
            "round": round_no,
            "brief": payload["brief"],
            "brief_text": render_brief(payload["brief"]),
            "complexity": payload["complexity"],
            "proposal": {**proposal,
                         "estimated_cost_usd": estimate_cost(
                             proposal["model"], payload["complexity"], scope)},
        }

        usage = meter.snapshot()
        duration = time.time() - started
        broker.log(
            f"Refined a question in {duration:.0f}s · round {round_no} · "
            f"{len(payload['questions'])} questions · coverage "
            f"{payload['coverage']['score']}% · ${usage['cost_usd']:.3f}",
            level="info",
        )
        yield {
            "type": "refine_done",
            "refine_id": refine_id,
            "round": round_no,
            "ready": payload["ready"],
            "needs_answers": bool(payload["questions"]),
            "usage": usage,
            "duration_s": round(duration, 1),
            "budget": await asyncio.to_thread(budget.status, actor),
        }
    except Exception as exc:  # noqa: BLE001
        broker.log(
            f"refine failed: {type(exc).__name__}: {exc}",
            level="error",
            source="refine",
            context={"exc_type": type(exc).__name__, "traceback": traceback.format_exc(limit=12)},
        )
        yield {"type": "error", "message": f"{type(exc).__name__}: {exc}"}
    finally:
        await client.close()
