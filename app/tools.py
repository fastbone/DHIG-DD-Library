"""The analyst's tool surface.

Definitions are kept in a fixed, sorted order — the tool block renders at
position 0 of every request, so reordering it would invalidate the prompt cache
that makes the corpus map affordable.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

from . import db, docgen, extract, search
from .config import WORKSTREAMS, settings

MAX_READ_CHARS = 40_000

TOOLS: list[dict] = [
    {
        "name": "create_deliverable",
        "description": (
            "Write a deliverable to disk and return a download link. Use for memos, red-flag "
            "reports, findings tables, Q&A logs and management presentations. Supply a "
            "declarative spec — do not write file-format code yourself.\n"
            "kind='docx'|'xlsx'|'pptx'|'md'. For docx/md use `blocks`; for xlsx use `sheets`; "
            "for pptx use `slides`. Keep inline citations ([[doc_id:anchor]]) in the text you "
            "write into the document — the reader needs them as much as the chat user does."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "kind": {"type": "string", "enum": ["docx", "xlsx", "pptx", "md"]},
                "filename": {"type": "string"},
                "title": {"type": "string"},
                "blocks": {"type": "array", "items": docgen.BLOCK_SCHEMA},
                "sheets": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "name": {"type": "string"},
                            "header": {"type": "array", "items": {"type": "string"}},
                            "rows": {
                                "type": "array",
                                "items": {"type": "array", "items": {"type": "string"}},
                            },
                        },
                        "required": ["name"],
                    },
                },
                "slides": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "title": {"type": "string"},
                            "bullets": {"type": "array", "items": {"type": "string"}},
                            "notes": {"type": "string"},
                        },
                        "required": ["title"],
                    },
                },
            },
            "required": ["kind", "filename"],
        },
    },
    {
        "name": "document_card",
        "description": (
            "Full catalogue card for one document plus its complete anchor list, so you can see "
            "how it is structured (pages / slides / sheets) before reading it."
        ),
        "input_schema": {
            "type": "object",
            "properties": {"doc_id": {"type": "string"}},
            "required": ["doc_id"],
        },
    },
    {
        "name": "list_documents",
        "description": (
            "Page through the catalogue with filters. Use this when the corpus map in your system "
            "prompt is a rollup rather than a full listing, or to enumerate everything in one "
            "workstream, everything flagged, or the near-duplicate version families."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "Substring over path/title/summary/parties"},
                "workstream": {"type": "string", "enum": WORKSTREAMS},
                "doc_type": {"type": "string"},
                "flagged": {"type": "boolean"},
                "duplicates": {"type": "string", "enum": ["all", "near_only", "multi_path"]},
                "limit": {"type": "integer"},
                "offset": {"type": "integer"},
            },
            "required": [],
        },
    },
    {
        "name": "read_document",
        "description": (
            "Read the extracted text of a document. Pass `anchor` (e.g. 'p14', 'slide3', "
            "'Summary!A1:H240') to jump to one unit, or `char_start` to page through. Returns "
            "anchor markers inline so you can cite precisely."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "doc_id": {"type": "string"},
                "anchor": {"type": "string"},
                "char_start": {"type": "integer"},
                "max_chars": {"type": "integer", "description": f"default 12000, max {MAX_READ_CHARS}"},
            },
            "required": ["doc_id"],
        },
    },
    {
        "name": "run_python",
        "description": (
            "Run Python over the ORIGINAL files (not the extracted text). This is how you do "
            "arithmetic: load a workbook with pandas/openpyxl and compute, never eyeball figures "
            "out of extracted text. A module `dd` is importable in the working directory:\n"
            "  dd.path('<doc_id>') -> absolute path to the original file\n"
            "  dd.find('revenue model') -> [(doc_id, rel_path), …] catalogue substring search\n"
            "  dd.text('<doc_id>') -> extracted text mirror as a string\n"
            "pandas, openpyxl, pymupdf, python-docx and python-pptx are available. Print what you "
            "want to see; stdout is truncated at 20k characters."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "code": {"type": "string"},
                "purpose": {"type": "string", "description": "One line, shown to the user"},
            },
            "required": ["code", "purpose"],
        },
    },
    {
        "name": "search_corpus",
        "description": (
            "Keyword (BM25) search across every extracted page, slide and sheet. Returns "
            "citations of the form doc_id:anchor with snippets. Prefer several narrow searches "
            "over one broad one; quote multi-word phrases."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "query": {"type": "string"},
                "workstream": {"type": "string", "enum": WORKSTREAMS},
                "doc_type": {"type": "string"},
                "doc_id": {"type": "string", "description": "Restrict to one document"},
                "limit": {"type": "integer", "description": "default 20, max 60"},
            },
            "required": ["query"],
        },
    },
]

TOOL_NAMES = [t["name"] for t in TOOLS]

PRELUDE = '''"""Helpers for the due-diligence corpus (auto-generated)."""
import json, pathlib
_MAP = json.loads(pathlib.Path(__file__).with_name("corpus.json").read_text())

def path(doc_id):
    entry = _MAP.get(doc_id)
    if entry is None:
        raise KeyError(f"unknown doc_id {doc_id!r}")
    return entry["abs_path"]

def rel(doc_id):
    return _MAP[doc_id]["rel_path"]

def text(doc_id):
    return pathlib.Path(_MAP[doc_id]["mirror"]).read_text(encoding="utf-8", errors="replace")

def find(needle):
    n = needle.lower()
    return [(k, v["rel_path"]) for k, v in _MAP.items()
            if n in v["rel_path"].lower() or n in (v.get("title") or "").lower()]

def all_docs():
    return dict(_MAP)
'''


OUT_OF_SCOPE = (
    "That document is outside the folders selected for this question. "
    "Say so rather than guessing at its contents — the reader chose the folders."
)


def _corpus_map(scope=None) -> dict:
    """The doc_id → paths map handed to run_python.

    Scoping it is how the scope survives `run_python`: `dd.path`, `dd.find`,
    `dd.text` and `dd.all_docs` all read from this map, so a map that stops at the
    selected folders stops all four. Note this narrows what the *tools* offer, not
    what the subprocess could reach — the sandbox has a filesystem, and that has
    always been a trust boundary with the model rather than a wall (see README).
    """
    scope_sql, params = search.scope_clause(scope)
    where = f" WHERE {scope_sql}" if scope_sql else ""
    out = {}
    for r in db.rows(
        f"SELECT id, abs_path, rel_path, title FROM documents{where}", params
    ):
        out[r["id"]] = {
            "abs_path": r["abs_path"],
            "rel_path": r["rel_path"],
            "title": r["title"],
            "mirror": str(settings.text_path(r["id"])),
        }
    return out


def run_python(code: str, scope=None) -> dict:
    if not settings.enable_python_tool:
        return {"error": "run_python is disabled (DD_ENABLE_PYTHON=0)"}
    with tempfile.TemporaryDirectory(prefix="dd-py-") as tmp:
        tmpdir = Path(tmp)
        (tmpdir / "dd.py").write_text(PRELUDE)
        (tmpdir / "corpus.json").write_text(json.dumps(_corpus_map(scope)))
        (tmpdir / "script.py").write_text(code)
        env = {
            "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
            "HOME": str(tmpdir),
            "PYTHONPATH": str(tmpdir),
            "MPLBACKEND": "Agg",
            "LANG": "C.UTF-8",
        }
        try:
            # -s only (no user site-packages). Not -I: that also drops the script
            # directory from sys.path, which would make `import dd` fail.
            proc = subprocess.run(  # noqa: S603 — see README security note
                [sys.executable, "-s", "script.py"],
                cwd=tmpdir,
                env=env,
                capture_output=True,
                text=True,
                timeout=settings.python_timeout_s,
            )
        except subprocess.TimeoutExpired:
            return {"error": f"timed out after {settings.python_timeout_s}s"}
        out = (proc.stdout or "")[:20_000]
        err = (proc.stderr or "")[-4_000:]
        return {"exit_code": proc.returncode, "stdout": out, "stderr": err}


def read_document(doc_id: str, anchor: str | None, char_start: int, max_chars: int) -> dict:
    doc = db.doc_dict(db.one("SELECT * FROM documents WHERE id=?", (doc_id,)))
    if doc is None:
        return {"error": f"unknown doc_id {doc_id}"}
    max_chars = max(500, min(int(max_chars or 12_000), MAX_READ_CHARS))
    if anchor:
        unit = db.one(
            "SELECT char_start, char_end FROM units WHERE doc_id=? AND anchor=?", (doc_id, anchor)
        )
        if unit is None:
            anchors = [r["anchor"] for r in db.rows(
                "SELECT anchor FROM units WHERE doc_id=? ORDER BY ordinal LIMIT 80", (doc_id,))]
            return {"error": f"unknown anchor {anchor!r}", "available_anchors": anchors}
        char_start = max(0, unit["char_start"] - 200)
        max_chars = min(MAX_READ_CHARS, max(max_chars, unit["char_end"] - unit["char_start"] + 400))
    char_start = max(0, int(char_start or 0))
    body = extract.read_mirror(doc_id, char_start, char_start + max_chars)
    return {
        "doc_id": doc_id,
        "title": doc.get("title") or doc["rel_path"],
        "rel_path": doc["rel_path"],
        "workstream": doc.get("workstream"),
        "char_start": char_start,
        "char_end": char_start + len(body),
        "total_chars": doc.get("n_chars") or 0,
        "more_available": char_start + len(body) < (doc.get("n_chars") or 0),
        "text": body,
    }


def document_card(doc_id: str, scope=None) -> dict:
    doc = db.doc_dict(db.one("SELECT * FROM documents WHERE id=?", (doc_id,)))
    if doc is None:
        return {"error": f"unknown doc_id {doc_id}"}
    anchors = [
        {"anchor": r["anchor"], "kind": r["kind"], "chars": r["char_end"] - r["char_start"]}
        for r in db.rows(
            "SELECT anchor, kind, char_start, char_end FROM units WHERE doc_id=?"
            " ORDER BY ordinal LIMIT 400",
            (doc_id,),
        )
    ]
    # The version family is a list of *other documents*, so it has to obey the
    # scope: handing over the id and title of a near-duplicate the reader excluded
    # gives the model something to cite that it is not allowed to open, which is
    # exactly the failure gating read_document is meant to prevent.
    dupe_sql, dupe_params = search.scope_clause(scope)
    near = [
        {"doc_id": r["id"], "rel_path": r["rel_path"], "title": r["title"]}
        for r in db.rows(
            "SELECT id, rel_path, title FROM documents WHERE dupe_group IS NOT NULL"
            " AND dupe_group=(SELECT dupe_group FROM documents WHERE id=?) AND id!=?"
            + (f" AND {dupe_sql}" if dupe_sql else ""),
            (doc_id, doc_id, *dupe_params),
        )
    ]
    # Not scoped, deliberately: these are other filings of *this* document, which
    # is in scope by the time we get here. They expose no further content and no
    # other id, and they are how the reader learns the same file sits in two
    # places — which is a finding, not a leak.
    also_at = [
        r["rel_path"]
        for r in db.rows(
            "SELECT rel_path FROM occurrences WHERE doc_id=? AND rel_path!=? ORDER BY rel_path",
            (doc_id, doc["rel_path"]),
        )
    ]
    for k in ("abs_path", "sha256", "created_at", "extracted_at", "carded_at"):
        doc.pop(k, None)
    return {
        **doc,
        "anchors": anchors,
        "near_duplicates": near,
        "identical_copies_at": also_at,
    }


def dispatch(name: str, payload: dict, scope=None) -> dict:
    """Run one tool call, restricted to `scope` if one is set.

    The scope is enforced here rather than only described in the system prompt.
    Every tool that can reach a document takes it: a scope the model is merely told
    about is a scope it can forget, and the reader chose these folders.
    """
    if name == "search_corpus":
        limit = max(1, min(int(payload.get("limit") or 20), 60))
        hits = search.search(
            payload.get("query", ""),
            limit=limit,
            workstream=payload.get("workstream"),
            doc_type=payload.get("doc_type"),
            doc_id=payload.get("doc_id"),
            scope=scope,
        )
        return {"n_hits": len(hits), "hits": hits}
    if name == "list_documents":
        result = search.list_documents(
            query=payload.get("query"),
            workstream=payload.get("workstream"),
            doc_type=payload.get("doc_type"),
            flagged=bool(payload.get("flagged")),
            duplicates=payload.get("duplicates") or "all",
            scope=scope,
            limit=max(1, min(int(payload.get("limit") or 60), 300)),
            offset=int(payload.get("offset") or 0),
        )
        for d in result["documents"]:
            for k in ("abs_path", "sha256", "error", "created_at", "extracted_at", "carded_at",
                      "mtime", "ocr_used"):
                d.pop(k, None)
        return result
    if name == "read_document":
        doc_id = payload.get("doc_id", "")
        # An explicit refusal, not empty output: the model has to be able to tell the
        # reader what it could not open, rather than reporting a gap in the corpus.
        if not search.in_scope(doc_id, scope):
            return {"error": OUT_OF_SCOPE, "doc_id": doc_id}
        return read_document(
            doc_id,
            payload.get("anchor"),
            payload.get("char_start", 0),
            payload.get("max_chars", 12_000),
        )
    if name == "document_card":
        doc_id = payload.get("doc_id", "")
        if not search.in_scope(doc_id, scope):
            return {"error": OUT_OF_SCOPE, "doc_id": doc_id}
        return document_card(doc_id, scope)
    if name == "run_python":
        return run_python(payload.get("code", ""), scope)
    if name == "create_deliverable":
        return docgen.create(payload)
    return {"error": f"unknown tool {name}"}
