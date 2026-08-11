#!/usr/bin/env python3
"""A stand-in for rclone, so the sync job can be tested without a tenant.

It speaks the parts of rclone's interface that `app/sync.py` depends on — the
`size --json` preflight and the `--use-json-log` stats stream — and is driven by
environment variables so one binary covers every scenario:

    FAKE_RCLONE_MODE=ok        transfer FAKE_RCLONE_FILES files, then exit 0
    FAKE_RCLONE_MODE=fail      emit an error and exit 1
    FAKE_RCLONE_MODE=hang      run until terminated (for cancellation)
    FAKE_RCLONE_MODE=badauth   fail the size probe the way bad credentials do

    FAKE_RCLONE_FILES=3        files to "transfer"
    FAKE_RCLONE_DELETES=2      deletions to report in the stats
    FAKE_RCLONE_SIZE=1048576   bytes the remote claims to hold
    FAKE_RCLONE_COUNT=3        files the remote claims to hold
    FAKE_RCLONE_WRITE_DIR=...  create real files in the destination, so ingest
                               has something to index
    FAKE_RCLONE_ENV_DUMP=path  write the env it was called with, to assert the
                               secret arrives out of band rather than in argv

Deliberately not a mock inside the test process: the real code path spawns a
subprocess and parses its stderr, and that is the part worth testing.
"""

from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

MODE = os.environ.get("FAKE_RCLONE_MODE", "ok")
FILES = int(os.environ.get("FAKE_RCLONE_FILES", "3"))
DELETES = int(os.environ.get("FAKE_RCLONE_DELETES", "0"))
SIZE = int(os.environ.get("FAKE_RCLONE_SIZE", str(1 << 20)))
COUNT = int(os.environ.get("FAKE_RCLONE_COUNT", "3"))

SAMPLES = [
    ("finance/model.xlsx", b"Revenue,412600000\nEBITDA,88200000\n"),
    ("legal/spa.md", b"# Share Purchase Agreement\n\nChange of control clause 8.4.\n"),
    ("commercial/notes.txt", b"Top customer is 31% of revenue in FY2024.\n"),
    ("hr/headcount.csv", b"dept,heads\nsales,42\neng,77\n"),
]


def emit(obj: dict) -> None:
    """rclone writes its JSON log to stderr, one object per line."""
    sys.stderr.write(json.dumps(obj) + "\n")
    sys.stderr.flush()


def dump_env() -> None:
    target = os.environ.get("FAKE_RCLONE_ENV_DUMP")
    if not target:
        return
    interesting = {k: v for k, v in os.environ.items() if k.startswith("RCLONE_")}
    interesting["_argv"] = " ".join(sys.argv[1:])
    Path(target).write_text(json.dumps(interesting, indent=2))


def do_size() -> int:
    if MODE == "badauth":
        sys.stderr.write(
            "ERROR : : error listing: invalid_client: AADSTS7000215: "
            "Invalid client secret provided.\n"
        )
        return 1
    print(json.dumps({"count": COUNT, "bytes": SIZE, "sizeless": 0}))
    return 0


def do_transfer() -> int:
    # rclone logs a non-stats line first; sync.py must tolerate it.
    sys.stderr.write("NOTICE: Config file not found - using defaults\n")
    sys.stderr.flush()

    if MODE == "hang":
        emit({"level": "info", "msg": "starting", "stats": {"transfers": 0, "checks": 0,
                                                            "bytes": 0, "errors": 0}})
        while True:
            time.sleep(0.2)

    dest = os.environ.get("FAKE_RCLONE_WRITE_DIR")
    per_file = max(1, SIZE // max(1, FILES))
    for i in range(FILES):
        if dest:
            rel, body = SAMPLES[i % len(SAMPLES)]
            path = Path(dest) / rel
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(body)
        emit({
            "level": "info",
            "msg": f"transferred {i + 1}",
            "stats": {
                "transfers": i + 1,
                "totalTransfers": FILES,
                # Unchanged files show up as checks above the transfer count.
                "checks": i + 2,
                "bytes": per_file * (i + 1),
                "deletes": DELETES,
                "errors": 1 if MODE == "fail" else 0,
            },
        })
        time.sleep(0.05)

    # rclone emits stats on its timer regardless of how much moved, so a run that
    # only deleted things still reports those deletions. Without this final line a
    # deletions-only sync would look like a no-op.
    emit({
        "level": "info",
        "msg": "finished",
        "stats": {
            "transfers": FILES,
            "totalTransfers": FILES,
            "checks": FILES + 1,
            "bytes": SIZE if FILES else 0,
            "deletes": DELETES,
            "errors": 1 if MODE == "fail" else 0,
        },
    })

    if MODE == "fail":
        emit({"level": "error", "msg": "failed to copy: 403 Forbidden"})
        sys.stderr.write("ERROR : finance/locked.xlsx: 403 Forbidden\n")
        return 1
    return 0


def main() -> int:
    dump_env()
    verb = sys.argv[1] if len(sys.argv) > 1 else ""
    if verb == "size":
        return do_size()
    if verb in {"sync", "copy"}:
        return do_transfer()
    sys.stderr.write(f"fake_rclone: unsupported verb {verb!r}\n")
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
