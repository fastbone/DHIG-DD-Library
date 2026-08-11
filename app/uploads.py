"""Archive upload and safe extraction.

Uploads stream to disk (never into memory), and extraction is deliberately
paranoid: path traversal, absolute paths, symlinks, device nodes, member-count
explosions, and compression bombs are all rejected rather than trusted, because
the archive comes from outside and lands on the server's filesystem.
"""

from __future__ import annotations

import asyncio
import hashlib
import re
import shutil
import tarfile
import time
import traceback
import uuid
import zipfile
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

from . import db, security
from .config import SUPPORTED_EXTS, settings
from .events import broker

ARCHIVE_SUFFIXES = (".zip", ".tar", ".tar.gz", ".tgz", ".tar.bz2", ".tbz2", ".tar.xz", ".txz")
CHUNK = 1 << 20
_SAFE_NAME = re.compile(r"[^A-Za-z0-9._ -]+")


class UnsafeArchive(RuntimeError):
    pass


def archive_kind(filename: str) -> str:
    low = filename.lower()
    if low.endswith(".zip"):
        return "zip"
    if low.endswith(ARCHIVE_SUFFIXES):
        return "tar"
    raise UnsafeArchive(
        f"unsupported archive type: {Path(filename).suffix or filename!r} "
        f"(accepted: {', '.join(ARCHIVE_SUFFIXES)})"
    )


def safe_label(filename: str) -> str:
    base = _SAFE_NAME.sub("_", Path(filename).name).strip("._ ") or "archive"
    return base[:120]


# --- upload --------------------------------------------------------------


async def save_upload(upload, *, actor: str | None = None) -> dict:
    """Stream an UploadFile to the archives directory and register it."""
    filename = safe_label(upload.filename or "archive.zip")
    archive_kind(filename)  # raises on unsupported type before writing anything

    archive_id = uuid.uuid4().hex[:12]
    dest = settings.archives_dir / f"{archive_id}_{filename}"
    limit = settings.max_upload_mb * 1024 * 1024
    digest = hashlib.sha256()
    written = 0

    try:
        with dest.open("wb") as out:
            while chunk := await upload.read(CHUNK):
                written += len(chunk)
                if written > limit:
                    raise UnsafeArchive(
                        f"upload exceeds the {settings.max_upload_mb} MB limit "
                        "(raise DD_MAX_UPLOAD_MB if that is intended)"
                    )
                digest.update(chunk)
                out.write(chunk)
    except Exception:
        dest.unlink(missing_ok=True)
        raise
    finally:
        await upload.close()

    if written == 0:
        dest.unlink(missing_ok=True)
        raise UnsafeArchive("uploaded file is empty")

    db.execute(
        "INSERT INTO archives(id, filename, stored_path, size_bytes, sha256, status, uploaded_by,"
        " created_at) VALUES(?,?,?,?,?, 'uploaded', ?,?)",
        (archive_id, filename, str(dest), written, digest.hexdigest(), actor, time.time()),
    )
    db.audit("archive.upload", actor=actor, detail=f"{filename} ({written / 1e6:.1f} MB)")
    broker.log(f"Uploaded {filename} ({written / 1e6:.1f} MB).", level="success")
    broker.publish("archives_dirty")
    return get_archive(archive_id)


def get_archive(archive_id: str) -> dict | None:
    row = db.one("SELECT * FROM archives WHERE id=?", (archive_id,))
    return dict(row) if row else None


def list_archives() -> list[dict]:
    return [dict(r) for r in db.rows("SELECT * FROM archives ORDER BY created_at DESC")]


def delete_archive(archive_id: str, *, drop_extracted: bool = False,
                   actor: str | None = None) -> dict:
    arc = get_archive(archive_id)
    if arc is None:
        raise ValueError("no such archive")
    Path(arc["stored_path"]).unlink(missing_ok=True)
    removed_dir = False
    if drop_extracted and arc["extract_dir"]:
        target = Path(arc["extract_dir"])
        if security.is_within(target, settings.extract_root) and target.is_dir():
            shutil.rmtree(target, ignore_errors=True)
            removed_dir = True
    db.execute("DELETE FROM archives WHERE id=?", (archive_id,))
    db.audit("archive.delete", actor=actor,
             detail=f"{arc['filename']} extracted_removed={removed_dir}")
    broker.publish("archives_dirty")
    return {"deleted": archive_id, "extracted_removed": removed_dir}


# --- extraction ----------------------------------------------------------


@dataclass
class Member:
    name: str
    size: int
    compressed: int


def _sanitise(name: str, root: Path) -> Path:
    """Resolve an archive member name to a safe path inside `root`."""
    raw = name.replace("\\", "/")
    pure = PurePosixPath(raw)
    if pure.is_absolute() or raw.startswith("/") or re.match(r"^[A-Za-z]:", raw):
        raise UnsafeArchive(f"absolute path in archive: {name!r}")
    parts = [p for p in pure.parts if p not in ("", ".")]
    if any(p == ".." for p in parts):
        raise UnsafeArchive(f"path traversal in archive: {name!r}")
    if not parts:
        raise UnsafeArchive(f"empty member name: {name!r}")
    target = root.joinpath(*parts)
    # Belt and braces: also check the resolved path, which catches a symlinked
    # parent directory created earlier in the same archive.
    if not security.is_within(target.parent if target.parent.exists() else root, root):
        raise UnsafeArchive(f"member escapes the extraction root: {name!r}")
    return target


def _members_zip(zf: zipfile.ZipFile) -> list[Member]:
    out: list[Member] = []
    for info in zf.infolist():
        if info.is_dir():
            continue
        # Unix mode lives in the top 16 bits; 0o120000 marks a symlink.
        mode = info.external_attr >> 16
        if mode and (mode & 0o170000) == 0o120000:
            raise UnsafeArchive(f"archive contains a symlink: {info.filename!r}")
        out.append(Member(info.filename, info.file_size, info.compress_size or 1))
    return out


def _members_tar(tf: tarfile.TarFile) -> list[Member]:
    out: list[Member] = []
    for info in tf.getmembers():
        if info.isdir():
            continue
        if info.issym() or info.islnk():
            raise UnsafeArchive(f"archive contains a link: {info.name!r}")
        if not info.isfile():
            raise UnsafeArchive(f"archive contains a special file: {info.name!r}")
        out.append(Member(info.name, info.size, max(info.size, 1)))
    return out


def _check_limits(members: list[Member], archive_size: int) -> int:
    if len(members) > settings.max_archive_members:
        raise UnsafeArchive(
            f"{len(members)} members exceeds the limit of {settings.max_archive_members}"
        )
    total = sum(m.size for m in members)
    cap = settings.max_extract_gb * 1024**3
    if total > cap:
        raise UnsafeArchive(
            f"uncompressed size {total / 1e9:.1f} GB exceeds the "
            f"{settings.max_extract_gb} GB limit"
        )
    if archive_size > 0:
        ratio = total / archive_size
        if ratio > settings.max_compression_ratio:
            raise UnsafeArchive(
                f"compression ratio {ratio:.0f}:1 looks like a decompression bomb "
                f"(limit {settings.max_compression_ratio}:1)"
            )
    return total


class ExtractJob:
    """Expands one uploaded archive into its own directory under `extract_root`."""

    def __init__(self, archive_id: str, *, actor: str | None = None) -> None:
        self.id = f"extract-{uuid.uuid4().hex[:8]}"
        self.archive_id = archive_id
        self.actor = actor
        self.cancel = asyncio.Event()
        self.total = 0
        self.done = 0
        self.skipped = 0
        self.bytes_written = 0
        self.target: Path | None = None

    def _publish(self, status: str = "running", message: str = "") -> None:
        # `skipped` is a member refused for safety, not a failure — the job only
        # fails as a whole, so `failed` stays 0.
        broker.publish(
            "job", job_id=self.id, job_kind="extract", status=status, total=self.total,
            done=self.done, failed=0, skipped=self.skipped,
            bytes_done=self.bytes_written, message=message,
        )

    def _extract(self, arc: dict) -> dict:
        """Blocking worker — runs in a thread."""
        source = Path(arc["stored_path"])
        if not source.exists():
            raise UnsafeArchive("the uploaded archive is no longer on disk")
        kind = archive_kind(arc["filename"])
        stem = safe_label(Path(arc["filename"]).name)
        for suffix in ARCHIVE_SUFFIXES:
            if stem.lower().endswith(suffix):
                stem = stem[: -len(suffix)]
                break
        target = settings.extract_root / f"{arc['id']}_{stem or 'archive'}"
        target.mkdir(parents=True, exist_ok=True)
        self.target = target

        opener = (
            (lambda: zipfile.ZipFile(source))
            if kind == "zip"
            else (lambda: tarfile.open(source, "r:*"))
        )
        with opener() as handle:
            members = _members_zip(handle) if kind == "zip" else _members_tar(handle)
            declared = _check_limits(members, arc["size_bytes"] or 0)
            self.total = len(members)
            broker.log(
                f"Extracting {arc['filename']}: {self.total} files, "
                f"{declared / 1e6:.0f} MB uncompressed → {target.name}"
            )
            self._publish(message="extracting")

            budget = settings.max_extract_gb * 1024**3
            for member in members:
                if self.cancel.is_set():
                    break
                try:
                    dest = _sanitise(member.name, target)
                except UnsafeArchive as exc:
                    self.skipped += 1
                    broker.log(f"  skipped: {exc}", level="warn")
                    continue
                dest.parent.mkdir(parents=True, exist_ok=True)
                if dest.parent.is_symlink() or not security.is_within(dest.parent, target):
                    self.skipped += 1
                    broker.log(f"  skipped (unsafe parent): {member.name}", level="warn")
                    continue
                try:
                    src = (
                        handle.open(member.name)
                        if kind == "zip"
                        else handle.extractfile(member.name)
                    )
                    if src is None:
                        raise UnsafeArchive("member is not a readable file")
                    with src, dest.open("wb") as out:
                        while chunk := src.read(CHUNK):
                            self.bytes_written += len(chunk)
                            if self.bytes_written > budget:
                                raise UnsafeArchive(
                                    "extraction exceeded the size budget mid-stream "
                                    "(the archive lied about its member sizes)"
                                )
                            out.write(chunk)
                except UnsafeArchive:
                    raise
                except Exception as exc:  # noqa: BLE001 — one bad member must not abort the rest
                    self.skipped += 1
                    dest.unlink(missing_ok=True)
                    broker.log(
                        f"  failed: {member.name}: {type(exc).__name__}: {exc}",
                        level="warn",
                        source="upload",
                        job_id=self.id,
                        context={"member": member.name,
                                 "exc_type": type(exc).__name__,
                                 "traceback": traceback.format_exc(limit=8)},
                    )
                    continue
                self.done += 1
                if self.done % 25 == 0 or self.done == self.total:
                    self._publish()

        supported = sum(
            1
            for p in target.rglob("*")
            if p.is_file() and p.suffix.lower() in SUPPORTED_EXTS
        )
        return {"target": target, "supported": supported}

    async def run(self, *, then_ingest: bool = False) -> None:
        arc = get_archive(self.archive_id)
        if arc is None:
            broker.log("Extraction aborted: archive not found.", level="error")
            return
        db.execute("UPDATE archives SET status='extracting', error=NULL WHERE id=?",
                   (self.archive_id,))
        db.job_upsert(self.id, kind="extract", status="running",
                      message=f"extracting {arc['filename']}")
        started = time.time()
        try:
            result = await asyncio.to_thread(self._extract, arc)
        except Exception as exc:  # noqa: BLE001
            msg = f"{type(exc).__name__}: {exc}"
            db.execute("UPDATE archives SET status='failed', error=? WHERE id=?",
                       (msg[:400], self.archive_id))
            db.job_upsert(self.id, status="failed", message=msg, finished_at=time.time())
            broker.log(
                f"Extraction failed: {msg}",
                level="error",
                source="upload",
                job_id=self.id,
                context={"archive": arc["filename"], "exc_type": type(exc).__name__,
                         "traceback": traceback.format_exc(limit=12)},
            )
            self._publish("failed", msg)
            broker.publish("archives_dirty")
            return

        status = "cancelled" if self.cancel.is_set() else "extracted"
        db.execute(
            "UPDATE archives SET status=?, extract_dir=?, n_files=?, n_skipped=?,"
            " bytes_written=?, extracted_at=? WHERE id=?",
            (status, str(result["target"]), self.done, self.skipped, self.bytes_written,
             time.time(), self.archive_id),
        )
        written = self.bytes_written
        size = f"{written / 1e9:.1f} GB" if written >= 1e9 else (
            f"{written / 1e6:.1f} MB" if written >= 1e6 else f"{written / 1e3:.1f} KB"
        )
        msg = (
            f"Extracted {self.done}/{self.total} files ({size}) in "
            f"{time.time() - started:.0f}s · {result['supported']} ingestable"
            + (f" · {self.skipped} refused" if self.skipped else "")
        )
        db.job_upsert(self.id, status="done" if status == "extracted" else status,
                      total=self.total, done=self.done, failed=self.skipped, message=msg,
                      finished_at=time.time())
        broker.log(msg, level="success")
        self._publish("done" if status == "extracted" else status, msg)
        broker.publish("archives_dirty")
        db.audit("archive.extract", actor=self.actor,
                 detail=f"{arc['filename']} → {result['target'].name} ({self.done} files)")

        if then_ingest and status == "extracted" and not self.cancel.is_set():
            from . import ingest, manifest

            broker.log(f"Auto-ingesting {result['target'].name} …")
            settings.set_corpus_root(str(result["target"]))
            job = ingest.IngestJob(result["target"], ocr=settings.ocr_enabled)
            await job.run()
            manifest.invalidate_manifest()
