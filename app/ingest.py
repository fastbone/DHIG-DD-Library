"""Corpus ingest: walk → hash → extract → mirror → index → dedupe.

Runs as a cancellable background job that streams progress to the UI. Safe to
re-run: a file whose SHA-256 already has a text mirror on disk is skipped, so
adding 200 documents to a data room costs 200 documents of work, not 5,000.
"""

from __future__ import annotations

import asyncio
import hashlib
import re
import time
import uuid
from pathlib import Path

from . import db, extract, security
from .config import SUPPORTED_EXTS, settings
from .events import broker

# Junk directory names, matched against components *inside* the corpus root.
# Deliberately no "data" here: the app's own data directory is excluded by
# identity below, and matching that name would skip any corpus whose path
# happens to contain it — which in Docker is every corpus on the data volume.
SKIP_DIRS = {".git", "__pycache__", "node_modules", ".venv", ".DS_Store"}
_WORD = re.compile(r"[a-z0-9]{2,}")

# Version noise that a data room sprays over otherwise identical filenames.
_VERSION_NOISE = re.compile(
    r"(?<![a-z0-9])(?:"
    r"v?\d+(?:\.\d+)*|ver|version|rev|revision|draft|final|fin|clean|copy|kopie|"
    r"signed|executed|conformed|latest|neu|new|old|bak|backup|comments?|markup|"
    r"\(\d+\)|\[\d+\]|\d{1,2}[._-]\d{1,2}[._-]\d{2,4}|\d{6,8}"
    r")(?![a-z0-9])",
    re.IGNORECASE,
)
SHINGLE_K = 5
JACCARD_THRESHOLD = 0.75
MAX_BLOCK = 60


def sha256_file(path: Path, chunk: int = 1 << 20) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        while block := fh.read(chunk):
            h.update(block)
    return h.hexdigest()


def normalized_stem(filename: str) -> str:
    """Filename with version noise stripped, for near-duplicate blocking."""
    stem = Path(filename).stem.lower()
    stem = _VERSION_NOISE.sub(" ", stem)
    return re.sub(r"[^a-z]+", "", stem)


def shingles(text: str, k: int = SHINGLE_K) -> frozenset[int]:
    """Hashed word k-shingles. Hashes rather than strings to keep memory sane."""
    words = _WORD.findall(text.lower())
    if not words:
        return frozenset()
    if len(words) <= k:
        return frozenset({hash(" ".join(words))})
    return frozenset(hash(" ".join(words[i : i + k])) for i in range(len(words) - k + 1))


def jaccard(a: frozenset[int], b: frozenset[int]) -> float:
    if not a or not b:
        return 0.0
    inter = len(a & b)
    return inter / (len(a) + len(b) - inter)


def scan(root: Path) -> list[Path]:
    found: list[Path] = []
    limit = settings.max_file_mb * 1024 * 1024
    # The app's own data directory is only inside the corpus when someone points
    # the root at the project directory; skip it then so ingest doesn't index its
    # own text mirror. Resolved once, into a component prefix, so the per-file
    # test is a tuple compare rather than a path resolution.
    #
    # A corpus root *inside* the data volume is normal and must keep working —
    # that is where uploaded archives are expanded and remote libraries are
    # mirrored — so this deliberately does not fire when data_dir contains root.
    data_prefix: tuple[str, ...] | None = None
    resolved_root = root.resolve()
    data_dir = settings.data_dir.resolve()
    if data_dir != resolved_root and security.is_within(data_dir, resolved_root):
        data_prefix = data_dir.relative_to(resolved_root).parts

    for p in root.rglob("*"):
        try:
            rel = p.relative_to(root)
        except ValueError:  # rglob shouldn't produce these, but don't assume
            continue
        # Matched relative to the root, so a junk name in the path *above* the
        # corpus (a checkout under ~/node_modules, say) cannot hide the corpus.
        if any(part in SKIP_DIRS or part.startswith("~$") for part in rel.parts):
            continue
        if data_prefix and rel.parts[: len(data_prefix)] == data_prefix:
            continue
        if not p.is_file():
            continue
        if p.suffix.lower() not in SUPPORTED_EXTS:
            continue
        try:
            if p.stat().st_size > limit or p.stat().st_size == 0:
                continue
        except OSError:
            continue
        found.append(p)
    return sorted(found)


def _extract_one(path: Path, root: Path, ocr: bool) -> dict:
    """Blocking worker — runs in a thread."""
    stat = path.stat()
    digest = sha256_file(path)
    doc_id = digest[:16]
    family = SUPPORTED_EXTS[path.suffix.lower()]
    rel = str(path.relative_to(root))
    base = {
        "id": doc_id,
        "sha256": digest,
        "rel_path": rel,
        "abs_path": str(path),
        "filename": path.name,
        "ext": path.suffix.lower(),
        "family": family,
        "size_bytes": stat.st_size,
        "mtime": stat.st_mtime,
    }

    db.execute(
        "INSERT INTO occurrences(abs_path, doc_id, rel_path, mtime, size_bytes, seen_at)"
        " VALUES(?,?,?,?,?,?) ON CONFLICT(abs_path) DO UPDATE SET doc_id=excluded.doc_id,"
        " rel_path=excluded.rel_path, mtime=excluded.mtime, size_bytes=excluded.size_bytes,"
        " seen_at=excluded.seen_at",
        (str(path), doc_id, rel, stat.st_mtime, stat.st_size, time.time()),
    )

    existing = db.one("SELECT status, n_units FROM documents WHERE id=?", (doc_id,))
    if existing and existing["status"] in {"extracted", "carded"} and settings.text_path(doc_id).exists():
        # Already have this content. The document keeps its first-seen path as
        # the canonical one; the extra path is recorded in `occurrences`.
        return {**base, "skipped": True, "n_units": existing["n_units"] or 0}

    try:
        text, units, ocr_used = extract.extract(path, family, ocr=ocr)
    except Exception as exc:  # noqa: BLE001 — one bad file must not kill the sweep
        db.execute(
            "INSERT INTO documents(id, sha256, rel_path, abs_path, filename, ext, family,"
            " size_bytes, mtime, status, error, created_at)"
            " VALUES(?,?,?,?,?,?,?,?,?, 'extract_failed', ?, ?)"
            " ON CONFLICT(id) DO UPDATE SET status='extract_failed', error=excluded.error",
            (
                doc_id, digest, rel, str(path), path.name, path.suffix.lower(), family,
                stat.st_size, stat.st_mtime, f"{type(exc).__name__}: {exc}"[:500], time.time(),
            ),
        )
        return {**base, "error": f"{type(exc).__name__}: {exc}"}

    extract.write_mirror(doc_id, text)
    db.execute(
        "INSERT INTO documents(id, sha256, rel_path, abs_path, filename, ext, family, size_bytes,"
        " mtime, status, n_units, n_chars, ocr_used, extracted_at, created_at)"
        " VALUES(?,?,?,?,?,?,?,?,?, 'extracted', ?,?,?,?,?)"
        " ON CONFLICT(id) DO UPDATE SET status='extracted', error=NULL, n_units=excluded.n_units,"
        " n_chars=excluded.n_chars, ocr_used=excluded.ocr_used, extracted_at=excluded.extracted_at",
        (
            doc_id, digest, rel, str(path), path.name, path.suffix.lower(), family, stat.st_size,
            stat.st_mtime, len(units), len(text), int(ocr_used), time.time(), time.time(),
        ),
    )
    db.replace_units(
        doc_id,
        [
            {
                "ordinal": u.ordinal,
                "anchor": u.anchor,
                "kind": u.kind,
                "char_start": u.char_start,
                "char_end": u.char_end,
                "text": u.text,
            }
            for u in units
        ],
    )
    return {**base, "n_units": len(units), "n_chars": len(text)}


def mark_duplicates() -> dict:
    """Group near-duplicate versions of the same document.

    Byte-identical files never reach here as separate rows — they share a
    content-addressed id, so their extra paths live in ``occurrences``. What
    this catches is the ``_v3`` / ``_v3_FINAL`` / ``_v3_FINAL_JD`` family a data
    room is full of: the same document with one figure changed.

    Comparing every pair is quadratic, so candidates are *blocked* first — by
    version-stripped filename, and by an identical first unit (which catches a
    renamed copy). Only candidate pairs get an exact Jaccard comparison over
    5-word shingles. A near-duplicate that shares neither signal is not found;
    that is the documented trade-off for staying linear on a large data room.
    """
    docs = db.rows(
        "SELECT id, filename, n_chars FROM documents"
        " WHERE status IN ('extracted','carded') ORDER BY filename"
    )
    exact = db.scalar("SELECT COUNT(*) - COUNT(DISTINCT doc_id) FROM occurrences")
    if not docs:
        return {"exact_duplicates": exact, "near_duplicate_extras": 0, "families": 0, "skipped_blocks": 0}

    texts: dict[str, str] = {d["id"]: extract.read_mirror(d["id"], 0, 200_000) for d in docs}

    blocks: dict[str, list[str]] = {}
    for d in docs:
        stem = normalized_stem(d["filename"])
        if len(stem) >= 4:
            blocks.setdefault("n:" + stem, []).append(d["id"])
        head = texts[d["id"]][:1500].strip()
        if len(head) > 200:
            blocks.setdefault(
                "h:" + hashlib.blake2b(head.encode(), digest_size=8).hexdigest(), []
            ).append(d["id"])

    candidates: set[tuple[str, str]] = set()
    skipped = 0
    for members in blocks.values():
        uniq = sorted(set(members))
        if len(uniq) < 2:
            continue
        if len(uniq) > MAX_BLOCK:
            skipped += 1
            uniq = uniq[:MAX_BLOCK]
        for i in range(len(uniq)):
            for j in range(i + 1, len(uniq)):
                candidates.add((uniq[i], uniq[j]))

    sig: dict[str, frozenset[int]] = {}
    parent: dict[str, str] = {d["id"]: d["id"] for d in docs}

    def find(x: str) -> str:
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    for a, b in candidates:
        for doc_id in (a, b):
            if doc_id not in sig:
                sig[doc_id] = shingles(texts[doc_id])
        if jaccard(sig[a], sig[b]) >= JACCARD_THRESHOLD:
            ra, rb = find(a), find(b)
            if ra != rb:
                parent[rb] = ra

    groups: dict[str, list[str]] = {}
    for d in docs:
        groups.setdefault(find(d["id"]), []).append(d["id"])
    near = families = 0
    for root_id, members in groups.items():
        group = root_id if len(members) > 1 else None
        if group:
            families += 1
            near += len(members) - 1
        for m in members:
            db.execute("UPDATE documents SET dupe_group=? WHERE id=?", (group, m))
    return {
        "exact_duplicates": exact,
        "near_duplicate_extras": near,
        "families": families,
        "candidate_pairs": len(candidates),
        "skipped_blocks": skipped,
    }


class IngestJob:
    def __init__(self, root: Path, ocr: bool) -> None:
        self.id = f"ingest-{uuid.uuid4().hex[:8]}"
        self.root = root
        self.ocr = ocr
        self.cancel = asyncio.Event()
        self.done = 0
        self.failed = 0
        self.skipped = 0
        self.total = 0
        self.bytes_done = 0

    def _publish(self, message: str | None = None) -> None:
        broker.publish(
            "job",
            job_id=self.id,
            job_kind="ingest",
            status="running",
            total=self.total,
            done=self.done,
            failed=self.failed,
            skipped=self.skipped,
            bytes_done=self.bytes_done,
            message=message or "",
        )

    async def run(self) -> None:
        started = time.time()
        db.job_upsert(self.id, kind="ingest", status="running", message=f"scanning {self.root}")
        broker.log(f"Scanning {self.root} …")
        self._publish("scanning")

        paths = await asyncio.to_thread(scan, self.root)
        self.total = len(paths)
        total_bytes = sum(p.stat().st_size for p in paths)
        db.job_upsert(self.id, total=self.total, message=f"{self.total} files")
        broker.log(
            f"Found {self.total} supported files ({total_bytes / 1e9:.2f} GB) under {self.root}"
        )
        self._publish(f"{self.total} files queued")

        sem = asyncio.Semaphore(settings.extract_workers)

        async def worker(path: Path) -> None:
            if self.cancel.is_set():
                return
            async with sem:
                if self.cancel.is_set():
                    return
                try:
                    result = await asyncio.to_thread(_extract_one, path, self.root, self.ocr)
                except Exception as exc:  # noqa: BLE001
                    self.failed += 1
                    broker.log(f"{path.name}: {type(exc).__name__}: {exc}", level="error")
                else:
                    if result.get("error"):
                        self.failed += 1
                        broker.log(f"{path.name}: {result['error']}", level="warn")
                    elif result.get("skipped"):
                        self.skipped += 1
                    self.bytes_done += result.get("size_bytes", 0)
                self.done += 1
                if self.done % 5 == 0 or self.done == self.total:
                    db.job_upsert(self.id, done=self.done, failed=self.failed)
                    self._publish()

        await asyncio.gather(*(worker(p) for p in paths))

        if self.cancel.is_set():
            db.job_upsert(self.id, status="cancelled", finished_at=time.time())
            broker.publish("job", job_id=self.id, job_kind="ingest", status="cancelled",
                           total=self.total, done=self.done, failed=self.failed)
            broker.log("Ingest cancelled.", level="warn")
            return

        mismatches = await asyncio.to_thread(db.unit_count_mismatches)
        if mismatches:
            broker.log(
                f"Index integrity: {len(mismatches)} document(s) have missing search units; "
                "re-run ingest for those files.",
                level="warn",
            )
            for m in mismatches[:10]:
                broker.log(f"  {m['rel_path']}: expected {m['n_units']}, indexed {m['actual']}",
                           level="warn")

        broker.log("Detecting duplicates …")
        self._publish("deduplicating")
        dupes = await asyncio.to_thread(mark_duplicates)
        elapsed = time.time() - started
        msg = (
            f"Ingested {self.done - self.failed - self.skipped} new, {self.skipped} unchanged, "
            f"{self.failed} failed in {elapsed:.0f}s · "
            f"{dupes['exact_duplicates']} exact + {dupes['near_duplicate_extras']} near duplicates"
        )
        db.job_upsert(
            self.id, status="done", done=self.done, failed=self.failed,
            message=msg, finished_at=time.time(),
        )
        broker.log(msg, level="success")
        broker.publish("job", job_id=self.id, job_kind="ingest", status="done", total=self.total,
                       done=self.done, failed=self.failed, skipped=self.skipped, message=msg)
        broker.publish("stats_dirty")
