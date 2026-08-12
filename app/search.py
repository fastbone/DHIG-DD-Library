"""BM25 search over the extracted corpus, and catalogue browsing.

Both accept a **scope**: a list of absolute path prefixes narrowing every query to
part of the corpus. `abs_path` is the key rather than `rel_path`, which is relative
to whichever root was active at ingest and therefore collides between corpora.
"""

from __future__ import annotations

from . import db


def _like_prefix(prefix: str) -> str:
    """A folder prefix as a LIKE pattern. Real paths contain % and _ ."""
    escaped = prefix.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return escaped.rstrip("/") + "/%"


def scope_clause(scope, table: str = "documents") -> tuple[str, list]:
    """SQL and params restricting `table` to documents under any of `scope`.

    Returns ``("", [])`` for an empty scope, so callers can splice it in
    unconditionally.

    A document is in scope if *any* of its paths is, not just its canonical one.
    Documents are content-addressed: the same bytes filed in two folders collapse
    to one row whose `abs_path` is wherever it was seen first, with the rest in
    `occurrences`. Matching only `abs_path` would hide a document that genuinely
    sits in the folder the reader selected, purely because an identical copy was
    ingested from elsewhere first.
    """
    prefixes = [p for p in (scope or []) if p]
    if not prefixes:
        return "", []
    parts: list[str] = []
    params: list = []
    for prefix in prefixes:
        pattern = _like_prefix(prefix)
        parts.append(
            f"({table}.abs_path LIKE ? ESCAPE '\\'"
            f" OR EXISTS (SELECT 1 FROM occurrences o2 WHERE o2.doc_id = {table}.id"
            f" AND o2.abs_path LIKE ? ESCAPE '\\'))"
        )
        params.extend([pattern, pattern])
    return "(" + " OR ".join(parts) + ")", params


def in_scope(doc_id: str, scope) -> bool:
    """Is this document reachable under `scope`? An empty scope reaches everything."""
    scope_sql, params = scope_clause(scope)
    if not scope_sql:
        return True
    return bool(
        db.scalar(
            f"SELECT COUNT(*) FROM documents WHERE id=? AND {scope_sql}", [doc_id, *params]
        )
    )


def folder_tree(roots: list[str] | None = None, min_docs: int = 1) -> list[dict]:
    """Folders that actually contain indexed documents, with counts.

    Built by grouping the paths already in the database rather than walking the
    filesystem: the point of the picker is to offer only folders with something
    in them, and to say how much, so the choice is informed. A directory full of
    files that failed to extract would be a trap if the tree came from disk.

    Counts are per document, deduplicated across a folder's subtree and across a
    document's several filings, so a node's count is the number of distinct
    documents a scope of that node would expose.

    `roots` trims the ancestors above each corpus root. Without it the tree starts
    at "/" and every level of the host's directory layout becomes a pickable node —
    true, useless, and more than the picker should show.
    """
    keep = [r.rstrip("/") for r in (roots or []) if r]
    rows = db.rows(
        "SELECT DISTINCT doc_id, abs_path FROM occurrences"
        " UNION SELECT id AS doc_id, abs_path FROM documents"
    )
    carded = {
        r["id"]
        for r in db.rows("SELECT id FROM documents WHERE status='carded'")
    }
    docs: dict[str, set[str]] = {}
    indexed: dict[str, set[str]] = {}
    for r in rows:
        path = r["abs_path"] or ""
        doc_id = r["doc_id"]
        folder = path.rsplit("/", 1)[0] if "/" in path else path
        # Every ancestor gets the document, so a parent's count includes its subtree.
        parts = folder.split("/")
        for i in range(1, len(parts) + 1):
            prefix = "/".join(parts[:i])
            if not prefix:
                continue
            docs.setdefault(prefix, set()).add(doc_id)
            if doc_id in carded:
                indexed.setdefault(prefix, set()).add(doc_id)
    def under(prefix: str) -> str | None:
        """The longest root this prefix sits in, or None if it sits in none."""
        matches = [r for r in keep if prefix == r or prefix.startswith(r + "/")]
        return max(matches, key=len) if matches else None

    out = []
    for prefix, ids in sorted(docs.items()):
        if len(ids) < min_docs:
            continue
        root = under(prefix) if keep else ""
        if keep and root is None:
            continue
        # Depth relative to the root, so the picker can indent without knowing
        # how deep on the host the data room happens to live.
        rest = prefix[len(root):].strip("/") if root else prefix.strip("/")
        out.append({
            "path": prefix,
            "name": prefix.rsplit("/", 1)[-1] or prefix,
            "root": root,
            "depth": len(rest.split("/")) if rest else 0,
            "n_documents": len(ids),
            "n_indexed": len(indexed.get(prefix, ())),
        })
    return out


def search(
    query: str,
    *,
    limit: int = 20,
    workstream: str | None = None,
    doc_type: str | None = None,
    family: str | None = None,
    doc_id: str | None = None,
    scope=None,
    snippet_tokens: int = 24,
) -> list[dict]:
    match = db.fts_query(query)
    where = ["units_fts MATCH ?"]
    params: list = [match]
    scope_sql, scope_params = scope_clause(scope, table="d")
    if scope_sql:
        where.append(scope_sql)
        params.extend(scope_params)
    if workstream:
        where.append("d.workstream = ?")
        params.append(workstream)
    if doc_type:
        where.append("d.doc_type LIKE ?")
        params.append(f"%{doc_type}%")
    if family:
        where.append("d.family = ?")
        params.append(family)
    if doc_id:
        where.append("d.id = ?")
        params.append(doc_id)
    params.append(limit)

    sql = f"""
        SELECT f.rowid AS unit_id, f.doc_id, f.anchor,
               snippet(units_fts, 0, '«', '»', ' … ', {int(snippet_tokens)}) AS snippet,
               bm25(units_fts, 10.0) AS score,
               d.title, d.rel_path, d.workstream, d.doc_type, d.family,
               d.period_covered, d.summary,
               u.char_start, u.char_end, u.kind
        FROM units_fts f
        JOIN documents d ON d.id = f.doc_id
        LEFT JOIN units u ON u.id = f.rowid
        WHERE {' AND '.join(where)}
        ORDER BY score
        LIMIT ?
    """
    try:
        found = db.rows(sql, params)
    except Exception as exc:  # malformed MATCH despite sanitising
        return [{"error": f"search failed: {exc}", "query": match}]
    return [
        {
            "doc_id": r["doc_id"],
            "anchor": r["anchor"],
            "citation": f"{r['doc_id']}:{r['anchor']}",
            "title": r["title"] or r["rel_path"],
            "rel_path": r["rel_path"],
            "workstream": r["workstream"],
            "doc_type": r["doc_type"],
            "family": r["family"],
            "period": r["period_covered"],
            "snippet": (r["snippet"] or "").replace("\n", " "),
            "score": round(-(r["score"] or 0), 3),
            "char_start": r["char_start"],
            "char_end": r["char_end"],
        }
        for r in found
    ]


def list_documents(
    *,
    query: str | None = None,
    workstream: str | None = None,
    doc_type: str | None = None,
    status: str | None = None,
    flagged: bool = False,
    duplicates: str = "all",  # all | near_only | multi_path
    scope=None,
    limit: int = 100,
    offset: int = 0,
) -> dict:
    where: list[str] = ["1=1"]
    params: list = []
    scope_sql, scope_params = scope_clause(scope)
    if scope_sql:
        where.append(scope_sql)
        params.extend(scope_params)
    if query:
        where.append(
            "(filename LIKE ? OR rel_path LIKE ? OR title LIKE ? OR summary LIKE ?"
            " OR doc_type LIKE ? OR parties LIKE ?)"
        )
        params.extend([f"%{query}%"] * 6)
    if workstream:
        where.append("workstream = ?")
        params.append(workstream)
    if doc_type:
        where.append("doc_type LIKE ?")
        params.append(f"%{doc_type}%")
    if status:
        where.append("status = ?")
        params.append(status)
    if flagged:
        where.append("card_flags IS NOT NULL AND card_flags NOT IN ('', '[]')")
    if duplicates == "near_only":
        where.append("dupe_group IS NOT NULL")
    elif duplicates == "multi_path":
        where.append("(SELECT COUNT(*) FROM occurrences o WHERE o.doc_id = documents.id) > 1")

    clause = " AND ".join(where)
    total = db.scalar(f"SELECT COUNT(*) FROM documents WHERE {clause}", params)
    found = db.rows(
        f"SELECT *, (SELECT COUNT(*) FROM occurrences o WHERE o.doc_id = documents.id) AS n_paths"
        f" FROM documents WHERE {clause}"
        " ORDER BY workstream IS NULL, workstream, doc_type, rel_path LIMIT ? OFFSET ?",
        (*params, limit, offset),
    )
    return {
        "total": total,
        "limit": limit,
        "offset": offset,
        "documents": [db.doc_dict(r) for r in found],
    }


def stats() -> dict:
    by_status = {
        r["status"]: r["n"]
        for r in db.rows("SELECT status, COUNT(*) n FROM documents GROUP BY status")
    }
    by_workstream = [
        {"workstream": r["workstream"] or "unindexed", "n": r["n"], "bytes": r["bytes"]}
        for r in db.rows(
            "SELECT workstream, COUNT(*) n, SUM(size_bytes) bytes FROM documents"
            " GROUP BY workstream ORDER BY n DESC"
        )
    ]
    by_family = [
        {"family": r["family"], "n": r["n"], "bytes": r["bytes"] or 0}
        for r in db.rows(
            "SELECT family, COUNT(*) n, SUM(size_bytes) bytes FROM documents"
            " GROUP BY family ORDER BY n DESC"
        )
    ]
    return {
        "files_seen": db.scalar("SELECT COUNT(*) FROM occurrences"),
        "documents": db.scalar("SELECT COUNT(*) FROM documents"),
        "exact_duplicates": db.scalar("SELECT COUNT(*) - COUNT(DISTINCT doc_id) FROM occurrences"),
        "near_dupe_groups": db.scalar(
            "SELECT COUNT(DISTINCT dupe_group) FROM documents WHERE dupe_group IS NOT NULL"
        ),
        "units": db.scalar("SELECT COUNT(*) FROM units"),
        "chars": db.scalar("SELECT SUM(n_chars) FROM documents"),
        "bytes": db.scalar("SELECT SUM(size_bytes) FROM documents"),
        "flagged": db.scalar(
            "SELECT COUNT(*) FROM documents WHERE card_flags IS NOT NULL"
            " AND card_flags NOT IN ('', '[]')"
        ),
        "by_status": by_status,
        "by_workstream": by_workstream,
        "by_family": by_family,
    }
