"""Citation verification.

Splits the answer into cited claims, re-reads each cited span from the corpus,
and asks a small model whether the span actually supports the claim. This is
the difference between an assistant your lawyers will use and one they won't.
"""

from __future__ import annotations

import asyncio
import json
import re
from typing import AsyncIterator

from anthropic import AsyncAnthropic

from . import db, extract, pricing
from .config import settings

CITATION_RE = re.compile(r"\[\[([0-9a-f]{6,32}):([^\]\s]{1,120})\]\]")
SENTENCE_SPLIT = re.compile(r"(?<=[.!?])\s+(?=[A-Z(«\"'\d])|\n+")

VERDICT_SCHEMA = {
    "type": "object",
    "properties": {
        "verdict": {"type": "string", "enum": ["supported", "partial", "unsupported", "unclear"]},
        "note": {"type": "string", "description": "One sentence. Empty string if fully supported."},
    },
    "required": ["verdict", "note"],
    "additionalProperties": False,
}

SYSTEM = """You check citations for a due diligence report. You are given one claim and the exact
source text it cites. Decide whether the source text supports the claim.
- supported: the source states it, or it follows directly from the source.
- partial: partly supported; a number, date, scope or qualifier does not match.
- unsupported: the source does not state this, or contradicts it.
- unclear: the source excerpt is too garbled or truncated to tell.
Be strict about numbers and dates. Do not use outside knowledge. Keep `note` to one sentence."""

MAX_CLAIMS = 40
SPAN_CHARS = 6000


def extract_claims(answer: str) -> list[dict]:
    claims: list[dict] = []
    for raw in SENTENCE_SPLIT.split(answer or ""):
        sentence = raw.strip()
        if not sentence:
            continue
        cites = CITATION_RE.findall(sentence)
        if not cites:
            continue
        clean = CITATION_RE.sub("", sentence).strip(" -•\t")
        if len(clean) < 15:
            continue
        claims.append(
            {
                "claim": clean,
                "citations": [f"{d}:{a}" for d, a in cites],
            }
        )
        if len(claims) >= MAX_CLAIMS:
            break
    return claims


def span_for(citation: str) -> tuple[str, str]:
    doc_id, _, anchor = citation.partition(":")
    row = db.one("SELECT char_start, char_end FROM units WHERE doc_id=? AND anchor=?",
                 (doc_id, anchor))
    doc = db.one("SELECT title, rel_path FROM documents WHERE id=?", (doc_id,))
    label = (doc["title"] if doc else None) or (doc["rel_path"] if doc else citation)
    if row is None:
        return label, ""
    start = row["char_start"]
    end = min(row["char_end"], start + SPAN_CHARS)
    return label, extract.read_mirror(doc_id, start, end)


async def _check(client: AsyncAnthropic, claim: dict, meter: pricing.Meter | None) -> dict:
    parts = []
    resolved = 0
    for c in claim["citations"][:4]:
        label, text = span_for(c)
        if text:
            resolved += 1
        parts.append(f"--- source {c} ({label}) ---\n{text or '[citation does not resolve]'}")
    if resolved == 0:
        return {**claim, "verdict": "unsupported", "note": "citation does not resolve to any indexed unit"}

    prompt = f"CLAIM:\n{claim['claim']}\n\n" + "\n\n".join(parts)
    resp = await client.messages.create(
        model=settings.verifier_model,
        max_tokens=400,
        system=SYSTEM,
        output_config={"format": {"type": "json_schema", "schema": VERDICT_SCHEMA}},
        messages=[{"role": "user", "content": prompt}],
    )
    pricing.record(settings.verifier_model, resp.usage, meter=meter)
    text = next((b.text for b in resp.content if b.type == "text"), "{}")
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError:
        parsed = {"verdict": "unclear", "note": "verifier returned unparseable output"}
    return {**claim, "verdict": parsed.get("verdict", "unclear"), "note": parsed.get("note", "")}


async def verify_answer(
    answer: str, *, meter: pricing.Meter | None = None
) -> AsyncIterator[dict]:
    claims = extract_claims(answer)
    if not claims:
        return
    client = AsyncAnthropic()
    sem = asyncio.Semaphore(settings.verify_concurrency)

    async def one(claim: dict) -> dict:
        async with sem:
            try:
                return await _check(client, claim, meter)
            except Exception as exc:  # noqa: BLE001
                return {**claim, "verdict": "unclear", "note": f"{type(exc).__name__}: {exc}"}

    try:
        for coro in asyncio.as_completed([one(c) for c in claims]):
            yield await coro
    finally:
        await client.close()
