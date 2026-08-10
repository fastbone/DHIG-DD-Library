"""BM25 search over the extracted corpus, and catalogue browsing."""

from __future__ import annotations

from . import db


def search(
    query: str,
    *,
    limit: int = 20,
    workstream: str | None = None,
    doc_type: str | None = None,
    family: str | None = None,
    doc_id: str | None = None,
    snippet_tokens: int = 24,
) -> list[dict]:
    match = db.fts_query(query)
    where = ["units_fts MATCH ?"]
    params: list = [match]
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
    limit: int = 100,
    offset: int = 0,
) -> dict:
    where: list[str] = ["1=1"]
    params: list = []
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
