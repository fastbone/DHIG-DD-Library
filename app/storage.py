"""Data folder management: what is on disk, and how to reclaim it.

Every destructive operation is explicit, reports what it removed, and is written
to the audit log. Nothing here touches the original corpus files except
``delete_extracted``, which only ever removes directories the app itself created
under ``data/uploads/extracted``.
"""

from __future__ import annotations

import shutil
import time
from pathlib import Path

from . import db, extract, manifest, search, security
from .config import settings
from .events import broker


def dir_size(path: Path) -> tuple[int, int]:
    """(bytes, files) for a directory tree; (0, 0) when absent."""
    total = files = 0
    if not path.exists():
        return 0, 0
    for p in path.rglob("*"):
        try:
            if p.is_file():
                total += p.stat().st_size
                files += 1
        except OSError:
            continue
    return total, files


def usage() -> dict:
    db_bytes = sum(
        p.stat().st_size
        for p in [settings.db_path, Path(f"{settings.db_path}-wal"), Path(f"{settings.db_path}-shm")]
        if p.exists()
    )
    areas = []
    for label, path, note in [
        ("index (sqlite)", settings.db_path, "catalogue, search index, accounts, audit"),
        ("text mirror", settings.derived_dir, "extracted text — rebuildable from the corpus"),
        ("deliverables", settings.artifacts_dir, "generated documents"),
        ("uploaded archives", settings.archives_dir, "the .zip files as uploaded"),
        ("extracted corpora", settings.extract_root, "archives expanded into folders"),
    ]:
        if label == "index (sqlite)":
            size, files = db_bytes, 1 if settings.db_path.exists() else 0
        else:
            size, files = dir_size(path)
        areas.append({"label": label, "path": str(path), "bytes": size, "files": files,
                      "note": note})

    corpus = settings.corpus_root
    corpus_size, corpus_files = dir_size(corpus) if corpus else (0, 0)

    missing = db.scalar(
        "SELECT COUNT(*) FROM occurrences o WHERE NOT EXISTS"
        " (SELECT 1 FROM documents d WHERE d.id = o.doc_id)"
    )
    return {
        "data_dir": str(settings.data_dir),
        "areas": areas,
        "total_bytes": sum(a["bytes"] for a in areas),
        "corpus": {
            "path": str(corpus) if corpus else None,
            "bytes": corpus_size,
            "files": corpus_files,
            "exists": bool(corpus and corpus.is_dir()),
        },
        "known_roots": known_roots(),
        "orphan_occurrences": missing,
        "stats": search.stats(),
        "secret_key_source": security.secret_key_source(),
    }


def known_roots() -> list[dict]:
    """Every corpus folder the app knows: extracted archives, plus any root it
    has been pointed at (including ones no longer active)."""
    roots: dict[str, dict] = {}
    for child in sorted(settings.extract_root.glob("*")):
        if child.is_dir():
            size, files = dir_size(child)
            roots[str(child)] = {"path": str(child), "name": child.name, "source": "extracted",
                                 "bytes": size, "files": files, "exists": True}
    for recorded in settings.root_history:
        if recorded in roots:
            continue
        path = Path(recorded)
        size, files = dir_size(path)
        roots[recorded] = {"path": recorded, "name": path.name, "source": "external",
                           "bytes": size, "files": files, "exists": path.is_dir()}
    active = settings.corpus_root
    if active:
        entry = roots.get(str(active))
        if entry is None:
            size, files = dir_size(active)
            entry = {"path": str(active), "name": active.name, "source": "external",
                     "bytes": size, "files": files, "exists": active.is_dir()}
            roots[str(active)] = entry
        entry["active"] = True
    for r in roots.values():
        r.setdefault("active", False)
        r.setdefault("exists", True)
        r["indexed_documents"] = db.scalar(
            "SELECT COUNT(DISTINCT doc_id) FROM occurrences WHERE abs_path LIKE ?",
            (r["path"].rstrip("/") + "/%",),
        )
    return sorted(roots.values(), key=lambda r: (not r["active"], r["name"].lower()))


# --- reclaim operations --------------------------------------------------


def clear_artifacts(*, actor: str | None = None) -> dict:
    size, files = dir_size(settings.artifacts_dir)
    for p in settings.artifacts_dir.glob("*"):
        if p.is_file():
            p.unlink(missing_ok=True)
    db.execute("DELETE FROM artifacts")
    db.audit("storage.clear_artifacts", actor=actor, detail=f"{files} files, {size} bytes")
    broker.log(f"Removed {files} generated documents ({size / 1e6:.1f} MB).", level="warn")
    return {"removed_files": files, "reclaimed_bytes": size}


def purge_missing(*, actor: str | None = None) -> dict:
    """Drop documents whose underlying files have all disappeared."""
    removed: list[str] = []
    for row in db.rows("SELECT id FROM documents"):
        paths = [r["abs_path"] for r in db.rows(
            "SELECT abs_path FROM occurrences WHERE doc_id=?", (row["id"],))]
        if paths and not any(Path(p).exists() for p in paths):
            removed.append(row["id"])
    for doc_id in removed:
        _drop_document(doc_id)
    db.execute(
        "DELETE FROM occurrences WHERE NOT EXISTS"
        " (SELECT 1 FROM documents d WHERE d.id = occurrences.doc_id)"
    )
    if removed:
        manifest.invalidate_manifest()
        broker.publish("stats_dirty")
    db.audit("storage.purge_missing", actor=actor, detail=f"{len(removed)} documents")
    broker.log(f"Purged {len(removed)} documents whose files are gone.", level="warn")
    return {"purged_documents": len(removed)}


def _drop_document(doc_id: str) -> None:
    unit_ids = [r["id"] for r in db.rows("SELECT id FROM units WHERE doc_id=?", (doc_id,))]
    for uid in unit_ids:
        db.execute("DELETE FROM units_fts WHERE rowid=?", (uid,))
    db.execute("DELETE FROM units WHERE doc_id=?", (doc_id,))
    db.execute("DELETE FROM occurrences WHERE doc_id=?", (doc_id,))
    db.execute("DELETE FROM documents WHERE id=?", (doc_id,))
    settings.text_path(doc_id).unlink(missing_ok=True)


def reset_index(*, actor: str | None = None) -> dict:
    """Forget the catalogue and the text mirror. Originals are untouched."""
    docs = db.scalar("SELECT COUNT(*) FROM documents")
    size, files = dir_size(settings.derived_dir)
    db.execute("DELETE FROM units_fts")
    db.execute("DELETE FROM units")
    db.execute("DELETE FROM occurrences")
    db.execute("DELETE FROM documents")
    for p in settings.derived_dir.glob("*.md"):
        p.unlink(missing_ok=True)
    manifest.invalidate_manifest()
    broker.publish("stats_dirty")
    db.audit("storage.reset_index", actor=actor, detail=f"{docs} documents, {files} mirror files")
    broker.log(f"Index reset: {docs} documents and {files} mirror files removed. "
               "Re-run Ingest to rebuild.", level="warn")
    return {"removed_documents": docs, "reclaimed_bytes": size}


def clear_cards(*, actor: str | None = None) -> dict:
    """Keep the text mirror, drop the catalogue cards so a sweep can be redone."""
    n = db.scalar("SELECT COUNT(*) FROM documents WHERE status='carded'")
    db.execute(
        "UPDATE documents SET status='extracted', title=NULL, doc_type=NULL, workstream=NULL,"
        " parties=NULL, period_covered=NULL, key_figures=NULL, summary=NULL, card_flags=NULL,"
        " languages=NULL, carded_at=NULL WHERE status IN ('carded','card_failed')"
    )
    manifest.invalidate_manifest()
    broker.publish("stats_dirty")
    db.audit("storage.clear_cards", actor=actor, detail=f"{n} cards")
    broker.log(f"Cleared {n} catalogue cards — run the sweep to rebuild the map.", level="warn")
    return {"cleared_cards": n}


def delete_extracted(path: str, *, actor: str | None = None) -> dict:
    """Remove one extracted corpus directory (only under the extraction root)."""
    target = Path(path).expanduser()
    if not security.is_within(target, settings.extract_root):
        raise ValueError("only directories under data/uploads/extracted can be deleted here")
    if not target.is_dir():
        raise ValueError("not a directory")
    size, files = dir_size(target)
    shutil.rmtree(target)
    db.execute("UPDATE archives SET extract_dir=NULL WHERE extract_dir=?", (str(target),))
    if settings.corpus_root and str(settings.corpus_root) == str(target.resolve()):
        settings.clear_corpus_root()
    db.audit("storage.delete_extracted", actor=actor, detail=f"{target.name}: {files} files")
    broker.log(f"Deleted extracted corpus {target.name} ({files} files, {size / 1e6:.0f} MB). "
               "Run 'purge missing' to drop its documents from the index.", level="warn")
    return {"deleted": str(target), "reclaimed_bytes": size, "files": files}


def vacuum(*, actor: str | None = None) -> dict:
    before = settings.db_path.stat().st_size if settings.db_path.exists() else 0
    db.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    db.conn().execute("VACUUM")
    after = settings.db_path.stat().st_size if settings.db_path.exists() else 0
    db.audit("storage.vacuum", actor=actor, detail=f"{before} → {after} bytes")
    return {"before_bytes": before, "after_bytes": after, "reclaimed_bytes": max(0, before - after)}


def housekeeping() -> dict:
    """Cheap periodic maintenance: expire sessions."""
    from . import auth

    removed = auth.purge_expired_sessions()
    return {"expired_sessions": removed, "at": time.time()}


OPERATIONS = {
    "clear_artifacts": clear_artifacts,
    "clear_cards": clear_cards,
    "purge_missing": purge_missing,
    "reset_index": reset_index,
    "vacuum": vacuum,
}
